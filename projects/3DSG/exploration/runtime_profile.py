"""Validated YAML configuration loader for the navigation runtime."""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any


def load_runtime_profile(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path).expanduser().resolve(strict=True)
    text = profile_path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        data = _parse_yaml_mapping_subset(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict) or data.get("schema") != "frontier-cpu-profile/v1":
        raise ValueError("unsupported or malformed navigation profile")
    for section in ("runtime", "camera", "navigation", "keyframes", "resources"):
        if not isinstance(data.get(section), dict):
            raise ValueError(f"profile section {section!r} must be a mapping")

    _positive(data["runtime"], "exploration_wall_budget_s")
    _positive(data["runtime"], "shutdown_timeout_s")
    _positive(data["camera"], "target_fps")
    resources = data["resources"]
    for key in ("target_process_tree_rss_gib", "warning_mem_available_gib",
                "stop_mem_available_gib", "stop_mem_grace_s", "sample_period_s"):
        _positive(resources, key)
    if float(resources["stop_mem_available_gib"]) >= float(resources["warning_mem_available_gib"]):
        raise ValueError("resources.stop_mem_available_gib must be below warning_mem_available_gib")
    nav = data["navigation"]
    for key in ("async_mapping", "async_planning"):
        if type(nav.get(key)) is not bool:
            raise ValueError(f"navigation.{key} must be a boolean")
    for key in (
        "grid_resolution_m", "map_update_hz", "planner_hz", "control_hz",
        "nominal_speed_mps", "max_speed_mps", "max_yaw_degps",
        "acceleration_mps2", "yaw_acceleration_degps2", "depth_stale_s",
        "nav_stale_s", "min_frontier_length_m", "min_viewpoint_offset_m",
        "max_viewpoint_offset_m", "lookahead_m", "stop_distance_m",
        "initial_scan_yaw_deg", "initial_scan_rate_degps", "collision_recovery_yaw_deg",
    ):
        _positive(nav, key)
    for key in ("depth_stride",):
        if type(nav.get(key)) is not int or nav[key] < 1:
            raise ValueError(f"navigation.{key} must be a positive integer")
    if "max_viewpoints_per_frontier" in nav:
        value = nav["max_viewpoints_per_frontier"]
        if type(value) is not int or not 1 <= value <= 8:
            raise ValueError("navigation.max_viewpoints_per_frontier must be an integer in [1, 8]")
    if "min_viewpoint_separation_m" in nav:
        _positive(nav, "min_viewpoint_separation_m")
    for key in ("inflation_margin_m", "goal_hold_s"):
        value = float(nav.get(key, math.nan))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"navigation.{key} must be finite and non-negative")
    for key in ("visited_viewpoint_radius_m", "failed_viewpoint_radius_m"):
        value = float(nav.get(key, 0.0))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"navigation.{key} must be finite and non-negative")
    for key in ("progress_timeout_s", "progress_min_distance_m", "obstacle_block_timeout_s"):
        if key in nav:
            _positive(nav, key)
    improvement = float(nav.get("goal_switch_improvement", math.nan))
    if not math.isfinite(improvement) or not 0.0 <= improvement < 1.0:
        raise ValueError("navigation.goal_switch_improvement must be in [0, 1)")
    if nav["nominal_speed_mps"] > nav["max_speed_mps"]:
        raise ValueError("nominal_speed_mps cannot exceed max_speed_mps")
    if nav["max_viewpoint_offset_m"] < nav["min_viewpoint_offset_m"]:
        raise ValueError("max viewpoint offset must be at least min viewpoint offset")
    keyframes = data["keyframes"]
    for key in ("min_translation_m", "min_yaw_deg", "raw_pending_byte_cap_mib"):
        _positive(keyframes, key)
    for key, lower, upper in (("max_pending", 1, 2), ("max_protected_pending", 0, 1)):
        value = keyframes.get(key)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"keyframes.{key} must be an integer in [{lower}, {upper}]")
    if keyframes["max_protected_pending"] > keyframes["max_pending"]:
        raise ValueError("keyframes.max_protected_pending cannot exceed max_pending")
    return data


def keyframe_runtime_settings(profile: dict[str, Any]) -> dict[str, float | int]:
    """Return validated keyframe policy and bounded scheduler settings."""
    keyframes = profile["keyframes"]
    return {
        "min_translation_m": float(keyframes["min_translation_m"]),
        "min_yaw_deg": float(keyframes["min_yaw_deg"]),
        "max_pending": int(keyframes["max_pending"]),
        "max_protected_pending": int(keyframes["max_protected_pending"]),
        "raw_pending_byte_cap": int(float(keyframes["raw_pending_byte_cap_mib"]) * 1024 ** 2),
    }


def m2_transport_metadata(*, enabled: bool, requested: str | None) -> dict[str, str | None]:
    """Record transport selection separately from per-frame worker evidence."""
    if not enabled:
        return {
            "occusg_m2_transport_requested": None,
            "occusg_m2_transport_effective": None,
            "occusg_m2_transport_effective_source": None,
        }
    return {
        "occusg_m2_transport_requested": requested or "ascii",
        "occusg_m2_transport_effective": None,
        "occusg_m2_transport_effective_source": "per_frame_worker_diagnostics",
    }


def _positive(section: dict[str, Any], key: str) -> float:
    try:
        value = float(section[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"profile value {key!r} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"profile value {key!r} must be a finite positive number")
    return value


def _parse_yaml_mapping_subset(text: str) -> dict[str, Any]:
    """Parse this profile's strict indentation-based mapping/scalar subset.

    It intentionally rejects sequences, tabs, duplicate keys and inline YAML
    features. This keeps the Habitat environment dependency-light while
    avoiding a general-purpose, unsafe YAML implementation.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line[:indent] or indent % 2:
            raise ValueError(f"profile YAML indentation must use pairs of spaces (line {line_number})")
        content = raw_line.strip()
        if ":" not in content:
            raise ValueError(f"profile YAML supports mapping entries only (line {line_number})")
        key, raw_value = (part.strip() for part in content.split(":", 1))
        if not key or key.startswith("-"):
            raise ValueError(f"invalid profile YAML mapping key on line {line_number}")
        while stack[-1][0] >= indent:
            stack.pop()
        expected_indent = 0 if stack[-1][0] < 0 else stack[-1][0] + 2
        if indent != expected_indent:
            raise ValueError(f"profile YAML has a missing parent level on line {line_number}")
        parent = stack[-1][1]
        if key in parent:
            raise ValueError(f"duplicate profile YAML key {key!r} on line {line_number}")
        if raw_value:
            parent[key] = _parse_scalar(raw_value, line_number)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def _parse_scalar(value: str, line_number: int) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?[0-9]+", value):
        return int(value)
    try:
        number = float(value)
    except ValueError:
        if value.startswith(("[", "{", "&", "*", "!", "|", ">")):
            raise ValueError(f"unsupported YAML scalar on line {line_number}")
        return value
    if math.isfinite(number):
        return number
    raise ValueError(f"non-finite profile scalar on line {line_number}")
