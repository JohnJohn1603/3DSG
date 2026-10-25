#!/usr/bin/env python3
"""One persistent T2-D mapper worker.

The coordinator starts this process once per mapper branch.  Frames arrive as
newline-delimited commands and are never reloaded as a batch subprocess.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))

from integration_v2.live_processing import _read, _sha  # noqa: E402
from standalone_sg.serialization.live_revision_io import AcknowledgedPublisher, atomic_write_json  # noqa: E402
from integration_v2.camera_live_contract import host_monotonic_domain, validate_current_source_packet  # noqa: E402


def _execution_event(run_root: Path, event: str, **fields) -> None:
    row = {"schema": "camera-live-execution-event/v1", "event": event,
           "event_monotonic_s": time.monotonic(), "clock_domain": host_monotonic_domain(),
           **fields}
    payload = (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    fd = os.open(Path(run_root) / "execution_ledger.jsonl", os.O_APPEND | os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _load_commands() -> list[dict]:
    return [json.loads(line) for line in sys.stdin.read().splitlines() if line.strip()]


def _feature_rows(packet_root: Path, packets: list[Path]) -> tuple[dict, dict]:
    features = {}
    text_by_id = {}
    for packet_path in packets:
        packet = _read(packet_path)
        for row in packet.get("features", []):
            oid = str(row["observation_id"])
            path = (ROOT / row["feature_path"]).resolve()
            with np.load(path, allow_pickle=False) as archive:
                image = np.asarray(archive["image_feature"], dtype=np.float32)
                text = np.asarray(archive["text_feature"], dtype=np.float32)
            if image.shape != (512,) or text.shape != (512,):
                raise ValueError(f"feature shape invalid for {oid}")
            features[oid] = {"feature_path": row["feature_path"],
                             "feature_file_sha256": row["feature_file_sha256"]}
            text_by_id[oid] = text
    return features, text_by_id


def _validate_current_packet(packet: dict, source: Path, kind: str) -> dict:
    """Run the same frame adapter used by the mapper worker, without mutation."""
    from adapters.habitat.frames import frame_from_capture_record

    clock_identity = validate_current_source_packet(packet, context=f"{kind} mapper input")

    capture_root = Path(packet.get("capture_root", source / "capture")).resolve()
    rgbd, frame_packet = frame_from_capture_record(packet["capture_record"], capture_root)
    if frame_packet.frame_id != str(packet["frame_id"]):
        raise ValueError("capture adapter frame_id differs from packet")
    if frame_packet.frame_index != int(packet["frame_index"]):
        raise ValueError("capture adapter frame_index differs from packet")
    if frame_packet.scene_id != str(packet["scene_id"]) or frame_packet.sequence_id != str(packet["sequence_id"]):
        raise ValueError("capture adapter scene/sequence differs from packet")
    if rgbd.depth_m.shape != (int(rgbd.intrinsics.height), int(rgbd.intrinsics.width)):
        raise ValueError("adapter depth shape differs from capture metadata")
    meta = frame_packet.source_metadata
    required = {"depth_units": "m", "depth_representation": "axial", "depth_scale_to_m": 1.0}
    for key, value in required.items():
        if meta.get(key) != value:
            raise ValueError(f"adapter metadata {key}={meta.get(key)!r} does not match {value!r}")
    if frame_packet.rgbd is None or frame_packet.rgbd.intrinsics.width != int(rgbd.intrinsics.width):
        raise ValueError("adapter intrinsics/RGB-D packet is incomplete")
    if kind == "conceptgraphs":
        # These are exactly the assets consumed by the existing ConceptGraphs
        # update body; P3 acceptance is not used to filter this population.
        p2 = list(packet.get("p2_detections", []))
        ids = {str(row["detection_id"]) for row in p2}
        feature_ids = {str(row["observation_id"]) for row in packet.get("features", [])}
        if ids != feature_ids:
            raise ValueError("P2/feature observation IDs differ")
        for row in p2:
            mask = Path(packet["asset_root"]) / "detection_2d" / str(row["mask_file"])
            if not mask.is_file() or _sha(mask) != str(row["mask_sha256"]):
                raise ValueError(f"P2 mask missing/hash mismatch: {mask}")
        for row in packet.get("features", []):
            path = (ROOT / str(row["feature_path"])).resolve()
            if not path.is_file() or _sha(path) != str(row["feature_file_sha256"]):
                raise ValueError(f"feature missing/hash mismatch: {path}")
            with np.load(path, allow_pickle=False) as archive:
                image = np.asarray(archive["image_feature"], dtype=np.float32)
                text = np.asarray(archive["text_feature"], dtype=np.float32)
            if image.shape != (512,) or text.shape != (512,) or not np.isfinite(image).all() or not np.isfinite(text).all():
                raise ValueError(f"feature shape/finite check failed: {path}")
    else:
        # This invokes the same adapter used before OccuSGSession.update_frame;
        # it validates world-frame detections and RGB-D metadata only.
        _occ_packet(packet, source)
    return {**clock_identity,
            "observation_count": len(packet.get("p2_detections", [])), "kind": kind}


def run_preflight(kind: str, packet_root: Path, source: Path) -> int:
    packets = sorted(packet_root.glob("packet-*.json"))
    if not packets:
        raise ValueError("preflight requires at least one current-frame packet")
    docs = [_read(path) for path in packets]
    identities = [(int(doc["frame_index"]), str(doc["frame_id"])) for doc in docs]
    if (len({fid for _, fid in identities}) != len(identities)
            or len({idx for idx, _ in identities}) != len(identities)
            or identities != sorted(identities)):
        raise ValueError("preflight packets must have unique IDs and strictly increasing frame indices")
    results = [_validate_current_packet(doc, source, kind) for doc in docs]
    print(json.dumps({"status": "preflight_pass", "kind": kind, "frames": results}, sort_keys=True), flush=True)
    return 0


def _write_update_marker(run_root: Path, kind: str, packet: dict, *, status: str,
                         error: str | None = None, mutation_possible: bool = True) -> None:
    branch = "objects" if kind == "conceptgraphs" else "rooms"
    path = run_root / "branches" / branch / "updates" / f"frame-{int(packet['frame_index']):06d}.json"
    atomic_write_json(path, {"schema": "t2-d-update-marker/v1", "kind": kind,
        "status": status, "mutation_possible": mutation_possible,
        "source_frame_index": int(packet["frame_index"]), "source_frame_id": str(packet["frame_id"]),
        "source_hashes": dict(packet["source_hashes"]), "updated_utc": time.time(),
        "error": error})


def _init_conceptgraphs(source: Path, packet_paths: list[Path], run_root: Path,
                        seed: int | None = None):
    import torch
    from omegaconf import OmegaConf
    import scripts.run_conceptgraphs_cached_hybrid as cg
    from integration_v2.conceptgraphs_session import ConceptGraphsMappingSession
    from adapters.habitat.frames import frame_from_capture_record

    source_c2 = _read(source / "c2_run_manifest.json")
    packets = [_read(path) for path in packet_paths]
    p2_frames = []
    capture_by_frame = {}
    if packets:
        p2_doc = _read(source / "detection_2d/detection_2d_index.json")
        p2_by_frame = {str(row["frame_id"]): row for row in p2_doc["frames"]}
        capture = _read(source / "capture/capture_manifest.json")
        capture_by_frame = {str(row["frame_id"]): row for row in capture["frames"]}
        p2_frames = [p2_by_frame[p["frame_id"]] for p in packets]
    poses = {}
    for packet in packets:
        rgbd_frame, _frame_packet = frame_from_capture_record(packet["capture_record"], source / "capture")
        poses[str(packet["frame_id"])] = np.asarray(rgbd_frame.T_W_Copt.matrix, dtype=np.float64)
    rng_provenance = cg.seed_mapping_rngs(int(seed)) if seed is not None else None
    bridge, MapObjectList, spatial, visual, aggregate, merge_dets, to_dets, filter_gobs, denoise, filter_objs, merge_objs, merge_one = cg.make_reference_api()
    first_packet = packets[0] if packets else None
    state = SimpleNamespace(
        cfg=OmegaConf.create(cg.base_mapping_config()), cache=source,
        asset_root=Path(first_packet["asset_root"]) if first_packet else source,
        capture_root=Path(first_packet["capture_root"]) if first_packet else source / "capture",
        features={}, text_by_id={}, p2_frames=p2_frames,
        poses=poses, timeline_recorder=None, MapObjectList=MapObjectList, spatial=spatial,
        visual=visual, aggregate=aggregate, merge_dets=merge_dets, to_dets=to_dets,
        filter_gobs=filter_gobs, denoise=denoise, filter_objs=filter_objs,
        merge_objs=merge_objs, merge_one=merge_one, objects=MapObjectList(),
        bg_objects={}, ledger=[], filtered_ids_by_frame={}, class_names=[],
        capture_records=capture_by_frame,
        rng_provenance=rng_provenance,
    )
    state.features, state.text_by_id = _feature_rows(run_root / "packets", packet_paths) if packets else ({}, {})
    session = ConceptGraphsMappingSession.open(state=state,
        update_impl=cg._update_mapping_state, finalize_impl=cg._finalize_mapping_state)
    return cg, session, state


def _hydrate_current_conceptgraphs_packet(state: SimpleNamespace, packet: dict, source: Path) -> None:
    """Add only the current packet to the reference state before update_frame.

    This is used by the live worker mode.  It deliberately does not inspect a
    route manifest, future packet paths, or a future class registry.
    """
    fid = str(packet["frame_id"])
    if fid in state.capture_records:
        return
    state.asset_root = Path(packet.get("asset_root", state.asset_root))
    state.capture_root = Path(packet.get("capture_root", state.capture_root))
    record = packet.get("capture_record")
    if not isinstance(record, dict):
        raise ValueError("live ConceptGraphs packet requires current capture_record")
    state.capture_records[fid] = record
    state.p2_frames.append({"frame_id": fid, "frame_index": int(packet["frame_index"]),
                            "detections": list(packet.get("p2_detections", []))})
    from adapters.habitat.frames import frame_from_capture_record
    rgbd, _ = frame_from_capture_record(record, state.capture_root)
    state.poses[fid] = np.asarray(rgbd.T_W_Copt.matrix, dtype=np.float64)
    for row in packet.get("features", []):
        oid = str(row["observation_id"])
        feature_path = str(row["feature_path"])
        path = (ROOT / feature_path).resolve()
        if not path.is_file() or _sha(path) != str(row["feature_file_sha256"]):
            raise ValueError(f"live feature missing/hash mismatch: {path}")
        with np.load(path, allow_pickle=False) as archive:
            image = np.asarray(archive["image_feature"], dtype=np.float32)
            text = np.asarray(archive["text_feature"], dtype=np.float32)
        if image.shape != (512,) or text.shape != (512,):
            raise ValueError(f"live feature shape invalid for {oid}")
        state.features[oid] = {"feature_path": feature_path,
                               "feature_file_sha256": str(row["feature_file_sha256"])}
        state.text_by_id[oid] = text


def _write_object_state(run_root: Path, state: SimpleNamespace, packet: dict) -> tuple[Path, dict]:
    import scripts.run_conceptgraphs_cached_hybrid as cg
    frame_index = int(packet["frame_index"])
    fid = str(packet["frame_id"])
    rows = cg._stage_lineage(state.objects, state.filtered_ids_by_frame, state.p2_frames,
                             state.poses, "online_hypotheses")
    branch_root = run_root / "branches/objects"
    cloud_root = branch_root / "clouds"; snap_root = branch_root / "snapshots"
    cloud_root.mkdir(parents=True, exist_ok=True); snap_root.mkdir(parents=True, exist_ok=True)
    objects = []
    for ordinal, (row, obj) in enumerate(zip(rows, state.objects)):
        hyp = "hypothesis_" + str(row["lineage_key"])
        cloud = cloud_root / f"rev-{frame_index:06d}-{ordinal:04d}.npz"
        xyz = np.asarray(obj["pcd"].points, dtype=np.float32)
        colors = np.asarray(obj["pcd"].colors, dtype=np.float32)
        np.savez_compressed(cloud, xyz=xyz, rgb=np.clip(colors * 255, 0, 255).astype(np.uint8))
        objects.append({"hypothesis_id": hyp, "causal_object_id": hyp,
                        "cloud_npz": str(cloud.relative_to(branch_root)),
                        "cloud_sha256": _sha(cloud), "point_count": int(len(xyz)),
                        "observation_ids": row["observation_ids"],
                        "detection_count": row["detection_count"],
                        "distinct_frame_count": row["distinct_frame_count"],
                        "distinct_pose_count": row["distinct_pose_count"],
                        "class_history_raw": row["class_history_raw"],
                        "stage_status": "online_hypothesis"})
    state_path = snap_root / f"rev-{frame_index:06d}.json"
    atomic_write_json(state_path, {"schema": "t2-d-conceptgraphs-prefix-state/v1",
        "scene_id": str(packet["scene_id"]), "sequence_id": str(packet["sequence_id"]),
        "frame_index": frame_index, "frame_id": fid, "timestamp_s": float(packet["timestamp_s"]),
        "revision": frame_index + 1, "camera_pose_W_Copt": np.asarray(state.poses[fid]).tolist(),
        "objects": objects, "future_observations_used": False,
        "source_hashes": dict(packet["source_hashes"]),
        "stage": "online_hypotheses"})
    return state_path, {str(path.relative_to(run_root)): _sha(path) for path in cloud_root.glob(f"rev-{frame_index:06d}-*.npz")}


def _init_occusg(source: Path, run_root: Path):
    import shlex
    from adapters.habitat.frames import frame_from_capture_record
    from standalone_sg.config import load_config
    from standalone_sg.mapping import NativeRGBDMappingStage
    from standalone_sg.navigation import NavigationBlockGraph
    from standalone_sg.objects import ObjectAssociationManager
    from standalone_sg.pipeline import DudeM5RegionStage, SceneGraphBuilder
    from standalone_sg.regions.dude_client import DuDeConfig, DuDeWorkerClient
    from standalone_sg.regions.pipeline import RegionUpdateProcessor
    from integration_v2.occusg_session import OccuSGSession, close_resources, attach_cleanup_errors
    from standalone_sg.contracts.objects import Detection3D, DetectionBatch

    manifest = _read(source / "c2_run_manifest.json")
    command = manifest["stages"]["occusg_rooms"]["command"]
    def arg(name: str) -> Path:
        return Path(command[command.index(name) + 1]).resolve()
    pipeline_config, m2, dude = arg("--pipeline-config"), arg("--m2-m3-binary"), arg("--dude-binary")
    phase = load_config(pipeline_config)
    navigation = NavigationBlockGraph(phase.navigation)
    objects = ObjectAssociationManager(phase.object_association)
    mapping = region = worker = session = None
    try:
        mapping = NativeRGBDMappingStage(m2, resolution_m=0.05, timeout_s=20.0, default_max_range_m=10.0)
        worker = DuDeWorkerClient(dude, DuDeConfig(preprocessing_policy="source"))
        region = DudeM5RegionStage(worker, RegionUpdateProcessor(), capture_preprocessed_mask=True)
        session = OccuSGSession.open(builder=SceneGraphBuilder(mapping_stage=mapping,
            region_stage=region, navigation=navigation, objects=objects,
            config_hash=_sha(pipeline_config)))
    except BaseException as exc:
        errors = close_resources((region, worker, mapping))
        if errors:
            cleanup = RuntimeError("; ".join(str(x) for x in errors)); cleanup.errors = errors
            attach_cleanup_errors(exc, cleanup)
        raise
    return session, mapping, region


def _occ_packet(packet: dict, source: Path):
    from dataclasses import replace
    from adapters.habitat.frames import frame_from_capture_record
    from standalone_sg.contracts.objects import Detection3D, DetectionBatch
    capture_root = Path(packet.get("capture_root", source / "capture")).resolve()
    _, base = frame_from_capture_record(packet["capture_record"], capture_root)
    detections = []
    for row in packet.get("p3_detections", []):
        if row.get("status") != "accepted" or row.get("detection_3d") is None:
            continue
        d = row["detection_3d"]
        detections.append(Detection3D(
            detection_id=str(d["detection_id"]), timestamp_s=float(d["provider_metadata"].get("timestamp_s", packet["timestamp_s"])),
            center_W=tuple(float(x) for x in d["center_W"]), center_method=str(d.get("center_method", "M1")),
            class_name=d.get("class_name"), class_id=d.get("class_id"), confidence=d.get("confidence"),
            # center_W is already expressed in the canonical world frame;
            # retain the source camera frame in provider_metadata instead of
            # mislabelling the detection coordinate frame.
            frame_id="world", layer_id=str(d.get("layer_id", "floor_0")),
            extents_m=tuple(float(x) for x in d["extents_m"]) if d.get("extents_m") is not None else None,
            provider_metadata=d.get("provider_metadata", {})))
    batch = DetectionBatch(provider_id="t2_b2_p3_current_frame", frame_id=str(packet["frame_id"]),
                           timestamp_s=float(packet["timestamp_s"]), detections=tuple(detections),
                           provenance={"scope_semantics": "unavailable", "source_hashes": packet["source_hashes"]})
    return replace(base, detections=batch, source_metadata={
        "source_hashes": packet["source_hashes"], "future_manifest_required": False,
        "scope_semantics": "unavailable", "depth_units": "m",
        "depth_representation": "axial", "depth_scale_to_m": 1.0,
        "backprojection_stride": 1, "min_depth_m": 0.1, "max_depth_m": 10.0,
    })


def run_worker(kind: str, run_root: Path, packet_root: Path, source: Path, *, live: bool = False,
               ready_file: Path | None = None, run_id: str | None = None,
               bootstrap_id: str | None = None, route_config_sha256: str | None = None,
               seed: int | None = None) -> int:
    packet_paths: list[Path] = []
    packet_by_index = {} if live else {int(p.stem.split("-")[-1]): p for p in packet_root.glob("packet-*.json")}
    publisher = AcknowledgedPublisher(run_root, "objects" if kind == "conceptgraphs" else "rooms", timeout_s=600)
    ready_kind = "occusg_rooms" if kind == "occusg" else kind
    live_seen: set[str] = set()
    live_last_index = -1
    _execution_event(run_root, "worker_initialization_started", worker=ready_kind,
                     pid=os.getpid(), run_id=run_id, bootstrap_id=bootstrap_id,
                     interpreter=sys.executable, live=live)
    try:
        if kind == "conceptgraphs":
            cg, session, state = _init_conceptgraphs(source, [] if live else sorted(packet_by_index.values()), run_root,
                                                    seed=seed)
            terminal = None
        else:
            session, mapping, region = _init_occusg(source, run_root)
            terminal = None
    except BaseException as exc:
        _execution_event(run_root, "worker_initialization_failed", worker=ready_kind,
                         pid=os.getpid(), run_id=run_id, bootstrap_id=bootstrap_id,
                         error=f"{type(exc).__name__}: {exc}")
        raise
    _execution_event(run_root, "worker_initialized", worker=ready_kind,
                     pid=os.getpid(), run_id=run_id, bootstrap_id=bootstrap_id,
                     interpreter=sys.executable, live=live, model_load_count=0,
                     model_load_count_basis="mapper consumes current packet features; no detector/encoder is initialized here")
    if ready_file is not None:
        from integration_v2.camera_live_contract import host_monotonic_domain
        from integration_v2.camera_live_startup import ready_marker
        if not run_id or not bootstrap_id or not route_config_sha256:
            raise ValueError("live mapper readiness requires run/bootstrap/config ownership")
        atomic_write_json(ready_file, ready_marker(
            kind=("conceptgraphs" if kind == "conceptgraphs" else "occusg_rooms"),
            pid=__import__("os").getpid(), run_id=run_id, bootstrap_id=bootstrap_id,
            route_config_sha256=route_config_sha256, ready_monotonic_s=time.monotonic(),
            clock_domain=host_monotonic_domain()))
        _execution_event(run_root, "worker_ready", worker=ready_kind,
                         pid=os.getpid(), run_id=run_id, bootstrap_id=bootstrap_id,
                         interpreter=sys.executable)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            op = message.get("op")
            if op == "frame":
                path = Path(message["packet_path"]).resolve()
                if _sha(path) != message.get("packet_sha256"):
                    raise ValueError("dispatch packet hash mismatch")
                packet = _read(path)
                source_clock = validate_current_source_packet(packet, context=f"{kind} mapper dispatch")
                if live:
                    fid = str(packet["frame_id"])
                    findex = int(packet["frame_index"])
                    if fid in live_seen or findex <= live_last_index:
                        raise ValueError(f"duplicate/stale live packet before mutation: {fid}/{findex}")
                if kind == "conceptgraphs":
                    if live:
                        _hydrate_current_conceptgraphs_packet(state, packet, source)
                    update = {"frame_index": int(packet["frame_index"]), "frame_id": str(packet["frame_id"]),
                              "timestamp_s": float(packet["timestamp_s"]), "detections": packet["p2_detections"],
                              "capture_record": packet["capture_record"], "capture_root": packet["capture_root"],
                              "asset_root": packet["asset_root"]}
                    update_started_monotonic = time.monotonic()
                    _write_update_marker(run_root, kind, packet, status="update_started")
                    _execution_event(run_root, "mapper_update_started", worker=kind,
                                     pid=os.getpid(), run_id=run_id,
                                     frame_id=str(packet["frame_id"]), frame_index=int(packet["frame_index"]),
                                     source_hashes=packet["source_hashes"])
                    try:
                        started = time.perf_counter(); result = session.update_frame(update)
                    except BaseException as exc:
                        _write_update_marker(run_root, kind, packet, status="update_failed",
                                             error=f"{type(exc).__name__}: {exc}", mutation_possible=True)
                        raise
                    state_path, cloud_hashes = _write_object_state(run_root, state, packet)
                    processing_ms = (time.perf_counter() - started) * 1000.0
                    publisher.publish({"schema": "t2-d-branch-event/v1", "scene_id": packet["scene_id"],
                        "sequence_id": packet["sequence_id"], "source_frame_index": int(packet["frame_index"]),
                        "source_frame_id": packet["frame_id"], "source_timestamp_s": float(packet["timestamp_s"]),
                        "source_timestamp_clock_domain": source_clock["timestamp_clock_domain"],
                        "source_capture_wall_time_unix_s": source_clock["capture_wall_time_unix_s"],
                        "source_capture_monotonic_s": source_clock["capture_monotonic_s"],
                        "source_capture_monotonic_domain": source_clock["capture_monotonic_domain"],
                        "update_started_monotonic_s": update_started_monotonic,
                        "update_clock_domain": host_monotonic_domain(),
                        "source_revision": int(state_path.stem.split("-")[-1]) + 1,
                        "revision_kind": "conceptgraphs_prefix_online_hypotheses", "processing_ms": processing_ms,
                        "source_hashes": packet["source_hashes"]}, state_path=state_path, referenced_files=cloud_hashes)
                    _execution_event(run_root, "mapper_update_completed", worker=kind,
                                     pid=os.getpid(), run_id=run_id,
                                     frame_id=str(packet["frame_id"]), frame_index=int(packet["frame_index"]),
                                     processing_ms=processing_ms, source_hashes=packet["source_hashes"])
                else:
                    occ_packet = _occ_packet(packet, source)
                    update_started_monotonic = time.monotonic()
                    _write_update_marker(run_root, kind, packet, status="update_started")
                    _execution_event(run_root, "mapper_update_started", worker=kind,
                                     pid=os.getpid(), run_id=run_id,
                                     frame_id=str(packet["frame_id"]), frame_index=int(packet["frame_index"]),
                                     source_hashes=packet["source_hashes"])
                    try:
                        started = time.perf_counter(); snapshot, delta = session.update_frame(occ_packet)
                    except BaseException as exc:
                        _write_update_marker(run_root, kind, packet, status="update_failed",
                                             error=f"{type(exc).__name__}: {exc}", mutation_possible=True)
                        raise
                    from standalone_sg.serialization import write_snapshot
                    snap_root = run_root / "branches/rooms/snapshots"; snap_root.mkdir(parents=True, exist_ok=True)
                    state_path = snap_root / f"rev-{int(packet['frame_index']):06d}.json"
                    write_snapshot(state_path, snapshot)
                    processing_ms = (time.perf_counter() - started) * 1000.0
                    publisher.publish({"schema": "t2-d-branch-event/v1", "scene_id": packet["scene_id"],
                        "sequence_id": packet["sequence_id"], "source_frame_index": int(packet["frame_index"]),
                        "source_frame_id": packet["frame_id"], "source_timestamp_s": float(packet["timestamp_s"]),
                        "source_timestamp_clock_domain": source_clock["timestamp_clock_domain"],
                        "source_capture_wall_time_unix_s": source_clock["capture_wall_time_unix_s"],
                        "source_capture_monotonic_s": source_clock["capture_monotonic_s"],
                        "source_capture_monotonic_domain": source_clock["capture_monotonic_domain"],
                        "update_started_monotonic_s": update_started_monotonic,
                        "update_clock_domain": host_monotonic_domain(),
                        "source_revision": int(snapshot.revision), "revision_kind": "occusg_prefix_room_graph",
                        "processing_ms": processing_ms, "source_hashes": packet["source_hashes"]}, state_path=state_path)
                    _execution_event(run_root, "mapper_update_completed", worker=kind,
                                     pid=os.getpid(), run_id=run_id,
                                     frame_id=str(packet["frame_id"]), frame_index=int(packet["frame_index"]),
                                     processing_ms=processing_ms, source_hashes=packet["source_hashes"])
                if live:
                    live_seen.add(str(packet["frame_id"]))
                    live_last_index = int(packet["frame_index"])
                print(json.dumps({"status": "frame_published", "kind": kind, "frame_index": packet["frame_index"]}), flush=True)
            elif op == "finalize":
                _execution_event(run_root, "mapper_terminal_started", worker=kind,
                                 pid=os.getpid(), run_id=run_id)
                if kind == "conceptgraphs":
                    terminal = session.finalize()
                    atomic_write_json(run_root / "branches/objects/terminal.json", {
                        "schema": "t2-d-conceptgraphs-terminal/v1", "stage": "terminal_postprocess",
                        "final": terminal, "source_frames": list(session.processed_frame_ids),
                        "rng_provenance": state.rng_provenance})
                else:
                    terminal = session.finalize()
                    atomic_write_json(run_root / "branches/rooms/terminal.json", {
                        "schema": "t2-d-occusg-terminal/v1", "stage": "terminal_flush",
                        "source_frames": list(session.processed_frame_ids), "snapshot_revision": int(terminal[0].revision)})
                session.close()
                _execution_event(run_root, "mapper_terminal_completed", worker=kind,
                                 pid=os.getpid(), run_id=run_id)
                print(json.dumps({"status": "finalized", "kind": kind}), flush=True)
                return 0
            elif op == "close":
                session.close(); return 0
            else:
                raise ValueError(f"unsupported worker command: {op}")
    finally:
        if session is not None and session.status not in {"closed", "finalized"}:
            try: session.close()
            except BaseException: pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--worker", choices=("conceptgraphs", "occusg"), required=True)
    parser.add_argument("--run-root", type=Path, required=True); parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--bootstrap-id")
    parser.add_argument("--route-config-sha256")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--live", action="store_true",
                        help="config-only worker mode; packets arrive one at a time and are not preloaded")
    args = parser.parse_args()
    try:
        if args.preflight:
            return run_preflight(args.worker, args.packet_root.resolve(), args.source.resolve())
        return run_worker(args.worker, args.run_root.resolve(), args.packet_root.resolve(), args.source.resolve(),
                          live=args.live, ready_file=args.ready_file.resolve() if args.ready_file else None,
                          run_id=args.run_id, bootstrap_id=args.bootstrap_id,
                          route_config_sha256=args.route_config_sha256, seed=args.seed)
    except BaseException as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
