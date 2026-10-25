"""Read-only preflight contracts for the fixed-assignment fusion launcher."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def p3_detection_map(path: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for batch in read_jsonl(path):
        for detection in batch.get("detections", []):
            oid = str(detection.get("detection_id", ""))
            if not oid or oid in result:
                raise ValueError(f"P3 cache contains missing/duplicate observation ID: {oid or '<missing>'}")
            result[oid] = detection
    return result


def validate_lineage(replay_root: Path, observation_ids: set[str]) -> dict:
    replay = replay_root / "replay" if (replay_root / "replay").is_dir() else replay_root
    path = replay / "object_association_lineage.jsonl"
    if not path.is_file():
        raise ValueError(f"association lineage missing: {path}")
    rows = read_jsonl(path)
    ledger: dict[str, dict] = {}
    for row in rows:
        oid = str(row.get("detection_id", ""))
        if oid not in observation_ids:
            continue
        if oid in ledger:
            raise ValueError(f"duplicate association lineage rows for {oid}")
        ledger[oid] = row
    missing = sorted(observation_ids - ledger.keys())
    if missing:
        raise ValueError(f"lineage is incomplete ({len(missing)} accepted P3 observations lack a decision): {missing[:5]}")
    assigned_decisions = {"created", "geometry_match", "same_class_match", "same_pose_suppressed", "matched"}
    alias_decisions = {"duplicate_suppressed", "duplicate_alias"}
    unassigned_decisions = {"unassigned", "unmatched"}
    assigned = aliases = deferred = 0
    for oid, row in ledger.items():
        decision = str(row.get("decision", ""))
        if decision not in assigned_decisions | alias_decisions | unassigned_decisions and not decision.startswith("defer"):
            raise ValueError(f"lineage decision missing/unsupported for {oid}: {decision or 'missing'}")
        if decision in assigned_decisions:
            if not row.get("object_id"):
                raise ValueError(f"assigned observation has no object_id: {oid}")
            assigned += 1
        elif decision in alias_decisions:
            canonical = row.get("canonical_observation_id") or row.get("duplicate_of")
            if not canonical:
                diagnostics_path = replay / "frame_diagnostics.json"
                diagnostics = read_json(diagnostics_path) if diagnostics_path.is_file() else []
                for frame in diagnostics:
                    for item in frame.get("object_backend", {}).get("decisions", []):
                        if str(item.get("detection_id", "")) == oid:
                            canonical = item.get("canonical_observation_id") or item.get("duplicate_of")
                if not canonical:
                    raise ValueError(f"alias has no canonical observation reference: {oid}")
            aliases += 1
        else:
            if row.get("object_id") is not None:
                raise ValueError(f"deferred/unassigned lineage unexpectedly names an object_id: {oid}")
            deferred += 1
    return {"rows": len(ledger), "assigned": assigned, "aliases": aliases, "deferred_or_unassigned": deferred}


def compare_detection3d(authoritative_path: Path, candidate_path: Path, *, tolerance: float = 1e-6) -> dict:
    """Compare all Detection3D fields relevant to fixed-assignment fusion."""
    authoritative = p3_detection_map(authoritative_path)
    candidate = p3_detection_map(candidate_path)
    missing = sorted(authoritative.keys() - candidate.keys())
    extra = sorted(candidate.keys() - authoritative.keys())
    differences: list[dict[str, Any]] = []
    numeric_fields = ("timestamp_s", "confidence")
    vector_fields = ("center_W", "extents_m")
    exact_fields = ("center_method", "class_name", "class_id", "frame_id", "layer_id")
    for oid in sorted(authoritative.keys() & candidate.keys()):
        left, right = authoritative[oid], candidate[oid]
        for field in exact_fields:
            if left.get(field) != right.get(field):
                differences.append({"observation_id": oid, "field": field, "authoritative": left.get(field), "candidate": right.get(field)})
        for field in numeric_fields:
            if left.get(field) is None or right.get(field) is None or abs(float(left[field]) - float(right[field])) > tolerance:
                differences.append({"observation_id": oid, "field": field, "authoritative": left.get(field), "candidate": right.get(field)})
        for field in vector_fields:
            a, b = left.get(field), right.get(field)
            if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b) or any(abs(float(x)-float(y)) > tolerance for x, y in zip(a, b)):
                differences.append({"observation_id": oid, "field": field, "authoritative": a, "candidate": b})
        for field in ("frame_index", "cluster_points", "class_raw", "mask_file"):
            a = left.get("provider_metadata", {}).get(field)
            b = right.get("provider_metadata", {}).get(field)
            if a != b:
                differences.append({"observation_id": oid, "field": f"provider_metadata.{field}", "authoritative": a, "candidate": b})
        pose_a = left.get("provider_metadata", {}).get("camera_pose_W_Copt")
        pose_b = right.get("provider_metadata", {}).get("camera_pose_W_Copt")
        if pose_a is not None or pose_b is not None:
            if pose_a is None or pose_b is None or len(pose_a) != len(pose_b) or any(
                    abs(float(x)-float(y)) > tolerance for row_a, row_b in zip(pose_a, pose_b) for x, y in zip(row_a, row_b)):
                differences.append({"observation_id": oid, "field": "provider_metadata.camera_pose_W_Copt", "authoritative": pose_a, "candidate": pose_b})
    return {"status": "pass" if not (missing or extra or differences) else "diverged",
            "authoritative_count": len(authoritative), "candidate_count": len(candidate),
            "missing_ids": missing, "extra_ids": extra, "differences": differences[:20],
            "comparison_tolerance": tolerance}


def validate_sidecar(input_root: Path, p3_output: Path) -> dict:
    manifest_path = p3_output / "detection_3d_manifest.json"
    detection_path = p3_output / "detection_3d.jsonl"
    index_path = p3_output / "object_evidence/index.jsonl"
    for path in (manifest_path, detection_path, index_path):
        if not path.is_file():
            raise ValueError(f"selected-point sidecar missing required file: {path}")
    manifest = read_json(manifest_path)
    if manifest.get("status") != "pass":
        raise ValueError(f"sidecar P3 manifest status is {manifest.get('status')}")
    capture_path = input_root / "capture/capture_manifest.json"
    p2_path = input_root / "detection_2d/detection_2d_index.json"
    if manifest.get("capture_manifest_sha256") != sha256(capture_path):
        raise ValueError("sidecar capture hash differs from input run")
    if manifest.get("detection_2d_cache_sha256") != sha256(p2_path):
        raise ValueError("sidecar P2 hash differs from input run")
    sidecar_status = manifest.get("object_evidence_sidecar", {})
    if sidecar_status.get("status") != "pass" or sidecar_status.get("complete") is False:
        raise ValueError(f"selected-point sidecar is partial/unavailable: {sidecar_status}")
    detections = p3_detection_map(detection_path)
    evidence_rows = read_jsonl(index_path)
    evidence = {str(row.get("observation_id", "")): row for row in evidence_rows}
    if len(evidence) != len(evidence_rows) or set(evidence) != set(detections):
        raise ValueError(f"sidecar observation coverage mismatch: P3={len(detections)} evidence={len(evidence_rows)}")
    authoritative_manifest = read_json(input_root / "detection_3d/detection_3d_manifest.json")
    for field in ("profile_sha256", "effective_sampling_stride", "effective_cluster_min_points", "effective_cluster_eps_m"):
        if manifest.get(field) != authoritative_manifest.get(field):
            raise ValueError(f"P3 sidecar effective config mismatch for {field}: {manifest.get(field)} != {authoritative_manifest.get(field)}")
    if int(sidecar_status.get("records", -1)) != int(authoritative_manifest.get("detection_3d_count", -2)):
        raise ValueError("selected-point sidecar count differs from authoritative P3 accepted count")
    for oid, row in evidence.items():
        if int(row.get("point_count", -1)) != int(detections[oid].get("provider_metadata", {}).get("cluster_points", -2)):
            raise ValueError(f"selected point count differs from P3 Detection3D: {oid}")
        if row.get("capture_manifest_sha256") != sha256(capture_path) or row.get("detection_2d_index_sha256") != sha256(p2_path):
            raise ValueError(f"per-observation evidence hash mismatch: {oid}")
        point_file = p3_output / "object_evidence" / str(row.get("point_file", ""))
        if not point_file.is_file() or sha256(point_file) != row.get("point_file_sha256"):
            raise ValueError(f"selected point file missing/hash mismatch: {oid}")
    return {"manifest": manifest, "detection_path": detection_path, "evidence_count": len(evidence),
            "detection_count": len(detections), "evidence_ids": set(evidence)}


def inspect_input(run_root: Path) -> dict:
    run_manifest_path = run_root / "launcher_manifest.json"
    if not run_manifest_path.is_file():
        raise ValueError(f"input run lacks launcher manifest: {run_manifest_path}")
    run_manifest = read_json(run_manifest_path)
    if run_manifest.get("status") != "completed":
        raise ValueError(f"association input status must be completed, got {run_manifest.get('status')}")

    # A cached native replay intentionally stores only graph/replay outputs.
    # Its launcher manifest has an explicit source_cache_run pointer and input
    # hashes; bind capture/P2/P3 through that provenance instead of guessing
    # from IDs/endpoints or manufacturing a lineage ledger.
    cache_root = run_root
    has_local_capture = (run_root / "capture/capture_manifest.json").is_file()
    source_cache_run = run_manifest.get("source_cache_run")
    if not has_local_capture and source_cache_run:
        linked_root = Path(str(source_cache_run)).expanduser().resolve()
        allowed_root = run_root.parent.resolve()
        if not linked_root.is_relative_to(allowed_root) or linked_root == run_root.resolve():
            raise ValueError(f"source_cache_run is outside this workspace's run store: {linked_root}")
        cache_root = linked_root

    paths = {
        "capture": cache_root / "capture/capture_manifest.json",
        "p2": cache_root / "detection_2d/detection_2d_index.json",
        "p3": cache_root / "detection_3d/detection_3d.jsonl",
        "p3_manifest": cache_root / "detection_3d/detection_3d_manifest.json",
        "replay_manifest": run_root / "replay/manifest.json",
        "lineage": run_root / "replay/object_association_lineage.jsonl",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise ValueError(f"input lacks capture/P2/P3 cache or replay lineage artifacts: {missing}")
    cache_manifest_path = cache_root / "launcher_manifest.json"
    cache_manifest = read_json(cache_manifest_path) if cache_manifest_path.is_file() else run_manifest
    if cache_manifest.get("status") != "completed":
        raise ValueError(f"capture/P2/P3 cache run status must be completed, got {cache_manifest.get('status')}")
    capture = read_json(paths["capture"])
    p2 = read_json(paths["p2"])
    p3_manifest = read_json(paths["p3_manifest"])
    replay_manifest = read_json(paths["replay_manifest"])
    capture_sha = sha256(paths["capture"])
    p2_sha = sha256(paths["p2"])
    p3_sha = sha256(paths["p3"])
    p3_manifest_sha = sha256(paths["p3_manifest"])
    if cache_root != run_root:
        hashes = run_manifest.get("input_hashes", {})
        expected = {"capture_manifest": capture_sha, "p2_index": p2_sha,
                    "p3_cache": p3_sha, "p3_manifest": p3_manifest_sha}
        mismatches = {key: (hashes.get(key), value) for key, value in expected.items() if hashes.get(key) != value}
        if mismatches:
            raise ValueError(f"source replay provenance hashes do not match linked capture/P2/P3 cache: {mismatches}")
    if p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != capture_sha:
        raise ValueError("P2 cache status/hash does not match capture")
    if p3_manifest.get("status") != "pass" or p3_manifest.get("capture_manifest_sha256") != capture_sha:
        raise ValueError("P3 cache status/hash does not match capture")
    if int(p2.get("frame_count", -1)) != int(capture.get("frame_count", -2)) or int(p3_manifest.get("frame_count", -1)) != int(capture.get("frame_count", -2)):
        raise ValueError("capture/P2/P3 frame counts do not match")
    p3_ids = set(p3_detection_map(paths["p3"]))
    if len(p3_ids) != int(p3_manifest.get("detection_3d_count", -1)):
        raise ValueError("P3 Detection3D count differs from authoritative P3 manifest")
    if (replay_manifest.get("pipeline_status") != "pass"
            or replay_manifest.get("capture_manifest_sha256") != capture_sha
            or replay_manifest.get("detection_3d_cache_sha256") != p3_sha):
        raise ValueError("input replay is not bound to this capture/P3 cache")
    backend = run_manifest.get("association_profile", run_manifest.get("graph_backend"))
    replay_backend = replay_manifest.get("object_backend", replay_manifest.get("association_profile"))
    backend_compatible = (backend == replay_backend or (backend == "source" and replay_backend == "source-wrapper"))
    if not backend or not backend_compatible:
        raise ValueError(f"association backend provenance mismatch: run={backend}, replay={replay_backend}")
    lineage = validate_lineage(run_root / "replay", p3_ids)
    result = {"run_id": run_manifest.get("run_id", run_root.name), "scene_id": capture.get("scene_id"),
              "route_alias": run_manifest.get("route_alias", cache_manifest.get("route_alias", "unrecorded")),
              "resolution_px": capture.get("resolution_px"), "frames": int(capture.get("frame_count", 0)),
              "backend": backend, "capture_sha256": capture_sha,
              "p2_sha256": p2_sha, "p3_sha256": p3_sha,
              "p3_manifest_sha256": p3_manifest_sha, "accepted_p3": len(p3_ids),
              "lineage": lineage, "run_manifest": run_manifest, "cache_run_manifest": cache_manifest,
              "cache_root": cache_root, "replay_root": run_root / "replay",
              "p3_manifest": p3_manifest, "p3_ids": p3_ids}
    try:
        result["reused_sidecar"] = validate_sidecar(cache_root, cache_root / "detection_3d")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result["reused_sidecar"] = None
        result["sidecar_issue"] = str(exc)
    return result
