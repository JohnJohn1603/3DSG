from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from scripts.report_hybrid_filter_ablation import _partition, verify_prefilter_equal
from scripts.run_conceptgraphs_cached_hybrid import apply_locked_experiment_config, validate_feature_population
from scripts.report_conceptgraphs_filter_sequence import attribute_postfilter_merges


def test_partition_comparison_ignores_renamed_object_ids():
    left = [{"lineage_key": "track_a", "observation_ids": ["o1", "o2"]},
            {"lineage_key": "track_b", "observation_ids": ["o3"]}]
    right = [{"lineage_key": "renamed_7", "observation_ids": ["o3"]},
             {"lineage_key": "renamed_8", "observation_ids": ["o2", "o1"]}]
    assert _partition(left) == _partition(right)


def test_locked_filter_ablation_changes_only_min_detections(tmp_path):
    base = {"association": "same", "obj_min_detections": 3, "merge": True}
    base_hash = hashlib.sha256(json.dumps(base, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    hashes = {"capture": "c", "p2": "p2", "p3": "p3", "text": "t"}
    spec = {"schema": "conceptgraphs-hybrid-filter-ablation/v1", "experiment_id": "test_min1",
            "base_effective_config_sha256": base_hash, "seed": 7,
            "input_hashes": hashes, "overrides": {"obj_min_detections": 1}}
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(spec))
    effective, metadata = apply_locked_experiment_config(base, path, 7, hashes)
    assert effective == {"association": "same", "obj_min_detections": 1, "merge": True}
    assert base["obj_min_detections"] == 3
    assert metadata["overrides"] == {"obj_min_detections": 1}

    spec["overrides"] = {"obj_min_detections": 1, "sim_threshold": 0.1}
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="exactly obj_min_detections=1"):
        apply_locked_experiment_config(base, path, 7, hashes)


def test_feature_population_accepts_non_u6_sequence_and_rejects_missing_rows():
    p2 = {f"hm3d-00802-main-{i:04d}:det-0000" for i in range(67)}
    validate_feature_population(p2, set(p2), set(p2))
    with pytest.raises(ValueError, match="feature population mismatch"):
        validate_feature_population(p2, set(p2) - {next(iter(p2))}, set(p2))


def test_merge_report_attributes_newly_admitted_hypotheses_to_min3_filter(tmp_path):
    min3 = tmp_path / "min3"
    min1 = tmp_path / "min1"
    for root in (min3, min1):
        (root / "diagnostic").mkdir(parents=True)
    (min3 / "diagnostic/diagnostic_manifest.json").write_text(json.dumps({"objects": [
        {"hypothesis_id": "h-kept", "status_after_filter": "retained"},
        {"hypothesis_id": "h-new", "status_after_filter": "filtered"},
    ]}))
    (min1 / "diagnostic/diagnostic_manifest.json").write_text(json.dumps({"objects": [
        {"hypothesis_id": "h-kept", "observation_ids": ["o1"]},
        {"hypothesis_id": "h-new", "observation_ids": ["o2"]},
    ]}))
    (min1 / "lineage_stages.json").write_text(json.dumps({"stages": {
        "after_text_gated_merge": [{"lineage_key": "merged", "observation_ids": ["o1", "o2"]}]
    }}))
    report = attribute_postfilter_merges(min3, min1)
    assert report["min1_merges"] == 1
    assert report["min1_merges_involving_newly_admitted_hypotheses"] == 1
    assert report["min1_merge_components"][0]["hypotheses_filtered_at_min3"] == ["h-new"]


def _make_mapping(root, rows, xyz, rgb):
    (root / "diagnostic/hypotheses").mkdir(parents=True, exist_ok=True)
    (root / "diagnostic/diagnostic_manifest.json").write_text(json.dumps({"objects": rows}))
    for row in rows:
        np.savez(root / "diagnostic" / row["cloud_npz"], xyz=xyz, rgb=rgb)


def test_prefilter_parity_checks_membership_and_geometry(tmp_path):
    base, candidate = tmp_path / "base", tmp_path / "candidate"
    for root in (base, candidate):
        root.mkdir()
    common = {"input_observation_ids": ["o1", "o2"], "stages": {
        "online_before_postprocess": [{"lineage_key": "g", "observation_ids": ["o1", "o2"]}],
        "after_denoise": [{"lineage_key": "g", "observation_ids": ["o1", "o2"]}],
    }}
    (base / "lineage_stages.json").write_text(json.dumps(common))
    renamed = {"input_observation_ids": ["o1", "o2"], "stages": {
        "online_before_postprocess": [{"lineage_key": "x", "observation_ids": ["o2", "o1"]}],
        "after_denoise": [{"lineage_key": "x", "observation_ids": ["o1", "o2"]}],
    }}
    (candidate / "lineage_stages.json").write_text(json.dumps(renamed))
    xyz = np.asarray([[0., 1., 2.], [3., 4., 5.]])
    rgb = np.asarray([[1, 2, 3], [4, 5, 6]], dtype=np.uint8)
    _make_mapping(base, [{"lineage_key": "g", "observation_ids": ["o1", "o2"], "cloud_npz": "hypotheses/a.npz",
                          "bounds_minmax": [[0, 1, 2], [3, 4, 5]], "cloud_sha256": "same"}], xyz, rgb)
    _make_mapping(candidate, [{"lineage_key": "x", "observation_ids": ["o2", "o1"], "cloud_npz": "hypotheses/b.npz",
                              "bounds_minmax": [[0, 1, 2], [3, 4, 5]], "cloud_sha256": "same"}], xyz, rgb)
    result = verify_prefilter_equal(base, candidate)
    assert result["online_before_postprocess_partition_equal"]
    assert result["geometry_xyz_rgb_exact"]

    (candidate / "diagnostic/hypotheses/b.npz").unlink()
    _make_mapping(candidate, [{"lineage_key": "x", "observation_ids": ["o2", "o1"], "cloud_npz": "hypotheses/b.npz",
                              "bounds_minmax": [[0, 1, 2], [3, 4, 5]], "cloud_sha256": "different"}], xyz + 1, rgb)
    with pytest.raises(AssertionError, match="geometry differs"):
        verify_prefilter_equal(base, candidate)
