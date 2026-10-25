"""Rigid-transform helpers following the Phase 1 coordinate convention."""

from __future__ import annotations

import numpy as np

from ..contracts.errors import ContractError
from ..contracts.frames import Transform


def transform_points(points: np.ndarray, transform: Transform) -> np.ndarray:
    """Apply T_parent_child to an ``(N, 3)`` point array."""

    points_array = np.asarray(points, dtype=np.float64)
    if points_array.ndim != 2 or points_array.shape[1] != 3:
        raise ContractError("points must have shape (N, 3)")
    if not np.all(np.isfinite(points_array)):
        raise ContractError("points must be finite")
    transformed = points_array @ transform.matrix[:3, :3].T + transform.matrix[:3, 3]
    return np.asarray(transformed, dtype=np.float32)


def inverse_transform(transform: Transform) -> Transform:
    """Return the inverse rigid transform with swapped frame IDs."""

    inverse_matrix = np.eye(4, dtype=np.float64)
    rotation = transform.matrix[:3, :3]
    inverse_matrix[:3, :3] = rotation.T
    inverse_matrix[:3, 3] = -rotation.T @ transform.matrix[:3, 3]
    return Transform(
        inverse_matrix,
        parent_frame=transform.child_frame,
        child_frame=transform.parent_frame,
        timestamp=transform.timestamp,
        source=transform.source,
    )

