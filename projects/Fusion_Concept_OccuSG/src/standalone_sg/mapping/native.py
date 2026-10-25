"""Persistent subprocess boundary for the M2 OctoMap/M3 MapConverter backend.

The worker protocol is intentionally line-oriented and inspectable. The child
process owns both the OcTree and pinned C++ projection state; Python never
serializes or recreates that state per scan. DuDe remains outside this module.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import selectors
import subprocess
import struct
import threading
import time
from collections.abc import Mapping
from collections.abc import Mapping
from collections import deque
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from typing import Iterable

import numpy as np

from ..contracts.errors import ContractError
from .projection import FreeSpaceGrid2D, SOURCE_COMMIT


@dataclass(frozen=True, slots=True)
class ScanPoint:
    x: float
    y: float
    z: float
    # False means a confirmed no-return through max_range, never invalid data.
    has_hit: bool = True

    def __post_init__(self) -> None:
        for name, value in (("x", self.x), ("y", self.y), ("z", self.z)):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                raise ContractError(f"ScanPoint.{name} must be finite")
        if type(self.has_hit) is not bool:
            raise ContractError("ScanPoint.has_hit must be boolean")


class OctoMapWorker:
    """Own one persistent native OctoMap process for a sequence."""

    def __init__(
        self,
        binary: str | Path,
        *,
        resolution_m: float = 0.05,
        prob_hit: float = 0.7,
        prob_miss: float = 0.4,
        clamping_min: float = 0.12,
        clamping_max: float = 0.97,
        occupancy_threshold: float = 0.5,
        projection_min_free_height_m: float = 0.3,
        projection_minimum_occupancy: int = 5,
        height_interval_trace_dir: str | Path | None = None,
        timeout_s: float = 5.0,
        response_mode: str | None = None,
        transport_mode: str | None = None,
        max_payload_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.binary = str(binary)
        if not Path(self.binary).exists():
            raise ContractError(f"OctoMap worker binary does not exist: {self.binary}")
        self._validate_positive(resolution_m, "resolution_m")
        self._validate_positive(timeout_s, "timeout_s")
        for name, value in (
            ("prob_hit", prob_hit),
            ("prob_miss", prob_miss),
            ("clamping_min", clamping_min),
            ("clamping_max", clamping_max),
            ("occupancy_threshold", occupancy_threshold),
        ):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or not 0.0 < float(value) < 1.0:
                raise ContractError(f"{name} must be finite and strictly between 0 and 1")
        if clamping_min >= clamping_max:
            raise ContractError("clamping_min must be less than clamping_max")
        if (
            isinstance(projection_min_free_height_m, bool)
            or not isinstance(projection_min_free_height_m, Real)
            or not math.isfinite(float(projection_min_free_height_m))
            or float(projection_min_free_height_m) < 0.0
        ):
            raise ContractError("projection_min_free_height_m must be finite and non-negative")
        if type(projection_minimum_occupancy) is not int or not 0 <= projection_minimum_occupancy <= 100:
            raise ContractError("projection_minimum_occupancy must be an integer in 0..100")
        if response_mode is None:
            response_mode = os.environ.get("OCCUSG_M2_RESPONSE_MODE", "compact")
        if response_mode not in {"compact", "debug"}:
            raise ContractError("response_mode must be compact or debug")
        self.response_mode = response_mode
        if transport_mode is None:
            transport_mode = os.environ.get("OCCUSG_M2_TRANSPORT", "ascii")
        if transport_mode not in {"ascii", "binary"}:
            raise ContractError("transport_mode must be ascii or binary")
        if type(max_payload_bytes) is not int or not 1 <= max_payload_bytes <= 128 * 1024 * 1024:
            raise ContractError("max_payload_bytes must be an integer in 1..134217728")
        self.transport_mode = transport_mode
        self.max_payload_bytes = max_payload_bytes
        self._next_request_id = 1
        self._binary_expected_identity = None
        self.timeout_s = float(timeout_s)
        command = [
            self.binary,
            "--serve",
            "--resolution",
            str(resolution_m),
            "--prob-hit",
            str(prob_hit),
            "--prob-miss",
            str(prob_miss),
            "--clamping-min",
            str(clamping_min),
            "--clamping-max",
            str(clamping_max),
            "--occupancy-threshold",
            str(occupancy_threshold),
            "--projection-min-free-height",
            str(projection_min_free_height_m),
            "--projection-minimum-occupancy",
            str(projection_minimum_occupancy),
        ]
        self.height_interval_trace_dir = (
            None if height_interval_trace_dir is None
            else str(Path(height_interval_trace_dir).resolve())
        )
        if self.height_interval_trace_dir is not None:
            command.extend(["--height-interval-trace-dir", self.height_interval_trace_dir])
        command.extend(["--m2-response-mode", self.response_mode])
        command.extend(["--m2-input-mode", self.transport_mode])
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
            )
        except OSError as error:
            raise ContractError(f"could not start OctoMap worker: {error}") from error
        self._stderr_lines: deque[str] = deque(maxlen=128)
        self.last_projection_diagnostics: dict = {}
        self.last_height_interval_trace: dict | None = None
        self._failed_reason: str | None = None
        self._closed = False
        self._request_lock = threading.Lock()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    @staticmethod
    def _validate_positive(value: float, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or float(value) <= 0.0:
            raise ContractError(f"{name} must be finite and positive")

    def _drain_stderr(self) -> None:
        if self._process.stderr is None:
            return
        for line in self._process.stderr:
            self._stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())

    def _stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def _terminate(self) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=0.5)

    def _mark_failed(self, reason: str) -> None:
        if self._failed_reason is None:
            self._failed_reason = reason
        self._terminate()

    def _ensure_running(self) -> None:
        if self._closed:
            raise ContractError("OctoMap worker is closed")
        if self._failed_reason is not None:
            raise ContractError(f"OctoMap worker is in error state: {self._failed_reason}")
        if self._process.poll() is not None or self._process.stdin is None or self._process.stdout is None:
            raise ContractError("OctoMap worker is not running")

    def _write_request(self, payload: bytes, operation: str, deadline: float) -> None:
        """Write the complete request under the same deadline as its response."""

        if self._process.stdin is None:
            raise ContractError("OctoMap worker stdin is unavailable")
        descriptor = self._process.stdin.fileno()
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_WRITE)
        offset = 0
        try:
            os.set_blocking(descriptor, False)
            while offset < len(payload):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    message = f"timed out writing {operation} after {self.timeout_s}s"
                    self._mark_failed(message)
                    raise ContractError(f"OctoMap worker {message}")
                ready = selector.select(remaining)
                if not ready:
                    message = f"timed out writing {operation} after {self.timeout_s}s"
                    self._mark_failed(message)
                    raise ContractError(f"OctoMap worker {message}")
                try:
                    offset += os.write(descriptor, payload[offset:])
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError) as error:
                    message = f"write failed during {operation}: {self._stderr_text()}"
                    self._mark_failed(message)
                    raise ContractError(f"OctoMap worker {message}") from error
        finally:
            selector.close()

    def _read_response(self, operation: str, deadline: float) -> dict:
        if self._process.stdout is None:
            raise ContractError("OctoMap worker stdout is unavailable")
        selector = selectors.DefaultSelector()
        descriptor = self._process.stdout.fileno()
        selector.register(descriptor, selectors.EVENT_READ)
        buffer = bytearray()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    message = f"timed out during {operation} after {self.timeout_s}s"
                    self._mark_failed(message)
                    raise ContractError(f"OctoMap worker {message}")
                ready = selector.select(remaining)
                if not ready:
                    message = f"timed out during {operation} after {self.timeout_s}s"
                    self._mark_failed(message)
                    raise ContractError(f"OctoMap worker {message}")
                try:
                    chunk = os.read(descriptor, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    message = (
                        f"truncated response during {operation}"
                        if buffer
                        else f"worker exited during {operation}: {self._stderr_text()}"
                    )
                    self._mark_failed(message)
                    raise ContractError(f"OctoMap worker {message}")
                buffer.extend(chunk)
                newline = buffer.find(b"\n")
                if newline >= 0:
                    line_bytes = bytes(buffer[:newline])
                    break
        finally:
            selector.close()
        try:
            response = json.loads(line_bytes.decode("utf-8"))
        except json.JSONDecodeError as error:
            message = f"invalid response during {operation}: {line_bytes!r}"
            self._mark_failed(message)
            raise ContractError(message) from error
        except UnicodeDecodeError as error:
            message = f"response was not UTF-8 during {operation}"
            self._mark_failed(message)
            raise ContractError(message) from error
        if not isinstance(response, dict):
            message = f"response during {operation} must be a JSON object"
            self._mark_failed(message)
            raise ContractError(message)
        if operation == "integrate":
            if type(response.get("revision")) is not int:
                message = "invalid integrate response schema"
                self._mark_failed(message)
                raise ContractError(message)
            if self.response_mode == "compact":
                count_names = ("free_key_count", "occupied_key_count")
                if any(type(response.get(name)) is not int or response[name] < 0
                       for name in count_names):
                    message = "invalid integrate response schema"
                    self._mark_failed(message)
                    raise ContractError(message)
            elif not {"free_keys", "occupied_keys", "observations"}.issubset(response) or not all(
                isinstance(response[name], list)
                for name in ("free_keys", "occupied_keys", "observations")
            ):
                message = "invalid integrate response schema"
                self._mark_failed(message)
                raise ContractError(message)
        elif operation == "snapshot":
            required = {"revision", "resolution", "bounds_min", "bounds_max", "leaves"}
            if not required.issubset(response) or type(response["revision"]) is not int:
                message = "invalid snapshot response schema"
                self._mark_failed(message)
                raise ContractError(message)
            if not isinstance(response["leaves"], list) or any(
                not isinstance(response[name], list) or len(response[name]) != 3
                for name in ("bounds_min", "bounds_max")
            ):
                message = "invalid snapshot response schema"
                self._mark_failed(message)
                raise ContractError(message)
            if isinstance(response["resolution"], bool) or not isinstance(response["resolution"], Real) or not math.isfinite(float(response["resolution"])) or float(response["resolution"]) <= 0:
                message = "invalid snapshot response schema"
                self._mark_failed(message)
                raise ContractError(message)
        elif operation == "project":
            required = {
                "revision", "updated", "mode", "resolution", "origin_xy", "width",
                "height", "values", "z_floor", "z_ceiling", "changed_bounds_xy",
                "changed_bounds_xyz",
            }
            if not required.issubset(response) or type(response["revision"]) is not int:
                message = "invalid projection response schema"
                self._mark_failed(message)
                raise ContractError(message)
            if (
                type(response["updated"]) is not bool
                or response["mode"] not in {"full_refresh", "partial_update", "clean_full_refresh"}
                or type(response["width"]) is not int
                or type(response["height"]) is not int
                or response["width"] < 0
                or response["height"] < 0
                or not isinstance(response["origin_xy"], list)
                or len(response["origin_xy"]) != 2
            ):
                message = "invalid projection response schema"
                self._mark_failed(message)
                raise ContractError(message)
            if any(
                not isinstance(response[name], list)
                for name in ("values", "z_floor", "z_ceiling")
            ):
                message = "invalid projection response schema"
                self._mark_failed(message)
                raise ContractError(message)
            expected_count = response["width"] * response["height"]
            if any(len(response[name]) != expected_count for name in ("values", "z_floor", "z_ceiling")):
                message = "projection array length does not match width*height"
                self._mark_failed(message)
                raise ContractError(message)
        if operation == "integrate" and self._binary_expected_identity is not None:
            expected = self._binary_expected_identity
            for name, value in expected.items():
                if response.get(name) != value:
                    message = f"M2 binary response {name} does not match request"
                    self._mark_failed(message)
                    raise ContractError(message)
            self._binary_expected_identity = None
        if operation == "integrate" and self._binary_expected_identity is not None:
            expected = self._binary_expected_identity
            for name, value in expected.items():
                if response.get(name) != value:
                    message = f"M2 binary response {name} does not match request"
                    self._mark_failed(message)
                    raise ContractError(message)
            self._binary_expected_identity = None
        return response

    def _validate_scan_request(
        self,
        point_list: tuple[ScanPoint, ...],
        origin: tuple[float, float, float],
        max_range_m: float,
    ) -> None:
        if len(origin) != 3 or any(
            isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value))
            for value in origin
        ):
            raise ContractError("origin must contain three finite numbers")
        self._validate_positive(max_range_m, "max_range_m")
        for point in point_list:
            if not isinstance(point, ScanPoint):
                raise ContractError("points must contain ScanPoint values")

    def integrate(
        self,
        points: Iterable[ScanPoint],
        *,
        origin: tuple[float, float, float],
        max_range_m: float,
        frame_identity: dict | None = None,
        projection_mode: str = "full_refresh",
    ) -> dict:
        point_list = tuple(points)
        self._validate_scan_request(point_list, origin, max_range_m)
        if projection_mode not in {"full_refresh", "partial_update", "clean_full_refresh"}:
            raise ContractError("invalid M2 projection_mode")
        with self._request_lock:
            self._ensure_running()
            deadline = time.monotonic() + self.timeout_s
            if self.transport_mode == "binary":
                if not isinstance(frame_identity, Mapping):
                    raise ContractError("binary M2 transport requires frame_identity")
                payload_bytes, expected = self._encode_binary_integrate(
                    point_list, origin, max_range_m, frame_identity, projection_mode)
                self._binary_expected_identity = expected
                self._write_request(payload_bytes, "integrate", deadline)
            else:
                payload = [f"scan {max_range_m} {origin[0]} {origin[1]} {origin[2]} {len(point_list)}\n"]
                payload.extend(f"{point.x} {point.y} {point.z} {int(point.has_hit)}\n" for point in point_list)
                self._write_request("".join(payload).encode("ascii"), "integrate", deadline)
            return self._read_response("integrate", deadline)

    def _encode_binary_integrate(self, points, origin, max_range_m, identity, projection_mode):
        frame_id = identity.get("m2_frame_id")
        frame_index = identity.get("m2_frame_index")
        capture_ns = identity.get("m2_capture_monotonic_ns")
        package_sha = identity.get("m2_package_sha256")
        clock_domain = identity.get("m2_capture_monotonic_domain")
        if not isinstance(frame_id, str) or not frame_id or not isinstance(clock_domain, str) or not clock_domain.startswith("host_monotonic:"):
            raise ContractError("binary M2 frame identity/clock domain is invalid")
        if type(frame_index) is not int or frame_index < 0 or type(capture_ns) is not int or capture_ns < 0:
            raise ContractError("binary M2 frame index/timestamp is invalid")
        if not isinstance(package_sha, str) or len(package_sha) != 64:
            raise ContractError("binary M2 package_sha256 is invalid")
        try:
            digest = bytes.fromhex(package_sha)
            frame_bytes = frame_id.encode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ContractError("binary M2 frame ID or package hash is invalid") from error
        if len(digest) != 32 or not 1 <= len(frame_bytes) <= 1024:
            raise ContractError("binary M2 frame ID/hash length is invalid")
        mode_value = {"full_refresh": 1, "partial_update": 2, "clean_full_refresh": 3}[projection_mode]
        xyz = np.asarray([(p.x, p.y, p.z) for p in points], dtype="<f4")
        if not np.isfinite(xyz).all():
            raise ContractError("binary M2 point cloud exceeds finite float32 range")
        hits = np.fromiter((p.has_hit for p in points), dtype=np.bool_, count=len(points))
        packed_hits = np.packbits(hits, bitorder="little").tobytes()
        metadata = struct.pack("<d3fI", float(max_range_m), *[float(v) for v in origin], len(points))
        identity_bytes = struct.pack("<QQH", frame_index, capture_ns, len(frame_bytes))
        body = metadata + bytes((mode_value,)) + identity_bytes + frame_bytes + digest + xyz.tobytes() + packed_hits
        if len(body) > self.max_payload_bytes:
            raise ContractError("binary M2 payload exceeds configured maximum")
        request_id = self._next_request_id
        self._next_request_id += 1
        header = struct.pack("<4sHHQQII", b"3SGM", 2, 1, request_id, len(body), 0, 0)
        expected = {"request_id": request_id, "frame_id": frame_id, "frame_index": frame_index,
                    "capture_monotonic_ns": capture_ns, "package_sha256": package_sha,
                    "projection_mode": projection_mode}
        return header + body, expected

    def snapshot(self) -> dict:
        with self._request_lock:
            self._ensure_running()
            deadline = time.monotonic() + self.timeout_s
            self._write_request(b"snapshot\n", "snapshot", deadline)
            return self._read_response("snapshot", deadline)

    def project(self, *, mode: str = "partial_update") -> FreeSpaceGrid2D:
        """Project the accumulated OcTree through pinned native MapConverter.

        ``full_refresh`` feeds all current leaves to one persistent MapConverter.
        ``partial_update`` follows the source callback's changed-AABB update.
        """

        mode_to_protocol = {"full_refresh": "full", "partial_update": "partial",
                            "clean_full_refresh": "clean"}
        if mode not in mode_to_protocol:
            raise ContractError("projection mode must be full_refresh or partial_update")
        with self._request_lock:
            self._ensure_running()
            deadline = time.monotonic() + self.timeout_s
            self._write_request(f"project {mode_to_protocol[mode]}\n".encode("ascii"), "project", deadline)
            response = self._read_response("project", deadline)
        width, height = response["width"], response["height"]
        self.last_projection_diagnostics = response.get("source_oracle", {})
        trace = response.get("height_interval_trace")
        if trace is not None:
            if not isinstance(trace, dict) or not isinstance(trace.get("file"), str):
                raise ContractError("invalid height-interval trace response")
            trace_file = Path(trace["file"])
            if not trace_file.is_file():
                raise ContractError(f"native height-interval trace is missing: {trace_file}")
            self.last_height_interval_trace = {
                **trace,
                "file": str(trace_file.resolve()),
                "sha256": hashlib.sha256(trace_file.read_bytes()).hexdigest(),
                "bytes": trace_file.stat().st_size,
            }
        else:
            self.last_height_interval_trace = None
        values = np.asarray(response["values"], dtype=np.int8).reshape((height, width))
        z_floor = np.asarray(
            [np.nan if value is None else float(value) for value in response["z_floor"]],
            dtype=np.float64,
        ).reshape((height, width))
        z_ceiling = np.asarray(
            [np.nan if value is None else float(value) for value in response["z_ceiling"]],
            dtype=np.float64,
        ).reshape((height, width))
        changed_bounds = response["changed_bounds_xy"]
        return FreeSpaceGrid2D(
            values=values,
            resolution_m=float(response["resolution"]),
            origin_xy=tuple(float(value) for value in response["origin_xy"]),
            z_floor=z_floor,
            z_ceiling=z_ceiling,
            revision=response["revision"],
            frame_id="world",
            changed_bounds_xy=None if changed_bounds is None else tuple(float(value) for value in changed_bounds),
            changed_bounds_xyz=(
                None if response["changed_bounds_xyz"] is None
                else tuple(float(value) for value in response["changed_bounds_xyz"])
            ),
            projection_mode=("full_refresh" if response["mode"] == "clean_full_refresh" else response["mode"]),
            updated=response["updated"],
            source=f"MapConverter:{SOURCE_COMMIT}",
            backend="cpp_mapconverter",
        )

    def close(self) -> None:
        self._closed = True
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except (OSError, ValueError):
                pass
        if self._process.poll() is None:
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._terminate()
        self._stderr_thread.join(timeout=1.0)

    def __enter__(self) -> "OctoMapWorker":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
