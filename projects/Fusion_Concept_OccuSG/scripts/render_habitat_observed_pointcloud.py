#!/usr/bin/env python3
"""Render a 3-D view of surfaces actually observed in a Habitat RGB-D capture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from adapters.habitat.frames import frame_from_capture_record  # noqa: E402
from standalone_sg.geometry import backproject_depth  # noqa: E402


def _equal_axes(ax, points: np.ndarray) -> None:
    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    centers = (mins + maxs) / 2.0
    radius = max(float(np.max(maxs - mins)) / 2.0, 1e-3)
    ax.set_xlim(centers[0] - radius, centers[0] + radius)
    ax.set_ylim(centers[1] - radius, centers[1] + radius)
    ax.set_zlim(centers[2] - radius, centers[2] + radius)
    ax.set_box_aspect((1, 1, 1))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backprojection-stride", type=int, default=4,
                        help="visualization-only pixel stride; does not change M1-M3 artifacts")
    args = parser.parse_args()
    if args.backprojection_stride < 1:
        raise ValueError("backprojection stride must be positive")
    capture = args.capture.resolve()
    manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    points = []
    sensor_origins = []
    base_positions = []
    for record in manifest["frames"]:
        _rgbd, packet = frame_from_capture_record(record, capture)
        cloud = backproject_depth(
            packet.rgbd.depth_m,
            packet.rgbd.intrinsics,
            packet.rgbd.T_W_Copt,
            meters_per_unit=1.0,
            stride=args.backprojection_stride,
            min_depth_m=0.1,
            max_depth_m=10.0,
            valid_mask=packet.rgbd.valid_mask,
            timestamp=packet.timestamp_s,
            scan_id=packet.frame_id,
        )
        points.append(cloud.xyz_W)
        sensor_origins.append(np.asarray(cloud.sensor_origin_W, dtype=float))
        base_positions.append(np.asarray(packet.base_pose_W, dtype=float))
    xyz = np.concatenate(points, axis=0)
    origins = np.asarray(sensor_origins)
    bases = np.asarray(base_positions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output = args.output.resolve()
    z = xyz[:, 2]
    colors = plt.get_cmap("viridis")((z - z.min()) / max(float(z.max() - z.min()), 1e-6))
    fig = plt.figure(figsize=(16, 8), constrained_layout=True)
    ax3d = fig.add_subplot(1, 2, 1, projection="3d")
    ax3d.scatter(xyz[:, 0], xyz[:, 1], xyz[:, 2], c=colors, s=0.18, alpha=0.24, linewidths=0)
    ax3d.plot(bases[:, 0], bases[:, 1], bases[:, 2], color="#00a6ff", linewidth=2.0, label="base pose sequence")
    ax3d.scatter(origins[:, 0], origins[:, 1], origins[:, 2], color="#e31a1c", s=8, alpha=.8, label="sensor poses")
    ax3d.set_title("Observed RGB-D surface point cloud")
    ax3d.set_xlabel("canonical X (m)")
    ax3d.set_ylabel("canonical Y (m)")
    ax3d.set_zlabel("canonical Z (m)")
    _equal_axes(ax3d, xyz)
    ax3d.legend(fontsize=8)

    ax2d = fig.add_subplot(1, 2, 2)
    ax2d.scatter(xyz[:, 0], xyz[:, 1], c=colors, s=0.18, alpha=0.24, linewidths=0)
    ax2d.plot(bases[:, 0], bases[:, 1], color="#00a6ff", linewidth=2.0, label="base pose sequence")
    ax2d.scatter(origins[:, 0], origins[:, 1], color="#e31a1c", s=8, label="sensor poses")
    ax2d.set_title("Observed surfaces — top-down projection")
    ax2d.set_xlabel("canonical X (m)")
    ax2d.set_ylabel("canonical Y (m)")
    ax2d.set_aspect("equal", adjustable="box")
    ax2d.grid(alpha=.18)
    ax2d.legend(fontsize=8)
    fig.suptitle(f"{manifest.get('scene_id')} — {len(manifest['frames'])} RGB-D frames; gray/unknown areas are not rendered", fontsize=13)
    fig.savefig(args.output, dpi=160, facecolor="white")
    plt.close(fig)
    report = {
        "schema_version": 1,
        "kind": "habitat_observed_rgbd_pointcloud_visualization",
        "status": "pass",
        "capture": str(capture),
        "scene_id": manifest.get("scene_id"),
        "frame_count": len(manifest["frames"]),
        "point_count": int(xyz.shape[0]),
        "visualization_backprojection_stride": args.backprojection_stride,
        "output": str(args.output),
        "uses_scene_mesh": False,
        "uses_room_gt": False,
        "uses_semantic_gt": False,
        "interpretation": "Only surfaces reached by captured valid depth rays are shown; this is not a complete mesh or GT map.",
    }
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
