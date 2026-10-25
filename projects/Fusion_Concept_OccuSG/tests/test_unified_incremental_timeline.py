from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from integration_v2.unified_incremental_timeline import (
    build_unified_revision, build_unified_timeline, replay_unified_snapshots,
)
from src.standalone_sg.contracts.regions import Polygon2D
from src.standalone_sg.semantics.room_belief_v1 import (
    RoomBeliefEvent, RoomBeliefLedger, RoomBeliefPolicy, RoomObjectEvidence,
    greedy_voxel_support_membership,
)


ROOT = Path(__file__).resolve().parents[1]
TIMELINE = ROOT / "artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline"
CAUSAL = ROOT / "artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a1_v6"


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _room_doc(revision: int, room_ids: list[str]) -> dict:
    rooms = {
        "room:A": {"room_id": "A", "name": "Room A", "centroid_xy": [0.5, 0.5],
                   "outer_ring": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        "room:B": {"room_id": "B", "name": "Room B", "centroid_xy": [2.5, 0.5],
                   "outer_ring": [[2, 0], [3, 0], [3, 1], [2, 1]]},
    }
    return {"kind": "snapshot", "schema_version": 1, "snapshot": {
        "revision": revision, "nodes": [{"node_id": rid, "kind": "room", "attributes": rooms[rid]}
                                          for rid in room_ids],
        "edges": [],
    }}


def _make_fixture(root: Path) -> tuple[Path, Path]:
    timeline, causal = root / "object_timeline", root / "causal_state"
    frames = []
    polygons = {
        "room:A": Polygon2D(((0, 0), (1, 0), (1, 1), (0, 1))),
        "room:B": Polygon2D(((2, 0), (3, 0), (3, 1), (2, 1))),
    }
    ledger = RoomBeliefLedger(
        room_ids=("kitchen", "living_room"), room_node_ids=("room:A", "room:B"),
        room_priors={"kitchen": 1.0, "living_room": 1.0},
        category_room_scores={"chair": {"kitchen": -2.0, "living_room": 2.0}},
        label_policy=RoomBeliefPolicy("fixture-policy", 1, 0.70, 0.20, 0.25, 1),
        evidence_policy_id="fixture-evidence", ontology_version="fixture-ontology", a_table_hash="fixture-A",
    )
    frames_spec = [
        (0, ["room:A", "room:B"], [0.5, 0.5, 0.1], "assigned", "room:A", []),
        (1, ["room:A", "room:B"], [2.5, 0.5, 0.1], "assigned", "room:B", []),
        (2, ["room:A"], [2.5, 0.5, 0.1], "unassigned", None, ["room:B"]),
    ]
    for frame, active_rooms, point, membership_status, room_id, retired in frames_spec:
        revision = frame + 1
        room_rel = f"room_revisions/rev-{revision:06d}.json"
        room_doc = _room_doc(revision, active_rooms)
        _write(timeline / room_rel, room_doc)
        observation_ids = [f"scene-{idx:04d}:det-0000" for idx in range(frame + 1)]
        h = "hypothesis_chair"
        object_doc = {
            "scene_id": "fixture-scene", "sequence_id": "fixture-route", "frame_index": frame,
            "frame_id": f"scene-{frame:04d}", "revision": revision, "timestamp_s": float(frame),
            "future_observations_used": False, "stage": "online_hypotheses",
            "objects": [{"hypothesis_id": h, "id_semantics": "fixture-prefix-id",
                         "lineage_key": h, "cloud_npz": "geometry/chair.npz", "cloud_sha256": "a" * 64,
                         "point_count": 1, "bounds_minmax": [point, point], "geometry_revision": f"geometry-{frame}",
                         "class_history_raw": ["chair"], "detection_count": len(observation_ids),
                         "distinct_frame_count": len(observation_ids), "distinct_pose_count": len(observation_ids),
                         "frame_indices": list(range(frame + 1)), "observation_ids": observation_ids}],
            "rooms": {"status": "prefix_state_only_native_replay_reference", "state_ref": room_rel},
        }
        snapshot_rel = f"snapshots/rev-{revision:06d}.json"
        _write(timeline / snapshot_rel, object_doc)
        frames.append({"frame_index": frame, "frame_id": object_doc["frame_id"], "revision": revision,
                       "snapshot": snapshot_rel})

        support = greedy_voxel_support_membership(
            "causal:chair", [point], {rid: polygons[rid] for rid in active_rooms},
            voxel_size_m=0.1, voxel_grid_origin_xyz_m=(0.0, 0.0, 0.0),
            geometry_revisions={rid: revision for rid in active_rooms},
        ).as_dict()
        if membership_status == "unassigned":
            assert support["status"] == "unassigned"
        else:
            assert support["room_id"] == room_id
        evidence = RoomObjectEvidence(
            object_id="causal:chair", revision=revision,
            retained=membership_status == "assigned", semantic_status="stable", category="chair",
            membership_status=membership_status, room_id=room_id,
            source_observation_ids=(observation_ids[-1],), provenance="explicit-verified-scope-fixture",
            membership_evidence=support,
            semantic_evidence={"status": "stable", "category": "chair", "future_observations_used": False},
        )
        event = RoomBeliefEvent(f"fixture-event-{revision}", "causal:chair", revision,
                                evidence if membership_status == "assigned" else None)
        ledger.apply(event)
        if retired:
            # The retired room has no current node and no current semantic state.
            assert ledger.room_state("room:B").category_counts == ()
        room_states = {rid: ledger.room_state(rid).as_dict() for rid in active_rooms}
        causal_object = {
            "causal_object_id": "causal:chair", "snapshot_hypothesis_id": h,
            "observation_ids": observation_ids, "detection_count": len(observation_ids),
            "distinct_frame_count": len(observation_ids), "distinct_pose_count": len(observation_ids),
            "raw_class_history": ["chair"], "prefix_admission": {"eligible": True, "status": "eligible", "reason": "fixture gate met"},
            "semantic": {"status": "stable", "category": "chair", "view_group_count": frame + 1,
                         "eligible_observation_ids": observation_ids, "future_observations_used": False},
            "membership": {**support, "status": membership_status, "room_id": room_id},
            "room_evidence_eligible": membership_status == "assigned",
            "geometry_revision": f"geometry-{frame}",
        }
        edges = ([{"relation": "room_contains", "source_id": room_id,
                   "target_id": "causal:chair", "provenance": "rb1_fixture", "revision": revision}]
                 if membership_status == "assigned" else [])
        state = {
            "schema": "causal-hybrid-room-belief-revision/v1", "revision": revision,
            "frame_index": frame, "frame_id": object_doc["frame_id"], "timestamp_s": float(frame),
            "future_observations_used": False, "membership_causal": True,
            "membership_scope": {"status": "verified_single_observed_floor_scope", "reason": "explicit fixture scope"},
            "room_semantics_status": "causal", "objects": [causal_object],
            "rooms": {"mean_no_neutral": room_states}, "room_contains_edges": edges,
            "room_lifecycle": {"active_room_ids": active_rooms,
                               "created_room_ids": active_rooms if frame == 0 else [],
                               "retired_room_ids": retired, "room_revision": revision},
        }
        _write(causal / "revisions" / f"rev-{revision:06d}.json", state)

    _write(timeline / "frame_index.json", {"frames": frames})
    _write(timeline / "manifest.json", {
        "schema": "conceptgraphs-causal-object-timeline/v1", "scene_id": "fixture-scene",
        "sequence_id": "fixture-route", "snapshot_index": "frame_index.json", "revision_count": 3,
        "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference"},
    })
    cm = {"schema": "causal-hybrid-room-belief-timeline/v1", "scene_id": "fixture-scene",
          "sequence_id": "fixture-route", "source_timeline_manifest_sha256": _sha(timeline / "manifest.json"),
          "variants": ["mean_no_neutral"],
          "revisions": [{"frame_index": frame, "revision": frame + 1,
                         "state": f"revisions/rev-{frame+1:06d}.json"} for frame in range(3)]}
    _write(causal / "manifest.json", cm)
    return timeline, causal


def test_real_00802_unified_prefixes_preserve_unavailable_scope_and_no_membership_edges(tmp_path: Path):
    out = tmp_path / "unified"
    manifest = build_unified_timeline(TIMELINE, CAUSAL, out)
    states = replay_unified_snapshots(out)
    assert len(states) == 29 == manifest["revision_count"]
    timeline_manifest = json.loads((TIMELINE / "manifest.json").read_text())
    frame_rows = json.loads((TIMELINE / timeline_manifest["snapshot_index"]).read_text())["frames"]
    causal_manifest = json.loads((CAUSAL / "manifest.json").read_text())
    causal_rows = {int(row["frame_index"]): row for row in causal_manifest["revisions"]}
    for source_row, state in zip(frame_rows, states):
        object_snapshot = json.loads((TIMELINE / source_row["snapshot"]).read_text())
        room_doc = json.loads((TIMELINE / object_snapshot["rooms"]["state_ref"]).read_text())
        room_snapshot = room_doc.get("snapshot", room_doc)
        recomputed = build_unified_revision(TIMELINE, source_row, CAUSAL,
                                            causal_rows[int(source_row["frame_index"])],
                                            causal_manifest["variants"])
        assert state == recomputed
        source_objects = {obj["hypothesis_id"]: obj for obj in object_snapshot["objects"]}
        assert {obj["hypothesis_id"] for obj in state["objects"]} == set(source_objects)
        for obj in state["objects"]:
            source_obj = source_objects[obj["hypothesis_id"]]
            assert obj["geometry_ref"] == {
                "path": source_obj["cloud_npz"], "sha256": source_obj["cloud_sha256"],
                "point_count": source_obj["point_count"], "bounds_minmax": source_obj["bounds_minmax"],
            }
        source_rooms = {node["node_id"]: node for node in room_snapshot["nodes"] if node["kind"] == "room"}
        for room in state["rooms"]:
            attrs = source_rooms[room["node_id"]]["attributes"]
            assert room["geometry"] == {key: attrs[key] for key in ("outer_ring", "holes", "centroid_xy") if key in attrs}
        expected_room_edges = [edge for edge in room_snapshot.get("edges", [])
                               if edge.get("source_id") in source_rooms and edge.get("target_id") in source_rooms]
        actual_room_edges = [edge for edge in state["edges"] if edge["relation"] != "room_contains"]
        assert [(e["source_id"], e["target_id"], e["relation"]) for e in actual_room_edges] == [
            (e["source_id"], e["target_id"], e.get("relation")) for e in expected_room_edges
        ]
    for state in states:
        assert state["future_observations_used"] is False
        assert state["membership_availability"] == {
            "status": "unavailable", "reason": "unavailable_elevation_scope"}
        assert state["room_semantics_availability"] == {
            "status": "unavailable", "reason": "membership_unavailable_elevation_scope"}
        assert not [edge for edge in state["edges"] if edge["relation"] == "room_contains"]
        assert all(room["semantic_by_variant"]["mean_no_neutral"]["status"] == "unavailable"
                   and room["semantic_by_variant"]["mean_no_neutral"]["label"] is None
                   and room["semantic_by_variant"]["mean_no_neutral"]["beliefs"] is None
                   for room in state["rooms"])
        for obj in state["objects"]:
            assert all(int(oid.split(":", 1)[0].rsplit("-", 1)[-1]) <= state["frame_index"]
                       for oid in obj["observation_ids"])
        node_ids = {node["node_id"] for node in state["nodes"]}
        assert all(edge["source_id"] in node_ids and edge["target_id"] in node_ids for edge in state["edges"])
    final = states[-1]
    assert final["room_lifecycle"]["active_room_ids"] == [room["node_id"] for room in final["rooms"]]
    assert final["causality"]["membership"] == "unavailable_elevation_scope"


def test_verified_scope_fixture_membership_transfer_belief_retraction_and_room_retirement(tmp_path: Path):
    timeline, causal = _make_fixture(tmp_path)
    out = tmp_path / "unified-fixture"
    build_unified_timeline(timeline, causal, out)
    rev0, rev1, rev2 = replay_unified_snapshots(out)
    assert [(e["source_id"], e["target_id"]) for e in rev0["edges"] if e["relation"] == "room_contains"] == [
        ("room:A", "object:hypothesis_chair")]
    by_room0 = {room["node_id"]: room for room in rev0["rooms"]}
    assert by_room0["room:A"]["semantic_by_variant"]["mean_no_neutral"]["label"] == "living_room"
    assert [(e["source_id"], e["target_id"]) for e in rev1["edges"] if e["relation"] == "room_contains"] == [
        ("room:B", "object:hypothesis_chair")]
    by_room1 = {room["node_id"]: room for room in rev1["rooms"]}
    assert by_room1["room:A"]["semantic_by_variant"]["mean_no_neutral"]["status"] == "unknown"
    assert by_room1["room:B"]["semantic_by_variant"]["mean_no_neutral"]["label"] == "living_room"
    assert [room["node_id"] for room in rev2["rooms"]] == ["room:A"]
    assert not [edge for edge in rev2["edges"] if edge["relation"] == "room_contains"]
    assert "room:B" in rev2["room_lifecycle"]["retired_room_ids"]
    assert all(edge["source_id"] != "room:B" and edge["target_id"] != "room:B" for edge in rev2["edges"])
    assert rev0["rooms"][0]["semantic_by_variant"]["mean_no_neutral"]["label"] == "living_room"
    assert by_room1["room:B"]["semantic_by_variant"]["mean_no_neutral"]["label"] == "living_room"


def test_unified_builder_rejects_manifest_hash_mismatch(tmp_path: Path):
    timeline, causal = _make_fixture(tmp_path)
    manifest_path = timeline / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["causality"]["test_mutation"] = True
    _write(manifest_path, manifest)
    with pytest.raises(ValueError, match="exact object timeline manifest"):
        build_unified_timeline(timeline, causal, tmp_path / "bad-output")
