import json
import math

from scripts.compare_habitat_evaluations import compare


def _bundle(root, run_id, scene, resolution, value):
    root.mkdir()
    (root / "metrics.json").write_text(json.dumps({
        "protocol_id": "habitat_scene_eval_v1",
        "run_summary": {"evaluation_run_id": run_id, "scene_id": scene,
                        "dataset_type": "hm3d", "dataset_version": "v0.2",
                        "sequence_id": run_id, "evaluation_mode": "diagnostic_only",
                        "frame_count_input": 3, "room_macro_iou": None,
                        "object_mask_precision": None, "object_mask_recall": None,
                        "valid_depth_fraction": value, "trajectory_length_m": 1.0},
        "metrics": {
            "run_metadata": {"dataset_type": "hm3d", "dataset_version": "v0.2",
                             "resolution_px": resolution, "horizontal_fov_deg": 90,
                             "pipeline_config_sha256": "pipe", "detector_weights_sha256": "weights"},
            "operations": {"observation": {"valid_depth_fraction": {
                "value": value, "unit": "fraction", "numerator": int(value * 10), "denominator": 10,
                "sample_count": 10, "scope": "capture pixels", "gt_status": "not_applicable",
                "thresholds": {}, "unavailable_reason": None}}}},
    }))
    (root / "manifest.json").write_text(json.dumps({"input_hashes": {"capture_manifest": {"sha256": run_id}}}))


def test_comparison_pools_compatible_scenes_but_separates_sensor_resolution(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _bundle(a, "a", "scene_a", [160, 120], 0.9)
    _bundle(b, "b", "scene_b", [160, 120], 0.8)
    _bundle(c, "c", "scene_c", [320, 240], 0.7)
    result = compare([a, b, c], tmp_path / "comparison")
    rows = result["compatible_aggregates"]
    assert len(rows) == 2
    at_160 = next(row for row in rows if row["conditions"]["resolution_px"] == [160, 120])
    at_320 = next(row for row in rows if row["conditions"]["resolution_px"] == [320, 240])
    assert at_160["scene_count"] == 2
    assert math.isclose(at_160["macro_mean_per_scene"], 0.85)
    assert math.isclose(at_160["pooled_ratio_from_counts"], 0.85)
    assert at_320["scene_count"] == 1
    assert len((tmp_path / "comparison" / "metrics.csv").read_text().splitlines()) == 4
