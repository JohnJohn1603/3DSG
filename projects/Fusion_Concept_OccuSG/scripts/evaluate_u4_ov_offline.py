#!/usr/bin/env python3
"""Posthoc U4-OV evaluation; predictor artifacts are frozen before GT/review load."""
from __future__ import annotations
import csv, hashlib, json, sys
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/u4_ov"
U3 = ROOT / "artifacts/audits/u3_causal_prefix"
USER_VERSION = ROOT / "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1"
BASELINE_MAP = Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058/object_evaluations/eval_20261006T184217.537953Z_880c87/category_mapping_snapshot.json")
sys.path.insert(0, str(ROOT / "scripts"))
from run_u4_ov_offline import normalize, score  # noqa: E402
from u4_ov_evaluation_core import mapped_class_metrics, taxonomy_relation  # noqa: E402

def sha(p: Path) -> str: return hashlib.sha256(p.read_bytes()).hexdigest()

def metric(preds: list[str | None], truths: list[str]) -> dict:
    if len(preds) != len(truths): raise ValueError("prediction/truth count mismatch")
    covered = [(p, t) for p, t in zip(preds, truths) if p is not None]
    correct = sum(p == t for p, t in covered)
    wrong = len(covered) - correct
    return {"n": len(truths), "correct": correct, "incorrect": wrong,
            "abstention": len(truths)-len(covered), "coverage": len(covered)/len(truths) if truths else None,
            "accuracy_on_covered": correct/len(covered) if covered else None,
            "correct_over_all": correct/len(truths) if truths else None}

def plurality(values: list[str | None]) -> tuple[str | None, str]:
    usable = [v for v in values if v is not None]
    if not usable: return None, "unknown"
    counts = Counter(usable)
    ordered = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]: return None, "conflicted"
    return ordered[0][0], "classified"

def main():
    pred_report_path = OUT / "u4_ov_prediction_report.json"
    pred_report = json.loads(pred_report_path.read_text())
    if pred_report.get("status") != "u4_ov_offline_predictions_ready_unscored":
        raise RuntimeError("U4-OV prediction outputs are not frozen/unscored")
    lock = json.loads((OUT / "u4_ov_config.locked.json").read_text())
    if pred_report["locked_config_sha256"] != sha(OUT / "u4_ov_config.locked.json"):
        raise RuntimeError("U4-OV locked config changed after prediction")
    if pred_report["vocabulary_sha256"] != sha(OUT / "vocabulary_v1.json"):
        raise RuntimeError("U4-OV vocabulary changed after prediction")

    # Only now load user review/evaluator records.
    annotations = list(csv.DictReader((USER_VERSION / "observation_annotations.csv").open(encoding="utf-8")))
    ann_by_id = {r["observation_id"]: r for r in annotations}
    prediction_rows = [json.loads(line) for line in (OUT / "observations_predictions.jsonl").read_text().splitlines() if line]
    pred_by_id = {r["observation_id"]: r for r in prediction_rows}
    track_rows = [json.loads(line) for line in (OUT / "predicted_track_labels.jsonl").read_text().splitlines() if line]
    track_by_id = {r["u3_geometry_only_track_id"]: r for r in track_rows}
    causal_rows = [json.loads(line) for line in (OUT / "causal_track_predictions.jsonl").read_text().splitlines() if line]
    causal_by_id = {r["observation_id"]: r for r in causal_rows}
    if set(pred_by_id) != set(causal_by_id): raise RuntimeError("causal track outputs do not cover the frozen prediction observations")
    u4_vote_rows = {r["detection_id"]: r for r in
                    (json.loads(line) for line in (ROOT / "artifacts/audits/u4_semantic_fusion/observations_geometry_only.jsonl").read_text().splitlines() if line)}
    if not set(ann_by_id).issubset(pred_by_id): raise RuntimeError("review observation lacks U4-OV output")

    review_rows = []
    raw_preds, vote_preds, single_preds, causal_preds, track_preds, truths = [], [], [], [], [], []
    taxonomy_doc = json.loads((OUT / "taxonomy_hierarchy_proposal_v1.json").read_text())
    taxonomy_parent = {r["specific_label"]: r["general_label"] for r in taxonomy_doc["relations"]}
    taxonomy_confirmed_edges = {(r["specific_label"], r["general_label"]) for r in taxonomy_doc["relations"] if r["status"] == "confirmed"}
    for did, ann in sorted(ann_by_id.items(), key=lambda x: (int(x[1]["frame_index"]), x[0])):
        p = pred_by_id[did]; truth = ann["reviewed_category"]
        raw = p["raw_detector_class"]
        vote_row = p["u4_voting"]
        vote = vote_row["fused_semantic_label"] if vote_row["status"] == "confirmed" else None
        single = p["u4_ov_single_view"]["fused_semantic_label"]
        track_id = p["u3_geometry_only_track_id"]
        track_pred = track_by_id.get(track_id, {}).get("fused_semantic_label") if track_id else None
        causal_pred = causal_by_id[did]["causal_fused_semantic_label"]
        raw_preds.append(raw); vote_preds.append(vote); single_preds.append(single); causal_preds.append(causal_pred); track_preds.append(track_pred); truths.append(truth)
        relation_by_method = {"raw_yolo": taxonomy_relation(raw, truth, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_voting": taxonomy_relation(vote, truth, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_ov_single_view": taxonomy_relation(single, truth, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_ov_causal_track": taxonomy_relation(causal_pred, truth, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_ov_final_track": taxonomy_relation(track_pred, truth, taxonomy_parent, taxonomy_confirmed_edges)}
        review_rows.append({"frame_id": ann["frame_id"], "frame_index": ann["frame_index"],
            "detection_id": ann["detection_id"], "observation_id": did, "case_refs": ann["case_refs"],
            "reviewed_label_evaluator_only": truth,
            "strict_exact_match_scope": True,
            "taxonomy_relation_by_method": json.dumps(relation_by_method, sort_keys=True),
            "raw_detector_class": raw,
            "u4_voting_label": vote, "u4_voting_status": vote_row["status"],
            "u4_ov_single_label": single,
            "u4_ov_single_status": p["u4_ov_single_view"]["status"],
            "u4_ov_top1_candidate": p["u4_ov_single_view"]["top1_candidate"],
            "u4_ov_top1_cosine": p["u4_ov_single_view"]["top1_cosine"],
            "u4_ov_top2_candidate": p["u4_ov_single_view"]["top2_candidate"],
            "u4_ov_margin": p["u4_ov_single_view"]["cosine_margin"],
            "u4_ov_online_causal_track_label": causal_pred,
            "u4_ov_online_causal_status": causal_by_id[did]["causal_status"],
            "u4_ov_online_causal_evidence_end_frame_id": causal_by_id[did]["causal_evidence_end_frame_id"],
            "u4_ov_online_causal_support_bins": causal_by_id[did]["causal_distinct_view_bin_count"],
            "predicted_u3_track_id": track_id,
            "u4_ov_predicted_track_label": track_pred})
    with (OUT / "review_observation_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(review_rows[0])); w.writeheader(); w.writerows(review_rows)

    # Human-confirmed identity groups are diagnostic aggregation conditions only.
    case_doc = json.loads((USER_VERSION / "case_order_mapping.json").read_text())
    groups = [c for c in case_doc["cases"] if c["identity_relation_within_case"] == "same_object"]
    vocab = json.loads((OUT / "vocabulary_v1.json").read_text())
    labels = [c["label"] for c in vocab["categories"]]
    cache_npz = next((OUT / "text_cache").glob("text_features__*.npz"))
    with np.load(cache_npz, allow_pickle=False) as z:
        text_labels = z["labels"].astype(str).tolist()
        text_matrix = np.asarray(z["label_features"], dtype=np.float32)
    if labels != text_labels: raise RuntimeError("text cache vocabulary order mismatch")
    index = json.loads((U3 / "causal_input_index.json").read_text())
    feature_refs = {r["detection_id"]: r for r in index["records"]}
    feature_cache = {}
    for did in ann_by_id:
        p = ROOT / feature_refs[did]["feature_ref"]
        if sha(p) != feature_refs[did]["feature_sha256"]: raise RuntimeError(f"image feature hash mismatch: {did}")
        with np.load(p, allow_pickle=False) as z: feature_cache[did] = normalize(z["image_feature"])
    human_group_rows = []
    for case in groups:
        ids = case["review_order_observation_ids"]
        truth_labels = {ann_by_id[did]["reviewed_category"] for did in ids}
        if len(truth_labels) != 1: raise RuntimeError(f"human same-object group has inconsistent class labels: {case['case_id']}")
        # Select earliest member of each already-locked, sensor-derived view bin.
        bin_map = {}
        for did in sorted(ids, key=lambda x: (int(x.split(":",1)[0].rsplit("-",1)[-1]), x)):
            b = pred_by_id[did]["view_bin"]
            if b != "unknown" and b not in bin_map: bin_map[b] = did
        view_ids = list(bin_map.values())
        if len(view_ids) >= lock["classification"]["multi_view"]["minimum_distinct_known_view_bins"]:
            agg = normalize(np.mean([feature_cache[did] for did in view_ids], axis=0))
            multi = score(agg, labels, text_matrix, lock)
        else:
            multi = {"fused_semantic_label": None, "status": "unknown",
                     "top1_candidate": None, "top1_cosine": None, "top2_candidate": None,
                     "top2_cosine": None, "cosine_margin": None, "top5": []}
        raw_group, raw_status = plurality([pred_by_id[did]["raw_detector_class"] for did in ids])
        vote_group, vote_status = plurality([
            pred_by_id[did]["u4_voting"]["fused_semantic_label"]
            if pred_by_id[did]["u4_voting"]["status"] == "confirmed" else None for did in ids])
        single_group, single_status = plurality([
            pred_by_id[did]["u4_ov_single_view"]["fused_semantic_label"] for did in ids])
        all_track_ids = {pred_by_id[did]["u3_geometry_only_track_id"] for did in ids}
        track_group, track_status = plurality([track_by_id[t]["fused_semantic_label"] for t in all_track_ids if t in track_by_id])
        gt = next(iter(truth_labels))
        human_group_rows.append({"case_id": case["case_id"], "observation_ids": ids,
            "user_confirmed_class_evaluator_only": gt, "confirmed_identity_group_used_for_grouping": True,
            "predictor_is_end_to_end": False, "distinct_known_view_bins": len(view_ids),
            "selected_view_observation_ids": view_ids,
            "raw_yolo_group_plurality": raw_group, "raw_yolo_group_status": raw_status,
            "raw_yolo_group_taxonomy_relation": taxonomy_relation(raw_group, gt, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_voting_group_plurality": vote_group, "u4_voting_group_status": vote_status,
            "u4_voting_group_taxonomy_relation": taxonomy_relation(vote_group, gt, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_ov_single_view_group_plurality": single_group, "u4_ov_single_view_group_status": single_status,
            "u4_ov_single_view_group_taxonomy_relation": taxonomy_relation(single_group, gt, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_ov_multi_view_label": multi["fused_semantic_label"], "u4_ov_multi_view_status": multi["status"],
            "u4_ov_multi_view_taxonomy_relation": taxonomy_relation(multi["fused_semantic_label"], gt, taxonomy_parent, taxonomy_confirmed_edges),
            "u4_ov_multi_view_top1": multi["top1_candidate"], "u4_ov_multi_view_cosine": multi["top1_cosine"],
            "u4_ov_multi_view_margin": multi["cosine_margin"],
            "u3_predicted_track_id_set": sorted(str(x) for x in all_track_ids),
            "u4_ov_predicted_track_plurality": track_group, "u4_ov_predicted_track_status": track_status,
            "u4_ov_predicted_track_taxonomy_relation": taxonomy_relation(track_group, gt, taxonomy_parent, taxonomy_confirmed_edges)})
    with (OUT / "confirmed_identity_group_diagnostic.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(human_group_rows[0])); w.writeheader(); w.writerows(human_group_rows)

    group_metrics = {}
    # Strict exact-label result includes every reviewed same-object group.
    # Generic/specific compatibility is reported separately as taxonomy relation.
    category_eligible_groups = human_group_rows
    for method, key in (("raw_yolo", "raw_yolo_group_plurality"),
                         ("u4_voting_raw_labels", "u4_voting_group_plurality"),
                         ("u4_ov_single_view_plurality", "u4_ov_single_view_group_plurality"),
                         ("u4_ov_multi_view", "u4_ov_multi_view_label"),
                         ("u4_ov_predicted_u3_track", "u4_ov_predicted_track_plurality")):
        group_metrics[method] = metric([r[key] for r in category_eligible_groups], [r["user_confirmed_class_evaluator_only"] for r in category_eligible_groups])
        group_metrics[method]["evaluation_scope"] = "4 user-confirmed same-object groups (cases 2-5); strict exact-label diagnostic only; group identities supplied only for evaluation grouping"
        relation_key = {"raw_yolo":"raw_yolo_group_taxonomy_relation", "u4_voting_raw_labels":"u4_voting_group_taxonomy_relation",
            "u4_ov_single_view_plurality":"u4_ov_single_view_group_taxonomy_relation", "u4_ov_multi_view":"u4_ov_multi_view_taxonomy_relation",
            "u4_ov_predicted_u3_track":"u4_ov_predicted_track_taxonomy_relation"}[method]
        group_metrics[method]["specificity_relation_pending_approval_count"] = sum(
            r[relation_key] in ("prediction_is_specific_to_reviewed_general_label_pending_approval",
                                "prediction_is_general_to_reviewed_specific_label_pending_approval")
            for r in human_group_rows)
    for method, key in (("u4_voting_raw_labels", "u4_voting_group_plurality"),
                        ("u4_ov_single_view_plurality", "u4_ov_single_view_group_plurality"),
                        ("u4_ov_multi_view", "u4_ov_multi_view_label"),
                        ("u4_ov_predicted_u3_track", "u4_ov_predicted_track_plurality")):
        vals = [r[key] for r in category_eligible_groups]
        gold = [r["user_confirmed_class_evaluator_only"] for r in category_eligible_groups]
        group_metrics[method]["corrected_raw_error_count"] = sum(
            r["raw_yolo_group_plurality"] != r["user_confirmed_class_evaluator_only"] and r[key] == r["user_confirmed_class_evaluator_only"]
            for r in category_eligible_groups)
        group_metrics[method]["harmful_correction_count"] = sum(
            r["raw_yolo_group_plurality"] == r["user_confirmed_class_evaluator_only"] and r[key] is not None and r[key] != r["user_confirmed_class_evaluator_only"]
            for r in category_eligible_groups)
        group_metrics[method]["changed_from_raw_count"] = sum(
            r[key] is not None and r[key] != r["raw_yolo_group_plurality"] for r in category_eligible_groups)
        group_metrics[method]["raw_group_to_abstention_count"] = sum(
            r[key] is None and r["raw_yolo_group_plurality"] is not None for r in category_eligible_groups)

    # Separate evaluator for fused labels. No category mapping is self-confirmed.
    map_doc = json.loads((OUT / "fused_label_evaluator_mapping_v1.json").read_text())
    map_by_label = {r["fused_semantic_label"]: r for r in map_doc["mappings"]}
    matches_path = Path(json.loads((ROOT / "locks/external_resources.json").read_text())["files"]["dense_object_evaluation_observation_matches"]["path"])
    match_lock = json.loads((ROOT / "locks/external_resources.json").read_text())["files"]["dense_object_evaluation_observation_matches"]
    if sha(matches_path) != match_lock["sha256"]: raise RuntimeError("evaluator matches changed from lock")
    evaluator = {r["detection_id"]: r for r in (json.loads(line) for line in matches_path.read_text().splitlines() if line)}
    pred_mapping_status = Counter()
    gt_status = Counter()
    mapping_eligible_rows = []
    for row in prediction_rows:
        label = row["u4_ov_single_view"]["fused_semantic_label"]
        causal_label = causal_by_id[row["observation_id"]]["causal_fused_semantic_label"]
        if label is None:
            pred_mapping_status[row["u4_ov_single_view"]["status"]] += 1
        else:
            pred_mapping_status[map_by_label.get(label, {}).get("status", "unmapped")] += 1
        gt = evaluator.get(row["observation_id"], {})
        gt_status[str(gt.get("match_status", "missing_from_evaluator"))] += 1
        if gt.get("match_status") == "matched":
            gt_map_status = gt.get("class_mapping_status", "unmapped")
            gt_status[f"matched_gt_mapping_{gt_map_status}"] += 1
        mapping_eligible_rows.append({"observation_id": row["observation_id"],
            "raw_detector_class": row["raw_detector_class"],
            "u4_ov_fused_label": label,
            "u4_ov_online_causal_track_label": causal_label,
            "fused_mapping_status": map_by_label.get(label, {}).get("status", "not_applicable") if label else row["u4_ov_single_view"]["status"],
            "evaluator_match_status_posthoc": gt.get("match_status"),
            "evaluator_gt_category_posthoc": gt.get("gt_category"),
            "evaluator_gt_mapping_status_posthoc": gt.get("class_mapping_status")})
    with (OUT / "fused_label_evaluator_observations_posthoc.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(mapping_eligible_rows[0])); w.writeheader(); w.writerows(mapping_eligible_rows)
    fused_mapping_rows = map_doc["mappings"]
    ov_observation_eval_records = []
    causal_observation_eval_records = []
    final_track_records: dict[str, list[dict]] = defaultdict(list)
    for row in prediction_rows:
        did = row["observation_id"]
        gt = evaluator.get(did, {})
        ov_observation_eval_records.append({"match_status": gt.get("match_status"), "gt_category": gt.get("gt_category"),
            "predicted_label": row["u4_ov_single_view"]["fused_semantic_label"]})
        causal_observation_eval_records.append({"match_status": gt.get("match_status"), "gt_category": gt.get("gt_category"),
            "predicted_label": causal_by_id[did]["causal_fused_semantic_label"]})
        track_id = row.get("u3_geometry_only_track_id")
        if track_id:
            final_track_records[track_id].append({"match_status": gt.get("match_status"), "gt_category": gt.get("gt_category"),
                "gt_semantic_id": str(gt.get("gt_semantic_id")) if gt.get("gt_semantic_id") is not None else None})
    final_map_class_records = []
    final_map_identity_exclusions = Counter()
    track_output_by_id = {row["u3_geometry_only_track_id"]: row for row in track_rows}
    for track_id, gt_rows in sorted(final_track_records.items()):
        matched = [r for r in gt_rows if r["match_status"] == "matched" and r["gt_semantic_id"] is not None and r["gt_category"]]
        semantic_ids = {r["gt_semantic_id"] for r in matched}
        categories = {r["gt_category"] for r in matched}
        if len(semantic_ids) > 1:
            final_map_identity_exclusions["mixed_identity_false_merge_track"] += 1
            continue
        if len(semantic_ids) == 0:
            final_map_identity_exclusions["no_matched_gt_identity"] += 1
            continue
        if len(categories) != 1:
            final_map_identity_exclusions["inconsistent_gt_category_within_identity"] += 1
            continue
        final = track_output_by_id.get(track_id, {})
        final_map_class_records.append({"match_status": "matched", "gt_category": next(iter(categories)),
            "predicted_label": final.get("fused_semantic_label"), "u3_track_id": track_id,
            "gt_semantic_id": next(iter(semantic_ids)), "observation_count": len(gt_rows),
            "matched_observation_count": len(matched)})
    mapped_metrics = {
        "u4_ov_single_view_observations": mapped_class_metrics(ov_observation_eval_records, fused_mapping_rows),
        "u4_ov_online_causal_track_observations": mapped_class_metrics(causal_observation_eval_records, fused_mapping_rows),
        "u4_ov_final_map_tracks": mapped_class_metrics(final_map_class_records, fused_mapping_rows),
    }
    mapped_metrics["u4_ov_final_map_tracks"]["identity_exclusion_counts"] = dict(final_map_identity_exclusions)
    fused_confirmed = [r for r in fused_mapping_rows if r["status"] == "confirmed" and r.get("reviewer") and r.get("review_date") and r.get("decision")]
    fused_evaluator = {"status": "available" if any(r["denominator"] for r in mapped_metrics.values()) else "unavailable_no_eligible_confirmed_mapping_truths",
        "mapping_status_counts": dict(Counter(r["status"] for r in fused_mapping_rows)),
        "prediction_mapping_or_status_counts": dict(pred_mapping_status),
        "evaluator_gt_status_counts": dict(gt_status),
        "modes": mapped_metrics,
        "implementation": "metrics are computed from confirmed fused-label mappings; micro precision/recall/F1, exact accuracy, coverage, abstentions/unsupported, and per-class TP/FP/FN are implemented",
        "unsupported_unmapped_handling": "provisional/unmapped/unknown/conflicted outputs abstain; abstentions count as FN for eligible matched GT; unmatched/uncertain GT and GT categories outside confirmed mapping support are excluded and counted",
        "raw_yolo_evaluator_unchanged": True,
        "raw_mapping_reference_sha256": sha(OUT / "raw_detector_evaluator_mapping_reference.json"),
        "fused_mapping_file_sha256": sha(OUT / "fused_label_evaluator_mapping_v1.json")}

    # Identity-conditioned diagnostics on fixed review observations and full U3 tracks.
    identity_doc = json.loads((USER_VERSION / "identity_relations.json").read_text())
    pair_rel = {}
    for r in identity_doc["relations"]:
        pair = tuple(sorted((r["left_observation_id"],r["right_observation_id"])))
        if pair not in pair_rel or r["relation"] != "uncertain": pair_rel[pair]=r["relation"]
    fixed_assignments = {did: pred_by_id[did]["u3_geometry_only_track_id"] for did in ann_by_id}
    known_pairs = [(a,b,rel) for (a,b),rel in pair_rel.items() if rel != "uncertain"]
    pair_tp=sum(rel=="same_object" and fixed_assignments[a]==fixed_assignments[b] for a,b,rel in known_pairs)
    pair_fp=sum(rel=="different_objects" and fixed_assignments[a]==fixed_assignments[b] for a,b,rel in known_pairs)
    pair_fn=sum(rel=="same_object" and fixed_assignments[a]!=fixed_assignments[b] for a,b,rel in known_pairs)
    review_track_labels=defaultdict(set)
    for did,a in ann_by_id.items(): review_track_labels[fixed_assignments[did]].add(a["reviewed_category"])
    mixed_tracks=sorted(t for t,ls in review_track_labels.items() if len(ls)>1)
    evaluator_identity_counts=defaultdict(set); evaluator_category_counts=defaultdict(set)
    for row in prediction_rows:
        gt=evaluator.get(row["observation_id"],{})
        t=row["u3_geometry_only_track_id"]
        if t and gt.get("match_status")=="matched" and gt.get("gt_semantic_id") is not None:
            evaluator_identity_counts[t].add(str(gt["gt_semantic_id"]))
            if gt.get("gt_category"): evaluator_category_counts[t].add(gt["gt_category"])
    track_identity_report={r["u3_geometry_only_track_id"]:{
        "u4_ov_multi_view_label":r["fused_semantic_label"],"status":r["status"],
        "distinct_known_view_bins":r["distinct_known_view_bins"],"observation_count":r["observation_count"],
        "reviewed_user_labels_for_this_predicted_track":sorted(review_track_labels[r["u3_geometry_only_track_id"]]),
        "review_scope_identity_mixed":len(review_track_labels[r["u3_geometry_only_track_id"]])>1,
        "matched_evaluator_semantic_id_count_posthoc":len(evaluator_identity_counts[r["u3_geometry_only_track_id"]]),
        "matched_evaluator_category_count_posthoc":len(evaluator_category_counts[r["u3_geometry_only_track_id"]])}
        for r in track_rows}

    method_inputs = {
        "raw_yolo_exact_review_label": raw_preds,
        "u4_voting_raw_labels": vote_preds,
        "u4_ov_single_view": single_preds,
        "u4_ov_online_causal_track": causal_preds,
        "u4_ov_final_map_track_projected_to_observations": track_preds,
    }
    method_metrics = {name: metric(preds, truths) for name,preds in method_inputs.items()}
    for method, preds in (("u4_voting_raw_labels",vote_preds), ("u4_ov_single_view",single_preds),
                          ("u4_ov_online_causal_track",causal_preds),
                          ("u4_ov_final_map_track_projected_to_observations",track_preds)):
        correction=sum(raw_preds[i] != truths[i] and preds[i] == truths[i] for i in range(len(truths)) if preds[i] is not None)
        harmful=sum(raw_preds[i] == truths[i] and preds[i] is not None and preds[i] != truths[i] for i in range(len(truths)))
        method_metrics[method]["corrected_raw_error_count"] = correction
        method_metrics[method]["harmful_correction_count"] = harmful
        method_metrics[method]["changed_from_raw_count"] = sum(preds[i] is not None and preds[i] != raw_preds[i] for i in range(len(truths)))
        method_metrics[method]["raw_to_abstention_count"] = sum(raw_preds[i] is not None and preds[i] is None for i in range(len(truths)))

    # Count each reviewed predicted track once. Mixed-identity tracks are
    # reported as association failures and never assigned a majority truth.
    review_by_track: dict[str, list[int]] = defaultdict(list)
    for i, review_row in enumerate(review_rows):
        review_by_track[review_row["predicted_u3_track_id"]].append(i)
    review_track_rows = []
    method_track_preds: dict[str, list[str | None]] = defaultdict(list)
    track_truths: list[str] = []
    for track_id, indices in sorted(review_by_track.items()):
        labels_in_track = {truths[i] for i in indices}
        mixed = len(labels_in_track) != 1
        raw_value, _ = plurality([raw_preds[i] for i in indices])
        vote_value, _ = plurality([vote_preds[i] for i in indices])
        single_value, _ = plurality([single_preds[i] for i in indices])
        last_reviewed = max(indices, key=lambda i: (int(review_rows[i]["frame_index"]), review_rows[i]["observation_id"]))
        causal_value = causal_by_id[review_rows[last_reviewed]["observation_id"]]["causal_fused_semantic_label"]
        final_value = track_by_id.get(track_id, {}).get("fused_semantic_label")
        truth = next(iter(labels_in_track)) if not mixed else None
        review_track_rows.append({"predicted_track_id": track_id, "review_observation_count": len(indices),
            "reviewed_categories": sorted(labels_in_track), "identity_mixed_or_conflicted": mixed,
            "evaluator_truth": truth, "raw_yolo_track_plurality": raw_value,
            "u4_voting_track_plurality": vote_value, "u4_ov_single_view_track_plurality": single_value,
            "u4_ov_online_causal_track_at_last_reviewed_observation": causal_value,
            "u4_ov_final_map_track_label": final_value,
            "final_map_taxonomy_relation": taxonomy_relation(final_value, truth, taxonomy_parent, taxonomy_confirmed_edges)})
        if not mixed:
            track_truths.append(truth)
            method_track_preds["raw_yolo"].append(raw_value)
            method_track_preds["u4_voting"].append(vote_value)
            method_track_preds["u4_ov_single_view"].append(single_value)
            method_track_preds["u4_ov_online_causal_at_last_observation"].append(causal_value)
            method_track_preds["u4_ov_final_map"].append(final_value)
    with (OUT / "review_track_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w=csv.DictWriter(f, fieldnames=list(review_track_rows[0])); w.writeheader(); w.writerows(review_track_rows)
    track_level_metrics = {name: metric(values, track_truths) for name,values in method_track_preds.items()}
    for name, value in track_level_metrics.items():
        value["mixed_identity_tracks_excluded"] = len(mixed_tracks)
        value["evaluation_unit"] = "one reviewed predicted track, counted once; user review classes are evaluator-only"
    legacy_reference = {
        "source": "previous U4_OV_PROTOTYPE.vi.md strict-review table before this correction",
        "scope": "14 observations after case-4-specific exclusion; retained only as historical exact-match reference",
        "raw_yolo": {"correct":3,"incorrect":11,"abstention":0,"n":14},
        "u4_voting": {"correct":2,"incorrect":5,"abstention":7,"n":14},
        "u4_ov_single_view": {"correct":1,"incorrect":1,"abstention":12,"n":14},
        "u4_ov_final_track_projected_per_observation": {"correct":4,"incorrect":0,"abstention":10,"n":14},
        "interpretation": "The four final-track correct observations all belong to the same piano track u3-0046; they are one reviewed track-level positive, not four distinct corrected objects."
    }
    report={"schema_version":1,"status":"u4_ov_offline_prototype_scored_on_development_only",
        "development_cases_are_not_benchmark_or_holdout":True,
        "fresh_test_status":"not_available_no_unseen_multiframe_sequence_in_current_saved_caches",
        "fresh_test_note":"Only scene 00802 route cache exists for U2/U3; frames 0..278 were already reviewed/scored in prior turns. Frame 279 has one P3 observation, too small for a meaningful fresh test. It was not substituted or scored as a test.",
        "vocabulary_id":lock["vocabulary_id"],"vocabulary_sha256":lock["vocabulary_sha256"],
        "locked_config_sha256":sha(OUT/"u4_ov_config.locked.json"),"candidate_count":len(labels),
        "zero_shot_is_candidate_vocabulary_only":True,
        "observation_level_development_metrics":method_metrics,
        "same_object_group_metrics":group_metrics,
        "class_evaluation_protocol":"Current strict exact match includes all 18 user-reviewed observations and counts labels literally. Specificity relations are emitted separately; pending taxonomy relations are neither silently equated nor removed. Historical 14-observation results remain in legacy_exact_match_reference.",
        "legacy_exact_match_reference":legacy_reference,
        "online_causal_observation_metrics":method_metrics,
        "final_map_review_track_metrics":track_level_metrics,
        "final_map_review_track_rows":review_track_rows,
        "taxonomy_proposal_status":taxonomy_doc["status"],
        "taxonomy_relation_counts":dict(Counter(row["u4_ov_multi_view_taxonomy_relation"] for row in human_group_rows)),
        "same_object_group_scope_note":"Identity groups case2-case5 were confirmed by user and used only for diagnostic aggregation; this is not end-to-end prediction. Strict class scores include all four groups; general/specific relations remain pending until taxonomy approval.",
        "identity_diagnostics":{"confirmed_pair_TP":pair_tp,"confirmed_pair_FP":pair_fp,"confirmed_pair_FN":pair_fn,
            "confirmed_pair_count":len(known_pairs),"uncertain_pair_count":sum(r=="uncertain" for r in pair_rel.values()),
            "mixed_identity_predicted_tracks_in_review_scope":mixed_tracks,
            "false_merge_or_fragmentation_is_separate_from_classification":True,
            "predicted_track_posthoc_identity_detail":track_identity_report},
        "fused_label_evaluator":fused_evaluator,
        "raw_yolo_evaluator":{"status":"existing evaluator preserved; no rerun or mapping edits",
            "mapping_reference":"raw_detector_evaluator_mapping_reference.json",
            "confirmed_mapping_count":json.loads((OUT/"raw_detector_evaluator_mapping_reference.json").read_text())["confirmed_mapping_count_in_source"]},
        "limits":{"score_is_calibrated_probability":False,"arbitrary_open_vocabulary":False,
            "raw_detector_class_hard_gate":False,"gt_or_room_labels_used_for_prediction":False,
            "category_mappings_auto_confirmed":False,
            "class_metrics_on_fused_outputs_unavailable_without_confirmed_mapping":True},
        "outputs":["review_observation_comparison.csv","review_track_comparison.csv","confirmed_identity_group_diagnostic.csv",
            "fused_label_evaluator_observations_posthoc.csv","predicted_track_labels.jsonl","causal_track_predictions.jsonl"]}
    (OUT/"u4_ov_evaluation_report.json").write_text(json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n")
    print(json.dumps({"status":report["status"],"fresh_test_status":report["fresh_test_status"],
        "observation_level_metrics":method_metrics,"same_object_group_metrics":group_metrics,
        "identity_diagnostics":{k:v for k,v in report["identity_diagnostics"].items() if k!="predicted_track_posthoc_identity_detail"},
        "fused_label_evaluator":fused_evaluator},indent=2,sort_keys=True,ensure_ascii=False))

if __name__=="__main__": main()
