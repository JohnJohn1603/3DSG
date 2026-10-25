"""World XY to grid-index helpers shared by coordinate fixtures."""

from __future__ import annotations

import math

from ..contracts.errors import ContractError


def _validate_grid(origin_xy: tuple[float, float], resolution: float) -> None:
    if len(origin_xy) != 2 or not all(map(lambda value: isinstance(value, (int, float)), origin_xy)):
        raise ContractError("origin_xy must contain two numbers")
    if resolution <= 0:
        raise ContractError("resolution must be positive")


def world_xy_to_cell(
    x: float,
    y: float,
    *,
    origin_xy: tuple[float, float],
    resolution: float,
) -> tuple[int, int]:
    """Return ``(row, col)`` using floor, including for negative coordinates."""

    _validate_grid(origin_xy, resolution)
    return (
        math.floor((y - origin_xy[1]) / resolution),
        math.floor((x - origin_xy[0]) / resolution),
    )


def cell_center_xy(
    row: int,
    col: int,
    *,
    origin_xy: tuple[float, float],
    resolution: float,
) -> tuple[float, float]:
    """Return the physical center of ``values[row, col]``."""

    _validate_grid(origin_xy, resolution)
    return (
        origin_xy[0] + (col + 0.5) * resolution,
        origin_xy[1] + (row + 0.5) * resolution,
    )

