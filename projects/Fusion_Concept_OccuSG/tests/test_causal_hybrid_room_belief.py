from __future__ import annotations

import json
from pathlib import Path

from src.standalone_sg.semantics.room_belief_v1 import (
    ConsensusPolicy, RoomBeliefLedger, RoomBeliefPolicy, RoomBeliefEvent,
    RoomObjectEvidence, SemanticObservation, aggregate_object_semantics,
)


ROOT = Path(__file__).resolve().parents[1]
CAUSAL = ROOT / "artifacts/scene_graph_timeline/m4_causal_hybrid_00802_v1"


def test_causal_artifact_has_no_future_and_membership_gate_is_explicit():
    manifest = json.loads((CAUSAL / "manifest.json").read_text())
    assert manifest["causality"]["objects"] == "prefix_state_only"
    assert manifest["causality"]["membership"] == "unavailable_elevation_scope"
    assert manifest["membership_scope"]["floor_profile_metadata_ignored"] is True
    for row in manifest["revisions"]:
        state = json.loads((CAUSAL / row["state"]).read_text())
        assert state["future_observations_used"] is False
        assert state["room_contains_edges"] == []
        limit = int(state["frame_index"])
        for obj in state["objects"]:
            assert all(int(oid.split("-")[-2].split(":")[0]) <= limit
                       for oid in obj["observation_ids"])
            assert obj["membership"]["status"] == "unavailable"


def _ledger():
    policy = RoomBeliefPolicy("test", 2, 0.0, 0.0, 0.0, 1)
    return RoomBeliefLedger(
        room_ids=("kitchen", "living_room"), room_node_ids=("room:1",),
        room_priors={"kitchen": 1.0, "living_room": 1.0},
        category_room_scores={
            "chair": {"kitchen": 0.0, "living_room": 1.0},
            "sofa": {"kitchen": 0.0, "living_room": 1.0},
        }, label_policy=policy, evidence_policy_id="test", ontology_version="test", a_table_hash="test",
    )


def _evidence(object_id, category, room="room:1"):
    return RoomObjectEvidence(object_id, 1, True, "stable", category, "assigned", room,
                              source_observation_ids=(object_id + ":obs",), provenance="test")


def test_category_cap_and_retract_are_incremental_and_idempotent():
    ledger = _ledger()
    ledger.apply(RoomBeliefEvent("e1", "o1", 1, _evidence("o1", "chair")))
    one = ledger.room_state("room:1").as_dict()
    ledger.apply(RoomBeliefEvent("e2", "o2", 1, _evidence("o2", "chair")))
    duplicate_category = ledger.room_state("room:1").as_dict()
    assert duplicate_category["raw_scores"] == one["raw_scores"]
    assert duplicate_category["category_counts"] == {"chair": 2}
    assert ledger.apply(RoomBeliefEvent("e2", "o2", 1, _evidence("o2", "chair")))["status"] == "idempotent_replay"
    ledger.apply(RoomBeliefEvent("e3", "o3", 1, _evidence("o3", "sofa")))
    two_categories = ledger.room_state("room:1").as_dict()
    assert set(two_categories["category_counts"]) == {"chair", "sofa"}
    ledger.apply(RoomBeliefEvent("e2-retract", "o2", 2, None))
    assert ledger.room_state("room:1").as_dict()["category_counts"] == {"chair": 1, "sofa": 1}
    ledger.apply(RoomBeliefEvent("e1-retract", "o1", 2, None))
    assert ledger.room_state("room:1").as_dict()["category_counts"] == {"sofa": 1}
    ledger.apply(RoomBeliefEvent("e3-retract", "o3", 2, None))
    empty = ledger.room_state("room:1").as_dict()
    assert empty["category_counts"] == {}
    assert empty["label"] is None


def test_consensus_rejects_revisit_as_second_view_group():
    policy = ConsensusPolicy("test", "exact_pose", 2, 0.7, 0.2)
    obs = [
        SemanticObservation("a", "f0", "pose:one", "couch", "sofa"),
        SemanticObservation("b", "f1", "pose:one", "couch", "sofa"),
    ]
    result = aggregate_object_semantics("o", obs, semantic_revision=1, policy=policy)
    assert result.view_group_count == 1
    assert result.status == "provisional"


def test_verified_single_floor_scope_reuses_locked_source_provenance():
    causal = ROOT / "artifacts/scene_graph_timeline/m4_causal_hybrid_00802_v5"
    manifest = json.loads((causal / "manifest.json").read_text())
    scope = manifest["membership_scope"]
    assert manifest["causality"]["membership"] == "prefix_greedy_voxel_support"
    assert scope["status"] == "verified_single_observed_floor_scope"
    assert scope["observed_floor_ids"] == ["floor_0"]
    assert scope["observed_source_object_layer_ids"] == ["floor_0"]
    assert scope["evaluation_profile_floor_id"] == "floor_0"
    assert scope["multi_floor_safety"] is False
    state = json.loads((causal / "revisions/rev-000029.json").read_text())
    assert state["room_contains_edges"]
    assert any(s["label"] == "living_room" for s in state["rooms"]["mean_no_neutral"].values())
    assert any(s["label"] == "living_room" for s in state["rooms"]["sum_no_neutral"].values())
    assert all(o["membership"]["status"] in {"assigned", "unassigned"} for o in state["objects"])
