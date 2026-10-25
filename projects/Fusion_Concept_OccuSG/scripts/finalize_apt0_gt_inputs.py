#!/usr/bin/env python3
"""Lock apt_0 evaluation inputs and install validated visible-instance masks."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--route-config", type=Path, required=True)
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--dataset-root", type=Path, required=True)
    ap.add_argument("--instance-render-run", type=Path, required=True)
    ap.add_argument("--evaluation-run-id", required=True)
    args = ap.parse_args()
    capture, run_root, dataset = args.capture.resolve(), args.run_root.resolve(), args.dataset_root.resolve()
    render_root = args.instance_render_run.resolve()
    gt_root = ROOT / "evaluation" / "gt" / "apt_0"
    out_run = ROOT / "evaluation" / "runs" / args.evaluation_run_id
    if out_run.exists():
        raise FileExistsError(f"evaluation run exists; choose a fresh ID, not overwrite: {out_run}")
    if not (render_root / "instance_masks" / "visibility_index.json").is_file():
        raise FileNotFoundError("validated instance render visibility_index.json missing")
    render_report = json.loads((render_root / "instance_masks" / "visibility_index.json").read_text())
    registry_report = json.loads((render_root / "object_registry_provisional.json").read_text())
    if render_report.get("status") != "pass" or render_report.get("frame_count") != 24:
        raise RuntimeError("refusing to install instance masks before all 24 frame equivalence checks pass")
    if (gt_root / "visible_instance_masks").exists():
        raise FileExistsError("canonical visible_instance_masks exists; refusing to overwrite existing GT evidence")
    dest_masks = gt_root / "visible_instance_masks"
    dest_masks.mkdir(parents=True)
    for row in render_report["frames"]:
        src = render_root / "instance_masks" / row["mask_file"]
        if not src.is_file():
            raise FileNotFoundError(src)
        shutil.copy2(src, dest_masks / src.name)

    scene = json.loads((dataset / "configs/scenes/apt_0.scene_instance.json").read_text())
    lex = json.loads((dataset / "configs/ssd/replicaCAD_semantic_lexicon.json").read_text())
    category_by_id = {int(c["id"]): c["name"] for c in lex.get("classes", [])}
    objs = registry_report["objects"]
    total_visible = 0
    for obj in objs:
        source_instance = scene["object_instances"][int(obj["scene_instance_index"])]
        template_cfg = json.loads((dataset / "configs" / f"{obj['source_template']}.object_config.json").read_text())
        obj.pop("rotation_xyzw", None)
        obj["rotation_quaternion_wxyz"] = source_instance.get("rotation")
        obj["translation_origin"] = source_instance.get("translation_origin", "dataset_default")
        obj["template_scale"] = template_cfg.get("scale", [1.0, 1.0, 1.0])
        obj["instance_uniform_scale"] = source_instance.get("uniform_scale", 1.0)
        obj["instance_non_uniform_scale"] = source_instance.get("non_uniform_scale", [1.0, 1.0, 1.0])
        obj.pop("scale", None)
        gtid = obj["gt_instance_id"]
        counts = {row["frame_id"]: int(row["visible_pixel_count_by_gt_id"][gtid]) for row in render_report["frames"]}
        visible = [f for f, n in counts.items() if n > 0]
        eligible = [f for f, n in counts.items() if n >= 64]
        total_visible += bool(visible)
        obj["visible_pixel_count_by_frame"] = counts
        obj["visible_frame_ids"] = visible
        obj["eligible_2d_frame_ids_at_64px"] = eligible
        obj["loaded_status"] = "simulator_reference_verified" if visible else "uncertain_not_visible_in_locked_capture"
        obj["instance_identity_status"] = "simulator_reference_verified_unique_instance_semantic_id"
        category_id = obj.get("semantic_category_id_from_source_config")
        obj["replicacad_semantic_class"] = category_by_id.get(category_id)
        obj["replicacad_class_status"] = "simulator_reference_verified" if category_id in category_by_id else "uncertain"
        obj["geometry_status"] = "source_asset_and_scene_transform_locked; world_bounds_not_yet_computed"
        obj["semantic_ids_are_evaluator_only"] = True
    excluded = []
    for index, inst in enumerate(scene.get("articulated_object_instances", [])):
        excluded.append({
            "gt_instance_id": f"apt0-ao-excluded-{index:03d}",
            "template_name": inst.get("template_name"),
            "translation_habitat_m": inst.get("translation"),
            "rotation_quaternion_raw": inst.get("rotation"),
            "uniform_scale": inst.get("uniform_scale", 1.0),
            "translation_origin": inst.get("translation_origin"),
            "loaded_status": "unsupported_not_loaded_in_capture_runtime",
            "excluded_from_detector_false_negatives": True,
            "reason": "Habitat-Sim 0.3.3 runtime without Bullet logs articulated object creation unsupported; the capture and equivalence render use the same non-Bullet runtime.",
        })
    object_gt = {
        "schema_version": 1, "scene_id": "apt_0", "status": "simulator_reference_verified_ids_and_classes_geometry_bounds_pending",
        "coordinate_frame": "habitat_world_y_up_xyz_m", "source_scene_sha256": sha(dataset / "configs/scenes/apt_0.scene_instance.json"),
        "source_dataset_sha256": sha(dataset / "replicaCAD.scene_dataset_config.json"),
        "unique_instance_id_source": "evaluator-only semantic_id override per scene object; full-frame RGB and depth equivalence passed",
        "instances": objs, "excluded_instances": excluded,
        "counts": {"rigid_scene_instances": len(objs), "visible_any_pixel": total_visible,
                   "articulated_declared_but_unsupported": len(excluded)},
        "note": "The instance IDs never enter M1-M10. Canonical class mapping for perception must be reviewed separately. Full geometry AABBs are not yet scored.",
    }
    (gt_root / "object_registry.json").write_text(json.dumps(object_gt, indent=2, sort_keys=True, allow_nan=False) + "\n")
    vis_installed = dict(render_report)
    vis_installed["status"] = "simulator_reference_verified"
    vis_installed["mask_root"] = "visible_instance_masks/"
    vis_installed["mask_files_copied_to_gt"] = True
    (gt_root / "visibility_index.json").write_text(json.dumps(vis_installed, indent=2, sort_keys=True, allow_nan=False) + "\n")
    manifest = {
        "schema_version": 1, "scene_id": "apt_0", "status": "partial_gt_inputs_locked_room_annotation_pending",
        "capture_id": run_root.name, "capture_frame_count": 24,
        "capture_manifest_sha256": sha(capture / "capture_manifest.json"),
        "route_config": str(args.route_config.resolve()), "route_config_sha256": sha(args.route_config.resolve()),
        "dataset_config_sha256": sha(dataset / "replicaCAD.scene_dataset_config.json"),
        "scene_instance_sha256": sha(dataset / "configs/scenes/apt_0.scene_instance.json"),
        "stage_mesh_sha256": sha(dataset / "stages/frl_apartment_stage.glb"),
        "instance_render_report_sha256": sha(render_root / "instance_masks" / "visibility_index.json"),
        "object_registry_sha256": sha(gt_root / "object_registry.json"),
        "visible_instance_mask_count": len(render_report["frames"]),
        "room_polygons_status": "not_annotated; no room metrics may be computed",
        "room_adjacency_status": "not_annotated",
        "ai_assisted_annotation_status": "provisional only",
        "human_review_complete": False,
    }
    (gt_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    checks = {}
    for rec in json.loads((capture / "capture_manifest.json").read_text())["frames"]:
        frame_dir = capture / rec["frame_dir"]
        meta = json.loads((frame_dir / "metadata.json").read_text())
        if meta.get("frame_id") != rec["frame_id"] or abs(float(meta.get("timestamp_s")) - float(rec["timestamp_s"])) > 1e-9:
            raise RuntimeError(f"capture manifest/frame metadata mismatch at {rec['frame_id']}")
        for name in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"):
            checks[str((frame_dir / name).relative_to(capture))] = sha(frame_dir / name)
    det2d_index = run_root / "detection_2d/detection_2d_index.json"
    det2d = json.loads(det2d_index.read_text())
    detected_classes = sorted({str(d["class_canonical"]) for f in det2d["frames"] for d in f.get("detections", [])})
    category_names = sorted(set(category_by_id.values()))
    synonym_proposals = {"bicycle": "bike", "potted_plant": "indoor_plant", "tv": "tv_screen"}
    model_to_category = {}
    for model_class in detected_classes:
        if model_class in category_names:
            candidate, status = model_class, "ai_assisted_provisional_exact_name"
        elif model_class in synonym_proposals and synonym_proposals[model_class] in category_names:
            candidate, status = synonym_proposals[model_class], "ai_assisted_provisional_synonym"
        elif model_class == "refrigerator":
            candidate, status = None, "uncertain_articulated_or_stage_fixture_not_in_loaded_rigid_instance_registry"
        else:
            candidate, status = None, "unmapped_needs_review"
        model_to_category[model_class] = {"candidate_replicacad_category": candidate, "status": status}
    class_map = {
        "schema_version": 1, "status": "ai_assisted_provisional",
        "source_detector_vocabulary": "detection_2d_index.json canonical class_canonical",
        "source_replicacad_vocabulary": "official ReplicaCAD semantic lexicon referenced by object template configs",
        "detector_to_replicacad_category": model_to_category,
        "replicacad_instance_categories": category_names,
        "articulated_categories_excluded": [x.get("template_name") for x in excluded],
        "review_instruction": "Confirm category equivalence from RGB/object instance references. A candidate is not a confirmed mapping; built-in stage geometry and unsupported articulated declarations are not detector false positives.",
        "gt_ids_passed_to_builder": False,
    }
    (gt_root / "class_mapping.json").write_text(json.dumps(class_map, indent=2, sort_keys=True, allow_nan=False) + "\n")
    mask_hashes = {}
    for frame in det2d["frames"]:
        for detection in frame.get("detections", []):
            p = run_root / "detection_2d" / detection["mask_file"]
            if sha(p) != detection["mask_sha256"]:
                raise RuntimeError(f"detection mask hash mismatch: {p}")
            mask_hashes[str(p.relative_to(run_root))] = detection["mask_sha256"]
    read_only_hashes = {}
    candidates = [
        args.route_config.resolve(), ROOT / "configs/reference_code_mp3d.yaml",
        ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json",
        ROOT / "scripts/run_habitat_native_replay.py", ROOT / "scripts/capture_habitat_replica.py",
        ROOT / "src/standalone_sg/geometry/backprojection.py",
        ROOT / "build/native-m4-dude/occusg_dude_worker",
        ROOT / "build/native-m3/occupancy/standalone_sg_octomap_fixture_runner",
        dataset / "replicaCAD.scene_dataset_config.json", dataset / "configs/scenes/apt_0.scene_instance.json",
        dataset / "stages/frl_apartment_stage.glb", dataset / "navmeshes/apt_0.navmesh",
        run_root / "capture/capture_manifest.json", det2d_index,
        run_root / "detection_3d/detection_3d.jsonl", run_root / "detection_3d/object_evidence_ledger.jsonl",
        run_root / "replay/snapshot.json", run_root / "replay/delta_log.json", run_root / "replay/frame_diagnostics.json",
        run_root / "replay/manifest.json",
    ]
    for p in candidates:
        if p.is_file():
            read_only_hashes[str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)] = sha(p)
    try:
        dataset_commit = subprocess.check_output(["git", "-C", str(dataset), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        dataset_commit = None
    out_run.mkdir(parents=True)
    lock = {
        "schema_version": 1, "kind": "ground_truth_evaluation_input_lock", "evaluation_run_id": args.evaluation_run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(), "project_git_root": None,
        "project_source_revision": "unavailable: workspace root has no Git metadata",
        "replicacad_dataset_git_revision": dataset_commit,
        "capture": str(capture), "capture_manifest_sha256": sha(capture / "capture_manifest.json"),
        "capture_raw_file_sha256": checks, "detection_2d_mask_sha256": mask_hashes,
        "read_only_input_sha256": read_only_hashes,
        "detector_cache_authoritative": bool(det2d.get("cache_authoritative")),
        "detector_detection_count": int(det2d.get("detection_count", 0)),
        "detector_profile_weights_sha256": det2d.get("frames", [{}])[0].get("detections", [{}])[0].get("model_fingerprint", {}).get("weights_sha256"),
        "instance_mask_equivalence": {"status": render_report["status"], "frame_count": render_report["frame_count"],
                                       "rgb_exact_all_frames": all(x["rgb_match"] for x in render_report["frames"]),
                                       "depth_valid_mask_equal_all_frames": all(x["valid_depth_mask_match"] for x in render_report["frames"]),
                                       "max_depth_abs_error_m": max(x["depth_max_abs_error_m"] for x in render_report["frames"] if x["depth_max_abs_error_m"] is not None)},
        "runtime_inventory": {"rigid_scene_instances": len(objs), "unique_instances_visible": total_visible,
                              "articulated_declared": len(excluded), "articulated_loaded": 0,
                              "articulated_exclusion_reason": object_gt["excluded_instances"][0]["reason"] if excluded else None},
        "baseline_replay": {"snapshot_sha256": sha(run_root / "replay/snapshot.json"),
                            "delta_log_sha256": sha(run_root / "replay/delta_log.json"),
                            "snapshot_reload_equal": json.loads((run_root / "replay/manifest.json").read_text()).get("snapshot_reload_equal"),
                            "delta_replay_equal": json.loads((run_root / "replay/manifest.json").read_text()).get("delta_replay_equal")},
        "room_annotation": "pending independent human review; metrics intentionally null",
        "note": "Checksums identify immutable inputs; no input capture or baseline replay was modified.",
    }
    (out_run / "manifest.json").write_text(json.dumps(lock, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"evaluation_run": str(out_run), "status": manifest["status"],
                      "capture_frames": 24, "rgb_depth_masks_equivalent": lock["instance_mask_equivalence"],
                      "rigid_instances": len(objs), "visible_any_pixel": total_visible,
                      "articulated_excluded": len(excluded), "raw_capture_files_hashed": len(checks),
                      "detection_masks_hashed": len(mask_hashes), "project_git": dataset_commit}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
