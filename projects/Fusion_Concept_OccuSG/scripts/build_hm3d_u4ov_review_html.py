#!/usr/bin/env python3
"""Render the locked review sample from saved RGB and masks (no GT)."""
import json,html
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from standalone_sg.semantics.u4_class_fusion import quantize_view_bin
RUN=ROOT/"artifacts/runs/hm3d_00800_u4ov_test_v1"
OUT=RUN/"u4ov/review"
OUT.mkdir(parents=True,exist_ok=True)
cap=json.loads((RUN/"capture/capture_manifest.json").read_text())
d2=json.loads((RUN/"detection_2d/detection_2d_index.json").read_text())
samples=json.loads((RUN/"u4ov/review_sample.json").read_text())["observations"]
frames={f["frame_id"]:f for f in cap["frames"]}; dets={d["detection_id"]:d for f in d2["frames"] for d in f["detections"]}
ledger={x["detection_id"]:x for x in map(json.loads,(RUN/"detection_3d/object_evidence_ledger.jsonl").read_text().splitlines())}
p3={d["detection_id"]:d for f in map(json.loads,(RUN/"detection_3d/detection_3d.jsonl").read_text().splitlines()) for d in f["detections"]}
association={}
for mode in ("source_dedup","geometry_only","geometry_appearance"):
    association[mode]={x["detection_id"]:x for x in map(json.loads,(RUN/f"association/decisions_{mode}.jsonl").read_text().splitlines())}
cards=[]
for n,row in enumerate(samples,1):
    did=row["observation_id"]; d=dets[did]; fid=did.split(":",1)[0]
    rgb=np.load(RUN/"capture"/frames[fid]["frame_dir"]/"rgb.npy",allow_pickle=False)[...,:3]
    mask=np.load(RUN/"detection_2d"/d["mask_file"],allow_pickle=False).astype(bool)
    image=Image.fromarray(rgb).convert("RGBA")
    tint=Image.new("RGBA",image.size,(255,40,30,95))
    image.alpha_composite(Image.composite(tint,Image.new("RGBA",image.size,(0,0,0,0)),Image.fromarray(mask.astype(np.uint8)*255)))
    draw=ImageDraw.Draw(image); x0,y0,x1,y1=d["bbox_xyxy"]; draw.rectangle((x0,y0,x1,y1),outline=(255,255,0,255),width=2)
    preview=OUT/f"case_{n:02d}.png"; image.convert("RGB").resize((640,480)).save(preview)
    crop_rel="../crops/"+Path(row["crop_path"]).name
    p3row=p3.get(did); lrow=ledger.get(did)
    if p3row:
        pose=p3row.get("provider_metadata",{}).get("camera_pose_W_Copt")
        camera=(pose[0][3],pose[1][3],pose[2][3]) if pose else None
        view=quantize_view_bin(camera,p3row["center_W"],azimuth_bin_deg=30,elevation_bin_deg=30) if camera else "unavailable"
        p3text="accepted"
        assoc_text="; ".join(f"{mode}: {association[mode][did]['decision']} → {association[mode][did]['track_id']}" for mode in association)
    else:
        view="unavailable (P3 rejected; no 3D center)"
        p3text=(lrow or {}).get("reason","not accepted")
        assoc_text="not presented to association (no accepted P3 geometry)"
    label="—" if row["fused_semantic_label"] is None else row["fused_semantic_label"]
    cards.append(f'''<section class="case"><h2>Observation {n}: {html.escape(did)}</h2>
      <div class="imgs"><img src="case_{n:02d}.png" alt="Frame with detection mask"><img src="{html.escape(crop_rel)}" alt="Detection crop"></div>
      <dl><dt>Raw detector</dt><dd>{html.escape(row['raw_class_name'])} ({row['raw_confidence']:.3f})</dd>
      <dt>U4-OV status</dt><dd>{html.escape(row['u4ov_status'])}; output label {html.escape(label)}</dd>
      <dt>Top candidates</dt><dd>{html.escape(row['top1_candidate'])} {row['top1_cosine']:.3f}; {html.escape(row['top2_candidate'])} {row['top2_cosine']:.3f}; margin {row['cosine_margin']:.3f}</dd>
      <dt>P3 / association / view</dt><dd>{html.escape(p3text)}; {html.escape(assoc_text)}; view bin: {html.escape(str(view))}</dd>
      <dt>Mask / crop SHA-256</dt><dd><code>{row['mask_sha256']}</code><br><code>{row['crop_sha256']}</code></dd></dl>
      <p><b>a. Identity:</b> same physical object as any other reviewed observation? Record pairwise yes/no/uncertain using detection IDs. No association-hard candidate was produced by the locked sequence.</p>
      <p><b>b. Class:</b> what class is visible, or uncertain? Record category independently from identity. Candidate vocabulary scores are not probabilities.</p></section>''')
page='''<!doctype html><html lang="en"><meta charset="utf-8"><title>HM3D 00800 U4-OV review</title>
<style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222} .case{border:1px solid #bbb;border-radius:8px;padding:1rem;margin:1.5rem 0} .imgs{display:flex;gap:1rem;flex-wrap:wrap}.imgs img{width:min(46%,640px);height:auto;object-fit:contain;background:#eee}dt{font-weight:bold;float:left;clear:left;width:180px}dd{margin-left:190px;margin-bottom:.6rem}code{font-size:.75rem;word-break:break-all}</style>
<h1>HM3D 00800 U4-OV review</h1><p>Fresh-scene diagnostic; predictions frozen before deterministic sample selection. Sample contains 1 classified and 4 conflicted observations; association-hard stratum was empty. Review identity and category separately. No GT or room labels are shown.</p>'''+"\n".join(cards)+"</html>"
(OUT/"index.html").write_text(page,encoding="utf-8")
print(json.dumps({"review_html":str(OUT/"index.html"),"cases":len(cards)},indent=2))
