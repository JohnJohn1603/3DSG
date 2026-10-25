#!/usr/bin/env python3
"""Replay one M2 wire golden packet through OccuSG compact and debug responses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exploration.native_m2_wire import decode_integrate_project


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _grid_summary(grid: Any) -> dict[str, Any]:
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
        **metadata,
        "array_sha256": {name: _sha256(array.tobytes())
                         for name, array in arrays.items()},
        "metadata_sha256": _sha256(_canonical_json(metadata)),
    }


def _run(packet: dict[str, Any], binary: Path, response_mode: str) -> dict[str, Any]:
    os.environ["OCCUSG_M2_RESPONSE_MODE"] = response_mode
    from standalone_sg.mapping import OctoMapWorker, ScanPoint

    xyz = packet["xyz"]
    hits = packet["has_hit"]
    points = tuple(ScanPoint(float(row[0]), float(row[1]), float(row[2]), bool(hit))
                   for row, hit in zip(xyz, hits))
    with OctoMapWorker(binary, resolution_m=0.05, timeout_s=180.0,
                       response_mode=response_mode) as worker:
        update = worker.integrate(
            points,
            origin=tuple(float(value) for value in packet["origin_xyz"]),
            max_range_m=float(packet["max_range_m"]),
        )
        grid = worker.project(mode=packet["projection_mode"])
    m2_counts = {
        "revision": update["revision"],
        "free_key_count": (update["free_key_count"] if response_mode == "compact"
                            else len(update["free_keys"])),
        "occupied_key_count": (update["occupied_key_count"] if response_mode == "compact"
                                else len(update["occupied_keys"])),
    }
    result = {"m2": m2_counts, "m3": _grid_summary(grid)}
    if response_mode == "debug":
        result["m2"]["free_keys_sha256"] = _sha256(
            np.asarray(sorted(map(tuple, update["free_keys"])), dtype="<i8").tobytes())
        result["m2"]["occupied_keys_sha256"] = _sha256(
            np.asarray(sorted(map(tuple, update["occupied_keys"])), dtype="<i8").tobytes())
    return result


def replay(packet_path: Path, binary: Path, output: Path) -> dict[str, Any]:
    packet_bytes = packet_path.read_bytes()
    packet = decode_integrate_project(packet_bytes)
    debug = _run(packet, binary, "debug")
    compact = _run(packet, binary, "compact")
    comparable_debug_m2 = {key: value for key, value in debug["m2"].items()
                           if not key.endswith("_sha256")}
    report = {
        "schema": "3dsg-occusg-m2-compact-parity/v1",
        "packet_sha256": _sha256(packet_bytes),
        "frame_id": packet["frame_id"],
        "frame_index": packet["frame_index"],
        "point_count": len(packet["xyz"]),
        "projection_mode": packet["projection_mode"],
        "m2_counts_match": comparable_debug_m2 == compact["m2"],
        "m2_free_keys_sha256_debug": debug["m2"]["free_keys_sha256"],
        "m2_occupied_keys_sha256_debug": debug["m2"]["occupied_keys_sha256"],
        "m3_exact_match": debug["m3"] == compact["m3"],
        "debug": debug,
        "compact": compact,
        "transport": "existing ASCII point input; response-mode parity only",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True,
                                 allow_nan=False) + "\n", encoding="utf-8")
    if not report["m2_counts_match"] or not report["m3_exact_match"]:
        raise RuntimeError("OccuSG compact response changed M2 counts or exact M3 state")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, default=ROOT / "runs/native-m2-wire-v2-schema-complete-20261024-a/frame_00000146.m2v2.bin")
    parser.add_argument("--binary", type=Path, default=Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/build/native-m3/occupancy/standalone_sg_octomap_fixture_runner"))
    parser.add_argument("--output", type=Path, default=ROOT / "runs/occusg-m2-compact-parity-frame146-20261024-a/report.json")
    args = parser.parse_args()
    result = replay(args.packet, args.binary, args.output)
    print(json.dumps({key: result[key] for key in (
        "packet_sha256", "frame_id", "frame_index", "point_count",
        "m2_counts_match", "m3_exact_match", "transport")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
