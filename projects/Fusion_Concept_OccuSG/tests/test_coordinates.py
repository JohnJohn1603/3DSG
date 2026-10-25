import numpy as np
import pytest

from standalone_sg.contracts import CameraIntrinsics, ContractError, Transform
from standalone_sg.geometry import (
    cell_center_xy,
    habitat_to_canonical,
    inverse_transform,
    webots_to_canonical,
    world_xy_to_cell,
)
from standalone_sg.geometry.transforms import transform_points


def test_transform_round_trip_and_sensor_translation() -> None:
    yaw = np.pi / 2.0
    matrix = np.eye(4)
    matrix[:3, :3] = np.array(
        [[np.cos(yaw), -np.sin(yaw), 0.0], [np.sin(yaw), np.cos(yaw), 0.0], [0.0, 0.0, 1.0]]
    )
    matrix[:3, 3] = [1.0, 2.0, 3.0]
    transform = Transform(matrix, "world", "camera_optical")
    point = np.array([[1.0, 0.0, 2.0]])
    world = transform_points(point, transform)
    assert np.allclose(world, [[1.0, 3.0, 5.0]], atol=1e-7)
    assert np.allclose(transform_points(world, inverse_transform(transform)), point, atol=1e-6)
    assert np.allclose(transform.translation, [1.0, 2.0, 3.0])


def test_transform_rejects_reflection() -> None:
    reflected = np.diag([-1.0, 1.0, 1.0, 1.0])
    with pytest.raises(ContractError, match="determinant"):
        Transform(reflected, "world", "camera_optical")


@pytest.mark.parametrize(
    "bad_rotation",
    [
        np.diag([1.000004, 1.0, 1.0]),
        np.array([[1.0, 0.01, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
    ],
)
def test_transform_rejects_scale_or_shear_with_zero_relative_tolerance(bad_rotation: np.ndarray) -> None:
    with pytest.raises(ContractError, match="orthonormal"):
        Transform(np.block([[bad_rotation, np.zeros((3, 1))], [np.zeros((1, 3)), np.ones((1, 1))]]), "world", "camera_optical")


def _rpy(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def _transform(rotation: np.ndarray, translation: list[float], parent: str, child: str) -> Transform:
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = translation
    return Transform(matrix, parent, child)


def test_habitat_pitch_roll_chain_and_sensor_extrinsics() -> None:
    T_W_H = _transform(_rpy(0.2, -0.3, 0.4), [10.0, -3.0, 1.5], "world", "habitat")
    T_H_S = _transform(_rpy(-0.1, 0.25, -0.2), [0.4, 0.1, 1.2], "habitat", "sensor")
    result = habitat_to_canonical(T_W_H, T_H_S)
    optical = np.eye(4)
    optical[:3, :3] = np.diag([1.0, -1.0, -1.0])
    assert result.parent_frame == "world"
    assert result.child_frame == "camera_optical"
    assert np.allclose(result.matrix, T_W_H.matrix @ T_H_S.matrix @ optical, rtol=0.0, atol=1e-12)
    assert not np.allclose(result.translation, T_W_H.translation)


def test_webots_pitch_roll_chain_and_sensor_extrinsics() -> None:
    T_W_Webots = _transform(_rpy(-0.15, 0.1, 0.7), [2.0, 3.0, 0.2], "world", "webots")
    T_Webots_S = _transform(_rpy(0.05, -0.2, 0.1), [0.3, -0.1, 0.9], "webots", "sensor")
    result = webots_to_canonical(T_W_Webots, T_Webots_S)
    optical = np.eye(4)
    optical[:3, :3] = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
    assert np.allclose(result.matrix, T_W_Webots.matrix @ T_Webots_S.matrix @ optical, rtol=0.0, atol=1e-12)


def test_grid_uses_floor_for_negative_coordinates_and_row_col_order() -> None:
    origin = (0.0, 0.0)
    assert world_xy_to_cell(-0.001, -0.001, origin_xy=origin, resolution=0.05) == (-1, -1)
    assert world_xy_to_cell(0.051, 0.101, origin_xy=origin, resolution=0.05) == (2, 1)
    assert cell_center_xy(2, 1, origin_xy=origin, resolution=0.05) == pytest.approx((0.075, 0.125))


def test_intrinsics_matrix_and_validation() -> None:
    intrinsics = CameraIntrinsics(3, 2, 2.0, 3.0, 1.0, 0.5)
    assert np.allclose(intrinsics.matrix, [[2.0, 0.0, 1.0], [0.0, 3.0, 0.5], [0.0, 0.0, 1.0]])
    with pytest.raises(ContractError, match="positive"):
        CameraIntrinsics(3, 2, 0.0, 3.0, 1.0, 0.5)
