#!/usr/bin/env python3
"""Evaluate source/extended M4 profiles against authored apartment geometry.

The reference rectangles and door gaps are evaluation-only. They are never
passed to M3, DuDe, M5, M8 or SceneGraphBuilder. Metrics are restricted to
known M3 cells; unknown overlap is reported separately as a policy metric.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.path import Path as PolygonPath
from matplotlib.patches import Polygon, Rectangle


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _corners(room: dict) -> np.ndarray:
    sx, sy = (float(x) for x in room["size_xy_local"])
    angle = float(room["rotation_rad"])
    local = np.array([[-sx / 2, -sy / 2], [sx / 2, -sy / 2],
                      [sx / 2, sy / 2], [-sx / 2, sy / 2]], dtype=float)
    rotation = np.array([[math.cos(angle), -math.sin(angle)],
                         [math.sin(angle), math.cos(angle)]])
    return local @ rotation.T + np.asarray(room["center_xy"], dtype=float)


def _grid(root: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    index = _json(root / "m3_m4_grid_trace" / "index.json")
    last = index["frames"][-1]
    trace_root = root / "m3_m4_grid_trace"
    metadata = _json(trace_root / last["metadata_file"])
    array_path = trace_root / last["array_file"]
    with np.load(array_path, allow_pickle=False) as arrays:
        values = arrays["values"].copy()
    rows, cols = values.shape
    resolution = float(metadata["resolution_m"])
    origin_x, origin_y = (float(x) for x in metadata["origin_xy"])
    xx, yy = np.meshgrid(
        origin_x + (np.arange(cols) + 0.5) * resolution,
        origin_y + (np.arange(rows) + 0.5) * resolution,
    )
    return values, xx, yy, metadata


def _polygon_mask(points_x: np.ndarray, points_y: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    points = np.column_stack((points_x.ravel(), points_y.ravel()))
    return PolygonPath(polygon).contains_points(points).reshape(points_x.shape)


def _regions(metadata: dict) -> list[dict]:
    return metadata["m4"]["raw_regions"]


def _region_masks(regions: list[dict], xx: np.ndarray, yy: np.ndarray) -> list[np.ndarray]:
    return [_polygon_mask(xx, yy, np.asarray(region["outer"], dtype=float)) for region in regions]


def _room_metrics(room: dict, values: np.ndarray, xx: np.ndarray, yy: np.ndarray,
                  regions: list[dict]) -> dict:
    gt = _polygon_mask(xx, yy, _corners(room))
    known = values >= 0
    gt_known = gt & known
    total = int(gt.sum())
    known_count = int(gt_known.sum())
    free_count = int((gt & (values == 0)).sum())
    occupied_count = int((gt & (values > 0)).sum())
    unknown_count = int((gt & (values < 0)).sum())
    candidates = []
    for region, mask in zip(regions, _region_masks(regions, xx, yy)):
        pred_known = mask & known
        intersection = int((pred_known & gt_known).sum())
        union = int((pred_known | gt_known).sum())
        iou = intersection / union if union else 0.0
        candidates.append({
            "raw_id": int(region["raw_id"]),
            "area_m2": float(region.get("area_m2", 0.0)),
            "intersection_known_cells": intersection,
            "iou_known_cells": iou,
            "known_leak_cells_outside_gt": int((pred_known & ~gt).sum()),
            "unknown_overlap_cells": int((mask & ~known).sum()),
        })
    candidates.sort(key=lambda item: (-item["iou_known_cells"], item["raw_id"]))
    assigned = [item for item in candidates if item["intersection_known_cells"] >= max(3, int(0.02 * max(1, known_count)))]
    best = candidates[0] if candidates else None
    return {
        "name": room["name"],
        "source_def": room["source_def"],
        "grid_cells": total,
        "known_cells": known_count,
        "coverage_known_fraction": known_count / total if total else 0.0,
        "free_cells": free_count,
        "occupied_cells": occupied_count,
        "unknown_cells": unknown_count,
        "unknown_fraction": unknown_count / total if total else 0.0,
        "candidate_region_count": len(candidates),
        "assigned_region_count": len(assigned),
        "best_region": best,
        "oversegmentation_candidate_count": max(0, len(assigned) - 1),
        "undersegmentation_best_iou_below_0_25": bool(best and best["iou_known_cells"] < 0.25),
        "candidates": candidates[:12],
    }


def _door_metrics(door: dict, rooms: dict, values: np.ndarray, xx: np.ndarray, yy: np.ndarray,
                  regions: list[dict]) -> dict:
    cx, cy = (float(x) for x in door["center_xy"])
    width = float(door["width_m"])
    depth = float(door["depth_m"])
    door_mask = (np.abs(xx - cx) <= width / 2) & (np.abs(yy - cy) <= depth / 2)
    known = values >= 0
    corridor, other = (rooms[name] for name in door["connects"])
    corridor_mask = _polygon_mask(xx, yy, _corners(corridor))
    other_mask = _polygon_mask(xx, yy, _corners(other))
    side_a = corridor_mask & (yy > cy + depth / 2) & (yy <= cy + 1.0) & (np.abs(xx - cx) <= width / 2)
    side_b = other_mask & (yy < cy - depth / 2) & (yy >= cy - 1.0) & (np.abs(xx - cx) <= width / 2)
    spanning = []
    for region, mask in zip(regions, _region_masks(regions, xx, yy)):
        if int((mask & side_a).sum()) > 0 and int((mask & side_b).sum()) > 0:
            spanning.append(int(region["raw_id"]))
    return {
        "name": door["name"],
        "connects": door["connects"],
        "source_walls": door["source_walls"],
        "grid_cells": int(door_mask.sum()),
        "known_cells": int((door_mask & known).sum()),
        "coverage_known_fraction": float((door_mask & known).sum() / door_mask.sum()) if door_mask.any() else 0.0,
        "free_cells": int((door_mask & (values == 0)).sum()),
        "occupied_cells": int((door_mask & (values > 0)).sum()),
        "unknown_cells": int((door_mask & (values < 0)).sum()),
        "raw_regions_spanning_both_sides": spanning,
        "geometry_connectivity_observed": bool(spanning),
    }


def _profile(root: Path, evaluation: dict) -> tuple[dict, dict, dict]:
    values, xx, yy, metadata = _grid(root)
    regions = _regions(metadata)
    rooms = {room["name"]: room for room in evaluation["rooms"]}
    metrics = [_room_metrics(room, values, xx, yy, regions) for room in evaluation["rooms"]]
    doors = [_door_metrics(door, rooms, values, xx, yy, regions) for door in evaluation["doorways"]]
    clear = max(metrics, key=lambda item: (item["coverage_known_fraction"], -item["unknown_fraction"]))
    unknown = max(metrics, key=lambda item: (item["unknown_fraction"], -item["coverage_known_fraction"]))
    graph_manifest = _json(root / "graph" / "manifest.json")
    return {
        "path": str(root),
        "policy": graph_manifest.get("dude_preprocessing_policy", "source"),
        "map_revision": int(metadata["map_revision"]),
        "simulation_timestamp_s": float(metadata["simulation_timestamp_s"]),
        "m3_shape": list(values.shape),
        "m3_unknown_cells": int((values < 0).sum()),
        "m3_free_cells": int((values == 0).sum()),
        "m3_occupied_cells": int((values > 0).sum()),
        "m4_raw_region_count": len(regions),
        "rooms_final": graph_manifest["graph"].get("rooms_final"),
        "room_metrics": metrics,
        "doorway_metrics": doors,
        "selected_clear_room": clear["name"],
        "selected_unknown_room": unknown["name"],
        "selected_clear_room_metrics": clear,
        "selected_unknown_room_metrics": unknown,
    }, (values, xx, yy), metadata


def _plot(root: Path, evaluation: dict, output: Path, title: str) -> None:
    profile, arrays, metadata = _profile(root, evaluation)
    values, xx, yy = arrays
    fig, ax = plt.subplots(figsize=(12, 9))
    image = np.full(values.shape, 0.5, dtype=float)
    image[values == 0] = 1.0
    image[values > 0] = 0.0
    ax.imshow(image, origin="lower", extent=(xx.min(), xx.max(), yy.min(), yy.max()), cmap="gray", vmin=0, vmax=1, interpolation="nearest", alpha=0.75)
    for room in evaluation["rooms"]:
        corners = _corners(room)
        ax.add_patch(Polygon(corners, fill=False, linewidth=1.0, edgecolor="#2b6cb0", alpha=0.55))
        center = np.asarray(room["center_xy"])
        ax.text(center[0], center[1], room["name"], fontsize=7, color="#1f4e79", ha="center", va="center")
    for door in evaluation["doorways"]:
        cx, cy = door["center_xy"]
        ax.add_patch(Rectangle((cx - door["width_m"] / 2, cy - door["depth_m"] / 2), door["width_m"], door["depth_m"], fill=False, edgecolor="#d97706", linewidth=2.0))
    for region in _regions(metadata):
        outer = np.asarray(region["outer"], dtype=float)
        ax.plot(outer[:, 0], outer[:, 1], color="#d53f8c", linewidth=1.2, alpha=0.8)
    trajectory_path = root / "graph" / "trajectory.json"
    if trajectory_path.is_file():
        trajectory = _json(trajectory_path)
        poses = trajectory.get("poses", trajectory) if isinstance(trajectory, dict) else trajectory
        if poses:
            ax.plot([p["x_m"] for p in poses], [p["y_m"] for p in poses], color="#15803d", linewidth=1.4, label="trajectory")
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("world X (m)")
    ax.set_ylabel("world Y (m)")
    ax.set_title(f"{title} — revision {profile['map_revision']} — {profile['policy']}")
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(output, dpi=150)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--extended", type=Path, required=True)
    parser.add_argument("--evaluation-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluation = _json(args.evaluation_config)
    source, _source_arrays, _source_meta = _profile(args.source, evaluation)
    extended, _extended_arrays, _extended_meta = _profile(args.extended, evaluation)
    clear_name = source["selected_clear_room"]
    unknown_name = source["selected_unknown_room"]
    report = {
        "kind": "complete_apartment_source_vs_extended_evaluation",
        "scope": "same 276 captured packets; final M3 grid and raw M4 contours; authored floor/door geometry is evaluation-only",
        "reference": {
            "evaluation_config": str(args.evaluation_config),
            "source_world": evaluation["source_world"],
            "gt_passed_to_builder": False,
            "independent_room_gt": False,
            "authored_floor_reference": True,
            "room_count_not_accuracy": True,
        },
        "source": source,
        "extended": extended,
        "selected_windows": {
            "clear_room": clear_name,
            "unknown_region": unknown_name,
            "doorways": [door["name"] for door in evaluation["doorways"]],
        },
        "policy_metric": {
            "unknown_overlap_is_not_accuracy": True,
            "interpretation": "unknown-overlap measures whether a profile grows the free mask into unobserved M3 cells; it is a preprocessing-policy metric, not room correctness.",
        },
        "decision": "extended_unknown_boundary remains non-default until an independently verified room/door GT evaluation shows better observed geometry without damaging doorway connectivity.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    image_dir = args.output.parent
    _plot(args.source, evaluation, image_dir / "source_evaluation_overlay.png", "Source evaluation overlay")
    _plot(args.extended, evaluation, image_dir / "extended_evaluation_overlay.png", "Extended evaluation overlay")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
