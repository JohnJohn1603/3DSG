#!/usr/bin/env python3
"""Extract locked U2 image embeddings from already saved P3-accepted RGB crops."""
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


def canon(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    run, out = args.run_dir.resolve(), args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    lock_path = ROOT / "artifacts/audits/u4_ov/u4_ov_config.locked.json"
    lock = json.loads(lock_path.read_text())
    model_dir = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878"
    metadata = json.loads((model_dir / "checkpoint_metadata.json").read_text())
    checkpoint = model_dir / metadata["filename"]
    if sha(checkpoint) != lock["model"]["checkpoint_sha256"]:
        raise RuntimeError("U2 checkpoint hash differs from locked U4-OV checkpoint")
    cap = json.loads((run / "capture/capture_manifest.json").read_text())
    p2 = json.loads((run / "detection_2d/detection_2d_index.json").read_text())
    if p2.get("capture_manifest_sha256") != sha(run / "capture/capture_manifest.json"):
        raise RuntimeError("P2 cache does not match capture manifest")
    accepted = {d["detection_id"] for batch in
        (json.loads(line) for line in (run / "detection_3d/detection_3d.jsonl").read_text().splitlines() if line)
        for d in batch.get("detections", [])}
    cap_by_id = {x["frame_id"]: x for x in cap["frames"]}
    records = []
    for frame in p2["frames"]:
        for det in frame["detections"]:
            did = det["detection_id"]
            if did not in accepted:
                continue
            rgb_path = run / "capture" / cap_by_id[frame["frame_id"]]["frame_dir"] / "rgb.npy"
            mask_path = run / "detection_2d" / det["mask_file"]
            if sha(mask_path) != det["mask_sha256"]:
                raise RuntimeError(f"mask hash mismatch: {did}")
            records.append({"observation_id": did, "frame_id": frame["frame_id"], "detection_id": did,
                "rgb": np.asarray(np.load(rgb_path, allow_pickle=False)), "rgb_sha256": sha(rgb_path),
                "mask_path": mask_path, "mask_sha256": det["mask_sha256"],
                "bbox_xyxy": [float(x) for x in det["bbox_xyxy"]], "frame_index": int(frame["frame_index"])})
    if len(records) != len(accepted):
        raise RuntimeError("accepted P3 set does not match cached P2 crops")
    crop_records = u2.prepare_crop_records(records, out / "crops")
    model, preprocess, torch = u2.load_model(checkpoint)
    preprocess_fp = canon({"id": u2.PREPROCESSING_ID, "actual_transform": u2.preprocessing_description(preprocess),
                           "bbox_padding_px": u2.PADDING_PX})
    t_inference = time.perf_counter()
    manifest = []
    for row in crop_records:
        did = row["observation_id"]
        key = canon({"rgb_sha256": row["rgb_sha256"], "mask_sha256": row["mask_sha256"],
            "crop_sha256": row["crop_sha256"], "checkpoint_sha256": lock["model"]["checkpoint_sha256"],
            "preprocessing_fingerprint": preprocess_fp})
        vector = u2.encode_crop(model, preprocess, torch, row["crop_rgb"])
        feature_path = out / "features" / f"{key[:24]}.npz"
        feature_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(feature_path, image_feature=vector)
        manifest.append({"observation_id": did, "frame_id": row["frame_id"], "frame_index": row["frame_index"],
            "rgb_sha256": row["rgb_sha256"], "mask_sha256": row["mask_sha256"], "crop_sha256": row["crop_sha256"],
            "crop_path": str(row["crop_path"].relative_to(ROOT)), "crop_box_xyxy_float": row["crop_box_xyxy_float"],
            "cache_key_sha256": key, "feature_path": str(feature_path.relative_to(ROOT)),
            "feature_file_sha256": sha(feature_path), "feature_shape": [512], "dtype": "float32",
            "finite": bool(np.isfinite(vector).all()), "l2_norm": float(np.linalg.norm(vector)),
            "checkpoint_sha256": lock["model"]["checkpoint_sha256"], "preprocessing_fingerprint": preprocess_fp})
    infer_s = time.perf_counter() - t_inference
    (out / "feature_manifest.jsonl").write_text("".join(json.dumps(row,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n" for row in manifest))
    report={"status":"pass","run_dir":str(run.relative_to(ROOT)),"observation_count":len(manifest),
        "checkpoint_sha256":lock["model"]["checkpoint_sha256"],"u4ov_lock_sha256":sha(lock_path),
        "preprocessing_fingerprint":preprocess_fp,"feature_dimension":512,"all_finite":all(r["finite"] for r in manifest),
        "normalization_tolerance":{"atol":1e-5,"rtol":1e-5},"all_normalized":all(abs(r["l2_norm"]-1)<1e-5 for r in manifest),
        "runtime_inference_seconds":infer_s,"runtime_total_seconds":time.perf_counter()-started,
        "peak_process_rss_mib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024.0,
        "input_p2_hash":sha(run/"detection_2d/detection_2d_index.json"),"input_p3_hash":sha(run/"detection_3d/detection_3d.jsonl")}
    (out/"feature_report.json").write_text(json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n")
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
