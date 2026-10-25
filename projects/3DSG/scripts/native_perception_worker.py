#!/usr/bin/env python3
"""Persistent P3 geometry + OpenCLIP worker for live/replay graph providers.

The control channel is JSON-lines on stdin/stdout. Large RGB-D arrays remain in
the immutable capture packet on disk; responses only contain identities and
artifact paths. The appearance environment and both model sessions are loaded
once per worker lifetime.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import time
import traceback
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.legacy_mapper_packets import OpenCLIPFeatureArtifactStore  # noqa: E402


def _emit(value: dict[str, Any]) -> None:
    sys.__stdout__.write(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False) + "\n")
    sys.__stdout__.flush()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _normalize_detection(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {key: value for key, value in row.items() if key != "detection_3d"}
    detection = row.get("detection_3d")
    if detection is not None:
        normalized["detection_3d"] = {
            "detection_id": str(detection.detection_id),
            "timestamp_s": float(detection.timestamp_s),
            "center_W": [float(x) for x in detection.center_W],
            "center_method": str(detection.center_method),
            "class_name": detection.class_name,
            "class_id": detection.class_id,
            "confidence": float(detection.confidence),
            "frame_id": str(detection.frame_id), "layer_id": str(detection.layer_id),
            "extents_m": [float(x) for x in detection.extents_m],
            "provider_metadata": dict(detection.provider_metadata),
        }
    return normalized


class _PerceptionRuntime:
    def __init__(self, args: argparse.Namespace) -> None:
        cg, occ = args.conceptgraphs_root.resolve(strict=True), args.occusg_root.resolve(strict=True)
        sys.path[:0] = [str(cg), str(cg / "src"),
                        str(cg / "ref_ConceptGraph/concept-graphs"), str(occ / "src")]
        # Third-party constructors can print banners. Keep stdout reserved for
        # the strict worker protocol while retaining their diagnostics in logs.
        with redirect_stdout(sys.stderr):
            from adapters.habitat.frames import frame_from_capture_record
            from integration_v2.perception_feature_sessions import (
                FeatureContract, OpenCLIPFeatureSession, P3GeometrySession, validate_join,
            )
            from exploration.p3_clustering import project_current_frame_optimized

            self.frame_from_capture_record = frame_from_capture_record
            profile = json.loads(args.p3_profile.read_text(encoding="utf-8"))
            contract_data = json.loads(args.feature_contract.read_text(encoding="utf-8"))[
                "feature_contract"]
            self.feature_contract = FeatureContract(**contract_data)
            cluster_backend = os.environ.get(
                "THREEDSG_P3_CLUSTER_BACKEND", "spatial_hash_v1")
            if cluster_backend == "reference":
                projector = None
            elif cluster_backend == "spatial_hash_v1":
                projector = project_current_frame_optimized
            else:
                raise ValueError(
                    "THREEDSG_P3_CLUSTER_BACKEND must be 'reference' or 'spatial_hash_v1'")
            self.p3 = P3GeometrySession.open(profile=profile, projector=projector)
            try:
                self.clip = OpenCLIPFeatureSession.open(
                    checkpoint=args.checkpoint, contract=self.feature_contract)
            except BaseException:
                self.p3.close()
                raise
        self.output = args.output.resolve()
        self.feature_store = OpenCLIPFeatureArtifactStore(self.output / "assets/openclip")
        self.validate_join = validate_join
        self.closed = False

    def process(self, packet_path: Path) -> dict[str, Any]:
        process_started = time.perf_counter()
        packet_path = packet_path.resolve(strict=True)
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        identity = (str(packet.get("frame_id", "")), int(packet.get("frame_index", -1)))
        if not identity[0] or identity[1] < 0:
            raise ValueError("perception packet requires frame_id and non-negative frame_index")
        capture_record = packet["capture_record"]
        rgbd, frame_contract = self.frame_from_capture_record(
            capture_record, Path(packet["capture_root"]))
        p2_for_sessions = []
        for row in packet["p2_detections"]:
            mask_path = Path(row["mask_file"]).resolve(strict=True)
            mask = np.load(mask_path, allow_pickle=False)
            p2_for_sessions.append({**row, "mask": mask,
                                    "mask_sha256": _sha256_file(mask_path)})
        frame_input = {
            "frame_id": identity[0], "frame_index": identity[1],
            "timestamp_s": float(packet["timestamp_s"]),
            "rgb": np.asarray(rgbd.rgb, dtype=np.uint8),
            "rgbd": {"depth_m": np.asarray(rgbd.depth_m, dtype=np.float32),
                     "valid_mask": np.asarray(rgbd.valid_mask, dtype=bool)},
            "intrinsics": rgbd.intrinsics, "T_W_Copt": rgbd.T_W_Copt,
            "base_pose_W": tuple(float(x) for x in frame_contract.base_pose_W),
            "source_hashes": dict(packet["source_hashes"]),
            "p2_detections": p2_for_sessions,
        }
        input_ready = time.perf_counter()
        # Keep native library output out of the machine-readable control stream.
        with redirect_stdout(sys.stderr):
            p3_started = time.perf_counter()
            p3_result = self.p3.update_frame(frame_input)
            p3_finished = time.perf_counter()
            clip_started = time.perf_counter()
            clip_result = self.clip.update_frame(frame_input)
            clip_finished = time.perf_counter()
            join_started = time.perf_counter()
            joined = self.validate_join(frame_input, p3_result, clip_result)
            join_finished = time.perf_counter()
        persist_started = time.perf_counter()
        feature_artifacts = self.feature_store.persist(identity[1], clip_result["rows"])
        p3_rows = [_normalize_detection(row) for row in p3_result.get("detections", [])]
        frame_dir = self.output / "native_perception" / f"frame_{identity[1]:08d}"
        p3_path = frame_dir / "p3_detections.json"
        feature_path = frame_dir / "feature_artifacts.json"
        timings_ms = {
            "input_load_and_validation": (input_ready - process_started) * 1000.0,
            "p3_geometry": (p3_finished - p3_started) * 1000.0,
            "openclip_features": (clip_finished - clip_started) * 1000.0,
            "join_validation": (join_finished - join_started) * 1000.0,
            "artifact_persistence_and_hashing": (time.perf_counter() - persist_started) * 1000.0,
            "total": (time.perf_counter() - process_started) * 1000.0,
        }
        _atomic_json(p3_path, {"frame_id": identity[0], "frame_index": identity[1],
                    "status": p3_result["status"], "detections": p3_rows,
                    "provenance": p3_result["provenance"], "join": joined,
                    "stage_timings_ms": timings_ms})
        _atomic_json(feature_path, {"frame_id": identity[0], "frame_index": identity[1],
                    "features": feature_artifacts,
                    "model_load_count": self.clip.model_load_count,
                    "image_encode_count": self.clip.image_encode_count,
                    "text_encode_count": self.clip.text_encode_count,
                    "text_cache_size": self.clip.text_cache_size,
                    "contract": self.feature_contract.__dict__})
        return {"status": "frame_processed", "frame_id": identity[0],
                "frame_index": identity[1], "p3_path": str(p3_path),
                "p3_sha256": _sha256_file(p3_path),
                "feature_path": str(feature_path),
                "feature_sha256": _sha256_file(feature_path),
                "p3_count": len(p3_rows), "feature_count": len(feature_artifacts),
                "join": joined, "model_load_count": self.clip.model_load_count,
                "image_encode_count": self.clip.image_encode_count,
                "stage_timings_ms": timings_ms}

    def close(self, *, finalize: bool) -> dict[str, Any]:
        if self.closed:
            return {"finalized": False, "already_closed": True}
        self.closed = True
        error: BaseException | None = None
        try:
            if finalize:
                with redirect_stdout(sys.stderr):
                    self.p3.finalize()
                    self.clip.finalize()
        except BaseException as exc:
            error = exc
        finally:
            try:
                self.p3.close()
            except BaseException as exc:
                error = error or exc
            try:
                self.clip.close()
            except BaseException as exc:
                error = error or exc
        if error is not None:
            raise error
        return {"finalized": bool(finalize),
                "model_load_count": self.clip.model_load_count,
                "image_encode_count": self.clip.image_encode_count,
                "text_encode_count": self.clip.text_encode_count}


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(args: argparse.Namespace) -> int:
    runtime: _PerceptionRuntime | None = None
    try:
        runtime = _PerceptionRuntime(args)
        _atomic_json(args.ready_file, {"schema": "3dsg-native-perception-ready/v1",
                    "status": "ready", "pid": os.getpid(),
                    "protocol": "3dsg-native-perception-worker/v1"})
        _emit({"status": "ready", "protocol": "3dsg-native-perception-worker/v1"})
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("worker request must be a JSON object")
                operation = request.get("op")
                if operation == "frame":
                    packet_path = Path(request["packet_path"])
                    expected = str(request.get("packet_sha256", ""))
                    if not expected or _sha256_file(packet_path) != expected:
                        raise ValueError("perception packet is missing or fails SHA-256 verification")
                    result = runtime.process(packet_path)
                    _emit({"request_id": request.get("request_id"), **result})
                elif operation in {"finalize", "close"}:
                    result = runtime.close(finalize=operation == "finalize")
                    _emit({"status": "finalized" if operation == "finalize" else "closed",
                           **result})
                    return 0
                else:
                    raise ValueError(f"unsupported operation: {operation!r}")
            except BaseException as exc:
                _emit({"status": "error", "error_type": type(exc).__name__,
                       "error": str(exc), "traceback": traceback.format_exc()})
                if runtime is not None:
                    try:
                        runtime.close(finalize=False)
                    except BaseException:
                        pass
                return 2
        if runtime is not None:
            runtime.close(finalize=False)
        return 0
    except BaseException as exc:
        _emit({"status": "startup_error", "error_type": type(exc).__name__,
               "error": str(exc), "traceback": traceback.format_exc()})
        if runtime is not None:
            try:
                runtime.close(finalize=False)
            except BaseException:
                pass
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conceptgraphs-root", type=Path, required=True)
    parser.add_argument("--occusg-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--p3-profile", type=Path, required=True)
    parser.add_argument("--feature-contract", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
