from __future__ import annotations

import pytest

from exploration.p2_runtime import cpu_session_options


class _Options:
    intra_op_num_threads = 0
    inter_op_num_threads = 0
    execution_mode = None


class _Ort:
    SessionOptions = _Options
    ExecutionMode = type("ExecutionMode", (), {"ORT_SEQUENTIAL": "sequential"})


def test_p2_cpu_session_options_are_bounded_and_sequential() -> None:
    options = cpu_session_options(_Ort, intra_op_threads=2, inter_op_threads=1)
    assert options.intra_op_num_threads == 2
    assert options.inter_op_num_threads == 1
    assert options.execution_mode == "sequential"


@pytest.mark.parametrize(
    ("intra", "inter"),
    [(0, 1), (9, 1), (1, 0), (1, 5), (True, 1), (1, False)],
)
def test_p2_cpu_session_options_reject_invalid_thread_limits(intra: int, inter: int) -> None:
    with pytest.raises(ValueError):
        cpu_session_options(_Ort, intra_op_threads=intra, inter_op_threads=inter)
