from standalone_sg.objects.profile_comparison import (pairwise_identity_metrics, partition_delta,
                                                      partition_members, relation_changes)


def test_deferred_same_identity_pair_is_fn_not_dropped():
    result = pairwise_identity_metrics(
        {"f0:det-0": "track-a", "f1:det-0": None},
        {"f0:det-0": 7, "f1:det-0": 7},
    )
    assert result["pairwise_tp"] == 0
    assert result["pairwise_fn"] == 1
    assert result["unassigned_scored_observations"] == 1


def test_candidate_false_merge_is_fp_and_false_merge_track():
    result = pairwise_identity_metrics(
        {"f0:det-0": "renamed", "f1:det-0": "renamed"},
        {"f0:det-0": 1, "f1:det-0": 2},
    )
    assert result["pairwise_fp"] == 1
    assert result["false_merge_tracks"] == 1


def test_same_frame_pairs_are_scored_separately_from_cross_frame():
    result = pairwise_identity_metrics(
        {"f0:det-0": "t", "f0:det-1": "t", "f1:det-0": "t"},
        {"f0:det-0": 1, "f0:det-1": 1, "f1:det-0": 1},
    )
    assert result["same_frame_pairs_excluded_from_cross_frame"] == 1
    assert result["cross_frame_pair_population"] == 2
    assert result["pairwise_tp"] == 2


def test_unmatched_gt_is_excluded_and_reported_not_counted_as_fp():
    result = pairwise_identity_metrics(
        {"f0:det-0": "t", "f1:det-0": "t", "f2:det-0": "t"},
        {"f0:det-0": 4, "f1:det-0": 4, "f2:det-0": None},
    )
    assert result["evaluator_matched_observations_scored"] == 2
    assert result["observations_without_evaluator_identity"] == 1
    assert result["pairwise_tp"] == 1
    assert result["pairwise_fp"] == 0


def test_track_id_renaming_does_not_change_partition_or_relations():
    left = {"f0:det-0": "one", "f1:det-0": "one", "f2:det-0": "two"}
    renamed = {"f0:det-0": "X", "f1:det-0": "X", "f2:det-0": "Y"}
    assert partition_members(left) == partition_members(renamed)
    assert relation_changes(left, renamed, sorted(left)) == []


def test_track_rename_only_has_zero_identity_changes():
    original = {"f0:det-0": "u3-0001", "f1:det-0": "u3-0001",
                "f2:det-0": "u3-0002", "f3:det-0": None}
    renamed = {"f0:det-0": "object-0009", "f1:det-0": "object-0009",
               "f2:det-0": "object-0010", "f3:det-0": None}
    delta = partition_delta(original, renamed)
    assert delta["identity_relation_changes"] == 0
    assert delta["track_id_renamed_only_groups"] == 2
    assert delta["source_membership_groups_removed"] == 0
    assert delta["candidate_membership_groups_added"] == 0


def test_alias_pair_does_not_create_cross_frame_vote():
    # Same-frame alias IDs remain in raw lineage, but their pair is excluded
    # from cross-frame association quality and no class/vote input exists here.
    result = pairwise_identity_metrics(
        {"f0:det-0": "t", "f0:det-1": "t", "f1:det-0": "t"},
        {"f0:det-0": 2, "f0:det-1": 2, "f1:det-0": 2},
    )
    assert result["cross_frame_pair_population"] == 2
    assert result["same_frame_pairs_excluded_from_cross_frame"] == 1


def test_candidate_correction_source_split_same_evaluator_identity():
    evaluator = {"f0:det-0": "same", "f1:det-0": "same"}
    source = pairwise_identity_metrics({"f0:det-0": "s1", "f1:det-0": "s2"}, evaluator)
    candidate = pairwise_identity_metrics({"f0:det-0": "c1", "f1:det-0": "c1"}, evaluator)
    assert source["pairwise_fn"] == 1
    assert candidate["pairwise_tp"] == 1


def test_mask_and_mixed_object_annotations_are_not_inputs_to_association_score():
    # This scorer accepts only assignments and evaluator IDs.  A mask match
    # ledger cannot turn a same-frame mixed-object containment into identity GT.
    result = pairwise_identity_metrics(
        {"f0:det-0": "t", "f1:det-0": "t"},
        {"f0:det-0": 11, "f1:det-0": 22},
    )
    assert result["pairwise_fp"] == 1
    assert "mask" not in result["metric_contract"]


def test_canonical_choice_does_not_change_track_partition():
    # Observation alias/canonical metadata is deliberately outside the
    # partition score; only the relation represented by assigned tracks counts.
    original = {"f0:det-0": "track1", "f0:det-1": "track1", "f1:det-0": "track1"}
    changed_representative = {"f0:det-0": "renamed", "f0:det-1": "renamed", "f1:det-0": "renamed"}
    assert partition_members(original) == partition_members(changed_representative)
    assert relation_changes(original, changed_representative, sorted(original)) == []


def test_evaluator_pair_score_does_not_mutate_one_to_one_ledger_data():
    official_ledger = [
        {"detection_id": "f0:det-0", "gt_semantic_id": 10, "match_status": "matched"},
        {"detection_id": "f0:det-1", "gt_semantic_id": None, "match_status": "unmatched_or_ignored"},
    ]
    before = [dict(row) for row in official_ledger]
    pairwise_identity_metrics(
        {"f0:det-0": "t", "f0:det-1": "t"},
        {row["detection_id"]: row["gt_semantic_id"] for row in official_ledger},
    )
    assert official_ledger == before
