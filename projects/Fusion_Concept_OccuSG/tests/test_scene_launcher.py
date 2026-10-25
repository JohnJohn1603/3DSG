from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import scene_launcher as sl  # noqa: E402


def test_registry_is_strict_and_current_entries_validate():
    doc = sl.load_registry()
    sl.validate_registry(doc)
    assert {entry["alias"] for entry in doc["scenes"]} == {"hm3d_00802", "replica_apt0"}


def test_duplicate_v2_reference_is_selected_by_verified_cache_hashes():
    cache = ROOT / "artifacts/runs/u6_hm3d_00808_quality_v1_20261010"
    reference = sl.find_duplicate_v2_reference(cache)
    assert reference == (ROOT / "artifacts/runs/u3_same_frame_dup_candidate_v2_20261011_v2/00808/assignments_candidate_v2.jsonl").resolve()


def test_duplicate_v2_reference_absence_is_not_a_run_blocker(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    (cache / "capture").mkdir(parents=True)
    (cache / "detection_2d").mkdir()
    (cache / "detection_3d").mkdir()
    (cache / "capture/capture_manifest.json").write_text("{}\n")
    (cache / "detection_2d/detection_2d_index.json").write_text("{}\n")
    (cache / "detection_3d/detection_3d.jsonl").write_text("")
    monkeypatch.setattr(sl, "RUNS", tmp_path / "empty-runs")
    assert sl.find_duplicate_v2_reference(cache) is None


def test_compare_profiles_is_exposed_in_launcher_help(capsys):
    assert "compare-profiles" in sl.COMMANDS
    launcher = sl.Launcher()
    launcher.handle(["help", "compare-profiles"])
    assert "--candidate geometry-only-dedup-v2" in capsys.readouterr().out


def test_unified_graph_import_is_opt_in_and_exposed_in_launcher_help(capsys):
    assert "unified-graph" in sl.COMMANDS
    launcher = sl.Launcher()
    launcher.handle(["help", "unified-graph"])
    output = capsys.readouterr().out
    assert "--room-snapshot" in output and "--object-export" in output
    assert "fixture|legacy|reference" in output


def test_hybrid_map_is_opt_in_and_exposed_in_launcher_help(capsys):
    assert "hybrid-map" in sl.COMMANDS
    sl.Launcher().handle(["help", "hybrid-map"])
    output = capsys.readouterr().out
    assert "--cache" in output and "--text-features" in output and "--room-snapshot" in output


def _write_d2_pointer(run_root: Path, entrypoint: str = "index_xyz_build42.html") -> None:
    viewer = run_root / "viewer"
    viewer.mkdir(parents=True, exist_ok=True)
    page = viewer / entrypoint
    renderer = viewer / "renderer.js"
    renderer.write_text("window.createXyzViewer = () => ({});\n", encoding="utf-8")
    page.write_text('<!doctype html><script src="./renderer.js"></script><title>XYZ</title>', encoding="utf-8")
    manifest = {"status": "completed", "viewer_version": "build42",
                "viewer_entrypoint": entrypoint,
                "output_files_sha256": {entrypoint: hashlib.sha256(page.read_bytes()).hexdigest(),
                                         "renderer.js": hashlib.sha256(renderer.read_bytes()).hexdigest()}}
    manifest_path = viewer / "d2_xyz_manifest_build42.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pointer = {"schema": "d2-viewer-entrypoint/v1", "status": "completed",
               "manifest_path": manifest_path.name,
               "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
               "entrypoint": entrypoint,
               "entrypoint_sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
               "viewer_version": "build42"}
    (viewer / "d2_viewer_entrypoint.json").write_text(json.dumps(pointer), encoding="utf-8")


def test_d1_results_prints_manifest_selected_d2_entrypoint(tmp_path, monkeypatch, capsys):
    run_root = tmp_path / "artifacts/live_processing_runs/d1_fixture"
    run_root.mkdir(parents=True)
    _write_d2_pointer(run_root)
    manifest_path = run_root / "run_manifest.json"
    manifest_path.write_text(json.dumps({
        "status": "completed", "run_id": "d1_fixture", "execution_mode": "fixture",
        "scene_id": "replica-x", "sequence_id": "route-x", "joined_event_count": 2,
        "viewer": {"port": 8765, "url": "http://127.0.0.1:8765/old"},
    }), encoding="utf-8")
    (run_root / "run_manifest.sha256.json").write_text(json.dumps({
        "schema": "d1-run-manifest-hash-receipt/v1", "manifest": "run_manifest.json",
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    monkeypatch.setattr(sl, "ROOT", tmp_path)
    sl.Launcher().cmd_results(["d1_fixture"])
    output = capsys.readouterr().out
    assert str(run_root / "viewer/index_xyz_build42.html") in output
    assert "d2_xyz_manifest_build42.json" in output
    assert "Root run manifest SHA receipt: verified" in output
    assert "Viewer URL (pointer-selected): http://127.0.0.1:8765/artifacts/live_processing_runs/d1_fixture/viewer/index_xyz_build42.html?live=1" in output
    assert "./run_scene.sh open d1_fixture live --port 8765" in output


def test_d1_results_rejects_root_manifest_receipt_mismatch(tmp_path, monkeypatch):
    run_root = tmp_path / "artifacts/live_processing_runs/d1_fixture"
    run_root.mkdir(parents=True)
    _write_d2_pointer(run_root)
    manifest_path = run_root / "run_manifest.json"
    manifest_path.write_text(json.dumps({
        "status": "completed", "run_id": "d1_fixture", "execution_mode": "fixture",
        "scene_id": "replica-x", "sequence_id": "route-x", "joined_event_count": 2,
        "viewer": {"port": 8765, "url": "http://127.0.0.1:8765/old"},
    }), encoding="utf-8")
    (run_root / "run_manifest.sha256.json").write_text(json.dumps({
        "schema": "d1-run-manifest-hash-receipt/v1", "manifest": "run_manifest.json",
        "sha256": "0" * 64,
    }), encoding="utf-8")
    monkeypatch.setattr(sl, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="root manifest hash receipt mismatch"):
        sl.Launcher().cmd_results(["d1_fixture"])


def test_results_resolves_c2_run_by_manifest_schema_without_c2_prefix(tmp_path, monkeypatch, capsys):
    run_id = "hm3d00802_main29_640_quality_v1_attempt2"
    run_root = tmp_path / "artifacts/conceptgraphs_hybrid_runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "c2_run_manifest.json").write_text(json.dumps({
        "schema": "scene-graph-c2-fresh-replica-run/v1", "status": "completed",
        "run_id": run_id, "execution_mode": "fresh_replica", "scene_id": "00802-test",
        "sequence_id": "main-route", "summary": {"frame_count": 29,
            "final_conceptgraphs_objects": 6, "room_count": 5,
            "membership_status": "unavailable_elevation_scope",
            "room_semantics_status": "unavailable_membership_scope"},
        "outputs": {"unified_graph": "graph.json", "viewer": "viewer.html"},
    }), encoding="utf-8")
    monkeypatch.setattr(sl, "HYBRID_MAPPINGS", tmp_path / "artifacts/conceptgraphs_hybrid_runs")
    monkeypatch.setattr(sl, "ROOT", tmp_path)

    sl.Launcher().cmd_results([run_id])

    output = capsys.readouterr().out
    assert f"Run ID: {run_id} | Status: completed" in output
    assert "Scene/sequence: 00802-test / main-route | Frames: 29" in output
    assert f"./run_scene.sh open {run_id} hybrid" in output


def test_results_rejects_c2_manifest_with_wrong_schema_or_run_id(tmp_path, monkeypatch):
    run_id = "custom_run_name"
    run_root = tmp_path / "artifacts/conceptgraphs_hybrid_runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "c2_run_manifest.json").write_text(json.dumps({
        "schema": "not-a-c2-manifest/v1", "status": "completed", "run_id": run_id,
    }), encoding="utf-8")
    monkeypatch.setattr(sl, "HYBRID_MAPPINGS", tmp_path / "artifacts/conceptgraphs_hybrid_runs")
    monkeypatch.setattr(sl, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="C2 manifest schema/run ID mismatch"):
        sl.Launcher().cmd_results([run_id])


def test_d1_open_uses_d2_manifest_entrypoint_and_leaves_server_untouched(tmp_path, monkeypatch, capsys):
    import urllib.request

    run_root = tmp_path / "artifacts/live_processing_runs/d1_fixture"
    viewer = run_root / "viewer"
    viewer.mkdir(parents=True)
    (viewer / "index.html").write_text("<!doctype html><title>XY</title>", encoding="utf-8")
    _write_d2_pointer(run_root)
    (run_root / "run_manifest.json").write_text(json.dumps({
        "status": "completed", "run_id": "d1_fixture", "execution_mode": "fixture",
        "scene_id": "replica-x", "sequence_id": "route-x",
        "viewer": {"index": "artifacts/live_processing_runs/d1_fixture/viewer/index.html",
                   "port": 8765, "url": "http://127.0.0.1:8765/artifacts/live_processing_runs/d1_fixture/viewer/index.html?live=1",
                   "server_mode": "existing_read_only_loopback_server"},
    }), encoding="utf-8")
    monkeypatch.setattr(sl, "ROOT", tmp_path)
    served = []

    class Response:
        def __init__(self, status): self.status = status
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _size=-1): return b"<!doctype html><title>XYZ</title>"

    def fake_urlopen(url, **_kwargs):
        served.append(url)
        return Response(404 if "/artifacts/" in url else 200)

    opened = []
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sl.subprocess, "Popen", lambda command, **_kwargs: opened.append(command))
    assert sl.Launcher()._open_live_timeline("d1_fixture", 8765) == 0
    expected = "http://127.0.0.1:8765/viewer/index_xyz_build42.html?live=1"
    assert served == [
        "http://127.0.0.1:8765/artifacts/live_processing_runs/d1_fixture/viewer/index_xyz_build42.html?live=1",
        expected,
    ]
    assert opened and opened[0][0] == "xdg-open" and opened[0][1] == expected
    output = capsys.readouterr().out
    assert expected in output and "did not replace or stop" in output


def test_duplicate_alias_is_rejected():
    doc = sl.load_registry()
    doc["scenes"].append(dict(doc["scenes"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        sl.validate_registry(doc)


def test_route_config_is_bound_to_selected_scene_and_unknown_route_fails():
    entry = sl.scene_entry("hm3d_00802")
    context = sl._scene_context(entry, "smoke")
    assert context["route"]["scene_id"] == context["dataset"]["scene_id"]
    with pytest.raises(ValueError, match="route"):
        sl._scene_context(entry, "apt0")


def test_effective_profile_tracks_smoke_route_sensor_and_dataset_paths(tmp_path):
    launcher = sl.Launcher()
    entry = sl.scene_entry("hm3d_00802", launcher.registry)
    profile_path, temp = launcher._temp_profile(entry, "smoke")
    try:
        profile = sl.read_json(profile_path)
        assert profile["route_config"].endswith("hm3d_minival_v02_00802_smoke.json")
        route = sl.read_json(sl.resolve_path(entry["routes"]["smoke"]))
        assert profile["dataset"]["scene_dataset_config"] == str(sl.resolve_path(route["scene_dataset_config"]))
        assert profile["sensor"]["resolution_px"] == [160, 120]
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def test_main_320_route_preserves_main_observations_and_changes_only_resolution():
    entry = sl.scene_entry("hm3d_00802")
    main = sl.read_json(sl.resolve_path(entry["routes"]["main"]))
    high = sl.read_json(sl.resolve_path(entry["routes"]["main_320"]))
    assert entry["default_route"] == "main"
    assert high["resolution_px"] == [320, 240]
    assert main["resolution_px"] == [160, 120]
    assert len(main["route"]) == len(high["route"]) == 29
    allowed = {"resolution_px", "route_policy", "comparison_baseline_run_id"}
    assert {k: v for k, v in main.items() if k not in allowed} == {
        k: v for k, v in high.items() if k not in allowed
    }


def test_effective_profile_tracks_main_320_resolution():
    launcher = sl.Launcher()
    entry = sl.scene_entry("hm3d_00802", launcher.registry)
    profile_path, temp = launcher._temp_profile(entry, "main_320")
    try:
        profile = sl.read_json(profile_path)
        assert profile["sensor"]["resolution_px"] == [320, 240]
        assert profile["route_config"].endswith("hm3d_minival_v02_00802_main_320_route.json")
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def test_main_600_route_uses_same_29_poses_and_only_changes_sensor_resolution():
    entry = sl.scene_entry("hm3d_00802")
    main = sl.read_json(sl.resolve_path(entry["routes"]["main"]))
    high = sl.read_json(sl.resolve_path(entry["routes"]["main_600"]))
    assert high["resolution_px"] == [600, 340]
    assert len(high["route"]) == len(main["route"]) == 29
    allowed = {"resolution_px", "route_policy"}
    assert {k: v for k, v in main.items() if k not in allowed} == {
        k: v for k, v in high.items() if k not in allowed
    }


def test_effective_profile_tracks_main_600_resolution():
    launcher = sl.Launcher()
    entry = sl.scene_entry("hm3d_00802", launcher.registry)
    profile_path, temp = launcher._temp_profile(entry, "main_600")
    try:
        profile = sl.read_json(profile_path)
        assert profile["sensor"]["resolution_px"] == [600, 340]
        assert profile["route_config"].endswith("hm3d_minival_v02_00802_main_600_route.json")
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def test_main_dense_route_keeps_original_waypoints_and_sensor_profile():
    entry = sl.scene_entry("hm3d_00802")
    main_600 = sl.read_json(sl.resolve_path(entry["routes"]["main_600"]))
    dense = sl.read_json(sl.resolve_path(entry["routes"]["main_dense"]))
    assert entry["default_route"] == "main"
    assert len(main_600["route"]) == 29
    assert 200 <= len(dense["route"]) <= 300
    assert dense["resolution_px"] == main_600["resolution_px"] == [600, 340]
    sensor_keys = {
        "horizontal_fov_deg", "sensor_position_habitat", "sensor_orientation_habitat_rad",
        "calibration_id", "min_depth_m", "max_depth_m", "simulation_step_s",
    }
    assert {key: dense[key] for key in sensor_keys} == {key: main_600[key] for key in sensor_keys}
    cursor = 0
    for original in main_600["route"]:
        cursor = next(index for index in range(cursor, len(dense["route"]))
                      if dense["route"][index] == original) + 1
    assert dense["dense_route_provenance"]["original_waypoints_preserved_in_order"] == 29


def test_effective_profile_tracks_main_dense_route_and_resolution():
    launcher = sl.Launcher()
    entry = sl.scene_entry("hm3d_00802", launcher.registry)
    profile_path, temp = launcher._temp_profile(entry, "main_dense")
    try:
        profile = sl.read_json(profile_path)
        assert profile["sensor"]["resolution_px"] == [600, 340]
        assert profile["route_config"].endswith("hm3d_minival_v02_00802_main_dense_route.json")
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def test_cli_and_interactive_dispatch_use_same_map_handler(capsys):
    launcher = sl.Launcher()
    assert launcher.handle(["maps"]) == 0
    first = capsys.readouterr().out
    assert launcher.handle(["/maps"], interactive=True) == 0
    second = capsys.readouterr().out
    assert first == second


def test_dry_run_is_plan_only_and_does_not_create_output(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs with spaces"
    monkeypatch.setattr(sl, "RUNS", runs)
    monkeypatch.setattr(sl, "LOCK_PATH", runs / ".launcher.lock")
    launcher = sl.Launcher()
    plan = launcher._run_pipeline("hm3d_00802", "smoke", dry_run=True, association_profile="geometry-only-dedup-v2")
    assert "capture" in plan["outputs"]
    assert plan["association_profile"] == "geometry-only-dedup-v2"
    assert plan["u4_ov_diagnostics"] == "off"
    assert not runs.exists()
    assert json.loads(capsys.readouterr().out)["route"] == "smoke"


def test_run_cli_passes_selected_profile_and_keeps_source_default(monkeypatch):
    launcher = sl.Launcher()
    calls = []
    monkeypatch.setattr(launcher, "_run_pipeline", lambda alias, route, dry_run=False, association_profile="source":
                        calls.append((alias, route, dry_run, association_profile)) or {"status": "completed"})
    assert launcher.cmd_run(["hm3d_00802", "--route", "smoke", "--dry-run"], interactive=False) == 0
    assert launcher.cmd_run(["hm3d_00802", "--route", "smoke", "--profile", "geometry-only-dedup-v2", "--dry-run"], interactive=False) == 0
    assert calls == [
        ("hm3d_00802", "smoke", True, "source"),
        ("hm3d_00802", "smoke", True, "geometry-only-dedup-v2"),
    ]


def test_interactive_run_keeps_selected_profile_in_worker_args(monkeypatch, capsys):
    seen = {}

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            seen.update(target=target, args=args, daemon=daemon)
        def start(self):
            pass
        def is_alive(self):
            return False

    monkeypatch.setattr(sl.threading, "Thread", FakeThread)
    launcher = sl.Launcher()
    assert launcher.handle(["/run", "hm3d_00802", "--route", "smoke", "--profile", "geometry-only"], interactive=True) == 0
    assert seen["args"] == ("hm3d_00802", "smoke", "geometry-only")
    assert "profile=geometry-only" in capsys.readouterr().out


def test_paths_with_spaces_are_preserved_as_argument_items(tmp_path):
    p = tmp_path / "folder with spaces" / "input file.json"
    args = ["python3", "runner.py", "--config", str(p)]
    assert sl._safe_manifest_args(args) == args
    assert "folder with spaces" in subprocess.list2cmdline(args)


def test_run_id_is_unique_at_subsecond_resolution():
    ids = {sl.unique_run_id("apt0") for _ in range(20)}
    assert len(ids) == 20


def test_run_lookup_rejects_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "RUNS", tmp_path)
    launcher = sl.Launcher()
    with pytest.raises(ValueError, match="only be ID|chỉ được là ID"):
        launcher._find_run("../somewhere")


def test_failed_stage_records_failure_and_does_not_claim_success(tmp_path, monkeypatch):
    launcher = sl.Launcher()
    monkeypatch.setattr(launcher, "_subprocess", lambda *a, **k: (7, "intentional stage failure"))
    run_root = tmp_path / "run"
    launcher.active_run = run_root
    launcher.active_state = {"run_id": "r", "status": "running", "stages": {}}
    with pytest.raises(RuntimeError, match="failed"):
        launcher._run_logged_stage("capture", ["false"], "capture.log", py_env=os.environ.copy(),
                                   required=[run_root / "capture_manifest.json"], run_root=run_root)
    assert launcher.active_state["stages"]["capture"]["status"] == "failed"
    assert not (run_root / "capture_manifest.json").exists()


def test_launcher_entrypoint_works_from_other_cwd(tmp_path):
    script = ROOT / "run_scene.sh"
    result = subprocess.run([str(script), "info", "hm3d_00802"], cwd=tmp_path,
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert "00802-wcojb4TFT35" in result.stdout


def test_project_python_probe_checks_all_pipeline_capabilities():
    launcher = sl.Launcher()
    assert ".venv-perception/bin/python" in launcher.env["project_python"]
    report = launcher._project_python_probe()
    assert report["status"] == "pass", report
    assert report["versions"]["onnxruntime"] == "1.24.4"
    assert report["versions"]["yaml"] == "6.0.1"
    assert report["versions"]["matplotlib"] == "3.9.1"
    assert report["matplotlib_plot_smoke"] is True
    assert {name: row["status"] for name, row in report["capabilities"].items()} == {
        "perception": "pass", "replay": "pass", "semantic": "pass", "evaluation": "pass"}


def test_project_python_probe_detects_system_python_missing_onnx_without_installing(monkeypatch):
    launcher = sl.Launcher()
    launcher.env["project_python"] = "/usr/bin/python3"
    report = launcher._project_python_probe()
    assert report["status"] == "fail"
    assert "onnxruntime" in report["missing"]
    assert report["capabilities"]["perception"]["status"] == "fail"


def test_scene_route_mismatch_is_rejected_before_run():
    entry = sl.scene_entry("hm3d_00802")
    entry["routes"] = {"wrong": "configs/habitat/m12_replica_cad_apt0_perception_dev_route.json"}
    entry["default_route"] = "wrong"
    with pytest.raises(ValueError, match="dataset_type|scene_id"):
        sl._scene_context(entry)


def test_output_collision_refuses_to_reuse_existing_run(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    existing_id = "fixed-id"
    (runs / existing_id).mkdir(parents=True)
    monkeypatch.setattr(sl, "RUNS", runs)
    monkeypatch.setattr(sl, "unique_run_id", lambda _: existing_id)
    launcher = sl.Launcher()
    with pytest.raises(FileExistsError):
        launcher._run_pipeline("hm3d_00802", "smoke", dry_run=False)
    assert list((runs / existing_id).iterdir()) == []


def test_full_run_is_recorded_failed_before_capture_when_project_env_is_missing(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    monkeypatch.setattr(sl, "RUNS", runs)
    monkeypatch.setattr(sl, "LOCK_PATH", runs / ".launcher.lock")
    monkeypatch.setattr(sl, "ACTIVE_POINTER", runs / ".launcher_active.json")
    launcher = sl.Launcher()
    launcher.env["project_python"] = "/bin/false"
    state = launcher._run_pipeline("hm3d_00802", "smoke", dry_run=False)
    run_root = runs / state["run_id"]
    assert state["status"] == "failed"
    assert state["stages"]["preflight"]["status"] == "failed"
    assert not (run_root / "capture").exists()
    failed_manifest = sl.read_json(run_root / "launcher_manifest.json")
    assert failed_manifest["status"] == "failed"
    assert sl.rel_or_abs(sl.resolve_path("configs/habitat/hm3d_minival_v02_00802_smoke.json")) in failed_manifest["input_sha256"]
    assert not (runs / ".launcher_active.json").exists()


def test_stale_active_pointer_recovers_as_interrupted_without_resume(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    active = runs / ".launcher_active.json"
    lock = runs / ".launcher.lock"
    monkeypatch.setattr(sl, "RUNS", runs)
    monkeypatch.setattr(sl, "ACTIVE_POINTER", active)
    monkeypatch.setattr(sl, "LOCK_PATH", lock)
    run_id = "crashed-run"
    sl.atomic_json(active, {"schema_version": 1, "kind": "habitat_launcher_run", "run_id": run_id,
                            "status": "running", "current_stage": "native_replay", "stages": {}})
    launcher = sl.Launcher()
    launcher._recover_interrupted()
    recovered = sl.read_json(runs / run_id / "launcher_manifest.json")
    assert recovered["status"] == "interrupted"
    assert "no automatic resume" in recovered["error"]
    assert not active.exists()


def test_cancel_terminates_only_owned_stage_process(tmp_path):
    import threading
    launcher = sl.Launcher()
    event = threading.Event()
    launcher.cancel_requested = event
    result = {}

    def worker():
        result["value"] = launcher._subprocess([sys.executable, "-c", "import time; time.sleep(20)"],
            stage="test", log_path=tmp_path / "cancel.log", env=os.environ.copy())

    thread = threading.Thread(target=worker)
    thread.start()
    for _ in range(100):
        if launcher.active_process is not None:
            break
        time.sleep(0.02)
    assert launcher.active_process is not None
    event.set()
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert result["value"][0] == 130


def test_invalid_scene_entry_is_not_registered_or_written(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    original = sl.load_registry()
    sl.atomic_json(registry_path, original)
    launcher = sl.Launcher(registry_path=registry_path)
    entry_path = tmp_path / "entry.json"
    sl.atomic_json(entry_path, {"alias": "hm3d_00802", "evaluation_profile": "configs/evaluation/hm3d_00802_scene_eval_v1.json",
                                "routes": {"smoke": "configs/habitat/hm3d_minival_v02_00802_smoke.json"}, "default_route": "smoke"})
    with pytest.raises(ValueError, match="duplicate"):
        launcher.cmd_register(["--config", str(entry_path)])
    assert sl.read_json(registry_path) == original
