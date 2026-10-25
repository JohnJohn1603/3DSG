from __future__ import annotations

import pytest

from scripts.run_m12_webots_native_replay import _capture_replay_policy, _sha256_file


def _manifest(**overrides):
    value = {
        "webots_version": "R2025a",
        "capture_status": "complete",
        "status": "pass",
        "frame_count": 3,
        "detections": "absent",
        "regions_or_room_gt_in_capture_or_builder": False,
        "waypoint_count": 1,
        "waypoints_reached": 1,
    }
    value.update(overrides)
    return value


def test_complete_capture_remains_replayable():
    policy = _capture_replay_policy(_manifest(), allow_unsaved_route_capture=False)
    assert policy["output_capture_status"] == "complete"
    assert policy["route_replayable"] is True


def test_unsaved_keyboard_capture_is_explicitly_partial():
    policy = _capture_replay_policy(
        _manifest(
            capture_status="failed",
            status="error",
            error="no waypoint was recorded; press SPACE before Q",
            waypoint_count=0,
            waypoints_reached=0,
        ),
        allow_unsaved_route_capture=True,
    )
    assert policy["output_capture_status"] == "partial_unsaved_route"
    assert policy["route_replayable"] is False


def test_incomplete_capture_is_rejected_by_default():
    with pytest.raises(ValueError, match="allow-unsaved-route-capture"):
        _capture_replay_policy(
            _manifest(
                capture_status="failed",
                status="error",
                error="no waypoint was recorded; press SPACE before Q",
                waypoint_count=0,
                waypoints_reached=0,
            ),
            allow_unsaved_route_capture=False,
        )


def test_allow_flag_does_not_accept_an_unrelated_failure():
    with pytest.raises(ValueError, match="mismatched fields"):
        _capture_replay_policy(
            _manifest(
                capture_status="failed",
                status="error",
                error="controller crashed",
                waypoint_count=0,
                waypoints_reached=0,
            ),
            allow_unsaved_route_capture=True,
        )


def test_replay_fingerprint_is_content_based(tmp_path):
    artifact = tmp_path / "worker"
    artifact.write_bytes(b"worker-build-a")
    first = _sha256_file(artifact)
    artifact.write_bytes(b"worker-build-b")
    second = _sha256_file(artifact)
    assert first != second
