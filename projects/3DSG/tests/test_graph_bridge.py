from dataclasses import dataclass
import threading
import time

import numpy as np

from exploration.contracts import CameraIntrinsics, FramePacket, Pose3
from exploration.graph_bridge import GraphIntegrationWorker
from exploration.habitat_online import RGBFrame
from exploration.keyframe_scheduler import KeyframeScheduler
from exploration.keyframe_store import KeyframeStore
from exploration.scene_graph_provider import GraphProviderCapabilities


def _persist(store, index):
    frame_id = f"run:{index:08d}"
    intr = CameraIntrinsics(2, 2, 1, 1, 2, 2)
    packet = FramePacket(
        "run", "scene", frame_id, index, index * 0.1, 100.0 + index,
        np.full((2, 2), 2.0, dtype=np.float32), intr,
        Pose3(np.eye(4), frame_id), 0.0, "calibration", 0.1, 5.0,
        nav_depth_m=np.full((1, 2), 1.5, dtype=np.float32),
        nav_depth_intrinsics=CameraIntrinsics(2, 2, 0.5, 0, 2, 1),
        T_world_nav_depth_camera=Pose3(np.eye(4), frame_id),
        nav_depth_min_m=0.1, nav_depth_max_m=4.0,
    )
    rgb = RGBFrame("run", "scene", frame_id, index, index * 0.1,
                   100.0 + index, np.zeros((2, 2, 3), dtype=np.uint8))
    return store.persist(rgb, packet, novelty=float(index + 1),
                         protected_reason="first_view" if index == 0 else None)


def test_keyframe_roundtrip_preserves_secondary_navigation_depth(tmp_path):
    store = KeyframeStore(tmp_path / "run")
    ref = _persist(store, 0)
    _rgb, packet = store.load(ref)
    assert packet.nav_depth_m.shape == (1, 2)
    assert np.allclose(packet.nav_depth_m, 1.5)
    assert packet.nav_depth_intrinsics.width == 2
    assert packet.nav_depth_min_m == 0.1
    assert np.allclose(packet.T_world_nav_depth_camera.matrix, np.eye(4))


class _Session:
    def __init__(self):
        self.seen = []

    def update_frame(self, packet):
        frame_id = packet["frame_id"] if isinstance(packet, dict) else packet.frame_id
        self.seen.append(frame_id)
        return frame_id


@dataclass
class _OccPacket:
    scene_id: str
    frame_id: str
    frame_index: int


def test_graph_worker_loads_durable_keyframes_and_commits_pair(tmp_path):
    store = KeyframeStore(tmp_path / "run")
    scheduler = KeyframeScheduler(max_pending=2, raw_pending_byte_cap=4096)
    refs = [_persist(store, i) for i in range(2)]
    for ref in refs:
        assert scheduler.offer(ref).accepted
    cg, occ, published, order = _Session(), _Session(), [], []

    def cg_packet(obs):
        assert obs.perception_result["p2"] == obs.keyframe.frame_id
        return {"run_id": obs.keyframe.run_id, "scene_id": obs.keyframe.scene_id,
                "frame_id": obs.keyframe.frame_id, "frame_index": obs.keyframe.frame_index}

    def occ_packet(obs):
        assert obs.perception_result["p3"] == obs.keyframe.frame_id
        return _OccPacket(obs.keyframe.scene_id, obs.keyframe.frame_id,
                          obs.keyframe.frame_index)

    worker = GraphIntegrationWorker(
        scheduler=scheduler, store=store,
        conceptgraphs_session=cg, occusg_session=occ,
        conceptgraphs_packet=cg_packet,
        occusg_packet=occ_packet,
        publish=published.append,
        perceive=lambda obs: order.append(("perception", obs.keyframe.frame_id)) or {
            "p2": obs.keyframe.frame_id, "p3": obs.keyframe.frame_id,
        },
    )
    worker.start()
    worker.close(timeout_s=2)

    expected = [ref.frame_id for ref in refs]
    assert worker.failure is None, repr(worker.failure.__cause__)
    assert cg.seen == expected
    assert occ.seen == expected
    assert [commit.frame_id for commit in published] == expected
    assert [commit.dispatch_sequence for commit in published] == [1, 2]
    assert scheduler.snapshot()["counts"]["processed"] == 2
    assert worker.commit_status == {"count": 2, "last_frame_id": expected[-1]}
    assert [frame_id for stage, frame_id in order if stage == "perception"] == expected


def test_graph_worker_stops_and_does_not_publish_partial_pair(tmp_path):
    store = KeyframeStore(tmp_path / "run")
    scheduler = KeyframeScheduler(raw_pending_byte_cap=4096)
    ref = _persist(store, 0)
    assert scheduler.offer(ref).accepted
    cg, published = _Session(), []

    class _FailingOcc:
        def update_frame(self, packet):
            raise RuntimeError("native stage failed")

    worker = GraphIntegrationWorker(
        scheduler=scheduler, store=store,
        conceptgraphs_session=cg, occusg_session=_FailingOcc(),
        conceptgraphs_packet=lambda obs: {
            "frame_id": obs.keyframe.frame_id,
            "frame_index": obs.keyframe.frame_index,
        },
        occusg_packet=lambda obs: _OccPacket(
            obs.keyframe.scene_id, obs.keyframe.frame_id, obs.keyframe.frame_index),
        publish=published.append,
    )
    worker.start()
    deadline = time.monotonic() + 2
    while scheduler.snapshot()["status"] != "failed" and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.close(timeout_s=2)

    assert worker.failure is not None
    assert published == []
    assert scheduler.snapshot()["status"] == "failed"


def test_graph_worker_abort_receives_shared_absolute_shutdown_deadline(tmp_path):
    store = KeyframeStore(tmp_path / "run")
    scheduler = KeyframeScheduler(raw_pending_byte_cap=4096)
    ref = _persist(store, 0)
    assert scheduler.offer(ref).accepted
    entered = threading.Event()
    release = threading.Event()

    class _Provider:
        provider_id = "test-provider"
        capabilities = GraphProviderCapabilities(provider_id=provider_id)

        def update(self, observation):
            entered.set()
            release.wait(2.0)
            return None

        def abort(self, reason, *, deadline_monotonic_s=None):
            self.abort_call = (reason, deadline_monotonic_s)
            release.set()

    provider = _Provider()
    worker = GraphIntegrationWorker(
        scheduler=scheduler, store=store, provider=provider, publish=lambda _commit: None,
    )
    worker.start()
    assert entered.wait(1.0)
    deadline = time.monotonic() + 0.05
    worker.close(deadline_monotonic_s=deadline, drain_pending=False)
    assert provider.abort_call == ("graph_worker_shutdown_timeout", deadline)
    assert worker._done.wait(1.0)
