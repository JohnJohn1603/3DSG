#!/usr/bin/env python3
"""Run the locked P1 instance-segmentation model on a raw RGB capture.

This is an isolated P2 provider.  It writes Detection2D cache artifacts and
never injects scene-instance metadata, semantic IDs or room annotations into
the graph pipeline.  The native replay consumes this cache only through the
explicit detection-cache option.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def sigmoid(value: np.ndarray) -> np.ndarray:
    value = np.clip(value, -80.0, 80.0)
    return 1.0 / (1.0 + np.exp(-value))


def letterbox(rgb: np.ndarray, size: int) -> tuple[np.ndarray, float, int, int, int, int]:
    h, w = rgb.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = np.asarray(Image.fromarray(rgb, mode="RGB").resize((nw, nh), Image.Resampling.BILINEAR), dtype=np.uint8)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y, nw, nh


def iou_xyxy(a: np.ndarray, b: np.ndarray) -> float:
    x0, y0 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1]))
    x1, y1 = min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_a = max(0.0, float(a[2] - a[0])) * max(0.0, float(a[3] - a[1]))
    area_b = max(0.0, float(b[2] - b[0])) * max(0.0, float(b[3] - b[1]))
    return inter / max(area_a + area_b - inter, 1e-12)


def nms_classwise(boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, iou_threshold: float) -> list[int]:
    kept: list[int] = []
    for cls in sorted(set(int(v) for v in classes.tolist())):
        indices = [int(i) for i in np.flatnonzero(classes == cls)]
        indices.sort(key=lambda i: (-float(scores[i]), i))
        while indices:
            current = indices.pop(0)
            kept.append(current)
            indices = [i for i in indices if iou_xyxy(boxes[current], boxes[i]) <= iou_threshold]
    return kept


def unletterbox_mask(mask_640: np.ndarray, *, height: int, width: int, scale: float,
                     pad_x: int, pad_y: int, resized_width: int, resized_height: int) -> np.ndarray:
    cropped = mask_640[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width]
    restored = np.asarray(Image.fromarray((cropped.astype(np.uint8) * 255), mode="L").resize((width, height), Image.Resampling.NEAREST))
    return restored > 127


def decode_frame(session, rgb: np.ndarray, *, names: list[str], preprocess: dict, frame_id: str) -> list[dict]:
    if rgb.ndim != 3 or rgb.shape[2] not in (3, 4) or rgb.dtype != np.uint8:
        raise ValueError(f"{frame_id}: RGB must be uint8 HxWx3/4")
    rgb = np.ascontiguousarray(rgb[..., :3])
    h, w = rgb.shape[:2]
    input_size = int(preprocess["input_size"][0])
    image, scale, pad_x, pad_y, nw, nh = letterbox(rgb, input_size)
    tensor = np.ascontiguousarray(image.transpose(2, 0, 1)[None].astype(np.float32) / 255.0)
    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: tensor})
    if len(outputs) != 2:
        raise RuntimeError(f"{frame_id}: expected YOLO segmentation outputs, got {len(outputs)}")
    predictions, prototypes = np.asarray(outputs[0])[0].T, np.asarray(outputs[1])[0]
    if predictions.shape[1] < 5 or prototypes.ndim != 3:
        raise RuntimeError(f"{frame_id}: unsupported output shapes {predictions.shape}, {prototypes.shape}")
    class_count = len(names)
    coeff_count = int(prototypes.shape[0])
    if predictions.shape[1] != 4 + class_count + coeff_count:
        raise RuntimeError(
            f"{frame_id}: output width {predictions.shape[1]} does not match 4+{class_count}+{coeff_count}"
        )
    class_scores = predictions[:, 4:4 + class_count]
    classes = np.argmax(class_scores, axis=1).astype(np.int64)
    scores = class_scores[np.arange(len(predictions)), classes]
    confidence = float(preprocess["confidence_threshold"])
    candidates = np.flatnonzero(scores >= confidence)
    if not len(candidates):
        return []
    raw_boxes = predictions[candidates, :4]
    boxes = np.empty_like(raw_boxes, dtype=np.float64)
    boxes[:, 0] = raw_boxes[:, 0] - raw_boxes[:, 2] / 2.0
    boxes[:, 1] = raw_boxes[:, 1] - raw_boxes[:, 3] / 2.0
    boxes[:, 2] = raw_boxes[:, 0] + raw_boxes[:, 2] / 2.0
    boxes[:, 3] = raw_boxes[:, 1] + raw_boxes[:, 3] / 2.0
    selected_classes, selected_scores = classes[candidates], scores[candidates]
    kept_local = nms_classwise(boxes, selected_scores, selected_classes, float(preprocess["nms_iou_threshold"]))
    decoded: list[dict] = []
    for local_index in kept_local:
        box = boxes[local_index]
        box_original = np.array([
            (box[0] - pad_x) / scale, (box[1] - pad_y) / scale,
            (box[2] - pad_x) / scale, (box[3] - pad_y) / scale,
        ], dtype=np.float64)
        box_original[[0, 2]] = np.clip(box_original[[0, 2]], 0.0, w)
        box_original[[1, 3]] = np.clip(box_original[[1, 3]], 0.0, h)
        if box_original[2] <= box_original[0] or box_original[3] <= box_original[1]:
            continue
        coeff = predictions[candidates[local_index], 4 + class_count:]
        mask_small = sigmoid(coeff @ prototypes.reshape(coeff_count, -1)).reshape(prototypes.shape[1:])
        mask_input = np.asarray(Image.fromarray((mask_small * 255.0).astype(np.uint8), mode="L").resize((input_size, input_size), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
        # Crop in letterboxed coordinates, matching the source mask/bbox contract.
        x0, y0, x1, y1 = [int(round(v)) for v in box]
        mask_input[:max(0, y0)] = 0.0
        mask_input[min(input_size, y1):] = 0.0
        mask_input[:, :max(0, x0)] = 0.0
        mask_input[:, min(input_size, x1):] = 0.0
        mask = unletterbox_mask(mask_input >= float(preprocess["mask_threshold"]), height=h, width=w,
                                scale=scale, pad_x=pad_x, pad_y=pad_y,
                                resized_width=nw, resized_height=nh)
        decoded.append({
            "class_id": int(selected_classes[local_index]),
            "class_raw": names[int(selected_classes[local_index])],
            "score": float(selected_scores[local_index]),
            "bbox_xyxy": [float(v) for v in box_original],
            "mask": mask,
        })
    decoded.sort(key=lambda d: (d["class_id"], -d["score"], *d["bbox_xyxy"]))
    return decoded


def canonical_class(raw: str, mapping: dict[str, str]) -> str:
    return mapping.get(raw, raw.replace(" ", "_"))


def draw_overlay(rgb: np.ndarray, detections: list[dict], out: Path) -> None:
    image = Image.fromarray(rgb[..., :3], mode="RGB").convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for det in detections:
        color = (255, 80, 40, 100)
        mask = Image.fromarray((det["mask"].astype(np.uint8) * 90), mode="L")
        solid = Image.new("RGBA", image.size, color)
        overlay.alpha_composite(Image.composite(solid, Image.new("RGBA", image.size), mask))
        x0, y0, x1, y1 = det["bbox_xyxy"]
        draw.rectangle((x0, y0, x1, y1), outline=(255, 255, 0, 255), width=1)
        draw.text((x0, max(0, y0 - 10)), f'{det["class_raw"]} {det["score"]:.2f}', fill=(255, 255, 0, 255))
    image.alpha_composite(overlay)
    image.convert("RGB").save(out)


def run(capture: Path, output: Path, config: Path, weights: Path, names_path: Path) -> dict:
    import onnxruntime as ort

    profile = json.loads(config.read_text(encoding="utf-8"))
    model_cfg, runtime, preprocess_cfg = profile["model"], profile["runtime"], profile["preprocessing"]
    if Path(model_cfg["weights"]).resolve() != weights.resolve():
        raise ValueError("--weights must match model.weights in the locked P1 profile")
    if sha256(weights) != model_cfg["weights_sha256"]:
        raise ValueError("weights SHA-256 does not match the locked P1 profile")
    names = [line.strip() for line in names_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(names) != 80:
        raise ValueError("COCO vocabulary must contain exactly 80 non-empty names")
    capture_manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    records = capture_manifest.get("frames", [])
    if not records:
        raise ValueError("capture has no frame records")
    output.mkdir(parents=True, exist_ok=True)
    masks_dir, overlays_dir = output / "masks", output / "overlays"
    masks_dir.mkdir(exist_ok=True)
    overlays_dir.mkdir(exist_ok=True)
    session = ort.InferenceSession(str(weights), providers=["CPUExecutionProvider"])
    frame_entries: list[dict] = []
    total = 0
    frame_errors: list[dict] = []
    synonym_map = profile["vocabulary"].get("canonical_map", {})
    preprocess_fingerprint = json_hash({"profile": profile["profile_id"], "preprocessing": preprocess_cfg, "vocabulary": profile["vocabulary"]})
    for record in records:
        frame_dir = (capture / record["frame_dir"]).resolve()
        rgb_path = frame_dir / "rgb.npy"
        if not rgb_path.is_file():
            frame_errors.append({"frame_id": record["frame_id"], "error": "rgb.npy missing"})
            continue
        rgb = np.load(rgb_path, allow_pickle=False)
        try:
            detections = decode_frame(session, rgb, names=names, preprocess=runtime | preprocess_cfg, frame_id=str(record["frame_id"]))
        except Exception as exc:
            frame_errors.append({"frame_id": record["frame_id"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        frame_id = str(record["frame_id"])
        frame_detections = []
        for det_index, det in enumerate(detections):
            detection_id = f"{frame_id}:det-{det_index:04d}"
            mask_path = masks_dir / f"{frame_id}__{det_index:04d}.npy"
            np.save(mask_path, det["mask"].astype(np.bool_), allow_pickle=False)
            frame_detections.append({
                "detection_id": detection_id,
                "class_id": det["class_id"],
                "class_raw": det["class_raw"],
                "class_canonical": canonical_class(det["class_raw"], synonym_map),
                "score_raw": det["score"],
                "bbox_xyxy": det["bbox_xyxy"],
                "mask_file": str(mask_path.relative_to(output)),
                "mask_sha256": sha256(mask_path),
                "image_shape_hw": [int(rgb.shape[0]), int(rgb.shape[1])],
                "model_fingerprint": {"weights_sha256": model_cfg["weights_sha256"], "profile_id": profile["profile_id"]},
                "preprocess_fingerprint": preprocess_fingerprint,
                "preprocess": {"color_order": "RGB", "input_size": [int(runtime["input_size"][0]), int(runtime["input_size"][1])],
                               "letterbox_scale": None, "mask_space": "original_rgb_pixels"},
            })
        draw_overlay(rgb, [{**d, "class_raw": d["class_raw"]} for d in detections], overlays_dir / f"{frame_id}.png")
        frame_entries.append({
            "scene_id": capture_manifest.get("scene_id"), "sequence_id": capture_manifest.get("sequence_id"),
            "frame_id": frame_id, "frame_index": int(record["frame_index"]), "timestamp_s": float(record["timestamp_s"]),
            "image_shape_hw": [int(rgb.shape[0]), int(rgb.shape[1])], "detections": frame_detections,
            "status": "present" if frame_detections else "empty",
        })
        total += len(frame_detections)
        print(f"PROGRESS: frame {int(record['frame_index']) + 1}/{len(records)} detected", flush=True)
    cache_manifest = {
        "schema_version": 1, "kind": "detection_2d_cache",
        "capture_manifest_sha256": sha256(capture / "capture_manifest.json"),
        "capture": str(capture), "config": str(config), "config_sha256": sha256(config),
        "weights": str(weights), "weights_sha256": sha256(weights), "runtime": runtime,
        "preprocessing": preprocess_cfg, "vocabulary": profile["vocabulary"],
        "frame_count": len(frame_entries), "detection_count": total, "frame_errors": frame_errors,
        "frames": frame_entries, "cache_authoritative": True,
        "semantic_gt_used": False, "room_gt_used": False, "navmesh_used": False,
        "status": "error" if frame_errors else "pass",
    }
    (output / "detection_2d_index.json").write_text(json.dumps(cache_manifest, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    if frame_errors:
        raise RuntimeError(f"P2 detector failed on {len(frame_errors)} frame(s); see detection_2d_index.json")
    return cache_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json")
    parser.add_argument("--weights", type=Path, default=ROOT / "artifacts/models/ultralytics/yolo11n-seg.onnx")
    parser.add_argument("--names", type=Path, default=ROOT / "configs/perception/coco.names")
    args = parser.parse_args()
    print(json.dumps(run(args.capture.resolve(), args.output.resolve(), args.config.resolve(), args.weights.resolve(), args.names.resolve()), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
