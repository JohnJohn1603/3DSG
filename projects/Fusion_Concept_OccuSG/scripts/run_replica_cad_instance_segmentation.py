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
import resource
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.resource_binding import load_resource_lock, resolve_external_file  # noqa: E402
from integration_v2.perception_session import PerceptionSession  # noqa: E402


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


class P2DetectorSession:
    """Keep one ONNX detector session while accepting current frames only."""

    def __init__(self, *, ort_session, names: list[str], preprocess: dict):
        self._ort_session = ort_session
        self._names = list(names)
        self._preprocess = dict(preprocess)
        self.model_load_count = 1
        self._lifecycle = PerceptionSession.open(
            process_frame=self._process_frame,
        )

    @classmethod
    def open(cls, *, weights: Path, names: list[str], preprocess: dict,
             providers: list[str] | None = None) -> "P2DetectorSession":
        import onnxruntime as ort
        return cls(
            ort_session=ort.InferenceSession(
                str(weights), providers=providers or ["CPUExecutionProvider"]
            ),
            names=names, preprocess=preprocess,
        )

    def _process_frame(self, packet: dict) -> list[dict]:
        rgb = packet.get("rgb")
        return decode_frame(
            self._ort_session, rgb, names=self._names,
            preprocess=self._preprocess, frame_id=str(packet["frame_id"]),
        )

    def update_frame(self, packet: dict) -> list[dict]:
        return self._lifecycle.update_frame(packet)

    def finalize(self) -> None:
        self._lifecycle.finalize()

    def close(self) -> None:
        self._lifecycle.close()
        self._ort_session = None

    @property
    def processed_frame_ids(self) -> tuple[str, ...]:
        return self._lifecycle.processed_frame_ids

    @property
    def status(self) -> str:
        return self._lifecycle.status


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


def run(capture: Path, output: Path, config: Path, weights: Path, names_path: Path,
        resource_manifest: Path = ROOT / "locks/external_resources.json",
        frame_start: int = 0, frame_count: int | None = None,
        subset_manifest: Path | None = None) -> dict:
    import onnxruntime as ort
    from integration_v2.capture_compatibility import canonical_sha256, p2_cache_compatibility
    run_started = time.perf_counter()

    profile = json.loads(config.read_text(encoding="utf-8"))
    model_cfg, runtime, preprocess_cfg = profile["model"], profile["runtime"], profile["preprocessing"]
    resource_lock = load_resource_lock(resource_manifest.resolve())
    bound_weights = resolve_external_file("yolo_onnx", required=True, lock_path=resource_manifest.resolve())
    declared_weights = Path(model_cfg["weights"])
    expected_weights = (declared_weights if declared_weights.is_absolute()
                        else Path(resource_lock["baseline_root"]) / declared_weights)
    if expected_weights.resolve() != bound_weights.resolve() or weights.resolve() != bound_weights.resolve():
        raise ValueError("--weights must match model.weights in the locked P1 profile")
    if sha256(weights) != model_cfg["weights_sha256"]:
        raise ValueError("weights SHA-256 does not match the locked P1 profile")
    names = [line.strip() for line in names_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(names) != 80:
        raise ValueError("COCO vocabulary must contain exactly 80 non-empty names")
    capture_manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    all_records = capture_manifest.get("frames", [])
    if not all_records:
        raise ValueError("capture has no frame records")
    if type(frame_start) is not int or frame_start < 0:
        raise ValueError("frame_start must be a non-negative integer")
    selected = all_records[frame_start:] if frame_count is None else all_records[frame_start:frame_start + int(frame_count)]
    if not selected:
        raise ValueError("frame selection is empty")
    if frame_count is not None and (type(frame_count) is not int or frame_count < 1 or len(selected) != frame_count):
        raise ValueError("frame_count must select at least one frame within capture")
    records = list(selected)
    subset_manifest_sha256 = None
    if subset_manifest is not None:
        subset_manifest = subset_manifest.resolve()
        lock = json.loads(subset_manifest.read_text(encoding="utf-8"))
        subset_manifest_sha256 = sha256(subset_manifest)
        expected_ids = [str(row["frame_id"]) for row in records]
        if lock.get("schema") != "perception-smoke-subset/v1":
            raise ValueError("unsupported perception smoke subset manifest schema")
        if lock.get("capture_manifest_sha256") != sha256(capture / "capture_manifest.json"):
            raise ValueError("subset manifest capture hash mismatch")
        if lock.get("config_sha256") != sha256(config) or lock.get("weights_sha256") != sha256(weights):
            raise ValueError("subset manifest model/config hash mismatch")
        if lock.get("frame_ids") != expected_ids:
            raise ValueError("subset manifest frame selection mismatch")
    output.mkdir(parents=True, exist_ok=True)
    masks_dir, overlays_dir = output / "masks", output / "overlays"
    masks_dir.mkdir(exist_ok=True)
    overlays_dir.mkdir(exist_ok=True)
    model_load_started = time.perf_counter()
    detector_session = P2DetectorSession.open(
        weights=weights, names=names, preprocess=runtime | preprocess_cfg,
        providers=["CPUExecutionProvider"],
    )
    model_load_s = time.perf_counter() - model_load_started
    frame_entries: list[dict] = []
    total = 0
    frame_errors: list[dict] = []
    frame_timings: list[dict] = []
    synonym_map = profile["vocabulary"].get("canonical_map", {})
    sensor_contract_hash = capture_manifest.get("capture_sensor_contract_sha256")
    sensor_contract_doc = capture_manifest.get("capture_sensor_contract")
    if sensor_contract_doc is not None and canonical_sha256(sensor_contract_doc) != sensor_contract_hash:
        raise ValueError("capture sensor/calibration contract hash mismatch")
    if sensor_contract_doc is not None and list(sensor_contract_doc.get("resolution_px", [])) != list(capture_manifest.get("resolution_px", [])):
        raise ValueError("capture sensor contract resolution differs from capture manifest")
    preprocess_fingerprint = json_hash({"profile": profile["profile_id"], "runtime": runtime,
                                        "preprocessing": preprocess_cfg, "vocabulary": profile["vocabulary"]})
    rgb_input_hashes: dict[str, str] = {}
    try:
        for record in records:
            frame_dir = (capture / record["frame_dir"]).resolve()
            for name, expected_hash in record.get("raw_files_sha256", {}).items():
                raw_path = frame_dir / str(name)
                if not raw_path.is_file() or sha256(raw_path) != expected_hash:
                    raise ValueError(f"capture source file missing/hash mismatch: {record['frame_id']}:{name}")
            rgb_path = frame_dir / "rgb.npy"
            if not rgb_path.is_file():
                frame_errors.append({"frame_id": record["frame_id"], "error": "rgb.npy missing"})
                continue
            rgb_sha = sha256(rgb_path)
            declared_raw = record.get("raw_files_sha256", {}).get("rgb.npy")
            if declared_raw and declared_raw != rgb_sha:
                raise ValueError(f"capture RGB hash mismatch: {record['frame_id']}")
            rgb_input_hashes[str(record["frame_id"])] = rgb_sha
            rgb = np.load(rgb_path, allow_pickle=False)
            depth_path = frame_dir / "depth_m.npy"
            valid_path = frame_dir / "valid_mask.npy"
            metadata_path = frame_dir / "metadata.json"
            depth = np.load(depth_path, allow_pickle=False)
            valid_mask = np.load(valid_path, allow_pickle=False)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            try:
                frame_started = time.perf_counter()
                detections = detector_session.update_frame({
                    "frame_index": int(record["frame_index"]),
                    "frame_id": str(record["frame_id"]),
                    "timestamp_s": float(record["timestamp_s"]),
                    "rgb": rgb,
                    "rgbd": {"depth_m": depth, "valid_mask": valid_mask,
                             "depth_units": metadata.get("depth_units"),
                             "depth_representation": metadata.get("depth_representation")},
                    "K": metadata.get("intrinsics"),
                    "intrinsics": metadata.get("intrinsics"),
                    "pose": metadata.get("base_pose_canonical"),
                    "source_hashes": {
                        "rgb.npy": rgb_sha,
                        "depth_m.npy": record.get("raw_files_sha256", {}).get("depth_m.npy"),
                        "valid_mask.npy": record.get("raw_files_sha256", {}).get("valid_mask.npy"),
                        "metadata.json": record.get("raw_files_sha256", {}).get("metadata.json"),
                        "capture_sensor_contract_sha256": sensor_contract_hash,
                    },
                })
                frame_runtime_s = time.perf_counter() - frame_started
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
                    "capture_sensor_contract_sha256": sensor_contract_hash,
                    "preprocess": {"color_order": "RGB", "input_size": [int(runtime["input_size"][0]), int(runtime["input_size"][1])],
                                   "letterbox_scale": None, "mask_space": "original_rgb_pixels"},
                })
            draw_overlay(rgb, [{**d, "class_raw": d["class_raw"]} for d in detections], overlays_dir / f"{frame_id}.png")
            frame_entries.append({
                "scene_id": capture_manifest.get("scene_id"), "sequence_id": capture_manifest.get("sequence_id"),
                "frame_id": frame_id, "frame_index": int(record["frame_index"]), "timestamp_s": float(record["timestamp_s"]),
                "image_shape_hw": [int(rgb.shape[0]), int(rgb.shape[1])], "detections": frame_detections,
                "rgb_input_sha256": rgb_sha,
                "capture_sensor_contract_sha256": sensor_contract_hash,
                "status": "present" if frame_detections else "empty",
            })
            total += len(frame_detections)
            frame_timings.append({"frame_id": frame_id, "frame_index": int(record["frame_index"]),
                                  "runtime_s": frame_runtime_s, "detection_count": len(frame_detections)})
            print(f"PROGRESS: frame {int(record['frame_index']) + 1}/{len(records)} detected", flush=True)
        detector_session.finalize()
    except BaseException:
        detector_session.close()
        raise
    detector_session.close()
    cache_compatibility = p2_cache_compatibility(
        capture_manifest_sha256=sha256(capture / "capture_manifest.json"),
        capture_sensor_contract_sha256=sensor_contract_hash,
        resolution_px=list(capture_manifest["resolution_px"]), rgb_input_sha256_by_frame=rgb_input_hashes,
        detector_config_sha256=sha256(config), weights_sha256=sha256(weights),
        runtime=runtime, preprocessing=preprocess_cfg, vocabulary=profile["vocabulary"])
    cache_manifest = {
        "schema_version": 1, "kind": "detection_2d_cache",
        "capture_manifest_sha256": sha256(capture / "capture_manifest.json"),
        "capture_sensor_contract_sha256": sensor_contract_hash,
        "cache_compatibility": cache_compatibility,
        "cache_compatibility_sha256": canonical_sha256(cache_compatibility),
        "capture": str(capture), "config": str(config), "config_sha256": sha256(config),
        "weights": str(weights), "weights_sha256": sha256(weights), "runtime": runtime,
        "preprocessing": preprocess_cfg, "vocabulary": profile["vocabulary"],
        "frame_count": len(frame_entries), "full_capture_frame_count": len(all_records),
        "selected_frame_start": frame_start, "selected_frame_indices": [int(r["frame_index"]) for r in records],
        "selected_frame_ids": [str(r["frame_id"]) for r in records],
        "subset_manifest": None if subset_manifest is None else str(subset_manifest),
        "subset_manifest_sha256": subset_manifest_sha256,
        "detection_count": total, "frame_errors": frame_errors,
        "timing": {
            "model_load_s": model_load_s,
            "frame_timings": frame_timings,
            "runtime_s": time.perf_counter() - run_started,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        },
        "frames": frame_entries, "cache_authoritative": True,
        "semantic_gt_used": False, "room_gt_used": False, "navmesh_used": False,
        "status": "error" if frame_errors else "pass",
        "perception_session": {
            "api": "P2DetectorSession.open/update_frame/finalize/close via PerceptionSession",
            "model_load_count": detector_session.model_load_count,
            "processed_frame_count": len(detector_session.processed_frame_ids),
            "empty_detections_valid": True,
            "future_manifest_required": False,
            "p3_session": "not_covered_in_t2_b_smoke",
            "image_feature_session": "not_covered_in_t2_b_smoke",
            "text_feature_session": "not_covered_in_t2_b_smoke",
        },
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
    parser.add_argument("--resource-manifest", type=Path, default=ROOT / "locks/external_resources.json",
                        help="explicit input-only resource lock for the P1 model checkpoint")
    parser.add_argument("--frame-start", type=int, default=0)
    parser.add_argument("--frame-count", type=int, default=None,
                        help="optional bounded smoke subset; default processes the full capture")
    parser.add_argument("--subset-manifest", type=Path, default=None,
                        help="locked subset manifest required for a bounded smoke run")
    args = parser.parse_args()
    print(json.dumps(run(args.capture.resolve(), args.output.resolve(), args.config.resolve(), args.weights.resolve(),
                         args.names.resolve(), args.resource_manifest.resolve(), args.frame_start,
                         args.frame_count, args.subset_manifest), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
