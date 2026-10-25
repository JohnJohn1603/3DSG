"""Bounded RGB-D samples for auditing initial-scan observation coverage."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np


class InitialScanAuditRecorder:
    """Keep at most one paired RGB-D frame per 15-degree yaw sector.

    The fixed byte cap prevents diagnostics from scaling with episode length.
    Samples include the camera pose and the NavMap revision used by the tick,
    so offline ray coverage can be related to the exact observation.
    """

    YAW_BINS = 24
    MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, root: str | Path, *, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._seen_bins: set[int] = set()
        self._samples: list[dict[str, Any]] = []
        self._bytes_written = 0
        self._status = "capturing"
        self._write_manifest()

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def record(self, *, rgb: Any, packet: Any, robot_pose: Any,
               snapshot: Any, elapsed_s: float) -> dict[str, Any] | None:
        if snapshot.as_of_frame_id != packet.frame_id:
            raise ValueError("scan audit requires the NavMap revision for the sampled frame")
        yaw = float(robot_pose.yaw_rad)
        yaw_bin = int(math.floor(((yaw + math.pi) % math.tau) / math.tau * self.YAW_BINS))
        if yaw_bin in self._seen_bins:
            return None

        rgb_array = np.asarray(rgb.rgb)
        depth_array = np.asarray(packet.depth_m, dtype=np.float32)
        expected_shape = (packet.intrinsics.height, packet.intrinsics.width)
        if depth_array.shape != expected_shape or rgb_array.shape[:2] != expected_shape:
            raise ValueError("scan audit RGB-D dimensions do not match camera intrinsics")
        packet_nav_depth = getattr(packet, "nav_depth_m", None)
        nav_depth_array = (None if packet_nav_depth is None
                           else np.asarray(packet_nav_depth, dtype=np.float32))
        sample_bytes = int(rgb_array.nbytes + depth_array.nbytes
                           + (0 if nav_depth_array is None else nav_depth_array.nbytes))
        if self._bytes_written + sample_bytes > self.MAX_BYTES:
            self._status = "byte_cap_reached"
            self._write_manifest()
            return None

        prefix = f"frame_{packet.frame_index:08d}"
        rgb_name, depth_name = f"{prefix}_rgb.npy", f"{prefix}_depth_m.npy"
        self._write_npy(self.root / rgb_name, rgb_array)
        self._write_npy(self.root / depth_name, depth_array)
        intrinsics = packet.intrinsics
        row = {
            "yaw_bin": yaw_bin,
            "yaw_bin_center_deg": -180.0 + (yaw_bin + 0.5) * (360.0 / self.YAW_BINS),
            "frame_id": packet.frame_id,
            "frame_index": int(packet.frame_index),
            "simulator_time_s": float(packet.simulator_time_s),
            "host_monotonic_s": float(packet.host_monotonic_s),
            "elapsed_s": float(elapsed_s),
            "robot_pose_xy_yaw": [float(robot_pose.x_m), float(robot_pose.y_m), yaw],
            "T_world_camera": packet.T_world_camera.matrix.tolist(),
            "floor_z_m": float(packet.floor_z_m),
            "depth_min_m": float(packet.depth_min_m),
            "depth_max_m": float(packet.depth_max_m),
            "intrinsics": {
                "fx": float(intrinsics.fx), "fy": float(intrinsics.fy),
                "cx": float(intrinsics.cx), "cy": float(intrinsics.cy),
                "width": int(intrinsics.width), "height": int(intrinsics.height),
            },
            "nav_revision": int(snapshot.revision),
            "nav_as_of_frame_id": snapshot.as_of_frame_id,
            "nav_as_of_frame_index": int(snapshot.as_of_frame_index),
            "rgb_file": rgb_name,
            "depth_file": depth_name,
            "array_bytes": sample_bytes,
        }
        if nav_depth_array is not None:
            nav_pose = packet.T_world_nav_depth_camera
            nav_intrinsics = packet.nav_depth_intrinsics
            assert nav_intrinsics is not None and nav_pose is not None
            nav_name = f"{prefix}_nav_depth_m.npy"
            self._write_npy(self.root / nav_name, nav_depth_array)
            row.update({
                "nav_depth_file": nav_name,
                "nav_depth_min_m": float(packet.nav_depth_min_m),
                "nav_depth_max_m": float(packet.nav_depth_max_m),
                "T_world_nav_depth_camera": nav_pose.matrix.tolist(),
                "nav_depth_intrinsics": {
                    "fx": float(nav_intrinsics.fx), "fy": float(nav_intrinsics.fy),
                    "cx": float(nav_intrinsics.cx), "cy": float(nav_intrinsics.cy),
                    "width": int(nav_intrinsics.width), "height": int(nav_intrinsics.height),
                },
            })
        self._samples.append(row)
        self._seen_bins.add(yaw_bin)
        self._bytes_written += sample_bytes
        self._write_manifest()
        return row

    def close(self, *, status: str = "closed") -> None:
        self._status = str(status)
        self._write_manifest()

    def _write_manifest(self) -> None:
        payload = {
            "schema": "initial-scan-rgbd-audit/v1",
            "run_id": self.run_id,
            "status": self._status,
            "yaw_bin_count": self.YAW_BINS,
            "max_bytes": self.MAX_BYTES,
            "bytes_written": self._bytes_written,
            "sample_count": len(self._samples),
            "samples": self._samples,
        }
        encoded = (json.dumps(payload, allow_nan=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8")
        self._write_bytes(self.root / "manifest.json", encoded)

    @staticmethod
    def _write_npy(path: Path, array: np.ndarray) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                np.save(stream, array, allow_pickle=False)
                stream.flush()
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _write_bytes(path: Path, payload: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
