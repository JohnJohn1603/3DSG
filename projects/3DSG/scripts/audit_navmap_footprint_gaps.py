#!/usr/bin/env python3
"""Offline audit of known-free but footprint-blocked robot neighborhoods.

This script reads saved NavMap artifacts only. Counterfactual single-cell
results are diagnostic and must never be used as traversability evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.contracts import GridSnapshot, GridState
from exploration.frontier import FrontierExtractor, _flood, _neighbors
from exploration.planner import AStarPlanner, PlannerConfig
from scripts.profile_saved_navmaps import _load_snapshot


def _within_footprint_offsets(snapshot: GridSnapshot, radius_m: float) -> tuple[tuple[int, int], ...]:
    cells = int(np.ceil(radius_m / snapshot.resolution_m))
    return tuple(
        (dr, dc)
        for dr in range(-cells, cells + 1)
        for dc in range(-cells, cells + 1)
        if (dr * dr + dc * dc) * snapshot.resolution_m**2 <= radius_m**2
    )


def _audit(run_dir: Path) -> dict[str, object]:
    snapshot, live, meta = _load_snapshot(run_dir)
    nav = live.get("navigation", {})
    pose_values = nav.get("robot_pose", [])
    if len(pose_values) < 2:
        raise ValueError("live state has no robot pose")
    robot_xy = float(pose_values[0]), float(pose_values[1])
    start = snapshot.world_to_cell(*robot_xy)
    if start is None:
        raise ValueError("robot pose is outside saved NavMap")

    radius = float(meta["scene"]["agent"]["navmesh_baked_radius_m"])
    margin = float(meta["nav"].get("inflation_margin_m", 0.0))
    planner = AStarPlanner(PlannerConfig(agent_radius_m=radius, inflation_margin_m=margin))
    traversable = planner._traversable(snapshot)
    if snapshot.states[start] == GridState.FREE:
        # Mirror the Habitat host's validated current-pose start exception.
        traversable = traversable.copy()
        traversable[start] = True
    reachable = _flood(traversable, start, _neighbors(8), prevent_corner_cut=True)

    offsets = _within_footprint_offsets(snapshot, radius + margin)
    blocked_cells: dict[tuple[int, int], set[tuple[int, int]]] = {}
    cardinal = ((-1, 0, "north"), (1, 0, "south"), (0, -1, "west"), (0, 1, "east"))
    neighbor_results: list[dict[str, object]] = []
    height, width = snapshot.shape
    for dr, dc, label in cardinal:
        cell = start[0] + dr, start[1] + dc
        if not (0 <= cell[0] < height and 0 <= cell[1] < width):
            neighbor_results.append({"direction": label, "inside_map": False})
            continue
        blockers = {
            (cell[0] + off_r, cell[1] + off_c)
            for off_r, off_c in offsets
            if 0 <= cell[0] + off_r < height
            and 0 <= cell[1] + off_c < width
            and snapshot.states[cell[0] + off_r, cell[1] + off_c] != GridState.FREE
        }
        for blocker in blockers:
            blocked_cells.setdefault(blocker, set()).add(cell)
        neighbor_results.append({
            "direction": label,
            "cell": list(cell),
            "state": GridState(int(snapshot.states[cell])).name.lower(),
            "footprint_traversable": bool(planner._traversable(snapshot)[cell]),
            "blocker_count": len(blockers),
            "unknown_blockers": int(sum(snapshot.states[pos] == GridState.UNKNOWN for pos in blockers)),
            "occupied_blockers": int(sum(snapshot.states[pos] == GridState.OCCUPIED for pos in blockers)),
        })

    counterfactuals = []
    for cell, blocks in sorted(blocked_cells.items()):
        state = GridState(int(snapshot.states[cell]))
        if state != GridState.UNKNOWN:
            continue
        evidence = {
            "floor_support": int(snapshot.floor_support[cell]),
            "clearance_evidence": int(snapshot.clearance_evidence[cell]),
            "occupancy_log_odds": float(snapshot.occupancy_log_odds[cell]),
        }
        if evidence["floor_support"] != 0 or evidence["clearance_evidence"] == 0:
            continue

        # Deliberate what-if only: leave the saved map untouched and do not
        # infer that these conditions certify a real floor surface.
        hypothetical_states = snapshot.states.copy()
        hypothetical_states[cell] = GridState.FREE
        hypothetical = replace(snapshot, revision=snapshot.revision + 1,
                               states=hypothetical_states)
        hypothetical_planner = AStarPlanner(
            PlannerConfig(agent_radius_m=radius, inflation_margin_m=margin))
        hypothetical_mask = hypothetical_planner._traversable(hypothetical).copy()
        hypothetical_mask[start] = snapshot.states[start] == GridState.FREE
        after = int(_flood(hypothetical_mask, start, _neighbors(8),
                            prevent_corner_cut=True).sum())
        before = int(reachable.sum())
        counterfactuals.append({
            "cell": list(cell),
            "world_xy": list(snapshot.cell_to_world(*cell)),
            "blocks_neighbor_cells": [list(item) for item in sorted(blocks)],
            **evidence,
            "hypothetical_reachable_cells_if_marked_free": after,
            "reachable_cells_before": before,
            "warning": "diagnostic counterfactual only; not a free-space observation",
        })

    extractor = FrontierExtractor(agent_radius_m=radius, inflation_margin_m=margin)
    frontier_set = extractor.extract(snapshot, robot_xy, allow_validated_start_anchor=True)
    viewpoints = [list(item.viewpoint_cell) for item in frontier_set.frontiers]
    return {
        "run_id": run_dir.name,
        "map_revision": snapshot.revision,
        "map_frame_id": snapshot.as_of_frame_id,
        "robot_xy": list(robot_xy),
        "robot_cell": list(start),
        "robot_state": GridState(int(snapshot.states[start])).name.lower(),
        "robot_footprint_traversable_without_anchor": bool(planner._traversable(snapshot)[start]),
        "known_free_cells": int(np.count_nonzero(snapshot.states == GridState.FREE)),
        "reachable_with_validated_start_anchor": int(reachable.sum()),
        "cardinal_neighbors": neighbor_results,
        "unsupported_floor_cells_blocking_neighbor_footprints": counterfactuals,
        "frontier_count": len(frontier_set.frontiers),
        "unique_frontier_viewpoints": len({tuple(point) for point in viewpoints}),
        "frontier_viewpoints": viewpoints,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--run-id", action="append", default=None,
                        help="run ID filter; may be repeated")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    run_dirs = sorted(args.runs_root.iterdir())
    selected = set(args.run_id or [])
    reports = []
    for run_dir in run_dirs:
        if selected and run_dir.name not in selected:
            continue
        if not run_dir.is_dir() or not (run_dir / "navmap_final.npz").is_file():
            continue
        if not (run_dir / "live/state.json").is_file():
            continue
        try:
            report = _audit(run_dir)
        except Exception as exc:
            print(f"SKIP {run_dir.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        reports.append(report)
        print(
            f"{run_dir.name}: reachable={report['reachable_with_validated_start_anchor']} "
            f"free={report['known_free_cells']} frontiers={report['frontier_count']} "
            f"unique_viewpoints={report['unique_frontier_viewpoints']} "
            f"floor-gap blockers={len(report['unsupported_floor_cells_blocking_neighbor_footprints'])}"
        )

    output = {"schema": "navmap-footprint-gap-audit/v1", "runs": reports}
    encoded = json.dumps(output, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(f"WROTE {args.output}")
    else:
        print(encoded)
    return 0 if reports else 2


if __name__ == "__main__":
    raise SystemExit(main())
