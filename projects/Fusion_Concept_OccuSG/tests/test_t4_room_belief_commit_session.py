from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from integration_v2.room_belief_commit_session import GraphCommitEnvelope, RoomBeliefCommitSession, VARIANTS
from standalone_sg.semantics.room_belief_v1 import ConsensusPolicy, RoomBeliefPolicy


ROOM_TYPES = ("bathroom", "bedroom", "dining_room", "entryway", "kitchen", "living_room", "office", "other")


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def session(tmp_path: Path, *, admission_min: int = 3, room_count: int = 8) -> RoomBeliefCommitSession:
    room_types = ROOM_TYPES[:room_count]
    categories = ("chair", "sofa")
    tables = {}
    digests = {}
    for variant in VARIANTS:
        tables[variant] = {
            "chair": {room: (1.0 if room == "living_room" else 0.0) for room in room_types},
            "sofa": {room: (1.0 if room == "living_room" else 0.0) for room in room_types},
        }
        digests[variant] = sha("a-table-" + variant)
    return RoomBeliefCommitSession.from_resolved(
        config={"scope_policy": {"mode": "verified_only", "assumed_declaration": None}},
        tables=tables, table_digests=digests, categories=categories, semantic_rooms=room_types,
        aliases={"couch": "sofa"},
        consensus_policy=ConsensusPolicy("fixture-consensus", "exact_camera_pose_matrix_v1", 2, 0.7, 0.2),
        label_policy=RoomBeliefPolicy("fixture-policy", 2, 0.2, 0.01, 0.15, 2),
        admission_min_detections=admission_min,
        config_provenance={"source": "synthetic_model_free_fixture"}, overlay_root=tmp_path / "overlays")


def pose(x: float) -> list[list[float]]:
    return [[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 1.0], [0.0, 0.0, 0.0, 1.0]]


def make_envelope(tmp_path: Path, s: RoomBeliefCommitSession, frame: int, *, objects: list[dict],
                  obs_rows: list[dict], rooms: tuple[str, ...] = ("room:1",),
                  scope_status: str = "verified", room_revision: int | None = None,
                  event_id: str | None = None, timestamp: float | None = None) -> dict:
    unified_revision = frame + 1
    object_revision = frame * 3 + 4
    room_revision = frame + 2 if room_revision is None else room_revision
    graph_path = tmp_path / f"graph-{unified_revision}.json"
    graph = {
        "graph_revision_id": f"unified-{unified_revision:06d}", "scene_id": "scene-fixture",
        "sequence_id": "route-fixture", "frame_id": f"frame-{frame}", "frame_index": frame,
        "timestamp_s": float(frame + 1) if timestamp is None else timestamp,
        "object_revision": object_revision, "room_revision": room_revision,
        "source_revisions": {"object": {"state_sha256": sha(f"object-{frame}")},
                             "room": {"state_sha256": sha(f"room-{frame}")}},
        "objects": [{"causal_object_id": row["object_id"]} for row in objects],
        "rooms": [{"node_id": room_id} for room_id in rooms],
        "future_observations_used": False,
    }
    graph_path.write_text(json.dumps(graph, sort_keys=True) + "\n", encoding="utf-8")
    top_hashes = {"perception_packet": sha(f"packet-{frame}"), "object_state": sha(f"object-{frame}"),
                  "room_state": sha(f"room-{frame}")}
    for row in obs_rows:
        row.setdefault("source_hashes", {"rgb": sha(f"rgb-{frame}"), "mask": sha(f"mask-{row['observation_id']}")})
    for row in objects:
        membership = row.setdefault("membership", {"status": "assigned", "room_id": "room:1"})
        if membership.get("status") == "assigned":
            membership.update({"policy": "greedy_voxel_support_v1", "scope_status": scope_status,
                               "room_revision": room_revision, "object_revision": object_revision,
                               "support_fraction": 0.8, "runner_up_fraction": 0.1, "outside_fraction": 0.1,
                               "support_counts": {membership["room_id"]: 8}, "tied": False,
                               "geometry_revisions": {membership["room_id"]: room_revision}})
    return {
        "event_id": event_id or f"event-{unified_revision}", "run_id": "run-fixture", "scene_id": "scene-fixture",
        "sequence_id": "route-fixture", "frame_index": frame, "frame_id": f"frame-{frame}",
        "timestamp_s": float(frame + 1) if timestamp is None else timestamp,
        "unified_revision": unified_revision, "graph_revision_id": graph["graph_revision_id"],
        "graph_path": str(graph_path), "graph_sha256": hashlib.sha256(graph_path.read_bytes()).hexdigest(),
        "object_revision": object_revision, "object_state_sha256": sha(f"object-{frame}"),
        "room_revision": room_revision, "room_state_sha256": sha(f"room-{frame}"),
        "source_hashes": top_hashes, "current_observations": obs_rows, "objects": objects,
        "rooms": [{"node_id": room_id} for room_id in rooms],
        "scope": {"status": scope_status, "evidence_status": "verified" if scope_status == "verified" else "assumed_not_verified",
                  "reason": "unavailable_elevation_scope"},
    }


def frame_payload(frame: int, object_to_category: dict[str, str], *, active_rooms=("room:1",), room_by_object=None,
                  same_pose=False) -> tuple[list[dict], list[dict]]:
    room_by_object = room_by_object or {}
    current = []
    objects = []
    for object_id, raw_class in object_to_category.items():
        oid = f"{object_id}:obs-{frame}"
        current.append({"observation_id": oid, "frame_id": f"frame-{frame}", "frame_index": frame,
                        "raw_class": "couch" if raw_class == "sofa" else raw_class,
                        "camera_pose_W_Copt": pose(0.0 if same_pose else float(frame)),
                        "source_hashes": {"rgb": sha(f"rgb-{frame}"), "mask": sha(f"mask-{oid}")}})
        ids = [f"{object_id}:obs-{i}" for i in range(frame + 1)]
        room_id = room_by_object.get(object_id, active_rooms[0])
        objects.append({"object_id": object_id, "observation_ids": ids, "detection_count": frame + 1,
                        "membership": {"status": "assigned", "room_id": room_id}})
    return current, objects


def make_session_frame(tmp_path: Path, s: RoomBeliefCommitSession, frame: int, object_to_category: dict[str, str],
                       *, active_rooms=("room:1",), room_by_object=None, same_pose=False,
                       scope_status="verified", room_revision=None) -> dict:
    observations, objects = frame_payload(frame, object_to_category, active_rooms=active_rooms,
                                          room_by_object=room_by_object, same_pose=same_pose)
    return make_envelope(tmp_path, s, frame, objects=objects, obs_rows=observations,
                         rooms=active_rooms, scope_status=scope_status, room_revision=room_revision)


def test_open_resolves_pinned_runtime_contract_without_loading_models(tmp_path: Path):
    from integration_v2.room_belief_commit_session import ROOT
    opened = RoomBeliefCommitSession.open(config_path=ROOT / "configs/room_semantics/t4_live_room_belief_v1.json",
        overlay_root=tmp_path / "overlays", admission_min_detections=3)
    assert opened.config_provenance["a_tables_archive_sha256"] == "0e30cc12058ec22372fe8b2df49279ec6a917027bef74182f8831374a7418997"
    assert opened.config_provenance["view_group_policy_id"] == "exact_camera_pose_matrix_v1"
    assert opened.config["scope_policy"] == {"mode": "verified_only", "assumed_declaration": None}
    assert opened.status == "open"
    opened.close()


def test_prefix_consensus_and_category_cap_match_rb1_core(tmp_path: Path):
    s = session(tmp_path)
    active = {"sofa-a": "sofa", "sofa-b": "sofa", "chair-a": "chair"}
    result = None
    for i in range(3):
        result = s.update_after_unified_commit(make_session_frame(tmp_path, s, i, active))
    overlay = result["overlay"]
    assert overlay["scope_status"] == "verified"
    for variant in VARIANTS:
        room = overlay["room_diagnostics"][variant]["room:1"]
        assert room["status"] == "predicted"
        assert room["candidate_label"] == "living_room"
        assert room["committed_label"] == "living_room"
        assert room["eligible_categories"] == ["chair", "sofa"]
        sofa = next(row for row in room["category_contributions"] if row["category"] == "sofa")
        assert sofa["object_ids"] == ["sofa-a", "sofa-b"]
        assert len(room["category_contributions"]) == 2
    # Consensus is the same existing RB1/T3 aggregation function, with exact-pose view groups.
    from standalone_sg.semantics.room_belief_v1 import SemanticObservation, aggregate_object_semantics
    policy = s.consensus_policy
    records = [SemanticObservation(f"sofa-a:obs-{i}", f"frame-{i}", f"pose_exact:{hashlib.sha256(json.dumps(pose(float(i)),sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16]}", "couch", "sofa") for i in range(3)]
    expected = aggregate_object_semantics("sofa-a", records, semantic_revision=3, policy=policy)
    assert overlay["objects"][0]["semantic_consensus"] == expected.as_dict() | {"future_observations_used": False}


def test_retracting_one_and_then_last_same_category_object(tmp_path: Path):
    s = session(tmp_path)
    initial = {"sofa-a": "sofa", "sofa-b": "sofa", "chair-a": "chair"}
    for i in range(3):
        result = s.update_after_unified_commit(make_session_frame(tmp_path, s, i, initial))
    assert result["overlay"]["room_diagnostics"][VARIANTS[0]]["room:1"]["eligible_categories"] == ["chair", "sofa"]
    result = s.update_after_unified_commit(make_session_frame(tmp_path, s, 3, {"sofa-b": "sofa", "chair-a": "chair"}))
    room = result["overlay"]["room_diagnostics"][VARIANTS[0]]["room:1"]
    sofa = next(row for row in room["category_contributions"] if row["category"] == "sofa")
    assert sofa["object_ids"] == ["sofa-b"]
    assert room["eligible_categories"] == ["chair", "sofa"]
    result = s.update_after_unified_commit(make_session_frame(tmp_path, s, 4, {"chair-a": "chair"}))
    room = result["overlay"]["room_diagnostics"][VARIANTS[0]]["room:1"]
    assert room["eligible_categories"] == ["chair"]
    assert room["committed_label"] is None
    assert room["abstain_reason"] == "insufficient_support"
    assert room["label_history_delta"][-1]["reason"] == "label_gate_no_longer_met"


def test_verified_scope_unavailable_is_not_scored_not_abstained(tmp_path: Path):
    s = session(tmp_path)
    observations, objects = frame_payload(0, {"chair-a": "chair"})
    envelope = make_envelope(tmp_path, s, 0, objects=objects, obs_rows=observations,
        scope_status="unavailable")
    result = s.update_after_unified_commit(envelope)["overlay"]
    assert result["membership_availability"]["status"] == "unavailable"
    assert result["room_semantics_availability"]["status"] == "not_scored"
    assert result["room_contains_edges"] == []
    room = result["room_diagnostics"][VARIANTS[0]]["room:1"]
    assert room["not_scored"] is True
    assert room["candidate_label"] is None and room["abstain_reason"] is None


def test_same_pose_revisits_do_not_create_multiple_view_groups(tmp_path: Path):
    s = session(tmp_path)
    active = {"chair-a": "chair"}
    for i in range(3):
        result = s.update_after_unified_commit(make_session_frame(tmp_path, s, i, active, same_pose=True))
    consensus = result["overlay"]["objects"][0]["semantic_consensus"]
    assert consensus["view_group_count"] == 1
    assert consensus["status"] == "provisional"
    room = result["overlay"]["room_diagnostics"][VARIANTS[0]]["room:1"]
    assert room["eligible_categories"] == []


def test_class_conflict_then_recovery_is_prefix_local(tmp_path: Path):
    s = session(tmp_path)
    classes = ("chair", "sofa", "chair", "chair")
    result = None
    for frame, raw_class in enumerate(classes):
        oid = f"chair-a:obs-{frame}"
        obs = [{"observation_id": oid, "frame_id": f"frame-{frame}", "frame_index": frame,
                "raw_class": "couch" if raw_class == "sofa" else raw_class,
                "camera_pose_W_Copt": pose(float(frame)),
                "source_hashes": {"rgb": sha(f"rgb-{frame}"), "mask": sha(f"mask-{oid}")}}]
        object_row = {"object_id": "chair-a", "observation_ids": [f"chair-a:obs-{i}" for i in range(frame + 1)],
                      "detection_count": frame + 1, "membership": {"status": "assigned", "room_id": "room:1"}}
        result = s.update_after_unified_commit(make_envelope(tmp_path, s, frame, objects=[object_row], obs_rows=obs))
        consensus = result["overlay"]["objects"][0]["semantic_consensus"]
        if frame == 2:
            assert consensus["status"] == "conflicted"
            assert consensus["category"] is None
            assert result["overlay"]["room_diagnostics"][VARIANTS[0]]["room:1"]["eligible_categories"] == []
    assert result["overlay"]["objects"][0]["semantic_consensus"]["status"] == "stable"
    assert result["overlay"]["objects"][0]["semantic_consensus"]["category"] == "chair"


def test_empty_detection_frame_is_valid_and_emits_no_fake_evidence(tmp_path: Path):
    s = session(tmp_path)
    result = s.update_after_unified_commit(make_envelope(tmp_path, s, 0, objects=[], obs_rows=[],
        rooms=("room:1",), scope_status="unavailable"))["overlay"]
    assert result["status"] == "complete"
    assert result["objects"] == []
    assert result["room_contains_edges"] == []
    assert result["room_diagnostics"][VARIANTS[0]]["room:1"]["not_scored"] is True


def test_assumed_scope_from_another_run_does_not_open_live_gate(tmp_path: Path):
    s = session(tmp_path)
    observations, objects = frame_payload(0, {"chair-a": "chair"})
    envelope = make_envelope(tmp_path, s, 0, objects=objects, obs_rows=observations,
        scope_status="assumed")
    envelope["scope"].update({"declaration_id": "t1-replay-only", "declaration_sha256": "d" * 64,
                              "run_id": "another-run", "scene_id": "scene-fixture",
                              "sequence_id": "route-fixture"})
    result = s.update_after_unified_commit(envelope)["overlay"]
    assert result["scope_status"] == "unavailable"
    assert result["room_semantics_availability"]["status"] == "not_scored"
    assert result["room_contains_edges"] == []
    assert result["room_diagnostics"][VARIANTS[0]]["room:1"]["abstain_reason"] is None


def test_room_transfer_and_retirement_retracts_old_room_contribution(tmp_path: Path):
    s = session(tmp_path)
    active = {"sofa-a": "sofa", "chair-a": "chair"}
    for i in range(3):
        result = s.update_after_unified_commit(make_session_frame(tmp_path, s, i, active))
    current, objects = frame_payload(3, active, active_rooms=("room:2",),
                                     room_by_object={"sofa-a": "room:2", "chair-a": "room:2"})
    result = s.update_after_unified_commit(make_envelope(tmp_path, s, 3, objects=objects, obs_rows=current,
        rooms=("room:2",)))
    overlay = result["overlay"]
    assert overlay["room_lifecycle"]["retired_room_ids"] == ["room:1"]
    assert overlay["room_contains_edges"] and {e["source_id"] for e in overlay["room_contains_edges"]} == {"room:2"}
    assert "room:1" not in overlay["room_diagnostics"][VARIANTS[0]]
    assert overlay["room_diagnostics"][VARIANTS[0]]["room:2"]["eligible_categories"] == ["chair", "sofa"]


def test_duplicate_idempotency_conflict_stale_and_graph_hash_rejection(tmp_path: Path):
    s = session(tmp_path)
    envelope = make_session_frame(tmp_path, s, 0, {"chair-a": "chair"})
    first = s.update_after_unified_commit(envelope)
    before = s._ledgers[VARIANTS[0]].as_dict()
    duplicate = s.update_after_unified_commit(envelope)
    assert duplicate["idempotent_replay"] is True
    assert s._ledgers[VARIANTS[0]].as_dict() == before
    conflict = copy.deepcopy(envelope)
    conflict["timestamp_s"] += 0.125
    with pytest.raises(ValueError, match="different envelope hash"):
        s.update_after_unified_commit(conflict)
    stale = copy.deepcopy(envelope)
    stale["event_id"] = "stale-new-event"
    with pytest.raises(ValueError, match="stale/out-of-order unified revision"):
        s.update_after_unified_commit(stale)
    assert s._ledgers[VARIANTS[0]].as_dict() == before
    bad = make_session_frame(tmp_path, s, 1, {"chair-a": "chair"})
    bad["graph_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="missing or hash-mismatched"):
        s.update_after_unified_commit(bad)
    assert s._ledgers[VARIANTS[0]].as_dict() == before


def test_failure_overlay_binds_current_graph_without_stale_labels(tmp_path: Path):
    s = session(tmp_path)
    envelope = make_session_frame(tmp_path, s, 0, {"chair-a": "chair"})
    failure = s.write_failure_overlay(envelope, RuntimeError("fixture failure"))["overlay"]
    assert failure["status"] == "failed"
    assert failure["graph_sha256"] == envelope["graph_sha256"]
    assert failure["room_semantics_availability"]["status"] == "failed"
    assert failure["room_contains_edges"] == []
    assert failure["rooms"] == {variant: {} for variant in VARIANTS}


def test_room_retirement_keeps_terminal_state_separate(tmp_path: Path):
    s = session(tmp_path)
    active = {"sofa-a": "sofa", "chair-a": "chair"}
    for i in range(3):
        s.update_after_unified_commit(make_session_frame(tmp_path, s, i, active))
    current, _objects = frame_payload(3, {})
    terminal = make_envelope(tmp_path, s, 3, objects=[], obs_rows=current, rooms=())
    result = s.update_after_unified_commit(terminal)["overlay"]
    assert result["room_lifecycle"]["retired_room_ids"] == ["room:1"]
    terminal_doc = s.finalize({"stage": "terminal_postprocess", "lineage": None})["document"]
    assert terminal_doc["status"] == "terminal_semantics_unavailable"
    assert terminal_doc["prefix_ledgers_modified"] is False
