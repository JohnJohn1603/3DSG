#!/usr/bin/env python3
"""Materialize a review-only effective route from a locked geometry search."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--base-effective", type=Path, required=True)
    parser.add_argument("--route-out", type=Path, required=True)
    parser.add_argument("--policy-out", type=Path, required=True)
    parser.add_argument("--effective-out", type=Path, required=True)
    args = parser.parse_args()

    lock_path = args.lock.resolve()
    result_path = args.result.resolve()
    base_path = args.base_effective.resolve()
    route_out, policy_out, effective_out = (p.resolve() for p in
                                              (args.route_out, args.policy_out, args.effective_out))
    for path in (route_out, policy_out, effective_out):
        if path.exists():
            raise SystemExit(f"refusing to overwrite existing candidate config: {path}")

    lock = json.loads(lock_path.read_text())
    result = json.loads(result_path.read_text())
    base = json.loads(base_path.read_text())
    candidate = result.get("candidate")
    if result.get("candidate_status") != "within_duration_target" or not isinstance(candidate, dict):
        raise SystemExit("search result does not contain an in-target geometry candidate")
    if result.get("search_lock_sha256") != sha256(lock_path):
        raise SystemExit("search result is not bound to this locked search configuration")
    for source, key in ((Path(lock["input_hashes"]["navmesh"]["path"]), "sha256"),
                        (Path(lock["input_hashes"]["scene_asset"]["path"]), "sha256")):
        expected = lock["input_hashes"]["navmesh" if source == Path(lock["input_hashes"]["navmesh"]["path"]) else "scene_asset"][key]
        if sha256(source) != expected:
            raise SystemExit(f"locked route source changed: {source}")

    points = candidate["route_points"]
    if len(points) < 4:
        raise SystemExit("candidate does not contain start, two movement turns, and end")
    if not all(all(math.isfinite(float(v)) for v in row["position_habitat"]) and
               math.isfinite(float(row["yaw_rad"])) for row in points):
        raise SystemExit("candidate contains non-finite pose data")

    sequence_id = "hm3d-v02-00802-t4-geometry-candidate-v1"
    route_id = "t4_geometry_turn_continue_candidate_v1"
    run_id = "t4_hm3d00802_geometry_candidate_v1_review_only"
    route = {
        "schema_version": 1,
        "dataset_type": "hm3d",
        "dataset_version": "v0.2",
        "split": "minival",
        "scene_id": base["scene_id"],
        "scene_provenance": "Matterport HM3D v0.2 minival local asset",
        "scene_asset": base["scene_asset"],
        "scene_asset_sha256": base["scene_asset_sha256"],
        "scene_dataset_config": base["scene_dataset_config"],
        "scene_dataset_config_sha256": base["scene_dataset_config_sha256"],
        "navmesh_file": base["navmesh_file"],
        "navmesh_sha256": base["navmesh_sha256"],
        "route_id": route_id,
        "sequence_id": sequence_id,
        "frame_id_prefix": sequence_id,
        "resolution_px": list(base["resolution_px"]),
        "horizontal_fov_deg": base["horizontal_fov_deg"],
        "sensor_position_habitat": list(base["sensor_position_habitat"]),
        "sensor_orientation_habitat_rad": list(base["sensor_orientation_habitat_rad"]),
        "calibration_id": base["calibration_id"],
        "min_depth_m": base["min_depth_m"],
        "max_depth_m": base["max_depth_m"],
        "simulation_step_s": lock["acceptance_and_selection"]["nominal_dt_s"],
        "simulator_seed": base["seed"],
        "route_policy": "Frozen geometry-only four-anchor route: move to anchor 1, turn while moving toward anchor 2, then turn and continue to end. Each leg is independently connected by Habitat PathFinder. No predictor, object labels, room predictions, or semantic GT informed selection.",
        "route_collision_scope": "Navmesh footprint compatibility, navigability, bounded PathFinder clearance, and PathFinder.try_step_no_sliding only. Physics is disabled and navmesh include_static_objects=false; this is not assurance against every static scene object.",
        "geometry_preflight_status": "passed_pathfinder_only_runtime_movement_unverified",
        "geometry_search_id": lock["search_id"],
        "geometry_search_lock_path": str(lock_path),
        "geometry_search_lock_sha256": sha256(lock_path),
        "geometry_search_result_path": str(result_path),
        "geometry_search_result_sha256": sha256(result_path),
        "candidate_route_form_review": "The first movement turn is a near reversal; inspect the supplied top-down drawing before approving any pilot. No runtime movement claim.",
        "route": [{"position_habitat": row["position_habitat"], "yaw_rad": row["yaw_rad"],
                   "label": ("start" if i == 0 else "end" if i == len(points)-1 else f"movement_turn_{i:02d}")}
                  for i, row in enumerate(points)],
        "predictor": {"semantic_gt_input": False, "navmesh_input": False},
        "autonomous_navigation_value_map_slam": False,
    }
    write_json(route_out, route)

    policy = json.loads(json.dumps(base["policy"]))
    policy["schema"] = "smooth-route-t1-policy/v6"
    policy["policy_id"] = "smooth_route_hm3d00802_geometry_candidate_v6"
    policy.pop("agent_footprint", None)
    contract_path = ROOT / lock["geometry_gates"]["geometry_contract_path"]
    policy["geometry_contract"] = json.loads(contract_path.read_text())
    policy["route_follow"].pop("vertical_navmesh_tolerance_m", None)
    policy["route_follow"]["max_camera_frames"] = 1800
    policy["route_follow"]["max_sim_time_s"] = 75.0
    policy["motion_timebase"]["operational_stop_limits"]["max_camera_frames"] = 1800
    policy["motion_timebase"]["operational_stop_limits"]["max_simulation_elapsed_s"] = 75.0
    write_json(policy_out, policy)

    effective = json.loads(json.dumps(base))
    effective["run_id"] = run_id
    effective["sequence_id"] = sequence_id
    effective["source_sequence_id"] = sequence_id
    effective["route_id"] = route_id
    effective["source_route_path"] = str(route_out)
    effective["source_route_sha256"] = sha256(route_out)
    effective["policy_path"] = str(policy_out)
    effective["policy_sha256"] = sha256(policy_out)
    effective["policy"] = policy
    effective["route_points"] = [{"label": row["label"], "position_habitat": row["position_habitat"],
                                   "source_index": i, "yaw_rad": row["yaw_rad"]}
                                  for i, row in enumerate(points)]
    effective["selected_route_indices"] = [0, len(points) - 1]
    effective["geometry_candidate_provenance"] = {
        "status": "geometry_preflight_passed_review_only_not_runtime_verified",
        "search_id": lock["search_id"], "search_lock_path": str(lock_path),
        "search_lock_sha256": sha256(lock_path), "result_path": str(result_path),
        "result_sha256": sha256(result_path), "route_form_review_required": True,
        "no_capture_or_perception_performed": True,
    }
    effective["route_budget_proposal"] = {
        "max_camera_frames": 1800, "max_keyframe_candidates": 1800,
        "max_simulation_elapsed_s": 75.0, "camera_runner_timeout_s": 240.0,
        "basis": "1569 conservative frame upper bound from locked geometry/timebase plus a bounded guard; no producer run performed",
    }
    write_json(effective_out, effective)
    print(json.dumps({"route_config": str(route_out), "route_sha256": sha256(route_out),
                      "policy_config": str(policy_out), "policy_sha256": sha256(policy_out),
                      "effective_config": str(effective_out), "effective_sha256": sha256(effective_out),
                      "route_points": len(points), "route_id": route_id, "sequence_id": sequence_id},
                     indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
