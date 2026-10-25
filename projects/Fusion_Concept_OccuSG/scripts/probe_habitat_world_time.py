#!/usr/bin/env python3
"""One bounded, no-persistence Habitat world-time probe for a locked route.

The probe replays only the already-recorded action prefix in Habitat-Sim and
records scalar clocks/poses. Habitat's normal ``sim.step`` internally renders
configured sensors; returned RGB-D arrays are discarded in memory and never
written. No perception or mapper process is started.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration_v2.camera_pacing import SimulationClockPacer  # noqa: E402
from integration_v2.camera_live_contract import host_monotonic_domain  # noqa: E402
from scripts import run_habitat_smooth_route_preview as route_runner  # noqa: E402


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def run(config_path: Path, source_run: Path, output: Path, *, max_steps: int, timeout_s: float) -> dict:
    if max_steps < 1 or max_steps > 400:
        raise ValueError("probe max_steps must be in [1, 400]")
    if timeout_s <= 0 or timeout_s > 120:
        raise ValueError("probe timeout must be in (0, 120] seconds")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    source_events = source_run / "events.jsonl"
    source_manifest = source_run / "camera_producer_manifest.json"
    if not source_events.is_file() or not source_manifest.is_file():
        raise FileNotFoundError("locked failed-run action journal/manifest is missing")
    event_rows = [json.loads(line) for line in source_events.read_text(encoding="utf-8").splitlines() if line.strip()]
    camera_rows = sorted((row for row in event_rows if row.get("event") == "camera_frame"),
                         key=lambda row: int(row["frame_index"]))
    if len(camera_rows) < 2 or [int(row["frame_index"]) for row in camera_rows] != list(range(len(camera_rows))):
        raise ValueError("failed-run camera frame journal is not a complete zero-based prefix")
    actions = [str(row["action"]) for row in camera_rows[1:]]
    if any(action not in {"move_forward", "turn_left", "turn_right"} for action in actions):
        raise ValueError("locked camera journal contains an unsupported action")
    if len(actions) + 1 > max_steps:
        raise ValueError(f"replaying source prefix plus next action needs {len(actions)+1} steps; cap={max_steps}")

    import habitat_sim
    import magnum as mn
    import quaternion  # noqa: F401
    from habitat_sim.utils.common import quat_from_angle_axis

    policy = config["policy"]
    dt_s = float(policy["simulator_step_s"])
    linear_speed = float(policy["linear_speed_mps"])
    angular_speed = float(policy["angular_speed_deg_per_s"])
    move_m, turn_deg = route_runner.motion_increment_for_step(linear_speed, angular_speed, dt_s)
    width, height = (int(value) for value in config["resolution_px"])
    if width != 640 or height != 480:
        raise ValueError("timebase probe must use the locked 640x480 sensors")

    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_id = str(config["scene_asset"])
    sim_cfg.create_renderer = True
    sim_cfg.requires_textures = True
    sim_cfg.gpu_device_id = -1
    sim_cfg.enable_physics = False
    sim_cfg.allow_sliding = False
    sim_cfg.random_seed = int(config["seed"])
    if config.get("scene_dataset_config"):
        sim_cfg.scene_dataset_config_file = str(config["scene_dataset_config"])

    agent_cfg = habitat_sim.AgentConfiguration()
    agent_cfg.radius = float(policy["agent_footprint"]["radius_m"])
    agent_cfg.height = float(policy["agent_footprint"]["height_m"])
    agent_cfg.action_space = {
        "move_forward": habitat_sim.ActionSpec("move_forward", habitat_sim.ActuationSpec(amount=move_m)),
        "turn_left": habitat_sim.ActionSpec("turn_left", habitat_sim.ActuationSpec(amount=turn_deg)),
        "turn_right": habitat_sim.ActionSpec("turn_right", habitat_sim.ActuationSpec(amount=turn_deg)),
    }
    sensor_position = mn.Vector3(*[float(v) for v in config["sensor_position_habitat"]])
    sensor_orientation = mn.Vector3(*[float(v) for v in config["sensor_orientation_habitat_rad"]])
    sensors = []
    for sensor_type, uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                              (habitat_sim.SensorType.DEPTH, "depth_sensor")):
        spec = habitat_sim.CameraSensorSpec()
        spec.uuid = uuid
        spec.sensor_type = sensor_type
        spec.resolution = mn.Vector2i(height, width)
        spec.position = sensor_position
        spec.orientation = sensor_orientation
        spec.hfov = mn.Deg(float(config["horizontal_fov_deg"]))
        spec.near = float(config["min_depth_m"])
        spec.far = float(config["max_depth_m"])
        if sensor_type == habitat_sim.SensorType.DEPTH and hasattr(spec, "normalize_depth"):
            spec.normalize_depth = False
        sensors.append(spec)
    agent_cfg.sensor_specifications = sensors

    manifest = {
        "schema": "habitat-world-time-probe/v1", "status": "started",
        "source_run_id": json.loads(source_manifest.read_text(encoding="utf-8"))["run_id"],
        "scene_id": config["scene_id"], "sequence_id": config["sequence_id"],
        "route_id": config["route_id"], "requested_dt_s": dt_s,
        "action_prefix_frames": len(camera_rows), "action_calls_from_prefix": len(actions),
        "step_cap": max_steps, "timeout_s": timeout_s, "frames_written": 0,
        "rgbd_written": False, "perception_started": False, "mapping_started": False,
        "native_room_worker_started": False,
        "rendering": "Habitat sim.step returns configured RGB/depth observations in memory; arrays discarded",
        "code_provenance": {"probe_sha256": sha(Path(__file__).resolve()),
            "route_runner_sha256": sha(ROOT / "scripts/run_habitat_smooth_route_preview.py"),
            "camera_pacing_sha256": sha(ROOT / "integration_v2/camera_pacing.py"),
            "effective_config_sha256": sha(config_path),
            "source_events_sha256": sha(source_events), "source_manifest_sha256": sha(source_manifest),
            "scene_sha256": sha(Path(config["scene_asset"])),
            "navmesh_sha256": sha(Path(config["navmesh_file"])),
            "scene_dataset_config_sha256": sha(Path(config["scene_dataset_config"])) if config.get("scene_dataset_config") else None,
            "habitat_sim": str(getattr(habitat_sim, "__version__", "unknown")),
            "python": sys.version.split()[0], "platform": platform.platform()},
        "simulator_config": {"enable_physics": False, "allow_sliding": False,
            "renderer": True, "sensors": ["color_sensor", "depth_sensor"],
            "resolution_px": [width, height], "seed": int(config["seed"])},
    }
    write_json(output / "probe_manifest.json", manifest)
    step_path = output / "steps.jsonl"
    started = time.monotonic()
    wall_domain = host_monotonic_domain()
    sim = None
    rows = []
    failure = None
    try:
        sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
        if not sim.pathfinder.load_nav_mesh(str(config["navmesh_file"])):
            raise RuntimeError("Habitat failed to load the locked navmesh")
        agent = sim.initialize_agent(0)
        first = config["route_points"][0]
        initial = agent.get_state()
        initial.position = np.asarray(first["position_habitat"], dtype=np.float32)
        initial.rotation = quat_from_angle_axis(float(first["yaw_rad"]), np.array([0.0, 1.0, 0.0], dtype=np.float32))
        agent.set_state(initial, infer_sensor_states=True)
        initial_obs = sim.get_sensor_observations()
        initial_shapes = {key: list(np.asarray(initial_obs[key]).shape) for key in ("color_sensor", "depth_sensor")}
        del initial_obs
        pacer = SimulationClockPacer(float(policy["camera_pacing"]["real_time_factor"]))
        sim_before = float(sim.get_world_time())
        pacer.start(sim_before, time.monotonic())

        def step(action: str, source_frame_index: int | None, phase: str) -> dict:
            if len(rows) >= max_steps:
                raise RuntimeError("probe reached the locked simulation-step cap")
            if time.monotonic() - started >= timeout_s:
                raise TimeoutError("probe reached the locked wall timeout")
            before = float(sim.get_world_time())
            wall_start = time.time()
            mono_start = time.monotonic()
            observations = sim.step(action, dt=dt_s)
            after = float(sim.get_world_time())
            mono_end = time.monotonic()
            result = {"step_index": len(rows), "action": action, "phase": phase,
                "source_frame_index": source_frame_index, "requested_dt_s": dt_s,
                "world_time_before_s": before, "world_time_after_s": after,
                "observed_delta_s": after - before,
                "capture_wall_start_unix_s": wall_start,
                "capture_monotonic_start_s": mono_start,
                "capture_monotonic_end_s": mono_end,
                "capture_monotonic_domain": wall_domain,
                "step_wall_elapsed_s": mono_end - mono_start,
                "observation_keys": sorted(observations),
                "color_shape": list(np.asarray(observations["color_sensor"]).shape),
                "depth_shape": list(np.asarray(observations["depth_sensor"]).shape)}
            del observations
            rows.append(result)
            with step_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
                stream.flush()
            return result

        for index, action in enumerate(actions, start=1):
            if len(rows) >= max_steps:
                break
            step(action, index, "replayed_locked_action_prefix")
            pacer.pace_after_frame(float(sim.get_world_time()), continuing=True)

        source_last = camera_rows[-1]
        state = agent.get_state()
        state_pos = route_runner._position(state)
        source_pos = np.asarray(source_last["base_position_habitat"], dtype=np.float64)
        final_yaw = route_runner.wrap_angle_rad(float(config["route_points"][-1]["yaw_rad"]))
        current_yaw = route_runner._yaw(route_runner._rotation_matrix(state.rotation, quaternion))
        yaw_delta = route_runner.yaw_delta_rad(current_yaw, final_yaw)
        tolerance = math.radians(float(policy["route_follow"]["yaw_tolerance_deg"]))
        if abs(yaw_delta) <= tolerance:
            next_action = None
        else:
            next_action = "turn_left" if yaw_delta > 0.0 else "turn_right"
        manifest["replay_prefix_check"] = {
            "source_last_frame_index": int(source_last["frame_index"]),
            "source_last_timestamp_s": float(source_last["timestamp_s"]),
            "probe_world_time_after_prefix_s": float(sim.get_world_time()),
            "world_time_difference_s": float(sim.get_world_time()) - float(source_last["timestamp_s"]),
            "probe_position_habitat": list(map(float, state_pos)),
            "source_position_habitat": list(map(float, source_pos)),
            "position_difference_m": float(np.linalg.norm(np.asarray(state_pos) - source_pos)),
            "current_yaw_rad": float(current_yaw), "target_yaw_rad": float(final_yaw),
            "next_route_action": next_action, "yaw_tolerance_rad": tolerance,
            "initial_sensor_observation_shapes": initial_shapes,
        }
        if next_action is not None and len(rows) < max_steps:
            # This is the exact next action selected by the runner's final-yaw rule.
            step(next_action, None, "next_action_at_failed_step_boundary")
        manifest["status"] = "completed"
        manifest["step_count"] = len(rows)
        manifest["first_non_requested_delta"] = next((row for row in rows
            if not math.isclose(row["observed_delta_s"], dt_s, rel_tol=0.0, abs_tol=1e-6)), None)
        manifest["sim_step_world_time_doc"] = str(habitat_sim.Simulator.step_world.__doc__)
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
        manifest["status"] = "failed"
        manifest["error"] = failure
        manifest["step_count"] = len(rows)
    finally:
        if sim is not None:
            try:
                sim.close()
                manifest["simulator_closed"] = True
            except BaseException as exc:
                manifest["simulator_closed"] = False
                manifest.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")
        manifest["elapsed_wall_s"] = time.monotonic() - started
        manifest["wall_clock_domain"] = wall_domain
        write_json(output / "probe_manifest.json", manifest)
    if failure:
        raise RuntimeError(failure)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=400)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    args = parser.parse_args()
    result = run(args.config.resolve(), args.source_run.resolve(), args.output,
                 max_steps=args.max_steps, timeout_s=args.timeout_s)
    print(json.dumps({"status": result["status"], "step_count": result["step_count"],
                      "first_non_requested_delta": result["first_non_requested_delta"],
                      "elapsed_wall_s": result["elapsed_wall_s"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
