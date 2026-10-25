#!/usr/bin/env python3
"""Capture one fixed-pose RGB-D route from Habitat-Sim.

This is deliberately a capture-only adapter.  It imports Habitat-Sim here,
serializes raw observations plus actual sensor/agent poses, and leaves M1-M10
to the project Python replay runner.  Poses are set at route waypoints for a
calibrated observation sequence; this is not a physical robot or a navigation
policy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from adapters.habitat.raw import canonical_from_habitat_matrix, pose_matrix  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_pose_matrix(position, rotation, *, parent: str, child: str, timestamp: float) -> np.ndarray:
    del parent, child, timestamp
    return pose_matrix(position, rotation)


def _capture(args: argparse.Namespace) -> dict:
    try:
        import quaternion  # type: ignore
        import habitat_sim  # type: ignore
        import magnum as mn  # type: ignore
        from habitat_sim.utils.common import quat_from_angle_axis  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Habitat capture requires the pinned Habitat-Sim environment; "
            "run this script with /home/kiettran05/miniconda3/envs/habitat/bin/python"
        ) from exc

    config = json.loads(args.config.read_text(encoding="utf-8"))
    scene = args.scene.resolve()
    if not scene.is_file():
        raise FileNotFoundError(f"Habitat scene file does not exist: {scene}")
    dataset_type = str(config.get("dataset_type", "replica_cad"))
    dataset_version = config.get("dataset_version")
    default_prefix = "replica-cad" if dataset_type == "replica_cad" else dataset_type.replace("_", "-")
    frame_id_prefix = str(config.get("frame_id_prefix", default_prefix))
    scene_dataset_config = None
    if config.get("scene_dataset_config"):
        scene_dataset_config = (ROOT / str(config["scene_dataset_config"])).resolve()
        if not scene_dataset_config.is_file():
            raise FileNotFoundError(f"configured Habitat scene dataset config does not exist: {scene_dataset_config}")
    route = config.get("route")
    if not isinstance(route, list) or not route:
        raise ValueError("config.route must contain at least one fixed observation pose")
    width, height = int(config["resolution_px"][0]), int(config["resolution_px"][1])
    hfov_deg = float(config["horizontal_fov_deg"])
    min_depth_m, max_depth_m = float(config["min_depth_m"]), float(config["max_depth_m"])
    dt_s = float(config["simulation_step_s"])
    sensor_position = mn.Vector3(*[float(v) for v in config["sensor_position_habitat"]])
    sensor_orientation = mn.Vector3(*[float(v) for v in config.get("sensor_orientation_habitat_rad", [0, 0, 0])])

    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_id = str(scene)
    if scene_dataset_config is not None:
        sim_cfg.scene_dataset_config_file = str(scene_dataset_config)
    sim_cfg.create_renderer = True
    sim_cfg.requires_textures = True
    sim_cfg.gpu_device_id = int(config.get("gpu_device_id", 0))
    agent_cfg = habitat_sim.AgentConfiguration()
    sensor_specs = []
    for sensor_type, uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                               (habitat_sim.SensorType.DEPTH, "depth_sensor")):
        spec = habitat_sim.CameraSensorSpec()
        spec.uuid = uuid
        spec.sensor_type = sensor_type
        spec.resolution = mn.Vector2i(height, width)
        spec.position = sensor_position
        spec.orientation = sensor_orientation
        spec.hfov = mn.Deg(hfov_deg)
        spec.near = min_depth_m
        spec.far = max_depth_m
        sensor_specs.append(spec)
    agent_cfg.sensor_specifications = sensor_specs
    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
    navmesh_path = None
    if config.get("navmesh_file"):
        navmesh_path = (ROOT / str(config["navmesh_file"])).resolve()
        if not navmesh_path.is_file():
            sim.close()
            raise FileNotFoundError(f"configured Habitat navmesh does not exist: {navmesh_path}")
        if not sim.pathfinder.load_nav_mesh(str(navmesh_path)):
            sim.close()
            raise RuntimeError(f"Habitat-Sim failed to load configured navmesh: {navmesh_path}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    frames_dir = output / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    canonical_from_habitat = canonical_from_habitat_matrix().tolist()
    agent = sim.initialize_agent(0)
    records = []
    try:
        for frame_index, waypoint in enumerate(route):
            if not isinstance(waypoint, dict):
                raise ValueError("each route entry must be an object")
            position = np.asarray(waypoint["position_habitat"], dtype=np.float64)
            yaw = float(waypoint.get("yaw_rad", 0.0))
            if position.shape != (3,) or not np.isfinite(position).all() or not math.isfinite(yaw):
                raise ValueError(f"invalid route pose at index {frame_index}")
            navigable = bool(sim.pathfinder.is_loaded and sim.pathfinder.is_navigable(position))
            if config.get("require_navigable", True) and not navigable:
                raise RuntimeError(f"route pose {frame_index} is not navigable according to the loaded navmesh")
            state = agent.get_state()
            state.position = position.astype(np.float32)
            state.rotation = quat_from_angle_axis(yaw, np.array([0.0, 1.0, 0.0], dtype=np.float32))
            agent.set_state(state, infer_sensor_states=True)
            actual = agent.get_state()
            observations = sim.get_sensor_observations()
            depth = np.asarray(observations["depth_sensor"], dtype=np.float32).copy()
            color = np.asarray(observations["color_sensor"], dtype=np.uint8).copy()
            if depth.shape != (height, width):
                raise RuntimeError(f"unexpected depth shape {depth.shape}; expected {(height, width)}")
            if color.shape[:2] != (height, width):
                raise RuntimeError(f"unexpected color shape {color.shape}; expected {(height, width)}")
            valid = np.isfinite(depth) & (depth >= min_depth_m) & (depth <= max_depth_m)
            depth[~valid] = np.nan
            frame_id = f"{frame_id_prefix}-{frame_index:04d}"
            timestamp_s = float(frame_index * dt_s)
            frame_dir = frames_dir / f"{frame_index:06d}"
            frame_dir.mkdir(parents=True, exist_ok=True)
            np.save(frame_dir / "depth_m.npy", depth, allow_pickle=False)
            np.save(frame_dir / "valid_mask.npy", valid, allow_pickle=False)
            np.save(frame_dir / "rgb.npy", color, allow_pickle=False)
            agent_rotation_h = quaternion.as_rotation_matrix(actual.rotation)
            sensor_state = actual.sensor_states["depth_sensor"]
            sensor_rotation_h = quaternion.as_rotation_matrix(sensor_state.rotation)
            t_h_base = _finite_pose_matrix(actual.position, agent_rotation_h,
                                           parent="habitat_world", child="habitat_base", timestamp=timestamp_s)
            t_h_sensor = _finite_pose_matrix(sensor_state.position, sensor_rotation_h,
                                             parent="habitat_world", child="habitat_sensor", timestamp=timestamp_s)
            t_c_h = np.asarray(canonical_from_habitat, dtype=np.float64)
            t_c_base = t_c_h @ t_h_base
            metadata = {
                "schema_version": 1, "dataset_type": dataset_type, "dataset_version": dataset_version,
                "scene_id": config.get("scene_id", scene.stem),
                "scene_file": str(scene), "scene_sha256": _sha256(scene),
                "scene_dataset_config": None if scene_dataset_config is None else str(scene_dataset_config),
                "scene_dataset_config_sha256": None if scene_dataset_config is None else _sha256(scene_dataset_config),
                "sequence_id": config.get("sequence_id", "replica-cad-route-01"),
                "frame_id": frame_id, "frame_index": frame_index, "timestamp_s": timestamp_s,
                "width": width, "height": height, "hfov_deg": hfov_deg,
                "calibration_id": config.get("calibration_id", "habitat-rgbd-v1"),
                "depth_units": "m", "depth_representation": "axial",
                "min_depth_m": min_depth_m, "max_depth_m": max_depth_m,
                "rgb_encoding": "Habitat color observation; RGB(A), alpha dropped by adapter",
                "depth_encoding": "float32 metric axial metres; invalid values stored as NaN with valid_mask",
                "canonical_from_habitat": canonical_from_habitat,
                "sensor_position_habitat": np.asarray(sensor_state.position, dtype=float).tolist(),
                "sensor_rotation_habitat": sensor_rotation_h.tolist(),
                "base_pose_habitat": t_h_base.tolist(), "base_pose_canonical": t_c_base.tolist(),
                "sensor_pose_source": "Habitat agent sensor state",
                "base_pose_source": "Habitat agent ground-truth state",
                "navmesh_used_for_route": navigable,
                "navigable": navigable,
                "route_entry": waypoint,
                "raw_files": ["depth_m.npy", "valid_mask.npy", "rgb.npy"],
                "habitat_sim_depth_semantics": "configured CameraSensorSpec DEPTH, unnormalized metric axial output",
            }
            (frame_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            records.append({"frame_id": frame_id, "frame_index": frame_index,
                            "timestamp_s": timestamp_s, "frame_dir": str(frame_dir.relative_to(output))})
            print(f"PROGRESS: frame {frame_index + 1}/{len(route)} captured", flush=True)
    finally:
        sim.close()
    manifest = {
        "schema_version": 1,
        "kind": "habitat_sim_replica_cad_raw_rgbd_capture" if dataset_type == "replica_cad" else "habitat_sim_raw_rgbd_capture",
        "dataset_type": dataset_type, "dataset_version": dataset_version,
        "status": "pass", "habitat_sim_version": getattr(habitat_sim, "__version__", "unknown"),
        "scene_id": config.get("scene_id", scene.stem), "scene_file": str(scene),
        "scene_sha256": _sha256(scene), "scene_provenance": config.get("scene_provenance"),
        "scene_dataset_config": None if scene_dataset_config is None else str(scene_dataset_config),
        "scene_dataset_config_sha256": None if scene_dataset_config is None else _sha256(scene_dataset_config),
        "scene_license_note": config.get("scene_license_note"),
        "navmesh_file": None if navmesh_path is None else str(navmesh_path),
        "navmesh_sha256": None if navmesh_path is None else _sha256(navmesh_path),
        "sequence_id": config.get("sequence_id", "replica-cad-route-01"),
        "route_type": "fixed_ground_truth_observation_poses",
        "motion_model": "set_agent_state_at_observation_steps; no physics robot, no SLAM",
        "navmesh_route_tool": "Habitat-Sim pathfinder navigability check only; no planner",
        "frames": records, "frame_count": len(records),
        "resolution_px": [width, height], "horizontal_fov_deg": hfov_deg,
        "simulation_step_s": dt_s, "canonical_world": "Z-up; C.x=H.x, C.y=-H.z, C.z=H.y",
        "camera_optical": "+X right, +Y down, +Z forward",
        "depth_units": "m", "depth_representation": "axial", "rgb_channel_order": "RGB",
        "canonical_from_habitat": canonical_from_habitat,
        "sensor_extrinsics": "actual per-frame Habitat sensor state; co-located color/depth specs",
        "semantic_gt_saved": False, "room_gt_passed_to_builder": False,
        "cloud_upload": False,
    }
    (output / "capture_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", type=Path, required=True, help="one ReplicaCAD .glb scene")
    parser.add_argument("--config", type=Path, required=True, help="fixed route/sensor JSON")
    parser.add_argument("--output", type=Path, required=True, help="new raw capture directory")
    args = parser.parse_args()
    print(json.dumps(_capture(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
