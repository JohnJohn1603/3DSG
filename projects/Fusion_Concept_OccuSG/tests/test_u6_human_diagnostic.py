import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_u6_human_diagnostic as human_eval  # noqa: E402
import u6_scoring  # noqa: E402


def row(oid, frame, identity, track):
    return {"observation_id": oid, "frame_id": f"f{frame}", "frame_index": frame,
            "gt_identity": identity, "track_id": track, "association_status": "matched"}


def ann(oid, frame, identity):
    return {"observation_id": oid, "frame_index": frame, "identity_token": identity,
            "identity_status": "confirmed"}


def test_human_cross_frame_score_excludes_same_frame_d2_pair():
    d1 = [row("a0", 0, "table_1", "t1"), row("a1", 1, "table_1", "t1"),
          row("a2", 2, "table_1", "t1"), row("a4", 4, "table_1", "t1")]
    d2 = [row("b4", 2, "table_2", "t2"), row("b5", 2, "table_2", "t2")]
    result = u6_scoring.pairwise_association(d1)
    assert result["eligible_cross_frame_pairs"] == 6
    assert result["ground_truth_positive_pairs"] == 6
    assert result["tp"] == 6
    assert u6_scoring.pairwise_association(d1 + d2)["eligible_cross_frame_pairs"] == 12
    # D2 must be filtered before calling the cross-frame scorer; otherwise its
    # links to D1 would silently enlarge the population as well as add a frame.


def test_exact_pose_revisit_exclusion_is_scoring_only_and_keeps_history_rows():
    d1 = [row("a0", 0, "table_1", "t1"), row("a1", 1, "table_1", "t1"),
          row("a2", 2, "table_1", "t1"), row("a4", 4, "table_1", "t1")]
    all_pairs = u6_scoring.pairwise_association(d1)
    without_revisit = u6_scoring.pairwise_association(
        [item for item in d1 if item["frame_index"] != 4], excluded_frame_indices={4})
    assert all_pairs["eligible_cross_frame_pairs"] == 6
    assert all_pairs["tp"] == 6
    assert without_revisit["eligible_cross_frame_pairs"] == 3
    assert without_revisit["tp"] == 3
    assert without_revisit["excluded_frame_indices"] == [4]
    assert [item["observation_id"] for item in d1] == ["a0", "a1", "a2", "a4"]


def test_same_frame_same_identity_consistent_or_split_is_reported_separately():
    annotations = [ann("b4", 2, "table_2"), ann("b5", 2, "table_2")]
    source = {"b4": {"track_id": "s1"}, "b5": {"track_id": "s1"}}
    geometry = {"b4": {"track_id": "g1"}, "b5": {"track_id": "g2"}}
    source_score = human_eval.score_same_frame_identity(annotations, source)
    geometry_score = human_eval.score_same_frame_identity(annotations, geometry)
    assert source_score["same_frame_same_identity_pair_count"] == 1
    assert source_score["same_track_consistent_pair_count"] == 1
    assert source_score["excess_same_frame_tracks"] == 0
    assert geometry_score["split_across_tracks_pair_count"] == 1
    assert geometry_score["excess_same_frame_tracks"] == 1


def test_physical_track_structure_counts_d1_fragmentation_and_d2_duplicate_track_separately():
    annotations = [ann("a0", 0, "table_1"), ann("a1", 1, "table_1"),
                   ann("b4", 2, "table_2"), ann("b5", 2, "table_2")]
    assignments = {"a0": {"track_id": "t1"}, "a1": {"track_id": "t2"},
                   "b4": {"track_id": "t3"}, "b5": {"track_id": "t4"}}
    result = human_eval.score_identity_track_structure(annotations, assignments)
    assert result["unique_confirmed_physical_objects"] == 2
    assert result["fragmented_physical_identities"] == {"table_1": ["t1", "t2"], "table_2": ["t3", "t4"]}
    assert result["excess_track_count_across_physical_identities"] == 2


def test_exact_label_normalization_does_not_apply_parent_child_or_synonym_mapping():
    assert human_eval.exact_label(" table ") == "table"
    assert human_eval.exact_label("dining_table") == "dining_table"
    assert human_eval.exact_label("coffee table") == "coffee_table"
    assert human_eval.exact_label("dining_table") != human_eval.exact_label("table")
    assert human_eval.exact_label("coffee table") != human_eval.exact_label("table")
