#!/usr/bin/env python3
"""Refresh evaluator review metadata from verified apt_0 render evidence."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "artifacts/habitat_data/versioned_data/replica_cad_dataset"
RUN = ROOT / "artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001"
GT = ROOT / "evaluation/gt/apt_0"


def sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    registry_path = GT / "object_registry.json"
    registry = json.loads(registry_path.read_text())
    scene = json.loads((DATA / "configs/scenes/apt_0.scene_instance.json").read_text())
    lex = json.loads((DATA / "configs/ssd/replicaCAD_semantic_lexicon.json").read_text())
    categories = {int(x["id"]): x["name"] for x in lex["classes"]}
    for obj in registry["instances"]:
        inst = scene["object_instances"][int(obj["scene_instance_index"])]
        cfg = json.loads((DATA / "configs" / f"{obj['source_template']}.object_config.json").read_text())
        obj.pop("rotation_xyzw", None); obj.pop("scale", None)
        obj["rotation_quaternion_wxyz"] = inst.get("rotation")
        obj["translation_origin"] = inst.get("translation_origin", "dataset_default")
        obj["template_scale"] = cfg.get("scale", [1.0, 1.0, 1.0])
        obj["instance_uniform_scale"] = inst.get("uniform_scale", 1.0)
        obj["instance_non_uniform_scale"] = inst.get("non_uniform_scale", [1.0, 1.0, 1.0])
        obj["replicacad_semantic_class"] = categories.get(obj.get("semantic_category_id_from_source_config"))
        obj["replicacad_class_status"] = "simulator_reference_verified" if obj["replicacad_semantic_class"] else "uncertain"
        obj["geometry_status"] = "mesh_asset_and_wxyz_scene_transform_recorded; full world AABB not yet computed"
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True, allow_nan=False) + "\n")

    index = json.loads((RUN / "detection_2d/detection_2d_index.json").read_text())
    model_classes = sorted({str(d["class_canonical"]) for frame in index["frames"] for d in frame.get("detections", [])})
    cat_names = sorted({x.get("replicacad_semantic_class") for x in registry["instances"] if x.get("replicacad_semantic_class")})
    synonyms = {"bicycle": "bike", "potted_plant": "indoor_plant", "tv": "tv_screen"}
    proposals = {}
    for cls in model_classes:
        if cls in cat_names:
            proposal, status = cls, "ai_assisted_provisional_exact_name"
        elif synonyms.get(cls) in cat_names:
            proposal, status = synonyms[cls], "ai_assisted_provisional_synonym"
        elif cls == "refrigerator":
            proposal, status = None, "uncertain_articulated_or_stage_fixture_not_in_loaded_rigid_instance_registry"
        else:
            proposal, status = None, "unmapped_needs_review"
        proposals[cls] = {"candidate_replicacad_category": proposal, "status": status}
    excluded = [x.get("template_name") for x in registry.get("excluded_instances", [])]
    class_map = {
        "schema_version": 1, "status": "ai_assisted_provisional",
        "source_detector_vocabulary": "detection_2d_index.json canonical class_canonical",
        "source_replicacad_vocabulary": "official ReplicaCAD semantic lexicon referenced by object template configs",
        "detector_to_replicacad_category": proposals,
        "replicacad_instance_categories": cat_names,
        "articulated_categories_excluded": excluded,
        "review_instruction": "Confirm category equivalence from RGB/object masks. Candidate mappings are not verified GT. Built-in stage geometry and unsupported articulated declarations are not detector false positives.",
        "gt_ids_passed_to_builder": False,
    }
    class_path = GT / "class_mapping.json"
    class_path.write_text(json.dumps(class_map, indent=2, sort_keys=True, allow_nan=False) + "\n")
    manifest_path = GT / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update({"object_registry_sha256": sha(registry_path), "visibility_index_sha256": sha(GT / "visibility_index.json"),
                     "class_mapping_sha256": sha(class_path), "class_mapping_status": "ai_assisted_provisional",
                     "unique_instance_masks_status": "simulator_reference_verified_rgb_depth_equivalence_pass"})
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"registry_instances": len(registry["instances"]), "categories": len(cat_names),
                      "detector_classes": proposals, "status": manifest["status"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
