#!/usr/bin/env python3
"""Evaluator-only HM3D semantic instance masks and object metrics.

This tool reads an immutable completed launcher run. Semantic labels never
enter capture, perception, P3, M1-M10, or the existing evaluation directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_VERSION = "hm3d_object_gt_v2"
# Semantic-GT rendering is evaluator-only.  Keep it deterministic and
# independent of an interactive desktop GPU: the captured RGB/depth are the
# authority, and the alignment gate below will reject any renderer mismatch.
SEMANTIC_RENDER_BACKEND = {
    # Habitat-Sim selects an EGL device from SimulatorConfiguration. Forcing
    # Mesa/llvmpipe at the same time can make EGL reject the explicitly
    # selected device (and has caused native crashes on this host). Keep the
    # evaluator headless, but let Habitat use the same device selection as
    # the capture profile.
    "name": "headless_habitat_selected_device",
    "EGL_PLATFORM": "surfaceless",
}
PROTOCOL = {
    "protocol_id": PROTOCOL_VERSION,
    "dataset_scope": "HM3D v0.2 semantic instance annotations; object instances only",
    "matching": {"method": "maximum-weight one-to-one Hungarian assignment", "mask_iou_threshold": 0.50,
                 "minimum_visible_pixels": 32, "pixel_space": "captured RGB pixel grid"},
    "render_validation": {
        "depth_pixel_error_tolerance_m": 0.03,
        "depth_p99_abs_error_max_m": 0.03,
        "depth_fraction_over_tolerance_max": 0.0001,
        "valid_mask_equal": True,
        "rgb_mean_abs_error_max": 2.0,
        "rgb_p99_abs_error_max": 16,
        "rgb_pixel_delta_tolerance": 2,
        "rgb_pixels_over_delta_tolerance_fraction_max": 0.05,
        "note": "RGB any-difference fraction and depth maximum are diagnostics; gates use tolerant per-pixel fractions plus robust percentiles to avoid single-pixel rasterization noise.",
    },
    "eligibility": {"frame": "frame IDs and indices must match capture, D2 and P3 artifacts; semantic render must pass alignment checks",
                    "gt_instance": "visible non-architectural semantic object instance with >=32 pixels and a known non-void descriptor category",
                    "zero_denominator": "metric value null and status unavailable; never coerce to 0"},
    "ignored_policy": {"architectural_categories": ["ceiling", "door", "door frame", "floor", "wall", "window"],
                       "unknown_categories": ["unknown", "unlabeled"],
                       "prediction_ignore_fraction": 0.50,
                       "rule": "For class-aware metrics, predictions with >=50% pixels over unsupported/unmapped/void/architectural GT are ignored, not FP. For class-agnostic diagnostics, predictions predominantly over void/architectural pixels are ignored. A class mismatch against an eligible confirmed GT instance is FP+FN."},
    "association": {"lineage_source": "existing predictor artifacts only; no association rerun or nearest-node inference",
                     "allowed_fields": ["first_detection_id", "last_detection_id"],
                     "incomplete_lineage": "endpoint lineage only; suppression and intermediate detection membership unavailable"},
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def mapping_is_confirmed(row: dict) -> bool:
    provenance = row.get("provenance")
    return (row.get("status") == "confirmed" and isinstance(provenance, dict) and
            all(isinstance(provenance.get(key), str) and provenance[key].strip()
                for key in ("reviewer", "date", "decision")))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        f.write("\n")
        f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)


def semantic_render_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a headless environment without overriding Habitat's EGL device.

    Habitat-Sim selects a device through SimulatorConfiguration. Forcing
    llvmpipe through Mesa environment variables can conflict with that
    explicit EGL device selection. The evaluator therefore removes desktop
    display variables and keeps EGL surfaceless, while retaining Habitat's
    selected-device policy. This is isolated from capture and predictor.
    """
    env = dict(os.environ if base is None else base)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    # Do not inherit a caller's software-renderer override: Magnum may already
    # have selected an EGL hardware device from gpu_device_id.
    for key in ("LIBGL_ALWAYS_SOFTWARE", "MESA_LOADER_DRIVER_OVERRIDE", "GALLIUM_DRIVER"):
        env.pop(key, None)
    env.update(SEMANTIC_RENDER_BACKEND)
    return env


def renderer_failure_summary(returncode: int, stderr: str) -> str:
    """Describe native renderer exits without presenting preceding warnings as the cause."""
    if returncode < 0:
        import signal
        try:
            signal_name = signal.Signals(-returncode).name
        except ValueError:
            signal_name = f"signal {-returncode}"
        return f"renderer process terminated by {signal_name} ({returncode})"
    tail = stderr.strip().splitlines()
    return f"renderer exited with code {returncode}" + (f": {tail[-1]}" if tail else "")


def render_alignment_checks(*, rgb_mean: float, rgb_p99: float, rgb_over_tolerance_fraction: float,
                            valid_mask_equal: bool, depth_p99: float | None,
                            depth_over_tolerance_fraction: float | None,
                            unknown_semantic_ids: list[int]) -> dict[str, bool]:
    """Version-2 alignment gates, tolerant to sparse rasterization-level outliers."""
    cfg = PROTOCOL["render_validation"]
    return {
        "rgb_mean": rgb_mean <= cfg["rgb_mean_abs_error_max"],
        "rgb_p99": rgb_p99 <= cfg["rgb_p99_abs_error_max"],
        "rgb_pixels_over_tolerance": rgb_over_tolerance_fraction <= cfg["rgb_pixels_over_delta_tolerance_fraction_max"],
        "depth_valid_mask": valid_mask_equal,
        "depth_p99": depth_p99 is not None and depth_p99 <= cfg["depth_p99_abs_error_max_m"],
        "depth_over_tolerance_fraction": (depth_over_tolerance_fraction is not None and
                                          depth_over_tolerance_fraction <= cfg["depth_fraction_over_tolerance_max"]),
        "descriptor_ids": not unknown_semantic_ids,
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _locked_habitat_data_root() -> Path:
    """Read the evaluator's read-only scene-data root without importing project runtime code."""
    lock_path = ROOT / "locks/external_resources.json"
    lock = load_json(lock_path)
    if (lock.get("status") != "bound_input_only"
            or Path(lock.get("workspace_root", "")).resolve() != ROOT.resolve()):
        raise RuntimeError(f"external resource lock is not bound input-only to this workspace: {lock_path}")
    record = lock.get("directories", {}).get("habitat_data_root")
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        raise RuntimeError(f"habitat_data_root is not bound in {lock_path}")
    role = str(record.get("role", ""))
    if not any(token in role for token in ("read_only", "input_only", "posthoc")):
        raise RuntimeError(f"habitat_data_root is not declared read-only in {lock_path}")
    path = Path(record["path"]).expanduser().resolve()
    if path.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"habitat_data_root resolves inside the writable workspace: {path}")
    return path


def resolve_evaluator_asset(value: str | Path | None) -> Path | None:
    """Resolve scene assets from the workspace or its locked read-only HM3D root."""
    if value is None or not str(value).strip():
        return None
    configured = Path(value).expanduser()
    workspace_candidate = configured if configured.is_absolute() else ROOT / configured
    workspace_candidate = workspace_candidate.resolve()
    if workspace_candidate.is_file():
        return workspace_candidate

    # Profile paths conventionally use artifacts/habitat_data/<relative path>.
    # Old run manifests may contain either that relative path or an absolute
    # path under a previous workspace root, so recover only this known prefix.
    parts = configured.parts
    relative_to_habitat = None
    for index in range(len(parts) - 1):
        if parts[index:index + 2] == ("artifacts", "habitat_data"):
            relative_to_habitat = Path(*parts[index + 2:])
            break
    if relative_to_habitat is not None:
        try:
            bound_root = _locked_habitat_data_root()
            candidate = (bound_root / relative_to_habitat).resolve()
            if candidate.is_relative_to(bound_root) and candidate.is_file():
                return candidate
            # Return the locked-root candidate so preflight tells the user
            # exactly which bound location was checked.
            return candidate
        except Exception:
            pass
    return workspace_candidate


def resolve_mapping_path(run_root: Path, explicit: Path | None = None) -> Path | None:
    """Select the scene's evaluator mapping; never apply another scene's map."""
    if explicit is not None:
        return explicit.expanduser().resolve()
    profile = load_json(run_root / "effective_evaluation_profile.json")
    configured = profile.get("category_mapping")
    if configured:
        path = Path(configured).expanduser()
        return (path if path.is_absolute() else ROOT / path).resolve()
    if profile.get("dataset", {}).get("scene_id") == "00802-wcojb4TFT35":
        return (ROOT / "configs/evaluation/hm3d_00802_object_category_mapping_v1.json").resolve()
    # A missing per-scene map does not prevent class-agnostic mask diagnostics.
    return None


def run_paths(run_root: Path) -> dict[str, Path]:
    return {name: run_root / rel for name, rel in {
        "launcher": "launcher_manifest.json", "capture_manifest": "capture/capture_manifest.json",
        "route": "effective_route_config.json", "profile": "effective_evaluation_profile.json",
        "d2": "detection_2d/detection_2d_index.json", "d3_manifest": "detection_3d/detection_3d_manifest.json",
        "d3": "detection_3d/detection_3d.jsonl", "ledger": "detection_3d/object_evidence_ledger.jsonl",
        "snapshot": "replay/snapshot.json", "delta": "replay/delta_log.json", "replay_manifest": "replay/manifest.json",
    }.items()}


def predictor_content_hashes(run_root: Path) -> dict[str, str]:
    paths = run_paths(run_root)
    hashes = {key: sha256(path) for key, path in paths.items()}
    capture = load_json(paths["capture_manifest"]); detections = load_json(paths["d2"])
    for record in capture.get("frames", []):
        frame_dir = run_root / "capture" / record["frame_dir"]
        for name in ("metadata.json", "rgb.npy", "depth_m.npy", "valid_mask.npy"):
            path = frame_dir / name
            if path.is_file(): hashes[f"capture_frame:{record['frame_id']}:{name}"] = sha256(path)
    for frame in detections.get("frames", []):
        for det in frame.get("detections", []):
            mask = (run_root / "detection_2d" / det["mask_file"]).resolve()
            if mask.is_file(): hashes[f"detection_mask:{det['detection_id']}"] = sha256(mask)
    for key, raw in (("detector_config", detections.get("config")), ("detector_weights", detections.get("weights"))):
        if raw and Path(raw).is_file(): hashes[key] = sha256(Path(raw))
    return hashes


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def preflight(run_root: Path, mapping_path: Path | None = None) -> dict:
    issues: list[dict] = []
    statuses: list[str] = []
    paths = run_paths(run_root)
    missing = [key for key, path in paths.items() if not path.is_file()]
    if missing:
        return {"status": "blocked", "run_root": str(run_root), "issues": [{"kind": "missing_input", "paths": missing}],
                "ready_for_render": False}
    launch = load_json(paths["launcher"]); cap = load_json(paths["capture_manifest"])
    d2 = load_json(paths["d2"]); d3m = load_json(paths["d3_manifest"]); rep = load_json(paths["replay_manifest"])
    profile = load_json(paths["profile"]); route = load_json(paths["route"])
    mapping_path = resolve_mapping_path(run_root, mapping_path)
    input_hashes = predictor_content_hashes(run_root)
    if launch.get("status") != "completed": issues.append({"kind": "predictor_run_not_completed", "status": launch.get("status")})
    if cap.get("status") != "pass" or not cap.get("frame_count"): issues.append({"kind": "capture_invalid"})
    if d2.get("status") != "pass" or d2.get("capture_manifest_sha256") != input_hashes["capture_manifest"]:
        issues.append({"kind": "detection2d_provenance_mismatch"})
    d2_config = Path(str(d2.get("config", ""))).expanduser()
    d2_weights = Path(str(d2.get("weights", ""))).expanduser()
    if not d2_config.is_file() or (d2_config.is_file() and sha256(d2_config) != d2.get("config_sha256")):
        issues.append({"kind": "detector_config_missing_or_hash_mismatch", "path": str(d2_config)})
    if not d2_weights.is_file() or (d2_weights.is_file() and sha256(d2_weights) != d2.get("weights_sha256")):
        issues.append({"kind": "detector_weights_missing_or_hash_mismatch", "path": str(d2_weights)})
    if d3m.get("status") != "pass" or d3m.get("capture_manifest_sha256") != input_hashes["capture_manifest"]:
        issues.append({"kind": "p3_provenance_mismatch"})
    if rep.get("pipeline_status") != "pass" or rep.get("capture_manifest_sha256") != input_hashes["capture_manifest"]:
        issues.append({"kind": "graph_provenance_mismatch"})
    if cap.get("semantic_gt_saved") is not False or profile.get("ground_truth", {}).get("semantic_capture_available") is not False:
        issues.append({"kind": "semantic_capture_contract_unexpected", "note": "This evaluator expects semantic GT absent from predictor capture."})
    scene_id = cap.get("scene_id")
    if profile.get("dataset", {}).get("scene_id") != scene_id or route.get("scene_id") != scene_id:
        issues.append({"kind": "scene_id_mismatch", "capture": scene_id,
                       "profile": profile.get("dataset", {}).get("scene_id"), "route": route.get("scene_id")})
    records = cap.get("frames", []); route_entries = route.get("route", [])
    d2_frames = d2.get("frames", [])
    if len(records) != cap.get("frame_count") or len(route_entries) != len(records) or len(d2_frames) != len(records):
        issues.append({"kind": "frame_count_mismatch", "capture": len(records), "route": len(route_entries), "d2": len(d2_frames)})
    frame_ids = [str(x.get("frame_id")) for x in records]
    if len(set(frame_ids)) != len(frame_ids): issues.append({"kind": "duplicate_capture_frame_id"})
    for i, rec in enumerate(records):
        if i >= len(d2_frames) or rec.get("frame_id") != d2_frames[i].get("frame_id") or rec.get("frame_index") != d2_frames[i].get("frame_index"):
            issues.append({"kind": "frame_alignment_mismatch", "index": i}); break
        frame_dir = run_root / "capture" / rec["frame_dir"]
        for filename in ("metadata.json", "rgb.npy", "depth_m.npy", "valid_mask.npy"):
            if not (frame_dir / filename).is_file():
                issues.append({"kind": "capture_frame_file_missing", "frame_id": rec.get("frame_id"), "file": filename})
                break
        metadata = load_json(frame_dir / "metadata.json")
        if metadata.get("frame_id") != rec.get("frame_id") or metadata.get("scene_id") != scene_id:
            issues.append({"kind": "capture_metadata_mismatch", "frame_id": rec.get("frame_id")})
        if metadata.get("route_entry") != route_entries[i]:
            # JSON numbers are serialized identically by the capture route copy; flag instead of silently tolerating drift.
            issues.append({"kind": "capture_route_pose_mismatch", "frame_id": rec.get("frame_id")})
        if metadata.get("semantic_gt_used") or metadata.get("room_gt_used"):
            issues.append({"kind": "predictor_gt_contamination_flag", "frame_id": rec.get("frame_id")})
    descriptor = profile.get("dataset", {}).get("semantic_descriptor_evaluator_only")
    sem_mesh = profile.get("dataset", {}).get("semantic_mesh_evaluator_only")
    dataset_cfg = cap.get("scene_dataset_config")
    scene_file = cap.get("scene_file")
    resolved = {"semantic_descriptor": resolve_evaluator_asset(descriptor),
                "semantic_mesh": resolve_evaluator_asset(sem_mesh),
                "scene_dataset_config": resolve_evaluator_asset(dataset_cfg),
                "scene_file": resolve_evaluator_asset(scene_file)}
    for name, path in resolved.items():
        if path is None or not path.is_file(): issues.append({"kind": "semantic_asset_missing", "asset": name, "path": str(path)})
    mapping = load_json(mapping_path) if mapping_path and mapping_path.is_file() else None
    if not mapping:
        statuses.append("category_mapping_unconfigured_class_agnostic_only")
    else:
        mapping_scene = mapping.get("scene_id")
        if mapping_scene != scene_id: issues.append({"kind": "mapping_scene_mismatch", "mapping_scene_id": mapping_scene})
        rows = mapping.get("categories", {})
        for category, row in rows.items():
            if row.get("status") == "confirmed" and not mapping_is_confirmed(row):
                issues.append({"kind": "mapping_confirmation_missing_provenance", "category": category,
                               "required": ["provenance.reviewer", "provenance.date", "provenance.decision"]})
        counts = Counter("confirmed" if mapping_is_confirmed(row) else row.get("status", "unmapped")
                         for row in rows.values())
        statuses.append("class_aware_incomplete" if counts.get("confirmed", 0) == 0 else "mapping_has_confirmed_rows")
        canonical_map = d2.get("vocabulary", {}).get("canonical_map", {})
        names_path = ROOT / "configs/perception/coco.names"
        detector_classes = {canonical_map.get(name, name.replace(" ", "_"))
                            for name in names_path.read_text(encoding="utf-8").splitlines() if name.strip()} if names_path.is_file() else set()
        for category, row in rows.items():
            target = row.get("detector_class")
            if target and target not in detector_classes:
                issues.append({"kind": "mapping_target_not_in_detector_vocabulary", "category": category,
                               "detector_class": target})
    # Existing graph snapshots retain first/last detection endpoints only; no M6 decisions per observation are persisted.
    snapshot = load_json(paths["snapshot"])
    graph = snapshot.get("snapshot", snapshot)
    has_endpoints = any(n.get("kind") == "object" and (n.get("attributes", {}).get("first_detection_id") or n.get("attributes", {}).get("last_detection_id"))
                        for n in graph.get("nodes", []))
    statuses.append("association_endpoint_lineage_only" if has_endpoints else "association_lineage_missing")
    status = "blocked" if issues else "ready_with_limits"
    return {"status": status, "ready_for_render": not issues, "run_id": launch.get("run_id"), "scene_id": scene_id,
            "frame_count": cap.get("frame_count"), "resolution_px": cap.get("resolution_px"), "route_alias": launch.get("route_alias"),
            "input_hashes": input_hashes, "assets": {name: {"path": str(path), "sha256": sha256(path)} for name, path in resolved.items() if path and path.is_file()},
            "mapping": {"path": str(mapping_path) if mapping_path else None, "sha256": sha256(mapping_path) if mapping_path and mapping_path.is_file() else None,
                        "status_counts": dict(Counter(row.get("status", "unmapped") for row in mapping.get("categories", {}).values())) if mapping else {}},
            "readiness_notes": statuses, "association_lineage": "snapshot first/last detection endpoints only; per-detection matched/suppressed/created decisions absent",
            "issues": issues}


def summarize_preflight(doc: dict) -> str:
    """Human-sized preflight view; full hashes remain available via --json."""
    lines = [f"Object GT preflight: {str(doc.get('status', 'unknown')).upper()}",
             f"Run: {doc.get('run_id', 'N/A')} | Scene: {doc.get('scene_id', 'N/A')}",
             f"Capture: {doc.get('frame_count', 'N/A')} frames | resolution: {doc.get('resolution_px', 'N/A')} px"]
    assets = doc.get("assets", {})
    asset_names = ("semantic_descriptor", "semantic_mesh", "scene_dataset_config", "scene_file")
    lines.append("Assets: " + ", ".join(f"{name}={'ok' if assets.get(name, {}).get('path') else 'missing'}" for name in asset_names))
    mapping = doc.get("mapping", {}).get("status_counts", {})
    readiness = ", ".join(f"{key}={value}" for key, value in sorted(mapping.items())) or "missing"
    notes = set(doc.get("readiness_notes", []))
    aware = "incomplete — cần category mapping human-confirmed" if "class_aware_incomplete" in notes else "has confirmed mappings"
    lines.append(f"Category mapping: {readiness}; class-aware metrics {aware}.")
    lines.append(f"Association lineage: {doc.get('association_lineage', 'unavailable')}.")
    issues = doc.get("issues", [])
    blockers = [f"{item.get('kind')}: {item.get('note') or item.get('path') or item.get('status') or item}" for item in issues]
    lines.append("Blockers: " + ("; ".join(blockers) if blockers else "none"))
    if doc.get("status") == "ready_with_limits":
        lines.append("Ready with limits: GT diagnostic/review can run; class-aware or full association scoring may be unavailable.")
    return "\n".join(lines)


def parse_descriptor(path: Path) -> dict[int, dict]:
    out: dict[int, dict] = {}
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        lines = f.read().splitlines()
    for line_no, line in enumerate(lines[1:], start=2):
        if not line.strip(): continue
        cols = next(csv.reader([line]))
        if len(cols) < 4: continue
        try: sid = int(cols[0])
        except ValueError: continue
        if sid in out: raise ValueError(f"duplicate semantic ID {sid} in descriptor line {line_no}")
        out[sid] = {"semantic_id": sid, "color_hex": cols[1], "category": cols[2].strip().lower(),
                    "region_id": int(cols[3]) if cols[3].strip().isdigit() else cols[3].strip(), "descriptor_line": line_no}
    if not out: raise ValueError(f"no semantic rows parsed from {path}")
    return out


def render_semantic(args: argparse.Namespace) -> int:
    # Set these before importing Habitat-Sim/Magnum: the graphics context is
    # created during simulator construction, not when observations are read.
    os.environ.update(semantic_render_env())
    try:
        import habitat_sim
        import magnum as mn
        from habitat_sim.utils.common import quat_from_angle_axis
    except Exception as exc:
        raise RuntimeError(f"semantic rendering needs the pinned Habitat-Sim environment: {exc}") from exc
    run_root = args.run_root.resolve(); cap_root = run_root / "capture"
    cap = load_json(cap_root / "capture_manifest.json"); route = load_json(run_root / "effective_route_config.json")
    width, height = map(int, cap["resolution_px"]); hfov = float(cap["horizontal_fov_deg"])
    scene_file = resolve_evaluator_asset(cap["scene_file"]); dataset_config = resolve_evaluator_asset(cap["scene_dataset_config"])
    descriptor_path = resolve_evaluator_asset(args.descriptor)
    if scene_file is None or dataset_config is None or descriptor_path is None:
        raise FileNotFoundError("semantic renderer requires a scene, dataset config, and descriptor")
    descriptor = parse_descriptor(descriptor_path)
    sim_cfg = habitat_sim.SimulatorConfiguration(); sim_cfg.scene_id = str(scene_file)
    sim_cfg.scene_dataset_config_file = str(dataset_config); sim_cfg.create_renderer = True
    sim_cfg.requires_textures = True; sim_cfg.enable_physics = False
    sim_cfg.gpu_device_id = int(route.get("gpu_device_id", -1))
    agent_cfg = habitat_sim.AgentConfiguration(); specs = []
    for typ, sensor_uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                             (habitat_sim.SensorType.DEPTH, "depth_sensor"),
                             (habitat_sim.SensorType.SEMANTIC, "semantic_sensor")):
        spec = habitat_sim.CameraSensorSpec(); spec.uuid = sensor_uuid; spec.sensor_type = typ
        spec.resolution = mn.Vector2i(height, width)
        spec.position = mn.Vector3(*[float(v) for v in route["sensor_position_habitat"]])
        spec.orientation = mn.Vector3(*[float(v) for v in route.get("sensor_orientation_habitat_rad", [0, 0, 0])])
        spec.hfov = mn.Deg(hfov); spec.near = float(route.get("min_depth_m", 0.1)); spec.far = float(route.get("max_depth_m", 10.0))
        specs.append(spec)
    agent_cfg.sensor_specifications = specs
    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg])); agent = sim.initialize_agent(0)
    out_dir = Path(args.output).resolve(); out_dir.mkdir(parents=True, exist_ok=True)
    frame_out_dir = out_dir / "frames"; frame_out_dir.mkdir(exist_ok=True)
    instance_out_dir = out_dir / "instance_masks"; instance_out_dir.mkdir(exist_ok=True)
    rows = []; min_depth = float(route.get("min_depth_m", .1)); max_depth = float(route.get("max_depth_m", 10.0))
    try:
        for rec in cap["frames"]:
            i = int(rec["frame_index"]); frame_dir = cap_root / rec["frame_dir"]
            md = load_json(frame_dir / "metadata.json"); pose = np.asarray(md["base_pose_habitat"], dtype=np.float64)
            route_entry = md["route_entry"]; yaw = float(route_entry["yaw_rad"])
            expected_rot = np.asarray(pose[:3, :3], dtype=np.float64)
            # Poses are from the saved per-frame ground-truth state; yaw is used only to reconstruct Habitat's quaternion.
            state = agent.get_state(); state.position = np.asarray(pose[:3, 3], dtype=np.float32)
            state.rotation = quat_from_angle_axis(yaw, np.array([0, 1, 0], dtype=np.float32))
            agent.set_state(state, infer_sensor_states=True)
            actual = agent.get_state(); actual_rot = np.asarray(__import__("quaternion").as_rotation_matrix(actual.rotation))
            pose_rot_error = float(np.max(np.abs(actual_rot - expected_rot)))
            if pose_rot_error > 2e-5: raise RuntimeError(f"frame {i}: reconstructed base rotation differs from captured pose by {pose_rot_error}")
            obs = sim.get_sensor_observations()
            rgb = np.asarray(obs["color_sensor"]); rgb = rgb[..., :3] if rgb.ndim == 3 and rgb.shape[-1] == 4 else rgb
            depth = np.asarray(obs["depth_sensor"], dtype=np.float32)
            ids = np.asarray(obs["semantic_sensor"])
            if ids.shape != (height, width): raise RuntimeError(f"semantic shape mismatch {ids.shape}; expected {(height,width)}")
            if ids.dtype.kind not in "ui": raise RuntimeError(f"semantic sensor must produce integer labels; got {ids.dtype}")
            ref_rgb = np.load(frame_dir / "rgb.npy", allow_pickle=False); ref_rgb = ref_rgb[..., :3] if ref_rgb.ndim == 3 and ref_rgb.shape[-1] == 4 else ref_rgb
            ref_depth = np.load(frame_dir / "depth_m.npy", allow_pickle=False); ref_valid = np.load(frame_dir / "valid_mask.npy", allow_pickle=False)
            if rgb.shape != ref_rgb.shape or depth.shape != ref_depth.shape: raise RuntimeError(f"frame {i}: RGB/depth dimensions differ from capture")
            diff = np.abs(rgb.astype(np.int16) - ref_rgb.astype(np.int16))
            rgb_mean = float(diff.mean()); rgb_p99 = float(np.percentile(diff, 99)); rgb_changed = float(np.mean(np.any(diff != 0, axis=-1)))
            rgb_pixel_max_delta = np.max(diff, axis=-1)
            rgb_changed_over_2 = float(np.mean(rgb_pixel_max_delta > 2))
            valid = np.isfinite(depth) & (depth >= min_depth) & (depth <= max_depth)
            valid_equal = bool(np.array_equal(valid, ref_valid)); both = valid & ref_valid
            dep_err = np.abs(depth[both] - ref_depth[both]); dep_max = float(dep_err.max()) if dep_err.size else None
            dep_p99 = float(np.percentile(dep_err, 99)) if dep_err.size else None
            dep_over_tolerance = int(np.count_nonzero(dep_err > PROTOCOL["render_validation"]["depth_pixel_error_tolerance_m"]))
            dep_over_tolerance_fraction = float(dep_over_tolerance / dep_err.size) if dep_err.size else None
            if dep_err.size:
                max_rc = np.argwhere(both)[int(np.argmax(dep_err))]
                dep_max_rc = [int(max_rc[0]), int(max_rc[1])]
                dep_max_values = {"render_m": float(depth[tuple(max_rc)]),
                                  "capture_m": float(ref_depth[tuple(max_rc)])}
            else:
                dep_max_rc = None; dep_max_values = None
            unknown = sorted(int(v) for v in np.unique(ids) if int(v) != 0 and int(v) not in descriptor)
            sensor_state = actual.sensor_states["semantic_sensor"]
            sensor_pos_error = float(np.linalg.norm(np.asarray(sensor_state.position, dtype=np.float64) - np.asarray(md["sensor_position_habitat"], dtype=np.float64)))
            sensor_rot = np.asarray(__import__("quaternion").as_rotation_matrix(sensor_state.rotation), dtype=np.float64)
            sensor_rot_error = float(np.max(np.abs(sensor_rot - np.asarray(md["sensor_rotation_habitat"], dtype=np.float64))))
            if sensor_pos_error > 2e-5 or sensor_rot_error > 2e-5:
                raise RuntimeError(f"frame {i}: sensor extrinsics differ from capture (position {sensor_pos_error} m, rotation {sensor_rot_error})")
            checks = render_alignment_checks(rgb_mean=rgb_mean, rgb_p99=rgb_p99,
                      rgb_over_tolerance_fraction=rgb_changed_over_2, valid_mask_equal=valid_equal,
                      depth_p99=dep_p99, depth_over_tolerance_fraction=dep_over_tolerance_fraction,
                      unknown_semantic_ids=unknown)
            map_name = f"{rec['frame_id']}__semantic_id_map.npy"
            np.save(frame_out_dir / map_name, ids.astype(np.uint32), allow_pickle=False)
            for semantic_id in np.unique(ids):
                semantic_id = int(semantic_id)
                if semantic_id == 0: continue
                np.save(instance_out_dir / f"{rec['frame_id']}__{semantic_id:06d}.npy", ids == semantic_id, allow_pickle=False)
            vals, counts = np.unique(ids, return_counts=True)
            rows.append({"frame_id": rec["frame_id"], "frame_index": i, "timestamp_s": rec["timestamp_s"],
                         "semantic_id_map": f"frames/{map_name}", "base_pose_rotation_max_abs_error": pose_rot_error,
                         "sensor_position_error_m": sensor_pos_error, "sensor_rotation_max_abs_error": sensor_rot_error,
                         "rgb_mean_abs_error": rgb_mean, "rgb_p99_abs_error": rgb_p99, "rgb_changed_pixel_fraction": rgb_changed,
                         "rgb_pixel_max_delta_over_2_fraction": rgb_changed_over_2,
                         "depth_valid_mask_equal": valid_equal, "depth_max_abs_error_m": dep_max,
                         "depth_p99_abs_error_m": dep_p99, "depth_pixels_over_tolerance": dep_over_tolerance,
                         "depth_fraction_over_tolerance": dep_over_tolerance_fraction,
                         "depth_max_error_pixel_rc": dep_max_rc, "depth_max_error_values_m": dep_max_values,
                         "unknown_nonzero_semantic_ids": unknown, "checks": checks,
                         "visible_pixels_by_semantic_id": {str(int(v)): int(c) for v,c in zip(vals, counts)}})
    finally: sim.close()
    report = {"schema_version": 1, "kind": "hm3d_semantic_instance_gt_render", "status": "pass" if rows and all(all(r["checks"].values()) for r in rows) else "alignment_failed",
              "habitat_sim_version": getattr(habitat_sim, "__version__", "unknown"), "scene_id": cap["scene_id"], "semantic_descriptor_sha256": sha256(descriptor_path),
              "frame_count": len(rows), "frames": rows, "semantic_ids_are_evaluator_only": True}
    write_json(out_dir / "render_validation.json", report)
    return 0 if report["status"] == "pass" else 3


def _mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = int(np.count_nonzero(a & b)); union = int(np.count_nonzero(a | b))
    return inter / union if union else 0.0


def _assign(iou: np.ndarray, threshold: float) -> list[tuple[int, int, float]]:
    if not iou.size: return []
    # Rectangular Hungarian assignment (rows <= columns after adding one dummy
    # unmatched column per prediction). A valid match has reward 1+IoU, so the
    # objective first maximizes the number of threshold-valid pairs, then IoU.
    n, gt_count = iou.shape
    m = gt_count + n
    eligible = iou >= threshold
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, gt_count + 1):
            cost[i][j] = -(1.0 + float(iou[i - 1, j - 1])) if eligible[i - 1, j - 1] else 1e6
        # Dummy columns have zero reward and encode an unmatched detection.
    u = [0.0] * (n + 1); v = [0.0] * (m + 1); p = [0] * (m + 1); way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i; j0 = 0; minv = [float("inf")] * (m + 1); used = [False] * (m + 1)
        while True:
            used[j0] = True; i0 = p[j0]; delta = float("inf"); j1 = 0
            for j in range(1, m + 1):
                if used[j]: continue
                cur = cost[i0][j] - u[i0] - v[j]
                if cur < minv[j]: minv[j] = cur; way[j] = j0
                if minv[j] < delta: delta = minv[j]; j1 = j
            for j in range(m + 1):
                if used[j]: u[p[j]] += delta; v[j] -= delta
                else: minv[j] -= delta
            j0 = j1
            if p[j0] == 0: break
        while True:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
            if j0 == 0: break
    assigned = [0] * (n + 1)
    for j in range(1, m + 1):
        if p[j]: assigned[p[j]] = j
    return [(i - 1, j - 1, float(iou[i - 1, j - 1]))
            for i, j in enumerate(assigned[1:], start=1)
            if j and j <= gt_count and eligible[i - 1, j - 1]]


def _read_detector(run_root: Path) -> tuple[dict[str, list[dict]], Path]:
    idx_path = run_root / "detection_2d/detection_2d_index.json"; doc = load_json(idx_path)
    result = {}
    for frame in doc.get("frames", []):
        items = []
        for det in frame.get("detections", []):
            mask_path = (idx_path.parent / det["mask_file"]).resolve()
            if not mask_path.is_file(): raise FileNotFoundError(f"detection mask missing: {mask_path}")
            if sha256(mask_path) != det.get("mask_sha256"): raise ValueError(f"detection mask hash mismatch: {mask_path.name}")
            mask = np.load(mask_path, allow_pickle=False).astype(bool)
            if mask.shape != tuple(det["image_shape_hw"]): raise ValueError(f"mask shape mismatch for {det['detection_id']}")
            items.append({**det, "mask": mask, "mask_path": mask_path})
        result[str(frame["frame_id"])] = items
    return result, idx_path


def _read_gt_frame(path: Path, descriptors: dict[int, dict], mapping: dict) -> tuple[list[dict], np.ndarray, dict[int, str]]:
    labels = np.load(path, allow_pickle=False).astype(np.uint32)
    instances = []; status_by_sid = {}; ignored = set()
    arch = set(PROTOCOL["ignored_policy"]["architectural_categories"] + PROTOCOL["ignored_policy"]["unknown_categories"])
    category_rows = mapping.get("categories", {})
    for sid in np.unique(labels):
        sid = int(sid)
        if sid == 0: continue
        desc = descriptors.get(sid)
        if not desc: ignored.add(sid); status_by_sid[sid] = "unmapped"; continue
        cat = desc["category"]; m = labels == sid; area = int(m.sum())
        row = category_rows.get(cat, {"status": mapping.get("unlisted_category_status", "unmapped"), "detector_class": None})
        effective_status = "confirmed" if mapping_is_confirmed(row) else ("provisional" if row.get("status") == "confirmed" else row.get("status", "unmapped"))
        status_by_sid[sid] = effective_status
        if cat in arch or area < int(PROTOCOL["matching"]["minimum_visible_pixels"]):
            ignored.add(sid); continue
        instances.append({"semantic_id": sid, "category": cat, "mask": m, "area": area,
                          "mapping_status": effective_status, "detector_class": row.get("detector_class")})
    return instances, labels, status_by_sid


def _metric(tp: int, fp: int, fn: int, *, scope: str, available: bool = True, reason: str | None = None) -> dict:
    den_p, den_r = tp + fp, tp + fn
    precision = tp / den_p if den_p else None; recall = tp / den_r if den_r else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else (0.0 if precision == 0 and recall == 0 else None)
    return {"status": "available" if available and den_p + den_r else "unavailable", "scope": scope,
            "tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1,
            "unavailable_reason": None if available and den_p + den_r else (reason or "zero denominator")}


def _overlay(rgb: np.ndarray, pred_mask: np.ndarray, gt_mask: np.ndarray | None, target: Path) -> None:
    base = Image.fromarray(rgb[..., :3].astype(np.uint8), "RGB")
    def panel(mask: np.ndarray | None, color: tuple[int,int,int]) -> Image.Image:
        p = base.copy().convert("RGBA")
        if mask is not None:
            lay = Image.new("RGBA", p.size, (*color, 0)); alpha = Image.fromarray((mask.astype(np.uint8) * 120), "L"); lay.putalpha(alpha); p.alpha_composite(lay)
        return p.convert("RGB")
    a, b = panel(pred_mask, (0, 255, 70)), panel(gt_mask, (255, 0, 220))
    canvas = Image.new("RGB", (base.width * 2, base.height + 28), "white"); canvas.paste(a, (0, 28)); canvas.paste(b, (base.width, 28))
    draw = ImageDraw.Draw(canvas); draw.text((4, 7), "RGB + prediction (green)", fill="black"); draw.text((base.width + 4, 7), "RGB + semantic GT (magenta)", fill="black")
    target.parent.mkdir(parents=True, exist_ok=True); canvas.save(target)


def _render_html(out: Path, review_rows: list[dict]) -> None:
    cards = []
    for row in review_rows:
        image = html.escape(row["image"]); detail = html.escape(json.dumps(row["details"], ensure_ascii=False, indent=2))
        cards.append(f'<article><h3>{html.escape(row["title"])}</h3><img src="{image}"><details><summary>Chi tiết</summary><pre>{detail}</pre></details></article>')
    page = """<!doctype html><html lang=vi><meta charset=utf-8><meta name=viewport content='width=device-width'><title>HM3D object GT review</title><style>body{font:15px system-ui;margin:20px;background:#f4f5f7}h1{font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(660px,1fr));gap:16px}article{background:white;padding:12px;border:1px solid #ccd;border-radius:8px}img{width:100%;height:auto}pre{white-space:pre-wrap;max-height:28em;overflow:auto}</style><h1>Object GT review — GT magenta; prediction green</h1><p>Diagnostic/evaluation-only. Unconfirmed category mappings are excluded from class-aware metrics. Use image and per-detection details; this is not room ground truth.</p><main class=grid>""" + "".join(cards) + "</main></html>"
    (out / "review/index.html").write_text(page, encoding="utf-8")


def evaluate(args: argparse.Namespace) -> dict:
    print("PROGRESS: 1/4 Preflight và khóa inputs predictor", flush=True)
    run_root = args.run_root.resolve(); out = args.output.resolve()
    mapping_path = resolve_mapping_path(run_root, args.mapping)
    pf = preflight(run_root, mapping_path)
    if not pf["ready_for_render"]: raise RuntimeError(f"preflight blocked: {pf['issues']}")
    if out.exists(): raise FileExistsError(f"refusing to overwrite evaluation output: {out}")
    out.mkdir(parents=True)
    effective_route = load_json(run_root / "effective_route_config.json")
    render_backend = {**SEMANTIC_RENDER_BACKEND,
                      "habitat_gpu_device_id": int(effective_route.get("gpu_device_id", -1))}
    manifest_path = out / "manifest.json"
    manifest = {"schema_version": 1, "kind": "hm3d_object_gt_evaluation", "status": "running", "run_id": pf["run_id"],
                "evaluation_id": out.name, "started_utc": utc_now(), "run_root": str(run_root), "output": str(out),
                "predictor_input_hashes_before": pf["input_hashes"], "predictor_outputs_mutated": False,
                "semantic_render_backend": render_backend}
    write_json(manifest_path, manifest); write_json(out / "protocol.json", PROTOCOL)
    mapping_doc = (load_json(mapping_path) if mapping_path and mapping_path.is_file() else {
        "schema_version": 1, "mapping_id": "unconfigured_scene_mapping",
        "scene_id": cap["scene_id"], "categories": {},
        "status_policy": "No category mapping configured; class-agnostic mask metrics only.",
        "unlisted_category_status": "unmapped"})
    write_json(out / "category_mapping_snapshot.json", mapping_doc)
    write_json(out / "preflight.json", pf)
    profile = load_json(run_root / "effective_evaluation_profile.json"); cap = load_json(run_root / "capture/capture_manifest.json")
    descriptor_path = resolve_evaluator_asset(profile["dataset"]["semantic_descriptor_evaluator_only"])
    if descriptor_path is None or not descriptor_path.is_file():
        raise FileNotFoundError(f"semantic descriptor not found after resource binding: {descriptor_path}")
    descriptors = parse_descriptor(descriptor_path)
    print("PROGRESS: 2/4 Chuẩn bị semantic GT cache; kiểm tra RGB/depth alignment", flush=True)
    cache_inputs = {"protocol": PROTOCOL_VERSION, "semantic_render_backend": render_backend,
                    "capture_manifest": pf["input_hashes"]["capture_manifest"],
                    "route": pf["input_hashes"]["route"], "renderer_script": sha256(Path(__file__).resolve()),
                    "scene": cap.get("scene_sha256"), "scene_dataset_config": cap.get("scene_dataset_config_sha256"),
                    "semantic_mesh": pf["assets"]["semantic_mesh"]["sha256"], "descriptor": sha256(descriptor_path),
                    "frames": []}
    for rec in cap["frames"]:
        fd = run_root / "capture" / rec["frame_dir"]
        cache_inputs["frames"].append({"frame_id": rec["frame_id"], **{name: sha256(fd / name) for name in ("metadata.json", "rgb.npy", "depth_m.npy", "valid_mask.npy")}})
    cache_key = hash_json(cache_inputs); cache_root = ROOT / "artifacts/gt_cache/hm3d" / cap["scene_id"]
    cache_dir = cache_root / f"{cache_key[:24]}"; cache_root.mkdir(parents=True, exist_ok=True)
    import fcntl
    cache_lock_path = cache_root / ".cache.lock"
    with cache_lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        cache_reused = False
        cache_manifest = cache_dir / "cache_manifest.json"
        if cache_manifest.is_file() and load_json(cache_manifest).get("cache_key") == cache_key:
            render_index = load_json(cache_manifest)
            cache_reused = True
        else:
            stage = cache_root / f".stage-{out.name}-{uuid.uuid4().hex[:8]}"
            stage.mkdir()
            cmd = [args.habitat_python, "-B", str(Path(__file__).resolve()), "--render-semantic", "--run-root", str(run_root),
                   "--descriptor", str(descriptor_path), "--output", str(stage)]
            result = subprocess.run(cmd, cwd=ROOT, env=semantic_render_env(), text=True, capture_output=True)
            if result.returncode:
                render_validation_path = stage / "render_validation.json"
                if result.returncode == 3 and render_validation_path.is_file():
                    # Exit 3 is the renderer's explicit alignment-failed
                    # result, not a process crash. Preserve its per-frame
                    # measurements before discarding the incomplete cache.
                    rendered = load_json(render_validation_path)
                    write_json(out / "gt_validation.json", rendered)
                    failed_checks = sorted({name for frame in rendered.get("frames", [])
                                            for name, passed in frame.get("checks", {}).items()
                                            if not passed})
                    failure_summary = ("semantic render completed; alignment checks failed: " +
                                       (", ".join(failed_checks) if failed_checks else "see gt_validation.json"))
                    failure_status = "blocked_alignment"
                else:
                    failure_summary = renderer_failure_summary(result.returncode, result.stderr)
                    failure_status = "renderer_failed"
                # Preserve the failure reason in the evaluation directory.
                # The partial stage is still discarded because it is not a
                # valid GT cache, but diagnostics must survive a renderer
                # crash (for example an EGL/driver failure).
                write_json(out / "gt_renderer_failure.json", {
                    "status": failure_status,
                    "returncode": result.returncode,
                    "summary": failure_summary,
                    "backend": render_backend,
                    "stderr_tail": result.stderr[-4000:],
                    "stdout_tail": result.stdout[-2000:],
                })
                if failure_status == "blocked_alignment":
                    manifest.update({"status": "blocked_alignment", "finished_utc": utc_now(),
                                     "cache_key": cache_key, "alignment_failed_checks": failed_checks})
                    write_json(manifest_path, manifest)
                shutil.rmtree(stage)
                raise RuntimeError(f"{failure_summary}; diagnostics: {out / 'gt_renderer_failure.json'}" +
                                   (f" and {out / 'gt_validation.json'}" if failure_status == "blocked_alignment" else ""))
            rendered = load_json(stage / "render_validation.json")
            if rendered.get("status") != "pass":
                write_json(out / "gt_validation.json", rendered); shutil.rmtree(stage)
                manifest.update({"status": "blocked_alignment", "finished_utc": utc_now(), "cache_key": cache_key})
                write_json(manifest_path, manifest)
                raise RuntimeError("semantic GT alignment validation failed; scoring was not run")
            render_index = {"schema_version": 1, "cache_key": cache_key, "cache_inputs": cache_inputs,
                            "semantic_render_backend": render_backend,
                            "cache_created_utc": utc_now(), "render_validation": rendered,
                            "render_script_sha256": sha256(Path(__file__).resolve())}
            write_json(stage / "cache_manifest.json", render_index)
            if cache_dir.exists():
                shutil.rmtree(stage)
                render_index = load_json(cache_manifest)
                cache_reused = True
            else: os.replace(stage, cache_dir)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    render_report = render_index["render_validation"]
    write_json(out / "gt_validation.json", {"status": render_report["status"], "cache_reused": cache_reused,
                "semantic_render_backend": render_index.get("semantic_render_backend", SEMANTIC_RENDER_BACKEND),
                "cache_key": cache_key, "cache_path": str(cache_dir), "protocol_tolerances": PROTOCOL["render_validation"],
                "frames": [{k:v for k,v in row.items() if k != "visible_pixels_by_semantic_id"} for row in render_report["frames"]],
                "descriptor_instance_count": len(descriptors), "semantic_ids_outside_descriptor": sorted(set(sid for row in render_report["frames"] for sid in row["unknown_nonzero_semantic_ids"]))})
    predictions, d2_index = _read_detector(run_root)
    print("PROGRESS: 3/4 Đối chiếu mask, P3 ledger và lineage đã lưu", flush=True)
    p3_rows = {x["detection_id"]: x for x in read_jsonl(run_root / "detection_3d/object_evidence_ledger.jsonl")}
    graph_doc = load_json(run_root / "replay/snapshot.json"); graph = graph_doc.get("snapshot", graph_doc)
    object_nodes = [n for n in graph.get("nodes", []) if n.get("kind") == "object"]
    endpoint_lineage = {}
    for node in object_nodes:
        attrs = node.get("attributes", {}); oid = attrs.get("object_id") or node.get("node_id")
        for endpoint in (attrs.get("first_detection_id"), attrs.get("last_detection_id")):
            if endpoint: endpoint_lineage.setdefault(endpoint, set()).add(oid)
    mapping_categories = mapping_doc.get("categories", {})
    frame_metrics = []; observation_rows = []; review_rows = []; identity_pairs = []
    totals = Counter(); iou_values = []; confirmed_counts = Counter(); provisional_seen = Counter()
    category_coverage = defaultdict(Counter)
    archive_arch = set(PROTOCOL["ignored_policy"]["architectural_categories"] + PROTOCOL["ignored_policy"]["unknown_categories"])
    for rec in cap["frames"]:
        fid = str(rec["frame_id"]); frame_dir = run_root / "capture" / rec["frame_dir"]
        gt_path = cache_dir / "frames" / f"{fid}__semantic_id_map.npy"
        if not gt_path.is_file(): raise FileNotFoundError(f"cached semantic map missing: {gt_path}")
        gt_instances, label_map, status_by_sid = _read_gt_frame(gt_path, descriptors, mapping_doc)
        dets = predictions.get(fid, [])
        all_iou = np.zeros((len(dets), len(gt_instances)), dtype=np.float64)
        for i, det in enumerate(dets):
            for j, inst in enumerate(gt_instances): all_iou[i,j] = _mask_iou(det["mask"], inst["mask"])
        pairs = _assign(all_iou, float(PROTOCOL["matching"]["mask_iou_threshold"]))
        matched_d, matched_g = {i for i,_,_ in pairs}, {j for _,j,_ in pairs}
        pair_iou = {i:(j,v) for i,j,v in pairs}
        # Class-aware matching is confined to confirmed category rows with a validated detector-class mapping.
        eligible_gt_idx = [j for j,inst in enumerate(gt_instances) if inst["mapping_status"] == "confirmed" and inst["detector_class"]]
        eligible_det_idx = [i for i,det in enumerate(dets) if det.get("class_canonical") in {mapping_categories[gt_instances[j]["category"]]["detector_class"] for j in eligible_gt_idx}]
        aware_iou = np.zeros((len(eligible_det_idx),len(eligible_gt_idx)),dtype=np.float64)
        for ri,di in enumerate(eligible_det_idx):
            for cj,gj in enumerate(eligible_gt_idx):
                if dets[di].get("class_canonical") == gt_instances[gj]["detector_class"]: aware_iou[ri,cj]=all_iou[di,gj]
        aware_pairs = _assign(aware_iou,float(PROTOCOL["matching"]["mask_iou_threshold"]))
        aware_d={eligible_det_idx[i] for i,_,_ in aware_pairs}; aware_g={eligible_gt_idx[j] for _,j,_ in aware_pairs}
        cls_tp=len(aware_pairs); cls_fp=len(eligible_det_idx)-len(aware_d); cls_fn=len(eligible_gt_idx)-len(aware_g)
        # A prediction whose visible pixels mostly lie on ignored GT/background is not a trustworthy FP.
        ignored_pixels = np.isin(label_map, np.asarray([sid for sid,st in status_by_sid.items() if st in {"unsupported","unmapped"}], dtype=np.uint32))
        ignored_pixels |= label_map == 0
        class_agnostic_ignored = np.isin(label_map, np.asarray([sid for sid,cat in descriptors.items() if cat["category"] in archive_arch],dtype=np.uint32)) | (label_map == 0)
        frame_ignored_ca = 0; frame_ignored_cls = 0; ca_fp = len(dets)-len(matched_d); ca_fn=len(gt_instances)-len(matched_g)
        frame_p3_accepted = 0; frame_p3_rejected = 0
        for i,det in enumerate(dets):
            d_id=det["detection_id"]; p3=p3_rows.get(d_id,{"decision":"missing","reason":"not in P3 ledger"})
            gt_inst = gt_instances[pair_iou[i][0]] if i in pair_iou else None
            ca_outcome = "matched" if gt_inst else "false_positive"
            ca_ignored_fraction = None
            if i not in matched_d and det["mask"].any():
                area=int(det["mask"].sum()); ignored_frac=float(np.count_nonzero(det["mask"] & class_agnostic_ignored)/area)
                ca_ignored_fraction = ignored_frac
                if ignored_frac >= PROTOCOL["ignored_policy"]["prediction_ignore_fraction"]:
                    ca_fp-=1; frame_ignored_ca+=1; ca_outcome = "ignored"
            if i not in aware_d and i in eligible_det_idx and det["mask"].any():
                area=int(det["mask"].sum()); ignored_frac=float(np.count_nonzero(det["mask"] & ignored_pixels)/area)
                if ignored_frac >= PROTOCOL["ignored_policy"]["prediction_ignore_fraction"]:
                    cls_fp-=1; frame_ignored_cls+=1
            decision = {"detection_id": d_id, "frame_id": fid, "class_canonical":det.get("class_canonical"),
                        "p3_decision":p3.get("decision",p3.get("status","missing")),"p3_reason":p3.get("reason"),
                        "match_status":"matched" if gt_inst else "unmatched_or_ignored", "gt_semantic_id":gt_inst["semantic_id"] if gt_inst else None,
                        "gt_category":gt_inst["category"] if gt_inst else None,"mask_iou":pair_iou[i][1] if i in pair_iou else None,
                        "class_agnostic_outcome":ca_outcome,"class_agnostic_ignored_fraction":ca_ignored_fraction,
                        "object_node_endpoint_ids":sorted(endpoint_lineage.get(d_id,set())),"lineage_status":"exact_snapshot_endpoint" if d_id in endpoint_lineage else "unavailable_for_detection",
                        "class_mapping_status":mapping_categories.get(gt_inst["category"],{}).get("status","unmapped") if gt_inst else None}
            observation_rows.append(decision)
            if gt_inst:
                totals["p3_matched_gt_observations"] += 1
                if p3.get("decision") == "accepted": totals["p3_accepted_matched_gt"]+=1
                if p3.get("decision") == "accepted": frame_p3_accepted += 1
                if p3.get("decision") == "rejected": totals["p3_rejected_matched_gt"]+=1; frame_p3_rejected += 1
                iou_values.append(float(pair_iou[i][1]))
                if d_id in endpoint_lineage:
                    for oid in endpoint_lineage[d_id]: identity_pairs.append({"gt_instance_id":gt_inst["semantic_id"],"predicted_object_id":oid,"detection_id":d_id,"frame_id":fid})
            elif p3.get("decision") in {"accepted","rejected"}:
                totals[f"p3_{p3['decision']}_unmatched_or_uncertain"]+=1
            gt_mask=gt_inst["mask"] if gt_inst else None
            rgb=np.load(frame_dir/"rgb.npy",allow_pickle=False)
            image_rel=f"images/{fid}__{d_id.rsplit(':',1)[-1]}.png"
            _overlay(rgb,det["mask"],gt_mask,out/"review"/image_rel)
            review_rows.append({"title":f"{fid} · {d_id} · {det.get('class_canonical')} · P3 {p3.get('decision',p3.get('status','missing'))}","image":image_rel,"details":decision | {"score":det.get("score_raw"),"bbox_xyxy":det.get("bbox_xyxy"),"p3":p3}})
        for j, inst in enumerate(gt_instances):
            category_coverage[inst["category"]]["visible"] += 1
            if j in matched_g:
                category_coverage[inst["category"]]["mask_matched"] += 1
            else:
                category_coverage[inst["category"]]["unmatched"] += 1
                if inst["area"] >= 128:
                    image_rel = f"images/{fid}__missed_gt_{inst['semantic_id']:06d}.png"
                    _overlay(np.load(frame_dir/"rgb.npy",allow_pickle=False), np.zeros_like(inst["mask"]), inst["mask"], out/"review"/image_rel)
                    review_rows.append({"title":f"{fid} · missed GT {inst['semantic_id']} · {inst['category']}","image":image_rel,
                                        "details":{"review_status":"GT instance unmatched by any prediction","frame_id":fid,
                                                   "semantic_id":inst["semantic_id"],"category":inst["category"],"visible_pixels":inst["area"],
                                                   "mapping_status":inst["mapping_status"],"minimum_review_area_pixels":128}})
        totals["ca_tp"]+=len(pairs);totals["ca_fp"]+=max(0,ca_fp);totals["ca_fn"]+=ca_fn;totals["ca_ignored_predictions"]+=frame_ignored_ca
        totals["class_tp"]+=cls_tp;totals["class_fp"]+=max(0,cls_fp);totals["class_fn"]+=cls_fn;totals["class_ignored_predictions"]+=frame_ignored_cls
        totals["gt_visible_instances"]+=len(gt_instances);totals["prediction_observations"]+=len(dets)
        for inst in gt_instances:
            if inst["mapping_status"] == "confirmed": confirmed_counts[inst["category"]]+=1
            elif inst["mapping_status"] == "provisional": provisional_seen[inst["category"]]+=1
        frame_metrics.append({"frame_id":fid,"gt_instances":len(gt_instances),"predictions":len(dets),"class_agnostic_matches":len(pairs),
                             "class_agnostic_fp":max(0,ca_fp),"class_agnostic_fn":ca_fn,
                             "class_aware_matches":cls_tp,"class_agnostic_ignored_predictions":frame_ignored_ca,"class_aware_ignored_predictions":frame_ignored_cls,
                             "matched_p3_accepted":frame_p3_accepted,"matched_p3_rejected":frame_p3_rejected})
    ca = _metric(totals["ca_tp"],totals["ca_fp"],totals["ca_fn"],scope="all-instance coverage diagnostic: visible labeled non-architectural HM3D object observations; category-agnostic")
    mapping_has_confirmed = bool(mapping_doc.get("categories")) and any(mapping_is_confirmed(x) for x in mapping_doc["categories"].values())
    cls = _metric(totals["class_tp"],totals["class_fp"],totals["class_fn"],scope="confirmed HM3D category mappings only",
                  available=mapping_has_confirmed,reason="no human-confirmed category mappings; see category_mapping_snapshot.json")
    if not mapping_has_confirmed: cls.update({"status":"unavailable","tp":None,"fp":None,"fn":None,"precision":None,"recall":None,"f1":None})
    p3matched=totals["p3_accepted_matched_gt"]+totals["p3_rejected_matched_gt"]
    matched_p3_rows=[row for row in observation_rows if row["match_status"]=="matched"]
    p3_rejection_reasons=Counter(str(row["p3_reason"] or "unspecified") for row in matched_p3_rows if row["p3_decision"]=="rejected")
    p3_summary={"matched_gt_observations":p3matched,"accepted":totals["p3_accepted_matched_gt"],"rejected":totals["p3_rejected_matched_gt"],
                "accepted_rate_among_matched_gt":totals["p3_accepted_matched_gt"]/p3matched if p3matched else None,
                "rejection_reasons_among_matched_gt":dict(p3_rejection_reasons),
                "unmatched_or_uncertain_accepted":totals["p3_accepted_unmatched_or_uncertain"],"unmatched_or_uncertain_rejected":totals["p3_rejected_unmatched_or_uncertain"],
                "note":"Acceptance rate is not accuracy; matching is based on Detection2D mask to semantic GT visible instance."}
    gt_to_pred=defaultdict(set); pred_to_gt=defaultdict(set)
    for row in identity_pairs: gt_to_pred[row["gt_instance_id"]].add(row["predicted_object_id"]);pred_to_gt[row["predicted_object_id"]].add(row["gt_instance_id"])
    association={"lineage_completeness":"incomplete_endpoint_only","lineage_source":"snapshot first_detection_id/last_detection_id; no per-frame M6 ledger persisted",
                 "fragmentation_candidates":[{"gt_semantic_id":k,"predicted_object_ids":sorted(v)} for k,v in gt_to_pred.items() if len(v)>1],
                 "false_merge_candidates":[{"predicted_object_id":k,"gt_semantic_ids":sorted(v)} for k,v in pred_to_gt.items() if len(v)>1],
                 "identity_pairs_scored":len(identity_pairs),"suppression_vs_match":"unavailable: predictor artifacts do not persist per-detection M6 decision; no association rerun performed"}
    # Export stable CSV/JSONL artifacts.
    with (out/"observation_matches.jsonl").open("w",encoding="utf-8") as f:
        for row in observation_rows:f.write(json.dumps(row,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n")
    metrics={"protocol_id":PROTOCOL_VERSION,"detection2d":{"class_agnostic":ca,"class_aware_confirmed_mapping":cls,
             "matched_mask_iou_mean":float(np.mean(iou_values)) if iou_values else None,"matched_mask_iou_count":len(iou_values),
             "predictions_ignored_class_agnostic":totals["ca_ignored_predictions"],"predictions_ignored_class_aware":totals["class_ignored_predictions"],
             "visible_gt_instances":totals["gt_visible_instances"],"prediction_observations":totals["prediction_observations"],
             "per_gt_category_coverage_diagnostic":{k:dict(v) for k,v in sorted(category_coverage.items())},
             "provisional_gt_instance_counts":dict(provisional_seen),"confirmed_gt_instance_counts":dict(confirmed_counts)},
             "p3":p3_summary,"association":association,"frames":frame_metrics,
             "scope_note":"Observation-level mask diagnostics for visible labeled non-architectural semantic instances (unknown/void/architectural and <32px GT excluded). All-instance coverage is not supported-vocabulary YOLO recall, AP/mAP, or room accuracy."}
    write_json(out/"metrics.json",metrics);write_json(out/"association_audit.json",association)
    with (out/"metrics.csv").open("w",encoding="utf-8",newline="") as f:
        writer=csv.writer(f);writer.writerow(["metric","value","scope"])
        for key,val in (("ca_tp",totals["ca_tp"]),("ca_fp",totals["ca_fp"]),("ca_fn",totals["ca_fn"]),("ca_precision",ca["precision"]),("ca_recall",ca["recall"]),("ca_f1",ca["f1"]),("mean_matched_mask_iou",metrics["detection2d"]["matched_mask_iou_mean"]),("p3_acceptance_rate_on_matched_gt",p3_summary["accepted_rate_among_matched_gt"])):writer.writerow([key,val,"observation-level; HM3D semantic instance annotation"])
    print("PROGRESS: 4/4 Xuất bảng metrics, observation review và manifest", flush=True)
    _render_html(out,review_rows)
    category_inventory=Counter(row["category"] for row in descriptors.values())
    visible_categories=Counter(inst["category"] for rec in cap["frames"] for inst in _read_gt_frame(cache_dir/"frames"/f"{rec['frame_id']}__semantic_id_map.npy",descriptors,mapping_doc)[0])
    mapping_review={"scene_id":cap["scene_id"],"categories_in_descriptor":dict(category_inventory),"categories_visible_in_capture":dict(visible_categories),
                    "mapping_status_counts":dict(Counter("confirmed" if mapping_is_confirmed(x) else x.get("status","unmapped") for x in mapping_doc.get("categories",{}).values())),
                    "review_instructions":"Review only categories visible in capture. Confirm only if the HM3D category and detector canonical class are semantically equivalent; otherwise leave provisional/unmapped/unsupported. Record reviewer/date/provenance.",
                    "mapping_file":str(mapping_path) if mapping_path else None}
    write_json(out/"mapping_review.json",mapping_review)
    report=f'''# Đánh giá Object GT — {pf['run_id']}\n\n- Evaluation ID: `{out.name}`\n- Scene: `{cap['scene_id']}`; capture: {cap['frame_count']} frame ở {cap['resolution_px'][0]}×{cap['resolution_px'][1]}\n- Semantic GT: HM3D evaluator-only instance IDs; không được đưa vào predictor.\n- Render/alignment: **{render_report['status']}**, cache {'reused' if cache_reused else 'generated'} `{cache_key[:24]}`.\n- Detection2D mask IoU threshold: 0,50; minimum GT visible pixels: 32; Hungarian one-to-one per frame.\n\n## Metrics\n\n- Class-agnostic visible-instance diagnostic: TP={ca['tp']}, FP={ca['fp']}, FN={ca['fn']}, P={ca['precision']}, R={ca['recall']}, F1={ca['f1']}. Đây không phải class-aware recall hay AP/mAP.\n- Class-aware: **{cls['status']}** — {cls.get('unavailable_reason') or 'only confirmed mappings'}; current provisional mappings are excluded.\n- Mean IoU of matched masks: {metrics['detection2d']['matched_mask_iou_mean']}; n={metrics['detection2d']['matched_mask_iou_count']}.\n- P3 on matched GT observations: accepted {p3_summary['accepted']}, rejected {p3_summary['rejected']}, acceptance rate {p3_summary['accepted_rate_among_matched_gt']}; acceptance is not accuracy.\n- Association: endpoint-only/incomplete. Suppression vs match is unavailable because this run did not persist per-detection M6 decisions; evaluator did not rerun M6.\n- Ignored prediction policy: prediction is ignored (not FP) when at least 50% of its pixels fall in void/architectural areas (class-agnostic), or unsupported/unmapped/void areas (class-aware).\n\n## Review\n\nOpen `review/index.html` for all detections, green prediction masks, magenta semantic GT mask, per-frame IoU, P3 status and any exact snapshot endpoint lineage. `mapping_review.json` lists categories in view; edit the separate mapping config only after human review, then rerun to a new evaluation ID.\n\n## Limits\n\nHM3D semantic object IDs/categories are not human-verified detector vocabulary mappings or room GT. Class-aware metrics are withheld until category mappings are confirmed with reviewer provenance. Unique-instance identity metrics are candidates from persisted endpoint lineage only; intermediate/suppression decisions are unavailable. This report does not evaluate room boundaries or functional room labels.\n'''
    (out/"report.vi.md").write_text(report,encoding="utf-8")
    predictor_hashes_after=predictor_content_hashes(run_root)
    predictor_inputs_unchanged=pf["input_hashes"]==predictor_hashes_after
    manifest.update({"status":"completed" if predictor_inputs_unchanged else "integrity_error",
                     "finished_utc":utc_now(),"preflight_status":pf["status"],"cache_key":cache_key,
                     "cache_path":str(cache_dir),"cache_reused":cache_reused,"protocol_sha256":hash_json(PROTOCOL),
                     "mapping_sha256":sha256(mapping_path) if mapping_path and mapping_path.is_file() else None,"predictor_input_hashes_after":predictor_hashes_after,
                     "predictor_inputs_unchanged":predictor_inputs_unchanged,
                     "artifacts":{"protocol":"protocol.json","mapping_snapshot":"category_mapping_snapshot.json","mapping_review":"mapping_review.json",
                                  "gt_validation":"gt_validation.json","metrics":"metrics.json","metrics_csv":"metrics.csv",
                                  "matches":"observation_matches.jsonl","association_audit":"association_audit.json","review":"review/index.html","report":"report.vi.md"},
                     "summary":{"class_agnostic":ca,"class_aware":cls,"p3":p3_summary,"association_lineage":"incomplete_endpoint_only"}})
    write_json(manifest_path,manifest)
    if not predictor_inputs_unchanged: raise RuntimeError("predictor input hashes changed during evaluation")
    print("PROGRESS: Hoàn tất evaluation", flush=True)
    return manifest


def _completed_evaluations(run_root: Path) -> list[tuple[str, Path, dict]]:
    found = []
    root = run_root / "object_evaluations"
    if not root.is_dir(): return found
    for folder in root.iterdir():
        manifest_path = folder / "manifest.json"
        if not folder.is_dir() or not manifest_path.is_file(): continue
        try: manifest = load_json(manifest_path)
        except Exception: continue
        if manifest.get("status") == "completed":
            found.append((str(manifest.get("finished_utc", "")), folder, manifest))
    return sorted(found, key=lambda row: (row[0], row[1].name))


def _classify_legacy_unmatched(run_root: Path, evaluation_root: Path,
                               rows: list[dict]) -> dict[str, dict]:
    """Recover only ignored-vs-FP status for old exports, using their exact saved masks/GT cache and protocol."""
    unmatched = [row for row in rows if row.get("match_status") != "matched" and
                 not row.get("class_agnostic_outcome")]
    if not unmatched: return {}
    validation_path = evaluation_root / "gt_validation.json"
    if not validation_path.is_file(): return {}
    validation = load_json(validation_path); cache_root = Path(validation.get("cache_path", ""))
    if not cache_root.is_dir(): return {}
    cap = load_json(run_root / "capture/capture_manifest.json")
    profile = load_json(run_root / "effective_evaluation_profile.json")
    descriptor_path = resolve_evaluator_asset(profile.get("dataset", {}).get("semantic_descriptor_evaluator_only"))
    if descriptor_path is None:
        return {}
    if not descriptor_path.is_file(): return {}
    descriptors = parse_descriptor(descriptor_path)
    arch = {sid for sid, item in descriptors.items()
            if item["category"] in set(PROTOCOL["ignored_policy"]["architectural_categories"] + PROTOCOL["ignored_policy"]["unknown_categories"])}
    predictions, _ = _read_detector(run_root)
    by_frame = {str(rec["frame_id"]): rec for rec in cap.get("frames", [])}
    label_cache: dict[str, np.ndarray] = {}; statuses = {}
    for row in unmatched:
        fid = str(row.get("frame_id")); did = row.get("detection_id")
        rec = by_frame.get(fid); det = next((d for d in predictions.get(fid, []) if d.get("detection_id") == did), None)
        gt_path = cache_root / "frames" / f"{fid}__semantic_id_map.npy"
        if rec is None or det is None or not gt_path.is_file() or not det["mask"].any(): continue
        if fid not in label_cache: label_cache[fid] = np.load(gt_path, allow_pickle=False).astype(np.uint32)
        labels = label_cache[fid]
        if labels.shape != det["mask"].shape: continue
        ignored_mask = np.isin(labels, np.asarray(sorted(arch), dtype=np.uint32)) | (labels == 0)
        fraction = float(np.count_nonzero(det["mask"] & ignored_mask) / int(det["mask"].sum()))
        outcome = "ignored" if fraction >= PROTOCOL["ignored_policy"]["prediction_ignore_fraction"] else "false_positive"
        statuses[did] = {"class_agnostic_outcome": outcome, "class_agnostic_ignored_fraction": fraction,
                         "outcome_source": "reconstructed from saved detection mask + validated semantic GT cache using saved protocol"}
    return statuses


def load_object_results(run_root: Path, *, evaluation_id: str | None = None,
                        frame_index: int | None = None) -> dict:
    choices = _completed_evaluations(run_root)
    if evaluation_id is not None:
        selected = next((row for row in choices if row[1].name == evaluation_id), None)
        if selected is None:
            raise ValueError(f"evaluation `{evaluation_id}` không completed/hợp lệ; không đọc failed hoặc superseded")
    else:
        if not choices: raise FileNotFoundError("run chưa có object evaluation completed hợp lệ")
        selected = choices[-1]
    _, eval_root, manifest = selected
    metrics_path = eval_root / "metrics.json"; matches_path = eval_root / "observation_matches.jsonl"
    if not metrics_path.is_file(): raise FileNotFoundError(f"evaluation thiếu metrics.json: {metrics_path}")
    if not matches_path.is_file(): raise FileNotFoundError(f"evaluation thiếu observation_matches.jsonl: {matches_path}")
    metrics = load_json(metrics_path); d2 = metrics.get("detection2d", {})
    ca = d2.get("class_agnostic")
    if not isinstance(ca, dict): raise ValueError("metrics thiếu detection2d.class_agnostic")
    capture = load_json(run_root / "capture/capture_manifest.json")
    frame_ids = [str(row["frame_id"]) for row in capture.get("frames", [])]
    metric_rows = {str(row.get("frame_id")): row for row in metrics.get("frames", [])}
    if len(metric_rows) != len(metrics.get("frames", [])): raise ValueError("metrics có frame_id trùng")
    if set(frame_ids) != set(metric_rows): raise ValueError("capture frame IDs và metrics frame IDs không khớp")
    match_rows = [json.loads(line) for line in matches_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_frame: dict[str, list[dict]] = defaultdict(list)
    for row in match_rows: by_frame[str(row.get("frame_id"))].append(row)
    legacy_outcomes = _classify_legacy_unmatched(run_root, eval_root, match_rows)
    frame_output = []
    p3_summary = metrics.get("p3", {})
    for index, fid in enumerate(frame_ids):
        src = metric_rows[fid]
        gt = src.get("gt_instances"); pred = src.get("predictions"); matched = src.get("class_agnostic_matches")
        ignored = src.get("class_agnostic_ignored_predictions")
        fp = src.get("class_agnostic_fp")
        if fp is None and all(isinstance(x, int) for x in (pred, matched, ignored)):
            fp = pred - matched - ignored
        fn = src.get("class_agnostic_fn")
        if fn is None and isinstance(gt, int) and isinstance(matched, int): fn = gt - matched
        rows = by_frame.get(fid, [])
        accepted = sum(row.get("match_status") == "matched" and row.get("p3_decision") == "accepted" for row in rows)
        rejected = sum(row.get("match_status") == "matched" and row.get("p3_decision") == "rejected" for row in rows)
        frame_output.append({"frame_index": index, "frame_id": fid, "eligible_gt": gt, "predictions": pred,
                             "matched": matched, "false_positives": fp, "ignored": ignored, "false_negatives": fn,
                             "matched_p3_accepted": accepted if p3_summary else None,
                             "matched_p3_rejected": rejected if p3_summary else None})
    total = {key: sum(row[key] for row in frame_output) for key in ("eligible_gt", "predictions", "matched", "false_positives", "ignored", "false_negatives")
             if all(isinstance(row[key], int) for row in frame_output)}
    expected = {"eligible_gt": d2.get("visible_gt_instances"), "predictions": d2.get("prediction_observations"),
                "matched": ca.get("tp"), "false_positives": ca.get("fp"), "ignored": d2.get("predictions_ignored_class_agnostic"),
                "false_negatives": ca.get("fn")}
    consistency = {key: total.get(key) == value for key, value in expected.items() if isinstance(value, int)}
    if not all(consistency.values()): raise ValueError(f"per-frame counts do not sum to saved metrics: {consistency}")
    details = None
    if frame_index is not None:
        if frame_index < 0 or frame_index >= len(frame_output): raise ValueError(f"frame index ngoài phạm vi 0..{len(frame_output)-1}")
        summary = frame_output[frame_index]; fid = summary["frame_id"]
        rows = by_frame.get(fid, [])
        missing = [row for row in rows if row.get("match_status") != "matched" and not row.get("class_agnostic_outcome")]
        recovery = _classify_legacy_unmatched(run_root, eval_root, missing)
        details = []
        for row in rows:
            did = str(row.get("detection_id", "")); outcome = row.get("class_agnostic_outcome")
            outcome_doc = {"class_agnostic_outcome": outcome} if outcome else recovery.get(did, legacy_outcomes.get(did, {}))
            if row.get("match_status") == "matched": outcome_doc = {"class_agnostic_outcome": "matched"}
            filename = f"{fid}__{did.rsplit(':', 1)[-1]}.png"
            image_path = eval_root / "review/images" / filename
            details.append({"detection_id": did.rsplit(":", 1)[-1] or "unavailable", "predicted_class": row.get("class_canonical") or "N/A",
                            "gt_category": row.get("gt_category") or "N/A", "gt_instance_id": row.get("gt_semantic_id"),
                            "mask_iou": row.get("mask_iou"),
                            "status": outcome_doc.get("class_agnostic_outcome", "unavailable: legacy export did not persist this classification"),
                            "status_note": outcome_doc.get("outcome_source"),
                            "p3_decision": row.get("p3_decision") or "N/A", "p3_reason": row.get("p3_reason") or "N/A",
                            "object_nodes": row.get("object_node_endpoint_ids") or "unavailable (no persisted snapshot endpoint)",
                            "lineage_status": row.get("lineage_status", "unavailable"),
                            "review_image": str(image_path) if image_path.is_file() else "unavailable"})
    return {"status": "ok", "run_id": manifest.get("run_id"), "evaluation_id": eval_root.name,
            "evaluation_finished_utc": manifest.get("finished_utc"), "scene_id": capture.get("scene_id"),
            "resolution_px": capture.get("resolution_px"), "frame_count": len(frame_output),
            "diagnostic_scope": ca.get("scope", "class-agnostic diagnostic"),
            "class_aware": d2.get("class_aware_confirmed_mapping", {"status": "unavailable", "unavailable_reason": "missing metric"}),
            "totals": total, "mean_matched_mask_iou": d2.get("matched_mask_iou_mean"),
            "mean_iou_count": d2.get("matched_mask_iou_count"), "p3": p3_summary,
            "frames": frame_output, "frame_index_base": 0, "frame_details": details,
            "frame_counts_match_saved_metrics": consistency, "evaluation_root": str(eval_root)}


def format_object_results(result: dict, *, frames: bool = False, frame: int | None = None) -> str:
    if result.get("status") != "ok": raise ValueError("object-results input invalid")
    lines = [f"Object GT results — {result['run_id']} / {result['evaluation_id']}",
             f"Scene: {result.get('scene_id')} | {result.get('resolution_px')} px | frames: {result.get('frame_count')}"]
    if frame is not None:
        row = result["frames"][frame]
        lines.append(f"Frame index: {frame} (0-based) | source frame: {row['frame_id']}")
        lines.append("Status/GT match dùng class-agnostic mask protocol; matched không khẳng định đúng class hoặc cùng identity vật lý.")
        lines.append(f"Review images: {result['evaluation_root']}/review/images/{row['frame_id']}__*.png")
        if not result["frame_details"]:
            lines.append("Không có detection trong frame này.")
        else:
            lines.append("Detection | Class | GT category/ID | Mask IoU | Status | P3 decision/reason | ObjectNode")
            for row in result["frame_details"]:
                gt_text = f"{row['gt_category']}/{row['gt_instance_id']}" if row["gt_instance_id"] is not None else "N/A (no matched GT)"
                iou_text = str(row["mask_iou"]) if row["mask_iou"] is not None else "N/A (no matched GT)"
                lines.append(f"{row['detection_id']} | {row['predicted_class']} | {gt_text} | {iou_text} | {row['status']} | {row['p3_decision']}/{row['p3_reason']} | {', '.join(row['object_nodes']) if isinstance(row['object_nodes'], list) else row['object_nodes']}")
                if row.get("status_note"): lines.append(f"  Status source: {row['status_note']}")
                if row.get("review_image") != "unavailable": lines.append(f"  Image: {row['review_image']}")
                if row.get("lineage_status") != "exact_snapshot_endpoint": lines.append(f"  Lineage: {row.get('lineage_status') or 'unavailable'}")
        return "\n".join(lines)
    if frames:
        lines.append("Frame index bắt đầu từ 0; các count là observation/frame-level, không phải unique objects.")
        headers = ("Frame", "GT eligible", "Pred", "Matched", "FP", "Ignored", "FN", "P3 accepted", "P3 rejected")
        widths = (5, 12, 5, 8, 5, 8, 5, 11, 11)
        lines.append(" | ".join(label.ljust(width) for label, width in zip(headers, widths)))
        lines.append("-+-".join("-" * width for width in widths))
        for row in result["frames"]:
            values = [row.get(key) for key in ("frame_index", "eligible_gt", "predictions", "matched", "false_positives", "ignored", "false_negatives", "matched_p3_accepted", "matched_p3_rejected")]
            rendered = ["N/A" if value is None else str(value) for value in values]
            lines.append(" | ".join(value.rjust(width) for value, width in zip(rendered, widths)))
        return "\n".join(lines)
    totals = result["totals"]; p3 = result.get("p3", {})
    lines += [f"Scope: {result.get('diagnostic_scope')} (mask match không đồng nghĩa đúng class hay đúng identity).",
              "TOTAL OBSERVATIONS",
              f"  Eligible GT observations       {totals.get('eligible_gt', 'N/A')}",
              f"  Detection observations         {totals.get('predictions', 'N/A')}",
              f"  Matched masks                  {totals.get('matched', 'N/A')}",
              f"  False positives                {totals.get('false_positives', 'N/A')}",
              f"  Ignored predictions            {totals.get('ignored', 'N/A')}",
              f"  False negatives                {totals.get('false_negatives', 'N/A')}",
              f"  Mean matched-mask IoU          {result.get('mean_matched_mask_iou') if result.get('mean_matched_mask_iou') is not None else 'N/A (no matched masks)'}",
              f"  Matched P3 accepted / rejected {p3.get('accepted', 'N/A (saved P3 metrics unavailable)')} / {p3.get('rejected', 'N/A (saved P3 metrics unavailable)')}"]
    aware = result.get("class_aware", {})
    if aware.get("status") != "available":
        lines.append(f"Class-aware metrics: N/A ({aware.get('unavailable_reason') or 'mapping not confirmed'})")
    lines.append(f"Frame totals consistent with saved metrics: {'YES' if all(result.get('frame_counts_match_saved_metrics', {}).values()) else 'NO'}")
    lines.append(f"Frame index starts at 0; counts are observations, not unique object count.\nOutput: {result['evaluation_root']}")
    return "\n".join(lines)


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--run-root",type=Path)
    ap.add_argument("--check",action="store_true")
    ap.add_argument("--json",action="store_true",help="print full machine-readable JSON for --check or --object-results")
    ap.add_argument("--object-results",action="store_true",help=argparse.SUPPRESS)
    ap.add_argument("--eval-id")
    ap.add_argument("--frames",action="store_true")
    ap.add_argument("--frame",type=int)
    ap.add_argument("--output",type=Path)
    ap.add_argument("--mapping",type=Path,default=None,help="override scene mapping; defaults to the mapping in the run's evaluation profile")
    ap.add_argument("--habitat-python",default="")
    ap.add_argument("--render-semantic",action="store_true",help=argparse.SUPPRESS)
    ap.add_argument("--descriptor")
    args=ap.parse_args()
    try:
        if args.render_semantic: return render_semantic(args)
        if args.run_root is None: ap.error("--run-root required")
        if args.object_results:
            if args.check or args.output or args.habitat_python or args.descriptor:
                ap.error("--object-results cannot be combined with evaluation/render options")
            if args.frames and args.frame is not None: ap.error("choose only one of --frames or --frame")
            result=load_object_results(args.run_root.resolve(),evaluation_id=args.eval_id,frame_index=args.frame)
            print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False) if args.json else
                  format_object_results(result,frames=args.frames,frame=args.frame))
            return 0
        if args.check:
            if args.output or args.habitat_python or args.descriptor or args.eval_id or args.frames or args.frame is not None:
                ap.error("--check cannot be combined with evaluation output/frame options")
            result=preflight(args.run_root.resolve(),args.mapping.resolve() if args.mapping else None)
            print(json.dumps(result,indent=2,ensure_ascii=False) if args.json else summarize_preflight(result))
            return 0 if result["ready_for_render"] else 2
        if args.json or args.eval_id or args.frames or args.frame is not None:
            ap.error("--json, --eval-id, --frames, and --frame are only valid with --check or --object-results")
        if args.output is None or not args.habitat_python: ap.error("--output and --habitat-python required unless --check")
        result=evaluate(args);print(json.dumps(result,indent=2,ensure_ascii=False));return 0
    except Exception as exc:
        print(f"object GT evaluation error: {type(exc).__name__}: {exc}",file=sys.stderr);return 1


if __name__=="__main__":raise SystemExit(main())
