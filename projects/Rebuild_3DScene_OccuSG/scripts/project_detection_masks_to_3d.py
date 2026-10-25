#!/usr/bin/env python3
"""P3: project cached RGB masks through the existing M1 depth contract.

The script consumes only raw capture data, a Detection2D cache and the locked
geometry profile.  It uses ``standalone_sg.geometry.backproject_depth`` for
the actual axial-depth/backprojection contract, then applies the source
Euclidean-cluster/largest-cluster/AABB-center policy.  It does not use scene
instance metadata, semantic IDs, navmesh or room GT.
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from adapters.habitat.frames import frame_from_capture_record  # noqa: E402
from standalone_sg.contracts.objects import Detection3D  # noqa: E402
from standalone_sg.geometry import backproject_depth  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def cluster_components(points: np.ndarray, eps: float) -> list[np.ndarray]:
    """Return all Euclidean-connected components in deterministic order."""
    if len(points) == 0:
        return []
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(points)
        neighbours = tree.query_ball_point(points, eps)
    except ImportError:
        # Deterministic fallback for minimal environments.  It is deliberately
        # local and does not alter the reference M1/M4 implementation.
        neighbours = []
        for i, point in enumerate(points):
            distance = np.linalg.norm(points - point, axis=1)
            neighbours.append(np.flatnonzero(distance <= eps).tolist())
    seen = np.zeros(len(points), dtype=bool)
    components: list[list[int]] = []
    for start in range(len(points)):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        component: list[int] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in neighbours[current]:
                neighbour = int(neighbour)
                if not seen[neighbour]:
                    seen[neighbour] = True
                    stack.append(neighbour)
        components.append(sorted(component))
    components.sort(key=lambda component: (-len(component), min(component)))
    return [np.asarray(component, dtype=np.int64) for component in components]


def cluster_largest(points: np.ndarray, eps: float) -> np.ndarray:
    """Return the largest Euclidean-connected point index set."""
    components = cluster_components(points, eps)
    return components[0] if components else np.empty(0, dtype=np.int64)


def write_rejection_overlay(rgb: np.ndarray, mask: np.ndarray, bbox: list[float], reason: str, output: Path) -> None:
    """Save an evaluator diagnostic for a rejected 2D→3D detection."""
    image = Image.fromarray(np.asarray(rgb)[..., :3].astype(np.uint8), mode="RGB").convert("RGBA")
    solid = Image.new("RGBA", image.size, (220, 40, 40, 100))
    mask_image = Image.fromarray((np.asarray(mask, dtype=bool).astype(np.uint8) * 120), mode="L")
    image.alpha_composite(Image.composite(solid, Image.new("RGBA", image.size), mask_image))
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = [float(value) for value in bbox]
    draw.rectangle((x0, y0, x1, y1), outline=(255, 220, 0, 255), width=2)
    draw.rectangle((0, 0, min(image.width, 8 * max(12, len(reason))), 18), fill=(0, 0, 0, 190))
    draw.text((2, 2), reason, fill=(255, 255, 255, 255))
    image.convert("RGB").save(output)


def transform_aabb_corners(points: np.ndarray, transform: np.ndarray) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    low, high = points.min(axis=0), points.max(axis=0)
    corners = np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])], dtype=np.float64)
    world = (transform[:3, :3] @ corners.T).T + transform[:3, 3]
    return tuple(float(v) for v in world.min(axis=0)), tuple(float(v) for v in world.max(axis=0))


def run(capture: Path, cache: Path, output: Path, profile_path: Path, *,
        sampling_stride: int = 1, cluster_min_points: int | None = None,
        cluster_eps_m: float | None = None) -> dict:
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    geometry = profile["geometry"]
    cache_index = json.loads((cache / "detection_2d_index.json").read_text(encoding="utf-8"))
    if cache_index.get("status") != "pass":
        raise ValueError("Detection2D cache is not a successful authoritative cache")
    if cache_index.get("capture_manifest_sha256") != sha256(capture / "capture_manifest.json"):
        raise ValueError("Detection2D cache was produced from a different capture manifest")
    capture_manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    records = capture_manifest["frames"]
    cache_frames = {str(frame["frame_id"]): frame for frame in cache_index["frames"]}
    output.mkdir(parents=True, exist_ok=True)
    jsonl_path = output / "detection_3d.jsonl"
    ledger_path = output / "object_evidence_ledger.jsonl"
    rejection_dir = output / "rejection_overlays"
    rejection_dir.mkdir(exist_ok=True)
    if type(sampling_stride) is not int or sampling_stride < 1:
        raise ValueError("sampling_stride must be a positive integer")
    effective_min_points = int(geometry["cluster_min_points"] if cluster_min_points is None else cluster_min_points)
    effective_eps_m = float(geometry["cluster_eps_m"] if cluster_eps_m is None else cluster_eps_m)
    if effective_min_points < 1 or not math.isfinite(effective_eps_m) or effective_eps_m <= 0:
        raise ValueError("invalid clustering ablation override")
    total = 0
    present_batches = 0
    rejected = 0
    ledger_rows: list[dict] = []
    with jsonl_path.open("w", encoding="utf-8") as det_out, ledger_path.open("w", encoding="utf-8") as ledger_out:
        for record in records:
            frame_id = str(record["frame_id"])
            if frame_id not in cache_frames:
                raise ValueError(f"Detection2D cache missing frame {frame_id}")
            frame_dir = capture / record["frame_dir"]
            _rgbd, packet = frame_from_capture_record(record, capture)
            frame_cache = cache_frames[frame_id]
            detections_3d: list[dict] = []
            rgb = np.load(frame_dir / "rgb.npy", allow_pickle=False)
            for det_index, det in enumerate(frame_cache["detections"]):
                mask_path = cache / det["mask_file"]
                mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
                if mask.shape != packet.rgbd.depth_m.shape:
                    raise ValueError(f"{frame_id}/{det['detection_id']}: mask shape mismatch")
                valid = packet.rgbd.valid_mask if packet.rgbd.valid_mask is not None else np.isfinite(packet.rgbd.depth_m)
                selected = valid & mask
                mask_pixel_count = int(mask.sum())
                valid_pixel_count = int(selected.sum())
                valid_depth_values = np.asarray(packet.rgbd.depth_m[selected], dtype=np.float64)
                valid_depth_values = valid_depth_values[np.isfinite(valid_depth_values)]
                rejection_overlay = rejection_dir / f"{frame_id}__{det_index:04d}.png"
                common = {
                    "frame_id": frame_id, "frame_index": int(record["frame_index"]),
                    "timestamp_s": float(record["timestamp_s"]), "detection_id": det["detection_id"],
                    "class_canonical": det["class_canonical"], "class_raw": det["class_raw"],
                    "score_raw": float(det["score_raw"]), "mask_file": det["mask_file"],
                    "mask_pixel_count": mask_pixel_count, "valid_depth_pixels": valid_pixel_count,
                    "valid_depth_fraction": float(valid_pixel_count / mask_pixel_count) if mask_pixel_count else 0.0,
                    "depth_min_m": float(valid_depth_values.min()) if len(valid_depth_values) else None,
                    "depth_max_m": float(valid_depth_values.max()) if len(valid_depth_values) else None,
                    "geometry_method": "M1_backproject+source_euclidean_largest_cluster",
                    "source_profile": profile["profile_id"],
                }
                if valid_pixel_count < effective_min_points:
                    common["status"] = "rejected_insufficient_depth_points"
                    rejected += 1
                    write_rejection_overlay(rgb, mask, det["bbox_xyxy"], common["status"], rejection_overlay)
                    ledger_rows.append({**common, "decision": "rejected", "reason": common["status"],
                                        "cluster_count": 0, "largest_cluster_points": 0,
                                        "rejection_overlay": rejection_overlay.name})
                    continue
                try:
                    masked_depth = np.where(selected, packet.rgbd.depth_m, np.nan).astype(np.float32)
                    cloud = backproject_depth(
                        masked_depth, packet.rgbd.intrinsics, packet.rgbd.T_W_Copt,
                        meters_per_unit=1.0,
                        min_depth_m=float(geometry["min_depth_m"]), max_depth_m=float(geometry["max_depth_m"]),
                        valid_mask=selected, timestamp=packet.timestamp_s, scan_id=frame_id,
                        stride=sampling_stride,
                    )
                    cloud_points = np.asarray(cloud.xyz_W, dtype=np.float64)
                    components = cluster_components(cloud_points, effective_eps_m)
                    cluster_ids = components[0] if components else np.empty(0, dtype=np.int64)
                    common.update({
                        "backprojected_points": int(len(cloud_points)),
                        "cluster_count": len(components),
                        "largest_cluster_points": int(len(cluster_ids)),
                    })
                    if len(cluster_ids) < effective_min_points:
                        common["status"] = "rejected_cluster_below_minimum"
                        rejected += 1
                        write_rejection_overlay(rgb, mask, det["bbox_xyxy"], common["status"], rejection_overlay)
                        ledger_rows.append({**common, "cluster_points": int(len(cluster_ids)), "decision": "rejected",
                                            "reason": common["status"], "rejection_overlay": rejection_overlay.name})
                        continue
                    if len(cluster_ids) > int(geometry["cluster_max_points"]):
                        common["status"] = "rejected_cluster_above_maximum"
                        rejected += 1
                        write_rejection_overlay(rgb, mask, det["bbox_xyxy"], common["status"], rejection_overlay)
                        ledger_rows.append({**common, "cluster_points": int(len(cluster_ids)), "decision": "rejected",
                                            "reason": common["status"], "rejection_overlay": rejection_overlay.name})
                        continue
                    points_world = np.asarray(cloud.xyz_W[cluster_ids], dtype=np.float64)
                    # Source path computes the visible-cluster AABB center in its
                    # working camera/point frame; the canonical equivalent is the
                    # world AABB center after the same M1 transform.
                    low, high = points_world.min(axis=0), points_world.max(axis=0)
                    center = (low + high) / 2.0
                    extents = high - low
                    common["largest_cluster_extent_m"] = [float(value) for value in extents]
                    common["largest_cluster_diameter_m"] = float(np.linalg.norm(extents))
                    detection = Detection3D(
                        detection_id=str(det["detection_id"]), timestamp_s=packet.timestamp_s,
                        center_W=tuple(float(v) for v in center),
                        center_method=str(geometry["center_method"]), class_name=str(det["class_canonical"]),
                        class_id=str(det["class_id"]), confidence=float(det["score_raw"]), frame_id="world",
                        layer_id="floor_0", extents_m=tuple(float(max(0.0, v)) for v in extents),
                        provider_metadata={
                            "provider": "p2_yolo11n_seg_onnx+p3_depth",
                            "detection_2d_id": str(det["detection_id"]),
                            "class_raw": str(det["class_raw"]), "bbox_xyxy": det["bbox_xyxy"],
                            "mask_file": str(det["mask_file"]), "valid_depth_pixels": valid_pixel_count,
                            "cluster_points": int(len(cluster_ids)), "frame_index": int(record["frame_index"]),
                            "camera_pose_W_Copt": packet.rgbd.T_W_Copt.matrix.tolist(),
                            "depth_units": "m", "depth_representation": "axial",
                            "semantic_gt_used": False, "room_gt_used": False,
                        },
                    )
                    detections_3d.append({
                        "detection_id": detection.detection_id, "timestamp_s": detection.timestamp_s,
                        "center_W": list(detection.center_W), "center_method": detection.center_method,
                        "class_name": detection.class_name, "class_id": detection.class_id,
                        "confidence": detection.confidence, "frame_id": detection.frame_id,
                        "layer_id": detection.layer_id, "extents_m": list(detection.extents_m),
                        "provider_metadata": dict(detection.provider_metadata),
                    })
                    ledger_rows.append({**common, "cluster_points": int(len(cluster_ids)), "center_W": list(detection.center_W),
                                        "extents_m": list(detection.extents_m), "decision": "accepted", "reason": "largest_cluster"})
                except Exception as exc:
                    rejected += 1
                    reason = f"{type(exc).__name__}: {exc}"
                    write_rejection_overlay(rgb, mask, det["bbox_xyxy"], reason[:120], rejection_overlay)
                    ledger_rows.append({**common, "decision": "rejected", "reason": reason,
                                        "rejection_overlay": rejection_overlay.name})
            batch = {
                "provider_id": "replica_cad_yolo11n_seg_dev_v2_p3",
                "frame_id": frame_id, "timestamp_s": float(record["timestamp_s"]),
                "status": "present" if detections_3d else "empty", "detections": detections_3d,
                "provenance": {"detection_2d_cache": str(cache), "profile": str(profile_path),
                                "profile_sha256": sha256(profile_path), "semantic_gt_used": False,
                                "room_gt_used": False, "navmesh_used": False},
            }
            if detections_3d:
                present_batches += 1
            total += len(detections_3d)
            det_out.write(json.dumps(batch, sort_keys=True, allow_nan=False) + "\n")
            print(f"PROGRESS: frame {int(record['frame_index']) + 1}/{len(records)} projected", flush=True)
        for row in ledger_rows:
            ledger_out.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    report = {
        "schema_version": 1, "kind": "detection_3d_cache", "status": "pass",
        "capture": str(capture), "capture_manifest_sha256": sha256(capture / "capture_manifest.json"),
        "detection_2d_cache": str(cache), "detection_2d_cache_sha256": sha256(cache / "detection_2d_index.json"),
        "profile": str(profile_path), "profile_sha256": sha256(profile_path),
        "frame_count": len(records), "present_batch_count": present_batches,
        "detection_3d_count": total, "rejected_detection_count": rejected,
        "effective_sampling_stride": sampling_stride,
        "effective_cluster_eps_m": effective_eps_m,
        "effective_cluster_min_points": effective_min_points,
        "ablation_overrides": {
            "sampling_stride": sampling_stride,
            "cluster_eps_m": None if cluster_eps_m is None else effective_eps_m,
            "cluster_min_points": None if cluster_min_points is None else effective_min_points,
        },
        "backprojection": "standalone_sg.geometry.backproject_depth",
        "cluster_policy": "source Euclidean epsilon/largest cluster/AABB center",
        "semantic_gt_used": False, "room_gt_used": False, "navmesh_used": False,
        "detection_3d_jsonl": jsonl_path.name, "evidence_ledger": ledger_path.name,
        "rejection_overlays": rejection_dir.name,
    }
    (output / "detection_3d_manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--detection-2d", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json")
    parser.add_argument("--sampling-stride", type=int, default=1,
                        help="development ablation only; default 1 preserves the reference path")
    parser.add_argument("--cluster-min-points", type=int, default=None,
                        help="development ablation only; omitted uses the locked profile")
    parser.add_argument("--cluster-eps-m", type=float, default=None,
                        help="development ablation only; omitted uses the locked profile")
    args = parser.parse_args()
    print(json.dumps(run(args.capture.resolve(), args.detection_2d.resolve(), args.output.resolve(), args.profile.resolve(),
                         sampling_stride=args.sampling_stride, cluster_min_points=args.cluster_min_points,
                         cluster_eps_m=args.cluster_eps_m), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
