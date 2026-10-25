from __future__ import annotations

import math

import pytest

from standalone_sg.adapters.semantic.cached import CachedDetectionProvider
from standalone_sg.config import ObjectAssociationConfig, load_config
from standalone_sg.contracts import ContractError, DetectionBatch, Detection3D
from standalone_sg.objects import ObjectAssociationManager


def det(key, t, xyz, label="chair", *, class_id=None, score=0.9, method="aabb_center",
        layer="floor_0", frame="world", metadata=None, extents=(0.4, 0.3, 0.5)):
    return Detection3D(
        detection_id=key, timestamp_s=t, center_W=xyz, center_method=method,
        class_name=label, class_id=class_id, confidence=score, frame_id=frame,
        layer_id=layer, extents_m=extents, provider_metadata=metadata or {},
    )

def batch(frame, t, detections, *, provider="cache"):
    return DetectionBatch(provider, frame, t, detections)


def test_shared_reference_config_contains_pinned_association_thresholds():
    config = load_config("configs/reference_code.yaml")
    assert config.object_association.same_class_radius_m == pytest.approx(0.75)
    assert config.object_association.same_pose_suppression_cap_m == pytest.approx(0.15)
    manager = ObjectAssociationManager(config.object_association)
    assert manager.config == ObjectAssociationConfig()
    with pytest.raises(ContractError, match="finite"):
        ObjectAssociationConfig(float("nan"), 0.15)
    with pytest.raises(ContractError, match="finite"):
        ObjectAssociationConfig(True, 0.15)


def test_cached_provider_distinguishes_absent_from_present_empty_and_manager_does_not_age():
    empty = batch("frame-empty", 2.0, ())
    provider = CachedDetectionProvider({"frame-empty": empty}, provider_id="cache")
    absent = provider.get_detections("frame-missing", 1.0)
    present_empty = provider.get_detections("frame-empty", 2.0)
    assert absent.status == "absent" and absent.detections is None
    assert present_empty.status == "empty" and present_empty.detections == ()

    manager = ObjectAssociationManager()
    created = manager.update(batch("frame-1", 1.0, (det("d1", 1.0, (0, 0, 0)),)))
    before = manager.snapshot()
    no_provider = manager.update(absent)
    empty_frame = manager.update(present_empty)
    assert no_provider.status == "provider_absent"
    assert empty_frame.status == "empty_frame"
    assert no_provider.objects == empty_frame.objects == before
    assert before[0].last_seen_s == created.objects[0].last_seen_s == 1.0


def test_same_class_radius_is_nearest_normalized_and_first_geometry_score_are_preserved():
    manager = ObjectAssociationManager()
    first = manager.update(batch("f1", 1.0, (det("a", 1.0, (0, 0, 0), " Chair ", score=0.91),)))
    second = manager.update(batch("f2", 2.0, (det("b", 2.0, (0.4, 0, 0), "chair", score=0.2,
                                                method="point_centroid", extents=(1, 1, 1)),)))
    node = second.objects[0]
    assert second.associations[0].decision == "same_class_match"
    assert second.associations[0].object_id == first.objects[0].object_id
    assert second.associations[0].distance_m == pytest.approx(0.4)
    assert node.center_W == (0.0, 0.0, 0.0)
    assert node.center_method == "aabb_center" and node.extents_m == (0.4, 0.3, 0.5)
    assert node.confidence == pytest.approx(0.91) and node.class_name == "Chair"
    assert node.first_seen_s == 1.0 and node.last_seen_s == 2.0
    assert node.first_detection_id == "a" and node.last_detection_id == "b"


def test_unrelated_classes_and_same_pose_suppression_follow_source_code():
    manager = ObjectAssociationManager()
    first = manager.update(batch("f1", 1.0, (det("chair", 1.0, (0, 0, 0), "chair"),)))
    separate = manager.update(batch("f2", 2.0, (det("table", 2.0, (0.3, 0, 0), "table"),)))
    assert separate.associations[0].decision == "created"
    assert len(separate.objects) == 2

    # Same-pose suppression picks the nearest node regardless of class. It does
    # not relabel, move, or replace the first-observation geometry/score.
    suppressed = manager.update(batch("f3", 3.0, (det("flicker", 3.0, (0.29, 0, 0), "lamp", score=0.1),)))
    assert suppressed.associations[0].decision == "same_pose_suppressed"
    assert suppressed.associations[0].object_id == separate.objects[1].object_id
    table = next(o for o in suppressed.objects if o.class_name == "table")
    assert table.center_W == (0.3, 0.0, 0.0) and table.confidence == 0.9
    assert table.class_name == "table" and table.last_detection_id == "flicker"
    assert first.objects[0].object_id != table.object_id


def test_unlabeled_suppression_floor_layer_and_three_dimensional_distance():
    manager = ObjectAssociationManager()
    manager.update(batch("f1", 1.0, (det("u1", 1.0, (0, 0, 0), None),)))
    close_unlabeled = manager.update(batch("f2", 2.0, (det("u2", 2.0, (0.1, 0, 0), None),)))
    assert close_unlabeled.associations[0].decision == "same_pose_suppressed"
    far_unlabeled = manager.update(batch("f3", 3.0, (det("u3", 3.0, (0.16, 0, 0), None),)))
    assert far_unlabeled.associations[0].decision == "created"
    other_floor = manager.update(batch("f4", 4.0, (det("u4", 4.0, (0.1, 0, 0), None, layer="floor_1"),)))
    assert other_floor.associations[0].decision == "created"

    # A same-class object with identical XY but >radius in Z is not associated.
    elevated = manager.update(batch("f5", 5.0, (det("chair-z", 5.0, (0.0, 0.0, 1.0), "chair"),)))
    assert elevated.associations[0].decision == "created"


def test_nearest_then_object_id_tie_break_and_expected_close_chair_false_merge():
    manager = ObjectAssociationManager()
    seeded = manager.update(batch("f1", 1.0, (
        det("left", 1.0, (-0.5, 0, 0), "chair"),
        det("right", 1.0, (0.5, 0, 0), "chair"),
    )))
    assert len(seeded.new_object_ids) == 2
    result = manager.update(batch("f2", 2.0, (det("middle", 2.0, (0, 0, 0), "CHAIR"),)))
    assert result.associations[0].decision == "same_class_match"
    assert result.associations[0].object_id == "object-000001"
    assert len(result.objects) == 2

    # By baseline design, two chairs closer than .75m collapse into one node.
    close = ObjectAssociationManager()
    close.update(batch("one", 1.0, (det("c1", 1.0, (0, 0, 0), "chair"),)))
    merged = close.update(batch("two", 2.0, (det("c2", 2.0, (0.5, 0, 0), "chair"),)))
    assert merged.associations[0].decision == "same_class_match"
    assert len(merged.objects) == 1
    assert merged.objects[0].center_W == (0, 0, 0)


def test_gt_like_provider_metadata_is_not_an_association_key():
    def run(gt):
        m = ObjectAssociationManager()
        m.update(batch("a", 1.0, (det("x", 1.0, (0, 0, 0), "chair", metadata={"gt_instance_id": gt}),)))
        return m.update(batch("b", 2.0, (det("y", 2.0, (0.2, 0, 0), "chair", metadata={"gt_instance_id": "different"}),)))
    left, right = run("same"), run("unrelated")
    assert left.associations == right.associations
    assert left.objects == right.objects


def test_contract_validation_rejects_invalid_center_and_cache_timestamp_mismatch():
    with pytest.raises(ContractError, match="finite"):
        det("bad", 1.0, (math.nan, 0, 0))
    with pytest.raises(ContractError, match="world"):
        manager = ObjectAssociationManager()
        manager.update(batch("camera-frame", 1.0, (det("d", 1.0, (0, 0, 0), frame="camera"),)))
    provider = CachedDetectionProvider({"f": batch("f", 1.0, ())}, provider_id="cache")
    with pytest.raises(ContractError, match="timestamp"):
        provider.get_detections("f", 1.1)


def test_invalid_later_detection_does_not_partially_create_or_consume_id():
    manager = ObjectAssociationManager()
    before = manager.snapshot()
    invalid_batch = batch("bad-frame", 2.0, (
        det("valid-first", 2.0, (0, 0, 0)),
        det("invalid-second", 2.0, (1, 0, 0), frame="camera"),
    ))
    with pytest.raises(ContractError, match="world"):
        manager.update(invalid_batch)
    assert manager.snapshot() == before
    assert manager._next_id == 1

    accepted = manager.update(batch("next", 3.0, (det("after-error", 3.0, (0, 0, 0)),)))
    assert accepted.new_object_ids == ("object-000001",)


def test_invalid_later_detection_does_not_mutate_existing_match_or_provenance():
    manager = ObjectAssociationManager()
    seeded = manager.update(batch("seed", 1.0, (det("seed-detection", 1.0, (0, 0, 0)),)))
    before = manager.snapshot()
    next_id_before = manager._next_id
    invalid_batch = batch("bad-frame", 2.0, (
        det("would-match", 2.0, (0.1, 0, 0)),
        det("invalid-second", 2.0, (1, 0, 0), frame="camera"),
    ), provider="different-provider")
    with pytest.raises(ContractError, match="world"):
        manager.update(invalid_batch)
    assert manager.snapshot() == before
    assert manager.snapshot()[0].last_seen_s == seeded.objects[0].last_seen_s == 1.0
    assert manager.snapshot()[0].last_detection_id == "seed-detection"
    assert manager.snapshot()[0].last_provider_id == "cache"
    assert manager._next_id == next_id_before

    accepted = manager.update(batch("recovery", 3.0, (det("valid-match", 3.0, (0.1, 0, 0)),)))
    assert accepted.associations[0].object_id == "object-000001"
    assert accepted.objects[0].last_seen_s == 3.0
    assert accepted.objects[0].last_detection_id == "valid-match"


def test_class_match_wins_over_closer_cross_class_suppression_candidate():
    manager = ObjectAssociationManager()
    first = manager.update(batch("f1", 1.0, (
        det("chair", 1.0, (0.0, 0.0, 0.0), "chair"),
        det("table", 1.0, (0.2, 0.0, 0.0), "table"),
    )))
    assert len(first.objects) == 2
    result = manager.update(batch("f2", 2.0, (det("chair-near", 2.0, (0.14, 0, 0), " CHAIR "),)))
    assert result.associations[0].decision == "same_class_match"
    assert result.associations[0].object_id == first.objects[0].object_id


def test_radius_boundaries_are_inclusive_and_suppression_cap_is_clamped_by_merge_radius():
    manager = ObjectAssociationManager(ObjectAssociationConfig(0.75, 0.15))
    manager.update(batch("f1", 1.0, (det("c1", 1.0, (0, 0, 0), "chair"),)))
    at_radius = manager.update(batch("f2", 2.0, (det("c2", 2.0, (0.75, 0, 0), "chair"),)))
    assert at_radius.associations[0].decision == "same_class_match"

    tight = ObjectAssociationManager(ObjectAssociationConfig(0.1, 0.15))
    tight.update(batch("a", 1.0, (det("x", 1.0, (0, 0, 0), "chair"),)))
    too_far_for_clamped_suppression = tight.update(
        batch("b", 2.0, (det("y", 2.0, (0.12, 0, 0), "table"),))
    )
    assert too_far_for_clamped_suppression.associations[0].decision == "created"
