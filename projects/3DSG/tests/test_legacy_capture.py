from __future__ import annotations

import json

import numpy as np
import pytest

from exploration.contracts import CameraIntrinsics, FramePacket, Pose3
from exploration.graph_bridge import GraphObservation
from exploration.habitat_online import RGBFrame
from exploration.keyframe_scheduler import PersistedKeyframe
from exploration.legacy_capture import LegacyCaptureStore
from exploration.legacy_mapper_packets import (
    LegacyMapperPacketError,
    OpenCLIPFeatureArtifactStore,
    build_current_mapper_packet,
)


def test_legacy_capture_export_preserves_frame_identity_and_camera_pose(tmp_path) -> None:
    rotation = np.array([[0.0, 0.5, np.sqrt(3.0) / 2.0],
                         [1.0, 0.0, 0.0],
                         [0.0, np.sqrt(3.0) / 2.0, -0.5]])
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = (1.0, 2.0, 1.5)
    rgb = np.full((2, 3, 3), 17, dtype=np.uint8)
    depth = np.full((2, 3), 2.0, dtype=np.float32)
    intrinsics = CameraIntrinsics(2.0, 2.0, 1.0, 0.5, 3, 2)
    packet = FramePacket(
        run_id="run", scene_id="scene", frame_id="run:00000003", frame_index=3,
        simulator_time_s=0.12, host_monotonic_s=1.2, depth_m=depth,
        intrinsics=intrinsics, T_world_camera=Pose3(transform), floor_z_m=0.0,
        calibration_hash="calibration", depth_min_m=0.1, depth_max_m=10.0,
        capture_wall_time_unix_s=1_700_000_000.0,
        capture_monotonic_domain="host_monotonic:test-boot-id",
    )
    keyframe = PersistedKeyframe(
        run_id="run", scene_id="scene", frame_id=packet.frame_id, frame_index=3,
        host_monotonic_s=1.2, payload_ref=str(tmp_path / "keyframe"), payload_bytes=1,
        payload_sha256="0" * 64, novelty=0.5, metadata={"robot_pose": [1.0, 2.0, 0.3]},
    )
    rgb_frame = RGBFrame("run", "scene", packet.frame_id, 3, 0.12, 1.2, rgb)
    observation = GraphObservation(keyframe, rgb_frame, packet)
    store = LegacyCaptureStore(tmp_path / "legacy_capture", scene_file="scene.glb")

    from exploration.keyframe_store import KeyframeStore
    keyframe_store = KeyframeStore(tmp_path / "run")
    persisted = keyframe_store.persist(rgb_frame, packet, novelty=0.5)
    _, restored_packet = keyframe_store.load(persisted)
    assert restored_packet.capture_wall_time_unix_s == packet.capture_wall_time_unix_s
    assert restored_packet.capture_monotonic_domain == packet.capture_monotonic_domain

    record = store.export(observation)
    root = tmp_path / "legacy_capture"
    metadata = json.loads((root / record["frame_dir"] / "metadata.json").read_text())
    habitat_sensor = np.eye(4)
    habitat_sensor[:3, :3] = np.asarray(metadata["sensor_rotation_habitat"])
    habitat_sensor[:3, 3] = metadata["sensor_position_habitat"]
    optical_from_sensor = np.diag([1.0, -1.0, -1.0, 1.0])
    reconstructed = (np.asarray(metadata["canonical_from_habitat"])
                     @ habitat_sensor @ optical_from_sensor)

    assert record["frame_id"] == packet.frame_id
    assert record["frame_index"] == packet.frame_index
    assert record["sequence_id"] == packet.run_id
    assert np.allclose(reconstructed, packet.T_world_camera.matrix, atol=1e-8)
    assert np.array_equal(np.load(root / record["frame_dir"] / "rgb.npy"), rgb)
    assert np.load(root / record["frame_dir"] / "valid_mask.npy").all()
    assert record["timestamp_clock_domain"] == "habitat_simulation"
    assert record["capture_monotonic_s"] == packet.host_monotonic_s
    assert record["raw_files_sha256"] == record["source_files_sha256"]
    assert set(record["raw_files_sha256"]) == {
        "rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"}


def test_current_mapper_packet_joins_frame_clocks_hashes_and_all_perception_rows(tmp_path) -> None:
    transform = np.eye(4)
    rgb = np.full((2, 3, 3), 17, dtype=np.uint8)
    depth = np.full((2, 3), 2.0, dtype=np.float32)
    packet = FramePacket(
        run_id="run", scene_id="scene", frame_id="run:00000003", frame_index=3,
        simulator_time_s=0.12, host_monotonic_s=1.2, depth_m=depth,
        intrinsics=CameraIntrinsics(2.0, 2.0, 1.0, 0.5, 3, 2),
        T_world_camera=Pose3(transform), floor_z_m=0.0,
        calibration_hash="calibration", depth_min_m=0.1, depth_max_m=10.0,
        capture_wall_time_unix_s=1_700_000_000.0,
        capture_monotonic_domain="host_monotonic:test-boot-id",
    )
    keyframe = PersistedKeyframe(
        run_id="run", scene_id="scene", frame_id=packet.frame_id, frame_index=3,
        host_monotonic_s=1.2, payload_ref=str(tmp_path / "keyframe"), payload_bytes=1,
        payload_sha256="0" * 64, novelty=0.5, metadata={"robot_pose": [1.0, 2.0, 0.3]},
    )
    observation = GraphObservation(keyframe, RGBFrame(
        "run", "scene", packet.frame_id, 3, 0.12, 1.2, rgb), packet)
    capture_store = LegacyCaptureStore(tmp_path / "capture", scene_file="scene.glb")
    capture_record = capture_store.export(observation)

    mask_root = tmp_path / "assets" / "detection_2d"
    mask_root.mkdir(parents=True)
    mask_path = mask_root / "mask.npy"
    with mask_path.open("wb") as stream:
        np.save(stream, np.ones((2, 3), dtype=bool), allow_pickle=False)
    import hashlib
    mask_hash = hashlib.sha256(mask_path.read_bytes()).hexdigest()
    observation_id = f"{packet.frame_id}:d0000"
    feature_vector = np.zeros(512, dtype=np.float32)
    feature_vector[0] = 1.0
    features = OpenCLIPFeatureArtifactStore(tmp_path / "features").persist(3, [{
        "observation_id": observation_id,
        "image_feature": feature_vector,
        "text_feature": feature_vector,
    }])

    mapper_packet = build_current_mapper_packet(
        observation=observation, capture_root=capture_store.root,
        capture_record=capture_record, asset_root=tmp_path / "assets",
        p2_detections=[{"detection_id": observation_id, "mask_file": "mask.npy",
                        "mask_sha256": mask_hash}],
        p3_detections=[{"detection_id": observation_id, "status": "rejected"}],
        features=features,
    )
    assert mapper_packet["frame_id"] == capture_record["frame_id"]
    assert mapper_packet["capture_record"]["raw_files_sha256"] == mapper_packet["source_hashes"]
    assert mapper_packet["p2_detections"][0]["mask_file"] == str(mask_path)
    assert mapper_packet["features"][0]["observation_id"] == observation_id

    mismatched = dict(capture_record, frame_index=4)
    with pytest.raises(LegacyMapperPacketError, match="frame_index"):
        build_current_mapper_packet(
            observation=observation, capture_root=capture_store.root,
            capture_record=mismatched, asset_root=tmp_path / "assets",
            p2_detections=[{"detection_id": observation_id, "mask_file": "mask.npy",
                            "mask_sha256": mask_hash}],
            p3_detections=[{"detection_id": observation_id, "status": "rejected"}],
            features=features,
        )
