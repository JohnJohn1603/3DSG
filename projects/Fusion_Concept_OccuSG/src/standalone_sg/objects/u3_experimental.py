"""Isolated, diagnostic U3 association backend.

This module is an experiment only. It does not mutate the scene graph or the
source ObjectAssociationManager. Geometry gates precede optional appearance;
neither distance nor cosine similarity can independently accept a match.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Literal, Mapping

import numpy as np
from .surface_support import surface_support


class RawClassEvidenceHistory:
    """Causal confidence-weighted raw detector class history for offline experiments."""

    def __init__(self) -> None:
        self._weights: dict[str, dict[str, float]] = {}

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {track_id: dict(hist) for track_id, hist in self._weights.items()}

    def commit_frame(self, observations: list["U3Observation"],
                     assigned_track_by_observation: Mapping[str, str | None], *,
                     alias_ids: set[str] | None = None) -> None:
        aliases = alias_ids or set()
        for obs in observations:
            if obs.detection_id in aliases:
                continue
            track_id = assigned_track_by_observation.get(obs.detection_id)
            if track_id is None or not obs.class_name:
                continue
            confidence = 0.0 if obs.class_confidence is None else min(1.0, max(0.0, float(obs.class_confidence)))
            if confidence == 0.0:
                continue
            track = self._weights.setdefault(track_id, {})
            track[obs.class_name] = track.get(obs.class_name, 0.0) + confidence


@dataclass(frozen=True)
class U3Observation:
    detection_id: str
    frame_id: str
    timestamp_s: float
    center: tuple[float, float, float]
    extents: tuple[float, float, float] | None
    layer_id: str
    class_name: str | None
    feature: tuple[float, ...] | None
    mask_hash: str
    class_confidence: float | None = None


@dataclass
class _Track:
    track_id: str
    center: tuple[float, float, float]
    extents: tuple[float, float, float] | None
    layer_id: str
    feature: tuple[float, ...] | None
    observations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssociationDecision:
    detection_id: str
    track_id: str | None
    decision: Literal["created", "matched", "defer", "duplicate_suppressed"]
    selected_candidate_score: float | None
    reason: str


def deduplicate_frame_masks(observations: list[U3Observation], masks: dict[str, np.ndarray],
                            *, iou_threshold: float = 0.98) -> tuple[list[U3Observation], list[dict]]:
    """Suppress exact/near duplicate same-frame masks before any strategy sees them."""
    kept: list[U3Observation] = []
    duplicates: list[dict] = []
    frames: dict[str, list[U3Observation]] = {}
    for obs in observations:
        frames.setdefault(obs.frame_id, []).append(obs)
    for frame_id in sorted(frames):
        accepted: list[U3Observation] = []
        for obs in sorted(frames[frame_id], key=lambda x: x.detection_id):
            duplicate_of = None
            for prior in accepted:
                lhs, rhs = masks[obs.detection_id], masks[prior.detection_id]
                if lhs.shape != rhs.shape:
                    continue
                union = int(np.logical_or(lhs, rhs).sum())
                overlap = float(np.logical_and(lhs, rhs).sum() / union) if union else 1.0
                if overlap >= iou_threshold:
                    duplicate_of = prior.detection_id
                    duplicates.append({"frame_id": frame_id, "detection_id": obs.detection_id,
                                       "duplicate_of": duplicate_of, "mask_iou": overlap,
                                       "decision": "duplicate_suppressed"})
                    break
            if duplicate_of is None:
                accepted.append(obs)
                kept.append(obs)
    return kept, duplicates


def exclude_same_frame_aliases(observations: list[U3Observation], aliases: Mapping[str, str]) -> list[U3Observation]:
    """Return canonical observations only; aliases stay in external raw lineage."""
    by_id = {obs.detection_id: obs for obs in observations}
    if len(by_id) != len(observations):
        raise ValueError("same-frame input contains duplicate observation IDs")
    for alias_id, canonical_id in aliases.items():
        if alias_id not in by_id or canonical_id not in by_id:
            raise ValueError(f"alias must reference observations in the current frame: {alias_id} -> {canonical_id}")
        if by_id[alias_id].frame_id != by_id[canonical_id].frame_id:
            raise ValueError(f"alias crosses frame: {alias_id} -> {canonical_id}")
    return [obs for obs in observations if obs.detection_id not in aliases]


class ExperimentalAssociation:
    """Deterministic greedy one-to-one association with a complete score ledger."""

    def __init__(self, mode: Literal["geometry", "geometry_appearance"], *,
                 max_center_m: float = 0.75, geom_min_iou: float = 0.01,
                 geom_only_min_iou: float = 0.05, appearance_min_cosine: float = 0.72,
                 combined_min_score: float = 0.55, ambiguity_margin: float = 0.05) -> None:
        self.mode = mode
        self.max_center_m = max_center_m
        self.geom_min_iou = geom_min_iou
        self.geom_only_min_iou = geom_only_min_iou
        self.appearance_min_cosine = appearance_min_cosine
        self.combined_min_score = combined_min_score
        self.ambiguity_margin = ambiguity_margin
        self.tracks: list[_Track] = []
        self.next_id = 1
        self.ledger: list[dict] = []
        self.decisions: list[AssociationDecision] = []

    @staticmethod
    def _iou(a: U3Observation, b: _Track) -> float | None:
        if a.extents is None or b.extents is None:
            return None
        alo = [a.center[i] - a.extents[i] / 2 for i in range(3)]
        ahi = [a.center[i] + a.extents[i] / 2 for i in range(3)]
        blo = [b.center[i] - b.extents[i] / 2 for i in range(3)]
        bhi = [b.center[i] + b.extents[i] / 2 for i in range(3)]
        inter = math.prod(max(0.0, min(ahi[i], bhi[i]) - max(alo[i], blo[i])) for i in range(3))
        va = math.prod(max(0.0, x) for x in a.extents)
        vb = math.prod(max(0.0, x) for x in b.extents)
        union = va + vb - inter
        return inter / union if union > 0 else 0.0

    @staticmethod
    def _cosine(a: tuple[float, ...] | None, b: tuple[float, ...] | None) -> float | None:
        if a is None or b is None or len(a) != len(b):
            return None
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else None

    @staticmethod
    def _size_score(a: U3Observation, b: _Track) -> float | None:
        if a.extents is None or b.extents is None or any(x <= 0 for x in (*a.extents, *b.extents)):
            return None
        ratios = [min(x, y) / max(x, y) for x, y in zip(a.extents, b.extents)]
        return sum(ratios) / len(ratios)

    def update_frame(self, observations: list[U3Observation], *,
                     surface_points_by_detection: Mapping[str, np.ndarray] | None = None,
                     surface_points_by_track: Mapping[str, np.ndarray] | None = None,
                     surface_config: Mapping[str, float] | None = None,
                     prior_geometry_by_track: Mapping[str, Mapping] | None = None,
                     prior_class_evidence_by_track: Mapping[str, Mapping[str, float]] | None = None,
                     class_soft_lambda: float = 0.0) -> list[AssociationDecision]:
        """Score all prior-track candidates, then make deterministic one-to-one choices."""
        if class_soft_lambda < 0 or not math.isfinite(class_soft_lambda):
            raise ValueError("class_soft_lambda must be finite and non-negative")
        if class_soft_lambda and prior_class_evidence_by_track is None:
            raise ValueError("soft class reranking requires a pre-frame class-evidence snapshot")
        if surface_config is not None and self.mode != "geometry":
            raise ValueError("surface continuity is supported only by geometry mode")
        if surface_config is not None and (surface_points_by_detection is None or surface_points_by_track is None):
            raise ValueError("surface config requires current observation and prior track point evidence")
        matcher_tracks: dict[str, _Track] = {}
        if prior_geometry_by_track is not None:
            track_ids = {track.track_id for track in self.tracks}
            if set(prior_geometry_by_track) != track_ids:
                raise ValueError("prior matcher geometry must cover exactly the pre-frame track snapshot")
            for track in self.tracks:
                geometry = prior_geometry_by_track[track.track_id]
                center = tuple(float(v) for v in geometry["center_W"])
                extents = tuple(float(v) for v in geometry["extents_m"])
                if len(center) != 3 or len(extents) != 3 or not all(math.isfinite(v) for v in (*center, *extents)):
                    raise ValueError(f"invalid prior matcher geometry for track {track.track_id}")
                matcher_tracks[track.track_id] = replace(track, center=center, extents=extents)
        before = len(self.decisions)
        candidate_rows: dict[str, list[dict]] = {}
        for obs in sorted(observations, key=lambda item: item.detection_id):
            rows = []
            for tr in self.tracks:
                match_geometry = matcher_tracks.get(tr.track_id, tr)
                row = {"mode": self.mode, "frame_id": obs.frame_id,
                       "detection_id": obs.detection_id, "track_id": tr.track_id,
                       "layer_pass": obs.layer_id == tr.layer_id,
                       "center_distance_m": math.dist(obs.center, match_geometry.center),
                       "aabb_iou": None, "extent_consistency": None,
                       "cosine": None, "gate_order": ["layer", "center_radius", "geometry", "appearance"],
                       "gate_failures": []}
                if class_soft_lambda:
                    prior_hist = prior_class_evidence_by_track.get(tr.track_id, {})
                    prior_total = sum(max(0.0, float(v)) for v in prior_hist.values())
                    raw_class = obs.class_name
                    confidence = (0.0 if obs.class_confidence is None else
                                  min(1.0, max(0.0, float(obs.class_confidence))))
                    agreement = (max(0.0, float(prior_hist.get(raw_class, 0.0))) / prior_total
                                 if raw_class is not None and prior_total > 0 else None)
                    delta = (class_soft_lambda * confidence * (2.0 * agreement - 1.0)
                             if agreement is not None else 0.0)
                    row.update({"raw_class": raw_class, "raw_class_confidence": confidence,
                                "prior_class_evidence": dict(prior_hist),
                                "raw_class_agreement": agreement,
                                "class_soft_delta": delta})
                if prior_geometry_by_track is not None:
                    prior = prior_geometry_by_track[tr.track_id]
                    row.update({"matcher_geometry_source": prior["geometry_source"],
                                "prior_geometry_revision": int(prior["prior_revision"]),
                                "prior_geometry_evidence_ids": list(prior["evidence_ids"]),
                                "prior_matcher_center_W": list(match_geometry.center),
                                "prior_matcher_extents_m": list(match_geometry.extents)})
                if not row["layer_pass"]:
                    row["gate_failures"].append("layer_mismatch")
                if row["center_distance_m"] > self.max_center_m:
                    row["gate_failures"].append("center_radius")
                # Do not calculate geometry or appearance when the coarse spatial
                # gate fails. This also keeps each stage's audit semantics explicit.
                if row["gate_failures"]:
                    row["geometry_score"] = None
                    row["appearance_score"] = None
                    row["combined_score"] = None
                    row["eligible"] = False
                    rows.append(row)
                    self.ledger.append(row)
                    continue
                row["aabb_iou"] = self._iou(obs, match_geometry)
                row["extent_consistency"] = self._size_score(obs, match_geometry)
                if row["aabb_iou"] is None:
                    row["gate_failures"].append("missing_extents")
                elif row["aabb_iou"] < self.geom_min_iou:
                    row["gate_failures"].append("no_3d_overlap")
                geom = 0.0
                if row["aabb_iou"] is not None and row["extent_consistency"] is not None:
                    center_score = max(0.0, 1.0 - row["center_distance_m"] / self.max_center_m)
                    geom = 0.50 * row["aabb_iou"] + 0.30 * center_score + 0.20 * row["extent_consistency"]
                row["geometry_score"] = geom
                row["appearance_score"] = None
                row["combined_score"] = None
                row["eligible"] = False
                if not row["gate_failures"]:
                    if self.mode == "geometry":
                        if row["aabb_iou"] < self.geom_only_min_iou:
                            row["gate_failures"].append("geometry_iou_below_acceptance")
                        else:
                            row["score"] = geom
                            row["eligible"] = True
                            row["association_branch"] = "original_geometry"
                    else:
                        # Appearance is evaluated only after layer, radius and 3D
                        # overlap gates have passed.
                        row["cosine"] = self._cosine(obs.feature, tr.feature)
                        row["appearance_score"] = row["cosine"]
                        if row["cosine"] is None:
                            row["gate_failures"].append("missing_appearance")
                        elif row["cosine"] < self.appearance_min_cosine:
                            row["gate_failures"].append("appearance_below_acceptance")
                        else:
                            row["combined_score"] = 0.55 * geom + 0.45 * row["cosine"]
                            row["score"] = row["combined_score"]
                            row["eligible"] = row["combined_score"] >= self.combined_min_score
                            if not row["eligible"]:
                                row["gate_failures"].append("combined_score_below_acceptance")
                if row["eligible"] and class_soft_lambda:
                    row["base_score"] = float(row["score"])
                    row["score"] = float(row["score"] + row.get("class_soft_delta", 0.0))
                    row["score_adjustment"] = "raw_class_soft_rerank"
                rows.append(row)
                self.ledger.append(row)
            candidate_rows[obs.detection_id] = rows

            # The original AABB policy remains authoritative whenever it has
            # at least one eligible prior track. Surface continuity is a
            # separate fallback branch only for the geometry gate failures.
            if surface_config is not None and not any(row.get("eligible") for row in rows):
                current_points = surface_points_by_detection.get(obs.detection_id)
                for row in rows:
                    if not row["layer_pass"] or row["center_distance_m"] > self.max_center_m:
                        continue
                    if row["aabb_iou"] is None or row["extent_consistency"] is None:
                        row["surface_branch_status"] = "unavailable_missing_geometry"
                        continue
                    if not set(row["gate_failures"]).issubset({
                            "geometry_iou_below_acceptance", "no_3d_overlap"}):
                        continue
                    prior_points = surface_points_by_track.get(row["track_id"])
                    row["surface_continuity_evaluated"] = True
                    if current_points is None or prior_points is None:
                        row["surface_branch_status"] = "unavailable_missing_point_evidence"
                        continue
                    support = surface_support(
                        current_points, prior_points,
                        voxel_size_m=float(surface_config["voxel_size_m"]),
                        radius_m=float(surface_config["support_radius_m"]))
                    row.update(support)
                    enough = (
                        support["forward_supported_voxels"] >= int(surface_config["minimum_supported_voxels"])
                        and support["forward_support_ratio"] >= float(surface_config["minimum_forward_support_ratio"])
                    )
                    obs_larger = (support["observation_surface_voxels"]
                                  > support["track_surface_voxels"] * float(surface_config["mixed_mask_size_ratio"])
                                  and support["reverse_support_ratio"]
                                  >= float(surface_config["mixed_mask_reverse_support_min"])
                                  and support["forward_support_ratio"]
                                  < float(surface_config["mixed_mask_forward_support_max"]))
                    if enough and obs_larger:
                        row["surface_branch_status"] = "ambiguous_possible_mixed_mask_expansion"
                    elif enough:
                        row["surface_branch_status"] = "eligible_surface_continuity"
                        row["association_branch"] = "surface_continuity"
                        row["score"] = float(support["forward_support_ratio"])
                        row["eligible"] = True
                    else:
                        row["surface_branch_status"] = "below_surface_support_minimum"

                support_ambiguous = any(
                    row.get("surface_branch_status") == "ambiguous_possible_mixed_mask_expansion"
                    for row in rows)
                if support_ambiguous:
                    for row in rows:
                        if row.get("surface_continuity_evaluated"):
                            row["decision"] = "defer_surface_ambiguous"
                            row["eligible"] = False
                    self.decisions.append(AssociationDecision(
                        obs.detection_id, None, "defer", None, "surface_support_possible_mixed_mask"))

        proposals: list[tuple[float, str, str]] = []
        for obs in observations:
            eligible = sorted((r for r in candidate_rows[obs.detection_id] if r["eligible"]),
                              key=lambda r: (-r["score"], r["track_id"]))
            margin = self.ambiguity_margin
            if (eligible and surface_config is not None
                    and eligible[0].get("association_branch") == "surface_continuity"):
                margin = float(surface_config["ambiguity_score_margin"])
            if eligible and len(eligible) > 1 and eligible[0]["score"] - eligible[1]["score"] < margin:
                for row in eligible:
                    row["decision"] = "defer_ambiguous"
                self.decisions.append(AssociationDecision(obs.detection_id, None, "defer", None, "ambiguous_candidates"))
            elif eligible:
                proposals.append((eligible[0]["score"], obs.detection_id, eligible[0]["track_id"]))

        used_tracks: set[str] = set()
        used_detections: set[str] = set()
        assignments: dict[str, str] = {}
        for score, detection_id, track_id in sorted(proposals, key=lambda x: (-x[0], x[1], x[2])):
            if track_id in used_tracks or detection_id in used_detections:
                self.decisions.append(AssociationDecision(detection_id, None, "defer", score, "one_to_one_conflict"))
                continue
            assignments[detection_id] = track_id
            used_tracks.add(track_id)
            used_detections.add(detection_id)

        by_id = {obs.detection_id: obs for obs in observations}
        for detection_id, track_id in assignments.items():
            obs = by_id[detection_id]
            tr = next(t for t in self.tracks if t.track_id == track_id)
            tr.center = tuple((x + y) / 2 for x, y in zip(tr.center, obs.center))
            if obs.extents is not None and tr.extents is not None:
                tr.extents = tuple((x + y) / 2 for x, y in zip(tr.extents, obs.extents))
            elif obs.extents is not None:
                tr.extents = obs.extents
            if obs.feature is not None:
                prev = tr.feature or obs.feature
                merged = tuple(x + y for x, y in zip(prev, obs.feature))
                norm = math.sqrt(sum(x * x for x in merged))
                tr.feature = tuple(x / norm for x in merged) if norm else obs.feature
            tr.observations.append(detection_id)
            selected = next(r for r in candidate_rows[detection_id] if r["track_id"] == track_id)
            score = selected["score"]
            reason = "surface_continuity" if selected.get("association_branch") == "surface_continuity" else "geometry_gated_one_to_one"
            self.decisions.append(AssociationDecision(detection_id, track_id, "matched", score, reason))

        for obs in observations:
            if obs.detection_id in assignments or any(d.detection_id == obs.detection_id for d in self.decisions[before:]):
                continue
            tr_id = f"u3-{self.next_id:04d}"
            self.next_id += 1
            self.tracks.append(_Track(tr_id, obs.center, obs.extents, obs.layer_id, obs.feature, [obs.detection_id]))
            self.decisions.append(AssociationDecision(obs.detection_id, tr_id, "created", None, "new_track"))
        return self.decisions[before:]
