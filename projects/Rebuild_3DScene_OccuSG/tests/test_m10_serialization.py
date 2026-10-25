from __future__ import annotations
import json
from pathlib import Path
import pytest
from standalone_sg.contracts import ContractError
from standalone_sg.pipeline import GraphEdge, GraphNode, SceneGraphSnapshot
from standalone_sg.serialization import (deserialize_snapshot, load_delta_log, load_snapshot, query_nodes, replay_deltas, serialize_snapshot, trajectory_rows, write_snapshot, write_trajectory_csv, write_trajectory_json)

def graph_snapshot():
    agent=GraphNode("agent-0007","agent",1.25,2.5,"accepted:frame-7",{"frame_index":7,"frame_id":"frame-7","pose_W":(1.0,-2.0,0.5),"large_id":2**64+17})
    room=GraphNode("room-3","room",0.5,2.0,"m8:accepted-region-3",{"anchor_region_id":3,"outer_ring":((0.,0.),(4.,0.),(4.,3.),(0.,3.)),"holes":(((1.,1.),(1.,2.),(2.,2.),(2.,1.)),),"centroid_xy":(2.,1.5),"observation_revision":5})
    edge=GraphEdge("agent-0007","room-3","observed_in","m9:fixture",True)
    return SceneGraphSnapshot(1,"scene-A","sequence-B",9,2.5,(agent,room),(edge,),"config-sha256:abc",{"regions":True,"objects":False})

def test_snapshot_json_roundtrip_preserves_ids_geometry_provenance_times_and_staleness():
    expected=graph_snapshot(); encoded=serialize_snapshot(expected); payload=json.loads(encoded)
    assert payload["schema_version"]==1 and deserialize_snapshot(encoded)==expected
    assert query_nodes(expected,"room")[0].attributes["holes"]==expected.nodes[1].attributes["holes"]
    assert expected.nodes[0].attributes["large_id"]==2**64+17
    assert expected.nodes[0].provenance=="accepted:frame-7"
    assert expected.nodes[0].first_seen_s==1.25 and expected.nodes[0].last_seen_s==2.5
    assert dict(expected.stale_flags)=={"regions":True,"objects":False}

def test_empty_snapshot_and_legacy_v0_migration():
    empty=SceneGraphSnapshot(1,None,None,0,None,(),(),"empty",{})
    assert deserialize_snapshot(serialize_snapshot(empty))==empty
    raw=json.loads(serialize_snapshot(graph_snapshot())); raw["schema_version"]=0
    raw["snapshot"].pop("config_hash"); raw["snapshot"].pop("stale_flags")
    migrated=deserialize_snapshot(json.dumps(raw))
    assert migrated.config_hash=="legacy-v0" and dict(migrated.stale_flags)=={}
    assert migrated.nodes==graph_snapshot().nodes and migrated.edges==graph_snapshot().edges

@pytest.mark.parametrize("text", ['{"schema_version":1,"kind":"snapshot","snapshot":{"bad":NaN}}','{"schema_version":1,"kind":"snapshot","snapshot":{"bad":Infinity}}','{"schema_version":99,"kind":"snapshot","snapshot":{}}'])
def test_snapshot_loader_rejects_nonfinite_tokens_and_unknown_schema(text):
    with pytest.raises(ContractError): deserialize_snapshot(text)

def test_atomic_snapshot_and_trajectory_exports(tmp_path:Path):
    snapshot=graph_snapshot(); target=tmp_path/"nested"/"snapshot.json"; target.parent.mkdir()
    target.write_text("old content",encoding="utf-8"); write_snapshot(target,snapshot)
    assert load_snapshot(target)==snapshot and not list(target.parent.glob(".*.tmp"))
    rows=trajectory_rows(snapshot)
    assert len(rows)==1
    assert rows[0]["frame_id"]=="frame-7" and rows[0]["x_m"]==1.0
    csv_path,json_path=tmp_path/"trajectory.csv",tmp_path/"trajectory.json"
    write_trajectory_csv(csv_path,snapshot); write_trajectory_json(json_path,snapshot)
    assert len(csv_path.read_text().splitlines())==2
    assert json.loads(json_path.read_text())["poses"]==list(rows)

def test_demo_delta_log_replays_to_exported_snapshot_and_is_queryable(tmp_path:Path):
    from scripts.export_m10_demo import build_demo
    output=tmp_path/"demo"; manifest=build_demo(output)
    assert manifest["kind"]=="synthetic_cached_fixture"
    assert manifest["simulator_end_to_end"] is False and manifest["native_backend_checkpoint"] is False
    final=load_snapshot(output/"snapshot.json")
    initial=SceneGraphSnapshot(1,None,None,0,None,(),(),"m10-synthetic-fixture-v1",{})
    deltas=load_delta_log(output/"delta_log.json")
    assert replay_deltas(initial,deltas)==final
    assert len(query_nodes(final,"agent"))==manifest["agent_pose_count"]==3
    assert len(query_nodes(final,"room"))==2
    assert all((output/name).is_file() and (output/name).stat().st_size>0 for name in manifest["outputs"])

def test_reader_rejects_json_number_overflow_after_parse():
    encoded=serialize_snapshot(graph_snapshot()).replace('"timestamp_s": 2.5','"timestamp_s": 1e999')
    with pytest.raises(ContractError,match="finite"):
        deserialize_snapshot(encoded)

@pytest.mark.parametrize("mutation", [
    lambda payload: payload["snapshot"]["edges"].append({"source_id":"missing-node","target_id":"room-3","relation":"observed_in","provenance":"bad","directed":True}),
    lambda payload: payload["snapshot"]["edges"].append({"source_id":"agent-0007","target_id":"agent-0007","relation":"bad","provenance":"bad","directed":True}),
    lambda payload: payload["snapshot"]["nodes"].append(dict(payload["snapshot"]["nodes"][0])),
    lambda payload: payload["snapshot"]["stale_flags"].update({"regions":1}),
    lambda payload: payload["snapshot"]["edges"][0].update({"directed":1}),
])
def test_reader_rejects_malformed_graph_invariants(mutation):
    payload=json.loads(serialize_snapshot(graph_snapshot()))
    mutation(payload)
    with pytest.raises(ContractError):
        deserialize_snapshot(json.dumps(payload))

def test_reader_rejects_duplicate_room_anchor():
    payload=json.loads(serialize_snapshot(graph_snapshot()))
    payload["snapshot"]["nodes"][0]["kind"]="room"
    payload["snapshot"]["nodes"][0]["attributes"]["anchor_region_id"]=3
    with pytest.raises(ContractError,match="anchors"):
        deserialize_snapshot(json.dumps(payload))

def test_replay_validates_delta_result_invariants():
    from standalone_sg.pipeline import GraphDelta
    agent=graph_snapshot().nodes[0]
    delta=GraphDelta(0,1,0.0,(agent,),(),(),
        (GraphEdge("agent-0007","missing-node","observed_in","bad",True),),(),(),{}, {}, {})
    empty=SceneGraphSnapshot(1,None,None,0,None,(),(),"config-sha256:abc",{})
    with pytest.raises(ContractError,match="dangling"):
        replay_deltas(empty,(delta,))
