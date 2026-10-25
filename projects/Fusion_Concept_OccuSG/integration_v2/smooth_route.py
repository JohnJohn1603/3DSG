"""Dependency-light route and keyframe primitives for the T1 Habitat preview.

This module deliberately does not know about object mapping or room semantics.
It only handles navigable path sampling, pose deltas, keyframe decisions, and a
bounded asynchronous sink for immutable raw keyframe packets.
"""
from __future__ import annotations

import math
import queue
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence


class RouteExecutionError(RuntimeError):
    """The requested route cannot be executed without violating its contract."""


def _clearance_value_status(value: float, query_radius: float) -> str:
    if not math.isfinite(value) or value < 0.0:
        raise RouteExecutionError("clearance report contains a non-finite or negative query value")
    try:
        cap_bytes = struct.pack("<f", float(query_radius))
        cap_f32 = struct.unpack("<f", cap_bytes)[0]
        cap_bits = struct.unpack("<I", cap_bytes)[0]
        previous_f32 = struct.unpack("<f", struct.pack("<I", max(0, cap_bits - 1)))[0]
    except (OverflowError, struct.error) as exc:
        raise RouteExecutionError("clearance query radius is not representable by Habitat's float32 API") from exc
    if not math.isfinite(cap_f32) or cap_f32 <= 0.0:
        raise RouteExecutionError("clearance query radius is not representable by Habitat's float32 API")
    # Habitat's PathFinder binding returns float32 distances. A value at the
    # representable query cap, or one float32 ULP below it, is indistinguishable
    # from the API's capped result and must remain a lower bound, not a measured
    # true minimum. This is numerical classification only; it does not affect
    # geometry acceptance or the physical clearance margin.
    cap_ulp = cap_f32 - previous_f32
    return "query_capped_lower_bound" if value >= cap_f32 - cap_ulp else "measured"


def _vec3(value: Sequence[float], label: str) -> tuple[float, float, float]:
    if len(value) != 3:
        raise ValueError(f"{label} must have three coordinates")
    result = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in result):
        raise ValueError(f"{label} must contain finite coordinates")
    return result  # type: ignore[return-value]


def distance3(a: Sequence[float], b: Sequence[float]) -> float:
    av, bv = _vec3(a, "a"), _vec3(b, "b")
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(av, bv)))


def wrap_angle_rad(angle: float) -> float:
    value = float(angle)
    if not math.isfinite(value):
        raise ValueError("angle must be finite")
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def yaw_delta_rad(source_yaw: float, target_yaw: float) -> float:
    return wrap_angle_rad(float(target_yaw) - float(source_yaw))


def yaw_toward_habitat_xz(source: Sequence[float], target: Sequence[float]) -> float:
    """Habitat yaw for a camera/base whose zero-yaw forward is world -Z."""
    sx, _, sz = _vec3(source, "source")
    tx, _, tz = _vec3(target, "target")
    dx, dz = tx - sx, tz - sz
    if math.hypot(dx, dz) <= 1e-9:
        raise ValueError("cannot derive yaw toward a coincident point")
    return math.atan2(-dx, -dz)


def sample_polyline(points: Iterable[Sequence[float]], max_step_m: float) -> list[tuple[float, float, float]]:
    """Sample every polyline segment at bounded spacing, preserving vertices."""
    path = [_vec3(point, "path point") for point in points]
    step = float(max_step_m)
    if not math.isfinite(step) or step <= 0.0:
        raise ValueError("max_step_m must be finite and positive")
    if len(path) < 2:
        raise ValueError("a route path needs at least two points")
    sampled = [path[0]]
    for start, end in zip(path, path[1:]):
        length = distance3(start, end)
        count = max(1, int(math.ceil(length / step)))
        for index in range(1, count + 1):
            t = index / count
            candidate = tuple(a + (b - a) * t for a, b in zip(start, end))
            if distance3(sampled[-1], candidate) > 1e-9:
                sampled.append(candidate)  # type: ignore[arg-type]
    return sampled


def find_waypoint_polyline(pathfinder: Any, waypoints: Iterable[Sequence[float]], *,
                           shortest_path_factory: Callable[[], Any],
                           vector_factory: Callable[..., Any],
                           endpoint_tolerance_m: float = 0.025) -> dict[str, Any]:
    """Resolve every consecutive waypoint pair and concatenate the paths.

    A single shortest-path query from the first point to the last point can
    silently skip intermediate route waypoints. This helper keeps the route
    contract explicit and returns segment provenance for the run manifest.
    """
    raw = [_vec3(point, "waypoint") for point in waypoints]
    tolerance = float(endpoint_tolerance_m)
    if len(raw) < 2:
        raise ValueError("a waypoint route needs at least two points")
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise ValueError("endpoint_tolerance_m must be finite and non-negative")
    combined: list[tuple[float, float, float]] = []
    segments: list[dict[str, Any]] = []
    total_geodesic = 0.0
    for index, (start, end) in enumerate(zip(raw, raw[1:])):
        shortest = shortest_path_factory()
        shortest.requested_start = vector_factory(*start)
        shortest.requested_end = vector_factory(*end)
        if not bool(pathfinder.find_path(shortest)):
            raise RouteExecutionError(f"no navmesh path for waypoint segment {index}: {start} -> {end}")
        segment = [_vec3(point, f"waypoint segment {index} path point") for point in shortest.points]
        if len(segment) < 2:
            raise RouteExecutionError(f"waypoint segment {index} returned fewer than two path points")
        start_error = distance3(segment[0], start)
        end_error = distance3(segment[-1], end)
        if start_error > tolerance or end_error > tolerance:
            raise RouteExecutionError(
                f"waypoint segment {index} endpoint mismatch: start_error={start_error:.6f} m, "
                f"end_error={end_error:.6f} m, tolerance={tolerance:.6f} m"
            )
        if not combined:
            combined.extend(segment)
        else:
            if distance3(combined[-1], segment[0]) > tolerance:
                raise RouteExecutionError(f"waypoint segment {index} does not join the previous segment")
            combined.extend(segment[1:])
        geodesic = float(getattr(shortest, "geodesic_distance", distance3(start, end)))
        total_geodesic += geodesic
        segments.append({
            "segment_index": index,
            "waypoint_start": list(start),
            "waypoint_end": list(end),
            "path_points": [list(point) for point in segment],
            "geodesic_distance_m": geodesic,
            "start_error_m": start_error,
            "end_error_m": end_error,
        })
    return {"points": combined, "segments": segments,
            "geodesic_distance_m": total_geodesic}


def validate_navmesh_polyline(pathfinder: Any, points: Iterable[Sequence[float]], *,
                              max_step_m: float, footprint_radius_m: float,
                              clearance_margin_m: float = 0.0,
                              endpoint_tolerance_m: float = 0.025,
                              navmesh_agent_radius_m: Optional[float] = None,
                              navmesh_agent_height_m: Optional[float] = None,
                              required_agent_height_m: Optional[float] = None) -> dict[str, Any]:
    """Check sampled path points and no-slide segment steps without snapping.

    The supplied points must already come from Habitat's ShortestPath result.
    Any clipped/blocked `try_step_no_sliding` result fails instead of being
    accepted as a silent correction.
    """
    raw = [_vec3(point, "path point") for point in points]
    samples = sample_polyline(raw, max_step_m=max_step_m)
    radius = float(footprint_radius_m)
    margin = float(clearance_margin_m)
    tolerance = float(endpoint_tolerance_m)
    if not all(math.isfinite(v) and v >= 0.0 for v in (radius, margin, tolerance)):
        raise ValueError("footprint and tolerance settings must be finite and non-negative")
    checked: list[dict[str, Any]] = []
    clearance_fn = getattr(pathfinder, "distance_to_closest_obstacle", None)
    query_radius = radius + margin + 0.05 if clearance_fn is not None else None
    for index, point in enumerate(samples):
        if not bool(pathfinder.is_navigable(point)):
            raise RouteExecutionError(f"navmesh sample {index} is not navigable: {point}")
        clearance = None if clearance_fn is None else float(
            clearance_fn(point, max_search_radius=query_radius))
        if clearance is not None and math.isfinite(clearance) and clearance >= 0.0:
            if clearance < radius + margin:
                raise RouteExecutionError(
                    f"footprint clearance failed at sample {index}: {clearance} m; "
                    f"required {radius + margin} m"
                )
            clearance_method = "PathFinder.distance_to_closest_obstacle"
        else:
            # A finite zero is a real geometric clearance and is handled above.
            # Only a missing/non-finite API result may use the serialized
            # navmesh footprint fallback.
            values = (navmesh_agent_radius_m, navmesh_agent_height_m, required_agent_height_m)
            if any(value is None or not math.isfinite(float(value)) for value in values):
                raise RouteExecutionError(
                    f"obstacle clearance query unavailable at sample {index} and serialized navmesh footprint is missing"
                )
            if float(navmesh_agent_radius_m) + 1e-6 < radius + margin:
                raise RouteExecutionError(
                    f"serialized navmesh radius {navmesh_agent_radius_m} m is smaller than required {radius + margin} m"
                )
            if float(navmesh_agent_height_m) + 1e-6 < float(required_agent_height_m):
                raise RouteExecutionError(
                    f"serialized navmesh height {navmesh_agent_height_m} m is smaller than agent {required_agent_height_m} m"
                )
            clearance_method = "serialized_navmesh_agent_footprint"
        if clearance_method == "PathFinder.distance_to_closest_obstacle":
            clearance_value_status = _clearance_value_status(clearance, float(query_radius))
            reported_clearance = clearance
        else:
            clearance_value_status = "api_unavailable_footprint_fallback"
            reported_clearance = None
        checked.append({"index": index, "position_habitat": list(point),
                        "navigable": True, "obstacle_clearance_m": reported_clearance,
                        "clearance_method": clearance_method,
                        "clearance_query_max_search_radius_m": query_radius,
                        "clearance_value_status": clearance_value_status,
                        "sample_validation_status": "navigable_and_clearance_requirement_passed"})
    step_fn = getattr(pathfinder, "try_step_no_sliding", None)
    if step_fn is None:
        raise RouteExecutionError("PathFinder has no try_step_no_sliding; collision-safe route cannot be verified")
    checked_steps = 0
    for index, (start, end) in enumerate(zip(samples, samples[1:])):
        actual = _vec3(step_fn(start, end), "try_step_no_sliding result")
        error = distance3(actual, end)
        if error > tolerance:
            raise RouteExecutionError(
                f"navmesh segment {index} is blocked/clipped by {error:.6f} m; "
                "route was not snapped"
            )
        checked_steps += 1
    return {"status": "pass", "report_schema": "habitat-navmesh-clearance-legacy/v1",
            "source": "Habitat ShortestPath polyline",
            "sample_step_m": float(max_step_m), "sample_count": len(checked),
            "checked_no_sliding_segments": checked_steps,
            "footprint_radius_m": radius, "clearance_margin_m": margin,
            "clearance_api": ("PathFinder.distance_to_closest_obstacle" if clearance_fn is not None else None),
            "clearance_query_max_search_radius_m": query_radius,
            "clearance_value_semantics": "distance to the radius-eroded navmesh boundary, not physical scene-object clearance",
            "samples": checked}


def validate_navmesh_profile_compatibility(navmesh_settings: Any, *,
                                           geometry_contract: dict[str, Any]) -> dict[str, Any]:
    """Validate the runtime capsule against the footprint baked into a navmesh.

    The baked radius is a compatibility check only. It is never added to the
    extra clearance margin: Habitat's PathFinder clearance query measures to
    the edge of the already-eroded navmesh.
    """
    if not isinstance(geometry_contract, dict) or geometry_contract.get("schema") != "habitat-navmesh-clearance-contract/v1":
        raise RouteExecutionError("unsupported or missing versioned navmesh geometry contract")
    if navmesh_settings is None:
        raise RouteExecutionError("loaded navmesh has no serialized settings; runtime profile compatibility is unavailable")
    tolerance = float(geometry_contract.get("profile_compatibility_tolerance_m", float("nan")))
    expected_radius = float(geometry_contract.get("navmesh_baked_agent_radius_m", float("nan")))
    expected_height = float(geometry_contract.get("navmesh_baked_agent_height_m", float("nan")))
    runtime_profile = geometry_contract.get("runtime_agent_profile")
    if not isinstance(runtime_profile, dict):
        raise RouteExecutionError("geometry contract is missing runtime_agent_profile")
    runtime_radius = float(runtime_profile.get("radius_m", float("nan")))
    runtime_height = float(runtime_profile.get("height_m", float("nan")))
    values = (tolerance, expected_radius, expected_height, runtime_radius, runtime_height)
    if not all(math.isfinite(value) and value >= 0.0 for value in values) or tolerance <= 0.0:
        raise RouteExecutionError("geometry contract profile values must be finite and tolerance positive")
    try:
        actual_radius = float(navmesh_settings.agent_radius)
        actual_height = float(navmesh_settings.agent_height)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RouteExecutionError("loaded navmesh settings do not expose agent radius/height") from exc
    if not all(math.isfinite(value) and value >= 0.0 for value in (actual_radius, actual_height)):
        raise RouteExecutionError("loaded navmesh radius/height is invalid")
    checks = {
        "binary_matches_contract_radius": abs(actual_radius - expected_radius) <= tolerance,
        "binary_matches_contract_height": abs(actual_height - expected_height) <= tolerance,
        "runtime_matches_baked_radius": abs(runtime_radius - actual_radius) <= tolerance,
        "runtime_matches_baked_height": abs(runtime_height - actual_height) <= tolerance,
    }
    if not all(checks.values()):
        raise RouteExecutionError(
            "runtime agent profile is incompatible with navmesh baked footprint: "
            f"actual=({actual_radius:.9g} m, {actual_height:.9g} m), "
            f"contract=({expected_radius:.9g} m, {expected_height:.9g} m), "
            f"runtime=({runtime_radius:.9g} m, {runtime_height:.9g} m)"
        )
    return {
        "status": "compatible",
        "navmesh_baked_agent_radius_m": actual_radius,
        "navmesh_baked_agent_height_m": actual_height,
        "runtime_agent_profile": {"radius_m": runtime_radius, "height_m": runtime_height},
        "profile_compatibility_tolerance_m": tolerance,
        "checks": checks,
    }


def resolve_vertical_navmesh_tolerance(policy: dict[str, Any]) -> float:
    """Read one authoritative vertical tolerance for both preflight and motion."""
    contract = policy.get("geometry_contract")
    if isinstance(contract, dict):
        if contract.get("schema") != "habitat-navmesh-clearance-contract/v1":
            raise RouteExecutionError("unsupported versioned geometry contract")
        raw = contract.get("vertical_navmesh_tolerance_m")
    else:
        raw = (policy.get("route_follow") or {}).get("vertical_navmesh_tolerance_m")
    try:
        tolerance = float(raw)
    except (TypeError, ValueError) as exc:
        raise RouteExecutionError("vertical navmesh tolerance is missing or invalid") from exc
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise RouteExecutionError("vertical navmesh tolerance must be finite and non-negative")
    return tolerance


def validate_navmesh_polyline_v2(pathfinder: Any, points: Iterable[Sequence[float]], *,
                                 max_step_m: float,
                                 geometry_contract: dict[str, Any],
                                 navmesh_settings: Any,
                                 endpoint_tolerance_m: float,
                                 vertical_navmesh_tolerance_m: float) -> dict[str, Any]:
    """Validate against the radius-eroded navmesh plus explicit extra margin.

    Numeric boundary tolerance is fail-closed and separate from the physical
    clearance margin. Missing/non-finite query results are unavailable; they
    never fall back to the baked radius. A measured zero is a real boundary
    result and fails the numerical boundary band even when physical margin is
    zero.
    """
    profile = validate_navmesh_profile_compatibility(
        navmesh_settings, geometry_contract=geometry_contract)
    margin = float(geometry_contract.get("extra_navmesh_clearance_margin_m", float("nan")))
    epsilon = float(geometry_contract.get("numeric_boundary_tolerance_m", float("nan")))
    if not all(math.isfinite(value) and value >= 0.0 for value in (margin, epsilon)) or epsilon <= 0.0:
        raise RouteExecutionError("extra clearance margin must be non-negative and numeric tolerance positive")
    vertical_tolerance = float(vertical_navmesh_tolerance_m)
    endpoint_tolerance = float(endpoint_tolerance_m)
    if not math.isfinite(vertical_tolerance) or vertical_tolerance < 0.0:
        raise RouteExecutionError("vertical navmesh tolerance must be finite and non-negative")
    if not math.isfinite(endpoint_tolerance) or endpoint_tolerance < 0.0:
        raise RouteExecutionError("endpoint tolerance must be finite and non-negative")
    raw = [_vec3(point, "path point") for point in points]
    samples = sample_polyline(raw, max_step_m=max_step_m)
    clearance_fn = getattr(pathfinder, "distance_to_closest_obstacle", None)
    if clearance_fn is None:
        raise RouteExecutionError("navmesh clearance API is unavailable; route fails closed")
    step_fn = getattr(pathfinder, "try_step_no_sliding", None)
    if step_fn is None:
        raise RouteExecutionError("PathFinder has no try_step_no_sliding; route fails closed")
    # Search far enough to distinguish passing the margin plus its numerical
    # uncertainty band from the API's capped 'no obstacle in range' result.
    max_search_radius = margin + (2.0 * epsilon)
    checked: list[dict[str, Any]] = []
    for index, point in enumerate(samples):
        if not bool(pathfinder.is_navigable(point, max_y_delta=vertical_tolerance)):
            raise RouteExecutionError(
                f"navmesh sample {index} is not navigable within vertical tolerance {vertical_tolerance} m: {point}")
        clearance = float(clearance_fn(point, max_search_radius=max_search_radius))
        if not math.isfinite(clearance) or clearance < 0.0:
            raise RouteExecutionError(f"navmesh clearance unavailable/invalid at sample {index}: {clearance}")
        if clearance <= margin + epsilon:
            if clearance < margin - epsilon:
                raise RouteExecutionError(
                    f"extra navmesh clearance failed at sample {index}: {clearance} m; "
                    f"required margin {margin} m")
            raise RouteExecutionError(
                f"clearance is on/within numeric boundary tolerance at sample {index}: "
                f"{clearance} m around margin {margin} m (epsilon {epsilon} m)")
        value_status = _clearance_value_status(clearance, max_search_radius)
        checked.append({"index": index, "position_habitat": list(point),
                        "navigable": True, "vertical_navmesh_tolerance_m": vertical_tolerance,
                        "extra_navmesh_clearance_m": clearance,
                        "clearance_query_max_search_radius_m": max_search_radius,
                        "clearance_value_status": value_status,
                        "clearance_status": "above_extra_margin_and_boundary_tolerance"})
    checked_steps = 0
    max_step_error = 0.0
    for index, (start, end) in enumerate(zip(samples, samples[1:])):
        actual = _vec3(step_fn(start, end), "try_step_no_sliding result")
        error = distance3(actual, end)
        max_step_error = max(max_step_error, error)
        if error > endpoint_tolerance:
            raise RouteExecutionError(
                f"navmesh segment {index} was clipped by {error:.6f} m; route was not snapped")
        checked_steps += 1
    clearances = [row["extra_navmesh_clearance_m"] for row in checked]
    return {
        "status": "pass", "contract_schema": geometry_contract["schema"],
        "profile_compatibility": profile,
        "extra_navmesh_clearance_margin_m": margin,
        "numeric_boundary_tolerance_m": epsilon,
        "boundary_policy": "fail_closed_within_margin_plus_numeric_tolerance",
        "clearance_api": "PathFinder.distance_to_closest_obstacle",
        "clearance_query_max_search_radius_m": max_search_radius,
        "clearance_value_semantics": "distance to the radius-eroded navmesh boundary; not physical scene-object clearance",
        "vertical_navmesh_tolerance_m": vertical_tolerance,
        "sample_step_m": float(max_step_m), "sample_count": len(checked),
        "checked_no_sliding_segments": checked_steps,
        "max_try_step_no_sliding_error_m": max_step_error,
        "min_extra_navmesh_clearance_m": min(clearances),
        "min_extra_navmesh_clearance_value_status": (
            "query_capped_lower_bound"
            if any(row["clearance_value_status"] == "query_capped_lower_bound"
                   and math.isclose(row["extra_navmesh_clearance_m"], min(clearances), rel_tol=1e-9, abs_tol=1e-12)
                   for row in checked)
            else "measured"
        ),
        "min_uncapped_extra_navmesh_clearance_m": min(
            (row["extra_navmesh_clearance_m"] for row in checked
             if row["clearance_value_status"] == "measured"), default=None),
        "samples": checked,
    }


def build_clearance_preflight_artifacts(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate and build the exact manifest/journal payloads used by the route runner.

    V2 keeps API/method, per-sample validation status, and query-value status
    separate. Capped values are lower bounds, not measured minima. The
    legacy report remains supported through its explicit per-sample method.
    """
    def require(mapping: dict[str, Any], key: str, where: str) -> Any:
        if key not in mapping:
            raise RouteExecutionError(f"clearance report {where} is missing required field {key}")
        return mapping[key]

    def finite_nonnegative(value: Any, where: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise RouteExecutionError(f"clearance report {where} must be a finite non-negative number") from exc
        if not math.isfinite(result) or result < 0.0:
            raise RouteExecutionError(f"clearance report {where} must be a finite non-negative number")
        return result

    if not isinstance(report, dict):
        raise RouteExecutionError("clearance report must be an object")
    report_status = require(report, "status", "root")
    if report_status != "pass":
        raise RouteExecutionError(f"clearance report status must be pass, got {report_status!r}")
    samples = require(report, "samples", "root")
    if not isinstance(samples, list) or not samples:
        raise RouteExecutionError("clearance report samples must be a non-empty list")
    sample_count = require(report, "sample_count", "root")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count != len(samples):
        raise RouteExecutionError("clearance report sample_count does not match samples")

    api_counts: dict[str, int] = {}
    method_counts: dict[str, int] = {}
    validation_status_counts: dict[str, int] = {}
    value_status_counts: dict[str, int] = {}
    api_values: list[tuple[float, str, float]] = []
    report_schema: str

    if report.get("contract_schema") == "habitat-navmesh-clearance-contract/v1":
        report_schema = "habitat-navmesh-clearance-contract/v1"
        api = require(report, "clearance_api", "root")
        if not isinstance(api, str) or not api:
            raise RouteExecutionError("clearance report clearance_api must be a non-empty string")
        if api != "PathFinder.distance_to_closest_obstacle":
            raise RouteExecutionError(f"unsupported clearance API in v2 report: {api!r}")
        query_radius = finite_nonnegative(require(report, "clearance_query_max_search_radius_m", "root"),
                                          "clearance_query_max_search_radius_m")
        if query_radius <= 0.0:
            raise RouteExecutionError("clearance report query radius must be positive")
        margin = finite_nonnegative(require(report, "extra_navmesh_clearance_margin_m", "root"),
                                    "extra_navmesh_clearance_margin_m")
        epsilon = finite_nonnegative(require(report, "numeric_boundary_tolerance_m", "root"),
                                     "numeric_boundary_tolerance_m")
        boundary_policy = require(report, "boundary_policy", "root")
        if boundary_policy != "fail_closed_within_margin_plus_numeric_tolerance":
            raise RouteExecutionError(f"unsupported v2 boundary policy: {boundary_policy!r}")
        require(report, "profile_compatibility", "root")
        finite_nonnegative(require(report, "vertical_navmesh_tolerance_m", "root"),
                            "vertical_navmesh_tolerance_m")
        finite_nonnegative(require(report, "sample_step_m", "root"), "sample_step_m")
        checked_segments = require(report, "checked_no_sliding_segments", "root")
        if isinstance(checked_segments, bool) or not isinstance(checked_segments, int) or checked_segments < 0:
            raise RouteExecutionError("clearance report checked_no_sliding_segments must be a non-negative integer")
        finite_nonnegative(require(report, "max_try_step_no_sliding_error_m", "root"),
                            "max_try_step_no_sliding_error_m")
        semantics = require(report, "clearance_value_semantics", "root")
        if semantics != "distance to the radius-eroded navmesh boundary; not physical scene-object clearance":
            raise RouteExecutionError("clearance report has unsupported clearance_value_semantics")
        if query_radius <= margin + epsilon:
            raise RouteExecutionError("clearance report query radius cannot distinguish the locked margin boundary")
        reported_min = finite_nonnegative(require(report, "min_extra_navmesh_clearance_m", "root"),
                                          "min_extra_navmesh_clearance_m")
        reported_min_status = require(report, "min_extra_navmesh_clearance_value_status", "root")
        api_counts[api] = len(samples)
        reported_values: list[float] = []
        sample_indices: set[int] = set()
        vertical_tolerance = float(report["vertical_navmesh_tolerance_m"])
        for index, row in enumerate(samples):
            where = f"sample[{index}]"
            if not isinstance(row, dict):
                raise RouteExecutionError(f"clearance report {where} must be an object")
            sample_index = require(row, "index", where)
            if isinstance(sample_index, bool) or not isinstance(sample_index, int) or sample_index in sample_indices:
                raise RouteExecutionError(f"clearance report {where}.index must be a unique integer")
            sample_indices.add(sample_index)
            position = require(row, "position_habitat", where)
            if not isinstance(position, (list, tuple)) or len(position) != 3:
                raise RouteExecutionError(f"clearance report {where}.position_habitat must contain three coordinates")
            for coordinate in position:
                try:
                    finite_coordinate = float(coordinate)
                except (TypeError, ValueError) as exc:
                    raise RouteExecutionError(f"clearance report {where}.position_habitat must be finite") from exc
                if not math.isfinite(finite_coordinate):
                    raise RouteExecutionError(f"clearance report {where}.position_habitat must be finite")
            if require(row, "navigable", where) is not True:
                raise RouteExecutionError(f"clearance report {where}.navigable must be true after validation")
            row_vertical = finite_nonnegative(require(row, "vertical_navmesh_tolerance_m", where),
                                              f"{where}.vertical_navmesh_tolerance_m")
            if not math.isclose(row_vertical, vertical_tolerance, rel_tol=1e-12, abs_tol=1e-12):
                raise RouteExecutionError(f"clearance report {where} vertical tolerance differs from report-level policy")
            status = require(row, "clearance_status", where)
            if not isinstance(status, str) or not status:
                raise RouteExecutionError(f"clearance report {where}.clearance_status must be a non-empty validation status")
            if status != "above_extra_margin_and_boundary_tolerance":
                raise RouteExecutionError(f"clearance report {where} has unexpected validation status {status!r}")
            validation_status_counts[status] = validation_status_counts.get(status, 0) + 1
            row_radius = finite_nonnegative(require(row, "clearance_query_max_search_radius_m", where),
                                            f"{where}.clearance_query_max_search_radius_m")
            if not math.isclose(row_radius, query_radius, rel_tol=1e-12, abs_tol=1e-12):
                raise RouteExecutionError(f"clearance report {where} query radius differs from report-level API radius")
            value = finite_nonnegative(require(row, "extra_navmesh_clearance_m", where),
                                       f"{where}.extra_navmesh_clearance_m")
            value_status = _clearance_value_status(value, row_radius)
            declared_value_status = require(row, "clearance_value_status", where)
            if declared_value_status != value_status:
                raise RouteExecutionError(f"clearance report {where} clearance_value_status disagrees with the query cap")
            value_status_counts[value_status] = value_status_counts.get(value_status, 0) + 1
            api_values.append((value, value_status, row_radius))
            reported_values.append(value)
        if not math.isclose(reported_min, min(reported_values), rel_tol=1e-12, abs_tol=1e-12):
            raise RouteExecutionError("clearance report min_extra_navmesh_clearance_m disagrees with its samples")
        expected_min_status = (
            "query_capped_lower_bound"
            if any(status == "query_capped_lower_bound"
                   and math.isclose(value, reported_min, rel_tol=1e-9, abs_tol=1e-12)
                   for value, status, _radius in api_values)
            else "measured"
        )
        if reported_min_status != expected_min_status:
            raise RouteExecutionError("clearance report minimum value status disagrees with its samples")
        reported_uncapped = require(report, "min_uncapped_extra_navmesh_clearance_m", "root")
        measured_values = [value for value, status, _radius in api_values if status == "measured"]
        expected_uncapped = min(measured_values, default=None)
        if expected_uncapped is None:
            if reported_uncapped is not None:
                raise RouteExecutionError("clearance report declares an uncapped minimum but every sample is query-capped")
        else:
            declared_uncapped = finite_nonnegative(reported_uncapped, "min_uncapped_extra_navmesh_clearance_m")
            if not math.isclose(declared_uncapped, expected_uncapped, rel_tol=1e-12, abs_tol=1e-12):
                raise RouteExecutionError("clearance report uncapped minimum disagrees with its samples")
    elif report.get("report_schema") == "habitat-navmesh-clearance-legacy/v1":
        report_schema = "habitat-navmesh-clearance-legacy/v1"
        methods = {"PathFinder.distance_to_closest_obstacle", "serialized_navmesh_agent_footprint"}
        legacy_api = require(report, "clearance_api", "root")
        if legacy_api not in (None, "PathFinder.distance_to_closest_obstacle"):
            raise RouteExecutionError(f"unsupported legacy clearance API: {legacy_api!r}")
        semantics = require(report, "clearance_value_semantics", "root")
        if semantics != "distance to the radius-eroded navmesh boundary, not physical scene-object clearance":
            raise RouteExecutionError("legacy clearance report has unsupported clearance_value_semantics")
        legacy_query_radius = require(report, "clearance_query_max_search_radius_m", "root")
        if legacy_api is None:
            if legacy_query_radius is not None:
                raise RouteExecutionError("legacy report without a clearance API cannot declare a query radius")
        else:
            legacy_query_radius = finite_nonnegative(legacy_query_radius, "clearance_query_max_search_radius_m")
            if legacy_query_radius <= 0.0:
                raise RouteExecutionError("legacy report query radius must be positive")
        for index, row in enumerate(samples):
            where = f"sample[{index}]"
            if not isinstance(row, dict):
                raise RouteExecutionError(f"clearance report {where} must be an object")
            method = require(row, "clearance_method", where)
            if method not in methods:
                raise RouteExecutionError(f"clearance report {where} has unsupported legacy method {method!r}")
            method_counts[method] = method_counts.get(method, 0) + 1
            sample_validation_status = require(row, "sample_validation_status", where)
            if sample_validation_status != "navigable_and_clearance_requirement_passed":
                raise RouteExecutionError(f"clearance report {where} has unsupported validation status {sample_validation_status!r}")
            validation_status_counts[sample_validation_status] = validation_status_counts.get(sample_validation_status, 0) + 1
            value_status = require(row, "clearance_value_status", where)
            value = require(row, "obstacle_clearance_m", where)
            row_query_radius = row.get("clearance_query_max_search_radius_m")
            if legacy_query_radius is None:
                if row_query_radius is not None:
                    raise RouteExecutionError(f"clearance report {where} cannot declare a query radius without an API")
            else:
                row_query_radius = finite_nonnegative(row_query_radius,
                                                      f"{where}.clearance_query_max_search_radius_m")
                if not math.isclose(row_query_radius, legacy_query_radius, rel_tol=1e-12, abs_tol=1e-12):
                    raise RouteExecutionError(f"clearance report {where} query radius differs from legacy report radius")
            if method == "PathFinder.distance_to_closest_obstacle":
                if legacy_api != method or row_query_radius is None:
                    raise RouteExecutionError(f"clearance report {where} API method does not match report provenance")
                numeric = finite_nonnegative(value, f"{where}.obstacle_clearance_m")
                expected_status = _clearance_value_status(numeric, row_query_radius)
                if value_status != expected_status:
                    raise RouteExecutionError(f"clearance report {where} clearance_value_status disagrees with the query cap")
                api_counts[method] = api_counts.get(method, 0) + 1
                api_values.append((numeric, expected_status, row_query_radius))
            else:
                if value is not None or value_status != "api_unavailable_footprint_fallback":
                    raise RouteExecutionError(f"clearance report {where} legacy footprint fallback must mark query value unavailable")
                value_status_counts[value_status] = value_status_counts.get(value_status, 0) + 1
                continue
            value_status_counts[expected_status] = value_status_counts.get(expected_status, 0) + 1
    else:
        raise RouteExecutionError("clearance report schema is missing or unsupported; expected v2 contract or explicit legacy samples")

    reported_min = min((value for value, _status, _radius in api_values), default=None)
    min_status = None
    if reported_min is not None:
        min_status = ("query_capped_lower_bound"
                      if any(status == "query_capped_lower_bound"
                             and math.isclose(value, reported_min, rel_tol=1e-9, abs_tol=1e-12)
                             for value, status, _radius in api_values)
                      else "measured")
    measured_values = [value for value, status, _radius in api_values if status == "measured"]
    capped_values = [radius for _value, status, radius in api_values if status == "query_capped_lower_bound"]
    all_sample_lower_bounds = [*measured_values, *capped_values]
    minimum_all_status = None
    if all_sample_lower_bounds:
        min_lower_bound = min(all_sample_lower_bounds)
        minimum_all_status = (
            "query_capped_lower_bound"
            if any(status == "query_capped_lower_bound" and math.isclose(radius, min_lower_bound, rel_tol=1e-9, abs_tol=1e-12)
                   for _value, status, radius in api_values)
            else "measured"
        )
    else:
        min_lower_bound = None
    summary = {
        "schema": "route-clearance-summary/v1",
        "input_report_schema": report_schema,
        "report_validation_status": report_status,
        "sample_count": len(samples),
        "clearance_api_counts": api_counts,
        "legacy_clearance_method_counts": method_counts,
        "validation_status_counts": validation_status_counts,
        "clearance_value_status_counts": value_status_counts,
        "query_capped_sample_count": len(capped_values),
        "uncapped_api_sample_count": len(measured_values),
        "min_reported_navmesh_boundary_query_value_m": reported_min,
        "min_reported_navmesh_boundary_query_value_status": min_status,
        "min_uncapped_navmesh_boundary_distance_m": min(measured_values, default=None),
        "min_capped_navmesh_boundary_lower_bound_m": min(capped_values, default=None),
        "min_all_samples_navmesh_boundary_lower_bound_m": min_lower_bound,
        "min_all_samples_navmesh_boundary_value_status": minimum_all_status,
        "physical_obstacle_clearance_min_m": None,
        "physical_obstacle_clearance_status": "not_measured_by_navmesh_boundary_query",
        "measurement_semantics": "PathFinder float32 distance is to the radius-eroded navmesh boundary; values within one float32 ULP of the representable query cap are reported as lower bounds, not measured true minima or distance to scene objects",
    }
    journal = {
        "clearance_summary": summary,
        "clearance_sample_count": len(samples),
        "clearance_api_counts": api_counts,
        "clearance_query_capped_sample_count": len(capped_values),
        "min_uncapped_navmesh_boundary_distance_m": min(measured_values, default=None),
        "physical_obstacle_clearance_min_m": None,
    }
    return {
        "manifest": {"clearance_validation": report, "clearance_summary": summary},
        "journal": journal,
    }


@dataclass(frozen=True)
class KeyframeDecision:
    candidate: bool
    reason: str
    translation_from_reference_m: Optional[float]
    yaw_from_reference_deg: Optional[float]
    elapsed_from_reference_s: Optional[float]
    reference_frame_id: Optional[str]
    reference_kind: str


class KeyframePolicyV1:
    """Causal pose-delta policy. One camera frame is never itself evidence."""

    def __init__(self, *, min_translation_m: float, min_yaw_deg: float,
                 min_interval_s: float, first_frame: bool = True):
        values = (float(min_translation_m), float(min_yaw_deg), float(min_interval_s))
        if not all(math.isfinite(x) and x > 0.0 for x in values):
            raise ValueError("keyframe thresholds must be finite and positive")
        self.min_translation_m, self.min_yaw_rad, self.min_interval_s = (
            values[0], math.radians(values[1]), values[2]
        )
        self.first_frame = bool(first_frame)
        self.reference: Optional[tuple[str, tuple[float, float, float], float, float]] = None
        self._last_decision: Optional[KeyframeDecision] = None

    def evaluate(self, frame_id: str, position: Sequence[float], yaw_rad: float,
                 sim_time_s: float) -> KeyframeDecision:
        pos = _vec3(position, "keyframe position")
        yaw, stamp = float(yaw_rad), float(sim_time_s)
        if not frame_id or not math.isfinite(yaw) or not math.isfinite(stamp) or stamp < 0.0:
            raise ValueError("keyframe input identity/pose/time is invalid")
        if self.reference is None:
            decision = KeyframeDecision(self.first_frame, "first_valid_frame" if self.first_frame else "no_reference",
                                        None, None, None, None, "none")
        else:
            ref_id, ref_pos, ref_yaw, ref_time = self.reference
            translation = distance3(pos, ref_pos)
            angle = abs(yaw_delta_rad(ref_yaw, yaw))
            elapsed = stamp - ref_time
            if elapsed < -1e-9:
                raise ValueError("camera simulation timestamps must be monotonic")
            if elapsed + 1e-9 < self.min_interval_s:
                candidate, reason = False, "below_min_interval"
            elif translation >= self.min_translation_m or angle >= self.min_yaw_rad:
                candidate = True
                reason = "translation_threshold" if translation >= self.min_translation_m else "yaw_threshold"
            else:
                candidate, reason = False, "pose_delta_below_thresholds"
            decision = KeyframeDecision(candidate, reason, translation, math.degrees(angle), elapsed,
                                        ref_id, "last_queue_accepted_candidate")
        self._last_decision = decision
        return decision

    def mark_queue_accepted(self, frame_id: str, position: Sequence[float], yaw_rad: float,
                            sim_time_s: float) -> None:
        if self._last_decision is None or not self._last_decision.candidate:
            raise RuntimeError("cannot advance keyframe reference without a candidate decision")
        self.reference = (str(frame_id), _vec3(position, "keyframe position"),
                          float(yaw_rad), float(sim_time_s))


class BoundedKeyframeWriter:
    """One in-flight writer plus one replaceable pending packet.

    `write_fn` must persist an immutable copy of a captured packet. This is a
    raw keyframe file sink for T1, not a mapping worker and not graph evidence.
    """

    def __init__(self, write_fn: Callable[[dict[str, Any]], Any],
                 emit: Callable[[dict[str, Any]], None], *, name: str = "t1-keyframe-writer"):
        self._write_fn, self._emit = write_fn, emit
        self._condition = threading.Condition()
        self._pending: Optional[dict[str, Any]] = None
        self._in_flight: Optional[dict[str, Any]] = None
        self._closed = False
        self._failure: Optional[BaseException] = None
        self._submitted_ids: set[str] = set()
        self._worker = threading.Thread(target=self._run, name=name, daemon=True)
        self._worker.start()

    @property
    def failure(self) -> Optional[BaseException]:
        with self._condition:
            return self._failure

    def submit(self, packet: dict[str, Any]) -> str:
        frame_id = str(packet.get("frame_id", ""))
        if not frame_id:
            raise ValueError("keyframe packet requires frame_id")
        with self._condition:
            if self._closed:
                raise RuntimeError("keyframe writer is closed")
            if self._failure is not None:
                raise RuntimeError("keyframe writer failed") from self._failure
            if frame_id in self._submitted_ids:
                raise ValueError(f"duplicate keyframe submission: {frame_id}")
            self._submitted_ids.add(frame_id)
            replaced = self._pending
            self._pending = packet
            # Publish queue decisions while holding the same lock used by the
            # worker. This keeps the journal order causal even if the worker
            # was about to wake between two submissions.
            try:
                if replaced is not None:
                    self._emit({"event": "keyframe_superseded", "frame_id": replaced["frame_id"],
                                "replacement_frame_id": frame_id,
                                "reason": "superseded_before_processing"})
                self._emit({"event": "keyframe_queued", "frame_id": frame_id,
                            "queue_policy": "one_in_flight_plus_one_latest_pending"})
            except BaseException as exc:
                self._failure = exc
                self._pending = None
                self._condition.notify_all()
                raise
            self._condition.notify_all()
        return "queued"

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._pending is None and self._closed:
                    return
                packet, self._pending = self._pending, None
                self._in_flight = packet
            frame_id = str(packet["frame_id"])
            try:
                self._emit({"event": "keyframe_write_started", "frame_id": frame_id})
                result = self._write_fn(packet)
                self._emit({"event": "keyframe_admitted", "frame_id": frame_id,
                            "persisted": True, "mapper_processed": False, "write_result": result})
            except BaseException as exc:
                with self._condition:
                    self._failure = exc
                    self._pending = None
                    self._in_flight = None
                    self._condition.notify_all()
                try:
                    self._emit({"event": "keyframe_write_failed", "frame_id": frame_id,
                                "error_type": type(exc).__name__, "error": str(exc)})
                except BaseException:
                    pass
                return
            else:
                with self._condition:
                    self._in_flight = None
                    self._condition.notify_all()

    def close(self, *, timeout_s: float = 30.0) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._worker.join(timeout=max(0.0, float(timeout_s)))
        if self._worker.is_alive():
            raise TimeoutError("bounded keyframe writer did not drain before timeout")
        if self.failure is not None:
            raise RuntimeError("bounded keyframe writer failed") from self.failure
