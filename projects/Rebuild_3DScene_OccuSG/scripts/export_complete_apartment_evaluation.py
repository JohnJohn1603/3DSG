#!/usr/bin/env python3
"""Render reference-only geometry beside one serialized graph snapshot.

This is an evaluation view, not a mapping input: authored AABBs come from the
world/config only to make wall/door alignment inspectable.  It deliberately
does not synthesize an occupancy grid or feed reference geometry to M3--M10.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.serialization import load_snapshot, trajectory_rows  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    collision = config.get("collision", {})
    fig, ax = plt.subplots(figsize=(12, 9))

    walls = collision.get("wall_aabbs_xy", [])
    obstacles = collision.get("obstacle_aabbs_xy", [])
    for item in walls:
        x0, y0 = item["min"]
        x1, y1 = item["max"]
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0,
                               facecolor="#333333", edgecolor="#111111",
                               alpha=0.35, linewidth=0.6))
    for item in obstacles:
        x0, y0 = item["min"]
        x1, y1 = item["max"]
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0,
                               facecolor="#d95f02", edgecolor="#8c2d04",
                               alpha=0.45, linewidth=0.8))

    room_seen = False
    for node in snapshot.nodes:
        if node.kind != "room":
            continue
        ring = node.attributes.get("outer_ring")
        if not ring:
            continue
        points = [(float(x), float(y)) for x, y in ring]
        ax.add_patch(Polygon(points, closed=True, facecolor="#7b61a8",
                             edgecolor="#5a3d87", alpha=0.18, linewidth=1.4,
                             linestyle="--" if snapshot.stale_flags.get("regions") else "-"))
        centroid = node.attributes.get("centroid_xy")
        if centroid is not None:
            ax.annotate(str(node.attributes.get("name", node.node_id)),
                        (float(centroid[0]), float(centroid[1])),
                        color="#5a3d87", weight="bold", ha="center")
        room_seen = True

    rows = trajectory_rows(snapshot)
    if rows:
        xs = [float(row["x_m"]) for row in rows]
        ys = [float(row["y_m"]) for row in rows]
        ax.plot(xs, ys, color="#1f77b4", linewidth=2.0, label="actual agent trajectory")
        ax.scatter([xs[0]], [ys[0]], color="#2ca02c", s=38, label="start", zorder=5)
        ax.scatter([xs[-1]], [ys[-1]], color="#d62728", marker="^", s=48, label="latest pose", zorder=5)

    ax.set_title(
        f"Complete apartment evaluation overlay — revision {snapshot.revision}\n"
        "reference walls/AABBs + native RoomNodes/trajectory; not M3 occupancy"
    )
    ax.set_xlabel("World X (m)")
    ax.set_ylabel("World Y (m)")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, alpha=0.18)
    handles, labels = ax.get_legend_handles_labels()
    if room_seen:
        from matplotlib.lines import Line2D
        handles.extend([
            Rectangle((0, 0), 1, 1, facecolor="#333333", alpha=0.35),
            Rectangle((0, 0), 1, 1, facecolor="#d95f02", alpha=0.45),
            Rectangle((0, 0), 1, 1, facecolor="#7b61a8", alpha=0.18),
        ])
        labels.extend(["authored wall/window AABB", "authored obstacle AABB", "native RoomNode footprint"])
    if handles:
        ax.legend(handles, labels, loc="best", fontsize=8)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(args.output, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(json.dumps({"output": str(args.output), "revision": snapshot.revision,
                      "rooms": sum(node.kind == "room" for node in snapshot.nodes),
                      "trajectory_points": len(rows), "reference_only": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
