import numpy as np
import pytest

from standalone_sg.objects.object_geometry_fusion import EvidenceConflictError, ObjectGeometryFusion
from scripts.run_object_fusion_offline import fusion_admission


def _apply(engine, observation_id="obs-a", object_id="track-1", points=None, pose=None):
    xyz = np.asarray(points if points is not None else [[-0.001, 0.001, 0.0], [0.004, 0.002, 0.0]], dtype=np.float32)
    return engine.apply(object_id=object_id, observation_id=observation_id, timestamp_s=1.0,
                        xyz_W=xyz, rgb_u8=np.asarray([[10, 20, 30]] * len(xyz), dtype=np.uint8),
                        pixel_rc=np.asarray([[0, i] for i in range(len(xyz))], dtype=np.int32),
                        T_W_Copt=np.asarray(pose if pose is not None else np.eye(4), dtype=np.float64))


def test_negative_coordinates_use_floor_voxel_keys_and_snapshot_bounds():
    engine = ObjectGeometryFusion(voxel_size_m=0.02)
    _apply(engine)
    state = engine.snapshot("track-1")
    assert state["voxel_keys"].tolist() == [[-1, 0, 0], [0, 0, 0]]
    assert state["voxel_count"] == 2
    assert state["observation_count"] == 1


def test_prior_matcher_geometry_uses_voxel_representative_min_max_formula():
    engine = ObjectGeometryFusion(voxel_size_m=0.02)
    _apply(engine, points=[[-0.001, 0.001, 0.0], [0.044, 0.022, 0.061]])
    geometry = engine.prior_matcher_geometry("track-1")
    assert geometry["geometry_source"] == "prior_accumulated_surface_voxel_representatives"
    assert np.allclose(geometry["bounds_min_W"], [-0.001, 0.001, 0.0])
    assert np.allclose(geometry["bounds_max_W"], [0.044, 0.022, 0.061])
    assert np.allclose(geometry["center_W"], (geometry["bounds_min_W"] + geometry["bounds_max_W"]) / 2)
    assert np.allclose(geometry["extents_m"], geometry["bounds_max_W"] - geometry["bounds_min_W"])
    assert geometry["prior_revision"] == 1
    assert geometry["evidence_ids"] == ["obs-a"]


def test_same_observation_and_payload_is_idempotent():
    engine = ObjectGeometryFusion(voxel_size_m=0.02)
    first = _apply(engine)
    second = _apply(engine)
    assert first["status"] == "applied"
    assert second["status"] == "idempotent_noop"
    assert engine.snapshot("track-1")["raw_point_count"] == 2


def test_same_observation_with_different_payload_fails():
    engine = ObjectGeometryFusion(voxel_size_m=0.02)
    _apply(engine)
    with pytest.raises(EvidenceConflictError, match="different payload"):
        _apply(engine, points=[[0.03, 0.0, 0.0]])


def test_exact_pose_repeat_adds_geometry_but_not_independent_view():
    engine = ObjectGeometryFusion(voxel_size_m=0.02)
    first = _apply(engine, "obs-a")
    second = _apply(engine, "obs-b", points=[[0.001, 0.001, 0.0], [0.005, 0.002, 0.0]])
    state = engine.snapshot("track-1")
    assert first["unique_sensor_pose_added"] is True
    assert second["unique_sensor_pose_added"] is False
    assert state["observation_count"] == 2
    assert state["unique_sensor_pose_count"] == 1
    assert state["raw_point_count"] == 4


def test_voxel_limit_fails_without_dropping_new_evidence():
    engine = ObjectGeometryFusion(voxel_size_m=0.01, max_voxels_per_object=1)
    _apply(engine, points=[[0.001, 0.001, 0.0]])
    with pytest.raises(MemoryError, match="not silently dropped"):
        _apply(engine, "obs-b", points=[[0.051, 0.0, 0.0]])
    assert engine.snapshot("track-1")["observation_count"] == 1


def test_new_observation_must_not_move_object_time_backwards():
    engine = ObjectGeometryFusion(voxel_size_m=0.02)
    _apply(engine, "later", points=[[0.001, 0.001, 0.0]])
    # Helper applies at t=1.0; first payload is already committed.
    with pytest.raises(ValueError, match="older than object state"):
        engine.apply(object_id="track-1", observation_id="earlier", timestamp_s=0.5,
                     xyz_W=np.asarray([[0.003, 0.001, 0.0]], dtype=np.float32),
                     rgb_u8=np.asarray([[10, 20, 30]], dtype=np.uint8),
                     pixel_rc=np.asarray([[0, 0]], dtype=np.int32), T_W_Copt=np.eye(4))
    assert engine.snapshot("track-1")["observation_count"] == 1


def test_observation_id_has_one_object_owner_and_stale_idempotent_replay_is_noop():
    engine = ObjectGeometryFusion(voxel_size_m=0.02)
    xyz = np.asarray([[-0.001, 0.001, 0.0], [0.004, 0.002, 0.0]], dtype=np.float32)
    colors = np.asarray([[10, 20, 30]] * 2, dtype=np.uint8)
    pixels = np.asarray([[0, 0], [0, 1]], dtype=np.int32)
    pose = np.eye(4)
    engine.apply(object_id="track-1", observation_id="obs-replay", timestamp_s=0.5,
                 xyz_W=xyz, rgb_u8=colors, pixel_rc=pixels, T_W_Copt=pose)
    # Replaying the exact older event is a no-op after state time advances.
    engine.apply(object_id="track-1", observation_id="obs-later", timestamp_s=1.0,
                 xyz_W=np.asarray([[0.03, 0.0, 0.0]], dtype=np.float32),
                 rgb_u8=np.asarray([[10, 20, 30]], dtype=np.uint8),
                 pixel_rc=np.asarray([[1, 1]], dtype=np.int32), T_W_Copt=pose)
    exact = engine.apply(object_id="track-1", observation_id="obs-replay", timestamp_s=0.5,
                         xyz_W=np.asarray([[-0.001, 0.001, 0.0], [0.004, 0.002, 0.0]], dtype=np.float32),
                         rgb_u8=colors, pixel_rc=pixels, T_W_Copt=pose)
    assert exact["status"] == "idempotent_noop"
    with pytest.raises(EvidenceConflictError, match="already owned by object"):
        engine.apply(object_id="track-2", observation_id="obs-replay", timestamp_s=1.0,
                     xyz_W=np.asarray([[-0.001, 0.001, 0.0], [0.004, 0.002, 0.0]], dtype=np.float32),
                     rgb_u8=colors, pixel_rc=pixels, T_W_Copt=pose)


def test_alias_and_deferred_observations_do_not_enter_geometry():
    assert fusion_admission({"decision": "duplicate_suppressed", "object_id": "t1"}) == (False, "same_frame_alias")
    assert fusion_admission({"decision": "defer_ambiguous", "object_id": None}) == (False, "unassigned_or_deferred")
    assert fusion_admission({"decision": "geometry_match", "object_id": "t1"}) == (True, "assigned_observation")
