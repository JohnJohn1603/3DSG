"""Progress memory must survive frontier relabeling and rolling map origins."""

from dataclasses import replace

import numpy as np

from exploration.contracts import GridSnapshot, GridState
from exploration.frontier import Frontier, FrontierSet
from exploration.planner import AStarPlanner, PlannerConfig
from exploration.policy import FrontierPolicy, PolicyConfig


def _grid() -> GridSnapshot:
    shape = (30, 30)
    evidence = np.ones(shape, dtype=np.uint16)
    return GridSnapshot(
        revision=1, as_of_frame_id="progress", as_of_frame_index=0,
        host_monotonic_s=1.0, resolution_m=0.05,
        origin_x_m=0.0, origin_y_m=0.0,
        states=np.full(shape, GridState.FREE, dtype=np.uint8),
        floor_support=evidence, clearance_evidence=evidence,
        spawn_validated=np.zeros(shape, dtype=bool),
        occupancy_log_odds=np.full(shape, -1.0, dtype=np.float32),
    )


def _policy(**kwargs) -> FrontierPolicy:
    return FrontierPolicy(
        AStarPlanner(PlannerConfig(agent_radius_m=0.025, inflation_margin_m=0.0)),
        PolicyConfig(reached_distance_m=0.05, failed_viewpoint_radius_m=0.20, **kwargs),
    )


def _frontier(snapshot: GridSnapshot, name: str, xy: tuple[float, float]) -> Frontier:
    cell = snapshot.world_to_cell(*xy)
    assert cell is not None
    return Frontier(name, snapshot.revision, (cell,), cell, 10, snapshot.cell_to_world(*cell))


def _select(policy, snapshot, *frontiers, robot_xy=(0.275, 0.275), now=1.0):
    return policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id, frontiers),
        robot_xy=robot_xy, now_monotonic_s=now,
    )


def test_failed_viewpoint_survives_new_id_and_rolling_grid_origin() -> None:
    snapshot = _grid()
    policy = _policy()
    target_xy = snapshot.cell_to_world(10, 10)
    first = _select(policy, snapshot, _frontier(snapshot, "old-id", target_xy))
    assert first.state == "MOVE"
    policy.report_goal_failure("old-id", snapshot.revision)

    rolled = replace(snapshot, revision=2, origin_x_m=0.10, origin_y_m=0.05)
    decision = _select(policy, rolled,
                       _frontier(rolled, "changed-centroid-and-length", target_xy),
                       _frontier(rolled, "farther", (1.075, 1.075)))

    assert decision.frontier is not None
    assert decision.frontier.frontier_id == "farther"
    assert decision.goal_diagnostics["failed_spatially_suppressed_candidate_count"] == 1


def test_spatial_failure_expires_so_all_suppressed_frontiers_can_retry() -> None:
    snapshot = _grid()
    policy = _policy(failed_viewpoint_cooldown_revisions=3)
    target_xy = snapshot.cell_to_world(10, 10)
    _select(policy, snapshot, _frontier(snapshot, "old", target_xy))
    policy.report_goal_failure("old", snapshot.revision)

    at_expiry = replace(snapshot, revision=4)
    suppressed = _select(policy, at_expiry, _frontier(at_expiry, "id-v4", target_xy))
    assert suppressed.frontier is None
    assert suppressed.reason == "frontiers_temporarily_suppressed_after_collision"

    refreshed = replace(snapshot, revision=5)
    retry = _select(policy, refreshed, _frontier(refreshed, "id-v5", target_xy))
    assert retry.state == "MOVE"
    assert retry.frontier is not None and retry.frontier.frontier_id == "id-v5"


def test_explicit_failed_target_prevents_held_alias_goal_from_reappearing() -> None:
    snapshot = _grid()
    policy = _policy()
    target_xy = snapshot.cell_to_world(10, 10)
    _select(policy, snapshot, _frontier(snapshot, "current-alias", target_xy))
    # A late watchdog report can refer to an older ID after a planning update.
    policy.report_goal_failure("old-id", snapshot.revision, goal_world_xy=target_xy)

    decision = _select(policy, snapshot,
                       _frontier(snapshot, "current-alias", target_xy),
                       _frontier(snapshot, "farther", (1.075, 1.075)))

    assert decision.frontier is not None and decision.frontier.frontier_id == "farther"
    assert decision.goal_diagnostics["held_goal_recovery"] == (
        "held_cell_temporarily_suppressed_after_failure")


def test_failure_radius_keeps_legitimate_goal_a_quarter_meter_farther() -> None:
    snapshot = _grid()
    policy = _policy()
    target_xy = snapshot.cell_to_world(10, 10)
    farther_xy = (target_xy[0] + 0.25, target_xy[1])
    policy.report_goal_failure("overwritten-goal", snapshot.revision,
                               goal_world_xy=target_xy)

    decision = _select(policy, snapshot,
                       _frontier(snapshot, "failed-relabel", target_xy),
                       _frontier(snapshot, "nearby-valid", farther_xy))

    assert decision.state == "MOVE"
    assert decision.frontier is not None and decision.frontier.frontier_id == "nearby-valid"


def test_visited_viewpoint_survives_frontier_id_change_and_rolling_origin() -> None:
    snapshot = _grid()
    policy = _policy(visited_viewpoint_radius_m=0.30)
    reached_xy = snapshot.cell_to_world(10, 10)
    reached = _select(policy, snapshot, _frontier(snapshot, "reached", reached_xy),
                      robot_xy=reached_xy)
    assert reached.state == "OBSERVE"

    rolled = replace(snapshot, revision=2, origin_x_m=0.10, origin_y_m=0.05)
    decision = _select(policy, rolled,
                       _frontier(rolled, "reached-new-id", reached_xy),
                       _frontier(rolled, "new-view", (1.075, 1.075)))

    assert decision.frontier is not None and decision.frontier.frontier_id == "new-view"
    assert decision.goal_diagnostics["spatially_suppressed_candidate_count"] == 1


def test_controller_reached_endpoint_suppresses_drifted_active_target() -> None:
    snapshot = _grid()
    policy = _policy(visited_viewpoint_radius_m=0.30)
    reached_xy = snapshot.cell_to_world(10, 10)
    drifted_xy = snapshot.cell_to_world(10, 13)
    _select(policy, snapshot, _frontier(snapshot, "new-id", drifted_xy))

    # The controller finished its old path while planning had already selected
    # a boundary viewpoint 0.15m away. It must record the path's actual endpoint.
    policy.report_goal_reached("old-id", reached_xy)
    decision = _select(policy, snapshot,
                       _frontier(snapshot, "new-id", drifted_xy),
                       _frontier(snapshot, "farther", (1.075, 1.075)))

    assert decision.frontier is not None and decision.frontier.frontier_id == "farther"
    assert decision.goal_diagnostics["held_goal_recovery"] == "held_cell_near_visited_viewpoint"
    assert decision.goal_diagnostics["spatially_suppressed_candidate_count"] == 1


def test_late_reached_report_preserves_new_distinct_goal_even_with_same_id() -> None:
    snapshot = _grid()
    policy = _policy(visited_viewpoint_radius_m=0.30)
    reached_xy = snapshot.cell_to_world(10, 10)
    current_xy = snapshot.cell_to_world(10, 20)
    first = _select(policy, snapshot, _frontier(snapshot, "same-id", current_xy))
    assert first.frontier is not None

    policy.report_goal_reached("same-id", reached_xy)
    decision = _select(policy, snapshot,
                       _frontier(snapshot, "same-id", current_xy), now=2.0)

    assert decision.frontier is not None and decision.frontier.viewpoint_world_xy == current_xy
    assert decision.selected_at_monotonic_s == first.selected_at_monotonic_s
    assert decision.goal_diagnostics["held_goal_recovery"] == "held_cell_replanned"


def test_reached_report_marks_id_and_clears_same_endpoint_after_id_change() -> None:
    snapshot = _grid()
    policy = _policy()
    reached_xy = snapshot.cell_to_world(10, 10)
    _select(policy, snapshot, _frontier(snapshot, "active-alias", reached_xy))

    # Radius is disabled here: the old ID must still be recorded as visited,
    # and the current alias of the exact reached endpoint must be cleared.
    policy.report_goal_reached("old-id", reached_xy)
    decision = _select(policy, snapshot,
                       _frontier(snapshot, "old-id", reached_xy),
                       _frontier(snapshot, "farther", (1.075, 1.075)))

    assert decision.frontier is not None and decision.frontier.frontier_id == "farther"
    assert decision.goal_diagnostics["previous_goal_id"] is None
