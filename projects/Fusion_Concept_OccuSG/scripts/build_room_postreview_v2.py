#!/usr/bin/env python3
"""Read-only post-review report for room:6 and H04/H05 membership.
No perception or mapping is run; only existing JSON/NPZ/cache artifacts are read.
"""
from __future__ import annotations
import argparse, json, hashlib, math, shutil
from pathlib import Path
import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
HYBRID=ROOT/'artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_min3_v1'
READY=ROOT/'artifacts/room_belief/readiness/hm3d00802_main29_min3_static_v1/readiness.json'
STATIC=ROOT/'artifacts/room_belief/rb2_00802_min3_static_v2/static_room_diagnostic.json'
SNAP=ROOT/'artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/replay/snapshot.json'
CACHE=ROOT/'artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace'
EVID=HYBRID/'mapping/diagnostic/evidence_manifest.json'
OUT=ROOT/'artifacts/human_review/hm3d00802_main29_min3_v1_review_v1/postreview_v2'

def sha(p):
 h=hashlib.sha256();
 with open(p,'rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
 return h.hexdigest()

def inside(x,y,poly):
 c=False; j=len(poly)-1
 for i in range(len(poly)):
  xi,yi=poly[i]; xj,yj=poly[j]
  if ((yi>y)!=(yj>y)) and x < (xj-xi)*(y-yi)/(yj-yi+1e-15)+xi: c=not c
  j=i
 return c

def softmax(d):
 a=np.array(list(d.values()),float); z=a-a.max(); e=np.exp(z); e/=e.sum(); return {k:float(v) for k,v in zip(d,e)}

def svg_membership(points, rooms, outpath, title):
 allxy=[p[:2] for p in points]
 for r in rooms: allxy += r['poly']
 xs=[p[0] for p in allxy]; ys=[p[1] for p in allxy]
 xmin,xmax=min(xs)-.4,max(xs)+.4; ymin,ymax=min(ys)-.4,max(ys)+.4
 W,H=1100,850; sx=(W-80)/(xmax-xmin); sy=(H-100)/(ymax-ymin); s=min(sx,sy)
 def tr(p): return (40+(p[0]-xmin)*s, H-50-(p[1]-ymin)*s)
 lines=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', '<rect width="100%" height="100%" fill="white"/>', f'<text x="20" y="28" font-family="sans-serif" font-size="18">{title}</text>']
 cols=['#283b70','#4e79a7','#59a14f','#f28e2b','#e15759','#b07aa1','#76b7b2']
 for idx,r in enumerate(rooms):
  pts=' '.join(f'{tr(p)[0]:.1f},{tr(p)[1]:.1f}' for p in r['poly'])
  col=cols[idx%len(cols)]
  lines.append(f'<polygon points="{pts}" fill="none" stroke="{col}" stroke-width="2" opacity=".9"/>')
  c=tr(r['centroid']); lines.append(f'<text x="{c[0]:.1f}" y="{c[1]:.1f}" font-family="sans-serif" font-size="13" fill="{col}">{r["room_id"]}</text>')
 for p in points:
  x,y=tr(p[:2]); col=p[3]; lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="{col}" opacity=".72"/>')
 lines += ['<text x="20" y="820" font-family="sans-serif" font-size="12" fill="#16855b">green=in room:6 support</text>', '<text x="260" y="820" font-family="sans-serif" font-size="12" fill="#4e79a7">blue=in another room polygon</text>', '<text x="535" y="820" font-family="sans-serif" font-size="12" fill="#d97706">orange=outside all polygons</text>', '</svg>']
 outpath.write_text('\n'.join(lines))

def depth_png(src,out):
 a=np.load(src).astype(float); valid=np.isfinite(a)&(a>0)
 if not valid.any(): Image.fromarray(np.zeros(a.shape,np.uint8)).save(out); return
 lo,hi=np.percentile(a[valid],[2,98]); x=np.clip((a-lo)/(hi-lo+1e-9),0,1); x[~valid]=0
 Image.fromarray((x*255).astype(np.uint8)).save(out)

def main():
 OUT.mkdir(parents=True,exist_ok=True)
 ready=json.loads(READY.read_text()); static=json.loads(STATIC.read_text()); snap=json.loads(SNAP.read_text())['snapshot']; evid=json.loads(EVID.read_text())
 # rooms from snapshot, normalized to room: IDs
 rooms=[]
 for n in snap['nodes']:
  if n.get('kind')!='room': continue
  a=n['attributes']; rid=n['node_id']; rooms.append({'room_id':rid,'revision':a.get('revision'),'centroid':a.get('centroid_xy'),'poly':a['outer_ring'],'name':a.get('name')})
 # score room6 against human review, no predictor changes
 human=json.loads((ROOT/'artifacts/human_review/hm3d00802_main29_min3_v1_review_v1/human_review_response_v1.json').read_text())
 room_review=human['room_review']
 scoring=[]
 for variant,v in static['variants'].items():
  r=next(x for x in v['rooms'] if x['room_id']=='room:6')
  cand=r['candidate_label']; commit=r['commit_label']
  reviewed_room_type=room_review.get('canonical_room_type', room_review.get('room_type'))
  scoring.append({'variant':variant,'candidate_label':cand,'candidate_correct':cand==reviewed_room_type,'commit_label':commit,'committed_correct':(commit==reviewed_room_type) if commit else None,'abstained':commit is None,'status':r['status'],'reason':r['reason'],'belief':r['belief'],'raw_S':r['raw_S'],'A_contributions':r['A_contributions'],'eligible_categories':r['eligible_categories'],'category_object_ids':r['category_object_ids'],'supporting_objects':r['supporting_objects']})
 # leave-one-category sensitivity, explicitly not pipeline quality
 sens=[]
 for s in scoring:
  cats=s['A_contributions']; prior={k:.125 for k in s['raw_S']}
  for drop in [None]+sorted(cats):
   keep=[c for c in cats if c!=drop]; raw={room:math.log(prior[room])+sum(cats[c][room] for c in keep) for room in s['raw_S']}; bel=softmax(raw); top=sorted(bel.items(),key=lambda x:x[1],reverse=True); top1,top2=top[0],top[1]
   sens.append({'variant':s['variant'],'left_out_category':drop,'eligible_category_count':len(keep),'candidate_label':top1[0],'candidate_belief':top1[1],'margin':top1[1]-top2[1],'commit_under_existing_policy':len(keep)>=2 and top1[1]>=.60 and top1[1]-top2[1]>=.20})
 # membership H04/H05
 objmap={o['hypothesis_id']:o for o in ready['objects']}; mem=[]; allroom={r['room_id']:r for r in rooms}
 for hid in ['hypothesis_0415c816d24c0a17','hypothesis_d579ba788876c64a']:
  o=objmap[hid]; npz=np.load(HYBRID/o['geometry_source']); xyz=np.asarray(npz['xyz'] if 'xyz' in npz else npz[list(npz.files)[0]])
  # ensure xyz shape
  if xyz.ndim!=2 or xyz.shape[1]<3: raise ValueError((hid,npz.files,xyz.shape))
  pts=[]; support={r['room_id']:0 for r in rooms}; in6=0
  for p in xyz:
   hits=[r['room_id'] for r in rooms if inside(float(p[0]),float(p[1]),r['poly'])]
   for h in hits: support[h]+=1
   c='#16855b' if 'room:6' in hits else ('#4e79a7' if hits else '#d97706')
   if 'room:6' in hits: in6+=1
   pts.append([float(p[0]),float(p[1]),float(p[2]),c])
  mr=o['membership']; entry={'hypothesis_id':hid,'human_relation':'H04 outside_room_polygon' if hid.startswith('hypothesis_0415') else 'H05 inside_room_polygon','geometry_source':o['geometry_source'],'geometry_sha256':o['geometry_sha256'],'point_count':int(len(xyz)),'voxel_representative_count':o['voxel_representative_count'],'membership':mr,'recomputed_cloud_support_counts':support,'recomputed_cloud_room6_fraction':in6/max(len(xyz),1),'coordinate_contract':ready['membership_scope'],'room_polygons':[{'room_id':r['room_id'],'revision':r['revision'],'name':r['name'],'vertex_count':len(r['poly'])} for r in rooms]}
  # source evidence assets and depth visuals
  evdir=OUT/'evidence'/('H04' if hid.startswith('hypothesis_0415') else 'H05'); evdir.mkdir(parents=True,exist_ok=True)
  obs=[]
  for rec in o['observation_records']:
   oid=rec['observation_id']; e=evid['observations'][oid]; frame=rec['frame_index']; det=oid.split(':det-')[1]; stem=oid.replace(':det-','_det-')
   srcframe=CACHE/f'capture/frames/{frame:06d}'; depth=srcframe/'depth_m.npy'
   # Copy/convert existing evidence panel paths, and depth from cached input.
   item={'observation_id':oid,'frame_index':frame,'raw_class':rec['raw_class'],'view_group_id':rec['view_group_id'],'camera_pose':rec['camera_pose'],'source_rgb':e['source_rgb'],'source_mask':e['source_mask'],'source_depth':str(depth),'evidence_panels':e['panels']}
   for kind in ['rgb','mask','crop','overlay']:
    rel=e['panels'].get(kind)
    if rel:
     sp=HYBRID/'mapping/diagnostic'/rel
     if sp.exists(): shutil.copy2(sp,evdir/f'{stem}_{kind}.png'); item[f'{kind}_copy']=str(evdir/f'{stem}_{kind}.png')
   if depth.exists():
    dp=evdir/f'{stem}_depth.png'; depth_png(depth,dp); item['depth_visual']=str(dp)
   md=srcframe/'metadata.json'; item['frame_metadata']=str(md)
   obs.append(item)
  entry['observations']=obs
  mem.append(entry)
  svg_membership(pts,rooms,evdir/'cloud_room_polygons.svg',f'{hid} cloud vs room polygons (world XY)')
 (OUT/'room6_scoring.json').write_text(json.dumps({'annotation':room_review,'variants':scoring,'sensitivity_analysis':sens,'note':'candidate/commit correctness is only against this development-room review; no general accuracy claim'},indent=2))
 (OUT/'membership_h04_h05.json').write_text(json.dumps(mem,indent=2))
 (OUT/'provenance.json').write_text(json.dumps({'run_id':ready['hybrid_run_id'],'readiness_sha256':sha(READY),'static_diagnostic_sha256':sha(STATIC),'room_snapshot':str(SNAP),'room_snapshot_sha256':sha(SNAP),'evidence_manifest_sha256':sha(EVID),'scope':'read-only post-review; no pipeline rerun'},indent=2))
 # report
 lines=['# Hậu kiểm room scoring và membership — hm3d00802_main29_min3_v1','', 'Phạm vi: đọc artifacts đã có; không chạy model, capture, detector, encoder hoặc mapping. Bảng A và thresholds giữ nguyên. `room:6` được đối chiếu với annotation người dùng `living_room`.','', '## Room:6 — bốn biến thể','', '| Variant | Candidate | Candidate đúng | Commit | Commit đúng | Abstain | Reason |','|---|---|---:|---|---:|---:|---|']
 for s in scoring: lines.append(f"| {s['variant']} | {s['candidate_label']} | {'yes' if s['candidate_correct'] else 'no'} | {s['commit_label'] or '—'} | {'yes' if s['committed_correct'] else ('—' if s['committed_correct'] is None else 'no')} | {'yes' if s['abstained'] else 'no'} | {s['reason']} |")
 lines += ['', 'Hai biến thể no-neutral đã commit `living_room` và đúng trên phòng development này. Đây không phải accuracy tổng quát. `mean_neutral` chọn candidate `kitchen` nhưng abstain; `sum_neutral` có candidate `living_room` nhưng abstain do proposal gate.', '', '## Attribution và category cap','', 'Room:6 có 3 supporting objects nhưng chỉ 2 unique categories: `chair` và `sofa`. H01 (`hypothesis_69353ed1529074db`) đóng góp `sofa` qua raw `couch→sofa`; H03 (`hypothesis_e238d343db74a41b`) và H06 (`hypothesis_bded5482ee86bd0e`) đều đóng góp `chair`. Category cap làm H03 và H06 không tạo hai phiếu `chair`; mỗi variant cộng contribution `A(chair,·)` một lần. Vì vậy label `living_room` đúng theo review room không chứng minh H01/H03/H06 đều đúng object/class. H01 bị human review ghi nhận mixed armchair; H03 là phần chân piano; H06 là chair.', '', 'Leave-one-category-out ở `room6_scoring.json` chỉ là sensitivity analysis. Khi bỏ một trong hai category, min-support hiện tại làm commit false/abstain vì còn 1 unique category; không phải pipeline mới hay quality evidence.', '', '## Membership H04/H05','', 'H04 review xác nhận `car_1` ở ngoài polygon, nhưng cloud RB1 có 20/33 voxel representatives trong `room:6` (support 0.6061) và 13/33 ngoài; greedy winner vì có positive max support. Đây là mâu thuẫn boundary/footprint giữa review và cloud, không đủ để tự quy cho geometry hay review.', '', 'H05 review xác nhận `mirror_1` trong room, nhưng retained cloud có 0/1399 voxel representatives trong mọi room polygon (`outside_fraction=1.0`), nên RB1 unassigned. Raw history là `tv` trên 6 pose groups. Artifacts hiện có chưa phân biệt được: (a) lift/depth/transform sai; (b) room polygon/world-frame mismatch; (c) review đang nói physical mirror còn cloud đại diện vùng khác. Không kết luận mirror reflection.', '', 'Cả hai dùng `conceptgraphs_world_from_capture_contract`, mét, Z-up, room polygons XY, voxel origin `[0,0,0]`, voxel size `0.025 m`, room polygon revision 31 khi có support. Snapshot không có floor/Z bounds; scope chỉ là observed single elevation `floor_0`, không phải multi-floor guarantee. `membership_h04_h05.json` và từng SVG ghi support, polygons, revisions, poses, RGB/mask/depth refs.', '', '## Annotation schema','', '- `armchair` (H01/H02) là exact label nhưng ngoài A vocabulary; không tự map `armchair→chair`.', '- `piano_leg` (H03) được giữ là visible-part label; relation tới whole-object `piano` chưa được duyệt. Đây là taxonomy relation chưa duyệt, không tự gọi predictor misclassification.', '- `car` và `mirror` ngoài A vocabulary. Raw `tv` cho H05 là predictor/semantic mismatch cần tách khỏi membership; không đổi annotation.', '- H06 `chair` là exact supported category. Room annotation `living_room` độc lập với object annotations.', '', '## Kết luận và điểm dừng','', 'Đã xác minh: no-neutral commit đúng trên room:6; category cap gộp H03/H06 thành một contribution; H04 là partial support/boundary conflict; H05 là zero-support conflict; bảng A và geometry không bị thay đổi. Chưa có room-function GT ngoài room:6 review, nên không có accuracy/F1 tổng quát.', '', 'Một thay đổi implementation ưu tiên tiếp theo: bổ sung provenance kiểm tra lift/depth→world và hiển thị nó cạnh membership cho H05 trước khi thay đổi bất kỳ rule/threshold membership nào. Không thực hiện thay đổi đó trong lượt này.']
 (OUT/'postreview_report_v2.md').write_text('\n'.join(lines)+'\n')
 html=['<!doctype html><meta charset="utf-8"><title>H04/H05 membership post-review</title><h1>H04/H05 membership diagnostic</h1><p>Read-only artifact view; predicted room labels are not changed.</p>']
 for tag in ['H04','H05']:
  html.append(f'<h2>{tag}</h2><img src="evidence/{tag}/cloud_room_polygons.svg" style="max-width:1000px">')
  entry=next(x for x in mem if (x['hypothesis_id'].startswith('hypothesis_0415') if tag=='H04' else x['hypothesis_id'].startswith('hypothesis_d579')))
  html.append('<table border="1"><tr><th>Observation</th><th>RGB</th><th>Mask</th><th>Depth</th><th>Pose</th></tr>')
  for o in entry['observations']:
   stem=o['observation_id'].replace(':det-','_det-'); base=f'evidence/{tag}/{stem}'
   html.append(f'<tr><td>{o["observation_id"]}<br>raw={o["raw_class"]}</td><td><img width="180" src="{base}_rgb.png"></td><td><img width="180" src="{base}_mask.png"></td><td><img width="180" src="{base}_depth.png"></td><td><pre>{json.dumps(o["camera_pose"],separators=(",",":"))}</pre></td></tr>')
  html.append('</table>')
 (OUT/'membership_h04_h05.html').write_text('\n'.join(html))
 files=[]
 for fp in sorted(OUT.rglob('*')):
  if fp.is_file() and fp.name!='postreview_manifest.json':
   files.append({'path':str(fp.relative_to(OUT)),'bytes':fp.stat().st_size,'sha256':sha(fp)})
 (OUT/'postreview_manifest.json').write_text(json.dumps({'schema':'room-postreview-v2','status':'complete','scope':'read-only artifacts; no pipeline rerun','run_id':ready['hybrid_run_id'],'source_hashes':{'readiness.json':sha(READY),'static_room_diagnostic.json':sha(STATIC),'snapshot.json':sha(SNAP),'evidence_manifest.json':sha(EVID)},'files':files},indent=2)+'\n')
 print(json.dumps({'out':str(OUT),'room_scoring':str(OUT/'room6_scoring.json'),'membership':str(OUT/'membership_h04_h05.json'),'report':str(OUT/'postreview_report_v2.md'),'h04_points':mem[0]['point_count'],'h05_points':mem[1]['point_count']},indent=2))
if __name__=='__main__': main()
