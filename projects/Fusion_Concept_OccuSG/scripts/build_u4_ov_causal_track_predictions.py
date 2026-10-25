#!/usr/bin/env python3
"""Build causal multi-view track labels from frozen U2 embeddings only."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/u4_ov"
U3 = ROOT / "artifacts/audits/u3_causal_prefix"
sys.path.insert(0, str(ROOT / "scripts"))
from run_u4_ov_offline import normalize, score  # noqa: E402
from u4_ov_evaluation_core import causal_prefix_view_ids  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    started = time.perf_counter()
    lock_path = OUT / "u4_ov_config.locked.json"
    lock = json.loads(lock_path.read_text())
    pred_report = json.loads((OUT / "u4_ov_prediction_report.json").read_text())
    if pred_report.get("locked_config_sha256") != sha(lock_path):
        raise RuntimeError("locked U4-OV config changed since the prediction run")
    if pred_report.get("status") != "u4_ov_offline_predictions_ready_unscored":
        raise RuntimeError("expected frozen, unscored observation predictions")
    vocab_path = OUT / "vocabulary_v1.json"
    if pred_report.get("vocabulary_sha256") != sha(vocab_path):
        raise RuntimeError("locked U4-OV vocabulary changed since prediction")
    vocab = json.loads(vocab_path.read_text())
    labels = [row["label"] for row in vocab["categories"]]
    cache_path = next((OUT / "text_cache").glob("text_features__*.npz"))
    cache_sha = sha(cache_path)
    if cache_sha != pred_report.get("text_cache_sha256"):
        raise RuntimeError("text feature cache hash changed since prediction")
    with np.load(cache_path, allow_pickle=False) as archive:
        cache_labels = archive["labels"].astype(str).tolist()
        text_features = np.asarray(archive["label_features"], dtype=np.float32)
    if cache_labels != labels:
        raise RuntimeError("text cache vocabulary order mismatch")

    rows = [json.loads(line) for line in (OUT / "observations_predictions.jsonl").read_text().splitlines() if line]
    rows.sort(key=lambda row: (int(row["frame_index"]), row["observation_id"]))
    prefix_view_ids = causal_prefix_view_ids(rows)
    input_index = json.loads((U3 / "causal_input_index.json").read_text())
    feature_refs = {row["detection_id"]: row for row in input_index["records"]}
    feature_cache: dict[str, np.ndarray] = {}
    for row in rows:
        did = row["observation_id"]
        ref = feature_refs[did]
        feature_path = ROOT / ref["feature_ref"]
        if sha(feature_path) != ref["feature_sha256"]:
            raise RuntimeError(f"U2 feature hash mismatch for {did}")
        with np.load(feature_path, allow_pickle=False) as archive:
            feature_cache[did] = normalize(np.asarray(archive["image_feature"], dtype=np.float32))

    outputs = []
    for row in rows:
        selected = prefix_view_ids[row["observation_id"]]
        if row.get("u3_geometry_only_track_id") and len(selected) >= int(lock["classification"]["multi_view"]["minimum_distinct_known_view_bins"]):
            vector = normalize(np.mean([feature_cache[did] for did in selected], axis=0))
            result = score(vector, labels, text_features, lock)
        else:
            result = {"fused_semantic_label": None, "status": "unknown", "top1_candidate": None,
                      "top1_cosine": None, "top2_candidate": None, "top2_cosine": None,
                      "cosine_margin": None, "top5": [],
                      "score_semantics": "not classified: no track or fewer than two distinct known view bins"}
        outputs.append({"observation_id": row["observation_id"], "frame_id": row["frame_id"],
            "frame_index": int(row["frame_index"]), "u3_geometry_only_track_id": row.get("u3_geometry_only_track_id"),
            "causal_evidence_end_frame_id": row["frame_id"],
            "causal_distinct_view_bin_count": len(selected), "causal_selected_observation_ids": selected,
            "causal_fused_semantic_label": result["fused_semantic_label"], "causal_status": result["status"],
            "top1_candidate": result["top1_candidate"], "top1_cosine": result["top1_cosine"],
            "top2_candidate": result["top2_candidate"], "top2_cosine": result["top2_cosine"],
            "cosine_margin": result["cosine_margin"], "top5": result["top5"],
            "raw_detector_class_used_as_gate": False, "future_observations_used": False,
            "locked_config_sha256": sha(lock_path), "vocabulary_sha256": sha(vocab_path),
            "text_cache_sha256": cache_sha})

    path = OUT / "causal_track_predictions.jsonl"
    path.write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for row in outputs))
    from collections import Counter
    status_counts = Counter(row["causal_status"] for row in outputs)
    report = {"schema_version": 1, "status": "causal_track_predictions_ready_unscored",
        "input_observations": len(rows), "output_rows": len(outputs),
        "status_counts": dict(status_counts), "future_observations_used": False,
        "uses_frozen_u3_assignments_only": True, "u3_assignments_modified": False,
        "locked_config_sha256": sha(lock_path), "vocabulary_sha256": sha(vocab_path),
        "text_cache_sha256": cache_sha, "runtime_seconds": time.perf_counter() - started,
        "output": path.name,
        "definition": "At each observation time, normalize the mean U2 image embedding from earliest observations in distinct known view bins seen so far for that predicted U3 track; score with locked U4-OV text vectors. No GT/review/room labels are loaded."}
    (OUT / "causal_track_prediction_report.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
