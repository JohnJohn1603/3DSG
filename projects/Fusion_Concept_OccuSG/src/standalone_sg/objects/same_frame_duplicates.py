"""Conservative, offline same-frame duplicate canonicalization candidate.

The module keeps the original observations and emits aliases as sidecar
decisions. It does not merge tracks, delete ObjectNodes, or inspect labels/GT.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from standalone_sg.objects.u3_experimental import U3Observation


def _aabb_iou(a: U3Observation, b: U3Observation) -> float | None:
    if a.extents is None or b.extents is None:
        return None
    alo = [a.center[i] - a.extents[i] / 2 for i in range(3)]
    ahi = [a.center[i] + a.extents[i] / 2 for i in range(3)]
    blo = [b.center[i] - b.extents[i] / 2 for i in range(3)]
    bhi = [b.center[i] + b.extents[i] / 2 for i in range(3)]
    intersection = math.prod(max(0.0, min(ahi[i], bhi[i]) - max(alo[i], blo[i])) for i in range(3))
    va = math.prod(max(0.0, x) for x in a.extents)
    vb = math.prod(max(0.0, x) for x in b.extents)
    union = va + vb - intersection
    return intersection / union if union > 0 else 0.0


def _extent_consistency(a: U3Observation, b: U3Observation) -> float | None:
    if a.extents is None or b.extents is None or any(x <= 0 for x in (*a.extents, *b.extents)):
        return None
    return sum(min(x, y) / max(x, y) for x, y in zip(a.extents, b.extents)) / 3.0


def _pair_evidence(a: U3Observation, b: U3Observation, masks: dict[str, np.ndarray],
                   support: dict[str, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    ga = np.asarray(masks[a.detection_id], dtype=bool)
    gb = np.asarray(masks[b.detection_id], dtype=bool)
    area_a, area_b = int(ga.sum()), int(gb.sum())
    same_shape = ga.shape == gb.shape
    intersection = int(np.logical_and(ga, gb).sum()) if same_shape else 0
    union = int(np.logical_or(ga, gb).sum()) if same_shape else 0
    iou = (intersection / union if union else (1.0 if area_a == area_b == 0 else 0.0)) if same_shape else None
    contain_a = intersection / area_a if same_shape and area_a else 0.0
    contain_b = intersection / area_b if same_shape and area_b else 0.0
    containment_min_area = intersection / min(area_a, area_b) if same_shape and min(area_a, area_b) else 0.0
    sa, sb = support.get(a.detection_id, {}), support.get(b.detection_id, {})
    valid_a, valid_b = int(sa.get("valid_depth_pixels", 0)), int(sb.get("valid_depth_pixels", 0))
    points_a, points_b = int(sa.get("cluster_points", 0)), int(sb.get("cluster_points", 0))
    depth_fraction_a = valid_a / area_a if area_a else 0.0
    depth_fraction_b = valid_b / area_b if area_b else 0.0
    center_distance = math.dist(a.center, b.center)
    box_iou = _aabb_iou(a, b)
    extent_score = _extent_consistency(a, b)
    cfg = config["duplicate_evidence"]
    mask_pass = same_shape and (
        (iou is not None and iou >= float(cfg["mask_iou_min_or_containment_min"]))
        or containment_min_area >= float(cfg["mask_containment_min"])
    )
    depth_pass = all((
        valid >= int(cfg["valid_depth_pixels_min_each"])
        and fraction >= float(cfg["valid_depth_fraction_min_each"])
        and points >= int(cfg["cluster_points_min_each"])
    ) for valid, fraction, points in ((valid_a, depth_fraction_a, points_a), (valid_b, depth_fraction_b, points_b)))
    layer_pass = a.layer_id == b.layer_id
    geometry_pass = (
        a.extents is not None and b.extents is not None
        and center_distance <= float(cfg["center_distance_m_max"])
        and box_iou is not None and box_iou >= float(cfg["aabb_iou_3d_min"])
        and extent_score is not None and extent_score >= float(cfg["extent_consistency_min"])
    )
    failures = []
    if not same_shape:
        failures.append("mask_shape_mismatch")
    if not mask_pass:
        failures.append("insufficient_2d_mask_overlap")
    if not depth_pass:
        failures.append("insufficient_valid_depth_or_cluster_support")
    if not layer_pass:
        failures.append("layer_mismatch")
    if a.extents is None or b.extents is None:
        failures.append("missing_3d_geometry")
    elif center_distance > float(cfg["center_distance_m_max"]):
        failures.append("center_distance")
    if box_iou is None or box_iou < float(cfg["aabb_iou_3d_min"]):
        failures.append("3d_aabb_iou")
    if extent_score is None or extent_score < float(cfg["extent_consistency_min"]):
        failures.append("extent_consistency")
    return {
        "left_detection_id": a.detection_id, "right_detection_id": b.detection_id,
        "frame_id": a.frame_id, "raw_classes": [a.class_name, b.class_name],
        "mask_area_pixels": [area_a, area_b], "mask_intersection_pixels": intersection,
        "mask_union_pixels": union, "mask_iou": iou,
        "mask_containment_left_in_right": contain_a,
        "mask_containment_right_in_left": contain_b,
        "mask_containment_min_area": containment_min_area,
        "valid_depth_pixels": [valid_a, valid_b],
        "valid_depth_fraction_of_mask": [depth_fraction_a, depth_fraction_b],
        "selected_cluster_points": [points_a, points_b],
        "center_distance_m": center_distance, "aabb_iou_3d": box_iou,
        "extent_consistency": extent_score, "layer_pass": layer_pass,
        "raw_class_gate_used": False, "eligible_duplicate": not failures,
        "gate_failures": failures,
    }


def _canonical_rank(obs: U3Observation, masks: dict[str, np.ndarray],
                    support: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    row = support.get(obs.detection_id, {})
    return (-int(row.get("cluster_points", 0)), -int(row.get("valid_depth_pixels", 0)),
            -float(row.get("detector_score", 0.0)), -int(np.asarray(masks[obs.detection_id], dtype=bool).sum()),
            obs.detection_id)


def canonicalize_same_frame_duplicates(
    observations: list[U3Observation], masks: dict[str, np.ndarray],
    support: dict[str, dict[str, Any]], config: dict[str, Any],
) -> tuple[list[U3Observation], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return canonical inputs, observation aliases, and a complete pair ledger.

    A raw observation is suppressed only when it passes all 2D+depth+3D gates
    against exactly one previously selected canonical in its frame. If it
    qualifies against multiple canonicals, it is retained as ambiguous.
    Canonical ordering is quality-ranked with a stable detection-ID tie break.
    """
    frames: dict[str, list[U3Observation]] = {}
    for obs in observations:
        if obs.detection_id not in masks:
            raise ValueError(f"missing mask for {obs.detection_id}")
        frames.setdefault(obs.frame_id, []).append(obs)
    canonical: list[U3Observation] = []
    aliases: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    pair_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for frame_id, frame_obs in sorted(frames.items()):
        sorted_obs = sorted(frame_obs, key=lambda x: _canonical_rank(x, masks, support))
        for i, a in enumerate(sorted_obs):
            for b in sorted_obs[i + 1:]:
                pair_rows[tuple(sorted((a.detection_id, b.detection_id)))] = _pair_evidence(
                    a, b, masks, support, config)
        frame_canonical: list[U3Observation] = []
        for obs in sorted_obs:
            eligible = []
            for representative in frame_canonical:
                key = tuple(sorted((obs.detection_id, representative.detection_id)))
                evidence = pair_rows[key]
                if evidence["eligible_duplicate"]:
                    eligible.append(representative)
            if len(eligible) == 1:
                rep = eligible[0]
                aliases.append({"frame_id": frame_id, "detection_id": obs.detection_id,
                                "duplicate_of": rep.detection_id, "canonical_observation_id": rep.detection_id,
                                "decision": "duplicate_alias_suppressed_from_association",
                                "counts_as_view": False, "semantic_vote_count": 0})
                decisions.append({"frame_id": frame_id, "detection_id": obs.detection_id,
                                  "decision": "alias_suppressed", "duplicate_of": rep.detection_id,
                                  "reason": "unique_canonical_passed_all_2d_depth_3d_gates"})
            elif len(eligible) > 1:
                frame_canonical.append(obs)
                decisions.append({"frame_id": frame_id, "detection_id": obs.detection_id,
                                  "decision": "kept_ambiguous", "duplicate_of": None,
                                  "candidate_canonicals": sorted(x.detection_id for x in eligible),
                                  "reason": "multiple_canonicals_passed_duplicate_gates"})
            else:
                frame_canonical.append(obs)
                relevant = [r for r in pair_rows.values() if obs.detection_id in (r["left_detection_id"], r["right_detection_id"])]
                failures = sorted({failure for row in relevant for failure in row["gate_failures"]})
                decisions.append({"frame_id": frame_id, "detection_id": obs.detection_id,
                                  "decision": "kept_canonical", "duplicate_of": None,
                                  "reason": "no_unique_duplicate_evidence",
                                  "gate_failures": failures})
        canonical.extend(frame_canonical)
    pair_ledger = [pair_rows[k] for k in sorted(pair_rows)]
    return canonical, aliases, {"pair_evidence": pair_ledger, "observation_decisions": decisions}
