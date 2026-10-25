"""Versioned value contracts shared by the online navigation components."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import math
from typing import Mapping

import numpy as np


def _finite(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class CameraIntrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("fx", "fy", "cx", "cy"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.fx <= 0 or self.fy <= 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("focal lengths and image dimensions must be positive")


@dataclass(frozen=True)
class Pose3:
    """Rigid transform in canonical world Z-up from a named source frame."""

    matrix: np.ndarray = field(repr=False, compare=False)
    frame_id: str = ""

    def __post_init__(self) -> None:
        matrix = np.asarray(self.matrix, dtype=np.float64)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
            raise ValueError("pose matrix must be a finite 4x4 matrix")
        if not np.allclose(matrix[3], (0.0, 0.0, 0.0, 1.0), atol=1e-7):
            raise ValueError("pose matrix must be homogeneous")
        if not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-3):
            raise ValueError("pose rotation must be orthonormal")
        if not np.isclose(np.linalg.det(matrix[:3, :3]), 1.0, atol=1e-3):
            raise ValueError("pose rotation must be right-handed")
        matrix = matrix.copy()
        matrix.setflags(write=False)
        object.__setattr__(self, "matrix", matrix)

    @property
    def position(self) -> np.ndarray:
        return self.matrix[:3, 3].copy()


@dataclass(frozen=True)
class RobotPose2:
    x_m: float
    y_m: float
    yaw_rad: float

    def __post_init__(self) -> None:
        for name in ("x_m", "y_m", "yaw_rad"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))


class GridState(IntEnum):
    UNKNOWN = 0
    FREE = 1
    OCCUPIED = 2


@dataclass(frozen=True)
class FramePacket:
    """One RGB-D observation; depth is axial metres in optical +Z."""

    run_id: str
    scene_id: str
    frame_id: str
    frame_index: int
    simulator_time_s: float
    host_monotonic_s: float
    depth_m: np.ndarray = field(repr=False, compare=False)
    intrinsics: CameraIntrinsics
    T_world_camera: Pose3
    floor_z_m: float
    calibration_hash: str
    depth_min_m: float
    depth_max_m: float
    rgb_ref: str | None = None
    capture_wall_time_unix_s: float | None = None
    capture_monotonic_domain: str | None = None
    nav_depth_m: np.ndarray | None = field(default=None, repr=False, compare=False)
    nav_depth_intrinsics: CameraIntrinsics | None = None
    T_world_nav_depth_camera: Pose3 | None = None
    nav_depth_min_m: float | None = None
    nav_depth_max_m: float | None = None

    def __post_init__(self) -> None:
        if not all((self.run_id, self.scene_id, self.frame_id, self.calibration_hash)):
            raise ValueError("run, scene, frame, and calibration IDs are required")
        if self.frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        for name in ("simulator_time_s", "host_monotonic_s", "floor_z_m", "depth_min_m", "depth_max_m"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        clock_fields = (self.capture_wall_time_unix_s, self.capture_monotonic_domain)
        if any(value is not None for value in clock_fields):
            if any(value is None for value in clock_fields):
                raise ValueError("capture wall time and monotonic clock domain must be provided together")
            object.__setattr__(self, "capture_wall_time_unix_s",
                               _finite(self.capture_wall_time_unix_s, "capture_wall_time_unix_s"))
            if not str(self.capture_monotonic_domain).startswith("host_monotonic:"):
                raise ValueError("capture_monotonic_domain must identify a host monotonic clock")
        if self.depth_min_m <= 0 or self.depth_max_m <= self.depth_min_m:
            raise ValueError("depth range must be positive and ordered")
        depth = np.asarray(self.depth_m)
        if depth.shape != (self.intrinsics.height, self.intrinsics.width):
            raise ValueError("depth dimensions do not match intrinsics")
        if depth.dtype.kind not in "fiu":
            raise ValueError("depth must be a numeric array")
        depth = depth.astype(np.float32, copy=False)
        depth.setflags(write=False)
        object.__setattr__(self, "depth_m", depth)
        nav_fields = (self.nav_depth_m, self.nav_depth_intrinsics,
                      self.T_world_nav_depth_camera, self.nav_depth_min_m,
                      self.nav_depth_max_m)
        if any(value is not None for value in nav_fields):
            if any(value is None for value in nav_fields):
                raise ValueError("navigation depth requires image, intrinsics, pose, and range")
            nav_min = _finite(self.nav_depth_min_m, "nav_depth_min_m")
            nav_max = _finite(self.nav_depth_max_m, "nav_depth_max_m")
            if nav_min <= 0 or nav_max <= nav_min:
                raise ValueError("navigation depth range must be positive and ordered")
            nav_depth = np.asarray(self.nav_depth_m)
            if nav_depth.shape != (self.nav_depth_intrinsics.height,
                                  self.nav_depth_intrinsics.width):
                raise ValueError("navigation depth dimensions do not match intrinsics")
            if nav_depth.dtype.kind not in "fiu":
                raise ValueError("navigation depth must be a numeric array")
            nav_depth = nav_depth.astype(np.float32, copy=False)
            nav_depth.setflags(write=False)
            object.__setattr__(self, "nav_depth_m", nav_depth)
            object.__setattr__(self, "nav_depth_min_m", nav_min)
            object.__setattr__(self, "nav_depth_max_m", nav_max)


@dataclass(frozen=True)
class GridSnapshot:
    revision: int
    as_of_frame_id: str
    as_of_frame_index: int
    host_monotonic_s: float
    resolution_m: float
    origin_x_m: float
    origin_y_m: float
    states: np.ndarray = field(repr=False, compare=False)
    floor_support: np.ndarray = field(repr=False, compare=False)
    clearance_evidence: np.ndarray = field(repr=False, compare=False)
    spawn_validated: np.ndarray = field(repr=False, compare=False)
    occupancy_log_odds: np.ndarray = field(repr=False, compare=False)
    scope_id: str = "observed_single_level"
    world_frame: str = "canonical_world_z_up"
    spawn_center_xy: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.revision < 0 or self.as_of_frame_index < 0:
            raise ValueError("revision and frame index must be non-negative")
        if not self.as_of_frame_id:
            raise ValueError("as_of_frame_id is required")
        for name in ("resolution_m", "origin_x_m", "origin_y_m", "host_monotonic_s"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.resolution_m <= 0:
            raise ValueError("resolution_m must be positive")
        if self.spawn_center_xy is not None:
            if len(self.spawn_center_xy) != 2 or not all(math.isfinite(float(v)) for v in self.spawn_center_xy):
                raise ValueError("spawn_center_xy must contain two finite coordinates")
        shapes = [np.asarray(getattr(self, name)).shape for name in
                  ("states", "floor_support", "clearance_evidence", "spawn_validated", "occupancy_log_odds")]
        if len(shapes[0]) != 2 or any(shape != shapes[0] for shape in shapes):
            raise ValueError("all map arrays must share a non-empty 2D shape")
        for name in ("states", "floor_support", "clearance_evidence", "spawn_validated", "occupancy_log_odds"):
            arr = np.asarray(getattr(self, name)).copy()
            arr.setflags(write=False)
            object.__setattr__(self, name, arr)

    @property
    def shape(self) -> tuple[int, int]:
        return self.states.shape

    def world_to_cell(self, x_m: float, y_m: float) -> tuple[int, int] | None:
        col = math.floor((float(x_m) - self.origin_x_m) / self.resolution_m)
        row = math.floor((float(y_m) - self.origin_y_m) / self.resolution_m)
        if 0 <= row < self.states.shape[0] and 0 <= col < self.states.shape[1]:
            return row, col
        return None

    def cell_to_world(self, row: int, col: int) -> tuple[float, float]:
        if not (0 <= row < self.states.shape[0] and 0 <= col < self.states.shape[1]):
            raise IndexError("cell is outside map")
        return (self.origin_x_m + (col + 0.5) * self.resolution_m,
                self.origin_y_m + (row + 0.5) * self.resolution_m)


@dataclass(frozen=True)
class PathResult:
    success: bool
    cells: tuple[tuple[int, int], ...]
    cost_m: float
    map_revision: int
    reason: str = ""
    grid_origin_x_m: float | None = None
    grid_origin_y_m: float | None = None
    grid_resolution_m: float | None = None


@dataclass(frozen=True)
class ActionCommand:
    linear_mps: float
    yaw_rate_radps: float
    reason: str
    frame_id: str
    map_revision: int
    valid_until_monotonic_s: float
    metadata: Mapping[str, float | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("linear_mps", "yaw_rate_radps", "valid_until_monotonic_s"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
