#!/usr/bin/env python3
"""Evaluate the one locked U4-OV margin-veto candidate from cached scores."""
from __future__ import annotations
import csv, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/object_graph_improvement/q4_semantic_baseline_v1_20261012"
sys.path.insert(0, str(ROOT / "scripts"))
from q4_semantic_evaluation import (  # noqa: E402
    correction_transitions, group_plurality, label_key, load_jsonl, metric, sha256,
)


def emit_cached_candidate(row: dict, label_field: str, status_field: str,
                          top1_field: str, cosine_field: str, margin_field: str,
                          min_cosine: float) -> str | None:
    status = row.get(status_field)
    if status == "classified":
        return row.get(label_field)
    if status != "conflicted":
        return None
    top1, cosine, margin = row.get(top1_field), row.get(cosine_field), row.get(margin_field)
    if top1 is None or cosine is None or margin is None:
        return None
    if float(cosine) >= min_cosine and float(margin) >= 0.0:
        return top1
    return None


def metrics_with_ids(preds: list[str | None], truths: list[str], raw: list[str], ids: list[str]) -> dict:
    result = metric(preds, truths)
    result["transitions_vs_raw"] = correction_transitions(raw, preds, truths)
    result["transition_observation_ids"] = {}
    for did, p, gt, r in zip(ids, preds, truths, raw):
        pn, gn, rn = label_key(p), label_key(gt), label_key(r)
        raw_correct = rn == gn
        if pn is None:
            kind = "correct_to_abstain" if raw_correct else "wrong_to_abstain"
        elif raw_correct and pn == gn:
            kind = "correct_retained"
        elif raw_correct:
            kind = "correct_to_wrong"
        elif pn == gn:
            kind = "wrong_to_correct"
        else:
            kind = "wrong_retained_wrong"
        result["transition_observation_ids"].setdefault(kind, []).append(did)
    return result


def main() -> int:
    contract_path = OUT / "q4_margin_candidate_contract.locked.json"
    contract = json.loads(contract_path.read_text())
    for rel, digest in contract["inputs"].items():
        path = ROOT / rel
        if not path.is_file() or sha256(path) != digest:
            raise RuntimeError(f"candidate input changed from lock: {rel}")
    baseline = json.loads((OUT / "q4_semantic_baseline_report.json").read_text())
    ann_root = ROOT / "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1"
    ann = {r["observation_id"]: r for r in csv.DictReader((ann_root / "observation_annotations.csv").open(encoding="utf-8"))}
    reviewed = {did: r for did, r in ann.items() if r.get("reviewed_category") and r.get("provenance") == "user-reviewed"}
    ids = sorted(reviewed, key=lambda did: (int(reviewed[did]["frame_index"]), did))
    truth = [label_key(reviewed[did]["reviewed_category"]) for did in ids]
    u4ov = {r["observation_id"]: r for r in load_jsonl(ROOT/"artifacts/audits/u4_ov/observations_predictions.jsonl")}
    causal = {r["observation_id"]: r for r in load_jsonl(ROOT/"artifacts/audits/u4_ov/causal_track_predictions.jsonl")}
    final = {r["u3_geometry_only_track_id"]: r for r in load_jsonl(ROOT/"artifacts/audits/u4_ov/predicted_track_labels.jsonl")}
    lock = json.loads((ROOT/"artifacts/audits/u4_ov/u4_ov_config.locked.json").read_text())
    floor = float(lock["classification"]["top1_unknown_if_max_cosine_below"])
    raw = [u4ov[did]["raw_detector_class"] for did in ids]
    single = [emit_cached_candidate(u4ov[did]["u4_ov_single_view"], "fused_semantic_label", "status",
        "top1_candidate", "top1_cosine", "cosine_margin", floor) for did in ids]
    causal_pred = [emit_cached_candidate(causal[did], "causal_fused_semantic_label", "causal_status",
        "top1_candidate", "top1_cosine", "cosine_margin", floor) for did in ids]

    observation_rows=[]
    for did, gt, raw_label, pred in zip(ids, truth, raw, single):
        row=u4ov[did]["u4_ov_single_view"]
        observation_rows.append({"observation_id":did,"reviewed_class_evaluator_only":gt,
            "raw_detector_class":raw_label,"baseline_label":row.get("fused_semantic_label"),
            "baseline_status":row.get("status"),"cached_top1":row.get("top1_candidate"),
            "cached_cosine":row.get("top1_cosine"),"cached_margin":row.get("cosine_margin"),
            "candidate_label":pred,"candidate_status":"classified" if pred is not None else row.get("status"),
            "candidate_uses_cached_scores_only":True})

    group_rows=[]
    for group in baseline["identity_conditioned_final_object_diagnostic"]["groups"]:
        obs_ids=group["observation_ids"]
        labels=[single[ids.index(did)] for did in obs_ids]
        group_rows.append({"identity_group_id_evaluator_only":group["identity_group_id_evaluator_only"],
            "observation_ids":obs_ids,"confirmed_class":group["confirmed_class"],
            "candidate_plurality":group_plurality(labels),"candidate_coverage_count":sum(x is not None for x in labels),
            "denominator_observations":len(labels),"identity_supplied_for_diagnostic_grouping":True})

    track_rows=baseline["predicted_track_final_map"]["geometry_only"]["tracks"]
    final_candidate_by_track={}
    for tid, row in final.items():
        final_candidate_by_track[tid]=emit_cached_candidate(row,"fused_semantic_label","status",
            "top1_candidate","top1_cosine","cosine_margin",floor)
    eligible=[r for r in track_rows if r.get("scored_as_single_class_track")]
    gt_tracks=[r["track_truth"] for r in eligible]
    pred_tracks=[final_candidate_by_track.get(r["track_id_evaluator_only"]) for r in eligible]
    all_reviewed_track_ids={r["track_id_evaluator_only"] for r in track_rows}
    report={"schema_version":1,"status":"q4_single_margin_candidate_scored_development_not_promoted",
        "contract_sha256":sha256(contract_path),"input_population":len(ids),
        "candidate":contract["candidate_id"],"baseline_observation_metrics":baseline["observation_causal_exact_metrics"]["u4_ov_single_view"],
        "candidate_observation_metrics":metrics_with_ids(single,truth,raw,ids),
        "baseline_causal_track_metrics":baseline["observation_causal_exact_metrics"]["u4_ov_causal_track_geometry_only"],
        "candidate_causal_track_metrics":metrics_with_ids(causal_pred,truth,raw,ids),
        "identity_conditioned_final_object":{
            "baseline":baseline["identity_conditioned_final_object_diagnostic"]["metrics"]["u4_ov_single_view_group"],
            "candidate":metric([r["candidate_plurality"] for r in group_rows],[r["confirmed_class"] for r in group_rows]),
            "candidate_identity_diagnostic_groups":group_rows,"identity_is_evaluator_only":True},
        "geometry_only_predicted_track_final_map":{
            "all_reviewed_tracks":len(all_reviewed_track_ids),
            "single_class_non_mixed_scored_tracks":len(eligible),
            "explicit_mixed_identity_tracks_retained":sum(bool(r.get("mixed_identity")) for r in track_rows),
            "baseline":baseline["predicted_track_final_map"]["geometry_only"]["single_class_track_metrics"]["u4_ov_final"],
            "candidate_single_class_tracks":metric(pred_tracks,gt_tracks),
            "candidate_label_coverage_over_all_tracks":sum(final_candidate_by_track.get(t) is not None for t in all_reviewed_track_ids)/len(all_reviewed_track_ids) if all_reviewed_track_ids else None},
        "selection_rule":contract["selection_rule"],"selected":False,
        "decision":"pending exact metrics"}
    base=report["baseline_observation_metrics"]
    cand=report["candidate_observation_metrics"]
    report["selected"]=(cand["correct_over_total"]>base["correct_over_total"] and cand["transitions_vs_raw"]["correct_to_wrong"]==0)
    report["decision"]="retain for further development only" if report["selected"] else "reject; criterion not met"
    report["input_files"]={"observations_csv":"q4_margin_candidate_observations.csv","identity_groups_csv":"q4_margin_candidate_identity_groups.csv"}
    for name, rows in (("q4_margin_candidate_observations.csv",observation_rows),("q4_margin_candidate_identity_groups.csv",group_rows)):
        with (OUT/name).open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader()
            for row in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()})
    (OUT/"q4_margin_candidate_report.json").write_text(json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n")
    print(json.dumps({k:report[k] for k in ["candidate","baseline_observation_metrics","candidate_observation_metrics","identity_conditioned_final_object","geometry_only_predicted_track_final_map","decision"]},indent=2,ensure_ascii=False))
    return 0

if __name__=="__main__": raise SystemExit(main())
