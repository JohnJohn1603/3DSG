"""Timeout-bounded client for the external persistent ONNX P2 worker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time
from typing import Any

import numpy as np

from .process_priority import apply_process_nice, graph_worker_nice_from_env


class P2WorkerError(RuntimeError):
    pass


class P2WorkerClient:
    """Own exactly one long-lived detector process and chronological request stream."""

    def __init__(self, *, python: str | Path, worker_script: str | Path,
                 legacy_root: str | Path, weights: str | Path,
                 profile: str | Path, names: str | Path, output_root: str | Path,
                 stderr_path: str | Path, intra_op_threads: int = 2,
                 inter_op_threads: int = 1, startup_timeout_s: float = 60.0,
                 frame_timeout_s: float = 20.0,
                 nice_value: int | None = None) -> None:
        if startup_timeout_s <= 0 or frame_timeout_s <= 0:
            raise ValueError("P2 worker timeouts must be positive")
        # Keep venv launcher symlinks intact: resolving ``.venv/bin/python`` to
        # its base interpreter bypasses the venv's site-packages entirely.
        python_path = Path(python).expanduser()
        if not python_path.is_file():
            raise FileNotFoundError(f"P2 Python executable is missing: {python_path}")
        self.python = str(python_path.absolute())
        self.worker_script = str(Path(worker_script).resolve(strict=True))
        self.legacy_root = str(Path(legacy_root).resolve(strict=True))
        self.weights = str(Path(weights).resolve(strict=True))
        self.profile = str(Path(profile).resolve(strict=True))
        self.names = str(Path(names).resolve(strict=True))
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        stderr = Path(stderr_path).resolve()
        stderr.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_stream = stderr.open("ab", buffering=0)
        self.frame_timeout_s = float(frame_timeout_s)
        if type(intra_op_threads) is not int or not 1 <= intra_op_threads <= 8:
            raise ValueError("P2 intra-op thread count must be an integer in [1, 8]")
        if type(inter_op_threads) is not int or not 1 <= inter_op_threads <= 4:
            raise ValueError("P2 inter-op thread count must be an integer in [1, 4]")
        self.intra_op_threads = intra_op_threads
        self.inter_op_threads = inter_op_threads
        self._lock = threading.Lock()
        self._closed = False
        command = [self.python, "-B", self.worker_script, self.legacy_root,
                   self.weights, self.profile, self.names, str(self.output_root),
                   str(self.intra_op_threads), str(self.inter_op_threads)]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr_stream, text=True, encoding="utf-8", bufsize=1,
        )
        try:
            selected_nice = graph_worker_nice_from_env() if nice_value is None else nice_value
            apply_process_nice(self.process, selected_nice)
            ready = self._read_message(startup_timeout_s)
            if ready.get("event") != "ready" or ready.get("status") != "ready":
                raise P2WorkerError(f"P2 worker did not become ready: {ready}")
            self.provenance = ready
        except BaseException:
            self.close(timeout_s=1.0)
            raise

    def update_frame(self, *, frame_index: int, frame_id: str,
                     timestamp_s: float, rgb_file: str | Path) -> dict[str, Any]:
        with self._lock:
            if self._closed or self.process.poll() is not None:
                raise P2WorkerError("P2 worker is not running; replay_required")
            message = {"op": "frame", "frame_index": int(frame_index),
                       "frame_id": str(frame_id), "timestamp_s": float(timestamp_s),
                       "rgb_file": str(Path(rgb_file).resolve(strict=True))}
            self._write_message(message)
            result = self._read_message(self.frame_timeout_s)
            if (result.get("event") != "frame" or result.get("status") != "ok"
                    or result.get("frame_id") != frame_id
                    or int(result.get("frame_index", -1)) != int(frame_index)):
                raise P2WorkerError(f"P2 frame failed; replay_required: {result}")
            self._validate_detections(result, frame_id=frame_id)
            return result

    def close(self, *, timeout_s: float = 5.0,
              deadline_monotonic_s: float | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        process = getattr(self, "process", None)
        deadline = (time.monotonic() + max(0.0, float(timeout_s))
                    if deadline_monotonic_s is None else float(deadline_monotonic_s))

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        try:
            if process is not None and process.poll() is None:
                try:
                    self._write_message({"op": "shutdown"})
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=remaining())
                except subprocess.TimeoutExpired:
                    process.terminate()
                    grace = min(1.0, remaining())
                    if grace > 0:
                        try:
                            process.wait(timeout=grace)
                        except subprocess.TimeoutExpired:
                            pass
                    if process.poll() is None:
                        process.kill()
                        final_wait = remaining()
                        if final_wait > 0:
                            try:
                                process.wait(timeout=final_wait)
                            except subprocess.TimeoutExpired:
                                pass
        finally:
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
            self._stderr_stream.close()

    def _write_message(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n")
        self.process.stdin.flush()

    def _read_message(self, timeout_s: float) -> dict:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout_s)
        if not ready:
            raise TimeoutError(f"P2 worker response exceeded {timeout_s:.1f}s")
        line = self.process.stdout.readline()
        if not line:
            raise P2WorkerError(f"P2 worker exited with code {self.process.poll()}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise P2WorkerError(f"P2 worker returned invalid JSON: {line[:200]!r}") from exc
        if not isinstance(value, dict):
            raise P2WorkerError("P2 worker response must be a JSON object")
        return value

    @staticmethod
    def _validate_detections(result: dict[str, Any], *, frame_id: str) -> None:
        seen = set()
        for row in result.get("detections", []):
            detection_id = str(row.get("detection_id", ""))
            mask_path = Path(str(row.get("mask_file", "")))
            if not detection_id or detection_id in seen:
                raise P2WorkerError("P2 detection IDs must be present and unique")
            seen.add(detection_id)
            if not detection_id.startswith(frame_id + ":"):
                raise P2WorkerError("P2 detection ID does not belong to the source frame")
            if not mask_path.is_file():
                raise P2WorkerError(f"P2 worker mask is missing: {mask_path}")
            mask = np.load(mask_path, allow_pickle=False, mmap_mode="r")
            if mask.ndim != 2 or mask.dtype != np.bool_:
                raise P2WorkerError(f"P2 mask has invalid shape/dtype: {mask_path}")
            if int(mask.sum()) != int(row.get("mask_pixels", -1)):
                raise P2WorkerError(f"P2 mask pixel count mismatch: {mask_path}")
