import json
from pathlib import Path

import numpy as np
import pytest

from standalone_sg.contracts import CameraIntrinsics, ContractError, Transform
from standalone_sg.geometry import backproject_depth, decode_depth_to_meters, radial_range_to_axial_depth


def _intrinsics(width: int = 3, height: int = 3) -> CameraIntrinsics:
    return CameraIntrinsics(width, height, 2.0, 2.0, 1.0, 1.0)


def test_identity_backprojection_matches_axial_pinhole_formula_and_row_major_order() -> None:
    depth = np.full((3, 3), 2.0, dtype=np.float32)
    cloud = backproject_depth(
        depth,
        _intrinsics(),
        Transform.identity(parent_frame="world", child_frame="camera_optical"),
        meters_per_unit=1.0,
        min_depth_m=0.1,
        max_depth_m=10.0,
    )
    expected = np.array(
        [
            [-1.0, -1.0, 2.0],
            [0.0, -1.0, 2.0],
            [1.0, -1.0, 2.0],
            [-1.0, 0.0, 2.0],
            [0.0, 0.0, 2.0],
            [1.0, 0.0, 2.0],
            [-1.0, 1.0, 2.0],
            [0.0, 1.0, 2.0],
            [1.0, 1.0, 2.0],
        ],
        dtype=np.float32,
    )
    assert cloud.xyz_W.dtype == np.float32
    assert np.allclose(cloud.xyz_W, expected, atol=1e-7)
    assert np.all(cloud.has_hit)
    assert np.allclose(cloud.sensor_origin_W, [0.0, 0.0, 0.0])


def test_fixed_fixture_matches_baseline_backprojection_kernel() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/m1_backprojection_reference.json").read_text(encoding="utf-8")
    )
    cloud = backproject_depth(
        np.asarray(fixture["depth_m"], dtype=np.float32),
        CameraIntrinsics(
            fixture["width"], fixture["height"], fixture["fx"], fixture["fy"], fixture["cx"], fixture["cy"]
        ),
        Transform.identity(parent_frame="world", child_frame="camera_optical"),
        meters_per_unit=1.0,
    )
    assert np.allclose(cloud.xyz_W, fixture["expected_xyz_camera"], rtol=0.0, atol=1e-7)


def test_explicit_16uc1_scale_and_invalid_depth_filtering() -> None:
    raw_mm = np.array([[0, 1000, 11000], [0, 500, 65535]], dtype=np.uint16)
    depth_m = decode_depth_to_meters(raw_mm, meters_per_unit=0.001)
    assert depth_m[0, 1] == pytest.approx(1.0)
    intrinsics = CameraIntrinsics(3, 2, 2.0, 2.0, 1.0, 0.5)
    cloud = backproject_depth(
        raw_mm,
        intrinsics,
        Transform.identity(parent_frame="world", child_frame="camera_optical"),
        meters_per_unit=0.001,
        min_depth_m=0.1,
        max_depth_m=10.0,
    )
    assert cloud.xyz_W.shape == (2, 3)
    assert np.allclose(cloud.xyz_W[:, 2], [1.0, 0.5])


def test_valid_mask_is_applied_before_occupancy_input() -> None:
    depth = np.ones((2, 3), dtype=np.float32)
    mask = np.array([[True, False, True], [False, False, True]], dtype=bool)
    cloud = backproject_depth(
        depth,
        CameraIntrinsics(3, 2, 1.0, 1.0, 0.0, 0.0),
        Transform.identity(parent_frame="world", child_frame="camera_optical"),
        meters_per_unit=1.0,
        valid_mask=mask,
    )
    assert cloud.xyz_W.shape == (3, 3)
    assert np.allclose(cloud.xyz_W[:, 2], 1.0)


def test_all_invalid_depth_returns_empty_cloud() -> None:
    cloud = backproject_depth(
        np.zeros((2, 2), dtype=np.uint16),
        CameraIntrinsics(2, 2, 1.0, 1.0, 0.0, 0.0),
        Transform.identity(parent_frame="world", child_frame="camera_optical"),
        meters_per_unit=0.001,
    )
    assert cloud.xyz_W.shape == (0, 3)
    assert cloud.has_hit is not None and cloud.has_hit.shape == (0,)


def test_valid_mask_must_be_bool_and_same_shape() -> None:
    with pytest.raises(ContractError, match="valid_mask"):
        backproject_depth(
            np.ones((2, 2), dtype=np.float32),
            CameraIntrinsics(2, 2, 1.0, 1.0, 0.0, 0.0),
            Transform.identity(parent_frame="world", child_frame="camera_optical"),
            meters_per_unit=1.0,
            valid_mask=np.ones((2, 2), dtype=np.uint8),
        )


def test_stride_and_world_transform_are_applied() -> None:
    depth = np.arange(1, 17, dtype=np.float32).reshape(4, 4)
    matrix = np.eye(4)
    matrix[:3, 3] = [10.0, -2.0, 0.5]
    cloud = backproject_depth(
        depth,
        CameraIntrinsics(4, 4, 1.0, 1.0, 0.0, 0.0),
        Transform(matrix, "world", "camera_optical"),
        meters_per_unit=1.0,
        stride=2,
        min_depth_m=0.1,
        max_depth_m=20.0,
    )
    # Pixels (row, col) = (0,0), (0,2), (2,0), (2,2): depths 1, 3, 9, 11.
    assert np.allclose(cloud.xyz_W[:, 2], [1.5, 3.5, 9.5, 11.5])
    assert np.allclose(cloud.sensor_origin_W, [10.0, -2.0, 0.5])


def test_integer_depth_without_explicit_scale_is_rejected() -> None:
    with pytest.raises(ContractError, match="meters_per_unit must be explicit"):
        decode_depth_to_meters(np.ones((1, 1), dtype=np.uint16), meters_per_unit=None)


def test_radial_range_adapter_produces_axial_depth_before_backprojection() -> None:
    intrinsics = _intrinsics(3, 3)
    radial = np.full((3, 3), 2.0, dtype=np.float32)
    axial = radial_range_to_axial_depth(radial, intrinsics)
    assert axial[1, 1] == pytest.approx(2.0)
    assert axial[0, 0] == pytest.approx(2.0 / np.sqrt(1.5))
    invalid = radial_range_to_axial_depth(np.full((3, 3), np.inf), intrinsics)
    assert np.isnan(invalid[0, 0])
