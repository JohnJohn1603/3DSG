"""Run-local RGB/mask evidence assets for camera-live D2 revisions."""
from __future__ import annotations

import binascii
import hashlib
import json
import math
import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


CAPTURE_RECORD_SCHEMA = "camera-live-capture-record/v2"
ARRAY_HASH_KEYS = ("rgb.npy", "depth_m.npy", "valid_mask.npy")
CAPTURE_FILE_HASH_KEYS = frozenset((*ARRAY_HASH_KEYS, "metadata.json"))
WALL_CLOCK_DOMAIN = "unix_epoch_utc"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)


def _png_bytes(rgb: np.ndarray) -> bytes:
    image = np.asarray(rgb)
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("live evidence panel must be uint8 HxWx3 RGB")
    height, width = image.shape[:2]
    rows = b"".join(b"\x00" + image[y].tobytes() for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(rows, level=6))
            + _png_chunk(b"IEND", b""))


def _atomic_write_new(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite live evidence asset: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _asset_name(identity: str, kind: str) -> str:
    return f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}_{kind}.png"


def _validate_capture_contract(*, run_root: Path, run_id: str, scene_id: str,
                               sequence_id: str, frame_id: str, frame_index: int,
                               timestamp_s: float, timestamp_clock_domain: str,
                               capture_wall_time_unix_s: float,
                               capture_monotonic_s: float,
                               capture_monotonic_domain: str,
                               frame_dir: Path, capture_record: Mapping[str, Any],
                               source_hashes: Mapping[str, str],
                               allow_legacy_unversioned_record: bool) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate one versioned capture record and its on-disk source files.

    V2 makes ``capture_record.raw_files_sha256`` authoritative for all four
    files.  The packet-level ``source_hashes`` is a required exact duplicate;
    metadata binds the three arrays but cannot bind its own file hash.  The
    one explicit legacy mode exists only for immutable artifact regressions of
    the original T4 writer, whose unversioned record has the same authoritative
    full hash map and packet duplicate.
    """
    run_root = Path(run_root).resolve()
    capture_root = (run_root / "capture").resolve()
    frame_dir = Path(frame_dir).resolve()
    if not frame_dir.is_relative_to(capture_root):
        raise ValueError("capture frame path is outside this run's capture root")
    if not isinstance(capture_record, Mapping):
        raise ValueError("live evidence requires a capture_record")

    record_schema = capture_record.get("schema")
    legacy = record_schema is None and allow_legacy_unversioned_record
    if legacy:
        if "run_id" in capture_record:
            raise ValueError("legacy capture record unexpectedly contains a run_id")
    elif record_schema != CAPTURE_RECORD_SCHEMA:
        raise ValueError(f"unsupported capture record schema: {record_schema!r}")
    elif capture_record.get("run_id") != run_id:
        raise ValueError("capture record run_id differs from live evidence request")

    relative_frame_dir = capture_record.get("frame_dir")
    if not isinstance(relative_frame_dir, str) or not relative_frame_dir:
        raise ValueError("capture record frame_dir is required")
    relative_path = Path(relative_frame_dir)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("capture record frame_dir must be run-relative and cannot traverse")
    resolved_from_record = (capture_root / relative_path).resolve()
    if resolved_from_record != frame_dir:
        raise ValueError("capture record frame_dir does not resolve to the supplied capture directory")

    identities = {"frame_id": frame_id, "frame_index": frame_index,
                  "scene_id": scene_id, "sequence_id": sequence_id}
    if not legacy:
        identities["run_id"] = run_id
    for key, expected in identities.items():
        if capture_record.get(key) != expected:
            raise ValueError(f"capture record {key} differs from live evidence request")

    expected_clocks = {
        "timestamp_s": float(timestamp_s),
        "timestamp_clock_domain": timestamp_clock_domain,
        "capture_wall_time_unix_s": float(capture_wall_time_unix_s),
        "capture_monotonic_s": float(capture_monotonic_s),
        "capture_monotonic_domain": capture_monotonic_domain,
    }
    if not legacy:
        expected_clocks["capture_wall_time_clock_domain"] = WALL_CLOCK_DOMAIN
    for key, expected in expected_clocks.items():
        if capture_record.get(key) != expected:
            raise ValueError(f"capture record {key} differs from live evidence request")

    record_hashes = capture_record.get("raw_files_sha256")
    if not isinstance(record_hashes, Mapping) or set(record_hashes) != CAPTURE_FILE_HASH_KEYS:
        raise ValueError("capture record raw_files_sha256 must bind RGB, depth, valid mask, and metadata")
    normalized_record_hashes: dict[str, str] = {}
    for name, digest in record_hashes.items():
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"capture record has invalid SHA-256 for {name}")
        normalized_record_hashes[name] = digest
    if set(source_hashes) != CAPTURE_FILE_HASH_KEYS:
        raise ValueError("packet source_hashes must bind exactly the four current capture files")
    packet_hashes = dict(source_hashes)
    if packet_hashes != normalized_record_hashes:
        raise ValueError("packet source_hashes conflict with authoritative capture_record hashes")
    for name, expected in normalized_record_hashes.items():
        path = frame_dir / name
        if not path.is_file() or _sha(path) != expected:
            raise ValueError(f"current capture source hash mismatch: {path}")

    metadata = json.loads((frame_dir / "metadata.json").read_text(encoding="utf-8"))
    for key, expected in identities.items():
        if key == "run_id" and legacy:
            if metadata.get(key) is not None and metadata.get(key) != expected:
                raise ValueError("legacy capture metadata run_id conflicts with the manifest-bound run")
            continue
        if metadata.get(key) != expected:
            raise ValueError(f"current capture metadata {key} differs from capture record")
    for key, expected in expected_clocks.items():
        if metadata.get(key) != expected:
            raise ValueError(f"current capture metadata {key} differs from capture record")

    metadata_array_hashes = metadata.get("raw_files_sha256")
    if legacy and metadata_array_hashes is None:
        # The old writer rewrote metadata and deliberately omitted all hashes;
        # in this one read-only regression mode the external capture record is
        # authoritative. Never search or substitute another hash location.
        pass
    else:
        if (not isinstance(metadata_array_hashes, Mapping)
                or set(metadata_array_hashes) != set(ARRAY_HASH_KEYS)):
            raise ValueError("capture metadata raw_files_sha256 must bind exactly the three array sources")
        if dict(metadata_array_hashes) != {key: normalized_record_hashes[key] for key in ARRAY_HASH_KEYS}:
            raise ValueError("capture metadata array hashes conflict with capture_record hashes")
    if not legacy and capture_record.get("schema") != metadata.get("capture_record_schema"):
        raise ValueError("capture metadata schema binding differs from capture record")
    return metadata, normalized_record_hashes


def publish_live_frame_evidence(*, run_root: Path, viewer_root: Path, run_id: str,
                                scene_id: str, sequence_id: str, frame_id: str,
                                frame_index: int, timestamp_s: float,
                                timestamp_clock_domain: str,
                                capture_wall_time_unix_s: float,
                                capture_monotonic_s: float,
                                capture_monotonic_domain: str,
                                frame_dir: Path, capture_record: Mapping[str, Any],
                                source_hashes: Mapping[str, str],
                                detections: Sequence[Mapping[str, Any]],
                                mask_asset_root: Path,
                                allow_legacy_unversioned_record: bool = False) -> dict[str, Any]:
    """Write immutable panels for one current-run capture and its P2 masks.

    All source identities/hashes are checked before panel publication. The
    return value is attached to exactly one live graph frame before the live
    index/head pointer advances.
    """
    run_root = Path(run_root).resolve()
    viewer_root = Path(viewer_root).resolve()
    frame_dir = Path(frame_dir).resolve()
    mask_asset_root = Path(mask_asset_root).resolve()
    evidence_root = (viewer_root / "evidence").resolve()
    if not viewer_root.is_relative_to(run_root):
        raise ValueError("live evidence viewer must belong to this run")
    if not frame_dir.is_relative_to(run_root / "capture"):
        raise ValueError("capture frame path is outside this run's capture root")
    if not mask_asset_root.is_relative_to(run_root / "packets" / "assets"):
        raise ValueError("live evidence mask root must belong to this run packet assets")
    if not isinstance(run_id, str) or not run_id or not isinstance(frame_id, str) or not frame_id:
        raise ValueError("live evidence requires run and frame IDs")
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        raise ValueError("live evidence frame_index must be a non-negative integer")
    if (not math.isfinite(float(timestamp_s)) or not math.isfinite(float(capture_wall_time_unix_s))
            or not math.isfinite(float(capture_monotonic_s))):
        raise ValueError("live evidence timestamps must be finite")
    if not timestamp_clock_domain or not capture_monotonic_domain:
        raise ValueError("live evidence timestamp clock domains are required")
    metadata, normalized_hashes = _validate_capture_contract(
        run_root=run_root, run_id=run_id, scene_id=scene_id, sequence_id=sequence_id,
        frame_id=frame_id, frame_index=frame_index, timestamp_s=timestamp_s,
        timestamp_clock_domain=timestamp_clock_domain,
        capture_wall_time_unix_s=capture_wall_time_unix_s,
        capture_monotonic_s=capture_monotonic_s,
        capture_monotonic_domain=capture_monotonic_domain,
        frame_dir=frame_dir, capture_record=capture_record,
        source_hashes=source_hashes,
        allow_legacy_unversioned_record=allow_legacy_unversioned_record)
    source_hashes = normalized_hashes
    clock_fields = {"timestamp_s": float(timestamp_s),
        "timestamp_clock_domain": timestamp_clock_domain,
        "capture_wall_time_unix_s": float(capture_wall_time_unix_s),
        "capture_wall_time_clock_domain": WALL_CLOCK_DOMAIN,
        "capture_monotonic_s": float(capture_monotonic_s),
        "capture_monotonic_domain": capture_monotonic_domain}
    rgb_source = frame_dir / "rgb.npy"
    rgb = np.asarray(np.load(rgb_source, allow_pickle=False))
    if rgb.ndim != 3 or rgb.shape[2] not in (3, 4) or rgb.dtype != np.uint8:
        raise ValueError("current capture RGB must be uint8 HxWx3/4")
    rgb = np.ascontiguousarray(rgb[..., :3])
    height, width = rgb.shape[:2]
    if (int(metadata.get("width", width)) != width or int(metadata.get("height", height)) != height):
        raise ValueError("current capture RGB dimensions differ from frame metadata")

    def publish_panel(identity: str, kind: str, pixels: np.ndarray, source_sha256: str) -> dict[str, str]:
        filename = _asset_name(identity, kind)
        target = evidence_root / filename
        payload = _png_bytes(pixels)
        _atomic_write_new(target, payload)
        return {"url": f"evidence/{filename}", "sha256": hashlib.sha256(payload).hexdigest(),
                "source_sha256": source_sha256, "width": int(pixels.shape[1]),
                "height": int(pixels.shape[0]), "encoding": "RGB PNG derived from current-run source"}

    camera_rgb = publish_panel(frame_id, "camera_rgb", rgb, source_hashes["rgb.npy"])
    observations: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for detection in detections:
        oid = str(detection.get("detection_id", ""))
        if not oid or not oid.startswith(frame_id + ":det-") or oid in seen_ids:
            raise ValueError(f"invalid or duplicate current-frame P2 observation ID: {oid!r}")
        seen_ids.add(oid)
        mask_rel = Path(str(detection.get("mask_file", "")))
        if mask_rel.is_absolute() or ".." in mask_rel.parts:
            raise ValueError(f"unsafe P2 mask path for {oid}")
        mask_path = (mask_asset_root / mask_rel).resolve()
        if not mask_path.is_relative_to(mask_asset_root) or not mask_path.is_file():
            raise FileNotFoundError(f"current-run P2 mask is missing or outside packet assets: {mask_path}")
        mask_sha = str(detection.get("mask_sha256", ""))
        if len(mask_sha) != 64 or _sha(mask_path) != mask_sha:
            raise ValueError(f"current-run P2 mask hash mismatch for {oid}")
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        if mask.shape != (height, width):
            raise ValueError(f"current-run RGB/mask dimensions differ for {oid}")
        mask_rgb = np.repeat((mask.astype(np.uint8) * 255)[..., None], 3, axis=2)
        overlay = rgb.copy()
        if mask.any():
            overlay[mask] = (0.50 * overlay[mask] + 0.50 * np.asarray([255, 48, 48])).astype(np.uint8)
            ys, xs = np.where(mask)
            pad = max(8, int(0.12 * max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)))
            x0, x1 = max(0, int(xs.min()) - pad), min(width, int(xs.max()) + pad + 1)
            y0, y1 = max(0, int(ys.min()) - pad), min(height, int(ys.max()) + pad + 1)
        else:
            x0, y0, x1, y1 = 0, 0, width, height
        identity = f"{frame_id}|{oid}"
        panels = {
            "rgb": camera_rgb,
            "mask": publish_panel(identity, "mask", mask_rgb, mask_sha),
            "overlay": publish_panel(identity, "overlay", overlay, source_hashes["rgb.npy"] + ":" + mask_sha),
            "crop": publish_panel(identity, "crop", overlay[y0:y1, x0:x1], source_hashes["rgb.npy"] + ":" + mask_sha),
        }
        score = float(detection.get("score_raw", 0.0))
        if not math.isfinite(score):
            raise ValueError(f"non-finite P2 score for {oid}")
        observations.append({"observation_id": oid, "frame_id": frame_id, "frame_index": frame_index,
            "raw_class": str(detection.get("class_raw", "unavailable")), "score_raw": score,
            "source_hashes": {"rgb.npy": source_hashes["rgb.npy"], "mask.npy": mask_sha},
            "mask_crop_xyxy": [x0, y0, x1, y1], "panels": panels})
    return {"schema": "camera-live-frame-evidence/v1", "capture_record_schema":
        capture_record.get("schema") or "legacy-t4-unversioned-capture/v1", "run_id": run_id,
        "scene_id": scene_id, "sequence_id": sequence_id, "frame_id": frame_id,
        "frame_index": frame_index, **clock_fields,
        "source_hashes": dict(source_hashes), "camera_rgb": camera_rgb,
        "observations": observations}
