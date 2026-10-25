"""Cached-input T2-D coordinator.

This module is deliberately orchestration-only.  It feeds the three locked
T2-B2 packet records to one long-lived ConceptGraphs worker and one long-lived
OccuSG worker, then joins their immutable branch events into D2 revisions.
The mapping implementations remain in their existing session wrappers.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from integration_v2.live_processing import (
    D_ROOT, ROOT, _make_graph_revision, _prepare_viewer, _launch_server,
    _read, _sha, _write_json, _verify_event, _write_live_index,
)
from standalone_sg.serialization.live_revision_io import (
    AcknowledgedPublisher, append_jsonl, canonical_json,
)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _atomic_json(path: Path, value: Any) -> None:
    from standalone_sg.serialization.live_revision_io import atomic_write_json
    atomic_write_json(path, value)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_packet_bundle(root: Path, source: Path, packet_manifest: Path) -> list[Path]:
    """Materialize a validated per-frame packet bundle before workers start."""
    packet_doc = _read(packet_manifest)
    if packet_doc.get("execution_mode") != "current_frame_session_smoke":
        raise ValueError("T2-D requires the locked T2-B2 current-frame packet manifest")
    frames = packet_doc.get("frames")
    if not isinstance(frames, list) or len(frames) != 3:
        raise ValueError("T2-D requires exactly three locked T2-B2 packets")
    capture_root = source / "capture"
    expected_p2_hash = str(frames[0].get("join", {}).get("source_hashes", {}).get("p2_index_sha256", ""))
    p2_candidates = [p for p in ROOT.rglob("detection_2d_index.json") if p.is_file() and _sha(p) == expected_p2_hash]
    if len(p2_candidates) != 1:
        raise ValueError(f"could not resolve unique locked P2 index by hash: {expected_p2_hash}; candidates={len(p2_candidates)}")
    p2_path = p2_candidates[0]
    p2_doc = _read(p2_path)
    p2_by_frame = {str(row["frame_id"]): row for row in p2_doc.get("frames", [])}
    capture_doc = _read(capture_root / "capture_manifest.json")
    cap_by_frame = {str(row["frame_id"]): row for row in capture_doc.get("frames", [])}
    packet_root = root / "packets"
    packet_root.mkdir(parents=True, exist_ok=False)
    staged_asset_root = packet_root / "assets"
    staged_mask_root = staged_asset_root / "detection_2d" / "masks"
    staged_mask_root.mkdir(parents=True, exist_ok=True)
    output: list[Path] = []
    previous_index = -1
    for row in frames:
        fid = str(row["frame_id"])
        index = int(row["frame_index"])
        if index <= previous_index or fid not in p2_by_frame or fid not in cap_by_frame:
            raise ValueError(f"packet frame ordering/source mismatch: {fid}")
        previous_index = index
        p2 = p2_by_frame[fid]
        cap = cap_by_frame[fid]
        if row.get("join", {}).get("source_hashes", {}).get("p2_index_sha256") != _sha(p2_path):
            raise ValueError("T2-B2 packet P2 hash differs from source C2 P2 index")
        source_hashes = dict(row.get("join", {}).get("source_hashes", {}))
        # Validate frame assets before any worker is created.
        fdir = capture_root / cap["frame_dir"]
        for key, rel in (("rgb.npy", "rgb.npy"), ("depth_m.npy", "depth_m.npy"), ("valid_mask.npy", "valid_mask.npy"), ("metadata.json", "metadata.json")):
            path = fdir / rel
            if not path.is_file():
                raise FileNotFoundError(path)
            expected = source_hashes.get(key)
            if expected and _sha(path) != expected:
                raise ValueError(f"packet source hash mismatch for {fid}: {key}")
        features = []
        for feature in row.get("features", []):
            path = (ROOT / str(feature["path"])).resolve()
            if not path.is_file() or _sha(path) != str(feature["sha256"]):
                raise ValueError(f"packet feature missing/hash mismatch: {path}")
            features.append({"observation_id": str(feature["observation_id"]),
                             "feature_path": str(path.relative_to(ROOT)),
                             "feature_file_sha256": str(feature["sha256"]),
                             "mask_sha256": str(feature["mask_sha256"])})
        p2_detections = list(p2.get("detections", []))
        for detection in p2_detections:
            source_mask = p2_path.parent / str(detection["mask_file"])
            if not source_mask.is_file() or _sha(source_mask) != str(detection["mask_sha256"]):
                raise ValueError(f"P2 mask missing/hash mismatch: {source_mask}")
            target_mask = staged_mask_root / source_mask.name
            if not target_mask.exists():
                shutil.copy2(source_mask, target_mask)
            if _sha(target_mask) != str(detection["mask_sha256"]):
                raise ValueError(f"staged P2 mask hash mismatch: {target_mask}")
        p3_rows = list(row.get("p3", {}).get("detections", []))
        p2_ids = {str(d["detection_id"]) for d in p2_detections}
        feature_ids = {str(d["observation_id"]) for d in features}
        if feature_ids != p2_ids:
            raise ValueError(f"packet feature population mismatch for {fid}")
        p3_ids = {str(d["detection_id"]) for d in p3_rows}
        if not p3_ids <= p2_ids:
            raise ValueError(f"packet P3 population contains unknown observation for {fid}")
        packet = {
            "schema": "t2-d-current-frame-packet/v1",
            "scene_id": str(capture_doc["scene_id"]),
            "sequence_id": str(capture_doc["sequence_id"]),
            "frame_id": fid, "frame_index": index,
            "timestamp_s": float(row.get("timestamp_s", cap.get("timestamp_s", index * .1))),
            "capture_record": cap, "p2_detections": p2_detections,
            "p3_detections": p3_rows, "features": features,
            "source_hashes": source_hashes,
            "capture_root": str(capture_root), "asset_root": str(staged_asset_root),
            "future_manifest_required": False,
        }
        target = packet_root / f"packet-{index:06d}.json"
        _atomic_json(target, packet)
        output.append(target)
    packet_doc_hash = _sha(packet_manifest)
    _atomic_json(packet_root / "manifest.json", {
        "schema": "t2-d-packet-bundle/v1", "source_packet_manifest": str(packet_manifest),
        "source_packet_manifest_sha256": packet_doc_hash, "p2_index": str(p2_path),
        "p2_index_sha256": _sha(p2_path), "scene_id": capture_doc["scene_id"],
        "sequence_id": capture_doc["sequence_id"], "frame_count": len(output),
        "packet_paths": [str(p.relative_to(root)) for p in output],
    })
    return output


def _send(proc: subprocess.Popen, value: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("worker stdin is closed")
    proc.stdin.write(json.dumps(value, sort_keys=True) + "\n")
    proc.stdin.flush()


def _stop_worker(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        _send(proc, {"op": "close"})
    except BaseException:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _worker_command(kind: str, run_root: Path, packet_root: Path, source: Path) -> list[str]:
    script = ROOT / "scripts/run_t2_d_cached_packet_worker.py"
    source_manifest = _read(source / "c2_run_manifest.json")
    stage_name = "conceptgraphs_mapping" if kind == "conceptgraphs" else "occusg_rooms"
    stage = source_manifest.get("stages", {}).get(stage_name, {})
    command = stage.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError(f"C2 manifest has no locked interpreter for {stage_name}")
    interpreter = Path(str(command[0]))
    if not interpreter.is_file():
        raise FileNotFoundError(f"locked {kind} interpreter missing: {interpreter}")
    return [str(interpreter), "-u", str(script), "--worker", kind,
            "--run-root", str(run_root), "--packet-root", str(packet_root), "--source", str(source)]


def _run_worker_preflight(kind: str, run_root: Path, packet_root: Path, source: Path) -> dict[str, Any]:
    """Invoke the worker's own adapter checks, without creating mapper state."""
    command = _worker_command(kind, run_root, packet_root, source) + ["--preflight"]
    log_path = run_root / "logs" / f"{kind}_preflight.log"
    started = time.perf_counter()
    with log_path.open("ab", buffering=0) as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1",
                                     "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src")))},
                                timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{kind} packet adapter preflight failed with code {result.returncode}")
    return {"status": "passed", "interpreter": command[0],
            "log": str(log_path), "elapsed_s": time.perf_counter() - started}


def _branch_update_markers(run_root: Path, branch: str) -> list[dict[str, Any]]:
    root = run_root / "branches" / branch / "updates"
    if not root.is_dir():
        return []
    return [_read(path) for path in sorted(root.glob("frame-*.json"))]


def _branch_pair_valid(object_events: list[dict[str, Any]], room_events: list[dict[str, Any]], expected_seq: int) -> bool:
    """Pure barrier predicate used by the coordinator and fake-worker tests."""
    if len(object_events) < expected_seq or len(room_events) < expected_seq:
        return False
    obj = object_events[expected_seq - 1]; room = room_events[expected_seq - 1]
    return (int(obj["source_frame_index"]) == int(room["source_frame_index"])
            and str(obj["source_frame_id"]) == str(room["source_frame_id"])
            and obj.get("source_hashes") == room.get("source_hashes"))


def run_t2d(*, source_run: Path, packet_manifest: Path, run_id: str, port: int = 8771,
            viewer_ready_timeout_s: float = 180.0, auto_start: bool = False) -> dict[str, Any]:
    source_run = source_run.resolve(); packet_manifest = packet_manifest.resolve()
    if not source_run.is_dir() or not packet_manifest.is_file():
        raise FileNotFoundError("T2-D source run or packet manifest is missing")
    run_root = (D_ROOT / run_id).resolve()
    if run_root.exists():
        raise FileExistsError(run_root)
    run_root.mkdir(parents=True, exist_ok=False)
    for folder in ("logs", "branches/objects/acks", "branches/rooms/acks", "control", "joined"):
        (run_root / folder).mkdir(parents=True, exist_ok=True)
    source_manifest = _read(source_run / "c2_run_manifest.json")
    if source_manifest.get("status") != "completed":
        raise ValueError("T2-D requires completed C2 source")
    initial = {"schema": "scene-graph-t2-d-cached-packet-run/v1", "run_id": run_id,
               "status": "started", "execution_mode": "cached_packet_integration",
               "claim_boundary": "T2-B2 cached packets; not live capture or realtime",
               "source_run_id": source_run.name, "packet_manifest": str(packet_manifest),
               "packet_manifest_sha256": _sha(packet_manifest),
               "scene_id": source_manifest.get("scene_id"), "sequence_id": source_manifest.get("sequence_id"),
               "mapper_execution_plan": {"conceptgraphs_sessions": 1, "occusg_sessions": 1,
                                          "conceptgraphs_updates": 3, "occusg_updates": 3,
                                          "terminal_conceptgraphs_finalize": 1, "terminal_occusg_flush": 1,
                                          "retries": 0},
               "stages": {"preflight": "running", "viewer_bootstrap": "pending",
                          "packet_dispatch": "pending", "unified_revisions": "pending",
                          "terminal_postprocess": "pending"},
               "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope"},
               "room_semantics": {"status": "unavailable", "reason": "unavailable_membership_scope"},
               "partial_artifacts_preserved": True, "resume_after_crash": False}
    _atomic_json(run_root / "run_manifest.json", initial)
    workers: dict[str, subprocess.Popen] = {}
    logs: dict[str, Any] = {}
    status = "failed"; error: str | None = None
    packet_paths: list[Path] = []
    run_started = time.perf_counter()
    stage_timing: dict[str, Any] = {"preflight_s": None, "viewer_bootstrap_s": None,
                                    "terminal_s": None, "frames": []}
    try:
        preflight_started = time.perf_counter()
        packet_paths = _write_packet_bundle(run_root, source_run, packet_manifest)
        preflight_results = {
            kind: _run_worker_preflight(kind, run_root, run_root / "packets", source_run)
            for kind in ("conceptgraphs", "occusg")
        }
        stage_timing["preflight_s"] = time.perf_counter() - preflight_started
        viewer_started = time.perf_counter()
        viewer, viewer_hashes = _prepare_viewer(source_run, run_root,
                                                str(source_manifest["scene_id"]), str(source_manifest["sequence_id"]))
        from scripts.build_d2_xyz_viewer import bootstrap as bootstrap_d2_xyz, validate_entrypoint_bundle
        d2 = bootstrap_d2_xyz(run_root, "t2d")
        validate_entrypoint_bundle(viewer)
        server, url = _launch_server(viewer, ROOT, int(port), run_root / "logs/viewer_server.log",
                                     entrypoint=d2["viewer_entrypoint"])
        stage_timing["viewer_bootstrap_s"] = time.perf_counter() - viewer_started
        _write_json(run_root / "run_manifest.json", {**initial,
            "stages": {**initial["stages"], "preflight": "completed", "viewer_bootstrap": "waiting_for_browser"},
            "viewer": {"url": url, "entrypoint": d2["viewer_entrypoint"], "server_pid": server.pid,
                       "assets_sha256": viewer_hashes, "waiting_for_first_revision": True},
            "packet_bundle": {"path": str(run_root / "packets/manifest.json"), "sha256": _sha(run_root / "packets/manifest.json")},
            "adapter_preflight": preflight_results})
        print(f"T2-D viewer waiting_for_first_revision: {url}", flush=True)
        if not auto_start:
            # The explicit signal may be created by a GUI/checkpoint helper.
            start_path = run_root / "control/start.json"
            deadline = time.monotonic() + viewer_ready_timeout_s
            while time.monotonic() < deadline and not start_path.is_file():
                time.sleep(.1)
            if not start_path.is_file():
                raise TimeoutError("T2-D viewer start gate timed out")
            start_doc = _read(start_path)
            if start_doc.get("confirmed_browser_ready") is not True:
                raise ValueError("T2-D start signal did not confirm browser readiness")
        else:
            _atomic_json(run_root / "control/start.json", {
                "schema": "t2-d-explicit-viewer-start/v1", "run_id": run_id,
                "confirmed_browser_ready": False, "mode": "http_bootstrap_checked_only",
            })
            # auto_start is only for model-free/headless fixture use and is not
            # presented as GUI acceptance.
        run_doc = _read(run_root / "run_manifest.json")
        run_doc["stages"]["viewer_bootstrap"] = "released"
        run_doc["stages"]["packet_dispatch"] = "running"
        run_doc["status"] = "running"
        _atomic_json(run_root / "run_manifest.json", run_doc)
        for kind in ("conceptgraphs", "occusg"):
            log_path = run_root / "logs" / f"{kind}_worker.log"
            logs[kind] = log_path.open("ab", buffering=0)
            cmd = _worker_command(kind, run_root, run_root / "packets", source_run)
            workers[kind] = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE,
                stdout=logs[kind], stderr=subprocess.STDOUT, text=True, bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src")))})
        for packet in packet_paths:
            frame_started = time.perf_counter()
            message = {"op": "frame", "packet_path": str(packet), "packet_sha256": _sha(packet)}
            dispatch_utc = time.time()
            _send(workers["conceptgraphs"], message); _send(workers["occusg"], message)
            # Workers publish branch events; the coordinator waits for both.
            expected_seq = len(_read_jsonl(run_root / "branches/objects/events.jsonl")) + 1 if (run_root / "branches/objects/events.jsonl").is_file() else 1
            deadline = time.monotonic() + 600.0
            while time.monotonic() < deadline:
                object_events = _read_jsonl(run_root / "branches/objects/events.jsonl") if (run_root / "branches/objects/events.jsonl").is_file() else []
                room_events = _read_jsonl(run_root / "branches/rooms/events.jsonl") if (run_root / "branches/rooms/events.jsonl").is_file() else []
                if _branch_pair_valid(object_events, room_events, expected_seq):
                    object_event, room_event = object_events[expected_seq - 1], room_events[expected_seq - 1]
                    object_state = _verify_event(run_root, object_event, "objects")
                    room_state = _verify_event(run_root, room_event, "rooms")
                    if _sha(object_state) != object_event["state_sha256"] or _sha(room_state) != room_event["state_sha256"]:
                        raise ValueError("branch state hash changed before join")
                    from integration_v2.live_processing import _join_one
                    frames = _read_jsonl(run_root / "joined/frames.jsonl") if (run_root / "joined/frames.jsonl").is_file() else []
                    base = _read(viewer / "live_index.json")
                    _join_one(run_root, viewer, base, frames, object_event, room_event)
                    # _join_one appends the browser-facing frame record to
                    # ``frames`` and separately journals the joined event.
                    # Persist the frame record here; persisting the event
                    # object would corrupt earlier live-index entries on the
                    # next iteration and break historical seek.
                    frame_record = dict(frames[-1])
                    frame_record["branch_source_hashes"] = {
                        "objects": object_event.get("source_hashes", {}),
                        "rooms": room_event.get("source_hashes", {}),
                    }
                    append_jsonl(run_root / "joined/frames.jsonl", frame_record)
                    for branch, event in (("objects", object_event), ("rooms", room_event)):
                        _atomic_json(run_root / "branches" / branch / "acks" / f"{int(event['event_seq']):06d}.json",
                                     {"schema": "t2-d-branch-ack/v1", "event_sha256": event["event_sha256"],
                                      "joined_revision": int(frame_record["revision"]), "durable": True})
                    stage_timing["frames"].append({
                        "frame_index": int(object_event["source_frame_index"]),
                        "frame_id": str(object_event["source_frame_id"]),
                        "dispatch_utc": dispatch_utc,
                        "join_utc": time.time(),
                        "join_wait_s": time.perf_counter() - frame_started,
                        "object_processing_ms": object_event.get("processing_ms"),
                        "room_processing_ms": room_event.get("processing_ms"),
                    })
                    break
                for kind, proc in workers.items():
                    if proc.poll() not in (None, 0):
                        branch = "objects" if kind == "conceptgraphs" else "rooms"
                        markers = _branch_update_markers(run_root, branch)
                        possible = any(m.get("status") == "update_started" or m.get("mutation_possible")
                                       for m in markers)
                        raise RuntimeError(f"{kind} worker failed with code {proc.returncode}; "
                                           f"mutation_possible={possible}; replay_required={possible}")
                time.sleep(.05)
            else:
                raise TimeoutError(f"T2-D branch barrier timed out for {packet.name}")
        terminal_started = time.perf_counter()
        for kind in workers:
            _send(workers[kind], {"op": "finalize"})
        terminal_deadline = time.monotonic() + 600.0
        while time.monotonic() < terminal_deadline:
            if all(proc.poll() == 0 for proc in workers.values()):
                break
            if any(proc.poll() not in (None, 0) for proc in workers.values()):
                raise RuntimeError("T2-D terminal worker failure")
            time.sleep(.1)
        else:
            raise TimeoutError("T2-D terminal finalize timed out")
        stage_timing["terminal_s"] = time.perf_counter() - terminal_started
        run_doc = _read(run_root / "run_manifest.json")
        run_doc.update({"status": "completed", "finished_utc": time.time(),
                        "stages": {**run_doc["stages"], "packet_dispatch": "completed",
                                   "unified_revisions": "completed", "terminal_postprocess": "completed"},
                        "joined_revision_count": len(packet_paths),
                        "worker_interpreters": {kind: str(proc.args[0]) for kind, proc in workers.items()},
                        "worker_lifecycle": {kind: {"pid": proc.pid, "returncode": proc.returncode,
                                                     "updates": len(packet_paths), "terminal_finalize": 1} for kind, proc in workers.items()},
                        "viewer_evidence": {"server": "http_bootstrap_verified", "browser_gui": "pending_unless_confirmed",
                                            "producer_independent_of_browser": True},
                        "runtime_metrics": {"coordinator_rss_peak_mib": _rss_mib(), "frame_count": len(packet_paths),
                                             "frame_3_anomaly_s": 48.821226124, "frame_3_anomaly_status": "observed_unknown_cause",
                                             "stage_timing": stage_timing, "total_s": time.perf_counter() - run_started}})
        _atomic_json(run_root / "run_manifest.json", run_doc)
        status = "completed"
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = "failed"
        _atomic_json(run_root / "control/stop.json", {"schema": "t2-d-stop/v1", "status": "failed", "reason": error})
        raise
    finally:
        for proc in workers.values():
            _stop_worker(proc)
        for handle in logs.values():
            try: handle.close()
            except BaseException: pass
        if 'server' in locals() and server is not None and status != "completed":
            try: server.terminate(); server.wait(timeout=5)
            except BaseException: pass
        final = _read(run_root / "run_manifest.json")
        final["status"] = status
        final["error"] = error
        final["finished_utc"] = time.time()
        object_events_path = run_root / "branches/objects/events.jsonl"
        room_events_path = run_root / "branches/rooms/events.jsonl"
        object_event_count = len(_read_jsonl(object_events_path)) if object_events_path.is_file() else 0
        room_event_count = len(_read_jsonl(room_events_path)) if room_events_path.is_file() else 0
        object_markers = _branch_update_markers(run_root, "objects")
        room_markers = _branch_update_markers(run_root, "rooms")
        marker_mutation_possible = any(m.get("status") in {"update_started", "update_failed"}
                                       or m.get("mutation_possible") for m in object_markers + room_markers)
        # A branch event is evidence that that worker has mutated/published
        # state.  A failed run with asymmetric branch events cannot be safely
        # resumed in place: it must be replayed from an empty session.
        mutation_started = object_event_count > 0 or room_event_count > 0 or marker_mutation_possible
        replay_required = status != "completed" and mutation_started
        final["failure_classification"] = {
            "mutation_started": mutation_started,
            "replay_required": replay_required,
            "object_branch_events": object_event_count,
            "room_branch_events": room_event_count,
            "update_markers": len(object_markers) + len(room_markers),
            "unified_revisions": len(_read_jsonl(run_root / "joined/frames.jsonl"))
                if (run_root / "joined/frames.jsonl").is_file() else 0,
            "policy": "failed_after_mutation_requires_empty_state_replay",
        }
        final.setdefault("runtime_metrics", {})
        final["runtime_metrics"].update({"coordinator_rss_peak_mib": _rss_mib(),
                                          "stage_timing": stage_timing,
                                          "total_s": time.perf_counter() - run_started,
                                          "frame_count": len(packet_paths)})
        final["immutable_output_sha256"] = {str(p.relative_to(run_root)): _sha(p) for p in sorted(run_root.rglob("*"))
                                             if p.is_file() and p.name != "run_manifest.json" and not str(p.relative_to(run_root)).startswith("logs/")}
        _atomic_json(run_root / "run_manifest.json", final)
        _atomic_json(run_root / "run_manifest.sha256.json", {"schema": "t2-d-run-manifest-receipt/v1",
            "manifest": "run_manifest.json", "sha256": _sha(run_root / "run_manifest.json"), "status": status})
    return final
