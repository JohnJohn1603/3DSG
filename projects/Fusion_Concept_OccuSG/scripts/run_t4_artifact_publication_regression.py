#!/usr/bin/env python3
"""Republish one locked failed T4 branch pair from artifacts only.

No detector, encoder, model, ConceptGraphs update, OccuSG update, or capture is
run. The copied branch events are joined through the production publication
helpers into a new regression directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from integration_v2 import live_processing as lp  # noqa: E402
from integration_v2.room_belief_commit_session import RoomBeliefCommitSession  # noqa: E402
from scripts import run_conceptgraphs_cached_hybrid as cg_config  # noqa: E402
from integration_v2.camera_live_contract import host_monotonic_domain  # noqa: E402


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value: dict) -> None:
    lp._write_json(path, value)


def _file_hashes(root: Path, *, excluded: set[str] = frozenset()) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha(path)
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.relative_to(root).as_posix() not in excluded}


def _http_read_all(run_root: Path, viewer: Path, frame: dict, evidence: dict) -> dict:
    handler = partial(SimpleHTTPRequestHandler, directory=str(run_root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    checked: dict[str, str] = {}
    try:
        targets: dict[str, str] = {}
        for rel in ("index.html", "live_index.json", "live_head.json", "evidence_index.json"):
            targets[f"viewer/{rel}"] = sha(viewer / rel)
        targets["viewer/xyz_renderer.js"] = sha(viewer / "xyz_renderer.js")
        for field in ("unified_url", "snapshot_url", "room_url", "geometry_url", "joined_state_url",
                      "room_belief_overlay_url"):
            relative = "viewer/" + frame[field]
            targets[relative] = sha(lp._resolve_viewer_artifact_url(run_root, viewer, frame[field], label=field))
        targets["viewer/" + evidence["camera_rgb"]["url"]] = evidence["camera_rgb"]["sha256"]
        for observation in evidence.get("observations", []):
            for panel in observation["panels"].values():
                targets["viewer/" + panel["url"]] = panel["sha256"]
        for relative, expected_sha in targets.items():
            with urlopen(urljoin(base, relative), timeout=5) as response:
                body = response.read()
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} for {relative}")
            actual_sha = hashlib.sha256(body).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(f"HTTP viewer asset hash differs from local committed asset: {relative}")
            checked[relative] = actual_sha
        index_doc = json.loads((viewer / "live_index.json").read_text(encoding="utf-8"))
        head_doc = json.loads((viewer / "live_head.json").read_text(encoding="utf-8"))
        if sha(viewer / "live_index.json") != head_doc.get("live_index_sha256"):
            raise ValueError("HTTP viewer live_head does not bind current live_index")
        if len(index_doc.get("frames", [])) != 1:
            raise ValueError("HTTP viewer index does not expose the single regression revision")
        return {"status": "pass", "method": "localhost HTTP GET", "base_url": base,
                "checked_paths": checked, "head_revision_count": head_doc.get("revision_count"),
                "head_status": head_doc.get("status")}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run(source_run: Path, output: Path) -> dict:
    source_run = source_run.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite regression output: {output}")
    if not (source_run / "run_manifest.json").is_file():
        raise FileNotFoundError(f"failed source run manifest is missing: {source_run}")
    original_manifest = json.loads((source_run / "run_manifest.json").read_text(encoding="utf-8"))
    source_run_id = str(original_manifest["run_id"])
    source_packet = json.loads((source_run / "packets/frame-000000.json").read_text(encoding="utf-8"))
    source_perception = json.loads((source_run / "packets/joined-000000.json").read_text(encoding="utf-8"))
    p2_response = json.loads((source_run / "packets/p2-000000.json").read_text(encoding="utf-8"))
    source_frame = source_run / "capture" / source_packet["capture_record"]["frame_dir"]
    source_metadata = json.loads((source_frame / "metadata.json").read_text(encoding="utf-8"))
    if "raw_files_sha256" in source_metadata:
        raise ValueError("locked failed-run artifact no longer matches its known legacy metadata contract")
    if source_packet.get("capture_record", {}).get("schema") is not None:
        raise ValueError("locked failed-run artifact no longer matches its known unversioned capture record")
    if source_packet.get("source_hashes") != source_packet["capture_record"].get("raw_files_sha256"):
        raise ValueError("source packet and legacy capture record hashes differ")

    source_inputs = [source_run / "run_manifest.json", source_run / "packets/frame-000000.json",
                     source_run / "packets/joined-000000.json", source_run / "packets/p2-000000.json",
                     source_run / "branches/objects/events.jsonl", source_run / "branches/rooms/events.jsonl"]
    for branch in ("objects", "rooms"):
        event_rows = [json.loads(line) for line in (source_run / f"branches/{branch}/events.jsonl").read_text().splitlines() if line.strip()]
        for row in event_rows:
            source_inputs.append(source_run / row["state_path"])
            source_inputs.extend(source_run / relative for relative in row.get("asset_sha256", {}))
    for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"):
        source_inputs.append(source_frame / name)
    for detection in p2_response["detections"]:
        source_inputs.append(source_run / "packets/assets/detection_2d" / detection["mask_file"])
    input_hashes_before = {path.relative_to(source_run).as_posix(): sha(path) for path in sorted(set(source_inputs))}

    output.mkdir(parents=True)
    for branch in ("objects", "rooms"):
        shutil.copytree(source_run / "branches" / branch, output / "branches" / branch)
    capture_rel = Path(source_packet["capture_record"]["frame_dir"])
    destination_frame = output / "capture" / capture_rel
    destination_frame.mkdir(parents=True)
    for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"):
        shutil.copy2(source_frame / name, destination_frame / name)
    for relative in ("packets/frame-000000.json", "packets/joined-000000.json", "packets/p2-000000.json"):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_run / relative, destination)
    mask_root = output / "packets/assets/detection_2d"
    mask_root.mkdir(parents=True)
    for detection in p2_response["detections"]:
        relative = Path(detection["mask_file"])
        source_mask = source_run / "packets/assets/detection_2d" / relative
        target_mask = mask_root / relative
        target_mask.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_mask, target_mask)

    regression_id = f"artifact_publication_regression_{source_run_id}"
    manifest = {"schema": "t4-artifact-publication-regression/v1", "run_id": regression_id,
        "source_run_id": source_run_id, "execution_mode": "artifact_driven_regression",
        "status": "started", "started_utc": datetime.now(timezone.utc).isoformat(),
        "source_run_manifest_sha256": sha(source_run / "run_manifest.json"),
        "source_frame_id": source_packet["frame_id"], "source_frame_index": source_packet["frame_index"],
        "source_hashes": dict(source_packet["source_hashes"]),
        "prefix_source_revisions": {"objects": 1, "rooms": 1},
        "mapper_updates_executed": 0, "perception_executed": False,
        "capture_executed": False, "semantic_update_executed": False,
        "scope_policy": "verified_only"}
    write_json(output / "run_manifest.json", manifest)

    viewer, _ = lp._prepare_viewer(source_run, output, source_packet["scene_id"], source_packet["sequence_id"],
        evidence_mode="camera_live_current_run_only", run_id=source_run_id)
    # causal_timeline_viewer.html imports this pinned local asset; the viewer
    # bundle must be self-contained and must not rely on a CDN.
    renderer_source = ROOT / "scripts/xyz_renderer.js"
    renderer_target = viewer / "xyz_renderer.js"
    if renderer_target.exists():
        raise FileExistsError(f"refusing to overwrite viewer renderer asset: {renderer_target}")
    shutil.copy2(renderer_source, renderer_target)
    base_index = lp._read(viewer / "live_index.json")
    object_event = json.loads((output / "branches/objects/events.jsonl").read_text().splitlines()[0])
    room_event = json.loads((output / "branches/rooms/events.jsonl").read_text().splitlines()[0])
    frames: list[dict] = []
    joined_event = lp._join_one(output, viewer, base_index, frames, object_event, room_event,
                                publish_index=False, publish_event=False, acknowledge=False)
    frame = frames[-1]
    from integration_v2.live_evidence import publish_live_frame_evidence
    frame["live_evidence"] = publish_live_frame_evidence(
        run_root=output, viewer_root=viewer, run_id=source_run_id,
        scene_id=source_packet["scene_id"], sequence_id=source_packet["sequence_id"],
        frame_id=source_packet["frame_id"], frame_index=int(source_packet["frame_index"]),
        timestamp_s=float(source_packet["timestamp_s"]),
        timestamp_clock_domain=source_packet["timestamp_clock_domain"],
        capture_wall_time_unix_s=float(source_packet["capture_wall_time_unix_s"]),
        capture_monotonic_s=float(source_packet["capture_monotonic_s"]),
        capture_monotonic_domain=source_packet["capture_monotonic_domain"],
        frame_dir=destination_frame, capture_record=source_packet["capture_record"],
        source_hashes=source_packet["source_hashes"], detections=p2_response["detections"],
        mask_asset_root=mask_root, allow_legacy_unversioned_record=True)

    graph_path = (viewer / frame["unified_url"]).resolve()
    graph_doc = lp._read(graph_path)
    object_path = (output / object_event["state_path"]).resolve()
    room_path = (output / room_event["state_path"]).resolve()
    object_doc = lp._read(object_path)
    room_doc = lp._read(room_path)
    pose = object_doc.get("camera_pose_W_Copt")
    observations = []
    for detection in p2_response["detections"]:
        oid = str(detection["detection_id"])
        observations.append({"observation_id": oid, "frame_id": source_packet["frame_id"],
            "frame_index": int(source_packet["frame_index"]), "raw_class": str(detection["class_raw"]),
            "camera_pose_W_Copt": pose,
            "source_hashes": dict(source_packet["source_hashes"]) | {f"mask:{oid}": str(detection["mask_sha256"])}})
    source_hashes = dict(source_packet["source_hashes"]) | {
        "perception_packet": sha(source_run / "packets/joined-000000.json"),
        "object_state": str(object_event["state_sha256"]), "room_state": str(room_event["state_sha256"]),
        "object_event": str(object_event["event_sha256"]), "room_event": str(room_event["event_sha256"])}
    envelope = {"event_id": f"unified:1:{source_packet['frame_id']}", "run_id": source_run_id,
        "scene_id": source_packet["scene_id"], "sequence_id": source_packet["sequence_id"],
        "frame_index": int(graph_doc["frame_index"]), "frame_id": graph_doc["frame_id"],
        "timestamp_s": float(graph_doc["timestamp_s"]), "unified_revision": int(frame["revision"]),
        "graph_revision_id": str(graph_doc["graph_revision_id"]), "graph_path": str(graph_path),
        "graph_sha256": sha(graph_path), "object_revision": int(object_doc["revision"]),
        "object_state_sha256": sha(object_path),
        "room_revision": int(room_doc.get("revision", room_doc.get("snapshot", {}).get("revision"))),
        "room_state_sha256": sha(room_path), "source_hashes": source_hashes,
        "current_observations": observations,
        "objects": [{"object_id": str(row["hypothesis_id"]),
            "observation_ids": list(row.get("observation_ids", [])),
            "detection_count": int(row.get("detection_count", 0)),
            "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope"}}
            for row in object_doc.get("objects", [])],
        "rooms": [{"node_id": str(row["node_id"])} for row in graph_doc.get("rooms", [])],
        "scope": {"status": "unavailable", "evidence_status": "unavailable",
                  "reason": "unavailable_elevation_scope"}}
    semantic_config = ROOT / "configs/room_semantics/t4_live_room_belief_v1.json"
    mapping_config = cg_config.base_mapping_config()
    semantic = RoomBeliefCommitSession.open(config_path=semantic_config,
        overlay_root=output / "semantics/overlays",
        admission_min_detections=int(mapping_config["obj_min_detections"]))
    try:
        if semantic.config.get("scope_policy") != {"mode": "verified_only", "assumed_declaration": None}:
            raise ValueError("artifact regression accepts only the locked verified-only policy")
        semantic.bind_identity(run_id=source_run_id, scene_id=source_packet["scene_id"],
                               sequence_id=source_packet["sequence_id"])
        semantic_result = semantic.update_after_unified_commit(envelope)
    finally:
        semantic.close()
    overlay_path = Path(semantic_result["overlay_path"]).resolve()
    frame["room_belief_overlay_url"] = Path(os.path.relpath(overlay_path, viewer)).as_posix()
    frame["room_belief_overlay_sha256"] = semantic_result["overlay_sha256"]
    frame["room_belief_status"] = semantic_result["overlay"]["room_semantics_availability"]["status"]
    frame["room_belief_scope_status"] = semantic_result["overlay"]["scope_status"]
    frame["room_belief_variant_count"] = 4
    lp._publish_joined_semantic_commit(output, viewer, base_index, frames, joined_event,
                                       object_event, room_event)
    (output / "joined/frames.jsonl").write_text(json.dumps(frame, sort_keys=True) + "\n", encoding="utf-8")
    lp._write_live_index(output, viewer, base_index, frames, "completed", {
        "status": "artifact_driven_regression", "terminal_postprocess": "not_executed",
        "source_run_status": original_manifest.get("status"), "semantic_status": frame["room_belief_status"]})

    published = lp._read(viewer / "live_index.json")
    head = lp._read(viewer / "live_head.json")
    overlay = lp._read(overlay_path)
    if len(published["frames"]) != 1 or head["revision_count"] != 1:
        raise ValueError("published index/head do not expose exactly one paired revision")
    if frame["room_belief_status"] != "not_scored" or overlay["room_semantics_availability"]["status"] != "not_scored":
        raise ValueError("verified-only unavailable scope did not remain not_scored")
    if overlay["scope_status"] != "unavailable" or overlay["room_contains_edges"]:
        raise ValueError("unavailable scope produced semantic membership/edges")
    if overlay["graph_sha256"] != frame["unified_sha256"] or overlay["frame_id"] != frame["frame_id"]:
        raise ValueError("semantic overlay does not bind the exact graph/frame")
    http_result = _http_read_all(output, viewer, frame, frame["live_evidence"])

    input_hashes_after = {path.relative_to(source_run).as_posix(): sha(path) for path in sorted(set(source_inputs))}
    if input_hashes_before != input_hashes_after:
        raise ValueError("source run inputs changed during artifact-driven regression")
    manifest.update({"status": "completed", "finished_utc": datetime.now(timezone.utc).isoformat(),
        "semantic_update_executed": True, "semantic_status": "not_scored",
        "semantic_reason": overlay["scope_reason"], "unified_revisions": 1,
        "branch_updates_reused": {"objects": 1, "rooms": 1},
        "branch_source_event_hashes": {"objects": object_event["event_sha256"], "rooms": room_event["event_sha256"]},
        "graph_sha256": frame["unified_sha256"], "overlay_sha256": frame["room_belief_overlay_sha256"],
        "live_index_sha256": sha(viewer / "live_index.json"), "live_head_sha256": sha(viewer / "live_head.json"),
        "live_head_binds_live_index": head.get("live_index_sha256") == sha(viewer / "live_index.json"),
        "http_viewer_data_load": http_result, "source_inputs_unchanged": True,
        "source_input_hashes": input_hashes_before,
        "notes": ["No mapper, capture, detector, encoder, Qwen, or membership update ran.",
                  "Verified-only scope gate remained unavailable; room scoring was executed as not_scored."]})
    write_json(output / "run_manifest.json", manifest)
    output_hashes = _file_hashes(output, excluded={"run_manifest.json", "output_sha256.json", "run_manifest.sha256.json"})
    write_json(output / "output_sha256.json", {"schema": "t4-artifact-regression-output-hashes/v1",
        "files": output_hashes})
    manifest["output_hashes_sha256"] = sha(output / "output_sha256.json")
    write_json(output / "run_manifest.json", manifest)
    write_json(output / "run_manifest.sha256.json", {"schema": "t4-artifact-regression-manifest-receipt/v1",
        "manifest": "run_manifest.json", "sha256": sha(output / "run_manifest.json"), "status": "completed"})
    return manifest


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.source_run, args.output)
    print(json.dumps({"status": result["status"], "execution_mode": result["execution_mode"],
        "unified_revisions": result["unified_revisions"], "semantic_status": result["semantic_status"],
        "http_viewer_data_load": result["http_viewer_data_load"]["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
