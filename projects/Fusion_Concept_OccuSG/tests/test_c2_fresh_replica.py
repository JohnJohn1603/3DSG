from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from integration_v2.fresh_replica import (
    _apply_capture_profile, _config_contract, _process_tree_rss_bytes,
    excluded_native_object_memberships, room_graph_integration_signature,
    room_graph_projection_signature, validate_unavailable_membership,
)
from scripts.run_conceptgraphs_cached_hybrid import complete_frame_pose_map

ROOT = Path(__file__).resolve().parents[1]


def test_c2_config_is_scene_and_route_driven_and_keeps_scope_unavailable():
    config = json.loads((ROOT / "configs/scene_runs/replica_cad_apt0_short_c2_v1.json").read_text())
    _config_contract(config)
    assert config["dataset"]["scene_id"] == "apt_0"
    assert config["route_config"].endswith("m12_replica_cad_apt0_instance_route.json")
    assert config["mapping"]["membership_policy"] == "unavailable_elevation_scope"
    assert config["resource_policy"]["download_models"] is False
    assert config["resource_policy"]["install_dependencies"] is False


def test_c2_refuses_assumed_xy_membership_and_unlocked_side_effects():
    config = json.loads((ROOT / "configs/scene_runs/replica_cad_apt0_short_c2_v1.json").read_text())
    config["mapping"]["membership_policy"] = "geometry_footprint_xy"
    with pytest.raises(ValueError, match="must keep membership unavailable"):
        _config_contract(config)
    config["mapping"]["membership_policy"] = "unavailable_elevation_scope"
    config["resource_policy"]["download_models"] = True
    with pytest.raises(ValueError, match="must not download models"):
        _config_contract(config)


def test_capture_profile_overlays_only_resolution_and_calibration_without_mutating_route():
    route = {"resolution_px": [160, 120], "horizontal_fov_deg": 90.0,
             "sensor_position_habitat": [0.0, 1.5, 0.0], "sequence_id": "main"}
    profile = {"schema": "habitat-capture-profile/v1", "profile_id": "native_640x480_v1",
               "profile_sha256": "a" * 64, "resolution_px": [640, 480],
               "calibration_id": "habitat-rgbd-stage-v1-640x480",
               "intrinsics_adapter": "adapters.habitat.frames.habitat_camera_intrinsics"}
    effective = _apply_capture_profile(route, profile)
    assert effective["resolution_px"] == [640, 480]
    assert effective["calibration_id"] == profile["calibration_id"]
    assert effective["horizontal_fov_deg"] == route["horizontal_fov_deg"]
    assert effective["sensor_position_habitat"] == route["sensor_position_habitat"]
    assert route["resolution_px"] == [160, 120]
    legacy_effective = _apply_capture_profile(route, None)
    assert legacy_effective == route


def test_c2_process_tree_rss_sampler_sums_only_run_descendants(tmp_path):
    for pid, ppid, kib in ((10, 1, 100), (11, 10, 200), (12, 11, 50), (20, 1, 999)):
        proc = tmp_path / str(pid); proc.mkdir()
        (proc / "stat").write_text(f"{pid} (fixture worker) S {ppid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        (proc / "status").write_text(f"Name:\tfixture\nVmRSS:\t{kib} kB\n")
    assert _process_tree_rss_bytes(10, tmp_path) == 350 * 1024


def test_c2_mapping_uses_capture_pose_when_p3_has_no_accepted_detections():
    pose = np.eye(4, dtype=np.float64)
    result = complete_frame_pose_map(["frame_0"], {}, {"frame_0": pose})
    np.testing.assert_array_equal(result["frame_0"], pose)


def test_c2_mapping_checks_p3_pose_against_capture_and_requires_coverage():
    pose = np.eye(4, dtype=np.float64)
    result = complete_frame_pose_map(["frame_0"], {"frame_0": pose}, {"frame_0": pose.copy()})
    np.testing.assert_array_equal(result["frame_0"], pose)
    with pytest.raises(ValueError, match="disagrees"):
        complete_frame_pose_map(["frame_0"], {"frame_0": pose}, {"frame_0": pose + 1})
    with pytest.raises(ValueError, match="no verified camera pose"):
        complete_frame_pose_map(["frame_0"], {}, {})


def test_room_projection_signature_covers_polygons_adjacency_and_agent_membership():
    room = SimpleNamespace(node_id="room:1", kind="room", geometry={"xy": [[0, 0], [1, 1]]})
    agent = SimpleNamespace(node_id="agent:1", kind="agent")
    adjacency = SimpleNamespace(relation="room_adjacency", source_id="room:1", target_id="room:2",
                                directed=True, provenance="M8")
    contains = SimpleNamespace(relation="room_contains", source_id="room:1", target_id="agent:1",
                               directed=True, provenance="M8")
    base = SimpleNamespace(nodes=[room, agent], edges=[adjacency, contains])
    equivalent = SimpleNamespace(nodes=[room, agent], edges=[contains, adjacency])
    assert room_graph_projection_signature(base) == room_graph_projection_signature(equivalent)
    changed = SimpleNamespace(nodes=[room, agent], edges=[adjacency])
    assert room_graph_projection_signature(base) != room_graph_projection_signature(changed)


def test_c2_unavailable_membership_accepts_valid_zero_retained_objects():
    room = SimpleNamespace(node_id="room:1", kind="room", attributes={})
    graph = SimpleNamespace(nodes=[room], edges=[])
    assert validate_unavailable_membership(graph) == 0


def test_c2_unavailable_membership_checks_every_retained_object_and_edge():
    room = SimpleNamespace(node_id="room:1", kind="room", attributes={})
    good = SimpleNamespace(node_id="object:conceptgraphs:o1", kind="object", attributes={
        "membership_status": "unavailable", "membership_reason": "unavailable_elevation_scope"})
    graph = SimpleNamespace(nodes=[room, good], edges=[])
    assert validate_unavailable_membership(graph) == 1
    bad = SimpleNamespace(node_id="object:conceptgraphs:o2", kind="object", attributes={
        "membership_status": "assigned", "membership_reason": None})
    with pytest.raises(AssertionError, match="lost unavailable_elevation_scope"):
        validate_unavailable_membership(SimpleNamespace(nodes=[room, bad], edges=[]))
    edge = SimpleNamespace(relation="room_contains", target_id=good.node_id)
    with pytest.raises(AssertionError, match="room_contains"):
        validate_unavailable_membership(SimpleNamespace(nodes=[room, good], edges=[edge]))


def test_c2_import_keeps_room_geometry_but_reports_native_object_relation_boundary():
    room = SimpleNamespace(node_id="room:6", kind="room", attributes={"outer_ring": [[0, 0], [1, 0], [1, 1]]})
    nav = SimpleNamespace(node_id="navigation:n1", kind="navigation", attributes={})
    native = SimpleNamespace(node_id="object:object-000001", kind="object", provenance="M4:P3",
                             attributes={"class_name": "potted_plant"})
    adjacency = SimpleNamespace(relation="room_adjacency", source_id="room:6", target_id="room:7",
                                directed=True, provenance="M8:adjacency")
    nav_edge = SimpleNamespace(relation="room_contains", source_id="room:6", target_id="navigation:n1",
                               directed=True, provenance="M8:membership")
    native_edge = SimpleNamespace(relation="room_contains", source_id="room:6", target_id=native.node_id,
                                  directed=True, provenance="M8:membership")
    source = SimpleNamespace(nodes=[room, nav, native], edges=[adjacency, nav_edge, native_edge])
    unified = SimpleNamespace(nodes=[room, nav], edges=[adjacency, nav_edge])
    assert room_graph_integration_signature(source) == room_graph_integration_signature(unified)
    assert excluded_native_object_memberships(source, unified) == [{
        "room_id": "room:6", "target_node_id": "object:object-000001", "target_provider": "M4:P3",
        "target_class": "potted_plant", "edge_provenance": "M8:membership",
        "status": "source_native_object_excluded_from_conceptgraphs_import"}]
