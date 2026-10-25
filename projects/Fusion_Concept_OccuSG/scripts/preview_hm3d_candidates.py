#!/usr/bin/env python3
"""Render RGB/navmesh-floor contact sheets for completed HM3D minival v0.2.

All sampled viewpoints are preview-only samples from the navmesh, not route
waypoints or predictor inputs. Navmesh views indicate navigability only and
must not be treated as room ground truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "artifacts/habitat_data/scene_datasets/hm3d"
RECEIPT = ROOT / "artifacts/runs/hm3d_integration_20261005/download_receipt.json"
TILE_W, TILE_H, LABEL_H = 320, 240, 30


def _floor_samples(pathfinder: Any, seed: int, count: int = 160) -> List[Tuple[float, np.ndarray]]:
    pathfinder.seed(seed)
    points = []
    for _ in range(count):
        point = np.asarray(pathfinder.get_random_navigable_point(), dtype=np.float64)
        if point.shape == (3,) and np.isfinite(point).all():
            points.append(point)
    if not points:
        raise RuntimeError("navmesh produced no finite navigable point for preview")
    points.sort(key=lambda point: float(point[1]))
    clusters: List[List[np.ndarray]] = []
    for point in points:
        if not clusters or float(point[1] - np.median([p[1] for p in clusters[-1]])) > 0.75:
            clusters.append([point])
        else:
            clusters[-1].append(point)
    floors = []
    for cluster in clusters:
        height = float(np.median([point[1] for point in cluster]))
        representative = min(cluster, key=lambda point: abs(float(point[1]) - height))
        floors.append((height, representative))
    return floors


def _centered(image: Any, width: int, height: int) -> Any:
    from PIL import Image
    scale = min(width / image.width, height / image.height)
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (28, 30, 34))
    canvas.paste(resized.convert("RGB"), ((width - size[0]) // 2, (height - size[1]) // 2))
    return canvas


def _save_contact_sheet(scene_output: Path, scene_id: str, floors: List[Dict[str, Any]]) -> str:
    from PIL import Image, ImageDraw
    columns = 5
    sheet = Image.new("RGB", (columns * TILE_W, len(floors) * (TILE_H + LABEL_H)), (22, 24, 27))
    draw = ImageDraw.Draw(sheet)
    for row, floor in enumerate(floors):
        y = row * (TILE_H + LABEL_H)
        topdown = Image.open(scene_output / floor["topdown_file"])
        previews = [topdown] + [Image.open(scene_output / name) for name in floor["rgb_files"]]
        labels = ["navmesh top-down"] + ["RGB preview %d" % (idx + 1) for idx in range(4)]
        for column, (image, label) in enumerate(zip(previews, labels)):
            x = column * TILE_W
            sheet.paste(_centered(image, TILE_W, TILE_H), (x, y))
            draw.text((x + 8, y + TILE_H + 6), "%s | y=%.2f m" % (label, floor["height_habitat_y_m"]),
                      fill=(238, 238, 238))
    filename = "contact_sheet.png"
    sheet.save(scene_output / filename)
    return filename


def preview_scene(scene_file: Path, dataset_root: Path, config_path: Path, config: Dict[str, Any], output: Path,
                  meters_per_pixel: float, sample_count: int) -> Dict[str, Any]:
    import habitat_sim  # type: ignore
    import magnum as mn  # type: ignore
    from habitat_sim.utils.common import quat_from_angle_axis  # type: ignore
    from PIL import Image

    scene_id = scene_file.parent.name
    scene_rel = scene_file.relative_to(config_path.parent).as_posix()
    navmesh_file = scene_file.with_name(scene_file.name[:-len(".basis.glb")] + ".basis.navmesh")
    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_dataset_config_file = str(config_path)
    sim_cfg.scene_id = scene_rel
    sim_cfg.create_renderer = True
    sim_cfg.requires_textures = True
    sim_cfg.gpu_device_id = int(config.get("gpu_device_id", -1))
    agent_cfg = habitat_sim.AgentConfiguration()
    width, height = [int(value) for value in config["resolution_px"]]
    sensors = []
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
        sensors.append(spec)
    agent_cfg.sensor_specifications = sensors
    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
    try:
        if not sim.pathfinder.is_loaded and navmesh_file.is_file():
            sim.pathfinder.load_nav_mesh(str(navmesh_file))
        if not sim.pathfinder.is_loaded:
            raise RuntimeError("no usable navmesh loaded for scene " + scene_id)
        seed = int(hashlib.sha256(scene_id.encode("utf-8")).hexdigest()[:8], 16)
        floor_samples = _floor_samples(sim.pathfinder, seed, sample_count)
        output.mkdir(parents=True, exist_ok=False)
        agent = sim.initialize_agent(0)
        floor_records = []
        yaws = [0.0, math.pi / 2.0, math.pi, -math.pi / 2.0]
        for floor_index, (floor_y, position) in enumerate(floor_samples):
            topdown = np.asarray(sim.pathfinder.get_topdown_view(float(meters_per_pixel), floor_y))
            topdown_name = "floor_%02d_navmesh_topdown.png" % floor_index
            Image.fromarray(np.uint8(topdown.astype(np.uint8) * 255)).save(output / topdown_name)
            rgb_files = []
            for yaw_index, yaw in enumerate(yaws):
                state = agent.get_state()
                state.position = position.astype(np.float32)
                state.rotation = quat_from_angle_axis(float(yaw), np.array([0.0, 1.0, 0.0], dtype=np.float32))
                agent.set_state(state, infer_sensor_states=True)
                rgb = np.asarray(sim.get_sensor_observations()["color_sensor"])[..., :3].astype(np.uint8)
                filename = "floor_%02d_rgb_%02d.png" % (floor_index, yaw_index)
                Image.fromarray(rgb).save(output / filename)
                rgb_files.append(filename)
            floor_records.append({
                "floor_index": floor_index,
                "height_habitat_y_m": floor_y,
                "preview_only_sample_pose_habitat": [float(v) for v in position],
                "preview_pose_is_route_waypoint": False,
                "topdown_file": topdown_name,
                "topdown_shape": list(topdown.shape),
                "rgb_files": rgb_files,
                "yaw_rad": yaws,
            })
        contact_sheet = _save_contact_sheet(output, scene_id, floor_records)
        record = {
            "scene_id": scene_id,
            "scene_file": scene_file.relative_to(dataset_root).as_posix(),
            "simulator_scene_id": scene_rel,
            "scene_file_bytes": scene_file.stat().st_size,
            "navmesh_file": str(navmesh_file.relative_to(dataset_root)) if navmesh_file.is_file() else None,
            "scene_dataset_config": str(config_path),
            "semantic_mesh_present": scene_file.with_name(scene_file.name[:-len(".basis.glb")] + ".semantic.glb").is_file(),
            "semantic_descriptor_present": scene_file.with_name(scene_file.name[:-len(".basis.glb")] + ".semantic.txt").is_file(),
            "habitat_sim_version": getattr(habitat_sim, "__version__", "unknown"),
            "sensor_profile": {key: config[key] for key in ("resolution_px", "horizontal_fov_deg",
                                                              "sensor_position_habitat", "min_depth_m", "max_depth_m")},
            "navmesh_is_room_ground_truth": False,
            "semantic_annotations_used_for_preview": False,
            "floors": floor_records,
            "contact_sheet": contact_sheet,
        }
        (output / "preview_manifest.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return record
    finally:
        sim.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--dataset-config", type=Path,
                        default=Path("artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/hm3d_minival_basis.scene_dataset_config.json"))
    parser.add_argument("--sensor-config", type=Path,
                        default=Path("configs/habitat/hm3d_minival_v02_preview_template.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/runs/hm3d_integration_20261005/candidate_previews"))
    parser.add_argument("--meters-per-pixel", type=float, default=0.05)
    parser.add_argument("--random-samples-per-floor", type=int, default=160)
    parser.add_argument("--scene-id", action="append", default=None,
                        help="optional scene directory ID filter; repeat for multiple candidates")
    args = parser.parse_args()
    data_root = args.dataset_root.resolve()
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    if receipt.get("status") != "downloaded" or receipt.get("dataset_version") != "HM3D v0.2":
        raise RuntimeError("completed HM3D v0.2 download receipt is required")
    config_path = (ROOT / args.dataset_config).resolve() if not args.dataset_config.is_absolute() else args.dataset_config
    sensor_config_path = (ROOT / args.sensor_config).resolve() if not args.sensor_config.is_absolute() else args.sensor_config
    if not config_path.is_file() or not sensor_config_path.is_file():
        raise FileNotFoundError("scene dataset config or sensor profile config is missing")
    config = json.loads(sensor_config_path.read_text(encoding="utf-8"))
    if args.output.exists():
        raise FileExistsError("refusing to overwrite an existing candidate preview directory")
    scenes = sorted((data_root / "minival").glob("*/*.basis.glb"))
    if args.scene_id:
        selected = set(args.scene_id)
        scenes = [scene for scene in scenes if scene.parent.name in selected]
        missing = selected - {scene.parent.name for scene in scenes}
        if missing:
            raise ValueError("requested scene IDs are not present in minival assets: " + ", ".join(sorted(missing)))
    if not scenes:
        raise FileNotFoundError("no minival .basis.glb scenes found")
    args.output.mkdir(parents=True, exist_ok=False)

    import habitat_sim  # type: ignore
    habitat_sim.logging.logger.setLevel(habitat_sim.logging.ERROR)
    records = []
    failures = []
    for scene in scenes:
        scene_output = args.output / scene.parent.name
        try:
            records.append(preview_scene(scene, data_root, config_path, config, scene_output,
                                         args.meters_per_pixel, args.random_samples_per_floor))
            print("previewed", scene.parent.name)
        except Exception as exc:
            failures.append({"scene_id": scene.parent.name, "error_type": type(exc).__name__,
                             "message": str(exc)})
            print("preview failed", scene.parent.name, type(exc).__name__, str(exc))
    index = {"kind": "hm3d_minival_v0.2_candidate_preview_index", "dataset_version": "HM3D v0.2",
             "split": "minival", "status": "pass" if not failures else "partial",
             "scene_count": len(scenes), "previewed_count": len(records), "failures": failures,
             "previews": records, "selection_basis": "architecture and asset usability only; no pipeline score",
             "navmesh_is_room_ground_truth": False}
    (args.output / "candidate_index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": index["status"], "previewed": len(records), "failures": failures,
                      "index": str(args.output / "candidate_index.json")}, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
