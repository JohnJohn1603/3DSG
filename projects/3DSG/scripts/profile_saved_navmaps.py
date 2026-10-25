#!/usr/bin/env python3
"""Repeatable offline timing for saved NavMap snapshots and RGB-D tails.

This is a diagnostic benchmark, not a realtime guarantee. It measures:
* replaying each run's final RGB-D image into its saved map at stride 4 and 8;
* known-free and footprint-safe flood fills;
* full frontier extraction; and
* A* to up to eight reachable free-space destinations per snapshot.

Run from the repository root with either the system or Habitat Python:
    python scripts/profile_saved_navmaps.py --output runs/navmap_profile.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.contracts import CameraIntrinsics, FramePacket, GridSnapshot, GridState, Pose3
from exploration.frontier import FrontierExtractor, _flood, _neighbors
from exploration.nav_mapper import NavMapper, NavMapperConfig
from exploration.planner import AStarPlanner, PlannerConfig


def _percentiles(samples: list[float]) -> dict[str, float | int | None]:
    if not samples:
        return {"n": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "median_ms": 1000.0 * statistics.median(samples),
        "p95_ms": 1000.0 * ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)],
        "max_ms": 1000.0 * ordered[-1],
    }


def _timed(callable_, repeats: int) -> tuple[object, list[float]]:
    values: list[float] = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = callable_()
        values.append(time.perf_counter() - started)
    return result, values


def _load_snapshot(run_dir: Path) -> tuple[GridSnapshot, dict[str, object], dict[str, object]]:
    archive = np.load(run_dir / "navmap_final.npz")
    live = json.loads((run_dir / "live/state.json").read_text(encoding="utf-8"))
    effective = json.loads((run_dir / "effective_config.json").read_text(encoding="utf-8"))
    nav = live.get("navigation", {})
    scene = effective.get("scene_config", {})
    nav_profile = effective.get("navigation_profile_effective", {}).get("navigation", {})
    spawn = archive["spawn_center_xy"]
    spawn_xy = tuple(float(v) for v in spawn) if np.isfinite(spawn).all() else None
    frame_id = str(nav.get("as_of_frame_id", f"{run_dir.name}:snapshot"))
    frame_index = int(nav.get("as_of_frame_index", 0))
    snapshot = GridSnapshot(
        revision=int(nav.get("revision", 0)), as_of_frame_id=frame_id,
        as_of_frame_index=frame_index, host_monotonic_s=0.0,
        resolution_m=float(archive["resolution_m"]),
        origin_x_m=float(archive["origin_x_m"]), origin_y_m=float(archive["origin_y_m"]),
        states=archive["states"], floor_support=archive["floor_support"],
        clearance_evidence=archive["clearance_evidence"],
        spawn_validated=archive["spawn_validated"],
        occupancy_log_odds=archive["occupancy_log_odds"], spawn_center_xy=spawn_xy,
    )
    return snapshot, live, {"scene": scene, "nav": nav_profile}


def _mapper_replay(snapshot: GridSnapshot, live: dict[str, object], meta: dict[str, object],
                   run_dir: Path, stride: int, repeats: int,
                   ray_chunk_size: int = 512,
                   snapshots_out: list[GridSnapshot] | None = None,
                   phase_samples_out: list[dict[str, float]] | None = None) -> list[float]:
    camera = live["camera"]
    nav = live.get("navigation", {})
    scene = meta["scene"]
    profile = meta["nav"]
    intr = camera["intrinsics"]
    depth_path = run_dir / "live" / camera["depth_file"]
    depth = np.load(depth_path, mmap_mode="r")
    matrix = np.asarray(camera["T_world_camera"], dtype=np.float64)
    base_index = max(snapshot.as_of_frame_index, int(camera["frame_index"]))
    packet = FramePacket(
        run_id=str(live["run_id"]), scene_id=str(effective_scene_id(run_dir)),
        frame_id=f"{run_dir.name}:profile", frame_index=base_index + 1,
        simulator_time_s=float(camera.get("simulator_time_s", 0.0)),
        host_monotonic_s=float(camera.get("host_monotonic_s", 1.0)),
        depth_m=depth,
        intrinsics=CameraIntrinsics(float(intr["fx"]), float(intr["fy"]),
                                    float(intr["cx"]), float(intr["cy"]),
                                    int(intr["width"]), int(intr["height"])),
        T_world_camera=Pose3(matrix, str(camera.get("frame_id", "profile"))),
        floor_z_m=float(scene.get("spawn", {}).get("position_habitat_m", [0, 0])[1]),
        calibration_hash="saved-run-profile", depth_min_m=0.1, depth_max_m=10.0,
    )
    cfg = NavMapperConfig(resolution_m=snapshot.resolution_m, depth_stride=stride,
                          max_cells=2_000_000,
                          ray_chunk_size=ray_chunk_size,
                          agent_height_m=float(scene.get("agent", {}).get(
                              "navmesh_baked_height_m", 1.5)))
    times: list[float] = []
    for repeat in range(repeats):
        mapper = NavMapper(cfg, agent_radius_m=float(scene.get("agent", {}).get(
            "navmesh_baked_radius_m", 0.1)))
        mapper._origin_x, mapper._origin_y = snapshot.origin_x_m, snapshot.origin_y_m
        mapper._states = snapshot.states.copy()
        mapper._floor = snapshot.floor_support.copy()
        mapper._clearance = snapshot.clearance_evidence.copy()
        mapper._spawn_validated = snapshot.spawn_validated.copy()
        mapper._odds = snapshot.occupancy_log_odds.copy()
        mapper._revision = snapshot.revision
        mapper._last_frame_index = base_index
        mapper._last_frame_id = snapshot.as_of_frame_id
        mapper._last_host_time = snapshot.host_monotonic_s
        mapper._spawn_initialized = True
        mapper._spawn_center_xy = snapshot.spawn_center_xy
        phase_totals = {"bounds_expansion_s": 0.0, "ray_integration_s": 0.0,
                        "classification_s": 0.0}
        if phase_samples_out is not None:
            for method_name, phase_name in (
                    ("_ensure_bounds", "bounds_expansion_s"),
                    ("_integrate_rays", "ray_integration_s"),
                    ("_classify", "classification_s")):
                original = getattr(mapper, method_name)

                def timed_method(*method_args, _original=original, _phase=phase_name,
                                 **method_kwargs):
                    started = time.perf_counter()
                    try:
                        return _original(*method_args, **method_kwargs)
                    finally:
                        phase_totals[_phase] += time.perf_counter() - started

                setattr(mapper, method_name, timed_method)
        timed_packet = FramePacket(
            run_id=packet.run_id, scene_id=packet.scene_id, frame_id=f"{packet.frame_id}:{repeat}",
            frame_index=base_index + 1, simulator_time_s=packet.simulator_time_s,
            host_monotonic_s=packet.host_monotonic_s, depth_m=packet.depth_m,
            intrinsics=packet.intrinsics, T_world_camera=packet.T_world_camera,
            floor_z_m=packet.floor_z_m, calibration_hash=packet.calibration_hash,
            depth_min_m=packet.depth_min_m, depth_max_m=packet.depth_max_m,
        )
        started = time.perf_counter()
        result_snapshot = mapper.update(timed_packet)
        times.append(time.perf_counter() - started)
        if snapshots_out is not None:
            snapshots_out.append(result_snapshot)
        if phase_samples_out is not None:
            total = times[-1]
            phase_totals["projection_and_packet_prep_s"] = max(
                0.0, total - sum(phase_totals.values()))
            phase_totals["total_update_s"] = total
            phase_samples_out.append(phase_totals)
    return times


def effective_scene_id(run_dir: Path) -> str:
    config = json.loads((run_dir / "effective_config.json").read_text(encoding="utf-8"))
    return str(config.get("scene_config", {}).get("scene_id", run_dir.name))


def _profile_map(run_dir: Path, repeats: int) -> dict[str, object]:
    snapshot, live, meta = _load_snapshot(run_dir)
    radius = float(meta["scene"].get("agent", {}).get("navmesh_baked_radius_m", 0.1))
    margin = float(meta["nav"].get("inflation_margin_m", 0.025))
    extractor = FrontierExtractor(
        min_frontier_length_m=float(meta["nav"].get("min_frontier_length_m", 0.2)),
        agent_radius_m=radius, inflation_margin_m=margin,
        min_viewpoint_offset_m=float(meta["nav"].get("min_viewpoint_offset_m", 0.3)),
        max_viewpoint_offset_m=float(meta["nav"].get("max_viewpoint_offset_m", 0.6)),
    )
    start_xy = tuple(float(v) for v in live.get("navigation", {}).get("robot_pose", [])[:2])
    if len(start_xy) != 2 or snapshot.world_to_cell(*start_xy) is None:
        start_xy = snapshot.spawn_center_xy or (snapshot.origin_x_m, snapshot.origin_y_m)
    start_cell = snapshot.world_to_cell(*start_xy)
    known = snapshot.states == GridState.FREE
    traversable = extractor._traversable(snapshot).copy()
    if snapshot.states[start_cell] == GridState.FREE:
        traversable[start_cell] = True
    known_times: list[float] = []
    safe_times: list[float] = []
    for _ in range(repeats):
        t = time.perf_counter()
        _flood(known, start_cell, _neighbors(8))
        known_times.append(time.perf_counter() - t)
        t = time.perf_counter()
        _flood(traversable, start_cell, _neighbors(8), prevent_corner_cut=True)
        safe_times.append(time.perf_counter() - t)
    # Saved runs use Habitat's loaded, radius-eroded NavMesh to certify the
    # live robot pose. Mirror the host's narrow start-cell exception while
    # keeping all successors and diagonal side cells under the observed mask.
    frontier, frontier_times = _timed(
        lambda: extractor.extract(snapshot, start_xy, allow_validated_start_anchor=True),
        repeats,
    )

    # Select up to eight cells in the reachable component, ordered from far to
    # near to expose representative A* work instead of timing only a trivial goal.
    reachable = _flood(traversable, start_cell, _neighbors(8), prevent_corner_cut=True)
    rows, cols = np.nonzero(reachable)
    distances = np.hypot(rows - start_cell[0], cols - start_cell[1])
    if len(distances):
        order = np.argsort(distances)
        indices = np.unique(np.linspace(0, len(order) - 1, min(8, len(order)), dtype=int))
        goals = [(int(rows[order[i]]), int(cols[order[i]])) for i in indices if distances[order[i]] > 0]
    else:
        goals = []
    astar_times: list[float] = []
    planner = AStarPlanner(PlannerConfig(agent_radius_m=radius, inflation_margin_m=margin))
    for goal in goals:
        _, samples = _timed(
            lambda g=goal: planner.plan(
                snapshot, start_xy, g, allow_validated_start_anchor=True),
            repeats,
        )
        astar_times.extend(samples)

    states = snapshot.states
    return {
        "run_id": run_dir.name,
        "shape": list(snapshot.shape),
        "cells": int(states.size),
        "known_free_cells": int(np.count_nonzero(states == GridState.FREE)),
        "occupied_cells": int(np.count_nonzero(states == GridState.OCCUPIED)),
        "reachable_free_cells": frontier.reachable_free_cells,
        "raw_frontier_cells": frontier.raw_frontier_cells,
        "frontier_count": len(frontier.frontiers),
        "frontier_reason": frontier.reason,
        "timings": {
            "navmap_stride_4": _percentiles(_mapper_replay(snapshot, live, meta, run_dir, 4, repeats)),
            "navmap_stride_8": _percentiles(_mapper_replay(snapshot, live, meta, run_dir, 8, repeats)),
            "known_free_flood": _percentiles(known_times),
            "footprint_flood": _percentiles(safe_times),
            "frontier_extract": _percentiles(frontier_times),
            "astar_per_goal": _percentiles(astar_times),
        },
        "astar_goal_count": len(goals),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--minimum-snapshot-cells", type=int, default=1000)
    parser.add_argument("--run-id", action="append", default=None,
                        help="optional run ID filter; may be repeated")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")

    results = []
    for run_dir in sorted(args.runs_root.iterdir()):
        if args.run_id and run_dir.name not in set(args.run_id):
            continue
        if not run_dir.is_dir() or not (run_dir / "navmap_final.npz").is_file():
            continue
        if not (run_dir / "live/state.json").is_file() or not (run_dir / "effective_config.json").is_file():
            continue
        try:
            with np.load(run_dir / "navmap_final.npz") as archive:
                if int(archive["states"].size) < args.minimum_snapshot_cells:
                    continue
            result = _profile_map(run_dir, args.repeats)
        except Exception as exc:
            print(f"SKIP {run_dir.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        results.append(result)
        nav4 = result["timings"]["navmap_stride_4"]["median_ms"]
        nav8 = result["timings"]["navmap_stride_8"]["median_ms"]
        frontier_ms = result["timings"]["frontier_extract"]["median_ms"]
        print(f"{run_dir.name}: shape={result['shape']} free={result['known_free_cells']} "
              f"reachable={result['reachable_free_cells']} frontiers={result['frontier_count']} "
              f"map4={nav4:.1f}ms map8={nav8:.1f}ms extract={frontier_ms:.1f}ms "
              f"astar_goals={result['astar_goal_count']}")

    output = {
        "schema": "saved-navmap-profile/v1",
        "python": sys.version,
        "numpy": np.__version__,
        "repeats": args.repeats,
        "snapshot_count": len(results),
        "snapshots": results,
    }
    encoded = json.dumps(output, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(f"WROTE {args.output}")
    else:
        print(encoded)
    return 0 if results else 2


if __name__ == "__main__":
    raise SystemExit(main())
