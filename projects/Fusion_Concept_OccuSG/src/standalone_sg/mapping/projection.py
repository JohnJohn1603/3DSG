"""Debug Python port of the pinned MapConversion projection algorithm.

This module ports behavior from ``MapConverter::updateMap``,
``HeightRangeMap`` and ``Map2D`` at source commit
``14b8cb9e6534ba21e84f624c94d20960f6613f3c``. It is useful for fixtures and
debugging; it is not the planned C++ reference/runtime backend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import Any, Iterable, Mapping

import numpy as np

from ..contracts.errors import ContractError


SOURCE_COMMIT = "14b8cb9e6534ba21e84f624c94d20960f6613f3c"


@dataclass(frozen=True, slots=True)
class FreeSpaceGrid2D:
    """World-anchored structural clearance grid.

    ``values`` is row-major ``[row_y, col_x]`` and uses the source convention
    ``-1=unknown, 0=free, 1..100=neighbour occupation``.  Occupation is kept
    as an intermediate score; callers must not reinterpret every positive
    value as a traversability/collision result.
    """

    values: np.ndarray
    resolution_m: float
    origin_xy: tuple[float, float]
    z_floor: np.ndarray
    z_ceiling: np.ndarray
    revision: int
    frame_id: str = "world"
    kind: str = "structural_clearance"
    changed_bounds_xy: tuple[float, float, float, float] | None = None
    changed_bounds_xyz: tuple[float, float, float, float, float, float] | None = None
    projection_mode: str | None = None
    updated: bool | None = None
    source: str = f"MapConverter:{SOURCE_COMMIT}"
    backend: str = "python_port_debug"

    def __post_init__(self) -> None:
        values = np.asarray(self.values)
        z_floor = np.asarray(self.z_floor, dtype=np.float64)
        z_ceiling = np.asarray(self.z_ceiling, dtype=np.float64)
        if values.ndim != 2 or z_floor.shape != values.shape or z_ceiling.shape != values.shape:
            raise ContractError("FreeSpaceGrid2D arrays must have matching HxW shapes")
        if values.dtype.kind not in "iu" or np.any(values < -1) or np.any(values > 100):
            raise ContractError("FreeSpaceGrid2D.values must contain int8 values in -1..100")
        if not math.isfinite(float(self.resolution_m)) or self.resolution_m <= 0:
            raise ContractError("FreeSpaceGrid2D resolution must be finite and positive")
        if len(self.origin_xy) != 2 or not all(math.isfinite(float(v)) for v in self.origin_xy):
            raise ContractError("FreeSpaceGrid2D origin_xy must contain two finite numbers")
        if type(self.revision) is not int or self.revision < 0:
            raise ContractError("FreeSpaceGrid2D revision must be a non-negative integer")
        if self.projection_mode not in {None, "full_refresh", "partial_update"}:
            raise ContractError("FreeSpaceGrid2D projection_mode is invalid")
        if self.updated is not None and type(self.updated) is not bool:
            raise ContractError("FreeSpaceGrid2D updated must be boolean or None")
        if self.changed_bounds_xy is not None and (
            len(self.changed_bounds_xy) != 4
            or not all(math.isfinite(float(value)) for value in self.changed_bounds_xy)
        ):
            raise ContractError("FreeSpaceGrid2D changed_bounds_xy must contain four finite numbers")
        if self.changed_bounds_xyz is not None and (
            len(self.changed_bounds_xyz) != 6
            or not all(math.isfinite(float(value)) for value in self.changed_bounds_xyz)
        ):
            raise ContractError("FreeSpaceGrid2D changed_bounds_xyz must contain six finite numbers")
        object.__setattr__(self, "values", values.astype(np.int8, copy=True))
        object.__setattr__(self, "z_floor", z_floor.copy())
        object.__setattr__(self, "z_ceiling", z_ceiling.copy())

    @property
    def shape(self) -> tuple[int, int]:
        return self.values.shape


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ContractError(f"{name} must be finite")
    return float(value)


def _leaf_value(leaf: Any, name: str) -> Any:
    if isinstance(leaf, Mapping):
        if name not in leaf:
            raise ContractError(f"occupancy leaf is missing {name}")
        return leaf[name]
    if not hasattr(leaf, name):
        raise ContractError(f"occupancy leaf is missing {name}")
    return getattr(leaf, name)


def _normalise_leaves(leaves: Iterable[Any]) -> list[tuple[float, float, float, float, bool]]:
    result: list[tuple[float, float, float, float, bool]] = []
    for index, leaf in enumerate(leaves):
        center = _leaf_value(leaf, "center")
        if len(center) != 3:
            raise ContractError(f"occupancy leaf {index} center must have length 3")
        x, y, z = (_finite(value, f"leaf[{index}].center[{axis}]") for axis, value in enumerate(center))
        size = _finite(_leaf_value(leaf, "size"), f"leaf[{index}].size")
        if size <= 0:
            raise ContractError(f"leaf[{index}].size must be positive")
        occupied = _leaf_value(leaf, "occupied")
        if type(occupied) is not bool:
            raise ContractError(f"leaf[{index}].occupied must be boolean")
        result.append((x, y, z, size, occupied))
    return result


class _HeightRangeMap:
    """Small faithful port of HeightRangeMap's range and occupation methods."""

    def __init__(self, width: int, height: int) -> None:
        self.free: list[list[list[tuple[float, float]]]] = [
            [[] for _ in range(height)] for _ in range(width)
        ]
        self.occupied: list[list[list[tuple[float, float]]]] = [
            [[] for _ in range(height)] for _ in range(width)
        ]

    @staticmethod
    def _insert_range(value: tuple[float, float], ranges: list[tuple[float, float]]) -> None:
        top, bottom = value
        for index, (old_top, old_bottom) in enumerate(ranges):
            if top < old_bottom or bottom > old_top:
                continue
            ranges.pop(index)
            _HeightRangeMap._insert_range(
                (max(old_top, top), min(old_bottom, bottom)), ranges
            )
            return
        # Match HeightRangeMap::inserthghtRange: ranges are descending by Z.
        for index, (old_top, _) in enumerate(ranges):
            if bottom <= old_top:
                continue
            ranges.insert(index, value)
            return
        ranges.append(value)

    def add_range(self, x: int, y: int, value: tuple[float, float], occupied: bool) -> None:
        if x < 0 or x >= len(self.free) or y < 0 or y >= len(self.free[x]):
            return
        self._insert_range(value, (self.occupied if occupied else self.free)[x][y])

    def remove_short_free_ranges(self, x: int, y: int, minimum_z: float) -> None:
        self.free[x][y][:] = [
            value for value in self.free[x][y] if value[0] - value[1] > minimum_z
        ]

    def _occupation(self, free_range: tuple[float, float], occupied: list[tuple[float, float]], minimum: int) -> int:
        top, bottom = free_range
        overlap = 0.0
        for occupied_top, occupied_bottom in occupied:
            compared = min(top, occupied_top) - max(bottom, occupied_bottom)
            if compared >= 0:
                overlap += compared
        if top <= bottom:
            return -1
        score = int(overlap / (top - bottom) * 100.0) * 2
        return -1 if score < minimum else min(score, 100)

    def neighbour_occupation(self, x: int, y: int, minimum: int) -> int:
        best = -1
        for dx, dy in ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)):
            nx, ny = x + dx, y + dy
            if nx < 0 or nx >= len(self.free) or ny < 0 or ny >= len(self.free[nx]):
                continue
            if not self.free[nx][ny]:
                continue
            free_range = self.free[nx][ny][-1]
            best = max(best, self._occupation(free_range, self.occupied[x][y], minimum))
        return best


def project_leaves(
    leaves: Iterable[Any],
    *,
    resolution_m: float,
    min_free_height_m: float = 0.3,
    minimum_occupancy: int = 5,
    interval_epsilon_fraction: float = 0.001,
    bounds_xy: tuple[float, float, float, float] | None = None,
    revision: int = 0,
    frame_id: str = "world",
    changed_bounds_xy: tuple[float, float, float, float] | None = None,
) -> FreeSpaceGrid2D:
    """Debug-project accumulated OcTree leaves using a Python port of pinned MC."""

    resolution = _finite(resolution_m, "resolution_m")
    minimum_z = _finite(min_free_height_m, "min_free_height_m")
    epsilon_fraction = _finite(interval_epsilon_fraction, "interval_epsilon_fraction")
    if resolution <= 0 or minimum_z < 0 or epsilon_fraction < 0:
        raise ContractError("resolution_m must be positive; projection values must be non-negative")
    if type(minimum_occupancy) is not int or not 0 <= minimum_occupancy <= 100:
        raise ContractError("minimum_occupancy must be an integer in 0..100")
    if type(revision) is not int or revision < 0:
        raise ContractError("revision must be a non-negative integer")

    normalised = _normalise_leaves(leaves)
    if not normalised:
        raise ContractError("cannot project an empty occupancy leaf set")
    if bounds_xy is None:
        bounds_xy = (
            min(item[0] for item in normalised),
            max(item[0] for item in normalised),
            min(item[1] for item in normalised),
            max(item[1] for item in normalised),
        )
    if len(bounds_xy) != 4 or not all(math.isfinite(float(value)) for value in bounds_xy):
        raise ContractError("bounds_xy must contain four finite numbers")
    min_x, max_x, min_y, max_y = map(float, bounds_xy)
    if max_x < min_x or max_y < min_y:
        raise ContractError("bounds_xy min values must not exceed max values")
    width = int(math.ceil((max_x - min_x) / resolution)) + 1
    height = int(math.ceil((max_y - min_y) / resolution)) + 1
    if width <= 0 or height <= 0 or width > 100000 or height > 100000:
        raise ContractError("projection bounds produce unreasonable grid dimensions")

    ranges = _HeightRangeMap(width, height)
    epsilon = resolution * epsilon_fraction
    for center_x, center_y, center_z, size, occupied in normalised:
        # The pinned dependency stores voxel::halfSize as float. Keep that
        # conversion because floor() at XY voxel boundaries changes cells at
        # 0.05 m when the value is held in Python float64.
        half_size = float(np.float32(size / 2.0))
        size_index = max(1, int(math.ceil((half_size * 2.0) / resolution)))
        height_range = (center_z + half_size + epsilon, center_z - half_size - epsilon)
        for offset_x in range(size_index):
            for offset_y in range(size_index):
                cell_x = math.floor((center_x - half_size + resolution * offset_x - min_x) / resolution)
                cell_y = math.floor((center_y - half_size + resolution * offset_y - min_y) / resolution)
                ranges.add_range(cell_x, cell_y, height_range, occupied)

    values = np.full((height, width), -1, dtype=np.int8)
    z_floor = np.full((height, width), np.nan, dtype=np.float64)
    z_ceiling = np.full((height, width), np.nan, dtype=np.float64)
    for cell_x in range(1, width - 1):
        for cell_y in range(1, height - 1):
            ranges.remove_short_free_ranges(cell_x, cell_y, minimum_z)
            if not ranges.free[cell_x][cell_y]:
                continue
            values[cell_y, cell_x] = 0
            z_floor[cell_y, cell_x] = ranges.free[cell_x][cell_y][-1][1]
            z_ceiling[cell_y, cell_x] = ranges.free[cell_x][cell_y][0][0]

    for cell_x in range(2, width - 2):
        for cell_y in range(2, height - 2):
            if not ranges.free[cell_x][cell_y]:
                continue
            for dx, dy in ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)):
                nx, ny = cell_x + dx, cell_y + dy
                if not ranges.free[nx][ny]:
                    values[ny, nx] = ranges.neighbour_occupation(nx, ny, minimum_occupancy)

    return FreeSpaceGrid2D(
        values=values,
        resolution_m=resolution,
        origin_xy=(min_x, min_y),
        z_floor=z_floor,
        z_ceiling=z_ceiling,
        revision=revision,
        frame_id=frame_id,
        changed_bounds_xy=changed_bounds_xy,
    )


def project_snapshot(
    snapshot: Mapping[str, Any],
    *,
    min_free_height_m: float = 0.3,
    minimum_occupancy: int = 5,
    interval_epsilon_fraction: float = 0.001,
    frame_id: str = "world",
) -> FreeSpaceGrid2D:
    """Full-refresh the Python debug grid from an accumulated M2 snapshot.

    Snapshot bounds describe the full map extent, not the latest delta; they
    are therefore not reported as ``changed_bounds_xy``.
    """

    for key in ("leaves", "resolution", "revision", "bounds_min", "bounds_max"):
        if key not in snapshot:
            raise ContractError(f"occupancy snapshot is missing {key}")
    bounds_min, bounds_max = snapshot["bounds_min"], snapshot["bounds_max"]
    if len(bounds_min) != 3 or len(bounds_max) != 3:
        raise ContractError("occupancy snapshot bounds must have length 3")
    bounds = (bounds_min[0], bounds_max[0], bounds_min[1], bounds_max[1])
    return project_leaves(
        snapshot["leaves"],
        resolution_m=snapshot["resolution"],
        min_free_height_m=min_free_height_m,
        minimum_occupancy=minimum_occupancy,
        interval_epsilon_fraction=interval_epsilon_fraction,
        bounds_xy=bounds,
        revision=snapshot["revision"],
        frame_id=frame_id,
        # Snapshot bounds describe full-map extent, not the latest delta.
        changed_bounds_xy=None,
    )
