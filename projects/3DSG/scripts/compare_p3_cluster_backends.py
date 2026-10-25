#!/usr/bin/env python3
"""Compare reference and spatial-hash P3 clustering on saved native packets."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exploration.p3_clustering import euclidean_components  # noqa: E402
from scripts.native_perception_worker import _normalize_detection, _sha256_file  # noqa: E402


DEFAULT_CG = Path("/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG")
DEFAULT_OCC = Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG")


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())
                if key not in ("cluster_backend", "cluster_equivalence_status")}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, np.ndarray):
        return _canonical(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def _build_frame_input(packet_path: Path, frame_from_capture_record: Callable) -> dict[str, Any]:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    capture_record = packet["capture_record"]
    rgbd, contract = frame_from_capture_record(
        capture_record, Path(packet["capture_root"]))
    detections = []
    for row in packet["p2_detections"]:
        mask_path = Path(row["mask_file"]).resolve(strict=True)
        if _sha256_file(mask_path) != row.get("mask_sha256"):
            raise ValueError(f"mask SHA-256 mismatch: {mask_path}")
        detections.append({**row,
                           "mask": np.load(mask_path, allow_pickle=False),
                           "mask_sha256": _sha256_file(mask_path)})
    return {
        "frame_id": str(packet["frame_id"]),
        "frame_index": int(packet["frame_index"]),
        "timestamp_s": float(packet["timestamp_s"]),
        "rgb": np.asarray(rgbd.rgb, dtype=np.uint8),
        "rgbd": {"depth_m": np.asarray(rgbd.depth_m, dtype=np.float32),
                 "valid_mask": np.asarray(rgbd.valid_mask, dtype=bool)},
        "intrinsics": rgbd.intrinsics,
        "T_W_Copt": rgbd.T_W_Copt,
        "base_pose_W": tuple(float(x) for x in contract.base_pose_W),
        "source_hashes": dict(packet["source_hashes"]),
        "p2_detections": detections,
    }


def _run_projector(projector: Callable, source_module: Any, cluster_fn: Callable,
                   frame_input: dict[str, Any], profile: dict[str, Any]):
    started = time.perf_counter()
    original = source_module.cluster_components
    recorded: list[list[np.ndarray]] = []

    def recording_cluster(points: np.ndarray, eps: float) -> list[np.ndarray]:
        result = cluster_fn(points, eps)
        recorded.append(result)
        return result

    source_module.cluster_components = recording_cluster
    try:
        result = projector(frame_input, profile)
    finally:
        source_module.cluster_components = original
    normalized = {
        "frame_id": result["frame_id"],
        "frame_index": result["frame_index"],
        "timestamp_s": result["timestamp_s"],
        "status": result["status"],
        "detections": [_normalize_detection(row) for row in result["detections"]],
    }
    return normalized, recorded, time.perf_counter() - started


def compare(args: argparse.Namespace) -> dict[str, Any]:
    cg = args.conceptgraphs_root.resolve(strict=True)
    occ = args.occusg_root.resolve(strict=True)
    sys.path[:0] = [str(cg), str(cg / "src"),
                    str(cg / "ref_ConceptGraph/concept-graphs"), str(occ / "src")]
    from adapters.habitat.frames import frame_from_capture_record
    from integration_v2.perception_feature_sessions import _p3_project_current_frame

    source_module = importlib.import_module("scripts.project_detection_masks_to_3d")
    profile_path = cg / "configs/perception/yolo11n_seg_replica_cad_dev.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    input_dir = args.run_dir.resolve(strict=True) / "native_graph/perception_inputs"
    packet_paths = sorted(input_dir.glob("frame_*.json"))
    if args.frames:
        wanted = set(args.frames)
        packet_paths = [path for path in packet_paths
                        if int(path.stem.removeprefix("frame_")) in wanted]
    if not packet_paths:
        raise FileNotFoundError(f"no selected packet inputs under {input_dir}")

    frame_reports = []
    all_equal = True
    started = time.perf_counter()
    for packet_path in packet_paths:
        frame_input = _build_frame_input(packet_path, frame_from_capture_record)
        ref, ref_groups, ref_elapsed = _run_projector(
            _p3_project_current_frame, source_module, source_module.cluster_components,
            frame_input, profile)
        sparse, sparse_groups, sparse_elapsed = _run_projector(
            _p3_project_current_frame, source_module, euclidean_components,
            frame_input, profile)
        component_equal = (len(ref_groups) == len(sparse_groups)
                           and all(len(a) == len(b)
                                   and all(np.array_equal(x, y) for x, y in zip(a, b))
                                   for a, b in zip(ref_groups, sparse_groups)))
        output_equal = _canonical(ref) == _canonical(sparse)
        equal = component_equal and output_equal
        all_equal &= equal
        frame_reports.append({
            "frame_id": frame_input["frame_id"],
            "frame_index": frame_input["frame_index"],
            "p2_detection_count": len(frame_input["p2_detections"]),
            "cluster_call_count": len(ref_groups),
            "reference_elapsed_s": ref_elapsed,
            "sparse_elapsed_s": sparse_elapsed,
            "speedup": ref_elapsed / sparse_elapsed if sparse_elapsed else None,
            "reference_cluster_sizes": [[len(group) for group in groups]
                                        for groups in ref_groups],
            "sparse_cluster_sizes": [[len(group) for group in groups]
                                     for groups in sparse_groups],
            "component_membership_identical": component_equal,
            "p3_output_identical": output_equal,
            "identical": equal,
        })
    return {
        "schema": "p3-cluster-backend-parity/v1",
        "source_run": str(args.run_dir.resolve()),
        "frames_compared": len(frame_reports),
        "all_identical": all_equal,
        "elapsed_s": time.perf_counter() - started,
        "frames": frame_reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--frames", type=int, nargs="*")
    parser.add_argument("--conceptgraphs-root", type=Path, default=DEFAULT_CG)
    parser.add_argument("--occusg-root", type=Path, default=DEFAULT_OCC)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps({"output": str(args.output),
                      "frames_compared": report["frames_compared"],
                      "all_identical": report["all_identical"],
                      "elapsed_s": report["elapsed_s"]}, sort_keys=True))
    return 0 if report["all_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
