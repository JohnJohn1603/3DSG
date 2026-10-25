from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_hm3d_object_gt as ev  # noqa: E402


def test_one_to_one_assignment_maximizes_cardinality_before_iou():
    # Greedy best edge would take (pred0, gt0)=.90 and leave pred1 unmatched.
    # Correct matching keeps both eligible observations.
    matrix = np.asarray([[0.90, 0.80], [0.85, 0.10]])
    assert ev._assign(matrix, 0.50) == [(0, 1, 0.8), (1, 0, 0.85)]


def test_assignment_threshold_and_empty_inputs():
    assert ev._assign(np.asarray([[0.49, 0.0], [0.0, 0.50]]), 0.50) == [(1, 1, 0.5)]
    assert ev._assign(np.zeros((0, 2)), 0.5) == []
    assert ev._assign(np.zeros((2, 0)), 0.5) == []


def test_empty_denominator_is_unavailable_not_zero():
    result = ev._metric(0, 0, 0, scope="fixture")
    assert result["status"] == "unavailable"
    assert result["precision"] is None and result["recall"] is None and result["f1"] is None


def test_preflight_terminal_summary_is_compact_and_json_contains_full_hashes():
    preflight = {"status": "ready_with_limits", "run_id": "run-1", "scene_id": "scene-1", "frame_count": 29,
                 "resolution_px": [320, 240], "assets": {"semantic_descriptor": {"path": "a", "sha256": "secret-hash"}},
                 "mapping": {"status_counts": {"provisional": 4, "unsupported": 2}},
                 "readiness_notes": ["class_aware_incomplete"], "association_lineage": "endpoint-only", "issues": [],
                 "input_hashes": {"capture": "full-checksum"}}
    summary = ev.summarize_preflight(preflight)
    assert "run-1" in summary and "29 frames" in summary and "Blockers: none" in summary
    assert "full-checksum" not in summary and "secret-hash" not in summary


def test_descriptor_semantic_id_not_line_number_and_duplicate_rejected(tmp_path):
    descriptor = tmp_path / "scene.semantic.txt"
    descriptor.write_text('HM3D Semantic Annotations\n428,ABCDEF,"armchair",16\n912,123456,"couch",17\n', encoding="utf-8")
    parsed = ev.parse_descriptor(descriptor)
    assert parsed[428]["category"] == "armchair"
    assert parsed[912]["category"] == "couch"
    descriptor.write_text('HM3D Semantic Annotations\n428,ABCDEF,"armchair",16\n428,123456,"couch",17\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate semantic ID"):
        ev.parse_descriptor(descriptor)


def test_cache_key_components_change_key_and_mapping_is_independent():
    geometry_inputs = {"capture": "capturehash", "semantic_asset": "assethash", "renderer": "habitat-0.3.3",
                       "code": "renderer-code", "frames": [{"rgb": "a", "depth": "b", "meta": "c"}]}
    original = ev.hash_json(geometry_inputs)
    changed_depth = json.loads(json.dumps(geometry_inputs)); changed_depth["frames"][0]["depth"] = "different"
    assert ev.hash_json(changed_depth) != original
    # Category mappings are intentionally not part of the geometry-cache key.
    changed_mapping = {"category": "new mapping version"}
    assert ev.hash_json(geometry_inputs) == original
    assert changed_mapping


def test_protocol_does_not_infer_suppression_from_p3_or_nearest_node():
    assert "no association rerun" in ev.PROTOCOL["association"]["lineage_source"]
    assert "endpoint lineage only" in ev.PROTOCOL["association"]["incomplete_lineage"]
    assert "first_detection_id" in ev.PROTOCOL["association"]["allowed_fields"]


def test_semantic_renderer_environment_is_headless_and_does_not_inherit_desktop_gpu(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    env = ev.semantic_render_env({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "KEEP": "yes",
                                  "LIBGL_ALWAYS_SOFTWARE": "1", "MESA_LOADER_DRIVER_OVERRIDE": "llvmpipe",
                                  "GALLIUM_DRIVER": "llvmpipe"})
    assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env
    assert env["EGL_PLATFORM"] == "surfaceless"
    assert "MESA_LOADER_DRIVER_OVERRIDE" not in env
    assert "LIBGL_ALWAYS_SOFTWARE" not in env
    assert "GALLIUM_DRIVER" not in env
    assert env["KEEP"] == "yes"


def test_renderer_failure_summary_identifies_signal_without_blame_from_warnings():
    assert ev.renderer_failure_summary(-11, "libEGL warning: software rendering conflict") == \
        "renderer process terminated by SIGSEGV (-11)"
    assert ev.renderer_failure_summary(2, "first warning\nrenderer error") == \
        "renderer exited with code 2: renderer error"


def test_v2_alignment_gates_tolerate_sparse_pixel_noise_but_reject_broad_drift():
    assert ev.PROTOCOL_VERSION == "hm3d_object_gt_v2"
    checks = ev.render_alignment_checks(rgb_mean=0.5, rgb_p99=2.0,
        rgb_over_tolerance_fraction=0.015, valid_mask_equal=True,
        depth_p99=0.001, depth_over_tolerance_fraction=1 / 76800,
        unknown_semantic_ids=[])
    assert all(checks.values())
    broad_drift = ev.render_alignment_checks(rgb_mean=0.5, rgb_p99=2.0,
        rgb_over_tolerance_fraction=0.10, valid_mask_equal=True,
        depth_p99=0.001, depth_over_tolerance_fraction=0.0,
        unknown_semantic_ids=[])
    assert not broad_drift["rgb_pixels_over_tolerance"]
    depth_drift = ev.render_alignment_checks(rgb_mean=0.5, rgb_p99=2.0,
        rgb_over_tolerance_fraction=0.01, valid_mask_equal=True,
        depth_p99=0.04, depth_over_tolerance_fraction=0.0002,
        unknown_semantic_ids=[])
    assert not depth_drift["depth_p99"]
    assert not depth_drift["depth_over_tolerance_fraction"]


def _saved_results_fixture(tmp_path: Path, *, zero_detections: bool = False) -> tuple[Path, str]:
    run = tmp_path / "run-fixture"
    run.mkdir()
    frames = [{"frame_id": f"frame-{i}"} for i in range(3)]
    (run / "capture").mkdir()
    (run / "capture/capture_manifest.json").write_text(json.dumps({"scene_id": "scene-fixture", "resolution_px": [320, 240], "frames": frames}), encoding="utf-8")
    evaluation_id = "eval_valid"
    evaluation = run / "object_evaluations" / evaluation_id
    (evaluation / "review/images").mkdir(parents=True)
    (evaluation / "manifest.json").write_text(json.dumps({"status": "completed", "run_id": run.name, "finished_utc": "2026-01-01"}), encoding="utf-8")
    if zero_detections:
        frame_metrics = [{"frame_id": f"frame-{i}", "gt_instances": 0, "predictions": 0, "class_agnostic_matches": 0,
                          "class_agnostic_ignored_predictions": 0} for i in range(3)]
        ca = {"tp": 0, "fp": 0, "fn": 0, "scope": "fixture all-instance diagnostic", "status": "unavailable"}
        d2 = {"class_agnostic": ca, "class_aware_confirmed_mapping": {"status": "unavailable", "unavailable_reason": "no mappings"},
              "matched_mask_iou_mean": None, "matched_mask_iou_count": 0}
        rows = []
        p3 = {}
    else:
        frame_metrics = [
            {"frame_id": "frame-0", "gt_instances": 1, "predictions": 2, "class_agnostic_matches": 1, "class_agnostic_ignored_predictions": 1},
            {"frame_id": "frame-1", "gt_instances": 0, "predictions": 1, "class_agnostic_matches": 0, "class_agnostic_ignored_predictions": 0},
            {"frame_id": "frame-2", "gt_instances": 0, "predictions": 0, "class_agnostic_matches": 0, "class_agnostic_ignored_predictions": 0},
        ]
        ca = {"tp": 1, "fp": 1, "fn": 0, "scope": "fixture all-instance diagnostic", "status": "available"}
        d2 = {"class_agnostic": ca, "class_aware_confirmed_mapping": {"status": "unavailable", "unavailable_reason": "no confirmed mappings"},
              "matched_mask_iou_mean": 0.8, "matched_mask_iou_count": 1}
        rows = [
            {"frame_id": "frame-0", "detection_id": "frame-0:det-0000", "class_canonical": "chair", "match_status": "matched",
             "class_agnostic_outcome": "matched", "gt_category": "chair", "gt_semantic_id": 5, "mask_iou": 0.8,
             "p3_decision": "accepted", "p3_reason": "largest_cluster", "object_node_endpoint_ids": ["object-000001"], "lineage_status": "exact_snapshot_endpoint"},
            {"frame_id": "frame-0", "detection_id": "frame-0:det-0001", "class_canonical": "unknown", "match_status": "unmatched_or_ignored",
             "class_agnostic_outcome": "ignored", "gt_category": None, "gt_semantic_id": None, "mask_iou": None,
             "p3_decision": "accepted", "p3_reason": "largest_cluster", "object_node_endpoint_ids": [], "lineage_status": "unavailable_for_detection"},
            {"frame_id": "frame-1", "detection_id": "frame-1:det-0000", "class_canonical": "table", "match_status": "unmatched_or_ignored",
             "class_agnostic_outcome": "false_positive", "gt_category": None, "gt_semantic_id": None, "mask_iou": None,
             "p3_decision": "rejected", "p3_reason": "cluster_small", "object_node_endpoint_ids": [], "lineage_status": "unavailable_for_detection"},
        ]
        p3 = {"accepted": 1, "rejected": 0}
    metrics = {"detection2d": d2, "frames": frame_metrics, "p3": p3}
    (evaluation / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (evaluation / "observation_matches.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    for row in rows:
        image = evaluation / "review/images" / f"{row['frame_id']}__{row['detection_id'].rsplit(':',1)[-1]}.png"
        image.write_bytes(b"fixture")
    # A newer failed result must not become the implicit selection.
    failed = run / "object_evaluations/eval_failed"
    failed.mkdir()
    (failed / "manifest.json").write_text(json.dumps({"status": "failed", "finished_utc": "2099-01-01"}), encoding="utf-8")
    return run, evaluation_id


def test_object_results_summary_frames_and_frame_details_include_zero_detection_frames(tmp_path):
    run, eval_id = _saved_results_fixture(tmp_path)
    result = ev.load_object_results(run)
    assert result["evaluation_id"] == eval_id
    assert result["totals"] == {"eligible_gt": 1, "predictions": 3, "matched": 1, "false_positives": 1, "ignored": 1, "false_negatives": 0}
    assert result["frame_counts_match_saved_metrics"] == {"matched": True, "false_positives": True, "false_negatives": True}
    table = ev.format_object_results(result, frames=True)
    assert "Frame index bắt đầu từ 0" in table and "frame-2" not in table
    assert "    2 |            0 |     0 |        0 |" in table
    detail = ev.load_object_results(run, frame_index=0)
    assert detail["frame_details"][1]["status"] == "ignored"
    assert detail["frame_details"][0]["object_nodes"] == ["object-000001"]
    assert Path(detail["frame_details"][0]["review_image"]).is_file()
    assert "mask match không đồng nghĩa đúng class" in ev.format_object_results(result)


def test_object_results_summary_is_grouped_and_frame_table_has_aligned_columns(tmp_path):
    run, _ = _saved_results_fixture(tmp_path)
    result = ev.load_object_results(run)
    summary = ev.format_object_results(result)
    assert "TOTAL OBSERVATIONS" in summary
    assert "  Eligible GT observations       1" in summary
    assert "  Matched P3 accepted / rejected" in summary
    assert "Metric | Count" not in summary
    frame_table = ev.format_object_results(result, frames=True)
    assert "P3 accepted" in frame_table and "P3 rejected" in frame_table


def test_object_results_zero_detections_and_n_a_reason(tmp_path):
    run, _ = _saved_results_fixture(tmp_path, zero_detections=True)
    result = ev.load_object_results(run)
    assert result["totals"] == {"eligible_gt": 0, "predictions": 0, "matched": 0, "false_positives": 0, "ignored": 0, "false_negatives": 0}
    assert "N/A (no matched masks)" in ev.format_object_results(result)
    assert "N/A (saved P3 metrics unavailable)" in ev.format_object_results(result)
    assert "Không có detection" in ev.format_object_results(ev.load_object_results(run, frame_index=2), frame=2)


def test_legacy_unmatched_or_ignored_rows_are_classified_from_saved_masks_and_gt_protocol(tmp_path):
    run, eval_id = _saved_results_fixture(tmp_path)
    evaluation = run / "object_evaluations" / eval_id
    matches_path = evaluation / "observation_matches.jsonl"
    rows = [json.loads(line) for line in matches_path.read_text().splitlines()]
    for row in rows:
        row.pop("class_agnostic_outcome", None)
    matches_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    descriptor = tmp_path / "scene.semantic.txt"
    descriptor.write_text('HM3D Semantic Annotations\n5,AAAAAA,"chair",1\n9,BBBBBB,"wall",1\n', encoding="utf-8")
    (run / "effective_evaluation_profile.json").write_text(json.dumps({"dataset": {"semantic_descriptor_evaluator_only": str(descriptor)}}), encoding="utf-8")
    cache = tmp_path / "gt-cache"; (cache / "frames").mkdir(parents=True)
    np.save(cache / "frames/frame-0__semantic_id_map.npy", np.full((2, 2), 9, dtype=np.uint32))
    np.save(cache / "frames/frame-1__semantic_id_map.npy", np.full((2, 2), 5, dtype=np.uint32))
    (evaluation / "gt_validation.json").write_text(json.dumps({"cache_path": str(cache)}), encoding="utf-8")
    masks_dir = run / "detection_2d/masks"; masks_dir.mkdir(parents=True)
    detections = []
    for fid, did, klass in (("frame-0", "frame-0:det-0001", "unknown"), ("frame-1", "frame-1:det-0000", "table")):
        filename = f"{did.replace(':', '__')}.npy"; mask_path = masks_dir / filename
        np.save(mask_path, np.ones((2, 2), dtype=bool))
        detections.append({"detection_id": did, "class_canonical": klass, "mask_file": f"masks/{filename}",
                           "mask_sha256": ev.sha256(mask_path), "image_shape_hw": [2, 2]})
    (run / "detection_2d/detection_2d_index.json").write_text(json.dumps({"frames": [
        {"frame_id": "frame-0", "detections": [detections[0]]}, {"frame_id": "frame-1", "detections": [detections[1]]}]}), encoding="utf-8")
    result = ev.load_object_results(run, frame_index=0)
    statuses = {row["detection_id"]: row["status"] for row in result["frame_details"]}
    assert statuses["det-0001"] == "ignored"
    second = ev.load_object_results(run, frame_index=1)
    assert second["frame_details"][0]["status"] == "false_positive"


def test_object_results_missing_metrics_invalid_eval_and_inconsistent_totals_fail(tmp_path):
    run, eval_id = _saved_results_fixture(tmp_path)
    invalid = run / "object_evaluations/eval_failed"
    with pytest.raises(ValueError, match="không completed/hợp lệ"):
        ev.load_object_results(run, evaluation_id=invalid.name)
    missing = run / "object_evaluations/eval_missing_metrics"
    missing.mkdir()
    (missing / "manifest.json").write_text(json.dumps({"status": "completed", "finished_utc": "2098-01-01"}), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="thiếu metrics.json"):
        ev.load_object_results(run, evaluation_id=missing.name)
    metrics_path = run / "object_evaluations" / eval_id / "metrics.json"
    doc = json.loads(metrics_path.read_text()); doc["detection2d"]["class_agnostic"]["fp"] = 0
    metrics_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="per-frame counts"):
        ev.load_object_results(run, evaluation_id=eval_id)
