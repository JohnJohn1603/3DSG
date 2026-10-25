#!/usr/bin/env python3
"""Compare fresh complete-apartment replays without confusing IDs with geometry.

This is an evaluation tool only. It never feeds a trace back into the pipeline.
For trace-enabled runs it reports the first differing layer in the order
M3 -> M4 mask -> M4 geometry/adjacency -> M5 -> M8, and separately records
ID/order-only differences.  Trace-disabled runs are compared at the
frame-diagnostics and final graph level because their intermediate layers were
not persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

import numpy as np


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _num(value: Any) -> float:
    return round(float(value), 6)


def _ring_key(points: list[list[float]]) -> tuple[tuple[float, float], ...]:
    """Canonicalize a polygon ring for start-point/orientation-independent comparison."""
    pts = [(_num(p[0]), _num(p[1])) for p in points]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    if not pts:
        return ()
    candidates = []
    for sequence in (pts, list(reversed(pts))):
        for index in range(len(sequence)):
            candidates.append(tuple(sequence[index:] + sequence[:index]))
    return min(candidates)


def _region_key(region: dict) -> tuple:
    polygon = region.get("polygon", region)
    return _ring_key(polygon.get("outer", []))


def _m4_geometry(item: dict) -> tuple:
    regions = item["m4"]["raw_regions"]
    return tuple(sorted(_region_key(region) for region in regions))


def _m4_adjacency(item: dict) -> tuple:
    regions = item["m4"]["raw_regions"]
    by_id = {int(region["raw_id"]): _region_key(region) for region in regions}
    edges = set()
    for region in regions:
        source = by_id[int(region["raw_id"])]
        for raw_target in region.get("adjacent_raw_ids", []):
            target = by_id.get(int(raw_target))
            if target is not None and source != target:
                edges.add(tuple(sorted((source, target))))
    return tuple(sorted(edges))


def _m5_geometry(item: dict) -> tuple:
    return tuple(sorted(_region_key(region) for region in item["m5"]["current_regions"]))


def _m5_adjacency(item: dict) -> tuple:
    regions = item["m5"]["current_regions"]
    by_id = {int(region["persistent_id"]): _region_key(region) for region in regions}
    edges = set()
    for region in regions:
        source = by_id[int(region["persistent_id"])]
        for target_id in region.get("adjacent_region_ids", []):
            target = by_id.get(int(target_id))
            if target is not None and source != target:
                edges.add(tuple(sorted((source, target))))
    return tuple(sorted(edges))


def _m8_geometry(item: dict) -> tuple:
    return tuple(sorted(_region_key(room) for room in item["m8"]["rooms"]))


def _m8_adjacency(item: dict) -> tuple:
    rooms = item["m8"]["rooms"]
    by_id = {str(room.get("room_id", room.get("id", ""))): _region_key(room) for room in rooms}
    edges = set()
    for edge in item["m8"].get("room_edges", []):
        source = by_id.get(str(edge.get("source_id", edge.get("source", ""))))
        target = by_id.get(str(edge.get("target_id", edge.get("target", ""))))
        if source is not None and target is not None and source != target:
            edges.add(tuple(sorted((source, target))))
    return tuple(sorted(edges))


def _load_trace(root: Path) -> tuple[list[dict], dict]:
    trace = root / "m3_m4_grid_trace"
    index = _json(trace / "index.json")
    frames = [_json(trace / entry["metadata_file"]) for entry in index["frames"]]
    return frames, _json(root / "graph" / "manifest.json")


def _load_diag(root: Path) -> tuple[list[dict], dict]:
    graph = root / "graph"
    return _json(graph / "frame_diagnostics.json"), _json(graph / "manifest.json")


def _array_equal(left: Path, right: Path, name: str) -> bool:
    with np.load(left, allow_pickle=False) as a, np.load(right, allow_pickle=False) as b:
        return np.array_equal(a[name], b[name], equal_nan=True)


def _trace_first_difference(left: Path, right: Path) -> dict | None:
    left_frames, _ = _load_trace(left)
    right_frames, _ = _load_trace(right)
    for index, (a, b) in enumerate(zip(left_frames, right_frames)):
        la = left / "m3_m4_grid_trace" / "frames" / a["array_file"]
        lb = right / "m3_m4_grid_trace" / "frames" / b["array_file"]
        for array_name in ("values", "z_floor", "z_ceiling"):
            if not _array_equal(la, lb, array_name):
                return {"frame_index": index, "layer": "M3_grid", "detail": array_name}
        if not _array_equal(la, lb, "m4_preprocessed_mask"):
            return {"frame_index": index, "layer": "M4_mask", "detail": "m4_preprocessed_mask"}
        if _m4_geometry(a) != _m4_geometry(b):
            return {"frame_index": index, "layer": "M4_raw_geometry", "detail": "geometry differs"}
        if _m4_adjacency(a) != _m4_adjacency(b):
            return {"frame_index": index, "layer": "M4_raw_adjacency", "detail": "geometry-mapped adjacency differs"}
        if [int(r["raw_id"]) for r in a["m4"]["raw_regions"]] != [int(r["raw_id"]) for r in b["m4"]["raw_regions"]]:
            return {"frame_index": index, "layer": "M4_id_order_only", "detail": "raw region ID/order differs"}
        if _m5_geometry(a) != _m5_geometry(b):
            return {"frame_index": index, "layer": "M5_geometry", "detail": "current region geometry differs"}
        if _m5_adjacency(a) != _m5_adjacency(b):
            return {"frame_index": index, "layer": "M5_adjacency", "detail": "geometry-mapped adjacency differs"}
        if _m8_geometry(a) != _m8_geometry(b):
            return {"frame_index": index, "layer": "M8_room_geometry", "detail": "room footprint differs"}
        if _m8_adjacency(a) != _m8_adjacency(b):
            return {"frame_index": index, "layer": "M8_room_adjacency", "detail": "room relationship differs"}
    if len(left_frames) != len(right_frames):
        return {"frame_index": min(len(left_frames), len(right_frames)), "layer": "trace_length", "detail": "frame count differs"}
    return None


def _trace_id_order_only(left: Path, right: Path) -> list[int]:
    a, _ = _load_trace(left)
    b, _ = _load_trace(right)
    out = []
    for index, (x, y) in enumerate(zip(a, b)):
        if (_m4_geometry(x) == _m4_geometry(y) and _m4_adjacency(x) == _m4_adjacency(y)
                and x["m4"].get("raw_regions") != y["m4"].get("raw_regions")):
            out.append(index)
    return out


def _graph_terminal(root: Path) -> dict:
    graph = root / "graph"
    delta_payload = _json(graph / "delta_log.json")
    delta = delta_payload.get("deltas", delta_payload) if isinstance(delta_payload, dict) else delta_payload
    last = delta[-1] if delta else None
    return {
        "snapshot_sha256": _hash(graph / "snapshot.json"),
        "delta_sha256": _hash(graph / "delta_log.json"),
        "last_delta_event": last.get("events") if isinstance(last, dict) else None,
        "last_delta_revision": last.get("new_revision") if isinstance(last, dict) else None,
        "manifest_graph": _json(graph / "manifest.json").get("graph", {}),
    }


def _trace_summary(root: Path) -> dict:
    frames, manifest = _load_trace(root)
    return {
        "path": str(root),
        "frames": len(frames),
        "policy": manifest.get("dude_preprocessing_policy"),
        "final_graph": manifest.get("graph", {}),
        "m4_branches": sorted({item["m4"]["branch"] for item in frames}),
        "m5_stale_frames": sum(bool(item["m5"].get("stale")) for item in frames),
        "terminal": _graph_terminal(root),
    }


def _diag_summary(root: Path) -> dict:
    diagnostics, manifest = _load_diag(root)
    return {
        "path": str(root),
        "frames": len(diagnostics),
        "policy": manifest.get("dude_preprocessing_policy"),
        "final_graph": manifest.get("graph", {}),
        "m4_branch_sequence_sha256": hashlib.sha256(json.dumps(
            [(d.get("m4_branch"), d.get("m4_status")) for d in diagnostics],
            separators=(",", ":"), sort_keys=True).encode()).hexdigest(),
        "terminal": _graph_terminal(root),
    }


def _pairwise(paths: list[Path], traced: bool) -> list[dict]:
    rows = []
    for left, right in itertools.combinations(paths, 2):
        if traced:
            first = _trace_first_difference(left, right)
            id_only = _trace_id_order_only(left, right)
            rows.append({"left": str(left), "right": str(right), "first_difference": first,
                         "id_order_only_frames": id_only[:20], "id_order_only_count": len(id_only),
                         "left_terminal": _graph_terminal(left), "right_terminal": _graph_terminal(right)})
        else:
            a, _ = _load_diag(left)
            b, _ = _load_diag(right)
            # Runtime/RSS sampling is intentionally excluded: it is not a
            # pipeline output and differs between fresh processes.
            def semantic(value: Any) -> Any:
                # Runtime and RSS are measurements, not graph semantics.  The
                # mapping record nests them, so remove them recursively.
                if isinstance(value, dict):
                    return {
                        key: semantic(item) for key, item in value.items()
                        if key not in {"processing_runtime_ms", "sampled_python_ru_maxrss_kib"}
                        and not key.endswith("_runtime_ms")
                    }
                if isinstance(value, list):
                    return [semantic(item) for item in value]
                return value
            first = next((i for i, (x, y) in enumerate(zip(a, b))
                          if semantic(x) != semantic(y)), None)
            rows.append({"left": str(left), "right": str(right),
                         "first_diagnostic_difference_frame": first,
                         "left_summary": _diag_summary(left), "right_summary": _diag_summary(right)})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, action="append", default=[])
    parser.add_argument("--no-grid", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.trace) < 2 and len(args.no_grid) < 2:
        parser.error("provide at least two trace or no-grid runs")
    report = {
        "kind": "complete_apartment_source_repeatability",
        "comparison_order": ["M3_grid", "M4_mask", "M4_raw_geometry", "M4_raw_adjacency", "M5", "M8", "terminal_flush"],
        "trace_runs": [_trace_summary(path) for path in args.trace],
        "no_grid_runs": [_diag_summary(path) for path in args.no_grid],
        "trace_pairwise": _pairwise(args.trace, True),
        "no_grid_pairwise": _pairwise(args.no_grid, False),
        "interpretation": {
            "id_order_only": "A difference is classified as ID/order-only only when normalized polygon geometry and geometry-mapped adjacency are equal.",
            "terminal_flush": "Final snapshot/delta hashes and the last delta event are reported separately; a frame-level match is not enough.",
            "nondeterminism": "A repeatability difference is evidence of a fresh-process output difference, not yet a root-cause attribution to native nondeterminism.",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
