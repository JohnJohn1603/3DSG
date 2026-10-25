from __future__ import annotations

import json
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
        assert profile["dataset"]["scene_dataset_config"] == sl.read_json(sl.resolve_path(entry["routes"]["smoke"]))["scene_dataset_config"]
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
    plan = launcher._run_pipeline("hm3d_00802", "smoke", dry_run=True)
    assert "capture" in plan["outputs"]
    assert not runs.exists()
    assert json.loads(capsys.readouterr().out)["route"] == "smoke"


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
