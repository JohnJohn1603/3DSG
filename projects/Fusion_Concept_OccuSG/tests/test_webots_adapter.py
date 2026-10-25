import math

import numpy as np
import pytest

from adapters.webots.capture import (
    camera_intrinsics,
    copy_bgra_rgb,
    copy_rangefinder_depth_m,
    webots_camera_node_pose_to_optical,
)
from standalone_sg.contracts import RGBDFrame
from standalone_sg.contracts import Transform
from standalone_sg.geometry import backproject_depth


def _sensor_pose_facing_world_x(position=(0.25, 0.0, 1.0)):
    # R2025a device pose is FLU; identity sensor rotation faces world +X.
    T = np.eye(4)
    T[:3, 3] = position
    return T.reshape(-1)


def test_webots_depth_copy_preserves_meters_and_marks_inf_invalid():
    original = np.array([2.7, np.inf, 0.0, 1.25], dtype=np.float32)
    depth, valid = copy_rangefinder_depth_m(original.tobytes(), 2, 2)
    original[:] = 77.0
    assert depth.shape == (2, 2)
    assert depth[0, 0] == pytest.approx(2.7)
    assert np.isinf(depth[0, 1])
    assert valid.tolist() == [[True, False], [False, True]]
    assert depth.flags.owndata


def test_webots_bgra_copy_reorders_channels_and_owns_buffer():
    bgra = bytearray([3, 2, 1, 255, 30, 20, 10, 0])
    rgb = copy_bgra_rgb(bgra, 2, 1)
    bgra[:] = bytes([0] * 8)
    assert rgb.tolist() == [[[1, 2, 3], [10, 20, 30]]]
    assert rgb.flags.owndata


def test_webots_fov_intrinsics_use_planar_square_pixel_convention():
    intr = camera_intrinsics(5, 3, math.pi / 2)
    assert intr.fx == pytest.approx(2.5)
    assert intr.fy == pytest.approx(2.5)
    assert (intr.cx, intr.cy) == pytest.approx((2.0, 1.0))


def test_actual_webots_camera_node_pose_backprojects_known_wall_point():
    timestamp = 3.25
    # This identity is fixture-specific: WorldInfo is ENU, SI metres, and its
    # origin/axes are explicitly the canonical-world origin/axes.
    T_world_webots = Transform(np.eye(4), "world", "webots_world", source="calibration")
    T_W_Copt = webots_camera_node_pose_to_optical(
        _sensor_pose_facing_world_x(), timestamp, T_world_webots
    )
    assert T_W_Copt.parent_frame == "world"
    assert T_W_Copt.child_frame == "camera_optical"
    assert T_W_Copt.source == "ground_truth"
    assert T_W_Copt.timestamp == timestamp
    assert T_W_Copt.matrix[:3, 2] == pytest.approx([1.0, 0.0, 0.0])

    depth = np.full((3, 5), np.nan, dtype=np.float32)
    depth[1, 2] = 2.7
    mask = np.isfinite(depth)
    intr = camera_intrinsics(5, 3, math.pi / 2)
    frame = RGBDFrame(depth, intr, T_W_Copt, timestamp, valid_mask=mask, frame_id="calibration-0")
    cloud = backproject_depth(frame.depth_m, frame.intrinsics, frame.T_W_Copt,
                              min_depth_m=0.1, max_depth_m=5.0, meters_per_unit=1.0,
                              valid_mask=frame.valid_mask)
    assert cloud.xyz_W.shape == (1, 3)
    # Wall front face is x=2.95; camera is offset to x=0.25 and faces +X.
    assert cloud.xyz_W[0] == pytest.approx([2.95, 0.0, 1.0], abs=1e-6)
    assert cloud.sensor_origin_W == pytest.approx([0.25, 0.0, 1.0], abs=1e-12)


def test_webots_world_transform_is_explicit_and_frame_checked():
    pose = _sensor_pose_facing_world_x()
    with pytest.raises(TypeError):
        webots_camera_node_pose_to_optical(pose, 1.0)
    wrong_chain = Transform(np.eye(4), "world", "other_world")
    with pytest.raises(ValueError, match="T_world_webots"):
        webots_camera_node_pose_to_optical(pose, 1.0, wrong_chain)

    translated = np.eye(4)
    translated[0, 3] = 10.0
    T_world_webots = Transform(translated, "world", "webots_world")
    result = webots_camera_node_pose_to_optical(pose, 1.0, T_world_webots)
    assert result.translation == pytest.approx([10.25, 0.0, 1.0])
