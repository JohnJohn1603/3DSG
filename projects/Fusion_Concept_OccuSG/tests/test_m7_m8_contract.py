from __future__ import annotations
import numpy as np
import pytest
from standalone_sg.config import load_config
from standalone_sg.contracts import ContractError
from standalone_sg.contracts.regions import CanonicalRegion,Polygon2D
from standalone_sg.mapping import FreeSpaceGrid2D
from standalone_sg.navigation import NavigationBlockGraph
from standalone_sg.rooms import RegionMembershipResolver,RoomAnchoringManager,RoomEntity,room_entity_from_nav_node


def make_grid(shape,revision,origin=(0.,0.),res=.05):
    a=np.zeros(shape,np.int8); nan=np.full(shape,np.nan)
    return FreeSpaceGrid2D(a,res,origin,nan,nan,revision)
def region(rid,bounds,adjacent=()):
    x0,y0,x1,y1=bounds
    return CanonicalRegion(rid,rid,Polygon2D(((x0,y0),(x1,y0),(x1,y1),(x0,y1))),((x0+x1)/2,(y0+y1)/2),(x1-x0)*(y1-y0),1.,1,tuple(adjacent))

def test_real_m7_node_adapter_uses_polygon_bounds_order_and_preserves_geometry():
    graph=NavigationBlockGraph(load_config("configs/reference_code_mp3d.yaml").navigation)
    positive=graph.update(make_grid((15,15),1)).nodes[0]
    assert positive.bounds_xy==(0.0,0.0,.75,.75)
    assert positive.center_xy==(.375,.375)
    entity=room_entity_from_nav_node(positive)
    assert entity.entity_id==positive.node_id and entity.center_xy==positive.center_xy
    assert entity.bounds_xy==positive.bounds_xy and entity.footprint_area_m2==pytest.approx(.5625)
    assert entity.provenance=="m7_nav_node"
    negative=NavigationBlockGraph(load_config("configs/reference_code_mp3d.yaml").navigation).update(
        make_grid((15,15),2,origin=(-1.5,2.25))).nodes[0]
    neg_entity=room_entity_from_nav_node(negative)
    assert negative.block_id==(-2,3)
    assert negative.bounds_xy==(-1.5,2.25,-.75,3.0)
    assert neg_entity.center_xy==negative.center_xy==(-1.125,2.625)
    assert neg_entity.footprint_area_m2==pytest.approx(.5625)

def test_real_m7_nav_footprint_overlap_selects_candidate_region():
    node=NavigationBlockGraph(load_config("configs/reference_code_mp3d.yaml").navigation).update(make_grid((15,15),1)).nodes[0]
    entity=room_entity_from_nav_node(node)
    small=region(1,(0.,0.,.3,.75)); larger=region(2,(.15,0.,.9,.75))
    assignment=RegionMembershipResolver().resolve((small,larger),(entity,))[0]
    assert assignment.region_id==2 and assignment.plausible_region_ids==(1,2)

def test_two_real_m7_nav_nodes_and_bridge_produce_m8_room_adjacency():
    config=load_config("configs/reference_code_mp3d.yaml").navigation
    snapshot=NavigationBlockGraph(config).update(make_grid((15,30),4))
    assert {n.block_id for n in snapshot.nodes}=={(0,0),(1,0)}
    assert any(e.source_block==(0,0) and e.target_block==(1,0) for e in snapshot.edges)
    entities=[room_entity_from_nav_node(n) for n in snapshot.nodes]
    from standalone_sg.navigation import NavEdge
    regions=(region(11,(0,0,.75,.75),adjacent=(12,)),region(12,(.75,0,1.5,.75),adjacent=(11,)))
    entities.extend((RoomEntity("agent-left","agent",(.2,.2),provenance="synthetic_m7_m8_fixture"),
                     RoomEntity("object-right","object",(1.3,.2),provenance="synthetic_m7_m8_fixture")))
    result=RoomAnchoringManager().update(regions,(),entities,revision=4,nav_edges=snapshot.edges)
    assert len(result.rooms)==2 and len(result.adjacency)==1
    assert result.adjacency[0].region_pair==(11,12)
    nav_members={member for room in result.rooms for member in room.member_ids if member.startswith("nav:")}
    assert nav_members=={n.node_id for n in snapshot.nodes}

@pytest.mark.parametrize("bounds",[(0.,0.,0.,1.),(0.,0.,1.,0.),(2.,0.,1.,1.),(0.,2.,1.,1.),(0.,0.,float("nan"),1.),(0.,"0",1.,1.)])
def test_room_entity_rejects_degenerate_nonfinite_or_wrong_type_bounds(bounds):
    with pytest.raises(ContractError):RoomEntity("nav","navigation",(.5,.5),bounds)
