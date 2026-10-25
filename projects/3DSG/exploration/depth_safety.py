"""Local depth-only obstacle check for the upcoming controller step."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .contracts import CameraIntrinsics, Pose3


@dataclass(frozen=True)
class DepthSafetyConfig:
    corridor_half_width_m: float
    floor_z_m: float
    agent_height_m: float
    obstacle_min_height_m: float = 0.05
    max_check_distance_m: float = 1.0
    min_valid_returns: int = 3
    pixel_stride: int = 3

    def __post_init__(self) -> None:
        if (self.corridor_half_width_m <= 0 or self.agent_height_m <= self.obstacle_min_height_m
                or self.max_check_distance_m <= 0 or self.min_valid_returns < 1 or self.pixel_stride < 1):
            raise ValueError("invalid depth safety configuration")


@dataclass(frozen=True)
class DepthSafetyResult:
    frame_id: str
    host_monotonic_s: float
    status: str
    minimum_obstacle_distance_m: float | None
    valid_corridor_returns: int
    check_distance_m: float


class DepthSafetyGate:
    """Project current depth into a forward corridor using explicit pose data."""

    def __init__(self, config: DepthSafetyConfig) -> None:
        self.config = config

    def evaluate(self, *, frame_id: str, host_monotonic_s: float,
                 depth_m: np.ndarray, intrinsics: CameraIntrinsics,
                 T_world_camera: Pose3, robot_xy: tuple[float, float],
                 robot_yaw_rad: float, depth_min_m: float, depth_max_m: float) -> DepthSafetyResult:
        depth = np.asarray(depth_m, dtype=np.float32)
        if depth.shape != (intrinsics.height, intrinsics.width):
            raise ValueError("depth dimensions do not match intrinsics")
        if depth_min_m <= 0 or depth_max_m <= depth_min_m:
            raise ValueError("invalid sensor depth limits")
        stride = self.config.pixel_stride
        vv, uu = np.mgrid[0:intrinsics.height:stride, 0:intrinsics.width:stride]
        z = depth[vv, uu].astype(np.float64, copy=False)
        valid = np.isfinite(z) & (z >= depth_min_m) & (z <= depth_max_m)
        if not np.any(valid):
            return DepthSafetyResult(frame_id, host_monotonic_s, "insufficient_depth", None, 0,
                                     self.config.max_check_distance_m)
        u, v, z = uu[valid].astype(np.float64), vv[valid].astype(np.float64), z[valid]
        camera = np.stack(((u - intrinsics.cx) * z / intrinsics.fx,
                           (v - intrinsics.cy) * z / intrinsics.fy, z, np.ones_like(z)), axis=0)
        world = (T_world_camera.matrix @ camera)[:3].T
        cos_yaw, sin_yaw = math.cos(robot_yaw_rad), math.sin(robot_yaw_rad)
        origin = T_world_camera.position
        origin_delta = origin[:2] - np.asarray(robot_xy, dtype=np.float64)
        forward0 = origin_delta[0] * cos_yaw + origin_delta[1] * sin_yaw
        lateral0 = -origin_delta[0] * sin_yaw + origin_delta[1] * cos_yaw
        delta_xy = world[:, :2] - origin[None, :2]
        forward_delta = delta_xy[:, 0] * cos_yaw + delta_xy[:, 1] * sin_yaw
        lateral_delta = -delta_xy[:, 0] * sin_yaw + delta_xy[:, 1] * cos_yaw
        forward = forward0 + forward_delta
        lateral = lateral0 + lateral_delta
        low_z = self.config.floor_z_m + self.config.obstacle_min_height_m
        high_z = self.config.floor_z_m + self.config.agent_height_m

        # A valid ray ending beyond the check distance certifies visibility to
        # the far plane only when the ray crosses that plane inside the robot's
        # vertical and lateral corridor. A floor/ceiling return by itself is
        # therefore not counted as body-height clearance.
        check_distance = self.config.max_check_distance_m
        intersects_far_plane = forward_delta > 1e-8
        t_far = np.zeros(len(world), dtype=np.float64)
        t_far[intersects_far_plane] = (check_distance - forward0) / forward_delta[intersects_far_plane]
        far_lateral = lateral0 + t_far * lateral_delta
        far_z = origin[2] + t_far * (world[:, 2] - origin[2])
        coverage = (intersects_far_plane & (t_far >= 0.0) & (t_far <= 1.0)
                    & (np.abs(far_lateral) <= self.config.corridor_half_width_m)
                    & (far_z >= low_z) & (far_z <= high_z))

        # Obstacle proximity uses endpoints inside the corridor. Returns past
        # the check plane are free-space evidence, not obstacle locations.
        obstacle = ((forward > 0.0) & (forward <= check_distance)
                    & (np.abs(lateral) <= self.config.corridor_half_width_m)
                    & (world[:, 2] >= low_z) & (world[:, 2] <= high_z))
        coverage_count = int(np.count_nonzero(coverage | obstacle))
        obstacle_count = int(np.count_nonzero(obstacle))
        minimum = float(np.min(forward[obstacle])) if obstacle_count else None
        if (int(np.count_nonzero(coverage)) >= self.config.min_valid_returns
                or obstacle_count >= self.config.min_valid_returns):
            status = "observed"
        else:
            status = "insufficient_depth"
        return DepthSafetyResult(frame_id, host_monotonic_s, status, minimum, coverage_count,
                                 self.config.max_check_distance_m)
