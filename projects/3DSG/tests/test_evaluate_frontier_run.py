from __future__ import annotations

import json

from scripts.evaluate_frontier_run import evaluate


def _write_run(tmp_path, viewpoints):
    (tmp_path / "run_summary.json").write_text(json.dumps({
        "run_id": "fixture", "elapsed_exploration_s": 10.0, "frame_count": len(viewpoints) + 1,
        "collision_or_filter_block_count": 0,
    }))
    (tmp_path / "effective_config.json").write_text(json.dumps({
        "scene_config": {"agent": {"navmesh_baked_radius_m": 0.1}},
        "navigation_profile_effective": {"navigation": {"grid_resolution_m": 0.05}},
    }))
    rows = [{
        "event": "navigation_tick", "frame_index": 0,
        "robot_pose_xy_yaw": [0.0, 0.0, 0.0],
        "map_cell_counts": {"free": 13, "occupied": 0, "unknown": 100},
        "frontier_count": 2,
    }]
    for index, point in enumerate(viewpoints, start=1):
        rows.append({
            "event": "navigation_tick", "frame_index": index,
            "robot_pose_xy_yaw": [point[0], point[1], 0.0],
            "goal_id": f"goal-{index}", "goal_viewpoint_xy": list(point),
            "command": {"reason": "goal_reached"},
            "map_cell_counts": {"free": 13 + index * 50, "occupied": 0, "unknown": 100},
            "frontier_count": 2,
        })
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_close_viewpoints_pass_movement_but_fail_spatial_coverage(tmp_path):
    _write_run(tmp_path, [(0.3, 0.0), (0.4, 0.0)])

    result = evaluate(tmp_path)

    assert result["gate"]["passed"] is True
    assert result["exploration_quality_gate"]["passed"] is False
    assert result["overall_gate"]["passed"] is False
    assert result["exploration_quality_gate"]["known_free_cell_net_change_proxy"] == 100
    assert result["exploration_quality_gate"]["coverage_proxy_is_ground_truth"] is False


def test_spatially_separated_viewpoints_pass_both_gates(tmp_path):
    _write_run(tmp_path, [(0.3, 0.0), (1.0, 0.0)])

    result = evaluate(tmp_path)

    assert result["gate"]["passed"] is True
    assert result["exploration_quality_gate"]["spatially_distinct_viewpoints"] == 2
    assert result["exploration_quality_gate"]["passed"] is True
    assert result["overall_gate"]["passed"] is True
