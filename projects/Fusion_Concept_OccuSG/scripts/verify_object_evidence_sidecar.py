#!/usr/bin/env python3
"""Verify point-to-pixel and Detection3D parity for a direct P3 sidecar."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
from adapters.habitat.frames import frame_from_capture_record  # noqa: E402


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def verify(input_run: Path, p3_output: Path, full_p3: Path, tolerance_m: float = 2e-5) -> dict:
    capture = input_run / "capture"
    manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    capture_records = {int(r["frame_index"]): r for r in manifest["frames"]}
    p2 = json.loads((input_run / "detection_2d/detection_2d_index.json").read_text(encoding="utf-8"))
    p2_by_id = {d["detection_id"]: d for f in p2["frames"] for d in f["detections"]}
    index_path = p3_output / "object_evidence/index.jsonl"
    records = read_jsonl(index_path)
    full = {d["detection_id"]: d for frame in read_jsonl(full_p3) for d in frame.get("detections", [])
            if int(frame["frame_id"].rsplit("-", 1)[-1]) in {int(x["frame_index"]) for x in records}}
    partial = {d["detection_id"]: d for frame in read_jsonl(p3_output / "detection_3d.jsonl") for d in frame.get("detections", [])}
    checks = []
    max_error = 0.0
    for row in records:
        oid = row["observation_id"]
        frame_index = int(row["frame_index"])
        capture_row = capture_records[frame_index]
        _rgbd, packet = frame_from_capture_record(capture_row, capture)
        frame_dir = capture / capture_row["frame_dir"]
        rgb = np.asarray(np.load(frame_dir / "rgb.npy", allow_pickle=False), dtype=np.uint8)[..., :3]
        det = p2_by_id[oid]
        mask_path = input_run / "detection_2d" / det["mask_file"]
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        point_path = p3_output / "object_evidence" / row["point_file"]
        if sha(point_path) != row["point_file_sha256"]:
            raise ValueError(f"sidecar file hash mismatch for {oid}")
        with np.load(point_path, allow_pickle=False) as cloud:
            xyz = cloud["xyz_W"].astype(np.float64)
            pixels = cloud["pixel_rc"].astype(np.int64)
            colors = cloud["rgb_u8"]
            pose = cloud["T_W_Copt"].astype(np.float64)
        if len(xyz) != len(pixels) or len(xyz) != len(colors):
            raise ValueError(f"sidecar arrays do not align for {oid}")
        if len(pixels) != int(row["point_count"]):
            raise ValueError(f"sidecar point count metadata mismatch for {oid}")
        if not np.all(mask[pixels[:, 0], pixels[:, 1]]):
            raise ValueError(f"selected pixel falls outside detection mask for {oid}")
        if not np.array_equal(colors, rgb[pixels[:, 0], pixels[:, 1]]):
            raise ValueError(f"selected RGB does not match source image for {oid}")
        depth = np.asarray(packet.rgbd.depth_m[pixels[:, 0], pixels[:, 1]], dtype=np.float64)
        intr = packet.rgbd.intrinsics
        u, v = pixels[:, 1].astype(np.float64), pixels[:, 0].astype(np.float64)
        xyz_c = np.column_stack(((u - intr.cx) * depth / intr.fx,
                                 (v - intr.cy) * depth / intr.fy, depth))
        projected = (pose[:3, :3] @ xyz_c.T).T + pose[:3, 3]
        err = float(np.max(np.abs(projected - xyz))) if len(xyz) else 0.0
        max_error = max(max_error, err)
        if err > tolerance_m:
            raise ValueError(f"XYZ/pixel backprojection error {err} m exceeds tolerance for {oid}")
        if oid not in partial or oid not in full:
            raise ValueError(f"P3 accepted observation missing in partial/full cache: {oid}")
        for field in ("center_W", "extents_m", "class_name", "class_id", "confidence"):
            if partial[oid].get(field) != full[oid].get(field):
                raise ValueError(f"P3 Detection3D sidecar pass changed {field} for {oid}")
        checks.append({"observation_id": oid, "point_count": len(xyz), "max_xyz_error_m": err,
                       "mask_pixels_match": True, "rgb_pixels_match": True, "detection3d_parity": True})
    full_prefix_ids = set(full)
    if full_prefix_ids != set(partial):
        raise ValueError("P3 accepted-ID denominator changed in selected frame range")
    report = {"status": "pass", "tolerance_m": tolerance_m, "max_xyz_error_m": max_error,
              "direct_sidecar": True, "reconstructed": False,
              "capture_manifest_sha256": sha(capture / "capture_manifest.json"),
              "p2_index_sha256": sha(input_run / "detection_2d/detection_2d_index.json"),
              "full_p3_sha256": sha(full_p3), "partial_p3_sha256": sha(p3_output / "detection_3d.jsonl"),
              "accepted_observations": len(partial), "verified_sidecar_observations": len(checks),
              "center_extent_class_score_exact_parity": True, "observations": checks}
    out = p3_output / "object_evidence/verification.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-run", type=Path, required=True)
    ap.add_argument("--p3-output", type=Path, required=True)
    ap.add_argument("--full-p3", type=Path, required=True)
    args = ap.parse_args()
    report = verify(args.input_run.resolve(), args.p3_output.resolve(), args.full_p3.resolve())
    print(json.dumps({"status": report["status"], "accepted_observations": report["accepted_observations"],
                      "max_xyz_error_m": report["max_xyz_error_m"], "tolerance_m": report["tolerance_m"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
