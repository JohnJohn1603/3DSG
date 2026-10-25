"""Geometry-gated direct room membership, ported from RegionManager."""
from __future__ import annotations
from dataclasses import dataclass
import math
from numbers import Real
from typing import Literal, Sequence
from ..config.schema import RoomConfig
from ..contracts.errors import ContractError
from ..contracts.regions import CanonicalRegion, Polygon2D
from .geometry import distance_to_polygon, intersection_area, point_in_polygon

EntityKind=Literal["agent","object","navigation"]
@dataclass(frozen=True,slots=True)
class RoomEntity:
    """Direct graph entity; optional XY bounds use (min_x,min_y,max_x,max_y)."""
    entity_id:str
    kind:EntityKind
    center_xy:tuple[float,float]
    bounds_xy:tuple[float,float,float,float]|None=None
    provenance:str="caller_supplied_graph_state"
    def __post_init__(self):
        if type(self.entity_id) is not str or not self.entity_id: raise ContractError("room entity ID must be a non-empty string")
        if type(self.provenance) is not str or not self.provenance: raise ContractError("room entity provenance must be a non-empty string")
        if self.kind not in ("agent","object","navigation"): raise ContractError("unsupported direct room member kind")
        if type(self.center_xy) is not tuple or len(self.center_xy)!=2 or any(isinstance(v,bool) or not isinstance(v,Real) or not math.isfinite(float(v)) for v in self.center_xy): raise ContractError("room entity center must be a finite numeric XY tuple")
        if self.bounds_xy is not None:
            if type(self.bounds_xy) is not tuple or len(self.bounds_xy)!=4 or any(isinstance(v,bool) or not isinstance(v,Real) or not math.isfinite(float(v)) for v in self.bounds_xy):
                raise ContractError("room entity bounds_xy must be a finite numeric tuple (min_x,min_y,max_x,max_y)")
            min_x,min_y,max_x,max_y=map(float,self.bounds_xy)
            if not min_x<max_x or not min_y<max_y:
                raise ContractError("room entity footprint bounds must have positive width and height")
    @property
    def footprint_area_m2(self)->float|None:
        if self.bounds_xy is None:return None
        min_x,min_y,max_x,max_y=self.bounds_xy
        return (max_x-min_x)*(max_y-min_y)

def room_entity_from_nav_node(node):
    """Explicit M7→M8 adapter: preserve NavNode ID, block center and XY bounds."""
    from ..navigation.blocks import NavNode
    if not isinstance(node,NavNode):raise ContractError("navigation adapter input must be NavNode")
    return RoomEntity(node.node_id,"navigation",node.center_xy,node.bounds_xy,"m7_nav_node")

@dataclass(frozen=True,slots=True)
class MembershipAssignment:
    entity_id:str
    region_id:int|None
    reason:str
    plausible_region_ids:tuple[int,...]=()
    used_tiebreak:bool=False

class RegionMembershipResolver:
    def __init__(self,config:RoomConfig|None=None):
        self.config=config or RoomConfig(); self._previous:dict[str,int]={}
    def snapshot(self): return dict(self._previous)
    def resolve(self,regions:Sequence[CanonicalRegion],entities:Sequence[RoomEntity],nav_edges=()):
        by_id={int(r.persistent_id):r for r in regions if r.persistent_id is not None}
        if len(by_id)!=len(regions): raise ContractError("current regions require unique persistent IDs")
        entity_ids=[e.entity_id for e in entities]
        if len(set(entity_ids))!=len(entity_ids): raise ContractError("room entity IDs must be unique")
        nav_neighbors={}
        for edge in nav_edges:
            a=getattr(edge,"source_block",None); b=getattr(edge,"target_block",None)
            if a is None or b is None: continue
            aid=f"nav:{a[0]}:{a[1]}"; bid=f"nav:{b[0]}:{b[1]}"
            nav_neighbors.setdefault(aid,set()).add(bid)
        assignments=[]; current={}; previous=dict(self._previous)
        for entity in sorted(entities,key=lambda e:(e.kind,e.entity_id)):
            candidates=[]; prev=self._previous.get(entity.entity_id)
            for rid,region in by_id.items():
                inside=point_in_polygon(entity.center_xy,region.polygon)
                distance=distance_to_polygon(entity.center_xy,region.polygon)
                if not inside and distance>self.config.boundary_epsilon_m: continue
                # Overlap is only a tie-break among multiple candidates.  Do
                # not triangulate a potentially large region polygon when
                # point/boundary gating already leaves exactly one candidate;
                # this preserves the source ordering while avoiding repeated
                # expensive polygon work on ordinary single-room updates.
                overlap=0.0; overlap_area=0.0
                cd=math.dist(entity.center_xy,region.centroid_xy)
                neighbor_support=(sum(1 for n in nav_neighbors.get(entity.entity_id,()) if previous.get(n)==rid)
                                  if self.config.nav_neighbor_tiebreak else 0)
                candidates.append((rid,inside,distance,cd,overlap_area,overlap,neighbor_support,prev==rid))
            if entity.kind=="navigation" and len(candidates)>1 and entity.bounds_xy:
                x0,y0,x1,y1=entity.bounds_xy
                boxpoly=Polygon2D(((x0,y0),(x1,y0),(x1,y1),(x0,y1)))
                candidates=[
                    candidate[:4]
                    + (area := intersection_area(boxpoly, by_id[candidate[0]].polygon),
                       area/max((x1-x0)*(y1-y0),1e-12))
                    + candidate[6:]
                    for candidate in candidates
                ]
            if entity.kind=="navigation":
                candidates.sort(key=lambda c:(not c[7],-c[5],-c[4],c[2],c[3],-c[6],c[0]))
            else:
                candidates.sort(key=lambda c:(not c[1],not c[7],c[2],c[3],c[0]))
            chosen=candidates[0] if candidates else None
            rid=None if chosen is None else chosen[0]
            if rid is not None: current[entity.entity_id]=rid
            assignments.append(MembershipAssignment(entity.entity_id,rid,"outside" if chosen is None else ("point" if chosen[1] else "boundary"),tuple(sorted(c[0] for c in candidates)),len(candidates)>1))
        self._previous=current
        return tuple(assignments)
