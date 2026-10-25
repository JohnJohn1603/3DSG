#!/usr/bin/env python3
"""Geometry-only, action-increment validation for one locked T4 route.

Loads the locked Habitat navmesh into PathFinder only. It does not create a
Simulator, render sensors, write RGB-D, or load perception/mapping workers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration_v2.camera_pacing import motion_increment_for_step
from integration_v2.smooth_route import (
    distance3,
    find_waypoint_polyline,
    resolve_vertical_navmesh_tolerance,
    sample_polyline,
    validate_navmesh_polyline_v2,
    validate_navmesh_profile_compatibility,
    yaw_delta_rad,
    yaw_toward_habitat_xz,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def xz_distance(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def point_to_polyline_xz(point, polyline) -> float:
    best = float("inf")
    for start, end in zip(polyline, polyline[1:]):
        dx, dz = end[0] - start[0], end[2] - start[2]
        den = dx * dx + dz * dz
        t = 0.0 if den <= 1e-18 else max(0.0, min(1.0,
            ((point[0] - start[0]) * dx + (point[2] - start[2]) * dz) / den))
        projected = (start[0] + t * dx, point[1], start[2] + t * dz)
        best = min(best, xz_distance(point, projected))
    return best


def run(config_path: Path) -> dict:
    import habitat_sim
    import magnum as mn

    config_path = config_path.resolve()
    config = json.loads(config_path.read_text())
    policy_path = Path(config["policy_path"]).resolve()
    route_path = Path(config["source_route_path"]).resolve()
    policy = json.loads(policy_path.read_text())
    route_doc = json.loads(route_path.read_text())
    contract = policy["geometry_contract"]
    navmesh = Path(config["navmesh_file"]).resolve()
    scene = Path(config["scene_asset"]).resolve()
    dataset_config = Path(config["scene_dataset_config"]).resolve()
    for label, path, expected in (
        ("policy", policy_path, config["policy_sha256"]),
        ("route", route_path, config["source_route_sha256"]),
        ("navmesh", navmesh, config["navmesh_sha256"]),
        ("scene_asset", scene, config["scene_asset_sha256"]),
        ("scene_dataset_config", dataset_config, config["scene_dataset_config_sha256"]),
    ):
        if not path.is_file() or sha256(path) != expected:
            raise RuntimeError(f"locked {label} missing/hash mismatch: {path}")
    if route_doc["sequence_id"] != config["sequence_id"] or route_doc["route_id"] != config["route_id"]:
        raise RuntimeError("effective config route identity differs from source route")
    if policy["schema"] != "smooth-route-t1-policy/v6":
        raise RuntimeError("this diagnostic requires the locked geometry policy v6")

    pathfinder = habitat_sim.PathFinder()
    if not pathfinder.load_nav_mesh(str(navmesh)):
        raise RuntimeError("PathFinder could not load the locked navmesh")
    settings = pathfinder.nav_mesh_settings
    profile = validate_navmesh_profile_compatibility(settings, geometry_contract=contract)
    vertical_tolerance = resolve_vertical_navmesh_tolerance(policy)
    points = [tuple(float(v) for v in row["position_habitat"])
              for row in config["route_points"]]
    resolved = find_waypoint_polyline(
        pathfinder, points, shortest_path_factory=habitat_sim.ShortestPath,
        vector_factory=lambda *values: mn.Vector3(*values),
        endpoint_tolerance_m=float(policy["route_follow"]["endpoint_tolerance_m"]))
    nav_path = resolved["points"]
    sample_spacing = float(policy["route_follow"]["path_sample_step_m"])
    samples = sample_polyline(nav_path, sample_spacing)
    gate_report = validate_navmesh_polyline_v2(
        pathfinder, nav_path, max_step_m=sample_spacing,
        geometry_contract=contract, navmesh_settings=settings,
        endpoint_tolerance_m=float(policy["route_follow"]["endpoint_tolerance_m"]),
        vertical_navmesh_tolerance_m=vertical_tolerance)

    diagnostic_radius = 0.5
    clearance_rows = []
    for index, point in enumerate(samples):
        value = float(pathfinder.distance_to_closest_obstacle(
            mn.Vector3(*point), max_search_radius=diagnostic_radius))
        finite = math.isfinite(value) and value >= 0.0
        capped = finite and math.isclose(value, diagnostic_radius, rel_tol=0.0, abs_tol=1e-6)
        clearance_rows.append({"sample_index": index, "position_habitat_m": list(point),
            "value_m": value if finite else None,
            "status": "search_radius_cap_no_wall_reported_within_0.5m" if capped else
                      "measured_navmesh_boundary_distance" if finite else "unavailable_nonfinite",
            "query_radius_m": diagnostic_radius})

    # Mirror the production route-follow decision loop over the exact same
    # sampled PathFinder target sequence, using PathFinder's no-sliding step.
    dt = float(policy["simulator_step_s"])
    move_step, turn_step = motion_increment_for_step(
        float(policy["linear_speed_mps"]), float(policy["angular_speed_deg_per_s"]), dt)
    route_follow = policy["route_follow"]
    pos = points[0]
    yaw = float(config["route_points"][0]["yaw_rad"])
    targets = samples[1:]
    target_index = action_count = move_count = turn_count = 0
    clips = []
    min_move = float("inf")
    max_move = 0.0
    max_corridor = 0.0
    max_frames = int(route_follow["max_camera_frames"])
    frame_count = 1  # initial actual observation in production
    while target_index < len(targets):
        if action_count >= max_frames or frame_count >= max_frames:
            raise RuntimeError("exact action emulation reached the configured camera-frame guard")
        target = targets[target_index]
        if xz_distance(pos, target) <= float(route_follow["position_tolerance_m"]):
            target_index += 1
            continue
        target_yaw = yaw_toward_habitat_xz(pos, target)
        delta = yaw_delta_rad(yaw, target_yaw)
        if abs(delta) > math.radians(float(route_follow["yaw_tolerance_deg"])):
            yaw += math.copysign(math.radians(turn_step), delta)
            yaw = math.atan2(math.sin(yaw), math.cos(yaw))
            turn_count += 1
        else:
            expected = (pos[0] - math.sin(yaw) * move_step, pos[1],
                        pos[2] - math.cos(yaw) * move_step)
            clipped_vec = pathfinder.try_step_no_sliding(
                mn.Vector3(*pos), mn.Vector3(*expected))
            clipped = tuple(float(v) for v in clipped_vec)
            clip_error = distance3(clipped, expected)
            clips.append({"action_index": action_count, "position_before_m": list(pos),
                "expected_position_m": list(expected), "pathfinder_result_m": list(clipped),
                "deviation_m": clip_error})
            # This is the actual production step_filter guard: endpoint_tolerance_m.
            if clip_error > float(route_follow["endpoint_tolerance_m"]):
                raise RuntimeError(f"production try_step_no_sliding clip guard failed at action {action_count}: {clip_error:.9f} m")
            if not pathfinder.is_navigable(
                    mn.Vector3(*clipped), max_y_delta=vertical_tolerance):
                raise RuntimeError(f"production vertical/navigability check failed at action {action_count}")
            moved = xz_distance(pos, clipped)
            if moved < move_step * 0.5 or moved > move_step * 1.5:
                raise RuntimeError(f"production move increment check failed at action {action_count}: {moved:.9f} m")
            corridor = point_to_polyline_xz(clipped, nav_path)
            if corridor > 0.35:
                raise RuntimeError(f"production route corridor check failed at action {action_count}: {corridor:.9f} m")
            min_move, max_move = min(min_move, moved), max(max_move, moved)
            max_corridor = max(max_corridor, corridor)
            pos = clipped
            move_count += 1
        action_count += 1
        frame_count += 1
    endpoint = tuple(float(v) for v in points[-1])
    endpoint_error = distance3(pos, endpoint)
    if endpoint_error > float(route_follow["endpoint_tolerance_m"]):
        raise RuntimeError(f"production final endpoint check failed: {endpoint_error:.9f} m")

    # Production has a final bounded turn to the last configured yaw.
    final_yaw = float(config["route_points"][-1]["yaw_rad"])
    while abs(yaw_delta_rad(yaw, final_yaw)) > math.radians(float(route_follow["yaw_tolerance_deg"])):
        if action_count >= max_frames or frame_count >= max_frames:
            raise RuntimeError("final yaw correction reached the configured camera-frame guard")
        delta = yaw_delta_rad(yaw, final_yaw)
        yaw += math.copysign(math.radians(turn_step), delta)
        yaw = math.atan2(math.sin(yaw), math.cos(yaw))
        action_count += 1
        frame_count += 1
        turn_count += 1

    sim_elapsed_requested = action_count * dt
    if sim_elapsed_requested > float(route_follow["max_sim_time_s"]):
        raise RuntimeError("route action duration exceeds configured simulation-time guard under requested dt")
    return {
        "schema": "t4-route-action-geometry-diagnostic/v1",
        "status": "geometry_action_preflight_passed_not_simulator_movement_verified",
        "execution_mode": "pathfinder_geometry_only_no_simulator_no_sensor_capture",
        "run_identity": {k: config[k] for k in ("run_id", "scene_id", "sequence_id", "route_id")},
        "inputs": {"effective_config_path": str(config_path), "effective_config_sha256": sha256(config_path),
            "source_route_path": str(route_path), "source_route_sha256": sha256(route_path),
            "policy_path": str(policy_path), "policy_sha256": sha256(policy_path),
            "navmesh_path": str(navmesh), "navmesh_sha256": sha256(navmesh),
            "scene_asset_sha256": sha256(scene), "scene_dataset_config_sha256": sha256(dataset_config)},
        "habitat_sim_version": str(getattr(habitat_sim, "__version__", "unknown")),
        "navmesh_profile_compatibility": profile,
        "navmesh_scope": {"include_static_objects": bool(settings.include_static_objects),
            "navmesh_baked_agent_radius_m": float(settings.agent_radius),
            "navmesh_baked_agent_height_m": float(settings.agent_height),
            "coverage_limit": "no independent assurance for static object instances omitted from navmesh"},
        "route": {"waypoint_count": len(points), "pathfinder_polyline_point_count": len(nav_path),
            "waypoint_segments": resolved["segments"], "geodesic_distance_m": resolved["geodesic_distance_m"],
            "sample_spacing_m": sample_spacing, "locked_path_samples": len(samples),
            "vertical_navmesh_tolerance_m": vertical_tolerance,
            "existing_geometry_gate": gate_report["status"],
            "existing_gate_clearance_query_radius_m": float(contract["extra_navmesh_clearance_margin_m"])
                + 2.0 * float(contract["numeric_boundary_tolerance_m"])},
        "clearance_diagnostic": {"query_radius_m": diagnostic_radius,
            "sample_count": len(clearance_rows),
            "cap_count": sum(row["status"] == "search_radius_cap_no_wall_reported_within_0.5m" for row in clearance_rows),
            "measured_count": sum(row["status"] == "measured_navmesh_boundary_distance" for row in clearance_rows),
            "unavailable_count": sum(row["status"] == "unavailable_nonfinite" for row in clearance_rows),
            "minimum_measured_navmesh_boundary_distance_m": min(
                (row["value_m"] for row in clearance_rows if row["status"] == "measured_navmesh_boundary_distance"),
                default=None),
            "samples": clearance_rows,
            "meaning": "Distance to the baked navmesh boundary only; not distance to static scene objects."},
        "production_action_emulation": {"production_loop_source": "scripts/run_habitat_smooth_route_preview.py action loop",
            "sample_target_count": len(targets), "action_count": action_count,
            "camera_frame_count_including_initial": frame_count,
            "movement_actions": move_count, "turn_actions": turn_count,
            "pathfinder_try_step_no_sliding_checks": len(clips),
            "max_deviation_from_expected_step_m": max((row["deviation_m"] for row in clips), default=0.0),
            "max_deviation_action_index": max(clips, key=lambda row: row["deviation_m"])["action_index"] if clips else None,
            "deviation_passes_previous_0.025m_diagnostic_gate": max((row["deviation_m"] for row in clips), default=0.0) <= 0.025,
            "production_clip_guard_m": float(route_follow["endpoint_tolerance_m"]),
            "minimum_actual_xz_displacement_m": None if not move_count else min_move,
            "maximum_actual_xz_displacement_m": max_move,
            "maximum_route_corridor_distance_m": max_corridor,
            "final_position_m": list(pos), "final_position_error_m": endpoint_error,
            "final_yaw_error_deg": abs(math.degrees(yaw_delta_rad(yaw, final_yaw))),
            "requested_dt_s": dt, "requested_time_estimate_s": sim_elapsed_requested,
            "camera_frame_guard": max_frames, "simulation_elapsed_guard_s": float(route_follow["max_sim_time_s"]),
            "not_verified_without_simulator": ["Simulator.step/step_filter output", "collided observation flag",
                "sensor render/transform at every step", "wall-clock capture and observed world-time deltas"]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output}")
    result = run(args.config)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({k: result[k] for k in (
        "status", "route", "clearance_diagnostic", "production_action_emulation")
    }, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
