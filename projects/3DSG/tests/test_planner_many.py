"""Shared frontier searches preserve exact costs and observed-space geometry."""

from dataclasses import replace
import math

import numpy as np
import pytest

from exploration.contracts import GridSnapshot, GridState
from exploration.frontier import Frontier, FrontierSet
from exploration.planner import AStarPlanner, PlannerConfig
from exploration.policy import FrontierPolicy, PolicyConfig


def _snapshot(states=None) -> GridSnapshot:
    if states is None:
        states = np.full((25, 30), GridState.FREE, dtype=np.uint8)
    evidence = np.ones(states.shape, dtype=np.uint16)
    return GridSnapshot(
        revision=7, as_of_frame_id="many", as_of_frame_index=1,
        host_monotonic_s=1.0, resolution_m=0.1, origin_x_m=-1.0, origin_y_m=-0.5,
        states=states, floor_support=evidence, clearance_evidence=evidence,
        spawn_validated=np.zeros(states.shape, dtype=bool),
        occupancy_log_odds=np.full(states.shape, -1.0, dtype=np.float32),
    )


def _planner(**kwargs) -> AStarPlanner:
    return AStarPlanner(PlannerConfig(agent_radius_m=0.025, inflation_margin_m=0.0, **kwargs))


def _assert_valid_path(snapshot, path, traversable, blocked_edges=frozenset(), anchor=None):
    assert path.grid_origin_x_m == snapshot.origin_x_m
    assert path.grid_origin_y_m == snapshot.origin_y_m
    assert path.grid_resolution_m == snapshot.resolution_m
    assert path.map_revision == snapshot.revision
    cost = 0.0
    for index, cell in enumerate(path.cells):
        assert traversable[cell] or index == 0 and cell == anchor
        if index:
            previous = path.cells[index - 1]
            dr, dc = cell[0] - previous[0], cell[1] - previous[1]
            assert max(abs(dr), abs(dc)) == 1
            assert tuple(sorted((previous, cell))) not in blocked_edges
            if dr and dc:
                assert traversable[previous[0] + dr, previous[1]]
                assert traversable[previous[0], previous[1] + dc]
            cost += math.hypot(dr, dc) * snapshot.resolution_m
    assert path.cost_m == pytest.approx(cost)


@pytest.mark.parametrize("allow_diagonal", [False, True])
def test_shared_costs_match_individual_astar_on_obstructed_maps(allow_diagonal) -> None:
    rng = np.random.default_rng(20261024)
    for _ in range(5):
        states = np.where(rng.random((25, 30)) < 0.2, GridState.OCCUPIED,
                          GridState.FREE).astype(np.uint8)
        states[2, 2] = GridState.FREE
        snapshot = _snapshot(states)
        planner = _planner(allow_diagonal=allow_diagonal)
        start = snapshot.cell_to_world(2, 2)
        goals = [(int(r), int(c)) for r, c in rng.integers((0, 0), (25, 30), size=(20, 2))]
        goals += [(2, 2), (-1, 3), (25, 30)]
        batch = planner.plan_many(snapshot, start, goals)
        assert set(batch) == set(goals)
        for goal in goals:
            individual = planner.plan(snapshot, start, goal)
            assert batch[goal].success == individual.success
            assert batch[goal].reason == individual.reason
            assert batch[goal].cost_m == pytest.approx(individual.cost_m)
            if batch[goal].success:
                assert batch[goal].cells[0] == (2, 2)
                assert batch[goal].cells[-1] == goal
                _assert_valid_path(snapshot, batch[goal], planner.traversable(snapshot))


def test_shared_search_obeys_blocked_edges() -> None:
    snapshot = _snapshot()
    planner = _planner()
    start = (3, 3)
    goals = [(3, 4), (4, 4), (7, 8)]
    blocked = frozenset({tuple(sorted((start, (3, 4))))})
    paths = planner.plan_many(snapshot, snapshot.cell_to_world(*start), goals,
                              blocked_edges=blocked)
    assert paths[(3, 4)].cost_m > snapshot.resolution_m
    for goal, path in paths.items():
        assert path.success
        assert path.cost_m == pytest.approx(planner.plan(
            snapshot, snapshot.cell_to_world(*start), goal, blocked_edges=blocked).cost_m)
        _assert_valid_path(snapshot, path, planner.traversable(snapshot), blocked)


def test_shared_search_never_crosses_diagonal_unknown_corner() -> None:
    states = np.full((8, 8), GridState.UNKNOWN, dtype=np.uint8)
    states[3, 3] = states[4, 4] = GridState.FREE
    snapshot = _snapshot(states)
    paths = _planner().plan_many(snapshot, snapshot.cell_to_world(3, 3), [(4, 4), (3, 4)])
    assert paths[(4, 4)].reason == "no_known_free_path"
    assert paths[(3, 4)].reason == "goal_not_traversable"


def test_validated_start_anchor_is_single_cell_and_keeps_cached_mask_intact() -> None:
    states = np.full((12, 12), GridState.FREE, dtype=np.uint8)
    states[4, 4] = GridState.OCCUPIED
    snapshot = _snapshot(states)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.11, inflation_margin_m=0.0))
    start = snapshot.cell_to_world(4, 5)
    goals = [(4, 7), (5, 6), (4, 3)]
    assert not planner.traversable(snapshot)[4, 5]
    rejected = planner.plan_many(snapshot, start, goals)
    assert all(path.reason == "start_not_traversable" for path in rejected.values())
    paths = planner.plan_many(snapshot, start, goals, allow_validated_start_anchor=True)
    assert not planner.traversable(snapshot)[4, 5]
    for goal, path in paths.items():
        individual = planner.plan(snapshot, start, goal, allow_validated_start_anchor=True)
        assert path.success == individual.success
        assert path.cost_m == pytest.approx(individual.cost_m)
        if path.success:
            _assert_valid_path(snapshot, path, planner.traversable(snapshot), anchor=(4, 5))


def test_budget_preserves_settled_goals_and_marks_remaining_unsettled() -> None:
    snapshot = _snapshot()
    planner = _planner(max_expansions=1)
    paths = planner.plan_many(snapshot, snapshot.cell_to_world(3, 3), [(3, 3), (3, 4), (20, 20)])
    assert paths[(3, 3)].success and paths[(3, 3)].cost_m == 0.0
    assert paths[(3, 4)].reason == "expansion_budget_exceeded"
    assert paths[(20, 20)].reason == "expansion_budget_exceeded"
    assert planner.plan_many(snapshot, (999.0, 999.0), []) == {}
    assert planner.plan_many(snapshot, (999.0, 999.0), [(-1, -1)])[(-1, -1)].reason == "start_outside_map"


class _CountingPlanner(AStarPlanner):
    def __init__(self):
        super().__init__(PlannerConfig(agent_radius_m=0.025, inflation_margin_m=0.0))
        self.batch_calls = []
        self.single_calls = 0

    def plan_many(self, snapshot, start_xy, goal_cells, **kwargs):
        goals = tuple(goal_cells)
        self.batch_calls.append(goals)
        return super().plan_many(snapshot, start_xy, goals, **kwargs)

    def plan(self, *args, **kwargs):
        self.single_calls += 1
        return super().plan(*args, **kwargs)


def _frontier(snapshot, name, cell):
    return Frontier(name, snapshot.revision, (cell,), cell, 10, snapshot.cell_to_world(*cell))


def _select(policy, snapshot, frontiers, now=1.0):
    return policy.select(snapshot=snapshot,
                         frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id,
                                               tuple(frontiers)),
                         robot_xy=snapshot.cell_to_world(3, 3), now_monotonic_s=now)


def test_policy_batches_once_and_retains_missing_held_target_with_diagnostics() -> None:
    snapshot = _snapshot()
    planner = _CountingPlanner()
    policy = FrontierPolicy(planner, PolicyConfig(reached_distance_m=0.05))
    selected = _select(policy, snapshot, [_frontier(snapshot, "near", (3, 7)),
                                         _frontier(snapshot, "far", (10, 10))])
    assert selected.frontier.frontier_id == "near"
    assert selected.goal_diagnostics["reachable_candidate_count"] == 2
    newer = replace(snapshot, revision=8)
    retained = _select(policy, newer, [_frontier(newer, "far", (10, 10))], now=2.0)
    assert retained.frontier.frontier_id == "near"
    assert retained.reason == "goal_hysteresis"
    assert retained.goal_diagnostics["held_goal_recovery"] == "held_cell_replanned"
    assert retained.goal_diagnostics["reachable_candidate_count"] == 1
    assert retained.goal_diagnostics["best_candidate_id"] == "far"
    assert retained.selected_at_monotonic_s == selected.selected_at_monotonic_s
    assert len(planner.batch_calls) == 2
    assert set(planner.batch_calls[-1]) == {(3, 7), (10, 10)}
    assert planner.single_calls == 0


def test_policy_keeps_sequential_fallback_for_custom_planner_without_batch() -> None:
    class SingleOnly:
        def __init__(self):
            self.inner = _planner()
            self.calls = 0

        def plan(self, *args, **kwargs):
            self.calls += 1
            return self.inner.plan(*args, **kwargs)

    snapshot = _snapshot()
    planner = SingleOnly()
    decision = _select(FrontierPolicy(planner), snapshot,
                       [_frontier(snapshot, "near", (3, 7)),
                        _frontier(snapshot, "far", (10, 10))])
    assert decision.frontier.frontier_id == "near"
    assert planner.calls == 2
