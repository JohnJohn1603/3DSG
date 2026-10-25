#!/usr/bin/env python3
"""Run one approved experimental assumed-XY T3 replay over locked T1.1 prefixes.

This reads existing mapper/capture artifacts only. It does not run capture,
perception, Qwen, ConceptGraphs mapping, or OccuSG replay.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Import shared source validation, adapter construction, and the single RB1 runner.
from scripts import run_t1_1_t3_room_belief as adapter_module
from scripts.build_t1_1_t3_room_belief_viewer import build as build_t3_viewer
from scripts.run_causal_hybrid_room_belief import (
    _load_effective_semantic_config, load_a_tables, run as run_causal_rb1,
    sha as rb_sha, validate_assumed_scope_declaration,
)

EXPECTED_RUN_ID = "hm3d00802_t1_1_all13_mapper_only_20261019_v1"
EXPECTED_FRAMES = [0, 26, 33, 40, 50, 61, 68, 75, 82, 89, 96, 103, 110]
POSE_CONVERSION = "T_W_Copt = T_W_H @ T_H_S @ diag(1,-1,-1)"


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def spatial_preflight(validated: dict[str, Any]) -> dict[str, Any]:
    """Reject known unit/frame/transform contradictions before semantic replay."""
    if validated["manifest"].get("run_id") != EXPECTED_RUN_ID:
        raise ValueError("this approved experiment is restricted to the exact T1.1 mapper-only run")
    if [int(x) for x in validated["manifest"].get("ordered_frame_indices", [])] != EXPECTED_FRAMES:
        raise ValueError("mapper source does not have the approved exact 13-frame prefix sequence")
    optical_basis = np.diag([1.0, -1.0, -1.0, 1.0])
    room_config_hashes: set[str] = set()
    frame_checks = []
    all_clouds = 0
    for row in validated["source_packet_rows"]:
        frame = int(row["frame_index"])
        raw_dir = Path(row["raw_source_path"])
        metadata_path = raw_dir / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        packet = json.loads(Path(row["packet_path"]).read_text(encoding="utf-8"))
        object_snapshot = json.loads(Path(row["object_snapshot_path"]).read_text(encoding="utf-8"))
        room_doc = json.loads(Path(row["room_snapshot_path"]).read_text(encoding="utf-8"))
        room_snapshot = room_doc.get("snapshot", room_doc)
        required_identity = (validated["manifest"]["scene_id"], validated["manifest"]["sequence_id"],
                             row["frame_id"], frame)
        metadata_identity = (metadata.get("scene_id"), metadata.get("sequence_id"),
                             metadata.get("frame_id"), metadata.get("frame_index"))
        if metadata_identity != required_identity:
            raise ValueError(f"capture metadata identity mismatch at frame {frame}")
        if (metadata.get("world_frame_canonical") != "Z-up; C.x=H.x,C.y=-H.z,C.z=H.y" or
            metadata.get("depth_units") != "m" or metadata.get("depth_representation") != "axial" or
            metadata.get("camera_optical") != "+X right,+Y down,+Z forward"):
            raise ValueError(f"capture coordinate/depth contract mismatch at frame {frame}")
        if metadata.get("width") != 640 or metadata.get("height") != 480:
            raise ValueError(f"capture resolution mismatch at frame {frame}")
        raw_hashes = packet.get("raw_keyframe_source", {}).get("files_sha256", {})
        if raw_hashes.get("metadata.json") != adapter_module.sha(metadata_path):
            raise ValueError(f"capture metadata hash mismatch at frame {frame}")
        sensor_pose = np.asarray(metadata.get("sensor_pose_canonical"), dtype=np.float64)
        object_pose = np.asarray(object_snapshot.get("camera_pose_W_Copt"), dtype=np.float64)
        if sensor_pose.shape != (4, 4) or object_pose.shape != (4, 4) or not np.isfinite(sensor_pose).all() or not np.isfinite(object_pose).all():
            raise ValueError(f"capture/object pose is malformed at frame {frame}")
        pose_error = float(np.max(np.abs(sensor_pose @ optical_basis - object_pose)))
        if pose_error > 1e-5:
            raise ValueError(f"known capture/object transform mismatch at frame {frame}: {pose_error}")
        if (room_snapshot.get("scene_id"), room_snapshot.get("sequence_id")) != required_identity[:2]:
            raise ValueError(f"room revision scene/sequence mismatch at frame {frame}")
        config_hash = room_snapshot.get("config_hash")
        if not isinstance(config_hash, str) or not config_hash:
            raise ValueError(f"room source config hash missing at frame {frame}")
        room_config_hashes.add(config_hash)
        room_count = 0
        for node in room_snapshot.get("nodes", []):
            if node.get("kind") != "room":
                continue
            room_count += 1
            attrs = node.get("attributes", {})
            explicit_frame = attrs.get("coordinate_frame") or attrs.get("frame_id")
            explicit_units = attrs.get("units")
            if explicit_frame is not None and str(explicit_frame) not in {"world_xy", "canonical_world_xy", "capture/world metric meters, Z-up"}:
                raise ValueError(f"room revision has known coordinate-frame conflict at frame {frame}: {explicit_frame}")
            if explicit_units is not None and str(explicit_units).lower() not in {"m", "meter", "meters"}:
                raise ValueError(f"room revision has known unit conflict at frame {frame}: {explicit_units}")
            ring = np.asarray(attrs.get("outer_ring", []), dtype=np.float64)
            if ring.ndim != 2 or ring.shape[1] != 2 or len(ring) < 3 or not np.isfinite(ring).all():
                raise ValueError(f"room polygon geometry is malformed at frame {frame}")
        if room_count == 0:
            raise ValueError(f"room revision has no room polygons at frame {frame}")
        object_count = 0
        for obj in object_snapshot.get("objects", []):
            cloud_path = validated["mapper_run"] / "branches/objects" / obj["cloud_npz"]
            cloud = np.load(cloud_path, allow_pickle=False)
            xyz = np.asarray(cloud["xyz"])
            if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
                raise ValueError(f"object cloud XYZ is malformed/non-finite at frame {frame}: {obj.get('hypothesis_id')}")
            object_count += 1
        all_clouds += object_count
        frame_checks.append({
            "frame_index": frame, "frame_id": row["frame_id"],
            "object_revision": row["object_revision"], "room_revision": row["room_revision"],
            "unified_revision": row["unified_revision"], "camera_transform_max_abs_error": pose_error,
            "depth_units": metadata["depth_units"], "depth_representation": metadata["depth_representation"],
            "resolution": [metadata["width"], metadata["height"]],
            "room_config_hash": config_hash, "room_count": room_count, "object_hypothesis_count": object_count,
        })
    if len(room_config_hashes) != 1:
        raise ValueError("room prefix revisions have inconsistent source config hashes")
    capture_manifest_path = validated["raw_capture_run_root"] / "run_manifest.json"
    capture_manifest = json.loads(capture_manifest_path.read_text(encoding="utf-8"))
    if (capture_manifest.get("scene_id"), capture_manifest.get("sequence_id")) != (
            validated["manifest"].get("scene_id"), validated["manifest"].get("sequence_id")):
        raise ValueError("capture and mapper scene/sequence provenance conflict")
    return {
        "status": "passed_no_known_coordinate_unit_transform_conflict",
        "scope_result": "still_assumed_not_verified; room revisions contain XY polygons without elevation metadata",
        "world_frame_canonical": "Z-up; C.x=H.x,C.y=-H.z,C.z=H.y",
        "object_pose_conversion": POSE_CONVERSION,
        "max_capture_to_object_pose_error": max(x["camera_transform_max_abs_error"] for x in frame_checks),
        "room_config_hash": next(iter(room_config_hashes)), "object_clouds_checked": all_clouds,
        "room_geometry_coordinate_fields_present": False,
        "frames": frame_checks,
    }


def make_declaration(validated: dict[str, Any], adapter_info: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    mapper = validated["manifest"]
    return {
        "schema": "assumed-spatial-scope-declaration/v2",
        "status": "approved_experimental_declaration",
        "declaration_id": "hm3d00802_t1_1_all13_assumed_single_level_demo_v1",
        "mode": "assumed_single_level_demo", "scope_status": "assumed",
        "approval": {
            "status": "approved_by_user", "approved_by_user": True,
            "approval_date": "2026-10-19",
            "approval_scope": "one semantic replay on this exact mapper run and its 13 locked prefixes only",
        },
        "assumption": {
            "evidence_status": "assumed_not_verified",
            "statement": "Treat the listed room XY polygons and object clouds as XY-compatible solely for this integration experiment; this is not evidence that they share one physical floor or elevation scope.",
        },
        "dataset": "hm3d", "run_id": mapper["run_id"],
        "source_capture_run_id": adapter_info["input_provenance"]["source_capture_run_id"],
        "source_capture_manifest_run_id": adapter_info["input_provenance"]["source_capture_manifest_run_id"],
        "scene_id": mapper["scene_id"], "sequence_id": mapper["sequence_id"],
        "source_frame_indices_exact": [int(x) for x in mapper["ordered_frame_indices"]],
        "effective_prefixes": adapter_info["effective_prefixes"],
        "coordinate_frame": "capture/world metric meters, Z-up", "units": "meters",
        "projection_plane": "world_xy",
        "spatial_extent_assumption": "Only the exact 13 listed prefix states are treated as XY-compatible for experimental membership. No floor identity, Z bounds, verified single-floor scope, or transfer to other runs/scenes is asserted.",
        "transform_provenance": {
            "world_frame_canonical": "Z-up; C.x=H.x,C.y=-H.z,C.z=H.y",
            "habitat_camera_pose_conversion": POSE_CONVERSION,
            "camera_optical": "+X right,+Y down,+Z forward",
            "depth_units": "m", "depth_representation": "axial",
            "object_geometry": {
                "source": "ConceptGraphs online prefix snapshots and their referenced XYZ clouds",
                "coordinate_frame": "canonical world Z-up metric XYZ, as used by the T1.1 mapper contract",
                "pose_relation": "all 13 camera_pose_W_Copt transforms equal capture sensor_pose_canonical composed with the locked sensor-to-optical basis transform",
                "source_manifest_sha256": validated["manifest_sha256"],
            },
            "room_geometry": {
                "source": "OccuSG room snapshots from the same mapper-only run, scene, and sequence",
                "representation": "2D polygon outer_ring/holes on world XY",
                "source_config_hash": preflight["room_config_hash"],
                "elevation_metadata_available": False,
                "coordinate_compatibility_status": "assumed_not_verified",
                "explicit_room_units_or_frame_fields": False,
            },
            "capture_source_run_manifest_sha256": adapter_info["input_provenance"]["source_capture_run_manifest_sha256"],
            "capture_pose_sensor_fields": "metadata.json:sensor_pose_canonical; each record hashes in effective_prefixes",
        },
        "source_provenance": adapter_info["input_provenance"],
        "limits": {
            "applies_only_to_this_run_and_exact_prefix_list": True,
            "verified_single_floor_claim": False, "multi_floor_claim": False,
            "reusable_for_other_runs": False, "changes_A_tables": False,
            "changes_thresholds": False, "changes_default": False,
            "forces_room_label": False,
        },
        "not_for_default_runtime": True,
        "preflight_summary": preflight,
    }


def _status_counts(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field, "unavailable"))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def make_diagnostic_report(semantic_run: Path, a_verified_run: Path, a_zip: Path,
                           output_root: Path) -> dict[str, Any]:
    b_manifest = json.loads((semantic_run / "manifest.json").read_text())
    a_manifest = json.loads((a_verified_run / "manifest.json").read_text())
    if a_manifest.get("scope_status") != "unavailable":
        raise ValueError("A verified-only control must retain closed scope for this run")
    prefix_reports = []
    room_csv_rows = []
    transition_counts: dict[str, dict[str, int]] = {v: {} for v in b_manifest["variants"]}
    _, categories, _, _ = load_a_tables(a_zip)
    supported = set(categories)
    for index_row in b_manifest["revisions"]:
        row = json.loads((semantic_run / index_row["state"]).read_text())
        objects = row["objects"]
        object_gates = [{
            "causal_object_id": obj.get("causal_object_id"),
            "hypothesis_id": obj.get("snapshot_hypothesis_id"),
            "observation_ids": obj.get("observation_ids", []),
            "admission_eligible": obj.get("prefix_admission", {}).get("eligible"),
            "admission_reason": obj.get("prefix_admission", {}).get("reason"),
            "consensus_status": obj.get("semantic", {}).get("status"),
            "consensus_category": obj.get("semantic", {}).get("category"),
            "supported_category": obj.get("semantic", {}).get("category") in supported,
            "membership_status": obj.get("membership", {}).get("status"),
            "membership_reason": obj.get("membership", {}).get("reason"),
            "room_id": obj.get("membership", {}).get("room_id"),
            "support_counts": obj.get("membership", {}).get("support_counts", {}),
            "support_fraction": obj.get("membership", {}).get("support_fraction"),
            "runner_up_fraction": obj.get("membership", {}).get("runner_up_fraction"),
            "outside_fraction": obj.get("membership", {}).get("outside_fraction"),
            "tie": obj.get("membership", {}).get("tied"),
            "room_evidence_eligible": obj.get("room_evidence_eligible"),
        } for obj in objects]
        admission_count = sum(bool(obj.get("prefix_admission", {}).get("eligible")) for obj in objects)
        intersection = [obj for obj in objects
                       if obj.get("prefix_admission", {}).get("eligible") is True
                       and obj.get("semantic", {}).get("status") == "stable"
                       and obj.get("semantic", {}).get("category") in supported]
        assigned = [obj for obj in objects if obj.get("membership", {}).get("status") == "assigned"]
        membership_counts = _status_counts([obj.get("membership", {}) for obj in objects], "status")
        tie_count = sum(bool(obj.get("membership", {}).get("tied")) for obj in objects)
        variants = {}
        for variant in b_manifest["variants"]:
            variants[variant] = {}
            for room_id, diag in row.get("room_diagnostics", {}).get(variant, {}).items():
                variants[variant][room_id] = {
                    "eligible_objects": diag.get("eligible_objects", []),
                    "eligible_categories": diag.get("eligible_categories", []),
                    "category_contributions": diag.get("category_contributions", []),
                    "membership_support": diag.get("membership_support", []),
                    "excluded_objects": diag.get("excluded_objects", []),
                    "raw_S": diag.get("raw_S", {}), "beliefs": diag.get("beliefs", []),
                    "candidate_label": diag.get("candidate_label"),
                    "committed_label": diag.get("committed_label"),
                    "commit_status": diag.get("commit_status"),
                    "abstain_reason": diag.get("abstain_reason"),
                    "label_history_delta": diag.get("label_history_delta", []),
                }
                for event in diag.get("label_history_delta", []):
                    event_type = str(event.get("event_type", event.get("action", event.get("status", "transition"))))
                    transition_counts[variant][event_type] = transition_counts[variant].get(event_type, 0) + 1
                room_csv_rows.append({"frame_index": row["frame_index"], "frame_id": row["frame_id"],
                    "unified_revision": row.get("source_prefix", {}).get("unified_revision"),
                    "room_id": room_id, "variant": variant,
                    "eligible_objects": json.dumps(diag.get("eligible_objects", [])),
                    "eligible_categories": json.dumps(diag.get("eligible_categories", [])),
                    "contributions": json.dumps(diag.get("category_contributions", []), ensure_ascii=False),
                    "membership_support": json.dumps(diag.get("membership_support", []), ensure_ascii=False),
                    "excluded_objects": json.dumps(diag.get("excluded_objects", []), ensure_ascii=False),
                    "raw_S": json.dumps(diag.get("raw_S", {})), "beliefs": json.dumps(diag.get("beliefs", [])),
                    "candidate": diag.get("candidate_label"), "committed": diag.get("committed_label"),
                    "commit_status": diag.get("commit_status"), "abstain_reason": diag.get("abstain_reason"),
                    "transitions": json.dumps(diag.get("label_history_delta", []), ensure_ascii=False)})
        prefix_reports.append({
            "frame_index": row["frame_index"], "frame_id": row["frame_id"],
            "object_revision": row.get("source_prefix", {}).get("object_revision"),
            "room_revision": row.get("source_prefix", {}).get("room_revision"),
            "unified_revision": index_row.get("unified_revision"),
            "hypothesis_count": len(objects), "admission_sufficient_hypotheses": admission_count,
            "consensus_status_counts": _status_counts([obj.get("semantic", {}) for obj in objects], "status"),
            "admission_stable_supported_vocabulary_intersection_count": len(intersection),
            "admission_stable_supported_vocabulary_intersection": [obj["causal_object_id"] for obj in intersection],
            "membership_status_counts": membership_counts,
            "membership_assigned_count": len(assigned), "membership_tie_count": tie_count,
            "eligible_categories_by_room_variant": {
                v: {rid: d.get("eligible_categories", []) for rid, d in row.get("room_diagnostics", {}).get(v, {}).items()}
                for v in b_manifest["variants"]},
            "room_variant_states": variants,
            "object_gate_rows": object_gates,
            "cumulative_committed_room_states_by_variant": {
                v: sum(1 for state in row.get("rooms", {}).get(v, {}).values() if state.get("committed_label"))
                for v in b_manifest["variants"]},
            "label_transition_count_by_variant": {
                v: sum(len(d.get("label_history_delta", [])) for d in row.get("room_diagnostics", {}).get(v, {}).values())
                for v in b_manifest["variants"]},
            "room_lifecycle": row.get("room_lifecycle"),
        })
    csv_path = output_root / "diagnostic/room_variant_prefixes.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(room_csv_rows[0]) if room_csv_rows else ["frame_index", "frame_id", "unified_revision", "room_id", "variant"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader(); writer.writerows(room_csv_rows)
    report = {
        "schema": "t1-1-t3-assumed-b-diagnostic/v1", "status": "complete",
        "evidence_status": "assumed_not_verified", "scope_status": "assumed",
        "A_control": {"scope_status": a_manifest.get("scope_status"),
                       "membership_status": a_manifest.get("causality", {}).get("membership"),
                       "room_semantics_status": a_manifest.get("causality", {}).get("room_semantics"),
                       "interpretation": "scope gate closed: scoring unavailable; this is not an abstention after score computation"},
        "B_experimental": {"declaration_id": b_manifest.get("scope_declaration_id"),
                           "declaration_sha256": b_manifest.get("scope_declaration_sha256"),
                           "frame_count": b_manifest.get("frame_count"),
                           "final_hypotheses": prefix_reports[-1]["hypothesis_count"] if prefix_reports else 0,
                           "final_admission_sufficient": prefix_reports[-1]["admission_sufficient_hypotheses"] if prefix_reports else 0,
                           "final_admission_stable_supported_intersection": prefix_reports[-1]["admission_stable_supported_vocabulary_intersection_count"] if prefix_reports else 0,
                           "final_membership_status_counts": prefix_reports[-1]["membership_status_counts"] if prefix_reports else {},
                           "label_transition_counts_by_variant": transition_counts},
        "prefixes": prefix_reports,
        "outputs": {"prefix_details": "diagnostic/t3_assumed_b_report.json",
                    "per_room_variant_csv": "diagnostic/room_variant_prefixes.csv"},
        "quality_claim": "none; development integration diagnostic only",
    }
    _write(output_root / "diagnostic/t3_assumed_b_report.json", report)
    return report


def prepare(mapper_run: Path, a_zip: Path, verified_run: Path, output: Path) -> dict[str, Any]:
    """Model-free declaration, transform and source-hash preflight."""
    started = time.perf_counter()
    mapper_run = mapper_run.resolve(); a_zip = a_zip.resolve(); verified_run = verified_run.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"B output must be a new directory; refusing overwrite: {output}")
    validated = adapter_module.validate_mapper_run(mapper_run, a_zip)
    preflight = spatial_preflight(validated)
    a_manifest = json.loads((verified_run / "verified_only/manifest.json").read_text(encoding="utf-8"))
    if (a_manifest.get("scope_status") != "unavailable" or
        a_manifest.get("input_provenance", {}).get("source_mapper_run_manifest_sha256") != validated["manifest_sha256"] or
        a_manifest.get("input_provenance", {}).get("A_archive_sha256") != validated["a_zip_sha256"]):
        raise ValueError("existing verified-only output is not the exact A control for this mapper run/A archive")

    output.mkdir(parents=True, exist_ok=False)
    _write(output / "run_state.json", {"status": "preparation_started", "started_monotonic_s": time.monotonic(),
        "mapper_run_id": validated["manifest"]["run_id"], "semantic_replay_attempts": 0,
        "preflight": preflight})
    try:
        adapter_info = adapter_module.prepare_adapter(validated, output / "input_adapter")
        declaration = make_declaration(validated, adapter_info, preflight)
        declaration_path = output / "assumed_scope_declaration_v2.json"
        _write(declaration_path, declaration)

        # Full model-free scope/hash/prefix validation happens before the one RB1 replay.
        adapter_manifest_path = Path(adapter_info["adapter_manifest"])
        adapter_manifest = json.loads(adapter_manifest_path.read_text(encoding="utf-8"))
        _load_effective_semantic_config(adapter_manifest_path.parent, adapter_manifest)
        validation = validate_assumed_scope_declaration(declaration, declaration_path,
            adapter_manifest_path.parent, adapter_manifest,
            json.loads((adapter_manifest_path.parent / "frame_index.json").read_text())["frames"],
            a_zip, {})
        _write(output / "preflight_report.json", {"status": "passed", "spatial_transform": preflight,
            "declaration_path": str(declaration_path), "declaration_sha256": adapter_module.sha(declaration_path),
            "declaration_validation": validation, "A_control_manifest_sha256": rb_sha(verified_run / "verified_only/manifest.json"),
            "semantic_replay_count_before_start": 0,
            "scope_status": "assumed_not_verified"})
    except BaseException as exc:
        _write(output / "run_state.json", {"status": "preparation_failed", "semantic_replay_attempts": 0,
            "error_type": type(exc).__name__, "error": str(exc),
            "partial_files": sorted(str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()),
            "elapsed_s": time.perf_counter() - started})
        raise
    ready = {"status": "prepared", "semantic_replay_attempts": 0,
             "declaration_sha256": adapter_module.sha(output / "assumed_scope_declaration_v2.json"),
             "preflight_report_sha256": adapter_module.sha(output / "preflight_report.json"),
             "elapsed_s": time.perf_counter() - started,
             "scope_status": "assumed_not_verified"}
    _write(output / "run_state.json", ready)
    return ready


def run(mapper_run: Path, a_zip: Path, verified_run: Path, output: Path,
        viewer_output: Path | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    mapper_run = mapper_run.resolve(); a_zip = a_zip.resolve(); verified_run = verified_run.resolve()
    output = output.resolve()
    if not output.exists():
        prepare(mapper_run, a_zip, verified_run, output)
    state = json.loads((output / "run_state.json").read_text(encoding="utf-8"))
    if state.get("status") != "prepared" or state.get("semantic_replay_attempts") != 0:
        raise ValueError("real replay requires a model-free prepared run; failed/used runs cannot be retried")
    declaration_path = output / "assumed_scope_declaration_v2.json"
    adapter_manifest_path = output / "input_adapter/manifest.json"
    preflight_path = output / "preflight_report.json"
    for path, expected_key in ((declaration_path, "declaration_sha256"),
                               (preflight_path, "preflight_report_sha256")):
        if not path.is_file() or adapter_module.sha(path) != state.get(expected_key):
            raise ValueError(f"prepared {path.name} changed since model-free preflight")
    adapter_manifest = json.loads(adapter_manifest_path.read_text(encoding="utf-8"))
    adapter_info_path = adapter_manifest_path.parent
    semantic_provenance = _load_effective_semantic_config(adapter_info_path, adapter_manifest)
    declaration = json.loads(declaration_path.read_text(encoding="utf-8"))
    validation = validate_assumed_scope_declaration(declaration, declaration_path,
        adapter_info_path, adapter_manifest,
        json.loads((adapter_info_path / "frame_index.json").read_text())["frames"],
        a_zip, semantic_provenance)
    if validation.get("status") != "assumed":
        raise ValueError("prepared declaration did not pass exact-prefix validation")
    verified_manifest = json.loads((verified_run / "verified_only/manifest.json").read_text(encoding="utf-8"))
    if (verified_manifest.get("scope_status") != "unavailable" or
        verified_manifest.get("input_provenance", {}).get("source_mapper_run_manifest_sha256") != adapter_manifest["semantic_context"]["source_run_manifest_sha256"] or
        verified_manifest.get("input_provenance", {}).get("A_archive_sha256") != adapter_module.sha(a_zip)):
        raise ValueError("verified-only control no longer matches the prepared source/A tables")
    validated = adapter_module.validate_mapper_run(mapper_run, a_zip)
    spatial_preflight(validated)

    semantic_output = output / "assumed_b"
    _write(output / "run_state.json", {"status": "semantic_replay_started", "started_monotonic_s": time.monotonic(),
        "mapper_run_id": validated["manifest"]["run_id"], "semantic_replay_attempts": 1,
        "declaration_sha256": adapter_module.sha(declaration_path), "preflight_status": "passed"})
    try:
        # Exactly one call to the existing causal RB1 prefix runner.
        result = run_causal_rb1(adapter_info_path, semantic_output, a_zip, declaration_path)
    except BaseException as exc:
        state = {"status": "failed", "semantic_replay_attempts": 1,
                 "error_type": type(exc).__name__, "error": str(exc),
                 "partial_files": sorted(str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()),
                 "elapsed_s": time.perf_counter() - started}
        _write(output / "run_state.json", state)
        raise
    if result.get("frame_count") != 13 or result.get("scope_status") != "assumed":
        _write(output / "run_state.json", {"status": "failed", "semantic_replay_attempts": 1,
            "reason": "semantic output did not retain assumed scope or exact 13 prefixes",
            "elapsed_s": time.perf_counter() - started})
        raise RuntimeError("B semantic replay output failed exact-prefix/scope acceptance")

    report = make_diagnostic_report(semantic_output, verified_run / "verified_only", a_zip, output)
    viewer_receipt = None
    viewer_status = "not_built"
    if viewer_output is None:
        viewer_output = output / "viewer_package_v4"
    try:
        viewer_receipt = build_t3_viewer(mapper_run, a_zip, verified_run / "verified_only",
                                         viewer_output, semantic_output)
        viewer_status = "built_gui_pending"
    except Exception as exc:
        viewer_status = "build_failed"
        _write(output / "viewer_build_failure.json", {"error_type": type(exc).__name__, "error": str(exc)})

    summary = {
        "schema": "t1-1-t3-assumed-b-run-summary/v1", "status": "completed",
        "source_mapper_run_id": validated["manifest"]["run_id"],
        "scene_id": validated["manifest"]["scene_id"], "sequence_id": validated["manifest"]["sequence_id"],
        "source_frame_indices_exact": EXPECTED_FRAMES,
        "semantic_replay_attempts": 1, "semantic_revision_count": 13,
        "capture_perception_mapper_runs": 0,
        "scope_status": "assumed", "evidence_status": "assumed_not_verified",
        "scope_declaration": declaration_path.name,
        "scope_declaration_sha256": adapter_module.sha(declaration_path),
        "A_control_manifest_sha256": rb_sha(verified_run / "verified_only/manifest.json"),
        "B_semantic_manifest_sha256": rb_sha(semantic_output / "manifest.json"),
        "viewer_status": viewer_status,
        "viewer_entrypoint": str(viewer_output / "viewer/index.html") if viewer_status == "built_gui_pending" else None,
        "viewer_receipt_sha256": rb_sha(viewer_output / "viewer_build_receipt.json") if viewer_receipt else None,
        "runtime_s": time.perf_counter() - started,
    }
    _write(output / "run_summary.json", summary)
    _write(output / "receipt.json", {"schema": "t1-1-t3-assumed-b-receipt/v1", **summary,
        "preflight_report_sha256": adapter_module.sha(output / "preflight_report.json"),
        "diagnostic_report_sha256": adapter_module.sha(output / "diagnostic/t3_assumed_b_report.json"),
        "viewer_build_receipt": viewer_receipt})
    _write(output / "run_state.json", {"status": "completed", "semantic_replay_attempts": 1,
        "elapsed_s": time.perf_counter() - started, "scope_status": "assumed_not_verified",
        "viewer_status": viewer_status})
    return {"summary": summary, "diagnostic": report, "viewer_receipt": viewer_receipt}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mapper-run", type=Path, required=True)
    ap.add_argument("--a-zip", type=Path, required=True)
    ap.add_argument("--verified-run", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--viewer-output", type=Path, default=None)
    ap.add_argument("--prepare-only", action="store_true",
                    help="run model-free preflight and write the approved exact-prefix declaration without semantic replay")
    args = ap.parse_args()
    if args.prepare_only:
        result = prepare(args.mapper_run, args.a_zip, args.verified_run, args.output)
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        result = run(args.mapper_run, args.a_zip, args.verified_run, args.output, args.viewer_output)
        print(json.dumps(result["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
