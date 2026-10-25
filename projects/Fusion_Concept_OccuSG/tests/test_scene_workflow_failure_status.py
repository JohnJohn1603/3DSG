from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import integration_v2.fresh_replica as fresh_replica
import scripts.scene_launcher as scene_launcher


def test_c2_preflight_exception_marks_failing_stage(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "dataset": {"type": "hm3d", "scene_id": "scene-x", "sequence_id": "route-x"},
        "mapping": {"membership_policy": "unavailable_elevation_scope"},
        "route_config": "route.json",
    }))
    monkeypatch.setattr(fresh_replica, "HYBRID_RUNS", tmp_path / "runs")
    monkeypatch.setattr(fresh_replica, "_config_contract", lambda _config: None)

    def fail_preflight(_config_path, _config, _run_root):
        raise RuntimeError("memory gate")

    monkeypatch.setattr(fresh_replica, "_prepare", fail_preflight)
    with pytest.raises(RuntimeError, match="memory gate"):
        fresh_replica.run_fresh_replica(config_path, run_id="c2_fail_preflight")

    manifest = json.loads((tmp_path / "runs/c2_fail_preflight/c2_run_manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["stages"]["preflight"]["status"] == "failed"
    assert manifest["stages"]["preflight"]["error"] == "RuntimeError: memory gate"


def test_scene_workflow_failure_marks_preflight_and_downstream_stages(tmp_path: Path, monkeypatch):
    workflow_root = tmp_path / "workflows"
    route_path = tmp_path / "route.json"
    route_path.write_text("{}")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}")
    template_path = tmp_path / "template.json"
    template_path.write_text("{}")
    scene = SimpleNamespace(scene_id="scene-x", scene_asset=str(tmp_path / "scene.glb"),
                            navmesh=str(tmp_path / "scene.navmesh"), scene_dataset_config="dataset.json",
                            adapter="habitat_hm3d_minival_v02_v1")
    route = SimpleNamespace(sequence_id="route-x", config_path=str(route_path), pose_count=2)
    config = {"dataset": {"scene_id": "scene-x"},
              "mapping": {"membership_policy": "unavailable_elevation_scope",
                          "room_semantics_status": "unavailable_membership_scope"}}
    (tmp_path / "scene.glb").write_bytes(b"scene")
    (tmp_path / "scene.navmesh").write_bytes(b"navmesh")
    monkeypatch.setattr(scene_launcher, "ROOT", tmp_path)
    monkeypatch.setattr(scene_launcher, "SCENE_WORKFLOWS", workflow_root)
    monkeypatch.setattr(scene_launcher, "sha256", lambda path: "a" * 64)
    monkeypatch.setattr(scene_launcher, "atomic_json", lambda path, value: Path(path).write_text(
        json.dumps(value, indent=2)))
    monkeypatch.setattr(scene_launcher, "read_json", lambda path: json.loads(Path(path).read_text()))
    monkeypatch.setattr(scene_launcher.Launcher, "_print_scene_resolution", lambda *args, **kwargs: None)
    monkeypatch.setattr(scene_launcher, "datetime", scene_launcher.datetime)
    monkeypatch.setattr("integration_v2.scene_registry.resolve_scene_route",
                        lambda *args, **kwargs: (scene, route, config))
    monkeypatch.setattr("integration_v2.fresh_replica.make_c2_run_id", lambda: "c2_fixture")

    def fail_preflight(*_args, **_kwargs):
        raise RuntimeError("preflight memory gate")

    monkeypatch.setattr("integration_v2.fresh_replica.run_fresh_replica", fail_preflight)
    launcher = object.__new__(scene_launcher.Launcher)
    launcher.registry_path = registry_path
    launcher.env_path = tmp_path / "env.json"
    launcher.registry = {"catalog": {"c2_template": str(template_path)}}

    with pytest.raises(RuntimeError, match="preflight memory gate"):
        launcher._run_scene_workflow("hm3d", "scene-x", "main", port=8766)

    manifests = list(workflow_root.glob("*/run_manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["status"] == "failed"
    assert manifest["stages"]["preflight"]["status"] == "failed"
    assert manifest["stages"]["C2"]["status"] == "failed"
    assert manifest["inputs"]["c2_template_sha256"] == "a" * 64
    assert manifest["inputs"]["navmesh_sha256"] == "a" * 64
    assert manifest["inputs"]["scene_dataset_config_sha256"] == "a" * 64
    assert set(manifest["inputs"]["source_sha256"]) == {
        "scene_registry", "fresh_replica_runner", "scene_launcher"
    }
    assert manifest["stages"]["D1"]["status"] == "not_started_due_to_prior_failure"
    assert manifest["stages"]["D2_bootstrap"]["status"] == "not_started_due_to_prior_failure"
