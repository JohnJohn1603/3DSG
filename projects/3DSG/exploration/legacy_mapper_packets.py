"""Build provenance-checked current-frame packets for the legacy mapper workers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

import numpy as np

from .graph_bridge import GraphObservation


class LegacyMapperPacketError(ValueError):
    """A current keyframe cannot be safely dispatched to a legacy mapper."""


class OpenCLIPFeatureArtifactStore:
    """Persist the exact per-observation vectors expected by ConceptGraphs."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def persist(self, frame_index: int,
                feature_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        records = []
        seen: set[str] = set()
        for row in feature_rows:
            observation_id = str(row.get("observation_id", ""))
            if not observation_id or observation_id in seen:
                raise LegacyMapperPacketError("OpenCLIP feature IDs must be present and unique")
            seen.add(observation_id)
            image = _unit_feature(row.get("image_feature"), observation_id, "image")
            text = _unit_feature(row.get("text_feature"), observation_id, "text")
            suffix = hashlib.sha256(observation_id.encode("utf-8")).hexdigest()[:16]
            target = self.root / f"frame_{int(frame_index):08d}_{suffix}.npz"
            if target.exists():
                with np.load(target, allow_pickle=False) as archive:
                    if (not np.array_equal(archive["image_feature"], image)
                            or not np.array_equal(archive["text_feature"], text)):
                        raise LegacyMapperPacketError(f"feature artifact conflicts with prior data: {target}")
            else:
                descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=self.root)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        np.savez_compressed(stream, image_feature=image, text_feature=text)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                    _fsync_directory(self.root)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            records.append({"observation_id": observation_id,
                            "feature_path": str(target),
                            "feature_file_sha256": _sha256_file(target)})
        return records


def build_current_mapper_packet(*, observation: GraphObservation,
                                capture_root: str | Path,
                                capture_record: Mapping[str, Any],
                                asset_root: str | Path,
                                p2_detections: Sequence[Mapping[str, Any]],
                                p3_detections: Sequence[Mapping[str, Any]],
                                features: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Create one live-worker packet and reject any cross-frame/missing join."""
    packet = observation.packet
    keyframe = observation.keyframe
    identity = (keyframe.run_id, keyframe.scene_id, keyframe.frame_id, keyframe.frame_index)
    if identity != (packet.run_id, packet.scene_id, packet.frame_id, packet.frame_index):
        raise LegacyMapperPacketError("observation and RGB-D packet identity differ")
    if packet.capture_wall_time_unix_s is None or packet.capture_monotonic_domain is None:
        raise LegacyMapperPacketError("source capture clocks are required for mapper dispatch")

    record = dict(capture_record)
    expected_clocks = {
        "frame_id": packet.frame_id,
        "frame_index": packet.frame_index,
        "timestamp_s": float(packet.simulator_time_s),
        "timestamp_clock_domain": "habitat_simulation",
        "capture_wall_time_unix_s": float(packet.capture_wall_time_unix_s),
        "capture_monotonic_s": float(packet.host_monotonic_s),
        "capture_monotonic_domain": str(packet.capture_monotonic_domain),
        "scene_id": packet.scene_id,
        "sequence_id": packet.run_id,
    }
    for field, expected in expected_clocks.items():
        if record.get(field) != expected:
            raise LegacyMapperPacketError(f"capture record {field} does not match keyframe")
    capture_hashes = record.get("raw_files_sha256")
    if not isinstance(capture_hashes, Mapping) or not capture_hashes:
        raise LegacyMapperPacketError("capture record requires raw_files_sha256")
    capture_dir = Path(capture_root).resolve() / str(record.get("frame_dir", ""))
    for name, expected in capture_hashes.items():
        path = capture_dir / str(name)
        if not path.is_file() or _sha256_file(path) != str(expected):
            raise LegacyMapperPacketError(f"capture source file missing/hash mismatch: {name}")

    p2 = [dict(row) for row in p2_detections]
    p3 = [dict(row) for row in p3_detections]
    feature_rows = [dict(row) for row in features]
    p2_ids = _unique_ids(p2, "detection_id", "P2")
    p3_ids = _unique_ids(p3, "detection_id", "P3")
    feature_ids = _unique_ids(feature_rows, "observation_id", "OpenCLIP")
    if any(not observation_id.startswith(packet.frame_id + ":") for observation_id in p2_ids):
        raise LegacyMapperPacketError("P2 observation ID does not belong to the current frame")
    if p2_ids != p3_ids or p2_ids != feature_ids:
        raise LegacyMapperPacketError("P2, P3 and OpenCLIP rows must cover identical observation IDs")

    mask_root = Path(asset_root).resolve() / "detection_2d"
    for detection in p2:
        mask_value = Path(str(detection.get("mask_file", "")))
        mask_path = mask_value if mask_value.is_absolute() else mask_root / mask_value
        expected = str(detection.get("mask_sha256", ""))
        if not expected or not mask_path.is_file() or _sha256_file(mask_path) != expected:
            raise LegacyMapperPacketError(f"P2 mask missing/hash mismatch: {detection.get('detection_id')}")
        detection["mask_file"] = str(mask_path.resolve())

    for feature in feature_rows:
        feature_path = Path(str(feature.get("feature_path", ""))).resolve()
        expected = str(feature.get("feature_file_sha256", ""))
        if not expected or not feature_path.is_file() or _sha256_file(feature_path) != expected:
            raise LegacyMapperPacketError(f"OpenCLIP feature missing/hash mismatch: {feature.get('observation_id')}")
        with np.load(feature_path, allow_pickle=False) as archive:
            for name in ("image_feature", "text_feature"):
                _unit_feature(archive[name], str(feature["observation_id"]), name)
        feature["feature_path"] = str(feature_path)

    raw_hashes = {str(name): str(value).lower() for name, value in capture_hashes.items()}
    if record.get("source_files_sha256", raw_hashes) != raw_hashes:
        raise LegacyMapperPacketError("capture source hash aliases disagree")
    record["raw_files_sha256"] = raw_hashes
    return {
        "schema": "3dsg-current-mapper-packet/v1",
        "run_id": packet.run_id,
        "scene_id": packet.scene_id,
        "sequence_id": packet.run_id,
        "frame_id": packet.frame_id,
        "frame_index": int(packet.frame_index),
        "timestamp_s": float(packet.simulator_time_s),
        "timestamp_clock_domain": "habitat_simulation",
        "capture_wall_time_unix_s": float(packet.capture_wall_time_unix_s),
        "capture_monotonic_s": float(packet.host_monotonic_s),
        "capture_monotonic_domain": str(packet.capture_monotonic_domain),
        "capture_record": record,
        "capture_root": str(Path(capture_root).resolve()),
        "asset_root": str(Path(asset_root).resolve()),
        "source_hashes": raw_hashes,
        "p2_detections": p2,
        "p3_detections": p3,
        "features": feature_rows,
    }


def _unique_ids(rows: Sequence[Mapping[str, Any]], field: str, stage: str) -> set[str]:
    ids = [str(row.get(field, "")) for row in rows]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise LegacyMapperPacketError(f"{stage} observation IDs must be present and unique")
    return set(ids)


def _unit_feature(value: Any, observation_id: str, kind: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float32)
    if (vector.shape != (512,) or not np.isfinite(vector).all()
            or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5, rtol=1e-5)):
        raise LegacyMapperPacketError(f"invalid {kind} 512-D unit feature for {observation_id}")
    return vector


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
