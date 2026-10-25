#!/usr/bin/env python3
"""Freeze route, inputs, predictor configs, and evaluation protocol pre-capture."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "artifacts/audits/u4ov_new_scene_hm3d_00800"
BASE = Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG")
SCENE_DIR = BASE / "artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/00800-TEEsavR23oF"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    route = ROOT / "configs/habitat/hm3d_minival_v02_00800_u4ov_15frame_route_v1.json"
    validation = AUDIT / "route_validation.json"
    validate_doc = json.loads(validation.read_text())
    if validate_doc.get("status") != "pass" or validate_doc.get("pose_count") != 15:
        raise RuntimeError("route validation must pass for exactly 15 poses before locking")
    if validate_doc.get("route_config_sha256") != sha(route):
        raise RuntimeError("route config changed after navigability validation")

    u3_config = ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json"
    u4_config = ROOT / "artifacts/audits/u4_semantic_fusion/u4_config.locked.json"
    ov_config = ROOT / "artifacts/audits/u4_ov/u4_ov_config.locked.json"
    vocab = ROOT / "artifacts/audits/u4_ov/vocabulary_v1.json"
    text_cache = next((ROOT / "artifacts/audits/u4_ov/text_cache").glob("text_features__*.npz"))
    checkpoint = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/open_clip_model.safetensors"
    model_meta = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/checkpoint_metadata.json"
    assets = {
        "hm3d_basis_scene": SCENE_DIR / "TEEsavR23oF.basis.glb",
        "hm3d_semantic_scene_evaluator_only": SCENE_DIR / "TEEsavR23oF.semantic.glb",
        "hm3d_semantic_descriptor_evaluator_only": SCENE_DIR / "TEEsavR23oF.semantic.txt",
        "hm3d_navmesh": SCENE_DIR / "TEEsavR23oF.basis.navmesh",
        "hm3d_dataset_config": BASE / "artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/hm3d_annotated_minival_basis.scene_dataset_config.json",
        "route_config": route,
        "route_validation": validation,
        "u3_association_lock": u3_config,
        "u4_semantic_fusion_lock": u4_config,
        "u4_ov_lock": ov_config,
        "u4_ov_vocabulary": vocab,
        "u4_ov_text_cache": text_cache,
        "u2_openclip_checkpoint": checkpoint,
        "u2_checkpoint_metadata": model_meta,
        "detector_profile": ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json",
        "pipeline_profile": ROOT / "configs/reference_code_mp3d.yaml",
        "p2_script": ROOT / "scripts/run_replica_cad_instance_segmentation.py",
        "p3_script": ROOT / "scripts/project_detection_masks_to_3d.py",
        "source_association": ROOT / "src/standalone_sg/objects/association.py",
        "u3_candidate_association": ROOT / "src/standalone_sg/objects/u3_experimental.py",
    }
    file_records = {}
    for name, path in assets.items():
        if not path.is_file():
            raise FileNotFoundError(f"locked input missing: {name}: {path}")
        file_records[name] = {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path),
                              "role": "evaluator_only" if "evaluator_only" in name else "input_only"}

    output = ROOT / "artifacts/runs/hm3d_00800_u4ov_test_v1"
    if output.exists():
        raise FileExistsError(f"test output path already exists: {output}")
    lock = {
        "schema_version": 1,
        "status": "locked_before_capture",
        "test_id": "hm3d_00800_u4ov_route_v1",
        "unseen_scene_holdout": True,
        "scene_id": "00800-TEEsavR23oF",
        "scene_selection_reason": "first/lowest-ID locally available HM3D minival scene other than 00802 with basis mesh, semantic mesh, semantic descriptor, and navmesh; smallest complete basis mesh among inspected eligible scenes; selected without model scores or category labels",
        "route_pose_count": 15,
        "route_station_count": 5,
        "headings_per_station": 3,
        "sensor_profile": {"resolution_px": [160, 120], "horizontal_fov_deg": 90.0,
            "sensor_position_habitat": [0.0, 1.5, 0.0], "depth_range_m": [0.1, 10.0],
            "profile_id": "habitat-rgbd-stage-v1"},
        "run_output_path": str(output),
        "locked_inputs": file_records,
        "predictor_policy": {
            "modes": ["source_raw", "source_dedup", "geometry_only", "geometry_appearance"],
            "state_initialization": "each mode starts with empty, independent state at route frame 0",
            "shared_input": "all modes consume exactly the same saved P2 detections/masks and P3 geometry; no rerun per mode",
            "no_ground_truth_input": True,
            "no_room_label_input": True,
            "u4_ov_candidates": "all 105 locked vocabulary labels; raw YOLO class is not a gate",
            "future_observations_used": False,
            "model_vocabulary_prompts_thresholds_changed": False,
        },
        "evaluation_protocol": {
            "association_pairwise": "Cross-frame detection pairs; both endpoints need a matched evaluator mask and non-null semantic instance ID. Same-frame pairs excluded. Missing GT excluded and counted, never assumed correct. Deferred/unassigned pairs to a same GT instance count FN.",
            "association_fragmentation": "A GT semantic instance assigned to more than one predicted track; report both affected instances and excess track count.",
            "association_false_merge": "A predicted track containing more than one GT semantic instance among matched observations; report track IDs and instance counts.",
            "causal_semantics": "At frame t, use only current/past image features and assignments; report first frame classified, labels/status timeline, correctness, coverage, abstentions and harmful raw-to-predicted changes.",
            "final_map_semantics": "One record per predicted track; report identity-pure tracks and mixed-identity tracks separately; mixed identity remains visible as association failure and receives no majority class truth.",
            "mapping_policy": "No provisional mapping is promoted automatically. Class correctness on evaluator GT remains unavailable until user confirms evaluator mappings. Human review is reported separately.",
            "review_sampling": {
                "seed": "u4ov-hm3d00800-review-v1",
                "selection": "After predictions are frozen, select up to 12 observations using deterministic SHA-256 ordering within predeclared strata: up to 4 classified outputs, 4 unknown/conflicted outputs (balanced across statuses where available), and 4 association-hard observations (source/candidate disagreement or explicit defer). Deduplicate identical observations across strata; fill short strata from next SHA-ordered row in that stratum.",
                "ground_truth_or_room_labels_used_for_selection": False,
                "questions": ["Do these detections depict the same physical object?", "What is the object class, or is it uncertain?"],
                "identity_and_category_annotations_separate": True,
            },
        },
        "expected_cost_before_run": {"frames": 15, "capture_storage_estimate_mb": 3,
            "end_to_end_runtime_estimate_minutes": [2, 5], "total_artifact_storage_budget_mb": 250,
            "new_downloads": False},
        "taxonomy_and_category_mappings": "Proposals remain pending user review; none is predictor input.",
    }
    lock_path = AUDIT / "test_lock.json"
    encoded = json.dumps(lock, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    if lock_path.exists():
        if lock_path.read_text() != encoded:
            raise RuntimeError("immutable test lock already exists with different content")
    else:
        lock_path.write_text(encoded)
    lock_sha = sha(lock_path)
    digest_path = AUDIT / "test_lock.sha256"
    digest_value = f"{lock_sha}  test_lock.json\n"
    if digest_path.exists() and digest_path.read_text() != digest_value:
        raise RuntimeError("test lock digest already exists with different value")
    if not digest_path.exists():
        digest_path.write_text(digest_value)
    print(json.dumps({"status": lock["status"], "test_id": lock["test_id"],
        "route_pose_count": lock["route_pose_count"], "route_sha256": file_records["route_config"]["sha256"],
        "test_lock_sha256": lock_sha, "output_path": str(output)}, indent=2))


if __name__ == "__main__":
    main()
