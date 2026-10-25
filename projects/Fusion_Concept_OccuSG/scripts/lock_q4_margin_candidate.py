#!/usr/bin/env python3
"""Lock one post-hoc U4-OV margin-gate candidate before candidate scoring."""
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
    "artifacts/audits/u4_ov/u4_ov_config.locked.json",
    "artifacts/audits/u4_ov/vocabulary_v1.json",
    "artifacts/audits/u4_ov/observations_predictions.jsonl",
    "artifacts/audits/u4_ov/causal_track_predictions.jsonl",
    "artifacts/audits/u4_ov/predicted_track_labels.jsonl",
    "artifacts/audits/object_graph_improvement/q4_semantic_baseline_v1_20261012/q4_semantic_baseline_report.json",
]

def main() -> int:
    target = OUT / "q4_margin_candidate_contract.locked.json"
    if target.exists():
        raise RuntimeError(f"refusing to overwrite candidate contract: {target}")
    inputs = {}
    for rel in INPUTS:
        p = ROOT / rel
        if not p.is_file():
            raise FileNotFoundError(p)
        inputs[rel] = sha256(p)
    config = json.loads((ROOT / INPUTS[2]).read_text())
    baseline_margin = float(config["classification"]["top1_conflicted_if_top1_minus_top2_below"])
    min_cos = float(config["classification"]["top1_unknown_if_max_cosine_below"])
    doc = {
        "schema_version": 1,
        "status": "locked_before_q4_margin_candidate_scoring",
        "candidate_id": "u4ov_margin_veto_removed_dev_v1",
        "hypothesis": "Some exact-label-correct top-1 candidates are abstained solely by the locked margin veto; removing only that veto may improve exact correctness coverage, with possible harmful forced labels.",
        "one_mechanism": "For existing status=conflicted predictions only, emit cached top1 when top1 cosine >= existing locked floor and margin >= 0; keep unknown outputs unknown and keep already-classified outputs unchanged.",
        "parameters": {"baseline_min_margin": baseline_margin, "candidate_min_margin": 0.0,
            "top1_min_cosine_unchanged": min_cos, "model_checkpoint_unchanged": True,
            "vocabulary_unchanged": True, "prompts_unchanged": True,
            "image_features_or_text_embeddings_recomputed": False},
        "selection_rule": "Retain only for further development if exact correct_over_total on the fixed 18 confirmed class labels increases and correct_to_wrong remains zero; otherwise reject. No hierarchy credit.",
        "split": "Previously reviewed development observations; not a benchmark or holdout.",
        "predictor_inputs": {"GT_or_identity_used": False, "future_frames_used": False},
        "inputs": inputs,
    }
    target.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(target)
    print(json.dumps({"inputs": len(inputs), "sha256": sha256(target), "parameters": doc["parameters"]}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
