"""Experimental, model-independent contracts for incremental Room Belief v1.

This module deliberately does not load a model, infer aliases, alter object
association, or change the existing M8 membership resolver. Callers provide
canonical object categories, view-group IDs, retained status, room polygons,
and (later) the versioned category-to-room A table.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import copy
from numbers import Real
from typing import Mapping, Sequence

from ..contracts.errors import ContractError
from ..contracts.regions import Polygon2D
from ..rooms.geometry import point_in_polygon


def _hash(value: object) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("Room Belief payload must be finite JSON data") from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _nonempty(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")
    return value


def _unit(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ContractError(f"{name} must be a finite number in [0, 1]")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ContractError(f"{name} must be a finite number in [0, 1]")
    return result


def _json_mapping(value: Mapping[str, object], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ContractError(f"{name} must be a string-keyed mapping")
    try:
        return json.loads(json.dumps(dict(value), sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} must contain finite JSON data") from exc


@dataclass(frozen=True, slots=True)
class SemanticObservation:
    """Raw class and externally canonicalized category for one observation.

    ``view_group_id`` is supplied by a versioned upstream grouping policy.
    It is not inferred here and does not claim statistically independent views.
    Aliases, deferred observations, and filtered hypotheses cannot vote.
    """

    observation_id: str
    frame_id: str
    view_group_id: str
    raw_class: str | None
    category: str | None
    admission_status: str = "retained"
    alias_of: str | None = None

    def __post_init__(self) -> None:
        for name in ("observation_id", "frame_id", "view_group_id"):
            _nonempty(getattr(self, name), name)
        for name in ("raw_class", "category", "alias_of"):
            value = getattr(self, name)
            if value is not None:
                _nonempty(value, name)
        if self.admission_status not in ("retained", "filtered", "deferred"):
            raise ContractError("admission_status must be retained, filtered, or deferred")
        if self.alias_of == self.observation_id:
            raise ContractError("an observation cannot be an alias of itself")

    def as_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id, "frame_id": self.frame_id,
            "view_group_id": self.view_group_id, "raw_class": self.raw_class,
            "category": self.category, "admission_status": self.admission_status,
            "alias_of": self.alias_of,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SemanticObservation":
        allowed = {"observation_id", "frame_id", "view_group_id", "raw_class", "category", "admission_status", "alias_of"}
        if not isinstance(value, Mapping) or set(value) - allowed or not {"observation_id", "frame_id", "view_group_id"}.issubset(value):
            raise ContractError("invalid SemanticObservation serialization fields")
        return cls(**{key: value[key] for key in allowed if key in value})


@dataclass(frozen=True, slots=True)
class ConsensusPolicy:
    """Explicit semantic-consensus policy; values have no validated default."""

    policy_id: str
    view_group_policy_id: str
    minimum_view_groups: int
    minimum_top_share: float
    minimum_margin: float

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        _nonempty(self.view_group_policy_id, "view_group_policy_id")
        if type(self.minimum_view_groups) is not int or self.minimum_view_groups < 1:
            raise ContractError("minimum_view_groups must be a positive integer")
        object.__setattr__(self, "minimum_top_share", _unit(self.minimum_top_share, "minimum_top_share"))
        object.__setattr__(self, "minimum_margin", _unit(self.minimum_margin, "minimum_margin"))

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id, "view_group_policy_id": self.view_group_policy_id,
            "minimum_view_groups": self.minimum_view_groups,
            "minimum_top_share": self.minimum_top_share, "minimum_margin": self.minimum_margin,
        }


@dataclass(frozen=True, slots=True)
class ObjectSemanticConsensus:
    object_id: str
    semantic_revision: int
    status: str
    category: str | None
    view_group_count: int
    category_support: tuple[tuple[str, float], ...]
    raw_observations: tuple[SemanticObservation, ...]
    eligible_observation_ids: tuple[str, ...]
    excluded_observations: tuple[tuple[str, str], ...]
    policy_id: str

    def __post_init__(self) -> None:
        _nonempty(self.object_id, "object_id")
        if type(self.semantic_revision) is not int or self.semantic_revision < 0:
            raise ContractError("semantic_revision must be a non-negative integer")
        if self.status not in ("unknown", "provisional", "stable", "conflicted"):
            raise ContractError("unsupported object consensus status")
        if self.status == "stable" and self.category is None:
            raise ContractError("stable consensus requires a category")
        if self.category is not None:
            _nonempty(self.category, "category")
        if type(self.view_group_count) is not int or self.view_group_count < 0:
            raise ContractError("view_group_count must be a non-negative integer")
        support_names = [name for name, _ in self.category_support]
        if len(support_names) != len(set(support_names)):
            raise ContractError("category support names must be unique")
        for name, amount in self.category_support:
            _nonempty(name, "support category")
            if isinstance(amount, bool) or not isinstance(amount, Real) or not math.isfinite(float(amount)) or float(amount) < 0:
                raise ContractError("category support must contain finite non-negative values")
        if any(not isinstance(item, SemanticObservation) for item in self.raw_observations):
            raise ContractError("raw_observations must be SemanticObservation records")
        raw_ids = [item.observation_id for item in self.raw_observations]
        if len(raw_ids) != len(set(raw_ids)):
            raise ContractError("raw semantic observation IDs must be unique")
        if self.category is not None and self.category not in set(support_names):
            raise ContractError("consensus category must exist in its support ledger")
        if any(type(item) is not str or not item for item in self.eligible_observation_ids):
            raise ContractError("eligible observation IDs must be non-empty strings")
        if len(set(self.eligible_observation_ids)) != len(self.eligible_observation_ids):
            raise ContractError("eligible observation IDs must be unique")
        if any(not isinstance(item, (tuple, list)) or len(item) != 2
               or type(item[0]) is not str or not item[0] or type(item[1]) is not str or not item[1]
               for item in self.excluded_observations):
            raise ContractError("excluded observations must contain observation ID/reason pairs")
        excluded_ids = [item[0] for item in self.excluded_observations]
        if len(set(excluded_ids)) != len(excluded_ids):
            raise ContractError("excluded observation IDs must be unique")
        if set(raw_ids) != set(self.eligible_observation_ids) | set(excluded_ids):
            raise ContractError("raw semantic observations must be partitioned into eligible and excluded IDs")
        _nonempty(self.policy_id, "policy_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "object_id": self.object_id, "semantic_revision": self.semantic_revision,
            "status": self.status, "category": self.category,
            "view_group_count": self.view_group_count,
            "category_support": {key: value for key, value in self.category_support},
            "raw_observations": [item.as_dict() for item in self.raw_observations],
            "eligible_observation_ids": list(self.eligible_observation_ids),
            "excluded_observations": [{"observation_id": key, "reason": reason}
                                      for key, reason in self.excluded_observations],
            "policy_id": self.policy_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ObjectSemanticConsensus":
        allowed = {"object_id", "semantic_revision", "status", "category", "view_group_count",
                   "category_support", "raw_observations", "eligible_observation_ids",
                   "excluded_observations", "policy_id"}
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise ContractError("invalid ObjectSemanticConsensus serialization fields")
        raw = value["raw_observations"]
        eligible = value["eligible_observation_ids"]
        excluded = value["excluded_observations"]
        support = value["category_support"]
        if not isinstance(raw, (list, tuple)) or not isinstance(eligible, (list, tuple)) or not isinstance(excluded, (list, tuple)) or not isinstance(support, Mapping):
            raise ContractError("malformed ObjectSemanticConsensus lists/support")
        return cls(
            object_id=value["object_id"], semantic_revision=value["semantic_revision"],
            status=value["status"], category=value["category"], view_group_count=value["view_group_count"],
            category_support=tuple((key, value) for key, value in support.items()),
            raw_observations=tuple(SemanticObservation.from_dict(item) for item in raw),
            eligible_observation_ids=tuple(eligible),
            excluded_observations=tuple((item["observation_id"], item["reason"]) for item in excluded),
            policy_id=value["policy_id"],
        )


def aggregate_object_semantics(
    object_id: str,
    observations: Sequence[SemanticObservation],
    *,
    semantic_revision: int,
    policy: ConsensusPolicy,
) -> ObjectSemanticConsensus:
    """Aggregate at most one unit of class support per supplied view group.

    Distinct categories within a group split that group's unit support evenly.
    Detector confidence is intentionally not treated as a calibrated probability.
    """
    _nonempty(object_id, "object_id")
    if type(semantic_revision) is not int or semantic_revision < 0:
        raise ContractError("semantic_revision must be a non-negative integer")
    if not isinstance(policy, ConsensusPolicy):
        raise ContractError("a ConsensusPolicy must be supplied explicitly")
    if any(not isinstance(item, SemanticObservation) for item in observations):
        raise ContractError("observations must contain SemanticObservation records")
    ids = [item.observation_id for item in observations]
    if len(ids) != len(set(ids)):
        raise ContractError("observation IDs must be unique within one object aggregation")

    groups: dict[str, set[str]] = {}
    eligible_ids: list[str] = []
    excluded: list[tuple[str, str]] = []
    for item in observations:
        if item.admission_status != "retained":
            excluded.append((item.observation_id, item.admission_status))
        elif item.alias_of is not None:
            excluded.append((item.observation_id, "duplicate_alias"))
        elif item.category is None:
            excluded.append((item.observation_id, "category_unavailable"))
        else:
            groups.setdefault(item.view_group_id, set()).add(item.category)
            eligible_ids.append(item.observation_id)

    support: dict[str, float] = {}
    for categories in groups.values():
        share = 1.0 / len(categories)
        for category in categories:
            support[category] = support.get(category, 0.0) + share
    ranked = sorted(support.items(), key=lambda pair: (-pair[1], pair[0]))
    group_count = len(groups)
    total = float(group_count)
    if not ranked:
        status, category = "unknown", None
    else:
        top_name, top_count = ranked[0]
        second_count = ranked[1][1] if len(ranked) > 1 else 0.0
        top_share = top_count / total
        margin = (top_count - second_count) / total
        if group_count < policy.minimum_view_groups:
            status, category = "provisional", None
        elif (top_share >= policy.minimum_top_share and margin >= policy.minimum_margin):
            status, category = "stable", top_name
        elif len(ranked) > 1 and (top_share < policy.minimum_top_share or margin < policy.minimum_margin):
            status, category = "conflicted", None
        else:
            status, category = "provisional", None
    return ObjectSemanticConsensus(
        object_id=object_id, semantic_revision=semantic_revision, status=status, category=category,
        view_group_count=group_count, category_support=tuple((key, value) for key, value in ranked),
        raw_observations=tuple(observations),
        eligible_observation_ids=tuple(sorted(eligible_ids)),
        excluded_observations=tuple(sorted(excluded)), policy_id=policy.policy_id,
    )


@dataclass(frozen=True, slots=True)
class VoxelMembership:
    object_id: str
    room_id: str | None
    status: str
    reason: str
    voxel_size_m: float
    voxel_grid_origin_xyz_m: tuple[float, float, float]
    support_counts: tuple[tuple[str, int], ...]
    support_fraction: float
    runner_up_fraction: float
    outside_fraction: float
    tied: bool
    plausible_room_ids: tuple[str, ...]
    geometry_revisions: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "object_id": self.object_id, "room_id": self.room_id, "status": self.status,
            "reason": self.reason, "voxel_size_m": self.voxel_size_m,
            "voxel_grid_origin_xyz_m": list(self.voxel_grid_origin_xyz_m),
            "support_counts": {key: value for key, value in self.support_counts},
            "support_fraction": self.support_fraction, "runner_up_fraction": self.runner_up_fraction,
            "outside_fraction": self.outside_fraction, "tied": self.tied,
            "plausible_room_ids": list(self.plausible_room_ids),
            "geometry_revisions": {key: value for key, value in self.geometry_revisions},
        }


def greedy_voxel_support_membership(
    object_id: str,
    voxel_representatives_xyz_m: Sequence[Sequence[Real]],
    room_polygons: Mapping[str, Polygon2D],
    *,
    voxel_size_m: float,
    voxel_grid_origin_xyz_m: Sequence[Real],
    previous_room_id: str | None = None,
    geometry_revisions: Mapping[str, int] | None = None,
) -> VoxelMembership:
    """Assign by point support in existing metric XY room polygons.

    Inputs must already be canonical-world voxel representatives at the
    declared resolution and explicit grid origin. Room polygons and geometry are read-only. Boundary
    semantics reuse ``point_in_polygon``: outer boundaries are covered and
    strict hole interiors are excluded. This function does not alter M8 edges.
    """
    _nonempty(object_id, "object_id")
    if isinstance(voxel_size_m, bool) or not isinstance(voxel_size_m, Real) or not math.isfinite(float(voxel_size_m)) or float(voxel_size_m) <= 0:
        raise ContractError("voxel_size_m must be finite and positive")
    voxel_size_m = float(voxel_size_m)
    if isinstance(voxel_grid_origin_xyz_m, (str, bytes)) or len(voxel_grid_origin_xyz_m) != 3:
        raise ContractError("voxel_grid_origin_xyz_m must be an explicit XYZ triple")
    origin_values = []
    for axis, value in enumerate(voxel_grid_origin_xyz_m):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
            raise ContractError(f"voxel grid origin coordinate {axis} must be finite")
        origin_values.append(float(value))
    origin = tuple(origin_values)
    if not isinstance(room_polygons, Mapping) or any(type(key) is not str or not key for key in room_polygons):
        raise ContractError("room_polygons must map non-empty room IDs to Polygon2D")
    if any(not isinstance(poly, Polygon2D) for poly in room_polygons.values()):
        raise ContractError("room_polygons values must be Polygon2D")
    if previous_room_id is not None:
        _nonempty(previous_room_id, "previous_room_id")
    revisions = dict(geometry_revisions or {})
    if set(revisions) - set(room_polygons) or any(type(v) is not int or v < 0 for v in revisions.values()):
        raise ContractError("geometry revisions must be non-negative integers for supplied rooms")

    points: list[tuple[float, float, float]] = []
    keys: set[tuple[int, int, int]] = set()
    for index, point in enumerate(voxel_representatives_xyz_m):
        if isinstance(point, (str, bytes)) or len(point) != 3:
            raise ContractError(f"voxel representative {index} must be XYZ")
        xyz = []
        for axis, value in enumerate(point):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                raise ContractError(f"voxel representative {index} coordinate {axis} must be finite")
            xyz.append(float(value))
        point3 = tuple(xyz)
        key = tuple(math.floor((value - origin[axis]) / voxel_size_m) for axis, value in enumerate(point3))
        if key in keys:
            raise ContractError("voxel representatives contain duplicate voxel cells")
        keys.add(key)
        points.append(point3)

    if not points:
        return VoxelMembership(object_id, None, "unassigned", "zero_voxel_support", voxel_size_m, origin,
                               (), 0.0, 0.0, 0.0, False, (), ())
    if not room_polygons:
        return VoxelMembership(object_id, None, "unassigned", "room_geometry_unavailable", voxel_size_m, origin,
                               (), 0.0, 0.0, 0.0, False, (), ())

    counts = {room_id: 0 for room_id in room_polygons}
    outside = 0
    for x, y, _z in points:
        hits = [room_id for room_id, polygon in room_polygons.items() if point_in_polygon((x, y), polygon)]
        if not hits:
            outside += 1
        for room_id in hits:
            counts[room_id] += 1
    nonzero = sorted((room_id, count) for room_id, count in counts.items() if count > 0)
    if not nonzero:
        return VoxelMembership(object_id, None, "unassigned", "zero_room_support", voxel_size_m, origin,
                               tuple(sorted(counts.items())), 0.0, 0.0, outside / len(points), False, (), ())

    maximum = max(count for _, count in nonzero)
    tied_ids = sorted(room_id for room_id, count in nonzero if count == maximum)
    tied = len(tied_ids) > 1
    chosen = previous_room_id if previous_room_id in tied_ids else tied_ids[0]
    ranked_counts = sorted((count for _, count in nonzero), reverse=True)
    runner_up = ranked_counts[1] if len(ranked_counts) > 1 else 0
    candidates = tuple(sorted(room_id for room_id, count in nonzero))
    return VoxelMembership(
        object_id, chosen, "assigned", "max_voxel_support_tie_previous_owner" if tied and previous_room_id in tied_ids
        else ("max_voxel_support_tie_lexical_id" if tied else "max_voxel_support"), voxel_size_m, origin,
        tuple(sorted(counts.items())), maximum / len(points), runner_up / len(points), outside / len(points), tied,
        candidates, tuple(sorted((room_id, revisions[room_id]) for room_id in candidates if room_id in revisions)),
    )


@dataclass(frozen=True, slots=True)
class RoomObjectEvidence:
    """Latest object-level semantic/membership state for one producer revision."""

    object_id: str
    revision: int
    retained: bool
    semantic_status: str
    category: str | None
    membership_status: str
    room_id: str | None
    source_observation_ids: tuple[str, ...] = ()
    provenance: str = "unspecified"
    membership_evidence: Mapping[str, object] | None = None
    semantic_evidence: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _nonempty(self.object_id, "object_id")
        if type(self.revision) is not int or self.revision < 0:
            raise ContractError("object revision must be a non-negative integer")
        if type(self.retained) is not bool:
            raise ContractError("retained must be a boolean")
        if self.semantic_status not in ("unknown", "provisional", "stable", "conflicted"):
            raise ContractError("unsupported object semantic status")
        if self.category is not None:
            _nonempty(self.category, "category")
        if self.membership_status not in ("assigned", "ambiguous", "unassigned"):
            raise ContractError("membership_status must be assigned, ambiguous, or unassigned")
        if self.room_id is not None:
            _nonempty(self.room_id, "room_id")
        if self.membership_status == "assigned" and self.room_id is None:
            raise ContractError("assigned object evidence requires room_id")
        if self.membership_status != "assigned" and self.room_id is not None:
            raise ContractError("non-assigned object evidence must not claim a room_id")
        if type(self.provenance) is not str or not self.provenance:
            raise ContractError("provenance must be non-empty")
        if not isinstance(self.source_observation_ids, (tuple, list)):
            raise ContractError("source_observation_ids must be a sequence")
        source_ids = tuple(self.source_observation_ids)
        if any(type(item) is not str or not item for item in source_ids):
            raise ContractError("source observation IDs must be non-empty strings")
        if len(set(source_ids)) != len(source_ids):
            raise ContractError("source observation IDs must be unique")
        object.__setattr__(self, "source_observation_ids", source_ids)
        if self.membership_evidence is not None:
            details = _json_mapping(self.membership_evidence, "membership_evidence")
            if details.get("object_id") not in (None, self.object_id):
                raise ContractError("membership evidence object_id must match its object")
            if details.get("room_id") not in (None, self.room_id):
                raise ContractError("membership evidence room_id must match the assigned room")
            object.__setattr__(self, "membership_evidence", details)
        if self.semantic_evidence is not None:
            details = _json_mapping(self.semantic_evidence, "semantic_evidence")
            if details.get("object_id") not in (None, self.object_id):
                raise ContractError("semantic evidence object_id must match its object")
            if details.get("status") not in (None, self.semantic_status):
                raise ContractError("semantic evidence status must match its object state")
            if details.get("category") not in (None, self.category):
                raise ContractError("semantic evidence category must match its object state")
            object.__setattr__(self, "semantic_evidence", details)

    def as_dict(self) -> dict[str, object]:
        return {
            "object_id": self.object_id, "revision": self.revision, "retained": self.retained,
            "semantic_status": self.semantic_status, "category": self.category,
            "membership_status": self.membership_status, "room_id": self.room_id,
            "source_observation_ids": list(self.source_observation_ids), "provenance": self.provenance,
            "membership_evidence": None if self.membership_evidence is None else dict(self.membership_evidence),
            "semantic_evidence": None if self.semantic_evidence is None else dict(self.semantic_evidence),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RoomObjectEvidence":
        allowed = {"object_id", "revision", "retained", "semantic_status", "category", "membership_status",
                   "room_id", "source_observation_ids", "provenance", "membership_evidence", "semantic_evidence"}
        if not isinstance(value, Mapping) or set(value) - allowed or not {"object_id", "revision", "retained", "semantic_status", "category", "membership_status", "room_id"}.issubset(value):
            raise ContractError("invalid RoomObjectEvidence serialization fields")
        ids = value.get("source_observation_ids", ())
        if not isinstance(ids, (list, tuple)):
            raise ContractError("source_observation_ids must be a sequence")
        return cls(**{key: value[key] for key in allowed if key in value})


@dataclass(frozen=True, slots=True)
class RoomBeliefPolicy:
    """Explicit label/relabel policy. No project-wide threshold is implicit."""

    policy_id: str
    minimum_supported_categories: int
    minimum_top_belief: float
    minimum_margin: float
    relabel_margin: float
    relabel_evidence_revisions: int

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        if type(self.minimum_supported_categories) is not int or self.minimum_supported_categories < 1:
            raise ContractError("minimum_supported_categories must be positive")
        for name in ("minimum_top_belief", "minimum_margin", "relabel_margin"):
            object.__setattr__(self, name, _unit(getattr(self, name), name))
        if type(self.relabel_evidence_revisions) is not int or self.relabel_evidence_revisions < 1:
            raise ContractError("relabel_evidence_revisions must be positive")

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "minimum_supported_categories": self.minimum_supported_categories,
            "minimum_top_belief": self.minimum_top_belief, "minimum_margin": self.minimum_margin,
            "relabel_margin": self.relabel_margin,
            "relabel_evidence_revisions": self.relabel_evidence_revisions,
        }


@dataclass(frozen=True, slots=True)
class RoomBeliefState:
    room_id: str
    evidence_revision: int
    label: str | None
    status: str
    last_supported_label: str | None
    candidates: tuple[tuple[str, float], ...]
    raw_scores: tuple[tuple[str, float], ...]
    category_counts: tuple[tuple[str, int], ...]
    category_object_ids: tuple[tuple[str, tuple[str, ...]], ...]
    supporting_object_ids: tuple[str, ...]
    pending_label: str | None
    pending_revision_count: int
    label_history: tuple[tuple[int, str | None, str | None, str], ...]
    score_kind: str = "normalized_uncalibrated_belief_score"

    def as_dict(self) -> dict[str, object]:
        return {
            "room_id": self.room_id, "evidence_revision": self.evidence_revision,
            "label": self.label, "status": self.status, "last_supported_label": self.last_supported_label,
            "candidates": [[key, value] for key, value in self.candidates],
            "raw_scores": {key: value for key, value in self.raw_scores},
            "category_counts": {key: value for key, value in self.category_counts},
            "category_object_ids": {key: list(value) for key, value in self.category_object_ids},
            "supporting_object_ids": list(self.supporting_object_ids),
            "pending_label": self.pending_label, "pending_revision_count": self.pending_revision_count,
            "label_history": [{"revision": revision, "from": old, "to": new, "reason": reason}
                              for revision, old, new, reason in self.label_history],
            "score_kind": self.score_kind,
        }


@dataclass(frozen=True, slots=True)
class RoomBeliefEvent:
    event_id: str
    object_id: str
    producer_revision: int
    evidence: RoomObjectEvidence | None

    def __post_init__(self) -> None:
        _nonempty(self.event_id, "event_id")
        _nonempty(self.object_id, "object_id")
        if type(self.producer_revision) is not int or self.producer_revision < 0:
            raise ContractError("producer_revision must be a non-negative integer")
        if self.evidence is not None and (self.evidence.object_id != self.object_id or self.evidence.revision != self.producer_revision):
            raise ContractError("event and object evidence IDs/revisions must agree")

    def as_dict(self) -> dict[str, object]:
        payload = {"event_id": self.event_id, "object_id": self.object_id,
                   "producer_revision": self.producer_revision,
                   "evidence": None if self.evidence is None else self.evidence.as_dict()}
        payload["payload_hash"] = _hash(payload)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RoomBeliefEvent":
        allowed = {"event_id", "object_id", "producer_revision", "evidence", "payload_hash"}
        if not isinstance(value, Mapping) or set(value) - allowed or not {"event_id", "object_id", "producer_revision", "evidence"}.issubset(value):
            raise ContractError("invalid RoomBeliefEvent serialization fields")
        payload = {key: value[key] for key in ("event_id", "object_id", "producer_revision", "evidence")}
        expected = _hash(payload)
        if value.get("payload_hash") not in (None, expected):
            raise ContractError("RoomBeliefEvent payload hash mismatch")
        evidence = value["evidence"]
        return cls(value["event_id"], value["object_id"], value["producer_revision"],
                   None if evidence is None else RoomObjectEvidence.from_dict(evidence))


class RoomBeliefLedger:
    """Incremental, retractable category-presence ledger and room belief.

    The supplied A table is an input contract only; this class does not create
    or validate model quality. Repeated objects of one category update
    provenance/counts but that category contributes to room scores once.
    """

    SCHEMA_VERSION = 1

    def __init__(self, *, room_ids: Sequence[str], room_priors: Mapping[str, float],
                 category_room_scores: Mapping[str, Mapping[str, float]],
                 label_policy: RoomBeliefPolicy, evidence_policy_id: str,
                 ontology_version: str, a_table_hash: str,
                 room_node_ids: Sequence[str] | None = None,
                 allow_empty_room_nodes: bool = False):
        self.room_ids = tuple(sorted(_nonempty(value, "room_id") for value in room_ids))
        if not self.room_ids or len(self.room_ids) != len(set(self.room_ids)):
            raise ContractError("room_ids must be non-empty and unique")
        self.room_node_ids = tuple(sorted(_nonempty(value, "room_node_id") for value in
                                          (self.room_ids if room_node_ids is None else room_node_ids)))
        if (not self.room_node_ids and not allow_empty_room_nodes) or len(self.room_node_ids) != len(set(self.room_node_ids)):
            raise ContractError("room_node_ids must be non-empty and unique")
        _nonempty(evidence_policy_id, "evidence_policy_id")
        _nonempty(ontology_version, "ontology_version")
        if not isinstance(label_policy, RoomBeliefPolicy):
            raise ContractError("label_policy must be explicit")
        if set(room_priors) != set(self.room_ids):
            raise ContractError("room_priors must cover exactly the configured room IDs")
        priors = {}
        for room_id, value in room_priors.items():
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ContractError("room prior values must be finite and positive")
            priors[room_id] = float(value)
        prior_scale = max(priors.values())
        scaled_total = sum(value / prior_scale for value in priors.values())
        self.room_priors = {key: (value / prior_scale) / scaled_total for key, value in priors.items()}
        if any(value <= 0.0 or not math.isfinite(value) for value in self.room_priors.values()):
            raise ContractError("normalized room priors must remain finite and positive")
        if not isinstance(category_room_scores, Mapping):
            raise ContractError("category_room_scores must be a mapping")
        self.category_room_scores: dict[str, dict[str, float]] = {}
        for category, row in category_room_scores.items():
            _nonempty(category, "A-table category")
            if not isinstance(row, Mapping) or set(row) != set(self.room_ids):
                raise ContractError(f"A-table row {category!r} must cover exactly all configured rooms")
            values = {}
            for room_id, value in row.items():
                if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                    raise ContractError("A-table scores must be finite real values")
                values[room_id] = float(value)
            self.category_room_scores[category] = values
        self.label_policy = label_policy
        self.evidence_policy_id = evidence_policy_id
        self.ontology_version = ontology_version
        self.a_table_hash = _nonempty(a_table_hash, "a_table_hash")
        self._objects: dict[str, RoomObjectEvidence] = {}
        self._latest_revision: dict[str, int] = {}
        self._latest_state_hash: dict[str, str] = {}
        self._category_members: dict[str, dict[str, set[str]]] = {rid: {} for rid in self.room_node_ids}
        self._room_revision = {rid: 0 for rid in self.room_node_ids}
        self._room_label: dict[str, str | None] = {rid: None for rid in self.room_node_ids}
        self._room_status: dict[str, str] = {rid: "unknown" for rid in self.room_node_ids}
        self._last_supported: dict[str, str | None] = {rid: None for rid in self.room_node_ids}
        self._pending: dict[str, str | None] = {rid: None for rid in self.room_node_ids}
        self._pending_count: dict[str, int] = {rid: 0 for rid in self.room_node_ids}
        self._pending_last_revision: dict[str, int] = {rid: -1 for rid in self.room_node_ids}
        self._event_hashes: dict[str, str] = {}
        self._event_log: list[RoomBeliefEvent] = []
        self._label_history: dict[str, list[dict[str, object]]] = {rid: [] for rid in self.room_node_ids}

    def register_room_node(self, room_id: str) -> None:
        """Register a physical room when its first revision becomes active."""
        room_id = _nonempty(room_id, "room_node_id")
        if room_id in self.room_node_ids:
            return
        self.room_node_ids = tuple(sorted((*self.room_node_ids, room_id)))
        self._category_members[room_id] = {}
        self._room_revision[room_id] = 0
        self._room_label[room_id] = None
        self._room_status[room_id] = "unknown"
        self._last_supported[room_id] = None
        self._pending[room_id] = None
        self._pending_count[room_id] = 0
        self._pending_last_revision[room_id] = -1
        self._label_history[room_id] = []

    def _eligible(self, evidence: RoomObjectEvidence | None) -> tuple[str, str] | None:
        if evidence is None or not evidence.retained or evidence.semantic_status != "stable":
            return None
        if evidence.membership_status != "assigned" or evidence.room_id not in self.room_node_ids:
            return None
        if evidence.category not in self.category_room_scores:
            return None
        return evidence.room_id, evidence.category

    def _room_members_after(self, room_id: str, old: RoomObjectEvidence | None,
                            new: RoomObjectEvidence | None) -> dict[str, set[str]]:
        members = {category: set(ids) for category, ids in self._category_members[room_id].items()}
        old_pair, new_pair = self._eligible(old), self._eligible(new)
        if old_pair is not None and old_pair[0] == room_id:
            members.get(old_pair[1], set()).discard(old.object_id)
            if old_pair[1] in members and not members[old_pair[1]]:
                del members[old_pair[1]]
        if new_pair is not None and new_pair[0] == room_id:
            members.setdefault(new_pair[1], set()).add(new.object_id)
        return members

    def _score_room(self, room_id: str, members: Mapping[str, set[str]], *,
                    evidence_revision: int | None = None) -> RoomBeliefState:
        categories = sorted(category for category, ids in members.items() if ids)
        scores = {label: math.log(self.room_priors[label]) for label in self.room_ids}
        for category in categories:
            row = self.category_room_scores.get(category)
            if row is not None:
                for label in self.room_ids:
                    scores[label] += row[label]
        if any(not math.isfinite(value) for value in scores.values()):
            raise ContractError("room evidence scores overflowed; ledger update was not committed")
        max_score = max(scores.values())
        exp_values = {key: math.exp(value - max_score) for key, value in scores.items()}
        denom = sum(exp_values.values())
        ranked = tuple(sorted(((key, value / denom) for key, value in exp_values.items()),
                              key=lambda pair: (-pair[1], pair[0])))
        support_ids = tuple(sorted({object_id for category in categories for object_id in members[category]}))
        return RoomBeliefState(
            room_id=room_id,
            evidence_revision=self._room_revision[room_id] if evidence_revision is None else evidence_revision,
            label=self._room_label[room_id], status=self._room_status[room_id],
            last_supported_label=self._last_supported[room_id], candidates=ranked,
            raw_scores=tuple(sorted(scores.items())),
            category_counts=tuple(sorted((category, len(ids)) for category, ids in members.items() if ids)),
            category_object_ids=tuple(sorted((category, tuple(sorted(ids))) for category, ids in members.items() if ids)),
            supporting_object_ids=support_ids, pending_label=self._pending[room_id],
            pending_revision_count=self._pending_count[room_id],
            label_history=tuple((int(item["revision"]), item["from"], item["to"], item["reason"])
                                for item in self._label_history[room_id]),
        )

    def _meets_absolute_gate(self, ranked: tuple[tuple[str, float], ...], category_count: int) -> bool:
        if not ranked or category_count < self.label_policy.minimum_supported_categories:
            return False
        top = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        return top >= self.label_policy.minimum_top_belief and top - second >= self.label_policy.minimum_margin

    def _update_label(self, room_id: str, ranked: tuple[tuple[str, float], ...], category_count: int,
                      revision: int, *, new_members: Mapping[str, set[str]]) -> None:
        old_label = self._room_label[room_id]
        incumbent_label = old_label or self._last_supported[room_id]
        if not any(new_members.values()):
            if incumbent_label is not None:
                self._last_supported[room_id] = incumbent_label
                self._label_history[room_id].append({"revision": revision, "from": incumbent_label, "to": None,
                                                     "reason": "eligible_evidence_retracted"})
            self._room_label[room_id] = None
            self._room_status[room_id] = "unknown"
            self._pending[room_id] = None
            self._pending_count[room_id] = 0
            self._pending_last_revision[room_id] = -1
            return

        if not self._meets_absolute_gate(ranked, category_count):
            if incumbent_label is not None:
                self._last_supported[room_id] = incumbent_label
                self._label_history[room_id].append({"revision": revision, "from": incumbent_label, "to": None,
                                                     "reason": "label_gate_no_longer_met"})
            self._room_label[room_id] = None
            self._room_status[room_id] = "ambiguous" if len(ranked) > 1 else "unknown"
            self._pending[room_id] = None
            self._pending_count[room_id] = 0
            self._pending_last_revision[room_id] = -1
            return

        top_label = ranked[0][0]
        if incumbent_label is None:
            self._room_label[room_id] = top_label
            self._room_status[room_id] = "predicted"
            self._last_supported[room_id] = top_label
            self._label_history[room_id].append({"revision": revision, "from": None, "to": top_label,
                                                 "reason": "initial_label_gate_met"})
            self._pending[room_id] = None
            self._pending_count[room_id] = 0
            self._pending_last_revision[room_id] = -1
            return

        if top_label == incumbent_label:
            self._room_label[room_id] = incumbent_label
            self._room_status[room_id] = "predicted"
            self._last_supported[room_id] = incumbent_label
            self._pending[room_id] = None
            self._pending_count[room_id] = 0
            self._pending_last_revision[room_id] = -1
            return

        current_belief = dict(ranked).get(incumbent_label, 0.0)
        if ranked[0][1] - current_belief < self.label_policy.relabel_margin:
            self._room_label[room_id] = None
            self._last_supported[room_id] = incumbent_label
            self._room_status[room_id] = "ambiguous"
            self._pending[room_id] = None
            self._pending_count[room_id] = 0
            self._pending_last_revision[room_id] = -1
            return

        if self._pending[room_id] != top_label:
            self._pending[room_id] = top_label
            self._pending_count[room_id] = 1
        elif self._pending_last_revision[room_id] != revision:
            self._pending_count[room_id] += 1
        self._pending_last_revision[room_id] = revision
        if self._pending_count[room_id] >= self.label_policy.relabel_evidence_revisions:
            self._room_label[room_id] = top_label
            self._room_status[room_id] = "predicted"
            self._last_supported[room_id] = top_label
            self._label_history[room_id].append({"revision": revision, "from": incumbent_label, "to": top_label,
                                                 "reason": "relabel_hysteresis_satisfied"})
            self._pending[room_id] = None
            self._pending_count[room_id] = 0
            self._pending_last_revision[room_id] = -1
        else:
            self._room_label[room_id] = None
            self._last_supported[room_id] = incumbent_label
            self._room_status[room_id] = "provisional"

    def apply(self, event: RoomBeliefEvent) -> dict[str, object]:
        """Apply an object upsert/retract atomically; repeated/stale events no-op."""
        if not isinstance(event, RoomBeliefEvent):
            raise ContractError("apply requires RoomBeliefEvent")
        event_payload = event.as_dict()
        payload_hash = event_payload["payload_hash"]
        existing_event_hash = self._event_hashes.get(event.event_id)
        if existing_event_hash is not None:
            if existing_event_hash != payload_hash:
                raise ContractError("event_id was reused with a different payload")
            return {"status": "idempotent_replay", "event_id": event.event_id, "affected_rooms": []}

        current_revision = self._latest_revision.get(event.object_id)
        incoming_hash = _hash(None if event.evidence is None else event.evidence.as_dict())
        if current_revision is not None and event.producer_revision < current_revision:
            self._event_hashes[event.event_id] = payload_hash
            self._event_log.append(event)
            return {"status": "stale_rejected", "event_id": event.event_id, "affected_rooms": []}
        if current_revision is not None and event.producer_revision == current_revision:
            if self._latest_state_hash[event.object_id] != incoming_hash:
                raise ContractError("same producer revision carries a different object payload")
            self._event_hashes[event.event_id] = payload_hash
            self._event_log.append(event)
            return {"status": "idempotent_revision", "event_id": event.event_id, "affected_rooms": []}

        old = self._objects.get(event.object_id)
        new = event.evidence
        old_pair, new_pair = self._eligible(old), self._eligible(new)
        affected = sorted({pair[0] for pair in (old_pair, new_pair) if pair is not None})
        trial_members = {rid: self._category_members[rid] for rid in affected}
        replacements = {rid: self._room_members_after(rid, old, new) for rid in affected}
        next_revisions: dict[str, int] = {}
        for room_id in affected:
            before_signature = tuple(sorted((cat, tuple(sorted(ids))) for cat, ids in trial_members[room_id].items() if ids))
            after_signature = tuple(sorted((cat, tuple(sorted(ids))) for cat, ids in replacements[room_id].items() if ids))
            next_revisions[room_id] = self._room_revision[room_id] + (before_signature != after_signature)

        # Compute new labels on a temporary ledger snapshot. If validation or
        # scoring fails, no object/category state is committed partially.
        saved = (self._room_label.copy(), self._room_status.copy(), self._last_supported.copy(),
                 self._pending.copy(), self._pending_count.copy(), self._pending_last_revision.copy(),
                 {rid: list(rows) for rid, rows in self._label_history.items()})
        try:
            for room_id in affected:
                new_members = replacements[room_id]
                self._update_label(room_id, self._score_room(room_id, new_members,
                                                              evidence_revision=next_revisions[room_id]).candidates,
                                   len([cat for cat, ids in new_members.items() if ids]),
                                   next_revisions[room_id], new_members=new_members)
        except Exception:
            (self._room_label, self._room_status, self._last_supported, self._pending,
             self._pending_count, self._pending_last_revision, self._label_history) = saved
            raise

        if new is None:
            self._objects.pop(event.object_id, None)
            self._latest_state_hash[event.object_id] = incoming_hash
        else:
            self._objects[event.object_id] = new
            self._latest_state_hash[event.object_id] = incoming_hash
        self._latest_revision[event.object_id] = event.producer_revision
        for room_id in affected:
            self._category_members[room_id] = replacements[room_id]
            self._room_revision[room_id] = next_revisions[room_id]
        self._event_hashes[event.event_id] = payload_hash
        self._event_log.append(event)
        return {"status": "applied", "event_id": event.event_id, "affected_rooms": affected}

    def apply_frame(self, events: Sequence[RoomBeliefEvent]) -> list[dict[str, object]]:
        """Apply one causal frame transaction and relabel each room once.

        All events in a frame share one producer revision.  The final object
        payloads are assembled first, room category membership is computed as
        one net change, and only then are labels/hysteresis updated.  A failed
        validation or scoring step restores the complete ledger state.
        """
        if not isinstance(events, (list, tuple)):
            raise ContractError("frame events must be a sequence")
        if not events:
            return []
        if any(not isinstance(event, RoomBeliefEvent) for event in events):
            raise ContractError("frame events must contain RoomBeliefEvent records")
        revisions = {event.producer_revision for event in events}
        if len(revisions) != 1:
            raise ContractError("all frame events must share one producer revision")
        # Sorting makes the transaction independent of caller event order.
        ordered = sorted(events, key=lambda event: (event.object_id, event.event_id))
        if len({event.event_id for event in ordered}) != len(ordered):
            raise ContractError("frame event IDs must be unique")
        if len({event.object_id for event in ordered}) != len(ordered):
            raise ContractError("a frame may contain at most one update per object")

        result_by_event_id: dict[str, dict[str, object]] = {}
        pending: list[RoomBeliefEvent] = []
        for event in ordered:
            payload = event.as_dict()
            prior_hash = self._event_hashes.get(event.event_id)
            if prior_hash is not None:
                if prior_hash != payload["payload_hash"]:
                    raise ContractError("event_id was reused with a different payload")
                result_by_event_id[event.event_id] = {"status": "idempotent_replay", "event_id": event.event_id, "affected_rooms": []}
                continue
            current_revision = self._latest_revision.get(event.object_id)
            if current_revision is not None and event.producer_revision <= current_revision:
                raise ContractError("frame event is stale or repeats an object revision")
            pending.append(event)
        if not pending:
            return [result_by_event_id[event.event_id] for event in events]

        old_by_object = {event.object_id: self._objects.get(event.object_id) for event in pending}
        new_by_object = {event.object_id: event.evidence for event in pending}
        affected = sorted({pair[0] for event in pending
                           for pair in (self._eligible(old_by_object[event.object_id]),
                                        self._eligible(new_by_object[event.object_id]))
                           if pair is not None})
        replacements = {
            room_id: {category: set(ids) for category, ids in self._category_members[room_id].items()}
            for room_id in affected
        }
        for event in pending:
            old_pair = self._eligible(old_by_object[event.object_id])
            new_pair = self._eligible(new_by_object[event.object_id])
            if old_pair is not None:
                replacements[old_pair[0]].setdefault(old_pair[1], set()).discard(event.object_id)
            if new_pair is not None:
                replacements[new_pair[0]].setdefault(new_pair[1], set()).add(event.object_id)
        for members in replacements.values():
            for category in list(members):
                if not members[category]:
                    del members[category]

        saved = (
            copy.deepcopy(self._objects), self._latest_revision.copy(), self._latest_state_hash.copy(),
            copy.deepcopy(self._category_members), self._room_revision.copy(),
            self._room_label.copy(), self._room_status.copy(), self._last_supported.copy(),
            self._pending.copy(), self._pending_count.copy(), self._pending_last_revision.copy(),
            copy.deepcopy(self._label_history), self._event_hashes.copy(), list(self._event_log),
        )
        try:
            next_revisions: dict[str, int] = {}
            for room_id in affected:
                before = tuple(sorted((cat, tuple(sorted(ids)))
                                      for cat, ids in self._category_members[room_id].items() if ids))
                after = tuple(sorted((cat, tuple(sorted(ids)))
                                     for cat, ids in replacements[room_id].items() if ids))
                next_revisions[room_id] = self._room_revision[room_id] + (before != after)
            for room_id in affected:
                state = self._score_room(room_id, replacements[room_id],
                                         evidence_revision=next_revisions[room_id])
                self._update_label(room_id, state.candidates,
                                   len(replacements[room_id]), next_revisions[room_id],
                                   new_members=replacements[room_id])
            for event in pending:
                evidence = event.evidence
                incoming_hash = _hash(None if evidence is None else evidence.as_dict())
                if evidence is None:
                    self._objects.pop(event.object_id, None)
                else:
                    self._objects[event.object_id] = evidence
                self._latest_state_hash[event.object_id] = incoming_hash
                self._latest_revision[event.object_id] = event.producer_revision
                self._event_hashes[event.event_id] = event.as_dict()["payload_hash"]
                self._event_log.append(event)
            for room_id in affected:
                self._category_members[room_id] = replacements[room_id]
                self._room_revision[room_id] = next_revisions[room_id]
        except Exception:
            (self._objects, self._latest_revision, self._latest_state_hash,
             self._category_members, self._room_revision, self._room_label,
             self._room_status, self._last_supported, self._pending,
             self._pending_count, self._pending_last_revision,
             self._label_history, self._event_hashes, self._event_log) = saved
            raise
        for event in pending:
            old_pair = self._eligible(old_by_object[event.object_id])
            new_pair = self._eligible(new_by_object[event.object_id])
            result_by_event_id[event.event_id] = {
                "status": "applied", "event_id": event.event_id,
                "affected_rooms": sorted({pair[0] for pair in (old_pair, new_pair) if pair is not None}),
            }
        return [result_by_event_id[event.event_id] for event in events]

    def room_state(self, room_id: str) -> RoomBeliefState:
        if room_id not in self.room_node_ids:
            raise ContractError(f"unknown room_id: {room_id}")
        return self._score_room(room_id, self._category_members[room_id])

    def object_evidence(self, object_id: str) -> RoomObjectEvidence | None:
        return self._objects.get(object_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "config": {
                "room_ids": list(self.room_ids), "room_node_ids": list(self.room_node_ids),
                "room_priors": dict(self.room_priors),
                "category_room_scores": self.category_room_scores,
                "label_policy": self.label_policy.as_dict(),
                "evidence_policy_id": self.evidence_policy_id,
                "ontology_version": self.ontology_version, "a_table_hash": self.a_table_hash,
            },
            "events": [event.as_dict() for event in self._event_log],
            "room_states": {room_id: self.room_state(room_id).as_dict() for room_id in self.room_node_ids},
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RoomBeliefLedger":
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "config", "events", "room_states"}:
            raise ContractError("invalid RoomBeliefLedger serialization fields")
        if value["schema_version"] != cls.SCHEMA_VERSION or not isinstance(value["config"], Mapping):
            raise ContractError("unsupported RoomBeliefLedger schema/config")
        config = value["config"]
        expected_config = {"room_ids", "room_priors", "category_room_scores", "label_policy",
                           "evidence_policy_id", "ontology_version", "a_table_hash"}
        allowed_config = expected_config | {"room_node_ids"}
        if set(config) not in (expected_config, allowed_config):
            raise ContractError("invalid RoomBeliefLedger config serialization fields")
        policy = config.get("label_policy")
        if not isinstance(policy, Mapping):
            raise ContractError("serialized RoomBeliefLedger is missing label policy")
        ledger = cls(
            room_ids=config["room_ids"], room_node_ids=config.get("room_node_ids"), room_priors=config["room_priors"],
            category_room_scores=config["category_room_scores"],
            label_policy=RoomBeliefPolicy(**policy), evidence_policy_id=config["evidence_policy_id"],
            ontology_version=config["ontology_version"], a_table_hash=config.get("a_table_hash"),
            allow_empty_room_nodes=not bool(config.get("room_node_ids")),
        )
        events = value["events"]
        if not isinstance(events, (tuple, list)):
            raise ContractError("serialized RoomBeliefLedger events must be a sequence")
        for raw_event in events:
            ledger.apply(RoomBeliefEvent.from_dict(raw_event))
        expected_states = value["room_states"]
        actual_states = {room_id: ledger.room_state(room_id).as_dict() for room_id in ledger.room_node_ids}
        if expected_states != actual_states:
            raise ContractError("serialized room state does not match replayed evidence ledger")
        return ledger

    @property
    def category_members(self) -> dict[str, dict[str, tuple[str, ...]]]:
        """Copy of reverse index: room -> category -> eligible object IDs."""
        return {room_id: {category: tuple(sorted(ids)) for category, ids in sorted(categories.items())}
                for room_id, categories in self._category_members.items()}
