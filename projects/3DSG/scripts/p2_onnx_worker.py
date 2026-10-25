#!/usr/bin/env python3
"""Persistent CPU P2 worker using the locked local YOLO11-seg session API.

This process runs in the existing ONNX Runtime environment. RGB-D frames are
selected and persisted by the 3DSG host; the worker owns one detector session
and writes masks below the current run directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.p2_runtime import cpu_session_options


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _emit(value: dict) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                allow_nan=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    if len(sys.argv) != 8:
        raise SystemExit(
            "usage: p2_onnx_worker.py LEGACY_ROOT WEIGHTS PROFILE NAMES OUTPUT_ROOT "
            "INTRA_OP_THREADS INTER_OP_THREADS")
    legacy_root, weights, profile_path, names_path, output_root = map(Path, sys.argv[1:6])
    intra_op_threads, inter_op_threads = map(int, sys.argv[6:8])
    legacy_root = legacy_root.resolve(strict=True)
    weights = weights.resolve(strict=True)
    profile_path = profile_path.resolve(strict=True)
    names_path = names_path.resolve(strict=True)
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(legacy_root))

    import scripts.run_replica_cad_instance_segmentation as reference_p2
    import onnxruntime as ort

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    names = [line.strip() for line in names_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    if len(names) != 80:
        raise ValueError("P2 profile requires the locked 80-class COCO vocabulary")
    options = cpu_session_options(
        ort, intra_op_threads=intra_op_threads,
        inter_op_threads=inter_op_threads,
    )
    ort_session = ort.InferenceSession(
        str(weights), sess_options=options, providers=["CPUExecutionProvider"])
    session = reference_p2.P2DetectorSession(
        ort_session=ort_session, names=names,
        preprocess=profile["runtime"] | profile["preprocessing"],
    )
    class_map = profile["vocabulary"].get("canonical_map", {})
    _emit({"event": "ready", "status": "ready", "model_load_count": 1,
           "weights_sha256": _sha256(weights), "backend": "onnxruntime_cpu",
           "intra_op_threads": intra_op_threads,
           "inter_op_threads": inter_op_threads,
           "execution_mode": "sequential"})
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            if message.get("op") == "shutdown":
                break
            if message.get("op") != "frame":
                _emit({"event": "error", "error": "unsupported_operation"})
                continue
            try:
                frame_index = int(message["frame_index"])
                frame_id = str(message["frame_id"])
                timestamp_s = float(message["timestamp_s"])
                rgb_path = Path(message["rgb_file"]).resolve(strict=True)
                rgb = np.load(rgb_path, allow_pickle=False)
                detections = session.update_frame({
                    "frame_index": frame_index, "frame_id": frame_id,
                    "timestamp_s": timestamp_s, "rgb": rgb,
                })
                frame_root = output_root / f"frame_{frame_index:08d}"
                frame_root.mkdir(parents=True, exist_ok=False)
                rows = []
                for ordinal, detection in enumerate(detections):
                    mask_path = frame_root / f"mask_{ordinal:04d}.npy"
                    with mask_path.open("wb") as stream:
                        np.save(stream, np.asarray(detection["mask"], dtype=bool), allow_pickle=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    rows.append({
                        "detection_id": f"{frame_id}:d{ordinal:04d}",
                        "class_id": int(detection["class_id"]),
                        "class_raw": str(detection["class_raw"]),
                        "class_canonical": str(class_map.get(
                            detection["class_raw"], detection["class_raw"].replace(" ", "_"))),
                        "score": float(detection["score"]),
                        "bbox_xyxy": [float(x) for x in detection["bbox_xyxy"]],
                        "mask_file": str(mask_path),
                        "mask_sha256": _sha256(mask_path),
                        "mask_pixels": int(np.asarray(detection["mask"], dtype=bool).sum()),
                    })
                _emit({"event": "frame", "status": "ok", "frame_id": frame_id,
                       "frame_index": frame_index, "detections": rows,
                       "processing_status": session.status})
            except BaseException as exc:
                _emit({"event": "frame", "status": "failed",
                       "frame_id": message.get("frame_id"),
                       "frame_index": message.get("frame_index"),
                       "error": f"{type(exc).__name__}: {exc}"})
                return 2
    finally:
        session.close()
    _emit({"event": "closed", "status": "closed"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
