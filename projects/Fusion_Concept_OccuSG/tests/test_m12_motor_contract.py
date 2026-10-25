import json
import math
import queue
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from m12_motor_geometry import (
    capture_due,
    collision_report,
    enqueue_bounded_or_raise,
    slew_command,
    timeout_reached,
    validate_collision_config,
)


def _config(name):
    return json.loads((ROOT / "configs" / "webots" / name).read_text(encoding="utf-8"))


def test_motor_collision_geometry_matches_authored_world_extents():
    corridor = _config("m12_corridor_motor_route.json")
    assert corridor["collision"]["wall_aabbs_xy"] == [
        {"name": "left_wall", "min": [-3.5, -1.56], "max": [3.5, -1.44]},
        {"name": "right_wall", "min": [-3.5, 1.44], "max": [3.5, 1.56]},
    ]
    assert {box["name"] for box in corridor["collision"]["obstacle_aabbs_xy"]} == {
        "orange_obstacle", "blue_obstacle"
    }

    two_room = _config("m12_two_room_motor_route.json")
    assert two_room["collision"]["wall_aabbs_xy"][0] == {
        "name": "outer_left", "min": [-0.06, -2.56], "max": [0.06, 2.44]
    }
    assert len(two_room["collision"]["obstacle_aabbs_xy"]) == 2


def test_collision_report_distinguishes_clear_contact_and_penetration():
    collision = {
        "robot_radius_m": 0.3,
        "contact_tolerance_m": 1e-4,
        "wall_aabbs_xy": [{"name": "wall", "min": [0.0, 0.0], "max": [1.0, 1.0]}],
        "obstacle_aabbs_xy": [{"name": "obstacle", "min": [3.0, 0.0], "max": [3.5, 0.5]}],
    }
    assert collision_report(-1.0, 0.5, collision)["state"] == "clear"
    assert collision_report(-0.3, 0.5, collision)["state"] == "contact"
    report = collision_report(0.1, 0.5, collision)
    assert report["state"] == "penetration"
    assert "wall" in report["penetration_names"]
    assert "obstacle" not in report["penetration_names"]


def test_motor_contract_rejects_bad_aabb_and_limits_wheel_acceleration():
    with pytest.raises(ValueError):
        validate_collision_config({"robot_radius_m": 0.3, "wall_aabbs_xy": []})
    assert slew_command(0.0, 10.0, 4.0, 0.5) == pytest.approx(2.0)
    assert slew_command(3.0, -10.0, 4.0, 0.5) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        slew_command(0.0, math.inf, 4.0, 0.5)


def test_timeout_stuck_and_capture_cadence_contracts_are_distinct():
    assert timeout_reached(4.1, 0.0, 4.0)
    assert not timeout_reached(4.0, 0.0, 4.0)
    assert capture_due(40, 40)
    assert not capture_due(39, 40)
    assert not capture_due(41, 40)
    assert capture_due(120, 40)


def test_full_live_queue_fails_explicitly_without_dropping():
    packet_queue = queue.Queue(maxsize=1)
    packet_queue.put("first")
    with pytest.raises(RuntimeError, match="queue full"):
        enqueue_bounded_or_raise(packet_queue, "second", 0.001)


def test_motor_controller_records_physics_trace_and_does_not_teleport():
    source = (ROOT / "webots/controllers/m12_motor_capture/m12_motor_capture.py").read_text(encoding="utf-8")
    assert "physics_trace.json" in source
    assert "collision_report(" in source
    assert "setVelocity(" in source
    assert "setSFVec3f" not in source
    assert "setSFRotation" not in source
    assert "capture_every_physics_steps" in source


def test_wheel_cylinders_are_rotated_to_match_hinge_axis():
    source = (ROOT / "webots/protos/M12DiffDriveRGBD.proto").read_text(encoding="utf-8")
    assert source.count("HingeJointParameters { axis 0 1 0") == 2
    # One visual cylinder and one collision cylinder per wheel.
    assert source.count("rotation 1 0 0 -1.57079632679") == 4
    assert source.count("Cylinder { radius 0.18 height 0.07 }") == 4


def test_live_bridge_has_bounded_blocking_queue_without_silent_drop():
    source = (ROOT / "scripts/run_m12_webots_live_bridge.py").read_text(encoding="utf-8")
    assert "queue.Queue(maxsize=" in source
    assert "enqueue_bounded_or_raise(packet_queue" in source
    assert "silent_drop" in source
    assert "block_sender_then_fail_explicitly" in source


def test_motor_done_marker_has_drain_deadline_separate_from_frame_send_timeout():
    source = (ROOT / "webots/controllers/m12_motor_capture/m12_motor_capture.py").read_text(encoding="utf-8")
    assert "def _send_done_marker" in source
    assert "connection.settimeout(float(timeout_s))" in source
    assert "_send_done_marker(live_socket, manifest)" in source


def test_live_bridge_config_overrides_world_custom_data_without_mutating_world():
    controller = (ROOT / "webots/controllers/m12_motor_capture/m12_motor_capture.py").read_text(encoding="utf-8")
    bridge = (ROOT / "scripts/run_m12_webots_live_bridge.py").read_text(encoding="utf-8")
    assert 'os.environ.get("M12_CONFIG_PATH")' in controller
    assert 'env["M12_CONFIG_PATH"]' in bridge
    assert 'env["M12_CONFIG_PATH"] = str(config_path.relative_to(ROOT))' in bridge
