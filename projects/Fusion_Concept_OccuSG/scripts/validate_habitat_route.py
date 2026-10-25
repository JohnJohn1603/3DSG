#!/usr/bin/env python3
"""Validate fixed Habitat GT poses against a loaded scene and navmesh."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _input_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    parts = path.parts
    if len(parts) >= 2 and parts[0] == "artifacts" and parts[1] in {"habitat_data", "models"}:
        env = json.loads((ROOT / "configs/launcher/local_environment.json").read_text(encoding="utf-8"))
        external_root = env["habitat_data_root"] if parts[1] == "habitat_data" else env["model_root"]
        return Path(external_root).expanduser().resolve().joinpath(*parts[2:]).resolve()
    return (ROOT / path).resolve()


def validate(scene_file: Path, config_path: Path) -> dict:
    import habitat_sim  # type: ignore
    import magnum as mn  # type: ignore

    config = json.loads(config_path.read_text(encoding="utf-8"))
    scene_file, config_path = scene_file.resolve(), config_path.resolve()
    dataset_config = _input_path(config["scene_dataset_config"])
    navmesh = _input_path(config["navmesh_file"])
    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_id = str(scene_file)
    sim_cfg.scene_dataset_config_file = str(dataset_config)
    sim_cfg.create_renderer = False
    sim_cfg.requires_textures = False
    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [habitat_sim.AgentConfiguration()]))
    try:
        if not sim.pathfinder.load_nav_mesh(str(navmesh)):
            raise RuntimeError("configured navmesh did not load")
        route = config.get("route", [])
        if len(route) < 2:
            raise ValueError("route needs at least two fixed poses")
        points = [np.asarray(row["position_habitat"], dtype=np.float64) for row in route]
        navigable = [bool(sim.pathfinder.is_navigable(point)) for point in points]
        edges = []
        for idx in range(len(points) - 1):
            path = habitat_sim.ShortestPath()
            path.requested_start = mn.Vector3(*[float(v) for v in points[idx]])
            path.requested_end = mn.Vector3(*[float(v) for v in points[idx + 1]])
            found = bool(sim.pathfinder.find_path(path))
            edges.append({"from_index": idx, "to_index": idx + 1,
                          "from_label": route[idx].get("label"), "to_label": route[idx + 1].get("label"),
                          "path_exists": found,
                          "geodesic_distance_m": float(path.geodesic_distance) if found else None,
                          "path_points_habitat": [[float(v) for v in p] for p in path.points] if found else []})
        return {
            "kind": "habitat_fixed_gt_route_validation",
            "status": "pass" if all(navigable) and all(edge["path_exists"] for edge in edges) else "fail",
            "scene_id": config.get("scene_id"),
            "scene_file": str(scene_file),
            "scene_sha256": __import__("hashlib").sha256(scene_file.read_bytes()).hexdigest(),
            "scene_dataset_config": str(dataset_config),
            "navmesh_file": str(navmesh),
            "route_config": str(config_path),
            "route_config_sha256": __import__("hashlib").sha256(config_path.read_bytes()).hexdigest(),
            "pose_count": len(route),
            "navigable_pose_count": sum(navigable),
            "all_poses_navigable": all(navigable),
            "all_consecutive_poses_connected": all(edge["path_exists"] for edge in edges),
            "waypoints": [{"index": idx, "label": route[idx].get("label"),
                           "position_habitat": [float(v) for v in points[idx]],
                           "yaw_rad": float(route[idx].get("yaw_rad", 0.0)),
                           "navigable": navigable[idx]} for idx in range(len(route))],
            "edges": edges,
            "navmesh_is_room_ground_truth": False,
        }
    finally:
        sim.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite route validation report")
    report = validate(args.scene, args.config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "pose_count": report["pose_count"],
                      "navigable_pose_count": report["navigable_pose_count"],
                      "all_consecutive_poses_connected": report["all_consecutive_poses_connected"],
                      "report": str(args.output)}, indent=2))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
