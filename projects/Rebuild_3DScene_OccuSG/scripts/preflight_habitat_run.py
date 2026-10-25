#!/usr/bin/env python3
"""Read-only preflight for a versioned Habitat scene-evaluation profile."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HABITAT_PYTHON = Path("/home/kiettran05/miniconda3/envs/habitat/bin/python")
NATIVE_BINARIES = (
    ROOT / "build/native-m3/occupancy/standalone_sg_octomap_fixture_runner",
    ROOT / "build/native-m4-dude/occusg_dude_worker",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def run(profile_path: Path, run_root: Path, habitat_python: Path, *,
        skip_route_validation: bool = False) -> dict:
    profile_path = profile_path.resolve()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    def check_file(label: str, value: str | Path | None, *, hash_file: bool = False) -> Path | None:
        if not value:
            return None
        path = project_path(value)
        exists = path.is_file()
        row = {"label": label, "path": str(path), "exists": exists}
        if exists:
            row["size_bytes"] = path.stat().st_size
            if hash_file:
                row["sha256"] = sha256(path)
        checks.append(row)
        if not exists:
            errors.append(f"missing {label}: {path}")
        return path

    if profile.get("protocol_id") != "habitat_scene_eval_v1":
        errors.append("profile protocol_id must be habitat_scene_eval_v1")
    mode = profile.get("workflow_mode")
    if mode not in {"diagnostic_only", "partial_gt_evaluation", "gt_evaluation"}:
        errors.append(f"unsupported workflow_mode: {mode!r}")

    dataset = profile.get("dataset", {})
    asset_field = "scene_file" if dataset.get("scene_file") else "scene_instance"
    scene_file = check_file(f"dataset.{asset_field}", dataset.get(asset_field))
    scene_config = check_file("dataset.scene_dataset_config", dataset.get("scene_dataset_config"), hash_file=True)
    navmesh = check_file("dataset.navmesh_file", dataset.get("navmesh_file"))
    check_file("dataset.semantic_mesh_evaluator_only", dataset.get("semantic_mesh_evaluator_only"))
    check_file("dataset.semantic_descriptor_evaluator_only", dataset.get("semantic_descriptor_evaluator_only"))
    if profile.get("route_pose_source") is None:
        errors.append("profile must declare route_pose_source")

    route_config = check_file("route_config", profile.get("route_config"), hash_file=True)
    route = json.loads(route_config.read_text(encoding="utf-8")) if route_config and route_config.is_file() else {}
    if route_config and route:
        if not isinstance(route.get("route"), list) or len(route["route"]) < 2:
            errors.append("route config needs at least two poses")
        if route.get("scene_id") and dataset.get("scene_id") and route["scene_id"] != dataset["scene_id"]:
            errors.append("profile scene_id differs from route config scene_id")
        for key in ("resolution_px", "horizontal_fov_deg", "sensor_position_habitat", "simulation_step_s"):
            if key not in route:
                errors.append(f"route config missing required sensor/capture field: {key}")
        if bool(route.get("require_navigable", False)) and not navmesh:
            errors.append("route requires navigability but profile has no navmesh_file")
        if route.get("require_navigable", False) and skip_route_validation:
            warnings.append("route/navmesh navigability was explicitly not validated")
        for route_key, expected in (("scene_dataset_config", dataset.get("scene_dataset_config")),
                                    ("navmesh_file", dataset.get("navmesh_file"))):
            if expected and route.get(route_key) and project_path(route[route_key]) != project_path(expected):
                errors.append(f"profile dataset.{route_key} differs from route config {route_key}")
        sensor = profile.get("sensor", {})
        if sensor.get("resolution_px") and list(sensor["resolution_px"]) != list(route.get("resolution_px", [])):
            errors.append("profile sensor resolution differs from route config")
        if sensor.get("horizontal_fov_deg") is not None and route.get("horizontal_fov_deg") is not None:
            if abs(float(sensor["horizontal_fov_deg"]) - float(route["horizontal_fov_deg"])) > 1e-6:
                errors.append("profile sensor HFOV differs from route config")
        if route.get("predictor"):
            for route_key, configured_profile_path in (("pipeline_config", profile.get("pipeline_config")),
                                                       ("detector_config", profile.get("perception", {}).get("detector_profile")),
                                                       ("semantic_rules", profile.get("semantic_rules"))):
                if configured_profile_path and route["predictor"].get(route_key) and project_path(route["predictor"][route_key]) != project_path(configured_profile_path):
                    errors.append(f"profile {route_key} differs from route predictor config")

    predictor = profile.get("predictor", {})
    perception = profile.get("perception", {})
    check_file("pipeline_config", predictor.get("pipeline_config") or profile.get("pipeline_config"), hash_file=True)
    detector_config = check_file("detector_profile", perception.get("detector_profile"), hash_file=True)
    weights = check_file("detector_weights", perception.get("weights"), hash_file=True)
    semantic_rules = check_file("semantic_rules", predictor.get("semantic_rules") or profile.get("semantic_rules"), hash_file=True)
    if detector_config and detector_config.is_file() and weights and weights.is_file():
        model_cfg = json.loads(detector_config.read_text(encoding="utf-8")).get("model", {})
        expected = model_cfg.get("weights_sha256")
        if expected and sha256(weights) != expected:
            errors.append("detector weight SHA-256 does not match detector profile")

    gt = profile.get("ground_truth", {})
    if mode == "diagnostic_only" and any(gt.get(k) for k in ("room_polygons", "object_instance_masks", "functional_room_labels")):
        errors.append("diagnostic_only profile must not attach scoring GT")
    if mode != "diagnostic_only" and gt.get("room_polygons"):
        room_gt = check_file("confirmed_room_gt", gt["room_polygons"], hash_file=True)
        if room_gt and room_gt.is_file():
            room_doc = json.loads(room_gt.read_text(encoding="utf-8"))
            status = str(room_doc.get("metadata", {}).get("status", ""))
            if not any(token in status for token in ("human_verified", "user_confirmed")):
                errors.append(f"room GT is not confirmed (metadata.status={status!r})")
            expected_status = gt.get("room_boundary_status")
            if expected_status and status != expected_status:
                errors.append("room GT metadata status differs from profile room_boundary_status")
    elif mode in {"gt_evaluation", "partial_gt_evaluation"}:
        warnings.append("no confirmed room polygons attached; room metrics will remain unavailable")
    for key in ("object_instance_manifest", "doorways_adjacency"):
        value = gt.get(key)
        if value and "not_annotated" not in str(value):
            check_file(f"ground_truth.{key}", value, hash_file=True)

    for binary in NATIVE_BINARIES:
        check_file(f"native_binary:{binary.name}", binary, hash_file=True)
    route_predictor = route.get("predictor", {}) if route else {}
    if route_predictor.get("baseline_lock"):
        check_file("baseline_lock", route_predictor["baseline_lock"], hash_file=True)

    run_root = run_root.expanduser().resolve()
    if run_root.exists():
        errors.append(f"run output already exists; choose a new unique path: {run_root}")
    else:
        parent = run_root.parent
        if not parent.exists():
            warnings.append(f"run output parent will need creation: {parent}")
        elif not parent.is_dir():
            errors.append(f"run output parent is not a directory: {parent}")

    habitat_python = habitat_python.expanduser().resolve()
    if not habitat_python.is_file():
        errors.append(f"Habitat Python interpreter not found: {habitat_python}")
    else:
        probe = subprocess.run([str(habitat_python), "-c", "import habitat_sim; print(habitat_sim.__version__)"],
                               text=True, capture_output=True, check=False)
        checks.append({"label": "Habitat-Sim import", "interpreter": str(habitat_python),
                       "returncode": probe.returncode, "version": probe.stdout.strip() if probe.returncode == 0 else None})
        if probe.returncode:
            errors.append(f"Habitat-Sim import failed: {probe.stderr.strip()[-1200:]}")
        elif profile.get("habitat_sim_version") and probe.stdout.strip() != profile["habitat_sim_version"]:
            errors.append(f"Habitat-Sim version mismatch: expected {profile['habitat_sim_version']}, got {probe.stdout.strip()}")

    route_validation = None
    if not skip_route_validation and route_config and route_config.is_file() and scene_file and scene_file.is_file() and habitat_python.is_file():
        with tempfile.TemporaryDirectory(prefix="habitat-preflight-") as temp:
            report_path = Path(temp) / "route_validation.json"
            command = [str(habitat_python), str(ROOT / "scripts/validate_habitat_route.py"),
                       "--scene", str(scene_file), "--config", str(route_config), "--output", str(report_path)]
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
            if report_path.is_file():
                route_doc = json.loads(report_path.read_text(encoding="utf-8"))
                route_validation = {key: route_doc.get(key) for key in (
                    "status", "scene_id", "route_config_sha256", "pose_count",
                    "navigable_pose_count", "all_poses_navigable", "all_consecutive_poses_connected")}
            checks.append({"label": "route_navigability", "returncode": result.returncode,
                           "pose_count": None if route_validation is None else route_validation.get("pose_count"),
                           "all_poses_navigable": None if route_validation is None else route_validation.get("all_poses_navigable"),
                           "all_consecutive_poses_connected": None if route_validation is None else route_validation.get("all_consecutive_poses_connected"),
                           "route_config_sha256": None if route_validation is None else route_validation.get("route_config_sha256")})
            if result.returncode:
                errors.append(f"route validation failed: {(result.stderr or result.stdout).strip()[-1600:]}")
            elif route_validation and route_validation.get("status") != "pass":
                errors.append("route validator returned a non-pass status")

    return {"schema_version": 1, "kind": "habitat_run_preflight", "status": "pass" if not errors else "fail",
            "profile_id": profile.get("profile_id"), "profile_sha256": sha256(profile_path),
            "workflow_mode": mode, "run_root": str(run_root), "route_validation": route_validation,
            "checks": checks, "warnings": warnings, "errors": errors,
            "predictor_gt_leakage": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True, help="configs/evaluation/*scene_eval_v1.json")
    parser.add_argument("--run-root", type=Path, required=True, help="must not already exist")
    parser.add_argument("--habitat-python", type=Path, default=DEFAULT_HABITAT_PYTHON)
    parser.add_argument("--skip-route-validation", action="store_true", help="asset-only check; does not validate waypoint navigability")
    args = parser.parse_args()
    result = run(args.profile, args.run_root, args.habitat_python, skip_route_validation=args.skip_route_validation)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
