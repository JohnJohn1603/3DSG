#!/usr/bin/env python3
"""Replay saved native mapper packets as an ordered, persistent sequence.

This tool is for controlled OccuSG mapping ablations. It reuses immutable
packets from a completed live run and runs both native mapper sessions once
over the requested frames, preserving the original frame identities.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.replay_saved_native_keyframe import (
    ROOT,
    cancel_workers,
    launch_mapper,
    load_source_paths,
    read_json_line,
    send_frame,
    sha256_file,
    _read_paired_events_and_ack,
)


def _drop_adapter_stride_provenance(snapshot: dict[str, Any]) -> None:
    """Ignore one adapter-only config field when comparing semantic graph state."""
    for node in snapshot.get("nodes", ()):
        attributes = node.get("attributes", {})
        metadata = attributes.get("source_metadata", {})
        auxiliary = metadata.get("auxiliary_depth", {})
        if isinstance(auxiliary, dict):
            auxiliary.pop("backprojection_stride", None)


def _canonical_state_hash(snapshot: dict[str, Any]) -> str:
    import hashlib

    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _read_frame_artifact(packet_path: Path) -> dict[str, Any]:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    required = {"frame_id", "frame_index", "scene_id", "sequence_id", "source_hashes"}
    if not required.issubset(packet):
        raise ValueError(f"native packet is missing required fields: {sorted(required - packet.keys())}")
    return packet


def _finish_worker(process, kind: str) -> dict[str, Any]:
    if process.stdin is None:
        raise RuntimeError(f"{kind} worker stdin is closed before finalize")
    process.stdin.write('{"op":"finalize"}\n')
    process.stdin.flush()
    final = read_json_line(process)
    if final.get("status") != "finalized" or final.get("kind") != kind:
        raise RuntimeError(f"{kind} worker did not finalize cleanly: {final}")
    process.stdin.close()
    code = process.wait(timeout=120.0)
    if code != 0:
        raise RuntimeError(f"{kind} worker exited with code {code}")
    for name in ("_3dsg_log", "_3dsg_stdout_noise"):
        stream = getattr(process, name, None)
        if stream is not None and not stream.closed:
            stream.close()
    return final


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_run = args.source_run.resolve(strict=True)
    source_graph = source_run / "native_graph"
    if not source_graph.is_dir():
        raise FileNotFoundError(f"source live run has no native_graph directory: {source_graph}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for folder in ("logs", "ready", "packets"):
        (output / folder).mkdir(parents=True, exist_ok=True)

    packet_rows = []
    for frame_index in args.frame_indices:
        packet_path = source_graph / "packets" / f"packet-{frame_index:06d}.json"
        packet = _read_frame_artifact(packet_path.resolve(strict=True))
        if int(packet["frame_index"]) != frame_index:
            raise ValueError(f"packet frame index mismatch for requested frame {frame_index}")
        packet_rows.append((packet_path, packet))
    if not packet_rows or any(b[1]["frame_index"] <= a[1]["frame_index"]
                              for a, b in zip(packet_rows, packet_rows[1:])):
        raise ValueError("frame indices must be a non-empty, strictly increasing sequence")
    identities = {(row[1]["scene_id"], row[1]["sequence_id"]) for row in packet_rows}
    if len(identities) != 1:
        raise ValueError("all replay packets must belong to the same scene and sequence")

    args.conceptgraphs_root = args.conceptgraphs_root.resolve(strict=True)
    args.occusg_root = args.occusg_root.resolve(strict=True)
    args.source = args.source.resolve(strict=True)
    paths = load_source_paths(args)
    for key in ("cg_python", "occ_python", "worker_script", "route_config"):
        paths[key].resolve(strict=True)
    route_hash = sha256_file(paths["route_config"])
    run_id = str(packet_rows[0][1]["sequence_id"])
    bootstrap_id = f"bootstrap-{sha256_file(packet_rows[0][0])[:16]}"
    os.environ["THREEDSG_OCCUSG_BACKPROJECTION_STRIDE"] = str(args.stride)
    primary_stride = args.primary_stride if args.primary_stride is not None else args.stride
    auxiliary_stride = args.auxiliary_stride if args.auxiliary_stride is not None else args.stride
    os.environ["THREEDSG_OCCUSG_PRIMARY_BACKPROJECTION_STRIDE"] = str(primary_stride)
    os.environ["THREEDSG_OCCUSG_AUXILIARY_BACKPROJECTION_STRIDE"] = str(auxiliary_stride)
    os.environ["THREEDSG_OCCUSG_SCANPOINT_CONSTRUCTION"] = args.scanpoint_construction

    workers = []
    updates = []
    try:
        for kind, python in (("conceptgraphs", paths["cg_python"]),
                             ("occusg", paths["occ_python"])):
            workers.append((kind, launch_mapper(
                kind, python=python, worker=paths["worker_script"], output=output,
                packet_root=output / "packets", source=args.source, run_id=run_id,
                bootstrap_id=bootstrap_id, route_hash=route_hash,
                stderr_path=output / "logs" / f"{kind}.worker.stderr.log",
                occusg_root=args.occusg_root)))

        for packet_path, packet in packet_rows:
            started = time.perf_counter()
            for _kind, process in workers:
                send_frame(process, packet_path)
            ack_result: dict[str, Any] = {}
            ack_errors: list[BaseException] = []

            def acknowledge_pair() -> None:
                try:
                    ack_result.update(_read_paired_events_and_ack(
                        output, frame_id=packet["frame_id"],
                        frame_index=int(packet["frame_index"]),
                        source_hashes=packet["source_hashes"],
                        joined_revision=len(updates) + 1,
                        scene_id=packet["scene_id"], sequence_id=packet["sequence_id"]))
                except BaseException as error:
                    ack_errors.append(error)

            ack_thread = threading.Thread(target=acknowledge_pair,
                                          name="native-sequence-pair-ack", daemon=True)
            ack_thread.start()
            responses = {}
            for kind, process in workers:
                response = read_json_line(process)
                if (response.get("status") != "frame_published"
                        or response.get("kind") != kind
                        or int(response.get("frame_index", -1)) != packet["frame_index"]):
                    raise RuntimeError(f"{kind} returned a mismatched frame response: {response}")
                responses[kind] = response
            ack_thread.join(timeout=610.0)
            if ack_errors:
                raise ack_errors[0]
            if ack_thread.is_alive() or not ack_result:
                raise TimeoutError(f"paired acknowledgement did not finish for frame {packet['frame_index']}")
            updates.append({
                "frame_id": packet["frame_id"], "frame_index": packet["frame_index"],
                "packet_sha256": sha256_file(packet_path),
                "wall_s": time.perf_counter() - started,
                "mapper_responses": responses,
                "paired_commit": ack_result["commit"],
            })
            print(json.dumps({"event": "sequence_frame_complete",
                              "frame_index": packet["frame_index"],
                              "wall_s": updates[-1]["wall_s"]}, sort_keys=True), flush=True)

        terminals = {kind: _finish_worker(process, kind) for kind, process in workers}
    except BaseException as error:
        try:
            cancel_workers(output, workers, f"sequence_replay_failed:{type(error).__name__}")
        except BaseException:
            pass
        raise

    room_events = [json.loads(line) for line in
                   (output / "branches/rooms/events.jsonl").read_text().splitlines() if line.strip()]
    object_events = [json.loads(line) for line in
                     (output / "branches/objects/events.jsonl").read_text().splitlines() if line.strip()]
    mapping_records = []
    diagnostics = output / "diagnostics/occusg_multisensor"
    if diagnostics.is_dir():
        mapping_records = [json.loads(path.read_text())["mapping_record"]
                           for path in sorted(diagnostics.glob("frame_*.json"))]
    room_processing = [float(event["processing_ms"]) for event in room_events]
    object_processing = [float(event["processing_ms"]) for event in object_events]
    last_state = json.loads((output / room_events[-1]["state_path"]).read_text())["snapshot"]
    terminal_frame = int(packet_rows[-1][1]["frame_index"])
    branch_state_parity = {}
    for branch in ("rooms", "objects"):
        source_events_path = source_graph / "branches" / branch / "events.jsonl"
        replay_events_path = output / "branches" / branch / "events.jsonl"
        source_events = [json.loads(line) for line in source_events_path.read_text().splitlines()
                         if line.strip()]
        replay_events = [json.loads(line) for line in replay_events_path.read_text().splitlines()
                         if line.strip()]
        source_event = next((event for event in reversed(source_events)
                             if int(event.get("source_frame_index", -1)) == terminal_frame), None)
        replay_event = next((event for event in reversed(replay_events)
                             if int(event.get("source_frame_index", -1)) == terminal_frame), None)
        source_sha = None if source_event is None else source_event.get("state_sha256")
        replay_sha = None if replay_event is None else replay_event.get("state_sha256")
        normalized_source_sha = normalized_replay_sha = None
        if source_event is not None and replay_event is not None:
            source_state = json.loads((source_graph / source_event["state_path"]).read_text())
            replay_state = json.loads((output / replay_event["state_path"]).read_text())
            source_snapshot = source_state.get("snapshot", source_state)
            replay_snapshot = replay_state.get("snapshot", replay_state)
            _drop_adapter_stride_provenance(source_snapshot)
            _drop_adapter_stride_provenance(replay_snapshot)
            normalized_source_sha = _canonical_state_hash(source_snapshot)
            normalized_replay_sha = _canonical_state_hash(replay_snapshot)
        branch_state_parity[branch] = {
            "frame_index": terminal_frame,
            "source_state_sha256": source_sha,
            "replay_state_sha256": replay_sha,
            "exact_match": source_sha is not None and source_sha == replay_sha,
            "normalized_source_state_sha256": normalized_source_sha,
            "normalized_replay_state_sha256": normalized_replay_sha,
            "normalized_match": (normalized_source_sha is not None
                                 and normalized_source_sha == normalized_replay_sha),
            "normalization_ignored_fields": [
                "nodes[].attributes.source_metadata.auxiliary_depth.backprojection_stride"],
        }
    counts = {}
    for kind in ("room", "object", "navigation"):
        counts[f"{kind}_nodes"] = sum(node.get("kind") == kind for node in last_state["nodes"])
    counts["room_contains_edges"] = sum(edge.get("relation") == "room_contains"
                                         for edge in last_state["edges"])
    summary = {
        "schema": "3dsg-native-keyframe-sequence-replay/v1",
        "status": "pass",
        "source_run": str(source_run),
        "scene_id": packet_rows[0][1]["scene_id"],
        "sequence_id": run_id,
        "frame_indices": [int(packet["frame_index"]) for _path, packet in packet_rows],
        "occusg_backprojection_stride": args.stride,
        "occusg_primary_stride": primary_stride,
        "occusg_auxiliary_stride": auxiliary_stride,
        "occusg_scanpoint_construction": args.scanpoint_construction,
        "processed_frames": len(updates),
        "paired_frame_and_source_hashes_validated": True,
        "all_graph_join_performed_flags_false": all(
            not row["paired_commit"].get("graph_join_performed") for row in updates),
        "source_graph_state_parity": branch_state_parity,
        "per_frame_updates": updates,
        "runtime_ms": {
            "rooms_median": statistics.median(room_processing),
            "rooms_max": max(room_processing),
            "objects_median": statistics.median(object_processing),
            "objects_max": max(object_processing),
            # Keep m2_median for older audit consumers; it measures the primary
            # camera path. The explicit totals below include the auxiliary sensor.
            "m2_median": statistics.median(float(row["m2_runtime_ms"])
                                           for row in mapping_records),
            "m1_m2_m3_median": statistics.median(float(row["m1_m2_m3_runtime_ms"])
                                                  for row in mapping_records),
            "primary_m2_median": statistics.median(float(row.get(
                "primary_m2_runtime_ms", row["m2_runtime_ms"])) for row in mapping_records),
            "auxiliary_m1_median": statistics.median(float(row.get(
                "auxiliary_m1_runtime_ms", 0.0)) for row in mapping_records),
            "auxiliary_point_conversion_median": statistics.median(float(row.get(
                "auxiliary_point_conversion_runtime_ms", 0.0)) for row in mapping_records),
            "auxiliary_m2_median": statistics.median(float(row.get(
                "auxiliary_m2_runtime_ms", 0.0)) for row in mapping_records),
            "auxiliary_m3_median": statistics.median(float(row.get(
                "auxiliary_m3_runtime_ms", 0.0)) for row in mapping_records),
            "m2_total_median": statistics.median(float(row.get(
                "m2_total_runtime_ms", row["m2_runtime_ms"])) for row in mapping_records),
            "m3_total_median": statistics.median(float(row.get(
                "m3_total_runtime_ms", row.get("m3_runtime_ms", 0.0))) for row in mapping_records),
            "all_sensor_m1_m2_m3_median": statistics.median(float(row.get(
                "all_sensor_m1_m2_m3_runtime_ms", row["m1_m2_m3_runtime_ms"]))
                for row in mapping_records),
            "primary_conversion_and_stage_overhead_median": statistics.median(float(row.get(
                "primary_conversion_and_stage_overhead_ms", 0.0)) for row in mapping_records),
            "primary_integrate_python_cpu_median": statistics.median(float(
                row.get("primary_m2_transport_profile", {}).get("integrate_python_cpu_ms", 0.0))
                for row in mapping_records),
            "primary_integrate_prepare_response_cpu_median": statistics.median(float(
                row.get("primary_m2_transport_profile", {}).get(
                    "python_prepare_response_cpu_ms", 0.0)) for row in mapping_records),
            "primary_integrate_write_wall_median": statistics.median(float(
                row.get("primary_m2_transport_profile", {}).get("write_wall_ms", 0.0))
                for row in mapping_records),
            "primary_integrate_write_python_cpu_median": statistics.median(float(
                row.get("primary_m2_transport_profile", {}).get("write_python_cpu_ms", 0.0))
                for row in mapping_records),
            "primary_integrate_read_wall_median": statistics.median(float(
                row.get("primary_m2_transport_profile", {}).get("read_wall_ms", 0.0))
                for row in mapping_records),
            "primary_integrate_read_python_cpu_median": statistics.median(float(
                row.get("primary_m2_transport_profile", {}).get("read_python_cpu_ms", 0.0))
                for row in mapping_records),
            "primary_native_child_cpu_median": statistics.median(float(
                row.get("primary_m2_transport_profile", {}).get("native_child_cpu_ms") or 0.0)
                for row in mapping_records),
            "auxiliary_integrate_python_cpu_median": statistics.median(float(
                row.get("auxiliary_m2_transport_profile", {}).get("integrate_python_cpu_ms", 0.0))
                for row in mapping_records),
            "auxiliary_integrate_read_wall_median": statistics.median(float(
                row.get("auxiliary_m2_transport_profile", {}).get("read_wall_ms", 0.0))
                for row in mapping_records),
            "auxiliary_native_child_cpu_median": statistics.median(float(
                row.get("auxiliary_m2_transport_profile", {}).get("native_child_cpu_ms") or 0.0)
                for row in mapping_records),
        },
        "last_mapping_records": mapping_records,
        "final_graph": counts | {"edge_count": len(last_state["edges"]),
                                 "revision": last_state.get("revision")},
        "worker_terminals": terminals,
    }
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True,
                        help="completed 3DSG live run containing native_graph/packets")
    parser.add_argument("--frame-indices", type=int, nargs="+", required=True)
    parser.add_argument("--stride", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--primary-stride", type=int, choices=(1, 2, 4), default=None)
    parser.add_argument("--auxiliary-stride", type=int, choices=(1, 2, 4), default=None)
    parser.add_argument("--scanpoint-construction", choices=("standard", "trusted"),
                        default="trusted",
                        help="standard per-point validation or adapter's prevalidated subclass")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--conceptgraphs-root", type=Path,
                        default=Path("/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG"))
    parser.add_argument("--occusg-root", type=Path,
                        default=Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG"))
    parser.add_argument("--source", type=Path,
                        default=Path("/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/conceptgraphs_hybrid_runs/c2_20261017T063106Z_99bc38"))
    args = parser.parse_args()
    summary = run(args)
    print(json.dumps({"event": "sequence_replay_complete",
                      "summary": str(args.output.resolve() / "summary.json"),
                      "processed_frames": summary["processed_frames"],
                      "final_graph": summary["final_graph"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
