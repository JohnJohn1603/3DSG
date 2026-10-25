"""Velocity-limited path follower with stale-data and acceleration gates."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

import numpy as np

from .contracts import ActionCommand, GridSnapshot, GridState, PathResult


@dataclass(frozen=True)
class ControllerConfig:
    max_speed_mps: float = 0.20
    nominal_speed_mps: float = 0.15
    max_yaw_rate_radps: float = math.radians(30.0)
    acceleration_mps2: float = 0.15
    yaw_acceleration_radps2: float = math.radians(45.0)
    lookahead_m: float = 0.40
    max_depth_age_s: float = 0.30
    max_nav_age_s: float = 1.0
    rotate_in_place_angle_rad: float = math.radians(50.0)
    obstacle_align_deadband_rad: float = math.radians(6.0)
    stop_distance_m: float = 0.25
    goal_reached_distance_m: float = 0.025
    max_dt_s: float = 0.10

    def __post_init__(self) -> None:
        positive = (self.max_speed_mps, self.nominal_speed_mps, self.max_yaw_rate_radps,
                    self.acceleration_mps2, self.yaw_acceleration_radps2, self.lookahead_m,
                    self.max_depth_age_s, self.max_nav_age_s, self.stop_distance_m,
                    self.goal_reached_distance_m, self.max_dt_s,
                    self.obstacle_align_deadband_rad)
        if any(v <= 0 or not math.isfinite(v) for v in positive):
            raise ValueError("controller limits must be finite and positive")
        if self.nominal_speed_mps > self.max_speed_mps:
            raise ValueError("nominal speed cannot exceed max speed")


class Controller:
    """Pure command generator; caller must execute commands collision-filtered."""

    _HARD_STOP_REASONS = frozenset({
        "nav_stale", "depth_stale", "depth_coverage_insufficient",
        "front_obstacle_brake", "no_valid_path", "path_map_revision_stale",
        "empty_path", "path_pose_unavailable", "goal_reached",
        "path_segment_blocked",
    })

    def __init__(self, config: ControllerConfig, *,
                 traversable: Callable[[GridSnapshot], np.ndarray] | None = None) -> None:
        self.config = config
        self._traversable = traversable
        self._linear = 0.0
        self._yaw_rate = 0.0
        self._path_signature: tuple[tuple[float, float], ...] | None = None
        self._path_progress_m = 0.0

    def set_traversable(self, provider: Callable[[GridSnapshot], np.ndarray]) -> None:
        """Share the planner's observed-footprint rule without simulator geometry."""
        self._traversable = provider

    def tick(self, *, robot_xy: tuple[float, float], yaw_rad: float,
             now_monotonic_s: float, dt_s: float, depth_host_monotonic_s: float,
             depth_front_min_m: float | None, depth_coverage_ok: bool,
             snapshot: GridSnapshot | None,
             path: PathResult | None, frame_id: str,
             blocked_edges: frozenset[tuple[tuple[int, int], tuple[int, int]]] = frozenset()
             ) -> ActionCommand:
        dt = min(max(float(dt_s), 0.0), self.config.max_dt_s)
        reason = "tracking"
        target_v = target_w = 0.0
        nav_age = math.inf if snapshot is None else now_monotonic_s - snapshot.host_monotonic_s
        depth_age = now_monotonic_s - depth_host_monotonic_s

        if snapshot is None or nav_age < 0 or nav_age > self.config.max_nav_age_s:
            reason = "nav_stale"
        elif depth_age < 0 or depth_age > self.config.max_depth_age_s:
            reason = "depth_stale"
        elif not depth_coverage_ok:
            reason = "depth_coverage_insufficient"
        elif path is None or not path.success:
            reason = "no_valid_path"
        elif path.map_revision != snapshot.revision:
            reason = "path_map_revision_stale"
        elif not path.cells:
            reason = "empty_path"
        else:
            goal_xy = snapshot.cell_to_world(*path.cells[-1])
            target_xy = self._lookahead(snapshot, path.cells, robot_xy,
                                        blocked_edges=blocked_edges)
            if math.dist(robot_xy, goal_xy) <= self.config.goal_reached_distance_m:
                reason = "goal_reached"
            elif target_xy is None:
                reason = "path_segment_blocked"
            else:
                dx, dy = target_xy[0] - robot_xy[0], target_xy[1] - robot_xy[1]
                distance = math.hypot(dx, dy)
                heading_error = _wrap(math.atan2(dy, dx) - yaw_rad)
                front_blocked = (depth_front_min_m is not None
                                 and depth_front_min_m <= self.config.stop_distance_m)
                # Habitat translates along the current yaw, not the lookahead
                # bearing. Even a safe target can point around a rejected
                # edge while the next forward pulse still crosses that edge.
                forward_clear = True
                if self._traversable is not None or blocked_edges:
                    horizon = min(distance, max(snapshot.resolution_m * math.sqrt(2.0),
                                                self.config.max_speed_mps * self.config.max_dt_s))
                    forward_xy = (robot_xy[0] + horizon * math.cos(yaw_rad),
                                  robot_xy[1] + horizon * math.sin(yaw_rad))
                    source = snapshot.world_to_cell(*robot_xy)
                    anchor = (source if source == path.cells[0]
                              and snapshot.states[source] == GridState.FREE else None)
                    forward_clear = _segment_clear(
                        snapshot, (self._traversable(snapshot) if self._traversable is not None
                                   else snapshot.states == GridState.FREE), robot_xy, forward_xy,
                        anchor, blocked_edges)
                if front_blocked and abs(heading_error) <= self.config.obstacle_align_deadband_rad:
                    reason = "front_obstacle_brake"
                elif (not forward_clear
                      and abs(heading_error) <= self.config.obstacle_align_deadband_rad):
                    # A discrete turn cannot reliably resolve a corridor
                    # narrower than the alignment deadband. Request another
                    # local plan instead of oscillating forever at zero speed.
                    reason = "path_segment_blocked"
                elif (front_blocked or not forward_clear
                      or abs(heading_error) > self.config.rotate_in_place_angle_rad):
                    target_w = _clamp(heading_error * 1.5, -self.config.max_yaw_rate_radps,
                                      self.config.max_yaw_rate_radps)
                    # Forward depth certifies a translation corridor. A close
                    # return must not prevent turning toward a valid route:
                    # the simulator's cylindrical footprint is unchanged by
                    # in-place yaw. Stop aligning within one discrete turn so
                    # an obstacle on the route still reaches brake recovery.
                    reason = "obstacle_align_heading" if front_blocked else "align_heading"
                else:
                    target_w = _clamp(heading_error * 1.5, -self.config.max_yaw_rate_radps,
                                      self.config.max_yaw_rate_radps)
                    # A simple curvature cap; slows down before tight turns.
                    curvature = 2.0 * math.sin(heading_error) / max(distance, 1e-6)
                    curve_cap = math.sqrt(0.20 / max(abs(curvature), 1e-6))
                    age_cap = self.config.nominal_speed_mps * max(0.0, 1.0 - nav_age / self.config.max_nav_age_s)
                    target_v = min(self.config.nominal_speed_mps, self.config.max_speed_mps,
                                   curve_cap, age_cap, distance / self.config.max_dt_s)
                    if abs(heading_error) > math.radians(25.0):
                        target_v *= 0.5

        if reason in self._HARD_STOP_REASONS:
            # The simulator applies discrete, collision-filtered steps and has
            # no modeled inertia. An invalid/stale path must not keep issuing
            # forward pulses while a virtual acceleration ramp winds down.
            self._linear = 0.0
            self._yaw_rate = 0.0
        else:
            # Turning in place is a motion constraint, so do not let the
            # virtual acceleration ramp emit residual forward steps here.
            self._linear = (0.0 if reason in {"align_heading", "obstacle_align_heading"} else
                            min(distance / self.config.max_dt_s,
                                _approach(self._linear, target_v,
                                          self.config.acceleration_mps2 * dt)))
            self._yaw_rate = _approach(self._yaw_rate, target_w,
                                       self.config.yaw_acceleration_radps2 * dt)
        evidence_deadline = depth_host_monotonic_s + self.config.max_depth_age_s
        if snapshot is not None:
            evidence_deadline = min(evidence_deadline,
                                    snapshot.host_monotonic_s + self.config.max_nav_age_s)
        # Every command carries its evidence deadline; hard-stop reasons also
        # reset the discrete simulator's velocity state before returning.
        return ActionCommand(
            linear_mps=self._linear,
            yaw_rate_radps=self._yaw_rate,
            reason=reason,
            frame_id=frame_id,
            map_revision=-1 if snapshot is None else snapshot.revision,
            # Commands expire at the evidence deadlines, not at an arbitrary
            # horizon after planning. The runtime rechecks freshness at the
            # end of synchronous map/planner work before it returns a command.
            valid_until_monotonic_s=evidence_deadline,
            metadata={"depth_age_s": depth_age, "nav_age_s": nav_age},
        )

    def reset(self) -> None:
        self._linear = 0.0
        self._yaw_rate = 0.0
        self._path_signature = None
        self._path_progress_m = 0.0

    def _lookahead(self, snapshot: GridSnapshot, cells: tuple[tuple[int, int], ...],
                   robot_xy: tuple[float, float], *,
                   blocked_edges: frozenset[tuple[tuple[int, int], tuple[int, int]]] = frozenset()
                   ) -> tuple[float, float] | None:
        points = [snapshot.cell_to_world(r, c) for r, c in cells]
        if not points:
            return None
        signature = tuple(points)
        if signature != self._path_signature:
            self._path_signature = signature
            self._path_progress_m = 0.0
        if len(points) == 1:
            if self._traversable is not None or blocked_edges:
                source = snapshot.world_to_cell(*robot_xy)
                anchor = (source if source == cells[0]
                          and snapshot.states[source] == GridState.FREE else None)
                mask = (self._traversable(snapshot) if self._traversable is not None
                        else snapshot.states == GridState.FREE)
                if not _segment_clear(snapshot, mask,
                                      robot_xy, points[0], anchor, blocked_edges):
                    return None
            return points[0]

        cumulative = [0.0]
        for a, b in zip(points, points[1:]):
            cumulative.append(cumulative[-1] + math.dist(a, b))
        minimum_progress = max(0.0, self._path_progress_m - snapshot.resolution_m * 2.0)
        candidates: list[tuple[float, float]] = []
        for index, (a, b) in enumerate(zip(points, points[1:])):
            segment_length = cumulative[index + 1] - cumulative[index]
            if segment_length <= 1e-9 or cumulative[index + 1] < minimum_progress:
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = min(1.0, max(0.0, ((robot_xy[0] - a[0]) * dx + (robot_xy[1] - a[1]) * dy)
                                  / (segment_length * segment_length)))
            projected = (a[0] + t * dx, a[1] + t * dy)
            progress = cumulative[index] + t * segment_length
            if progress >= minimum_progress:
                candidates.append((math.dist(projected, robot_xy), progress))
        if not candidates:
            return None if self._traversable is not None or blocked_edges else points[-1]
        # Prefer the furthest progress when a loop or adjacent segments give
        # equal-distance projections; progress never moves backward on a path.
        _, projected_progress = min(candidates, key=lambda item: (item[0], -item[1]))
        self._path_progress_m = max(self._path_progress_m, projected_progress)
        target_s = min(cumulative[-1], self._path_progress_m + self.config.lookahead_m)
        if self._traversable is None:
            # Standalone callers may not supply footprint geometry. Stay on
            # the current straight run rather than shortcutting a corner.
            for index in range(1, len(points) - 1):
                if cumulative[index] <= self._path_progress_m + 1e-9:
                    continue
                before = (points[index][0] - points[index - 1][0],
                          points[index][1] - points[index - 1][1])
                after = (points[index + 1][0] - points[index][0],
                         points[index + 1][1] - points[index][1])
                cross = before[0] * after[1] - before[1] * after[0]
                dot = before[0] * after[0] + before[1] * after[1]
                if abs(cross) > 1e-9 or dot <= 0:
                    target_s = min(target_s, cumulative[index])
                    break
        target = points[-1]
        for index, (a, b) in enumerate(zip(points, points[1:])):
            segment_length = cumulative[index + 1] - cumulative[index]
            if target_s <= cumulative[index + 1] or index == len(points) - 2:
                if segment_length <= 1e-9:
                    target = b
                    break
                t = min(1.0, max(0.0, (target_s - cumulative[index]) / segment_length))
                target = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                break
        if self._traversable is None and not blocked_edges:
            return target
        traversable = (self._traversable(snapshot) if self._traversable is not None
                       else snapshot.states == GridState.FREE)
        source = snapshot.world_to_cell(*robot_xy)
        # A successful path can have one externally validated current-pose
        # anchor. Never extend that exception into another cell or unknown.
        anchor = (source if source == cells[0]
                  and snapshot.states[source] == GridState.FREE else None)
        choices = [target]
        choices.extend(points[index] for index in range(len(points) - 1, -1, -1)
                       if self._path_progress_m + 1e-9 < cumulative[index] < target_s)
        for choice in choices:
            if _segment_clear(snapshot, traversable, robot_xy, choice, anchor, blocked_edges):
                return choice
        # A* starts at the cell center. The actual sub-cell pose can be near
        # a rejected edge, so even its first diagonal waypoint may cross that
        # edge before entering the planned neighbor. Recenter within the
        # current cell before following the detour instead of discarding it.
        if (source == cells[0] and math.dist(robot_xy, points[0]) > 1e-6
                and _segment_clear(snapshot, traversable, robot_xy, points[0],
                                   anchor, blocked_edges)):
            return points[0]
        return None


def _segment_clear(snapshot: GridSnapshot, traversable: np.ndarray,
                   start: tuple[float, float], end: tuple[float, float],
                   start_anchor: tuple[int, int] | None = None,
                   blocked_edges: frozenset[tuple[tuple[int, int], tuple[int, int]]] = frozenset()
                   ) -> bool:
    """Supercover LOS on the planner mask and temporary collision overlay."""
    source, destination = snapshot.world_to_cell(*start), snapshot.world_to_cell(*end)
    if source is None or destination is None:
        return False

    def clear(row: int, col: int) -> bool:
        return (0 <= row < traversable.shape[0] and 0 <= col < traversable.shape[1]
                and ((row, col) == start_anchor or bool(traversable[row, col])))

    row, col = source
    if not clear(row, col):
        return False
    x0 = (start[0] - snapshot.origin_x_m) / snapshot.resolution_m
    y0 = (start[1] - snapshot.origin_y_m) / snapshot.resolution_m
    dx = (end[0] - start[0]) / snapshot.resolution_m
    dy = (end[1] - start[1]) / snapshot.resolution_m
    step_c, step_r = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    delta_c = math.inf if dx == 0 else abs(1.0 / dx)
    delta_r = math.inf if dy == 0 else abs(1.0 / dy)
    next_c = math.inf if dx == 0 else ((col + 1 - x0) / dx if dx > 0 else (col - x0) / dx)
    next_r = math.inf if dy == 0 else ((row + 1 - y0) / dy if dy > 0 else (row - y0) / dy)
    while (row, col) != destination:
        previous = (row, col)
        if math.isclose(next_c, next_r, rel_tol=0.0, abs_tol=1e-10):
            if not clear(row, col + step_c) or not clear(row + step_r, col):
                return False
            row, col = row + step_r, col + step_c
            next_r, next_c = next_r + delta_r, next_c + delta_c
        elif next_c < next_r:
            col += step_c
            next_c += delta_c
        else:
            row += step_r
            next_r += delta_r
        if not clear(row, col):
            return False
        current = (row, col)
        edge = (previous, current) if previous <= current else (current, previous)
        if edge in blocked_edges:
            return False
    return True


def _approach(current: float, target: float, delta: float) -> float:
    return min(current + delta, target) if current < target else max(current - delta, target)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi
