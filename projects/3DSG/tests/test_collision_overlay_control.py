"""A safe A* detour must survive lookahead and actual-yaw execution."""

import math

import numpy as np
import pytest

from exploration.contracts import GridState, RobotPose2
from exploration.controller import Controller, ControllerConfig, _segment_clear
from exploration.planner import AStarPlanner, PlannerConfig
from tests.test_controller_progress import _snapshot, _tick
from tests.test_navigation_regressions import _motion_packet, _runtime_for_motion_fixture


def _fixture():
    snapshot = _snapshot(np.full((20, 20), GridState.FREE, dtype=np.uint8))
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.01, inflation_margin_m=0.0))
    start = snapshot.cell_to_world(10, 10)
    edges = frozenset({((10, 10), (10, 11))})
    path = planner.plan(snapshot, start, (10, 15), blocked_edges=edges)
    assert path.success
    return snapshot, planner, start, edges, path


@pytest.mark.parametrize("provider", [True, False])
def test_lookahead_cannot_shortcut_a_rejected_edge(provider):
    snapshot, planner, start, edges, path = _fixture()
    controller = Controller(ControllerConfig(),
                            traversable=planner.traversable if provider else None)
    target = controller._lookahead(snapshot, path.cells, start, blocked_edges=edges)
    assert target is not None
    assert _segment_clear(snapshot, planner.traversable(snapshot), start, target,
                          blocked_edges=edges)
    assert target == pytest.approx(snapshot.cell_to_world(9, 11))


def test_actual_yaw_cannot_translate_across_edge_while_safe_target_is_to_side():
    snapshot, planner, start, edges, path = _fixture()
    controller = Controller(ControllerConfig(), traversable=planner.traversable)
    command = _tick(controller, snapshot, path, start, yaw_rad=0.0, blocked_edges=edges)
    assert command.reason == "align_heading"
    assert command.linear_mps == 0.0
    assert command.yaw_rate_radps < 0.0
    aligned = _tick(controller, snapshot, path, start, yaw_rad=-math.pi / 4,
                    blocked_edges=edges)
    assert aligned.reason == "tracking" and aligned.linear_mps > 0.0


@pytest.mark.parametrize("target", [(10, 11), (11, 10), (11, 11), (9, 9)])
@pytest.mark.parametrize("reverse", [False, True])
def test_segment_rejects_cardinal_and_diagonal_overlay_in_both_directions(target, reverse):
    snapshot, planner, _, _, _ = _fixture()
    source = (10, 10)
    edge = tuple(sorted((source, target)))
    a, b = (target, source) if reverse else (source, target)
    assert not _segment_clear(snapshot, planner.traversable(snapshot),
                              snapshot.cell_to_world(*a), snapshot.cell_to_world(*b),
                              blocked_edges=frozenset({edge}))


def test_off_center_robot_recenters_before_departing_along_detour():
    snapshot, planner, _, _, _ = _fixture()
    start = (1.001, 1.09)
    edges = frozenset({((10, 9), (10, 10))})
    path = planner.plan(snapshot, start, (10, 5), blocked_edges=edges)
    assert path.success
    controller = Controller(ControllerConfig(), traversable=planner.traversable)
    target = controller._lookahead(snapshot, path.cells, start, blocked_edges=edges)
    center = snapshot.cell_to_world(10, 10)
    assert target == pytest.approx(center)
    departing = controller._lookahead(snapshot, path.cells, center, blocked_edges=edges)
    assert departing is not None and math.dist(departing, center) > 0.05
    assert _segment_clear(snapshot, planner.traversable(snapshot), center, departing,
                          blocked_edges=edges)


def test_blocked_forward_within_discrete_turn_deadband_requests_replan():
    snapshot, planner, _, edges, _ = _fixture()
    start = (1.099, 1.09999)
    path = planner.plan(snapshot, start, (11, 19), blocked_edges=edges)
    assert path.success
    controller = Controller(ControllerConfig(lookahead_m=1.2),
                            traversable=planner.traversable)
    target = controller._lookahead(snapshot, path.cells, start, blocked_edges=edges)
    error = math.atan2(target[1] - start[1], target[0] - start[0])
    assert 0 < error < math.radians(6)
    stopped = _tick(controller, snapshot, path, start, blocked_edges=edges)
    assert stopped.reason == "path_segment_blocked"
    assert stopped.linear_mps == stopped.yaw_rate_radps == 0.0


def test_expired_overlay_does_not_leave_controller_stuck():
    snapshot, planner, start, edges, path = _fixture()
    controller = Controller(ControllerConfig(), traversable=planner.traversable)
    assert _tick(controller, snapshot, path, start,
                 blocked_edges=edges).reason == "align_heading"
    resumed = _tick(controller, snapshot, path, start, blocked_edges=frozenset())
    assert resumed.reason == "tracking" and resumed.linear_mps > 0


def test_runtime_supplies_collision_overlay_to_controller_after_recovery():
    runtime = _runtime_for_motion_fixture(0.0)
    received = []
    original_tick = runtime.controller.tick

    def spy(**kwargs):
        received.append(kwargs.get("blocked_edges"))
        return original_tick(**kwargs)

    runtime.controller.tick = spy
    try:
        first = runtime.tick(_motion_packet(0), RobotPose2(0, 0, 0), dt_s=0.04,
                             now_monotonic_s=10.0, collided=True,
                             collision_position_world_xy=(0.02, 0.0),
                             collision_filter_details={
                                 "reason": ["filtered_step_displacement_invalid"]})
        assert first.blocked_step_edge is not None
        second = runtime.tick(_motion_packet(1), RobotPose2(0, 0, -0.5), dt_s=0.04,
                              now_monotonic_s=10.04)
        assert received and first.blocked_step_edge in received[-1]
        assert received[-1] == runtime.policy.blocked_edges(second.snapshot.revision)
    finally:
        assert runtime.close()
