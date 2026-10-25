from __future__ import annotations

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from exploration.contracts import (
    CameraIntrinsics,
    GridSnapshot,
    Pose3,
    RobotPose2,
)
from exploration.scan_audit import InitialScanAuditRecorder


def _sample(frame_index: int, yaw_rad: float):
    intrinsics = CameraIntrinsics(2.0, 2.0, 1.0, 1.0, 2, 2)
    packet = SimpleNamespace(
        frame_id=f"run:{frame_index:08d}", frame_index=frame_index,
        simulator_time_s=frame_index * 0.04,
        host_monotonic_s=100.0 + frame_index * 0.04,
        depth_m=np.full((2, 2), 1.5, dtype=np.float32),
        intrinsics=intrinsics, T_world_camera=Pose3(np.eye(4)),
        floor_z_m=0.0, depth_min_m=0.1, depth_max_m=10.0,
    )
    rgb = SimpleNamespace(rgb=np.full((2, 2, 3), frame_index, dtype=np.uint8))
    snapshot = GridSnapshot(
        revision=frame_index + 1, as_of_frame_id=packet.frame_id,
        as_of_frame_index=frame_index, host_monotonic_s=packet.host_monotonic_s,
        resolution_m=0.05, origin_x_m=-0.05, origin_y_m=-0.05,
        states=np.zeros((2, 2), dtype=np.uint8),
        floor_support=np.zeros((2, 2), dtype=np.uint16),
        clearance_evidence=np.zeros((2, 2), dtype=np.uint16),
        spawn_validated=np.zeros((2, 2), dtype=bool),
        occupancy_log_odds=np.zeros((2, 2), dtype=np.float32),
    )
    pose = RobotPose2(0.0, 0.0, yaw_rad)
    return rgb, packet, pose, snapshot


def test_initial_scan_audit_keeps_one_paired_rgbd_sample_per_15_degree_sector(tmp_path) -> None:
    recorder = InitialScanAuditRecorder(tmp_path / "initial_scan", run_id="run")
    rgb, packet, pose, snapshot = _sample(7, 0.0)
    first = recorder.record(rgb=rgb, packet=packet, robot_pose=pose,
                            snapshot=snapshot, elapsed_s=0.5)
    same_sector = _sample(8, math.radians(8))
    skipped = recorder.record(rgb=same_sector[0], packet=same_sector[1],
                              robot_pose=same_sector[2], snapshot=same_sector[3],
                              elapsed_s=0.6)
    next_sector = _sample(9, math.radians(20))
    second = recorder.record(rgb=next_sector[0], packet=next_sector[1],
                             robot_pose=next_sector[2], snapshot=next_sector[3],
                             elapsed_s=0.7)

    assert first is not None and second is not None
    assert first["yaw_bin"] == second["yaw_bin"] - 1
    assert skipped is None
    assert recorder.sample_count == 2
    assert np.load(tmp_path / "initial_scan" / first["rgb_file"]).shape == (2, 2, 3)
    assert np.load(tmp_path / "initial_scan" / first["depth_file"]).shape == (2, 2)

    recorder.close(status="completed")
    manifest = json.loads((tmp_path / "initial_scan" / "manifest.json").read_text())
    assert manifest["schema"] == "initial-scan-rgbd-audit/v1"
    assert manifest["status"] == "completed"
    assert manifest["yaw_bin_count"] == 24
    assert [row["frame_index"] for row in manifest["samples"]] == [7, 9]
    assert manifest["samples"][0]["nav_revision"] == 8
    assert manifest["samples"][0]["T_world_camera"] == np.eye(4).tolist()


def test_initial_scan_audit_enforces_payload_cap(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(InitialScanAuditRecorder, "MAX_BYTES", 1)
    recorder = InitialScanAuditRecorder(tmp_path, run_id="run")
    rgb, packet, pose, snapshot = _sample(0, -math.pi)

    sample = recorder.record(rgb=rgb, packet=packet, robot_pose=pose,
                             snapshot=snapshot, elapsed_s=0.0)
    manifest = json.loads((tmp_path / "manifest.json").read_text())

    assert sample is None
    assert recorder.sample_count == 0
    assert manifest["status"] == "byte_cap_reached"
    assert manifest["bytes_written"] == 0


def test_initial_scan_audit_rejects_snapshot_from_another_frame(tmp_path) -> None:
    recorder = InitialScanAuditRecorder(tmp_path, run_id="run")
    rgb, packet, pose, snapshot = _sample(0, 0.0)
    mismatched = replace(snapshot, as_of_frame_id="run:older-frame")

    with pytest.raises(ValueError, match="NavMap revision for the sampled frame"):
        recorder.record(rgb=rgb, packet=packet, robot_pose=pose,
                        snapshot=mismatched, elapsed_s=0.0)
