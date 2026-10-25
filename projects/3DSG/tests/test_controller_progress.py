from __future__ import annotations

import math

import numpy as np
import pytest

from exploration.contracts import GridSnapshot, GridState, PathResult
from exploration.controller import Controller, ControllerConfig, _segment_clear
from exploration.planner import AStarPlanner, PlannerConfig


def _snapshot(states: np.ndarray | None = None) -> GridSnapshot:
    if states is None:
        states = np.full((12, 12), GridState.FREE, dtype=np.uint8)
    evidence = np.ones_like(states, dtype=bool)
    return GridSnapshot(
        revision=1, as_of_frame_id="frame", as_of_frame_index=0,
        host_monotonic_s=10.0, resolution_m=0.1, origin_x_m=0.0,
        origin_y_m=0.0, states=states, floor_support=evidence,
        clearance_evidence=evidence, spawn_validated=np.zeros_like(evidence),
        occupancy_log_odds=np.zeros_like(states, dtype=np.float32),
    )


def _tick(controller: Controller, snapshot: GridSnapshot, path: PathResult,
          robot_xy: tuple[float, float], yaw_rad: float = 0.0, **kwargs):
    fields = dict(robot_xy=robot_xy, yaw_rad=yaw_rad, now_monotonic_s=10.0,
                  dt_s=0.1, depth_host_monotonic_s=10.0,
                  depth_front_min_m=None, depth_coverage_ok=True,
                  snapshot=snapshot, path=path, frame_id="frame")
    fields.update(kwargs)
    return controller.tick(**fields)


def test_lookahead_stays_before_unknown_inside_of_narrow_corner() -> None:
    cells = ((2, 2), (2, 3), (2, 4), (3, 4), (4, 4), (5, 4))
    states = np.full((12, 12), GridState.UNKNOWN, dtype=np.uint8)
    for cell in cells:
        states[cell] = GridState.FREE
    snapshot = _snapshot(states)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.04, inflation_margin_m=0.0))
    controller = Controller(ControllerConfig(lookahead_m=0.4),
                            traversable=planner._traversable)
    robot_xy = snapshot.cell_to_world(*cells[0])

    # The old 0.4 m lookahead aimed at (4, 4), crossing unknown (3, 3).
    assert not _segment_clear(snapshot, planner._traversable(snapshot), robot_xy,
                              snapshot.cell_to_world(4, 4))
    target = controller._lookahead(snapshot, cells, robot_xy)

    assert target == pytest.approx(snapshot.cell_to_world(2, 4))
    assert _tick(controller, snapshot, PathResult(True, cells, 0.5, 1), robot_xy).linear_mps > 0


def test_lookahead_uses_inflated_footprint_instead_of_free_center_cells() -> None:
    states = np.full((12, 12), GridState.FREE, dtype=np.uint8)
    states[5, 5] = GridState.OCCUPIED
    snapshot = _snapshot(states)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.11, inflation_margin_m=0.0))
    cells = ((6, 3), (5, 3), (4, 3), (3, 3), (3, 4), (3, 5), (3, 6))
    traversable = planner._traversable(snapshot)
    assert all(traversable[cell] for cell in cells)
    assert states[5, 4] == GridState.FREE and not traversable[5, 4]
    controller = Controller(ControllerConfig(lookahead_m=0.8),
                            traversable=planner._traversable)

    target = controller._lookahead(snapshot, cells, snapshot.cell_to_world(*cells[0]))

    assert target == pytest.approx(snapshot.cell_to_world(3, 3))


def test_supercover_rejects_diagonal_side_cell_as_planner_does() -> None:
    snapshot = _snapshot()
    traversable = np.ones(snapshot.shape, dtype=bool)
    traversable[2, 3] = False
    assert not _segment_clear(snapshot, traversable,
                              snapshot.cell_to_world(2, 2), snapshot.cell_to_world(3, 3))


def test_validated_start_anchor_allows_only_departure_from_free_source() -> None:
    snapshot = _snapshot()
    traversable = np.ones(snapshot.shape, dtype=bool)
    traversable[2, 2] = False
    cells = ((2, 2), (2, 3), (2, 4), (2, 5))
    controller = Controller(ControllerConfig(), traversable=lambda _: traversable)
    robot_xy = snapshot.cell_to_world(*cells[0])
    assert controller._lookahead(snapshot, cells, robot_xy) == pytest.approx(
        snapshot.cell_to_world(2, 5))

    traversable[2, 3] = False
    command = _tick(controller, snapshot, PathResult(True, cells, 0.3, 1), robot_xy)
    assert command.reason == "path_segment_blocked"
    assert command.linear_mps == command.yaw_rate_radps == 0.0


def test_alignment_clears_previous_forward_velocity_immediately() -> None:
    snapshot = _snapshot()
    cells = tuple((2, col) for col in range(2, 10))
    path = PathResult(True, cells, 0.7, 1)
    controller = Controller(ControllerConfig())
    robot_xy = snapshot.cell_to_world(*cells[0])
    for _ in range(8):
        moving = _tick(controller, snapshot, path, robot_xy)
    assert moving.linear_mps > 0.1

    turning = _tick(controller, snapshot, path, robot_xy, yaw_rad=math.pi / 2)

    assert turning.reason == "align_heading"
    assert turning.linear_mps == 0.0
    assert turning.yaw_rate_radps < 0.0


@pytest.mark.parametrize("heading_error_deg", [90.0, 40.0, -40.0])
def test_front_obstacle_allows_route_alignment_without_forward_velocity(
        heading_error_deg: float) -> None:
    snapshot = _snapshot()
    cells = tuple((2, col) for col in range(2, 10))
    path = PathResult(True, cells, 0.7, 1)
    controller = Controller(ControllerConfig())
    robot_xy = snapshot.cell_to_world(*cells[0])
    for _ in range(8):
        moving = _tick(controller, snapshot, path, robot_xy)
    assert moving.linear_mps > 0.1

    turning = _tick(controller, snapshot, path, robot_xy,
                    yaw_rad=-math.radians(heading_error_deg), depth_front_min_m=0.1)

    assert turning.reason == "obstacle_align_heading"
    assert turning.linear_mps == 0.0
    assert turning.yaw_rate_radps * heading_error_deg > 0.0


def test_front_obstacle_alignment_continues_below_normal_rotation_threshold() -> None:
    snapshot = _snapshot()
    cells = tuple((2, col) for col in range(2, 10))
    path = PathResult(True, cells, 0.7, 1)
    controller = Controller(ControllerConfig())
    robot_xy = snapshot.cell_to_world(*cells[0])

    # A turn away from a wall may still see it after crossing the ordinary
    # 50-degree rotation threshold. Retain yaw until the path is aligned.
    for error_deg in (90.0, 40.0, 12.0):
        turning = _tick(controller, snapshot, path, robot_xy,
                        yaw_rad=-math.radians(error_deg), depth_front_min_m=0.1)
        assert turning.reason == "obstacle_align_heading"
        assert turning.linear_mps == 0.0
        assert turning.yaw_rate_radps > 0.0

    stopped = _tick(controller, snapshot, path, robot_xy,
                    yaw_rad=-math.radians(5.0), depth_front_min_m=0.1)
    assert stopped.reason == "front_obstacle_brake"
    assert stopped.linear_mps == stopped.yaw_rate_radps == 0.0


@pytest.mark.parametrize("kwargs, reason", [
    ({"now_monotonic_s": 11.1, "depth_host_monotonic_s": 11.1}, "nav_stale"),
    ({"depth_host_monotonic_s": 9.0}, "depth_stale"),
    ({"depth_coverage_ok": False}, "depth_coverage_insufficient"),
    ({"path": None}, "no_valid_path"),
    ({"path": PathResult(True, ((2, 2), (2, 3)), 0.1, 2)}, "path_map_revision_stale"),
])
def test_obstacle_alignment_requires_fresh_evidence_and_valid_path(kwargs, reason) -> None:
    snapshot = _snapshot()
    cells = tuple((2, col) for col in range(2, 10))
    path = PathResult(True, cells, 0.7, 1)
    controller = Controller(ControllerConfig())
    robot_xy = snapshot.cell_to_world(*cells[0])
    assert _tick(controller, snapshot, path, robot_xy, yaw_rad=-math.pi / 2,
                 depth_front_min_m=0.1).yaw_rate_radps > 0.0

    overrides = dict(kwargs)
    active_path = overrides.pop("path", path)
    stopped = _tick(controller, snapshot, active_path, robot_xy, yaw_rad=-math.pi / 2,
                    depth_front_min_m=0.1, **overrides)

    assert stopped.reason == reason
    assert stopped.linear_mps == stopped.yaw_rate_radps == 0.0


def test_nearby_intermediate_corner_does_not_count_as_goal() -> None:
    snapshot = _snapshot()
    cells = ((2, 2), (2, 3), (3, 3), (4, 3), (5, 3), (6, 3))
    controller = Controller(ControllerConfig())
    robot_xy = (0.345, 0.25)
    target = controller._lookahead(snapshot, cells, robot_xy)
    assert target is not None
    assert math.dist(robot_xy, target) < controller.config.goal_reached_distance_m

    command = _tick(controller, snapshot, PathResult(True, cells, 0.5, 1), robot_xy)

    assert command.reason == "tracking"
    assert 0.0 < command.linear_mps <= math.dist(robot_xy, target) / controller.config.max_dt_s


def test_actual_final_endpoint_stops_even_with_existing_velocity() -> None:
    snapshot = _snapshot()
    cells = tuple((2, col) for col in range(2, 10))
    path = PathResult(True, cells, 0.7, 1)
    controller = Controller(ControllerConfig())
    _tick(controller, snapshot, path, snapshot.cell_to_world(*cells[0]))

    reached = _tick(controller, snapshot, path, snapshot.cell_to_world(*cells[-1]))

    assert reached.reason == "goal_reached"
    assert reached.linear_mps == reached.yaw_rate_radps == 0.0


@pytest.mark.parametrize("kwargs, reason", [
    ({"now_monotonic_s": 11.1, "depth_host_monotonic_s": 11.1}, "nav_stale"),
    ({"depth_host_monotonic_s": 9.0}, "depth_stale"),
    ({"depth_coverage_ok": False}, "depth_coverage_insufficient"),
    ({"depth_front_min_m": 0.1}, "front_obstacle_brake"),
])
def test_safety_evidence_still_stops_a_moving_controller(kwargs, reason) -> None:
    snapshot = _snapshot()
    cells = tuple((2, col) for col in range(2, 10))
    path = PathResult(True, cells, 0.7, 1)
    controller = Controller(ControllerConfig())
    robot_xy = snapshot.cell_to_world(*cells[0])
    assert _tick(controller, snapshot, path, robot_xy).linear_mps > 0

    stopped = _tick(controller, snapshot, path, robot_xy, **kwargs)

    assert stopped.reason == reason
    assert stopped.linear_mps == stopped.yaw_rate_radps == 0.0
