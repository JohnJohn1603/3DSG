#!/usr/bin/env python3
"""Independent geometry check for one Habitat-Sim raw capture.

The expected depth is obtained from Habitat-Sim's physics ray cast, not from
the project's backprojection.  It checks center/off-axis pixels, the
Habitat-Y-up to canonical-Z-up transform, and actual sensor poses over a
translated/rotated route.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.habitat.raw import canonical_from_habitat_matrix  # noqa: E402


def _run(capture: Path, config_path: Path, output: Path) -> dict:
    import habitat_sim  # type: ignore
    import magnum as mn  # type: ignore
    import quaternion  # type: ignore
    from habitat_sim.utils.common import quat_from_angle_axis  # type: ignore

    manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    scene = Path(manifest["scene_file"])
    width, height = map(int, manifest["resolution_px"])
    hfov_deg = float(manifest["horizontal_fov_deg"])
    min_depth, max_depth = float(config["min_depth_m"]), float(config["max_depth_m"])
    fx = width / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))
    fy = height / (2.0 * math.tan(math.atan(math.tan(math.radians(hfov_deg) / 2.0) * height / width)))
    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_id = str(scene)
    sim_cfg.create_renderer = True
    # Match capture_habitat_replica.py.  Enabling physics here would change the
    # state of dynamic object declarations in a full scene and is not an
    # independent sensor/depth oracle for the captured frame.
    sim_cfg.enable_physics = bool(config.get("reference_enable_physics", False))
    scene_dataset_config = manifest.get("scene_dataset_config")
    if scene_dataset_config:
        sim_cfg.scene_dataset_config_file = str(scene_dataset_config)
    sim_cfg.gpu_device_id = int(config.get("gpu_device_id", -1))
    agent_cfg = habitat_sim.AgentConfiguration()
    specs = []
    for sensor_type, uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                               (habitat_sim.SensorType.DEPTH, "depth_sensor")):
        spec = habitat_sim.CameraSensorSpec()
        spec.uuid = uuid; spec.sensor_type = sensor_type
        spec.resolution = mn.Vector2i(height, width); spec.position = mn.Vector3(*config["sensor_position_habitat"])
        spec.orientation = mn.Vector3(*config.get("sensor_orientation_habitat_rad", [0, 0, 0]))
        spec.hfov = mn.Deg(hfov_deg); spec.near = min_depth; spec.far = max_depth
        specs.append(spec)
    agent_cfg.sensor_specifications = specs
    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
    navmesh_file = manifest.get("navmesh_file")
    if navmesh_file:
        sim.pathfinder.load_nav_mesh(str(navmesh_file))
    agent = sim.initialize_agent(0)
    reference_enable_physics = bool(config.get("reference_enable_physics", False))
    reference_mode = "fresh_full_scene_render" if not reference_enable_physics else "physics_cast_ray"
    checks = []
    route = config["route"]
    for record in manifest["frames"]:
        index = int(record["frame_index"])
        waypoint = route[index]
        state = agent.get_state()
        state.position = np.asarray(waypoint["position_habitat"], dtype=np.float32)
        state.rotation = quat_from_angle_axis(float(waypoint.get("yaw_rad", 0.0)), np.array([0, 1, 0], dtype=np.float32))
        agent.set_state(state, infer_sensor_states=True)
        actual = agent.get_state(); sensor = actual.sensor_states["depth_sensor"]
        depth = np.load(capture / str(record["frame_dir"]) / "depth_m.npy", allow_pickle=False)
        rgb = np.load(capture / str(record["frame_dir"]) / "rgb.npy", allow_pickle=False)
        rotation_h = quaternion.as_rotation_matrix(sensor.rotation)
        origin = np.asarray(sensor.position, dtype=np.float64)
        pixel_checks = []
        rendered_depth = None
        if reference_mode == "fresh_full_scene_render":
            # This is an independent fresh Habitat-Sim render of the same
            # full scene instance and pose.  It is intentionally not the
            # project's M1 backprojection and preserves object occlusions.
            rendered_depth = np.asarray(sim.get_sensor_observations()["depth_sensor"], dtype=np.float32)
            if rendered_depth.shape != depth.shape:
                raise RuntimeError(f"reference depth shape {rendered_depth.shape} != capture shape {depth.shape}")
        for u, v in ((int(round(cx)), int(round(cy))),
                     (min(width - 1, int(round(cx + width * 0.25))), int(round(cy))),
                     (int(round(cx)), min(height - 1, int(round(cy - height * 0.25))))):
            raw_value = float(depth[v, u])
            raw = raw_value if np.isfinite(raw_value) else None
            expected_axial = None
            ray_distance = None
            if reference_mode == "fresh_full_scene_render":
                raw_reference = float(rendered_depth[v, u])
                if np.isfinite(raw_reference) and raw_reference > 0.0:
                    expected_axial = raw_reference
            if expected_axial is not None and raw is not None and raw > 0.0:
                error = abs(raw - expected_axial)
                passed = error <= 0.03
            else:
                error = None if expected_axial is None or raw is None else abs(raw - expected_axial)
                passed = expected_axial is None and (raw is None or raw <= 0.0)
            if reference_mode == "physics_cast_ray":
                direction_camera = np.array([(u - cx) / fx, (v - cy) / fy, -1.0], dtype=np.float64)
                direction_camera /= np.linalg.norm(direction_camera)
                direction_h = rotation_h @ direction_camera
                ray = habitat_sim.geo.Ray(mn.Vector3(*origin), mn.Vector3(*direction_h))
                hits = sim.cast_ray(ray, max_distance=max_depth)
                ray_distance = None if not hits.hits else float(hits.hits[0].ray_distance)
                expected_axial = None if ray_distance is None else float(ray_distance * abs(direction_camera[2]))
                if expected_axial is not None and raw is not None and raw > 0.0:
                    error = abs(raw - expected_axial)
                    passed = error <= 0.03
                else:
                    error = None
                    passed = expected_axial is None and (raw is None or raw <= 0.0)
            pixel_checks.append({"u": u, "v": v, "raw_depth_m": raw,
                                 "ray_distance_m": ray_distance,
                                 "expected_axial_depth_m": expected_axial, "abs_error_m": error,
                                 "passed": passed})
        metadata = json.loads((capture / str(record["frame_dir"]) / "metadata.json").read_text(encoding="utf-8"))
        sensor_position_error = float(np.max(np.abs(origin - np.asarray(metadata["sensor_position_habitat"]))))
        sensor_rotation_error = float(np.max(np.abs(rotation_h - np.asarray(metadata["sensor_rotation_habitat"]))))
        frame_report = {"frame_index": index, "frame_id": record["frame_id"],
                       "pose_position_habitat": origin.tolist(), "pixel_checks": pixel_checks,
                       "sensor_rotation_habitat": rotation_h.tolist(),
                       "sensor_position_metadata_max_error_m": sensor_position_error,
                       "sensor_rotation_metadata_max_error": sensor_rotation_error,
                       "rgb_shape": list(rgb.shape), "rgb_dtype": str(rgb.dtype),
                       "rgb_alpha_present_in_raw": bool(rgb.shape[-1] == 4),
                       "all_pixel_checks_pass": all(item["passed"] for item in pixel_checks)}
        if rendered_depth is not None:
            capture_valid = np.isfinite(depth) & (depth > 0.0)
            reference_valid = np.isfinite(rendered_depth) & (rendered_depth > 0.0)
            overlap = capture_valid & reference_valid
            errors = np.abs(depth[overlap] - rendered_depth[overlap])
            frame_report["fresh_render_valid_pixel_count"] = int(np.count_nonzero(reference_valid))
            frame_report["capture_reference_valid_mask_xor_count"] = int(np.count_nonzero(capture_valid ^ reference_valid))
            frame_report["fresh_render_overlap_pixel_count"] = int(np.count_nonzero(overlap))
            frame_report["fresh_render_max_abs_error_m"] = float(errors.max()) if errors.size else None
            frame_report["fresh_render_mean_abs_error_m"] = float(errors.mean()) if errors.size else None
            frame_report["full_frame_depth_match"] = bool(
                np.array_equal(capture_valid, reference_valid)
                and errors.size > 0
                and float(errors.max()) <= 0.03
            )
        checks.append(frame_report)
    sim.close()
    positions = np.asarray([item["pose_position_habitat"] for item in checks])
    rotations = np.asarray([item["sensor_rotation_habitat"] for item in checks])
    report = {
        "kind": "habitat_sensor_geometry_validation",
        "capture": str(capture), "scene_file": str(scene),
        "depth_semantics_verified": "metric axial; fresh full-scene render comparison or ray_distance * abs(camera_direction_z)",
        "coordinate_transform": "C.x=H.x, C.y=-H.z, C.z=H.y; determinant +1",
        "camera_optical": "+X right, +Y down, +Z forward",
        "off_axis_pixels_checked": [[int(round(cx)), int(round(cy))],
                                    [min(width - 1, int(round(cx + width * 0.25))), int(round(cy))],
                                    [int(round(cx)), min(height - 1, int(round(cy - height * 0.25)))]],
        "frame_count": len(checks),
        "pose_translation_span_m": float(np.linalg.norm(positions.max(axis=0) - positions.min(axis=0))),
        "pose_movement_checked": bool(np.linalg.norm(positions.max(axis=0) - positions.min(axis=0)) > 0.1),
        "pose_rotation_checked": bool(np.max(np.abs(rotations - rotations[0])) > 1e-3),
        "actual_sensor_pose_metadata_max_error_m": max(item["sensor_position_metadata_max_error_m"] for item in checks),
        "actual_sensor_rotation_metadata_max_error": max(item["sensor_rotation_metadata_max_error"] for item in checks),
        "reference_mode": reference_mode,
        "checks": checks,
        "pass": all(item["all_pixel_checks_pass"] for item in checks)
                and max(item["sensor_position_metadata_max_error_m"] for item in checks) <= 1e-5
                and max(item["sensor_rotation_metadata_max_error"] for item in checks) <= 1e-5
                and all(item.get("full_frame_depth_match", True) for item in checks),
        "gt_source": (
            "fresh Habitat-Sim render against the loaded full scene asset; not project backprojection"
            if reference_mode == "fresh_full_scene_render"
            else "Habitat-Sim physics cast_ray against the loaded scene asset; not project backprojection"
        ),
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(_run(args.capture.resolve(), args.config.resolve(), args.output.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
