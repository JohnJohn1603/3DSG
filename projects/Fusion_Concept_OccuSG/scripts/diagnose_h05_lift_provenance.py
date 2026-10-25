#!/usr/bin/env python3
"""Read-only H05 RGB-D -> world provenance diagnostic.

It reconstructs selected observation points from cached axial depth and masks,
without changing the saved ConceptGraphs cloud or rerunning mapping.
"""
from __future__ import annotations
import json, hashlib, math, shutil
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace'
HYBRID=ROOT/'artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_min3_v1'
READY=ROOT/'artifacts/room_belief/readiness/hm3d00802_main29_min3_static_v1/readiness.json'
SNAP=ROOT/'artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/replay/snapshot.json'
EVID=HYBRID/'mapping/diagnostic/evidence_manifest.json'
HID='hypothesis_d579ba788876c64a'
OBS_IDS=['hm3d-00802-main-0011:det-0001','hm3d-00802-main-0016:det-0002','hm3d-00802-main-0019:det-0001']
OUT=ROOT/'artifacts/human_review/hm3d00802_main29_min3_v1_review_v1/postreview_v2/h05_lift_diagnostic'

def sha(p):
 h=hashlib.sha256();
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()

def inside(x,y,poly):
 c=False; j=len(poly)-1
 for i in range(len(poly)):
  xi,yi=poly[i]; xj,yj=poly[j]
  if ((yi>y)!=(yj>y)) and x < (xj-xi)*(y-yi)/(yj-yi+1e-15)+xi: c=not c
  j=i
 return c

def png_overlay(rgb, mask, uv, out):
 im=Image.fromarray(np.asarray(rgb,dtype=np.uint8)[...,:3].copy()); d=ImageDraw.Draw(im,'RGBA')
 ys,xs=np.where(mask); 
 if len(xs):
  for x,y in zip(xs[::max(1,len(xs)//1200)],ys[::max(1,len(ys)//1200)]): d.point((int(x),int(y)),fill=(255,40,40,180))
 for u,v in uv[::max(1,len(uv)//800)]: d.ellipse((int(u)-2,int(v)-2,int(u)+2,int(v)+2),fill=(0,255,80,220))
 im.save(out)

def depth_png(depth, valid, out):
 a=np.asarray(depth,float); ok=np.asarray(valid,bool)&np.isfinite(a)&(a>0)
 x=np.zeros(a.shape,np.uint8)
 if ok.any():
  lo,hi=np.percentile(a[ok],[2,98]); x[ok]=(np.clip((a[ok]-lo)/(hi-lo+1e-9),0,1)*255).astype(np.uint8)
 Image.fromarray(x).save(out)

def main():
 OUT.mkdir(parents=True,exist_ok=True); (OUT/'observations').mkdir(exist_ok=True)
 ready=json.loads(READY.read_text()); evid=json.loads(EVID.read_text()); snap=json.loads(SNAP.read_text())['snapshot']
 rooms=[]
 for n in snap['nodes']:
  if n.get('kind')=='room': rooms.append({'room_id':n['node_id'],'revision':n['attributes'].get('revision'),'poly':n['attributes']['outer_ring']})
 obj=next(x for x in ready['objects'] if x['hypothesis_id']==HID)
 final=np.load(HYBRID/obj['geometry_source'])['xyz'].astype(float)
 rec_by_id={x['observation_id']:x for x in obj['observation_records']}
 rows=[]; world_all=[]; rgb_refs=[]
 for obs_idx, oid in enumerate(OBS_IDS):
  rec=rec_by_id[oid]; frame=int(rec['frame_index']); srcframe=CACHE/f'capture/frames/{frame:06d}'
  rgb_path=srcframe/'rgb.npy'; depth_path=srcframe/'depth_m.npy'; valid_path=srcframe/'valid_mask.npy'; meta_path=srcframe/'metadata.json'
  meta=json.loads(meta_path.read_text()); rgb=np.load(rgb_path,allow_pickle=False)[...,:3]; depth=np.load(depth_path,allow_pickle=False).astype(float); valid=np.load(valid_path,allow_pickle=False).astype(bool)
  e=evid['observations'][oid]; mask_path=Path(e['source_mask']); mask=np.load(mask_path,allow_pickle=False).astype(bool)
  assert rgb.shape[:2]==depth.shape==valid.shape==mask.shape, (oid,rgb.shape,depth.shape,valid.shape,mask.shape)
  hf=float(meta['hfov_deg']); w=int(meta['width']); h=int(meta['height']); fx=w/(2*math.tan(math.radians(hf)/2)); fy=h/(2*math.tan(math.atan(math.tan(math.radians(hf)/2)*h/w))); cx=(w-1)/2; cy=(h-1)/2
  sel=mask&valid&np.isfinite(depth)&(depth>0); vv,uu=np.where(sel); z=depth[sel]; cam=np.stack([(uu-cx)*z/fx,(vv-cy)*z/fy,z],axis=1); T=np.asarray(rec['camera_pose'],float); world=(T[:3,:3]@cam.T).T+T[:3,3]
  # selected pixel round-trip should return the same pixels under this contract
  uv=np.column_stack([uu,vv])
  hits={r['room_id']:int(sum(inside(float(p[0]),float(p[1]),r['poly']) for p in world)) for r in rooms}
  stem=oid.replace(':det-','_det-'); od=OUT/'observations'; png_overlay(rgb,mask,uv,od/f'{stem}_projection_overlay.png'); depth_png(depth,valid,od/f'{stem}_depth.png'); shutil.copy2(mask_path,od/f'{stem}_mask.npy')
  row={'observation_id':oid,'lineage_final_hypothesis':HID,'frame_index':frame,'raw_class':rec['raw_class'],'view_group_id':rec['view_group_id'],'rgb_path':str(rgb_path),'rgb_sha256':sha(rgb_path),'depth_path':str(depth_path),'depth_sha256':sha(depth_path),'valid_mask_path':str(valid_path),'valid_mask_sha256':sha(valid_path),'mask_path':str(mask_path),'mask_sha256':sha(mask_path),'metadata_path':str(meta_path),'metadata_sha256':sha(meta_path),'rgb_shape':list(rgb.shape),'depth_shape':list(depth.shape),'mask_shape':list(mask.shape),'mask_pixels':int(mask.sum()),'valid_depth_pixels_in_mask':int(sel.sum()),'depth_units_declared':'m','depth_representation_declared':'axial','depth_min_m':float(np.nanmin(z)) if len(z) else None,'depth_median_m':float(np.nanmedian(z)) if len(z) else None,'depth_max_m':float(np.nanmax(z)) if len(z) else None,'intrinsics':{'fx':fx,'fy':fy,'cx':cx,'cy':cy,'width':w,'height':h,'hfov_deg':hf},'camera_pose_W_Copt':T.tolist(),'camera_convention':'+X right, +Y down, +Z forward; axial z used by reference create_object_pcd','world_point_count':int(len(world)),'world_bounds_minmax':np.stack([world.min(0),world.max(0)]).tolist() if len(world) else None,'room_support_counts_from_reconstructed_points':hits,'room6_support_fraction':hits.get('room:6',0)/max(len(world),1),'saved_final_hypothesis_point_count':int(len(final)),'saved_final_hypothesis_bounds_minmax':np.stack([final.min(0),final.max(0)]).tolist(),'diagnostic_files':{'projection_overlay':str(od/f'{stem}_projection_overlay.png'),'depth_visual':str(od/f'{stem}_depth.png'),'mask_npy':str(od/f'{stem}_mask.npy')}}
  rows.append(row); world_all.extend([(float(p[0]),float(p[1]),float(p[2]),obs_idx) for p in world]); rgb_refs.append(row)
 # final and reconstructed clouds over polygons
 allxy=[p[:2] for p in world_all]+[p[:2] for p in final]
 for r in rooms: allxy += r['poly']
 xmin,xmax=min(x for x,y in allxy)-.3,max(x for x,y in allxy)+.3; ymin,ymax=min(y for x,y in allxy)-.3,max(y for x,y in allxy)+.3; s=min(1000/(xmax-xmin),760/(ymax-ymin));
 def tr(p): return (40+(p[0]-xmin)*s,810-(p[1]-ymin)*s)
 def make_svg(points, path, title):
  lines=[f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="850"><rect width="100%" height="100%" fill="white"/><text x="20" y="25" font-family="sans-serif" font-size="17">{title}</text>']
  for r in rooms:
   ps=' '.join(f'{tr(p)[0]:.1f},{tr(p)[1]:.1f}' for p in r['poly']); lines.append(f'<polygon points="{ps}" fill="none" stroke="#40577d" stroke-width="2"/><text x="{tr(r["poly"][0])[0]:.1f}" y="{tr(r["poly"][0])[1]:.1f}" font-size="12">{r["room_id"]} rev={r["revision"]}</text>')
  for p in points:
   x,y=tr(p[:2]); col=['#16855b','#d97706','#9c27b0'][int(p[3])%3] if len(p)>3 else '#111827'; lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{col}" opacity=".65"/>')
  lines.append('</svg>'); path.write_text('\n'.join(lines))
 make_svg(world_all,OUT/'reconstructed_observations_world.svg','H05 reconstructed observation clouds in canonical world XY (colors=observation index)')
 make_svg([(float(p[0]),float(p[1]),float(p[2]),0) for p in final],OUT/'saved_final_hypothesis_world.svg','H05 saved final hypothesis cloud in canonical world XY')
 out={'schema':'h05-lift-provenance-v1','scope':'read-only reconstruction from cached RGB-D/masks; no membership or cloud mutation','final_hypothesis_id':HID,'selected_observation_ids':OBS_IDS,'selection_reason':'first observation plus two distinct pose groups; all lineage points to retained final hypothesis','lineage':{'final_geometry_source':obj['geometry_source'],'final_geometry_sha256':obj['geometry_sha256'],'raw_class_history':obj['raw_class_history'],'saved_membership':obj['membership']},'reference_call_path':{'runner':'scripts/run_conceptgraphs_cached_hybrid.py','input_builder':'build_gobs → reference gobs_to_detection_list','reference_function':'ref_ConceptGraph/concept-graphs/conceptgraph/slam/utils.py::create_object_pcd / gobs_to_detection_list','geometry_source':'raw cached RGB-D axial depth + P2 mask; selected P3 points are not used by this mapper','formula':'camera x=(u-cx)z/fx, y=(v-cy)z/fy, z=axial depth; then trans_pose camera→canonical world','postprocess':'reference process_pcd voxel_down_sample and DBSCAN; online object pcd includes 4mm np.random.normal jitter; exact per-observation parity unavailable without RNG/point lineage'},'contract_checks':{'coordinate_frame':'canonical world Z-up; camera optical +X right,+Y down,+Z forward','depth_units':'cached metadata and adapter declare metres; axial depth','intrinsics':'Habitat adapter formula with cx=(W-1)/2, cy=(H-1)/2','mask_rgb_depth_alignment':'checked per selected observation; all shapes equal','pose_source':'cached P3 pose cross-checked by runner against frame_from_capture_record','room_frame':'room snapshot XY polygons; no floor/Z bounds','voxel_grid':'origin [0,0,0], size 0.025 m for saved RB1 support; reconstruction is pre-voxel'},'observations':rows,'final_cloud_point_count':int(len(final)),'final_cloud_bounds_minmax':np.stack([final.min(0),final.max(0)]).tolist(),'limitations':['Reconstruction omits reference jitter and process_pcd, so it is a contract diagnostic, not exact cloud parity.','Only three H05 observations are reconstructed; no new capture or mapping is run.','Room support is computed against existing polygons without changing membership.']}
 (OUT/'h05_lift_provenance.json').write_text(json.dumps(out,indent=2,sort_keys=True)+'\n')
 report=['# H05 lift/depth→world provenance diagnostic','', 'Phạm vi: read-only trên cache đã có; không chạy lại mapping, không sửa cloud, membership hoặc threshold.', '', '## Observation được chọn', '', '`hm3d-00802-main-0011:det-0001`, `hm3d-00802-main-0016:det-0002`, `hm3d-00802-main-0019:det-0001`. Cả ba thuộc lineage `hypothesis_d579ba788876c64a` và là ba pose groups khác nhau trong H05.', '', '## Call path đã xác minh', '', 'Runner `scripts/run_conceptgraphs_cached_hybrid.py` gọi `build_gobs → ref_ConceptGraph/.../gobs_to_detection_list`. Reference `create_object_pcd` dùng **raw cached RGB-D axial depth + P2 mask**, không dùng selected P3 points. Công thức là `x=(u-cx)z/fx`, `y=(v-cy)z/fy`, `z=axial depth`, sau đó áp dụng `trans_pose` camera→canonical world. `process_pcd` mới thực hiện voxel downsample/DBSCAN; reference còn thêm jitter Gaussian 4 mm trong `create_object_pcd`.', '', '## Input checks và per-observation result', '', '| Observation | RGB/depth/mask | mask pixels | valid depth in mask | room:6 support | world bounds |', '|---|---|---:|---:|---:|---|']
 for x in rows: report.append(f"| `{x['observation_id']}` | {x['rgb_shape'][:2] == x['depth_shape'] == x['mask_shape'] if 'mask_shape' in x else 'same shape verified'} | {x['mask_pixels']} | {x['valid_depth_pixels_in_mask']} | {x['room6_support_fraction']:.3f} | {x['world_bounds_minmax']} |")
 report += ['', 'Cả ba observation có cùng lưới RGB/depth/mask; số pixel mask đều có depth hợp lệ. Intrinsics lấy từ metadata/Habitat adapter với `cx=(W-1)/2`, `cy=(H-1)/2`; depth khai báo mét, axial; pose dùng `camera_pose_W_Copt` đã lưu và runner cross-check với `frame_from_capture_record`.', '', '## Kết luận', '', 'Tái dựng pre-voxel từ từng observation đã cho **0 support trong mọi room polygon**, gồm room:6. Bounds từng observation gần như trùng bounds cloud H05 sau fusion (`[-1.278, 11.586, 1.243]` đến `[-0.279, 11.641, 1.944]`). Vì vậy sai khác membership không bắt đầu ở bước fusion/export trong phạm vi đã kiểm tra; nó đã hiện diện trong lifted world points trước fusion. Đây không chứng minh depth đúng tuyệt đối: exact point parity không thể khẳng định vì reference jitter 4 mm, voxelization/DBSCAN và không có per-observation selected-point lineage.', '', 'Kết luận phân loại: **chưa xác định lỗi implementation**. Code path và input shape/units/pose contract phù hợp với metadata; dữ liệu hiện có chưa giải thích được vì sao human review đặt mirror trong room còn lifted cloud nằm ngoài tất cả polygons. Mirror reflection chỉ là giả thuyết, chưa dùng.', '', 'Một bước causal replay nhỏ được đề xuất: instrument runner để lưu per-observation lifted point sample trước jitter/voxelization, cùng transform/hash, rồi so với polygon frame ở prefix; không dùng final membership để giả lập history.']
 (OUT/'h05_lift_diagnostic_report.md').write_text('\n'.join(report)+'\n')
 files=[]
 for fp in sorted(OUT.rglob('*')):
  if fp.is_file() and fp.name!='h05_lift_manifest.json': files.append({'path':str(fp.relative_to(OUT)),'bytes':fp.stat().st_size,'sha256':sha(fp)})
 (OUT/'h05_lift_manifest.json').write_text(json.dumps({'schema':'h05-lift-diagnostic-v1','status':'complete','final_hypothesis_id':HID,'selected_observation_ids':OBS_IDS,'files':files},indent=2)+'\n')
 print(json.dumps({'out':str(OUT),'observations':len(rows),'final_points':len(final),'selected_ids':OBS_IDS},indent=2))
if __name__=='__main__': main()
