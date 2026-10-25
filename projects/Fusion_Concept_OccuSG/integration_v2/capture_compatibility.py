"""Versioned Habitat sensor identity used to prevent incompatible cache reuse."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sensor_contract(config: dict[str, Any]) -> dict[str, Any]:
    """Describe image/depth calibration and sensor settings from effective config."""
    from adapters.habitat.calibration import habitat_intrinsics_values

    width, height = (int(v) for v in config["resolution_px"])
    hfov = float(config["horizontal_fov_deg"])
    calibration_id = str(config.get("calibration_id", "habitat-rgbd-v1"))
    intr = habitat_intrinsics_values(width, height, hfov)
    profile = config.get("capture_profile") or {}
    return {
        "schema": "habitat-rgbd-sensor-contract/v1",
        "capture_profile_id": profile.get("profile_id", "route_config_explicit"),
        "capture_profile_sha256": profile.get("profile_sha256"),
        "resolution_px": [width, height], "horizontal_fov_deg": hfov,
        "calibration_id": calibration_id,
        "intrinsics_adapter": "adapters.habitat.frames.habitat_camera_intrinsics",
        "intrinsics": {**intr,
                       "pixel_center": "(W-1)/2,(H-1)/2"},
        "sensor_position_habitat": [float(v) for v in config["sensor_position_habitat"]],
        "sensor_orientation_habitat_rad": [float(v) for v in config.get("sensor_orientation_habitat_rad", [0, 0, 0])],
        "depth_range_m": [float(config["min_depth_m"]), float(config["max_depth_m"])],
        "depth_representation": "axial", "depth_units": "m",
        "rgb_channel_order": "RGB", "capture_kind": "native_habitat_sensor",
    }


def sensor_contract_sha256(config: dict[str, Any]) -> str:
    return canonical_sha256(sensor_contract(config))


def validate_effective_capture(capture_root: Path, effective_route: dict[str, Any], *,
                               expected_scene_id: str, expected_sequence_id: str,
                               expected_effective_config_sha256: str | None = None) -> dict[str, Any]:
    """Verify the first native capture really used the resolved sensor profile.

    This intentionally reads the actual frame arrays and metadata, rather than
    trusting the profile name or the capture command arguments.
    """
    import math
    import numpy as np

    root = Path(capture_root)
    manifest_path = root / "capture_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("status"), manifest.get("scene_id"), manifest.get("sequence_id")) != (
            "pass", expected_scene_id, expected_sequence_id):
        raise ValueError("capture validation: manifest status or scene/sequence mismatch")
    if (expected_effective_config_sha256 is not None
            and manifest.get("capture_config_sha256") != expected_effective_config_sha256):
        raise ValueError("capture validation: capture worker did not consume the expected effective route config")
    expected_sensor = sensor_contract(effective_route)
    expected_sensor_hash = canonical_sha256(expected_sensor)
    if manifest.get("capture_sensor_contract") != expected_sensor:
        raise ValueError("capture validation: actual sensor contract differs from effective route/profile")
    if manifest.get("capture_sensor_contract_sha256") != expected_sensor_hash:
        raise ValueError("capture validation: sensor contract hash mismatch")
    width, height = (int(v) for v in effective_route["resolution_px"])
    hfov = float(effective_route["horizontal_fov_deg"])
    if manifest.get("resolution_px") != [width, height]:
        raise ValueError(f"capture validation: manifest resolution is {manifest.get('resolution_px')}, expected {[width, height]}")
    if not math.isclose(float(manifest.get("horizontal_fov_deg", float("nan"))), hfov, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("capture validation: manifest HFOV differs from route")
    frames = manifest.get("frames", [])
    if not frames or int(manifest.get("frame_count", -1)) != len(frames):
        raise ValueError("capture validation: no complete frame population")
    first = frames[0]
    frame_dir = root / str(first["frame_dir"])
    expected_hashes = first.get("raw_files_sha256", {})
    for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"):
        path = frame_dir / name
        if name not in expected_hashes or not path.is_file():
            raise ValueError(f"capture validation: first-frame asset/hash missing: {name}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hashes[name]:
            raise ValueError(f"capture validation: first-frame asset hash mismatch: {name}")
    metadata = json.loads((frame_dir / "metadata.json").read_text(encoding="utf-8"))
    if (int(metadata.get("width", -1)), int(metadata.get("height", -1))) != (width, height):
        raise ValueError("capture validation: first-frame metadata resolution mismatch")
    if not math.isclose(float(metadata.get("hfov_deg", float("nan"))), hfov, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("capture validation: first-frame HFOV mismatch")
    if metadata.get("calibration_id") != expected_sensor["calibration_id"]:
        raise ValueError("capture validation: first-frame calibration ID mismatch")
    intr = metadata.get("intrinsics", {})
    expected_intr = expected_sensor["intrinsics"]
    for key in ("fx", "fy", "cx", "cy"):
        if not math.isclose(float(intr.get(key, float("nan"))), float(expected_intr[key]), rel_tol=0.0, abs_tol=1e-9):
            raise ValueError(f"capture validation: first-frame K.{key} differs from calibration adapter")
    rgb = np.load(frame_dir / "rgb.npy", allow_pickle=False)
    depth = np.load(frame_dir / "depth_m.npy", allow_pickle=False)
    if rgb.shape[:2] != (height, width) or depth.shape != (height, width):
        raise ValueError(f"capture validation: actual arrays are RGB={rgb.shape}, depth={depth.shape}; expected {(height, width)}")
    return {
        "status": "pass", "checked_frame_id": str(first["frame_id"]),
        "resolution_px": [width, height], "rgb_array_shape": list(rgb.shape),
        "depth_array_shape": list(depth.shape), "horizontal_fov_deg": hfov,
        "calibration_id": expected_sensor["calibration_id"], "intrinsics": expected_intr,
        "sensor_contract_sha256": expected_sensor_hash,
        "first_frame_metadata_sha256": hashlib.sha256((frame_dir / "metadata.json").read_bytes()).hexdigest(),
        "rgb_sha256": hashlib.sha256((frame_dir / "rgb.npy").read_bytes()).hexdigest(),
        "depth_sha256": hashlib.sha256((frame_dir / "depth_m.npy").read_bytes()).hexdigest(),
        "validation_basis": "effective route contract + first-frame metadata + loaded native RGB/depth array shapes",
    }


def p2_cache_compatibility(*, capture_manifest_sha256: str, capture_sensor_contract_sha256: str | None,
                           resolution_px: list[int], rgb_input_sha256_by_frame: dict[str, str],
                           detector_config_sha256: str, weights_sha256: str,
                           runtime: dict[str, Any], preprocessing: dict[str, Any],
                           vocabulary: dict[str, Any]) -> dict[str, Any]:
    """Bind P2 masks to their exact native image/calibration and inference contract."""
    return {
        "schema": "habitat-p2-cache-compatibility/v1",
        "capture_manifest_sha256": capture_manifest_sha256,
        "capture_sensor_contract_sha256": capture_sensor_contract_sha256,
        "resolution_px": [int(v) for v in resolution_px],
        "rgb_input_sha256_by_frame": dict(sorted(rgb_input_sha256_by_frame.items())),
        "detector_config_sha256": detector_config_sha256,
        "weights_sha256": weights_sha256,
        "runtime": runtime,
        "preprocessing": preprocessing,
        "vocabulary": vocabulary,
    }


def image_feature_source_matches(existing: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Require exact image/mask/calibration/model/preprocess source for feature reuse."""
    keys = ("rgb_input_sha256", "capture_sensor_contract_sha256", "mask_sha256",
            "checkpoint_sha256", "model_fingerprint", "preprocessing_id", "feature_shape")
    return all(existing.get(key) == expected.get(key) for key in keys)


def validate_capture_p2_pair(capture_root: Path, p2_root: Path,
                              capture_manifest: dict[str, Any], p2_index: dict[str, Any]) -> str | None:
    """Validate current raw files against a new-contract P2 cache; return sensor hash.

    Legacy indexes without the compatibility block remain readable by legacy
    callers. New profiles always emit the block and get strict file-level checks.
    """
    def file_sha(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    cap_path = capture_root / "capture_manifest.json"
    if p2_index.get("capture_manifest_sha256") != file_sha(cap_path):
        raise ValueError("P2 index is not hash-bound to this capture manifest")
    compatibility = p2_index.get("cache_compatibility")
    if not compatibility:
        return None
    if canonical_sha256(compatibility) != p2_index.get("cache_compatibility_sha256"):
        raise ValueError("P2 cache compatibility fingerprint is corrupt")
    sensor = capture_manifest.get("capture_sensor_contract")
    sensor_hash = capture_manifest.get("capture_sensor_contract_sha256")
    if not sensor_hash or canonical_sha256(sensor or {}) != sensor_hash:
        raise ValueError("capture sensor/calibration contract hash mismatch")
    if (compatibility.get("capture_manifest_sha256") != file_sha(cap_path)
            or compatibility.get("capture_sensor_contract_sha256") != sensor_hash
            or compatibility.get("resolution_px") != capture_manifest.get("resolution_px")):
        raise ValueError("P2 cache sensor/resolution contract differs from capture")
    cap_by_id = {str(row["frame_id"]): row for row in capture_manifest.get("frames", [])}
    p2_by_id = {str(row["frame_id"]): row for row in p2_index.get("frames", [])}
    cap_order = [str(row["frame_id"]) for row in capture_manifest.get("frames", [])]
    p2_order = [str(row["frame_id"]) for row in p2_index.get("frames", [])]
    if cap_order != p2_order or set(cap_by_id) != set(p2_by_id):
        raise ValueError("P2 frame population/order identity differs from capture")
    for frame_id, cap_frame in cap_by_id.items():
        frame_dir = capture_root / str(cap_frame["frame_dir"])
        raw_hashes = cap_frame.get("raw_files_sha256", {})
        required = {"rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"}
        if not required <= set(raw_hashes):
            raise ValueError(f"capture raw-file hashes are incomplete: {frame_id}")
        for name, expected in raw_hashes.items():
            path = frame_dir / str(name)
            if not path.is_file() or file_sha(path) != expected:
                raise ValueError(f"capture raw file hash mismatch: {frame_id}:{name}")
        rgb_hash = raw_hashes["rgb.npy"]
        p2_frame = p2_by_id[frame_id]
        if p2_frame.get("rgb_input_sha256") != rgb_hash:
            raise ValueError(f"P2 RGB source differs from capture: {frame_id}")
        if compatibility.get("rgb_input_sha256_by_frame", {}).get(frame_id) != rgb_hash:
            raise ValueError(f"P2 compatibility RGB source differs from capture: {frame_id}")
        for det in p2_by_id[frame_id].get("detections", []):
            mask_path = p2_root / str(det["mask_file"])
            if not mask_path.is_file() or file_sha(mask_path) != det.get("mask_sha256"):
                raise ValueError(f"P2 mask file missing/hash mismatch: {det.get('detection_id')}")
    return str(sensor_hash)
