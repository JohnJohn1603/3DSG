from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from exploration.contracts import GridSnapshot, GridState, RobotPose2
from exploration.navmap_trace import NavMapTraceRecorder


def _snapshot(revision: int = 1) -> GridSnapshot:
    states = np.full((31, 31), GridState.FREE, dtype=np.uint8)
    floor = np.full((31, 31), 4, dtype=np.uint16)
    clearance = np.full((31, 31), 8, dtype=np.uint16)
    odds = np.full((31, 31), -2.0, dtype=np.float32)
    validated = np.zeros((31, 31), dtype=bool)
    return GridSnapshot(
        revision=revision, as_of_frame_id=f"run:{revision:08d}",
        as_of_frame_index=revision, host_monotonic_s=100.0 + revision,
        resolution_m=0.05, origin_x_m=-0.75, origin_y_m=-0.75,
        states=states, floor_support=floor, clearance_evidence=clearance,
        spawn_validated=validated, occupancy_log_odds=odds,
    )


def _decision():
    frontier = SimpleNamespace(frontier_id="frontier-a")
    path = SimpleNamespace(success=True, reason="ok", cost_m=0.4,
                           cells=((15, 15), (15, 16), (15, 17)))
    return SimpleNamespace(state="MOVE", reason="goal_retained",
                           frontier=frontier, path=path)


def _record(recorder, revision: int, elapsed_s: float) -> bool:
    command = SimpleNamespace(reason="tracking", linear_mps=0.1,
                              yaw_rate_radps=0.0)
    frontiers = SimpleNamespace(map_revision=revision - 1,
                                as_of_frame_id=f"run:{revision - 1:08d}",
                                reachable_free_cells=50,
                                known_free_cells=80,
                                frontiers=[object(), object()])
    return recorder.record(
        snapshot=_snapshot(revision), robot_pose=RobotPose2(0.0, 0.0, 0.2),
        elapsed_s=elapsed_s, decision=_decision(), command=command,
        frontier_set=frontiers, map_worker_pending=False, planner_pending=False,
    )


def test_navmap_trace_samples_bounded_local_patches_and_writes_after_close(tmp_path) -> None:
    output = tmp_path / "trace.npz"
    recorder = NavMapTraceRecorder(output, sample_hz=1.0, radius_cells=3,
                                   max_samples=2)

    assert _record(recorder, 1, 0.0)
    assert not _record(recorder, 2, 0.5)
    assert _record(recorder, 2, 1.0)
    assert _record(recorder, 3, 2.0)
    assert recorder.sample_count == 2
    assert not output.exists()

    recorder.close(status="finished")
    with np.load(output, allow_pickle=False) as saved:
        metadata = json.loads(str(saved["metadata_json"].item()))
        assert metadata["schema"] == "navmap-local-trace/v1"
        assert metadata["status"] == "finished"
        assert [row["revision"] for row in metadata["samples"]] == [2, 3]
        assert saved["states"].shape == (2, 7, 7)
        assert saved["floor_support"].dtype == np.uint16
        assert saved["occupancy_log_odds"].dtype == np.float32
        assert metadata["samples"][0]["goal_id"] == "frontier-a"
        assert metadata["samples"][0]["reachable_free_cells"] == 50
        assert metadata["samples"][0]["frontier_map_revision"] == 1
        assert metadata["samples"][0]["frontier_as_of_frame_id"] == "run:00000001"


def test_navmap_trace_requires_positive_finite_rate(tmp_path) -> None:
    for rate in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="sample_hz"):
            NavMapTraceRecorder(tmp_path / "trace.npz", sample_hz=rate)
