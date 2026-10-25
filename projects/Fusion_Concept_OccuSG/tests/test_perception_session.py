from __future__ import annotations

import pytest

from integration_v2.perception_session import (
    DuplicateOrStalePerceptionFrame,
    PerceptionSession,
    PerceptionSessionError,
)


def packet(index: int, *, frame_id: str | None = None, timestamp: float | None = None):
    return {
        "frame_index": index,
        "frame_id": frame_id or f"f{index}",
        "timestamp_s": float(index if timestamp is None else timestamp),
        "rgbd": "current-frame-placeholder",
        "K": "current-intrinsics-placeholder",
        "pose": "current-pose-placeholder",
        "source_hashes": {"rgb.npy": f"sha-{index}"},
    }


class Resource:
    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures

    def close(self):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("close failed")


def test_empty_detection_is_valid_and_model_callback_is_reused():
    calls = []
    session = PerceptionSession.open(
        process_frame=lambda p: calls.append(p["frame_id"]) or {"detections": []},
    )
    assert session.update_frame(packet(0)) == {"detections": []}
    assert session.update_frame(packet(1)) == {"detections": []}
    session.finalize()
    session.close()
    assert calls == ["f0", "f1"]


def test_duplicate_and_stale_rejected_before_callback():
    calls = []
    session = PerceptionSession.open(
        process_frame=lambda p: calls.append(p["frame_id"]),
    )
    session.update_frame(packet(1))
    with pytest.raises(DuplicateOrStalePerceptionFrame):
        session.update_frame(packet(1))
    with pytest.raises(DuplicateOrStalePerceptionFrame):
        session.update_frame(packet(0))
    assert calls == ["f1"]
    session.close()


def test_failure_requires_replay_and_close_is_retryable():
    resource = Resource(failures=1)

    def fail(_packet):
        raise RuntimeError("inference failed after provider mutation")

    session = PerceptionSession.open(process_frame=fail, resources=(resource,))
    with pytest.raises(RuntimeError, match="inference failed"):
        session.update_frame(packet(0))
    with pytest.raises(PerceptionSessionError, match="replay_required"):
        session.update_frame(packet(1))
    session.close()
    assert hasattr(session.failure, "cleanup_errors")
    # The close retry is explicit; the session never silently retries inference.
    session.close()
    assert resource.calls == 2


def test_cancellation_does_not_become_success_and_resource_is_attempted():
    resource = Resource()

    def cancel(_packet):
        raise KeyboardInterrupt("cancelled")

    session = PerceptionSession.open(process_frame=cancel, resources=(resource,))
    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        session.update_frame(packet(0))
    session.close()
    assert resource.calls == 1
    assert session.status == "failed"
