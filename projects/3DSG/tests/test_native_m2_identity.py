from __future__ import annotations

import hashlib
import json

import pytest

from scripts.native_occusg_worker import _m2_identity


def _packet() -> dict:
    return {
        "frame_id": "run-a:00000146",
        "frame_index": 146,
        "capture_monotonic_s": 12.345678901,
        "capture_monotonic_domain": "host_monotonic:boot-a",
        "source_hashes": {"rgb.npy": "a" * 64, "depth_m.npy": "b" * 64},
    }


def test_m2_identity_binds_sorted_source_and_sensor_hashes() -> None:
    packet = _packet()
    identity = _m2_identity(
        packet,
        sensor_id="navigation_depth_camera",
        additional_hashes={"depth_file_sha256": "c" * 64},
    )
    canonical = json.dumps(
        {
            "source_hashes": {"depth_m.npy": "b" * 64, "rgb.npy": "a" * 64},
            "sensor_id": "navigation_depth_camera",
            "additional_hashes": {"depth_file_sha256": "c" * 64},
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert identity["m2_frame_id"] == packet["frame_id"]
    assert identity["m2_frame_index"] == 146
    assert identity["m2_capture_monotonic_ns"] == 12_345_678_901
    assert identity["m2_capture_monotonic_domain"] == "host_monotonic:boot-a"
    assert identity["m2_package_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert identity["m2_sensor_id"] == "navigation_depth_camera"


def test_m2_identity_changes_for_auxiliary_calibration_or_sensor() -> None:
    packet = _packet()
    primary = _m2_identity(packet, sensor_id="primary")
    auxiliary = _m2_identity(
        packet, sensor_id="navigation_depth_camera",
        additional_hashes={"calibration_sha256": "d" * 64},
    )
    assert primary["m2_package_sha256"] != auxiliary["m2_package_sha256"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_hashes", {}, "source frame hashes"),
        ("capture_monotonic_s", None, "capture_monotonic_s"),
        ("capture_monotonic_domain", "simulation", "clock domain"),
    ],
)
def test_m2_identity_rejects_missing_provenance(field: str, value, message: str) -> None:
    packet = _packet()
    packet[field] = value
    with pytest.raises(ValueError, match=message):
        _m2_identity(packet, sensor_id="primary")
