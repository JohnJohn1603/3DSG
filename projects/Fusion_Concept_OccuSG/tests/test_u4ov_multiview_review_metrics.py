import unittest

from scripts.u4ov_multiview_review_metrics import (
    class_metrics,
    normalize_label,
    pairwise_association_metrics,
)
from scripts import score_u4ov_multiview_user_review_v1 as scorer


class U4OVMultiviewReviewMetricTests(unittest.TestCase):
    def test_format_normalization_does_not_apply_semantic_mapping(self):
        self.assertEqual(normalize_label("  kitchen_island "), "kitchen island")
        self.assertEqual(normalize_label("stove"), "stove")
        self.assertNotEqual(normalize_label("stove"), normalize_label("oven"))

    def test_pairwise_uses_only_confirmed_cross_frame_identity_pairs(self):
        annotations = {
            "a": {"frame_index": 0, "identity_status": "confirmed", "identity_token": "obj_A"},
            "b": {"frame_index": 1, "identity_status": "confirmed", "identity_token": "obj_A"},
            "c": {"frame_index": 1, "identity_status": "confirmed", "identity_token": "obj_B"},
            "d": {"frame_index": 2, "identity_status": "uncertain", "identity_token": None},
        }
        assignments = {"a": {"track_id": "t1"}, "b": {"track_id": "t1"},
                       "c": {"track_id": "t1"}, "d": {"track_id": "t2"}}
        result = pairwise_association_metrics(annotations, assignments)
        self.assertEqual(result["scored_cross_frame_pairs"], 2)
        self.assertEqual(result["pairs_excluded_for_missing_or_uncertain_identity"], 3)
        self.assertEqual((result["TP"], result["FP"], result["FN"], result["TN"]), (1, 1, 0, 0))
        self.assertEqual(result["false_merge_track_count"], 1)
        self.assertEqual(result["fragmented_identity_count"], 0)

    def test_pairwise_reports_fragmentation_and_unmatched_positive_as_fn(self):
        annotations = {
            "a": {"frame_index": 0, "identity_status": "confirmed", "identity_token": "obj_A"},
            "b": {"frame_index": 1, "identity_status": "confirmed", "identity_token": "obj_A"},
            "c": {"frame_index": 2, "identity_status": "confirmed", "identity_token": "obj_B"},
        }
        assignments = {"a": {"track_id": "t1"}, "b": {"track_id": None}, "c": {"track_id": "t2"}}
        result = pairwise_association_metrics(annotations, assignments)
        self.assertEqual((result["TP"], result["FP"], result["FN"], result["TN"]), (0, 0, 1, 2))
        self.assertEqual(result["fragmented_identity_count"], 0)
        assignments["b"]["track_id"] = "t3"
        result = pairwise_association_metrics(annotations, assignments)
        self.assertEqual(result["fragmented_identity_count"], 1)
        self.assertEqual(result["fragmentation_excess_track_count"], 1)

    def test_class_metrics_separates_unsupported_uncertain_unreviewed_and_abstention(self):
        annotations = {
            "a": {"frame_index": 0, "class_status": "confirmed", "normalized_reviewed_label": "chair"},
            "b": {"frame_index": 1, "class_status": "confirmed", "normalized_reviewed_label": "kitchen island"},
            "c": {"frame_index": 2, "class_status": "uncertain", "normalized_reviewed_label": ""},
            "d": {"frame_index": 3, "class_status": "unreviewed", "normalized_reviewed_label": ""},
        }
        predictions = {"a": {"m": "chair"}, "b": {"m": "dining table"},
                       "c": {"m": "chair"}, "d": {"m": "chair"}}
        result = class_metrics(annotations, predictions, {"chair", "dining table"})
        self.assertEqual(result["eligible_observation_count"], 1)
        self.assertEqual(result["unsupported_truth_observations"][0]["reviewed_label"], "kitchen island")
        self.assertEqual(result["unreviewed_observation_ids"], ["d"])
        self.assertEqual(result["uncertain_class_observation_ids"], ["c"])
        self.assertEqual(result["m"]["correct"], 1)
        self.assertEqual(result["m"]["abstention"], 0)
        predictions["a"]["m"] = None
        self.assertEqual(class_metrics(annotations, predictions, {"chair"})["m"]["abstention"], 1)

    def test_final_track_class_does_not_majority_score_mixed_identity_track(self):
        scorer.VOCAB = {"chair"}
        annotations = {
            "a": {"frame_index": 0, "identity_status": "confirmed", "identity_token": "obj_A",
                  "class_status": "confirmed", "normalized_reviewed_label": "chair"},
            "b": {"frame_index": 1, "identity_status": "confirmed", "identity_token": "obj_B",
                  "class_status": "confirmed", "normalized_reviewed_label": "chair"},
            "c": {"frame_index": 2, "identity_status": "uncertain", "identity_token": None,
                  "class_status": "confirmed", "normalized_reviewed_label": "chair"},
        }
        assignments = {"test": {"a": {"track_id": "t"}, "b": {"track_id": "t"}, "c": {"track_id": "u"}}}
        single = {oid: {"observation_id": oid, "frame_index": i, "raw_detector_class": "chair"}
                 for i, oid in enumerate(annotations)}
        final = {("test", "t"): {"final_map_classification": {"status": "classified", "fused_semantic_label": "chair"}},
                 ("test", "u"): {"final_map_classification": {"status": "classified", "fused_semantic_label": "chair"}}}
        result = scorer.final_track_metrics(annotations, ["a", "b", "c"], single, assignments, final, "test")
        self.assertEqual(result["eligible_identity_pure_tracks"], 0)
        self.assertEqual(result["mixed_identity_tracks_excluded_no_majority_truth"][0]["identity_tokens"], ["obj_A", "obj_B"])
        self.assertEqual(len(result["tracks_without_confirmed_identity_excluded"]), 1)


if __name__ == "__main__":
    unittest.main()
