#!/usr/bin/env python3
"""Score the six explicitly reviewed U4 cases without expanding their scope."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = ROOT / "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1"
U4 = ROOT / "artifacts/audits/u4_semantic_fusion"
U3 = ROOT / "artifacts/audits/u3_causal_prefix"
OUT = ROOT / "artifacts/audits/u4_user_review/u4_development_results.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    annotations = list(csv.DictReader((VERSION / "observation_annotations.csv").open(encoding="utf-8")))
    annotation_by_id = {r["observation_id"]: r for r in annotations}
    u4_rows = {r["detection_id"]: r for r in
               (json.loads(line) for line in (U4 / "observations_geometry_only.jsonl").read_text().splitlines() if line)}
    assignments = {r["detection_id"]: r for r in
                   (json.loads(line) for line in (U3 / "decisions_geometry_only.jsonl").read_text().splitlines() if line)}
    for did in annotation_by_id:
        if did not in u4_rows or did not in assignments:
            raise RuntimeError(f"reviewed observation missing from locked U3/U4 outputs: {did}")

    identity = json.loads((VERSION / "identity_relations.json").read_text())["relations"]
    unique_rel = {}
    for row in identity:
        pair = tuple(sorted((row["left_observation_id"], row["right_observation_id"])))
        if pair not in unique_rel or row["relation"] != "uncertain":
            unique_rel[pair] = row["relation"]
    # If a pair is uncertain in one case and confirmed in another, the explicit
    # confirmed relation wins. Only the printer-printer pair remains uncertain.
    tp = fp = fn = uncertain = 0
    for (a, b), expected in unique_rel.items():
        if expected == "uncertain":
            uncertain += 1
            continue
        same_pred = assignments[a].get("track_id") == assignments[b].get("track_id")
        same_expected = expected == "same_object"
        if same_expected and same_pred: tp += 1
        elif same_expected and not same_pred: fn += 1
        elif not same_expected and same_pred: fp += 1
    false_merge_tracks = sorted({assignments[a]["track_id"] for (a, b), rel in unique_rel.items()
                                 if rel == "different_objects" and assignments[a].get("track_id") == assignments[b].get("track_id")})

    confirmed = [r for r in annotations if u4_rows[r["observation_id"]]["class_status"] == "confirmed"
                 and u4_rows[r["observation_id"]].get("resolved_class")]
    correct = [r for r in confirmed if u4_rows[r["observation_id"]]["resolved_class"] == r["reviewed_category"]]
    direct_mismatch = [r for r in confirmed if u4_rows[r["observation_id"]]["resolved_class"] != r["reviewed_category"]]
    abstained = [r for r in annotations if r not in confirmed]
    # Only exact chair labels are directly comparable to the locked detector
    # vocabulary without accepting an unreviewed ontology mapping.
    eligible = [r for r in annotations if r["reviewed_category"] == "chair"]
    eligible_confirmed = [r for r in eligible if u4_rows[r["observation_id"]]["class_status"] == "confirmed"
                          and u4_rows[r["observation_id"]].get("resolved_class")]
    eligible_correct = [r for r in eligible_confirmed
                        if u4_rows[r["observation_id"]]["resolved_class"] == r["reviewed_category"]]

    rows = []
    for a in annotations:
        u = u4_rows[a["observation_id"]]
        p = assignments[a["observation_id"]]
        rows.append({"frame_id": a["frame_id"], "frame_index": a["frame_index"],
                     "detection_id": a["detection_id"], "observation_id": a["observation_id"],
                     "reviewed_category": a["reviewed_category"], "case_refs": a["case_refs"],
                     "geometry_only_track_id": p.get("track_id"),
                     "initial_class": u.get("initial_class"), "resolved_class": u.get("resolved_class"),
                     "class_status": u.get("class_status"),
                     "exact_review_label_match": (u.get("resolved_class") == a["reviewed_category"])
                         if u.get("class_status") == "confirmed" and u.get("resolved_class") else None})
    with (VERSION.parent / "scored_observation_table.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    case1_same = tuple(sorted(("hm3d-00802-main-dense-0010:det-0005", "hm3d-00802-main-dense-0238:det-0000")))
    counts_by_class = Counter(r["reviewed_category"] for r in annotations)
    results = {
        "schema_version": 1, "status": "six_case_development_review_scored_not_benchmark",
        "scope": "18 unique reviewed observations only; no annotations propagated to other track observations",
        "annotation_version": "u4-review-20261008-v1",
        "annotation_manifest_sha256_before_result_file": sha(VERSION / "manifest.json"),
        "locked_configs": {
            "u3_geometry_only_sha256": sha(U3 / "association_config.locked.json"),
            "u3_geometry_only_decisions_sha256": sha(U3 / "decisions_geometry_only.jsonl"),
            "u4_config_sha256": sha(U4 / "u4_config.locked.json"),
            "u4_geometry_only_observations_sha256": sha(U4 / "observations_geometry_only.jsonl")},
        "association": {
            "metric": "pairwise identity on unique explicitly confirmed user-reviewed observation pairs; case-1 printer/printer relation excluded as uncertain",
            "pairwise_TP": tp, "pairwise_FP": fp, "pairwise_FN": fn, "excluded_uncertain_pairs": uncertain,
            "pairwise_precision": tp/(tp+fp) if tp+fp else None,
            "pairwise_recall": tp/(tp+fn) if tp+fn else None,
            "confirmed_false_merge_track_ids": false_merge_tracks,
            "false_merge_observation_pairs": [list(pair) for pair, rel in unique_rel.items()
                if rel == "different_objects" and assignments[pair[0]].get("track_id") == assignments[pair[1]].get("track_id")],
            "printer_first_vs_last_relation": "uncertain; not scored"},
        "class_fusion": {
            "unique_reviewed_observations": len(annotations),
            "reviewed_label_counts": dict(sorted(counts_by_class.items())),
            "confirmed_predictions": len(confirmed), "abstentions": len(abstained),
            "confirmed_coverage": len(confirmed)/len(annotations),
            "literal_exact_label_matches_among_confirmed": len(correct),
            "literal_exact_label_mismatches_among_confirmed": len(direct_mismatch),
            "literal_exact_label_precision": len(correct)/len(confirmed) if confirmed else None,
            "direct_mismatch_observations": [{"frame_id": r["frame_id"], "detection_id": r["detection_id"],
                "reviewed_label": r["reviewed_category"], "resolved_class": u4_rows[r["observation_id"]]["resolved_class"],
                "track_id": u4_rows[r["observation_id"]]["track_id"]} for r in direct_mismatch],
            "supported_exact_label_subset": {"category": "chair", "reviewed": len(eligible),
                "confirmed": len(eligible_confirmed), "correct": len(eligible_correct),
                "confirmed_precision": len(eligible_correct)/len(eligible_confirmed) if eligible_confirmed else None,
                "coverage": len(eligible_confirmed)/len(eligible) if eligible else None},
            "corrections_to_confirmed_reviewed_label": 0,
            "harmful_confirmed_label_mismatches": len(direct_mismatch),
            "causal_transition_note": "The observed final mismatches are not all proof that fusion worsened the initial prediction: initial-to-review mappings for armchair, generic table, printer and piano are not confirmed; no majority class was assigned to the mixed-identity track.",
            "abstention_observation_ids": [r["observation_id"] for r in abstained]},
        "unsupported_or_unresolved_category_vocabulary": {
            "armchair": "user-reviewed observation label; armchair-to-chair mapping remains unconfirmed",
            "printer": "user-reviewed observation label; no confirmed detector/evaluator mapping",
            "piano": "user-reviewed observation label; no confirmed detector/evaluator mapping",
            "table": "user-reviewed generic label; dining_table subtype explicitly unconfirmed",
            "chair": "exact label used only for the three reviewed observations; no broader propagation"},
        "case_1_identity": {"frame10_vs_frame230": "different objects confirmed",
                             "frame230_vs_frame238": "different objects confirmed",
                             "frame10_vs_frame238": "uncertain"},
        "predictor_leakage": False,
        "development_only": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"output": str(OUT), "reviewed_observations": len(annotations),
                      "association": results["association"], "class_fusion": results["class_fusion"]},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
