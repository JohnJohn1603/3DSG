#!/usr/bin/env python3
"""Create a short, fixed ReplicaCAD route from navmesh geometry only.

The route is selected before perception and uses no detector output, object
labels, room labels, annotations, or scene GT semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def path_sample(points: list, fraction: float) -> tuple[np.ndarray, np.ndarray]:
    poly = [np.asarray(point, dtype=np.float64) for point in points]
    lengths = [float(np.linalg.norm(b - a)) for a, b in zip(poly, poly[1:])]
    cumulative = np.asarray([0.0, *np.cumsum(lengths)], dtype=np.float64)
    target = min(max(float(fraction), 0.0), 1.0) * cumulative[-1]
    idx = min(len(lengths) - 1, int(np.searchsorted(cumulative, target, side="right") - 1))
    t = 0.0 if lengths[idx] == 0 else (target - cumulative[idx]) / lengths[idx]
    position = poly[idx] + t * (poly[idx + 1] - poly[idx])
    tangent = poly[idx + 1] - poly[idx]
    return position, tangent


def generate(scene: Path, dataset_config: Path, navmesh: Path, scene_id: str,
             sequence_id: str, seed: int, sample_count: int = 72) -> dict:
    import habitat_sim  # type: ignore
    import magnum as mn  # type: ignore

    cfg = habitat_sim.SimulatorConfiguration()
    cfg.scene_id = str(scene.resolve())
    cfg.scene_dataset_config_file = str(dataset_config.resolve())
    cfg.create_renderer = False
    cfg.requires_textures = False
    sim = habitat_sim.Simulator(habitat_sim.Configuration(cfg, [habitat_sim.AgentConfiguration()]))
    try:
        if not sim.pathfinder.load_nav_mesh(str(navmesh.resolve())):
            raise RuntimeError("failed to load configured ReplicaCAD navmesh")
        sim.pathfinder.seed(int(seed))
        samples: list[np.ndarray] = []
        for _ in range(sample_count):
            p = np.asarray(sim.pathfinder.get_random_navigable_point(), dtype=np.float64)
            if np.isfinite(p).all() and not any(np.linalg.norm(p - q) < 0.8 for q in samples):
                samples.append(p)
        if len(samples) < 2:
            raise RuntimeError("navmesh yielded fewer than two distinct navigable points")

        candidates: list[tuple[float, np.ndarray, np.ndarray, list]] = []
        for i, start in enumerate(samples):
            for goal in samples[i + 1:]:
                path = habitat_sim.ShortestPath()
                path.requested_start = mn.Vector3(*[float(x) for x in start])
                path.requested_end = mn.Vector3(*[float(x) for x in goal])
                if not sim.pathfinder.find_path(path):
                    continue
                distance = float(path.geodesic_distance)
                points = [np.asarray(row, dtype=np.float64) for row in path.points]
                if distance >= 4.0 and len(points) >= 2:
                    candidates.append((distance, start, goal, points))
        if not candidates:
            raise RuntimeError("navmesh has no connected route of at least 4 m")
        # Choose a short cross-map path: closest to 7 m, then deterministic tie-break.
        distance, start, goal, points = min(
            candidates, key=lambda row: (abs(min(row[0], 9.0) - 7.0), row[0], tuple(row[1]), tuple(row[2])))

        def yaw_for(tangent: np.ndarray) -> float:
            dx, dz = float(tangent[0]), float(tangent[2])
            return math.atan2(-dx, -dz)

        fractions = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        route = []

        def add(index: int, fraction: float, reverse: bool, turn: bool, label: str) -> None:
            pos, tangent = path_sample(points, fraction)
            if reverse:
                tangent = -tangent
            yaw = yaw_for(tangent) + (math.pi / 2 if turn else 0.0)
            route.append({"position_habitat": [round(float(x), 6) for x in pos],
                          "yaw_rad": round(float(yaw), 6), "label": label})

        add(0, 0.0, False, False, "start_face_route")
        add(1, 0.0, False, True, "start_revisit_turn")
        for fraction, label in ((0.2, "route_20pct"), (0.4, "route_40pct"), (0.6, "route_60pct")):
            add(len(route), fraction, False, False, label)
        add(len(route), 0.6, False, True, "route_60pct_revisit_turn")
        add(len(route), 0.8, False, False, "route_80pct")
        add(len(route), 1.0, False, False, "route_end")
        add(len(route), 1.0, False, True, "route_end_revisit_turn")
        add(len(route), 0.8, True, False, "return_80pct")
        add(len(route), 0.4, True, False, "return_40pct")
        add(len(route), 0.0, True, False, "return_start")

        return {
            "schema_version": 1, "scene_id": scene_id,
            "scene_provenance": "Habitat-Sim ReplicaCAD dataset v1.6 local scene instance",
            "scene_license_note": "ReplicaCAD dataset CC BY 4.0; local experiment only",
            "scene_dataset_config": str(dataset_config), "scene_instance": str(scene),
            "navmesh_file": str(navmesh), "sequence_id": sequence_id,
            "resolution_px": [160, 120], "horizontal_fov_deg": 90.0,
            "sensor_position_habitat": [0.0, 1.5, 0.0],
            "sensor_orientation_habitat_rad": [0.0, 0.0, 0.0],
            "calibration_id": "habitat-rgbd-stage-v1", "min_depth_m": 0.1,
            "max_depth_m": 10.0, "simulation_step_s": 0.1,
            "gpu_device_id": -1, "require_navigable": True,
            "route_policy": "fixed short route selected only from navmesh free-space connectivity; includes same-position yaw revisits; no GT semantic or predictor input",
            "route_generation": {
                "method": "seeded random navmesh sampling + shortest path; candidate selection nearest 7 m geodesic",
                "seed": seed, "sample_count": sample_count, "valid_candidate_count": len(candidates),
                "geodesic_distance_m": round(distance, 4),
                "start_position_habitat": [round(float(x), 6) for x in start],
                "goal_position_habitat": [round(float(x), 6) for x in goal],
                "scene_sha256": sha(scene), "navmesh_sha256": sha(navmesh),
                "selection_inputs": ["scene navmesh geometry"],
                "semantic_or_predictor_inputs": False,
            },
            "route": route,
        }
    finally:
        sim.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--dataset-config", type=Path, required=True)
    parser.add_argument("--navmesh", type=Path, required=True)
    parser.add_argument("--scene-id", required=True)
    parser.add_argument("--sequence-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite route config")
    result = generate(args.scene, args.dataset_config, args.navmesh, args.scene_id,
                      args.sequence_id, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "route_generated", "scene_id": args.scene_id,
                      "sequence_id": args.sequence_id, "poses": len(result["route"]),
                      "geodesic_distance_m": result["route_generation"]["geodesic_distance_m"],
                      "route_sha256": sha(args.output), "output": str(args.output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
