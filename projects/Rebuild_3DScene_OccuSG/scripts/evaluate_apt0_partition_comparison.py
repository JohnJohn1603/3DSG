#!/usr/bin/env python3
"""Protocol-fixed baseline/candidate comparison for the user-confirmed apt_0 GT."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Polygon as MplPolygon
import numpy as np

from evaluation.metrics import room_partition_metrics, validate_room_annotations
from standalone_sg.pipeline.contracts import SceneGraphSnapshot
from standalone_sg.serialization import load_delta_log, load_snapshot, replay_deltas

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GT = ROOT / "evaluation/gt/apt_0/versions/apt0_room_gt_user_confirmed_20261004_normalized_v1/room_polygons_normalized.geojson"
DEFAULT_BASELINE = ROOT / "artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001"
RES_M = 0.05
CHECKPOINTS = (0, 5, 11, 17, 23)
PALETTE = {"room_1":"#ffd166", "room_2":"#06d6a0", "room_3":"#4cc9f0", "room_4":"#c77dff"}


def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
    return h.hexdigest()


def load_rooms(doc):
    return [{"id":str(f["properties"]["room_id"]),"polygon":f["geometry"]} for f in doc["features"]]


def polygon_region(rid, outer, holes=()):
    rings=[outer,*holes]
    if rings and rings[0] and rings[0][0] != rings[0][-1]: rings[0]=[ *rings[0], rings[0][0] ]
    return {"id":str(rid),"polygon":{"coordinates":rings}}


def plain(value):
    if isinstance(value, dict): return {str(k):plain(v) for k,v in value.items()}
    if hasattr(value,"items"): return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [plain(x) for x in value]
    return value


def node_record(node):
    return {"node_id":node.node_id,"kind":node.kind,"first_seen_s":node.first_seen_s,
            "last_seen_s":node.last_seen_s,"provenance":node.provenance,"attributes":plain(node.attributes)}


def regions_at(meta, layer):
    if layer=="m4":
        return [polygon_region(x["raw_id"],x["outer"]) for x in meta.get("m4",{}).get("raw_regions",[]) if len(x.get("outer",[]))>=3]
    if layer=="m5":
        m5=meta.get("m5",{})
        return [polygon_region(x["persistent_id"],x["polygon"]["outer"],x["polygon"].get("holes",[]))
                for x in m5.get("current_regions",[]) if len(x.get("polygon",{}).get("outer",[]))>=3]
    if layer=="m8":
        return [polygon_region(x["node_id"],x["attributes"]["outer_ring"],x["attributes"].get("holes",[]))
                for x in meta.get("m8",{}).get("rooms",[]) if len(x.get("attributes",{}).get("outer_ring",[]))>=3]
    raise ValueError(layer)


def _cell_indices_for_known_free(meta, arrays):
    res=float(meta["resolution_m"]); ox,oy=map(float,meta["origin_xy"])
    ix0,iy0=round(ox/res),round(oy/res)
    if abs(ix0*res-ox)>1e-7 or abs(iy0*res-oy)>1e-7 or abs(res-RES_M)>1e-9:
        raise ValueError("M3 grid origin/resolution is not aligned to the fixed evaluation lattice")
    rows,cols=np.nonzero(arrays["values"]==0)
    return {(ix0+int(c),iy0+int(r)) for r,c in zip(rows,cols)}


def load_trace(run: Path):
    root=run/"grid_trace"
    if not root.is_dir(): root=run/"m3_m4_grid_trace"
    if not root.is_dir(): root=run/"replay"/"grid_trace"
    index=json.loads((root/"index.json").read_text())
    if index.get("frame_count")!=24 or len(index.get("frames",[]))!=24:
        raise ValueError(f"expected the locked 24-frame trace, got {index.get('frame_count')}")
    records=[]
    for ent in index["frames"]:
        meta=json.loads((root/ent["metadata_file"]).read_text())
        arrays=np.load(root/ent["array_file"])
        if meta["frame_id"]!=ent["frame_id"] or meta["frame_index"]!=ent["frame_index"]:
            raise ValueError("grid trace index and metadata disagree")
        records.append({"entry":ent,"meta":meta,"arrays":{k:arrays[k].copy() for k in arrays.files}})
    return index,records


def restore_m8_from_deltas(run:Path,trace_records):
    replay=run/"replay" if (run/"replay"/"manifest.json").is_file() else run
    manifest=json.loads((replay/"manifest.json").read_text())
    saved=load_snapshot(replay/"snapshot.json")
    deltas=load_delta_log(replay/"delta_log.json")
    initial=SceneGraphSnapshot(1,None,None,0,None,(),(),manifest["config_sha256"],{})
    by_revision={0:initial}; current=initial
    for i,delta in enumerate(deltas):
        current=replay_deltas(current,(delta,));by_revision[current.revision]=current
    if current != saved:
        raise RuntimeError("restored M8 graph from the locked delta log does not equal final snapshot")
    comparisons=[]
    for rec in trace_records:
        m8=rec["meta"]["m8"]; rev=int(m8["snapshot_revision"])
        snap=by_revision.get(rev)
        if snap is None: raise RuntimeError(f"no graph delta prefix reconstructs M8 snapshot revision {rev}")
        restored=[node_record(n) for n in snap.nodes if n.kind=="room"]
        traced=m8.get("rooms",[])
        norm=lambda xs: sorted(xs,key=lambda x:x["node_id"])
        if norm(restored)!=norm(traced):
            raise RuntimeError(f"M8 rooms in grid trace differ from delta-restored revision {rev}")
        comparisons.append({"frame_index":rec["meta"]["frame_index"],"frame_id":rec["meta"]["frame_id"],
                            "graph_revision":rev,"room_count":len(restored),"matches_delta_reconstruction":True})
    return saved,by_revision,comparisons,manifest


def _rooms_from_snapshot(snapshot):
    return [polygon_region(n.node_id,n.attributes.get("outer_ring",[]),n.attributes.get("holes",[]))
            for n in snapshot.nodes if n.kind=="room" and len(n.attributes.get("outer_ring",[]))>=3]


def _score_series(run:Path, records, gt_rooms, roi, observed_free, final_rooms):
    rows=[]
    for rec in records:
        meta=rec["meta"]
        row={"frame_index":meta["frame_index"],"frame_id":meta["frame_id"],
             "simulation_timestamp_s":meta["simulation_timestamp_s"],"map_revision":meta["map_revision"],
             "graph_revision":meta["m8"]["snapshot_revision"],
             "m4_branch":meta["m4"]["branch"],"m4_status":meta["m4"]["status"],
             "m5_accepted":meta["m5"]["accepted"],"m5_stale":meta["m5"]["stale"],
             "m8_stale":meta["m8"]["regions_stale"]}
        for layer in ("m4","m5","m8"):
            pred=regions_at(meta,layer)
            row[f"{layer}_region_count"]=len(pred)
            row[f"{layer}_metrics"]=room_partition_metrics(gt_rooms,pred,resolution_m=RES_M,roi_cells=roi,
                    observed_free_cells=observed_free)
        rows.append(row)
    terminal=room_partition_metrics(gt_rooms,final_rooms,resolution_m=RES_M,roi_cells=roi,observed_free_cells=observed_free)
    return rows,terminal


def _draw_region_lines(ax,regions,color,linestyle="-",linewidth=1.4):
    for reg in regions:
        for ring in reg["polygon"]["coordinates"]:
            xy=np.asarray(ring,float)
            if len(xy)>1: ax.plot(xy[:,0],xy[:,1],color=color,linestyle=linestyle,linewidth=linewidth,zorder=4)


def render_layer_checkpoints(records,gt_features,run_id,out_path):
    selected=[next(r for r in records if r["meta"]["frame_index"]==i) for i in CHECKPOINTS]
    fig,axes=plt.subplots(4,len(selected),figsize=(22,14),sharex=True,sharey=True)
    cmap=ListedColormap(["#bdbdbd","#ffffff","#20242a"]);norm=BoundaryNorm([-1.5,-.5,.5,101],cmap.N)
    for col,rec in enumerate(selected):
        meta,arr=rec["meta"],rec["arrays"]
        ax=axes[0,col]; ox,oy=meta["origin_xy"];res=meta["resolution_m"]
        ax.imshow(arr["values"],origin="lower",extent=[ox,ox+arr["values"].shape[1]*res,oy,oy+arr["values"].shape[0]*res],
                  cmap=cmap,norm=norm,interpolation="nearest",aspect="equal")
        mask=arr.get("m4_preprocessed_mask")
        if mask is not None and np.any(mask): ax.contour(mask,levels=[.5],colors=["#ce2d88"],linewidths=.7,
             extent=[ox,ox+mask.shape[1]*res,oy,oy+mask.shape[0]*res],origin="lower")
        ax.set_title(f"frame {meta['frame_index']} · map rev {meta['map_revision']}\nM4 {meta['m4']['branch']}",fontsize=8)
        layers=("m4","m5","m8")
        for row,layer in enumerate(layers,1):
            ax=axes[row,col]
            for f in gt_features:
                rid=f["properties"]["room_id"];ring=f["geometry"]["coordinates"][0]
                ax.add_patch(MplPolygon(ring,closed=True,facecolor=PALETTE.get(rid,"#ccc"),edgecolor="none",alpha=.14,zorder=1))
            color={"m4":"#20242a","m5":"#1464c0","m8":"#d43d35"}[layer]
            _draw_region_lines(ax,regions_at(meta,layer),color,"--" if layer=="m8" else "-",1.25)
            ax.set_title(f"{layer.upper()} · {len(regions_at(meta,layer))} regions",fontsize=9)
        for row in range(4):
            ax=axes[row,col];ax.set_xlim(-2.95,4.85);ax.set_ylim(-8.45,5.10);ax.set_aspect("equal",adjustable="box")
            ax.grid(True,color="#ddd",linewidth=.3)
            if row==3:ax.set_xlabel("world X (m)",fontsize=8)
        axes[0,col].set_ylabel("M3 values / M4 mask" if col==0 else "")
        axes[1,col].set_ylabel("M4 raw" if col==0 else "")
        axes[2,col].set_ylabel("M5 current" if col==0 else "")
        axes[3,col].set_ylabel("M8 materialized" if col==0 else "")
    fig.suptitle(f"{run_id}: M3 → M4 → M5 → M8 checkpoints; colored room fills are evaluation GT only",y=.995)
    fig.tight_layout(rect=(0,0,1,.98));fig.savefig(out_path,dpi=150,bbox_inches="tight");plt.close(fig)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--gt",type=Path,default=DEFAULT_GT)
    ap.add_argument("--baseline",type=Path,default=DEFAULT_BASELINE)
    ap.add_argument("--candidate",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    out=args.output.resolve()
    if out.exists() and any(out.iterdir()): raise SystemExit(f"Refusing to overwrite non-empty evaluation output: {out}")
    gt_doc=json.loads(args.gt.read_text());gt_rooms=load_rooms(gt_doc)
    validation=validate_room_annotations(gt_rooms,resolution_m=RES_M)
    bidx,brec=load_trace(args.baseline);cidx,crec=load_trace(args.candidate)
    if [x["meta"]["frame_id"] for x in brec] != [x["meta"]["frame_id"] for x in crec]:
        raise RuntimeError("baseline and candidate do not contain identical ordered frame IDs")
    # M2/M3 must be byte-identical per frame; this is an M4-only comparison.
    for b,c in zip(brec,crec):
        bm,cm=b["meta"],c["meta"]
        for key in ("values","z_floor","z_ceiling"):
            if not np.array_equal(b["arrays"][key],c["arrays"][key],equal_nan=True):
                raise RuntimeError(f"M3 array {key} changed at {bm['frame_id']}; comparison is not M4-only")
        for key in ("origin_xy","resolution_m","shape","frame_id_coordinate","projection_mode"):
            if bm[key]!=cm[key]: raise RuntimeError(f"M3 geometry/provenance differs at {bm['frame_id']}: {key}")
    roi_rooms,_=__import__("evaluation.metrics",fromlist=["_raster_rooms"])._raster_rooms(gt_rooms,RES_M)
    roi=set().union(*roi_rooms.values())
    # Fixed observed free-space domain from baseline M3 (0 means free; positive
    # source occupation score remains non-probabilistic and is excluded here).
    observed=set()
    for rec in brec: observed |= _cell_indices_for_known_free(rec["meta"],rec["arrays"])
    observed &= roi
    bfinal,bdeltas,brebuild,bmanifest=restore_m8_from_deltas(args.baseline,brec)
    cfinal,cdeltas,crebuild,cmanifest=restore_m8_from_deltas(args.candidate,crec)
    if bmanifest.get("capture_manifest_sha256")!=cmanifest.get("capture_manifest_sha256"):
        raise RuntimeError("candidate did not use the same capture manifest")
    bseries,bterminal=_score_series(args.baseline,brec,gt_rooms,roi,observed,_rooms_from_snapshot(bfinal))
    cseries,cterminal=_score_series(args.candidate,crec,gt_rooms,roi,observed,_rooms_from_snapshot(cfinal))
    out.mkdir(parents=True,exist_ok=True)
    report={"schema_version":1,"kind":"apt0_fixed_capture_room_partition_comparison",
        "gt_file":str(args.gt.resolve()),"gt_sha256":sha256(args.gt.resolve()),"gt_status":gt_doc["metadata"].get("status"),
        "gt_protocol_validation":validation,"capture_manifest_sha256":bmanifest["capture_manifest_sha256"],
        "capture_frame_count":len(brec),"resolution_m":RES_M,"roi_rule":"union of normalized four user-confirmed polygons on globally anchored 0.05m cell-centre lattice; identical for both profiles",
        "roi_cells":len(roi),"roi_area_m2":len(roi)*RES_M**2,
        "observed_domain_rule":"union of baseline M3 cells equal to 0 (known free); fixed and reused for both profiles; positive source occupation scores are not treated as probabilities or free cells",
        "observed_free_cells_in_roi":len(observed),"observed_free_area_m2":len(observed)*RES_M**2,
        "matching":{"algorithm":"maximum-total-IoU one-to-one Hungarian assignment","threshold":0.10,
                    "macro_iou_primary":"sum of matched IoUs plus zero for every unmatched GT room, divided by all four GT rooms",
                    "split_merge_significance":"directional area fractions at 1%, 5%, and 10%; full nonzero intersection matrix retained",
                    "boundary_metrics":None,"boundary_metrics_reason":"not part of the primary partition objective; external annotation edges and doorway/open-plan convention are not sufficiently specified for a defensible boundary score"},
        "m3_identical_all_frames":True,
        "baseline":{"run":str(args.baseline.resolve()),"manifest_sha256":sha256(args.baseline/"replay/manifest.json"),
            "config_sha256":bmanifest.get("config_sha256"),"m2_m3_binary_sha256":bmanifest.get("m2_m3_binary_sha256"),"m4_binary_sha256":bmanifest.get("dude_binary_sha256"),
            "m4_policy":"source","grid_trace_m8_matches_delta_restored_history":all(x["matches_delta_reconstruction"] for x in brebuild),
            "m8_terminal_rooms":len([n for n in bfinal.nodes if n.kind=="room"]),"m8_terminal_metrics":bterminal,
            "timeline":bseries,"delta_reconstruction":brebuild},
        "candidate":{"run":str(args.candidate.resolve()),"manifest_sha256":sha256((args.candidate/"replay"/"manifest.json") if (args.candidate/"replay"/"manifest.json").is_file() else (args.candidate/"manifest.json")),
            "m4_policy":cmanifest.get("m4_preprocessing_policy"),"m4_effective_parameters":cmanifest.get("m4_effective_parameters"),
            "m3_identical_all_frames":True,"grid_trace_m8_matches_delta_restored_history":all(x["matches_delta_reconstruction"] for x in crebuild),
            "m8_terminal_rooms":len([n for n in cfinal.nodes if n.kind=="room"]),"m8_terminal_metrics":cterminal,
            "timeline":cseries,"delta_reconstruction":crebuild,"runtime_ms":cmanifest.get("runtime_ms"),
            "python_process_ru_maxrss_kib":cmanifest.get("python_process_ru_maxrss_kib"),
            "snapshot_reload_equal":cmanifest.get("snapshot_reload_equal"),"delta_replay_equal":cmanifest.get("delta_replay_equal"),
            "fresh_replay_equal":cmanifest.get("fresh_replay_equal")},
        "candidate_decision_thresholds_predeclared":{"macro_iou_absolute_gain_min":0.05,"coverage_drop_max_absolute":0.02,
            "any_room_iou_drop_over_0.05_requires_explanation_and_blocks_automatic_promotion":True},
        "gt_used_in_builder":False,"functional_labels_adjacency_membership_metrics":None}
    (out/"comparison.json").write_text(json.dumps(report,indent=2,sort_keys=True,allow_nan=False)+"\n")
    render_layer_checkpoints(brec,gt_doc["features"],"baseline-source",out/"baseline_M3_M4_M5_M8_checkpoints.png")
    render_layer_checkpoints(crec,gt_doc["features"],str(cmanifest.get("m4_preprocessing_policy")),out/"candidate_M3_M4_M5_M8_checkpoints.png")
    print(json.dumps({"output":str(out),"baseline_macro_iou":bterminal["macro_iou_all_gt_rooms"],
        "candidate_macro_iou":cterminal["macro_iou_all_gt_rooms"],"baseline_union_iou":bterminal["union_iou"],
        "candidate_union_iou":cterminal["union_iou"],"baseline_observed_macro_iou":bterminal["observed_free_domain"]["macro_iou_all_gt_rooms"],
        "candidate_observed_macro_iou":cterminal["observed_free_domain"]["macro_iou_all_gt_rooms"],
        "candidate_runtime_ms":cmanifest.get("runtime_ms")},indent=2))


if __name__=="__main__": main()
