"""Local validation for ConceptGraphs final-map exports."""
from __future__ import annotations
import hashlib, json
from pathlib import Path
import numpy as np
from .capture_package import sha256_file

SCHEMA = "conceptgraphs-object-export/v1"

def validate_object_export(root: Path) -> dict:
    root = root.resolve()
    manifest = json.loads((root / "export_manifest.json").read_text())
    if manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported ConceptGraphs object export schema")
    if manifest.get("identity_scope") != "final_map_only_no_persistent_lifecycle_claim":
        raise ValueError("export must explicitly declare final-map-only identity scope")
    if manifest.get("coordinate_frame") != "conceptgraphs_world_from_capture_contract":
        raise ValueError("unknown output coordinate frame")
    if manifest.get("units") != "m":
        raise ValueError("object export must declare units='m'")
    for key in ("scene_id", "sequence_id"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"object export must declare non-empty {key}")
    if manifest.get("source_run_id") is not None and (not isinstance(manifest["source_run_id"], str) or not manifest["source_run_id"].strip()):
        raise ValueError("source_run_id must be a non-empty string when provided")
    T=np.asarray(manifest.get("T_capture_world_from_cg_world"),dtype=np.float64)
    if T.shape!=(4,4) or not np.isfinite(T).all() or not np.allclose(T[3],[0,0,0,1],atol=1e-9):
        raise ValueError("invalid CG-world to capture-world transform")
    if not np.allclose(T[:3,:3].T@T[:3,:3],np.eye(3),atol=1e-7) or not np.isclose(np.linalg.det(T[:3,:3]),1,atol=1e-7):
        raise ValueError("CG-world transform rotation must be proper rigid rotation")
    native_rel=manifest.get("native_map_file")
    native_hash=manifest.get("native_map_sha256")
    if not native_rel or not native_hash:
        raise ValueError("native ConceptGraphs map artifact provenance is required")
    native=(root.parent/native_rel).resolve()
    if root.parent not in native.parents or not native.is_file() or sha256_file(native)!=native_hash:
        raise ValueError("native ConceptGraphs map artifact is missing or hash-mismatched")
    for rel, digest in manifest.get("files_sha256", {}).items():
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file() or sha256_file(p) != digest:
            raise ValueError(f"export path/hash invalid: {rel}")
    file_hashes = manifest.get("files_sha256", {})
    for obj in manifest.get("objects", []):
        rel = obj.get("cloud_npz")
        cloud_path = (root / rel).resolve() if isinstance(rel, str) else root
        if root not in cloud_path.parents or not cloud_path.is_file():
            raise ValueError(f"object cloud path is missing or escapes export: {rel}")
        if rel not in file_hashes:
            raise ValueError(f"object cloud is absent from files_sha256: {rel}")
        cloud = np.load(cloud_path, allow_pickle=False)
        xyz = cloud["xyz"]
        bounds = np.asarray(obj["bounds_minmax"], dtype=np.float64)
        if xyz.ndim != 2 or xyz.shape[1] != 3 or len(xyz) == 0 or not np.isfinite(xyz).all():
            raise ValueError(f"invalid object cloud: {obj.get('export_id')}")
        if bounds.shape != (2, 3) or np.any(bounds[1] < bounds[0]):
            raise ValueError(f"invalid object bounds: {obj.get('export_id')}")
        if obj.get("membership_status") not in ("available", "unavailable"):
            raise ValueError("membership status must be explicit")
    export_ids = [obj.get("export_id") for obj in manifest.get("objects", [])]
    if any(not isinstance(value, str) or not value.strip() for value in export_ids):
        raise ValueError("every object must have a non-empty export_id")
    if len(export_ids) != len(set(export_ids)):
        raise ValueError("object export_id values must be unique")
    return manifest
