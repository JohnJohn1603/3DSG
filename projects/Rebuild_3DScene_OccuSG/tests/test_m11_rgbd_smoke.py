from __future__ import annotations

from pathlib import Path
import math

from standalone_sg.serialization import load_delta_log, load_snapshot, replay_deltas, trajectory_rows


def test_synthetic_rgbd_native_m1_to_m10_smoke(tmp_path: Path):
    from scripts.run_native_rgbd_smoke import run_smoke

    manifest = run_smoke(tmp_path)
    snapshot = load_snapshot(tmp_path / "snapshot.json")
    deltas = load_delta_log(tmp_path / "delta_log.json")
    empty = type(snapshot)(1, None, None, 0, None, (), (), snapshot.config_hash, {})
    replayed = replay_deltas(empty, deltas)

    assert manifest["kind"] == "synthetic_depth_pose_native_local_smoke"
    assert manifest["input_mode"] == "rgbd_frame_depth_channel"
    assert manifest["fixture"]["rgb_image_present"] is False
    assert manifest["fixture"]["camera_moves_and_rotates"] is True
    assert manifest["gt_regions_in_builder"] is False
    assert manifest["m3_backend"] == "cpp_mapconverter"
    assert manifest["delta_replay_equal"] and replayed == snapshot
    assert manifest["reloaded_snapshot_queryable"]
    assert manifest["dangling_or_self_edges"] is False
    assert manifest["agent_pose_count"] == 2
    assert manifest["trajectory_displacement_m"] > 0.1
    assert manifest["region_count_final"] == len(
        [node for node in snapshot.nodes if node.kind == "room"]
    )
    assert manifest["region_observation_count_final"] >= 0
    assert manifest["stale_flags"]["regions"] is False

    frames = manifest["frame_diagnostics"]
    assert len(frames) == 2
    assert [frame["mapping"]["m3_mode"] for frame in frames] == ["full_refresh", "partial_update"]
    assert [frame["camera_yaw_rad"] for frame in frames] == [0.0, 0.14]
    assert frames[0]["sensor_origin_W"] != frames[1]["sensor_origin_W"]
    for frame in frames:
        mapping = frame["mapping"]
        assert mapping["backprojection"] == "M1:standalone_sg.geometry.backproject_depth"
        assert mapping["depth_units"] == "m"
        assert mapping["depth_representation"] == "axial"
        assert mapping["sensor_origin_W"][2] == 1.2
        assert mapping["sensor_origin_W"] != frame["base_pose_W"]
        assert mapping["backprojected_hit_count"] == mapping["point_count"]
        assert mapping["m2_hit_scan_point_count"] == mapping["point_count"]
        assert mapping["m2_no_return_scan_point_count"] == 0
        assert math.isclose(mapping["m3_resolution_m"], 0.05, rel_tol=0.0, abs_tol=1e-12)
        assert math.isclose(mapping["m3_origin_xy"][1], -2.0, rel_tol=0.0, abs_tol=0.051)
        assert mapping["m3_changed_bounds_xyz"] is not None
        assert mapping["m3_changed_bounds_xyz"][0] >= 0.0
        assert mapping["m2_free_key_count"] > 0
        assert mapping["m2_occupied_key_count"] > 0
        assert mapping["m3_native_free_cells"] > 0
        assert mapping["m3_native_occupied_cells"] > 0
        assert frame["finite_depth_pixels"] < mapping["input_pixel_count"]
        assert mapping["valid_mask_pixel_count"] < mapping["input_pixel_count"]
        assert frame["m4_backend_status"] == "success"
        assert frame["m4_guard_accepted"] is True
        assert frame["m4_stale"] is False
        assert frame["m4_region_count"] >= 0

    path = trajectory_rows(snapshot)
    assert len(path) == 2
    assert math.dist((path[0]["x_m"], path[0]["y_m"]),
                     (path[1]["x_m"], path[1]["y_m"])) > 0.1
    assert all((tmp_path / name).is_file() and (tmp_path / name).stat().st_size > 0
               for name in manifest["outputs"])
    assert (tmp_path / "diagnostics.json").is_file()

