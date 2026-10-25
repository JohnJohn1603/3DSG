"""Validated Python boundary for the persistent pinned C++ Incremental DuDe worker."""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from typing import Any

import numpy as np

from ..contracts.errors import ContractError
from ..contracts.regions import (
    CanonicalRegion, GuardDecision, Polygon2D, RegionObservation, RegionUpdateResult,
    TrackedRegion,
)
from ..mapping.projection import FreeSpaceGrid2D
from .pipeline import RegionUpdateProcessor


@dataclass(frozen=True, slots=True)
class DuDeConfig:
    decomp_threshold_m: float = 1.5
    safety_distance_m: float = 0.5
    timeout_s: float = 30.0
    preprocessing_policy: str = "source"

    def __post_init__(self) -> None:
        for name in ("decomp_threshold_m", "safety_distance_m", "timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                raise ContractError(f"{name} must be a finite real number")
        if self.decomp_threshold_m <= 0 or self.safety_distance_m < 0 or self.timeout_s <= 0:
            raise ContractError("DuDe threshold/timeout must be positive and safety distance nonnegative")
        if not isinstance(self.preprocessing_policy, str) or self.preprocessing_policy not in {"source", "extended_unknown_boundary"}:
            raise ContractError("preprocessing_policy must be source or extended_unknown_boundary")


@dataclass(frozen=True, slots=True)
class DuDeUpdateResult:
    """Adapter outcome with request and accepted-observation revisions distinct."""

    backend_status: str
    backend_branch: str
    request_revision: int
    accepted_observation_revision: int | None
    stale: bool
    current_regions: tuple[CanonicalRegion, ...]
    retained_tracks: tuple[TrackedRegion, ...]
    m5_result: RegionUpdateResult | None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.m5_result.accepted if self.m5_result is not None else False

    @property
    def guard(self) -> GuardDecision | None:
        return self.m5_result.guard if self.m5_result is not None else None

    @property
    def matched_ids(self) -> tuple[int, ...]:
        return self.m5_result.matched_ids if self.m5_result is not None else ()

    @property
    def new_ids(self) -> tuple[int, ...]:
        return self.m5_result.new_ids if self.m5_result is not None else ()

    @property
    def expired_ids(self) -> tuple[int, ...]:
        return self.m5_result.expired_ids if self.m5_result is not None else ()


class DuDeWorkerClient:
    """Own one stateful worker. A timeout/crash poisons the client; state is not guessed."""

    protocol_version = 1

    def __init__(self, binary: str | Path, config: DuDeConfig | None = None):
        self.binary = str(binary)
        if not Path(self.binary).is_file():
            raise ContractError(f"DuDe worker binary does not exist: {self.binary}")
        self.config = config or DuDeConfig()
        self._lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=256)
        self._closed = False
        self._failed: str | None = None
        self.last_timing = {
            "m4_backend_runtime_ms": 0.0,
            "m5_guard_tracker_runtime_ms": 0.0,
            "m4_m5_runtime_ms": 0.0,
        }
        try:
            self._proc = subprocess.Popen(
                [self.binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
            )
        except OSError as exc:
            raise ContractError(f"could not start DuDe worker: {exc}") from exc
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        if self._proc.stderr is not None:
            for line in self._proc.stderr:
                self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr)

    def _terminate(self) -> None:
        if self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
                self._proc.wait(timeout=0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self._proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._proc.wait(timeout=1.0)

    def _fail(self, reason: str) -> None:
        self._failed = reason
        self._terminate()

    def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise ContractError("DuDe worker is closed")
        if self._failed:
            raise ContractError(f"DuDe worker is in error state: {self._failed}")
        if self._proc.poll() is not None or self._proc.stdin is None or self._proc.stdout is None:
            raise ContractError("DuDe worker died before request")
        data = (json.dumps(payload, allow_nan=False, separators=(",", ":")) + "\n").encode()
        deadline = time.monotonic() + self.config.timeout_s
        fdin, fdout = self._proc.stdin.fileno(), self._proc.stdout.fileno()
        selector = selectors.DefaultSelector()
        os.set_blocking(fdin, False)
        os.set_blocking(fdout, False)
        selector.register(fdin, selectors.EVENT_WRITE, "write")
        offset = 0
        response = bytearray()
        reading = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("whole request deadline exceeded")
                events = selector.select(remaining)
                if not events:
                    raise TimeoutError("whole request deadline exceeded")
                for key, _ in events:
                    if key.data == "write":
                        try:
                            offset += os.write(fdin, data[offset:])
                        except BlockingIOError:
                            continue
                        if offset == len(data):
                            selector.unregister(fdin)
                            selector.register(fdout, selectors.EVENT_READ, "read")
                            reading = True
                    elif key.data == "read":
                        try:
                            chunk = os.read(fdout, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            raise BrokenPipeError("worker closed stdout before newline response")
                        response.extend(chunk)
                        if len(response) > 64 * 1024 * 1024:
                            raise ValueError("worker response exceeds 64 MiB")
                        if b"\n" in response:
                            line, _, _tail = response.partition(b"\n")
                            parsed = json.loads(line)
                            if not isinstance(parsed, dict):
                                raise ValueError("response must be a JSON object")
                            if parsed.get("protocol_version") != self.protocol_version:
                                raise ValueError("worker protocol version mismatch")
                            if parsed.get("status") == "error":
                                raise ContractError(f"DuDe worker {parsed.get('code')}: {parsed.get('message')}")
                            return parsed
        except Exception as exc:
            self._fail(str(exc))
            if isinstance(exc, ContractError):
                raise
            raise ContractError(f"DuDe worker request failed: {exc}; stderr={self.stderr_tail[-8:]}") from exc
        finally:
            selector.close()

    @staticmethod
    def _validate_grid(grid: FreeSpaceGrid2D) -> None:
        if not isinstance(grid, FreeSpaceGrid2D):
            raise ContractError("DuDe input must be a FreeSpaceGrid2D")
        if grid.values.ndim != 2 or grid.values.size == 0:
            raise ContractError("DuDe grid must be a non-empty HxW array")
        if grid.frame_id == "" or not np.isfinite(grid.values).all():
            raise ContractError("DuDe grid frame/data invalid")

    def integrate(self, grid: FreeSpaceGrid2D, *, timestamp_s: float,
                  capture_preprocessed_mask: bool = False) -> tuple[dict[str, Any], tuple[RegionObservation, ...]]:
        self._validate_grid(grid)
        if isinstance(timestamp_s, bool) or not isinstance(timestamp_s, Real) or not math.isfinite(float(timestamp_s)):
            raise ContractError("timestamp_s must be finite")
        if type(capture_preprocessed_mask) is not bool:
            raise ContractError("capture_preprocessed_mask must be bool")
        height, width = grid.shape
        req = {
            "protocol_version": self.protocol_version, "op": "integrate",
            "request_id": f"revision-{grid.revision}", "revision": grid.revision,
            "frame_id": grid.frame_id,
            "grid": {"width": width, "height": height, "resolution_m": float(grid.resolution_m),
                     "origin_xy": [float(v) for v in grid.origin_xy], "values": grid.values.astype(int).ravel().tolist()},
            "config": {"decomp_threshold_m": self.config.decomp_threshold_m,
                       "safety_distance_m": self.config.safety_distance_m,
                       "preprocessing_policy": self.config.preprocessing_policy},
            "debug_capture_preprocessing": capture_preprocessed_mask,
        }
        response = self._exchange(req)
        if response.get("protocol_status") != "ok" or response.get("status") not in {"success", "fallback", "rejected"} or response.get("input_revision") != grid.revision:
            self._fail("malformed or wrong-revision response")
            raise ContractError("DuDe worker returned wrong status/revision")
        if response.get("frame_id") != grid.frame_id or response.get("units") != "meters":
            self._fail("malformed frame/units")
            raise ContractError("DuDe worker returned invalid coordinate contract")
        observations: list[RegionObservation] = []
        # Native retained contours on rejection are diagnostic/stale only.
        # Source-accepted reuse branches remain candidate updates.
        if response["status"] == "rejected":
            response["m5_observation_policy"] = "stale_snapshot_only"
            return response, ()
        response["m5_observation_policy"] = "candidate_update"
        regions = response.get("regions")
        if not isinstance(regions, list):
            raise ContractError("DuDe response regions must be a list")
        expected_ids = set(range(len(regions)))
        for i, region in enumerate(regions):
            if not isinstance(region, dict) or region.get("raw_id") != i:
                raise ContractError("DuDe raw IDs must be contiguous current-snapshot IDs")
            outer = region.get("outer")
            if not isinstance(outer, list) or len(outer) < 3:
                raise ContractError("DuDe polygon outer ring malformed")
            adjacent = region.get("adjacent_raw_ids")
            if not isinstance(adjacent, list) or any(type(v) is not int or v not in expected_ids or v == i for v in adjacent):
                raise ContractError("DuDe adjacency must reference other current raw IDs")
            area = region.get("area_m2")
            centroid = region.get("centroid_xy")
            observations.append(RegionObservation(
                raw_id=i, polygon=Polygon2D(tuple(tuple(p) for p in outer)),
                timestamp_s=float(timestamp_s), source_grid_revision=grid.revision,
                centroid_xy=tuple(centroid), area_m2=area,
                adjacent_raw_ids=tuple(adjacent),
                metadata={"backend":"dude_incremental_cpp", "branch":response.get("branch"),
                          "holes_supported":False, "holes_contract":response.get("diagnostics",{}).get("holes_contract")},
            ))
        return response, tuple(observations)

    def update_regions(self, processor: RegionUpdateProcessor, grid: FreeSpaceGrid2D,
                       *, timestamp_s: float | None = None,
                       capture_preprocessed_mask: bool = False):
        timestamp = time.monotonic() if timestamp_s is None else timestamp_s
        backend_started = time.perf_counter()
        response, observations = self.integrate(
            grid, timestamp_s=timestamp, capture_preprocessed_mask=capture_preprocessed_mask,
        )
        backend_finished = time.perf_counter()
        if response["status"] == "rejected":
            # Do not evaluate/commit the M5 guard or call tracker.update().
            # Return only M5's last accepted snapshot and its old provenance.
            regions = tuple(processor.guard.last_published_regions)
            tracks = processor.tracker.snapshot()
            revision = max((r.source_grid_revision for r in regions), default=None)
            result = DuDeUpdateResult(
                backend_status="rejected", backend_branch=response["branch"],
                request_revision=grid.revision, accepted_observation_revision=revision,
                stale=bool(regions), current_regions=regions, retained_tracks=tracks,
                m5_result=None, reason=response.get("rejection_reason") or response["branch"],
            )
            response["m5_observation_policy"] = "stale_snapshot_only"
            response["accepted_observation_revision"] = revision
            self.last_timing = {
                "m4_backend_runtime_ms": (backend_finished - backend_started) * 1000.0,
                "m5_guard_tracker_runtime_ms": 0.0,
                "m4_m5_runtime_ms": (backend_finished - backend_started) * 1000.0,
            }
            return response, result
        flat = grid.values.ravel()
        free_cells = int(np.count_nonzero(flat == 0))
        unknown_cells = int(np.count_nonzero(flat == -1))
        processor_started = time.perf_counter()
        m5_result = processor.update(observations, free_cells=free_cells,
                                     unknown_cells=unknown_cells, expected_cells=int(flat.size))
        processor_finished = time.perf_counter()
        self.last_timing = {
            "m4_backend_runtime_ms": (backend_finished - backend_started) * 1000.0,
            "m5_guard_tracker_runtime_ms": (processor_finished - processor_started) * 1000.0,
            "m4_m5_runtime_ms": (processor_finished - backend_started) * 1000.0,
        }
        if m5_result.accepted:
            accepted_revision = grid.revision
        else:
            accepted_revision = max(
                (r.source_grid_revision for r in processor.guard.last_published_regions), default=None,
            )
        response["accepted_observation_revision"] = accepted_revision
        return response, DuDeUpdateResult(
            backend_status=response["status"], backend_branch=response["branch"],
            request_revision=grid.revision, accepted_observation_revision=accepted_revision,
            stale=m5_result.stale, current_regions=m5_result.current_regions,
            retained_tracks=m5_result.retained_tracks, m5_result=m5_result,
            reason=m5_result.reason,
        )

    def reset(self) -> dict[str, Any]:
        return self._exchange({"protocol_version": self.protocol_version, "op":"reset"})

    def close(self) -> None:
        if self._closed:
            return
        if self._proc.poll() is None and not self._failed:
            try:
                self._exchange({"protocol_version":self.protocol_version,"op":"close"})
            except ContractError:
                pass
        self._closed = True
        if self._proc.poll() is None:
            self._terminate()
        self._stderr_thread.join(timeout=0.5)

    def __enter__(self) -> "DuDeWorkerClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
