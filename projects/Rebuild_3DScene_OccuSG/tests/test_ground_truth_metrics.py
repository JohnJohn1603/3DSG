from __future__ import annotations

import numpy as np
import pytest

from evaluation.metrics import (
    EvaluationContractError,
    association_metrics,
    mask_iou,
    membership_metrics,
    object_mask_metrics,
    p3_filter_metrics,
    room_metrics,
    room_partition_metrics,
    semantic_metrics,
    validate_frame,
    validate_room_annotations,
    _raster_rooms,
    _raster_rooms_vectorized,
)


def rect(room_id: str, x0: float, y0: float, x1: float, y1: float):
    return {"id": room_id, "polygon": {"coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}}


def test_room_perfect_prediction_and_adjacency():
    gt = [rect("a", 0, 0, 2, 2), rect("b", 2, 0, 4, 2)]
    result = room_metrics(gt, gt, adjacency_gt=[("a", "b")], adjacency_pred=[("a", "b")])
    assert result["matched_count"] == 2
    assert result["macro_iou_matched"] == pytest.approx(1.0)
    assert result["union_iou"] == pytest.approx(1.0)
    assert result["adjacency_after_room_matching"]["recall"] == 1.0
    assert result["boundary_metrics"]["f1"] == 1.0


def test_split_merge_missing_and_extra_rooms_are_not_hidden():
    split = room_metrics([rect("a", 0, 0, 4, 2)], [rect("p1", 0, 0, 2, 2), rect("p2", 2, 0, 4, 2)])
    assert split["over_segmentation"] == [{"gt_room_id": "a", "predicted_room_ids": ["p1", "p2"]}]
    assert split["unmatched_predicted_room_ids"]

    merged = room_metrics([rect("a", 0, 0, 2, 2), rect("b", 2, 0, 4, 2)], [rect("p", 0, 0, 4, 2)])
    assert len(merged["under_segmentation"]) == 1
    assert len(merged["missed_gt_room_ids"]) == 1

    missing_extra = room_metrics([rect("a", 0, 0, 1, 1)], [rect("extra", 3, 0, 4, 1)])
    assert missing_extra["missed_gt_room_ids"] == ["a"]
    assert missing_extra["unmatched_predicted_room_ids"] == ["extra"]


def test_boundary_offset_and_observed_domain():
    gt = [rect("a", 0, 0, 1, 1)]
    pred = [rect("p", 0.08, 0, 1.08, 1)]
    observed = {(x, y) for x in range(0, 60) for y in range(0, 50)}
    result = room_metrics(gt, pred, observed_domain=observed)
    assert result["union_iou"] < 1.0
    assert result["boundary_metrics"]["tolerance_m"] == 0.10
    assert result["observed_domain"]["domain_cells"] == len(observed)
    assert result["observed_domain_boundary_metrics"]["domain_clipped_to_original_boundaries_only"]


def test_empty_room_prediction_and_empty_gt_are_explicit():
    gt = [rect("a", 0, 0, 1, 1)]
    none = room_metrics(gt, [])
    assert none["missed_gt_room_ids"] == ["a"]
    assert none["union_iou"] == 0.0
    empty = room_metrics([], [])
    assert empty["gt_room_count"] == empty["predicted_room_count"] == 0
    assert empty["macro_iou_matched"] is None


def test_coordinate_or_scale_mismatch_is_rejected():
    with pytest.raises(EvaluationContractError):
        validate_frame({"coordinate_frame": "habitat_y_up_xz", "units": "m"})
    with pytest.raises(EvaluationContractError):
        validate_frame({"coordinate_frame": "canonical_world_z_up_xy_m", "units": "cm"})
    validate_frame({"coordinate_frame": "canonical_world_z_up_xy_m", "units": "m"})


def test_room_annotation_validation_rejects_overlap_and_self_intersection():
    validate_room_annotations([rect("a", 0, 0, 1, 1), rect("b", 1, 0, 2, 1)])
    with pytest.raises(EvaluationContractError, match="overlap"):
        validate_room_annotations([rect("a", 0, 0, 1, 1), rect("b", .5, .5, 1.5, 1.5)])
    bow_tie = {"id": "bad", "polygon": {"coordinates": [[[0, 0], [2, 2], [0, 2], [2, 0], [0, 0]]]}}
    with pytest.raises(EvaluationContractError, match="self-intersecting"):
        validate_room_annotations([bow_tie])


def test_partition_metrics_include_unmatched_gt_as_zero_and_separate_roi_extras():
    gt = [rect("a", 0, 0, 1, 1), rect("b", 1, 0, 2, 1)]
    roi = {(x, y) for x in range(20) for y in range(10)}
    report = room_partition_metrics(gt, [rect("pa", 0, 0, 1, 1), rect("outside", 3, 0, 4, 1)],
                                    resolution_m=.1, roi_cells=roi)
    assert report["macro_iou_all_gt_rooms"] == pytest.approx(.5)
    assert report["macro_iou_matched_only_diagnostic"] == pytest.approx(1.0)
    assert report["missed_gt_room_ids"] == ["b"]
    assert report["extra_predicted_room_ids_inside_roi"] == []
    assert report["predictions_only_outside_roi_not_scored"] == ["outside"]
    assert report["union_iou"] == pytest.approx(.5)


def test_partition_split_merge_and_tiny_overlap_sensitivity_are_explicit():
    split_report = room_partition_metrics(
        [rect("whole", 0, 0, 2, 1)],
        [rect("left", 0, 0, 1, 1), rect("right", 1, 0, 2, 1)], resolution_m=.1,
    )
    assert split_report["split_merge_sensitivity"]["results"]["0.10"]["splits"]
    assert len(split_report["intersection_matrix"]) == 2
    merged_report = room_partition_metrics(
        [rect("a", 0, 0, 1, 1), rect("b", 1, 0, 2, 1)],
        [rect("merged", 0, 0, 2, 1)], resolution_m=.1,
    )
    assert merged_report["split_merge_sensitivity"]["results"]["0.10"]["merges"]
    tiny_report = room_partition_metrics(
        [rect("whole", 0, 0, 2, 1)],
        [rect("main", 0, 0, 1.95, 1), rect("sliver", 1.95, 0, 2, 1)], resolution_m=.01,
    )
    assert tiny_report["split_merge_sensitivity"]["results"]["0.01"]["splits"]
    assert not tiny_report["split_merge_sensitivity"]["results"]["0.05"]["splits"]


def test_partition_metrics_report_predicted_overlap_and_fixed_observed_domain():
    gt = [rect("a", 0, 0, 1, 1), rect("b", 1, 0, 2, 1)]
    p = [rect("pa", 0, 0, 1.1, 1), rect("pb", .9, 0, 2, 1)]
    observed = {(x, y) for x in range(5, 35) for y in range(0, 10)}
    report = room_partition_metrics(gt, p, resolution_m=.1, observed_free_cells=observed)
    assert report["predicted_room_overlaps"]
    assert report["observed_free_domain"]["status"] == "scored_on_baseline_m3_free_cells; fixed across candidates"
    assert report["observed_free_domain"]["cells"] == 150


def test_partition_metrics_reject_empty_gt_room_inside_roi():
    gt = [rect("a", 0, 0, 1, 1), rect("b", 2, 0, 3, 1)]
    roi = {(x, y) for x in range(10) for y in range(10)}
    with pytest.raises(EvaluationContractError, match="no cells in the fixed ROI"):
        room_partition_metrics(gt, gt, resolution_m=.1, roi_cells=roi)


def test_vectorized_partition_raster_matches_protocol_reference_including_holes():
    donut = {"id":"donut", "polygon":{"coordinates":[
        [[0,0],[2,0],[2,2],[0,2],[0,0]],
        [[.5,.5],[.5,1.5],[1.5,1.5],[1.5,.5],[.5,.5]],
    ]}}
    rooms=[donut,rect("other",2,0,3,2)]
    slow=_raster_rooms(rooms,.1);fast=_raster_rooms_vectorized(rooms,.1)
    assert slow==fast


def test_object_mask_match_duplicate_track_and_empty_cases():
    mask_a = np.zeros((24, 24), dtype=bool); mask_a[1:11, 1:11] = True
    mask_b = np.zeros((24, 24), dtype=bool); mask_b[12:21, 12:21] = True
    assert mask_iou(mask_a, mask_a) == 1.0
    result = object_mask_metrics(
        [{"detection_id": "d1", "class_name": "chair", "mask": mask_a},
         {"detection_id": "d2", "class_name": "chair", "mask": mask_a}],
        [{"instance_id": "g1", "class_name": "chair", "mask": mask_a},
         {"instance_id": "g2", "class_name": "chair", "mask": mask_b}],
    )
    assert (result["tp"], result["fp"], result["fn"]) == (1, 1, 1)
    assert object_mask_metrics([], [])["recall"] is None
    assert object_mask_metrics([], [{"instance_id": "tiny", "class_name": "book", "mask": np.zeros((4, 4), bool)}])["ignored_count"] == 1


def test_object_assignment_scales_past_twenty_and_all_ignored_is_not_zero_score():
    gt = []; pred = []
    for i in range(24):
        mask = np.zeros((160, 160), dtype=bool)
        y, x = divmod(i, 6)
        mask[y * 24:y * 24 + 12, x * 24:x * 24 + 12] = True
        gt.append({"instance_id": f"g{i}", "class_name": "any", "mask": mask})
        pred.append({"detection_id": f"d{i}", "class_name": "any", "mask": mask.copy()})
    result = object_mask_metrics(pred, gt)
    assert (result["tp"], result["fp"], result["fn"]) == (24, 0, 0)
    ignored = [{"instance_id": "g0", "class_name": "any", "mask": np.ones((12, 12), bool), "status": "uncertain"}]
    all_ignored = object_mask_metrics([], ignored)
    assert all_ignored["tp"] == all_ignored["fn"] == 0
    assert all_ignored["recall"] is None
    assert all_ignored["ignored_count"] == 1


def test_prediction_over_ignored_gt_is_not_counted_as_false_positive():
    small = np.zeros((20, 20), dtype=bool); small[2:7, 3:8] = True
    eligible = np.zeros((20, 20), dtype=bool); eligible[10:18, 10:18] = True
    report = object_mask_metrics(
        [{"detection_id": "d-small", "class_name": "chair", "mask": small.copy()},
         {"detection_id": "d-large", "class_name": "chair", "mask": eligible.copy()}],
        [{"instance_id": "g-small", "class_name": "chair", "mask": small, "status": "below_min_visible_pixels"},
         {"instance_id": "g-eligible", "class_name": "chair", "mask": eligible, "status": "eligible"}],
    )
    assert (report["tp"], report["fp"], report["fn"]) == (1, 0, 0)
    assert report["ignored_prediction_count"] == 1
    assert report["ignored_gt"][0]["reason"] == "below_min_visible_pixels"


def test_prediction_matching_eligible_gt_takes_priority_over_ignored_overlap():
    eligible = np.zeros((20, 20), dtype=bool); eligible[2:10, 2:10] = True
    ignored = np.zeros((20, 20), dtype=bool); ignored[2:9, 2:9] = True
    report = object_mask_metrics(
        [{"detection_id": "d", "class_name": "any", "mask": eligible.copy()}],
        [{"instance_id": "g", "class_name": "any", "mask": eligible, "status": "eligible"},
         {"instance_id": "small", "class_name": "any", "mask": ignored, "status": "below_min_visible_pixels"}],
    )
    assert report["tp"] == 1 and report["fp"] == report["ignored_prediction_count"] == 0


def test_p3_filter_cross_tab_uses_independent_mask_lineage():
    report = p3_filter_metrics([
        {"detection_id": "kept-good", "decision": "accepted"},
        {"detection_id": "rejected-good", "decision": "rejected"},
        {"detection_id": "kept-unmatched", "decision": "accepted"},
        {"detection_id": "rejected-unmatched", "decision": "rejected"},
    ], mask_matched_detection_ids={"kept-good", "rejected-good"})
    assert report["counts"] == {"mask_matched_kept": 1, "mask_matched_rejected": 1,
                                 "unmatched_kept": 1, "unmatched_rejected": 1, "unknown_decision": 0}


def test_association_fragmentation_and_false_merge_are_distinct():
    report = association_metrics([
        {"gt_instance_id": "g1", "predicted_track_id": "t1"},
        {"gt_instance_id": "g1", "predicted_track_id": "t2"},
        {"gt_instance_id": "g2", "predicted_track_id": "t2"},
    ])
    assert report["fragmented_count"] == 1
    assert report["merged_track_count"] == 1


def test_membership_wrong_room_and_semantic_unknown_are_reported():
    member = membership_metrics(
        [{"predicted_object_id": "o1", "predicted_room_id": "r2", "status": "accepted"}],
        object_match={"o1": "g1"}, room_match={"r2": "room_b"}, gt_membership={"g1": "room_a"},
    )
    assert member["conditional_accuracy"] == 0.0
    sem = semantic_metrics([{"room_id": "r1", "label": "unknown", "status": "unknown"}], {})
    assert sem["accuracy"] is None and len(sem["excluded"]) == 1
