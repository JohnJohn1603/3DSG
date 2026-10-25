#!/usr/bin/env python3
"""Export source-policy P3 selected points for a small, explicit frame set."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from adapters.habitat.frames import frame_from_capture_record  # noqa: E402
from standalone_sg.geometry import backproject_depth  # noqa: E402
from project_detection_masks_to_3d import cluster_components  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def export(capture: Path, cache: Path, detections_3d: Path, output: Path,
           frame_ids: set[str]) -> list[dict]:
    capture = capture.resolve()
    cache = cache.resolve()
    detections_3d = detections_3d.resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile_path = ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    geometry = profile["geometry"]
    capture_manifest_path = capture / "capture_manifest.json"
    cache_index_path = cache / "detection_2d_index.json"
    capture_manifest = json.loads(capture_manifest_path.read_text(encoding="utf-8"))
    cache_index = json.loads(cache_index_path.read_text(encoding="utf-8"))
    if cache_index.get("status") != "pass":
        raise ValueError("Detection2D cache is not a successful cache")
    if cache_index.get("capture_manifest_sha256") != sha256(capture_manifest_path):
        raise ValueError("Detection2D cache does not match capture manifest")
    detections_by_frame = {}
    for line in detections_3d.open(encoding="utf-8"):
        row = json.loads(line)
        detections_by_frame[row["frame_id"]] = row
    cache_by_frame = {str(row["frame_id"]): row for row in cache_index["frames"]}
    records = {str(row["frame_id"]): row for row in capture_manifest["frames"]}
    missing = frame_ids - records.keys()
    if missing:
        raise ValueError(f"requested frame IDs absent from capture: {sorted(missing)}")
    rows = []
    for frame_id in sorted(frame_ids, key=lambda value: int(value.rsplit("-", 1)[-1])):
        record = records[frame_id]
        _rgbd, packet = frame_from_capture_record(record, capture)
        frame_cache = cache_by_frame[frame_id]
        accepted = {item["detection_id"]: item for item in detections_by_frame[frame_id]["detections"]}
        for det in frame_cache["detections"]:
            det_id = str(det["detection_id"])
            if det_id not in accepted:
                continue
            mask_path = cache / det["mask_file"]
            mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
            valid = packet.rgbd.valid_mask if packet.rgbd.valid_mask is not None else np.isfinite(packet.rgbd.depth_m)
            selected = valid & mask
            masked_depth = np.where(selected, packet.rgbd.depth_m, np.nan).astype(np.float32)
            cloud = backproject_depth(
                masked_depth, packet.rgbd.intrinsics, packet.rgbd.T_W_Copt,
                meters_per_unit=1.0,
                min_depth_m=float(geometry["min_depth_m"]),
                max_depth_m=float(geometry["max_depth_m"]),
                valid_mask=selected, timestamp=packet.timestamp_s, scan_id=frame_id,
            )
            components = cluster_components(np.asarray(cloud.xyz_W, dtype=np.float64), float(geometry["cluster_eps_m"]))
            cluster_ids = components[0] if components else np.empty(0, dtype=np.int64)
            sampled_valid = np.isfinite(masked_depth)
            sampled_valid &= masked_depth >= float(geometry["min_depth_m"])
            sampled_valid &= masked_depth <= float(geometry["max_depth_m"])
            sampled_valid &= selected
            pixel_rc = np.column_stack(np.nonzero(sampled_valid)).astype(np.int32, copy=False)
            if len(pixel_rc) != len(cloud.xyz_W):
                raise RuntimeError(f"{frame_id}/{det_id}: P3 point/pixel ordering parity failed")
            points = np.asarray(cloud.xyz_W[cluster_ids], dtype=np.float32)
            pixels = pixel_rc[cluster_ids]
            legacy_detection = accepted[det_id]
            low, high = points.min(axis=0), points.max(axis=0)
            center = (low + high) / 2.0
            if not np.allclose(center, legacy_detection["center_W"], rtol=0, atol=2e-6):
                raise RuntimeError(f"{frame_id}/{det_id}: selected point-set center differs from cached P3 center")
            ref_name = f"{frame_id}__{det_id.replace(':', '_')}.npz"
            pointset_path = output / ref_name
            np.savez_compressed(pointset_path, points_W=points, pixel_rc=pixels)
            rows.append({
                "observation_id": det_id,
                "frame_id": frame_id,
                "timestamp_s": packet.timestamp_s,
                "class_name": legacy_detection["class_name"],
                "p3_decision": "accepted",
                "p3_reason": "largest_cluster",
                "selected_point_count": len(points),
                "selected_pixel_count": len(pixels),
                "pointset_ref": ref_name,
                "pointset_sha256": sha256(pointset_path),
                "mask_ref": str(det["mask_file"]),
                "mask_sha256": sha256(mask_path),
                "detection_3d_cache_sha256": sha256(detections_3d),
                "detection_2d_index_sha256": sha256(cache_index_path),
                "capture_manifest_sha256": sha256(capture_manifest_path),
                "center_W": legacy_detection["center_W"],
                "center_matches_cached_p3": True,
                "algorithm_changed": False,
            })
    (output / "observation_pointset_lineage.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--detection-2d-cache", type=Path, required=True)
    parser.add_argument("--detection-3d-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frame-ids", nargs="+", required=True)
    args = parser.parse_args()
    rows = export(args.capture, args.detection_2d_cache, args.detection_3d_cache,
                  args.output, set(args.frame_ids))
    print(json.dumps({"status": "pass", "observations": len(rows), "output": str(args.output.resolve())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
