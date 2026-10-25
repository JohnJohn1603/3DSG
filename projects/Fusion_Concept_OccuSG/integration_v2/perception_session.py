"""Persistent perception-session lifecycle shared by batch and streaming callers.

This module owns ordering and failure semantics only.  Detector/feature
implementations remain in their existing providers and are injected as
callbacks, so this does not create a second perception algorithm.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from typing import Any


class PerceptionSessionError(RuntimeError):
    """The perception session cannot perform the requested lifecycle action."""


class DuplicateOrStalePerceptionFrame(PerceptionSessionError):
    """A frame rejected before detector/encoder callback or RNG use."""


def _attach_cleanup_errors(primary: BaseException, errors: Iterable[BaseException]) -> None:
    errors = tuple(errors)
    if not errors:
        return
    setattr(primary, "cleanup_errors", errors)
    add_note = getattr(primary, "add_note", None)
    if callable(add_note):
        add_note("perception cleanup errors: " + "; ".join(str(error) for error in errors))


def _close_all(resources: Iterable[Any]) -> tuple[BaseException, ...]:
    """Close each owned resource; preserve cancellation after the pass."""
    errors: list[BaseException] = []
    signals: list[BaseException] = []
    seen: set[int] = set()
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
        except BaseException as exc:
            errors.append(exc)
    if signals:
        _attach_cleanup_errors(signals[0], errors)
        raise signals[0]
    return tuple(errors)


class PerceptionSession:
    """One ordered, stateful perception lifecycle.

    ``process_frame`` receives the current packet only.  It may return an
    empty-detection result.  A callback failure poisons the session because a
    provider may have mutated model/feature state before raising; callers must
    replay from a known prefix rather than retrying the same packet.
    """

    def __init__(self, *, process_frame: Callable[[Mapping[str, Any]], Any],
                 finalize_frame: Callable[[], Any] | None = None,
                 resources: Iterable[Any] = ()):
        if not callable(process_frame):
            raise TypeError("process_frame must be callable")
        self._process_frame = process_frame
        self._finalize_frame = finalize_frame
        self._resources = tuple(resources)
        self._closed = False
        self._failed: BaseException | None = None
        self._finalized = False
        self._terminal_result: Any = None
        self._last_frame_index: int | None = None
        self._last_timestamp_s: float | None = None
        self._seen_frame_ids: set[str] = set()
        self._close_complete = False

    @classmethod
    def open(cls, *, process_frame: Callable[[Mapping[str, Any]], Any],
             finalize_frame: Callable[[], Any] | None = None,
             resources: Iterable[Any] = ()) -> "PerceptionSession":
        return cls(process_frame=process_frame, finalize_frame=finalize_frame, resources=resources)

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
            raise PerceptionSessionError("perception session is closed")
        if self._failed is not None:
            raise PerceptionSessionError(
                f"perception session failed; replay_required before {operation}: {self._failed}"
            )
        if self._finalized:
            raise PerceptionSessionError("perception session is finalized")

    @staticmethod
    def _validate_packet(packet: Mapping[str, Any]) -> tuple[int, str, float]:
        if not isinstance(packet, Mapping):
            raise PerceptionSessionError("update_frame requires a mapping packet")
        try:
            frame_index = int(packet["frame_index"])
            frame_id = str(packet["frame_id"])
            timestamp_s = float(packet["timestamp_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PerceptionSessionError("packet requires frame_index/frame_id/timestamp_s") from exc
        if frame_index < 0 or not frame_id or not math.isfinite(timestamp_s):
            raise PerceptionSessionError("packet identity/timestamp is invalid")
        if "source_hashes" in packet and not isinstance(packet["source_hashes"], Mapping):
            raise PerceptionSessionError("source_hashes must be a mapping when present")
        return frame_index, frame_id, timestamp_s

    def update_frame(self, packet: Mapping[str, Any]) -> Any:
        self._check_open("update_frame")
        frame_index, frame_id, timestamp_s = self._validate_packet(packet)
        if frame_id in self._seen_frame_ids:
            raise DuplicateOrStalePerceptionFrame(f"duplicate frame_id: {frame_id}")
        if self._last_frame_index is not None and frame_index <= self._last_frame_index:
            raise DuplicateOrStalePerceptionFrame(
                f"stale frame_index={frame_index}; last={self._last_frame_index}"
            )
        if self._last_timestamp_s is not None and timestamp_s < self._last_timestamp_s:
            raise DuplicateOrStalePerceptionFrame(
                f"stale timestamp_s={timestamp_s}; last={self._last_timestamp_s}"
            )
        try:
            result = self._process_frame(packet)
        except BaseException as exc:
            self._failed = exc
            raise
        self._seen_frame_ids.add(frame_id)
        self._last_frame_index = frame_index
        self._last_timestamp_s = timestamp_s
        return result

    def finalize(self) -> Any:
        if self._closed:
            raise PerceptionSessionError("perception session is closed")
        if self._failed is not None:
            raise PerceptionSessionError("perception session failed; replay_required")
        if self._finalized:
            return self._terminal_result
        try:
            self._terminal_result = self._finalize_frame() if self._finalize_frame else None
        except BaseException as exc:
            self._failed = exc
            raise
        self._finalized = True
        return self._terminal_result

    def close(self) -> None:
        if self._close_complete:
            return
        self._closed = True
        errors = _close_all(self._resources)
        if errors:
            cleanup = RuntimeError("perception cleanup incomplete")
            cleanup.errors = errors
            if self._failed is not None:
                _attach_cleanup_errors(self._failed, errors)
                self._close_complete = False
                return
            raise cleanup
        self._close_complete = True

