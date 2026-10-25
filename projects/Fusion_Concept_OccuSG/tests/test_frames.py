import numpy as np
import pytest

from standalone_sg.contracts import CameraIntrinsics, ContractError, RGBDFrame, Transform


def test_rgbd_frame_accepts_invalid_sensor_samples_but_freezes_arrays() -> None:
    intrinsics = CameraIntrinsics(2, 1, 1.0, 1.0, 0.5, 0.0)
    frame = RGBDFrame(
        depth_m=np.array([[1.0, np.inf]], dtype=np.float32),
        intrinsics=intrinsics,
        T_W_Copt=Transform.identity(parent_frame="world", child_frame="camera_optical"),
        timestamp=1.25,
        valid_mask=np.array([[True, False]]),
        frame_id="frame-0001",
    )
    assert frame.depth_m.flags.writeable is False
    assert frame.valid_mask is not None and frame.valid_mask.flags.writeable is False
    assert frame.depth_m[0, 1] == np.inf


def test_rgbd_frame_requires_camera_optical_child_frame() -> None:
    intrinsics = CameraIntrinsics(1, 1, 1.0, 1.0, 0.0, 0.0)
    with pytest.raises(ContractError, match="camera_optical"):
        RGBDFrame(
            depth_m=np.ones((1, 1), dtype=np.float32),
            intrinsics=intrinsics,
            T_W_Copt=Transform.identity(parent_frame="world", child_frame="camera"),
            timestamp=0.0,
        )


def test_distorted_intrinsics_are_rejected_until_rectified() -> None:
    with pytest.raises(ContractError, match="rectified"):
        CameraIntrinsics(1, 1, 1.0, 1.0, 0.0, 0.0, distortion=(0.1, 0.0, 0.0, 0.0, 0.0))
