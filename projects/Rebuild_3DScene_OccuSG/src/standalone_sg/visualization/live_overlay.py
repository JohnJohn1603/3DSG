"""Headless renderer for the Webots M3 live overlay.

The renderer consumes the ``FreeSpaceGrid2D`` returned by native M3.  It does
not inspect the Webots world, collision AABBs, room ground truth, or the
OctoMap.  Positive grid values are rendered as the MapConverter neighbour
occupation score (0..100), not as probabilities or traversability.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Iterable, Mapping

import numpy as np

from ..contracts.errors import ContractError


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise ContractError(f"{name} must be finite")
    return float(value)


def _color(value: object, name: str) -> tuple[int, int, int]:
    if type(value) is int and 0 <= value <= 0xFFFFFF:
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(type(v) is int and 0 <= v <= 255 for v in value):
        return tuple(int(v) for v in value)
    raise ContractError(f"{name} must be an RGB integer or a 3-item RGB list")


@dataclass(frozen=True, slots=True)
class LiveVisualizationConfig:
    enabled: bool = False
    display_name: str = "m12_overlay"
    width: int = 640
    height: int = 480
    publish_max_hz: float = 5.0
    render_max_hz: float = 10.0
    stale_timeout_s: float = 2.0
    finish_behavior: str = "hold"
    hold_after_finish_s: float = 5.0
    connection_timeout_s: float = 0.5
    max_payload_bytes: int = 640 * 480 * 3
    show_unknown: bool = True
    show_free: bool = True
    show_occupied: bool = True
    show_trajectory: bool = True
    show_robot: bool = True
    show_rooms: bool = False
    unknown_color: tuple[int, int, int] = (128, 128, 128)
    free_color: tuple[int, int, int] = (245, 245, 245)
    occupied_low_color: tuple[int, int, int] = (190, 190, 190)
    occupied_high_color: tuple[int, int, int] = (15, 15, 15)
    trajectory_color: tuple[int, int, int] = (30, 110, 230)
    robot_color: tuple[int, int, int] = (220, 45, 35)
    background_color: tuple[int, int, int] = (35, 40, 48)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "LiveVisualizationConfig":
        raw = {} if value is None else dict(value)
        allowed = {
            "enabled", "display_name", "width", "height", "publish_max_hz", "render_max_hz",
            "stale_timeout_s", "finish_behavior", "hold_after_finish_s", "connection_timeout_s",
            "max_payload_bytes", "layers", "colors",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ContractError(f"unknown visualization config key(s): {', '.join(unknown)}")
        layers = raw.get("layers", {})
        colors = raw.get("colors", {})
        if not isinstance(layers, Mapping) or not isinstance(colors, Mapping):
            raise ContractError("visualization.layers and visualization.colors must be mappings")
        layer_allowed = {"unknown", "free", "occupied", "trajectory", "robot", "rooms"}
        color_allowed = {"unknown", "free", "occupied_low", "occupied_high", "trajectory", "robot", "background"}
        bad_layers = sorted(set(layers) - layer_allowed)
        bad_colors = sorted(set(colors) - color_allowed)
        if bad_layers or bad_colors:
            raise ContractError("unknown visualization layer/color key")
        defaults = cls()
        result = cls(
            enabled=value_or(raw, "enabled", defaults.enabled, bool),
            display_name=value_or(raw, "display_name", defaults.display_name, str),
            width=value_or(raw, "width", defaults.width, int),
            height=value_or(raw, "height", defaults.height, int),
            publish_max_hz=_finite(raw.get("publish_max_hz", defaults.publish_max_hz), "visualization.publish_max_hz"),
            render_max_hz=_finite(raw.get("render_max_hz", defaults.render_max_hz), "visualization.render_max_hz"),
            stale_timeout_s=_finite(raw.get("stale_timeout_s", defaults.stale_timeout_s), "visualization.stale_timeout_s"),
            finish_behavior=value_or(raw, "finish_behavior", defaults.finish_behavior, str),
            hold_after_finish_s=_finite(raw.get("hold_after_finish_s", defaults.hold_after_finish_s), "visualization.hold_after_finish_s"),
            connection_timeout_s=_finite(raw.get("connection_timeout_s", defaults.connection_timeout_s), "visualization.connection_timeout_s"),
            max_payload_bytes=value_or(raw, "max_payload_bytes", defaults.max_payload_bytes, int),
            show_unknown=value_or(layers, "unknown", defaults.show_unknown, bool),
            show_free=value_or(layers, "free", defaults.show_free, bool),
            show_occupied=value_or(layers, "occupied", defaults.show_occupied, bool),
            show_trajectory=value_or(layers, "trajectory", defaults.show_trajectory, bool),
            show_robot=value_or(layers, "robot", defaults.show_robot, bool),
            show_rooms=value_or(layers, "rooms", defaults.show_rooms, bool),
            unknown_color=_color(colors.get("unknown", defaults.unknown_color), "visualization.colors.unknown"),
            free_color=_color(colors.get("free", defaults.free_color), "visualization.colors.free"),
            occupied_low_color=_color(colors.get("occupied_low", defaults.occupied_low_color), "visualization.colors.occupied_low"),
            occupied_high_color=_color(colors.get("occupied_high", defaults.occupied_high_color), "visualization.colors.occupied_high"),
            trajectory_color=_color(colors.get("trajectory", defaults.trajectory_color), "visualization.colors.trajectory"),
            robot_color=_color(colors.get("robot", defaults.robot_color), "visualization.colors.robot"),
            background_color=_color(colors.get("background", defaults.background_color), "visualization.colors.background"),
        )
        if result.width < 32 or result.height < 32 or result.width * result.height > 4096 * 4096:
            raise ContractError("visualization display dimensions are unreasonable")
        if result.publish_max_hz <= 0 or result.render_max_hz <= 0 or result.stale_timeout_s < 0:
            raise ContractError("visualization frequencies and stale timeout must be positive")
        if result.hold_after_finish_s < 0 or result.connection_timeout_s <= 0:
            raise ContractError("visualization finish/connection timings are invalid")
        if result.finish_behavior not in {"hold", "close"}:
            raise ContractError("visualization.finish_behavior must be hold or close")
        if result.max_payload_bytes < result.width * result.height * 3:
            raise ContractError("visualization.max_payload_bytes is smaller than one RGB frame")
        return result


def value_or(mapping: Mapping[str, object], key: str, default: object, expected: type):
    value = mapping.get(key, default)
    if type(value) is not expected:
        raise ContractError(f"visualization.{key} must be {expected.__name__}")
    return value


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], amount: np.ndarray) -> np.ndarray:
    left = np.asarray(a, dtype=np.float32)
    right = np.asarray(b, dtype=np.float32)
    return np.rint(left[None, None, :] * (1.0 - amount[..., None]) + right[None, None, :] * amount[..., None]).astype(np.uint8)


def _line(image: np.ndarray, p0: tuple[int, int], p1: tuple[int, int], color: tuple[int, int, int], width: int = 2) -> None:
    x0, y0 = p0
    x1, y1 = p1
    count = max(abs(x1 - x0), abs(y1 - y0), 1) + 1
    xs = np.rint(np.linspace(x0, x1, count)).astype(int)
    ys = np.rint(np.linspace(y0, y1, count)).astype(int)
    half = max(0, width // 2)
    for x, y in zip(xs, ys):
        x0c, x1c = max(0, x - half), min(image.shape[1], x + half + 1)
        y0c, y1c = max(0, y - half), min(image.shape[0], y + half + 1)
        image[y0c:y1c, x0c:x1c] = color


def _pixel(point: tuple[float, float], view: tuple[float, float, float, float, float, float, float]) -> tuple[int, int]:
    xmin, ymin, ymax, scale, left, top, _height = view
    x, y = point
    return int(round(left + (x - xmin) * scale)), int(round(top + (ymax - y) * scale))


def render_grid_overlay(
    grid,
    *,
    base_xy: tuple[float, float] | None,
    yaw_rad: float | None,
    trajectory_xy: Iterable[tuple[float, float]] = (),
    config: LiveVisualizationConfig | None = None,
) -> tuple[bytes, dict]:
    """Render one M3 grid and processed poses as an RGB888 Webots image."""
    cfg = config or LiveVisualizationConfig(enabled=True)
    image = np.empty((cfg.height, cfg.width, 3), dtype=np.uint8)
    image[:] = cfg.background_color
    trajectory = [(float(x), float(y)) for x, y in trajectory_xy]
    points = list(trajectory)
    if base_xy is not None:
        points.append((float(base_xy[0]), float(base_xy[1])))
    grid_extent = None
    if grid is not None:
        h, w = grid.shape
        gx0, gy0 = (float(grid.origin_xy[0]), float(grid.origin_xy[1]))
        gx1, gy1 = gx0 + w * float(grid.resolution_m), gy0 + h * float(grid.resolution_m)
        grid_extent = (gx0, gy0, gx1, gy1)
        points.extend(((gx0, gy0), (gx1, gy1)))
    if not points:
        points = [(-1.0, -1.0), (1.0, 1.0)]
    xs, ys = zip(*points)
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    span = max(xmax - xmin, ymax - ymin, 1e-6)
    pad = max(0.5, 0.06 * span)
    xmin, xmax, ymin, ymax = xmin - pad, xmax + pad, ymin - pad, ymax + pad
    margin_px = 18.0
    scale = min((cfg.width - 2.0 * margin_px) / max(xmax - xmin, 1e-6), (cfg.height - 2.0 * margin_px) / max(ymax - ymin, 1e-6))
    scale = max(scale, 1.0)
    actual_w, actual_h = (xmax - xmin) * scale, (ymax - ymin) * scale
    left, top = (cfg.width - actual_w) / 2.0, (cfg.height - actual_h) / 2.0
    view = (xmin, ymin, ymax, scale, left, top, cfg.height)

    if grid is not None:
        values = np.asarray(grid.values, dtype=np.int16)
        colors = np.empty((*values.shape, 3), dtype=np.uint8)
        colors[:] = cfg.unknown_color
        if cfg.show_free:
            colors[values == 0] = cfg.free_color
        if cfg.show_occupied:
            positive = values > 0
            amount = np.clip(values.astype(np.float32) / 100.0, 0.0, 1.0)
            colors[positive] = _blend(cfg.occupied_low_color, cfg.occupied_high_color, amount)[positive]
        if not cfg.show_unknown:
            colors[values < 0] = cfg.background_color
        if not cfg.show_free:
            colors[values == 0] = cfg.background_color
        if not cfg.show_occupied:
            colors[values > 0] = cfg.background_color
        map_px_w = max(1, int(round((grid_extent[2] - grid_extent[0]) * scale)))
        map_px_h = max(1, int(round((grid_extent[3] - grid_extent[1]) * scale)))
        columns = np.rint(np.linspace(0, values.shape[1] - 1, map_px_w)).astype(int)
        # ``values`` is row-major [row_y, col_x] with row 0 at world +Y
        # origin.  Display pixel row 0 is the top, so +Y must be flipped.
        rows = np.rint(np.linspace(values.shape[0] - 1, 0, map_px_h)).astype(int)
        resized = colors[rows[:, None], columns[None, :]]
        map_left = int(round(left + (grid_extent[0] - xmin) * scale))
        map_top = int(round(top + (ymax - grid_extent[3]) * scale))
        x0, y0 = max(0, map_left), max(0, map_top)
        x1, y1 = min(cfg.width, map_left + map_px_w), min(cfg.height, map_top + map_px_h)
        if x1 > x0 and y1 > y0:
            image[y0:y1, x0:x1] = resized[y0 - map_top:y1 - map_top, x0 - map_left:x1 - map_left]

    if cfg.show_trajectory and len(trajectory) > 1:
        for first, second in zip(trajectory, trajectory[1:]):
            _line(image, _pixel(first, view), _pixel(second, view), cfg.trajectory_color, width=2)
    if cfg.show_robot and base_xy is not None:
        center = _pixel((float(base_xy[0]), float(base_xy[1])), view)
        radius = max(3, int(round(0.14 * scale)))
        image[max(0, center[1] - radius):min(cfg.height, center[1] + radius + 1),
              max(0, center[0] - radius):min(cfg.width, center[0] + radius + 1)] = cfg.robot_color
        if yaw_rad is not None and math.isfinite(float(yaw_rad)):
            tip = (float(base_xy[0]) + 0.42 * math.cos(float(yaw_rad)), float(base_xy[1]) + 0.42 * math.sin(float(yaw_rad)))
            _line(image, center, _pixel(tip, view), cfg.robot_color, width=3)

    # A small visual legend; labels and revision/timestamps are drawn by the
    # controller so that the metadata is always paired with the image frame.
    legend_y = max(0, cfg.height - 18)
    for index, color in enumerate((cfg.unknown_color, cfg.free_color, cfg.occupied_high_color)):
        x0 = 8 + index * 62
        image[legend_y:cfg.height - 5, x0:x0 + 14] = color
    diagnostics = {
        "grid_shape": None if grid is None else [int(grid.shape[0]), int(grid.shape[1])],
        "origin_xy": None if grid is None else [float(v) for v in grid.origin_xy],
        "resolution_m": None if grid is None else float(grid.resolution_m),
        "grid_semantics": "-1=unknown, 0=free, 1..100=MapConverter neighbour occupation score; not probability",
        "viewport_world_xy": [float(xmin), float(xmax), float(ymin), float(ymax)],
        "world_to_pixel_scale": float(scale),
        "trajectory_point_count": len(trajectory),
    }
    return image.tobytes(order="C"), diagnostics


__all__ = ["LiveVisualizationConfig", "render_grid_overlay"]
