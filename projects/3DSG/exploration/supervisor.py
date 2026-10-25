"""Episode lifecycle clock; warmup is separate from the exploration budget."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Callable


class EpisodeState(str, Enum):
    WARMUP = "WARMUP"
    READY = "READY"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class EpisodeSnapshot:
    state: EpisodeState
    reason: str | None
    elapsed_exploration_s: float
    remaining_exploration_s: float
    flush_remaining_s: float | None
    motion_allowed: bool


class EpisodeSupervisor:
    """Bound exploration and shutdown time with monotonic deadlines.

    This class owns the episode clock and lifecycle state only. A process
    manager must still call ``request_stop`` on signal/crash and enforce actual
    worker cleanup; those adapters are separate runtime components.
    """

    def __init__(self, *, exploration_budget_s: float = 600.0,
                 shutdown_timeout_s: float = 30.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if (not math.isfinite(exploration_budget_s) or exploration_budget_s <= 0
                or not math.isfinite(shutdown_timeout_s) or shutdown_timeout_s <= 0):
            raise ValueError("episode and shutdown budgets must be finite and positive")
        self.exploration_budget_s = float(exploration_budget_s)
        self.shutdown_timeout_s = float(shutdown_timeout_s)
        self._clock = monotonic
        self._state = EpisodeState.WARMUP
        self._reason: str | None = None
        self._started_at: float | None = None
        self._deadline: float | None = None
        self._stopping_at: float | None = None
        self._flush_deadline: float | None = None

    def mark_ready(self) -> None:
        if self._state is not EpisodeState.WARMUP:
            raise RuntimeError(f"cannot enter READY from {self._state.value}")
        self._state = EpisodeState.READY

    def start(self) -> None:
        if self._state is not EpisodeState.READY:
            raise RuntimeError(f"cannot start episode from {self._state.value}")
        now = self._clock()
        if not math.isfinite(now):
            raise RuntimeError("monotonic clock returned a non-finite value")
        self._started_at = now
        self._deadline = now + self.exploration_budget_s
        self._state = EpisodeState.RUNNING

    def request_stop(self, reason: str) -> None:
        if not reason:
            raise ValueError("stop reason is required")
        if self._state in (EpisodeState.STOPPING, EpisodeState.FINISHED, EpisodeState.FAILED):
            return
        now = self._clock()
        if self._state is EpisodeState.RUNNING:
            self.check_deadline(now)
            if self._state is EpisodeState.STOPPING:
                return
        self._reason = reason
        self._begin_stopping(now)

    def check_deadline(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else float(now)
        if self._state is EpisodeState.RUNNING and self._deadline is not None and now >= self._deadline:
            self._reason = "budget_exhausted"
            self._begin_stopping(now)
            return True
        if self._state is EpisodeState.STOPPING and self._flush_deadline is not None and now >= self._flush_deadline:
            self._reason = f"{self._reason or 'stopping'};flush_timeout"
            self._state = EpisodeState.FAILED
            return True
        return False

    def finalize(self, *, success: bool, reason: str | None = None) -> None:
        if self._state is not EpisodeState.STOPPING:
            raise RuntimeError(f"cannot finalize from {self._state.value}")
        if self._flush_deadline is not None and self._clock() >= self._flush_deadline:
            self.check_deadline()
            return
        if reason:
            self._reason = reason
        elif self._reason is None:
            self._reason = "completed" if success else "finalization_failed"
        self._state = EpisodeState.FINISHED if success else EpisodeState.FAILED

    def snapshot(self, now: float | None = None) -> EpisodeSnapshot:
        now = self._clock() if now is None else float(now)
        elapsed = 0.0 if self._started_at is None else max(0.0, now - self._started_at)
        remaining = self.exploration_budget_s if self._started_at is None else max(
            0.0, (self._deadline or now) - now)
        flush_remaining = None if self._flush_deadline is None else max(0.0, self._flush_deadline - now)
        motion_allowed = (self._state is EpisodeState.RUNNING and self._deadline is not None and now < self._deadline)
        return EpisodeSnapshot(self._state, self._reason, elapsed, remaining, flush_remaining, motion_allowed)

    def _begin_stopping(self, now: float) -> None:
        self._state = EpisodeState.STOPPING
        self._stopping_at = now
        self._flush_deadline = now + self.shutdown_timeout_s
