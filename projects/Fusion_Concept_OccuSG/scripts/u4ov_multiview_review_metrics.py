"""Metric helpers for evaluator-only HM3D 00800 U4-OV user review."""
from __future__ import annotations

from itertools import combinations


def normalize_label(value: str | None) -> str | None:
    """Formatting normalization only: underscores/whitespace and case."""
    if value is None:
        return None
    normalized = " ".join(value.replace("_", " ").split()).casefold()
    return normalized or None


def pairwise_association_metrics(annotations: dict[str, dict], assignments: dict[str, dict],
                                 exclude_observation_ids: set[str] | None = None) -> dict:
    """Score unique cross-frame pairs with confirmed identity labels only."""
    excluded_ids = exclude_observation_ids or set()
    reviewed = [
        (oid, row) for oid, row in annotations.items()
        if row["identity_status"] == "confirmed" and row.get("identity_token")
        and oid not in excluded_ids
    ]
    total_tp = total_fp = total_fn = total_tn = 0
    scored_pairs = []
    all_cross_frame_pairs = 0
    excluded_missing_identity_pairs = 0
    same_frame_pairs_omitted = 0
    for (a, ann_a), (b, ann_b) in combinations(sorted(reviewed, key=lambda x: x[0]), 2):
        if ann_a["frame_index"] == ann_b["frame_index"]:
            same_frame_pairs_omitted += 1
            continue
        same_gt = ann_a["identity_token"] == ann_b["identity_token"]
        pa = assignments.get(a, {}).get("track_id")
        pb = assignments.get(b, {}).get("track_id")
        same_pred = pa is not None and pb is not None and pa == pb
        if same_gt and same_pred:
            outcome = "TP"; total_tp += 1
        elif same_gt:
            outcome = "FN"; total_fn += 1
        elif same_pred:
            outcome = "FP"; total_fp += 1
        else:
            outcome = "TN"; total_tn += 1
        scored_pairs.append({"left_observation_id": a, "right_observation_id": b,
                             "same_user_identity": same_gt, "same_predicted_track": same_pred,
                             "left_track_id": pa, "right_track_id": pb, "outcome": outcome})

    # Count the possible cross-frame pairs over the packet to quantify exclusions.
    all_rows = [(oid, row) for oid, row in annotations.items() if oid not in excluded_ids]
    for (_, ann_a), (_, ann_b) in combinations(all_rows, 2):
        if ann_a["frame_index"] != ann_b["frame_index"]:
            all_cross_frame_pairs += 1
    excluded_missing_identity_pairs = all_cross_frame_pairs - len(scored_pairs)

    gt_to_tracks: dict[str, set[str]] = {}
    track_to_gt: dict[str, set[str]] = {}
    for oid, ann in reviewed:
        track = assignments.get(oid, {}).get("track_id")
        if track is None:
            continue
        gt_to_tracks.setdefault(ann["identity_token"], set()).add(str(track))
        track_to_gt.setdefault(str(track), set()).add(ann["identity_token"])
    fragmented = {gid: sorted(tracks) for gid, tracks in gt_to_tracks.items() if len(tracks) > 1}
    false_merge = {track: sorted(ids) for track, ids in track_to_gt.items() if len(ids) > 1}
    return {
        "confirmed_identity_observations": len(reviewed),
        "scored_cross_frame_pairs": len(scored_pairs),
        "all_packet_cross_frame_pairs": all_cross_frame_pairs,
        "pairs_excluded_for_missing_or_uncertain_identity": excluded_missing_identity_pairs,
        "same_frame_pairs_omitted": same_frame_pairs_omitted,
        "TP": total_tp, "FP": total_fp, "FN": total_fn, "TN": total_tn,
        "precision": total_tp / (total_tp + total_fp) if total_tp + total_fp else None,
        "recall": total_tp / (total_tp + total_fn) if total_tp + total_fn else None,
        "confirmed_identity_count": len({r["identity_token"] for _, r in reviewed}),
        "false_merge_track_count": len(false_merge),
        "false_merge_tracks": false_merge,
        "false_merge_pair_count": total_fp,
        "fragmented_identity_count": len(fragmented),
        "fragmentation_excess_track_count": sum(len(v) - 1 for v in fragmented.values()),
        "fragmented_identities": fragmented,
        "pair_rows": scored_pairs,
        "metric_definition": "unique cross-frame observation pairs; same user token means same physical object; different confirmed tokens means different objects; both endpoints must have confirmed identity; missing/unreviewed/uncertain identity pairs are excluded; same-frame pairs are omitted; missing predicted assignment is a non-match and therefore FN for a positive pair",
    }


def class_metrics(annotations: dict[str, dict], predictions: dict[str, dict], vocabulary: set[str]) -> dict:
    """Strict label diagnostics; unsupported truths stay outside accuracy denominator."""
    rows = []
    unsupported = []
    unreviewed = []
    uncertain = []
    for oid, ann in sorted(annotations.items(), key=lambda x: (x[1]["frame_index"], x[0])):
        if ann["class_status"] == "unreviewed":
            unreviewed.append(oid)
            continue
        if ann["class_status"] != "confirmed" or not ann.get("normalized_reviewed_label"):
            uncertain.append(oid)
            continue
        truth = normalize_label(ann["normalized_reviewed_label"])
        if truth not in vocabulary:
            unsupported.append({"observation_id": oid, "reviewed_label": truth})
            continue
        rows.append((oid, truth))
    result = {"eligible_observation_count": len(rows), "unsupported_truth_observations": unsupported,
              "unreviewed_observation_ids": unreviewed, "uncertain_class_observation_ids": uncertain}
    for method in sorted({method for p in predictions.values() for method in p}):
        correct = incorrect = abstain = 0
        comparisons = []
        for oid, truth in rows:
            pred = predictions.get(oid, {}).get(method)
            pred = normalize_label(pred) if pred is not None else None
            if pred is None:
                abstain += 1; outcome = "abstain"
            elif pred == truth:
                correct += 1; outcome = "correct"
            else:
                incorrect += 1; outcome = "incorrect"
            comparisons.append({"observation_id": oid, "truth": truth, "prediction": pred, "outcome": outcome})
        covered = correct + incorrect
        result[method] = {"n": len(rows), "correct": correct, "incorrect": incorrect,
                          "abstention": abstain, "coverage": covered / len(rows) if rows else None,
                          "accuracy_on_covered": correct / covered if covered else None,
                          "correct_over_all_eligible": correct / len(rows) if rows else None,
                          "comparisons": comparisons}
    return result
