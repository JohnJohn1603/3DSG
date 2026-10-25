#!/usr/bin/env python3
"""Offline forward/reverse visibility diagnostics on cached K4 observations.

No GT, annotation, semantic labels, or association result is read as a scoring
input. Both projection directions are evaluated at an explicit decision time;
neither direction revises a previously emitted decision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from adapters.habitat.frames import frame_from_capture_record  # noqa: E402
from standalone_sg.objects.pixel_visibility import (  # noqa: E402
    enforce_available_by_decision, score_prior_surface_visibility,
)

COLORS = {
    "support": (35, 205, 95),
    "depth_agreement_outside_mask": (45, 150, 245),
    "occluded": (255, 185, 35),
    "depth_contradiction": (235, 55, 55),
    "invalid_depth": (190, 70, 220),
}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_point_record(evidence_root: Path, observation_id: str) -> tuple[dict, np.ndarray]:
    index = [json.loads(line) for line in (evidence_root / "index.jsonl").read_text().splitlines() if line.strip()]
    matches = [row for row in index if row["observation_id"] == observation_id]
    if len(matches) != 1:
        raise ValueError(f"expected one P3 sidecar record for {observation_id}; found {len(matches)}")
    row = matches[0]
    point_path = evidence_root / row["point_file"]
    if sha(point_path) != row["point_file_sha256"]:
        raise ValueError(f"P3 point sidecar hash mismatch: {observation_id}")
    with np.load(point_path, allow_pickle=False) as archive:
        points = np.asarray(archive["xyz_W"], dtype=np.float64).copy()
        pose = np.asarray(archive["T_W_Copt"], dtype=np.float64).copy()
    return row | {"_point_path": str(point_path), "_pose": pose}, points


def find_detection(p2: dict, observation_id: str) -> dict:
    rows = [d for frame in p2["frames"] for d in frame.get("detections", [])
            if d.get("detection_id") == observation_id]
    if len(rows) != 1:
        raise ValueError(f"expected one P2 detection for {observation_id}; found {len(rows)}")
    return rows[0]


def frame_paths(capture_root: Path, row: dict) -> dict[str, Path]:
    frame_dir = capture_root / "frames" / f"{int(row['frame_index']):06d}"
    return {name: frame_dir / f"{name}.npy" for name in ("rgb", "depth_m", "valid_mask")}


def save_overlay(rgb: np.ndarray, mask: np.ndarray, result: dict, path: Path, title: str) -> None:
    base = np.asarray(rgb, dtype=np.uint8)
    if base.ndim == 3 and base.shape[2] == 4:
        base = base[:, :, :3]
    base = base.copy()
    if base.ndim != 3 or base.shape[2] != 3 or mask.shape != base.shape[:2]:
        raise ValueError("RGB/mask overlay shape mismatch")
    tint = np.zeros_like(base)
    tint[:] = (40, 130, 255)
    base[mask] = (0.55 * base[mask] + 0.45 * tint[mask]).astype(np.uint8)
    image = Image.fromarray(base, "RGB")
    draw = ImageDraw.Draw(image)
    # Points are rasterized from the prior surface; circles expose each status.
    for item in result["pixels"]:
        color = COLORS[item["status"]]
        x, y = int(item["column"]), int(item["row"])
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color, outline=(0, 0, 0))
    legend = Image.new("RGB", (image.width, 76), (25, 25, 30))
    ld = ImageDraw.Draw(legend)
    ld.text((6, 4), title, fill=(255, 255, 255))
    labels = [("support", "depth+mask support"), ("depth_agreement_outside_mask", "depth agree, outside mask"),
              ("occluded", "occluded"), ("depth_contradiction", "depth contradiction"), ("invalid_depth", "invalid depth")]
    for i, (key, label) in enumerate(labels):
        x = 6 + i * max(124, (image.width - 12) // len(labels))
        ld.rectangle((x, 34, x + 10, 44), fill=COLORS[key])
        ld.text((x + 14, 31), label, fill=(240, 240, 240))
    canvas = Image.new("RGB", (image.width, image.height + legend.height))
    canvas.paste(image, (0, 0)); canvas.paste(legend, (0, image.height))
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)


def one_direction(*, source_record: dict, points_W: np.ndarray, target_record: dict,
                  target_paths: dict[str, Path], target_mask: np.ndarray,
                  intrinsics, tolerance: float, output_png: Path,
                  decision_frame_index: int, decision_timestamp_s: float,
                  source_available_timestamp_s: float, target_view_timestamp_s: float,
                  direction_role: str) -> dict:
    enforce_available_by_decision(source_record["frame_index"], target_record["frame_index"], decision_frame_index)
    rgb = np.load(target_paths["rgb"], allow_pickle=False)
    depth = np.load(target_paths["depth_m"], allow_pickle=False)
    valid = np.load(target_paths["valid_mask"], allow_pickle=False).astype(bool)
    pose = target_record["_pose"]
    result = score_prior_surface_visibility(
        points_W, pose, depth, valid, target_mask,
        intrinsics,
        depth_tolerance_m=tolerance,
    )
    save_overlay(rgb, target_mask, result, output_png,
                 f"decision t={decision_frame_index}: {source_record['observation_id']} -> view {target_record['observation_id']}")
    available_at = max(float(source_available_timestamp_s), float(target_view_timestamp_s))
    return {
        **result,
        "source_observation_id": source_record["observation_id"],
        "target_observation_id": target_record["observation_id"],
        "source_frame_index": int(source_record["frame_index"]),
        "target_frame_index": int(target_record["frame_index"]),
        "decision_time_frame_index": int(decision_frame_index),
        "decision_time_timestamp_s": float(decision_timestamp_s),
        "source_observation_time_s": float(source_available_timestamp_s),
        "target_view_time_s": float(target_view_timestamp_s),
        "evidence_available_at_s": available_at,
        "available_by_decision_time": available_at <= float(decision_timestamp_s) + 1e-9,
        "direction_role": direction_role,
        "revises_past_decision": False,
        "overlay": str(output_png),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--p3-evidence-root", type=Path, required=True)
    parser.add_argument("--source-observation", required=True)
    parser.add_argument("--target-observation", required=True)
    parser.add_argument("--decision-time-frame-index", type=int, default=None,
                        help="default: later of source/target frames; evidence after this frame is rejected")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    run_root = args.run_root.resolve()
    evidence_root = args.p3_evidence_root.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output must be new/empty to preserve previous artifacts: {output}")
    capture_root = run_root / "capture"
    capture_manifest_path = capture_root / "capture_manifest.json"
    p2_index_path = run_root / "detection_2d/detection_2d_index.json"
    manifest = read_json(capture_manifest_path)
    p2 = read_json(p2_index_path)
    config_path = ROOT / "configs/association/k4_pixel_visibility_diagnostic_v2.json"
    config = read_json(config_path)
    tolerance = float(config["depth_agreement_tolerance_m"])
    p3_manifest_path = evidence_root.parent / "detection_3d_manifest.json"
    p3_manifest = read_json(p3_manifest_path)
    if p3_manifest.get("capture_manifest_sha256") != sha(capture_manifest_path):
        raise ValueError("P3 evidence is not bound to this capture manifest")
    if p3_manifest.get("detection_2d_cache_sha256") != sha(p2_index_path):
        raise ValueError("P3 evidence is not bound to this P2 cache")
    source_row, source_points = load_point_record(evidence_root, args.source_observation)
    target_row, target_points = load_point_record(evidence_root, args.target_observation)
    decision_frame = (max(int(source_row["frame_index"]), int(target_row["frame_index"]))
                      if args.decision_time_frame_index is None else int(args.decision_time_frame_index))
    if decision_frame not in {int(row["frame_index"]) for row in manifest["frames"]}:
        raise ValueError(f"decision time frame {decision_frame} is absent from capture manifest")
    decision_timestamp = next(float(row["timestamp_s"]) for row in manifest["frames"]
                              if int(row["frame_index"]) == decision_frame)
    source_detection = find_detection(p2, args.source_observation)
    target_detection = find_detection(p2, args.target_observation)
    W, H = map(int, manifest["resolution_px"])
    capture_rows = {str(row["frame_id"]): row for row in manifest["frames"]}
    _, source_packet = frame_from_capture_record(capture_rows[source_row["frame_id"]], capture_root)
    _, target_packet = frame_from_capture_record(capture_rows[target_row["frame_id"]], capture_root)
    source_intrinsics = source_packet.rgbd.intrinsics
    target_intrinsics = target_packet.rgbd.intrinsics
    if (source_intrinsics.width, source_intrinsics.height) != (W, H) or (target_intrinsics.width, target_intrinsics.height) != (W, H):
        raise ValueError("adapter intrinsics dimensions disagree with capture manifest")
    if source_intrinsics != target_intrinsics:
        raise ValueError("source/target camera calibration differs; explicit reprojection calibration is required")
    for row, packet in ((source_row, source_packet), (target_row, target_packet)):
        if not np.allclose(row["_pose"], packet.rgbd.T_W_Copt.matrix, rtol=0, atol=1e-6):
            raise ValueError(f"P3 sidecar pose differs from Habitat adapter pose for {row['frame_id']}")
    intrinsics = target_intrinsics
    intrinsics_doc = {"width": intrinsics.width, "height": intrinsics.height,
                      "fx": intrinsics.fx, "fy": intrinsics.fy,
                      "cx": intrinsics.cx, "cy": intrinsics.cy,
                      "calibration_id": intrinsics.calibration_id}
    target_paths = frame_paths(capture_root, target_row)
    source_paths = frame_paths(capture_root, source_row)
    source_mask_path = run_root / "detection_2d" / source_detection["mask_file"]
    target_mask_path = run_root / "detection_2d" / target_detection["mask_file"]
    source_mask = np.load(source_mask_path, allow_pickle=False).astype(bool)
    target_mask = np.load(target_mask_path, allow_pickle=False).astype(bool)
    for row, mask, paths in ((source_row, source_mask, source_paths), (target_row, target_mask, target_paths)):
        rgb = np.load(paths["rgb"], allow_pickle=False)
        depth = np.load(paths["depth_m"], allow_pickle=False)
        valid = np.load(paths["valid_mask"], allow_pickle=False)
        if rgb.shape[:2] != (H, W) or mask.shape != (H, W) or depth.shape != (H, W) or valid.shape != (H, W):
            raise ValueError(f"cached frame/mask shape mismatch at {row['frame_id']}")

    # Both directions use only observations/views available by decision time.
    forward = one_direction(source_record=source_row, points_W=source_points,
                            target_record=target_row, target_paths=target_paths,
                            target_mask=target_mask, intrinsics=target_intrinsics,
                            tolerance=tolerance, output_png=output / "forward_overlay.png",
                            decision_frame_index=decision_frame, decision_timestamp_s=decision_timestamp,
                            source_available_timestamp_s=float(source_row["timestamp_s"]),
                            target_view_timestamp_s=float(target_row["timestamp_s"]),
                            direction_role="prior_surface_projected_into_target_view")
    # Reverse uses current observation points, but only as a diagnostic at decision time.
    reverse = one_direction(source_record=target_row, points_W=target_points,
                            target_record=source_row, target_paths=source_paths,
                            target_mask=source_mask, intrinsics=source_intrinsics,
                            tolerance=tolerance, output_png=output / "reverse_current_time_overlay.png",
                            decision_frame_index=decision_frame, decision_timestamp_s=decision_timestamp,
                            source_available_timestamp_s=float(target_row["timestamp_s"]),
                            target_view_timestamp_s=float(source_row["timestamp_s"]),
                            direction_role="current_observation_projected_into_prior_view_at_current_decision_time")
    input_paths = [capture_manifest_path, p2_index_path, source_mask_path, target_mask_path,
                   *target_paths.values(), *source_paths.values(), Path(source_row["_point_path"]),
                   Path(target_row["_point_path"]), p3_manifest_path]
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1,
        "kind": "k4_pixel_registered_visibility_diagnostic",
        "status": "diagnostic_only_no_identity_or_match_decision",
        "config": str(config_path), "config_sha256": sha(config_path),
        "capture_run": str(run_root), "capture_manifest_sha256": sha(capture_manifest_path),
        "p2_index_sha256": sha(p2_index_path), "p3_manifest_sha256": sha(p3_manifest_path),
        "intrinsics": intrinsics_doc,
        "intrinsics_source": "frame_from_capture_record -> adapters.habitat.frames.habitat_camera_intrinsics",
        "pixel_center_convention": "cx=(width-1)/2; cy=(height-1)/2",
        "decision_time_frame_index": decision_frame,
        "decision_time_timestamp_s": decision_timestamp,
        "depth_representation": manifest["depth_representation"],
        "depth_units": manifest["depth_units"],
        "source_mask_sha256": sha(source_mask_path), "target_mask_sha256": sha(target_mask_path),
        "source_p3_point_file_sha256": source_row["point_file_sha256"],
        "target_p3_point_file_sha256": target_row["point_file_sha256"],
        "predictor_inputs_used": ["prior selected P3 world points", "current RGB-D", "current predicted mask", "camera pose/intrinsics"],
        "predictor_inputs_excluded": ["GT", "semantic sensor", "human annotation", "class labels", "frames later than decision time"],
        "forward_diagnostic": forward,
        "reverse_diagnostic": reverse,
        "input_sha256": {str(p): sha(p) for p in input_paths},
        "limitations": [
            "Selected P3 surface is partial and is not a complete object silhouette.",
            "Depth agreement plus mask overlap is surface-consistency evidence, not proof of physical identity.",
            "An identical RGB-D/geometry input for two different physical identities is observationally indistinguishable here.",
            "This sidecar emits no match, veto, track, or class decision and does not alter K4 assignments.",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "visibility.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    summary = forward["counts"]
    print("Pixel visibility diagnostic (no association decision)")
    print(f"Decision time t={decision_frame} ({decision_timestamp:.3f}s): {source_row['observation_id']} -> view {target_row['observation_id']}")
    print("Counts: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    print("Forward overlay:", output / "forward_overlay.png")
    print("Reverse diagnostic overlay:", output / "reverse_current_time_overlay.png")
    print("Manifest:", output / "visibility.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
