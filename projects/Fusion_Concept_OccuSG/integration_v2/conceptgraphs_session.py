"""Small lifecycle wrapper for stateful ConceptGraphs mapping.

The wrapper owns lifecycle and input-order rules.  The mapping implementation is
injected by the existing cached runner, so batch and future streaming callers use
the same per-frame and terminal functions without copying the algorithm.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class SessionStateError(RuntimeError):
    """The session cannot accept the requested lifecycle operation."""


class DuplicateOrStaleFrame(SessionStateError):
    """A frame was rejected before the mapping callback was invoked."""


@dataclass(frozen=True)
class SessionIdentity:
    frame_index: int
    frame_id: str


class ConceptGraphsMappingSession:
    """Lifecycle and ordering boundary around the existing mapper core.

    ``update_impl`` and ``finalize_impl`` are the reference mapping functions;
    this class deliberately does not implement association or geometry.
    """

    def __init__(self, *, state: Any, update_impl: Callable[[Any, dict], Any],
                 finalize_impl: Callable[[Any], Any]):
        self.state = state
        self._update_impl = update_impl
        self._finalize_impl = finalize_impl
        self._opened = True
        self._closed = False
        self._failed = False
        self._finalized = False
        self._failure: BaseException | None = None
        self._last_frame_index: int | None = None
        self._seen_frame_ids: set[str] = set()
        self._terminal_result: Any = None

    @classmethod
    def open(cls, *, state: Any, update_impl: Callable[[Any, dict], Any],
             finalize_impl: Callable[[Any], Any]) -> "ConceptGraphsMappingSession":
        return cls(state=state, update_impl=update_impl, finalize_impl=finalize_impl)

    @property
    def status(self) -> str:
        if self._failed:
            return "failed"
        if self._closed:
            return "closed"
        if self._finalized:
            return "finalized"
        return "open"

    @property
    def processed_frame_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._seen_frame_ids))

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def update_frame(self, packet: dict) -> Any:
        if not self._opened or self._closed:
            raise SessionStateError("session is closed")
        if self._failed:
            raise SessionStateError("session failed; replay_required")
        if self._finalized:
            raise SessionStateError("session is finalized")
        try:
            frame_index = int(packet["frame_index"])
            frame_id = str(packet["frame_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionStateError("frame packet lacks frame_index/frame_id") from exc

        # These checks happen before the mapper callback, so a rejected input
        # cannot consume RNG or mutate the mapper state.
        if frame_id in self._seen_frame_ids:
            raise DuplicateOrStaleFrame(f"duplicate frame_id: {frame_id}")
        if self._last_frame_index is not None and frame_index <= self._last_frame_index:
            raise DuplicateOrStaleFrame(
                f"stale frame_index={frame_index}; last={self._last_frame_index}")

        try:
            result = self._update_impl(self.state, packet)
        except BaseException as exc:
            # The callback may have mutated reference state before failing.  Do
            # not retry it: callers must discard the session and replay from a
            # known prefix.
            self._failed = True
            self._failure = exc
            raise
        self._seen_frame_ids.add(frame_id)
        self._last_frame_index = frame_index
        return result

    def finalize(self) -> Any:
        if self._closed:
            raise SessionStateError("session is closed")
        if self._failed:
            raise SessionStateError("session failed; replay_required")
        if self._finalized:
            return self._terminal_result
        try:
            self._terminal_result = self._finalize_impl(self.state)
        except BaseException as exc:
            self._failed = True
            self._failure = exc
            raise
        self._finalized = True
        return self._terminal_result

    def close(self) -> None:
        # There are no owned subprocesses in this checkpoint.  Close is still
        # idempotent so a failed caller can safely run cleanup in finally.
        self._closed = True

