from __future__ import annotations

import json
import hashlib
from pathlib import Path

from scripts.run_causal_hybrid_room_belief import membership_for, room_evidence_eligible, run, scope_assessment
from standalone_sg.contracts.regions import Polygon2D


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a_v2"


def test_a_scope_missing_does_not_open_xy_membership_gate():
    manifest = json.loads((RUN / "manifest.json").read_text())
    scope = manifest["membership_scope"]
    assert scope["status"] == "unavailable"
    assert scope["reason"] == "room_scope_contract_missing_for_2d_polygons"
    assert scope["profile_matches_source_layers"] is True
    assert scope["scope_contract"] is None
    for row in manifest["revisions"]:
        state = json.loads((RUN / row["state"]).read_text())
        assert state["membership_causal"] is False
        assert state["room_contains_edges"] == []
        assert all(obj["membership"]["status"] == "unavailable" for obj in state["objects"])


def test_a_room_lifecycle_emits_only_current_rooms_and_retirement():
    manifest = json.loads((RUN / "manifest.json").read_text())
    seen = set()
    retired = set()
    for row in manifest["revisions"]:
        state = json.loads((RUN / row["state"]).read_text())
        lifecycle = state["room_lifecycle"]
        active = set(lifecycle["active_room_ids"])
        seen |= active
        retired |= set(lifecycle["retired_room_ids"])
        for variant in manifest["variants"]:
            assert set(state["rooms"][variant]) == active
        assert not (retired & active)
    final = json.loads((RUN / manifest["revisions"][-1]["state"]).read_text())
    assert {"room:1", "room:4"}.issubset(retired)
    assert "room:1" not in final["room_lifecycle"]["active_room_ids"]
    assert "room:4" not in final["room_lifecycle"]["active_room_ids"]


def test_a_effective_admission_and_a_digests_are_provenance_bound():
    manifest = json.loads((RUN / "manifest.json").read_text())
    assert manifest["admission_policy"]["obj_min_detections"] == 3
    assert manifest["admission_policy"]["provenance"]["sha256"]
    shape = manifest["a_table_shape"]
    assert shape == {"categories": 11, "rooms": 8, "rows_per_variant": 88}
    assert set(manifest["a_table_digests"]) == set(manifest["variants"])
    assert all(len(value) == 64 for value in manifest["a_table_digests"].values())


def test_a_prefix_has_no_future_observations_and_revision_chain_is_indexed():
    manifest = json.loads((RUN / "manifest.json").read_text())
    revisions = [int(row["revision"]) for row in manifest["revisions"]]
    assert revisions == list(range(1, len(revisions) + 1))
    for row in manifest["revisions"]:
        state = json.loads((RUN / row["state"]).read_text())
        limit = int(state["frame_index"])
        assert state["future_observations_used"] is False
        for obj in state["objects"]:
            for observation_id in obj["observation_ids"]:
                frame = int(observation_id.split("-")[-2].split(":")[0])
                assert frame <= limit


def test_a_mixed_prefix_layers_keep_scope_gate_closed(tmp_path: Path):
    timeline = tmp_path / "timeline"
    (timeline / "snapshots").mkdir(parents=True)
    (timeline / "room_revisions").mkdir()
    cache = tmp_path / "cache"
    (cache / "detection_3d").mkdir(parents=True)
    (cache / "replay").mkdir()
    (cache / "capture" / "frames").mkdir(parents=True)
    (cache / "effective_evaluation_profile.json").write_text(json.dumps({"dataset": {"floor_id": "floor_0"}}))
    (cache / "detection_3d" / "detection_3d.jsonl").write_text(
        json.dumps({"detections": [{"detection_id": "scene-0000:det-0000", "layer_id": "floor_0"}]}) + "\n"
        + json.dumps({"detections": [{"detection_id": "scene-0001:det-0000", "layer_id": "floor_1"}]}) + "\n"
    )
    room = {"snapshot": {"revision": 1, "nodes": [{"kind": "room", "node_id": "room:1",
        "attributes": {"outer_ring": [[0, 0], [1, 0], [1, 1], [0, 1]]}}]}}
    room_docs = []
    frames = []
    for idx in range(2):
        (timeline / "room_revisions" / f"rev-{idx:06d}.json").write_text(json.dumps(room))
        snap_rel = f"snapshots/rev-{idx+1:06d}.json"
        (timeline / snap_rel).write_text(json.dumps({"frame_index": idx, "observation_ids": [f"scene-{idx:04d}:det-0000"]}))
        frames.append({"frame_index": idx, "snapshot": snap_rel})
        room_docs.append(room)
    manifest = {"cache_root": str(cache), "room_revision_files": {"0": "room_revisions/rev-000000.json", "1": "room_revisions/rev-000001.json"}}
    result = scope_assessment(timeline, room_docs, manifest, frames, {},
                              {"status": "verified_single_floor_scope", "floor_ids": ["floor_0"], "source_refs": ["locked"]})
    assert result["status"] == "unavailable"
    assert result["reason"] == "prefix_scope_contains_mixed_floor_layers"


def _scope_fixture(tmp_path: Path, layers: list[str]):
    timeline = tmp_path / "timeline"
    (timeline / "snapshots").mkdir(parents=True)
    cache = tmp_path / "cache"
    (cache / "detection_3d").mkdir(parents=True)
    (cache / "capture" / "frames").mkdir(parents=True)
    (cache / "effective_evaluation_profile.json").write_text(json.dumps({"dataset": {"floor_id": "floor_0"}}))
    rows = []
    room_docs = []
    frames = []
    room = {"snapshot": {"revision": 1, "nodes": [{"kind": "room", "node_id": "room:1",
        "attributes": {"outer_ring": [[0, 0], [1, 0], [1, 1], [0, 1]]}}]}}
    for idx, layer in enumerate(layers):
        oid = f"scene-{idx:04d}:det-0000"
        rows.append(json.dumps({"detections": [{"detection_id": oid, "layer_id": layer}]}))
        rel = f"snapshots/rev-{idx + 1:06d}.json"
        (timeline / rel).write_text(json.dumps({"frame_index": idx, "frame_id": f"frame-{idx}",
                                                "observation_ids": [oid]}))
        frames.append({"frame_index": idx, "snapshot": rel, "revision": idx + 1})
        room_docs.append(room)
    (cache / "detection_3d" / "detection_3d.jsonl").write_text("\n".join(rows) + "\n")
    source = tmp_path / "room_producer_scope.json"
    source.write_text(json.dumps({"scene_id": "scene-test", "sequence_id": "seq-test",
                                   "coordinate_frame": "capture/world metric meters, Z-up"}))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    contract = {"status": "verified_single_floor_scope", "floor_ids": ["floor_0"],
                "scene_id": "scene-test", "sequence_id": "seq-test",
                "coordinate_frame": "capture/world metric meters, Z-up", "units": "meters",
                "source_refs": [{"path": str(source), "sha256": digest}]}
    manifest = {"cache_root": str(cache), "scene_id": "scene-test", "sequence_id": "seq-test"}
    return timeline, manifest, room_docs, frames, contract


def test_profile_and_p3_floor_agreement_without_room_scope_stays_unavailable(tmp_path: Path):
    timeline, manifest, rooms, frames, _ = _scope_fixture(tmp_path, ["floor_0", "floor_0"])

    result = scope_assessment(timeline, rooms, manifest, frames, {}, scope_contract=None)

    assert result["status"] == "unavailable"
    assert result["reason"] == "room_scope_contract_missing_for_2d_polygons"
    assert result["evaluation_profile_floor_id"] == "floor_0"
    assert result["observed_floor_ids"] == ["floor_0"]
    assert result["layer_metadata_complete"] is True
    assert result["room_nodes_with_elevation_metadata"] == 0


def test_a_prefix_scope_does_not_change_when_later_layer_appears(tmp_path: Path):
    timeline, manifest, rooms, frames, contract = _scope_fixture(tmp_path, ["floor_0", "floor_1"])
    prefix = scope_assessment(timeline, rooms[:1], manifest, frames[:1], {}, contract)
    full = scope_assessment(timeline, rooms, manifest, frames, {}, contract)
    assert prefix["status"] == "verified_single_observed_floor_scope"
    assert full["status"] == "unavailable"
    assert full["reason"] == "prefix_scope_contains_mixed_floor_layers"


def test_a_scope_contract_source_scene_hash_and_coordinate_checks(tmp_path: Path):
    timeline, manifest, rooms, frames, contract = _scope_fixture(tmp_path, ["floor_0"])
    bad_hash = dict(contract)
    bad_hash["source_refs"] = [{"path": contract["source_refs"][0]["path"], "sha256": "0" * 64}]
    assert scope_assessment(timeline, rooms, manifest, frames, {}, bad_hash)["reason"] == "scope_contract_source_hash_mismatch"
    missing = dict(contract); missing["source_refs"] = [{"path": str(tmp_path / "missing.json")}]
    assert scope_assessment(timeline, rooms, manifest, frames, {}, missing)["reason"] == "scope_contract_source_missing"
    bad_scene = dict(contract); bad_scene["scene_id"] = "other-scene"
    assert scope_assessment(timeline, rooms, manifest, frames, {}, bad_scene)["reason"] == "scope_contract_scene_mismatch"
    bad_sequence = dict(contract); bad_sequence["sequence_id"] = "other-sequence"
    assert scope_assessment(timeline, rooms, manifest, frames, {}, bad_sequence)["reason"] == "scope_contract_sequence_mismatch"
    bad_coord = dict(contract); bad_coord.pop("coordinate_frame")
    assert scope_assessment(timeline, rooms, manifest, frames, {}, bad_coord)["reason"] == "scope_contract_coordinate_frame_missing"
    wrong_source = tmp_path / "wrong_source.json"
    wrong_source.write_text(json.dumps({"scene_id": "scene-test", "sequence_id": "seq-test", "coordinate_frame": "other-frame"}))
    wrong_source_contract = dict(contract)
    wrong_source_contract["source_refs"] = [{"path": str(wrong_source), "sha256": hashlib.sha256(wrong_source.read_bytes()).hexdigest()}]
    assert scope_assessment(timeline, rooms, manifest, frames, {}, wrong_source_contract)["reason"] == "scope_contract_source_coordinate_frame_mismatch"


def test_a_valid_scope_opens_membership_without_using_future_metadata(tmp_path: Path):
    timeline, manifest, rooms, frames, contract = _scope_fixture(tmp_path, ["floor_0", "floor_0"])
    prefix = scope_assessment(timeline, rooms[:1], manifest, frames[:1], {}, contract)
    full = scope_assessment(timeline, rooms, manifest, frames, {}, contract)
    assert prefix["status"] == full["status"] == "verified_single_observed_floor_scope"
    import numpy as np
    cloud_path = tmp_path / "cloud.npz"
    np.savez(cloud_path, xyz=np.asarray([[0.25, 0.25, 0.0], [0.30, 0.30, 0.0]], dtype=float))
    membership = membership_for({"observation_ids": ["scene-0000:det-0000"], "cloud_npz": "cloud.npz"},
                                {"room:1": Polygon2D(((0, 0), (1, 0), (1, 1), (0, 1)))},
                                prefix, tmp_path, None, 1, 0.025, [0.0, 0.0, 0.0])
    assert membership["status"] == "assigned"
    assert membership["room_id"] == "room:1"


def test_a_scope_contract_effective_frame_is_prefix_local(tmp_path: Path):
    timeline, manifest, rooms, frames, contract = _scope_fixture(tmp_path, ["floor_0", "floor_0"])
    late = dict(contract); late["effective_from_frame"] = 1
    before = scope_assessment(timeline, rooms[:1], manifest, frames[:1], {}, late)
    after = scope_assessment(timeline, rooms, manifest, frames, {}, late)
    assert before["reason"] == "scope_contract_not_effective_for_prefix"
    assert after["status"] == "verified_single_observed_floor_scope"


def test_a_object_evidence_eligibility_is_not_inherited_from_previous_object():
    table = {"sofa": {"living_room": 1.0}}
    assigned = {"status": "assigned"}
    assert room_evidence_eligible(True, "stable", assigned, "sofa", table)
    assert not room_evidence_eligible(False, "stable", assigned, "sofa", table)
    assert not room_evidence_eligible(True, "provisional", assigned, "sofa", table)
    assert not room_evidence_eligible(True, "stable", {"status": "unavailable"}, "sofa", table)


def test_a_unavailable_prefix_matches_same_prefix_of_full_runner(tmp_path: Path):
    source_timeline = ROOT / "artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2/object_timeline/timeline"
    full_artifact = ROOT / "artifacts/room_belief/t3_hm3d00802_main29_640_verified_v1"
    short_timeline = tmp_path / "timeline"
    short_timeline.mkdir()
    (short_timeline / "snapshots").symlink_to(source_timeline / "snapshots", target_is_directory=True)
    (short_timeline / "room_revisions").symlink_to(source_timeline / "room_revisions", target_is_directory=True)
    (short_timeline / "geometry").symlink_to(source_timeline / "geometry", target_is_directory=True)
    manifest = json.loads((source_timeline / "manifest.json").read_text())
    index = json.loads((source_timeline / "frame_index.json").read_text())
    index["frames"] = index["frames"][:2]
    (short_timeline / "frame_index.json").write_text(json.dumps(index))
    manifest["snapshot_index"] = "frame_index.json"
    manifest["frame_count"] = 2
    (short_timeline / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "out"
    a_zip = ROOT / "artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip"
    run(short_timeline, out, a_zip)
    short_state = json.loads((out / "revisions/rev-000001.json").read_text())
    full_state = json.loads((full_artifact / "revisions/rev-000001.json").read_text())
    for key in ("objects", "rooms", "room_lifecycle", "membership_scope", "room_contains_edges",
                "membership_causal", "room_semantics_status"):
        assert short_state[key] == full_state[key]
