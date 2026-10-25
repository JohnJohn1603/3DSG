"""Webots R2025a sensor adapter; core receives only canonical frame packets."""

from .capture import (
    camera_intrinsics,
    copy_bgra_rgb,
    copy_rangefinder_depth_m,
    webots_camera_node_pose_to_optical,
)

__all__ = [
    "camera_intrinsics",
    "copy_bgra_rgb",
    "copy_rangefinder_depth_m",
    "webots_camera_node_pose_to_optical",
]
