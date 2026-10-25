from __future__ import annotations

import json

import pytest

from scripts.audit_navigation_progress import audit, main


def _tick(
    time_s: float, x: float = 0.0, y: float = 0.0, *, frontier_count: int = 2,
    decision_reason: str = "nearest_reachable_frontier", decision_state: str = "MOVE",
    command_reason: str = "tracking", linear_mps: float = 0.0,
    executed_action: str = "stop",
) -> dict[str, object]:
    return {
        "event": "navigation_tick",
        "capture_host_monotonic_s": time_s,
        "robot_pose_xy_yaw": [x, y, 0.0],
        "frontier_count": frontier_count,
        "decision_reason": decision_reason,
        "decision_state": decision_state,
        "command": {"reason": command_reason, "linear_mps": linear_mps},
        "executed_action": executed_action,
    }


def _write_run(tmp_path, ticks: list[dict[str, object]], *, completed: bool = False) -> None:
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(row, allow_nan=True) + "\n" for row in ticks), encoding="utf-8")
    if completed:
        (tmp_path / "run_summary.json").write_text(
            json.dumps({"run_id": "fixture", "state": "completed"}), encoding="utf-8")


def test_400_second_stationary_active_frontier_stall_fails_with_span_diagnostics(tmp_path):
    _write_run(tmp_path, [
        _tick(100.0, frontier_count=4, decision_reason="no_reachable_frontier_path",
              decision_state="OBSERVE", command_reason="no_valid_path"),
        _tick(500.0, frontier_count=4, decision_reason="no_reachable_frontier_path",
              decision_state="OBSERVE", command_reason="no_valid_path"),
    ])

    result = audit(tmp_path)
    gate = result["continuity_gate"]
    worst = gate["longest_stationary_episode"]

    assert gate["status"] == "FAIL"
    assert worst["duration_s"] == pytest.approx(400.0)
    assert worst["start_capture_host_monotonic_s"] == pytest.approx(100.0)
    assert worst["end_capture_host_monotonic_s"] == pytest.approx(500.0)
    assert worst["reason_counts"]["decision_reason"] == {"no_reachable_frontier_path": 2}
    assert worst["reason_counts"]["command_reason"] == {"no_valid_path": 2}


def test_initial_scan_time_is_excluded_from_stationary_episode(tmp_path):
    ticks = [
        _tick(0.0, frontier_count=0, decision_reason="initial_scan",
              decision_state="SCAN", command_reason="initial_scan"),
        _tick(100.0, frontier_count=0, decision_reason="initial_scan",
              decision_state="SCAN", command_reason="initial_scan"),
        _tick(101.0, frontier_count=2),
        _tick(111.0, frontier_count=2),
    ]
    _write_run(tmp_path, ticks)

    result = audit(tmp_path)

    assert result["continuity_gate"]["status"] == "PASS"
    assert result["continuity_gate"]["longest_stationary_episode"]["duration_s"] == pytest.approx(10.0)


def test_frontier_exhaustion_breaks_stationary_episode(tmp_path):
    _write_run(tmp_path, [
        _tick(0.0, frontier_count=2),
        _tick(10.0, frontier_count=2),
        _tick(100.0, frontier_count=0, decision_reason="none"),
        _tick(110.0, frontier_count=2),
        _tick(120.0, frontier_count=2),
    ])

    result = audit(tmp_path)

    assert result["continuity_gate"]["status"] == "PASS"
    assert result["continuity_gate"]["longest_stationary_episode"]["duration_s"] == pytest.approx(10.0)


def test_small_steps_that_accumulate_beyond_anchor_tolerance_keep_continuity(tmp_path):
    _write_run(tmp_path, [
        _tick(float(index * 10), x=index * 0.06, frontier_count=3)
        for index in range(41)
    ])

    result = audit(tmp_path)

    assert result["continuity_gate"]["status"] == "PASS"
    assert result["motion"]["total_path_m"] == pytest.approx(2.4)
    assert result["motion"]["net_displacement_m"] == pytest.approx(2.4)
    assert result["windowed_progress"]["window_s"] == pytest.approx(30.0)
    assert len(result["windowed_progress"]["windows"]) == 14


def test_partial_run_needs_no_summary_and_cli_returns_pass_exit_code(tmp_path):
    _write_run(tmp_path, [_tick(50.0, frontier_count=1)])
    output = tmp_path / "reports" / "progress.json"

    exit_code = main([str(tmp_path), "--output", str(output)])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["run_status"] == "partial"
    assert report["continuity_gate"]["status"] == "PASS"


def test_cli_returns_failure_exit_code_for_continuity_failure(tmp_path):
    _write_run(tmp_path, [_tick(0.0), _tick(31.0)])

    assert main([str(tmp_path)]) == 1


def test_nonfinite_capture_time_is_rejected(tmp_path):
    _write_run(tmp_path, [_tick(float("nan"))])

    with pytest.raises(ValueError, match="capture_host_monotonic_s must be a finite number"):
        audit(tmp_path)
