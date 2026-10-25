"""Versioned JSON serialization and atomic exporters for graph snapshots/deltas."""
from __future__ import annotations
import csv
import json
import os
from pathlib import Path
import tempfile
from dataclasses import fields, is_dataclass
import math
from typing import Any, Iterable, Mapping

from ..contracts.errors import ContractError
from ..pipeline.contracts import GraphDelta, GraphEdge, GraphNode, SceneGraphSnapshot
from ..pipeline.builder import SceneGraphBuilder

SCHEMA_VERSION=1
_NODE_KINDS={"agent","object","navigation","room"}

def _validate_finite_json(value: Any, path: str = "json") -> None:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ContractError(f"{path} object keys must be strings")
        for key, item in value.items():
            _validate_finite_json(item, f"{path}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _validate_finite_json(item, f"{path}[{index}]")
    elif type(value) is float and not math.isfinite(value):
        raise ContractError(f"{path} must be finite")

def _string(value: Any, path: str) -> str:
    if type(value) is not str or not value:
        raise ContractError(f"{path} must be a non-empty string")
    return value

def _revision(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        raise ContractError(f"{path} must be a non-negative integer")
    return value

def _finite(value: Any, path: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ContractError(f"{path} must be finite")

def _validate_stale_flags(value: Any, path: str) -> None:
    if not isinstance(value, Mapping) or any(type(key) is not str or type(item) is not bool for key, item in value.items()):
        raise ContractError(f"{path} must be a string-to-boolean mapping")

def _node_from_record(record: Any, path: str) -> GraphNode:
    if not isinstance(record, Mapping):
        raise ContractError(f"{path} must be an object")
    required={"node_id","kind","first_seen_s","last_seen_s","attributes"}
    if not required.issubset(record) or set(record)-required-{"provenance"}:
        raise ContractError(f"{path} has an invalid schema")
    node_id=_string(record["node_id"], f"{path}.node_id")
    kind=_string(record["kind"], f"{path}.kind")
    if kind not in _NODE_KINDS:
        raise ContractError(f"{path}.kind is unsupported")
    _finite(record["first_seen_s"], f"{path}.first_seen_s")
    _finite(record["last_seen_s"], f"{path}.last_seen_s")
    if float(record["first_seen_s"]) > float(record["last_seen_s"]):
        raise ContractError(f"{path} first_seen_s must not exceed last_seen_s")
    provenance=_string(record.get("provenance", "unknown:legacy-v0"), f"{path}.provenance")
    if not isinstance(record["attributes"], Mapping):
        raise ContractError(f"{path}.attributes must be an object")
    return GraphNode(node_id, kind, float(record["first_seen_s"]), float(record["last_seen_s"]), provenance, record["attributes"])

def _nodes_from_records(records: Any, path: str) -> tuple[GraphNode, ...]:
    if not isinstance(records, list):
        raise ContractError(f"{path} must be an array")
    nodes=tuple(_node_from_record(record, f"{path}[{index}]") for index, record in enumerate(records))
    if len({node.node_id for node in nodes}) != len(nodes):
        raise ContractError(f"{path} contains duplicate node IDs")
    return nodes

def _edge_from_record(record: Any, path: str) -> GraphEdge:
    if not isinstance(record, Mapping):
        raise ContractError(f"{path} must be an object")
    required={"source_id","target_id","relation","provenance","directed"}
    if set(record) != required:
        raise ContractError(f"{path} has an invalid schema")
    source=_string(record["source_id"], f"{path}.source_id")
    target=_string(record["target_id"], f"{path}.target_id")
    relation=_string(record["relation"], f"{path}.relation")
    provenance=_string(record["provenance"], f"{path}.provenance")
    if type(record["directed"]) is not bool:
        raise ContractError(f"{path}.directed must be boolean")
    return GraphEdge(source, target, relation, provenance, record["directed"])

def _edges_from_records(records: Any, path: str) -> tuple[GraphEdge, ...]:
    if not isinstance(records, list):
        raise ContractError(f"{path} must be an array")
    edges=tuple(_edge_from_record(record, f"{path}[{index}]") for index, record in enumerate(records))
    if len(set(edges)) != len(edges):
        raise ContractError(f"{path} contains duplicate edges")
    return edges

def _validate_graph_invariants(nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]) -> None:
    node_map={node.node_id:node for node in nodes}
    owners={}
    anchors={}
    for node in node_map.values():
        if node.kind == "room":
            anchor=node.attributes.get("anchor_region_id")
            if type(anchor) is not int or anchor < 0:
                raise ContractError("room nodes require a non-negative anchor_region_id")
            if anchor in anchors:
                raise ContractError("more than one RoomNode anchors a region")
            anchors[anchor]=node.node_id
    for edge in edges:
        if edge.source_id not in node_map or edge.target_id not in node_map:
            raise ContractError(f"dangling graph edge {edge}")
        if edge.source_id == edge.target_id:
            raise ContractError("self edge is not allowed")
        if edge.relation == "room_contains":
            if node_map[edge.source_id].kind != "room" or node_map[edge.target_id].kind == "room":
                raise ContractError("room_contains must connect a room to a non-room entity")
            prior=owners.get(edge.target_id)
            if prior is not None and prior != edge.source_id:
                raise ContractError("entity has multiple direct RoomNode owners")
            owners[edge.target_id]=edge.source_id
        if edge.relation == "room_adjacency" and (node_map[edge.source_id].kind != "room" or node_map[edge.target_id].kind != "room"):
            raise ContractError("room_adjacency must connect RoomNodes")

def _validate_snapshot_object(snapshot: SceneGraphSnapshot) -> None:
    if type(snapshot.schema_version) is not int or snapshot.schema_version != SCHEMA_VERSION:
        raise ContractError("unsupported snapshot schema_version")
    if snapshot.scene_id is not None: _string(snapshot.scene_id, "snapshot.scene_id")
    if snapshot.sequence_id is not None: _string(snapshot.sequence_id, "snapshot.sequence_id")
    _revision(snapshot.revision, "snapshot.revision")
    _finite(snapshot.timestamp_s, "snapshot.timestamp_s", allow_none=True)
    _string(snapshot.config_hash, "snapshot.config_hash")
    _validate_stale_flags(snapshot.stale_flags, "snapshot.stale_flags")
    _validate_graph_invariants(snapshot.nodes, snapshot.edges)

def _validate_delta_object(delta: GraphDelta) -> None:
    _revision(delta.base_revision, "delta.base_revision")
    _revision(delta.new_revision, "delta.new_revision")
    if delta.new_revision != delta.base_revision + 1:
        raise ContractError("delta revisions must be consecutive")
    _finite(delta.timestamp_s, "delta.timestamp_s")
    added={node.node_id for node in delta.added_nodes}
    updated={node.node_id for node in delta.updated_nodes}
    removed=set(delta.removed_node_ids)
    if len(added) != len(delta.added_nodes) or len(updated) != len(delta.updated_nodes) or len(removed) != len(delta.removed_node_ids):
        raise ContractError("delta node IDs must be unique")
    if added & updated or (added | updated) & removed:
        raise ContractError("delta node changes overlap")
    for node in (*delta.added_nodes, *delta.updated_nodes):
        _node_from_record({"node_id":node.node_id,"kind":node.kind,"first_seen_s":node.first_seen_s,"last_seen_s":node.last_seen_s,"provenance":node.provenance,"attributes":node.attributes}, "delta.node")
    if any(type(node_id) is not str or not node_id for node_id in delta.removed_node_ids):
        raise ContractError("delta removed_node_ids must be non-empty strings")
    if len(set(delta.added_edges)) != len(delta.added_edges) or len(set(delta.removed_edges)) != len(delta.removed_edges):
        raise ContractError("delta edge changes must be unique")
    for edge in (*delta.added_edges, *delta.removed_edges):
        _edge_from_record({"source_id":edge.source_id,"target_id":edge.target_id,"relation":edge.relation,"provenance":edge.provenance,"directed":edge.directed}, "delta.edge")
        if edge.source_id == edge.target_id:
            raise ContractError("self edge is not allowed")
    if not isinstance(delta.events, tuple) or any(type(event) is not str for event in delta.events):
        raise ContractError("delta events must be strings")
    _validate_finite_json(delta.diagnostics, "delta.diagnostics")
    _validate_stale_flags(delta.stale_flags, "delta.stale_flags")
    if not isinstance(delta.stage_versions, Mapping) or any(type(key) is not str or type(value) is not str for key, value in delta.stage_versions.items()):
        raise ContractError("delta stage_versions must be string-to-string")

def _primitive(value: Any):
    if is_dataclass(value): return {field.name:_primitive(getattr(value,field.name)) for field in fields(value)}
    if isinstance(value,Mapping):
        if any(type(key) is not str for key in value):
            raise ContractError("serialized JSON object keys must be strings")
        return {key:_primitive(value[key]) for key in sorted(value)}
    if isinstance(value,(tuple,list)): return [_primitive(v) for v in value]
    if isinstance(value,(set,frozenset)): raise ContractError("sets are not canonical JSON values")
    if value is None or type(value) in (str,bool,int): return value
    if type(value) is float:
        if not __import__('math').isfinite(value): raise ContractError("serialized JSON values must be finite")
        return value
    if hasattr(value,"item"):
        return _primitive(value.item())
    if hasattr(value,"tolist"):
        return _primitive(value.tolist())
    raise ContractError(f"unsupported JSON value type {type(value).__name__}")

def snapshot_payload(snapshot:SceneGraphSnapshot)->dict[str,Any]:
    if not isinstance(snapshot,SceneGraphSnapshot): raise ContractError("expected SceneGraphSnapshot")
    payload=_primitive(snapshot); payload["nodes"]=sorted(payload["nodes"],key=lambda n:n["node_id"])
    payload["edges"]=sorted(payload["edges"],key=lambda e:(e["source_id"],e["target_id"],e["relation"],e["provenance"],e["directed"]))
    return {"schema_version":SCHEMA_VERSION,"kind":"snapshot","snapshot":payload}

def delta_payload(delta:GraphDelta)->dict[str,Any]:
    if not isinstance(delta,GraphDelta): raise ContractError("expected GraphDelta")
    return {"schema_version":SCHEMA_VERSION,"kind":"delta","delta":_primitive(delta)}

def _dumps(payload,*,indent=2):
    try: return json.dumps(payload,sort_keys=True,indent=indent,ensure_ascii=False,allow_nan=False)+"\n"
    except (TypeError,ValueError) as exc: raise ContractError(f"value is not canonical JSON: {exc}") from exc

def serialize_snapshot(snapshot:SceneGraphSnapshot)->str: return _dumps(snapshot_payload(snapshot))
def serialize_delta(delta:GraphDelta)->str: return _dumps(delta_payload(delta))

def _reject_constant(value): raise ContractError(f"non-finite JSON token {value!r} is forbidden")
def _loads(text):
    try:
        value=json.loads(text,parse_constant=_reject_constant)
    except ContractError: raise
    except (json.JSONDecodeError,TypeError) as exc: raise ContractError(f"invalid graph JSON: {exc}") from exc
    _validate_finite_json(value)
    if not isinstance(value, Mapping):
        raise ContractError("graph JSON root must be an object")
    return value

def _migrate(payload):
    if not isinstance(payload, Mapping):
        raise ContractError("graph JSON envelope must be an object")
    version=payload.get("schema_version")
    if version==SCHEMA_VERSION: return payload
    if version==0 and payload.get("kind")=="snapshot" and isinstance(payload.get("snapshot"),dict):
        # v0 is the pre-M10 snapshot record: graph fields were present, but config/stale metadata omitted.
        snap=dict(payload["snapshot"]); snap.setdefault("config_hash","legacy-v0"); snap.setdefault("stale_flags",{})
        payload=dict(payload); payload["schema_version"]=SCHEMA_VERSION; payload["snapshot"]=snap
        return payload
    raise ContractError(f"unsupported graph schema_version {version!r}")

def snapshot_from_payload(raw)->SceneGraphSnapshot:
    payload=_migrate(raw)
    if payload.get("kind")!="snapshot" or not isinstance(payload.get("snapshot"),dict): raise ContractError("expected snapshot envelope")
    data=payload["snapshot"]
    if type(data.get("schema_version",1)) is not int or data.get("schema_version",1) != SCHEMA_VERSION:
        raise ContractError("unsupported snapshot schema_version")
    _revision(data.get("revision"), "snapshot.revision")
    _finite(data.get("timestamp_s"), "snapshot.timestamp_s", allow_none=True)
    if data.get("scene_id") is not None: _string(data.get("scene_id"), "snapshot.scene_id")
    if data.get("sequence_id") is not None: _string(data.get("sequence_id"), "snapshot.sequence_id")
    _string(data.get("config_hash"), "snapshot.config_hash")
    _validate_stale_flags(data.get("stale_flags",{}), "snapshot.stale_flags")
    parsed_nodes=_nodes_from_records(data.get("nodes"), "snapshot.nodes")
    parsed_edges=_edges_from_records(data.get("edges"), "snapshot.edges")
    _validate_graph_invariants(parsed_nodes, parsed_edges)
    try:
        snapshot=SceneGraphSnapshot(SCHEMA_VERSION,data.get("scene_id"),data.get("sequence_id"),data["revision"],data.get("timestamp_s"),parsed_nodes,parsed_edges,data["config_hash"],data.get("stale_flags",{}))
        _validate_snapshot_object(snapshot)
        return snapshot
    except (KeyError,TypeError,ValueError) as exc: raise ContractError(f"malformed snapshot payload: {exc}") from exc

def deserialize_snapshot(text:str)->SceneGraphSnapshot: return snapshot_from_payload(_loads(text))
def load_snapshot(path:str|Path)->SceneGraphSnapshot: return deserialize_snapshot(Path(path).read_text(encoding="utf-8"))

def delta_from_payload(raw)->GraphDelta:
    payload=_migrate(raw)
    if payload.get("kind")!="delta": raise ContractError("expected delta envelope")
    d=payload["delta"]
    try:
        added=_nodes_from_records(d["added_nodes"], "delta.added_nodes")
        updated=_nodes_from_records(d["updated_nodes"], "delta.updated_nodes")
        added_edges=_edges_from_records(d["added_edges"], "delta.added_edges")
        removed_edges=_edges_from_records(d["removed_edges"], "delta.removed_edges")
        removed_ids=tuple(d["removed_node_ids"])
        if not isinstance(d["events"], list) or any(type(event) is not str for event in d["events"]): raise ContractError("delta.events must be an array of strings")
        _revision(d["base_revision"], "delta.base_revision"); _revision(d["new_revision"], "delta.new_revision")
        _finite(d["timestamp_s"], "delta.timestamp_s")
        _validate_finite_json(d["diagnostics"], "delta.diagnostics")
        _validate_stale_flags(d["stale_flags"], "delta.stale_flags")
        if not isinstance(d["stage_versions"], Mapping) or any(type(k) is not str or type(v) is not str for k,v in d["stage_versions"].items()): raise ContractError("delta.stage_versions must be string-to-string")
        delta=GraphDelta(d["base_revision"],d["new_revision"],d["timestamp_s"],added,updated,removed_ids,added_edges,removed_edges,tuple(d["events"]),d["diagnostics"],d["stale_flags"],d["stage_versions"])
        _validate_delta_object(delta)
        return delta
    except (KeyError,TypeError,ValueError) as exc: raise ContractError(f"malformed delta payload: {exc}") from exc

def deserialize_delta(text:str)->GraphDelta: return delta_from_payload(_loads(text))

def replay_deltas(initial:SceneGraphSnapshot,deltas:Iterable[GraphDelta])->SceneGraphSnapshot:
    if not isinstance(initial,SceneGraphSnapshot): raise ContractError("initial must be SceneGraphSnapshot")
    _validate_snapshot_object(initial)
    nodes={n.node_id:n for n in initial.nodes}; edges=set(initial.edges); revision=initial.revision; current=initial
    for delta in deltas:
        if not isinstance(delta,GraphDelta): raise ContractError("replay input must contain GraphDelta values")
        _validate_delta_object(delta)
        if delta.base_revision!=revision or delta.new_revision!=revision+1: raise ContractError("delta revision chain is discontinuous")
        if any(key not in nodes for key in delta.removed_node_ids): raise ContractError("delta removes an unknown node")
        if any(node.node_id in nodes for node in delta.added_nodes): raise ContractError("delta adds an existing node")
        if any(node.node_id not in nodes for node in delta.updated_nodes): raise ContractError("delta updates an unknown node")
        for key in delta.removed_node_ids: nodes.pop(key)
        for node in (*delta.added_nodes,*delta.updated_nodes): nodes[node.node_id]=node
        edges.difference_update(delta.removed_edges); edges.update(delta.added_edges)
        scene_id=delta.diagnostics.get("scene_id",current.scene_id)
        sequence_id=delta.diagnostics.get("sequence_id",current.sequence_id)
        current=SceneGraphSnapshot(initial.schema_version,scene_id,sequence_id,delta.new_revision,delta.timestamp_s,
            tuple(nodes[k] for k in sorted(nodes)),tuple(sorted(edges)),initial.config_hash,delta.stale_flags)
        _validate_snapshot_object(current)
        revision=delta.new_revision
    return current

def query_nodes(snapshot:SceneGraphSnapshot,kind:str|None=None)->tuple[GraphNode,...]:
    _validate_snapshot_object(snapshot)
    if kind is not None and kind not in {"agent","object","navigation","room"}: raise ContractError("unknown node kind")
    return tuple(n for n in snapshot.nodes if kind is None or n.kind==kind)

def trajectory_rows(snapshot:SceneGraphSnapshot)->tuple[dict[str,Any],...]:
    rows=[]
    for node in query_nodes(snapshot,"agent"):
        pose=node.attributes.get("pose_W")
        if not isinstance(pose,(tuple,list)) or len(pose)!=3: continue
        rows.append({"frame_index":int(node.attributes["frame_index"]),"frame_id":node.attributes["frame_id"],
          "timestamp_s":float(node.last_seen_s),"x_m":float(pose[0]),"y_m":float(pose[1]),"z_m":float(pose[2]),
          "provenance":node.provenance})
    return tuple(sorted(rows,key=lambda r:(r["frame_index"],r["frame_id"])))

def _atomic_bytes(path:Path,data:bytes):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp,path)
        try:
            dfd=os.open(path.parent,os.O_RDONLY); os.fsync(dfd); os.close(dfd)
        except OSError: pass
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise

def write_snapshot(path,snapshot): _atomic_bytes(Path(path),serialize_snapshot(snapshot).encode("utf-8"))
def write_delta_log(path,deltas:Iterable[GraphDelta]):
    payload={"schema_version":SCHEMA_VERSION,"kind":"delta_log","deltas":[delta_payload(d)["delta"] for d in deltas]}
    _atomic_bytes(Path(path),_dumps(payload).encode("utf-8"))
def load_delta_log(path)->tuple[GraphDelta,...]:
    payload=_migrate(_loads(Path(path).read_text(encoding="utf-8")))
    if payload.get("kind")!="delta_log": raise ContractError("expected delta_log envelope")
    return tuple(delta_from_payload({"schema_version":SCHEMA_VERSION,"kind":"delta","delta":d}) for d in payload.get("deltas",()))

def write_trajectory_json(path,snapshot): _atomic_bytes(Path(path),_dumps({"schema_version":SCHEMA_VERSION,"kind":"trajectory","poses":list(trajectory_rows(snapshot))}).encode())
def write_trajectory_csv(path,snapshot):
    import io
    rows=trajectory_rows(snapshot); output=io.StringIO(newline="")
    fields_order=("frame_index","frame_id","timestamp_s","x_m","y_m","z_m","provenance")
    writer=csv.DictWriter(output,fieldnames=fields_order,lineterminator="\n"); writer.writeheader(); writer.writerows(rows)
    _atomic_bytes(Path(path),output.getvalue().encode("utf-8"))
