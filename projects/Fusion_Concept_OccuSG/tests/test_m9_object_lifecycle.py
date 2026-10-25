from __future__ import annotations

import pytest

from standalone_sg.contracts import CanonicalRegion, ContractError, Detection3D, DetectionBatch, Polygon2D
from standalone_sg.objects.association import ObjectAssociationResult, ObjectNode
from standalone_sg.objects.lifecycle import (
    ObjectBackendCapabilities,
    ObjectLifecycleEvent,
    empty_object_lifecycle_state,
    reconcile_object_lifecycle,
    validate_object_lifecycle_state,
)
from standalone_sg.pipeline import FramePacket, SceneGraphBuilder
from standalone_sg.serialization import deserialize_snapshot, replay_deltas, serialize_snapshot


ALL_CAPABILITIES = ObjectBackendCapabilities(1, ("create", "update", "canonical_alias", "retire"),
                                             backend_name="scripted-lifecycle-fixture")


def _object(object_id: str, t: float, x: float) -> ObjectNode:
    return ObjectNode(object_id, "chair", "chair", .9, (x, 1., 0.), "fixture", (1., 1., 1.),
                      "floor_0", 0., t, f"d-{object_id}-0", f"d-{object_id}-{t}", "script", "script")


def _event(revision: int, kind: str, object_id: str, frame_id: str, t: float,
           *, target_id: str | None = None, evidence: str | None = None, event_id: str | None = None):
    return ObjectLifecycleEvent(1, event_id or f"ev-{revision}", kind, object_id, frame_id, t,
                                revision, "scripted-backend-fixture", (() if evidence is None else (evidence,)),
                                target_id)


def _batch(i: int) -> DetectionBatch:
    detection = Detection3D(f"d-{i}", float(i), (float(i % 2 + 1), 1., 0.), "fixture",
                            "chair", "chair", .9, "world", "floor_0", (1., 1., 1.))
    return DetectionBatch("script", f"f{i}", float(i), (detection,))


def _region():
    return CanonicalRegion(1, 1, Polygon2D(((0., 0.), (4., 0.), (4., 4.), (0., 4.))),
                           (2., 2.), 16., 1., 1, ())


class ScriptedLifecycleBackend:
    backend_name = "scripted-lifecycle-fixture"
    capabilities = ALL_CAPABILITIES

    def __init__(self, steps):
        self.steps = list(steps)
        self.index = 0
        self._active = ()

    def snapshot(self):
        return self._active

    def update(self, batch):
        step = self.steps[self.index]
        self.index += 1
        self._active = step["objects"]
        return ObjectAssociationResult("processed", batch.frame_id, batch.timestamp_s, (), self._active,
                                       lifecycle_events=step["events"])


def _run_packet(builder: SceneGraphBuilder, i: int, backend: ScriptedLifecycleBackend | None = None,
                *, objects=None, events=(), stale=False):
    if backend is not None:
        detections = _batch(i)
    else:
        detections = _batch(i)
        backend._active = objects
        backend.steps = [{"objects": objects, "events": events}]
        backend.index = 0
    return builder.update(FramePacket("scene", "seq", f"f{i}", i, float(i), "regions",
                                      regions=(_region(),), regions_stale=stale,
                                      detections=detections))


def test_m9_alias_and_retirement_reconcile_active_graph_and_preserve_history_through_m10():
    steps = [
        {"objects": (_object("a", 0., 1.),), "events": (_event(1, "create", "a", "f0", 0., evidence="d-0"),)},
        {"objects": (_object("a", 0., 1.), _object("b", 1., 2.)),
         "events": (_event(2, "create", "b", "f1", 1., evidence="d-1"),)},
        {"objects": (_object("a", 2., 1.),),
         "events": (_event(3, "canonical_alias", "b", "f2", 2., target_id="a", evidence="d-2"),)},
        {"objects": (), "events": (_event(4, "retire", "a", "f3", 3., evidence="d-3"),)},
    ]
    backend = ScriptedLifecycleBackend(steps)
    builder = SceneGraphBuilder(objects=backend)
    initial = builder.snapshot()
    deltas = []
    snapshots = []
    for i in range(4):
        snapshot, delta = builder.update(FramePacket("scene", "seq", f"f{i}", i, float(i), "regions",
            regions=(_region(),), detections=_batch(i)))
        snapshots.append(snapshot); deltas.append(delta)

    assert "object:b" in {node_id for node_id in deltas[2].removed_node_ids}
    assert "object:a" in {node_id for node_id in deltas[3].removed_node_ids}
    final = snapshots[-1]
    assert not [node for node in final.nodes if node.kind == "object"]
    assert not [edge for edge in final.edges if edge.relation == "room_contains" and edge.target_id.startswith("object:")]
    lifecycle = final.object_lifecycle
    assert lifecycle["snapshot_semantics"] == "active_canonical_objects"
    assert lifecycle["history_semantics"] == "append_only_events"
    assert lifecycle["active_canonical_ids"] == ()
    assert lifecycle["aliases"] == {"b": "a"}
    assert lifecycle["retired_ids"] == ("a",)
    assert [event["kind"] for event in lifecycle["events"]] == ["create", "create", "canonical_alias", "retire"]

    restored = deserialize_snapshot(serialize_snapshot(final))
    assert restored == final
    assert replay_deltas(initial, deltas) == final


def test_fresh_geometry_update_rechecks_membership_but_stale_region_keeps_last_accepted_membership():
    steps = [
        {"objects": (_object("a", 0., 1.),), "events": (_event(1, "create", "a", "f0", 0., evidence="d-0"),)},
        {"objects": (_object("a", 1., 9.),), "events": (_event(2, "update", "a", "f1", 1., evidence="d-1"),)},
        {"objects": (_object("a", 2., 9.),), "events": (_event(3, "update", "a", "f2", 2., evidence="d-2"),)},
    ]
    backend = ScriptedLifecycleBackend(steps)
    builder = SceneGraphBuilder(objects=backend)
    inside, _ = builder.update(FramePacket("scene", "seq", "f0", 0, 0., "regions", regions=(_region(),), detections=_batch(0)))
    stale_outside, _ = builder.update(FramePacket("scene", "seq", "f1", 1, 1., "regions", regions=(_region(),), regions_stale=True, detections=_batch(1)))
    fresh_outside, _ = builder.update(FramePacket("scene", "seq", "f2", 2, 2., "regions", regions=(_region(),), detections=_batch(2)))

    room_members = lambda snapshot: next(node for node in snapshot.nodes if node.kind == "room").attributes["member_ids"]
    assert "object:a" in room_members(inside)
    assert "object:a" in room_members(stale_outside)
    assert "object:a" not in room_members(fresh_outside)


def test_lifecycle_contract_rejects_alias_cycle_missing_target_duplicate_and_conflict():
    cyclic = empty_object_lifecycle_state()
    cyclic["aliases"] = {"a": "b", "b": "a"}
    with pytest.raises(ContractError, match="cycle"):
        validate_object_lifecycle_state(cyclic)

    state = empty_object_lifecycle_state()
    state["active_canonical_ids"] = ["a"]
    with pytest.raises(ContractError, match="target must be an active canonical"):
        reconcile_object_lifecycle(state, (_event(1, "canonical_alias", "a", "f1", 1., target_id="missing"),),
                                   set(), ALL_CAPABILITIES, frame_id="f1", timestamp_s=1.)

    duplicate = _event(1, "create", "a", "f0", 0., evidence="d-0")
    with pytest.raises(ContractError, match="duplicate object lifecycle event_id"):
        reconcile_object_lifecycle(empty_object_lifecycle_state(), (duplicate, duplicate), {"a"},
                                   ALL_CAPABILITIES, frame_id="f0", timestamp_s=0.)

    conflict_state = empty_object_lifecycle_state()
    conflict_state["active_canonical_ids"] = ["a"]
    conflict_events = (
        _event(1, "create", "c", "f1", 1., evidence="d-1"),
        _event(2, "canonical_alias", "a", "f1", 1., target_id="c", evidence="d-1"),
        _event(3, "retire", "a", "f1", 1., evidence="d-1"),
    )
    with pytest.raises(ContractError, match="not an active canonical"):
        reconcile_object_lifecycle(conflict_state, conflict_events, {"c"}, ALL_CAPABILITIES,
                                   frame_id="f1", timestamp_s=1.)


def test_backend_mutation_then_invalid_lifecycle_keeps_committed_graph_and_requires_replay():
    backend = ScriptedLifecycleBackend([
        {"objects": (_object("a", 0., 1.),), "events": ()},
    ])
    builder = SceneGraphBuilder(objects=backend)
    before = builder.snapshot()
    with pytest.raises(ContractError, match="unexplained additions"):
        builder.update(FramePacket("scene", "seq", "f0", 0, 0., "regions", regions=(_region(),), detections=_batch(0)))
    assert backend.snapshot()  # backend mutation is deliberately not rolled back
    assert builder.snapshot() == before
    assert builder.failed and builder.replay_required
