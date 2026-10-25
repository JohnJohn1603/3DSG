from __future__ import annotations

import numpy as np

from adapters.habitat.frames import (
    habitat_camera_intrinsics,
    habitat_world_to_canonical_transform,
    normalize_habitat_depth,
    normalize_habitat_rgb,
)


def test_habitat_world_conversion_is_explicit_z_up_rigid_transform():
    transform = habitat_world_to_canonical_transform()
    assert transform.parent_frame == "world"
    assert transform.child_frame == "habitat_world"
    assert np.allclose(transform.matrix[:3, :3].T @ transform.matrix[:3, :3], np.eye(3), rtol=0.0, atol=1e-12)
    assert np.isclose(np.linalg.det(transform.matrix[:3, :3]), 1.0, rtol=0.0, atol=1e-12)
    assert np.allclose(transform.matrix @ [0.0, 1.0, 0.0, 1.0], [0.0, 0.0, 1.0, 1.0], rtol=0.0, atol=1e-12)


def test_habitat_intrinsics_and_depth_contract_are_metric_axial():
    intrinsics = habitat_camera_intrinsics(160, 120, 90.0, calibration_id="test")
    assert intrinsics.fx > 0 and intrinsics.fy > 0
    raw = np.array([[0.0, 1.0, 2.0, np.inf], [np.nan, 11.0, 3.0, -1.0]], dtype=np.float32)
    depth, mask = normalize_habitat_depth(raw, min_depth_m=0.1, max_depth_m=10.0)
    assert mask.tolist() == [[False, True, True, False], [False, False, True, False]]
    assert np.isnan(depth[0, 0]) and np.isfinite(depth[0, 1])


def test_habitat_color_drops_alpha_without_aliasing():
    rgba = np.zeros((2, 3, 4), dtype=np.uint8)
    rgba[..., 0] = 7
    rgb = normalize_habitat_rgb(rgba)
    assert rgb.shape == (2, 3, 3)
    assert rgb[0, 0].tolist() == [7, 0, 0]
    rgba[0, 0, 0] = 99
    assert rgb[0, 0, 0] == 7
