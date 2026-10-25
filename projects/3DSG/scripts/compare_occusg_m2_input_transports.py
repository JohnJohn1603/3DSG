#!/usr/bin/env python3
"""Exact M2 key and M3 array parity for one packet over ASCII and binary input."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.native_m2_wire import decode_integrate_project


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def integrate(packet: dict, binary: Path, transport: str) -> dict:
    os.environ["OCCUSG_M2_TRANSPORT"] = transport
    os.environ["OCCUSG_M2_RESPONSE_MODE"] = "debug"
    from standalone_sg.mapping import OctoMapWorker, ScanPoint

    points = tuple(ScanPoint(float(row[0]), float(row[1]), float(row[2]), bool(hit))
                   for row, hit in zip(packet["xyz"], packet["has_hit"]))
    identity = {
        "m2_frame_id": packet["frame_id"],
        "m2_frame_index": packet["frame_index"],
        "m2_capture_monotonic_ns": packet["capture_monotonic_ns"],
        "m2_capture_monotonic_domain": "host_monotonic:golden-packet",
        "m2_package_sha256": packet["package_sha256"],
    }
    with OctoMapWorker(binary, resolution_m=0.05, timeout_s=180.0,
                       response_mode="debug", transport_mode=transport) as worker:
        update = worker.integrate(
            points,
            origin=tuple(float(v) for v in packet["origin_xyz"]),
            max_range_m=float(packet["max_range_m"]),
            frame_identity=identity,
            projection_mode=packet["projection_mode"],
        )
        grid = worker.project(mode=packet["projection_mode"])
    arrays = {
        "values": np.ascontiguousarray(grid.values),
        "z_floor": np.ascontiguousarray(grid.z_floor, dtype="<f8"),
        "z_ceiling": np.ascontiguousarray(grid.z_ceiling, dtype="<f8"),
    }
    metadata = {
        "revision": grid.revision,
        "resolution_m": grid.resolution_m,
        "origin_xy": list(grid.origin_xy),
        "shape": list(grid.values.shape),
        "changed_bounds_xy": grid.changed_bounds_xy,
        "changed_bounds_xyz": grid.changed_bounds_xyz,
        "projection_mode": grid.projection_mode,
        "updated": grid.updated,
        "backend": grid.backend,
    }
    return {
        "m2": {
            "revision": update["revision"],
            "free_key_count": len(update["free_keys"]),
            "occupied_key_count": len(update["occupied_keys"]),
            "free_keys_sha256": sha(np.asarray(
                sorted(map(tuple, update["free_keys"])), dtype="<i8").tobytes()),
            "occupied_keys_sha256": sha(np.asarray(
                sorted(map(tuple, update["occupied_keys"])), dtype="<i8").tobytes()),
        },
        "m3": {
            **metadata,
            "array_sha256": {name: sha(array.tobytes())
                             for name, array in arrays.items()},
            "metadata_sha256": sha(canonical(metadata)),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet_bytes = args.packet.read_bytes()
    packet = decode_integrate_project(packet_bytes)
    ascii_result = integrate(packet, args.binary, "ascii")
    binary_result = integrate(packet, args.binary, "binary")
    report = {
        "schema": "3dsg-occusg-m2-input-transport-parity/v1",
        "packet_sha256": sha(packet_bytes),
        "frame_id": packet["frame_id"],
        "frame_index": packet["frame_index"],
        "point_count": int(packet["xyz"].shape[0]),
        "ascii_equals_binary_m2": ascii_result["m2"] == binary_result["m2"],
        "ascii_equals_binary_m3": ascii_result["m3"] == binary_result["m3"],
        "ascii": ascii_result,
        "binary": binary_result,
    }
    if not report["ascii_equals_binary_m2"] or not report["ascii_equals_binary_m3"]:
        raise RuntimeError("ASCII and binary OccuSG M2/M3 states differ")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "packet_sha256", "frame_id", "point_count", "ascii_equals_binary_m2",
        "ascii_equals_binary_m3")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
