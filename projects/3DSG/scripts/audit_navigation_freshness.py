#!/usr/bin/env python3
"""Summarize capture-to-map freshness and worker timing from a saved run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys


def _percentiles(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median_ms": 1000.0 * statistics.median(values),
        "p95_ms": 1000.0 * ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))],
        "max_ms": 1000.0 * ordered[-1],
    }


def _frame_percentiles(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0, "median_frames": None, "p95_frames": None, "max_frames": None}
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median_frames": statistics.median(values),
        "p95_frames": ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))],
        "max_frames": ordered[-1],
    }


def _frame_index(frame_id: str | None) -> int | None:
    if not frame_id:
        return None


def _resource_cpu_by_process(run_dir: Path, root_pid: int | None) -> list[dict[str, object]]:
    sample_path = run_dir / "diagnostics" / "resource_samples.jsonl"
    if not sample_path.exists():
        return []
    roles: dict[int, str] = {}
    native_dir = run_dir / "native_graph"
    if native_dir.exists():
        for ready_path in native_dir.glob("ready/*.json"):
            try:
                record = json.loads(ready_path.read_text(encoding="utf-8"))
                roles[int(record["pid"])] = str(record.get("kind", ready_path.stem))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        perception_ready = native_dir / "native_perception" / "ready.json"
        if perception_ready.exists():
            try:
                record = json.loads(perception_ready.read_text(encoding="utf-8"))
                roles[int(record["pid"])] = str(record.get("protocol", "native_perception"))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
    totals: dict[int, dict[str, object]] = {}
    for line in sample_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sample = json.loads(line)
        for process in sample.get("process_cpu_seconds_delta_by_process", []):
            pid = int(process["pid"])
            row = totals.setdefault(pid, {"pid": pid, "name": str(process.get("name", "unknown")),
                                          "cpu_seconds": 0.0})
            row["cpu_seconds"] = float(row["cpu_seconds"]) + float(process["cpu_seconds"])
    for pid, row in totals.items():
        role = roles.get(pid)
        if pid == root_pid:
            role = "habitat_runner"
        elif str(row["name"]).startswith("standalone_sg_o"):
            role = "occusg_native_m2_m3"
        elif str(row["name"]).startswith("occusg_dude_wor"):
            role = "occusg_dude"
        row["role"] = role or "unclassified_process"
    return sorted(totals.values(), key=lambda row: float(row["cpu_seconds"]), reverse=True)
    try:
        return int(frame_id.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def audit(run_dir: Path) -> dict[str, object]:
    events_path = run_dir / "events.jsonl"
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    ticks = [row for row in rows if row.get("event") == "navigation_tick"]
    if not ticks:
        raise ValueError(f"no navigation_tick events in {events_path}")

    capture_time_by_frame = {
        row["frame_id"]: float(row["capture_host_monotonic_s"])
        for row in ticks if row.get("frame_id") and row.get("capture_host_monotonic_s") is not None
    }
    map_age: list[float] = []
    map_frame_lag: list[float] = []
    unpaired_map_frames = 0
    stage_samples: dict[str, list[float]] = {}
    for row in ticks:
        current_time = row.get("capture_host_monotonic_s")
        map_frame_id = row.get("map_as_of_frame_id")
        source_time = capture_time_by_frame.get(map_frame_id)
        if current_time is not None and source_time is not None:
            map_age.append(max(0.0, float(current_time) - source_time))
        elif map_frame_id and map_frame_id != "uninitialized":
            unpaired_map_frames += 1
        current_index = _frame_index(row.get("frame_id"))
        source_index = _frame_index(map_frame_id)
        if current_index is not None and source_index is not None:
            map_frame_lag.append(float(max(0, current_index - source_index)))
        for stage, duration in row.get("stage_latency_s", {}).items():
            if isinstance(duration, (int, float)) and duration > 0:
                stage_samples.setdefault(stage, []).append(float(duration))

    summary_path = run_dir / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    live_path = run_dir / "live" / "state.json"
    live = json.loads(live_path.read_text(encoding="utf-8")) if live_path.exists() else {}
    graph_path = run_dir / "live" / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8")) if graph_path.exists() else {}
    camera_index = live.get("camera", {}).get("frame_index")
    nav_index = live.get("navigation", {}).get("as_of_frame_index")
    graph_index = _frame_index(graph.get("as_of_frame_id"))
    if graph_index is None:
        graph_index = live.get("graph_as_of_frame_index")

    command_stale = sum((row.get("command") or {}).get("reason") == "nav_stale" for row in ticks)
    policy_stale = sum(row.get("decision_reason") == "nav_stale" for row in ticks)
    depth_stale = sum((row.get("command") or {}).get("reason") == "depth_stale" for row in ticks)
    movement_path = run_dir / "movement_gate.json"
    movement = json.loads(movement_path.read_text(encoding="utf-8")) if movement_path.exists() else {}
    resources = summary.get("resource_monitor", {})
    root_pid = resources.get("root_pid")

    return {
        "schema": "3dsg-navigation-freshness-audit/v1",
        "run_id": summary.get("run_id", run_dir.name),
        "tick_count": len(ticks),
        "elapsed_s": summary.get("elapsed_exploration_s"),
        "capture_fps": live.get("capture_fps"),
        "map_capture_age": _percentiles(map_age),
        "map_frame_lag": _frame_percentiles(map_frame_lag),
        "map_frames_without_capture_pair": unpaired_map_frames,
        "worker_stage_latency": {name: _percentiles(values)
                                  for name, values in sorted(stage_samples.items())},
        "stale_and_queue": {
            "command_nav_stale_ticks": command_stale,
            "policy_nav_stale_ticks": policy_stale,
            "command_depth_stale_ticks": depth_stale,
            "max_map_dropped_frames": max((int(row.get("map_dropped_frames", 0) or 0)
                                            for row in ticks), default=0),
            "planner_result_stale_ticks": sum(bool(row.get("planner_result_stale"))
                                               for row in ticks),
        },
        "live_frame_indices": {
            "camera": camera_index,
            "navmap": nav_index,
            "graph": graph_index,
            "camera_to_navmap_lag": (camera_index - nav_index
                                      if isinstance(camera_index, int) and isinstance(nav_index, int)
                                      else None),
            "camera_to_graph_lag": (camera_index - graph_index
                                     if isinstance(camera_index, int) and isinstance(graph_index, int)
                                     else None),
        },
        "movement_gate": movement.get("gate"),
        "resource_monitor": {
            "peak_process_tree_rss_gib": resources.get("peak_process_tree_rss_gib"),
            "rss_cap_gib": resources.get("process_tree_rss_cap_gib"),
            "minimum_mem_available_gib": resources.get("minimum_mem_available_gib"),
            "process_tree_cpu_seconds": resources.get("process_tree_cpu_seconds"),
            "average_process_tree_cpu_cores": resources.get("average_process_tree_cpu_cores"),
            "peak_process_tree_cpu_cores": resources.get("peak_process_tree_cpu_cores"),
            "process_cpu_seconds_by_name": resources.get("process_cpu_seconds_by_name", {}),
            "process_cpu_seconds_by_process": _resource_cpu_by_process(
                run_dir, int(root_pid) if root_pid is not None else None),
            "stop_reason": resources.get("stop_reason"),
        },
        "graph_worker": {
            "connected": summary.get("graph_worker_connected"),
            "ok": summary.get("graph_worker_ok"),
            "backend": summary.get("graph_backend"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.run_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(args.output)
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
