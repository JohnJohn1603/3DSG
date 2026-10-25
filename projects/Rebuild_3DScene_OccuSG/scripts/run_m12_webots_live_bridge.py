#!/usr/bin/env python3
"""Bridge Webots raw packets into the project Python runtime while simulation runs.

The controller remains stdlib-only.  This host process owns the native builder,
stores the exact bytes it receives, and compares its live graph with a fresh
replay of those same stored packets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import resource
import socket
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.webots.capture import (  # noqa: E402
    camera_intrinsics, copy_bgra_rgb, copy_rangefinder_depth_m,
    webots_camera_node_pose_to_optical,
)
from standalone_sg.config import load_config  # noqa: E402
from standalone_sg.contracts import RGBDFrame, Transform  # noqa: E402
from standalone_sg.mapping import NativeRGBDMappingStage  # noqa: E402
from standalone_sg.navigation import NavigationBlockGraph  # noqa: E402
from standalone_sg.pipeline import DudeM5RegionStage, FramePacket, SceneGraphBuilder  # noqa: E402
from standalone_sg.pipeline.contracts import SceneGraphSnapshot  # noqa: E402
from standalone_sg.regions.dude_client import DuDeWorkerClient  # noqa: E402
from standalone_sg.regions.pipeline import RegionUpdateProcessor  # noqa: E402
from standalone_sg.serialization import (  # noqa: E402
    load_delta_log, load_snapshot, query_nodes, replay_deltas, trajectory_rows,
    write_delta_log, write_snapshot, write_trajectory_csv, write_trajectory_json,
)
from standalone_sg.visualization import render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map  # noqa: E402
from standalone_sg.visualization.live_overlay import (  # noqa: E402
    LiveVisualizationConfig, render_grid_overlay,
)
from m12_motor_geometry import enqueue_bounded_or_raise  # noqa: E402
from m12_webots_preflight import validate_world_contract  # noqa: E402
from run_m12_webots_native_replay import run_replay  # noqa: E402


M2_M3_BINARY = ROOT / "build/native-m3/occupancy/standalone_sg_octomap_fixture_runner"
DUDE_BINARY = ROOT / "build/native-m4-dude/occusg_dude_worker"


def _read_exact(stream, size: int) -> bytes:
    data = stream.read(size)
    if data is None or len(data) != size:
        raise RuntimeError(f"live socket ended while reading {size} bytes")
    return data


class LatestVisualizationSender:
    """Dedicated latest-only downlink so visualization cannot block RGB-D."""

    def __init__(self, connection: socket.socket | None):
        self.connection = connection
        self.condition = threading.Condition()
        self.pending = None
        self.in_flight = False
        self.closed = False
        self.error = None
        self.published = 0
        self.replaced = 0
        self.sent = 0
        self.send_events = []
        self.thread = None
        if connection is not None:
            connection.settimeout(2.0)
            self.thread = threading.Thread(target=self._run, name="m12-visualization-sender", daemon=True)
            self.thread.start()

    def publish(self, header: dict, payload: bytes) -> bool:
        if self.connection is None:
            return False
        with self.condition:
            if self.closed:
                return False
            if self.pending is not None:
                self.replaced += 1
            self.pending = (header, payload)
            self.published += 1
            self.condition.notify()
            return True

    def _run(self) -> None:
        while True:
            with self.condition:
                while self.pending is None and not self.closed:
                    self.condition.wait(timeout=0.2)
                if self.pending is None and self.closed:
                    return
                item = self.pending
                self.pending = None
                self.in_flight = True
            try:
                header, payload = item
                send_started = time.monotonic()
                self.connection.sendall((json.dumps(header, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
                self.connection.sendall(payload)
                send_finished = time.monotonic()
                self.sent += 1
                self.send_events.append({
                    "source_frame_id": header.get("source_frame_id"),
                    "map_revision": header.get("map_revision"),
                    "host_send_start_monotonic_s": send_started,
                    "host_send_end_monotonic_s": send_finished,
                    "host_send_ms": (send_finished - send_started) * 1000.0,
                })
            except (OSError, ValueError) as exc:
                with self.condition:
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.closed = True
            finally:
                with self.condition:
                    self.in_flight = False
                    self.condition.notify_all()
            if self.error is not None:
                return

    def flush(self, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        with self.condition:
            while (self.pending is not None or self.in_flight) and self.error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.error = "visualization sender flush timeout"
                    break
                self.condition.wait(timeout=remaining)

    def close(self) -> None:
        self.flush()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass

    def stats(self) -> dict:
        return {
            "enabled": self.connection is not None,
            "published": self.published,
            "sent": self.sent,
            "replaced_before_send": self.replaced,
            "error": self.error,
            "send_events": list(self.send_events),
            "transport": "dedicated TCP latest-only downlink",
        }


class LatestVisualizationWorker:
    """Render and send overlays off the mapping thread with latest-only input."""

    def __init__(self, connection: socket.socket | None, config: LiveVisualizationConfig):
        self.connection = connection
        self.config = config
        self.sender = LatestVisualizationSender(connection)
        self.condition = threading.Condition()
        self.pending = None
        self.in_flight = False
        self.closed = False
        self.submitted = 0
        self.replaced = 0
        self.rendered = 0
        self.diagnostics = []
        self.thread = threading.Thread(target=self._run, name="m12-visualization-worker", daemon=True)
        self.thread.start()

    @property
    def enabled(self) -> bool:
        return self.connection is not None

    def submit(self, packet: FramePacket, grid, trajectory_xy: list[tuple[float, float]], status: str) -> bool:
        if not self.enabled:
            return False
        with self.condition:
            if self.closed:
                return False
            if self.pending is not None:
                self.replaced += 1
            self.pending = (packet, grid, list(trajectory_xy), status, time.monotonic())
            self.submitted += 1
            self.condition.notify()
            return True

    def _run(self) -> None:
        while True:
            with self.condition:
                while self.pending is None and not self.closed:
                    self.condition.wait(timeout=0.2)
                if self.pending is None and self.closed:
                    return
                job = self.pending
                self.pending = None
                self.in_flight = True
            try:
                packet, grid, trajectory_xy, status, submitted_monotonic_s = job
                render_started = time.monotonic()
                header, payload, diagnostics = _visualization_message(
                    packet, grid, trajectory_xy, self.config, status,
                )
                render_finished = time.monotonic()
                header.update({
                    "host_overlay_submit_monotonic_s": submitted_monotonic_s,
                    "host_overlay_render_start_monotonic_s": render_started,
                    "host_overlay_render_end_monotonic_s": render_finished,
                    "host_overlay_render_ms": (render_finished - render_started) * 1000.0,
                })
                if self.sender.publish(header, payload):
                    self.rendered += 1
                    self.diagnostics.append({
                        "source_frame_id": packet.frame_id, "map_revision": int(grid.revision),
                        "simulation_timestamp_s": float(packet.timestamp_s), "status": status,
                        "host_overlay_submit_monotonic_s": submitted_monotonic_s,
                        "host_overlay_render_start_monotonic_s": render_started,
                        "host_overlay_render_end_monotonic_s": render_finished,
                        "host_overlay_render_ms": (render_finished - render_started) * 1000.0,
                        **diagnostics,
                    })
            except Exception as exc:
                self.sender.error = f"{type(exc).__name__}: {exc}"
                with self.condition:
                    self.closed = True
            finally:
                with self.condition:
                    self.in_flight = False
                    self.condition.notify_all()
            if self.sender.error is not None:
                return

    def flush(self, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        with self.condition:
            while (self.pending is not None or self.in_flight) and self.sender.error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.sender.error = "visualization worker flush timeout"
                    break
                self.condition.wait(timeout=remaining)
        self.sender.flush(timeout_s=max(0.1, deadline - time.monotonic()))

    def close(self) -> None:
        self.flush()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.thread.join(timeout=2.0)
        self.sender.close()

    def stats(self) -> dict:
        return {
            "enabled": self.enabled, "submitted": self.submitted,
            "rendered": self.rendered, "replaced_before_render": self.replaced,
            "render_diagnostics": list(self.diagnostics), "sender": self.sender.stats(),
        }


def _packet_from_raw(metadata: dict, bgra: bytes, depth_bytes: bytes, config: dict,
                     pipeline_config: Path) -> tuple[FramePacket, np.ndarray, np.ndarray]:
    width, height = map(int, metadata["resolution_px"])
    rgb = copy_bgra_rgb(bgra, width, height)
    depth_m, valid_mask = copy_rangefinder_depth_m(depth_bytes, width, height)
    intrinsics = camera_intrinsics(width, height, float(metadata["rangefinder_horizontal_fov_rad"]))
    matrix = np.asarray(config["world_to_canonical_matrix"], dtype=np.float64)
    T_world_webots = Transform(matrix, "world", "webots_world", source="calibration")
    timestamp = float(metadata["timestamp_s"])
    T_W_Copt = webots_camera_node_pose_to_optical(metadata["camera_node_pose_row_major"], timestamp, T_world_webots)
    T_webots_base = Transform(np.asarray(metadata["base_pose_row_major"], dtype=np.float64).reshape(4, 4),
                              "webots_world", "base", timestamp, "ground_truth")
    T_W_base = Transform(matrix @ T_webots_base.matrix, "world", "base", timestamp, "ground_truth")
    baseline = load_config(pipeline_config)
    packet = FramePacket(
        scene_id=str(config["world_id"]), sequence_id=str(config["sequence_id"]),
        frame_id=str(metadata["frame_id"]), frame_index=int(metadata["frame_index"]),
        timestamp_s=timestamp, input_mode="rgbd", base_pose_W=tuple(float(x) for x in T_W_base.translation),
        rgbd=RGBDFrame(depth_m, intrinsics, T_W_Copt, timestamp, rgb=rgb,
                       valid_mask=valid_mask, frame_id=str(metadata["frame_id"])),
        detections=None,
        source_metadata={
            "capture_kind": "webots_r2025a_raw_capture_offline_replay",
            "world_id": config["world_id"], "webots_version": "R2025a",
            "depth_units": "m", "depth_scale_to_m": 1.0, "depth_representation": "axial",
            "depth_invalid_policy": "non-finite and non-positive RangeFinder samples masked; never converted to no-return rays",
            "valid_mask_source": "finite positive RangeFinder depth samples",
            "sensor_pose_source": "actual Supervisor.getPose(CAMERA_NODE)",
            "base_pose_source": "actual Supervisor.getPose(PROBE_ROBOT); separately transformed",
            "T_world_webots": matrix.tolist(), "T_world_sensor_optical": T_W_Copt.matrix.tolist(),
            "T_world_base": T_W_base.matrix.tolist(),
            "min_depth_m": float(metadata["min_range_m"]), "max_depth_m": float(metadata["max_range_m"]),
            "max_range_m": float(metadata["max_range_m"]),
            "backprojection_stride": baseline.backprojection.stride,
            "intrinsics": {"width": width, "height": height, "fx": intrinsics.fx,
                           "fy": intrinsics.fy, "cx": intrinsics.cx, "cy": intrinsics.cy,
                           "calibration_id": intrinsics.calibration_id},
            "detections_provider": "absent", "regions_or_room_gt_in_builder": False,
            "capture_config_sha256": hashlib.sha256((ROOT / config["config_path"]).read_bytes()).hexdigest()
            if "config_path" in config else "runtime_config",
        },
    )
    return packet, depth_m, valid_mask


class _TimedMappingStage:
    """Observe M1/M2/M3 completion without adding a second projection call."""

    def __init__(self, delegate, grid_ready_callback=None):
        self.delegate = delegate
        self.grid_ready_callback = grid_ready_callback
        self.last_timing = {}

    @property
    def records(self):
        return self.delegate.records

    def build_grid(self, packet):
        started = time.perf_counter()
        grid = self.delegate.build_grid(packet)
        finished = time.perf_counter()
        record = dict(self.delegate.records[-1])
        self.last_timing = {
            "mapping_runtime_ms": (finished - started) * 1000.0,
            "m1_runtime_ms": float(record.get("m1_runtime_ms", 0.0)),
            "m2_runtime_ms": float(record.get("m2_runtime_ms", 0.0)),
            "m3_runtime_ms": float(record.get("m3_runtime_ms", 0.0)),
        }
        if self.grid_ready_callback is not None:
            self.grid_ready_callback(packet, grid, dict(self.last_timing))
        return grid

    def child_pids(self):
        return self.delegate.child_pids()

    def close(self):
        self.delegate.close()


def _summarize(values: list[float]) -> dict:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return {"count": 0, "median": None, "p95": None, "max": None}
    def percentile(fraction: float) -> float:
        if len(clean) == 1:
            return clean[0]
        position = (len(clean) - 1) * fraction
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return clean[lower]
        return clean[lower] + (clean[upper] - clean[lower]) * (position - lower)
    return {
        "count": len(clean),
        "median": percentile(0.5),
        "p95": percentile(0.95),
        "max": clean[-1],
    }


def _latency_report(
    diagnostics: list[dict],
    frames: list[dict],
    visualization_stats: dict,
    controller_manifest: dict,
    queue_metrics: dict,
    simulation_mode: str,
) -> dict:
    worker_events = {
        event.get("source_frame_id"): event
        for event in visualization_stats.get("render_diagnostics", [])
    }
    send_events = {
        event.get("source_frame_id"): event
        for event in visualization_stats.get("sender", {}).get("send_events", [])
    }
    controller_events = {
        event.get("source_frame_id"): event
        for event in controller_manifest.get("visualization", {}).get("render_events", [])
    }
    rows = []
    for diagnostic in diagnostics:
        frame_id = diagnostic["frame_id"]
        capture = diagnostic.get("capture_timing", {})
        host = diagnostic.get("host_timing", {})
        timing = diagnostic.get("timing", {})
        worker = worker_events.get(frame_id, {})
        sender = send_events.get(frame_id, {})
        controller = controller_events.get(frame_id, {})
        row = {
            "frame_id": frame_id,
            "frame_index": diagnostic["frame_index"],
            "simulation_timestamp_s": diagnostic["simulation_timestamp_s"],
            "capture_interval_simulation_s": None,
            "controller_capture_monotonic_s": capture.get("controller_capture_monotonic_s"),
            "host_receive_completed_monotonic_s": host.get("host_receive_completed_monotonic_s"),
            "host_process_started_monotonic_s": host.get("host_process_started_monotonic_s"),
            "host_process_finished_monotonic_s": host.get("host_process_finished_monotonic_s"),
            "m1_runtime_ms": timing.get("m1_runtime_ms"),
            "m2_runtime_ms": timing.get("m2_runtime_ms"),
            "m3_runtime_ms": timing.get("m3_runtime_ms"),
            "m4_backend_runtime_ms": timing.get("m4_backend_runtime_ms"),
            "m5_guard_tracker_runtime_ms": timing.get("m5_guard_tracker_runtime_ms"),
            "m4_m5_runtime_ms": timing.get("m4_m5_runtime_ms"),
            "m6_m9_graph_runtime_ms": timing.get("m6_m9_graph_runtime_ms"),
            "builder_update_runtime_ms": timing.get("builder_update_runtime_ms"),
            "queue_wait_ms": host.get("host_queue_wait_ms"),
            "queue_length_after_dequeue": host.get("host_queue_length_after_dequeue"),
            "queue_length_after_enqueue": host.get("queue_length_after_enqueue"),
            "host_processing_wall_ms": host.get("host_processing_wall_ms"),
            "overlay_submit_monotonic_s": worker.get("host_overlay_submit_monotonic_s"),
            "overlay_render_start_monotonic_s": worker.get("host_overlay_render_start_monotonic_s"),
            "overlay_render_end_monotonic_s": worker.get("host_overlay_render_end_monotonic_s"),
            "overlay_render_ms": worker.get("host_overlay_render_ms"),
            "overlay_send_ms": sender.get("host_send_ms"),
            "display_map_revision": controller.get("map_revision"),
            "display_map_simulation_timestamp_s": controller.get("map_simulation_timestamp_s"),
            "display_simulation_timestamp_s": controller.get("display_simulation_timestamp_s"),
            "map_age_simulation_s": controller.get("map_age_simulation_s"),
            "display_render_ms": controller.get("controller_display_render_ms"),
            "display_render_end_monotonic_s": controller.get("controller_display_render_end_monotonic_s"),
        }
        rows.append(row)

    rows.sort(key=lambda row: row["frame_index"])
    previous_simulation_s = None
    for row in rows:
        current = float(row["simulation_timestamp_s"])
        if previous_simulation_s is not None:
            row["capture_interval_simulation_s"] = current - previous_simulation_s
        previous_simulation_s = current

    def values(key: str) -> list[float]:
        return [float(row[key]) for row in rows if row.get(key) is not None]

    route_stop_s = controller_manifest.get("route_stop_simulation_s")
    rendered_before_stop = [
        event for event in controller_manifest.get("visualization", {}).get("render_events", [])
        if route_stop_s is None or float(event.get("display_simulation_timestamp_s", 0.0)) <= float(route_stop_s)
    ]
    send_ends = [event.get("host_send_end_monotonic_s") for event in visualization_stats.get("sender", {}).get("send_events", [])]
    send_ends = [float(value) for value in send_ends if value is not None]
    route_stop_mono = controller_manifest.get("route_stop_monotonic_s")
    drain_ms = None
    if route_stop_mono is not None and send_ends:
        drain_ms = max(0.0, (max(send_ends) - float(route_stop_mono)) * 1000.0)
    return {
        "clock": "time.monotonic for wall-clock processing/transport; simulation timestamps are Webots time",
        "simulation_mode": simulation_mode,
        "frame_count": len(rows),
        "per_frame": rows,
        "summary_ms": {
            "queue_wait": _summarize(values("queue_wait_ms")),
            "host_processing": _summarize(values("host_processing_wall_ms")),
            "m1": _summarize(values("m1_runtime_ms")),
            "m2": _summarize(values("m2_runtime_ms")),
            "m3": _summarize(values("m3_runtime_ms")),
            "m4_backend": _summarize(values("m4_backend_runtime_ms")),
            "m5_guard_tracker": _summarize(values("m5_guard_tracker_runtime_ms")),
            "m4_m5": _summarize(values("m4_m5_runtime_ms")),
            "m6_m9_graph": _summarize(values("m6_m9_graph_runtime_ms")),
            "builder_update": _summarize(values("builder_update_runtime_ms")),
            "overlay_render": _summarize(values("overlay_render_ms")),
            "overlay_send": _summarize(values("overlay_send_ms")),
            "display_render": _summarize(values("display_render_ms")),
            "capture_interval_simulation": _summarize(values("capture_interval_simulation_s")),
        },
        "summary_simulation_s": {
            "map_age": _summarize(values("map_age_simulation_s")),
            "capture_interval": _summarize(values("capture_interval_simulation_s")),
        },
        "summary_queue_length": {
            "after_dequeue": _summarize(values("queue_length_after_dequeue")),
            "after_enqueue": _summarize(values("queue_length_after_enqueue")),
        },
        "queue": dict(queue_metrics),
        "rendered_revisions_before_route_end": len(rendered_before_stop),
        "drain_after_route_stop_wall_ms": drain_ms,
        "controller_visualization_render_count": len(controller_manifest.get("visualization", {}).get("render_events", [])),
    }


class LiveGraph:
    def __init__(self, config: dict, config_sha: str, pipeline_config: Path,
                 m2_binary: Path, dude_binary: Path, grid_ready_callback=None):
        phase_config = load_config(pipeline_config)
        self.mapping = NativeRGBDMappingStage(m2_binary, resolution_m=phase_config.occupancy.resolution_m,
                                              timeout_s=30.0, default_max_range_m=8.0)
        self.mapping_stage = _TimedMappingStage(self.mapping, grid_ready_callback)
        self.region_stage = DudeM5RegionStage(DuDeWorkerClient(dude_binary), RegionUpdateProcessor())
        self.navigation = NavigationBlockGraph(phase_config.navigation)
        self.builder = SceneGraphBuilder(mapping_stage=self.mapping_stage, region_stage=self.region_stage,
                                          navigation=self.navigation, config_hash=f"m12-webots-{config_sha}")
        self.deltas, self.snapshots, self.diagnostics = [], [], []
        self.last_packet = None
        self.trajectory_xy: list[tuple[float, float]] = []
        self.started = time.perf_counter()

    def update(self, packet: FramePacket) -> None:
        started = time.perf_counter()
        snapshot, delta = self.builder.update(packet)
        finished = time.perf_counter()
        grid = self.builder.last_grid
        if grid is None:
            raise RuntimeError("native M3 did not expose the grid used by SceneGraphBuilder")
        self.deltas.append(delta)
        self.snapshots.append(snapshot)
        self.last_packet = packet
        self.trajectory_xy.append((float(packet.base_pose_W[0]), float(packet.base_pose_W[1])))
        response, result = self.region_stage.last_response, self.region_stage.last_result
        mapping_timing = dict(self.mapping_stage.last_timing)
        region_timing = dict(self.region_stage.last_timing)
        builder_runtime_ms = (finished - started) * 1000.0
        downstream_runtime_ms = max(
            0.0,
            builder_runtime_ms
            - float(mapping_timing.get("mapping_runtime_ms", 0.0))
            - float(region_timing.get("m4_m5_runtime_ms", 0.0)),
        )
        self.diagnostics.append({
            "frame_id": packet.frame_id, "frame_index": packet.frame_index,
            "simulation_timestamp_s": packet.timestamp_s,
            "processing_runtime_ms": builder_runtime_ms,
            "timing": {
                **mapping_timing,
                **region_timing,
                "builder_update_runtime_ms": builder_runtime_ms,
                "m6_m9_graph_runtime_ms": downstream_runtime_ms,
            },
            "base_pose_W": list(packet.base_pose_W),
            "sensor_pose_W_Copt": packet.rgbd.T_W_Copt.matrix.tolist(),
            "m4_status": response.get("status"), "m4_branch": response.get("branch"),
            "m5_accepted": bool(result.accepted), "m5_stale": bool(result.stale),
            "region_count": len(result.current_regions),
            "region_ids": [int(r.persistent_id) for r in result.current_regions],
            "room_count": len(query_nodes(snapshot, "room")),
            "room_ids": [n.node_id for n in query_nodes(snapshot, "room")],
            "graph_revision": snapshot.revision,
            "stage_status": dict(delta.diagnostics["stage_status"]), "events": list(delta.events),
            "sampled_python_ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        })
        return snapshot, delta, grid

    def close(self, final_timestamp: float):
        final, flush_delta = self.builder.flush(final_timestamp + 1e-6)
        self.deltas.append(flush_delta)
        self.builder.close()
        return final


def _write_capture_manifest(
    capture: Path,
    config_path: Path,
    config: dict,
    frames: list[dict],
    done_message: dict | None = None,
) -> None:
    config_bytes = config_path.read_bytes()
    world_relative = config.get("world_file")
    world_path = (ROOT / world_relative).resolve() if isinstance(world_relative, str) else None
    if world_path is None or ROOT not in world_path.parents or not world_path.is_file():
        raise RuntimeError("live config must declare an existing project-local world_file")
    done_message = done_message or {}
    manifest = {
        "capture_status": str(done_message.get("capture_status", "complete")),
        "status": done_message.get("status", "complete"),
        "error": done_message.get("error"),
        "webots_version": "R2025a",
        "world_id": config["world_id"], "sequence_id": config["sequence_id"],
        "world_config": str(config_path.relative_to(ROOT)),
        "world_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "world_file": world_relative, "world_sha256": hashlib.sha256(world_path.read_bytes()).hexdigest(),
        "route_type": config["route_type"], "motion_mode": config.get("motion_mode", "pose_replay"),
        "frame_count": len(frames), "frames": frames,
        "detections": "absent", "regions_or_room_gt_in_capture_or_builder": False,
        "capture_transport": "localhost TCP live socket; raw buffers persisted before replay",
    }
    # The controller writes the authoritative all-physics-step trace to its
    # configured capture directory. Copy that trace beside the live packets so
    # the same replay validator can check collision coverage for live captures.
    controller_capture = (ROOT / config["output_dir"]).resolve()
    controller_manifest_path = controller_capture / "capture_manifest.json"
    controller_trace_path = controller_capture / "physics_trace.json"
    controller_manifest = {}
    if config.get("motion_mode") == "motor":
        if controller_manifest_path.is_file():
            controller_manifest = json.loads(controller_manifest_path.read_text(encoding="utf-8"))
            manifest.update({
                "capture_status": controller_manifest.get(
                    "capture_status", manifest["capture_status"]
                ),
                "status": controller_manifest.get("status", manifest["status"]),
                "error": controller_manifest.get("error", manifest["error"]),
                "waypoints_reached": int(controller_manifest["waypoints_reached"]),
                "waypoint_count": int(controller_manifest["waypoint_count"]),
                "route_stop_simulation_s": controller_manifest.get("route_stop_simulation_s"),
                "route_stop_monotonic_s": controller_manifest.get("route_stop_monotonic_s"),
            })
            if controller_trace_path.is_file():
                shutil.copyfile(controller_trace_path, capture / "physics_trace.json")
                manifest.update({
                    "physics_trace_file": "physics_trace.json",
                    "physics_step_count": int(controller_manifest["physics_step_count"]),
                    "collision_check": controller_manifest["collision_check"],
                })
            else:
                manifest["capture_status"] = "incomplete"
                manifest["error"] = "motor controller manifest has no physics trace"
        else:
            # A GUI Save-before-closing dialog can prevent the controller
            # artifact from being visible before the host has to finalize.
            # Preserve the processed packets and make the missing evidence
            # explicit instead of discarding the graph PNGs.
            manifest["capture_status"] = "incomplete"
            manifest["status"] = "controller_manifest_missing"
            manifest["error"] = "motor controller manifest was unavailable before Webots shutdown"
        if "visualization" in controller_manifest:
            manifest["visualization"] = controller_manifest["visualization"]
    (capture / "capture_manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_live_graph_artifacts(output: Path, final_live, deltas: list, diagnostics: list) -> None:
    """Persist graph/PNG output before waiting for the Webots GUI to close."""
    output.mkdir(parents=True, exist_ok=True)
    write_snapshot(output / "snapshot.json", final_live)
    write_delta_log(output / "delta_log.json", deltas)
    write_trajectory_csv(output / "trajectory.csv", final_live)
    write_trajectory_json(output / "trajectory.json", final_live)
    render_trajectory_map(final_live, output / "trajectory_rooms.png")
    render_graph(final_live, output / "scene_graph.png")
    render_spatial_map(final_live, output / "spatial_map.png")
    render_relationship_graph(final_live, output / "relationship_graph.png")
    (output / "frame_diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def _packet_base_yaw(packet: FramePacket) -> float:
    matrix = np.asarray(packet.source_metadata["T_world_base"], dtype=np.float64).reshape(4, 4)
    return float(math.atan2(matrix[1, 0], matrix[0, 0]))


def _visualization_message(
    packet: FramePacket,
    grid,
    trajectory_xy: list[tuple[float, float]],
    config: LiveVisualizationConfig,
    status: str,
) -> tuple[dict, bytes, dict]:
    payload, diagnostics = render_grid_overlay(
        grid,
        base_xy=(float(packet.base_pose_W[0]), float(packet.base_pose_W[1])),
        yaw_rad=_packet_base_yaw(packet),
        trajectory_xy=trajectory_xy,
        config=config,
    )
    header = {
        "type": "visualization", "schema_version": 1,
        "sequence_id": packet.sequence_id, "source_frame_id": packet.frame_id,
        "map_revision": int(grid.revision), "simulation_timestamp_s": float(packet.timestamp_s),
        "frame_id": grid.frame_id, "origin_xy": [float(v) for v in grid.origin_xy],
        "resolution_m": float(grid.resolution_m), "grid_width": int(grid.shape[1]),
        "grid_height": int(grid.shape[0]), "projection_mode": grid.projection_mode,
        "grid_semantics": "-1=unknown, 0=free, 1..100=MapConverter neighbour occupation score; not probability",
        "payload_kind": "rgb888", "display_width": config.width, "display_height": config.height,
        "payload_bytes": len(payload), "status": status,
    }
    return header, payload, diagnostics


def run_live(args) -> dict:
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["config_path"] = str(config_path)
    preflight = validate_world_contract(args.world, config_path, config, ROOT)
    visualization_config = LiveVisualizationConfig.from_mapping(config.get("visualization", {}))
    pipeline_config = (ROOT / config.get("pipeline_config", "configs/reference_code_mp3d.yaml")).resolve()
    config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    for binary in (args.m2_m3_binary, args.dude_binary):
        if not binary.is_file():
            raise RuntimeError(f"native worker missing: {binary}")
    capture = args.capture.resolve()
    capture.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.live_port))
    server.listen(1)
    visualization_server = None
    if visualization_config.enabled:
        visualization_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        visualization_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        visualization_server.bind(("127.0.0.1", args.visualization_port))
        visualization_server.listen(1)
    env = dict(os.environ)
    env["M12_LIVE_PORT"] = str(args.live_port)
    try:
        env["M12_CONFIG_PATH"] = str(config_path.relative_to(ROOT))
    except ValueError as exc:
        raise RuntimeError("--config must be a project-local path") from exc
    if visualization_config.enabled:
        env["M12_VIS_PORT"] = str(args.visualization_port)
    webots_command = [args.webots, f"--port={args.webots_port}"]
    if args.webots_mode == "fast":
        webots_command.append("--mode=fast")
    if not args.show_webots:
        webots_command.append("--batch")
    webots_command.extend(["--stdout", "--stderr", str(args.world)])
    webots = subprocess.Popen(webots_command, cwd=ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    server.settimeout(30.0)
    visualization_connection = None
    try:
        conn, _address = server.accept()
    except TimeoutError:
        webots.terminate()
        webots_output, _ = webots.communicate(timeout=10)
        raise RuntimeError(f"live Webots controller did not connect: {webots_output[-3000:]}")
    if visualization_server is not None:
        visualization_server.settimeout(5.0)
        try:
            visualization_connection, _ = visualization_server.accept()
        except TimeoutError:
            visualization_connection = None
    stream = conn.makefile("rb")
    visualization_worker = LatestVisualizationWorker(visualization_connection, visualization_config)
    live = None
    frames, last_timestamp = [], None
    packet_queue = queue.Queue(maxsize=max(1, int(args.queue_size)))
    queue_metrics = {"high_water_mark": 0, "put_count": 0}
    queue_metrics_lock = threading.Lock()
    receiver_error = []
    receiver_done = threading.Event()
    done_message = {}
    last_publish_simulation_s = -float("inf")
    visualization_submissions = {}
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    graph_artifacts_written = False
    controller_capture_error = None
    webots_shutdown_warning = None

    def publish_visualization(
        packet: FramePacket, grid, status: str, force: bool = False,
        trajectory_xy: list[tuple[float, float]] | None = None,
        stage: str = "post_graph",
        timing: dict | None = None,
    ) -> None:
        nonlocal last_publish_simulation_s
        if not visualization_config.enabled or not visualization_worker.enabled or grid is None:
            return
        period = 1.0 / visualization_config.publish_max_hz
        if not force and packet.timestamp_s - last_publish_simulation_s < period:
            return
        submitted_monotonic_s = time.monotonic()
        trajectory = list(trajectory_xy if trajectory_xy is not None else live.trajectory_xy)
        if visualization_worker.submit(packet, grid, trajectory, status):
            last_publish_simulation_s = packet.timestamp_s
            visualization_submissions[packet.frame_id] = {
                "host_overlay_submit_monotonic_s": submitted_monotonic_s,
                "map_revision": int(grid.revision),
                "map_simulation_timestamp_s": float(packet.timestamp_s),
                "status": status,
                "stage": stage,
                "timing": dict(timing or {}),
            }

    def mapping_grid_ready(packet: FramePacket, grid, timing: dict) -> None:
        # The proxy calls this immediately after M3 returns and before M4–M9.
        current_trajectory = list(live.trajectory_xy)
        current_trajectory.append((float(packet.base_pose_W[0]), float(packet.base_pose_W[1])))
        publish_visualization(
            packet, grid, "active", trajectory_xy=current_trajectory,
            stage="m3_ready", timing=timing,
        )

    live = LiveGraph(
        config, config_sha, pipeline_config, args.m2_m3_binary, args.dude_binary,
        grid_ready_callback=mapping_grid_ready,
    )

    def receive_packets() -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    raise RuntimeError("Webots live socket closed without done marker")
                message = json.loads(line.decode("utf-8"))
                if message.get("type") == "done":
                    done_message.update(message)
                    break
                if message.get("type") != "frame":
                    raise RuntimeError(f"unexpected live message type: {message.get('type')!r}")
                metadata = message["metadata"]
                host_receive_started_monotonic_s = time.monotonic()
                bgra = _read_exact(stream, int(message["camera_bgra_bytes"]))
                depth_bytes = _read_exact(stream, int(message["range_depth_bytes"]))
                host_receive_completed_monotonic_s = time.monotonic()
                host_enqueue_started_monotonic_s = time.monotonic()
                host_timing = {
                    "host_receive_started_monotonic_s": host_receive_started_monotonic_s,
                    "host_receive_completed_monotonic_s": host_receive_completed_monotonic_s,
                    "host_enqueue_started_monotonic_s": host_enqueue_started_monotonic_s,
                }
                # Bounded queue: block the controller's sender, then fail explicitly.
                enqueue_bounded_or_raise(packet_queue,
                    (metadata, bgra, depth_bytes, host_timing),
                    args.queue_put_timeout_s,
                )
                host_enqueue_completed_monotonic_s = time.monotonic()
                queue_length = packet_queue.qsize()
                host_timing["host_enqueue_completed_monotonic_s"] = host_enqueue_completed_monotonic_s
                host_timing["queue_length_after_enqueue"] = queue_length
                with queue_metrics_lock:
                    queue_metrics["put_count"] += 1
                    queue_metrics["high_water_mark"] = max(queue_metrics["high_water_mark"], queue_length)
        except Exception as exc:
            receiver_error.append(f"{type(exc).__name__}: {exc}")
        finally:
            receiver_done.set()

    receiver = threading.Thread(target=receive_packets, name="m12-live-receiver", daemon=True)
    receiver.start()

    try:
        while not receiver_done.is_set() or not packet_queue.empty():
            try:
                metadata, bgra, depth_bytes, host_timing = packet_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            host_process_started_monotonic_s = time.monotonic()
            host_timing["host_enqueue_completed_monotonic_s"] = host_timing.get(
                "host_enqueue_completed_monotonic_s", host_process_started_monotonic_s,
            )
            host_timing["host_process_started_monotonic_s"] = host_process_started_monotonic_s
            host_timing["host_queue_length_after_dequeue"] = packet_queue.qsize()
            host_timing["host_queue_wait_ms"] = max(
                0.0,
                (host_process_started_monotonic_s - host_timing["host_enqueue_completed_monotonic_s"]) * 1000.0,
            )
            index = int(metadata["frame_index"])
            frame_dir = capture / "frames" / f"{index:03d}"
            frame_dir.mkdir(parents=True, exist_ok=True)
            (frame_dir / "camera_bgra.bin").write_bytes(bgra)
            (frame_dir / "range_depth_f32.bin").write_bytes(depth_bytes)
            persisted_metadata = dict(metadata)
            persisted_metadata["host_timing"] = dict(host_timing)
            (frame_dir / "metadata.json").write_text(
                json.dumps(persisted_metadata, indent=2, allow_nan=False) + "\n", encoding="utf-8"
            )
            packet, _depth, _mask = _packet_from_raw(metadata, bgra, depth_bytes, config, pipeline_config)
            _snapshot, _delta, grid = live.update(packet)
            host_process_finished_monotonic_s = time.monotonic()
            host_timing["host_process_finished_monotonic_s"] = host_process_finished_monotonic_s
            host_timing["host_processing_wall_ms"] = (
                host_process_finished_monotonic_s - host_process_started_monotonic_s
            ) * 1000.0
            live.diagnostics[-1]["host_timing"] = host_timing
            live.diagnostics[-1]["capture_timing"] = {
                key: metadata.get(key)
                for key in (
                    "controller_capture_monotonic_s",
                    "controller_capture_buffer_complete_monotonic_s",
                    "controller_frame_send_start_monotonic_s",
                )
                if key in metadata
            }
            response = live.region_stage.last_response or {}
            region_result = live.region_stage.last_result
            status = "stale" if region_result is not None and region_result.stale else "active"
            if response.get("status") == "rejected":
                status = "stale"
            live.diagnostics[-1]["visualization_submit"] = visualization_submissions.get(packet.frame_id)
            last_timestamp = packet.timestamp_s
            frames.append({"frame_id": metadata["frame_id"], "frame_index": index,
                           "timestamp_s": metadata["timestamp_s"], "route_label": metadata.get("route_label"),
                           "frame_dir": str(frame_dir.relative_to(capture)),
                           "base_pose_row_major": metadata["base_pose_row_major"],
                           "sensor_pose_row_major": metadata["camera_node_pose_row_major"],
                           "host_timing": dict(host_timing)})
            packet_queue.task_done()
        receiver.join(timeout=1.0)
        if receiver_error:
            webots_diagnostic = ""
            if webots.poll() is not None:
                try:
                    webots_diagnostic, _ = webots.communicate(timeout=5.0)
                except subprocess.TimeoutExpired:
                    webots_diagnostic = "<Webots output did not close within diagnostic timeout>"
            raise RuntimeError("live receiver failed: " + receiver_error[0] +
                               (f"; Webots output: {webots_diagnostic[-4000:]}" if webots_diagnostic else ""))
        if config.get("motion_mode") == "motor" and done_message.get("capture_status") != "complete":
            # Do not discard frames already accepted by M1--M9.  Export a
            # partial graph and report the controller failure in its manifest.
            # This is especially important for Q/route-recording runs that
            # stop after a collision or timeout.
            controller_capture_error = (
                "Webots motor controller did not complete route: "
                + json.dumps(done_message, sort_keys=True)
            )
        if last_timestamp is None:
            raise RuntimeError("live stream contained no frames")
        final_live = live.close(last_timestamp)
        if live.last_packet is not None and live.builder.last_grid is not None:
            publish_visualization(
                live.last_packet, live.builder.last_grid, "finished", force=True,
                trajectory_xy=live.trajectory_xy, stage="finished",
            )
        visualization_worker.flush(timeout_s=10.0)
        # Export before Webots is asked to close.  The GUI can show a
        # Save-before-closing dialog and otherwise block the old finalizer.
        _write_live_graph_artifacts(output, final_live, live.deltas, live.diagnostics)
        graph_artifacts_written = True
    except Exception as exc:
        visualization_worker.close()
        stream.close(); conn.close(); server.close()
        if visualization_server is not None:
            visualization_server.close()
        if webots.poll() is None:
            webots.terminate()
        try:
            diagnostic_output, _ = webots.communicate(timeout=10.0)
        except subprocess.TimeoutExpired:
            diagnostic_output = "<Webots diagnostic process did not terminate>"
        raise RuntimeError(f"{exc}; Webots output: {diagnostic_output[-4000:]}") from exc
    visualization_worker.close()
    stream.close(); conn.close(); server.close()
    if visualization_server is not None:
        visualization_server.close()
    try:
        webots_output, _ = webots.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        # At this point graph artifacts are already durable.  A GUI close
        # dialog must not erase them or leave the host waiting forever.
        webots_shutdown_warning = "Webots did not exit after finalization; terminated after 45s"
        webots.terminate()
        try:
            webots_output, _ = webots.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            webots.kill()
            webots_output, _ = webots.communicate(timeout=10)
    if webots.returncode != 0 and not graph_artifacts_written:
        raise RuntimeError(f"Webots live process failed with code {webots.returncode}: {webots_output[-2000:]}")
    _write_capture_manifest(capture, config_path, config, frames, done_message)
    initial = SceneGraphSnapshot(1, None, None, 0, None, (), (), final_live.config_hash,
                                 {"mapping": False, "regions": False})
    _write_live_graph_artifacts(output, final_live, live.deltas, live.diagnostics)
    if replay_deltas(initial, load_delta_log(output / "delta_log.json")) != final_live:
        raise RuntimeError("live delta log does not replay to live snapshot")
    (output / "frame_diagnostics.json").write_text(json.dumps(live.diagnostics, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    controller_manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    visualization_host_stats = visualization_worker.stats()
    with queue_metrics_lock:
        final_queue_metrics = dict(queue_metrics)
    latency_report = _latency_report(
        live.diagnostics, frames, visualization_host_stats, controller_manifest,
        final_queue_metrics, args.webots_mode,
    )
    (output / "latency_diagnostics.json").write_text(
        json.dumps(latency_report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )

    replay_output = args.replay_output.resolve()
    if controller_manifest.get("capture_status") == "complete":
        replay_manifest = run_replay(capture, config_path, replay_output, deterministic_check=True,
                                     m2_binary=args.m2_m3_binary, dude_binary=args.dude_binary)
        replay_snapshot = load_snapshot(replay_output / "snapshot.json")
        live_equal_replay = replay_snapshot == final_live
        if not live_equal_replay:
            raise RuntimeError("live graph differs from replay graph for the same captured packets")
    else:
        replay_manifest = {
            "status": "skipped",
            "reason": "capture is not complete; replay requires a complete motor physics trace",
        }
        live_equal_replay = False
    run_status = "pass" if controller_manifest.get("capture_status") == "complete" and live_equal_replay else "partial"
    warnings = []
    if controller_capture_error:
        warnings.append(controller_capture_error)
    if webots_shutdown_warning:
        warnings.append(webots_shutdown_warning)
    if webots.returncode != 0:
        warnings.append(f"Webots exited with code {webots.returncode}; graph artifacts were already exported")
    report = {
        "kind": "webots_live_socket_to_native_builder_then_same_packet_replay",
        "status": run_status, "world_id": config["world_id"], "sequence_id": config["sequence_id"],
        "simulation_mode": args.webots_mode,
        "frames": len(frames), "transport": "Webots controller stdlib socket -> bounded host queue -> project Python runtime",
        "queue": {"maxsize": int(args.queue_size), "put_timeout_s": float(args.queue_put_timeout_s),
                  "slow_consumer_policy": "block_sender_then_fail_explicitly", "silent_drop": False},
        "detections": "absent", "regions_or_room_gt_in_builder": False,
        "live_builder_snapshot": {"revision": final_live.revision, "nodes": len(final_live.nodes),
                                  "edges": len(final_live.edges), "regions": len(live.diagnostics[-1]["region_ids"]),
                                  "rooms": len(query_nodes(final_live, "room"))},
        "live_delta_replay_equal": True, "live_equals_same_packet_replay": live_equal_replay,
        "mapping_updates": len(frames),
        "latency": latency_report,
        "visualization": {
            "config": {
                "enabled": visualization_config.enabled, "publish_max_hz": visualization_config.publish_max_hz,
                "render_max_hz": visualization_config.render_max_hz,
            },
            "host": visualization_host_stats,
            "controller": controller_manifest.get("visualization"),
            "published_frame_diagnostics": visualization_worker.diagnostics,
        },
        "replay_manifest": replay_manifest,
        "warnings": warnings,
        "artifacts": {"capture": str(capture.relative_to(ROOT)), "live_graph": str(output.relative_to(ROOT)),
                       "replay_graph": str(replay_output.relative_to(ROOT))},
        "world_preflight": preflight,
        "limitations": ["scripted Supervisor poses, not robot dynamics or navigation policy",
                        "live bridge validates graph parity, not live Webots controller execution of SceneGraphBuilder",
                        "no RGB object detector; detections are absent", "cloud and license gates remain open"],
    }
    (output / "live_bridge_manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-output", type=Path, required=True)
    parser.add_argument("--live-port", type=int, default=1260)
    parser.add_argument("--visualization-port", type=int, default=1262)
    parser.add_argument("--webots-port", type=int, default=1261)
    parser.add_argument("--webots-mode", choices=("fast", "real-time"), default="fast",
                        help="Webots simulation speed; fast adds --mode=fast, real-time uses Webots default by omitting --mode")
    parser.add_argument("--webots", default="/snap/bin/webots")
    parser.add_argument("--queue-size", type=int, default=2)
    parser.add_argument("--queue-put-timeout-s", type=float, default=30.0)
    parser.add_argument("--show-webots", action="store_true",
                        help="keep the Webots GUI open; omit --batch for the live run")
    parser.add_argument("--m2-m3-binary", type=Path, default=M2_M3_BINARY)
    parser.add_argument("--dude-binary", type=Path, default=DUDE_BINARY)
    args = parser.parse_args()
    report = run_live(args)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
