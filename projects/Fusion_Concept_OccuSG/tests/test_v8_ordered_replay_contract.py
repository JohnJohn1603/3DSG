import pytest
import json
from pathlib import Path

from integration_v2.v8_ordered_replay import (
    EXPECTED_INDICES, _bootstrap_replay_viewer, _mapper_ready_worker_pairs,
    validate_locked_frames,
)
from integration_v2.camera_live_contract import host_monotonic_domain
from scripts.run_camera_live_pilot import _wait_ready_workers


def _rows(indices=None):
    indices = EXPECTED_INDICES if indices is None else indices
    return [{"frame_index": index, "frame_id": f"frame-{index:06d}"} for index in indices]


def test_locked_replay_accepts_all_13_indices_in_order():
    validate_locked_frames(_rows())


def test_locked_replay_rejects_missing_frame_before_inference():
    with pytest.raises(ValueError, match="differ from locked 13-frame sequence"):
        validate_locked_frames(_rows(EXPECTED_INDICES[:-1]))


def test_locked_replay_rejects_reordered_frame_before_inference():
    rows = _rows()
    rows[3], rows[4] = rows[4], rows[3]
    with pytest.raises(ValueError):
        validate_locked_frames(rows)


def test_locked_replay_rejects_duplicate_frame_identity():
    rows = _rows()
    rows[5]["frame_id"] = rows[4]["frame_id"]
    with pytest.raises(ValueError, match="duplicate IDs"):
        validate_locked_frames(rows)


def test_d2_bootstrap_runs_once_after_started_manifest(tmp_path, monkeypatch):
    import scripts.build_d2_xyz_viewer as d2

    run_root = tmp_path / "run"
    viewer = run_root / "viewer"
    viewer.mkdir(parents=True)
    (viewer / "live_index.json").write_text(json.dumps({"frames": []}))
    manifest = {"status": "preflight", "run_id": "fixture"}
    calls = []

    def fake_bootstrap(root, version):
        calls.append((root, version, json.loads((root / "run_manifest.json").read_text())["status"]))
        return {"viewer_entrypoint": "index_d2_fixture.html"}

    monkeypatch.setattr(d2, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(d2, "validate_entrypoint_bundle", lambda _viewer: {"status": "ready"})
    index, result = _bootstrap_replay_viewer(run_root, viewer, manifest)

    assert len(calls) == 1
    assert calls[0][1:] == ("v8_13_keyframes", "started")
    assert index == {"frames": []}
    assert result["viewer_entrypoint"] == "index_d2_fixture.html"
    assert manifest["viewer"]["bootstrap_status"] == "passed"
    assert manifest["status"] == "running_mapper_sessions"


def test_mapper_readiness_uses_occusg_rooms_contract_kind(tmp_path):
    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        @staticmethod
        def poll():
            return None

    root = tmp_path / "run"
    (root / "ready").mkdir(parents=True)
    cg, occ = FakeProcess(101), FakeProcess(102)
    pairs = _mapper_ready_worker_pairs(cg, occ)
    run_id, bootstrap_id, route_hash = "r1", "b1", "a" * 64
    for kind, proc in pairs:
        (root / "ready" / f"{kind}.json").write_text(json.dumps({
            "schema": "camera-live-worker-ready/v2",
            "kind": "occusg_rooms" if kind == "occusg" else kind,
            "run_id": run_id, "bootstrap_id": bootstrap_id,
            "route_config_sha256": route_hash, "pid": proc.pid,
            "clock_domain": host_monotonic_domain(),
        }))
    result = _wait_ready_workers(root, pairs, run_id=run_id,
        bootstrap_id=bootstrap_id, route_config_sha256=route_hash,
        marker_kinds={"occusg": "occusg_rooms"})
    assert result["workers"]["occusg"]["pid"] == 102
