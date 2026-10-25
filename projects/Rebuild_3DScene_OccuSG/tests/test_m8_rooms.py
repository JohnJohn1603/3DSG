from __future__ import annotations

import math

from standalone_sg.config import RoomConfig
from standalone_sg.contracts.regions import CanonicalRegion, Polygon2D, TrackedRegion
from standalone_sg.navigation import NavEdge
from standalone_sg.rooms import RegionMembershipResolver, RoomAnchoringManager, RoomEntity
from standalone_sg.rooms.geometry import intersection_area, _triangulate_cached


def region(rid, bounds, *, adjacent=(), holes=(), rev=1):
    x0,y0,x1,y1=bounds
    return CanonicalRegion(rid,rid,Polygon2D(((x0,y0),(x1,y0),(x1,y1),(x0,y1)),holes),
                           ((x0+x1)/2,(y0+y1)/2),(x1-x0)*(y1-y0),float(rev),rev,tuple(adjacent))

def track(rid,status="current"):
    poly=Polygon2D(((0,0),(1,0),(1,1),(0,1)))
    return TrackedRegion(rid,poly,(.5,.5),1.0,0.0,1.0,0,status,rid,1)

def nav(entity_id,x,y,bounds):return RoomEntity(entity_id,"navigation",(x,y),bounds)


def test_no_meaningful_member_does_not_promote_geometric_region_but_agent_does():
    manager=RoomAnchoringManager(); r=region(1,(0,0,5,5))
    empty=manager.update((r,),(track(1),),(),revision=1)
    assert empty.rooms==() and empty.created_room_ids==()
    promoted=manager.update((r,),(track(1),),(RoomEntity("agent-1","agent",(2,2)),),revision=2)
    assert len(promoted.rooms)==1 and promoted.rooms[0].created_reason=="agent_bootstrap"
    assert promoted.rooms[0].member_ids==("agent-1",)
    assert not hasattr(promoted.rooms[0],"semantic_label")


def test_object_and_navigation_membership_and_one_region_one_room():
    manager=RoomAnchoringManager(); r1=region(1,(0,0,4,4)); r2=region(2,(3,0,7,4))
    entities=(RoomEntity("obj","object",(3.5,2)),nav("nav:0:0",3.2,2,(2.8,1.5,3.6,2.5)))
    update=manager.update((r1,r2),(track(1),track(2)),entities,revision=1)
    assert len(update.rooms)==1  # both entities resolve into exactly one current region
    assert all(a.region_id in (1,2,None) for a in update.assignments)
    assert sum("obj" in room.member_ids for room in update.rooms)==1


def test_boundary_epsilon_and_sticky_tie_are_source_ordered():
    resolver=RegionMembershipResolver(RoomConfig(boundary_epsilon_m=.15))
    left=region(1,(0,0,5,5)); right=region(2,(5,0,10,5))
    first=resolver.resolve((left,right),(RoomEntity("agent","agent",(4.9,2)),))
    assert first[0].region_id==1
    boundary=resolver.resolve((left,right),(RoomEntity("agent","agent",(5,2)),))
    assert boundary[0].region_id==1 and boundary[0].used_tiebreak
    outside=resolver.resolve((left,right),(RoomEntity("far","object",(10.16,2)),))
    assert outside[0].region_id is None


def test_navigation_overlap_tie_break_uses_footprint_overlap_then_stable_id():
    resolver=RegionMembershipResolver()
    small=region(1,(0,0,4,4)); larger=region(2,(2,0,8,4))
    assigned=resolver.resolve((small,larger),(nav("nav:0:0",4,2,(1,1,7,3)),))
    assert assigned[0].region_id==2
    assert assigned[0].plausible_region_ids==(1,2)


def test_repeated_navigation_overlap_reuses_exact_triangulation_without_changing_area():
    box = Polygon2D(((0, 0), (6, 0), (6, 4), (0, 4)))
    region_polygon = Polygon2D(((2, -1), (8, -1), (8, 3), (2, 3)))
    before = _triangulate_cached.cache_info()
    first = intersection_area(box, region_polygon)
    second = intersection_area(box, region_polygon)
    after = _triangulate_cached.cache_info()
    assert math.isclose(first, 12.0, rel_tol=0.0, abs_tol=1e-9)
    assert math.isclose(second, 12.0, rel_tol=0.0, abs_tol=1e-9)
    assert after.hits > before.hits


def test_single_navigation_candidate_skips_expensive_overlap_intersection(monkeypatch):
    import standalone_sg.rooms.membership as membership

    monkeypatch.setattr(membership, "intersection_area", lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected overlap")))
    assigned = RegionMembershipResolver().resolve(
        (region(1, (0, 0, 4, 4)),),
        (nav("nav:0:0", 2, 2, (1, 1, 3, 3)),),
    )
    assert assigned[0].region_id == 1


def test_holes_are_supported_by_contract_geometry_but_not_claimed_as_m4_output():
    p=Polygon2D(((0,0),(10,0),(10,10),(0,10)),(((4,4),(6,4),(6,6),(4,6)),))
    r=CanonicalRegion(1,1,p,(5,5),96,1.0,1)
    assigned=RegionMembershipResolver().resolve((r,),(RoomEntity("hole-object","object",(5,5)),))
    assert assigned[0].region_id is None


def test_grow_doorway_new_region_creates_room_and_adjacency_without_dangling_edges():
    manager=RoomAnchoringManager()
    r1=region(1,(0,0,4,4))
    t1=manager.update((r1,),(track(1),),(RoomEntity("agent","agent",(2,2)),nav("nav:0:0",3.5,2,(3,1.5,4,2.5))),revision=1)
    room1=t1.rooms[0].room_id
    grown=region(1,(0,0,5,4))
    t2=manager.update((grown,),(track(1),),(RoomEntity("agent","agent",(2,2)),nav("nav:0:0",4.7,2,(4.2,1.5,5.2,2.5))),revision=2)
    assert t2.rooms[0].room_id==room1 and t2.rooms[0].anchor_region_id==1
    r1=region(1,(0,0,5,4),adjacent=(2,),rev=3); r2=region(2,(5,0,10,4),adjacent=(1,),rev=3)
    ents=(RoomEntity("agent","agent",(2,2)),RoomEntity("object-2","object",(8,2)),
          nav("nav:0:0",4.7,2,(4.2,1.5,5.0,2.5)),nav("nav:1:0",5.3,2,(5.0,1.5,5.8,2.5)))
    edges=(NavEdge((0,0),(1,0),3),NavEdge((1,0),(0,0),3))
    t3=manager.update((r1,r2),(track(1),track(2)),ents,revision=3,nav_edges=edges)
    assert len(t3.rooms)==2 and len(t3.adjacency)==1
    assert (t3.adjacency[0].room_a,t3.adjacency[0].room_b)==tuple(sorted(r.room_id for r in t3.rooms))
    assert all(e.room_a in {r.room_id for r in t3.rooms} and e.room_b in {r.room_id for r in t3.rooms} for e in t3.adjacency)
    assert len({r.anchor_region_id for r in t3.rooms})==len(t3.rooms)


def test_current_snapshot_missing_but_retained_track_prunes_anchor_unless_snapshot_is_stale():
    manager=RoomAnchoringManager(); r=region(9,(0,0,5,5))
    first=manager.update((r,),(track(9),),(RoomEntity("agent","agent",(2,2)),),revision=1)
    assert first.rooms
    missing=manager.update((),(track(9,"temporarily_missing"),),(),revision=2)
    assert missing.retained_track_ids==(9,) and missing.removed_room_ids==(first.rooms[0].room_id,)
    # Backend/guard stale snapshot means source maintenance skips destructive pruning.
    second=RoomAnchoringManager(); seeded=second.update((r,),(track(9),),(RoomEntity("agent","agent",(2,2)),),revision=1)
    stale=second.update((),(track(9,"temporarily_missing"),),(),revision=2,stale=True)
    assert stale.rooms==seeded.rooms and stale.removed_room_ids==()
    expired=second.update((),(),(),revision=3,expired_track_ids=(9,))
    assert expired.expired_track_ids==(9,) and expired.rooms==()


def test_relink_split_successor_and_margin_rejection_keep_id_rules_explicit():
    manager=RoomAnchoringManager(); old=region(1,(0,0,10,10))
    members=(RoomEntity("agent","agent",(2,2)),RoomEntity("object","object",(3,3)))
    seeded=manager.update((old,),(track(1),),members,revision=1); room_id=seeded.rooms[0].room_id
    replacement=region(2,(0,0,9,10))
    relink=manager.update((replacement,),(track(2),),members,revision=2)
    assert relink.relinked==((room_id,1,2),) and relink.rooms[0].room_id==room_id
    # Two equally scoring replacement candidates fall inside the source margin and are refused.
    left=region(3,(0,0,9,10)); right=region(4,(0,0,9,10))
    ambiguous=manager.update((left,right),(track(3),track(4)),members,revision=3)
    assert ambiguous.relinked==() and room_id in ambiguous.removed_room_ids
    assert len(ambiguous.rooms)==1  # deterministic membership tie assigns the entities to one region


def test_source_maintenance_cadence_and_terminal_flush():
    manager=RoomAnchoringManager(RoomConfig(maintenance_hz=.5))
    assert manager.maintenance_due(10.0)
    assert not manager.maintenance_due(11.99)
    assert manager.maintenance_due(12.0)
    assert manager.maintenance_due(12.1,terminal=True)
    assert not manager.maintenance_due(12.2)
