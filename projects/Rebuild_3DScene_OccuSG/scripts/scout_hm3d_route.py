#!/usr/bin/env python3
"""Render preview-only RGB views from spatially spread navmesh samples.

The output is for manual fixed-route design. Sampling is not navigation policy,
waypoints are not automatically committed, and semantic annotations are unused.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "artifacts/habitat_data/scene_datasets/hm3d"


def choose_spread(points: np.ndarray, count: int) -> np.ndarray:
    if len(points) <= count:
        return points
    center = points[:, [0, 2]].mean(axis=0)
    chosen = [int(np.argmax(np.sum((points[:, [0, 2]] - center) ** 2, axis=1)))]
    while len(chosen) < count:
        distances = np.min(np.linalg.norm(points[:, None, [0, 2]] - points[chosen][None, :, [0, 2]], axis=2), axis=1)
        distances[chosen] = -1.0
        chosen.append(int(np.argmax(distances)))
    return points[chosen]


def largest_connected_samples(pathfinder: Any, mn: Any, points: np.ndarray) -> np.ndarray:
    if len(points) < 2:
        return points
    probe_indices = np.linspace(0, len(points) - 1, min(12, len(points)), dtype=int)
    best = []
    for index in probe_indices:
        start = points[index]
        reachable = []
        for point in points:
            path = __import__("habitat_sim").ShortestPath()
            path.requested_start = mn.Vector3(*[float(v) for v in start])
            path.requested_end = mn.Vector3(*[float(v) for v in point])
            if pathfinder.find_path(path):
                reachable.append(point)
        if len(reachable) > len(best):
            best = reachable
    return np.asarray(best, dtype=np.float64)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene-id", required=True)
    parser.add_argument("--floor-height", type=float, required=True)
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path,
                        default=Path("configs/habitat/hm3d_minival_v02_preview_template.json"))
    parser.add_argument("--dataset-config", type=Path,
                        default=Path("artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/hm3d_minival_basis.scene_dataset_config.json"))
    args = parser.parse_args()
    scene_dir = DATA / "minival" / args.scene_id
    scene_file = next(scene_dir.glob("*.basis.glb")).resolve()
    navmesh = next(scene_dir.glob("*.basis.navmesh")).resolve()
    config = json.loads((ROOT / args.config).read_text(encoding="utf-8"))
    dataset_config = (ROOT / args.dataset_config).resolve()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite existing route-scoute evidence")
    import habitat_sim  # type: ignore
    import magnum as mn  # type: ignore
    from habitat_sim.utils.common import quat_from_angle_axis  # type: ignore
    from PIL import Image, ImageDraw

    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_dataset_config_file = str(dataset_config.resolve())
    sim_cfg.scene_id = scene_file.relative_to(dataset_config.parent).as_posix()
    sim_cfg.create_renderer = True
    sim_cfg.requires_textures = True
    sim_cfg.gpu_device_id = int(config.get("gpu_device_id", -1))
    agent_cfg = habitat_sim.AgentConfiguration()
    width, height = [int(x) for x in config["resolution_px"]]
    sensors = []
    for sensor_type, uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                              (habitat_sim.SensorType.DEPTH, "depth_sensor")):
        spec = habitat_sim.CameraSensorSpec()
        spec.uuid, spec.sensor_type = uuid, sensor_type
        spec.resolution = mn.Vector2i(height, width)
        spec.position = mn.Vector3(*[float(v) for v in config["sensor_position_habitat"]])
        spec.orientation = mn.Vector3(*[float(v) for v in config.get("sensor_orientation_habitat_rad", [0, 0, 0])])
        spec.hfov = mn.Deg(float(config["horizontal_fov_deg"]))
        spec.near, spec.far = float(config["min_depth_m"]), float(config["max_depth_m"])
        sensors.append(spec)
    agent_cfg.sensor_specifications = sensors
    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
    try:
        if not sim.pathfinder.is_loaded:
            sim.pathfinder.load_nav_mesh(str(navmesh.resolve()))
        if not sim.pathfinder.is_loaded:
            raise RuntimeError("navmesh did not load")
        sim.pathfinder.seed(802)
        sampled = []
        for _ in range(600):
            point = np.asarray(sim.pathfinder.get_random_navigable_point(), dtype=np.float64)
            if abs(float(point[1]) - args.floor_height) <= 0.25:
                sampled.append(point)
        if len(sampled) < 2:
            raise RuntimeError("too few navigable samples at requested floor height")
        connected = largest_connected_samples(sim.pathfinder, mn, np.asarray(sampled))
        if len(connected) < 2:
            raise RuntimeError("navmesh has fewer than two reachable samples at requested floor height")
        positions = choose_spread(connected, max(2, args.count))
        args.output.mkdir(parents=True, exist_ok=False)
        agent = sim.initialize_agent(0)
        yaws = [0.0, math.pi / 2.0, math.pi, -math.pi / 2.0]
        cards = []
        for idx, position in enumerate(positions):
            files = []
            for j, yaw in enumerate(yaws):
                state = agent.get_state()
                state.position = position.astype(np.float32)
                state.rotation = quat_from_angle_axis(float(yaw), np.array([0.0, 1.0, 0.0], dtype=np.float32))
                agent.set_state(state, infer_sensor_states=True)
                rgb = np.asarray(sim.get_sensor_observations()["color_sensor"])[..., :3].astype(np.uint8)
                filename = "candidate_%02d_yaw_%02d.png" % (idx, j)
                Image.fromarray(rgb).save(args.output / filename)
                files.append(filename)
            cards.append({"candidate_id": idx, "position_habitat": [float(v) for v in position],
                          "yaw_rad": yaws, "rgb_files": files, "navmesh_is_navigable": bool(sim.pathfinder.is_navigable(position))})
        path_rows = []
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                path = habitat_sim.ShortestPath()
                path.requested_start = mn.Vector3(*[float(v) for v in positions[i]])
                path.requested_end = mn.Vector3(*[float(v) for v in positions[j]])
                found = bool(sim.pathfinder.find_path(path))
                path_rows.append({"from_candidate": i, "to_candidate": j, "path_exists": found,
                                  "distance_m": float(path.geodesic_distance) if found else None,
                                  "path_points_habitat": [[float(v) for v in point] for point in path.points] if found else []})
        tile_w, tile_h = width, height
        sheet = Image.new("RGB", (4 * tile_w, len(cards) * (tile_h + 34)), (25, 27, 30))
        draw = ImageDraw.Draw(sheet)
        for card in cards:
            y = card["candidate_id"] * (tile_h + 34)
            for j, filename in enumerate(card["rgb_files"]):
                sheet.paste(Image.open(args.output / filename).convert("RGB"), (j * tile_w, y))
            draw.text((4, y + tile_h + 7), "candidate %02d, Habitat position %s" %
                      (card["candidate_id"], [round(v, 2) for v in card["position_habitat"]]), fill=(245, 245, 245))
        sheet.save(args.output / "route_candidate_contact.png")
        report = {"kind": "hm3d_fixed_route_candidate_scout", "scene_id": args.scene_id,
                  "scene_file": str(scene_file.relative_to(DATA.resolve())), "floor_height_habitat_y_m": args.floor_height,
                  "sensor_profile_source": str(args.config), "scene_dataset_config": str(dataset_config),
                  "candidates": cards, "pairwise_geodesic_checks": path_rows,
                  "route_committed": False, "semantic_annotations_used": False,
                  "navmesh_is_room_ground_truth": False}
        (args.output / "route_candidates.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"scene_id": args.scene_id, "candidates": len(cards),
                          "path_checks": path_rows, "contact_sheet": str(args.output / "route_candidate_contact.png")}, indent=2))
    finally:
        sim.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
