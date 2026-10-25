"""Frontier components retain fresh, footprint-safe observation choices."""

from dataclasses import replace
import math

import numpy as np
import pytest

from exploration.contracts import GridSnapshot, GridState
from exploration.frontier import FrontierExtractor
from exploration.planner import AStarPlanner, PlannerConfig


def _snapshot() -> GridSnapshot:
    shape = (45, 65)
    states = np.full(shape, GridState.OCCUPIED, dtype=np.uint8)
    states[3:-3, 3:-3] = GridState.FREE
    # Two distinct unknown boundaries in one connected free-space region.
    states[9:11, 10:28] = GridState.UNKNOWN
    states[30:32, 40:52] = GridState.UNKNOWN
    evidence = np.ones(shape, dtype=np.uint16)
    return GridSnapshot(
        revision=1, as_of_frame_id="viewpoints", as_of_frame_index=1,
        host_monotonic_s=1.0, resolution_m=0.05,
        origin_x_m=-0.5, origin_y_m=-0.5,
        states=states, floor_support=evidence, clearance_evidence=evidence,
        spawn_validated=np.zeros(shape, dtype=bool),
        occupancy_log_odds=np.full(shape, -1.0, dtype=np.float32),
    )


def _extractor(**kwargs) -> FrontierExtractor:
    return FrontierExtractor(agent_radius_m=0.1, inflation_margin_m=0.025, **kwargs)


def _robot(snapshot: GridSnapshot) -> tuple[float, float]:
    return snapshot.cell_to_world(24, 34)


def test_default_preserves_canonical_primary_ids_and_order() -> None:
    snapshot = _snapshot()
    default = _extractor().extract(snapshot, _robot(snapshot))
    explicit = _extractor(max_viewpoints_per_frontier=1).extract(snapshot, _robot(snapshot))
    multiple = _extractor(max_viewpoints_per_frontier=4).extract(snapshot, _robot(snapshot))
    assert default == explicit
    assert len(default.frontiers) == 2
    assert multiple.frontiers[:2] == default.frontiers
    assert all("-view-" not in frontier.frontier_id for frontier in default.frontiers)


def test_multiple_views_are_separated_observed_and_reachable() -> None:
    snapshot = _snapshot()
    robot = _robot(snapshot)
    extractor = _extractor(max_viewpoints_per_frontier=4, min_viewpoint_separation_m=0.35)
    result = extractor.extract(snapshot, robot)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.1, inflation_margin_m=0.025))
    safe = planner.traversable(snapshot)
    assert len(result.frontiers) == 8
    assert result.components_with_viewpoint == 2
    assert len({frontier.frontier_id for frontier in result.frontiers}) == 8
    groups = {}
    for frontier in result.frontiers:
        assert snapshot.states[frontier.viewpoint_cell] == GridState.FREE
        assert safe[frontier.viewpoint_cell]
        assert planner.plan(snapshot, robot, frontier.viewpoint_cell).success
        groups.setdefault(frontier.boundary_cells, []).append(frontier.viewpoint_world_xy)
    for points in groups.values():
        assert len(points) == 4
        assert all(math.dist(point, previous) + 1e-9 >= 0.35
                   for index, point in enumerate(points) for previous in points[:index])


def test_rolling_origin_preserves_world_targets_and_variant_ids() -> None:
    snapshot = _snapshot()
    extractor = _extractor(max_viewpoints_per_frontier=4)
    original = extractor.extract(snapshot, _robot(snapshot))
    row_pad, col_pad = 7, 11
    arrays = {
        name: np.pad(getattr(snapshot, name), ((row_pad, 0), (col_pad, 0)),
                     constant_values=GridState.OCCUPIED if name == "states" else 0)
        for name in ("states", "floor_support", "clearance_evidence",
                     "spawn_validated", "occupancy_log_odds")
    }
    rolled = replace(snapshot, revision=2,
                     origin_x_m=snapshot.origin_x_m - col_pad * snapshot.resolution_m,
                     origin_y_m=snapshot.origin_y_m - row_pad * snapshot.resolution_m,
                     **arrays)
    result = extractor.extract(rolled, _robot(snapshot))
    assert [frontier.frontier_id for frontier in result.frontiers] == [
        frontier.frontier_id for frontier in original.frontiers]
    assert np.allclose([frontier.viewpoint_world_xy for frontier in result.frontiers],
                       [frontier.viewpoint_world_xy for frontier in original.frontiers])


def test_exclusions_resample_full_pool_after_initial_views_are_visited() -> None:
    snapshot = _snapshot()
    extractor = _extractor(max_viewpoints_per_frontier=4)
    first = extractor.extract(snapshot, _robot(snapshot))
    visited = tuple(frontier.viewpoint_world_xy for frontier in first.frontiers)
    fresh = extractor.extract(snapshot, _robot(snapshot), excluded_viewpoints=visited,
                              excluded_viewpoint_radius_m=0.35)
    assert fresh.frontiers
    assert {frontier.boundary_cells for frontier in fresh.frontiers} == {
        frontier.boundary_cells for frontier in first.frontiers}
    assert all(math.dist(frontier.viewpoint_world_xy, point) >= 0.35
               for frontier in fresh.frontiers for point in visited)
    assert not {frontier.frontier_id for frontier in first.frontiers}.intersection(
        frontier.frontier_id for frontier in fresh.frontiers)
    assert all("-view-" in frontier.frontier_id for frontier in fresh.frontiers)


def test_global_budget_retains_components_before_extra_views() -> None:
    snapshot = _snapshot()
    canonical = _extractor().extract(snapshot, _robot(snapshot)).frontiers
    limited = _extractor(max_viewpoints_per_frontier=4, max_frontiers=3).extract(
        snapshot, _robot(snapshot))
    assert len(limited.frontiers) == 3
    assert limited.frontiers[:2] == canonical
    assert limited.frontiers[2].boundary_cells == canonical[0].boundary_cells


@pytest.mark.parametrize("kwargs", [
    {"max_viewpoints_per_frontier": 0},
    {"max_viewpoints_per_frontier": 1.5},
    {"min_viewpoint_separation_m": 0.0},
    {"min_viewpoint_separation_m": math.inf},
])
def test_invalid_sampling_configuration(kwargs) -> None:
    with pytest.raises(ValueError):
        _extractor(**kwargs)
