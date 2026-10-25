from __future__ import annotations

import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import scene_launcher as sl  # noqa: E402


def test_terminal_menu_resolves_hm3d_scene_and_route_through_common_workflow(monkeypatch, capsys):
    launcher = sl.Launcher()
    scenes = launcher._scene_catalog_items("hm3d_minival_v0.2")
    scene_index = next(i for i, s in enumerate(scenes) if s.scene_id == "00802-wcojb4TFT35")
    route_index = next(i for i, r in enumerate(scenes[scene_index].routes) if r.route_id == "main")
    inputs = iter(["2", str(scene_index + 1), str(route_index + 1), ""])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(inputs))
    selected = {}
    monkeypatch.setattr(launcher, "_run_scene_workflow",
                        lambda dataset, scene, route, **kwargs: selected.update(
                            dataset=dataset, scene=scene, route=route, **kwargs) or 0)

    assert launcher.cmd_scene_menu([]) == 0
    assert selected == {"dataset": "hm3d_minival_v0.2", "scene": "00802-wcojb4TFT35",
                        "route": "main", "port": 8766, "assume_run_confirmed": False,
                        "capture_profile_id": "native_640x480_v1"}
    output = capsys.readouterr().out
    assert "10 local scenes discovered" in output
    assert "C2=" in output and "D1=" in output and "D2-simultaneous=" in output
    assert "native_640x480_v1" in output and "[640, 480] px [default]" in output


def test_menu_keeps_route_missing_scene_visible_and_does_not_run(monkeypatch, capsys):
    launcher = sl.Launcher()
    scenes = launcher._scene_catalog_items("hm3d_minival_v0.2")
    scene_index = next(i for i, s in enumerate(scenes) if s.scene_id == "00801-HaxA7YrQdEC")
    inputs = iter(["2", str(scene_index + 1), "q"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(inputs))
    called = []
    monkeypatch.setattr(launcher, "_run_scene_workflow", lambda *a, **k: called.append((a, k)) or 0)
    assert launcher.cmd_scene_menu([]) == 0
    assert not called
    output = capsys.readouterr().out
    assert "NO ROUTE — Run blocked" in output
    assert "No registered route config matches this scene" in output


def test_scene_run_cli_and_menu_share_the_same_workflow_entrypoint(monkeypatch):
    launcher = sl.Launcher()
    selected = {}
    monkeypatch.setattr(launcher, "_run_scene_workflow",
                        lambda dataset, scene, route, **kwargs: selected.update(
                            dataset=dataset, scene=scene, route=route, **kwargs) or 0)
    assert launcher.cmd_scene_run(["--dataset", "hm3d_minival_v0.2", "--scene", "00802-wcojb4TFT35",
                                   "--route", "main", "--yes", "--port", "8767"]) == 0
    assert selected == {"dataset": "hm3d_minival_v0.2", "scene": "00802-wcojb4TFT35",
                        "route": "main", "port": 8767, "assume_run_confirmed": True,
                        "capture_profile_id": "native_640x480_v1"}

    assert launcher.cmd_scene_run(["--dataset", "replica_cad", "--scene", "apt_5", "--route", "navmesh",
                                   "--capture-profile", "legacy_160x120_smoke_v1", "--yes"]) == 0
    assert selected["capture_profile_id"] == "legacy_160x120_smoke_v1"


def test_new_commands_do_not_remove_legacy_entrypoints():
    assert {"menu", "scene-run", "fresh-replica", "live-replay", "results", "open"} <= set(sl.COMMANDS)
