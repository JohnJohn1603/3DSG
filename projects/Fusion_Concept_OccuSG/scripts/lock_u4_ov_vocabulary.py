#!/usr/bin/env python3
"""Freeze the zero-shot vocabulary, prompt, model and decision contract."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/u4_ov"

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

def main():
    vocab_path = OUT / "vocabulary_v1.json"
    vocab = json.loads(vocab_path.read_text())
    labels = [r["label"] for r in vocab["categories"]]
    if len(labels) != len(set(labels)):
        raise RuntimeError("duplicate labels in U4-OV vocabulary")
    if len(labels) < 70:
        raise RuntimeError("vocabulary is unexpectedly narrow")
    model_dir = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878"
    metadata_path = model_dir / "checkpoint_metadata.json"
    metadata = json.loads(metadata_path.read_text())
    checkpoint = model_dir / metadata["filename"]
    payload = {
        "schema_version": 1,
        "status": "locked_before_u4_ov_scoring",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "vocabulary_id": vocab["vocabulary_id"],
        "vocabulary_sha256": sha(vocab_path),
        "vocabulary_content_sha256": canonical_hash(vocab),
        "candidate_count": len(labels),
        "prompt_templates_sha256": canonical_hash(vocab["prompt_templates"]),
        "candidate_vocabulary_provenance": vocab["provenance"],
        "model": {
            "architecture": "ViT-B-32", "checkpoint_repo": "timm/vit_base_patch32_clip_224.openai",
            "revision": "a6f597a30f7b82c51704746581f9a4e41421e878",
            "checkpoint_sha256": sha(checkpoint), "checkpoint_bytes": checkpoint.stat().st_size,
            "u2_image_encoder_fingerprint": json.loads((ROOT / "artifacts/audits/dining_table_bed_224_230/appearance/extraction_report.json").read_text())["model_fingerprint"]
        },
        "classification": {
            "score": "cosine similarity between normalized image and mean normalized prompt text embeddings",
            "score_is_calibrated_probability": False,
            "top1_unknown_if_max_cosine_below": 0.20,
            "top1_conflicted_if_top1_minus_top2_below": 0.015,
            "thresholds_status": "fixed prototype defaults; not calibrated or selected against review/test outcomes",
            "raw_detector_class_is_candidate_gate": False,
            "multi_view": {
                "aggregation": "earliest observation per distinct 30-degree azimuth/elevation view bin; L2-normalize mean embedding",
                "minimum_distinct_known_view_bins": 2,
                "insufficient_view_status": "unknown",
                "unknown_view_bin_contributes_independent_support": False
            }
        },
        "evaluation_split_policy": {
            "development": "the 18 user-reviewed observations and four confirmed same-object groups",
            "fresh_test": "not available in current saved caches; do not score previously seen frames 0..278 as a new test",
            "ground_truth_used_for_vocabulary_or_prediction": False
        }
    }
    lock = OUT / "u4_ov_config.locked.json"
    if lock.exists():
        raise RuntimeError(f"lock already exists; refusing to overwrite: {lock}")
    lock.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps({"vocabulary_id": vocab["vocabulary_id"], "candidate_count": len(labels),
                      "vocabulary_sha256": payload["vocabulary_sha256"],
                      "config_sha256": sha(lock), "checkpoint_sha256": payload["model"]["checkpoint_sha256"]}, indent=2))

if __name__ == "__main__":
    main()
