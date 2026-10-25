#!/usr/bin/env python3
"""Run the CPU Habitat frontier navigation milestone (mapping only)."""

from __future__ import annotations

import argparse
from collections import OrderedDict
import inspect
import json
import math
import os
import numpy as np
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECTS_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.handoff_paths import interpreter, ASSETS_ROOT
from exploration.controller import Controller, ControllerConfig
from exploration.contracts import ActionCommand, GridState
from exploration.depth_safety import DepthSafetyConfig, DepthSafetyGate
from exploration.frontier import FrontierExtractor
from exploration.graph_plugins import load_graph_plugin
from exploration.habitat_factory import create_habitat_bundle
from exploration.graph_bridge import GraphIntegrationWorker
from exploration.keyframe_policy import KeyframePolicy
from exploration.keyframe_scheduler import KeyframeScheduler
from exploration.keyframe_store import KeyframeStore
from exploration.live_dashboard import LiveStatePublisher
from exploration.nav_mapper import NavMapper, NavMapperConfig
from exploration.navmap_trace import NavMapTraceRecorder
from exploration.planner import AStarPlanner, PlannerConfig
from exploration.policy import FrontierPolicy, PolicyConfig
from exploration.p2_worker_client import P2WorkerClient
from exploration.run_artifacts import prepare_run_artifacts, write_run_summary
from exploration.runtime import NavigationRuntime, NavigationRuntimeConfig
from exploration.runtime_profile import (
    keyframe_runtime_settings, load_runtime_profile, m2_transport_metadata,
)
from exploration.resource_monitor import ProcessTreeResourceMonitor
from exploration.scan_audit import InitialScanAuditRecorder
from exploration.supervisor import EpisodeState, EpisodeSupervisor
from exploration.telemetry import EventJournal


def _close_graph_plugin(plugin, deadline_monotonic_s: float | None = None) -> None:
    if plugin is None:
        return
    close = plugin.close
    try:
        supports_deadline = "deadline_monotonic_s" in inspect.signature(close).parameters
    except (TypeError, ValueError):
        supports_deadline = False
    if supports_deadline:
        close(deadline_monotonic_s=deadline_monotonic_s)
    else:
        close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scene-config", type=Path,
        default=ROOT / "configs/exploration/hm3d_00802_cpu.json",
        help="hash-locked Habitat scene and spawn config",
    )
    parser.add_argument("--run-id", default=None, help="run identifier; defaults to a timestamped UUID")
    parser.add_argument("--output-root", type=Path, default=ROOT / "runs",
                        help="directory for immutable run artifacts")
    parser.add_argument("--profile", type=Path, default=ROOT / "configs/exploration/cpu_smooth.yaml",
                        help="validated YAML navigation profile")
    parser.add_argument("--budget-s", type=float, default=None,
                        help="wall-clock exploration budget (defaults to profile)")
    parser.add_argument("--navmap-trace-hz", type=float, default=0.0,
                        help="optional bounded NavMap trace rate; 0 disables it")
    parser.add_argument("--navmap-trace-radius-cells", type=int, default=180,
                        help="trace patch radius in cells (default captures the full current map)")
    parser.add_argument("--control-file", type=Path, default=None,
                        help="optional JSON file with command=run|pause|stop")
    parser.add_argument("--enable-p2", action="store_true",
                        help="run the local persistent YOLO11-seg worker")
    parser.add_argument("--enable-graph", action="store_true",
                        help="enable keyframes and the selected scene-graph plugin")
    parser.add_argument("--graph-plugin", default="exploration.demo_graph_plugin:create_plugin",
                        help="loadable graph provider in module:factory form")
    parser.add_argument("--p2-python", type=Path,
                        default=interpreter("perception"))
    parser.add_argument("--p2-legacy-root", type=Path,
                        default=PROJECTS_ROOT / "Fusion_Concept_OccuSG")
    parser.add_argument("--p2-weights", type=Path,
                        default=ASSETS_ROOT / "models/yolo11n-seg.onnx")
    parser.add_argument("--p2-profile", type=Path,
                        default=PROJECTS_ROOT / "Fusion_Concept_OccuSG/configs/perception/yolo11n_seg_replica_cad_dev.json")
    parser.add_argument("--p2-names", type=Path,
                        default=PROJECTS_ROOT / "Fusion_Concept_OccuSG/configs/perception/coco.names")
    parser.add_argument("--p2-intra-op-threads", type=int, default=2,
                        help="bounded ONNX Runtime worker threads per P2 operator")
    parser.add_argument("--p2-inter-op-threads", type=int, default=1,
                        help="bounded ONNX Runtime graph scheduling threads")
    parser.add_argument("--occusg-backprojection-stride", type=int, choices=(1, 2, 4), default=1,
                        help="OccuSG primary and auxiliary depth stride (1 keeps full resolution)")
    parser.add_argument("--occusg-primary-stride", type=int, choices=(1, 2, 4), default=None,
                        help="override only OccuSG primary RGB-D sampling stride")
    parser.add_argument("--occusg-auxiliary-stride", type=int, choices=(1, 2, 4), default=None,
                        help="override only OccuSG auxiliary depth sampling stride")
    return parser


def _control_command(path: Path | None) -> str:
    if path is None:
        return "run"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "run"
    except (OSError, json.JSONDecodeError):
        return "run"
    command = value.get("command", "run") if isinstance(value, dict) else "run"
    return command if command in {"run", "pause", "stop"} else "run"


def _runtime(bundle, profile: dict) -> NavigationRuntime:
    scene = bundle.scene_config
    nav = profile["navigation"]
    agent = scene["agent"]
    radius = float(agent["navmesh_baked_radius_m"])
    height = float(agent["navmesh_baked_height_m"])
    # The depth mapper and local safety gate must share the scene's calibrated
    # observed floor reference; the navmesh spawn height can differ from the
    # rendered floor surface (e.g. thick HM3D floor slabs).
    floor_z = float(bundle.producer.floor_z_m)
    mapper = NavMapper(
        NavMapperConfig(resolution_m=float(nav["grid_resolution_m"]),
                        depth_stride=int(nav["depth_stride"]),
                        max_cells=2_000_000, agent_height_m=height),
        agent_radius_m=radius,
    )
    planner = AStarPlanner(PlannerConfig(
        agent_radius_m=radius, inflation_margin_m=float(nav["inflation_margin_m"])))
    frontiers = FrontierExtractor(
        min_frontier_length_m=float(nav["min_frontier_length_m"]), agent_radius_m=radius,
        inflation_margin_m=float(nav["inflation_margin_m"]),
        min_viewpoint_offset_m=float(nav["min_viewpoint_offset_m"]),
        max_viewpoint_offset_m=float(nav["max_viewpoint_offset_m"]),
        max_viewpoints_per_frontier=int(nav.get("max_viewpoints_per_frontier", 1)),
        min_viewpoint_separation_m=float(nav.get("min_viewpoint_separation_m", 0.35)),
    )
    policy = FrontierPolicy(planner, PolicyConfig(
        goal_hold_s=float(nav["goal_hold_s"]),
        switch_improvement=float(nav["goal_switch_improvement"]),
        visited_viewpoint_radius_m=float(nav.get("visited_viewpoint_radius_m", 0.0)),
        failed_viewpoint_radius_m=float(nav.get("failed_viewpoint_radius_m", 0.0)),
        # Count arrival when the robot footprint reaches the quantized target cell.
        reached_distance_m=max(radius, float(nav["grid_resolution_m"]) / math.sqrt(2.0))))
    controller = Controller(ControllerConfig(
        max_speed_mps=float(nav["max_speed_mps"]),
        nominal_speed_mps=float(nav["nominal_speed_mps"]),
        max_yaw_rate_radps=math.radians(float(nav["max_yaw_degps"])),
        acceleration_mps2=float(nav["acceleration_mps2"]),
        yaw_acceleration_radps2=math.radians(float(nav["yaw_acceleration_degps2"])),
        lookahead_m=float(nav["lookahead_m"]),
        max_depth_age_s=float(nav["depth_stale_s"]),
        max_nav_age_s=float(nav["nav_stale_s"]),
        stop_distance_m=float(nav["stop_distance_m"]),
        goal_reached_distance_m=max(radius, float(nav["grid_resolution_m"]) / math.sqrt(2.0)),
        max_dt_s=0.10,
    ))
    safety = DepthSafetyGate(DepthSafetyConfig(
        corridor_half_width_m=radius + float(nav["inflation_margin_m"]), floor_z_m=floor_z,
        agent_height_m=height, max_check_distance_m=1.0,
    ))
    return NavigationRuntime(
        mapper=mapper, frontier_extractor=frontiers, planner=planner,
        policy=policy, controller=controller, depth_safety=safety,
        config=NavigationRuntimeConfig(map_update_hz=float(nav["map_update_hz"]),
                                      planner_hz=float(nav["planner_hz"]),
                                      progress_timeout_s=float(nav.get("progress_timeout_s", 8.0)),
                                      progress_min_distance_m=float(nav.get("progress_min_distance_m", 0.05)),
                                      obstacle_block_timeout_s=float(nav.get("obstacle_block_timeout_s", 1.0)),
                                      initial_scan_yaw_rad=math.radians(float(nav["initial_scan_yaw_deg"])),
                                      initial_scan_rate_radps=math.radians(float(nav["initial_scan_rate_degps"])),
                                      collision_recovery_yaw_rad=math.radians(
                                          float(nav["collision_recovery_yaw_deg"])),
                                      async_mapping=bool(nav["async_mapping"]),
                                      async_planning=bool(nav["async_planning"]),
                                      # Habitat's loaded, radius-eroded NavMesh and
                                      # collision-filtered steps certify the live pose.
                                      allow_validated_start_anchor=True),
    )


def main() -> int:
    args = _parser().parse_args()
    if not 1 <= args.p2_intra_op_threads <= 8:
        raise SystemExit("--p2-intra-op-threads must be in [1, 8]")
    if not 1 <= args.p2_inter_op_threads <= 4:
        raise SystemExit("--p2-inter-op-threads must be in [1, 4]")
    os.environ["THREEDSG_OCCUSG_BACKPROJECTION_STRIDE"] = str(args.occusg_backprojection_stride)
    primary_stride = (args.occusg_backprojection_stride if args.occusg_primary_stride is None
                      else args.occusg_primary_stride)
    auxiliary_stride = (args.occusg_backprojection_stride if args.occusg_auxiliary_stride is None
                        else args.occusg_auxiliary_stride)
    os.environ["THREEDSG_OCCUSG_PRIMARY_BACKPROJECTION_STRIDE"] = str(primary_stride)
    os.environ["THREEDSG_OCCUSG_AUXILIARY_BACKPROJECTION_STRIDE"] = str(auxiliary_stride)
    profile = load_runtime_profile(args.profile)
    budget_s = float(profile["runtime"]["exploration_wall_budget_s"]
                     if args.budget_s is None else args.budget_s)
    if not math.isfinite(budget_s) or not 0 < budget_s <= 600.0:
        raise SystemExit("--budget-s must be finite and in (0, 600]")
    if not math.isfinite(args.navmap_trace_hz) or not 0 <= args.navmap_trace_hz <= 10.0:
        raise SystemExit("--navmap-trace-hz must be finite and in [0, 10]")
    if not 1 <= args.navmap_trace_radius_cells <= 512:
        raise SystemExit("--navmap-trace-radius-cells must be in [1, 512]")
    run_id = args.run_id or f"frontier-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_root, manifest = prepare_run_artifacts(
        output_root=args.output_root, run_id=run_id, scene_config_path=args.scene_config,
        exploration_budget_s=budget_s,
        shutdown_timeout_s=float(profile["runtime"]["shutdown_timeout_s"]),
        navigation_profile=profile, navigation_profile_path=args.profile,
        runtime_components={
            "graph_enabled": bool(args.enable_graph or args.enable_p2),
            "graph_plugin_factory": args.graph_plugin
            if args.enable_graph or args.enable_p2 else None,
            "p2_enabled": bool(args.enable_p2),
            "p2_python": str(args.p2_python) if args.enable_p2 else None,
            "p2_model": str(args.p2_weights) if args.enable_p2 else None,
            "p2_profile": str(args.p2_profile) if args.enable_p2 else None,
            "p2_intra_op_threads": args.p2_intra_op_threads if args.enable_p2 else None,
            "p2_inter_op_threads": args.p2_inter_op_threads if args.enable_p2 else None,
            "occusg_backprojection_stride": (args.occusg_backprojection_stride
                                               if args.enable_graph or args.enable_p2 else None),
            "occusg_primary_stride": primary_stride if args.enable_graph or args.enable_p2 else None,
            "occusg_auxiliary_stride": auxiliary_stride if args.enable_graph or args.enable_p2 else None,
            # Worker selection is only intent; actual mode is recorded from
            # each OccuSG mapping record after a successful frame integration.
            **m2_transport_metadata(
                enabled=bool(args.enable_graph or args.enable_p2),
                requested=os.environ.get("OCCUSG_M2_TRANSPORT", "ascii"),
            ),
        })
    resources = profile["resources"]
    resource_monitor = ProcessTreeResourceMonitor(
        run_root / "diagnostics/resource_samples.jsonl",
        process_tree_rss_cap_gib=float(resources["target_process_tree_rss_gib"]),
        warning_mem_available_gib=float(resources["warning_mem_available_gib"]),
        stop_mem_available_gib=float(resources["stop_mem_available_gib"]),
        stop_mem_grace_s=float(resources["stop_mem_grace_s"]),
        sample_period_s=float(resources["sample_period_s"]),
    )
    resource_monitor.start()
    resource_stats: dict | None = None
    shutdown_timeout_s = float(profile["runtime"]["shutdown_timeout_s"])
    supervisor = EpisodeSupervisor(exploration_budget_s=budget_s,
                                   shutdown_timeout_s=shutdown_timeout_s)
    run_error: Exception | None = None
    stop_action_ok = True
    summary_written = False
    live_publisher: LiveStatePublisher | None = None
    scan_audit: InitialScanAuditRecorder | None = None
    navmap_trace: NavMapTraceRecorder | None = None
    scan_audit_active = True
    scan_audit_packets: OrderedDict[str, object] = OrderedDict()
    keyframe_store: KeyframeStore | None = None
    keyframe_scheduler: KeyframeScheduler | None = None
    graph_worker: GraphIntegrationWorker | None = None
    p2_client: P2WorkerClient | None = None
    runtime: NavigationRuntime | None = None
    runtime_workers_stopped = False
    shutdown_deadline: float | None = None
    graph_plugin = None
    frame_count = 0
    collision_count = 0
    elapsed = 0.0
    with EventJournal(run_root, run_id=run_id, fsync_each_event=False) as journal:
        journal.emit("run_created", {"effective_config_sha256": manifest["effective_config_sha256"]})
        journal.emit("resource_monitor_started", {
            "resource_monitor": resource_monitor.snapshot(),
        })
        try:
            with create_habitat_bundle(args.scene_config, run_id=run_id) as bundle:
                runtime = _runtime(bundle, profile)
                live_publisher = LiveStatePublisher(run_root / "live", run_id=run_id)
                scan_audit = InitialScanAuditRecorder(
                    run_root / "diagnostics/initial_scan", run_id=run_id)
                if args.navmap_trace_hz > 0:
                    navmap_trace = NavMapTraceRecorder(
                        run_root / "diagnostics/navmap_trace.npz",
                        sample_hz=args.navmap_trace_hz,
                        radius_cells=args.navmap_trace_radius_cells,
                    )
                keyframe_policy: KeyframePolicy | None = None
                if args.enable_p2:
                    p2_client = P2WorkerClient(
                        python=args.p2_python,
                        worker_script=ROOT / "scripts/p2_onnx_worker.py",
                        legacy_root=args.p2_legacy_root,
                        weights=args.p2_weights,
                        profile=args.p2_profile,
                        names=args.p2_names,
                        output_root=run_root / "live/perception",
                        stderr_path=run_root / "live/p2_worker_stderr.log",
                        intra_op_threads=args.p2_intra_op_threads,
                        inter_op_threads=args.p2_inter_op_threads,
                    )
                    journal.emit("perception_worker_ready", {
                        key: value for key, value in p2_client.provenance.items()
                        if key != "event"
                    })
                if args.enable_graph or args.enable_p2:
                    keyframe_settings = keyframe_runtime_settings(profile)
                    keyframe_store = KeyframeStore(run_root, byte_cap=2 * 1024**3)
                    keyframe_scheduler = KeyframeScheduler(
                        max_pending=int(keyframe_settings["max_pending"]),
                        max_protected_pending=int(keyframe_settings["max_protected_pending"]),
                        raw_pending_byte_cap=int(keyframe_settings["raw_pending_byte_cap"]),
                    )
                    graph_plugin = load_graph_plugin(
                        args.graph_plugin, p2_client=p2_client,
                        live_publisher=live_publisher,
                        event_sink=lambda event, fields: journal.emit(event, fields),
                    )
                    journal.emit("graph_plugin_loaded", {
                        "factory": args.graph_plugin,
                        "provider_id": getattr(graph_plugin, "provider_id",
                                                getattr(graph_plugin, "backend_name", "unknown")),
                        "provider_capabilities": getattr(
                            getattr(graph_plugin, "capabilities", None), "to_dict", lambda: None)(),
                        "perception_worker": p2_client is not None,
                    })

                    generic_provider = graph_plugin if callable(
                        getattr(graph_plugin, "update", None)) else None
                    perceive = getattr(graph_plugin, "perceive", None) if generic_provider is None else None
                    graph_worker = GraphIntegrationWorker(
                        scheduler=keyframe_scheduler, store=keyframe_store,
                        provider=generic_provider,
                        conceptgraphs_session=getattr(graph_plugin, "object_session", None),
                        occusg_session=getattr(graph_plugin, "navigation_session", None),
                        conceptgraphs_packet=getattr(graph_plugin, "object_packet", None),
                        occusg_packet=getattr(graph_plugin, "navigation_packet", None),
                        perceive=perceive,
                        update_pair=getattr(graph_plugin, "update_pair", None),
                        publish=graph_plugin.publish,
                    )
                    graph_worker.start()
                    keyframe_policy = KeyframePolicy(
                        min_translation_m=float(keyframe_settings["min_translation_m"]),
                        min_yaw_rad=math.radians(float(keyframe_settings["min_yaw_deg"])))
                simulation_step_s = float(bundle.scene_config["simulation"]["step_s"])
                target_capture_interval_s = 1.0 / float(profile["camera"]["target_fps"])
                packet_step = bundle.producer.observe()
                supervisor.mark_ready()
                journal.emit("episode_ready", {
                    "scene_id": bundle.scene_config["scene_id"],
                    "initial_frame_id": packet_step.packet.frame_id,
                })
                supervisor.start()
                start = time.monotonic()
                next_capture_at = start
                last_status_at = start
                previous_frame_host_s = packet_step.packet.host_monotonic_s
                journal.emit("episode_started", {"exploration_budget_s": budget_s})
                print(f"READY run_id={run_id} scene={bundle.scene_config['scene_id']} budget_s={budget_s:.1f}",
                      flush=True)
                last_tick_host_s = start
                last_control_command = "run"
                try:
                    while supervisor.snapshot().motion_allowed:
                        resource_stop = resource_monitor.stop_reason
                        if resource_stop is not None:
                            supervisor.request_stop(f"resource_budget:{resource_stop}")
                            journal.emit("resource_budget_stop", {
                                "reason": resource_stop,
                                "resource_snapshot": resource_monitor.snapshot(),
                            })
                            break
                        control_command = _control_command(args.control_file)
                        if control_command == "stop":
                            supervisor.request_stop("desktop_user_stop")
                            journal.emit("stop_requested", {"reason": "desktop_user_stop"})
                            break
                        paused = control_command == "pause"
                        if paused != (last_control_command == "pause"):
                            runtime.controller.reset()
                            journal.emit("control_state_changed", {"state": control_command})
                        last_control_command = control_command
                        tick_host_s = time.monotonic()
                        tick = runtime.tick(packet_step.packet, packet_step.robot_pose,
                                            dt_s=max(0.0, tick_host_s - last_tick_host_s),
                                            now_monotonic_s=tick_host_s,
                                            collided=packet_step.collided,
                                            collision_position_world_xy=packet_step.collision_position_world_xy,
                                            collision_filter_details=packet_step.collision_filter_details)
                        command = tick.command
                        if paused:
                            command = ActionCommand(
                                0.0, 0.0, "desktop_paused", packet_step.packet.frame_id,
                                tick.snapshot.revision, tick_host_s + 0.2,
                            )
                        if navmap_trace is not None:
                            navmap_trace.record(
                                snapshot=tick.snapshot, robot_pose=packet_step.robot_pose,
                                elapsed_s=max(0.0, tick_host_s - start),
                                decision=tick.decision, command=command,
                                frontier_set=tick.frontiers,
                                map_worker_pending=tick.map_worker_pending,
                                planner_pending=tick.planner_pending,
                            )
                        audit_candidate = None
                        if scan_audit is not None and scan_audit_active:
                            if tick.command.reason == "initial_scan":
                                scan_audit_packets[packet_step.packet.frame_id] = packet_step
                                while len(scan_audit_packets) > 8:
                                    dropped_id, _ = scan_audit_packets.popitem(last=False)
                                    journal.emit("initial_scan_audit_frame_evicted", {
                                        "frame_id": dropped_id, "reason": "pending_cache_limit",
                                    })
                            audit_candidate = scan_audit_packets.pop(
                                tick.snapshot.as_of_frame_id, None)
                        if (keyframe_policy is not None and keyframe_store is not None
                                and keyframe_scheduler is not None):
                            frontier_rows = [] if tick.frontiers is None else [
                                {"id": item.frontier_id,
                                 "xy": list(item.viewpoint_world_xy or
                                            tick.snapshot.cell_to_world(*item.viewpoint_cell)),
                                 "gain_cells": item.predicted_gain_cells}
                                for item in tick.frontiers.frontiers
                            ]
                            should_admit, keyframe_reason, novelty = keyframe_policy.propose(
                                packet_step.robot_pose, tick.decision,
                                {row["id"] for row in frontier_rows},
                            )
                            if should_admit:
                                goal_xy = None
                                if tick.decision is not None and tick.decision.frontier is not None:
                                    goal_xy = list(
                                        tick.decision.frontier.viewpoint_world_xy or
                                        tick.snapshot.cell_to_world(
                                            *tick.decision.frontier.viewpoint_cell))
                                try:
                                    keyframe = keyframe_store.persist(
                                        packet_step.rgb, packet_step.packet,
                                        novelty=novelty,
                                        protected_reason=(None if keyframe_reason == "novel_view"
                                                          else keyframe_reason),
                                        extra_metadata={
                                            "trigger": keyframe_reason,
                                            "robot_pose": [packet_step.robot_pose.x_m,
                                                           packet_step.robot_pose.y_m,
                                                           packet_step.robot_pose.yaw_rad],
                                            "floor_z_m": packet_step.packet.floor_z_m,
                                            "nav_revision": tick.snapshot.revision,
                                            "frontiers": frontier_rows,
                                            "goal_xy": goal_xy,
                                        },
                                    )
                                    admission = keyframe_scheduler.offer(keyframe)
                                    if admission.accepted:
                                        keyframe_policy.commit(
                                            packet_step.robot_pose,
                                            {row["id"] for row in frontier_rows},
                                        )
                                    journal.emit("graph_keyframe_admission", {
                                        "frame_id": keyframe.frame_id,
                                        "frame_index": keyframe.frame_index,
                                        "trigger": keyframe_reason,
                                        "novelty": novelty,
                                        "accepted": admission.accepted,
                                        "reason": admission.reason,
                                        "queue_depth": admission.queue_depth,
                                        "queued_bytes": admission.queued_bytes,
                                        "replaced_frame_ids": list(admission.replaced_frame_ids),
                                    })
                                except Exception as exc:
                                    journal.emit("graph_keyframe_persist_failed", {
                                        "frame_id": packet_step.packet.frame_id,
                                        "frame_index": packet_step.packet.frame_index,
                                        "error": f"{type(exc).__name__}: {exc}",
                                    })
                        live_publisher.publish_tick(
                            rgb=packet_step.rgb, snapshot=tick.snapshot,
                            robot_pose=packet_step.robot_pose, frontiers=tick.frontiers,
                            decision=tick.decision,
                            command={"linear_mps": command.linear_mps,
                                     "yaw_rate_radps": command.yaw_rate_radps,
                                     "reason": command.reason},
                            depth_m=packet_step.packet.depth_m,
                            intrinsics={"fx": packet_step.packet.intrinsics.fx,
                                        "fy": packet_step.packet.intrinsics.fy,
                                        "cx": packet_step.packet.intrinsics.cx,
                                        "cy": packet_step.packet.intrinsics.cy,
                                        "width": packet_step.packet.intrinsics.width,
                                        "height": packet_step.packet.intrinsics.height},
                            T_world_camera=packet_step.packet.T_world_camera.matrix,
                            elapsed_s=max(0.0, time.monotonic() - start),
                            runtime_status="PAUSED" if paused else "RUNNING",
                        )
                        last_tick_host_s = tick_host_s
                        frame_count += 1

                        # Keep a monotonic rate target but drop catch-up slots
                        # after overruns instead of issuing a burst of ticks.
                        next_capture_at += target_capture_interval_s
                        delay_s = next_capture_at - time.monotonic()
                        if delay_s > 0:
                            time.sleep(delay_s)
                        if not supervisor.snapshot().motion_allowed:
                            supervisor.check_deadline()
                            break
                        prior_frame = packet_step
                        packet_step = bundle.producer.step(command)
                        collision_count += int(packet_step.collided)
                        if (scan_audit is not None and scan_audit_active
                                and audit_candidate is not None):
                            try:
                                sample = scan_audit.record(
                                    rgb=audit_candidate.rgb, packet=audit_candidate.packet,
                                    robot_pose=audit_candidate.robot_pose,
                                    snapshot=tick.snapshot,
                                    elapsed_s=max(0.0, tick_host_s - start),
                                )
                                if sample is not None:
                                    journal.emit("initial_scan_audit_frame_saved", {
                                        "frame_id": sample["frame_id"],
                                        "frame_index": sample["frame_index"],
                                        "yaw_bin": sample["yaw_bin"],
                                        "nav_revision": sample["nav_revision"],
                                        "array_bytes": sample["array_bytes"],
                                    })
                            except Exception as exc:
                                journal.emit("initial_scan_audit_error", {
                                    "exception_type": type(exc).__name__,
                                    "message": str(exc),
                                })
                                scan_audit_active = False
                        decision = tick.decision
                        observed_capture_interval_s = (prior_frame.packet.host_monotonic_s
                                                      - previous_frame_host_s)
                        previous_frame_host_s = prior_frame.packet.host_monotonic_s
                        journal.emit("navigation_tick", {
                            "frame_id": prior_frame.packet.frame_id,
                            "frame_index": prior_frame.packet.frame_index,
                            "capture_host_monotonic_s": prior_frame.packet.host_monotonic_s,
                            "capture_interval_s": observed_capture_interval_s,
                            "capture_jitter_s": observed_capture_interval_s - target_capture_interval_s,
                            "robot_pose_xy_yaw": [prior_frame.robot_pose.x_m,
                                                   prior_frame.robot_pose.y_m,
                                                   prior_frame.robot_pose.yaw_rad],
                            "map_revision": tick.snapshot.revision,
                            "map_as_of_frame_id": tick.snapshot.as_of_frame_id,
                            "map_cell_counts": {
                                "free": int((tick.snapshot.states == GridState.FREE).sum()),
                                "occupied": int((tick.snapshot.states == GridState.OCCUPIED).sum()),
                                "unknown": int((tick.snapshot.states == GridState.UNKNOWN).sum()),
                            },
                            "frontier_count": 0 if tick.frontiers is None else len(tick.frontiers.frontiers),
                            "frontier_diagnostics": None if tick.frontiers is None else {
                                "reason": tick.frontiers.reason,
                                "known_free_cells": tick.frontiers.known_free_cells,
                                "connected_known_free_cells": tick.frontiers.connected_known_free_cells,
                                "reachable_free_cells": tick.frontiers.reachable_free_cells,
                                "raw_frontier_cells": tick.frontiers.raw_frontier_cells,
                                "raw_component_count": tick.frontiers.raw_component_count,
                                "length_qualified_components": tick.frontiers.length_qualified_components,
                                "components_with_viewpoint": tick.frontiers.components_with_viewpoint,
                            },
                            "decision_state": "WAIT" if decision is None else decision.state,
                            "decision_reason": "none" if decision is None else decision.reason,
                            "goal_diagnostics": {} if decision is None else decision.goal_diagnostics,
                            "decision_path_failure_reasons": [] if decision is None
                            else list(decision.path_failure_reasons),
                            "path_cost_m": None if decision is None or decision.path is None
                            or not decision.path.success else decision.path.cost_m,
                            "goal_id": None if decision is None or decision.frontier is None
                            else decision.frontier.frontier_id,
                            "goal_viewpoint_cell": None
                            if decision is None or decision.frontier is None
                            else list(decision.frontier.viewpoint_cell),
                            "goal_viewpoint_xy": None
                            if decision is None or decision.frontier is None else list(
                                decision.frontier.viewpoint_world_xy
                                or tick.snapshot.cell_to_world(*decision.frontier.viewpoint_cell)
                            ),
                            "frontier_map_revision": None if tick.frontiers is None
                            else tick.frontiers.map_revision,
                            "frontier_as_of_frame_id": None if tick.frontiers is None
                            else tick.frontiers.as_of_frame_id,
                            "command": {
                                "linear_mps": command.linear_mps,
                                "yaw_rate_radps": command.yaw_rate_radps,
                                "reason": command.reason,
                                "valid_until_monotonic_s": command.valid_until_monotonic_s,
                                "nav_age_s": command.metadata.get("nav_age_s"),
                                "depth_age_s": command.metadata.get("depth_age_s"),
                            },
                            "depth_gate": tick.depth_safety.status,
                            "action_collision_reported": packet_step.collided,
                            "collision_position_world_xy": packet_step.collision_position_world_xy,
                            "collision_filter_details": packet_step.collision_filter_details,
                            "blocked_step_edge": tick.blocked_step_edge,
                            "active_path_next_edge": tick.active_path_next_edge,
                            "blocked_step_matches_path": tick.blocked_step_matches_path,
                            "active_blocked_edge_count": tick.active_blocked_edge_count,
                            "navmesh_projection_details": packet_step.navmesh_projection_details,
                            "executed_action": packet_step.executed_action,
                            "input_age_s": tick.input_age_s,
                            "stage_latency_s": {
                                "map": tick.map_duration_s,
                                "map_worker": tick.map_worker_duration_s,
                                "map_worker_cpu": tick.map_worker_cpu_duration_s,
                                "map_worker_queue_wait": tick.map_worker_queue_wait_s,
                                "map_worker_submit_to_publish": tick.map_worker_submit_to_publish_s,
                                "depth_safety": tick.safety_duration_s,
                                "frontier_planning": tick.planning_duration_s,
                                "frontier_planning_worker": tick.planner_worker_duration_s,
                                "control": tick.control_duration_s,
                                "total_tick": tick.total_duration_s,
                            },
                            "map_update_profile_s": tick.map_update_profile_s,
                            "planner_pending": tick.planner_pending,
                            "planner_result_stale": tick.planner_result_stale,
                            "planner_result_rebased": tick.planner_result_rebased,
                            "planner_result_revision": tick.planner_result_revision,
                            "planner_result_frame_id": tick.planner_result_frame_id,
                            "planner_worker_error": tick.planner_worker_error,
                            "path_revalidation_failure_reason": tick.path_revalidation_failure_reason,
                            "map_worker_pending": tick.map_worker_pending,
                            "map_dropped_frames": tick.map_dropped_frames,
                            "map_worker_error": tick.map_worker_error,
                        })

                        now = time.monotonic()
                        if now > next_capture_at:
                            next_capture_at = now
                        if now - last_status_at >= 2.0:
                            state = "WAIT" if decision is None else decision.state
                            goal = "none" if decision is None or decision.frontier is None else decision.frontier.frontier_id
                            print(
                                f"PROGRESS elapsed_s={now-start:.1f} frames={frame_count} "
                                f"map_revision={tick.snapshot.revision} frontiers="
                                f"{0 if tick.frontiers is None else len(tick.frontiers.frontiers)} "
                                f"state={state} goal={goal} action={tick.command.reason} "
                                f"depth_gate={tick.depth_safety.status}", flush=True)
                            last_status_at = now
                    supervisor.check_deadline()
                    if supervisor.snapshot().state is EpisodeState.RUNNING:
                        supervisor.request_stop("wall_budget_elapsed")
                    flush_remaining = supervisor.snapshot().flush_remaining_s
                    shutdown_deadline = time.monotonic() + (
                        shutdown_timeout_s if flush_remaining is None else flush_remaining)

                    def shutdown_time_left() -> float:
                        return max(0.0, shutdown_deadline - time.monotonic())

                    def bounded_shutdown_timeout(maximum_s: float) -> float:
                        return max(0.0, min(maximum_s, shutdown_time_left()))

                    runtime_workers_stopped = runtime.close(
                        timeout_s=bounded_shutdown_timeout(5.0),
                        deadline_monotonic_s=shutdown_deadline)
                    journal.emit("runtime_workers_shutdown", {
                        "stopped": runtime_workers_stopped,
                        "status": runtime.worker_status(),
                    })
                    final_map = runtime.map_snapshot()
                    np.savez_compressed(
                        run_root / "navmap_final.npz",
                        states=final_map.states,
                        occupancy_log_odds=final_map.occupancy_log_odds,
                        floor_support=final_map.floor_support,
                        clearance_evidence=final_map.clearance_evidence,
                        spawn_validated=final_map.spawn_validated,
                        resolution_m=np.float64(final_map.resolution_m),
                        origin_x_m=np.float64(final_map.origin_x_m),
                        origin_y_m=np.float64(final_map.origin_y_m),
                        spawn_center_xy=np.asarray(final_map.spawn_center_xy or (np.nan, np.nan)),
                    )
                    journal.emit("navmap_snapshot_saved", {
                        "file": "navmap_final.npz", "revision": final_map.revision,
                        "shape": list(final_map.states.shape),
                    })
                except KeyboardInterrupt:
                    supervisor.request_stop("keyboard_interrupt")
                    journal.emit("stop_requested", {"reason": "keyboard_interrupt"})
                    print("STOP reason=keyboard_interrupt", flush=True)
                except Exception as exc:
                    run_error = exc
                    supervisor.request_stop("runtime_error")
                    journal.emit("runtime_error", {
                        "exception_type": type(exc).__name__, "message": str(exc),
                    })
                finally:
                    supervisor.request_stop("launcher_exit")
                    if shutdown_deadline is None:
                        flush_remaining = supervisor.snapshot().flush_remaining_s
                        shutdown_deadline = time.monotonic() + (
                            shutdown_timeout_s if flush_remaining is None else flush_remaining)

                        def shutdown_time_left() -> float:
                            return max(0.0, shutdown_deadline - time.monotonic())

                        def bounded_shutdown_timeout(maximum_s: float) -> float:
                            return max(0.0, min(maximum_s, shutdown_time_left()))

                    if not runtime_workers_stopped:
                        runtime_workers_stopped = runtime.close(
                            timeout_s=bounded_shutdown_timeout(5.0),
                            deadline_monotonic_s=shutdown_deadline)
                        journal.emit("runtime_workers_shutdown", {
                            "stopped": runtime_workers_stopped,
                            "status": runtime.worker_status(),
                        })
                    # No perception worker is connected yet, so there is no
                    # queue to drain; advance one stop-only simulator step.
                    stop_now = time.monotonic()
                    try:
                        if shutdown_time_left() <= 0.02:
                            raise TimeoutError("shutdown budget exhausted before stop action")
                        stopped = bundle.producer.step(ActionCommand(
                            0.0, 0.0, "episode_shutdown_stop", packet_step.packet.frame_id,
                            -1, min(stop_now + 1.0, shutdown_deadline)))
                        journal.emit("shutdown_stop_action", {
                            "frame_id": stopped.packet.frame_id,
                            "collision_reported": stopped.collided,
                        })
                    except Exception as exc:
                        stop_action_ok = False
                        journal.emit("shutdown_stop_error", {
                            "exception_type": type(exc).__name__, "message": str(exc),
                        })
                    graph_worker_ok = True
                    if graph_worker is not None:
                        try:
                            dropped_graph_frames = graph_worker.close(
                                timeout_s=bounded_shutdown_timeout(shutdown_timeout_s),
                                deadline_monotonic_s=shutdown_deadline,
                                drain_pending=False)
                            graph_worker_ok = graph_worker.failure is None
                            if keyframe_scheduler is not None:
                                journal.emit("graph_worker_shutdown", {
                                    "scheduler": keyframe_scheduler.snapshot(),
                                    "pending_keyframes_preserved_for_replay": list(dropped_graph_frames),
                                    "failure": None if graph_worker.failure is None
                                    else f"{type(graph_worker.failure).__name__}: {graph_worker.failure}",
                                })
                        except Exception as exc:
                            graph_worker_ok = False
                            journal.emit("graph_worker_shutdown_error", {
                                "exception_type": type(exc).__name__, "message": str(exc),
                            })
                    if graph_plugin is not None:
                        try:
                            _close_graph_plugin(graph_plugin, shutdown_deadline)
                        except Exception as exc:
                            graph_worker_ok = False
                            journal.emit("graph_plugin_close_error", {
                                "exception_type": type(exc).__name__, "message": str(exc),
                            })
                    if p2_client is not None:
                        try:
                            p2_client.close(timeout_s=bounded_shutdown_timeout(5.0),
                                            deadline_monotonic_s=shutdown_deadline)
                        except Exception as exc:
                            graph_worker_ok = False
                            journal.emit("perception_worker_close_error", {
                                "exception_type": type(exc).__name__, "message": str(exc),
                            })
                    resource_stats = resource_monitor.close(
                        timeout_s=bounded_shutdown_timeout(3.0))
                    journal.emit("resource_monitor_stopped", {
                        "resource_monitor": resource_stats,
                    })
                    journal.emit("episode_stopping", {
                        "reason": supervisor.snapshot().reason,
                        "flush_timeout_s": shutdown_timeout_s,
                    })
                    shutdown_ok = stop_action_ok and runtime_workers_stopped and graph_worker_ok
                    supervisor.finalize(
                        success=run_error is None and shutdown_ok,
                        reason=(None if run_error is None and shutdown_ok else
                                "runtime_error" if run_error is not None else "shutdown_incomplete"),
                    )
                    final_state = supervisor.snapshot()
                    elapsed = final_state.elapsed_exploration_s
                    journal.emit("episode_finished", {
                        "state": final_state.state.value,
                        "reason": final_state.reason,
                        "elapsed_exploration_s": elapsed,
                        "frame_count": frame_count,
                    })
                    write_run_summary(run_root, {
                        "run_id": run_id,
                        "state": final_state.state.value,
                        "stop_reason": final_state.reason,
                        "elapsed_exploration_s": elapsed,
                        "frame_count": frame_count,
                        "collision_or_filter_block_count": collision_count,
                        "initial_scan_audit_manifest": "diagnostics/initial_scan/manifest.json",
                        "initial_scan_audit_samples": 0 if scan_audit is None
                        else scan_audit.sample_count,
                        "navmap_trace_path": "diagnostics/navmap_trace.npz"
                        if navmap_trace is not None else None,
                        "navmap_trace_samples": 0 if navmap_trace is None
                        else navmap_trace.sample_count,
                        "stop_action_succeeded": stop_action_ok,
                        "graph_worker_connected": graph_worker is not None,
                        "graph_worker_ok": graph_worker_ok,
                        "graph_backend": None if graph_plugin is None else getattr(
                            graph_plugin, "provider_id", getattr(graph_plugin, "backend_name", "unknown")),
                        "native_conceptgraphs_connected": bool(
                            graph_plugin is not None
                            and "conceptgraphs" in str(getattr(graph_plugin, "provider_id", ""))),
                        "native_occusg_connected": bool(
                            graph_plugin is not None
                            and "occusg" in str(getattr(graph_plugin, "provider_id", ""))),
                        "legacy_conceptgraphs_connected": False,
                        "legacy_occusg_connected": False,
                        "resource_monitor": resource_stats,
                    })
                    if live_publisher is not None:
                        live_publisher.close(status=final_state.state.value)
                    if scan_audit is not None:
                        scan_audit.close(status=final_state.state.value)
                    if navmap_trace is not None:
                        navmap_trace.close(status=final_state.state.value)
                    summary_written = True
                    journal.close()
                print(f"STOP reason={supervisor.snapshot().reason} elapsed_s={elapsed:.2f} "
                      f"frames={frame_count}", flush=True)
        except Exception as exc:
            # Setup can fail after one or more optional graph processes start,
            # before the episode loop's normal finally block owns shutdown.
            supervisor.request_stop("setup_or_runtime_error")
            flush_remaining = supervisor.snapshot().flush_remaining_s
            if shutdown_deadline is None:
                shutdown_deadline = time.monotonic() + (
                    shutdown_timeout_s if flush_remaining is None else flush_remaining)

            def exception_shutdown_timeout(maximum_s: float) -> float:
                return max(0.0, min(maximum_s, shutdown_deadline - time.monotonic()))

            if runtime is not None:
                try:
                    if not runtime_workers_stopped:
                        runtime_workers_stopped = runtime.close(
                            timeout_s=exception_shutdown_timeout(5.0),
                            deadline_monotonic_s=shutdown_deadline)
                        journal.emit("runtime_workers_shutdown", {
                            "stopped": runtime_workers_stopped,
                            "status": runtime.worker_status(),
                        })
                except Exception:
                    pass
            if graph_worker is not None:
                try:
                    graph_worker.close(
                        timeout_s=exception_shutdown_timeout(shutdown_timeout_s),
                        deadline_monotonic_s=shutdown_deadline)
                except Exception:
                    pass
            if graph_plugin is not None:
                try:
                    _close_graph_plugin(graph_plugin, shutdown_deadline)
                except Exception:
                    pass
            if p2_client is not None:
                try:
                    p2_client.close(timeout_s=exception_shutdown_timeout(5.0),
                                    deadline_monotonic_s=shutdown_deadline)
                except Exception:
                    pass
            if live_publisher is not None:
                try:
                    live_publisher.close(status=EpisodeState.FAILED.value)
                except Exception:
                    pass
            if scan_audit is not None:
                try:
                    scan_audit.close(status=EpisodeState.FAILED.value)
                except Exception:
                    pass
            if navmap_trace is not None:
                try:
                    navmap_trace.close(status=EpisodeState.FAILED.value)
                except Exception:
                    pass
            if not summary_written:
                if resource_stats is None:
                    resource_stats = resource_monitor.close()
                journal.emit("run_failed", {"exception_type": type(exc).__name__, "message": str(exc)})
                write_run_summary(run_root, {
                    "run_id": run_id,
                    "state": EpisodeState.FAILED.value,
                    "stop_reason": "startup_failure" if supervisor.snapshot().state is EpisodeState.WARMUP
                    else "run_failure_before_finalization",
                    "elapsed_exploration_s": supervisor.snapshot().elapsed_exploration_s,
                    "frame_count": frame_count,
                    "collision_or_filter_block_count": collision_count,
                    "initial_scan_audit_manifest": "diagnostics/initial_scan/manifest.json",
                    "initial_scan_audit_samples": 0 if scan_audit is None
                    else scan_audit.sample_count,
                    "navmap_trace_path": "diagnostics/navmap_trace.npz"
                    if navmap_trace is not None else None,
                    "navmap_trace_samples": 0 if navmap_trace is None
                    else navmap_trace.sample_count,
                    "stop_action_succeeded": False,
                    "graph_worker_connected": graph_worker is not None,
                    "graph_backend": None if graph_plugin is None else getattr(
                        graph_plugin, "provider_id", getattr(graph_plugin, "backend_name", "unknown")),
                    "native_conceptgraphs_connected": bool(
                        graph_plugin is not None
                        and "conceptgraphs" in str(getattr(graph_plugin, "provider_id", ""))),
                    "native_occusg_connected": bool(
                        graph_plugin is not None
                        and "occusg" in str(getattr(graph_plugin, "provider_id", ""))),
                    "legacy_conceptgraphs_connected": False,
                    "legacy_occusg_connected": False,
                    "resource_monitor": resource_stats,
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                })
            raise
        if run_error is not None:
            raise run_error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
