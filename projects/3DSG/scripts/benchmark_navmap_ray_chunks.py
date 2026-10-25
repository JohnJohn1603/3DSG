#!/usr/bin/env python3
"""Compare bounded NavMap ray batch sizes on saved RGB-D tails.

This diagnostic replays one saved depth tail on each saved map. It reports the
update latency and checks exact occupancy-state, floor-support, clearance, and
grid geometry parity against the 512-ray reference. Log-odds are compared with
a small tolerance because batching changes floating-point addition grouping.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import statistics
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.profile_saved_navmaps import _load_snapshot, _mapper_replay


def _summary(seconds: list[float]) -> dict[str, float]:
    ordered = sorted(seconds)
    return {
        "median_ms": 1000.0 * statistics.median(seconds),
        "p95_ms": 1000.0 * ordered[min(len(ordered) - 1, int(np.ceil(0.95 * len(ordered))) - 1)],
    }


def _same_geometry(left, right) -> bool:
    return (left.shape == right.shape
            and left.origin_x_m == right.origin_x_m
            and left.origin_y_m == right.origin_y_m
            and left.resolution_m == right.resolution_m)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--stride", type=int, default=8)
    parser.add_argument("--chunk-size", type=int, action="append", default=[256, 512, 1024, 2048])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1 or args.stride < 1 or any(value < 1 for value in args.chunk_size):
        parser.error("repeats, stride, and chunk sizes must be positive")

    runs = []
    for run_id in args.run_id:
        run_dir = args.runs_root / run_id
        snapshot, live, meta = _load_snapshot(run_dir)
        chunk_sizes = list(dict.fromkeys(args.chunk_size))
        if 512 not in chunk_sizes:
            parser.error("512 must be included as the semantic-parity reference")
        measured_samples = {size: [] for size in chunk_sizes}
        unique_samples = {size: [] for size in chunk_sizes}
        replay_snapshots = {}
        rng = random.Random(f"{run_id}:ray-chunk-order")

        # Warm each configuration once, then randomize the order in every
        # measured round to reduce cache/host-load ordering bias.
        for chunk_size in chunk_sizes:
            _mapper_replay(snapshot, live, meta, run_dir, args.stride, 1,
                           ray_chunk_size=chunk_size)
        for _round in range(args.repeats):
            order = chunk_sizes.copy()
            rng.shuffle(order)
            for chunk_size in order:
                snapshots = []
                unique_timing = {"seconds": 0.0, "calls": 0}
                original_unique = np.unique

                def timed_unique(*call_args, **call_kwargs):
                    started = time.perf_counter()
                    try:
                        return original_unique(*call_args, **call_kwargs)
                    finally:
                        unique_timing["seconds"] += time.perf_counter() - started
                        unique_timing["calls"] += 1

                np.unique = timed_unique
                try:
                    seconds = _mapper_replay(snapshot, live, meta, run_dir, args.stride,
                                             1, ray_chunk_size=chunk_size,
                                             snapshots_out=snapshots)
                finally:
                    np.unique = original_unique
                measured_samples[chunk_size].append(seconds[0])
                unique_samples[chunk_size].append(unique_timing)
                replay_snapshots[chunk_size] = snapshots[-1]
        timings = {str(size): _summary(measured_samples[size]) for size in chunk_sizes}
        unique_timings = {
            str(size): {
                "time_per_update_ms": _summary([sample["seconds"]
                                                for sample in unique_samples[size]])["median_ms"],
                "calls_per_update_median": statistics.median(
                    sample["calls"] for sample in unique_samples[size]),
            }
            for size in chunk_sizes
        }
        reference = replay_snapshots[512]
        phase_samples = []
        _mapper_replay(snapshot, live, meta, run_dir, args.stride,
                       args.repeats, ray_chunk_size=512,
                       phase_samples_out=phase_samples)
        phase_timings = {
            key: _summary([sample[key] for sample in phase_samples])
            for key in phase_samples[0]
        }
        chunk_results = {}
        parity = {}
        for chunk_size, result in replay_snapshots.items():
            parity[str(chunk_size)] = {
                "grid_geometry_exact": _same_geometry(reference, result),
                "states_exact": bool(np.array_equal(reference.states, result.states)),
                "floor_support_exact": bool(np.array_equal(reference.floor_support, result.floor_support)),
                "clearance_exact": bool(np.array_equal(reference.clearance_evidence,
                                                        result.clearance_evidence)),
                "spawn_validated_exact": bool(np.array_equal(reference.spawn_validated,
                                                              result.spawn_validated)),
                "occupancy_log_odds_max_abs_delta": float(np.max(np.abs(
                    reference.occupancy_log_odds.astype(np.float64)
                    - result.occupancy_log_odds.astype(np.float64)))),
            }
            chunk_results[str(chunk_size)] = {
                "state_counts": {
                    "unknown": int(np.count_nonzero(result.states == 0)),
                    "free": int(np.count_nonzero(result.states == 1)),
                    "occupied": int(np.count_nonzero(result.states == 2)),
                }
            }
        runs.append({"run_id": run_id, "shape": list(snapshot.shape),
                     "timings": timings, "phase_timings_at_chunk_512": phase_timings,
                     "np_unique_at_chunk_size": unique_timings,
                     "replay": chunk_results, "parity_vs_512": parity})
        print(f"{run_id}: " + ", ".join(
            f"{size}={timings[str(size)]['median_ms']:.1f}ms" for size in timings))

    result = {
        "schema": "3dsg.navmap_ray_chunk_benchmark.v1",
        "stride": args.stride,
        "repeats": args.repeats,
        "ordering": "one warmup per chunk size; seeded random order per measured round",
        "chunk_sizes": list(dict.fromkeys(args.chunk_size)),
        "runs": runs,
        "all_exact_semantic_parity": all(
            all(row[key] for key in ("grid_geometry_exact", "states_exact",
                                     "floor_support_exact", "clearance_exact",
                                     "spawn_validated_exact"))
            and row["occupancy_log_odds_max_abs_delta"] <= 1e-5
            for run in runs for row in run["parity_vs_512"].values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"parity={'PASS' if result['all_exact_semantic_parity'] else 'FAIL'} output={args.output}")
    return 0 if result["all_exact_semantic_parity"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
