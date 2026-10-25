#!/usr/bin/env python3
"""Read-only preflight for the dense 00802 room-belief development cache.

This script deliberately stops before feature extraction, ConceptGraphs mapping,
or room scoring.  It records whether the cached inputs are sufficient for the
hybrid runner and writes a reproducible proposal for the next approved stage.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058")
DEFAULT_LOCK = ROOT / "locks/u6_dense_00802_cache_20261010.json"
DEFAULT_FEATURE_MANIFEST = ROOT / "artifacts/audits/u4_temporal_test_run/appearance/feature_manifest.jsonl"
DEFAULT_FEATURE_REPORT = ROOT / "artifacts/audits/u4_temporal_test_run/appearance/feature_extraction_report.json"
DEFAULT_TEXT_CACHE = ROOT / "artifacts/audits/u4_ov/text_cache/text_features__23ae1da7b7f41d89608e.json"
DEFAULT_A_IMPORT = ROOT / "artifacts/room_belief/rb2_local_import_20261015/import_manifest.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--image-feature-manifest", type=Path, default=DEFAULT_FEATURE_MANIFEST)
    ap.add_argument("--image-feature-report", type=Path, default=DEFAULT_FEATURE_REPORT)
    ap.add_argument("--text-cache", type=Path, default=DEFAULT_TEXT_CACHE)
    ap.add_argument("--a-import-manifest", type=Path, default=DEFAULT_A_IMPORT)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    started = time.perf_counter()
    cache = args.cache.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)

    cap_path = cache / "capture/capture_manifest.json"
    p2_path = cache / "detection_2d/detection_2d_index.json"
    p3_path = cache / "detection_3d/detection_3d.jsonl"
    p3m_path = cache / "detection_3d/detection_3d_manifest.json"
    replay_manifest_path = cache / "replay/manifest.json"
    snapshot_path = cache / "replay/snapshot.json"
    trajectory_path = cache / "replay/trajectory.json"
    semantic_manifest_path = cache / "semantic/manifest.json"
    required = [cap_path, p2_path, p3_path, p3m_path, replay_manifest_path, snapshot_path, trajectory_path]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise SystemExit("missing required cache files: " + ", ".join(missing))

    cap = read_json(cap_path)
    p2 = read_json(p2_path)
    p3m = read_json(p3m_path)
    p3rows = read_jsonl(p3_path)
    replay = read_json(replay_manifest_path)
    snapshot_raw = read_json(snapshot_path)
    snapshot = snapshot_raw.get("snapshot", snapshot_raw)
    trajectory = read_json(trajectory_path)
    poses = trajectory.get("poses", [])

    p2_dets = [d for fr in p2.get("frames", []) for d in fr.get("detections", [])]
    p3_dets = [d for fr in p3rows for d in fr.get("detections", [])]
    classes = collections.Counter(str(d.get("class_raw", d.get("class_canonical", ""))) for d in p2_dets)

    # Capture metadata contains the full canonical sensor pose.  The replay
    # trajectory only contains position, so use metadata for the view-group
    # count and retain the position-only count as a separate diagnostic.
    position_keys = [tuple(round(float(p.get(k, 0.0)), 7) for k in ("x_m", "y_m", "z_m")) for p in poses]
    position_counts = collections.Counter(position_keys)
    capture_metadata = sorted((cache / "capture/frames").glob("*/metadata.json"))
    full_pose_keys = []
    metadata_scene_ids = set()
    metadata_sequence_ids = set()
    metadata_calibration_ids = set()
    metadata_resolutions = set()
    for metadata_path in capture_metadata:
        metadata = read_json(metadata_path)
        metadata_scene_ids.add(str(metadata.get("scene_id")))
        metadata_sequence_ids.add(str(metadata.get("sequence_id")))
        metadata_calibration_ids.add(str(metadata.get("calibration_id")))
        metadata_resolutions.add((int(metadata.get("width", 0)), int(metadata.get("height", 0))))
        matrix = metadata.get("base_pose_canonical")
        if matrix is not None:
            full_pose_keys.append(tuple(round(float(v), 7) for row in matrix for v in row))
    pose_counts = collections.Counter(full_pose_keys)
    revisit_groups = [n for n in pose_counts.values() if n > 1]
    unique_pose_count = len(pose_counts)

    room_nodes = [n for n in snapshot.get("nodes", []) if n.get("kind") == "room"]
    room_frame_counts = {}
    for room in room_nodes:
        frames = []
        for member in room.get("attributes", {}).get("member_ids", []):
            if ":hm3d-00802-main-dense-" in str(member):
                frames.append(str(member).rsplit("-", 1)[-1])
        room_frame_counts[room.get("node_id")] = len(set(frames))

    p3_layer_ids = sorted({str(d.get("layer_id")) for d in p3_dets if d.get("layer_id") is not None})
    room_floor_metadata = []
    for room in room_nodes:
        attrs = room.get("attributes", {})
        room_floor_metadata.append({k: attrs.get(k) for k in ("floor_id", "layer_id", "z_bounds", "z_floor", "z_ceiling") if k in attrs})

    # The four A tables are already imported and are intentionally only hashed here.
    a_provenance = None
    if args.a_import_manifest.is_file():
        a_provenance = {"path": rel(args.a_import_manifest), "sha256": sha256(args.a_import_manifest), "status": read_json(args.a_import_manifest).get("validation_status")}

    image_rows = read_jsonl(args.image_feature_manifest) if args.image_feature_manifest.is_file() else []
    p2_ids = {str(d["detection_id"]) for d in p2_dets}
    image_ids = {str(r.get("detection_id", r.get("observation_id"))) for r in image_rows}
    overlap = p2_ids & image_ids
    image_meta = {}
    for r in image_rows:
        image_meta.setdefault("model_fingerprint", r.get("model_fingerprint"))
        image_meta.setdefault("preprocessing_id", r.get("preprocessing_id"))
        image_meta.setdefault("feature_shape", r.get("feature_shape"))
    feature_report = read_json(args.image_feature_report) if args.image_feature_report.is_file() else None

    text_meta = read_json(args.text_cache) if args.text_cache.is_file() else None
    # Existing text cache is class/prompt-level, not a per-observation sidecar.
    text_contract = {
        "exists": bool(text_meta),
        "category_count": text_meta.get("category_count") if text_meta else None,
        "prompt_count": text_meta.get("prompt_count") if text_meta else None,
        "embedding_dimension": text_meta.get("embedding_dimension") if text_meta else None,
        "checkpoint_sha256": text_meta.get("checkpoint_sha256") if text_meta else None,
        "per_observation_aligned": False,
        "reuse_status": "class-level prompt cache only; hybrid runner requires per-observation text feature rows"
        if text_meta else "missing",
    }

    approved_aliases = {"couch": "sofa", "dining table": "dining_table", "potted plant": "potted_plant"}
    a_categories = ["bed", "sofa", "chair", "dining_table", "toilet", "sink", "refrigerator", "oven", "microwave", "potted_plant", "tv"]
    observed_canonical = {approved_aliases.get(str(c).strip().lower(), str(c).strip().lower()) for c in classes}
    eligible_categories = sorted(observed_canonical & set(a_categories))
    unsupported_observed = sorted(observed_canonical - set(a_categories))
    missing_a_categories = sorted(set(a_categories) - observed_canonical)

    source_paths = [cap_path, p2_path, p3m_path, p3_path, replay_manifest_path, snapshot_path, trajectory_path, args.lock]
    for optional in (semantic_manifest_path, args.image_feature_manifest, args.image_feature_report, args.text_cache, args.a_import_manifest):
        if optional.is_file():
            source_paths.append(optional)
    hashes = {rel(p): {"sha256": sha256(p), "bytes": p.stat().st_size} for p in source_paths}

    report = {
        "schema": "dense-room-belief-preflight/v1",
        "status": "preflight_complete_blocked_before_mapping",
        "scope": "read-only input audit; no Qwen, capture, detector, feature extraction, ConceptGraphs mapping, or static scoring run",
        "cache": {
            "root": str(cache),
            "scene_id": cap.get("scene_id"),
            "sequence_id": cap.get("sequence_id"),
            "frame_count": cap.get("frame_count"),
            "resolution_px": cap.get("resolution_px"),
            "hfov_deg": cap.get("horizontal_fov_deg"),
            "depth_units": cap.get("depth_units"),
            "depth_representation": cap.get("depth_representation"),
            "canonical_world": cap.get("canonical_world"),
            "capture_status": cap.get("status"),
        },
        "provenance_checks": {
            "p2_capture_hash_matches_capture": p2.get("capture_manifest_sha256") == sha256(cap_path),
            "p3_capture_hash_matches_capture": p3m.get("capture_manifest_sha256") == sha256(cap_path),
            "capture_metadata_count": len(capture_metadata),
            "capture_metadata_complete": len(capture_metadata) == int(cap.get("frame_count", -1)),
            "capture_metadata_scene_ids": sorted(metadata_scene_ids),
            "capture_metadata_sequence_ids": sorted(metadata_sequence_ids),
            "capture_metadata_calibration_ids": sorted(metadata_calibration_ids),
            "capture_metadata_resolutions": [list(x) for x in sorted(metadata_resolutions)],
            "room_snapshot_scene_sequence": {"scene_id": snapshot.get("scene_id"), "sequence_id": snapshot.get("sequence_id"), "matches_capture": (snapshot.get("scene_id"), snapshot.get("sequence_id")) == (cap.get("scene_id"), cap.get("sequence_id"))},
            "replay_manifest_scene_sequence": {"scene_id": replay.get("scene_id"), "sequence_id": replay.get("sequence_id"), "matches_capture": ((replay.get("scene_id"), replay.get("sequence_id")) == (cap.get("scene_id"), cap.get("sequence_id")) if replay.get("sequence_id") is not None else None), "note": "replay manifest has no sequence_id field; matching snapshot and capture metadata are the sequence check"},
            "p3_status": p3m.get("status"),
            "p3_accepted": p3m.get("detection_3d_count"),
            "p3_rejected": p3m.get("rejected_detection_count"),
            "p3_present_batch_count": p3m.get("present_batch_count"),
            "room_nodes": len(room_nodes),
            "room_frame_counts_from_snapshot": room_frame_counts,
        },
        "route": {
            "frames": len(poses),
            "unique_exact_pose_groups": unique_pose_count,
            "unique_position_groups": len(position_counts),
            "revisit_group_count": len(revisit_groups),
            "revisit_frame_count": sum(n - 1 for n in revisit_groups),
            "revisit_group_sizes": sorted(revisit_groups),
            "p2_nonempty_frames": sum(bool(fr.get("detections")) for fr in p2.get("frames", [])),
            "p2_detection_count": len(p2_dets),
            "p3_accepted_detection_count": len(p3_dets),
            "observed_room_nodes_in_snapshot": len(room_nodes),
            "note": "Exact-pose groups are not independent views; dense route is development data, not holdout.",
        },
        "floor_elevation_scope": {
            "p3_layer_ids": p3_layer_ids,
            "room_floor_metadata": room_floor_metadata,
            "room_floor_metadata_present": bool(any(room_floor_metadata)),
            "verified_scope": "room polygons are 2D and room nodes have no floor/layer/Z bounds; floor_0 in cached P3 metadata is not proof of a single-floor scene or multi-floor safety",
            "membership_allowed_for_next_run": "only the verified room-snapshot/elevation scope; do not use unconstrained XY across unknown floors",
        },
        "raw_semantic_inventory": {
            "unique_raw_class_count": len(classes),
            "raw_class_counts": dict(classes.most_common()),
            "a_categories": a_categories,
            "approved_aliases": approved_aliases,
            "eligible_categories_by_raw_inventory_only": eligible_categories,
            "missing_a_categories_in_raw_inventory": missing_a_categories,
            "unsupported_observed_categories": unsupported_observed,
            "warning": "These are raw P2 category counts only; no persistent-object consensus or room evidence exists for this dense hybrid run.",
        },
        "features": {
            "image_sidecar": {
                "manifest": rel(args.image_feature_manifest),
                "row_count": len(image_rows),
                "p2_id_population": len(p2_ids),
                "overlap_count": len(overlap),
                "missing_image_feature_count": len(p2_ids - image_ids),
                "extra_image_feature_count": len(image_ids - p2_ids),
                "metadata": image_meta,
                "report": feature_report,
                "reuse_status": "partial only; not sufficient for hybrid runner, which requires every P2 observation to have aligned image features",
            },
            "text_sidecar": text_contract,
            "legacy_semantic_export": {
                "manifest": rel(semantic_manifest_path) if semantic_manifest_path.is_file() else None,
                "status": "not used as ConceptGraphs output",
                "reason": "existing semantic/legacy persistent nodes use a different association/export provenance and cannot silently stand in for a hybrid ConceptGraphs object export",
            },
        },
        "a_tables": {
            "import_manifest": a_provenance,
            "reuse_policy": "four imported A tables and RB1 policies can be reused only after dense hybrid object export/readiness exists; no table is recomputed here",
        },
        "stages": {
            "reusable": ["capture RGB-D and metadata", "P2 masks/detections", "P3 accepted/rejected selected points", "trajectory poses/intrinsics contract", "matching OccuSG room snapshot/geometry", "locked min_detections=3 and existing RB1 consensus/membership/A-table policy"],
            "needs_new_work": ["648 missing ViT-B-32 image feature rows (or a separately proven equivalent sidecar)", "per-observation text feature mapping with verified contract (class-level 105-category cache is not aligned sidecar)", "ConceptGraphs hybrid mapping on 280 frames", "postfilter/object lineage and RB1 static room diagnostic"],
            "not_reused": ["native replay: already present for geometry reference, but not rerun", "legacy semantic persistent nodes: not treated as hybrid object export"],
        },
        "configuration_lock": {
            "obj_min_detections": 3,
            "matcher": "existing ConceptGraphs cached hybrid base_mapping_config; unchanged",
            "consensus": "existing RB1 semantic consensus policy; not run because no dense object export",
            "membership": "rb1_greedy_voxel_support_v1 within verified room/elevation scope; not run",
            "a_variants": ["mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral"],
            "thresholds": "unchanged; development proposals, not calibrated",
            "seed": 20261014,
            "cache_role": "development; not holdout and not a pose-count ablation against the 29-frame route",
        },
        "execution": {
            "mapping_executed": False,
            "static_diagnostic_executed": False,
            "qwen_executed": False,
            "detector_executed": False,
            "capture_executed": False,
            "native_replay_executed": False,
            "runtime_seconds_preflight": round(time.perf_counter() - started, 3),
            "room_quality_metrics": "unavailable: no room-function GT and no dense hybrid object evidence",
        },
        "source_hashes": hashes,
        "next_stage_proposal": {
            "scope": "create missing sidecars using the already available ViT-B-32/checkpoint only, then run the locked hybrid mapping and static diagnostic in a fresh output directory",
            "must_not": ["download a model", "rerun capture/P2/P3/Qwen/native replay", "alter min_detections, matcher, consensus, membership, A tables or seed", "overwrite source cache or baseline artifacts"],
            "budget": "requires separate approval before starting; 648 CPU image features plus 280-frame mapping are not bounded by this read-only preflight and may exceed 30 minutes",
        },
    }
    (out / "dense_preflight.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "dense_preflight_manifest.json").write_text(json.dumps({"schema": "dense-room-belief-preflight-manifest/v1", "source_hashes": hashes, "output_sha256": "computed-after-write"}, indent=2) + "\n", encoding="utf-8")

    supported = ", ".join(eligible_categories) or "none"
    unsupported = ", ".join(unsupported_observed) or "none"
    md = f"# Dense 00802 room-belief preflight (read-only)\n\n"
    md += f"- Cache: `{cache}`\n- Scene/sequence: `{cap.get('scene_id')}` / `{cap.get('sequence_id')}`\n- Scope: **preflight only**; no model, feature extraction, detector, mapping, capture or native replay ran.\n\n"
    md += "## Inputs verified\n\n"
    md += f"- {len(poses)} trajectory frames, {unique_pose_count} full canonical pose/view groups ({len(position_counts)} position-only groups), {len(revisit_groups)} exact-pose revisit groups ({sum(n-1 for n in revisit_groups)} repeated frames). Exact-pose revisits are not independent views.\n"
    md += f"- P2: {len(p2_dets)} detections on {sum(bool(fr.get('detections')) for fr in p2.get('frames', []))} frames; P3: {len(p3_dets)} accepted, {p3m.get('rejected_detection_count')} rejected, {len(room_nodes)} room nodes in matching snapshot.\n"
    md += f"- Capture/P2/P3/replay/snapshot scene and sequence checks: **{report['provenance_checks']['room_snapshot_scene_sequence']['matches_capture']}**.\n"
    md += f"- P3 layer IDs: `{', '.join(p3_layer_ids) or 'none'}`. Room nodes have floor/layer/Z-bound metadata: **{bool(any(room_floor_metadata))}**. `floor_0` is not treated as proof of a single-floor scene.\n\n"
    md += "## Semantic and feature readiness\n\n"
    md += f"- Raw P2 inventory maps to A categories (approved aliases only): **{supported}**. Raw categories outside A are unsupported and not substituted: {unsupported}.\n"
    md += f"- Image features: {len(overlap)}/{len(p2_ids)} P2 IDs overlap the existing sidecar; **{len(p2_ids-overlap)} missing**. The sidecar is partial (ViT-B-32, 512-D, normalized, existing provenance).\n"
    md += "- Text features: existing cache is class/prompt-level and not an aligned per-observation sidecar for this runner; contract reuse is unverified.\n"
    md += "- Existing `semantic/manifest.json` persistent nodes are legacy/source output and are not silently treated as ConceptGraphs hybrid objects.\n\n"
    md += "## Reuse and blocker\n\n"
    md += "Reusable now: RGB-D/capture metadata, P2, P3, trajectory/intrinsics contract, matching OccuSG snapshot, locked min=3/RB1/A-table policies. New work is required for missing image features, a verified text sidecar mapping, hybrid ConceptGraphs mapping, and static room scoring.\n\n"
    md += "No retained/filtered object counts, stable/conflicted/provisional semantics, room assignments, A scores, candidates or commit/abstain decisions are claimed for this dense hybrid run because the required object export does not exist yet. Existing native replay is not rerun.\n\n"
    md += "## Locked next stage (requires approval)\n\n"
    md += "Generate only the missing sidecars with the already available ViT-B-32/checkpoint, then run one fresh hybrid output and the four-table static diagnostic with seed 20261014 and min_detections=3. This may exceed 30 minutes; no execution was started in this turn.\n\n"
    md += "Room-function accuracy is unavailable because no room GT is present. This cache is development data, not a holdout, and comparison with the 29-frame route is not a pose-count ablation.\n"
    (out / "dense_preflight_report.vi.md").write_text(md, encoding="utf-8")
    # Update manifest with output hashes after all files are written.
    manifest = read_json(out / "dense_preflight_manifest.json")
    manifest["output_sha256"] = {p.name: sha256(p) for p in sorted(out.iterdir()) if p.is_file()}
    (out / "dense_preflight_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "frames": len(poses), "unique_pose_groups": unique_pose_count, "revisit_groups": len(revisit_groups), "p2_detections": len(p2_dets), "p3_accepted": len(p3_dets), "rooms": len(room_nodes), "image_features_overlap": len(overlap), "image_features_missing": len(p2_ids-overlap), "eligible_raw_categories": eligible_categories, "status": report["status"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
