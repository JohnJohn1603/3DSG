#!/usr/bin/env python3
"""Revalidate C2's unified graph/viewer after a zero-retained-object result.

This narrowly recovers only the final import/viewer gate. It never reruns
capture, perception, room construction, feature encoding, or object mapping.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "artifacts/conceptgraphs_hybrid_runs"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _atomic_write(path: Path, doc: dict) -> None:
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False,
                               allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def revalidate(run_id: str) -> dict:
    from integration_v2.fresh_replica import (
        excluded_native_object_memberships, room_graph_integration_signature,
        validate_unavailable_membership,
    )
    from integration_v2.hybrid_mapping_viewer import build_hybrid_mapping_viewer
    from standalone_sg.serialization.json_graph import load_snapshot

    if Path(run_id).name != run_id or not run_id.startswith("c2_"):
        raise ValueError("run_id must be a C2 run ID")
    run_root = (RUNS / run_id).resolve()
    if not run_root.is_relative_to(RUNS.resolve()):
        raise ValueError("C2 run escapes its artifact directory")
    manifest_path = run_root / "c2_run_manifest.json"
    manifest = _read(manifest_path)
    expected_error = "AssertionError: C2 graph did not retain unavailable_elevation_scope for every imported object"
    if manifest.get("status") != "failed" or manifest.get("error") != expected_error:
        raise ValueError("revalidation only accepts the known zero-retained-object assertion failure")
    for name in ("capture", "perception_p2", "projection_p3", "occusg_rooms",
                 "image_features", "text_features", "conceptgraphs_mapping"):
        if manifest.get("stages", {}).get(name, {}).get("status") != "completed":
            raise ValueError(f"cannot recover: stage {name} did not complete")

    started = time.perf_counter()
    replay = run_root / "replay/snapshot.json"
    mapping = run_root / "mapping"
    final_graph = run_root / "final_graph"
    unified_path = final_graph / "unified_snapshot.json"
    viewer_path = run_root / "hybrid_viewer.html"
    if not all(path.is_file() for path in (replay, mapping / "run_manifest.json",
                                            mapping / "export/export_manifest.json",
                                            mapping / "diagnostic/diagnostic_manifest.json",
                                            mapping / "diagnostic/evidence_manifest.json", unified_path)):
        raise FileNotFoundError("required C2 outputs for final graph/viewer revalidation are missing")
    if viewer_path.exists():
        raise FileExistsError(f"refusing to overwrite existing viewer: {viewer_path}")

    mapping_manifest = _read(mapping / "run_manifest.json")
    export_manifest = _read(mapping / "export/export_manifest.json")
    diag_manifest = _read(mapping / "diagnostic/diagnostic_manifest.json")
    if export_manifest.get("objects") != [] or int(mapping_manifest.get("final_objects_after_text_gated_merge", -1)) != 0:
        raise ValueError("the observed run is not a zero-retained-object mapping result")
    graph = load_snapshot(unified_path)
    base_room = load_snapshot(replay)
    object_count = validate_unavailable_membership(graph)
    if object_count != 0:
        raise ValueError("zero-retained-object result unexpectedly contains unified objects")
    room_nodes = {node.node_id: node for node in graph.nodes if node.kind == "room"}
    if not room_nodes:
        raise ValueError("C2 room snapshot contains no room nodes")
    if room_graph_integration_signature(base_room) != room_graph_integration_signature(graph):
        raise ValueError("unified import changed room geometry, adjacency, or non-object room relations")
    excluded_native_memberships = excluded_native_object_memberships(base_room, graph)
    context = {
        "display_note": "This short route produced 12 pre-filter hypotheses and 0 objects after the locked min_detections=3 filter. Room elevation scope is unverified; object-room membership and room labels are unavailable.",
        "membership_status": "unavailable_elevation_scope",
        "room_semantics_status": "unavailable_membership_scope",
        "execution_mode": "fresh_scene_route_batch / final map; revalidated final import/viewer only",
    }
    build_hybrid_mapping_viewer(replay, mapping, final_graph, viewer_path, viewer_context=context)
    if not viewer_path.is_file() or viewer_path.stat().st_size == 0:
        raise RuntimeError("hybrid viewer builder did not create a non-empty viewer")

    recovery_root = run_root / "recovery"
    recovery_root.mkdir(exist_ok=True)
    initial_path = recovery_root / "c2_run_manifest.initial_failure.json"
    if initial_path.exists():
        raise FileExistsError(f"initial failure record already exists: {initial_path}")
    initial_bytes = manifest_path.read_bytes()
    initial_path.write_bytes(initial_bytes)

    cap = _read(run_root / "capture/capture_manifest.json")
    p2 = _read(run_root / "detection_2d/detection_2d_index.json")
    p3 = _read(run_root / "detection_3d/detection_3d_manifest.json")
    rooms = _read(run_root / "replay/manifest.json")
    image_rows = [line for line in (run_root / "image_features/feature_manifest.jsonl").read_text().splitlines() if line.strip()]
    text_rows = [line for line in (run_root / "text_features/text_feature_manifest.jsonl").read_text().splitlines() if line.strip()]
    mapping_elapsed = mapping_manifest.get("mapping_runtime_s")
    stage_elapsed = time.perf_counter() - started
    manifest.pop("error", None)
    manifest["initial_validation_failure"] = {
        "error": expected_error,
        "manifest_backup": str(initial_path.relative_to(run_root)),
        "manifest_backup_sha256": _sha(initial_path),
        "cause": "The final graph gate incorrectly required at least one retained object; the locked filter legitimately retained zero.",
        "recovery_scope": "unified graph validation and viewer generation only; no capture/perception/feature/mapping rerun",
    }
    manifest["summary"] = {
        "frame_count": len(cap.get("frames", [])),
        "p2_observation_count": sum(len(frame.get("detections", [])) for frame in p2.get("frames", [])),
        "p3_accepted": p3.get("detection_3d_count"),
        "p3_rejected": p3.get("rejected_detection_count"),
        "image_feature_count": len(image_rows),
        "text_feature_count": len(text_rows),
        "pre_filter_hypotheses": mapping_manifest.get("foreground_objects_before_postprocess"),
        "objects_after_filter": mapping_manifest.get("objects_after_filter"),
        "final_conceptgraphs_objects": mapping_manifest.get("final_objects_after_text_gated_merge"),
        "room_count": len(room_nodes),
        "membership_status": "unavailable_elevation_scope",
        "room_semantics_status": "unavailable_membership_scope",
        "mapping_runtime_s": mapping_elapsed,
        "mapping_peak_rss_mib": mapping_manifest.get("peak_rss_mib"),
        "route_type": cap.get("route_type"),
        "source_native_object_memberships_excluded": len(excluded_native_memberships),
        "source_native_object_membership_exclusions": excluded_native_memberships,
    }
    manifest["stages"]["unified_graph_and_viewer"] = {
        "status": "completed", "elapsed_s": round(stage_elapsed, 4),
        "revalidated_from_existing_outputs": True,
        "object_count": object_count, "room_count": len(room_nodes),
        "room_contains_object_edges": 0, "room_graph_projection_unchanged": True,
        "source_native_object_memberships_excluded": len(excluded_native_memberships),
        "membership_status": "unavailable_elevation_scope",
        "room_semantics_status": "unavailable_membership_scope",
    }
    manifest["outputs"] = {
        "capture_manifest": str(run_root / "capture/capture_manifest.json"),
        "p2_index": str(run_root / "detection_2d/detection_2d_index.json"),
        "p3_manifest": str(run_root / "detection_3d/detection_3d_manifest.json"),
        "room_snapshot": str(replay),
        "object_timeline_manifest": str(run_root / "object_timeline/timeline/manifest.json"),
        "object_export_manifest": str(mapping / "export/export_manifest.json"),
        "unified_graph": str(unified_path),
        "viewer": str(viewer_path),
    }
    manifest["stages_actually_executed"] = [name for name, stage in manifest["stages"].items()
                                             if stage.get("status") == "completed"]
    manifest["stages_reused"] = [name for name, stage in manifest["stages"].items()
                                 if stage.get("status") == "reused"]
    manifest["execution_mode"] = "fresh_scene_route_batch"
    manifest["revalidation_command"] = [sys.executable, str(ROOT / "scripts/revalidate_c2_unified_output.py"),
                                         "--run-id", run_id]
    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["status"] = "completed"
    manifest["current_stage"] = None
    manifest["output_sha256"] = {
        path.relative_to(run_root).as_posix(): _sha(path)
        for path in sorted(run_root.rglob("*"))
        if path.is_file() and path != manifest_path and not path.name.endswith(".tmp")
    }
    _atomic_write(manifest_path, manifest)
    return {"status": "completed", "run_id": run_id, "summary": manifest["summary"],
            "viewer": str(viewer_path), "manifest": str(manifest_path),
            "initial_failure_record": str(initial_path), "final_validation_elapsed_s": round(stage_elapsed, 4),
            "output_files_hashed": len(manifest["output_sha256"]),
            "unused_room_replay_stage_frames": rooms.get("frames")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(json.dumps(revalidate(args.run_id), indent=2, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
