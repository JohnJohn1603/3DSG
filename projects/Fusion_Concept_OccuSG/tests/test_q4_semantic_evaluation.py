from scripts.q4_semantic_evaluation import (
    class_truth,
    causal_prefix_valid,
    correction_transitions,
    explicit_mixed_pairs,
    identity_groups,
    label_key,
    metric,
    view_support,
)
from scripts.run_q4_margin_candidate import emit_cached_candidate


def test_exact_label_normalization_does_not_map_taxonomy():
    assert label_key("kitchen_island") == label_key("kitchen island")
    assert label_key("table") != label_key("dining table")
    assert label_key("stove") != label_key("oven")


def test_metric_keeps_abstentions_and_unsupported_truth_in_denominator():
    result = metric(["piano", None, "chair"], ["piano", "printer", "armchair"])
    assert result == {
        "n": 3,
        "correct": 1,
        "wrong": 1,
        "abstain": 1,
        "coverage_total": 2 / 3,
        "accuracy_on_covered": 0.5,
        "correct_over_total": 1 / 3,
    }


def test_correction_harm_and_abstain_transitions_are_separate():
    got = correction_transitions(
        ["chair", "bed", "table", "sofa"],
        ["chair", "chair", None, "bed"],
        ["chair", "chair", "table", "sofa"],
    )
    assert got["correct_retained"] == 1
    assert got["wrong_to_correct"] == 1
    assert got["correct_to_abstain"] == 1
    assert got["correct_to_wrong"] == 1


def test_identity_is_built_only_from_reviewed_same_object_edges():
    rels = [
        {"left_observation_id": "a", "right_observation_id": "b", "relation": "same_object"},
        {"left_observation_id": "b", "right_observation_id": "c", "relation": "same_object"},
        {"left_observation_id": "a", "right_observation_id": "x", "relation": "different_objects"},
    ]
    assert identity_groups(rels) == [["a", "b", "c"]]
    assert explicit_mixed_pairs({"a", "x"}, rels) == [["a", "x"]]
    # Shared class is never passed to this identity relation function.


def test_aliases_and_revisits_do_not_increase_view_support():
    rows = [
        {"view_bin": "az1_el0"},
        {"view_bin": "az1_el0"},
        {"view_bin": "az2_el0"},
        {"view_bin": "az3_el0", "duplicate_alias": True},
        {"view_bin": "unknown"},
    ]
    assert view_support(rows) == 2


def test_causal_prefix_rejects_future_evidence_and_accepts_current_time():
    assert causal_prefix_valid({"frame_index": 18, "causal_evidence_end_frame_index": 18,
                                "future_observations_used": False})
    assert causal_prefix_valid({"frame_index": 18, "causal_evidence_end_frame_index": 17,
                                "future_observations_used": False})
    assert not causal_prefix_valid({"frame_index": 18, "causal_evidence_end_frame_index": 19,
                                    "future_observations_used": False})
    assert not causal_prefix_valid({"frame_index": 18, "future_observations_used": True})


def test_class_truth_ignores_identity_token_and_requires_user_review_provenance():
    assert class_truth({"reviewed_category": "chair", "identity_token": "obj_X",
                        "provenance": "user_reviewed"}) == "chair"
    assert class_truth({"reviewed_category": "chair", "identity_token": "obj_X",
                        "provenance": "unreviewed"}) is None


def test_margin_candidate_only_releases_conflicted_cached_top1():
    conflicted = {"status": "conflicted", "fused_semantic_label": None,
                  "top1_candidate": "piano", "top1_cosine": 0.24, "cosine_margin": 0.002}
    unknown = {**conflicted, "status": "unknown"}
    classified = {**conflicted, "status": "classified", "fused_semantic_label": "chair"}
    assert emit_cached_candidate(conflicted, "fused_semantic_label", "status", "top1_candidate",
                                 "top1_cosine", "cosine_margin", 0.2) == "piano"
    assert emit_cached_candidate(unknown, "fused_semantic_label", "status", "top1_candidate",
                                 "top1_cosine", "cosine_margin", 0.2) is None
    assert emit_cached_candidate(classified, "fused_semantic_label", "status", "top1_candidate",
                                 "top1_cosine", "cosine_margin", 0.2) == "chair"
    assert emit_cached_candidate({**conflicted, "top1_cosine": 0.19}, "fused_semantic_label", "status",
                                 "top1_candidate", "top1_cosine", "cosine_margin", 0.2) is None
