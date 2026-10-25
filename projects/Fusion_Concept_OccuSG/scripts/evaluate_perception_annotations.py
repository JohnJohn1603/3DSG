#!/usr/bin/env python3
"""Evaluate a small independent visible-object mask annotation pilot.

The annotation file is evaluator input only.  It is never converted into a
DetectionBatch or passed to the graph builder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def polygon_mask(points: list[list[float]], shape: tuple[int, int]) -> np.ndarray:
    height, width = shape
    image = Image.new("1", (width, height), 0)
    ImageDraw.Draw(image).polygon([(float(x), float(y)) for x, y in points], fill=1)
    return np.asarray(image, dtype=bool)


def run(annotation: Path, detection_cache: Path, output: Path) -> dict:
    spec = json.loads(annotation.read_text(encoding="utf-8"))
    cache = json.loads((detection_cache / "detection_2d_index.json").read_text(encoding="utf-8"))
    frame_map = {str(frame["frame_id"]): frame for frame in cache.get("frames", [])}
    rows = []
    for item in spec.get("annotations", []):
        frame_id = str(item["frame_id"])
        frame = frame_map.get(frame_id)
        if frame is None:
            raise ValueError(f"annotation references missing frame {frame_id}")
        shape = tuple(int(v) for v in frame["image_shape_hw"])
        reference = polygon_mask(item["polygon_xy"], shape)
        candidates = [det for det in frame.get("detections", [])
                      if det.get("class_canonical") == item["class_canonical"]]
        best = None
        for detection in candidates:
            mask = np.asarray(np.load(detection_cache / detection["mask_file"], allow_pickle=False), dtype=bool)
            intersection = int(np.count_nonzero(reference & mask))
            union = int(np.count_nonzero(reference | mask))
            iou = float(intersection / union) if union else 0.0
            candidate = {"detection_id": detection["detection_id"], "iou": iou,
                         "intersection_pixels": intersection, "union_pixels": union}
            if best is None or (candidate["iou"], candidate["detection_id"]) > (best["iou"], best["detection_id"]):
                best = candidate
        rows.append({
            "annotation_id": item["annotation_id"], "frame_id": frame_id,
            "class_canonical": item["class_canonical"], "visibility": item.get("visibility"),
            "reference_mask_pixels": int(reference.sum()), "best_detection": best,
        })
    report = {
        "schema_version": 1, "kind": "independent_perception_annotation_evaluation",
        "status": "pass", "annotation": str(annotation), "annotation_sha256": sha256(annotation),
        "detection_2d_cache": str(detection_cache),
        "detection_2d_cache_sha256": sha256(detection_cache / "detection_2d_index.json"),
        "annotation_count": len(rows), "rows": rows,
        "room_annotations": spec.get("room_annotations", []),
        "room_accuracy": None if not spec.get("room_annotations") else "not_implemented_in_pilot",
        "builder_input": False,
        "limitations": [
            "small manually drawn visible-mask pilot; not a full benchmark",
            "room annotations are absent unless independently supplied",
            "IoU is reported only for the annotated visible subset",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--detection-2d", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.annotations.resolve(), args.detection_2d.resolve(), args.output.resolve()),
                     indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
