#!/usr/bin/env python3
"""Replay one durable 3DSG keyframe through the real legacy mapper sessions.

All generated data and mapper state are written below the selected 3DSG run
directory. The ConceptGraphs and OccuSG repositories are read-only inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from typing import Any

from exploration.process_priority import apply_process_nice

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exploration.graph_bridge import GraphObservation  # noqa: E402
from exploration.keyframe_scheduler import PersistedKeyframe  # noqa: E402
from exploration.keyframe_store import KeyframeStore  # noqa: E402
from exploration.legacy_capture import LegacyCaptureStore  # noqa: E402
from exploration.legacy_mapper_packets import (  # noqa: E402
    build_current_mapper_packet,
)
from exploration.p2_worker_client import P2WorkerClient  # noqa: E402


DEFAULT_3DSG_RUN = ROOT / "runs/integrated-floor-calibrated-graph-120s-20261023-a"
from exploration.handoff_paths import CG_ROOT, OCC_ROOT, config_root, source_paths
DEFAULT_CG = CG_ROOT
DEFAULT_OCC = OCC_ROOT
DEFAULT_SOURCE = config_root()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def load_observation(run_root: Path, frame_index: int) -> GraphObservation:
    store = KeyframeStore(run_root)
    item_dir = store.root / f"frame_{frame_index:08d}"
    manifest = json.loads((item_dir / "manifest.json").read_text(encoding="utf-8"))
    metadata = json.loads((item_dir / "metadata.json").read_text(encoding="utf-8"))
    payload_bytes = sum((item_dir / name).stat().st_size for name in (*manifest["files"], "manifest.json"))
    item = PersistedKeyframe(
        run_id=str(metadata["run_id"]), scene_id=str(metadata["scene_id"]),
        frame_id=str(metadata["frame_id"]), frame_index=int(metadata["frame_index"]),
        host_monotonic_s=float(metadata["host_monotonic_s"]), payload_ref=str(item_dir),
        payload_bytes=payload_bytes, payload_sha256=str(manifest["package_sha256"]),
        novelty=float(metadata["novelty"]), protected_reason=metadata.get("protected_reason"),
        metadata=dict(metadata.get("extra", {})),
    )
    rgb, packet = store.load(item)
    return GraphObservation(item, rgb, packet)


def load_source_paths(args: argparse.Namespace) -> dict[str, Path]:
    return source_paths(args.conceptgraphs_root.resolve(), args.occusg_root.resolve())


def run_p2(observation: GraphObservation, paths: dict[str, Path], output: Path) -> list[dict[str, Any]]:
    saved_rows = output / "p2_detections.json"
    if saved_rows.is_file():
        document = json.loads(saved_rows.read_text(encoding="utf-8"))
        if (document.get("frame_id") == observation.packet.frame_id
                and int(document.get("frame_index", -1)) == observation.packet.frame_index):
            asset_masks = output / "assets/detection_2d/masks"
            asset_masks.mkdir(parents=True, exist_ok=True)
            remapped = []
            for row in document.get("detections", []):
                cached_mask = Path(row["mask_file"])
                if not cached_mask.is_file() or sha256_file(cached_mask) != row.get("mask_sha256"):
                    raise ValueError("cached P2 artifact failed its hash check")
                target_mask = asset_masks / cached_mask.name
                if cached_mask != target_mask.resolve():
                    target_mask.write_bytes(cached_mask.read_bytes())
                remapped.append({**row, "mask_file": str(target_mask.resolve())})
            return remapped
    p2_root = output / "p2_worker"
    client = P2WorkerClient(
        python=paths["occ_python"], worker_script=paths["p2_worker"],
        legacy_root=paths["conceptgraphs_root"], weights=paths["p2_weights"],
        profile=paths["p2_profile"], names=paths["p2_names"], output_root=p2_root,
        stderr_path=output / "logs/p2.stderr.log", startup_timeout_s=90, frame_timeout_s=60,
    )
    try:
        result = client.update_frame(
            frame_index=observation.packet.frame_index,
            frame_id=observation.packet.frame_id,
            timestamp_s=observation.packet.simulator_time_s,
            rgb_file=observation.packet.rgb_ref,
        )
    finally:
        client.close(timeout_s=10)

    asset_masks = output / "assets/detection_2d/masks"
    asset_masks.mkdir(parents=True, exist_ok=True)
    rows = []
    for ordinal, raw in enumerate(result.get("detections", [])):
        source_mask = Path(raw["mask_file"]).resolve(strict=True)
        target_mask = asset_masks / f"{observation.packet.frame_index:08d}_{ordinal:04d}.npy"
        target_mask.write_bytes(source_mask.read_bytes())
        rows.append({
            "detection_id": str(raw["detection_id"]),
            "class_id": int(raw["class_id"]), "class_raw": str(raw["class_raw"]),
            "class_canonical": str(raw["class_canonical"]),
            "score": float(raw["score"]), "score_raw": float(raw["score"]),
            "bbox_xyxy": [float(x) for x in raw["bbox_xyxy"]],
            "mask_file": str(target_mask.resolve()),
            "mask_sha256": sha256_file(target_mask),
            "mask_pixels": int(raw["mask_pixels"]),
        })
    atomic_json(output / "p2_detections.json", {"frame_id": observation.packet.frame_id,
                "frame_index": observation.packet.frame_index, "detections": rows,
                "provenance": client.provenance})
    return rows


def run_perception_child(args: argparse.Namespace) -> int:
    """Run one frame using the same session engine exposed by the live worker."""
    from scripts.native_perception_worker import _PerceptionRuntime

    runtime = _PerceptionRuntime(argparse.Namespace(
        conceptgraphs_root=args.conceptgraphs_root,
        occusg_root=args.occusg_root,
        output=args.output,
        p3_profile=args.p3_profile,
        feature_contract=args.feature_contract,
        checkpoint=args.checkpoint,
    ))
    try:
        runtime.process(args.packet_json)
    finally:
        runtime.close(finalize=True)
    return 0


def read_json_line(process: subprocess.Popen[str], timeout_s: float = 600.0) -> dict[str, Any]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            raise TimeoutError(f"mapper worker response timed out after {timeout_s:.0f}s")
        line = process.stdout.readline()
        if not line:
            try:
                return_code = process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                return_code = process.poll()
            raise RuntimeError(f"mapper worker exited early with code {return_code}")
        # Native dependencies occasionally write an empty stdout line during
        # flush. Ignore it while preserving strict JSON for protocol messages.
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if line.lstrip().startswith(("{", "[")):
                raise
            noise = getattr(process, "_3dsg_stdout_noise", None)
            if noise is not None:
                noise.write(line)
                noise.flush()
            continue
        if not isinstance(value, dict):
            raise RuntimeError("mapper worker emitted a non-object response")
        return value
    raise TimeoutError(f"mapper worker response timed out after {timeout_s:.0f}s")


def launch_mapper(kind: str, *, python: Path, worker: Path, output: Path,
                  packet_root: Path, source: Path, run_id: str,
                  bootstrap_id: str, route_hash: str, stderr_path: Path,
                  occusg_root: Path, nice_value: int | None = None) -> subprocess.Popen[str]:
    ready_file = output / "ready" / f"{kind}.json"
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.unlink(missing_ok=True)
    conceptgraphs_root = worker.parents[1]
    launched_worker = (ROOT / "scripts/native_occusg_worker.py") if kind == "occusg" else worker
    command = [str(python), str(launched_worker), "--worker", kind, "--run-root", str(output),
               "--packet-root", str(packet_root), "--source", str(source), "--live",
               "--ready-file", str(ready_file), "--run-id", run_id,
               "--bootstrap-id", bootstrap_id, "--route-config-sha256", route_hash,
               "--seed", "0"]
    env = os.environ.copy()
    env["THREEDSG_CONCEPTGRAPHS_ROOT"] = str(conceptgraphs_root)
    env["THREEDSG_PROJECT_ROOT"] = str(ROOT)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(conceptgraphs_root),
                                           str(conceptgraphs_root / "src"),
                                           str(conceptgraphs_root / "ref_ConceptGraph/concept-graphs"),
                                           str(occusg_root / "src"), env.get("PYTHONPATH", "")))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLCONFIGDIR"] = str(output / "cache/matplotlib")
    log = stderr_path.open("ab", buffering=0)
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=log, text=True, encoding="utf-8", bufsize=1, env=env)
    try:
        apply_process_nice(process, nice_value)
    except BaseException:
        log.close()
        raise
    process._3dsg_stdout_noise = (output / "logs" / f"{kind}.worker.stdout-noise.log").open("a", encoding="utf-8")  # type: ignore[attr-defined]
    deadline = time.monotonic() + 180.0
    while not ready_file.is_file():
        if process.poll() is not None:
            raise RuntimeError(f"{kind} failed before ready; inspect {stderr_path}")
        if time.monotonic() >= deadline:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
            log.close()
            process._3dsg_stdout_noise.close()  # type: ignore[attr-defined]
            raise TimeoutError(f"{kind} readiness timed out; inspect {stderr_path}")
        time.sleep(0.1)
    ready = json.loads(ready_file.read_text(encoding="utf-8"))
    if ready.get("run_id") != run_id or ready.get("bootstrap_id") != bootstrap_id:
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
        log.close()
        process._3dsg_stdout_noise.close()  # type: ignore[attr-defined]
        raise RuntimeError(f"{kind} readiness ownership mismatch: {ready}")
    # Keep the file handle alive for the life of the process.
    process._3dsg_log = log  # type: ignore[attr-defined]
    return process


def dispatch_and_finalize(process: subprocess.Popen[str], packet_path: Path,
                          kind: str) -> dict[str, Any]:
    assert process.stdin is not None
    payload = {"op": "frame", "packet_path": str(packet_path),
               "packet_sha256": sha256_file(packet_path)}
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()
    update = read_json_line(process)
    if update.get("status") != "frame_published" or update.get("kind") != kind:
        raise RuntimeError(f"{kind} did not publish the requested frame: {update}")
    process.stdin.write('{"op":"finalize"}\n')
    process.stdin.flush()
    final = read_json_line(process)
    if final.get("status") != "finalized" or final.get("kind") != kind:
        raise RuntimeError(f"{kind} did not finalize cleanly: {final}")
    process.stdin.close()
    code = process.wait(timeout=120.0)
    if code != 0:
        raise RuntimeError(f"{kind} exited with code {code}")
    process._3dsg_log.close()  # type: ignore[attr-defined]
    process._3dsg_stdout_noise.close()  # type: ignore[attr-defined]
    return {"update": update, "terminal": final, "exit_code": code}


def _paired_ack_coordinator(output: Path, *, frame_id: str, frame_index: int,
                            source_hashes: dict[str, str], timeout_s: float = 600.0) -> dict[str, Any]:
    """Acknowledge only after both native branches publish the same source frame."""
    branch_paths = {name: output / "branches" / name / "events.jsonl"
                    for name in ("objects", "rooms")}
    deadline = time.monotonic() + timeout_s
    events: dict[str, dict[str, Any]] = {}
    stop_path = output / "control/stop.json"
    while time.monotonic() < deadline:
        if stop_path.is_file():
            raise InterruptedError("native replay stop requested while waiting for paired barrier")
        for branch, path in branch_paths.items():
            if branch in events or not path.is_file():
                continue
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            matching = [row for row in rows if row.get("source_frame_id") == frame_id]
            if matching:
                if len(matching) != 1:
                    raise ValueError(f"{branch} published duplicate events for {frame_id}")
                event = matching[0]
                if (int(event.get("source_frame_index", -1)) != frame_index
                        or event.get("source_hashes") != source_hashes):
                    raise ValueError(f"{branch} event identity/source hashes differ from the paired packet")
                state = output / str(event["state_path"])
                if not state.is_file() or sha256_file(state) != event.get("state_sha256"):
                    raise ValueError(f"{branch} published state is missing or hash-invalid")
                for relative, digest in event.get("asset_sha256", {}).items():
                    artifact = output / relative
                    if not artifact.is_file() or sha256_file(artifact) != digest:
                        raise ValueError(f"{branch} referenced artifact is missing or hash-invalid: {relative}")
                events[branch] = event
        if len(events) == 2:
            objects, rooms = events["objects"], events["rooms"]
            for field in ("source_frame_id", "source_frame_index", "source_hashes",
                          "source_timestamp_s", "source_capture_wall_time_unix_s",
                          "source_capture_monotonic_s", "source_capture_monotonic_domain"):
                if objects.get(field) != rooms.get(field):
                    raise ValueError(f"native branches disagree on paired provenance field {field}")
            pair_path = output / "paired_commits.jsonl"
            commit = {"schema": "3dsg-native-paired-replay-ack/v1", "joined_revision": 1,
                      "frame_id": frame_id, "frame_index": frame_index,
                      "source_hashes": source_hashes,
                      "branch_event_sha256": {name: events[name]["event_sha256"] for name in events},
                      "durable_state_paths": {name: events[name]["state_path"] for name in events},
                      "acknowledged_utc": time.time(),
                      "graph_join_performed": False}
            with pair_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(commit, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            for branch, event in (("objects", objects), ("rooms", rooms)):
                atomic_json(output / "branches" / branch / "acks" / f"{int(event['event_seq']):06d}.json",
                    {"schema": "t2-d-branch-ack/v1", "event_sha256": event["event_sha256"],
                     "joined_revision": 1, "durable": True})
            return commit
        time.sleep(0.05)
    raise TimeoutError("both mapper branches did not publish the current frame before barrier timeout")


def _read_paired_events_and_ack(output: Path, *, frame_id: str, frame_index: int,
                                source_hashes: dict[str, str], joined_revision: int,
                                scene_id: str | None = None, sequence_id: str | None = None,
                                timeout_s: float = 600.0
                                ) -> dict[str, Any]:
    """Persist a monotonically versioned pair barrier for a live frame."""
    branch_paths = {name: output / "branches" / name / "events.jsonl"
                    for name in ("objects", "rooms")}
    deadline = time.monotonic() + timeout_s
    events: dict[str, dict[str, Any]] = {}
    stop_path = output / "control/stop.json"
    while time.monotonic() < deadline:
        if stop_path.is_file():
            raise InterruptedError("native graph stop requested while waiting for paired barrier")
        for branch, path in branch_paths.items():
            if branch in events or not path.is_file():
                continue
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
            matches = [row for row in rows if row.get("source_frame_id") == frame_id]
            if len(matches) > 1:
                raise ValueError(f"{branch} published duplicate events for {frame_id}")
            if not matches:
                continue
            event = matches[0]
            if (int(event.get("source_frame_index", -1)) != frame_index
                    or event.get("source_hashes") != source_hashes
                    or scene_id is not None and event.get("scene_id") != scene_id
                    or sequence_id is not None and event.get("sequence_id") != sequence_id):
                raise ValueError(f"{branch} event identity/source hashes differ from paired packet")
            state = (output / str(event["state_path"])).resolve(strict=False)
            if not state.is_relative_to(output.resolve()):
                raise ValueError(f"{branch} state path escaped native run directory")
            if not state.is_file() or sha256_file(state) != event.get("state_sha256"):
                raise ValueError(f"{branch} published state is missing or hash-invalid")
            for relative, digest in event.get("asset_sha256", {}).items():
                artifact = (output / relative).resolve(strict=False)
                if not artifact.is_relative_to(output.resolve()):
                    raise ValueError(f"{branch} asset path escaped native run directory")
                if not artifact.is_file() or sha256_file(artifact) != digest:
                    raise ValueError(f"{branch} published artifact is missing/hash-invalid: {relative}")
            events[branch] = event
        if len(events) == 2:
            objects, rooms = events["objects"], events["rooms"]
            fields = ("source_frame_id", "source_frame_index", "source_hashes",
                      "scene_id", "sequence_id",
                      "source_timestamp_s", "source_capture_wall_time_unix_s",
                      "source_capture_monotonic_s", "source_capture_monotonic_domain")
            for field in fields:
                if objects.get(field) != rooms.get(field):
                    raise ValueError(f"native branches disagree on paired provenance field {field}")
            commit = {"schema": "3dsg-native-paired-replay-ack/v1",
                      "joined_revision": int(joined_revision),
                      "frame_id": frame_id, "frame_index": int(frame_index),
                      "source_hashes": source_hashes,
                      "branch_event_sha256": {
                          name: events[name]["event_sha256"] for name in events},
                      "durable_state_paths": {
                          name: events[name]["state_path"] for name in events},
                      "acknowledged_utc": time.time(), "graph_join_performed": False}
            pair_path = output / "paired_commits.jsonl"
            pair_path.parent.mkdir(parents=True, exist_ok=True)
            with pair_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(commit, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            for branch, event in events.items():
                atomic_json(output / "branches" / branch / "acks" /
                            f"{int(event['event_seq']):06d}.json",
                            {"schema": "t2-d-branch-ack/v1",
                             "event_sha256": event["event_sha256"],
                             "joined_revision": int(joined_revision), "durable": True})
            return {"commit": commit, "events": events}
        time.sleep(0.05)
    raise TimeoutError(f"native mapper pair did not publish frame {frame_id} before timeout")


def send_frame(process: subprocess.Popen[str], packet_path: Path) -> None:
    assert process.stdin is not None
    request = {"op": "frame", "packet_path": str(packet_path),
               "packet_sha256": sha256_file(packet_path)}
    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    process.stdin.flush()


def receive_frame_and_finalize(process: subprocess.Popen[str], kind: str) -> dict[str, Any]:
    assert process.stdin is not None
    update = read_json_line(process)
    if update.get("status") != "frame_published" or update.get("kind") != kind:
        raise RuntimeError(f"{kind} did not publish the requested frame: {update}")
    process.stdin.write('{"op":"finalize"}\n')
    process.stdin.flush()
    final = read_json_line(process)
    if final.get("status") != "finalized" or final.get("kind") != kind:
        raise RuntimeError(f"{kind} did not finalize cleanly: {final}")
    process.stdin.close()
    code = process.wait(timeout=120.0)
    if code != 0:
        raise RuntimeError(f"{kind} exited with code {code}")
    process._3dsg_log.close()  # type: ignore[attr-defined]
    process._3dsg_stdout_noise.close()  # type: ignore[attr-defined]
    return {"update": update, "terminal": final, "exit_code": code}


def cancel_workers(output: Path, workers: list[tuple[str, subprocess.Popen[str]]],
                   reason: str, *, timeout_s: float = 30.0,
                   terminate_grace_s: float = 3.0) -> dict[str, Any]:
    if timeout_s <= 0 or terminate_grace_s < 0:
        raise ValueError("cancel timeout must be positive and terminate grace non-negative")
    started = time.monotonic()
    deadline = started + timeout_s
    phase_grace = min(terminate_grace_s, timeout_s / 3.0)
    graceful_deadline = max(started, deadline - 2.0 * phase_grace)
    terminate_deadline = max(graceful_deadline, deadline - phase_grace)
    control = output / "control/stop.json"
    atomic_json(control, {"schema": "3dsg-native-replay-stop/v1", "reason": reason,
                          "requested_unix_s": time.time()})
    for _kind, process in workers:
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write('{"op":"close"}\n')
                process.stdin.flush()
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
    for _kind, process in workers:
        if process.poll() is None and time.monotonic() < graceful_deadline:
            try:
                process.wait(timeout=max(0.0, graceful_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
    terminated: list[str] = []
    for kind, process in workers:
        if process.poll() is None:
            terminated.append(kind)
            try:
                process.terminate()
            except ProcessLookupError:
                pass
    for _kind, process in workers:
        if process.poll() is None and time.monotonic() < terminate_deadline:
            try:
                process.wait(timeout=max(0.0, terminate_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
    killed: list[str] = []
    for kind, process in workers:
        if process.poll() is None:
            killed.append(kind)
            try:
                process.kill()
            except ProcessLookupError:
                pass
    for _kind, process in workers:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for stream_name in ("_3dsg_log", "_3dsg_stdout_noise"):
            stream = getattr(process, stream_name, None)
            if stream is not None and not stream.closed:
                stream.close()
    result = {
        "schema": "3dsg-native-worker-cancel-report/v1",
        "reason": reason,
        "timeout_s": timeout_s,
        "elapsed_s": time.monotonic() - started,
        "terminated_workers": terminated,
        "killed_workers": killed,
        "workers": {kind: {"returncode": process.poll(), "alive": process.poll() is None}
                    for kind, process in workers},
    }
    atomic_json(output / "control/cancel_report.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_3DSG_RUN)
    parser.add_argument("--frame-index", type=int, default=652)
    parser.add_argument("--conceptgraphs-root", type=Path, default=DEFAULT_CG)
    parser.add_argument("--occusg-root", type=Path, default=DEFAULT_OCC)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=ROOT / "runs/native-keyframe-replay-frame652")
    parser.add_argument("--prepared-dir", type=Path,
                        help="reuse a previously hash-checked perception packet and skip P2/P3/OpenCLIP")
    parser.add_argument("--perception-child", action="store_true")
    parser.add_argument("--packet-json", type=Path)
    parser.add_argument("--p3-profile", type=Path)
    parser.add_argument("--feature-contract", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--occusg-backprojection-stride", type=int, choices=(1, 2, 4), default=1,
                        help="OccuSG primary and auxiliary depth sampling stride; default 1 is full resolution")
    parser.add_argument("--occusg-primary-stride", type=int, choices=(1, 2, 4), default=None)
    parser.add_argument("--occusg-auxiliary-stride", type=int, choices=(1, 2, 4), default=None)
    args = parser.parse_args()
    os.environ["THREEDSG_OCCUSG_BACKPROJECTION_STRIDE"] = str(args.occusg_backprojection_stride)
    primary_stride = (args.occusg_backprojection_stride if args.occusg_primary_stride is None
                      else args.occusg_primary_stride)
    auxiliary_stride = (args.occusg_backprojection_stride if args.occusg_auxiliary_stride is None
                        else args.occusg_auxiliary_stride)
    os.environ["THREEDSG_OCCUSG_PRIMARY_BACKPROJECTION_STRIDE"] = str(primary_stride)
    os.environ["THREEDSG_OCCUSG_AUXILIARY_BACKPROJECTION_STRIDE"] = str(auxiliary_stride)
    args.output = args.output.resolve()

    if args.perception_child:
        if not all((args.packet_json, args.p3_profile, args.feature_contract, args.checkpoint)):
            parser.error("perception child requires packet, profile, feature contract and checkpoint")
        return run_perception_child(args)

    args.run_root = args.run_root.resolve(strict=True)
    args.conceptgraphs_root = args.conceptgraphs_root.resolve(strict=True)
    args.occusg_root = args.occusg_root.resolve(strict=True)
    args.source = args.source.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "logs").mkdir(parents=True, exist_ok=True)
    paths = load_source_paths(args)
    paths["conceptgraphs_root"] = args.conceptgraphs_root
    paths["occusg_root"] = args.occusg_root
    observation = load_observation(args.run_root, args.frame_index)

    packet_root = args.output / "packets"
    packet_root.mkdir(parents=True, exist_ok=True)
    if args.prepared_dir is not None:
        prepared = args.prepared_dir.resolve(strict=True)
        source_packet = prepared / "packets" / f"packet-{observation.packet.frame_index:06d}.json"
        packet = json.loads(source_packet.read_text(encoding="utf-8"))
        if (packet.get("frame_id") != observation.packet.frame_id
                or int(packet.get("frame_index", -1)) != observation.packet.frame_index):
            raise ValueError("prepared packet does not match selected durable keyframe")
        p2_doc = json.loads((prepared / "p2_detections.json").read_text(encoding="utf-8"))
        prepared_perception = prepared / "native_perception" / f"frame_{observation.packet.frame_index:08d}"
        p3_path = prepared_perception / "p3_detections.json"
        feature_path = prepared_perception / "feature_artifacts.json"
        if not p3_path.is_file():
            p3_path = prepared / "p3_detections.json"
        if not feature_path.is_file():
            feature_path = prepared / "feature_artifacts.json"
        p3_doc = json.loads(p3_path.read_text(encoding="utf-8"))
        feature_doc = json.loads(feature_path.read_text(encoding="utf-8"))
        capture_record = packet["capture_record"]
        p2_rows = p2_doc["detections"]
        packet_path = packet_root / source_packet.name
        atomic_json(packet_path, packet)
    else:
        manifest = json.loads((args.source / "c2_run_manifest.json").read_text(encoding="utf-8"))
        scene_file = paths["scene_file"]
        if scene_file is None:
            capture_manifest = json.loads((args.source / "capture/capture_manifest.json").read_text(encoding="utf-8"))
            first = capture_manifest["frames"][0]
            frame_meta = args.source / "capture" / first["frame_dir"] / "metadata.json"
            scene_file = Path(json.loads(frame_meta.read_text(encoding="utf-8"))["scene_file"])
        if not scene_file.is_absolute():
            scene_file = args.source / scene_file
        if not scene_file.exists():
            raise FileNotFoundError(f"source scene file is missing: {scene_file}")
        capture_store = LegacyCaptureStore(args.output / "capture", scene_file=scene_file)
        capture_record = capture_store.export(observation)
        p2_rows = run_p2(observation, paths, args.output)
        packet_seed = {
            "frame_id": observation.packet.frame_id,
            "frame_index": observation.packet.frame_index,
            "timestamp_s": float(observation.packet.simulator_time_s),
            "capture_record": capture_record,
            "capture_root": str((args.output / "capture").resolve()),
            "asset_root": str((args.output / "assets").resolve()),
            "source_hashes": dict(capture_record["raw_files_sha256"]),
            "p2_detections": p2_rows,
        }
        atomic_json(args.output / "perception_input.json", packet_seed)
        paths["p3_profile"].resolve(strict=True)
        paths["feature_contract"].resolve(strict=True)
        paths["checkpoint"].resolve(strict=True)
        perception_env = os.environ.copy()
        perception_env["PYTHONPATH"] = os.pathsep.join((str(args.conceptgraphs_root),
            str(args.conceptgraphs_root / "src"), str(args.conceptgraphs_root / "ref_ConceptGraph/concept-graphs"),
            str(args.occusg_root / "src"), str(ROOT), perception_env.get("PYTHONPATH", "")))
        child_command = [str(paths["appearance_python"]), str(Path(__file__).resolve()),
            "--perception-child", "--run-root", str(args.run_root),
            "--conceptgraphs-root", str(args.conceptgraphs_root), "--occusg-root", str(args.occusg_root),
            "--output", str(args.output), "--packet-json", str(args.output / "perception_input.json"),
            "--p3-profile", str(paths["p3_profile"]), "--feature-contract", str(paths["feature_contract"]),
            "--checkpoint", str(paths["checkpoint"])]
        child = subprocess.run(child_command, env=perception_env, text=True, capture_output=True,
                               timeout=900, check=False)
        (args.output / "logs").mkdir(exist_ok=True)
        (args.output / "logs/perception.stdout.log").write_text(child.stdout, encoding="utf-8")
        (args.output / "logs/perception.stderr.log").write_text(child.stderr, encoding="utf-8")
        if child.returncode != 0:
            raise RuntimeError(f"P3/OpenCLIP subprocess failed with {child.returncode}; see {args.output / 'logs'}")

        perception_frame_root = args.output / "native_perception" / f"frame_{observation.packet.frame_index:08d}"
        p3_doc = json.loads((perception_frame_root / "p3_detections.json").read_text(encoding="utf-8"))
        feature_doc = json.loads((perception_frame_root / "feature_artifacts.json").read_text(encoding="utf-8"))
        packet = build_current_mapper_packet(
            observation=observation, capture_root=args.output / "capture", capture_record=capture_record,
            asset_root=args.output / "assets", p2_detections=p2_rows,
            p3_detections=p3_doc["detections"], features=feature_doc["features"],
        )
        packet_path = packet_root / f"packet-{observation.packet.frame_index:06d}.json"
        atomic_json(packet_path, packet)

    worker = paths["worker_script"].resolve(strict=True)
    route_hash = sha256_file(paths["route_config"].resolve(strict=True))
    preflight_rows = []
    for kind, python in (("conceptgraphs", paths["cg_python"]), ("occusg", paths["occ_python"])):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((str(args.conceptgraphs_root),
            str(args.conceptgraphs_root / "src"), str(args.conceptgraphs_root / "ref_ConceptGraph/concept-graphs"),
            str(args.occusg_root / "src"), env.get("PYTHONPATH", "")))
        result = subprocess.run([str(python), str(worker), "--worker", kind,
            "--run-root", str(args.output), "--packet-root", str(packet_root),
            "--source", str(args.source), "--preflight"],
            env=env, text=True, capture_output=True, timeout=180, check=False)
        (args.output / "logs" / f"{kind}.preflight.stdout.log").write_text(result.stdout, encoding="utf-8")
        (args.output / "logs" / f"{kind}.preflight.stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"{kind} packet preflight failed; see {args.output / 'logs'}")
        preflight_rows.append(json.loads(result.stdout.strip().splitlines()[-1]))

    run_id = f"native-replay-{observation.packet.frame_id}"
    bootstrap_id = f"bootstrap-{sha256_file(packet_path)[:16]}"
    workers = []
    mapper_results = {}
    ack_result: dict[str, Any] = {}
    ack_error: list[BaseException] = []
    ack_thread: threading.Thread | None = None
    def acknowledge_pair() -> None:
        try:
            ack_result.update(_paired_ack_coordinator(args.output,
                frame_id=observation.packet.frame_id, frame_index=observation.packet.frame_index,
                source_hashes=packet["source_hashes"]))
        except BaseException as exc:
            ack_error.append(exc)
    try:
        for kind, python in (("conceptgraphs", paths["cg_python"]), ("occusg", paths["occ_python"])):
            workers.append((kind, launch_mapper(kind, python=python, worker=worker, output=args.output,
                packet_root=packet_root, source=args.source, run_id=run_id, bootstrap_id=bootstrap_id,
                route_hash=route_hash, stderr_path=args.output / "logs" / f"{kind}.worker.stderr.log",
                occusg_root=args.occusg_root)))
        for _kind, process in workers:
            send_frame(process, packet_path)
        ack_thread = threading.Thread(target=acknowledge_pair, name="3dsg-native-pair-ack", daemon=True)
        ack_thread.start()
        # Both stateful sessions receive the exact same immutable packet bytes;
        # the barrier acknowledges only after both states validate as a pair.
        for kind, process in workers:
            mapper_results[kind] = receive_frame_and_finalize(process, kind)
        ack_thread.join(timeout=10)
        if ack_error:
            raise ack_error[0]
        if ack_thread.is_alive() or not ack_result:
            raise TimeoutError("paired mapper acknowledgement did not complete")
    except BaseException as exc:
        cancel_workers(args.output, workers, f"replay_failed:{type(exc).__name__}")
        if ack_thread is not None:
            ack_thread.join(timeout=3.0)
        raise

    aux_adapter_path = (args.output / "diagnostics" / "occusg_multisensor"
                        / f"frame_{observation.packet.frame_index:08d}.json")
    aux_adapter_audit = (json.loads(aux_adapter_path.read_text(encoding="utf-8"))
                         if aux_adapter_path.is_file() else None)
    summary = {
        "schema": "3dsg-native-single-keyframe-replay/v1", "status": "pass",
        "occusg_backprojection_stride": args.occusg_backprojection_stride,
        "occusg_primary_stride": primary_stride,
        "occusg_auxiliary_stride": auxiliary_stride,
        "frame_id": observation.packet.frame_id, "frame_index": observation.packet.frame_index,
        "scene_id": observation.packet.scene_id, "run_id": observation.packet.run_id,
        "packet_sha256": sha256_file(packet_path), "capture_hashes": capture_record["raw_files_sha256"],
        "p2_count": len(p2_rows), "p3_accepted": sum(x["status"] == "accepted" for x in p3_doc["detections"]),
        "p3_rejected": sum(x["status"] != "accepted" for x in p3_doc["detections"]),
        "openclip_count": len(feature_doc["features"]), "native_preflight": preflight_rows,
        "mapper_results": mapper_results,
        "paired_commit": ack_result,
        "branch_events": {
            name: sum(1 for line in (args.output / "branches" / branch / "events.jsonl").read_text().splitlines() if line.strip())
            if (args.output / "branches" / branch / "events.jsonl").is_file() else 0
            for name, branch in (("conceptgraphs", "objects"), ("occusg", "rooms"))
        },
        "aux_depth_present": observation.packet.nav_depth_m is not None,
        "occusg_aux_depth_integrated": bool(aux_adapter_audit and aux_adapter_audit.get("mapping_record", {}).get("auxiliary_sensor_integrated")),
        "occusg_aux_depth_integration": aux_adapter_audit,
        "note": "OccuSG integrates the independently calibrated auxiliary depth as a separate M2 ray source; ConceptGraphs uses the primary RGB-D packet.",
    }
    atomic_json(args.output / "summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
