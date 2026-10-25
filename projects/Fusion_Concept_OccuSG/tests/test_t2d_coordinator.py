from pathlib import Path

import json

from integration_v2.t2d_coordinator import (
    ROOT, _branch_pair_valid, _branch_update_markers, _stop_worker,
    _worker_command, _write_packet_bundle,
)
from scripts.run_t2_d_cached_packet_worker import _occ_packet


SOURCE = ROOT / "artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2"
PACKETS = ROOT / "artifacts/t2_b2_perception_smoke_20261018_v3/packet_join_manifest.json"


def test_t2d_packet_bundle_validates_three_locked_packets(tmp_path):
    paths = _write_packet_bundle(tmp_path, SOURCE, PACKETS)
    assert len(paths) == 3
    assert [p.name for p in paths] == ["packet-000000.json", "packet-000001.json", "packet-000002.json"]
    manifest = (tmp_path / "packets/manifest.json").read_text()
    assert "t2-d-packet-bundle/v1" in manifest


def test_t2d_workers_use_locked_existing_interpreters():
    cg = _worker_command("conceptgraphs", ROOT / "artifacts/live_processing_runs/test", ROOT / "packets", SOURCE)
    occ = _worker_command("occusg", ROOT / "artifacts/live_processing_runs/test", ROOT / "packets", SOURCE)
    c2 = __import__("json").loads((SOURCE / "c2_run_manifest.json").read_text())
    assert cg[0] == c2["stages"]["conceptgraphs_mapping"]["command"][0]
    assert occ[0] == c2["stages"]["occusg_rooms"]["command"][0]
    assert cg[0] != occ[0]


def test_t2d_occ_packet_declares_world_detection_and_rgbd_contract(tmp_path):
    paths = _write_packet_bundle(tmp_path, SOURCE, PACKETS)
    packet = json.loads(paths[0].read_text())
    frame_packet = _occ_packet(packet, SOURCE)
    assert all(d.frame_id == "world" for d in frame_packet.detections.detections)
    assert frame_packet.source_metadata["depth_units"] == "m"
    assert frame_packet.source_metadata["depth_representation"] == "axial"


def _event(index, fid="f0", hashes=None):
    return {"source_frame_index": index, "source_frame_id": fid,
            "source_hashes": hashes or {"rgb.npy": "a"}}


def test_fake_worker_barrier_never_commits_slow_or_failed_branch():
    obj = [_event(0)]
    assert _branch_pair_valid(obj, [], 1) is False
    assert _branch_pair_valid(obj, [_event(1)], 1) is False
    assert _branch_pair_valid(obj, [_event(0, hashes={"rgb.npy": "b"})], 1) is False
    assert _branch_pair_valid(obj, [_event(0)], 1) is True


def test_update_marker_makes_uncertain_mutation_replayable(tmp_path):
    branch = tmp_path / "branches/objects/updates"
    branch.mkdir(parents=True)
    (branch / "frame-000000.json").write_text(json.dumps({
        "status": "update_started", "mutation_possible": True,
        "source_frame_index": 0,
    }))
    markers = _branch_update_markers(tmp_path, "objects")
    assert len(markers) == 1
    assert markers[0]["mutation_possible"] is True


class _FakeStdin:
    def __init__(self): self.lines = []
    def write(self, value): self.lines.append(value)
    def flush(self): pass
    def close(self): pass


class _FakeWorker:
    def __init__(self):
        self.stdin = _FakeStdin(); self.returncode = None; self.terminated = False
    def poll(self): return self.returncode
    def wait(self, timeout=None): self.returncode = 0; return 0
    def terminate(self): self.terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9


def test_fake_worker_cleanup_is_idempotent_after_cancellation():
    worker = _FakeWorker()
    _stop_worker(worker)
    _stop_worker(worker)
    assert worker.returncode == 0
    assert worker.stdin.lines[0].strip().startswith('{"op": "close"')
