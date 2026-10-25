#!/usr/bin/env python3
"""Run frozen source and U3 association modes on the new route's P3 cache."""
from __future__ import annotations
import hashlib,json,math,sys,time,resource
from collections import defaultdict
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/"artifacts/runs/hm3d_00800_u4ov_test_v1"
OUT=RUN/"association"
sys.path.insert(0,str(ROOT/"src"))
from standalone_sg.config.schema import ObjectAssociationConfig
from standalone_sg.contracts.objects import Detection3D,DetectionBatch
from standalone_sg.objects.association import ObjectAssociationManager
from standalone_sg.objects.u3_experimental import ExperimentalAssociation,U3Observation,deduplicate_frame_masks
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
def main():
    OUT.mkdir(parents=True,exist_ok=True)
    cfg=json.loads((ROOT/"artifacts/audits/u3_causal_prefix/association_config.locked.json").read_text())
    p2=json.loads((RUN/"detection_2d/detection_2d_index.json").read_text())
    p3=[json.loads(x) for x in (RUN/"detection_3d/detection_3d.jsonl").read_text().splitlines() if x]
    d2_by={d["detection_id"]:d for f in p2["frames"] for d in f["detections"]}
    p3_by={d["detection_id"]:d for f in p3 for d in f["detections"]}
    feat_by={}
    for path in (RUN/"u4ov/features").glob("*.npz"):
        with np.load(path,allow_pickle=False) as z:
            feat_by[path.name.split("__det-")[0]+":det-"+path.name.split("__det-")[1].split("__")[0]]=np.asarray(z["image_feature"],dtype=np.float32)
    # Direct immutable ID->feature mapping avoids filename parsing ambiguity.
    pred_rows=[json.loads(x) for x in (RUN/"u4ov/observation_predictions.jsonl").read_text().splitlines() if x]
    feature_path={r["observation_id"]:ROOT/r["feature_path"] for r in json.loads((RUN/"u4ov/prediction_report.json").read_text())["features"]}
    frame_obs=defaultdict(list); masks={}
    for did,d in p3_by.items():
        f=next(x for x in p3 if any(y["detection_id"]==did for y in x["detections"]))
        with np.load(feature_path[did],allow_pickle=False) as z: v=np.asarray(z["image_feature"],dtype=np.float32)
        o=U3Observation(detection_id=did,frame_id=f["frame_id"],timestamp_s=float(d["timestamp_s"]),
          center=tuple(d["center_W"]),extents=tuple(d["extents_m"]),layer_id=d["layer_id"],
          class_name=d["class_name"],feature=tuple(float(x) for x in v),mask_hash=d2_by[did]["mask_sha256"])
        frame_obs[int(d["provider_metadata"]["frame_index"])].append(o)
        masks[did]=np.asarray(np.load(RUN/"detection_2d"/d2_by[did]["mask_file"],allow_pickle=False),dtype=bool)
    dedup,dups=deduplicate_frame_masks([o for xs in frame_obs.values() for o in xs],masks,
      iou_threshold=float(cfg["experimental"]["duplicate_mask_iou_min"]))
    dupids={x["detection_id"] for x in dups}
    modes={}; summaries={}
    # Source baseline: separate independent states for raw and deduplicated input.
    for name,omit in (("source_raw",set()),("source_dedup",dupids)):
      manager=ObjectAssociationManager(ObjectAssociationConfig(float(cfg["source_manager"]["same_class_radius_m"]),float(cfg["source_manager"]["same_pose_suppression_cap_m"])))
      assignments={}; decisions=[]; candidates=[]; t=time.perf_counter()
      for fi in sorted(frame_obs):
       for o in sorted(frame_obs[fi],key=lambda a:a.detection_id):
        if o.detection_id in omit: continue
        for node in manager.snapshot():
         dist=math.dist(o.center,node.center_W)
         candidates.append({"detection_id":o.detection_id,"track_id":node.object_id,"distance_m":dist,
          "same_class":ObjectAssociationManager._normalize(o.class_name)==ObjectAssociationManager._normalize(node.class_name),
          "same_class_gate_pass":ObjectAssociationManager._normalize(o.class_name)==ObjectAssociationManager._normalize(node.class_name) and dist<=cfg["source_manager"]["same_class_radius_m"],
          "same_pose_gate_pass":dist<=cfg["source_manager"]["same_pose_suppression_cap_m"]})
        batch=DetectionBatch("cached_p3","world",o.timestamp_s,(Detection3D(detection_id=o.detection_id,timestamp_s=o.timestamp_s,
         center_W=o.center,center_method=p3_by[o.detection_id]["center_method"],class_name=o.class_name,
         class_id=str(p3_by[o.detection_id].get("class_id")),confidence=p3_by[o.detection_id].get("confidence"),
         frame_id="world",layer_id=o.layer_id,extents_m=o.extents),))
        result=manager.update(batch).associations[0]
        assignments[o.detection_id]=result.object_id
        decisions.append({"detection_id":o.detection_id,"track_id":result.object_id,"decision":result.decision,"distance_m":result.distance_m})
      modes[name]=assignments; summaries[name]={"decisions":decisions,"candidates":candidates,"runtime_seconds":time.perf_counter()-t,"peak_rss_mib":rss()}
    # Candidate geometry modes use the exact frozen thresholds and independent states.
    for name,mode in (("geometry_only","geometry"),("geometry_appearance","geometry_appearance")):
      ec=cfg["experimental"]; engine=ExperimentalAssociation(mode,max_center_m=float(ec["candidate_center_radius_m"]),
       geom_min_iou=float(ec["appearance_iou_min"]),geom_only_min_iou=float(ec["geometry_only_iou_min"]),
       appearance_min_cosine=float(ec["appearance_cosine_min"]),combined_min_score=float(ec["combined_score_min"]),ambiguity_margin=.05)
      assignments={}; decisions=[]; candidates=[]; t=time.perf_counter()
      for fi in sorted(frame_obs):
       result=engine.update_frame([o for o in frame_obs[fi] if o.detection_id not in dupids])
       candidates.extend(engine.ledger);engine.ledger.clear()
       for d in result:
        decisions.append({"detection_id":d.detection_id,"track_id":d.track_id,"decision":d.decision,"score":d.selected_candidate_score,"reason":d.reason})
        if d.track_id is not None: assignments[d.detection_id]=d.track_id
      modes[name]=assignments; summaries[name]={"decisions":decisions,"candidates":candidates,"runtime_seconds":time.perf_counter()-t,"peak_rss_mib":rss()}
    for name,row in summaries.items():
      (OUT/f"decisions_{name}.jsonl").write_text("".join(json.dumps(x,sort_keys=True,allow_nan=False)+"\n" for x in row["decisions"]))
      (OUT/f"candidates_{name}.jsonl").write_text("".join(json.dumps(x,sort_keys=True,allow_nan=False)+"\n" for x in row["candidates"]))
      row["assigned_observations"]=len(modes[name]);row["unique_tracks"]=len(set(modes[name].values()))
    report={"status":"completed_insufficient_temporal_evidence","input_p3_sha256":sha(RUN/"detection_3d/detection_3d.jsonl"),
      "association_config_sha256":sha(ROOT/"artifacts/audits/u3_causal_prefix/association_config.locked.json"),
      "p3_accepted_observations":len(p3_by),"p2_detections":len(d2_by),"same_frame_duplicate_masks_suppressed":len(dups),
      "gt_metrics":{"status":"unavailable","reason":"no eligible cross-frame observation pairs; only two P3 observations, in different frames and separated beyond the locked association radius; semantic GT evaluation cannot create temporal evidence"},
      "modes":summaries,"prediction_inputs_use_gt_or_room":False,"config_changed":False}
    (OUT/"association_report.json").write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False)+"\n")
    print(json.dumps(report,indent=2,sort_keys=True,allow_nan=False))
if __name__=="__main__":main()
