"""Public immutable packet, graph and delta contracts for M9."""
from __future__ import annotations
from dataclasses import dataclass, field
import math
from numbers import Real
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence
from ..contracts.errors import ContractError
from ..contracts.frames import PointCloud, RGBDFrame
from ..contracts.objects import DetectionBatch
from ..contracts.regions import CanonicalRegion, TrackedRegion
from ..mapping.projection import FreeSpaceGrid2D

InputMode=Literal["grid","regions","grid_regions","rgbd","pointcloud","occupancy"]
def _validate_json_value(value,path="metadata"):
    """Accept only JSON-native values and finite numbers, before any stage runs."""
    if value is None or type(value) in (str,bool,int): return
    if type(value) is float:
        if not math.isfinite(value): raise ContractError(f"{path} must not contain NaN or infinity")
        return
    if isinstance(value,Mapping):
        for key,item in value.items():
            if type(key) is not str: raise ContractError(f"{path} object keys must be strings")
            _validate_json_value(item,f"{path}.{key}")
        return
    if type(value) in (tuple,list):
        for index,item in enumerate(value): _validate_json_value(item,f"{path}[{index}]")
        return
    raise ContractError(f"{path} contains unsupported non-JSON value of type {type(value).__name__}")

def _freeze(value):
    if isinstance(value,Mapping): return MappingProxyType({str(k):_freeze(v) for k,v in value.items()})
    if isinstance(value,(list,tuple)): return tuple(_freeze(v) for v in value)
    if isinstance(value,set): return frozenset(_freeze(v) for v in value)
    return value

@dataclass(frozen=True,slots=True)
class FramePacket:
    scene_id:str
    sequence_id:str
    frame_id:str
    frame_index:int
    timestamp_s:float
    input_mode:InputMode
    base_pose_W:tuple[float,float,float]|None=None
    grid:FreeSpaceGrid2D|None=None
    regions:tuple[CanonicalRegion,...]|None=None
    retained_tracks:tuple[TrackedRegion,...]=()
    regions_stale:bool=False
    detections:DetectionBatch|None=None
    rgbd:RGBDFrame|None=None
    pointcloud:PointCloud|None=None
    occupancy_snapshot:Any|None=None
    source_metadata:Mapping[str,Any]=field(default_factory=dict)
    def __post_init__(self):
        for name in ("scene_id","sequence_id","frame_id"):
            value=getattr(self,name)
            if type(value) is not str or not value.strip(): raise ContractError(f"FramePacket.{name} must be a non-empty string")
        if type(self.frame_index) is not int or self.frame_index<0: raise ContractError("FramePacket.frame_index must be a non-negative integer")
        if isinstance(self.timestamp_s,bool) or not isinstance(self.timestamp_s,Real) or not math.isfinite(float(self.timestamp_s)): raise ContractError("FramePacket.timestamp_s must be finite")
        if type(self.regions_stale) is not bool: raise ContractError("FramePacket.regions_stale must be boolean")
        if self.input_mode not in ("grid","regions","grid_regions","rgbd","pointcloud","occupancy"): raise ContractError("unsupported FramePacket input_mode")
        requirements={"grid":(self.grid is not None and self.regions is None),
          "regions":(self.regions is not None),"grid_regions":(self.grid is not None and self.regions is not None),
          "rgbd":(self.rgbd is not None),"pointcloud":(self.pointcloud is not None),
          "occupancy":(self.occupancy_snapshot is not None)}
        if not requirements[self.input_mode]: raise ContractError(f"FramePacket fields do not satisfy input_mode={self.input_mode!r}")
        if self.base_pose_W is not None and (type(self.base_pose_W) is not tuple or len(self.base_pose_W)!=3 or any(isinstance(v,bool) or not isinstance(v,Real) or not math.isfinite(float(v)) for v in self.base_pose_W)):
            raise ContractError("FramePacket.base_pose_W must be a finite numeric world XYZ tuple")
        if self.grid is not None and (not isinstance(self.grid,FreeSpaceGrid2D) or self.grid.frame_id!="world"):
            raise ContractError("FramePacket.grid must be a canonical-world FreeSpaceGrid2D")
        if self.regions is not None and any(not isinstance(r,CanonicalRegion) or r.persistent_id is None for r in self.regions):
            raise ContractError("FramePacket.regions must be tracked CanonicalRegion values with persistent IDs")
        if self.detections is not None:
            if not isinstance(self.detections,DetectionBatch): raise ContractError("FramePacket.detections must be DetectionBatch or None")
            if self.detections.frame_id!=self.frame_id or abs(self.detections.timestamp_s-float(self.timestamp_s))>1e-6:
                raise ContractError("DetectionBatch frame/timestamp must match FramePacket")
        if not isinstance(self.source_metadata,Mapping): raise ContractError("FramePacket.source_metadata must be a mapping")
        _validate_json_value(self.source_metadata,"FramePacket.source_metadata")
        object.__setattr__(self,"source_metadata",_freeze(self.source_metadata))
        object.__setattr__(self,"timestamp_s",float(self.timestamp_s))
        if self.regions is not None: object.__setattr__(self,"regions",tuple(self.regions))
        object.__setattr__(self,"retained_tracks",tuple(self.retained_tracks))

@dataclass(frozen=True,slots=True)
class GraphNode:
    node_id:str
    kind:Literal["agent","object","navigation","room"]
    first_seen_s:float
    last_seen_s:float
    provenance:str
    attributes:Mapping[str,Any]
    def __post_init__(self):
        if not isinstance(self.attributes,Mapping): raise ContractError("GraphNode.attributes must be a mapping")
        _validate_json_value(self.attributes,"GraphNode.attributes")
        object.__setattr__(self,"attributes",_freeze(self.attributes))

@dataclass(frozen=True,slots=True,order=True)
class GraphEdge:
    source_id:str
    target_id:str
    relation:str
    provenance:str
    directed:bool=True

@dataclass(frozen=True,slots=True)
class SceneGraphSnapshot:
    schema_version:int
    scene_id:str|None
    sequence_id:str|None
    revision:int
    timestamp_s:float|None
    nodes:tuple[GraphNode,...]
    edges:tuple[GraphEdge,...]
    config_hash:str
    stale_flags:Mapping[str,bool]
    object_lifecycle:Mapping[str,Any]=field(default_factory=dict)
    def __post_init__(self):
        object.__setattr__(self,"stale_flags",_freeze(self.stale_flags))
        object.__setattr__(self,"object_lifecycle",_freeze(self.object_lifecycle))

@dataclass(frozen=True,slots=True)
class GraphPatch:
    """Atomic immutable graph diff accepted against exactly one base revision."""
    base_revision:int
    new_revision:int
    upsert_nodes:tuple[GraphNode,...]
    removed_node_ids:tuple[str,...]
    added_edges:tuple[GraphEdge,...]
    removed_edges:tuple[GraphEdge,...]
    def __post_init__(self):
        if type(self.base_revision) is not int or type(self.new_revision) is not int or self.new_revision!=self.base_revision+1:
            raise ContractError("GraphPatch revisions must be consecutive integers")
        if len({n.node_id for n in self.upsert_nodes})!=len(self.upsert_nodes):
            raise ContractError("GraphPatch upsert node IDs must be unique")
        if len(set(self.removed_node_ids))!=len(self.removed_node_ids):
            raise ContractError("GraphPatch removed node IDs must be unique")
    def apply(self,nodes:Mapping[str,GraphNode],edges:Sequence[GraphEdge],*,revision:int):
        if revision!=self.base_revision: raise ContractError("GraphPatch base revision mismatch")
        next_nodes=dict(nodes); next_edges=set(edges)
        for node_id in self.removed_node_ids: next_nodes.pop(node_id,None)
        for node in self.upsert_nodes: next_nodes[node.node_id]=node
        next_edges.difference_update(self.removed_edges); next_edges.update(self.added_edges)
        next_edges={e for e in next_edges if e.source_id in next_nodes and e.target_id in next_nodes}
        return next_nodes,next_edges


@dataclass(frozen=True,slots=True)
class GraphDelta:
    base_revision:int
    new_revision:int
    timestamp_s:float
    added_nodes:tuple[GraphNode,...]
    updated_nodes:tuple[GraphNode,...]
    removed_node_ids:tuple[str,...]
    added_edges:tuple[GraphEdge,...]
    removed_edges:tuple[GraphEdge,...]
    events:tuple[str,...]
    diagnostics:Mapping[str,Any]
    stale_flags:Mapping[str,bool]
    stage_versions:Mapping[str,str]
    def __post_init__(self):
        object.__setattr__(self,"diagnostics",_freeze(self.diagnostics))
        object.__setattr__(self,"stale_flags",_freeze(self.stale_flags))
        object.__setattr__(self,"stage_versions",_freeze(self.stage_versions))
