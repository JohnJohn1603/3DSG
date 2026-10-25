"""Pure Habitat-Sim capture normalization into the project contracts.

Habitat-Sim uses a Y-up world and a camera frame with +X right, +Y up and
-Z forward.  The core canonical world is Z-up and camera_optical is
+X right, +Y down, +Z forward.  The conversion is explicit and is never
performed by the mapping or graph stages.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from adapters.habitat.calibration import habitat_intrinsics_values

from standalone_sg.contracts import CameraIntrinsics, RGBDFrame, Transform
from standalone_sg.geometry.simulator_frames import habitat_to_canonical
from standalone_sg.pipeline import FramePacket


def habitat_world_to_canonical_transform(timestamp_s: float | None = None) -> Transform:
    """Return the calibrated Habitat Y-up -> canonical Z-up world transform.

    The mapping preserves right-handed orientation: C.x=H.x, C.y=-H.z,
    C.z=H.y.  It is a calibration contract, not an identity assumption.
    """
    matrix = np.array(
        [[1.0, 0.0, 0.0, 0.0],
         [0.0, 0.0, -1.0, 0.0],
         [0.0, 1.0, 0.0, 0.0],
         [0.0, 0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    return Transform(matrix, "world", "habitat_world", timestamp_s, "calibration")


def habitat_camera_intrinsics(width: int, height: int, hfov_deg: float, *, calibration_id: str) -> CameraIntrinsics:
    intr = habitat_intrinsics_values(width, height, hfov_deg)
    return CameraIntrinsics(
        width, height, intr["fx"], intr["fy"], intr["cx"], intr["cy"],
        calibration_id=calibration_id,
    )


def habitat_pose_matrix(position: Any, rotation_matrix: Any, *, parent_frame: str,
                        child_frame: str, timestamp_s: float, source: str = "ground_truth") -> Transform:
    position = np.asarray(position, dtype=np.float64)
    rotation = np.asarray(rotation_matrix, dtype=np.float64)
    if position.shape != (3,) or rotation.shape != (3, 3):
        raise ValueError("Habitat pose must contain a 3-vector and 3x3 rotation matrix")
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = position
    return Transform(matrix, parent_frame, child_frame, timestamp_s, source)  # type: ignore[arg-type]


def normalize_habitat_rgb(rgb: np.ndarray) -> np.ndarray:
    value = np.asarray(rgb)
    if value.ndim != 3 or value.shape[2] not in (3, 4):
        raise ValueError("Habitat color observation must have shape (H,W,3) or (H,W,4)")
    if value.dtype != np.uint8:
        value = np.clip(value, 0, 255).astype(np.uint8)
    return value[..., :3].copy()


def normalize_habitat_depth(depth: np.ndarray, *, min_depth_m: float, max_depth_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Copy metric axial depth and derive an explicit invalid mask.

    Habitat-Sim's depth sensor is configured with ``normalize_depth`` absent;
    the adapter therefore requires the capture metadata to declare metric
    metres.  Zero, non-finite and out-of-range samples are invalid and never
    become max-range no-return rays.
    """
    value = np.asarray(depth, dtype=np.float32).copy()
    valid = np.isfinite(value) & (value >= float(min_depth_m)) & (value <= float(max_depth_m))
    value[~valid] = np.nan
    return value, valid


def frame_from_capture_record(record: Mapping[str, Any], capture_dir: Path) -> tuple[RGBDFrame, FramePacket]:
    """Load one raw Habitat record without importing Habitat-Sim."""
    frame_dir = (capture_dir / str(record["frame_dir"])).resolve()
    if capture_dir.resolve() not in frame_dir.parents:
        raise ValueError("Habitat frame path escapes capture directory")
    metadata = dict(record)
    metadata.update(json.loads((frame_dir / "metadata.json").read_text(encoding="utf-8")))
    depth = np.load(frame_dir / "depth_m.npy", allow_pickle=False)
    rgb = np.load(frame_dir / "rgb.npy", allow_pickle=False) if (frame_dir / "rgb.npy").is_file() else None
    depth_m, valid_mask = normalize_habitat_depth(
        depth, min_depth_m=float(metadata["min_depth_m"]), max_depth_m=float(metadata["max_depth_m"])
    )
    saved_mask_path = frame_dir / "valid_mask.npy"
    if saved_mask_path.is_file():
        saved_mask = np.asarray(np.load(saved_mask_path, allow_pickle=False), dtype=bool)
        if saved_mask.shape != valid_mask.shape:
            raise ValueError("saved Habitat valid_mask shape does not match depth")
        if np.any(saved_mask & ~valid_mask):
            raise ValueError("saved Habitat valid_mask marks an invalid depth as valid")
        valid_mask = saved_mask
        depth_m[~valid_mask] = np.nan
    intrinsics = habitat_camera_intrinsics(
        int(metadata["width"]), int(metadata["height"]), float(metadata["hfov_deg"]),
        calibration_id=str(metadata["calibration_id"]),
    )
    t_c_h = Transform(np.asarray(metadata["canonical_from_habitat"], dtype=np.float64),
                      "world", "habitat_world", float(metadata["timestamp_s"]), "calibration")
    t_h_sensor = habitat_pose_matrix(
        metadata["sensor_position_habitat"], metadata["sensor_rotation_habitat"],
        parent_frame="habitat_world", child_frame="habitat_sensor", timestamp_s=float(metadata["timestamp_s"]),
    )
    t_w_copt = habitat_to_canonical(t_c_h, t_h_sensor)
    rgbd = RGBDFrame(
        depth_m, intrinsics, t_w_copt, float(metadata["timestamp_s"]),
        rgb=None if rgb is None else normalize_habitat_rgb(rgb),
        valid_mask=valid_mask, frame_id=str(metadata["frame_id"]),
    )
    base_matrix = np.asarray(metadata["base_pose_canonical"], dtype=np.float64)
    packet = FramePacket(
        scene_id=str(metadata["scene_id"]), sequence_id=str(metadata["sequence_id"]),
        frame_id=str(metadata["frame_id"]), frame_index=int(metadata["frame_index"]),
        timestamp_s=float(metadata["timestamp_s"]), input_mode="rgbd",
        base_pose_W=tuple(float(v) for v in base_matrix[:3, 3]), rgbd=rgbd,
        source_metadata={
            "capture_kind": f"habitat_sim_{metadata.get('dataset_type', 'replica_cad')}_raw_replay",
            "dataset_type": str(metadata.get("dataset_type", "replica_cad")),
            "dataset_version": metadata.get("dataset_version"),
            "simulator": "Habitat-Sim",
            "scene_id": str(metadata["scene_id"]),
            "scene_file": str(metadata["scene_file"]),
            "frame_id_coordinate": "world",
            "canonical_world": "Z-up; C.x=H.x, C.y=-H.z, C.z=H.y",
            "camera_optical": "+X right, +Y down, +Z forward",
            "depth_units": "m", "depth_representation": "axial",
            "depth_scale_to_m": 1.0,
            "min_depth_m": float(metadata["min_depth_m"]),
            "max_depth_m": float(metadata["max_depth_m"]),
            "backprojection_stride": 1,
            "valid_mask_source": "finite metric depth inside configured min/max range",
            "sensor_pose_source": "Habitat agent sensor state",
            "base_pose_source": "Habitat agent ground-truth state",
            "navmesh_used_for_route": bool(metadata.get("navmesh_used_for_route", False)),
            "semantic_gt_in_builder": False,
        },
    )
    return rgbd, packet
