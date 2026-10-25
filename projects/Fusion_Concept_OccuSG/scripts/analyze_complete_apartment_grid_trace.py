#!/usr/bin/env python3
"""Summarize the baseline M3 -> M4 -> M5 -> M8 trace without changing state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


def _load(root: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "frames").glob("*.json"))]


def _frame_metrics(root: Path, item: dict) -> dict:
    stem = f"{item['frame_index']:06d}"
    with np.load(root / "frames" / f"{stem}.npz", allow_pickle=False) as arrays:
        values = arrays["values"]
        mask = arrays["m4_preprocessed_mask"]
    return {
        "frame_index": item["frame_index"],
        "frame_id": item["frame_id"],
        "simulation_timestamp_s": item["simulation_timestamp_s"],
        "map_revision": item["map_revision"],
        "m4_status": item["m4"]["status"],
        "m4_branch": item["m4"]["branch"],
        "m5_accepted": item["m5"]["accepted"],
        "m5_stale": item["m5"]["stale"],
        "m8_room_count": len(item["m8"]["rooms"]),
        "m3_unknown_cells": int((values < 0).sum()),
        "m3_free_cells": int((values == 0).sum()),
        "m3_occupied_cells": int((values > 0).sum()),
        "m4_preprocessed_free_cells": int(mask.sum()),
        "m4_mask_on_m3_unknown_cells": int((mask & (values < 0)).sum()),
        "m4_mask_on_m3_occupied_cells": int((mask & (values > 0)).sum()),
        "m3_free_not_in_m4_mask": int(((values == 0) & ~mask).sum()),
        "m4_raw_region_count": len(item["m4"]["raw_regions"]),
        "m5_current_region_count": len(item["m5"]["current_regions"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid-trace", type=Path, required=True)
    parser.add_argument("--traced-graph", type=Path, required=True)
    parser.add_argument("--no-grid-graph", type=Path, default=None,
                        help="optional independent replay without grid export")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = _load(args.grid_trace)
    metrics = [_frame_metrics(args.grid_trace, item) for item in rows]

    def first(predicate):
        for item in metrics:
            if predicate(item):
                return item
        return None

    graph_files = ["snapshot.json", "delta_log.json", "trajectory.csv", "trajectory.json"]
    graph_hashes = {}
    for name in graph_files:
        traced = args.traced_graph / name
        item = {"traced_sha256": hashlib.sha256(traced.read_bytes()).hexdigest()}
        if args.no_grid_graph is not None:
            nongrid = args.no_grid_graph / name
            item.update({
                "no_grid_sha256": hashlib.sha256(nongrid.read_bytes()).hexdigest(),
                "equal": traced.read_bytes() == nongrid.read_bytes(),
            })
        else:
            item.update({"no_grid_sha256": None, "equal": None, "status": "not_run"})
        graph_hashes[name] = item

    first_fallback = first(lambda row: row["m4_status"] == "fallback")
    first_stale = first(lambda row: row["m5_stale"])
    first_multiple = first(lambda row: row["m4_raw_region_count"] > 1)
    report = {
        "kind": "complete_apartment_m3_m4_m5_m8_baseline_diagnostic",
        "scope": f"exact {len(metrics)}-packet offline replay; reference geometry is evaluation-only",
        "frames": len(metrics),
        "graph_export_toggle": graph_hashes,
        "source_observation": {
            "first_fallback": first_fallback,
            "first_rejection_or_stale": first_stale,
            "first_m4_multiple_raw_regions": first_multiple,
            "first_small_right_candidate": first(lambda row: any(
                float(region.get("area_m2", 0.0)) < 2.0
                and max(float(point[0]) for point in region["outer"]) > -3.0
                for region in rows[row["frame_index"]]["m4"]["raw_regions"]
            )),
        },
        "selected_frames": {
            "first_fallback": first_fallback,
            "first_stale": first_stale,
            "first_multiple_raw_regions": first_multiple,
            "final": metrics[-1] if metrics else None,
        },
        "interpretation": {
            "first_layer_with_diagonal": "inspect the selected-frame overlays; this report does not reuse frame indices from another capture",
            "m4_effect": "source preprocessing/morphology expands the binary mask beyond exact M3 free cells when the per-frame unknown-overlap count is nonzero; this is observed source behavior, not a standalone adapter rewrite",
            "m5_m8_effect": "stale frames retain previous evidence and do not create fresh observations; current/room geometry is reported per frame",
            "not_proven": "This does not establish room segmentation accuracy against ground truth, nor full source-pipeline equivalence",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
