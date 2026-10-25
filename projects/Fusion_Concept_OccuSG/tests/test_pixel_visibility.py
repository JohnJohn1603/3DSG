import numpy as np
import pytest

from adapters.habitat.frames import habitat_camera_intrinsics
from standalone_sg.contracts import CameraIntrinsics, Transform
from standalone_sg.geometry.backproject import backproject_depth
from standalone_sg.objects.pixel_visibility import (
    enforce_available_by_decision,
    project_world_points,
    score_prior_surface_visibility,
)


def setup_target(size=7):
    depth = np.full((size, size), np.nan, dtype=np.float32)
    valid = np.zeros((size, size), dtype=bool)
    mask = np.zeros((size, size), dtype=bool)
    T_W_C = np.eye(4, dtype=np.float64)
    return depth, valid, mask, T_W_C


def score(points, depth, valid, mask, pose=None, tolerance=0.05):
    return score_prior_surface_visibility(
        points, np.eye(4) if pose is None else pose, depth, valid, mask,
        CameraIntrinsics(depth.shape[1], depth.shape[0], 1, 1, 3, 3),
        depth_tolerance_m=tolerance,
    )


def test_camera_transform_and_axial_depth_match_at_projected_pixel():
    depth, valid, mask, T = setup_target()
    # Camera translated +1m in world X; point at world X=1 projects to center.
    T[0, 3] = 1.0
    depth[3, 3] = 2.0
    valid[3, 3] = True
    mask[3, 3] = True
    result = score(np.array([[1.0, 0.0, 2.0]]), depth, valid, mask, pose=T)
    assert result["counts"]["support"] == 1
    assert result["pixels"][0]["projected_axial_depth_m"] == 2.0


def test_nearer_observed_surface_is_occlusion_not_contradiction():
    depth, valid, mask, _ = setup_target()
    depth[3, 3] = 1.0
    valid[3, 3] = True
    mask[3, 3] = True
    result = score(np.array([[0.0, 0.0, 2.0]]), depth, valid, mask)
    assert result["counts"]["occluded"] == 1
    assert result["counts"]["depth_contradiction"] == 0


def test_farther_valid_depth_is_depth_contradiction():
    depth, valid, mask, _ = setup_target()
    depth[3, 3] = 2.5
    valid[3, 3] = True
    result = score(np.array([[0.0, 0.0, 2.0]]), depth, valid, mask)
    assert result["counts"]["depth_contradiction"] == 1


def test_outside_image_and_invalid_depth_are_unknown():
    depth, valid, mask, _ = setup_target()
    # x/z=10 -> outside. Center projection has invalid depth.
    points = np.array([[20.0, 0.0, 2.0], [0.0, 0.0, 2.0]])
    result = score(points, depth, valid, mask)
    assert result["counts"]["outside_image"] == 1
    assert result["counts"]["invalid_depth"] == 1
    assert result["counts"]["depth_contradiction"] == 0


def test_prior_surface_zbuffer_keeps_nearest_and_reports_hidden_sample():
    depth, valid, mask, _ = setup_target()
    depth[3, 3] = 2.0
    valid[3, 3] = True
    mask[3, 3] = True
    result = score(np.array([[0.0, 0.0, 2.0], [0.0, 0.0, 3.0]]), depth, valid, mask)
    assert result["counts"]["projected_prior_samples"] == 1
    assert result["counts"]["prior_zbuffer_occluded"] == 1
    assert result["counts"]["support"] == 1


def test_depth_agreement_outside_predicted_mask_is_diagnostic_not_identity_veto():
    depth, valid, mask, _ = setup_target()
    depth[3, 3] = 2.0
    valid[3, 3] = True
    result = score(np.array([[0.0, 0.0, 2.0]]), depth, valid, mask)
    assert result["counts"]["depth_agreement_outside_mask"] == 1
    assert result["identity_decision"] is None
    assert result["diagnostic_only"] is True


def test_partial_surface_does_not_treat_new_or_unseen_area_as_identity_evidence():
    depth, valid, mask, _ = setup_target()
    # Current view contains a new surface at another pixel; no prior projection there.
    depth[3, 3] = 2.0
    valid[3, 3] = True
    mask[3, 3] = True
    depth[3, 4] = 1.5
    valid[3, 4] = True
    mask[3, 4] = True
    result = score(np.array([[0.0, 0.0, 2.0]]), depth, valid, mask)
    assert result["counts"]["support"] == 1
    assert len(result["pixels"]) == 1  # pixel 3,4 is outside prior projection: unknown
    assert result["identity_decision"] is None


def test_touching_coplanar_and_mixed_mask_inputs_never_emit_identity_decision():
    depth, valid, mask, _ = setup_target()
    depth[3, 3:5] = 2.0
    valid[3, 3:5] = True
    mask[3, 3:5] = True  # may include two coplanar/touching objects
    points = np.array([[0.0, 0.0, 2.0], [1.0, 0.0, 2.0]])
    first = score(points, depth, valid, mask)
    # Whether those pixels belong to one or two physical objects is absent from the input.
    second = score(points.copy(), depth.copy(), valid.copy(), mask.copy())
    assert first["counts"] == second["counts"]
    assert first["identity_decision"] is None


def test_mask_with_extra_separate_object_does_not_become_safe_fusion_evidence():
    depth, valid, mask, _ = setup_target()
    # Prior surface projects to pixel (3,3); a second object contributes a
    # separate mask/depth region at (3,5), which has no prior projection.
    depth[3, 3] = 2.0
    depth[3, 5] = 1.0
    valid[3, 3] = valid[3, 5] = True
    mask[3, 3] = mask[3, 5] = True
    result = score(np.array([[0.0, 0.0, 2.0]]), depth, valid, mask)
    assert result["counts"]["support"] == 1
    assert result["denominators"]["unique_zbuffer_pixels"] == 1
    assert result["identity_decision"] is None
    # The extra object is outside the projected prior surface and therefore
    # remains unobserved by this comparison, rather than being called safe.


def test_low_projected_support_is_reported_without_match_recommendation():
    depth, valid, mask, _ = setup_target()
    result = score(np.array([[0.0, 0.0, 2.0]]), depth, valid, mask)
    assert result["denominators"]["valid_observed_depth_zbuffer_pixels"] == 0
    assert result.get("match_recommendation") is None


def test_causality_uses_availability_not_projection_direction():
    enforce_available_by_decision(17, 18, 18)
    # Reverse projection is also available at t=18 when both observations exist.
    enforce_available_by_decision(18, 17, 18)
    with pytest.raises(ValueError):
        enforce_available_by_decision(19, 18, 18)
    with pytest.raises(ValueError):
        enforce_available_by_decision(18, 19, 18)
    # Frame 18 data cannot rewrite a decision already emitted at frame 17.
    with pytest.raises(ValueError):
        enforce_available_by_decision(18, 17, 17)


def test_competing_tracks_receive_evidence_only_no_winner_selection():
    depth, valid, mask, _ = setup_target()
    depth[3, 3] = 2.0
    valid[3, 3] = True
    mask[3, 3] = True
    points = np.array([[0.0, 0.0, 2.0]])
    scores = [score(points, depth, valid, mask) for _ in range(2)]
    assert scores[0]["counts"] == scores[1]["counts"]
    assert all(item["identity_decision"] is None for item in scores)


def test_identical_geometry_with_different_hidden_identity_is_unidentifiable():
    depth, valid, mask, _ = setup_target()
    depth[3, 3] = 2.0
    valid[3, 3] = True
    mask[3, 3] = True
    points = np.array([[0.0, 0.0, 2.0]])
    # Two hypothetical worlds with different unprovided physical identities have identical input.
    result_world_a = score(points, depth, valid, mask)
    result_world_b = score(points.copy(), depth.copy(), valid.copy(), mask.copy())
    assert result_world_a == result_world_b
    assert result_world_a["identity_decision"] is None


def test_habitat_adapter_backproject_project_roundtrip_pixel_centers_and_edges():
    width, height = 320, 240
    intr = habitat_camera_intrinsics(width, height, 90.0, calibration_id="habitat-rgbd-stage-v1")
    assert (intr.cx, intr.cy) == (159.5, 119.5)
    depth = np.full((height, width), np.nan, dtype=np.float32)
    valid = np.zeros((height, width), dtype=bool)
    mask = np.zeros((height, width), dtype=bool)
    # Include corners, a mask boundary, and adjacent pixels across a depth step.
    samples = {(0, 0): 2.0, (239, 319): 2.5, (100, 100): 1.0,
               (100, 101): 4.0, (50, 200): 3.0}
    for (row, col), value in samples.items():
        depth[row, col] = value
        valid[row, col] = True
    for rc in ((0, 0), (239, 319), (100, 100), (50, 200)):
        mask[rc] = True

    T_W_C = Transform(np.eye(4), "world", "camera_optical", timestamp=18.0)
    cloud = backproject_depth(depth, intr, T_W_C, meters_per_unit=1.0,
                              min_depth_m=0.1, max_depth_m=10.0, valid_mask=valid)
    cam, u_float, v_float = project_world_points(cloud.xyz_W, T_W_C.matrix, intr)
    assert np.all(cam[:, 2] > 0)
    expected_rc = sorted(samples)
    expected_uv = np.asarray([[col, row] for row, col in expected_rc], dtype=np.float64)
    np.testing.assert_allclose(np.column_stack((u_float, v_float)), expected_uv, atol=1e-5, rtol=0)

    result = score_prior_surface_visibility(cloud.xyz_W, T_W_C.matrix, depth, valid, mask,
                                           intr, depth_tolerance_m=0.01)
    actual_rc = [(p["row"], p["column"]) for p in result["pixels"]]
    assert actual_rc == expected_rc
    for item in result["pixels"]:
        assert item["projected_u_float"] == pytest.approx(item["column"], abs=1e-5)
        assert item["projected_v_float"] == pytest.approx(item["row"], abs=1e-5)
    assert result["counts"]["support"] == 4
    assert result["counts"]["depth_agreement_outside_mask"] == 1
    # Adjacent depths remain at their respective pixels across the discontinuity.
    assert result["pixels"][1]["observed_axial_depth_m"] != result["pixels"][2]["observed_axial_depth_m"]
