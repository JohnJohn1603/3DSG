import json
import hashlib
import io
from dataclasses import replace

import numpy as np
import pytest
from PyQt5 import QtWidgets

from exploration.contracts import GridSnapshot, RobotPose2
from exploration.frontier import Frontier, FrontierSet
from exploration.habitat_online import RGBFrame
from exploration.desktop import LiveDashboardWidget
from exploration.live_dashboard import LiveStatePublisher
from exploration.policy import NavigationDecision


def _inputs():
    rgb = RGBFrame("run", "scene", "frame-1", 1, 0.1, 10.1,
                   np.full((2, 3, 3), 127, dtype=np.uint8))
    states = np.array([[1, 1, 0], [2, 1, 0]], dtype=np.uint8)
    snapshot = GridSnapshot(
        revision=2, as_of_frame_id="frame-1", as_of_frame_index=1,
        host_monotonic_s=10.1, resolution_m=0.05, origin_x_m=-0.05,
        origin_y_m=0.0, states=states, floor_support=np.zeros_like(states),
        clearance_evidence=np.zeros_like(states), spawn_validated=np.zeros_like(states),
        occupancy_log_odds=np.zeros_like(states, dtype=np.float32),
    )
    frontier = Frontier("frontier-a", 2, ((0, 1),), (0, 1), 1)
    frontiers = FrontierSet(2, "frame-1", (frontier,))
    decision = NavigationDecision("MOVE", frontier, None, "test", 10.0)
    return rgb, snapshot, frontiers, decision


def test_live_publisher_writes_camera_nav_and_graph_contract(tmp_path):
    publisher = LiveStatePublisher(tmp_path / "live", run_id="run")
    rgb, snapshot, frontiers, decision = _inputs()
    publisher.publish_tick(
        rgb=rgb, snapshot=snapshot, robot_pose=RobotPose2(0.0, 0.03, 0.2),
        frontiers=frontiers, decision=decision,
        command={"reason": "move", "linear_mps": 0.1},
        depth_m=np.ones((2, 3), dtype=np.float32),
        intrinsics={"fx": 2, "fy": 2, "cx": 1, "cy": 1, "width": 3, "height": 2},
        T_world_camera=np.eye(4), elapsed_s=1.2,
    )
    state = json.loads((tmp_path / "live/state.json").read_text())
    assert state["schema"] == "3dsg-live-state/v1"
    assert state["camera"]["frame_id"] == "frame-1"
    assert state["navigation"]["revision"] == 2
    assert state["navigation"]["frontiers"][0]["id"] == "frontier-a"
    assert state["generation_id"] == "run:frame-1:nav2"
    for field, filename in (("rgb_sha256", "rgb.npy"), ("depth_sha256", "depth_m.npy"),
                            ("states_sha256", "nav_states.npy")):
        section = state["camera"] if field != "states_sha256" else state["navigation"]
        assert section[field] == hashlib.sha256((tmp_path / "live" / filename).read_bytes()).hexdigest()
    assert np.load(tmp_path / "live/rgb.npy", allow_pickle=False).shape == (2, 3, 3)
    assert np.load(tmp_path / "live/nav_states.npy", allow_pickle=False).tolist() == [[1, 1, 0], [2, 1, 0]]

    publisher.publish_graph({
        "revision": 0, "as_of_frame_id": "frame-1", "backend": "fixture",
        "nodes": [
            {"id": "room-1", "kind": "room", "position": [0, 0, 0], "label": "room"},
            {"id": "object-1", "kind": "object", "position": [1, 0, 0], "label": "chair"},
        ],
        "edges": [{"source": "room-1", "target": "object-1", "relation": "contains"}],
    })
    graph = json.loads((tmp_path / "live/graph.json").read_text())
    assert graph["revision"] == 0
    assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1


def test_live_publisher_rejects_dangling_or_rewound_graph(tmp_path):
    publisher = LiveStatePublisher(tmp_path / "live", run_id="run")
    with pytest.raises(ValueError, match="absent node"):
        publisher.publish_graph({
            "revision": 1, "as_of_frame_id": "frame-1",
            "nodes": [{"id": "a", "kind": "object", "position": [0, 0, 0]}],
            "edges": [{"source": "a", "target": "missing", "relation": "near"}],
        })


def test_live_publisher_reports_smoothed_capture_rate(tmp_path):
    publisher = LiveStatePublisher(tmp_path / "live", run_id="run")
    rgb, snapshot, frontiers, decision = _inputs()
    options = {
        "robot_pose": RobotPose2(0.0, 0.03, 0.2), "frontiers": frontiers,
        "decision": decision, "command": {"reason": "move", "linear_mps": 0.1},
        "depth_m": np.ones((2, 3), dtype=np.float32),
        "intrinsics": {"fx": 2, "fy": 2, "cx": 1, "cy": 1,
                       "width": 3, "height": 2},
        "T_world_camera": np.eye(4), "elapsed_s": 1.4,
    }
    publisher.publish_tick(rgb=rgb, snapshot=snapshot, **options)
    second_rgb = replace(rgb, frame_id="frame-2", frame_index=2, host_monotonic_s=10.3)
    second_snapshot = replace(snapshot, revision=3, as_of_frame_id="frame-2",
                              as_of_frame_index=2, host_monotonic_s=10.3)
    publisher.publish_tick(rgb=second_rgb, snapshot=second_snapshot, **options)
    state = json.loads((tmp_path / "live/state.json").read_text())
    assert state["capture_fps"] == 5.0


def test_live_viewer_keeps_previous_generation_when_one_array_hash_mismatches(tmp_path):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    live_root = tmp_path / "live"
    publisher = LiveStatePublisher(live_root, run_id="run")
    rgb, snapshot, frontiers, decision = _inputs()
    options = {
        "robot_pose": RobotPose2(0.0, 0.03, 0.2), "frontiers": frontiers,
        "decision": decision, "command": {"reason": "move", "linear_mps": 0.1},
        "depth_m": np.ones((2, 3), dtype=np.float32),
        "intrinsics": {"fx": 2, "fy": 2, "cx": 1, "cy": 1,
                       "width": 3, "height": 2},
        "T_world_camera": np.eye(4), "elapsed_s": 1.0,
    }
    publisher.publish_tick(rgb=rgb, snapshot=snapshot, **options)
    viewer = LiveDashboardWidget(live_root)
    viewer.timer.stop()
    try:
        viewer.refresh()
        old_rgb = viewer._rgb.copy()
        old_depth = viewer._depth.copy()
        old_map = viewer._map.copy()
        assert viewer._state["camera"]["frame_id"] == "frame-1"

        next_rgb = replace(
            rgb, frame_id="frame-2", frame_index=2, host_monotonic_s=10.3,
            rgb=np.full((2, 3, 3), 42, dtype=np.uint8),
        )
        next_snapshot = replace(
            snapshot, revision=3, as_of_frame_id="frame-2", as_of_frame_index=2,
            host_monotonic_s=10.3, states=np.full((2, 3), 2, dtype=np.uint8),
        )
        publisher.publish_tick(rgb=next_rgb, snapshot=next_snapshot, **options)
        with (live_root / "depth_m.npy").open("wb") as stream:
            np.save(stream, np.full((2, 3), 3.0, dtype=np.float32), allow_pickle=False)

        viewer.refresh()

        assert viewer._live_snapshot_mismatch
        assert viewer._state["camera"]["frame_id"] == "frame-1"
        np.testing.assert_array_equal(viewer._rgb, old_rgb)
        np.testing.assert_array_equal(viewer._depth, old_depth)
        np.testing.assert_array_equal(viewer._map, old_map)
        assert viewer.scene_panel.frame_id == "frame-1"
    finally:
        viewer.close()
        app.processEvents()
