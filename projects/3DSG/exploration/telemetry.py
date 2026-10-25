"""Append-only run event journal with host monotonic provenance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping


class EventJournal:
    """Serialize causal events to JSONL; never rewrite earlier run evidence."""

    def __init__(self, run_root: str | Path, *, run_id: str,
                 fsync_each_event: bool = True) -> None:
        if not run_id or any(ch in run_id for ch in "/\\\0"):
            raise ValueError("run_id must be a non-empty path-safe token")
        self.root = Path(run_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "events.jsonl"
        self.run_id = run_id
        self.fsync_each_event = fsync_each_event
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        # A run is immutable: reopening an existing journal would duplicate
        # event sequence numbers and rewrite a failed run's history.
        self._stream = self.path.open("xb", buffering=0)

    def emit(self, event: str, fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not event:
            raise ValueError("event name is required")
        with self._lock:
            if self._closed:
                raise RuntimeError("event journal is closed")
            record = {
                "schema": "frontier-event/v1",
                "run_id": self.run_id,
                "event_seq": self._sequence,
                "event": event,
                "host_monotonic_s": time.monotonic(),
                "host_monotonic_clock": "python_time.monotonic",
            }
            if fields:
                overlap = set(record).intersection(fields)
                if overlap:
                    raise ValueError(f"event fields may not overwrite journal fields: {sorted(overlap)}")
                record.update(fields)
            payload = (json.dumps(record, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False) + "\n").encode("utf-8")
            self._stream.write(payload)
            if self.fsync_each_event:
                os.fsync(self._stream.fileno())
            self._sequence += 1
            return record

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._closed = True

    def __enter__(self) -> "EventJournal":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
