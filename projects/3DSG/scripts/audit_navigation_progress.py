#!/usr/bin/env python3
"""Audit robot motion continuity from a saved frontier-navigation run."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys
from typing import Any


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _load_ticks(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events_path = run_dir / "events.jsonl"
    rows: list[dict[str, Any]] = []
    with events_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {events_path}:{line_number}: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"event record at {events_path}:{line_number} must be an object")
            rows.append(row)

    ticks = [row for row in rows if row.get("event") == "navigation_tick"]
    if not ticks:
        raise ValueError(f"no navigation_tick events in {events_path}")

    for index, row in enumerate(ticks):
        prefix = f"navigation_tick[{index}]"
        row["_audit_time_s"] = _finite_number(
            row.get("capture_host_monotonic_s"), f"{prefix}.capture_host_monotonic_s")
        pose = row.get("robot_pose_xy_yaw")
        if not isinstance(pose, (list, tuple)) or len(pose) < 3:
            raise ValueError(f"{prefix}.robot_pose_xy_yaw must contain x, y, and yaw")
        row["_audit_pose_xy"] = (
            _finite_number(pose[0], f"{prefix}.robot_pose_xy_yaw[0]"),
            _finite_number(pose[1], f"{prefix}.robot_pose_xy_yaw[1]"),
        )
        _finite_number(pose[2], f"{prefix}.robot_pose_xy_yaw[2]")
        frontier_count = _finite_number(row.get("frontier_count"), f"{prefix}.frontier_count")
        if frontier_count < 0 or not frontier_count.is_integer():
            raise ValueError(f"{prefix}.frontier_count must be a nonnegative integer")
        row["_audit_frontier_count"] = int(frontier_count)
        command = row.get("command")
        if command is not None and not isinstance(command, dict):
            raise ValueError(f"{prefix}.command must be an object or null")
        if isinstance(command, dict) and command.get("linear_mps") is not None:
            row["_audit_linear_mps"] = _finite_number(
                command["linear_mps"], f"{prefix}.command.linear_mps")
        else:
            row["_audit_linear_mps"] = None

    ticks.sort(key=lambda row: float(row["_audit_time_s"]))
    return rows, ticks


def _is_initial_scan(row: dict[str, Any]) -> bool:
    command = row.get("command")
    command_reason = command.get("reason") if isinstance(command, dict) else None
    return (row.get("decision_reason") == "initial_scan"
            or command_reason == "initial_scan")


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    decisions = Counter(str(row.get("decision_reason") or "unknown") for row in rows)
    commands = Counter(
        str((row.get("command") or {}).get("reason") or "unknown")
        if isinstance(row.get("command"), dict) else "unknown"
        for row in rows
    )
    return {
        "decision_reason": dict(sorted(decisions.items())),
        "command_reason": dict(sorted(commands.items())),
    }


def _stationary_episode(
    ticks: list[dict[str, Any]], start: int, end: int,
    anchor_xy: tuple[float, float], epsilon_m: float, max_duration_s: float,
) -> dict[str, Any]:
    first = ticks[start]
    last = ticks[end]
    duration = max(0.0, float(last["_audit_time_s"]) - float(first["_audit_time_s"]))
    endpoint_xy = last["_audit_pose_xy"]
    offset = math.dist(anchor_xy, endpoint_xy)
    span_rows = ticks[start:end + 1]
    return {
        "start_capture_host_monotonic_s": first["_audit_time_s"],
        "end_capture_host_monotonic_s": last["_audit_time_s"],
        "duration_s": duration,
        "anchor_xy_m": list(anchor_xy),
        "endpoint_xy_m": list(endpoint_xy),
        "endpoint_displacement_from_anchor_m": offset,
        "movement_epsilon_m": epsilon_m,
        "tick_count": len(span_rows),
        "exceeds_threshold": duration > max_duration_s,
        "reason_counts": _reason_counts(span_rows),
    }


def _longest_stationary_episode(
    ticks: list[dict[str, Any]], epsilon_m: float, max_duration_s: float,
) -> tuple[dict[str, Any] | None, int]:
    best: dict[str, Any] | None = None
    episode_count = 0
    anchor_index: int | None = None
    previous_index: int | None = None
    anchor_xy: tuple[float, float] | None = None

    def finish() -> None:
        nonlocal best, episode_count, anchor_index, previous_index, anchor_xy
        if anchor_index is not None and previous_index is not None and anchor_xy is not None:
            episode_count += 1
            candidate = _stationary_episode(
                ticks, anchor_index, previous_index, anchor_xy, epsilon_m, max_duration_s)
            if best is None or candidate["duration_s"] > best["duration_s"]:
                best = candidate
        anchor_index = previous_index = None
        anchor_xy = None

    for index, row in enumerate(ticks):
        frontier_count = int(row["_audit_frontier_count"])
        if _is_initial_scan(row) or frontier_count == 0:
            finish()
            continue

        pose_xy = row["_audit_pose_xy"]
        if anchor_index is None or anchor_xy is None:
            anchor_index = previous_index = index
            anchor_xy = pose_xy
            continue

        if math.dist(anchor_xy, pose_xy) >= epsilon_m:
            finish()
            anchor_index = previous_index = index
            anchor_xy = pose_xy
        else:
            previous_index = index

    finish()
    return best, episode_count


def _windowed_progress(
    ticks: list[dict[str, Any]], window_s: float,
) -> list[dict[str, Any]]:
    eligible = [row for row in ticks if not _is_initial_scan(row)]
    if not eligible:
        return []
    origin_s = float(eligible[0]["_audit_time_s"])
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        window_index = int((float(row["_audit_time_s"]) - origin_s) // window_s)
        groups[window_index].append(row)

    result = []
    for window_index, rows in sorted(groups.items()):
        start = rows[0]
        end = rows[-1]
        points = [row["_audit_pose_xy"] for row in rows]
        path_m = sum(math.dist(previous, current)
                     for previous, current in zip(points, points[1:]))
        frontier_counts = [int(row["_audit_frontier_count"]) for row in rows]
        result.append({
            "window_index": window_index,
            "start_capture_host_monotonic_s": start["_audit_time_s"],
            "end_capture_host_monotonic_s": end["_audit_time_s"],
            "elapsed_from_exploration_start_s": float(start["_audit_time_s"]) - origin_s,
            "duration_s": max(0.0, float(end["_audit_time_s"]) - float(start["_audit_time_s"])),
            "tick_count": len(rows),
            "path_m": path_m,
            "net_displacement_m": math.dist(points[0], points[-1]),
            "active_frontier_tick_count": sum(count > 0 for count in frontier_counts),
            "no_frontier_tick_count": sum(count == 0 for count in frontier_counts),
            "frontier_count_min": min(frontier_counts),
            "frontier_count_max": max(frontier_counts),
            "reason_counts": _reason_counts(rows),
        })
    return result


def audit(
    run_dir: Path, *, max_stationary_s: float = 30.0,
    movement_epsilon_m: float = 0.05, window_s: float = 30.0,
) -> dict[str, Any]:
    max_stationary_s = _finite_number(max_stationary_s, "max_stationary_s")
    movement_epsilon_m = _finite_number(movement_epsilon_m, "movement_epsilon_m")
    window_s = _finite_number(window_s, "window_s")
    if max_stationary_s < 0:
        raise ValueError("max_stationary_s must be nonnegative")
    if movement_epsilon_m <= 0:
        raise ValueError("movement_epsilon_m must be positive")
    if window_s <= 0:
        raise ValueError("window_s must be positive")

    rows, ticks = _load_ticks(run_dir)
    summary_path = run_dir / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    if not isinstance(summary, dict):
        raise ValueError(f"{summary_path} must contain a JSON object")

    poses = [row["_audit_pose_xy"] for row in ticks]
    total_path_m = sum(math.dist(previous, current)
                       for previous, current in zip(poses, poses[1:]))
    net_displacement_m = math.dist(poses[0], poses[-1])
    first_time_s = float(ticks[0]["_audit_time_s"])
    last_time_s = float(ticks[-1]["_audit_time_s"])

    command_forward = [
        row for row in ticks
        if row["_audit_linear_mps"] is not None and row["_audit_linear_mps"] > 0
    ]
    executed_forward = [
        row for row in ticks if row.get("executed_action") == "move_forward"
    ]

    longest, episode_count = _longest_stationary_episode(
        ticks, movement_epsilon_m, max_stationary_s)
    gate_failed = longest is not None and bool(longest["exceeds_threshold"])
    has_finished_event = any(row.get("event") == "episode_finished" for row in rows)
    return {
        "schema": "3dsg-navigation-progress-audit/v1",
        "run_id": summary.get("run_id") or next(
            (row.get("run_id") for row in rows if row.get("run_id")), run_dir.name),
        "run_status": "completed" if summary_path.exists() or has_finished_event else "partial",
        "tick_count": len(ticks),
        "elapsed_window_s": {
            "start_capture_host_monotonic_s": first_time_s,
            "end_capture_host_monotonic_s": last_time_s,
            "duration_s": max(0.0, last_time_s - first_time_s),
        },
        "motion": {
            "total_path_m": total_path_m,
            "net_displacement_m": net_displacement_m,
            "last_forward_command": ({
                "capture_host_monotonic_s": command_forward[-1]["_audit_time_s"],
                "age_at_last_tick_s": max(0.0, last_time_s - float(command_forward[-1]["_audit_time_s"])),
                "linear_mps": command_forward[-1]["_audit_linear_mps"],
                "executed_action": command_forward[-1].get("executed_action"),
                "decision_reason": command_forward[-1].get("decision_reason"),
            } if command_forward else None),
            "last_forward_action": ({
                "capture_host_monotonic_s": executed_forward[-1]["_audit_time_s"],
                "age_at_last_tick_s": max(0.0, last_time_s - float(executed_forward[-1]["_audit_time_s"])),
                "decision_reason": executed_forward[-1].get("decision_reason"),
                "command_reason": ((executed_forward[-1].get("command") or {}).get("reason")
                                   if isinstance(executed_forward[-1].get("command"), dict) else None),
            } if executed_forward else None),
        },
        "continuity_gate": {
            "status": "FAIL" if gate_failed else "PASS",
            "max_stationary_s": max_stationary_s,
            "movement_epsilon_m": movement_epsilon_m,
            "active_frontier_ticks": sum(
                int(row["_audit_frontier_count"]) > 0 and not _is_initial_scan(row)
                for row in ticks),
            "stationary_episode_count": episode_count,
            "longest_stationary_episode": longest,
            "failure_rule": "stationary duration > max_stationary_s while frontier_count > 0",
        },
        "windowed_progress": {
            "window_s": window_s,
            "windows": _windowed_progress(ticks, window_s),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="saved run directory containing events.jsonl")
    parser.add_argument("--output", type=Path, help="write JSON report to this path")
    parser.add_argument("--max-stationary-s", type=float, default=30.0)
    parser.add_argument("--movement-epsilon-m", type=float, default=0.05)
    args = parser.parse_args(argv)
    try:
        result = audit(
            args.run_dir,
            max_stationary_s=args.max_stationary_s,
            movement_epsilon_m=args.movement_epsilon_m,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(args.output)
    else:
        sys.stdout.write(encoded)
    return 1 if result["continuity_gate"]["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
