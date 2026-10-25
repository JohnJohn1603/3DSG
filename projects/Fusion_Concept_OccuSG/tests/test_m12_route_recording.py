import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webots" / "controllers" / "m12_motor_capture"))

from route_recording import (  # noqa: E402
    append_waypoint,
    key_codes,
    manual_command,
    validate_settings,
    waypoint_from_pose,
    write_recorded_config,
)


def _pose(x=1.0, y=2.0, z=0.0, yaw=0.0):
    # Row-major Webots pose with planar yaw.
    import math
    return [
        math.cos(yaw), -math.sin(yaw), 0.0, x,
        math.sin(yaw), math.cos(yaw), 0.0, y,
        0.0, 0.0, 1.0, z,
        0.0, 0.0, 0.0, 1.0,
    ]


def test_route_recording_settings_are_strict_and_project_local(tmp_path):
    settings = validate_settings({"enabled": True, "output_path": "artifacts/routes/a.json"}, tmp_path)
    assert settings["output_path"] == "artifacts/routes/a.json"
    assert settings["mark_key"] == "SPACE"
    with pytest.raises(ValueError, match="project-relative"):
        validate_settings({"enabled": True, "output_path": "/tmp/a.json"}, tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        validate_settings({"enabled": True, "output_path": "../a.json"}, tmp_path)
    with pytest.raises(ValueError, match="finite"):
        validate_settings({"enabled": True, "max_duration_s": float("inf")}, tmp_path)


def test_keyboard_command_is_stop_when_focus_is_lost():
    command = manual_command(-1, up=315, down=317, left=314, right=316,
                             linear_speed=0.2, angular_speed=0.8)
    assert command == (0.0, 0.0, "stop")
    assert manual_command(ord("w"), up=315, down=317, left=314, right=316,
                          linear_speed=0.2, angular_speed=0.8) == (0.2, 0.0, "forward")
    assert manual_command(314, up=315, down=317, left=314, right=316,
                          linear_speed=0.2, angular_speed=0.8) == (0.0, 0.8, "turn_left")
    assert key_codes("Q") == {ord("Q"), ord("q")}
    assert key_codes("SPACE") == {ord(" ")}


def test_waypoint_uses_actual_pose_and_rejects_near_duplicate():
    route = []
    first = waypoint_from_pose(_pose(1.0, 2.0, yaw=0.5), 3.25, 0, "start")
    second = waypoint_from_pose(_pose(1.01, 2.0, yaw=0.6), 3.5, 1)
    assert append_waypoint(route, first, 0.05)
    assert not append_waypoint(route, second, 0.05)
    assert route[0]["translation"] == [1.0, 2.0, 0.0]
    assert route[0]["yaw_rad"] == pytest.approx(0.5)
    assert route[0]["recorded_simulation_timestamp_s"] == pytest.approx(3.25)


def test_recorded_config_is_complete_and_atomic(tmp_path):
    settings = validate_settings({"enabled": True, "output_path": "routes/recorded.json"}, tmp_path)
    source = {
        "schema_version": 1, "world_id": "fixture", "motion_mode": "motor",
        "route_type": "keyboard_motor_route_recording", "route": [],
    }
    route = [waypoint_from_pose(_pose(0.0, 0.0), 0.0, 0, "start")]
    path = write_recorded_config(tmp_path, source, route, settings,
                                 simulation_duration_s=2.0,
                                 initial_pose=_pose(), final_pose=_pose(0.1, 0.0))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["route"] == route
    assert payload["route_recording"]["enabled"] is False
    assert payload["route_type"] == "motor_waypoint_recorded_supervisor_feedback"
    assert payload["route_recording_provenance"]["pose_source"] == "Supervisor.getPose"
    with pytest.raises(FileExistsError):
        write_recorded_config(tmp_path, source, route, settings,
                              simulation_duration_s=2.0,
                              initial_pose=_pose(), final_pose=_pose())
