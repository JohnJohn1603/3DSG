import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import u6_scoring as score  # noqa: E402


def obs(name, frame, identity, track, status="matched"):
    return {"observation_id": name, "frame_id": f"f{frame}", "frame_index": frame,
            "gt_identity": identity, "track_id": track, "association_status": status}


def test_pairwise_denominator_excludes_same_frame_and_counts_each_cross_frame_pair_once():
    rows = [obs("a0", 0, "A", "t1"), obs("b0", 0, "B", "t2"),
            obs("a1", 1, "A", "t1"), obs("b1", 1, "B", "t2")]
    result = score.pairwise_association(rows)
    assert result["eligible_cross_frame_pairs"] == 4
    assert result["ground_truth_positive_pairs"] == 2
    assert (result["tp"], result["fp"], result["fn"]) == (2, 0, 0)


def test_deferred_and_unmatched_same_identity_observation_count_as_fn_not_silent_coverage_loss():
    rows = [obs("a0", 0, "A", "t1"), obs("a1", 1, "A", "t1", status="deferred"),
            obs("a2", 2, "A", None, status="unmatched")]
    result = score.pairwise_association(rows)
    assert result["eligible_cross_frame_pairs"] == 3
    assert result["ground_truth_positive_pairs"] == 3
    assert result["deferred_or_unmatched_observations"] == 2
    assert (result["tp"], result["fp"], result["fn"]) == (0, 0, 3)


def test_exact_pose_frame_exclusion_is_scoring_only_and_does_not_mutate_history_rows():
    rows = [obs("a0", 0, "A", "t1"), obs("a1", 1, "A", "t1"), obs("a4", 4, "A", "t1")]
    before = [dict(x) for x in rows]
    all_frames = score.pairwise_association(rows)
    no_revisit = score.pairwise_association(rows, excluded_frame_indices={4})
    assert all_frames["tp"] == 3
    assert no_revisit["tp"] == 1
    assert no_revisit["excluded_frame_indices"] == [4]
    assert rows == before


def test_same_frame_collision_is_reported_separately_not_as_false_merge_or_fragmentation():
    rows = [obs("a0", 0, "A", "t1"), obs("b0", 0, "B", "t1")]
    result = score.identity_structure(rows)
    assert result["same_frame_collision_count"] == 1
    assert result["same_frame_collisions"][0]["physical_merge_status"] == "unreviewed"
    assert result["false_merge_track_count_by_evaluator_id"] == 0
    assert result["fragmented_instance_count_by_evaluator_id"] == 0


def test_diagnostic_membership_does_not_add_a_second_primary_observation_or_pair():
    relation = score.primary_diagnostic_relation({"gt_matched": True, "p3_decision": "accepted"})
    assert relation["in_primary_gt_population"] is True
    assert relation["in_primary_conditional_association_population"] is True
    assert relation["diagnostic_is_additional_scoring_observation"] is False
