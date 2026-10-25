#!/usr/bin/env python3
"""Preview a selected HM3D scene using the pinned Habitat-Sim install.

Without --position-habitat this writes only a navmesh top-down preview. After
visual review, pass an explicitly chosen pose to additionally render one RGB
view using the locked apt_0 sensor profile in the supplied JSON config.
Never use the navmesh or semantic annotations as predictor inputs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="locked sensor JSON or HM3D preview template")
    parser.add_argument("--scene-file", type=Path, required=True, help="HM3D .basis.glb for provenance/existence check")
    parser.add_argument("--scene-id", required=True, help="scene ID/path registered by the selected scene dataset config")
    parser.add_argument("--output", type=Path, required=True, help="new preview directory; must not already exist")
    parser.add_argument("--meters-per-pixel", type=float, default=0.05)
    parser.add_argument("--floor-height", type=float, default=None, help="Habitat Y coordinate for navmesh slice")
    parser.add_argument("--position-habitat", type=float, nargs=3, default=None,
                        metavar=("X", "Y", "Z"), help="optional reviewed RGB preview pose")
    parser.add_argument("--yaw-rad", type=float, default=0.0)
    args = parser.parse_args()
    config_path = args.config.resolve()
    scene_file = args.scene_file.resolve()
    if not config_path.is_file() or not scene_file.is_file():
        raise FileNotFoundError("config and scene file must exist; only run after download/extraction is complete")
    if args.output.exists():
        raise FileExistsError("refusing to overwrite an existing preview directory")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    dataset_config = (ROOT / str(config["scene_dataset_config"])).resolve()
    if not dataset_config.is_file():
        raise FileNotFoundError("configured HM3D scene dataset config is missing")
    if args.meters_per_pixel <= 0:
        raise ValueError("meters-per-pixel must be positive")

    try:
        import habitat_sim  # type: ignore
        import magnum as mn  # type: ignore
        from habitat_sim.utils.common import quat_from_angle_axis  # type: ignore
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("run with the already-installed pinned Habitat-Sim environment") from exc

    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_dataset_config_file = str(dataset_config)
    sim_cfg.scene_id = args.scene_id
    sim_cfg.create_renderer = True
    sim_cfg.requires_textures = True
    sim_cfg.gpu_device_id = int(config.get("gpu_device_id", -1))
    agent_cfg = habitat_sim.AgentConfiguration()
    sensor_specs = []
    width, height = [int(v) for v in config["resolution_px"]]
    for sensor_type, uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                              (habitat_sim.SensorType.DEPTH, "depth_sensor")):
        spec = habitat_sim.CameraSensorSpec()
        spec.uuid = uuid
        spec.sensor_type = sensor_type
        spec.resolution = mn.Vector2i(height, width)
        spec.position = mn.Vector3(*[float(v) for v in config["sensor_position_habitat"]])
        spec.orientation = mn.Vector3(*[float(v) for v in config.get("sensor_orientation_habitat_rad", [0, 0, 0])])
        spec.hfov = mn.Deg(float(config["horizontal_fov_deg"]))
        spec.near = float(config["min_depth_m"])
        spec.far = float(config["max_depth_m"])
        sensor_specs.append(spec)
    agent_cfg.sensor_specifications = sensor_specs

    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
    try:
        if not sim.pathfinder.is_loaded:
            raise RuntimeError("scene has no loaded navmesh; cannot produce top-down preview")
        bounds = sim.pathfinder.get_bounds()
        floor_height = float(args.floor_height if args.floor_height is not None else (bounds[0][1] + bounds[1][1]) / 2.0)
        topdown = np.asarray(sim.pathfinder.get_topdown_view(float(args.meters_per_pixel), floor_height))
        if topdown.ndim != 2 or topdown.size == 0:
            raise RuntimeError("Habitat returned an empty navmesh top-down image")
        args.output.mkdir(parents=True, exist_ok=False)
        gray = np.clip(topdown.astype(np.float32), 0.0, 1.0)
        Image.fromarray(np.uint8(gray * 255.0), mode="L").save(args.output / "navmesh_topdown.png")
        outputs = ["navmesh_topdown.png"]
        if args.position_habitat is not None:
            position = np.asarray(args.position_habitat, dtype=np.float32)
            if not sim.pathfinder.is_navigable(position):
                raise ValueError("RGB preview pose is not navigable; inspect top-down and choose a pose explicitly")
            agent = sim.initialize_agent(0)
            state = agent.get_state()
            state.position = position
            state.rotation = quat_from_angle_axis(float(args.yaw_rad), np.array([0.0, 1.0, 0.0], dtype=np.float32))
            agent.set_state(state, infer_sensor_states=True)
            rgb = np.asarray(sim.get_sensor_observations()["color_sensor"])[..., :3]
            Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(args.output / "rgb_preview.png")
            outputs.append("rgb_preview.png")
        manifest = {
            "kind": "hm3d_scene_visual_preview",
            "dataset_version": config.get("dataset_version"),
            "scene_id": args.scene_id,
            "scene_file": str(scene_file),
            "scene_dataset_config": str(dataset_config),
            "habitat_sim_version": getattr(habitat_sim, "__version__", "unknown"),
            "navmesh_topdown": {"meters_per_pixel": float(args.meters_per_pixel),
                                 "floor_height_habitat_y": floor_height,
                                 "image_shape": list(topdown.shape)},
            "rgb_preview_pose_habitat": None if args.position_habitat is None else {
                "position": list(args.position_habitat), "yaw_rad": args.yaw_rad,
                "sensor_profile_source": str(config_path)},
            "outputs": outputs,
            "semantic_annotations_used": False,
            "predictor_run": False,
        }
        (args.output / "preview_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(manifest, indent=2))
    finally:
        sim.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
