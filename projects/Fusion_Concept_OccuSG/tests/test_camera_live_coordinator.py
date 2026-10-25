from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from integration_v2.camera_live import (
    CameraLiveError,
    CameraLiveCoordinator,
    CameraLiveReplayRequired,
    CurrentFramePacket,
)


class FakeSession:
    def __init__(self, *, fail=False, delay=0.0):
        self.frames = []
        self.finalized = False
        self.closed = False
        self.fail = fail
        self.delay = delay

    def update_frame(self, packet):
        if self.delay:
            time.sleep(self.delay)
        self.frames.append(packet["frame_id"])
        if self.fail:
            raise RuntimeError("worker failure")
        return {"frame_id": packet["frame_id"]}

    def finalize(self):
        self.finalized = True

    def close(self):
        self.closed = True


def packet(i):
    return CurrentFramePacket(f"f{i}", i, i * .1, i * .1, {"rgb": f"h{i}"},
                              {"rgb": b"current"}, camera_monotonic_s=time.monotonic(),
                              camera_monotonic_domain="coordinator_process")


def join(p):
    return ({"frame_id": p.frame_id}, {"frame_id": p.frame_id}, {"future_manifest_required": False})


def run(coord, count=3):
    coord.start()
    for i in range(count):
        assert coord.submit(packet(i)) == "admitted"
        deadline = time.monotonic() + 2.0
        while coord.metrics.processed < i + 1 and time.monotonic() < deadline:
            time.sleep(.001)
        assert coord.metrics.processed == i + 1
    coord.finish()


def test_pair_barrier_and_terminal_lifecycle():
    cg, occ, commits = FakeSession(), FakeSession(), []
    coord = CameraLiveCoordinator(perception_join=join, conceptgraphs_session=cg,
                                  occusg_session=occ,
                                  publish_revision=lambda p, a, b, m: commits.append(p.frame_id))
    run(coord)
    assert commits == ["f0", "f1", "f2"]
    assert cg.frames == occ.frames == commits
    assert cg.finalized and occ.finalized
    coord.close()
    assert cg.closed and occ.closed


def test_duplicate_and_future_packet_rejected_before_dispatch():
    cg, occ = FakeSession(), FakeSession()
    coord = CameraLiveCoordinator(perception_join=join, conceptgraphs_session=cg,
                                  occusg_session=occ, publish_revision=lambda *args: None)
    coord.start()
    assert coord.submit(packet(0)) == "admitted"
    assert coord.submit(packet(0)) == "superseded"
    with pytest.raises(ValueError, match="future"):
        coord.submit(CurrentFramePacket("future", 1, .1, .1, {"x": "h"}, {"future_packets": []}))
    coord.finish()
    coord.close()


def test_slow_branch_failure_stops_without_unified_commit():
    cg, occ, commits = FakeSession(), FakeSession(fail=True), []
    coord = CameraLiveCoordinator(perception_join=join, conceptgraphs_session=cg,
                                  occusg_session=occ,
                                  publish_revision=lambda *args: commits.append(1))
    coord.start()
    coord.submit(packet(0))
    with pytest.raises(CameraLiveReplayRequired):
        coord.finish()
    assert commits == []
    assert cg.frames == ["f0"]
    coord.close()


def test_perception_failure_is_not_claimed_as_mapper_mutation():
    cg, occ = FakeSession(), FakeSession()
    def fail_join(_):
        raise ValueError("bad current packet")
    coord = CameraLiveCoordinator(perception_join=fail_join, conceptgraphs_session=cg,
                                  occusg_session=occ, publish_revision=lambda *args: None)
    coord.start(); coord.submit(packet(0))
    with pytest.raises(CameraLiveError) as error:
        coord.finish()
    assert not coord.mutation_possible
    assert not cg.frames and not occ.frames
    coord.close()


def test_pending_latest_wins_is_logged():
    cg, occ = FakeSession(delay=.03), FakeSession(delay=.03)
    coord = CameraLiveCoordinator(perception_join=join, conceptgraphs_session=cg,
                                  occusg_session=occ, publish_revision=lambda *args: None)
    coord.start()
    coord.submit(packet(0)); coord.submit(packet(1)); coord.submit(packet(2))
    coord.finish()
    assert any(e.status == "superseded" and e.frame_id == "f1" for e in coord.events)
    coord.close()


def test_pair_dispatch_sends_both_branches_before_waiting():
    cg, occ, sent, commits = FakeSession(), FakeSession(), [], []
    def pair_dispatch(cg_packet, occ_packet):
        sent.extend([cg_packet["frame_id"], occ_packet["frame_id"]])
        return {"frame_id": cg_packet["frame_id"]}, {"frame_id": occ_packet["frame_id"]}
    coord = CameraLiveCoordinator(perception_join=join, conceptgraphs_session=cg,
                                  occusg_session=occ,
                                  mapper_pair_dispatch=pair_dispatch,
                                  publish_revision=lambda p, a, b, m: commits.append(p.frame_id))
    coord.start(); coord.submit(packet(0)); coord.finish(); coord.close()
    assert sent == ["f0", "f0"]
    assert commits == ["f0"]
    assert not cg.frames and not occ.frames


def test_file_backed_ipc_pair_dispatch_with_ack(tmp_path: Path):
    worker_code = """
import json, pathlib, sys
kind = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    packet = json.loads(pathlib.Path(msg['packet_path']).read_text())
    ack = pathlib.Path(msg['ack_path'])
    tmp = ack.with_suffix('.tmp')
    tmp.write_text(json.dumps({'kind': kind, 'frame_id': packet['frame_id'], 'source_hashes': packet['source_hashes']}))
    tmp.replace(ack)
"""
    procs = [subprocess.Popen([sys.executable, "-u", "-c", worker_code, kind],
                              stdin=subprocess.PIPE, text=True)
             for kind in ("objects", "rooms")]
    try:
        def pair_dispatch(cg_packet, occ_packet):
            frame_id = cg_packet["frame_id"]
            packet_path = tmp_path / f"packet-{frame_id}.json"
            packet_path.write_text(json.dumps({"frame_id": frame_id, "source_hashes": {"rgb": "h"}}))
            for proc, kind in zip(procs, ("objects", "rooms")):
                ack = tmp_path / f"ack-{kind}-{frame_id}.json"
                proc.stdin.write(json.dumps({"packet_path": str(packet_path), "ack_path": str(ack)}) + "\n")
                proc.stdin.flush()
            deadline = time.monotonic() + 2.0
            acks = []
            while len(acks) < 2 and time.monotonic() < deadline:
                acks = [json.loads(p.read_text()) for p in sorted(tmp_path.glob(f"ack-*-{frame_id}.json"))]
                time.sleep(.001)
            assert {a["kind"] for a in acks} == {"objects", "rooms"}
            return {"frame_id": frame_id}, {"frame_id": frame_id}

        cg, occ, commits = FakeSession(), FakeSession(), []
        coord = CameraLiveCoordinator(perception_join=join, conceptgraphs_session=cg,
                                      occusg_session=occ, mapper_pair_dispatch=pair_dispatch,
                                      publish_revision=lambda p, a, b, m: commits.append(p.frame_id))
        coord.start(); coord.submit(packet(0)); coord.finish(); coord.close()
        assert commits == ["f0"]
    finally:
        for proc in procs:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate(); proc.wait(timeout=3)
