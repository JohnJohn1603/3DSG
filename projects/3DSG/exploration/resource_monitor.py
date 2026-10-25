"""Linux process-tree memory sampling and episode resource stop thresholds."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any


_GIB = 1024 ** 3


class ProcessTreeResourceMonitor:
    """Sample RSS for the run's process tree and host available memory.

    RSS is summed conservatively across processes, so shared libraries can be
    counted more than once. The value is a budget guard, not proportional-set
    size accounting.
    """

    def __init__(self, path: str | Path, *, root_pid: int | None = None,
                 process_tree_rss_cap_gib: float = 6.0,
                 warning_mem_available_gib: float = 2.0,
                 stop_mem_available_gib: float = 1.0,
                 stop_mem_grace_s: float = 3.0,
                 sample_period_s: float = 1.0) -> None:
        values = (process_tree_rss_cap_gib, warning_mem_available_gib,
                  stop_mem_available_gib, stop_mem_grace_s, sample_period_s)
        if any(not isinstance(value, (int, float)) or value <= 0 for value in values):
            raise ValueError("resource thresholds and sample period must be positive")
        if stop_mem_available_gib >= warning_mem_available_gib:
            raise ValueError("stop memory threshold must be below warning threshold")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.root_pid = int(root_pid or os.getpid())
        self.rss_cap_bytes = int(float(process_tree_rss_cap_gib) * _GIB)
        self.warning_mem_bytes = int(float(warning_mem_available_gib) * _GIB)
        self.stop_mem_bytes = int(float(stop_mem_available_gib) * _GIB)
        self.stop_mem_grace_s = float(stop_mem_grace_s)
        self.sample_period_s = float(sample_period_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._samples = 0
        self._peak_rss_bytes = 0
        self._total_cpu_seconds = 0.0
        self._peak_cpu_cores = 0.0
        self._cpu_seconds_by_name: dict[str, float] = {}
        self._previous_cpu_ticks: dict[int, tuple[str, int]] = {}
        self._previous_sample_monotonic_s: float | None = None
        self._clock_ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
        self._previous_host_cpu_ticks: tuple[int, int] | None = None
        self._host_cpu_busy_sum = 0.0
        self._host_cpu_busy_samples = 0
        self._host_cpu_busy_max: float | None = None
        self._host_load_1m_max: float | None = None
        self._min_available_bytes: int | None = None
        self._warning_samples = 0
        self._rss_stop_samples = 0
        self._low_memory_since: float | None = None
        self._stop_reason: str | None = None
        self._started_monotonic_s: float | None = None
        self._stopped_monotonic_s: float | None = None
        self._stream = None

    @property
    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop_reason

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("resource monitor already started")
        self._stream = self.path.open("x", encoding="utf-8", buffering=1)
        self._started_monotonic_s = time.monotonic()
        self._sample_once()
        self._thread = threading.Thread(target=self._run, name="3dsg-resource-monitor", daemon=True)
        self._thread.start()

    def close(self, *, timeout_s: float = 3.0) -> dict[str, Any]:
        self._stop.set()
        thread_alive = False
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout_s))
            thread_alive = self._thread.is_alive()
            if thread_alive:
                self._set_stop_reason("resource_monitor_shutdown_timeout")
        if self._stream is not None and not thread_alive:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None
        self._stopped_monotonic_s = time.monotonic()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "frontier-resource-monitor-summary/v1",
                "sample_file": self.path.name,
                "root_pid": self.root_pid,
                "sample_count": self._samples,
                "process_tree_rss_cap_gib": self.rss_cap_bytes / _GIB,
                "peak_process_tree_rss_gib": self._peak_rss_bytes / _GIB,
                "process_tree_cpu_seconds": self._total_cpu_seconds,
                "average_process_tree_cpu_cores": (
                    None if self._started_monotonic_s is None or self._stopped_monotonic_s is None
                    else self._total_cpu_seconds
                    / max(1e-9, self._stopped_monotonic_s - self._started_monotonic_s)),
                "peak_process_tree_cpu_cores": self._peak_cpu_cores,
                "host_cpu_count": os.cpu_count(),
                "average_host_cpu_busy_fraction": (
                    None if not self._host_cpu_busy_samples
                    else self._host_cpu_busy_sum / self._host_cpu_busy_samples),
                "max_host_cpu_busy_fraction": self._host_cpu_busy_max,
                "max_host_load_1m": self._host_load_1m_max,
                "process_cpu_seconds_by_name": dict(sorted(
                    self._cpu_seconds_by_name.items(), key=lambda row: row[1], reverse=True)),
                "warning_mem_available_gib": self.warning_mem_bytes / _GIB,
                "stop_mem_available_gib": self.stop_mem_bytes / _GIB,
                "minimum_mem_available_gib": (None if self._min_available_bytes is None
                                                else self._min_available_bytes / _GIB),
                "warning_sample_count": self._warning_samples,
                "rss_cap_exceeded_sample_count": self._rss_stop_samples,
                "stop_reason": self._stop_reason,
                "started_monotonic_s": self._started_monotonic_s,
                "stopped_monotonic_s": self._stopped_monotonic_s,
            }

    def _run(self) -> None:
        next_sample = time.monotonic() + self.sample_period_s
        while not self._stop.is_set():
            remaining = max(0.0, next_sample - time.monotonic())
            if self._stop.wait(remaining):
                return
            try:
                self._sample_once()
            except Exception as exc:
                self._set_stop_reason(f"resource_monitor_error:{type(exc).__name__}")
                return
            next_sample += self.sample_period_s
            if next_sample <= time.monotonic():
                next_sample = time.monotonic() + self.sample_period_s

    def _sample_once(self) -> None:
        now = time.monotonic()
        pids, rss_bytes, processes = _process_tree_sample(self.root_pid)
        cpu_seconds_by_process: list[dict[str, Any]] = []
        cpu_seconds = 0.0
        previous_sample = self._previous_sample_monotonic_s
        interval_s = 0.0 if previous_sample is None else max(0.0, now - previous_sample)
        current_cpu_ticks: dict[int, tuple[str, int]] = {}
        for pid in pids:
            process = processes.get(pid)
            if process is None:
                continue
            name = str(process["name"])
            ticks = int(process["cpu_ticks"])
            current_cpu_ticks[pid] = (name, ticks)
            previous = self._previous_cpu_ticks.get(pid)
            if previous is None or previous[0] != name:
                continue
            delta_ticks = max(0, ticks - previous[1])
            delta_s = delta_ticks / max(1, self._clock_ticks_per_second)
            if delta_s <= 0:
                continue
            cpu_seconds += delta_s
            cpu_seconds_by_process.append({
                "pid": pid,
                "name": name,
                "cpu_seconds": delta_s,
                "cpu_cores": delta_s / interval_s if interval_s > 0 else 0.0,
            })
        self._previous_cpu_ticks = current_cpu_ticks
        self._previous_sample_monotonic_s = now
        cpu_cores = cpu_seconds / interval_s if interval_s > 0 else 0.0
        current_host_cpu_ticks = _host_cpu_ticks()
        host_cpu_busy_fraction: float | None = None
        if current_host_cpu_ticks is not None:
            previous_host_cpu_ticks = self._previous_host_cpu_ticks
            if previous_host_cpu_ticks is not None:
                total_delta = max(0, current_host_cpu_ticks[0] - previous_host_cpu_ticks[0])
                idle_delta = max(0, current_host_cpu_ticks[1] - previous_host_cpu_ticks[1])
                if total_delta > 0:
                    host_cpu_busy_fraction = min(
                        1.0, max(0.0, (total_delta - min(total_delta, idle_delta)) / total_delta))
            self._previous_host_cpu_ticks = current_host_cpu_ticks
        try:
            host_load_1m = float(os.getloadavg()[0])
        except (AttributeError, OSError):
            host_load_1m = None
        available_bytes = _mem_available_bytes()
        warning = available_bytes < self.warning_mem_bytes
        low_memory_for_s = 0.0
        reason: str | None = None
        with self._lock:
            self._samples += 1
            self._peak_rss_bytes = max(self._peak_rss_bytes, rss_bytes)
            self._total_cpu_seconds += cpu_seconds
            self._peak_cpu_cores = max(self._peak_cpu_cores, cpu_cores)
            if host_cpu_busy_fraction is not None:
                self._host_cpu_busy_sum += host_cpu_busy_fraction
                self._host_cpu_busy_samples += 1
                self._host_cpu_busy_max = (host_cpu_busy_fraction if self._host_cpu_busy_max is None
                                           else max(self._host_cpu_busy_max, host_cpu_busy_fraction))
            if host_load_1m is not None:
                self._host_load_1m_max = (host_load_1m if self._host_load_1m_max is None
                                          else max(self._host_load_1m_max, host_load_1m))
            for process in cpu_seconds_by_process:
                name = process["name"]
                self._cpu_seconds_by_name[name] = (
                    self._cpu_seconds_by_name.get(name, 0.0) + process["cpu_seconds"])
            self._min_available_bytes = (available_bytes if self._min_available_bytes is None
                                         else min(self._min_available_bytes, available_bytes))
            self._warning_samples += int(warning)
            if rss_bytes > self.rss_cap_bytes:
                self._rss_stop_samples += 1
                reason = "process_tree_rss_cap_exceeded"
            if available_bytes < self.stop_mem_bytes:
                if self._low_memory_since is None:
                    self._low_memory_since = now
                low_memory_for_s = now - self._low_memory_since
                if low_memory_for_s >= self.stop_mem_grace_s and reason is None:
                    reason = "mem_available_below_stop_threshold"
            else:
                self._low_memory_since = None
            if reason is not None and self._stop_reason is None:
                self._stop_reason = reason
        row = {
            "schema": "frontier-resource-sample/v1",
            "host_monotonic_s": now,
            "clock_domain": "python_time.monotonic",
            "root_pid": self.root_pid,
            "process_tree_pids": pids,
            "process_tree_rss_bytes": rss_bytes,
            "process_tree_rss_gib": rss_bytes / _GIB,
            "process_tree_cpu_seconds_delta": cpu_seconds,
            "process_tree_cpu_cores": cpu_cores,
            "host_cpu_busy_fraction": host_cpu_busy_fraction,
            "host_cpu_count": os.cpu_count(),
            "host_load_1m": host_load_1m,
            "process_cpu_seconds_delta_by_process": sorted(
                cpu_seconds_by_process, key=lambda row: row["cpu_seconds"], reverse=True),
            "rss_cap_bytes": self.rss_cap_bytes,
            "mem_available_bytes": available_bytes,
            "mem_available_gib": available_bytes / _GIB,
            "warning": warning,
            "low_memory_for_s": low_memory_for_s,
            "stop_reason": self.stop_reason,
        }
        stream = self._stream
        if stream is not None:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()

    def _set_stop_reason(self, reason: str) -> None:
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason


def _process_tree_sample(root_pid: int) -> tuple[list[int], int, dict[int, dict[str, int | str]]]:
    table: dict[int, tuple[int, int, str, int]] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii", errors="ignore")
            ppid = rss_kib = None
            for line in status.splitlines():
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                elif line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                if ppid is not None and rss_kib is not None:
                    break
            if ppid is not None and rss_kib is not None:
                stat = (entry / "stat").read_text(encoding="ascii", errors="ignore")
                stat_fields = stat[stat.rfind(")") + 2:].split()
                if len(stat_fields) < 13:
                    continue
                user_ticks, system_ticks = int(stat_fields[11]), int(stat_fields[12])
                try:
                    comm = (entry / "comm").read_text(encoding="ascii", errors="ignore").strip()
                except (FileNotFoundError, PermissionError):
                    comm = "unknown"
                name = comm
                try:
                    executable = Path(os.readlink(entry / "exe")).name.removesuffix(" (deleted)")
                    if executable:
                        name = executable
                except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                    pass
                try:
                    arguments = (entry / "cmdline").read_bytes().split(b"\0")
                    script = next((Path(value.decode("utf-8", errors="ignore")).name
                                   for value in arguments if value.lower().endswith(b".py")), None)
                except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                    script = None
                if script:
                    name = f"{name}:{script}"
                table[int(entry.name)] = (ppid, rss_kib * 1024, name,
                                         user_ticks + system_ticks)
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _rss, _name, _ticks) in table.items():
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    rss = sum(table.get(pid, (0, 0, "unknown", 0))[1] for pid in descendants)
    processes = {
        pid: {"name": table[pid][2], "cpu_ticks": table[pid][3]}
        for pid in descendants if pid in table
    }
    return sorted(descendants), rss, processes


def _mem_available_bytes() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("/proc/meminfo does not expose MemAvailable")


def _host_cpu_ticks() -> tuple[int, int] | None:
    """Return aggregate Linux CPU total and idle ticks for interval utilization."""
    try:
        contents = Path("/proc/stat").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        return None
    return _parse_host_cpu_ticks(contents)


def _parse_host_cpu_ticks(contents: str) -> tuple[int, int] | None:
    lines = contents.splitlines()
    if not lines:
        return None
    fields = lines[0].split()
    if not fields or fields[0] != "cpu" or len(fields) < 5:
        return None
    try:
        # Linux reports guest/guest_nice after steal; exclude them because
        # Linux already includes those values in user/nice.
        ticks = [int(value) for value in fields[1:9]]
    except ValueError:
        return None
    if len(ticks) < 4:
        return None
    idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
    return sum(ticks), idle
