"""Atomic RGB-D keyframe persistence before scheduler admission."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from .habitat_online import RGBFrame
from .keyframe_scheduler import PersistedKeyframe
from .contracts import FramePacket
from .contracts import CameraIntrinsics, Pose3


_PROTECTED_REASONS = {"first_view", "doorway", "new_region", "frontier_viewpoint"}


class KeyframeStore:
    """Persist only selected RGB-D evidence; never records full-rate depth."""

    def __init__(self, run_root: str | Path, *, byte_cap: int = 2 * 1024**3) -> None:
        self.root = Path(run_root).resolve() / "keyframes"
        if byte_cap <= 0:
            raise ValueError("keyframe store byte cap must be positive")
        self.byte_cap = int(byte_cap)
        self.root.mkdir(parents=True, exist_ok=True)

    def persist(self, rgb: RGBFrame, packet: FramePacket, *, novelty: float,
                protected_reason: str | None = None,
                extra_metadata: dict[str, Any] | None = None) -> PersistedKeyframe:
        if (rgb.run_id, rgb.scene_id, rgb.frame_id, rgb.frame_index) != (
                packet.run_id, packet.scene_id, packet.frame_id, packet.frame_index):
            raise ValueError("RGB and depth packet identity mismatch")
        rgb_array = np.asarray(rgb.rgb, dtype=np.uint8)
        depth_array = np.asarray(packet.depth_m, dtype=np.float32)
        expected = (packet.intrinsics.height, packet.intrinsics.width)
        if rgb_array.shape != (*expected, 3) or depth_array.shape != expected:
            raise ValueError("RGB-D shapes do not match packet calibration")
        if not math.isfinite(novelty) or novelty < 0:
            raise ValueError("novelty must be finite and non-negative")
        if protected_reason is not None and protected_reason not in _PROTECTED_REASONS:
            raise ValueError(f"unsupported protected keyframe reason: {protected_reason}")
        metadata_bytes = _canonical_json(_keyframe_metadata(
            packet, novelty=novelty, protected_reason=protected_reason,
            extra_metadata=extra_metadata,
        ))

        target = self.root / f"frame_{packet.frame_index:08d}"
        if target.exists():
            return self._existing(target, packet, rgb_array, depth_array, metadata_bytes,
                                  novelty, protected_reason)
        temp = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=self.root))
        try:
            files = {
                "rgb.npy": _save_npy(temp / "rgb.npy", rgb_array),
                "depth_m.npy": _save_npy(temp / "depth_m.npy", depth_array),
            }
            if packet.nav_depth_m is not None:
                files["nav_depth_m.npy"] = _save_npy(
                    temp / "nav_depth_m.npy", np.asarray(packet.nav_depth_m, dtype=np.float32))
            (temp / "metadata.json").write_bytes(metadata_bytes)
            _fsync_file(temp / "metadata.json")
            files["metadata.json"] = _sha256(metadata_bytes)
            package_hash = _package_hash(files)
            manifest = {
                "schema": "frontier-keyframe-manifest/v2",
                "frame_id": packet.frame_id,
                "frame_index": packet.frame_index,
                "files": files,
                "package_sha256": package_hash,
            }
            manifest_bytes = _canonical_json(manifest)
            (temp / "manifest.json").write_bytes(manifest_bytes)
            _fsync_file(temp / "manifest.json")
            _fsync_directory(temp)
            payload_bytes = sum((temp / name).stat().st_size for name in (*files.keys(), "manifest.json"))
            current_bytes = sum(p.stat().st_size for p in self.root.glob("frame_*/**/*") if p.is_file())
            if current_bytes + payload_bytes > self.byte_cap:
                raise OSError(f"keyframe disk cap exceeded ({current_bytes + payload_bytes} > {self.byte_cap})")
            os.replace(temp, target)
            _fsync_directory(self.root)
            return PersistedKeyframe(
                run_id=packet.run_id, scene_id=packet.scene_id, frame_id=packet.frame_id,
                frame_index=packet.frame_index, host_monotonic_s=packet.host_monotonic_s,
                payload_ref=str(target), payload_bytes=payload_bytes,
                payload_sha256=package_hash, novelty=float(novelty),
                protected_reason=protected_reason,
                metadata=dict(extra_metadata or {}),
            )
        except Exception:
            _remove_temp_tree(temp)
            raise

    def load(self, item: PersistedKeyframe) -> tuple[RGBFrame, FramePacket]:
        """Load and verify a durable keyframe for an asynchronous graph worker."""
        target = Path(item.payload_ref).resolve(strict=True)
        if target.parent != self.root or not target.is_dir():
            raise ValueError("keyframe reference is outside this run's keyframe store")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        metadata = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
        manifest_schema = manifest.get("schema")
        allowed_files = {"rgb.npy", "depth_m.npy", "metadata.json"}
        if manifest_schema == "frontier-keyframe-manifest/v2" and metadata.get("nav_depth") is not None:
            allowed_files.add("nav_depth_m.npy")
        if (manifest_schema not in {"frontier-keyframe-manifest/v1", "frontier-keyframe-manifest/v2"}
                or manifest.get("frame_id") != item.frame_id
                or manifest.get("frame_index") != item.frame_index
                or metadata.get("run_id") != item.run_id
                or metadata.get("scene_id") != item.scene_id
                or metadata.get("frame_id") != item.frame_id
                or metadata.get("frame_index") != item.frame_index):
            raise ValueError("persisted keyframe identity does not match scheduler reference")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != allowed_files:
            raise ValueError("persisted keyframe has an invalid file manifest")
        for name, digest in files.items():
            if _sha256((target / name).read_bytes()) != digest:
                raise ValueError(f"persisted keyframe hash mismatch: {name}")
        package_hash = _package_hash(files)
        if (package_hash != manifest.get("package_sha256")
                or package_hash != item.payload_sha256):
            raise ValueError("persisted keyframe package hash mismatch")

        intr = metadata["intrinsics"]
        intrinsics = CameraIntrinsics(**intr)
        pose = Pose3(np.asarray(metadata["T_world_camera"], dtype=np.float64),
                     frame_id=item.frame_id)
        depth = np.load(target / "depth_m.npy", allow_pickle=False)
        rgb = np.load(target / "rgb.npy", allow_pickle=False)
        nav_meta = metadata.get("nav_depth")
        nav_depth = (np.load(target / "nav_depth_m.npy", allow_pickle=False)
                     if nav_meta is not None else None)
        packet = FramePacket(
            run_id=item.run_id, scene_id=item.scene_id, frame_id=item.frame_id,
            frame_index=item.frame_index, simulator_time_s=metadata["simulator_time_s"],
            host_monotonic_s=metadata["host_monotonic_s"], depth_m=depth,
            intrinsics=intrinsics, T_world_camera=pose, floor_z_m=metadata["floor_z_m"],
            calibration_hash=metadata["calibration_hash"],
            depth_min_m=metadata["depth_min_m"], depth_max_m=metadata["depth_max_m"],
            rgb_ref=str(target / "rgb.npy"),
            capture_wall_time_unix_s=metadata.get("capture_wall_time_unix_s"),
            capture_monotonic_domain=metadata.get("capture_monotonic_domain"),
            nav_depth_m=nav_depth,
            nav_depth_intrinsics=(CameraIntrinsics(**nav_meta["intrinsics"])
                                  if nav_meta is not None else None),
            T_world_nav_depth_camera=(Pose3(np.asarray(nav_meta["T_world_camera"], dtype=np.float64),
                                            frame_id=item.frame_id)
                                     if nav_meta is not None else None),
            nav_depth_min_m=nav_meta["depth_min_m"] if nav_meta is not None else None,
            nav_depth_max_m=nav_meta["depth_max_m"] if nav_meta is not None else None,
        )
        if rgb.shape != (intrinsics.height, intrinsics.width, 3) or rgb.dtype != np.uint8:
            raise ValueError("persisted RGB data does not match calibration")
        rgb.setflags(write=False)
        return (RGBFrame(item.run_id, item.scene_id, item.frame_id, item.frame_index,
                         packet.simulator_time_s, packet.host_monotonic_s, rgb), packet)

    def _existing(self, target: Path, packet: FramePacket, rgb_array: np.ndarray,
                  depth_array: np.ndarray, expected_metadata: bytes, novelty: float,
                  protected_reason: str | None) -> PersistedKeyframe:
        manifest_path = target / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("frame_id") != packet.frame_id or manifest.get("frame_index") != packet.frame_index:
                raise ValueError("existing keyframe path has conflicting identity")
            metadata = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
            if _canonical_json(metadata) != expected_metadata:
                raise ValueError("existing keyframe metadata conflicts with the new admission")
            stored_rgb = np.load(target / "rgb.npy", allow_pickle=False)
            stored_depth = np.load(target / "depth_m.npy", allow_pickle=False)
            stored_nav = (np.load(target / "nav_depth_m.npy", allow_pickle=False)
                          if (target / "nav_depth_m.npy").exists() else None)
            requested_nav = packet.nav_depth_m
            if ((stored_nav is None) != (requested_nav is None)
                    or stored_nav is not None and not np.array_equal(
                        stored_nav, requested_nav, equal_nan=True)
                    or not np.array_equal(stored_rgb, rgb_array)
                    or not np.array_equal(stored_depth, depth_array, equal_nan=True)):
                raise ValueError("existing keyframe RGB-D identity conflict")
            files = manifest["files"]
            for name, digest in files.items():
                if _sha256((target / name).read_bytes()) != digest:
                    raise ValueError(f"persisted keyframe hash mismatch: {name}")
            package_hash = _package_hash(files)
            if package_hash != manifest.get("package_sha256"):
                raise ValueError("persisted keyframe package hash mismatch")
            payload_bytes = sum((target / name).stat().st_size for name in (*files.keys(), "manifest.json"))
            return PersistedKeyframe(packet.run_id, packet.scene_id, packet.frame_id,
                                     packet.frame_index, packet.host_monotonic_s,
                                     str(target), payload_bytes, package_hash,
                                     float(novelty), protected_reason,
                                     dict(json.loads(expected_metadata).get("extra", {})))
        except Exception as exc:
            raise ValueError(f"existing keyframe is not valid; refusing overwrite: {target}") from exc


def _save_npy(path: Path, array: np.ndarray) -> str:
    with path.open("wb") as stream:
        np.save(stream, array, allow_pickle=False)
        stream.flush()
        os.fsync(stream.fileno())
    return _sha256(path.read_bytes())


def _keyframe_metadata(packet: FramePacket, *, novelty: float,
                       protected_reason: str | None,
                       extra_metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "schema": "frontier-rgbd-keyframe/v1",
        "run_id": packet.run_id,
        "scene_id": packet.scene_id,
        "frame_id": packet.frame_id,
        "frame_index": packet.frame_index,
        "simulator_time_s": packet.simulator_time_s,
        "host_monotonic_s": packet.host_monotonic_s,
        "host_clock_domain": "python_time.monotonic",
        "capture_wall_time_unix_s": packet.capture_wall_time_unix_s,
        "capture_monotonic_domain": packet.capture_monotonic_domain,
        "depth_units": "m",
        "depth_representation": "axial",
        "camera_convention": "+X right,+Y down,+Z forward",
        "world_frame": "canonical_world_z_up",
        "depth_min_m": packet.depth_min_m,
        "depth_max_m": packet.depth_max_m,
        "floor_z_m": packet.floor_z_m,
        "intrinsics": {
            "fx": packet.intrinsics.fx, "fy": packet.intrinsics.fy,
            "cx": packet.intrinsics.cx, "cy": packet.intrinsics.cy,
            "width": packet.intrinsics.width, "height": packet.intrinsics.height,
        },
        "T_world_camera": packet.T_world_camera.matrix.tolist(),
        "nav_depth": (None if packet.nav_depth_m is None else {
            "depth_min_m": packet.nav_depth_min_m,
            "depth_max_m": packet.nav_depth_max_m,
            "intrinsics": {
                "fx": packet.nav_depth_intrinsics.fx, "fy": packet.nav_depth_intrinsics.fy,
                "cx": packet.nav_depth_intrinsics.cx, "cy": packet.nav_depth_intrinsics.cy,
                "width": packet.nav_depth_intrinsics.width,
                "height": packet.nav_depth_intrinsics.height,
            },
            "T_world_camera": packet.T_world_nav_depth_camera.matrix.tolist(),
        }),
        "calibration_hash": packet.calibration_hash,
        "novelty": float(novelty),
        "protected_reason": protected_reason,
        "rgb_dtype": "uint8", "depth_dtype": "float32",
        "extra": extra_metadata or {},
    }


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _package_hash(files: dict[str, str]) -> str:
    body = "\n".join(f"{name}:{files[name]}" for name in sorted(files)).encode("utf-8")
    return _sha256(body)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_temp_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            _remove_temp_tree(child)
        else:
            child.unlink()
    path.rmdir()
