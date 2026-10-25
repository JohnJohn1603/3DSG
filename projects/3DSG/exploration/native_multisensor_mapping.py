"""OccuSG mapping adapter that integrates independently calibrated depth sensors."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np


class MultiSensorNativeMappingStage:
    """Run OccuSG's native M1/M2/M3 path once per sensor, preserving origins."""

    def __init__(self, binary: str | Path, *, resolution_m: float = 0.05,
                 timeout_s: float = 20.0, default_max_range_m: float = 10.0) -> None:
        from standalone_sg.mapping import NativeRGBDMappingStage

        self._primary = NativeRGBDMappingStage(
            binary, resolution_m=resolution_m, timeout_s=timeout_s,
            default_max_range_m=default_max_range_m)
        self.records = self._primary.records
        construction = os.environ.get("THREEDSG_OCCUSG_SCANPOINT_CONSTRUCTION", "trusted")
        if construction not in {"standard", "trusted"}:
            raise ValueError("THREEDSG_OCCUSG_SCANPOINT_CONSTRUCTION must be standard or trusted")
        self.scanpoint_construction = construction
        if construction == "trusted":
            self._scan_point_type = _install_trusted_scan_point_type()
        else:
            from standalone_sg.mapping import native_pipeline
            self._scan_point_type = getattr(
                native_pipeline.ScanPoint, "_3dsg_base_scan_point_type",
                native_pipeline.ScanPoint)
            native_pipeline.ScanPoint = self._scan_point_type
        self._active_integrate_profile = None
        self._install_integrate_profiler()

    def _install_integrate_profiler(self) -> None:
        """Measure adapter-side CPU and IPC around the native M2 request."""
        worker = self._primary.worker
        integrate = worker.integrate
        write_request = worker._write_request
        read_response = worker._read_response

        def timed_write(payload, operation, deadline):
            profile = self._active_integrate_profile
            if profile is None or operation != "integrate":
                return write_request(payload, operation, deadline)
            wall_started = time.perf_counter_ns()
            cpu_started = time.thread_time_ns()
            try:
                return write_request(payload, operation, deadline)
            finally:
                profile["write_wall_ms"] += (time.perf_counter_ns() - wall_started) / 1e6
                profile["write_python_cpu_ms"] += (time.thread_time_ns() - cpu_started) / 1e6

        def timed_read(operation, deadline):
            profile = self._active_integrate_profile
            if profile is None or operation != "integrate":
                return read_response(operation, deadline)
            wall_started = time.perf_counter_ns()
            cpu_started = time.thread_time_ns()
            try:
                return read_response(operation, deadline)
            finally:
                profile["read_wall_ms"] += (time.perf_counter_ns() - wall_started) / 1e6
                profile["read_python_cpu_ms"] += (time.thread_time_ns() - cpu_started) / 1e6

        def timed_integrate(points, *, origin, max_range_m, frame_identity=None,
                            projection_mode="full_refresh"):
            profile = {
                "write_wall_ms": 0.0, "write_python_cpu_ms": 0.0,
                "read_wall_ms": 0.0, "read_python_cpu_ms": 0.0,
            }
            wall_started = time.perf_counter_ns()
            cpu_started = time.thread_time_ns()
            child_started = _worker_cpu_time_s(worker)
            self._active_integrate_profile = profile
            try:
                return integrate(
                    points, origin=origin, max_range_m=max_range_m,
                    frame_identity=frame_identity, projection_mode=projection_mode)
            finally:
                profile["integrate_wall_ms"] = (time.perf_counter_ns() - wall_started) / 1e6
                profile["integrate_python_cpu_ms"] = (time.thread_time_ns() - cpu_started) / 1e6
                profile["python_prepare_response_cpu_ms"] = max(
                    0.0, profile["integrate_python_cpu_ms"]
                    - profile["write_python_cpu_ms"] - profile["read_python_cpu_ms"])
                child_finished = _worker_cpu_time_s(worker)
                profile["native_child_cpu_ms"] = (
                    None if child_started is None or child_finished is None
                    else max(0.0, (child_finished - child_started) * 1000.0))
                worker.last_integrate_profile = dict(profile)
                self._active_integrate_profile = None

        worker._write_request = timed_write
        worker._read_response = timed_read
        worker.integrate = timed_integrate

    def build_grid(self, packet):
        from standalone_sg.contracts.frames import CameraIntrinsics, Transform
        from standalone_sg.geometry import backproject_depth
        grid = self._primary.build_grid(packet)
        primary_record = self.records[-1]
        primary_m1_ms = float(primary_record.get("m1_runtime_ms", 0.0))
        primary_m2_ms = float(primary_record.get("m2_runtime_ms", 0.0))
        primary_m3_ms = float(primary_record.get("m3_runtime_ms", 0.0))
        primary_total_ms = float(primary_record.get("m1_m2_m3_runtime_ms", 0.0))
        primary_record.update({
            "primary_m1_runtime_ms": primary_m1_ms,
            "primary_m2_runtime_ms": primary_m2_ms,
            "primary_m3_runtime_ms": primary_m3_ms,
            "primary_m1_m2_m3_runtime_ms": primary_total_ms,
            "primary_conversion_and_stage_overhead_ms": max(
                0.0, primary_total_ms - primary_m1_ms - primary_m2_ms - primary_m3_ms),
            "scan_point_constructor": self.scanpoint_construction,
            "primary_m2_transport_mode": str(getattr(
                self._primary.worker, "transport_mode", "unknown")),
            "primary_m2_transport_profile": dict(
                getattr(self._primary.worker, "last_integrate_profile", {})),
        })
        self.records[-1]["primary_backprojection_stride"] = int(
            packet.source_metadata.get("backprojection_stride", 1))
        auxiliary = packet.source_metadata.get("auxiliary_depth")
        if auxiliary is None:
            primary_record.update({
                "auxiliary_sensor_integrated": False,
                "m2_total_runtime_ms": primary_m2_ms,
                "m3_total_runtime_ms": primary_m3_ms,
                "all_sensor_m1_m2_m3_runtime_ms": primary_total_ms,
            })
            return grid
        auxiliary_started = time.perf_counter()
        path = Path(str(auxiliary["path"])).resolve(strict=True)
        expected_sha = str(auxiliary["file_sha256"])
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            raise ValueError("auxiliary depth file hash mismatch")
        calibration = _plain_json(auxiliary["calibration"])
        canonical = (json.dumps(calibration, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False) + "\n").encode()
        calibration_sha = hashlib.sha256(canonical).hexdigest()
        if calibration_sha != auxiliary.get("calibration_sha256"):
            raise ValueError("auxiliary depth calibration hash mismatch")
        intr = calibration["intrinsics"]
        intrinsics = CameraIntrinsics(
            width=int(intr["width"]), height=int(intr["height"]),
            fx=float(intr["fx"]), fy=float(intr["fy"]),
            cx=float(intr["cx"]), cy=float(intr["cy"]),
            calibration_id=str(calibration.get("calibration_id") or calibration_sha))
        backprojection_started = time.perf_counter()
        depth = np.load(path, allow_pickle=False)
        if depth.shape != (intrinsics.height, intrinsics.width):
            raise ValueError("auxiliary depth shape differs from its independent calibration")
        transform = Transform(
            np.asarray(calibration["T_world_camera_canonical"], dtype=np.float64),
            parent_frame="world", child_frame="camera_optical",
            timestamp=float(packet.timestamp_s), source="calibration")
        cloud = backproject_depth(
            depth, intrinsics, transform, meters_per_unit=1.0,
            stride=int(auxiliary.get("backprojection_stride", 1)),
            min_depth_m=float(calibration["min_depth_m"]),
            max_depth_m=float(calibration["max_depth_m"]),
            timestamp=float(packet.timestamp_s), scan_id=f"{packet.frame_id}:aux-depth")
        backprojection_finished = time.perf_counter()
        point_conversion_started = time.perf_counter()
        points = tuple(self._scan_point_type(float(x), float(y), float(z), True)
                       for x, y, z in cloud.xyz_W)
        point_conversion_finished = time.perf_counter()
        worker = self._primary.worker
        m2_started = time.perf_counter()
        update = worker.integrate(
            points, origin=tuple(float(v) for v in cloud.sensor_origin_W),
            max_range_m=float(calibration["max_depth_m"]),
            frame_identity=auxiliary.get("m2_identity"),
            projection_mode="partial_update")
        m2_finished = time.perf_counter()
        m3_started = time.perf_counter()
        final_grid = worker.project(mode="partial_update")
        m3_finished = time.perf_counter()
        if final_grid.revision != update.get("revision"):
            raise ValueError("auxiliary depth M2/M3 revision mismatch")
        self.records[-1].update({
            "auxiliary_sensor_integrated": True,
            "auxiliary_sensor_id": calibration["sensor_id"],
            "auxiliary_frame_id": packet.frame_id,
            "auxiliary_frame_index": packet.frame_index,
            "auxiliary_file_sha256": actual_sha,
            "auxiliary_calibration_sha256": calibration_sha,
            "auxiliary_point_count": len(points),
            "auxiliary_scan_point_constructor": self.scanpoint_construction,
            "auxiliary_m2_transport_mode": str(getattr(
                worker, "transport_mode", "unknown")),
            "auxiliary_backprojection_stride": int(auxiliary.get("backprojection_stride", 1)),
            "auxiliary_validation_runtime_ms": (backprojection_started - auxiliary_started) * 1000.0,
            "auxiliary_m1_runtime_ms": (backprojection_finished - backprojection_started) * 1000.0,
            "auxiliary_point_conversion_runtime_ms": (point_conversion_finished - point_conversion_started) * 1000.0,
            "auxiliary_m2_runtime_ms": (m2_finished - m2_started) * 1000.0,
            "auxiliary_m3_runtime_ms": (m3_finished - m3_started) * 1000.0,
            "auxiliary_m2_transport_profile": dict(
                getattr(worker, "last_integrate_profile", {})),
            "auxiliary_m1_m2_m3_runtime_ms": (m3_finished - backprojection_started) * 1000.0,
            "all_sensor_m1_m2_m3_runtime_ms": (m3_finished - auxiliary_started) * 1000.0 + primary_total_ms,
            "m2_total_runtime_ms": primary_m2_ms + (m2_finished - m2_started) * 1000.0,
            "m3_total_runtime_ms": primary_m3_ms + (m3_finished - m3_started) * 1000.0,
            "auxiliary_sensor_origin_W": [float(v) for v in cloud.sensor_origin_W],
            "auxiliary_m2_free_key_count": update.get(
                "free_key_count", len(update.get("free_keys", ()))),
            "auxiliary_m2_occupied_key_count": update.get(
                "occupied_key_count", len(update.get("occupied_keys", ()))),
            "m2_revision": update["revision"],
            "m3_revision": final_grid.revision,
            "m3_shape": list(final_grid.shape),
            "m3_origin_xy": list(final_grid.origin_xy),
            "m3_free_cell_count": int((final_grid.values == 0).sum()),
            "m3_occupied_cell_count": int((final_grid.values > 0).sum()),
            "m3_unknown_cell_count": int((final_grid.values < 0).sum()),
            "m3_source_oracle": dict(worker.last_projection_diagnostics),
        })
        return final_grid

    def child_pids(self):
        return self._primary.child_pids()

    def close(self):
        self._primary.close()


def _plain_json(value):
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


def _worker_cpu_time_s(worker) -> float | None:
    """Read Linux child CPU time with tick resolution, without extra dependencies."""
    process = getattr(worker, "_process", None)
    pid = getattr(process, "pid", None)
    if type(pid) is not int:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat[stat.rfind(")") + 2:].split()
        user_ticks, system_ticks = int(fields[11]), int(fields[12])
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None
    return (user_ticks + system_ticks) / ticks_per_second


def _install_trusted_scan_point_type():
    """Skip repeated scalar checks after PointCloud's vectorized finite check.

    The native mapper still receives ScanPoint instances and checks that type
    at its boundary. M1's PointCloud contract already validates every xyz
    coordinate as finite; the inherited fields are still assigned as floats,
    with has_hit retaining the ordinary boolean type.
    """
    from standalone_sg.mapping import native_pipeline

    scan_point_type = native_pipeline.ScanPoint
    if getattr(scan_point_type, "_3dsg_trusted_point", False):
        return scan_point_type

    class TrustedScanPoint(scan_point_type):
        __slots__ = ()

        def __post_init__(self) -> None:
            return None

    TrustedScanPoint.__name__ = "TrustedScanPoint"
    TrustedScanPoint._3dsg_trusted_point = True
    TrustedScanPoint._3dsg_base_scan_point_type = scan_point_type
    native_pipeline.ScanPoint = TrustedScanPoint
    return TrustedScanPoint
