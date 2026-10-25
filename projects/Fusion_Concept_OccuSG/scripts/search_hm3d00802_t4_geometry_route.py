#!/usr/bin/env python3
"""Bounded geometry-only route search for the locked HM3D 00802 navmesh.

This script only loads PathFinder and its saved navmesh. It does not create a
Simulator, render sensors, capture RGB-D, or load any perception/mapping code.
All limits are read from a pre-registered search_lock.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration_v2.camera_pacing import derive_route_budget, motion_increment_for_step
from integration_v2.smooth_route import (
    RouteExecutionError,
    distance3,
    find_waypoint_polyline,
    validate_navmesh_polyline_v2,
    validate_navmesh_profile_compatibility,
    yaw_delta_rad,
    yaw_toward_habitat_xz,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vec3(value: Any) -> tuple[float, float, float]:
    result = tuple(float(x) for x in value)
    if len(result) != 3 or not all(math.isfinite(x) for x in result):
        raise ValueError("point is not finite xyz")
    return result  # type: ignore[return-value]


def xz_distance(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def wrap_abs_deg(delta_rad: float) -> float:
    return abs(math.degrees(yaw_delta_rad(0.0, delta_rad)))


def shortest_path(pathfinder, habitat_sim, mn, start, end):
    query = habitat_sim.ShortestPath()
    query.requested_start = mn.Vector3(*start)
    query.requested_end = mn.Vector3(*end)
    if not pathfinder.find_path(query):
        return None
    points = [vec3(p) for p in query.points]
    if len(points) < 2:
        return None
    return points, float(query.geodesic_distance)


def turn_angles(anchors) -> list[float]:
    headings = [yaw_toward_habitat_xz(a, b) for a, b in zip(anchors, anchors[1:])]
    return [abs(math.degrees(yaw_delta_rad(a, b))) for a, b in zip(headings, headings[1:])]


def emulate_locked_route(points, *, pathfinder, dt_s, linear_speed, angular_speed,
                         position_tolerance, yaw_tolerance, try_step_tolerance,
                         vertical_tolerance, max_actions=20000):
    """Approximate the route executor with navmesh movement calls only."""
    if len(points) < 2:
        return None, "fewer_than_two_waypoints"
    move_step, turn_step_deg = motion_increment_for_step(linear_speed, angular_speed, dt_s)
    yaw = yaw_toward_habitat_xz(points[0], points[1])
    pos = tuple(points[0])
    action_count = 0
    movement_actions = 0
    turn_actions = 0
    collision_checks = 0
    target_index = 1
    while target_index < len(points):
        if action_count >= max_actions:
            return None, "geometry_action_guard"
        target = points[target_index]
        if xz_distance(pos, target) <= position_tolerance:
            target_index += 1
            continue
        target_yaw = yaw_toward_habitat_xz(pos, target)
        delta = yaw_delta_rad(yaw, target_yaw)
        if abs(delta) > math.radians(yaw_tolerance):
            sign = 1.0 if delta > 0 else -1.0
            yaw += sign * math.radians(turn_step_deg)
            yaw = math.atan2(math.sin(yaw), math.cos(yaw))
            turn_actions += 1
        else:
            expected = (
                pos[0] + -math.sin(yaw) * move_step,
                pos[1],
                pos[2] + -math.cos(yaw) * move_step,
            )
            clipped = vec3(pathfinder.try_step_no_sliding(pos, expected))
            collision_checks += 1
            if xz_distance(clipped, expected) > try_step_tolerance:
                return None, "try_step_no_sliding_clipped"
            if not pathfinder.is_navigable(clipped, max_y_delta=vertical_tolerance):
                return None, "movement_vertical_tolerance"
            moved = xz_distance(pos, clipped)
            if moved < move_step * 0.5 or moved > move_step * 1.5:
                return None, "movement_increment_mismatch"
            pos = clipped
            movement_actions += 1
        action_count += 1
    return {
        "action_count": action_count,
        "movement_actions": movement_actions,
        "turn_actions": turn_actions,
        "navmesh_movement_checks": collision_checks,
        "estimated_simulation_duration_s": action_count * dt_s,
        "initial_yaw_rad": yaw_toward_habitat_xz(points[0], points[1]),
        "final_pose_habitat": list(pos),
        "movement_model": "fixed increments + PathFinder.try_step_no_sliding; geometry-only approximation, not Simulator.step verification",
    }, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true",
                        help="validate locked files and PathFinder profile without sampling any route points")
    args = parser.parse_args()
    lock_path = args.lock.resolve()
    output = args.output.resolve()
    lock = json.loads(lock_path.read_text())
    if lock.get("status") != "locked_before_search":
        raise SystemExit("search lock must be marked locked_before_search")
    if output.exists() and not args.preflight_only:
        raise SystemExit(f"refusing to overwrite existing search output: {output}")
    if not args.preflight_only:
        output.mkdir(parents=True)
    lock_sha = sha256(lock_path)

    required = {
        "candidate_generation": ("accepted_leg_geodesic_range_m", "anchor_count", "candidate_chains_cap",
                                 "leg_count", "near_sample_radius_m", "nonconsecutive_anchor_min_xz_distance_m",
                                 "target_draws_per_leg_cap", "start_max_tries", "target_max_tries_per_draw",
                                 "emulated_actions_cap_per_candidate", "progress_every_attempts", "wall_time_cap_s"),
        "geometry_gates": ("geometry_contract_path", "sample_spacing_m", "extra_navmesh_clearance_margin_m",
                           "numeric_boundary_tolerance_m", "vertical_navmesh_tolerance_m",
                           "waypoint_endpoint_tolerance_m", "try_step_no_sliding_max_error_m",
                           "waypoint_dedup_distance_m"),
        "acceptance_and_selection": ("duration_target_sim_s", "duration_target_center_sim_s",
                                     "minimum_turn_angle_deg", "minimum_total_absolute_turn_deg",
                                     "minimum_distinct_movement_turns"),
        "route_runtime_profile": ("resolution_px", "horizontal_fov_deg", "sensor_position_habitat",
                                  "sensor_orientation_habitat_rad"),
    }
    for block, keys in required.items():
        if not isinstance(lock.get(block), dict) or any(key not in lock[block] for key in keys):
            raise SystemExit(f"search lock block {block!r} is missing required keys")
    for name, record in lock.get("input_hashes", {}).items():
        source = Path(record.get("path", ""))
        if not source.is_file() or sha256(source) != record.get("sha256"):
            raise SystemExit(f"locked search input missing/hash mismatch: {name}: {source}")

    import habitat_sim
    import magnum as mn

    actual_habitat_version = str(getattr(habitat_sim, "__version__", "unknown"))
    if actual_habitat_version != lock["search_runtime"]["habitat_sim_version"]:
        raise SystemExit(f"Habitat-Sim version mismatch: expected {lock['search_runtime']['habitat_sim_version']}, got {actual_habitat_version}")

    navmesh = Path(lock["input_hashes"]["navmesh"]["path"])
    expected_navmesh_hash = lock["input_hashes"]["navmesh"]["sha256"]
    if sha256(navmesh) != expected_navmesh_hash:
        raise SystemExit("navmesh hash changed after search was locked")
    pathfinder = habitat_sim.PathFinder()
    if not pathfinder.load_nav_mesh(str(navmesh)):
        raise SystemExit("PathFinder could not load the locked navmesh")
    settings = pathfinder.nav_mesh_settings
    contract_path = ROOT / lock["geometry_gates"]["geometry_contract_path"]
    if sha256(contract_path) != lock["input_hashes"]["geometry_contract"]["sha256"]:
        raise SystemExit("geometry contract changed after search was locked")
    contract = json.loads(contract_path.read_text())
    profile = validate_navmesh_profile_compatibility(settings, geometry_contract=contract)
    pathfinder.seed(int(lock["seed"]))
    island_index = int(lock["navmesh_settings_read_only"]["largest_island_index"])
    actual_area = float(pathfinder.island_area(island_index))
    expected_area = float(lock["navmesh_settings_read_only"]["largest_island_area_m2"])
    if abs(actual_area - expected_area) > 1e-4:
        raise SystemExit("locked largest-island metadata does not match the loaded navmesh")

    if args.preflight_only:
        print(json.dumps({"status": "preflight_passed", "lock_sha256": lock_sha,
                          "locked_input_count": len(lock["input_hashes"]),
                          "profile_compatibility": profile, "candidate_sampling_started": False,
                          "selected_island_index": island_index, "selected_island_area_m2": actual_area},
                         indent=2, allow_nan=False))
        return 0

    cap = int(lock["candidate_generation"]["candidate_chains_cap"])
    target_draws = int(lock["candidate_generation"]["target_draws_per_leg_cap"])
    start_tries = int(lock["candidate_generation"]["start_max_tries"])
    target_tries = int(lock["candidate_generation"]["target_max_tries_per_draw"])
    sample_radius = float(lock["candidate_generation"]["near_sample_radius_m"])
    waypoint_dedup = float(lock["geometry_gates"]["waypoint_dedup_distance_m"])
    leg_min, leg_max = map(float, lock["candidate_generation"]["accepted_leg_geodesic_range_m"])
    turn_min = float(lock["acceptance_and_selection"]["minimum_turn_angle_deg"])
    sum_turn_min = float(lock["acceptance_and_selection"]["minimum_total_absolute_turn_deg"])
    nonadjacent_min = float(lock["candidate_generation"]["nonconsecutive_anchor_min_xz_distance_m"])
    sim_budget = lock["acceptance_and_selection"]
    route_policy = json.loads(Path(lock["input_hashes"]["timebase_policy"]["path"]).read_text())
    dt_s = float(route_policy["simulator_step_s"])
    linear_speed = float(route_policy["linear_speed_mps"])
    angular_speed = float(route_policy["angular_speed_deg_per_s"])
    route_follow = route_policy["route_follow"]
    keyframes = route_policy["keyframes"]
    wall_time_cap_s = float(lock["candidate_generation"]["wall_time_cap_s"])
    deadline = time.monotonic() + wall_time_cap_s
    progress_every = int(lock["candidate_generation"]["progress_every_attempts"])
    progress_path = output / "search_progress.jsonl"
    reasons: Counter[str] = Counter()
    history: list[dict[str, Any]] = []
    accepted = None
    fallback = None
    attempts = 0

    def write_progress(status: str) -> None:
        row = {
            "status": status, "attempts": attempts,
            "elapsed_s": time.monotonic() - (deadline - wall_time_cap_s),
            "rejection_counts": dict(reasons),
        }
        with progress_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()

    for attempt in range(cap):
        if time.monotonic() >= deadline:
            reasons["wall_time_budget_exhausted"] += 1
            break
        attempts += 1
        if attempts == 1 or attempts % progress_every == 0:
            write_progress("searching")
        try:
            start = vec3(pathfinder.get_random_navigable_point(max_tries=start_tries, island_index=island_index))
        except Exception:
            reasons["invalid_start_sample"] += 1
            continue
        if not pathfinder.is_navigable(start, max_y_delta=float(contract["vertical_navmesh_tolerance_m"])):
            reasons["start_not_navigable"] += 1
            continue
        anchors = [start]
        leg_paths = []
        failed = None
        for leg_index in range(int(lock["candidate_generation"]["leg_count"])):
            chosen = None
            for draw in range(target_draws):
                try:
                    raw = pathfinder.get_random_navigable_point_near(
                        mn.Vector3(*anchors[-1]), sample_radius, max_tries=target_tries,
                        island_index=island_index)
                    target = vec3(raw)
                except Exception:
                    reasons["invalid_near_sample"] += 1
                    continue
                if not pathfinder.is_navigable(target, max_y_delta=float(contract["vertical_navmesh_tolerance_m"])):
                    reasons["target_not_navigable"] += 1
                    continue
                if any(xz_distance(target, prior) < nonadjacent_min
                       for prior in anchors[:-1]):
                    reasons["nonconsecutive_anchor_revisit"] += 1
                    continue
                result = shortest_path(pathfinder, habitat_sim, mn, anchors[-1], target)
                if result is None:
                    reasons["no_connected_path"] += 1
                    continue
                leg_points, leg_distance = result
                if not leg_min <= leg_distance <= leg_max:
                    reasons["leg_geodesic_out_of_range"] += 1
                    continue
                chosen = (target, leg_points, leg_distance)
                break
            if chosen is None:
                failed = "could_not_sample_valid_leg"
                break
            target, leg_points, leg_distance = chosen
            anchors.append(target)
            leg_paths.append((leg_points, leg_distance))
        if failed:
            reasons[failed] += 1
            continue

        angles = turn_angles(anchors)
        if len([angle for angle in angles if angle >= turn_min]) < int(lock["acceptance_and_selection"]["minimum_distinct_movement_turns"]):
            reasons["insufficient_movement_turns"] += 1
            continue
        if sum(angles) < sum_turn_min:
            reasons["insufficient_total_turn"] += 1
            continue

        polyline = []
        for leg_points, _distance in leg_paths:
            if not polyline:
                polyline.extend(leg_points)
            else:
                if distance3(polyline[-1], leg_points[0]) > float(lock["geometry_gates"]["waypoint_endpoint_tolerance_m"]):
                    failed = "path_legs_do_not_join"
                    break
                polyline.extend(leg_points[1:])
        if failed:
            reasons[failed] += 1
            continue
        waypoints = []
        for point in polyline:
            if not waypoints or distance3(waypoints[-1], point) > waypoint_dedup:
                waypoints.append(point)
        if len(waypoints) < 4:
            reasons["insufficient_path_waypoints"] += 1
            continue
        try:
            resolved = find_waypoint_polyline(
                pathfinder, waypoints, shortest_path_factory=habitat_sim.ShortestPath,
                vector_factory=lambda *values: mn.Vector3(*values),
                endpoint_tolerance_m=float(lock["geometry_gates"]["waypoint_endpoint_tolerance_m"]))
            path = resolved["points"]
            validation = validate_navmesh_polyline_v2(
                pathfinder, path, max_step_m=float(lock["geometry_gates"]["sample_spacing_m"]),
                geometry_contract=contract, navmesh_settings=settings,
                endpoint_tolerance_m=float(lock["geometry_gates"]["try_step_no_sliding_max_error_m"]),
                vertical_navmesh_tolerance_m=float(contract["vertical_navmesh_tolerance_m"]))
        except (RouteExecutionError, ValueError) as exc:
            message = str(exc)
            if "not navigable" in message or "vertical tolerance" in message:
                reasons["polyline_not_navigable_or_vertical_tolerance"] += 1
            elif "clearance" in message or "boundary" in message:
                reasons["extra_clearance_or_numeric_boundary"] += 1
            elif "clipped" in message:
                reasons["try_step_no_sliding_clipped"] += 1
            else:
                reasons["polyline_validation_error"] += 1
            history.append({"attempt": attempt + 1, "status": "rejected", "reason": message})
            continue

        # Route points mirror the executor's ordered targets. Start orientation
        # follows the first route segment; each bend remains a moving waypoint.
        route_points = []
        for idx, point in enumerate(waypoints):
            if idx < len(waypoints) - 1:
                yaw = yaw_toward_habitat_xz(point, waypoints[idx + 1])
            else:
                yaw = yaw_toward_habitat_xz(waypoints[-2], waypoints[-1])
            route_points.append({"position_habitat": list(point), "yaw_rad": yaw,
                                 "label": "start" if idx == 0 else ("end" if idx == len(waypoints)-1 else f"navmesh_turn_{idx:02d}")})
        emulated, failure_reason = emulate_locked_route(
            waypoints, pathfinder=pathfinder, dt_s=dt_s, linear_speed=linear_speed,
            angular_speed=angular_speed,
            position_tolerance=float(route_follow["position_tolerance_m"]),
            yaw_tolerance=float(route_follow["yaw_tolerance_deg"]),
            try_step_tolerance=float(lock["geometry_gates"]["waypoint_endpoint_tolerance_m"]),
            vertical_tolerance=float(contract["vertical_navmesh_tolerance_m"]),
            max_actions=int(lock["candidate_generation"]["emulated_actions_cap_per_candidate"]))
        if emulated is None:
            reasons[failure_reason or "movement_emulation_failed"] += 1
            history.append({"attempt": attempt + 1, "status": "rejected", "reason": failure_reason})
            continue
        budget = derive_route_budget(
            route_points, simulation_dt_s=dt_s, linear_speed_mps=linear_speed,
            angular_speed_deg_per_s=angular_speed,
            path_sample_step_m=float(route_follow["path_sample_step_m"]),
            keyframe_min_translation_m=float(keyframes["min_translation_m"]),
            keyframe_min_yaw_deg=float(keyframes["min_yaw_deg"]),
            max_camera_frames=100000)
        duration = float(emulated["estimated_simulation_duration_s"])
        target_low, target_high = map(float, sim_budget["duration_target_sim_s"])
        chain_distance = sum(distance for _points, distance in leg_paths)
        candidate = {
            "attempt": attempt + 1,
            "anchor_waypoints_habitat": [list(p) for p in anchors],
            "route_points": route_points,
            "pathfinder_polyline_habitat": [list(p) for p in path],
            "geodesic_distance_m": chain_distance,
            "total_abs_anchor_turn_deg": sum(angles),
            "anchor_turn_angles_deg": angles,
            "anchor_turn_waypoints_habitat": [list(point) for point in anchors],
            "duration_estimate": emulated,
            "derived_route_budget": budget,
            "clearance_validation": validation,
            "route_path_segment_count": len(waypoints) - 1,
            "route_transform_check": {
                "agent_radius_m": float(contract["runtime_agent_profile"]["radius_m"]),
                "agent_height_m": float(contract["runtime_agent_profile"]["height_m"]),
                "sensor_position_habitat": lock["route_runtime_profile"]["sensor_position_habitat"],
                "sensor_orientation_habitat_rad": lock["route_runtime_profile"]["sensor_orientation_habitat_rad"],
                "all_waypoints_finite": True,
                "note": "static transform provenance checked; no sensor render/collision check performed",
            },
            "target_duration_status": "within_target" if target_low <= duration <= target_high else "outside_target",
        }
        history.append({"attempt": attempt + 1, "status": "geometry_valid",
                        "duration_s": duration, "geodesic_distance_m": chain_distance,
                        "turn_deg": sum(angles)})
        score = abs(duration - float(sim_budget["duration_target_center_sim_s"]))
        candidate["duration_target_error_s"] = score
        if target_low <= duration <= target_high:
            accepted = candidate
            break
        if fallback is None or score < fallback["duration_target_error_s"]:
            fallback = candidate

    result = {
        "schema": "hm3d-geometry-route-search-result/v1",
        "search_id": lock["search_id"],
        "execution_mode": "geometry_only_navmesh_pathfinder",
        "search_lock_path": str(lock_path), "search_lock_sha256": lock_sha,
        "candidate_attempts": attempts, "candidate_chain_cap": cap,
        "wall_time_elapsed_s": time.monotonic() - (deadline - wall_time_cap_s),
        "rejection_counts": dict(reasons),
        "profile_compatibility": profile,
        "navmesh_settings": {
            "agent_radius_m": float(settings.agent_radius), "agent_height_m": float(settings.agent_height),
            "cell_size_m": float(settings.cell_size), "cell_height_m": float(settings.cell_height),
            "include_static_objects": bool(settings.include_static_objects),
            "selected_connected_component_index": island_index,
            "selected_connected_component_area_m2": actual_area,
            "selection_basis": "largest navmesh island by area; no floor semantics inferred",
        },
        "candidate": accepted or fallback,
        "candidate_status": ("within_duration_target" if accepted else
                             "geometry_valid_but_outside_duration_target" if fallback else "no_candidate"),
        "history": history,
        "known_limits": [
            "no Simulator/agent action/render was run; movement emulation is a PathFinder-only approximation",
            "navmesh records include_static_objects=false; static-object collision is not verified",
            "this route geometry result is not a room/floor scope proof",
            "no runtime movement or capture verification",
        ],
    }
    result_path = output / "search_result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    write_progress("completed" if result["candidate"] else "no_candidate")
    if result["candidate"]:
        svg = output / "route_topdown.svg"
        render_topdown_svg(result["candidate"], svg)
        result["route_topdown_svg"] = str(svg)
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({k: result[k] for k in (
        "candidate_attempts", "wall_time_elapsed_s", "rejection_counts", "candidate_status", "candidate")
        if k != "candidate" or result[k] is not None}, indent=2, allow_nan=False))
    return 0 if result["candidate"] else 2


def render_topdown_svg(candidate: dict[str, Any], path: Path) -> None:
    points = [p["position_habitat"] for p in candidate["route_points"]]
    anchors = candidate.get("anchor_turn_waypoints_habitat", [])
    xs = [float(p[0]) for p in points]
    zs = [float(p[2]) for p in points]
    pad = 0.7
    min_x, max_x = min(xs)-pad, max(xs)+pad
    min_z, max_z = min(zs)-pad, max(zs)+pad
    width, height = 1000, 760
    scale = min((width-120)/(max_x-min_x), (height-120)/(max_z-min_z))
    def xy(point):
        x = 60 + (float(point[0])-min_x)*scale
        y = 60 + (max_z-float(point[2]))*scale
        return x, y
    poly = " ".join(f"{x:.2f},{y:.2f}" for x,y in map(xy, points))
    grid_lines=[]
    step=1.0
    gx=math.floor(min_x/step)*step
    while gx<=max_x:
        x=60+(gx-min_x)*scale
        grid_lines.append(f'<line x1="{x:.2f}" y1="45" x2="{x:.2f}" y2="{height-50}" stroke="#e6e9ee"/>')
        grid_lines.append(f'<text x="{x+2:.2f}" y="{height-28}" font-size="12" fill="#556">{gx:.0f} m</text>')
        gx+=step
    gz=math.floor(min_z/step)*step
    while gz<=max_z:
        y=60+(max_z-gz)*scale
        grid_lines.append(f'<line x1="45" y1="{y:.2f}" x2="{width-45}" y2="{y:.2f}" stroke="#e6e9ee"/>')
        grid_lines.append(f'<text x="8" y="{y-2:.2f}" font-size="12" fill="#556">{gz:.0f} m</text>')
        gz+=step
    marks=[]
    for i,point in enumerate(anchors):
        x,y=xy(point)
        if i==0:
            color,label="#159447","START"
        elif i==len(anchors)-1:
            color,label="#c33131","END"
        else:
            color,label="#e18b1d",f"TURN {i}"
        marks.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="7" fill="{color}" stroke="white" stroke-width="2"/><text x="{x+10:.2f}" y="{y-9:.2f}" font-size="13" font-weight="bold" fill="{color}">{label}</text>')
    text=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
          '<rect width="100%" height="100%" fill="white"/>'
          '<text x="20" y="28" font-size="18" font-weight="bold">HM3D 00802 T4 geometry-only route — XZ projection (m)</text>'
          '<text x="20" y="48" font-size="12" fill="#555">Navmesh context omitted; Y/elevation is retained per waypoint in route JSON. Not a floor/scope map.</text>'
          + ''.join(grid_lines)+f'<polyline points="{poly}" fill="none" stroke="#1476c8" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
          + ''.join(marks)+'</svg>')
    path.write_text(text)


if __name__ == "__main__":
    raise SystemExit(main())
