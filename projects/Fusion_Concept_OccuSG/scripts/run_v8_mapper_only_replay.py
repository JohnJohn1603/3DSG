#!/usr/bin/env python3
"""Run the two persistent mapper sessions over already validated v8 packets.

No capture or perception stage is started here. Source packets/assets remain
read-only; a fresh run directory receives packet copies and mapper outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))

from integration_v2.v8_ordered_replay import (  # noqa: E402
    C2_DEFAULT, EXPECTED_INDICES, RUNS_ROOT, _bootstrap_replay_viewer,
    _hash, _mapper_ready_worker_pairs, _prepare_viewer, _read,
    _rss_bytes, _spawn_mapper, _stop_owned_processes, _verify_event,
    _wait_response, _write_integrity,
)
from integration_v2.camera_live_contract import (  # noqa: E402
    host_monotonic_domain, validate_mapper_pair_events,
)
from integration_v2.live_processing import (  # noqa: E402
    _join_one, _write_live_index,
)
from scripts.run_camera_live_pilot import (  # noqa: E402
    _wait_ready_workers, atomic_json, send,
)
from standalone_sg.serialization.live_revision_io import append_jsonl  # noqa: E402

SOURCE_RUN_DEFAULT = RUNS_ROOT / "hm3d00802_t1_1_all13_replay_20261019_v1"
DEFAULT_RUN_ID = "hm3d00802_t1_1_all13_mapper_only_20261019_v1"


def _source_processes_alive(source_run: Path) -> list[str]:
    """Refuse source mutation/read races if an old mapper still owns the run."""
    needle = str(source_run.resolve()).encode()
    active = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes()
        except OSError:
            continue
        if needle in args and b"run_t2_d_cached_packet_worker.py" in args:
            active.append(str(cmdline.parent.name))
    return active


def _load_and_validate_source(source_run: Path) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]]]:
    manifest_path = source_run / "run_manifest.json"
    receipt_path = source_run / "run_manifest.sha256.json"
    addendum_path = source_run / "post_run_validation_addendum.json"
    for path in (manifest_path, receipt_path, addendum_path):
        if not path.is_file():
            raise FileNotFoundError(f"failed perception run provenance missing: {path}")
    manifest, receipt, addendum = map(_read, (manifest_path, receipt_path, addendum_path))
    if manifest.get("status") != "failed" or manifest.get("run_id") != source_run.name:
        raise ValueError("mapper-only source must be the preserved failed all-13 replay run")
    if _hash(manifest_path) != receipt.get("sha256"):
        raise ValueError("source failed-run manifest receipt hash mismatch")
    if addendum.get("original_run_manifest_sha256") != _hash(manifest_path):
        raise ValueError("source failure addendum does not match the preserved manifest")
    root_cause = addendum.get("root_cause", {})
    if (addendum.get("outcome") != "failed_before_mapper_update"
            or root_cause.get("mapping_update_started") is not False
            or root_cause.get("object_update_markers") != 0
            or root_cause.get("room_update_markers") != 0
            or root_cause.get("unified_prefix_commits") != 0):
        raise ValueError("source failure was not proven to precede mapper mutation")
    if manifest.get("failure_classification", {}).get("mapper_mutation_possible") is not False:
        raise ValueError("source manifest does not prove zero mapper mutation")
    if addendum.get("perception_validation", {}).get("all_13_packets_readonly_validated") is not True:
        raise ValueError("source addendum does not record validated perception for all 13 packets")
    active = _source_processes_alive(source_run)
    if active:
        raise RuntimeError(f"source mapper workers still reference failed run: {active}")

    packet_dir = source_run / "packets"
    rows: list[tuple[Path, dict[str, Any]]] = []
    for index in EXPECTED_INDICES:
        path = packet_dir / f"joined-{index:06d}.json"
        if not path.is_file():
            raise FileNotFoundError(f"validated joined packet missing: {path}")
        recorded = manifest.get("immutable_output_sha256", {}).get(path.relative_to(source_run).as_posix())
        if not recorded or _hash(path) != recorded:
            raise ValueError(f"joined packet hash absent/mismatched in source integrity manifest: {path}")
        packet = _read(path)
        if (int(packet.get("frame_index", -1)) != index
                or packet.get("perception_status") != "completed"
                or not packet.get("frame_id") or not packet.get("source_hashes")):
            raise ValueError(f"joined packet identity/perception status invalid: {path}")
        if packet.get("scene_id") != manifest.get("scene_id") or packet.get("sequence_id") != manifest.get("sequence_id"):
            raise ValueError(f"joined packet scene/sequence differs from failed-run provenance: {path}")
        for field in ("timestamp_s", "frame_index"):
            if packet.get(field) is None:
                raise ValueError(f"joined packet missing current-frame {field}: {path}")
        rows.append((path, packet))
    if [int(packet["frame_index"]) for _, packet in rows] != EXPECTED_INDICES:
        raise ValueError("source joined packets are not the exact locked 13-frame order")
    if len({str(packet["frame_id"]) for _, packet in rows}) != 13:
        raise ValueError("source joined packet frame IDs are not unique")
    return manifest, rows


def run_mapper_only(*, source_run: Path = SOURCE_RUN_DEFAULT,
                    mapper_config_source: Path = C2_DEFAULT,
                    run_id: str = DEFAULT_RUN_ID,
                    runtime_cap_s: int = 30 * 60,
                    available_ram_floor_bytes: int = 3 * 1024**3) -> dict[str, Any]:
    source_run = Path(source_run).resolve()
    c2_root = Path(mapper_config_source).resolve()
    run_root = (RUNS_ROOT / run_id).resolve()
    if run_root.exists():
        raise FileExistsError(f"refusing to overwrite mapper-only run: {run_root}")
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
    available_ram = int(mem["MemAvailable"].split()[0]) * 1024
    if available_ram < available_ram_floor_bytes:
        raise MemoryError(f"RAM preflight below existing 3 GiB safety floor: {available_ram} bytes")

    run_root.mkdir(parents=True, exist_ok=False)
    for rel in ("logs", "ready", "control", "packets", "branches/objects/acks",
                "branches/rooms/acks", "joined"):
        (run_root / rel).mkdir(parents=True, exist_ok=True)
    run_started = time.perf_counter()
    deadline = time.monotonic() + runtime_cap_s
    processes: list[subprocess.Popen] = []
    handles: list[Any] = []
    status = "preflight"
    manifest: dict[str, Any] = {
        "schema": "v8-mapper-only-replay/v1", "run_id": run_id,
        "status": "preflight", "execution_mode": "mapper_only_replay_from_validated_perception_packets",
        "source_run_id": source_run.name, "source_run": str(source_run),
        "mapper_config_source": str(c2_root), "scene_id": None, "sequence_id": None,
        "ordered_frame_indices": EXPECTED_INDICES, "frame_count_expected": 13,
        "execution_plan": {"capture_runs": 0, "perception_runs": 0,
            "conceptgraphs_sessions": 1, "conceptgraphs_updates": 13,
            "conceptgraphs_terminal_postprocess": 1, "occusg_sessions": 1,
            "occusg_updates": 13, "occusg_terminal_flush": 1,
            "unified_prefix_revisions": 13, "mapper_retries": 0},
        "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope"},
        "room_semantics": {"status": "unavailable", "reason": "unavailable_membership_scope"},
        "scope_declaration_applied": False,
        "terminal_postprocess_is_separate": True,
        "perception_reused_read_only": True,
        "partial_artifacts_preserved": True,
    }
    atomic_json(run_root / "run_manifest.json", manifest)
    try:
        source_manifest, source_rows = _load_and_validate_source(source_run)
        # Copy exact joined packet bytes into the fresh run. Their referenced
        # capture/mask/feature assets remain source-owned and read-only.
        packet_refs = []
        for source_path, packet in source_rows:
            target = run_root / "packets" / f"packet-{int(packet['frame_index']):06d}.json"
            shutil.copy2(source_path, target)
            if _hash(target) != _hash(source_path):
                raise IOError(f"mapper input packet copy hash mismatch: {target}")
            packet_refs.append({"frame_index": int(packet["frame_index"]),
                "frame_id": str(packet["frame_id"]), "source_path": str(source_path),
                "source_sha256": _hash(source_path), "run_packet_path": str(target),
                "run_packet_sha256": _hash(target), "source_hashes": packet["source_hashes"]})
        manifest.update({"scene_id": source_manifest["scene_id"],
            "sequence_id": source_manifest["sequence_id"],
            "route_run_id": source_manifest.get("route_run_id"),
            "source_manifest_sha256": _hash(source_run / "run_manifest.json"),
            "source_addendum_sha256": _hash(source_run / "post_run_validation_addendum.json"),
            "source_packet_refs": packet_refs,
            "source_perception_summary": source_manifest.get("perception_summary"),
            "worker_interpreters": source_manifest["worker_interpreters"],
            "mapping_seed": source_manifest["mapping_seed"],
            "mapping_effective_config": source_manifest["mapping_effective_config"],
            "source_hashes": source_manifest["source_hashes"],
            "ram_preflight": {"available_bytes": available_ram,
                               "required_floor_bytes": available_ram_floor_bytes},
            "stages": {"source_packet_validation": "completed",
                       "mapper_adapter_preflight": "running", "viewer_bootstrap": "not_started",
                       "mapper_sessions": "not_started", "unified_revisions": "not_started",
                       "terminal": "not_started"}})
        atomic_json(run_root / "run_manifest.json", manifest)

        # Use each mapper's locked interpreter and exact runtime packet adapter.
        from integration_v2.v8_ordered_replay import _preflight_mapper_adapters
        preflight_start = time.perf_counter()
        mapper_preflight = _preflight_mapper_adapters(run_root, c2_root,
            {"conceptgraphs": source_manifest["worker_interpreters"]["conceptgraphs"],
             "occusg": source_manifest["worker_interpreters"]["occusg"]}, model_free=False)
        if any(row.get("frame_count") != 13 for row in mapper_preflight.values()):
            raise ValueError("one or more runtime mapper adapters did not validate all 13 packets")
        manifest["mapper_adapter_preflight"] = mapper_preflight
        manifest["mapper_adapter_preflight_wall_s"] = time.perf_counter() - preflight_start
        manifest["stages"]["mapper_adapter_preflight"] = "completed"
        atomic_json(run_root / "run_manifest.json", manifest)

        # Prepare and validate D2 before either mapper starts.
        viewer = _prepare_viewer(run_root, manifest["scene_id"], manifest["sequence_id"])
        base_index, d2 = _bootstrap_replay_viewer(run_root, viewer, manifest)
        manifest["stages"]["viewer_bootstrap"] = "completed"
        manifest["viewer"] = {**manifest.get("viewer", {}), **d2,
            "gui_acceptance": "not_browser_verified"}
        atomic_json(run_root / "run_manifest.json", manifest)

        route_hash = source_manifest["source_hashes"]["route_config"]
        bootstrap_id = uuid.uuid4().hex
        interpreters = source_manifest["worker_interpreters"]
        cg = _spawn_mapper("conceptgraphs", interpreters["conceptgraphs"], c2_root,
            run_root, run_id, bootstrap_id, route_hash, int(source_manifest["mapping_seed"]), handles, processes)
        occ = _spawn_mapper("occusg", interpreters["occusg"], c2_root,
            run_root, run_id, bootstrap_id, route_hash, int(source_manifest["mapping_seed"]), handles, processes)
        manifest["worker_lifecycle"] = {"conceptgraphs": {"pid": cg.pid, "initialization_attempts": 1},
                                         "occusg": {"pid": occ.pid, "initialization_attempts": 1}}
        manifest["status"] = "starting_mapper_sessions"
        atomic_json(run_root / "run_manifest.json", manifest)
        readiness = _wait_ready_workers(run_root, _mapper_ready_worker_pairs(cg, occ),
            run_id=run_id, bootstrap_id=bootstrap_id,
            route_config_sha256=route_hash, timeout_s=min(180, deadline - time.monotonic()),
            marker_kinds={"occusg": "occusg_rooms"})
        manifest["mapper_worker_readiness"] = readiness
        manifest["status"] = "running_mapper_sessions"
        manifest["stages"]["mapper_sessions"] = "running"
        atomic_json(run_root / "run_manifest.json", manifest)

        events_path = run_root / "logs/execution_events.jsonl"
        def event(name: str, **fields: Any) -> None:
            payload = {"event": name, "monotonic_s": time.monotonic(),
                       "clock_domain": host_monotonic_domain(), "wall_time_unix_s": time.time(), **fields}
            append_jsonl(events_path, payload)
            print(json.dumps(payload, sort_keys=True), flush=True)

        mapping_peaks: dict[str, int] = {}
        frame_timings = []
        from integration_v2.live_processing import _read as read_json
        for ordinal, packet_ref in enumerate(packet_refs, start=1):
            if time.monotonic() >= deadline:
                raise TimeoutError("mapper-only pass exceeded approved 30-minute cap")
            packet_path = Path(packet_ref["run_packet_path"])
            packet = read_json(packet_path)
            digest = _hash(packet_path)
            message = {"op": "frame", "packet_path": str(packet_path), "packet_sha256": digest}
            frame_id = str(packet["frame_id"]); index = int(packet["frame_index"])
            started = time.perf_counter(); dispatch = time.monotonic()
            event("mapper_pair_dispatch_started", ordinal=ordinal, frame_index=index,
                  frame_id=frame_id, packet_sha256=digest, source_hashes=packet["source_hashes"])
            # Dispatch to both sessions before waiting for either ACK/event.
            send(cg, message); send(occ, message)
            object_log = run_root / "branches/objects/events.jsonl"
            room_log = run_root / "branches/rooms/events.jsonl"
            pair_deadline = min(deadline, time.monotonic() + 1200)
            while time.monotonic() < pair_deadline:
                for name, proc in (("conceptgraphs", cg), ("occusg", occ)):
                    sample = _rss_bytes(proc.pid)
                    if sample is not None:
                        mapping_peaks[name] = max(mapping_peaks.get(name, 0), sample)
                def complete_events(path: Path) -> list[dict[str, Any]]:
                    if not path.is_file(): return []
                    result = []
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try: result.append(json.loads(line))
                        except json.JSONDecodeError: break
                    return result
                object_events = complete_events(object_log); room_events = complete_events(room_log)
                if len(object_events) >= ordinal and len(room_events) >= ordinal:
                    object_event, room_event = object_events[ordinal - 1], room_events[ordinal - 1]
                    for branch_event in (object_event, room_event):
                        if (int(branch_event["source_frame_index"]) != index
                                or str(branch_event["source_frame_id"]) != frame_id
                                or branch_event.get("source_hashes") != packet.get("source_hashes")):
                            raise ValueError(f"mapper pair identity/provenance mismatch at {frame_id}")
                    validate_mapper_pair_events(packet, object_event, room_event)
                    _verify_event(run_root, object_event, "objects")
                    _verify_event(run_root, room_event, "rooms")
                    joined_path = run_root / "joined/frames.jsonl"
                    frames = complete_events(joined_path)
                    _join_one(run_root, viewer, base_index, frames, object_event, room_event)
                    record = dict(frames[-1])
                    record["branch_source_hashes"] = {"objects": object_event["source_hashes"],
                                                       "rooms": room_event["source_hashes"]}
                    append_jsonl(joined_path, record)
                    for branch, branch_event in (("objects", object_event), ("rooms", room_event)):
                        atomic_json(run_root / "branches" / branch / "acks" /
                            f"{int(branch_event['event_seq']):06d}.json",
                            {"schema": "t2-d-branch-ack/v1", "event_sha256": branch_event["event_sha256"],
                             "joined_revision": ordinal, "durable": True})
                    frames = complete_events(joined_path)
                    _write_live_index(run_root, viewer, base_index, frames, "running")
                    timing = {"ordinal": ordinal, "frame_index": index, "frame_id": frame_id,
                        "dispatch_monotonic_s": dispatch, "commit_monotonic_s": time.monotonic(),
                        "clock_domain": host_monotonic_domain(), "pair_wait_wall_s": time.perf_counter() - started,
                        "conceptgraphs_processing_ms": object_event.get("processing_ms"),
                        "occusg_processing_ms": room_event.get("processing_ms"),
                        "object_revision": object_event.get("source_revision"),
                        "room_revision": room_event.get("source_revision")}
                    frame_timings.append(timing)
                    event("unified_revision_committed", ordinal=ordinal, frame_index=index,
                          frame_id=frame_id, object_event_seq=object_event.get("event_seq"),
                          room_event_seq=room_event.get("event_seq"))
                    manifest["unified_prefix_revisions"] = ordinal
                    manifest["last_committed_frame"] = {"frame_index": index, "frame_id": frame_id}
                    manifest["frame_timings"] = frame_timings
                    atomic_json(run_root / "run_manifest.json", manifest)
                    break
                if cg.poll() not in (None, 0) or occ.poll() not in (None, 0):
                    raise RuntimeError(f"mapper exited before paired commit at {frame_id}: cg={cg.poll()} occ={occ.poll()}")
                time.sleep(0.05)
            else:
                raise TimeoutError(f"paired mapper event timeout at frame {frame_id}")

        terminal_started = time.perf_counter()
        if time.monotonic() >= deadline:
            raise TimeoutError("mapper-only pass exceeded approved cap before terminal stages")
        send(cg, {"op": "finalize"}); send(occ, {"op": "finalize"})
        cg.wait(timeout=max(1.0, min(600, deadline - time.monotonic())))
        occ.wait(timeout=max(1.0, min(600, deadline - time.monotonic())))
        if cg.returncode != 0 or occ.returncode != 0:
            raise RuntimeError(f"terminal mapper stage failed: conceptgraphs={cg.returncode}, occusg={occ.returncode}")
        terminal_s = time.perf_counter() - terminal_started
        terminal_outputs = {
            "conceptgraphs": read_json(run_root / "branches/objects/terminal.json"),
            "occusg": read_json(run_root / "branches/rooms/terminal.json")}
        _write_live_index(run_root, viewer, base_index,
            complete_events(run_root / "joined/frames.jsonl"), "completed",
            {"status": "terminal", "frames": 13,
             "conceptgraphs_postprocess": "branches/objects/terminal.json",
             "occusg_flush": "branches/rooms/terminal.json"})
        # Promote the bootstrap viewer documents only after the complete live
        # index and terminal records are durable. The bootstrap manifest starts
        # with frame_count=0 and must not remain the user-facing final status.
        d2_manifest_name = f"d2_xyz_manifest_v8_13_keyframes.json"
        d2_manifest_path = viewer / d2_manifest_name
        d2_doc = read_json(d2_manifest_path)
        complete_index = read_json(viewer / "live_index.json")
        d2_doc.update({"status": "completed", "frame_count": len(complete_index["frames"]),
            "revision_alignment": [{key: row.get(key) for key in (
                "revision", "frame_index", "frame_id", "object_source_revision",
                "room_source_revision", "timestamp_s")} for row in complete_index["frames"]],
            "final_postprocess_status": "available" if complete_index.get("final_postprocess", {}).get("status") == "available" else "unavailable"})
        d2_doc["output_files_sha256"] = {name: _hash(viewer / name) for name in (
            "index.html", "index_d2_v8_13_keyframes.html", "xyz_renderer_d2_v8_13_keyframes.js")}
        atomic_json(d2_manifest_path, d2_doc)
        from scripts.build_d2_xyz_viewer import write_entrypoint_pointer
        pointer_path = write_entrypoint_pointer(viewer, d2_manifest_name, d2_doc)
        manifest.update({"status": "completed", "stages": {**manifest["stages"],
            "mapper_sessions": "completed", "unified_revisions": "completed", "terminal": "completed"},
            "mapper_execution_counts": {"conceptgraphs_sessions": 1, "conceptgraphs_updates": 13,
                "conceptgraphs_terminal_postprocess": 1, "occusg_sessions": 1, "occusg_updates": 13,
                "occusg_terminal_flush": 1, "paired_commits": 13, "retries": 0},
            "mapping_peak_rss_bytes_by_worker": mapping_peaks,
            "mapper_worker_exit_codes": {"conceptgraphs": cg.returncode, "occusg": occ.returncode},
            "worker_lifecycle": {"conceptgraphs": {"pid": cg.pid, "initialization_attempts": 1,
                "exit_code": cg.returncode, "state": "exited"},
                "occusg": {"pid": occ.pid, "initialization_attempts": 1,
                "exit_code": occ.returncode, "state": "exited"}},
            "terminal_outputs": terminal_outputs,
            "runtime_metrics": {"total_wall_s": time.perf_counter() - run_started,
                "mapper_adapter_preflight_wall_s": manifest["mapper_adapter_preflight_wall_s"],
                "frame_pair_wait_s": sum(x["pair_wait_wall_s"] for x in frame_timings),
                "terminal_wall_s": terminal_s,
                "coordinator_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
                "peak_rss_scope": "separate per-process samples; not summed as host peak"},
            "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope"},
            "room_semantics": {"status": "unavailable", "reason": "unavailable_membership_scope"},
            "quality_claim": "none; object counts are not quality metrics",
            "viewer": {**manifest.get("viewer", {}), "status": "completed",
                       "entrypoint": d2_doc.get("viewer_entrypoint"),
                       "frame_count": len(complete_index["frames"]),
                       "d2_manifest_sha256": _hash(d2_manifest_path),
                       "entrypoint_pointer_sha256": _hash(pointer_path),
                       "gui_acceptance": "not_browser_verified"},
            "source_code_sha256": {rel: _hash(ROOT / rel) for rel in (
                "scripts/run_v8_mapper_only_replay.py", "scripts/run_t2_d_cached_packet_worker.py",
                "scripts/run_camera_live_pilot.py", "scripts/build_d2_xyz_viewer.py",
                "scripts/causal_timeline_viewer.html", "scripts/xyz_renderer.js",
                "integration_v2/conceptgraphs_session.py", "integration_v2/occusg_session.py",
                "integration_v2/camera_live_contract.py", "integration_v2/live_processing.py")}})
        _write_integrity(run_root, manifest)
        status = "completed"
        return manifest
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
        manifest["status"] = status
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        object_markers = list((run_root / "branches/objects/updates").glob("frame-*.json"))
        room_markers = list((run_root / "branches/rooms/updates").glob("frame-*.json"))
        manifest["failure_classification"] = {
            "mapper_mutation_possible": bool(object_markers or room_markers),
            "replay_required": bool(object_markers or room_markers),
            "unified_prefix_revisions": int(manifest.get("unified_prefix_revisions", 0)),
            "retry_count": 0}
        atomic_json(run_root / "run_manifest.json", manifest)
        raise
    finally:
        cleanup = _stop_owned_processes(processes)
        for handle in handles:
            try: handle.close()
            except BaseException: pass
        if status != "completed":
            manifest["owned_worker_cleanup"] = cleanup
            manifest["status"] = status
            try: _write_integrity(run_root, manifest)
            except BaseException: atomic_json(run_root / "run_manifest.json", manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, default=SOURCE_RUN_DEFAULT)
    parser.add_argument("--mapper-config-source", type=Path, default=C2_DEFAULT)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--runtime-cap-s", type=int, default=30 * 60)
    args = parser.parse_args()
    try:
        result = run_mapper_only(source_run=args.source_run,
            mapper_config_source=args.mapper_config_source,
            run_id=args.run_id, runtime_cap_s=args.runtime_cap_s)
    except BaseException as exc:
        print(f"mapper-only replay {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps({"status": result.get("status"), "run_id": result.get("run_id"),
        "run_root": str(RUNS_ROOT / result.get("run_id", "")),
        "unified_prefix_revisions": result.get("unified_prefix_revisions"),
        "mapper_execution_counts": result.get("mapper_execution_counts")}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
