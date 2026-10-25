"""D1 lockstep cached live processing and immutable graph revision join."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import numpy as np

from standalone_sg.serialization.live_revision_io import (
    EventLogReader, append_jsonl, atomic_write_json, canonical_json, read_complete_jsonl, sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
C2_ROOT = ROOT / "artifacts/conceptgraphs_hybrid_runs"
D_ROOT = ROOT / "artifacts/live_processing_runs"
MAX_DISPLAY_POINTS = 300


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return sha256_file(Path(path))


def _read(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def _resolve_run_artifact(run_root: Path, artifact_path: str, *, label: str) -> Path:
    """Resolve an explicit run-root-relative filesystem reference."""
    if not isinstance(artifact_path, str) or not artifact_path or "\\" in artifact_path:
        raise ValueError(f"{label} must be a non-empty POSIX run-relative path")
    raw = Path(artifact_path)
    if raw.is_absolute():
        raise ValueError(f"{label} must be run-relative, not absolute")
    root = Path(run_root).resolve()
    target = (root / raw).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{label} escapes the run artifact root")
    if not target.is_file():
        raise FileNotFoundError(f"{label} is missing: {artifact_path}")
    return target


def _resolve_viewer_artifact_url(run_root: Path, viewer: Path, viewer_url: str, *, label: str) -> Path:
    """Resolve a viewer-relative URL against ``viewer`` exactly once."""
    if not isinstance(viewer_url, str) or not viewer_url or "\\" in viewer_url:
        raise ValueError(f"{label} must be a non-empty viewer-relative URL")
    parsed = urlsplit(viewer_url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or parsed.path.startswith("/"):
        raise ValueError(f"{label} must be a relative artifact URL without query/fragment")
    decoded = unquote(parsed.path)
    if not decoded or "\\" in decoded:
        raise ValueError(f"{label} has an invalid URL path")
    root, viewer_root = Path(run_root).resolve(), Path(viewer).resolve()
    if not viewer_root.is_relative_to(root):
        raise ValueError("viewer root is outside the run artifact root")
    target = (viewer_root / decoded).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{label} escapes the run artifact root")
    if not target.is_file():
        raise FileNotFoundError(f"{label} is missing: {viewer_url}")
    return target


def _viewer_artifact_url(run_root: Path, viewer: Path, artifact: Path) -> str:
    """Serialize a run artifact as a URL relative to the viewer directory."""
    root, viewer_root, target = Path(run_root).resolve(), Path(viewer).resolve(), Path(artifact).resolve()
    if not viewer_root.is_relative_to(root) or not target.is_relative_to(root) or not target.is_file():
        raise ValueError("viewer artifact URL requires an existing file inside the run root")
    relative = os.path.relpath(target, viewer_root).replace(os.sep, "/")
    return quote(relative, safe="/-._~")


def _write_run_manifest_hash_receipt(run_root: Path) -> Path:
    """Write a detached hash for the final enclosing manifest (which cannot self-hash)."""
    manifest_path = run_root / "run_manifest.json"
    receipt_path = run_root / "run_manifest.sha256.json"
    atomic_write_json(receipt_path, {"schema": "d1-run-manifest-hash-receipt/v1",
                                    "manifest": manifest_path.name,
                                    "sha256": _sha(manifest_path),
                                    "status": _read(manifest_path).get("status"),
                                    "created_utc": _utc()})
    return receipt_path


def _wait_for_explicit_viewer_start(run_root: Path, run_id: str,
                                    pointer_sha256: str, timeout_s: float) -> dict[str, Any]:
    """Block only before producers; after the validated release browser is not required."""
    start_path = run_root / "control/start.json"
    stop_path = run_root / "control/stop.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if stop_path.is_file():
            stop = _read(stop_path)
            raise InterruptedError(f"D1 start gate cancelled: {stop.get('reason', stop.get('status'))}")
        if start_path.is_file():
            signal_doc = _read(start_path)
            from integration_v2.camera_live_startup import (
                StartupContractError, validate_viewer_start_record,
            )
            pointer_path = run_root / "viewer/d2_viewer_entrypoint.json"
            pointer = _read(pointer_path)
            manifest = _read(run_root / "run_manifest.json")
            expected_url = manifest.get("viewer", {}).get("url")
            if not isinstance(expected_url, str):
                raise ValueError("D1 run manifest has no explicit viewer URL")
            try:
                validate_viewer_start_record(signal_doc, run_id=run_id, pointer=pointer,
                    pointer_sha256=sha256_file(pointer_path), expected_url=expected_url,
                    not_before_utc=manifest.get("created_utc"))
            except StartupContractError as exc:
                raise ValueError(f"D1 explicit start signal is invalid or refers to different viewer assets: {exc}") from exc
            if pointer_sha256 != sha256_file(pointer_path):
                raise ValueError("D1 viewer pointer changed while waiting for browser confirmation")
            return signal_doc
        time.sleep(0.1)
    raise TimeoutError(f"D1 viewer start gate timed out after {timeout_s:.0f}s; no producer was started")


def _seal_run_integrity(run_root: Path, run_doc: dict[str, Any],
                        server: subprocess.Popen | None = None) -> None:
    """Hash all immutable outputs; snapshot mutable operational logs separately."""
    immutable: dict[str, str] = {}
    operational: dict[str, dict[str, Any]] = {}
    server_alive = bool(server and server.poll() is None)
    for path in sorted(run_root.rglob("*")):
        if not path.is_file() or path == run_root / "run_manifest.json":
            continue
        rel = path.relative_to(run_root).as_posix()
        is_log = rel.startswith("logs/") or (path.name.startswith("viewer_server") and path.suffix == ".log")
        if is_log:
            operational[rel] = {"sha256_at_checkpoint": _sha(path),
                                "mutable_after_checkpoint": server_alive and path.name == "viewer_server.log",
                                "writer": ({"kind": "run_owned_viewer_server", "pid": server.pid,
                                            "alive_at_checkpoint": server_alive}
                                           if server_alive and path.name == "viewer_server.log" else
                                           {"kind": "closed_or_external_log", "alive_at_checkpoint": False})}
        else:
            immutable[rel] = _sha(path)
    checkpoint_utc = _utc()
    run_doc["immutable_output_sha256"] = immutable
    run_doc["output_sha256"] = immutable
    run_doc["output_sha256_scope"] = ("immutable producer outputs; includes graph, geometry, branch/object/room and D1/D2 manifests, "
                                      "entrypoint pointer and viewer assets; excludes mutable operational logs, the enclosing "
                                      "run_manifest.json self-reference, and its detached SHA receipt. No child graph, geometry "
                                      "or D1/D2 manifest is excluded.")
    run_doc["operational_logs"] = {"hash_checkpoint_utc": checkpoint_utc,
                                   "hash_scope": "snapshot only; live server access log may continue changing",
                                   "files": operational}
    run_doc["hash_checkpoint"] = {"utc": checkpoint_utc,
                                  "writer_status": {"run_owned_viewer_server":
                                      ({"pid": server.pid, "alive": server_alive} if server else "not_owned_or_not_running"),
                                      "object_producer": "exited", "room_producer": "exited"},
                                  "root_manifest_sha256_receipt": "run_manifest.sha256.json; detached receipt written after final manifest write"}
    run_doc.pop("output_hash_exclusions", None)


def _arg_value(args: list[str], option: str) -> str:
    try:
        return args[args.index(option) + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"locked C2 stage command is missing {option}") from exc


def _replace_arg(args: list[str], option: str, value: str) -> list[str]:
    result = list(args)
    try:
        index = result.index(option)
    except ValueError as exc:
        raise ValueError(f"locked C2 stage command is missing {option}") from exc
    if index + 1 >= len(result):
        raise ValueError(f"locked C2 stage command has no value for {option}")
    result[index + 1] = value
    return result


def _copy_evidence(source_mapping: Path, viewer: Path) -> tuple[str, dict[str, str]]:
    source_manifest = source_mapping / "diagnostic/evidence_manifest.json"
    if not source_manifest.is_file():
        raise FileNotFoundError(f"C2 cache lacks view evidence: {source_manifest}")
    raw = _read(source_manifest)
    evidence_root = viewer / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=False)
    rows: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    for observation_id, row in raw.get("observations", {}).items():
        panels: dict[str, Any] = {}
        for kind, rel in row.get("panels", {}).items():
            src = source_mapping / "diagnostic" / str(rel)
            if not src.is_file():
                raise FileNotFoundError(f"C2 evidence asset missing: {src}")
            target = evidence_root / f"{hashlib.sha256(observation_id.encode()).hexdigest()[:16]}_{kind}{src.suffix.lower()}"
            target.write_bytes(src.read_bytes())
            digest = _sha(target)
            panels[kind] = {"url": f"evidence/{target.name}", "sha256": digest,
                            "source_sha256": _sha(src)}
            hashes[target.relative_to(viewer).as_posix()] = digest
        rows[observation_id] = {**row, "panels": panels}
    evidence_index = viewer / "evidence_index.json"
    _write_json(evidence_index, {"schema": "d1-live-viewer-evidence/v1",
                                 "source_manifest": str(source_manifest),
                                 "source_manifest_sha256": _sha(source_manifest),
                                 "observations": rows})
    hashes[evidence_index.relative_to(viewer).as_posix()] = _sha(evidence_index)
    return "evidence_index.json", hashes


def _verify_source_run(source_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if Path(source_id).name != source_id or not source_id.startswith("c2_"):
        raise ValueError("--source-run-id must be a C2 run ID, not a path")
    source = (C2_ROOT / source_id).resolve()
    if not source.is_relative_to(C2_ROOT.resolve()):
        raise ValueError("C2 source run escapes the workspace run directory")
    manifest_path = source / "c2_run_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"C2 manifest missing: {manifest_path}")
    manifest = _read(manifest_path)
    if manifest.get("status") != "completed" or manifest.get("execution_mode") != "fresh_scene_route_batch":
        raise ValueError(f"D1 requires a completed C2 run; status={manifest.get('status')}")
    stages = manifest.get("stages", {})
    for name in ("occusg_rooms", "conceptgraphs_mapping"):
        stage = stages.get(name, {})
        if stage.get("status") != "completed" or int(stage.get("exit_code", -1)) != 0:
            raise ValueError(f"C2 stage {name} is not completed successfully")
        command = stage.get("command")
        if not isinstance(command, list) or len(command) < 3:
            raise ValueError(f"C2 stage {name} has no reproducible command")
        if not Path(command[0]).is_file() or not Path(command[2]).is_file():
            raise FileNotFoundError(f"C2 stage {name} interpreter/runner is missing")
    required = [source / p for p in (
        "capture/capture_manifest.json", "detection_2d/detection_2d_index.json",
        "detection_3d/detection_3d.jsonl", "detection_3d/detection_3d_manifest.json",
        "image_features/feature_manifest.jsonl", "text_features/manifest.json",
        "replay/snapshot.json", "replay/manifest.json", "mapping/experiment_contract.json",
        "mapping/diagnostic/evidence_manifest.json")]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("C2 run is missing required locked inputs: " + ", ".join(missing))
    cap = _read(source / "capture/capture_manifest.json")
    room_replay = _read(source / "replay/manifest.json")
    mapping_contract = _read(source / "mapping/experiment_contract.json")
    p3 = _read(source / "detection_3d/detection_3d_manifest.json")
    room_snapshot = _read(source / "replay/snapshot.json")
    room_snapshot = room_snapshot.get("snapshot", room_snapshot)
    expected_scene = (manifest.get("scene_id"), manifest.get("sequence_id"))
    for name, doc in (("capture", cap), ("room snapshot", room_snapshot)):
        if (doc.get("scene_id"), doc.get("sequence_id")) != expected_scene:
            raise ValueError(f"{name} scene/sequence does not match C2 manifest")
    cap_sha = _sha(source / "capture/capture_manifest.json")
    if room_replay.get("capture_manifest_sha256") != cap_sha or p3.get("capture_manifest_sha256") != cap_sha:
        raise ValueError("C2 room/P3 capture provenance does not match the locked capture")
    contract_expected = {
        "capture_manifest_sha256": cap_sha,
        "p2_index_sha256": _sha(source / "detection_2d/detection_2d_index.json"),
        "p3_manifest_sha256": _sha(source / "detection_3d/detection_3d_manifest.json"),
        "image_features_manifest_sha256": _sha(source / "image_features/feature_manifest.jsonl"),
        "text_features_manifest_sha256": _sha(source / "text_features/manifest.json"),
        "room_snapshot_sha256": _sha(source / "replay/snapshot.json"),
    }
    for key, value in contract_expected.items():
        if mapping_contract.get(key) != value:
            raise ValueError(f"C2 mapping contract hash mismatch for {key}")
    p2 = _read(source / "detection_2d/detection_2d_index.json")
    cap_frames = cap.get("frames", [])
    p2_frames = p2.get("frames", [])
    if len(cap_frames) != len(p2_frames) or len(cap_frames) != int(manifest.get("summary", {}).get("frame_count", len(cap_frames))):
        raise ValueError("capture/P2/C2 frame population is misaligned")
    capture_ids = [str(row["frame_id"]) for row in cap_frames]
    p2_ids = [str(row["frame_id"]) for row in p2_frames]
    if capture_ids != p2_ids:
        raise ValueError("capture and P2 frame IDs/order do not match")
    if int(room_replay.get("frames", -1)) != len(cap_frames):
        raise ValueError("C2 room replay frame count differs from capture")
    expected_outputs = manifest.get("output_sha256", {})
    required_prefixes = ("capture/", "detection_2d/", "detection_3d/", "image_features/",
                         "text_features/", "replay/", "mapping/diagnostic/")
    locked_outputs = {str(rel): str(digest) for rel, digest in expected_outputs.items()
                      if str(rel).startswith(required_prefixes)}
    if not locked_outputs:
        raise ValueError("C2 manifest has no checksummed locked input inventory")
    for rel, expected in locked_outputs.items():
        path = (source / rel).resolve()
        if not path.is_relative_to(source.resolve()) or not path.is_file() or _sha(path) != expected:
            raise ValueError(f"C2 locked input file is missing or hash-mismatched: {rel}")
    return source, manifest, {"capture": cap, "p2": p2, "mapping_contract": mapping_contract,
                              "contract_hashes": contract_expected, "room_snapshot": room_snapshot,
                              "locked_output_hashes": locked_outputs}


def _count_room_contains(room_doc: dict[str, Any]) -> dict[str, int]:
    doc = room_doc.get("snapshot", room_doc)
    kinds = {row.get("node_id"): row.get("kind") for row in doc.get("nodes", [])}
    counts: dict[str, int] = {}
    for edge in doc.get("edges", []):
        if edge.get("relation") != "room_contains":
            continue
        kind = kinds.get(edge.get("target_id"), "unknown")
        if kind == "object":
            kind = "occusg_native_object"
        elif kind == "agent":
            kind = "agent"
        elif kind == "navigation":
            kind = "navigation"
        counts[kind] = counts.get(kind, 0) + 1
    counts["conceptgraphs_object"] = 0
    return counts


def _verify_event(root: Path, row: dict[str, Any], expected_branch: str) -> Path:
    supplied = row.get("event_sha256")
    payload = {key: value for key, value in row.items() if key != "event_sha256"}
    actual = hashlib.sha256(canonical_json(payload)).hexdigest()
    if supplied != actual or row.get("branch") != expected_branch:
        raise ValueError(f"branch event digest/identity failed for {expected_branch}")
    for relative, expected_hash in row.get("asset_sha256", {}).items():
        path = _resolve_run_artifact(root, relative, label=f"{expected_branch} asset")
        if _sha(path) != expected_hash:
            raise ValueError(f"published asset missing or hash mismatch: {relative}")
    state = _resolve_run_artifact(root, row["state_path"], label=f"{expected_branch} state")
    if _sha(state) != row["state_sha256"]:
        raise ValueError(f"published state hash mismatch: {row['state_path']}")
    return state


def _sample_geometry(root: Path, object_state_path: Path, viewer: Path,
                     display_max_points: int = MAX_DISPLAY_POINTS) -> tuple[Path, dict[str, Any]]:
    state = _read(object_state_path)
    objects: list[dict[str, Any]] = []
    all_xy: list[tuple[float, float]] = []
    for row in state.get("objects", []):
        source = (object_state_path.parent.parent / str(row["cloud_npz"])).resolve()
        if not source.is_relative_to(root.resolve()) or not source.is_file() or _sha(source) != row.get("cloud_sha256"):
            raise ValueError(f"object cloud missing/hash mismatch: {source}")
        with np.load(source, allow_pickle=False) as archive:
            xyz = np.asarray(archive["xyz"], dtype=np.float32)
            rgb = np.asarray(archive["rgb"], dtype=np.uint8) if "rgb" in archive else np.zeros((len(xyz), 3), dtype=np.uint8)
        if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
            raise ValueError(f"invalid object geometry in {source}")
        if len(xyz) > display_max_points:
            indices = np.linspace(0, len(xyz) - 1, display_max_points, dtype=np.int64)
            xyz, rgb = xyz[indices], rgb[indices]
        objects.append({"hypothesis_id": row["hypothesis_id"], "points": xyz.astype(float).tolist(),
                        "colors": rgb.astype(int).tolist(), "sampled_points": len(xyz),
                        "source_points": int(row.get("point_count", len(xyz))),
                        "source_npz": str(source.relative_to(root)),
                        "source_sha256": row["cloud_sha256"]})
        all_xy.extend((float(p[0]), float(p[1])) for p in xyz)
    if state.get("camera_pose_W_Copt"):
        pose = state["camera_pose_W_Copt"]
        all_xy.append((float(pose[0][3]), float(pose[1][3])))
    target = viewer / "geometry" / f"rev-{int(state['revision']):06d}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, {"schema": "d1-live-derived-geometry/v1",
                         "source_snapshot_sha256": _sha(object_state_path),
                         "max_points_per_object": display_max_points,
                         "objects": objects})
    return target, {"points": all_xy, "objects": len(objects)}


def _make_graph_revision(root: Path, viewer: Path, revision: int,
                          object_state: dict[str, Any], room_state: dict[str, Any],
                          object_event: dict[str, Any], room_event: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    room_doc = room_state.get("snapshot", room_state)
    if (object_state.get("scene_id"), object_state.get("sequence_id")) != (room_doc.get("scene_id"), room_doc.get("sequence_id")):
        raise ValueError("joined room/object snapshot scene/sequence mismatch")
    if int(room_doc.get("revision", -1)) != int(room_event.get("source_revision", -2)):
        raise ValueError("room event revision does not match serialized room snapshot")
    if (int(object_state["frame_index"]) != int(room_event["source_frame_index"])
            or int(room_event["source_frame_index"]) != int(object_event["source_frame_index"])):
        raise ValueError("joined room/object source frame index mismatch")
    if (str(object_state["frame_id"]) != str(room_event["source_frame_id"])
            or str(room_event["source_frame_id"]) != str(object_event["source_frame_id"])):
        raise ValueError("joined room/object source frame ID mismatch")
    if not math.isclose(float(object_state["timestamp_s"]), float(room_event["source_timestamp_s"]), abs_tol=1e-9, rel_tol=0):
        raise ValueError("joined room/object timestamps mismatch")
    if room_doc.get("timestamp_s") is not None and not math.isclose(
            float(object_state["timestamp_s"]), float(room_doc["timestamp_s"]), abs_tol=1e-9, rel_tol=0):
        raise ValueError("joined room snapshot timestamp does not match source frame")
    graph_objects = [{
        "hypothesis_id": item["hypothesis_id"], "snapshot_hypothesis_id": item["hypothesis_id"],
        "causal_object_id": item["hypothesis_id"],
        "prefix_admission": {"status": "unavailable", "eligible": None,
                              "reason": "reference mapper exposes only final min-detection filtering"},
        "semantic_consensus": {"status": "unavailable", "reason": "not computed in D1"},
        "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope"},
    } for item in object_state.get("objects", [])]
    rooms = [{"node_id": n["node_id"], "semantic_by_variant": {},
              "semantic_status": "unavailable", "semantic_reason": "unavailable_membership_scope"}
             for n in room_doc.get("nodes", []) if n.get("kind") == "room"]
    room_node_ids = {row["node_id"] for row in rooms}
    hybrid_node_ids = {str(row["causal_object_id"]) for row in graph_objects}
    room_pipeline_nodes = [{
        "node_id": str(node["node_id"]), "node_type": str(node.get("kind", "unknown")),
        "source_branch": "rooms", "source_provenance": node.get("provenance"),
        "attributes": node.get("attributes", {}),
    } for node in room_doc.get("nodes", [])
      if node.get("node_id") not in room_node_ids and node.get("node_id") not in hybrid_node_ids]
    graph_node_ids = room_node_ids | hybrid_node_ids | {row["node_id"] for row in room_pipeline_nodes}
    dangling_edges = [edge for edge in room_doc.get("edges", [])
                      if edge.get("source_id") not in graph_node_ids
                      or edge.get("target_id") not in graph_node_ids]
    if dangling_edges:
        raise ValueError(f"room revision contains edges without source nodes: {dangling_edges[:3]}")
    graph = {
        "schema": "d1-unified-graph-revision/v1", "graph_revision_id": f"unified-{revision:06d}",
        "scene_id": object_state["scene_id"], "sequence_id": object_state["sequence_id"],
        "frame_index": int(object_state["frame_index"]), "frame_id": object_state["frame_id"],
        "timestamp_s": float(object_state["timestamp_s"]), "room_revision": int(room_doc["revision"]),
        "object_revision": int(object_state["revision"]),
        "objects": graph_objects, "rooms": rooms, "room_pipeline_nodes": room_pipeline_nodes,
        "edges": list(room_doc.get("edges", [])), "dangling_edge_count": 0,
        "room_contains_by_target_node_type": _count_room_contains(room_state),
        "membership_availability": {"status": "unavailable", "reason": "unavailable_elevation_scope"},
        "room_semantics_availability": {"status": "unavailable", "reason": "unavailable_membership_scope"},
        "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference",
                      "membership": "unavailable_elevation_scope", "room_semantics": "unavailable_membership_scope"},
        "stage_semantics": {"online_hypotheses": "available", "admission": "not_exposed_by_reference_mapper",
                            "final_postprocess": "separate_terminal_stage_after_all_frames"},
        "source_revisions": {"object": {"branch": "objects", "event_seq": object_event["event_seq"],
                                          "source_revision": object_event["source_revision"],
                                          "event_sha256": object_event["event_sha256"],
                                          "state_sha256": object_event["state_sha256"]},
                             "room": {"branch": "rooms", "event_seq": room_event["event_seq"],
                                      "source_revision": room_event["source_revision"],
                                      "event_sha256": room_event["event_sha256"],
                                      "state_sha256": room_event["state_sha256"]}},
    }
    target = viewer.parent / "joined" / "graph_states" / f"rev-{revision:06d}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, graph)
    return target, graph


def _prepare_viewer(source: Path, run_root: Path, scene_id: str, sequence_id: str, *,
                    evidence_mode: str = "source_cache", run_id: str | None = None) -> tuple[Path, dict[str, str]]:
    viewer = run_root / "viewer"
    viewer.mkdir(parents=True, exist_ok=False)
    (viewer / "geometry").mkdir()
    from shutil import copy2
    copy2(ROOT / "scripts/causal_timeline_viewer.html", viewer / "index.html")
    if evidence_mode == "source_cache":
        evidence_name, hashes = _copy_evidence(source / "mapping", viewer)
    elif evidence_mode == "camera_live_current_run_only":
        # A new live run must never make panels from the reused C2 cache look
        # like observations captured by this run. Current-run evidence is
        # attached to each immutable frame before live_index publication.
        evidence_root = viewer / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=False)
        evidence_name = "evidence_index.json"
        evidence_index = viewer / evidence_name
        _write_json(evidence_index, {"schema": "camera-live-current-run-evidence/v1",
            "run_id": run_id or run_root.name, "source": "current_run_frames_only", "observations": {}})
        hashes = {evidence_name: _sha(evidence_index)}
    else:
        raise ValueError(f"unsupported viewer evidence mode: {evidence_mode!r}")
    initial_index = {
        "schema": "d1-live-timeline-index/v1", "timeline_manifest": {
            "scene_id": scene_id, "sequence_id": sequence_id, "run_id": run_id or run_root.name,
            "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference",
                          "membership": "unavailable_elevation_scope", "room_semantics": "unavailable_membership_scope"}},
        "run_id": run_id or run_root.name,
        "source_timeline": "live joined branch events", "frames": [],
        "evidence_index": evidence_name, "evidence_source_mode": evidence_mode,
        "world_bounds_xy": [-5, 5, -5, 5],
        "live_mode": True, "status": "starting", "final_postprocess": None,
        "derived_geometry_policy": {"max_points_per_object": MAX_DISPLAY_POINTS,
                                     "source_npz_unchanged": True, "cache_policy": "viewer LRU 6"},
    }
    _write_json(viewer / "live_index.json", initial_index)
    _write_json(viewer / "live_head.json", {"schema": "d1-live-head/v1", "status": "starting",
        "revision_count": 0, "scene_id": scene_id, "sequence_id": sequence_id,
        "live_index_sha256": _sha(viewer / "live_index.json"), "updated_utc": _utc(),
        "final_postprocess_available": False})
    hashes["index.html"] = _sha(viewer / "index.html")
    hashes["live_index.json"] = _sha(viewer / "live_index.json")
    hashes["live_head.json"] = _sha(viewer / "live_head.json")
    return viewer, hashes


def _launch_server(viewer: Path, root: Path, port: int, log_path: Path,
                   *, entrypoint: str | None = None) -> tuple[subprocess.Popen, str]:
    if not 1024 <= int(port) <= 65535:
        raise ValueError("viewer port must be 1024..65535")
    host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(f"localhost port {port} is busy; choose another; existing server left untouched") from exc
    viewer_rel = viewer.relative_to(root).as_posix()
    if entrypoint is None and (viewer / "d2_viewer_entrypoint.json").is_file():
        from scripts.build_d2_xyz_viewer import validate_entrypoint_bundle
        entrypoint = validate_entrypoint_bundle(viewer)["pointer"]["entrypoint"]
    entrypoint = entrypoint or "index.html"
    if Path(entrypoint).name != entrypoint:
        raise ValueError("viewer entrypoint must be a filename under viewer/")
    url = f"http://{host}:{port}/{viewer_rel}/{entrypoint}?live=1"
    log = log_path.open("ab")
    proc = subprocess.Popen([sys.executable, "-B", str(ROOT / "scripts/serve_causal_timeline_viewer.py"),
                             "--root", str(root), "--viewer", viewer_rel, "--host", host, "--port", str(port)],
                            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    deadline = time.monotonic() + 8.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(f"viewer server exited early ({proc.returncode}); see {log_path}")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                body = response.read()
                if response.status == 200 and b"<!doctype html" in body[:256].lower():
                    if (viewer / "d2_viewer_entrypoint.json").is_file():
                        from scripts.build_d2_xyz_viewer import validate_served_entrypoint
                        try:
                            validate_served_entrypoint(viewer, url)
                        except BaseException:
                            _stop_owned(proc)
                            log.close()
                            raise
                    return proc, url
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
    except Exception:
        pass
    log.close()
    raise RuntimeError(f"viewer server did not become ready: {url}; last_error={last_error}")


def _existing_loopback_viewer_url(base_url: str, viewer: Path, root: Path) -> tuple[str, int]:
    """Build a URL for an already running local workspace server; never starts/replaces it."""
    parsed = urlsplit(base_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port is None):
        raise ValueError("--viewer-base-url must be a plain http loopback origin with an explicit port")
    if not 1024 <= parsed.port <= 65535:
        raise ValueError("existing viewer server port must be 1024..65535")
    viewer_rel = viewer.resolve().relative_to(root.resolve()).as_posix()
    entrypoint = "index.html"
    if (viewer / "d2_viewer_entrypoint.json").is_file():
        from scripts.build_d2_xyz_viewer import validate_entrypoint_bundle
        entrypoint = validate_entrypoint_bundle(viewer)["pointer"]["entrypoint"]
    url = f"{base_url.rstrip('/')}/{viewer_rel}/{entrypoint}?live=1"
    return url, parsed.port


def _write_live_index(run_root: Path, viewer: Path, base: dict[str, Any], frames: list[dict[str, Any]],
                      status: str, final_data: dict[str, Any] | None = None) -> None:
    document = {**base, "frames": frames, "status": status,
                "final_postprocess": final_data}
    _write_json(viewer / "live_index.json", document)
    _write_json(viewer / "live_head.json", {
        "schema": "d1-live-head/v1", "status": status, "revision_count": len(frames),
        "live_index_sha256": _sha(viewer / "live_index.json"), "updated_utc": _utc(),
        "final_postprocess_available": final_data is not None,
    })


def _join_one(run_root: Path, viewer: Path, base_index: dict[str, Any], frames: list[dict[str, Any]],
              object_event: dict[str, Any], room_event: dict[str, Any], *,
              publish_index: bool = True, publish_event: bool = True,
              acknowledge: bool = True) -> dict[str, Any]:
    index = len(frames) + 1
    if int(object_event["source_frame_index"]) != int(room_event["source_frame_index"]):
        raise ValueError("coordinator cannot join mismatched room/object source frame indices")
    object_path = _verify_event(run_root, object_event, "objects")
    room_path = _verify_event(run_root, room_event, "rooms")
    object_state = _read(object_path)
    room_state = _read(room_path)
    geometry_path, extents = _sample_geometry(run_root, object_path, viewer)
    graph_path, graph = _make_graph_revision(run_root, viewer, index, object_state, room_state,
                                             object_event, room_event)
    frame_index = int(object_state["frame_index"])
    room_doc = room_state.get("snapshot", room_state)
    outer = [pt for node in room_doc.get("nodes", []) if node.get("kind") == "room"
             for pt in node.get("attributes", {}).get("outer_ring", [])]
    all_xy = extents["points"] + [(float(pt[0]), float(pt[1])) for pt in outer]
    if all_xy:
        bounds = [min(p[0] for p in all_xy), max(p[0] for p in all_xy),
                  min(p[1] for p in all_xy), max(p[1] for p in all_xy)]
        px, py = max((bounds[1] - bounds[0]) * .08, .3), max((bounds[3] - bounds[2]) * .08, .3)
        bounds = [bounds[0] - px, bounds[1] + px, bounds[2] - py, bounds[3] + py]
    else:
        bounds = base_index.get("world_bounds_xy", [-5, 5, -5, 5])
    joined = {
        "schema": "d1-joined-revision-event/v1", "joined_revision": index,
        "source_frame_index": frame_index, "source_frame_id": object_state["frame_id"],
        "timestamp_s": float(object_state["timestamp_s"]),
        "object_revision": {"source_revision": int(object_state["revision"]),
                            "state": str(object_path.relative_to(run_root)),
                            "state_sha256": object_event["state_sha256"],
                            "event_sha256": object_event["event_sha256"]},
        "room_revision": {"source_revision": int(room_event["source_revision"]),
                           "state": str(room_path.relative_to(run_root)),
                           "state_sha256": room_event["state_sha256"],
                           "event_sha256": room_event["event_sha256"]},
        "unified_graph_state": str(graph_path.relative_to(run_root)),
        "unified_graph_state_sha256": _sha(graph_path),
        "object_snapshot": _viewer_artifact_url(run_root, viewer, object_path),
        "room_snapshot": _viewer_artifact_url(run_root, viewer, room_path),
        "geometry": _viewer_artifact_url(run_root, viewer, geometry_path),
        "camera_pose_W_Copt": object_state.get("camera_pose_W_Copt"),
        "world_bounds_xy": bounds, "room_contains_by_target_node_type": graph["room_contains_by_target_node_type"],
        "future_observations_used": False,
    }
    out = run_root / "joined/revisions" / f"rev-{index:06d}.json"
    _write_json(out, joined)
    event = {"schema": "d1-joined-event/v1", "event_seq": index,
             "joined_revision": index, "source_frame_index": frame_index,
             "source_frame_id": object_state["frame_id"], "timestamp_s": object_state["timestamp_s"],
             "joined_state": str(out.relative_to(run_root)), "joined_state_sha256": _sha(out),
             "object_event_sha256": object_event["event_sha256"],
             "room_event_sha256": room_event["event_sha256"],
             "coordinator_publish_unix_s": time.time()}
    if publish_event:
        append_jsonl(run_root / "joined/events.jsonl", event)
    frame = {"revision": index, "frame_index": frame_index,
             "frame_id": object_state["frame_id"], "timestamp_s": float(object_state["timestamp_s"]),
             "snapshot_url": joined["object_snapshot"], "snapshot_sha256": object_event["state_sha256"],
             "room_url": joined["room_snapshot"], "geometry_url": joined["geometry"],
             "unified_url": _viewer_artifact_url(run_root, viewer, graph_path),
             "unified_sha256": joined["unified_graph_state_sha256"],
             "camera_pose_W_Copt": joined["camera_pose_W_Copt"],
             "world_bounds_xy": bounds, "object_source_revision": int(object_state["revision"]),
             "room_source_revision": int(room_event["source_revision"]),
             "joined_state_url": _viewer_artifact_url(run_root, viewer, out),
             "joined_state_sha256": _sha(out),
             "source_hashes": dict(object_event.get("source_hashes", {}))}
    frames.append(frame)
    if publish_index:
        _write_live_index(run_root, viewer, base_index, frames, "running")
    if acknowledge:
        for branch, source_event in (("objects", object_event), ("rooms", room_event)):
            ack_path = run_root / "branches" / branch / "acks" / f"{int(source_event['event_seq']):06d}.json"
            _write_json(ack_path, {"schema": "d1-revision-ack/v1", "event_sha256": source_event["event_sha256"],
                                   "joined_revision": index, "joined_state_sha256": _sha(out), "durable": True})
    return event


def _publish_joined_semantic_commit(run_root: Path, viewer: Path, base_index: dict[str, Any],
                                    frames: list[dict[str, Any]], joined_event: dict[str, Any],
                                    object_event: dict[str, Any], room_event: dict[str, Any]) -> None:
    """Publish one graph+semantic pair through the D2 index, then release both branches.

    Immutable graph and overlay files must already exist and match their hashes.
    ``live_index.json`` is the viewer's atomic publication pointer; until its
    verified head is visible, the browser retains the previous complete bundle.
    Mapper ACKs are deliberately last so neither branch can advance early.
    """
    if not frames:
        raise ValueError("cannot publish an empty unified frame list")
    frame = frames[-1]
    for field in ("unified_url", "unified_sha256", "room_belief_overlay_url", "room_belief_overlay_sha256",
                  "joined_state_url", "joined_state_sha256"):
        if not frame.get(field):
            raise ValueError(f"semantic unified frame is missing publication field {field}")
    if (int(joined_event.get("joined_revision", -1)) != int(frame["revision"])
            or joined_event.get("source_frame_id") != frame.get("frame_id")
            or int(joined_event.get("source_frame_index", -1)) != int(frame.get("frame_index", -2))):
        raise ValueError("joined event does not identify the frame being published")
    if object_event.get("source_frame_id") != frame.get("frame_id") or room_event.get("source_frame_id") != frame.get("frame_id"):
        raise ValueError("both branch events must identify the published source frame")
    if object_event.get("source_hashes") != room_event.get("source_hashes"):
        raise ValueError("object and room branch events have different source hashes")

    # Frame *_url fields are browser URLs relative to ``viewer``. They are
    # resolved exactly once against that base; event state_path values above
    # are separate run-root-relative filesystem references.
    graph_path = _resolve_viewer_artifact_url(run_root, viewer, frame["unified_url"], label="unified graph URL")
    overlay_path = _resolve_viewer_artifact_url(run_root, viewer, frame["room_belief_overlay_url"], label="semantic overlay URL")
    joined_path = _resolve_viewer_artifact_url(run_root, viewer, frame["joined_state_url"], label="joined revision URL")
    for candidate, expected, label in ((graph_path, frame["unified_sha256"], "unified graph"),
                                       (overlay_path, frame["room_belief_overlay_sha256"], "semantic overlay"),
                                       (joined_path, frame["joined_state_sha256"], "joined revision")):
        if _sha(candidate) != expected:
            raise ValueError(f"{label} is missing, outside the run or hash-mismatched before publication")
    overlay = _read(overlay_path)
    graph = _read(graph_path)
    if (overlay.get("graph_sha256") != frame["unified_sha256"]
            or overlay.get("graph_revision_id") != graph.get("graph_revision_id")
            or overlay.get("frame_id") != frame.get("frame_id")
            or int(overlay.get("frame_index", -1)) != int(frame.get("frame_index", -2))
            or int(overlay.get("unified_revision", -1)) != int(frame["revision"])
            or int(overlay.get("object_revision", -1)) != int(frame.get("object_source_revision", -2))
            or int(overlay.get("room_revision", -1)) != int(frame.get("room_source_revision", -2))
            or overlay.get("scene_id") != graph.get("scene_id")
            or overlay.get("sequence_id") != graph.get("sequence_id")):
        raise ValueError("semantic overlay does not bind the exact unified graph revision")
    for source_event in (object_event, room_event):
        if any(overlay.get("source_hashes", {}).get(key) != value
               for key, value in source_event.get("source_hashes", {}).items()):
            raise ValueError("semantic overlay does not preserve all object/room source hashes")
    if overlay.get("status") not in {"complete", "failed"}:
        raise ValueError("semantic overlay is not in a publishable terminal status")
    if overlay.get("status") == "failed" and (overlay.get("room_contains_edges")
            or any(row.get("committed_label") for variant in overlay.get("room_diagnostics", {}).values()
                   for row in variant.values())):
        raise ValueError("failed semantic overlay may not publish labels or membership")

    # Journals and immutable artifacts become durable before the index can
    # expose this revision. The viewer verifies live_head against live_index.
    append_jsonl(run_root / "joined/frames.jsonl", frame)
    append_jsonl(run_root / "joined/events.jsonl", joined_event)
    _write_live_index(run_root, viewer, base_index, frames, "running")
    for branch, source_event in (("objects", object_event), ("rooms", room_event)):
        ack_path = run_root / "branches" / branch / "acks" / f"{int(source_event['event_seq']):06d}.json"
        _write_json(ack_path, {"schema": "d1-revision-ack/v1", "event_sha256": source_event["event_sha256"],
                               "joined_revision": int(frame["revision"]),
                               "joined_state_sha256": frame["joined_state_sha256"], "durable": True})


def _finalize_postprocess(run_root: Path, viewer: Path, source: Path, frame: dict[str, Any]) -> dict[str, Any]:
    export_root = run_root / "object_mapping/export"
    export_manifest_path = export_root / "export_manifest.json"
    export_manifest = _read(export_manifest_path)
    mapping_contract = _read(run_root / "object_mapping/experiment_contract.json")
    lineage = _read(run_root / "object_mapping/lineage_stages.json")
    final_lineage = {frozenset(row.get("observation_ids", [])): row
                     for row in lineage.get("stages", {}).get("after_text_gated_merge", [])}
    objects: list[dict[str, Any]] = []
    geometry_objects: list[dict[str, Any]] = []
    for item in export_manifest.get("objects", []):
        cloud_path = export_root / item["cloud_npz"]
        expected = export_manifest.get("files_sha256", {}).get(item["cloud_npz"])
        if not cloud_path.is_file() or _sha(cloud_path) != expected:
            raise ValueError(f"final object cloud missing/hash mismatch: {cloud_path}")
        with np.load(cloud_path, allow_pickle=False) as archive:
            xyz = np.asarray(archive["xyz"], dtype=np.float32)
            rgb = np.asarray(archive["rgb"], dtype=np.uint8)
        if not len(xyz) or not np.isfinite(xyz).all():
            raise ValueError(f"invalid final postprocess cloud: {cloud_path}")
        if len(xyz) > MAX_DISPLAY_POINTS:
            ids = np.linspace(0, len(xyz) - 1, MAX_DISPLAY_POINTS, dtype=np.int64)
            xyz, rgb = xyz[ids], rgb[ids]
        oid = str(item["export_id"])
        evidence = list(item.get("evidence", []))
        lineage_row = final_lineage.get(frozenset(evidence), {})
        frames = sorted({int(value.split(":det-", 1)[0].rsplit("-", 1)[1]) for value in evidence})
        objects.append({"hypothesis_id": oid, "snapshot_hypothesis_id": oid,
                        "causal_object_id": oid, "id_semantics": "final_export_identity_scope_only",
                        "class_history_raw": item.get("reference_class_history", []),
                        "detection_count": int(item.get("observation_count", len(evidence))),
                        "distinct_frame_count": int(lineage_row.get("distinct_frame_count", len(frames))),
                        "distinct_pose_count": lineage_row.get("distinct_pose_count"),
                        "observation_ids": evidence, "stage_status": "final_retained",
                        "point_count": int(item.get("point_count", len(xyz)))})
        geometry_objects.append({"hypothesis_id": oid, "points": xyz.astype(float).tolist(),
                                 "colors": rgb.astype(int).tolist(), "sampled_points": len(xyz),
                                 "source_points": int(item.get("point_count", len(xyz))),
                                 "source_npz": str(cloud_path.relative_to(run_root)),
                                 "source_sha256": expected})
    room_snapshot_path = run_root / "room_replay/snapshot.json"
    room_state = _read(room_snapshot_path)
    room_doc = room_state.get("snapshot", room_state)
    final_geometry = viewer / "final_geometry.json"
    _write_json(final_geometry, {"schema": "d1-final-derived-geometry/v1", "objects": geometry_objects,
                                 "source_export_manifest_sha256": _sha(export_manifest_path)})
    final_snapshot = viewer / "final_objects.json"
    _write_json(final_snapshot, {"schema": "d1-final-postprocess-objects/v1",
                                 "scene_id": room_doc["scene_id"], "sequence_id": room_doc["sequence_id"],
                                 "revision": frame["revision"], "frame_index": frame["frame_index"],
                                 "frame_id": frame["frame_id"], "timestamp_s": frame["timestamp_s"],
                                 "stage": "final_postprocess", "objects": objects,
                                 "future_observations_used": False})
    room_contains = _count_room_contains(room_state)
    final_graph = viewer / "final_unified.json"
    room_nodes = [{"node_id": n["node_id"], "semantic_by_variant": {},
                   "semantic_status": "unavailable", "semantic_reason": "unavailable_membership_scope"}
                  for n in room_doc.get("nodes", []) if n.get("kind") == "room"]
    _write_json(final_graph, {"schema": "d1-final-postprocess-graph/v1",
        "graph_revision_id": "terminal-final-postprocess", "scene_id": room_doc["scene_id"],
        "sequence_id": room_doc["sequence_id"], "frame_index": frame["frame_index"],
        "timestamp_s": frame["timestamp_s"], "objects": [
            {"hypothesis_id": o["hypothesis_id"], "snapshot_hypothesis_id": o["hypothesis_id"],
             "causal_object_id": o["hypothesis_id"], "prefix_admission": {"status": "final_retained"},
             "semantic_consensus": {"status": "unavailable", "reason": "not computed in D1"},
             "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope"}}
            for o in objects], "rooms": room_nodes, "edges": list(room_doc.get("edges", [])),
        "room_contains_by_target_node_type": room_contains,
        "membership_availability": {"status": "unavailable", "reason": "unavailable_elevation_scope"},
        "room_semantics_availability": {"status": "unavailable", "reason": "unavailable_membership_scope"},
        "causality": {"objects": "terminal_final_postprocess_only", "rooms": "terminal_native_replay_state",
                      "membership": "unavailable_elevation_scope", "room_semantics": "unavailable_membership_scope"},
        "stage_semantics": {"online_hypotheses": "separate_prefix_snapshots",
                            "admission": "not_exposed_by_reference_mapper",
                            "final_postprocess": "terminal_separate_stage"}})
    result = {"status": "available", "stage": "final_postprocess_terminal_only",
              "snapshot_url": final_snapshot.name, "snapshot_sha256": _sha(final_snapshot),
              "geometry_url": final_geometry.name, "geometry_sha256": _sha(final_geometry),
              "unified_url": final_graph.name, "unified_sha256": _sha(final_graph),
              "room_url": Path(os.path.relpath(room_snapshot_path, viewer)).as_posix(),
              "room_sha256": _sha(room_snapshot_path),
              "export_manifest_sha256": _sha(export_manifest_path),
              "effective_config_sha256": _sha(run_root / "object_mapping/experiment_contract.json"),
              "final_object_count": len(objects), "room_contains_by_target_node_type": room_contains,
              "prefix_admission": "not exposed; not inferred from final filter"}
    return result


def finalize_completed_cached_run(run_id: str) -> dict[str, Any]:
    """Finish viewer-only terminal artifacts after a validated producer run.

    This deliberately refuses to resume either producer. It exists to recover
    a completed D1's terminal viewer serialization if that final step failed.
    """
    if Path(run_id).name != run_id or not run_id.startswith("d1_"):
        raise ValueError("invalid D1 run ID")
    run_root = (D_ROOT / run_id).resolve()
    if not run_root.is_relative_to(D_ROOT.resolve()):
        raise ValueError("D1 run path escapes live-processing output root")
    manifest_path = run_root / "run_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    run_doc = _read(manifest_path)
    if run_doc.get("status") != "failed" or "not in the subpath of" not in str(run_doc.get("error", "")):
        raise ValueError("finalize-only recovery is limited to the known terminal viewer path failure")
    source_id = str(run_doc.get("source_run_id", ""))
    source, source_manifest, _ = _verify_source_run(source_id)
    if _sha(source / "c2_run_manifest.json") != run_doc.get("input_sha256", {}).get("c2_run_manifest"):
        raise ValueError("C2 source manifest changed since the D1 run")
    frame_count = int(source_manifest.get("summary", {}).get("frame_count", 0))
    mapping_doc = _read(run_root / "object_mapping/run_manifest.json")
    room_doc = _read(run_root / "room_replay/manifest.json")
    if mapping_doc.get("status") != "pass" or int(mapping_doc.get("frames", -1)) != frame_count:
        raise ValueError("ConceptGraphs mapping output manifest does not prove all C2 frames completed")
    if int(room_doc.get("frames", -1)) != frame_count or room_doc.get("delta_replay_equal") is not True:
        raise ValueError("OccuSG room output manifest does not prove complete prefix output")
    integrity = room_doc.get("graph_integrity", {})
    if any(int(integrity.get(key, -1)) != 0 for key in
           ("conflicting_room_owner_target_count", "dangling_edge_count", "self_edge_count")):
        raise ValueError("OccuSG output graph-integrity checks are missing or failed")
    events, _ = read_complete_jsonl(run_root / "joined/events.jsonl")
    frames = _read(run_root / "viewer/live_index.json").get("frames", [])
    if len(events) != frame_count or len(frames) != frame_count or int(run_doc.get("partial_frame_count", -1)) != frame_count:
        raise ValueError("joined revisions are incomplete; finalize-only recovery will not resume producers")
    for expected, event in enumerate(events, start=1):
        if int(event.get("event_seq", -1)) != expected or int(event.get("source_frame_index", -1)) != expected - 1:
            raise ValueError("joined revision history is not contiguous")
        state_path = (run_root / str(event.get("joined_state", ""))).resolve()
        if not state_path.is_relative_to(run_root) or not state_path.is_file() or _sha(state_path) != event.get("joined_state_sha256"):
            raise ValueError("joined revision state hash failed during finalize-only recovery")
    recovery_started = time.perf_counter()
    viewer = run_root / "viewer"
    final_data = _finalize_postprocess(run_root, viewer, source, frames[-1])
    _write_live_index(run_root, viewer, _read(viewer / "live_index.json"), frames, "completed", final_data)
    original_error = str(run_doc.get("error"))
    run_doc["status"] = "completed"
    run_doc["error"] = None
    run_doc["failure_history"] = [{"status": "failed", "error": original_error,
                                  "stage": "terminal_finalization", "producer_replay_count": 1}]
    run_doc["recovery"] = {"status": "completed", "scope": "terminal viewer serialization only",
                            "mapping_rerun": False, "occusg_rerun": False,
                            "room_membership_or_semantics_recomputed": False,
                            "recovery_code_sha256": _sha(Path(__file__).resolve()),
                            "reason": "fixed relative room snapshot URL after all producer revisions were already written"}
    run_doc["recovery_runtime_s"] = time.perf_counter() - recovery_started
    run_doc["stages"]["hybrid_mapping"].update({"status": "completed", "validated_output_manifest": "object_mapping/run_manifest.json"})
    run_doc["stages"]["occusg_mapping"].update({"status": "completed", "validated_output_manifest": "room_replay/manifest.json"})
    run_doc["stages"]["coordinator"]["status"] = "completed"
    run_doc["stages"]["terminal_finalization"] = {"status": "completed_after_path_repair", "mapping_rerun": False,
                                                   "occusg_rerun": False}
    run_doc["viewer"]["status"] = "completed_using_existing_xy_only_server"
    run_doc["viewer"]["access_check"] = "verified_in_browser_GUI"
    run_doc["viewer"]["assets_sha256"]["index.html"] = _sha(viewer / "index.html")
    run_doc["summary"] = {"frame_count": frame_count,
                           "online_hypothesis_counts": [len(_read((run_root / "viewer") / row["snapshot_url"]).get("objects", [])) for row in frames],
                           "final_postprocess_objects": final_data["final_object_count"],
                           "room_contains_by_target_node_type": final_data["room_contains_by_target_node_type"],
                           "hybrid_object_membership_edges": 0}
    run_doc["outputs"] = {"object_mapping_manifest": str(run_root / "object_mapping/run_manifest.json"),
                          "room_replay_manifest": str(run_root / "room_replay/manifest.json"),
                          "object_timeline": str(run_root / "object_timeline"),
                          "joined_revision_index": str(run_root / "joined/events.jsonl"),
                          "viewer": str(viewer / "index.html"), "run_manifest": str(manifest_path)}
    run_doc["joined_event_count"] = frame_count
    run_doc["metrics"]["frame_count"] = frame_count
    run_doc["finished_utc"] = _utc()
    run_doc["output_sha256"] = {str(path.relative_to(run_root)): _sha(path)
                                for path in sorted(run_root.rglob("*"))
                                if path.is_file() and path != manifest_path}
    _write_json(manifest_path, run_doc)
    return run_doc


def _stop_owned(proc: subprocess.Popen | None, *, grace_s: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=2.0)
            except Exception:
                pass
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def _build_commands(source: Path, source_manifest: dict[str, Any], run_root: Path) -> tuple[list[str], list[str]]:
    mapping = list(source_manifest["stages"]["conceptgraphs_mapping"]["command"])
    rooms = list(source_manifest["stages"]["occusg_rooms"]["command"])
    for args, option, path in (
        (mapping, "--output", run_root / "object_mapping"),
        (mapping, "--timeline-out", run_root / "object_timeline"),
        (rooms, "--output", run_root / "room_replay"),
        (rooms, "--grid-output", run_root / "grid_trace"),
    ):
        args[:] = _replace_arg(args, option, str(path))
    mapping.extend(["--live-publish-root", str(run_root)])
    if "--no-determinism-check" not in rooms:
        rooms.append("--no-determinism-check")
    rooms.extend(["--live-publish-root", str(run_root)])
    if Path(_arg_value(mapping, "--cache")).resolve() != source:
        raise ValueError("locked C2 hybrid command cache path mismatch")
    if Path(_arg_value(rooms, "--capture")).resolve() != (source / "capture").resolve():
        raise ValueError("locked C2 OccuSG command capture path mismatch")
    return mapping, rooms


def run_live_cached(source_id: str, *, run_id: str, port: int = 8766,
                    viewer_base_url: str | None = None,
                    overall_timeout_s: float = 3600.0,
                    xyz_viewer_version: str | None = None,
                    viewer_ready_timeout_s: float = 600.0) -> dict[str, Any]:
    source, source_manifest, verified = _verify_source_run(source_id)
    if not run_id or Path(run_id).name != run_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in run_id):
        raise ValueError("invalid D1 run ID")
    run_root = (D_ROOT / run_id).resolve()
    if not run_root.is_relative_to(D_ROOT.resolve()):
        raise ValueError("D1 output path escapes live processing run root")
    if run_root.exists():
        raise FileExistsError(f"D1 output must be a new run directory: {run_root}")
    run_root.mkdir(parents=True, exist_ok=False)
    for folder in ("logs", "branches/objects/acks", "branches/rooms/acks", "control", "joined"):
        (run_root / folder).mkdir(parents=True, exist_ok=True)
    scene_id, sequence_id = source_manifest["scene_id"], source_manifest["sequence_id"]
    initial_manifest = {"schema": "scene-graph-d1-live-processing-run/v1", "run_id": run_id,
                       "status": "started", "execution_mode": "live_processing_from_cached_inputs",
                       "source_run_id": source_id, "scene_id": scene_id, "sequence_id": sequence_id,
                       "started_utc": _utc(), "preflight_stage": "preparing_viewer_and_commands",
                       "partial_artifacts_preserved": True, "resume_after_crash": False}
    _write_json(run_root / "run_manifest.json", initial_manifest)
    d2_bootstrap = None
    try:
        viewer, viewer_hashes = _prepare_viewer(source, run_root, scene_id, sequence_id)
        if xyz_viewer_version:
            from scripts.build_d2_xyz_viewer import bootstrap as bootstrap_d2_xyz
            d2_bootstrap = bootstrap_d2_xyz(run_root, xyz_viewer_version)
            from scripts.build_d2_xyz_viewer import validate_entrypoint_bundle
            validate_entrypoint_bundle(viewer)
            viewer_hashes["index.html"] = _sha(viewer / "index.html")
            viewer_hashes[d2_bootstrap["viewer_entrypoint"]] = _sha(viewer / d2_bootstrap["viewer_entrypoint"])
            viewer_hashes[f"xyz_renderer_d2_{xyz_viewer_version}.js"] = _sha(
                viewer / f"xyz_renderer_d2_{xyz_viewer_version}.js")
        base_index = _read(viewer / "live_index.json")
        mapping_command, room_command = _build_commands(source, source_manifest, run_root)
        locked_input_hashes = source_manifest.get("input_sha256", {})
        room_stage_files = {
            "resource_lock": Path(_arg_value(room_command, "--resource-manifest")),
            "native_pipeline_config": Path(_arg_value(room_command, "--pipeline-config")),
            "native_m2_m3": Path(_arg_value(room_command, "--m2-m3-binary")),
            "native_dude": Path(_arg_value(room_command, "--dude-binary")),
        }
        for key, path in room_stage_files.items():
            expected = locked_input_hashes.get(key)
            if not expected or not path.is_file() or _sha(path) != expected:
                raise ValueError(f"locked C2 room-stage input hash mismatch: {key} ({path})")
    except BaseException as exc:
        initial_manifest.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                                 "finished_utc": _utc(), "partial_artifacts": True})
        _write_json(run_root / "run_manifest.json", initial_manifest)
        _write_run_manifest_hash_receipt(run_root)
        raise
    stage_logs = {"objects": run_root / "logs/objects.log", "rooms": run_root / "logs/rooms.log"}
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "src"), env.get("PYTHONPATH", ""))).rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    mpl = run_root / "cache/matplotlib"
    mpl.mkdir(parents=True, exist_ok=True)
    env["MPLCONFIGDIR"] = str(mpl)
    source_path_hashes = {"c2_run_manifest": _sha(source / "c2_run_manifest.json"),
                          "c2_input_sha256": source_manifest.get("input_sha256", {}),
                          "c2_locked_input_file_sha256": verified["locked_output_hashes"],
                          **{key: value for key, value in verified["contract_hashes"].items()}}
    code_files = [ROOT / "scripts/run_conceptgraphs_cached_hybrid.py",
                  ROOT / "scripts/run_habitat_native_replay.py",
                  ROOT / "scripts/run_live_cached_processing.py",
                  ROOT / "scripts/scene_launcher.py",
                  ROOT / "scripts/build_d2_xyz_viewer.py",
                  ROOT / "src/standalone_sg/serialization/hybrid_timeline.py",
                  ROOT / "src/standalone_sg/serialization/live_revision_io.py",
                  Path(__file__).resolve(), ROOT / "scripts/causal_timeline_viewer.html"]
    code_hashes = {str(path.relative_to(ROOT)): _sha(path) for path in code_files}
    run_doc: dict[str, Any] = {
        "schema": "scene-graph-d1-live-processing-run/v1", "run_id": run_id,
        "status": "started", "execution_mode": "live_processing_from_cached_inputs",
        "claim_boundary": "lockstep cached C2 inputs; not fresh live capture, not realtime, not support for all scenes",
        "source_run_id": source_id, "scene_id": scene_id, "sequence_id": sequence_id,
        "started_utc": _utc(), "command": ["./run_scene.sh", "live-replay", "--source-run-id", source_id,
                                             "--run-id", run_id, "--port", str(port)],
        "input_paths": {"c2_run": str(source), "capture": str(source / "capture"),
                        "p2": str(source / "detection_2d/detection_2d_index.json"),
                        "p3": str(source / "detection_3d/detection_3d.jsonl"),
                        "image_features": str(source / "image_features"),
                        "text_features": str(source / "text_features"),
                        "room_snapshot_used_for_scene_contract_only": str(source / "replay/snapshot.json")},
        "input_sha256": source_path_hashes, "code_sha256": code_hashes,
        "effective_room_stage_file_sha256": {key: _sha(path) for key, path in room_stage_files.items()},
        "commands": {"objects": mapping_command, "rooms": room_command},
        "stages": {"c2_capture_perception": "reused_locked_inputs",
                   "hybrid_mapping": {"status": "pending"}, "occusg_mapping": {"status": "pending"},
                   "coordinator": {"status": "starting", "max_unmatched_events_per_branch": 1,
                                   "browser_required_for_progress": False},
                   "viewer": {"status": "starting", "browser_3d_orbit": "not_supported_xy_only"}},
        "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference",
                      "membership": "unavailable_elevation_scope", "room_semantics": "unavailable_membership_scope"},
        "prefix_admission": "not separately exposed by ConceptGraphs reference mapper",
        "final_postprocess": "separate terminal artifact; never backfilled into prefixes",
        "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope",
                       "room_semantics": "unavailable_membership_scope",
                       "room_contains_conceptgraphs_objects": 0},
        "viewer": {"status": "starting", "port": int(port), "host": "127.0.0.1",
                   "index": str((viewer / (d2_bootstrap["viewer_entrypoint"] if d2_bootstrap else "index.html")).relative_to(ROOT)), "url": None,
                   "server_pid": None, "server_mode": ("existing_read_only_loopback_server" if viewer_base_url else "run_owned_server"),
                   "assets_sha256": viewer_hashes,
                   "d2_xyz": ({"status": "bootstrap_published_before_producers",
                               "entrypoint": d2_bootstrap["viewer_entrypoint"],
                               "manifest": str((viewer / f"d2_xyz_manifest_{xyz_viewer_version}.json").relative_to(ROOT)),
                               "version": xyz_viewer_version,
                               "final_manifest_status": "completed_before_integrity_checkpoint"}
                              if d2_bootstrap else {"status": "not_requested"})},
        "viewer_start_gate": ({"status": "waiting_for_viewer_ready", "requires_explicit_confirmation": True,
                               "timeout_s": float(viewer_ready_timeout_s), "producer_started": False}
                              if d2_bootstrap else {"status": "not_required", "producer_started": False}),
        "metrics": {"processing_ms": {"objects": [], "rooms": []},
                    "barrier_wait_ms": {"objects": [], "rooms": []},
                    "publish_ms": {"objects": [], "rooms": []},
                    "coordinator_join_ms": [], "browser_receive_render_ms": "instrumented in viewer; not collected by runner"},
        "resume_after_crash": False, "partial_artifacts_preserved": True,
    }
    if viewer_base_url:
        run_doc["command"].extend(["--viewer-base-url", viewer_base_url])
    _write_json(run_root / "run_manifest.json", run_doc)
    server: subprocess.Popen | None = None
    processes: dict[str, subprocess.Popen] = {}
    logs = {}
    readers = {branch: EventLogReader(run_root / "branches" / branch / "events.jsonl")
               for branch in ("objects", "rooms")}
    pending: dict[str, dict[int, dict[str, Any]]] = {"objects": {}, "rooms": {}}
    timing_offsets = {"objects": 0, "rooms": 0}
    joined_events: list[dict[str, Any]] = []
    frames: list[dict[str, Any]] = []
    started = time.monotonic()
    status = "failed"
    error = None
    try:
        if viewer_base_url:
            url, external_port = _existing_loopback_viewer_url(viewer_base_url, viewer, ROOT)
            run_doc["viewer"].update({"status": "reusing_existing_loopback_server",
                                      "port": external_port, "server_pid": None,
                                      "server_owned_by_run": False, "url": url,
                                      "access_check": "must_be_confirmed_in_browser_GUI"})
            if d2_bootstrap:
                from scripts.build_d2_xyz_viewer import validate_served_entrypoint
                validate_served_entrypoint(viewer, url)
        else:
            server, url = _launch_server(viewer, ROOT, int(port), run_root / "logs/viewer_server.log",
                                         entrypoint=d2_bootstrap["viewer_entrypoint"] if d2_bootstrap else None)
            run_doc["viewer"].update({"status": "serving", "server_pid": server.pid,
                                      "server_owned_by_run": True, "url": url})
        run_doc["stages"]["viewer"]["status"] = "serving_xyz" if d2_bootstrap else "serving_xy_only"
        _write_json(run_root / "run_manifest.json", run_doc)
        if d2_bootstrap:
            pointer_path = viewer / "d2_viewer_entrypoint.json"
            pointer_sha = _sha(pointer_path)
            run_doc["status"] = "waiting_for_viewer_ready"
            run_doc["viewer_start_gate"].update({"status": "waiting_for_viewer_ready",
                                                  "entrypoint_pointer_sha256": pointer_sha,
                                                  "waiting_since_utc": _utc()})
            run_doc["viewer"]["status"] = "waiting_for_first_revision"
            _write_json(run_root / "run_manifest.json", run_doc)
            print(f"D1 start gate waiting_for_first_revision: {url}", flush=True)
            print(f"Open and visually check the page; then run: ./run_scene.sh start-live {run_id} --confirm-browser-ready --port {run_doc['viewer']['port']}", flush=True)
            start_record = _wait_for_explicit_viewer_start(run_root, run_id, pointer_sha,
                                                           float(viewer_ready_timeout_s))
            run_doc["viewer_start_gate"].update({"status": "released",
                                                  "released_utc": start_record.get("started_utc"),
                                                  "producer_started": True,
                                                  "confirmed_browser_ready": start_record.get("confirmed_browser_ready") is True,
                                                  "start_signal_sha256": _sha(run_root / "control/start.json")})
            run_doc["status"] = "running"
            run_doc["viewer"]["status"] = "serving_before_producer_start"
            _write_json(run_root / "run_manifest.json", run_doc)
            print("Viewer gate released; starting both producers. Browser is no longer a dependency.", flush=True)
        log_objects = stage_logs["objects"].open("ab", buffering=0)
        log_rooms = stage_logs["rooms"].open("ab", buffering=0)
        logs = {"objects": log_objects, "rooms": log_rooms}
        processes["objects"] = subprocess.Popen(mapping_command, cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=log_objects, stderr=subprocess.STDOUT, start_new_session=True)
        processes["rooms"] = subprocess.Popen(room_command, cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=log_rooms, stderr=subprocess.STDOUT, start_new_session=True)
        run_doc["stages"]["hybrid_mapping"] = {"status": "running", "pid": processes["objects"].pid,
                                                 "command": mapping_command, "started_utc": _utc(), "log": str(stage_logs["objects"].relative_to(run_root))}
        run_doc["stages"]["occusg_mapping"] = {"status": "running", "pid": processes["rooms"].pid,
                                                "command": room_command, "started_utc": _utc(), "log": str(stage_logs["rooms"].relative_to(run_root))}
        run_doc["stages"]["coordinator"]["status"] = "running"
        _write_json(run_root / "run_manifest.json", run_doc)
        print(f"D1 live viewer: {url}", flush=True)
        print(f"D1 run manifest: {run_root / 'run_manifest.json'}", flush=True)
        while True:
            if time.monotonic() - started > overall_timeout_s:
                raise TimeoutError(f"D1 overall timeout after {overall_timeout_s:.0f}s")
            for branch in ("objects", "rooms"):
                for row in readers[branch].poll():
                    frame_index = int(row["source_frame_index"])
                    if frame_index in pending[branch]:
                        existing = pending[branch][frame_index]
                        if existing.get("event_sha256") != row.get("event_sha256"):
                            raise ValueError(f"conflicting branch event for frame {frame_index}: {branch}")
                        continue
                    if row.get("scene_id") != scene_id or row.get("sequence_id") != sequence_id:
                        raise ValueError(f"{branch} event scene/sequence mismatch")
                    pending[branch][frame_index] = row
                    if "processing_ms" in row and row["processing_ms"] is not None:
                        run_doc["metrics"]["processing_ms"][branch].append(float(row["processing_ms"]))
                timing_path = run_root / "branches" / branch / "timings.jsonl"
                from standalone_sg.serialization.live_revision_io import read_complete_jsonl
                new_timings, new_offset = read_complete_jsonl(timing_path, timing_offsets[branch])
                if new_timings:
                    timing_offsets[branch] = new_offset
                    run_doc["metrics"]["publish_ms"][branch].extend(float(r["publication_ms"]) for r in new_timings)
                    run_doc["metrics"]["barrier_wait_ms"][branch].extend(float(r["barrier_wait_ms"]) for r in new_timings)
            common = sorted(set(pending["objects"]) & set(pending["rooms"]))
            for frame_index in common:
                expected_index = len(frames)
                if frame_index != expected_index:
                    raise ValueError(f"noncontiguous joined source frame index: expected {expected_index}, got {frame_index}")
                object_event = pending["objects"].pop(frame_index)
                room_event = pending["rooms"].pop(frame_index)
                join_started = time.perf_counter()
                joined = _join_one(run_root, viewer, base_index, frames, object_event, room_event)
                join_ms = (time.perf_counter() - join_started) * 1000.0
                joined["coordinator_join_ms"] = join_ms
                joined_events.append(joined)
                run_doc["metrics"]["coordinator_join_ms"].append(join_ms)
                run_doc["joined_revision_count"] = len(frames)
                run_doc["last_joined_frame_id"] = joined["source_frame_id"]
                _write_json(run_root / "run_manifest.json", run_doc)
                print(f"D1 joined {len(frames)}/{len(verified['capture']['frames'])}: {joined['source_frame_id']} | coordinator={join_ms:.1f}ms", flush=True)
            proc_states = {name: proc.poll() for name, proc in processes.items()}
            failed = {name: code for name, code in proc_states.items() if code not in (None, 0)}
            if failed:
                raise RuntimeError(f"D1 producer failed: {failed}")
            if all(code == 0 for code in proc_states.values()):
                if pending["objects"] or pending["rooms"]:
                    raise RuntimeError("producer exited with unmatched source revisions")
                if len(frames) != len(verified["capture"]["frames"]):
                    raise RuntimeError(f"D1 joined {len(frames)} of {len(verified['capture']['frames'])} frames")
                break
            time.sleep(0.025)
        final_data = _finalize_postprocess(run_root, viewer, source, frames[-1])
        _write_live_index(run_root, viewer, base_index, frames, "completed", final_data)
        status = "completed"
        run_doc["stages"]["hybrid_mapping"]["status"] = "completed"
        run_doc["stages"]["hybrid_mapping"]["exit_code"] = processes["objects"].returncode
        run_doc["stages"]["hybrid_mapping"]["finished_utc"] = _utc()
        run_doc["stages"]["occusg_mapping"]["status"] = "completed"
        run_doc["stages"]["occusg_mapping"]["exit_code"] = processes["rooms"].returncode
        run_doc["stages"]["occusg_mapping"]["finished_utc"] = _utc()
        run_doc["stages"]["coordinator"]["status"] = "completed"
        run_doc["stages"]["viewer"]["status"] = "completed_serving_xyz" if d2_bootstrap else "completed_serving_xy_only"
        run_doc["metrics"]["frame_count"] = len(frames)
        run_doc["outputs"] = {"object_mapping_manifest": str(run_root / "object_mapping/run_manifest.json"),
                               "room_replay_manifest": str(run_root / "room_replay/manifest.json"),
                               "object_timeline": str(run_root / "object_timeline"),
                               "joined_revision_index": str(run_root / "joined/events.jsonl"),
                               "viewer": str(viewer / (d2_bootstrap["viewer_entrypoint"] if d2_bootstrap else "index.html")),
                               "run_manifest": str(run_root / "run_manifest.json")}
        run_doc["summary"] = {"frame_count": len(frames),
                              "online_hypothesis_counts": [len(_read(viewer / row["snapshot_url"]).get("objects", [])) for row in frames],
                              "final_postprocess_objects": final_data["final_object_count"],
                              "room_contains_by_target_node_type": final_data["room_contains_by_target_node_type"],
                              "hybrid_object_membership_edges": 0}
    except KeyboardInterrupt:
        status = "cancelled"
        error = "KeyboardInterrupt; stop requested by operator"
        _write_json(run_root / "control/stop.json", {"status": "cancelled", "reason": error, "utc": _utc()})
        raise
    except BaseException as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        _write_json(run_root / "control/stop.json", {"status": "failed", "reason": error, "utc": _utc()})
        raise
    finally:
        for name, proc in processes.items():
            if proc.poll() is None:
                _stop_owned(proc)
            stage_key = "hybrid_mapping" if name == "objects" else "occusg_mapping"
            stage_record = run_doc.get("stages", {}).get(stage_key, {})
            if proc.poll() is not None:
                stage_record.setdefault("exit_code", proc.returncode)
                if proc.returncode == 0 and stage_record.get("status") == "running":
                    stage_record["status"] = "producer_exited_successfully; terminal_outputs_not_yet_validated"
        for log in logs.values():
            try:
                log.close()
            except Exception:
                pass
        if server is not None and status != "completed":
            _stop_owned(server)
        run_doc["status"] = status
        run_doc["error"] = error
        run_doc["finished_utc"] = _utc()
        run_doc["runtime_s"] = time.monotonic() - started
        run_doc["joined_event_count"] = len(joined_events)
        run_doc["partial_frame_count"] = len(frames)
        if run_doc["viewer"].get("server_mode") == "existing_read_only_loopback_server":
            run_doc["viewer"]["status"] = ("completed_using_existing_xyz_server" if d2_bootstrap else "completed_using_existing_xy_only_server") if status == "completed" else "external_server_left_untouched"
        else:
            run_doc["viewer"]["status"] = ("completed_serving_xyz" if d2_bootstrap else "completed_serving_xy_only") if status == "completed" else "server_stopped_after_failure"
        if status == "completed" and d2_bootstrap:
            try:
                # The D2 manifest and pointer are terminal immutable outputs and are finalized
                # before the D1 integrity checkpoint, so neither is silently excluded.
                _write_json(run_root / "run_manifest.json", run_doc)
                from scripts.build_d2_xyz_viewer import build as build_d2_xyz
                final_d2 = build_d2_xyz(run_root, version=xyz_viewer_version or "live")
                run_doc["viewer"]["d2_xyz"].update({"status": "completed",
                    "entrypoint": final_d2["viewer_entrypoint"], "manifest_status": final_d2["status"]})
            except BaseException as exc:
                status = "failed"
                error = f"D2 finalization failed: {type(exc).__name__}: {exc}"
                run_doc["error"] = error
                run_doc["stages"]["viewer"]["status"] = "failed_d2_finalization"
                _write_json(run_root / "control/stop.json", {"status": "failed", "reason": error, "utc": _utc()})
        if status == "completed":
            _seal_run_integrity(run_root, run_doc, server)
        _write_json(run_root / "run_manifest.json", run_doc)
        _write_run_manifest_hash_receipt(run_root)
    return run_doc
