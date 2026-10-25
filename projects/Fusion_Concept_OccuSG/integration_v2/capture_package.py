"""Build and validate a minimal, portable Habitat RGB-D package for CG.

The package deliberately contains no semantic labels, annotations, masks from
legacy perception, or Habitat scene assets. Poses are encoded for the pinned
ConceptGraphs Hm3dDataset loader, whose loader applies P @ pose @ P.T.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

SCHEMA = "occusg-conceptgraphs-rgbd-package/v1"
CG_P = np.diag([1.0, -1.0, -1.0, 1.0])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _content_digest(files: dict[str, str], frame_order: list[dict[str, Any]], frames: list[dict[str, Any]]) -> str:
    camera=[]
    for f in frames:
        row={"frame_id":f["frame_id"],"T_W_Copt":f["T_W_Copt"],"intrinsics":f["intrinsics"],
             "source_metadata_sha256":f["source_metadata_sha256"]}
        if "source_raw_files_sha256" in f:
            row["source_raw_files_sha256"]=f["source_raw_files_sha256"]
        camera.append(row)
    return canonical_json_hash({"files_sha256":files,"frames":frame_order,"camera_contract":camera})


def _relative_file(root: Path, name: str) -> Path:
    p = (root / name).resolve()
    if root.resolve() not in p.parents:
        raise ValueError(f"path escapes capture root: {name}")
    if not p.is_file():
        raise FileNotFoundError(p)
    return p


def build_package(capture_root: Path, output_root: Path, *, expected_capture_sha256: str,
                  expected_frame_ids: list[str]) -> dict[str, Any]:
    """Export locked capture frames atomically to a new package directory."""
    capture_root = capture_root.resolve()
    output_root = output_root.resolve()
    source_manifest = capture_root / "capture_manifest.json"
    actual_capture_hash = sha256_file(source_manifest)
    if actual_capture_hash != expected_capture_sha256:
        raise ValueError("capture manifest SHA-256 differs from the locked proposal")
    capture = json.loads(source_manifest.read_text())
    frames = sorted(capture.get("frames", []), key=lambda f: int(f["frame_index"]))
    if [f["frame_id"] for f in frames] != expected_frame_ids:
        raise ValueError("capture frame IDs/order differ from the locked proposal")
    if output_root.exists():
        raise FileExistsError(f"output already exists; choose a new versioned directory: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    scene_dir = temp_root / "dataset" / "pilot_00808"
    scene_dir.mkdir(parents=True)
    files: dict[str, str] = {}
    frame_rows = []
    try:
        for row in frames:
            idx = int(row["frame_index"])
            stem = f"{idx:06d}"
            frame_dir = (capture_root / row["frame_dir"]).resolve()
            if capture_root not in frame_dir.parents:
                raise ValueError(f"frame directory escapes capture root: {row['frame_dir']}")
            metadata = json.loads(_relative_file(capture_root, f"{row['frame_dir']}/metadata.json").read_text())
            if metadata["frame_id"] != row["frame_id"] or int(metadata["frame_index"]) != idx:
                raise ValueError(f"capture metadata identity/order mismatch at frame {idx}")
            if metadata["depth_units"] != "m" or metadata["depth_representation"] != "axial":
                raise ValueError("capture depth must be axial metres")
            source_rgb_path=_relative_file(capture_root, f"{row['frame_dir']}/rgb.npy")
            source_depth_path=_relative_file(capture_root, f"{row['frame_dir']}/depth_m.npy")
            source_valid_path=_relative_file(capture_root, f"{row['frame_dir']}/valid_mask.npy")
            rgb = np.load(source_rgb_path, allow_pickle=False)
            depth = np.load(source_depth_path, allow_pickle=False).astype(np.float32)
            valid = np.load(source_valid_path, allow_pickle=False).astype(bool)
            h, w = int(metadata["height"]), int(metadata["width"])
            if rgb.shape[:2] != (h, w) or depth.shape != (h, w) or valid.shape != (h, w):
                raise ValueError(f"RGB/depth/valid-mask shape mismatch at frame {idx}")
            rgb = np.asarray(rgb[..., :3], dtype=np.uint8)
            if not np.all(np.isfinite(depth[valid])) or np.any(depth[valid] <= 0):
                raise ValueError(f"invalid metric depth marked valid at frame {idx}")
            depth = depth.copy()
            depth[~valid] = np.nan

            # Reuse the already-verified Habitat capture adapter as calibration authority.
            source_root = Path(__file__).resolve().parents[1]
            if str(source_root / "src") not in sys.path:
                sys.path.insert(0, str(source_root / "src"))
            if str(source_root) not in sys.path:
                sys.path.insert(0, str(source_root))
            from adapters.habitat.frames import frame_from_capture_record
            rgbd, _ = frame_from_capture_record(row, capture_root)
            intrinsics = rgbd.intrinsics
            intr = {"width": intrinsics.width, "height": intrinsics.height,
                    "fx": intrinsics.fx, "fy": intrinsics.fy, "cx": intrinsics.cx,
                    "cy": intrinsics.cy, "calibration_id": intrinsics.calibration_id}
            T_w_opt = rgbd.T_W_Copt.matrix
            # Hm3dDataset applies P @ file_pose @ P.T. This encoding makes its
            # returned absolute pose exactly the capture's canonical T_W_Copt.
            file_pose = CG_P @ T_w_opt @ CG_P

            rgb_rel = f"dataset/pilot_00808/{stem}.png"
            depth_rel = f"dataset/pilot_00808/{stem}_depth.npy"
            pose_rel = f"dataset/pilot_00808/{stem}.json"
            valid_rel = f"dataset/pilot_00808/{stem}_valid.npy"
            Image.fromarray(rgb, mode="RGB").save(temp_root / rgb_rel, format="PNG", optimize=False)
            np.save(temp_root / depth_rel, depth, allow_pickle=False)
            np.save(temp_root / valid_rel, valid, allow_pickle=False)
            (temp_root / pose_rel).write_text(json.dumps({
                "pose": file_pose.tolist(), "frame_id": row["frame_id"],
                "timestamp_s": float(row["timestamp_s"]), "pose_encoding": "P @ T_W_Copt @ P.T",
            }, indent=2) + "\n")
            for rel in (rgb_rel, depth_rel, pose_rel, valid_rel):
                files[rel] = sha256_file(temp_root / rel)
            frame_rows.append({
                "order": idx, "frame_id": row["frame_id"], "timestamp_s": float(row["timestamp_s"]),
                "rgb": rgb_rel, "depth_axial_m": depth_rel, "valid_depth_mask": valid_rel,
                "pose": pose_rel, "T_W_Copt": T_w_opt.tolist(), "intrinsics": intr,
                "source_metadata_sha256": sha256_file(frame_dir / "metadata.json"),
                "source_raw_files_sha256": {"rgb.npy":sha256_file(source_rgb_path),
                                            "depth_m.npy":sha256_file(source_depth_path),
                                            "valid_mask.npy":sha256_file(source_valid_path)},
            })
        h, w = frame_rows[0]["intrinsics"]["height"], frame_rows[0]["intrinsics"]["width"]
        if any(x["intrinsics"] != frame_rows[0]["intrinsics"] for x in frame_rows):
            raise ValueError("per-frame calibration differs; this package profile requires one fixed K/resolution")
        dataconfig = {
            "dataset_name": "hm3d",
            "camera_params": {"image_height": h, "image_width": w,
                              **{k: frame_rows[0]["intrinsics"][k] for k in ("fx", "fy", "cx", "cy")},
                              "png_depth_scale": 1.0},
        }
        (temp_root / "hm3d_adapter_dataconfig.yaml").write_text(
            "dataset_name: hm3d\ncamera_params:\n"
            f"  image_height: {h}\n  image_width: {w}\n"
            f"  fx: {dataconfig['camera_params']['fx']:.12g}\n  fy: {dataconfig['camera_params']['fy']:.12g}\n"
            f"  cx: {dataconfig['camera_params']['cx']:.12g}\n  cy: {dataconfig['camera_params']['cy']:.12g}\n"
            "  png_depth_scale: 1.0\n", encoding="utf-8")
        files["hm3d_adapter_dataconfig.yaml"] = sha256_file(temp_root / "hm3d_adapter_dataconfig.yaml")
        manifest = {
            "schema": SCHEMA, "schema_version": 1,
            "source": {"kind": "locked_habitat_raw_capture", "capture_manifest_sha256": actual_capture_hash,
                       "capture_root_relative_to_workspace": "artifacts/runs/q1_00808_20261011T160830Z/capture"},
            "scene_id": capture["scene_id"], "dataset_loader": "ConceptGraphs Hm3dDataset/get_dataset",
            "dataset_license_notice": capture.get("scene_license_note", "license notice absent from capture manifest; verify before sharing"),
            "third_party_upload_authorization": "not_verified; no upload performed",
            "reference_commit": "93277a02bd89171f8121e84203121cf7af9ebb5d",
            "rgb": {"format": "PNG", "channels": "RGB", "source": "raw capture RGB, alpha dropped only"},
            "depth": {"format": "NumPy float32", "units": "m", "representation": "axial", "invalid": "NaN; valid mask separately retained"},
            "camera_frame": "camera_optical (+X right,+Y down,+Z forward)",
            "world_frame": "canonical Z-up; C.x=H.x,C.y=-H.z,C.z=H.y",
            "pose_contract": {"stored": "P @ T_W_Copt @ P.T", "P": CG_P.tolist(),
                              "reference_loader": "Hm3dDataset applies P @ pose @ P.T; returned absolute pose equals T_W_Copt",
                              "mapping_batch_uses": "dataset.poses[idx] absolute pose; relative transformed_poses is not used for object lifting",
                              "local_to_capture_world": frame_rows[0]["T_W_Copt"]},
            "intrinsics_pixel_center": "Habitat adapter: cx=(W-1)/2, cy=(H-1)/2; no resize/crop",
            "frame_order": [{k: row[k] for k in ("order", "frame_id", "timestamp_s")} for row in frame_rows],
            "frames": frame_rows, "files_sha256": files,
            "excluded": ["semantic GT", "human annotations", "legacy P2/P3 predictions", "Habitat scene assets", "credentials"],
            "predictor_inputs_only": True,
        }
        manifest["content_sha256"] = _content_digest(files,manifest["frame_order"],frame_rows)
        (temp_root / "input_package_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_root, output_root)
        return manifest
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def validate_package(package_root: Path) -> dict[str, Any]:
    package_root = package_root.resolve()
    manifest_path = package_root / "input_package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported package schema")
    for rel, digest in manifest["files_sha256"].items():
        path = _relative_file(package_root, rel)
        if sha256_file(path) != digest:
            raise ValueError(f"file hash mismatch: {rel}")
    frames = manifest["frames"]
    ids=[x["frame_id"] for x in frames]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate frame IDs")
    if [x["order"] for x in frames] != list(range(len(frames))):
        raise ValueError("frame order is not contiguous/deterministic")
    for fr in frames:
        T = np.asarray(fr["T_W_Copt"], dtype=np.float64)
        stored = np.asarray(json.loads((package_root / fr["pose"]).read_text())["pose"], dtype=np.float64)
        if not np.allclose(CG_P @ stored @ CG_P, T, atol=1e-8, rtol=0):
            raise ValueError(f"ConceptGraphs pose adapter round-trip failed: {fr['frame_id']}")
        d = np.load(package_root / fr["depth_axial_m"], allow_pickle=False)
        valid = np.load(package_root / fr["valid_depth_mask"], allow_pickle=False)
        if d.shape != valid.shape or d.ndim != 2:
            raise ValueError(f"depth/mask shape mismatch: {fr['frame_id']}")
        with Image.open(package_root / fr["rgb"]) as im:
            if im.mode != "RGB" or im.size != (fr["intrinsics"]["width"], fr["intrinsics"]["height"]):
                raise ValueError(f"RGB resolution/channel mismatch: {fr['frame_id']}")
        if d.shape != (fr["intrinsics"]["height"], fr["intrinsics"]["width"]):
            raise ValueError(f"depth resolution differs from camera intrinsics: {fr['frame_id']}")
        if np.any(~np.isfinite(d[valid])) or np.any(d[valid] <= 0) or np.any(np.isfinite(d[~valid])):
            raise ValueError(f"depth validity encoding mismatch: {fr['frame_id']}")
    expected_hash=_content_digest(manifest["files_sha256"],manifest["frame_order"],frames)
    if expected_hash != manifest.get("content_sha256"):
        raise ValueError("package content hash does not match frame/calibration/file manifest")
    return manifest
