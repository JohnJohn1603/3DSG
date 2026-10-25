"""Timeout-bounded client for the persistent native P3/OpenCLIP worker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time
from typing import Any

from .process_priority import apply_process_nice, graph_worker_nice_from_env


class NativePerceptionWorkerError(RuntimeError):
    """The P3/OpenCLIP process failed or broke frame-stream invariants."""


class NativePerceptionWorkerClient:
    """Own one appearance-environment process and serialize frame requests."""

    def __init__(self, *, python: str | Path, worker_script: str | Path,
                 conceptgraphs_root: str | Path, occusg_root: str | Path,
                 output_root: str | Path, p3_profile: str | Path,
                 feature_contract: str | Path, checkpoint: str | Path,
                 stderr_path: str | Path, startup_timeout_s: float = 180.0,
                 frame_timeout_s: float = 180.0,
                 nice_value: int | None = None) -> None:
        if startup_timeout_s <= 0 or frame_timeout_s <= 0:
            raise ValueError("native perception worker timeouts must be positive")
        python_path = Path(python).expanduser()
        if not python_path.is_file():
            raise FileNotFoundError(f"appearance Python executable is missing: {python_path}")
        self.python = str(python_path.absolute())
        self.worker_script = str(Path(worker_script).resolve(strict=True))
        self.conceptgraphs_root = Path(conceptgraphs_root).resolve(strict=True)
        self.occusg_root = Path(occusg_root).resolve(strict=True)
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.ready_file = self.output_root / "native_perception" / "ready.json"
        self.ready_file.parent.mkdir(parents=True, exist_ok=True)
        self.ready_file.unlink(missing_ok=True)
        self.frame_timeout_s = float(frame_timeout_s)
        self._lock = threading.Lock()
        self._closed = False
        self._last_frame_index = -1
        stderr = Path(stderr_path).resolve()
        stderr.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_stream = stderr.open("ab", buffering=0)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((
            str(self.conceptgraphs_root), str(self.conceptgraphs_root / "src"),
            str(self.conceptgraphs_root / "ref_ConceptGraph/concept-graphs"),
            str(self.occusg_root / "src"), str(Path(self.worker_script).parents[1]),
            env.get("PYTHONPATH", ""),
        ))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["MPLCONFIGDIR"] = str(self.output_root / "cache/matplotlib-native-perception")
        command = [self.python, "-B", self.worker_script,
                   "--conceptgraphs-root", str(self.conceptgraphs_root),
                   "--occusg-root", str(self.occusg_root),
                   "--output", str(self.output_root),
                   "--ready-file", str(self.ready_file),
                   "--p3-profile", str(Path(p3_profile).resolve(strict=True)),
                   "--feature-contract", str(Path(feature_contract).resolve(strict=True)),
                   "--checkpoint", str(Path(checkpoint).resolve(strict=True))]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=self._stderr_stream, text=True, encoding="utf-8",
                                       bufsize=1, env=env)
        try:
            selected_nice = graph_worker_nice_from_env() if nice_value is None else nice_value
            apply_process_nice(self.process, selected_nice)
            ready = self._read_message(startup_timeout_s)
            if ready.get("status") != "ready":
                raise NativePerceptionWorkerError(f"native perception worker failed startup: {ready}")
            if not self.ready_file.is_file():
                raise NativePerceptionWorkerError("native perception worker omitted its ready file")
            ready_file = json.loads(self.ready_file.read_text(encoding="utf-8"))
            if ready_file.get("status") != "ready" or ready_file.get("pid") != self.process.pid:
                raise NativePerceptionWorkerError("native perception ready-file ownership mismatch")
            self.provenance = ready_file
        except BaseException:
            self.close(timeout_s=2.0, finalize=False)
            raise

    def process_frame(self, packet_path: str | Path, *, frame_id: str,
                      frame_index: int, timeout_s: float | None = None) -> dict[str, Any]:
        packet = Path(packet_path).resolve(strict=True)
        if not frame_id or type(frame_index) is not int or frame_index < 0:
            raise ValueError("native perception frame identity is invalid")
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("frame timeout must be positive")
        with self._lock:
            if self._closed or self.process.poll() is not None:
                raise NativePerceptionWorkerError("native perception worker is not running; replay_required")
            if frame_index <= self._last_frame_index:
                raise NativePerceptionWorkerError("native perception frames must be strictly increasing")
            request_id = f"{frame_index}:{frame_id}"
            try:
                self._write_message({"op": "frame", "request_id": request_id,
                                     "packet_path": str(packet),
                                     "packet_sha256": _sha256_file(packet)})
                response = self._read_message(self.frame_timeout_s if timeout_s is None else timeout_s)
                if response.get("status") == "error":
                    raise NativePerceptionWorkerError(
                        f"native P3/OpenCLIP failed at {frame_id}: {response.get('error_type')}: "
                        f"{response.get('error')}\n{response.get('traceback', '')}")
                if (response.get("status") != "frame_processed"
                        or response.get("request_id") != request_id
                        or response.get("frame_id") != frame_id
                        or int(response.get("frame_index", -1)) != frame_index):
                    raise NativePerceptionWorkerError(
                        f"native perception identity mismatch: {response}")
                for path_key, hash_key in (("p3_path", "p3_sha256"),
                                           ("feature_path", "feature_sha256")):
                    artifact = Path(str(response.get(path_key, ""))).resolve(strict=True)
                    if not artifact.is_relative_to(self.output_root):
                        raise NativePerceptionWorkerError(
                            f"native artifact escaped the run output directory: {artifact}")
                    if _sha256_file(artifact) != response.get(hash_key):
                        raise NativePerceptionWorkerError(
                            f"native perception artifact hash mismatch: {artifact}")
                    document = json.loads(artifact.read_text(encoding="utf-8"))
                    if (document.get("frame_id") != frame_id
                            or int(document.get("frame_index", -1)) != frame_index):
                        raise NativePerceptionWorkerError(
                            f"native artifact identity mismatch: {artifact}")
                self._last_frame_index = frame_index
                return response
            except BaseException:
                # Once a request is sent, a timeout or malformed reply makes
                # stream alignment uncertain. Poison the process so a late
                # response can never be mistaken for the next frame's result.
                self._terminate_locked()
                raise

    def close(self, *, timeout_s: float = 30.0, finalize: bool = True,
              deadline_monotonic_s: float | None = None) -> dict[str, Any] | None:
        if timeout_s <= 0:
            raise ValueError("shutdown timeout must be positive")
        deadline = (time.monotonic() + timeout_s if deadline_monotonic_s is None
                    else float(deadline_monotonic_s))

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        if not self._lock.acquire(timeout=remaining()):
            self._terminate_locked(deadline)
            raise TimeoutError("native perception worker lock exceeded shutdown deadline")
        try:
            if self._closed:
                return None
            self._closed = True
            response = None
            process = self.process
            try:
                if process.poll() is None:
                    operation = "finalize" if finalize else "close"
                    if remaining() > 0.0:
                        try:
                            self._write_message({"op": operation})
                            timeout = remaining()
                            if timeout > 0.0:
                                response = self._read_message(timeout)
                        except (BrokenPipeError, OSError, TimeoutError, NativePerceptionWorkerError):
                            pass
                    try:
                        process.wait(timeout=remaining())
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        if remaining() > 0.0:
                            try:
                                process.wait(timeout=remaining())
                            except subprocess.TimeoutExpired:
                                pass
                        if process.poll() is None:
                            process.kill()
                            try:
                                process.wait(timeout=max(0.001, remaining()))
                            except subprocess.TimeoutExpired:
                                pass
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
                self._stderr_stream.close()
            if process.returncode not in (0, None):
                raise NativePerceptionWorkerError(
                    f"native perception worker exited with code {process.returncode}")
            if response is not None and response.get("status") not in {"finalized", "closed"}:
                raise NativePerceptionWorkerError(f"native perception shutdown failed: {response}")
            return response
        finally:
            self._lock.release()

    def abort(self, *, deadline_monotonic_s: float | None = None) -> None:
        """Terminate an in-flight worker without waiting for its request lock."""
        self._terminate_locked(deadline_monotonic_s)

    def _terminate_locked(self, deadline_monotonic_s: float | None = None) -> None:
        self._closed = True
        process = self.process

        def remaining() -> float:
            return (2.0 if deadline_monotonic_s is None else
                    max(0.0, deadline_monotonic_s - time.monotonic()))

        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            timeout = remaining()
            if timeout > 0.0:
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=max(0.001, remaining()))
                except subprocess.TimeoutExpired:
                    pass
        for stream_name in ("stdin", "stdout"):
            stream = getattr(process, stream_name, None)
            if stream is not None and not stream.closed:
                stream.close()
        if not self._stderr_stream.closed:
            self._stderr_stream.close()

    def _write_message(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":"),
                                   allow_nan=False) + "\n")
        self.process.stdin.flush()

    def _read_message(self, timeout_s: float) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [],
                                        max(0.0, deadline - time.monotonic()))
            if not ready:
                raise TimeoutError(f"native perception response exceeded {timeout_s:.1f}s")
            line = self.process.stdout.readline()
            if not line:
                raise NativePerceptionWorkerError(
                    f"native perception worker exited with code {self.process.poll()}")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise NativePerceptionWorkerError(
                    f"native perception worker emitted invalid protocol JSON: {line[:200]!r}") from exc
            if not isinstance(value, dict):
                raise NativePerceptionWorkerError("native perception response must be a JSON object")
            return value
        raise TimeoutError(f"native perception response exceeded {timeout_s:.1f}s")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
