from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.config.schema import ObjectAssociationConfig
from standalone_sg.contracts.objects import Detection3D, DetectionBatch
from standalone_sg.objects.association import ObjectAssociationManager
from standalone_sg.objects.u3_experimental import (
    ExperimentalAssociation, RawClassEvidenceHistory, U3Observation, deduplicate_frame_masks,
)


def observation(did, frame, x=0.0, ext=(1.0, 1.0, 1.0), feature=(1.0, 0.0), cls="table", mask=None):
    return U3Observation(did, frame, float(frame), (x, 0.0, 0.0), ext, "floor_0", cls,
                         tuple(feature), mask or did)


def source_update(manager, obs):
    d = Detection3D(obs.detection_id, obs.timestamp_s, obs.center, "fixture",
                    class_name=obs.class_name, frame_id="world", layer_id=obs.layer_id,
                    extents_m=obs.extents)
    result = manager.update(DetectionBatch("fixture", "world", obs.timestamp_s, (d,)))
    return result.associations[0]


def test_class_flip_keeps_geometry_identity_without_class_gate():
    g = ExperimentalAssociation("geometry")
    ga = ExperimentalAssociation("geometry_appearance")
    a, b = observation("a", "1", cls="dining_table"), observation("b", "2", cls="bed")
    g.update_frame([a]); gd = g.update_frame([b])[0]
    ga.update_frame([a]); gad = ga.update_frame([b])[0]
    assert gd.decision == gad.decision == "matched"
    assert gd.track_id == gad.track_id
    source = ObjectAssociationManager(ObjectAssociationConfig(0.75, 0.15))
    first = source_update(source, a)
    second = source_update(source, b)
    assert first.object_id == second.object_id  # source's same-pose suppression


def test_nearby_same_class_distinct_objects_are_one_to_one_separate():
    objs = [observation("a", "1", x=0.0), observation("b", "1", x=0.7)]
    g = ExperimentalAssociation("geometry")
    decisions = g.update_frame(objs)
    assert [d.decision for d in decisions] == ["created", "created"]
    assert len(g.tracks) == 2
    source = ObjectAssociationManager(ObjectAssociationConfig(0.75, 0.15))
    s1, s2 = (source_update(source, o) for o in objs)
    assert s1.object_id == s2.object_id  # baseline's nearest same-class behavior


def test_close_cross_class_requires_appearance_as_well_as_geometry():
    a = observation("a", "1", ext=(0.5, 0.5, 0.5), feature=(1.0, 0.0), cls="table")
    b = observation("b", "2", x=0.32, ext=(0.5, 0.5, 0.5), feature=(-1.0, 0.0), cls="bed")
    g = ExperimentalAssociation("geometry")
    g.update_frame([a]); gd = g.update_frame([b])[0]
    ga = ExperimentalAssociation("geometry_appearance")
    ga.update_frame([a]); gad = ga.update_frame([b])[0]
    assert gd.decision == "matched"  # geometry-only is an explicit risky ablation
    assert gad.decision == "created"  # cosine alone cannot override the geometry gate


def test_partial_view_with_overlap_can_match():
    a = observation("a", "1", ext=(1.0, 1.0, 1.0))
    b = observation("b", "2", x=0.1, ext=(0.35, 0.5, 0.6))
    ga = ExperimentalAssociation("geometry_appearance")
    ga.update_frame([a]); result = ga.update_frame([b])[0]
    assert result.decision == "matched"
    assert ga.ledger[-1]["aabb_iou"] > 0.01


def test_same_frame_duplicate_masks_are_suppressed_before_association():
    a = observation("a", "1", mask="a")
    b = observation("b", "1", cls="bed", mask="b")
    masks = {"a": np.ones((8, 8), dtype=bool), "b": np.ones((8, 8), dtype=bool)}
    kept, dup = deduplicate_frame_masks([a, b], masks)
    assert [o.detection_id for o in kept] == ["a"]
    assert dup[0]["duplicate_of"] == "a"


def test_equal_candidates_defer_instead_of_using_arbitrary_tie_break():
    g = ExperimentalAssociation("geometry_appearance")
    first = observation("a", "1", feature=(1.0, 0.08))
    second = observation("b", "1", feature=(1.0, -0.08))
    g.update_frame([first, second])
    probe = observation("c", "2", feature=(1.0, 0.0))
    decision = g.update_frame([probe])[0]
    assert decision.decision == "defer"
    assert decision.reason == "ambiguous_candidates"


def test_prior_surface_geometry_is_one_causal_snapshot_for_whole_frame():
    prior = {"u3-0001": {
        "geometry_source": "prior_accumulated_surface_voxel_representatives",
        "center_W": (0.0, 0.0, 0.0), "extents_m": (1.0, 1.0, 1.0),
        "prior_revision": 1, "evidence_ids": ["f0:det-0"],
    }}
    matcher = ExperimentalAssociation("geometry")
    assert matcher.update_frame([observation("f0:det-0", "0")])[0].decision == "created"
    # Candidate observations from frame 1 are all scored against this same
    # prior snapshot, before either observation can update matcher state.
    current = [observation("f1:det-0", "1", x=0.0), observation("f1:det-1", "1", x=0.05)]
    decisions = matcher.update_frame(current, prior_geometry_by_track=prior)
    assert len(decisions) == 2
    rows = [row for row in matcher.ledger if row["frame_id"] == "1"]
    assert len(rows) == 2
    assert all(row["matcher_geometry_source"] == "prior_accumulated_surface_voxel_representatives" for row in rows)
    assert all(row["prior_geometry_revision"] == 1 for row in rows)
    assert all(row["prior_geometry_evidence_ids"] == ["f0:det-0"] for row in rows)
    assert all(row["prior_matcher_center_W"] == [0.0, 0.0, 0.0] for row in rows)
    assert all("f1:det-0" not in row["prior_geometry_evidence_ids"] for row in rows)


def test_current_observation_cannot_supply_its_own_prior_track_geometry():
    matcher = ExperimentalAssociation("geometry")
    current = observation("f0:det-0", "0")
    decisions = matcher.update_frame([current], prior_geometry_by_track={})
    assert decisions[0].decision == "created"
    assert matcher.ledger == []


def test_default_geometry_mode_is_unchanged_when_prior_representation_is_off():
    seq = [[observation("f0:det-0", "0")], [observation("f1:det-0", "1", x=0.1)]]
    left, right = ExperimentalAssociation("geometry"), ExperimentalAssociation("geometry")
    left_result = [d for frame in seq for d in left.update_frame(frame)]
    right_result = [d for frame in seq for d in right.update_frame(frame, prior_geometry_by_track=None)]
    assert left_result == right_result
    assert left.ledger == right.ledger


def test_prior_surface_representation_replay_is_deterministic():
    def replay():
        matcher = ExperimentalAssociation("geometry")
        first = observation("f0:det-0", "0")
        matcher.update_frame([first])
        prior = {"u3-0001": {
            "geometry_source": "prior_accumulated_surface_voxel_representatives",
            "center_W": (0.0, 0.0, 0.0), "extents_m": (1.0, 1.0, 1.0),
            "prior_revision": 1, "evidence_ids": [first.detection_id],
        }}
        current = observation("f1:det-0", "1", x=0.1)
        decisions = matcher.update_frame([current], prior_geometry_by_track=prior)
        return [(row.detection_id, row.track_id, row.decision, row.reason) for row in decisions], matcher.ledger

    assert replay() == replay()


def test_raw_class_soft_rerank_is_soft_and_uses_only_prior_frame_history():
    matcher = ExperimentalAssociation("geometry", ambiguity_margin=0.01)
    table = observation("f0:table", "0", x=0.0, cls="table")
    chair = observation("f0:chair", "0", x=0.4, cls="chair")
    table = U3Observation(**{**table.__dict__, "class_confidence": 1.0})
    chair = U3Observation(**{**chair.__dict__, "class_confidence": 1.0})
    matcher.update_frame([table, chair])
    tracks = {d.detection_id: d.track_id for d in matcher.decisions}
    history = RawClassEvidenceHistory()
    history.commit_frame([table, chair], tracks)
    prior = history.snapshot()
    probe = observation("f1:probe", "1", x=0.2, cls="table")
    probe = U3Observation(**{**probe.__dict__, "class_confidence": 1.0})
    decision = matcher.update_frame([probe], prior_class_evidence_by_track=prior,
                                    class_soft_lambda=0.15)[0]
    assert decision.decision == "matched"
    assert decision.track_id == tracks["f0:table"]
    rows = [r for r in matcher.ledger if r["detection_id"] == probe.detection_id]
    assert all(r["eligible"] for r in rows)
    assert {r["raw_class_agreement"] for r in rows} == {0.0, 1.0}
    assert all(r["score_adjustment"] == "raw_class_soft_rerank" for r in rows)
    # The observation under decision cannot alter its own frozen evidence snapshot.
    assert prior == history.snapshot()


def test_raw_class_does_not_hard_gate_cross_class_match():
    matcher = ExperimentalAssociation("geometry")
    first = observation("f0:a", "0", cls="table")
    first = U3Observation(**{**first.__dict__, "class_confidence": 1.0})
    first_decision = matcher.update_frame([first])[0]
    history = RawClassEvidenceHistory()
    history.commit_frame([first], {first.detection_id: first_decision.track_id})
    different_class = observation("f1:b", "1", x=0.05, cls="bed")
    different_class = U3Observation(**{**different_class.__dict__, "class_confidence": 1.0})
    decision = matcher.update_frame([different_class], prior_class_evidence_by_track=history.snapshot(),
                                    class_soft_lambda=0.30)[0]
    assert decision.decision == "matched"
    assert decision.track_id == first_decision.track_id


def test_raw_class_history_excludes_aliases_and_deferred_observations():
    history = RawClassEvidenceHistory()
    alias, deferred, assigned = (
        U3Observation("alias", "f0", 0.0, (0., 0., 0.), None, "floor_0", "table", None, "m", 1.0),
        U3Observation("deferred", "f0", 0.0, (0., 0., 0.), None, "floor_0", "chair", None, "m", 1.0),
        U3Observation("assigned", "f0", 0.0, (0., 0., 0.), None, "floor_0", "sofa", None, "m", 0.8),
    )
    history.commit_frame([alias, deferred, assigned],
                         {"alias": "track", "deferred": None, "assigned": "track"},
                         alias_ids={"alias"})
    assert history.snapshot() == {"track": {"sofa": 0.8}}


def test_same_class_nearby_tracks_remain_ambiguous_without_geometry_preference():
    matcher = ExperimentalAssociation("geometry", ambiguity_margin=0.05)
    a = observation("f0:a", "0", x=0.0, cls="chair")
    b = observation("f0:b", "0", x=0.4, cls="chair")
    matcher.update_frame([a, b])
    history = RawClassEvidenceHistory()
    history.commit_frame([a, b], {d.detection_id: d.track_id for d in matcher.decisions})
    probe = observation("f1:c", "1", x=0.2, cls="chair")
    probe = U3Observation(**{**probe.__dict__, "class_confidence": 1.0})
    prior = history.snapshot()
    result = matcher.update_frame([probe], prior_class_evidence_by_track=prior,
                                  class_soft_lambda=0.15)[0]
    assert result.decision == "defer"
    assert history.snapshot() == prior
