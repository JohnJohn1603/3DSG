#!/usr/bin/env python3
"""Compare two complete_apartment replays on the same captured packets."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np


def _load(root: Path) -> tuple[list[dict], list[dict], dict]:
    index = json.loads((root / "m3_m4_grid_trace" / "index.json").read_text())
    entries = index["frames"]
    metadata = [json.loads((root / "m3_m4_grid_trace" / item["metadata_file"]).read_text()) for item in entries]
    diagnostics = json.loads((root / "graph" / "frame_diagnostics.json").read_text())
    manifest = json.loads((root / "graph" / "manifest.json").read_text())
    if len(entries) != len(diagnostics):
        raise ValueError("grid trace and frame diagnostics lengths differ")
    return metadata, diagnostics, manifest


def _profile(root: Path) -> tuple[dict, list[int]]:
    metadata, diagnostics, manifest = _load(root)
    unknown_overlap = []
    for item in metadata:
        with np.load(root / "m3_m4_grid_trace" / "frames" / item["array_file"], allow_pickle=False) as arrays:
            unknown_overlap.append(int(np.count_nonzero(
                arrays["m4_preprocessed_mask"] & (arrays["values"] < 0)
            )))
    return {
        "path": str(root),
        "policy": manifest.get("dude_preprocessing_policy", "source"),
        "frames": len(metadata),
        "final_graph": manifest["graph"],
        "runtime": manifest["runtime"],
        "m4_branches": dict(Counter(item["m4_branch"] for item in diagnostics)),
        "m4_statuses": dict(Counter(item["m4_status"] for item in diagnostics)),
        "stale_frames": sum(bool(item["m5_stale"]) for item in diagnostics),
        "unknown_overlap": {
            "total_cells": sum(unknown_overlap),
            "max_cells_in_frame": max(unknown_overlap),
            "frames_with_overlap": sum(value > 0 for value in unknown_overlap),
        },
    }, unknown_overlap


def compare(source: Path, extended: Path) -> dict:
    source_meta, _source_diag, _source_manifest = _load(source)
    extended_meta, _extended_diag, _extended_manifest = _load(extended)
    if len(source_meta) != len(extended_meta):
        raise ValueError("profiles do not contain the same number of frames")
    m3_equal = True
    first_difference = None
    for index, (left, right) in enumerate(zip(source_meta, extended_meta)):
        if left["frame_id"] != right["frame_id"] or left["map_revision"] != right["map_revision"]:
            raise ValueError(f"frame/revision mismatch at index {index}")
        for name in ("values", "z_floor", "z_ceiling"):
            with np.load(source / "m3_m4_grid_trace" / "frames" / left["array_file"], allow_pickle=False) as a, \
                 np.load(extended / "m3_m4_grid_trace" / "frames" / right["array_file"], allow_pickle=False) as b:
                equal = np.array_equal(a[name], b[name], equal_nan=True)
            if not equal:
                m3_equal = False
                first_difference = {"frame_index": index, "array": name}
                break
        if first_difference is not None:
            break
    source_profile, source_overlap = _profile(source)
    extended_profile, extended_overlap = _profile(extended)
    return {
        "scope": "same captured RGB-D packets; M1-M3 and native mapping are compared before the M4 policy split",
        "source_profile": source_profile,
        "extended_profile": extended_profile,
        "m3_arrays_equal_all_frames": m3_equal,
        "m3_first_difference": first_difference,
        "unknown_overlap_reduction": {
            "source_total": sum(source_overlap),
            "extended_total": sum(extended_overlap),
            "source_max": max(source_overlap),
            "extended_max": max(extended_overlap),
            "source_frames": sum(value > 0 for value in source_overlap),
            "extended_frames": sum(value > 0 for value in extended_overlap),
        },
        "quality_claim": (
            "No room IoU/accuracy claim: no independently verified room GT is supplied to the builder. "
            "The demonstrated improvement is elimination of M4 free-mask cells on M3 unknown cells; "
            "room-count changes remain an evaluation result, not proof of accuracy."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--extended", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.source, args.extended)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
