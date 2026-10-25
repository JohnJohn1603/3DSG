import json
import math
from pathlib import Path

from standalone_sg.semantics.room_belief_v1 import (
    RoomBeliefEvent,
    RoomBeliefLedger,
    RoomBeliefPolicy,
    RoomObjectEvidence,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = RoomBeliefPolicy("test-rb2", 2, 0.6, 0.2, 0.15, 2)


def _ledger():
    return RoomBeliefLedger(
        room_ids=("bedroom", "living_room"),
        room_node_ids=("room:1",),
        room_priors={"bedroom": 0.5, "living_room": 0.5},
        category_room_scores={
            "sofa": {"bedroom": 0.2, "living_room": 0.8},
            "bed": {"bedroom": 1.2, "living_room": -0.4},
        },
        label_policy=POLICY,
        evidence_policy_id="test-evidence",
        ontology_version="test-ontology",
        a_table_hash="test-a",
    )


def _evidence(object_id, revision, category, *, retained=True):
    return RoomObjectEvidence(
        object_id=object_id, revision=revision, retained=retained,
        semantic_status="stable", category=category,
        membership_status="assigned", room_id="room:1",
        source_observation_ids=(f"{object_id}:obs",), provenance="test",
    )


def _apply(ledger, event_id, evidence):
    return ledger.apply(RoomBeliefEvent(event_id, evidence.object_id, evidence.revision, evidence))


def test_rb1_uses_log_prior_plus_one_a_per_unique_category_then_softmax():
    ledger = _ledger()
    _apply(ledger, "sofa", _evidence("sofa", 1, "sofa"))
    _apply(ledger, "bed", _evidence("bed", 1, "bed"))
    state = ledger.room_state("room:1")
    expected = {
        "bedroom": math.log(0.5) + 0.2 + 1.2,
        "living_room": math.log(0.5) + 0.8 - 0.4,
    }
    assert dict(state.raw_scores) == expected
    z = sum(math.exp(value - max(expected.values())) for value in expected.values())
    expected_belief = {key: math.exp(value - max(expected.values())) / z for key, value in expected.items()}
    assert all(math.isclose(dict(state.candidates)[key], value, abs_tol=1e-12)
               for key, value in expected_belief.items())


def test_same_category_objects_do_not_add_a_second_category_or_satisfy_gate():
    ledger = _ledger()
    _apply(ledger, "sofa-1", _evidence("sofa-1", 1, "sofa"))
    first = ledger.room_state("room:1")
    _apply(ledger, "sofa-2", _evidence("sofa-2", 1, "sofa"))
    second = ledger.room_state("room:1")
    assert dict(first.raw_scores) == dict(second.raw_scores)
    assert dict(second.category_counts) == {"sofa": 2}
    assert dict(second.category_object_ids) == {"sofa": ("sofa-1", "sofa-2")}
    assert second.label is None
    assert second.status in {"unknown", "ambiguous"}

    _apply(ledger, "remove-sofa-1", _evidence("sofa-1", 2, "sofa", retained=False))
    after_one_removed = ledger.room_state("room:1")
    assert dict(after_one_removed.category_counts) == {"sofa": 1}
    assert dict(after_one_removed.raw_scores) == dict(first.raw_scores)
    _apply(ledger, "remove-sofa-2", RoomObjectEvidence(
        object_id="sofa-2", revision=2, retained=False, semantic_status="unknown",
        category=None, membership_status="unassigned", room_id=None,
        source_observation_ids=("sofa-2:obs",), provenance="test",
    ))
    final = ledger.room_state("room:1")
    assert final.category_counts == ()
    assert final.status == "unknown"


def test_no_evidence_is_unknown():
    state = _ledger().room_state("room:1")
    assert state.category_counts == ()
    assert state.label is None
    assert state.status == "unknown"


def test_static_v2_recomputes_from_a_and_reloads_through_rb1_controller():
    output = ROOT / "artifacts/room_belief/rb2_local_import_20261015_v2_final"
    diagnostic = json.loads((output / "static_room_diagnostic.json").read_text())
    for variant, payload in diagnostic["variants"].items():
        room = payload["rooms"][0]
        expected = {label: math.log(room["runtime_prior"][label]) for label in room["runtime_prior"]}
        for contribution in room["A_contributions"].values():
            for label, value in contribution.items():
                expected[label] += value
        assert all(math.isclose(room["raw_S"][label], value, abs_tol=1e-12)
                   for label, value in expected.items())
        maximum = max(expected.values())
        z = sum(math.exp(value - maximum) for value in expected.values())
        assert all(math.isclose(room["belief"][label], math.exp(expected[label] - maximum) / z, abs_tol=1e-12)
                   for label in expected)
        restored = RoomBeliefLedger.from_dict(payload["ledger"])
        assert restored.room_state(room["room_id"]).as_dict() == room["ledger_state"]
        assert len(room["eligible_categories"]) == 1
        assert room["status"] != "predicted"


def test_00802_static_v2_recompute_matches_controller_for_every_room_variant():
    output = ROOT / "artifacts/room_belief/rb2_00802_min3_static_v2"
    diagnostic = json.loads((output / "static_room_diagnostic.json").read_text())
    assert diagnostic["schema"] == "rb2-static-room-diagnostic/v2"
    assert set(diagnostic["variants"]) == {
        "mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral"
    }
    assert all({room["room_id"] for room in payload["rooms"]} ==
               {"room:2", "room:3", "room:5", "room:6", "room:7"}
               for payload in diagnostic["variants"].values())
    for payload in diagnostic["variants"].values():
        for room in payload["rooms"]:
            expected = {label: math.log(room["runtime_prior"][label])
                        for label in room["runtime_prior"]}
            for contribution in room["A_contributions"].values():
                for label, value in contribution.items():
                    expected[label] += value
            assert all(math.isclose(room["raw_S"][label], value, abs_tol=1e-12)
                       for label, value in expected.items())
            maximum = max(expected.values())
            z = sum(math.exp(value - maximum) for value in expected.values())
            assert all(math.isclose(room["belief"][label],
                                    math.exp(expected[label] - maximum) / z,
                                    abs_tol=1e-12)
                       for label in expected)
            restored = RoomBeliefLedger.from_dict(payload["ledger"])
            assert restored.room_state(room["room_id"]).as_dict() == room["ledger_state"]
    assert all(
        next(room for room in diagnostic["variants"][variant]["rooms"] if room["room_id"] == "room:7")["reason"] == "no_evidence"
        for variant in diagnostic["variants"]
    )
