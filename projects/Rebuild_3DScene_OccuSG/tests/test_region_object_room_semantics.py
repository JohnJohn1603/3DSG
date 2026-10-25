import pytest

from standalone_sg.config import RoomConfig
from standalone_sg.contracts.regions import CanonicalRegion, Polygon2D
from standalone_sg.pipeline.contracts import GraphEdge, GraphNode, SceneGraphSnapshot
from standalone_sg.rooms.membership import RegionMembershipResolver
from standalone_sg.semantics import (
    RegionEvidence,
    RegionObjectEvidence,
    RuleBasedRoomLabeler,
    evidence_from_m5_regions,
    evidence_from_m8_snapshot,
)
from standalone_sg.contracts.errors import ContractError


def _labeler(config_overrides=None):
    config = {
        "schema_version": 1,
        "profile_id": "test_v1",
        "minimum_support_classes": 2,
        "minimum_top_score": 2.0,
        "minimum_margin": 1.0,
        "count_cap_per_class": 2,
        "labels": {"living_room": {"sofa": 3.0, "tv": 2.0}},
        "class_aliases": {"TV monitor": "tv"},
    }
    config.update(config_overrides or {})
    return RuleBasedRoomLabeler(config)


def _regions():
    return (CanonicalRegion(
        persistent_id=7, raw_id=0,
        polygon=Polygon2D(((0, 0), (2, 0), (2, 2), (0, 2))),
        centroid_xy=(1, 1), area_m2=4.0, timestamp_s=1.0, source_grid_revision=3,
    ),)


def _objects():
    return (
        GraphNode("object:a", "object", 0.0, 1.0, "M6", {
            "object_id": "a", "class_name": "sofa", "center_W": [0.5, 0.5, 0.3],
            "confidence": 0.9, "first_detection_id": "d0", "last_detection_id": "d1",
        }),
        GraphNode("object:b", "object", 0.2, 1.0, "M6", {
            "object_id": "b", "class_name": "TV monitor", "center_W": [1.0, 0.5, 0.3],
            "confidence": 0.8, "first_detection_id": "d2", "last_detection_id": "d2",
        }),
    )


def test_m5_and_m8_adapters_share_membership_scoring_and_ids():
    m5, audit = evidence_from_m5_regions(
        _regions(), _objects(), membership_resolver=RegionMembershipResolver(RoomConfig()),
        observation_revision=5, frame_id="frame-5", observation_timestamp_s=1.0,
    )
    room = GraphNode("room:7", "room", 0.0, 1.0, "M8", {
        "anchor_region_id": 7, "outer_ring": [[0, 0], [2, 0], [2, 2], [0, 2]],
        "observation_revision": 3,
    })
    snapshot = SceneGraphSnapshot(
        1, "scene", "seq", 5, 1.0, (room,) + _objects(),
        (GraphEdge("room:7", "object:a", "room_contains", "M8", True),
         GraphEdge("room:7", "object:b", "room_contains", "M8", True)),
        "cfg", {},
    )
    m8 = evidence_from_m8_snapshot(snapshot, frame_id="frame-5")
    labeler = _labeler()
    s5 = labeler.label_regions(m5)[0]
    s8 = labeler.label_regions(m8)[0]
    assert audit and all(row["region_id"] == "region:7" for row in audit)
    assert m5[0].source_frame_id == "frame-5"
    assert s5.status == s8.status == "predicted"
    assert s5.label == s8.label == "living_room"
    assert s5.candidates == s8.candidates
    assert s5.observed_object_ids == s8.observed_object_ids == ("object:a", "object:b")
    assert s5.semantic_signature == s8.semantic_signature


def test_empty_m5_region_is_kept_as_unknown_and_stale_is_explicit():
    evidence, _ = evidence_from_m5_regions(
        _regions(), (), membership_resolver=RegionMembershipResolver(RoomConfig()),
        observation_revision=1, frame_id="empty-frame", observation_timestamp_s=0.0,
    )
    assert len(evidence) == 1
    assert _labeler().label_regions(evidence)[0].status == "unknown"
    stale = (RegionEvidence(
        region_id=evidence[0].region_id, region_kind=evidence[0].region_kind,
        geometry_revision=evidence[0].geometry_revision, evidence_revision=evidence[0].evidence_revision,
        source_frame_id=evidence[0].source_frame_id, coordinate_frame=evidence[0].coordinate_frame,
        polygon=evidence[0].polygon, observation_timestamp_s=evidence[0].observation_timestamp_s,
        processing_timestamp_s=evidence[0].processing_timestamp_s, freshness="stale",
        source_region_id=evidence[0].source_region_id, source_room_id=None,
        objects=evidence[0].objects, membership_provenance=evidence[0].membership_provenance,
    ),)
    state = _labeler().label_regions(stale)[0]
    assert state.status == "stale"
    assert state.label is None


def test_repeated_observation_refs_do_not_multiply_unique_object_evidence():
    obj = RegionObjectEvidence(
        "object:a", "sofa", (0.5, 0.5), 0.9, 0.0, 1.0, "d0", "d1",
        observations=({"frame_id": "f0", "detection_id": "d0"}, {"frame_id": "f1", "detection_id": "d1"}),
    )
    duplicate = RegionObjectEvidence(
        "object:b", "tv", (1.0, 0.5), 0.8, 0.0, 1.0, "d2", "d2",
        observations=({"frame_id": "f1", "detection_id": "d2"},),
    )
    evidence = RegionEvidence(
        region_id="region:7", region_kind="m5_region", geometry_revision=1, evidence_revision=1,
        coordinate_frame="canonical_world_z_up_xy_m",
        polygon=Polygon2D(((0, 0), (2, 0), (2, 2), (0, 2))),
        observation_timestamp_s=1.0, processing_timestamp_s=1.1, freshness="fresh",
        source_region_id=7, source_room_id=None, objects=(obj, duplicate), membership_provenance="test membership",
        source_frame_id="f1",
    )
    state = _labeler().label_regions((evidence,))[0]
    assert state.class_counts == (("sofa", 1), ("tv", 1))
    assert state.observed_object_ids == ("object:a", "object:b")
    assert RegionEvidence.from_dict(evidence.as_dict()).as_dict() == evidence.as_dict()


@pytest.mark.parametrize("override", [
    {"minimum_top_score": True},
    {"minimum_top_score": float("nan")},
    {"count_cap_per_class": 1.5},
    {"class_aliases": {"chair": "not_in_rules"}},
])
def test_invalid_versioned_rule_configs_are_rejected_without_coercion(override):
    with pytest.raises(ContractError):
        _labeler(override)
