#!/usr/bin/env python3
"""T1 camera-only, stepwise Habitat route preview.

This worker deliberately stops before perception, object mapping, OccuSG and
room semantics. Every preview frame is rendered by Habitat-Sim at the pose
actually reached by simulator actions. Candidate RGB-D packets are copied from
actual sensor observations and written by a bounded latest-pending queue.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import resource
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from adapters.habitat.calibration import habitat_intrinsics_values  # noqa: E402
from adapters.habitat.raw import canonical_from_habitat_matrix, pose_matrix  # noqa: E402
from integration_v2.smooth_route import (  # noqa: E402
    BoundedKeyframeWriter,
    KeyframePolicyV1,
    RouteExecutionError,
    build_clearance_preflight_artifacts,
    distance3,
    find_waypoint_polyline,
    sample_polyline,
    validate_navmesh_polyline,
    validate_navmesh_polyline_v2,
    validate_navmesh_profile_compatibility,
    resolve_vertical_navmesh_tolerance,
    wrap_angle_rad,
    yaw_delta_rad,
    yaw_toward_habitat_xz,
)
from integration_v2.camera_live_startup import (  # noqa: E402
    StartupContractError,
    resolve_camera_producer_manifest_path,
    validate_bootstrap_before_capture,
    validate_route_startup,
)
from integration_v2.camera_pacing import (  # noqa: E402
    SimulationClockPacer, actual_pose_speeds, derive_route_budget, motion_increment_for_step,
    simulation_step_record, validate_simulation_elapsed,
)


class EventJournal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, row: Dict[str, Any]) -> None:
        record = {"wall_utc": datetime.now(timezone.utc).isoformat(), **row}
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()


class MemoryEventJournal:
    """No-filesystem event sink used only by the pre-capture startup dry-run."""
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []

    def emit(self, row: Dict[str, Any]) -> None:
        self.rows.append({"wall_utc": datetime.now(timezone.utc).isoformat(), **row})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _host_monotonic_domain() -> str:
    """Identify the host monotonic clock domain shared by this run's workers."""
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if not boot_id_path.is_file():
        raise RuntimeError("cannot identify host monotonic clock domain")
    boot_id = boot_id_path.read_text(encoding="ascii").strip()
    if not boot_id:
        raise RuntimeError("host monotonic clock boot ID is empty")
    return f"host_monotonic:{boot_id}"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _atomic_npy(path: Path, array: np.ndarray) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            np.save(stream, array, allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return _sha256(path)


def _atomic_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return _sha256(path)


def _position(state: Any) -> Tuple[float, float, float]:
    return tuple(float(x) for x in state.position)


def _rotation_matrix(rotation: Any, quaternion_module: Any) -> np.ndarray:
    result = np.asarray(quaternion_module.as_rotation_matrix(rotation), dtype=np.float64)
    if result.shape != (3, 3) or not np.isfinite(result).all():
        raise RuntimeError("Habitat returned an invalid rotation matrix")
    return result


def _yaw(rotation_matrix: np.ndarray) -> float:
    forward = rotation_matrix @ np.array([0.0, 0.0, -1.0], dtype=np.float64)
    return math.atan2(-float(forward[0]), -float(forward[2]))


def _xz_distance(a: Any, b: Any) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


def _point_segment_distance_xz(point: Any, start: Any, end: Any) -> float:
    px, pz = float(point[0]), float(point[2])
    ax, az = float(start[0]), float(start[2])
    bx, bz = float(end[0]), float(end[2])
    dx, dz = bx - ax, bz - az
    denom = dx * dx + dz * dz
    if denom <= 1e-12:
        return math.hypot(px - ax, pz - az)
    t = min(1.0, max(0.0, ((px - ax) * dx + (pz - az) * dz) / denom))
    return math.hypot(px - (ax + t * dx), pz - (az + t * dz))


def _polyline_distance_xz(point: Any, path: List[Tuple[float, float, float]]) -> float:
    return min(_point_segment_distance_xz(point, a, b) for a, b in zip(path, path[1:]))


def _load_json(path: Path) -> Dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"expected JSON object: {path}")
    return doc


def _write_keyframe(packet: Dict[str, Any], root: Path) -> Dict[str, Any]:
    frame_id = str(packet["frame_id"])
    frame_dir = root / "keyframes" / frame_id
    if frame_dir.exists():
        raise FileExistsError(f"refusing to overwrite keyframe {frame_id}")
    frame_dir.mkdir(parents=True)
    hashes = {
        "rgb.npy": _atomic_npy(frame_dir / "rgb.npy", packet["rgb"]),
        "depth_m.npy": _atomic_npy(frame_dir / "depth_m.npy", packet["depth_m"]),
        "valid_mask.npy": _atomic_npy(frame_dir / "valid_mask.npy", packet["valid_mask"]),
    }
    metadata = dict(packet["metadata"])
    metadata["raw_files_sha256"] = hashes
    _atomic_json(frame_dir / "metadata.json", metadata)
    hashes["metadata.json"] = _sha256(frame_dir / "metadata.json")
    return {"frame_id": frame_id, "path": str(frame_dir.relative_to(root)),
            "files_sha256": hashes}


def _build_html(index_rows: List[Dict[str, Any]], output: Path) -> str:
    import html

    payload = json.dumps(index_rows, separators=(",", ":"), ensure_ascii=True).replace("</", "<\\/")
    markup = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera-only smooth route preview</title><style>
body{font:16px system-ui,sans-serif;margin:0;background:#101722;color:#edf3fb}header{padding:14px 20px;background:#152940}
main{max-width:1200px;margin:18px auto;padding:0 16px}.controls{display:flex;gap:10px;align-items:center;margin:12px 0}
img{display:block;max-width:min(100%,960px);max-height:72vh;margin:auto;background:#000;object-fit:contain;border:1px solid #44546a}
input[type=range]{flex:1}.note{color:#b9c9dc}.tag{color:#ffd477;font-weight:700}
</style></head><body><header><strong>Habitat smooth route · camera-only preview</strong><div class="tag">No detector, encoder, mapper, room update, or semantic prediction ran.</div></header>
<main><div class="controls"><button id="play">Play</button><button id="prev">Previous</button><button id="next">Next</button>
<input id="slider" type="range" min="0" max="0" value="0"><span id="counter"></span></div>
<img id="frame" alt="Actual RGB frame rendered by Habitat-Sim"><p id="details" class="note"></p>
<p class="note">Playback presents captured simulator frames; it does not claim wall-clock realtime.</p></main>
<script>const frames=__FRAMES__;let index=0,timer=null;const img=document.getElementById('frame'),slider=document.getElementById('slider');
function show(i){index=Math.max(0,Math.min(frames.length-1,i));const f=frames[index];img.src=f.path;slider.value=index;
document.getElementById('counter').textContent=(index+1)+' / '+frames.length;
document.getElementById('details').textContent='Frame '+f.frame_id+' · sim t='+Number(f.timestamp_s).toFixed(2)+' s · '+f.width+'×'+f.height+' · '+f.pose_source;}
function stop(){if(timer){clearInterval(timer);timer=null;document.getElementById('play').textContent='Play';}}
document.getElementById('play').onclick=()=>{if(timer){stop();return;}document.getElementById('play').textContent='Pause';timer=setInterval(()=>{if(index+1>=frames.length){stop();return;}show(index+1);},100);};
document.getElementById('prev').onclick=()=>{stop();show(index-1);};document.getElementById('next').onclick=()=>{stop();show(index+1);};
slider.oninput=()=>{stop();show(Number(slider.value));};img.onerror=()=>{document.getElementById('details').textContent='Frame image failed to load.';};
show(0);</script></body></html>""".replace("__FRAMES__", payload)
    target = output / "viewer" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = _atomic_bytes(target, markup.encode("utf-8"))
    if "__FRAMES__" in markup or not markup.lower().startswith("<!doctype html>"):
        raise RuntimeError("camera preview HTML validation failed")
    return digest


def _artifact_hashes(root: Path, *, excluded_manifest: Optional[Path] = None) -> Dict[str, str]:
    rows: Dict[str, str] = {}
    excluded = Path(excluded_manifest).resolve() if excluded_manifest is not None else None
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if (path.is_file() and path.resolve() != excluded
                and path.name not in {"run_manifest.json", "artifacts_sha256.json"}
                and not rel.startswith("operational/")):
            rows[rel] = _sha256(path)
    return rows


def run_preview(config_path: Path, output_root: Path, timeout_s: float,
                on_keyframe: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
                allow_existing_pilot_root: bool = False,
                publish_journal: Optional[Path] = None,
                bootstrap_id: Optional[str] = None,
                manifest_out: Optional[Path] = None,
                startup_dry_run: bool = False) -> Dict[str, Any]:
    startup = validate_route_startup(config_path, output_root, timeout_s,
                                     allow_existing_pilot_root=allow_existing_pilot_root)
    manifest_path = resolve_camera_producer_manifest_path(
        output_root, manifest_out, allow_existing_pilot_root=allow_existing_pilot_root)
    if allow_existing_pilot_root:
        if not bootstrap_id:
            raise StartupContractError("--allow-existing-pilot-root requires --bootstrap-id")
        validate_bootstrap_before_capture(output_root, expected_bootstrap_id=bootstrap_id,
                                          expected_route_config_sha256=startup["config_sha256"])
    import habitat_sim
    import magnum as mn
    import quaternion
    from PIL import Image
    from habitat_sim.utils.common import quat_from_angle_axis

    config = startup["config"]
    policy = config["policy"]
    vertical_navmesh_tolerance_m = resolve_vertical_navmesh_tolerance(policy)
    if not allow_existing_pilot_root:
        output_root.mkdir(parents=True, exist_ok=False)
    journal = MemoryEventJournal() if startup_dry_run else EventJournal(output_root / "events.jsonl")
    started_mono = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    manifest: Dict[str, Any] = {
        "schema": "smooth-route-camera-preview-run/v1", "run_id": config["run_id"],
        "status": "started", "execution_mode": "camera_only_smooth_route_preview",
        "started_utc": started_utc, "scene_id": config["scene_id"],
        "sequence_id": config["sequence_id"], "dataset_id": config["dataset_id"],
        "route_id": config["route_id"], "selected_route_indices": config["selected_route_indices"],
        "source_route_path": config["source_route_path"],
        "source_route_sha256": config["source_route_sha256"],
        "effective_config_sha256": _sha256(config_path), "policy_sha256": config["policy_sha256"],
        "scene_asset": config["scene_asset"], "scene_asset_sha256": config["scene_asset_sha256"],
        "navmesh_file": config["navmesh_file"], "navmesh_sha256": config["navmesh_sha256"],
        "scene_dataset_config": config.get("scene_dataset_config"),
        "scene_dataset_config_sha256": config.get("scene_dataset_config_sha256"),
        "capture": {"resolution_px": config["resolution_px"],
                    "horizontal_fov_deg": config["horizontal_fov_deg"],
                    "intrinsics_adapter": "adapters.habitat.calibration.habitat_intrinsics_values",
                    "depth_units": "m", "depth_representation": "axial",
                    "sensor_position_habitat": config["sensor_position_habitat"],
                    "sensor_orientation_habitat_rad": config["sensor_orientation_habitat_rad"]},
        "scope": {"status": "not_evaluated_in_T1", "membership": "unavailable",
                  "room_semantics": "unavailable"},
        "stages": {"route_preflight": {"status": "started"}, "camera_capture": {"status": "pending"},
                   "keyframe_persistence": {"status": "pending"}, "viewer_build_validate": {"status": "pending"}},
        "predictor": {"detector_ran": False, "encoder_ran": False, "mapper_ran": False,
                      "room_backend_ran": False, "qwen_ran": False,
                      "predictor_inputs": []},
        "code_provenance": {"worker_sha256": _sha256(Path(__file__).resolve()),
                            "smooth_route_module_sha256": _sha256(ROOT / "integration_v2/smooth_route.py"),
                            "python": sys.version.split()[0], "platform": platform.platform(),
                            "habitat_sim": str(getattr(habitat_sim, "__version__", "unknown")),
                            "numpy": np.__version__},
    }
    if not startup_dry_run:
        _atomic_json(manifest_path, manifest)
    journal.emit({"event": "run_started", "run_id": config["run_id"],
                  "effective_config_sha256": manifest["effective_config_sha256"]})

    sim = None
    writer: Optional[BoundedKeyframeWriter] = None
    first_error: Optional[BaseException] = None
    try:
        width, height = (int(x) for x in config["resolution_px"])
        if width != 640 or height != 480:
            raise ValueError("T1 pilot is locked to native 640x480 capture")
        if timeout_s <= 0 or not math.isfinite(timeout_s):
            raise ValueError("timeout must be positive and finite")
        scene = Path(config["scene_asset"])
        navmesh = Path(config["navmesh_file"])
        dataset_config = Path(config["scene_dataset_config"]) if config.get("scene_dataset_config") else None
        for label, path, expected in (("scene", scene, config["scene_asset_sha256"]),
                                      ("navmesh", navmesh, config["navmesh_sha256"])):
            if not path.is_file() or _sha256(path) != expected:
                raise ValueError(f"{label} asset missing or changed since resolve: {path}")
        if dataset_config is not None and (not dataset_config.is_file()
                or _sha256(dataset_config) != config.get("scene_dataset_config_sha256")):
            raise ValueError(f"scene dataset config missing or changed: {dataset_config}")

        route = config.get("route_points")
        if not isinstance(route, list) or len(route) < 2:
            raise ValueError("selected route slice must have at least two waypoints")
        points = [tuple(float(v) for v in row["position_habitat"]) for row in route]
        if any(len(p) != 3 for p in points):
            raise ValueError("route waypoint position must be a 3-vector")
        dt_s = float(policy["simulator_step_s"])
        pacing_cfg = policy.get("camera_pacing")
        pacer = (SimulationClockPacer(float(pacing_cfg["real_time_factor"]))
                 if isinstance(pacing_cfg, dict) else None)
        linear_speed = float(policy["linear_speed_mps"])
        angular_speed = float(policy["angular_speed_deg_per_s"])
        action_move_m, action_turn_deg = motion_increment_for_step(linear_speed, angular_speed, dt_s)
        if min(dt_s, linear_speed, angular_speed, action_move_m, action_turn_deg) <= 0:
            raise ValueError("motion policy values must be positive")

        sim_cfg = habitat_sim.SimulatorConfiguration()
        sim_cfg.scene_id = str(scene)
        sim_cfg.create_renderer = True
        sim_cfg.requires_textures = True
        sim_cfg.gpu_device_id = -1
        sim_cfg.enable_physics = False
        sim_cfg.allow_sliding = False
        sim_cfg.random_seed = int(config["seed"])
        if dataset_config is not None:
            sim_cfg.scene_dataset_config_file = str(dataset_config)

        agent_cfg = habitat_sim.AgentConfiguration()
        geometry_contract = policy.get("geometry_contract")
        runtime_agent_profile = (
            geometry_contract.get("runtime_agent_profile")
            if isinstance(geometry_contract, dict)
            else policy.get("agent_footprint")
        )
        if not isinstance(runtime_agent_profile, dict):
            raise RouteExecutionError("policy is missing runtime agent profile")
        agent_cfg.radius = float(runtime_agent_profile["radius_m"])
        agent_cfg.height = float(runtime_agent_profile["height_m"])
        agent_cfg.action_space = {
            "move_forward": habitat_sim.ActionSpec("move_forward", habitat_sim.ActuationSpec(amount=action_move_m)),
            "turn_left": habitat_sim.ActionSpec("turn_left", habitat_sim.ActuationSpec(amount=action_turn_deg)),
            "turn_right": habitat_sim.ActionSpec("turn_right", habitat_sim.ActuationSpec(amount=action_turn_deg)),
        }
        sensor_position = mn.Vector3(*[float(v) for v in config["sensor_position_habitat"]])
        sensor_orientation = mn.Vector3(*[float(v) for v in config["sensor_orientation_habitat_rad"]])
        sensors = []
        for sensor_type, uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                                  (habitat_sim.SensorType.DEPTH, "depth_sensor")):
            spec = habitat_sim.CameraSensorSpec()
            spec.uuid = uuid
            spec.sensor_type = sensor_type
            spec.resolution = mn.Vector2i(height, width)
            spec.position = sensor_position
            spec.orientation = sensor_orientation
            spec.hfov = mn.Deg(float(config["horizontal_fov_deg"]))
            spec.near = float(config["min_depth_m"])
            spec.far = float(config["max_depth_m"])
            if sensor_type == habitat_sim.SensorType.DEPTH and hasattr(spec, "normalize_depth"):
                spec.normalize_depth = False
            sensors.append(spec)
        agent_cfg.sensor_specifications = sensors

        stage_start = time.monotonic()
        sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
        if not sim.pathfinder.load_nav_mesh(str(navmesh)):
            raise RouteExecutionError(f"Habitat failed to load navmesh: {navmesh}")
        if not sim.pathfinder.is_loaded:
            raise RouteExecutionError("Habitat PathFinder reports no loaded navmesh")
        nav_settings = sim.pathfinder.nav_mesh_settings
        if nav_settings is None:
            raise RouteExecutionError("loaded navmesh does not expose its serialized footprint settings")
        nav_agent_radius = float(nav_settings.agent_radius)
        nav_agent_height = float(nav_settings.agent_height)
        requested_radius = float(runtime_agent_profile["radius_m"])
        requested_height = float(runtime_agent_profile["height_m"])
        if isinstance(geometry_contract, dict):
            profile_compatibility = validate_navmesh_profile_compatibility(
                nav_settings, geometry_contract=geometry_contract)
        else:
            # Legacy policy: preserve its historical interpretation. New
            # geometry-contract configs use the versioned path below.
            if not math.isclose(requested_radius, nav_agent_radius, rel_tol=0.0, abs_tol=1e-6):
                raise RouteExecutionError(
                    f"agent radius {requested_radius} m differs from serialized navmesh radius {nav_agent_radius} m"
                )
            if not math.isclose(requested_height, nav_agent_height, rel_tol=0.0, abs_tol=1e-6):
                raise RouteExecutionError(
                    f"agent height {requested_height} m differs from serialized navmesh height {nav_agent_height} m"
                )
            profile_compatibility = {
                "status": "compatible_legacy_exact_profile_match",
                "navmesh_baked_agent_radius_m": nav_agent_radius,
                "navmesh_baked_agent_height_m": nav_agent_height,
            }
        resolved_route = find_waypoint_polyline(
            sim.pathfinder, points,
            shortest_path_factory=habitat_sim.ShortestPath,
            vector_factory=mn.Vector3,
            endpoint_tolerance_m=float(policy["route_follow"]["endpoint_tolerance_m"]),
        )
        nav_path = resolved_route["points"]
        waypoint_segments = resolved_route["segments"]
        boundary_query_radius = 1.0
        first_boundary_query_value = float(sim.pathfinder.distance_to_closest_obstacle(
            mn.Vector3(*nav_path[0]), max_search_radius=boundary_query_radius))
        boundary_query_value_status = (
            "unavailable_nonfinite_or_negative"
            if not math.isfinite(first_boundary_query_value) or first_boundary_query_value < 0.0
            else "query_capped_lower_bound"
            if math.isclose(first_boundary_query_value, boundary_query_radius, rel_tol=1e-9, abs_tol=1e-12)
            else "measured_navmesh_boundary_distance"
        )
        if isinstance(geometry_contract, dict):
            clearance_report = validate_navmesh_polyline_v2(
                sim.pathfinder, nav_path,
                max_step_m=float(policy["route_follow"]["path_sample_step_m"]),
                geometry_contract=geometry_contract,
                navmesh_settings=nav_settings,
                endpoint_tolerance_m=float(policy["route_follow"]["endpoint_tolerance_m"]),
                vertical_navmesh_tolerance_m=vertical_navmesh_tolerance_m,
            )
        else:
            clearance_report = validate_navmesh_polyline(
                sim.pathfinder, nav_path,
                max_step_m=float(policy["route_follow"]["path_sample_step_m"]),
                footprint_radius_m=requested_radius,
                clearance_margin_m=float(policy["agent_footprint"]["clearance_margin_m"]),
                endpoint_tolerance_m=float(policy["route_follow"]["endpoint_tolerance_m"]),
                navmesh_agent_radius_m=nav_agent_radius,
                navmesh_agent_height_m=nav_agent_height,
                required_agent_height_m=requested_height,
            )
        if distance3(nav_path[0], points[0]) > float(policy["route_follow"]["endpoint_tolerance_m"]):
            raise RouteExecutionError("navmesh path start differs from locked route start; no snapping allowed")
        if distance3(nav_path[-1], points[-1]) > float(policy["route_follow"]["endpoint_tolerance_m"]):
            raise RouteExecutionError("navmesh path end differs from locked route end; no snapping allowed")
        clearance_artifacts = build_clearance_preflight_artifacts(clearance_report)
        manifest["stages"]["route_preflight"] = {
            "status": "pass", "elapsed_s": round(time.monotonic() - stage_start, 6),
            "simulator_loaded_once": True, "path_source": "Habitat PathFinder.find_path ShortestPath",
            "route_waypoints": route, "pathfinder_polyline": [list(p) for p in nav_path],
            "waypoint_segments": waypoint_segments,
            "geodesic_distance_m": float(resolved_route["geodesic_distance_m"]),
            **clearance_artifacts["manifest"],
            "serialized_navmesh_settings": {
                "agent_radius_m": nav_agent_radius, "agent_height_m": nav_agent_height,
                "agent_max_climb_m": float(nav_settings.agent_max_climb),
                "agent_max_slope_deg": float(nav_settings.agent_max_slope),
                "cell_size_m": float(nav_settings.cell_size), "cell_height_m": float(nav_settings.cell_height),
                "include_static_objects": bool(nav_settings.include_static_objects),
                "source": "PathFinder.nav_mesh_settings loaded from the selected navmesh binary",
            },
            "optional_navmesh_boundary_distance_query": {
                "status": boundary_query_value_status,
                "first_path_point_query_value_m": (first_boundary_query_value
                    if math.isfinite(first_boundary_query_value) and first_boundary_query_value >= 0.0 else None),
                "query_max_search_radius_m": boundary_query_radius,
                "value_semantics": "distance to the radius-eroded navmesh boundary only; not physical scene-object clearance",
                "query_capped_values_are_lower_bounds": True,
            },
            "collision_policy": "no sliding; Simulator.step uses Simulator.step_filter -> PathFinder.try_step_no_sliding; action blocked/clipped/collision fails run; never snap",
            "collision_assurance_scope": {
                "physics_enabled": False,
                "navmesh_include_static_objects": bool(nav_settings.include_static_objects),
                "assurance": "loaded navmesh path/footprint plus action-level movement filter; not independent physics/static-object collision coverage",
            },
            "runtime_agent_profile": dict(runtime_agent_profile),
            "navmesh_profile_compatibility": profile_compatibility,
            "geometry_contract": (dict(geometry_contract) if isinstance(geometry_contract, dict)
                                  else {"status": "legacy_policy", "semantics": "historical"}),
        }
        if not startup_dry_run:
            _atomic_json(manifest_path, manifest)
        journal.emit({"event": "route_preflight_passed", "path_samples": clearance_report["sample_count"],
                      "waypoint_segment_count": len(waypoint_segments),
                      "geodesic_distance_m": float(resolved_route["geodesic_distance_m"]),
                      **clearance_artifacts["journal"]})
        print("PROGRESS: route preflight passed; starting camera-only motion", flush=True)

        agent = sim.initialize_agent(0)
        start_pose = route[0]
        initial_is_navigable = (
            sim.pathfinder.is_navigable(points[0], max_y_delta=vertical_navmesh_tolerance_m)
            if isinstance(geometry_contract, dict)
            else sim.pathfinder.is_navigable(points[0])
        )
        if not initial_is_navigable:
            raise RouteExecutionError("locked initial spawn is not navigable")
        initial_state = agent.get_state()
        initial_state.position = np.asarray(points[0], dtype=np.float32)
        initial_state.rotation = quat_from_angle_axis(float(start_pose["yaw_rad"]), np.array([0.0, 1.0, 0.0], dtype=np.float32))
        agent.set_state(initial_state, infer_sensor_states=True)  # one-time initial spawn only
        state = agent.get_state()
        if distance3(_position(state), points[0]) > float(policy["route_follow"]["endpoint_tolerance_m"]):
            raise RouteExecutionError("Habitat changed the selected initial spawn; refusing pose correction")
        if not sim.pathfinder.is_navigable(state.position):
            raise RouteExecutionError("actual initial spawn is outside loaded navmesh")

        raw_sample_path = sample_polyline(nav_path, float(policy["route_follow"]["path_sample_step_m"]))
        # Keep every sample in the validated path. This is only a route-following
        # target list; agent motion still comes from Habitat actions.
        targets = raw_sample_path[1:]
        if not targets:
            raise RouteExecutionError("selected route produced no movement targets")
        budget_points = [{"position_habitat": list(point),
                          "yaw_rad": float(route[0]["yaw_rad"] if index == 0 else route[-1]["yaw_rad"])}
                         for index, point in enumerate(nav_path)]
        route_budget = derive_route_budget(budget_points,
            simulation_dt_s=dt_s, linear_speed_mps=linear_speed,
            angular_speed_deg_per_s=angular_speed,
            path_sample_step_m=float(policy["route_follow"]["path_sample_step_m"]),
            keyframe_min_translation_m=float(policy["keyframes"]["min_translation_m"]),
            keyframe_min_yaw_deg=float(policy["keyframes"]["min_yaw_deg"]),
            max_camera_frames=int(policy["route_follow"]["max_camera_frames"]))
        manifest["stages"]["route_preflight"]["motion_budget"] = route_budget
        if not startup_dry_run:
            _atomic_json(manifest_path, manifest)
        journal.emit({"event": "route_motion_budget_locked", **route_budget})

        startup_event = None
        if allow_existing_pilot_root:
            startup_event = {
                "event": "camera_startup_validated",
                "event_owner": "habitat_route_runner",
                "pilot_run_id": output_root.name,
                "route_run_id": str(config["run_id"]),
                "sequence_id": str(config["sequence_id"]),
                "bootstrap_id": bootstrap_id,
                "effective_route_config_sha256": startup["config_sha256"],
                "event_monotonic_s": time.monotonic(),
                "clock_domain": _host_monotonic_domain(),
                "route_preflight_status": "pass",
                "navmesh_path_validated": True,
                "capture_boundary": "before_sim.get_sensor_observations",
            }
            journal.emit(startup_event)

        if startup_dry_run:
            # Recheck the production bootstrap after Habitat has loaded the
            # scene/navmesh and validated route/clearance, still before RGB-D.
            boundary = validate_bootstrap_before_capture(
                output_root, expected_bootstrap_id=str(bootstrap_id),
                expected_route_config_sha256=startup["config_sha256"])
            result = {
                "status": "startup_dry_run_route_preflight_passed",
                "pilot_run_id": output_root.name,
                "route_run_id": str(config["run_id"]),
                "sequence_id": str(config["sequence_id"]),
                "effective_route_config_sha256": startup["config_sha256"],
                "capture_boundary_reached": True,
                "capture_boundary": "immediately_before_sim.get_sensor_observations",
                "camera_started": False,
                "camera_frames": 0,
                "model_loads": 0,
                "inference_calls": 0,
                "mapper_updates": 0,
                "habitat_simulator_initialized": True,
                "route_preflight": manifest["stages"]["route_preflight"],
                "startup_event": startup_event,
                "bootstrap_boundary": boundary,
                "not_checked": ["RGB-D capture", "sensor observation values", "model inference",
                                "mapper mutation", "camera/mapping overlap"],
            }
            print("PROGRESS: route startup validated; stopped before first sensor observation", flush=True)
            return result

        frames_dir = output_root / "viewer" / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        keyframe_policy = KeyframePolicyV1(
            min_translation_m=float(policy["keyframes"]["min_translation_m"]),
            min_yaw_deg=float(policy["keyframes"]["min_yaw_deg"]),
            min_interval_s=float(policy["keyframes"]["min_interval_s"]),
            first_frame=bool(policy["keyframes"]["include_first_valid_frame"]),
        )
        keyframe_results: List[Dict[str, Any]] = []

        def write_packet(packet: Dict[str, Any]) -> Dict[str, Any]:
            result = _write_keyframe(packet, output_root)
            # The callback is invoked only after all immutable RGB-D files and
            # metadata have been atomically written.  It is intentionally a
            # narrow file-backed publication hook: camera capture remains the
            # source of truth and the callback cannot mutate simulator state.
            if on_keyframe is not None:
                on_keyframe(packet, result)
            if publish_journal is not None:
                # File-backed IPC publication happens only after the immutable
                # keyframe files have been written.  The payload contains no
                # arrays, so readers can safely load the referenced files.
                publish_monotonic = time.monotonic()
                published_wall = time.time()
                payload = {"schema": "camera-live-keyframe-event/v1",
                           "packet": {"frame_id": str(packet["frame_id"]),
                                      "metadata": dict(packet["metadata"])},
                           "result": dict(result), "published_wall_time_s": published_wall,
                           "published_wall_time_unix_s": published_wall,
                           "published_monotonic_s": publish_monotonic,
                           "published_monotonic_domain": _host_monotonic_domain()}
                record = {"schema": "camera-live-journal/v1", "event": "keyframe_persisted",
                          "frame_id": str(packet["frame_id"]), "payload": payload}
                publish_journal.parent.mkdir(parents=True, exist_ok=True)
                with publish_journal.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
                    stream.flush(); os.fsync(stream.fileno())
            keyframe_results.append(result)
            return result

        writer = BoundedKeyframeWriter(write_packet, journal.emit)
        frame_index = 0
        camera_rows: List[Dict[str, Any]] = []
        candidate_count = 0
        action_count = 0
        collision_check_count = 0
        capture_started = time.monotonic()
        capture_started_wall = time.time()
        capture_clock_domain = _host_monotonic_domain()
        pacing_events: List[Dict[str, Any]] = []
        last_progress = capture_started
        target_index = 0
        canonical_h = canonical_from_habitat_matrix()
        K = habitat_intrinsics_values(width, height, float(config["horizontal_fov_deg"]))
        max_simulation_elapsed_s = float(policy["route_follow"]["max_sim_time_s"])
        wall_deadline = time.monotonic() + timeout_s

        def capture_observation(observations: Dict[str, Any], action: str, collision: bool) -> None:
            nonlocal frame_index, candidate_count
            capture_monotonic = time.monotonic()
            capture_wall = time.time()
            if "color_sensor" not in observations or "depth_sensor" not in observations:
                raise RuntimeError("Habitat observation is missing configured color/depth sensor output")
            rgb_raw = np.asarray(observations["color_sensor"])
            if rgb_raw.ndim != 3 or rgb_raw.shape[:2] != (height, width) or rgb_raw.shape[2] not in (3, 4):
                raise RuntimeError(f"unexpected Habitat RGB shape {rgb_raw.shape}; expected {(height, width, 3)}")
            rgb = np.ascontiguousarray(rgb_raw[..., :3].astype(np.uint8, copy=False))
            depth = np.asarray(observations["depth_sensor"], dtype=np.float32).copy()
            if depth.shape != (height, width):
                raise RuntimeError(f"unexpected Habitat depth shape {depth.shape}; expected {(height, width)}")
            valid = np.isfinite(depth) & (depth >= float(config["min_depth_m"])) & (depth <= float(config["max_depth_m"]))
            depth[~valid] = np.nan
            stamp = float(sim.get_world_time())
            previous_stamp = float(camera_rows[-1]["timestamp_s"]) if camera_rows else None
            cumulative_deviation_before = (float(camera_rows[-1].get("cumulative_simulation_dt_deviation_s") or 0.0)
                                           if camera_rows else 0.0)
            step_clock = simulation_step_record(previous_stamp, stamp, dt_s,
                cumulative_deviation_before_s=cumulative_deviation_before)
            observed_sim_dt = float(step_clock["observed_simulation_dt_s"])
            current = agent.get_state()
            base_rot = _rotation_matrix(current.rotation, quaternion)
            base_position = _position(current)
            sensor_state = current.sensor_states["color_sensor"]
            sensor_rot = _rotation_matrix(sensor_state.rotation, quaternion)
            sensor_position_actual = _position(sensor_state)
            current_yaw = _yaw(base_rot)
            pose_speed = None
            if camera_rows:
                previous_pose = camera_rows[-1]["base_pose_habitat"]
                previous_position = [float(previous_pose[i][3]) for i in range(3)]
                pose_speed = actual_pose_speeds(previous_position, base_position,
                    float(camera_rows[-1]["base_yaw_rad"]), current_yaw, observed_sim_dt)
            simulation_origin = float(camera_rows[0]["timestamp_s"]) if camera_rows else stamp
            elapsed_simulation = stamp - simulation_origin
            validate_simulation_elapsed(elapsed_simulation, max_simulation_elapsed_s)
            frame_id = f"{config['sequence_id']}-cam-{frame_index:06d}"
            image_path = frames_dir / f"frame_{frame_index:06d}.jpg"
            image = Image.fromarray(rgb, mode="RGB")
            from io import BytesIO
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=int(policy["preview"]["jpeg_quality"]), optimize=False)
            image_sha = _atomic_bytes(image_path, buffer.getvalue())
            frame_row = {"frame_id": frame_id, "frame_index": frame_index, "timestamp_s": stamp,
                         "timestamp_clock_domain": "habitat_simulation",
                         "simulation_dt_from_previous_s": observed_sim_dt,
                         **step_clock,
                         "simulation_elapsed_from_first_frame_s": elapsed_simulation,
                         "requested_elapsed_from_actions_s": action_count * dt_s,
                         "cumulative_deviation_recomputed_s": elapsed_simulation - action_count * dt_s,
                         "capture_wall_time_unix_s": capture_wall,
                         "capture_monotonic_s": capture_monotonic,
                         "capture_monotonic_domain": capture_clock_domain,
                         "path": str(image_path.relative_to(output_root / "viewer")),
                         "sha256": image_sha, "width": width, "height": height,
                         "pose_source": "actual Habitat agent/sensor state after simulator step",
                         "action": action, "collision": bool(collision),
                         "base_yaw_rad": current_yaw,
                         "actual_pose_speed": pose_speed,
                         "base_pose_habitat": pose_matrix(base_position, base_rot).tolist(),
                         "sensor_pose_habitat": pose_matrix(sensor_position_actual, sensor_rot).tolist()}
            camera_rows.append(frame_row)
            journal.emit({"event": "camera_frame", "event_owner": "habitat_route_runner",
                          "frame_id": frame_id, "frame_index": frame_index,
                          "timestamp_s": stamp, "timestamp_clock_domain": "habitat_simulation",
                          "capture_wall_time_unix_s": capture_wall,
                          "capture_monotonic_s": capture_monotonic,
                          "capture_monotonic_domain": capture_clock_domain,
                          "action": action, "collision": bool(collision),
                          "rgb_path": frame_row["path"], "rgb_sha256": image_sha,
                          "base_position_habitat": list(base_position),
                          "sensor_position_habitat": list(sensor_position_actual)})
            yaw_now = _yaw(base_rot)
            decision = keyframe_policy.evaluate(frame_id, base_position, yaw_now, stamp)
            journal.emit({"event": "keyframe_decision", "frame_id": frame_id,
                          "candidate": decision.candidate, "reason": decision.reason,
                          "translation_from_reference_m": decision.translation_from_reference_m,
                          "yaw_from_reference_deg": decision.yaw_from_reference_deg,
                          "elapsed_from_reference_s": decision.elapsed_from_reference_s,
                          "reference_frame_id": decision.reference_frame_id,
                          "policy_id": policy["keyframes"]["policy"]})
            if decision.candidate:
                candidate_count += 1
                journal.emit({"event": "keyframe_candidate", "frame_id": frame_id,
                              "candidate_index": candidate_count, "mapper_processed": False})
                sensor_pose_h = pose_matrix(sensor_position_actual, sensor_rot)
                sensor_pose_c = canonical_h @ sensor_pose_h
                t_c_base = canonical_h @ pose_matrix(base_position, base_rot)
                metadata = {
                    "schema": "smooth-route-raw-keyframe/v1", "frame_id": frame_id,
                    "sequence_id": config["sequence_id"], "scene_id": config["scene_id"],
                    "dataset_id": config["dataset_id"], "route_id": config["route_id"],
                    "frame_index": frame_index, "timestamp_s": stamp,
                    "timestamp_clock_domain": "habitat_simulation",
                    "capture_wall_time_unix_s": capture_wall,
                    "capture_wall_time_clock_domain": "unix_epoch_utc",
                    "capture_monotonic_s": capture_monotonic,
                    "capture_monotonic_domain": capture_clock_domain,
                    "resolution_px": [width, height], "width": width, "height": height,
                    "horizontal_fov_deg": float(config["horizontal_fov_deg"]),
                    "calibration_id": config["calibration_id"], "intrinsics": K,
                    "depth_units": "m", "depth_representation": "axial",
                    "sensor_contract_sha256": config["sensor_contract_sha256"],
                    "base_pose_habitat": pose_matrix(base_position, base_rot).tolist(),
                    "sensor_pose_habitat": sensor_pose_h.tolist(),
                    "sensor_pose_canonical": sensor_pose_c.tolist(),
                    "base_pose_canonical": t_c_base.tolist(),
                    "world_frame_habitat": "Y-up, metres; camera +X right,+Y up,-Z forward",
                    "world_frame_canonical": "Z-up; C.x=H.x,C.y=-H.z,C.z=H.y",
                    "camera_optical": "+X right,+Y down,+Z forward",
                    "pose_source": "Habitat agent/sensor state; actual post-action pose",
                    "keyframe_candidate_reason": decision.reason,
                    "predictor_input": False, "mapper_processed": False,
                }
                writer.submit({"frame_id": frame_id, "rgb": rgb.copy(), "depth_m": depth.copy(),
                               "valid_mask": valid.copy(), "metadata": metadata})
                keyframe_policy.mark_queue_accepted(frame_id, base_position, yaw_now, stamp)
            frame_index += 1

        initial_obs = sim.get_sensor_observations()
        capture_observation(initial_obs, "initial_spawn_observation", False)
        if pacer is not None:
            first_frame = camera_rows[0]
            pacer.start(float(first_frame["timestamp_s"]),
                        float(first_frame["capture_monotonic_s"]))

        def pace_after_observation(*, continuing: bool) -> None:
            if pacer is None:
                return
            frame = camera_rows[-1]
            record = pacer.pace_after_frame(float(sim.get_world_time()), continuing=continuing)
            record.update({"frame_id": frame["frame_id"],
                           "frame_index": frame["frame_index"],
                           "real_time_factor_target": pacer.real_time_factor,
                           "clock_domain": capture_clock_domain})
            pacing_events.append(record)
            journal.emit({"event": "camera_pacing", **record})
        movement_records: List[Dict[str, Any]] = []
        while target_index < len(targets):
            if time.monotonic() > wall_deadline:
                raise TimeoutError(f"camera-only run exceeded wall timeout {timeout_s}s")
            if frame_index >= int(policy["route_follow"]["max_camera_frames"]):
                raise TimeoutError("camera frame guard reached before route completion")
            state = agent.get_state()
            pos = _position(state)
            target = targets[target_index]
            if _xz_distance(pos, target) <= float(policy["route_follow"]["position_tolerance_m"]):
                target_index += 1
                continue
            target_yaw = yaw_toward_habitat_xz(pos, target)
            current_yaw = _yaw(_rotation_matrix(state.rotation, quaternion))
            delta = yaw_delta_rad(current_yaw, target_yaw)
            turn_limit = math.radians(action_turn_deg)
            if abs(delta) > math.radians(float(policy["route_follow"]["yaw_tolerance_deg"])):
                action = "turn_left" if delta > 0.0 else "turn_right"
            else:
                action = "move_forward"
                rotation = _rotation_matrix(state.rotation, quaternion)
                forward = rotation @ np.array([0.0, 0.0, -1.0], dtype=np.float64)
                expected = np.asarray(pos, dtype=np.float64) + forward * action_move_m
                expected[1] = pos[1]
                clipped_raw = sim.pathfinder.try_step_no_sliding(mn.Vector3(*pos), mn.Vector3(*expected))
                clipped = np.asarray(tuple(float(v) for v in clipped_raw), dtype=np.float64)
                collision_check_count += 1
                if _xz_distance(clipped, expected) > float(policy["route_follow"]["endpoint_tolerance_m"]):
                    raise RouteExecutionError("per-step try_step_no_sliding clipped the motion; stopping without correction")
                if not sim.pathfinder.is_navigable(
                        mn.Vector3(*clipped), max_y_delta=vertical_navmesh_tolerance_m):
                    raise RouteExecutionError("per-step motion endpoint is not navigable")
            observations = sim.step(action, dt=dt_s)
            action_count += 1
            collided = bool(observations.get("collided", False)) if isinstance(observations, dict) else False
            if collided:
                raise RouteExecutionError(f"Habitat reported collision during action {action}")
            after = agent.get_state()
            after_pos = _position(after)
            if not sim.pathfinder.is_navigable(
                    after.position, max_y_delta=vertical_navmesh_tolerance_m):
                raise RouteExecutionError("post-action agent pose is outside the locked navmesh")
            if action == "move_forward":
                moved = _xz_distance(pos, after_pos)
                if moved < action_move_m * 0.5 or moved > action_move_m * 1.5:
                    raise RouteExecutionError(f"Habitat action displacement {moved:.4f} m differs from configured {action_move_m:.4f} m; no snap/retry")
                corridor = _polyline_distance_xz(after_pos, nav_path)
                if corridor > 0.35:
                    raise RouteExecutionError(f"actual pose left selected navmesh route corridor by {corridor:.3f} m")
            stamp = float(sim.get_world_time())
            validate_simulation_elapsed(stamp - float(camera_rows[0]["timestamp_s"]),
                                         max_simulation_elapsed_s)
            capture_observation(observations, action, collided)
            journal.emit({"event": "motion_step_checked", "action_index": action_count,
                          "action": action, "target_index": target_index,
                          "position_before_habitat": list(pos), "position_after_habitat": list(after_pos),
                          "post_step_navigable": True,
                          "pre_step_no_sliding_clipped": False if action == "move_forward" else None,
                          "collision_reported": collided,
                          "footprint_basis": "serialized_navmesh_agent_radius_height"})
            movement_records.append({"action_index": action_count, "action": action,
                                     "target_index": target_index, "target_habitat": list(target),
                                     "position_before": list(pos), "position_after": list(after_pos),
                                     "sim_time_s": stamp})
            final_position = tuple(float(v) for v in route[-1]["position_habitat"])
            final_yaw = wrap_angle_rad(float(route[-1]["yaw_rad"]))
            endpoint_reached = _xz_distance(after_pos, final_position) <= float(
                policy["route_follow"]["position_tolerance_m"])
            yaw_reached = abs(yaw_delta_rad(
                _yaw(_rotation_matrix(after.rotation, quaternion)), final_yaw)) <= math.radians(
                    float(policy["route_follow"]["yaw_tolerance_deg"]))
            pace_after_observation(continuing=not (endpoint_reached and yaw_reached))
            now = time.monotonic()
            if now - last_progress >= 2.0:
                print(f"PROGRESS: camera_frames={frame_index} target={target_index}/{len(targets)} "
                      f"sim_s={stamp:.1f} candidates={candidate_count}", flush=True)
                last_progress = now
        final_target = tuple(float(v) for v in route[-1]["position_habitat"])
        final_state = agent.get_state()
        if distance3(_position(final_state), final_target) > float(policy["route_follow"]["endpoint_tolerance_m"]):
            raise RouteExecutionError("final actual pose did not reach the selected route endpoint within tolerance")

        # Reach the final route yaw with the same bounded turn action, never by
        # assigning a pose. This is a final camera-motion stage, still T1 only.
        final_yaw_goal = wrap_angle_rad(float(route[-1]["yaw_rad"]))
        while abs(yaw_delta_rad(_yaw(_rotation_matrix(agent.get_state().rotation, quaternion)), final_yaw_goal)) > math.radians(float(policy["route_follow"]["yaw_tolerance_deg"])):
            if time.monotonic() > wall_deadline or frame_index >= int(policy["route_follow"]["max_camera_frames"]):
                raise TimeoutError("final orientation did not converge within run guard")
            current_yaw = _yaw(_rotation_matrix(agent.get_state().rotation, quaternion))
            delta = yaw_delta_rad(current_yaw, final_yaw_goal)
            action = "turn_left" if delta > 0 else "turn_right"
            observations = sim.step(action, dt=dt_s)
            action_count += 1
            if bool(observations.get("collided", False)):
                raise RouteExecutionError("Habitat reported collision while turning at route endpoint")
            capture_observation(observations, action, False)
            current_after_turn = agent.get_state()
            yaw_after_turn = _yaw(_rotation_matrix(current_after_turn.rotation, quaternion))
            turn_still_needed = abs(yaw_delta_rad(yaw_after_turn, final_yaw_goal)) > math.radians(
                float(policy["route_follow"]["yaw_tolerance_deg"]))
            pace_after_observation(continuing=turn_still_needed)

        capture_finished_monotonic = time.monotonic()
        capture_finished_wall = time.time()
        manifest["stages"]["camera_capture"] = {
            "status": "pass", "elapsed_s": round(capture_finished_monotonic - capture_started, 6),
            "capture_started_wall_time_unix_s": capture_started_wall,
            "capture_started_monotonic_s": capture_started,
            "capture_finished_wall_time_unix_s": capture_finished_wall,
            "capture_finished_monotonic_s": capture_finished_monotonic,
            "capture_monotonic_domain": capture_clock_domain,
            "clock_domains": {"simulation": "habitat_simulation",
                               "wall": "unix_epoch_seconds",
                               "monotonic": capture_clock_domain},
            "world_time_start_s": float(camera_rows[0]["timestamp_s"]),
            "world_time_end_s": float(camera_rows[-1]["timestamp_s"]),
            "simulated_duration_s": float(camera_rows[-1]["timestamp_s"] - camera_rows[0]["timestamp_s"]),
            "requested_action_time_s": action_count * dt_s,
            "cumulative_simulation_dt_deviation_s": float(camera_rows[-1]["timestamp_s"] - camera_rows[0]["timestamp_s"] - action_count * dt_s),
            "camera_frame_count": frame_index,
            "actions": action_count, "move_step_m": action_move_m, "turn_step_deg": action_turn_deg,
            "simulation_step_s_requested": dt_s,
            "motion_timebase": policy["motion_timebase"],
            "nominal_speed_contract": {"linear_speed_mps": linear_speed,
                "angular_speed_deg_per_s": angular_speed,
                "basis": "fixed action increments divided by requested simulator dt; nominal only"},
            "actual_speed_measurement": "actual planar pose delta / observed get_world_time delta; wrapped yaw delta / same observed delta",
            "observed_simulation_dt_deviation_policy": "record finite positive deltas; enforce locked elapsed simulation, wall timeout, camera frame, collision and route guards",
            "motion_budget": route_budget,
            "per_step_collision_clearance_checks": collision_check_count,
            "pacing": {"status": "enabled" if pacer is not None else "not_configured",
                       "mode": pacing_cfg.get("mode") if isinstance(pacing_cfg, dict) else None,
                       "target_camera_fps": pacing_cfg.get("target_camera_fps") if isinstance(pacing_cfg, dict) else None,
                       "real_time_factor_target": pacing_cfg.get("real_time_factor") if isinstance(pacing_cfg, dict) else None,
                       "wait_total_s": pacer.total_wait_s if pacer is not None else 0.0,
                       "overrun_count": pacer.overrun_count if pacer is not None else 0,
                       "max_lag_s": pacer.max_lag_s if pacer is not None else 0.0,
                       "events": pacing_events,
                       "terminal_wait": False},
            "camera_frames": camera_rows, "movement_trace": movement_records,
            "actual_initial_pose_habitat": camera_rows[0]["base_pose_habitat"],
            "actual_final_pose_habitat": pose_matrix(_position(agent.get_state()),
                _rotation_matrix(agent.get_state().rotation, quaternion)).tolist(),
            "motion": "Habitat Agent.act through Simulator.step with incremental move/turn controls; no waypoint teleport after initial spawn",
        }
        manifest["stages"]["route_preflight"]["status"] = "pass"
        manifest["stages"]["keyframe_persistence"] = {"status": "draining", "candidate_count": candidate_count}
        if not startup_dry_run:
            _atomic_json(manifest_path, manifest)
        drain_started = time.monotonic()
        writer.close(timeout_s=max(5.0, timeout_s))
        drain_elapsed = time.monotonic() - drain_started
        writer = None
        if len(keyframe_results) + sum(1 for line in (output_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
                                       if '"event":"keyframe_superseded"' in line) != candidate_count:
            # Superseded candidates are intentionally not persisted; this check
            # reconciles exactly the candidate/admitted/superseded counts.
            admitted = len(keyframe_results)
            superseded = sum(1 for line in (output_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
                            if '"event":"keyframe_superseded"' in line)
            if admitted + superseded != candidate_count:
                raise RuntimeError("candidate keyframe count does not reconcile to admitted plus superseded")
        manifest["stages"]["keyframe_persistence"] = {
            "status": "pass", "elapsed_drain_s": round(drain_elapsed, 6), "candidate_count": candidate_count,
            "admitted_count": len(keyframe_results), "superseded_count": candidate_count - len(keyframe_results),
            "queue": "one_in_flight_plus_one_latest_pending", "mapper_processed": False,
            "admitted_packets": keyframe_results,
        }
        preview_sha = _build_html(camera_rows, output_root)
        index_doc = {"schema": "smooth-route-camera-preview-index/v1", "frame_count": len(camera_rows),
                     "resolution_px": [width, height], "frames": camera_rows}
        _atomic_json(output_root / "viewer/index.json", index_doc)
        # index is embedded into HTML, but keeping this JSON allows independent
        # review of frame identity/timestamps without parsing markup.
        html_path = output_root / "viewer/index.html"
        manifest["stages"]["viewer_build_validate"] = {
            "status": "pass", "entrypoint": "viewer/index.html",
            "entrypoint_sha256": preview_sha,
            "frame_index": "viewer/index.json", "frame_index_sha256": _sha256(output_root / "viewer/index.json"),
            "all_frame_files_exist": all((output_root / "viewer" / row["path"]).is_file() for row in camera_rows),
            "all_frame_hashes_valid": all(_sha256(output_root / "viewer" / row["path"]) == row["sha256"] for row in camera_rows),
            "offline_no_CDN": True,
        }
        if not camera_rows or not html_path.is_file() or not manifest["stages"]["viewer_build_validate"]["all_frame_hashes_valid"]:
            raise RuntimeError("preview validation failed")
        # Every camera frame and persisted keyframe must have monotonic source
        # timestamps and exactly the resolved native sensor dimensions.
        stamps = [float(row["timestamp_s"]) for row in camera_rows]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise RuntimeError("camera frame timestamps are not strictly increasing")
        capture_mono_stamps = [float(row["capture_monotonic_s"]) for row in camera_rows]
        simulation_stamps = [float(row["timestamp_s"]) for row in camera_rows]
        wall_intervals = [b - a for a, b in zip(capture_mono_stamps, capture_mono_stamps[1:])]
        sim_intervals = [b - a for a, b in zip(simulation_stamps, simulation_stamps[1:])]
        capture_span = ((capture_mono_stamps[-1] - capture_mono_stamps[0])
                        if len(capture_mono_stamps) > 1 else 0.0)
        sim_span = ((simulation_stamps[-1] - simulation_stamps[0])
                    if len(simulation_stamps) > 1 else 0.0)
        observed_steps = camera_rows[1:]
        dt_deviations = [float(row["simulation_dt_deviation_s"]) for row in observed_steps]
        measured_linear_speeds = [float(row["actual_pose_speed"]["actual_planar_speed_mps"])
                                   for row in observed_steps if row.get("actual_pose_speed")]
        measured_angular_speeds = [float(row["actual_pose_speed"]["actual_angular_speed_deg_per_s"])
                                   for row in observed_steps if row.get("actual_pose_speed")]
        def _range_stats(values: List[float]) -> Dict[str, Optional[float]]:
            return {"count": len(values), "mean": float(np.mean(values)) if values else None,
                    "p50": float(np.percentile(values, 50)) if values else None,
                    "p95": float(np.percentile(values, 95)) if values else None,
                    "min": min(values) if values else None, "max": max(values) if values else None}
        interval_summary = {
            "wall_interval_s": {"mean": float(np.mean(wall_intervals)) if wall_intervals else None,
                                "p50": float(np.percentile(wall_intervals, 50)) if wall_intervals else None,
                                "p95": float(np.percentile(wall_intervals, 95)) if wall_intervals else None,
                                "max": max(wall_intervals) if wall_intervals else None},
            "simulation_interval_s": {"mean": float(np.mean(sim_intervals)) if sim_intervals else None,
                                      "p50": float(np.percentile(sim_intervals, 50)) if sim_intervals else None,
                                      "p95": float(np.percentile(sim_intervals, 95)) if sim_intervals else None,
                                      "max": max(sim_intervals) if sim_intervals else None},
            "requested_simulation_dt_s": dt_s,
            "observed_minus_requested_dt_s": _range_stats(dt_deviations),
            "cumulative_simulation_dt_deviation_s": float(camera_rows[-1].get("cumulative_simulation_dt_deviation_s", 0.0)),
            "measured_actual_planar_speed_mps": _range_stats(measured_linear_speeds),
            "measured_actual_angular_speed_deg_per_s": _range_stats(measured_angular_speeds),
            "nominal_linear_speed_mps": linear_speed,
            "nominal_angular_speed_deg_per_s": angular_speed,
            "nominal_speed_basis": "fixed increments / requested simulator dt; not measured speed",
            "capture_wall_span_s": capture_span,
            "simulation_per_wall_factor": sim_span / capture_span if capture_span > 0 else None,
            "actual_camera_fps": (len(camera_rows) - 1) / capture_span if capture_span > 0 else None,
            "target_camera_fps": pacing_cfg.get("target_camera_fps") if isinstance(pacing_cfg, dict) else None,
            "real_time_factor_target": pacing_cfg.get("real_time_factor") if isinstance(pacing_cfg, dict) else None,
            "pacing_wait_total_s": pacer.total_wait_s if pacer is not None else 0.0,
            "pacing_overrun_count": pacer.overrun_count if pacer is not None else 0,
            "pacing_max_lag_s": pacer.max_lag_s if pacer is not None else 0.0,
        }
        manifest["summary"] = {
            "camera_frames": frame_index, "candidate_keyframes": candidate_count,
            "admitted_raw_keyframes": len(keyframe_results),
            "superseded_before_write": candidate_count - len(keyframe_results),
            "world_time_start_s": float(camera_rows[0]["timestamp_s"]) if camera_rows else None,
            "world_time_end_s": float(camera_rows[-1]["timestamp_s"]) if camera_rows else None,
            "simulated_duration_s": (float(camera_rows[-1]["timestamp_s"] - camera_rows[0]["timestamp_s"])
                                      if camera_rows else 0.0),
            "requested_camera_sample_hz": round(1.0 / dt_s, 6),
            "camera_timing": interval_summary,
            "wall_clock_runtime_s": round(time.monotonic() - started_mono, 6),
            "peak_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
            "collision_events": 0, "scope_membership": "unavailable_not_evaluated",
            "room_semantics": "unavailable_not_run",
        }
        manifest["status"] = "completed"
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        journal.emit({"event": "run_ready_to_complete", "camera_frame_count": frame_index,
                      "candidate_keyframes": candidate_count, "admitted_keyframes": len(keyframe_results)})
        manifest["artifacts_sha256"] = _artifact_hashes(output_root, excluded_manifest=manifest_path)
        _atomic_json(output_root / "artifacts_sha256.json", {
            "schema": "smooth-route-artifact-hashes/v1", "files": manifest["artifacts_sha256"]})
        manifest["artifacts_sha256_manifest_sha256"] = _sha256(output_root / "artifacts_sha256.json")
        if not startup_dry_run:
            _atomic_json(manifest_path, manifest)
        print(f"PROGRESS: completed camera-only preview | frames={frame_index} candidates={candidate_count} "
              f"admitted={len(keyframe_results)}", flush=True)
        return manifest
    except BaseException as exc:
        first_error = exc
        if writer is not None:
            try:
                writer.close(timeout_s=max(5.0, timeout_s))
            except BaseException as close_exc:
                manifest["keyframe_writer_close_error"] = f"{type(close_exc).__name__}: {close_exc}"
            writer = None
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        for stage in manifest.get("stages", {}).values():
            if isinstance(stage, dict) and stage.get("status") in {"started", "draining"}:
                stage["status"] = "failed"
                stage["error"] = manifest["error"]
        manifest["traceback"] = traceback.format_exc()
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        if not startup_dry_run:
            manifest["partial_artifact_count"] = sum(1 for p in output_root.rglob("*") if p.is_file())
        if "frame_index" in locals():
            manifest["partial_camera_frame_count"] = int(frame_index)
        if "candidate_count" in locals():
            manifest["partial_candidate_keyframes"] = int(candidate_count)
        if "camera_rows" in locals():
            manifest["partial_camera_frames"] = camera_rows
        if not startup_dry_run:
            _atomic_json(manifest_path, manifest)
        try:
            journal.emit({"event": "run_failed", "error_type": type(exc).__name__, "error": str(exc)})
        except BaseException:
            pass
        if startup_dry_run:
            print(f"STARTUP PREFLIGHT FAILED: {type(exc).__name__}: {exc}; no capture artifacts written",
                  flush=True)
        else:
            print(f"FAILED: {type(exc).__name__}: {exc}; partial output preserved at {output_root}", flush=True)
        return manifest
    finally:
        if writer is not None:
            try:
                writer.close(timeout_s=max(5.0, timeout_s))
            except BaseException as close_exc:
                if first_error is None:
                    manifest["status"] = "failed"
                    manifest["error"] = f"keyframe writer close: {type(close_exc).__name__}: {close_exc}"
                    if not startup_dry_run:
                        _atomic_json(manifest_path, manifest)
        if sim is not None:
            sim.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Camera-only smooth Habitat route preview; no perception or mapping")
    parser.add_argument("--config", type=Path, required=True, help="resolved, hashed effective route config")
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--timeout-s", type=float, default=300.0)
    parser.add_argument("--publish-journal", type=Path, default=None,
                        help="append current keyframe events after atomic persistence")
    parser.add_argument("--manifest-out", type=Path, default=None,
                        help="write producer manifest separately from the owning camera-live pilot manifest")
    parser.add_argument("--allow-existing-pilot-root", action="store_true")
    parser.add_argument("--bootstrap-id", default=None,
                        help="ownership token issued by the camera-live coordinator")
    parser.add_argument("--startup-dry-run", action="store_true",
                        help="validate the real startup contract and stop at the capture boundary; no simulator/capture")
    args = parser.parse_args()
    try:
        if args.startup_dry_run:
            result = run_preview(args.config.resolve(), args.output.resolve(),
                float(args.timeout_s), allow_existing_pilot_root=bool(args.allow_existing_pilot_root),
                publish_journal=args.publish_journal.resolve() if args.publish_journal else None,
                bootstrap_id=args.bootstrap_id,
                manifest_out=args.manifest_out.resolve() if args.manifest_out else None,
                startup_dry_run=True)
            print(json.dumps(result, sort_keys=True, allow_nan=False), flush=True)
            return 0 if result.get("status") == "startup_dry_run_route_preflight_passed" else 1
        result = run_preview(args.config.resolve(), args.output.resolve(), float(args.timeout_s),
                             allow_existing_pilot_root=bool(args.allow_existing_pilot_root),
                             publish_journal=args.publish_journal.resolve() if args.publish_journal else None,
                             bootstrap_id=args.bootstrap_id,
                             manifest_out=args.manifest_out.resolve() if args.manifest_out else None)
    except BaseException as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", flush=True)
        return 1
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
