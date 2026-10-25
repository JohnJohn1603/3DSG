"""Evaluation helpers for paired object-association profile comparisons.

Track labels are treated as arbitrary partition labels.  The evaluator never
compares track ID strings across backends.
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Mapping


def partition_members(assignments: Mapping[str, str | None]) -> set[frozenset[str]]:
    """Return assigned observation membership groups, ignoring track names."""
    groups: dict[str, set[str]] = defaultdict(set)
    for observation_id, track_id in assignments.items():
        if track_id is not None:
            groups[str(track_id)].add(observation_id)
    return {frozenset(members) for members in groups.values()}


def pairwise_identity_metrics(
    assignments: Mapping[str, str | None],
    evaluator_ids: Mapping[str, str | int | None],
    *,
    eligible_ids: set[str] | None = None,
    cross_frame_only: bool = True,
) -> dict:
    """Score co-track relations on fixed evaluator-matched observations.

    A GT-same pair predicted apart, including when either prediction is
    deferred/unassigned, is an FN. GT-different pairs predicted together are
    FP. Unknown GT observations are excluded from the scored pair population
    and reported as coverage limits.
    """
    common = set(evaluator_ids) if eligible_ids is None else set(eligible_ids)
    scored = sorted(i for i in common if evaluator_ids.get(i) is not None)
    pair_population = 0
    tp = fp = fn = 0
    same_frame_pairs_excluded = 0
    for left, right in combinations(scored, 2):
        if cross_frame_only and left.split(":det-")[0] == right.split(":det-")[0]:
            same_frame_pairs_excluded += 1
            continue
        pair_population += 1
        gt_same = str(evaluator_ids[left]) == str(evaluator_ids[right])
        left_track, right_track = assignments.get(left), assignments.get(right)
        pred_same = left_track is not None and left_track == right_track
        if gt_same and pred_same:
            tp += 1
        elif not gt_same and pred_same:
            fp += 1
        elif gt_same:
            # Includes deferred, unassigned, and assigned to distinct tracks.
            fn += 1

    gt_to_tracks: dict[str, set[str]] = defaultdict(set)
    track_to_gt: dict[str, set[str]] = defaultdict(set)
    for observation_id in scored:
        track_id = assignments.get(observation_id)
        gt_id = str(evaluator_ids[observation_id])
        if track_id is not None:
            gt_to_tracks[gt_id].add(str(track_id))
            track_to_gt[str(track_id)].add(gt_id)
    fragmentation = {gt_id: max(0, len(tracks) - 1) for gt_id, tracks in gt_to_tracks.items()}
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else None)
    return {
        "eligible_observations": len(common),
        "evaluator_matched_observations_scored": len(scored),
        "observations_without_evaluator_identity": len(common) - len(scored),
        "cross_frame_pair_population": pair_population,
        "same_frame_pairs_excluded_from_cross_frame": same_frame_pairs_excluded,
        "pairwise_tp": tp, "pairwise_fp": fp, "pairwise_fn": fn,
        "association_precision": precision, "association_recall": recall,
        "association_f1": f1,
        "evaluator_instances_scored": len(gt_to_tracks),
        "fragmented_evaluator_instances": sum(n > 0 for n in fragmentation.values()),
        "fragmentation_excess_tracks": sum(fragmentation.values()),
        "false_merge_tracks": sum(len(ids) > 1 for ids in track_to_gt.values()),
        "predicted_tracks_touched_by_scored_observations": len(track_to_gt),
        "unassigned_scored_observations": sum(assignments.get(i) is None for i in scored),
        "fragmentation_by_evaluator_id": dict(sorted(fragmentation.items())),
        "identity_scope": "HM3D evaluator semantic instance IDs; posthoc identity proxy, not user-confirmed physical identity",
        "metric_contract": "unordered cross-frame pairs only; TP=same evaluator ID and same assigned track; FP=different evaluator IDs in same track; FN=same evaluator ID split/deferred/unassigned; unknown evaluator identity excluded",
    }


def relation_changes(source: Mapping[str, str | None], candidate: Mapping[str, str | None],
                     observation_ids: list[str]) -> list[dict]:
    """List pairwise partition transitions without relying on track IDs."""
    rows = []
    ids = sorted(observation_ids)
    for left, right in combinations(ids, 2):
        source_same = source.get(left) is not None and source.get(left) == source.get(right)
        candidate_same = candidate.get(left) is not None and candidate.get(left) == candidate.get(right)
        if source_same != candidate_same:
            rows.append({
                "observation_ids": [left, right],
                "source_relation": "same_track" if source_same else "different_or_unassigned",
                "candidate_relation": "same_track" if candidate_same else "different_or_unassigned",
                "transition": "different_to_same" if candidate_same else "same_to_different",
            })
    return rows


def partition_delta(source: Mapping[str, str | None], candidate: Mapping[str, str | None],
                    observation_ids: list[str] | None = None) -> dict:
    """Summarize semantic membership changes separately from arbitrary ID renames.

    A track ID is only a partition label.  Exact groups with identical
    observation membership are unchanged even if their labels differ.
    Pairwise relation changes capture merges, splits and assignment changes.
    """
    ids = sorted(set(source) | set(candidate) if observation_ids is None else set(observation_ids))
    source_groups = partition_members({oid: source.get(oid) for oid in ids})
    candidate_groups = partition_members({oid: candidate.get(oid) for oid in ids})

    def labels_by_membership(assignments: Mapping[str, str | None]) -> dict[frozenset[str], str]:
        grouped: dict[str, set[str]] = defaultdict(set)
        for oid in ids:
            track_id = assignments.get(oid)
            if track_id is not None:
                grouped[str(track_id)].add(oid)
        return {frozenset(members): track_id for track_id, members in grouped.items()}

    source_labels = labels_by_membership(source)
    candidate_labels = labels_by_membership(candidate)
    common_groups = source_groups & candidate_groups
    renamed_groups = [members for members in common_groups
                      if source_labels.get(members) != candidate_labels.get(members)]
    transitions = relation_changes(source, candidate, ids)
    return {
        "identity_relation_changes": len(transitions),
        "track_id_renamed_only_groups": len(renamed_groups),
        "unchanged_membership_groups": len(common_groups),
        "source_membership_groups_removed": len(source_groups - candidate_groups),
        "candidate_membership_groups_added": len(candidate_groups - source_groups),
        "relation_transitions": transitions,
    }
