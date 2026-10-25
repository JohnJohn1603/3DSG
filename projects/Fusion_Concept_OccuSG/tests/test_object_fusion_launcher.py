from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import object_fusion_launcher as ofl  # noqa: E402
from scripts.run_object_fusion_offline import (_write_index, assignments_for,
    fusion_admission, validate_assignment_lineage)  # noqa: E402
from scripts import scene_launcher as sl  # noqa: E402
from scripts.compare_object_fusion_results import membership_components  # noqa: E402


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_lineage_requires_explicit_decision_for_every_p3_id(tmp_path):
    replay = tmp_path / "replay"
    _jsonl(replay / "object_association_lineage.jsonl", [
        {"detection_id": "a", "decision": "created", "object_id": "t1"},
        {"detection_id": "b", "decision": "duplicate_suppressed", "object_id": "t1", "canonical_observation_id": "a"},
        {"detection_id": "c", "decision": "defer_ambiguous", "object_id": None},
    ])
    assert ofl.validate_lineage(replay, {"a", "b", "c"}) == {
        "rows": 3, "assigned": 1, "aliases": 1, "deferred_or_unassigned": 1}
    with pytest.raises(ValueError, match="incomplete"):
        ofl.validate_lineage(replay, {"a", "b", "c", "missing"})


def test_source_association_decisions_are_valid_fixed_assignments(tmp_path):
    replay = tmp_path / "replay"
    _jsonl(replay / "object_association_lineage.jsonl", [
        {"detection_id": "a", "decision": "created", "object_id": "object-1"},
        {"detection_id": "b", "decision": "same_class_match", "object_id": "object-1"},
        {"detection_id": "c", "decision": "same_pose_suppressed", "object_id": "object-1"},
    ])
    assert ofl.validate_lineage(replay, {"a", "b", "c"}) == {
        "rows": 3, "assigned": 3, "aliases": 0, "deferred_or_unassigned": 0}


def test_source_same_pose_suppressed_is_assigned_evidence_not_dedup_alias(tmp_path):
    replay = tmp_path / "replay"
    _jsonl(replay / "object_association_lineage.jsonl", [
        {"detection_id": "a", "decision": "same_class_match", "object_id": "object-1"},
        {"detection_id": "b", "decision": "same_pose_suppressed", "object_id": "object-1"},
    ])
    assert validate_assignment_lineage(replay, {"a", "b"}) == {
        "rows": 2, "assigned": 2, "aliases": 0, "deferred_or_unassigned": 0}
    assert fusion_admission({"decision": "same_pose_suppressed", "object_id": "object-1"}) == (
        True, "assigned_observation")


def test_fusion_comparison_matches_by_observation_membership_and_keeps_one_to_many():
    source = [{"object_id": "object-0001", "members": {"o1", "o2", "o3"}},
              {"object_id": "object-0002", "members": {"o4"}}]
    candidate = [{"object_id": "u3-0001", "members": {"o1", "o2"}},
                 {"object_id": "u3-0099", "members": {"o3"}},
                 {"object_id": "u3-0002", "members": {"o4"}}]
    groups = membership_components(source, candidate)
    split = next(group for group in groups if "o1" in group["shared_members"])
    assert split["source_track_count"] == 1
    assert split["candidate_track_count"] == 2
    assert split["topology_changed"] is True
    assert len(groups) == 2


def test_inspect_input_resolves_source_replay_only_through_locked_cache_hashes(tmp_path):
    runs = tmp_path / "runs"
    cache = runs / "cache-run"
    source = runs / "source-replay"
    for path in (cache / "capture", cache / "detection_2d", cache / "detection_3d",
                 source / "replay"):
        path.mkdir(parents=True, exist_ok=True)
    capture = cache / "capture/capture_manifest.json"
    p2 = cache / "detection_2d/detection_2d_index.json"
    p3 = cache / "detection_3d/detection_3d.jsonl"
    p3m = cache / "detection_3d/detection_3d_manifest.json"
    capture.write_text(json.dumps({"scene_id": "scene-x", "frame_count": 1, "resolution_px": [2, 2]}))
    capture_sha = ofl.sha256(capture)
    p2.write_text(json.dumps({"status": "pass", "frame_count": 1, "capture_manifest_sha256": capture_sha}))
    row = {"detection_id": "f:det-0", "frame_id": "f", "class_name": "chair", "confidence": 1,
           "timestamp_s": 0, "center_W": [0, 0, 0], "extents_m": [1, 1, 1], "provider_metadata": {}}
    _jsonl(p3, [{"detections": [row]}])
    p3m.write_text(json.dumps({"status": "pass", "frame_count": 1, "capture_manifest_sha256": capture_sha,
                               "detection_3d_count": 1}))
    hashes = {"capture_manifest": ofl.sha256(capture), "p2_index": ofl.sha256(p2),
              "p3_cache": ofl.sha256(p3), "p3_manifest": ofl.sha256(p3m)}
    (cache / "launcher_manifest.json").write_text(json.dumps({"status": "completed", "run_id": "cache-run", "route_alias": "main"}))
    (source / "launcher_manifest.json").write_text(json.dumps({"status": "completed", "run_id": "source-replay",
        "association_profile": "source", "graph_backend": "source-wrapper", "source_cache_run": str(cache),
        "input_hashes": hashes}))
    (source / "replay/manifest.json").write_text(json.dumps({"pipeline_status": "pass", "object_backend": "source-wrapper",
        "capture_manifest_sha256": capture_sha, "detection_3d_cache_sha256": hashes["p3_cache"]}))
    _jsonl(source / "replay/object_association_lineage.jsonl", [
        {"detection_id": "f:det-0", "decision": "same_pose_suppressed", "object_id": "object-1"}])
    info = ofl.inspect_input(source)
    assert info["run_id"] == "source-replay"
    assert info["backend"] == "source"
    assert info["cache_root"] == cache
    assert info["replay_root"] == source / "replay"
    assert info["lineage"]["assigned"] == 1 and info["lineage"]["aliases"] == 0


def test_lineage_does_not_infer_alias_from_endpoints(tmp_path):
    replay = tmp_path / "replay"
    _jsonl(replay / "object_association_lineage.jsonl", [
        {"detection_id": "a", "decision": "duplicate_suppressed", "object_id": "t1"},
    ])
    with pytest.raises(ValueError, match="canonical observation"):
        ofl.validate_lineage(replay, {"a"})


def test_detection3d_parity_checks_geometry_and_selected_cluster_provenance(tmp_path):
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    row = {"frame_id": "f", "detections": [{"detection_id": "f:det-0", "timestamp_s": 0.0,
        "center_W": [1.0, 2.0, 3.0], "center_method": "aabb", "class_name": "chair", "class_id": "56",
        "confidence": 0.8, "frame_id": "world", "layer_id": "floor_0", "extents_m": [1, 1, 1],
        "provider_metadata": {"frame_index": 0, "cluster_points": 120, "class_raw": "chair", "mask_file": "m.npy"}}]}
    _jsonl(first, [row]); _jsonl(second, [row])
    assert ofl.compare_detection3d(first, second)["status"] == "pass"
    changed = json.loads(json.dumps(row)); changed["detections"][0]["center_W"][0] += 0.01
    _jsonl(second, [changed])
    result = ofl.compare_detection3d(first, second)
    assert result["status"] == "diverged"
    assert any(change["field"] == "center_W" for change in result["differences"])


def test_assignment_diagnostics_may_add_alias_target_but_may_not_override_lineage(tmp_path):
    replay = tmp_path / "replay"
    _jsonl(replay / "object_association_lineage.jsonl", [
        {"detection_id": "a", "decision": "created", "object_id": "t1"},
        {"detection_id": "b", "decision": "duplicate_suppressed", "object_id": "t1"},
        {"detection_id": "c", "decision": "geometry_match", "object_id": "t1"},
    ])
    (replay / "frame_diagnostics.json").write_text(json.dumps([{"frame_id": "f", "object_backend": {
        "decisions": [{"detection_id": "b", "decision": "duplicate_alias", "track_id": "t1",
                       "duplicate_of": "a", "canonical_observation_id": "a"},
                      {"detection_id": "c", "decision": "matched", "track_id": "t1"}],
        "assignments": [{"detection_id": "b", "decision": "duplicate_suppressed", "object_id": "t1"},
                        {"detection_id": "c", "decision": "geometry_match", "object_id": "t1"}]}}]), encoding="utf-8")
    rows = assignments_for(replay, {"a", "b", "c"})
    assert rows["b"]["decision"] == "duplicate_suppressed"
    assert rows["b"]["canonical_observation_id"] == "a"
    assert rows["b"]["duplicate_of"] == "a"
    assert rows["c"]["decision"] == "geometry_match"
    (replay / "frame_diagnostics.json").write_text(json.dumps([{"frame_id": "f", "object_backend": {
        "decisions": [{"detection_id": "b", "decision": "created", "object_id": "t2"}]}}]), encoding="utf-8")
    with pytest.raises(ValueError, match="decision conflict"):
        assignments_for(replay, {"a", "b"})


def test_cli_and_interactive_fuse_command_share_dispatch(monkeypatch):
    launcher = sl.Launcher()
    calls = []
    monkeypatch.setattr(launcher, "cmd_fuse_objects", lambda args, interactive=False: calls.append((args, interactive)) or 0)
    assert launcher.handle(["fuse-objects", "run-a", "--check"]) == 0
    assert launcher.handle(["/fuse-objects", "run-b"], interactive=True) == 0
    assert calls == [(["run-a", "--check"], False), (["run-b"], True)]


def test_compare_object_fusions_cli_dispatch(monkeypatch):
    launcher = sl.Launcher()
    calls = []
    monkeypatch.setattr(launcher, "cmd_compare_object_fusions", lambda args: calls.append(args) or 0)
    assert launcher.handle(["compare-object-fusions", "fusion-source", "fusion-candidate"]) == 0
    assert calls == [["fusion-source", "fusion-candidate"]]


def test_open_fusion_handles_missing_gui_without_pipeline_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fusions"
    result = root / "fusion_test"
    result.mkdir(parents=True)
    (result / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(sl, "OBJECT_FUSIONS", root)
    monkeypatch.setattr(sl.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("xdg-open")))
    launcher = sl.Launcher()
    assert launcher.cmd_open(["fusion_test", "fusion"]) == 0
    assert "Không mở được GUI" in capsys.readouterr().out


def test_open_fusion_comparison_uses_comparison_viewer(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fusions"
    comparison = root / "comparisons" / "fusion_compare_test"
    comparison.mkdir(parents=True)
    (comparison / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(sl, "OBJECT_FUSIONS", root)
    monkeypatch.setattr(sl.subprocess, "Popen", lambda *args, **kwargs: None)
    assert sl.Launcher().cmd_open(["fusion_compare_test", "fusion-comparison"]) == 0
    assert "source/candidate object fusion comparison" in capsys.readouterr().out


def test_fusion_viewer_exposes_object_selector_aliases_and_backend_provenance(tmp_path):
    manifest = {"objects": {"geometry-only": [{"object_id": "t1", "observation_count": 2,
        "unique_sensor_pose_count": 1, "raw_point_count": 10, "voxel_count": 4,
        "bounds_min_W": [0, 0, 0], "bounds_max_W": [1, 1, 1], "center_W": [.5, .5, .5],
        "timeline_png": "geometry-only/objects/t1/revision.png", "evidence": []}]},
        "excluded_observations": {"geometry-only": [{"observation_id": "f:det-2", "decision": "alias_excluded_from_fusion",
            "canonical_observation_id": "f:det-1", "object_id": "t1", "reason": "same_frame_alias"}]},
        "input_run_id": "run-a", "input_capture_sha256": "abc", "association_assignments_are_fixed": True,
        "feedback_to_matcher": False, "source_frame_indices": [0, 1]}
    _write_index(tmp_path, manifest)
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "object-picker" in page
    assert "f:det-2" in page and "f:det-1" in page
    assert "input_capture_sha256" in page and "association_assignments_are_fixed" in page


def test_fuse_check_is_read_only(monkeypatch, tmp_path, capsys):
    runs = tmp_path / "runs"
    run = runs / "run-a"
    run.mkdir(parents=True)
    fusions = tmp_path / "object_fusions"
    monkeypatch.setattr(sl, "OBJECT_FUSIONS", fusions)
    launcher = sl.Launcher()
    monkeypatch.setattr(launcher, "_find_run", lambda run_id: (run, {"run_id": run_id, "status": "completed"}))
    monkeypatch.setattr(launcher, "_fusion_preflight", lambda path: {
        "run_id": "run-a", "scene_id": "scene", "route_alias": "route", "resolution_px": [10, 10],
        "frames": 2, "backend": "source", "accepted_p3": 1,
        "lineage": {"assigned": 1, "aliases": 0, "deferred_or_unassigned": 0},
        "reused_sidecar": None, "sidecar_issue": "not present", "runtime_estimate": {"seconds": 3, "basis": {}},
        "runtime": {"interpreter": "python", "versions": {}}})
    assert launcher.cmd_fuse_objects(["run-a", "--check"]) == 0
    assert not fusions.exists()
    assert "read-only" in capsys.readouterr().out


def test_fusion_result_path_rejects_path_components(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "OBJECT_FUSIONS", tmp_path / "fusions")
    launcher = sl.Launcher()
    with pytest.raises(ValueError, match="ID không hợp lệ"):
        launcher._fusion_result_root("fusion_../outside")
