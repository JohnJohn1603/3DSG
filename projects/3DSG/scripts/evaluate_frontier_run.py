#!/usr/bin/env python3
"""Summarize the 120 s frontier movement gate from an immutable run journal."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys


def evaluate(run_dir: Path, *, min_net_displacement_m: float = 0.2,
             min_reached_viewpoints: int = 2,
             min_spatially_distinct_viewpoints: int = 2,
             min_viewpoint_separation_m: float = 0.5,
             point_quantization_m: float = 0.05) -> dict:
    events_path = run_dir / "events.jsonl"
    summary_path = run_dir / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    ticks = [event for event in events if event.get("event") == "navigation_tick"]
    if not ticks:
        raise ValueError(f"run has no navigation_tick events: {events_path}")
    start = ticks[0].get("robot_pose_xy_yaw")
    end = ticks[-1].get("robot_pose_xy_yaw")
    if (not isinstance(start, list) or len(start) < 2
            or not isinstance(end, list) or len(end) < 2):
        raise ValueError("run journal does not contain start/end robot poses")
    net = math.dist((float(start[0]), float(start[1])),
                    (float(end[0]), float(end[1])))

    poses = [event.get("robot_pose_xy_yaw") for event in ticks]
    valid_pose = [
        (float(pose[0]), float(pose[1]))
        if isinstance(pose, list) and len(pose) >= 2
        and all(math.isfinite(float(value)) for value in pose[:2]) else None
        for pose in poses
    ]
    valid_xy = [pose for pose in valid_pose if pose is not None]
    path_length = sum(
        math.dist(first, second)
        for first, second in zip(valid_pose, valid_pose[1:])
        if first is not None and second is not None
    )
    path_efficiency = net / path_length if path_length > 1e-9 else 0.0

    effective_path = run_dir / "effective_config.json"
    effective = json.loads(effective_path.read_text(encoding="utf-8")) if effective_path.exists() else {}
    scene = effective.get("scene_config", {})
    profile = effective.get("navigation_profile_effective", {})
    radius = float(scene.get("agent", {}).get("navmesh_baked_radius_m", 0.0))
    resolution = float(profile.get("navigation", {}).get("grid_resolution_m", 0.0))
    reach_tolerance = max(radius, resolution / math.sqrt(2.0), point_quantization_m)

    map_samples = []
    for event in ticks:
        counts = event.get("map_cell_counts")
        if isinstance(counts, dict) and isinstance(counts.get("free"), int):
            map_samples.append({
                "frame_index": int(event["frame_index"]),
                "free": int(counts["free"]),
                "occupied": int(counts.get("occupied", 0)),
                "unknown": int(counts.get("unknown", 0)),
            })
    frontier_counts = [int(event["frontier_count"]) for event in ticks
                       if isinstance(event.get("frontier_count"), int)]

    reached: dict[tuple[int, int], dict] = {}
    for event in ticks:
        command = event.get("command") or {}
        viewpoint = event.get("goal_viewpoint_xy")
        pose = event.get("robot_pose_xy_yaw")
        reached_reason = (command.get("reason") == "goal_reached"
                          or event.get("decision_reason") == "frontier_viewpoint_reached")
        if (not reached_reason or not event.get("goal_id")
                or not isinstance(viewpoint, list) or len(viewpoint) != 2
                or not isinstance(pose, list) or len(pose) < 2):
            continue
        point = (float(viewpoint[0]), float(viewpoint[1]))
        distance = math.dist(point, (float(pose[0]), float(pose[1])))
        # The command reason is generated only after the runtime's reached
        # threshold. This guard catches malformed journals without substituting
        # a looser evaluation distance.
        if distance > reach_tolerance:
            continue
        key = (round(point[0] / point_quantization_m),
               round(point[1] / point_quantization_m))
        reached.setdefault(key, {
            "viewpoint_xy_m": list(point), "goal_id": str(event["goal_id"]),
            "frame_index": int(event["frame_index"]),
            "robot_distance_to_viewpoint_m": distance,
        })

    distinct_reached = list(reached.values())
    viewpoint_separations = [
        math.dist(first["viewpoint_xy_m"], second["viewpoint_xy_m"])
        for index, first in enumerate(distinct_reached)
        for second in distinct_reached[index + 1:]
    ]
    spatially_distinct: list[dict] = []
    for item in distinct_reached:
        if all(math.dist(item["viewpoint_xy_m"], other["viewpoint_xy_m"])
               >= min_viewpoint_separation_m
               for other in spatially_distinct):
            spatially_distinct.append(item)
    goal_ids = [str(event["goal_id"]) for event in ticks if event.get("goal_id")]
    goal_switches = sum(first != second for first, second in zip(goal_ids, goal_ids[1:]))
    stale_reasons = {"nav_stale", "depth_stale"}
    stale_command_ticks = sum((event.get("command") or {}).get("reason") in stale_reasons
                              for event in ticks)
    wait_policy_ticks = sum(event.get("decision_state") == "WAIT" for event in ticks)
    total_tick_latencies = [
        float((event.get("stage_latency_s") or {}).get("total_tick"))
        for event in ticks
        if isinstance((event.get("stage_latency_s") or {}).get("total_tick"), (int, float))
        and math.isfinite(float((event.get("stage_latency_s") or {}).get("total_tick")))
    ]
    result = {
        "schema": "frontier-movement-gate-report/v1",
        "run_id": summary.get("run_id", run_dir.name),
        "elapsed_s": float(summary.get("elapsed_exploration_s", 0.0)),
        "frame_count": int(summary.get("frame_count", len(ticks))),
        "start_xy_m": [float(start[0]), float(start[1])],
        "end_xy_m": [float(end[0]), float(end[1])],
        "net_displacement_m": net,
        "path_length_m": path_length,
        "path_efficiency_net_over_path": path_efficiency,
        "goal_id_switches": goal_switches,
        "exploration_progress": {
            "map_resolution_m": resolution if resolution > 0 else None,
            "known_free_cell_count": {
                "initial": map_samples[0]["free"] if map_samples else None,
                "final": map_samples[-1]["free"] if map_samples else None,
                "peak": max((sample["free"] for sample in map_samples), default=None),
                "net_change": (map_samples[-1]["free"] - map_samples[0]["free"]
                               if map_samples else None),
                "samples": len(map_samples),
            },
            "free_cell_area_proxy_m2": ({
                "initial": map_samples[0]["free"] * resolution ** 2,
                "final": map_samples[-1]["free"] * resolution ** 2,
                "peak": max(sample["free"] for sample in map_samples) * resolution ** 2,
            } if map_samples and resolution > 0 else None),
            "frontier_count": {
                "initial": frontier_counts[0] if frontier_counts else None,
                "final": frontier_counts[-1] if frontier_counts else None,
                "peak": max(frontier_counts) if frontier_counts else None,
            },
            "coverage_note": "Free-cell area is a NavMap count proxy, not ground-truth coverage; cells can change classification as evidence accumulates.",
        },
        "diagnostics": {
            "valid_pose_count": len(valid_xy),
            "stale_command_ticks": stale_command_ticks,
            "stale_command_fraction": stale_command_ticks / len(ticks),
            "policy_wait_ticks": wait_policy_ticks,
            "policy_wait_fraction": wait_policy_ticks / len(ticks),
            "total_tick_latency_p50_s": (
                statistics.median(total_tick_latencies) if total_tick_latencies else None
            ),
            "total_tick_latency_p95_s": (
                sorted(total_tick_latencies)[math.ceil(0.95 * len(total_tick_latencies)) - 1]
                if total_tick_latencies else None
            ),
            "minimum_reached_viewpoint_separation_m": (
                min(viewpoint_separations) if viewpoint_separations else None
            ),
            "minimum_viewpoint_separation_m": min_viewpoint_separation_m,
            "greedy_spatially_distinct_viewpoint_count": len(spatially_distinct),
            # Retain the existing key for older report readers.
            "greedy_spatially_distinct_viewpoints_at_0_5m": len(spatially_distinct),
        },
        "reach_tolerance_m": reach_tolerance,
        "move_forward_actions": sum(event.get("executed_action") == "move_forward"
                                     for event in ticks),
        "collision_or_filter_block_count": int(summary.get("collision_or_filter_block_count", 0)),
        "nav_stale_ticks": sum((event.get("command") or {}).get("reason") == "nav_stale"
                                for event in ticks),
        "depth_stale_ticks": sum((event.get("command") or {}).get("reason") == "depth_stale"
                                  for event in ticks),
        "policy_nav_stale_ticks": sum(event.get("decision_reason") == "nav_stale"
                                       for event in ticks),
        "policy_depth_stale_ticks": sum(event.get("decision_reason") == "depth_stale"
                                         for event in ticks),
        "unique_reached_viewpoints": distinct_reached,
        "gate": {
            "name": "movement",
            "minimum_net_displacement_m": min_net_displacement_m,
            "minimum_reached_viewpoints": min_reached_viewpoints,
            "passed": (net > min_net_displacement_m
                       and len(distinct_reached) >= min_reached_viewpoints),
        },
        "exploration_quality_gate": {
            "name": "spatial_coverage",
            "minimum_spatially_distinct_viewpoints": min_spatially_distinct_viewpoints,
            "minimum_viewpoint_separation_m": min_viewpoint_separation_m,
            "spatially_distinct_viewpoints": len(spatially_distinct),
            "path_length_m": path_length,
            "known_free_cell_net_change_proxy": (
                map_samples[-1]["free"] - map_samples[0]["free"] if map_samples else None
            ),
            "coverage_proxy_is_ground_truth": False,
            "passed": len(spatially_distinct) >= min_spatially_distinct_viewpoints,
        },
        "overall_gate": {
            "passed": (net > min_net_displacement_m
                       and len(distinct_reached) >= min_reached_viewpoints
                       and len(spatially_distinct) >= min_spatially_distinct_viewpoints),
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="immutable run artifact directory")
    parser.add_argument("--min-net-displacement-m", type=float, default=0.2)
    parser.add_argument("--min-reached-viewpoints", type=int, default=2)
    parser.add_argument("--min-spatially-distinct-viewpoints", type=int, default=2)
    parser.add_argument("--min-viewpoint-separation-m", type=float, default=0.5)
    parser.add_argument("--output", type=Path, default=None,
                        help="optional JSON report path; defaults to stdout")
    args = parser.parse_args()
    if (not math.isfinite(args.min_net_displacement_m)
            or not math.isfinite(args.min_viewpoint_separation_m)
            or args.min_net_displacement_m < 0 or args.min_reached_viewpoints < 1
            or args.min_spatially_distinct_viewpoints < 1
            or args.min_viewpoint_separation_m <= 0):
        parser.error("gate thresholds must be finite and non-negative")
    report = evaluate(args.run_dir, min_net_displacement_m=args.min_net_displacement_m,
                      min_reached_viewpoints=args.min_reached_viewpoints,
                      min_spatially_distinct_viewpoints=args.min_spatially_distinct_viewpoints,
                      min_viewpoint_separation_m=args.min_viewpoint_separation_m)
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
        print(args.output)
    else:
        sys.stdout.write(encoded)
    return 0 if report["overall_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
