"""Typed public boundary objects for standalone-sg."""

from .errors import ContractError
from .frames import CameraIntrinsics, PointCloud, RGBDFrame, Transform
from .objects import DetectionBatch, Detection3D
from .regions import (
    CanonicalRegion,
    GuardDecision,
    Polygon2D,
    RegionObservation,
    RegionUpdateResult,
    TrackedRegion,
)

__all__ = [
    "CameraIntrinsics",
    "ContractError",
    "PointCloud",
    "RGBDFrame",
    "Transform",
    "Detection3D",
    "DetectionBatch",
    "GuardDecision",
    "CanonicalRegion",
    "Polygon2D",
    "RegionObservation",
    "RegionUpdateResult",
    "TrackedRegion",
]
