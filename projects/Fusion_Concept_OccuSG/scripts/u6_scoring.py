"""Pure scoring helpers for the locked U6 offline evaluator."""
from __future__ import annotations

from collections import Counter, defaultdict


def pairwise_association(rows: list[dict], *, excluded_frame_indices=()) -> dict:
    """Score cross-frame pair links over GT-matched accepted observations.

    Each input row represents a P3-accepted detection matched to an eligible GT
    instance. `track_id=None` represents deferred/unmatched association; such an
    observation remains in the denominator, so a same-GT pair becomes FN.
    Unmatched detections without a GT match are intentionally outside this
    conditional association population and must be reported separately.
    """
    excluded = set(excluded_frame_indices)
    used = [r for r in rows if r["frame_index"] not in excluded]
    tp = fp = fn = eligible = predicted_positive = gt_positive = 0
    deferred = sum(r.get("association_status") in {"deferred", "unmatched"} or r.get("track_id") is None for r in used)
    for i, a in enumerate(used):
        for b in used[i + 1:]:
            if a["frame_index"] == b["frame_index"]:
                continue
            eligible += 1
            same_gt = a["gt_identity"] == b["gt_identity"]
            track_a = None if a.get("association_status") in {"deferred", "unmatched"} else a.get("track_id")
            track_b = None if b.get("association_status") in {"deferred", "unmatched"} else b.get("track_id")
            same_track = track_a is not None and track_a == track_b
            if same_gt:
                gt_positive += 1
                if same_track:
                    tp += 1
                else:
                    fn += 1
            elif same_track:
                fp += 1
                predicted_positive += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else (0.0 if precision == 0 and recall == 0 else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1,
            "eligible_cross_frame_pairs": eligible, "ground_truth_positive_pairs": gt_positive,
            "predicted_positive_pairs": tp + fp, "deferred_or_unmatched_observations": deferred,
            "scored_observations": len(used), "excluded_frame_indices": sorted(excluded),
            "status": "available" if tp + fp + fn else "unavailable"}


def identity_structure(rows: list[dict], *, excluded_frame_indices=()) -> dict:
    """Return dataset-instance false merges and fragmentation; collisions separate."""
    excluded = set(excluded_frame_indices)
    used = [r for r in rows if r["frame_index"] not in excluded and r.get("track_id") is not None
            and r.get("association_status") not in {"deferred", "unmatched"}]
    track_to_ids = defaultdict(set); id_to_tracks = defaultdict(set)
    false_merges = defaultdict(set); fragments = defaultdict(set)
    for r in used:
        track = r["track_id"]; identity = r["gt_identity"]
        track_to_ids[track].add(identity); id_to_tracks[identity].add(track)
    for i, a in enumerate(used):
        for b in used[i + 1:]:
            if a["frame_index"] == b["frame_index"]: continue
            if a["track_id"] is not None and a["track_id"] == b["track_id"] and a["gt_identity"] != b["gt_identity"]:
                false_merges[a["track_id"]].update((a["gt_identity"], b["gt_identity"]))
            if a["gt_identity"] == b["gt_identity"] and a["track_id"] is not None and b["track_id"] is not None and a["track_id"] != b["track_id"]:
                fragments[a["gt_identity"]].update((a["track_id"], b["track_id"]))
    collisions = []
    groups = defaultdict(list)
    for r in used: groups[(r["frame_id"], r["track_id"])].append(r)
    for (frame_id, track_id), items in groups.items():
        if len(items) > 1:
            identities = sorted({str(r["gt_identity"]) for r in items})
            collisions.append({"frame_id": frame_id, "track_id": track_id,
                               "observation_ids": sorted(r["observation_id"] for r in items),
                               "evaluator_identity_count": len(identities),
                               "evaluator_identities": identities,
                               "physical_merge_status": "unreviewed",
                               "interpretation": "cannot call false merge until human identity review; could be duplicate observation"})
    return {"distinct_evaluator_instances": len(id_to_tracks),
            "false_merge_tracks_by_evaluator_id": {t: sorted(ids, key=str) for t, ids in false_merges.items()},
            "false_merge_track_count_by_evaluator_id": len(false_merges),
            "fragmented_instances_by_evaluator_id": {str(i): sorted(ts, key=str) for i, ts in fragments.items()},
            "fragmented_instance_count_by_evaluator_id": len(fragments),
            "same_frame_collision_count": sum(len(g) - 1 for g in groups.values() if len(g) > 1),
            "same_frame_collisions": collisions,
            "scored_observations": len(used), "excluded_frame_indices": sorted(excluded)}


def primary_diagnostic_relation(row: dict) -> dict:
    """Describe diagnostic status without changing/duplicating scoring membership."""
    in_assoc_population = bool(row.get("gt_matched") and row.get("p3_decision") == "accepted")
    return {"in_primary_gt_population": bool(row.get("gt_matched")),
            "in_primary_conditional_association_population": in_assoc_population,
            "diagnostic_is_additional_scoring_observation": False,
            "note": "Diagnostic review may show a row already scored automatically; it does not add a second metric count."}
