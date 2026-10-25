#!/usr/bin/env python3
"""Frozen U4-OV diagnostic on one new HM3D route's saved RGB crops.

This script consumes the already saved P2 masks/RGB only. It never runs a
detector, reads semantic GT, or changes the locked vocabulary/configuration.
"""
from __future__ import annotations

import hashlib, json, sys, time, resource
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts/runs/hm3d_00800_u4ov_test_v1"
OUT = RUN / "u4ov"
U4OV = ROOT / "artifacts/audits/u4_ov"
sys.path.insert(0, str(ROOT / "scripts"))
import extract_u2_appearance as u2

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def normalize(v):
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    if not np.isfinite(v).all() or not np.isfinite(n) or n <= 0:
        raise RuntimeError("invalid feature")
    return v / np.float32(n)

def main():
    started = time.perf_counter()
    lock = json.loads((U4OV / "u4_ov_config.locked.json").read_text())
    vocab = json.loads((U4OV / "vocabulary_v1.json").read_text())
    if sha(U4OV / "vocabulary_v1.json") != lock["vocabulary_sha256"]:
        raise RuntimeError("vocabulary lock mismatch")
    cap = json.loads((RUN / "capture/capture_manifest.json").read_text())
    d2 = json.loads((RUN / "detection_2d/detection_2d_index.json").read_text())
    if d2["capture_manifest_sha256"] != sha(RUN / "capture/capture_manifest.json"):
        raise RuntimeError("P2 capture mismatch")
    d2frames = {x["frame_id"]: x for x in d2["frames"]}
    capframes = {x["frame_id"]: x for x in cap["frames"]}
    records=[]
    for fid, fr in sorted(d2frames.items(), key=lambda x: x[1]["frame_index"]):
        rgb_path=RUN/"capture"/capframes[fid]["frame_dir"]/"rgb.npy"
        rgb=np.load(rgb_path,allow_pickle=False)
        for det in fr["detections"]:
            if sha(RUN/"detection_2d"/det["mask_file"]) != det["mask_sha256"]:
                raise RuntimeError(f"mask hash mismatch {det['detection_id']}")
            row={"observation_id":det["detection_id"],"frame_id":fid,
                 "detection_id":det["detection_id"],"rgb":rgb,
                 "rgb_sha256":sha(rgb_path),"mask_path":RUN/"detection_2d"/det["mask_file"],
                 "mask_sha256":det["mask_sha256"],"bbox_xyxy":det["bbox_xyxy"],
                 "raw_detector_class":det["class_canonical"],"raw_class_name":det["class_raw"],
                 "raw_confidence":det["score_raw"],"frame_index":fr["frame_index"]}
            records.append(row)
    OUT.mkdir(parents=True,exist_ok=True)
    crop_rows=u2.prepare_crop_records(records,OUT/"crops")
    model_dir=ROOT/"artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878"
    md=json.loads((model_dir/"checkpoint_metadata.json").read_text())
    checkpoint=model_dir/md["filename"]
    if sha(checkpoint)!=lock["model"]["checkpoint_sha256"]: raise RuntimeError("checkpoint lock mismatch")
    model,preprocess,torch=u2.load_model(checkpoint)
    features={}; featmeta=[]
    for row in crop_rows:
        feature=u2.encode_crop(model,preprocess,torch,row["crop_rgb"])
        did=row["observation_id"]
        npz=OUT/"features"/(did.replace(":","__")+"__"+row["crop_sha256"][:16]+".npz")
        npz.parent.mkdir(parents=True,exist_ok=True)
        np.savez_compressed(npz,image_feature=feature)
        features[did]=feature
        featmeta.append({"observation_id":did,"crop_sha256":row["crop_sha256"],
                         "feature_path":str(npz.relative_to(ROOT)),"feature_sha256":sha(npz),
                         "shape":[512],"dtype":"float32","norm":float(np.linalg.norm(feature))})
    # Reuse the already locked text cache, verified by its manifest.
    cache_candidates=list((U4OV/"text_cache").glob("text_features__*.npz"))
    if not cache_candidates: raise RuntimeError("locked text cache absent")
    cache=cache_candidates[0]; meta_path=cache.with_suffix(".json")
    meta=json.loads(meta_path.read_text())
    if sha(cache)!=meta["cache_file_sha256"] or meta["checkpoint_sha256"]!=lock["model"]["checkpoint_sha256"]:
        raise RuntimeError("text cache verification failed")
    with np.load(cache,allow_pickle=False) as z:
        labels=z["labels"].astype(str).tolist(); text=z["label_features"].astype(np.float32)
    if len(labels)!=105 or text.shape!=(105,512): raise RuntimeError("locked text vocabulary shape mismatch")
    pred=[]
    for row in crop_rows:
        sims=text@features[row["observation_id"]]
        order=sorted(range(len(labels)),key=lambda i:(-float(sims[i]),labels[i]))
        i,j=order[:2]; top=float(sims[i]); margin=top-float(sims[j])
        if top<float(lock["classification"]["top1_unknown_if_max_cosine_below"]): status,label="unknown",None
        elif margin<float(lock["classification"]["top1_conflicted_if_top1_minus_top2_below"]): status,label="conflicted",None
        else: status,label="classified",labels[i]
        pred.append({"observation_id":row["observation_id"],"frame_index":row["frame_index"],
          "raw_detector_class":row["raw_detector_class"],"raw_class_name":row["raw_class_name"],
          "raw_confidence":row["raw_confidence"],"crop_sha256":row["crop_sha256"],
          "crop_path":str(row["crop_path"].relative_to(ROOT)),"mask_sha256":row["mask_sha256"],
          "rgb_sha256":row["rgb_sha256"],"u4ov_status":status,"fused_semantic_label":label,
          "top1_candidate":labels[i],"top1_cosine":top,"top2_candidate":labels[j],
          "top2_cosine":float(sims[j]),"cosine_margin":margin,
          "top5":[{"label":labels[k],"cosine":float(sims[k])} for k in order[:5]],
          "score_semantics":"cosine similarity, not calibrated probability"})
    # Locked sample: SHA order within classified, abstained, and association-hard.
    # Association-hard has no candidates because accepted P3 observations never
    # recur across frames and the source/candidate decisions are identical.
    seed="u4ov-hm3d00800-review-v1"
    rank=lambda x:hashlib.sha256((seed+"\0"+x["observation_id"]).encode()).hexdigest()
    selected=[]
    for status in ("classified","unknown_conflicted"):
        rows=[r for r in pred if (r["u4ov_status"]=="classified" if status=="classified" else r["u4ov_status"] in ("unknown","conflicted"))]
        rows.sort(key=rank); selected.extend(rows[:4])
    # Deduplicate by immutable observation id. No GT used in selection.
    selected=list({r["observation_id"]:r for r in selected}.values())
    report={"status":"predictions_ready_unscored","scene_id":cap["scene_id"],"frame_count":len(cap["frames"]),
      "detections":len(pred),"feature_count":len(features),"candidate_vocabulary_count":len(labels),
      "status_counts":{s:sum(r["u4ov_status"]==s for r in pred) for s in ("classified","unknown","conflicted")},
      "classified_count":sum(r["u4ov_status"]=="classified" for r in pred),
      "multi_view_available":False,"association_hard_review_candidates":0,
      "review_sample_ids":[r["observation_id"] for r in selected],
      "review_selection":"predeclared SHA-256 order: up to 4 classified and 4 abstained; association-hard stratum empty",
      "ground_truth_or_room_labels_used_for_prediction_or_review_selection":False,
      "u4ov_config_sha256":sha(U4OV/"u4_ov_config.locked.json"),
      "vocabulary_sha256":sha(U4OV/"vocabulary_v1.json"),"text_cache_sha256":sha(cache),
      "checkpoint_sha256":sha(checkpoint),"features":featmeta,
      "runtime_seconds":time.perf_counter()-started,"peak_process_rss_mib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024}
    (OUT/"observation_predictions.jsonl").write_text("".join(json.dumps(x,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n" for x in pred))
    (OUT/"review_sample.json").write_text(json.dumps({"protocol":"locked SHA-256 strata; predictions frozen before sample selection",
      "identity_question":"Do these detections depict the same physical object?",
      "class_question":"What is the object class, or is it uncertain?",
      "observations":selected},indent=2,sort_keys=True,ensure_ascii=False)+"\n")
    (OUT/"prediction_report.json").write_text(json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k!="features"},indent=2,sort_keys=True,ensure_ascii=False))

if __name__=="__main__": main()
