#!/usr/bin/env python3
"""Lock the short HM3D 00800 development pilot before capture."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
AUD=ROOT/"artifacts/audits/u4ov_new_scene_hm3d_00800"
RUN=ROOT/"artifacts/runs/hm3d_00800_u4ov_dev_pilot_v2"
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    route=ROOT/"configs/habitat/hm3d_minival_v02_00800_u4ov_dev_pilot_v2.json"
    validation=AUD/"pilot_v2_route_validation.json"
    val=json.loads(validation.read_text())
    if val.get("status")!="pass" or val.get("pose_count")!=5 or val.get("navigable_pose_count")!=5:
        raise SystemExit("pilot route validation failed")
    if val.get("route_config_sha256")!=sha(route): raise SystemExit("route changed after validation")
    if RUN.exists(): raise SystemExit(f"pilot output already exists: {RUN}")
    files={
      "route":route,"route_validation":validation,
      "p2_profile":ROOT/"configs/perception/yolo11n_seg_replica_cad_dev.json",
      "p2_script":ROOT/"scripts/run_replica_cad_instance_segmentation.py",
      "p3_script":ROOT/"scripts/project_detection_masks_to_3d.py",
      "u3_config":ROOT/"artifacts/audits/u3_causal_prefix/association_config.locked.json",
      "u4_config":ROOT/"artifacts/audits/u4_semantic_fusion/u4_config.locked.json",
      "u4ov_config":ROOT/"artifacts/audits/u4_ov/u4_ov_config.locked.json",
      "u4ov_vocabulary":ROOT/"artifacts/audits/u4_ov/vocabulary_v1.json",
      "u4ov_text_cache":next((ROOT/"artifacts/audits/u4_ov/text_cache").glob("text_features__*.npz")),
      "openclip_checkpoint":ROOT/"artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/open_clip_model.safetensors",
      "detection3d_profile":ROOT/"configs/perception/yolo11n_seg_replica_cad_dev.json",
    }
    records={k:{"path":str(v),"bytes":v.stat().st_size,"sha256":sha(v),"role":"input_only"} for k,v in files.items()}
    doc={"schema_version":1,"status":"development_pilot_locked_before_capture","pilot_id":"hm3d-00800-u4ov-dev-pilot-v2",
      "not_unseen_test":True,"scene_id":"00800-TEEsavR23oF","route_pose_count":5,
      "sensor_profile":{"resolution_px":[160,120],"hfov_deg":90.0,"sensor_height_m":1.5,"depth_range_m":[0.1,10.0]},
      "target_selection":{"source":"prior P3-accepted prediction in development run","detection_id":"hm3d-00800-u4ov-dense-0008:det-0002",
        "target_center_is_geometry_only":True,"no_semantic_GT_or_room_label":True},
      "route_protocol":{"target_views":["table_view_A1","table_view_B","table_view_C","table_revisit_A2"],
        "neighbor_glance":"neighbor_glance","route_validated_navigable":True,
        "pilot_pass_criterion":"At least three P3-accepted detections judged from RGB/mask sequence to show the same target across at least three distinct positions/view angles, including A2 revisit after neighbor_glance. No category or evaluator identity is used by prediction.",
        "failure_action":"If below criterion, inspect crops/poses and create another short development pilot. Keep detector, sensor and P3 thresholds fixed; no long run until pass."},
      "predictor_policy":{"same_saved_capture_p2_p3_for_all_modes":True,"modes":["source_raw","source_dedup","geometry_only","geometry_appearance"],
        "each_mode_state":"independent, empty at pilot frame 0","u4ov_vocabulary_labels":105,"gt_or_room_to_predictor":False,
        "thresholds_model_vocabulary_prompts_unchanged":True,"baseline_graph_mutation":False},
      "locked_inputs":records,"run_output_path":str(RUN)}
    path=AUD/"pilot_v2_lock.json"
    if path.exists(): raise SystemExit("pilot lock already exists")
    path.write_text(json.dumps(doc,indent=2,sort_keys=True,ensure_ascii=False)+"\n")
    digest=sha(path);(AUD/"pilot_v2_lock.sha256").write_text(f"{digest}  pilot_v2_lock.json\n")
    print(json.dumps({"status":doc["status"],"lock_sha256":digest,"route_sha256":records["route"]["sha256"],"output":str(RUN)},indent=2))
if __name__=="__main__":main()
