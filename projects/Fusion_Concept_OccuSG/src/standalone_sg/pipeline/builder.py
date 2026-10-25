"""Single-writer, revisioned offline SceneGraphBuilder (M9)."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
import math
from typing import Any

from ..config.schema import NavigationConfig, ObjectAssociationConfig, RoomConfig
from ..contracts.errors import ContractError
from ..navigation.blocks import NavigationBlockGraph
from ..objects.association import ObjectAssociationBackend, ObjectAssociationManager
from ..objects.lifecycle import (ObjectBackendCapabilities, empty_object_lifecycle_state,
                                 reconcile_object_lifecycle)
from ..rooms.anchoring import RoomAnchoringManager
from ..rooms.membership import RoomEntity, room_entity_from_nav_node
from .contracts import FramePacket, GraphDelta, GraphPatch, GraphEdge, GraphNode, SceneGraphSnapshot
from .scheduler import SequenceScheduler


class CleanupError(RuntimeError):
    """One or more owned stages failed to close.

    All stages are attempted before this error is raised.  ``errors`` keeps
    the original exceptions so callers can attach them to an in-flight
    failure without replacing the primary error.
    """

    def __init__(self, errors):
        self.errors = tuple(errors)
        details = "; ".join(f"{type(error).__name__}: {error}" for error in self.errors)
        super().__init__(f"scene graph cleanup incomplete: {details}")


def _plain(value: Any):
    if hasattr(value, "items"):
        return {str(k): _plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v) for v in value), key=repr)
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if hasattr(value, "tolist"):
        return _plain(value.tolist())
    return value


def _canonical(node_or_edge):
    return json.dumps(_plain(node_or_edge), sort_keys=True, separators=(",", ":"), allow_nan=False)


class SceneGraphBuilder:
    """Integrate canonical packets through injected mapping/region stages and M6–M8.

    Graph commits are atomic. Stateful native/pure stages are not transactionally
    rolled back if they mutate and a later stage fails; that limitation is surfaced
    rather than hidden.
    """
    def __init__(self, *, mapping_stage=None, region_stage=None,
                 navigation: NavigationBlockGraph | None = None,
                 objects: ObjectAssociationBackend | None = None,
                 rooms: RoomAnchoringManager | None = None,
                 config_hash: str = "unconfigured"):
        if type(config_hash) is not str or not config_hash:
            raise ContractError("config_hash must be a non-empty string")
        self.mapping_stage=mapping_stage; self.region_stage=region_stage
        self.navigation=navigation or NavigationBlockGraph(NavigationConfig())
        self.objects=objects or ObjectAssociationManager(ObjectAssociationConfig())
        self.rooms=rooms or RoomAnchoringManager(RoomConfig())
        self.config_hash=config_hash; self.scheduler=SequenceScheduler()
        self._nodes={}; self._edges=set(); self._revision=0; self._scene=None; self._sequence=None
        self._last_timestamp=None; self._last_regions=(); self._last_tracks=(); self._room_revision=0
        self._agent_ids=[]; self._event_log=[]; self._closed=False; self._closed_stage_ids=set(); self._failed_reason=None; self._flushed=False; self._mutation_started=False; self._room_maintenance_pending=False; self._have_region_snapshot=False; self._last_stale={"mapping":False,"regions":False}
        self._last_grid=None; self._last_nav_edges=()
        self._object_lifecycle=empty_object_lifecycle_state()
        self.last_object_association_result=None

    def snapshot(self) -> SceneGraphSnapshot:
        return SceneGraphSnapshot(1,self._scene,self._sequence,self._revision,self._last_timestamp,
            tuple(self._nodes[k] for k in sorted(self._nodes)),tuple(sorted(self._edges)),self.config_hash,
            dict(self._last_stale), self._object_lifecycle)

    @property
    def failed(self): return self._failed_reason is not None

    @property
    def replay_required(self): return self._failed_reason is not None

    @property
    def last_grid(self):
        """The exact M3 grid used by the most recent builder update.

        This is intentionally read-only and is exposed for observability
        consumers such as the Webots overlay.  It is not a second projection
        path: ``update()`` stores the grid returned by the injected mapping
        stage before the downstream stages consume it.
        """
        return self._last_grid

    def _check_lifecycle(self, operation):
        if self._closed: raise ContractError("SceneGraphBuilder is closed")
        if self._failed_reason is not None:
            raise ContractError(f"SceneGraphBuilder failed; replay required before {operation}: {self._failed_reason}")
        if self._flushed:
            raise ContractError("SceneGraphBuilder sequence is flushed; create a new instance and replay")

    def update(self, packet: FramePacket) -> tuple[SceneGraphSnapshot, GraphDelta]:
        self._check_lifecycle("update")
        self.last_object_association_result=None
        self._mutation_started=False
        try:
            return self._update_impl(packet)
        except Exception as exc:
            if self._mutation_started: self._failed_reason=f"{type(exc).__name__}: {exc}"
            raise

    def _update_impl(self, packet: FramePacket) -> tuple[SceneGraphSnapshot, GraphDelta]:
        if self._closed: raise ContractError("SceneGraphBuilder is closed")
        if not isinstance(packet,FramePacket): raise ContractError("update requires FramePacket")
        self.scheduler.validate(packet)
        scheduler_candidate=self.scheduler.candidate(packet)
        # All packet-level validation and ordering checks happen before stage calls.
        grid=packet.grid
        mapping_status="not_requested"
        if grid is None and packet.input_mode in ("rgbd","pointcloud","occupancy"):
            if self.mapping_stage is None: raise ContractError(f"input_mode={packet.input_mode} requires an injected MappingStage")
            self._mutation_started=True
            grid=self.mapping_stage.build_grid(packet); mapping_status="built"
        elif grid is not None: mapping_status="provided"

        old_nodes=dict(self._nodes); old_edges=set(self._edges)
        candidate_agent_ids=list(self._agent_ids); candidate_regions=self._last_regions; candidate_tracks=self._last_tracks
        candidate_room_revision=self._room_revision; candidate_grid=self._last_grid; candidate_nav_edges=self._last_nav_edges
        candidate_object_lifecycle=self._object_lifecycle
        next_nodes=dict(old_nodes); next_edges=set(old_edges); events=[]; stale=dict(self._last_stale)
        stage_status={"mapping":mapping_status,"regions":"not_requested","objects":"provider_absent","navigation":"not_requested","rooms":"not_requested"}
        current_regions=packet.regions; tracks=packet.retained_tracks; region_stale=packet.regions_stale
        region_observation_fresh=packet.regions is not None and not packet.regions_stale
        if grid is not None and packet.regions is None and self.region_stage is not None:
            self._mutation_started=True
            result=self.region_stage.update(grid,timestamp_s=packet.timestamp_s)
            current_regions=result.current_regions; tracks=result.retained_tracks
            region_stale=result.stale or not result.accepted
            region_observation_fresh=bool(result.accepted and not result.stale)
            stage_status["regions"]=result.backend_status + ("/stale" if region_stale else "/fresh")
            if region_stale: events.append("regions_stale" if result.stale else "regions_rejected")
            else: events.append("regions_accepted")
        elif current_regions is not None:
            stage_status["regions"]="provided_stale" if region_stale else "provided_fresh"
            if region_stale: events.append("regions_stale")
        if current_regions is None and self._have_region_snapshot:
            stage_status["regions"]="cached_accepted_snapshot"
            # Re-evaluate current membership/adjacency against cached accepted regions
            # when agent/object/navigation changes, without refreshing region evidence.
            current_regions=candidate_regions; tracks=candidate_tracks; region_stale=False
            region_observation_fresh=False

        # Persistent agent trajectory nodes.
        agent_id=None
        if packet.base_pose_W is not None:
            agent_id=f"agent:{packet.scene_id}:{packet.sequence_id}:{packet.frame_id}"
            candidate_agent_ids.append(agent_id)
            next_nodes[agent_id]=GraphNode(agent_id,"agent",packet.timestamp_s,packet.timestamp_s,"frame_packet.base_pose_W",{
                "pose_W":packet.base_pose_W,"frame_id":packet.frame_id,"frame_index":packet.frame_index,
                "source_metadata":packet.source_metadata})
            if len(candidate_agent_ids)>1:
                next_edges.add(GraphEdge(candidate_agent_ids[-2],agent_id,"temporal_successor","frame_order",True))
        # Object absent and explicit-empty are distinct M6 calls/diagnostics.
        if packet.detections is not None:
            self._mutation_started=True
            obj_result=self.objects.update(packet.detections)
            self.last_object_association_result=obj_result
            stage_status["objects"]=obj_result.status
            lifecycle_events=tuple(getattr(obj_result,"lifecycle_events",()))
            capabilities=getattr(self.objects,"capabilities",None)
            if capabilities is None:
                if lifecycle_events:
                    raise ContractError("object backend emitted lifecycle events without declaring capabilities")
                capabilities=ObjectBackendCapabilities(1,())
            if not isinstance(capabilities,ObjectBackendCapabilities):
                raise ContractError("object backend capabilities must use ObjectBackendCapabilities v1")
            active_objects=tuple(obj_result.objects)
            active_ids={obj.object_id for obj in active_objects}
            batch_evidence_ids=set() if packet.detections.detections is None else {
                detection.detection_id for detection in packet.detections.detections
            }
            for lifecycle_event in lifecycle_events:
                if lifecycle_event.kind in {"create","update"} and not set(lifecycle_event.evidence_ids) <= batch_evidence_ids:
                    raise ContractError("create/update lifecycle evidence must reference detections in the current frame")
            candidate_object_lifecycle=reconcile_object_lifecycle(
                self._object_lifecycle, lifecycle_events, active_ids, capabilities,
                frame_id=packet.frame_id, timestamp_s=packet.timestamp_s,
            )
            for lifecycle_event in lifecycle_events:
                events.append(f"object_{lifecycle_event.kind}")
            # The backend snapshot is authoritative for active canonical objects.
            # Removing absent nodes is permitted only when an explicit alias/retire
            # event explains the transition (validated above).
            active_node_ids={f"object:{object_id}" for object_id in active_ids}
            for node_id,node in tuple(next_nodes.items()):
                if node.kind=="object" and node_id not in active_node_ids:
                    del next_nodes[node_id]
            for obj in active_objects:
                node_id=f"object:{obj.object_id}"
                next_nodes[node_id]=GraphNode(node_id,"object",obj.first_seen_s,obj.last_seen_s,
                    f"provider:{obj.last_provider_id}",{"object_id":obj.object_id,"class_name":obj.class_name,
                    "class_id":obj.class_id,"confidence":obj.confidence,"center_W":obj.center_W,
                    "center_method":obj.center_method,"extents_m":obj.extents_m,"layer_id":obj.layer_id,
                    "first_detection_id":obj.first_detection_id,"last_detection_id":obj.last_detection_id,
                    "first_provider_id":obj.first_provider_id,"last_provider_id":obj.last_provider_id})
        # Structural nav graph from the grid; no claim of physical passability.
        nav_edges=candidate_nav_edges
        if grid is not None:
            self._mutation_started=True
            nav=self.navigation.update(grid); nav_edges=nav.edges; stage_status["navigation"]="updated"
            # M7 owns this complete edge relation; reconcile, never accumulate.
            next_edges={e for e in next_edges if e.relation!="navigable_path"}
            for node in nav.nodes:
                nid=node.node_id
                prior_nav=old_nodes.get(nid)
                first_seen=prior_nav.first_seen_s if prior_nav is not None else packet.timestamp_s
                provenance=prior_nav.provenance if prior_nav is not None else "M7:structural_block"
                next_nodes[nid]=GraphNode(nid,"navigation",first_seen,packet.timestamp_s,provenance,{
                    "block_id":node.block_id,"bounds_xy":node.bounds_xy,"center_xy":node.center_xy,
                    "free_cell_count":node.free_cell_count,"meets_minimum_free_cells":node.meets_minimum_free_cells,
                    "representative_cell_rc":node.representative_cell_rc,"representative_center_W":node.representative_center_W,
                    "revision":node.revision,"observation_timestamp_s":packet.timestamp_s,
                    "processing_timestamp_s":packet.timestamp_s,"robot_footprint_passability":False})
            for edge in nav.edges:
                a=f"nav:{edge.source_block[0]}:{edge.source_block[1]}"; b=f"nav:{edge.target_block[0]}:{edge.target_block[1]}"
                next_edges.add(GraphEdge(a,b,"navigable_path","M7:structural_free_cell_bridge",True))
            for removed in nav.removed_node_ids:
                next_nodes.pop(removed,None)
                next_edges={e for e in next_edges if e.source_id!=removed and e.target_id!=removed}
            stale["mapping"]=False; candidate_grid=grid; candidate_nav_edges=nav_edges

        # M8 is updated only when a region result/snapshot is explicitly supplied.
        if current_regions is not None:
            if not region_stale:
                candidate_regions=tuple(current_regions); candidate_tracks=tuple(tracks)
            regions_for_rooms=candidate_regions; tracks_for_rooms=candidate_tracks
            entities=[]
            for aid in candidate_agent_ids:
                n=next_nodes.get(aid) or self._nodes.get(aid)
                if n:
                    pose=n.attributes["pose_W"]; entities.append(RoomEntity(aid,"agent",(pose[0],pose[1]),provenance="M9:agent_pose"))
            for obj in self.objects.snapshot():
                entities.append(RoomEntity(f"object:{obj.object_id}","object",(obj.center_W[0],obj.center_W[1]),provenance=f"provider:{obj.last_provider_id}"))
            nav_now=self.navigation.snapshot()
            entities.extend(room_entity_from_nav_node(n) for n in nav_now)
            if region_stale:
                maintenance=False
            else:
                self._mutation_started=True
                maintenance=self.rooms.maintenance_due(packet.timestamp_s)
                if not maintenance: self._room_maintenance_pending=True
                else: self._room_maintenance_pending=False
            self._mutation_started=True
            candidate_room_revision=max(candidate_room_revision,grid.revision if grid is not None else candidate_room_revision)+1
            room_update=self.rooms.update(regions_for_rooms,tracks_for_rooms,entities,revision=candidate_room_revision,
                stale=region_stale,nav_edges=nav_edges,expired_track_ids=(),maintenance=maintenance)
            stage_status["rooms"]=("stale_preserved" if room_update.stale else
                ("membership_only" if not maintenance else "maintenance_and_membership"))
            if room_update.created_room_ids: events.append("rooms_promoted")
            if room_update.removed_room_ids: events.append("rooms_pruned")
            if room_update.relinked: events.append("rooms_relinked")
            # Stale or deferred-prune rooms retain their last accepted evidence exactly.
            current_region_ids={int(r.persistent_id) for r in regions_for_rooms if r.persistent_id is not None}
            if not region_stale:
                for key,node in tuple(next_nodes.items()):
                    if node.kind=="room": del next_nodes[key]
                next_edges={e for e in next_edges if e.relation not in ("room_contains","room_adjacency")}
            region_by_id={int(r.persistent_id):r for r in regions_for_rooms if r.persistent_id is not None}
            for room in room_update.rooms:
                rid=f"room:{room.room_id}"; previous_room=old_nodes.get(rid)
                fresh_evidence=(region_observation_fresh and room.anchor_region_id in current_region_ids)
                if not fresh_evidence:
                    if previous_room is None: continue
                    if region_stale or room.anchor_region_id not in current_region_ids:
                        next_nodes[rid]=previous_room
                        if not region_stale:
                            next_edges.update(e for e in old_edges if e.source_id==rid and e.relation=="room_contains")
                    else:
                        attrs=dict(previous_room.attributes); attrs["member_ids"]=room.member_ids
                        attrs["processing_timestamp_s"]=packet.timestamp_s
                        next_nodes[rid]=GraphNode(rid,"room",previous_room.first_seen_s,previous_room.last_seen_s,
                            previous_room.provenance,attrs)
                        for member in room.member_ids:
                            target=member if member in next_nodes else (f"object:{member}" if f"object:{member}" in next_nodes else member)
                            if target in next_nodes: next_edges.add(GraphEdge(rid,target,"room_contains","M8:membership",True))
                    continue
                poly=room.polygon; region_evidence=region_by_id[room.anchor_region_id]
                first_seen=previous_room.first_seen_s if previous_room is not None else packet.timestamp_s
                observed_at=float(region_evidence.timestamp_s)
                next_nodes[rid]=GraphNode(rid,"room",first_seen,observed_at,
                    previous_room.provenance if previous_room is not None else "M8:region_anchor",{
                    "room_id":room.room_id,"name":room.name,"anchor_region_id":room.anchor_region_id,
                    "outer_ring":poly.outer,"holes":poly.holes,"centroid_xy":room.centroid_xy,
                    "member_ids":room.member_ids,"created_reason":room.created_reason,"revision":room.revision,
                    "observation_timestamp_s":observed_at,"observation_revision":region_evidence.source_grid_revision,
                    "processing_timestamp_s":packet.timestamp_s})
                for member in room.member_ids:
                    target=member if member in next_nodes else (f"object:{member}" if f"object:{member}" in next_nodes else member)
                    if target in next_nodes: next_edges.add(GraphEdge(rid,target,"room_contains","M8:membership",True))
            if not region_stale:
                for adj in room_update.adjacency:
                    a=f"room:{adj.room_a}"; b=f"room:{adj.room_b}"
                    if a in next_nodes and b in next_nodes:
                        next_edges.add(GraphEdge(a,b,"room_adjacency","M8:region+M7 bridge",False))
                # M8's cadence-gated maintenance may retain an old RoomNode while
                # the accepted snapshot assigns one of its historical entities to
                # a current room.  The graph contract still permits only one direct
                # room owner, so current accepted membership wins.  Keep the stale
                # RoomNode and its provenance; suppress only the conflicting stale
                # room_contains edge at the M8 -> M9 boundary.
                current_room_ids = {
                    f"room:{room.room_id}"
                    for room in room_update.rooms
                    if room.anchor_region_id in current_region_ids
                }
                current_owners = {}
                for edge in sorted(next_edges):
                    if edge.relation != "room_contains" or edge.source_id not in current_room_ids:
                        continue
                    previous_owner = current_owners.get(edge.target_id)
                    if previous_owner is not None and previous_owner != edge.source_id:
                        raise ContractError("current accepted membership assigns entity to multiple RoomNodes")
                    current_owners[edge.target_id] = edge.source_id
                suppressed = {
                    edge for edge in next_edges
                    if edge.relation == "room_contains"
                    and edge.source_id not in current_room_ids
                    and edge.target_id in current_owners
                }
                if suppressed:
                    next_edges.difference_update(suppressed)
                    events.append("stale_room_ownership_reconciled")
        stale["regions"]=region_stale if current_regions is not None else stale["regions"]
        self._validate_graph(next_nodes,next_edges)
        next_revision=self._revision+1
        patch=GraphPatch(self._revision,next_revision,tuple(next_nodes[k] for k in sorted(next_nodes)),
            tuple(sorted(old_nodes.keys()-next_nodes.keys())),tuple(sorted(next_edges-old_edges)),tuple(sorted(old_edges-next_edges)))
        committed_nodes,committed_edges=patch.apply(old_nodes,old_edges,revision=self._revision)
        candidate_snapshot=SceneGraphSnapshot(1,packet.scene_id,packet.sequence_id,next_revision,packet.timestamp_s,
            tuple(committed_nodes[k] for k in sorted(committed_nodes)),tuple(sorted(committed_edges)),self.config_hash,
            stale,candidate_object_lifecycle)
        candidate_delta=self._make_delta(old_nodes,old_edges,packet.timestamp_s,events,stage_status,stale,packet,
            candidate_nodes=committed_nodes,candidate_edges=committed_edges,new_revision=next_revision,
            maintenance_pending=self._room_maintenance_pending,object_lifecycle=candidate_object_lifecycle,
            lifecycle_events=lifecycle_events if packet.detections is not None else ())
        # No public graph, scheduler or builder cache changes until every fallible
        # candidate operation above (including delta canonicalization) has succeeded.
        self._nodes,self._edges=committed_nodes,committed_edges; self._revision=next_revision
        self._scene=packet.scene_id; self._sequence=packet.sequence_id; self._last_timestamp=packet.timestamp_s; self._last_stale=stale
        self._agent_ids=candidate_agent_ids; self._last_regions=candidate_regions; self._last_tracks=candidate_tracks
        self._room_revision=candidate_room_revision; self._last_grid=candidate_grid; self._last_nav_edges=candidate_nav_edges
        self._object_lifecycle=candidate_object_lifecycle
        if current_regions is not None and not region_stale: self._have_region_snapshot=True
        self.scheduler.commit_candidate(scheduler_candidate)
        self._event_log.append({"frame_index":packet.frame_index,"input_mode":packet.input_mode,
            "events":tuple(events),"stage_status":stage_status,
            "object_lifecycle_events":[event.to_record() for event in (lifecycle_events if packet.detections is not None else ())]})
        return candidate_snapshot,candidate_delta

    @staticmethod
    def _validate_graph(nodes,edges):
        for edge in edges:
            if edge.source_id not in nodes or edge.target_id not in nodes: raise ContractError(f"dangling graph edge {edge}")
            if edge.source_id==edge.target_id: raise ContractError("self edge is not allowed")
        owners={}
        for edge in edges:
            if edge.relation=="room_contains":
                if edge.target_id in owners and owners[edge.target_id]!=edge.source_id: raise ContractError("entity has multiple direct RoomNode owners")
                owners[edge.target_id]=edge.source_id
        anchors=[n.attributes.get("anchor_region_id") for n in nodes.values() if n.kind=="room"]
        if len(anchors)!=len(set(anchors)): raise ContractError("more than one RoomNode anchors a region")

    def _make_delta(self,old_nodes,old_edges,timestamp,events,status,stale,packet,*,candidate_nodes,candidate_edges,new_revision,maintenance_pending,object_lifecycle,lifecycle_events=()):
        added=tuple(candidate_nodes[k] for k in sorted(candidate_nodes.keys()-old_nodes.keys()))
        updated=tuple(candidate_nodes[k] for k in sorted(candidate_nodes.keys()&old_nodes.keys()) if _canonical(candidate_nodes[k])!=_canonical(old_nodes[k]))
        removed=tuple(sorted(old_nodes.keys()-candidate_nodes.keys()))
        return GraphDelta(new_revision-1,new_revision,timestamp,added,updated,removed,
            tuple(sorted(candidate_edges-old_edges)),tuple(sorted(old_edges-candidate_edges)),tuple(events),
            {"input_mode":packet.input_mode,"frame_id":packet.frame_id,"frame_index":packet.frame_index,
             "scene_id":packet.scene_id,"sequence_id":packet.sequence_id,
             "processing_timestamp_s":timestamp,"stage_status":status,
             "room_maintenance_pending":maintenance_pending,
             "object_lifecycle_state":object_lifecycle,
             "object_lifecycle_events":[event.to_record() for event in lifecycle_events],
             "graph_transaction":"committed","backend_state_rollback":"not_guaranteed"},
            stale,{"builder":"M9-local-v3","m7":"standalone_m7","m8":"standalone_m8"})

    def flush(self,timestamp_s:float|None=None)->tuple[SceneGraphSnapshot,GraphDelta]:
        self._check_lifecycle("flush")
        if timestamp_s is None: timestamp_s=self._last_timestamp if self._last_timestamp is not None else 0.0
        if isinstance(timestamp_s,bool) or not isinstance(timestamp_s,(int,float)) or not math.isfinite(float(timestamp_s)):
            raise ContractError("flush timestamp must be finite")
        timestamp_s=float(timestamp_s); self.scheduler.validate_flush(timestamp_s)
        self._mutation_started=False
        try:
            old_nodes=dict(self._nodes); old_edges=set(self._edges)
            next_nodes=dict(old_nodes); next_edges=set(old_edges); events=[]
            next_room_revision=self._room_revision
            next_pending=self._room_maintenance_pending
            if self._room_maintenance_pending and self._have_region_snapshot:
                self._mutation_started=True
                self.rooms.maintenance_due(timestamp_s,terminal=True)
                entities=[]
                for aid in self._agent_ids:
                    node=old_nodes.get(aid)
                    if node:
                        pose=node.attributes["pose_W"]; entities.append(RoomEntity(aid,"agent",(pose[0],pose[1]),provenance="M9:agent_pose"))
                for obj in self.objects.snapshot():
                    entities.append(RoomEntity(f"object:{obj.object_id}","object",(obj.center_W[0],obj.center_W[1]),provenance=f"provider:{obj.last_provider_id}"))
                entities.extend(room_entity_from_nav_node(n) for n in self.navigation.snapshot())
                next_room_revision=self._room_revision+1
                room_update=self.rooms.update(self._last_regions,self._last_tracks,entities,revision=next_room_revision,
                    stale=False,nav_edges=self._last_nav_edges,maintenance=True)
                for key,node in tuple(next_nodes.items()):
                    if node.kind=="room": del next_nodes[key]
                next_edges={e for e in next_edges if e.relation not in ("room_contains","room_adjacency")}
                regions={int(r.persistent_id):r for r in self._last_regions if r.persistent_id is not None}
                for room in room_update.rooms:
                    reg=regions.get(room.anchor_region_id); rid=f"room:{room.room_id}"
                    prior=old_nodes.get(rid)
                    if reg is None:
                        if prior is not None: next_nodes[rid]=prior
                        continue
                    observed_at=float(reg.timestamp_s)
                    next_nodes[rid]=GraphNode(rid,"room",prior.first_seen_s if prior else observed_at,observed_at,
                        prior.provenance if prior else "M8:region_anchor",{
                        "room_id":room.room_id,"name":room.name,"anchor_region_id":room.anchor_region_id,
                        "outer_ring":room.polygon.outer,"holes":room.polygon.holes,"centroid_xy":room.centroid_xy,
                        "member_ids":room.member_ids,"created_reason":room.created_reason,"revision":room.revision,
                        "observation_timestamp_s":observed_at,"observation_revision":reg.source_grid_revision,
                        "processing_timestamp_s":timestamp_s})
                    for member in room.member_ids:
                        target=member if member in next_nodes else (f"object:{member}" if f"object:{member}" in next_nodes else member)
                        if target in next_nodes: next_edges.add(GraphEdge(rid,target,"room_contains","M8:membership",True))
                for adj in room_update.adjacency:
                    a=f"room:{adj.room_a}"; b=f"room:{adj.room_b}"
                    if a in next_nodes and b in next_nodes: next_edges.add(GraphEdge(a,b,"room_adjacency","M8:region+M7 bridge",False))
                events.append("room_maintenance_flushed"); next_pending=False
            self._validate_graph(next_nodes,next_edges)
            next_revision=self._revision+1
            patch=GraphPatch(self._revision,next_revision,tuple(next_nodes[k] for k in sorted(next_nodes)),
                tuple(sorted(old_nodes.keys()-next_nodes.keys())),tuple(sorted(next_edges-old_edges)),tuple(sorted(old_edges-next_edges)))
            committed_nodes,committed_edges=patch.apply(old_nodes,old_edges,revision=self._revision)
            candidate_snapshot=SceneGraphSnapshot(1,self._scene,self._sequence,next_revision,timestamp_s,
                tuple(committed_nodes[k] for k in sorted(committed_nodes)),tuple(sorted(committed_edges)),self.config_hash,
                self._last_stale,self._object_lifecycle)
            added=tuple(committed_nodes[k] for k in sorted(committed_nodes.keys()-old_nodes.keys()))
            updated=tuple(committed_nodes[k] for k in sorted(committed_nodes.keys()&old_nodes.keys()) if _canonical(committed_nodes[k])!=_canonical(old_nodes[k]))
            removed=tuple(sorted(old_nodes.keys()-committed_nodes.keys()))
            events.append("terminal_flush")
            candidate_delta=GraphDelta(next_revision-1,next_revision,timestamp_s,added,updated,removed,
                tuple(sorted(committed_edges-old_edges)),tuple(sorted(old_edges-committed_edges)),tuple(events),
                {"processing_timestamp_s":timestamp_s,"scene_id":self._scene,"sequence_id":self._sequence,
                 "room_maintenance_pending":next_pending,
                 "object_lifecycle_state":self._object_lifecycle,
                 "object_lifecycle_events":[],
                 "graph_transaction":"committed","backend_state_rollback":"not_guaranteed"},
                dict(self._last_stale),{"builder":"M9-local-v3","m8":"standalone_m8"})
            # Snapshot and delta are fully constructed before the terminal commit.
            self._nodes,self._edges=committed_nodes,committed_edges
            self._revision=next_revision; self._last_timestamp=timestamp_s; self._flushed=True
            self._room_revision=next_room_revision; self._room_maintenance_pending=next_pending
            self._event_log.append({"event":"terminal_flush","maintenance":("room_maintenance_flushed" in events)})
            return candidate_snapshot,candidate_delta
        except Exception as exc:
            if self._mutation_started: self._failed_reason=f"{type(exc).__name__}: {exc}"
            raise

    def canonical_hash(self)->str:
        """Replay hash omitting timestamps and environment/source metadata."""
        snapshot=self.snapshot(); nodes=[]
        for node in snapshot.nodes:
            attrs=_plain(node.attributes); attrs.pop("source_metadata",None)
            nodes.append({"node_id":node.node_id,"kind":node.kind,"provenance":node.provenance,"attributes":attrs})
        payload={"scene_id":snapshot.scene_id,"sequence_id":snapshot.sequence_id,"revision":snapshot.revision,
                 "nodes":nodes,"edges":[_plain(e) for e in snapshot.edges],"stale_flags":_plain(snapshot.stale_flags),
                 "object_lifecycle":_plain(snapshot.object_lifecycle)}
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()

    def canonical_event_hash(self)->str:
        """Deterministic state-transition hash; wall-clock timestamps are excluded."""
        payload=_plain(self._event_log)
        return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()

    def close(self):
        self._closed=True
        errors=[]
        signals=[]
        seen=set()
        for stage in (self.region_stage,self.mapping_stage):
            if stage is None or id(stage) in seen or id(stage) in self._closed_stage_ids:
                continue
            seen.add(id(stage)); closer=getattr(stage,"close",None)
            if not callable(closer):
                self._closed_stage_ids.add(id(stage))
                continue
            try:
                closer()
            except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
                # Do not turn cancellation into an ordinary cleanup result,
                # but still give every owned stage its cleanup opportunity.
                signals.append(exc)
                continue
            except BaseException as exc:
                errors.append(exc)
                continue
            self._closed_stage_ids.add(id(stage))
        if signals:
            if errors:
                setattr(signals[0], "cleanup_errors", tuple(errors))
                add_note = getattr(signals[0], "add_note", None)
                if callable(add_note):
                    add_note("cleanup errors: " + "; ".join(str(error) for error in errors))
            raise signals[0]
        if errors:
            raise CleanupError(errors)
