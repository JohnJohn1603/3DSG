#!/usr/bin/env python3
"""Offline validation for the U2 feature cache and exhaustive pair table."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "artifacts/audits/dining_table_bed_224_230/appearance"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    u1_rows = [json.loads(line) for line in (ROOT / "artifacts/audits/dining_table_bed_224_230/pointsets/observation_pointset_lineage.jsonl").read_text().splitlines()]
    expected_ids = {row["observation_id"] for row in u1_rows if row.get("p3_decision") == "accepted"}
    manifest_history = [json.loads(line) for line in (AUDIT / "feature_manifest.jsonl").read_text().splitlines()]
    extraction = json.loads((AUDIT / "extraction_report.json").read_text())
    benchmark = json.loads((AUDIT / "benchmark_report.json").read_text())
    manifest = [row for row in manifest_history if row["preprocessing_fingerprint"] == extraction["preprocessing_fingerprint"]]
    if len(manifest) != 47 or {row["observation_id"] for row in manifest} != expected_ids:
        raise RuntimeError("active feature manifest observation set differs from the locked 47 accepted U1 observations")
    if extraction["preprocessing_fingerprint"] != benchmark["model"]["preprocessing_fingerprint"]:
        raise RuntimeError("benchmark and extraction preprocessing fingerprints differ")

    vectors: dict[str, np.ndarray] = {}
    class_by_id: dict[str, str] = {}
    for row in manifest:
        for field in ("rgb_sha256", "mask_sha256", "crop_sha256", "model_fingerprint", "preprocessing_fingerprint", "cache_key_sha256"):
            if not row.get(field):
                raise RuntimeError(f"cache-key provenance missing {field}: {row['observation_id']}")
        for path_field in ("crop_ref", "feature_ref"):
            path = (ROOT / row[path_field]).resolve()
            path.relative_to(ROOT)
            if not path.is_file():
                raise RuntimeError(f"cache file missing: {path}")
        crop_path = ROOT / row["crop_ref"]
        crop_rgb = np.asarray(Image.open(crop_path).convert("RGB"), dtype=np.uint8)
        crop_hash = hashlib.sha256(crop_rgb.tobytes()).hexdigest()
        if crop_hash != row["crop_sha256"]:
            raise RuntimeError(f"crop content hash mismatch: {row['observation_id']}")
        feature_path = ROOT / row["feature_ref"]
        if sha256_file(feature_path) != row["feature_sha256"]:
            raise RuntimeError(f"feature file SHA-256 mismatch: {row['observation_id']}")
        with np.load(feature_path, allow_pickle=False) as cache:
            if set(cache.files) != {"image_feature"}:
                raise RuntimeError(f"feature NPZ contains non-feature fields: {row['observation_id']}")
            vector = np.asarray(cache["image_feature"])
        if vector.shape != (512,) or vector.dtype != np.float32 or not np.isfinite(vector).all():
            raise RuntimeError(f"feature finite/shape/dtype contract failed: {row['observation_id']}")
        norm = float(np.linalg.norm(vector))
        if not np.isclose(norm, 1.0, atol=1e-5, rtol=1e-5):
            raise RuntimeError(f"feature L2 normalization failed: {row['observation_id']} norm={norm}")
        if abs(norm - row["feature_l2_norm"]) > 1e-6:
            raise RuntimeError(f"manifest feature norm differs from NPZ: {row['observation_id']}")
        vectors[row["observation_id"]] = vector
        class_by_id[row["observation_id"]] = row["class_canonical_review_only"]

    pair_path = AUDIT / "similarity_all_pairs.csv"
    seen_pairs = set()
    relation_counts = Counter()
    max_dot_error = 0.0
    with pair_path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"observation_id_left", "observation_id_right", "detection_id_left", "detection_id_right", "crop_ref_left", "crop_ref_right", "cosine_similarity", "m6_object_id_left_review_only", "m6_object_id_right_review_only"}
        if not required.issubset(reader.fieldnames or []):
            raise RuntimeError(f"similarity review CSV is missing columns: {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            left, right = row["observation_id_left"], row["observation_id_right"]
            if left not in vectors or right not in vectors or left == right:
                raise RuntimeError(f"invalid pair IDs: {left}, {right}")
            key = tuple(sorted((left, right)))
            if key in seen_pairs:
                raise RuntimeError(f"duplicate similarity pair: {key}")
            seen_pairs.add(key)
            score = float(row["cosine_similarity"])
            exact = float(np.clip(np.dot(vectors[left], vectors[right]), -1.0, 1.0))
            max_dot_error = max(max_dot_error, abs(score - exact))
            if not np.isfinite(score) or abs(score - exact) > 1e-7:
                raise RuntimeError(f"similarity value mismatch: {key}")
            same_class = class_by_id[left] == class_by_id[right]
            same_m6 = row["m6_object_id_left_review_only"] == row["m6_object_id_right_review_only"]
            relation_counts[("same_class" if same_class else "different_class") + ("_same_m6_object" if same_m6 else "_different_m6_objects")] += 1
    expected_pair_count = len(vectors) * (len(vectors) - 1) // 2
    if len(seen_pairs) != expected_pair_count:
        raise RuntimeError(f"pair table incomplete: {len(seen_pairs)} != {expected_pair_count}")

    if benchmark["status"] != "pass" or benchmark["benchmark_crop_count"] < 10 or not benchmark["repeatability"]["allclose_pass"]:
        raise RuntimeError("benchmark report is not a passing 10-20 crop gate")
    benchmark_id = benchmark["repeatability"]["observation_id"]
    benchmark_feature = vectors[benchmark_id]
    cross_process_sha256 = hashlib.sha256(benchmark_feature.tobytes()).hexdigest()
    cross_process_repeatability = cross_process_sha256 == benchmark["repeatability"]["first_feature_vector_sha256"]
    if not cross_process_repeatability:
        raise RuntimeError("benchmark and extraction feature differ for the same crop/model/preprocess")
    lock = json.loads((ROOT / "locks/appearance_encoder_proposal.json").read_text())
    checkpoint_path = ROOT / "artifacts/models/openclip" / f"vit-b-32-openai-{lock['encoder']['checkpoint_revision']}" / lock["encoder"]["checkpoint_file"]
    if sha256_file(checkpoint_path) != lock["encoder"]["checkpoint_sha256"]:
        raise RuntimeError("checkpoint SHA-256 differs from appearance encoder lock")

    report = {
        "schema_version": 1,
        "status": "pass",
        "verification": {
            "observations": len(vectors),
            "historical_cache_manifest_rows": len(manifest_history) - len(manifest),
            "active_cache_manifest_rows": len(manifest),
            "feature_shape_per_observation": [512],
            "feature_dtype": "float32",
            "all_features_finite": True,
            "all_features_l2_normalized_atol_rtol": 1e-5,
            "all_crop_hashes_match_saved_png": True,
            "all_feature_file_hashes_match_manifest": True,
            "feature_npz_contains_only_image_feature": True,
            "pair_count": len(seen_pairs),
            "expected_pair_count": expected_pair_count,
            "all_cosines_recomputed_from_cached_vectors": True,
            "max_cosine_csv_recompute_error": max_dot_error,
            "benchmark_crop_count": benchmark["benchmark_crop_count"],
            "repeatability_atol_rtol": [benchmark["repeatability"]["allclose_atol"], benchmark["repeatability"]["allclose_rtol"]],
            "repeatability_cosine": benchmark["repeatability"]["cosine_similarity"],
            "benchmark_to_extraction_cross_process_exact_repeatability": cross_process_repeatability,
            "pair_relation_counts_review_only": {key: int(value) for key, value in sorted(relation_counts.items())},
            "gt_used_for_features_or_match_selection": False,
            "identity_assigned_from_cosine": False,
        },
        "artifacts": {
            "feature_manifest": str((AUDIT / "feature_manifest.jsonl").relative_to(ROOT)),
            "similarity_csv": str(pair_path.relative_to(ROOT)),
            "benchmark_report": str((AUDIT / "benchmark_report.json").relative_to(ROOT)),
            "crop_dir": str((AUDIT / "crops").relative_to(ROOT)),
            "feature_dir": str((AUDIT / "features").relative_to(ROOT)),
        },
    }
    output = AUDIT / "validation_report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
