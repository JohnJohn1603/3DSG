#!/usr/bin/env python3
"""Capture M2/M3/M4/M5 intermediates for the pinned two-room smoke; diagnostic-only."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch, Rectangle
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from standalone_sg.mapping import NativeRGBDMappingStage
from standalone_sg.regions.dude_client import DuDeConfig, DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor
from run_native_rgbd_smoke import DUDE_BINARY, M2_M3_BINARY, _rss_kib
from run_native_two_room_rgbd_sequence import (
    DOOR_Y, PARTITION_X, ROUTE_Y, WORLD_X_MAX, make_packet, scripted_route,
)

CHECKPOINTS = {13: "before_door", 16: "just_through_door", 22: "observe_room_2",
               34: "return_through_door", 43: "final_revisit"}


def json_region_summary(regions):
    return [{"raw_id": r.get("raw_id"), "area_m2": r.get("area_m2"),
             "adjacent_raw_ids": r.get("adjacent_raw_ids", []), "outer_world": r.get("outer", [])}
            for r in regions]


def m5_summary(result):
    return {"accepted": result.accepted, "stale": result.stale, "reason": result.reason,
            "request_revision": result.request_revision,
            "accepted_observation_revision": result.accepted_observation_revision,
            "current_regions": [{"persistent_id": r.persistent_id, "raw_id": r.raw_id,
                                  "source_grid_revision": r.source_grid_revision,
                                  "timestamp_s": r.timestamp_s, "area_m2": r.area_m2,
                                  "centroid_xy": list(r.centroid_xy), "outer_world": [list(p) for p in r.polygon.outer]}
                                 for r in result.current_regions],
            "retained_tracks": [{"persistent_id": t.persistent_id, "status": t.status,
                                 "miss_count": t.miss_count, "last_seen_s": t.last_seen_s,
                                 "source_grid_revision": t.last_source_grid_revision}
                                 for t in result.retained_tracks]}


def write_json_gzip(path: Path, value) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)


def _new_stderr_lines(old, new):
    old, new = list(old), list(new)
    for overlap in range(min(len(old), len(new)), -1, -1):
        if old[-overlap:] == new[:overlap] if overlap else True:
            return new[overlap:]
    return new


def _counts_and_heights(grid):
    h, w = grid.shape
    x = grid.origin_xy[0] + np.arange(w) * grid.resolution_m
    y = grid.origin_xy[1] + np.arange(h) * grid.resolution_m
    rows = []
    for label, x0, x1 in (("west_approach", 3.8, 4.2), ("partition_door_band", 4.2, 4.6), ("east_approach", 4.6, 5.0)):
        mx = (x >= x0) & (x < x1)
        for y_label, my in (("doorway", (np.abs(y) <= DOOR_Y[1])),
                            ("wall_jambs", (np.abs(y) > DOOR_Y[1]))):
            mask = my[:, None] & mx[None, :]
            values = grid.values[mask]
            floors, ceilings = grid.z_floor[mask], grid.z_ceiling[mask]
            floors = floors[np.isfinite(floors)]
            ceilings = ceilings[np.isfinite(ceilings)]
            rows.append({"x_band": label, "y_band": y_label,
                         "cells": int(mask.sum()), "unknown": int(np.count_nonzero(values < 0)),
                         "free": int(np.count_nonzero(values == 0)),
                         "occupied_score_positive": int(np.count_nonzero(values > 0)),
                         "occupied_score_gt_90": int(np.count_nonzero(values > 90)),
                         "height_valid_cells": int(len(floors)),
                         "z_floor_median_m": None if not len(floors) else float(np.median(floors)),
                         "z_ceiling_median_m": None if not len(ceilings) else float(np.median(ceilings))})
    return rows


def _m2_partition_stats(snapshot):
    bands = {"floor_0_0.5m": [0.0, 0.5], "body_0.5_1.5m": [0.5, 1.5], "upper_1.5_2.5m": [1.5, 2.5]}
    out = {name: {"free_leaves": 0, "occupied_leaves": 0} for name in bands}
    near = []
    for leaf in snapshot["leaves"]:
        x, y, z = leaf["center"]
        half = leaf["size"] / 2
        if x + half < 3.5 or x - half > 5.3 or y + half < -2 or y - half > 2 or z + half < 0 or z - half > 2.5:
            continue
        near.append(leaf)
        for name, (z0, z1) in bands.items():
            if z + half >= z0 and z - half < z1:
                out[name]["occupied_leaves" if leaf["occupied"] else "free_leaves"] += 1
    return {"partition_neighbour_leaf_counts": out, "partition_neighbour_leaf_total": len(near),
            "tree_revision": snapshot["revision"], "tree_resolution_m": snapshot["resolution"],
            "tree_bounds_min": snapshot["bounds_min"], "tree_bounds_max": snapshot["bounds_max"]}


def _ideal_geometry_grid(reference_grid):
    """Diagnostic-only GT occupancy encoding, never sent to the exploration builder."""
    h, w = reference_grid.shape
    res = reference_grid.resolution_m
    ox, oy = reference_grid.origin_xy
    xs, ys = ox + np.arange(w) * res, oy + np.arange(h) * res
    xx, yy = np.meshgrid(xs, ys)
    inside = (xx >= 0) & (xx <= WORLD_X_MAX) & (yy >= -2) & (yy <= 2)
    values = np.full((h, w), -1, dtype=np.int8)
    values[inside] = 0
    wall = inside & ((xx <= 0.10) | (xx >= WORLD_X_MAX - 0.10) | (yy <= -1.90) | (yy >= 1.90))
    partition = inside & (np.abs(xx - PARTITION_X) <= res) & (np.abs(yy) > DOOR_Y[1])
    obstacles = inside & (
        ((xx >= 2.05) & (xx <= 2.65) & (yy >= -0.42) & (yy <= 0.42)) |
        ((xx >= 6.10) & (xx <= 6.55) & (yy >= -0.25) & (yy <= 0.25)))
    values[wall | partition | obstacles] = 100
    return values, {"diagnostic_only_ground_truth_input": True, "same_resolution_m": res,
                    "same_origin_xy": list(reference_grid.origin_xy), "same_shape_yx": list(reference_grid.shape),
                    "doorway_y_bounds_m": list(DOOR_Y), "partition_x_m": PARTITION_X,
                    "warning": "not used by exploration/M2/M3/M5/M6-M9; comparison only"}


def _draw_gt(ax):
    ax.plot([0, WORLD_X_MAX, WORLD_X_MAX, 0, 0], [-2, -2, 2, 2, -2], color="white", lw=1.0, ls="--", label="fixture GT shell")
    ax.plot([PARTITION_X, PARTITION_X], [-2, DOOR_Y[0]], color="#ff00cc", lw=2, label="GT partition")
    ax.plot([PARTITION_X, PARTITION_X], [DOOR_Y[1], 2], color="#ff00cc", lw=2)
    for lo, hi in (([2.05, -0.42], [2.65, 0.42]), ([6.1, -0.25], [6.55, 0.25])):
        ax.add_patch(Rectangle(lo, hi[0]-lo[0], hi[1]-lo[1], fill=False, ec="#ffa500", lw=1.0))


def _render_checkpoint(path, name, grid, m2, m4, fresh=None, ideal=None):
    fig, axes = plt.subplots(1, 4, figsize=(20, 5.5))
    extent = [grid.origin_xy[0], grid.origin_xy[0] + grid.shape[1]*grid.resolution_m,
              grid.origin_xy[1], grid.origin_xy[1] + grid.shape[0]*grid.resolution_m]
    cmap = ListedColormap(["#8d99ae", "#62b36f", "#f3a712", "#d62828"])
    norm = BoundaryNorm([-1.5, -0.5, 0.5, 90.5, 101], cmap.N)
    axes[0].imshow(grid.values, origin="lower", extent=extent, interpolation="nearest", cmap=cmap, norm=norm, aspect="equal")
    axes[0].set_title("Native M3 categorical grid + fixture GT")
    _draw_gt(axes[0])
    axes[0].legend(handles=[
        Patch(color="#8d99ae", label="unknown (<0)"),
        Patch(color="#62b36f", label="free (=0)"),
        Patch(color="#f3a712", label="positive score 1..90"),
        Patch(color="#d62828", label="obstacle threshold >90"),
        Patch(color="#ff00cc", label="fixture GT partition"),
    ], fontsize=6, loc="lower right")
    rgb = np.ones((*grid.shape, 3), dtype=float)
    cleaned = np.asarray(m4["diagnostics"]["preprocessed_mask"], dtype=bool)
    black = np.asarray(m4["diagnostics"]["black_mask"], dtype=bool)
    rgb[cleaned] = [0.22, 0.72, 0.35]
    rgb[black] = [0.92, 0.20, 0.18]
    axes[1].imshow(rgb, origin="lower", extent=extent, interpolation="nearest", aspect="equal")
    axes[1].set_title("M4 cleaned free mask (green), black/obstacle (red)")
    for region in m4.get("regions", []):
        ring = np.asarray(region["outer"], dtype=float)
        if len(ring): axes[2].plot(*np.vstack([ring, ring[0]]).T, color="#1769aa", lw=2, label="sequential raw M4")
    if fresh:
        for region in fresh.get("regions", []):
            ring = np.asarray(region["outer"], dtype=float)
            if len(ring): axes[2].plot(*np.vstack([ring, ring[0]]).T, color="#be2edd", lw=1.7, ls="--", label="fresh M4")
    if ideal:
        for region in ideal.get("regions", []):
            ring = np.asarray(region["outer"], dtype=float)
            if len(ring): axes[2].plot(*np.vstack([ring, ring[0]]).T, color="#149447", lw=1.5, ls=":", label="ideal-grid fresh M4")
    _draw_gt(axes[2]); axes[2].set_xlim(extent[0], extent[1]); axes[2].set_ylim(extent[2], extent[3])
    axes[2].set_aspect("equal"); axes[2].grid(alpha=.15); axes[2].set_title("M4 raw contours vs GT geometry")
    # M2 leaf centers around the partition; color is tree state, not traversability.
    leaves = m2.get("leaves", [])
    for occupied, color, label in ((False, "#277da1", "M2 free leaves"), (True, "#d62828", "M2 occupied leaves")):
        pts = [leaf["center"] for leaf in leaves if leaf["occupied"] is occupied and 3.4 <= leaf["center"][0] <= 5.4 and -2 <= leaf["center"][1] <= 2 and 0 <= leaf["center"][2] <= 2.5]
        if pts:
            step = max(1, len(pts)//25000)
            axes[3].scatter([p[0] for p in pts[::step]], [p[1] for p in pts[::step]], s=1, c=color, alpha=.45, label=label, rasterized=True)
    _draw_gt(axes[3]); axes[3].set_xlim(3.4, 5.4); axes[3].set_ylim(-2, 2)
    axes[3].set_aspect("equal"); axes[3].grid(alpha=.15); axes[3].set_title("M2 leaf centers near doorway (XY; Z-filtered)")
    for ax in axes:
        ax.set_xlabel("world x (m)"); ax.set_ylabel("world y (m)")
        handles, labels = ax.get_legend_handles_labels()
        if handles: ax.legend(fontsize=6, loc="best")
    fig.suptitle(f"{name}: native M3 → pinned M4 diagnostics; fixture GT shown only as overlay")
    fig.tight_layout(); fig.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)


def _grid_metadata(grid):
    return {"revision": grid.revision, "resolution_m": grid.resolution_m,
            "origin_xy": list(grid.origin_xy), "shape_yx": list(grid.shape),
            "free_cells": int(np.count_nonzero(grid.values == 0)),
            "occupied_cells": int(np.count_nonzero(grid.values > 0)),
            "unknown_cells": int(np.count_nonzero(grid.values < 0)),
            "projection_mode": grid.projection_mode, "backend": grid.backend}


def _aligned_grid_comparison(a, b):
    if not math.isclose(a.resolution_m, b.resolution_m, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("full-refresh and partial grids have different resolutions")
    res = a.resolution_m
    ax0, ay0 = (round(v / res) for v in a.origin_xy)
    bx0, by0 = (round(v / res) for v in b.origin_xy)
    x0, y0 = max(ax0, bx0), max(ay0, by0)
    x1 = min(ax0 + a.shape[1], bx0 + b.shape[1])
    y1 = min(ay0 + a.shape[0], by0 + b.shape[0])
    aa = a.values[y0-ay0:y1-ay0, x0-ax0:x1-ax0]
    bb = b.values[y0-by0:y1-by0, x0-bx0:x1-bx0]
    return {"common_shape_yx": list(aa.shape), "different_cells": int(np.count_nonzero(aa != bb)),
            "same_cells": int(np.count_nonzero(aa == bb)), "partial_unknown": int(np.count_nonzero(aa < 0)),
            "full_unknown": int(np.count_nonzero(bb < 0)),
            "partial_free": int(np.count_nonzero(aa == 0)), "full_free": int(np.count_nonzero(bb == 0)),
            "partial_occupied": int(np.count_nonzero(aa > 0)), "full_occupied": int(np.count_nonzero(bb > 0)),
            "world_bounds_xy": [x0*res, y0*res, x1*res, y1*res]}


def run_diagnostics(output: Path, *, m2_m3_binary=M2_M3_BINARY, dude_binary=DUDE_BINARY):
    output.mkdir(parents=True, exist_ok=True)
    milestone_dir = output / "milestones"; milestone_dir.mkdir(exist_ok=True)
    mapping = NativeRGBDMappingStage(m2_m3_binary, resolution_m=0.05, timeout_s=20.0, default_max_range_m=5.5)
    # Same explicit local technical-test profile as configs/m4_local.yaml.
    dude_config = DuDeConfig(decomp_threshold_m=1.5, safety_distance_m=0.5, timeout_s=30.0)
    sequential = DuDeWorkerClient(dude_binary, dude_config)
    processor = RegionUpdateProcessor()
    fresh_probe = DuDeWorkerClient(dude_binary, dude_config)
    route = scripted_route()
    all_frames = []
    saved = {}
    branches_diverged = []
    try:
        for index, route_item in enumerate(route):
            packet, pose_info = make_packet(index, route_item)
            grid = mapping.build_grid(packet)
            snap = mapping.worker.snapshot() if index in CHECKPOINTS else None
            capture = index in CHECKPOINTS
            before_logs = sequential.stderr_tail
            response, m5 = sequential.update_regions(processor, grid, timestamp_s=packet.timestamp_s,
                                                      capture_preprocessed_mask=capture)
            after_logs = sequential.stderr_tail
            fresh_probe.reset()
            before_fresh_logs = fresh_probe.stderr_tail
            fresh_response, _ = fresh_probe.integrate(grid, timestamp_s=packet.timestamp_s,
                                                       capture_preprocessed_mask=capture)
            after_fresh_logs = fresh_probe.stderr_tail
            raw_seq, raw_fresh = response.get("regions", []), fresh_response.get("regions", [])
            same_geom = raw_seq == raw_fresh
            if not same_geom:
                branches_diverged.append(index)
            record = {
                "frame_index": index, "frame_id": packet.frame_id, "timestamp_s": packet.timestamp_s,
                "phase": pose_info["route_phase"], "pose_W": pose_info["base_pose_W"],
                "camera_yaw_rad": pose_info["camera_yaw_rad"], "mapping": mapping.records[-1],
                "m4_sequential": {"status": response["status"], "branch": response["branch"],
                                  "rejection_reason": response.get("rejection_reason"),
                                  "diagnostics": {k:v for k,v in response.get("diagnostics",{}).items() if k not in ("preprocessed_mask","black_mask")},
                                  "raw_regions": json_region_summary(raw_seq)},
                "m4_fresh_same_grid": {"status": fresh_response["status"], "branch": fresh_response["branch"],
                                       "diagnostics": {k:v for k,v in fresh_response.get("diagnostics",{}).items() if k not in ("preprocessed_mask","black_mask")},
                                       "raw_regions": json_region_summary(raw_fresh), "geometry_equal": same_geom},
                "m5": m5_summary(m5),
                "native_m4_stderr_new_lines": _new_stderr_lines(before_logs, after_logs),
                "fresh_m4_stderr_new_lines": _new_stderr_lines(before_fresh_logs, after_fresh_logs),
            }
            all_frames.append(record)
            if not capture:
                continue
            name = CHECKPOINTS[index]
            m2 = snap
            source = {"checkpoint": name, "frame_index": index, "input": record,
                      "m4_sequential_raw": response, "m4_fresh_same_grid_raw": fresh_response,
                      "m5_output": m5_summary(m5), "m2_partition_diagnostics": _m2_partition_stats(m2),
                      "m3_partition_diagnostics": _counts_and_heights(grid)}
            np.savez_compressed(milestone_dir / f"{name}.npz", values=grid.values,
                                z_floor=grid.z_floor, z_ceiling=grid.z_ceiling,
                                m4_cleaned=np.asarray(response["diagnostics"]["preprocessed_mask"], dtype=bool),
                                m4_black=np.asarray(response["diagnostics"]["black_mask"], dtype=bool))
            write_json_gzip(milestone_dir / f"{name}_m2_snapshot.json.gz", m2)
            (milestone_dir / f"{name}_diagnostics.json").write_text(json.dumps(source, indent=2, sort_keys=True, allow_nan=False)+"\n")
            saved[name] = {"grid": grid, "m2": m2, "m4": response, "fresh": fresh_response}
            _render_checkpoint(milestone_dir / f"{name}_overlay.png", name, grid, m2, response, fresh_response)
        # Diagnostic-only control: rebuild M3 from the same accumulated M2 OcTree
        # after the reference partial-update sequence has finished.
        full_refresh_grid = mapping.worker.project(mode="full_refresh")
        full_refresh_oracle = dict(mapping.worker.last_projection_diagnostics)
        clean_grid = mapping.worker.project(mode="clean_full_refresh")
        clean_oracle = dict(mapping.worker.last_projection_diagnostics)
        clean_client = DuDeWorkerClient(dude_binary, dude_config)
        try:
            clean_logs_before = clean_client.stderr_tail
            clean_response, _ = clean_client.integrate(clean_grid, timestamp_s=float(len(route)), capture_preprocessed_mask=True)
            clean_logs_after = clean_client.stderr_tail
        finally:
            clean_client.close()
        full_refresh_client = DuDeWorkerClient(dude_binary, dude_config)
        try:
            full_refresh_logs_before = full_refresh_client.stderr_tail
            full_refresh_response, _ = full_refresh_client.integrate(
                full_refresh_grid, timestamp_s=float(len(route)), capture_preprocessed_mask=True)
            full_refresh_logs_after = full_refresh_client.stderr_tail
        finally:
            full_refresh_client.close()
    finally:
        mapping.close(); sequential.close(); fresh_probe.close()

    final = saved["final_revisit"]["grid"]
    ideal_values, ideal_meta = _ideal_geometry_grid(final)
    from standalone_sg.mapping.projection import FreeSpaceGrid2D
    ideal = FreeSpaceGrid2D(values=ideal_values, resolution_m=final.resolution_m,
        origin_xy=final.origin_xy, z_floor=np.full(final.shape, np.nan), z_ceiling=np.full(final.shape, np.nan),
        revision=final.revision, frame_id="world", source="diagnostic_gt_geometry_only", backend="diagnostic_only")
    ideal_client = DuDeWorkerClient(dude_binary, dude_config)
    try:
        ideal_logs_before = ideal_client.stderr_tail
        ideal_response, _ = ideal_client.integrate(ideal, timestamp_s=float(len(route)), capture_preprocessed_mask=True)
        ideal_logs_after = ideal_client.stderr_tail
    finally:
        ideal_client.close()
    final_entry = saved["final_revisit"]
    np.savez_compressed(milestone_dir / "final_full_refresh_m3.npz",
                        values=full_refresh_grid.values, z_floor=full_refresh_grid.z_floor,
                        z_ceiling=full_refresh_grid.z_ceiling)
    np.savez_compressed(milestone_dir / "final_clean_converter_m3.npz", values=clean_grid.values,
                        z_floor=clean_grid.z_floor, z_ceiling=clean_grid.z_ceiling)
    np.savez_compressed(milestone_dir / "final_full_refresh_m4_masks.npz",
                        m4_cleaned=np.asarray(full_refresh_response["diagnostics"]["preprocessed_mask"], dtype=bool),
                        m4_black=np.asarray(full_refresh_response["diagnostics"]["black_mask"], dtype=bool))
    (milestone_dir / "final_clean_converter_m4.json").write_text(json.dumps({
        "m3_grid": _grid_metadata(clean_grid), "source_oracle": clean_oracle,
        "m4_response": clean_response, "scope": "new MapConverter on exact accumulated OcTree; no M2 reinsertion"
    }, indent=2, sort_keys=True, allow_nan=False)+"\n")
    (milestone_dir / "final_full_refresh_m4.json").write_text(json.dumps({
        "m3_grid": _grid_metadata(full_refresh_grid), "m4_response": full_refresh_response,
        "m4_stderr_new_lines": _new_stderr_lines(full_refresh_logs_before, full_refresh_logs_after),
        "scope": "same final accumulated M2 OcTree; diagnostic full-refresh control after reference partial sequence",
    }, indent=2, sort_keys=True, allow_nan=False)+"\n")
    _render_checkpoint(milestone_dir / "final_full_refresh_overlay.png", "final accumulated tree full-refresh",
                       full_refresh_grid, final_entry["m2"], full_refresh_response)
    _render_checkpoint(milestone_dir / "final_ideal_grid_comparison.png", "final grid vs ideal GT grid",
                       final, final_entry["m2"], final_entry["m4"], final_entry["fresh"], ideal_response)
    np.savez_compressed(milestone_dir / "ideal_geometry_grid.npz", values=ideal_values)
    (milestone_dir / "ideal_geometry_comparison.json").write_text(json.dumps({
        "ideal_grid_metadata": ideal_meta, "actual_final_grid": {"shape_yx": list(final.shape),
          "resolution_m": final.resolution_m, "origin_xy": list(final.origin_xy), "revision": final.revision},
        "ideal_m4": ideal_response,
        "ideal_m4_stderr_new_lines": _new_stderr_lines(ideal_logs_before, ideal_logs_after),
        "actual_sequential_m4": final_entry["m4"],
        "actual_fresh_m4": final_entry["fresh"],
        "scope": "diagnostic only; ideal ground-truth grid never enters main mapping or graph pipeline",
    }, indent=2, sort_keys=True, allow_nan=False)+"\n")
    manifest = {
        "kind": "two_room_sequence_layered_diagnostics", "baseline_artifacts_unchanged": True,
        "input_artifact": "artifacts/m11_two_room_rgbd_sequence/manifest.json",
        "frames": len(route), "checkpoints": {str(i): CHECKPOINTS[i] for i in CHECKPOINTS},
        "m4_binary_sha256": hashlib.sha256(Path(dude_binary).read_bytes()).hexdigest(),
        "m2_m3_binary_sha256": hashlib.sha256(Path(m2_m3_binary).read_bytes()).hexdigest(),
        "m4_sequential_status_counts": {s:sum(f["m4_sequential"]["status"]==s for f in all_frames) for s in ("success","fallback","rejected")},
        "m4_first_fresh_geometry_divergence_frame": branches_diverged[0] if branches_diverged else None,
        "m4_fresh_geometry_diff_frames": branches_diverged,
        "final_actual_m4_regions": len(final_entry["m4"]["regions"]),
        "final_fresh_m4_regions": len(final_entry["fresh"]["regions"]),
        "final_full_refresh_m3": _grid_metadata(full_refresh_grid),
        "final_full_refresh_source_oracle": full_refresh_oracle,
        "final_clean_converter_m3": _grid_metadata(clean_grid),
        "final_clean_converter_regions": len(clean_response["regions"]),
        "final_clean_converter_branch": clean_response["branch"],
        "final_clean_converter_source_oracle": clean_oracle,
        "partial_vs_full_refresh_m3": _aligned_grid_comparison(final, full_refresh_grid),
        "final_full_refresh_m4_regions": len(full_refresh_response["regions"]),
        "final_clean_control_not_yet_decomposed": False,
        "final_full_refresh_m4_branch": full_refresh_response["branch"],
        "ideal_geometry_fresh_m4_regions": len(ideal_response["regions"]),
        "ideal_grid_scope": ideal_meta,
        "frames_diagnostics": all_frames,
        "artifacts": sorted(str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()),
        "no_kernel_or_baseline_config_change": True,
        "cloud_upload": False,
    }
    (output / "diagnostics.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False)+"\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/m11_two_room_diagnostics")
    parser.add_argument("--m2-m3-binary", type=Path, default=M2_M3_BINARY)
    parser.add_argument("--dude-binary", type=Path, default=DUDE_BINARY)
    args = parser.parse_args()
    result = run_diagnostics(args.output, m2_m3_binary=args.m2_m3_binary, dude_binary=args.dude_binary)
    print(json.dumps({k:v for k,v in result.items() if k != "frames_diagnostics"}, sort_keys=True, indent=2))
