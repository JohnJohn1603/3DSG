#!/usr/bin/env python3
"""Compare current OccuSG scan text with a proposed binary batch envelope.

This measures adapter-side encoding only. It does not invoke OctoMap and cannot
establish native mapping parity or end-to-end latency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from exploration.native_m2_wire import decode_integrate_project, encode_integrate_project


DEFAULT_KEYFRAME = ROOT / "runs/native-live-scanpoint-trusted-ab-120s-20261023-a/keyframes/frame_00000146"
def _load_cloud(keyframe_dir: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, dict]:
    metadata = json.loads((keyframe_dir / "metadata.json").read_text(encoding="utf-8"))
    manifest = json.loads((keyframe_dir / "manifest.json").read_text(encoding="utf-8"))
    depth = np.load(keyframe_dir / "depth_m.npy", allow_pickle=False).astype(np.float32, copy=False)
    intr = metadata["intrinsics"]
    transform = np.asarray(metadata["T_world_camera"], dtype=np.float64)
    if depth.shape != (int(intr["height"]), int(intr["width"])):
        raise ValueError("saved depth shape disagrees with metadata intrinsics")
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        raise ValueError("camera transform must be a finite 4x4 matrix")

    valid = np.isfinite(depth)
    valid &= depth >= max(float(metadata["depth_min_m"]), float(np.nextafter(np.float32(0), np.float32(1))))
    valid &= depth <= float(metadata["depth_max_m"])
    rows, cols = np.nonzero(valid)
    z = depth[rows, cols].astype(np.float64, copy=False)
    x = ((cols.astype(np.float64) - float(intr["cx"])) / float(intr["fx"])) * z
    y = ((rows.astype(np.float64) - float(intr["cy"])) / float(intr["fy"])) * z
    points_camera = np.column_stack((x, y, z))
    points_world = points_camera @ transform[:3, :3].T + transform[:3, 3]
    xyz = np.asarray(points_world, dtype=np.float32)
    if not np.isfinite(xyz).all():
        raise ValueError("backprojected points contain non-finite coordinates")
    has_hit = np.ones((xyz.shape[0],), dtype=np.bool_)
    origin = np.asarray(transform[:3, 3], dtype=np.float64)
    max_range = float(metadata["depth_max_m"])
    identity = {
        "frame_id": str(metadata["frame_id"]),
        "frame_index": int(metadata["frame_index"]),
        "capture_monotonic_ns": round(float(metadata["host_monotonic_s"]) * 1_000_000_000),
        "package_sha256": str(manifest["package_sha256"]),
    }
    return xyz, has_hit, origin, max_range, identity


def _encode_text(xyz: np.ndarray, has_hit: np.ndarray, origin: np.ndarray,
                 max_range: float) -> bytes:
    lines = [f"scan {max_range} {origin[0]} {origin[1]} {origin[2]} {len(xyz)}\n"]
    lines.extend(f"{float(point[0])} {float(point[1])} {float(point[2])} {int(hit)}\n"
                 for point, hit in zip(xyz, has_hit))
    return "".join(lines).encode("ascii")


def _encode_binary(xyz: np.ndarray, has_hit: np.ndarray, origin: np.ndarray,
                   max_range: float, request_id: int, identity: dict) -> bytes:
    return encode_integrate_project(
        xyz=xyz, has_hit=has_hit, origin_xyz=origin, max_range_m=max_range,
        request_id=request_id, frame_id=identity["frame_id"],
        frame_index=identity["frame_index"],
        capture_monotonic_ns=identity["capture_monotonic_ns"],
        package_sha256=identity["package_sha256"],
        projection_mode="full_refresh",
    )


def _validate_binary_roundtrip(packet: bytes, xyz: np.ndarray, has_hit: np.ndarray,
                               origin: np.ndarray, max_range: float, identity: dict) -> None:
    decoded = decode_integrate_project(packet)
    if (decoded["request_id"] != 0
            or decoded["max_range_m"] != max_range
            or decoded["projection_mode"] != "full_refresh"
            or not np.array_equal(decoded["origin_xyz"], origin.astype(np.float32))
            or decoded["frame_index"] != identity["frame_index"]
            or decoded["capture_monotonic_ns"] != identity["capture_monotonic_ns"]
            or decoded["frame_id"] != identity["frame_id"]
            or decoded["package_sha256"] != identity["package_sha256"]
            or not np.array_equal(decoded["xyz"], xyz)
            or not np.array_equal(decoded["has_hit"], has_hit)):
        raise ValueError("binary point or hit-flag round-trip changed the scan")


def _measure(function, repeats: int) -> tuple[int, list[float]]:
    durations = []
    size = 0
    for request_id in range(repeats):
        started = time.perf_counter_ns()
        result = function(request_id)
        durations.append((time.perf_counter_ns() - started) / 1e6)
        size = len(result)
    return size, durations


def benchmark(keyframe_dir: Path, repeats: int, packet_output: Path | None = None) -> dict:
    xyz, has_hit, origin, max_range, identity = _load_cloud(keyframe_dir)
    text_size, text_ms = _measure(
        lambda _request_id: _encode_text(xyz, has_hit, origin, max_range), repeats)
    binary_size, binary_ms = _measure(
        lambda request_id: _encode_binary(xyz, has_hit, origin, max_range, request_id, identity), repeats)
    binary_sample = _encode_binary(xyz, has_hit, origin, max_range, 0, identity)
    _validate_binary_roundtrip(binary_sample, xyz, has_hit, origin, max_range, identity)
    compression = text_size / binary_size
    packet_sha256 = hashlib.sha256(binary_sample).hexdigest()
    if packet_output is not None:
        packet_output.parent.mkdir(parents=True, exist_ok=True)
        packet_output.write_bytes(binary_sample)
    return {
        "schema": "3dsg-native-m2-wire-benchmark/v2",
        "keyframe_dir": str(keyframe_dir.resolve()),
        "frame_identity": identity,
        "golden_packet_sha256": packet_sha256,
        "golden_packet_path": None if packet_output is None else str(packet_output.resolve()),
        "point_count": int(len(xyz)),
        "hit_count": int(has_hit.sum()),
        "no_return_count": int((~has_hit).sum()),
        "repeats": repeats,
        "current_text": {
            "payload_bytes": text_size,
            "encode_median_ms": statistics.median(text_ms),
            "encode_samples_ms": text_ms,
        },
        "proposed_binary_v2": {
            "envelope_bytes": binary_size,
            "encode_median_ms": statistics.median(binary_ms),
            "encode_samples_ms": binary_ms,
            "roundtrip_exact_for_scan_and_frame_identity": True,
        },
        "text_to_binary_size_ratio": compression,
        "scope_limit": "Encoding, identity round-trip, and byte-size microbenchmark only; no native M2 parity or end-to-end timing.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyframe-dir", type=Path, default=DEFAULT_KEYFRAME)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--packet-output", type=Path, default=None,
                        help="also write the exact v2 binary packet for native parser parity")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if not args.keyframe_dir.is_dir():
        parser.error(f"keyframe directory does not exist: {args.keyframe_dir}")
    report = benchmark(args.keyframe_dir, args.repeats, args.packet_output)
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
