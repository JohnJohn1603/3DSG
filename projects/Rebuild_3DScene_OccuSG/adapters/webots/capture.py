"""Pure adapter functions for Webots Camera/RangeFinder data and node poses.

Webots R2025a Camera/RangeFinder node poses use the FLU device convention
(+X forward, +Y left, +Z up), matching the sensor body frame expected by the
existing simulator transform contract. The shared webots_to_canonical()
function then supplies camera_optical (+X right, +Y down, +Z forward). The
adapter never substitutes the robot/base pose.
"""
from __future__ import annotations

import math

import numpy as np

from standalone_sg.contracts import CameraIntrinsics, Transform
from standalone_sg.geometry.simulator_frames import webots_to_canonical


def camera_intrinsics(width: int, height: int, horizontal_fov_rad: float) -> CameraIntrinsics:
    if type(width) is not int or width < 2 or type(height) is not int or height < 2:
        raise ValueError("Webots camera dimensions must be integer values >= 2")
    fov = float(horizontal_fov_rad)
    if not math.isfinite(fov) or not 0.0 < fov < math.pi:
        raise ValueError("planar Webots horizontal FOV must be finite and in (0, pi)")
    fx = width / (2.0 * math.tan(fov / 2.0))
    fy = height / (2.0 * math.tan(math.atan(math.tan(fov / 2.0) * height / width)))
    return CameraIntrinsics(width, height, fx, fy, (width - 1) / 2.0, (height - 1) / 2.0,
                            calibration_id=f"webots-planar-{width}x{height}-hfov-{fov:.12g}")


def copy_rangefinder_depth_m(buffer: bytes | bytearray | memoryview, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Copy Webots RangeFinder float buffer; values are axial metres, Inf invalid."""
    if type(width) is not int or width <= 0 or type(height) is not int or height <= 0:
        raise ValueError("RangeFinder dimensions must be positive integers")
    raw = np.frombuffer(buffer, dtype=np.float32)
    if raw.size != width * height:
        raise ValueError(f"RangeFinder buffer has {raw.size} floats; expected {width * height}")
    depth_m = raw.reshape(height, width).copy()
    valid = np.isfinite(depth_m) & (depth_m > 0.0)
    return depth_m, valid


def copy_bgra_rgb(buffer: bytes | bytearray | memoryview, width: int, height: int) -> np.ndarray:
    """Copy Webots Camera's top-left BGRA buffer into owned RGB uint8 memory."""
    if type(width) is not int or width <= 0 or type(height) is not int or height <= 0:
        raise ValueError("Camera dimensions must be positive integers")
    raw = np.frombuffer(buffer, dtype=np.uint8)
    expected = width * height * 4
    if raw.size != expected:
        raise ValueError(f"Camera buffer has {raw.size} bytes; expected {expected}")
    bgra = raw.reshape(height, width, 4)
    return bgra[..., (2, 1, 0)].copy()


def webots_camera_node_pose_to_optical(
    pose_row_major: np.ndarray | list[float],
    timestamp_s: float,
    T_world_webots: Transform,
) -> Transform:
    """Convert an actual Supervisor Camera/RangeFinder node pose to T_W_Copt.

    In the pinned post-R2022a Webots device convention, the Camera/RangeFinder
    node pose is FLU (+X forward, +Y left, +Z up), the same sensor-body frame
    accepted by webots_to_canonical. Thus the actual Supervisor pose is used
    directly; no legacy OpenGL-axis conversion is inserted.
    """
    pose = np.asarray(pose_row_major, dtype=np.float64)
    if pose.size != 16:
        raise ValueError("Supervisor node pose must contain 16 row-major values")
    T_W_camera = pose.reshape(4, 4).copy()
    if not np.isfinite(T_W_camera).all():
        raise ValueError("Supervisor node pose must be finite")
    if T_world_webots.parent_frame != "world" or T_world_webots.child_frame != "webots_world":
        raise ValueError("T_world_webots must explicitly map webots_world into canonical world")
    T_webots_sensor = Transform(T_W_camera, "webots_world", "webots_sensor_body",
                                float(timestamp_s), "ground_truth")
    return webots_to_canonical(T_world_webots, T_webots_sensor)
