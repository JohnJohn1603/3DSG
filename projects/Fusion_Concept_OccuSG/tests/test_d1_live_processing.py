from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

import integration_v2.live_processing as live_processing
from integration_v2.live_processing import (_existing_loopback_viewer_url, _finalize_postprocess,
                                            _join_one, _stop_owned,
                                            _seal_run_integrity,
                                            _wait_for_explicit_viewer_start)
from standalone_sg.serialization.live_revision_io import (
    AcknowledgedPublisher, EventLogReader, append_jsonl, atomic_write_json,
    canonical_json, read_complete_jsonl, sha256_file,
)


def _make_event(root: Path, branch: str, state_path: Path, event_seq: int,
                frame_index: int, frame_id: str, timestamp: float,
                source_revision: int, assets: list[Path] = ()) -> dict:
    asset_hashes = {p.resolve().relative_to(root.resolve()).as_posix(): sha256_file(p)
                    for p in [state_path, *assets]}
    row = {"schema": "scene-graph-live-branch-event/v1", "scene_id": "replica-x",
           "sequence_id": "route-a", "source_frame_index": frame_index,
           "source_frame_id": frame_id, "source_timestamp_s": timestamp,
           "source_revision": source_revision, "revision_kind": branch,
           "event_seq": event_seq, "branch": branch,
           "state_path": state_path.resolve().relative_to(root.resolve()).as_posix(),
           "state_sha256": asset_hashes[state_path.resolve().relative_to(root.resolve()).as_posix()],
           "asset_sha256": asset_hashes}
    row["event_sha256"] = hashlib.sha256(canonical_json(row)).hexdigest()
    return row


def test_jsonl_reader_ignores_partial_tail_then_accepts_record(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    event = {"event_seq": 1, "value": "prefix"}
    payload = canonical_json(event)
    path.write_bytes(payload[:9])
    reader = EventLogReader(path)
    assert reader.poll() == []
    assert reader.offset == 0
    with path.open("ab") as stream:
        stream.write(payload[9:] + b"\n")
    assert reader.poll() == [event]
    assert reader.last_seq == 1


def test_event_reader_exact_duplicate_is_idempotent_and_conflict_fails(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    row = {"event_seq": 1, "value": "same"}
    append_jsonl(path, row)
    reader = EventLogReader(path)
    assert reader.poll() == [row]
    append_jsonl(path, row)
    assert reader.poll() == []
    append_jsonl(path, {"event_seq": 1, "value": "conflict"})
    with pytest.raises(ValueError, match="conflicting duplicate"):
        reader.poll()


def test_publisher_waits_for_coordinator_ack_without_browser(tmp_path: Path):
    root = tmp_path / "run"
    root.mkdir()
    state = root / "state.json"
    atomic_write_json(state, {"revision": 1})
    publisher = AcknowledgedPublisher(root, "objects", timeout_s=3.0)
    outcome: list[dict] = []
    thread = threading.Thread(target=lambda: outcome.append(publisher.publish(
        {"source_frame_index": 0, "source_frame_id": "frame-0", "source_timestamp_s": 0.0},
        state_path=state)))
    thread.start()
    event_path = root / "branches/objects/events.jsonl"
    deadline = time.monotonic() + 2
    rows = []
    while time.monotonic() < deadline and not rows:
        rows, _ = read_complete_jsonl(event_path)
        time.sleep(.01)
    assert len(rows) == 1
    assert thread.is_alive(), "producer must wait for coordinator, independent of any browser"
    atomic_write_json(root / "branches/objects/acks/000001.json",
                      {"event_sha256": rows[0]["event_sha256"], "durable": True})
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcome[0]["event_sha256"] == rows[0]["event_sha256"]
    assert publisher.last_timing["publication_ms"] >= 0
    assert publisher.last_timing["barrier_wait_ms"] >= 0


def test_publisher_honors_run_cancellation_and_preserves_partial_event(tmp_path: Path):
    root = tmp_path / "run"
    root.mkdir()
    state = root / "state.json"
    atomic_write_json(state, {"revision": 1})
    publisher = AcknowledgedPublisher(root, "rooms", timeout_s=1.0)
    atomic_write_json(root / "control/stop.json", {"status": "cancelled"})
    with pytest.raises(InterruptedError, match="stop requested"):
        publisher.publish({"source_frame_index": 0, "source_frame_id": "frame-0",
                           "source_timestamp_s": 0.0}, state_path=state)
    rows, _ = read_complete_jsonl(root / "branches/rooms/events.jsonl")
    assert len(rows) == 1
    assert rows[0]["state_sha256"] == sha256_file(state)
    assert not (root / "branches/rooms/acks/000001.json").exists()


def test_coordinator_joins_matching_prefixes_and_keeps_membership_unavailable(tmp_path: Path):
    root = tmp_path / "d1"
    viewer = root / "viewer"
    viewer.mkdir(parents=True)
    (viewer / "geometry").mkdir()
    (root / "object_timeline/snapshots").mkdir(parents=True)
    (root / "object_timeline/geometry/rev-000001").mkdir(parents=True)
    (root / "room_replay/prefix_snapshots").mkdir(parents=True)
    cloud = root / "object_timeline/geometry/rev-000001/hypothesis_a.npz"
    np.savez_compressed(cloud, xyz=np.asarray([[0, 0, 1], [1, 0, 1]], dtype=np.float32),
                        rgb=np.asarray([[20, 30, 40], [40, 30, 20]], dtype=np.uint8))
    object_state_path = root / "object_timeline/snapshots/rev-000001.json"
    object_state = {"scene_id": "replica-x", "sequence_id": "route-a", "revision": 1,
        "frame_index": 0, "frame_id": "frame-0", "timestamp_s": .1,
        "stage": "online_hypotheses", "camera_pose_W_Copt": np.eye(4).tolist(),
        "objects": [{"hypothesis_id": "hypothesis_a", "lineage_key": "a",
                     "cloud_npz": "geometry/rev-000001/hypothesis_a.npz",
                     "cloud_sha256": sha256_file(cloud), "point_count": 2,
                     "observation_ids": ["frame-0:det-0"], "class_history_raw": ["chair"],
                     "detection_count": 1, "distinct_frame_count": 1, "distinct_pose_count": 1}]}
    atomic_write_json(object_state_path, object_state)
    room_state_path = root / "room_replay/prefix_snapshots/rev-000001.json"
    room_state = {"scene_id": "replica-x", "sequence_id": "route-a", "revision": 7,
        "timestamp_s": .1, "nodes": [
            {"node_id": "room:r1", "kind": "room", "attributes": {"room_id": "r1", "outer_ring": [[0,0],[2,0],[2,2],[0,2]]}},
            {"node_id": "nav:n1", "kind": "navigation", "attributes": {}},
            {"node_id": "agent:a1", "kind": "agent", "attributes": {}},
            {"node_id": "object:native1", "kind": "object", "attributes": {}}],
        "edges": [{"source_id": "room:r1", "target_id": target, "relation": "room_contains"}
                  for target in ("nav:n1", "agent:a1", "object:native1")]}
    atomic_write_json(room_state_path, room_state)
    object_event = _make_event(root, "objects", object_state_path, 1, 0, "frame-0", .1, 1, [cloud])
    room_event = _make_event(root, "rooms", room_state_path, 1, 0, "frame-0", .1, 7)
    base = {"timeline_manifest": {"scene_id": "replica-x", "sequence_id": "route-a"},
            "evidence_index": "evidence_index.json", "world_bounds_xy": [-5, 5, -5, 5]}
    frames: list[dict] = []
    joined = _join_one(root, viewer, base, frames, object_event, room_event)
    assert joined["source_frame_index"] == 0
    assert len(frames) == 1
    joined_state = json.loads((root / joined["joined_state"]).read_text())
    graph = json.loads((root / joined_state["unified_graph_state"]).read_text())
    assert graph["membership_availability"] == {"status": "unavailable", "reason": "unavailable_elevation_scope"}
    assert graph["room_semantics_availability"]["reason"] == "unavailable_membership_scope"
    assert graph["room_contains_by_target_node_type"] == {
        "navigation": 1, "agent": 1, "occusg_native_object": 1, "conceptgraphs_object": 0}
    assert len(graph["edges"]) == 3
    graph_node_ids = ({row["node_id"] for row in graph["rooms"]}
                      | {row["causal_object_id"] for row in graph["objects"]}
                      | {row["node_id"] for row in graph["room_pipeline_nodes"]})
    assert graph["dangling_edge_count"] == 0
    assert all(edge["source_id"] in graph_node_ids and edge["target_id"] in graph_node_ids
               for edge in graph["edges"])
    native = next(row for row in graph["room_pipeline_nodes"] if row["node_type"] == "object")
    assert native["node_id"] == "object:native1" and native["source_branch"] == "rooms"
    assert (root / "branches/objects/acks/000001.json").is_file()


def test_owned_process_group_is_stopped_without_global_process_search():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    _stop_owned(proc, grace_s=.2)
    assert proc.poll() is not None


def test_producer_failure_marks_partial_run_failed_and_stops_sibling(tmp_path: Path, monkeypatch):
    root = tmp_path
    d_root = root / "artifacts/live_processing_runs"
    source = root / "source"
    source.mkdir()
    monkeypatch.setattr(live_processing, "ROOT", root)
    monkeypatch.setattr(live_processing, "D_ROOT", d_root)
    monkeypatch.setattr(live_processing, "__file__", str(root / "integration_v2/live_processing.py"))
    monkeypatch.setattr(live_processing, "_sha", lambda _path: "a" * 64)
    monkeypatch.setattr(live_processing, "_verify_source_run", lambda _source_id: (
        source,
        {"scene_id": "replica-x", "sequence_id": "route-a", "input_sha256": {
            "resource_lock": "a" * 64, "native_pipeline_config": "a" * 64,
            "native_m2_m3": "a" * 64, "native_dude": "a" * 64}},
        {"capture": {"frames": [{"frame_index": 0, "frame_id": "frame-0"}]},
         "locked_output_hashes": {}, "contract_hashes": {}}))

    def prepare_viewer(_source, run_root, _scene, _sequence):
        viewer = run_root / "viewer"
        viewer.mkdir(parents=True)
        atomic_write_json(viewer / "live_index.json", {"frames": []})
        return viewer, {"index.html": "a" * 64}

    monkeypatch.setattr(live_processing, "_prepare_viewer", prepare_viewer)
    room_inputs = []
    for name in ("resource-manifest", "pipeline-config", "m2-m3-binary", "dude-binary"):
        path = root / name
        path.write_text("fixture")
        room_inputs.extend(["--" + name, str(path)])
    monkeypatch.setattr(live_processing, "_build_commands", lambda *_args: (
        ["python", "object-fixture"], ["python", "room-fixture", *room_inputs]))

    class FakeProcess:
        def __init__(self, returncode, pid):
            self.returncode = returncode
            self.pid = pid

        def poll(self):
            return self.returncode

    processes = [FakeProcess(17, 101), FakeProcess(None, 102)]
    monkeypatch.setattr(live_processing.subprocess, "Popen", lambda *_args, **_kwargs: processes.pop(0))
    stopped = []

    def stop_owned(process, **_kwargs):
        stopped.append(process)
        process.returncode = -15

    monkeypatch.setattr(live_processing, "_stop_owned", stop_owned)
    run_id = "d1_fixture_producer_failure"
    with pytest.raises(RuntimeError, match="D1 producer failed"):
        live_processing.run_live_cached("c2_fixture", run_id=run_id,
                                        viewer_base_url="http://127.0.0.1:8765",
                                        overall_timeout_s=5)

    run_root = d_root / run_id
    manifest = json.loads((run_root / "run_manifest.json").read_text())
    stop = json.loads((run_root / "control/stop.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["partial_artifacts_preserved"] is True
    assert manifest["partial_frame_count"] == 0
    assert stop["status"] == "failed"
    assert len(stopped) == 1, "only the still-running sibling producer should be stopped"
    assert manifest["viewer"]["status"] == "external_server_left_untouched"


def test_xyz_bootstrap_start_gate_precedes_any_producer_process(tmp_path: Path, monkeypatch):
    import scripts.build_d2_xyz_viewer as d2

    root = tmp_path
    d_root = root / "artifacts/live_processing_runs"
    source = root / "source"
    source.mkdir()
    monkeypatch.setattr(live_processing, "ROOT", root)
    monkeypatch.setattr(live_processing, "D_ROOT", d_root)
    monkeypatch.setattr(live_processing, "__file__", str(root / "integration_v2/live_processing.py"))
    monkeypatch.setattr(live_processing, "_sha", lambda _path: "a" * 64)
    monkeypatch.setattr(live_processing, "_verify_source_run", lambda _source_id: (
        source,
        {"scene_id": "replica-x", "sequence_id": "route-a", "input_sha256": {
            "resource_lock": "a" * 64, "native_pipeline_config": "a" * 64,
            "native_m2_m3": "a" * 64, "native_dude": "a" * 64}},
        {"capture": {"frames": [{"frame_index": 0, "frame_id": "frame-0"}]},
         "locked_output_hashes": {}, "contract_hashes": {}}))

    def prepare_viewer(_source, run_root, _scene, _sequence):
        viewer = run_root / "viewer"
        viewer.mkdir(parents=True)
        atomic_write_json(viewer / "live_index.json", {"frames": [], "status": "starting"})
        atomic_write_json(viewer / "live_head.json", {"revision_count": 0, "status": "starting"})
        (viewer / "index.html").write_text("<!doctype html>", encoding="utf-8")
        return viewer, {"index.html": "a" * 64}

    monkeypatch.setattr(live_processing, "_prepare_viewer", prepare_viewer)
    def bootstrap(run_root, _version):
        (run_root / "viewer/d2_viewer_entrypoint.json").write_bytes(b"pointer")
        return {"viewer_entrypoint": "index_fixture.html", "status": "running"}

    monkeypatch.setattr(d2, "bootstrap", bootstrap)
    monkeypatch.setattr(d2, "validate_entrypoint_bundle", lambda _viewer: {})
    room_inputs = []
    for name in ("resource-manifest", "pipeline-config", "m2-m3-binary", "dude-binary"):
        path = root / name
        path.write_text("fixture")
        room_inputs.extend(["--" + name, str(path)])
    monkeypatch.setattr(live_processing, "_build_commands", lambda *_args: (
        ["python", "object-fixture"], ["python", "room-fixture", *room_inputs]))

    class Server:
        pid = 321
        def poll(self): return None

    server = Server()
    monkeypatch.setattr(live_processing, "_launch_server", lambda *_args, **_kwargs: (server, "http://127.0.0.1:8766/viewer/index_fixture.html?live=1"))
    monkeypatch.setattr(live_processing, "_stop_owned", lambda _proc, **_kwargs: None)
    spawned = []
    monkeypatch.setattr(live_processing.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    run_id = "d1_gate_timeout_fixture"
    with pytest.raises(TimeoutError, match="no producer was started"):
        live_processing.run_live_cached("c2_fixture", run_id=run_id,
            xyz_viewer_version="fixture", viewer_ready_timeout_s=.01,
            overall_timeout_s=5)
    assert spawned == []
    manifest = json.loads((d_root / run_id / "run_manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["viewer_start_gate"]["producer_started"] is False
    receipt = json.loads((d_root / run_id / "run_manifest.sha256.json").read_text())
    assert receipt["schema"] == "d1-run-manifest-hash-receipt/v1"
    # This fixture deliberately stubs _sha; production uses the real SHA-256 helper.
    assert receipt["sha256"] == "a" * 64


def test_existing_viewer_url_is_loopback_only_and_does_not_create_server(tmp_path: Path):
    root = tmp_path / "workspace"
    viewer = root / "artifacts/run/viewer"
    viewer.mkdir(parents=True)
    url, port = _existing_loopback_viewer_url("http://127.0.0.1:8765", viewer, root)
    assert port == 8765
    assert url == "http://127.0.0.1:8765/artifacts/run/viewer/index.html?live=1"
    with pytest.raises(ValueError, match="loopback origin"):
        _existing_loopback_viewer_url("http://example.com:8765", viewer, root)


def test_explicit_viewer_start_gate_accepts_only_matching_pointer_and_run(tmp_path: Path):
    root = tmp_path / "d1"
    (root / "control").mkdir(parents=True)
    (root / "viewer").mkdir()
    pointer = {"schema": "d2-viewer-entrypoint/v1", "status": "ready",
               "pipeline_status": "running", "entrypoint": "index_d2_live.html"}
    pointer_path = root / "viewer/d2_viewer_entrypoint.json"
    atomic_write_json(pointer_path, pointer)
    pointer_hash = hashlib.sha256(pointer_path.read_bytes()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    atomic_write_json(root / "run_manifest.json", {"run_id": "d1_gate_fixture",
        "created_utc": now,
        "viewer": {"url": "http://127.0.0.1:8765/viewer/index_d2_live.html?live=1"}})
    atomic_write_json(root / "control/start.json", {
        "schema": "d1-explicit-viewer-start/v1", "run_id": "d1_gate_fixture",
        "confirmed_browser_ready": True, "entrypoint_pointer_sha256": pointer_hash,
        "entrypoint": "index_d2_live.html",
        "url": "http://127.0.0.1:8765/viewer/index_d2_live.html?live=1",
        "started_by": "camera-live-pilot-confirm-browser-ready",
        "started_utc": now})
    result = _wait_for_explicit_viewer_start(root, "d1_gate_fixture", pointer_hash, .1)
    assert result["confirmed_browser_ready"] is True
    with pytest.raises(ValueError, match="does not match this run"):
        _wait_for_explicit_viewer_start(root, "d1_other_run", pointer_hash, .1)


def test_explicit_viewer_start_gate_timeout_does_not_release_producer(tmp_path: Path):
    root = tmp_path / "d1"
    (root / "control").mkdir(parents=True)
    with pytest.raises(TimeoutError, match="no producer was started"):
        _wait_for_explicit_viewer_start(root, "d1_gate_fixture", "b" * 64, .01)


def test_integrity_seals_graph_and_manifests_but_tracks_mutable_server_log_separately(tmp_path: Path):
    root = tmp_path / "d1"
    (root / "joined").mkdir(parents=True)
    (root / "viewer").mkdir()
    (root / "logs").mkdir()
    (root / "joined/graph.json").write_text('{"edges":[]}\n')
    (root / "viewer/d2_xyz_manifest_live.json").write_text('{"status":"completed"}\n')
    (root / "viewer/d2_viewer_entrypoint.json").write_text('{"status":"completed"}\n')
    access_log = root / "logs/viewer_server.log"
    access_log.write_text("GET /index.html 200\n")

    class Server:
        pid = 321
        def poll(self): return None

    doc = {}
    _seal_run_integrity(root, doc, Server())
    for rel in ("joined/graph.json", "viewer/d2_xyz_manifest_live.json", "viewer/d2_viewer_entrypoint.json"):
        assert rel in doc["immutable_output_sha256"]
    assert "logs/viewer_server.log" not in doc["immutable_output_sha256"]
    assert doc["operational_logs"]["files"]["logs/viewer_server.log"]["mutable_after_checkpoint"] is True
    sealed = dict(doc["immutable_output_sha256"])
    access_log.write_text("GET /index.html 200\nGET /live_head.json 200\n")
    assert doc["immutable_output_sha256"] == sealed


def test_terminal_viewer_room_snapshot_url_is_relative_to_viewer(tmp_path: Path):
    root = tmp_path / "run"
    viewer = root / "viewer"
    export = root / "object_mapping/export"
    export.mkdir(parents=True)
    viewer.mkdir()
    (root / "object_mapping").mkdir(exist_ok=True)
    (root / "room_replay").mkdir()
    cloud = export / "objects/o1.npz"
    cloud.parent.mkdir()
    np.savez_compressed(cloud, xyz=np.asarray([[.1, .2, .3]], dtype=np.float32),
                        rgb=np.asarray([[1, 2, 3]], dtype=np.uint8))
    evidence = ["replica-cad-apt_0-0000:det-0001"]
    atomic_write_json(export / "export_manifest.json", {
        "objects": [{"export_id": "object-1", "cloud_npz": "objects/o1.npz",
                     "evidence": evidence, "reference_class_history": ["chair"],
                     "observation_count": 1, "point_count": 1}],
        "files_sha256": {"objects/o1.npz": sha256_file(cloud)}})
    atomic_write_json(root / "object_mapping/experiment_contract.json", {"seed": 20261014})
    atomic_write_json(root / "object_mapping/lineage_stages.json", {
        "stages": {"after_text_gated_merge": [{"observation_ids": evidence,
                                                  "distinct_frame_count": 1,
                                                  "distinct_pose_count": 1}]}})
    atomic_write_json(root / "room_replay/snapshot.json", {
        "scene_id": "apt_0", "sequence_id": "route-a", "nodes": [], "edges": []})
    result = _finalize_postprocess(root, viewer, tmp_path / "unused-source", {
        "revision": 12, "frame_index": 11, "frame_id": "replica-cad-apt_0-0011",
        "timestamp_s": 1.1})
    assert result["room_url"] == "../room_replay/snapshot.json"
    assert (viewer / result["unified_url"]).is_file()
