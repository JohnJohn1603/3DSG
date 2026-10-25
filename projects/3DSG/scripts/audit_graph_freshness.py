#!/usr/bin/env python3
"""Measure live graph age from shared monotonic event timestamps."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def audit(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve(strict=True)
    rows = read_events(run_dir / "events.jsonl")
    ticks = [row for row in rows if row.get("event") == "navigation_tick"]
    publishes = [row for row in rows if row.get("event") == "graph_revision_published"]
    if not ticks:
        raise ValueError("run contains no navigation_tick events")
    if not publishes:
        raise ValueError("run contains no graph_revision_published events")
    domains = {row.get("host_monotonic_clock") for row in (*ticks, *publishes)}
    if len(domains) != 1 or None in domains:
        raise ValueError(f"navigation and graph events do not share one monotonic clock: {domains}")

    publishes.sort(key=lambda row: float(row["host_monotonic_s"]))
    cursor = 0
    latest = None
    age_s: list[float] = []
    frame_lag: list[float] = []
    missing_ticks = 0
    for tick in sorted(ticks, key=lambda row: float(row["host_monotonic_s"])):
        tick_s = float(tick["host_monotonic_s"])
        while cursor < len(publishes) and float(publishes[cursor]["host_monotonic_s"]) <= tick_s:
            latest = publishes[cursor]
            cursor += 1
        if latest is None:
            missing_ticks += 1
            continue
        age = tick_s - float(latest["host_monotonic_s"])
        lag = int(tick["frame_index"]) - int(latest["frame_index"])
        if age < -1e-6 or lag < 0:
            raise ValueError("graph publish is later/newer than the navigation tick")
        age_s.append(max(0.0, age))
        frame_lag.append(float(lag))

    shutdown = next((row for row in reversed(rows)
                     if row.get("event") == "graph_worker_shutdown"), None)
    summary_path = run_dir / "run_summary.json"
    run_summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    final_tick = max(ticks, key=lambda row: float(row["host_monotonic_s"]))
    active_publications = [row for row in publishes
                           if float(row["host_monotonic_s"])
                           <= float(final_tick["host_monotonic_s"])]
    terminal_publication = publishes[-1]
    return {
        "schema": "3dsg-graph-freshness-audit/v1",
        "run_id": run_summary.get("run_id", run_dir.name),
        "clock_domain": next(iter(domains)),
        "navigation_ticks": len(ticks),
        "ticks_without_prior_graph_revision": missing_ticks,
        "active_graph_revisions": len(active_publications),
        "graph_age_s": {
            "median": percentile(age_s, 0.50),
            "p95": percentile(age_s, 0.95),
            "max": max(age_s) if age_s else None,
            "samples": len(age_s),
        },
        "camera_to_graph_frame_lag": {
            "median": percentile(frame_lag, 0.50),
            "p95": percentile(frame_lag, 0.95),
            "max": max(frame_lag) if frame_lag else None,
            "unit": "frame indices; not converted to seconds",
        },
        "last_active_tick": {
            "frame_index": final_tick["frame_index"],
            "host_monotonic_s": final_tick["host_monotonic_s"],
            "latest_graph_frame_at_tick": (active_publications[-1]["frame_index"]
                                            if active_publications else None),
        },
        "last_graph_publication_including_shutdown_drain": {
            "frame_index": terminal_publication["frame_index"],
            "host_monotonic_s": terminal_publication["host_monotonic_s"],
        },
        "graph_worker_ok": run_summary.get("graph_worker_ok"),
        "shutdown_pending_frame_ids": (shutdown or {}).get(
            "pending_keyframes_preserved_for_replay", []),
        "interpretation": "Age is measured directly from event monotonic timestamps. Frame lag is reported as an index difference only; sparse keyframe indices are not a time unit.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.run_dir)
    encoded = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
