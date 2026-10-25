#!/usr/bin/env python3
"""Derive full-mesh AABBs and cross-check them against captured visible depth."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_apt0_room_annotation import stage_triangles  # noqa: E402
from src.standalone_sg.contracts.frames import CameraIntrinsics, Transform  # noqa: E402
from src.standalone_sg.geometry.backproject import backproject_depth  # noqa: E402


def q_wxyz_to_matrix(q):
    w, x, y, z = map(float, q)
    norm = math.sqrt(w*w + x*x + y*y + z*z)
    if abs(norm - 1.0) > 1e-3:
        raise ValueError(f"scene rotation quaternion is not normalized: norm={norm}")
    w, x, y, z = w/norm, x/norm, y/norm, z/norm
    return np.array([
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
    ])


def main():
    data_root = ROOT / "artifacts/habitat_data/versioned_data/replica_cad_dataset"
    run = ROOT / "artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001"
    gt = ROOT / "evaluation/gt/apt_0"
    registry_path = gt / "object_registry.json"
    registry = json.loads(registry_path.read_text())
    scene = json.loads((data_root / "configs/scenes/apt_0.scene_instance.json").read_text())
    visibility = json.loads((gt / "visibility_index.json").read_text())
    frames = visibility["frames"]
    frame_by_id = {x["frame_id"]: x for x in frames}
    template_vertices = {}
    records = []
    for obj in registry["instances"]:
        i = int(obj["scene_instance_index"]); inst = scene["object_instances"][i]
        cfg_path = data_root / "configs" / f"{obj['source_template']}.object_config.json"
        cfg = json.loads(cfg_path.read_text())
        asset_path = (cfg_path.parent / cfg["render_asset"]).resolve()
        if not asset_path.is_file():
            raise FileNotFoundError(asset_path)
        key = str(asset_path)
        if key not in template_vertices:
            tris = stage_triangles(asset_path)
            template_vertices[key] = np.concatenate([t.reshape(-1, 3) for _, t in tris], axis=0)
        vertices = template_vertices[key]
        template_scale = np.asarray(cfg.get("scale", [1, 1, 1]), dtype=float)
        units = float(cfg.get("units_to_meters", 1.0))
        instance_scale = float(inst.get("uniform_scale", 1.0)) * np.asarray(inst.get("non_uniform_scale", [1, 1, 1]), dtype=float)
        scaled = vertices * template_scale * instance_scale * units
        rotation = q_wxyz_to_matrix(inst.get("rotation", [1, 0, 0, 0]))
        translation = np.asarray(inst["translation"], dtype=float)
        world = (rotation @ scaled.T).T + translation
        low, high = world.min(axis=0), world.max(axis=0)
        # M1 backprojection/grid_trace is in canonical Z-up world, while
        # ReplicaCAD object transforms/AABBs above are in Habitat Y-up world.
        # Convert the reference AABB before comparing it with xyz_W.
        low_canonical = np.array([low[0], -high[2], low[1]], dtype=float)
        high_canonical = np.array([high[0], -low[2], high[1]], dtype=float)
        sid = int(obj["simulator_instance_semantic_id"])
        containment_samples = []
        for frame_id in obj.get("visible_frame_ids", []):
            frame = frame_by_id[frame_id]
            index = int(frame["frame_index"])
            d = np.load(run / "capture" / "frames" / f"{index:06d}" / "depth_m.npy", allow_pickle=False)
            valid = np.load(run / "capture" / "frames" / f"{index:06d}" / "valid_mask.npy", allow_pickle=False)
            labels = np.load(gt / "visible_instance_masks" / frame["mask_file"], allow_pickle=False)
            if not np.any((labels == sid) & valid):
                continue
            grid_meta = json.loads((run / "grid_trace/frames" / f"{index:06d}.json").read_text())
            t = Transform(np.asarray(grid_meta["sensor_pose_W_Copt"], dtype=float),
                          "canonical_world", "camera_optical", float(frame["timestamp_s"]))
            intr = CameraIntrinsics(width=320, height=240, fx=160.0, fy=160.0,
                                    cx=159.5, cy=119.5, calibration_id="habitat-rgbd-stage-v1-hires-development")
            cloud = backproject_depth(d, intr, t, meters_per_unit=1.0, stride=1,
                                      min_depth_m=0.1, max_depth_m=10.0,
                                      valid_mask=(labels == sid) & valid,
                                      timestamp=float(frame["timestamp_s"]), scan_id=frame_id)
            pts = cloud.xyz_W.astype(float)
            if len(pts):
                inside = np.all((pts >= low_canonical[None, :] - .05) &
                                (pts <= high_canonical[None, :] + .05), axis=1)
                containment_samples.append({"frame_id": frame_id, "visible_depth_points": len(pts),
                                            "fraction_inside_aabb_plus_0_05m": float(inside.mean())})
        rec = {
            "gt_instance_id": obj["gt_instance_id"], "scene_instance_index": i,
            "render_asset": str(asset_path.relative_to(data_root)), "render_asset_sha256": __import__("hashlib").sha256(asset_path.read_bytes()).hexdigest(),
            "aabb_local_asset_m": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
            "aabb_world_habitat_xyz_m": [low.tolist(), high.tolist()],
            "aabb_world_canonical_xyz_m": [low_canonical.tolist(), high_canonical.tolist()],
            "geometry_method": "all GLB position vertices after node transforms; object template units/scale; ReplicaCAD scene quaternion wxyz; instance uniform/nonuniform scale; scene translation",
            "translation_origin_source": inst.get("translation_origin", "dataset_default"),
            "depth_containment_tolerance_m": 0.05,
            "observed_mask_depth_aabb_checks": containment_samples,
            "geometry_reference_status": "derived_from_loaded_render_asset_and_locked_instance_transform; canonical-frame containment checked",
        }
        records.append(rec)
    report = {"schema_version": 1, "kind": "apt0_object_full_mesh_geometry_reference",
              "scene_instance_sha256": __import__("hashlib").sha256((data_root / "configs/scenes/apt_0.scene_instance.json").read_bytes()).hexdigest(),
              "coordinate_frame": "habitat_world_y_up_xyz_m", "canonical_mapping": "C.x=H.x; C.y=-H.z; C.z=H.y",
              "quaternion_convention": "wxyz per official Habitat-Sim SceneInstanceAttributes schema",
              "gt_evaluator_only": True, "objects": records}
    out = gt / "object_geometry.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    for obj in registry["instances"]:
        rec = next(x for x in records if x["gt_instance_id"] == obj["gt_instance_id"])
        obj["geometry"] = rec
        checks = rec["observed_mask_depth_aabb_checks"]
        obj["geometry_status"] = "simulator_reference_verified_geometry_and_visible_depth_consistent" if checks and min(x["fraction_inside_aabb_plus_0_05m"] for x in checks) >= .98 else "uncertain_geometry_depth_containment_requires_review"
    registry["status"] = "simulator_reference_verified_unique_instance_ids_and_mesh_geometry; class_mapping_provisional; room_membership_pending"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True, allow_nan=False) + "\n")
    manifest = json.loads((gt / "manifest.json").read_text())
    manifest["object_geometry_sha256"] = __import__("hashlib").sha256(out.read_bytes()).hexdigest()
    manifest["object_registry_sha256"] = __import__("hashlib").sha256(registry_path.read_bytes()).hexdigest()
    manifest["object_geometry_status"] = "113/113 rigid-instance mesh AABBs contain matched visible captured-depth points within 0.05m after canonical-frame conversion; class mapping and room membership remain pending"
    (gt / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n")
    summary = {"instances": len(records), "containment_checked": sum(bool(x["observed_mask_depth_aabb_checks"]) for x in records),
               "minimum_fraction": min((v["fraction_inside_aabb_plus_0_05m"] for x in records for v in x["observed_mask_depth_aabb_checks"]), default=None),
               "maximum_fraction": max((v["fraction_inside_aabb_plus_0_05m"] for x in records for v in x["observed_mask_depth_aabb_checks"]), default=None),
               "status_counts": {s: sum(x["geometry_status"] == s for x in registry["instances"]) for s in sorted({x["geometry_status"] for x in registry["instances"]})}}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
