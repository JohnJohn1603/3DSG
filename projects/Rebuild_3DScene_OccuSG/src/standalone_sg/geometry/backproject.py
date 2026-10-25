"""ROS-free axial-depth back-projection matching the OccuSG baseline kernel."""

from __future__ import annotations

from typing import Final

import numpy as np

from ..contracts.errors import ContractError
from ..contracts.frames import CameraIntrinsics, PointCloud, Transform
from .transforms import transform_points

_POSITIVE_FLOAT32: Final[float] = float(np.nextafter(np.float32(0.0), np.float32(1.0)))


def decode_depth_to_meters(
    depth: np.ndarray,
    *,
    meters_per_unit: float | None,
) -> np.ndarray:
    """Convert a raw depth image using an explicit scale.

    The baseline uses ``meters_per_unit=0.001`` for ``16UC1`` and ``1.0`` for
    ``32FC1``. No dtype-based millimeter assumption is made here.
    """

    raw = np.asarray(depth)
    if raw.ndim != 2:
        raise ContractError("depth must have shape (H, W)")
    if meters_per_unit is None:
        raise ContractError("meters_per_unit must be explicit; depth dtype is not enough")
    if not np.isfinite(meters_per_unit) or meters_per_unit <= 0:
        raise ContractError("meters_per_unit must be a finite positive number")
    if not np.issubdtype(raw.dtype, np.number):
        raise ContractError("depth must have a numeric dtype")
    return np.asarray(raw, dtype=np.float32) * np.float32(meters_per_unit)


def radial_range_to_axial_depth(
    radial_range: np.ndarray,
    intrinsics: CameraIntrinsics,
) -> np.ndarray:
    """Convert a radial range image to camera-axial depth."""

    ranges = np.asarray(radial_range, dtype=np.float32)
    if ranges.shape != (intrinsics.height, intrinsics.width):
        raise ContractError("radial_range shape must match CameraIntrinsics")
    u = np.arange(intrinsics.width, dtype=np.float32)
    v = np.arange(intrinsics.height, dtype=np.float32)
    u_grid, v_grid = np.meshgrid(u, v, indexing="xy")
    ray_norm = np.sqrt(
        1.0
        + ((u_grid - np.float32(intrinsics.cx)) / np.float32(intrinsics.fx)) ** 2
        + ((v_grid - np.float32(intrinsics.cy)) / np.float32(intrinsics.fy)) ** 2
    )
    axial = ranges / ray_norm
    axial[~np.isfinite(ranges)] = np.nan
    return axial.astype(np.float32, copy=False)


def backproject_depth(
    depth: np.ndarray,
    intrinsics: CameraIntrinsics,
    T_W_Copt: Transform,
    *,
    meters_per_unit: float | None,
    stride: int = 1,
    min_depth_m: float = 0.1,
    max_depth_m: float = 10.0,
    valid_mask: np.ndarray | None = None,
    timestamp: float | None = None,
    scan_id: str | None = None,
) -> PointCloud:
    """Back-project axial depth into a canonical world-frame point cloud.

    Pixel order is the baseline's row-major order after ``[::stride, ::stride]``.
    Invalid, non-finite, below-minimum, and above-maximum depths are omitted.
    """

    if T_W_Copt.child_frame != "camera_optical":
        raise ContractError("T_W_Copt child frame must be camera_optical")
    if isinstance(stride, bool) or not isinstance(stride, int) or stride < 1:
        raise ContractError("stride must be a positive integer")
    if not np.isfinite(min_depth_m) or not np.isfinite(max_depth_m):
        raise ContractError("depth bounds must be finite")
    if max_depth_m <= min_depth_m:
        raise ContractError("max_depth_m must be greater than min_depth_m")

    depth_m = decode_depth_to_meters(depth, meters_per_unit=meters_per_unit)
    if depth_m.shape != (intrinsics.height, intrinsics.width):
        raise ContractError("depth shape must match CameraIntrinsics")

    sampled = depth_m[::stride, ::stride]
    mask_flat = None
    if valid_mask is not None:
        mask = np.asarray(valid_mask)
        if mask.shape != depth_m.shape or mask.dtype != np.bool_:
            raise ContractError("valid_mask must be bool with the same shape as depth")
        mask_flat = mask[::stride, ::stride].ravel()
    u = np.arange(0, intrinsics.width, stride, dtype=np.float64)
    v = np.arange(0, intrinsics.height, stride, dtype=np.float64)
    u_grid, v_grid = np.meshgrid(u, v, indexing="xy")
    depth_flat = sampled.ravel()
    x_factor = ((u_grid - intrinsics.cx) / intrinsics.fx).ravel()
    y_factor = ((v_grid - intrinsics.cy) / intrinsics.fy).ravel()

    valid = np.isfinite(depth_flat)
    valid &= depth_flat >= max(float(min_depth_m), _POSITIVE_FLOAT32)
    valid &= depth_flat <= float(max_depth_m)
    if mask_flat is not None:
        valid &= mask_flat
    depth_valid = depth_flat[valid].astype(np.float64, copy=False)
    points_C = np.column_stack(
        (x_factor[valid] * depth_valid, y_factor[valid] * depth_valid, depth_valid)
    )
    points_W = transform_points(points_C, T_W_Copt)
    has_hit = np.ones(points_W.shape[0], dtype=bool)
    return PointCloud(
        xyz_W=points_W,
        sensor_origin_W=T_W_Copt.translation,
        frame_id=T_W_Copt.parent_frame,
        timestamp=timestamp,
        has_hit=has_hit,
        scan_id=scan_id,
    )
