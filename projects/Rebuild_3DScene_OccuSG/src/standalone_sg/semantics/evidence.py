"""Graph-independent contracts for object-supported region semantics."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from numbers import Real
from typing import Mapping

from ..contracts.errors import ContractError
from ..contracts.regions import Polygon2D


def _finite_optional(value, label: str, *, low: float | None = None, high: float | None = None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ContractError(f"{label} must be a finite number or None")
    result = float(value)
    if not math.isfinite(result) or (low is not None and result < low) or (high is not None and result > high):
        raise ContractError(f"{label} is outside its valid finite range")
    return result


def _json_mapping(value: Mapping[str, object], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ContractError(f"{label} must be a string-keyed mapping")
    try:
        copied = json.loads(json.dumps(dict(value), sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} must contain finite JSON data") from exc
    return copied


@dataclass(frozen=True, slots=True)
class RegionObjectEvidence:
    """One persistent object identity; repeated detections stay observations, not counts."""

    object_id: str
    class_name: str | None
    position_xy_m: tuple[float, float] | None
    detector_score_raw: float | None
    first_seen_s: float | None
    last_seen_s: float | None
    first_detection_id: str | None
    last_detection_id: str | None
    observations: tuple[Mapping[str, object], ...] = ()
    membership_reason: str = "unspecified"
    plausible_region_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.object_id) is not str or not self.object_id.strip():
            raise ContractError("region evidence object_id must be non-empty")
        if self.class_name is not None and (type(self.class_name) is not str or not self.class_name.strip()):
            raise ContractError("object class_name must be a non-empty string or None")
        if self.position_xy_m is not None:
            if type(self.position_xy_m) is not tuple or len(self.position_xy_m) != 2:
                raise ContractError("object position_xy_m must be an XY tuple or None")
            xy = tuple(_finite_optional(v, "object position") for v in self.position_xy_m)
            object.__setattr__(self, "position_xy_m", xy)
        object.__setattr__(self, "detector_score_raw", _finite_optional(self.detector_score_raw, "detector score", low=0, high=1))
        object.__setattr__(self, "first_seen_s", _finite_optional(self.first_seen_s, "first_seen_s"))
        object.__setattr__(self, "last_seen_s", _finite_optional(self.last_seen_s, "last_seen_s"))
        if self.first_seen_s is not None and self.last_seen_s is not None and self.first_seen_s > self.last_seen_s:
            raise ContractError("object first_seen_s must not exceed last_seen_s")
        for field_name in ("first_detection_id", "last_detection_id"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not str or not value):
                raise ContractError(f"{field_name} must be a non-empty string or None")
        if type(self.membership_reason) is not str or not self.membership_reason:
            raise ContractError("membership_reason must be non-empty")
        if len(set(self.plausible_region_ids)) != len(self.plausible_region_ids) or any(type(v) is not str for v in self.plausible_region_ids):
            raise ContractError("plausible_region_ids must be unique strings")
        normalized = tuple(_json_mapping(v, "object observation") for v in self.observations)
        object.__setattr__(self, "observations", normalized)

    def as_dict(self) -> dict[str, object]:
        return {
            "object_id": self.object_id, "class_name": self.class_name,
            "position_xy_m": None if self.position_xy_m is None else list(self.position_xy_m),
            "detector_score_raw": self.detector_score_raw,
            "first_seen_s": self.first_seen_s, "last_seen_s": self.last_seen_s,
            "first_detection_id": self.first_detection_id, "last_detection_id": self.last_detection_id,
            "observations": [dict(item) for item in self.observations],
            "membership_reason": self.membership_reason,
            "plausible_region_ids": list(self.plausible_region_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RegionObjectEvidence":
        allowed = {"object_id", "class_name", "position_xy_m", "detector_score_raw", "first_seen_s", "last_seen_s",
                   "first_detection_id", "last_detection_id", "observations", "membership_reason", "plausible_region_ids"}
        if not isinstance(value, Mapping) or set(value) - allowed or not {"object_id", "class_name"}.issubset(value):
            raise ContractError("invalid RegionObjectEvidence serialization fields")
        position = value.get("position_xy_m")
        if position is not None:
            if not isinstance(position, (list, tuple)):
                raise ContractError("serialized object position must be a two-number sequence")
            position = tuple(position)
        observations = value.get("observations", ())
        plausible = value.get("plausible_region_ids", ())
        if not isinstance(observations, (list, tuple)) or not isinstance(plausible, (list, tuple)):
            raise ContractError("serialized object observations/region IDs must be sequences")
        return cls(
            object_id=value["object_id"], class_name=value["class_name"], position_xy_m=position,
            detector_score_raw=value.get("detector_score_raw"), first_seen_s=value.get("first_seen_s"),
            last_seen_s=value.get("last_seen_s"), first_detection_id=value.get("first_detection_id"),
            last_detection_id=value.get("last_detection_id"), observations=tuple(observations),
            membership_reason=value.get("membership_reason", "unspecified"),
            plausible_region_ids=tuple(plausible),
        )


@dataclass(frozen=True, slots=True)
class RegionEvidence:
    """Semantic input for either a tracked M5 region or an M8 RoomNode."""

    region_id: str
    region_kind: str
    geometry_revision: int
    evidence_revision: int
    coordinate_frame: str
    polygon: Polygon2D | None
    observation_timestamp_s: float | None
    processing_timestamp_s: float | None
    freshness: str
    source_region_id: int | None
    source_room_id: str | None
    objects: tuple[RegionObjectEvidence, ...]
    membership_provenance: str
    warnings: tuple[str, ...] = ()
    source_frame_id: str | None = None
    pose_context: Mapping[str, object] | None = None

    def __post_init__(self):
        for name in ("region_id", "region_kind", "coordinate_frame", "membership_provenance"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ContractError(f"RegionEvidence.{name} must be a non-empty string")
        if type(self.geometry_revision) is not int or self.geometry_revision < 0:
            raise ContractError("geometry_revision must be a non-negative integer")
        if type(self.evidence_revision) is not int or self.evidence_revision < 0:
            raise ContractError("evidence_revision must be a non-negative integer")
        if self.source_frame_id is not None and (type(self.source_frame_id) is not str or not self.source_frame_id):
            raise ContractError("source_frame_id must be a non-empty string or None")
        if self.pose_context is not None:
            object.__setattr__(self, "pose_context", _json_mapping(self.pose_context, "pose_context"))
        if self.freshness not in ("fresh", "stale"):
            raise ContractError("freshness must be fresh or stale")
        if self.source_region_id is not None and (type(self.source_region_id) is not int or self.source_region_id < 0):
            raise ContractError("source_region_id must be a non-negative integer or None")
        if self.source_room_id is not None and (type(self.source_room_id) is not str or not self.source_room_id):
            raise ContractError("source_room_id must be a non-empty string or None")
        for name in ("observation_timestamp_s", "processing_timestamp_s"):
            object.__setattr__(self, name, _finite_optional(getattr(self, name), name))
        if self.polygon is not None and not isinstance(self.polygon, Polygon2D):
            raise ContractError("RegionEvidence.polygon must be Polygon2D or None")
        if any(not isinstance(obj, RegionObjectEvidence) for obj in self.objects):
            raise ContractError("RegionEvidence.objects must contain RegionObjectEvidence")
        ids = [obj.object_id for obj in self.objects]
        if len(ids) != len(set(ids)):
            raise ContractError("RegionEvidence must contain one record per unique persistent object ID")
        if any(type(w) is not str or not w for w in self.warnings):
            raise ContractError("RegionEvidence.warnings must be non-empty strings")

    def as_dict(self) -> dict[str, object]:
        return {
            "region_id": self.region_id, "region_kind": self.region_kind,
            "geometry_revision": self.geometry_revision, "evidence_revision": self.evidence_revision,
            "source_frame_id": self.source_frame_id,
            "pose_context": None if self.pose_context is None else dict(self.pose_context),
            "coordinate_frame": self.coordinate_frame,
            "polygon": None if self.polygon is None else {
                "outer": [list(p) for p in self.polygon.outer],
                "holes": [[list(p) for p in ring] for ring in self.polygon.holes],
            },
            "observation_timestamp_s": self.observation_timestamp_s,
            "processing_timestamp_s": self.processing_timestamp_s,
            "freshness": self.freshness, "source_region_id": self.source_region_id,
            "source_room_id": self.source_room_id,
            "objects": [obj.as_dict() for obj in self.objects],
            "membership_provenance": self.membership_provenance,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RegionEvidence":
        allowed = {"region_id", "region_kind", "geometry_revision", "evidence_revision", "source_frame_id",
                   "pose_context", "coordinate_frame", "polygon", "observation_timestamp_s", "processing_timestamp_s",
                   "freshness", "source_region_id", "source_room_id", "objects", "membership_provenance", "warnings"}
        required = {"region_id", "region_kind", "geometry_revision", "evidence_revision", "coordinate_frame",
                    "polygon", "observation_timestamp_s", "processing_timestamp_s", "freshness", "source_region_id",
                    "source_room_id", "objects", "membership_provenance"}
        if not isinstance(value, Mapping) or set(value) - allowed or not required.issubset(value):
            raise ContractError("invalid RegionEvidence serialization fields")
        polygon_value = value.get("polygon")
        polygon = None
        if polygon_value is not None:
            if not isinstance(polygon_value, Mapping) or set(polygon_value) - {"outer", "holes"} or "outer" not in polygon_value:
                raise ContractError("invalid serialized RegionEvidence polygon")
            polygon = Polygon2D(
                tuple(tuple(point) for point in polygon_value["outer"]),
                tuple(tuple(tuple(point) for point in ring) for ring in polygon_value.get("holes", ())),
            )
        objects = value["objects"]
        warnings = value.get("warnings", ())
        if not isinstance(objects, (list, tuple)) or not isinstance(warnings, (list, tuple)):
            raise ContractError("serialized RegionEvidence objects/warnings must be sequences")
        return cls(
            region_id=value["region_id"], region_kind=value["region_kind"],
            geometry_revision=value["geometry_revision"], evidence_revision=value["evidence_revision"],
            coordinate_frame=value["coordinate_frame"], polygon=polygon,
            observation_timestamp_s=value["observation_timestamp_s"], processing_timestamp_s=value["processing_timestamp_s"],
            freshness=value["freshness"], source_region_id=value["source_region_id"], source_room_id=value["source_room_id"],
            objects=tuple(RegionObjectEvidence.from_dict(item) for item in objects),
            membership_provenance=value["membership_provenance"], warnings=tuple(warnings),
            source_frame_id=value.get("source_frame_id"), pose_context=value.get("pose_context"),
        )
