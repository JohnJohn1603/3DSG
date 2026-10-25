import pytest

from standalone_sg.contracts.errors import ContractError
from standalone_sg.semantics.room_belief_readiness import (
    exact_pose_group_id,
    group_raw_classes_by_exact_pose,
    validate_xy_membership_scope,
)


POSE_A = [[1, 0, 0, 1.0], [0, 1, 0, 2.0], [0, 0, 1, 3.0], [0, 0, 0, 1]]
POSE_B = [[1, 0, 0, 4.0], [0, 1, 0, 2.0], [0, 0, 1, 3.0], [0, 0, 0, 1]]


def test_exact_pose_group_revisit_is_same_key_but_new_pose_is_distinct():
    assert exact_pose_group_id(POSE_A) == exact_pose_group_id(POSE_A)
    assert exact_pose_group_id(POSE_A) != exact_pose_group_id(POSE_B)
    near = [row[:] for row in POSE_A]
    near[0][3] += 1e-7
    assert exact_pose_group_id(POSE_A) != exact_pose_group_id(near)


def test_repeated_same_class_at_exact_pose_adds_no_extra_vote_unit():
    report = group_raw_classes_by_exact_pose([
        {"observation_id": "f0:d0", "raw_class": "sofa", "camera_pose": POSE_A},
        {"observation_id": "f1:d0", "raw_class": "sofa", "camera_pose": POSE_A},
        {"observation_id": "f2:d0", "raw_class": "sofa", "camera_pose": POSE_B},
    ])
    assert report["view_group_count"] == 2
    assert report["raw_observation_class_counts"] == {"sofa": 3}
    assert report["one_unit_per_group_raw_class_support"] == {"sofa": 2.0}
    assert "not statistically independent" in report["support_semantics"]


def test_conflicting_classes_in_one_pose_split_one_unit_not_two():
    report = group_raw_classes_by_exact_pose([
        {"observation_id": "f0:d0", "raw_class": "bed", "camera_pose": POSE_A},
        {"observation_id": "f0:d1", "raw_class": "sofa", "camera_pose": POSE_A},
    ])
    assert report["view_group_count"] == 1
    assert report["one_unit_per_group_raw_class_support"] == {"bed": 0.5, "sofa": 0.5}


def test_xy_scope_filters_explicit_room_floors_or_requires_verified_single_floor():
    filtered = validate_xy_membership_scope(
        ["r0", "r1"], observed_floor_ids=["floor_0"],
        room_floor_ids={"r0": "floor_0", "r1": "floor_1"},
        single_floor_scope_verified=False, scope_provenance="manifest-hash",
    )
    assert filtered["candidate_room_ids"] == ["r0"]
    assert filtered["cross_floor_xy_membership_allowed"] is False
    with pytest.raises(ContractError, match="requires complete room floor IDs"):
        validate_xy_membership_scope(
            ["r0", "r1"], observed_floor_ids=["floor_0"], room_floor_ids=None,
            single_floor_scope_verified=False, scope_provenance="unverified",
        )
    single = validate_xy_membership_scope(
        ["r0"], observed_floor_ids=["floor_0"], room_floor_ids=None,
        single_floor_scope_verified=True, scope_provenance="capture+snapshot hashes",
    )
    assert single["status"] == "verified_single_observed_floor_scope"
    assert "no Z bounds" in single["limitation"]

