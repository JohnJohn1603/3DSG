"""Navigation-only runtime tick connecting observed map, frontier and control."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import math
import time

from .contracts import ActionCommand, FramePacket, GridSnapshot, PathResult, RobotPose2
from .controller import Controller
from .depth_safety import DepthSafetyGate, DepthSafetyResult
from .frontier import FrontierExtractor, FrontierSet
from .map_worker import LatestMapWorker
from .nav_mapper import NavMapper
from .planner import AStarPlanner
from .plan_worker import LatestPlanWorker
from .policy import FrontierPolicy, NavigationDecision


@dataclass(frozen=True)
class NavigationRuntimeConfig:
    map_update_hz: float = 5.0
    planner_hz: float = 1.0
    initial_scan_yaw_rad: float = math.tau
    initial_scan_rate_radps: float = math.radians(30.0)
    collision_recovery_yaw_rad: float = math.radians(20.0)
    async_mapping: bool = False
    async_planning: bool = False
    allow_validated_start_anchor: bool = False
    progress_timeout_s: float = 8.0
    progress_min_distance_m: float = 0.05
    obstacle_block_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        if (not math.isfinite(self.map_update_hz) or self.map_update_hz <= 0
                or not math.isfinite(self.planner_hz) or self.planner_hz <= 0
                or not math.isfinite(self.initial_scan_yaw_rad) or self.initial_scan_yaw_rad < 0
                or not math.isfinite(self.initial_scan_rate_radps) or self.initial_scan_rate_radps <= 0
                or not math.isfinite(self.collision_recovery_yaw_rad) or self.collision_recovery_yaw_rad <= 0
                or not math.isfinite(self.progress_timeout_s) or self.progress_timeout_s <= 0
                or not math.isfinite(self.progress_min_distance_m) or self.progress_min_distance_m <= 0
                or not math.isfinite(self.obstacle_block_timeout_s) or self.obstacle_block_timeout_s <= 0
                or type(self.async_mapping) is not bool
                or type(self.async_planning) is not bool
                or type(self.allow_validated_start_anchor) is not bool):
            raise ValueError("map and planner rates must be finite and positive")


@dataclass(frozen=True)
class NavigationTick:
    command: ActionCommand
    snapshot: GridSnapshot
    depth_safety: DepthSafetyResult
    frontiers: FrontierSet | None
    decision: NavigationDecision | None
    map_updated: bool
    planner_ran: bool
    map_duration_s: float
    safety_duration_s: float
    planning_duration_s: float
    control_duration_s: float
    total_duration_s: float
    input_age_s: float
    planner_worker_duration_s: float
    planner_pending: bool
    planner_result_stale: bool
    planner_result_rebased: bool
    planner_result_revision: int | None
    planner_result_frame_id: str | None
    planner_worker_error: str | None
    path_revalidation_failure_reason: str | None
    map_worker_duration_s: float
    map_worker_cpu_duration_s: float
    map_worker_queue_wait_s: float
    map_worker_submit_to_publish_s: float
    map_update_profile_s: dict[str, float]
    map_worker_pending: bool
    map_dropped_frames: int
    map_worker_error: str | None
    blocked_step_edge: tuple[tuple[int, int], tuple[int, int]] | None
    active_path_next_edge: tuple[tuple[int, int], tuple[int, int]] | None
    blocked_step_matches_path: bool | None
    active_blocked_edge_count: int


class NavigationRuntime:
    """Run fast local safety/control around slower map and planner updates.

    Caller invokes ``tick`` once per latest camera frame and executes the
    returned command through the collision-filtered Habitat adapter. This
    class does not own simulator stepping or episode/process shutdown.
    """

    def __init__(self, *, mapper: NavMapper, frontier_extractor: FrontierExtractor,
                 planner: AStarPlanner, policy: FrontierPolicy,
                 controller: Controller, depth_safety: DepthSafetyGate,
                 config: NavigationRuntimeConfig = NavigationRuntimeConfig()) -> None:
        self.mapper = mapper
        self.frontier_extractor = frontier_extractor
        self.planner = planner
        self.policy = policy
        self.controller = controller
        self.depth_safety = depth_safety
        self.config = config
        if policy.planner is not planner:
            raise ValueError("runtime and frontier policy must share the same planner instance")
        if (not math.isclose(mapper.agent_radius_m, planner.config.agent_radius_m, abs_tol=1e-6)
                or not math.isclose(mapper.agent_radius_m, frontier_extractor.agent_radius_m, abs_tol=1e-6)
                or not math.isclose(mapper.config.agent_height_m, depth_safety.config.agent_height_m, abs_tol=1e-6)):
            raise ValueError("NavMap, frontier, planner, and local depth gate must share geometry contract")
        self._spawn_initialized = False
        self._last_map_update_s: float | None = None
        self._last_plan_s: float | None = None
        self._last_plan_request_s: float | None = None
        self._last_planned_revision = -1
        self._path: PathResult | None = None
        self._frontiers: FrontierSet | None = None
        self._decision: NavigationDecision | None = None
        self._scan_last_yaw: float | None = None
        self._scan_progress_rad = 0.0
        self._scan_complete = self.config.initial_scan_yaw_rad == 0
        self._recovery_last_yaw: float | None = None
        self._recovery_progress_rad = 0.0
        self._recovery_sign = 1.0
        self._last_command_yaw_rate = 0.0
        self._progress_anchor_xy: tuple[float, float] | None = None
        self._progress_commanded_s = 0.0
        self._last_motion_requested = False
        self._front_brake_started_s: float | None = None
        self._rejected_goal_targets: list[tuple[tuple[float, float], float]] = []
        self._map_worker: LatestMapWorker | None = None
        self._last_map_revision = -1
        self._plan_worker = (LatestPlanWorker(frontier_extractor, policy)
                             if config.async_planning else None)
        # The main thread may revalidate the active path while the worker plans
        # against another immutable snapshot. Keep the A* cache instances apart.
        self._path_revalidator = (AStarPlanner(planner.config)
                                 if config.async_planning else planner)
        self.controller.set_traversable(self._path_revalidator.traversable)

    def _reject_goal(self, snapshot: GridSnapshot, now: float, *,
                     blocked_edge=None) -> str | None:
        frontier = None if self._decision is None else self._decision.frontier
        target = None if frontier is None else frontier.viewpoint_world_xy
        if target is None and frontier is not None:
            target = snapshot.cell_to_world(*frontier.viewpoint_cell)
        frontier_id = "" if frontier is None else frontier.frontier_id
        reporter = self.policy if self._plan_worker is None else self._plan_worker
        reporter.report_goal_failure(frontier_id, snapshot.revision,
                                     blocked_edge=blocked_edge, goal_world_xy=target)
        if target is not None:
            # Reject already-in-flight plans too; the planner worker drains
            # failure messages only before its next request.
            self._rejected_goal_targets.append((target, now + 20.0))
            self._rejected_goal_targets = self._rejected_goal_targets[-64:]
        self._path = None
        self._decision = None
        self._last_plan_s = self._last_plan_request_s = None
        self._progress_commanded_s = 0.0
        self._last_motion_requested = False
        self.controller.reset()
        return frontier_id or None

    def close(self, timeout_s: float = 5.0, *,
              deadline_monotonic_s: float | None = None) -> bool:
        """Drain optional workers; return whether all workers stopped in time."""
        deadline = (time.monotonic() + max(0.0, float(timeout_s))
                    if deadline_monotonic_s is None else float(deadline_monotonic_s))

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        map_stopped = (True if self._map_worker is None
                       else self._map_worker.close(remaining()))
        plan_stopped = (True if self._plan_worker is None
                        else self._plan_worker.close(remaining()))
        return map_stopped and plan_stopped

    def map_snapshot(self) -> GridSnapshot:
        """Return the latest completely integrated immutable map generation."""
        if self._map_worker is not None:
            return self._map_worker.snapshot()
        return self.mapper.snapshot()

    def worker_status(self) -> dict[str, object]:
        return {
            "map": None if self._map_worker is None else self._map_worker.stats(),
            "planner_pending": False if self._plan_worker is None else self._plan_worker.busy,
        }

    def _adopt_worker_outcome(self, outcome, snapshot: GridSnapshot,
                              robot_xy: tuple[float, float], now: float,
                              rejected_frontier_id: str | None = None) -> tuple[bool, bool]:
        """Rebase a recent worker goal onto the current map and robot pose."""
        source_age = now - outcome.map_host_monotonic_s
        compatible = (
            outcome.error is None
            and bool(outcome.map_frame_id)
            and 0 <= source_age <= self.controller.config.max_nav_age_s
            and outcome.map_revision <= snapshot.revision
            and outcome.resolution_m == snapshot.resolution_m
            and outcome.world_frame == snapshot.world_frame
            and outcome.scope_id == snapshot.scope_id
            and outcome.decision is not None
        )
        if not compatible:
            return False, False

        decision = outcome.decision
        assert decision is not None
        self._rejected_goal_targets = [(xy, until) for xy, until in self._rejected_goal_targets
                                       if now < until]
        if (decision.frontier is not None and decision.frontier.viewpoint_world_xy is not None
                and any(math.dist(decision.frontier.viewpoint_world_xy, xy)
                        < max(0.001, self.policy.config.failed_viewpoint_radius_m)
                        for xy, _until in self._rejected_goal_targets)):
            return False, False
        if (rejected_frontier_id is not None and decision.frontier is not None
                and decision.frontier.frontier_id == rejected_frontier_id):
            return False, False
        if decision.reason == "frontier_viewpoint_reached":
            target = None if decision.frontier is None else decision.frontier.viewpoint_world_xy
            if target is None or math.dist(robot_xy, target) > self.policy.config.reached_distance_m:
                return False, False
        if decision.state != "MOVE" and outcome.map_revision != snapshot.revision:
            # An arrival remains true across map revisions when the robot is
            # still at that exact world target. Other old observation-only
            # results can hide newly opened frontiers and must be discarded.
            target = None if decision.frontier is None else decision.frontier.viewpoint_world_xy
            if (decision.reason != "frontier_viewpoint_reached" or target is None
                    or math.dist(robot_xy, target) > self.policy.config.reached_distance_m):
                return False, False
        path = decision.path
        if decision.state == "MOVE" and decision.frontier is not None and path is not None and path.success:
            target_xy = decision.frontier.viewpoint_world_xy
            if target_xy is None:
                return False, False
            goal_cell = snapshot.world_to_cell(*target_xy)
            if goal_cell is None:
                return False, False
            # The worker selected the target on its source snapshot. Replan
            # from the current pose on today's map before the controller sees it.
            path = self._path_revalidator.plan(
                snapshot, robot_xy, goal_cell,
                allow_validated_start_anchor=self.config.allow_validated_start_anchor,
                blocked_edges=self.policy.blocked_edges(snapshot.revision),
            )
            if not path.success:
                return False, False
            decision = replace(decision, path=path)
        elif path is not None and path.success:
            # A path without a semantic frontier target cannot be safely rebased.
            return False, False

        previous_target = (None if self._decision is None or self._decision.frontier is None
                           else self._decision.frontier.viewpoint_world_xy)
        next_target = None if decision.frontier is None else decision.frontier.viewpoint_world_xy
        if next_target != previous_target:
            self._progress_anchor_xy = robot_xy
            self._progress_commanded_s = 0.0
            self._last_motion_requested = False
        self._frontiers = outcome.frontiers
        self._decision = decision
        self._path = decision.path if decision.state == "MOVE" else None
        self._last_plan_s = now
        if decision.reason == "frontier_viewpoint_reached":
            self._last_plan_s = self._last_plan_request_s = None
        self._last_planned_revision = snapshot.revision
        return True, outcome.map_revision != snapshot.revision

    def tick(self, packet: FramePacket, robot_pose: RobotPose2, *, dt_s: float,
             now_monotonic_s: float | None = None, collided: bool = False,
             collision_position_world_xy: tuple[float, float] | None = None,
             collision_filter_details: dict[str, object] | None = None) -> NavigationTick:
        tick_started = time.perf_counter()
        now = time.monotonic() if now_monotonic_s is None else float(now_monotonic_s)
        if not math.isclose(packet.floor_z_m, self.depth_safety.config.floor_z_m, abs_tol=0.05):
            raise ValueError("frame floor scope differs from local depth safety configuration")
        if not self._spawn_initialized:
            self.mapper.initialize_spawn(robot_pose.x_m, robot_pose.y_m)
            self._spawn_initialized = True
            if self.config.async_mapping:
                self._map_worker = LatestMapWorker(self.mapper, self.mapper.snapshot())
        map_period = 1.0 / self.config.map_update_hz
        map_updated = self._last_map_update_s is None or now - self._last_map_update_s >= map_period
        map_started = time.perf_counter()
        map_worker_duration = 0.0
        map_worker_cpu_duration = 0.0
        map_worker_queue_wait = 0.0
        map_worker_submit_to_publish = 0.0
        map_update_profile: dict[str, float] = {}
        map_worker_error = None
        map_dropped_frames = 0
        map_worker_pending = False
        if self._map_worker is None and map_updated:
            snapshot = self.mapper.update(packet)
            map_update_profile = dict(self.mapper.last_update_profile_s)
            self._last_map_update_s = now
        elif self._map_worker is None:
            snapshot = self.mapper.snapshot()
        else:
            if map_updated and self._map_worker.submit(packet):
                self._last_map_update_s = now
            snapshot = self._map_worker.snapshot()
            map_updated = (snapshot.as_of_frame_id != "uninitialized"
                           and snapshot.revision != self._last_map_revision)
            stats = self._map_worker.stats()
            if map_updated:
                map_worker_duration = float(stats["last_duration_s"])
                map_worker_cpu_duration = float(stats["last_cpu_duration_s"])
                map_worker_queue_wait = float(stats["last_queue_wait_s"])
                map_worker_submit_to_publish = float(stats["last_submit_to_publish_s"])
                map_update_profile = dict(stats["last_update_profile_s"])
            map_worker_error = stats["last_error"]
            map_dropped_frames = int(stats["dropped_pending"])
            map_worker_pending = bool(stats["pending"] or stats["running"])
        self._last_map_revision = snapshot.revision
        map_duration = time.perf_counter() - map_started

        safety_started = time.perf_counter()
        safety = self.depth_safety.evaluate(
            frame_id=packet.frame_id,
            host_monotonic_s=packet.host_monotonic_s,
            depth_m=packet.depth_m,
            intrinsics=packet.intrinsics,
            T_world_camera=packet.T_world_camera,
            robot_xy=(robot_pose.x_m, robot_pose.y_m),
            robot_yaw_rad=robot_pose.yaw_rad,
            depth_min_m=packet.depth_min_m,
            depth_max_m=packet.depth_max_m,
        )
        safety_duration = time.perf_counter() - safety_started

        if not self._scan_complete:
            if self._scan_last_yaw is not None:
                self._scan_progress_rad += _wrap_angle(robot_pose.yaw_rad - self._scan_last_yaw)
            self._scan_last_yaw = robot_pose.yaw_rad
            self._scan_complete = self._scan_progress_rad >= self.config.initial_scan_yaw_rad
        scan_active = not self._scan_complete

        failed_frontier_id: str | None = None
        progress_timed_out = False
        robot_xy = (robot_pose.x_m, robot_pose.y_m)
        if (self._progress_anchor_xy is None
                or math.dist(robot_xy, self._progress_anchor_xy) >= self.config.progress_min_distance_m):
            self._progress_anchor_xy = robot_xy
            self._progress_commanded_s = 0.0
        elif self._last_motion_requested and not collided:
            # Count translation requests, excluding waits and alignment.
            self._progress_commanded_s += min(max(dt_s, 0.0), 0.30)
            if self._progress_commanded_s >= self.config.progress_timeout_s:
                failed_frontier_id = self._reject_goal(snapshot, now)
                progress_timed_out = True
        blocked_step_edge = None
        active_path_next_edge = None
        blocked_step_matches_path = None
        if collided and self._scan_complete:
            raw_filter_reasons = (None if collision_filter_details is None else
                                  collision_filter_details.get("reason"))
            filter_reasons = ((raw_filter_reasons,) if isinstance(raw_filter_reasons, str) else
                              raw_filter_reasons if isinstance(raw_filter_reasons, (list, tuple, set))
                              else ())
            if (collision_position_world_xy is not None
                    and "filtered_step_displacement_invalid" in filter_reasons):
                blocked_step_edge = _blocked_step_edge(
                    snapshot, (robot_pose.x_m, robot_pose.y_m),
                    collision_position_world_xy,
                    allow_diagonal=self.planner.config.allow_diagonal,
                )
                active_path_next_edge = _next_path_edge(
                    snapshot, self._path,
                    (robot_pose.x_m, robot_pose.y_m),
                )
                blocked_step_matches_path = (
                    None if blocked_step_edge is None or active_path_next_edge is None
                    else blocked_step_edge == active_path_next_edge
                )
            failed_frontier_id = self._reject_goal(snapshot, now, blocked_edge=blocked_step_edge)
            if self._recovery_last_yaw is None:
                self._recovery_last_yaw = robot_pose.yaw_rad
                self._recovery_progress_rad = 0.0
                if abs(self._last_command_yaw_rate) > 1e-6:
                    self._recovery_sign = -math.copysign(1.0, self._last_command_yaw_rate)
                else:
                    self._recovery_sign *= -1.0
            self.controller.reset()

        planning_started = time.perf_counter()
        planner_due = False
        planner_worker_duration = 0.0
        planner_result_stale = False
        planner_result_rebased = False
        planner_result_revision = None
        planner_result_frame_id = None
        planner_worker_error = None
        path_revalidation_failure_reason = None
        if not scan_active:
            if self._path is not None and self._path.map_revision != snapshot.revision:
                self._path = self._path_revalidator.revalidate(
                    snapshot, self._path,
                    current_xy=(robot_pose.x_m, robot_pose.y_m),
                    allow_validated_start_anchor=self.config.allow_validated_start_anchor,
                    blocked_edges=self.policy.blocked_edges(snapshot.revision),
                )
                if not self._path.success:
                    path_revalidation_failure_reason = self._path.reason
                    self._path = None
                if self._decision is not None:
                    self._decision = replace(self._decision, path=self._path)

            planner_period = 1.0 / self.config.planner_hz
            if self._plan_worker is None:
                planner_due = (self._decision is None or self._last_plan_s is None
                               or now - self._last_plan_s >= planner_period
                               or (self._path is None and self._decision.state == "MOVE"))
            else:
                # Throttle pending-slot refreshes to planner_hz even while no
                # result has been accepted. Without this separate request
                # clock, an unset decision/old last-plan time makes every
                # control tick replace the pending plan and overload the worker.
                last_planner_activity = max(
                    (value for value in (self._last_plan_s, self._last_plan_request_s)
                     if value is not None), default=None,
                )
                planner_due = (last_planner_activity is None
                               or now - last_planner_activity >= planner_period)
            if self._plan_worker is None and planner_due:
                viewpoint_options = {}
                if getattr(self.frontier_extractor, "max_viewpoints_per_frontier", 1) > 1:
                    viewpoint_options = {
                        "excluded_viewpoints": self.policy.visited_viewpoints(),
                        "excluded_viewpoint_radius_m": self.policy.config.visited_viewpoint_radius_m,
                    }
                self._frontiers = self.frontier_extractor.extract(
                    snapshot, (robot_pose.x_m, robot_pose.y_m),
                    allow_validated_start_anchor=self.config.allow_validated_start_anchor,
                    **viewpoint_options)
                self._decision = self.policy.select(
                    snapshot=snapshot, frontiers=self._frontiers,
                    robot_xy=(robot_pose.x_m, robot_pose.y_m), now_monotonic_s=now,
                    allow_validated_start_anchor=self.config.allow_validated_start_anchor,
                )
                self._path = self._decision.path
                self._last_plan_s = now
                self._last_planned_revision = snapshot.revision
            elif self._plan_worker is not None:
                outcome = self._plan_worker.poll()
                accepted_result = False
                if outcome is not None:
                    planner_worker_duration = outcome.duration_s
                    planner_result_revision = outcome.map_revision
                    planner_result_frame_id = outcome.map_frame_id
                    planner_worker_error = outcome.error
                    accepted_result, planner_result_rebased = self._adopt_worker_outcome(
                        outcome, snapshot, (robot_pose.x_m, robot_pose.y_m), now,
                        rejected_frontier_id=failed_frontier_id,
                    )
                    planner_result_stale = not accepted_result
                if planner_due and not accepted_result:
                    # Keep the worker's single pending slot current at the
                    # configured planner rate while an older request runs.
                    submitted = self._plan_worker.submit(
                        snapshot, (robot_pose.x_m, robot_pose.y_m), now,
                        allow_validated_start_anchor=self.config.allow_validated_start_anchor,
                    )
                    if submitted:
                        self._last_plan_request_s = now
        planning_duration = time.perf_counter() - planning_started
        control_started = time.perf_counter()
        # Age evidence again immediately before command creation. In async
        # mode this uses the current packet/map, never the planner's source age.
        decision_now = now + (time.perf_counter() - tick_started)
        depth_age = decision_now - safety.host_monotonic_s
        nav_age = decision_now - snapshot.host_monotonic_s
        evidence_deadline = min(
            safety.host_monotonic_s + self.controller.config.max_depth_age_s,
            snapshot.host_monotonic_s + self.controller.config.max_nav_age_s,
        )
        stale_reason = ("depth_stale" if depth_age < 0
                        or depth_age > self.controller.config.max_depth_age_s else
                        "nav_stale" if nav_age < 0
                        or nav_age > self.controller.config.max_nav_age_s else None)
        recovery_active = self._recovery_last_yaw is not None
        if recovery_active:
            assert self._recovery_last_yaw is not None
            delta = _wrap_angle(robot_pose.yaw_rad - self._recovery_last_yaw)
            self._recovery_progress_rad += delta * self._recovery_sign
            self._recovery_last_yaw = robot_pose.yaw_rad
            if self._recovery_progress_rad >= self.config.collision_recovery_yaw_rad:
                self._recovery_last_yaw = None
                recovery_active = False

        display_decision = self._decision
        if stale_reason is not None:
            self.controller.reset()
            command = ActionCommand(
                linear_mps=0.0, yaw_rate_radps=0.0, reason=stale_reason,
                frame_id=packet.frame_id, map_revision=snapshot.revision,
                valid_until_monotonic_s=min(decision_now, evidence_deadline),
                metadata={"depth_age_s": depth_age, "nav_age_s": nav_age},
            )
            display_decision = NavigationDecision("WAIT", None, None, stale_reason, None)
        elif scan_active or recovery_active:
            label = "initial_scan" if scan_active else "collision_recovery"
            rate = min(self.config.initial_scan_rate_radps,
                       self.controller.config.max_yaw_rate_radps)
            yaw_sign = 1.0 if scan_active else self._recovery_sign
            command = ActionCommand(
                linear_mps=0.0, yaw_rate_radps=yaw_sign * rate, reason=label,
                frame_id=packet.frame_id, map_revision=snapshot.revision,
                valid_until_monotonic_s=evidence_deadline,
                metadata={"scan_progress_rad": self._scan_progress_rad,
                          "recovery_progress_rad": self._recovery_progress_rad},
            )
            display_decision = NavigationDecision(
                "SCAN" if scan_active else "RECOVERY", None, None, label, None)
        else:
            command = self.controller.tick(
            robot_xy=(robot_pose.x_m, robot_pose.y_m), yaw_rad=robot_pose.yaw_rad,
            now_monotonic_s=decision_now, dt_s=dt_s,
            depth_host_monotonic_s=safety.host_monotonic_s,
            depth_front_min_m=safety.minimum_obstacle_distance_m,
            depth_coverage_ok=safety.status == "observed",
            snapshot=snapshot, path=self._path, frame_id=packet.frame_id,
            blocked_edges=self.policy.blocked_edges(snapshot.revision),
            )
            if command.reason == "goal_reached" and self._decision is not None:
                frontier = self._decision.frontier
                if frontier is not None and self._path is not None:
                    target = snapshot.cell_to_world(*self._path.cells[-1])
                    reporter = self.policy if self._plan_worker is None else self._plan_worker
                    reporter.report_goal_reached(frontier.frontier_id, target)
                self._decision = replace(self._decision, state="OBSERVE", path=None,
                                         reason="frontier_viewpoint_reached")
                display_decision = self._decision
                self._path = None
                self._last_plan_s = self._last_plan_request_s = None
                self._progress_commanded_s = 0.0
            elif command.reason == "path_segment_blocked":
                self._reject_goal(snapshot, now)
                display_decision = NavigationDecision("WAIT", None, None, "local_path_blocked", None)
        if progress_timed_out:
            display_decision = NavigationDecision("WAIT", None, None, "goal_progress_timeout", None)
        elif (command.reason == "no_valid_path" and display_decision is not None
              and display_decision.state == "MOVE"):
            display_decision = replace(display_decision, state="WAIT",
                                       reason="path_unavailable_waiting_for_replan")
        self._last_motion_requested = (command.linear_mps > 0.01 and command.reason == "tracking")
        if command.reason == "front_obstacle_brake":
            if self._front_brake_started_s is None:
                self._front_brake_started_s = now
            elif now - self._front_brake_started_s >= self.config.obstacle_block_timeout_s:
                # Decide from this tick's controller output, after evaluating
                # the CURRENT route. An old brake timer must not cancel a new
                # goal that requires safe in-place heading alignment.
                self._reject_goal(snapshot, now)
                self._recovery_last_yaw = robot_pose.yaw_rad
                self._recovery_progress_rad = 0.0
                rate = min(self.config.initial_scan_rate_radps,
                           self.controller.config.max_yaw_rate_radps)
                command = replace(command, linear_mps=0.0,
                                  yaw_rate_radps=self._recovery_sign * rate,
                                  reason="collision_recovery")
                display_decision = NavigationDecision("RECOVERY", None, None,
                                                       "collision_recovery", None)
                self._front_brake_started_s = None
        else:
            self._front_brake_started_s = None
        # One bounded action per fresh observation remains the execution limit.
        # Integrate velocity in host time even if CPU rendering misses 25 Hz.
        command = replace(command, metadata={**command.metadata,
                          "execution_dt_s": min(max(float(dt_s), 0.0), 0.10)})
        control_duration = time.perf_counter() - control_started
        self._last_command_yaw_rate = command.yaw_rate_radps
        return NavigationTick(command, snapshot, safety, self._frontiers,
                              display_decision, map_updated, planner_due,
                              map_duration, safety_duration, planning_duration,
                              control_duration, time.perf_counter() - tick_started,
                              decision_now - packet.host_monotonic_s,
                              planner_worker_duration,
                              False if self._plan_worker is None else self._plan_worker.busy,
                              planner_result_stale, planner_result_rebased,
                              planner_result_revision, planner_result_frame_id,
                              planner_worker_error, path_revalidation_failure_reason,
                              map_worker_duration,
                              map_worker_cpu_duration, map_worker_queue_wait,
                              map_worker_submit_to_publish, map_update_profile,
                              map_worker_pending, map_dropped_frames,
                              map_worker_error, blocked_step_edge,
                              active_path_next_edge, blocked_step_matches_path,
                              len(self.policy.blocked_edges(snapshot.revision)))


def _wrap_angle(angle: float) -> float:
    return (angle + math.pi) % math.tau - math.pi


def _blocked_step_edge(snapshot: GridSnapshot, robot_xy: tuple[float, float],
                       blocked_xy: tuple[float, float], *, allow_diagonal: bool
                       ) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Quantize a rejected sub-cell step to a temporary planner edge overlay."""
    start = snapshot.world_to_cell(*robot_xy)
    target = snapshot.world_to_cell(*blocked_xy)
    if start is None:
        return None
    if target is not None:
        delta = target[0] - start[0], target[1] - start[1]
        if max(abs(delta[0]), abs(delta[1])) == 1 and (allow_diagonal or not all(delta)):
            return _edge_key(start, target)
    dx, dy = float(blocked_xy[0]) - robot_xy[0], float(blocked_xy[1]) - robot_xy[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    direction = dx / length, dy / length
    neighbors = ((-1, 0), (1, 0), (0, -1), (0, 1))
    if allow_diagonal:
        neighbors += ((-1, -1), (-1, 1), (1, -1), (1, 1))
    candidates = []
    for dr, dc in neighbors:
        cell = start[0] + dr, start[1] + dc
        if not (0 <= cell[0] < snapshot.states.shape[0]
                and 0 <= cell[1] < snapshot.states.shape[1]):
            continue
        world = snapshot.cell_to_world(*cell)
        vx, vy = world[0] - robot_xy[0], world[1] - robot_xy[1]
        norm = math.hypot(vx, vy)
        if norm > 1e-9:
            candidates.append((direction[0] * vx / norm + direction[1] * vy / norm,
                               cell))
    if not candidates:
        return None
    alignment, neighbor = max(candidates)
    if alignment < math.cos(math.radians(45.0)):
        return None
    return _edge_key(start, neighbor)


def _next_path_edge(snapshot: GridSnapshot, path: PathResult | None,
                    robot_xy: tuple[float, float]
                    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Return the next path edge after remapping the route into this snapshot."""
    if (path is None or not path.success or len(path.cells) < 2
            or path.grid_origin_x_m is None or path.grid_origin_y_m is None
            or path.grid_resolution_m is None
            or not math.isclose(path.grid_resolution_m, snapshot.resolution_m,
                                rel_tol=0.0, abs_tol=1e-9)):
        return None
    remapped = []
    for row, col in path.cells:
        world_x = path.grid_origin_x_m + (col + 0.5) * path.grid_resolution_m
        world_y = path.grid_origin_y_m + (row + 0.5) * path.grid_resolution_m
        cell = snapshot.world_to_cell(world_x, world_y)
        if cell is None:
            return None
        if not remapped or remapped[-1] != cell:
            remapped.append(cell)
    if len(remapped) < 2:
        return None
    nearest_index = min(
        range(len(remapped)),
        key=lambda index: math.dist(snapshot.cell_to_world(*remapped[index]), robot_xy),
    )
    if nearest_index + 1 >= len(remapped):
        return None
    return _edge_key(remapped[nearest_index], remapped[nearest_index + 1])


def _edge_key(a: tuple[int, int], b: tuple[int, int]):
    return (a, b) if a <= b else (b, a)
