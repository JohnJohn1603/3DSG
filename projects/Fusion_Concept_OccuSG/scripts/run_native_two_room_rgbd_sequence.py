#!/usr/bin/env python3
"""Scripted two-room synthetic RGB-D route through native M1-M10, not a simulator."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from standalone_sg.config import load_config
from standalone_sg.contracts import CameraIntrinsics, Detection3D, DetectionBatch, RGBDFrame
from standalone_sg.geometry import decode_depth_to_meters
from standalone_sg.mapping import NativeRGBDMappingStage
from standalone_sg.navigation import NavigationBlockGraph
from standalone_sg.pipeline import DudeM5RegionStage, FramePacket, SceneGraphBuilder
from standalone_sg.regions.dude_client import DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor
from standalone_sg.serialization import (
    load_delta_log, load_snapshot, query_nodes, replay_deltas, trajectory_rows,
    write_delta_log, write_snapshot, write_trajectory_csv, write_trajectory_json,
)
from standalone_sg.visualization import render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map
from run_native_rgbd_smoke import (
    DUDE_BINARY, INTRINSICS, M2_M3_BINARY, WIDTH, HEIGHT,
    _box_near_t, _camera_transform, _rss_kib,
)

WORLD_X_MAX = 8.8
PARTITION_X = 4.4
DOOR_Y = (-1.1, 1.1)
ROUTE_Y = 0.9
SCENE_ID = "synthetic-two-room-doorway"
SEQUENCE_ID = "scripted-44-frame-explore-return-v1"


def scripted_route() -> list[tuple[tuple[float, float, float], float, str]]:
    route: list[tuple[tuple[float, float, float], float, str]] = []

    def line(xs, yaw, phase):
        route.extend((((float(x), ROUTE_Y, 0.0), float(yaw), phase) for x in xs))

    line((0.8, 1.0, 1.2, 1.4, 1.6, 1.8), 0.0, "observe_room_1")
    line((2.0, 2.25, 2.5, 2.8, 3.1, 3.4, 3.7, 4.0), 0.0, "approach_door")
    line((4.2, 4.65, 4.9), 0.0, "cross_door")
    line((5.2, 5.6, 6.0, 6.4, 6.8, 7.2), 0.0, "explore_room_2")
    route.extend((((7.2, ROUTE_Y, 0.0), yaw, "turn_in_room_2")
                  for yaw in (0.5, 1.1, 1.8, 2.5, math.pi)))
    line((6.8, 6.3, 5.8, 5.3, 4.8, 4.3, 3.8), math.pi, "return_through_door")
    line((3.3, 2.8, 2.3, 1.8, 1.3), math.pi, "revisit_room_1")
    route.extend((((1.3, ROUTE_Y, 0.0), yaw, "turn_to_revisit_room_1")
                  for yaw in (2.4, 1.5, 0.7, 0.0)))
    return route


def validate_route(route) -> dict:
    """Check sampled base-center path against shell, partition and boxes."""
    obstacle_boxes = (
        (np.array([2.05, -0.42, 0.0]), np.array([2.65, 0.42, 1.05])),
        (np.array([6.1, -0.25, 0.0]), np.array([6.55, 0.25, 0.9])),
    )
    radius = 0.20
    points = [np.asarray(pose, dtype=float) for pose, _yaw, _phase in route]
    samples = []
    for a, b in zip(points, points[1:]):
        steps = max(1, math.ceil(float(np.linalg.norm(b - a)) / 0.025))
        samples.extend(a + (b - a) * (i / steps) for i in range(steps + 1))
    samples.extend(points[:1])
    for p in samples:
        if not (radius <= p[0] <= WORLD_X_MAX - radius and -2 + radius <= p[1] <= 2 - radius):
            raise ValueError(f"route exits room shell with clearance: {p.tolist()}")
        for low, high in obstacle_boxes:
            closest = np.maximum(low[:2], np.minimum(p[:2], high[:2]))
            if float(np.linalg.norm(p[:2] - closest)) < radius - 1e-8:
                raise ValueError(f"route intersects scripted obstacle clearance at {p.tolist()}")
    crossings = [(a, b) for a, b in zip(points, points[1:])
                 if (a[0] - PARTITION_X) * (b[0] - PARTITION_X) < 0]
    if len(crossings) != 2 or any(not (DOOR_Y[0] + radius <= a[1] <= DOOR_Y[1] - radius)
                                  for a, _ in crossings):
        raise ValueError("route must cross the partition twice through the doorway with clearance")
    return {"base_center_samples_checked": len(samples), "obstacle_clearance_radius_m": radius,
            "partition_crossings": len(crossings), "doorway_y_bounds_m": list(DOOR_Y),
            "route_collision_check": "scripted point path with 0.20m planar clearance; no robot dynamics"}


def render_two_room_depth(T_W_Copt) -> tuple[np.ndarray, np.ndarray]:
    u, v = np.meshgrid(np.arange(WIDTH), np.arange(HEIGHT), indexing="xy")
    rays_camera = np.stack(((u - INTRINSICS.cx) / INTRINSICS.fx,
                            (v - INTRINSICS.cy) / INTRINSICS.fy,
                            np.ones_like(u)), axis=-1).reshape(-1, 3)
    rays = rays_camera @ T_W_Copt.matrix[:3, :3].T
    origin = T_W_Copt.translation
    depth = np.full(len(rays), np.inf, dtype=np.float64)
    # Outer shell, floor and ceiling.
    faces = (
        (0, 0.0, (1, 2), ((-2.0, 2.0), (0.0, 2.4))),
        (0, WORLD_X_MAX, (1, 2), ((-2.0, 2.0), (0.0, 2.4))),
        (1, -2.0, (0, 2), ((0.0, WORLD_X_MAX), (0.0, 2.4))),
        (1, 2.0, (0, 2), ((0.0, WORLD_X_MAX), (0.0, 2.4))),
        (2, 0.0, (0, 1), ((0.0, WORLD_X_MAX), (-2.0, 2.0))),
        (2, 2.4, (0, 1), ((0.0, WORLD_X_MAX), (-2.0, 2.0))),
    )
    for axis, value, others, bounds in faces:
        denom = rays[:, axis]
        t = np.divide(value - origin[axis], denom, out=np.full(len(denom), np.inf), where=np.abs(denom) > 1e-12)
        pts = origin[None, :] + t[:, None] * rays
        valid = (t > 1e-5)
        for coord, (lo, hi) in zip(others, bounds):
            valid &= (pts[:, coord] >= lo) & (pts[:, coord] <= hi)
        depth[valid] = np.minimum(depth[valid], t[valid])
    # Partition wall at x=4.4, with a real opening y∈[-1.1,1.1], z<2.1.
    t = np.divide(PARTITION_X - origin[0], rays[:, 0], out=np.full(len(rays), np.inf), where=np.abs(rays[:, 0]) > 1e-12)
    pts = origin[None, :] + t[:, None] * rays
    solid = ((pts[:, 1] < DOOR_Y[0]) | (pts[:, 1] > DOOR_Y[1]) | (pts[:, 2] >= 2.1))
    valid = (t > 1e-5) & (pts[:, 1] >= -2.0) & (pts[:, 1] <= 2.0) & (pts[:, 2] >= 0.0) & (pts[:, 2] <= 2.4) & solid
    depth[valid] = np.minimum(depth[valid], t[valid])
    # Two solid obstacles create occupied hit surfaces, away from scripted route.
    for low, high in (
        (np.array([2.05, -0.42, 0.0]), np.array([2.65, 0.42, 1.05])),
        (np.array([6.1, -0.25, 0.0]), np.array([6.55, 0.25, 0.9])),
    ):
        for i, ray in enumerate(rays):
            hit = _box_near_t(origin, ray, low, high)
            if hit < depth[i]:
                depth[i] = hit
    depth = depth.reshape(HEIGHT, WIDTH).astype(np.float32)
    valid_mask = np.isfinite(depth) & (depth > 0.1) & (depth <= 5.5)
    depth[~valid_mask] = np.nan
    valid_mask.reshape(-1)[::97] = False
    depth.reshape(-1)[::131] = np.nan
    return depth, valid_mask


def make_packet(index: int, route_item):
    (base, yaw, phase) = route_item
    timestamp = float(index)
    sensor_pose = _camera_transform(base, yaw, timestamp)
    depth_m, mask = render_two_room_depth(sensor_pose)
    raw_mm = np.where(np.isfinite(depth_m), np.rint(depth_m * 1000), 0).astype(np.uint16)
    decoded = decode_depth_to_meters(raw_mm, meters_per_unit=0.001)
    decoded[~np.isfinite(depth_m)] = np.nan
    frame_id = f"two-room-{index:03d}"
    frame = RGBDFrame(decoded, INTRINSICS, sensor_pose, timestamp, rgb=None, valid_mask=mask, frame_id=frame_id)
    # One explicitly provenance-tagged cached detection, then provider-absent frames.
    detections = None
    if index == 0:
        detections = DetectionBatch(
            provider_id="cached-two-room-smoke-detections-v1", frame_id=frame_id, timestamp_s=timestamp,
            detections=(Detection3D("cached-obstacle-prop", 0.0, (2.3, 0.0, 0.55),
                                    "cached_synthetic", class_name="box", confidence=0.7,
                                    provider_metadata={"source": "synthetic_fixture_cache", "gt_instance_id_used": False}),),
            provenance={"data_kind": "cached_synthetic_detection", "only_frame": frame_id},
        )
    packet = FramePacket(
        scene_id=SCENE_ID, sequence_id=SEQUENCE_ID, frame_id=frame_id, frame_index=index,
        timestamp_s=timestamp, input_mode="rgbd", base_pose_W=base, rgbd=frame, detections=detections,
        source_metadata={
            "capture_kind": "scripted_synthetic_depth_raycast", "route_phase": phase,
            "depth_units": "m", "depth_scale_to_m": 1.0, "depth_representation": "axial",
            "valid_mask_source": "synthetic ray hits with deterministic NaN/mask omissions",
            "sensor_pose_source": "analytic T_W_Copt derived from scripted base pose and fixed extrinsics",
            "sensor_extrinsics": "camera optical mount offset +0.10m forward, +1.20m vertical",
            "backprojection_stride": 1, "min_depth_m": 0.1, "max_depth_m": 5.5,
            "max_range_m": 5.5, "gt_regions_in_builder": False, "gt_room_ids_in_builder": False,
            "gt_instance_ids_used": False, "rgb_image_present": False,
            "cached_detection_source": "one synthetic cached detection at frame 0; provider absent afterwards",
        },
    )
    return packet, {"base_pose_W": list(base), "camera_yaw_rad": yaw,
                    "sensor_origin_W": sensor_pose.translation.tolist(), "route_phase": phase,
                    "finite_depth_pixels": int(np.isfinite(decoded).sum()),
                    "valid_mask_pixels": int(mask.sum()), "nan_depth_pixels": int(np.isnan(decoded).sum())}


def _room_state(snapshot):
    rooms = [node for node in snapshot.nodes if node.kind == "room"]
    memberships = sorted([list((edge.source_id, edge.target_id)) for edge in snapshot.edges
                          if edge.relation == "room_contains"])
    adjacency = sorted([list((edge.source_id, edge.target_id)) for edge in snapshot.edges
                        if edge.relation == "room_adjacency"])
    return ({"room_ids": sorted(node.attributes["room_id"] for node in rooms),
             "room_node_ids": sorted(node.node_id for node in rooms),
             "room_anchors": sorted([{"room_id": node.attributes["room_id"],
                                      "anchor_region_id": node.attributes.get("anchor_region_id"),
                                      "member_ids": list(node.attributes.get("member_ids", ())),
                                      "last_seen_s": node.last_seen_s,
                                      "observation_revision": node.attributes.get("observation_revision")}
                                     for node in rooms], key=lambda row: row["room_id"]),
             "membership_edges": memberships, "room_adjacency_edges": adjacency,
             "navigation_edge_count": sum(edge.relation == "navigable_path" for edge in snapshot.edges)},
            rooms)


def run_sequence(output: Path, *, m2_m3_binary=M2_M3_BINARY, dude_binary=DUDE_BINARY) -> dict:
    for binary in (m2_m3_binary, dude_binary):
        if not Path(binary).is_file():
            raise RuntimeError(f"required native worker is missing: {binary}")
    output.mkdir(parents=True, exist_ok=True)
    route = scripted_route()
    route_check = validate_route(route)
    navigation = NavigationBlockGraph(load_config(ROOT / "configs/reference_code_mp3d.yaml").navigation)
    mapping = NativeRGBDMappingStage(m2_m3_binary, resolution_m=0.05, timeout_s=20.0, default_max_range_m=5.5)
    region_stage = DudeM5RegionStage(DuDeWorkerClient(dude_binary), RegionUpdateProcessor())
    builder = SceneGraphBuilder(mapping_stage=mapping, region_stage=region_stage,
                                navigation=navigation, config_hash="m11-two-room-rgbd-sequence-v1")
    initial = builder.snapshot()
    deltas, diagnostics, milestone_snapshots = [], [], {}
    rss_max = 0
    statuses, branches = Counter(), Counter()
    previous_room_ids: set[str] | None = None
    room_jaccards = []
    phase_milestones = {"approach_door": "before_door", "explore_room_2": "after_door",
                        "turn_to_revisit_room_1": "return"}
    stage_start = time.perf_counter()
    fixture_ms = update_ms = 0.0
    try:
        for index, route_item in enumerate(route):
            gen_start = time.perf_counter()
            packet, input_diag = make_packet(index, route_item)
            gen_ms = (time.perf_counter() - gen_start) * 1000
            fixture_ms += gen_ms
            update_start = time.perf_counter()
            snapshot, delta = builder.update(packet)
            elapsed = (time.perf_counter() - update_start) * 1000
            update_ms += elapsed
            deltas.append(delta)
            sampled_rss = sum(_rss_kib(pid) for pid in mapping.child_pids() +
                              ((region_stage.worker._proc.pid,) if region_stage.worker._proc.pid else ()))
            rss_max = max(rss_max, sampled_rss)
            response, result = region_stage.last_response, region_stage.last_result
            statuses[response["status"]] += 1
            branches[response["branch"]] += 1
            graph_state, rooms = _room_state(snapshot)
            current_room_ids = set(graph_state["room_ids"])
            if previous_room_ids is not None:
                union = previous_room_ids | current_room_ids
                room_jaccards.append(len(previous_room_ids & current_room_ids) / len(union) if union else 1.0)
            previous_room_ids = current_room_ids
            curr_regions = result.current_regions if result is not None else ()
            lifecycle = {"promoted": "rooms_promoted" in delta.events,
                         "relinked": "rooms_relinked" in delta.events,
                         "pruned": "rooms_pruned" in delta.events,
                         "created_room_node_ids": sorted(n.node_id for n in delta.added_nodes if n.kind == "room"),
                         "removed_room_node_ids": sorted(nid for nid in delta.removed_node_ids if nid.startswith("room:")),
                         "updated_room_node_ids": sorted(n.node_id for n in delta.updated_nodes if n.kind == "room")}
            diagnostics.append({
                **input_diag, "frame_id": packet.frame_id, "frame_index": index,
                "timestamp_s": packet.timestamp_s, "fixture_generation_ms": gen_ms,
                "update_runtime_ms": elapsed, "graph_revision": snapshot.revision,
                "m4_status": response["status"], "m4_branch": response["branch"],
                "m4_reason": response.get("rejection_reason"),
                "m4_source_diagnostics": response.get("diagnostics", {}),
                "m5_accepted": bool(result.accepted) if result is not None else False,
                "m5_stale": bool(result.stale) if result is not None else True,
                "m5_reason": result.reason if result is not None else "backend_rejected_no_m5_update",
                "request_revision": result.request_revision if result is not None else None,
                "accepted_observation_revision": result.accepted_observation_revision if result is not None else None,
                "current_region_ids": sorted(int(r.persistent_id) for r in curr_regions if r.persistent_id is not None),
                "retained_track_ids": sorted(int(t.persistent_id) for t in result.retained_tracks) if result else [],
                "region_count": len(curr_regions), "room_count": len(rooms), **graph_state,
                "room_lifecycle": lifecycle, "events": list(delta.events),
                "stage_status": dict(delta.diagnostics["stage_status"]),
                "mapping": dict(mapping.records[-1]), "sampled_child_rss_kib": sampled_rss,
                "detections_provider": "cached_frame_0" if packet.detections is not None else "absent",
            })
            if input_diag["route_phase"] in phase_milestones:
                milestone_snapshots[phase_milestones[input_diag["route_phase"]]] = snapshot
    finally:
        final_grid = builder._last_grid
        last_m4_response = region_stage.last_response
        final, flush_delta = builder.flush(float(len(route)))
        deltas.append(flush_delta)
        rss_max = max(rss_max, sum(_rss_kib(pid) for pid in mapping.child_pids() +
                                   ((region_stage.worker._proc.pid,) if region_stage.worker._proc.pid else ())))
        builder.close()
    fresh_probe_start = time.perf_counter()
    fresh_client = DuDeWorkerClient(dude_binary)
    try:
        fresh_response, _fresh_observations = fresh_client.integrate(final_grid, timestamp_s=float(len(route) - 1))
    finally:
        fresh_client.close()
    fresh_probe_ms = (time.perf_counter() - fresh_probe_start) * 1000
    fresh_compare = {
        "comparison": "last sequential M4 output versus a new M4 worker on the final accumulated M3 grid",
        "sequential_status": last_m4_response.get("status"), "sequential_branch": last_m4_response.get("branch"),
        "fresh_status": fresh_response.get("status"), "fresh_branch": fresh_response.get("branch"),
        "sequential_region_count": len(last_m4_response.get("regions", [])),
        "fresh_region_count": len(fresh_response.get("regions", [])),
        "raw_region_geometry_equal": last_m4_response.get("regions", []) == fresh_response.get("regions", []),
        "sequential_preprocessing": last_m4_response.get("diagnostics", {}),
        "fresh_preprocessing": fresh_response.get("diagnostics", {}),
    }
    active_ms = (time.perf_counter() - stage_start) * 1000 - fixture_ms
    write_snapshot(output / "snapshot.json", final)
    write_delta_log(output / "delta_log.json", deltas)
    write_trajectory_csv(output / "trajectory.csv", final)
    write_trajectory_json(output / "trajectory.json", final)
    render_trajectory_map(final, output / "trajectory_return.png")
    render_graph(final, output / "scene_graph.png")
    render_spatial_map(final, output / "spatial_map.png")
    render_relationship_graph(final, output / "relationship_graph.png")
    for milestone, snapshot in milestone_snapshots.items():
        write_snapshot(output / f"snapshot_{milestone}.json", snapshot)
        render_trajectory_map(snapshot, output / f"trajectory_{milestone}.png")
        render_spatial_map(snapshot, output / f"spatial_map_{milestone}.png")
        render_relationship_graph(snapshot, output / f"relationship_graph_{milestone}.png")
    reloaded = load_snapshot(output / "snapshot.json")
    replayed = replay_deltas(initial, load_delta_log(output / "delta_log.json"))
    if reloaded != final or replayed != reloaded:
        raise RuntimeError("two-room snapshot reload/delta replay mismatch")
    node_ids = {node.node_id for node in reloaded.nodes}
    if any(e.source_id == e.target_id or e.source_id not in node_ids or e.target_id not in node_ids for e in reloaded.edges):
        raise RuntimeError("two-room graph has self or dangling edges")
    room_nodes = [node for node in reloaded.nodes if node.kind == "room"]
    anchors = [node.attributes.get("anchor_region_id") for node in room_nodes]
    if len(anchors) != len(set(anchors)):
        raise RuntimeError("duplicate room anchors")
    traj = trajectory_rows(reloaded)
    if len(traj) != len(route) or len({(row["x_m"], row["y_m"]) for row in traj}) < 5:
        raise RuntimeError("trajectory did not preserve scripted movement")
    frame_ids = {d["frame_id"] for d in diagnostics}
    if len(frame_ids) != len(route):
        raise RuntimeError("missing per-frame diagnostics")
    replay_hash = hashlib.sha256((output / "snapshot.json").read_bytes()).hexdigest()
    manifest = {
        "kind": "synthetic_two_room_doorway_rgbd_native_local_sequence",
        "sequence_id": SEQUENCE_ID, "frame_count": len(route),
        "fixture": {"kind": "analytic_depth_ray_cast", "room_shell_x_m": [0.0, WORLD_X_MAX],
                    "partition_x_m": PARTITION_X, "doorway_y_bounds_m": list(DOOR_Y),
                    "obstacles": 2, "rgb_image_present": False,
                    "pose_source": "scripted synthetic base route plus fixed camera-to-base extrinsics",
                    "depth_source": "uint16 millimetre representation decoded with shared M1 helper",
                    "route_is_navigation_policy": False, **route_check},
        "pipeline": ["M1 backproject_depth", "native M2 OctoMap", "native M3 C++ MapConverter",
                     "pinned local DuDe/M5", "M6", "M7", "M8", "M9", "M10"],
        "gt_room_polygons_or_ids_in_builder": False, "gt_region_inputs": False,
        "cached_detections": "one synthetic cached detection at frame 0; GT instance IDs unused; absent thereafter",
        "m4_status_counts": dict(statuses), "m4_branch_counts": dict(branches),
        "m4_rejected_frames": sum(d["m4_status"] == "rejected" for d in diagnostics),
        "m5_accepted_frames": sum(d["m5_accepted"] for d in diagnostics),
        "m5_stale_frames": sum(d["m5_stale"] for d in diagnostics),
        "m5_stale_rate": sum(d["m5_stale"] for d in diagnostics) / len(diagnostics),
        "fresh_final_grid_comparison": fresh_compare,
        "room_count_final": len(room_nodes), "room_ids_final": sorted(n.attributes["room_id"] for n in room_nodes),
        "region_track_ids_observed": sorted(set(rid for d in diagnostics for rid in d["current_region_ids"])),
        "region_ids_at_final_frame": diagnostics[-1]["current_region_ids"],
        "region_count_max_current": max(d["region_count"] for d in diagnostics),
        "room_id_set_jaccard_mean_consecutive_frames": sum(room_jaccards) / len(room_jaccards) if room_jaccards else 1.0,
        "room_membership_edge_count_final": sum(e.relation == "room_contains" for e in reloaded.edges),
        "room_adjacency_edge_count_final": sum(e.relation == "room_adjacency" for e in reloaded.edges),
        "revision": reloaded.revision, "node_count": len(reloaded.nodes), "edge_count": len(reloaded.edges),
        "agent_pose_count": len(traj), "trajectory_displacement_m": math.dist((traj[0]["x_m"], traj[0]["y_m"]), (traj[-1]["x_m"], traj[-1]["y_m"])),
        "delta_replay_equal": True, "snapshot_sha256": replay_hash,
        "dangling_self_edges": False, "duplicate_room_anchors": False,
        "frame_diagnostics": diagnostics,
        "runtime_scope": {"fixture_generation_ms": fixture_ms, "builder_updates_flush_close_and_fresh_probe_ms": active_ms,
                          "fresh_final_grid_probe_ms": fresh_probe_ms,
                          "mean_frame_update_ms": sum(d["update_runtime_ms"] for d in diagnostics) / len(diagnostics),
                          "child_rss_max_sampled_kib": rss_max,
                          "python_process_ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                          "not_a_true_peak": True},
        "classification": "synthetic scripted local native pipeline smoke; not simulator capture, navigation policy, Kaggle benchmark or real-scene quality evaluation",
        "m4_license_gate": "unresolved_for_distribution", "cloud_upload": False,
        "artifacts": ["snapshot.json", "delta_log.json", "trajectory.csv", "trajectory.json",
                      "trajectory_return.png", "scene_graph.png", "spatial_map.png", "relationship_graph.png",
                      "diagnostics.json", "manifest.json",
                      "snapshot_before_door.json", "snapshot_after_door.json", "snapshot_return.json",
                      "trajectory_before_door.png", "trajectory_after_door.png", "trajectory_return.png"],
    }
    (output / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2, sort_keys=True, allow_nan=False) + "\n")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/m11_two_room_rgbd_sequence")
    parser.add_argument("--m2-m3-binary", type=Path, default=M2_M3_BINARY)
    parser.add_argument("--dude-binary", type=Path, default=DUDE_BINARY)
    args = parser.parse_args()
    print(json.dumps(run_sequence(args.output, m2_m3_binary=args.m2_m3_binary,
                                  dude_binary=args.dude_binary), sort_keys=True, indent=2))
