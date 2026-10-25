import hashlib

import numpy as np
import pytest

from exploration.native_m2_wire import decode_integrate_project, encode_integrate_project


def _encode(**overrides):
    fields = {
        "xyz": np.asarray([[1.25, -2.5, 3.75], [4.0, 5.0, 6.0]], dtype=np.float32),
        "has_hit": np.asarray([True, False], dtype=np.bool_),
        "origin_xyz": np.asarray([0.1, 0.2, 0.3], dtype=np.float64),
        "max_range_m": 8.0,
        "request_id": 17,
        "frame_id": "run:帧-12",
        "frame_index": 12,
        "capture_monotonic_ns": 123456789,
        "package_sha256": hashlib.sha256(b"fixture").hexdigest(),
        "projection_mode": "partial_update",
    }
    fields.update(overrides)
    return encode_integrate_project(**fields)


def test_m2_v2_codec_roundtrips_identity_points_hit_flags_and_projection_mode():
    packet = _encode()
    decoded = decode_integrate_project(packet)

    assert decoded["request_id"] == 17
    assert decoded["frame_id"] == "run:帧-12"
    assert decoded["frame_index"] == 12
    assert decoded["capture_monotonic_ns"] == 123456789
    assert decoded["projection_mode"] == "partial_update"
    assert decoded["max_range_m"] == 8.0
    assert decoded["package_sha256"] == hashlib.sha256(b"fixture").hexdigest()
    np.testing.assert_array_equal(decoded["xyz"], [[1.25, -2.5, 3.75], [4.0, 5.0, 6.0]])
    np.testing.assert_array_equal(decoded["has_hit"], [True, False])
    np.testing.assert_array_equal(decoded["origin_xyz"], np.asarray([0.1, 0.2, 0.3], dtype=np.float32))


def test_m2_v2_decoder_rejects_payload_length_mismatch():
    packet = _encode()
    with pytest.raises(ValueError, match="payload length"):
        decode_integrate_project(packet[:-1])


def test_m2_v2_encoder_enforces_payload_limit():
    with pytest.raises(ValueError, match="configured limit"):
        _encode(max_payload_bytes=1)


def test_m2_v2_encoder_rejects_nonfinite_xyz():
    with pytest.raises(ValueError, match="non-finite"):
        _encode(xyz=np.asarray([[np.nan, 0.0, 0.0]], dtype=np.float32),
                has_hit=np.asarray([True], dtype=np.bool_))
