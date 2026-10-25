#!/usr/bin/env python3
"""Extract locked U2/OpenCLIP features from saved P3-accepted scene crops.

No detector, P3, GT, room labels, or association assignments are run or read.
The crop and feature cache keys bind RGB, mask, crop, checkpoint and actual
preprocessing fingerprints. This is a generic scene-scoped companion to the
historical fixed-set U2 extractor; it does not alter the encoder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import extract_u2_appearance as u2  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument("--all-p2-observations", action="store_true",
                        help="Extract every cached P2 detection, including P3-rejected observations; does not run detector or P3.")
    args = parser.parse_args()
    started = time.perf_counter()
    run = args.run_dir.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite feature output: {output}")

    lock_path = ROOT / "artifacts/audits/u4_ov/u4_ov_config.locked.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if args.checkpoint:
        checkpoint = args.checkpoint.resolve()
    else:
        model_dir = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878"
        meta = json.loads((model_dir / "checkpoint_metadata.json").read_text(encoding="utf-8"))
        checkpoint = model_dir / meta["filename"]
    checkpoint_hash = sha(checkpoint)
    if checkpoint_hash != lock["model"]["checkpoint_sha256"]:
        raise RuntimeError("OpenCLIP checkpoint does not match the frozen U4-OV lock")

    capture_path = run / "capture/capture_manifest.json"
    p2_path = run / "detection_2d/detection_2d_index.json"
    p3_path = run / "detection_3d/detection_3d.jsonl"
    p3_manifest_path = run / "detection_3d/detection_3d_manifest.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    p2 = json.loads(p2_path.read_text(encoding="utf-8"))
    p3_manifest = json.loads(p3_manifest_path.read_text(encoding="utf-8"))
    if p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != sha(capture_path):
        raise RuntimeError("P2 cache is missing or does not match the captured RGB-D input")
    if p3_manifest.get("status") != "pass" or p3_manifest.get("capture_manifest_sha256") != sha(capture_path):
        raise RuntimeError("P3 cache is missing or does not match the captured RGB-D input")
    if p3_manifest.get("detection_2d_cache_sha256") != sha(p2_path):
        raise RuntimeError("P3 cache does not match the P2 index")

    cap_by_id = {str(row["frame_id"]): row for row in capture["frames"]}
    p2_by_id = {det["detection_id"]: (frame, det) for frame in p2["frames"] for det in frame.get("detections", [])}
    p3_rows = read_jsonl(p3_path)
    p3_accepted = {det["detection_id"] for batch in p3_rows for det in batch.get("detections", [])}
    if not p3_accepted or p3_accepted - set(p2_by_id):
        raise RuntimeError("P3 accepted IDs are empty or absent from P2")
    population_ids = set(p2_by_id) if args.all_p2_observations else p3_accepted

    observations = []
    for did in sorted(population_ids, key=lambda x: (int(p2_by_id[x][0]["frame_index"]), x)):
        frame, det = p2_by_id[did]
        caprec = cap_by_id[str(frame["frame_id"])]
        rgb_path = run / "capture" / caprec["frame_dir"] / "rgb.npy"
        mask_path = run / "detection_2d" / det["mask_file"]
        if sha(mask_path) != det["mask_sha256"]:
            raise RuntimeError(f"P2 mask hash mismatch: {did}")
        rgb = np.asarray(np.load(rgb_path, allow_pickle=False))
        if list(rgb.shape[:2]) != det["image_shape_hw"]:
            raise RuntimeError(f"RGB shape mismatch: {did}")
        observations.append({
            "observation_id": did, "frame_id": str(frame["frame_id"]), "detection_id": did,
            "class_canonical_review_only": str(det["class_canonical"]),
            "frame_index": int(frame["frame_index"]), "rgb": rgb,
            "rgb_path": rgb_path, "rgb_sha256": sha(rgb_path),
            "mask_path": mask_path, "mask_sha256": det["mask_sha256"],
            "bbox_xyxy": [float(x) for x in det["bbox_xyxy"]],
        })

    output.mkdir(parents=True, exist_ok=False)
    crop_records = u2.prepare_crop_records(observations, output / "crops")
    model_load_start = time.perf_counter()
    model, preprocess, torch = u2.load_model(checkpoint)
    model_load_s = time.perf_counter() - model_load_start
    preprocess_repr = u2.preprocessing_description(preprocess)
    preprocessing_fp = canonical_hash({"id": u2.PREPROCESSING_ID, "repr": preprocess_repr})
    model_fp = canonical_hash({"architecture": u2.MODEL_NAME, "checkpoint_repo": u2.CHECKPOINT_REPO,
                               "checkpoint_revision": u2.REVISION, "checkpoint_sha256": checkpoint_hash})

    cold_start = time.perf_counter()
    first_tensor = preprocess(__import__("PIL.Image", fromlist=["Image"]).fromarray(crop_records[0]["crop_rgb"], mode="RGB")).unsqueeze(0)
    with torch.inference_mode():
        first_output = model.encode_image(first_tensor)
    first_raw = first_output.detach().cpu().numpy().astype(np.float32, copy=False).reshape(-1)
    first_feature = first_raw / np.float32(np.linalg.norm(first_raw))
    cold_first_inference_s = time.perf_counter() - cold_start
    warm_start = time.perf_counter()
    with torch.inference_mode():
        _ = model.encode_image(first_tensor)
    warmup_s = time.perf_counter() - warm_start

    feature_rows = []
    vectors = []
    timings = []
    for i, row in enumerate(crop_records):
        tick = time.perf_counter()
        feature = u2.encode_crop(model, preprocess, torch, row["crop_rgb"])
        timings.append(time.perf_counter() - tick)
        cache_key = canonical_hash({
            "rgb_sha256": row["rgb_sha256"], "mask_sha256": row["mask_sha256"],
            "bbox_xyxy": row["bbox_xyxy"], "crop_sha256": row["crop_sha256"],
            "crop_policy": u2.PREPROCESSING_ID, "model_fingerprint": model_fp,
            "preprocessing_fingerprint": preprocessing_fp,
        })
        feature_path = output / "features" / (row["observation_id"].replace(":", "__") + "__" + cache_key[:16] + ".npz")
        feature_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(feature_path, image_feature=feature.astype(np.float32, copy=False))
        feature_rows.append({
            "schema_version": 1, "observation_id": row["observation_id"], "frame_id": row["frame_id"],
            "detection_id": row["detection_id"], "frame_index": row["frame_index"],
            "raw_class_review_only": row["class_canonical_review_only"],
            "rgb_sha256": row["rgb_sha256"], "mask_sha256": row["mask_sha256"],
            "bbox_xyxy": row["bbox_xyxy"], "crop_box_xyxy_float": row["crop_box_xyxy_float"],
            "crop_path": str(row["crop_path"].resolve().relative_to(ROOT)), "crop_sha256": row["crop_sha256"],
            "checkpoint_sha256": checkpoint_hash, "model_fingerprint": model_fp,
            "preprocessing_id": u2.PREPROCESSING_ID, "preprocessing_fingerprint": preprocessing_fp,
            "cache_key_sha256": cache_key,
            "feature_path": str(feature_path.resolve().relative_to(ROOT)),
            "feature_file_sha256": sha(feature_path), "feature_shape": [int(x) for x in feature.shape],
            "feature_dtype": str(feature.dtype), "feature_finite": bool(np.isfinite(feature).all()),
            "feature_l2_norm": float(np.linalg.norm(feature)), "device": "cpu",
        })
        vectors.append(feature)

    repeat_start = time.perf_counter()
    repeat = u2.encode_crop(model, preprocess, torch, crop_records[0]["crop_rgb"])
    repeat_s = time.perf_counter() - repeat_start
    repeat_ok = bool(np.allclose(first_feature, repeat, atol=u2.REPEAT_ATOL, rtol=u2.REPEAT_RTOL))
    repeat_cos = float(np.clip(np.dot(first_feature, repeat), -1.0, 1.0))
    if not repeat_ok or repeat_cos < u2.REPEAT_MIN_COSINE:
        raise RuntimeError(f"repeatability failed: allclose={repeat_ok}, cosine={repeat_cos}")

    manifest_path = output / "feature_manifest.jsonl"
    manifest_path.write_text("".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in feature_rows), encoding="utf-8")
    matrix = np.stack(vectors).astype(np.float32)
    report = {
        "schema_version": 1, "status": "pass",
        "scope": ("all P2 detections, including P3-rejected observations" if args.all_p2_observations
                  else "all P3-accepted observations; no GT/class-based selection"),
        "observation_count": len(feature_rows), "feature_shape": list(matrix.shape), "embedding_dimension": 512,
        "p3_accepted_count": len(p3_accepted), "p3_rejected_p2_count": len(set(p2_by_id) - p3_accepted),
        "all_finite": bool(np.isfinite(matrix).all()), "all_l2_norms_close_to_one": bool(np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5, rtol=1e-5)),
        "repeatability": {"observation_id": feature_rows[0]["observation_id"], "atol": u2.REPEAT_ATOL,
                          "rtol": u2.REPEAT_RTOL, "minimum_cosine": u2.REPEAT_MIN_COSINE,
                          "allclose": repeat_ok, "cosine": repeat_cos},
        "runtime_s": {"process_start_to_report": time.perf_counter() - started,
                      "model_load": model_load_s, "first_cold_inference": cold_first_inference_s,
                      "warmup_inference": warmup_s, "repeat_inference": repeat_s,
                      "inference_total": float(sum(timings)), "inference_mean_per_crop": float(np.mean(timings)),
                      "inference_p95_per_crop": float(np.quantile(timings, .95))},
        "peak_rss_mib": float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0,
        "checkpoint_sha256": checkpoint_hash, "preprocessing_id": u2.PREPROCESSING_ID,
        "preprocessing_fingerprint": preprocessing_fp, "model_fingerprint": model_fp,
        "feature_manifest": str(manifest_path.resolve().relative_to(ROOT)),
        "p2_sha256": sha(p2_path), "p3_sha256": sha(p3_path), "capture_sha256": sha(capture_path),
        "gt_used": False, "association_assignments_used": False,
    }
    (output / "extraction_report.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if report["all_finite"] and report["all_l2_norms_close_to_one"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
