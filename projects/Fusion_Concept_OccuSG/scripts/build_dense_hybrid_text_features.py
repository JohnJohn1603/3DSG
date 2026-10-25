#!/usr/bin/env python3
"""Build observation-indexed ConceptGraphs text_feats for a dense P2 cache.

The existing U4-OV prompt cache is intentionally not reused: its prompt
templates differ from the pinned ConceptGraphs raw-class contract.  This
script encodes each unique cached detector class once and references it for
all observations of that class.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--image-manifest", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    started = time.perf_counter()
    cache = args.cache.resolve(); output = args.output.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    p2_path = cache / "detection_2d/detection_2d_index.json"
    p2 = json.loads(p2_path.read_text())
    detections = []
    class_by_id = {}
    for frame in sorted(p2["frames"], key=lambda x: int(x["frame_index"])):
        for d in frame.get("detections", []):
            oid = str(d["detection_id"]); cls = str(d["class_raw"]); cid = int(d["class_id"])
            if cid in class_by_id and class_by_id[cid] != cls:
                raise RuntimeError(f"class_id mapping conflict: {cid}")
            class_by_id[cid] = cls
            detections.append({"observation_id": oid, "frame_id": str(frame["frame_id"]), "frame_index": int(frame["frame_index"]),
                               "class_id": cid, "input_text": cls, "mask_sha256": d["mask_sha256"], "score_raw": float(d["score_raw"])})
    image_rows = [json.loads(x) for x in args.image_manifest.read_text().splitlines() if x.strip()]
    image_ids = {str(r["observation_id"]) for r in image_rows}
    if image_ids != {r["observation_id"] for r in detections}:
        raise RuntimeError("image feature manifest does not cover the dense P2 population")
    meta_path = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/checkpoint_metadata.json"
    model_meta = json.loads(meta_path.read_text())
    expected_ckpt = model_meta["sha256"]
    if sha(args.checkpoint) != expected_ckpt:
        raise RuntimeError("checkpoint SHA mismatch")
    import torch
    import open_clip
    from safetensors.torch import load_file
    if open_clip.__version__ != "3.2.0":
        raise RuntimeError(f"expected OpenCLIP 3.2.0, found {open_clip.__version__}")
    torch.set_num_threads(min(4, __import__("os").cpu_count() or 1)); torch.set_num_interop_threads(1)
    model, _train, _pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained=None, device="cpu")
    state = load_file(str(args.checkpoint), device="cpu"); model.load_state_dict(state, strict=True); del state
    model.eval()
    tokenizer = open_clip.get_tokenizer("ViT-B-32")
    unique_texts = sorted({r["input_text"] for r in detections})
    text_to_vec = {}
    token_records = {}
    with torch.inference_mode():
        for text in unique_texts:
            tokens = tokenizer([text]).to("cpu")
            vec = model.encode_text(tokens)
            vec = vec / vec.norm(dim=-1, keepdim=True)
            arr = vec[0].detach().cpu().numpy().astype(np.float32, copy=False)
            if arr.shape != (512,) or not np.isfinite(arr).all() or not np.isclose(np.linalg.norm(arr), 1.0, atol=1e-5, rtol=1e-5):
                raise RuntimeError(f"invalid text vector for {text!r}")
            text_to_vec[text] = arr.copy()
            token_records[text] = {"input_text": text, "token_ids": tokens[0].detach().cpu().tolist(), "token_count": int(tokens.shape[1])}
    vectors = np.stack([text_to_vec[r["input_text"]] for r in detections]).astype(np.float32)
    ids = np.asarray([r["observation_id"] for r in detections], dtype=np.str_)
    texts = np.asarray([r["input_text"] for r in detections], dtype=np.str_)
    class_ids = np.asarray([r["class_id"] for r in detections], dtype=np.int32)
    npz = output / "text_features.npz"
    np.savez_compressed(npz, observation_id=ids, class_id=class_ids, input_text=texts, text_feature=vectors)
    rows_path = output / "text_feature_manifest.jsonl"
    rows_path.write_text("".join(json.dumps({**r, "vector_index": i, "vector_sha256": hashlib.sha256(vectors[i].tobytes()).hexdigest()}, sort_keys=True, allow_nan=False) + "\n" for i, r in enumerate(detections)))
    contract = {"schema": "conceptgraphs-text-feature-sidecar/v1", "status": "pass",
                "scope": "dense cached P2 raw detector classes; no GT, annotation, room label, detector or image encoding",
                "model": {"architecture": "ViT-B-32", "checkpoint_revision": model_meta["revision"], "checkpoint_sha256": expected_ckpt, "embedding_dimension": 512, "open_clip_version": open_clip.__version__},
                "tokenizer": {"call": "open_clip.get_tokenizer('ViT-B-32')", "input_rule": "exact cached class_raw string; no prompt template or canonicalization", "context_length": 77},
                "reference_equivalence": {"reference_call": "clip_tokenizer([classes[class_id]]) -> clip_model.encode_text(tokens) -> L2 normalize", "adaptation": "CPU device only; each unique raw class encoded once and referenced by observation"},
                "inputs": {"p2_index": str(p2_path), "p2_index_sha256": sha(p2_path), "image_feature_manifest": str(args.image_manifest), "image_feature_manifest_sha256": sha(args.image_manifest), "unique_class_count": len(unique_texts), "observation_count": len(detections)},
                "unique_texts": unique_texts, "token_records": token_records}
    contract_path = output / "experiment_contract.json"; contract_path.write_text(json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    norms = np.linalg.norm(vectors, axis=1)
    manifest = {"schema": "conceptgraphs-text-feature-sidecar/v1", "status": "pass", "observation_count": len(detections), "unique_class_text_count": len(unique_texts), "embedding_shape": list(vectors.shape), "all_finite": bool(np.isfinite(vectors).all()), "l2_norm_min": float(norms.min()), "l2_norm_max": float(norms.max()), "checkpoint_sha256": expected_ckpt, "contract_sha256": sha(contract_path), "text_features_sha256": sha(npz), "rows_sha256": sha(rows_path), "image_feature_manifest_sha256": sha(args.image_manifest), "runtime_s": time.perf_counter() - started, "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, "device": "cpu", "model_downloaded": False, "detector_or_image_encoder_rerun": False, "prompt_cache_reused": False, "prompt_cache_reason": "existing U4-OV cache uses prompt templates; raw-class contract required"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
