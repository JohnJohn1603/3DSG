"""Arrival messages reach the background policy before its next selection."""

import time

import numpy as np

from exploration.contracts import GridSnapshot, GridState
from exploration.frontier import Frontier, FrontierSet
from exploration.plan_worker import LatestPlanWorker
from exploration.planner import AStarPlanner, PlannerConfig
from exploration.policy import FrontierPolicy, PolicyConfig


def test_worker_applies_exact_arrival_before_selecting_drifted_frontier():
    shape = (20, 20)
    states = np.full(shape, GridState.FREE, dtype=np.uint8)
    evidence = np.ones(shape, dtype=np.uint16)
    snapshot = GridSnapshot(
        revision=1, as_of_frame_id="f1", as_of_frame_index=1,
        host_monotonic_s=10.0, resolution_m=0.1, origin_x_m=0.0, origin_y_m=0.0,
        states=states, floor_support=evidence, clearance_evidence=evidence,
        spawn_validated=np.zeros(shape, dtype=bool),
        occupancy_log_odds=np.full(shape, -1.0, dtype=np.float32))
    frontiers = FrontierSet(1, "f1", tuple(
        Frontier(name, 1, (cell,), cell, 1, snapshot.cell_to_world(*cell))
        for name, cell in (("drifted-id", (5, 7)), ("far-goal", (5, 15)))))

    class Extractor:
        max_viewpoints_per_frontier = 4

        def extract(self, *_args, **_kwargs):
            assert _kwargs["excluded_viewpoints"] == ((0.65, 0.55),)
            assert _kwargs["excluded_viewpoint_radius_m"] == 0.3
            return frontiers

    policy = FrontierPolicy(AStarPlanner(PlannerConfig(
        agent_radius_m=0.05, inflation_margin_m=0.0)),
        PolicyConfig(visited_viewpoint_radius_m=0.3))
    worker = LatestPlanWorker(Extractor(), policy)
    try:
        worker.report_goal_reached("old-id", (0.65, 0.55))
        worker.submit(snapshot, (0.55, 0.55), 10.0)
        outcome = None
        deadline = time.monotonic() + 2.0
        while outcome is None and time.monotonic() < deadline:
            outcome = worker.poll()
            if outcome is None:
                time.sleep(0.005)
        assert outcome is not None and outcome.error is None
        assert outcome.decision.frontier.frontier_id == "far-goal"
        assert outcome.decision.goal_diagnostics["spatially_suppressed_candidate_count"] == 1
    finally:
        assert worker.close(timeout_s=1.0)
