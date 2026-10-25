"""Atomic, append-only publication primitives for the D1 cached live runner."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace a file only after its complete contents have reached disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(Path(path), json.dumps(value, sort_keys=True, indent=2,
                                              ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")


def append_jsonl(path: Path, event: dict[str, Any]) -> int:
    """Append one complete JSON record; return the byte offset after its newline."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(event) + b"\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        written = 0
        while written < len(line):
            written += os.write(fd, line[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    return path.stat().st_size


def read_complete_jsonl(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read newline-terminated records only; leave a partial tail for the next poll."""
    path = Path(path)
    if not path.is_file():
        return [], offset
    with path.open("rb") as stream:
        stream.seek(offset)
        payload = stream.read()
    rows: list[dict[str, Any]] = []
    consumed = 0
    for line in payload.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        body = line[:-1]
        if body:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL event must be an object: {path}")
            rows.append(value)
        consumed += len(line)
    return rows, offset + consumed


class EventLogReader:
    """Incremental reader with idempotent exact duplicates and strict ordering."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0
        self.last_seq = 0
        self._seen: dict[int, str] = {}

    def poll(self) -> list[dict[str, Any]]:
        rows, offset = read_complete_jsonl(self.path, self.offset)
        accepted: list[dict[str, Any]] = []
        last = self.last_seq
        seen = dict(self._seen)
        for row in rows:
            if type(row.get("event_seq")) is not int or row["event_seq"] < 1:
                raise ValueError(f"invalid event_seq in {self.path}")
            seq = row["event_seq"]
            fingerprint = hashlib.sha256(canonical_json(row)).hexdigest()
            if seq in seen:
                if seen[seq] != fingerprint:
                    raise ValueError(f"conflicting duplicate event_seq={seq} in {self.path}")
                continue
            if seq != last + 1:
                raise ValueError(f"out-of-order event sequence in {self.path}: expected {last + 1}, got {seq}")
            seen[seq] = fingerprint
            last = seq
            accepted.append(row)
        self.offset = offset
        self.last_seq = last
        self._seen = seen
        return accepted


class AcknowledgedPublisher:
    """Publish one immutable revision at a time and wait for coordinator ack.

    The browser is not part of this protocol. The coordinator acknowledges only
    after it has durably written the matching joined revision and event.
    """

    def __init__(self, run_root: Path, branch: str, *, timeout_s: float = 300.0):
        self.run_root = Path(run_root).resolve()
        self.branch = branch
        self.branch_root = self.run_root / "branches" / branch
        self.events_path = self.branch_root / "events.jsonl"
        self.acks = self.branch_root / "acks"
        self.acks.mkdir(parents=True, exist_ok=True)
        self.timeout_s = float(timeout_s)
        self.event_seq = 0
        self.last_timing: dict[str, float] = {}

    def publish(self, event: dict[str, Any], *, state_path: Path,
                referenced_files: dict[str, str] | None = None) -> dict[str, Any]:
        state_path = Path(state_path).resolve()
        if not state_path.is_file() or not state_path.is_relative_to(self.run_root):
            raise ValueError("published state must be a complete file inside the new D run")
        refs = dict(referenced_files or {})
        refs[state_path.relative_to(self.run_root).as_posix()] = sha256_file(state_path)
        for rel, expected in refs.items():
            ref_path = (self.run_root / rel).resolve()
            if not ref_path.is_relative_to(self.run_root) or not ref_path.is_file():
                raise FileNotFoundError(f"revision asset is missing/outside run: {rel}")
            actual = sha256_file(ref_path)
            if actual != expected:
                raise ValueError(f"revision asset hash mismatch: {rel}")
        durable_started = time.perf_counter()
        self.event_seq += 1
        row = {**event, "event_seq": self.event_seq, "branch": self.branch,
               "state_path": state_path.relative_to(self.run_root).as_posix(),
               "state_sha256": refs[state_path.relative_to(self.run_root).as_posix()],
               "asset_sha256": refs}
        row["event_sha256"] = hashlib.sha256(canonical_json(row)).hexdigest()
        append_jsonl(self.events_path, row)
        publication_ms = (time.perf_counter() - durable_started) * 1000.0
        atomic_write_json(self.branch_root / "progress.json", {
            "status": "waiting_for_barrier", "branch": self.branch,
            "event_seq": self.event_seq, "frame_index": row.get("source_frame_index"),
            "frame_id": row.get("source_frame_id"), "event_sha256": row["event_sha256"],
            "updated_unix_s": time.time(),
        })
        ack_path = self.acks / f"{self.event_seq:06d}.json"
        stop_path = self.run_root / "control" / "stop.json"
        barrier_started = time.perf_counter()
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if stop_path.is_file():
                raise InterruptedError(f"D run stop requested while {self.branch} waited at event {self.event_seq}")
            if ack_path.is_file():
                ack = json.loads(ack_path.read_text(encoding="utf-8"))
                if ack.get("event_sha256") != row["event_sha256"]:
                    raise ValueError(f"coordinator acknowledgement mismatch for {self.branch} event {self.event_seq}")
                atomic_write_json(self.branch_root / "progress.json", {
                    "status": "processed", "branch": self.branch,
                    "event_seq": self.event_seq, "frame_index": row.get("source_frame_index"),
                    "frame_id": row.get("source_frame_id"), "event_sha256": row["event_sha256"],
                    "updated_unix_s": time.time(),
                })
                barrier_wait_ms = (time.perf_counter() - barrier_started) * 1000.0
                self.last_timing = {"publication_ms": publication_ms,
                                    "barrier_wait_ms": barrier_wait_ms}
                append_jsonl(self.branch_root / "timings.jsonl", {
                    "event_seq": self.event_seq,
                    "source_frame_index": row.get("source_frame_index"),
                    "source_frame_id": row.get("source_frame_id"),
                    **self.last_timing,
                })
                return row
            time.sleep(0.05)
        raise TimeoutError(f"{self.branch} revision barrier timed out at frame {row.get('source_frame_id')}")
