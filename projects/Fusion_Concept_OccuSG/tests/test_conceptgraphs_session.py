from __future__ import annotations

import pytest

from integration_v2.conceptgraphs_session import (
    ConceptGraphsMappingSession,
    DuplicateOrStaleFrame,
    SessionStateError,
)


def test_duplicate_and_stale_frames_are_rejected_before_callback():
    calls = []

    def update(state, packet):
        calls.append(packet["frame_id"])
        state.append(packet["frame_id"])

    session = ConceptGraphsMappingSession.open(
        state=[], update_impl=update, finalize_impl=lambda state: list(state))
    session.update_frame({"frame_index": 1, "frame_id": "f1"})
    with pytest.raises(DuplicateOrStaleFrame):
        session.update_frame({"frame_index": 1, "frame_id": "f1"})
    with pytest.raises(DuplicateOrStaleFrame):
        session.update_frame({"frame_index": 0, "frame_id": "f0"})
    assert calls == ["f1"]
    assert session.state == ["f1"]


def test_failure_requires_replay_and_close_is_idempotent():
    calls = []

    def update(state, packet):
        calls.append(packet["frame_id"])
        state.append(packet["frame_id"])
        if packet["frame_id"] == "bad":
            raise ValueError("mapping failure after mutation")

    finalized = []
    session = ConceptGraphsMappingSession.open(
        state=[], update_impl=update,
        finalize_impl=lambda state: finalized.append(list(state)) or list(state))
    with pytest.raises(ValueError, match="mapping failure"):
        session.update_frame({"frame_index": 1, "frame_id": "bad"})
    with pytest.raises(SessionStateError, match="replay_required"):
        session.update_frame({"frame_index": 2, "frame_id": "f2"})
    with pytest.raises(SessionStateError, match="replay_required"):
        session.finalize()
    session.close()
    session.close()
    assert calls == ["bad"]
    assert finalized == []


def test_finalize_is_terminal_and_close_does_not_finalize_again():
    calls = []
    session = ConceptGraphsMappingSession.open(
        state=[], update_impl=lambda state, packet: state.append(packet["frame_id"]),
        finalize_impl=lambda state: calls.append(tuple(state)) or tuple(state))
    session.update_frame({"frame_index": 0, "frame_id": "f0"})
    assert session.finalize() == ("f0",)
    assert session.finalize() == ("f0",)
    assert calls == [("f0",)]
    session.close()
    with pytest.raises(SessionStateError, match="closed"):
        session.update_frame({"frame_index": 1, "frame_id": "f1"})

