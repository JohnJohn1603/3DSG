"""Ordered asynchronous bridge from durable RGB-D keyframes to graph providers.

Providers return a frame-aligned canonical graph snapshot. A compatibility path
remains for older integrations that inject two mapper sessions and packet
converters directly; backend-specific dependencies stay outside navigation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
import threading
import time
from typing import Any, Callable

from .contracts import FramePacket
from .habitat_online import RGBFrame
from .keyframe_scheduler import KeyframeScheduler, PersistedKeyframe
from .keyframe_store import KeyframeStore
from .scene_graph_provider import SceneGraphProvider, SceneGraphSnapshot, validate_snapshot_for_observation


@dataclass(frozen=True)
class GraphObservation:
    keyframe: PersistedKeyframe
    rgb: RGBFrame
    packet: FramePacket
    perception_result: Any = None

    def __post_init__(self) -> None:
        identity = (self.keyframe.run_id, self.keyframe.scene_id,
                    self.keyframe.frame_id, self.keyframe.frame_index)
        if identity != (self.packet.run_id, self.packet.scene_id,
                        self.packet.frame_id, self.packet.frame_index):
            raise ValueError("loaded graph observation identity differs from keyframe")
        if identity != (self.rgb.run_id, self.rgb.scene_id,
                        self.rgb.frame_id, self.rgb.frame_index):
            raise ValueError("loaded RGB identity differs from keyframe")


@dataclass(frozen=True)
class GraphCommit:
    run_id: str
    scene_id: str
    frame_id: str
    frame_index: int
    dispatch_sequence: int
    conceptgraphs_result: Any
    occusg_result: Any
    committed_monotonic_s: float
    graph_snapshot: SceneGraphSnapshot | None = None


class GraphBridgeError(RuntimeError):
    """The paired graph sessions can no longer be trusted without replay."""


class GraphIntegrationWorker:
    """Consume scheduler leases without blocking Habitat capture/control.

    Mapper updates mutate independent persistent state and cannot be rolled
    back atomically. If either side fails after dispatch, this worker stops,
    marks the scheduler terminal, and never publishes a partial pair.
    """

    def __init__(self, *, scheduler: KeyframeScheduler, store: KeyframeStore,
                 publish: Callable[[GraphCommit], None],
                 provider: SceneGraphProvider | None = None,
                 conceptgraphs_session: Any = None, occusg_session: Any = None,
                 conceptgraphs_packet: Callable[[GraphObservation], Any] | None = None,
                 occusg_packet: Callable[[GraphObservation], Any] | None = None,
                 perceive: Callable[[GraphObservation], Any] | None = None,
                 update_pair: Callable[[GraphObservation, Any, Any], tuple[Any, Any]] | None = None,
                 poll_interval_s: float = 0.02) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll interval must be positive")
        if provider is not None:
            if not str(provider.provider_id).strip():
                raise ValueError("provider ID must be non-empty")
            if provider.capabilities.provider_id != provider.provider_id:
                raise ValueError("provider capabilities ID does not match provider ID")
        if provider is None and any(value is None for value in (
                conceptgraphs_session, occusg_session, conceptgraphs_packet, occusg_packet)):
            raise ValueError("legacy mapper sessions and packet converters are required without a provider")
        self.scheduler = scheduler
        self.store = store
        self.provider = provider
        self.cg = conceptgraphs_session
        self.occ = occusg_session
        self.cg_packet = conceptgraphs_packet
        self.occ_packet = occusg_packet
        self.publish = publish
        self.perceive = perceive
        self.update_pair = update_pair
        self.poll_interval_s = float(poll_interval_s)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self._failure: BaseException | None = None
        # Commit payloads can contain large mapper results. Retain metadata only;
        # authoritative revisions live in the mapper sessions/publisher.
        self._commit_count = 0
        self._last_committed_frame_id: str | None = None
        self._lock = threading.Lock()

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def commit_status(self) -> dict[str, Any]:
        with self._lock:
            return {"count": self._commit_count,
                    "last_frame_id": self._last_committed_frame_id}

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("graph integration worker already started")
        self._thread = threading.Thread(target=self._run,
                                       name="3dsg-graph-integration", daemon=True)
        self._thread.start()

    def close(self, *, timeout_s: float = 30.0,
               deadline_monotonic_s: float | None = None,
               drain_pending: bool = True) -> tuple[str, ...]:
        """Stop accepting work, optionally discard queued leases, then join.

        The in-flight mutation is always allowed to finish within the deadline.
        When ``drain_pending`` is false, queued durable keyframes remain on disk
        for replay and are returned to the caller for explicit reporting.
        """
        if timeout_s <= 0 and deadline_monotonic_s is None:
            raise ValueError("shutdown timeout must be positive")
        if self._thread is None:
            return ()
        deadline = (time.monotonic() + timeout_s if deadline_monotonic_s is None
                    else float(deadline_monotonic_s))
        dropped = (() if drain_pending else
                   self.scheduler.close("shutdown_pending_preserved_for_replay"))
        self._stop.set()
        remaining = max(0.0, deadline - time.monotonic())
        if not self._done.wait(remaining):
            self._failure = self._failure or TimeoutError("graph worker did not drain before shutdown")
            self.scheduler.close("graph_worker_shutdown_timeout")
            abort_provider = getattr(self.provider, "abort", None)
            if callable(abort_provider):
                try:
                    try:
                        supports_deadline = "deadline_monotonic_s" in inspect.signature(
                            abort_provider).parameters
                    except (TypeError, ValueError):
                        supports_deadline = False
                    if supports_deadline:
                        abort_provider("graph_worker_shutdown_timeout",
                                       deadline_monotonic_s=deadline)
                    else:
                        abort_provider("graph_worker_shutdown_timeout")
                except BaseException as exc:
                    self._failure.__context__ = exc
                remaining = max(0.0, deadline - time.monotonic())
                if remaining:
                    self._done.wait(min(5.0, remaining))
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            self._failure = self._failure or TimeoutError("graph worker thread did not stop")
        return dropped

    def _run(self) -> None:
        try:
            while True:
                lease = self.scheduler.take_next()
                if lease is None:
                    if self._stop.is_set():
                        self.scheduler.close("graph_worker_drained")
                        return
                    if self.scheduler.snapshot()["status"] == "failed":
                        return
                    time.sleep(self.poll_interval_s)
                    continue
                self._process(lease.keyframe, lease.dispatch_sequence)
        except BaseException as exc:
            self._failure = exc
        finally:
            self._done.set()

    def _process(self, keyframe: PersistedKeyframe, sequence: int) -> None:
        try:
            rgb, packet = self.store.load(keyframe)
            observation = GraphObservation(keyframe, rgb, packet)
            if self.provider is not None:
                graph_snapshot = self.provider.update(observation)
                validate_snapshot_for_observation(
                    graph_snapshot, observation, expected_provider_id=self.provider.provider_id)
                provenance = dict(graph_snapshot.provenance)
                source_capture_s = float(packet.host_monotonic_s)
                source_clock_domain = packet.capture_monotonic_domain
                existing_source_s = provenance.get("source_capture_monotonic_s")
                if (existing_source_s is not None
                        and abs(float(existing_source_s) - source_capture_s) > 1e-6):
                    raise ValueError("graph provenance capture clock differs from source keyframe")
                provenance["source_capture_monotonic_s"] = source_capture_s
                if source_clock_domain is not None:
                    provenance["source_capture_monotonic_domain"] = str(source_clock_domain)
                graph_snapshot = replace(graph_snapshot, provenance=provenance)
                cg_result = occ_result = None
            else:
                graph_snapshot = None
            if self.provider is None and self.perceive is not None:
                # P2 masks/features and P3 world detections are generated once
                # for this admitted keyframe, then shared by both mappers.
                perception_result = self.perceive(observation)
                observation = GraphObservation(keyframe, rgb, packet, perception_result)
            if self.provider is None:
                cg_packet = self.cg_packet(observation)
                occ_packet = self.occ_packet(observation)
                _assert_packet_identity(cg_packet, keyframe)
                _assert_packet_identity(occ_packet, keyframe)
                if self.update_pair is None:
                    cg_result = self.cg.update_frame(cg_packet)
                    occ_result = self.occ.update_frame(occ_packet)
                else:
                    # Native workers often block each branch until the paired
                    # publisher acknowledges both revisions. Let the plugin own
                    # that barrier and dispatch both packets together.
                    cg_result, occ_result = self.update_pair(observation, cg_packet, occ_packet)
            commit = GraphCommit(
                keyframe.run_id, keyframe.scene_id, keyframe.frame_id,
                keyframe.frame_index, sequence, cg_result, occ_result,
                time.monotonic(), graph_snapshot,
            )
            self.publish(commit)
            with self._lock:
                self._commit_count += 1
                self._last_committed_frame_id = commit.frame_id
        except BaseException as exc:
            self._failure = GraphBridgeError(
                f"paired graph dispatch failed at frame {keyframe.frame_id}; replay_required"
            )
            self._failure.__cause__ = exc
            self.scheduler.complete(keyframe.frame_id, success=False,
                                    reason=str(self._failure))
            return
        self.scheduler.complete(keyframe.frame_id, success=True)


def _assert_packet_identity(packet: Any, keyframe: PersistedKeyframe) -> None:
    try:
        get = packet.__getitem__
        frame_id, frame_index = str(get("frame_id")), int(get("frame_index"))
        run_id = get("run_id") if "run_id" in packet else None
        scene_id = get("scene_id") if "scene_id" in packet else None
    except (TypeError, KeyError, IndexError, AttributeError):
        frame_id = str(getattr(packet, "frame_id", ""))
        frame_index = int(getattr(packet, "frame_index", -1))
        run_id = getattr(packet, "run_id", None)
        scene_id = getattr(packet, "scene_id", None)
    expected = (keyframe.run_id, keyframe.scene_id, keyframe.frame_id, keyframe.frame_index)
    actual = (run_id, scene_id, frame_id, frame_index)
    if (frame_id != keyframe.frame_id or frame_index != keyframe.frame_index
            or run_id is not None and str(run_id) != keyframe.run_id
            or scene_id is not None and str(scene_id) != keyframe.scene_id):
        raise ValueError(f"mapper packet identity {actual} differs from keyframe {expected}")
