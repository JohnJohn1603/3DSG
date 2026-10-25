from __future__ import annotations

import numpy as np
import pytest

from integration_v2.occusg_session import (
    DuplicateOrStalePacket,
    OccuSGSession,
    OccuSGSessionError,
    close_resources,
)
from standalone_sg.contracts import ContractError
from standalone_sg.mapping.projection import FreeSpaceGrid2D
from standalone_sg.pipeline import FramePacket, SceneGraphBuilder
from standalone_sg.pipeline.builder import CleanupError


def _packet(index: int, timestamp: float | None = None) -> FramePacket:
    values = np.zeros((2, 2), dtype=np.int8)
    z = np.full(values.shape, np.nan)
    grid = FreeSpaceGrid2D(values, 1.0, (0.0, 0.0), z, z, revision=index + 1)
    return FramePacket(
        "scene", "sequence", f"frame-{index}", index,
        float(index if timestamp is None else timestamp), "grid", grid=grid,
    )


def test_session_rejects_duplicate_stale_and_cross_sequence_before_builder():
    session = OccuSGSession.open(builder=SceneGraphBuilder())
    session.update_frame(_packet(0))
    before = session.builder.snapshot()
    with pytest.raises(DuplicateOrStalePacket):
        session.update_frame(_packet(0))
    with pytest.raises(DuplicateOrStalePacket):
        session.update_frame(FramePacket(
            "scene", "sequence", "stale", 0, 0.0, "grid", grid=_packet(0).grid,
        ))
    cross = FramePacket("other", "sequence", "frame-1", 1, 1.0, "grid", grid=_packet(1).grid)
    with pytest.raises(DuplicateOrStalePacket):
        session.update_frame(cross)
    assert session.builder.snapshot() == before
    session.close()


def test_session_finalize_is_terminal_and_close_is_idempotent():
    session = OccuSGSession.open(builder=SceneGraphBuilder())
    session.update_frame(_packet(0))
    first = session.finalize(1.0)
    assert first[0].revision == 2
    assert session.finalize(1.0) == first
    with pytest.raises(OccuSGSessionError, match="finalized"):
        session.update_frame(_packet(1))
    session.close()
    session.close()
    assert session.status == "closed"


def test_builder_failure_requires_replay_and_session_cleanup():
    class FailingBuilder(SceneGraphBuilder):
        def update(self, packet):
            raise RuntimeError("native update failed after mutation")

    session = OccuSGSession.open(builder=FailingBuilder())
    with pytest.raises(RuntimeError, match="native update failed"):
        session.update_frame(_packet(0))
    assert session.status == "failed"
    with pytest.raises(OccuSGSessionError, match="replay_required"):
        session.update_frame(_packet(1))
    with pytest.raises(OccuSGSessionError, match="replay_required"):
        session.finalize()
    session.close()


class _CloseProbe:
    def __init__(self, *, failures=0, signal=False):
        self.failures = failures
        self.signal = signal
        self.calls = 0

    def close(self):
        self.calls += 1
        if self.signal:
            raise KeyboardInterrupt("close interrupted")
        if self.failures:
            self.failures -= 1
            raise RuntimeError("close failed")


def test_builder_close_attempts_next_stage_and_retries_failed_stage():
    region = _CloseProbe(failures=1)
    mapping = _CloseProbe()
    builder = SceneGraphBuilder(region_stage=region, mapping_stage=mapping)

    with pytest.raises(CleanupError) as caught:
        builder.close()
    assert region.calls == 1
    assert mapping.calls == 1
    assert len(caught.value.errors) == 1

    # A failed stage remains retryable; the already-closed stage is not called again.
    builder.close()
    assert region.calls == 2
    assert mapping.calls == 1


def test_partial_initialization_cleanup_attempts_all_created_resources():
    mapping = _CloseProbe()
    region = _CloseProbe(failures=1)
    errors = close_resources((region, mapping))
    assert region.calls == 1
    assert mapping.calls == 1
    assert len(errors) == 1


def test_builder_close_releases_next_stage_before_reraising_cancellation():
    region = _CloseProbe(signal=True)
    mapping = _CloseProbe()
    builder = SceneGraphBuilder(region_stage=region, mapping_stage=mapping)
    with pytest.raises(KeyboardInterrupt, match="close interrupted"):
        builder.close()
    assert region.calls == 1
    assert mapping.calls == 1


def test_interruption_during_update_preserves_signal_and_closes_stage():
    class InterruptingBuilder(SceneGraphBuilder):
        def update(self, packet):
            raise KeyboardInterrupt("update interrupted")

    region = _CloseProbe()
    session = OccuSGSession.open(builder=InterruptingBuilder(region_stage=region))
    with pytest.raises(KeyboardInterrupt, match="update interrupted"):
        session.update_frame(_packet(0))
    assert session.status == "failed"
    session.close()
    assert region.calls == 1


def test_close_retries_after_failure_and_is_idempotent_after_success():
    region = _CloseProbe(failures=1)
    session = OccuSGSession.open(builder=SceneGraphBuilder(region_stage=region))
    with pytest.raises(CleanupError):
        session.close()
    assert session.status == "closed"
    session.close()
    assert region.calls == 2
