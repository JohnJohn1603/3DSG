import json

from standalone_sg.pipeline.contracts import GraphEdge, GraphNode, SceneGraphSnapshot
from standalone_sg.semantics import RuleBasedRoomLabeler


def _snapshot(objects, *, stale=False, revision=1):
    rooms = [GraphNode("room:1", "room", 0.0, 0.0, "M8", {
        "anchor_region_id": 1, "name": "room_1", "outer_ring": [[0, 0], [2, 0], [2, 2], [0, 2]],
        "centroid_xy": [1, 1],
    })]
    nodes = rooms + [GraphNode(f"object:{i}", "object", 0.0, 1.0, "P4", {
        "object_id": i, "class_name": cls,
    }) for i, cls in objects]
    edges = tuple(GraphEdge("room:1", f"object:{i}", "room_contains", "M8", True) for i, _ in objects)
    return SceneGraphSnapshot(1, "scene", "seq", revision, 1.0, tuple(nodes), edges, "cfg", {"regions": stale})


def _labeler():
    return RuleBasedRoomLabeler({
        "score_kind": "uncalibrated_additive_evidence", "minimum_support_classes": 2,
        "minimum_top_score": 2.0, "minimum_margin": 1.0, "count_cap_per_class": 2,
        "labels": {"living_room": {"sofa": 3.0, "tv": 2.0}, "kitchen": {"refrigerator": 3.0, "microwave": 2.0}},
    })


def test_repeated_observation_is_counted_once_and_two_class_evidence_predicts():
    states = _labeler().label_snapshot(_snapshot([("object-1", "sofa"), ("object-2", "tv")]))
    assert states[0].status == "predicted"
    assert states[0].label == "living_room"
    assert states[0].observed_object_ids == ("object:object-1", "object:object-2")
    assert states[0].supporting_object_ids == ("object:object-1", "object:object-2")
    assert states[0].score_kind == "uncalibrated_additive_evidence"


def test_unsupported_or_single_evidence_abstains():
    state = _labeler().label_snapshot(_snapshot([("object-1", "bench")]))[0]
    assert state.status == "unknown"
    assert state.label is None


def test_support_classes_are_counted_per_candidate_not_globally():
    # bed supports bedroom; potted_plant supports living_room only.  The
    # unrelated living-room class must not satisfy bedroom's two-class gate.
    state = _labeler().label_snapshot(_snapshot([("object-1", "bed"), ("object-2", "potted_plant")]))[0]
    assert state.status == "unknown"
    assert state.label is None
    assert state.observed_object_ids == ("object:object-1", "object:object-2")
    assert state.supporting_object_ids == ()


def test_evidence_hash_ignores_global_revision_but_tracks_evidence():
    labeler = _labeler()
    first = labeler.label_snapshot(_snapshot([("object-1", "sofa"), ("object-2", "tv")], revision=1))[0]
    same_evidence = labeler.label_snapshot(_snapshot([("object-1", "sofa"), ("object-2", "tv")], revision=99))[0]
    changed_evidence = labeler.label_snapshot(_snapshot([("object-1", "sofa"), ("object-2", "bench")], revision=100))[0]
    assert first.evidence_hash == same_evidence.evidence_hash
    assert first.source_revision == 1
    assert same_evidence.source_revision == 99
    assert first.evidence_hash != changed_evidence.evidence_hash


def test_semantic_lifecycle_stale_then_fresh_and_membership_change():
    labeler = _labeler()
    stale = labeler.label_snapshot(_snapshot([("object-1", "sofa"), ("object-2", "tv")], stale=True, revision=2))[0]
    fresh = labeler.label_snapshot(_snapshot([("object-1", "sofa"), ("object-2", "tv")], stale=False, revision=3))[0]
    changed = labeler.label_snapshot(_snapshot([("object-1", "refrigerator"), ("object-2", "microwave")], stale=False, revision=4))[0]
    assert stale.status == "stale"
    assert fresh.status == "predicted"
    assert changed.status == "predicted"
    assert changed.label == "kitchen"
    assert changed.observed_object_ids == ("object:object-1", "object:object-2")


def test_competing_evidence_is_ambiguous_and_stale_is_not_fresh():
    state = _labeler().label_snapshot(_snapshot([
        ("object-1", "sofa"), ("object-2", "tv"),
        ("object-3", "refrigerator"), ("object-4", "microwave"),
    ]))[0]
    assert state.status == "ambiguous"
    stale = _labeler().label_snapshot(_snapshot([("object-1", "sofa"), ("object-2", "tv")], stale=True))[0]
    assert stale.status == "stale"
    assert stale.stale_reason == "source_room_geometry_stale"
