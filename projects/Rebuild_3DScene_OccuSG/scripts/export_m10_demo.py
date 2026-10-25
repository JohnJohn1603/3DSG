#!/usr/bin/env python3
"""Build deterministic synthetic M10 exports; no simulator or native restore."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
import numpy as np
from standalone_sg.config import NavigationConfig
from standalone_sg.contracts import CanonicalRegion, Detection3D, DetectionBatch, Polygon2D
from standalone_sg.mapping.projection import FreeSpaceGrid2D
from standalone_sg.navigation import NavigationBlockGraph
from standalone_sg.pipeline import FramePacket, SceneGraphBuilder
from standalone_sg.serialization import (replay_deltas, serialize_snapshot, trajectory_rows,
    write_delta_log, write_snapshot, write_trajectory_csv, write_trajectory_json)
from standalone_sg.visualization import render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map


def make_region(rid,x0,x1,t,adjacent):
    y0,y1=0.0,6.0
    return CanonicalRegion(rid,rid,Polygon2D(((x0,y0),(x1,y0),(x1,y1),(x0,y1))),
        ((x0+x1)/2,(y0+y1)/2),(x1-x0)*(y1-y0),t,int(t*10+1),tuple(adjacent),
        {"source":"synthetic_m10_fixture"})

def make_grid(revision):
    values=np.zeros((12,12),dtype=np.int8); z=np.full(values.shape,np.nan)
    return FreeSpaceGrid2D(values,.5,(0.0,0.0),z,z,revision)

def build_demo(output:Path):
    nav=NavigationBlockGraph(NavigationConfig(cell_stride_cells=3,min_free_cell_count=4,connectivity=8))
    builder=SceneGraphBuilder(navigation=nav,config_hash="m10-synthetic-fixture-v1")
    initial=builder.snapshot(); deltas=[]
    regions=(make_region(1,0,3,0.0,(2,)),make_region(2,3,6,0.0,(1,)))
    chair=Detection3D("cached-chair-1",0.0,(4.3,1.1,.45),"synthetic_cached_detection",class_name="chair",class_id="chair",confidence=.91,provider_metadata={"fixture":"synthetic"})
    frames=(
      FramePacket("synthetic-floor","doorway-demo","frame-000",0,0.0,"grid_regions",base_pose_W=(.8,.8,0.0),grid=make_grid(1),regions=regions,
        detections=DetectionBatch("synthetic-cache","frame-000",0.0,(chair,),{"data_kind":"synthetic_cached"}),source_metadata={"dataset":"synthetic_m10_demo","not_simulator":True}),
      FramePacket("synthetic-floor","doorway-demo","frame-001",1,1.0,"grid_regions",base_pose_W=(1.8,1.0,0.0),grid=make_grid(2),regions=(make_region(1,0,3,1.0,(2,)),make_region(2,3,6,1.0,(1,))),
        detections=DetectionBatch("synthetic-cache","frame-001",1.0,(),{"data_kind":"synthetic_cached"}),source_metadata={"dataset":"synthetic_m10_demo","not_simulator":True}),
      FramePacket("synthetic-floor","doorway-demo","frame-002",2,2.0,"grid_regions",base_pose_W=(4.8,1.0,0.0),grid=make_grid(3),regions=(),regions_stale=True,
        source_metadata={"dataset":"synthetic_m10_demo","not_simulator":True}),
    )
    for frame in frames:
        _,delta=builder.update(frame);deltas.append(delta)
    final,flush_delta=builder.flush(3.0);deltas.append(flush_delta)
    replayed=replay_deltas(initial,deltas)
    if replayed!=final: raise RuntimeError("delta replay does not match final snapshot")
    output.mkdir(parents=True,exist_ok=True)
    write_snapshot(output/"snapshot.json",final);write_delta_log(output/"delta_log.json",deltas)
    write_trajectory_csv(output/"trajectory.csv",final);write_trajectory_json(output/"trajectory.json",final)
    render_trajectory_map(final,output/"trajectory_rooms.png")
    render_graph(final,output/"scene_graph.png")
    render_spatial_map(final,output/"spatial_map.png")
    render_relationship_graph(final,output/"relationship_graph.png")
    manifest={"kind":"synthetic_cached_fixture","simulator_end_to_end":False,"scene_id":final.scene_id,
      "sequence_id":final.sequence_id,"revision":final.revision,"node_count":len(final.nodes),"edge_count":len(final.edges),
      "agent_pose_count":len(trajectory_rows(final)),"stale_flags":dict(final.stale_flags),
      "delta_replay_equal":replayed==final,"canonical_graph_hash":builder.canonical_hash(),
      "canonical_event_hash":builder.canonical_event_hash(),"native_backend_checkpoint":False,
      "outputs":["snapshot.json","delta_log.json","trajectory.csv","trajectory.json","trajectory_rooms.png","scene_graph.png","spatial_map.png","relationship_graph.png"]}
    (output/"manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    builder.close()
    return manifest

if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output",type=Path,default=Path("artifacts/m10_demo"));args=parser.parse_args()
    print(json.dumps(build_demo(args.output),sort_keys=True,indent=2))
