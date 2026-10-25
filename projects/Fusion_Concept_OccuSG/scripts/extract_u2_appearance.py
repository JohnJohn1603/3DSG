#!/usr/bin/env python3
"""U2 OpenCLIP extraction from the already saved RGB-D detection audit set.

This script reads cached images, masks, boxes and the U1 accepted observation
list. It never invokes a detector, P3, native mapping, or an evaluator.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import resource
import sys
import time
from pathlib import Path
from typing import Any

PROCESS_START = time.perf_counter()

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a6f597a30f7b82c51704746581f9a4e41421e878"
MODEL_NAME = "ViT-B-32"
CHECKPOINT_REPO = "timm/vit_base_patch32_clip_224.openai"
CHECKPOINT_NAME = "open_clip_model.safetensors"
PADDING_PX = 20
PREPROCESSING_ID = "openclip-vit-b-32-openai-eval-224-cg-bboxpad20-pil-floatcrop-v1"
BENCHMARK_COUNT = 16
REPEAT_ATOL = 1e-5
REPEAT_RTOL = 1e-5
REPEAT_MIN_COSINE = 0.999999


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def preprocessing_description(preprocess) -> str:
    # Torchvision's repr embeds function memory addresses; normalize them so the
    # same actual transform has the same fingerprint across Python processes.
    return re.sub(r" at 0x[0-9a-fA-F]+", " at <address>", str(preprocess))


def external_paths() -> tuple[Path, Path, Path, Path]:
    resources = json.loads((ROOT / "locks/external_resources.json").read_text())
    manifest_path = Path(resources["files"]["dense_capture_manifest"]["path"])
    index_path = Path(resources["files"]["dense_detection_2d_index"]["path"])
    return manifest_path.parent, index_path.parent, manifest_path, index_path


def load_observations() -> list[dict[str, Any]]:
    _capture, _cache, _manifest, _index = external_paths()
    lineage_path = ROOT / "artifacts/audits/dining_table_bed_224_230/pointsets/observation_pointset_lineage.jsonl"
    accepted_ids = {
        row["observation_id"]
        for row in (json.loads(line) for line in lineage_path.read_text().splitlines())
        if row.get("p3_decision") == "accepted"
    }
    manifest = json.loads(_manifest.read_text())
    records = {str(row["frame_id"]): row for row in manifest["frames"]}
    cache_index = json.loads(_index.read_text())
    if cache_index.get("status") != "pass":
        raise RuntimeError("locked Detection2D cache is not pass")
    if cache_index.get("capture_manifest_sha256") != sha256_file(_manifest):
        raise RuntimeError("Detection2D cache no longer matches locked capture manifest")
    frames = {str(row["frame_id"]): row for row in cache_index["frames"]}
    observations = []
    for frame_id in sorted({value.rsplit(":det-", 1)[0] for value in accepted_ids}):
        if frame_id not in records or frame_id not in frames:
            raise RuntimeError(f"audit frame is missing from locked cache: {frame_id}")
        capture_record = records[frame_id]
        frame_cache = frames[frame_id]
        frame_dir = _capture / capture_record["frame_dir"]
        rgb_path = frame_dir / "rgb.npy"
        rgb = np.asarray(np.load(rgb_path, allow_pickle=False))
        if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] < 3:
            raise RuntimeError(f"unexpected RGB array schema: {rgb_path}")
        for detection in frame_cache["detections"]:
            observation_id = str(detection["detection_id"])
            if observation_id not in accepted_ids:
                continue
            mask_path = _cache / detection["mask_file"]
            if detection.get("mask_sha256") and sha256_file(mask_path) != detection["mask_sha256"]:
                raise RuntimeError(f"cached mask hash mismatch: {observation_id}")
            if list(rgb.shape[:2]) != list(detection["image_shape_hw"]):
                raise RuntimeError(f"RGB image shape mismatch: {observation_id}")
            observations.append({
                "observation_id": observation_id,
                "frame_id": frame_id,
                "detection_id": observation_id,
                "class_canonical_review_only": str(detection["class_canonical"]),
                "rgb_path": rgb_path,
                "rgb": rgb,
                "rgb_sha256": sha256_file(rgb_path),
                "mask_path": mask_path,
                "mask_sha256": sha256_file(mask_path),
                "bbox_xyxy": [float(value) for value in detection["bbox_xyxy"]],
            })
    observations.sort(key=lambda row: row["observation_id"])
    if len(observations) != len(accepted_ids) or len(observations) != 47:
        raise RuntimeError(f"expected exactly 47 unique accepted observations, got {len(observations)}")
    return observations


def make_crop(observation: dict[str, Any]) -> tuple[Image.Image, list[float]]:
    image = Image.fromarray(observation["rgb"][..., :3], mode="RGB")
    x_min, y_min, x_max, y_max = observation["bbox_xyxy"]
    width, height = image.size
    if not (0 <= x_min <= x_max <= width and 0 <= y_min <= y_max <= height):
        raise RuntimeError(f"out-of-image bbox; source crop helper would be unsafe: {observation['observation_id']}")
    left = min(PADDING_PX, x_min)
    top = min(PADDING_PX, y_min)
    right = min(PADDING_PX, width - x_max)
    bottom = min(PADDING_PX, height - y_max)
    # Preserve ConceptGraphs' float box behavior; PIL performs the pixel-edge rounding.
    crop_box = [x_min - left, y_min - top, x_max + right, y_max + bottom]
    return image.crop(tuple(crop_box)), crop_box


def prepare_crop_records(observations: list[dict[str, Any]], crop_dir: Path) -> list[dict[str, Any]]:
    crop_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for row in observations:
        crop, crop_box = make_crop(row)
        pixels = np.asarray(crop, dtype=np.uint8)
        crop_sha = sha256_bytes(pixels.tobytes())
        crop_path = crop_dir / (row["observation_id"].replace(":", "__") + "__" + crop_sha[:16] + ".png")
        crop.save(crop_path, format="PNG", optimize=False)
        records.append({
            **{key: value for key, value in row.items() if key not in ("rgb",)},
            "crop_box_xyxy_float": crop_box,
            "crop_sha256": crop_sha,
            "crop_path": crop_path,
            "crop_size_wh": list(crop.size),
            "crop_rgb": pixels,
        })
    return records


def deterministic_benchmark_indices(count: int, total: int) -> list[int]:
    if count > total:
        count = total
    return sorted(set(int(round(i * (total - 1) / max(1, count - 1))) for i in range(count)))


def load_model(checkpoint: Path):
    import logging
    import open_clip
    import torch
    from safetensors.torch import load_file

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    torch.set_num_interop_threads(1)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.setLevel(logging.ERROR)
    try:
        model, _train_preprocess, preprocess = open_clip.create_model_and_transforms(
            MODEL_NAME, pretrained=None, device="cpu"
        )
    finally:
        root_logger.setLevel(previous_level)
    state_dict = load_file(str(checkpoint), device="cpu")
    incompat = model.load_state_dict(state_dict, strict=True)
    if incompat.missing_keys or incompat.unexpected_keys:
        raise RuntimeError(f"checkpoint state mismatch: {incompat}")
    del state_dict
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model, preprocess, torch


def encode_crop(model, preprocess, torch, crop_rgb: np.ndarray) -> np.ndarray:
    image = Image.fromarray(crop_rgb, mode="RGB")
    tensor = preprocess(image).unsqueeze(0)
    with torch.inference_mode():
        output = model.encode_image(tensor)
    feature = output.detach().cpu().numpy().astype(np.float32, copy=False).reshape(-1)
    if feature.shape != (512,) or not np.isfinite(feature).all():
        raise RuntimeError(f"invalid image feature: shape={feature.shape}, finite={np.isfinite(feature).all()}")
    norm = float(np.linalg.norm(feature))
    if not np.isfinite(norm) or norm <= 0:
        raise RuntimeError("zero or non-finite image feature norm")
    feature = (feature / np.float32(norm)).astype(np.float32, copy=False)
    if not np.isclose(float(np.linalg.norm(feature)), 1.0, atol=1e-5, rtol=1e-5):
        raise RuntimeError("normalized feature norm is outside tolerance")
    return feature


def peak_rss_mib() -> float:
    # Linux reports ru_maxrss in KiB.
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def benchmark(checkpoint: Path, records: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    import torch
    import open_clip

    load_start = time.perf_counter()
    model, preprocess, torch = load_model(checkpoint)
    model_load_s = time.perf_counter() - load_start
    indices = deterministic_benchmark_indices(BENCHMARK_COUNT, len(records))
    selected = [records[index] for index in indices]
    prepared_start = time.perf_counter()
    tensors = [preprocess(Image.fromarray(row["crop_rgb"], mode="RGB")).unsqueeze(0) for row in selected]
    preprocess_s = time.perf_counter() - prepared_start

    first_start = time.perf_counter()
    with torch.inference_mode():
        first = model.encode_image(tensors[0])
    first_s = time.perf_counter() - first_start
    first_feature = first.detach().cpu().numpy().astype(np.float32).reshape(-1)
    first_norm = float(np.linalg.norm(first_feature))
    first_feature = first_feature / np.float32(first_norm)

    # One further warm-up call before timed inference.
    with torch.inference_mode():
        _ = model.encode_image(tensors[0])
    durations = []
    vectors = []
    for tensor in tensors:
        start = time.perf_counter()
        with torch.inference_mode():
            output_feature = model.encode_image(tensor)
        durations.append(time.perf_counter() - start)
        vector = output_feature.detach().cpu().numpy().astype(np.float32).reshape(-1)
        vector_norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector_norm) or vector_norm <= 0:
            raise RuntimeError("benchmark output has zero or non-finite pre-normalization norm")
        vector = vector / np.float32(vector_norm)
        if vector.shape != (512,) or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5, rtol=1e-5):
            raise RuntimeError("benchmark feature validation failed")
        vectors.append(vector)

    repeat_start = time.perf_counter()
    repeated = encode_crop(model, preprocess, torch, selected[0]["crop_rgb"])
    repeat_s = time.perf_counter() - repeat_start
    atol_match = bool(np.allclose(first_feature, repeated, atol=REPEAT_ATOL, rtol=REPEAT_RTOL))
    repeat_cosine = float(np.clip(np.dot(first_feature, repeated) / (np.linalg.norm(first_feature) * np.linalg.norm(repeated)), -1.0, 1.0))
    if not atol_match or repeat_cosine < REPEAT_MIN_COSINE:
        raise RuntimeError(f"repeatability failed: allclose={atol_match}, cosine={repeat_cosine}")

    sorted_durations = sorted(durations)
    report = {
        "schema_version": 1,
        "status": "pass",
        "benchmark_crop_count": len(selected),
        "selection": "16 evenly spread indices in sorted observation_id order; class/GT/track values not used",
        "benchmark_observation_ids": [row["observation_id"] for row in selected],
        "process_start_to_report_s": time.perf_counter() - PROCESS_START,
        "model_import_create_and_checkpoint_load_s": model_load_s,
        "crop_preprocess_wall_s": preprocess_s,
        "first_inference_s": first_s,
        "warmup_calls": 1,
        "timed_inference_after_warmup": {
            "count": len(durations),
            "total_s": float(sum(durations)),
            "mean_s_per_crop": float(np.mean(durations)),
            "median_s_per_crop": float(np.median(durations)),
            "p95_s_per_crop": float(np.quantile(durations, 0.95)),
            "throughput_crops_s": float(len(durations) / sum(durations)),
        },
        "repeatability": {
            "observation_id": selected[0]["observation_id"],
            "first_feature_vector_sha256": sha256_bytes(first_feature.astype(np.float32, copy=False).tobytes()),
            "repeat_inference_s": repeat_s,
            "allclose_atol": REPEAT_ATOL,
            "allclose_rtol": REPEAT_RTOL,
            "allclose_pass": atol_match,
            "cosine_similarity": repeat_cosine,
            "minimum_cosine": REPEAT_MIN_COSINE,
        },
        "feature_contract": {
            "shape": [512],
            "dtype": "float32",
            "all_finite": bool(all(np.isfinite(vector).all() for vector in vectors)),
            "l2_norm_atol_rtol": 1e-5,
            "all_norms_close_to_one": bool(all(np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5, rtol=1e-5) for vector in vectors)),
        },
        "runtime": {
            "torch": torch.__version__,
            "open_clip": open_clip.__version__ if hasattr(open_clip, "__version__") else "unknown",
            "device": "cpu",
            "torch_num_threads": torch.get_num_threads(),
            "torch_num_interop_threads": torch.get_num_interop_threads(),
            "cpu_count": os.cpu_count(),
            "peak_rss_mib": peak_rss_mib(),
            "preprocess_repr": preprocessing_description(preprocess),
        },
        "model": {
            "architecture": MODEL_NAME,
            "checkpoint_repository": CHECKPOINT_REPO,
            "checkpoint_revision": REVISION,
            "checkpoint_file_sha256": sha256_file(checkpoint),
            "checkpoint_state_dict_load": "strict_pass",
            "preprocessing_id": PREPROCESSING_ID,
            "preprocessing_fingerprint": canonical_hash({"id": PREPROCESSING_ID, "repr": preprocessing_description(preprocess)}),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report


def extract_all(checkpoint: Path, records: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    model, preprocess, torch = load_model(checkpoint)
    preprocess_repr = preprocessing_description(preprocess)
    checkpoint_hash = sha256_file(checkpoint)
    preprocess_fingerprint = canonical_hash({"id": PREPROCESSING_ID, "repr": preprocess_repr})
    model_fingerprint = canonical_hash({
        "architecture": MODEL_NAME,
        "checkpoint_repo": CHECKPOINT_REPO,
        "checkpoint_revision": REVISION,
        "checkpoint_sha256": checkpoint_hash,
    })
    crop_dir = output_dir / "crops"
    features_dir = output_dir / "features"
    features_dir.mkdir(parents=True, exist_ok=True)
    prepared_records = prepare_crop_records(records, crop_dir)
    feature_rows = []
    vectors = []
    manifest_path = output_dir / "feature_manifest.jsonl"
    existing_cache_keys = set()
    if manifest_path.exists():
        existing_cache_keys = {
            json.loads(line)["cache_key_sha256"]
            for line in manifest_path.read_text().splitlines()
            if line.strip()
        }
    for row in prepared_records:
        key = {
            "rgb_sha256": row["rgb_sha256"],
            "mask_sha256": row["mask_sha256"],
            "bbox_xyxy": row["bbox_xyxy"],
            "crop_policy": PREPROCESSING_ID,
            "crop_sha256": row["crop_sha256"],
            "model_fingerprint": model_fingerprint,
            "preprocessing_fingerprint": preprocess_fingerprint,
        }
        cache_key = canonical_hash(key)
        feature_path = features_dir / (row["observation_id"].replace(":", "__") + "__" + cache_key[:16] + ".npz")
        if feature_path.exists():
            with np.load(feature_path, allow_pickle=False) as cached:
                feature = np.asarray(cached["image_feature"], dtype=np.float32)
            if feature.shape != (512,) or not np.isfinite(feature).all() or not np.isclose(np.linalg.norm(feature), 1.0, atol=1e-5, rtol=1e-5):
                raise RuntimeError(f"existing cache failed feature contract: {feature_path}")
        else:
            feature = encode_crop(model, preprocess, torch, row["crop_rgb"])
            np.savez_compressed(feature_path, image_feature=feature)
        if feature_path.stat().st_size <= 0:
            raise RuntimeError(f"feature cache is empty: {feature_path}")
        row_out = {
            "schema_version": 1,
            "observation_id": row["observation_id"],
            "frame_id": row["frame_id"],
            "detection_id": row["detection_id"],
            "class_canonical_review_only": row["class_canonical_review_only"],
            "rgb_sha256": row["rgb_sha256"],
            "mask_sha256": row["mask_sha256"],
            "bbox_xyxy": row["bbox_xyxy"],
            "crop_box_xyxy_float": row["crop_box_xyxy_float"],
            "crop_size_wh": row["crop_size_wh"],
            "crop_sha256": row["crop_sha256"],
            "crop_ref": str(row["crop_path"].relative_to(ROOT)),
            "model_fingerprint": model_fingerprint,
            "preprocessing_id": PREPROCESSING_ID,
            "preprocessing_fingerprint": preprocess_fingerprint,
            "cache_key_sha256": cache_key,
            "feature_ref": str(feature_path.relative_to(ROOT)),
            "feature_sha256": sha256_file(feature_path),
            "feature_shape": [int(value) for value in feature.shape],
            "feature_dtype": str(feature.dtype),
            "feature_finite": bool(np.isfinite(feature).all()),
            "feature_l2_norm": float(np.linalg.norm(feature)),
            "device": "cpu",
        }
        feature_rows.append(row_out)
        vectors.append(feature)

    new_rows = [row for row in feature_rows if row["cache_key_sha256"] not in existing_cache_keys]
    if new_rows:
        with manifest_path.open("a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in new_rows)
    matrix = np.stack(vectors).astype(np.float32)
    # M6 output is joined only after extraction, for human review of every pair.
    # It is never passed to crop preparation, model inference, or cache-key creation.
    association_path = ROOT / "artifacts/audits/dining_table_bed_224_230/object_association_lineage_prefix_231.jsonl"
    review_associations = {
        row["detection_id"]: row
        for row in (json.loads(line) for line in association_path.read_text().splitlines())
    }
    if any(row["detection_id"] not in review_associations for row in feature_rows):
        raise RuntimeError("M6 review lineage is missing an accepted observation")
    similarity_path = output_dir / "similarity_all_pairs.csv"
    with similarity_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "observation_id_left", "detection_id_left", "class_left_review_only", "m6_object_id_left_review_only", "m6_decision_left_review_only", "crop_ref_left", "crop_sha256_left",
            "observation_id_right", "detection_id_right", "class_right_review_only", "m6_object_id_right_review_only", "m6_decision_right_review_only", "crop_ref_right", "crop_sha256_right",
            "cosine_similarity",
        ])
        writer.writeheader()
        for left in range(len(feature_rows)):
            for right in range(left + 1, len(feature_rows)):
                writer.writerow({
                    "observation_id_left": feature_rows[left]["observation_id"],
                    "detection_id_left": feature_rows[left]["detection_id"],
                    "class_left_review_only": feature_rows[left]["class_canonical_review_only"],
                    "m6_object_id_left_review_only": review_associations[feature_rows[left]["detection_id"]]["object_id"],
                    "m6_decision_left_review_only": review_associations[feature_rows[left]["detection_id"]]["decision"],
                    "crop_ref_left": feature_rows[left]["crop_ref"],
                    "crop_sha256_left": feature_rows[left]["crop_sha256"],
                    "observation_id_right": feature_rows[right]["observation_id"],
                    "detection_id_right": feature_rows[right]["detection_id"],
                    "class_right_review_only": feature_rows[right]["class_canonical_review_only"],
                    "m6_object_id_right_review_only": review_associations[feature_rows[right]["detection_id"]]["object_id"],
                    "m6_decision_right_review_only": review_associations[feature_rows[right]["detection_id"]]["decision"],
                    "crop_ref_right": feature_rows[right]["crop_ref"],
                    "crop_sha256_right": feature_rows[right]["crop_sha256"],
                    "cosine_similarity": float(np.clip(np.dot(matrix[left], matrix[right]), -1.0, 1.0)),
                })
    summary = {
        "schema_version": 1,
        "status": "pass",
        "observation_count": len(feature_rows),
        "pair_count": len(feature_rows) * (len(feature_rows) - 1) // 2,
        "manifest": str(manifest_path.relative_to(ROOT)),
        "similarity_csv": str(similarity_path.relative_to(ROOT)),
        "review_only_m6_lineage": str(association_path.relative_to(ROOT)),
        "m6_lineage_used_for_features_or_match_selection": False,
        "crop_dir": str(crop_dir.relative_to(ROOT)),
        "feature_dir": str(features_dir.relative_to(ROOT)),
        "model_fingerprint": model_fingerprint,
        "preprocessing_fingerprint": preprocess_fingerprint,
        "all_features_finite": bool(np.isfinite(matrix).all()),
        "all_l2_norms_close_to_one": bool(all(np.isclose(np.linalg.norm(v), 1.0, atol=1e-5, rtol=1e-5) for v in matrix)),
        "feature_shape": list(matrix.shape),
        "peak_rss_mib": peak_rss_mib(),
    }
    (output_dir / "extraction_report.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("benchmark", "extract"))
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "artifacts/models/openclip" / f"vit-b-32-openai-{REVISION}" / CHECKPOINT_NAME)
    args = parser.parse_args()
    if not args.checkpoint.is_file():
        raise SystemExit(f"checkpoint not found: {args.checkpoint}")
    observations = load_observations()
    audit_dir = ROOT / "artifacts/audits/dining_table_bed_224_230/appearance"
    if args.mode == "benchmark":
        indices = deterministic_benchmark_indices(BENCHMARK_COUNT, len(observations))
        selected = [observations[index] for index in indices]
        prepared = prepare_crop_records(selected, audit_dir / "benchmark_crops")
        report = benchmark(args.checkpoint, prepared, audit_dir / "benchmark_report.json")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    output = extract_all(args.checkpoint, observations, audit_dir)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["all_features_finite"] and output["all_l2_norms_close_to_one"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
