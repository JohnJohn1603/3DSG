#!/usr/bin/env python3
"""Compare paired ASCII and binary OccuSG replays without timing metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


SEMANTIC_FIELDS = (
    "frame_id", "frame_index", "m2_revision", "m2_free_key_count",
    "m2_occupied_key_count", "m2_hit_scan_point_count",
    "m2_no_return_scan_point_count", "m3_revision", "m3_backend",
    "m3_mode", "m3_shape", "m3_origin_xy", "m3_free_cell_count",
    "m3_occupied_cell_count", "m3_unknown_cell_count",
    "m3_changed_bounds_xyz", "m3_source_oracle",
    "auxiliary_sensor_integrated", "auxiliary_sensor_id",
    "auxiliary_frame_id", "auxiliary_frame_index", "auxiliary_file_sha256",
    "auxiliary_calibration_sha256", "auxiliary_point_count",
    "auxiliary_m2_free_key_count", "auxiliary_m2_occupied_key_count",
)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mapping_rows(run: Path) -> dict[int, dict]:
    folder = run / "diagnostics/occusg_multisensor"
    rows = {}
    for path in sorted(folder.glob("frame_*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        record = doc["mapping_record"]
        rows[int(record["frame_index"])] = record
    return rows


def paired_sources(run: Path) -> list[dict]:
    return [json.loads(line)["source_hashes"] for line in
            (run / "paired_commits.jsonl").read_text(encoding="utf-8").splitlines()]


def snapshot_hashes(run: Path) -> dict[str, str]:
    room_path = run / "branches/rooms/snapshots/rev-000462.json"
    room_doc = json.loads(room_path.read_text(encoding="utf-8"))
    room = room_doc["snapshot"]
    # Strip transport-only provenance added by binary requests. Keep all
    # object/room geometry, identity, adjacency, lifecycle and source hashes.
    for node in room.get("nodes", []):
        attributes = node.get("attributes", {})
        metadata = attributes.get("source_metadata", {})
        metadata.pop("m2_identity", None)
        auxiliary = metadata.get("auxiliary_depth", {})
        if isinstance(auxiliary, dict):
            auxiliary.pop("m2_identity", None)
    room.pop("timestamp_s", None)
    object_doc = json.loads(
        (run / "branches/objects/snapshots/rev-000462.json").read_text(encoding="utf-8"))
    return {"rooms_semantic": canonical_hash(room),
            "objects": canonical_hash(object_doc)}


def run(ascii_run: Path, binary_run: Path) -> dict:
    ascii_rows = mapping_rows(ascii_run)
    binary_rows = mapping_rows(binary_run)
    if ascii_rows.keys() != binary_rows.keys():
        raise ValueError("ASCII and binary replay frame sets differ")
    if paired_sources(ascii_run) != paired_sources(binary_run):
        raise ValueError("source frame hashes differ between replays")
    frame_reports = []
    for frame_index in ascii_rows:
        left, right = ascii_rows[frame_index], binary_rows[frame_index]
        a = {name: left.get(name) for name in SEMANTIC_FIELDS}
        b = {name: right.get(name) for name in SEMANTIC_FIELDS}
        if a != b:
            different = [name for name in SEMANTIC_FIELDS if a[name] != b[name]]
            raise ValueError(f"semantic mapping mismatch at frame {frame_index}: {different}")
        if left.get("auxiliary_sensor_integrated") is not True or right.get("auxiliary_sensor_integrated") is not True:
            raise ValueError(f"auxiliary depth missing at frame {frame_index}")
        frame_reports.append({
            "frame_index": frame_index,
            "frame_id": left["frame_id"],
            "m2_free_key_count": left.get("m2_free_key_count"),
            "m2_occupied_key_count": left.get("m2_occupied_key_count"),
            "m3_shape": left.get("m3_shape"),
            "m3_source_oracle_value_mismatches": left.get("m3_source_oracle", {}).get("value_mismatches"),
            "auxiliary_depth_sha256": left.get("auxiliary_file_sha256"),
        })
    ascii_hashes, binary_hashes = snapshot_hashes(ascii_run), snapshot_hashes(binary_run)
    if ascii_hashes != binary_hashes:
        raise ValueError(f"final graph semantic hash mismatch: {ascii_hashes} != {binary_hashes}")
    ascii_summary = json.loads((ascii_run / "summary.json").read_text(encoding="utf-8"))
    binary_summary = json.loads((binary_run / "summary.json").read_text(encoding="utf-8"))
    if ascii_summary["final_graph"] != binary_summary["final_graph"]:
        raise ValueError("final graph counts differ")
    return {
        "schema": "3dsg-occusg-ascii-binary-replay-parity/v1",
        "ascii_run": str(ascii_run),
        "binary_run": str(binary_run),
        "frame_count": len(frame_reports),
        "source_hashes_match": True,
        "mapping_records_match": True,
        "final_graph_counts_match": True,
        "final_graph_hashes": ascii_hashes,
        "final_graph": binary_summary["final_graph"],
        "frames": frame_reports,
        "input_transport": {"ascii": "legacy text point lines", "binary": "3SGM v2 bounded request"},
        "note": "M2 key-set hashes are directly parity-checked on the golden packet and exact M2 map snapshot on worker fixtures; this sequence compares compact key counts and M3 projection oracle state.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ascii-run", type=Path, required=True)
    parser.add_argument("--binary-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.ascii_run, args.binary_run)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
