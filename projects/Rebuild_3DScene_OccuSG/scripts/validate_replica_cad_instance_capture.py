#!/usr/bin/env python3
"""Validate a captured ReplicaCAD scene-instance inventory and sensor images.

This does not feed scene-instance metadata, object IDs, or mesh geometry into
the mapping/room pipeline.  It only records provenance and writes RGB/depth
previews from the raw capture for human inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _preview(capture: Path, record: dict, output: Path) -> dict:
    frame = capture / str(record["frame_dir"])
    rgb = np.asarray(np.load(frame / "rgb.npy", allow_pickle=False))
    depth = np.asarray(np.load(frame / "depth_m.npy", allow_pickle=False), dtype=np.float32)
    valid = np.asarray(np.load(frame / "valid_mask.npy", allow_pickle=False), dtype=bool)
    if rgb.ndim != 3 or rgb.shape[2] not in (3, 4):
        raise ValueError(f"unexpected RGB shape: {rgb.shape}")
    if depth.shape != valid.shape or depth.shape != rgb.shape[:2]:
        raise ValueError("RGB/depth/valid-mask shapes do not agree")
    rgb_path = output / f"{record['frame_id']}_rgb.png"
    depth_path = output / f"{record['frame_id']}_depth.png"
    Image.fromarray(rgb[..., :3].astype(np.uint8), mode="RGB").save(rgb_path)
    finite = valid & np.isfinite(depth)
    preview = np.zeros(depth.shape, dtype=np.uint8)
    if np.any(finite):
        lo, hi = float(np.nanmin(depth[finite])), float(np.nanmax(depth[finite]))
        if hi > lo:
            preview[finite] = np.clip((depth[finite] - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)
        else:
            preview[finite] = 255
    Image.fromarray(preview, mode="L").save(depth_path)
    return {
        "frame_id": record["frame_id"],
        "rgb_shape": list(rgb.shape),
        "depth_shape": list(depth.shape),
        "valid_depth_pixels": int(np.count_nonzero(valid)),
        "total_depth_pixels": int(valid.size),
        "rgb_png": rgb_path.name,
        "depth_png": depth_path.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--scene-instance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture = args.capture.resolve()
    scene_instance = args.scene_instance.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    scene = json.loads(scene_instance.read_text(encoding="utf-8"))
    records = manifest.get("frames", [])
    if not records:
        raise ValueError("capture contains no frames")
    inventory = {
        "schema_version": 1,
        "scene_id": manifest.get("scene_id"),
        "scene_instance": str(scene_instance),
        "scene_instance_sha256": _sha256(scene_instance),
        "stage_template": scene.get("stage_instance", {}).get("template_name"),
        "declared_object_instances": len(scene.get("object_instances", [])),
        "declared_articulated_object_instances": len(scene.get("articulated_object_instances", [])),
        "object_template_count": len({x.get("template_name") for x in scene.get("object_instances", [])}),
        "object_templates": sorted({x.get("template_name") for x in scene.get("object_instances", [])}),
        "articulated_object_templates": [x.get("template_name") for x in scene.get("articulated_object_instances", [])],
        "runtime_note": "Scene instance declarations were loaded through Habitat-Sim dataset config; this build reports no Bullet physics, so articulated objects are declaration/provenance only, not dynamic-articulation validation.",
        "capture_manifest_sha256": _sha256(capture / "capture_manifest.json"),
        "frame_previews": [_preview(capture, records[0], output), _preview(capture, records[-1], output)],
        "semantic_gt_passed_to_builder": False,
        "object_ids_passed_to_builder": False,
    }
    (output / "scene_instance_validation.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
