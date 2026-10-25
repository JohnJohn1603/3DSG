#!/usr/bin/env python3
"""Offline U6 quality scoring over frozen detections and U3/U4-OV outputs.

Semantic IDs are loaded only here, after all predictor artifacts are frozen.
This evaluates HM3D evaluator-instance IDs; it does not claim perfect physical
identity. Class scores are literal-label diagnostics because no evaluator
category mapping has been confirmed for this scene.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_hm3d_object_gt as gt_eval  # noqa: E402
import u6_scoring  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canon(label):
    return "_".join(str(label or "").strip().lower().split())


def metric(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p is not None and r is not None and p + r else (0.0 if p == 0 and r == 0 else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1,
            "status": "available" if tp + fp + fn else "unavailable"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--semantic-dir", type=Path, required=True)
    ap.add_argument("--descriptor", type=Path, required=True)
    ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("--u4ov-dir", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    run = args.run_dir.resolve(); semdir = args.semantic_dir.resolve(); out = args.output_dir.resolve()
    if out.exists(): raise FileExistsError(f"refusing to overwrite {out}")
    out.mkdir(parents=True)

    cap = read_json(run / "capture/capture_manifest.json")
    d2_doc = read_json(run / "detection_2d/detection_2d_index.json")
    p3_rows = {r["detection_id"]: r for r in read_jsonl(run / "detection_3d/object_evidence_ledger.jsonl")}
    descriptors = gt_eval.parse_descriptor(args.descriptor.resolve())
    mapping = read_json(args.mapping.resolve())
    predictions, _ = gt_eval._read_detector(run)
    route_frames = cap["frames"]

    # Build a single, class-agnostic, per-frame Hungarian GT-to-detection map.
    gt_by_det = {}; gt_obs = []; det_rows = []
    frame_gt = {}
    for rec in route_frames:
        fid = rec["frame_id"]
        gt_path = semdir / "frames" / f"{fid}__semantic_id_map.npy"
        instances, labels, statuses = gt_eval._read_gt_frame(gt_path, descriptors, mapping)
        frame_gt[fid] = instances
        dets = predictions.get(fid, [])
        matrix = np.zeros((len(dets), len(instances)), dtype=np.float64)
        for i, det in enumerate(dets):
            for j, inst in enumerate(instances): matrix[i, j] = gt_eval._mask_iou(det["mask"], inst["mask"])
        matches = gt_eval._assign(matrix, 0.50)
        match_by_det = {i: (instances[j], iou) for i, j, iou in matches}
        for i, det in enumerate(dets):
            d_id = det["detection_id"]; match = match_by_det.get(i)
            p3 = p3_rows.get(d_id, {})
            row = {"detection_id": d_id, "frame_id": fid, "frame_index": rec["frame_index"],
                   "raw_detector_class": det.get("class_canonical"), "detector_score": det.get("score_raw"),
                   "p3_decision": p3.get("decision", "missing"), "p3_reason": p3.get("reason"),
                   "gt_matched": match is not None,
                   "gt_semantic_id": match[0]["semantic_id"] if match else None,
                   "gt_category": match[0]["category"] if match else None,
                   "mask_iou": match[1] if match else None,
                   "gt_area_px": match[0]["area"] if match else None}
            det_rows.append(row)
            if match: gt_by_det[d_id] = {**row, "gt_instance": match[0]}
        # Every eligible GT observation enters the fixed primary denominator,
        # whether or not D2/P3 found it.
        for inst in instances:
            matched_detection = next((d["detection_id"] for d in dets
                                      if d["detection_id"] in gt_by_det and
                                      gt_by_det[d["detection_id"]]["gt_semantic_id"] == inst["semantic_id"]), None)
            gt_obs.append({"frame_id": fid, "frame_index": rec["frame_index"],
                           "gt_semantic_id": inst["semantic_id"], "gt_category": inst["category"],
                           "visible_pixels": inst["area"], "mapping_status": inst["mapping_status"],
                           "matched_detection_id": matched_detection})

    assignments_by_mode = {}
    for mode in ("source_raw", "source_dedup", "geometry_only"):
        assignments_by_mode[mode] = {r["detection_id"]: r for r in read_jsonl(run / "association_u3" / f"assignments_{mode}.jsonl")}

    # Pairwise association only over cross-frame pairs where both observations
    # are matched to eligible evaluator instances. Detection/GT coverage remains
    # reported against the full, predictor-independent GT observation set.
    association = {}
    for mode, assign in assignments_by_mode.items():
        matched = []
        for d_id, row in gt_by_det.items():
            a = assign.get(d_id)
            if a and row["p3_decision"] == "accepted":
                matched.append({"observation_id": d_id, "frame_id": row["frame_id"],
                                "frame_index": row["frame_index"], "gt_identity": str(row["gt_semantic_id"]),
                                "track_id": a.get("track_id"), "association_status": a.get("decision")})
        track_to_gt = defaultdict(set); gt_to_tracks = defaultdict(set)
        track_to_obs = defaultdict(list); gt_to_obs = defaultdict(list)
        for row in matched:
            track = row["track_id"]
            if track is None: continue
            track_to_gt[track].add(row["gt_identity"])
            gt_to_tracks[row["gt_identity"]].add(track)
            track_to_obs[track].append(row["observation_id"])
            gt_to_obs[row["gt_identity"]].append({"detection_id": row["observation_id"], "frame_id": row["frame_id"], "track_id": track})
        pairwise_main = u6_scoring.pairwise_association(matched)
        pairwise_no_revisit = u6_scoring.pairwise_association(matched, excluded_frame_indices={4})
        structure_main = u6_scoring.identity_structure(matched)
        structure_no_revisit = u6_scoring.identity_structure(matched, excluded_frame_indices={4})
        assignment_rows = read_jsonl(run / "association_u3" / f"assignments_{mode}.jsonl")
        same_frame_track_groups = Counter((r["frame_id"], r.get("track_id")) for r in assignment_rows if r.get("track_id") is not None)
        collision_details = []
        for (fid, tid), count in same_frame_track_groups.items():
            if count > 1:
                collision_details.append({"frame_id": fid, "track_id": tid, "observation_ids": sorted(r["detection_id"] for r in assignment_rows if r.get("frame_id") == fid and r.get("track_id") == tid)})
        association[mode] = {
            "pairwise_scope": "unordered cross-frame pairs among P3-accepted detections matched to eligible HM3D evaluator instances; GT-matched observations with deferred/unmatched association remain and create FN for same-GT pairs; unmatched detections without GT masks are counted separately",
            "pairwise": pairwise_main,
            "exact_pose_revisit_sensitivity_scoring_only": {"excluded_frame_indices": [4], "predictor_history_unchanged": True,
                "pairwise": pairwise_no_revisit, "identity_structure": structure_no_revisit},
            "matched_p3_observations": len(matched),
            "distinct_evaluator_instances_observed": structure_main["distinct_evaluator_instances"],
            "false_merge_track_count": structure_main["false_merge_track_count_by_evaluator_id"],
            "false_merge_tracks": structure_main["false_merge_tracks_by_evaluator_id"],
            "false_merge_observations": {t: sorted(obs for r in matched if r["track_id"] == t for obs in [r["observation_id"]])
                                         for t in structure_main["false_merge_tracks_by_evaluator_id"]},
            "fragmented_evaluator_instance_count": structure_main["fragmented_instance_count_by_evaluator_id"],
            "fragmented_instances": structure_main["fragmented_instances_by_evaluator_id"],
            "fragmented_observations": {str(i): [{"detection_id": r["observation_id"], "frame_id": r["frame_id"], "track_id": r["track_id"]}
                                                   for r in matched if r["gt_identity"] == str(i)]
                                        for i in structure_main["fragmented_instances_by_evaluator_id"]},
            "unmatched_gt_observations_in_primary_population": sum(not r["matched_detection_id"] for r in gt_obs),
            "unmatched_predictions": sum(not r["gt_matched"] for r in det_rows),
            "same_frame_track_collisions": sum(n - 1 for n in same_frame_track_groups.values() if n > 1),
            "same_frame_collision_details": [{**d,
                "evaluator_instance_ids_for_matched_observations": sorted({str(gt_by_det[did]["gt_semantic_id"]) for did in d["observation_ids"] if did in gt_by_det}),
                "physical_merge_status": "unreviewed; do not count as false merge"} for d in collision_details],
            "identity_caveat": "HM3D semantic instance IDs are evaluator IDs, not independently human-confirmed physical identity; uncertain/split/merged annotation behavior is not corrected here."}

    assignment_rows_by_mode = {m: read_jsonl(run / "association_u3" / f"assignments_{m}.jsonl")
                               for m in ("source_raw", "source_dedup", "geometry_only")}
    lookup = {m: {r["detection_id"]: r for r in rows} for m, rows in assignment_rows_by_mode.items()}
    all_detection_ids = sorted(set.intersection(*(set(x) for x in lookup.values())))
    association_disagreements = []
    for da, db in itertools.combinations(all_detection_ids, 2):
        ra = gt_by_det.get(da); rb = gt_by_det.get(db)
        if not ra or not rb or ra["frame_index"] == rb["frame_index"]: continue
        linked = {m: lookup[m][da].get("track_id") == lookup[m][db].get("track_id") for m in lookup}
        if len(set(linked.values())) > 1:
            association_disagreements.append({"observation_ids": [da, db],
                "gt_evaluator_instance_ids": [ra["gt_semantic_id"], rb["gt_semantic_id"]],
                "same_track_by_mode": linked, "in_primary_gt_population": True,
                "scored_in_primary_pairwise_association": bool(ra["p3_decision"] == "accepted" and rb["p3_decision"] == "accepted"),
                "diagnostic_is_additional_scoring_pair": False,
                "physical_identity_review_status": "unreviewed"})

    # Label scoring. Exact token agreement is a diagnostic only. The locked
    # category mapping has no confirmed rows, so mapped-class metrics are N/A.
    ov_single = {r["observation_id"]: r for r in read_jsonl(args.u4ov_dir / "single_view_predictions.jsonl")}
    causal = defaultdict(dict)
    for row in read_jsonl(args.u4ov_dir / "causal_predictions_by_mode.jsonl"):
        causal[row["mode"]][row["observation_id"]] = row
    final_rows = read_jsonl(args.u4ov_dir / "final_track_predictions_by_mode.jsonl")
    confirmed_mapping_count = sum(1 for x in mapping.get("categories", {}).values() if x.get("status") == "confirmed")
    class_report = {"confirmed_evaluator_category_mappings": confirmed_mapping_count,
                    "mapped_class_metrics": {"status": "unavailable", "reason": "no user-confirmed category mappings in locked mapping file"},
                    "label_normalization": "lowercase, trim, spaces to underscores only; no synonym, parent-child, or inferred mapping",
                    "raw_detector_exact_label_diagnostic": {}, "u4_ov": {}}
    matched_rows = list(gt_by_det.values())
    raw_correct = sum(canon(r["raw_detector_class"]) == canon(r["gt_category"]) for r in matched_rows)
    class_report["raw_detector_exact_label_diagnostic"] = {
        "correct": raw_correct, "incorrect": len(matched_rows) - raw_correct,
        "n_matched_p3_observations": len(matched_rows), "accuracy": raw_correct / len(matched_rows) if matched_rows else None,
        "scope": "literal normalized string equality on P3 accepted mask-matched observations; diagnostic only, not mapping-confirmed class metric"}
    for mode in assignments_by_mode:
        online_rows = [r for r in causal[mode].values() if r["observation_id"] in gt_by_det]
        online_covered = [r for r in online_rows if r.get("causal_classification", {}).get("fused_semantic_label")]
        online_correct = sum(canon(r["causal_classification"].get("fused_semantic_label")) == canon(gt_by_det[r["observation_id"]]["gt_category"]) for r in online_covered)
        mode_tracks = [r for r in final_rows if r["mode"] == mode]
        track_labeled = [r for r in mode_tracks if r.get("final_map_classification", {}).get("fused_semantic_label")]
        track_to_gt = defaultdict(set)
        for d_id, g in gt_by_det.items():
            a = assignments_by_mode[mode].get(d_id)
            if a and a.get("track_id") is not None: track_to_gt[a["track_id"]].add(g["gt_semantic_id"])
        track_correct = 0; clean_labeled = 0; mixed_labeled = 0
        for tr in track_labeled:
            ids = track_to_gt.get(tr["track_id"], set())
            if len(ids) > 1: mixed_labeled += 1
            elif len(ids) == 1:
                clean_labeled += 1
                gt_categories = {r["gt_category"] for d_id, r in gt_by_det.items()
                                 if assignments_by_mode[mode].get(d_id, {}).get("track_id") == tr["track_id"]}
                if len(gt_categories) == 1 and canon(tr["final_map_classification"].get("fused_semantic_label")) == canon(next(iter(gt_categories))): track_correct += 1
        class_report["u4_ov"][mode] = {
            "single_view": {"status_counts": dict(Counter(ov_single.get(r["detection_id"], {}).get("u4_ov_single_view", {}).get("status", "missing") for r in det_rows)),
                            "classified_observations": sum(bool(ov_single.get(r["detection_id"], {}).get("u4_ov_single_view", {}).get("fused_semantic_label")) for r in det_rows),
                            "abstained_observations": sum(not bool(ov_single.get(r["detection_id"], {}).get("u4_ov_single_view", {}).get("fused_semantic_label")) for r in det_rows),
                            "scope": "single-view U4-OV outputs; identical across association modes"},
            "causal": {"correct_exact_token": online_correct, "incorrect_exact_token": len(online_covered)-online_correct,
                       "covered": len(online_covered), "abstained": len(online_rows)-len(online_covered),
                       "eligible_matched_p3_observations": len(online_rows),
                       "coverage": len(online_covered)/len(online_rows) if online_rows else None,
                       "status_counts": dict(Counter(r.get("causal_classification", {}).get("status", "missing") for r in online_rows)),
                       "future_observation_flags": sorted({str(r.get("future_observations_used")) for r in online_rows}),
                       "exact_token_scores_diagnostic_only": True},
            "final_map": {"predicted_tracks": len(mode_tracks), "labeled_tracks": len(track_labeled),
                          "coverage": len(track_labeled)/len(mode_tracks) if mode_tracks else None,
                          "clean_labeled_tracks": clean_labeled, "correct_exact_token_clean_tracks": track_correct,
                          "mixed_identity_labeled_tracks_reported_separately": mixed_labeled,
                          "status_counts": dict(Counter(r.get("final_map_classification", {}).get("status", "missing") for r in mode_tracks)),
                          "exact_token_scores_diagnostic_only": True}}

    vocab_labels = set()
    vocab_path = ROOT / "artifacts/audits/u4_ov/vocabulary_v1.json"
    if vocab_path.is_file():
        vd = read_json(vocab_path)
        vocab_labels = {canon(x.get("label") if isinstance(x, dict) else x)
                        for x in vd.get("categories", [])}
    gt_categories = Counter(r["gt_category"] for r in gt_obs)
    supported_cats = {cat for cat in gt_categories if canon(cat) in vocab_labels}
    supported_gt_obs = [r for r in gt_obs if r["gt_category"] in supported_cats]
    supported_matched = [r for r in matched_rows if r["gt_category"] in supported_cats]
    supported_raw_correct = sum(canon(r["raw_detector_class"]) == canon(r["gt_category"]) for r in supported_matched)
    class_report["fixed_candidate_vocabulary_support"] = {
        "vocabulary_file_sha256": sha256(vocab_path) if vocab_path.is_file() else None,
        "all_eligible_gt_observations": len(gt_obs),
        "exact_literal_categories_present_in_locked_vocabulary": sorted(supported_cats),
        "supported_gt_observation_count": sum(n for c, n in gt_categories.items() if c in supported_cats),
        "unsupported_gt_observation_count": sum(n for c, n in gt_categories.items() if c not in supported_cats),
        "supported_gt_observation_detection_coverage": len(supported_matched)/len(supported_gt_obs) if supported_gt_obs else None,
        "raw_exact_token_correct_on_supported_matched": supported_raw_correct,
        "raw_exact_token_accuracy_on_supported_matched": supported_raw_correct/len(supported_matched) if supported_matched else None,
        "supported_matched_observations": len(supported_matched),
        "supported_matched_observations_u4ov_abstained": sum(not bool(ov_single.get(r["detection_id"], {}).get("u4_ov_single_view", {}).get("fused_semantic_label")) for r in supported_matched),
        "supported_matched_observations_u4ov_labeled": sum(bool(ov_single.get(r["detection_id"], {}).get("u4_ov_single_view", {}).get("fused_semantic_label")) for r in supported_matched),
        "note": "Vocabulary presence is candidate support, not a confirmed evaluator mapping or proof of recognition."}

    # Primary population and diagnostics are separate: the primary GT population
    # is all eligible GT object observations; diagnostic cases are unmatched and
    # all non-confirmed U4-OV outputs, listed independently.
    diagnostics = [{"detection_id": r["detection_id"], "frame_id": r["frame_id"],
                    "raw_detector_class": r["raw_detector_class"], "gt_semantic_id": r["gt_semantic_id"],
                    "gt_category": r["gt_category"], "p3_decision": r["p3_decision"],
                    "match_status": "matched" if r["gt_matched"] else "unmatched",
                    "u4_ov_status": ov_single.get(r["detection_id"], {}).get("u4_ov_single_view", {}).get("status"),
                    **u6_scoring.primary_diagnostic_relation(r)}
                   for r in det_rows if not r["gt_matched"] or ov_single.get(r["detection_id"], {}).get("u4_ov_single_view", {}).get("status") != "confirmed"]

    result = {
        "status": "scored_offline_development",
        "scene_id": cap["scene_id"], "frame_count": len(route_frames), "resolution_px": cap["resolution_px"],
        "protocol": {"route_lock": "locks/u6_hm3d_00808_quality_v1_1.json", "mask_iou_threshold": 0.50,
                     "minimum_gt_visible_pixels": 32, "hungarian_one_to_one_per_frame": True,
                     "primary_population": "all eligible non-architectural HM3D evaluator object observations in all captured frames, independent of predictions",
                     "diagnostic_population": "unmatched detections and all abstained/conflicted/unknown U4-OV predictions, reported separately",
                     "cross_frame_pairs_only": True, "exact_pose_revisit_counts_as_independent_view": False,
                     "semantic_id_physical_identity_caveat": True},
        "population": {"eligible_gt_observations": len(gt_obs), "eligible_gt_observations_by_frame": dict(Counter(r["frame_id"] for r in gt_obs)),
                       "eligible_evaluator_instances": len({r["gt_semantic_id"] for r in gt_obs}),
                       "matched_p3_accepted_observations": len(gt_by_det),
                       "matched_p3_accepted_observations_by_frame": dict(Counter(r["frame_id"] for r in gt_by_det.values())),
                       "unmatched_or_missed_gt_observations": sum(not r["matched_detection_id"] for r in gt_obs),
                       "unmatched_detections": sum(not r["gt_matched"] for r in det_rows),
                       "p3_accepts": sum(r.get("decision") == "accepted" for r in p3_rows.values()),
                       "p3_rejects": sum(r.get("decision") == "rejected" for r in p3_rows.values()),
                       "gt_category_observations": dict(gt_categories)},
        "association": association, "class": class_report,
        "diagnostic_cases": {"count": len(diagnostics), "rows": diagnostics,
                             "association_pair_disagreement_count": len(association_disagreements),
                             "association_pair_disagreements": association_disagreements,
                             "diagnostics_add_no_observations_or_pairs_to_metrics": True,
                             "some_diagnostic_rows_are_already_in_primary_automatic_population": True},
        "provenance": {"capture_manifest_sha256": sha256(run / "capture/capture_manifest.json"),
                       "d2_index_sha256": sha256(run / "detection_2d/detection_2d_index.json"),
                       "p3_jsonl_sha256": sha256(run / "detection_3d/detection_3d.jsonl"),
                       "u3_report_sha256": sha256(run / "association_u3/report.json"),
                       "u4ov_prediction_report_sha256": sha256(args.u4ov_dir / "prediction_report.json"),
                       "semantic_render_validation_sha256": sha256(semdir / "render_validation.json"),
                       "descriptor_sha256": sha256(args.descriptor.resolve()),
                       "mapping_sha256": sha256(args.mapping.resolve()),
                       "predictor_used_gt": False, "predictor_used_room_labels": False},
        "limitations": ["One 5-frame development route, not a scene-wide generalization claim.",
                        "HM3D semantic IDs/categories are evaluator annotations and may not perfectly equal physical identity.",
                        "No user-confirmed category mappings; mapped class accuracy is unavailable.",
                        "Offline association scoring does not replace native graph integration replay.",
                        "Frame 4 is an exact-pose revisit and is not independent-view evidence."]}
    (out / "metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (out / "primary_gt_observations.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in gt_obs), encoding="utf-8")
    (out / "detection_gt_matches.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in det_rows), encoding="utf-8")
    (out / "diagnostic_cases.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in diagnostics), encoding="utf-8")
    (out / "diagnostic_association_pair_disagreements.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in association_disagreements), encoding="utf-8")
    print(json.dumps({"status": result["status"], "primary_gt_observations": len(gt_obs),
                      "eligible_evaluator_instances": result["population"]["eligible_evaluator_instances"],
                      "matched_p3_observations": len(gt_by_det), "association": {k:v["pairwise"] for k,v in association.items()},
                      "class": class_report}, indent=2, ensure_ascii=False))


if __name__ == "__main__": main()
