#!/usr/bin/env python3
"""One bounded Habitat -> perception -> persistent mappers pilot.

The pilot is deliberately small and file-backed.  Habitat publishes an
immutable keyframe only after its RGB-D files are complete; persistent P2,
P3, OpenCLIP, ConceptGraphs and OccuSG workers consume that current frame.
No future route manifest is loaded by the live workers.
"""
from __future__ import annotations

import argparse, json, os, shutil, subprocess, sys, time, hashlib, resource, threading, uuid, math
import dataclasses, inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))

import numpy as np
from integration_v2.camera_live_contract import (
    CameraLiveContractError,
    BoundedPublishedKeyframeQueue,
    build_joined_perception_packet,
    frame_stage_input,
    validate_clock_fields,
    validate_current_source_packet,
    validate_event_file_hashes,
    validate_published_event,
    validate_stage_response,
    read_complete_jsonl,
    assert_no_incomplete_jsonl_tail,
    host_monotonic_domain,
    send_mapper_pair,
    validate_mapper_pair_events,
    evaluate_live_overlap,
)
from integration_v2.camera_live_startup import (
    CAMERA_PRODUCER_MANIFEST, EXPECTED_WORKERS, build_bootstrap_document,
    mark_camera_capture_completed, mark_camera_capture_started,
    mark_camera_runner_failure, mark_camera_runner_preflight_stopped,
    mark_camera_runner_spawned, mark_camera_startup_validated,
    ready_marker, terminal_stage_statuses, terminalize_pilot_documents,
    validate_viewer_start_record,
    validate_bootstrap_before_capture, validate_route_startup,
)

# Habitat's locked environment is Python 3.9 while the shared contract
# modules use the Python 3.10 ``dataclass(slots=...)`` spelling.  This narrow
# compatibility shim affects only the orchestration process; worker
# interpreters remain the pinned environments and no package is changed.
if "slots" not in inspect.signature(dataclasses.dataclass).parameters:
    _dataclass = dataclasses.dataclass
    def _compat_dataclass(cls=None, **kwargs):
        kwargs.pop("slots", None)
        return _dataclass(cls, **kwargs) if cls is not None else (lambda c: _dataclass(c, **kwargs))
    dataclasses.dataclass = _compat_dataclass

def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush(); os.fsync(stream.fileno())

def execution_event(run_root: Path, event: str, **fields: Any) -> None:
    append_jsonl(Path(run_root) / "execution_ledger.jsonl", {
        "schema": "camera-live-execution-event/v1", "event": event,
        "event_monotonic_s": time.monotonic(), "event_wall_time_unix_s": time.time(),
        "clock_domain": host_monotonic_domain(), **fields})

def _worker_execution_fields(args: argparse.Namespace, *, fake: bool) -> dict[str, Any]:
    """Return optional run identity for workers also exercised in unit fixtures."""
    return {"run_id": getattr(args, "run_id", None),
            "bootstrap_id": getattr(args, "bootstrap_id", None),
            "fake": bool(fake)}

def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def resolve_timeout_budgets(config: dict[str, Any], *, overall_timeout_s: float,
                            camera_timeout_override_s: Optional[float] = None) -> dict[str, Any]:
    """Keep the route wall deadline distinct from the pipeline deadline.

    The overall deadline includes worker readiness, capture, queue drain,
    terminal worker finalization, and semantic finalization. The route runner
    receives the smaller route-specific wall budget, further bounded by the
    remaining overall budget when it is spawned.
    """
    overall = float(overall_timeout_s)
    if not math.isfinite(overall) or overall <= 0.0:
        raise ValueError("overall pipeline timeout must be positive and finite")
    if camera_timeout_override_s is None:
        proposal = config.get("route_budget_proposal", {})
        configured = proposal.get("camera_runner_timeout_s", overall) if isinstance(proposal, dict) else overall
    else:
        configured = camera_timeout_override_s
    camera_requested = float(configured)
    if not math.isfinite(camera_requested) or camera_requested <= 0.0:
        raise ValueError("camera/route timeout must be positive and finite")
    return {
        "overall_pipeline_timeout_s": overall,
        "overall_deadline_includes_queue_drain": True,
        "camera_route_timeout_configured_s": camera_requested,
        "camera_route_timeout_effective_s": min(camera_requested, overall),
        "camera_route_timeout_capped_by_overall": camera_requested > overall,
        "source": ("cli_override" if camera_timeout_override_s is not None else
                   "route_budget_proposal.camera_runner_timeout_s" if isinstance(config.get("route_budget_proposal"), dict)
                   and "camera_runner_timeout_s" in config["route_budget_proposal"] else "overall_timeout_fallback"),
    }

def _write_worker_ready(args: argparse.Namespace, kind: str) -> None:
    if not getattr(args, "ready_file", None):
        return
    atomic_json(args.ready_file, ready_marker(kind=kind, pid=os.getpid(),
        run_id=args.run_id, bootstrap_id=args.bootstrap_id,
        route_config_sha256=args.route_config_sha256,
        ready_monotonic_s=time.monotonic(), clock_domain=host_monotonic_domain()))
    execution_event(args.run_root, "worker_ready", worker=kind, pid=os.getpid(),
                    run_id=getattr(args, "run_id", None),
                    bootstrap_id=getattr(args, "bootstrap_id", None),
                    interpreter=sys.executable)


def fake_readiness_worker(kind: str, args: argparse.Namespace) -> int:
    """Dry-run IPC peer: publishes owned readiness, then waits for close."""
    execution_event(args.run_root, "worker_initialization_started", worker=kind,
                    pid=os.getpid(), run_id=args.run_id, bootstrap_id=args.bootstrap_id,
                    fake=True, interpreter=sys.executable)
    if kind == "p2":
        (args.run_root / "perception" / "p2").mkdir(parents=True, exist_ok=True)
    elif kind == "conceptgraphs":
        (args.run_root / "branches" / "objects" / "acks").mkdir(parents=True, exist_ok=True)
    elif kind == "occusg_rooms":
        (args.run_root / "branches" / "rooms" / "acks").mkdir(parents=True, exist_ok=True)
    _write_worker_ready(args, kind)
    execution_event(args.run_root, "worker_initialized", worker=kind, pid=os.getpid(),
                    run_id=args.run_id, bootstrap_id=args.bootstrap_id,
                    fake=True, model_load_count=0)
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        if message.get("op") == "close":
            return 0
        raise ValueError("startup dry-run workers accept only close after readiness")
    return 0

def _clock_response(packet: dict, *, stage_started_monotonic_s: float) -> dict:
    clocks = validate_clock_fields(packet, context="worker response")
    return {**clocks, "frame_id": str(packet["frame_id"]),
            "frame_index": int(packet["frame_index"]),
            "source_hashes": dict(packet["source_hashes"]),
            "stage_started_monotonic_s": float(stage_started_monotonic_s),
            "stage_clock_domain": packet["capture_monotonic_domain"]}


def worker_main(kind: str, args: argparse.Namespace, *, test_hooks: dict | None = None) -> int:
    """Persistent worker entrypoint; each process uses its locked environment."""
    if kind == "p2":
        if test_hooks is None:
            from scripts.run_replica_cad_instance_segmentation import P2DetectorSession, canonical_class
        else:
            canonical_class = test_hooks.get("canonical_class", lambda raw, _mapping: raw)
        profile = read(args.profile)
        names = [x.strip() for x in args.names.read_text(encoding="utf-8").splitlines() if x.strip()]
        weights = args.weights.resolve()
        execution_event(args.run_root, "worker_initialization_started", worker=kind,
                        pid=os.getpid(), **_worker_execution_fields(args, fake=test_hooks is not None),
                        interpreter=sys.executable)
        try:
            if test_hooks is None:
                session = P2DetectorSession.open(weights=weights, names=names,
                                                 preprocess=profile["runtime"] | profile["preprocessing"],
                                                 providers=["CPUExecutionProvider"])
            else:
                session = test_hooks["session"]
        except BaseException as exc:
            execution_event(args.run_root, "worker_initialization_failed", worker=kind,
                            pid=os.getpid(), **_worker_execution_fields(args, fake=test_hooks is not None),
                            error=f"{type(exc).__name__}: {exc}")
            raise
        execution_event(args.run_root, "worker_initialized", worker=kind, pid=os.getpid(),
                        **_worker_execution_fields(args, fake=test_hooks is not None),
                        model_load_count=int(getattr(session, "model_load_count", 1)),
                        model="locked_ONNX_detector")
        _write_worker_ready(args, kind)
        canonical = profile["vocabulary"].get("canonical_map", {})
        out_root = args.run_root / "perception" / "p2"
        out_root.mkdir(parents=True, exist_ok=True)
        try:
            for line in sys.stdin:
                if not line.strip(): continue
                msg = json.loads(line); op = msg.get("op")
                if op == "close": break
                if op != "frame": raise ValueError("unsupported P2 operation")
                frame = read(Path(msg["frame_json"]))
                validate_current_source_packet(frame, context="P2 runtime input")
                frame_dir = Path(frame["frame_dir"])
                rgb = np.load(frame_dir / "rgb.npy", allow_pickle=False)
                depth = np.load(frame_dir / "depth_m.npy", allow_pickle=False)
                valid = np.load(frame_dir / "valid_mask.npy", allow_pickle=False)
                meta = read(frame_dir / "metadata.json")
                stage_started = time.monotonic()
                detections = session.update_frame(frame_stage_input(frame, {
                    "rgb": rgb, "rgbd": {"depth_m": depth, "valid_mask": valid,
                    "depth_units": meta.get("depth_units"), "depth_representation": meta.get("depth_representation")},
                    "K": meta.get("intrinsics"), "intrinsics": meta.get("intrinsics"),
                    "pose": meta.get("base_pose_canonical")}, stage="P2"))
                # Publish masks directly under the immutable mapper asset root;
                # the packet then contains a stable path relative to
                # ``asset_root/detection_2d``.
                mask_root = args.run_root / "packets" / "assets" / "detection_2d" / "masks"; mask_root.mkdir(parents=True, exist_ok=True)
                rows=[]
                for i, det in enumerate(detections):
                    name=f"{frame['frame_id']}__{i:04d}.npy"; mp=mask_root/name
                    np.save(mp, np.asarray(det["mask"], dtype=bool), allow_pickle=False)
                    rows.append({"detection_id": f"{frame['frame_id']}:det-{i:04d}",
                        "class_id": int(det["class_id"]), "class_raw": str(det["class_raw"]),
                        "class_canonical": canonical_class(str(det["class_raw"]), canonical),
                        "score_raw": float(det["score"]), "bbox_xyxy": list(det["bbox_xyxy"]),
                        "mask_file": str(mp.relative_to(args.run_root / "packets" / "assets" / "detection_2d")),
                        "mask_sha256": sha(mp), "image_shape_hw": [int(rgb.shape[0]), int(rgb.shape[1])]})
                atomic_json(Path(msg["response_json"]), {"status":"completed", **_clock_response(frame, stage_started_monotonic_s=stage_started), "detections":rows,
                    "model_load_count": int(session.model_load_count), "processed_frame_ids": list(session.processed_frame_ids)})
        finally:
            session.close()
        return 0
    if kind == "p3":
        from adapters.habitat.frames import frame_from_capture_record, habitat_world_to_canonical_transform, habitat_camera_intrinsics
        from integration_v2.perception_feature_sessions import P3GeometrySession
        profile = read(args.profile)
        execution_event(args.run_root, "worker_initialization_started", worker=kind,
                        pid=os.getpid(), **_worker_execution_fields(args, fake=test_hooks is not None),
                        interpreter=sys.executable)
        try:
            session = P3GeometrySession.open(profile=profile) if test_hooks is None else test_hooks["session"]
        except BaseException as exc:
            execution_event(args.run_root, "worker_initialization_failed", worker=kind,
                            pid=os.getpid(), **_worker_execution_fields(args, fake=test_hooks is not None),
                            error=f"{type(exc).__name__}: {exc}")
            raise
        execution_event(args.run_root, "worker_initialized", worker=kind, pid=os.getpid(),
                        **_worker_execution_fields(args, fake=test_hooks is not None),
                        model_load_count=int(getattr(session, "model_load_count", 0)),
                        model="P3_geometry_session")
        _write_worker_ready(args, kind)
        try:
            for line in sys.stdin:
                if not line.strip(): continue
                msg=json.loads(line); op=msg.get("op")
                if op == "close": break
                if op != "frame": raise ValueError("unsupported P3 operation")
                packet=read(Path(msg["packet_json"])); rec=packet["capture_record"]
                validate_current_source_packet(packet, context="P3 runtime input")
                rgbd, fp = frame_from_capture_record(rec, Path(packet["capture_root"]))
                p2=[]
                for d in packet["p2_detections"]:
                    mask_path=Path(packet["asset_root"])/"detection_2d"/str(d["mask_file"])
                    if not mask_path.is_file() or sha(mask_path)!=str(d.get("mask_sha256", "")):
                        raise CameraLiveContractError(f"P3 input mask missing/hash mismatch: {mask_path}")
                    mask=np.load(mask_path, allow_pickle=False)
                    p2.append({**d, "mask": mask})
                stage_started = time.monotonic()
                result=session.update_frame(frame_stage_input(packet, {
                    "rgb":rgbd.rgb, "rgbd":{"depth_m":rgbd.depth_m,"valid_mask":rgbd.valid_mask},
                    "intrinsics":rgbd.intrinsics, "T_W_Copt":rgbd.T_W_Copt, "p2_detections":p2,
                    }, stage="P3"))
                clean=[]
                for row in result.get("detections",[]):
                    x=dict(row); d=x.get("detection_3d")
                    if d is not None:
                        x["detection_3d"]={"detection_id":d.detection_id,"center_W":list(d.center_W),
                          "center_method":d.center_method,"class_name":d.class_name,"class_id":d.class_id,
                          "confidence":d.confidence,"layer_id":d.layer_id,"extents_m":list(d.extents_m) if d.extents_m else None,
                          "provider_metadata":dict(d.provider_metadata)}
                    clean.append(x)
                result["detections"]=clean
                atomic_json(Path(msg["response_json"]), {"status":"completed", **_clock_response(packet, stage_started_monotonic_s=stage_started),
                    "detections":result.get("detections", []), "result":result,
                    "processed_frame_ids":list(session.processed_frame_ids)})
        finally: session.close()
        return 0
    if kind == "features":
        from integration_v2.perception_feature_sessions import FeatureContract, OpenCLIPFeatureSession
        contract=FeatureContract(checkpoint_sha256=args.checkpoint_sha256, model_fingerprint=args.model_fingerprint,
            tokenizer_id="open_clip.get_tokenizer('ViT-B-32')", preprocessing_id=args.preprocessing_id,
            prompt_policy="exact cached class_raw string; no prompt template or canonicalization")
        execution_event(args.run_root, "worker_initialization_started", worker=kind,
                        pid=os.getpid(), **_worker_execution_fields(args, fake=test_hooks is not None),
                        interpreter=sys.executable)
        try:
            session=OpenCLIPFeatureSession.open(checkpoint=args.checkpoint, contract=contract) if test_hooks is None else test_hooks["session"]
        except BaseException as exc:
            execution_event(args.run_root, "worker_initialization_failed", worker=kind,
                            pid=os.getpid(), **_worker_execution_fields(args, fake=test_hooks is not None),
                            error=f"{type(exc).__name__}: {exc}")
            raise
        execution_event(args.run_root, "worker_initialized", worker=kind, pid=os.getpid(),
                        **_worker_execution_fields(args, fake=test_hooks is not None),
                        model_load_count=int(getattr(session, "model_load_count", 1)),
                        model="locked_OpenCLIP_checkpoint")
        _write_worker_ready(args, kind)
        try:
            for line in sys.stdin:
                if not line.strip(): continue
                msg=json.loads(line); op=msg.get("op")
                if op == "close": break
                if op != "frame": raise ValueError("unsupported feature operation")
                packet=read(Path(msg["packet_json"]))
                validate_current_source_packet(packet, context="OpenCLIP runtime input")
                frame_dir=Path(packet["frame_dir"])
                rgb=np.load(frame_dir/"rgb.npy", allow_pickle=False)
                p2=packet["p2_detections"]
                stage_started = time.monotonic()
                result=session.update_frame(frame_stage_input(packet, {
                    "rgb":rgb,"p2_detections":p2}, stage="OpenCLIP"))
                rows=[]; out=args.run_root/"packets"/"features"; out.mkdir(parents=True, exist_ok=True)
                for row in result.get("rows",[]):
                    fp=out/(str(row["observation_id"]).replace(":","__")+".npz")
                    np.savez_compressed(fp,image_feature=np.asarray(row["image_feature"],dtype=np.float32),
                        text_feature=np.asarray(row["text_feature"],dtype=np.float32))
                    rows.append({k:v for k,v in row.items() if k not in {"image_feature","text_feature"}} |
                        {"feature_path":str(fp.relative_to(ROOT)),"feature_file_sha256":sha(fp)})
                atomic_json(Path(msg["response_json"]), {"status":"completed", **_clock_response(packet, stage_started_monotonic_s=stage_started), "rows":rows,
                    "result":{**result,"rows":rows},
                    "model_load_count":int(session.model_load_count),"image_encode_count":int(session.image_encode_count),
                    "text_encode_count":int(session.text_encode_count),"text_cache_size":int(session.text_cache_size)})
        finally: session.close()
        return 0
    raise ValueError(kind)

def send(proc, msg):
    proc.stdin.write(json.dumps(msg, sort_keys=True)+"\n"); proc.stdin.flush()

def wait_json(path: Path, timeout: float = 120.0):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if path.is_file(): return read(path)
        time.sleep(.02)
    raise TimeoutError(f"worker response timeout: {path}")

def wait_worker_json(proc, path: Path, *, stage: str, timeout: float = 120.0):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if path.is_file():
            response=read(path)
            if response.get("status") != "completed":
                raise RuntimeError(f"{stage} stage failed: {response.get('error', response.get('status'))}")
            return response
        code=proc.poll()
        if code is not None:
            raise RuntimeError(f"{stage} worker exited with code {code} before publishing a response")
        time.sleep(.02)
    raise TimeoutError(f"{stage} worker response timeout: {path}")

def remaining_before(deadline: float, stage: str) -> float:
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"camera-live pilot runtime cap reached before {stage}")
    return remaining

def confirm_browser_ready(run_root: Path, run_id: str | None = None) -> dict:
    """Validate the served waiting D2 bundle and release only this pilot run."""
    run_root = Path(run_root).resolve()
    manifest = read(run_root / "run_manifest.json")
    expected_run_id = str(manifest.get("run_id", ""))
    if not expected_run_id or (run_id is not None and run_id != expected_run_id):
        raise ValueError("camera-live run ID does not match its manifest")
    if manifest.get("schema") != "camera-live-pilot/v1" or manifest.get("status") not in {
            "waiting_for_viewer_ready", "started"}:
        raise ValueError("camera-live pilot is not waiting for or already released by browser confirmation")
    viewer = run_root / "viewer"
    pointer_path = viewer / "d2_viewer_entrypoint.json"
    pointer = read(pointer_path)
    url = manifest.get("viewer", {}).get("url")
    if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
        raise ValueError("camera-live manifest has no loopback D2 URL")
    from scripts.build_d2_xyz_viewer import validate_served_entrypoint
    bundle = validate_served_entrypoint(viewer, url)
    if bundle["pointer"] != pointer:
        raise ValueError("served D2 pointer differs from the run-local pointer")
    start_path = run_root / "control/start.json"
    record = {"schema": "d1-explicit-viewer-start/v1", "run_id": expected_run_id,
        "confirmed_browser_ready": True, "entrypoint_pointer_sha256": sha(pointer_path),
        "entrypoint": pointer["entrypoint"], "url": url,
        "started_by": "camera-live-pilot-confirm-browser-ready",
        "started_utc": datetime.now(timezone.utc).isoformat()}
    if start_path.exists():
        existing = read(start_path)
        validate_viewer_start_record(existing, run_id=expected_run_id, pointer=pointer,
                                     pointer_sha256=sha(pointer_path), expected_url=url,
                                     not_before_utc=manifest.get("created_utc"))
        if (manifest.get("status") == "started"
                and manifest.get("viewer", {}).get("browser_ready_start_signal_sha256") != sha(start_path)):
            raise ValueError("started run manifest is not bound to its existing browser start marker")
    else:
        atomic_json(start_path, record)
        existing = read(start_path)
        validate_viewer_start_record(existing, run_id=expected_run_id, pointer=pointer,
                                     pointer_sha256=sha(pointer_path), expected_url=url,
                                     not_before_utc=manifest.get("created_utc"))
    ledger=run_root/"execution_ledger.jsonl"
    prior=[]
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            try: prior.append(json.loads(line))
            except json.JSONDecodeError: raise ValueError("execution ledger has an incomplete tail before browser confirmation")
    bound=[row for row in prior if row.get("event")=="browser_ready_confirmed"
           and row.get("run_id")==expected_run_id
           and row.get("start_signal_sha256")==sha(start_path)]
    if len(bound)>1:
        raise ValueError("execution ledger contains duplicate browser confirmation events")
    if not bound:
        execution_event(run_root,"browser_ready_confirmed",run_id=expected_run_id,url=url,
                        start_signal_sha256=sha(start_path),dry_run=bool(manifest.get("startup_dry_run")))
    return {"entrypoint": pointer["entrypoint"], "url": url,
            "start_signal_sha256": sha(start_path), "served_entrypoint_validated": True}

def _wait_ready_workers(root: Path, workers: list[tuple[str, Any]], *, run_id: str,
                        bootstrap_id: str, route_config_sha256: str,
                        timeout_s: float = 300.0,
                        marker_kinds: dict[str, str] | None = None) -> dict:
    deadline=time.monotonic()+timeout_s
    marker_kinds = marker_kinds or {}
    expected=[kind for kind,_ in workers]
    while True:
        dead=[(kind,proc.poll()) for kind,proc in workers if proc.poll() is not None]
        if dead:
            raise RuntimeError(f"workers exited before ready: {dead}")
        missing=[kind for kind in expected if not (root/"ready"/(kind+".json")).is_file()]
        if not missing:
            docs={kind:read(root/"ready"/(kind+".json")) for kind in expected}
            if any(docs[kind].get("schema")!="camera-live-worker-ready/v2"
                   or docs[kind].get("kind")!=marker_kinds.get(kind, kind) or docs[kind].get("run_id") != run_id
                   or docs[kind].get("bootstrap_id") != bootstrap_id
                   or docs[kind].get("route_config_sha256") != route_config_sha256
                   or docs[kind].get("pid") != proc.pid
                   for kind,proc in workers):
                raise RuntimeError("worker readiness marker schema/kind mismatch")
            domains={doc.get("clock_domain") for doc in docs.values()}
            if len(domains)!=1 or next(iter(domains)) != host_monotonic_domain():
                raise RuntimeError("workers do not share the current host monotonic clock domain")
            return {"clock_domain":next(iter(domains)),"workers":docs}
        if time.monotonic()>=deadline:
            raise TimeoutError(f"workers did not become ready before camera start: {missing}")
        time.sleep(.05)

def _stop_owned_processes(processes: list[Any], *, timeout_s: float = 5.0) -> list[dict]:
    """Stop only process handles created by this run and report cleanup state."""
    rows=[]
    for proc in processes:
        if proc is None or proc.poll() is not None:
            continue
        proc.terminate()
    for proc in processes:
        if proc is None:
            continue
        try:
            code=proc.wait(timeout=timeout_s)
            rows.append({"pid":proc.pid,"returncode":code,"status":"exited"})
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                code=proc.wait(timeout=2.0)
                rows.append({"pid":proc.pid,"returncode":code,"status":"killed_after_timeout"})
            except subprocess.TimeoutExpired:
                rows.append({"pid":proc.pid,"returncode":None,"status":"still_alive_after_kill"})
    return rows

def write_capture_packet(root: Path, packet: dict, keyframe_result: dict, route_cfg: dict) -> dict:
    fid=str(packet["frame_id"]); src=root/keyframe_result["path"]
    if str(keyframe_result.get("frame_id")) != fid:
        raise CameraLiveContractError("published keyframe result frame_id mismatch")
    clocks = validate_clock_fields(packet.get("metadata", {}), context="published keyframe")
    declared = keyframe_result.get("files_sha256")
    if not isinstance(declared, dict) or set(declared) != {"rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"}:
        raise CameraLiveContractError("published keyframe has incomplete raw file hashes")
    validate_event_file_hashes(src, declared)
    source_meta = read(src / "metadata.json")
    if source_meta.get("raw_files_sha256") != {k: declared[k] for k in ("rgb.npy", "depth_m.npy", "valid_mask.npy")}:
        raise CameraLiveContractError("published keyframe metadata hashes differ from event")
    if source_meta.get("frame_id") != fid or source_meta.get("frame_index") != packet.get("metadata", {}).get("frame_index"):
        raise CameraLiveContractError("published keyframe metadata identity differs from event")
    for field in ("scene_id", "sequence_id"):
        if source_meta.get(field) != packet.get("metadata", {}).get(field):
            raise CameraLiveContractError(f"published keyframe metadata {field} differs from event")
        if route_cfg.get(field) is not None and source_meta.get(field) != route_cfg.get(field):
            raise CameraLiveContractError(f"published keyframe metadata {field} differs from locked route")
    for field, expected in clocks.items():
        if source_meta.get(field) != expected or packet["metadata"].get(field) != expected:
            raise CameraLiveContractError(f"published keyframe {field} lineage mismatch")
    cap=root/"capture"/"frames"/fid; cap.mkdir(parents=True, exist_ok=False)
    for name in ("rgb.npy","depth_m.npy","valid_mask.npy","metadata.json"):
        shutil.copy2(src/name, cap/name)
    run_manifest_path=root/"run_manifest.json"
    if not run_manifest_path.is_file():
        raise CameraLiveContractError("camera-live run manifest is required before writing capture packet")
    run_manifest=read(run_manifest_path)
    run_id=str(run_manifest.get("run_id", ""))
    if not run_id:
        raise CameraLiveContractError("camera-live run manifest is missing run_id")
    wall_domain=source_meta.get("capture_wall_time_clock_domain")
    if wall_domain is None and source_meta.get("schema") == "smooth-route-raw-keyframe/v1":
        # Compatibility for immutable T1.1 keyframes written before the
        # explicit domain field was added: the field name itself is Unix time.
        wall_domain="unix_epoch_utc"
    if wall_domain != "unix_epoch_utc":
        raise CameraLiveContractError("published keyframe wall clock domain is unsupported or missing")
    packet_wall_domain=packet["metadata"].get("capture_wall_time_clock_domain")
    if packet_wall_domain not in (None, wall_domain):
        raise CameraLiveContractError("published keyframe wall clock domain differs between event and stored metadata")
    meta=dict(packet["metadata"])
    h=np.asarray(meta["sensor_pose_habitat"],dtype=float)
    b=np.asarray(meta["base_pose_habitat"],dtype=float)
    meta.update({"hfov_deg":meta["horizontal_fov_deg"],"scene_file":route_cfg["scene_asset"],
        "navmesh_used_for_route":True,"min_depth_m":route_cfg["min_depth_m"],"max_depth_m":route_cfg["max_depth_m"],
        "canonical_from_habitat": [[1,0,0,0],[0,0,-1,0],[0,1,0,0],[0,0,0,1]],
        "sensor_position_habitat": h[:3,3].tolist(),"sensor_rotation_habitat":h[:3,:3].tolist(),
        "base_pose_canonical":packet["metadata"]["base_pose_canonical"],
        "capture_wall_time_clock_domain":wall_domain,
        "run_id":run_id,"capture_record_schema":"camera-live-capture-record/v2"})
    array_hashes={n:sha(cap/n) for n in ("rgb.npy","depth_m.npy","valid_mask.npy")}
    meta["raw_files_sha256"]=array_hashes
    atomic_json(cap/"metadata.json",meta)
    hashes={n:sha(cap/n) for n in ("rgb.npy","depth_m.npy","valid_mask.npy","metadata.json")}
    record={"schema":"camera-live-capture-record/v2","run_id":run_id,
        "frame_id":fid,"frame_index":int(packet["metadata"]["frame_index"]),
        "scene_id":str(packet["metadata"]["scene_id"]),"sequence_id":str(packet["metadata"]["sequence_id"]),
        "capture_wall_time_clock_domain":"unix_epoch_utc",**clocks,
        "frame_dir":str(cap.relative_to(root/"capture")),
        "raw_files_sha256":hashes, "width":int(meta["width"]),"height":int(meta["height"]),
        "hfov_deg":float(meta["hfov_deg"]),"calibration_id":str(meta["calibration_id"]),"min_depth_m":float(meta["min_depth_m"]),"max_depth_m":float(meta["max_depth_m"])}
    return {"schema":"camera-live-frame-packet/v2","run_id":run_id,
            "capture_record":record,"capture_root":str(root/"capture"),"frame_dir":str(cap),"frame_id":fid,
            "frame_index":record["frame_index"],**clocks,
            "capture_wall_time_clock_domain":"unix_epoch_utc",
            "scene_id":record["scene_id"],"sequence_id":record["sequence_id"],"source_hashes":hashes}

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--config",type=Path); ap.add_argument("--output",type=Path); ap.add_argument("--timeout-s",type=float,default=240,
        help="overall wall-clock budget for bootstrap through queue drain and terminal stages")
    ap.add_argument("--camera-timeout-s",type=float,default=None,
        help="route/capture subprocess wall timeout; defaults to the locked route budget proposal")
    ap.add_argument("--confirm-browser-ready",action="store_true",
                    help="after visually confirming waiting_for_first_revision, validate served D2 and release a waiting camera-live pilot")
    ap.add_argument("--source",type=Path); ap.add_argument("--port",type=int,default=8776)
    ap.add_argument("--room-belief-config",type=Path,default=ROOT/"configs/room_semantics/t4_live_room_belief_v1.json")
    ap.add_argument("--worker",choices=["p2","p3","features"]); ap.add_argument("--run-root",type=Path)
    ap.add_argument("--ready-file",type=Path); ap.add_argument("--run-id",default=None)
    ap.add_argument("--bootstrap-id",default=None); ap.add_argument("--route-config-sha256",default=None)
    ap.add_argument("--fake-readiness-worker",choices=EXPECTED_WORKERS,default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--startup-dry-run",action="store_true",
                    help="exercise real bootstrap, D2 HTTP, ready IPC and route preflight using fake workers; no models/capture")
    ap.add_argument("--profile",type=Path); ap.add_argument("--names",type=Path); ap.add_argument("--weights",type=Path)
    ap.add_argument("--checkpoint",type=Path); ap.add_argument("--checkpoint-sha256",default=""); ap.add_argument("--model-fingerprint",default=""); ap.add_argument("--preprocessing-id",default="")
    a=ap.parse_args()
    if a.confirm_browser_ready:
        if a.output is None:
            raise SystemExit("--output must name the waiting camera-live run directory")
        try:
            result=confirm_browser_ready(a.output, a.run_id)
            print(json.dumps({"status":"browser_ready_confirmed",**result},sort_keys=True),flush=True)
            return 0
        except BaseException as exc:
            print(f"Browser-ready confirmation failed: {type(exc).__name__}: {exc}",file=sys.stderr,flush=True)
            return 1
    if a.fake_readiness_worker:
        return fake_readiness_worker(a.fake_readiness_worker, a)
    if a.worker: return worker_main(a.worker,a)
    if a.config is None or a.output is None or a.source is None:
        raise SystemExit("--config, --output, and --source are required outside worker mode")
    pilot_started_monotonic=time.monotonic()
    pilot_deadline=pilot_started_monotonic+float(a.timeout_s)
    route = validate_route_startup(a.config, a.output, a.timeout_s)
    timeouts = resolve_timeout_budgets(route["config"], overall_timeout_s=a.timeout_s,
                                       camera_timeout_override_s=a.camera_timeout_s)
    cfg=route["config"]; root=route["output_root"]; source=a.source.expanduser().resolve()
    if not source.is_dir() or not (source/"c2_run_manifest.json").is_file():
        raise FileNotFoundError(f"C2 source run/manifest is missing: {source}")
    source_manifest_sha256=sha(source/"c2_run_manifest.json")
    root.mkdir(parents=True,exist_ok=False)
    (root/"logs").mkdir(); (root/"control").mkdir(); (root/"ready").mkdir()
    (root/"packets"/"assets"/"detection_2d"/"masks").mkdir(parents=True)
    (root/"execution_ledger.jsonl").touch(exist_ok=False)
    run_id=root.name
    bootstrap_id=uuid.uuid4().hex
    bootstrap=build_bootstrap_document(run_id=run_id,bootstrap_id=bootstrap_id,route=route,
        source_root=source,source_manifest_sha256=source_manifest_sha256,
        clock_domain=host_monotonic_domain(),created_monotonic_s=time.monotonic())
    manifest={"schema":"camera-live-pilot/v1","run_id":run_id,"pilot_run_id":run_id,
      "status":"started","created_utc":datetime.now(timezone.utc).isoformat(),
      "execution_mode":"camera_live_file_backed",
      "lifecycle_ownership":{"pilot_manifest_writer":"camera_live_coordinator",
          "route_runner_writes_pilot_manifest":False,
          "camera_runner_state_owner":"camera_live_coordinator",
          "camera_startup_validation_event_owner":"habitat_route_runner",
          "first_frame_evidence_owner":"habitat_route_runner; observed by camera_live_coordinator"},
      "bootstrap_id":bootstrap_id,"effective_route_config_path":str(route["config_path"]),
      "effective_route_config_sha256":route["config_sha256"],"source_root":str(source),
      "source_manifest_sha256":source_manifest_sha256,
      "route_run_id":cfg["run_id"],"scene_id":cfg["scene_id"],"sequence_id":cfg["sequence_id"],
      "route_frames_expected":None,"frame_count_source":"measured_at_runtime",
      "runtime_cap_s":float(a.timeout_s),"runtime_started_monotonic_s":pilot_started_monotonic,
      "timeouts":timeouts,
      "route_budget_preflight":route.get("route_budget"),
      "motion_timebase":cfg["policy"].get("motion_timebase"),
      "camera_rate_policy":{"schema":cfg["policy"].get("camera_pacing",{}).get("schema"),
          "mode":cfg["policy"].get("camera_pacing",{}).get("mode"),
          "target_camera_fps":cfg["policy"].get("camera_pacing",{}).get("target_camera_fps"),
          "real_time_factor":cfg["policy"].get("camera_pacing",{}).get("real_time_factor"),
          "overrun_policy":cfg["policy"].get("camera_pacing",{}).get("overrun_policy")},
      "camera_producer_manifest_path":str(root/CAMERA_PRODUCER_MANIFEST),
      "code_provenance":{"camera_live_pilot_sha256":sha(Path(__file__).resolve()),
          "camera_pacing_module_sha256":sha(ROOT/"integration_v2/camera_pacing.py"),
          "live_processing_sha256":sha(ROOT/"integration_v2/live_processing.py"),
          "live_evidence_sha256":sha(ROOT/"integration_v2/live_evidence.py"),
          "startup_contract_sha256":sha(ROOT/"integration_v2/camera_live_startup.py"),
          "habitat_route_runner_sha256":sha(ROOT/"scripts/run_habitat_smooth_route_preview.py"),
          "d2_viewer_builder_sha256":sha(ROOT/"scripts/build_d2_xyz_viewer.py"),
          "viewer_template_sha256":sha(ROOT/"scripts/causal_timeline_viewer.html")},
      "claim_boundary":"camera-live pilot; not realtime; room belief is not-scored when verified spatial scope is unavailable",
      "models":{"model_restart_per_frame":False,"p2":"locked ONNX","openclip":"locked local checkpoint"},
      "scope":{"policy":"verified_only","membership":"unavailable_elevation_scope","room_semantics":"not_scored_until_membership_scope_is_verified"},
      "stages":{"viewer_bootstrap":"pending","worker_start":"not_started","camera_capture":"not_started","perception":"not_started","mappers":"not_started","unified_publication":"not_started","terminal":"not_run"},
      "camera_runner":{"status":"not_spawned","state_owner":"camera_live_coordinator",
          "pid":None,"spawned_monotonic_s":None,"clock_domain":None,
          "startup_validated_monotonic_s":None,"first_frame_monotonic_s":None,
          "first_frame_timestamp_s":None,"first_frame_timestamp_clock_domain":None,
          "capture_finished_monotonic_s":None,"capture_finished_timestamp_s":None,
          "capture_finished_timestamp_clock_domain":None,"capture_producer_manifest_sha256":None,
          "capture_has_started":False,"capture_completed":False},
      "mapper_execution_plan":{"conceptgraphs_sessions":1,"occusg_sessions":1,"retries":0}}
    atomic_json(root/"run_manifest.json",manifest); atomic_json(root/"pilot_bootstrap.json",bootstrap)
    execution_event(root, "pilot_startup_attempt_started", run_id=run_id,
                    route_run_id=cfg["run_id"], sequence_id=cfg["sequence_id"],
                    config_sha256=route["config_sha256"], capture_attempt_allowed=True)
    camera_proc = None
    camera_started = False
    terminal_started = False
    dispatched = 0
    accepted = 0
    frames = []
    errors = []

    def execution_summary() -> dict[str, Any]:
        rows=[]
        ledger=root/"execution_ledger.jsonl"
        if ledger.is_file():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try: rows.append(json.loads(line))
                    except json.JSONDecodeError: break
        events=[str(row.get("event")) for row in rows]
        consumed=sum(row.get("event")=="keyframe_event_consumed" for row in rows)
        dispatch=sum(row.get("event")=="mapper_pair_dispatch_started" for row in rows)
        commits=sum(row.get("event")=="unified_revision_committed" for row in rows)
        ready={str(row.get("worker")) for row in rows if row.get("event")=="worker_ready"}
        camera_frames=sum(row.get("event")=="camera_frame_observed" for row in rows)
        return {"ledger_path":"execution_ledger.jsonl", "event_count":len(rows),
                "worker_initialization_attempts":sum(row.get("event")=="worker_initialization_started" for row in rows),
                "worker_initializations_succeeded":sum(row.get("event")=="worker_initialized" for row in rows),
                "worker_initializations_failed":sum(row.get("event")=="worker_initialization_failed" for row in rows),
                "worker_ready_count":len(ready), "keyframes_consumed":consumed,
                "mapper_pair_dispatches":dispatch, "unified_revisions":commits,
                "camera_runner_spawned":("camera_runner_spawned" in events),
                "camera_startup_validations":sum(row.get("event")=="camera_startup_validated" for row in rows),
                "camera_capture_completions":sum(row.get("event")=="camera_capture_completed" for row in rows),
                "camera_frames_observed":camera_frames, "capture_started":camera_frames > 0}

    def finish_lifecycle(status: str, error: Optional[str] = None) -> None:
        summary=execution_summary()
        if status != "completed":
            camera_state=manifest.get("camera_runner",{})
            stages=terminal_stage_statuses(manifest.get("stages",{}),
                camera_started=bool(camera_started), keyframes_consumed=int(summary["keyframes_consumed"]),
                mapper_dispatched=int(summary["mapper_pair_dispatches"]),
                unified_revisions=int(summary["unified_revisions"]),
                terminal_started=bool(terminal_started), run_status=status,
                camera_runner_spawned=camera_state.get("status") not in (None,"not_spawned"),
                camera_startup_validated=camera_state.get("startup_validated_monotonic_s") is not None,
                capture_completed=bool(camera_state.get("capture_completed")))
            manifest["stages"]=stages
        manifest["execution_summary"]=summary
        finished = datetime.now(timezone.utc).isoformat()
        terminalize_pilot_documents(manifest, bootstrap, status=status,
                                    finished_utc=finished, error=error)
        atomic_json(root/"pilot_bootstrap.json",bootstrap)
        atomic_json(root/"run_manifest.json",manifest)
    from integration_v2 import live_processing as lp
    from scripts.build_d2_xyz_viewer import (bootstrap as bootstrap_d2_xyz,
        terminalize_live_viewer, validate_entrypoint_bundle)
    semantic_session=None
    try:
        from integration_v2.room_belief_commit_session import RoomBeliefCommitSession
        import scripts.run_conceptgraphs_cached_hybrid as cg_config
        effective_mapping=cg_config.base_mapping_config()
        semantic_session=RoomBeliefCommitSession.open(
            config_path=a.room_belief_config,
            overlay_root=root/"semantics"/"overlays",
            admission_min_detections=int(effective_mapping["obj_min_detections"]))
        if semantic_session.config.get("scope_policy") != {"mode":"verified_only","assumed_declaration":None}:
            raise ValueError("camera-live T4 accepts only the locked verified-only scope policy; no prior assumed declaration transfers to this capture")
        semantic_session.bind_identity(run_id=run_id,scene_id=cfg["scene_id"],sequence_id=cfg["sequence_id"])
        manifest["room_belief_session"]={
            "status":"ready_before_workers", "config_path":str(a.room_belief_config.resolve()),
            "config_provenance":semantic_session.config_provenance,
            "variants":["mean_no_neutral","mean_neutral","sum_no_neutral","sum_neutral"],
            "scope_policy":"verified_only", "membership_default":"unavailable_elevation_scope",
            "no_qwen":True, "update_hook":"after immutable unified graph commit; before pair ACK"}
        atomic_json(root/"run_manifest.json",manifest)
    except BaseException as exc:
        error=f"{type(exc).__name__}: {exc}"
        manifest.update({"stages":{"viewer_bootstrap":"not_started","worker_start":"not_started",
                     "camera_capture":"not_started","room_belief_preflight":"failed_before_models"}})
        finish_lifecycle("startup_preflight_failed",error)
        raise
    try:
        viewer,_=lp._prepare_viewer(source,root,cfg["scene_id"],cfg["sequence_id"],
            evidence_mode="camera_live_current_run_only",run_id=run_id)
        initial_index=read(viewer/"live_index.json")
        initial_index["room_belief_variants"]=["mean_no_neutral","mean_neutral","sum_no_neutral","sum_neutral"]
        initial_index["room_belief_scope_modes"]=["verified_only"]
        initial_index["room_belief_contract"]="RB1 T3 core; overlays bind exact graph revision/hash; verified-only default"
        atomic_json(viewer/"live_index.json",initial_index)
        head=read(viewer/"live_head.json"); head["live_index_sha256"]=sha(viewer/"live_index.json"); atomic_json(viewer/"live_head.json",head)
        d2=bootstrap_d2_xyz(root,"camera_live")
        validate_entrypoint_bundle(viewer)
        server,url=lp._launch_server(viewer,ROOT,a.port,root/"logs/viewer_server.log",entrypoint=d2["viewer_entrypoint"])
    except BaseException as exc:
        error=f"{type(exc).__name__}: {exc}"
        manifest.update({"stages":{"viewer_bootstrap":"failed_before_workers","worker_start":"not_started",
                "camera_capture":"not_started","models_loaded":0}})
        finish_lifecycle("startup_preflight_failed",error)
        raise
    if a.startup_dry_run:
        fake_procs=[]; fake_handles=[]; camera_log=None
        try:
            for kind in EXPECTED_WORKERS:
                log=(root/"logs"/(kind+"_worker.log")).open("ab",buffering=0); fake_handles.append(log)
                ready=root/"ready"/(kind+".json")
                cmd=[sys.executable,"-u",str(Path(__file__).resolve()),"--fake-readiness-worker",kind,
                    "--run-root",str(root),"--ready-file",str(ready),"--run-id",run_id,
                    "--bootstrap-id",bootstrap_id,"--route-config-sha256",route["config_sha256"]]
                proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=log,stderr=log,text=True,bufsize=1,
                    env={**os.environ,"PYTHONPATH":os.pathsep.join((str(ROOT),str(ROOT/"src"))),"PYTHONUNBUFFERED":"1"})
                fake_procs.append(proc)
            readiness=_wait_ready_workers(root,list(zip(EXPECTED_WORKERS,fake_procs)),run_id=run_id,
                bootstrap_id=bootstrap_id,route_config_sha256=route["config_sha256"],timeout_s=30)
            manifest["status"]="waiting_for_viewer_ready"
            manifest["startup_dry_run"]=True
            manifest["stages"].update({"viewer_bootstrap":"served_waiting_for_browser_confirmation",
                "worker_start":"all_fake_workers_ready","camera_capture":"not_started",
                "perception":"not_started","mappers":"not_started"})
            manifest["viewer"]={"url":url,"entrypoint":d2["viewer_entrypoint"],
                                 "waiting_for_first_revision":True}
            atomic_json(root/"run_manifest.json",manifest)
            execution_event(root,"browser_gate_wait_started",run_id=run_id,url=url,
                            dry_run=True,workers_ready=len(readiness["workers"]))
            # The model-free path uses the same explicit confirmation helper as
            # the production path.  The served bundle is checked over HTTP and
            # the resulting start.json is present before route validation.
            start_signal=confirm_browser_ready(root,run_id)
            manifest["status"]="started"
            manifest["stages"]["viewer_bootstrap"]="browser_ready_confirmed"
            manifest["viewer"]["browser_ready_start_signal_sha256"]=start_signal["start_signal_sha256"]
            manifest["viewer"]["browser_ready_entrypoint"]=start_signal["entrypoint"]
            atomic_json(root/"run_manifest.json",manifest)
            manifest["startup_dry_run"]=True
            atomic_json(root/"run_manifest.json",manifest)
            camera_log=(root/"logs/camera_producer.log").open("ab",buffering=0)
            route_python=Path("/home/kiettran05/miniconda3/envs/habitat/bin/python")
            if not route_python.is_file():
                raise FileNotFoundError(f"locked Habitat interpreter is missing: {route_python}")
            route_cmd=[str(route_python),"-u",str(ROOT/"scripts/run_habitat_smooth_route_preview.py"),
                "--config",str(route["config_path"]),"--output",str(root),"--timeout-s",str(float(a.timeout_s)),
                "--publish-journal",str(root/"camera_keyframes.jsonl"),"--manifest-out",str(root/CAMERA_PRODUCER_MANIFEST),
                "--allow-existing-pilot-root",
                "--bootstrap-id",bootstrap_id,"--startup-dry-run"]
            route_proc=subprocess.Popen(route_cmd,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,env={**os.environ,"PYTHONUNBUFFERED":"1"})
            manifest=read(root/"run_manifest.json")
            mark_camera_runner_spawned(manifest,pid=route_proc.pid)
            atomic_json(root/"run_manifest.json",manifest)
            execution_event(root,"camera_runner_spawned",run_id=run_id,camera_pid=route_proc.pid,
                route_config_sha256=route["config_sha256"],owner="camera_live_coordinator",dry_run=True)
            try:
                route_stdout,_=route_proc.communicate(timeout=300)
            except subprocess.TimeoutExpired:
                route_proc.terminate()
                try: route_proc.wait(timeout=5)
                except subprocess.TimeoutExpired: route_proc.kill(); route_proc.wait(timeout=5)
                raise TimeoutError("route startup dry-run exceeded 300 seconds before capture boundary")
            camera_log.write(route_stdout.encode("utf-8",errors="replace"))
            if route_proc.returncode != 0:
                detail=route_stdout[-4000:]
                raise RuntimeError(f"route runner startup dry-run failed ({route_proc.returncode}): {detail}")
            route_result_doc=None
            for line in reversed(route_stdout.splitlines()):
                try:
                    candidate=json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate,dict) and candidate.get("status"):
                    route_result_doc=candidate
                    break
            if not isinstance(route_result_doc,dict):
                raise RuntimeError("route startup dry-run exited zero without a structured result")
            if (route_result_doc.get("status")!="startup_dry_run_route_preflight_passed"
                    or route_result_doc.get("pilot_run_id")!=run_id
                    or route_result_doc.get("route_run_id")!=cfg["run_id"]
                    or route_result_doc.get("sequence_id")!=cfg["sequence_id"]
                    or route_result_doc.get("effective_route_config_sha256")!=route["config_sha256"]
                    or route_result_doc.get("capture_boundary_reached") is not True
                    or route_result_doc.get("capture_boundary")!="immediately_before_sim.get_sensor_observations"
                    or route_result_doc.get("camera_started") is not False
                    or route_result_doc.get("camera_frames")!=0
                    or route_result_doc.get("model_loads")!=0
                    or route_result_doc.get("inference_calls")!=0
                    or route_result_doc.get("mapper_updates")!=0):
                raise RuntimeError("route startup result does not prove the locked pre-capture boundary and zero-work counters")
            manifest=read(root/"run_manifest.json")
            mark_camera_startup_validated(manifest,route_result_doc.get("startup_event",{}),pid=route_proc.pid)
            atomic_json(root/"run_manifest.json",manifest)
            execution_event(root,"camera_startup_validated",run_id=run_id,
                route_run_id=cfg["run_id"],sequence_id=cfg["sequence_id"],
                camera_pid=route_proc.pid,owner="habitat_route_runner",
                event_monotonic_s=route_result_doc["startup_event"]["event_monotonic_s"],
                clock_domain=route_result_doc["startup_event"]["clock_domain"],dry_run=True)
            mark_camera_runner_preflight_stopped(manifest,returncode=route_proc.returncode)
            atomic_json(root/"run_manifest.json",manifest)
            if route_result_doc.get("bootstrap_boundary",{}).get("capture_boundary_reached") is not True:
                raise RuntimeError("route runner did not return validated bootstrap boundary evidence")
            route_cleanup={"pid":route_proc.pid,"returncode":route_proc.returncode,
                "status":"exited_before_capture","capture_frames":0,
                "owner":"camera_live_coordinator"}
            if route_proc.poll() is None or route_proc.returncode != 0:
                raise RuntimeError(f"route preflight subprocess did not shut down cleanly: {route_cleanup}")
            execution_event(root,"route_capture_boundary_validated",run_id=run_id,
                            start_signal_present=(root/"control/start.json").is_file(),
                            capture_boundary=route_result_doc["capture_boundary"],dry_run=True)
            ready_pids={kind:proc.pid for kind,proc in zip(EXPECTED_WORKERS,fake_procs)}
            boundary=validate_bootstrap_before_capture(root,expected_bootstrap_id=bootstrap_id,
                expected_route_config_sha256=route["config_sha256"],expected_ready_pids=ready_pids)
            for proc in fake_procs:
                send(proc,{"op":"close"})
            worker_cleanup=[]
            for proc in fake_procs:
                try:
                    code=proc.wait(timeout=5)
                    worker_cleanup.append({"pid":proc.pid,"returncode":code,"status":"graceful_close"})
                except subprocess.TimeoutExpired:
                    worker_cleanup.extend(_stop_owned_processes([proc]))
            if any(row.get("returncode") != 0 for row in worker_cleanup):
                raise RuntimeError(f"fake readiness worker cleanup failed: {worker_cleanup}")
            lp._stop_owned(server)
            server_cleanup={"pid":server.pid,"returncode":server.poll(),"stopped":server.poll() is not None,
                            "owner":"camera_live_coordinator"}
            if not server_cleanup["stopped"]:
                raise RuntimeError(f"run-owned viewer server did not stop cleanly: {server.pid}")
            bootstrap.update({"preflight_result":"capture_boundary_ready"})
            manifest["owned_process_cleanup"]=[route_cleanup,*worker_cleanup,server_cleanup]
            manifest["viewer_server_cleanup"]=server_cleanup
            manifest.update({
                "stages":{"viewer_bootstrap":"served_and_validated","worker_start":"fake_ipc_ready",
                    "camera_startup":"validated_no_frame","camera_capture":"not_started_by_design",
                    "perception":"not_started","mappers":"not_started",
                    "unified_publication":"not_started","route_preflight":"capture_boundary_ready"},
                "startup_dry_run":{"same_live_launcher_sequence":True,"viewer_http_entrypoint":url,
                    "worker_readiness_ipc":"passed","route_runner_validation":"passed",
                    "habitat_simulator_initialized":route_result_doc.get("habitat_simulator_initialized"),
                    "route_preflight":route_result_doc.get("route_preflight"),
                    "capture_boundary_reached":True,"camera_started":False,"model_loads":0,
                    "capture_frames":0,"worker_cleanup":worker_cleanup,"readiness":readiness,
                    "boundary":boundary,"not_checked":["RGB-D capture","sensor observation values",
                        "model initialization/inference","mapper update","camera/graph overlap"]}})
            finish_lifecycle("startup_preflight_passed")
            print(json.dumps({"status":"startup_preflight_passed","run_root":str(root),"url":url,
                "capture_boundary_reached":True,"camera_started":False,"model_loads":0},sort_keys=True),flush=True)
            return 0
        except BaseException as exc:
            cleanup=_stop_owned_processes(fake_procs)
            route_child=locals().get("route_proc")
            route_cleanup=[]
            if route_child is not None:
                if route_child.poll() is None:
                    route_cleanup=_stop_owned_processes([route_child])
                    try: route_child.communicate(timeout=1)
                    except BaseException: pass
                if not route_cleanup:
                    route_cleanup=[{"pid":route_child.pid,"returncode":route_child.poll(),
                        "status":"exited_before_capture" if route_child.poll() is not None else "still_alive",
                        "owner":"camera_live_coordinator"}]
                camera_state=manifest.get("camera_runner",{})
                if camera_state.get("status") not in {"preflight_validated_stopped","capture_completed"}:
                    camera_status=("cancelled_before_capture" if isinstance(exc,KeyboardInterrupt)
                                   else "failed_before_capture")
                    mark_camera_runner_failure(manifest,status=camera_status,
                        error=f"{type(exc).__name__}: {exc}",returncode=route_child.poll())
            if server is not None:
                lp._stop_owned(server)
                manifest["viewer_server_cleanup"]={"pid":server.pid,"returncode":server.poll(),
                    "stopped":server.poll() is not None,"owner":"camera_live_coordinator"}
            error=f"{type(exc).__name__}: {exc}"
            manifest.update({
                "camera_started":False,"owned_process_cleanup":[*route_cleanup,*cleanup],
                "cleanup_errors":[row for row in [*route_cleanup,*cleanup]
                    if row.get("status") in {"still_alive","still_alive_after_kill"}]})
            finish_lifecycle("cancelled_before_capture" if isinstance(exc, KeyboardInterrupt) else "startup_preflight_failed",error)
            raise
        finally:
            _stop_owned_processes(fake_procs)
            lp._stop_owned(server)
            if semantic_session is not None: semantic_session.close()
            if camera_log is not None: camera_log.close()
            for handle in fake_handles: handle.close()
    # Resolve interpreter paths from the locked C2 manifest, never from a future route manifest.
    sm=read(source/"c2_run_manifest.json")
    p2py=Path(sm["stages"]["perception_p2"]["command"][0]); p3py=Path(sm["stages"]["projection_p3"]["command"][0]); fpy=Path(sm["stages"]["image_features"]["command"][0])
    p2cfg=ROOT/"configs/perception/yolo11n_seg_replica_cad_dev.json"; names=ROOT/"configs/perception/coco.names"; weights=Path(sm["stages"]["perception_p2"]["command"][sm["stages"]["perception_p2"]["command"].index("--weights")+1])
    p3profile=ROOT/"configs/perception/yolo11n_seg_replica_cad_dev.json"; feat_report=read(ROOT/"artifacts/room_belief/dense_00802_hybrid_sidecars_20261015/image_features/feature_extraction_report.json")
    clip=Path(sm["stages"]["image_features"]["command"][sm["stages"]["image_features"]["command"].index("--checkpoint")+1])
    procs=[]; handles=[]
    def spawn(kind, py, extra):
        log=(root/"logs"/(kind+"_worker.log")).open("ab",buffering=0); handles.append(log)
        ready = root/"ready"/(kind+".json")
        execution_event(root,"worker_process_spawn_requested",worker=kind,interpreter=str(py),
                        bootstrap_id=bootstrap_id)
        proc=subprocess.Popen([str(py),"-u",str(Path(__file__).resolve()),"--worker",kind,"--run-root",str(root),"--ready-file",str(ready),
            "--run-id",run_id,"--bootstrap-id",bootstrap_id,"--route-config-sha256",route["config_sha256"],*extra],stdin=subprocess.PIPE,stdout=log,stderr=log,text=True,bufsize=1,env={**os.environ,"PYTHONPATH":os.pathsep.join((str(ROOT),str(ROOT/"src"))),"PYTHONUNBUFFERED":"1"})
        procs.append(proc)
        execution_event(root,"worker_process_spawned",worker=kind,pid=proc.pid,
                        interpreter=str(py),bootstrap_id=bootstrap_id)
        return proc
    mapper=[]
    try:
        p2=spawn("p2",p2py,["--profile",str(p2cfg),"--names",str(names),"--weights",str(weights)])
        p3=spawn("p3",p3py,["--profile",str(p3profile)])
        fproc=spawn("features",fpy,["--checkpoint",str(clip),"--checkpoint-sha256",feat_report["checkpoint_sha256"],"--model-fingerprint",feat_report["model_fingerprint"],"--preprocessing-id",feat_report["preprocessing_id"]])
        # mapper worker commands reuse T2-D live workers and locked C2 interpreters.
        for kind,stage in (("conceptgraphs","conceptgraphs_mapping"),("occusg_rooms","occusg_rooms")):
            py=Path(sm["stages"][stage]["command"][0]); log=(root/"logs"/(kind+"_worker.log")).open("ab",buffering=0); handles.append(log)
            ready = root/"ready"/(kind+".json")
            cmd=[str(py),"-u",str(ROOT/"scripts/run_t2_d_cached_packet_worker.py"),"--worker",("conceptgraphs" if kind=="conceptgraphs" else "occusg"),"--run-root",str(root),"--packet-root",str(root/"packets"),"--source",str(source),"--ready-file",str(ready),"--live",
                 "--run-id",run_id,"--bootstrap-id",bootstrap_id,"--route-config-sha256",route["config_sha256"]]
            execution_event(root,"worker_process_spawn_requested",worker=kind,interpreter=str(py),
                            bootstrap_id=bootstrap_id)
            proc=subprocess.Popen(cmd,cwd=ROOT,stdin=subprocess.PIPE,stdout=log,stderr=log,text=True,bufsize=1,env={**os.environ,"PYTHONPATH":os.pathsep.join((str(ROOT),str(ROOT/"src"))),"PYTHONUNBUFFERED":"1"}); mapper.append(proc)
            execution_event(root,"worker_process_spawned",worker=kind,pid=proc.pid,
                            interpreter=str(py),bootstrap_id=bootstrap_id)
    except BaseException as exc:
        cleanup_rows=_stop_owned_processes(procs+mapper)
        lp._stop_owned(server)
        for handle in handles: handle.close()
        error=f"{type(exc).__name__}: {exc}"
        manifest.update({
            "stages":{"viewer_bootstrap":"completed","worker_start":"failed_before_camera"},
            "owned_process_cleanup":cleanup_rows,"worker_lifecycle":"workers_stopped_after_startup_error",
            "camera_started":False})
        finish_lifecycle("cancelled_before_capture" if isinstance(exc, KeyboardInterrupt) else "failed_before_capture",error)
        raise
    manifest["stages"].update({"viewer_bootstrap":"ready_waiting_for_first_revision","worker_start":"waiting_for_all_worker_ready","camera_capture":"not_started","perception":"not_started","mappers":"not_started"}); manifest["viewer"]={"url":url,"entrypoint":d2["viewer_entrypoint"],"waiting_for_first_revision":True}; atomic_json(root/"run_manifest.json",manifest)
    try:
        worker_readiness=_wait_ready_workers(root,[(k,p) for k,p in zip(
            EXPECTED_WORKERS,procs+mapper)],run_id=run_id,bootstrap_id=bootstrap_id,
            route_config_sha256=route["config_sha256"],
            timeout_s=min(300.0,remaining_before(pilot_deadline,"worker readiness")))
        manifest["status"]="waiting_for_viewer_ready"
        manifest["stages"]["worker_start"]="all_workers_ready_before_camera"
        manifest["worker_readiness"]=worker_readiness
        atomic_json(root/"run_manifest.json",manifest)
        execution_event(root,"browser_gate_wait_started",run_id=run_id,url=url,dry_run=False,
                        workers_ready=len(worker_readiness["workers"]))
        print(json.dumps({"status":"waiting_for_viewer_ready","url":url,"run_root":str(root)},sort_keys=True),flush=True)
        pointer_sha=sha(viewer/"d2_viewer_entrypoint.json")
        start_signal=lp._wait_for_explicit_viewer_start(root,run_id,pointer_sha,
            min(180.0,remaining_before(pilot_deadline,"browser-ready confirmation")))
    except BaseException as exc:
        cleanup_rows=_stop_owned_processes(procs+mapper)
        lp._stop_owned(server)
        for handle in handles: handle.close()
        error=f"{type(exc).__name__}: {exc}"
        manifest.update({
            "stages":{"viewer_bootstrap":"ready_waiting_for_first_revision","worker_start":"failed_before_camera"},
            "worker_readiness":None,"owned_process_cleanup":cleanup_rows,
            "worker_lifecycle":"workers_stopped_before_capture","camera_started":False})
        finish_lifecycle("cancelled_before_capture" if isinstance(exc, KeyboardInterrupt) else "failed_before_capture",error)
        raise
    manifest["status"]="started"
    manifest["stages"]["viewer_bootstrap"]="browser_ready_confirmed"
    manifest["viewer"]["browser_ready_start_signal_sha256"]=sha(root/"control/start.json")
    manifest["viewer"]["browser_ready_entrypoint"]=start_signal.get("entrypoint")
    atomic_json(root/"run_manifest.json",manifest)
    print(json.dumps({"status":"browser_ready_confirmed","url":url,"run_root":str(root)},sort_keys=True),flush=True)
    frames=[]
    pipeline_event_path=root/"logs"/"pipeline_events.jsonl"
    def pipeline_event(name, **fields):
        row={"event":name,"event_monotonic_s":time.monotonic(),
            "event_wall_time_unix_s":time.time(),"clock_domain":host_monotonic_domain(),**fields}
        append_jsonl(pipeline_event_path,row)
        execution_event(root,name,**fields)
    queue=BoundedPublishedKeyframeQueue(lambda row: pipeline_event("pipeline_queue_"+row["event"],**{k:v for k,v in row.items() if k!="event"}))
    tailer_done=threading.Event(); tailer_errors=[]; tailer_stop=threading.Event()
    try:
        def process_keyframe_event(event):
            nonlocal dispatched,accepted
            validate_published_event(event)
            packet = event["packet"]; result = event["result"]
            fid=str(packet["frame_id"]); k=write_capture_packet(root,packet,result,cfg)
            pipeline_event("keyframe_event_consumed",frame_id=fid,frame_index=int(k["frame_index"]),
                source_timestamp_s=k["timestamp_s"],source_timestamp_clock_domain=k["timestamp_clock_domain"],
                captured_monotonic_s=k["capture_monotonic_s"],capture_monotonic_domain=k["capture_monotonic_domain"],
                published_monotonic_s=event["published_monotonic_s"],published_monotonic_domain=event["published_monotonic_domain"],
                camera_process_running=(camera_proc is not None and camera_proc.poll() is None))
            manifest["stages"]["perception"]="running"
            frame_json=root/"packets"/f"frame-{int(packet['metadata']['frame_index']):06d}.json"; atomic_json(frame_json,k); k["asset_root"]=str(root/"packets"/"assets");
            # P2 current frame.
            resp=root/"packets"/f"p2-{int(k['frame_index']):06d}.json"; send(p2,{"op":"frame","frame_json":str(frame_json),"response_json":str(resp)})
            p2r=wait_worker_json(p2,resp,stage="P2",timeout=min(120.0,remaining_before(pilot_deadline,"P2")))
            if p2r.get("status") != "completed": raise RuntimeError(f"P2 failed for {fid}: {p2r.get('error')}")
            validate_stage_response(k, p2r, stage="P2")
            pipeline_event("perception_stage_started",stage="P2",frame_id=fid,
                stage_started_monotonic_s=p2r["stage_started_monotonic_s"],stage_clock_domain=p2r["stage_clock_domain"])
            k["p2_detections"]=p2r["detections"]
            # Copy masks into the immutable mapper asset root and preserve the declared relative path.
            k["asset_root"]=str(root/"packets"/"assets")
            pj=root/"packets"/f"p3-{int(k['frame_index']):06d}.json"; fj=root/"packets"/f"feat-{int(k['frame_index']):06d}.json"; packet_json=root/"packets"/f"joined-{int(k['frame_index']):06d}.json"; atomic_json(packet_json,k)
            send(p3,{"op":"frame","packet_json":str(packet_json),"response_json":str(pj)}); send(fproc,{"op":"frame","packet_json":str(packet_json),"response_json":str(fj)})
            p3r=wait_worker_json(p3,pj,stage="P3",timeout=min(120.0,remaining_before(pilot_deadline,"P3")))
            fr=wait_worker_json(fproc,fj,stage="OpenCLIP",timeout=min(120.0,remaining_before(pilot_deadline,"OpenCLIP")))
            if p3r.get("status") != "completed": raise RuntimeError(f"P3 failed for {fid}: {p3r.get('error')}")
            if fr.get("status") != "completed": raise RuntimeError(f"OpenCLIP failed for {fid}: {fr.get('error')}")
            joined = build_joined_perception_packet(k, p2r, p3r, fr)
            for stage,response in (("P3",p3r),("OpenCLIP",fr)):
                pipeline_event("perception_stage_started",stage=stage,frame_id=fid,
                    stage_started_monotonic_s=response["stage_started_monotonic_s"],stage_clock_domain=response["stage_clock_domain"])
            k.update({"p2_detections": joined["p2_detections"], "p3_detections": joined["p3_detections"],
                      "features": joined["features"], "perception_status": joined["perception_status"]})
            atomic_json(packet_json,k)
            packet_sha=sha(packet_json); msg={"op":"frame","packet_path":str(packet_json),"packet_sha256":packet_sha}
            pipeline_event("mapper_pair_dispatch_started",frame_id=fid,frame_index=int(k["frame_index"]),
                packet_sha256=packet_sha,source_hashes=k["source_hashes"],branch_order=["conceptgraphs","occusg_rooms"])
            # Both worker IPC commands are enqueued before the pair barrier is polled.
            manifest["stages"]["mappers"]="running"
            send_mapper_pair(send,mapper,msg); dispatched+=1
            # Both workers publish an event and wait for durable ACK.  Join only matching frame/hash events.
            deadline=min(time.monotonic()+180.0,pilot_deadline); obj_events=root/"branches/objects/events.jsonl"; room_events=root/"branches/rooms/events.jsonl"; expected=dispatched
            while time.monotonic()<deadline:
                def rows(path): return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.is_file() else []
                oe=rows(obj_events); re=rows(room_events)
                if len(oe)>=expected and len(re)>=expected:
                    oe1, re1=oe[expected-1], re[expected-1]
                    if int(oe1["source_frame_index"])==int(re1["source_frame_index"]) and oe1["source_hashes"]==re1["source_hashes"]:
                        validate_mapper_pair_events(k,oe1,re1)
                        base=read(viewer/"live_index.json"); joined=rows(root/"joined/frames.jsonl")
                        joined_event=lp._join_one(root,viewer,base,joined,oe1,re1,
                            publish_index=False,publish_event=False,acknowledge=False); rec=joined[-1]
                        from integration_v2.live_evidence import publish_live_frame_evidence
                        rec["live_evidence"] = publish_live_frame_evidence(
                            run_root=root, viewer_root=viewer, run_id=run_id,
                            scene_id=cfg["scene_id"], sequence_id=cfg["sequence_id"],
                            frame_id=fid, frame_index=int(k["frame_index"]),
                            timestamp_s=float(k["timestamp_s"]),
                            timestamp_clock_domain=str(k["timestamp_clock_domain"]),
                            capture_wall_time_unix_s=float(k["capture_wall_time_unix_s"]),
                            capture_monotonic_s=float(k["capture_monotonic_s"]),
                            capture_monotonic_domain=str(k["capture_monotonic_domain"]),
                            frame_dir=Path(k["frame_dir"]), capture_record=k["capture_record"],
                            source_hashes=k["source_hashes"],
                            detections=k.get("p2_detections", []),
                            mask_asset_root=root/"packets"/"assets"/"detection_2d")
                        graph_path=(viewer/rec["unified_url"]).resolve(); graph_doc=read(graph_path)
                        object_path=(root/oe1["state_path"]).resolve(); object_doc=read(object_path)
                        room_path=(root/re1["state_path"]).resolve(); room_doc=read(room_path)
                        pose=packet.get("metadata",{}).get("sensor_pose_canonical")
                        current_observations=[]
                        for detection in k.get("p2_detections",[]):
                            oid=str(detection["detection_id"])
                            current_observations.append({"observation_id":oid,"frame_id":fid,
                                "frame_index":int(k["frame_index"]),"raw_class":str(detection["class_raw"]),
                                "camera_pose_W_Copt":pose,
                                "source_hashes":dict(k["source_hashes"])|{f"mask:{oid}":str(detection["mask_sha256"])}})
                        source_hashes=dict(k["source_hashes"])|{
                            "perception_packet":packet_sha,"object_state":str(oe1["state_sha256"]),
                            "room_state":str(re1["state_sha256"]),
                            "object_event":str(oe1["event_sha256"]),"room_event":str(re1["event_sha256"])}
                        envelope={"event_id":f"unified:{len(joined)}:{fid}","run_id":run_id,
                            "scene_id":cfg["scene_id"],"sequence_id":cfg["sequence_id"],
                            "frame_index":int(graph_doc["frame_index"]),"frame_id":str(graph_doc["frame_id"]),
                            "timestamp_s":float(graph_doc["timestamp_s"]),"unified_revision":len(joined),
                            "graph_revision_id":str(graph_doc["graph_revision_id"]),"graph_path":str(graph_path),
                            "graph_sha256":sha(graph_path),"object_revision":int(object_doc["revision"]),
                            "object_state_sha256":sha(object_path),"room_revision":int(room_doc.get("revision",room_doc.get("snapshot",{}).get("revision"))),
                            "room_state_sha256":sha(room_path),"source_hashes":source_hashes,
                            "current_observations":current_observations,
                            "objects":[{"object_id":str(row["hypothesis_id"]),
                                "observation_ids":list(row.get("observation_ids",[])),
                                "detection_count":int(row.get("detection_count",0)),
                                "membership":{"status":"unavailable","reason":"unavailable_elevation_scope"}}
                                for row in object_doc.get("objects",[])],
                            "rooms":[{"node_id":str(row["node_id"])} for row in graph_doc.get("rooms",[])],
                            "scope":{"status":"unavailable","evidence_status":"unavailable",
                                "reason":"unavailable_elevation_scope"}}
                        semantic_started=time.monotonic()
                        try:
                            semantic_result=semantic_session.update_after_unified_commit(envelope)
                        except Exception as semantic_error:
                            semantic_result=semantic_session.write_failure_overlay(envelope,semantic_error)
                            manifest.setdefault("room_belief_session",{}).setdefault("failed_revisions",[]).append({
                                "unified_revision":len(joined),"frame_id":fid,
                                "error":f"{type(semantic_error).__name__}: {semantic_error}"})
                            atomic_json(root/"run_manifest.json",manifest)
                        overlay_path=Path(semantic_result["overlay_path"]).resolve()
                        rec["room_belief_overlay_url"]=Path(os.path.relpath(overlay_path,viewer)).as_posix()
                        rec["room_belief_overlay_sha256"]=semantic_result["overlay_sha256"]
                        rec["room_belief_status"]=semantic_result["overlay"]["room_semantics_availability"]["status"]
                        rec["room_belief_scope_status"]=semantic_result["overlay"].get("scope_status","unavailable")
                        rec["room_belief_variant_count"]=4
                        # One reusable commit helper validates both immutable artifacts,
                        # atomically advances the viewer pointer, then ACKs both branches.
                        lp._publish_joined_semantic_commit(root,viewer,base,joined,joined_event,oe1,re1)
                        accepted+=1
                        lp.append_jsonl(root/"semantics"/"commits.jsonl",{
                            "schema":"t4-semantic-overlay-commit/v1","unified_revision":len(joined),
                            "frame_id":fid,"graph_sha256":rec["unified_sha256"],
                            "overlay_url":rec["room_belief_overlay_url"],
                            "overlay_sha256":rec["room_belief_overlay_sha256"],
                            "status":rec["room_belief_status"],"commit_monotonic_s":time.monotonic(),
                            "clock_domain":host_monotonic_domain()})
                        pipeline_event("semantic_overlay_committed",frame_id=fid,frame_index=int(k["frame_index"]),
                            unified_revision=len(joined),semantic_status=rec["room_belief_status"],
                            overlay_sha256=rec["room_belief_overlay_sha256"],
                            update_elapsed_s=time.monotonic()-semantic_started)
                        pipeline_event("unified_revision_committed",frame_id=fid,frame_index=int(k["frame_index"]),
                            unified_revision=len(joined),object_event_seq=int(oe1["event_seq"]),room_event_seq=int(re1["event_seq"]))
                        return
                time.sleep(.05)
            raise TimeoutError(f"mapper pair barrier timeout for {fid}")
        journal=root/"camera_keyframes.jsonl"
        camera_log=(root/"logs/camera_producer.log").open("ab",buffering=0); handles.append(camera_log)
        ready_pids={kind:proc.pid for kind,proc in zip(EXPECTED_WORKERS,procs+mapper)}
        validate_bootstrap_before_capture(root,expected_bootstrap_id=bootstrap_id,
            expected_route_config_sha256=route["config_sha256"],expected_ready_pids=ready_pids)
        camera_cmd=["/home/kiettran05/miniconda3/envs/habitat/bin/python","-u",str(ROOT/"scripts/run_habitat_smooth_route_preview.py"),
                "--config",str(a.config.resolve()),"--output",str(root),"--timeout-s",str(min(
                    timeouts["camera_route_timeout_effective_s"],
                    remaining_before(pilot_deadline,"Habitat capture"))),
                    "--publish-journal",str(journal),"--manifest-out",str(root/CAMERA_PRODUCER_MANIFEST),
                    "--allow-existing-pilot-root","--bootstrap-id",bootstrap_id]
        camera_proc=subprocess.Popen(camera_cmd,cwd=ROOT,stdout=camera_log,stderr=camera_log,env={**os.environ,"PYTHONUNBUFFERED":"1"})
        # Do not mark the run as running here: the child route runner still
        # validates the same run manifest before it may create Habitat frames.
        # Only an observed camera_frame event crosses that boundary.
        mark_camera_runner_spawned(manifest,pid=camera_proc.pid)
        atomic_json(root/"run_manifest.json",manifest)
        execution_event(root,"camera_runner_spawned",run_id=run_id,camera_pid=camera_proc.pid,
                        route_config_sha256=route["config_sha256"])
        camera_first_frame_seen=threading.Event()
        camera_frame_seen_ids=set()
        camera_first_frame_record={}
        def tail_keyframe_journal():
            offset=0
            camera_offset=0
            camera_startup_validated=False

            def handle_camera_event(event_row):
                nonlocal camera_startup_validated
                if event_row.get("event")=="camera_startup_validated":
                    if camera_startup_validated:
                        raise CameraLiveContractError("duplicate camera_startup_validated event")
                    current=read(root/"run_manifest.json")
                    mark_camera_startup_validated(current,event_row,pid=camera_proc.pid)
                    atomic_json(root/"run_manifest.json",current)
                    execution_event(root,"camera_startup_validated",run_id=run_id,
                        route_run_id=cfg["run_id"],sequence_id=cfg["sequence_id"],
                        camera_pid=camera_proc.pid,owner="habitat_route_runner",
                        event_monotonic_s=event_row["event_monotonic_s"],
                        clock_domain=event_row["clock_domain"])
                    camera_startup_validated=True
                    return
                if event_row.get("event")!="camera_frame":
                    return
                if not camera_startup_validated:
                    raise CameraLiveContractError("camera_frame arrived before route startup validation event")
                frame_id=str(event_row.get("frame_id"))
                if frame_id not in camera_frame_seen_ids:
                    camera_frame_seen_ids.add(frame_id)
                    if not camera_first_frame_record:
                        camera_first_frame_record["event"]=dict(event_row)
                    execution_event(root,"camera_frame_observed",run_id=run_id,
                        frame_id=frame_id,frame_index=int(event_row["frame_index"]),
                        timestamp_s=float(event_row["timestamp_s"]),
                        timestamp_clock_domain=event_row.get("timestamp_clock_domain"),
                        capture_monotonic_s=float(event_row["capture_monotonic_s"]),
                        capture_monotonic_domain=event_row.get("capture_monotonic_domain"),
                        owner="habitat_route_runner")
                    camera_first_frame_seen.set()
            try:
                while not tailer_stop.is_set():
                    records, offset = read_complete_jsonl(journal,offset)
                    for record in records:
                        if record.get("schema")!="camera-live-journal/v1":
                            raise CameraLiveContractError("unsupported keyframe journal record schema")
                        if record.get("event")=="keyframe_persisted": queue.offer(record["payload"])
                    event_journal=root/"events.jsonl"
                    if event_journal.is_file():
                        camera_rows,camera_offset=read_complete_jsonl(event_journal,camera_offset)
                        for event_row in camera_rows:
                            handle_camera_event(event_row)
                    if camera_proc.poll() is not None:
                        # Re-read after process exit so a final flushed line cannot be missed.
                        records, offset = read_complete_jsonl(journal,offset)
                        for record in records:
                            if record.get("schema")!="camera-live-journal/v1":
                                raise CameraLiveContractError("unsupported keyframe journal record schema")
                            if record.get("event")=="keyframe_persisted": queue.offer(record["payload"])
                        if event_journal.is_file():
                            camera_rows,camera_offset=read_complete_jsonl(event_journal,camera_offset)
                            for event_row in camera_rows:
                                handle_camera_event(event_row)
                        assert_no_incomplete_jsonl_tail(journal,offset)
                        if event_journal.is_file(): assert_no_incomplete_jsonl_tail(event_journal,camera_offset)
                        queue.close(); return
                    time.sleep(.02)
            except BaseException as exc:
                tailer_errors.append(f"{type(exc).__name__}: {exc}")
                queue.close()
            finally:
                tailer_done.set()
        tailer=threading.Thread(target=tail_keyframe_journal,name="camera-live-journal-tailer",daemon=True)
        tailer.start()
        producer_deadline=pilot_deadline
        while time.monotonic()<producer_deadline:
            if camera_first_frame_seen.is_set() and not camera_started:
                camera_started=True
                manifest=read(root/"run_manifest.json")
                mark_camera_capture_started(manifest,camera_first_frame_record["event"],pid=camera_proc.pid)
                atomic_json(root/"run_manifest.json",manifest)
            event=queue.get(timeout_s=.05)
            if event is not None:
                process_keyframe_event(event)
                queue.mark_processed(str(event["packet"]["frame_id"]))
            if tailer_errors:
                raise RuntimeError(f"keyframe journal reader failed: {tailer_errors[0]}")
            if tailer_done.is_set() and not queue.has_pending:
                break
        else:
            camera_proc.terminate(); tailer_stop.set(); queue.close()
            raise TimeoutError("camera producer or journal consumer did not finish within pilot budget")
        tailer.join(timeout=2.0)
        camera_manifest_path=root/CAMERA_PRODUCER_MANIFEST
        if camera_proc.returncode != 0:
            detail=(root/"logs/camera_producer.log").read_text(encoding="utf-8",errors="replace")[-8000:]
            raise RuntimeError(f"camera runner failed before publishing its producer manifest (code {camera_proc.returncode}): {detail}")
        if not camera_manifest_path.is_file():
            detail=(root/"logs/camera_producer.log").read_text(encoding="utf-8",errors="replace")[-8000:]
            raise RuntimeError(f"camera runner exited without producer manifest: {detail}")
        camera_result=read(camera_manifest_path)
        if camera_result.get("run_id") != cfg["run_id"] or camera_result.get("sequence_id") != cfg["sequence_id"]:
            raise RuntimeError("camera producer manifest route/sequence identity mismatch")
        if camera_result.get("status") != "completed":
            raise RuntimeError(f"camera producer failed with code {camera_proc.returncode}")
        capture_stage_for_lifecycle=camera_result.get("stages",{}).get("camera_capture",{})
        manifest=read(root/"run_manifest.json")
        mark_camera_capture_completed(manifest,capture_stage_for_lifecycle,
            producer_manifest_sha256=sha(camera_manifest_path),pid=camera_proc.pid)
        atomic_json(root/"run_manifest.json",manifest)
        execution_event(root,"camera_capture_completed",run_id=run_id,
            route_run_id=cfg["run_id"],sequence_id=cfg["sequence_id"],
            camera_pid=camera_proc.pid,
            capture_finished_monotonic_s=capture_stage_for_lifecycle.get("capture_finished_monotonic_s"),
            clock_domain=capture_stage_for_lifecycle.get("capture_monotonic_domain"),
            producer_manifest_sha256=sha(camera_manifest_path),owner="camera_live_coordinator")
        terminal_started=True
        execution_event(root,"terminal_processing_started",run_id=run_id,
                        dispatched_frames=dispatched,unified_revisions=accepted)
        send(p2,{"op":"close"}); send(p3,{"op":"close"}); send(fproc,{"op":"close"})
        for proc in mapper: send(proc,{"op":"finalize"})
        for proc in procs+mapper: proc.wait(timeout=min(180.0,remaining_before(pilot_deadline,"terminal worker finalize")))
        worker_exits=[{"pid":proc.pid,"returncode":proc.returncode} for proc in procs+mapper]
        if any(row["returncode"] != 0 for row in worker_exits):
            raise RuntimeError(f"worker terminal stage failed: {worker_exits}")
        terminal_semantics=semantic_session.finalize({"stage":"terminal_filter_merge_flush",
            "conceptgraphs_terminal":str(root/"branches/objects/terminal.json"),
            "occusg_terminal":str(root/"branches/rooms/terminal.json"),"lineage":None})
        semantic_session.close()
        base=read(viewer/"live_index.json"); joined=[json.loads(x) for x in (root/"joined/frames.jsonl").read_text().splitlines() if x.strip()] if (root/"joined/frames.jsonl").is_file() else []; lp._write_live_index(root,viewer,base,joined,"completed",{"status":"terminal","frames":len(joined),"room_belief_status":terminal_semantics["status"],"room_belief_url":Path(os.path.relpath(terminal_semantics["path"],viewer)).as_posix(),"room_belief_sha256":terminal_semantics["sha256"]})
        capture_stage=camera_result.get("stages",{}).get("camera_capture",{})
        pipeline_rows=[json.loads(line) for line in pipeline_event_path.read_text().splitlines() if line.strip()] if pipeline_event_path.is_file() else []
        perception_rows=[e for e in pipeline_rows if e.get("event")=="perception_stage_started"]
        commit_rows=[e for e in pipeline_rows if e.get("event")=="unified_revision_committed"]
        capture_end=capture_stage.get("capture_finished_monotonic_s")
        capture_domain=capture_stage.get("capture_monotonic_domain")
        perception_start_docs=[{"stage":e["stage"],"frame_id":e["frame_id"],
            "started_monotonic_s":e["stage_started_monotonic_s"],"clock_domain":e["stage_clock_domain"]} for e in perception_rows]
        commit_docs=[{"frame_id":e["frame_id"],"revision":e["unified_revision"],
            "committed_monotonic_s":e["event_monotonic_s"],"clock_domain":e["clock_domain"]} for e in commit_rows]
        capture_docs=[{"frame_id":e["frame_id"],"captured_monotonic_s":e["captured_monotonic_s"],
            "clock_domain":e["capture_monotonic_domain"]} for e in pipeline_rows if e.get("event")=="keyframe_event_consumed"]
        overlap_evidence=evaluate_live_overlap(capture_end,capture_domain,perception_start_docs,commit_docs,capture_docs)
        semantic_failures=manifest.get("room_belief_session",{}).get("failed_revisions",[])
        camera_timing=camera_result.get("summary",{}).get("camera_timing",{})
        manifest=read(root/"run_manifest.json"); manifest.update({"camera_producer_manifest":{"path":CAMERA_PRODUCER_MANIFEST,"sha256":sha(camera_manifest_path),"route_run_id":camera_result.get("run_id"),"sequence_id":camera_result.get("sequence_id")},"stages":{"viewer_bootstrap":"completed","worker_start":"completed","camera_capture":"completed","perception":"completed","mappers":"completed","terminal":"completed"},"worker_exit_codes":worker_exits,"camera":{"frames":camera_result.get("summary",{}).get("camera_frames"),"route_frames_actual":camera_result.get("summary",{}).get("camera_frames"),"simulation_s":camera_result.get("summary",{}).get("simulated_duration_s"),"wall_clock_s":camera_result.get("summary",{}).get("wall_clock_runtime_s"),"capture_started_monotonic_s":capture_stage.get("capture_started_monotonic_s"),"capture_finished_monotonic_s":capture_end,"capture_duration_s":capture_stage.get("elapsed_s"),"capture_monotonic_domain":capture_domain,"target_fps":camera_timing.get("target_camera_fps"),"actual_fps":camera_timing.get("actual_camera_fps"),"simulation_per_wall_factor":camera_timing.get("simulation_per_wall_factor"),"wall_interval_s":camera_timing.get("wall_interval_s"),"simulation_interval_s":camera_timing.get("simulation_interval_s"),"pacing_wait_total_s":camera_timing.get("pacing_wait_total_s"),"pacing_overrun_count":camera_timing.get("pacing_overrun_count")},"keyframes":{"queued":queue.queued,"admitted_to_processing":queue.admitted,"superseded_before_processing":queue.superseded,"processed":queue.processed,"mapper_dispatched":dispatched,"unified_revisions":accepted,"pending_capacity":1},"overlap_evidence":overlap_evidence,"viewer":{"url":url,"entrypoint":d2["viewer_entrypoint"],"live_follow":True},"room_belief_session":{"status":"completed_with_semantic_failures" if semantic_failures else "completed","commits":accepted,"failed_revisions":semantic_failures,"terminal_status":terminal_semantics["status"],"scope_policy":"verified_only","membership":"unavailable_elevation_scope","room_scoring":"not_scored"},"claims":{"camera_live":True,"realtime":False,"room_semantics":"not_scored_until_verified_scope"}})
        finish_lifecycle("completed")
        terminal_sync=terminalize_live_viewer(root,status="completed")
        manifest=read(root/"run_manifest.json")
        manifest.setdefault("viewer",{})["terminal_sync"]=terminal_sync
        atomic_json(root/"run_manifest.json",manifest)
        print(json.dumps({"status":"completed","run_root":str(root),"url":url,"dispatched":dispatched,"revisions":accepted}),flush=True); return 0
    except BaseException as exc:
        errors.append(f"{type(exc).__name__}: {exc}");
        tailer_stop.set(); queue.close()
        cleanup_rows=_stop_owned_processes(([camera_proc] if camera_proc is not None else [])+procs+mapper)
        manifest=read(root/"run_manifest.json"); manifest.update({"owned_process_cleanup":cleanup_rows,"keyframes":{"queued":queue.queued,"admitted_to_processing":queue.admitted,"superseded_before_processing":queue.superseded,"processed":queue.processed,"mapper_dispatched":dispatched,"unified_revisions":accepted,"pending_capacity":1},"replay_required":dispatched>0})
        if camera_proc is not None:
            camera_state=manifest.get("camera_runner",{})
            if camera_state.get("status") not in {"capture_completed","preflight_validated_stopped"}:
                runner_status=("cancelled_after_first_frame" if isinstance(exc,KeyboardInterrupt) and camera_started
                    else "cancelled_before_capture" if isinstance(exc,KeyboardInterrupt)
                    else "failed_after_first_frame" if camera_started else "failed_before_capture")
                mark_camera_runner_failure(manifest,status=runner_status,error=errors[-1],
                    returncode=camera_proc.poll())
        if semantic_session is not None:
            try: semantic_session.close()
            except BaseException as cleanup_error: manifest.setdefault("room_belief_session",{})["close_error"]=f"{type(cleanup_error).__name__}: {cleanup_error}"
        status=("cancelled_before_capture" if isinstance(exc, KeyboardInterrupt) and not camera_started
                else "failed_before_capture" if not camera_started
                else "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed")
        finish_lifecycle(status,errors[-1])
        if (root/"viewer"/"d2_viewer_entrypoint.json").is_file():
            try:
                terminal_sync=terminalize_live_viewer(root,status=("cancelled" if status.startswith("cancelled") else "failed"),error=errors[-1])
                manifest=read(root/"run_manifest.json")
                manifest.setdefault("viewer",{})["terminal_sync"]=terminal_sync
                atomic_json(root/"run_manifest.json",manifest)
            except BaseException as terminal_error:
                manifest=read(root/"run_manifest.json")
                manifest.setdefault("viewer",{}).setdefault("terminal_sync_error",f"{type(terminal_error).__name__}: {terminal_error}")
                atomic_json(root/"run_manifest.json",manifest)
        print(json.dumps({"status":status,"error":errors[-1],"run_root":str(root)},sort_keys=True),file=sys.stderr,flush=True); return 1
    finally:
        tailer_stop.set(); queue.close()
        if "tailer" in locals() and tailer.is_alive(): tailer.join(timeout=2.0)
        _stop_owned_processes(([camera_proc] if camera_proc is not None else [])+procs+mapper)
        for h in handles:
            h.close()

if __name__=="__main__": raise SystemExit(main())
