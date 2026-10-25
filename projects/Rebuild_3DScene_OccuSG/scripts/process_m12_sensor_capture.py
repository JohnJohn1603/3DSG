#!/usr/bin/env python3
"""Offline M1 processing/geometry validation for one Webots sensor capture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from adapters.webots.capture import (
    camera_intrinsics,
    copy_bgra_rgb,
    copy_rangefinder_depth_m,
    webots_camera_node_pose_to_optical,
)
from standalone_sg.contracts import Transform
from standalone_sg.geometry import backproject_depth


GEOMETRY_TOLERANCE_M = 0.02


def process(output: Path) -> dict:
    metadata = json.loads((output / "capture_metadata.json").read_text(encoding="utf-8"))
    if metadata["webots_version"] != "R2025a" or metadata["coordinate_system"] != "ENU":
        raise ValueError("capture version/coordinate frame does not match this pinned fixture")
    width, height = metadata["resolution_px"]
    bgra = (output / "camera_bgra.bin").read_bytes()
    depth_buffer = (output / "range_depth_f32.bin").read_bytes()
    rgb = copy_bgra_rgb(bgra, width, height)
    depth_m, valid_mask = copy_rangefinder_depth_m(depth_buffer, width, height)
    intrinsics = camera_intrinsics(width, height, metadata["rangefinder_horizontal_fov_rad"])
    Image.fromarray(rgb, mode="RGB").save(output / "rgb.png")
    preview = np.zeros(depth_m.shape, dtype=np.uint8)
    preview[valid_mask] = np.clip(
        255.0 * depth_m[valid_mask] / metadata["max_range_m"], 0, 255
    ).astype(np.uint8)
    Image.fromarray(preview, mode="L").save(output / "depth_preview.png")

    # Verified fixture contract: authored WorldInfo is ENU; Webots uses metres;
    # the owned world origin is canonical origin. No identity is used by default
    # in the adapter; this explicit T_world_webots is supplied by this fixture.
    T_world_webots = Transform(np.eye(4), "world", "webots_world",
                               metadata["timestamp_s"], "calibration")
    T_world_camera_optical = webots_camera_node_pose_to_optical(
        metadata["camera_node_pose_row_major"], metadata["timestamp_s"], T_world_webots
    )
    cloud = backproject_depth(
        depth_m,
        intrinsics,
        T_world_camera_optical,
        meters_per_unit=1.0,
        min_depth_m=metadata["min_range_m"],
        max_depth_m=metadata["max_range_m"],
        valid_mask=valid_mask,
        timestamp=metadata["timestamp_s"],
        scan_id="webots-r2025a-sensor-calibration-0",
    )

    u, v = width // 2, height // 2
    center_depth_m = float(depth_m[v, u])
    center_mask = np.zeros((height, width), dtype=bool)
    center_mask[v, u] = True
    center_cloud = backproject_depth(
        depth_m,
        intrinsics,
        T_world_camera_optical,
        meters_per_unit=1.0,
        min_depth_m=metadata["min_range_m"],
        max_depth_m=metadata["max_range_m"],
        valid_mask=center_mask,
        timestamp=metadata["timestamp_s"],
        scan_id="webots-r2025a-center-ray",
    )
    if center_cloud.xyz_W.shape != (1, 3):
        raise ValueError("central pixel must contain exactly one valid wall hit")
    measured = center_cloud.xyz_W[0].astype(np.float64)
    expected = np.array([metadata["fixture"]["wall_front_x_m"], 0.0, 1.0])
    geometry_error_m = float(np.linalg.norm(measured - expected))

    sensor_pose = np.asarray(metadata["camera_node_pose_row_major"], dtype=np.float64).reshape(4, 4)
    range_pose = np.asarray(metadata["rangefinder_node_pose_row_major"], dtype=np.float64).reshape(4, 4)
    base_pose = np.asarray(metadata["robot_base_pose_row_major"], dtype=np.float64).reshape(4, 4)
    measured_extrinsic = sensor_pose[:3, 3] - base_pose[:3, 3]
    expected_extrinsic = np.asarray(metadata["fixture"]["sensor_offset_xyz_m"], dtype=np.float64)
    extrinsic_error_m = float(np.linalg.norm(measured_extrinsic - expected_extrinsic))
    rotation_difference = float(np.linalg.norm(sensor_pose[:3, :3] - base_pose[:3, :3]))
    rgb_api_sample_errors = {}
    for sample in metadata["rgb_channel_samples_via_webots_api"].values():
        sample_u, sample_v = sample["uv"]
        rgb_api_sample_errors[f"{sample_u},{sample_v}"] = [
            int(rgb[sample_v, sample_u, channel]) - int(sample["rgb"][channel])
            for channel in range(3)
        ]
    rgb_api_samples_match = all(all(delta == 0 for delta in delta_rgb)
                                for delta_rgb in rgb_api_sample_errors.values())
    finite_depth = depth_m[valid_mask]
    if finite_depth.size == 0:
        raise ValueError("no valid depth samples in captured frame")

    np.savez_compressed(
        output / "capture_processed.npz",
        rgb=rgb,
        depth_m=depth_m,
        valid_mask=valid_mask,
        intrinsics=intrinsics.matrix,
        T_world_camera_optical=T_world_camera_optical.matrix,
        cloud_world_xyz=cloud.xyz_W,
        timestamp_s=np.asarray(metadata["timestamp_s"]),
    )

    report = {
        "status": "pass" if (geometry_error_m <= GEOMETRY_TOLERANCE_M
                              and extrinsic_error_m <= 1e-6
                              and float(np.max(np.abs(sensor_pose - range_pose))) <= 1e-9
                              and rotation_difference > 0.1
                              and rgb_api_samples_match
                              and metadata["owned_copies_stable_after_next_step"])
        else "fail",
        "webots_version": metadata["webots_version"],
        "coordinate_system": metadata["coordinate_system"],
        "documented_depth_semantics": metadata["depth_semantics"],
        "depth_unit": "metres (verified against fixture geometry)",
        "timestamp_s": metadata["timestamp_s"],
        "resolution_px": [width, height],
        "horizontal_fov_rad": metadata["rangefinder_horizontal_fov_rad"],
        "intrinsics_px": {"fx": intrinsics.fx, "fy": intrinsics.fy,
                           "cx": intrinsics.cx, "cy": intrinsics.cy},
        "T_world_camera_optical": T_world_camera_optical.matrix.tolist(),
        "camera_sensor_pose_source": "actual Supervisor CAMERA_NODE.getPose()",
        "base_pose_used_for_backprojection": False,
        "camera_sensor_offset_error_m": extrinsic_error_m,
        "sensor_rotation_differs_from_base_frobenius": rotation_difference,
        "camera_rangefinder_pose_max_abs_difference": float(np.max(np.abs(sensor_pose - range_pose))),
        "center_pixel_uv": [u, v],
        "center_depth_m": center_depth_m,
        "measured_center_world_xyz_m": measured.tolist(),
        "expected_center_world_xyz_m": expected.tolist(),
        "center_geometry_error_m": geometry_error_m,
        "geometry_tolerance_m": GEOMETRY_TOLERANCE_M,
        "valid_depth_pixel_count": int(valid_mask.sum()),
        "invalid_depth_pixel_count": int((~valid_mask).sum()),
        "world_backprojected_point_count": int(cloud.xyz_W.shape[0]),
        "rgb_conversion": "BGRA→RGB using pinned Camera buffer contract; capture and RGB PNG saved",
        "rgb_matches_webots_channel_api_samples": rgb_api_samples_match,
        "rgb_channel_sample_deltas": rgb_api_sample_errors,
        "rgb_unique_colors": int(np.unique(rgb.reshape(-1, 3), axis=0).shape[0]),
        "owned_buffer_copies_survived_next_step": metadata["owned_copies_stable_after_next_step"],
        "raw_capture_artifacts": ["camera_bgra.bin", "range_depth_f32.bin", "capture_metadata.json"],
    }
    (output / "calibration.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/m12_sensor_calibration"))
    args = parser.parse_args()
    report = process(args.output)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
