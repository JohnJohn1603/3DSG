#!/usr/bin/env python3
"""Encode cached detector class names with the already locked OpenCLIP model.

This produces a new, provenance-rich ConceptGraphs text_feats sidecar. It reads
P2 detector outputs and the existing image-feature ledger, but never reads GT,
human annotations, room labels, or association outputs, and never invokes the
image encoder or detector.
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
sys.path.insert(0, str(ROOT))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value / 1024 if sys.platform != "darwin" else value / (1024 * 1024))


def run(cache_root: Path, checkpoint: Path, output: Path, image_features_root: Path | None = None) -> dict:
    start = time.perf_counter()
    cache_root = cache_root.resolve()
    checkpoint = checkpoint.resolve()
    output = output.resolve()
    p2_root = cache_root / "detection_2d"
    index_path = p2_root / "detection_2d_index.json"
    feature_root = (image_features_root or (cache_root / "appearance/u2_features")).resolve()
    feature_manifest_path = feature_root / "feature_manifest.jsonl"
    extraction_report_path = feature_root / "extraction_report.json"
    model_meta_path = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/checkpoint_metadata.json"

    p2 = json.loads(index_path.read_text())
    extraction = json.loads(extraction_report_path.read_text())
    model_meta = json.loads(model_meta_path.read_text())
    expected_checkpoint_hash = model_meta["sha256"]
    if checkpoint.stat().st_size != model_meta["size_bytes"] or sha256_file(checkpoint) != expected_checkpoint_hash:
        raise ValueError("local ViT-B-32 checkpoint does not match pinned metadata SHA-256/size")
    if extraction.get("checkpoint_sha256") != expected_checkpoint_hash:
        raise ValueError("existing image-feature report uses a different checkpoint")
    if extraction.get("model_fingerprint") != "17d5da24f0319f81b00b9372aa11c366ab7c98e9b4911e4e2cfef65e8554ad83":
        raise ValueError("existing image-feature model fingerprint differs from the locked model")
    if p2.get("status") not in ("pass", None):
        raise ValueError(f"P2 cache status is not pass: {p2.get('status')}")

    detections = []
    class_by_id: dict[int, str] = {}
    for frame in sorted(p2["frames"], key=lambda row: int(row["frame_index"])):
        for det in frame["detections"]:
            obs_id = str(det["detection_id"])
            class_id = int(det["class_id"])
            raw_name = str(det["class_raw"])
            if not raw_name:
                raise ValueError(f"detector raw class missing: {obs_id}")
            if class_id in class_by_id and class_by_id[class_id] != raw_name:
                raise ValueError(f"cached class_id mapping conflicts for {class_id}: {class_by_id[class_id]!r} vs {raw_name!r}")
            class_by_id[class_id] = raw_name
            detections.append({"observation_id": obs_id, "frame_id": frame["frame_id"], "frame_index": int(frame["frame_index"]),
                               "class_id": class_id, "input_text": raw_name,
                               "mask_sha256": det["mask_sha256"], "score_raw": float(det["score_raw"])})

    feature_rows = [json.loads(line) for line in feature_manifest_path.read_text().splitlines() if line.strip()]
    p2_ids = {row["observation_id"] for row in detections}
    feature_by_id = {str(row["observation_id"]): row for row in feature_rows}
    if len(p2_ids) != len(detections) or p2_ids != set(feature_by_id):
        raise ValueError("P2 detections and cached image-feature observation IDs do not align exactly")
    if not detections:
        raise ValueError("P2 cache contains no detections")
    for row in detections:
        feat = feature_by_id[row["observation_id"]]
        if feat.get("checkpoint_sha256") != expected_checkpoint_hash or feat.get("feature_shape") != [512]:
            raise ValueError(f"cached image-feature provenance mismatch for {row['observation_id']}")
        if feat.get("mask_sha256") != row["mask_sha256"]:
            raise ValueError(f"P2 mask/image-feature provenance mismatch for {row['observation_id']}")

    output.mkdir(parents=True, exist_ok=False)
    tokenizer_source = ROOT / ".venv-appearance/lib/python3.12/site-packages/open_clip/tokenizer.py"
    bpe_source = tokenizer_source.with_name("bpe_simple_vocab_16e6.txt.gz")
    if not tokenizer_source.is_file() or not bpe_source.is_file():
        raise FileNotFoundError("OpenCLIP tokenizer source or bundled BPE vocabulary is missing from .venv-appearance")
    contract = {
        "schema": "conceptgraphs-text-feature-sidecar/v1",
        "status": "locked_before_encoding",
        "scope": "Cached detector raw class text encoding only; no mapping, detector, image encoding, GT, annotation, or room inputs.",
        "model": {"architecture": "ViT-B-32", "checkpoint_repository": model_meta["repository"],
                  "checkpoint_revision": model_meta["revision"], "checkpoint_file": checkpoint.name,
                  "checkpoint_sha256": expected_checkpoint_hash, "embedding_dimension": 512,
                  "open_clip_version": "3.2.0", "torch_version_from_existing_image_run": "2.6.0+cpu"},
        "tokenizer": {"call": "open_clip.get_tokenizer('ViT-B-32')", "input_rule": "exact cached class_raw string; no prompt template or canonicalization",
                      "tokenizer_module_sha256": sha256_file(tokenizer_source), "bpe_vocab_sha256": sha256_file(bpe_source),
                      "context_length": 77},
        "reference_equivalence": {"reference_call": "clip_tokenizer([classes[class_id]]) -> clip_model.encode_text(tokens) -> L2 normalize",
                                  "adaptation": "device is CPU; exact class text is resolved from cached detector class_id -> class_raw mapping",
                                  "text_encoding_only": True},
        "inputs": {"p2_index": str(index_path.relative_to(ROOT)), "p2_index_sha256": sha256_file(index_path),
                   "image_feature_manifest": str(feature_manifest_path.relative_to(ROOT)),
                   "image_feature_manifest_sha256": sha256_file(feature_manifest_path),
                   "model_metadata": str(model_meta_path.relative_to(ROOT)), "model_metadata_sha256": sha256_file(model_meta_path),
                   "detection_count": len(detections), "unique_class_id_count": len(class_by_id)},
        "output_policy": "new sidecar directory; source P2 and image-feature caches are read-only",
        "effective_config_sha256": None,
    }
    contract["effective_config_sha256"] = canonical_hash({k: v for k, v in contract.items() if k != "effective_config_sha256"})
    (output / "experiment_contract.json").write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")

    import torch
    import open_clip
    from safetensors.torch import load_file
    if open_clip.__version__ != "3.2.0":
        raise RuntimeError(f"expected OpenCLIP 3.2.0, found {open_clip.__version__}")
    torch.set_num_threads(min(4, __import__("os").cpu_count() or 1))
    torch.set_num_interop_threads(1)
    model, _train_preprocess, _preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained=None, device="cpu")
    state_dict = load_file(str(checkpoint), device="cpu")
    model.load_state_dict(state_dict, strict=True)
    del state_dict
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    tokenizer = open_clip.get_tokenizer("ViT-B-32")

    text_to_feature: dict[str, np.ndarray] = {}
    with torch.inference_mode():
        for text in sorted({row["input_text"] for row in detections}):
            tokens = tokenizer([text]).to("cpu")
            vector = model.encode_text(tokens)
            vector = vector / vector.norm(dim=-1, keepdim=True)
            arr = vector[0].detach().cpu().numpy().astype(np.float32, copy=False)
            if arr.shape != (512,) or not np.isfinite(arr).all() or not np.isclose(np.linalg.norm(arr), 1.0, atol=1e-5, rtol=1e-5):
                raise ValueError(f"invalid normalized text feature for detector label {text!r}")
            text_to_feature[text] = arr.copy()

    vectors = np.stack([text_to_feature[row["input_text"]] for row in detections]).astype(np.float32)
    ids = np.asarray([row["observation_id"] for row in detections], dtype=np.str_)
    texts = np.asarray([row["input_text"] for row in detections], dtype=np.str_)
    class_ids = np.asarray([row["class_id"] for row in detections], dtype=np.int32)
    npz_path = output / "text_features.npz"
    np.savez_compressed(npz_path, observation_id=ids, class_id=class_ids, input_text=texts, text_feature=vectors)
    rows_path = output / "text_feature_manifest.jsonl"
    with rows_path.open("w", encoding="utf-8") as stream:
        for index, row in enumerate(detections):
            stream.write(json.dumps({**row, "vector_index": index, "vector_sha256": hashlib.sha256(vectors[index].tobytes()).hexdigest()}, sort_keys=True, allow_nan=False) + "\n")
    if len(set(ids.tolist())) != len(detections) or vectors.shape != (len(detections), 512) or not np.isfinite(vectors).all():
        raise ValueError("text feature sidecar failed coverage/shape/finiteness validation")
    norms = np.linalg.norm(vectors, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-5, rtol=1e-5):
        raise ValueError("text feature sidecar failed L2 normalization validation")
    manifest = {
        "schema": "conceptgraphs-text-feature-sidecar/v1", "status": "pass",
        "observation_count": len(detections), "unique_class_text_count": len(text_to_feature),
        "embedding_shape": list(vectors.shape), "all_finite": True,
        "l2_norm_min": float(norms.min()), "l2_norm_max": float(norms.max()),
        "checkpoint_sha256": expected_checkpoint_hash,
        "contract_sha256": sha256_file(output / "experiment_contract.json"),
        "text_features_sha256": sha256_file(npz_path),
        "rows_sha256": sha256_file(rows_path),
        "runtime_s": time.perf_counter() - start, "peak_rss_mib": peak_rss_mib(),
        "device": "cpu", "model_downloaded": False, "detector_or_image_encoder_rerun": False,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-features", type=Path, default=None,
                        help="Image feature sidecar root; defaults to cache appearance/u2_features")
    args = parser.parse_args()
    print(json.dumps(run(args.cache_root, args.checkpoint, args.output, args.image_features), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
