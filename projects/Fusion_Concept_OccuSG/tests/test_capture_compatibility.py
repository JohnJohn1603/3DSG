from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from integration_v2.capture_compatibility import (  # noqa: E402
    canonical_sha256, image_feature_source_matches, p2_cache_compatibility,
    sensor_contract, sensor_contract_sha256, validate_capture_p2_pair,
    validate_effective_capture,
)


def base_route(resolution=(640, 480)):
    return {"resolution_px": list(resolution), "horizontal_fov_deg": 90.0,
            "calibration_id": f"habitat-rgbd-stage-v1-{resolution[0]}x{resolution[1]}",
            "sensor_position_habitat": [0.0, 1.5, 0.0],
            "sensor_orientation_habitat_rad": [0.0, 0.0, 0.0],
            "min_depth_m": 0.1, "max_depth_m": 10.0,
            "capture_profile": {"profile_id": "native_640x480_v1", "profile_sha256": "a" * 64}}


def test_sensor_fingerprint_uses_native_calibration_and_changes_with_resolution_or_sensor_settings():
    high = sensor_contract(base_route())
    assert high["resolution_px"] == [640, 480]
    assert high["intrinsics"] == {"fx": pytest.approx(320.0), "fy": pytest.approx(320.0),
                                  "cx": 319.5, "cy": 239.5, "pixel_center": "(W-1)/2,(H-1)/2"}
    low = sensor_contract(base_route((160, 120)))
    assert low["intrinsics"]["fx"] == pytest.approx(80.0)
    assert sensor_contract_sha256(base_route()) != sensor_contract_sha256(base_route((160, 120)))
    changed = base_route(); changed["sensor_position_habitat"] = [0.0, 1.6, 0.0]
    assert sensor_contract_sha256(base_route()) != sensor_contract_sha256(changed)


def test_effective_capture_gate_checks_actual_first_frame_arrays_metadata_and_intrinsics(tmp_path):
    import numpy as np
    route = base_route((4, 2))
    route["calibration_id"] = "test-calibration"
    expected = sensor_contract(route)
    cap = tmp_path / "capture"
    frame = cap / "frames" / "000000"
    frame.mkdir(parents=True)
    np.save(frame / "rgb.npy", np.zeros((2, 4, 3), dtype=np.uint8), allow_pickle=False)
    np.save(frame / "depth_m.npy", np.ones((2, 4), dtype=np.float32), allow_pickle=False)
    metadata = {"width": 4, "height": 2, "hfov_deg": 90.0,
                "calibration_id": "test-calibration", "intrinsics": expected["intrinsics"]}
    (frame / "metadata.json").write_text(json.dumps(metadata))
    (frame / "valid_mask.npy").write_bytes(b"fixture")
    raw_hashes = {name: hashlib.sha256((frame / name).read_bytes()).hexdigest()
                  for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json")}
    manifest = {"status": "pass", "scene_id": "s", "sequence_id": "q", "resolution_px": [4, 2],
                "horizontal_fov_deg": 90.0, "frame_count": 1,
                "capture_sensor_contract": expected,
                "capture_sensor_contract_sha256": canonical_sha256(expected),
                "frames": [{"frame_id": "f0", "frame_dir": "frames/000000", "raw_files_sha256": raw_hashes}]}
    (cap / "capture_manifest.json").write_text(json.dumps(manifest))
    result = validate_effective_capture(cap, route, expected_scene_id="s", expected_sequence_id="q")
    assert result["status"] == "pass"
    assert result["rgb_array_shape"] == [2, 4, 3]
    assert result["depth_array_shape"] == [2, 4]
    assert result["intrinsics"]["fx"] == pytest.approx(2.0)

    with pytest.raises(ValueError, match="actual arrays"):
        np.save(frame / "rgb.npy", np.zeros((2, 3, 3), dtype=np.uint8), allow_pickle=False)
        manifest["frames"][0]["raw_files_sha256"]["rgb.npy"] = hashlib.sha256((frame / "rgb.npy").read_bytes()).hexdigest()
        (cap / "capture_manifest.json").write_text(json.dumps(manifest))
        validate_effective_capture(cap, route, expected_scene_id="s", expected_sequence_id="q")


def test_p2_cache_fingerprint_binds_image_capture_and_detector_preprocessing():
    args = dict(capture_manifest_sha256="c" * 64, capture_sensor_contract_sha256="s" * 64,
        resolution_px=[640, 480], rgb_input_sha256_by_frame={"f0": "r" * 64},
        detector_config_sha256="d" * 64, weights_sha256="w" * 64,
        runtime={"input_size": [640, 640]}, preprocessing={"resize": "letterbox", "confidence": .25},
        vocabulary={"classes": 80})
    original = p2_cache_compatibility(**args)
    assert canonical_sha256(original) == canonical_sha256(p2_cache_compatibility(**args))
    for key, value in (("resolution_px", [160, 120]),
                       ("capture_sensor_contract_sha256", "t" * 64),
                       ("rgb_input_sha256_by_frame", {"f0": "x" * 64}),
                       ("preprocessing", {"resize": "different"})):
        changed = dict(args); changed[key] = value
        assert canonical_sha256(original) != canonical_sha256(p2_cache_compatibility(**changed))


def test_image_features_cannot_be_reused_across_source_rgb_or_calibration():
    expected = {"rgb_input_sha256": "r" * 64, "capture_sensor_contract_sha256": "s" * 64,
                "mask_sha256": "m" * 64, "checkpoint_sha256": "c" * 64,
                "model_fingerprint": "vit-b32", "preprocessing_id": "crop-v1", "feature_shape": [512]}
    assert image_feature_source_matches(dict(expected), expected)
    for key, value in (("rgb_input_sha256", "x" * 64),
                       ("capture_sensor_contract_sha256", "t" * 64),
                       ("mask_sha256", "z" * 64)):
        changed = dict(expected); changed[key] = value
        assert not image_feature_source_matches(changed, expected)
    assert not image_feature_source_matches({k: v for k, v in expected.items() if k != "rgb_input_sha256"}, expected)


def test_p3_feature_readers_validate_capture_depth_and_mask_hashes(tmp_path):
    capture_root=tmp_path/'capture'; p2_root=tmp_path/'p2'; frame_dir=capture_root/'frames'/'0000'
    frame_dir.mkdir(parents=True); (p2_root/'masks').mkdir(parents=True)
    raw={}
    for name, payload in (("rgb.npy",b"rgb"),("depth_m.npy",b"depth"),
                          ("valid_mask.npy",b"valid"),("metadata.json",b"{}")):
        path=frame_dir/name; path.write_bytes(payload); raw[name]=hashlib.sha256(payload).hexdigest()
    sensor={"schema":"sensor/v1","resolution_px":[640,480]}; sensor_hash=canonical_sha256(sensor)
    cap={"resolution_px":[640,480],"capture_sensor_contract":sensor,
         "capture_sensor_contract_sha256":sensor_hash,
         "frames":[{"frame_id":"f0","frame_dir":"frames/0000","raw_files_sha256":raw}]}
    (capture_root/'capture_manifest.json').write_text(json.dumps(cap))
    capture_hash=hashlib.sha256((capture_root/'capture_manifest.json').read_bytes()).hexdigest()
    mask=p2_root/'masks'/'f0.npy'; mask.write_bytes(b"mask"); mask_hash=hashlib.sha256(mask.read_bytes()).hexdigest()
    compat={"capture_manifest_sha256":capture_hash,"capture_sensor_contract_sha256":sensor_hash,
            "resolution_px":[640,480],"rgb_input_sha256_by_frame":{"f0":raw['rgb.npy']}}
    p2={"capture_manifest_sha256":capture_hash,"cache_compatibility":compat,
        "cache_compatibility_sha256":canonical_sha256(compat),
        "frames":[{"frame_id":"f0","rgb_input_sha256":raw['rgb.npy'],
                   "detections":[{"detection_id":"f0:det-0","mask_file":"masks/f0.npy","mask_sha256":mask_hash}]}]}
    assert validate_capture_p2_pair(capture_root,p2_root,cap,p2)==sensor_hash
    (frame_dir/'depth_m.npy').write_bytes(b"tampered-depth")
    with pytest.raises(ValueError,match="raw file hash mismatch"):
        validate_capture_p2_pair(capture_root,p2_root,cap,p2)
