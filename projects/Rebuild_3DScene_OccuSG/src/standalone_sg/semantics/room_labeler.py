"""P5 rule-based room labels from observed object evidence only.

Scores are additive evidence, not probabilities.  Room IDs and geometry are
never replaced by semantic labels; abstention is a first-class result.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from numbers import Real
from typing import Mapping, Sequence

from ..contracts.errors import ContractError
from ..pipeline.contracts import SceneGraphSnapshot
from .evidence import RegionEvidence


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RoomSemanticState:
    room_id: str
    region_id: str
    source_revision: int
    geometry_revision: int
    evidence_revision: int
    source_region_id: int | None
    label: str | None
    status: str
    candidates: tuple[tuple[str, float], ...]
    observed_object_ids: tuple[str, ...]
    eligible_object_ids: tuple[str, ...]
    supporting_object_ids: tuple[str, ...]
    class_counts: tuple[tuple[str, int], ...]
    semantic_signature: tuple[tuple[str, float, float], ...]
    evidence_hash: str
    score_kind: str
    method: str
    computed_timestamp_s: float | None
    last_evidence_timestamp_s: float | None
    stale_reason: str | None

    def as_dict(self) -> dict:
        return {
            "room_id": self.room_id, "source_revision": self.source_revision,
            "region_id": self.region_id, "geometry_revision": self.geometry_revision,
            "evidence_revision": self.evidence_revision,
            "source_region_id": self.source_region_id, "label": self.label,
            "status": self.status, "candidates": [[k, v] for k, v in self.candidates],
            "observed_object_ids": list(self.observed_object_ids),
            "eligible_object_ids": list(self.eligible_object_ids),
            "supporting_object_ids": list(self.supporting_object_ids),
            "class_counts": {k: v for k, v in self.class_counts},
            "semantic_signature": [{"class_name": c, "x": x, "y": y} for c, x, y in self.semantic_signature],
            "evidence_hash": self.evidence_hash, "score_kind": self.score_kind,
            "method": self.method, "computed_timestamp_s": self.computed_timestamp_s,
            "last_evidence_timestamp_s": self.last_evidence_timestamp_s,
            "stale_reason": self.stale_reason,
        }


class RuleBasedRoomLabeler:
    def __init__(self, config: Mapping[str, object]):
        if not isinstance(config, Mapping):
            raise ContractError("room semantic config must be a mapping")
        self.config = dict(config)
        allowed = {"schema_version", "profile_id", "status_values", "score_kind", "minimum_support_classes",
                   "minimum_top_score", "minimum_margin", "count_cap_per_class", "labels", "provenance",
                   "class_aliases", "detector_vocabulary_observed"}
        unknown = set(self.config) - allowed
        if unknown:
            raise ContractError(f"unknown room semantic config keys: {sorted(unknown)}")
        schema_version = self.config.get("schema_version", 1)
        if type(schema_version) is not int or schema_version < 1:
            raise ContractError("schema_version must be a positive integer")
        profile_id = self.config.get("profile_id", "room_rules_unversioned")
        if type(profile_id) is not str or not profile_id.strip():
            raise ContractError("profile_id must be a non-empty string")
        labels = self.config.get("labels")
        if not isinstance(labels, Mapping) or not labels:
            raise ContractError("room semantic config requires labels")
        self.labels: dict[str, dict[str, float]] = {}
        for label, weights in labels.items():
            if type(label) is not str or not label.strip() or not isinstance(weights, Mapping) or not weights:
                raise ContractError("each room label must have a non-empty string and class-weight mapping")
            normalized = {}
            for class_name, weight in weights.items():
                if type(class_name) is not str or not class_name.strip() or isinstance(weight, bool) or not isinstance(weight, Real):
                    raise ContractError("room rule class names must be strings and weights must be real numbers")
                number = float(weight)
                if not math.isfinite(number) or number <= 0:
                    raise ContractError("room rule weights must be finite and positive")
                normalized[class_name] = number
            self.labels[label] = normalized

        self.minimum_support_classes = self._positive_int("minimum_support_classes", 2)
        self.count_cap = self._positive_int("count_cap_per_class", 2)
        self.minimum_top_score = self._finite_nonnegative("minimum_top_score", 2.0)
        self.minimum_margin = self._finite_nonnegative("minimum_margin", 1.0)
        self.score_kind = self.config.get("score_kind", "uncalibrated_additive_evidence")
        if type(self.score_kind) is not str or not self.score_kind.strip():
            raise ContractError("score_kind must be a non-empty string")
        aliases = self.config.get("class_aliases", {})
        if not isinstance(aliases, Mapping):
            raise ContractError("class_aliases must be a mapping")
        self.class_aliases = {}
        supported_classes = {class_name for weights in self.labels.values() for class_name in weights}
        for raw_class, canonical_class in aliases.items():
            if (type(raw_class) is not str or not raw_class.strip() or type(canonical_class) is not str
                    or canonical_class not in supported_classes):
                raise ContractError("class_aliases must map non-empty strings to supported canonical classes")
            self.class_aliases[raw_class] = canonical_class
        status_values = self.config.get("status_values", ["unknown", "predicted", "ambiguous", "stale"])
        if (type(status_values) is not list or any(type(v) is not str for v in status_values)
                or len(status_values) != 4 or len(set(status_values)) != 4
                or set(status_values) != {"unknown", "predicted", "ambiguous", "stale"}):
            raise ContractError("status_values must list unknown/predicted/ambiguous/stale exactly once")
        provenance = self.config.get("provenance", {})
        if not isinstance(provenance, Mapping):
            raise ContractError("provenance must be a mapping")
        try:
            json.dumps(dict(provenance), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ContractError("provenance must contain finite JSON data") from exc
        vocabulary = self.config.get("detector_vocabulary_observed", [])
        if type(vocabulary) is not list or any(type(item) is not str or not item for item in vocabulary):
            raise ContractError("detector_vocabulary_observed must be a list of non-empty strings")
        self.profile_id = profile_id
        self._rule_fingerprint = _hash({
            "schema_version": schema_version, "profile_id": self.profile_id,
            "labels": self.labels,
            "class_aliases": self.class_aliases,
            "minimum_support_classes": self.minimum_support_classes,
            "minimum_top_score": self.minimum_top_score,
            "minimum_margin": self.minimum_margin,
            "count_cap_per_class": self.count_cap,
            "score_kind": self.score_kind,
        })

    def _positive_int(self, name: str, default: int) -> int:
        value = self.config.get(name, default)
        if type(value) is not int or value < 1:
            raise ContractError(f"{name} must be a positive integer")
        return value

    def _finite_nonnegative(self, name: str, default: float) -> float:
        value = self.config.get(name, default)
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ContractError(f"{name} must be a finite non-negative real number")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ContractError(f"{name} must be a finite non-negative real number")
        return value

    def label_regions(self, evidence: Sequence[RegionEvidence]) -> tuple[RoomSemanticState, ...]:
        """Pure P5 additive scoring over generic M5/M8 region evidence."""
        if any(not isinstance(item, RegionEvidence) for item in evidence):
            raise ContractError("label_regions requires RegionEvidence records")
        ids = [item.region_id for item in evidence]
        if len(ids) != len(set(ids)):
            raise ContractError("region evidence IDs must be unique within one revision")
        states = []
        supported_vocab = {cls for weights in self.labels.values() for cls in weights}
        for item in sorted(evidence, key=lambda x: x.region_id):
            unique_objects = tuple(sorted(item.objects, key=lambda obj: obj.object_id))
            class_counts: dict[str, int] = {}
            canonical_by_id = {}
            for obj in unique_objects:
                cls = None if obj.class_name is None else self.class_aliases.get(obj.class_name, obj.class_name)
                canonical_by_id[obj.object_id] = cls
                if cls is not None:
                    class_counts[cls] = min(self.count_cap, class_counts.get(cls, 0) + 1)
            eligible_ids = tuple(obj.object_id for obj in unique_objects if canonical_by_id[obj.object_id] in supported_vocab)
            scores = {
                label: sum(weights.get(cls, 0.0) * min(count, self.count_cap) for cls, count in class_counts.items())
                for label, weights in self.labels.items()
            }
            ranked = tuple(sorted(((label, float(score)) for label, score in scores.items()), key=lambda pair: (-pair[1], pair[0])))
            top_score = ranked[0][1] if ranked else 0.0
            second_score = ranked[1][1] if len(ranked) > 1 else 0.0
            top_label = ranked[0][0] if ranked else None
            top_weights = self.labels.get(top_label, {}) if top_label is not None else {}
            top_supported_classes = sum(1 for cls, count in class_counts.items() if count > 0 and top_weights.get(cls, 0.0) > 0.0)
            if item.freshness == "stale":
                status, label, reason = "stale", None, "source_region_geometry_stale"
            elif not unique_objects or not eligible_ids or top_score < self.minimum_top_score:
                status, label, reason = "unknown", None, "insufficient_supported_object_evidence"
            elif top_supported_classes < self.minimum_support_classes:
                status, label, reason = "unknown", None, "fewer_than_minimum_supported_classes_for_candidate"
            elif top_score - second_score < self.minimum_margin:
                status, label, reason = "ambiguous", None, "top_two_evidence_scores_too_close"
            else:
                status, label, reason = "predicted", top_label, None
            supporting_ids = tuple(obj.object_id for obj in unique_objects if label is not None
                                   and canonical_by_id[obj.object_id] in self.labels[label])
            signature = tuple(sorted({(canonical_by_id[obj.object_id], round(obj.position_xy_m[0], 1), round(obj.position_xy_m[1], 1))
                                      for obj in unique_objects if canonical_by_id[obj.object_id] is not None
                                      and obj.position_xy_m is not None}))
            evidence_payload = {
                "region_id": item.region_id, "source_region_id": item.source_region_id,
                "polygon": None if item.polygon is None else {"outer": item.polygon.outer, "holes": item.polygon.holes},
                "objects": [obj.as_dict() for obj in unique_objects],
                "class_counts": class_counts, "semantic_signature": signature,
                "rule_fingerprint": self._rule_fingerprint,
            }
            evidence_hash = _hash(evidence_payload)
            last_evidence = max((obj.last_seen_s for obj in unique_objects if obj.last_seen_s is not None), default=None)
            states.append(RoomSemanticState(
                room_id=item.source_room_id or item.region_id, region_id=item.region_id,
                source_revision=item.evidence_revision, geometry_revision=item.geometry_revision,
                evidence_revision=item.evidence_revision, source_region_id=item.source_region_id,
                label=label, status=status, candidates=ranked,
                observed_object_ids=tuple(obj.object_id for obj in unique_objects),
                eligible_object_ids=eligible_ids, supporting_object_ids=supporting_ids,
                class_counts=tuple(sorted(class_counts.items())), semantic_signature=signature,
                evidence_hash=evidence_hash, score_kind=self.score_kind,
                method=f"project_extension:rule_based_region_object_evidence_v2:{self.profile_id}",
                computed_timestamp_s=item.processing_timestamp_s,
                last_evidence_timestamp_s=last_evidence, stale_reason=reason,
            ))
        return tuple(states)

    def label_snapshot(self, snapshot: SceneGraphSnapshot) -> tuple[RoomSemanticState, ...]:
        """Compatibility adapter preserving the existing M8 room_contains input."""
        from .adapters import evidence_from_m8_snapshot

        evidence = evidence_from_m8_snapshot(snapshot)
        return tuple(replace(state, method="P5:rule_based_object_evidence_v1",
                             stale_reason="source_room_geometry_stale" if state.status == "stale" else state.stale_reason)
                     for state in self.label_regions(evidence))
