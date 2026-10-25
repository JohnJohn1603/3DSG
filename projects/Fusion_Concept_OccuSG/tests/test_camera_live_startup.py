from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from integration_v2.camera_live_contract import host_monotonic_domain
from integration_v2.camera_live_startup import (
    CAMERA_PRODUCER_MANIFEST, EXPECTED_WORKERS, StartupContractError,
    build_bootstrap_document, ready_marker, resolve_camera_producer_manifest_path,
    mark_camera_capture_started, mark_camera_runner_spawned,
    mark_camera_startup_validated, mark_camera_runner_preflight_stopped,
    mark_camera_runner_failure,
    sha256_file, terminalize_pilot_documents, validate_bootstrap_before_capture,
    validate_route_startup, terminal_stage_statuses,
)
from scripts.run_camera_live_pilot import confirm_browser_ready


def _write(path: Path, data: bytes | str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return sha256_file(path)


def _route(tmp_path: Path) -> tuple[Path, dict]:
    scene = tmp_path / "scene.glb"
    navmesh = tmp_path / "scene.navmesh"
    route = tmp_path / "route.json"
    policy = tmp_path / "policy.json"
    profile = tmp_path / "capture-profile.json"
    scene_hash = _write(scene, b"fixture-scene")
    navmesh_hash = _write(navmesh, b"fixture-navmesh")
    route_hash = _write(route, "{}\n")
    policy_hash = _write(policy, '{"schema":"smooth-route-t1-policy/v2"}\n')
    profile_hash = _write(profile, '{"schema":"habitat-capture-profile/v1"}\n')
    config = {
        "schema": "habitat-smooth-route-effective/v1", "run_id": "route-fixture-v1",
        "scene_id": "fixture-scene", "sequence_id": "fixture-sequence",
        "dataset_id": "fixture", "route_id": "route-a", "resolution_px": [640, 480],
        "horizontal_fov_deg": 90.0, "calibration_id": "k-fixture",
        "route_points": [{"position_habitat": [0, 0, 0], "yaw_rad": 0},
                         {"position_habitat": [1, 0, 0], "yaw_rad": 0}],
        "selected_route_indices": [0, 1],
        "policy": {"schema": "smooth-route-t1-policy/v2"},
        "scene_asset": str(scene), "scene_asset_sha256": scene_hash,
        "navmesh_file": str(navmesh), "navmesh_sha256": navmesh_hash,
        "source_route_path": str(route), "source_route_sha256": route_hash,
        "policy_path": str(policy), "policy_sha256": policy_hash,
        "capture_profile": {"profile_path": str(profile), "profile_sha256": profile_hash,
                             "resolution_px": [640, 480], "calibration_id": "k-fixture"},
    }
    config_path = tmp_path / "effective-route.json"
    _write(config_path, json.dumps(config))
    return config_path, config


def _live_root(tmp_path: Path) -> tuple[Path, str, str, Path]:
    config_path, config = _route(tmp_path)
    run_id = "camera-fixture-v1"
    bootstrap_id = "a" * 32
    root = tmp_path / run_id
    route_result = validate_route_startup(config_path, root, 30)
    root.mkdir()
    for rel in (
        "logs", "control", "ready", "packets/assets/detection_2d/masks",
        "perception/p2", "branches/objects/acks", "branches/rooms/acks", "viewer",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source-c2"
    source.mkdir()
    source_manifest_sha = _write(source / "c2_run_manifest.json", '{"status":"completed"}\n')
    bootstrap = build_bootstrap_document(run_id=run_id, bootstrap_id=bootstrap_id,
        route=route_result, source_root=source, source_manifest_sha256=source_manifest_sha,
        clock_domain=host_monotonic_domain(), created_monotonic_s=time.monotonic())
    _write(root / "pilot_bootstrap.json", json.dumps(bootstrap))
    run_manifest = {"schema": "camera-live-pilot/v1", "run_id": run_id,
        "pilot_run_id": run_id,
        "bootstrap_id": bootstrap_id, "status": "started",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "effective_route_config_sha256": route_result["config_sha256"],
        "source_root": str(source.resolve()), "source_manifest_sha256": source_manifest_sha,
        "route_run_id": config["run_id"], "scene_id": config["scene_id"],
        "sequence_id": config["sequence_id"]}
    _write(root / "run_manifest.json", json.dumps(run_manifest))
    for name in {"viewer_server.log", "camera_producer.log"} | {f"{k}_worker.log" for k in EXPECTED_WORKERS}:
        _write(root / "logs" / name, b"")
    for kind in EXPECTED_WORKERS:
        doc = ready_marker(kind=kind, pid=1234, run_id=run_id, bootstrap_id=bootstrap_id,
            route_config_sha256=route_result["config_sha256"], ready_monotonic_s=time.monotonic(),
            clock_domain=host_monotonic_domain())
        _write(root / "ready" / f"{kind}.json", json.dumps(doc))
    ledger=[{"schema":"camera-live-execution-event/v1","event":"pilot_startup_attempt_started",
             "run_id":run_id}]
    for kind in EXPECTED_WORKERS:
        ledger.extend([
            {"schema":"camera-live-execution-event/v1","event":"worker_initialized","worker":kind,"run_id":run_id},
            {"schema":"camera-live-execution-event/v1","event":"worker_ready","worker":kind,"run_id":run_id,"pid":1234},
        ])
    _write(root / "execution_ledger.jsonl", "\n".join(json.dumps(x) for x in ledger)+"\n")
    entrypoint = "index_d2_fixture.html"
    manifest_name = "d2_manifest_fixture.json"
    entry_hash = _write(root / "viewer" / entrypoint, "<!doctype html><html></html>")
    d2_manifest = {"schema": "d2-xyz-viewer/v1", "status": "running", "run_id": run_id,
        "viewer_entrypoint": entrypoint}
    manifest_hash = _write(root / "viewer" / manifest_name, json.dumps(d2_manifest))
    _write(root / "viewer" / "d2_viewer_entrypoint.json", json.dumps({
        "schema": "d2-viewer-entrypoint/v1", "status": "ready", "manifest_path": manifest_name,
        "manifest_sha256": manifest_hash, "entrypoint": entrypoint,
        "entrypoint_sha256": entry_hash,
    }))
    live_index_hash = _write(root / "viewer" / "live_index.json", json.dumps({"status": "starting", "frames": []}))
    _write(root / "viewer" / "live_head.json", json.dumps({"revision_count": 0,
        "live_index_sha256": live_index_hash}))
    return root, bootstrap_id, route_result["config_sha256"], config_path


def test_route_startup_resolves_hash_bound_config_and_refuses_existing_output(tmp_path):
    config_path, config = _route(tmp_path)
    output = tmp_path / "new-run"
    result = validate_route_startup(config_path, output, 20)
    assert result["config"]["route_id"] == "route-a"
    assert result["config_sha256"] == sha256_file(config_path)
    output.mkdir()
    with pytest.raises(FileExistsError, match="refusing existing output"):
        validate_route_startup(config_path, output, 20)


def test_route_startup_rejects_hash_mismatch_before_model_or_simulator(tmp_path):
    config_path, config = _route(tmp_path)
    config["scene_asset_sha256"] = "0" * 64
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(StartupContractError, match="scene_asset hash mismatch"):
        validate_route_startup(config_path, tmp_path / "new-run", 20)


def test_route_startup_accepts_consistent_v3_camera_pacing_policy(tmp_path):
    config_path, config = _route(tmp_path)
    policy = tmp_path / "policy-v3.json"
    policy_doc = {
        "schema": "smooth-route-t1-policy/v3", "simulator_step_s": 1.0 / 30.0,
        "camera_pacing": {"schema": "camera-pacing-policy/v1",
                           "mode": "monotonic_sim_time", "target_camera_fps": 30.0,
                           "real_time_factor": 1.0,
                           "overrun_policy": "record_and_continue_no_catchup"},
    }
    policy_hash = _write(policy, json.dumps(policy_doc))
    config["policy"] = policy_doc
    config["policy_path"] = str(policy)
    config["policy_sha256"] = policy_hash
    _write(config_path, json.dumps(config))
    resolved = validate_route_startup(config_path, tmp_path / "paced-run", 30)
    assert resolved["config"]["policy"]["camera_pacing"]["target_camera_fps"] == 30.0


def test_route_startup_rejects_inconsistent_v3_camera_step(tmp_path):
    config_path, config = _route(tmp_path)
    policy = tmp_path / "policy-v3.json"
    policy_doc = {
        "schema": "smooth-route-t1-policy/v3", "simulator_step_s": 0.1,
        "camera_pacing": {"schema": "camera-pacing-policy/v1",
                           "mode": "monotonic_sim_time", "target_camera_fps": 30.0,
                           "real_time_factor": 1.0,
                           "overrun_policy": "record_and_continue_no_catchup"},
    }
    policy_hash = _write(policy, json.dumps(policy_doc))
    config["policy"] = policy_doc
    config["policy_path"] = str(policy)
    config["policy_sha256"] = policy_hash
    _write(config_path, json.dumps(config))
    with pytest.raises(StartupContractError, match="simulator_step_s must equal"):
        validate_route_startup(config_path, tmp_path / "paced-run", 30)


def test_v4_effective_route_locks_habitat_dt_motion_and_derived_frame_budget(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config_path = root / "configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_v2.effective.json"
    result = validate_route_startup(config_path, tmp_path / "t4-v4-run", 1200)
    policy = result["config"]["policy"]
    assert policy["simulator_step_s"] == pytest.approx(0.04)
    assert policy["camera_pacing"]["target_camera_fps"] == pytest.approx(25.0)
    assert policy["motion_timebase"]["per_step_displacement_m"] == pytest.approx(0.008)
    assert policy["motion_timebase"]["per_step_yaw_deg"] == pytest.approx(1.2)
    budget = result["route_budget"]
    assert budget["estimated_camera_frames_including_initial"] == 291
    assert budget["conservative_camera_frame_upper_bound"] == 306
    assert budget["keyframe_candidate_upper_bound"] == 306


def test_v4_rejects_inconsistent_per_step_motion_before_capture(tmp_path):
    root = Path(__file__).resolve().parents[1]
    original = json.loads((root / "configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_v2.effective.json").read_text())
    policy = json.loads(json.dumps(original["policy"]))
    policy["motion_timebase"]["per_step_displacement_m"] = 0.01
    policy_path = tmp_path / "policy-v4.json"
    policy_hash = _write(policy_path, json.dumps(policy))
    original["policy"] = policy
    original["policy_path"] = str(policy_path)
    original["policy_sha256"] = policy_hash
    config_path = tmp_path / "effective-v4.json"
    _write(config_path, json.dumps(original))
    with pytest.raises(StartupContractError, match="per-step motion differs"):
        validate_route_startup(config_path, tmp_path / "new-run", 1200)


def test_v5_timebase_a_route_locks_nominal_vs_observed_contract_and_guards(tmp_path):
    root = Path(__file__).resolve().parents[1]
    config_path = root / "configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_timebaseA_v1.effective.json"
    prior_path = root / "configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_v2.effective.json"
    result = validate_route_startup(config_path, tmp_path / "t4-timebase-a-run", 1200)
    config = result["config"]
    prior = json.loads(prior_path.read_text())
    policy = config["policy"]
    clock = policy["motion_timebase"]
    assert policy["schema"] == "smooth-route-t1-policy/v5"
    assert clock["requested_simulator_dt_s"] == pytest.approx(0.04)
    assert clock["move_increment_m_per_action"] == pytest.approx(0.008)
    assert clock["turn_increment_deg_per_action"] == pytest.approx(1.2)
    assert clock["nominal_linear_speed_mps"] == pytest.approx(0.2)
    assert clock["nominal_angular_speed_deg_per_s"] == pytest.approx(30.0)
    assert clock["timestamp_authority"] == "habitat_sim.Simulator.get_world_time_after_step"
    assert clock["operational_stop_limits"]["max_simulation_elapsed_s"] == pytest.approx(60.0)
    assert result["route_budget"]["conservative_camera_frame_upper_bound"] == 306
    assert result["route_budget"]["configured_camera_frame_cap"] == 400
    assert config["route_points"] == prior["route_points"]
    assert config["selected_route_indices"] == prior["selected_route_indices"]
    assert config["sensor_contract"] == prior["sensor_contract"]
    assert config["resolution_px"] == [640, 480]
    assert policy["queue"] == prior["policy"]["queue"]
    assert policy["route_follow"] == prior["policy"]["route_follow"]
    assert policy["keyframes"] == prior["policy"]["keyframes"]


def test_v5_timebase_a_rejects_tolerance_or_speed_semantics_drift(tmp_path):
    root = Path(__file__).resolve().parents[1]
    original = json.loads((root / "configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_timebaseA_v1.effective.json").read_text())
    policy = json.loads(json.dumps(original["policy"]))
    policy["motion_timebase"]["deviation_policy"] = "widened_tolerance_to_48ms"
    policy_path = tmp_path / "policy-v5.json"
    policy_hash = _write(policy_path, json.dumps(policy))
    original["policy"] = policy
    original["policy_path"] = str(policy_path)
    original["policy_sha256"] = policy_hash
    config_path = tmp_path / "effective-v5.json"
    _write(config_path, json.dumps(original))
    with pytest.raises(StartupContractError, match="observed-delta policy"):
        validate_route_startup(config_path, tmp_path / "new-run", 1200)


def _v6_policy_from_locked_v5(v5_policy):
    policy = json.loads(json.dumps(v5_policy))
    policy["schema"] = "smooth-route-t1-policy/v6"
    policy["policy_id"] = "smooth_route_t4_geometry_contract_v6"
    policy.pop("agent_footprint", None)
    policy["route_follow"].pop("vertical_navmesh_tolerance_m", None)
    policy["geometry_contract"] = {
        "schema": "habitat-navmesh-clearance-contract/v1",
        "navmesh_baked_agent_radius_m": 0.1,
        "navmesh_baked_agent_height_m": 1.5,
        "runtime_agent_profile": {"radius_m": 0.1, "height_m": 1.5},
        "profile_compatibility_tolerance_m": 1e-6,
        "extra_navmesh_clearance_margin_m": 0.0,
        "numeric_boundary_tolerance_m": 0.0001,
        "numeric_boundary_tolerance_basis": "float32 PathFinder API; 0.1mm fail-closed band, 0.05m navmesh cell",
        "clearance_api": "PathFinder.distance_to_closest_obstacle",
        "clearance_unavailable_policy": "fail_closed",
        "boundary_policy": "fail_closed_within_margin_plus_numeric_tolerance",
        "max_search_radius_policy": "extra_margin_plus_2x_numeric_boundary_tolerance",
        "vertical_navmesh_tolerance_m": 0.05,
    }
    return policy


def test_v6_route_startup_accepts_explicit_geometry_contract_and_timebase(tmp_path):
    root = Path(__file__).resolve().parents[1]
    original = json.loads((root / "configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_timebaseA_v1.effective.json").read_text())
    policy = _v6_policy_from_locked_v5(original["policy"])
    policy_path = tmp_path / "policy-v6.json"
    original["policy"] = policy
    original["policy_path"] = str(policy_path)
    original["policy_sha256"] = _write(policy_path, json.dumps(policy))
    config_path = tmp_path / "effective-v6.json"
    _write(config_path, json.dumps(original))
    result = validate_route_startup(config_path, tmp_path / "new-run", 1200)
    assert result["config"]["policy"]["schema"] == "smooth-route-t1-policy/v6"
    assert result["route_budget"]["configured_camera_frame_cap"] == 400


@pytest.mark.parametrize("field,value", [
    (("runtime_agent_profile", "radius_m"), 0.08),
    (("navmesh_baked_agent_radius_m",), 0.08),
    (("extra_navmesh_clearance_margin_m",), -0.01),
    (("numeric_boundary_tolerance_m",), 0.0),
])
def test_v6_route_startup_rejects_geometry_contract_drift(tmp_path, field, value):
    root = Path(__file__).resolve().parents[1]
    original = json.loads((root / "configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_timebaseA_v1.effective.json").read_text())
    policy = _v6_policy_from_locked_v5(original["policy"])
    target = policy["geometry_contract"]
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = value
    policy_path = tmp_path / "policy-v6.json"
    original["policy"] = policy
    original["policy_path"] = str(policy_path)
    original["policy_sha256"] = _write(policy_path, json.dumps(policy))
    config_path = tmp_path / "effective-v6.json"
    _write(config_path, json.dumps(original))
    with pytest.raises(StartupContractError):
        validate_route_startup(config_path, tmp_path / "new-run", 1200)


def test_exact_current_bootstrap_inventory_reaches_capture_boundary(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    result = validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
        expected_route_config_sha256=config_sha, expected_ready_pids={k: 1234 for k in EXPECTED_WORKERS})
    assert result["status"] == "capture_boundary_ready"
    assert result["camera_started"] is False
    assert result["capture_directory_exists"] is False


def test_validated_viewer_start_signal_is_allowed_at_capture_boundary(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    pointer_path = root / "viewer/d2_viewer_entrypoint.json"
    pointer = json.loads(pointer_path.read_text())
    url = "http://127.0.0.1:8777/camera-fixture-v1/viewer/index.html?live=1"
    start = {
        "schema": "d1-explicit-viewer-start/v1", "run_id": root.name,
        "confirmed_browser_ready": True,
        "entrypoint_pointer_sha256": sha256_file(pointer_path),
        "entrypoint": pointer["entrypoint"], "url": url,
        "started_by": "camera-live-pilot-confirm-browser-ready",
        "started_utc": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    }
    start_path = root / "control/start.json"
    start_sha = _write(start_path, json.dumps(start))
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["viewer"] = {"url": url, "entrypoint": pointer["entrypoint"],
                          "browser_ready_start_signal_sha256": start_sha}
    _write(manifest_path, json.dumps(manifest))
    with (root / "execution_ledger.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"schema":"camera-live-execution-event/v1",
            "event":"browser_ready_confirmed","run_id":root.name,
            "start_signal_sha256":start_sha})+"\n")

    result = validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
        expected_route_config_sha256=config_sha, expected_ready_pids={k: 1234 for k in EXPECTED_WORKERS})
    assert result["status"] == "capture_boundary_ready"
    assert result["camera_started"] is False
    assert result["capture_directory_exists"] is False


def test_production_confirmation_helper_creates_valid_marker_before_route_validator(tmp_path, monkeypatch):
    import scripts.build_d2_xyz_viewer as viewer_builder
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    pointer=json.loads((root/"viewer/d2_viewer_entrypoint.json").read_text())
    url=f"http://127.0.0.1:8777/{root.name}/viewer/{pointer['entrypoint']}?live=1"
    manifest_path=root/"run_manifest.json"
    manifest=json.loads(manifest_path.read_text())
    manifest.update({"status":"waiting_for_viewer_ready","viewer":{"url":url,"entrypoint":pointer["entrypoint"],
        "waiting_for_first_revision":True}})
    _write(manifest_path,json.dumps(manifest))
    monkeypatch.setattr(viewer_builder,"validate_served_entrypoint",
        lambda _viewer,_url:{"pointer":pointer})
    result=confirm_browser_ready(root,root.name)
    manifest=json.loads(manifest_path.read_text())
    manifest["status"]="started"
    manifest["viewer"]["browser_ready_start_signal_sha256"]=result["start_signal_sha256"]
    mark_camera_runner_spawned(manifest,pid=9876)
    _write(manifest_path,json.dumps(manifest))
    first=validate_bootstrap_before_capture(root,expected_bootstrap_id=bootstrap_id,
        expected_route_config_sha256=config_sha,expected_ready_pids={k:1234 for k in EXPECTED_WORKERS})
    assert first["status"]=="capture_boundary_ready"
    confirm_browser_ready(root,root.name)
    rows=[json.loads(x) for x in (root/"execution_ledger.jsonl").read_text().splitlines() if x.strip()]
    assert sum(row.get("event")=="browser_ready_confirmed" for row in rows)==1


def test_camera_runner_spawn_preserves_started_state_until_actual_frame():
    manifest={"status":"started","run_id":"pilot-fixture","route_run_id":"route-fixture",
        "sequence_id":"sequence-fixture","bootstrap_id":"bootstrap-fixture",
        "effective_route_config_sha256":"a"*64,
        "stages":{"camera_capture":"not_started","perception":"not_started","mappers":"not_started"}}
    mark_camera_runner_spawned(manifest,pid=7654)
    assert manifest["status"]=="started"
    assert manifest["camera_runner"]["capture_has_started"] is False
    assert manifest["stages"]["camera_capture"]=="not_started"
    startup_event={"event":"camera_startup_validated","event_owner":"habitat_route_runner",
        "pilot_run_id":"pilot-fixture","route_run_id":"route-fixture","sequence_id":"sequence-fixture",
        "bootstrap_id":"bootstrap-fixture","effective_route_config_sha256":"a"*64,
        "event_monotonic_s":manifest["camera_runner"]["spawned_monotonic_s"]+0.01,
        "clock_domain":manifest["camera_runner"]["clock_domain"]}
    mark_camera_startup_validated(manifest,startup_event,pid=7654)
    assert manifest["status"]=="started"
    assert manifest["camera_runner"]["first_frame_monotonic_s"] is None
    frame_event={"event":"camera_frame","event_owner":"habitat_route_runner",
        "frame_id":"seq-cam-000000","frame_index":0,
        "capture_monotonic_s":startup_event["event_monotonic_s"]+0.01,
        "capture_monotonic_domain":startup_event["clock_domain"],
        "timestamp_s":0.0,"timestamp_clock_domain":"habitat_simulation"}
    mark_camera_capture_started(manifest,frame_event,pid=7654)
    assert manifest["status"]=="running"
    assert manifest["camera_runner"]["first_frame_index"]==0
    assert manifest["stages"]["camera_capture"]=="running"


def test_camera_capture_cannot_transition_from_non_frame_event():
    manifest={"status":"started","stages":{}}
    mark_camera_runner_spawned(manifest,pid=7654)
    with pytest.raises(StartupContractError,match="camera_frame event"):
        mark_camera_capture_started(manifest,{"event":"keyframe_persisted","frame_id":"seq-cam-000000",
            "frame_index":0},pid=7654)


def test_camera_frame_is_rejected_until_route_startup_validation():
    manifest={"status":"started","run_id":"pilot","route_run_id":"route",
        "sequence_id":"seq","bootstrap_id":"boot","effective_route_config_sha256":"a"*64}
    mark_camera_runner_spawned(manifest,pid=987)
    frame_event={"event":"camera_frame","event_owner":"habitat_route_runner",
        "frame_id":"seq-cam-000000","frame_index":0,"capture_monotonic_s":time.monotonic()+1,
        "capture_monotonic_domain":manifest["camera_runner"]["clock_domain"],
        "timestamp_s":0,"timestamp_clock_domain":"habitat_simulation"}
    with pytest.raises(StartupContractError,match="before route startup validation"):
        mark_camera_capture_started(manifest,frame_event,pid=987)


def test_old_start_marker_is_rejected_after_run_creation(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    pointer_path=root/"viewer/d2_viewer_entrypoint.json"
    pointer=json.loads(pointer_path.read_text())
    manifest=json.loads((root/"run_manifest.json").read_text())
    url="http://127.0.0.1:8777/camera-fixture-v1/viewer/index_d2_fixture.html?live=1"
    created=datetime.fromisoformat(manifest["created_utc"].replace("Z","+00:00"))
    marker={"schema":"d1-explicit-viewer-start/v1","run_id":root.name,
        "confirmed_browser_ready":True,"entrypoint_pointer_sha256":sha256_file(pointer_path),
        "entrypoint":pointer["entrypoint"],"url":url,
        "started_by":"camera-live-pilot-confirm-browser-ready",
        "started_utc":(created-timedelta(seconds=1)).isoformat()}
    marker_sha=_write(root/"control/start.json",json.dumps(marker))
    manifest["viewer"]={"url":url,"browser_ready_start_signal_sha256":marker_sha}
    _write(root/"run_manifest.json",json.dumps(manifest))
    with (root/"execution_ledger.jsonl").open("a",encoding="utf-8") as stream:
        stream.write(json.dumps({"schema":"camera-live-execution-event/v1",
            "event":"browser_ready_confirmed","run_id":root.name,
            "start_signal_sha256":marker_sha})+"\n")
    with pytest.raises(StartupContractError,match="stale for this pilot run"):
        validate_bootstrap_before_capture(root,expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_start_marker_with_wrong_pointer_hash_is_rejected(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    pointer_path=root/"viewer/d2_viewer_entrypoint.json"
    pointer=json.loads(pointer_path.read_text())
    manifest=json.loads((root/"run_manifest.json").read_text())
    url="http://127.0.0.1:8777/camera-fixture-v1/viewer/index_d2_fixture.html?live=1"
    marker={"schema":"d1-explicit-viewer-start/v1","run_id":root.name,
        "confirmed_browser_ready":True,"entrypoint_pointer_sha256":"0"*64,
        "entrypoint":pointer["entrypoint"],"url":url,
        "started_by":"camera-live-pilot-confirm-browser-ready",
        "started_utc":(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()}
    marker_sha=_write(root/"control/start.json",json.dumps(marker))
    manifest["viewer"]={"url":url,"browser_ready_start_signal_sha256":marker_sha}
    _write(root/"run_manifest.json",json.dumps(manifest))
    with (root/"execution_ledger.jsonl").open("a",encoding="utf-8") as stream:
        stream.write(json.dumps({"schema":"camera-live-execution-event/v1",
            "event":"browser_ready_confirmed","run_id":root.name,
            "start_signal_sha256":marker_sha})+"\n")
    with pytest.raises(StartupContractError,match="does not match this run's validated D2 entrypoint"):
        validate_bootstrap_before_capture(root,expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


@pytest.mark.parametrize("schedule",["coordinator_write_before_child_read","child_waits_while_coordinator_writes"])
def test_route_validator_is_stable_across_controlled_schedule_orders(tmp_path,monkeypatch,schedule):
    import threading
    import scripts.build_d2_xyz_viewer as viewer_builder
    root,bootstrap_id,config_sha,_=_live_root(tmp_path)
    pointer=json.loads((root/"viewer/d2_viewer_entrypoint.json").read_text())
    url=f"http://127.0.0.1:8777/{root.name}/viewer/{pointer['entrypoint']}?live=1"
    manifest_path=root/"run_manifest.json"
    manifest=json.loads(manifest_path.read_text())
    manifest.update({"status":"waiting_for_viewer_ready","viewer":{"url":url,
        "entrypoint":pointer["entrypoint"],"waiting_for_first_revision":True}})
    _write(manifest_path,json.dumps(manifest))
    monkeypatch.setattr(viewer_builder,"validate_served_entrypoint",lambda _v,_u:{"pointer":pointer})
    confirmed=confirm_browser_ready(root,root.name)
    manifest=json.loads(manifest_path.read_text())
    manifest.update({"status":"started","viewer":{"url":url,"entrypoint":pointer["entrypoint"],
        "browser_ready_start_signal_sha256":confirmed["start_signal_sha256"]}})
    if schedule=="coordinator_write_before_child_read":
        _write(manifest_path,json.dumps(manifest))
        result=validate_bootstrap_before_capture(root,expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)
    else:
        child_ready=threading.Event(); allow_read=threading.Event(); results=[]; errors=[]
        def child_validate():
            child_ready.set()
            if not allow_read.wait(timeout=2):
                errors.append(TimeoutError("child read synchronization timed out")); return
            try:
                results.append(validate_bootstrap_before_capture(root,
                    expected_bootstrap_id=bootstrap_id,expected_route_config_sha256=config_sha))
            except BaseException as exc:
                errors.append(exc)
        child=threading.Thread(target=child_validate)
        child.start()
        assert child_ready.wait(timeout=2)
        _write(manifest_path,json.dumps(manifest))
        allow_read.set(); child.join(timeout=3)
        assert not child.is_alive()
        assert not errors
        result=results[0]
    assert result["status"]=="capture_boundary_ready"
    assert json.loads(manifest_path.read_text())["status"]=="started"


def test_preflight_stopped_is_terminal_before_any_camera_frame():
    manifest={"status":"started"}
    mark_camera_runner_spawned(manifest,pid=987)
    event={"event":"camera_startup_validated","event_owner":"habitat_route_runner",
        "pilot_run_id":"pilot","route_run_id":"route","sequence_id":"seq",
        "bootstrap_id":"boot","effective_route_config_sha256":"a"*64,
        "event_monotonic_s":manifest["camera_runner"]["spawned_monotonic_s"]+0.01,
        "clock_domain":manifest["camera_runner"]["clock_domain"]}
    manifest.update({"run_id":"pilot","route_run_id":"route","sequence_id":"seq",
        "bootstrap_id":"boot","effective_route_config_sha256":"a"*64})
    mark_camera_startup_validated(manifest,event,pid=987)
    mark_camera_runner_preflight_stopped(manifest,returncode=0)
    assert manifest["camera_runner"]["status"]=="preflight_validated_stopped"
    assert manifest["camera_runner"]["capture_has_started"] is False


def test_post_spawn_failure_has_terminal_stage_states_and_preserves_main_error():
    manifest={"status":"started","stages":{"camera_capture":"pending",
        "perception":"running","mappers":"running","terminal":"pending"}}
    mark_camera_runner_spawned(manifest,pid=987)
    mark_camera_runner_failure(manifest,status="failed_before_capture",
        error="child exited before capture",returncode=3)
    stages=terminal_stage_statuses(manifest["stages"],camera_started=False,
        keyframes_consumed=0,mapper_dispatched=0,unified_revisions=0,
        terminal_started=False,run_status="failed_before_capture",
        camera_runner_spawned=True,camera_startup_validated=False)
    assert manifest["camera_runner"]["terminal_error"]=="child exited before capture"
    assert stages["camera_capture"]=="not_started"
    assert stages["perception"]=="not_started"
    assert stages["mappers"]=="not_started"
    assert stages["terminal"]=="not_run"


def test_cancelled_before_first_frame_keeps_source_times_null():
    manifest={"status":"started","stages":{"camera_capture":"pending"}}
    mark_camera_runner_spawned(manifest,pid=321)
    mark_camera_runner_failure(manifest,status="cancelled_before_capture",
        error="cancel requested before first frame",returncode=-2)
    runner=manifest["camera_runner"]
    assert runner["status"]=="cancelled_before_capture"
    assert runner["spawned_monotonic_s"] is not None
    assert runner["startup_validated_monotonic_s"] is None
    assert runner["first_frame_monotonic_s"] is None
    assert runner["first_frame_timestamp_s"] is None
    assert runner["capture_finished_monotonic_s"] is None
    assert runner["capture_completed"] is False


def test_cleanup_stops_only_the_owned_child_process():
    from scripts.run_camera_live_pilot import _stop_owned_processes
    child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"])
    rows=_stop_owned_processes([child],timeout_s=1)
    assert child.poll() is not None
    assert rows and rows[0]["pid"]==child.pid
    assert rows[0]["status"] in {"exited","killed_after_timeout"}


def test_unbound_viewer_start_signal_is_rejected_before_capture(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    (root / "control/start.json").write_text(json.dumps({
        "schema": "d1-explicit-viewer-start/v1", "run_id": "another-run",
        "confirmed_browser_ready": True,
    }))
    with pytest.raises(StartupContractError, match="unexpected pre-capture control contents|explicit viewer start signal"):
        validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_start_signal_with_unexpected_content_is_rejected_before_capture(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    pointer_path=root/"viewer/d2_viewer_entrypoint.json"
    pointer=json.loads(pointer_path.read_text())
    url="http://127.0.0.1:8777/camera-fixture-v1/viewer/index.html?live=1"
    start={"schema":"d1-explicit-viewer-start/v1","run_id":root.name,
        "confirmed_browser_ready":True,"entrypoint_pointer_sha256":sha256_file(pointer_path),
        "entrypoint":pointer["entrypoint"],"url":url,
        "started_by":"camera-live-pilot-confirm-browser-ready",
        "started_utc":(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(),"unexpected":"must reject"}
    start_sha=_write(root/"control/start.json",json.dumps(start))
    manifest=json.loads((root/"run_manifest.json").read_text())
    manifest["viewer"]={"url":url,"entrypoint":pointer["entrypoint"],
                         "browser_ready_start_signal_sha256":start_sha}
    _write(root/"run_manifest.json",json.dumps(manifest))
    with (root/"execution_ledger.jsonl").open("a",encoding="utf-8") as stream:
        stream.write(json.dumps({"schema":"camera-live-execution-event/v1",
            "event":"browser_ready_confirmed","run_id":root.name,
            "start_signal_sha256":start_sha})+"\n")
    with pytest.raises(StartupContractError, match="unexpected/missing fields"):
        validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_cancellation_before_start_is_terminal_without_capture(tmp_path):
    from integration_v2.live_processing import _wait_for_explicit_viewer_start
    root, _, _, _ = _live_root(tmp_path)
    (root/"control/stop.json").write_text(json.dumps({"reason":"cancelled_before_start"}))
    with pytest.raises(InterruptedError,match="cancelled_before_start"):
        _wait_for_explicit_viewer_start(root,root.name,"pointer-hash",timeout_s=1)


def test_cancellation_after_ready_is_rejected_before_route_validation(tmp_path, monkeypatch):
    import scripts.build_d2_xyz_viewer as viewer_builder
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    pointer=json.loads((root/"viewer/d2_viewer_entrypoint.json").read_text())
    url=f"http://127.0.0.1:8777/{root.name}/viewer/{pointer['entrypoint']}?live=1"
    manifest_path=root/"run_manifest.json"
    manifest=json.loads(manifest_path.read_text())
    manifest.update({"status":"waiting_for_viewer_ready","viewer":{"url":url,"entrypoint":pointer["entrypoint"]}})
    _write(manifest_path,json.dumps(manifest))
    monkeypatch.setattr(viewer_builder,"validate_served_entrypoint",lambda _v,_u:{"pointer":pointer})
    result=confirm_browser_ready(root,root.name)
    manifest=json.loads(manifest_path.read_text())
    manifest["status"]="started"
    manifest["viewer"]["browser_ready_start_signal_sha256"]=result["start_signal_sha256"]
    _write(manifest_path,json.dumps(manifest))
    (root/"control/stop.json").write_text(json.dumps({"reason":"cancelled_after_readiness"}))
    with pytest.raises(StartupContractError,match="unexpected pre-capture control contents"):
        validate_bootstrap_before_capture(root,expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_other_run_readiness_marker_is_rejected(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    marker = json.loads((root / "ready/p2.json").read_text())
    marker["run_id"] = "another-run"
    (root / "ready/p2.json").write_text(json.dumps(marker))
    with pytest.raises(StartupContractError, match="belongs to another run"):
        validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


@pytest.mark.parametrize("relative", ["ready/unexpected.tmp", "control/stop.json"])
def test_unexpected_ready_or_control_content_is_reported(tmp_path, relative):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    (root / relative).write_text("unexpected", encoding="utf-8")
    with pytest.raises(StartupContractError, match="ready/ contents mismatch|control contents"):
        validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_prior_capture_is_always_rejected(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    (root / "capture").mkdir()
    with pytest.raises(StartupContractError, match="capture/ already exists"):
        validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_preexisting_camera_producer_manifest_is_rejected_before_capture(tmp_path):
    root, bootstrap_id, config_sha, _ = _live_root(tmp_path)
    (root / CAMERA_PRODUCER_MANIFEST).write_text('{"status":"old"}', encoding="utf-8")
    with pytest.raises(StartupContractError, match="producer manifest already exists"):
        validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_config_hash_change_after_bootstrap_is_rejected(tmp_path):
    root, bootstrap_id, config_sha, config_path = _live_root(tmp_path)
    config_path.write_text(config_path.read_text() + " ", encoding="utf-8")
    with pytest.raises(StartupContractError, match="config changed after coordinator preflight"):
        validate_bootstrap_before_capture(root, expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=config_sha)


def test_producer_manifest_uses_sidecar_without_clobbering_pilot_manifest(tmp_path):
    root = tmp_path / "pilot"
    root.mkdir()
    pilot_manifest = root / "run_manifest.json"
    pilot_manifest.write_text('{"run_id":"pilot"}', encoding="utf-8")
    target = resolve_camera_producer_manifest_path(
        root, root / CAMERA_PRODUCER_MANIFEST, allow_existing_pilot_root=True)
    assert target == root / CAMERA_PRODUCER_MANIFEST
    assert json.loads(pilot_manifest.read_text(encoding="utf-8"))["run_id"] == "pilot"
    with pytest.raises(StartupContractError, match="separate owners"):
        resolve_camera_producer_manifest_path(
            root, pilot_manifest, allow_existing_pilot_root=True)
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        resolve_camera_producer_manifest_path(
            root, target, allow_existing_pilot_root=True)


@pytest.mark.parametrize("status", ["completed", "failed", "failed_before_capture", "cancelled",
                                     "cancelled_before_capture", "startup_preflight_passed", "startup_preflight_failed"])
def test_pilot_and_bootstrap_reach_same_terminal_status_without_replacing_route_id(tmp_path, status):
    root, _, _, _ = _live_root(tmp_path)
    pilot = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    bootstrap = json.loads((root / "pilot_bootstrap.json").read_text(encoding="utf-8"))
    pilot_id = pilot["run_id"]
    route_id = pilot["route_run_id"]
    pilot, bootstrap = terminalize_pilot_documents(
        pilot, bootstrap, status=status, finished_utc="2026-10-18T12:00:00+00:00")
    assert pilot["run_id"] == pilot["pilot_run_id"] == bootstrap["run_id"] == pilot_id
    assert pilot["route_run_id"] == bootstrap["route_run_id"] == route_id
    assert pilot["status"] == bootstrap["status"] == status
    assert pilot["terminal"] is bootstrap["terminal"] is True
    assert pilot["finished_utc"] == bootstrap["finished_utc"]


def test_lifecycle_rejects_route_id_collision_and_conflicting_terminal_transition(tmp_path):
    root, _, _, _ = _live_root(tmp_path)
    pilot = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    bootstrap = json.loads((root / "pilot_bootstrap.json").read_text(encoding="utf-8"))
    pilot["route_run_id"] = "wrong-route"
    with pytest.raises(StartupContractError, match="route_run_id"):
        terminalize_pilot_documents(pilot, bootstrap, status="failed", finished_utc="now")
    pilot["route_run_id"] = bootstrap["route_run_id"]
    terminalize_pilot_documents(pilot, bootstrap, status="completed", finished_utc="then")
    with pytest.raises(StartupContractError, match="terminal lifecycle transition"):
        terminalize_pilot_documents(pilot, bootstrap, status="failed", finished_utc="later")


def test_pre_capture_failure_does_not_leave_processing_stages_running():
    stages=terminal_stage_statuses({"viewer_bootstrap":"browser_ready_confirmed",
        "worker_start":"all_workers_ready_before_camera","camera_capture":"pending",
        "perception":"running","mappers":"running","terminal":"pending"},
        camera_started=False,keyframes_consumed=0,mapper_dispatched=0,
        unified_revisions=0,terminal_started=False,run_status="failed_before_capture")
    assert stages["worker_start"]=="workers_ready_then_stopped"
    assert stages["camera_capture"]=="not_started"
    assert stages["perception"]=="not_started"
    assert stages["mappers"]=="not_started"
    assert stages["terminal"]=="not_run"
