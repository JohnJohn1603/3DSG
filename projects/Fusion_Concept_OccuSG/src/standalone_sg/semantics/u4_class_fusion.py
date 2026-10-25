"""Offline, causal, view-capped class evidence for locked U3 assignments.

This module does not perform object association. Callers provide the immutable
track assignment for each observation and replay observations in time order.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Iterable


@dataclass(frozen=True)
class ClassObservation:
    observation_id: str
    frame_index: int
    timestamp_s: float
    raw_class: str | None
    confidence: float
    view_bin: str


def quantize_view_bin(camera_xyz: Iterable[float], object_xyz: Iterable[float], *,
                      azimuth_bin_deg: int = 30, elevation_bin_deg: int = 30) -> str:
    """Quantize camera-to-object viewing direction into yaw/elevation bins."""
    from math import atan2, degrees, hypot

    camera = tuple(float(x) for x in camera_xyz)
    obj = tuple(float(x) for x in object_xyz)
    if len(camera) != 3 or len(obj) != 3:
        raise ValueError("camera_xyz and object_xyz must be 3-vectors")
    dx, dy, dz = camera[0] - obj[0], camera[1] - obj[1], camera[2] - obj[2]
    horizontal = hypot(dx, dy)
    if horizontal < 1e-6 and abs(dz) < 1e-6:
        return "unknown"
    az = (degrees(atan2(dy, dx)) + 360.0) % 360.0
    el = degrees(atan2(dz, max(horizontal, 1e-6)))
    az_bin = int(floor(az / azimuth_bin_deg)) % (360 // azimuth_bin_deg)
    el_bin = int(floor((el + 90.0) / elevation_bin_deg))
    return f"az{az_bin:02d}_el{el_bin:02d}"


class CausalClassEvidence:
    """Track-local evidence accumulator; only past/current rows are accepted."""

    def __init__(self, *, min_independent_views: int = 2,
                 minimum_score_margin: float = 0.25,
                 minimum_winner_fraction: float = 0.67):
        if min_independent_views < 2:
            raise ValueError("min_independent_views must be at least two")
        if not 0.0 < minimum_winner_fraction <= 1.0:
            raise ValueError("minimum_winner_fraction must be in (0, 1]")
        self.min_independent_views = int(min_independent_views)
        self.minimum_score_margin = float(minimum_score_margin)
        self.minimum_winner_fraction = float(minimum_winner_fraction)
        self.observations: list[ClassObservation] = []
        self.initial_class: str | None = None
        self.resolved_class: str | None = None
        self.class_status = "tentative"
        self.events: list[dict] = []
        self.first_timestamp_s: float | None = None

    def _view_winners(self) -> dict[str, ClassObservation]:
        # One observation per angular bin contributes. Repeated frames, masks,
        # or near-identical views cannot add unbounded votes.
        best: dict[str, ClassObservation] = {}
        for row in self.observations:
            if not row.raw_class:
                continue
            prior = best.get(row.view_bin)
            if (prior is None or row.confidence > prior.confidence
                    or (row.confidence == prior.confidence
                        and (row.frame_index, row.observation_id) < (prior.frame_index, prior.observation_id))):
                best[row.view_bin] = row
        return best

    def _scores(self) -> tuple[dict[str, float], dict[str, int], dict[str, list[str]]]:
        scores: dict[str, float] = {}
        bins: dict[str, int] = {}
        used: dict[str, list[str]] = {}
        for view_bin, row in self._view_winners().items():
            label = str(row.raw_class)
            scores[label] = scores.get(label, 0.0) + max(0.0, min(1.0, row.confidence))
            bins[label] = bins.get(label, 0) + 1
            used.setdefault(label, []).append(view_bin)
        return scores, bins, used

    def _winner(self) -> tuple[str | None, dict[str, float], dict[str, int], dict[str, list[str]], float, float]:
        scores, bins, used = self._scores()
        ordered = sorted(scores, key=lambda label: (-scores[label], label))
        if not ordered:
            return None, scores, bins, used, 0.0, 0.0
        top = ordered[0]
        second_score = scores[ordered[1]] if len(ordered) > 1 else 0.0
        total = sum(scores.values())
        margin = scores[top] - second_score
        fraction = scores[top] / total if total else 0.0
        qualifies = (bins.get(top, 0) >= self.min_independent_views
                     and margin >= self.minimum_score_margin
                     and fraction >= self.minimum_winner_fraction)
        return (top if qualifies else None), scores, bins, used, margin, fraction

    def add(self, row: ClassObservation) -> dict:
        if self.observations and (row.frame_index, row.timestamp_s, row.observation_id) < (
                self.observations[-1].frame_index,
                self.observations[-1].timestamp_s,
                self.observations[-1].observation_id):
            raise ValueError("observations must be provided in causal order")
        if not 0.0 <= row.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.initial_class is None and row.raw_class:
            self.initial_class = row.raw_class
            self.first_timestamp_s = row.timestamp_s
            self.events.append({"event": "initial_class", "class": row.raw_class,
                                "observation_id": row.observation_id,
                                "frame_index": row.frame_index})
        self.observations.append(row)
        prior = self.resolved_class
        candidate, scores, bins, used, margin, fraction = self._winner()
        if candidate is not None:
            if prior and prior != candidate:
                self.events.append({"event": "deconfirmed", "class": prior,
                                    "observation_id": row.observation_id,
                                    "frame_index": row.frame_index,
                                    "reason": "new independent-view evidence changed the confirmed winner"})
                self.events.append({"event": "class_correction", "from": prior, "to": candidate,
                                    "observation_id": row.observation_id,
                                    "frame_index": row.frame_index})
            elif prior is None and self.initial_class is not None and self.initial_class != candidate:
                self.events.append({"event": "class_correction", "from": self.initial_class, "to": candidate,
                                    "observation_id": row.observation_id,
                                    "frame_index": row.frame_index})
            if prior != candidate:
                self.events.append({"event": "confirmed", "class": candidate,
                                    "observation_id": row.observation_id,
                                    "frame_index": row.frame_index,
                                    "supporting_view_bins": sorted(used.get(candidate, []))})
            self.resolved_class = candidate
            self.class_status = "confirmed"
        elif prior is not None:
            # Once two independent views support an alternative, withdraw the
            # old confirmation if the evidence no longer meets its own margin.
            rival_bins = max((n for label, n in bins.items() if label != prior), default=0)
            old_winner = next((label for label, value in scores.items()
                               if label == prior and bins.get(label, 0) >= self.min_independent_views), None)
            old_still_clear = False
            if old_winner:
                old_score = scores[prior]
                best_rival = max((v for k, v in scores.items() if k != prior), default=0.0)
                total = sum(scores.values())
                old_still_clear = (old_score - best_rival >= self.minimum_score_margin
                                   and (old_score / total if total else 0.0) >= self.minimum_winner_fraction)
            if rival_bins >= self.min_independent_views and not old_still_clear:
                self.events.append({"event": "deconfirmed", "class": prior,
                                    "observation_id": row.observation_id,
                                    "frame_index": row.frame_index,
                                    "reason": "confirmed class lost evidence margin to independent views"})
                self.resolved_class = None
                self.class_status = "conflicted"
            else:
                self.class_status = "confirmed"
        else:
            self.class_status = "conflicted" if len([x for x, n in bins.items() if n]) > 1 else "tentative"
        return {
            "observation_id": row.observation_id,
            "frame_index": row.frame_index,
            "raw_class": row.raw_class,
            "initial_class": self.initial_class,
            "resolved_class": self.resolved_class,
            "class_status": self.class_status,
            "candidate_class": candidate,
            "score_by_class": dict(sorted(scores.items())),
            "independent_view_bins_by_class": dict(sorted(bins.items())),
            "winning_margin": margin,
            "winning_fraction": fraction,
            "event_count": len(self.events),
        }

    def summary(self) -> dict:
        scores, bins, used = self._scores()
        first = self.observations[0] if self.observations else None
        last = self.observations[-1] if self.observations else None
        first_confirm = next((e for e in self.events if e["event"] == "confirmed"), None)
        confirm_row = None
        if first_confirm:
            confirm_row = next((r for r in self.observations
                                if r.observation_id == first_confirm["observation_id"]), None)
        return {
            "initial_class": self.initial_class,
            "resolved_class": self.resolved_class,
            "class_status": self.class_status,
            "observation_count": len(self.observations),
            "distinct_view_bins": len(self._view_winners()),
            "evidence_score_by_class": dict(sorted(scores.items())),
            "independent_view_bins_by_class": dict(sorted(bins.items())),
            "confirmation_frame_index": None if confirm_row is None else confirm_row.frame_index,
            "confirmation_timestamp_s": None if confirm_row is None else confirm_row.timestamp_s,
            "confirmation_delay_s": (None if confirm_row is None or first is None
                                      else confirm_row.timestamp_s - first.timestamp_s),
            "confirmation_view_bins": (None if not first_confirm
                                       else first_confirm.get("supporting_view_bins", [])),
            "last_frame_index": None if last is None else last.frame_index,
            "event_count": len(self.events),
        }
