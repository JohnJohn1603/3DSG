"""Run the existing unified-graph and timeline-viewer builders on pinned caches.

This module orchestrates immutable inputs only.  It does not run perception,
mapping, room geometry, membership, or room-belief inference.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from integration_v2.unified_incremental_timeline import replay_unified_snapshots


INPUT_SCHEMA = "scene-graph-cached-replay-inputs/v1"
RUN_SCHEMA = "scene-graph-cached-replay-run/v1"
POLICY_FIELDS = (
    "admission_policy", "category_cap", "membership_policy", "room_belief_policy",
    "semantic_policy", "variants", "a_table_digests", "a_score_field",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                    prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _resolve_input(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else config_path.parent / path).resolve()


def _safe_ref(root: Path, relative: str, label: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"{label} escapes its input root: {relative}")
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    return path


def _policy_projection(causal_manifest: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in POLICY_FIELDS if key not in causal_manifest]
    if missing:
        raise ValueError(f"causal manifest is missing policy/provenance fields: {missing}")
    return {key: causal_manifest[key] for key in POLICY_FIELDS}


def _coordinate_metadata(room_snapshot: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for node in room_snapshot.get("nodes", []):
        metadata = node.get("attributes", {}).get("source_metadata")
        if isinstance(metadata, dict) and metadata.get("canonical_world"):
            candidates.append(metadata)
    if not candidates:
        raise ValueError("room revision has no source_metadata.canonical_world coordinate provenance")
    canonical = {str(item.get("canonical_world")) for item in candidates}
    frames = {str(item.get("frame_id_coordinate")) for item in candidates}
    units = {str(item.get("depth_units")) for item in candidates}
    scales = {float(item.get("depth_scale_to_m", math.nan)) for item in candidates}
    if len(canonical) != 1 or len(frames) != 1 or len(units) != 1 or len(scales) != 1:
        raise ValueError("room revision contains conflicting coordinate provenance")
    return {"canonical_world": next(iter(canonical)), "frame_id_coordinate": next(iter(frames)),
            "depth_units": next(iter(units)), "depth_scale_to_m": next(iter(scales))}


def _check_coordinate_contract(config: dict[str, Any], capture: dict[str, Any],
                               causal_manifest: dict[str, Any], room_snapshot: dict[str, Any],
                               camera_pose: Any, frame_index: int) -> dict[str, Any]:
    declared = config.get("coordinate_contract")
    if not isinstance(declared, dict):
        raise ValueError("input config requires coordinate_contract")
    required = {"object_frame": "capture/world", "room_frame": "capture/world_xy",
                "units": "m", "up_axis": "Z"}
    for key, expected in required.items():
        if declared.get(key) != expected:
            raise ValueError(f"unsupported coordinate contract {key}: expected {expected!r}")

    canonical_world = capture.get("canonical_world")
    if not canonical_world or "Z-up" not in str(canonical_world):
        raise ValueError(f"capture coordinate transform is unavailable at frame {frame_index}")
    if declared.get("transform_convention") != canonical_world:
        raise ValueError(f"configured transform convention differs from capture provenance at frame {frame_index}")
    if capture.get("depth_units") != "m" or float(capture.get("depth_scale_to_m", 1.0)) != 1.0:
        raise ValueError(f"capture is not in metric units at frame {frame_index}")
    room_meta = _coordinate_metadata(room_snapshot)
    if (room_meta["canonical_world"] != canonical_world or room_meta["frame_id_coordinate"] != "world"
            or room_meta["depth_units"] != "m" or room_meta["depth_scale_to_m"] != 1.0):
        raise ValueError(f"room/capture coordinate transform or units differ at frame {frame_index}")

    scope_text = str(causal_manifest.get("membership_scope", {}).get("coordinate_scope", ""))
    normalized_scope = scope_text.lower()
    if not all(token in normalized_scope for token in ("capture/world", "meter", "z-up", "world xy")):
        raise ValueError("causal membership scope does not confirm the object/room coordinate frame and units")
    pose_status = "camera_to_world_transform_available"
    if camera_pose is None:
        pose_status = "camera_transform_omitted; room agent position provenance available"
        poses = [node.get("attributes", {}).get("pose_W") for node in room_snapshot.get("nodes", [])
                 if node.get("kind") == "agent"]
        if not poses:
            raise ValueError(f"camera pose and prefix room-agent pose are missing at frame {frame_index}")
        try:
            import numpy as np
            if not any(np.asarray(pose, dtype=np.float64).shape == (3,) and
                       np.isfinite(np.asarray(pose, dtype=np.float64)).all() for pose in poses):
                raise ValueError
        except (ImportError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid prefix room-agent pose at frame {frame_index}") from exc
    else:
        if not isinstance(camera_pose, list) or len(camera_pose) != 4 or any(
                not isinstance(row, list) or len(row) != 4 for row in camera_pose):
            raise ValueError(f"camera_pose_W_Copt must be a 4x4 transform at frame {frame_index}")
        try:
            import numpy as np
            matrix = np.asarray(camera_pose, dtype=np.float64)
            if not np.isfinite(matrix).all() or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-7):
                raise ValueError
        except (ImportError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid camera_pose_W_Copt transform at frame {frame_index}") from exc
    return {"status": "coordinate_compatible", "object_frame": declared["object_frame"],
            "room_frame": declared["room_frame"], "units": declared["units"],
            "up_axis": declared["up_axis"], "transform": canonical_world,
            "pose_provenance": pose_status,
            "membership_scope_status": causal_manifest.get("membership_scope", {}).get("status", "unavailable"),
            "membership_scope_reason": causal_manifest.get("membership_scope", {}).get("reason")}


def validate_inputs(config_path: Path, *, workspace_root: Path) -> dict[str, Any]:
    """Validate explicit input manifests and every referenced prefix asset."""
    config_path = Path(config_path).expanduser().resolve()
    config = read_json(config_path)
    if config.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"unsupported cached replay input config schema: {config.get('schema')!r}")
    expected = config.get("expected_identity", {})
    scene_id, sequence_id = expected.get("scene_id"), expected.get("sequence_id")
    if not scene_id or not sequence_id:
        raise ValueError("expected_identity must declare scene_id and sequence_id")
    inputs = config.get("inputs", {})
    for input_name in ("object_timeline", "causal_state"):
        if not isinstance(inputs.get(input_name), dict) or not inputs[input_name].get("root"):
            raise ValueError(f"input config must declare inputs.{input_name}.root")

    timeline = _resolve_input(inputs["object_timeline"]["root"], config_path)
    causal_root = _resolve_input(inputs["causal_state"]["root"], config_path)
    for name, root in (("object timeline", timeline), ("causal state", causal_root)):
        if not root.is_dir():
            raise FileNotFoundError(f"{name} root not found: {root}")
        if not root.is_relative_to(workspace_root.resolve()):
            raise ValueError(f"{name} must be under the workspace so the read-only viewer can serve its source assets")

    timeline_manifest_path = timeline / "manifest.json"
    causal_manifest_path = causal_root / "manifest.json"
    timeline_manifest_hash = sha256(timeline_manifest_path)
    causal_manifest_hash = sha256(causal_manifest_path)
    if timeline_manifest_hash != inputs["object_timeline"].get("manifest_sha256"):
        raise ValueError("object timeline manifest SHA-256 mismatch")
    if causal_manifest_hash != inputs["causal_state"].get("manifest_sha256"):
        raise ValueError("causal state manifest SHA-256 mismatch")
    tm, cm = read_json(timeline_manifest_path), read_json(causal_manifest_path)
    if tm.get("schema") != "conceptgraphs-causal-object-timeline/v1":
        raise ValueError(f"unsupported object timeline schema: {tm.get('schema')!r}")
    if cm.get("schema") != "causal-hybrid-room-belief-timeline/v1":
        raise ValueError(f"unsupported causal state schema: {cm.get('schema')!r}")
    if tm.get("status") != "complete" or cm.get("status") != "complete":
        raise ValueError("both cached input manifests must have status=complete")
    for label, manifest in (("object timeline", tm), ("causal state", cm)):
        if (manifest.get("scene_id"), manifest.get("sequence_id")) != (scene_id, sequence_id):
            raise ValueError(f"{label} scene/sequence does not match expected_identity")
    if (tm.get("scene_id"), tm.get("sequence_id")) != (cm.get("scene_id"), cm.get("sequence_id")):
        raise ValueError("object timeline and causal state scene/sequence differ")
    if cm.get("source_timeline_manifest_sha256") != timeline_manifest_hash:
        raise ValueError("causal state was not built from this exact object timeline manifest")
    if tm.get("causality", {}).get("objects") != "prefix_state_only":
        raise ValueError("object timeline causality is not prefix_state_only")
    if tm.get("causality", {}).get("rooms") != "prefix_state_only_native_replay_reference":
        raise ValueError("room revisions are not declared as prefix-state native replay references")

    policy = _policy_projection(cm)
    actual_policy_hash = canonical_hash(policy)
    if config.get("policy_fingerprint_sha256") != actual_policy_hash:
        raise ValueError("policy/config fingerprint mismatch; input policy versions changed")
    if not cm.get("implementation_provenance", {}).get("runner_sha256"):
        raise ValueError("causal input lacks semantic/controller implementation provenance")
    if not cm.get("variants") or len(cm["variants"]) != len(set(cm["variants"])):
        raise ValueError("causal input has missing or duplicate semantic variants")

    frame_index_path = _safe_ref(timeline, str(tm.get("snapshot_index", "frame_index.json")), "object frame index")
    frame_index = read_json(frame_index_path).get("frames")
    causal_rows = cm.get("revisions")
    if not isinstance(frame_index, list) or not frame_index or not isinstance(causal_rows, list):
        raise ValueError("object frame index and causal revisions must be non-empty lists")
    if int(tm.get("frame_count", -1)) != len(frame_index) or int(cm.get("frame_count", -1)) != len(causal_rows):
        raise ValueError("input manifest frame_count disagrees with its revision index")

    cache_root_value = tm.get("cache_root")
    if not cache_root_value:
        raise ValueError("object timeline provenance is missing cache_root")
    cache_root = Path(cache_root_value).expanduser().resolve()
    capture_manifest_path = cache_root / "capture/capture_manifest.json"
    if not capture_manifest_path.is_file():
        raise FileNotFoundError(f"capture provenance required by object timeline is missing: {capture_manifest_path}")
    capture_hash = sha256(capture_manifest_path)
    expected_capture_hash = tm.get("input_hashes", {}).get("capture_manifest_sha256")
    if not expected_capture_hash or capture_hash != expected_capture_hash:
        raise ValueError("capture manifest hash differs from object timeline provenance")
    capture = read_json(capture_manifest_path)
    if (capture.get("scene_id"), capture.get("sequence_id")) != (scene_id, sequence_id):
        raise ValueError("capture provenance scene/sequence differs from cached timelines")
    mapping_output_value = tm.get("mapping_output")
    if not mapping_output_value:
        raise ValueError("object timeline provenance is missing mapping_output used by the existing viewer builder")
    mapping_output = Path(mapping_output_value).expanduser().resolve()
    if not mapping_output.is_relative_to(workspace_root.resolve()):
        raise ValueError("mapping evidence assets must be under the workspace for localhost viewer access")
    evidence_manifest_path = mapping_output / "diagnostic/evidence_manifest.json"
    if not evidence_manifest_path.is_file():
        raise FileNotFoundError(f"viewer evidence manifest referenced by object timeline is missing: {evidence_manifest_path}")
    evidence_doc = read_json(evidence_manifest_path)

    causal_by_frame: dict[int, dict[str, Any]] = {}
    for row in causal_rows:
        frame = int(row["frame_index"])
        if frame in causal_by_frame:
            raise ValueError(f"duplicate causal frame index: {frame}")
        causal_by_frame[frame] = row
    frame_ids = [int(row["frame_index"]) for row in frame_index]
    if len(frame_ids) != len(set(frame_ids)) or set(frame_ids) != set(causal_by_frame):
        raise ValueError("object and causal frame populations differ or contain duplicate frame indices")

    by_cloud: dict[str, str] = {}
    input_files: dict[str, str] = {
        str(config_path): sha256(config_path), str(timeline_manifest_path): timeline_manifest_hash,
        str(frame_index_path): sha256(frame_index_path), str(causal_manifest_path): causal_manifest_hash,
        str(capture_manifest_path): capture_hash,
        str(evidence_manifest_path): sha256(evidence_manifest_path),
    }
    for observation_id, evidence_row in evidence_doc.get("observations", {}).items():
        for panel_kind, panel_ref in evidence_row.get("panels", {}).items():
            panel_path = _safe_ref(mapping_output / "diagnostic", str(panel_ref),
                                   f"viewer evidence panel {observation_id}/{panel_kind}")
            input_files[str(panel_path)] = sha256(panel_path)
    a_zip_value = cm.get("source_a_zip")
    a_zip_hash = cm.get("source_a_zip_sha256")
    if not a_zip_value or not a_zip_hash:
        raise ValueError("causal semantic output does not identify the source A-table archive and checksum")
    a_zip_path = Path(a_zip_value).expanduser().resolve()
    if not a_zip_path.is_file() or sha256(a_zip_path) != a_zip_hash:
        raise ValueError(f"source A-table archive missing or hash mismatch: {a_zip_path}")
    input_files[str(a_zip_path)] = a_zip_hash
    for name, path_key, hash_key in (("semantic runner", "runner_path", "runner_sha256"),
                                     ("RB1 ledger", "ledger_path", "ledger_sha256")):
        provenance = cm["implementation_provenance"]
        source_path = Path(provenance.get(path_key, "")).expanduser().resolve()
        expected_hash = provenance.get(hash_key)
        if not source_path.is_file() or not expected_hash or sha256(source_path) != expected_hash:
            raise ValueError(f"{name} provenance missing or hash mismatch: {source_path}")
        input_files[str(source_path)] = expected_hash
    room_alignment = []
    scope_statuses: dict[str, int] = {}
    coordinate_checks = []
    for row in sorted(frame_index, key=lambda x: int(x["frame_index"])):
        frame = int(row["frame_index"])
        if frame not in causal_by_frame:
            raise ValueError(f"causal state missing frame {frame}")
        snapshot_path = _safe_ref(timeline, str(row.get("snapshot", "")), f"object snapshot for frame {frame}")
        snapshot_hash = sha256(snapshot_path)
        if row.get("snapshot_sha256") != snapshot_hash:
            raise ValueError(f"object snapshot hash mismatch at frame {frame}")
        snapshot = read_json(snapshot_path)
        causal_row = causal_by_frame[frame]
        causal_path = _safe_ref(causal_root, str(causal_row.get("state", "")), f"causal revision for frame {frame}")
        causal_hash = sha256(causal_path)
        causal_state = read_json(causal_path)
        if (snapshot.get("frame_index"), snapshot.get("revision"), snapshot.get("frame_id")) != (
                frame, int(row.get("revision", -1)), row.get("frame_id")):
            raise ValueError(f"object frame index/snapshot alignment mismatch at frame {frame}")
        if (causal_state.get("frame_index"), causal_state.get("revision"), causal_state.get("frame_id")) != (
                frame, int(causal_row.get("revision", -1)), causal_row.get("frame_id")):
            raise ValueError(f"causal manifest/state alignment mismatch at frame {frame}")
        if (int(row["revision"]), row["frame_id"]) != (int(causal_row["revision"]), causal_row["frame_id"]):
            raise ValueError(f"object/causal revision or frame ID mismatch at frame {frame}")
        if snapshot.get("future_observations_used") is not False or causal_state.get("future_observations_used") is not False:
            raise ValueError(f"input prefix claims future observations at frame {frame}")
        timestamp = snapshot.get("timestamp_s")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(float(timestamp)):
            raise ValueError(f"object timestamp is missing/invalid at frame {frame}")
        if not math.isclose(float(timestamp), float(causal_state.get("timestamp_s", math.nan)), rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"object/causal timestamp mismatch at frame {frame}")

        room_ref = snapshot.get("rooms", {}).get("state_ref")
        if not room_ref:
            raise ValueError(f"room revision reference missing at frame {frame}")
        expected_room_ref = tm.get("room_revision_files", {}).get(str(frame))
        if expected_room_ref and expected_room_ref != room_ref:
            raise ValueError(f"room revision reference differs from object timeline manifest at frame {frame}")
        room_path = _safe_ref(timeline, str(room_ref), f"room revision at frame {frame}")
        room_doc = read_json(room_path)
        room_state = room_doc.get("snapshot", room_doc)
        if (room_state.get("scene_id"), room_state.get("sequence_id")) != (scene_id, sequence_id):
            raise ValueError(f"room revision scene/sequence mismatch at frame {frame}")
        if not math.isclose(float(room_state.get("timestamp_s", math.nan)), float(timestamp), rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"room/object timestamp mismatch at frame {frame}")
        room_agent_frames = {node.get("attributes", {}).get("frame_id") for node in room_state.get("nodes", [])
                             if node.get("kind") == "agent" and node.get("attributes", {}).get("frame_id")}
        if room_agent_frames and snapshot.get("frame_id") not in room_agent_frames:
            raise ValueError(f"room revision agent frame ID differs from object prefix at frame {frame}")
        # Object and room revision numbers intentionally need not match.  The
        # frame ID/timestamp/state_ref is the verified alignment relation.
        room_revision = room_state.get("revision")
        if room_revision is None:
            raise ValueError(f"room revision ID missing at frame {frame}")
        coordinate = _check_coordinate_contract(config, capture, cm, room_state,
                                                snapshot.get("camera_pose_W_Copt"), frame)
        coordinate_checks.append(coordinate)
        input_files[str(snapshot_path)] = snapshot_hash
        input_files[str(causal_path)] = causal_hash
        input_files[str(room_path)] = sha256(room_path)

        for obj in snapshot.get("objects", []):
            geometry_rel = obj.get("cloud_npz")
            expected_geometry_hash = obj.get("cloud_sha256")
            if not geometry_rel or not expected_geometry_hash:
                raise ValueError(f"object geometry reference/hash missing at frame {frame}: {obj.get('hypothesis_id')}")
            geometry_path = _safe_ref(timeline, str(geometry_rel), f"object geometry at frame {frame}")
            actual_geometry_hash = by_cloud.get(str(geometry_path))
            if actual_geometry_hash is None:
                actual_geometry_hash = sha256(geometry_path)
                by_cloud[str(geometry_path)] = actual_geometry_hash
                try:
                    import numpy as np
                    with np.load(geometry_path, allow_pickle=False) as archive:
                        xyz = np.asarray(archive["xyz"], dtype=np.float64)
                    if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
                        raise ValueError
                    if obj.get("bounds_minmax"):
                        bounds = np.asarray(obj["bounds_minmax"], dtype=np.float64)
                        if bounds.shape != (2, 3) or not np.allclose(bounds[0], xyz.min(axis=0), atol=1e-5) or not np.allclose(bounds[1], xyz.max(axis=0), atol=1e-5):
                            raise ValueError
                except (ImportError, KeyError, ValueError, OSError) as exc:
                    raise ValueError(f"invalid object cloud xyz/bounds at frame {frame}: {geometry_path}") from exc
            if actual_geometry_hash != expected_geometry_hash:
                raise ValueError(f"object geometry hash mismatch at frame {frame}: {geometry_path}")
            input_files[str(geometry_path)] = actual_geometry_hash

        scope = causal_state.get("membership_scope", {})
        scope_status = str(scope.get("status", "unavailable"))
        if not scope_status:
            scope_status = "unavailable"
        scope_statuses[scope_status] = scope_statuses.get(scope_status, 0) + 1
        room_alignment.append({"frame_index": frame, "frame_id": row["frame_id"],
                               "timestamp_s": float(timestamp), "object_revision": int(row["revision"]),
                               "causal_revision": int(causal_row["revision"]),
                               "room_revision": room_revision, "room_state_ref": str(room_ref),
                               "room_revision_sha256": sha256(room_path),
                               "membership_scope_status": scope_status})

    if len(frame_index) != len(causal_rows):
        raise ValueError("object and causal revision counts differ")
    if config.get("frame_alignment") != "frame_index+frame_id+timestamp_s; room revision via object snapshot state_ref":
        raise ValueError("input config does not pin the supported frame/revision alignment policy")
    return {
        "config": config, "config_path": config_path, "timeline": timeline, "causal_state": causal_root,
        "timeline_manifest": tm, "causal_manifest": cm, "frame_index": frame_index,
        "room_alignment": room_alignment, "input_files": input_files, "policy_projection": policy,
        "policy_fingerprint_sha256": actual_policy_hash, "membership_scope_status_counts": scope_statuses,
        "coordinate_contract_status": "compatible", "coordinate_contract": coordinate_checks[0],
        "coordinate_provenance_by_frame": coordinate_checks,
        "capture_manifest_path": capture_manifest_path, "mapping_output": mapping_output,
    }


def _build_unified(timeline: Path, causal: Path, output: Path) -> dict[str, Any]:
    from integration_v2.unified_incremental_timeline import build_unified_timeline
    return build_unified_timeline(timeline, causal, output)


def _build_viewer(timeline: Path, viewer_output: Path, max_points: int, unified: Path) -> dict[str, Any]:
    from scripts.build_causal_timeline_viewer import build
    return build(timeline, viewer_output, max_points=max_points, unified_timeline=unified)


def _output_hashes(root: Path) -> list[dict[str, Any]]:
    return [{"path": path.relative_to(root).as_posix(), "sha256": sha256(path),
             "size_bytes": path.stat().st_size}
            for path in sorted(root.rglob("*")) if path.is_file() and path.name != "run_manifest.json"]


def _validate_outputs(output: Path, validated: dict[str, Any], max_points: int) -> dict[str, Any]:
    graph_root, viewer_root = output / "unified", output / "viewer"
    graph_manifest = read_json(graph_root / "manifest.json")
    if graph_manifest.get("status") != "complete":
        raise ValueError("unified builder output is not complete")
    if (graph_manifest.get("scene_id"), graph_manifest.get("sequence_id")) != (
            validated["timeline_manifest"].get("scene_id"), validated["timeline_manifest"].get("sequence_id")):
        raise ValueError("unified graph output identity differs from the inputs")
    if graph_manifest.get("source_timeline_manifest_sha256") != sha256(validated["timeline"] / "manifest.json"):
        raise ValueError("unified graph output references a different object timeline")
    states = replay_unified_snapshots(graph_root)
    if len(states) != len(validated["frame_index"]):
        raise ValueError("unified output revision count differs from the validated input population")
    for state in states:
        if state.get("future_observations_used") is not False:
            raise ValueError(f"unified graph revision {state.get('revision')} includes future observations")
        if state.get("membership_availability", {}).get("status") == "unavailable":
            if [edge for edge in state.get("edges", []) if edge.get("relation") == "room_contains"]:
                raise ValueError(f"unavailable membership emitted room_contains in revision {state.get('revision')}")
        node_ids = {node.get("node_id") for node in state.get("nodes", [])}
        if any(edge.get("source_id") not in node_ids or edge.get("target_id") not in node_ids
               for edge in state.get("edges", [])):
            raise ValueError(f"unified graph has dangling edges at revision {state.get('revision')}")

    viewer_manifest = read_json(viewer_root / "viewer_manifest.json")
    viewer_index = read_json(viewer_root / "timeline_index.json")
    if viewer_manifest.get("status") != "complete":
        raise ValueError("viewer builder output is not complete")
    if viewer_manifest.get("source_manifest_sha256") != sha256(validated["timeline"] / "manifest.json"):
        raise ValueError("viewer output source timeline hash mismatch")
    frames = viewer_index.get("frames", [])
    if len(frames) != len(states):
        raise ValueError("viewer frame/revision count differs from unified graph")
    if not viewer_index.get("unified_index") or not (viewer_root / "unified_index.json").is_file():
        raise ValueError("viewer does not include the unified graph index")
    for expected_row, viewer_row in zip(validated["room_alignment"], frames):
        if (int(viewer_row.get("frame_index", -1)), int(viewer_row.get("revision", -1))) != (
                expected_row["frame_index"], expected_row["object_revision"]):
            raise ValueError("viewer frame/revision mapping differs from the validated input alignment")
    if viewer_manifest.get("max_points_per_object") != max_points:
        raise ValueError("viewer geometry display policy differs from the requested config")
    return {"revisions": len(states), "objects_final_prefix": len(states[-1].get("objects", [])),
            "rooms_final_prefix": len(states[-1].get("rooms", [])),
            "edges_final_prefix": len(states[-1].get("edges", [])),
            "membership_status_counts": {
                status: sum(state.get("membership_availability", {}).get("status") == status for state in states)
                for status in ("available", "unavailable")},
            "room_semantics_status_counts": {
                status: sum(state.get("room_semantics_availability", {}).get("status") == status for state in states)
                for status in ("available", "unavailable")}}


def run_cached_replay(config_path: Path, output: Path, *, command: list[str],
                      workspace_root: Path, max_points: int | None = None) -> dict[str, Any]:
    """Validate inputs, run B builders, validate artifacts, and persist a run manifest."""
    output = Path(output).expanduser().resolve()
    allowed_root = (Path(workspace_root).resolve() / "artifacts/cached_replays").resolve()
    if not output.is_relative_to(allowed_root):
        raise ValueError(f"cached replay output must be under {allowed_root}")
    if output.exists():
        raise FileExistsError(f"cached replay output directory already exists: {output}")
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    manifest_path = output / "run_manifest.json"
    manifest: dict[str, Any] = {
        "schema": RUN_SCHEMA, "status": "started", "run_id": output.name,
        "execution_mode": "cached_replay", "command": list(command),
        "started_utc": started, "finished_utc": None,
        "config": {"path": str(Path(config_path).expanduser().resolve()), "sha256": None,
                   "schema": INPUT_SCHEMA},
        "inputs": {"requested_by_config": str(Path(config_path).expanduser().resolve())},
        "code_provenance": {}, "policy_versions": {},
        "causality": {}, "scope_status": {}, "stages": {}, "outputs": {},
        "runtime_seconds": {}, "error": None,
        "stages_reused": ["cached_object_timeline", "cached_causal_room_state", "cached_room_revisions"],
        "stages_not_run": ["capture", "detector", "encoder", "Qwen", "hybrid_mapping", "native_replay", "A_table_scoring"],
    }
    config_resolved = Path(config_path).expanduser().resolve()
    manifest["config"]["path"] = str(config_resolved)
    if config_resolved.is_file():
        manifest["config"]["sha256"] = sha256(config_resolved)
        try:
            initial_config = read_json(config_resolved)
            manifest["inputs"]["expected_identity"] = initial_config.get("expected_identity")
            manifest["inputs"]["requested_inputs"] = {
                name: {"root": record.get("root"), "manifest_sha256": record.get("manifest_sha256")}
                for name, record in initial_config.get("inputs", {}).items() if isinstance(record, dict)}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    manifest["outputs"] = {"run_root": str(output), "run_manifest": str(manifest_path)}
    _atomic_json(manifest_path, manifest)
    total_started = time.monotonic()
    current_stage: str | None = None
    try:
        current_stage = "input_preflight"
        stage_started = time.monotonic()
        manifest["stages"][current_stage] = {"status": "started"}
        _atomic_json(manifest_path, manifest)
        validated = validate_inputs(Path(config_path), workspace_root=workspace_root)
        manifest["config"]["sha256"] = sha256(validated["config_path"])
        manifest["inputs"] = {
            "scene_id": validated["timeline_manifest"]["scene_id"],
            "sequence_id": validated["timeline_manifest"]["sequence_id"],
            "object_timeline_root": str(validated["timeline"]),
            "causal_state_root": str(validated["causal_state"]),
            "file_hashes": [{"path": path, "sha256": digest}
                            for path, digest in sorted(validated["input_files"].items())],
            "file_count": len(validated["input_files"]),
            "frame_count": len(validated["room_alignment"]),
            "revision_alignment": validated["room_alignment"],
        }
        manifest["policy_versions"] = {
            "policy_fingerprint_sha256": validated["policy_fingerprint_sha256"],
            "source_policy_contract": validated["policy_projection"],
            "input_config_schema": INPUT_SCHEMA,
        }
        manifest["coordinate_contract"] = validated["coordinate_contract"]
        manifest["coordinate_provenance_by_frame"] = validated["coordinate_provenance_by_frame"]
        manifest["scope_status"] = {
            "status_by_input_revision": validated["membership_scope_status_counts"],
            "membership": validated["causal_manifest"].get("causality", {}).get("membership", "unavailable"),
            "room_semantics": validated["causal_manifest"].get("causality", {}).get("room_semantics", "unavailable"),
        }
        manifest["causality"] = {
            **validated["causal_manifest"].get("causality", {}),
            "objects": validated["timeline_manifest"].get("causality", {}).get("objects"),
            "rooms": validated["timeline_manifest"].get("causality", {}).get("rooms"),
            "final_postprocess_included": False,
            "execution_mode": "cached_replay",
        }
        manifest["stages"][current_stage] = {"status": "completed",
                                             "runtime_seconds": time.monotonic() - stage_started,
                                             "files_validated": len(validated["input_files"])}
        manifest["runtime_seconds"][current_stage] = time.monotonic() - stage_started
        _atomic_json(manifest_path, manifest)

        current_stage = "unified_graph_builder"
        stage_started = time.monotonic()
        graph_output = output / "unified"
        manifest["stages"][current_stage] = {"status": "started", "output": str(graph_output)}
        _atomic_json(manifest_path, manifest)
        unified_manifest = _build_unified(validated["timeline"], validated["causal_state"], graph_output)
        manifest["stages"][current_stage] = {"status": "completed",
                                             "runtime_seconds": time.monotonic() - stage_started,
                                             "revision_count": unified_manifest.get("revision_count")}
        manifest["runtime_seconds"][current_stage] = time.monotonic() - stage_started
        _atomic_json(manifest_path, manifest)

        effective_max_points = max_points if max_points is not None else int(
            validated["config"].get("viewer", {}).get("max_points_per_object", 300))
        if not 1 <= effective_max_points <= 10000:
            raise ValueError("viewer max_points_per_object must be in 1..10000")
        current_stage = "viewer_builder"
        stage_started = time.monotonic()
        viewer_output = output / "viewer"
        manifest["stages"][current_stage] = {"status": "started", "output": str(viewer_output)}
        _atomic_json(manifest_path, manifest)
        _build_viewer(validated["timeline"], viewer_output, effective_max_points, graph_output)
        manifest["stages"][current_stage] = {"status": "completed",
                                             "runtime_seconds": time.monotonic() - stage_started,
                                             "max_points_per_object": effective_max_points}
        manifest["runtime_seconds"][current_stage] = time.monotonic() - stage_started
        _atomic_json(manifest_path, manifest)

        current_stage = "output_validation"
        stage_started = time.monotonic()
        summary = _validate_outputs(output, validated, effective_max_points)
        manifest["stages"][current_stage] = {"status": "completed",
                                             "runtime_seconds": time.monotonic() - stage_started,
                                             "summary": summary}
        manifest["runtime_seconds"][current_stage] = time.monotonic() - stage_started
        manifest["summary"] = summary
        manifest["outputs"] = {
            "run_root": str(output), "unified_timeline": str(output / "unified"),
            "viewer": str(output / "viewer"), "viewer_index": str(output / "viewer/index.html"),
            "run_manifest": str(manifest_path),
        }
        manifest["code_provenance"] = {}
        for relative in ("integration_v2/cached_replay.py", "integration_v2/unified_incremental_timeline.py",
                         "scripts/build_unified_incremental_timeline.py", "scripts/build_causal_timeline_viewer.py",
                         "scripts/causal_timeline_viewer.html", "scripts/serve_causal_timeline_viewer.py",
                         "scripts/scene_launcher.py"):
            path = Path(workspace_root).resolve() / relative
            manifest["code_provenance"][relative] = sha256(path) if path.is_file() else None
        manifest["outputs"]["files"] = _output_hashes(output)
        manifest["runtime_seconds"][current_stage] = time.monotonic() - stage_started
        manifest["runtime_seconds"]["total"] = time.monotonic() - total_started
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["status"] = "completed"
        _atomic_json(manifest_path, manifest)
        return manifest
    except Exception as exc:
        if current_stage:
            prior = manifest.get("stages", {}).get(current_stage, {})
            manifest.setdefault("stages", {})[current_stage] = {
                **prior, "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                "runtime_seconds": prior.get("runtime_seconds", 0.0)}
        manifest["runtime_seconds"]["total"] = time.monotonic() - total_started
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["status"] = "failed"
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc),
                              "stage": current_stage}
        _atomic_json(manifest_path, manifest)
        raise RuntimeError(f"cached_replay failed at {current_stage}; run manifest: {manifest_path}: {exc}") from exc


def make_run_id() -> str:
    from uuid import uuid4
    return f"cached_replay_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:6]}"
