import unittest

from scripts.u4_ov_evaluation_core import (
    causal_prefix_view_ids,
    confirmed_label_mapping,
    mapped_class_metrics,
    taxonomy_relation,
)


def mapping(label, category, status="confirmed", reviewer="reviewer", date="2026-10-08", decision="accepted"):
    return {"fused_semantic_label": label, "evaluator_category_candidate": category,
            "status": status, "reviewer": reviewer, "review_date": date, "decision": decision}


class U4OVEvaluationCoreTests(unittest.TestCase):
    def test_provisional_mapping_is_never_scored(self):
        rows = [mapping("cat", "cat", status="provisional", reviewer=None, date=None, decision=None)]
        self.assertEqual(confirmed_label_mapping(rows), {})
        metrics = mapped_class_metrics([
            {"match_status": "matched", "gt_category": "cat", "predicted_label": "cat"}
        ], rows)
        self.assertEqual(metrics["status"], "unavailable_no_eligible_confirmed_mapping_truths")
        self.assertEqual(metrics["denominator"], 0)

    def test_mapping_metrics_handle_abstention_and_unsupported_truth(self):
        rows = [mapping("cat", "cat"), mapping("dog", "dog")]
        records = [
            {"match_status": "matched", "gt_category": "cat", "predicted_label": "cat"},
            {"match_status": "matched", "gt_category": "cat", "predicted_label": "dog"},
            {"match_status": "matched", "gt_category": "dog", "predicted_label": None},
            {"match_status": "matched", "gt_category": "chair", "predicted_label": "cat"},
            {"match_status": "unmatched_or_ignored", "gt_category": None, "predicted_label": "cat"},
        ]
        metrics = mapped_class_metrics(records, rows)
        self.assertEqual(metrics["denominator"], 3)
        self.assertEqual(metrics["correct"], 1)
        self.assertEqual(metrics["mapped_prediction_count"], 2)
        self.assertEqual(metrics["abstention_or_unsupported_count"], 1)
        self.assertAlmostEqual(metrics["coverage"], 2 / 3)
        self.assertAlmostEqual(metrics["precision_micro"], 1 / 2)
        self.assertAlmostEqual(metrics["recall_micro"], 1 / 3)
        self.assertEqual(metrics["excluded_counts"]["excluded_gt_category_without_confirmed_mapping"], 1)
        self.assertEqual(metrics["excluded_counts"]["excluded_unmatched_or_uncertain_gt"], 1)

    def test_mapping_conflict_fails_closed(self):
        with self.assertRaises(ValueError):
            confirmed_label_mapping([mapping("chair", "chair"), mapping("chair", "seat")])

    def test_taxonomy_specificity_is_pending_until_edge_approval(self):
        hierarchy = {"dining table": "table", "armchair": "chair"}
        self.assertEqual(taxonomy_relation("dining table", "table", hierarchy),
                         "prediction_is_specific_to_reviewed_general_label_pending_approval")
        self.assertEqual(taxonomy_relation("chair", "armchair", hierarchy),
                         "prediction_is_general_to_reviewed_specific_label_pending_approval")
        self.assertEqual(taxonomy_relation("bed", "table", hierarchy), "unrelated_or_conflicting")
        self.assertEqual(taxonomy_relation("dining_table", "table", hierarchy),
                         "prediction_is_specific_to_reviewed_general_label_pending_approval")
        self.assertEqual(taxonomy_relation("dining table", "table", hierarchy, {("dining table", "table")}),
                         "confirmed_specific_to_general_match")

    def test_causal_prefix_never_uses_future_or_duplicate_view(self):
        rows = [
            {"observation_id": "o1", "u3_geometry_only_track_id": "t1", "view_bin": "a"},
            {"observation_id": "o2", "u3_geometry_only_track_id": "t1", "view_bin": "b"},
            {"observation_id": "o3", "u3_geometry_only_track_id": "t1", "view_bin": "c", "duplicate_alias": True},
        ]
        result = causal_prefix_view_ids(rows)
        self.assertEqual(result["o1"], ["o1"])
        self.assertEqual(result["o2"], ["o1", "o2"])
        self.assertEqual(result["o3"], ["o1", "o2"])


if __name__ == "__main__":
    unittest.main()
