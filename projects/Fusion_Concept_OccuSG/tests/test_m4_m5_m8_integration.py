"""Local M4 worker → M5 tracker/guard → M8 room integration, synthetic entities."""
from __future__ import annotations
import numpy as np
from standalone_sg.navigation import NavEdge
from standalone_sg.regions.dude_client import DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor
from standalone_sg.rooms import RoomAnchoringManager,RoomEntity
from test_m4_dude import BIN,grid


def _synthetic_entities(regions):
    ordered=sorted(regions,key=lambda r:int(r.persistent_id))
    if not ordered:return (),()
    # Synthetic cached graph state, not detector output, Habitat/Webots data, or GT IDs.
    entities=[RoomEntity("synthetic-agent-bootstrap","agent",ordered[0].centroid_xy,provenance="synthetic_doorway_fixture")]
    for index,region in enumerate(ordered):
        rid=int(region.persistent_id); cx,cy=region.centroid_xy
        entities.append(RoomEntity(f"synthetic-object-region-{rid}","object",(cx,cy),provenance="synthetic_doorway_fixture"))
        half=.05
        entities.append(RoomEntity(f"nav:{index}:0","navigation",(cx,cy),
                                   (cx-half,cy-half,cx+half,cy+half),
                                   "synthetic_doorway_fixture"))
    edges=(NavEdge((0,0),(1,0),int(ordered[0].source_grid_revision)),
           NavEdge((1,0),(0,0),int(ordered[0].source_grid_revision))) if len(ordered)>1 else ()
    return tuple(entities),edges


def test_m4_m5_doorway_sequence_promotes_stable_rooms_then_stale_rejection_preserves_them():
    height=width=180
    hidden=np.full((height,width),-1,dtype=np.int8); hidden[40:140,15:90]=0
    revealed=hidden.copy(); revealed[50:130,110:175]=0; revealed[85:95,90:111]=0
    unknown=np.full((height,width),-1,dtype=np.int8)
    processor=RegionUpdateProcessor(); rooms=RoomAnchoringManager()
    previous_room_by_region={}; states=[]
    with DuDeWorkerClient(BIN) as worker:
        accepted=[]
        for revision,mask in enumerate((hidden,revealed),1):
            response,result=worker.update_regions(processor,grid(mask,revision,origin=(-4.,-2.),resolution=.05),
                                                  timestamp_s=float(revision))
            assert response["status"] in ("success","fallback")
            assert result.accepted and not result.stale and result.m5_result is not None
            assert all(entity.provenance=="synthetic_doorway_fixture" for entity in _synthetic_entities(result.current_regions)[0])
            entities,edges=_synthetic_entities(result.current_regions)
            graph=rooms.update(result.current_regions,result.retained_tracks,entities,revision=revision,nav_edges=edges,expired_track_ids=result.expired_ids)
            accepted.append((result,graph))
        before=accepted[-1][1]
        rejected_response,rejected=worker.update_regions(processor,grid(unknown,3,origin=(-4.,-2.),resolution=.05),timestamp_s=3.)
        assert rejected.stale and not rejected.accepted
        after=rooms.update(rejected.current_regions,rejected.retained_tracks,(),revision=3,stale=rejected.stale,
                           nav_edges=(),expired_track_ids=rejected.expired_ids)
    first_m5,first_graph=accepted[0]; second_m5,second_graph=accepted[1]
    assert len(first_m5.current_regions)==1 and len(first_graph.rooms)==1
    assert len(second_m5.current_regions)==2 and len(second_graph.rooms)==2
    assert len(second_graph.adjacency)==1
    first_anchor=int(first_m5.current_regions[0].persistent_id)
    stable_room=next(r for r in first_graph.rooms if r.anchor_region_id==first_anchor)
    after_room=next(r for r in second_graph.rooms if r.anchor_region_id==first_anchor)
    assert after_room.room_id==stable_room.room_id
    assert all(e.room_a in {r.room_id for r in after.rooms} and e.room_b in {r.room_id for r in after.rooms} for e in after.adjacency)
    assert after.rooms==before.rooms and after.removed_room_ids==() and after.stale
    # The scene evidence here is synthetic; this is local module integration, not simulator E2E.
