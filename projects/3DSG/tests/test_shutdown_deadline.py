from __future__ import annotations

import time

from exploration.runtime import NavigationRuntime


def test_navigation_workers_share_one_absolute_shutdown_deadline():
    runtime = object.__new__(NavigationRuntime)
    observed_timeouts: list[float] = []

    class _Worker:
        def close(self, timeout_s: float) -> bool:
            observed_timeouts.append(timeout_s)
            time.sleep(0.025)
            return True

    runtime._map_worker = _Worker()
    runtime._plan_worker = _Worker()
    deadline = time.monotonic() + 0.08
    started = time.monotonic()

    assert runtime.close(deadline_monotonic_s=deadline)
    elapsed = time.monotonic() - started

    assert len(observed_timeouts) == 2
    assert observed_timeouts[1] < observed_timeouts[0]
    assert observed_timeouts[1] <= deadline - started - 0.015
    assert elapsed < 0.07
