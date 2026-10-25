"""Explicit Habitat/Webots sensor-frame chains into the canonical frame."""

from __future__ import annotations

import numpy as np

from ..contracts.frames import Transform


def _rotation_transform(rotation: np.ndarray, parent: str, child: str) -> Transform:
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = rotation
    return Transform(matrix, parent, child)


def _compose(first: Transform, second: Transform) -> Transform:
    if first.child_frame != second.parent_frame:
        raise ValueError(f"frame chain mismatch: {first.child_frame} != {second.parent_frame}")
    return Transform(
        first.matrix @ second.matrix,
        parent_frame=first.parent_frame,
        child_frame=second.child_frame,
        timestamp=second.timestamp if second.timestamp is not None else first.timestamp,
        source=second.source,
    )


def habitat_to_canonical(T_W_H: Transform, T_H_S: Transform) -> Transform:
    """Compose Habitat H→world and actual sensor H→S into W→camera_optical."""

    sensor_to_optical = _rotation_transform(
        np.diag([1.0, -1.0, -1.0]), T_H_S.child_frame, "camera_optical"
    )
    return _compose(_compose(T_W_H, T_H_S), sensor_to_optical)


def webots_to_canonical(T_W_Webots: Transform, T_Webots_S: Transform) -> Transform:
    """Compose Webots world/sensor poses with the pinned optical convention."""

    sensor_to_optical = _rotation_transform(
        np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]]),
        T_Webots_S.child_frame,
        "camera_optical",
    )
    return _compose(_compose(T_W_Webots, T_Webots_S), sensor_to_optical)

