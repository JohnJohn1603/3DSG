import json
from argparse import Namespace
from pathlib import Path

from scripts.evaluate_habitat_run import evaluate, polygon_rooms_from_geojson


def test_diagnostic_bundle_keeps_missing_gt_null_and_not_run(tmp_path):
    capture = tmp_path / "capture"
    replay = tmp_path / "replay"
    output = tmp_path / "evaluation"
    (capture / "frames" / "000000").mkdir(parents=True)
    replay.mkdir()
    import numpy as np
    np.save(capture / "frames" / "000000" / "valid_mask.npy", np.ones((2, 3), dtype=bool))
    (capture / "capture_manifest.json").write_text(json.dumps({
        "frame_count": 1, "frames": [{"frame_id": "f0", "frame_dir": "frames/000000", "timestamp_s": 0.0}],
        "dataset_type": "hm3d", "dataset_version": "v0.2", "scene_id": "test", "sequence_id": "seq",
        "resolution_px": [3, 2], "route_type": "sampled_fixed_pose", "motion_model": "ground_truth_pose"
    }))
    (replay / "manifest.json").write_text(json.dumps({
        "snapshot_reload_equal": True, "delta_replay_equal": True, "fresh_replay_equal": None,
        "runtime_ms": 10.0, "python_process_ru_maxrss_kib": 100
    }))
    (replay / "frame_diagnostics.json").write_text(json.dumps([{
        "frame_id": "f0", "frame_index": 0, "timestamp_s": 0.0,
        "m4_guard_accepted": True, "m4_stale": False, "stage_status": {},
        "native_mapping": {"m1_runtime_ms": 1.0, "m2_runtime_ms": 2.0, "m3_runtime_ms": 3.0,
                           "m3_free_cell_count": 2, "m3_occupied_cell_count": 1, "m3_unknown_cell_count": 3},
        "update_runtime_ms": 5.0
    }]))
    (replay / "snapshot.json").write_text(json.dumps({"snapshot": {"revision": 1, "nodes": [], "edges": []}}))
    (replay / "delta_log.json").write_text("{}")
    (replay / "trajectory.csv").write_text("x_m,y_m\n0,0\n")
    summary = evaluate(Namespace(capture=capture, replay=replay, output=output, grid_trace=None,
                                 semantic=None, room_gt=None, object_evaluation=None, run_config=None, run_id="test"))
    metrics = json.loads((output / "metrics.json").read_text())
    assert summary["evaluation_mode"] == "diagnostic_only"
    assert metrics["metrics"]["room"]["macro_iou_all_gt_rooms"]["value"] is None
    assert metrics["metrics"]["room"]["macro_iou_all_gt_rooms"]["unavailable_reason"]
    assert metrics["metrics"]["operations"]["replay_checks"]["fresh_process_equal"]["unavailable_reason"] == "fresh-process check not run"
    assert len((output / "metrics.csv").read_text().splitlines()) == 2
    assert (output / "failure_samples" / "index.json").is_file()


def test_room_gt_requires_explicitly_confirmed_annotation(tmp_path):
    path = tmp_path / "rooms.json"
    path.write_text(json.dumps({
        "metadata": {"status": "provisional", "coordinate_frame": "canonical_world_z_up_xy_m", "units": "m"},
        "features": []
    }))
    try:
        polygon_rooms_from_geojson(path)
    except ValueError as exc:
        assert "refusing score" in str(exc)
    else:
        raise AssertionError("provisional room GT must not be scored")
