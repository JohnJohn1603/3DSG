from types import SimpleNamespace

import numpy as np

from scripts.report_conceptgraphs_hybrid_quality import pair_metrics
from scripts.report_conceptgraphs_hybrid_quality import compare_reviewed_classes, reviewed_identity_coverage
from scripts.compare_conceptgraphs_reproducibility import arrays_equal_as_point_sets
from scripts.run_conceptgraphs_cached_hybrid import seed_mapping_rngs
from scripts.run_conceptgraphs_cached_hybrid import _stage_lineage
from scripts.run_conceptgraphs_cached_hybrid import _finalize_diagnostic_hypotheses


def test_pair_metrics_keeps_missing_tracks_as_negative_predictions():
    truth = {"a": "table_1", "b": "table_1", "c": "table_2"}
    frames = {"a": 0, "b": 1, "c": 2}
    predicted = {"a": "track-x", "b": "track-renamed", "c": None}
    metrics = pair_metrics(truth, predicted, frame_by_id=frames)
    assert (metrics["TP"], metrics["FP"], metrics["FN"], metrics["TN"]) == (0, 0, 1, 2)


def test_same_frame_pairs_are_excluded_only_when_requested():
    truth = {"a": "same", "b": "same"}
    frames = {"a": 0, "b": 0}
    predicted = {"a": "track", "b": "track"}
    all_pairs = pair_metrics(truth, predicted)
    cross_frame = pair_metrics(truth, predicted, frame_by_id=frames)
    assert all_pairs["TP"] == 1 and all_pairs["pair_count"] == 1
    assert cross_frame["TP"] == 0 and cross_frame["pair_count"] == 0


def test_stage_lineage_counts_frames_and_unique_poses_not_views():
    obj = {"image_idx": [0, 1, 2], "mask_idx": [0, 0, 0],
           "pcd": SimpleNamespace(points=np.asarray([[0, 0, 0], [1, 1, 1]], dtype=float)),
           "class_name": ["table", "couch", "table"]}
    ids = {0: ["obs-a"], 1: ["obs-b"], 2: ["obs-c"]}
    frames = [{"frame_index": i, "frame_id": f"frame-{i}"} for i in range(3)]
    poses = {"frame-0": np.eye(4), "frame-1": np.eye(4), "frame-2": np.eye(4)}
    row = _stage_lineage([obj], ids, frames, poses, "stage")[0]
    assert row["observation_ids"] == ["obs-a", "obs-b", "obs-c"]
    assert row["detection_count"] == 3
    assert row["distinct_frame_count"] == 3
    assert row["distinct_pose_count"] == 1
    assert row["class_history_raw"] == ["table", "couch", "table"]


def test_human_identity_loss_is_reported_separately_from_fragmentation():
    groups = {"table_1": ["a", "b"], "table_2": ["c", "d"]}
    metrics = reviewed_identity_coverage(groups, {"a": "track", "b": "track"})
    assert metrics["identities_present"] == 1
    assert metrics["identity_tokens_absent"] == ["table_2"]
    # No multi-track fragmentation is implied by an entirely absent identity.


def test_class_review_uses_class_csv_not_identity_token_and_keeps_hierarchy_unresolved():
    all_obs = {"couch_obs": {"raw_class": "couch"}, "dining_obs": {"raw_class": "dining table"}}
    rows = [
        {"observation_id": "couch_obs", "normalized_reviewed_label": "table", "class_status": "confirmed",
         "class_specificity": "general_label", "subtype_status": "unconfirmed"},
        {"observation_id": "dining_obs", "normalized_reviewed_label": "table", "class_status": "confirmed",
         "class_specificity": "general_label", "subtype_status": "unconfirmed"},
    ]
    got = compare_reviewed_classes(all_obs, rows)
    assert [x["comparison"] for x in got] == ["confirmed_label_mismatch", "taxonomy_relation_unresolved"]
    assert all(x["semantic_output"].startswith("unavailable") for x in got)


def test_fixed_seed_resets_numpy_rng_for_reference_noise():
    import sys
    from types import SimpleNamespace
    calls = []
    original = sys.modules.get("torch")
    sys.modules["torch"] = SimpleNamespace(manual_seed=lambda value: calls.append(value))
    try:
        seed_mapping_rngs(12345)
        first = np.random.normal(0, 4e-3, (5, 3))
        seed_mapping_rngs(12345)
        second = np.random.normal(0, 4e-3, (5, 3))
    finally:
        if original is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = original
    assert np.array_equal(first, second)
    assert calls == [12345, 12345]


def test_point_set_comparison_distinguishes_order_from_changed_samples():
    xyz = np.asarray([[0, 0, 0], [1, 0, 0]], dtype=np.float32)
    rgb = np.asarray([[10, 20, 30], [40, 50, 60]], dtype=np.uint8)
    assert arrays_equal_as_point_sets(xyz, rgb, xyz[::-1], rgb[::-1])
    assert not arrays_equal_as_point_sets(xyz, rgb, xyz + 0.001, rgb)


def test_filtered_hypotheses_are_not_misreported_as_merged(tmp_path):
    (tmp_path / "diagnostic").mkdir()
    rows = [
        {"lineage_key": "d2-a", "hypothesis_id": "hyp-a", "observation_ids": ["frame2:det4"], "detection_count": 1},
        {"lineage_key": "d2-b", "hypothesis_id": "hyp-b", "observation_ids": ["frame2:det5"], "detection_count": 1},
    ]
    manifest = _finalize_diagnostic_hypotheses(tmp_path, rows, [], [], min_detections=3)
    assert manifest["counts"] == {
        "before_filter": 2, "retained_after_filter": 0, "filtered": 2,
        "merged_or_repartitioned_after_filter": 0,
    }
    assert {r["postprocess_disposition"] for r in manifest["objects"]} == {"filtered_not_merged"}
    assert all(r["status_after_filter"] == "filtered" for r in manifest["objects"])
