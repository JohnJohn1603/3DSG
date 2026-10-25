import numpy as np
import pytest

from standalone_sg.objects.surface_support import surface_support, voxel_representatives
from standalone_sg.objects.u3_experimental import (ExperimentalAssociation, U3Observation,
                                                   exclude_same_frame_aliases)


def _grid(x0, x1, y0=0.0, y1=0.2, z=0.0):
    xs = np.arange(x0, x1, 0.02, dtype=np.float32)
    ys = np.arange(y0, y1, 0.02, dtype=np.float32)
    return np.asarray([[x, y, z] for x in xs for y in ys], dtype=np.float32)


def test_partial_view_surface_has_high_forward_support_and_lower_reverse():
    prior = _grid(0.0, 1.0)
    current = _grid(0.4, 0.8)
    got = surface_support(current, voxel_representatives(prior, 0.02),
                          voxel_size_m=0.02, radius_m=0.04)
    assert got["forward_supported_voxels"] >= 100
    assert got["forward_support_ratio"] == 1.0
    assert got["reverse_support_ratio"] < 0.5


def test_nearby_but_disjoint_surfaces_have_no_support():
    left = _grid(0.0, 0.2)
    right = _grid(0.3, 0.5)
    got = surface_support(left, voxel_representatives(right, 0.02),
                          voxel_size_m=0.02, radius_m=0.04)
    assert got["forward_supported_voxels"] == 0
    assert got["reverse_supported_voxels"] == 0


def test_small_contact_patch_does_not_meet_minimum_support():
    prior = _grid(0.0, 1.0)
    current = _grid(0.0, 0.08)
    got = surface_support(current, voxel_representatives(prior, 0.02),
                          voxel_size_m=0.02, radius_m=0.04)
    assert got["forward_supported_voxels"] < 100


def test_mixed_mask_containment_has_asymmetric_support_diagnostics():
    target = _grid(0.0, 0.4, y1=0.2)
    extra_object = _grid(0.6, 1.0, y1=0.2)
    mixed = np.concatenate([target, extra_object], axis=0)
    got = surface_support(mixed, voxel_representatives(target, 0.02),
                          voxel_size_m=0.02, radius_m=0.04)
    assert got["forward_support_ratio"] < 0.8
    assert got["reverse_support_ratio"] >= 0.9
    assert got["observation_surface_voxels"] > got["track_surface_voxels"] * 1.5


def test_surface_support_is_deterministic_under_input_order():
    a = _grid(0.0, 0.4)
    b = _grid(0.2, 0.6)
    first = surface_support(a, voxel_representatives(b, 0.02), voxel_size_m=0.02, radius_m=0.04)
    second = surface_support(a[::-1].copy(), voxel_representatives(b[::-1].copy(), 0.02),
                             voxel_size_m=0.02, radius_m=0.04)
    assert first == second


@pytest.fixture
def continuity_config():
    return {
        "voxel_size_m": 0.02,
        "support_radius_m": 0.04,
        "minimum_supported_voxels": 100,
        "minimum_forward_support_ratio": 0.35,
        "ambiguity_score_margin": 0.1,
        "mixed_mask_size_ratio": 1.5,
        "mixed_mask_reverse_support_min": 0.9,
        "mixed_mask_forward_support_max": 0.8,
    }


def _obs(detection_id, center, extents):
    return U3Observation(detection_id, detection_id.split(":")[0], 1.0, center, extents,
                         "floor_0", None, None, "mask-hash")


def test_partial_view_uses_surface_continuity_only_after_old_iou_rejects(continuity_config):
    matcher = ExperimentalAssociation("geometry", max_center_m=0.75,
                                      geom_min_iou=0.01, geom_only_min_iou=0.05,
                                      ambiguity_margin=0.05)
    first = _obs("f0:det-0", (0.0, 0.0, 0.0), (0.8, 0.8, 0.2))
    assert matcher.update_frame([first])[0].decision == "created"
    prior_surface = _grid(0.0, 1.0)
    current_surface = _grid(0.4, 0.8)
    second = _obs("f1:det-0", (0.2, 0.0, 0.0), (0.1, 0.4, 0.1))
    result = matcher.update_frame([second],
        surface_points_by_detection={second.detection_id: current_surface},
        surface_points_by_track={"u3-0001": voxel_representatives(prior_surface, 0.02)},
        surface_config=continuity_config)
    assert result[0].decision == "matched"
    assert result[0].track_id == "u3-0001"
    assert result[0].reason == "surface_continuity"
    row = next(row for row in matcher.ledger if row["detection_id"] == second.detection_id)
    assert row["aabb_iou"] < 0.05
    assert row["association_branch"] == "surface_continuity"


def test_candidate_tie_defers_instead_of_forcing_match(continuity_config):
    matcher = ExperimentalAssociation("geometry", max_center_m=0.75,
                                      geom_min_iou=0.01, geom_only_min_iou=0.05,
                                      ambiguity_margin=0.05)
    a = _obs("f0:det-0", (-0.1, 0.0, 0.0), (0.8, 0.8, 0.2))
    b = _obs("f0:det-1", (0.1, 0.0, 0.0), (0.8, 0.8, 0.2))
    matcher.update_frame([a, b])
    points = _grid(0.0, 1.0)
    current = _obs("f1:det-0", (0.0, 0.0, 0.0), (0.1, 0.4, 0.1))
    result = matcher.update_frame([current],
        surface_points_by_detection={current.detection_id: points},
        surface_points_by_track={"u3-0001": voxel_representatives(points, 0.02),
                                 "u3-0002": voxel_representatives(points, 0.02)},
        surface_config=continuity_config)
    assert result[0].decision == "defer"
    assert result[0].reason == "ambiguous_candidates"


def test_touching_distinct_surfaces_can_still_produce_confident_continuity_match(continuity_config):
    """Record a known counterexample: surface support is not physical identity evidence.

    The two prior planes and the current strip are labeled as distinct-object geometry
    by this synthetic fixture only. Those labels are never passed to the matcher.
    Voxel-boundary asymmetry gives one plane a higher support score, so the current
    implementation emits a match instead of deferring. A passing test here documents
    this limitation; it is not evidence that this behavior is safe.
    """
    matcher = ExperimentalAssociation("geometry", max_center_m=0.75,
                                      geom_min_iou=0.01, geom_only_min_iou=0.05,
                                      ambiguity_margin=0.05)
    left = _obs("touch-f0:det-0", (0.65, 1.0, 0.0), (1.3, 2.0, 0.02))
    right = _obs("touch-f0:det-1", (1.95, 1.0, 0.0), (1.3, 2.0, 0.02))
    matcher.update_frame([left, right])

    left_surface = _grid(0.0, 1.3, y1=2.0)
    right_surface = _grid(1.3, 2.6, y1=2.0)
    current_surface = _grid(1.26, 1.34, y1=2.0)
    current = _obs("touch-f1:det-0", (1.3, 1.0, 0.0), (0.08, 2.0, 0.02))
    decisions = matcher.update_frame(
        [current],
        surface_points_by_detection={current.detection_id: current_surface},
        surface_points_by_track={
            "u3-0001": voxel_representatives(left_surface, 0.02),
            "u3-0002": voxel_representatives(right_surface, 0.02),
        },
        surface_config=continuity_config,
    )

    assert decisions[0].decision == "matched"
    assert decisions[0].reason == "surface_continuity"


def test_q2_prior_surface_bounds_fixture_for_touching_surface_counterexample(continuity_config):
    """A synthetic diagnostic only; its outcome is not a general safety claim."""
    matcher = ExperimentalAssociation("geometry", max_center_m=0.75,
                                      geom_min_iou=0.01, geom_only_min_iou=0.05,
                                      ambiguity_margin=0.05)
    left = _obs("touch-f0:det-0", (0.65, 1.0, 0.0), (1.3, 2.0, 0.02))
    right = _obs("touch-f0:det-1", (1.95, 1.0, 0.0), (1.3, 2.0, 0.02))
    matcher.update_frame([left, right])
    left_surface = voxel_representatives(_grid(0.0, 1.3, y1=2.0), 0.02)
    right_surface = voxel_representatives(_grid(1.3, 2.6, y1=2.0), 0.02)
    current_surface = _grid(1.26, 1.34, y1=2.0)

    def prior(points, evidence):
        lo, hi = points.min(axis=0), points.max(axis=0)
        return {"geometry_source": "prior_accumulated_surface_voxel_representatives",
                "center_W": (lo + hi) / 2, "extents_m": hi - lo,
                "prior_revision": 1, "evidence_ids": [evidence]}

    current = _obs("touch-f1:det-0", (1.3, 1.0, 0.0), (0.08, 2.0, 0.02))
    decisions = matcher.update_frame(
        [current],
        surface_points_by_detection={current.detection_id: current_surface},
        surface_points_by_track={"u3-0001": left_surface, "u3-0002": right_surface},
        surface_config=continuity_config,
        prior_geometry_by_track={"u3-0001": prior(left_surface, left.detection_id),
                                 "u3-0002": prior(right_surface, right.detection_id)},
    )
    # The prior-surface AABB blocks the K4 match in this exact fixture. This is
    # one geometry fixture result, not evidence of broad false-merge safety.
    assert decisions[0].decision != "matched"
    row = next(row for row in matcher.ledger if row["detection_id"] == current.detection_id)
    assert row["aabb_iou"] == 0.0
    assert row["extent_consistency"] is None
    assert "no_3d_overlap" in row["gate_failures"]
    assert row.get("surface_branch_status") == "unavailable_missing_geometry"


def test_possible_containment_defers_without_admitting_surface(continuity_config):
    matcher = ExperimentalAssociation("geometry", max_center_m=0.75,
                                      geom_min_iou=0.01, geom_only_min_iou=0.05)
    prior = _grid(0.0, 0.4, y1=0.2)
    first = _obs("f0:det-0", (0.0, 0.0, 0.0), (0.5, 0.5, 0.2))
    matcher.update_frame([first])
    mixed = np.concatenate([prior, _grid(0.6, 1.0, y1=0.2)], axis=0)
    second = _obs("f1:det-0", (0.7, 0.0, 0.0), (1.0, 0.5, 0.2))
    result = matcher.update_frame([second],
        surface_points_by_detection={second.detection_id: mixed},
        surface_points_by_track={"u3-0001": voxel_representatives(prior, 0.02)},
        surface_config=continuity_config)
    assert result[0].decision == "defer"
    assert result[0].reason == "surface_support_possible_mixed_mask"


def test_alias_is_excluded_from_matcher_input_and_surface_evidence():
    canonical = _obs("f0:det-0", (0, 0, 0), (1, 1, 1))
    alias = _obs("f0:det-1", (0, 0, 0), (1, 1, 1))
    kept = exclude_same_frame_aliases([canonical, alias], {alias.detection_id: canonical.detection_id})
    assert [obs.detection_id for obs in kept] == [canonical.detection_id]


def test_current_observation_cannot_self_match_without_prior_track_state(continuity_config):
    matcher = ExperimentalAssociation("geometry", max_center_m=0.75,
                                      geom_min_iou=0.01, geom_only_min_iou=0.05)
    current = _obs("f0:det-0", (0, 0, 0), (1, 1, 1))
    decisions = matcher.update_frame([current],
        surface_points_by_detection={current.detection_id: _grid(0.0, 1.0)},
        surface_points_by_track={}, surface_config=continuity_config)
    assert decisions[0].decision == "created"
    assert decisions[0].reason == "new_track"


def test_original_geometry_accepted_branch_is_unchanged_when_surface_is_enabled(continuity_config):
    original = ExperimentalAssociation("geometry", max_center_m=0.75,
                                       geom_min_iou=0.01, geom_only_min_iou=0.05,
                                       ambiguity_margin=0.05)
    candidate = ExperimentalAssociation("geometry", max_center_m=0.75,
                                        geom_min_iou=0.01, geom_only_min_iou=0.05,
                                        ambiguity_margin=0.05)
    first = _obs("f0:det-0", (0, 0, 0), (0.5, 0.5, 0.5))
    original.update_frame([first]); candidate.update_frame([first])
    second = _obs("f1:det-0", (0.02, 0, 0), (0.5, 0.5, 0.5))
    expected = original.update_frame([second])
    got = candidate.update_frame([second], surface_points_by_detection={second.detection_id: _grid(0, 1)},
        surface_points_by_track={"u3-0001": voxel_representatives(_grid(0, 1), 0.02)},
        surface_config=continuity_config)
    assert [(d.decision, d.track_id, d.selected_candidate_score) for d in got] == [
        (d.decision, d.track_id, d.selected_candidate_score) for d in expected]
    assert candidate.ledger[-1]["association_branch"] == "original_geometry"
