"""Pure helpers for static Room Belief evidence-readiness inspection."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Mapping, Sequence

from ..contracts.errors import ContractError


def exact_pose_group_id(matrix: Sequence[Sequence[object]]) -> str:
    """Hash an exact canonical camera transform; this is a pose key, not view independence."""
    if isinstance(matrix, (str, bytes)) or len(matrix) != 4:
        raise ContractError("camera pose must be a finite 4x4 matrix")
    normalized: list[list[float]] = []
    for row in matrix:
        if isinstance(row, (str, bytes)) or len(row) != 4:
            raise ContractError("camera pose must be a finite 4x4 matrix")
        out = []
        for value in row:
            if isinstance(value, bool):
                raise ContractError("camera pose entries must be finite numbers")
            try:
                converted = float(value)
            except (TypeError, ValueError) as exc:
                raise ContractError("camera pose entries must be finite numbers") from exc
            if not math.isfinite(converted):
                raise ContractError("camera pose entries must be finite numbers")
            out.append(0.0 if converted == 0.0 else converted)
        normalized.append(out)
    payload = json.dumps(normalized, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "pose_exact:" + hashlib.sha256(payload).hexdigest()[:16]


def validate_xy_membership_scope(
    room_ids: Sequence[str],
    *,
    observed_floor_ids: Sequence[str],
    room_floor_ids: Mapping[str, str] | None,
    single_floor_scope_verified: bool,
    scope_provenance: str,
) -> dict[str, object]:
    """Fail closed before XY-only membership unless the geometry is floor-scoped.

    Explicit per-room floor IDs are preferred. For a genuinely single-floor
    snapshot, the caller may supply its verified scope provenance. The result
    records that this does not establish whole-scene multi-floor safety.
    """
    if isinstance(room_ids, (str, bytes)) or not room_ids or any(type(x) is not str or not x for x in room_ids):
        raise ContractError("room_ids must be a non-empty sequence of non-empty IDs")
    if isinstance(observed_floor_ids, (str, bytes)) or not observed_floor_ids or any(type(x) is not str or not x for x in observed_floor_ids):
        raise ContractError("observed_floor_ids must be a non-empty sequence from provenance")
    if type(single_floor_scope_verified) is not bool:
        raise ContractError("single_floor_scope_verified must be an explicit boolean")
    if type(scope_provenance) is not str or not scope_provenance.strip():
        raise ContractError("scope_provenance must identify the verifying source")
    observed = sorted(set(observed_floor_ids))
    if room_floor_ids is not None:
        if set(room_floor_ids) != set(room_ids) or any(type(v) is not str or not v for v in room_floor_ids.values()):
            raise ContractError("room_floor_ids must cover every candidate room with a known floor ID")
        candidates = sorted(room_id for room_id in room_ids if room_floor_ids[room_id] in observed)
        if not candidates:
            return {"status": "no_floor_compatible_rooms", "candidate_room_ids": [],
                    "observed_floor_ids": observed, "scope_provenance": scope_provenance,
                    "cross_floor_xy_membership_allowed": False}
        return {"status": "room_floor_ids_filtered", "candidate_room_ids": candidates,
                "observed_floor_ids": observed, "room_floor_ids": dict(room_floor_ids),
                "scope_provenance": scope_provenance, "cross_floor_xy_membership_allowed": False}
    if single_floor_scope_verified and len(observed) == 1:
        return {"status": "verified_single_observed_floor_scope", "candidate_room_ids": sorted(set(room_ids)),
                "observed_floor_ids": observed, "scope_provenance": scope_provenance,
                "cross_floor_xy_membership_allowed": False,
                "limitation": "scope applies only to the verified snapshot/sequence; room XY geometry has no Z bounds"}
    raise ContractError("XY-only membership requires complete room floor IDs or a verified single-floor snapshot scope")


def group_raw_classes_by_exact_pose(observations: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Summarize raw class evidence with one unit per exact camera pose.

    Conflicting raw classes at one pose split that group's unit evenly. This is
    an explicit conservative heuristic; pose groups are not asserted to be
    statistically independent views.
    """
    seen: set[str] = set()
    groups: dict[str, set[str]] = {}
    group_observations: dict[str, list[str]] = {}
    raw_counts: dict[str, int] = {}
    for item in observations:
        if not isinstance(item, Mapping):
            raise ContractError("observations must be mappings")
        observation_id = item.get("observation_id")
        raw_class = item.get("raw_class")
        pose = item.get("camera_pose")
        if type(observation_id) is not str or not observation_id:
            raise ContractError("every observation needs a full observation_id")
        if observation_id in seen:
            raise ContractError("observation IDs must be unique within one object")
        seen.add(observation_id)
        if type(raw_class) is not str or not raw_class:
            continue
        pose_id = exact_pose_group_id(pose)
        groups.setdefault(pose_id, set()).add(raw_class)
        group_observations.setdefault(pose_id, []).append(observation_id)
        raw_counts[raw_class] = raw_counts.get(raw_class, 0) + 1

    support: dict[str, float] = {}
    for classes in groups.values():
        if not classes:
            continue
        amount = 1.0 / len(classes)
        for raw_class in classes:
            support[raw_class] = support.get(raw_class, 0.0) + amount
    ordered_groups = [
        {"view_group_id": pose_id, "observation_ids": sorted(ids),
         "raw_classes": sorted(groups[pose_id])}
        for pose_id, ids in sorted(group_observations.items())
    ]
    return {
        "view_group_policy_id": "exact_camera_pose_matrix_v1",
        "view_group_count": len(groups),
        "view_groups": ordered_groups,
        "raw_observation_class_counts": dict(sorted(raw_counts.items())),
        "one_unit_per_group_raw_class_support": dict(sorted(support.items())),
        "support_semantics": "heuristic vote units; not calibrated probabilities and pose groups are not statistically independent views",
    }
