#!/usr/bin/env python3
"""Render evaluator-only per-instance masks for the locked apt_0 capture.

The script clones dataset *configuration* into an evaluation-only tree and
assigns a distinct semantic_id to each rigid scene instance. Asset geometry,
instance transforms, sensor poses and scene state are unchanged. RGB/depth
must match the locked capture before any rendered mask is accepted.

Run with the pinned Habitat-Sim 0.3.3 environment. This does not feed masks or
IDs to the graph builder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _symlink(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"refusing to replace evaluator dataset path: {dest}")
    dest.symlink_to(source.resolve(), target_is_directory=source.is_dir())


def prepare_dataset(dataset_root: Path, scene_file: Path, output_root: Path) -> tuple[Path, dict]:
    dataset_root = dataset_root.resolve()
    scene_file = scene_file.resolve()
    tree = output_root / "evaluator_dataset_v1"
    if tree.exists():
        raise FileExistsError(f"evaluator dataset already exists; choose a fresh output root: {tree}")
    tree.mkdir(parents=True)

    for name in ("objects", "stages", "urdf"):
        source = dataset_root / name
        if source.exists():
            _symlink(source, tree / name)
    # The reduced dataset config intentionally omits unrelated ReplicaCAD
    # variants/navmesh entries. It preserves apt_0's source templates/assets.
    for name in ("stages", "lighting", "ssd"):
        _symlink(dataset_root / "configs" / name, tree / "configs" / name)
    object_cfg_dir = tree / "configs" / "objects"
    object_cfg_dir.mkdir(parents=True)
    for source in sorted((dataset_root / "configs" / "objects").glob("*.json")):
        (object_cfg_dir / source.name).symlink_to(source.resolve())

    scene = json.loads(scene_file.read_text(encoding="utf-8"))
    object_instances = scene.get("object_instances", [])
    registry = []
    id_to_gt: dict[int, str] = {}
    for index, instance in enumerate(object_instances):
        source_template = str(instance["template_name"])
        source_config = dataset_root / "configs" / f"{source_template}.object_config.json"
        if not source_config.is_file():
            raise FileNotFoundError(f"object template config not found: {source_config}")
        config = json.loads(source_config.read_text(encoding="utf-8"))
        gt_id = f"apt0-inst-{index:04d}"
        semantic_id = 2000 + index
        config["semantic_id"] = semantic_id
        template_token = f"gt_apt0_instance_{index:04d}"
        config_path = object_cfg_dir / f"{template_token}.object_config.json"
        config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        instance["template_name"] = f"objects/{template_token}"
        id_to_gt[semantic_id] = gt_id
        registry.append({
            "gt_instance_id": gt_id,
            "scene_instance_index": index,
            "source_template": source_template,
            "evaluator_template": f"objects/{template_token}",
            "simulator_instance_semantic_id": semantic_id,
            "semantic_category_id_from_source_config": json.loads(source_config.read_text(encoding="utf-8")).get("semantic_id"),
            "class_name_proposal": source_template.removeprefix("objects/frl_apartment_"),
            "class_status": "ai_assisted_provisional",
            "translation_habitat_m": instance.get("translation"),
            "rotation_quaternion_wxyz": instance.get("rotation"),
            "translation_origin": instance.get("translation_origin", "dataset_default"),
            "template_scale": config.get("scale", [1.0, 1.0, 1.0]),
            "instance_uniform_scale": instance.get("uniform_scale", 1.0),
            "instance_non_uniform_scale": instance.get("non_uniform_scale", [1.0, 1.0, 1.0]),
            "motion_type_declared": instance.get("motion_type"),
            "loaded_status": "pending_render_equivalence",
            "geometry_status": "asset_template_and_transform_recorded; full geometry bounds pending",
        })

    custom_scene = tree / "configs" / "scenes" / "apt_0_gt_instance.scene_instance.json"
    custom_scene.parent.mkdir(parents=True, exist_ok=True)
    custom_scene.write_text(json.dumps(scene, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    config = {
        "stages": {"paths": {".json": ["configs/stages"]}},
        "objects": {"paths": {".json": ["configs/objects"]}},
        "articulated_objects": {"paths": {".urdf": ["urdf/*/"]}},
        "light_setups": {"default_attributes": {"positive_intensity_scale": 2.5, "negative_intensity_scale": 0.1},
                          "paths": {".json": ["configs/lighting"]}},
        "scene_instances": {"default_attributes": {"semantic_scene_instance": "replicaCAD_ssd_map"},
                            "paths": {".json": ["configs/scenes"]}},
        "semantic_scene_descriptor_instances": {"replicaCAD_ssd_map": "configs/ssd/replicaCAD_semantic_lexicon.json"},
    }
    dataset_config = tree / "apt0_gt.scene_dataset_config.json"
    dataset_config.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "status": "prepared_not_yet_render_verified",
        "source_dataset_root": str(dataset_root),
        "source_scene_file": str(scene_file),
        "source_scene_sha256": sha256(scene_file),
        "source_dataset_config_sha256": sha256(dataset_root / "replicaCAD.scene_dataset_config.json"),
        "evaluator_scene_file": str(custom_scene),
        "evaluator_scene_sha256": sha256(custom_scene),
        "evaluator_dataset_config": str(dataset_config),
        "evaluator_dataset_config_sha256": sha256(dataset_config),
        "geometry_pose_scale_changes": 0,
        "unique_instance_ids": len(registry),
        "instance_id_to_gt": {str(k): v for k, v in id_to_gt.items()},
        "objects": registry,
        "articulated_declarations_excluded_by_pinned_runtime": scene.get("articulated_object_instances", []),
        "articulated_exclusion_reason": "Pinned Habitat-Sim 0.3.3 build reports articulated-object creation unsupported without Bullet; these are not treated as visible instances in the captured runtime.",
        "gt_is_evaluator_only": True,
    }
    return custom_scene, report


def render(capture: Path, route_config: Path, dataset_config: Path, scene_file: Path,
           out_dir: Path, registry_report: dict, *, max_frames: int | None = None) -> dict:
    try:
        import habitat_sim  # type: ignore
        import magnum as mn  # type: ignore
        import quaternion  # type: ignore
        from habitat_sim.utils.common import quat_from_angle_axis  # type: ignore
    except ImportError as exc:
        raise RuntimeError("run with the pinned Habitat-Sim 0.3.3 environment") from exc

    cap_manifest = json.loads((capture / "capture_manifest.json").read_text(encoding="utf-8"))
    cfg = json.loads(route_config.read_text(encoding="utf-8"))
    route = cfg["route"]
    width, height = map(int, cap_manifest["resolution_px"])
    hfov = float(cap_manifest["horizontal_fov_deg"])
    sensor_position = cfg["sensor_position_habitat"]
    sensor_orientation = cfg.get("sensor_orientation_habitat_rad", [0, 0, 0])

    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_id = str(scene_file.resolve())
    sim_cfg.scene_dataset_config_file = str(dataset_config.resolve())
    sim_cfg.create_renderer = True
    sim_cfg.requires_textures = True
    sim_cfg.enable_physics = False
    sim_cfg.gpu_device_id = int(cfg.get("gpu_device_id", -1))
    agent_cfg = habitat_sim.AgentConfiguration()
    specs = []
    for kind, uuid in ((habitat_sim.SensorType.COLOR, "color_sensor"),
                       (habitat_sim.SensorType.DEPTH, "depth_sensor"),
                       (habitat_sim.SensorType.SEMANTIC, "instance_sensor")):
        spec = habitat_sim.CameraSensorSpec()
        spec.uuid = uuid
        spec.sensor_type = kind
        spec.resolution = mn.Vector2i(height, width)
        spec.position = mn.Vector3(*sensor_position)
        spec.orientation = mn.Vector3(*sensor_orientation)
        spec.hfov = mn.Deg(hfov)
        spec.near = float(cfg.get("min_depth_m", 0.1))
        spec.far = float(cfg.get("max_depth_m", 10.0))
        specs.append(spec)
    agent_cfg.sensor_specifications = specs
    sim = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
    agent = sim.initialize_agent(0)
    out_dir.mkdir(parents=True, exist_ok=True)
    mapping = {int(k): v for k, v in registry_report["instance_id_to_gt"].items()}
    frames = cap_manifest["frames"][:max_frames]
    rows = []
    try:
        for rec in frames:
            i = int(rec["frame_index"])
            waypoint = route[i]
            state = agent.get_state()
            state.position = np.asarray(waypoint["position_habitat"], dtype=np.float32)
            state.rotation = quat_from_angle_axis(float(waypoint.get("yaw_rad", 0.0)), np.array([0, 1, 0], dtype=np.float32))
            agent.set_state(state, infer_sensor_states=True)
            observations = sim.get_sensor_observations()
            rgb = np.asarray(observations["color_sensor"])
            depth = np.asarray(observations["depth_sensor"], dtype=np.float32)
            labels = np.asarray(observations["instance_sensor"], dtype=np.uint32)
            source = capture / rec["frame_dir"]
            rgb_ref = np.load(source / "rgb.npy", allow_pickle=False)
            depth_ref = np.load(source / "depth_m.npy", allow_pickle=False)
            valid_ref = np.load(source / "valid_mask.npy", allow_pickle=False)
            if rgb.ndim == 3 and rgb.shape[-1] == 4:
                rgb_cmp = rgb[..., :3]
            else:
                rgb_cmp = rgb
            if rgb_ref.ndim == 3 and rgb_ref.shape[-1] == 4:
                rgb_ref_cmp = rgb_ref[..., :3]
            else:
                rgb_ref_cmp = rgb_ref
            valid = np.isfinite(depth) & (depth >= float(cfg.get("min_depth_m", .1))) & (depth <= float(cfg.get("max_depth_m", 10.0)))
            rgb_diff = np.abs(rgb_cmp.astype(np.int16) - rgb_ref_cmp.astype(np.int16)) if rgb_cmp.shape == rgb_ref_cmp.shape else np.asarray([])
            rgb_max_error = int(rgb_diff.max()) if rgb_diff.size else None
            rgb_mean_error = float(rgb_diff.mean()) if rgb_diff.size else None
            rgb_changed_fraction = float(np.mean(np.any(rgb_diff != 0, axis=-1))) if rgb_diff.ndim == 3 else None
            rgb_equal = bool(rgb_cmp.shape == rgb_ref_cmp.shape and rgb_max_error == 0)
            valid_equal = bool(np.array_equal(valid, valid_ref))
            both = valid & valid_ref
            abs_err = np.abs(depth[both] - depth_ref[both]) if np.any(both) else np.asarray([])
            max_depth_error = float(abs_err.max()) if abs_err.size else None
            depth_equal = bool(valid_equal and abs_err.size > 0 and max_depth_error is not None and max_depth_error <= 0.03)
            unknown_ids = sorted(int(x) for x in np.unique(labels) if int(x) not in mapping and int(x) != 0)
            if not (rgb_equal and depth_equal) or unknown_ids:
                if rgb_cmp.shape == rgb_ref_cmp.shape:
                    np.save(out_dir / f"{rec['frame_id']}__rgb_abs_diff.npy", rgb_diff.astype(np.uint8), allow_pickle=False)
                raise RuntimeError(f"frame {i} failed scene-equivalence check: rgb_equal={rgb_equal}, rgb_max={rgb_max_error}, rgb_mean={rgb_mean_error}, rgb_changed_fraction={rgb_changed_fraction}, depth={depth_equal}, unknown instance IDs={unknown_ids[:20]}")
            counts = {gtid: int(np.count_nonzero(labels == sid)) for sid, gtid in mapping.items()}
            np.save(out_dir / f"{rec['frame_id']}__instance_ids.npy", labels, allow_pickle=False)
            rows.append({
                "frame_id": rec["frame_id"], "frame_index": i, "timestamp_s": rec["timestamp_s"],
                "mask_file": f"{rec['frame_id']}__instance_ids.npy",
                "source_rgb_sha256": sha256(source / "rgb.npy"),
                "source_depth_sha256": sha256(source / "depth_m.npy"),
                "rgb_match": rgb_equal, "valid_depth_mask_match": valid_equal,
                "rgb_max_abs_error": rgb_max_error, "rgb_mean_abs_error": rgb_mean_error,
                "rgb_changed_pixel_fraction": rgb_changed_fraction,
                "depth_max_abs_error_m": max_depth_error, "full_scene_equivalence_pass": rgb_equal and depth_equal,
                "visible_pixel_count_by_gt_id": counts,
                "rendered_instance_ids": sorted(int(x) for x in np.unique(labels)),
            })
    finally:
        sim.close()
    status = "pass" if rows and all(row["full_scene_equivalence_pass"] for row in rows) else "fail"
    report = {
        "schema_version": 1, "kind": "apt0_unique_visible_instance_mask_render",
        "status": status, "habitat_sim_version": getattr(habitat_sim, "__version__", "unknown"),
        "capture_manifest_sha256": sha256(capture / "capture_manifest.json"),
        "route_config": str(route_config.resolve()), "route_config_sha256": sha256(route_config),
        "evaluator_dataset_config_sha256": sha256(dataset_config),
        "evaluator_scene_sha256": sha256(scene_file), "geometry_pose_scale_changes": 0,
        "semantic_ids_passed_to_builder": False, "room_gt_passed_to_builder": False,
        "matching_rgb_channel_order": "RGB first 3 channels", "depth_tolerance_m": 0.03,
        "frame_count": len(rows), "frames": rows,
        "limitations": ["six articulated declarations are unsupported by the pinned non-Bullet runtime and excluded, matching the captured runtime", "visible instance masks do not constitute independent architectural room GT"],
    }
    (out_dir / "visibility_index.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--route-config", type=Path, required=True)
    ap.add_argument("--dataset-root", type=Path, required=True)
    ap.add_argument("--scene-file", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True, help="new, empty evaluation-only output directory")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--max-frames", type=int, help="small scene-equivalence smoke before all frames")
    args = ap.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError(f"output is non-empty; choose a unique path, not overwrite: {args.output}")
    args.output.mkdir(parents=True, exist_ok=True)
    custom_scene, registry = prepare_dataset(args.dataset_root, args.scene_file, args.output)
    (args.output / "object_registry_provisional.json").write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.prepare_only:
        print(json.dumps(registry, indent=2, sort_keys=True))
        return 0
    report = render(args.capture.resolve(), args.route_config.resolve(),
                    args.output / "evaluator_dataset_v1" / "apt0_gt.scene_dataset_config.json",
                    custom_scene, args.output / "instance_masks", registry, max_frames=args.max_frames)
    (args.output / "object_registry_provisional.json").write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
