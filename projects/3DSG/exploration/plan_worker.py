"""Bounded background worker for immutable frontier-planning snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time

from .contracts import GridSnapshot
from .frontier import FrontierExtractor, FrontierSet
from .policy import FrontierPolicy, NavigationDecision


@dataclass(frozen=True)
class PlanOutcome:
    map_revision: int
    map_frame_id: str
    map_host_monotonic_s: float
    resolution_m: float
    world_frame: str
    scope_id: str
    frontiers: FrontierSet | None
    decision: NavigationDecision | None
    duration_s: float
    error: str | None = None


@dataclass(frozen=True)
class _PlanRequest:
    snapshot: GridSnapshot
    robot_xy: tuple[float, float]
    requested_at_s: float
    allow_validated_start_anchor: bool


class LatestPlanWorker:
    """Run at most one plan at a time and retain only the newest queued map.

    GridSnapshot owns read-only array copies, so requests are coherent while the
    main thread continues updating NavMap. Results are tagged with both map
    revision and source frame; the caller decides whether they are still usable.
    """

    def __init__(self, extractor: FrontierExtractor, policy: FrontierPolicy) -> None:
        self._extractor = extractor
        self._policy = policy
        self._condition = threading.Condition()
        self._request: _PlanRequest | None = None
        self._outcome: PlanOutcome | None = None
        self._arrivals: list[tuple[str, tuple[float, float]]] = []
        self._failures: list[
            tuple[str, int, tuple[tuple[int, int], tuple[int, int]] | None,
                  tuple[float, float] | None]
        ] = []
        self._running = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="frontier-plan-worker", daemon=True,
        )
        self._thread.start()

    def submit(self, snapshot: GridSnapshot, robot_xy: tuple[float, float],
               requested_at_s: float, *,
               allow_validated_start_anchor: bool = False) -> bool:
        with self._condition:
            if self._closed:
                return False
            # Replacing a not-yet-started job prevents a backlog of stale maps.
            self._request = _PlanRequest(snapshot, tuple(map(float, robot_xy)),
                                         float(requested_at_s),
                                         bool(allow_validated_start_anchor))
            self._condition.notify()
            return True

    def report_goal_failure(self, frontier_id: str, map_revision: int, *,
                            blocked_edge: tuple[tuple[int, int], tuple[int, int]] | None = None,
                            goal_world_xy: tuple[float, float] | None = None) -> None:
        if not frontier_id and blocked_edge is None and goal_world_xy is None:
            return
        with self._condition:
            if not self._closed:
                self._failures.append((frontier_id, int(map_revision), blocked_edge, goal_world_xy))

    def report_goal_reached(self, frontier_id: str, goal_world_xy: tuple[float, float]) -> None:
        with self._condition:
            if not self._closed:
                self._arrivals.append((frontier_id, goal_world_xy))

    def poll(self) -> PlanOutcome | None:
        with self._condition:
            outcome = self._outcome
            self._outcome = None
            return outcome

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._running or self._request is not None

    def close(self, timeout_s: float = 5.0) -> bool:
        with self._condition:
            self._closed = True
            self._request = None
            self._condition.notify_all()
        self._thread.join(max(0.0, float(timeout_s)))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._request is not None)
                if self._closed:
                    return
                request = self._request
                self._request = None
                failures = self._failures
                self._failures = []
                arrivals = self._arrivals
                self._arrivals = []
                self._running = True
            assert request is not None
            started = time.perf_counter()
            try:
                for frontier_id, goal_world_xy in arrivals:
                    self._policy.report_goal_reached(frontier_id, goal_world_xy)
                for frontier_id, revision, blocked_edge, goal_world_xy in failures:
                    self._policy.report_goal_failure(
                        frontier_id, revision, blocked_edge=blocked_edge,
                        goal_world_xy=goal_world_xy)
                viewpoint_options = {}
                if getattr(self._extractor, "max_viewpoints_per_frontier", 1) > 1:
                    viewpoint_options = {
                        "excluded_viewpoints": self._policy.visited_viewpoints(),
                        "excluded_viewpoint_radius_m": self._policy.config.visited_viewpoint_radius_m,
                    }
                frontiers = self._extractor.extract(
                    request.snapshot, request.robot_xy,
                    allow_validated_start_anchor=request.allow_validated_start_anchor,
                    **viewpoint_options,
                )
                decision = self._policy.select(
                    snapshot=request.snapshot, frontiers=frontiers,
                    robot_xy=request.robot_xy, now_monotonic_s=request.requested_at_s,
                    allow_validated_start_anchor=request.allow_validated_start_anchor,
                )
                outcome = PlanOutcome(
                    request.snapshot.revision, request.snapshot.as_of_frame_id,
                    request.snapshot.host_monotonic_s,
                    request.snapshot.resolution_m, request.snapshot.world_frame,
                    request.snapshot.scope_id, frontiers, decision,
                    time.perf_counter() - started,
                )
            except Exception as exc:  # Surface worker failures to the runtime loop.
                outcome = PlanOutcome(
                    request.snapshot.revision, request.snapshot.as_of_frame_id,
                    request.snapshot.host_monotonic_s,
                    request.snapshot.resolution_m, request.snapshot.world_frame,
                    request.snapshot.scope_id, None, None,
                    time.perf_counter() - started,
                    f"{type(exc).__name__}: {exc}",
                )
            with self._condition:
                if self._outcome is None or outcome.map_revision >= self._outcome.map_revision:
                    self._outcome = outcome
                self._running = False
                self._condition.notify_all()
