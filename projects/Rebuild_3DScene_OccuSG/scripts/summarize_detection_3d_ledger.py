#!/usr/bin/env python3
"""Select auditable accepted/rejected P3 examples by class and geometry."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def run(ledger: Path, output: Path, *, per_group: int = 2) -> dict:
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(str(row.get("class_canonical")), str(row.get("decision")))].append(row)
    selected = []
    for (class_name, decision), items in sorted(groups.items()):
        items = sorted(items, key=lambda row: (
            int(row.get("largest_cluster_points", row.get("cluster_points", 0))),
            int(row.get("mask_pixel_count", 0)), str(row.get("detection_id")),
        ))
        picks = items[:per_group]
        if len(items) > per_group:
            picks += items[-per_group:]
        unique_picks = []
        seen = set()
        for row in picks:
            key = str(row.get("detection_id"))
            if key in seen:
                continue
            seen.add(key)
            unique_picks.append(row)
        for row in unique_picks:
            selected.append({
                "class_canonical": class_name,
                "decision": decision,
                "frame_id": row.get("frame_id"),
                "detection_id": row.get("detection_id"),
                "mask_pixel_count": row.get("mask_pixel_count"),
                "valid_depth_pixels": row.get("valid_depth_pixels"),
                "depth_min_m": row.get("depth_min_m"),
                "depth_max_m": row.get("depth_max_m"),
                "backprojected_points": row.get("backprojected_points"),
                "cluster_count": row.get("cluster_count"),
                "largest_cluster_points": row.get("largest_cluster_points", row.get("cluster_points")),
                "largest_cluster_diameter_m": row.get("largest_cluster_diameter_m"),
                "reason": row.get("reason"),
                "rejection_overlay": row.get("rejection_overlay"),
            })
    report = {
        "schema_version": 1,
        "kind": "p3_accepted_rejected_sample_report",
        "ledger": str(ledger),
        "row_count": len(rows),
        "group_counts": {f"{key[0]}::{key[1]}": len(value) for key, value in sorted(groups.items())},
        "samples": selected,
        "selection_policy": "smallest and largest largest-cluster counts per class/decision",
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-group", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(run(args.ledger.resolve(), args.output.resolve(), per_group=args.per_group),
                     indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
