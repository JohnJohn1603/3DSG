from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from integration_v2.live_evidence import publish_live_frame_evidence
from integration_v2.live_processing import _prepare_viewer
from scripts import run_camera_live_pilot as pilot
from scripts.run_habitat_smooth_route_preview import _write_keyframe


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_frame(root: Path):
    run = root / "pilot-run"
    run.mkdir(parents=True)
    pilot.atomic_json(run / "run_manifest.json", {"run_id": "pilot-run"})
    frame_dir = run / "capture/frames/frame-0000"
    (run / "packets/assets/detection_2d/masks").mkdir(parents=True)
    viewer = run / "viewer"
    viewer.mkdir(parents=True)
    rgb = np.zeros((3, 4, 3), dtype=np.uint8)
    rgb[..., 1] = 120
    mask = np.zeros((3, 4), dtype=bool)
    mask[1, 1:3] = True
    clocks = {"timestamp_s": 1.25, "timestamp_clock_domain": "habitat_simulation",
        "capture_wall_time_unix_s": 123.0, "capture_wall_time_clock_domain": "unix_epoch_utc",
        "capture_monotonic_s": 45.0, "capture_monotonic_domain": "host_monotonic:test"}
    pose = np.eye(4).tolist()
    metadata = {"schema": "smooth-route-raw-keyframe/v1", "frame_id": "frame-0000",
        "frame_index": 0, "scene_id": "scene-a", "sequence_id": "seq-a", **clocks,
        "width": 4, "height": 3, "resolution_px": [4, 3], "horizontal_fov_deg": 90.0,
        "calibration_id": "fixture-calibration-v1", "intrinsics": {"fx": 2.0, "fy": 2.0, "cx": 1.5, "cy": 1.0},
        "depth_units": "m", "depth_representation": "axial", "sensor_pose_habitat": pose,
        "sensor_rotation_habitat": np.eye(3).tolist(), "base_pose_habitat": pose,
        "base_pose_canonical": pose}
    raw = {"frame_id": "frame-0000", "rgb": rgb,
        "depth_m": np.ones((3, 4), dtype=np.float32),
        "valid_mask": np.ones((3, 4), dtype=bool), "metadata": metadata}
    keyframe = _write_keyframe(raw, run)
    packet = {"frame_id": "frame-0000", "metadata": metadata}
    base = pilot.write_capture_packet(run, packet, keyframe,
        {"scene_asset": "fixture.glb", "min_depth_m": 0.1, "max_depth_m": 10.0})
    frame_dir = Path(base["frame_dir"])
    mask_path = run / "packets/assets/detection_2d/masks/frame-0000__0000.npy"
    np.save(mask_path, mask, allow_pickle=False)
    hashes = base["source_hashes"]
    detections = [{"detection_id": "frame-0000:det-0000", "class_raw": "chair",
        "score_raw": 0.75, "mask_file": "masks/frame-0000__0000.npy", "mask_sha256": sha(mask_path)}]
    return run, viewer, frame_dir, hashes, detections, base["capture_record"]


def test_camera_live_viewer_starts_without_reused_c2_evidence(tmp_path: Path):
    run = tmp_path / "pilot-run"
    run.mkdir()
    viewer, hashes = _prepare_viewer(tmp_path / "c2-cache-is-not-read", run,
        "scene-a", "seq-a", evidence_mode="camera_live_current_run_only", run_id="pilot-run-001")
    index = json.loads((viewer / "live_index.json").read_text())
    evidence = json.loads((viewer / "evidence_index.json").read_text())
    assert index["run_id"] == "pilot-run-001"
    assert index["evidence_source_mode"] == "camera_live_current_run_only"
    assert evidence["source"] == "current_run_frames_only"
    assert evidence["observations"] == {}
    assert list((viewer / "evidence").iterdir()) == []
    assert "evidence_index.json" in hashes


def test_live_evidence_panels_bind_current_frame_and_mask_hashes(tmp_path: Path):
    run, viewer, frame_dir, hashes, detections, capture_record = make_frame(tmp_path)
    bundle = publish_live_frame_evidence(run_root=run, viewer_root=viewer, run_id="pilot-run",
        scene_id="scene-a", sequence_id="seq-a", frame_id="frame-0000", frame_index=0,
        timestamp_s=1.25, timestamp_clock_domain="habitat_simulation",
        capture_wall_time_unix_s=123.0, capture_monotonic_s=45.0,
        capture_monotonic_domain="host_monotonic:test", frame_dir=frame_dir,
        capture_record=capture_record,
        source_hashes=hashes, detections=detections,
        mask_asset_root=run / "packets/assets/detection_2d")
    row = bundle["observations"][0]
    assert bundle["run_id"] == "pilot-run"
    assert row["observation_id"] == "frame-0000:det-0000"
    assert row["source_hashes"]["rgb.npy"] == hashes["rgb.npy"]
    assert row["source_hashes"]["mask.npy"] == detections[0]["mask_sha256"]
    for panel in [bundle["camera_rgb"], *row["panels"].values()]:
        panel_path = viewer / panel["url"]
        assert panel_path.is_file() and sha(panel_path) == panel["sha256"]
    assert bundle["camera_rgb"]["width"] == 4
    assert bundle["camera_rgb"]["height"] == 3


def test_live_evidence_rejects_stale_capture_or_wrong_mask(tmp_path: Path):
    run, viewer, frame_dir, hashes, detections, capture_record = make_frame(tmp_path)
    with pytest.raises(ValueError, match="conflict with authoritative"):
        altered = dict(hashes)
        altered["rgb.npy"] = "0" * 64
        publish_live_frame_evidence(run_root=run, viewer_root=viewer, run_id="pilot-run",
            scene_id="scene-a", sequence_id="seq-a", frame_id="frame-0000", frame_index=0,
            timestamp_s=1.25, timestamp_clock_domain="habitat_simulation",
            capture_wall_time_unix_s=123.0, capture_monotonic_s=45.0,
            capture_monotonic_domain="host_monotonic:test", frame_dir=frame_dir,
            capture_record=capture_record,
            source_hashes=altered, detections=detections,
            mask_asset_root=run / "packets/assets/detection_2d")
    with pytest.raises(ValueError, match="mask hash mismatch"):
        bad = [dict(detections[0], mask_sha256="f" * 64)]
        publish_live_frame_evidence(run_root=run, viewer_root=viewer, run_id="pilot-run",
            scene_id="scene-a", sequence_id="seq-a", frame_id="frame-0000", frame_index=0,
            timestamp_s=1.25, timestamp_clock_domain="habitat_simulation",
            capture_wall_time_unix_s=123.0, capture_monotonic_s=45.0,
            capture_monotonic_domain="host_monotonic:test", frame_dir=frame_dir,
            capture_record=capture_record,
            source_hashes=hashes, detections=bad,
            mask_asset_root=run / "packets/assets/detection_2d")


def _publish(run, viewer, frame_dir, hashes, detections, record, **overrides):
    values = {"run_root": run, "viewer_root": viewer, "run_id": "pilot-run",
        "scene_id": "scene-a", "sequence_id": "seq-a", "frame_id": "frame-0000",
        "frame_index": 0, "timestamp_s": 1.25,
        "timestamp_clock_domain": "habitat_simulation",
        "capture_wall_time_unix_s": 123.0, "capture_monotonic_s": 45.0,
        "capture_monotonic_domain": "host_monotonic:test", "frame_dir": frame_dir,
        "capture_record": record, "source_hashes": hashes,
        "detections": detections,
        "mask_asset_root": run / "packets/assets/detection_2d"}
    values.update(overrides)
    return publish_live_frame_evidence(**values)


def test_production_writer_emits_v2_capture_record_and_empty_detections_are_valid(tmp_path: Path):
    run, viewer, frame_dir, hashes, _, record = make_frame(tmp_path)
    metadata = json.loads((frame_dir / "metadata.json").read_text())
    assert record["schema"] == "camera-live-capture-record/v2"
    assert record["run_id"] == "pilot-run"
    assert record["raw_files_sha256"] == hashes
    assert record["raw_files_sha256"]["metadata.json"] == sha(frame_dir / "metadata.json")
    assert metadata["capture_record_schema"] == record["schema"]
    assert metadata["run_id"] == record["run_id"]
    assert set(metadata["raw_files_sha256"]) == {"rgb.npy", "depth_m.npy", "valid_mask.npy"}
    bundle = _publish(run, viewer, frame_dir, hashes, [], record)
    assert bundle["observations"] == []


def test_capture_record_rejects_missing_wrong_and_conflicting_hashes(tmp_path: Path):
    run, viewer, frame_dir, hashes, detections, record = make_frame(tmp_path)
    missing = dict(record)
    missing["raw_files_sha256"] = dict(record["raw_files_sha256"])
    missing["raw_files_sha256"].pop("metadata.json")
    with pytest.raises(ValueError, match="capture record raw_files_sha256"):
        _publish(run, viewer, frame_dir, hashes, detections, missing)

    wrong = dict(record)
    wrong["raw_files_sha256"] = dict(record["raw_files_sha256"])
    wrong["raw_files_sha256"]["rgb.npy"] = "0" * 64
    wrong_hashes = dict(hashes)
    wrong_hashes["rgb.npy"] = "0" * 64
    with pytest.raises(ValueError, match="capture source hash mismatch"):
        _publish(run, viewer, frame_dir, wrong_hashes, detections, wrong)

    conflict = dict(hashes)
    conflict["depth_m.npy"] = "f" * 64
    with pytest.raises(ValueError, match="conflict with authoritative"):
        _publish(run, viewer, frame_dir, conflict, detections, record)


@pytest.mark.parametrize("field,value,match", [
    ("scene_id", "other-scene", "scene_id"),
    ("sequence_id", "other-seq", "sequence_id"),
    ("frame_id", "other-frame", "frame_id"),
    ("run_id", "other-run", "run_id"),
    ("timestamp_clock_domain", "unix_epoch_utc", "timestamp_clock_domain"),
])
def test_capture_record_rejects_identity_and_clock_mismatch(tmp_path: Path, field, value, match):
    run, viewer, frame_dir, hashes, detections, record = make_frame(tmp_path)
    altered = dict(record)
    altered[field] = value
    with pytest.raises(ValueError, match=match):
        _publish(run, viewer, frame_dir, hashes, detections, altered)


def test_capture_metadata_clock_domain_and_array_hashes_are_enforced(tmp_path: Path):
    run, viewer, frame_dir, hashes, detections, record = make_frame(tmp_path)
    metadata_path = frame_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["capture_wall_time_clock_domain"] = "host_monotonic"
    metadata_path.write_text(json.dumps(metadata))
    altered_record = dict(record)
    altered_record["raw_files_sha256"] = dict(record["raw_files_sha256"])
    altered_record["raw_files_sha256"]["metadata.json"] = sha(metadata_path)
    altered_hashes = dict(hashes)
    altered_hashes["metadata.json"] = sha(metadata_path)
    with pytest.raises(ValueError, match="metadata capture_wall_time_clock_domain"):
        _publish(run, viewer, frame_dir, altered_hashes, detections, altered_record)

    metadata["capture_wall_time_clock_domain"] = "unix_epoch_utc"
    metadata["raw_files_sha256"]["rgb.npy"] = "1" * 64
    metadata_path.write_text(json.dumps(metadata))
    altered_record["raw_files_sha256"]["metadata.json"] = sha(metadata_path)
    altered_hashes["metadata.json"] = sha(metadata_path)
    with pytest.raises(ValueError, match="metadata array hashes conflict"):
        _publish(run, viewer, frame_dir, altered_hashes, detections, altered_record)


def test_capture_record_rejects_path_outside_run_root(tmp_path: Path):
    run, viewer, frame_dir, hashes, detections, record = make_frame(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside this run's capture root"):
        _publish(run, viewer, outside, hashes, detections, record)


def test_actual_failed_run_metadata_and_packet_are_readable_without_mutation(tmp_path: Path):
    source = Path(__file__).resolve().parents[1] / "artifacts/live_processing_runs/t4_camera_live_00802_t1_1_20261019_v1"
    assert source.is_dir(), "locked failed-run artifacts are required for this local regression"
    source_packet = json.loads((source / "packets/frame-000000.json").read_text())
    run_manifest = json.loads((source / "run_manifest.json").read_text())
    source_record = source_packet["capture_record"]
    source_frame = source / "capture" / source_record["frame_dir"]
    source_meta_bytes = (source_frame / "metadata.json").read_bytes()
    metadata = json.loads(source_meta_bytes)
    assert "raw_files_sha256" not in metadata

    regression = tmp_path / "artifact-regression"
    (regression / "capture").mkdir(parents=True)
    frame_dir = regression / "capture" / source_record["frame_dir"]
    frame_dir.mkdir(parents=True)
    for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"):
        shutil.copy2(source_frame / name, frame_dir / name)
    viewer = regression / "viewer"
    viewer.mkdir()
    mask_root = regression / "packets/assets/detection_2d"
    mask_root.mkdir(parents=True)
    p2 = json.loads((source / "packets/p2-000000.json").read_text())
    detections = p2["detections"]
    for row in detections:
        relative = Path(row["mask_file"])
        source_mask = source / "packets/assets/detection_2d" / relative
        destination = mask_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_mask, destination)
    before = {name: sha(frame_dir / name) for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json")}
    bundle = publish_live_frame_evidence(run_root=regression, viewer_root=viewer,
        run_id=run_manifest["run_id"], scene_id=source_packet["scene_id"],
        sequence_id=source_packet["sequence_id"], frame_id=source_packet["frame_id"],
        frame_index=source_packet["frame_index"], timestamp_s=source_packet["timestamp_s"],
        timestamp_clock_domain=source_packet["timestamp_clock_domain"],
        capture_wall_time_unix_s=source_packet["capture_wall_time_unix_s"],
        capture_monotonic_s=source_packet["capture_monotonic_s"],
        capture_monotonic_domain=source_packet["capture_monotonic_domain"],
        frame_dir=frame_dir, capture_record=source_record,
        source_hashes=source_packet["source_hashes"], detections=detections,
        mask_asset_root=mask_root, allow_legacy_unversioned_record=True)
    after = {name: sha(frame_dir / name) for name in before}
    assert before == after
    assert bundle["frame_id"] == source_packet["frame_id"]
    assert len(bundle["observations"]) == len(detections)
