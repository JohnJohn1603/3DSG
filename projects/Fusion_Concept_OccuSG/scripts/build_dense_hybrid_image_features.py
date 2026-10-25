#!/usr/bin/env python3
"""Build a complete dense-cache image-feature sidecar from cached RGB/P2 only.

Existing compatible features are referenced without copying. Missing vectors are
encoded with the already pinned local OpenCLIP ViT-B/32 checkpoint. No detector,
capture, P3 or mapping stage is invoked.
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
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import extract_u2_appearance as u2  # noqa: E402
from integration_v2.capture_compatibility import image_feature_source_matches, validate_capture_p2_pair  # noqa: E402


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def crop(rgb: np.ndarray, bbox):
    image = Image.fromarray(rgb[..., :3], mode="RGB")
    x0, y0, x1, y1 = [float(x) for x in bbox]
    w, h = image.size
    if not (0 <= x0 <= x1 <= w and 0 <= y0 <= y1 <= h):
        raise ValueError(f"invalid bbox {bbox}")
    pad = 20.0
    left, top = min(pad, x0), min(pad, y0)
    right, bottom = min(pad, w - x1), min(pad, h - y1)
    box = (x0 - left, y0 - top, x1 + right, y1 + bottom)
    return image.crop(box), [float(v) for v in box]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--existing-manifest", type=Path, required=True)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--checkpoint-report", type=Path, required=True)
    args = ap.parse_args()
    started = time.perf_counter()
    cache = args.cache.resolve(); output = args.output.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    (output / "features").mkdir()
    p2_root = cache / "detection_2d"
    capture_root = cache / "capture"
    p2_path = p2_root / "detection_2d_index.json"
    cap_path = capture_root / "capture_manifest.json"
    p2 = json.loads(p2_path.read_text()); cap = json.loads(cap_path.read_text())
    sensor_contract_hash = validate_capture_p2_pair(capture_root, p2_root, cap, p2)
    if not sensor_contract_hash:
        raise RuntimeError("capture lacks a valid sensor/calibration contract; image feature cache reuse is blocked")
    cap_by_id = {str(r["frame_id"]): r for r in cap["frames"]}
    detections = []
    for frame in sorted(p2["frames"], key=lambda x: int(x["frame_index"])):
        capture_frame = cap_by_id.get(str(frame["frame_id"]))
        if capture_frame is None:
            raise RuntimeError(f"P2 frame is absent from capture manifest: {frame['frame_id']}")
        rgb_path = capture_root / capture_frame["frame_dir"] / "rgb.npy"
        if not rgb_path.is_file():
            raise FileNotFoundError(rgb_path)
        rgb_sha = sha(rgb_path)
        if (rgb_sha != frame.get("rgb_input_sha256")
                or rgb_sha != capture_frame.get("raw_files_sha256", {}).get("rgb.npy")):
            raise RuntimeError(f"P2 RGB source hash differs from capture: {frame['frame_id']}")
        for det in frame.get("detections", []):
            detections.append({"observation_id": str(det["detection_id"]), "frame_id": str(frame["frame_id"]),
                               "frame_index": int(frame["frame_index"]), "bbox": det["bbox_xyxy"],
                               "mask_sha256": det["mask_sha256"], "rgb_input_sha256": rgb_sha,
                               "capture_sensor_contract_sha256": sensor_contract_hash})
    existing_rows = []
    for line in args.existing_manifest.read_text().splitlines():
        if line.strip():
            row = json.loads(line); row["observation_id"] = str(row.get("observation_id", row.get("detection_id"))); existing_rows.append(row)
    existing = {r["observation_id"]: r for r in existing_rows}
    meta = json.loads(args.checkpoint_report.read_text())
    expected_ckpt = str(meta["checkpoint_sha256"])
    if sha(args.checkpoint) != expected_ckpt:
        raise RuntimeError("checkpoint SHA does not match existing ViT-B/32 feature provenance")
    expected_fp = str(meta["model_fingerprint"]); expected_pre = str(meta["preprocessing_id"])
    rows = []
    invalid_existing = []
    for d in detections:
        old = existing.get(d["observation_id"])
        if old is None:
            continue
        ref = old.get("feature_ref") or old.get("feature_path")
        path = ROOT / ref if ref and not Path(ref).is_absolute() else Path(ref)
        try:
            with np.load(path, allow_pickle=False) as z:
                vec = np.asarray(z["image_feature"], dtype=np.float32).reshape(-1)
            ok = path.is_file() and sha(path) == old.get("feature_sha256", old.get("feature_file_sha256"))
            ok = ok and vec.shape == (512,) and np.isfinite(vec).all() and np.isclose(float(np.linalg.norm(vec)), 1.0, atol=1e-5, rtol=1e-5)
            expected_source = {**d, "checkpoint_sha256": expected_ckpt,
                               "model_fingerprint": expected_fp, "preprocessing_id": expected_pre,
                               "feature_shape": [512]}
            ok = ok and image_feature_source_matches(old, expected_source)
        except Exception:
            ok = False
        if not ok:
            invalid_existing.append(d["observation_id"])
            continue
        rows.append({"observation_id": d["observation_id"], "frame_id": d["frame_id"], "frame_index": d["frame_index"],
                     "feature_path": str(path.resolve().relative_to(ROOT)), "feature_file_sha256": sha(path),
                     "mask_sha256": d["mask_sha256"], "checkpoint_sha256": expected_ckpt,
                     "model_fingerprint": expected_fp, "preprocessing_id": expected_pre,
                     "feature_shape": [512], "device": "cpu", "rgb_input_sha256": d["rgb_input_sha256"],
                     "capture_sensor_contract_sha256": sensor_contract_hash,
                     "source": "reused_compatible_existing_sidecar"})
    if invalid_existing:
        raise RuntimeError(f"existing features failed compatibility checks: {invalid_existing[:5]} ({len(invalid_existing)} total)")
    by_id = {r["observation_id"]: r for r in rows}
    missing = [d for d in detections if d["observation_id"] not in by_id]
    print(json.dumps({"p2_observations": len(detections), "reused": len(rows), "missing_to_encode": len(missing), "checkpoint": str(args.checkpoint)}, ensure_ascii=False), flush=True)

    model = preprocess = torch = None
    if missing:
        model, preprocess, torch = u2.load_model(args.checkpoint)
        model.eval()
        for i, d in enumerate(missing, 1):
            frame = cap_by_id[d["frame_id"]]
            rgb_path = capture_root / frame["frame_dir"] / "rgb.npy"
            rgb = np.asarray(np.load(rgb_path, allow_pickle=False))
            if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] < 3:
                raise RuntimeError(f"invalid RGB schema: {d['observation_id']}")
            image, crop_box = crop(rgb, d["bbox"])
            tensor = preprocess(image).unsqueeze(0)
            with torch.inference_mode():
                raw = model.encode_image(tensor)
            vec = raw.detach().cpu().numpy().astype(np.float32).reshape(-1)
            norm = float(np.linalg.norm(vec))
            if vec.shape != (512,) or not np.isfinite(vec).all() or norm <= 0:
                raise RuntimeError(f"invalid output feature: {d['observation_id']}")
            vec = (vec / np.float32(norm)).astype(np.float32)
            name = d["observation_id"].replace(":", "__") + "__" + hashlib.sha256(vec.tobytes()).hexdigest()[:16] + ".npz"
            path = output / "features" / name
            np.savez_compressed(path, image_feature=vec)
            row = {"observation_id": d["observation_id"], "frame_id": d["frame_id"], "frame_index": d["frame_index"],
                   "feature_path": str(path.resolve().relative_to(ROOT)), "feature_file_sha256": sha(path),
                   "mask_sha256": d["mask_sha256"], "checkpoint_sha256": expected_ckpt,
                   "model_fingerprint": expected_fp, "preprocessing_id": expected_pre,
                   "feature_shape": [512], "device": "cpu", "rgb_input_sha256": d["rgb_input_sha256"],
                   "capture_sensor_contract_sha256": sensor_contract_hash,
                   "crop_box_xyxy_float": crop_box, "source": "new_cached_rgb_p2_only"}
            by_id[d["observation_id"]] = row
            if i == 1 or i % 50 == 0 or i == len(missing):
                (output / "progress.json").write_text(json.dumps({"status": "running", "completed_new": i, "total_new": len(missing), "last_observation_id": d["observation_id"], "elapsed_s": time.perf_counter() - started}, indent=2) + "\n")
                print(json.dumps({"stage": "image_features", "completed_new": i, "total_new": len(missing), "elapsed_s": round(time.perf_counter() - started, 2)}, ensure_ascii=False), flush=True)
    rows = [by_id[d["observation_id"]] for d in detections]
    if len(rows) != len(detections) or len({r["observation_id"] for r in rows}) != len(rows):
        raise RuntimeError("image sidecar coverage/uniqueness failed")
    manifest_path = output / "feature_manifest.jsonl"
    manifest_path.write_text("".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in rows))
    report = {"schema": "conceptgraphs-image-feature-sidecar/v2", "status": "pass", "observation_count": len(rows),
              "reused_count": len(existing) if existing else 0, "new_count": len(missing), "feature_dimension": 512,
              "checkpoint_sha256": expected_ckpt, "model_fingerprint": expected_fp, "preprocessing_id": expected_pre,
              "p2_index_sha256": sha(p2_path), "capture_manifest_sha256": sha(cap_path),
              "capture_sensor_contract_sha256": sensor_contract_hash,
              "manifest_sha256": sha(manifest_path), "runtime_s": time.perf_counter() - started,
              "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
              "detector_rerun": False, "p3_rerun": False, "native_mapping_rerun": False, "normalization_tolerance": 1e-5}
    (output / "feature_extraction_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (output / "progress.json").write_text(json.dumps({"status": "complete", **report}, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
