"""Export durable 3DSG keyframes to the legacy Habitat capture contract.

The exporter writes only current RGB-D evidence and calibration/provenance. It
does not import either mapper, create semantic labels, or build future-frame
manifests; the caller owns the persistent native session and its lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from .graph_bridge import GraphObservation


_CANONICAL_FROM_HABITAT = np.array(
    [[1.0, 0.0, 0.0, 0.0],
     [0.0, 0.0, -1.0, 0.0],
     [0.0, 1.0, 0.0, 0.0],
     [0.0, 0.0, 0.0, 1.0]], dtype=np.float64,
)
_HABITAT_SENSOR_FROM_OPTICAL = np.diag([1.0, -1.0, -1.0, 1.0])


class LegacyCaptureStore:
    """Write identity-checked, atomic frames readable by the legacy adapter."""

    def __init__(self, root: str | Path, *, scene_file: str | Path,
                 byte_cap: int = 2 * 1024**3) -> None:
        if byte_cap <= 0:
            raise ValueError("legacy capture byte cap must be positive")
        self.root = Path(root).resolve()
        self.frames_root = self.root / "frames"
        self.frames_root.mkdir(parents=True, exist_ok=True)
        self.scene_file = str(Path(scene_file).resolve())
        self.byte_cap = int(byte_cap)

    def export(self, observation: GraphObservation) -> dict[str, Any]:
        """Persist one paired RGB-D keyframe and return its legacy record."""
        rgb = np.asarray(observation.rgb.rgb, dtype=np.uint8)
        packet = observation.packet
        depth = np.asarray(packet.depth_m, dtype=np.float32)
        shape = (packet.intrinsics.height, packet.intrinsics.width)
        if rgb.shape != (*shape, 3) or depth.shape != shape:
            raise ValueError("legacy capture RGB-D dimensions differ from packet calibration")
        if (observation.keyframe.frame_id != packet.frame_id
                or observation.keyframe.frame_index != packet.frame_index):
            raise ValueError("legacy capture observation identity mismatch")

        relative_dir = Path("frames") / f"frame_{packet.frame_index:08d}"
        target = self.root / relative_dir
        metadata = self._metadata(observation)
        metadata_bytes = _canonical_json(metadata)
        valid = (np.isfinite(depth) & (depth >= packet.depth_min_m)
                 & (depth <= packet.depth_max_m))
        files = {"rgb.npy": rgb, "depth_m.npy": depth,
                 "valid_mask.npy": valid.astype(np.bool_)}
        if packet.nav_depth_m is not None:
            files["navigation_depth_m.npy"] = np.asarray(packet.nav_depth_m, dtype=np.float32)
        if target.exists():
            return self._validate_existing(target, observation, metadata_bytes, files, relative_dir)

        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=self.frames_root))
        try:
            nbytes = 0
            for name, array in files.items():
                path = temporary / name
                _save_npy(path, array)
                nbytes += path.stat().st_size
            (temporary / "metadata.json").write_bytes(metadata_bytes)
            _fsync_file(temporary / "metadata.json")
            nbytes += len(metadata_bytes)
            current_bytes = sum(path.stat().st_size for path in self.frames_root.glob("frame_*/**/*")
                                if path.is_file())
            if current_bytes + nbytes > self.byte_cap:
                raise OSError(f"legacy capture byte cap exceeded ({current_bytes + nbytes} > {self.byte_cap})")
            os.replace(temporary, target)
            _fsync_directory(self.frames_root)
        except Exception:
            _remove_tree(temporary)
            raise

        return self._record_with_hashes(observation, target, relative_dir)

    def _validate_existing(self, target: Path, observation: GraphObservation,
                           metadata_bytes: bytes, files: dict[str, np.ndarray],
                           relative_dir: Path) -> dict[str, Any]:
        if _canonical_json(json.loads((target / "metadata.json").read_text(encoding="utf-8"))) != metadata_bytes:
            raise ValueError("existing legacy capture metadata conflicts with current keyframe")
        for name, array in files.items():
            stored = np.load(target / name, allow_pickle=False)
            if not np.array_equal(stored, array, equal_nan=array.dtype.kind == "f"):
                raise ValueError(f"existing legacy capture conflicts with current keyframe: {name}")
        return self._record_with_hashes(observation, target, relative_dir)

    def _record(self, observation: GraphObservation, relative_dir: Path) -> dict[str, Any]:
        packet = observation.packet
        if packet.capture_wall_time_unix_s is None or packet.capture_monotonic_domain is None:
            raise ValueError("native mapper capture requires source wall time and monotonic clock domain")
        return {
            "schema": "frontier-legacy-habitat-capture-record/v1",
            "frame_dir": relative_dir.as_posix(),
            "frame_id": packet.frame_id,
            "frame_index": int(packet.frame_index),
            "timestamp_s": float(packet.simulator_time_s),
            "timestamp_clock_domain": "habitat_simulation",
            "capture_wall_time_unix_s": float(packet.capture_wall_time_unix_s),
            "capture_monotonic_s": float(packet.host_monotonic_s),
            "capture_monotonic_domain": str(packet.capture_monotonic_domain),
            "scene_id": packet.scene_id,
            "sequence_id": packet.run_id,
        }

    def _record_with_hashes(self, observation: GraphObservation, target: Path,
                            relative_dir: Path) -> dict[str, Any]:
        names = ["rgb.npy", "depth_m.npy", "valid_mask.npy"]
        if (target / "navigation_depth_m.npy").is_file():
            names.append("navigation_depth_m.npy")
        names.append("metadata.json")
        hashes = {
            name: _sha256((target / name).read_bytes())
            for name in names
        }
        row = self._record(observation, relative_dir)
        row["raw_files_sha256"] = hashes
        row["source_files_sha256"] = hashes
        metadata = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
        if "auxiliary_depth" in metadata:
            auxiliary = dict(metadata["auxiliary_depth"])
            auxiliary["file_sha256"] = hashes[auxiliary["file"]]
            row["auxiliary_depth"] = auxiliary
        return row

    def _metadata(self, observation: GraphObservation) -> dict[str, Any]:
        packet = observation.packet
        camera_to_world = packet.T_world_camera.matrix
        habitat_sensor_to_world = (np.linalg.inv(_CANONICAL_FROM_HABITAT)
                                   @ camera_to_world
                                   @ np.linalg.inv(_HABITAT_SENSOR_FROM_OPTICAL))
        sensor_rotation = habitat_sensor_to_world[:3, :3]
        pose = observation.keyframe.metadata.get("robot_pose") if observation.keyframe.metadata else None
        if not isinstance(pose, (tuple, list)) or len(pose) != 3:
            raise ValueError("keyframe metadata must contain robot_pose=[x,y,yaw] for legacy capture")
        x_m, y_m, yaw_rad = map(float, pose)
        if not all(math.isfinite(value) for value in (x_m, y_m, yaw_rad)):
            raise ValueError("legacy capture robot pose must be finite")
        c, s = math.cos(yaw_rad), math.sin(yaw_rad)
        base_pose = np.eye(4, dtype=np.float64)
        base_pose[:3, :3] = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
        base_pose[:3, 3] = (x_m, y_m, packet.floor_z_m)
        hfov_rad = 2.0 * math.atan(packet.intrinsics.width / (2.0 * packet.intrinsics.fx))
        metadata = {
            "schema": "legacy-habitat-frame-metadata/v1",
            "frame_id": packet.frame_id,
            "frame_index": int(packet.frame_index),
            "timestamp_s": float(packet.simulator_time_s),
            "timestamp_clock_domain": "habitat_simulation",
            "capture_wall_time_unix_s": packet.capture_wall_time_unix_s,
            "capture_monotonic_s": float(packet.host_monotonic_s),
            "capture_monotonic_domain": packet.capture_monotonic_domain,
            "scene_id": packet.scene_id,
            "sequence_id": packet.run_id,
            "scene_file": self.scene_file,
            "dataset_type": "hm3d",
            "dataset_version": "hm3d-minival-0.2",
            "depth_units": "m",
            "depth_representation": "axial",
            "min_depth_m": packet.depth_min_m,
            "max_depth_m": packet.depth_max_m,
            "width": packet.intrinsics.width,
            "height": packet.intrinsics.height,
            "hfov_deg": math.degrees(hfov_rad),
            "calibration_id": packet.calibration_hash,
            "canonical_from_habitat": _CANONICAL_FROM_HABITAT.tolist(),
            "sensor_position_habitat": habitat_sensor_to_world[:3, 3].tolist(),
            "sensor_rotation_habitat": sensor_rotation.tolist(),
            "base_pose_canonical": base_pose.tolist(),
            "navmesh_used_for_route": False,
            "semantic_gt_in_builder": False,
        }
        if packet.nav_depth_m is not None:
            intrinsics = packet.nav_depth_intrinsics
            nav_pose = packet.T_world_nav_depth_camera
            assert intrinsics is not None and nav_pose is not None
            nav_calibration = {
                "sensor_id": "navigation_depth_camera",
                "depth_units": "m", "depth_representation": "axial",
                "min_depth_m": packet.nav_depth_min_m,
                "max_depth_m": packet.nav_depth_max_m,
                "width": intrinsics.width, "height": intrinsics.height,
                "intrinsics": {"fx": intrinsics.fx, "fy": intrinsics.fy,
                               "cx": intrinsics.cx, "cy": intrinsics.cy,
                               "width": intrinsics.width, "height": intrinsics.height},
                "T_world_camera_canonical": nav_pose.matrix.tolist(),
                "calibration_id": packet.calibration_hash,
                "file": "navigation_depth_m.npy",
            }
            nav_calibration["calibration_sha256"] = _sha256(_canonical_json(nav_calibration))
            metadata["auxiliary_depth"] = nav_calibration
        return metadata


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                        allow_nan=False) + "\n").encode("utf-8")


def _save_npy(path: Path, array: np.ndarray) -> None:
    with path.open("wb") as stream:
        np.save(stream, array, allow_pickle=False)
        stream.flush()
        os.fsync(stream.fileno())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            _remove_tree(child)
        else:
            child.unlink()
    path.rmdir()
