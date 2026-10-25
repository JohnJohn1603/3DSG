"""Optional process niceness for auxiliary graph/perception workers."""

from __future__ import annotations

import os
import subprocess


def graph_worker_nice_from_env() -> int | None:
    """Read a bounded nice value; None preserves the host default priority."""
    value = os.environ.get("THREEDSG_GRAPH_WORKER_NICE")
    if value is None:
        return None
    try:
        nice_value = int(value)
    except ValueError as error:
        raise ValueError("THREEDSG_GRAPH_WORKER_NICE must be an integer in 0..19") from error
    if not 0 <= nice_value <= 19:
        raise ValueError("THREEDSG_GRAPH_WORKER_NICE must be an integer in 0..19")
    return nice_value


def apply_process_nice(process: subprocess.Popen, nice_value: int | None) -> None:
    """Lower one worker's CPU scheduling priority and fail closed on errors."""
    if nice_value is None:
        return
    if type(nice_value) is not int or not 0 <= nice_value <= 19:
        raise ValueError("worker niceness must be an integer in 0..19")
    try:
        os.setpriority(os.PRIO_PROCESS, process.pid, nice_value)
    except OSError:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
        raise
