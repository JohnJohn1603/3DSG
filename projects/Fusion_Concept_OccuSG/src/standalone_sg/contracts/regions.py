"""ROS-free typed contracts for raw and tracked 2D regions."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from numbers import Real
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from .errors import ContractError

Point2 = tuple[float, float]
Ring2D = tuple[Point2, ...]


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ContractError(f"{label} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{label} must be finite")
    return result


def _ring(value: Sequence[Sequence[Real]], label: str) -> Ring2D:
    if isinstance(value, (str, bytes)):
        raise ContractError(f"{label} must be a sequence of XY points")
    result: list[Point2] = []
    for index, point in enumerate(value):
        if isinstance(point, (str, bytes)) or len(point) != 2:
            raise ContractError(f"{label}[{index}] must contain exactly x and y")
        result.append((_finite(point[0], f"{label}[{index}].x"), _finite(point[1], f"{label}[{index}].y")))
    if len(result) < 3:
        raise ContractError(f"{label} must contain at least three points")
    return tuple(result)


def ring_area(ring: Ring2D) -> float:
    return abs(
        sum(
            ring[i][0] * ring[(i + 1) % len(ring)][1]
            - ring[(i + 1) % len(ring)][0] * ring[i][1]
            for i in range(len(ring))
        )
        * 0.5
    )


@dataclass(frozen=True, slots=True)
class Polygon2D:
    """Metric polygon; rings may be open or explicitly closed."""

    outer: Ring2D
    holes: tuple[Ring2D, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "outer", _ring(self.outer, "polygon.outer"))
        normalized_holes = tuple(_ring(hole, f"polygon.holes[{i}]") for i, hole in enumerate(self.holes))
        object.__setattr__(self, "holes", normalized_holes)

    @property
    def net_area_m2(self) -> float:
        return max(0.0, ring_area(self.outer) - sum(ring_area(hole) for hole in self.holes))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = [point[0] for point in self.outer]
        ys = [point[1] for point in self.outer]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True, slots=True)
class RegionObservation:
    """One raw segmentation result; raw_id is local to this snapshot."""

    raw_id: int | str
    polygon: Polygon2D
    timestamp_s: float
    source_grid_revision: int
    centroid_xy: Point2
    area_m2: float | None = None
    convex_hull_xy: Ring2D | None = None
    adjacent_raw_ids: tuple[int | str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.raw_id) not in (int, str):
            raise ContractError("region raw_id must be int or str")
        object.__setattr__(self, "timestamp_s", _finite(self.timestamp_s, "region.timestamp_s"))
        if type(self.source_grid_revision) is not int or self.source_grid_revision < 0:
            raise ContractError("region.source_grid_revision must be a non-negative integer")
        if len(self.centroid_xy) != 2:
            raise ContractError("region.centroid_xy must contain x and y")
        object.__setattr__(
            self,
            "centroid_xy",
            (_finite(self.centroid_xy[0], "region.centroid_xy.x"), _finite(self.centroid_xy[1], "region.centroid_xy.y")),
        )
        if self.area_m2 is not None:
            area = _finite(self.area_m2, "region.area_m2")
            object.__setattr__(self, "area_m2", area)
        if self.convex_hull_xy is not None:
            object.__setattr__(self, "convex_hull_xy", _ring(self.convex_hull_xy, "region.convex_hull_xy"))
        if any(type(raw_id) not in (int, str) for raw_id in self.adjacent_raw_ids):
            raise ContractError("region adjacency IDs must be int or str")
        object.__setattr__(self, "adjacent_raw_ids", tuple(self.adjacent_raw_ids))
        metadata = dict(self.metadata)
        if any(type(key) is not str for key in metadata):
            raise ContractError("region metadata keys must be strings")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    @property
    def reference_area_m2(self) -> float:
        """Mirror BasicRegionTracker: positive message area, else shell area."""
        if self.area_m2 is not None and self.area_m2 > 0.0:
            return self.area_m2
        return ring_area(self.polygon.outer)

    @property
    def reference_bounds(self) -> tuple[float, float, float, float]:
        ring = self.convex_hull_xy if self.convex_hull_xy is not None and len(self.convex_hull_xy) >= 3 else self.polygon.outer
        xs = [point[0] for point in ring]
        ys = [point[1] for point in ring]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True, slots=True)
class TrackedRegion:
    """Latest retained geometry for a canonical persistent ID."""

    persistent_id: int
    polygon: Polygon2D
    centroid_xy: Point2
    area_m2: float
    first_seen_s: float
    last_seen_s: float
    miss_count: int
    status: Literal["current", "temporarily_missing"]
    last_raw_id: int | str
    last_source_grid_revision: int


@dataclass(frozen=True, slots=True)
class CanonicalRegion:
    """Region in the current/published snapshot; absent tracks are not included."""

    persistent_id: int | None
    raw_id: int | str
    polygon: Polygon2D
    centroid_xy: Point2
    area_m2: float
    timestamp_s: float
    source_grid_revision: int
    adjacent_region_ids: tuple[int | str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GuardDecision:
    accepted: bool
    reason: str | None
    reasons: tuple[str, ...]
    region_count: int
    total_region_area_m2: float
    unknown_ratio: float
    free_ratio: float
    stale: bool


@dataclass(frozen=True, slots=True)
class RegionUpdateResult:
    accepted: bool
    reason: str | None
    stale: bool
    current_regions: tuple[CanonicalRegion, ...]
    retained_tracks: tuple[TrackedRegion, ...]
    matched_ids: tuple[int, ...] = ()
    new_ids: tuple[int, ...] = ()
    expired_ids: tuple[int, ...] = ()
    guard: GuardDecision | None = None
