from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from adapters.habitat.raw import canonical_from_habitat_matrix
from integration_v2.camera_live_contract import (
    CameraLiveContractError,
    BoundedPublishedKeyframeQueue,
    build_joined_perception_packet,
    assert_no_incomplete_jsonl_tail,
    host_monotonic_domain,
    read_complete_jsonl,
    send_mapper_pair,
    validate_current_source_packet,
    validate_published_event,
    validate_mapper_pair_events,
    evaluate_live_overlap,
)
from integration_v2 import live_processing
from standalone_sg.serialization.live_revision_io import canonical_json, sha256_file
from integration_v2.perception_feature_sessions import FeatureContract, OpenCLIPFeatureSession
from scripts import run_camera_live_pilot as pilot
from scripts import run_t2_d_cached_packet_worker as mapper_worker
from scripts.run_habitat_smooth_route_preview import _write_keyframe


def test_camera_and_overall_pipeline_timeout_budgets_are_distinct():
    resolved = pilot.resolve_timeout_budgets(
        {"route_budget_proposal": {"camera_runner_timeout_s": 240}},
        overall_timeout_s=1200)
    assert resolved == {
        "overall_pipeline_timeout_s": 1200.0,
        "overall_deadline_includes_queue_drain": True,
        "camera_route_timeout_configured_s": 240.0,
        "camera_route_timeout_effective_s": 240.0,
        "camera_route_timeout_capped_by_overall": False,
        "source": "route_budget_proposal.camera_runner_timeout_s",
    }


def test_camera_timeout_is_capped_by_remaining_pipeline_budget():
    resolved = pilot.resolve_timeout_budgets(
        {"route_budget_proposal": {"camera_runner_timeout_s": 240}},
        overall_timeout_s=120)
    assert resolved["camera_route_timeout_configured_s"] == 240.0
    assert resolved["camera_route_timeout_effective_s"] == 120.0
    assert resolved["camera_route_timeout_capped_by_overall"] is True


@pytest.mark.parametrize("overall,camera", [(0, None), (float("nan"), None), (1200, 0), (1200, float("inf"))])
def test_timeout_budgets_reject_nonpositive_or_nonfinite_values(overall, camera):
    with pytest.raises(ValueError):
        pilot.resolve_timeout_budgets(
            {"route_budget_proposal": {"camera_runner_timeout_s": 240}},
            overall_timeout_s=overall, camera_timeout_override_s=camera)


class FakeP2:
    def __init__(self, detections):
        self.detections = detections
        self.inputs = []
        self.closed = False
        self.model_load_count = 1
        self.processed_frame_ids = ()

    def update_frame(self, packet):
        self.inputs.append(packet)
        return self.detections

    def close(self):
        self.closed = True


class FakeP3:
    def __init__(self, rejected=False):
        self.inputs = []
        self.rejected = rejected
        self.closed = False
        self.processed_frame_ids = ()

    def update_frame(self, packet):
        self.inputs.append(packet)
        rows = []
        for det in packet["p2_detections"]:
            rows.append({"detection_id": det["detection_id"], "status":
                         "rejected_test_fixture" if self.rejected else "accepted",
                         "decision": "rejected" if self.rejected else "accepted"})
        return {"frame_id": packet["frame_id"], "frame_index": packet["frame_index"],
                "timestamp_s": packet["timestamp_s"], "detections": rows}

    def close(self):
        self.closed = True


class FailP2(FakeP2):
    def update_frame(self, packet):
        self.inputs.append(packet)
        raise RuntimeError("fixture inference failure")


def _run_worker(monkeypatch, kind, args, msg, hooks):
    monkeypatch.setattr(pilot.sys, "stdin", io.StringIO(json.dumps(msg) + "\n" + json.dumps({"op": "close"}) + "\n"))
    return pilot.worker_main(kind, args, test_hooks=hooks)


def _fixture(tmp_path: Path, monkeypatch, *, detection_count=1):
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    monkeypatch.setattr(mapper_worker, "ROOT", tmp_path)
    run_root = tmp_path / "run"
    run_root.mkdir()
    pilot.atomic_json(run_root / "run_manifest.json", {"run_id": "fixture-pilot-run"})
    timestamp = 1.375
    clocks = {"timestamp_s": timestamp, "timestamp_clock_domain": "habitat_simulation",
              "capture_wall_time_unix_s": 1792330000.25,
              "capture_wall_time_clock_domain": "unix_epoch_utc", "capture_monotonic_s": 25432.125,
              "capture_monotonic_domain": host_monotonic_domain()}
    rgb = np.full((8, 10, 3), 127, dtype=np.uint8)
    depth = np.full((8, 10), 2.0, dtype=np.float32)
    valid = np.ones((8, 10), dtype=bool)
    pose = np.eye(4).tolist()
    metadata = {"schema": "smooth-route-raw-keyframe/v1", "frame_id": "fixture-seq-cam-000007",
        "frame_index": 7, "scene_id": "fixture-scene", "sequence_id": "fixture-seq",
        "dataset_id": "fixture", "route_id": "fixture-route", **clocks,
        "width": 10, "height": 8, "resolution_px": [10, 8], "horizontal_fov_deg": 90.0,
        "calibration_id": "fixture-k-v1", "intrinsics": {"fx": 5.0, "fy": 5.0, "cx": 4.5, "cy": 3.5},
        "depth_units": "m", "depth_representation": "axial", "min_depth_m": 0.1,
        "max_depth_m": 10.0, "canonical_from_habitat": canonical_from_habitat_matrix().tolist(),
        "sensor_position_habitat": [0.0, 0.0, 0.0], "sensor_rotation_habitat": np.eye(3).tolist(),
        "base_pose_canonical": pose, "base_pose_habitat": pose, "sensor_pose_habitat": pose}
    raw_packet = {"frame_id": metadata["frame_id"], "rgb": rgb, "depth_m": depth,
                  "valid_mask": valid, "metadata": metadata}
    keyframe = _write_keyframe(raw_packet, run_root)
    event = {"schema": "camera-live-keyframe-event/v1",
        "packet": {"frame_id": metadata["frame_id"], "metadata": metadata},
        "result": keyframe, "published_wall_time_s": 1792330000.5,
        "published_wall_time_unix_s": 1792330000.5, "published_monotonic_s": 25432.25,
        "published_monotonic_domain": clocks["capture_monotonic_domain"]}
    validate_published_event(event)
    base = pilot.write_capture_packet(run_root, event["packet"], keyframe,
        {"scene_asset": "fixture.glb", "min_depth_m": 0.1, "max_depth_m": 10.0,
         "scene_id": "fixture-scene", "sequence_id": "fixture-seq"})
    base["asset_root"] = str(run_root / "packets" / "assets")
    p2_detections = []
    for i in range(detection_count):
        mask = np.zeros((8, 10), dtype=bool)
        mask[1:5, 2 + i:5 + i] = True
        mask_root = run_root / "packets" / "assets" / "detection_2d" / "masks"
        mask_root.mkdir(parents=True, exist_ok=True)
        mask_path = mask_root / f"obs-{i}.npy"
        np.save(mask_path, mask, allow_pickle=False)
        p2_detections.append({"detection_id": f"{metadata['frame_id']}:det-{i:04d}",
            "class_id": 1, "class_raw": "chair", "class_canonical": "chair", "score_raw": 0.9,
            "bbox_xyxy": [2.0 + i, 1.0, 5.0 + i, 5.0],
            "mask_file": str(mask_path.relative_to(run_root / "packets" / "assets" / "detection_2d")),
            "mask_sha256": pilot.sha(mask_path), "image_shape_hw": [8, 10]})
    base["p2_detections"] = p2_detections
    base["asset_root"] = str(run_root / "packets" / "assets")
    frame_json = run_root / "packets" / "frame-000007.json"
    frame_json.parent.mkdir(parents=True, exist_ok=True)
    pilot.atomic_json(frame_json, base)
    return run_root, base, frame_json


def test_runtime_ipc_preserves_source_timestamp_through_p2_p3_features_and_join(tmp_path, monkeypatch):
    run_root, base, frame_json = _fixture(tmp_path, monkeypatch)
    mask_path = Path(base["asset_root"]) / "detection_2d" / base["p2_detections"][0]["mask_file"]
    mask = np.load(mask_path, allow_pickle=False)
    expected_det = base["p2_detections"][0]
    p2_session = FakeP2([{"class_id": 1, "class_raw": "chair", "score": 0.9,
                          "bbox_xyxy": expected_det["bbox_xyxy"], "mask": mask}])
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"runtime": {}, "preprocessing": {}, "vocabulary": {"canonical_map": {}}}))
    names = tmp_path / "names.txt"
    names.write_text("chair\n")
    p2_resp = run_root / "p2.json"
    p2_ready = run_root / "ready" / "p2.json"
    _run_worker(monkeypatch, "p2", argparse.Namespace(profile=profile, names=names,
        weights=tmp_path / "fake.onnx", run_root=run_root, ready_file=p2_ready,
        run_id="fixture-run", bootstrap_id="fixture-bootstrap", route_config_sha256="a" * 64),
        {"op": "frame", "frame_json": str(frame_json), "response_json": str(p2_resp)},
        {"session": p2_session})
    p2_output = pilot.read(p2_resp)
    assert pilot.read(p2_ready)["schema"] == "camera-live-worker-ready/v2"
    assert p2_session.closed
    assert p2_session.inputs[0]["timestamp_s"] == base["timestamp_s"]
    assert p2_session.inputs[0]["timestamp_clock_domain"] == "habitat_simulation"
    assert p2_session.inputs[0]["capture_monotonic_s"] == base["capture_monotonic_s"]

    p3_session = FakeP3(rejected=True)
    packet_json = run_root / "packets" / "p3-input.json"
    base["p2_detections"] = [dict(row) for row in p2_output["detections"]]
    pilot.atomic_json(packet_json, base)
    p3_resp = run_root / "p3.json"
    _run_worker(monkeypatch, "p3", argparse.Namespace(profile=profile, run_root=run_root, ready_file=None),
        {"op": "frame", "packet_json": str(packet_json), "response_json": str(p3_resp)},
        {"session": p3_session})
    p3_output = pilot.read(p3_resp)
    assert p3_session.inputs[0]["timestamp_s"] == base["timestamp_s"]
    assert p3_output["detections"][0]["status"] == "rejected_test_fixture"

    calls = {"image": 0, "text": 0}
    vector = np.zeros(512, dtype=np.float32); vector[0] = 1.0
    def image_encoder(_rgb, _bbox):
        calls["image"] += 1
        return vector.copy()
    def text_encoder(_text):
        calls["text"] += 1
        return vector.copy(), [1, 2]
    feature_session = OpenCLIPFeatureSession.open(checkpoint=tmp_path / "fake.pt",
        contract=FeatureContract("a" * 64, "fixture-model", "fixture-tokenizer", "fixture-preprocess", "raw"),
        image_encoder=image_encoder, text_encoder=text_encoder)
    feature_input = {**base, "p2_detections": p2_output["detections"]}
    pilot.atomic_json(packet_json, feature_input)
    f_resp = run_root / "features.json"
    _run_worker(monkeypatch, "features", argparse.Namespace(checkpoint=tmp_path / "fake.pt",
        checkpoint_sha256="a" * 64, model_fingerprint="fixture-model", preprocessing_id="fixture-preprocess",
        run_root=run_root, ready_file=None), {"op": "frame", "packet_json": str(packet_json),
        "response_json": str(f_resp)}, {"session": feature_session})
    feature_output = pilot.read(f_resp)
    assert calls == {"image": 1, "text": 1}
    assert feature_output["rows"][0]["observation_id"] == p2_output["detections"][0]["detection_id"]

    joined = build_joined_perception_packet(base, p2_output, p3_output, feature_output)
    assert joined["timestamp_s"] == 1.375
    assert joined["p3_detections"][0]["decision"] == "rejected"
    assert len(joined["features"]) == len(joined["p2_detections"]) == 1
    assert joined["perception_status"] == "completed"
    cg_boundary = mapper_worker._validate_current_packet(joined, tmp_path, "conceptgraphs")
    occ_boundary = mapper_worker._validate_current_packet(joined, tmp_path, "occusg")
    assert cg_boundary["timestamp_s"] == occ_boundary["timestamp_s"] == base["timestamp_s"]
    assert cg_boundary["source_hashes"] == occ_boundary["source_hashes"] == base["source_hashes"]
    mapper_packet_path = run_root / "packets" / "mapper-ready.json"
    pilot.atomic_json(mapper_packet_path, joined)
    mapper_msg = {"op": "frame", "packet_path": str(mapper_packet_path),
                  "packet_sha256": pilot.sha(mapper_packet_path)}
    ack_root = run_root / "fake_mapper_acks"; ack_root.mkdir()
    fake_mapper = """
import json, pathlib, sys
kind = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    packet = json.loads(pathlib.Path(msg['packet_path']).read_text())
    target = pathlib.Path(sys.argv[2]) / (kind + '.json')
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps({'kind':kind,'frame_id':packet['frame_id'],'frame_index':packet['frame_index'],
        'timestamp_s':packet['timestamp_s'],'source_hashes':packet['source_hashes'],'packet_sha256':msg['packet_sha256']}))
    tmp.replace(target)
"""
    mapper_procs = [subprocess.Popen([sys.executable, "-u", "-c", fake_mapper, kind, str(ack_root)],
        stdin=subprocess.PIPE, text=True, bufsize=1) for kind in ("conceptgraphs", "occusg")]
    try:
        send_mapper_pair(pilot.send, mapper_procs, mapper_msg)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(list(ack_root.glob("*.json"))) < 2:
            time.sleep(.01)
        received = [pilot.read(path) for path in sorted(ack_root.glob("*.json"))]
        assert {row["kind"] for row in received} == {"conceptgraphs", "occusg"}
        assert all(row["frame_id"] == joined["frame_id"] and row["frame_index"] == joined["frame_index"]
                   and row["timestamp_s"] == joined["timestamp_s"]
                   and row["source_hashes"] == joined["source_hashes"] for row in received)
    finally:
        for proc in mapper_procs:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate(); proc.wait(timeout=3)
    object_event, room_event = _fake_branch_events(tmp_path / "unified", joined)
    validate_mapper_pair_events(joined, object_event, room_event)
    viewer = (tmp_path / "unified" / "viewer"); viewer.mkdir(parents=True)
    unified_frames = []
    unified = live_processing._join_one(tmp_path / "unified", viewer,
        {"world_bounds_xy": [-2, 2, -2, 2]}, unified_frames, object_event, room_event)
    assert unified["source_frame_id"] == joined["frame_id"]
    assert unified["source_frame_index"] == joined["frame_index"]
    assert unified["timestamp_s"] == joined["timestamp_s"]
    unified_state = pilot.read(tmp_path / "unified" / unified["joined_state"])
    assert unified_state["future_observations_used"] is False


def _fake_branch_events(root: Path, packet: dict):
    root.mkdir(parents=True, exist_ok=True)
    obj_path = root / "object.json"
    room_path = root / "room.json"
    pilot.atomic_json(obj_path, {"scene_id": packet["scene_id"], "sequence_id": packet["sequence_id"],
        "revision": 1, "frame_index": packet["frame_index"], "frame_id": packet["frame_id"],
        "timestamp_s": packet["timestamp_s"], "camera_pose_W_Copt": np.eye(4).tolist(), "objects": []})
    pilot.atomic_json(room_path, {"scene_id": packet["scene_id"], "sequence_id": packet["sequence_id"],
        "revision": 9, "timestamp_s": packet["timestamp_s"], "nodes": [], "edges": []})
    def make(branch, path, revision):
        rel = path.relative_to(root).as_posix()
        event = {"schema": "t2-d-branch-event/v1", "branch": branch, "event_seq": 1,
            "source_frame_index": packet["frame_index"], "source_frame_id": packet["frame_id"],
            "source_timestamp_s": packet["timestamp_s"], "source_timestamp_clock_domain": packet["timestamp_clock_domain"],
            "source_capture_wall_time_unix_s": packet["capture_wall_time_unix_s"],
            "source_capture_monotonic_s": packet["capture_monotonic_s"],
            "source_capture_monotonic_domain": packet["capture_monotonic_domain"],
            "source_hashes": packet["source_hashes"], "source_revision": revision,
            "revision_kind": "fixture", "state_path": rel, "state_sha256": sha256_file(path),
            "asset_sha256": {rel: sha256_file(path)}}
        event["event_sha256"] = hashlib.sha256(canonical_json(event)).hexdigest()
        return event
    return make("objects", obj_path, 1), make("rooms", room_path, 9)


def test_empty_p2_is_valid_and_p3_rejection_does_not_drop_feature_population(tmp_path, monkeypatch):
    run_root, base, frame_json = _fixture(tmp_path, monkeypatch, detection_count=0)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"runtime": {}, "preprocessing": {}, "vocabulary": {"canonical_map": {}}}))
    names = tmp_path / "names.txt"; names.write_text("chair\n")
    p2_resp = run_root / "empty-p2.json"
    _run_worker(monkeypatch, "p2", argparse.Namespace(profile=profile, names=names,
        weights=tmp_path / "fake.onnx", run_root=run_root, ready_file=None),
        {"op": "frame", "frame_json": str(frame_json), "response_json": str(p2_resp)},
        {"session": FakeP2([])})
    empty_p2 = pilot.read(p2_resp)
    base_with_p2 = {**base, "p2_detections": []}
    packet_json = run_root / "packets" / "empty-input.json"; pilot.atomic_json(packet_json, base_with_p2)
    p3_resp = run_root / "empty-p3.json"
    _run_worker(monkeypatch, "p3", argparse.Namespace(profile=profile, run_root=run_root, ready_file=None),
        {"op": "frame", "packet_json": str(packet_json), "response_json": str(p3_resp)},
        {"session": FakeP3()})
    p3 = pilot.read(p3_resp)
    feature_session = OpenCLIPFeatureSession.open(checkpoint=tmp_path / "fake.pt",
        contract=FeatureContract("b" * 64, "fixture-model", "fixture-tokenizer", "fixture-preprocess", "raw"),
        image_encoder=lambda _rgb, _bbox: np.zeros(512, dtype=np.float32),
        text_encoder=lambda _text: (np.zeros(512, dtype=np.float32), []))
    f_resp = run_root / "empty-features.json"
    _run_worker(monkeypatch, "features", argparse.Namespace(checkpoint=tmp_path / "fake.pt",
        checkpoint_sha256="b" * 64, model_fingerprint="fixture-model", preprocessing_id="fixture-preprocess",
        run_root=run_root, ready_file=None), {"op": "frame", "packet_json": str(packet_json),
        "response_json": str(f_resp)}, {"session": feature_session})
    features = pilot.read(f_resp)
    joined = build_joined_perception_packet(base_with_p2, empty_p2, p3, features)
    assert joined["perception_status"] == "empty_detections"
    assert joined["p2_detections"] == joined["p3_detections"] == joined["features"] == []
    assert mapper_worker._validate_current_packet(joined, tmp_path, "conceptgraphs")["observation_count"] == 0
    assert mapper_worker._validate_current_packet(joined, tmp_path, "occusg")["timestamp_s"] == base["timestamp_s"]


def test_missing_timestamp_and_hash_mismatch_stop_before_session_update(tmp_path, monkeypatch):
    run_root, base, frame_json = _fixture(tmp_path, monkeypatch)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"runtime": {}, "preprocessing": {}, "vocabulary": {"canonical_map": {}}}))
    names = tmp_path / "names.txt"; names.write_text("chair\n")
    p2 = FakeP2([])
    bad = dict(base); bad.pop("timestamp_s")
    bad_path = run_root / "missing-time.json"; pilot.atomic_json(bad_path, bad)
    with pytest.raises(CameraLiveContractError, match="timestamp_s"):
        _run_worker(monkeypatch, "p2", argparse.Namespace(profile=profile, names=names,
            weights=tmp_path / "fake.onnx", run_root=run_root, ready_file=None),
            {"op": "frame", "frame_json": str(bad_path), "response_json": str(run_root / "bad-time-response.json")},
            {"session": p2})
    assert not p2.inputs

    class NeverFeature(FakeP2):
        pass
    feature_session = NeverFeature([])
    feature_packet = dict(base)
    feature_packet["p2_detections"] = base["p2_detections"]
    feature_packet.pop("timestamp_s")
    feature_path = run_root / "feature-missing-time.json"; pilot.atomic_json(feature_path, feature_packet)
    with pytest.raises(CameraLiveContractError, match="timestamp_s"):
        _run_worker(monkeypatch, "features", argparse.Namespace(checkpoint=tmp_path / "fake.pt",
            checkpoint_sha256="c" * 64, model_fingerprint="fixture-model", preprocessing_id="fixture-preprocess",
            run_root=run_root, ready_file=None), {"op": "frame", "packet_json": str(feature_path),
            "response_json": str(run_root / "feature-missing-time-response.json")}, {"session": feature_session})
    assert not feature_session.inputs

    bad_hash = dict(base); bad_hash["source_hashes"] = {**base["source_hashes"], "rgb.npy": "0" * 64}
    bad_hash_path = run_root / "bad-hash.json"; pilot.atomic_json(bad_hash_path, bad_hash)
    with pytest.raises(CameraLiveContractError, match="source hashes mismatch"):
        _run_worker(monkeypatch, "p2", argparse.Namespace(profile=profile, names=names,
            weights=tmp_path / "fake.onnx", run_root=run_root, ready_file=None),
            {"op": "frame", "frame_json": str(bad_hash_path), "response_json": str(run_root / "bad-hash-response.json")},
            {"session": p2})
    assert not p2.inputs

    p3_session = FakeP3()
    p3_packet = dict(base)
    bad_mask_path = Path(base["asset_root"]) / "detection_2d" / base["p2_detections"][0]["mask_file"]
    np.save(bad_mask_path, np.zeros((8, 10), dtype=bool), allow_pickle=False)
    p3_path = run_root / "p3-bad-mask.json"; pilot.atomic_json(p3_path, p3_packet)
    with pytest.raises(CameraLiveContractError, match="mask missing/hash mismatch"):
        _run_worker(monkeypatch, "p3", argparse.Namespace(profile=profile, run_root=run_root, ready_file=None),
            {"op": "frame", "packet_json": str(p3_path), "response_json": str(run_root / "p3-bad-mask-response.json")},
            {"session": p3_session})
    assert not p3_session.inputs


def test_failed_fake_inference_is_not_empty_success(tmp_path, monkeypatch):
    run_root, base, frame_json = _fixture(tmp_path, monkeypatch)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"runtime": {}, "preprocessing": {}, "vocabulary": {"canonical_map": {}}}))
    names = tmp_path / "names.txt"; names.write_text("chair\n")
    session = FailP2([])
    response = run_root / "failed-p2.json"
    with pytest.raises(RuntimeError, match="fixture inference failure"):
        _run_worker(monkeypatch, "p2", argparse.Namespace(profile=profile, names=names,
            weights=tmp_path / "fake.onnx", run_root=run_root, ready_file=None),
            {"op": "frame", "frame_json": str(frame_json), "response_json": str(response)},
            {"session": session})
    assert session.inputs and session.closed
    assert not response.exists()


def test_jsonl_reader_waits_for_complete_event_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"event":"complete"}\n{"event":"par')
    rows, offset = read_complete_jsonl(path)
    assert rows == [{"event": "complete"}]
    assert offset == len(b'{"event":"complete"}\n')
    with pytest.raises(CameraLiveContractError, match="incomplete record"):
        assert_no_incomplete_jsonl_tail(path, offset)
    with path.open("ab") as stream:
        stream.write(b'tial"}\n')
    rows, next_offset = read_complete_jsonl(path, offset)
    assert rows == [{"event": "partial"}]
    assert next_offset == path.stat().st_size


def test_bounded_runtime_queue_logs_supersede_before_dispatch_only():
    events = []
    queue = BoundedPublishedKeyframeQueue(events.append)
    def event(fid):
        return {"packet": {"frame_id": fid}}
    queue.offer(event("f0")); queue.offer(event("f1"))
    admitted = queue.get(timeout_s=.01)
    assert admitted["packet"]["frame_id"] == "f1"
    queue.offer(event("f2"))
    queue.mark_processed("f1")
    assert queue.get(timeout_s=.01)["packet"]["frame_id"] == "f2"
    queue.mark_processed("f2"); queue.close()
    assert [(row["event"], row["frame_id"]) for row in events] == [
        ("queued", "f0"), ("superseded", "f0"), ("queued", "f1"),
        ("admitted_to_processing", "f1"), ("queued", "f2"),
        ("processed", "f1"), ("admitted_to_processing", "f2"), ("processed", "f2")]
    assert queue.queued == 3 and queue.admitted == 2 and queue.superseded == 1 and queue.processed == 2


def test_live_overlap_requires_shared_host_monotonic_domain():
    domain = host_monotonic_domain()
    result = evaluate_live_overlap(100.0, domain,
        [{"frame_id": "f1", "stage": "P2", "started_monotonic_s": 90.0, "clock_domain": domain}],
        [{"frame_id": "f1", "revision": 1, "committed_monotonic_s": 99.0, "clock_domain": domain}],
        [{"frame_id": "f1", "captured_monotonic_s": 80.0, "clock_domain": domain}])
    assert result["perception_started_before_capture_end"] is True
    assert result["unified_revision_committed_before_capture_end"] is True
    assert result["first_keyframe_capture_to_p2_start_s"] == 10.0
    assert result["first_perception_start_to_same_frame_unified_commit_s"] == 9.0
    wrong_domain = evaluate_live_overlap(100.0, domain,
        [{"started_monotonic_s": 1.0, "clock_domain": "habitat_simulation"}],
        [{"committed_monotonic_s": 2.0, "clock_domain": "unix_epoch_seconds"}])
    assert wrong_domain["perception_started_before_capture_end"] is False
    assert wrong_domain["unified_revision_committed_before_capture_end"] is False
    assert wrong_domain["latency_comparison_clock_domain"] is None


def test_workers_must_publish_ready_before_camera_start(tmp_path):
    class Alive:
        pid = 123
        def poll(self):
            return None
    root = tmp_path
    domain = host_monotonic_domain()
    (root / "ready").mkdir()
    for kind in ("p2", "p3", "features", "conceptgraphs", "occusg_rooms"):
        pilot.atomic_json(root / "ready" / f"{kind}.json", pilot.ready_marker(kind=kind, pid=123,
            run_id="fixture-run", bootstrap_id="fixture-bootstrap", route_config_sha256="b" * 64,
            ready_monotonic_s=time.monotonic(), clock_domain=domain))
    ready = pilot._wait_ready_workers(root, [(kind, Alive()) for kind in
        ("p2", "p3", "features", "conceptgraphs", "occusg_rooms")], run_id="fixture-run",
        bootstrap_id="fixture-bootstrap", route_config_sha256="b" * 64, timeout_s=.1)
    assert set(ready["workers"]) == {"p2", "p3", "features", "conceptgraphs", "occusg_rooms"}
    assert ready["clock_domain"] == domain


def test_parent_reports_failed_worker_stage_without_waiting_for_timeout(tmp_path):
    class DeadWorker:
        def poll(self):
            return 17
    with pytest.raises(RuntimeError, match="P3 worker exited with code 17"):
        pilot.wait_worker_json(DeadWorker(), tmp_path / "missing.json", stage="P3", timeout=.5)


def test_queue_offer_racing_consumer_get_has_no_lost_or_double_dispatched_packet():
    for _ in range(25):
        events = []
        queue = BoundedPublishedKeyframeQueue(events.append)
        queue.offer({"packet": {"frame_id": "f0"}})
        barrier = threading.Barrier(3)
        taken = []
        def consume():
            barrier.wait()
            item = queue.get(timeout_s=.2)
            if item is not None:
                taken.append(item["packet"]["frame_id"])
        def replace():
            barrier.wait()
            queue.offer({"packet": {"frame_id": "f1"}})
        get_thread = threading.Thread(target=consume)
        put_thread = threading.Thread(target=replace)
        get_thread.start(); put_thread.start(); barrier.wait()
        get_thread.join(timeout=1); put_thread.join(timeout=1)
        assert not get_thread.is_alive() and not put_thread.is_alive()
        pending = queue.get(timeout_s=.01)
        if pending is not None:
            taken.append(pending["packet"]["frame_id"])
        assert taken.count("f1") == 1
        assert taken.count("f0") + sum(row["event"] == "superseded" and row["frame_id"] == "f0" for row in events) == 1
        assert len(taken) == len(set(taken))
        queue.close()


def test_queue_log_failure_is_not_treated_as_admission():
    def fail_on_admit(row):
        if row["event"] == "admitted_to_processing":
            raise OSError("disk log unavailable")
    queue = BoundedPublishedKeyframeQueue(fail_on_admit)
    queue.offer({"packet": {"frame_id": "f0"}})
    with pytest.raises(RuntimeError, match="admission logging failed"):
        queue.get(timeout_s=.01)
    assert queue.processed == 0
