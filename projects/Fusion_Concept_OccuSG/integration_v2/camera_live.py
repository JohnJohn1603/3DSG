"""Small current-frame coordinator for camera-live integration.

This module owns orchestration only.  Perception and mapping implementations
are injected as the existing session objects/callbacks; no mapping algorithm
is duplicated here.  A frame is admitted once, sent through one perception
join, and then delivered to both mapper sessions before a unified revision is
published.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class CameraLiveError(RuntimeError):
    """The current-frame coordinator cannot continue safely."""


class CameraLiveReplayRequired(CameraLiveError):
    """A worker may have mutated state before failing; restart from empty state."""


@dataclass(frozen=True)
class CurrentFramePacket:
    """The only input required for one frame.

    ``payload`` is the current frame adapter input.  It must not contain a
    future route manifest or a list of future packets.
    """

    frame_id: str
    frame_index: int
    timestamp_s: float
    camera_timestamp_s: float
    source_hashes: Mapping[str, str]
    payload: Mapping[str, Any]
    camera_monotonic_s: float | None = None
    camera_wall_time_s: float | None = None
    camera_monotonic_domain: str | None = None

    def validate(self) -> None:
        if not self.frame_id or self.frame_index < 0:
            raise ValueError("current frame identity is invalid")
        if self.timestamp_s != self.timestamp_s or self.camera_timestamp_s != self.camera_timestamp_s:
            raise ValueError("current frame timestamp is NaN")
        for name, value in (("camera_monotonic_s", self.camera_monotonic_s),
                            ("camera_wall_time_s", self.camera_wall_time_s)):
            if value is not None and value != value:
                raise ValueError(f"{name} is NaN")
        if self.camera_monotonic_s is not None and not self.camera_monotonic_domain:
            raise ValueError("camera_monotonic_domain is required with camera_monotonic_s")
        if not isinstance(self.source_hashes, Mapping) or not self.source_hashes:
            raise ValueError("current frame requires source hashes")
        if not isinstance(self.payload, Mapping):
            raise ValueError("current frame payload must be a mapping")
        if self.payload.get("future_manifest_required") is True or "future_packets" in self.payload:
            raise ValueError("future route/packet data is forbidden for camera-live input")


@dataclass(frozen=True)
class AdmissionEvent:
    status: str
    frame_id: str
    frame_index: int
    reason: str
    wall_time_s: float
    replaced_frame_id: str | None = None


@dataclass
class LiveMetrics:
    camera_frames: int = 0
    candidates: int = 0
    admitted: int = 0
    superseded: int = 0
    processed: int = 0
    unified_commits: int = 0
    queue_wait_s: list[float] = field(default_factory=list)
    graph_lag_s: list[float] = field(default_factory=list)


class _Stop:
    pass


class CameraLiveCoordinator:
    """Bounded producer/consumer coordinator for one live run.

    ``perception_join`` must process only the supplied packet and return
    ``(conceptgraphs_packet, occusg_packet, metadata)``.  The two mapper
    sessions are the existing ``update_frame`` implementations.  The
    coordinator never waits for a browser and never dispatches a frame to
    only one mapper.
    """

    def __init__(self, *, perception_join: Callable[[CurrentFramePacket], tuple[Any, Any, Mapping[str, Any]]],
                 conceptgraphs_session: Any, occusg_session: Any,
                 publish_revision: Callable[[CurrentFramePacket, Any, Any, Mapping[str, Any]], None],
                 mapper_pair_dispatch: Callable[[Any, Any], tuple[Any, Any]] | None = None,
                 max_pending: int = 1):
        if max_pending != 1:
            raise ValueError("camera-live policy is locked to one pending frame")
        self._perception_join = perception_join
        self._cg = conceptgraphs_session
        self._occ = occusg_session
        self._publish = publish_revision
        self._mapper_pair_dispatch = mapper_pair_dispatch
        self._queue: queue.Queue[CurrentFramePacket | _Stop] = queue.Queue(maxsize=1)
        self._queue_lock = threading.Lock()
        self._events: list[AdmissionEvent] = []
        self._metrics = LiveMetrics()
        self._seen: set[str] = set()
        self._last_index = -1
        self._lock = threading.Lock()
        self._active = False
        self._failed: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._started_wall = time.monotonic()
        self._enqueued_wall: dict[str, float] = {}
        self._mutation_possible = False

    @property
    def events(self) -> tuple[AdmissionEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def metrics(self) -> LiveMetrics:
        return self._metrics

    @property
    def failure(self) -> BaseException | None:
        return self._failed

    @property
    def mutation_possible(self) -> bool:
        return self._mutation_possible

    def _event(self, event: AdmissionEvent) -> None:
        with self._lock:
            self._events.append(event)

    def start(self) -> None:
        if self._thread is not None:
            raise CameraLiveError("coordinator already started")
        self._active = True
        self._thread = threading.Thread(target=self._run, name="camera-live-coordinator", daemon=True)
        self._thread.start()

    def submit(self, packet: CurrentFramePacket, *, candidate: bool = True) -> str:
        if not self._active or self._thread is None:
            raise CameraLiveError("coordinator is not running")
        packet.validate()
        with self._lock:
            self._metrics.camera_frames += 1
            if candidate:
                self._metrics.candidates += 1
        if candidate:
            self._event(AdmissionEvent("candidate", packet.frame_id, packet.frame_index,
                                       "keyframe_policy_candidate", time.time()))
        with self._lock:
            if packet.frame_id in self._seen or packet.frame_index <= self._last_index:
                duplicate = True
            else:
                duplicate = False
                self._seen.add(packet.frame_id)
                self._last_index = packet.frame_index
        if duplicate:
            self._event(AdmissionEvent("superseded", packet.frame_id, packet.frame_index,
                                       "duplicate_or_stale_before_dispatch", time.time()))
            return "superseded"
        try:
            with self._queue_lock:
                self._queue.put_nowait(packet)
        except queue.Full:
            # The consumer may have taken the pending packet between the
            # Full check and get_nowait.  In that case there is no pending
            # packet to supersede; retry the put as a fresh admission.
            old = None
            with self._queue_lock:
                try:
                    old = self._queue.get_nowait()
                except queue.Empty:
                    pass
                self._queue.put_nowait(packet)
            if old is not None:
                assert isinstance(old, CurrentFramePacket)
                self._metrics.superseded += 1
                self._event(AdmissionEvent("superseded", old.frame_id, old.frame_index,
                                           "pending_replaced_latest_wins", time.time(), old.frame_id))
            self._enqueued_wall[packet.frame_id] = time.monotonic()
            self._metrics.admitted += 1
            self._event(AdmissionEvent("admitted", packet.frame_id, packet.frame_index,
                                       "pending_replacement" if old is not None else "consumer_freed_slot", time.time()))
            return "admitted"
        self._enqueued_wall[packet.frame_id] = time.monotonic()
        self._metrics.admitted += 1
        self._event(AdmissionEvent("admitted", packet.frame_id, packet.frame_index,
                                   "bounded_queue_capacity", time.time()))
        return "admitted"

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if isinstance(item, _Stop):
                return
            started = time.monotonic()
            enqueued = self._enqueued_wall.pop(item.frame_id, self._started_wall)
            self._metrics.queue_wait_s.append(max(0.0, started - enqueued))
            try:
                cg_packet, occ_packet, metadata = self._perception_join(item)
                # Dispatch is a pair barrier: both sessions receive this
                # admitted frame before publication can happen.
                self._event(AdmissionEvent("update_started", item.frame_id, item.frame_index,
                                           "mapper_pair_dispatch", time.time()))
                self._mutation_possible = True
                if self._mapper_pair_dispatch is not None:
                    try:
                        cg_result, occ_result = self._mapper_pair_dispatch(cg_packet, occ_packet)
                    except BaseException as exc:
                        raise CameraLiveReplayRequired("mapper pair failed after dispatch") from exc
                else:
                    cg_result = self._cg.update_frame(cg_packet)
                    try:
                        occ_result = self._occ.update_frame(occ_packet)
                    except BaseException as exc:
                        raise CameraLiveReplayRequired("OccuSG failed after ConceptGraphs mutation") from exc
                self._publish(item, cg_result, occ_result, metadata)
                self._metrics.processed += 1
                self._metrics.unified_commits += 1
                if item.camera_wall_time_s is not None:
                    self._metrics.graph_lag_s.append(max(0.0, time.time() - item.camera_wall_time_s))
                elif (item.camera_monotonic_s is not None
                      and item.camera_monotonic_domain == "coordinator_process"):
                    self._metrics.graph_lag_s.append(max(0.0, time.monotonic() - item.camera_monotonic_s))
                self._event(AdmissionEvent("processed", item.frame_id, item.frame_index,
                                           "both_branches_published", time.time()))
            except BaseException as exc:
                self._failed = exc
                self._active = False
                return

    def finish(self, *, timeout_s: float = 30.0) -> None:
        if self._thread is None:
            raise CameraLiveError("coordinator was not started")
        deadline = time.monotonic() + timeout_s
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not self._queue.empty():
            self._failed = TimeoutError("camera-live queue did not drain")
            self._active = False
        try:
            self._queue.put(_Stop(), timeout=max(0.0, deadline - time.monotonic()))
        except queue.Full as exc:
            raise TimeoutError("camera-live stop signal could not be queued") from exc
        self._thread.join(max(0.0, deadline - time.monotonic()))
        self._active = False
        if self._thread.is_alive():
            raise TimeoutError("camera-live coordinator did not stop")
        if self._failed is not None:
            error_type = CameraLiveReplayRequired if self._mutation_possible else CameraLiveError
            raise error_type(str(self._failed)) from self._failed
        self._cg.finalize()
        self._occ.finalize()

    def close(self) -> None:
        self._active = False
        if self._thread is not None and self._thread.is_alive():
            try:
                self._queue.put_nowait(_Stop())
            except queue.Full:
                pass
            self._thread.join(timeout=5.0)
        errors: list[BaseException] = []
        for session in (self._cg, self._occ):
            try:
                session.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise CameraLiveError("camera-live cleanup incomplete: " + "; ".join(map(str, errors)))
