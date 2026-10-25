"""Shared model-free startup contract for the camera-live pilot.

Both the coordinator and Habitat route runner call these functions.  The
contract validates the same config/route arguments before model loading, and
then proves that an existing output root belongs to this exact run before the
route runner is allowed to create a simulator or capture a frame.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from integration_v2.camera_pacing import derive_route_budget


BOOTSTRAP_SCHEMA = "camera-live-bootstrap/v1"
READY_SCHEMA = "camera-live-worker-ready/v2"
VIEWER_START_SCHEMA = "d1-explicit-viewer-start/v1"
VIEWER_START_FIELDS = {
    "schema", "run_id", "confirmed_browser_ready", "entrypoint_pointer_sha256",
    "entrypoint", "url", "started_by", "started_utc",
}
EXPECTED_WORKERS = ("p2", "p3", "features", "conceptgraphs", "occusg_rooms")
CAMERA_PRODUCER_MANIFEST = "camera_producer_manifest.json"
ROOT_ENTRIES = {
    "branches", "control", "logs", "packets", "perception", "pilot_bootstrap.json",
    "ready", "run_manifest.json", "viewer", "execution_ledger.jsonl",
}


class StartupContractError(ValueError):
    pass


def _host_monotonic_domain() -> str:
    """Return a boot-scoped identifier for the shared host monotonic clock."""
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if not boot_id_path.is_file():
        raise StartupContractError("cannot identify host monotonic clock domain")
    boot_id = boot_id_path.read_text(encoding="ascii").strip()
    if not boot_id:
        raise StartupContractError("host monotonic clock boot ID is empty")
    return f"host_monotonic:{boot_id}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, *, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StartupContractError(f"{context} is not readable JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StartupContractError(f"{context} must be a JSON object: {path}")
    return value


def validate_viewer_start_record(record: Mapping[str, Any], *, run_id: str,
                                 pointer: Mapping[str, Any], pointer_sha256: str,
                                 expected_url: str,
                                 not_before_utc: Optional[str] = None) -> None:
    """Validate the exact control marker produced by browser confirmation."""
    if set(record) != VIEWER_START_FIELDS:
        raise StartupContractError(
            "explicit viewer start signal has unexpected/missing fields: "
            f"{sorted(set(record) ^ VIEWER_START_FIELDS)}")
    if (record.get("schema") != VIEWER_START_SCHEMA
            or record.get("run_id") != run_id
            or record.get("confirmed_browser_ready") is not True
            or record.get("entrypoint_pointer_sha256") != pointer_sha256
            or record.get("entrypoint") != pointer.get("entrypoint")
            or record.get("url") != expected_url
            or record.get("started_by") != "camera-live-pilot-confirm-browser-ready"):
        raise StartupContractError(
            "explicit viewer start signal does not match this run's validated D2 entrypoint")
    started_utc = record.get("started_utc")
    if not isinstance(started_utc, str) or not started_utc:
        raise StartupContractError("explicit viewer start signal has no started_utc")
    try:
        started = datetime.fromisoformat(started_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StartupContractError("explicit viewer start signal has invalid started_utc") from exc
    if started.tzinfo is None:
        raise StartupContractError("explicit viewer start signal started_utc must include a timezone")
    if not_before_utc is not None:
        try:
            created = datetime.fromisoformat(str(not_before_utc).replace("Z", "+00:00"))
        except ValueError as exc:
            raise StartupContractError("pilot created_utc is invalid") from exc
        if created.tzinfo is None:
            raise StartupContractError("pilot created_utc must include a timezone")
        if started < created:
            raise StartupContractError("explicit viewer start signal is stale for this pilot run")
    if started > datetime.now(timezone.utc).astimezone(started.tzinfo) + timedelta(seconds=60):
        raise StartupContractError("explicit viewer start signal is dated in the future")


def terminal_stage_statuses(stages: Mapping[str, Any], *, camera_started: bool,
                            keyframes_consumed: int, mapper_dispatched: int,
                            unified_revisions: int, terminal_started: bool,
                            run_status: str, camera_runner_spawned: bool = False,
                            camera_startup_validated: bool = False,
                            capture_completed: bool = False) -> dict[str, str]:
    """Resolve processing-stage states at exit; worker readiness is separate."""
    result = {str(key): (str(value) if not isinstance(value, Mapping)
                         else str(value.get("status", "unknown")))
              for key, value in stages.items()}
    if run_status == "completed":
        return result
    result["worker_start"] = (
        "workers_ready_then_stopped" if result.get("worker_start") in {
            "all_workers_ready_before_camera", "waiting_for_all_worker_ready",
            "all_fake_workers_ready", "fake_ipc_ready"}
        else "not_started" if result.get("worker_start") in {None, "", "pending", "not_started"}
        else "initialization_failed_or_incomplete")
    result["camera_capture"] = (
        "completed" if capture_completed else "failed_or_partial" if camera_started else "not_started")
    result["camera_startup"] = (
        "completed" if camera_startup_validated and camera_started else
        "validated_no_frame" if camera_startup_validated else
        "failed_or_partial" if camera_runner_spawned else "not_started")
    result["perception"] = (
        "failed_or_partial" if keyframes_consumed > 0 else "not_started")
    result["mappers"] = (
        "failed_or_partial" if mapper_dispatched > 0 else "not_started")
    result["unified_publication"] = (
        "failed_or_partial" if mapper_dispatched > 0 or unified_revisions > 0 else "not_started")
    result["terminal"] = "failed_or_partial" if terminal_started else "not_run"
    return result


def mark_camera_runner_spawned(manifest: dict[str, Any], *, pid: int,
                               monotonic_s: Optional[float] = None,
                               clock_domain: Optional[str] = None) -> dict[str, Any]:
    """Record process launch without claiming capture began or releasing preflight."""
    if manifest.get("status") != "started":
        raise StartupContractError("camera runner may spawn only while pilot remains at pre-capture started state")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise StartupContractError("camera runner PID must be a positive integer")
    if manifest.get("camera_runner", {}).get("status") not in (None, "not_spawned"):
        raise StartupContractError("camera runner has already been spawned for this run")
    spawn_time = time.monotonic() if monotonic_s is None else _finite(monotonic_s, "camera_runner.spawned_monotonic_s")
    domain = clock_domain or _host_monotonic_domain()
    if not isinstance(domain, str) or not domain.startswith("host_monotonic:"):
        raise StartupContractError("camera runner spawn clock domain is invalid")
    manifest["camera_runner"] = {
        "status": "spawned_waiting_for_startup_validation", "state_owner": "camera_live_coordinator",
        "pid": pid, "spawned_monotonic_s": spawn_time, "clock_domain": domain,
        "startup_validated_monotonic_s": None, "first_frame_monotonic_s": None,
        "first_frame_timestamp_s": None, "first_frame_timestamp_clock_domain": None,
        "capture_finished_monotonic_s": None, "capture_finished_timestamp_s": None,
        "capture_finished_timestamp_clock_domain": None, "capture_producer_manifest_sha256": None,
        "capture_has_started": False, "capture_completed": False,
    }
    return manifest


def mark_camera_startup_validated(manifest: dict[str, Any], camera_event: Mapping[str, Any], *,
                                 pid: int) -> dict[str, Any]:
    """Record route-runner validation without claiming a camera frame exists."""
    if camera_event.get("event") != "camera_startup_validated":
        raise StartupContractError("camera startup readiness requires camera_startup_validated event")
    runner = manifest.get("camera_runner")
    if not isinstance(runner, dict) or runner.get("status") != "spawned_waiting_for_startup_validation":
        raise StartupContractError("camera startup validation arrived before process spawn or more than once")
    if runner.get("pid") != pid:
        raise StartupContractError("camera startup validation PID differs from spawned camera process")
    if (camera_event.get("pilot_run_id") != manifest.get("run_id")
            or camera_event.get("route_run_id") != manifest.get("route_run_id")
            or camera_event.get("bootstrap_id") != manifest.get("bootstrap_id")
            or camera_event.get("effective_route_config_sha256") != manifest.get("effective_route_config_sha256")):
        raise StartupContractError("camera startup validation belongs to another run/config")
    if camera_event.get("event_owner") != "habitat_route_runner":
        raise StartupContractError("camera startup validation has no route-runner event owner")
    domain = camera_event.get("clock_domain")
    if domain != runner.get("clock_domain"):
        raise StartupContractError("camera startup validation clock domain differs from coordinator")
    started = _finite(camera_event.get("event_monotonic_s"), "camera_startup_validated.event_monotonic_s")
    if started < float(runner["spawned_monotonic_s"]):
        raise StartupContractError("camera startup validation predates process spawn")
    runner.update({"status": "validated_waiting_for_first_frame", "startup_validated_monotonic_s": started,
                   "startup_validated_clock_domain": domain, "startup_validation_owner": "habitat_route_runner",
                   "capture_has_started": False})
    manifest.setdefault("stages", {})["camera_startup"] = "ready_waiting_for_first_frame"
    return manifest


def mark_camera_capture_started(manifest: dict[str, Any], camera_event: Mapping[str, Any], *,
                                pid: int) -> dict[str, Any]:
    """Move the pilot to running only after observing the first actual camera frame."""
    if camera_event.get("event") != "camera_frame":
        raise StartupContractError("camera capture start requires an observed camera_frame event")
    if not isinstance(camera_event.get("frame_id"), str) or not camera_event.get("frame_id"):
        raise StartupContractError("first camera_frame event is missing frame_id")
    if isinstance(camera_event.get("frame_index"), bool) or not isinstance(camera_event.get("frame_index"), int):
        raise StartupContractError("first camera_frame event has invalid frame_index")
    if manifest.get("status") not in {"started", "running"}:
        raise StartupContractError("first camera frame cannot transition a terminal/preflight run")
    runner = manifest.get("camera_runner")
    if not isinstance(runner, dict) or runner.get("status") != "validated_waiting_for_first_frame":
        raise StartupContractError("first camera frame arrived before route startup validation")
    if runner.get("pid") != pid:
        raise StartupContractError("first camera frame PID differs from spawned camera process")
    if camera_event.get("event_owner") != "habitat_route_runner":
        raise StartupContractError("camera frame event has no route-runner owner")
    if camera_event.get("capture_monotonic_domain") != runner.get("clock_domain"):
        raise StartupContractError("first camera frame clock domain differs from coordinator")
    simulation_domain = camera_event.get("timestamp_clock_domain")
    if not isinstance(simulation_domain, str) or not simulation_domain:
        raise StartupContractError("first camera frame has no source timestamp clock domain")
    frame_mono = _finite(camera_event.get("capture_monotonic_s"), "camera_frame.capture_monotonic_s")
    frame_sim = _finite(camera_event.get("timestamp_s"), "camera_frame.timestamp_s")
    if frame_mono < float(runner["startup_validated_monotonic_s"]):
        raise StartupContractError("first camera frame predates route startup validation")
    manifest["status"] = "running"
    runner.update({"status": "capturing", "capture_has_started": True,
        "first_frame_id": camera_event["frame_id"], "first_frame_index": camera_event["frame_index"],
        "first_frame_monotonic_s": frame_mono, "first_frame_timestamp_s": frame_sim,
        "first_frame_timestamp_clock_domain": simulation_domain,
        "first_frame_observed_owner": "camera_live_coordinator"})
    manifest.setdefault("stages", {}).update({"camera_startup": "completed", "camera_capture": "running",
        "perception": "waiting_for_keyframe", "mappers": "waiting_for_packet",
        "unified_publication": "not_started"})
    return manifest


def mark_camera_capture_completed(manifest: dict[str, Any], capture_stage: Mapping[str, Any], *,
                                  producer_manifest_sha256: str, pid: int) -> dict[str, Any]:
    """Record capture completion from the route runner's terminal producer manifest."""
    runner = manifest.get("camera_runner")
    if not isinstance(runner, dict) or runner.get("status") != "capturing":
        raise StartupContractError("capture completion arrived before a camera frame")
    if runner.get("pid") != pid:
        raise StartupContractError("capture completion PID differs from spawned camera process")
    if capture_stage.get("status") != "pass":
        raise StartupContractError("route producer manifest does not report completed capture")
    finished = _finite(capture_stage.get("capture_finished_monotonic_s"), "camera_capture.capture_finished_monotonic_s")
    domain = capture_stage.get("capture_monotonic_domain")
    if domain != runner.get("clock_domain"):
        raise StartupContractError("capture completion clock domain differs from coordinator")
    if finished < float(runner["first_frame_monotonic_s"]):
        raise StartupContractError("capture completion predates first camera frame")
    if not isinstance(producer_manifest_sha256, str) or len(producer_manifest_sha256) != 64:
        raise StartupContractError("capture completion has no producer-manifest hash")
    finished_sim = _finite(capture_stage.get("simulated_duration_s"), "camera_capture.simulated_duration_s")
    runner.update({"status": "capture_completed", "capture_completed": True,
        "capture_finished_monotonic_s": finished, "capture_finished_timestamp_s": finished_sim,
        "capture_finished_timestamp_clock_domain": "habitat_simulation",
        "capture_completion_owner": "habitat_route_runner_manifest",
        "capture_producer_manifest_sha256": producer_manifest_sha256,
        "camera_process_returncode": 0})
    manifest.setdefault("stages", {})["camera_capture"] = "completed"
    return manifest


def mark_camera_runner_failure(manifest: dict[str, Any], *, status: str,
                               error: str, returncode: Optional[int] = None) -> dict[str, Any]:
    """Terminalize only the camera lifecycle fields owned by the coordinator."""
    if status not in {"failed_before_capture", "cancelled_before_capture",
                      "failed_after_first_frame", "cancelled_after_first_frame"}:
        raise StartupContractError(f"invalid camera runner failure state: {status}")
    runner = manifest.get("camera_runner")
    if not isinstance(runner, dict):
        runner = {"status": "not_spawned", "state_owner": "camera_live_coordinator", "pid": None,
                  "spawned_monotonic_s": None, "startup_validated_monotonic_s": None,
                  "first_frame_monotonic_s": None, "first_frame_timestamp_s": None,
                  "first_frame_timestamp_clock_domain": None, "capture_finished_monotonic_s": None,
                  "capture_finished_timestamp_s": None, "capture_finished_timestamp_clock_domain": None,
                  "capture_has_started": False, "capture_completed": False}
    runner.update({"status": status, "terminal_owner": "camera_live_coordinator",
                   "terminal_error": str(error), "camera_process_returncode": returncode})
    manifest["camera_runner"] = runner
    return manifest


def mark_camera_runner_preflight_stopped(manifest: dict[str, Any], *, returncode: int) -> dict[str, Any]:
    """Record a successful model-free route preflight stopped before capture."""
    runner = manifest.get("camera_runner")
    if not isinstance(runner, dict) or runner.get("status") != "validated_waiting_for_first_frame":
        raise StartupContractError("route preflight may stop only after startup validation")
    if isinstance(returncode, bool) or returncode != 0:
        raise StartupContractError("preflight stop requires a zero child process return code")
    if runner.get("capture_has_started") is not False or runner.get("first_frame_monotonic_s") is not None:
        raise StartupContractError("preflight stop cannot follow a camera frame")
    runner.update({"status": "preflight_validated_stopped", "camera_process_returncode": 0,
                   "stopped_before_capture": True, "stop_owner": "camera_live_coordinator"})
    return manifest


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise StartupContractError(f"{field} must be finite numeric data")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StartupContractError(f"{field} must be finite numeric data") from exc
    if not math.isfinite(number):
        raise StartupContractError(f"{field} must be finite numeric data")
    return number


def _hash_bound_path(config: Mapping[str, Any], path_key: str, hash_key: str,
                     *, optional: bool = False) -> Optional[Path]:
    raw = config.get(path_key)
    expected = config.get(hash_key)
    if optional and raw in (None, ""):
        if expected not in (None, ""):
            raise StartupContractError(f"{hash_key} is set while {path_key} is absent")
        return None
    if not isinstance(raw, str) or not raw:
        raise StartupContractError(f"route config missing {path_key}")
    if not isinstance(expected, str) or len(expected) != 64:
        raise StartupContractError(f"route config missing valid SHA-256 field {hash_key}")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise StartupContractError(f"route {path_key} does not exist: {path}")
    actual = sha256_file(path)
    if actual != expected.lower():
        raise StartupContractError(f"route {path_key} hash mismatch: expected {expected}, got {actual}")
    return path


def validate_route_startup(config_path: Path, output_root: Path, timeout_s: float,
                           *, allow_existing_pilot_root: bool = False) -> dict[str, Any]:
    """Validate config, assets, route args and output policy without a model/simulator."""
    config_path = Path(config_path).expanduser().resolve()
    output_root = Path(output_root).expanduser().resolve()
    if not config_path.is_file():
        raise StartupContractError(f"effective route config does not exist: {config_path}")
    timeout = _finite(timeout_s, "timeout_s")
    if timeout <= 0:
        raise StartupContractError("timeout_s must be greater than zero")
    config = _read_json(config_path, context="effective route config")
    if config.get("schema") != "habitat-smooth-route-effective/v1":
        raise StartupContractError("unsupported effective route config schema")
    policy = config.get("policy")
    if not isinstance(policy, dict) or policy.get("schema") not in {
            "smooth-route-t1-policy/v2", "smooth-route-t1-policy/v3", "smooth-route-t1-policy/v4",
            "smooth-route-t1-policy/v5", "smooth-route-t1-policy/v6"}:
        raise StartupContractError("missing versioned T1 policy")
    if policy.get("schema") in {"smooth-route-t1-policy/v3", "smooth-route-t1-policy/v4",
                                 "smooth-route-t1-policy/v5", "smooth-route-t1-policy/v6"}:
        pacing = policy.get("camera_pacing")
        if not isinstance(pacing, dict) or pacing.get("schema") != "camera-pacing-policy/v1":
            raise StartupContractError("v3 route policy requires camera-pacing-policy/v1")
        if pacing.get("mode") != "monotonic_sim_time":
            raise StartupContractError("unsupported camera pacing mode")
        if pacing.get("overrun_policy") != "record_and_continue_no_catchup":
            raise StartupContractError("unsupported camera pacing overrun policy")
        target_fps = _finite(pacing.get("target_camera_fps"), "camera_pacing.target_camera_fps")
        rtf = _finite(pacing.get("real_time_factor"), "camera_pacing.real_time_factor")
        step = _finite(policy.get("simulator_step_s"), "simulator_step_s")
        if target_fps <= 0 or rtf <= 0:
            raise StartupContractError("camera target fps and real-time factor must be positive")
        if not math.isclose(step, 1.0 / target_fps, rel_tol=0.0, abs_tol=1e-9):
            raise StartupContractError("simulator_step_s must equal 1 / target_camera_fps")
        if policy.get("schema") == "smooth-route-t1-policy/v4":
            timebase = policy.get("motion_timebase")
            if not isinstance(timebase, dict) or timebase.get("schema") != "habitat-motion-timebase/v1":
                raise StartupContractError("v4 route policy requires an explicit Habitat motion timebase")
            if timebase.get("simulation_clock_source") != "habitat_sim.Simulator.step(dt=simulator_step_s); observed with get_world_time":
                raise StartupContractError("v4 motion timebase source is not the locked Habitat API")
            if not math.isclose(_finite(timebase.get("simulation_dt_s"), "motion_timebase.simulation_dt_s"), step,
                                rel_tol=0.0, abs_tol=1e-9):
                raise StartupContractError("motion_timebase simulation dt differs from simulator_step_s")
            if not math.isclose(_finite(timebase.get("camera_sampling_interval_sim_s"), "motion_timebase.camera_sampling_interval_sim_s"),
                                step, rel_tol=0.0, abs_tol=1e-9):
                raise StartupContractError("camera sampling interval differs from the locked simulator dt")
            if not math.isclose(_finite(timebase.get("real_time_factor"), "motion_timebase.real_time_factor"),
                                rtf, rel_tol=0.0, abs_tol=1e-9):
                raise StartupContractError("motion timebase RTF differs from camera pacing policy")
            if (not math.isclose(_finite(timebase.get("linear_speed_mps"), "motion_timebase.linear_speed_mps"),
                                 _finite(policy.get("linear_speed_mps"), "linear_speed_mps"), rel_tol=0.0, abs_tol=1e-12)
                    or not math.isclose(_finite(timebase.get("angular_speed_deg_per_s"), "motion_timebase.angular_speed_deg_per_s"),
                                        _finite(policy.get("angular_speed_deg_per_s"), "angular_speed_deg_per_s"), rel_tol=0.0, abs_tol=1e-12)):
                raise StartupContractError("v4 motion timebase speeds differ from the locked policy speeds")
            expected_move = float(policy["linear_speed_mps"]) * step
            expected_yaw = float(policy["angular_speed_deg_per_s"]) * step
            if (not math.isclose(_finite(timebase.get("per_step_displacement_m"), "motion_timebase.per_step_displacement_m"),
                                 expected_move, rel_tol=0.0, abs_tol=1e-12)
                    or not math.isclose(_finite(timebase.get("per_step_yaw_deg"), "motion_timebase.per_step_yaw_deg"),
                                        expected_yaw, rel_tol=0.0, abs_tol=1e-12)):
                raise StartupContractError("v4 per-step motion differs from speed multiplied by simulation dt")
            wall_interval = _finite(timebase.get("target_wall_interval_s"), "motion_timebase.target_wall_interval_s")
            expected_wall_interval = 1.0 / (target_fps * rtf)
            if not math.isclose(wall_interval, expected_wall_interval, rel_tol=0.0, abs_tol=1e-9):
                raise StartupContractError("v4 target wall pacing interval differs from camera fps and RTF")
        if policy.get("schema") in {"smooth-route-t1-policy/v5", "smooth-route-t1-policy/v6"}:
            timebase = policy.get("motion_timebase")
            if not isinstance(timebase, dict) or timebase.get("schema") != "habitat-motion-timebase/v2":
                raise StartupContractError("v5/v6 route policy requires habitat-motion-timebase/v2")
            if timebase.get("timestamp_authority") != "habitat_sim.Simulator.get_world_time_after_step":
                raise StartupContractError("v5 timestamp authority must be Habitat world time after each step")
            if timebase.get("requested_dt_semantics") != "requested_step_dt_not_assumed_observed_delta":
                raise StartupContractError("v5 requested/observed dt semantics are not explicit")
            if timebase.get("deviation_policy") != "record_positive_finite_observed_delta; enforce_locked_operational_guards":
                raise StartupContractError("v5 observed-delta policy is not the locked record-and-guard contract")
            if timebase.get("actual_speed_semantics") != "pose_delta_divided_by_observed_simulation_delta":
                raise StartupContractError("v5 actual speed measurement semantics are missing")
            if not math.isclose(_finite(timebase.get("requested_simulator_dt_s"), "motion_timebase.requested_simulator_dt_s"),
                                step, rel_tol=0.0, abs_tol=1e-9):
                raise StartupContractError("v5 requested simulator dt differs from simulator_step_s")
            if not math.isclose(_finite(timebase.get("target_fps"), "motion_timebase.target_fps"),
                                target_fps, rel_tol=0.0, abs_tol=1e-9):
                raise StartupContractError("v5 target fps differs from camera pacing policy")
            if not math.isclose(_finite(timebase.get("real_time_factor_target"), "motion_timebase.real_time_factor_target"),
                                rtf, rel_tol=0.0, abs_tol=1e-9):
                raise StartupContractError("v5 RTF target differs from camera pacing policy")
            expected_move = float(policy["linear_speed_mps"]) * step
            expected_yaw = float(policy["angular_speed_deg_per_s"]) * step
            if (not math.isclose(_finite(timebase.get("move_increment_m_per_action"), "motion_timebase.move_increment_m_per_action"),
                                 expected_move, rel_tol=0.0, abs_tol=1e-12)
                    or not math.isclose(_finite(timebase.get("turn_increment_deg_per_action"), "motion_timebase.turn_increment_deg_per_action"),
                                        expected_yaw, rel_tol=0.0, abs_tol=1e-12)):
                raise StartupContractError("v5 fixed action increments differ from the locked nominal speeds and requested dt")
            limits = timebase.get("operational_stop_limits")
            if not isinstance(limits, dict):
                raise StartupContractError("v5 timebase must name locked operational stop limits")
            route_follow = policy.get("route_follow")
            if (not isinstance(route_follow, dict)
                    or limits.get("max_simulation_elapsed_s") != route_follow.get("max_sim_time_s")
                    or limits.get("max_camera_frames") != route_follow.get("max_camera_frames")
                    or limits.get("wall_timeout_source") != "camera runner --timeout-s" ):
                raise StartupContractError("v5 timebase stop limits differ from the locked route/runtime guards")
        if policy.get("schema") == "smooth-route-t1-policy/v6":
            geometry = policy.get("geometry_contract")
            if not isinstance(geometry, dict) or geometry.get("schema") != "habitat-navmesh-clearance-contract/v1":
                raise StartupContractError("v6 route policy requires habitat-navmesh-clearance-contract/v1")
            if geometry.get("clearance_api") != "PathFinder.distance_to_closest_obstacle":
                raise StartupContractError("v6 clearance API is not the locked PathFinder query")
            if geometry.get("clearance_unavailable_policy") != "fail_closed":
                raise StartupContractError("v6 must fail closed when the clearance query is unavailable")
            if geometry.get("boundary_policy") != "fail_closed_within_margin_plus_numeric_tolerance":
                raise StartupContractError("v6 clearance boundary policy is unsupported")
            if geometry.get("max_search_radius_policy") != "extra_margin_plus_2x_numeric_boundary_tolerance":
                raise StartupContractError("v6 clearance search-radius policy is unsupported")
            expected_radius = _finite(geometry.get("navmesh_baked_agent_radius_m"),
                                      "geometry_contract.navmesh_baked_agent_radius_m")
            expected_height = _finite(geometry.get("navmesh_baked_agent_height_m"),
                                      "geometry_contract.navmesh_baked_agent_height_m")
            tolerance = _finite(geometry.get("profile_compatibility_tolerance_m"),
                                "geometry_contract.profile_compatibility_tolerance_m")
            margin = _finite(geometry.get("extra_navmesh_clearance_margin_m"),
                             "geometry_contract.extra_navmesh_clearance_margin_m")
            epsilon = _finite(geometry.get("numeric_boundary_tolerance_m"),
                              "geometry_contract.numeric_boundary_tolerance_m")
            vertical = _finite(geometry.get("vertical_navmesh_tolerance_m"),
                               "geometry_contract.vertical_navmesh_tolerance_m")
            runtime_profile = geometry.get("runtime_agent_profile")
            if not isinstance(runtime_profile, dict):
                raise StartupContractError("v6 geometry contract requires runtime_agent_profile")
            runtime_radius = _finite(runtime_profile.get("radius_m"), "runtime_agent_profile.radius_m")
            runtime_height = _finite(runtime_profile.get("height_m"), "runtime_agent_profile.height_m")
            if min(expected_radius, expected_height, runtime_radius, runtime_height) <= 0.0:
                raise StartupContractError("v6 navmesh/profile radius and height must be positive")
            if tolerance <= 0.0 or epsilon <= 0.0 or margin < 0.0 or vertical < 0.0:
                raise StartupContractError("v6 tolerances/margins are invalid")
            if (abs(expected_radius - runtime_radius) > tolerance
                    or abs(expected_height - runtime_height) > tolerance):
                raise StartupContractError("v6 runtime profile differs from the declared baked navmesh profile")
            if geometry.get("numeric_boundary_tolerance_basis") != "float32 PathFinder API; 0.1mm fail-closed band, 0.05m navmesh cell":
                raise StartupContractError("v6 numeric boundary tolerance basis is missing or changed")
            if "vertical_navmesh_tolerance_m" in policy.get("route_follow", {}):
                if not math.isclose(
                        _finite(policy["route_follow"]["vertical_navmesh_tolerance_m"],
                                "route_follow.vertical_navmesh_tolerance_m"),
                        vertical, rel_tol=0.0, abs_tol=1e-12):
                    raise StartupContractError("v6 preflight/runtime vertical tolerances must match")
    for key in ("run_id", "scene_id", "sequence_id", "dataset_id", "route_id"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise StartupContractError(f"route config missing non-empty {key}")
    if config.get("resolution_px") != [640, 480]:
        raise StartupContractError("camera-live route is locked to native 640x480 capture")
    fov = _finite(config.get("horizontal_fov_deg"), "horizontal_fov_deg")
    if not 1.0 <= fov < 179.0:
        raise StartupContractError("horizontal_fov_deg is outside (1, 179)")
    points = config.get("route_points")
    if not isinstance(points, list) or len(points) < 2:
        raise StartupContractError("route config must contain at least two route_points")
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise StartupContractError(f"route_points[{index}] must be an object")
        position = point.get("position_habitat")
        if not isinstance(position, list) or len(position) != 3:
            raise StartupContractError(f"route_points[{index}].position_habitat must have 3 coordinates")
        for axis, value in zip("xyz", position):
            _finite(value, f"route_points[{index}].position_habitat.{axis}")
        _finite(point.get("yaw_rad"), f"route_points[{index}].yaw_rad")
    indices = config.get("selected_route_indices")
    if (not isinstance(indices, list) or len(indices) < 2
            or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 or i >= len(points) for i in indices)):
        raise StartupContractError("selected_route_indices must index at least two configured route points")

    route_budget = None
    # Geometry-derived motion budgets use the requested dt because the route
    # is executed with fixed per-action increments. Observed Habitat clock
    # deltas are measured and guarded separately by the route runner.
    if policy.get("schema") in {"smooth-route-t1-policy/v4", "smooth-route-t1-policy/v5",
                                 "smooth-route-t1-policy/v6"}:
        try:
            route_budget = derive_route_budget(points, simulation_dt_s=float(policy["simulator_step_s"]),
                linear_speed_mps=float(policy["linear_speed_mps"]),
                angular_speed_deg_per_s=float(policy["angular_speed_deg_per_s"]),
                path_sample_step_m=float(policy["route_follow"]["path_sample_step_m"]),
                keyframe_min_translation_m=float(policy["keyframes"]["min_translation_m"]),
                keyframe_min_yaw_deg=float(policy["keyframes"]["min_yaw_deg"]),
                max_camera_frames=int(policy["route_follow"]["max_camera_frames"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise StartupContractError(f"route/timebase frame budget is invalid: {exc}") from exc

    bound_paths = {
        "scene_asset": _hash_bound_path(config, "scene_asset", "scene_asset_sha256"),
        "navmesh_file": _hash_bound_path(config, "navmesh_file", "navmesh_sha256"),
        "source_route_path": _hash_bound_path(config, "source_route_path", "source_route_sha256"),
        "policy_path": _hash_bound_path(config, "policy_path", "policy_sha256"),
    }
    profile = config.get("capture_profile")
    if not isinstance(profile, dict):
        raise StartupContractError("route config missing capture_profile provenance")
    profile_path = profile.get("profile_path")
    profile_hash = profile.get("profile_sha256")
    if not isinstance(profile_path, str) or not isinstance(profile_hash, str):
        raise StartupContractError("capture_profile must include profile_path and profile_sha256")
    profile_file = Path(profile_path).expanduser().resolve()
    if not profile_file.is_file() or sha256_file(profile_file) != profile_hash.lower():
        raise StartupContractError(f"capture profile missing or hash mismatch: {profile_file}")
    if profile.get("resolution_px") != [640, 480] or profile.get("calibration_id") != config.get("calibration_id"):
        raise StartupContractError("capture profile resolution/calibration differs from effective route config")
    bound_paths["capture_profile"] = profile_file
    bound_paths["scene_dataset_config"] = _hash_bound_path(
        config, "scene_dataset_config", "scene_dataset_config_sha256", optional=True)

    if allow_existing_pilot_root:
        if not output_root.is_dir():
            raise StartupContractError(f"pilot output root must already exist: {output_root}")
    elif output_root.exists():
        raise FileExistsError(f"refusing existing output path: {output_root}")
    return {"config": config, "config_path": config_path, "config_sha256": sha256_file(config_path),
            "output_root": output_root, "timeout_s": timeout, "bound_paths": bound_paths,
            "route_budget": route_budget}


def build_bootstrap_document(*, run_id: str, bootstrap_id: str, route: Mapping[str, Any],
                             source_root: Path, source_manifest_sha256: str,
                             clock_domain: str, created_monotonic_s: float) -> dict[str, Any]:
    config = route["config"]
    return {
        "schema": BOOTSTRAP_SCHEMA, "status": "bootstrapping", "run_id": run_id,
        "pilot_run_id": run_id,
        "bootstrap_id": bootstrap_id, "output_root": str(route["output_root"]),
        "effective_route_config_path": str(route["config_path"]),
        "effective_route_config_sha256": route["config_sha256"],
        "route_run_id": config["run_id"], "scene_id": config["scene_id"],
        "sequence_id": config["sequence_id"], "source_root": str(Path(source_root).resolve()),
        "source_manifest_sha256": source_manifest_sha256,
        "expected_workers": list(EXPECTED_WORKERS), "clock_domain": clock_domain,
        "created_monotonic_s": float(created_monotonic_s),
    }


def resolve_camera_producer_manifest_path(output_root: Path, requested: Optional[Path] = None,
                                          *, allow_existing_pilot_root: bool) -> Path:
    """Keep route-producer bookkeeping separate from the owning pilot manifest."""
    root = Path(output_root).expanduser().resolve()
    expected = (root / CAMERA_PRODUCER_MANIFEST if allow_existing_pilot_root
                else root / "run_manifest.json")
    target = Path(requested).expanduser().resolve() if requested is not None else expected
    if target != expected:
        raise StartupContractError(
            f"manifest output must be {expected}; route and pilot manifests have separate owners")
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing producer manifest: {target}")
    return target


TERMINAL_BOOTSTRAP_STATUSES = {
    "startup_preflight_passed", "startup_preflight_failed", "completed", "failed",
    "failed_before_capture", "cancelled", "cancelled_before_capture",
}


def terminalize_pilot_documents(manifest: dict[str, Any], bootstrap: dict[str, Any], *,
                                status: str, finished_utc: str,
                                error: Optional[str] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one terminal lifecycle status without conflating route and pilot IDs."""
    if status not in TERMINAL_BOOTSTRAP_STATUSES:
        raise StartupContractError(f"not a terminal pilot/bootstrap status: {status}")
    pilot_id = str(bootstrap.get("pilot_run_id", bootstrap.get("run_id", "")))
    if not pilot_id or manifest.get("run_id") != pilot_id or bootstrap.get("run_id") != pilot_id:
        raise StartupContractError("pilot manifest and bootstrap run IDs do not agree")
    if manifest.get("route_run_id") != bootstrap.get("route_run_id"):
        raise StartupContractError("pilot and bootstrap route_run_id values do not agree")
    for document in (manifest, bootstrap):
        previous = document.get("status")
        if previous in TERMINAL_BOOTSTRAP_STATUSES and previous != status:
            raise StartupContractError(f"refusing terminal lifecycle transition {previous!r} -> {status!r}")
        document["status"] = status
        document["pilot_run_id"] = pilot_id
        document["terminal"] = True
        document["finished_utc"] = finished_utc
        if error is not None:
            document["error"] = error
    return manifest, bootstrap


def ready_marker(*, kind: str, pid: int, run_id: str, bootstrap_id: str,
                 route_config_sha256: str, ready_monotonic_s: float,
                 clock_domain: str) -> dict[str, Any]:
    if kind not in EXPECTED_WORKERS:
        raise StartupContractError(f"unexpected ready worker kind: {kind}")
    return {"schema": READY_SCHEMA, "kind": kind, "pid": int(pid), "run_id": run_id,
            "bootstrap_id": bootstrap_id, "route_config_sha256": route_config_sha256,
            "ready_monotonic_s": float(ready_monotonic_s), "clock_domain": clock_domain}


def validate_bootstrap_before_capture(run_root: Path, *, expected_bootstrap_id: str,
                                      expected_route_config_sha256: str,
                                      expected_ready_pids: Optional[Mapping[str, int]] = None) -> dict[str, Any]:
    """Reject stale/foreign capture roots and unexpected bootstrap state."""
    run_root = Path(run_root).expanduser().resolve()
    if not run_root.is_dir():
        raise StartupContractError(f"pilot root is missing: {run_root}")
    if (run_root / "capture").exists():
        raise StartupContractError("capture/ already exists; refusing to append or overwrite a prior capture")
    entries = {p.name for p in run_root.iterdir()}
    if (run_root / CAMERA_PRODUCER_MANIFEST).exists():
        raise StartupContractError("camera producer manifest already exists before capture; refusing reuse")
    unexpected = sorted(entries - ROOT_ENTRIES)
    missing = sorted(ROOT_ENTRIES - entries)
    if unexpected or missing:
        raise StartupContractError(f"pilot root inventory mismatch; missing={missing}, unexpected={unexpected}")
    bootstrap = _read_json(run_root / "pilot_bootstrap.json", context="pilot bootstrap")
    manifest = _read_json(run_root / "run_manifest.json", context="pilot run manifest")
    if bootstrap.get("schema") != BOOTSTRAP_SCHEMA or bootstrap.get("status") != "bootstrapping":
        raise StartupContractError("pilot bootstrap schema/status is not a fresh pre-capture state")
    if bootstrap.get("expected_workers") != list(EXPECTED_WORKERS):
        raise StartupContractError("pilot bootstrap worker inventory differs from the locked runtime contract")
    if (bootstrap.get("run_id") != run_root.name or manifest.get("run_id") != run_root.name
            or bootstrap.get("pilot_run_id", bootstrap.get("run_id")) != run_root.name
            or manifest.get("pilot_run_id", manifest.get("run_id")) != run_root.name):
        raise StartupContractError("pilot bootstrap/run manifest belongs to a different run")
    if bootstrap.get("bootstrap_id") != expected_bootstrap_id or manifest.get("bootstrap_id") != expected_bootstrap_id:
        raise StartupContractError("pilot bootstrap ownership token mismatch")
    if bootstrap.get("effective_route_config_sha256") != expected_route_config_sha256:
        raise StartupContractError("pilot bootstrap route config hash mismatch")
    config_path = Path(str(bootstrap.get("effective_route_config_path", ""))).resolve()
    if not config_path.is_file() or sha256_file(config_path) != expected_route_config_sha256:
        raise StartupContractError("effective route config changed after coordinator preflight")
    config = _read_json(config_path, context="effective route config")
    if any(bootstrap.get(key) != config.get(source_key) for key, source_key in (
            ("route_run_id", "run_id"), ("scene_id", "scene_id"), ("sequence_id", "sequence_id"))):
        raise StartupContractError("pilot bootstrap scene/route identity differs from the locked effective config")
    if bootstrap.get("output_root") != str(run_root):
        raise StartupContractError("pilot bootstrap output root does not match the active directory")
    for key in ("route_run_id", "scene_id", "sequence_id"):
        if bootstrap.get(key) != manifest.get(key):
            raise StartupContractError(f"pilot manifest {key} differs from bootstrap")
    if manifest.get("effective_route_config_sha256") != expected_route_config_sha256:
        raise StartupContractError("pilot run manifest route config hash mismatch")
    if (manifest.get("source_root") != bootstrap.get("source_root")
            or manifest.get("source_manifest_sha256") != bootstrap.get("source_manifest_sha256")):
        raise StartupContractError("pilot run manifest source identity differs from bootstrap")
    if manifest.get("status") != "started":
        raise StartupContractError(f"pilot manifest is not at pre-capture started state: {manifest.get('status')}")
    created_utc = manifest.get("created_utc")
    if not isinstance(created_utc, str) or not created_utc:
        raise StartupContractError("pilot manifest has no created_utc lifecycle anchor")
    try:
        created_time = datetime.fromisoformat(created_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StartupContractError("pilot manifest created_utc is invalid") from exc
    if created_time.tzinfo is None:
        raise StartupContractError("pilot manifest created_utc must include a timezone")
    source_root = Path(str(bootstrap.get("source_root", ""))).resolve()
    source_manifest_path = source_root / "c2_run_manifest.json"
    if (not source_manifest_path.is_file()
            or sha256_file(source_manifest_path) != bootstrap.get("source_manifest_sha256")):
        raise StartupContractError("C2 source manifest is missing or changed since bootstrap")

    ledger_path = run_root / "execution_ledger.jsonl"
    if not ledger_path.is_file():
        raise StartupContractError("execution ledger is missing before capture")
    ledger_rows = []
    try:
        for line_no, line in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("schema") != "camera-live-execution-event/v1":
                raise ValueError(f"invalid ledger schema at line {line_no}")
            ledger_rows.append(row)
    except Exception as exc:
        raise StartupContractError(f"execution ledger has invalid/incomplete pre-capture data: {exc}") from exc
    for event_name in ("pilot_startup_attempt_started",):
        if sum(row.get("event") == event_name and row.get("run_id") == run_root.name
               for row in ledger_rows) != 1:
            raise StartupContractError(f"execution ledger must contain one run-bound {event_name}")

    control = run_root / "control"
    control_entries = sorted(p.name for p in control.iterdir()) if control.is_dir() else None
    if control_entries is None:
        raise StartupContractError("control/ must be a directory")
    if control_entries not in ([], ["start.json"]):
        raise StartupContractError(f"unexpected pre-capture control contents: {control_entries}")
    if control_entries == ["start.json"]:
        # The live runner deliberately waits for the user to see the served
        # waiting page before capture. Accept only that exact, run-bound start
        # receipt; all other control files and unbound markers remain blocked.
        start_path = control / "start.json"
        start = _read_json(start_path, context="explicit viewer start signal")
        viewer = manifest.get("viewer")
        pointer_path = run_root / "viewer" / "d2_viewer_entrypoint.json"
        try:
            pointer = _read_json(pointer_path, context="D2 entrypoint pointer")
        except StartupContractError as exc:
            raise StartupContractError(f"explicit start signal has no valid D2 pointer: {exc}") from exc
        pointer_hash = sha256_file(pointer_path)
        expected_url = viewer.get("url") if isinstance(viewer, dict) else None
        if not isinstance(expected_url, str):
            raise StartupContractError("explicit viewer start signal has no run-bound URL")
        validate_viewer_start_record(start, run_id=run_root.name, pointer=pointer,
                                     pointer_sha256=pointer_hash, expected_url=expected_url,
                                     not_before_utc=created_utc)
        if manifest.get("viewer", {}).get("browser_ready_start_signal_sha256") != sha256_file(start_path):
            raise StartupContractError("explicit viewer start signal hash differs from the run manifest")
        confirmed = [row for row in ledger_rows if row.get("event") == "browser_ready_confirmed"
                     and row.get("run_id") == run_root.name
                     and row.get("start_signal_sha256") == sha256_file(start_path)]
        if len(confirmed) != 1:
            raise StartupContractError("execution ledger does not bind exactly one browser confirmation to start.json")

    ready_dir = run_root / "ready"
    if not ready_dir.is_dir():
        raise StartupContractError("ready/ must be a directory")
    expected_names = {f"{kind}.json" for kind in EXPECTED_WORKERS}
    actual_names = {p.name for p in ready_dir.iterdir()}
    if actual_names != expected_names or any(not (ready_dir / name).is_file() for name in expected_names):
        raise StartupContractError(f"ready/ contents mismatch; expected={sorted(expected_names)}, actual={sorted(actual_names)}")
    expected_fields = {"schema", "kind", "pid", "run_id", "bootstrap_id",
                       "route_config_sha256", "ready_monotonic_s", "clock_domain"}
    markers = {}
    for kind in EXPECTED_WORKERS:
        doc = _read_json(ready_dir / f"{kind}.json", context=f"{kind} readiness marker")
        if set(doc) != expected_fields:
            raise StartupContractError(f"{kind} readiness marker has unexpected/missing fields: {sorted(set(doc) ^ expected_fields)}")
        if (doc.get("schema") != READY_SCHEMA or doc.get("kind") != kind
                or doc.get("run_id") != run_root.name
                or doc.get("bootstrap_id") != expected_bootstrap_id
                or doc.get("route_config_sha256") != expected_route_config_sha256):
            raise StartupContractError(f"{kind} readiness marker belongs to another run/config")
        if isinstance(doc.get("pid"), bool) or not isinstance(doc.get("pid"), int) or doc["pid"] <= 0:
            raise StartupContractError(f"{kind} readiness marker has invalid pid")
        _finite(doc.get("ready_monotonic_s"), f"{kind}.ready_monotonic_s")
        if not isinstance(doc.get("clock_domain"), str) or not doc["clock_domain"].startswith("host_monotonic:"):
            raise StartupContractError(f"{kind} readiness marker has invalid clock domain")
        if expected_ready_pids is not None and doc["pid"] != expected_ready_pids.get(kind):
            raise StartupContractError(f"{kind} readiness pid does not match launched worker")
        markers[kind] = doc
    initialized = {str(row.get("worker")) for row in ledger_rows
                   if row.get("event") == "worker_initialized" and row.get("run_id") == run_root.name}
    ready_events = {str(row.get("worker")): row for row in ledger_rows
                    if row.get("event") == "worker_ready" and row.get("run_id") == run_root.name}
    if initialized != set(EXPECTED_WORKERS) or set(ready_events) != set(EXPECTED_WORKERS):
        raise StartupContractError("execution ledger does not account for every initialized and ready worker")
    if any(ready_events[kind].get("pid") != markers[kind].get("pid") for kind in EXPECTED_WORKERS):
        raise StartupContractError("execution ledger worker-ready PIDs differ from readiness markers")
    domains = {doc["clock_domain"] for doc in markers.values()}
    if len(domains) != 1:
        raise StartupContractError("workers do not share one host monotonic clock domain")

    # Validate the deterministic runtime layout. Operational logs may be
    # empty, but no artifact from another run may be smuggled into these roots.
    expected_dirs = {
        "packets": {"assets"}, "packets/assets": {"detection_2d"},
        "packets/assets/detection_2d": {"masks"}, "packets/assets/detection_2d/masks": set(),
        "perception": {"p2"}, "perception/p2": set(),
        "branches": {"objects", "rooms"}, "branches/objects": {"acks"},
        "branches/rooms": {"acks"}, "branches/objects/acks": set(),
        "branches/rooms/acks": set(),
    }
    for rel, expected in expected_dirs.items():
        path = run_root / rel
        if not path.is_dir():
            raise StartupContractError(f"runtime bootstrap directory missing: {rel}")
        names = {p.name for p in path.iterdir()}
        if names != expected:
            raise StartupContractError(f"unexpected bootstrap contents at {rel}: expected={sorted(expected)}, actual={sorted(names)}")
    logs = run_root / "logs"
    expected_logs = {"viewer_server.log", "camera_producer.log"} | {f"{kind}_worker.log" for kind in EXPECTED_WORKERS}
    actual_logs = {p.name for p in logs.iterdir()} if logs.is_dir() else set()
    if actual_logs != expected_logs or any(not (logs / name).is_file() for name in expected_logs):
        raise StartupContractError(f"pre-capture logs mismatch; expected={sorted(expected_logs)}, actual={sorted(actual_logs)}")

    viewer = run_root / "viewer"
    for name in ("live_index.json", "live_head.json", "d2_viewer_entrypoint.json"):
        if not (viewer / name).is_file():
            raise StartupContractError(f"D2 bootstrap file missing: viewer/{name}")
    index = _read_json(viewer / "live_index.json", context="D2 live index")
    head = _read_json(viewer / "live_head.json", context="D2 live head")
    if index.get("status") != "starting" or index.get("frames") or head.get("revision_count") != 0:
        raise StartupContractError("D2 viewer is not waiting before first revision")
    if head.get("live_index_sha256") != sha256_file(viewer / "live_index.json"):
        raise StartupContractError("D2 live head hash does not match the initial live index")
    pointer = _read_json(viewer / "d2_viewer_entrypoint.json", context="D2 entrypoint pointer")
    if pointer.get("schema") != "d2-viewer-entrypoint/v1" or pointer.get("status") != "ready":
        raise StartupContractError("D2 viewer entrypoint pointer is not ready")
    manifest_name = pointer.get("manifest_path")
    entrypoint_name = pointer.get("entrypoint")
    if (not isinstance(manifest_name, str) or Path(manifest_name).name != manifest_name
            or not isinstance(entrypoint_name, str) or Path(entrypoint_name).name != entrypoint_name):
        raise StartupContractError("D2 pointer has an invalid manifest or entrypoint path")
    d2_manifest_path = viewer / manifest_name
    entrypoint_path = viewer / entrypoint_name
    if not d2_manifest_path.is_file() or not entrypoint_path.is_file():
        raise StartupContractError("D2 viewer manifest or entrypoint file is missing")
    if sha256_file(d2_manifest_path) != pointer.get("manifest_sha256"):
        raise StartupContractError("D2 viewer manifest hash differs from entrypoint pointer")
    if sha256_file(entrypoint_path) != pointer.get("entrypoint_sha256"):
        raise StartupContractError("D2 entrypoint hash differs from entrypoint pointer")
    d2_manifest = _read_json(d2_manifest_path, context="D2 viewer manifest")
    if d2_manifest.get("run_id") != run_root.name or d2_manifest.get("status") != "running":
        raise StartupContractError("D2 viewer manifest belongs to another run or is not ready")
    if d2_manifest.get("viewer_entrypoint") != entrypoint_name:
        raise StartupContractError("D2 manifest and pointer name different viewer entrypoints")
    return {"status": "capture_boundary_ready", "run_id": run_root.name,
            "bootstrap_id": expected_bootstrap_id, "ready_workers": markers,
            "camera_started": False, "models_loaded_by_preflight": False,
            "capture_directory_exists": False, "capture_boundary_reached": True}
