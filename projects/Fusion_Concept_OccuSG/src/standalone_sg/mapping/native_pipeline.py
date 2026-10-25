"""Concrete native point-cloud MappingStage used by the local M11 smoke."""
from __future__ import annotations

import math
import time
from dataclasses import replace
from numbers import Real
from pathlib import Path
import numpy as np


from ..contracts.errors import ContractError
from ..contracts.frames import PointCloud, RGBDFrame
from ..geometry import backproject_depth
from .native import OctoMapWorker, ScanPoint


class NativePointCloudMappingStage:
    """Convert canonical world point clouds through persistent native M2 and M3."""

    def __init__(self, binary: str | Path, *, resolution_m: float = 0.05,
                 timeout_s: float = 15.0, default_max_range_m: float = 1.0,
                 height_interval_trace_dir: str | Path | None = None) -> None:
        self.worker = OctoMapWorker(binary, resolution_m=resolution_m, timeout_s=timeout_s,
                                    height_interval_trace_dir=height_interval_trace_dir)
        if isinstance(default_max_range_m, bool) or not isinstance(default_max_range_m, Real) or not math.isfinite(float(default_max_range_m)) or float(default_max_range_m) <= 0.0:
            self.worker.close()
            raise ContractError("default_max_range_m must be finite and positive")
        self.default_max_range_m = float(default_max_range_m)
        self.records: list[dict] = []

    def build_grid(self, packet) -> object:
        if getattr(packet, "input_mode", None) != "pointcloud" or not isinstance(getattr(packet, "pointcloud", None), PointCloud):
            raise ContractError("NativePointCloudMappingStage requires a pointcloud FramePacket")
        cloud = packet.pointcloud
        max_range = packet.source_metadata.get("max_range_m", self.default_max_range_m)
        if isinstance(max_range, bool) or not isinstance(max_range, Real) or not math.isfinite(float(max_range)) or float(max_range) <= 0.0:
            raise ContractError("pointcloud source_metadata.max_range_m must be finite and positive")
        hit_values = cloud.has_hit
        points = tuple(
            ScanPoint(float(x), float(y), float(z), True if hit_values is None else bool(hit_values[index]))
            for index, (x, y, z) in enumerate(cloud.xyz_W)
        )
        mode = "full_refresh" if not self.records else "partial_update"
        frame_identity = packet.source_metadata.get("m2_identity")
        mode = "full_refresh" if not self.records else "partial_update"
        frame_identity = packet.source_metadata.get("m2_identity")
        m2_started = time.perf_counter()
        update = self.worker.integrate(
            points,
            origin=tuple(float(value) for value in cloud.sensor_origin_W),
            max_range_m=float(max_range), frame_identity=frame_identity,
            projection_mode=mode,
        )
        m2_finished = time.perf_counter()
        m3_started = time.perf_counter()
        grid = self.worker.project(mode=mode)
        m3_finished = time.perf_counter()
        if grid.revision != update.get("revision"):
            self.close()
            raise ContractError("native M2/M3 revision mismatch")
        self.records.append({
            "frame_id": packet.frame_id,
            "frame_index": packet.frame_index,
            "point_count": len(points),
            "m2_hit_scan_point_count": sum(point.has_hit for point in points),
            "m2_no_return_scan_point_count": sum(not point.has_hit for point in points),
            "max_range_m": float(max_range),
            "m2_revision": update["revision"],
            "m2_free_key_count": update.get("free_key_count", len(update.get("free_keys", ()))),
            "m2_occupied_key_count": update.get("occupied_key_count", len(update.get("occupied_keys", ()))),
            "m3_resolution_m": grid.resolution_m,
            "m3_revision": grid.revision,
            "m3_mode": grid.projection_mode,
            "m3_backend": grid.backend,
            "m3_shape": list(grid.shape),
            "m3_origin_xy": list(grid.origin_xy),
            "m3_free_cell_count": int((grid.values == 0).sum()),
            "m3_occupied_cell_count": int((grid.values > 0).sum()),
            "m3_unknown_cell_count": int((grid.values < 0).sum()),
            "m3_changed_bounds_xyz": None if grid.changed_bounds_xyz is None else list(grid.changed_bounds_xyz),
            "m3_source_oracle": dict(self.worker.last_projection_diagnostics),
            "m2_runtime_ms": (m2_finished - m2_started) * 1000.0,
            "m3_runtime_ms": (m3_finished - m3_started) * 1000.0,
        })
        return grid

    def child_pids(self) -> tuple[int, ...]:
        pid=self.worker._process.pid
        return () if pid is None else (pid,)

    def close(self) -> None:
        self.worker.close()

class NativeRGBDMappingStage:
    """M1 back-projection followed by the persistent native M2/M3 point path.

    RGBDFrame.depth_m is metric axial depth. Units and representation are
    required in packet metadata; invalid/masked pixels are omitted by M1 and
    never become no-return rays.
    """

    def __init__(self, binary: str | Path, *, resolution_m: float = 0.05,
                 timeout_s: float = 15.0, default_max_range_m: float = 5.0,
                 height_interval_trace_dir: str | Path | None = None) -> None:
        self._native = NativePointCloudMappingStage(
            binary, resolution_m=resolution_m, timeout_s=timeout_s,
            default_max_range_m=default_max_range_m,
            height_interval_trace_dir=height_interval_trace_dir,
        )

    @property
    def worker(self):
        return self._native.worker

    @property
    def records(self) -> list[dict]:
        return self._native.records

    def child_pids(self) -> tuple[int, ...]:
        return self._native.child_pids()

    def close(self) -> None:
        self._native.close()

    def build_grid(self, packet) -> object:
        frame = getattr(packet, "rgbd", None)
        if getattr(packet, "input_mode", None) != "rgbd" or not isinstance(frame, RGBDFrame):
            raise ContractError("NativeRGBDMappingStage requires an rgbd FramePacket")
        metadata = packet.source_metadata
        if metadata.get("depth_units") != "m" or metadata.get("depth_representation") != "axial":
            raise ContractError("RGB-D metadata must declare depth_units='m' and depth_representation='axial'")
        scale = metadata.get("depth_scale_to_m")
        if isinstance(scale, bool) or not isinstance(scale, Real) or not math.isfinite(float(scale)) or float(scale) != 1.0:
            raise ContractError("RGBDFrame.depth_m is metric; depth_scale_to_m must explicitly equal 1.0")
        stride = metadata.get("backprojection_stride", 1)
        if type(stride) is not int or stride < 1:
            raise ContractError("backprojection_stride must be a positive integer")
        min_depth = metadata.get("min_depth_m", 0.1)
        max_depth = metadata.get("max_depth_m", 10.0)
        for key, value in (("min_depth_m", min_depth), ("max_depth_m", max_depth)):
            if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
                raise ContractError(f"{key} must be finite")
        if float(max_depth) <= float(min_depth):
            raise ContractError("max_depth_m must be greater than min_depth_m")
        m1_started = time.perf_counter()
        cloud = backproject_depth(
            frame.depth_m, frame.intrinsics, frame.T_W_Copt,
            meters_per_unit=1.0, stride=stride,
            min_depth_m=float(min_depth), max_depth_m=float(max_depth),
            valid_mask=frame.valid_mask, timestamp=frame.timestamp,
            scan_id=frame.frame_id or packet.frame_id,
        )
        m1_finished = time.perf_counter()
        point_packet = replace(packet, input_mode="pointcloud", rgbd=None, pointcloud=cloud)
        grid = self._native.build_grid(point_packet)
        self.records[-1].update({
            "input_mode": "rgbd",
            "backprojection": "M1:standalone_sg.geometry.backproject_depth",
            "depth_units": "m",
            "depth_representation": "axial",
            "depth_scale_to_m": 1.0,
            "rgb_present": frame.rgb is not None,
            "input_pixel_count": int(frame.depth_m.size),
            "finite_depth_pixel_count": int(np.isfinite(frame.depth_m).sum()),
            "valid_mask_pixel_count": int(frame.valid_mask.sum()) if frame.valid_mask is not None else int(frame.depth_m.size),
            "backprojected_hit_count": len(cloud.xyz_W),
            "m1_runtime_ms": (m1_finished - m1_started) * 1000.0,
            "m1_m2_m3_runtime_ms": (time.perf_counter() - m1_started) * 1000.0,
            "sensor_origin_W": [float(value) for value in cloud.sensor_origin_W],
            "sensor_pose_source": frame.T_W_Copt.source,
            "sensor_pose_timestamp_s": frame.T_W_Copt.timestamp,
            "intrinsics": {
                "width": frame.intrinsics.width, "height": frame.intrinsics.height,
                "fx": frame.intrinsics.fx, "fy": frame.intrinsics.fy,
                "cx": frame.intrinsics.cx, "cy": frame.intrinsics.cy,
                "calibration_id": frame.intrinsics.calibration_id,
            },
        })
        return grid
