"""Bounded asynchronous NavMap integration with immutable published snapshots."""

from __future__ import annotations

import threading
import time

from .contracts import FramePacket, GridSnapshot
from .nav_mapper import NavMapper


class LatestMapWorker:
    """Integrate RGB-D sequentially without making the caller wait for mapping.

    One packet may be running and one may be pending. If a newer packet arrives
    while the pending slot is occupied, it replaces that slot; the skipped
    frame is counted. The mapper remains single-writer and consumers read only
    fully constructed, immutable GridSnapshot values.
    """

    def __init__(self, mapper: NavMapper, initial_snapshot: GridSnapshot) -> None:
        self._mapper = mapper
        self._condition = threading.Condition()
        self._pending: FramePacket | None = None
        self._snapshot = initial_snapshot
        self._running = False
        self._closed = False
        self._submitted = 0
        self._completed = 0
        self._dropped = 0
        self._last_duration_s = 0.0
        self._last_cpu_duration_s = 0.0
        self._last_queue_wait_s = 0.0
        self._last_submit_to_publish_s = 0.0
        self._last_update_profile_s: dict[str, float] = {}
        self._pending_submitted_monotonic_s: float | None = None
        self._last_error: str | None = None
        self._thread = threading.Thread(target=self._run, name="navmap-worker", daemon=True)
        self._thread.start()

    def submit(self, packet: FramePacket) -> bool:
        with self._condition:
            if self._closed:
                return False
            if self._pending is not None:
                self._dropped += 1
            self._pending = packet
            self._pending_submitted_monotonic_s = time.monotonic()
            self._submitted += 1
            self._condition.notify()
            return True

    def snapshot(self) -> GridSnapshot:
        with self._condition:
            return self._snapshot

    def stats(self) -> dict[str, object]:
        with self._condition:
            return {
                "submitted": self._submitted,
                "completed": self._completed,
                "dropped_pending": self._dropped,
                "pending": self._pending is not None,
                "running": self._running,
                "last_duration_s": self._last_duration_s,
                "last_cpu_duration_s": self._last_cpu_duration_s,
                "last_queue_wait_s": self._last_queue_wait_s,
                "last_submit_to_publish_s": self._last_submit_to_publish_s,
                "last_update_profile_s": dict(self._last_update_profile_s),
                "last_error": self._last_error,
                "snapshot_revision": self._snapshot.revision,
                "snapshot_frame_id": self._snapshot.as_of_frame_id,
            }

    def close(self, timeout_s: float = 5.0) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join(max(0.0, float(timeout_s)))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._pending is None and self._closed:
                    return
                packet = self._pending
                submitted_monotonic_s = self._pending_submitted_monotonic_s
                self._pending = None
                self._pending_submitted_monotonic_s = None
                self._running = True
            assert packet is not None
            started_monotonic_s = time.monotonic()
            started = time.perf_counter()
            cpu_started = time.thread_time()
            error = None
            snapshot = None
            update_profile = {}
            try:
                snapshot = self._mapper.update(packet)
                update_profile = dict(getattr(self._mapper, "last_update_profile_s", {}))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            duration = time.perf_counter() - started
            cpu_duration = max(0.0, time.thread_time() - cpu_started)
            published_monotonic_s = time.monotonic()
            queue_wait = (0.0 if submitted_monotonic_s is None else
                          max(0.0, started_monotonic_s - submitted_monotonic_s))
            submit_to_publish = (duration + queue_wait if submitted_monotonic_s is None else
                                 max(0.0, published_monotonic_s - submitted_monotonic_s))
            with self._condition:
                if snapshot is not None:
                    self._snapshot = snapshot
                    self._completed += 1
                self._last_duration_s = duration
                self._last_cpu_duration_s = cpu_duration
                self._last_queue_wait_s = queue_wait
                self._last_submit_to_publish_s = submit_to_publish
                self._last_update_profile_s = update_profile
                self._last_error = error
                self._running = False
                self._condition.notify_all()
