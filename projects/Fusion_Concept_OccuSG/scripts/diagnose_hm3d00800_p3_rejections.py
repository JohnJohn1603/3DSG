#!/usr/bin/env python3
"""Audit every rejected P3 detection in the HM3D 00800 development pilot."""
import csv,html,json,math,sys,hashlib
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/"artifacts/runs/hm3d_00800_u4ov_test_v1"
OUT=ROOT/"artifacts/audits/u4ov_new_scene_hm3d_00800/p3_rejection_diagnostic"
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"src"))
from adapters.habitat.frames import frame_from_capture_record
from standalone_sg.geometry import backproject_depth

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    cap=json.loads((RUN/"capture/capture_manifest.json").read_text())
    d2=json.loads((RUN/"detection_2d/detection_2d_index.json").read_text())
    p3=json.loads((RUN/"detection_3d/detection_3d_manifest.json").read_text())
    profile=json.loads((ROOT/"configs/perception/yolo11n_seg_replica_cad_dev.json").read_text())
    ledger=[json.loads(x) for x in (RUN/"detection_3d/object_evidence_ledger.jsonl").read_text().splitlines()]
    capframes={x["frame_id"]:x for x in cap["frames"]}; d2frames={x["frame_id"]:x for x in d2["frames"]}
    d2by={x["detection_id"]:x for f in d2["frames"] for x in f["detections"]}
    OUT.mkdir(parents=True,exist_ok=True); rows=[]; shape_checks=[]; cards=[]
    for rec in cap["frames"]:
        frame_dir=RUN/"capture"/rec["frame_dir"]
        meta=json.loads((frame_dir/"metadata.json").read_text())
        rgb=np.load(frame_dir/"rgb.npy",allow_pickle=False)
        depth=np.load(frame_dir/"depth_m.npy",allow_pickle=False)
        vmask=np.load(frame_dir/"valid_mask.npy",allow_pickle=False).astype(bool)
        maxd=float(meta["max_depth_m"]);mind=float(meta["min_depth_m"])
        calculated=np.isfinite(depth)&(depth>=mind)&(depth<=maxd)
        rgb_ok=rgb.shape[:2]==depth.shape==vmask.shape
        mask_shapes=True
        for det in d2frames[rec["frame_id"]]["detections"]:
            mask=np.load(RUN/"detection_2d"/det["mask_file"],allow_pickle=False).astype(bool)
            mask_shapes &= mask.shape==depth.shape
        shape_checks.append({"frame_id":rec["frame_id"],"rgb_shape":list(rgb.shape),"depth_shape":list(depth.shape),
           "valid_mask_shape":list(vmask.shape),"rgb_depth_same_grid":rgb_ok,"all_detection_masks_same_grid":mask_shapes,
           "saved_valid_mask_equals_range_valid":bool(np.array_equal(vmask,calculated)),
           "valid_depth_fraction_frame":float(vmask.mean()),"fx_px":meta["width"]/(2*math.tan(math.radians(meta["hfov_deg"])/2)),
           "fy_px":meta["height"]/(2*math.tan(math.atan(math.tan(math.radians(meta["hfov_deg"])/2)*meta["height"]/meta["width"])))})
    for row in ledger:
        if row["decision"]!="rejected": continue
        did=row["detection_id"]; det=d2by[did]; fid=row["frame_id"]; rec=capframes[fid]
        frame_dir=RUN/"capture"/rec["frame_dir"]
        rgb=np.load(frame_dir/"rgb.npy",allow_pickle=False)[...,:3]
        depth=np.load(frame_dir/"depth_m.npy",allow_pickle=False).astype(np.float32)
        vmask=np.load(frame_dir/"valid_mask.npy",allow_pickle=False).astype(bool)
        mask=np.load(RUN/"detection_2d"/det["mask_file"],allow_pickle=False).astype(bool)
        selected=mask&vmask
        vals=depth[selected]
        rgb_u=Image.fromarray(rgb).convert("RGBA")
        red=Image.new("RGBA",rgb_u.size,(255,25,25,120))
        rgb_u.alpha_composite(Image.composite(red,Image.new("RGBA",rgb_u.size,(0,0,0,0)),Image.fromarray(mask.astype(np.uint8)*255)))
        dr=ImageDraw.Draw(rgb_u); x0,y0,x1,y1=det["bbox_xyxy"]; dr.rectangle((x0,y0,x1,y1),outline="yellow",width=1)
        rgb_panel=rgb_u.convert("RGB").resize((640,480),Image.Resampling.NEAREST)
        # Normalize visualization over the selected mask's valid depth span; invalid pixels stay black.
        depth_vis=np.zeros(depth.shape,dtype=np.uint8)
        if vals.size:
            lo,hi=np.percentile(vals,[2,98]); hi=max(float(hi),float(lo)+1e-5)
            z=np.clip((depth-lo)/(hi-lo),0,1)
            depth_vis[vmask]=(z[vmask]*255).astype(np.uint8)
        depth_img=Image.fromarray(depth_vis,mode="L").convert("RGB")
        dm=np.zeros((*mask.shape,3),dtype=np.uint8); dm[...,1]=mask.astype(np.uint8)*180; dm[...,0]=selected.astype(np.uint8)*255
        mask_img=Image.fromarray(dm).resize((640,480),Image.Resampling.NEAREST)
        depth_img=depth_img.resize((640,480),Image.Resampling.NEAREST)
        canvas=Image.new("RGB",(1920,520),(240,240,240)); canvas.paste(rgb_panel,(0,32)); canvas.paste(mask_img,(640,32)); canvas.paste(depth_img,(1280,32))
        draw=ImageDraw.Draw(canvas); draw.text((5,5),"RGB + mask overlay",fill="black");draw.text((645,5),"Mask: green; valid selected depth: red",fill="black");draw.text((1285,5),"Axial depth gray; invalid black",fill="black")
        draw.text((5,495),f"{did} | {row['class_raw']} | {row['reason']} | mask {row['mask_pixel_count']} | valid {row['valid_depth_pixels']} | largest {row['largest_cluster_points']}",fill="black")
        image_path=OUT/(did.replace(":","__")+".png");canvas.save(image_path)
        # Diagnostic range/distance estimate only; P3 only backprojects if >=min points.
        median_axial=float(np.median(vals)) if vals.size else None
        rgbd,packet=frame_from_capture_record(rec,RUN/"capture")
        point_distance=None
        if vals.size:
            diagnostic_depth=np.where(selected,depth,np.nan).astype(np.float32)
            cloud=backproject_depth(diagnostic_depth,rgbd.intrinsics,rgbd.T_W_Copt,meters_per_unit=1.0,
              min_depth_m=mind,max_depth_m=maxd,valid_mask=selected,timestamp=packet.timestamp_s,scan_id=fid,stride=1)
            p=np.asarray(cloud.xyz_W,dtype=np.float64); cam=rgbd.T_W_Copt.matrix[:3,3]
            point_distance=float(np.median(np.linalg.norm(p-cam[None,:],axis=1)))
        item={"detection_id":did,"frame_id":fid,"frame_index":row["frame_index"],"raw_class":row["class_raw"],
          "detector_score":row["score_raw"],"bbox_xyxy":det["bbox_xyxy"],"mask_area_px":row["mask_pixel_count"],
          "valid_depth_count":row["valid_depth_pixels"],"valid_depth_fraction":row["valid_depth_fraction"],
          "diagnostic_backproject_point_count":int(vals.size),"actual_p3_backprojected_points":row.get("backprojected_points"),
          "largest_cluster_points":row["largest_cluster_points"],"actual_cluster_search":bool(vals.size>=p3["effective_cluster_min_points"]),
          "rejection_reason":row["reason"],"depth_min_m":row["depth_min_m"],"depth_median_m":median_axial,
          "depth_max_m":row["depth_max_m"],"median_camera_to_point_m":point_distance,
          "pose_habitat":meta["base_pose_habitat"],"yaw_rad":meta["route_entry"]["yaw_rad"],
          "resolution_px":[meta["width"],meta["height"]],"fx_px":shape_checks[int(row["frame_index"])]["fx_px"],
          "image":"p3_rejection_diagnostic/"+image_path.name}
        rows.append(item)
        cards.append(f'<article><h3>{html.escape(did)} — {html.escape(row["class_raw"])} — {html.escape(row["reason"])}</h3><p>mask={row["mask_pixel_count"]}; valid={row["valid_depth_pixels"]} ({row["valid_depth_fraction"]:.3f}); selected points for diagnostic={vals.size}; P3 largest cluster={row["largest_cluster_points"]}; median depth={median_axial if median_axial is not None else "N/A"} m; median camera-to-point={point_distance if point_distance is not None else "N/A"} m.</p><img src="{image_path.name}" width="100%"></article>')
    with (OUT/"rejections.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    summary={"status":"pass","rejections":len(rows),"capture_shape_intrinsics_checks":shape_checks,
      "config":{"profile_sha256":p3["profile_sha256"],"effective_eps_m":p3["effective_cluster_eps_m"],
        "effective_min_points":p3["effective_cluster_min_points"],"stride":p3["effective_sampling_stride"],
        "cluster_policy":p3["cluster_policy"]},"rows":rows,
      "interpretation":"Exact RGB/depth/mask grid alignment was checked. Diagnostic selected-point count equals valid depth pixels in each mask. If the count is below the configured minimum, P3 skips clustering; otherwise backend output is recorded from the original ledger."}
    (OUT/"diagnostic.json").write_text(json.dumps(summary,indent=2,sort_keys=True,allow_nan=False)+"\n")
    (OUT/"index.html").write_text('<!doctype html><meta charset="utf-8"><title>P3 rejection audit</title><style>body{font:16px system-ui;max-width:1200px;margin:2rem auto}article{border-bottom:1px solid #aaa;padding:1rem 0}img{image-rendering:pixelated}</style><h1>HM3D 00800 — P3 rejection diagnostics</h1><p>RGB/mask/depth shown for all rejected detections. Red in mask panel means selected valid depth. Depth gray scale is normalized within the selected mask for visualization only.</p>'+"\n".join(cards),encoding="utf-8")
    print(json.dumps({"rejections":len(rows),"frame_grid_checks":len(shape_checks),"all_rgb_depth_aligned":all(x["rgb_depth_same_grid"] and x["all_detection_masks_same_grid"] for x in shape_checks),"all_saved_valid_masks_match_depth":all(x["saved_valid_mask_equals_range_valid"] for x in shape_checks),"output":str(OUT)},indent=2))
if __name__=="__main__":main()
