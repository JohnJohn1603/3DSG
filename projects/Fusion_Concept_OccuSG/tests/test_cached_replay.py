from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from integration_v2 import cached_replay as cr
from integration_v2.unified_incremental_timeline import replay_unified_snapshots
from scripts import scene_launcher as sl


CANONICAL_WORLD = "Z-up; C.x=H.x, C.y=-H.z, C.z=H.y"
COORDINATE_SCOPE = "capture/world metric meters, Z-up; room polygons use world XY"


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_inputs(root: Path) -> Path:
    timeline = root / "source/object_timeline"
    causal = root / "source/causal_state"
    capture_root = root / "source/capture_cache"
    scene_id, sequence_id = "fixture-scene-without-workspace-name", "fixture-route-alpha"
    capture_path = capture_root / "capture/capture_manifest.json"
    write_json(capture_path, {"scene_id": scene_id, "sequence_id": sequence_id,
                              "frame_count": 2, "canonical_world": CANONICAL_WORLD,
                              "depth_units": "m", "depth_scale_to_m": 1.0})
    mapping_output = root / "source/mapping_output"
    write_json(mapping_output / "diagnostic/evidence_manifest.json", {"observations": {}})

    frame_rows, causal_rows = [], []
    a_zip_path = root / "source/a_tables.zip"
    a_zip_path.parent.mkdir(parents=True, exist_ok=True)
    a_zip_path.write_bytes(b"fixture A table archive")
    runner_path = root / "source/causal_runner.py"
    ledger_path = root / "source/room_belief_ledger.py"
    runner_path.write_text("# pinned fixture runner\n")
    ledger_path.write_text("# pinned fixture ledger\n")
    policies = {
        "admission_policy": {"obj_min_detections": 3, "policy_id": "fixture-admission-v1"},
        "category_cap": "one contribution per unique eligible category; RB1 ledger",
        "membership_policy": {"policy_id": "rb1_greedy_voxel_support_v1", "voxel_size_m": 0.025},
        "room_belief_policy": {"policy_id": "fixture-RB1-v1", "approval_status": "proposed_unapproved"},
        "semantic_policy": {"policy_id": "fixture-consensus-v1"},
        "variants": ["mean_no_neutral"],
        "a_table_digests": {"mean_no_neutral": "a" * 64},
        "a_score_field": "A_log_ratio_uncalibrated",
        "source_a_zip": str(a_zip_path), "source_a_zip_sha256": sha(a_zip_path),
        "implementation_provenance": {"runner_path": str(runner_path), "runner_sha256": sha(runner_path),
                                      "ledger_path": str(ledger_path), "ledger_sha256": sha(ledger_path)},
    }
    for frame in range(2):
        revision = frame + 1
        timestamp = float(frame) * 0.5
        room_revision = frame + 19  # Independent room revision IDs are allowed.
        room_ref = f"room_revisions/room-{room_revision}.json"
        room_state = {
            "scene_id": scene_id, "sequence_id": sequence_id, "revision": room_revision,
            "timestamp_s": timestamp,
            "nodes": [
                {"node_id": f"agent:{frame}", "kind": "agent", "attributes": {
                    "frame_id": f"fixture-frame-{frame:04d}", "pose_W": [float(frame), 0.0, 0.0],
                    "source_metadata": {"canonical_world": CANONICAL_WORLD,
                                         "frame_id_coordinate": "world", "depth_units": "m",
                                         "depth_scale_to_m": 1.0}}},
                {"node_id": "room:fixture", "kind": "room", "attributes": {
                    "outer_ring": [[-1, -1], [2, -1], [2, 2], [-1, 2]],
                    "holes": [], "centroid_xy": [0.5, 0.5]}}
            ], "edges": []}
        write_json(timeline / room_ref, {"snapshot": room_state})

        points = np.array([[0.2 + frame, 0.3, 0.1], [0.4 + frame, 0.5, 0.2]], dtype=np.float32)
        cloud_rel = f"geometry/rev-{revision:06d}/object.npz"
        cloud_path = timeline / cloud_rel
        cloud_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cloud_path, xyz=points, rgb=np.full((2, 3), 100, dtype=np.uint8))
        cloud_hash = sha(cloud_path)
        object_id = "hypothesis_fixture"
        observation_ids = [f"fixture-frame-{i:04d}:det-0000" for i in range(frame + 1)]
        snapshot_rel = f"snapshots/rev-{revision:06d}.json"
        snapshot = {
            "scene_id": scene_id, "sequence_id": sequence_id, "frame_index": frame,
            "frame_id": f"fixture-frame-{frame:04d}", "revision": revision,
            "timestamp_s": timestamp, "future_observations_used": False,
            "stage": "online_hypotheses", "camera_pose_W_Copt": [
                [1, 0, 0, float(frame)], [0, 1, 0, 0], [0, 0, 1, 1], [0, 0, 0, 1]],
            "objects": [{"hypothesis_id": object_id, "cloud_npz": cloud_rel,
                         "cloud_sha256": cloud_hash, "point_count": len(points),
                         "bounds_minmax": [points.min(axis=0).tolist(), points.max(axis=0).tolist()],
                         "geometry_revision": f"geometry-{revision}", "class_history_raw": ["chair"],
                         "detection_count": len(observation_ids), "distinct_frame_count": len(observation_ids),
                         "distinct_pose_count": len(observation_ids), "observation_ids": observation_ids}],
            "rooms": {"state_ref": room_ref, "status": "prefix_state_only_native_replay_reference"},
        }
        write_json(timeline / snapshot_rel, snapshot)
        frame_rows.append({"frame_index": frame, "frame_id": snapshot["frame_id"],
                           "revision": revision, "snapshot": snapshot_rel,
                           "snapshot_sha256": sha(timeline / snapshot_rel)})

        causal_state = {
            "schema": "causal-hybrid-room-belief-revision/v1", "frame_index": frame,
            "frame_id": snapshot["frame_id"], "revision": revision, "timestamp_s": timestamp,
            "future_observations_used": False, "membership_causal": False,
            "membership_scope": {"status": "unavailable", "reason": "fixture scope unavailable",
                                 "coordinate_scope": COORDINATE_SCOPE},
            "room_semantics_status": "unavailable", "room_lifecycle": {
                "active_room_ids": ["room:fixture"], "created_room_ids": ["room:fixture"] if frame == 0 else [],
                "retired_room_ids": [], "room_revision": room_revision},
            "objects": [{"causal_object_id": "causal:fixture", "snapshot_hypothesis_id": object_id,
                         "observation_ids": observation_ids, "detection_count": len(observation_ids),
                         "distinct_frame_count": len(observation_ids), "distinct_pose_count": len(observation_ids),
                         "raw_class_history": ["chair"],
                         "prefix_admission": {"eligible": False, "status": "provisional", "reason": "fixture"},
                         "semantic": {"status": "unavailable", "category": None},
                         "membership": {"status": "unavailable", "reason": "fixture scope unavailable"},
                         "room_evidence_eligible": False}],
            "rooms": {"mean_no_neutral": {"room:fixture": {"status": "unavailable", "label": None}}},
            "room_contains_edges": [],
        }
        state_rel = f"revisions/rev-{revision:06d}.json"
        write_json(causal / state_rel, causal_state)
        causal_rows.append({"frame_index": frame, "frame_id": snapshot["frame_id"],
                            "revision": revision, "state": state_rel, "future_observations_used": False})

    write_json(timeline / "frame_index.json", {"frames": frame_rows})
    write_json(timeline / "manifest.json", {
        "schema": "conceptgraphs-causal-object-timeline/v1", "status": "complete",
        "scene_id": scene_id, "sequence_id": sequence_id, "cache_root": str(capture_root),
        "mapping_output": str(mapping_output),
        "frame_count": 2, "revision_count": 2, "snapshot_index": "frame_index.json",
        "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference"},
        "input_hashes": {"capture_manifest_sha256": sha(capture_path)},
    })
    policy_fingerprint = cr.canonical_hash({key: policies[key] for key in cr.POLICY_FIELDS})
    timeline_manifest_hash = sha(timeline / "manifest.json")
    write_json(causal / "manifest.json", {
        "schema": "causal-hybrid-room-belief-timeline/v1", "status": "complete",
        "scene_id": scene_id, "sequence_id": sequence_id, "frame_count": 2,
        "source_timeline_manifest_sha256": timeline_manifest_hash,
        "revisions": causal_rows, "membership_scope": {"status": "unavailable",
            "coordinate_scope": COORDINATE_SCOPE, "reason": "fixture scope unavailable"},
        "causality": {"membership": "unavailable_elevation_scope", "room_semantics": "unavailable"},
        "implementation_provenance": {"runner_sha256": "b" * 64},
        **policies,
    })
    config = {
        "schema": cr.INPUT_SCHEMA, "expected_identity": {"scene_id": scene_id, "sequence_id": sequence_id},
        "inputs": {
            "object_timeline": {"root": "../source/object_timeline",
                                "manifest_sha256": sha(timeline / "manifest.json")},
            "causal_state": {"root": "../source/causal_state",
                             "manifest_sha256": sha(causal / "manifest.json")}},
        "frame_alignment": "frame_index+frame_id+timestamp_s; room revision via object snapshot state_ref",
        "coordinate_contract": {"object_frame": "capture/world", "room_frame": "capture/world_xy",
                                "units": "m", "up_axis": "Z", "transform_convention": CANONICAL_WORLD},
        "policy_fingerprint_sha256": policy_fingerprint,
        "viewer": {"max_points_per_object": 25},
    }
    config_path = root / "configs/cached-inputs.json"
    write_json(config_path, config)
    return config_path


def run_fixture(config: Path, output: Path, root: Path, **kwargs):
    return cr.run_cached_replay(config, output, command=["fixture-cached-replay"],
                                workspace_root=root, **kwargs)


def test_cached_replay_builds_unified_viewer_and_matches_builder_output(tmp_path: Path):
    config = fixture_inputs(tmp_path)
    output = tmp_path / "artifacts/cached_replays/cached_replay_fixture_a"
    result = run_fixture(config, output, tmp_path)
    assert result["status"] == "completed"
    states = replay_unified_snapshots(output / "unified")
    assert len(states) == 2
    assert all(state["membership_availability"]["status"] == "unavailable" for state in states)
    assert all(not [edge for edge in state["edges"] if edge["relation"] == "room_contains"] for state in states)
    assert all(room["semantic_by_variant"]["mean_no_neutral"]["status"] == "unavailable"
               for state in states for room in state["rooms"])
    assert len(json.loads((output / "viewer/timeline_index.json").read_text())["frames"]) == 2

    direct = tmp_path / "artifacts/direct_b_builder"
    validated = cr.validate_inputs(config, workspace_root=tmp_path)
    direct_manifest = cr._build_unified(validated["timeline"], validated["causal_state"], direct)
    assert direct_manifest["revision_count"] == len(states)
    assert replay_unified_snapshots(direct) == states


def test_cached_replay_hash_and_identity_mismatch_fail_before_builder(tmp_path: Path, monkeypatch):
    config = fixture_inputs(tmp_path)
    config_doc = json.loads(config.read_text())
    config_doc["inputs"]["object_timeline"]["manifest_sha256"] = "0" * 64
    write_json(config, config_doc)
    output = tmp_path / "artifacts/cached_replays/cached_replay_bad_hash"
    monkeypatch.setattr(cr, "_build_unified", lambda *args: pytest.fail("builder must not run"))
    with pytest.raises(RuntimeError, match="manifest SHA-256 mismatch"):
        run_fixture(config, output, tmp_path)
    manifest = json.loads((output / "run_manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["stages"]["input_preflight"]["status"] == "failed"

    config = fixture_inputs(tmp_path / "identity")
    config_doc = json.loads(config.read_text())
    config_doc["expected_identity"]["sequence_id"] = "wrong-route"
    write_json(config, config_doc)
    with pytest.raises(RuntimeError, match="scene/sequence"):
        run_fixture(config, tmp_path / "identity/artifacts/cached_replays/cached_replay_bad_identity",
                    tmp_path / "identity")


def test_cached_replay_missing_asset_and_alignment_mismatch_fail_preflight(tmp_path: Path, monkeypatch):
    config = fixture_inputs(tmp_path)
    config_doc = json.loads(config.read_text())
    causal_root = (config.parent / config_doc["inputs"]["causal_state"]["root"]).resolve()
    state_path = causal_root / "revisions/rev-000002.json"
    state = json.loads(state_path.read_text())
    state["frame_id"] = "wrong-frame"
    write_json(state_path, state)
    output = tmp_path / "artifacts/cached_replays/cached_replay_bad_alignment"
    monkeypatch.setattr(cr, "_build_unified", lambda *args: pytest.fail("builder must not run"))
    with pytest.raises(RuntimeError, match="causal manifest/state alignment mismatch"):
        run_fixture(config, output, tmp_path)
    assert json.loads((output / "run_manifest.json").read_text())["status"] == "failed"

    config = fixture_inputs(tmp_path / "missing")
    cfg = json.loads(config.read_text())
    causal_root = (config.parent / cfg["inputs"]["causal_state"]["root"]).resolve()
    (causal_root / "revisions/rev-000002.json").unlink()
    with pytest.raises(RuntimeError, match="causal revision for frame 1 is missing"):
        run_fixture(config, tmp_path / "missing/artifacts/cached_replays/cached_replay_missing",
                    tmp_path / "missing")


def test_cached_replay_existing_output_and_builder_failure_never_report_completed(tmp_path: Path, monkeypatch):
    config = fixture_inputs(tmp_path)
    output = tmp_path / "artifacts/cached_replays/cached_replay_existing"
    output.mkdir(parents=True)
    marker = output / "keep.txt"
    marker.write_text("preserve")
    with pytest.raises(FileExistsError):
        run_fixture(config, output, tmp_path)
    assert marker.read_text() == "preserve"

    failing_output = tmp_path / "artifacts/cached_replays/cached_replay_builder_failure"
    monkeypatch.setattr(cr, "_build_unified", lambda *args: (_ for _ in ()).throw(ValueError("fixture builder failure")))
    with pytest.raises(RuntimeError, match="fixture builder failure"):
        run_fixture(config, failing_output, tmp_path)
    manifest = json.loads((failing_output / "run_manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"]["stage"] == "unified_graph_builder"
    assert not (failing_output / "viewer/viewer_manifest.json").exists()


def test_launcher_exposes_cached_replay_and_busy_open_port_is_not_replaced(tmp_path: Path, monkeypatch, capsys):
    import urllib.request  # Import ssl before the socket probe is replaced below.
    assert "cached-replay" in sl.COMMANDS
    sl.Launcher().handle(["help", "cached-replay"])
    assert "--config" in capsys.readouterr().out

    run_id = "cached_replay_busy_port_test"
    root = sl.CACHED_REPLAYS / run_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "viewer").mkdir(exist_ok=True)
    (root / "viewer/index.html").write_text("<!doctype html><html></html>")
    write_json(root / "run_manifest.json", {"status": "completed"})
    class BusySocket:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def bind(self, _address): raise OSError("address already in use")

    import socket
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: BusySocket())
    with pytest.raises(ValueError, match="is busy"):
        sl.Launcher().cmd_open([run_id, "timeline", "--port", "9876"])
    # This test uses an isolated run root in the workspace; remove only
    # its own temporary test tree.
    import shutil
    shutil.rmtree(root)


def test_cached_replay_open_uses_viewer_subdirectory_and_localhost(tmp_path: Path, monkeypatch, capsys):
    import socket
    import urllib.request

    run_id = "cached_replay_open_test"
    root = sl.CACHED_REPLAYS / run_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "viewer").mkdir(exist_ok=True)
    (root / "viewer/index.html").write_text("<!doctype html><html></html>")
    write_json(root / "run_manifest.json", {"status": "completed"})
    commands = []

    class FreeSocket:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def bind(self, _address): return None

    class DummyResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self, _size): return b"<!doctype html><html></html>"

    class DummyProcess:
        pid = 12345
        def poll(self): return None

    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: FreeSocket())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: DummyResponse())
    monkeypatch.setattr(sl.subprocess, "Popen", lambda command, **_kwargs: (commands.append(command) or DummyProcess()))
    try:
        assert sl.Launcher().cmd_open([run_id, "timeline", "--port", "9876"]) == 0
        assert len(commands) == 2  # server and existing browser launcher
        server_command = commands[0]
        assert server_command[server_command.index("--host") + 1] == "127.0.0.1"
        assert server_command[server_command.index("--viewer") + 1] == (root / "viewer").relative_to(sl.ROOT).as_posix()
        assert "http://127.0.0.1:9876/" in capsys.readouterr().out
    finally:
        import shutil
        shutil.rmtree(root)
