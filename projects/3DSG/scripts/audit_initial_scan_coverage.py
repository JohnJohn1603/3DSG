#!/usr/bin/env python3
"""Check whether saved initial-scan RGB-D samples raster floor rays through a cell."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.contracts import GridSnapshot
from scripts.profile_saved_navmaps import _load_snapshot


def _cell_floor_ray_hits(depth: np.ndarray, metadata: dict[str, object],
                         snapshot: GridSnapshot, stride: int,
                         target_cell: tuple[int, int]) -> tuple[int, int]:
    intr = metadata["intrinsics"]
    T = np.asarray(metadata["T_world_camera"], dtype=np.float64)
    origin = T[:3, 3]
    rows, cols = np.meshgrid(
        np.arange(0, depth.shape[0], stride, dtype=np.int32),
        np.arange(0, depth.shape[1], stride, dtype=np.int32),
        indexing="ij",
    )
    z_all = np.asarray(depth[rows, cols], dtype=np.float64)
    valid = (np.isfinite(z_all) & (z_all >= float(metadata["depth_min_m"])))
    valid &= z_all <= float(metadata["depth_max_m"])
    u = cols[valid].astype(np.float64)
    v = rows[valid].astype(np.float64)
    z = z_all[valid]
    camera = np.vstack((
        (u - float(intr["cx"])) * z / float(intr["fx"]),
        (v - float(intr["cy"])) * z / float(intr["fy"]),
        z, np.ones_like(z),
    ))
    xyz = (T @ camera)[:3].T
    floor_seen = np.abs(xyz[:, 2] - float(metadata["floor_z_m"])) <= 0.08

    res = snapshot.resolution_m
    origin_xy = np.array((snapshot.origin_x_m, snapshot.origin_y_m))
    ends = np.floor((xyz[:, :2] - origin_xy) / res).astype(np.int64)
    start = np.floor((origin[:2] - origin_xy) / res).astype(np.int64)
    delta_col = ends[:, 0] - start[0]
    delta_row = ends[:, 1] - start[1]
    steps = np.maximum(np.abs(delta_col), np.abs(delta_row))
    eligible = floor_seen & (steps <= 599)  # NavMapper max_ray_cells=600.
    if not np.any(eligible):
        return int(np.count_nonzero(floor_seen)), 0

    delta_col = delta_col[eligible]
    delta_row = delta_row[eligible]
    steps = steps[eligible]
    hit_count = 0
    for chunk_start in range(0, len(steps), 512):
        chunk_end = min(len(steps), chunk_start + 512)
        chunk_steps = steps[chunk_start:chunk_end]
        max_steps = int(np.max(chunk_steps))
        positions = np.arange(max_steps + 1, dtype=np.int64)[None, :]
        denominator = np.maximum(chunk_steps, 1)[:, None]
        valid_path = positions <= chunk_steps[:, None]
        ray_rows = start[1] + np.rint(
            delta_row[chunk_start:chunk_end, None] * positions / denominator
        ).astype(np.int64)
        ray_cols = start[0] + np.rint(
            delta_col[chunk_start:chunk_end, None] * positions / denominator
        ).astype(np.int64)
        hit_count += int(np.count_nonzero(
            valid_path & (ray_rows == target_cell[0]) & (ray_cols == target_cell[1])))
    return int(np.count_nonzero(floor_seen)), hit_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="saved run directory")
    parser.add_argument("--target-xy", nargs=2, type=float, default=(0.875, 4.575),
                        metavar=("X", "Y"), help="world-space center of target cell")
    parser.add_argument("--stride", type=int, default=None,
                        help="pixel sampling stride; defaults to the effective run config")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    run_dir = args.run_dir
    audit_path = run_dir / "diagnostics/initial_scan/manifest.json"
    if not audit_path.is_file():
        parser.error(f"missing initial-scan manifest: {audit_path}")
    manifest = json.loads(audit_path.read_text(encoding="utf-8"))
    snapshot, _, meta = _load_snapshot(run_dir)
    event_path = run_dir / "events.jsonl"
    integrated_frame_ids: set[str] = set()
    if event_path.is_file():
        for line in event_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "navigation_tick" and event.get("map_as_of_frame_id"):
                integrated_frame_ids.add(str(event["map_as_of_frame_id"]))
    target = snapshot.world_to_cell(float(args.target_xy[0]), float(args.target_xy[1]))
    if target is None:
        parser.error("target XY is outside the final NavMap")
    stride = int(meta["nav"].get("depth_stride", 4) if args.stride is None else args.stride)
    if stride < 1:
        parser.error("--stride must be positive")

    results = []
    total_floor_rays = 0
    total_target_hits = 0
    matched_map_frames = 0
    for sample in manifest.get("samples", []):
        depth = np.load(run_dir / "diagnostics/initial_scan" / sample["depth_file"],
                        mmap_mode="r")
        floor_rays, hits = _cell_floor_ray_hits(depth, sample, snapshot, stride, target)
        total_floor_rays += floor_rays
        total_target_hits += hits
        frame_was_integrated = sample.get("frame_id") in integrated_frame_ids
        matched_map_frames += int(frame_was_integrated)
        results.append({
            "frame_id": sample["frame_id"],
            "frame_index": sample["frame_index"],
            "yaw_bin": sample["yaw_bin"],
            "yaw_deg": round(float(sample["robot_pose_xy_yaw"][2]) * 180.0 / np.pi, 3),
            "nav_revision": sample["nav_revision"],
            "map_integrated_during_episode": frame_was_integrated,
            "sampled_floor_rays": floor_rays,
            "floor_rays_through_target_cell": hits,
        })

    target_world = snapshot.cell_to_world(*target)
    output = {
        "schema": "initial-scan-cell-coverage-audit/v1",
        "run_id": manifest.get("run_id"),
        "audit_status": manifest.get("status"),
        "sample_count": len(results),
        "yaw_bin_count": manifest.get("yaw_bin_count"),
        "depth_stride": stride,
        "target_cell": list(target),
        "target_cell_center_xy": list(target_world),
        "target_final_state": int(snapshot.states[target]),
        "target_final_floor_support": int(snapshot.floor_support[target]),
        "sampled_floor_ray_count": total_floor_rays,
        "sampled_floor_ray_hits": total_target_hits,
        "sample_frames_integrated_exactly": matched_map_frames,
        "samples": results,
        "interpretation": (
            "This audits only retained yaw-sector samples; zero hits do not prove that "
            "unsaved initial-scan frames missed the cell."
        ),
    }
    encoded = json.dumps(output, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(f"WROTE {args.output}")
    else:
        print(encoded)
    print(f"target={target} final_floor_support={snapshot.floor_support[target]} "
          f"sampled_floor_rays={total_floor_rays} hits={total_target_hits} "
          f"integrated_during_episode={matched_map_frames}/{len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
