#!/usr/bin/env python3
"""Run a deterministic local native M2-M10 smoke with canonical point clouds."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import time

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT/"src"))

from standalone_sg.config import load_config
from standalone_sg.contracts import Detection3D, DetectionBatch, PointCloud
from standalone_sg.mapping import NativePointCloudMappingStage
from standalone_sg.navigation import NavigationBlockGraph
from standalone_sg.pipeline import DudeM5RegionStage, FramePacket, SceneGraphBuilder
from standalone_sg.regions.dude_client import DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor
from standalone_sg.resource_binding import resolve_external_file
from standalone_sg.serialization import (load_delta_log, load_snapshot, query_nodes,
    replay_deltas, trajectory_rows, write_delta_log, write_snapshot,
    write_trajectory_csv, write_trajectory_json)
from standalone_sg.visualization import render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map


M2_M3_BINARY=resolve_external_file("native_m2_m3", required=True, executable=True)
DUDE_BINARY=resolve_external_file("native_dude", required=True, executable=True)


def _rss_kib(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return 0
    return 0


def _cloud(radius_m: float, timestamp_s: float, scan_id: str) -> PointCloud:
    xyz=np.asarray(
        [(index*0.1, lateral*0.1, height) for index in range(1, int(round(radius_m*10))
         ) for lateral in range(1, int(round(radius_m*10))) for height in (0.35,0.45,0.55)],
        dtype=np.float32,
    )
    return PointCloud(
        xyz_W=xyz,
        sensor_origin_W=np.asarray((0.05,0.05,0.05),dtype=np.float64),
        frame_id="world",
        timestamp=timestamp_s,
        has_hit=np.zeros(len(xyz),dtype=bool),
        scan_id=scan_id,
    )


def _pids(mapping, region_stage) -> tuple[int, ...]:
    mapping_pids=mapping.child_pids()
    proc=getattr(region_stage.worker,"_proc",None)
    region_pid=() if proc is None or proc.pid is None else (proc.pid,)
    return mapping_pids+region_pid


def run_smoke(output: Path, *, m2_m3_binary: Path=M2_M3_BINARY, dude_binary: Path=DUDE_BINARY) -> dict:
    if not m2_m3_binary.is_file():
        raise RuntimeError(f"native M2/M3 binary is missing: {m2_m3_binary}")
    if not dude_binary.is_file():
        raise RuntimeError(f"native DuDe binary is missing: {dude_binary}")
    output.mkdir(parents=True,exist_ok=True)
    navigation=NavigationBlockGraph(load_config(ROOT/"configs/reference_code_mp3d.yaml").navigation)
    mapping=NativePointCloudMappingStage(m2_m3_binary,resolution_m=0.05,timeout_s=15.0,default_max_range_m=1.0)
    region_stage=DudeM5RegionStage(DuDeWorkerClient(dude_binary),RegionUpdateProcessor())
    builder=SceneGraphBuilder(mapping_stage=mapping,region_stage=region_stage,navigation=navigation,config_hash="m11-native-pointcloud-smoke-v1")
    initial=builder.snapshot()
    deltas=[]
    frames=[]
    peak_child_rss=0
    fixture_generation_start=time.perf_counter()
    radii=(1.0,1.3)
    clouds=tuple(_cloud(radius,float(index),f"scan-{index}") for index,radius in enumerate(radii))
    fixture_generation_elapsed=(time.perf_counter()-fixture_generation_start)*1000.0
    update_flush_close_start=time.perf_counter()
    try:
        for frame_index,(radius,cloud) in enumerate(zip(radii,clouds)):
            frame_id=f"native-frame-{frame_index:03d}"
            detections=DetectionBatch(
                provider_id="cached-smoke-detections-v1",
                frame_id=frame_id,
                timestamp_s=float(frame_index),
                detections=(() if frame_index else (Detection3D(
                    "cached-chair-001",0.0,(0.35,0.35,0.45),"cached_world_provider",
                    class_name="chair",confidence=0.8,
                    provider_metadata={"source":"cached_smoke_fixture","gt_instance_id_used":False},
                ),)),
                provenance={"data_kind":"cached_synthetic_detection","source":"local_fixture"},
            )
            packet=FramePacket(
                scene_id="native-smoke-scene",sequence_id="native-smoke-sequence",frame_id=frame_id,
                frame_index=frame_index,timestamp_s=float(frame_index),input_mode="pointcloud",
                base_pose_W=(0.5,0.5,0.0),pointcloud=cloud,
                detections=detections,
                source_metadata={
                    "capture_kind":"synthetic_canonical_pointcloud",
                    "sensor_source":"deterministic_no_return_scan",
                    "max_range_m":radius,
                    "rgbd_end_to_end":False,
                    "gt_regions_in_builder":False,
                    "cached_detection_source":"cached_smoke_fixture",
                },
            )
            frame_start=time.perf_counter()
            snapshot,delta=builder.update(packet)
            frame_elapsed=(time.perf_counter()-frame_start)*1000.0
            deltas.append(delta)
            peak_child_rss=max(peak_child_rss,sum(_rss_kib(pid) for pid in _pids(mapping,region_stage)))
            result=region_stage.last_result
            response=region_stage.last_response
            frames.append({
                "frame_id":frame_id,"frame_index":frame_index,"timestamp_s":float(frame_index),
                "runtime_ms":frame_elapsed,"graph_revision":snapshot.revision,
                "stage_status":dict(delta.diagnostics["stage_status"]),"events":list(delta.events),
                "m4_backend_status":response["status"],"m4_backend_branch":response["branch"],
                "m4_guard_accepted":bool(result.accepted),"m4_stale":bool(result.stale),
                "m4_request_revision":result.request_revision,
                "m4_accepted_observation_revision":result.accepted_observation_revision,
                "m4_region_count":len(result.current_regions),
                "m4_reason":result.reason,
                "m6_detection_status":delta.diagnostics["stage_status"]["objects"],
                "node_counts":{kind:len(query_nodes(snapshot,kind)) for kind in ("agent","object","navigation","room")},
                "native_mapping":dict(mapping.records[-1]),
            })
        flush_start=time.perf_counter()
        final,flush_delta=builder.flush(2.0)
        flush_elapsed=(time.perf_counter()-flush_start)*1000.0
        deltas.append(flush_delta)
        peak_child_rss=max(peak_child_rss,sum(_rss_kib(pid) for pid in _pids(mapping,region_stage)))
    finally:
        builder.close()
    update_flush_close_elapsed=(time.perf_counter()-update_flush_close_start)*1000.0
    write_snapshot(output/"snapshot.json",final)
    write_delta_log(output/"delta_log.json",deltas)
    write_trajectory_csv(output/"trajectory.csv",final)
    write_trajectory_json(output/"trajectory.json",final)
    render_trajectory_map(final,output/"trajectory_rooms.png")
    render_graph(final,output/"scene_graph.png")
    render_spatial_map(final,output/"spatial_map.png")
    render_relationship_graph(final,output/"relationship_graph.png")
    reloaded=load_snapshot(output/"snapshot.json")
    replayed=replay_deltas(initial,load_delta_log(output/"delta_log.json"))
    if reloaded != final or replayed != reloaded:
        raise RuntimeError("native smoke snapshot reload or delta replay mismatch")
    manifest={
        "kind":"native_pointcloud_local_smoke","input_mode":"pointcloud",
        "rgbd_end_to_end":False,"simulator_end_to_end":False,"kaggle_run":False,
        "gt_regions_in_builder":False,"regions_source":"native DuDe/M5 output",
        "fixture_generation_runtime_ms":fixture_generation_elapsed,
        "detections_source":"cached_smoke_fixture","native_backend_checkpoint":False,
        "m2_m3_binary":str(m2_m3_binary.resolve()),
        "m2_m3_binary_sha256":hashlib.sha256(m2_m3_binary.read_bytes()).hexdigest(),
        "dude_binary":str(dude_binary.resolve()),
        "dude_binary_sha256":hashlib.sha256(dude_binary.read_bytes()).hexdigest(),
        "m3_backend":"cpp_mapconverter",
        "m4_backend":"pinned_local_dude_worker","m4_license_gate":"unresolved_for_distribution",
        "m4_updates":frames,"flush_runtime_ms":flush_elapsed,
        "update_flush_close_runtime_ms":update_flush_close_elapsed,
        "sampled_child_rss_max_kib":peak_child_rss,
        "python_process_ru_maxrss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "measurement_scope":"worker/builder startup, fixture generation, update/flush/close, export/render/reload are reported separately; child RSS is sampled after frames and flush; Python ru_maxrss is process high-water mark",
        "fixture":{"ray_type":"no_return_only","has_hit_count":0,"no_return_count":sum(f["native_mapping"]["point_count"] for f in frames),
                   "base_pose_W_per_frame":[[0.5,0.5,0.0],[0.5,0.5,0.0]],"camera_motion":"stationary poses"},
        "revision":reloaded.revision,"node_count":len(reloaded.nodes),"edge_count":len(reloaded.edges),
        "agent_pose_count":len(trajectory_rows(reloaded)),
        "reloaded_snapshot_queryable":bool(query_nodes(reloaded)),"delta_replay_equal":replayed==reloaded,
        "stale_flags":dict(reloaded.stale_flags),"cloud_upload":False,
        "outputs":["snapshot.json","delta_log.json","trajectory.csv","trajectory.json","trajectory_rooms.png","scene_graph.png","spatial_map.png","relationship_graph.png"],
        "limitations":["canonical point-cloud smoke, not RGB-D capture","synthetic no-return scan and cached detection","not Habitat/Webots/Kaggle end-to-end"],
    }
    (output/"manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    return manifest


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/m11_native_smoke")
    parser.add_argument("--m2-m3-binary",type=Path,default=M2_M3_BINARY)
    parser.add_argument("--dude-binary",type=Path,default=DUDE_BINARY)
    args=parser.parse_args()
    print(json.dumps(run_smoke(args.output,m2_m3_binary=args.m2_m3_binary,dude_binary=args.dude_binary),sort_keys=True,indent=2))
