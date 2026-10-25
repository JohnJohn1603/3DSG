#!/usr/bin/env python3
"""Evaluator-only scoring of the HM3D 00800 multiview pilot user review."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

try:  # Support both direct script execution and package-style unit-test imports.
    from .u4ov_multiview_review_metrics import class_metrics, normalize_label, pairwise_association_metrics
except ImportError:  # pragma: no cover - direct execution path
    from u4ov_multiview_review_metrics import class_metrics, normalize_label, pairwise_association_metrics

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts/runs/hm3d_00800_u4ov_multiview_pilot_v2_640"
AUDIT = ROOT / "artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1"
VERSION = ROOT / "artifacts/audits/u4ov_multiview_pilot_user_review/versions/u4ov-00800-mv-pilot-v2-review-20261009-v1"
MODES = ("source_raw", "source_dedup", "geometry_only", "geometry_appearance")
EXACT_REVISIT = "hm3d-00800-u4ov-mv-pilot-v2-0004:det-0003"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_annotations() -> dict[str, dict]:
    rows = list(csv.DictReader((VERSION / "observation_annotations.csv").open(encoding="utf-8")))
    result = {row["observation_id"]: row for row in rows}
    if len(result) != len(rows):
        raise RuntimeError("annotation observation IDs are not unique")
    for row in rows:
        row["frame_index"] = int(row["frame_index"])
        if row.get("class_status") == "confirmed":
            if normalize_label(row.get("reviewed_label_raw")) != normalize_label(row.get("normalized_reviewed_label")):
                raise RuntimeError(f"label normalization changed meaning or value: {row['observation_id']}")
    return result


def prediction_value(row: dict) -> str | None:
    c = row.get("causal_classification", row.get("final_map_classification", {}))
    if c.get("status") != "classified":
        return None
    return c.get("fused_semantic_label")


def causal_transitions(annotations: dict[str, dict], eligible_ids: list[str],
                       causal_by_mode: dict[tuple[str, str], dict],
                       assignment_by_mode: dict[str, dict[str, dict]],
                       single_by_id: dict[str, dict], mode: str) -> dict:
    """Compare causal U4-OV to the raw class on the first observation of its causal track."""
    by_track: dict[str, list[dict]] = defaultdict(list)
    for oid, assignment in assignment_by_mode[mode].items():
        if assignment.get("track_id") is not None:
            by_track[str(assignment["track_id"])].append(single_by_id[oid])
    rows = []
    corrected = harmful_wrong = harmful_abstain = raw_correct = raw_wrong = 0
    for oid in eligible_ids:
        ann = annotations[oid]
        assignment = assignment_by_mode[mode][oid]
        track = str(assignment["track_id"])
        history = [r for r in by_track[track] if int(r["frame_index"]) <= ann["frame_index"]]
        first = min(history, key=lambda r: (int(r["frame_index"]), r["observation_id"]))
        raw = normalize_label(first.get("raw_detector_class") or first.get("raw_class_name"))
        truth = ann["normalized_reviewed_label"]
        predrow = causal_by_mode[(mode, oid)]
        pred = normalize_label(prediction_value(predrow))
        raw_is_correct = raw == truth
        if raw_is_correct: raw_correct += 1
        else: raw_wrong += 1
        if not raw_is_correct and pred == truth: corrected += 1
        if raw_is_correct and pred is not None and pred != truth: harmful_wrong += 1
        if raw_is_correct and pred is None: harmful_abstain += 1
        rows.append({"observation_id": oid, "first_observation_raw_class": raw,
                     "reviewed_label": truth, "causal_label": pred,
                     "causal_status": predrow["causal_classification"]["status"],
                     "first_raw_exact_correct": raw_is_correct,
                     "corrected_raw_error": (not raw_is_correct and pred == truth),
                     "harmful_wrong_relabel": (raw_is_correct and pred is not None and pred != truth),
                     "harmful_abstention_after_correct_raw": (raw_is_correct and pred is None)})
    return {"n": len(rows), "first_raw_exact_correct": raw_correct, "first_raw_exact_incorrect": raw_wrong,
            "correction_of_raw_error": corrected, "harmful_wrong_relabel": harmful_wrong,
            "harmful_abstention_after_correct_raw": harmful_abstain,
            "observations": rows,
            "definition": "first raw detector class on the causal predicted track at or before this observation; formatting-only normalization; transition counts use supported, class-confirmed observations only"}


def final_track_metrics(annotations: dict[str, dict], supported_class_ids: list[str],
                        all_single: dict[str, dict], assignments: dict[str, dict[str, dict]],
                        final_predictions: dict[tuple[str, str], dict], mode: str) -> dict:
    reviewed_tracks: dict[str, list[str]] = defaultdict(list)
    for oid in annotations:
        track = assignments[mode].get(oid, {}).get("track_id")
        if track is not None:
            reviewed_tracks[str(track)].append(oid)
    track_rows = []
    mixed_identity_tracks = []
    identity_unconfirmed_tracks = []
    mixed_class_tracks = []
    unsupported_or_unlabeled_tracks = []
    correct = incorrect = abstain = corrected = harmful_wrong = harmful_abstain = 0
    unique_objects = set()
    for track, ids in sorted(reviewed_tracks.items()):
        anns = [annotations[oid] for oid in ids]
        identity_tokens = {r["identity_token"] for r in anns if r["identity_status"] == "confirmed" and r.get("identity_token")}
        confirmed_classes = {r["normalized_reviewed_label"] for r in anns
                             if r["class_status"] == "confirmed" and r.get("normalized_reviewed_label")}
        supported_classes = {c for c in confirmed_classes if c in VOCAB}
        truth_ids = [oid for oid in ids if annotations[oid]["class_status"] == "confirmed"
                     and annotations[oid].get("normalized_reviewed_label") in VOCAB]
        if len(identity_tokens) > 1:
            mixed_identity_tracks.append({"track_id": track, "identity_tokens": sorted(identity_tokens),
                                          "observation_ids": ids})
            continue
        if len(identity_tokens) != 1:
            identity_unconfirmed_tracks.append({"track_id": track, "observation_ids": ids,
                                                "class_status_is_scored_separately": True})
            continue
        if len(confirmed_classes) > 1:
            mixed_class_tracks.append({"track_id": track, "reviewed_classes": sorted(confirmed_classes),
                                       "observation_ids": ids})
            continue
        if len(supported_classes) != 1 or not truth_ids:
            unsupported_or_unlabeled_tracks.append({"track_id": track, "reviewed_classes": sorted(confirmed_classes),
                                                    "observation_ids": ids})
            continue
        truth = next(iter(supported_classes))
        if identity_tokens:
            unique_objects.add(next(iter(identity_tokens)))
        frow = final_predictions.get((mode, track))
        classification = frow.get("final_map_classification", {}) if frow else {}
        pred = normalize_label(prediction_value({"final_map_classification": classification})) if frow else None
        raw_first_record = min((all_single[oid] for oid, assignment in assignments[mode].items()
                                if str(assignment.get("track_id")) == track),
                               key=lambda r: (int(r["frame_index"]), r["observation_id"]))
        first_raw = normalize_label(raw_first_record.get("raw_detector_class") or raw_first_record.get("raw_class_name"))
        if pred is None:
            abstain += 1; outcome = "abstain"
        elif pred == truth:
            correct += 1; outcome = "correct"
        else:
            incorrect += 1; outcome = "incorrect"
        if first_raw != truth and pred == truth: corrected += 1
        if first_raw == truth and pred is not None and pred != truth: harmful_wrong += 1
        if first_raw == truth and pred is None: harmful_abstain += 1
        track_rows.append({"track_id": track, "observation_ids": ids,
                           "reviewed_identity_token": next(iter(identity_tokens)) if identity_tokens else None,
                           "reviewed_class": truth, "first_raw_class": first_raw,
                           "final_label": pred, "final_status": classification.get("status", "missing"),
                           "outcome": outcome})
    covered = correct + incorrect
    return {"eligible_identity_pure_tracks": len(track_rows), "correct": correct, "incorrect": incorrect,
            "abstention": abstain, "coverage": covered / len(track_rows) if track_rows else None,
            "accuracy_on_covered": correct / covered if covered else None,
            "correct_over_all_eligible_tracks": correct / len(track_rows) if track_rows else None,
            "unique_reviewed_objects_represented": len(unique_objects),
            "mixed_identity_tracks_excluded_no_majority_truth": mixed_identity_tracks,
            "tracks_without_confirmed_identity_excluded": identity_unconfirmed_tracks,
            "mixed_class_tracks_excluded": mixed_class_tracks,
            "unsupported_or_unlabeled_tracks_excluded": unsupported_or_unlabeled_tracks,
            "correction_of_first_raw_error": corrected, "harmful_wrong_relabel": harmful_wrong,
            "harmful_abstention_after_correct_raw": harmful_abstain,
            "track_rows": track_rows,
            "scope_note": "one record per predicted track containing reviewed observations; tracks with multiple confirmed user identity tokens are false-merge cases and excluded from class scoring; no majority GT class is assigned; denominator is selected annotation scope, not all run tracks"}


def main() -> None:
    annotations = read_annotations()
    selection = json.loads((RUN / "u4ov/review_short_blinded/selection_manifest.json").read_text())
    if set(annotations) != {r["observation_id"] for r in selection["selected_observations"]}:
        raise RuntimeError("annotation rows do not match the frozen review packet IDs")

    lock = json.loads((AUDIT / "multiview_pilot_v2_lock.json").read_text())
    for key, path in (("u3_association_lock", ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json"),
                      ("u4ov_config_lock", ROOT / "artifacts/audits/u4_ov/u4_ov_config.locked.json"),
                      ("u4ov_vocabulary", ROOT / "artifacts/audits/u4_ov/vocabulary_v1.json")):
        if sha(path) != lock["files"][key]["sha256"]:
            raise RuntimeError(f"locked predictor input changed: {key}")

    single_rows = jsonl(RUN / "u4ov/single_view_predictions.jsonl")
    single = {r["observation_id"]: r for r in single_rows}
    if len(single) != 30:
        raise RuntimeError(f"expected the frozen 30 observations, got {len(single)}")
    causal = {(r["mode"], r["observation_id"]): r for r in jsonl(RUN / "u4ov/causal_predictions_by_mode.jsonl")}
    final = {(r["mode"], r["track_id"]): r for r in jsonl(RUN / "u4ov/final_track_predictions_by_mode.jsonl")}
    assignments = {mode: {r["detection_id"]: r for r in jsonl(RUN / f"association/assignments_{mode}.jsonl")}
                   for mode in MODES}
    vocabulary_json = json.loads((ROOT / "artifacts/audits/u4_ov/vocabulary_v1.json").read_text())
    global VOCAB
    VOCAB = {normalize_label(r["label"]) for r in vocabulary_json["categories"]}

    identity_confirmed = {oid: row for oid, row in annotations.items()
                          if row["identity_status"] == "confirmed" and row.get("identity_token")}
    class_confirmed = {oid: row for oid, row in annotations.items()
                       if row["class_status"] == "confirmed" and row.get("normalized_reviewed_label")}
    supported_class = {oid: row for oid, row in class_confirmed.items()
                       if normalize_label(row["normalized_reviewed_label"]) in VOCAB}
    class_eligible_ids = sorted(supported_class, key=lambda oid: (annotations[oid]["frame_index"], oid))
    predictions_by_observation: dict[str, dict[str, str | None]] = {oid: {} for oid in annotations}

    mode_reports = {}
    all_pair_rows = {}
    for mode in MODES:
        pair = pairwise_association_metrics(annotations, assignments[mode])
        pair_no_revisit = pairwise_association_metrics(annotations, assignments[mode], {EXACT_REVISIT})
        for (pred_mode, oid), row in causal.items():
            if pred_mode == mode and oid in predictions_by_observation:
                predictions_by_observation[oid][f"{mode}_causal"] = prediction_value(row)
        for oid, row in annotations.items():
            track = assignments[mode].get(oid, {}).get("track_id")
            frow = final.get((mode, str(track))) if track is not None else None
            predictions_by_observation[oid][f"{mode}_final_track_projected"] = prediction_value(frow) if frow else None
        mode_class = class_metrics(annotations, {
            oid: {"causal": predictions_by_observation[oid].get(f"{mode}_causal")}
            for oid in annotations
        }, VOCAB)["causal"]
        mode_class["prediction_status_counts_on_eligible_observations"] = dict(Counter(
            causal[(mode, oid)]["causal_classification"]["status"] for oid in class_eligible_ids
        ))
        transitions = causal_transitions(annotations, class_eligible_ids, causal, assignments, single, mode)
        final_metrics = final_track_metrics(annotations, class_eligible_ids, single, assignments, final, mode)
        final_metrics["prediction_status_counts_on_eligible_tracks"] = dict(
            Counter(row["final_status"] for row in final_metrics["track_rows"])
        )
        mode_reports[mode] = {"association": {k: v for k, v in pair.items() if k != "pair_rows"},
                              "association_without_exact_pose_revisit_sensitivity": {k: v for k, v in pair_no_revisit.items() if k != "pair_rows"},
                              "causal_class": mode_class, "causal_transitions_vs_first_raw": transitions,
                              "final_track_class": final_metrics}
        all_pair_rows[mode] = pair["pair_rows"]

    view_index = {r["detection_id"]: r for r in csv.DictReader(
        (AUDIT / "multiview_pilot_observation_index.csv").open(encoding="utf-8"))}
    object_view_support = []
    for token in sorted({r["identity_token"] for r in identity_confirmed.values()}):
        ids = [oid for oid, row in identity_confirmed.items() if row["identity_token"] == token]
        bins = sorted({view_index[oid]["u4ov_view_bin"] for oid in ids if view_index[oid]["u4ov_view_bin"] != "unknown"})
        hashes = [view_index[oid]["rgb_sha256"] for oid in ids]
        positions = [tuple(round(float(x), 5) for x in json.loads(view_index[oid]["sensor_position_habitat_actual"])) for oid in ids]
        pose_signatures = [(positions[i], round(float(view_index[oid]["yaw_rad"]), 5)) for i, oid in enumerate(ids)]
        object_view_support.append({"identity_token": token, "observation_ids": ids,
                                   "distinct_locked_view_bins": bins,
                                   "distinct_locked_view_bin_count": len(bins),
                                   "distinct_actual_sensor_position_count": len(set(positions)),
                                   "distinct_actual_position_and_yaw_pose_count": len(set(pose_signatures)),
                                   "unique_rgb_hash_count": len(set(hashes)),
                                   "revisit_observations_reusing_rgb": [oid for oid in ids
                                       if hashes.count(view_index[oid]["rgb_sha256"]) > 1]})

    class_report = class_metrics(annotations, predictions_by_observation, VOCAB)
    raw_baselines = {}
    for mode in MODES:
        raw_pred = {}
        for oid in annotations:
            track = assignments[mode].get(oid, {}).get("track_id")
            if track is None:
                raw_pred[oid] = None; continue
            history = [r for r in single_rows if str(assignments[mode].get(r["observation_id"], {}).get("track_id")) == str(track)
                       and int(r["frame_index"]) <= annotations[oid]["frame_index"]]
            first = min(history, key=lambda r: (int(r["frame_index"]), r["observation_id"])) if history else single[oid]
            raw_pred[oid] = first.get("raw_detector_class") or first.get("raw_class_name")
        raw_baselines[mode] = class_metrics(annotations, {oid: {"first_observation_raw": p} for oid, p in raw_pred.items()}, VOCAB)["first_observation_raw"]

    manifest_path = VERSION / "annotation_manifest.json"
    source_paths = [
        VERSION / "observation_annotations.csv", VERSION / "user_response_verbatim.vi.txt",
        VERSION / "category_mapping_status.json",
        RUN / "u4ov/review_short_blinded/selection_manifest.json",
        RUN / "u4ov/single_view_predictions.jsonl", RUN / "u4ov/causal_predictions_by_mode.jsonl",
        RUN / "u4ov/final_track_predictions_by_mode.jsonl",
        *[RUN / f"association/assignments_{mode}.jsonl" for mode in MODES],
        AUDIT / "multiview_pilot_v2_lock.json",
    ]
    manifest = {"annotation_version": VERSION.name, "provenance": "user_reviewed; evaluator-only; never loaded by predictors",
                "created_date_local": "2026-10-09", "raw_user_response_sha256": sha(VERSION / "user_response_verbatim.vi.txt"),
                "annotation_table_sha256": sha(VERSION / "observation_annotations.csv"),
                "source_sha256": {str(p.relative_to(ROOT)): sha(p) for p in source_paths},
                "identity_and_class_status_are_independent": True,
                "class_normalization": "strip, casefold, collapse whitespace, replace underscore with space; no semantic mapping",
                "unconfirmed_identity_proposal": "obj_C at frame 1 det 2 retained in proposal field and excluded from identity scoring",
                "unreviewed_observation": "frame 3 det 2 excluded from correctness scoring",
                "exact_pose_revisit": EXACT_REVISIT}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report = {
        "schema_version": 1, "status": "development_user_review_scored_evaluator_only",
        "annotation_version": VERSION.name, "scene": "HM3D 00800; development only; not unseen test",
        "predictor_inputs_modified": False, "capture_or_model_rerun": False, "thresholds_tuned": False,
        "scope": {"packet_observations": 20, "identity_confirmed_observations": len(identity_confirmed),
                  "identity_uncertain_observations": sum(r["identity_status"] == "uncertain" for r in annotations.values()),
                  "identity_unreviewed_observations": sum(r["identity_status"] == "unreviewed" for r in annotations.values()),
                  "independent_user_identity_tokens": len({r["identity_token"] for r in identity_confirmed.values()}),
                  "class_confirmed_observations": len(class_confirmed),
                  "class_uncertain_observations": sum(r["class_status"] == "uncertain" for r in annotations.values()),
                  "class_unreviewed_observations": sum(r["class_status"] == "unreviewed" for r in annotations.values()),
                  "supported_exact_vocabulary_observations": len(supported_class),
                  "supported_class_object_count": len({r["identity_token"] for oid, r in identity_confirmed.items()
                                                         if oid in supported_class}),
                  "unsupported_vocabulary_observations": class_report["unsupported_truth_observations"],
                  "identity_uncertain_ids_excluded_from_identity_scoring": [oid for oid, r in annotations.items() if r["identity_status"] == "uncertain"],
                  "unreviewed_ids_excluded_from_all_scoring": [oid for oid, r in annotations.items() if r["identity_status"] == "unreviewed"]},
        "category_mapping": {"status": "no COCO/evaluator category mappings were confirmed in this review",
                             "exact_scoring_rule": "direct literal U4-OV label comparison after formatting-only normalization",
                             "kitchen island": "not in locked 105-label vocabulary; no mapping to dining table/table",
                             "stove": "not in locked 105-label vocabulary; no mapping to oven",
                             "supported_exact_labels": sorted(VOCAB.intersection({"chair", "clock", "microwave", "refrigerator"}))},
        "association_pairwise_protocol": "cross-frame pairs only; same-frame pairs omitted; identity confirmed at both endpoints required; exact-pose revisit included as a timestamped observation pair in primary score and excluded only in sensitivity; it never adds distinct view-bin support",
        "independent_view_support_by_reviewed_object": object_view_support,
        "association_and_semantic_by_mode": mode_reports,
        "causal_class_metrics_all_modes": class_report,
        "first_observation_raw_exact_metrics_by_mode": raw_baselines,
        "metric_limits": ["sample is small and selected for review, not a benchmark", "class correctness only for exact labels present in locked candidate vocabulary", "final-track scoring is restricted to reviewed-scope tracks and excludes tracks with multiple confirmed identity tokens", "user annotations are evaluator-only"],
        "locked_predictor_config_hashes": {key: lock["files"][key]["sha256"] for key in ["u3_association_lock", "u4_semantic_lock", "u4ov_config_lock", "u4ov_vocabulary", "u4ov_text_features"]},
        "source_artifact_sha256": {str(p.relative_to(ROOT)): sha(p) for p in source_paths},
    }
    (VERSION / "evaluation_report.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (VERSION / "pairwise_rows_by_mode.json").write_text(json.dumps(all_pair_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (VERSION / "scored_observation_class_table.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["observation_id", "identity_token", "identity_status", "reviewed_label", "class_status", "vocabulary_supported"]
        for mode in MODES:
            fields += [f"{mode}_raw_initial", f"{mode}_causal_label", f"{mode}_causal_status", f"{mode}_final_track_id", f"{mode}_final_label", f"{mode}_final_status"]
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        for oid, ann in sorted(annotations.items(), key=lambda x: (x[1]["frame_index"], x[0])):
            row = {"observation_id": oid, "identity_token": ann.get("identity_token") or "",
                   "identity_status": ann["identity_status"], "reviewed_label": ann.get("normalized_reviewed_label") or "",
                   "class_status": ann["class_status"],
                   "vocabulary_supported": str(normalize_label(ann.get("normalized_reviewed_label")) in VOCAB).lower()}
            for mode in MODES:
                c = causal.get((mode, oid), {})
                track = assignments[mode].get(oid, {}).get("track_id")
                fr = final.get((mode, str(track))) if track is not None else None
                causal_class = c.get("causal_classification", {})
                final_class = fr.get("final_map_classification", {}) if fr else {}
                row.update({f"{mode}_raw_initial": raw_baselines[mode]["comparisons"][[r["observation_id"] for r in raw_baselines[mode]["comparisons"]].index(oid)]["prediction"] if oid in [r["observation_id"] for r in raw_baselines[mode]["comparisons"]] else "",
                            f"{mode}_causal_label": prediction_value(c) or "", f"{mode}_causal_status": causal_class.get("status", "missing"),
                            f"{mode}_final_track_id": track or "", f"{mode}_final_label": prediction_value(fr) if fr and prediction_value(fr) else "",
                            f"{mode}_final_status": final_class.get("status", "missing")})
            writer.writerow(row)
    print(json.dumps({"output": str(VERSION / "evaluation_report.json"), "scope": report["scope"],
                      "association_by_mode": {m: {k: v for k, v in r["association"].items() if k in {"TP", "FP", "FN", "precision", "recall", "false_merge_track_count", "fragmented_identity_count"}} for m, r in mode_reports.items()},
                      "causal_class": {m: {k: v for k, v in r["causal_class"].items() if k in {"n", "correct", "incorrect", "abstention", "coverage", "accuracy_on_covered"}} for m, r in mode_reports.items()},
                      "final_tracks": {m: {k: v for k, v in r["final_track_class"].items() if k in {"eligible_identity_pure_tracks", "correct", "incorrect", "abstention", "coverage", "accuracy_on_covered", "correction_of_first_raw_error", "harmful_wrong_relabel", "harmful_abstention_after_correct_raw"}} for m, r in mode_reports.items()}}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
