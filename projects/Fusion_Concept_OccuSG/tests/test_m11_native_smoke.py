from __future__ import annotations

from pathlib import Path

from standalone_sg.serialization import load_delta_log, load_snapshot, replay_deltas, trajectory_rows


def test_native_pointcloud_m2_to_m10_smoke(tmp_path: Path):
    from scripts.run_native_pipeline_smoke import run_smoke

    manifest=run_smoke(tmp_path)
    snapshot=load_snapshot(tmp_path/"snapshot.json")
    deltas=load_delta_log(tmp_path/"delta_log.json")
    replayed=replay_deltas(
        type(snapshot)(1,None,None,0,None,(),(),snapshot.config_hash,{}),
        deltas,
    )
    assert manifest["input_mode"]=="pointcloud"
    assert manifest["rgbd_end_to_end"] is False
    assert manifest["gt_regions_in_builder"] is False
    assert manifest["m3_backend"]=="cpp_mapconverter"
    assert all(frame["m4_backend_status"] in {"success","fallback","rejected"} for frame in manifest["m4_updates"])
    assert all(frame["native_mapping"]["m3_backend"]=="cpp_mapconverter" for frame in manifest["m4_updates"])
    assert manifest["delta_replay_equal"] and replayed==snapshot
    assert manifest["revision"]==3 and manifest["agent_pose_count"]==2
    assert manifest["fixture"]["ray_type"]=="no_return_only"
    assert manifest["fixture"]["has_hit_count"]==0
    assert manifest["fixture"]["camera_motion"]=="stationary poses"
    assert "update_flush_close_runtime_ms" in manifest and "fixture_generation_runtime_ms" in manifest
    assert "sampled_child_rss_max_kib" in manifest
    assert "total_runtime_ms" not in manifest and "peak_child_rss_kib" not in manifest
    assert len([node for node in snapshot.nodes if node.kind=="room"]) >= 1
    assert len(trajectory_rows(snapshot))==2
    assert all((tmp_path/name).is_file() and (tmp_path/name).stat().st_size>0 for name in manifest["outputs"])
