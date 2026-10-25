#!/usr/bin/env python3
"""Deterministic synthetic depth-and-pose native M2-M10 smoke; no RGB is fabricated."""
from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.config import load_config
from standalone_sg.contracts import CameraIntrinsics, Detection3D, DetectionBatch, RGBDFrame, Transform
from standalone_sg.geometry import decode_depth_to_meters
from standalone_sg.mapping import NativeRGBDMappingStage
from standalone_sg.navigation import NavigationBlockGraph
from standalone_sg.pipeline import DudeM5RegionStage, FramePacket, SceneGraphBuilder
from standalone_sg.regions.dude_client import DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor
from standalone_sg.resource_binding import resolve_external_file
from standalone_sg.serialization import (
    load_delta_log, load_snapshot, query_nodes, replay_deltas, trajectory_rows,
    write_delta_log, write_snapshot, write_trajectory_csv, write_trajectory_json,
)
from standalone_sg.visualization import render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map


M2_M3_BINARY = resolve_external_file("native_m2_m3", required=True, executable=True)
DUDE_BINARY = resolve_external_file("native_dude", required=True, executable=True)
WIDTH, HEIGHT = 64, 48
INTRINSICS = CameraIntrinsics(WIDTH, HEIGHT, 43.0, 43.0, (WIDTH - 1) / 2, (HEIGHT - 1) / 2,
                              calibration_id="synthetic-room-pinhole-v1")


def _camera_transform(base_xyz: tuple[float, float, float], yaw: float, timestamp: float) -> Transform:
    # camera optical +z points along robot +x; optical +y points down.
    camera_to_base = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
    cy, sy = math.cos(yaw), math.sin(yaw)
    base_yaw = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    rotation = base_yaw @ camera_to_base
    sensor_mount_base = np.array([0.10, 0.0, 1.20])
    origin = np.asarray(base_xyz, dtype=np.float64) + base_yaw @ sensor_mount_base
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = origin
    return Transform(matrix, "world", "camera_optical", timestamp, "ground_truth")


def _box_near_t(origin: np.ndarray, direction: np.ndarray, low: np.ndarray, high: np.ndarray) -> float:
    inv = np.divide(1.0, direction, out=np.full(3, np.inf), where=np.abs(direction) > 1e-12)
    t0 = (low - origin) * inv
    t1 = (high - origin) * inv
    near = float(np.max(np.minimum(t0, t1)))
    far = float(np.min(np.maximum(t0, t1)))
    if far < max(near, 1e-5):
        return math.inf
    return near if near > 1e-5 else far


def _render_depth_m(T_W_Copt: Transform) -> tuple[np.ndarray, np.ndarray]:
    """Ray-cast axial depth against a fixed room, floor and one box obstacle."""
    u, v = np.meshgrid(np.arange(WIDTH), np.arange(HEIGHT), indexing="xy")
    rays_camera = np.stack(
        ((u - INTRINSICS.cx) / INTRINSICS.fx,
         (v - INTRINSICS.cy) / INTRINSICS.fy,
         np.ones_like(u)), axis=-1,
    ).reshape(-1, 3)
    rays_world = rays_camera @ T_W_Copt.matrix[:3, :3].T
    origin = T_W_Copt.translation
    depth = np.full(len(rays_world), np.inf, dtype=np.float64)

    # Room shell: x=[0,4.4], y=[-2,2], z=[0,2.4]. For each plane,
    # retain the intersection only when it lies inside the corresponding face.
    for axis, value, other_axes, ranges in (
        (0, 0.0, (1, 2), ((-2.0, 2.0), (0.0, 2.4))),
        (0, 4.4, (1, 2), ((-2.0, 2.0), (0.0, 2.4))),
        (1, -2.0, (0, 2), ((0.0, 4.4), (0.0, 2.4))),
        (1, 2.0, (0, 2), ((0.0, 4.4), (0.0, 2.4))),
        (2, 0.0, (0, 1), ((0.0, 4.4), (-2.0, 2.0))),
        (2, 2.4, (0, 1), ((0.0, 4.4), (-2.0, 2.0))),
    ):
        denom = rays_world[:, axis]
        t = np.divide(value - origin[axis], denom, out=np.full(len(denom), np.inf),
                      where=np.abs(denom) > 1e-12)
        pts = origin[None, :] + t[:, None] * rays_world
        valid = t > 1e-5
        for coord, bounds in zip(other_axes, ranges):
            valid &= (pts[:, coord] >= bounds[0]) & (pts[:, coord] <= bounds[1])
        depth[valid] = np.minimum(depth[valid], t[valid])

    # A table-height rectangular obstacle is represented by real hit surfaces.
    obstacle_low = np.array([2.05, -0.42, 0.0])
    obstacle_high = np.array([2.65, 0.42, 1.05])
    for index, ray in enumerate(rays_world):
        t = _box_near_t(origin, ray, obstacle_low, obstacle_high)
        if t < depth[index]:
            depth[index] = t

    depth = depth.reshape(HEIGHT, WIDTH).astype(np.float32)
    hit_mask = np.isfinite(depth) & (depth > 0.1) & (depth <= 5.5)
    depth[~hit_mask] = np.nan
    # Deterministic invalid samples exercise both NaN and the explicit mask.
    valid_mask = hit_mask.copy()
    valid_mask.reshape(-1)[::97] = False
    depth.reshape(-1)[::131] = np.nan
    return depth, valid_mask


def _frame_packet(frame_index: int) -> tuple[FramePacket, dict]:
    timestamp = float(frame_index)
    base_pose = ((0.55, 0.0, 0.0), (1.10, 0.18, 0.0))[frame_index]
    yaw = (0.0, 0.14)[frame_index]
    sensor_pose = _camera_transform(base_pose, yaw, timestamp)
    depth_m, valid_mask = _render_depth_m(sensor_pose)
    # Source fixture uses uint16 millimetres. Decode via the shared M1 helper,
    # then RGBDFrame contains canonical metric axial depth. RGB is intentionally
    # absent; this fixture makes no claim to contain a captured/rendered color image.
    raw_mm = np.where(np.isfinite(depth_m), np.rint(depth_m * 1000.0), 0).astype(np.uint16)
    decoded_depth_m = decode_depth_to_meters(raw_mm, meters_per_unit=0.001)
    decoded_depth_m[~np.isfinite(depth_m)] = np.nan
    frame_id = f"synthetic-rgbd-{frame_index:03d}"
    rgbd = RGBDFrame(decoded_depth_m, INTRINSICS, sensor_pose, timestamp,
                     rgb=None, valid_mask=valid_mask, frame_id=frame_id)
    detections = DetectionBatch(
        provider_id="cached-rgbd-smoke-detections-v1", frame_id=frame_id, timestamp_s=timestamp,
        detections=(() if frame_index else (Detection3D(
            "cached-prop-001", 0.0, (2.3, 0.0, 0.55), "cached_rgbd_fixture",
            class_name="box", confidence=0.7,
            provider_metadata={"source": "cached_smoke_fixture", "gt_instance_id_used": False},
        ),)),
        provenance={"data_kind": "cached_synthetic_detection", "source": "local_rgbd_smoke"},
    )
    packet = FramePacket(
        scene_id="synthetic-rgbd-room", sequence_id="moving-camera-sequence",
        frame_id=frame_id, frame_index=frame_index, timestamp_s=timestamp,
        input_mode="rgbd", base_pose_W=base_pose, rgbd=rgbd, detections=detections,
        source_metadata={
            "capture_kind": "synthetic_depth_raycast_fixture",
            "geometry": "fixed room shell + floor + box obstacle; deterministic analytic ray intersections",
            "raw_depth_storage": "uint16 millimetres decoded by M1 decode_depth_to_meters",
            "depth_units": "m", "depth_scale_to_m": 1.0, "depth_representation": "axial",
            "valid_mask_source": "synthetic ray-hit validity with deterministic invalid-pixel omissions",
            "sensor_pose_source": "explicit T_W_Copt ground_truth fixture with camera mount offset",
            "sensor_extrinsics": "camera optical mount translated 0.10m forward and 1.20m above base",
            "max_range_m": 5.5, "min_depth_m": 0.1, "max_depth_m": 5.5,
            "backprojection_stride": 1,
            "gt_regions_in_builder": False, "synthetic_depth_fixture": True,
            "rgb_image_present": False,
            "cached_detection_source": "cached synthetic detection; no GT instance IDs used",
        },
    )
    diagnostic = {
        "base_pose_W": list(base_pose), "camera_yaw_rad": yaw,
        "sensor_origin_W": sensor_pose.translation.tolist(),
        "valid_mask_pixels": int(valid_mask.sum()),
        "finite_depth_pixels": int(np.isfinite(decoded_depth_m).sum()),
        "raw_invalid_nan_pixels": int(np.isnan(decoded_depth_m).sum()),
        "depth_hit_min_m": float(np.nanmin(decoded_depth_m)),
        "depth_hit_max_m": float(np.nanmax(decoded_depth_m)),
    }
    return packet, diagnostic


def _rss_kib(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return 0
    return 0


def _child_pids(mapping, region_stage) -> tuple[int, ...]:
    pids = mapping.child_pids()
    proc = getattr(region_stage.worker, "_proc", None)
    return pids + (() if proc is None or proc.pid is None else (proc.pid,))


def run_smoke(output: Path, *, m2_m3_binary: Path = M2_M3_BINARY,
              dude_binary: Path = DUDE_BINARY) -> dict:
    for binary in (m2_m3_binary, dude_binary):
        if not binary.is_file():
            raise RuntimeError(f"required native worker is missing: {binary}")
    output.mkdir(parents=True, exist_ok=True)
    ready_start = time.perf_counter()
    navigation = NavigationBlockGraph(load_config(ROOT / "configs/reference_code_mp3d.yaml").navigation)
    mapping = NativeRGBDMappingStage(m2_m3_binary, resolution_m=0.05, timeout_s=20.0,
                                     default_max_range_m=5.5)
    region_stage = DudeM5RegionStage(DuDeWorkerClient(dude_binary), RegionUpdateProcessor())
    builder = SceneGraphBuilder(mapping_stage=mapping, region_stage=region_stage,
                                navigation=navigation, config_hash="m11-rgbd-synthetic-smoke-v1")
    startup_ms = (time.perf_counter() - ready_start) * 1000.0
    initial = builder.snapshot()
    deltas, frame_diagnostics, updates = [], [], []
    sampled_child_rss_max = 0
    update_flush_close_start = time.perf_counter()
    fixture_generation_ms = 0.0
    try:
        for frame_index in range(2):
            generation_start = time.perf_counter()
            packet, input_diagnostic = _frame_packet(frame_index)
            generation_ms = (time.perf_counter() - generation_start) * 1000.0
            fixture_generation_ms += generation_ms
            update_start = time.perf_counter()
            snapshot, delta = builder.update(packet)
            elapsed_ms = (time.perf_counter() - update_start) * 1000.0
            deltas.append(delta)
            sampled_rss = sum(_rss_kib(pid) for pid in _child_pids(mapping, region_stage))
            sampled_child_rss_max = max(sampled_child_rss_max, sampled_rss)
            response, result = region_stage.last_response, region_stage.last_result
            record = dict(mapping.records[-1])
            record.update({
                "m2_occupied_key_count": record["m2_occupied_key_count"],
                "m2_free_key_count": record["m2_free_key_count"],
                "m3_native_free_cells": record["m3_free_cell_count"],
                "m3_native_occupied_cells": record["m3_occupied_cell_count"],
                "sampled_child_rss_kib": sampled_rss,
            })
            frame_diagnostics.append({
                **input_diagnostic,
                "frame_id": packet.frame_id, "frame_index": frame_index,
                "timestamp_s": packet.timestamp_s, "fixture_generation_ms": generation_ms, "update_runtime_ms": elapsed_ms,
                "graph_revision": snapshot.revision,
                "m4_backend_status": response["status"], "m4_backend_branch": response["branch"],
                "m4_guard_accepted": bool(result.accepted), "m4_stale": bool(result.stale),
                "m4_request_revision": result.request_revision,
                "m4_accepted_observation_revision": result.accepted_observation_revision,
                "m4_region_count": len(result.current_regions),
                "m4_reason": result.reason,
                "m6_detection_status": delta.diagnostics["stage_status"]["objects"],
                "node_counts": {kind: len(query_nodes(snapshot, kind))
                                for kind in ("agent", "object", "navigation", "room")},
                "mapping": record,
                "stage_status": dict(delta.diagnostics["stage_status"]),
                "events": list(delta.events),
            })
            updates.append({
                "frame_id": packet.frame_id, "frame_index": frame_index,
                "graph_revision": snapshot.revision, "m4_backend_status": response["status"],
                "m4_backend_branch": response["branch"], "m4_guard_accepted": bool(result.accepted),
                "m4_stale": bool(result.stale), "m4_region_count": len(result.current_regions),
                "native_mapping": record,
            })
        flush_start = time.perf_counter()
        final, flush_delta = builder.flush(2.0)
        flush_runtime_ms = (time.perf_counter() - flush_start) * 1000.0
        deltas.append(flush_delta)
        sampled_child_rss_max = max(
            sampled_child_rss_max, sum(_rss_kib(pid) for pid in _child_pids(mapping, region_stage))
        )
    finally:
        builder.close()
    update_flush_close_ms = (time.perf_counter() - update_flush_close_start) * 1000.0 - fixture_generation_ms

    export_start = time.perf_counter()
    write_snapshot(output / "snapshot.json", final)
    write_delta_log(output / "delta_log.json", deltas)
    write_trajectory_csv(output / "trajectory.csv", final)
    write_trajectory_json(output / "trajectory.json", final)
    render_trajectory_map(final, output / "trajectory_rooms.png")
    render_graph(final, output / "scene_graph.png")
    render_spatial_map(final, output / "spatial_map.png")
    render_relationship_graph(final, output / "relationship_graph.png")
    reloaded = load_snapshot(output / "snapshot.json")
    replayed = replay_deltas(initial, load_delta_log(output / "delta_log.json"))
    if reloaded != final or replayed != reloaded:
        raise RuntimeError("RGB-D smoke snapshot reload or delta replay mismatch")
    trajectory = trajectory_rows(reloaded)
    if len(trajectory) != 2 or math.dist(
        (trajectory[0]["x_m"], trajectory[0]["y_m"]),
        (trajectory[1]["x_m"], trajectory[1]["y_m"]),
    ) <= 0.1:
        raise RuntimeError("synthetic camera sequence did not create a moving agent trajectory")
    node_ids = {node.node_id for node in reloaded.nodes}
    if any(edge.source_id not in node_ids or edge.target_id not in node_ids
           or edge.source_id == edge.target_id for edge in reloaded.edges):
        raise RuntimeError("graph contains dangling or self edges")
    export_runtime_ms = (time.perf_counter() - export_start) * 1000.0
    manifest = {
        "kind": "synthetic_depth_pose_native_local_smoke",
        "input_mode": "rgbd_frame_depth_channel",
        "fixture": {
            "kind": "synthetic_analytic_depth_raycast",
            "geometry": "rectangular room shell, floor, ceiling, one box obstacle",
            "rgb_image_present": False,
            "camera_moves_and_rotates": True,
            "camera_views": [
                {"base_pose_W": [0.55, 0.0, 0.0], "yaw_rad": 0.0},
                {"base_pose_W": [1.10, 0.18, 0.0], "yaw_rad": 0.14},
            ],
            "raw_depth": "uint16 millimetres; decoded to metric axial depth using M1 decode_depth_to_meters",
            "invalid_depth_policy": "NaN and valid_mask-false pixels are omitted by M1; only resulting measured endpoints are submitted as hits",
        },
        "pipeline": ["M1 backproject_depth", "native M2 OctoMap", "native M3 C++ MapConverter",
                     "pinned local DuDe/M5", "M6", "M7", "M8", "M9", "M10"],
        "m3_backend": "cpp_mapconverter",
        "m4_backend": "pinned_local_dude_worker",
        "m4_license_gate": "unresolved_for_distribution",
        "gt_regions_in_builder": False,
        "regions_source": "native DuDe/M5 output",
        "detections_source": "cached synthetic detection; GT instance IDs unused",
        "depth_units": "m",
        "depth_representation": "axial",
        "depth_scale_raw_mm_to_m": 0.001,
        "frame_diagnostics": frame_diagnostics,
        "m4_updates": updates,
        "region_count_final": len(query_nodes(reloaded, "room")),
        "region_observation_count_final": updates[-1]["m4_region_count"],
        "stale_flags": dict(reloaded.stale_flags),
        "revision": reloaded.revision,
        "node_count": len(reloaded.nodes),
        "edge_count": len(reloaded.edges),
        "agent_pose_count": len(trajectory),
        "trajectory_displacement_m": math.dist(
            (trajectory[0]["x_m"], trajectory[0]["y_m"]),
            (trajectory[-1]["x_m"], trajectory[-1]["y_m"]),
        ),
        "fixture_generation_runtime_ms": fixture_generation_ms,
        "delta_replay_equal": replayed == reloaded,
        "reloaded_snapshot_queryable": bool(query_nodes(reloaded)),
        "dangling_or_self_edges": False,
        "startup_runtime_ms": startup_ms,
        "update_flush_close_runtime_ms": update_flush_close_ms,
        "flush_runtime_ms": flush_runtime_ms,
        "export_render_reload_runtime_ms": export_runtime_ms,
        "sampled_child_rss_max_kib": sampled_child_rss_max,
        "python_process_ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "measurement_scope": "startup, synthetic fixture generation, builder update/flush/close, export/render/reload and process high-water memory are reported separately; child RSS is max of per-frame/flush samples",
        "native_backend_checkpoint": False,
        "simulator_capture": False,
        "kaggle_benchmark": False,
        "cloud_upload": False,
        "outputs": ["snapshot.json", "delta_log.json", "trajectory.csv", "trajectory.json",
                    "trajectory_rooms.png", "scene_graph.png", "spatial_map.png", "relationship_graph.png",
                    "diagnostics.json"],
        "limitations": [
            "synthetic depth geometry and ground-truth poses, not an RGB/color capture or simulator capture",
            "one deterministic room scene and two moving camera views; segmentation quality and real-scene performance are not evaluated",
            "M4 rights/cloud distribution gate unresolved; this local run does not grant or infer distribution permission",
        ],
    }
    (output / "diagnostics.json").write_text(
        json.dumps(frame_diagnostics, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/m11_rgbd_synthetic_smoke")
    parser.add_argument("--m2-m3-binary", type=Path, default=M2_M3_BINARY)
    parser.add_argument("--dude-binary", type=Path, default=DUDE_BINARY)
    args = parser.parse_args()
    print(json.dumps(run_smoke(args.output, m2_m3_binary=args.m2_m3_binary,
                               dude_binary=args.dude_binary), sort_keys=True, indent=2))
