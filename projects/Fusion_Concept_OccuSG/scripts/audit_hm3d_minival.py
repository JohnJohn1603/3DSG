#!/usr/bin/env python3
"""Inventory an already-complete HM3D minival v0.2 extraction.

This read-only audit does not load meshes or invoke Habitat-Sim. Run only after
hm3d_v02_download.py has exited successfully, never while extraction is active.
Semantic files are reported per scene; missing annotations are not treated as
an error or silently inferred from a mesh/navmesh.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import glob
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d"
DEFAULT_REPORT = ROOT / "artifacts/runs/hm3d_integration_20261005/dataset_inventory.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def nonempty_line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        return sum(1 for line in stream if line.strip())


def semantic_descriptor_inventory(path: Path) -> Dict[str, Any]:
    rows = []
    errors = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
        for line_number, row in enumerate(csv.reader(stream), 1):
            if not row or not row[0].strip().isdigit():
                continue
            if len(row) < 4:
                errors.append(line_number)
                continue
            rows.append({"instance_id": row[0].strip(), "color_hex": row[1].strip(),
                         "category": row[2].strip(), "region_id": row[3].strip()})
    classes: Dict[str, int] = {}
    by_region: Dict[str, List[str]] = {}
    for row in rows:
        classes[row["category"]] = classes.get(row["category"], 0) + 1
        by_region.setdefault(row["region_id"], []).append(row["instance_id"])
    return {"record_count": len(rows), "unique_instance_id_count": len(set(row["instance_id"] for row in rows)),
            "unique_region_id_count": len(by_region), "class_counts": dict(sorted(classes.items())),
            "region_object_ids": {key: value for key, value in sorted(by_region.items())},
            "instance_records": rows, "malformed_data_line_numbers": errors,
            "room_polygon_or_boundary_ground_truth_in_descriptor": False,
            "region_id_semantics": "object grouping identifier only; no room category or polygon inferred"}


def _json_strings(value: Any) -> List[str]:
    result: List[str] = []
    if isinstance(value, dict):
        for item in value.values():
            result.extend(_json_strings(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_json_strings(item))
    elif isinstance(value, str):
        result.append(value)
    return result


def _check_reference(raw: str, config_path: Path, root: Path) -> Dict[str, Any]:
    if ("://" in raw or "%%" in raw
            or not raw.lower().endswith((".glb", ".gltf", ".navmesh", ".json", ".txt"))):
        return {"value": raw, "status": "placeholder_or_external"}
    candidate = Path(raw)
    first_part = candidate.parts[0] if candidate.parts else ""
    if config_path.parent.name == "minival":
        in_scope = first_part not in {"example", "train", "val", "test"}
    else:
        in_scope = first_part == "minival"
    if not in_scope and first_part in {"example", "train", "val", "test"}:
        return {"value": raw, "status": "outside_selected_split"}
    probes = [candidate] if candidate.is_absolute() else [config_path.parent / candidate, root / candidate]
    matches: List[Path] = []
    for path in probes:
        if glob.has_magic(str(path)):
            matches.extend(Path(item).resolve() for item in glob.glob(str(path), recursive=True))
        elif path.exists():
            matches.append(path.resolve())
    unique = sorted(set(matches))
    if unique:
        return {"value": raw, "status": "present", "matches": [str(path) for path in unique]}
    return {"value": raw, "status": "missing_in_minival" if in_scope else "not_checked_out_of_scope"}


def inventory(data_root: Path) -> Dict[str, Any]:
    root = data_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError("dataset root is not a directory; wait for downloader/extraction to finish")
    files = [path for path in root.rglob("*") if path.is_file()]
    configs = sorted(path for path in files if path.name.endswith("scene_dataset_config.json"))
    basis_meshes = sorted(path for path in files if path.name.endswith(".basis.glb"))
    scenes: List[Dict[str, Any]] = []
    for mesh in basis_meshes:
        stem = mesh.name[:-len(".basis.glb")]
        navmesh = mesh.with_name(stem + ".basis.navmesh")
        semantic_mesh = mesh.with_name(stem + ".semantic.glb")
        descriptor = mesh.with_name(stem + ".semantic.txt")
        semantic_mesh_exists, descriptor_exists = semantic_mesh.is_file(), descriptor.is_file()
        if semantic_mesh_exists and descriptor_exists:
            annotation_status = "semantic_mesh_and_descriptor_present"
        elif semantic_mesh_exists or descriptor_exists:
            annotation_status = "partial_semantic_annotation_pair"
        else:
            annotation_status = "semantic_annotation_not_found"
        scenes.append({
            "scene_id_candidate": mesh.parent.name,
            "scene_file": str(mesh.relative_to(root)),
            "scene_file_bytes": mesh.stat().st_size,
            "navmesh": {"path": str(navmesh.relative_to(root)), "present": navmesh.is_file()},
            "semantic_mesh": {"path": str(semantic_mesh.relative_to(root)), "present": semantic_mesh_exists},
            "semantic_descriptor": {"path": str(descriptor.relative_to(root)), "present": descriptor_exists,
                                    "bytes": descriptor.stat().st_size if descriptor_exists else None,
                                    "sha256": sha256(descriptor) if descriptor_exists else None,
                                    "nonempty_lines": nonempty_line_count(descriptor) if descriptor_exists else None},
            "semantic_descriptor_inventory": semantic_descriptor_inventory(descriptor) if descriptor_exists else None,
            "semantic_annotation_status": annotation_status,
        })

    config_reports = []
    unresolved: Set[str] = set()
    outside_scope = 0
    for path in configs:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            config_reports.append({"path": str(path.relative_to(root)), "parse_error": type(exc).__name__})
            continue
        references = []
        for raw in sorted(set(_json_strings(document))):
            reference = _check_reference(raw, path, root)
            references.append(reference)
            if reference["status"] == "missing_in_minival":
                unresolved.add(str(path.relative_to(root)) + " -> " + raw)
            if reference["status"] in ("outside_selected_split", "not_checked_out_of_scope"):
                outside_scope += 1
        config_reports.append({"path": str(path.relative_to(root)), "sha256": sha256(path),
                              "bytes": path.stat().st_size, "referenced_asset_paths": references})
    counts: Dict[str, int] = {}
    for scene in scenes:
        status = scene["semantic_annotation_status"]
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": 1,
        "kind": "hm3d_minival_v0.2_dataset_inventory",
        "status": "inventory_complete",
        "dataset_root": str(root),
        "scene_dataset_configs": config_reports,
        "scene_count_basis_glb": len(scenes),
        "scenes": scenes,
        "semantic_annotation_presence_counts": counts,
        "unresolved_config_path_references": sorted(unresolved),
        "out_of_scope_config_references_not_checked": outside_scope,
        "limitations": [
            "Presence of a semantic mesh/descriptor does not establish completeness or room-polygon ground truth.",
            "The semantic descriptor maps instance IDs to object categories and region IDs; region IDs are not room polygons/categories.",
            "Navmesh is navigability support only, not room GT or predictor input.",
            "No annotation completeness is inferred for scenes where files are absent.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    receipt_path = ROOT / "artifacts/runs/hm3d_integration_20261005/download_receipt.json"
    if not receipt_path.is_file():
        raise RuntimeError("successful downloader receipt is absent; do not inventory during download/extraction")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("status") != "downloaded" or receipt.get("dataset_version") != "HM3D v0.2":
        raise RuntimeError("download receipt does not confirm completed HM3D v0.2 extraction")
    if args.output.exists():
        raise FileExistsError("refusing to overwrite an existing inventory report")
    report = inventory(args.dataset_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "scene_count": report["scene_count_basis_glb"],
                      "annotation_presence": report["semantic_annotation_presence_counts"],
                      "report": str(args.output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
