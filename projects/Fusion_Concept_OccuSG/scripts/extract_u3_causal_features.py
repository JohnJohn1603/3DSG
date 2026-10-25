#!/usr/bin/env python3
"""Extract/reuse U2 image features for the frozen 0..238 causal sequence.

Only cached RGB, 2D boxes/masks, saved P3-accepted detections, and the pinned
U2 encoder are read. This does not invoke YOLO, P3, GT, or native stages.
New artifacts are isolated under artifacts/audits/u3_causal_prefix/.
"""
from __future__ import annotations

import hashlib
import json
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import extract_u2_appearance as u2  # noqa: E402

APP_U2 = ROOT / "artifacts/audits/dining_table_bed_224_230/appearance"
OUT = ROOT / "artifacts/audits/u3_causal_prefix"
FEATURES = OUT / "appearance"


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def locked_file(key: str) -> Path:
    lock = json.loads((ROOT / "locks/external_resources.json").read_text())["files"][key]
    path = Path(lock["path"])
    if path.stat().st_size != lock["bytes"] or sha_file(path) != lock["sha256"]:
        raise RuntimeError(f"locked input changed: {key}")
    return path


def read_locked_inputs():
    capture_manifest_path = locked_file("dense_capture_manifest")
    d2_index_path = locked_file("dense_detection_2d_index")
    d3_cache_path = locked_file("dense_detection_3d_cache")
    capture_root, d2_root = capture_manifest_path.parent, d2_index_path.parent
    capture = json.loads(capture_manifest_path.read_text())
    d2_index = json.loads(d2_index_path.read_text())
    if d2_index.get("status") != "pass" or d2_index.get("capture_manifest_sha256") != sha_file(capture_manifest_path):
        raise RuntimeError("locked detection cache does not match capture manifest")
    capture_frames = {r["frame_id"]: r for r in capture["frames"]}
    d2_frames = {r["frame_id"]: r for r in d2_index["frames"]}
    d2_by_id = {d["detection_id"]: d for f in d2_index["frames"] for d in f["detections"]}
    d3_frames = [json.loads(line) for line in d3_cache_path.read_text().splitlines() if line]
    selected = []
    for frame in d3_frames:
        frame_number = int(frame["frame_id"].rsplit("-", 1)[-1])
        if not 0 <= frame_number <= 238:
            continue
        if frame["frame_id"] not in capture_frames or frame["frame_id"] not in d2_frames:
            raise RuntimeError(f"missing locked input frame {frame['frame_id']}")
        for d3 in frame["detections"]:
            did = d3["detection_id"]
            d2 = d2_by_id.get(did)
            if d2 is None:
                raise RuntimeError(f"saved P3 observation has no saved P2 record: {did}")
            if d2["mask_file"] != d3["provider_metadata"]["mask_file"]:
                raise RuntimeError(f"P2/P3 mask provenance differs: {did}")
            mask_path = d2_root / d2["mask_file"]
            if sha_file(mask_path) != d2["mask_sha256"]:
                raise RuntimeError(f"mask hash mismatch: {did}")
            selected.append({"frame_id": frame["frame_id"], "frame_number": frame_number,
                             "timestamp_s": float(d3["timestamp_s"]), "d3": d3, "d2": d2,
                             "mask_path": mask_path, "capture_record": capture_frames[frame["frame_id"]]})
    selected.sort(key=lambda r: (r["frame_number"], r["d3"]["detection_id"]))
    return selected, capture_root, d2_root, capture_manifest_path, d2_index_path, d3_cache_path


def load_active_u2_rows(sequence_ids: set[str]) -> tuple[dict[str, dict], dict]:
    report = json.loads((APP_U2 / "extraction_report.json").read_text())
    manifest_path = APP_U2 / "feature_manifest.jsonl"
    rows = [json.loads(line) for line in manifest_path.read_text().splitlines() if line]
    active = {r["detection_id"]: r for r in rows
              if r["preprocessing_fingerprint"] == report["preprocessing_fingerprint"]}
    if len(active) != 47 or not set(active).issubset(sequence_ids):
        raise RuntimeError("U2 active input must be exactly 47 records within the causal sequence")
    for r in active.values():
        if sha_file(ROOT / r["feature_ref"]) != r["feature_sha256"]:
            raise RuntimeError(f"U2 feature file changed: {r['detection_id']}")
    return active, report


def load_pointset_index() -> dict[str, dict]:
    path = ROOT / "artifacts/audits/dining_table_bed_224_230/pointsets/observation_pointset_lineage.jsonl"
    rows = {json.loads(line)["observation_id"]: json.loads(line) for line in path.read_text().splitlines() if line}
    for did, row in rows.items():
        point_path = path.parent / row["pointset_ref"]
        if sha_file(point_path) != row["pointset_sha256"]:
            raise RuntimeError(f"pointset sidecar changed: {did}")
    return rows


def main() -> int:
    started = time.perf_counter()
    observations, capture_root, d2_root, capture_manifest_path, d2_index_path, d3_cache_path = read_locked_inputs()
    ids = {r["d3"]["detection_id"] for r in observations}
    active_u2, u2_report = load_active_u2_rows(ids)
    pointsets = load_pointset_index()
    model_meta_path = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/checkpoint_metadata.json"
    model_meta = json.loads(model_meta_path.read_text())
    checkpoint = model_meta_path.parent / model_meta["filename"]
    benchmark_lock = json.loads((APP_U2 / "benchmark_report.json").read_text())
    if model_meta["sha256"] != benchmark_lock["model"]["checkpoint_file_sha256"]:
        raise RuntimeError("checkpoint metadata differs from the frozen U2 model lock")
    if not checkpoint.is_file() or sha_file(checkpoint) != benchmark_lock["model"]["checkpoint_file_sha256"]:
        raise RuntimeError("pinned U2 checkpoint is missing or has a different SHA-256")

    OUT.mkdir(parents=True, exist_ok=True)
    crop_dir, feature_dir = FEATURES / "crops", FEATURES / "features"
    crop_dir.mkdir(parents=True, exist_ok=True); feature_dir.mkdir(parents=True, exist_ok=True)
    model, preprocess, torch = u2.load_model(checkpoint)
    preprocess_repr = u2.preprocessing_description(preprocess)
    preprocessing_fp = u2.canonical_hash({"id": u2.PREPROCESSING_ID, "repr": preprocess_repr})
    model_fp = u2.canonical_hash({"architecture": u2.MODEL_NAME, "checkpoint_repo": u2.CHECKPOINT_REPO,
                                  "checkpoint_revision": u2.REVISION,
                                  "checkpoint_sha256": u2.sha256_file(checkpoint)})
    if preprocessing_fp != u2_report["preprocessing_fingerprint"] or model_fp != u2_report["model_fingerprint"]:
        raise RuntimeError("U2 runtime/model fingerprint differs from the frozen lock")

    records_out = []
    timing = []
    frame_rgb_cache: dict[str, tuple[np.ndarray, str, Path]] = {}
    for obs in observations:
        d3, d2, fid = obs["d3"], obs["d2"], obs["frame_id"]
        if fid not in frame_rgb_cache:
            rgb_path = capture_root / obs["capture_record"]["frame_dir"] / "rgb.npy"
            rgb = np.asarray(np.load(rgb_path, allow_pickle=False))
            if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] < 3:
                raise RuntimeError(f"unexpected RGB schema: {rgb_path}")
            frame_rgb_cache = {fid: (rgb, sha_file(rgb_path), rgb_path)}
        rgb, rgb_sha, rgb_path = frame_rgb_cache[fid]
        if list(rgb.shape[:2]) != list(d2["image_shape_hw"]):
            raise RuntimeError(f"RGB and detection dimensions differ: {d3['detection_id']}")
        crop, crop_box = u2.make_crop({"rgb": rgb, "bbox_xyxy": d2["bbox_xyxy"]})
        crop_rgb = np.asarray(crop, dtype=np.uint8)
        crop_sha = u2.sha256_bytes(crop_rgb.tobytes())
        mask_sha = d2["mask_sha256"]
        did = d3["detection_id"]
        old = active_u2.get(did)
        if old is not None:
            if (old["rgb_sha256"] != rgb_sha or old["mask_sha256"] != mask_sha
                    or old["crop_sha256"] != crop_sha
                    or old["bbox_xyxy"] != [float(v) for v in d2["bbox_xyxy"]]):
                raise RuntimeError(f"locked U2 record no longer matches raw crop inputs: {did}")
            row = dict(old)
            # Existing U2 record is reused exactly; retain its established refs.
            with np.load(ROOT / row["feature_ref"], allow_pickle=False) as saved:
                vec = np.asarray(saved["image_feature"], dtype=np.float32)
            if vec.shape != (512,) or not np.isfinite(vec).all() or not np.isclose(np.linalg.norm(vec), 1., atol=1e-5, rtol=1e-5):
                raise RuntimeError(f"U2 feature failed contract: {did}")
        else:
            crop_path = crop_dir / (did.replace(":", "__") + "__" + crop_sha[:16] + ".png")
            if crop_path.exists():
                if u2.sha256_bytes(np.asarray(Image.open(crop_path).convert("RGB"), dtype=np.uint8).tobytes()) != crop_sha:
                    raise RuntimeError(f"existing crop cache mismatch: {did}")
            else:
                crop.save(crop_path, format="PNG", optimize=False)
            cache_key = u2.canonical_hash({
                "rgb_sha256": rgb_sha, "mask_sha256": mask_sha,
                "bbox_xyxy": [float(v) for v in d2["bbox_xyxy"]],
                "crop_policy": u2.PREPROCESSING_ID, "crop_sha256": crop_sha,
                "model_fingerprint": model_fp, "preprocessing_fingerprint": preprocessing_fp,
            })
            feature_path = feature_dir / (did.replace(":", "__") + "__" + cache_key[:16] + ".npz")
            if feature_path.exists():
                with np.load(feature_path, allow_pickle=False) as saved:
                    vec = np.asarray(saved["image_feature"], dtype=np.float32)
            else:
                infer_start = time.perf_counter()
                vec = u2.encode_crop(model, preprocess, torch, crop_rgb)
                timing.append(time.perf_counter() - infer_start)
                np.savez_compressed(feature_path, image_feature=vec)
            if vec.shape != (512,) or not np.isfinite(vec).all() or not np.isclose(np.linalg.norm(vec), 1., atol=1e-5, rtol=1e-5):
                raise RuntimeError(f"new feature failed contract: {did}")
            row = {
                "schema_version": 1, "observation_id": did, "frame_id": fid, "detection_id": did,
                "bbox_xyxy": [float(v) for v in d2["bbox_xyxy"]],
                "crop_box_xyxy_float": [float(v) for v in crop_box],
                "rgb_sha256": rgb_sha, "mask_sha256": mask_sha, "crop_sha256": crop_sha,
                "crop_ref": str(crop_path.relative_to(ROOT)), "crop_file_sha256": sha_file(crop_path),
                "crop_size_wh": list(crop.size), "model_fingerprint": model_fp,
                "preprocessing_id": u2.PREPROCESSING_ID, "preprocessing_fingerprint": preprocessing_fp,
                "cache_key_sha256": cache_key, "feature_ref": str(feature_path.relative_to(ROOT)),
                "feature_sha256": sha_file(feature_path), "feature_shape": [512],
                "feature_dtype": "float32", "feature_finite": True,
                "feature_l2_norm": float(np.linalg.norm(vec)), "device": "cpu",
            }
        did = d3["detection_id"]
        ps = pointsets.get(did)
        geom = {"center_W": d3["center_W"], "extents_m": d3["extents_m"], "layer_id": d3["layer_id"]}
        records_out.append({
            "detection_id": did, "frame_id": fid, "frame_index": obs["capture_record"]["frame_index"],
            "timestamp_s": obs["timestamp_s"], "crop_sha256": row["crop_sha256"],
            "crop_ref": row["crop_ref"], "crop_file_sha256": sha_file(ROOT / row["crop_ref"]),
            "mask_sha256": row["mask_sha256"], "rgb_sha256": row["rgb_sha256"],
            "feature_ref": row["feature_ref"], "feature_sha256": row["feature_sha256"],
            "cache_key_sha256": row["cache_key_sha256"],
            "geometry_sha256": sha_bytes(json.dumps(geom, sort_keys=True, separators=(",", ":")).encode()),
            "center_W": d3["center_W"], "extents_m": d3["extents_m"], "layer_id": d3["layer_id"],
            "pointset_ref": None if ps is None else str((ROOT / "artifacts/audits/dining_table_bed_224_230/pointsets" / ps["pointset_ref"]).relative_to(ROOT)),
            "pointset_sha256": None if ps is None else ps["pointset_sha256"],
            "pointset_available": ps is not None,
        })

    # Sequence contract and cache completeness are checked before writing the
    # immutable index. GT/evaluator data is deliberately not read here.
    expected = {r["d3"]["detection_id"] for r in observations}
    if len(records_out) != 622 or len({r["detection_id"] for r in records_out}) != len(expected):
        raise RuntimeError("causal sequence must contain 622 unique saved P3 observations")
    index = {
        "schema_version": 1, "immutable": True, "status": "pass", "record_count": len(records_out),
        "prefix_frames": [0, 230], "temporal_holdout_frames": [231, 238],
        "sequence_scope": "same-scene temporal holdout; not a scene holdout",
        "input_locks": {
            "capture_manifest_sha256": sha_file(capture_manifest_path),
            "detection_2d_index_sha256": sha_file(d2_index_path),
            "detection_3d_cache_sha256": sha_file(d3_cache_path),
            "u2_feature_manifest_sha256": sha_file(APP_U2 / "feature_manifest.jsonl"),
            "u2_preprocessing_fingerprint": preprocessing_fp,
            "u2_model_fingerprint": model_fp,
        },
        "counts": {"prefix_0_230": 583, "holdout_231_238": 39,
                   "u2_features_reused": len(active_u2), "features_extracted_here": len(records_out) - len(active_u2),
                   "pointset_sidecars_available": sum(r["pointset_available"] for r in records_out),
                   "geometry_records_available": sum(r["center_W"] is not None and r["extents_m"] is not None for r in records_out)},
        "records": records_out,
    }
    index_path = OUT / "causal_input_index.json"
    payload = json.dumps(index, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if index_path.exists():
        if json.loads(index_path.read_text()) != index:
            raise RuntimeError("causal input index already exists with different contents")
    else:
        index_path.write_text(payload)
    index_sha = sha_file(index_path)
    digest_path = OUT / "causal_input_index.sha256"
    digest_text = f"{index_sha}  causal_input_index.json\n"
    if digest_path.exists() and digest_path.read_text() != digest_text:
        raise RuntimeError("causal index digest differs from existing immutable lock")
    if not digest_path.exists():
        digest_path.write_text(digest_text)
    summary = {
        "schema_version": 1, "status": "features_ready",
        "record_count": len(records_out), "u2_features_reused": len(active_u2),
        "features_extracted_here": len(records_out) - len(active_u2),
        "feature_inference_seconds": float(sum(timing)),
        "mean_seconds_per_new_crop": float(np.mean(timing)) if timing else 0.0,
        "feature_shape": [512], "all_finite_and_normalized": True,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        "runtime_seconds_total": time.perf_counter() - started,
        "pointset_sidecars_available": sum(r["pointset_available"] for r in records_out),
        "geometry_available": sum(r["center_W"] is not None and r["extents_m"] is not None for r in records_out),
        "input_index_sha256": index_sha,
        "no_detector_p3_or_native_stage_invoked": True,
        "gt_used": False,
    }
    (OUT / "feature_extraction_report.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
