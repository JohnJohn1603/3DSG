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
import os
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
from standalone_sg.objects.u3_experimental import U3Observation, deduplicate_frame_masks  # noqa: E402
from standalone_sg.objects.u5_backends import (  # noqa: E402
    GeometryOnlyAssociationBackend, GeometryOnlyDuplicateV2AssociationBackend,
    SourceAssociationBackend,
)
from standalone_sg.pipeline import DudeM5RegionStage, SceneGraphBuilder  # noqa: E402
from integration_v2.occusg_session import (  # noqa: E402
    OccuSGSession, attach_cleanup_errors, close_resources,
)
from standalone_sg.regions.dude_client import DuDeConfig, DuDeWorkerClient  # noqa: E402
from standalone_sg.regions.pipeline import RegionUpdateProcessor  # noqa: E402
from standalone_sg.resource_binding import resolve_external_file  # noqa: E402
from standalone_sg.serialization import (  # noqa: E402
    load_delta_log, load_snapshot, query_nodes, replay_deltas,
    trajectory_rows, write_delta_log, write_snapshot,
    write_trajectory_csv, write_trajectory_json,
)
from standalone_sg.serialization.live_revision_io import AcknowledgedPublisher  # noqa: E402
from standalone_sg.visualization import (  # noqa: E402
    render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map,
)

M2_M3_BINARY = resolve_external_file("native_m2_m3", required=True, executable=True)
DUDE_BINARY = resolve_external_file("native_dude", required=True, executable=True)
DUPLICATE_V2_CONFIG_SHA256 = "257dd354c7b562fed4b8d2b6548fcf38fe67946aef2e95ae1c084d79044d3ec3"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_native_bindings(m2_binary: Path, dude_binary: Path,
                            resource_manifest: Path = ROOT / "locks/external_resources.json") -> dict:
    lock = json.loads(resource_manifest.read_text(encoding="utf-8"))
    if lock.get("status") != "bound_input_only" or Path(lock.get("workspace_root", "")).resolve() != ROOT:
        raise ValueError("external resource lock is not bound to this workspace")
    result = {}
    for name, path in (("native_m2_m3", m2_binary.resolve()), ("native_dude", dude_binary.resolve())):
        resolved = resolve_external_file(name, required=True, executable=True, lock_path=resource_manifest)
        if resolved != path:
            raise ValueError(f"{name} explicit argument differs from shared resource resolver")
        record = lock.get("files", {}).get(name)
        if not record or Path(record["path"]).resolve() != path:
            raise ValueError(f"{name} path differs from locked read-only resource binding")
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FileNotFoundError(f"{name} is missing or not executable: {path}")
        digest = _sha256(path)
        if digest != record.get("sha256"):
            raise ValueError(f"{name} hash differs from locks/external_resources.json")
        result[name] = {"path": str(path), "sha256": digest, "bytes": path.stat().st_size,
                        "role": record.get("role", "read_only_external_input")}
    return {"resource_manifest": str(resource_manifest.resolve()),
            "resource_manifest_sha256": _sha256(resource_manifest), "binaries": result}


def _load_geometry_duplicates(packets: list, p2_index_path: Path,
                              capture_manifest_path: Path, duplicate_iou_threshold: float) -> tuple[dict[str, str], dict]:
    import numpy as np

    p2_path = p2_index_path.resolve()
    p2 = json.loads(p2_path.read_text(encoding="utf-8"))
    capture_hash = _sha256(capture_manifest_path)
    if p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != capture_hash:
        raise ValueError("geometry-only backend requires a P2 index verified against this capture")
    p2_by_id = {str(d["detection_id"]): d for frame in p2["frames"] for d in frame["detections"]}
    masks: dict[str, object] = {}
    observations: list[U3Observation] = []
    for packet in packets:
        if packet.detections is None:
            continue
        for detection in sorted(packet.detections.detections, key=lambda item: item.detection_id):
            row = p2_by_id.get(detection.detection_id)
            if row is None:
                raise ValueError(f"P3 detection missing from verified P2 index: {detection.detection_id}")
            mask_path = p2_path.parent / row["mask_file"]
            if not mask_path.is_file() or _sha256(mask_path) != row.get("mask_sha256"):
                raise ValueError(f"P2 mask missing or hash mismatch: {detection.detection_id}")
            masks[detection.detection_id] = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
            observations.append(U3Observation(
                detection_id=detection.detection_id, frame_id=packet.frame_id,
                timestamp_s=detection.timestamp_s, center=detection.center_W,
                extents=detection.extents_m, layer_id=detection.layer_id,
                class_name=detection.class_name, feature=None,
                mask_hash=str(row.get("mask_sha256", "")),
            ))
    kept, duplicates = deduplicate_frame_masks(observations, masks, iou_threshold=duplicate_iou_threshold)
    aliases = {str(row["detection_id"]): str(row["duplicate_of"]) for row in duplicates}
    provenance = {"p2_index_path": str(p2_path), "p2_index_sha256": _sha256(p2_path),
        "capture_manifest_sha256": capture_hash, "verified_p2_masks": len(masks),
        "p3_observations": len(observations), "duplicate_mask_iou_threshold": duplicate_iou_threshold,
        "suppressed_duplicate_masks": len(duplicates), "kept_observations": len(kept)}
    return aliases, provenance


def _attach_duplicate_v2_evidence(packets: list, p2_index_path: Path,
                                  capture_manifest_path: Path) -> tuple[list, dict]:
    """Bind current-frame M9 detections to verified cached P2/P3 evidence."""
    p2_path = p2_index_path.resolve()
    p2 = json.loads(p2_path.read_text(encoding="utf-8"))
    capture_hash = _sha256(capture_manifest_path)
    if p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != capture_hash:
        raise ValueError("dedup-v2 requires a P2 index verified against this capture")
    p2_by_id = {str(d["detection_id"]): d for frame in p2["frames"] for d in frame["detections"]}
    enriched_packets = []
    verified_mask_hashes = {}
    p3_observations = 0
    for packet in packets:
        batch = packet.detections
        if batch is None or batch.detections is None:
            enriched_packets.append(packet)
            continue
        enriched_detections = []
        for detection in batch.detections:
            row = p2_by_id.get(detection.detection_id)
            if row is None:
                raise ValueError(f"P3 detection missing from verified P2 index: {detection.detection_id}")
            mask_path = (p2_path.parent / row["mask_file"]).resolve()
            if not mask_path.is_file() or _sha256(mask_path) != row.get("mask_sha256"):
                raise ValueError(f"P2 mask missing/hash mismatch: {detection.detection_id}")
            metadata = dict(detection.provider_metadata)
            metadata.update({
                "u5_duplicate_v2_mask_path": str(mask_path),
                "u5_duplicate_v2_mask_sha256": str(row["mask_sha256"]),
                "u5_duplicate_v2_valid_depth_pixels": int(metadata.get("valid_depth_pixels", 0)),
                "u5_duplicate_v2_cluster_points": int(metadata.get("cluster_points", 0)),
                "u5_duplicate_v2_detector_score": float(row.get("score_raw", 0.0)),
                "u5_duplicate_v2_evidence_provenance": "verified_cached_P2_mask_and_P3_support",
            })
            enriched_detections.append(replace(detection, provider_metadata=metadata))
            verified_mask_hashes[detection.detection_id] = row["mask_sha256"]
            p3_observations += 1
        enriched_batch = replace(batch, detections=tuple(enriched_detections))
        enriched_packets.append(replace(packet, detections=enriched_batch))
    expected_p3_ids = {d.detection_id for packet in enriched_packets if packet.detections is not None
                       for d in (packet.detections.detections or ())}
    if set(verified_mask_hashes) != expected_p3_ids or p3_observations != len(expected_p3_ids):
        raise ValueError("dedup-v2 verified P2 mask set does not exactly cover cached P3 detections")
    return enriched_packets, {
        "profile": "u3_same_frame_duplicate_candidate_v2",
        "p2_index_path": str(p2_path), "p2_index_sha256": _sha256(p2_path),
        "capture_manifest_sha256": capture_hash,
        "verified_p2_masks": len(verified_mask_hashes),
        "p3_observations": p3_observations,
        "mask_hashes_sha256": hashlib.sha256(json.dumps(verified_mask_hashes, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        "mask_support_policy": "P2 mask plus cached P3 valid-depth/cluster support, current frame only",
    }


def _offline_geometry_reference(packets: list, duplicate_aliases: dict[str, str],
                                geometry_config: dict) -> dict[str, str]:
    """Independent empty-state U3 replay used to check the M9 adapter assignments."""
    from standalone_sg.objects.u3_experimental import ExperimentalAssociation

    engine = ExperimentalAssociation(
        "geometry", max_center_m=float(geometry_config["candidate_center_radius_m"]),
        geom_min_iou=float(geometry_config["appearance_iou_min"]),
        geom_only_min_iou=float(geometry_config["geometry_only_iou_min"]),
        ambiguity_margin=0.05,
    )
    assignments: dict[str, str] = {}
    for packet in packets:
        if packet.detections is None:
            continue
        detections = sorted((d for d in packet.detections.detections if d.detection_id not in duplicate_aliases),
                            key=lambda item: item.detection_id)
        observations = [U3Observation(
            detection_id=d.detection_id, frame_id=packet.frame_id, timestamp_s=d.timestamp_s,
            center=d.center_W, extents=d.extents_m, layer_id=d.layer_id,
            class_name=d.class_name, feature=None,
            mask_hash=str(d.provider_metadata.get("mask_sha256", "")),
        ) for d in detections]
        for decision in engine.update_frame(observations):
            if decision.track_id is not None:
                assignments[decision.detection_id] = decision.track_id
        for detection_id, canonical_id in duplicate_aliases.items():
            if canonical_id in assignments and any(d.detection_id == detection_id for d in packet.detections.detections):
                assignments[detection_id] = assignments[canonical_id]
    return assignments


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
              height_interval_trace_dir: Path | None = None,
              scope_bridge_provenance: dict | None = None,
              progress_label: str = "builder", object_backend_mode: str = "source",
              duplicate_aliases: dict[str, str] | None = None,
              geometry_config: dict | None = None, duplicate_v2_config: dict | None = None,
              graph_config_hash: str | None = None,
              semantic_by_id: dict[str, dict] | None = None,
              live_publisher: AcknowledgedPublisher | None = None,
              live_snapshot_dir: Path | None = None):
    phase = load_config(pipeline_config)
    navigation = NavigationBlockGraph(phase.navigation)
    # Make the effective M6 values explicit.  They already equal the
    # reference-code defaults in this profile; passing them through the
    # loaded pipeline config prevents a perception-side metadata block from
    # silently becoming the runtime source.
    if object_backend_mode == "source":
        objects = ObjectAssociationManager(phase.object_association)
    elif object_backend_mode == "source-wrapper":
        objects = SourceAssociationBackend(ObjectAssociationManager(phase.object_association))
    elif object_backend_mode == "geometry-only":
        if geometry_config is None:
            raise ValueError("geometry-only backend requires locked U3 config")
        objects = GeometryOnlyAssociationBackend(
            duplicate_aliases=duplicate_aliases,
            max_center_m=float(geometry_config["candidate_center_radius_m"]),
            geom_min_iou=float(geometry_config["appearance_iou_min"]),
            geom_only_min_iou=float(geometry_config["geometry_only_iou_min"]),
            ambiguity_margin=0.05,
        )
    elif object_backend_mode == "geometry-only-dedup-v2":
        if geometry_config is None or duplicate_v2_config is None:
            raise ValueError("geometry-only-dedup-v2 requires locked U3 and duplicate-v2 configs")
        objects = GeometryOnlyDuplicateV2AssociationBackend(
            duplicate_config=duplicate_v2_config,
            max_center_m=float(geometry_config["candidate_center_radius_m"]),
            geom_min_iou=float(geometry_config["appearance_iou_min"]),
            geom_only_min_iou=float(geometry_config["geometry_only_iou_min"]),
            ambiguity_margin=float(geometry_config.get("ambiguity_margin", 0.05)),
        )
    else:
        raise ValueError(f"unknown object backend mode: {object_backend_mode}")
    mapping = None
    region_stage = None
    dude_worker = None
    try:
        mapping = NativeRGBDMappingStage(
            m2_binary, resolution_m=0.05, timeout_s=20.0, default_max_range_m=10.0,
            height_interval_trace_dir=height_interval_trace_dir,
        )
        # Keep the worker as a separately owned handle until the region stage
        # has been constructed.  This closes a worker even if stage/builder
        # initialization fails after the subprocess has started.
        dude_worker = DuDeWorkerClient(
            dude_binary, DuDeConfig(preprocessing_policy=dude_preprocessing_policy)
        )
        region_stage = DudeM5RegionStage(
            dude_worker,
            RegionUpdateProcessor(), capture_preprocessed_mask=True,
        )
        session = OccuSGSession.open(builder=SceneGraphBuilder(
            mapping_stage=mapping, region_stage=region_stage,
            navigation=navigation, objects=objects,
            config_hash=graph_config_hash or _sha256(pipeline_config)))
    except BaseException as exc:
        try:
            cleanup_errors = close_resources((region_stage, dude_worker, mapping))
            if cleanup_errors:
                cleanup = RuntimeError("; ".join(str(error) for error in cleanup_errors))
                cleanup.errors = cleanup_errors
                attach_cleanup_errors(exc, cleanup)
        except BaseException as cleanup_signal:
            attach_cleanup_errors(exc, cleanup_signal)
        raise
    builder = session.builder
    initial = builder.snapshot()
    deltas, diagnostics, snapshots = [], [], []
    try:
        for packet_index, packet in enumerate(packets, start=1):
            started = time.perf_counter()
            snapshot, delta = session.update_frame(packet)
            update_runtime_ms = (time.perf_counter() - started) * 1000.0
            snapshots.append(snapshot)
            deltas.append(delta)
            if live_publisher is not None:
                if live_snapshot_dir is None:
                    raise ValueError("live room publishing requires a prefix snapshot directory")
                live_snapshot_dir.mkdir(parents=True, exist_ok=True)
                live_state_path = live_snapshot_dir / f"rev-{packet.frame_index:06d}.json"
                write_snapshot(live_state_path, snapshot)
                live_publisher.publish({
                    "schema": "scene-graph-live-branch-event/v1",
                    "scene_id": snapshot.scene_id, "sequence_id": snapshot.sequence_id,
                    "source_frame_index": int(packet.frame_index),
                    "source_frame_id": str(packet.frame_id),
                    "source_timestamp_s": float(packet.timestamp_s),
                    "source_revision": int(snapshot.revision),
                    "revision_kind": "occusg_room_graph_prefix",
                    "processing_ms": update_runtime_ms,
                }, state_path=live_state_path)
            association_result = builder.last_object_association_result
            association_lineage = [] if association_result is None else [
                {
                    "frame_id": association.frame_id,
                    "timestamp_s": association.timestamp_s,
                    "detection_id": association.detection_id,
                    "provider_id": association.provider_id,
                    "observation_class_name": next(
                        (det.class_name for det in packet.detections.detections
                         if det.detection_id == association.detection_id), None),
                    "candidate_id": association.object_id,
                    "object_id": association.object_id,
                    "decision": association.decision,
                    "distance_m": association.distance_m,
                    "normalized_class_name": association.normalized_class_name,
                }
                for association in association_result.associations
            ]
            if object_backend_mode != "source":
                diagnostics_backend = {
                    "backend_name": getattr(objects, "backend_name", object_backend_mode),
                    "lifecycle_events": list(getattr(objects, "last_lifecycle_events", ())),
                }
                if hasattr(objects, "last_decisions"):
                    diagnostics_backend["decisions"] = list(objects.last_decisions)
                    diagnostics_backend["candidate_ledger"] = list(objects.last_candidate_ledger)
                    diagnostics_backend["deferred_detection_ids"] = list(objects.last_deferred_detection_ids)
                if hasattr(objects, "last_duplicate_decisions"):
                    diagnostics_backend["duplicate_decisions"] = list(objects.last_duplicate_decisions)
                    diagnostics_backend["duplicate_aliases"] = list(objects.last_duplicate_aliases)
                    diagnostics_backend["duplicate_pair_evidence"] = list(objects.last_duplicate_pair_evidence)
                    diagnostics_backend["ambiguous_detection_ids"] = list(objects.last_ambiguous_detection_ids)
                diagnostics_backend["assignments"] = [
                    {"detection_id": item["detection_id"], "object_id": item["object_id"], "decision": item["decision"]}
                    for item in association_lineage
                ]
            else:
                diagnostics_backend = None
            semantic_rows = []
            if semantic_by_id is not None and packet.detections is not None:
                actual_by_id = {row["detection_id"]: row for row in association_lineage}
                for detection in packet.detections.detections:
                    sidecar_row = semantic_by_id.get(detection.detection_id)
                    if sidecar_row is None:
                        continue
                    if int(sidecar_row["evidence_end_frame_index"]) > packet.frame_index:
                        raise RuntimeError(f"U4-OV sidecar contains future evidence for {detection.detection_id}")
                    actual = actual_by_id.get(detection.detection_id)
                    semantic_rows.append({
                        "observation_id": detection.detection_id,
                        "raw_detector_class": detection.class_name,
                        "sidecar_raw_detector_class": sidecar_row.get("raw_detector_class"),
                        "raw_class_provenance_matches": detection.class_name == sidecar_row.get("raw_detector_class"),
                        "fused_semantic_label": sidecar_row.get("fused_semantic_label"),
                        "fused_semantic_status": sidecar_row.get("fused_semantic_status"),
                        "label_source": sidecar_row.get("label_source"),
                        "evidence_ids": sidecar_row.get("evidence_ids", []),
                        "evidence_end_frame_index": int(sidecar_row["evidence_end_frame_index"]),
                        "semantic_config_sha256": sidecar_row.get("config_sha256"),
                        "semantic_model_revision": sidecar_row.get("model_revision"),
                        "semantic_model_checkpoint_sha256": sidecar_row.get("model_checkpoint_sha256"),
                        "offline_reference_track_id": sidecar_row.get("association_track_id"),
                        "graph_backend_track_id": None if actual is None else actual["object_id"],
                        "association_reference_match": None if actual is None else actual["object_id"] == sidecar_row.get("association_track_id"),
                        "future_observations_used": False,
                    })
            response = region_stage.last_response or {}
            result = region_stage.last_result
            frame_diag = {
                "frame_id": packet.frame_id, "frame_index": packet.frame_index,
                "timestamp_s": packet.timestamp_s, "graph_revision": snapshot.revision,
                "update_runtime_ms": update_runtime_ms,
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
                "object_lifecycle_events": [dict(event) for event in delta.diagnostics.get("object_lifecycle_events", [])],
                "object_associations": association_lineage,
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
            }
            if diagnostics_backend is not None:
                frame_diag["object_backend"] = diagnostics_backend
            if semantic_by_id is not None:
                frame_diag["u5_semantic_observations"] = semantic_rows
            diagnostics.append(frame_diag)
            if grid_writer is not None:
                interval_trace = getattr(mapping.worker, "last_height_interval_trace", None)
                grid_writer.write_frame(packet=packet, grid=builder.last_grid, response=response,
                                        result=result, snapshot=snapshot,
                                        height_interval_trace=interval_trace,
                                        scope_bridge_provenance=scope_bridge_provenance,
                                        m3_source_oracle=mapping.records[-1].get("m3_source_oracle", {}))
            print(f"PROGRESS: {progress_label} frame {packet_index}/{len(packets)} updated", flush=True)
        flush_timestamp = packets[-1].timestamp_s + 1e-3
        final, flush_delta = session.finalize(flush_timestamp)
        deltas.append(flush_delta)
        # The session owns the builder/stages.  Keep this close at the terminal
        # boundary; callers may still call the native stage close methods, which
        # are idempotent by contract.
        session.close()
        return initial, final, deltas, diagnostics, snapshots, mapping, region_stage
    except BaseException as exc:
        try:
            session.close()
        except BaseException as cleanup_error:
            attach_cleanup_errors(exc, cleanup_error)
        raise


def run_replay(capture: Path, output: Path, *, pipeline_config: Path,
               m2_binary: Path = M2_M3_BINARY, dude_binary: Path = DUDE_BINARY,
               grid_output: Path | None = None, deterministic_check: bool = True,
               height_interval_trace: bool = False,
               detection_3d_path: Path | None = None,
               detection_3d_manifest_path: Path | None = None,
               dude_preprocessing_policy: str = "source", object_backend_mode: str = "source",
               detection_2d_index_path: Path | None = None,
               u3_config_lock_path: Path = ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json",
               duplicate_v2_config_path: Path = ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json",
               duplicate_v2_reference_path: Path | None = None,
               disable_same_frame_mask_dedup: bool = False,
               u4_semantic_sidecar_path: Path | None = None,
               resource_manifest_path: Path = ROOT / "locks/external_resources.json",
               live_publish_root: Path | None = None) -> dict:
    capture, output, pipeline_config = capture.resolve(), output.resolve(), pipeline_config.resolve()
    if disable_same_frame_mask_dedup and object_backend_mode != "geometry-only":
        raise ValueError("--disable-same-frame-mask-dedup is only valid for geometry-only diagnostic replay")
    phase = load_config(pipeline_config)
    detection_3d_path = None if detection_3d_path is None else detection_3d_path.resolve()
    manifest, packets = _load_packets(capture, detection_3d_path)
    m2_binary, dude_binary = m2_binary.resolve(), dude_binary.resolve()
    if not pipeline_config.is_file():
        raise FileNotFoundError("Habitat replay requires pipeline config, M2/M3 binary and DuDe binary")
    native_binding = _verify_native_bindings(m2_binary, dude_binary, resource_manifest_path.resolve())
    d3_manifest_data = None
    if detection_3d_manifest_path is not None:
        d3_manifest_path = detection_3d_manifest_path.resolve()
        d3_manifest_data = json.loads(d3_manifest_path.read_text(encoding="utf-8"))
        if (d3_manifest_data.get("status") != "pass"
                or d3_manifest_data.get("capture_manifest_sha256") != _sha256(capture / "capture_manifest.json")
                or d3_manifest_data.get("frame_count") != len(packets)):
            raise ValueError("P3 manifest does not match the selected capture/frame count")
        p3_count = sum(len(packet.detections.detections) for packet in packets if packet.detections is not None)
        if d3_manifest_data.get("detection_3d_count") != p3_count:
            raise ValueError("P3 manifest observation count differs from P3 cache")
    if output.exists():
        raise FileExistsError(f"replay output already exists; refusing to overwrite: {output}")
    if grid_output is not None and grid_output.resolve().exists():
        raise FileExistsError(f"grid output already exists; refusing to overwrite: {grid_output.resolve()}")
    if height_interval_trace and grid_output is None:
        raise ValueError("height-interval tracing requires --grid-output")

    duplicate_aliases: dict[str, str] = {}
    duplicate_provenance = None
    geometry_config = None
    duplicate_v2_config = None
    duplicate_v2_reference = None
    graph_config_hash = _sha256(pipeline_config)
    if object_backend_mode == "geometry-only":
        lock_path = u3_config_lock_path.resolve()
        u3_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        geometry_config = u3_lock["experimental"]
        if disable_same_frame_mask_dedup:
            if detection_2d_index_path is None:
                raise ValueError("geometry-only diagnostic replay still requires the verified P2 index")
            duplicate_aliases = {}
            duplicate_provenance = {"profile": "off", "diagnostic_ablation": "same_frame_mask_dedup_disabled",
                                    "detection_2d_index_sha256": _sha256(detection_2d_index_path.resolve())}
        elif detection_2d_index_path is None:
            raise ValueError("geometry-only backend requires --detection-2d-index for verified duplicate-mask suppression")
        else:
            duplicate_aliases, duplicate_provenance = _load_geometry_duplicates(
                packets, detection_2d_index_path, capture / "capture_manifest.json",
                float(geometry_config["duplicate_mask_iou_min"]),
            )
        graph_config_hash = hashlib.sha256(json.dumps({
            "pipeline_config_sha256": _sha256(pipeline_config),
            "object_backend": "u3_geometry_only",
            "u3_config_sha256": _sha256(lock_path),
            "duplicate_provenance": duplicate_provenance,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    elif object_backend_mode == "geometry-only-dedup-v2":
        lock_path = u3_config_lock_path.resolve()
        v2_config_path = duplicate_v2_config_path.resolve()
        if detection_2d_index_path is None:
            raise ValueError("geometry-only-dedup-v2 requires --detection-2d-index")
        if _sha256(v2_config_path) != DUPLICATE_V2_CONFIG_SHA256:
            raise ValueError("duplicate v2 config hash differs from the offline lock")
        if duplicate_v2_reference_path is not None and not duplicate_v2_reference_path.is_file():
            raise FileNotFoundError(f"offline duplicate-v2 reference does not exist: {duplicate_v2_reference_path}")
        u3_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        geometry_config = u3_lock["experimental"]
        duplicate_v2_document = json.loads(v2_config_path.read_text(encoding="utf-8"))
        if duplicate_v2_document.get("status") != "development_locked_before_paired_evaluation":
            raise ValueError("duplicate v2 config is not in its frozen development state")
        duplicate_v2_config = duplicate_v2_document["candidate"]
        packets, duplicate_provenance = _attach_duplicate_v2_evidence(
            packets, detection_2d_index_path, capture / "capture_manifest.json",
        )
        if duplicate_v2_reference_path is not None:
            duplicate_v2_reference = {
                row["detection_id"]: row.get("track_id")
                for row in (json.loads(line) for line in duplicate_v2_reference_path.read_text(encoding="utf-8").splitlines() if line.strip())
            }
        graph_config_hash = hashlib.sha256(json.dumps({
            "pipeline_config_sha256": _sha256(pipeline_config),
            "object_backend": "u3_geometry_only_dedup_v2",
            "u3_config_sha256": _sha256(lock_path),
            "duplicate_v2_config_sha256": _sha256(v2_config_path),
            "duplicate_v2_reference_sha256": (None if duplicate_v2_reference_path is None
                                                else _sha256(duplicate_v2_reference_path)),
            "duplicate_evidence_provenance": duplicate_provenance,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    elif object_backend_mode not in ("source", "source-wrapper"):
        raise ValueError(f"unsupported object backend: {object_backend_mode}")

    semantic_by_id = None
    semantic_sidecar_provenance = None
    if u4_semantic_sidecar_path is not None:
        sidecar_path = u4_semantic_sidecar_path.resolve()
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        expected_profile = {"source": "source_raw", "source-wrapper": "source_raw",
                            "geometry-only": "geometry_only"}.get(object_backend_mode)
        if expected_profile is None or sidecar.get("association_mode") != expected_profile:
            raise ValueError("U4-OV sidecar association profile does not match selected graph backend")
        sidecar_inputs = sidecar.get("input_provenance", {})
        if detection_3d_path is None or sidecar_inputs.get("p3_cache_sha256") != _sha256(detection_3d_path):
            raise ValueError("U4-OV sidecar was not generated from the replay's exact P3 cache")
        if sidecar_inputs.get("capture_manifest_sha256") != _sha256(capture / "capture_manifest.json"):
            raise ValueError("U4-OV sidecar capture provenance differs from replay input")
        semantic_by_id = {str(row["observation_id"]): row for row in sidecar.get("observations", [])}
        cache_ids = {d.detection_id for p in packets if p.detections is not None for d in p.detections.detections}
        if set(semantic_by_id) != cache_ids:
            raise ValueError("U4-OV semantic sidecar does not cover exactly the replay P3 detections")
        for row in semantic_by_id.values():
            if row.get("future_observations_used") is not False:
                raise ValueError(f"semantic sidecar is not certified causal: {row['observation_id']}")
        semantic_sidecar_provenance = {"path": str(sidecar_path), "sha256": _sha256(sidecar_path),
            "u4ov_config_sha256": sidecar.get("config_model_provenance", {}).get("u4ov_config_sha256"),
            "checkpoint_sha256": sidecar.get("config_model_provenance", {}).get("checkpoint_sha256"),
            "labels_are_graph_inputs": False, "room_scores_updated": False}

    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=False, exist_ok=False)
    live_publisher = None
    live_snapshot_dir = None
    if live_publish_root is not None:
        if deterministic_check:
            raise ValueError("D1 live publication requires --no-determinism-check; no extra verification replay is allowed")
        live_publish_root = Path(live_publish_root).resolve()
        if not live_publish_root.is_dir():
            raise FileNotFoundError(f"D1 live run root is missing: {live_publish_root}")
        live_publisher = AcknowledgedPublisher(live_publish_root, "rooms")
        live_snapshot_dir = output / "prefix_snapshots"
    grid_writer = GridTraceWriter(grid_output) if grid_output is not None else None
    interval_trace_dir = None
    scope_bridge_provenance = None
    if height_interval_trace:
        interval_trace_dir = grid_output / "height_intervals" / "native_updates"
        interval_trace_dir.mkdir(parents=True, exist_ok=False)
        mapconversion_source = Path(json.loads((ROOT / "locks/sources.json").read_text())[
            "submodules"][0]["checkout"])
        mapconversion_source = (ROOT / mapconversion_source).resolve()
        scope_bridge_provenance = {
            "schema_version": 1,
            "instrumentation": "optional const-output capture from the exact local HeightRangeMap in updateMap; no occupancy/projection decision reads trace output",
            "upstream_mapconversion_commit": json.loads((ROOT / "locks/sources.json").read_text())["submodules"][0]["commit"],
            "upstream_mapconverter_sha256": _sha256(mapconversion_source / "mapconversion/src/MapConverter.cpp"),
            "instrumented_mapconverter_sha256": _sha256(ROOT / "native/scope_bridge/mapconversion/src/MapConverter.cpp"),
            "instrumented_mapconverter_header_sha256": _sha256(ROOT / "native/scope_bridge/mapconversion/include/MapConverter.hh"),
            "scope_bridge_fixture_runner_sha256": _sha256(ROOT / "native/occupancy/fixture_runner.cpp"),
            "projection_backend_sha256": _sha256(ROOT / "native/occupancy/projection_backend.cpp"),
            "m3_binary_sha256": _sha256(m2_binary),
            "pipeline_config_sha256": _sha256(pipeline_config),
            "source_lock_sha256": _sha256(ROOT / "locks/sources.json"),
            "resource_lock_sha256": _sha256(resource_manifest_path.resolve()),
            "trace_output_root": str(interval_trace_dir.resolve()),
            "raw_interval_policy": "all interior cells in the local update grid; free_before_min_height, free_used_for_projection, occupied and final M3 value",
        }
    start = time.perf_counter()
    initial, final, deltas, diagnostics, snapshots, mapping, region_stage = _run_once(
        packets, pipeline_config=pipeline_config, m2_binary=m2_binary, dude_binary=dude_binary,
        grid_writer=grid_writer, dude_preprocessing_policy=dude_preprocessing_policy,
        height_interval_trace_dir=interval_trace_dir,
        scope_bridge_provenance=scope_bridge_provenance,
        progress_label="builder", object_backend_mode=object_backend_mode,
        duplicate_aliases=duplicate_aliases, geometry_config=geometry_config,
        duplicate_v2_config=duplicate_v2_config,
        graph_config_hash=graph_config_hash, semantic_by_id=semantic_by_id,
        live_publisher=live_publisher, live_snapshot_dir=live_snapshot_dir,
    )
    geometry_reference = None
    if object_backend_mode == "geometry-only":
        expected_assignments = _offline_geometry_reference(packets, duplicate_aliases, geometry_config)
        actual_assignments = {
            row["detection_id"]: row["object_id"]
            for frame in diagnostics for row in frame.get("object_backend", {}).get("assignments", [])
        }
        all_ids = set(expected_assignments) | set(actual_assignments)
        mismatches = [detection_id for detection_id in sorted(all_ids)
                      if expected_assignments.get(detection_id) != actual_assignments.get(detection_id)]
        geometry_reference = {
            "kind": "independent_empty_state_u3_geometry_replay_same_packets",
            "expected_assigned_observations": len(expected_assignments),
            "actual_assigned_observations": len(actual_assignments),
            "exact_assignment_parity": not mismatches,
            "mismatch_count": len(mismatches), "mismatch_detection_ids": mismatches[:100],
            "reference_assignments_sha256": hashlib.sha256(json.dumps(expected_assignments, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        if mismatches:
            mapping.close()
            region_stage.close()
            raise RuntimeError(f"geometry M9 backend differs from offline U3 reference for {len(mismatches)} detections")
    elif object_backend_mode == "geometry-only-dedup-v2":
        if duplicate_v2_reference is None:
            geometry_reference = {
                "status": "not_available",
                "reason": "no offline duplicate-v2 assignments are bound to this capture/P2/P3 input",
                "exact_assignment_parity": None,
            }
        else:
            expected_assignments = duplicate_v2_reference
            actual_assignments = {}
            for frame in diagnostics:
                for row in frame.get("object_backend", {}).get("decisions", []):
                    actual_assignments[row["detection_id"]] = row.get("track_id")
            all_ids = set(expected_assignments) | set(actual_assignments)
            mismatches = [detection_id for detection_id in sorted(all_ids)
                          if expected_assignments.get(detection_id) != actual_assignments.get(detection_id)]
            geometry_reference = {
                "status": "pass" if not mismatches else "fail",
                "kind": "locked_offline_duplicate_v2_assignment_reference",
                "reference_path": str(duplicate_v2_reference_path.resolve()),
                "reference_sha256": _sha256(duplicate_v2_reference_path.resolve()),
                "expected_observations": len(expected_assignments),
                "actual_observations": len(actual_assignments),
                "exact_assignment_parity": not mismatches,
                "mismatch_count": len(mismatches),
                "mismatch_detection_ids": mismatches[:100],
            }
            if mismatches:
                mapping.close()
                region_stage.close()
                raise RuntimeError(f"dedup-v2 M9 backend differs from locked offline reference for {len(mismatches)} detections")
    grid_index_path = None
    if grid_writer is not None:
        grid_index_path = grid_writer.close(capture_dir=capture, config_path=pipeline_config)
    try:
        # This second fresh instance is the replay determinism check; it does
        # not write artifacts and uses the same raw packets/config/binaries.
        deterministic_equal = None
        if deterministic_check:
            initial_again, final_again, deltas_again, _diag_again, _snap_again, mapping_again, region_again = _run_once(
                packets, pipeline_config=pipeline_config, m2_binary=m2_binary, dude_binary=dude_binary,
                grid_writer=None, dude_preprocessing_policy=dude_preprocessing_policy,
                progress_label="fresh-check", object_backend_mode=object_backend_mode,
                duplicate_aliases=duplicate_aliases, geometry_config=geometry_config,
                duplicate_v2_config=duplicate_v2_config,
                graph_config_hash=graph_config_hash, semantic_by_id=semantic_by_id,
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
    dangling_edges = [edge for edge in reloaded.edges
        if edge.source_id not in node_ids or edge.target_id not in node_ids]
    self_edges = [edge for edge in reloaded.edges if edge.source_id == edge.target_id]
    room_owners: dict[str, set[str]] = {}
    for edge in reloaded.edges:
        if edge.relation == "room_contains" and edge.target_id.startswith(("object:", "agent:")):
            room_owners.setdefault(edge.target_id, set()).add(edge.source_id)
    conflicting_owner_edges = {target: sorted(owners) for target, owners in room_owners.items() if len(owners) > 1}
    if dangling_edges or self_edges or conflicting_owner_edges:
        raise RuntimeError(
            "Habitat replay graph integrity failed: "
            f"dangling={len(dangling_edges)} self={len(self_edges)} "
            f"conflicting_room_owners={len(conflicting_owner_edges)}"
        )
    (output / "frame_diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    with (output / "object_association_lineage.jsonl").open("w", encoding="utf-8") as stream:
        for frame_diagnostic in diagnostics:
            for observation in frame_diagnostic["object_associations"]:
                stream.write(json.dumps(observation, sort_keys=True, allow_nan=False) + "\n")
    geometry_validation = output.parent / "geometry_calibration.json"
    duplicate_summary = None
    if object_backend_mode == "geometry-only-dedup-v2":
        duplicate_rows = [
            row for frame in diagnostics
            for row in frame.get("object_backend", {}).get("duplicate_decisions", [])
        ]
        duplicate_summary = {
            "profile": "u3_same_frame_duplicate_candidate_v2",
            "raw_p3_observations": len(duplicate_rows),
            "aliased_observations": sum(row.get("decision") == "alias_suppressed" for row in duplicate_rows),
            "ambiguous_observations": sum(row.get("decision") == "kept_ambiguous" for row in duplicate_rows),
            "kept_canonical_observations": sum(row.get("decision") == "kept_canonical" for row in duplicate_rows),
            "association_deferred_observations": sum(
                len(frame.get("object_backend", {}).get("deferred_detection_ids", [])) for frame in diagnostics
            ),
            "ambiguous_means_alias_blocked_not_automatically_deferred": True,
            "alias_independent_view_count": 0,
            "semantic_votes_from_aliases": 0,
            "graph_track_merge_events_from_observation_aliases": 0,
        }
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
        "detection_3d_manifest": None if detection_3d_manifest_path is None else str(detection_3d_manifest_path.resolve()),
        "detection_3d_manifest_sha256": None if detection_3d_manifest_path is None else _sha256(detection_3d_manifest_path.resolve()),
        "room_gt_passed_to_builder": False,
        "navmesh_used_for_route": manifest.get("navmesh_route_tool"),
        "mapping_backend": "NativeRGBDMappingStage -> native M2/M3",
        "segmentation_backend": "pinned DuDe worker + M5 reference policy",
        "object_backend": object_backend_mode,
        "association_profile": object_backend_mode,
        "object_backend_config_sha256": graph_config_hash,
        "native_resource_binding": native_binding,
        "geometry_duplicate_input": duplicate_provenance,
        "same_frame_duplicate_summary": duplicate_summary,
        "same_frame_duplicate_profile": ("u3_same_frame_duplicate_candidate_v2" if object_backend_mode == "geometry-only-dedup-v2"
                                          else "off" if object_backend_mode == "geometry-only" and disable_same_frame_mask_dedup
                                          else "u3_same_frame_mask_iou_v1" if object_backend_mode == "geometry-only" else "off"),
        "same_frame_mask_dedup_diagnostic_override": bool(disable_same_frame_mask_dedup),
        "same_frame_duplicate_v2_config_sha256": (_sha256(duplicate_v2_config_path.resolve())
            if object_backend_mode == "geometry-only-dedup-v2" else None),
        "geometry_offline_reference": geometry_reference,
        "u4_semantic_sidecar": semantic_sidecar_provenance,
        "semantic_modes": {
            "room_labeler": "existing_rule_based_region_object_room_labeler",
            "u4_ov_diagnostics": "off" if semantic_sidecar_provenance is None else "causal_sidecar_diagnostics_only",
            "u4_ov_changes_graph_or_room_scores": False,
        },
        "u5_lifecycle_support": {
            "protocol_version": 1,
            "builder_event_plumbing": ["create", "update", "canonical_alias", "retire"],
            "snapshot_semantics": "active_canonical_objects",
            "history_semantics": "append_only_events",
            "backend_supported_event_kinds": ["create", "update"],
            "policy_decision": "unsupported_deferred",
            "real_alias_or_retirement_observed": False,
            "canonical_alias_reconciliation_infrastructure": True,
            "retirement_removal_reconciliation_infrastructure": True,
            "snapshot_reload_restores_backend_runtime_state": False,
            "membership_edges_reconciled_by_m8": True,
            "room_semantics_modified": False,
            "known_limitations": [
                "real source and geometry backends emit create/update only",
                "no cross-track merge or retirement policy is defined",
                "snapshot reload restores graph lifecycle history, not backend runtime state",
            ],
        },
        "graph_integrity": {
            "dangling_edge_count": len(dangling_edges),
            "self_edge_count": len(self_edges),
            "conflicting_room_owner_target_count": len(conflicting_owner_edges),
        },
        "u5_gate_status": "partial_experimental_backend_integration; lifecycle plumbing tested synthetically; real merge/retirement policy deferred",
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
                       "object_association_lineage.jsonl",
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
    if height_interval_trace:
        out_manifest["height_interval_trace"] = {
            "enabled": True,
            "directory": str(interval_trace_dir.resolve()),
            "frame_index": str(grid_index_path),
            "instrumentation_provenance": scope_bridge_provenance,
            "instrumentation_status": "read_only_candidate_pending_fixture_parity",
        }
        out_manifest["artifacts"].append("../grid_trace/index.json")
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
    parser.add_argument("--height-interval-trace", action="store_true",
                        help="diagnostic-only: require an instrumented M2/M3 binary and write raw MapConverter height intervals under grid-output")
    parser.add_argument("--detection-3d", type=Path, default=None,
                        help="explicit P3 DetectionBatch JSONL cache; omitted means provider absent")
    parser.add_argument("--detection-3d-manifest", type=Path, default=None,
                        help="P3 provenance manifest used to verify cached replay inputs")
    parser.add_argument("--object-backend", choices=("source", "source-wrapper", "geometry-only", "geometry-only-dedup-v2"), default="source",
                        help="M9 object backend; source is the unchanged default")
    parser.add_argument("--detection-2d-index", type=Path, default=None,
                        help="verified P2 index required by geometry-only same-frame duplicate suppression")
    parser.add_argument("--disable-same-frame-mask-dedup", action="store_true",
                        help="diagnostic ablation: geometry-only backend with same-frame mask suppression disabled; no effect on normal launcher profiles")
    parser.add_argument("--u3-config-lock", type=Path, default=ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json")
    parser.add_argument("--duplicate-v2-config", type=Path, default=ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json")
    parser.add_argument("--duplicate-v2-reference", type=Path, default=None,
                        help="frozen offline candidate-v2 assignments for exact M9 parity validation")
    parser.add_argument("--u4-semantic-sidecar", type=Path, default=None,
                        help="optional causal U4-OV sidecar from the same profile; diagnostics only")
    parser.add_argument("--resource-manifest", type=Path, default=ROOT / "locks/external_resources.json")
    parser.add_argument("--dude-preprocessing-policy", choices=("source", "extended_unknown_boundary"),
                        default="source", help="M4 preprocessing policy; extended mode is an experimental extension")
    parser.add_argument("--no-determinism-check", action="store_true")
    parser.add_argument("--live-publish-root", type=Path, default=None,
                        help="Optional D1 run root; publish and barrier each immutable OccuSG revision")
    args = parser.parse_args()
    print(json.dumps(run_replay(args.capture, args.output, pipeline_config=args.pipeline_config,
                                m2_binary=args.m2_m3_binary, dude_binary=args.dude_binary,
                                grid_output=args.grid_output,
                                height_interval_trace=args.height_interval_trace,
                                detection_3d_path=args.detection_3d,
                                detection_3d_manifest_path=args.detection_3d_manifest,
                                dude_preprocessing_policy=args.dude_preprocessing_policy,
                                object_backend_mode=args.object_backend,
                                detection_2d_index_path=args.detection_2d_index,
                                u3_config_lock_path=args.u3_config_lock,
                                duplicate_v2_config_path=args.duplicate_v2_config,
                                duplicate_v2_reference_path=args.duplicate_v2_reference,
                                disable_same_frame_mask_dedup=args.disable_same_frame_mask_dedup,
                                u4_semantic_sidecar_path=args.u4_semantic_sidecar,
                                resource_manifest_path=args.resource_manifest,
                                deterministic_check=not args.no_determinism_check,
                                live_publish_root=args.live_publish_root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
