#!/usr/bin/env python3
"""Render reference-only, same-frame M3/M4/M5/M8 diagnostic overlays.

The configured wall/obstacle AABBs are evaluation context only.  They are
never passed into mapping, DuDe, or the graph builder.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from matplotlib.patches import Polygon, Rectangle
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _draw_aabbs(ax, config):
    collision = config.get("collision", {})
    for item in collision.get("wall_aabbs_xy", []):
        x0, y0 = item["min"]; x1, y1 = item["max"]
        ax.add_patch(Rectangle((x0, y0), x1-x0, y1-y0, facecolor="#444444", edgecolor="#222222", alpha=.45, linewidth=.5))
    for item in collision.get("obstacle_aabbs_xy", []):
        x0, y0 = item["min"]; x1, y1 = item["max"]
        ax.add_patch(Rectangle((x0, y0), x1-x0, y1-y0, facecolor="#d95f02", edgecolor="#8c2d04", alpha=.55, linewidth=.7))


def _extent(meta):
    x, y = meta["origin_xy"]; h, w = meta["shape"]; r = meta["resolution_m"]
    return (x, x + w*r, y, y + h*r)


def _rings(ax, regions, *, color, label, linestyle="-", alpha=.9):
    first = True
    for region in regions:
        attributes = region.get("attributes", {})
        ring = region.get("polygon", {}).get("outer", region.get("outer"))
        if ring is None:
            ring = attributes.get("outer_ring")
        if not ring:
            continue
        pts = np.asarray(ring, dtype=float)
        ax.add_patch(Polygon(pts, closed=True, fill=False, edgecolor=color, linewidth=1.4,
                             linestyle=linestyle, alpha=alpha, label=label if first else None))
        center = region.get("centroid_xy", attributes.get("centroid_xy"))
        if center is not None:
            ident = region.get("persistent_id", region.get("raw_id", attributes.get("room_id", region.get("node_id", "?"))))
            ax.text(float(center[0]), float(center[1]), str(ident), color=color, fontsize=7,
                    ha="center", va="center", bbox={"facecolor":"white", "alpha":.65, "edgecolor":"none", "pad":.5})
        first = False


def _load_trajectory(path, max_frame_index):
    if path is None or not path.is_file():
        return None
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                if int(row["frame_index"]) <= max_frame_index:
                    rows.append((float(row["x_m"]), float(row["y_m"])))
            except (KeyError, TypeError, ValueError):
                continue
    return rows or None


def _draw_sensor_fov(ax, meta):
    pose = meta.get("sensor_pose_W_Copt")
    intr = meta.get("intrinsics")
    if pose is None or intr is None:
        return
    T = np.asarray(pose, dtype=float)
    if T.shape != (4, 4):
        return
    origin = T[:3, 3]
    forward = T[:3, 2]
    right = T[:3, 0]
    down = T[:3, 1]
    depth = 2.0
    half_x = math.atan2(float(intr["cx"]), float(intr["fx"]))
    half_y = math.atan2(float(intr["cy"]), float(intr["fy"]))
    center = origin + forward * depth
    left = center - right * depth * math.tan(half_x)
    right_pt = center + right * depth * math.tan(half_x)
    top = center - down * depth * math.tan(half_y)
    bottom = center + down * depth * math.tan(half_y)
    ax.plot([origin[0], center[0]], [origin[1], center[1]], color="#1f78b4", linewidth=1.0, alpha=.8,
            label="sensor optical +Z")
    ax.plot([left[0], right_pt[0]], [left[1], right_pt[1]], color="#6a3d9a", linewidth=.8, alpha=.7,
            label="sensor FOV")
    ax.plot([top[0], bottom[0]], [top[1], bottom[1]], color="#6a3d9a", linewidth=.8, alpha=.7)


def render_frame(meta, arrays, config, out_path, trajectory_path=None):
    extent = _extent(meta)
    values = arrays["values"]
    unknown = values < 0
    grid_display = np.where(unknown, 0.35, np.clip(values, 0, 100) / 100.0)
    cmap = ListedColormap(["#ffffff", "#111111"])
    fig, axes = plt.subplots(2, 2, figsize=(15, 11), constrained_layout=True)
    panels = [(axes[0, 0], "M3 raw FreeSpaceGrid2D", grid_display, "m3"),
              (axes[0, 1], "M4 preprocessing mask actually sent to DuDe", arrays.get("m4_preprocessed_mask"), "mask"),
              (axes[1, 0], "M4 raw contours / M5 current regions", grid_display, "regions"),
              (axes[1, 1], "M3 + M8 RoomNodes + captured trajectory", grid_display, "rooms")]
    for ax, title, image, kind in panels:
        if image is not None:
            if kind == "m3":
                rgba = np.empty((*image.shape, 4), dtype=float)
                rgba[unknown] = (0.55, 0.55, 0.55, 1.0)
                free = values == 0; occ = values > 0
                rgba[free] = (1.0, 1.0, 1.0, 1.0)
                rgba[occ] = (0.05, 0.05, 0.05, 1.0)
                ax.imshow(rgba, origin="lower", extent=extent, interpolation="nearest")
            elif kind == "mask":
                ax.imshow(np.asarray(image, dtype=bool), origin="lower", extent=extent, interpolation="nearest", cmap=ListedColormap(["#bdbdbd", "#5ab4ac"]), vmin=0, vmax=1)
            else:
                rgba = np.empty((*image.shape, 4), dtype=float)
                rgba[unknown] = (0.55, 0.55, 0.55, 1.0)
                rgba[values == 0] = (1.0, 1.0, 1.0, 1.0)
                rgba[values > 0] = (0.05, 0.05, 0.05, 1.0)
                ax.imshow(rgba, origin="lower", extent=extent, interpolation="nearest")
        _draw_aabbs(ax, config)
        if kind == "regions":
            _rings(ax, meta["m4"]["raw_regions"], color="#e66101", label="M4 raw outer contour")
            _rings(ax, meta["m5"]["current_regions"], color="#018571", label="M5 current region")
        elif kind == "rooms":
            _rings(ax, meta["m8"]["rooms"], color="#5e3c99", label="M8 RoomNode", linestyle="--" if meta["m8"]["regions_stale"] else "-")
            trajectory = _load_trajectory(trajectory_path, int(meta["frame_index"]))
            if trajectory:
                xs, ys = zip(*trajectory)
                ax.plot(xs, ys, color="#1f78b4", linewidth=1.2, label="captured base trajectory")
            pose = meta.get("base_pose_W")
            if pose is not None:
                ax.scatter([pose[0]], [pose[1]], color="#e31a1c", s=28, label="current base pose")
            _draw_sensor_fov(ax, meta)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("world X (m)"); ax.set_ylabel("world Y (m)"); ax.set_aspect("equal", adjustable="box")
        ax.grid(alpha=.15)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(loc="best", fontsize=7)
    fig.suptitle(f"frame {meta['frame_index']} / {meta['frame_id']} — M3 rev {meta['map_revision']} — {meta['m4']['status']}:{meta['m4']['branch']} — M5 stale={meta['m5']['stale']}", fontsize=12)
    fig.savefig(out_path, dpi=150, facecolor="white")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid-trace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, default=None,
                        help="optional trajectory.csv from the same replay")
    parser.add_argument("--frame-index", type=int, action="append")
    args = parser.parse_args()
    index = json.loads((args.grid_trace / "index.json").read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    entries = index["frames"]
    selected = set(args.frame_index or [])
    if not selected:
        selected = {entries[0]["frame_index"], entries[-1]["frame_index"]}
        for entry in entries:
            meta = json.loads((args.grid_trace / entry["metadata_file"]).read_text(encoding="utf-8"))
            if meta["m4"]["status"] == "fallback" or meta["m5"]["stale"] or len(meta["m4"]["raw_regions"]) > 1:
                selected.add(entry["frame_index"])
    args.output.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in entries:
        if entry["frame_index"] not in selected:
            continue
        meta = json.loads((args.grid_trace / entry["metadata_file"]).read_text(encoding="utf-8"))
        with np.load(args.grid_trace / entry["array_file"], allow_pickle=False) as arrays:
            data = {name: arrays[name].copy() for name in arrays.files}
        path = args.output / f"frame_{entry['frame_index']:06d}.png"
        render_frame(meta, data, config, path, args.trajectory)
        written.append(str(path))
    report = {"frames_rendered": len(written), "outputs": written, "reference_geometry_only": True,
              "grid_semantics": "-1 unknown, 0 free, 1..100 source occupation score"}
    (args.output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
