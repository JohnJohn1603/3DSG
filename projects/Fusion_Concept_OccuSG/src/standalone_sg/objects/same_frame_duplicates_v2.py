"""Offline U3 duplicate candidate v2 with a containment ambiguity guard.

V1 remains unchanged. This module keeps all original observations and
suppresses only under the locked v2 rule; it does not merge graph nodes.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from standalone_sg.objects.same_frame_duplicates import _canonical_rank, _pair_evidence
from standalone_sg.objects.u3_experimental import U3Observation


def canonicalize_same_frame_duplicates_v2(
    observations: list[U3Observation],
    masks: dict[str, np.ndarray],
    support: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[U3Observation], list[dict[str, Any]], dict[str, Any]]:
    """Canonicalize safe same-frame pairs and preserve uncertain pairs.

    Containment expansion over the frozen area-ratio cap is ambiguous, never
    a negative-identity claim. Missing support/geometry on an otherwise
    overlapping pair is also ambiguous. Only same-frame evidence is read.
    """
    frames: dict[str, list[U3Observation]] = defaultdict(list)
    for obs in observations:
        if obs.detection_id not in masks:
            raise ValueError(f"missing mask for {obs.detection_id}")
        frames[obs.frame_id].append(obs)

    duplicate_cfg = config["duplicate_evidence"]
    guard_cfg = config["containment_guard"]
    if not guard_cfg.get("enabled", False):
        raise ValueError("v2 containment guard must be enabled")
    area_ratio_limit = float(guard_cfg["max_larger_to_smaller_mask_area_ratio"])
    if not np.isfinite(area_ratio_limit) or area_ratio_limit < 1.0:
        raise ValueError("containment area ratio limit must be finite and >= 1")

    canonical: list[U3Observation] = []
    aliases: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for frame_id, frame_obs in sorted(frames.items()):
        ordered = sorted(frame_obs, key=lambda o: _canonical_rank(o, masks, support))
        by_id = {o.detection_id: o for o in ordered}
        frame_pair_rows: dict[frozenset[str], dict[str, Any]] = {}
        ambiguous_ids: set[str] = set()

        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                row = _pair_evidence(left, right, masks, support, config)
                area_left, area_right = row["mask_area_pixels"]
                area_ratio = (max(area_left, area_right) / min(area_left, area_right)
                              if min(area_left, area_right) > 0 else None)
                iou_threshold = float(duplicate_cfg["mask_iou_min_or_containment_min"])
                containment_threshold = float(duplicate_cfg["mask_containment_min"])
                containment_branch = (
                    row["mask_iou"] is not None
                    and row["mask_iou"] < iou_threshold
                    and row["mask_containment_min_area"] >= containment_threshold
                )
                overlap_branch = (
                    row["mask_iou"] is not None
                    and (row["mask_iou"] >= iou_threshold or containment_branch)
                )
                failures = set(row["gate_failures"])
                support_missing = (
                    "insufficient_valid_depth_or_cluster_support" in failures
                )
                geometry_missing = (
                    "missing_3d_geometry" in failures
                    or row["aabb_iou_3d"] is None
                    or row["extent_consistency"] is None
                )

                guard_reason = None
                diagnostic_reason = None
                if overlap_branch and support_missing:
                    guard_reason = "missing_or_insufficient_depth_point_support"
                elif overlap_branch and geometry_missing:
                    guard_reason = "missing_or_insufficient_3d_geometry"
                elif overlap_branch and containment_branch and area_ratio is not None and area_ratio > area_ratio_limit:
                    # A large mask expansion is ambiguous only when the
                    # available 3D evidence does not already disconfirm a
                    # duplicate. Clear 3D mismatch keeps the pair separate.
                    geometry_disconfirms = bool(
                        failures.intersection({
                            "center_distance", "aabb_iou_3d", "extent_consistency"
                        })
                    )
                    if geometry_disconfirms:
                        diagnostic_reason = "clear_3d_geometry_mismatch"
                    else:
                        guard_reason = "containment_area_expansion_over_limit"

                eligible = bool(row["eligible_duplicate"] and guard_reason is None)
                ambiguous = bool(guard_reason is not None)
                row.update({
                    "containment_branch": containment_branch,
                    "larger_to_smaller_mask_area_ratio": area_ratio,
                    "v2_guard_disposition": "ambiguous_keep_both_no_alias" if ambiguous
                                            else "canonicalize" if eligible else "keep_separate",
                    "v2_guard_reason": guard_reason or diagnostic_reason,
                    "eligible_duplicate": eligible,
                    "ambiguous_duplicate_decision": ambiguous,
                })
                key = frozenset((left.detection_id, right.detection_id))
                frame_pair_rows[key] = row
                pair_rows.append(row)
                if ambiguous:
                    ambiguous_ids.update((left.detection_id, right.detection_id))

        frame_canonical: list[U3Observation] = []
        frame_aliases: list[dict[str, Any]] = []
        frame_decisions: list[dict[str, Any]] = []
        for obs in ordered:
            related = []
            eligible = []
            ambiguous = []
            for representative in frame_canonical:
                evidence = frame_pair_rows[frozenset((obs.detection_id, representative.detection_id))]
                related.append((representative, evidence))
                if evidence["eligible_duplicate"]:
                    eligible.append(representative)
                if evidence["ambiguous_duplicate_decision"]:
                    ambiguous.append(representative)

            # Any ambiguous pair involving this observation blocks suppression,
            # including when there is another otherwise eligible canonical.
            if obs.detection_id in ambiguous_ids or ambiguous:
                frame_canonical.append(obs)
                frame_decisions.append({
                    "frame_id": frame_id, "detection_id": obs.detection_id,
                    "decision": "kept_ambiguous", "duplicate_of": None,
                    "candidate_canonicals": sorted(r.detection_id for r in eligible + ambiguous),
                    "reason": "containment_guard_or_missing_evidence",
                })
            elif len(eligible) == 1 and eligible[0].detection_id not in ambiguous_ids:
                rep = eligible[0]
                frame_aliases.append({
                    "frame_id": frame_id, "detection_id": obs.detection_id,
                    "duplicate_of": rep.detection_id, "canonical_observation_id": rep.detection_id,
                    "decision": "duplicate_alias_suppressed_from_association",
                    "counts_as_view": False, "semantic_vote_count": 0,
                })
                frame_decisions.append({
                    "frame_id": frame_id, "detection_id": obs.detection_id,
                    "decision": "alias_suppressed", "duplicate_of": rep.detection_id,
                    "reason": "unique_canonical_passed_existing_gates_and_v2_guard",
                })
            elif len(eligible) > 1:
                frame_canonical.append(obs)
                frame_decisions.append({
                    "frame_id": frame_id, "detection_id": obs.detection_id,
                    "decision": "kept_ambiguous", "duplicate_of": None,
                    "candidate_canonicals": sorted(r.detection_id for r in eligible),
                    "reason": "multiple_canonicals_passed_duplicate_gates",
                })
            else:
                frame_canonical.append(obs)
                frame_decisions.append({
                    "frame_id": frame_id, "detection_id": obs.detection_id,
                    "decision": "kept_canonical", "duplicate_of": None,
                    "reason": "no_unique_duplicate_evidence",
                })

        canonical.extend(frame_canonical)
        aliases.extend(frame_aliases)
        decisions.extend(frame_decisions)

    return canonical, aliases, {
        "pair_evidence": pair_rows,
        "observation_decisions": decisions,
        "ambiguous_observation_ids": sorted({
            row["detection_id"] for row in decisions if row["decision"] == "kept_ambiguous"
        }),
    }
