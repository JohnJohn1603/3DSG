from __future__ import annotations

import json
import math
from pathlib import Path

from standalone_sg.serialization import load_delta_log, load_snapshot, replay_deltas, trajectory_rows


def test_two_room_scripted_route_clearance():
    from scripts.run_native_two_room_rgbd_sequence import scripted_route, validate_route

    route = scripted_route()
    assert 30 <= len(route) <= 60
    check = validate_route(route)
    assert check["partition_crossings"] == 2
    assert check["base_center_samples_checked"] > len(route)


def test_two_room_native_rgbd_sequence_and_exports(tmp_path: Path):
    from scripts.run_native_two_room_rgbd_sequence import run_sequence

    manifest = run_sequence(tmp_path)
    snapshot = load_snapshot(tmp_path / "snapshot.json")
    deltas = load_delta_log(tmp_path / "delta_log.json")
    empty = type(snapshot)(1, None, None, 0, None, (), (), snapshot.config_hash, {})
    replayed = replay_deltas(empty, deltas)
    frames = manifest["frame_diagnostics"]

    assert manifest["kind"] == "synthetic_two_room_doorway_rgbd_native_local_sequence"
    assert manifest["frame_count"] == len(frames) == 44
    assert manifest["fixture"]["partition_crossings"] == 2
    assert manifest["fixture"]["route_is_navigation_policy"] is False
    assert manifest["gt_room_polygons_or_ids_in_builder"] is False
    assert manifest["gt_region_inputs"] is False
    assert manifest["cached_detections"].startswith("one synthetic cached detection")
    assert manifest["m4_status_counts"] == {"success": 43, "rejected": 1}
    assert manifest["m4_branch_counts"]["reject_expanded_empty_preserve_previous"] == 1
    assert manifest["m5_accepted_frames"] == 43
    assert manifest["m5_stale_frames"] == 1

    # Current result is deliberately recorded, not forced to match the two-room fixture geometry.
    assert manifest["room_count_final"] == 1
    assert manifest["room_ids_final"] == [1]
    assert manifest["region_count_max_current"] == 2
    assert manifest["region_track_ids_observed"] == [1, 2]
    assert manifest["region_ids_at_final_frame"] == [1]
    assert frames[1]["room_lifecycle"]["promoted"] is True
    assert frames[2]["room_lifecycle"]["pruned"] is True

    rejected = next(frame for frame in frames if frame["m4_status"] == "rejected")
    assert rejected["m5_stale"] is True and rejected["m5_accepted"] is False
    before, at_reject = frames[rejected["frame_index"] - 1], rejected
    assert before["room_anchors"] == at_reject["room_anchors"]
    assert at_reject["m4_source_diagnostics"]["stale_or_reused"] is True
    comparison = manifest["fresh_final_grid_comparison"]
    assert comparison["sequential_branch"] == "accept_reuse_adjusted_no_differential"
    assert comparison["fresh_branch"] == "accept_incremental"
    assert comparison["sequential_region_count"] == comparison["fresh_region_count"] == 1
    assert comparison["raw_region_geometry_equal"] is False
    assert comparison["sequential_preprocessing"]["kernel_invoked"] is False
    assert comparison["fresh_preprocessing"]["kernel_invoked"] is True

    assert replayed == snapshot
    assert manifest["delta_replay_equal"] is True
    assert manifest["dangling_self_edges"] is False
    assert manifest["duplicate_room_anchors"] is False
    ids = {node.node_id for node in snapshot.nodes}
    assert all(edge.source_id != edge.target_id and edge.source_id in ids and edge.target_id in ids
               for edge in snapshot.edges)
    path = trajectory_rows(snapshot)
    assert len(path) == 44
    assert len({(row["x_m"], row["y_m"]) for row in path}) >= 5
    assert math.dist((path[0]["x_m"], path[0]["y_m"]),
                     (path[-1]["x_m"], path[-1]["y_m"])) > 0.1
    assert len({tuple(frame["sensor_origin_W"]) for frame in frames}) > 10
    assert len({round(frame["camera_yaw_rad"], 3) for frame in frames}) > 5

    for frame in frames:
        assert frame["mapping"]["backprojection"] == "M1:standalone_sg.geometry.backproject_depth"
        assert frame["mapping"]["m2_hit_scan_point_count"] > 0
        assert frame["mapping"]["m2_no_return_scan_point_count"] == 0
        assert frame["mapping"]["m2_occupied_key_count"] > 0
        assert frame["mapping"]["m2_free_key_count"] > 0
        assert frame["m4_source_diagnostics"]["preprocessed_free_pixels"] >= 0
        assert frame["sampled_child_rss_kib"] >= 0
    for name in manifest["artifacts"]:
        artifact = tmp_path / name
        assert artifact.is_file() and artifact.stat().st_size > 0
    for name in ("trajectory_before_door.png", "trajectory_after_door.png", "trajectory_return.png"):
        assert (tmp_path / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    diagnostics = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))
    assert len(diagnostics) == 44
