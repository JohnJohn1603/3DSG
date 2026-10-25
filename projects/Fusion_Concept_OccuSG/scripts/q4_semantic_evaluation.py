"""Small post-hoc scorer for the locked Q4 semantic development population.

Identity relations are loaded only for evaluator grouping and mixed-track
diagnostics. Predictor outputs are read from frozen U4/U4-OV artifacts.
"""
from __future__ import annotations

import csv
import ast
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def label_key(value: str | None) -> str | None:
    """Normalize spelling only; never collapse synonym or parent-child labels."""
    if value is None or not str(value).strip():
        return None
    return " ".join(str(value).strip().lower().replace("_", " ").split())


def class_truth(annotation: dict[str, str]) -> str | None:
    """Only the explicit reviewed category is class GT; identity fields are ignored."""
    if annotation.get("provenance") not in {"user-reviewed", "user_reviewed"}:
        return None
    return label_key(annotation.get("reviewed_category") or annotation.get("normalized_reviewed_label"))


def metric(predictions: list[str | None], truths: list[str]) -> dict[str, Any]:
    if len(predictions) != len(truths):
        raise ValueError("prediction/truth count mismatch")
    correct = wrong = abstain = 0
    for pred, truth in zip(predictions, truths):
        pred, truth = label_key(pred), label_key(truth)
        if truth is None:
            raise ValueError("scored rows require confirmed class truth")
        if pred is None:
            abstain += 1
        elif pred == truth:
            correct += 1
        else:
            wrong += 1
    n = len(truths)
    covered = correct + wrong
    return {
        "n": n,
        "correct": correct,
        "wrong": wrong,
        "abstain": abstain,
        "coverage_total": covered / n if n else None,
        "accuracy_on_covered": correct / covered if covered else None,
        "correct_over_total": correct / n if n else None,
    }


def correction_transitions(raw: list[str | None], fused: list[str | None], truths: list[str]) -> dict[str, int]:
    if not (len(raw) == len(fused) == len(truths)):
        raise ValueError("transition input lengths differ")
    counts = Counter()
    for r, f, gt in zip(raw, fused, truths):
        r, f, gt = label_key(r), label_key(f), label_key(gt)
        raw_correct = r == gt
        if f is None:
            if raw_correct:
                counts["correct_to_abstain"] += 1
            else:
                counts["wrong_to_abstain"] += 1
        elif raw_correct and f == gt:
            counts["correct_retained"] += 1
        elif raw_correct:
            counts["correct_to_wrong"] += 1
        elif f == gt:
            counts["wrong_to_correct"] += 1
        else:
            counts["wrong_retained_wrong"] += 1
    for key in ("correct_to_wrong", "wrong_to_correct", "correct_to_abstain",
                "wrong_to_abstain", "correct_retained", "wrong_retained_wrong"):
        counts.setdefault(key, 0)
    return dict(counts)


def group_plurality(values: list[str | None]) -> str | None:
    vals = [label_key(v) for v in values if label_key(v) is not None]
    if not vals:
        return None
    counts = Counter(vals)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def identity_groups(relations: list[dict[str, Any]]) -> list[list[str]]:
    """Connected components from same_object review edges only."""
    graph: dict[str, set[str]] = defaultdict(set)
    for row in relations:
        if row.get("relation") == "same_object":
            a, b = row["left_observation_id"], row["right_observation_id"]
            graph[a].add(b); graph[b].add(a)
    groups = []
    seen: set[str] = set()
    for start in sorted(graph):
        if start in seen:
            continue
        stack, members = [start], []
        seen.add(start)
        while stack:
            node = stack.pop(); members.append(node)
            for nxt in sorted(graph[node]):
                if nxt not in seen:
                    seen.add(nxt); stack.append(nxt)
        groups.append(sorted(members))
    return groups


def explicit_mixed_pairs(track_observation_ids: set[str], relations: list[dict[str, Any]]) -> list[list[str]]:
    return [
        sorted([row["left_observation_id"], row["right_observation_id"]])
        for row in relations
        if row.get("relation") == "different_objects"
        and row["left_observation_id"] in track_observation_ids
        and row["right_observation_id"] in track_observation_ids
    ]


def view_support(rows: list[dict[str, Any]]) -> int:
    """Unique known view bins, excluding aliases; revisits in a bin count once."""
    return len({row.get("view_bin") for row in rows
                if row.get("view_bin") not in (None, "", "unknown")
                and not row.get("duplicate_alias", False)})


def causal_prefix_valid(row: dict[str, Any]) -> bool:
    """Check that the reported evidence endpoint is not after decision frame."""
    if row.get("future_observations_used") is True:
        return False
    decision = row.get("frame_index")
    end = row.get("causal_evidence_end_frame_index", decision)
    if decision is None or end is None:
        return False
    return int(end) <= int(decision)


def _status_label(row: dict[str, Any], label_key_name: str, status_key: str,
                  accepted: set[str]) -> str | None:
    return row.get(label_key_name) if row.get(status_key) in accepted else None


def score(root: Path, out: Path) -> dict[str, Any]:
    ann_root = root / "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1"
    u4 = root / "artifacts/audits/u4_semantic_fusion"
    ov = root / "artifacts/audits/u4_ov"
    contract_path = out / "q4_contract.locked.json"
    contract = json.loads(contract_path.read_text())
    for rel, digest in contract["inputs"].items():
        path = root / rel
        if not path.is_file() or sha256(path) != digest:
            raise RuntimeError(f"locked Q4 input changed or missing: {rel}")

    annotations = list(csv.DictReader((ann_root / "observation_annotations.csv").open(encoding="utf-8")))
    ann_by_id = {r["observation_id"]: r for r in annotations if class_truth(r) is not None}
    if len(ann_by_id) != 18:
        raise RuntimeError(f"expected 18 explicitly class-reviewed observations, got {len(ann_by_id)}")
    truths = {did: class_truth(row) for did, row in ann_by_id.items()}
    raw_u4ov = {r["observation_id"]: r for r in load_jsonl(ov / "observations_predictions.jsonl")}
    ov_causal = {r["observation_id"]: r for r in load_jsonl(ov / "causal_track_predictions.jsonl")}
    ov_final = {r["u3_geometry_only_track_id"]: r for r in load_jsonl(ov / "predicted_track_labels.jsonl")}
    u4_geo = {r["detection_id"]: r for r in load_jsonl(u4 / "observations_geometry_only.jsonl")}
    u4_source = {r["detection_id"]: r for r in load_jsonl(u4 / "observations_source_dedup.jsonl")}
    if not (len(raw_u4ov) == len(u4_geo) == len(u4_source) == 622):
        raise RuntimeError("locked U4/U4-OV observation populations are not the expected common 622-record cache")
    if set(raw_u4ov) != set(u4_geo) or set(raw_u4ov) != set(u4_source):
        raise RuntimeError("U4 and U4-OV full observation IDs differ")
    summaries = {
        "source_dedup": json.loads((u4 / "track_summary_source_dedup.json").read_text()),
        "geometry_only": json.loads((u4 / "track_summary_geometry_only.json").read_text()),
    }
    relations_doc = json.loads((ann_root / "identity_relations.json").read_text())
    relations = relations_doc["relations"]
    same_groups = identity_groups(relations)
    ordered = sorted(ann_by_id, key=lambda did: (int(ann_by_id[did]["frame_index"]), did))
    for did in ordered:
        if did not in raw_u4ov or did not in ov_causal or did not in u4_geo or did not in u4_source:
            raise RuntimeError(f"reviewed observation is missing locked prediction: {did}")
        if not causal_prefix_valid(ov_causal[did]):
            raise RuntimeError(f"U4-OV causal output contains future evidence: {did}")

    methods: dict[str, list[str | None]] = {
        "raw_detector_observation": [raw_u4ov[d]["raw_detector_class"] for d in ordered],
        "u4_causal_geometry_only": [
            _status_label(u4_geo[d], "resolved_class", "class_status", {"confirmed"}) for d in ordered],
        "u4_ov_single_view": [
            _status_label(raw_u4ov[d]["u4_ov_single_view"], "fused_semantic_label", "status", {"classified"}) for d in ordered],
        "u4_ov_causal_track_geometry_only": [
            _status_label(ov_causal[d], "causal_fused_semantic_label", "causal_status", {"classified"}) for d in ordered],
    }
    result_methods = {}
    transition_ids = {}
    timeline = []
    for method, predictions in methods.items():
        normalized_predictions = [label_key(p) for p in predictions]
        truth_values = [truths[d] for d in ordered]
        result_methods[method] = metric(normalized_predictions, truth_values)
        result_methods[method]["evaluation_unit"] = "confirmed-class observation"
        if method != "raw_detector_observation":
            result_methods[method]["transitions_vs_raw"] = correction_transitions(
                methods["raw_detector_observation"], predictions, truth_values)
            ids_by_transition: dict[str, list[str]] = defaultdict(list)
            for did, raw_label, fused_label, truth in zip(ordered, methods["raw_detector_observation"], predictions, truth_values):
                raw_correct = label_key(raw_label) == label_key(truth)
                fused = label_key(fused_label)
                if fused is None:
                    kind = "correct_to_abstain" if raw_correct else "wrong_to_abstain"
                elif raw_correct and fused == label_key(truth):
                    kind = "correct_retained"
                elif raw_correct:
                    kind = "correct_to_wrong"
                elif fused == label_key(truth):
                    kind = "wrong_to_correct"
                else:
                    kind = "wrong_retained_wrong"
                ids_by_transition[kind].append(did)
            transition_ids[method] = dict(ids_by_transition)

    for idx, did in enumerate(ordered):
        ann, ovrow, causal, geo = ann_by_id[did], raw_u4ov[did], ov_causal[did], u4_geo[did]
        truth = truths[did]
        timeline.append({
            "observation_id": did, "frame_index": int(ann["frame_index"]),
            "reviewed_class": truth, "class_provenance": ann["provenance"],
            "raw_detector_class": ovrow["raw_detector_class"],
            "u4_initial_class": geo.get("initial_class"), "u4_causal_label": methods["u4_causal_geometry_only"][idx],
            "u4_status": geo.get("class_status"), "u4ov_single_label": methods["u4_ov_single_view"][idx],
            "u4ov_single_status": ovrow["u4_ov_single_view"]["status"],
            "u4ov_top1": ovrow["u4_ov_single_view"].get("top1_candidate"),
            "u4ov_top2": ovrow["u4_ov_single_view"].get("top2_candidate"),
            "u4ov_margin": ovrow["u4_ov_single_view"].get("cosine_margin"),
            "u4ov_causal_label": methods["u4_ov_causal_track_geometry_only"][idx],
            "u4ov_causal_status": causal["causal_status"],
            "causal_evidence_end_frame_id": causal["causal_evidence_end_frame_id"],
            "causal_view_support": causal["causal_distinct_view_bin_count"],
            "geometry_only_track_id_evaluator_only": geo.get("track_id"),
            "u4ov_raw_fallback_used": False,
        })

    # Diagnostic object-level aggregation: the review supplies identity grouping
    # for these four same-object cases; it never enters predictor outputs.
    obs_index = {r["observation_id"]: r for r in raw_u4ov.values()}
    u4_obs_index = {r["detection_id"]: r for r in u4_geo.values()}
    ov_group_source = {}
    for source_row in csv.DictReader((ov / "confirmed_identity_group_diagnostic.csv").open(encoding="utf-8")):
        ov_group_source[frozenset(ast.literal_eval(source_row["observation_ids"]))] = source_row
    group_rows = []
    for number, group in enumerate(same_groups, 1):
        if not set(group).issubset(ann_by_id):
            continue
        gt_labels = {truths[d] for d in group}
        if len(gt_labels) != 1:
            raise RuntimeError(f"same-object review group has inconsistent class: {group}")
        raw_group = group_plurality([obs_index[d]["raw_detector_class"] for d in group])
        u4_group = group_plurality([
            _status_label(u4_obs_index[d], "resolved_class", "class_status", {"confirmed"}) for d in group])
        ov_single = group_plurality([
            _status_label(obs_index[d]["u4_ov_single_view"], "fused_semantic_label", "status", {"classified"}) for d in group])
        ov_causal_group = group_plurality([
            _status_label(ov_causal[d], "causal_fused_semantic_label", "causal_status", {"classified"}) for d in group])
        track_ids = {u4_obs_index[d].get("track_id") for d in group if u4_obs_index[d].get("track_id")}
        ov_final_labels = [ov_final[t]["fused_semantic_label"] for t in track_ids if t in ov_final]
        prior_ov_group = ov_group_source.get(frozenset(group), {})
        group_rows.append({"identity_group_id_evaluator_only": f"review_same_object_group_{number}",
            "observation_ids": group, "confirmed_class": next(iter(gt_labels)),
            "distinct_view_bins": len({obs_index[d].get("view_bin") for d in group if obs_index[d].get("view_bin") not in (None, "unknown")}),
            "raw_plurality": raw_group, "u4_causal_plurality": u4_group,
            "u4ov_single_view_plurality": ov_single, "u4ov_causal_plurality": ov_causal_group,
            "u4ov_identity_conditioned_multiview_label": (
                prior_ov_group.get("u4_ov_multi_view_label")
                if prior_ov_group.get("u4_ov_multi_view_status") == "classified" else None),
            "u4ov_identity_conditioned_multiview_status": prior_ov_group.get("u4_ov_multi_view_status", "unavailable"),
            "u4ov_final_predicted_track_plurality": group_plurality(ov_final_labels),
            "predicted_geometry_track_ids_evaluator_only": sorted(track_ids),
            "identity_supplied_for_diagnostic_grouping": True,
            "end_to_end_predictor": False})
    group_methods = {
        "raw_detector_group": [r["raw_plurality"] for r in group_rows],
        "u4_causal_group": [r["u4_causal_plurality"] for r in group_rows],
        "u4_ov_single_view_group": [r["u4ov_single_view_plurality"] for r in group_rows],
        "u4_ov_causal_group": [r["u4ov_causal_plurality"] for r in group_rows],
        "u4_ov_identity_conditioned_multiview_diagnostic": [r["u4ov_identity_conditioned_multiview_label"] for r in group_rows],
        "u4_ov_final_predicted_tracks_grouped_by_review_identity": [r["u4ov_final_predicted_track_plurality"] for r in group_rows],
    }
    group_truths = [r["confirmed_class"] for r in group_rows]
    group_metrics = {name: metric(preds, group_truths) for name, preds in group_methods.items()}
    for value in group_metrics.values():
        value["evaluation_unit"] = "unique human-confirmed physical-identity group; identity supplied for diagnostic grouping"

    # Track-level end-to-end snapshot on the two already generated U3 assignment
    # modes. Every reviewed track is represented; explicit mixed identities are
    # kept visible and withheld from the single-class track score.
    track_reports = {}
    for mode, rows_by_id, summaries_by_track in (
        ("source_dedup", u4_source, summaries["source_dedup"]),
        ("geometry_only", u4_geo, summaries["geometry_only"]),
    ):
        by_track: dict[str, list[str]] = defaultdict(list)
        unassigned_ids = []
        for did in ordered:
            track = rows_by_id[did].get("track_id")
            if track:
                by_track[str(track)].append(did)
            else:
                unassigned_ids.append(did)
        track_rows = []
        mixed_count = heterogeneous_count = 0
        for track, ids in sorted(by_track.items()):
            gt_set = {truths[d] for d in ids}
            mixed_edges = explicit_mixed_pairs(set(ids), relations)
            pred_raw = group_plurality([raw_u4ov[d]["raw_detector_class"] for d in ids])
            summary = summaries_by_track.get(track, {})
            pred_u4 = summary.get("resolved_class") if summary.get("class_status") == "confirmed" else None
            pred_ov = None
            if mode == "geometry_only" and track in ov_final:
                pred_ov = ov_final[track].get("fused_semantic_label") if ov_final[track].get("status") == "classified" else None
            row = {"track_id_evaluator_only": track, "observation_ids": ids,
                   "reviewed_class_set": sorted(gt_set), "raw_plurality": pred_raw,
                   "u4_final_label": pred_u4, "u4ov_final_label": pred_ov,
                   "explicit_different_object_pairs": mixed_edges,
                   "mixed_identity": bool(mixed_edges), "scored_as_single_class_track": False}
            if mixed_edges:
                mixed_count += 1
            elif len(gt_set) != 1:
                heterogeneous_count += 1
            else:
                row["scored_as_single_class_track"] = True
                gt = next(iter(gt_set))
                track_rows.append(row)
                # handled below per method without dropping other reviewed tracks
                row["track_truth"] = gt
                continue
            track_rows.append(row)

        def track_metric(pred_key: str) -> dict[str, Any]:
            eligible = [r for r in track_rows if r.get("scored_as_single_class_track")]
            return metric([r.get(pred_key) for r in eligible], [r["track_truth"] for r in eligible]) if eligible else metric([], [])
        # Add raw track labels for rows with a homogeneous reviewed class.
        track_reports[mode] = {
            "reviewed_predicted_tracks": len(track_rows),
            "explicit_mixed_identity_tracks_retained": mixed_count,
            "heterogeneous_reviewed_class_tracks_retained": heterogeneous_count,
            "unassigned_reviewed_observations": len(unassigned_ids),
            "unassigned_observation_ids": unassigned_ids,
            "single_class_track_metrics": {
                "raw_detector_plurality": track_metric("raw_plurality"),
                "u4_final": track_metric("u4_final_label"),
                "u4_ov_final": (track_metric("u4ov_final_label") if mode == "geometry_only" else {
                    "status": "unavailable",
                    "reason": "Frozen U4-OV final-track predictions only use geometry-only assignments; source assignment-specific OV track state was not generated."}),
            },
            "all_reviewed_track_label_coverage": {
                "denominator_all_reviewed_predicted_tracks": len(track_rows),
                "raw_detector_plurality": sum(r.get("raw_plurality") is not None for r in track_rows) / len(track_rows) if track_rows else None,
                "u4_final": sum(r.get("u4_final_label") is not None for r in track_rows) / len(track_rows) if track_rows else None,
                "u4_ov_final": (sum(r.get("u4ov_final_label") is not None for r in track_rows) / len(track_rows)
                    if mode == "geometry_only" and track_rows else None),
            },
            "track_denominator_accounting": {
                "all_reviewed_predicted_tracks": len(track_rows),
                "single_class_non_mixed_tracks_scored": sum(r.get("scored_as_single_class_track", False) for r in track_rows),
                "explicit_mixed_identity_tracks": mixed_count,
                "heterogeneous_reviewed_class_tracks": heterogeneous_count,
                "unassigned_reviewed_observations": len(unassigned_ids),
            },
            "tracks": track_rows,
            "caveat": "Track-level score is conditional on one reviewed class and no explicit reviewed different-object pair; mixed/heterogeneous tracks remain listed, not silently removed from the report denominator."}

    vocab = json.loads((ov / "vocabulary_v1.json").read_text())
    ov_vocab = {label_key(row["label"]) for row in vocab["categories"]}
    confirmed_gt = list(truths.values())
    unsupported_ov_truth = Counter(gt for gt in confirmed_gt if gt not in ov_vocab)
    raw_classes = {label_key(r["raw_detector_class"]) for r in raw_u4ov.values()}
    raw_truth_support = {"status": "observed-output-vocabulary-lower-bound_only",
        "confirmed_truth_supported_count": sum(gt in raw_classes for gt in confirmed_gt),
        "confirmed_truth_total": len(confirmed_gt),
        "unsupported_or_not_observed_labels": dict(Counter(gt for gt in confirmed_gt if gt not in raw_classes))}

    return {
        "schema_version": 1, "status": "q4_semantic_baseline_scored_development_not_promoted",
        "contract_sha256": sha256(contract_path),
        "population": {"reviewed_observations": len(ann_by_id), "confirmed_class": len(ann_by_id),
            "class_uncertain": 0, "class_unsupported_annotation": 0, "unreviewed_in_full_predictor_population": 622-len(ann_by_id),
            "same_object_identity_groups": len(group_rows), "identity_tokens_are_not_used_as_class": True,
            "annotation_is_evaluator_only": True, "heldout_status": "development cases already reviewed; not a benchmark or holdout"},
        "prediction_provenance": {"U4_config_sha256": sha256(u4/"u4_config.locked.json"),
            "U4_OV_config_sha256": sha256(ov/"u4_ov_config.locked.json"),
            "U4_OV_vocabulary_sha256": sha256(ov/"vocabulary_v1.json"),
            "U4_OV_image_text_predictions_reused": True, "new_embedding_extraction": False,
            "GT_or_review_loaded_for_prediction": False, "room_labels_used": False,
            "source_semantic_artifacts_available": True, "K4_same_cache_assignments_available": False,
            "K4_unavailable_reason": "Q3/K4 assignments use hm3d-00802-main observation IDs; Q4 class annotations and frozen semantic outputs use hm3d-00802-main-dense IDs. No full-ID compatible K4 assignment artifact is present."},
        "observation_causal_exact_metrics": result_methods,
        "observation_transition_ids_vs_raw": transition_ids,
        "identity_conditioned_final_object_diagnostic": {"grouping": "human-confirmed same_object relations; identity provided for diagnostic grouping only",
            "physical_identity_groups_scored": len(group_rows), "metrics": group_metrics, "groups": group_rows,
            "hierarchical_metrics": "unavailable; taxonomy parent-child proposal remains unapproved; exact-label metrics only"},
        "predicted_track_final_map": track_reports,
        "vocabulary_support": {"u4_ov_locked_vocab_labels": len(ov_vocab),
            "u4_ov_confirmed_truth_supported": len(confirmed_gt)-sum(unsupported_ov_truth.values()),
            "u4_ov_unsupported_truth_labels": dict(unsupported_ov_truth),
            "u4_raw_detector_and_voting_share_raw_class_domain": True,
            "u4_raw_detector_and_voting_support_lower_bound": raw_truth_support,
            "raw_u4_observed_vocabulary": raw_truth_support,
            "note": "Observed raw labels are only a lower bound on the detector label space. No evaluator category mapping is self-confirmed; user-reviewed exact labels are scored literally."},
        "hierarchical_metrics": "unavailable; no taxonomy edges approved. `table` remains distinct from `dining table`; `stove` remains distinct from `oven`.",
        "timeline_csv": "q4_observation_timeline.csv",
        "observation_timeline_rows": timeline,
        "improvement": {"status": "not_run_before_baseline_report", "reason": "The locked baseline comparison is the first Q4 result; any candidate policy must be specified and locked in a separate bounded development step before candidate scoring."},
    }
