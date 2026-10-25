"""Client-side codec for the proposed OccuSG M2 binary batch protocol.

This module only defines/validates wire bytes. It does not connect to OccuSG,
integrate OctoMap, or claim native map parity.
"""

from __future__ import annotations

import hashlib
import math
import struct

import numpy as np


MAGIC = b"3SGM"
VERSION = 2
OP_INTEGRATE_PROJECT = 1
HEADER = struct.Struct("<4sHHQQII")
INTEGRATE_METADATA = struct.Struct("<d3fI")
INTEGRATE_IDENTITY = struct.Struct("<QQH")
PROJECTION_MODE = struct.Struct("<B")
MAX_FRAME_ID_BYTES = 1024
DEFAULT_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
PROJECTION_MODES = {
    "full_refresh": 1,
    "partial_update": 2,
    "clean_full_refresh": 3,
}


def encode_integrate_project(
    *, xyz: np.ndarray, has_hit: np.ndarray, origin_xyz: np.ndarray,
    max_range_m: float, request_id: int, frame_id: str, frame_index: int,
    capture_monotonic_ns: int, package_sha256: str,
    projection_mode: str = "full_refresh",
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> bytes:
    """Encode one bounded M2 project/integrate request for parity fixtures."""
    points = np.ascontiguousarray(xyz, dtype="<f4")
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("xyz must have shape (N, 3)")
    if not np.isfinite(points).all():
        raise ValueError("xyz contains non-finite values")
    count = int(points.shape[0])
    if count > 0xFFFFFFFF:
        raise ValueError("point count exceeds uint32")
    hit = np.asarray(has_hit)
    if hit.dtype != np.bool_ or hit.shape != (count,):
        raise ValueError("has_hit must be a bool array with shape (N,)")
    origin = np.asarray(origin_xyz, dtype=np.float64)
    if origin.shape != (3,) or not np.isfinite(origin).all():
        raise ValueError("origin_xyz must contain three finite values")
    if not np.isfinite(origin.astype(np.float32)).all():
        raise ValueError("origin_xyz exceeds finite float32 range")
    max_range = float(max_range_m)
    if not math.isfinite(max_range) or max_range <= 0:
        raise ValueError("max_range_m must be finite and positive")
    for field, value in (("request_id", request_id), ("frame_index", frame_index),
                         ("capture_monotonic_ns", capture_monotonic_ns)):
        if not isinstance(value, int) or value < 0 or value > 0xFFFFFFFFFFFFFFFF:
            raise ValueError(f"{field} must fit uint64")
    if not isinstance(frame_id, str):
        raise ValueError("frame_id must be text")
    encoded_frame_id = frame_id.encode("utf-8")
    if not encoded_frame_id or len(encoded_frame_id) > MAX_FRAME_ID_BYTES:
        raise ValueError("frame_id must contain 1..1024 UTF-8 bytes")
    try:
        package_digest = bytes.fromhex(package_sha256)
    except ValueError as exc:
        raise ValueError("package_sha256 must be a SHA-256 hex digest") from exc
    if len(package_digest) != hashlib.sha256().digest_size:
        raise ValueError("package_sha256 must be a SHA-256 hex digest")
    try:
        mode = PROJECTION_MODES[projection_mode]
    except KeyError as exc:
        raise ValueError(f"unsupported projection mode: {projection_mode}") from exc

    hit_bits = np.packbits(hit, bitorder="little").tobytes()
    metadata = INTEGRATE_METADATA.pack(
        max_range, *origin.astype(np.float32).tolist(), count)
    identity = INTEGRATE_IDENTITY.pack(
        frame_index, capture_monotonic_ns, len(encoded_frame_id))
    payload = (metadata + PROJECTION_MODE.pack(mode) + identity + encoded_frame_id
               + package_digest + points.tobytes() + hit_bits)
    if len(payload) > max_payload_bytes:
        raise ValueError("M2 payload exceeds configured limit")
    header = HEADER.pack(MAGIC, VERSION, OP_INTEGRATE_PROJECT, request_id,
                         len(payload), 0, 0)
    return header + payload


def decode_integrate_project(
    packet: bytes, *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> dict:
    """Decode and validate a request packet, returning arrays backed by packet."""
    if len(packet) < HEADER.size:
        raise ValueError("truncated M2 envelope")
    magic, version, opcode, request_id, payload_len, flags, reserved = HEADER.unpack_from(packet)
    if magic != MAGIC or version != VERSION or opcode != OP_INTEGRATE_PROJECT:
        raise ValueError("unsupported M2 envelope identity")
    if flags != 0 or reserved != 0:
        raise ValueError("nonzero M2 envelope flags/reserved bits")
    if payload_len > max_payload_bytes or payload_len != len(packet) - HEADER.size:
        raise ValueError("M2 envelope payload length is invalid")
    offset = HEADER.size
    fixed = INTEGRATE_METADATA.size + PROJECTION_MODE.size + INTEGRATE_IDENTITY.size
    if payload_len < fixed + 1 + 32:
        raise ValueError("truncated M2 integrate metadata")
    max_range_m, ox, oy, oz, point_count = INTEGRATE_METADATA.unpack_from(packet, offset)
    offset += INTEGRATE_METADATA.size
    mode_value, = PROJECTION_MODE.unpack_from(packet, offset)
    offset += PROJECTION_MODE.size
    mode_by_value = {value: name for name, value in PROJECTION_MODES.items()}
    if mode_value not in mode_by_value:
        raise ValueError("unknown M2 projection mode")
    frame_index, capture_monotonic_ns, frame_id_length = INTEGRATE_IDENTITY.unpack_from(packet, offset)
    offset += INTEGRATE_IDENTITY.size
    if not 1 <= frame_id_length <= MAX_FRAME_ID_BYTES:
        raise ValueError("invalid M2 frame_id length")
    frame_end = offset + frame_id_length
    digest_end = frame_end + 32
    point_end = digest_end + point_count * 3 * 4
    hit_bytes = (point_count + 7) // 8
    expected_end = point_end + hit_bytes
    if expected_end != len(packet):
        raise ValueError("M2 point count does not match payload size")
    try:
        frame_id = packet[offset:frame_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("M2 frame_id is not valid UTF-8") from exc
    if not frame_id:
        raise ValueError("M2 frame_id must not be empty")
    package_sha256 = packet[frame_end:digest_end].hex()
    xyz = np.frombuffer(packet, dtype="<f4", count=point_count * 3,
                        offset=digest_end).reshape(point_count, 3)
    origin_xyz = np.asarray((ox, oy, oz), dtype=np.float32)
    if (not np.isfinite(xyz).all() or not np.isfinite(origin_xyz).all()
            or not math.isfinite(max_range_m) or max_range_m <= 0):
        raise ValueError("M2 scan contains invalid geometry metadata")
    packed_hits = np.frombuffer(packet, dtype=np.uint8, count=hit_bytes, offset=point_end)
    if point_count % 8 and hit_bytes:
        unused_mask = (0xFF << (point_count % 8)) & 0xFF
        if int(packed_hits[-1]) & unused_mask:
            raise ValueError("M2 hit bitset has nonzero padding bits")
    has_hit = np.unpackbits(packed_hits, bitorder="little", count=point_count).astype(bool)
    return {
        "request_id": request_id,
        "max_range_m": max_range_m,
        "origin_xyz": origin_xyz,
        "projection_mode": mode_by_value[mode_value],
        "frame_id": frame_id,
        "frame_index": frame_index,
        "capture_monotonic_ns": capture_monotonic_ns,
        "package_sha256": package_sha256,
        "xyz": xyz,
        "has_hit": has_hit,
    }
