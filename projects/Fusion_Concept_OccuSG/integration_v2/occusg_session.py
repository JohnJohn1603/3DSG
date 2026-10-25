"""Persistent OccuSG session around the existing M9/native core.

The session owns lifecycle and packet ordering only.  Occupancy, projection,
region segmentation, tracking and graph semantics remain in the injected
``SceneGraphBuilder`` and its existing stages.  This module is intentionally
small so batch replay and future streaming callers share ``builder.update``.
"""
from __future__ import annotations

from typing import Any

from standalone_sg.pipeline import FramePacket, SceneGraphBuilder


def attach_cleanup_errors(primary: BaseException, cleanup: BaseException) -> None:
    """Keep the primary failure while exposing cleanup failures to callers."""

    errors = tuple(getattr(cleanup, "errors", (cleanup,)))
    setattr(primary, "cleanup_errors", errors)
    details = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note(f"cleanup errors: {details}")


def close_resources(resources) -> tuple[BaseException, ...]:
    """Attempt every resource close and return non-signal cleanup errors.

    Cancellation signals are remembered while the remaining resources are
    closed, then re-raised after the cleanup pass.  A caller handling a
    primary failure may attach that signal before re-raising the primary.
    """

    errors = []
    signals = []
    seen = set()
    for resource in resources:
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        closer = getattr(resource, "close", None)
        if not callable(closer):
            continue
        try:
            closer()
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            signals.append(exc)
            continue
        except BaseException as exc:
            errors.append(exc)
    if signals:
        if errors:
            setattr(signals[0], "cleanup_errors", tuple(errors))
            add_note = getattr(signals[0], "add_note", None)
            if callable(add_note):
                add_note("cleanup errors: " + "; ".join(str(error) for error in errors))
        raise signals[0]
    return tuple(errors)


class OccuSGSessionError(RuntimeError):
    """The session cannot perform the requested lifecycle operation."""


class DuplicateOrStalePacket(OccuSGSessionError):
    """A packet rejected before the builder/native stages are called."""


class OccuSGSession:
    """Lifecycle boundary for one scene/sequence.

    ``SceneGraphBuilder.update`` remains the single implementation used by
    both batch and session paths.  A callback failure poisons the session:
    native stages may already have mutated state, so replay from a known
    prefix is required instead of an unsafe retry.
    """

    def __init__(self, *, builder: SceneGraphBuilder):
        if not isinstance(builder, SceneGraphBuilder):
            raise TypeError("builder must be a SceneGraphBuilder")
        self.builder = builder
        self._closed = False
        self._builder_closed = False
        self._failed: BaseException | None = None
        self._finalized = False
        self._terminal_result: tuple[Any, Any] | None = None
        self._last_frame_index: int | None = None
        self._last_timestamp_s: float | None = None
        self._scene_id: str | None = None
        self._sequence_id: str | None = None
        self._seen_frame_ids: set[str] = set()

    @classmethod
    def open(cls, *, builder: SceneGraphBuilder) -> "OccuSGSession":
        return cls(builder=builder)

    @property
    def status(self) -> str:
        if self._failed is not None:
            return "failed"
        if self._closed:
            return "closed"
        if self._finalized:
            return "finalized"
        return "open"

    @property
    def failure(self) -> BaseException | None:
        return self._failed

    @property
    def processed_frame_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._seen_frame_ids))

    def _check_open(self, operation: str) -> None:
        if self._closed:
            raise OccuSGSessionError("session is closed")
        if self._failed is not None:
            raise OccuSGSessionError(
                f"session failed; replay_required before {operation}: {self._failed}"
            )
        if self._finalized:
            raise OccuSGSessionError("session is finalized")

    def update_frame(self, packet: FramePacket):
        self._check_open("update_frame")
        if not isinstance(packet, FramePacket):
            raise OccuSGSessionError("update_frame requires FramePacket")

        # Validate all ordering/identity fields before entering builder.update;
        # rejected packets therefore cannot mutate native state or consume RNG.
        if packet.frame_id in self._seen_frame_ids:
            raise DuplicateOrStalePacket(f"duplicate frame_id: {packet.frame_id}")
        if self._last_frame_index is not None and packet.frame_index <= self._last_frame_index:
            raise DuplicateOrStalePacket(
                f"stale frame_index={packet.frame_index}; last={self._last_frame_index}"
            )
        if self._last_timestamp_s is not None and packet.timestamp_s < self._last_timestamp_s:
            raise DuplicateOrStalePacket(
                f"stale timestamp_s={packet.timestamp_s}; last={self._last_timestamp_s}"
            )
        if self._scene_id is not None and (
            packet.scene_id != self._scene_id or packet.sequence_id != self._sequence_id
        ):
            raise DuplicateOrStalePacket("packet scene/sequence differs from open session")

        try:
            result = self.builder.update(packet)
        except BaseException as exc:
            self._failed = exc
            raise

        self._seen_frame_ids.add(packet.frame_id)
        self._last_frame_index = packet.frame_index
        self._last_timestamp_s = packet.timestamp_s
        self._scene_id = packet.scene_id
        self._sequence_id = packet.sequence_id
        return result

    def finalize(self, timestamp_s: float | None = None):
        if self._finalized:
            if self._closed:
                return self._terminal_result
            return self._terminal_result
        self._check_open("finalize")
        try:
            self._terminal_result = self.builder.flush(timestamp_s)
        except BaseException as exc:
            self._failed = exc
            raise
        self._finalized = True
        return self._terminal_result

    def close(self) -> None:
        if self._builder_closed:
            return
        self._closed = True
        try:
            self.builder.close()
        except BaseException as cleanup_error:
            if self._failed is not None:
                attach_cleanup_errors(self._failed, cleanup_error)
                return
            raise
        self._builder_closed = True
