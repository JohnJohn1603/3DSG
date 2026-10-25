"""Bounded chronological admission for persistent graph keyframes.

Persistence/validation happens before ``offer``. This makes an accepted
decision an admission ACK for durable evidence, while the scheduler retains
only small references and enforces in-flight, pending, and raw-byte budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROTECTED_REASONS = frozenset({
    "first_view", "doorway", "new_region", "frontier_viewpoint",
})


@dataclass(frozen=True)
class PersistedKeyframe:
    run_id: str
    scene_id: str
    frame_id: str
    frame_index: int
    host_monotonic_s: float
    payload_ref: str
    payload_bytes: int
    payload_sha256: str
    novelty: float
    protected_reason: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not all((self.run_id, self.scene_id, self.frame_id, self.payload_ref)):
            raise ValueError("run, scene, frame and persisted payload reference are required")
        if self.frame_index < 0 or self.payload_bytes <= 0:
            raise ValueError("frame index and persisted byte count are invalid")
        if not math.isfinite(self.host_monotonic_s) or not math.isfinite(self.novelty) or self.novelty < 0:
            raise ValueError("timestamp and novelty must be finite; novelty must be non-negative")
        if not _SHA256.fullmatch(self.payload_sha256):
            raise ValueError("payload_sha256 must be 64 lowercase hexadecimal characters")
        if self.protected_reason is not None and self.protected_reason not in _PROTECTED_REASONS:
            raise ValueError(f"unsupported protected keyframe reason: {self.protected_reason}")

    @property
    def protected(self) -> bool:
        return self.protected_reason is not None


@dataclass(frozen=True)
class AdmissionDecision:
    accepted: bool
    frame_id: str
    frame_index: int
    reason: str
    replaced_frame_ids: tuple[str, ...] = ()
    queue_depth: int = 0
    queued_bytes: int = 0


@dataclass(frozen=True)
class WorkerLease:
    keyframe: PersistedKeyframe
    dispatch_sequence: int


class KeyframeScheduler:
    """One in-flight + bounded pending, with strictly increasing dispatch."""

    def __init__(self, *, max_pending: int = 2, max_protected_pending: int = 1,
                 raw_pending_byte_cap: int = 32 * 1024 * 1024) -> None:
        if max_pending < 1 or max_pending > 2 or max_protected_pending < 0 or max_protected_pending > 1:
            raise ValueError("invalid keyframe queue limits")
        if raw_pending_byte_cap < 1:
            raise ValueError("raw_pending_byte_cap must be positive")
        self.max_pending = max_pending
        self.max_protected_pending = max_protected_pending
        self.raw_pending_byte_cap = raw_pending_byte_cap
        self._pending: list[PersistedKeyframe] = []
        self._inflight: WorkerLease | None = None
        self._last_dispatched_index = -1
        self._dispatch_sequence = 0
        self._terminal_error: str | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._counts = {"offered": 0, "admitted": 0, "rejected": 0, "replaced": 0,
                        "processed": 0, "worker_failed": 0}

    def offer(self, item: PersistedKeyframe) -> AdmissionDecision:
        """Admit a durable keyframe reference or reject it with an explicit reason."""
        with self._lock:
            self._counts["offered"] += 1
            if self._closed:
                return self._reject(item, "scheduler_closed")
            if self._terminal_error is not None:
                return self._reject(item, "worker_failed_replay_required")
            if item.frame_index <= self._last_dispatched_index:
                return self._reject(item, "older_than_dispatch_watermark")
            if any(existing.frame_id == item.frame_id for existing in self._pending):
                return self._reject(item, "duplicate_pending_frame")
            if any(existing.frame_index == item.frame_index for existing in self._pending):
                return self._reject(item, "duplicate_pending_frame_index")
            if self._inflight and self._inflight.keyframe.frame_id == item.frame_id:
                return self._reject(item, "duplicate_inflight_frame")
            if self._inflight and self._inflight.keyframe.frame_index == item.frame_index:
                return self._reject(item, "duplicate_inflight_frame_index")

            pending = list(self._pending)
            replaced: list[PersistedKeyframe] = []
            same_class = [p for p in pending if p.protected == item.protected]
            slot_cap = self.max_protected_pending if item.protected else self.max_pending
            if item.protected and len(same_class) >= slot_cap:
                if slot_cap == 0:
                    return self._reject(item, "protected_pending_disabled")
                victim = min(same_class, key=lambda p: (p.novelty, -p.frame_index))
                if item.novelty <= victim.novelty:
                    return self._reject(item, "protected_pending_has_higher_novelty")
                pending.remove(victim)
                replaced.append(victim)
            elif not item.protected and same_class:
                # The ordinary slot is latest/most novel wins. Replacing it
                # never displaces the single protected observation.
                victim = min(same_class, key=lambda p: (p.novelty, p.frame_index))
                if item.novelty <= victim.novelty:
                    return self._reject(item, "pending_has_higher_novelty")
                pending.remove(victim)
                replaced.append(victim)

            if len(pending) >= self.max_pending:
                ordinary = [p for p in pending if not p.protected]
                if item.protected and ordinary:
                    victim = min(ordinary, key=lambda p: (p.novelty, p.frame_index))
                    pending.remove(victim)
                    replaced.append(victim)
                else:
                    return self._reject(item, "pending_queue_full")

            inflight_bytes = self._inflight.keyframe.payload_bytes if self._inflight else 0
            projected_bytes = inflight_bytes + sum(p.payload_bytes for p in pending) + item.payload_bytes
            while projected_bytes > self.raw_pending_byte_cap:
                ordinary = [p for p in pending if not p.protected]
                if ordinary:
                    victim = min(ordinary, key=lambda p: (p.novelty, p.frame_index))
                    pending.remove(victim)
                    replaced.append(victim)
                    projected_bytes = inflight_bytes + sum(p.payload_bytes for p in pending) + item.payload_bytes
                    continue
                if item.protected and pending:
                    victim = min(pending, key=lambda p: (p.novelty, p.frame_index))
                    if item.novelty > victim.novelty:
                        pending.remove(victim)
                        replaced.append(victim)
                        projected_bytes = inflight_bytes + item.payload_bytes
                        continue
                return self._reject(item, "raw_pending_byte_cap")

            pending.append(item)
            pending.sort(key=lambda p: p.frame_index)
            if len(pending) > self.max_pending:
                return self._reject(item, "pending_queue_full")
            self._pending = pending
            self._counts["admitted"] += 1
            self._counts["replaced"] += len(replaced)
            return AdmissionDecision(True, item.frame_id, item.frame_index,
                                     "admitted_persisted_keyframe",
                                     tuple(p.frame_id for p in replaced),
                                     len(self._pending), self._queued_bytes())

    def take_next(self) -> WorkerLease | None:
        """Claim the oldest pending frame; only one worker lease can exist."""
        with self._lock:
            if self._closed or self._terminal_error is not None or self._inflight is not None:
                return None
            while self._pending:
                item = self._pending.pop(0)
                if item.frame_index <= self._last_dispatched_index:
                    # This should be unreachable through offer(), but discard
                    # defensively rather than allowing a mapper session rewind.
                    self._counts["rejected"] += 1
                    continue
                self._last_dispatched_index = item.frame_index
                self._dispatch_sequence += 1
                self._inflight = WorkerLease(item, self._dispatch_sequence)
                return self._inflight
            return None

    def complete(self, frame_id: str, *, success: bool, reason: str = "") -> None:
        """ACK the sole in-flight job or make the scheduler terminal on failure."""
        with self._lock:
            if self._inflight is None:
                raise RuntimeError("no keyframe is in flight")
            if self._inflight.keyframe.frame_id != frame_id:
                raise RuntimeError("worker ACK frame_id does not match in-flight lease")
            self._inflight = None
            if success:
                self._counts["processed"] += 1
            else:
                self._terminal_error = reason or "worker_failed_after_dispatch"
                self._counts["worker_failed"] += 1

    def close(self, reason: str = "closed") -> tuple[str, ...]:
        """Stop admission/dispatch and report pending frames dropped by shutdown."""
        with self._lock:
            if self._closed:
                return ()
            self._closed = True
            dropped = tuple(item.frame_id for item in self._pending)
            self._pending.clear()
            self._close_reason = reason
            return dropped

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "failed" if self._terminal_error else "closed" if self._closed else "running",
                "failure_reason": self._terminal_error,
                "close_reason": getattr(self, "_close_reason", None),
                "inflight_frame_id": self._inflight.keyframe.frame_id if self._inflight else None,
                "inflight_index": self._inflight.keyframe.frame_index if self._inflight else None,
                "pending": [{"frame_id": item.frame_id, "frame_index": item.frame_index,
                             "protected": item.protected, "novelty": item.novelty,
                             "payload_bytes": item.payload_bytes} for item in self._pending],
                "queue_depth": len(self._pending),
                "queued_bytes": self._queued_bytes(),
                "last_dispatched_index": self._last_dispatched_index,
                "counts": dict(self._counts),
            }

    def _reject(self, item: PersistedKeyframe, reason: str) -> AdmissionDecision:
        self._counts["rejected"] += 1
        return AdmissionDecision(False, item.frame_id, item.frame_index, reason,
                                 (), len(self._pending), self._queued_bytes())

    def _queued_bytes(self) -> int:
        total = sum(item.payload_bytes for item in self._pending)
        if self._inflight:
            total += self._inflight.keyframe.payload_bytes
        return total
