"""ROS-free contracts for semantic detections and their provider provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from numbers import Real
from types import MappingProxyType
from typing import Mapping, Sequence

from .errors import ContractError


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ContractError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{name} must be finite")
    return result

def _text_or_none(value: object, name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ContractError(f"{name} must be a string or None")
    value = value.strip()
    return value or None

def _metadata(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(k) is not str for k in value):
        raise ContractError(f"{name} must be a string-keyed mapping")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class Detection3D:
    """One provider detection with center already expressed in canonical world coordinates.

    ``confidence`` is preserved as a raw provider score; it is not assumed to be
    calibrated probability. ``class_name`` is the source-compatible association
    label. Provider adapters should map Detection3D.id, or the first hypothesis
    class_id when id is absent, to this field.
    """

    detection_id: str
    timestamp_s: float
    center_W: tuple[float, float, float]
    center_method: str
    class_name: str | None = None
    class_id: str | None = None
    confidence: float | None = None
    frame_id: str = "world"
    layer_id: str = "floor_0"
    extents_m: tuple[float, float, float] | None = None
    provider_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.detection_id) is not str or not self.detection_id.strip():
            raise ContractError("detection_id must be a non-empty string")
        object.__setattr__(self, "detection_id", self.detection_id.strip())
        object.__setattr__(self, "timestamp_s", _finite(self.timestamp_s, "detection.timestamp_s"))
        if len(self.center_W) != 3:
            raise ContractError("center_W must contain exactly three coordinates")
        object.__setattr__(self, "center_W", tuple(_finite(v, f"center_W[{i}]") for i, v in enumerate(self.center_W)))
        if type(self.center_method) is not str or not self.center_method.strip():
            raise ContractError("center_method must be a non-empty string")
        if type(self.frame_id) is not str or not self.frame_id.strip():
            raise ContractError("frame_id must be a non-empty string")
        if type(self.layer_id) is not str or not self.layer_id.strip():
            raise ContractError("layer_id must be a non-empty string")
        object.__setattr__(self, "class_name", _text_or_none(self.class_name, "class_name"))
        object.__setattr__(self, "class_id", _text_or_none(self.class_id, "class_id"))
        if self.confidence is not None:
            object.__setattr__(self, "confidence", _finite(self.confidence, "confidence"))
        if self.extents_m is not None:
            if len(self.extents_m) != 3:
                raise ContractError("extents_m must contain exactly three dimensions")
            extents = tuple(_finite(v, f"extents_m[{i}]") for i, v in enumerate(self.extents_m))
            if any(v < 0.0 for v in extents):
                raise ContractError("extents_m must be non-negative")
            object.__setattr__(self, "extents_m", extents)
        object.__setattr__(self, "provider_metadata", _metadata(self.provider_metadata, "provider_metadata"))


@dataclass(frozen=True, slots=True)
class DetectionBatch:
    """Provider result for one requested frame. None means absent; () means ran, no detections."""

    provider_id: str
    frame_id: str
    timestamp_s: float
    detections: tuple[Detection3D, ...] | None
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("provider_id", "frame_id"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ContractError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        object.__setattr__(self, "timestamp_s", _finite(self.timestamp_s, "batch.timestamp_s"))
        if self.detections is not None:
            detections = tuple(self.detections)
            if any(not isinstance(d, Detection3D) for d in detections):
                raise ContractError("detections must contain Detection3D values")
            ids = [d.detection_id for d in detections]
            if len(set(ids)) != len(ids):
                raise ContractError("detection_id values must be unique within a batch")
            object.__setattr__(self, "detections", detections)
        object.__setattr__(self, "provenance", _metadata(self.provenance, "batch.provenance"))

    @property
    def status(self) -> str:
        if self.detections is None:
            return "absent"
        return "empty" if not self.detections else "present"
