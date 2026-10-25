from copy import deepcopy

import pytest

from standalone_sg.contracts.errors import ContractError
from standalone_sg.contracts.regions import Polygon2D
from standalone_sg.semantics.room_belief_v1 import (
    ConsensusPolicy,
    RoomBeliefEvent,
    RoomBeliefLedger,
    RoomBeliefPolicy,
    RoomObjectEvidence,
    SemanticObservation,
    aggregate_object_semantics,
    greedy_voxel_support_membership,
)


CONSENSUS = ConsensusPolicy("test_consensus", "test_view_groups", 2, 0.7, 0.2)
POLICY = RoomBeliefPolicy("test_room_policy", 1, 0.5, 0.05, 0.1, 2)


def _ledger(*, a=None, policy=POLICY):
    return RoomBeliefLedger(
        room_ids=("bedroom", "living_room"),
        room_priors={"bedroom": 0.5, "living_room": 0.5},
        category_room_scores=a or {
            "sofa": {"bedroom": 0.0, "living_room": 1.0},
            "bed": {"bedroom": 2.0, "living_room": 0.0},
            "nightstand": {"bedroom": 1.0, "living_room": 0.0},
            "chair": {"bedroom": 0.0, "living_room": 0.1},
        },
        label_policy=policy, evidence_policy_id="evidence_v1_test", ontology_version="ontology_test_v1",
        a_table_hash="test-a-table-hash",
    )


def _ev(object_id, revision, category, *, room="living_room", status="stable", retained=True,
        membership="assigned", observations=()):
    if membership != "assigned":
        room = None
    return RoomObjectEvidence(object_id, revision, retained, status, category, membership, room,
                              tuple(observations), "unit-test")


def _event(event_id, evidence, *, object_id=None, revision=None):
    return RoomBeliefEvent(event_id, object_id or evidence.object_id,
                           evidence.revision if revision is None and evidence else (revision or 0), evidence)


def _voxel_membership(*args, **kwargs):
    return greedy_voxel_support_membership(*args, voxel_grid_origin_xyz_m=(0.0, 0.0, 0.0), **kwargs)


def _consensus(object_id, observations):
    return aggregate_object_semantics(object_id, observations, semantic_revision=1, policy=CONSENSUS)


def test_consensus_counts_a_view_group_once_and_excludes_alias_deferred_filtered():
    observations = [
        SemanticObservation(f"o{i}", f"f{i}", "pose-a", "chair", "chair") for i in range(12)
    ] + [
        SemanticObservation("alias", "f12", "pose-b", "chair", "chair", alias_of="o11"),
        SemanticObservation("deferred", "f13", "pose-c", "chair", "chair", admission_status="deferred"),
        SemanticObservation("filtered", "f14", "pose-d", "chair", "chair", admission_status="filtered"),
        SemanticObservation("valid2", "f15", "pose-e", "chair", "chair"),
    ]
    result = _consensus("obj-1", observations)
    assert result.status == "stable"
    assert result.category == "chair"
    assert result.view_group_count == 2
    assert result.category_support == (("chair", 2.0),)
    assert dict(result.excluded_observations) == {
        "alias": "duplicate_alias", "deferred": "deferred", "filtered": "filtered"
    }


def test_consensus_preserves_conflict_and_does_not_infer_synonyms():
    observations = [
        SemanticObservation("a", "f1", "pose-1", "couch", "sofa"),
        SemanticObservation("b", "f2", "pose-2", "couch", "sofa"),
        SemanticObservation("c", "f3", "pose-3", "bed", "bed"),
    ]
    result = _consensus("obj", observations)
    assert result.status == "conflicted"
    assert result.category is None
    assert dict(result.category_support) == {"sofa": 2.0, "bed": 1.0}
    assert result.eligible_observation_ids == ("a", "b", "c")


def test_consensus_with_one_view_is_provisional_and_missing_class_is_unknown():
    one = _consensus("obj", [SemanticObservation("a", "f", "same", "sofa", "sofa")])
    none = _consensus("obj", [SemanticObservation("b", "f", "same", None, None)])
    assert (one.status, one.category) == ("provisional", None)
    assert (none.status, none.category) == ("unknown", None)


def test_consensus_rejects_duplicate_observation_id_and_invalid_thresholds():
    observation = SemanticObservation("x", "f", "v", "chair", "chair")
    with pytest.raises(ContractError, match="unique"):
        _consensus("obj", [observation, observation])
    with pytest.raises(ContractError):
        ConsensusPolicy("bad", "groups", 0, 0.7, 0.2)


def test_consensus_serialization_keeps_raw_class_view_group_and_revision():
    observations = [SemanticObservation("o1", "f1", "v1", "couch", "sofa"),
                    SemanticObservation("o2", "f2", "v2", "couch", "sofa")]
    state = aggregate_object_semantics("obj", observations, semantic_revision=4, policy=CONSENSUS)
    restored = type(state).from_dict(state.as_dict())
    assert restored == state
    assert restored.raw_observations[0].raw_class == "couch"
    assert restored.semantic_revision == 4


def test_voxel_membership_uses_support_not_center_and_records_outside_fraction():
    rooms = {
        "room-a": Polygon2D(((0, 0), (2, 0), (2, 2), (0, 2))),
        "room-b": Polygon2D(((2, 0), (4, 0), (4, 2), (2, 2))),
    }
    points = [(0.2, 0.5, 0.1), (0.4, 0.5, 0.1), (2.2, 0.5, 0.1), (8.0, 0.5, 0.1)]
    result = _voxel_membership("o", points, rooms, voxel_size_m=0.1,
                               geometry_revisions={"room-a": 3, "room-b": 4})
    assert result.room_id == "room-a"
    assert result.support_counts == (("room-a", 2), ("room-b", 1))
    assert result.support_fraction == 0.5
    assert result.runner_up_fraction == 0.25
    assert result.outside_fraction == 0.25
    assert dict(result.geometry_revisions) == {"room-a": 3, "room-b": 4}


def test_voxel_membership_ties_prefer_previous_owner_then_deterministic_id():
    rooms = {
        "a": Polygon2D(((0, 0), (2, 0), (2, 2), (0, 2))),
        "b": Polygon2D(((0, 0), (2, 0), (2, 2), (0, 2))),
    }
    points = [(0.5, 0.5, 0.0)]
    assert _voxel_membership("o", points, rooms, voxel_size_m=0.1,
                             previous_room_id="b").room_id == "b"
    no_previous = _voxel_membership("o", points, rooms, voxel_size_m=0.1)
    assert no_previous.room_id == "a" and no_previous.tied


def test_voxel_membership_unassigned_empty_outside_and_bad_resolution():
    polygon = Polygon2D(((0, 0), (1, 0), (1, 1), (0, 1)))
    assert _voxel_membership("empty", [], {"r": polygon}, voxel_size_m=0.1).status == "unassigned"
    no_rooms = _voxel_membership("no-room-geometry", [(2, 2, 0)], {}, voxel_size_m=0.1)
    assert no_rooms.reason == "room_geometry_unavailable" and no_rooms.outside_fraction == 0.0
    outside = _voxel_membership("outside", [(2, 2, 0)], {"r": polygon}, voxel_size_m=0.1)
    assert outside.room_id is None and outside.outside_fraction == 1.0
    with pytest.raises(ContractError, match="duplicate voxel"):
        _voxel_membership("dupe", [(0.01, 0.01, 0), (0.02, 0.02, 0)],
                          {"r": polygon}, voxel_size_m=0.1)
    with pytest.raises(ContractError, match="positive"):
        _voxel_membership("bad", [], {"r": polygon}, voxel_size_m=0)


def test_category_presence_caps_many_objects_and_removal_is_incremental():
    ledger = _ledger()
    for i in range(12):
        ledger.apply(_event(f"chair-event-{i}", _ev(f"chair-{i}", 1, "chair")))
    state1 = ledger.room_state("living_room")
    assert dict(state1.category_counts) == {"chair": 12}
    expected_score = dict(state1.raw_scores)
    ledger.apply(_event("remove-one-chair", _ev("chair-0", 2, "chair", retained=False)))
    state2 = ledger.room_state("living_room")
    assert dict(state2.category_counts) == {"chair": 11}
    assert dict(state2.raw_scores) == expected_score  # one category contribution remains
    for i in range(1, 12):
        ledger.apply(RoomBeliefEvent(f"remove-chair-{i}", f"chair-{i}", 2, None))
    assert ledger.room_state("living_room").category_counts == ()


def test_same_object_class_correction_retracts_old_category_contribution():
    ledger = _ledger()
    ledger.apply(_event("class-v1", _ev("obj", 1, "sofa")))
    assert dict(ledger.room_state("living_room").category_counts) == {"sofa": 1}
    ledger.apply(_event("class-v2", _ev("obj", 2, "bed")))
    assert dict(ledger.room_state("living_room").category_counts) == {"bed": 1}
    assert "sofa" not in ledger.category_members["living_room"]


def test_membership_evidence_and_observation_lineage_survive_event_serialization():
    ledger = _ledger()
    membership = {
        "object_id": "chair", "room_id": "living_room", "status": "assigned",
        "support_fraction": 0.8, "runner_up_fraction": 0.2, "outside_fraction": 0.0,
        "geometry_revisions": {"living_room": 7},
    }
    semantic = _consensus("chair", [
        SemanticObservation("obs-a", "frame-a", "pose-a", "chair", "chair"),
        SemanticObservation("obs-b", "frame-b", "pose-b", "chair", "chair"),
    ]).as_dict()
    evidence = RoomObjectEvidence(
        "chair", 1, True, "stable", "chair", "assigned", "living_room",
        ("obs-a", "obs-b"), "hybrid_test", membership, semantic,
    )
    ledger.apply(_event("membership-evidence", evidence))
    restored = RoomBeliefLedger.from_dict(ledger.as_dict())
    assert restored.object_evidence("chair").membership_evidence == membership
    assert restored.object_evidence("chair").source_observation_ids == ("obs-a", "obs-b")
    assert restored.object_evidence("chair").semantic_evidence == semantic


def test_only_retained_stable_assigned_objects_with_a_row_contribute():
    ledger = _ledger()
    cases = [
        _ev("filtered", 1, "chair", retained=False),
        _ev("provisional", 1, "chair", status="provisional"),
        _ev("ambiguous", 1, "chair", membership="ambiguous"),
        _ev("unsupported", 1, "table"),
    ]
    for i, evidence in enumerate(cases):
        ledger.apply(_event(f"e{i}", evidence))
    state = ledger.room_state("living_room")
    assert state.category_counts == ()
    assert state.label is None and state.status == "unknown"


def test_upsert_revision_stale_rejection_and_payload_conflicts():
    ledger = _ledger()
    first = _event("event-1", _ev("chair", 2, "chair"))
    assert ledger.apply(first)["status"] == "applied"
    assert ledger.apply(first)["status"] == "idempotent_replay"
    assert ledger.apply(_event("old", _ev("chair", 1, "sofa")))["status"] == "stale_rejected"
    assert ledger.object_evidence("chair").category == "chair"
    with pytest.raises(ContractError, match="same producer revision"):
        ledger.apply(_event("conflict", _ev("chair", 2, "sofa")))
    with pytest.raises(ContractError, match="event_id"):
        ledger.apply(first.__class__("event-1", "chair", 2, _ev("chair", 2, "sofa")))


def test_class_correction_retracts_old_category_and_atomic_room_transfer():
    ledger = _ledger()
    ledger.apply(_event("1", _ev("obj", 1, "sofa", room="living_room")))
    ledger.apply(_event("2", _ev("obj", 2, "bed", room="bedroom")))
    assert ledger.room_state("living_room").category_counts == ()
    assert dict(ledger.room_state("bedroom").category_counts) == {"bed": 1}
    assert ledger.category_members["living_room"] == {}
    assert ledger.category_members["bedroom"] == {"bed": ("obj",)}


def test_relabel_hysteresis_counts_only_evidence_changes_and_retracts_to_unknown():
    policy = RoomBeliefPolicy("relabel-test", 1, 0.55, 0.1, 0.1, 2)
    ledger = _ledger(policy=policy)
    ledger.apply(_event("sofa", _ev("sofa-obj", 1, "sofa")))
    assert ledger.room_state("living_room").label == "living_room"
    ledger.apply(_event("bed", _ev("bed-obj", 1, "bed", room="living_room")))
    pending = ledger.room_state("living_room")
    assert pending.label is None and pending.last_supported_label == "living_room"
    assert pending.status == "provisional" and pending.pending_label == "bedroom"
    assert pending.pending_revision_count == 1
    # An inference/event that does not change the eligible category/object set
    # must not advance relabel hysteresis.
    ledger.apply(_event("same-support-new-revision", _ev("bed-obj", 2, "bed", room="living_room",
                                                           observations=("new-frame",))))
    assert ledger.room_state("living_room").pending_revision_count == 1
    ledger.apply(_event("nightstand", _ev("nightstand-obj", 1, "nightstand", room="living_room")))
    changed = ledger.room_state("living_room")
    assert changed.label == "bedroom" and changed.status == "predicted"
    assert changed.label_history[-1] == (3, "living_room", "bedroom", "relabel_hysteresis_satisfied")
    ledger.apply(RoomBeliefEvent("remove-bedroom-evidence", "bed-obj", 3, None))
    ledger.apply(RoomBeliefEvent("remove-nightstand-evidence", "nightstand-obj", 2, None))
    final = ledger.room_state("living_room")
    assert final.label is None and final.status == "provisional"
    assert final.last_supported_label == "bedroom"
    ledger.apply(RoomBeliefEvent("remove-sofa", "sofa-obj", 2, None))
    empty = ledger.room_state("living_room")
    assert empty.label is None and empty.status == "unknown"
    assert empty.last_supported_label == "bedroom"


def test_serialization_replay_preserves_ledger_and_rejects_tampering():
    ledger = _ledger()
    ledger.apply(_event("a", _ev("chair", 1, "chair")))
    ledger.apply(_event("b", _ev("sofa", 1, "sofa")))
    serialized = ledger.as_dict()
    restored = RoomBeliefLedger.from_dict(serialized)
    assert restored.as_dict() == serialized
    corrupt = deepcopy(serialized)
    corrupt["events"][0]["payload_hash"] = "bad"
    with pytest.raises(ContractError, match="hash"):
        RoomBeliefLedger.from_dict(corrupt)


def test_missing_a_row_and_malformed_serialization_are_not_silently_accepted():
    ledger = _ledger()
    bad = ledger.as_dict()
    bad["room_states"]["living_room"]["label"] = "bedroom"
    with pytest.raises(ContractError, match="does not match"):
        RoomBeliefLedger.from_dict(bad)
    with pytest.raises(ContractError, match="exactly all"):
        _ledger(a={"chair": {"living_room": 1.0}})


def test_failed_overflow_during_room_transfer_does_not_partially_commit():
    ledger = _ledger(a={
        "sofa": {"bedroom": 0.0, "living_room": 1.0},
        "bed": {"bedroom": 1e308, "living_room": 0.0},
        "nightstand": {"bedroom": 1e308, "living_room": 0.0},
    })
    ledger.apply(_event("nightstand-existing", _ev("nightstand", 1, "nightstand", room="bedroom")))
    ledger.apply(_event("original", _ev("obj", 1, "sofa", room="living_room")))
    with pytest.raises(ContractError, match="overflowed"):
        ledger.apply(_event("transfer-overflow", _ev("obj", 2, "bed", room="bedroom")))
    assert ledger.object_evidence("obj").category == "sofa"
    assert dict(ledger.room_state("living_room").category_counts) == {"sofa": 1}
    assert dict(ledger.room_state("bedroom").category_counts) == {"nightstand": 1}


def test_apply_frame_is_order_invariant_and_relabels_once_per_room():
    events = [
        _event("frame-bed", _ev("bed-obj", 7, "bed", room="bedroom")),
        _event("frame-chair", _ev("chair-obj", 7, "chair", room="living_room")),
    ]
    first, second = _ledger(), _ledger()
    first.apply_frame(events)
    second.apply_frame(list(reversed(events)))
    assert first.as_dict() == second.as_dict()
    assert first.room_state("bedroom").evidence_revision == 1
    assert first.room_state("living_room").evidence_revision == 1


def test_apply_frame_failure_rolls_back_all_objects_and_labels():
    ledger = _ledger(a={
        "sofa": {"bedroom": 0.0, "living_room": 1.0},
        "bed": {"bedroom": 1e308, "living_room": 0.0},
        "nightstand": {"bedroom": 1e308, "living_room": 0.0},
    })
    before = ledger.as_dict()
    events = [
        _event("valid", _ev("valid", 3, "sofa", room="living_room")),
        _event("overflow", _ev("overflow", 3, "bed", room="bedroom")),
        _event("overflow-2", _ev("overflow-2", 3, "nightstand", room="bedroom")),
    ]
    with pytest.raises(ContractError, match="overflowed"):
        ledger.apply_frame(events)
    assert ledger.as_dict() == before


def test_apply_frame_results_follow_event_id_and_idempotent_replay_order():
    ledger = _ledger()
    events = [
        _event("z-event", _ev("z-object", 4, "sofa")),
        _event("a-event", _ev("a-object", 4, "bed", room="bedroom")),
    ]
    results = ledger.apply_frame(events)
    assert [row["event_id"] for row in results] == ["z-event", "a-event"]
    assert all(row["status"] == "applied" for row in results)
    replay = ledger.apply_frame(list(reversed(events)))
    assert [row["event_id"] for row in replay] == ["a-event", "z-event"]
    assert all(row["status"] == "idempotent_replay" for row in replay)


def test_room_registry_is_created_by_active_revision_not_final_catalog():
    ledger = RoomBeliefLedger(
        room_ids=("bedroom", "living_room"),
        room_priors={"bedroom": 0.5, "living_room": 0.5},
        category_room_scores={"sofa": {"bedroom": 0.0, "living_room": 1.0}},
        label_policy=POLICY, evidence_policy_id="evidence_v1_test", ontology_version="ontology_test_v1",
        a_table_hash="test-a-table-hash", room_node_ids=(), allow_empty_room_nodes=True,
    )
    assert ledger.room_node_ids == ()
    ledger.register_room_node("room:1")
    ledger.apply_frame([RoomBeliefEvent("room-event", "obj", 1, _ev("obj", 1, "sofa", room="room:1"))])
    assert ledger.room_state("room:1").category_counts == (("sofa", 1),)
    ledger.register_room_node("room:2")
    assert ledger.room_node_ids == ("room:1", "room:2")
