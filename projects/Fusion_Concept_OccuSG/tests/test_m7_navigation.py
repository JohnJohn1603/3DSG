from __future__ import annotations

import numpy as np
import pytest

from standalone_sg.contracts import ContractError
from standalone_sg.mapping.projection import FreeSpaceGrid2D, project_leaves
from standalone_sg.navigation import NavigationBlockGraph


def grid(values, revision, origin=(0.0,0.0), res=1.0):
    a=np.asarray(values,dtype=np.int8)
    nan=np.full(a.shape,np.nan)
    return FreeSpaceGrid2D(a,res,origin,nan,nan,revision)


def test_threshold_exactly_free_24_25_26():
    for count, expected in ((24,False),(25,True),(26,True)):
        data=np.full((15,15),-1,np.int8); data.flat[:count]=0
        snap=NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8).update(grid(data,1))
        assert bool(snap.nodes)==expected
        if expected:
            assert snap.nodes[0].free_cell_count==count
            assert snap.nodes[0].meets_minimum_free_cells


def test_world_aligned_negative_keys_cropped_origin_and_expansion_keep_ids():
    manager=NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8)
    a=np.full((30,30),-1,np.int8); a[:15,:15]=0; a[15:,15:]=0
    first=manager.update(grid(a,1,origin=(-15.0,-15.0)))
    ids={n.block_id:n.node_id for n in first.nodes}
    assert (-1,-1) in ids and (0,0) in ids
    expanded=np.full((45,45),-1,np.int8); expanded[:30,:30]=a; expanded[30:,30:]=0
    second=manager.update(grid(expanded,2,origin=(-15.0,-15.0)))
    assert not second.full_rescan
    assert all(next(n for n in second.nodes if n.block_id==key).node_id==value for key,value in ids.items())
    # A cropped grid shift is a source full rescan; overlapping world keys retain IDs.
    shifted=np.full((45,45),-1,np.int8); shifted[:30,:30]=0
    third=manager.update(grid(shifted,3,origin=(-7.5,-7.5)))
    assert third.full_rescan
    assert all(n.node_id==ids[n.block_id] for n in third.nodes if n.block_id in ids)


def test_diagonal_bridge_is_structural_and_bidirectional_but_not_footprint_claim():
    data=np.full((30,30),-1,np.int8); data[:15,:15]=0; data[15:,15:]=0
    snap=NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8).update(grid(data,1))
    assert {(e.source_block,e.target_block) for e in snap.edges}=={((0,0),(1,1)),((1,1),(0,0))}
    assert all(e.evidence=="structural_free_cell_bridge" and not e.robot_footprint_passability for e in snap.edges)
    assert snap.connectivity_scope=="structural_connectivity_not_robot_passability"


def test_blocked_bridge_rejects_neighbor_edge():
    data=np.full((15,30),-1,np.int8); data[:,:14]=0; data[:,16:]=0
    snap=NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8).update(grid(data,1))
    assert len(snap.nodes)==2 and not snap.edges


def test_retained_node_below_threshold_on_same_origin_but_pruned_on_origin_shift():
    manager=NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8); data=np.full((15,15),0,np.int8)
    first=manager.update(grid(data,1)); node_id=first.nodes[0].node_id
    low=np.full((15,15),-1,np.int8); low.flat[:24]=0
    retained=manager.update(grid(low,2))
    assert retained.nodes[0].node_id==node_id
    assert retained.nodes[0].free_cell_count==24 and not retained.nodes[0].meets_minimum_free_cells
    shifted=manager.update(grid(low,3,origin=(1.0,0.0)))
    assert not shifted.nodes and shifted.removed_node_ids==(node_id,)


def test_rejects_non_world_or_non_structural_grids():
    data=np.zeros((15,15),np.int8); nan=np.full(data.shape,np.nan)
    with pytest.raises(ContractError,match="world"):
        NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8).update(FreeSpaceGrid2D(data,1.0,(0,0),nan,nan,1,frame_id="map"))
    with pytest.raises(ContractError,match="structural"):
        NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8).update(FreeSpaceGrid2D(data,1.0,(0,0),nan,nan,1,kind="other"))


def test_m3_debug_projection_output_drives_block_graph_integration_fixture():
    # Synthetic leaves are projected through the M3 API; the result is explicitly
    # the debug-port fixture, not native C++ M3 or whole-pipeline parity.
    leaves=[{"center":(x+0.5,y+0.5,0.15),"size":0.3,"occupied":False}
            for x in range(29) for y in range(29)]
    projected=project_leaves(leaves,resolution_m=1.0,min_free_height_m=0.3,
                             bounds_xy=(0.0,31.0,0.0,31.0),revision=41)
    assert projected.backend=="python_port_debug" and np.count_nonzero(projected.values==0)>=25
    result=NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25,connectivity=8).update(projected)
    assert result.revision==41 and result.nodes
    assert all(n.revision==41 for n in result.nodes)


def test_resolution_change_source_tolerance_controls_retention_and_pruning():
    # Source uses strictly greater than 1e-4; at/below it is not a full rescan.
    manager=NavigationBlockGraph(cell_stride_cells=15,min_free_cell_count=25)
    full=np.zeros((15,15),np.int8)
    first=manager.update(grid(full,1,res=0.05)); old_id=first.nodes[0].node_id
    low=np.full((15,15),-1,np.int8); low.flat[:24]=0
    at=manager.update(grid(low,2,res=0.05009))
    assert not at.full_rescan and at.nodes[0].node_id==old_id
    assert at.nodes[0].free_cell_count==24 and at.removed_node_ids==()
    over=manager.update(grid(low,3,res=0.0502001))
    assert over.full_rescan and over.nodes==()
    assert over.removed_node_ids==(old_id,)


def test_navigation_config_profiles_and_source_tolerance():
    from standalone_sg.config import load_config
    runtime=load_config("configs/reference_code.yaml").navigation
    benchmark=load_config("configs/reference_code_mp3d.yaml").navigation
    assert (runtime.cell_stride_cells,runtime.min_free_cell_count,runtime.connectivity)==(10,50,8)
    assert (benchmark.cell_stride_cells,benchmark.min_free_cell_count,benchmark.connectivity)==(15,25,8)
    assert benchmark.resolution_change_tolerance_m==pytest.approx(0.0001)


def test_native_m2_integrate_m3_project_then_m7_smoke():
    import pytest
    from standalone_sg.mapping import OctoMapWorker, ScanPoint
    from standalone_sg.resource_binding import resolve_external_file
    binary=resolve_external_file("native_m2_m3")
    if not binary.exists(): pytest.skip("native M2/M3 worker is not built in this workspace")
    points=[ScanPoint(x,y,z,has_hit=False) for x in (0.05,0.15,0.25,0.35,0.45)
            for y in (0.05,0.15,0.25,0.35,0.45) for z in (0.35,0.45,0.55)]
    with OctoMapWorker(binary,resolution_m=0.05,timeout_s=15.0) as worker:
        integrated=worker.integrate(points,origin=(0.05,0.05,0.05),max_range_m=0.7)
        projected=worker.project(mode="full_refresh")
    from standalone_sg.config import load_config
    graph=NavigationBlockGraph(load_config("configs/reference_code_mp3d.yaml").navigation)
    result=graph.update(projected)
    assert integrated["revision"]==projected.revision==result.revision
    assert projected.backend=="cpp_mapconverter" and projected.frame_id=="world"
    assert projected.resolution_m==pytest.approx(0.05)
    assert len(projected.origin_xy)==2 and all(np.isfinite(projected.origin_xy))
    assert result.nodes, "native M3 structural grid should exercise at least one M7 NavNode"
    assert all(n.revision==projected.revision for n in result.nodes)
    ids={n.block_id for n in result.nodes}
    assert all(e.source_block in ids and e.target_block in ids for e in result.edges)
    assert all(not e.robot_footprint_passability for e in result.edges)
