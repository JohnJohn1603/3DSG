from __future__ import annotations
import numpy as np
import pytest
import math

from standalone_sg.config import NavigationConfig, RoomConfig
from standalone_sg.contracts import CanonicalRegion, ContractError, DetectionBatch, Detection3D, Polygon2D
from standalone_sg.mapping.projection import FreeSpaceGrid2D
from standalone_sg.navigation import NavigationBlockGraph
from standalone_sg.pipeline import FramePacket, GraphEdge, SceneGraphBuilder
from standalone_sg.rooms import RoomAnchoringManager


def region(rid=1, bounds=(0.0,0.0,4.0,4.0), adjacent=()):
    x0,y0,x1,y1=bounds
    return CanonicalRegion(rid,rid,Polygon2D(((x0,y0),(x1,y0),(x1,y1),(x0,y1))),
        ((x0+x1)/2,(y0+y1)/2),(x1-x0)*(y1-y0),1.0,1,tuple(adjacent))

def packet(i, *, regions=None, grid=None, detections=None, pose=None, mode=None):
    if mode is None: mode="grid_regions" if grid is not None and regions is not None else ("grid" if grid is not None else "regions")
    return FramePacket("scene","seq",f"f{i}",i,float(i),mode,base_pose_W=pose,grid=grid,
        regions=None if mode=="grid" else (() if regions is None and mode=="regions" else regions),
        detections=detections)

def test_builder_commits_revision_rooms_and_immutable_snapshots():
    manager=RoomAnchoringManager()
    builder=SceneGraphBuilder(rooms=manager)
    first,_=builder.update(packet(0,regions=(region(),),pose=(1,1,0)))
    room=next(n for n in first.nodes if n.kind=="room")
    assert room.attributes["anchor_region_id"]==1
    with pytest.raises(TypeError): room.attributes["x"]=1
    second,delta=builder.update(packet(1,regions=(region(1,(0,0,5,4)),),pose=(2,1,0)))
    assert second.revision==2 and delta.base_revision==1 and delta.new_revision==2
    assert any(e.relation=="temporal_successor" for e in second.edges)
    assert next(n for n in second.nodes if n.node_id==room.node_id).first_seen_s==0
    assert not any(e.source_id not in {n.node_id for n in second.nodes} or e.target_id not in {n.node_id for n in second.nodes} for e in second.edges)

def test_scheduler_rejects_duplicate_and_decreasing_time_without_graph_commit():
    b=SceneGraphBuilder(); b.update(packet(2,regions=()))
    snapshot=b.snapshot()
    with pytest.raises(ContractError,match="duplicate"): b.update(packet(2,regions=()))
    with pytest.raises(ContractError,match="timestamp"): b.update(FramePacket("scene","seq","f3",3,1.0,"regions",regions=()))
    assert b.snapshot()==snapshot

def test_empty_packet_and_detection_absent_vs_explicit_empty_are_visible():
    b=SceneGraphBuilder()
    s1,d1=b.update(packet(0,regions=()))
    assert d1.diagnostics["stage_status"]["objects"]=="provider_absent"
    empty=DetectionBatch("detector","f1",1.0,())
    s2,d2=b.update(packet(1,regions=(),detections=empty))
    assert d2.diagnostics["stage_status"]["objects"]=="empty_frame"
    assert s2.revision==s1.revision+1

def test_grid_drives_m7_and_region_snapshot_drives_m8():
    a=np.zeros((4,4),np.int8); z=np.full(a.shape,np.nan)
    g=FreeSpaceGrid2D(a,1.0,(0,0),z,z,revision=4)
    b=SceneGraphBuilder(navigation=NavigationBlockGraph(NavigationConfig(cell_stride_cells=2,min_free_cell_count=1,connectivity=4)))
    snap,delta=b.update(packet(0,regions=(region(1,(-1,-1,5,5)),),grid=g))
    assert {n.kind for n in snap.nodes}>={"navigation","room"}
    assert delta.diagnostics["stage_status"]["navigation"]=="updated"
    assert all(n.attributes["robot_footprint_passability"] is False for n in snap.nodes if n.kind=="navigation")

def test_input_mode_validation_precedes_stage_calls_and_commit():
    class Mapping:
        calls=0
        def build_grid(self,p): self.calls+=1; raise RuntimeError("must not run")
    m=Mapping(); b=SceneGraphBuilder(mapping_stage=m)
    with pytest.raises(ContractError):
        FramePacket("scene","seq","bad",0,0.0,"rgbd")
    assert m.calls==0 and b.snapshot().revision==0

def test_mapping_failure_leaves_graph_and_scheduler_uncommitted():
    class Mapping:
        def build_grid(self,p): raise RuntimeError("mapping failed")
    b=SceneGraphBuilder(mapping_stage=Mapping())
    before=b.snapshot()
    p=FramePacket("scene","seq","f0",0,0.0,"occupancy",occupancy_snapshot={"revision":1})
    with pytest.raises(RuntimeError,match="mapping failed"): b.update(p)
    assert b.snapshot()==before and b.scheduler.cursor.frame_index==-1
    assert b.failed and b.replay_required
    with pytest.raises(ContractError,match="replay required"): b.update(packet(0,regions=()))

def test_flush_and_close_are_explicit_and_idempotent():
    b=SceneGraphBuilder(); b.update(packet(0,regions=()))
    snap,delta=b.flush(1.0)
    assert snap.revision==2 and delta.events==("terminal_flush",)
    b.close(); b.close()
    with pytest.raises(ContractError,match="closed"): b.update(packet(1,regions=()))

def test_same_sequence_has_same_canonical_graph_hash():
    def run():
        b=SceneGraphBuilder(config_hash="fixture")
        b.update(packet(0,regions=(region(),),pose=(1,2,0)))
        b.update(packet(1,regions=(region(1,(0,0,5,4)),),pose=(2,2,0)))
        return b.canonical_hash(),b.canonical_event_hash()
    assert run()==run()


def test_canonical_offline_packet_integrates_agent_object_navigation_and_room_layers():
    a=np.zeros((4,4),np.int8); z=np.full(a.shape,np.nan)
    g=FreeSpaceGrid2D(a,1.0,(0,0),z,z,revision=1)
    detection=Detection3D("d1",0.0,(1.0,1.0,0.5),"synthetic_cached_detection",class_name="chair",confidence=.8)
    batch=DetectionBatch("cached-fixture","f0",0.0,(detection,),{"fixture":"synthetic"})
    b=SceneGraphBuilder(navigation=NavigationBlockGraph(NavigationConfig(cell_stride_cells=2,min_free_cell_count=1,connectivity=4)))
    snap,delta=b.update(packet(0,regions=(region(1,(-1,-1,5,5)),),grid=g,detections=batch,pose=(2,2,0)))
    assert {n.kind for n in snap.nodes}>={"agent","object","navigation","room"}
    assert any(e.relation=="room_contains" and e.target_id.startswith("object:") for e in snap.edges)
    ids={n.node_id for n in snap.nodes}
    assert all(e.source_id in ids and e.target_id in ids for e in snap.edges)
    assert delta.diagnostics["stage_status"]["objects"]=="processed"

def test_stale_region_packet_preserves_room_snapshot_and_is_observable():
    b=SceneGraphBuilder()
    fresh=FramePacket("scene","seq","f0",0,0.0,"regions",base_pose_W=(1,1,0),regions=(region(),))
    first,_=b.update(fresh); before=next(n for n in first.nodes if n.kind=="room")
    stale=FramePacket("scene","seq","f1",1,5.0,"regions",base_pose_W=(2,1,0),regions=(),regions_stale=True)
    second,delta=b.update(stale); after=next(n for n in second.nodes if n.kind=="room")
    assert after.node_id==before.node_id and after.attributes["anchor_region_id"]==1
    assert second.stale_flags["regions"] is True
    assert "regions_stale" in delta.events and delta.diagnostics["graph_transaction"]=="committed"


def test_navigation_edges_are_reconciled_when_bridge_disappears_and_returns():
    nav=NavigationBlockGraph(NavigationConfig(cell_stride_cells=2,min_free_cell_count=2,connectivity=4))
    b=SceneGraphBuilder(navigation=nav)
    def grid(revision,blocked=False):
        a=np.zeros((2,4),np.int8)
        if blocked:
            a[:,1:3]=1  # keep two free cells in each block, sever the cross-block bridge
        z=np.full(a.shape,np.nan)
        return FreeSpaceGrid2D(a,1.0,(0,0),z,z,revision)
    s1,d1=b.update(packet(0,grid=grid(1)))
    assert sum(e.relation=="navigable_path" for e in s1.edges)==2
    s2,d2=b.update(packet(1,grid=grid(2,True)))
    assert sum(n.kind=="navigation" for n in s2.nodes)==2
    assert not any(e.relation=="navigable_path" for e in s2.edges)
    assert len([e for e in d2.removed_edges if e.relation=="navigable_path"])==2
    s3,d3=b.update(packet(2,grid=grid(3)))
    assert sum(e.relation=="navigable_path" for e in s3.edges)==2
    assert len([e for e in d3.added_edges if e.relation=="navigable_path"])==2

def test_room_stale_preserves_observation_time_while_processing_time_advances():
    b=SceneGraphBuilder()
    fresh=FramePacket("scene","seq","f0",0,0.0,"regions",base_pose_W=(1,1,0),regions=(region(),))
    first,_=b.update(fresh); old=next(n for n in first.nodes if n.kind=="room")
    stale=FramePacket("scene","seq","f1",1,5.0,"regions",base_pose_W=(2,1,0),regions=(),regions_stale=True)
    second,delta=b.update(stale); current=next(n for n in second.nodes if n.kind=="room")
    assert current.last_seen_s==old.last_seen_s and current.provenance==old.provenance
    assert current.attributes["observation_timestamp_s"]==old.attributes["observation_timestamp_s"]
    assert current.attributes["observation_revision"]==old.attributes["observation_revision"]
    assert second.timestamp_s==5.0 and delta.diagnostics["processing_timestamp_s"]==5.0

def test_navnode_first_seen_is_stable_across_grid_observations():
    nav=NavigationBlockGraph(NavigationConfig(cell_stride_cells=2,min_free_cell_count=1,connectivity=4)); b=SceneGraphBuilder(navigation=nav)
    def make_grid(rev):
        a=np.zeros((2,2),np.int8);z=np.full(a.shape,np.nan);return FreeSpaceGrid2D(a,1.0,(0,0),z,z,rev)
    first,_=b.update(packet(0,grid=make_grid(1))); old=next(n for n in first.nodes if n.kind=="navigation")
    second,_=b.update(packet(1,grid=make_grid(2))); new=next(n for n in second.nodes if n.node_id==old.node_id)
    assert new.first_seen_s==old.first_seen_s==0.0
    assert new.last_seen_s==1.0 and new.attributes["observation_timestamp_s"]==1.0

def test_room_maintenance_cadence_defers_prune_until_terminal_flush():
    b=SceneGraphBuilder()
    first=FramePacket("scene","seq","f0",0,0.0,"regions",base_pose_W=(1,1,0),regions=(region(),))
    initial,_=b.update(first); room=next(n for n in initial.nodes if n.kind=="room")
    # Within the source 2-second period: immediate membership update, deferred relink/prune.
    second=FramePacket("scene","seq","f1",1,0.2,"regions",regions=())
    interim,delta=b.update(second)
    assert any(n.node_id==room.node_id for n in interim.nodes)
    assert b._room_maintenance_pending
    flushed,flush_delta=b.flush(10.0)
    assert not any(n.node_id==room.node_id for n in flushed.nodes)
    assert room.node_id in flush_delta.removed_node_ids
    assert "room_maintenance_flushed" in flush_delta.events
    assert flushed.timestamp_s==10.0
    with pytest.raises(ContractError,match="flushed"):
        b.update(FramePacket("scene","seq","f2",2,2.0,"regions",regions=()))

def test_flush_timestamp_cannot_move_graph_clock_backwards():
    b=SceneGraphBuilder(); b.update(packet(0,regions=()))
    before=b.snapshot()
    with pytest.raises(ContractError,match="flush timestamp"):
        b.flush(-1.0)
    assert b.snapshot()==before and not b._flushed

def test_mutating_stage_failure_poison_builder_but_preserves_last_graph_and_allows_close():
    from types import SimpleNamespace
    class RegionStage:
        mutated=False
        def update(self,grid,*,timestamp_s):
            self.mutated=True
            return SimpleNamespace(backend_status="synthetic",stale=False,accepted=True,current_regions=(),retained_tracks=())
    class Objects:
        def update(self,batch): raise RuntimeError("downstream object-stage failure")
    stage=RegionStage(); nav=NavigationBlockGraph(NavigationConfig(cell_stride_cells=2,min_free_cell_count=1))
    b=SceneGraphBuilder(region_stage=stage,objects=Objects(),navigation=nav)
    before=b.snapshot(); a=np.zeros((2,2),np.int8);z=np.full(a.shape,np.nan);g=FreeSpaceGrid2D(a,1.0,(0,0),z,z,1)
    batch=DetectionBatch("detector","f0",0.0,())
    p=FramePacket("scene","seq","f0",0,0.0,"grid",grid=g,detections=batch)
    with pytest.raises(RuntimeError,match="downstream object-stage failure"): b.update(p)
    assert stage.mutated and b.failed and b.replay_required and b.snapshot()==before
    with pytest.raises(ContractError,match="replay required"): b.update(packet(1,regions=()))
    with pytest.raises(ContractError,match="replay required"): b.flush()
    b.close()

def test_pre_stage_validation_failure_does_not_poison_builder():
    b=SceneGraphBuilder(); b.update(packet(0,regions=()))
    with pytest.raises(ContractError,match="duplicate"): b.update(packet(0,regions=()))
    assert not b.failed
    b.update(packet(1,regions=()))


def test_membership_updates_immediately_while_maintenance_prune_is_cadence_gated():
    b=SceneGraphBuilder()
    first=FramePacket("scene","seq","f0",0,0.0,"regions",base_pose_W=(1,1,0),regions=(region(),))
    s1,_=b.update(first); room=next(n for n in s1.nodes if n.kind=="room")
    det=Detection3D("chair1",0.2,(2.0,2.0,0.4),"synthetic",class_name="chair")
    batch=DetectionBatch("cache","f1",0.2,(det,))
    second=FramePacket("scene","seq","f1",1,0.2,"regions",base_pose_W=(1,1,0),regions=(region(),),detections=batch)
    s2,_=b.update(second)
    object_node=next(n for n in s2.nodes if n.kind=="object")
    assert GraphEdge(room.node_id,object_node.node_id,"room_contains","M8:membership",True) in s2.edges
    assert b._room_maintenance_pending  # accepted snapshot awaits the 0.5 Hz maintenance pass


def test_current_room_ownership_replaces_conflicting_retained_room_edge():
    """A cadence-retained old room must not double-own a historical agent node."""
    a=np.zeros((2,2),np.int8); z=np.full(a.shape,np.nan)
    grid1=FreeSpaceGrid2D(a,1.0,(-1.0,-1.0),z,z,1)
    grid2=FreeSpaceGrid2D(a,1.0,(-1.0,-1.0),z,z,2)
    first_region=region(1,(0.0,-1.0,2.0,1.0))
    replacement_region=region(2,(0.8,-1.0,2.8,1.0))
    b=SceneGraphBuilder()

    first,_=b.update(packet(0,grid=grid1,regions=(first_region,),pose=(0.9,0.0,0.0)))
    old_room=next(node for node in first.nodes if node.kind=="room")
    second,delta=b.update(packet(1,grid=grid2,regions=(replacement_region,),pose=(0.9,0.0,0.0)))

    ownership={}
    for edge in second.edges:
        if edge.relation=="room_contains":
            assert edge.target_id not in ownership
            ownership[edge.target_id]=edge.source_id
    historical_agent="agent:scene:seq:f0"
    current_room=next(node for node in second.nodes if node.kind=="room" and node.node_id != old_room.node_id)
    assert ownership[historical_agent]==current_room.node_id
    assert GraphEdge(old_room.node_id,historical_agent,"room_contains","M8:membership",True) in delta.removed_edges
    assert "stale_room_ownership_reconciled" in delta.events


@pytest.mark.parametrize("metadata", [
    {"nan":float("nan")}, {"pos_inf":float("inf")}, {"neg_inf":float("-inf")},
    {"opaque":object()}, {1:"non-string key"},
])
def test_frame_metadata_rejects_non_json_and_nonfinite_values(metadata):
    with pytest.raises(ContractError,match="metadata|JSON|finite|keys"):
        FramePacket("scene","seq","f0",0,0.0,"regions",regions=(),source_metadata=metadata)

def test_delta_construction_failure_keeps_graph_revision_and_scheduler_candidate_uncommitted(monkeypatch):
    b=SceneGraphBuilder(navigation=NavigationBlockGraph(NavigationConfig(cell_stride_cells=2,min_free_cell_count=1)))
    def grid(rev):
        a=np.zeros((2,2),np.int8);z=np.full(a.shape,np.nan);return FreeSpaceGrid2D(a,1.0,(0,0),z,z,rev)
    first,_=b.update(packet(0,grid=grid(1)))
    cursor=b.scheduler.cursor; nav_before=b.navigation.snapshot()
    def fail_delta(*args,**kwargs): raise ValueError("injected delta canonicalization failure")
    monkeypatch.setattr(b,"_make_delta",fail_delta)
    with pytest.raises(ValueError,match="delta canonicalization"):
        b.update(packet(1,grid=grid(2)))
    assert b.snapshot()==first and b.scheduler.cursor==cursor
    assert b.navigation.snapshot()!=nav_before  # backend state may have advanced
    assert b.failed and b.replay_required
    with pytest.raises(ContractError,match="replay required"): b.update(packet(2,regions=()))

def test_flush_delta_failure_keeps_committed_snapshot_and_marks_replay_required(monkeypatch):
    import standalone_sg.pipeline.builder as builder_module
    b=SceneGraphBuilder()
    first=FramePacket("scene","seq","f0",0,0.0,"regions",base_pose_W=(1,1,0),regions=(region(),))
    b.update(first)
    second=FramePacket("scene","seq","f1",1,0.2,"regions",regions=())
    b.update(second)
    assert b._room_maintenance_pending
    committed=b.snapshot(); cursor=b.scheduler.cursor
    class BrokenDelta:
        def __init__(self,*args,**kwargs): raise ValueError("flush delta serialization failure")
    monkeypatch.setattr(builder_module,"GraphDelta",BrokenDelta)
    with pytest.raises(ValueError,match="flush delta serialization"):
        b.flush(10.0)
    assert b.snapshot()==committed and b.scheduler.cursor==cursor and not b._flushed
    assert b.failed and b.replay_required
    b.close()
