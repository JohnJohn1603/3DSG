#!/usr/bin/env python3
"""Replay one captured Habitat-Sim RGB-D sequence through M1-M10.

The simulator is intentionally absent from this script.  It consumes the raw
capture written by ``capture_habitat_replica.py`` and uses the same native
mapping, DuDe/M5, M6-M9 and M10 stages as the Webots offline path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from adapters.habitat.frames import frame_from_capture_record  # noqa: E402
from standalone_sg.config import load_config  # noqa: E402
from standalone_sg.contracts.objects import Detection3D, DetectionBatch  # noqa: E402
from standalone_sg.mapping import NativeRGBDMappingStage  # noqa: E402
from standalone_sg.mapping.grid_artifacts import GridTraceWriter  # noqa: E402
from standalone_sg.navigation import NavigationBlockGraph  # noqa: E402
from standalone_sg.objects import ObjectAssociationManager  # noqa: E402
from standalone_sg.pipeline import DudeM5RegionStage, SceneGraphBuilder  # noqa: E402
from standalone_sg.regions.dude_client import DuDeConfig, DuDeWorkerClient  # noqa: E402
from standalone_sg.regions.pipeline import RegionUpdateProcessor  # noqa: E402
from standalone_sg.serialization import (  # noqa: E402
    load_delta_log, load_snapshot, query_nodes, replay_deltas,
    trajectory_rows, write_delta_log, write_snapshot,
    write_trajectory_csv, write_trajectory_json,
)
from standalone_sg.visualization import (  # noqa: E402
    render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map,
)

M2_M3_BINARY = ROOT / "build/native-m3/occupancy/standalone_sg_octomap_fixture_runner"
DUDE_BINARY = ROOT / "build/native-m4-dude/occusg_dude_worker"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_detection_cache(path: Path) -> dict[str, DetectionBatch]:
    """Load an explicit P3 cache; empty and absent remain distinct."""
    batches: dict[str, DetectionBatch] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            frame_id = str(raw["frame_id"])
            if frame_id in batches:
                raise ValueError(f"duplicate detection batch for {frame_id} at line {line_number}")
            detections = tuple(
                Detection3D(
                    detection_id=str(item["detection_id"]), timestamp_s=float(item["timestamp_s"]),
                    center_W=tuple(float(v) for v in item["center_W"]),
                    center_method=str(item["center_method"]), class_name=item.get("class_name"),
                    class_id=item.get("class_id"), confidence=item.get("confidence"),
                    frame_id=str(item["frame_id"]), layer_id=str(item.get("layer_id", "floor_0")),
                    extents_m=None if item.get("extents_m") is None else tuple(float(v) for v in item["extents_m"]),
                    provider_metadata=item.get("provider_metadata", {}),
                )
                for item in raw.get("detections", [])
            )
            batches[frame_id] = DetectionBatch(
                provider_id=str(raw["provider_id"]), frame_id=frame_id,
                timestamp_s=float(raw["timestamp_s"]), detections=detections,
                provenance=raw.get("provenance", {}),
            )
    if not batches:
        raise ValueError("detection cache is empty")
    return batches


def _load_packets(capture: Path, detection_3d: Path | None = None) -> tuple[dict, list]:
    manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    supported_capture_kinds = {"habitat_sim_replica_cad_raw_rgbd_capture", "habitat_sim_raw_rgbd_capture"}
    if manifest.get("kind") not in supported_capture_kinds:
        raise ValueError("capture is not a supported Habitat-Sim raw RGB-D capture")
    records = manifest.get("frames", [])
    if not records or len(records) != int(manifest.get("frame_count", -1)):
        raise ValueError("capture manifest frame list is missing or inconsistent")
    detection_batches = _load_detection_cache(detection_3d) if detection_3d is not None else None
    packets = []
    previous_timestamp = -math.inf
    for record in records:
        _rgbd, packet = frame_from_capture_record(record, capture)
        if packet.timestamp_s <= previous_timestamp:
            raise ValueError("Habitat simulation timestamps must be strictly increasing")
        previous_timestamp = packet.timestamp_s
        if detection_batches is not None:
            batch = detection_batches.get(str(record["frame_id"]))
            if batch is None:
                raise ValueError(f"detection cache has no batch for frame {record['frame_id']}")
            packet = replace(packet, detections=batch)
        packets.append(packet)
    if detection_batches is not None and set(detection_batches) != {str(record["frame_id"]) for record in records}:
        raise ValueError("detection cache frame set does not exactly match capture")
    return manifest, packets


def _run_once(packets, *, pipeline_config: Path, m2_binary: Path, dude_binary: Path,
              grid_writer: GridTraceWriter | None, dude_preprocessing_policy: str = "source",
              progress_label: str = "builder"):
    phase = load_config(pipeline_config)
    navigation = NavigationBlockGraph(phase.navigation)
    # Make the effective M6 values explicit.  They already equal the
    # reference-code defaults in this profile; passing them through the
    # loaded pipeline config prevents a perception-side metadata block from
    # silently becoming the runtime source.
    objects = ObjectAssociationManager(phase.object_association)
    mapping = NativeRGBDMappingStage(m2_binary, resolution_m=0.05, timeout_s=20.0, default_max_range_m=10.0)
    region_stage = DudeM5RegionStage(
        DuDeWorkerClient(dude_binary, DuDeConfig(preprocessing_policy=dude_preprocessing_policy)),
        RegionUpdateProcessor(), capture_preprocessed_mask=True,
    )
    builder = SceneGraphBuilder(mapping_stage=mapping, region_stage=region_stage,
                                navigation=navigation, objects=objects,
                                config_hash=_sha256(pipeline_config))
    initial = builder.snapshot()
    deltas, diagnostics, snapshots = [], [], []
    try:
        for packet_index, packet in enumerate(packets, start=1):
            started = time.perf_counter()
            snapshot, delta = builder.update(packet)
            snapshots.append(snapshot)
            deltas.append(delta)
            response = region_stage.last_response or {}
            result = region_stage.last_result
            diagnostics.append({
                "frame_id": packet.frame_id, "frame_index": packet.frame_index,
                "timestamp_s": packet.timestamp_s, "graph_revision": snapshot.revision,
                "update_runtime_ms": (time.perf_counter() - started) * 1000.0,
                "m4_backend_status": response.get("status"), "m4_backend_branch": response.get("branch"),
                "m4_guard_accepted": None if result is None else bool(result.accepted),
                "m4_stale": None if result is None else bool(result.stale),
                "m4_region_count": 0 if result is None else len(result.current_regions),
                "m4_request_revision": None if result is None else result.request_revision,
                "m4_accepted_observation_revision": None if result is None else result.accepted_observation_revision,
                "m4_reason": None if result is None else result.reason,
                "native_mapping": dict(mapping.records[-1]),
                "stage_status": dict(delta.diagnostics.get("stage_status", {})),
                "events": list(delta.events),
                "node_counts": {kind: len(query_nodes(snapshot, kind)) for kind in ("agent", "object", "navigation", "room")},
                "object_nodes": [
                    {
                        "node_id": node.node_id,
                        "class_name": node.attributes.get("class_name"),
                        "first_seen_s": node.first_seen_s,
                        "last_seen_s": node.last_seen_s,
                        "first_detection_id": node.attributes.get("first_detection_id"),
                        "last_detection_id": node.attributes.get("last_detection_id"),
                        "room_membership_edges": sorted(
                            edge.source_id for edge in snapshot.edges
                            if edge.relation == "room_contains" and edge.target_id == node.node_id
                        ),
                    }
                    for node in snapshot.nodes if node.kind == "object"
                ],
            })
            if grid_writer is not None:
                grid_writer.write_frame(packet=packet, grid=builder.last_grid, response=response,
                                        result=result, snapshot=snapshot)
            print(f"PROGRESS: {progress_label} frame {packet_index}/{len(packets)} updated", flush=True)
        flush_timestamp = packets[-1].timestamp_s + 1e-3
        final, flush_delta = builder.flush(flush_timestamp)
        deltas.append(flush_delta)
        return initial, final, deltas, diagnostics, snapshots, mapping, region_stage
    except Exception:
        builder.close()
        raise


def run_replay(capture: Path, output: Path, *, pipeline_config: Path,
               m2_binary: Path = M2_M3_BINARY, dude_binary: Path = DUDE_BINARY,
               grid_output: Path | None = None, deterministic_check: bool = True,
               detection_3d_path: Path | None = None,
               dude_preprocessing_policy: str = "source") -> dict:
    capture, output, pipeline_config = capture.resolve(), output.resolve(), pipeline_config.resolve()
    phase = load_config(pipeline_config)
    detection_3d_path = None if detection_3d_path is None else detection_3d_path.resolve()
    manifest, packets = _load_packets(capture, detection_3d_path)
    if not pipeline_config.is_file() or not m2_binary.is_file() or not dude_binary.is_file():
        raise FileNotFoundError("Habitat replay requires pipeline config, M2/M3 binary and DuDe binary")
    output.mkdir(parents=True, exist_ok=True)
    grid_writer = GridTraceWriter(grid_output) if grid_output is not None else None
    start = time.perf_counter()
    initial, final, deltas, diagnostics, snapshots, mapping, region_stage = _run_once(
        packets, pipeline_config=pipeline_config, m2_binary=m2_binary, dude_binary=dude_binary,
        grid_writer=grid_writer, dude_preprocessing_policy=dude_preprocessing_policy,
        progress_label="builder",
    )
    if grid_writer is not None:
        grid_writer.close(capture_dir=capture, config_path=pipeline_config)
    try:
        # This second fresh instance is the replay determinism check; it does
        # not write artifacts and uses the same raw packets/config/binaries.
        deterministic_equal = None
        if deterministic_check:
            initial_again, final_again, deltas_again, _diag_again, _snap_again, mapping_again, region_again = _run_once(
                packets, pipeline_config=pipeline_config, m2_binary=m2_binary, dude_binary=dude_binary,
                grid_writer=None, dude_preprocessing_policy=dude_preprocessing_policy,
                progress_label="fresh-check",
            )
            try:
                deterministic_equal = (initial_again == initial and final_again == final and deltas_again == deltas)
                if not deterministic_equal:
                    raise RuntimeError("fresh Habitat packet replay differs from the first replay")
            finally:
                mapping_again.close()
                region_again.close()
    finally:
        mapping.close()
        region_stage.close()

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
        raise RuntimeError("Habitat snapshot reload/delta replay mismatch")
    trajectory = trajectory_rows(reloaded)
    if len(trajectory) != len(packets):
        raise RuntimeError("Habitat trajectory length differs from captured packet count")
    node_ids = {node.node_id for node in reloaded.nodes}
    if any(edge.source_id not in node_ids or edge.target_id not in node_ids or edge.source_id == edge.target_id
           for edge in reloaded.edges):
        raise RuntimeError("Habitat replay graph contains dangling or self edges")
    (output / "frame_diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    geometry_validation = output.parent / "geometry_calibration.json"
    out_manifest = {
        "schema_version": 1,
        "kind": "habitat_sim_replica_cad_offline_native_replay" if manifest.get("dataset_type", "replica_cad") == "replica_cad" else "habitat_sim_offline_native_replay",
        "dataset_type": manifest.get("dataset_type", "replica_cad"),
        "dataset_version": manifest.get("dataset_version"),
        "pipeline_status": "pass", "capture": str(capture), "capture_manifest_sha256": _sha256(capture / "capture_manifest.json"),
        "habitat_sim_version": manifest.get("habitat_sim_version"), "scene_id": manifest.get("scene_id"),
        "scene_file": manifest.get("scene_file"), "scene_sha256": manifest.get("scene_sha256"),
        "scene_dataset_config": manifest.get("scene_dataset_config"),
        "scene_dataset_config_sha256": manifest.get("scene_dataset_config_sha256"),
        "navmesh_file": manifest.get("navmesh_file"), "navmesh_sha256": manifest.get("navmesh_sha256"),
        "scene_provenance": manifest.get("scene_provenance"),
        "source_profile": "reference/code" if dude_preprocessing_policy == "source" else "extended (not source parity)",
        "m4_preprocessing_policy": dude_preprocessing_policy,
        "m4_effective_parameters": {"decomp_threshold_m": 1.5, "safety_distance_m": 0.5,
                                     "grid_resolution_m": 0.05, "pixel_tau": 30.0,
                                     "parameter_provenance": "DuDeConfig defaults passed by this replay runner"},
        "geometry_validation": str(geometry_validation) if geometry_validation.is_file() else None,
        "source_lock": str((ROOT / "locks/sources.json").relative_to(ROOT)),
        "source_lock_sha256": _sha256(ROOT / "locks/sources.json"),
        "replay_runner": str(Path(__file__).resolve().relative_to(ROOT)),
        "replay_runner_sha256": _sha256(Path(__file__).resolve()),
        "habitat_adapter_sha256": {
            "adapters/habitat/raw.py": _sha256(ROOT / "adapters/habitat/raw.py"),
            "adapters/habitat/frames.py": _sha256(ROOT / "adapters/habitat/frames.py"),
        },
        "frames": len(packets),
        "detections": "cached_real_rgb_instance_segmentation" if detection_3d_path is not None else "absent",
        "detection_3d_cache": None if detection_3d_path is None else str(detection_3d_path),
        "detection_3d_cache_sha256": None if detection_3d_path is None else _sha256(detection_3d_path),
        "room_gt_passed_to_builder": False,
        "navmesh_used_for_route": manifest.get("navmesh_route_tool"),
        "mapping_backend": "NativeRGBDMappingStage -> native M2/M3",
        "segmentation_backend": "pinned DuDe worker + M5 reference policy",
        "graph_stages": ["M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10"],
        "config": str(pipeline_config.relative_to(ROOT)), "config_sha256": _sha256(pipeline_config),
        "effective_runtime_config": {
            "source": str(pipeline_config.relative_to(ROOT)),
            "source_sha256": _sha256(pipeline_config),
            "objects.association": {
                "same_class_radius_m": phase.object_association.same_class_radius_m,
                "same_pose_suppression_cap_m": phase.object_association.same_pose_suppression_cap_m,
            },
            "note": "P4/M6 runtime values are loaded from pipeline config; detector profile association metadata is not read by replay",
        },
        "m2_m3_binary": str(m2_binary), "m2_m3_binary_sha256": _sha256(m2_binary),
        "dude_binary": str(dude_binary), "dude_binary_sha256": _sha256(dude_binary),
        "revision": reloaded.revision, "nodes": len(reloaded.nodes), "edges": len(reloaded.edges),
        "regions_final": diagnostics[-1]["m4_region_count"], "rooms_final": len(query_nodes(reloaded, "room")),
        "snapshot_reload_equal": reloaded == final, "delta_replay_equal": replayed == reloaded,
        "fresh_replay_equal": deterministic_equal, "trajectory_poses": len(trajectory),
        "runtime_ms": (time.perf_counter() - start) * 1000.0,
        "python_process_ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "artifacts": ["snapshot.json", "delta_log.json", "trajectory.csv", "trajectory.json",
                       "trajectory_rooms.png", "scene_graph.png", "spatial_map.png", "relationship_graph.png",
                       "frame_diagnostics.json"],
        "limitations": [
            "fixed ground-truth observation poses, not a physical robot or SLAM",
            "navmesh is used only to validate the scripted route; no planner output enters the builder",
            "no semantic/room GT enters M1-M10; detector input is present only with explicit cached detections",
            "room segmentation quality is not claimed without independent ReplicaCAD room annotations",
        ],
        "cloud_upload": False,
    }
    if geometry_validation.is_file():
        out_manifest["artifacts"].append("../geometry_calibration.json")
    (output / "manifest.json").write_text(json.dumps(out_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pipeline-config", type=Path, default=ROOT / "configs/reference_code_mp3d.yaml")
    parser.add_argument("--m2-m3-binary", type=Path, default=M2_M3_BINARY)
    parser.add_argument("--dude-binary", type=Path, default=DUDE_BINARY)
    parser.add_argument("--grid-output", type=Path, default=None)
    parser.add_argument("--detection-3d", type=Path, default=None,
                        help="explicit P3 DetectionBatch JSONL cache; omitted means provider absent")
    parser.add_argument("--dude-preprocessing-policy", choices=("source", "extended_unknown_boundary"),
                        default="source", help="M4 preprocessing policy; extended mode is an experimental extension")
    parser.add_argument("--no-determinism-check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_replay(args.capture, args.output, pipeline_config=args.pipeline_config,
                                m2_binary=args.m2_m3_binary, dude_binary=args.dude_binary,
                                grid_output=args.grid_output,
                                detection_3d_path=args.detection_3d,
                                dude_preprocessing_policy=args.dude_preprocessing_policy,
                                deterministic_check=not args.no_determinism_check), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
