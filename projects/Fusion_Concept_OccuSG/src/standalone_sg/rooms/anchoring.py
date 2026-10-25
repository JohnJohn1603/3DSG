"""Room promotion, persistent region anchors and source-style maintenance."""
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Sequence
from ..config.schema import RoomConfig
from ..contracts.errors import ContractError
from ..contracts.regions import CanonicalRegion, TrackedRegion
from .membership import MembershipAssignment, RegionMembershipResolver, RoomEntity
from .relink import select_relink

@dataclass(frozen=True,slots=True)
class RoomNode:
    room_id:int
    name:str
    anchor_region_id:int
    polygon:object
    centroid_xy:tuple[float,float]
    member_ids:tuple[str,...]
    created_reason:str
    revision:int
@dataclass(frozen=True,slots=True)
class RoomAdjacency:
    room_a:int
    room_b:int
    region_pair:tuple[int,int]
@dataclass(frozen=True,slots=True)
class RoomUpdate:
    revision:int
    rooms:tuple[RoomNode,...]
    assignments:tuple[MembershipAssignment,...]
    adjacency:tuple[RoomAdjacency,...]
    created_room_ids:tuple[int,...]
    removed_room_ids:tuple[int,...]
    relinked:tuple[tuple[int,int,int],...]
    stale:bool
    retained_track_ids:tuple[int,...]
    expired_track_ids:tuple[int,...]

class RoomAnchoringManager:
    def __init__(self,config:RoomConfig|None=None):
        self.config=config or RoomConfig(); self.membership=RegionMembershipResolver(self.config)
        self._rooms:dict[int,RoomNode]={}; self._next_id=1; self._adj:set[tuple[int,int,int,int]]=set(); self._revision=-1
        self._last_maintenance_s:float|None=None
    def snapshot(self):return tuple(self._rooms[k] for k in sorted(self._rooms))
    def maintenance_due(self, now_s:float, *, terminal:bool=False)->bool:
        """Source cadence gate (0.5 Hz by default); terminal flush is immediate."""
        if isinstance(now_s,bool) or not isinstance(now_s,(int,float)) or not math.isfinite(float(now_s)):
            raise ContractError("maintenance time must be finite")
        now=float(now_s); period=1.0/self.config.maintenance_hz
        if terminal or self._last_maintenance_s is None or now-self._last_maintenance_s>=period:
            self._last_maintenance_s=now
            return True
        return False
    def update(self,regions:Sequence[CanonicalRegion],tracks:Sequence[TrackedRegion],entities:Sequence[RoomEntity],*,revision:int,stale:bool=False,nav_edges=(),expired_track_ids=(),maintenance:bool=True):
        if type(revision) is not int or revision<0:raise ContractError("room revision must be a non-negative integer")
        if revision<self._revision:raise ContractError("room revision must not move backwards")
        current={int(r.persistent_id):r for r in regions if r.persistent_id is not None}
        if len(current)!=len(regions):raise ContractError("current regions require unique persistent IDs")
        retained_ids=tuple(sorted(t.persistent_id for t in tracks if t.status=="temporarily_missing"))
        expired_ids=tuple(sorted(set(int(i) for i in expired_track_ids)))
        if stale:
            self._revision=revision
            return RoomUpdate(revision,self.snapshot(),(),self._adjacency_output(),(),(),(),True,retained_ids,expired_ids)
        assignments=self.membership.resolve(regions,entities,nav_edges)
        assigned={a.entity_id:a.region_id for a in assignments if a.region_id is not None}
        entities_by_id={e.entity_id:e for e in entities}
        members={rid:tuple(sorted(eid for eid,r in assigned.items() if r==rid)) for rid in current}
        removed=[]; relinked=[]
        claimed=set(rid for rid in current if any(r.anchor_region_id==rid for r in self._rooms.values()))
        # Relink old anchored rooms before pruning; one old room and one new region per winner.
        candidates=[]
        for room in self.snapshot() if maintenance else ():
            if room.anchor_region_id in current:continue
            candidate=select_relink(room.anchor_region_id,room.polygon,room.centroid_xy,room.member_ids,regions,entities,claimed,
                min_old_overlap_ratio=self.config.relink_min_old_overlap_ratio,
                min_member_support_ratio=self.config.relink_min_member_support_ratio,
                min_score_margin=self.config.relink_min_score_margin)
            if candidate:candidates.append((candidate,room.room_id))
        used_rooms=set();used_regions=set()
        candidates.sort(key=lambda x:(-x[0].score,-x[0].old_overlap_ratio,x[1]))
        for c,room_id in candidates:
            if room_id in used_rooms or c.new_region_id in claimed or c.new_region_id in used_regions:continue
            room=self._rooms[room_id]; reg=current[c.new_region_id]
            self._rooms[room_id]=RoomNode(room_id,room.name,c.new_region_id,reg.polygon,reg.centroid_xy,members.get(c.new_region_id,()),room.created_reason,revision)
            claimed.add(c.new_region_id);used_rooms.add(room_id);used_regions.add(c.new_region_id);relinked.append((room_id,c.old_region_id,c.new_region_id))
        for room_id,room in tuple(self._rooms.items()):
            if maintenance and room.anchor_region_id not in current:
                removed.append(room_id);del self._rooms[room_id]
        created=[]
        for rid,reg in sorted(current.items()):
            owner=next((r for r in self._rooms.values() if r.anchor_region_id==rid),None)
            if owner:
                self._rooms[owner.room_id]=RoomNode(owner.room_id,owner.name,rid,reg.polygon,reg.centroid_xy,members.get(rid,()),owner.created_reason,revision)
            elif members.get(rid):
                room_id=self._next_id;self._next_id+=1; name=f"room_{len(self._rooms)}"
                reason="agent_bootstrap" if any(entities_by_id[e].kind=="agent" for e in members[rid]) else "region_contains_existing_entities"
                self._rooms[room_id]=RoomNode(room_id,name,rid,reg.polygon,reg.centroid_xy,members[rid],reason,revision);created.append(room_id)
        # Enforce one direct room owner per entity; resolver returns at most one region.
        owner_by_region={r.anchor_region_id:r.room_id for r in self._rooms.values()}
        updated_rooms={rid:[] for rid in owner_by_region}
        for eid,rid in assigned.items():
            room_id=owner_by_region.get(rid)
            if room_id is not None:updated_rooms[rid].append(eid)
        for room_id,room in tuple(self._rooms.items()):
            if room.anchor_region_id not in current:
                continue  # deferred maintenance preserves the prior room evidence
            self._rooms[room_id]=RoomNode(room_id,room.name,room.anchor_region_id,room.polygon,room.centroid_xy,tuple(sorted(updated_rooms[room.anchor_region_id])),room.created_reason,revision)
        self._adj=self._compute_adjacency(current,owner_by_region,assigned,nav_edges)
        self._revision=revision
        return RoomUpdate(revision,self.snapshot(),assignments,self._adjacency_output(),tuple(created),tuple(sorted(removed)),tuple(relinked),False,retained_ids,expired_ids)
    def _compute_adjacency(self,regions,owner_by_region,assigned,nav_edges):
        region_pairs=set()
        for rid,r in regions.items():
            for other in r.adjacent_region_ids:
                try:other=int(other)
                except (TypeError,ValueError):continue
                if other in regions and other!=rid:region_pairs.add(tuple(sorted((rid,other))))
        nav_owner={eid:rid for eid,rid in assigned.items() if eid.startswith("nav:")}
        result=set()
        for edge in nav_edges:
            a,b=getattr(edge,"source_block",None),getattr(edge,"target_block",None)
            if a is None or b is None:continue
            na=f"nav:{a[0]}:{a[1]}";nb=f"nav:{b[0]}:{b[1]}";ra=nav_owner.get(na);rb=nav_owner.get(nb)
            if ra is None or rb is None or ra==rb or tuple(sorted((ra,rb))) not in region_pairs:continue
            room_a=owner_by_region.get(ra);room_b=owner_by_region.get(rb)
            if room_a is not None and room_b is not None and room_a!=room_b:result.add((min(room_a,room_b),max(room_a,room_b),min(ra,rb),max(ra,rb)))
        self._adj=set(result)
        return result
    def _adjacency_output(self):return tuple(RoomAdjacency(a,b,(ra,rb)) for a,b,ra,rb in sorted(self._adj))
