#!/usr/bin/env python3
"""Score fixed human-reviewed U6 diagnostic observations only.

Reads locked predictions/assignments and evaluator-only user annotations. It
never modifies predictor inputs, assignments, or the official Hungarian mask
matching population.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import u6_scoring  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_label(label: str | None) -> str | None:
    if label is None or not str(label).strip():
        return None
    return "_".join(str(label).strip().casefold().split())


def score_identity_track_structure(annotations: list[dict], assignments: dict[str, dict]) -> dict:
    tracks_by_identity: dict[str, set[str]] = defaultdict(set)
    identities_by_track: dict[str, set[str]] = defaultdict(set)
    for ann in annotations:
        assignment = assignments.get(ann["observation_id"], {})
        track = assignment.get("track_id")
        if ann["identity_status"] != "confirmed" or track is None:
            continue
        identity = ann["identity_token"]
        tracks_by_identity[identity].add(track)
        identities_by_track[track].add(identity)
    by_identity = {key: sorted(values) for key, values in sorted(tracks_by_identity.items())}
    fragmented = {key: values for key, values in by_identity.items() if len(values) > 1}
    false_merges = {key: sorted(values) for key, values in sorted(identities_by_track.items()) if len(values) > 1}
    return {
        "unique_confirmed_physical_objects": len(tracks_by_identity),
        "predicted_track_ids_by_physical_identity": by_identity,
        "fragmented_physical_identities": fragmented,
        "fragmented_identity_count": len(fragmented),
        "excess_track_count_across_physical_identities": sum(max(0, len(v) - 1) for v in by_identity.values()),
        "false_merge_tracks_across_confirmed_physical_identities": false_merges,
        "false_merge_track_count": len(false_merges),
    }


def score_same_frame_identity(annotations: list[dict], assignments: dict[str, dict]) -> dict:
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for ann in annotations:
        if ann["identity_status"] == "confirmed":
            groups[(int(ann["frame_index"]), ann["identity_token"])].append(ann)
    rows = []
    for (frame_index, identity), obs in sorted(groups.items()):
        if len(obs) < 2:
            continue
        tracks = [assignments.get(x["observation_id"], {}).get("track_id") for x in obs]
        unique_tracks = sorted({x for x in tracks if x is not None})
        rows.append({"frame_index": frame_index, "physical_identity": identity,
                     "observation_ids": sorted(x["observation_id"] for x in obs),
                     "same_frame_identity_pair_count": len(obs) * (len(obs) - 1) // 2,
                     "track_ids": tracks, "unique_track_ids": unique_tracks,
                     "identity_consistent_in_one_track": len(unique_tracks) == 1 and all(x is not None for x in tracks),
                     "excess_tracks_for_identity_in_frame": max(0, len(unique_tracks) - 1)})
    pair_count = sum(x["same_frame_identity_pair_count"] for x in rows)
    consistent = sum(x["same_frame_identity_pair_count"] for x in rows if x["identity_consistent_in_one_track"])
    return {"scope": "same-frame pairs only; excluded from cross-frame pairwise denominator",
            "same_frame_same_identity_pair_count": pair_count,
            "same_track_consistent_pair_count": consistent,
            "split_across_tracks_pair_count": pair_count - consistent,
            "identity_groups": rows,
            "excess_same_frame_tracks": sum(x["excess_tracks_for_identity_in_frame"] for x in rows)}


def class_diagnostics(annotations: list[dict], mode: str, raw_rows: dict[str, dict],
                      causal_rows: dict[str, dict], final_rows: dict[str, dict], crop_hashes: dict[str, str],
                      assignments: dict[str, dict]) -> tuple[dict, list[dict]]:
    observations = []
    raw_correct = raw_available = 0
    top1_correct = top1_available = 0
    causal_committed_correct = causal_committed = 0
    causal_top1_correct = causal_top1_available = 0
    for ann in annotations:
        oid = ann["observation_id"]
        raw = raw_rows[oid]
        human = ann["normalized_reviewed_label"]
        raw_label = raw.get("raw_detector_class")
        single = raw.get("u4_ov_single_view", {})
        causal = causal_rows[oid].get("causal_classification", {})
        final = final_rows.get(assignments[oid].get("track_id"), {}).get("final_map_classification", {})
        if exact_label(raw_label) is not None:
            raw_available += 1
            raw_correct += exact_label(raw_label) == exact_label(human)
        candidate = single.get("top1_candidate")
        if candidate:
            top1_available += 1
            top1_correct += exact_label(candidate) == exact_label(human)
        causal_label = causal.get("fused_semantic_label")
        if causal_label:
            causal_committed += 1
            causal_committed_correct += exact_label(causal_label) == exact_label(human)
        causal_candidate = causal.get("top1_candidate")
        if causal_candidate:
            causal_top1_available += 1
            causal_top1_correct += exact_label(causal_candidate) == exact_label(human)
        final_label = final.get("fused_semantic_label")
        observations.append({
            "observation_id": oid,
            "crop_sha256": crop_hashes.get(oid),
            "human_class": ann["reviewed_label_raw"],
            "raw_detector_class": raw_label,
            "raw_exact_correct": exact_label(raw_label) == exact_label(human),
            "u4ov_single_top1_candidate": candidate,
            "u4ov_single_top1_exact_correct_diagnostic": exact_label(candidate) == exact_label(human) if candidate else None,
            "u4ov_single_status": single.get("status"), "u4ov_single_cosine_margin": single.get("cosine_margin"),
            "causal_fused_label": causal_label, "causal_status": causal.get("status"),
            "causal_top1_candidate_diagnostic": causal_candidate,
            "causal_selected_view_bins": causal_rows[oid].get("selected_view_bins_so_far", []),
            "causal_top1_exact_correct_diagnostic": exact_label(causal_candidate) == exact_label(human) if causal_candidate else None,
            "final_track_id": assignments[oid].get("track_id"), "final_fused_label": final_label,
            "final_status": final.get("status"), "final_top1_candidate_diagnostic": final.get("top1_candidate"),
            "final_top1_exact_correct_diagnostic": exact_label(final.get("top1_candidate")) == exact_label(human) if final.get("top1_candidate") else None,
            "exact_label_protocol": "casefold, trim, spaces/underscores canonicalized only; no synonym or parent-child mapping",
        })
    track_ids = sorted({assignments[a["observation_id"]].get("track_id") for a in annotations})
    track_rows = []
    committed_track_count = committed_track_correct = track_top1_available = track_top1_correct = 0
    for track in track_ids:
        if track is None:
            continue
        pred = final_rows.get(track, {}).get("final_map_classification", {})
        members = [a for a in annotations if assignments[a["observation_id"]].get("track_id") == track]
        labels = sorted({exact_label(a["normalized_reviewed_label"]) for a in members})
        candidate = pred.get("top1_candidate")
        committed = pred.get("fused_semantic_label")
        if candidate:
            track_top1_available += 1
            track_top1_correct += len(labels) == 1 and exact_label(candidate) == labels[0]
        if committed:
            committed_track_count += 1
            committed_track_correct += len(labels) == 1 and exact_label(committed) == labels[0]
        track_rows.append({"track_id": track, "observation_ids": [a["observation_id"] for a in members],
                           "human_exact_labels": labels, "status": pred.get("status"),
                           "fused_semantic_label": committed, "top1_candidate_diagnostic": candidate,
                           "top1_exact_correct_diagnostic": len(labels) == 1 and exact_label(candidate) == labels[0] if candidate else None,
                           "mixed_human_class": len(labels) != 1})
    unique_crops: dict[str, list[dict]] = defaultdict(list)
    unique_causal_states: dict[tuple[str | None, tuple[str, ...]], list[dict]] = defaultdict(list)
    for detail in observations:
        crop = detail.get("crop_sha256") or detail["observation_id"]
        unique_crops[crop].append(detail)
        causal = causal_rows[detail["observation_id"]]
        state_key = (assignments[detail["observation_id"]].get("track_id"),
                     tuple(sorted(causal.get("selected_view_bins_so_far", []))))
        unique_causal_states[state_key].append(detail)

    def unique_top1_summary(groups: dict, candidate_field: str) -> dict:
        available = correct = 0
        states = []
        for key, members in groups.items():
            candidate = next((m.get(candidate_field) for m in members if m.get(candidate_field)), None)
            human_labels = {exact_label(m["human_class"]) for m in members}
            is_correct = bool(candidate and len(human_labels) == 1 and exact_label(candidate) in human_labels)
            if candidate:
                available += 1
                correct += is_correct
            states.append({"evidence_key": str(key), "observation_ids": [m["observation_id"] for m in members],
                           "top1_candidate": candidate, "exact_correct_diagnostic": is_correct if candidate else None})
        return {"correct": correct, "candidate_available": available, "unique_evidence_state_count": len(groups),
                "is_committed_output": False, "states": states}
    n = len(annotations)
    summary = {
        "mode": mode,
        "raw_detector_exact_label": {"correct": raw_correct, "eligible": raw_available, "total_observations": n,
                                     "coverage": raw_available / n if n else None},
        "u4ov_single_view_committed": {"covered": sum(bool(raw_rows[a["observation_id"]].get("u4_ov_single_view", {}).get("fused_semantic_label")) for a in annotations),
                                       "abstained": sum(not bool(raw_rows[a["observation_id"]].get("u4_ov_single_view", {}).get("fused_semantic_label")) for a in annotations),
                                       "correctness": None, "reason": "no committed fused labels in this reviewed subset"},
        "u4ov_single_view_top1_diagnostic": {"correct": top1_correct, "candidate_available": top1_available,
                                            "total_observations": n, "candidate_coverage": top1_available / n if n else None,
                                            "is_committed_output": False},
        "u4ov_single_view_unique_crop_top1_diagnostic": unique_top1_summary(unique_crops, "u4ov_single_top1_candidate"),
        "causal_committed_label": {"correct": causal_committed_correct, "covered": causal_committed,
                                   "abstained": n - causal_committed, "total_observations": n,
                                   "accuracy_when_covered": causal_committed_correct / causal_committed if causal_committed else None,
                                   "coverage": causal_committed / n if n else None,
                                   "harmful_committed_changes": sum(1 for x in observations if x["causal_fused_label"] and exact_label(x["causal_fused_label"]) != exact_label(x["human_class"]) and exact_label(x["causal_fused_label"]) != exact_label(x["raw_detector_class"]))},
        "causal_top1_diagnostic": {"correct": causal_top1_correct, "candidate_available": causal_top1_available,
                                   "total_observations": n, "candidate_coverage": causal_top1_available / n if n else None,
                                   "is_committed_output": False},
        "causal_top1_unique_track_view_state_diagnostic": unique_top1_summary(unique_causal_states, "causal_top1_candidate_diagnostic"),
        "final_track_committed_label": {"correct_tracks": committed_track_correct, "covered_tracks": committed_track_count,
                                        "track_count_in_subset": len(track_rows), "accuracy_when_covered": committed_track_correct / committed_track_count if committed_track_count else None,
                                        "coverage": committed_track_count / len(track_rows) if track_rows else None,
                                        "harmful_committed_changes": sum(1 for x in track_rows if x["fused_semantic_label"] and not x["mixed_human_class"] and exact_label(x["fused_semantic_label"]) != x["human_exact_labels"][0])},
        "final_track_top1_diagnostic": {"correct_tracks": track_top1_correct, "candidate_available_tracks": track_top1_available,
                                        "track_count_in_subset": len(track_rows), "candidate_coverage": track_top1_available / len(track_rows) if track_rows else None,
                                        "is_committed_output": False},
        "final_tracks": track_rows,
    }
    return summary, observations


def evaluate(run_dir: Path, annotation_csv: Path, previous_metrics: Path) -> dict:
    annotations = list(csv.DictReader(annotation_csv.open(encoding="utf-8", newline="")))
    for a in annotations:
        a["frame_index"] = int(a["frame_index"])
        a["identity_status"] = a["identity_status"].strip()
        a["class_status"] = a["class_status"].strip()
        a["identity_token"] = a["identity_token"].strip()
        a["normalized_reviewed_label"] = a["normalized_reviewed_label"].strip()
        if a["identity_status"] != "confirmed" or a["class_status"] != "confirmed":
            raise ValueError("this locked diagnostic scoring set requires confirmed identity and class on every included row")
    by_mode = {}
    all_details = {}
    raw_rows = {r["observation_id"]: r for r in read_jsonl(run_dir / "u4ov/single_view_predictions.jsonl")}
    feature_manifest_path = run_dir / "appearance/u2_features/feature_manifest.jsonl"
    crop_hashes = {r["observation_id"]: r["crop_sha256"] for r in read_jsonl(feature_manifest_path)}
    causal_all = read_jsonl(run_dir / "u4ov/causal_predictions_by_mode.jsonl")
    final_all = read_jsonl(run_dir / "u4ov/final_track_predictions_by_mode.jsonl")
    for mode in ("source_raw", "source_dedup", "geometry_only"):
        assignments = {r["detection_id"]: r for r in read_jsonl(run_dir / f"association_u3/assignments_{mode}.jsonl")}
        ids = {a["observation_id"] for a in annotations}
        if not ids.issubset(assignments) or not ids.issubset(raw_rows):
            raise ValueError(f"annotation observations missing from frozen inputs for mode {mode}")
        human_rows = [{"observation_id": a["observation_id"], "frame_id": a["frame_id"], "frame_index": a["frame_index"],
                       "gt_identity": a["identity_token"], "track_id": assignments[a["observation_id"]].get("track_id"),
                       "association_status": assignments[a["observation_id"]].get("decision")}
                      for a in annotations]
        d1 = [r for r in human_rows if r["gt_identity"] == "table_1"]
        d1_no_revisit = [r for r in d1 if r["frame_index"] != 4]
        causal = {r["observation_id"]: r for r in causal_all if r["mode"] == mode}
        final = {r["track_id"]: r for r in final_all if r["mode"] == mode}
        class_summary, class_details = class_diagnostics(annotations, mode, raw_rows, causal, final, crop_hashes, assignments)
        by_mode[mode] = {
            "cross_frame_association_D1_only": u6_scoring.pairwise_association(d1),
            "cross_frame_exact_pose_revisit_excluded_D1_only": u6_scoring.pairwise_association(d1_no_revisit, excluded_frame_indices={4}),
            "same_frame_duplicate_identity_D2_only": score_same_frame_identity(annotations, assignments),
            "physical_identity_track_structure_all_six": score_identity_track_structure(annotations, assignments),
            "class": class_summary,
        }
        all_details[mode] = class_details
    ds = json.loads(previous_metrics.read_text(encoding="utf-8"))
    return {
        "schema_version": 1, "kind": "u6_human_reviewed_diagnostic_scoring", "status": "scored_fixed_reviewed_subset",
        "annotation_scope": "six observations only; annotations are evaluator-only; no predictor, assignment, or source GT modified",
        "unique_confirmed_physical_objects": 2,
        "observations": [{k: a[k] for k in ("observation_id", "frame_id", "frame_index", "identity_token", "identity_status", "reviewed_label_raw", "normalized_reviewed_label", "class_status")} for a in annotations],
        "cross_frame_scope": "D1 table_1 only; D2 same-frame observations excluded from this denominator; frame4 inclusion shown with scoring-only exclusion sensitivity",
        "same_frame_scope": "D2 table_2 pair only; reported separately, never added to cross-frame denominator",
        "official_hungarian_metrics_unchanged": True,
        "dataset_instance_metrics": ds["association"],
        "human_identity_metrics_by_mode": by_mode,
        "class_protocol": "Exact label after lowercase/trim/space-to-underscore normalization only. No dining_table=table or coffee_table=table mapping; parent-child mapping unconfirmed. Cosine top1 remains non-committed diagnostic.",
        "class_detail_by_mode": all_details,
        "source_artifacts": {
            "annotation_csv_sha256": sha256(annotation_csv),
            "previous_dataset_metrics_sha256": sha256(previous_metrics),
            "source_raw_assignments_sha256": sha256(run_dir / "association_u3/assignments_source_raw.jsonl"),
            "source_dedup_assignments_sha256": sha256(run_dir / "association_u3/assignments_source_dedup.jsonl"),
            "geometry_only_assignments_sha256": sha256(run_dir / "association_u3/assignments_geometry_only.jsonl"),
            "u4ov_single_view_sha256": sha256(run_dir / "u4ov/single_view_predictions.jsonl"),
            "u4ov_causal_sha256": sha256(run_dir / "u4ov/causal_predictions_by_mode.jsonl"),
            "u4ov_final_sha256": sha256(run_dir / "u4ov/final_track_predictions_by_mode.jsonl"),
            "u2_feature_manifest_sha256": sha256(feature_manifest_path),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--annotations", type=Path, required=True)
    ap.add_argument("--previous-metrics", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    out = args.output_dir.resolve()
    if out.exists():
        raise FileExistsError(f"refusing to overwrite {out}")
    out.mkdir(parents=True)
    report = evaluate(args.run_dir.resolve(), args.annotations.resolve(), args.previous_metrics.resolve())
    (out / "evaluation_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for mode, rows in report["class_detail_by_mode"].items():
        with (out / f"class_details_{mode}.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader(); writer.writerows(rows)
    print(json.dumps({"status": report["status"], "output_dir": str(out),
                      "summary": {mode: {"cross_frame": report["human_identity_metrics_by_mode"][mode]["cross_frame_association_D1_only"],
                                         "same_frame": report["human_identity_metrics_by_mode"][mode]["same_frame_duplicate_identity_D2_only"],
                                         "track_structure": report["human_identity_metrics_by_mode"][mode]["physical_identity_track_structure_all_six"],
                                         "class": {k: report["human_identity_metrics_by_mode"][mode]["class"][k] for k in ("raw_detector_exact_label", "u4ov_single_view_top1_diagnostic", "causal_committed_label", "causal_top1_diagnostic", "final_track_committed_label", "final_track_top1_diagnostic")}}
                                 for mode in ("source_raw", "source_dedup", "geometry_only")}}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
