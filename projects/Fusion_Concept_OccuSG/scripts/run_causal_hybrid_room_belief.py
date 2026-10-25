#!/usr/bin/env python3
"""Build causal hybrid semantic/membership state from an existing M1 timeline.

This runner is read-only with respect to the M0/M1 timeline.  It never runs
perception, mapping, Qwen, or native room replay.  Prefix object hypotheses
are read at each frame, semantic consensus is computed only from observations
available in that prefix, and RB1 is updated with retractable events.  Room
membership is emitted only when the input explicitly proves an elevation
scope; this timeline does not, so the normal output is unavailable rather
than an unconditional XY assignment.
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
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.contracts.regions import Polygon2D
from standalone_sg.semantics.room_belief_v1 import (
    ConsensusPolicy, RoomBeliefLedger, RoomBeliefPolicy, RoomObjectEvidence,
    RoomBeliefEvent, SemanticObservation, aggregate_object_semantics,
    greedy_voxel_support_membership,
)
from standalone_sg.objects.surface_support import voxel_representatives

VARIANTS = ("mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral")


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical(value: str | None, aliases: dict[str, str]) -> str | None:
    if value is None:
        return None
    key = re.sub(r"\s+", " ", str(value).strip().lower())
    return aliases.get(key, key.replace(" ", "_"))


def _hash_set(items: list[tuple[str, str]]) -> str:
    payload = [{"path": path, "sha256": digest} for path, digest in items]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _input_set_hashes(timeline: Path, manifest: dict[str, Any], frame_items: list[dict[str, Any]]) -> dict[str, Any]:
    object_files = [(str(item["snapshot"]), sha(timeline / item["snapshot"])) for item in frame_items]
    room_files = []
    cloud_paths: set[str] = set()
    for item in frame_items:
        key = str(item["frame_index"])
        rel = manifest.get("room_revision_files", {}).get(key)
        if not rel:
            raise ValueError(f"room revision index missing frame_index={key}")
        room_files.append((str(rel), sha(timeline / rel)))
        snapshot = json.loads((timeline / item["snapshot"]).read_text())
        for obj in snapshot.get("objects", []):
            cloud_rel = obj.get("cloud_npz")
            if cloud_rel:
                cloud_paths.add(str(cloud_rel))
    cloud_files = []
    for rel in sorted(cloud_paths):
        geometry_root = Path(manifest.get("object_geometry_root", timeline)).resolve()
        path = geometry_root / rel
        if not path.is_file():
            raise FileNotFoundError(f"prefix object geometry is missing: {path}")
        cloud_files.append((rel, sha(path)))
    return {
        "object_snapshot_count": len(object_files), "object_snapshot_set_sha256": _hash_set(object_files),
        "room_revision_count": len(room_files), "room_revision_set_sha256": _hash_set(room_files),
        "object_cloud_count": len(cloud_files), "object_cloud_set_sha256": _hash_set(cloud_files),
    }


def _load_effective_semantic_config(timeline: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Resolve canonical aliases from the C2 effective config/profile and verify their hashes."""
    if manifest.get("semantic_context") is not None:
        context = manifest["semantic_context"]
        if context.get("schema") != "causal-room-belief-semantic-context/v1":
            raise ValueError("unsupported explicit semantic context schema")
        profile_path = Path(context["detector_profile_path"]).resolve()
        if not profile_path.is_file() or sha(profile_path) != context.get("detector_profile_sha256"):
            raise ValueError("explicit detector profile is missing or hash-mismatched")
        profile = json.loads(profile_path.read_text())
        aliases = profile.get("vocabulary", {}).get("canonical_map")
        if not isinstance(aliases, dict) or not aliases or any(not isinstance(k, str) or not isinstance(v, str) for k, v in aliases.items()):
            raise ValueError("explicit detector profile has no valid vocabulary.canonical_map")
        normalized_aliases = {re.sub(r"\s+", " ", k.strip().lower()): v.strip().lower() for k, v in aliases.items()}
        if normalized_aliases != context.get("canonical_aliases"):
            raise ValueError("explicit semantic context aliases differ from the hashed detector profile")
        route_path = Path(context["effective_route_config_path"]).resolve()
        if not route_path.is_file() or sha(route_path) != context.get("effective_route_config_sha256"):
            raise ValueError("explicit effective route config is missing or hash-mismatched")
        mapping = manifest.get("effective_mapping_config")
        if not isinstance(mapping, dict) or mapping != context.get("mapping_effective_config"):
            raise ValueError("explicit mapping config does not match the source run manifest")
        mapping_hash = hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if mapping_hash != context.get("mapping_effective_config_sha256"):
            raise ValueError("explicit mapping config digest mismatch")
        source_run_manifest = Path(context["source_run_manifest_path"]).resolve()
        if not source_run_manifest.is_file() or sha(source_run_manifest) != context.get("source_run_manifest_sha256"):
            raise ValueError("source mapper run manifest is missing or hash-mismatched")
        return {
            "run_id": context["source_run_id"],
            "source_run_manifest_path": str(source_run_manifest),
            "source_run_manifest_sha256": context["source_run_manifest_sha256"],
            "effective_route_config_path": str(route_path),
            "effective_route_config_sha256": context["effective_route_config_sha256"],
            "mapping_effective_config_sha256": mapping_hash,
            "detector_profile_path": str(profile_path),
            "detector_profile_sha256": context["detector_profile_sha256"],
            "canonical_aliases": normalized_aliases,
            "canonicalization_policy": "source_run_detector_profile.vocabulary.canonical_map_v1",
            "effective_config": {"mapping": {"obj_min_detections": int(mapping["obj_min_detections"])}},
        }
    cache_root = Path(manifest["cache_root"]).resolve()
    c2_manifest_path = cache_root / "c2_run_manifest.json"
    if not c2_manifest_path.is_file():
        raise ValueError(f"C2 provenance manifest missing: {c2_manifest_path}")
    c2_manifest = json.loads(c2_manifest_path.read_text())
    command = c2_manifest.get("command", [])
    if "--config" not in command:
        raise ValueError("C2 command has no --config path")
    config_path = Path(command[command.index("--config") + 1])
    if not config_path.is_absolute():
        config_path = (ROOT / config_path).resolve()
    if not config_path.is_file() or sha(config_path) != c2_manifest.get("input_sha256", {}).get("c2_config"):
        raise ValueError("effective C2 config is missing or hash-mismatched")
    effective = json.loads(config_path.read_text())
    profile_path = Path(effective["perception"]["detector_profile"])
    if not profile_path.is_absolute():
        profile_path = (ROOT / profile_path).resolve()
    expected_profile_hash = c2_manifest.get("input_sha256", {}).get("detector_profile")
    if not profile_path.is_file() or sha(profile_path) != expected_profile_hash:
        raise ValueError("effective detector profile is missing or hash-mismatched")
    profile = json.loads(profile_path.read_text())
    aliases = profile.get("vocabulary", {}).get("canonical_map")
    if not isinstance(aliases, dict) or not aliases or any(not isinstance(k, str) or not isinstance(v, str) for k, v in aliases.items()):
        raise ValueError("effective detector profile has no valid vocabulary.canonical_map")
    normalized_aliases = {re.sub(r"\s+", " ", k.strip().lower()): v.strip().lower() for k, v in aliases.items()}
    return {
        "c2_run_manifest_path": str(c2_manifest_path), "c2_run_manifest_sha256": sha(c2_manifest_path),
        "run_id": c2_manifest.get("run_id"), "effective_c2_config_path": str(config_path),
        "effective_c2_config_sha256": sha(config_path), "detector_profile_path": str(profile_path),
        "detector_profile_sha256": sha(profile_path), "canonical_aliases": normalized_aliases,
        "canonicalization_policy": "effective_detector_profile.vocabulary.canonical_map_v1",
        "effective_config": effective,
    }


def load_a_tables(path: Path) -> tuple[dict[str, dict[str, dict[str, float]]], list[str], list[str], dict[str, str]]:
    """Load locked A_log_ratio rows directly from the immutable ZIP."""
    tables: dict[str, dict[str, dict[str, float]]] = {}
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        prefix = next((n.split("/full_a/")[0] for n in names if n.endswith("/full_a/a_table_mean_no_neutral.csv")), None)
        if prefix is None:
            raise ValueError(f"A-table ZIP has no full_a tables: {path}")
        for variant in VARIANTS:
            name = f"{prefix}/full_a/a_table_{variant}.csv"
            rows = list(csv.DictReader(line.decode("utf-8") for line in zf.open(name)))
            tables[variant] = {}
            seen: set[tuple[str, str]] = set()
            for row in rows:
                category, room = row["category"], row["room_id"]
                key = (category, room)
                if key in seen:
                    raise ValueError(f"{variant}: duplicate category/room key: {key}")
                seen.add(key)
                value = float(row["A_log_ratio_uncalibrated"])
                if not math.isfinite(value):
                    raise ValueError(f"non-finite A score: {variant}/{category}/{room}")
                tables[variant].setdefault(category, {})[room] = value
        categories = sorted(tables[VARIANTS[0]])
        rooms = sorted(tables[VARIANTS[0]][categories[0]])
        if any(set(tables[v]) != set(categories) for v in VARIANTS):
            raise ValueError("A variants have different category vocabularies")
        if any(set(tables[v][c]) != set(rooms) for v in VARIANTS for c in categories):
            raise ValueError("A variants have incomplete room rows")
        expected_rows = len(categories) * len(rooms)
        for variant in VARIANTS:
            actual_rows = sum(len(row) for row in tables[variant].values())
            if actual_rows != expected_rows:
                raise ValueError(f"{variant}: expected {expected_rows} unique rows, got {actual_rows}")
    digests = {
        variant: hashlib.sha256(json.dumps(tables[variant], sort_keys=True,
                                           separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        for variant in VARIANTS
    }
    return tables, categories, rooms, digests


def room_nodes(room_doc: dict[str, Any]) -> dict[str, Polygon2D]:
    snap = room_doc.get("snapshot", room_doc)
    result: dict[str, Polygon2D] = {}
    for node in snap.get("nodes", []):
        if node.get("kind") != "room":
            continue
        attrs = node.get("attributes", {})
        ring = attrs.get("outer_ring")
        if ring and len(ring) >= 3:
            result[str(node["node_id"])] = Polygon2D(tuple(tuple(p) for p in ring),
                                                        tuple(tuple(tuple(p) for p in h) for h in attrs.get("holes", [])))
    return result


def object_key(obj: dict[str, Any]) -> str:
    """Stable causal key derived only from observations in the prefix."""
    ids = sorted(str(x) for x in obj.get("observation_ids", []))
    if not ids:
        return f"hypothesis:{obj.get('lineage_key', obj.get('hypothesis_id'))}"
    return "observation:" + ids[0]


def frame_number(observation_id: str) -> int:
    match = re.search(r"-(\d{4}):det-", observation_id)
    return int(match.group(1)) if match else -1


def make_ledgers(physical_rooms: list[str], tables: dict[str, dict[str, dict[str, float]]],
                 semantic_rooms: list[str], label_policy: RoomBeliefPolicy,
                 a_digests: dict[str, str]) -> dict[str, RoomBeliefLedger]:
    return {variant: RoomBeliefLedger(
        room_ids=semantic_rooms, room_node_ids=physical_rooms,
        allow_empty_room_nodes=True,
        room_priors={r: 1.0 for r in semantic_rooms}, category_room_scores=tables[variant],
        label_policy=label_policy, evidence_policy_id="rb1-causal-hybrid-v1",
        ontology_version="rb2-A-room-ontology-v1", a_table_hash=a_digests[variant])
            for variant in VARIANTS}


def _source_detection_layers(manifest: dict[str, Any], observation_ids: set[str]) -> tuple[dict[str, str], dict[str, Any]]:
    """Resolve explicit per-observation layer metadata from the cached P3 ledger."""
    cache_root = Path(manifest.get("cache_root", ""))
    path = cache_root / "detection_3d" / "detection_3d.jsonl"
    layers: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            doc = json.loads(line)
            for det in doc.get("detections", []):
                did = str(det.get("detection_id", ""))
                if did in observation_ids and det.get("layer_id") is not None:
                    layers[did] = str(det["layer_id"])
    return layers, {"path": str(path), "sha256": sha(path) if path.is_file() else None,
                    "observation_count": len(observation_ids), "resolved_count": len(layers)}


def _scope_ref_path(value: object, timeline: Path) -> tuple[Path | None, str | None]:
    """Resolve one scope provenance reference without treating a label as evidence."""
    if isinstance(value, str):
        path = Path(value)
        return ((timeline / path).resolve() if not path.is_absolute() else path.resolve()), None
    if not isinstance(value, dict):
        return None, None
    raw = value.get("path") or value.get("file")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    path = Path(raw)
    return ((timeline / path).resolve() if not path.is_absolute() else path.resolve()), str(value.get("sha256")) if value.get("sha256") else None


def validate_assumed_scope_declaration(declaration: dict[str, Any], declaration_path: Path,
                                       timeline: Path, manifest: dict[str, Any],
                                       frame_items: list[dict[str, Any]], a_zip: Path,
                                       semantic_provenance: dict[str, Any]) -> dict[str, Any]:
    """Validate an approved experimental declaration against the locked run inputs."""
    if declaration.get("schema") == "assumed-spatial-scope-declaration/v2":
        return _validate_exact_prefix_assumption(declaration, declaration_path, timeline,
                                                 manifest, frame_items, a_zip)
    if declaration.get("schema") != "assumed-spatial-scope-declaration/v1":
        raise ValueError("unsupported assumed-scope declaration schema")
    if declaration.get("mode") != "assumed_single_level_demo" or declaration.get("scope_status") != "assumed":
        raise ValueError("declaration must explicitly use assumed_single_level_demo with scope_status=assumed")
    if declaration.get("approval", {}).get("status") != "approved_by_user":
        raise ValueError("assumed-scope declaration has no user approval")
    if declaration.get("limits", {}).get("verified_single_floor_claim") is not False:
        raise ValueError("assumed-scope declaration must disclaim verified single-floor scope")

    cache_root = Path(manifest["cache_root"]).resolve()
    c2_path = cache_root / "c2_run_manifest.json"
    capture_path = cache_root / "capture" / "capture_manifest.json"
    c2 = json.loads(c2_path.read_text())
    scene_id, sequence_id = manifest.get("scene_id"), manifest.get("sequence_id")
    run_id = c2.get("run_id")
    for key, actual in (("scene_id", scene_id), ("sequence_id", sequence_id), ("run_id", run_id)):
        if declaration.get(key) != actual:
            raise ValueError(f"assumed-scope declaration {key} mismatch: {declaration.get(key)!r} != {actual!r}")

    frame_range = declaration.get("frame_indices_inclusive")
    if frame_range != [0, len(frame_items) - 1] or [int(item["frame_index"]) for item in frame_items] != list(range(len(frame_items))):
        raise ValueError("assumed-scope declaration frame range does not exactly cover the locked timeline")
    if len(frame_items) != 29 or frame_range != [0, 28]:
        raise ValueError("approved assumed scope is restricted to frames 0–28 of the locked 29-frame run")
    object_revisions = declaration.get("object_prefix", {}).get("snapshot_revisions_inclusive")
    room_revisions = declaration.get("room_prefix", {}).get("room_revision_keys_inclusive")
    if object_revisions != [int(frame_items[0]["revision"]), int(frame_items[-1]["revision"])]:
        raise ValueError("assumed-scope object revision range mismatch")
    expected_room_keys = list(range(0, len(frame_items)))
    if room_revisions != [expected_room_keys[0], expected_room_keys[-1]]:
        raise ValueError("assumed-scope room revision range mismatch")

    timeline_manifest_path = timeline / "manifest.json"
    sets = _input_set_hashes(timeline, manifest, frame_items)
    evidence_path = Path(manifest["mapping_output"]) / "diagnostic/evidence_manifest.json"
    admission_path = Path(manifest["mapping_output"]) / "diagnostic/diagnostic_manifest.json"
    expected = {
        "timeline_manifest_sha256": sha(timeline_manifest_path),
        "c2_run_manifest_sha256": sha(c2_path),
        "capture_manifest_sha256": sha(capture_path),
        "room_delta_log_sha256": sha(cache_root / "replay" / "delta_log.json"),
        "object_snapshot_set_sha256": sets["object_snapshot_set_sha256"],
        "room_revision_set_sha256": sets["room_revision_set_sha256"],
        "object_cloud_set_sha256": sets["object_cloud_set_sha256"],
        "observation_evidence_manifest_sha256": sha(evidence_path),
        "admission_diagnostic_sha256": sha(admission_path),
        "a_zip_sha256": sha(a_zip),
        "effective_c2_config_sha256": semantic_provenance["effective_c2_config_sha256"],
        "detector_profile_sha256": semantic_provenance["detector_profile_sha256"],
        "consensus_policy_sha256": sha(ROOT / "configs/room_semantics/consensus_v1.proposal.json"),
        "room_belief_policy_sha256": sha(ROOT / "configs/room_semantics/room_belief_v1.proposal.json"),
    }
    declared = declaration.get("input_provenance")
    if not isinstance(declared, dict):
        raise ValueError("assumed-scope declaration input_provenance is missing")
    for key, actual in expected.items():
        if declared.get(key) != actual:
            raise ValueError(f"assumed-scope declaration input hash mismatch for {key}")
    if declaration.get("object_prefix", {}).get("timeline_manifest_sha256") != expected["timeline_manifest_sha256"]:
        raise ValueError("assumed-scope nested object-prefix timeline hash mismatch")
    if declaration.get("object_prefix", {}).get("snapshot_set_sha256") != expected["object_snapshot_set_sha256"]:
        raise ValueError("assumed-scope nested object snapshot-set hash mismatch")
    if declaration.get("object_prefix", {}).get("cloud_set_sha256") != expected["object_cloud_set_sha256"]:
        raise ValueError("assumed-scope nested object cloud-set hash mismatch")
    if declaration.get("room_prefix", {}).get("revision_set_sha256") != expected["room_revision_set_sha256"]:
        raise ValueError("assumed-scope nested room revision-set hash mismatch")

    expected_capture_identity = (declaration.get("dataset"), scene_id, sequence_id)
    if (c2.get("dataset_type"), c2.get("scene_id"), c2.get("sequence_id")) != expected_capture_identity:
        raise ValueError("C2 manifest dataset/scene/sequence does not match the approved declaration")
    if declaration.get("coordinate_frame") != "capture/world metric meters, Z-up" or declaration.get("units") != "meters" or declaration.get("projection_plane") != "world_xy":
        raise ValueError("assumed-scope declaration must identify world metric XY projection")
    return {
        "status": "assumed", "reason": "approved_declaration_and_locked_input_hashes_match",
        "declaration_path": str(declaration_path.resolve()), "declaration_sha256": sha(declaration_path),
        "run_id": run_id, "scene_id": scene_id, "sequence_id": sequence_id,
        "frame_indices_inclusive": frame_range, "object_snapshot_revisions_inclusive": object_revisions,
        "room_revision_keys_inclusive": room_revisions, "input_hashes_validated": expected,
        "snapshot_set_hashes": sets,
    }


def _validate_exact_prefix_assumption(declaration: dict[str, Any], declaration_path: Path,
                                      timeline: Path, manifest: dict[str, Any],
                                      frame_items: list[dict[str, Any]], a_zip: Path) -> dict[str, Any]:
    """Validate a sparse, exact prefix allowlist for one experimental replay."""
    if declaration.get("mode") != "assumed_single_level_demo" or declaration.get("scope_status") != "assumed":
        raise ValueError("exact-prefix declaration must explicitly use assumed_single_level_demo/assumed")
    approval = declaration.get("approval", {})
    if approval.get("status") != "approved_by_user" or approval.get("approved_by_user") is not True:
        raise ValueError("exact-prefix declaration has no recorded user approval")
    assumption = declaration.get("assumption", {})
    if assumption.get("evidence_status") != "assumed_not_verified":
        raise ValueError("exact-prefix declaration must retain evidence_status=assumed_not_verified")
    limits = declaration.get("limits", {})
    if limits.get("verified_single_floor_claim") is not False or limits.get("reusable_for_other_runs") is not False:
        raise ValueError("exact-prefix declaration must disclaim verified scope and reuse")
    if "floor_id" in declaration or "z_bounds" in declaration:
        raise ValueError("assumed scope may not invent floor IDs or Z bounds")

    expected_frames = [int(row["frame_index"]) for row in frame_items]
    declared_frames = declaration.get("source_frame_indices_exact")
    if not isinstance(declared_frames, list) or declared_frames != expected_frames:
        raise ValueError("assumed-scope exact frame list does not match the locked source timeline")
    if any(int(b) <= int(a) for a, b in zip(declared_frames, declared_frames[1:])):
        raise ValueError("assumed-scope exact frame list must be strictly increasing")
    bindings = manifest.get("effective_prefixes")
    declared_bindings = declaration.get("effective_prefixes")
    if not isinstance(bindings, list) or len(bindings) != len(frame_items) or declared_bindings != bindings:
        raise ValueError("assumed-scope prefix bindings differ from the locked object/room/unified revisions")
    for item, binding in zip(frame_items, bindings):
        if int(binding.get("frame_index", -1)) != int(item["frame_index"]):
            raise ValueError("assumed-scope prefix binding frame order mismatch")
        if (str(binding.get("frame_id")) != str(item.get("frame_id")) or
            int(binding.get("object_revision", -1)) != int(item.get("object_revision", item["revision"]))):
            raise ValueError(f"assumed-scope object prefix reference mismatch at frame {item['frame_index']}")

    for key, actual in (("run_id", manifest.get("run_id")), ("scene_id", manifest.get("scene_id")),
                        ("sequence_id", manifest.get("sequence_id"))):
        if declaration.get(key) != actual:
            raise ValueError(f"assumed-scope declaration {key} mismatch: {declaration.get(key)!r} != {actual!r}")

    # Verify the complete current source hash set rather than trusting values copied
    # into the declaration. Prefix rows contain packet, object, room, joined graph,
    # and raw capture references, all of which are re-hashed here.
    source_provenance = manifest.get("input_provenance")
    declared_provenance = declaration.get("source_provenance")
    if not isinstance(source_provenance, dict) or declared_provenance != source_provenance:
        raise ValueError("assumed-scope source provenance does not match the prepared exact-prefix adapter")
    semantic_context = manifest.get("semantic_context", {})
    for path_key, hash_key in (("source_mapper_run_manifest_path", "source_mapper_run_manifest_sha256"),
                               ("source_mapper_run_receipt_path", "source_mapper_run_receipt_sha256"),
                               ("source_capture_run_manifest_path", "source_capture_run_manifest_sha256")):
        path_value = source_provenance.get(path_key)
        if not isinstance(path_value, str) or not Path(path_value).is_file() or sha(Path(path_value)) != source_provenance.get(hash_key):
            raise ValueError(f"assumed-scope source file/hash mismatch: {path_key}")
    if source_provenance.get("source_mapper_run_manifest_sha256") != semantic_context.get("source_run_manifest_sha256"):
        raise ValueError("assumed-scope mapper provenance differs from semantic context")
    if source_provenance.get("source_mapper_run_id") != manifest.get("run_id"):
        raise ValueError("assumed-scope mapper run ID differs from adapter run ID")
    capture_doc = json.loads(Path(source_provenance["source_capture_run_manifest_path"]).read_text())
    if (Path(source_provenance["source_capture_run_manifest_path"]).parent.name != source_provenance.get("source_capture_run_id") or
        capture_doc.get("run_id") != source_provenance.get("source_capture_manifest_run_id") or
        capture_doc.get("scene_id") != manifest.get("scene_id") or
        capture_doc.get("sequence_id") != manifest.get("sequence_id")):
        raise ValueError("assumed-scope capture manifest does not identify the same scene/sequence")

    sets = _input_set_hashes(timeline, manifest, frame_items)
    expected = {
        "packet_set_sha256": _hash_set([(str(row["packet_path"]), str(row["packet_sha256"])) for row in bindings]),
        "object_snapshot_set_sha256": sets["object_snapshot_set_sha256"],
        "room_revision_set_sha256": sets["room_revision_set_sha256"],
        "object_cloud_set_sha256": sets["object_cloud_set_sha256"],
        "joined_graph_state_set_sha256": _hash_set([(str(row["joined_graph_state_path"]), str(row["joined_graph_state_sha256"])) for row in bindings]),
        "raw_keyframe_file_set_sha256": _hash_set([(str(Path(row["raw_keyframe_path"]) / name), str(digest))
                                                    for row in bindings for name, digest in sorted(row["raw_keyframe_file_sha256"].items())]),
        "effective_prefix_set_sha256": hashlib.sha256(json.dumps(bindings, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "A_archive_sha256": sha(a_zip),
        "effective_route_config_sha256": semantic_context.get("effective_route_config_sha256"),
        "detector_profile_sha256": semantic_context.get("detector_profile_sha256"),
        "consensus_policy_sha256": sha(ROOT / "configs/room_semantics/consensus_v1.proposal.json"),
        "room_belief_policy_sha256": sha(ROOT / "configs/room_semantics/room_belief_v1.proposal.json"),
    }
    for key, actual in expected.items():
        if source_provenance.get(key) != actual:
            raise ValueError(f"assumed-scope source hash mismatch for {key}")
    if source_provenance.get("raw_keyframe_count") != len(frame_items):
        raise ValueError("assumed-scope raw keyframe count mismatch")

    transform = declaration.get("transform_provenance", {})
    if declaration.get("coordinate_frame") != "capture/world metric meters, Z-up" or declaration.get("units") != "meters" or declaration.get("projection_plane") != "world_xy":
        raise ValueError("exact-prefix declaration must identify metric canonical world XY")
    if transform.get("world_frame_canonical") != "Z-up; C.x=H.x,C.y=-H.z,C.z=H.y":
        raise ValueError("exact-prefix world transform provenance is missing or inconsistent")
    if transform.get("depth_units") != "m" or transform.get("depth_representation") != "axial":
        raise ValueError("exact-prefix capture depth units/representation mismatch")
    if transform.get("habitat_camera_pose_conversion") != "T_W_Copt = T_W_H @ T_H_S @ diag(1,-1,-1)":
        raise ValueError("exact-prefix Habitat optical transform provenance is missing or inconsistent")
    if transform.get("room_geometry", {}).get("elevation_metadata_available") is not False or transform.get("room_geometry", {}).get("coordinate_compatibility_status") != "assumed_not_verified":
        raise ValueError("room geometry provenance must retain the unverified XY assumption")

    import numpy as np
    optical_basis = np.diag([1.0, -1.0, -1.0, 1.0])
    room_config_hashes: set[str] = set()
    for binding in bindings:
        raw_dir = Path(binding["raw_keyframe_path"])
        meta_path = raw_dir / "metadata.json"
        if not meta_path.is_file() or sha(meta_path) != binding["raw_keyframe_file_sha256"].get("metadata.json"):
            raise ValueError(f"assumed-scope raw metadata hash mismatch at frame {binding['frame_index']}")
        meta = json.loads(meta_path.read_text())
        if (meta.get("world_frame_canonical") != transform["world_frame_canonical"] or
            meta.get("depth_units") != "m" or meta.get("depth_representation") != "axial"):
            raise ValueError(f"assumed-scope capture coordinate/depth contract mismatch at frame {binding['frame_index']}")
        sensor_pose = np.asarray(meta.get("sensor_pose_canonical"), dtype=np.float64)
        obj_snap = json.loads(Path(binding["object_snapshot_path"]).read_text())
        object_pose = np.asarray(obj_snap.get("camera_pose_W_Copt"), dtype=np.float64)
        if sensor_pose.shape != (4, 4) or object_pose.shape != (4, 4) or not np.isfinite(sensor_pose).all() or not np.isfinite(object_pose).all():
            raise ValueError(f"assumed-scope transform is malformed at frame {binding['frame_index']}")
        if np.max(np.abs(sensor_pose @ optical_basis - object_pose)) > 1e-5:
            raise ValueError(f"known capture/object camera transform mismatch at frame {binding['frame_index']}")
        room_doc = json.loads(Path(binding["room_snapshot_path"]).read_text())
        room_snap = room_doc.get("snapshot", room_doc)
        room_config_hashes.add(str(room_snap.get("config_hash")))
        # Reject explicit contradictions if a producer supplied unit/frame metadata.
        for node in room_snap.get("nodes", []):
            if node.get("kind") != "room":
                continue
            attrs = node.get("attributes", {})
            explicit_frame = attrs.get("coordinate_frame") or attrs.get("frame_id")
            explicit_units = attrs.get("units")
            if explicit_frame is not None and str(explicit_frame) not in {"world_xy", "canonical_world_xy", "capture/world metric meters, Z-up"}:
                raise ValueError(f"room polygon explicitly declares a conflicting coordinate frame: {explicit_frame}")
            if explicit_units is not None and str(explicit_units).lower() not in {"m", "meter", "meters"}:
                raise ValueError(f"room polygon explicitly declares conflicting units: {explicit_units}")
    if len(room_config_hashes) != 1 or transform.get("room_geometry", {}).get("source_config_hash") not in room_config_hashes:
        raise ValueError("room prefix config hashes are inconsistent with declaration provenance")

    return {
        "status": "assumed", "reason": "approved_exact_prefix_assumption_and_source_hashes_match",
        "declaration_path": str(declaration_path.resolve()), "declaration_sha256": sha(declaration_path),
        "run_id": manifest.get("run_id"), "scene_id": manifest.get("scene_id"),
        "sequence_id": manifest.get("sequence_id"), "frame_indices_exact": expected_frames,
        "effective_prefixes": bindings, "input_hashes_validated": expected,
        "assumption_not_verified": True,
    }


def _validate_scope_contract(scope_contract: dict[str, Any] | None, timeline: Path,
                             manifest: dict[str, Any], frame_items: list[dict[str, Any]],
                             current_frame: int | None = None) -> dict[str, Any]:
    """Validate provenance of a scope contract; a non-empty source_refs label is not enough."""
    if not scope_contract:
        return {"status": "missing", "reason": "scope_contract_missing"}
    expected_scene = manifest.get("scene_id")
    expected_sequence = manifest.get("sequence_id")
    if scope_contract.get("mode") == "assumed_single_level_demo":
        validation = scope_contract.get("_assumed_validation")
        if not isinstance(validation, dict) or validation.get("status") != "assumed":
            return {"status": "invalid", "reason": "assumed_scope_declaration_not_validated"}
        if scope_contract.get("scene_id") != expected_scene or scope_contract.get("sequence_id") != expected_sequence:
            return {"status": "invalid", "reason": "scope_contract_scene_or_sequence_mismatch"}
        effective_frame = int(current_frame) if current_frame is not None else None
        if scope_contract.get("schema") == "assumed-spatial-scope-declaration/v2":
            allowed = scope_contract.get("source_frame_indices_exact", [])
            prefix_refs = {int(row["frame_index"]): row for row in scope_contract.get("effective_prefixes", [])}
            current_ref = prefix_refs.get(effective_frame) if effective_frame is not None else None
            if not isinstance(allowed, list) or effective_frame not in allowed or current_ref is None:
                return {"status": "invalid", "reason": "assumed_scope_not_effective_for_exact_prefix"}
            matching_frame = next((item for item in frame_items if int(item["frame_index"]) == effective_frame), None)
            if matching_frame is None or str(current_ref.get("frame_id")) != str(matching_frame.get("frame_id")):
                return {"status": "invalid", "reason": "assumed_scope_prefix_frame_id_mismatch"}
            if int(current_ref.get("object_revision", -1)) != int(matching_frame.get("object_revision", matching_frame.get("revision", -2))):
                return {"status": "invalid", "reason": "assumed_scope_prefix_object_revision_mismatch"}
            if scope_contract.get("_assumed_validation", {}).get("status") != "assumed":
                return {"status": "invalid", "reason": "assumed_scope_declaration_not_validated"}
            return {"status": "assumed", "reason": "approved_exact_prefix_assumption_hashes_match",
                    "validation": scope_contract["_assumed_validation"], "current_prefix": current_ref}
        frame_range = scope_contract.get("frame_indices_inclusive", [])
        if not isinstance(frame_range, list) or len(frame_range) != 2 or effective_frame is None or not int(frame_range[0]) <= effective_frame <= int(frame_range[1]):
            return {"status": "invalid", "reason": "assumed_scope_not_effective_for_prefix"}
        return {"status": "assumed", "reason": "approved_assumption_hashes_match", "validation": validation}
    if scope_contract.get("scene_id") not in (None, expected_scene):
        return {"status": "invalid", "reason": "scope_contract_scene_mismatch"}
    if scope_contract.get("sequence_id") not in (None, expected_sequence):
        return {"status": "invalid", "reason": "scope_contract_sequence_mismatch"}
    coordinate_frame = scope_contract.get("coordinate_frame")
    units = scope_contract.get("units")
    if not isinstance(coordinate_frame, str) or not coordinate_frame.strip():
        return {"status": "invalid", "reason": "scope_contract_coordinate_frame_missing"}
    if not isinstance(units, str) or units.lower() not in {"m", "meter", "meters", "metric_meters"}:
        return {"status": "invalid", "reason": "scope_contract_units_missing_or_invalid"}
    refs = scope_contract.get("source_refs")
    if not isinstance(refs, list) or not refs:
        return {"status": "invalid", "reason": "scope_contract_source_refs_missing"}
    checked: list[dict[str, Any]] = []
    for ref in refs:
        path, expected_hash = _scope_ref_path(ref, timeline)
        if path is None or not path.is_file():
            return {"status": "invalid", "reason": "scope_contract_source_missing", "source": str(path) if path else ref}
        actual_hash = sha(path)
        if expected_hash and actual_hash != expected_hash:
            return {"status": "invalid", "reason": "scope_contract_source_hash_mismatch", "source": str(path), "expected_sha256": expected_hash, "actual_sha256": actual_hash}
        source_meta: dict[str, Any] = {"path": str(path), "sha256": actual_hash}
        if path.suffix.lower() == ".json":
            try:
                source_doc = json.loads(path.read_text())
            except (OSError, ValueError):
                source_doc = None
            if isinstance(source_doc, dict):
                for key, expected in (("scene_id", expected_scene), ("sequence_id", expected_sequence)):
                    if source_doc.get(key) is not None and expected is not None and str(source_doc[key]) != str(expected):
                        return {"status": "invalid", "reason": f"scope_contract_source_{key}_mismatch", "source": str(path)}
                source_coordinate = source_doc.get("coordinate_frame")
                if source_coordinate is not None and str(source_coordinate) != str(coordinate_frame):
                    return {"status": "invalid", "reason": "scope_contract_source_coordinate_frame_mismatch", "source": str(path)}
                source_meta["declared_scene_id"] = source_doc.get("scene_id")
                source_meta["declared_sequence_id"] = source_doc.get("sequence_id")
        checked.append(source_meta)
    from_frame = scope_contract.get("effective_from_frame")
    to_frame = scope_contract.get("effective_to_frame")
    last_frame = int(frame_items[-1]["frame_index"]) if frame_items else None
    effective_frame = int(current_frame) if current_frame is not None else last_frame
    if from_frame is not None and (effective_frame is None or effective_frame < int(from_frame)):
        return {"status": "invalid", "reason": "scope_contract_not_effective_for_prefix", "checked_sources": checked}
    if to_frame is not None and (effective_frame is None or effective_frame > int(to_frame)):
        return {"status": "invalid", "reason": "scope_contract_expired_for_prefix", "checked_sources": checked}
    return {"status": "validated", "reason": "scope_contract_sources_verified", "checked_sources": checked,
            "coordinate_frame": coordinate_frame, "units": units}


def scope_assessment(timeline: Path, room_docs: list[dict[str, Any]], manifest: dict[str, Any],
                     frame_items: list[dict[str, Any]], evidence: dict[str, Any],
                     scope_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assess elevation scope without treating a final snapshot as prefix knowledge.

    A profile floor and per-detection layers are useful provenance, but they do
    not by themselves prove that 2-D room polygons are scoped to one elevation.
    An explicit, locked room-scope contract is required to open membership.
    """
    room_nodes_seen = [n for d in room_docs for n in d.get("snapshot", d).get("nodes", []) if n.get("kind") == "room"]
    has_elevation = any(n.get("attributes", {}).get("floor_id") is not None or
                        n.get("attributes", {}).get("z_bounds") is not None or
                        n.get("attributes", {}).get("elevation") is not None for n in room_nodes_seen)
    observation_ids = {str(oid) for item in frame_items
                       for oid in json.loads((timeline / item["snapshot"]).read_text()).get("observation_ids", [])}
    layers, detection_provenance = _source_detection_layers(manifest, observation_ids)
    layer_values = sorted(set(layers.values()))
    capture_root = Path(manifest.get("cache_root", "")) / "capture"
    base_z: list[float] = []
    for item in frame_items:
        frame_index = int(item["frame_index"])
        metadata = capture_root / "frames" / f"{frame_index:06d}" / "metadata.json"
        if metadata.is_file():
            value = json.loads(metadata.read_text()).get("base_pose_canonical", [[0, 0, 0, 0]] * 4)[2][3]
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                base_z.append(float(value))
    source_snapshot_path = Path(manifest.get("cache_root", "")) / "replay" / "snapshot.json"
    source_snapshot_layers: list[str] = []
    if source_snapshot_path.is_file():
        source_snap = json.loads(source_snapshot_path.read_text()).get("snapshot", {})
        source_snapshot_layers = sorted({str(n.get("attributes", {}).get("layer_id"))
                                         for n in source_snap.get("nodes", [])
                                         if n.get("kind") == "object" and n.get("attributes", {}).get("layer_id") is not None})
    evaluation_profile_path = Path(manifest.get("cache_root", "")) / "effective_evaluation_profile.json"
    evaluation_profile = json.loads(evaluation_profile_path.read_text()) if evaluation_profile_path.is_file() else {}
    profile_floor = evaluation_profile.get("dataset", {}).get("floor_id")
    replay_manifest_path = Path(manifest.get("cache_root", "")) / "replay" / "manifest.json"
    replay_manifest = json.loads(replay_manifest_path.read_text()) if replay_manifest_path.is_file() else {}
    source_layer_verified = bool(layer_values) and len(layer_values) == 1
    source_object_layer_verified = bool(source_snapshot_layers) and len(source_snapshot_layers) == 1
    # The final replay snapshot is retained as provenance only. It must not
    # open membership for a shorter prefix or retroactively affect that prefix.
    profile_matches_layers = (profile_floor is not None and source_layer_verified
                              and str(profile_floor) == layer_values[0])
    prefix_layer_sets: dict[str, list[str]] = {}
    mixed_prefixes: list[int] = []
    for item in frame_items:
        snap = json.loads((timeline / item["snapshot"]).read_text())
        prefix_ids = {str(oid) for oid in snap.get("observation_ids", [])}
        values = sorted({layers[oid] for oid in prefix_ids if oid in layers})
        prefix_layer_sets[str(item["frame_index"])] = values
        if len(values) > 1:
            mixed_prefixes.append(int(item["frame_index"]))
    mixed_layer_values = len(layer_values) > 1
    layer_metadata_complete = bool(observation_ids) and len(layers) == len(observation_ids)
    contract_status = str((scope_contract or {}).get("status", ""))
    contract_mode = str((scope_contract or {}).get("mode", ""))
    contract_floors = [str(x) for x in (scope_contract or {}).get("floor_ids", [])]
    contract_sources = (scope_contract or {}).get("source_refs", [])
    explicit_contract = (contract_status == "verified_single_floor_scope"
                         and len(contract_floors) == 1 and bool(contract_sources))
    current_frame = max((int(item["frame_index"]) for item in frame_items), default=None)
    contract_validation = _validate_scope_contract(scope_contract, timeline, manifest, frame_items, current_frame)
    if contract_mode == "assumed_single_level_demo":
        exact_frames = scope_contract.get("source_frame_indices_exact")
        if exact_frames is not None:
            from_frame = int(exact_frames[0]) if exact_frames else None
            to_frame = int(exact_frames[-1]) if exact_frames else None
            effective = current_frame in exact_frames if current_frame is not None else False
        else:
            from_frame = int(scope_contract["frame_indices_inclusive"][0])
            to_frame = int(scope_contract["frame_indices_inclusive"][1])
            effective = current_frame is not None and from_frame <= current_frame <= to_frame
        if not effective:
            status, reason = "unavailable", "assumed_scope_not_effective_for_exact_prefix"
        elif contract_validation.get("status") != "assumed":
            status, reason = "unavailable", str(contract_validation.get("reason", "assumed_scope_declaration_not_validated"))
        else:
            status, reason = "assumed", "approved_assumed_single_level_demo_not_verified"
        return {
            "status": status, "scope_status": status, "mode": contract_mode,
            "reason": reason, "assumption_not_verified": True,
            "declaration_id": scope_contract.get("declaration_id"),
            "declaration_sha256": scope_contract.get("sha256"),
            "effective_frame_indices_exact": list(exact_frames) if exact_frames is not None else None,
            "effective_frame_range_inclusive": [from_frame, to_frame] if exact_frames is None else None,
            "current_frame_index": current_frame,
            "coordinate_frame": scope_contract.get("coordinate_frame"),
            "units": scope_contract.get("units"),
            "projection_plane": scope_contract.get("projection_plane"),
            "assumption": scope_contract.get("assumption"),
            "input_provenance": scope_contract.get("input_provenance"),
            "observed_floor_ids": layer_values,
            "layer_metadata_coverage": len(layers) / len(observation_ids) if observation_ids else 0.0,
            "layer_metadata_complete": layer_metadata_complete,
            "room_nodes_with_elevation_metadata": int(sum(1 for n in room_nodes_seen if n.get("attributes", {}).get("floor_id") is not None or n.get("attributes", {}).get("z_bounds") is not None or n.get("attributes", {}).get("elevation") is not None)),
            "room_nodes_total": len(room_nodes_seen),
            "room_geometry_kind": "2D XY polygons; no elevation bounds; assumption only",
            "detection_layer_provenance": detection_provenance,
            "prefix_layer_sets": prefix_layer_sets,
            "mixed_floor_prefixes": mixed_prefixes,
            "multi_floor_safety": False,
            "verified_single_floor_claim": False,
        }
    verified = (explicit_contract and not has_elevation and not mixed_prefixes
                and not mixed_layer_values
                and layer_metadata_complete and profile_matches_layers and contract_floors == [str(profile_floor)]
                and contract_validation["status"] == "validated")
    if mixed_prefixes or mixed_layer_values:
        status = "unavailable"
        reason = "prefix_scope_contains_mixed_floor_layers"
    elif verified:
        status = "verified_single_observed_floor_scope"
        reason = "explicit_locked_room_scope_contract_and_prefix_layers_agree"
    elif not scope_contract:
        status = "unavailable"
        reason = "room_scope_contract_missing_for_2d_polygons"
    elif not explicit_contract:
        status = "unavailable"
        reason = "room_scope_contract_is_missing_required_status_floor_ids_or_sources"
    elif contract_validation["status"] != "validated":
        status = "unavailable"
        reason = contract_validation["reason"]
    elif not layer_metadata_complete:
        status = "unavailable"
        reason = "prefix_scope_layer_metadata_incomplete"
    else:
        status = "unavailable"
        reason = "room_scope_contract_does_not_match_prefix_provenance"
    return {
        "status": status,
        "reason": reason,
        "floor_profile_metadata_ignored": not verified,
        "floor_profile_note": "profile/P3/object layers are provenance cross-checks; an explicit room-scope contract is required before 2-D XY membership is enabled",
        "room_nodes_with_elevation_metadata": int(sum(1 for n in room_nodes_seen if n.get("attributes", {}).get("floor_id") is not None or n.get("attributes", {}).get("z_bounds") is not None or n.get("attributes", {}).get("elevation") is not None)),
        "room_nodes_total": len(room_nodes_seen),
        "room_geometry_kind": "2D XY polygons; room nodes have no floor/layer/Z bounds",
        "observed_floor_ids": layer_values,
        "evaluation_profile_floor_id": str(profile_floor) if profile_floor is not None else None,
        "evaluation_profile_path": str(evaluation_profile_path),
        "evaluation_profile_sha256": sha(evaluation_profile_path) if evaluation_profile_path.is_file() else None,
        "observed_source_object_layer_ids": source_snapshot_layers,
        "observed_agent_base_z_m": sorted(set(round(x, 6) for x in base_z)),
        "observation_count": len(observation_ids),
        "layer_metadata_coverage": len(layers) / len(observation_ids) if observation_ids else 0.0,
        "layer_metadata_complete": layer_metadata_complete,
        "detection_layer_provenance": detection_provenance,
        "prefix_layer_sets": prefix_layer_sets,
        "mixed_floor_prefixes": mixed_prefixes,
        "mixed_observed_layer_ids": mixed_layer_values,
        "profile_matches_source_layers": profile_matches_layers,
        "profile_matches_prefix_layers": profile_matches_layers,
        "scope_contract": scope_contract,
        "scope_contract_validation": contract_validation,
        "p2_layer_policy": "P2 detections inherit cache-level evaluation_profile.dataset.floor_id; P3 layer metadata cross-check covers only accepted 3D detections",
        "source_snapshot_sha256": sha(source_snapshot_path) if source_snapshot_path.is_file() else None,
        "source_snapshot_path": str(source_snapshot_path),
        "room_generation_provenance": {
            "replay_manifest_path": str(replay_manifest_path),
            "replay_manifest_sha256": sha(replay_manifest_path) if replay_manifest_path.is_file() else None,
            "mapping_backend": replay_manifest.get("mapping_backend"),
            "m4_preprocessing_policy": replay_manifest.get("m4_preprocessing_policy"),
            "rooms_final": replay_manifest.get("rooms_final"),
            "regions_final": replay_manifest.get("regions_final"),
            "config": replay_manifest.get("config"),
            "config_sha256": replay_manifest.get("config_sha256"),
        },
        "coordinate_scope": "capture/world metric meters, Z-up; room polygons use world XY",
        "single_floor_scope_basis": "explicit locked room-scope contract plus prefix P3/profile cross-check; P2 rows inherit the locked cache scope" if verified else None,
        "multi_floor_safety": False,
    }


def membership_for(obj: dict[str, Any], room_polys: dict[str, Polygon2D], scope: dict[str, Any],
                   geometry_root: Path, previous: str | None, geometry_revision: int,
                   voxel_size_m: float, voxel_grid_origin_xyz_m: list[float]) -> dict[str, Any]:
    base = {"object_id": object_key(obj), "room_id": None, "status": "unavailable",
            "reason": scope["reason"], "support_counts": {}, "support_fraction": 0.0,
            "runner_up_fraction": 0.0, "outside_fraction": None, "tied": False,
            "plausible_room_ids": [], "geometry_revisions": {}, "scope": scope["status"],
            "scope_status": scope["status"], "declaration_id": scope.get("declaration_id"),
            "declaration_sha256": scope.get("declaration_sha256")}
    if scope["status"] not in {"verified", "verified_single_observed_floor_scope", "assumed"}:
        return base
    source = geometry_root / obj["cloud_npz"]
    import numpy as np
    cloud = np.load(source, allow_pickle=False)
    xyz = cloud["xyz"]
    reps = voxel_representatives(xyz, voxel_size_m)
    result = greedy_voxel_support_membership(object_key(obj), reps.tolist(), room_polys,
        voxel_size_m=voxel_size_m, voxel_grid_origin_xyz_m=voxel_grid_origin_xyz_m, previous_room_id=previous,
        geometry_revisions={rid: int(geometry_revision) for rid in room_polys})
    return result.as_dict() | {"scope": scope["status"], "scope_status": scope["status"],
                               "declaration_id": scope.get("declaration_id"),
                               "declaration_sha256": scope.get("declaration_sha256"),
                               "assumption_not_verified": scope.get("assumption_not_verified", False)}


def room_evidence_eligible(prefix_admission: bool, semantic_status: str,
                           membership: dict[str, Any], category: str | None,
                           table: dict[str, dict[str, float]]) -> bool:
    """Eligibility for one object; never inspect other objects in this frame."""
    return bool(prefix_admission and semantic_status == "stable"
                and membership.get("status") == "assigned"
                and category in table)


def _load_admission_threshold(timeline: Path, manifest: dict[str, Any], effective_config: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Read the locked admission value from the cached diagnostic manifest."""
    if manifest.get("effective_mapping_config") is not None:
        mapping = manifest["effective_mapping_config"]
        value = mapping.get("obj_min_detections")
        if type(value) is not int or value < 1 or value != effective_config.get("mapping", {}).get("obj_min_detections"):
            raise ValueError("source mapper obj_min_detections is missing or inconsistent with validated semantic context")
        context = manifest.get("semantic_context", {})
        return value, {"source": "mapper_run_manifest.mapping_effective_config.obj_min_detections",
                       "source_run_id": context.get("source_run_id"),
                       "source_run_manifest_sha256": context.get("source_run_manifest_sha256"),
                       "mapping_effective_config_sha256": context.get("mapping_effective_config_sha256"),
                       "effective_config_rule": f"obj_min_detections={value}"}
    diagnostic = Path(manifest["mapping_output"]) / "diagnostic/diagnostic_manifest.json"
    if not diagnostic.is_file():
        raise ValueError(f"admission provenance missing: {diagnostic}")
    doc = json.loads(diagnostic.read_text())
    match = re.search(r"obj_min_detections\s*=\s*(\d+)", str(doc.get("filter_rule", "")))
    if match is None:
        raise ValueError("diagnostic filter_rule has no obj_min_detections value")
    value = int(match.group(1))
    config_rule = str(effective_config.get("mapping", {}).get("object_filter", ""))
    config_match = re.search(r"min_detections\s*=\s*(\d+)", config_rule)
    if config_match is None or int(config_match.group(1)) != value:
        raise ValueError("effective C2 object-filter config and diagnostic admission value disagree")
    return value, {"path": str(diagnostic), "sha256": sha(diagnostic), "effective_config_rule": config_rule,
                                "filter_rule": doc.get("filter_rule")}


def _load_scope_contract(timeline: Path, manifest: dict[str, Any], explicit: Path | None) -> dict[str, Any] | None:
    candidate = explicit
    if candidate is None and manifest.get("scope_contract"):
        candidate = Path(manifest["scope_contract"])
    if candidate is None:
        default = timeline / "scope_contract.json"
        candidate = default if default.is_file() else None
    if candidate is None:
        return None
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"scope contract missing: {candidate}")
    value = json.loads(candidate.read_text())
    if not isinstance(value, dict):
        raise ValueError("scope contract must be a JSON object")
    value = dict(value)
    value["path"] = str(candidate)
    value["sha256"] = sha(candidate)
    return value


def _decision_fields(state: dict[str, Any], label_policy: RoomBeliefPolicy,
                     scope: dict[str, Any]) -> dict[str, Any]:
    candidates = state.get("candidates", [])
    category_counts = state.get("category_counts", {})
    candidate_label = candidates[0][0] if candidates and category_counts and scope.get("status") in {"verified", "verified_single_observed_floor_scope", "assumed"} else None
    candidate_belief = float(candidates[0][1]) if candidate_label is not None else None
    runner_up_belief = float(candidates[1][1]) if candidate_label is not None and len(candidates) > 1 else 0.0
    margin = candidate_belief - runner_up_belief if candidate_belief is not None else None
    if scope.get("status") not in {"verified", "verified_single_observed_floor_scope", "assumed"}:
        reason = "unavailable_elevation_scope"
    elif state.get("label") is not None:
        reason = "label_gate_met"
    elif len(category_counts) < label_policy.minimum_supported_categories:
        reason = "insufficient_support"
    elif candidate_belief is not None and candidate_belief < label_policy.minimum_top_belief:
        reason = "belief_below_threshold"
    elif margin is not None and margin < label_policy.minimum_margin:
        reason = "margin_below_threshold"
    elif state.get("pending_label") is not None:
        reason = "relabel_hysteresis_pending"
    elif state.get("status") == "ambiguous":
        reason = "competing_labels"
    elif not category_counts:
        reason = "insufficient_support"
    else:
        reason = str(state.get("status", "unknown"))
    return {
        "candidate_label": candidate_label, "candidate_belief": candidate_belief,
        "top_two_margin": margin, "committed_label": state.get("label"),
        "commit_status": "committed" if state.get("label") is not None else "abstain",
        "abstain_reason": None if state.get("label") is not None else reason,
        "decision_reason": reason,
    }


def _object_exclusion_reason(obj: dict[str, Any], scope: dict[str, Any],
                             tables: dict[str, dict[str, dict[str, float]]]) -> str | None:
    if scope.get("status") not in {"verified", "verified_single_observed_floor_scope", "assumed"}:
        return "unavailable_elevation_scope"
    admission = obj["prefix_admission"]
    if not admission.get("eligible"):
        return "prefix_admission_provisional"
    semantic = obj["semantic"]
    if semantic.get("status") != "stable":
        return f"semantic_{semantic.get('status', 'unavailable')}"
    category = semantic.get("category")
    if category not in tables[VARIANTS[0]]:
        return "unsupported_category"
    if obj["membership"].get("status") != "assigned":
        return str(obj["membership"].get("reason", "membership_unavailable"))
    return None


def run(timeline: Path, output: Path, a_zip: Path, scope_contract_path: Path | None = None) -> dict[str, Any]:
    start = time.perf_counter()
    timeline = timeline.resolve(); output = output.resolve(); a_zip = a_zip.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output must be new and empty: {output}")
    output.mkdir(parents=True, exist_ok=False); (output / "revisions").mkdir()
    manifest = json.loads((timeline / "manifest.json").read_text())
    frame_items = json.loads((timeline / manifest["snapshot_index"]).read_text())["frames"]
    frame_items = sorted(frame_items, key=lambda x: int(x["frame_index"]))
    evidence_path = Path(manifest["mapping_output"]) / "diagnostic/evidence_manifest.json"
    evidence = json.loads(evidence_path.read_text()).get("observations", {})
    tables, categories, semantic_rooms, a_digests = load_a_tables(a_zip)
    semantic_provenance = _load_effective_semantic_config(timeline, manifest)
    effective_config = semantic_provenance["effective_config"]
    admission_min, admission_provenance = _load_admission_threshold(timeline, manifest, effective_config)
    policy_cfg = json.loads((ROOT / "configs/room_semantics/consensus_v1.proposal.json").read_text())
    label_cfg = json.loads((ROOT / "configs/room_semantics/room_belief_v1.proposal.json").read_text())
    consensus_policy = ConsensusPolicy(policy_cfg["policy_id"], policy_cfg["view_group_policy_id"],
        int(policy_cfg["minimum_view_groups"]), float(policy_cfg["minimum_top_share"]), float(policy_cfg["minimum_margin"]))
    label_policy = RoomBeliefPolicy(label_cfg["policy_id"], int(label_cfg["minimum_supported_categories"]),
        float(label_cfg["minimum_top_belief"]), float(label_cfg["minimum_margin"]),
        float(label_cfg["relabel_margin"]), int(label_cfg["relabel_evidence_revisions"]))
    membership_policy = label_cfg["membership"]
    voxel_size_m = float(membership_policy["voxel_size_m"])
    voxel_origin = [float(x) for x in membership_policy["voxel_grid_origin_xyz_m"]]
    room_revision_files = manifest.get("room_revision_files", {})
    room_doc_paths: list[Path] = []
    for item in frame_items:
        key = str(item["frame_index"])
        rel = room_revision_files.get(key)
        if rel is None:
            raise ValueError(f"room revision index missing frame_index={key}")
        room_doc_paths.append(timeline / rel)
    scope_contract = _load_scope_contract(timeline, manifest, scope_contract_path)
    if scope_contract and scope_contract.get("mode") == "assumed_single_level_demo":
        scope_contract["_assumed_validation"] = validate_assumed_scope_declaration(
            scope_contract, Path(scope_contract["path"]), timeline, manifest, frame_items,
            a_zip, semantic_provenance)
    input_set_hashes = _input_set_hashes(timeline, manifest, frame_items)
    # Start with no physical-room catalog. A room is registered only when its
    # own revision becomes active; future room IDs are not needed to process a
    # prefix. The semantic A-table room labels remain fixed separately.
    ledgers = make_ledgers([], tables, semantic_rooms, label_policy, a_digests)
    active_by_variant: dict[str, set[str]] = {v: set() for v in VARIANTS}
    previous_rooms: dict[str, str | None] = {}
    revision_rows = []
    geometry_root = Path(manifest.get("object_geometry_root", timeline)).resolve()
    previous_active_rooms: set[str] = set()
    scope_by_frame: dict[str, dict[str, Any]] = {}
    prefix_room_docs: list[dict[str, Any]] = []
    previous_label_history_counts: dict[tuple[str, str], int] = {}
    for item_index, item in enumerate(frame_items):
        room_doc = json.loads(room_doc_paths[item_index].read_text())
        prefix_room_docs.append(room_doc)
        snap = json.loads((timeline / item["snapshot"]).read_text())
        frame = int(snap["frame_index"])
        polys = room_nodes(room_doc)
        active_rooms = set(polys)
        for ledger in ledgers.values():
            for room_id in sorted(active_rooms):
                ledger.register_room_node(room_id)
        # Scope is a prefix decision. Final room/object snapshots remain
        # provenance only and cannot retroactively open an earlier prefix.
        scope = scope_assessment(timeline, prefix_room_docs, manifest,
                                 frame_items[:item_index + 1], evidence, scope_contract)
        scope_by_frame[str(frame)] = scope
        room_revision = int(room_doc.get("snapshot", room_doc).get("revision", item["revision"]))
        room_created = sorted(active_rooms - previous_active_rooms)
        room_retired = sorted(previous_active_rooms - active_rooms)
        objects_out = []
        room_contains_edges = []
        events_by_variant = {}
        pending_events: dict[str, list[tuple[RoomBeliefEvent, bool]]] = {v: [] for v in VARIANTS}
        current_eligible_by_variant: dict[str, set[str]] = {v: set() for v in VARIANTS}
        current_keys = set()
        for obj in snap.get("objects", []):
            key = object_key(obj); current_keys.add(key)
            obs_records = []
            for oid in obj.get("observation_ids", []):
                if frame_number(oid) > frame:
                    raise AssertionError(f"future observation leaked into prefix {frame}: {oid}")
                ev = evidence.get(oid, {})
                pose = ev.get("camera_pose_W_Copt")
                # Exact matrix equality is the locked view-group policy.
                pose_key = hashlib.sha256(json.dumps(pose, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16] if pose else "missing"
                obs_records.append(SemanticObservation(
                    observation_id=str(oid), frame_id=str(obj.get("frame_indices", [frame])[0]),
                    view_group_id="pose_exact:" + pose_key, raw_class=ev.get("raw_class") or (obj.get("class_history_raw") or [None])[0],
                    category=canonical(ev.get("raw_class") or (obj.get("class_history_raw") or [None])[0], semantic_provenance["canonical_aliases"]),
                    admission_status="retained" if int(obj.get("detection_count", 0)) >= admission_min else "deferred"))
            prefix_admit = int(obj.get("detection_count", 0)) >= admission_min
            consensus = aggregate_object_semantics(key, obs_records, semantic_revision=frame + 1, policy=consensus_policy)
            mem = membership_for(obj, polys, scope, geometry_root, previous_rooms.get(key), room_revision,
                                 voxel_size_m, voxel_origin)
            if mem.get("status") == "assigned":
                previous_rooms[key] = mem.get("room_id")
            elif mem.get("status") in {"unassigned", "ambiguous"}:
                previous_rooms[key] = None
            semantic_doc = consensus.as_dict() | {
                "scope_status": scope["status"], "declaration_id": scope.get("declaration_id"),
                "declaration_sha256": scope.get("declaration_sha256"),
                "future_observations_used": False,
            }
            objects_out.append({
                "causal_object_id": key, "snapshot_hypothesis_id": obj["hypothesis_id"],
                "observation_ids": list(obj.get("observation_ids", [])),
                "detection_count": int(obj.get("detection_count", 0)),
                "distinct_frame_count": int(obj.get("distinct_frame_count", 0)),
                "distinct_pose_count": int(obj.get("distinct_pose_count", 0)),
                "raw_class_history": list(obj.get("class_history_raw", [])),
                "prefix_admission": {"eligible": prefix_admit, "status": "eligible" if prefix_admit else "provisional",
                                     "reason": f"detection_count>=obj_min_detections({admission_min})" if prefix_admit else f"detection_count<obj_min_detections({admission_min}); provisional_only"},
                "semantic": semantic_doc, "membership": mem,
                "room_evidence_eligible": False, "geometry_revision": obj.get("geometry_revision"),
            })
            if mem.get("status") == "assigned" and mem.get("room_id"):
                room_contains_edges.append({"relation": "room_contains", "source_id": str(mem["room_id"]),
                                            "target_id": key, "provenance": "causal_rb1_greedy_voxel_support",
                                            "revision": frame + 1})
            # One event per current object state; all variants share causal evidence.
            object_room_evidence_eligible = False
            for variant in VARIANTS:
                eligible = room_evidence_eligible(prefix_admit, consensus.status, mem,
                                                  consensus.category, tables[variant])
                object_room_evidence_eligible = object_room_evidence_eligible or eligible
                evidence_obj = RoomObjectEvidence(
                    object_id=key, revision=frame + 1, retained=eligible,
                    semantic_status=consensus.status, category=consensus.category,
                    membership_status="assigned" if mem.get("status") == "assigned" else "unassigned",
                    room_id=mem.get("room_id") if mem.get("status") == "assigned" else None,
                    source_observation_ids=tuple(consensus.eligible_observation_ids),
                    provenance=f"causal-hybrid-prefix:{variant}", membership_evidence=mem,
                    semantic_evidence={"status": consensus.status, "category": consensus.category,
                                       "view_group_count": consensus.view_group_count,
                                       "policy_id": consensus.policy_id, "future_observations_used": False,
                                       "scope_status": scope["status"],
                                       "declaration_id": scope.get("declaration_id"),
                                       "declaration_sha256": scope.get("declaration_sha256")})
                event = RoomBeliefEvent(f"causal:{variant}:{key}:{frame+1}", key, frame + 1, evidence_obj)
                pending_events[variant].append((event, eligible))
                if eligible:
                    current_eligible_by_variant[variant].add(key)
            objects_out[-1]["room_evidence_eligible"] = object_room_evidence_eligible
        # Build one deterministic transaction per variant.  Objects absent
        # from this prefix are retracted in the same frame transaction.
        for variant in VARIANTS:
            for key in sorted(active_by_variant[variant] - current_keys):
                event = RoomBeliefEvent(f"causal:{variant}:{key}:{frame+1}", key, frame + 1, None)
                pending_events[variant].append((event, False))
            results = ledgers[variant].apply_frame([event for event, _ in pending_events[variant]])
            results_by_event_id = {str(result["event_id"]): result for result in results}
            events_by_variant[variant] = [
                {"event": event.as_dict(), "result": results_by_event_id[event.event_id], "eligible": eligible,
                 "retracted": event.evidence is None}
                for event, eligible in pending_events[variant]
            ]
            active_by_variant[variant] = set(current_eligible_by_variant[variant])
        room_states = {}
        room_diagnostics = {}
        for variant in VARIANTS:
            # Future/retired rooms are not emitted as semantic states.
            room_states[variant] = {}
            room_diagnostics[variant] = {}
            for rid in sorted(active_rooms):
                raw_state = ledgers[variant].room_state(rid).as_dict()
                decision = _decision_fields(raw_state, label_policy, scope)
                history = raw_state.get("label_history", [])
                history_key = (variant, rid)
                previous_count = previous_label_history_counts.get(history_key, 0)
                history_delta = history[previous_count:]
                previous_label_history_counts[history_key] = len(history)
                decorated_state = raw_state | decision | {
                    "scope_status": scope["status"], "declaration_id": scope.get("declaration_id"),
                    "declaration_sha256": scope.get("declaration_sha256"),
                    "label_history_delta": history_delta,
                }
                room_states[variant][rid] = decorated_state
                assigned = [obj for obj in objects_out if obj["membership"].get("status") == "assigned"
                            and obj["membership"].get("room_id") == rid]
                eligible_objects = [obj for obj in assigned if room_evidence_eligible(
                    bool(obj["prefix_admission"]["eligible"]), obj["semantic"]["status"],
                    obj["membership"], obj["semantic"]["category"], tables[variant])]
                eligible_categories = sorted({str(obj["semantic"]["category"]) for obj in eligible_objects})
                exclusions = [{"object_id": obj["causal_object_id"], "reason": _object_exclusion_reason(obj, scope, tables)}
                              for obj in assigned if obj not in eligible_objects]
                contributions = [{"category": cat, "A_log_ratio_by_room_type": tables[variant][cat],
                                  "object_ids": sorted(obj["causal_object_id"] for obj in eligible_objects
                                                        if obj["semantic"]["category"] == cat)}
                                 for cat in eligible_categories]
                room_diagnostics[variant][rid] = {
                    "eligible_objects": sorted(obj["causal_object_id"] for obj in eligible_objects),
                    "eligible_categories": eligible_categories,
                    "category_contributions": contributions,
                    "membership_support": [{"object_id": obj["causal_object_id"],
                                            "category": obj["semantic"].get("category"),
                                            "support_counts": obj["membership"].get("support_counts", {}),
                                            "support_fraction": obj["membership"].get("support_fraction"),
                                            "runner_up_fraction": obj["membership"].get("runner_up_fraction"),
                                            "outside_fraction": obj["membership"].get("outside_fraction"),
                                            "tied": obj["membership"].get("tied")}
                                           for obj in assigned],
                    "excluded_objects": exclusions,
                    "raw_S": raw_state["raw_scores"], "beliefs": raw_state["candidates"],
                    "candidate_label": decision["candidate_label"],
                    "committed_label": decision["committed_label"],
                    "commit_status": decision["commit_status"], "abstain_reason": decision["abstain_reason"],
                    "decision_reason": decision["decision_reason"],
                    "label_history_delta": history_delta,
                    "scope_status": scope["status"], "declaration_id": scope.get("declaration_id"),
                    "declaration_sha256": scope.get("declaration_sha256"),
                }
        room_geometry_rel = room_revision_files[str(frame)]
        object_snapshot_rel = str(item["snapshot"])
        row = {"schema": "causal-hybrid-room-belief-revision/v1", "frame_index": frame,
               "frame_id": snap["frame_id"], "revision": snap["revision"], "timestamp_s": snap.get("timestamp_s"),
               "future_observations_used": False, "scope_status": scope["status"],
               "declaration_id": scope.get("declaration_id"), "declaration_sha256": scope.get("declaration_sha256"),
               "source_prefix": {"object_snapshot_path": object_snapshot_rel,
                                 "object_snapshot_sha256": sha(timeline / object_snapshot_rel),
                                 "object_revision": item.get("object_revision", item["revision"]),
                                 "unified_revision": item.get("unified_revision"),
                                 "room_revision_path": str(room_geometry_rel),
                                 "room_revision_sha256": sha(timeline / room_geometry_rel),
                                 "room_revision": room_revision, "frame_index": frame},
               "future_observations_used": False, "objects": objects_out, "rooms": room_states,
               "room_lifecycle": {"active_room_ids": sorted(active_rooms), "created_room_ids": room_created,
                                  "retired_room_ids": room_retired, "room_revision": room_revision},
               "membership_scope": scope, "membership_causal": scope["status"] in {"verified", "verified_single_observed_floor_scope", "assumed"},
               "room_contains_edges": room_contains_edges, "room_semantics_status": "available_but_no_membership_evidence" if scope["status"] not in {"verified", "verified_single_observed_floor_scope"} else "causal",
               "room_diagnostics": room_diagnostics, "events": events_by_variant}
        if scope["status"] == "unavailable":
            row["room_semantics_status"] = "unavailable_elevation_scope"
        elif scope["status"] == "assumed":
            row["room_semantics_status"] = "assumed_experimental"
        else:
            row["room_semantics_status"] = "causal_verified_scope"
        (output / "revisions" / f"rev-{int(item['revision']):06d}.json").write_text(json.dumps(row, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        revision_rows.append({"frame_index": frame, "frame_id": snap["frame_id"], "revision": snap["revision"],
                              "unified_revision": item.get("unified_revision"),
                              "state": f"revisions/rev-{int(item['revision']):06d}.json", "future_observations_used": False,
                              "object_count": len(objects_out), "room_contains_count": len(room_contains_edges)})
        previous_active_rooms = active_rooms
    out_manifest = {
        "schema": "causal-hybrid-room-belief-timeline/v1", "status": "complete",
        "source_timeline": str(timeline), "source_timeline_manifest_sha256": sha(timeline / "manifest.json"),
        "source_a_zip": str(a_zip), "source_a_zip_sha256": sha(a_zip),
        "implementation_provenance": {
            "runner_path": str(Path(__file__).resolve()), "runner_sha256": sha(Path(__file__).resolve()),
            "ledger_path": str((ROOT / "src/standalone_sg/semantics/room_belief_v1.py").resolve()),
            "ledger_sha256": sha(ROOT / "src/standalone_sg/semantics/room_belief_v1.py"),
        },
        "scene_id": manifest.get("scene_id"), "sequence_id": manifest.get("sequence_id"),
        "run_id": semantic_provenance.get("run_id"),
        "frame_count": len(revision_rows), "revisions": revision_rows,
        "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference",
                      "membership": ("unavailable_elevation_scope" if scope["status"] == "unavailable" else
                                     "prefix_greedy_voxel_support_assumed_single_level_demo" if scope["status"] == "assumed" else
                                     "prefix_greedy_voxel_support_verified_scope"),
                      "room_semantics": ("unavailable_elevation_scope" if scope["status"] == "unavailable" else
                                         "causal_rb1_prefix_assumed_experimental" if scope["status"] == "assumed" else
                                         "causal_rb1_prefix_verified_scope")},
        "stage_semantics": {"online_hypotheses": "source M1 snapshot; unchanged", "prefix_admission": "effective cached diagnostic admission only; eligibility, not final filter", "final_postprocess": "not inserted; remains separate"},
        "scope_status": scope["status"], "scope_declaration_id": scope.get("declaration_id"),
        "scope_declaration_sha256": scope.get("declaration_sha256"),
        "scope_declaration": {k: v for k, v in (scope_contract or {}).items() if not k.startswith("_")},
        "input_set_hashes": input_set_hashes,
        "input_provenance": (manifest.get("input_provenance") or {
                             "timeline_manifest_sha256": sha(timeline / "manifest.json"),
                             "c2_run_manifest_sha256": semantic_provenance["c2_run_manifest_sha256"],
                             "capture_manifest_sha256": sha(Path(manifest["cache_root"]) / "capture/capture_manifest.json"),
                             "object_snapshot_set_sha256": input_set_hashes["object_snapshot_set_sha256"],
                             "room_revision_set_sha256": input_set_hashes["room_revision_set_sha256"],
                             "object_cloud_set_sha256": input_set_hashes["object_cloud_set_sha256"],
                             "observation_evidence_manifest_sha256": sha(evidence_path),
                             "admission_diagnostic_sha256": admission_provenance["sha256"],
                             "room_delta_log_sha256": sha(Path(manifest["cache_root"]) / "replay/delta_log.json"),
                             "a_zip_sha256": sha(a_zip),
                             "effective_c2_config_sha256": semantic_provenance["effective_c2_config_sha256"],
                             "detector_profile_sha256": semantic_provenance["detector_profile_sha256"]}),
        "effective_semantic_provenance": {k: v for k, v in semantic_provenance.items() if k != "effective_config"},
        "membership_scope": scope, "membership_policy": {"policy_id": label_cfg["membership"]["policy"],
            "voxel_size_m": float(label_cfg["membership"]["voxel_size_m"]),
            "grid_origin_xyz_m": list(label_cfg["membership"]["voxel_grid_origin_xyz_m"]),
            "room_contains_emitted_when_scope_status_in": ["verified", "verified_single_observed_floor_scope", "assumed"]},
        "membership_scope_by_frame": scope_by_frame,
        "semantic_policy": policy_cfg, "room_belief_policy": label_cfg,
        "admission_policy": {"obj_min_detections": admission_min, "provenance": admission_provenance},
        "a_table_digests": a_digests, "a_table_shape": {"categories": len(categories), "rooms": len(semantic_rooms), "rows_per_variant": len(categories) * len(semantic_rooms)},
        "variants": list(VARIANTS), "a_score_field": "A_log_ratio_uncalibrated", "category_cap": "one contribution per unique eligible category; RB1 ledger",
        "native_membership_used_for_hybrid": False, "future_observations_used": False,
        "policy_config_provenance": {
            "consensus_path": str((ROOT / "configs/room_semantics/consensus_v1.proposal.json").resolve()),
            "consensus_sha256": sha(ROOT / "configs/room_semantics/consensus_v1.proposal.json"),
            "room_belief_path": str((ROOT / "configs/room_semantics/room_belief_v1.proposal.json").resolve()),
            "room_belief_sha256": sha(ROOT / "configs/room_semantics/room_belief_v1.proposal.json"),
            "alias_map": semantic_provenance["canonical_aliases"],
        },
        "runtime_s": time.perf_counter() - start,
    }
    (output / "manifest.json").write_text(json.dumps(out_manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    (output / "run_summary.json").write_text(json.dumps({"manifest": "manifest.json", "scope": scope,
        "frames": len(revision_rows), "final_object_count": revision_rows[-1]["object_count"],
        "room_contains_count": revision_rows[-1]["room_contains_count"], "variants": list(VARIANTS)}, indent=2) + "\n")
    return out_manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeline", type=Path, required=True)
    ap.add_argument("--a-zip", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--scope-contract", type=Path, default=None,
                    help="optional locked room-scope contract; without it 2-D room membership stays unavailable")
    args = ap.parse_args()
    result = run(args.timeline, args.output, args.a_zip, args.scope_contract)
    print(json.dumps({"status": result["status"], "output": str(args.output.resolve()),
                      "frames": result["frame_count"], "membership": result["membership_scope"],
                      "runtime_s": result["runtime_s"]}, indent=2))


if __name__ == "__main__":
    main()
