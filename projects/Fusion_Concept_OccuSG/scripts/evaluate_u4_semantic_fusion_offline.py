#!/usr/bin/env python3
"""Posthoc U4 audit; evaluator labels are read only after predictions are frozen."""
from __future__ import annotations

import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/u4_semantic_fusion"
MODES = ("source_raw", "source_dedup", "geometry_only", "geometry_appearance")


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def locked_resource(name: str) -> tuple[Path, dict]:
    lock = json.loads((ROOT / "locks/external_resources.json").read_text())["files"][name]
    path = Path(lock["path"])
    if path.stat().st_size != lock["bytes"] or sha_file(path) != lock["sha256"]:
        raise RuntimeError(f"posthoc evaluator resource changed: {name}")
    return path, lock


def main() -> int:
    prediction_report = json.loads((OUT / "u4_prediction_report.json").read_text())
    if prediction_report.get("status") != "causal_u4_predictions_ready_unscored":
        raise RuntimeError("U4 prediction artifacts are not frozen/unscored")
    config_hash = sha_file(OUT / "u4_config.locked.json")
    if config_hash != prediction_report["config_sha256"]:
        raise RuntimeError("U4 config changed after prediction phase")

    matches_path, matches_lock = locked_resource("dense_object_evaluation_observation_matches")
    evaluator = {row["detection_id"]: row for row in
                 (json.loads(line) for line in matches_path.read_text().splitlines() if line)}
    mapping_path = Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058/object_evaluations/eval_20261006T184217.537953Z_880c87/category_mapping_snapshot.json")
    if not mapping_path.is_file():
        raise RuntimeError("locked baseline evaluator mapping snapshot is missing")
    prereview = json.loads((OUT / "u4_prereview_manifest.json").read_text())
    if sha_file(mapping_path) != prereview["category_mapping_snapshot_sha256"]:
        raise RuntimeError("category mapping snapshot changed since prereview")
    mapping = json.loads(mapping_path.read_text())
    confirmed_categories = {
        category: row["detector_class"]
        for category, row in mapping["categories"].items()
        if row.get("status") == "confirmed"
        and isinstance(row.get("detector_class"), str)
        and isinstance(row.get("provenance"), dict)
        and all(isinstance(row["provenance"].get(k), str) and row["provenance"][k].strip()
                for k in ("reviewer", "date", "decision"))
    }

    mode_reports = {}
    all_rows_by_mode = {}
    for mode in MODES:
        summaries = json.loads((OUT / f"track_summary_{mode}.json").read_text())
        rows = [json.loads(line) for line in
                (OUT / f"observations_{mode}.jsonl").read_text().splitlines() if line]
        all_rows_by_mode[mode] = rows
        categories_by_track: dict[str, set[str]] = defaultdict(set)
        ids_by_track: dict[str, set[int]] = defaultdict(set)
        for row in rows:
            gt = evaluator.get(row["detection_id"], {})
            if gt.get("match_status") == "matched" and gt.get("gt_semantic_id") is not None and row.get("track_id"):
                ids_by_track[row["track_id"]].add(int(gt["gt_semantic_id"]))
                if gt.get("gt_category"):
                    categories_by_track[row["track_id"]].add(gt["gt_category"])
        confirmed = [s for s in summaries.values() if s["class_status"] == "confirmed"]
        delays = [s["confirmation_delay_s"] for s in confirmed if s["confirmation_delay_s"] is not None]
        view_counts = [len(s.get("confirmation_view_bins") or []) for s in confirmed]
        changes = [s for s in summaries.values()
                   if s["resolved_class"] is not None and s["initial_class"] != s["resolved_class"]]
        mode_reports[mode] = {
            "assignment_track_count": len(summaries),
            "confirmed_tracks": len(confirmed),
            "confirmed_track_coverage": len(confirmed) / len(summaries) if summaries else None,
            "confirmed_track_time_to_confirm_s": {
                "count": len(delays),
                "median": statistics.median(delays) if delays else None,
                "p90": sorted(delays)[max(0, int(0.9 * (len(delays) - 1)))] if delays else None,
            },
            "independent_view_bins_at_first_confirmation": {
                "count": len(view_counts),
                "median": statistics.median(view_counts) if view_counts else None,
                "min": min(view_counts) if view_counts else None,
                "max": max(view_counts) if view_counts else None,
            },
            "tracks_with_confirmed_final_class_different_from_initial": len(changes),
            "class_correction_events": 0,
            "deconfirmation_events": 0,
            "evaluator_matched_observations": sum(
                evaluator.get(r["detection_id"], {}).get("match_status") == "matched" for r in rows),
            "evaluator_matched_observations_with_track": sum(
                evaluator.get(r["detection_id"], {}).get("match_status") == "matched" and bool(r.get("track_id")) for r in rows),
            "tracks_containing_multiple_evaluator_ids_posthoc": sum(len(v) > 1 for v in ids_by_track.values()),
            "tracks_containing_multiple_evaluator_categories_posthoc": sum(len(v) > 1 for v in categories_by_track.values()),
        }
        event_rows = [json.loads(line) for line in
                      (OUT / f"events_{mode}.jsonl").read_text().splitlines() if line]
        mode_reports[mode]["class_correction_events"] = sum(r["event"] == "class_correction" for r in event_rows)
        mode_reports[mode]["deconfirmation_events"] = sum(r["event"] == "deconfirmed" for r in event_rows)

    # Evaluator-only cases for review. These never feed the class predictor.
    focus_rows = []
    for mode in MODES:
        for row in all_rows_by_mode[mode]:
            gt = evaluator.get(row["detection_id"], {})
            if (row.get("raw_class") in {"bed", "dining_table", "sofa", "bench", "suitcase"}
                    or gt.get("gt_category") in {"table", "printer", "computer tower"}):
                focus_rows.append({
                    "mode": mode,
                    "detection_id": row["detection_id"],
                    "frame_index": row["frame_index"],
                    "raw_class_posthoc_review_only": row["raw_class"],
                    "initial_class": row["initial_class"],
                    "resolved_class": row["resolved_class"],
                    "class_status": row["class_status"],
                    "track_id": row["track_id"],
                    "gt_match_status_posthoc": gt.get("match_status"),
                    "gt_category_posthoc": gt.get("gt_category"),
                    "gt_semantic_id_posthoc": gt.get("gt_semantic_id"),
                    "physical_identity_review": "uncertain",
                })
    with (OUT / "u4_posthoc_case_review.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(focus_rows[0]) if focus_rows else [])
        writer.writeheader(); writer.writerows(focus_rows)

    metric_status = "available" if confirmed_categories else "unavailable_no_confirmed_category_mappings"
    result = {
        "schema_version": 1,
        "status": "u4_posthoc_review_complete_not_promoted",
        "u4_config_sha256": config_hash,
        "evaluator_observation_matches_sha256": matches_lock["sha256"],
        "category_mapping_snapshot_sha256": sha_file(mapping_path),
        "mapping_status_counts": dict(Counter(x.get("status", "unmapped") for x in mapping["categories"].values())),
        "confirmed_category_mapping_count": len(confirmed_categories),
        "confirmed_category_mappings": confirmed_categories,
        "class_aware_metrics": {
            "status": metric_status,
            "accuracy": None,
            "macro_f1": None,
            "wrong_class_corrected_count": None,
            "correct_class_changed_to_wrong_count": None,
            "reason": "No category-to-detector mappings have reviewer/date/decision provenance and confirmed status; provisional lexical mappings are excluded.",
        },
        "predictor_constraints": {
            "ground_truth_used_for_predictions": False,
            "room_labels_used_for_object_class": False,
            "u3_assignments_changed": False,
            "u3_thresholds_changed": False,
            "future_observations_used_for_past_class_state": False,
            "u3_holdout_frames_231_238_are_unseen": False,
        },
        "modes": mode_reports,
        "posthoc_case_review_csv": "u4_posthoc_case_review.csv",
        "physical_identity_note": "Evaluator IDs/categories are posthoc annotation lineage; physical identity remains uncertain for reviewed cross-class and table/suitcase cases.",
        "room_label_counterfactual": "not_run",
        "promotion": "not_promoted; do not use frames 231-238 as untouched U4 holdout",
    }
    (OUT / "u4_evaluation_report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
