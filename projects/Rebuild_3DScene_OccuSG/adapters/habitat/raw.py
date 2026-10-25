"""NumPy-only Habitat capture math usable from the pinned Python 3.9 env."""
from __future__ import annotations

import numpy as np


def canonical_from_habitat_matrix() -> np.ndarray:
    matrix = np.array(
        [[1.0, 0.0, 0.0, 0.0],
         [0.0, 0.0, -1.0, 0.0],
         [0.0, 1.0, 0.0, 0.0],
         [0.0, 0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    return matrix


def pose_matrix(position, rotation_matrix) -> np.ndarray:
    position = np.asarray(position, dtype=np.float64)
    rotation = np.asarray(rotation_matrix, dtype=np.float64)
    if position.shape != (3,) or rotation.shape != (3, 3):
        raise ValueError("Habitat pose must contain a 3-vector and 3x3 rotation matrix")
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = position
    return matrix

