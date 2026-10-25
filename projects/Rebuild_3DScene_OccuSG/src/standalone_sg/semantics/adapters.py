"""Adapters from existing M5/M6 and M8 outputs into one RegionEvidence contract."""
from __future__ import annotations

from typing import Mapping, Sequence

from ..contracts.regions import CanonicalRegion, Polygon2D
from ..pipeline.contracts import GraphNode, SceneGraphSnapshot
from ..rooms.membership import RegionMembershipResolver, RoomEntity
from .evidence import RegionEvidence, RegionObjectEvidence


def _polygon(outer, holes=()) -> Polygon2D | None:
    if not outer:
        return None
    return Polygon2D(tuple(tuple(float(v) for v in p) for p in outer),
                     tuple(tuple(tuple(float(v) for v in p) for p in ring) for ring in holes))


def _object_evidence(node: GraphNode, *, membership_reason: str,
                     plausible_region_ids: Sequence[str] = (),
                     observations: Mapping[str, Sequence[Mapping[str, object]]] | None = None) -> RegionObjectEvidence:
    attrs = node.attributes
    center = attrs.get("center_W")
    xy = None if not isinstance(center, (tuple, list)) or len(center) < 2 else (float(center[0]), float(center[1]))
    # Keep the graph's persistent key verbatim (for P5 compatibility and
    # exact edge/provenance joins).  The plain ObjectNode attribute is only a
    # display identifier and need not be globally unique across node kinds.
    object_id = node.node_id
    return RegionObjectEvidence(
        object_id=object_id,
        class_name=attrs.get("class_name") if isinstance(attrs.get("class_name"), str) else None,
        position_xy_m=xy,
        detector_score_raw=attrs.get("confidence") if isinstance(attrs.get("confidence"), (int, float)) and not isinstance(attrs.get("confidence"), bool) else None,
        first_seen_s=node.first_seen_s, last_seen_s=node.last_seen_s,
        first_detection_id=attrs.get("first_detection_id") if isinstance(attrs.get("first_detection_id"), str) else None,
        last_detection_id=attrs.get("last_detection_id") if isinstance(attrs.get("last_detection_id"), str) else None,
        observations=tuple((observations or {}).get(object_id, ())),
        membership_reason=membership_reason,
        plausible_region_ids=tuple(sorted(set(plausible_region_ids))),
    )


def evidence_from_m8_snapshot(
    snapshot: SceneGraphSnapshot,
    *,
    observations: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    frame_id: str | None = None,
    pose_context: Mapping[str, object] | None = None,
    processing_timestamp_s: float | None = None,
) -> tuple[RegionEvidence, ...]:
    """Use existing M8 room_contains ownership; never recompute membership here."""
    nodes = {node.node_id: node for node in snapshot.nodes}
    rooms = {node.node_id: node for node in snapshot.nodes if node.kind == "room"}
    members: dict[str, list[str]] = {room_id: [] for room_id in rooms}
    for edge in snapshot.edges:
        if edge.relation == "room_contains" and edge.source_id in members:
            target = nodes.get(edge.target_id)
            if target is not None and target.kind == "object":
                members[edge.source_id].append(edge.target_id)
    stale = bool(snapshot.stale_flags.get("regions", False))
    output = []
    for room_id, room in sorted(rooms.items()):
        attrs = room.attributes
        object_evidence = tuple(
            _object_evidence(nodes[object_id], membership_reason="M8:room_contains",
                             plausible_region_ids=(str(attrs.get("anchor_region_id")),), observations=observations)
            for object_id in sorted(set(members[room_id]))
        )
        anchor = attrs.get("anchor_region_id")
        output.append(RegionEvidence(
            region_id=room_id, region_kind="m8_room", geometry_revision=int(attrs.get("observation_revision", attrs.get("revision", snapshot.revision))),
            evidence_revision=snapshot.revision, source_frame_id=frame_id, coordinate_frame="canonical_world_z_up_xy_m",
            pose_context=pose_context,
            polygon=_polygon(attrs.get("outer_ring", ()), attrs.get("holes", ())),
            observation_timestamp_s=attrs.get("observation_timestamp_s", room.last_seen_s),
            processing_timestamp_s=processing_timestamp_s if processing_timestamp_s is not None else snapshot.timestamp_s,
            freshness="stale" if stale else "fresh",
            source_region_id=anchor if type(anchor) is int else None, source_room_id=room_id,
            objects=object_evidence, membership_provenance="M8 room_contains edges",
            warnings=("M8 room membership is the existing graph ownership; room geometry may be stale",) if stale else (),
        ))
    return tuple(output)


def evidence_from_m5_regions(
    regions: Sequence[CanonicalRegion],
    objects: Sequence[GraphNode],
    *,
    membership_resolver: RegionMembershipResolver,
    observation_revision: int,
    frame_id: str | None = None,
    pose_context: Mapping[str, object] | None = None,
    observation_timestamp_s: float | None = None,
    processing_timestamp_s: float | None = None,
    stale: bool = False,
    room_nodes: Sequence[GraphNode] = (),
    observations: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> tuple[tuple[RegionEvidence, ...], tuple[dict[str, object], ...]]:
    """Apply the same existing M8 RegionMembershipResolver to every M5 region."""
    room_by_region = {
        int(room.attributes["anchor_region_id"]): room.node_id
        for room in room_nodes if room.attributes.get("anchor_region_id") is not None
    }
    by_node = {node.node_id: node for node in objects if node.kind == "object"}
    entities = []
    for node_id, node in sorted(by_node.items()):
        center = node.attributes.get("center_W")
        if isinstance(center, (tuple, list)) and len(center) >= 2:
            entities.append(RoomEntity(node_id, "object", (float(center[0]), float(center[1])), provenance="M6:ObjectNode.center_W"))
    assignments = membership_resolver.resolve(regions, entities)
    assignment_by_id = {item.entity_id: item for item in assignments}
    output = []
    for region in sorted(regions, key=lambda item: int(item.persistent_id)):
        rid = int(region.persistent_id)
        member_ids = sorted(node_id for node_id, assignment in assignment_by_id.items() if assignment.region_id == rid)
        plausible_by_object = {
            node_id: tuple(f"region:{value}" for value in assignment.plausible_region_ids)
            for node_id, assignment in assignment_by_id.items()
        }
        object_evidence = tuple(
            _object_evidence(by_node[node_id], membership_reason=assignment_by_id[node_id].reason,
                             plausible_region_ids=plausible_by_object.get(node_id, ()), observations=observations)
            for node_id in member_ids
        )
        room_id = room_by_region.get(rid)
        warnings = []
        if room_id is None:
            warnings.append("region_not_promoted_to_M8_RoomNode")
        if stale:
            warnings.append("M5_region_state_is_stale; not a fresh observation")
        output.append(RegionEvidence(
            region_id=f"region:{rid}", region_kind="m5_region",
            geometry_revision=int(region.source_grid_revision), evidence_revision=observation_revision,
            source_frame_id=frame_id, pose_context=pose_context,
            coordinate_frame="canonical_world_z_up_xy_m", polygon=region.polygon,
            observation_timestamp_s=region.timestamp_s,
            processing_timestamp_s=processing_timestamp_s if processing_timestamp_s is not None else observation_timestamp_s,
            freshness="stale" if stale else "fresh", source_region_id=rid, source_room_id=room_id,
            objects=object_evidence, membership_provenance="RegionMembershipResolver (M8 implementation)",
            warnings=tuple(warnings),
        ))
    audit = tuple({
        "object_id": node_id,
        "region_id": None if assignment.region_id is None else f"region:{assignment.region_id}",
        "reason": assignment.reason,
        "plausible_region_ids": [f"region:{rid}" for rid in assignment.plausible_region_ids],
        "used_tiebreak": assignment.used_tiebreak,
    } for node_id, assignment in sorted(assignment_by_id.items()))
    return tuple(output), audit
