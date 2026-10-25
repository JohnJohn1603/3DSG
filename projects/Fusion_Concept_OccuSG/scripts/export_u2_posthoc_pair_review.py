#!/usr/bin/env python3
"""Join evaluator-only strata after all U2 features and cosine pairs are fixed.

Every pair from similarity_all_pairs.csv is retained. GT metadata is never fed
back into feature generation, cache keys, pair generation, or match selection.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "artifacts/audits/dining_table_bed_224_230/appearance"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    resources = json.loads((ROOT / "locks/external_resources.json").read_text())
    match_lock = resources["files"]["dense_object_evaluation_observation_matches"]
    protocol_lock = resources["files"]["dense_object_evaluation_protocol"]
    matches_path = Path(match_lock["path"])
    protocol_path = Path(protocol_lock["path"])
    for path, lock in ((matches_path, match_lock), (protocol_path, protocol_lock)):
        if path.stat().st_size != lock["bytes"] or sha256_file(path) != lock["sha256"]:
            raise RuntimeError(f"posthoc evaluator input changed from lock: {path}")
    protocol = json.loads(protocol_path.read_text())
    if protocol.get("protocol_id") != "hm3d_object_gt_v1":
        raise RuntimeError("unexpected posthoc object evaluation protocol")
    evaluator_by_detection = {
        row["detection_id"]: row
        for row in (json.loads(line) for line in matches_path.read_text().splitlines())
    }
    source_pairs = AUDIT / "similarity_all_pairs.csv"
    rows_out = []
    relation_counts = Counter()
    scored_pairs = 0
    with source_pairs.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        original_fields = list(reader.fieldnames or [])
        for pair in reader:
            left = evaluator_by_detection.get(pair["detection_id_left"], {})
            right = evaluator_by_detection.get(pair["detection_id_right"], {})
            left_id = left.get("gt_semantic_id") if left.get("match_status") == "matched" else None
            right_id = right.get("gt_semantic_id") if right.get("match_status") == "matched" else None
            if left_id is None or right_id is None:
                gt_relation = "unknown_or_unmatched"
            elif int(left_id) == int(right_id):
                gt_relation = "same_gt_instance"
            else:
                gt_relation = "different_gt_instances"
            class_relation = "same_detector_class" if pair["class_left_review_only"] == pair["class_right_review_only"] else "different_detector_classes"
            m6_relation = "same_source_m6_object" if pair["m6_object_id_left_review_only"] == pair["m6_object_id_right_review_only"] else "different_source_m6_objects"
            row = {
                **pair,
                "gt_category_left_posthoc_only": left.get("gt_category"),
                "gt_semantic_id_left_posthoc_only": left_id,
                "gt_match_status_left_posthoc_only": left.get("match_status", "not_in_evaluator"),
                "gt_category_right_posthoc_only": right.get("gt_category"),
                "gt_semantic_id_right_posthoc_only": right_id,
                "gt_match_status_right_posthoc_only": right.get("match_status", "not_in_evaluator"),
                "gt_identity_relation_posthoc_only": gt_relation,
                "class_relation_review_only": class_relation,
                "m6_track_relation_review_only": m6_relation,
            }
            rows_out.append(row)
            relation_counts[(class_relation, m6_relation, gt_relation)] += 1
            scored_pairs += 1
    if scored_pairs != 47 * 46 // 2:
        raise RuntimeError(f"expected all 1081 pairs preserved, got {scored_pairs}")
    cross_class_same_gt = sum(
        count for (class_relation, _m6, gt_relation), count in relation_counts.items()
        if class_relation == "different_detector_classes" and gt_relation == "same_gt_instance"
    )
    same_class_diff_m6_gt_diff = sum(
        count for (class_relation, m6_relation, gt_relation), count in relation_counts.items()
        if class_relation == "same_detector_class" and m6_relation == "different_source_m6_objects" and gt_relation == "different_gt_instances"
    )
    output_csv = AUDIT / "similarity_pair_review_posthoc_gt.csv"
    extra = [
        "gt_category_left_posthoc_only", "gt_semantic_id_left_posthoc_only", "gt_match_status_left_posthoc_only",
        "gt_category_right_posthoc_only", "gt_semantic_id_right_posthoc_only", "gt_match_status_right_posthoc_only",
        "gt_identity_relation_posthoc_only", "class_relation_review_only", "m6_track_relation_review_only",
    ]
    with output_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=original_fields + extra)
        writer.writeheader()
        writer.writerows(rows_out)
    summary = {
        "schema_version": 1,
        "status": "pass",
        "rows": scored_pairs,
        "all_source_similarity_pairs_preserved": True,
        "posthoc_only": True,
        "gt_used_for_feature_creation_or_match_selection": False,
        "input_similarity_csv_sha256": sha256_file(source_pairs),
        "evaluator_matches_path": match_lock["path"],
        "evaluator_matches_sha256": match_lock["sha256"],
        "evaluator_protocol_id": protocol["protocol_id"],
        "evaluator_protocol_sha256": protocol_lock["sha256"],
        "pair_strata_counts": [
            {"detector_class_relation": key[0], "source_m6_relation": key[1], "posthoc_gt_identity_relation": key[2], "pairs": value}
            for key, value in sorted(relation_counts.items())
        ],
        "cross_class_same_gt_instance_pair_count": cross_class_same_gt,
        "same_class_different_m6_and_different_gt_pair_count": same_class_diff_m6_gt_diff,
        "output_csv": str(output_csv.relative_to(ROOT)),
    }
    output_json = AUDIT / "posthoc_pair_review_summary.json"
    output_json.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
