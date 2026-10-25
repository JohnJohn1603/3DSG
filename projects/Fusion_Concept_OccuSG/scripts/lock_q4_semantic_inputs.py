#!/usr/bin/env python3
"""Create the immutable Q4 evaluator-input/config lock before scoring."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/object_graph_improvement/q4_semantic_baseline_v1_20261012"
sys.path.insert(0, str(ROOT / "scripts"))
from q4_semantic_evaluation import sha256  # noqa: E402

INPUTS = [
    "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1/observation_annotations.csv",
    "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1/identity_relations.json",
    "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1/case_order_mapping.json",
    "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1/manifest.json",
    "artifacts/audits/u4_semantic_fusion/u4_config.locked.json",
    "artifacts/audits/u4_semantic_fusion/u4_prediction_report.json",
    "artifacts/audits/u4_semantic_fusion/observations_source_dedup.jsonl",
    "artifacts/audits/u4_semantic_fusion/observations_geometry_only.jsonl",
    "artifacts/audits/u4_semantic_fusion/track_summary_source_dedup.json",
    "artifacts/audits/u4_semantic_fusion/track_summary_geometry_only.json",
    "artifacts/audits/u4_ov/u4_ov_config.locked.json",
    "artifacts/audits/u4_ov/vocabulary_v1.json",
    "artifacts/audits/u4_ov/u4_ov_prediction_report.json",
    "artifacts/audits/u4_ov/causal_track_prediction_report.json",
    "artifacts/audits/u4_ov/observations_predictions.jsonl",
    "artifacts/audits/u4_ov/causal_track_predictions.jsonl",
    "artifacts/audits/u4_ov/predicted_track_labels.jsonl",
    "artifacts/audits/u3_causal_prefix/causal_input_index.json",
    "artifacts/audits/object_graph_improvement/q3_raw_class_00802_main_v2_20261012/lambda_0.05/candidate_assignments.jsonl",
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "q4_contract.locked.json"
    if target.exists():
        raise RuntimeError(f"refusing to overwrite existing Q4 contract: {target}")
    hashes = {}
    for rel in INPUTS:
        path = ROOT / rel
        if not path.is_file():
            raise FileNotFoundError(path)
        hashes[rel] = sha256(path)
    contract = {
        "schema_version": 1,
        "status": "locked_before_q4_semantic_baseline_scoring",
        "experiment": "Q4 cached semantic baseline; no classifier/config changes",
        "development_only": True,
        "annotation": {
            "source_version": "u4-review-20261008-v1",
            "class_ground_truth_field": "reviewed_category only",
            "identity_fields_used_for_class": False,
            "class_truth_scope": "explicit user-reviewed labels on 18 full observation IDs; other predictor observations remain unreviewed",
            "identity_relations_used_only_for": ["identity-conditioned diagnostic grouping", "explicit mixed-identity track flags"],
            "taxonomy_edges_approved": [],
            "label_normalization": "lowercase and whitespace/underscore normalization only; no synonym or parent-child mapping",
        },
        "predictors": {
            "raw": "cached raw_detector_class per observation",
            "u4": "existing causal U4 class evidence; status confirmed required to emit label; otherwise abstain",
            "u4_ov": "existing locked image-text outputs; classified required to emit label; otherwise abstain",
            "ground_truth_loaded_for_prediction": False,
            "new_embeddings_or_extraction": False,
            "u4_ov_vocabulary_id": "indoor_objects_v1",
        },
        "scoring": {
            "causal_observation_unit": "one explicitly class-reviewed observation; compare raw, U4, U4-OV single-view and U4-OV causal-track at that observation time",
            "identity_conditioned_final_object": "only confirmed same_object relation components; identity supplied to evaluator, not predictor",
            "predicted_track_final_map": "frozen source_dedup and geometry_only assignments already consumed by U4; retain/list mixed and unassigned cases",
            "metrics": "strict exact-label only; abstentions remain in total denominator; report coverage and covered accuracy",
            "hierarchical_metrics": "unavailable until taxonomy edges are user-approved",
            "candidate_improvement": "not part of baseline scoring; only propose a separate one-mechanism development check if baseline reveals a specific opportunity",
        },
        "assignments": {
            "source_dedup": "available in the same hm3d-00802-main-dense observation namespace",
            "k4": "unavailable for the Q4 reviewed population; Q3 K4 assignment full IDs are hm3d-00802-main, not hm3d-00802-main-dense",
            "geometry_only": "available and is the frozen assignment source for existing U4-OV track predictions",
        },
        "inputs": hashes,
    }
    target.write_text(json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(target)
    print(json.dumps({"locked_inputs": len(hashes), "sha256": sha256(target)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
