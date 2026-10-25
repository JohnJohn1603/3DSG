#!/usr/bin/env python3
"""Build a derived T3 viewer for the locked 13-frame T1.1 mapper run.

Only display assets are derived here. Mapper snapshots, clouds, room revisions,
packets, and T3 semantic outputs remain read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_causal_timeline_viewer import build as build_timeline_viewer
from scripts.build_causal_timeline_viewer import rel_url, sha
from scripts.run_t1_1_t3_room_belief import validate_mapper_run


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _panel_name(observation_id: str) -> str:
    return hashlib.sha256(observation_id.encode("utf-8")).hexdigest()[:24]


def _write_panels(mapper_run: Path, rows: list[dict[str, Any]], output: Path) -> int:
    """Render source RGB/mask/crop panels for inspection; no predictor use."""
    panel_root = output / "diagnostic/panels"
    panel_root.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, dict[str, Any]] = {}
    for row in rows:
        packet = json.loads(Path(row["packet_path"]).read_text(encoding="utf-8"))
        raw = packet["raw_keyframe_source"]
        raw_dir = Path(raw["source_path"])
        raw_hashes = raw["files_sha256"]
        rgb_path = raw_dir / "rgb.npy"
        if sha(rgb_path) != raw_hashes.get("rgb.npy"):
            raise ValueError(f"RGB source hash mismatch at frame {packet['frame_index']}")
        rgb = np.load(rgb_path, allow_pickle=False)
        if rgb.ndim != 3 or rgb.shape[:2] != (480, 640) or rgb.shape[2] < 3:
            raise ValueError(f"RGB shape is invalid for evidence display at frame {packet['frame_index']}: {rgb.shape}")
        rgb = np.asarray(rgb[..., :3], dtype=np.uint8)
        mask_root = Path(packet["asset_root"]) / "detection_2d"
        packet_hash = sha(Path(row["packet_path"]))
        for det in packet.get("p2_detections", []):
            oid = str(det["detection_id"])
            mask_path = mask_root / str(det["mask_file"])
            if not mask_path.is_file() or sha(mask_path) != det.get("mask_sha256"):
                raise ValueError(f"P2 mask source hash mismatch for {oid}")
            mask = np.asarray(np.load(mask_path, allow_pickle=False)).squeeze()
            if mask.shape != (480, 640):
                raise ValueError(f"P2 mask shape mismatch for {oid}: {mask.shape}")
            mask = mask.astype(bool, copy=False)
            stem = _panel_name(oid)
            paths = {
                "rgb": panel_root / f"{stem}-rgb.jpg",
                "mask": panel_root / f"{stem}-mask.png",
                "overlay": panel_root / f"{stem}-overlay.jpg",
                "crop": panel_root / f"{stem}-crop.jpg",
            }
            Image.fromarray(rgb, mode="RGB").save(paths["rgb"], quality=92, optimize=True)
            Image.fromarray(mask.astype(np.uint8) * 255, mode="L").save(paths["mask"], optimize=True)
            overlay = rgb.astype(np.float32)
            tint = np.array([30.0, 210.0, 140.0], dtype=np.float32)
            overlay[mask] = overlay[mask] * 0.50 + tint * 0.50
            Image.fromarray(overlay.clip(0, 255).astype(np.uint8), mode="RGB").save(paths["overlay"], quality=92, optimize=True)
            bbox = np.asarray(det.get("bbox_xyxy", []), dtype=np.float64)
            if bbox.shape != (4,) or not np.isfinite(bbox).all():
                raise ValueError(f"P2 bbox is malformed for {oid}")
            x0, y0, x1, y1 = bbox
            left, top = max(0, int(np.floor(x0))), max(0, int(np.floor(y0)))
            right, bottom = min(640, int(np.ceil(x1))), min(480, int(np.ceil(y1)))
            if right <= left or bottom <= top:
                raise ValueError(f"P2 bbox has no displayable crop for {oid}")
            Image.fromarray(overlay.clip(0, 255).astype(np.uint8), mode="RGB").crop((left, top, right, bottom)).save(paths["crop"], quality=92, optimize=True)
            evidence[oid] = {
                "frame_id": packet["frame_id"], "frame_index": int(packet["frame_index"]),
                "timestamp_s": float(packet["timestamp_s"]), "raw_class": det["class_raw"],
                "class_canonical": det.get("class_canonical"), "bbox_xyxy": det["bbox_xyxy"],
                "source_packet_sha256": packet_hash,
                "source_hashes": {"rgb.npy": raw_hashes["rgb.npy"], "mask": det["mask_sha256"]},
                "panels": {name: f"panels/{path.name}" for name, path in paths.items()},
            }
    _dump(output / "diagnostic/evidence_manifest.json", {
        "schema": "t1-1-t3-viewer-evidence/v1", "status": "complete",
        "source_mapper_run": str(mapper_run), "observations": evidence,
        "display_only": True, "predictor_or_membership_input": False,
    })
    return len(evidence)


def _prepare_view_source(validated: dict[str, Any], root: Path) -> tuple[Path, int]:
    source = root / "viewer_source"
    source.mkdir(parents=True, exist_ok=False)
    (source / "snapshots").mkdir()
    manifest = validated["manifest"]
    evidence_count = _write_panels(validated["mapper_run"], validated["source_packet_rows"], source)
    frames = []
    for row in validated["source_packet_rows"]:
        snap = json.loads(Path(row["object_snapshot_path"]).read_text(encoding="utf-8"))
        room_path = Path(row["room_snapshot_path"]).resolve()
        # These are display shims. Source prefix snapshots remain unchanged.
        for obj in snap.get("objects", []):
            obj["cloud_npz"] = str((validated["mapper_run"] / "branches/objects" / obj["cloud_npz"]).resolve())
        snap["rooms"] = {"state_ref": str(room_path)}
        rel_snapshot = Path("snapshots") / f"frame-{int(row['frame_index']):06d}.json"
        _dump(source / rel_snapshot, snap)
        frames.append({
            "frame_index": int(row["frame_index"]), "frame_id": row["frame_id"],
            "revision": int(row["object_revision"]), "object_source_revision": int(row["object_revision"]),
            "room_source_revision": int(row["room_revision"]), "timestamp_s": float(row["timestamp_s"]),
            "snapshot": rel_snapshot.as_posix(),
        })
    _dump(source / "frame_index.json", {"schema": "t1-1-viewer-source-frame-index/v1", "frames": frames})
    _dump(source / "manifest.json", {
        "schema": "t1-1-viewer-source-timeline/v1", "scene_id": manifest["scene_id"],
        "sequence_id": manifest["sequence_id"], "run_id": manifest["run_id"],
        "snapshot_index": "frame_index.json", "mapping_output": str(source),
        "cache_root": str(validated["raw_capture_run_root"]),
        "source_mapper_manifest_sha256": validated["manifest_sha256"],
        "source_kind": "display_adapter_only_no_source_state_mutation",
    })
    return source, evidence_count


def build(mapper_run: Path, a_zip: Path, verified_run: Path, output: Path,
          assumed_run: Path | None = None) -> dict[str, Any]:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"viewer output path already exists: {output}")
    output.mkdir(parents=True, exist_ok=False)
    validated = validate_mapper_run(mapper_run, a_zip)
    verified_run = verified_run.resolve()
    semantic_manifest = json.loads((verified_run / "manifest.json").read_text(encoding="utf-8"))
    if semantic_manifest.get("scope_status") != "unavailable" or semantic_manifest.get("scope_declaration_id") is not None:
        raise ValueError("viewer input must be the verified-only run with no assumed declaration")
    if semantic_manifest.get("input_provenance", {}).get("source_mapper_run_manifest_sha256") != validated["manifest_sha256"]:
        raise ValueError("verified-only semantic output does not reference this exact mapper run")
    assumed_manifest = None
    if assumed_run is not None:
        assumed_run = assumed_run.resolve()
        assumed_manifest = json.loads((assumed_run / "manifest.json").read_text(encoding="utf-8"))
        if assumed_manifest.get("scope_status") != "assumed" or not assumed_manifest.get("scope_declaration_id"):
            raise ValueError("assumed viewer input must carry an approved experimental scope declaration")
        if assumed_manifest.get("input_provenance", {}).get("source_mapper_run_manifest_sha256") != validated["manifest_sha256"]:
            raise ValueError("assumed semantic output does not reference this exact mapper run")
        if assumed_manifest.get("source_a_zip_sha256") != sha(a_zip):
            raise ValueError("assumed semantic output uses a different A archive")
        if assumed_manifest.get("frame_count") != 13 or semantic_manifest.get("frame_count") != 13:
            raise ValueError("T1.1 A/B viewer requires both semantic timelines to contain the locked 13 prefixes")
        a_frames = semantic_manifest.get("revisions", [])
        b_frames = assumed_manifest.get("revisions", [])
        if [(x.get("frame_index"), x.get("frame_id")) for x in a_frames] != [(x.get("frame_index"), x.get("frame_id")) for x in b_frames]:
            raise ValueError("verified and assumed semantic revisions are not aligned frame-for-frame")
    source, evidence_count = _prepare_view_source(validated, output)
    viewer = output / "viewer"
    build_timeline_viewer(source, viewer, max_points=300, room_belief_verified=verified_run,
                          room_belief_assumed=assumed_run)

    index_path = viewer / "timeline_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if len(index["frames"]) != 13:
        raise ValueError("derived viewer frame count differs from locked T1.1 source")
    for frame, row in zip(index["frames"], validated["source_packet_rows"]):
        if (frame["frame_id"], int(frame["frame_index"]), int(frame["object_source_revision"]), int(frame["room_source_revision"])) != (
            row["frame_id"], int(row["frame_index"]), int(row["object_revision"]), int(row["room_revision"])):
            raise ValueError(f"viewer source frame/object/room revision alignment failed at {row['frame_id']}")
        joined = Path(row["joined_state_path"]).resolve()
        if sha(joined) != row["joined_state_sha256"]:
            raise ValueError(f"source unified graph state changed after preflight: {joined}")
        joined_doc = json.loads(joined.read_text(encoding="utf-8"))
        match = re.fullmatch(r"unified-(\d+)", str(joined_doc.get("graph_revision_id", "")))
        if not match or (joined_doc.get("frame_id"), int(joined_doc.get("frame_index", -1))) != (row["frame_id"], int(row["frame_index"])):
            raise ValueError(f"source unified revision identity is malformed at {row['frame_id']}")
        # The public revision slider displays the unified revision ordinal.
        # Object and room source revisions remain separate fields.
        frame["revision"] = int(match.group(1))
        frame["base_graph_url"] = rel_url(joined, viewer)
        frame["base_graph_sha256"] = row["joined_state_sha256"]
        frame["source_unified_revision"] = int(match.group(1))
    _dump(index_path, index)

    viewer_manifest_path = viewer / "viewer_manifest.json"
    viewer_manifest = json.loads(viewer_manifest_path.read_text(encoding="utf-8"))
    viewer_manifest.update({
        "server_root_required": str((Path(__file__).resolve().parents[1] / "artifacts").resolve()),
        "entrypoint": "index.html", "source_mapper_run_id": validated["manifest"]["run_id"],
        "source_mapper_run_manifest_sha256": validated["manifest_sha256"],
        "verified_only_manifest_sha256": sha(verified_run / "manifest.json"),
        "assumed_manifest_sha256": sha(assumed_run / "manifest.json") if assumed_run else None,
        "assumed_declaration_id": assumed_manifest.get("scope_declaration_id") if assumed_manifest else None,
        "base_graph_source": "original immutable joined prefix states; source unified revision kept separate",
        "gui_acceptance": "pending_browser_access",
    })
    _dump(viewer_manifest_path, viewer_manifest)

    file_hashes = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "viewer_build_receipt.json":
            file_hashes[path.relative_to(output).as_posix()] = sha(path)
    receipt = {
        "schema": "t1-1-t3-viewer-build-receipt/v1", "status": "complete",
        "source_mapper_run_id": validated["manifest"]["run_id"],
        "source_mapper_manifest_sha256": validated["manifest_sha256"],
        "source_a_archive_sha256": validated["a_zip_sha256"],
        "verified_only_manifest_sha256": sha(verified_run / "manifest.json"),
        "viewer_entrypoint": "viewer/index.html", "displayed_frames": 13,
        "evidence_observation_panels": evidence_count,
        "scope_modes": (["verified_only", "assumed_single_level_demo"] if assumed_run else ["verified_only"]),
        "assumed_scope_applied": bool(assumed_run),
        "output_sha256": file_hashes,
    }
    _dump(output / "viewer_build_receipt.json", receipt)
    semantic_root = output.parent
    viewer_receipt_sha = sha(output / "viewer_build_receipt.json")
    for name in ("run_summary.json", "receipt.json"):
        summary_path = semantic_root / name
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["viewer_status"] = "built_validated_gui_pending"
            summary["viewer_entrypoint"] = f"{output.name}/viewer/index.html"
            summary["viewer_build_receipt_sha256"] = viewer_receipt_sha
            _dump(summary_path, summary)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapper-run", type=Path, required=True)
    parser.add_argument("--a-zip", type=Path, required=True)
    parser.add_argument("--verified-run", type=Path, required=True)
    parser.add_argument("--assumed-run", type=Path, default=None,
                        help="optional approved experimental assumed-scope semantic run")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.mapper_run, args.a_zip, args.verified_run, args.output,
                           args.assumed_run), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
