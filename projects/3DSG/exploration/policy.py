"""Nearest reachable frontier baseline with goal hysteresis."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import threading

from .contracts import GridSnapshot, GridState, PathResult
from .frontier import Frontier, FrontierSet
from .planner import AStarPlanner


_GOAL_TARGET_DRIFT_CELLS = 2.0
_GOAL_TARGET_DRIFT_MIN_M = 0.10


@dataclass(frozen=True)
class PolicyConfig:
    goal_hold_s: float = 3.0
    switch_improvement: float = 0.20
    reached_distance_m: float = 0.20
    visited_viewpoint_radius_m: float = 0.0
    failed_viewpoint_radius_m: float = 0.0
    failed_viewpoint_cooldown_revisions: int = 100

    def __post_init__(self) -> None:
        if (self.goal_hold_s < 0 or not 0 <= self.switch_improvement < 1
                or self.reached_distance_m <= 0 or self.visited_viewpoint_radius_m < 0
                or not math.isfinite(self.failed_viewpoint_radius_m)
                or self.failed_viewpoint_radius_m < 0
                or self.failed_viewpoint_cooldown_revisions < 1):
            raise ValueError("invalid exploration policy configuration")


@dataclass(frozen=True)
class NavigationDecision:
    state: str
    frontier: Frontier | None
    path: PathResult | None
    reason: str
    selected_at_monotonic_s: float | None
    path_failure_reasons: tuple[str, ...] = ()
    goal_diagnostics: dict[str, object] = field(default_factory=dict)


class FrontierPolicy:
    """Choose minimum path-cost frontier; preserve a viable goal across updates."""

    def __init__(self, planner: AStarPlanner, config: PolicyConfig = PolicyConfig()) -> None:
        self.planner = planner
        self.config = config
        self._goal: Frontier | None = None
        self._goal_path: PathResult | None = None
        self._goal_world_xy: tuple[float, float] | None = None
        self._selected_at: float | None = None
        self._visited_ids: set[str] = set()
        self._visited_viewpoints: list[tuple[float, float]] = []
        self._failed_until_revision: dict[str, int] = {}
        self._failure_counts: dict[str, int] = {}
        self._failed_viewpoints: list[tuple[tuple[float, float], int]] = []
        self._failed_edges_until_revision: dict[
            tuple[tuple[int, int], tuple[int, int]], int
        ] = {}
        self._failure_lock = threading.RLock()

    def report_goal_failure(self, frontier_id: str, map_revision: int, *,
                            blocked_edge: tuple[tuple[int, int], tuple[int, int]] | None = None,
                            goal_world_xy: tuple[float, float] | None = None) -> None:
        """Temporarily avoid a rejected goal and optionally its failed local step.

        A world-space record survives frontier ID changes and rolling map origins.
        An explicit target also works when a runtime recovery decision has already
        replaced the active goal. Spatial suppression is opt-in and expires after
        a bounded number of map revisions, allowing newly observed routes a retry.
        """
        if goal_world_xy is not None:
            if len(goal_world_xy) != 2 or not all(math.isfinite(v) for v in goal_world_xy):
                raise ValueError("failed goal world position must be finite XY")
            goal_world_xy = (float(goal_world_xy[0]), float(goal_world_xy[1]))
        elif self._goal is not None and self._goal.frontier_id == frontier_id:
            goal_world_xy = self._goal_world_xy
        with self._failure_lock:
            if frontier_id:
                failures = self._failure_counts.get(frontier_id, 0) + 1
                self._failure_counts[frontier_id] = failures
                cooldown_revisions = 100 if failures == 1 else 500
                self._failed_until_revision[frontier_id] = map_revision + cooldown_revisions
            radius = self.config.failed_viewpoint_radius_m
            if goal_world_xy is not None and radius > 0:
                # Refresh a small local record instead of accumulating all the
                # changing IDs of the same failed boundary in spatial memory.
                self._failed_viewpoints = [
                    (point, until) for point, until in self._failed_viewpoints
                    if map_revision <= until and math.dist(point, goal_world_xy) > radius
                ]
                self._failed_viewpoints.append((
                    goal_world_xy,
                    map_revision + self.config.failed_viewpoint_cooldown_revisions,
                ))
                del self._failed_viewpoints[:-256]
            if blocked_edge is not None:
                self._failed_edges_until_revision[_edge_key(*blocked_edge)] = map_revision + 100
        if frontier_id and self._goal is not None and self._goal.frontier_id == frontier_id:
            self._clear_goal()

    def report_goal_reached(self, frontier_id: str,
                            goal_world_xy: tuple[float, float]) -> None:
        """Record the controller's exact reached target on the policy thread.

        The active frontier may already have moved or changed ID since the
        controller received its path. Preserve a newer, distinct active target;
        spatial visited memory still filters nearby aliases on the next select.
        """
        if len(goal_world_xy) != 2 or not all(math.isfinite(v) for v in goal_world_xy):
            raise ValueError("reached goal world position must be finite XY")
        reached_xy = (float(goal_world_xy[0]), float(goal_world_xy[1]))
        if frontier_id:
            self._visited_ids.add(frontier_id)
        self._mark_viewpoint_visited(reached_xy)
        if (self._goal_world_xy is not None
                and math.dist(self._goal_world_xy, reached_xy) <= 1e-6):
            self._clear_goal()

    def blocked_edges(self, map_revision: int) -> frozenset[tuple[tuple[int, int], tuple[int, int]]]:
        """Return the short-lived planner overlay without changing NavMap state."""
        with self._failure_lock:
            expired = [edge for edge, until in self._failed_edges_until_revision.items()
                       if map_revision > until]
            for edge in expired:
                self._failed_edges_until_revision.pop(edge, None)
            return frozenset(edge for edge, until in self._failed_edges_until_revision.items()
                             if map_revision <= until)

    def select(self, *, snapshot: GridSnapshot, frontiers: FrontierSet,
               robot_xy: tuple[float, float], now_monotonic_s: float,
               allow_validated_start_anchor: bool = False) -> NavigationDecision:
        previous_goal = self._goal
        previous_goal_xy = self._goal_world_xy
        previous_selected_at = self._selected_at
        previous_goal_distance = (None if previous_goal_xy is None else
                                  math.dist(previous_goal_xy, robot_xy))
        previous_goal_age = (None if previous_selected_at is None else
                             max(0.0, now_monotonic_s - previous_selected_at))
        if frontiers.map_revision != snapshot.revision:
            return NavigationDecision(
                "WAIT", None, None, "frontier_snapshot_stale", self._selected_at,
                goal_diagnostics={
                    "previous_goal_id": None if previous_goal is None else previous_goal.frontier_id,
                    "previous_goal_xy": None if previous_goal_xy is None else list(previous_goal_xy),
                    "previous_goal_distance_m": previous_goal_distance,
                    "previous_goal_age_s": previous_goal_age,
                    "candidate_set_status": "frontier_snapshot_stale",
                },
            )
        reachable: list[tuple[float, Frontier, PathResult]] = []
        path_failures: list[str] = []
        with self._failure_lock:
            expired_failures = [key for key, until in self._failed_until_revision.items()
                                if snapshot.revision > until]
            for key in expired_failures:
                self._failed_until_revision.pop(key, None)
                self._failure_counts.pop(key, None)
            self._failed_viewpoints = [(point, until)
                                       for point, until in self._failed_viewpoints
                                       if snapshot.revision <= until]
            failed_viewpoints = tuple(point for point, _until in self._failed_viewpoints)
            failed_until_revision = dict(self._failed_until_revision)
        blocked_edges = self.blocked_edges(snapshot.revision)
        collision_suppressed = False
        spatially_suppressed = 0
        failed_spatially_suppressed = 0
        eligible: list[Frontier] = []
        for frontier in frontiers.frontiers:
            blocked_until = failed_until_revision.get(frontier.frontier_id, -1)
            if (frontier.map_revision != snapshot.revision or frontier.frontier_id in self._visited_ids
                    or snapshot.revision <= blocked_until):
                if snapshot.revision <= blocked_until:
                    collision_suppressed = True
                continue
            viewpoint_xy = snapshot.cell_to_world(*frontier.viewpoint_cell)
            if (self.config.failed_viewpoint_radius_m > 0
                    and any(math.dist(viewpoint_xy, point)
                            <= self.config.failed_viewpoint_radius_m
                            for point in failed_viewpoints)):
                failed_spatially_suppressed += 1
                collision_suppressed = True
                continue
            if (self.config.visited_viewpoint_radius_m > 0
                    and self._near_visited_viewpoint(viewpoint_xy)):
                spatially_suppressed += 1
                continue
            eligible.append(frontier)
        batch_planner = getattr(self.planner, "plan_many", None)
        batch_paths = None
        if callable(batch_planner):
            goals = [frontier.viewpoint_cell for frontier in eligible]
            # Include the held exact world target in the same wavefront. Its
            # boundary may have moved and vanished from the candidate set.
            held_cell = (None if self._goal_world_xy is None else
                         snapshot.world_to_cell(*self._goal_world_xy))
            if (held_cell is not None and snapshot.states[held_cell] == GridState.FREE
                    and not self._near_visited_viewpoint(self._goal_world_xy)
                    and not (self.config.failed_viewpoint_radius_m > 0
                             and any(math.dist(self._goal_world_xy, point)
                                     <= self.config.failed_viewpoint_radius_m
                                     for point in failed_viewpoints))):
                goals.append(held_cell)
            if goals:
                batch_paths = batch_planner(
                    snapshot, robot_xy, goals,
                    allow_validated_start_anchor=allow_validated_start_anchor,
                    blocked_edges=blocked_edges,
                )
        for frontier in eligible:
            path = (batch_paths[frontier.viewpoint_cell] if batch_paths is not None else
                    self.planner.plan(
                        snapshot, robot_xy, frontier.viewpoint_cell,
                        allow_validated_start_anchor=allow_validated_start_anchor,
                        blocked_edges=blocked_edges,
                    ))
            if path.success:
                reachable.append((path.cost_m, frontier, path))
            else:
                path_failures.append(path.reason)
        reachable.sort(key=lambda item: (item[0], item[1].frontier_id))
        diagnostic_candidates = tuple(reachable)

        current = self._match_current(reachable, snapshot)
        same_id_candidate = next((item[1] for item in reachable
                                  if previous_goal is not None
                                  and item[1].frontier_id == previous_goal.frontier_id), None)
        same_id_target_shift = (None if same_id_candidate is None or previous_goal_xy is None else
                                math.dist(snapshot.cell_to_world(*same_id_candidate.viewpoint_cell),
                                          previous_goal_xy))
        target_match_radius = max(
            _GOAL_TARGET_DRIFT_MIN_M,
            _GOAL_TARGET_DRIFT_CELLS * snapshot.resolution_m,
        )
        previous_goal_match = (
            "no_previous_goal" if previous_goal is None else
            "matched_frontier_id" if current is not None
            and current[1].frontier_id == previous_goal.frontier_id else
            "frontier_id_target_moved_over_match_radius"
            if same_id_target_shift is not None and
            same_id_target_shift > target_match_radius else
            "matched_nearby_candidate" if current is not None else
            "frontier_id_and_nearby_match_missing"
        )
        held_goal_recovery = "not_attempted"
        if current is None and self._goal is not None and self._goal_world_xy is not None:
            # A frontier boundary can move after the robot observes it, even
            # though the selected viewpoint remains a valid free-space goal.
            # Keep that exact target while it remains traversable and replan
            # against the current snapshot; do not abandon it just because
            # its frontier component ID disappeared or jumped spatially.
            held_cell = snapshot.world_to_cell(*self._goal_world_xy)
            if (self.config.failed_viewpoint_radius_m > 0
                    and any(math.dist(self._goal_world_xy, point)
                            <= self.config.failed_viewpoint_radius_m
                            for point in failed_viewpoints)):
                held_goal_recovery = "held_cell_temporarily_suppressed_after_failure"
            elif self._near_visited_viewpoint(self._goal_world_xy):
                held_goal_recovery = "held_cell_near_visited_viewpoint"
            elif held_cell is None:
                held_goal_recovery = "held_cell_outside_snapshot"
            elif snapshot.states[held_cell] != GridState.FREE:
                held_goal_recovery = f"held_cell_{GridState(int(snapshot.states[held_cell])).name.lower()}"
            else:
                held_path = (batch_paths[held_cell] if batch_paths is not None else
                             self.planner.plan(
                                 snapshot, robot_xy, held_cell,
                                 allow_validated_start_anchor=allow_validated_start_anchor,
                                 blocked_edges=blocked_edges,
                             ))
                if held_path.success:
                    held_frontier = replace(
                        self._goal, map_revision=snapshot.revision,
                        viewpoint_cell=held_cell,
                        viewpoint_world_xy=self._goal_world_xy,
                    )
                    current = (held_path.cost_m, held_frontier, held_path)
                    held_goal_recovery = "held_cell_replanned"
                else:
                    held_goal_recovery = f"held_cell_path_failed:{held_path.reason}"

        def goal_diagnostics(*, decision_reason: str, current_goal=None, best_goal=None,
                             improvement: float | None = None,
                             selected_goal: Frontier | None = None,
                             candidate_status: str = "available") -> dict[str, object]:
            selected_xy = (None if selected_goal is None else
                           selected_goal.viewpoint_world_xy or
                           snapshot.cell_to_world(*selected_goal.viewpoint_cell))
            # Keep a small, deterministic sample of alternatives in the event
            # journal. This is diagnostic only: selection still uses the same
            # path-cost ordering and hysteresis below.
            candidate_sample = [
                {
                    "frontier_id": candidate.frontier_id,
                    "viewpoint_xy": list(snapshot.cell_to_world(*candidate.viewpoint_cell)),
                    "path_cost_m": cost,
                    "predicted_gain_cells": candidate.predicted_gain_cells,
                }
                for cost, candidate, _path in diagnostic_candidates[:8]
            ]
            return {
                "previous_goal_id": None if previous_goal is None else previous_goal.frontier_id,
                "previous_goal_xy": None if previous_goal_xy is None else list(previous_goal_xy),
                "previous_goal_distance_m": previous_goal_distance,
                "previous_goal_age_s": previous_goal_age,
                "previous_goal_match": previous_goal_match,
                "same_id_target_shift_m": same_id_target_shift,
                "goal_target_match_radius_m": target_match_radius,
                "held_goal_recovery": held_goal_recovery,
                "candidate_set_status": candidate_status,
                "reachable_candidate_count": len(diagnostic_candidates),
                "reachable_candidate_sample": candidate_sample,
                "spatially_suppressed_candidate_count": spatially_suppressed,
                "visited_viewpoint_radius_m": self.config.visited_viewpoint_radius_m,
                "failed_spatially_suppressed_candidate_count": failed_spatially_suppressed,
                "failed_viewpoint_radius_m": self.config.failed_viewpoint_radius_m,
                "path_failure_count": len(path_failures),
                "current_goal_id": None if current_goal is None else current_goal[1].frontier_id,
                "current_goal_xy": None if current_goal is None else list(
                    snapshot.cell_to_world(*current_goal[1].viewpoint_cell)),
                "current_goal_distance_m": None if current_goal is None else math.dist(
                    snapshot.cell_to_world(*current_goal[1].viewpoint_cell), robot_xy),
                "current_goal_path_cost_m": None if current_goal is None else current_goal[0],
                "best_candidate_id": None if best_goal is None else best_goal[1].frontier_id,
                "best_candidate_xy": None if best_goal is None else list(
                    snapshot.cell_to_world(*best_goal[1].viewpoint_cell)),
                "best_candidate_path_cost_m": None if best_goal is None else best_goal[0],
                "switch_improvement_fraction": improvement,
                "goal_hold_remaining_s": (None if previous_goal_age is None else
                    max(0.0, self.config.goal_hold_s - previous_goal_age)),
                "decision_reason": decision_reason,
                "selected_goal_id": None if selected_goal is None else selected_goal.frontier_id,
                "selected_goal_xy": None if selected_xy is None else list(selected_xy),
            }
        if current is not None:
            goal_distance = math.dist(snapshot.cell_to_world(*current[1].viewpoint_cell), robot_xy)
            if goal_distance <= self.config.reached_distance_m:
                self._visited_ids.add(current[1].frontier_id)
                reached_xy = snapshot.cell_to_world(*current[1].viewpoint_cell)
                self._mark_viewpoint_visited(reached_xy)
                reachable = [item for item in reachable
                             if item[1].frontier_id != current[1].frontier_id]
                reached_frontier = current[1]
                self._clear_goal()
                return NavigationDecision("OBSERVE", reached_frontier, None,
                                          "frontier_viewpoint_reached", now_monotonic_s,
                                          goal_diagnostics=goal_diagnostics(
                                              decision_reason="frontier_viewpoint_reached",
                                              current_goal=current,
                                              selected_goal=reached_frontier,
                                          ))
        best = reachable[0] if reachable else None

        if current is not None:
            if best is None or best[1].frontier_id == current[1].frontier_id:
                self._remember(current[1], current[2], snapshot, now_monotonic_s, preserve_time=True)
                return NavigationDecision(
                    "MOVE", self._goal, self._goal_path, "goal_retained", self._selected_at,
                    goal_diagnostics=goal_diagnostics(
                        decision_reason="goal_retained", current_goal=current,
                        best_goal=best, selected_goal=self._goal,
                    ),
                )
            held = self._selected_at is not None and now_monotonic_s - self._selected_at < self.config.goal_hold_s
            improvement = (current[0] - best[0]) / max(current[0], 1e-6)
            if held or improvement < self.config.switch_improvement:
                self._remember(current[1], current[2], snapshot, now_monotonic_s, preserve_time=True)
                return NavigationDecision(
                    "MOVE", self._goal, self._goal_path, "goal_hysteresis", self._selected_at,
                    goal_diagnostics=goal_diagnostics(
                        decision_reason="goal_hysteresis", current_goal=current,
                        best_goal=best, improvement=improvement, selected_goal=self._goal,
                    ),
                )

        already_viewed = False
        while reachable:
            best = reachable.pop(0)
            goal_distance = math.dist(snapshot.cell_to_world(*best[1].viewpoint_cell), robot_xy)
            if goal_distance <= self.config.reached_distance_m:
                self._visited_ids.add(best[1].frontier_id)
                reached_xy = snapshot.cell_to_world(*best[1].viewpoint_cell)
                self._mark_viewpoint_visited(reached_xy)
                if self.config.visited_viewpoint_radius_m > 0:
                    before = len(reachable)
                    reachable = [item for item in reachable
                                 if not self._near_visited_viewpoint(
                                     snapshot.cell_to_world(*item[1].viewpoint_cell))]
                    spatially_suppressed += before - len(reachable)
                already_viewed = True
                continue
            self._remember(best[1], best[2], snapshot, now_monotonic_s, preserve_time=False)
            return NavigationDecision("MOVE", self._goal, self._goal_path,
                                      "nearest_reachable_frontier", self._selected_at,
                                      goal_diagnostics=goal_diagnostics(
                                          decision_reason="nearest_reachable_frontier",
                                          best_goal=best, selected_goal=self._goal,
                                      ))
        self._clear_goal()
        if already_viewed:
            return NavigationDecision("OBSERVE", None, None,
                                      "frontier_viewpoint_already_reached", now_monotonic_s,
                                      goal_diagnostics=goal_diagnostics(
                                          decision_reason="frontier_viewpoint_already_reached",
                                          candidate_status="candidate_reached_during_selection",
                                      ))
        reason = ("frontiers_temporarily_suppressed_after_collision"
                  if frontiers.frontiers and collision_suppressed else
                  "no_reachable_frontier_path" if frontiers.frontiers
                  else frontiers.reason or "no_reachable_frontier")
        return NavigationDecision(
            "OBSERVE", None, None, reason, None, tuple(sorted(path_failures)),
            goal_diagnostics=goal_diagnostics(
                decision_reason=reason, candidate_status="no_reachable_candidate",
            ),
        )

    def _match_current(self, reachable: list[tuple[float, Frontier, PathResult]],
                       snapshot: GridSnapshot) -> tuple[float, Frontier, PathResult] | None:
        if self._goal is None:
            return None
        exact = [item for item in reachable if item[1].frontier_id == self._goal.frontier_id]
        if exact:
            old_point = self._goal_world_xy
            if (old_point is None or
                    math.dist(snapshot.cell_to_world(*exact[0][1].viewpoint_cell), old_point)
                    <= max(_GOAL_TARGET_DRIFT_MIN_M,
                           _GOAL_TARGET_DRIFT_CELLS * snapshot.resolution_m)):
                return exact[0]
        # Component IDs can move slightly as the observed boundary grows. Keep
        # the prior target only when a new candidate remains spatially nearby.
        old_point = self._goal_world_xy
        if old_point is None:
            return None
        match_radius = max(_GOAL_TARGET_DRIFT_MIN_M,
                           _GOAL_TARGET_DRIFT_CELLS * snapshot.resolution_m)
        nearby = [item for item in reachable
                  if math.dist(snapshot.cell_to_world(*item[1].viewpoint_cell), old_point)
                  <= match_radius]
        if not nearby:
            return
        return min(nearby, key=lambda item: math.dist(snapshot.cell_to_world(*item[1].viewpoint_cell), old_point))

    def _remember(self, frontier: Frontier, path: PathResult, snapshot: GridSnapshot,
                  now: float, *, preserve_time: bool) -> None:
        if not preserve_time or self._goal is None:
            self._selected_at = now
        self._goal, self._goal_path = frontier, path
        self._goal_world_xy = snapshot.cell_to_world(*frontier.viewpoint_cell)

    def _clear_goal(self) -> None:
        self._goal = None
        self._goal_path = None
        self._goal_world_xy = None
        self._selected_at = None

    def visited_viewpoints(self) -> tuple[tuple[float, float], ...]:
        """Immutable world-space exclusions for the next frontier extraction.

        Called by the policy-owning planner thread, after queued arrivals have
        been applied, so alternatives can be generated outside visited disks.
        """
        return tuple(self._visited_viewpoints)

    def _near_visited_viewpoint(self, viewpoint_xy: tuple[float, float]) -> bool:
        radius = self.config.visited_viewpoint_radius_m
        return radius > 0 and any(math.dist(viewpoint_xy, visited) < radius
                                  for visited in self._visited_viewpoints)

    def _mark_viewpoint_visited(self, viewpoint_xy: tuple[float, float]) -> None:
        radius = self.config.visited_viewpoint_radius_m
        if radius <= 0 or self._near_visited_viewpoint(viewpoint_xy):
            return
        self._visited_viewpoints.append(viewpoint_xy)
        if len(self._visited_viewpoints) > 256:
            del self._visited_viewpoints[:-256]


def _edge_key(a: tuple[int, int], b: tuple[int, int]):
    return (a, b) if a <= b else (b, a)
