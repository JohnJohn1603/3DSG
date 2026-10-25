"""Sensor and transform contracts used by M1.

The contracts deliberately contain no ROS types. Arrays are copied and marked
read-only at the boundary so downstream stages cannot mutate a frame in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from .errors import ContractError


def _readonly_array(value: np.ndarray, *, dtype: np.dtype | None = None) -> np.ndarray:
    array = np.array(value, dtype=dtype, copy=True)
    array.setflags(write=False)
    return array


def _finite(value: float, name: str) -> None:
    if not np.isfinite(value):
        raise ContractError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class CameraIntrinsics:
    """Pinhole intrinsics for one already-rectified image shape."""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    model: Literal["pinhole"] = "pinhole"
    distortion: tuple[float, ...] = ()
    calibration_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.width, int) or not isinstance(self.height, int):
            raise ContractError("width and height must be integers")
        if isinstance(self.width, bool) or self.width < 1:
            raise ContractError("width must be a positive integer")
        if isinstance(self.height, bool) or self.height < 1:
            raise ContractError("height must be a positive integer")
        if self.model != "pinhole":
            raise ContractError(f"unsupported camera model: {self.model!r}")
        if self.distortion:
            raise ContractError("distortion is unsupported; input must already be rectified")
        for name, value in (("fx", self.fx), ("fy", self.fy), ("cx", self.cx), ("cy", self.cy)):
            _finite(float(value), name)
        if self.fx <= 0 or self.fy <= 0:
            raise ContractError("fx and fy must be positive")
        if any(not np.isfinite(v) for v in self.distortion):
            raise ContractError("distortion coefficients must be finite")

    @property
    def matrix(self) -> np.ndarray:
        matrix = np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        matrix.setflags(write=False)
        return matrix


@dataclass(frozen=True, slots=True)
class Transform:
    """Rigid transform T_parent_child using column vectors and left multiplication."""

    matrix: np.ndarray
    parent_frame: str
    child_frame: str
    timestamp: float | None = None
    source: Literal["ground_truth", "slam", "odometry", "calibration"] = "ground_truth"

    def __post_init__(self) -> None:
        matrix = _readonly_array(self.matrix, dtype=np.float64)
        if matrix.shape != (4, 4):
            raise ContractError("transform matrix must have shape (4, 4)")
        if not np.all(np.isfinite(matrix)):
            raise ContractError("transform matrix must be finite")
        if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], rtol=0.0, atol=1e-12):
            raise ContractError("transform last row must be [0, 0, 0, 1]")
        rotation = matrix[:3, :3]
        if not np.allclose(rotation.T @ rotation, np.eye(3), rtol=0.0, atol=1e-9):
            raise ContractError("transform rotation must be orthonormal")
        if not np.isclose(np.linalg.det(rotation), 1.0, rtol=0.0, atol=1e-9):
            raise ContractError("transform rotation must have determinant +1")
        if not self.parent_frame or not self.child_frame:
            raise ContractError("parent_frame and child_frame are required")
        if self.timestamp is not None:
            _finite(float(self.timestamp), "timestamp")
        object.__setattr__(self, "matrix", matrix)

    @property
    def translation(self) -> np.ndarray:
        value = self.matrix[:3, 3].copy()
        value.setflags(write=False)
        return value

    @classmethod
    def identity(
        cls,
        *,
        parent_frame: str = "world",
        child_frame: str = "camera_optical",
        timestamp: float | None = None,
    ) -> "Transform":
        return cls(np.eye(4, dtype=np.float64), parent_frame, child_frame, timestamp)


@dataclass(frozen=True, slots=True)
class PointCloud:
    """Canonical world-frame point cloud produced from one sensor observation."""

    xyz_W: np.ndarray
    sensor_origin_W: np.ndarray
    frame_id: str
    timestamp: float | None = None
    has_hit: np.ndarray | None = None
    scan_id: str | None = None

    def __post_init__(self) -> None:
        points = _readonly_array(self.xyz_W, dtype=np.float32)
        origin = _readonly_array(self.sensor_origin_W, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ContractError("xyz_W must have shape (N, 3)")
        if origin.shape != (3,):
            raise ContractError("sensor_origin_W must have shape (3,)")
        if not np.all(np.isfinite(points)) or not np.all(np.isfinite(origin)):
            raise ContractError("point cloud coordinates must be finite")
        if not self.frame_id:
            raise ContractError("frame_id is required")
        if self.timestamp is not None:
            _finite(float(self.timestamp), "timestamp")
        hit = None
        if self.has_hit is not None:
            hit = np.array(self.has_hit, dtype=bool, copy=True)
            hit.setflags(write=False)
            if hit.shape != (points.shape[0],):
                raise ContractError("has_hit must have shape (N,)")
        object.__setattr__(self, "xyz_W", points)
        object.__setattr__(self, "sensor_origin_W", origin)
        object.__setattr__(self, "has_hit", hit)


@dataclass(frozen=True, slots=True)
class RGBDFrame:
    """Synchronized RGB-D frame after adapter normalization."""

    depth_m: np.ndarray
    intrinsics: CameraIntrinsics
    T_W_Copt: Transform
    timestamp: float
    rgb: np.ndarray | None = None
    valid_mask: np.ndarray | None = None
    frame_id: str = ""

    def __post_init__(self) -> None:
        depth = _readonly_array(self.depth_m, dtype=np.float32)
        if depth.shape != (self.intrinsics.height, self.intrinsics.width):
            raise ContractError("depth_m shape must match CameraIntrinsics")
        if self.T_W_Copt.child_frame != "camera_optical":
            raise ContractError("T_W_Copt child frame must be camera_optical")
        _finite(float(self.timestamp), "timestamp")
        mask = None
        if self.valid_mask is not None:
            mask = np.array(self.valid_mask, dtype=bool, copy=True)
            mask.setflags(write=False)
            if mask.shape != depth.shape:
                raise ContractError("valid_mask must match depth_m shape")
        rgb = None
        if self.rgb is not None:
            rgb = _readonly_array(self.rgb, dtype=np.uint8)
            if rgb.shape != (self.intrinsics.height, self.intrinsics.width, 3):
                raise ContractError("rgb must have shape (H, W, 3)")
        object.__setattr__(self, "depth_m", depth)
        object.__setattr__(self, "valid_mask", mask)
        object.__setattr__(self, "rgb", rgb)
