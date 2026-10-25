#!/usr/bin/env python3
"""Summarize per-keyframe OccuSG mapping cost and graph output for a native run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Any


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "median": median(values) if values else None,
        "p95": _percentile(values, 0.95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def audit_run(root: Path) -> dict[str, Any]:
    diagnostics = root / "native_graph/diagnostics/occusg_multisensor"
    branch = root / "native_graph/branches/rooms"
    commits_path = root / "native_graph/paired_commits.jsonl"
    commits = {}
    if commits_path.is_file():
        commit_rows = [json.loads(line) for line in commits_path.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
        commits = {int(row["frame_index"]): row for row in commit_rows}
    rows = []
    for path in sorted(diagnostics.glob("frame_*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        mapping = doc.get("mapping_record", {})
        index = int(doc["frame_index"])
        snapshot_path = branch / "snapshots" / f"rev-{index:06d}.json"
        snapshot: dict[str, Any] = {}
        if snapshot_path.is_file():
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))["snapshot"]
        nodes = snapshot.get("nodes", [])
        edges = snapshot.get("edges", [])
        commit = commits.get(index, {})
        paired_hash = (commit.get("source_hashes", {}).get("navigation_depth_m.npy")
                       if commit else None)
        aux_frame_match = (mapping.get("auxiliary_frame_id") == doc.get("frame_id")
                           and mapping.get("auxiliary_frame_index") == index)
        rows.append({
            "frame_id": doc.get("frame_id"),
            "frame_index": index,
            "primary_stride": mapping.get("primary_backprojection_stride"),
            "auxiliary_stride": mapping.get("auxiliary_backprojection_stride"),
            "primary_points": mapping.get("backprojected_hit_count"),
            "auxiliary_points": mapping.get("auxiliary_point_count"),
            "auxiliary_integrated": mapping.get("auxiliary_sensor_integrated"),
            "auxiliary_identity_matches": aux_frame_match,
            "auxiliary_hash_matches_pair": bool(paired_hash and
                                                  mapping.get("auxiliary_file_sha256") == paired_hash),
            "m1_ms": mapping.get("m1_runtime_ms"),
            "m2_ms": mapping.get("m2_runtime_ms"),
            "m3_ms": mapping.get("m3_runtime_ms"),
            "total_mapping_ms": mapping.get("m1_m2_m3_runtime_ms"),
            "m2_free_keys": mapping.get("m2_free_key_count"),
            "m2_occupied_keys": mapping.get("m2_occupied_key_count"),
            "m3_free_cells": mapping.get("m3_free_cell_count"),
            "m3_occupied_cells": mapping.get("m3_occupied_cell_count"),
            "room_nodes": sum(node.get("kind") == "room" for node in nodes),
            "navigation_nodes": sum(node.get("kind") == "navigation" for node in nodes),
            "object_nodes": sum(node.get("kind") == "object" for node in nodes),
            "edge_count": len(edges),
        })

    if not rows:
        raise SystemExit(f"no OccuSG mapping records found under {diagnostics}")

    distributions = {}
    for key in ("m1_ms", "m2_ms", "m3_ms", "total_mapping_ms", "primary_points",
                "auxiliary_points", "m2_free_keys", "m2_occupied_keys",
                "m3_free_cells", "m3_occupied_cells"):
        values = [float(row[key]) for row in rows if row[key] is not None]
        distributions[key] = _distribution(values)

    return {
        "schema": "3dsg-native-occ-mapping-profile/v1",
        "run_id": json.loads((root / "run_manifest.json").read_text(encoding="utf-8")).get("run_id"),
        "run_dir": str(root),
        "record_count": len(rows),
        "all_auxiliary_integrated": all(row["auxiliary_integrated"] is True for row in rows),
        "all_auxiliary_identity_matches": all(row["auxiliary_identity_matches"] for row in rows),
        "all_auxiliary_hashes_match_pair": all(row["auxiliary_hash_matches_pair"] for row in rows),
        "distributions": distributions,
        "frames": rows,
        "interpretation": (
            "Mapping phase times are measured within the OccuSG Python worker and can vary "
            "with accumulated map size and host contention. Node counts are structural output, "
            "not room or object accuracy."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="3DSG run directory containing native_graph diagnostics")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.run_dir.resolve(strict=True)
    result = audit_run(root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "record_count": result["record_count"],
                      "m2_ms": result["distributions"]["m2_ms"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
