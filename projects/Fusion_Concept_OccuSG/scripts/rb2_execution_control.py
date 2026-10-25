"""Model-agnostic execution controls for the RB2 Kaggle scorer.

This module does not score or load a model. It provides bounded child-process
execution, flushed log streaming, run-state records, and hash-bound category
checkpoints for a future scorer cell.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class CheckpointMismatch(ValueError):
    """Raised when a checkpoint cannot safely be reused for this run."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_sha256(value: Any) -> str:
    """Hash JSON-compatible inputs independent of mapping insertion order."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    temp.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                               allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


def write_run_state(run_dir: Path, *, run_hash: str, status: str, stage: str,
                    details: Mapping[str, Any] | None = None) -> None:
    if status not in {"started", "completed", "failed"}:
        raise ValueError(f"Unsupported run status: {status}")
    _atomic_json(Path(run_dir) / "run_state.json", {
        "schema": "rb2-execution-state/v1",
        "run_hash": run_hash,
        "status": status,
        "stage": stage,
        "updated_utc": utc_now(),
        "details": dict(details or {}),
    })


def _category_file_key(category: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", category).strip("._-") or "category"
    suffix = hashlib.sha256(category.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:64]}-{suffix}.json"


def save_category_checkpoint(run_dir: Path, *, run_hash: str, category: str,
                             payload: Mapping[str, Any]) -> Path:
    """Atomically save one complete category result bound to an exact run hash."""
    payload_doc = dict(payload)
    payload_hash = canonical_sha256(payload_doc)
    path = Path(run_dir) / "checkpoints" / _category_file_key(category)
    _atomic_json(path, {
        "schema": "rb2-category-checkpoint/v1",
        "run_hash": run_hash,
        "category": category,
        "status": "completed",
        "payload_sha256": payload_hash,
        "payload": payload_doc,
        "completed_utc": utc_now(),
    })
    return path


def load_category_checkpoint(run_dir: Path, *, run_hash: str,
                             category: str) -> dict[str, Any] | None:
    """Return a verified completed result; fail closed on stale/corrupt files."""
    path = Path(run_dir) / "checkpoints" / _category_file_key(category)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointMismatch(f"Unreadable checkpoint {path}: {exc}") from exc
    if envelope.get("schema") != "rb2-category-checkpoint/v1":
        raise CheckpointMismatch(f"Unsupported checkpoint schema: {path}")
    if envelope.get("status") != "completed" or envelope.get("run_hash") != run_hash:
        raise CheckpointMismatch(f"Checkpoint is incomplete or belongs to another run: {path}")
    if envelope.get("category") != category:
        raise CheckpointMismatch(f"Checkpoint category mismatch: {path}")
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or canonical_sha256(payload) != envelope.get("payload_sha256"):
        raise CheckpointMismatch(f"Checkpoint payload hash mismatch: {path}")
    return payload


def _append_progress(run_dir: Path, event: Mapping[str, Any],
                     on_progress: Callable[[str], None] | None) -> None:
    line = json.dumps(dict(event), sort_keys=True, ensure_ascii=False,
                      allow_nan=False)
    path = Path(run_dir) / "progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", buffering=1) as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if on_progress:
        on_progress(line)


def run_category_units(run_dir: Path, *, run_hash: str,
                       categories: Sequence[str],
                       score_category: Callable[[str], Mapping[str, Any]],
                       on_progress: Callable[[str], None] | None = None
                       ) -> dict[str, dict[str, Any]]:
    """Run/resume category units with durable per-category status and outputs.

    ``score_category`` is expected to reuse one already-loaded model and to
    return only after all rows for that category have been validated. A
    completed checkpoint is reused only when both run and payload hashes
    verify. Model loading and inference remain the caller's responsibility.
    """
    categories = list(categories)
    if not categories or any(not isinstance(name, str) or not name for name in categories):
        raise ValueError("categories must be a non-empty sequence of non-empty strings")
    if len(set(categories)) != len(categories):
        raise ValueError("category names must be unique")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    total = len(categories)
    results: dict[str, dict[str, Any]] = {}
    write_run_state(run_dir, run_hash=run_hash, status="started", stage="categories",
                    details={"category_count": total})
    try:
        for index, category in enumerate(categories, start=1):
            started_at = time.monotonic()
            checkpoint = load_category_checkpoint(run_dir, run_hash=run_hash,
                                                   category=category)
            if checkpoint is not None:
                results[category] = checkpoint
                _append_progress(run_dir, {
                    "event": "category_resumed", "category": category,
                    "index": index, "total": total, "run_hash": run_hash,
                    "elapsed_seconds": time.monotonic() - started_at,
                }, on_progress)
                continue

            _append_progress(run_dir, {
                "event": "category_started", "category": category,
                "index": index, "total": total, "run_hash": run_hash,
            }, on_progress)
            write_run_state(run_dir, run_hash=run_hash, status="started",
                            stage="category_started",
                            details={"category": category, "index": index, "total": total})
            try:
                payload = dict(score_category(category))
                # Validate the exact serialization contract before checkpointing.
                canonical_sha256(payload)
                save_category_checkpoint(run_dir, run_hash=run_hash,
                                         category=category, payload=payload)
            except BaseException as exc:
                elapsed = time.monotonic() - started_at
                _append_progress(run_dir, {
                    "event": "category_failed", "category": category,
                    "index": index, "total": total, "run_hash": run_hash,
                    "elapsed_seconds": elapsed,
                    "error_type": type(exc).__name__, "error": str(exc),
                }, on_progress)
                write_run_state(run_dir, run_hash=run_hash, status="failed",
                                stage="category_failed",
                                details={"category": category, "index": index,
                                         "total": total, "elapsed_seconds": elapsed,
                                         "error_type": type(exc).__name__, "error": str(exc)})
                raise
            elapsed = time.monotonic() - started_at
            results[category] = payload
            _append_progress(run_dir, {
                "event": "category_completed", "category": category,
                "index": index, "total": total, "run_hash": run_hash,
                "elapsed_seconds": elapsed,
            }, on_progress)
            write_run_state(run_dir, run_hash=run_hash, status="started",
                            stage="category_completed",
                            details={"category": category, "index": index,
                                     "total": total, "elapsed_seconds": elapsed})
    except BaseException:
        state_path = run_dir / "run_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state.get("status") == "started":
            write_run_state(run_dir, run_hash=run_hash, status="failed",
                            stage="category_runner_failed",
                            details={"error_type": "CategoryRunnerError"})
        raise
    write_run_state(run_dir, run_hash=run_hash, status="completed",
                    stage="categories_completed", details={"category_count": total})
    return results


def _signal_process_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _terminate_process_group(proc: subprocess.Popen[bytes], grace_seconds: float) -> None:
    """Terminate the child session, then kill any remaining process-group members."""
    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=max(0.0, grace_seconds))
    except subprocess.TimeoutExpired:
        pass
    # Kill the process group even if its leader exited; descendants may still
    # hold inherited stdout/stderr open.
    _signal_process_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - OS-level last resort
        proc.kill()
        proc.wait()


def run_streamed_process(command: Sequence[str], *, run_dir: Path, run_hash: str,
                         timeout_seconds: float, cwd: Path | None = None,
                         env: Mapping[str, str] | None = None,
                         terminate_grace_seconds: float = 2.0,
                         on_progress: Callable[[str], None] | None = None) -> int:
    """Run one child with live logs and a real process-group timeout.

    The child is placed in a new session/process group. Combined stdout and
    stderr are appended to ``process.log`` and flushed as bytes arrive. Python
    children should still be invoked with ``-u`` (or PYTHONUNBUFFERED=1).
    """
    if not command or timeout_seconds <= 0 or terminate_grace_seconds < 0:
        raise ValueError("command and positive timeout are required")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "process.log"
    write_run_state(run_dir, run_hash=run_hash, status="started", stage="child_process",
                    details={"command": list(command), "timeout_seconds": timeout_seconds})
    started = time.monotonic()
    decoder = __import__("codecs").getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    proc: subprocess.Popen[bytes] | None = None
    try:
        child_env = dict(os.environ if env is None else env)
        child_env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(list(command), cwd=str(cwd) if cwd else None, env=child_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=0, start_new_session=True)
        assert proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        with log_path.open("ab", buffering=0) as log_file:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= timeout_seconds:
                    _terminate_process_group(proc, terminate_grace_seconds)
                    write_run_state(run_dir, run_hash=run_hash, status="failed", stage="timeout",
                                    details={"elapsed_seconds": elapsed, "pid": proc.pid})
                    raise TimeoutError(f"Child timed out after {elapsed:.3f}s; process group terminated")
                events = selector.select(min(0.2, timeout_seconds - elapsed))
                for key, _ in events:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    log_file.write(chunk)
                    decoded = decoder.decode(chunk)
                    pending += decoded
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        if on_progress:
                            on_progress(line)
                if proc.poll() is not None and not selector.get_map():
                    break
            if pending:
                if on_progress:
                    on_progress(pending)
        rc = proc.wait()
        elapsed = time.monotonic() - started
        if rc != 0:
            write_run_state(run_dir, run_hash=run_hash, status="failed", stage="child_exit",
                            details={"returncode": rc, "elapsed_seconds": elapsed, "pid": proc.pid})
            raise subprocess.CalledProcessError(rc, list(command))
        write_run_state(run_dir, run_hash=run_hash, status="completed", stage="child_exit",
                        details={"returncode": rc, "elapsed_seconds": elapsed, "pid": proc.pid})
        return rc
    except BaseException as exc:
        if proc is not None and proc.poll() is None:
            _terminate_process_group(proc, terminate_grace_seconds)
        state_path = run_dir / "run_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state.get("status") == "started":
            write_run_state(run_dir, run_hash=run_hash, status="failed", stage="runner_error",
                            details={"error_type": type(exc).__name__, "error": str(exc)})
        raise
    finally:
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
