#!/usr/bin/env python3
"""Adapt one completed T1.1 mapper-only run to the existing causal T3 RB1 runner.

This is a read-only semantic replay over already committed prefix snapshots. It
does not run perception, either mapper, Qwen, or terminal postprocessing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_causal_hybrid_room_belief import VARIANTS, run as run_causal_rb1


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_mapper_run(mapper_run: Path, a_zip: Path) -> dict[str, Any]:
    """Validate all source identities needed by the semantic adapter."""
    mapper_run = mapper_run.resolve()
    manifest_path = mapper_run / "run_manifest.json"
    receipt_path = mapper_run / "run_manifest.sha256.json"
    manifest = _load(manifest_path)
    receipt = _load(receipt_path)
    manifest_sha = sha(manifest_path)
    # The v8 receipt schema names the digest field simply ``sha256``.
    # Accept its explicit legacy spellings too, but reject disagreement rather
    # than choosing one silently.
    receipt_values = [receipt.get(k) for k in ("sha256", "manifest_sha256", "run_manifest_sha256") if receipt.get(k) is not None]
    if not receipt_values or len(set(receipt_values)) != 1:
        raise ValueError("mapper run manifest receipt digest is missing or conflicting")
    receipt_expected = receipt_values[0]
    if receipt_expected != manifest_sha:
        raise ValueError("mapper run manifest receipt does not match run_manifest.json")
    if manifest.get("status") != "completed" or manifest.get("execution_mode") != "mapper_only_replay_from_validated_perception_packets":
        raise ValueError("source must be a completed mapper-only replay run")
    if manifest.get("run_id") != mapper_run.name:
        raise ValueError("mapper run directory and manifest run_id differ")
    expected_indices = list(manifest.get("ordered_frame_indices", []))
    if len(expected_indices) != 13 or len(set(expected_indices)) != 13 or expected_indices != sorted(expected_indices):
        raise ValueError("locked mapper-only source must contain the 13 ordered T1.1 keyframes")
    if manifest.get("execution_plan", {}).get("conceptgraphs_updates") != 13 or manifest.get("execution_plan", {}).get("occusg_updates") != 13:
        raise ValueError("source mapper execution plan does not attest 13 updates per branch")
    if manifest.get("terminal_postprocess_is_separate") is not True:
        raise ValueError("source mapper run does not separate terminal processing")

    immutable = manifest.get("immutable_output_sha256")
    if not isinstance(immutable, dict) or not immutable:
        raise ValueError("immutable output hash inventory is missing")
    for rel, expected in immutable.items():
        path = mapper_run / rel
        if not path.is_file() or sha(path) != expected:
            raise ValueError(f"immutable source hash mismatch: {rel}")

    a_zip = a_zip.resolve()
    if not a_zip.is_file():
        raise FileNotFoundError(f"A tables archive missing: {a_zip}")
    source_refs = {int(x["frame_index"]): x for x in manifest.get("source_packet_refs", [])}
    if sorted(source_refs) != expected_indices:
        raise ValueError("source packet references do not match the locked 13-frame population")

    packet_docs: dict[int, dict[str, Any]] = {}
    packet_rows: list[dict[str, Any]] = []
    p2_by_observation: dict[str, dict[str, Any]] = {}
    raw_run_root: Path | None = None
    for frame_index in expected_indices:
        ref = source_refs[frame_index]
        packet_path = Path(ref["run_packet_path"]).resolve()
        if not packet_path.is_file() or sha(packet_path) != ref.get("run_packet_sha256"):
            raise ValueError(f"packet reference hash mismatch at frame {frame_index}")
        packet = _load(packet_path)
        if (packet.get("scene_id"), packet.get("sequence_id"), packet.get("frame_index"), packet.get("frame_id")) != (
                manifest.get("scene_id"), manifest.get("sequence_id"), frame_index, ref.get("frame_id")):
            raise ValueError(f"packet identity mismatch at frame {frame_index}")
        if packet.get("perception_status") not in {"completed", "reused_validated"}:
            raise ValueError(f"packet perception is not ready at frame {frame_index}")
        capture = packet.get("capture_record", {})
        if capture.get("timestamp_clock_domain") != "habitat_simulation" or not math.isfinite(float(capture.get("timestamp_s", math.nan))):
            raise ValueError(f"simulation timestamp provenance missing at frame {frame_index}")
        if capture.get("width") != 640 or capture.get("height") != 480:
            raise ValueError(f"source capture resolution mismatch at frame {frame_index}")
        if ref.get("source_hashes") != packet.get("source_hashes"):
            raise ValueError(f"raw source hash listing mismatch at frame {frame_index}")

        raw_source = packet.get("raw_keyframe_source", {})
        raw_path = Path(raw_source.get("source_path", "")).resolve()
        if not raw_path.is_dir():
            raise FileNotFoundError(f"raw T1.1 keyframe directory missing at frame {frame_index}")
        run_root = raw_path.parents[1]
        if raw_run_root is None:
            raw_run_root = run_root
        if run_root != raw_run_root or raw_source.get("run_id") != "camera_live_t1_1_20261018_v8":
            raise ValueError("the 13 packets do not reference one locked T1.1/v8 capture run")
        raw_file_hashes = raw_source.get("files_sha256", {})
        if not isinstance(raw_file_hashes, dict) or not raw_file_hashes:
            raise ValueError(f"raw source file hashes missing at frame {frame_index}")
        for name, expected_hash in raw_file_hashes.items():
            source_file = raw_path / name
            if not source_file.is_file() or sha(source_file) != expected_hash:
                raise ValueError(f"raw capture hash mismatch at frame {frame_index}: {name}")
        for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy"):
            if raw_file_hashes.get(name) != packet["source_hashes"].get(name):
                raise ValueError(f"raw/processed sensor asset hash differs at frame {frame_index}: {name}")

        for det in packet.get("p2_detections", []):
            oid = str(det.get("detection_id", ""))
            raw_class = det.get("class_raw")
            if not oid or not isinstance(raw_class, str) or not raw_class.strip():
                raise ValueError(f"P2 observation lacks full ID/raw class at frame {frame_index}")
            if oid in p2_by_observation:
                raise ValueError(f"duplicate P2 observation ID in source packets: {oid}")
            if not oid.startswith(str(packet["frame_id"]) + ":det-"):
                raise ValueError(f"P2 observation ID does not belong to packet frame {frame_index}")
            p2_by_observation[oid] = {"frame_id": packet["frame_id"], "frame_index": frame_index,
                                      "raw_class": raw_class}

        snapshot_path = mapper_run / "branches/objects/snapshots" / f"rev-{frame_index:06d}.json"
        # Snapshot filenames encode source frame IDs, not object revision IDs.
        if not snapshot_path.is_file():
            raise FileNotFoundError(f"object prefix snapshot missing for frame {frame_index}")
        snapshot = _load(snapshot_path)
        expected_object_revision = int(frame_index) + 1
        if (snapshot.get("scene_id"), snapshot.get("sequence_id"), snapshot.get("frame_id"), snapshot.get("frame_index"), snapshot.get("revision")) != (
                manifest.get("scene_id"), manifest.get("sequence_id"), packet["frame_id"], frame_index, expected_object_revision):
            raise ValueError(f"object prefix snapshot identity/revision mismatch at frame {frame_index}")
        if snapshot.get("future_observations_used") is not False or snapshot.get("stage") != "online_hypotheses":
            raise ValueError(f"object snapshot is not an online prefix at frame {frame_index}")
        pose = snapshot.get("camera_pose_W_Copt")
        if not isinstance(pose, list) or len(pose) != 4 or any(len(row) != 4 for row in pose):
            raise ValueError(f"camera pose missing/malformed at frame {frame_index}")
        if abs(float(snapshot.get("timestamp_s", math.nan)) - float(capture["timestamp_s"])) > 1e-6:
            raise ValueError(f"object snapshot timestamp mismatch at frame {frame_index}")
        room_path = mapper_run / "branches/rooms/snapshots" / f"rev-{frame_index:06d}.json"
        room_doc = _load(room_path)
        room = room_doc.get("snapshot", room_doc)
        if room.get("scene_id") != manifest.get("scene_id") or room.get("sequence_id") != manifest.get("sequence_id"):
            raise ValueError(f"room revision scene/sequence mismatch at frame {frame_index}")
        room_revision = int(room.get("revision", -1))
        if room_revision != expected_object_revision:
            # OccuSG revision count is ordinal while the object branch keeps
            # source-frame+1 revisions. The joined manifest is authoritative.
            room_revision = int(frame_index_to_ordinal(expected_indices, frame_index))
            if int(room.get("revision", -1)) != room_revision:
                raise ValueError(f"room revision is not aligned by frame identity at frame {frame_index}")
        if abs(float(room.get("timestamp_s", math.nan)) - float(capture["timestamp_s"])) > 1e-6:
            raise ValueError(f"room revision timestamp mismatch at frame {frame_index}")
        joined_path = mapper_run / "joined/graph_states" / f"rev-{frame_index_to_ordinal(expected_indices, frame_index):06d}.json"
        joined = _load(joined_path)
        if (joined.get("scene_id"), joined.get("sequence_id"), joined.get("frame_id"), joined.get("frame_index")) != (
                manifest.get("scene_id"), manifest.get("sequence_id"), packet["frame_id"], frame_index):
            raise ValueError(f"joined graph frame identity mismatch at frame {frame_index}")
        refs = joined.get("source_revisions", {})
        if int(refs.get("object", {}).get("source_revision", -1)) != expected_object_revision or int(refs.get("room", {}).get("source_revision", -1)) != room_revision:
            raise ValueError(f"joined graph object/room revision references mismatch at frame {frame_index}")
        if abs(float(joined.get("timestamp_s", math.nan)) - float(capture["timestamp_s"])) > 1e-6:
            raise ValueError(f"joined graph timestamp mismatch at frame {frame_index}")
        unified_match = re.fullmatch(r"unified-(\d+)", str(joined.get("graph_revision_id", "")))
        if unified_match is None or int(unified_match.group(1)) != frame_index_to_ordinal(expected_indices, frame_index):
            raise ValueError(f"joined unified revision is not aligned by frame identity at frame {frame_index}")
        for obj in snapshot.get("objects", []):
            for oid in obj.get("observation_ids", []):
                if oid not in p2_by_observation:
                    raise ValueError(f"prefix object references a P2 observation absent from the locked packets: {oid}")
                frame_match = re.search(r"-cam-(\d+):det-", str(oid))
                if not frame_match or int(frame_match.group(1)) > frame_index:
                    raise ValueError(f"future/malformed observation leaked into frame {frame_index}: {oid}")
            cloud = mapper_run / "branches/objects" / obj["cloud_npz"]
            if not cloud.is_file() or sha(cloud) != obj.get("cloud_sha256"):
                raise ValueError(f"prefix object cloud missing/hash mismatch at frame {frame_index}")
        packet_docs[frame_index] = packet
        packet_rows.append({"frame_index": frame_index, "frame_id": packet["frame_id"],
                            "packet_path": str(packet_path), "packet_sha256": sha(packet_path),
                            "object_snapshot_path": str(snapshot_path), "object_snapshot_sha256": sha(snapshot_path),
                            "object_revision": expected_object_revision, "room_revision": room_revision,
                            "room_snapshot_path": str(room_path), "room_snapshot_sha256": sha(room_path),
                            "joined_state_path": str(joined_path), "joined_state_sha256": sha(joined_path),
                            "unified_revision": int(unified_match.group(1)),
                            "raw_source_path": str(raw_path), "raw_file_hashes": dict(raw_file_hashes),
                            "timestamp_s": float(capture["timestamp_s"]), "camera_pose_W_Copt": pose})

    # Every object observation must be backed by exactly one P2 record in the
    # prefix population; the P2 set may additionally contain rejected/missed
    # associations and is never replaced by final retained state.
    all_object_obs = {oid for frame_index in expected_indices
                      for obj in _load(mapper_run / "branches/objects/snapshots" / f"rev-{frame_index:06d}.json").get("objects", [])
                      for oid in obj.get("observation_ids", [])}
    if not all_object_obs.issubset(set(p2_by_observation)):
        raise ValueError("object snapshot observations are not fully joined to P2 raw class records")

    profile_ref = manifest.get("source_hashes", {}).get("p2_profile", {})
    route_ref = manifest.get("source_hashes", {}).get("effective_route_config", {})
    profile_path = Path(profile_ref.get("path", "")).resolve()
    route_path = Path(route_ref.get("path", "")).resolve()
    if not profile_path.is_file() or sha(profile_path) != profile_ref.get("sha256"):
        raise ValueError("source P2 profile path/hash is missing or invalid")
    if not route_path.is_file() or sha(route_path) != route_ref.get("sha256"):
        raise ValueError("source effective route config path/hash is missing or invalid")
    profile = _load(profile_path)
    aliases = profile.get("vocabulary", {}).get("canonical_map")
    if not isinstance(aliases, dict) or not aliases:
        raise ValueError("source detector profile has no canonical alias map")
    normalized_aliases = {re.sub(r"\s+", " ", str(k).strip().lower()): str(v).strip().lower() for k, v in aliases.items()}
    mapping_config = manifest.get("mapping_effective_config")
    if not isinstance(mapping_config, dict) or type(mapping_config.get("obj_min_detections")) is not int:
        raise ValueError("effective mapper config is missing obj_min_detections")

    # Actual per-observation raw classes come from current-frame P2 packets;
    # pose groups come from the same object-prefix snapshot as the geometry.
    evidence = {}
    for frame_index, packet in packet_docs.items():
        pose = packet_rows[expected_indices.index(frame_index)]["camera_pose_W_Copt"]
        for det in packet.get("p2_detections", []):
            oid = str(det["detection_id"])
            evidence[oid] = {"frame_id": packet["frame_id"], "frame_index": frame_index,
                             "raw_class": str(det["class_raw"]), "camera_pose_W_Copt": pose,
                             "timestamp_s": float(packet["timestamp_s"]),
                             "source_packet_sha256": sha(Path(source_refs[frame_index]["run_packet_path"]))}

    return {"mapper_run": mapper_run, "manifest": manifest, "manifest_path": manifest_path,
            "manifest_sha256": manifest_sha, "receipt_path": receipt_path,
            "receipt_sha256": sha(receipt_path), "source_packet_rows": packet_rows,
            "packet_docs": packet_docs, "p2_by_observation": p2_by_observation,
            "evidence": evidence, "profile_path": profile_path, "profile_sha256": sha(profile_path),
            "route_path": route_path, "route_sha256": sha(route_path), "aliases": normalized_aliases,
            "mapping_config": mapping_config, "mapping_config_sha256": json_hash(mapping_config),
            "raw_capture_run_root": raw_run_root, "a_zip": a_zip, "a_zip_sha256": sha(a_zip)}


def frame_index_to_ordinal(indices: list[int], frame_index: int) -> int:
    try:
        return indices.index(frame_index) + 1
    except ValueError as exc:
        raise ValueError(f"frame index is not in locked source sequence: {frame_index}") from exc


def prepare_adapter(validated: dict[str, Any], adapter_root: Path) -> dict[str, Any]:
    adapter_root.mkdir(parents=True, exist_ok=False)
    diagnostic = adapter_root / "diagnostic"
    diagnostic.mkdir()
    (diagnostic / "evidence_manifest.json").write_text(json.dumps({
        "schema": "t1-1-prefix-semantic-evidence/v1", "status": "complete",
        "source_run_manifest_sha256": validated["manifest_sha256"],
        "observations": validated["evidence"]}, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    manifest = validated["manifest"]
    indices = list(manifest["ordered_frame_indices"])
    frame_rows = []
    effective_prefixes = []
    room_files = {}
    for row in validated["source_packet_rows"]:
        idx = row["frame_index"]
        packet = _load(Path(row["packet_path"]))
        room_doc = _load(Path(row["room_snapshot_path"]))
        room_snapshot = room_doc.get("snapshot", room_doc)
        frame_rows.append({"frame_index": idx, "frame_id": row["frame_id"],
                           "revision": row["object_revision"],
                           "object_revision": row["object_revision"],
                           "room_revision": row["room_revision"],
                           "unified_revision": row["unified_revision"],
                           "snapshot": row["object_snapshot_path"],
                           "timestamp_s": row["timestamp_s"]})
        effective_prefixes.append({
            "frame_id": row["frame_id"], "frame_index": idx,
            "object_revision": row["object_revision"], "room_revision": row["room_revision"],
            "unified_revision": row["unified_revision"],
            "packet_path": row["packet_path"], "packet_sha256": row["packet_sha256"],
            "packet_source_hashes": packet["source_hashes"],
            "object_snapshot_path": row["object_snapshot_path"],
            "object_snapshot_sha256": row["object_snapshot_sha256"],
            "room_snapshot_path": row["room_snapshot_path"],
            "room_snapshot_sha256": row["room_snapshot_sha256"],
            "room_config_hash": room_snapshot.get("config_hash"),
            "joined_graph_state_path": row["joined_state_path"],
            "joined_graph_state_sha256": row["joined_state_sha256"],
            "raw_keyframe_path": row["raw_source_path"],
            "raw_keyframe_file_sha256": dict(sorted(row["raw_file_hashes"].items())),
            "timestamp_s": row["timestamp_s"],
        })
        room_files[str(idx)] = row["room_snapshot_path"]
    (adapter_root / "frame_index.json").write_text(json.dumps({"schema": "t1-1-semantic-prefix-frame-index/v1",
        "frames": frame_rows}, indent=2, sort_keys=True) + "\n")

    source_run_manifest = _load(validated["manifest_path"])
    context = {
        "schema": "causal-room-belief-semantic-context/v1",
        "source_run_id": source_run_manifest["run_id"],
        "source_run_manifest_path": str(validated["manifest_path"]),
        "source_run_manifest_sha256": validated["manifest_sha256"],
        "effective_route_config_path": str(validated["route_path"]),
        "effective_route_config_sha256": validated["route_sha256"],
        "mapping_effective_config": validated["mapping_config"],
        "mapping_effective_config_sha256": validated["mapping_config_sha256"],
        "detector_profile_path": str(validated["profile_path"]),
        "detector_profile_sha256": validated["profile_sha256"],
        "canonical_aliases": validated["aliases"],
    }
    capture_run_manifest = validated["raw_capture_run_root"] / "run_manifest.json"
    if not capture_run_manifest.is_file():
        raise FileNotFoundError(f"source capture run manifest missing: {capture_run_manifest}")
    capture_run_doc = _load(capture_run_manifest)
    packet_set = hash_rows([(r["packet_path"], r["packet_sha256"]) for r in validated["source_packet_rows"]])
    object_set = hash_rows([(r["object_snapshot_path"], r["object_snapshot_sha256"]) for r in validated["source_packet_rows"]])
    room_set = hash_rows([(r["room_snapshot_path"], r["room_snapshot_sha256"]) for r in validated["source_packet_rows"]])
    joined_set = hash_rows([(r["joined_state_path"], r["joined_state_sha256"]) for r in validated["source_packet_rows"]])
    raw_file_rows = [(str(Path(r["raw_source_path"]) / name), digest)
                     for r in validated["source_packet_rows"]
                     for name, digest in sorted(r["raw_file_hashes"].items())]
    raw_file_set = hash_rows(raw_file_rows)
    prefix_set = hashlib.sha256(json.dumps(effective_prefixes, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    cloud_files: set[tuple[str, str]] = set()
    for row in validated["source_packet_rows"]:
        snap = _load(Path(row["object_snapshot_path"]))
        for obj in snap.get("objects", []):
            cloud = validated["mapper_run"] / "branches/objects" / obj["cloud_npz"]
            cloud_files.add((str(obj["cloud_npz"]), sha(cloud)))
    cloud_set = hash_rows(sorted(cloud_files))
    input_provenance = {
        "source_mapper_run_id": manifest["run_id"],
        "source_mapper_run_manifest_path": str(validated["manifest_path"]),
        "source_mapper_run_manifest_sha256": validated["manifest_sha256"],
        "source_mapper_run_receipt_path": str(validated["receipt_path"]),
        "source_mapper_run_receipt_sha256": validated["receipt_sha256"],
        "source_capture_run_id": validated["raw_capture_run_root"].name,
        "source_capture_manifest_run_id": capture_run_doc.get("run_id"),
        "source_capture_run_manifest_path": str(capture_run_manifest),
        "source_capture_run_manifest_sha256": sha(capture_run_manifest),
        "raw_keyframe_count": len(indices), "raw_keyframe_file_set_sha256": raw_file_set,
        "packet_set_sha256": packet_set, "object_snapshot_set_sha256": object_set,
        "room_revision_set_sha256": room_set, "joined_graph_state_set_sha256": joined_set,
        "object_cloud_set_sha256": cloud_set, "effective_prefix_set_sha256": prefix_set,
        "A_archive_sha256": validated["a_zip_sha256"],
        "effective_route_config_sha256": validated["route_sha256"],
        "detector_profile_sha256": validated["profile_sha256"],
        "mapping_effective_config_sha256": validated["mapping_config_sha256"],
        "consensus_policy_sha256": sha(ROOT / "configs/room_semantics/consensus_v1.proposal.json"),
        "room_belief_policy_sha256": sha(ROOT / "configs/room_semantics/room_belief_v1.proposal.json"),
        "raw_p2_evidence_manifest_sha256": sha(diagnostic / "evidence_manifest.json"),
    }
    (adapter_root / "manifest.json").write_text(json.dumps({
        "schema": "causal-hybrid-prefix-timeline-adapter/v1", "status": "validated",
        "scene_id": manifest["scene_id"], "sequence_id": manifest["sequence_id"],
        "run_id": manifest["run_id"], "snapshot_index": "frame_index.json",
        "room_revision_files": room_files,
        "mapping_output": str(adapter_root),
        "cache_root": str(validated["raw_capture_run_root"]),
        "object_geometry_root": str(validated["mapper_run"] / "branches/objects"),
        "effective_mapping_config": validated["mapping_config"],
        "semantic_context": context,
        "input_provenance": input_provenance,
        "effective_prefixes": effective_prefixes,
        "source_run_manifest_sha256": validated["manifest_sha256"],
        "source_frame_indices": indices,
        "future_observations_used": False,
        "scope_contract": None,
        "terminal_postprocess": "not an input to prefix semantic replay",
    }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return {"adapter_manifest": str(adapter_root / "manifest.json"),
            "adapter_manifest_sha256": sha(adapter_root / "manifest.json"),
            "evidence_manifest": str(diagnostic / "evidence_manifest.json"),
            "evidence_manifest_sha256": sha(diagnostic / "evidence_manifest.json"),
            "frame_count": len(indices), "frame_indices": indices,
            "effective_prefixes": effective_prefixes,
            "input_provenance": input_provenance}


def hash_rows(rows: list[tuple[str, str]]) -> str:
    return hashlib.sha256(json.dumps([{"path": p, "sha256": h} for p, h in rows],
                                   sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run(mapper_run: Path, a_zip: Path, output: Path) -> dict[str, Any]:
    started = time.perf_counter()
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output path already exists; use a new run ID: {output}")
    validated = validate_mapper_run(mapper_run, a_zip)
    output.mkdir(parents=True, exist_ok=False)
    adapter = prepare_adapter(validated, output / "input_adapter")
    # No assumed-scope declaration is supplied. T3's existing scope gate must
    # therefore keep membership and room belief unavailable.
    result = run_causal_rb1(output / "input_adapter", output / "verified_only", a_zip, None)
    if result["frame_count"] != 13 or result["scope_status"] != "unavailable":
        raise RuntimeError("T3 verified-only replay did not preserve the 13-prefix closed-scope contract")
    summary = {
        "schema": "t1-1-t3-room-belief-run-summary/v1", "status": "completed",
        "source_run_id": validated["manifest"]["run_id"],
        "source_capture_run_id": "camera_live_t1_1_20261018_v8",
        "frame_count": 13, "frame_indices": adapter["frame_indices"],
        "semantic_run_manifest": "verified_only/manifest.json",
        "scope_status": result["scope_status"],
        "membership": "unavailable_elevation_scope",
        "room_semantics": "unavailable_elevation_scope",
        "assumed_scope": "not_applied; declaration prepared separately for user decision",
        "mapper_replay_count": 0, "capture_perception_model_runs": 0,
        "runtime_s": time.perf_counter() - started,
    }
    (output / "run_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    report = {"schema": "t1-1-t3-room-belief-receipt/v1", **summary,
              "source_mapper_manifest_sha256": validated["manifest_sha256"],
              "adapter": adapter,
              "semantic_manifest_sha256": sha(output / "verified_only/manifest.json"),
              "semantic_revision_count": result["frame_count"],
              "variant_ids": result["variants"],
              "viewer_status": "not_built_yet"}
    (output / "receipt.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mapper-run", type=Path, required=True)
    ap.add_argument("--a-zip", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    result = run(args.mapper_run, args.a_zip, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
