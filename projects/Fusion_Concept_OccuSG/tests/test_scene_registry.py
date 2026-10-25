from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integration_v2.fresh_replica import _config_contract  # noqa: E402
from integration_v2.scene_registry import discover_scenes, manifest_acceptance, resolve_scene_route  # noqa: E402


def test_adapters_discover_local_replica_and_hm3d_assets_and_keep_route_gaps_visible():
    replica = discover_scenes("replica_cad")
    hm3d = discover_scenes("hm3d_minival_v0.2")
    assert len(replica) == 91
    assert len(hm3d) == 10
    assert {s.scene_id for s in hm3d if s.routes} == {
        "00800-TEEsavR23oF", "00802-wcojb4TFT35", "00808-y9hTuugGdiq"
    }
    assert len([s for s in hm3d if not s.routes]) == 7
    blocked = next(s for s in hm3d if s.scene_id == "00801-HaxA7YrQdEC")
    assert blocked.asset_status == "ready_assets"
    assert blocked.routes == ()
    assert "Run is blocked" in blocked.route_block_reason


def test_resolver_builds_hm3d_main_and_replica_cad_effective_configs():
    hm3d, hm3d_route, hm3d_config = resolve_scene_route(
        "hm3d_minival_v0.2", "00802-wcojb4TFT35", "main")
    assert hm3d_route.pose_count == 29
    assert hm3d_config["dataset"]["type"] == "hm3d"
    assert Path(hm3d_config["dataset"]["scene_asset"]).name == "wcojb4TFT35.basis.glb"
    assert hm3d_config["route_config"] == hm3d_route.config_path
    assert hm3d_config["mapping"]["membership_policy"] == "unavailable_elevation_scope"
    assert hm3d_config["capture_profile"]["profile_id"] == "native_640x480_v1"
    assert hm3d_config["capture_profile"]["resolution_px"] == [640, 480]
    assert "native Habitat sensor capture at 640x480" in hm3d_config["mapping"]["resolution_policy"]
    _config_contract(hm3d_config)

    legacy_scene, legacy_route, legacy_config = resolve_scene_route(
        "hm3d_minival_v0.2", "00802-wcojb4TFT35", "main", capture_profile_id="legacy_160x120_smoke_v1")
    assert legacy_route.resolution_px == (160, 120)  # immutable route remains unchanged
    assert legacy_config["capture_profile"]["resolution_px"] == [160, 120]
    assert legacy_config["capture_profile"]["profile_id"] == "legacy_160x120_smoke_v1"
    assert Path(legacy_route.config_path).read_text() == Path(hm3d_route.config_path).read_text()
    _config_contract(legacy_config)

    replica, replica_route, replica_config = resolve_scene_route("replica_cad", "apt_5", "navmesh")
    assert replica_route.pose_count == 12
    assert replica_config["dataset"]["type"] == "replica_cad"
    assert Path(replica_config["dataset"]["scene_instance"]).name == "apt_5.scene_instance.json"
    assert replica_config["capture_profile"]["resolution_px"] == [640, 480]
    _config_contract(replica_config)


def test_resolver_refuses_unregistered_route_without_inventing_one():
    with pytest.raises(ValueError, match="No registered route config"):
        resolve_scene_route("hm3d_minival_v0.2", "00801-HaxA7YrQdEC", "main")


def test_acceptance_is_reported_only_from_completed_run_manifests(tmp_path):
    assert manifest_acceptance("scene-x", "route-x", workspace_root=tmp_path)["c2_status"] == "no_completed_manifest_evidence"
    c2 = tmp_path / "artifacts/conceptgraphs_hybrid_runs/c2_x"
    d1 = tmp_path / "artifacts/live_processing_runs/d1_x"
    c2.mkdir(parents=True)
    d1.mkdir(parents=True)
    (c2 / "c2_run_manifest.json").write_text(json.dumps({
        "status": "completed", "run_id": "c2_x", "scene_id": "scene-x", "sequence_id": "route-x",
        "input_sha256": {"route": "abc"}, "output_sha256": {"snapshot": "def"}
    }), encoding="utf-8")
    (d1 / "run_manifest.json").write_text(json.dumps({
        "status": "completed", "run_id": "d1_x", "scene_id": "scene-x", "sequence_id": "route-x",
        "viewer_start_gate": {"status": "released", "producer_started": True, "confirmed_browser_ready": True},
        "viewer": {"d2_xyz": {"status": "completed"}}
    }), encoding="utf-8")
    result = manifest_acceptance("scene-x", "route-x", workspace_root=tmp_path)
    assert result["c2_status"] == "accepted_by_manifest"
    assert result["d1_status"] == "accepted_by_manifest"
    assert result["d2_simultaneous_status"] == "accepted_by_manifest"
