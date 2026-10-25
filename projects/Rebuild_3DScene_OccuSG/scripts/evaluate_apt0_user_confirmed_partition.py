#!/usr/bin/env python3
"""Preserve and provisionally score a user-confirmed apt_0 room partition.

This evaluator-only utility never feeds room GT to M1-M10 and never edits the
source submission, locked capture, detector cache, baseline replay, or profile.
Known polygon-overlap cells are excluded from the provisional scoring ROI and
reported explicitly; no polygon is silently normalized.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Polygon as MplPolygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.metrics import (  # noqa: E402
    EvaluationContractError,
    _best_assignment,
    _iou,
    _raster_rooms,
    room_metrics,
    validate_room_annotations,
)
from scripts.prepare_apt0_room_annotation import slice_segments, stage_triangles  # noqa: E402

CAPTURE_ID = "habitat_replica_cad_apt0_perception_dev_long_20261001"
BASE = ROOT / "artifacts/runs" / CAPTURE_ID
LOCK = ROOT / "evaluation/runs/apt0_gt_20261003_01/manifest.json"
EXPECTED_SOURCE_SHA256 = "078b6c8370403f8d2d9e77ee2db21df88407ce905447fa6b9f4b7de0eea1ee31"
STAGE = ROOT / "artifacts/habitat_data/versioned_data/replica_cad_dataset/stages/frl_apartment_stage.glb"
RESOLUTION_M = 0.05


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def load_rooms(doc):
    return [{"id": str(f["properties"]["room_id"]), "polygon": f["geometry"]} for f in doc["features"]]


def exact_convex_overlaps(features):
    rows = []
    contours = {}
    for f in features:
        rid = str(f["properties"]["room_id"])
        ring = np.asarray(f["geometry"]["coordinates"][0][:-1], dtype=np.float32).reshape(-1, 1, 2)
        contours[rid] = ring
    ids = list(contours)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pa, pb = contours[a], contours[b]
            if not cv2.isContourConvex(pa) or not cv2.isContourConvex(pb):
                continue
            area, poly = cv2.intersectConvexConvex(pa, pb)
            if float(area) > 1e-9:
                rows.append({"room_a": a, "room_b": b, "intersection_area_m2": float(area),
                             "intersection_polygon_xy": [] if poly is None else np.asarray(poly).reshape(-1, 2).astype(float).tolist(),
                             "method": "OpenCV intersectConvexConvex; both rings verified convex"})
    return contours, rows


def raster_overlap_cells(rooms):
    by_room, _ = _raster_rooms(rooms, RESOLUTION_M)
    ids = list(by_room)
    pair_rows = []
    all_overlap = set()
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            overlap = by_room[a] & by_room[b]
            if overlap:
                all_overlap |= overlap
                pair_rows.append({"room_a": a, "room_b": b, "overlap_cells": len(overlap),
                                  "overlap_area_on_lattice_m2": len(overlap) * RESOLUTION_M**2,
                                  "cell_indices_xy": [list(x) for x in sorted(overlap)]})
    return by_room, all_overlap, pair_rows


def clip_to_roi_metrics(gt_rooms, pred_rooms, roi_cells, gt_cells_by_room, overlap_cells):
    score_roi = set(roi_cells) - set(overlap_cells)
    gt = {rid: cells & score_roi for rid, cells in gt_cells_by_room.items()}
    pred_raw, _ = _raster_rooms(pred_rooms, RESOLUTION_M)
    pred = {rid: cells & score_roi for rid, cells in pred_raw.items() if cells & score_roi}
    gt_ids, pred_ids = list(gt), list(pred)
    weights = [[_iou(gt[g], pred[p]) for p in pred_ids] for g in gt_ids]
    assignment = _best_assignment(weights)
    matches = [(gt_ids[i], pred_ids[j], weights[i][j]) for i, j in assignment
               if weights[i][j] >= 0.10]
    matched_gt = {x[0] for x in matches}; matched_pred = {x[1] for x in matches}
    splits = []
    for g in gt_ids:
        covered = [p for p in pred_ids if gt[g] & pred[p]]
        if len(covered) > 1:
            splits.append({"gt_room_id": g, "predicted_room_ids": covered})
    merges = []
    for p in pred_ids:
        covered = [g for g in gt_ids if pred[p] & gt[g]]
        if len(covered) > 1:
            merges.append({"predicted_room_id": p, "gt_room_ids": covered})
    gt_union = set().union(*gt.values()) if gt else set()
    pred_union = set().union(*pred.values()) if pred else set()
    return {
        "status": "provisional_scored_with_confirmed_roi_and_overlap_band_excluded",
        "roi_rule": "union of the four user-confirmed room footprints on globally anchored 0.05m cell-centre lattice; same ROI applied to GT and predictions",
        "overlap_rule": "15 cells shared by GT polygons excluded from every room and ROI for this provisional score; raw submission unchanged",
        "resolution_m": RESOLUTION_M,
        "roi_cells_before_overlap_exclusion": len(roi_cells),
        "ambiguous_overlap_cells_excluded": len(overlap_cells),
        "scored_roi_cells": len(score_roi),
        "scored_roi_area_m2": len(score_roi) * RESOLUTION_M**2,
        "gt_room_count": len(gt_ids), "predicted_room_count_intersecting_roi": len(pred_ids),
        "matches": [{"gt_room_id": g, "predicted_room_id": p, "iou": s} for g, p, s in matches],
        "matched_count": len(matches), "missed_gt_room_ids": sorted(set(gt_ids)-matched_gt),
        "extra_predicted_room_ids": sorted(set(pred_raw)-matched_pred),
        "macro_iou_matched": sum(x[2] for x in matches)/len(matches) if matches else None,
        "union_iou": _iou(gt_union, pred_union),
        "gt_coverage_by_prediction": len(gt_union & pred_union)/len(gt_union) if gt_union else None,
        "predicted_area_over_scored_gt_roi": len(pred_union)/len(gt_union) if gt_union else None,
        "over_segmentation": splits, "under_segmentation": merges,
        "boundary_metrics": None,
        "boundary_metrics_reason": "user-defined partial spatial ROI has no confirmed external-boundary mask; overlap band and ROI clipping edges must not be scored as physical room boundaries",
    }


def metrics_for_pred(pred_rooms, gt_rooms, gt_by_room, overlap_cells, roi):
    return clip_to_roi_metrics(gt_rooms, pred_rooms, roi, gt_by_room, overlap_cells)


def stage_sections_with_polygon(stage_tri, gt_features, predicted_final, room4_feature, out_path):
    # Canonical XY = Habitat X,-Z; section Y is Habitat height.
    heights = [0.25, 0.85, 1.45, 1.85, 2.05]
    fig, axes = plt.subplots(1, len(heights), figsize=(20, 7), sharex=True, sharey=True)
    lo = np.array([-2.95, -8.45]); hi = np.array([4.85, 5.10])
    room4_ring = np.asarray(room4_feature["geometry"]["coordinates"][0], dtype=float)
    for ax, height in zip(axes, heights):
        segments, _ = slice_segments(stage_tri, height)
        for a, b in segments:
            ax.plot([a[0], b[0]], [a[1], b[1]], color="#263746", linewidth=0.55, zorder=1)
        ax.add_patch(MplPolygon(room4_ring, closed=True, facecolor="#e84a5f", edgecolor="#a40019",
                                linewidth=1.2, alpha=0.20, linestyle="--", zorder=3))
        ax.set(xlim=(lo[0], hi[0]), ylim=(lo[1], hi[1]), title=f"Habitat Y={height:.2f} m")
        ax.set_aspect("equal", adjustable="box"); ax.grid(True, color="#ddd", linewidth=.35)
        ax.set_xlabel("canonical X (m)")
    axes[0].set_ylabel("canonical Y (m)")
    fig.suptitle("room_4 vertical check — red outline is the submitted floor polygon; architecture-only sections", y=1.01)
    fig.tight_layout(rect=(0, 0, 1, .96)); fig.savefig(out_path, dpi=180, bbox_inches="tight"); plt.close(fig)


def stage_gt_m8_overlay(stage_tri, gt_features, pred_rooms, exact_overlaps, out_path):
    fig, ax = plt.subplots(figsize=(10, 13))
    segments, _ = slice_segments(stage_tri, 0.85)
    for a, b in segments:
        ax.plot([a[0], b[0]], [a[1], b[1]], color="#354451", linewidth=0.65, zorder=1)
    palette = {"room_1":"#ffd166", "room_2":"#06d6a0", "room_3":"#4cc9f0", "room_4":"#c77dff"}
    for f in gt_features:
        rid = str(f["properties"]["room_id"]); ring=np.asarray(f["geometry"]["coordinates"][0],float)
        ax.add_patch(MplPolygon(ring,closed=True,facecolor=palette.get(rid,"#ccc"),edgecolor=palette.get(rid,"#222"),
                                linewidth=1.8,alpha=.25,zorder=2,label=f"user GT {rid}"))
        c=ring[:-1].mean(axis=0); ax.text(c[0],c[1],rid,fontsize=9,ha="center",zorder=5,
                                           bbox={"facecolor":"white","alpha":.75,"edgecolor":"none"})
    for f in gt_features:
        ring=np.asarray(f["geometry"]["coordinates"][0],float)
        ax.plot(ring[:,0],ring[:,1],color=palette.get(str(f["properties"]["room_id"]),"#333"),linewidth=2,zorder=4)
    for overlap_i, row in enumerate(exact_overlaps):
        q=np.asarray(row["intersection_polygon_xy"],float)
        ax.add_patch(MplPolygon(q,closed=True,facecolor="#ff0000",edgecolor="#b00000",linewidth=2,
                                alpha=.80,zorder=6,label="GT polygon overlap" if overlap_i == 0 else "_nolegend_"))
        c=q.mean(axis=0); ax.annotate(f"overlap {row['room_a']}–{row['room_b']}\n{row['intersection_area_m2']:.4f} m²",
                                      c,xytext=(8,8),textcoords="offset points",fontsize=8,
                                      bbox={"facecolor":"white","alpha":.9})
    for p in pred_rooms:
        ring=np.asarray(p["polygon"]["coordinates"][0],float)
        ax.plot(ring[:,0],ring[:,1],color="#161616",linewidth=1.0,linestyle=(0,(4,2)),alpha=.8,zorder=3)
    ax.plot([],[],color="#111",linestyle=(0,(4,2)),label="baseline M8 RoomNode footprints")
    ax.set(xlim=(-2.95,4.85),ylim=(-8.45,5.10),xlabel="canonical world X (m)",ylabel="canonical world Y (m)",
           title="apt_0 — user-confirmed spatial partition vs baseline M8 (not semantic labels)")
    ax.set_aspect("equal",adjustable="box");ax.grid(True,color="#ddd",linewidth=.4);ax.legend(loc="lower left",fontsize=8)
    fig.tight_layout();fig.savefig(out_path,dpi=180);plt.close(fig)


def stage_m4_checkpoints(gt_features, temporal, index, base_dir, out_path):
    wanted = {0, 5, 11, 17, 23}
    selected = [x for x in temporal if x["frame_index"] in wanted]
    fig, axes = plt.subplots(1, len(selected), figsize=(21, 6), sharex=True, sharey=True)
    palette = {"room_1":"#ffd166", "room_2":"#06d6a0", "room_3":"#4cc9f0", "room_4":"#c77dff"}
    for ax, item in zip(axes, selected):
        meta_path = next(f["metadata_file"] for f in index["frames"] if f["frame_index"] == item["frame_index"])
        meta = json.loads((base_dir / "grid_trace" / meta_path).read_text())
        for feature in gt_features:
            rid = str(feature["properties"]["room_id"])
            ring = np.asarray(feature["geometry"]["coordinates"][0], float)
            ax.add_patch(MplPolygon(ring, closed=True, facecolor=palette[rid], edgecolor=palette[rid],
                                    linewidth=1, alpha=.11, zorder=1))
        for region in meta.get("m4", {}).get("raw_regions", []):
            ring = region.get("outer", [])
            if len(ring) >= 3:
                ring = np.asarray(ring, float)
                if not np.allclose(ring[0], ring[-1]): ring = np.vstack([ring, ring[0]])
                ax.plot(ring[:, 0], ring[:, 1], color="#151515", linewidth=1.45, zorder=3)
        ax.set(xlim=(-2.95,4.85), ylim=(-8.45,5.10), xlabel="canonical X (m)",
               title=f"frame {item['frame_index']} · {item['m4_raw_region_count']} M4 regions\n{item['m4_branch']}")
        ax.set_aspect("equal", adjustable="box"); ax.grid(True, color="#ddd", linewidth=.35)
    axes[0].set_ylabel("canonical Y (m)")
    fig.suptitle("apt_0 M4 raw-region geometry at five sequence checkpoints — GT fills are spatial context only", y=1.01)
    fig.tight_layout(rect=(0,0,1,.96)); fig.savefig(out_path, dpi=170, bbox_inches="tight"); plt.close(fig)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path,default=Path("/home/kiettran05/Downloads/room_polygons.geojson"))
    ap.add_argument("--gt-version-id",default="apt0_room_gt_user_confirmed_20261004_v1")
    ap.add_argument("--run-id",default="apt0_room_baseline_user_confirmed_20261004_v2")
    args=ap.parse_args()
    src=args.source.resolve()
    if not src.is_file(): ap.error(f"source file not found: {src}")
    source_hash=sha256(src)
    if source_hash!=EXPECTED_SOURCE_SHA256: ap.error(f"source hash mismatch: expected {EXPECTED_SOURCE_SHA256}, got {source_hash}")
    version_dir=ROOT/"evaluation/gt/apt_0/versions"/args.gt_version_id
    run_dir=ROOT/"evaluation/runs"/args.run_id
    if run_dir.exists(): ap.error("refusing to overwrite run directory")
    raw_dir=version_dir/"raw_submission"
    raw_copy=raw_dir/"room_polygons.geojson"
    if version_dir.exists():
        # Resume only our own interrupted, byte-identical preparation. Never
        # overwrite a different annotation or any completed version metadata.
        if not raw_copy.is_file() or sha256(raw_copy) != source_hash:
            ap.error("version directory exists with missing or different raw submission; refusing to modify it")
        confirmed_copy=version_dir/"spatial_partition_confirmed_unmodified.geojson"
        if confirmed_copy.exists():
            existing=json.loads(confirmed_copy.read_text(encoding="utf-8"))
            if existing.get("metadata",{}).get("partition_review",{}).get("source_sha256") != source_hash:
                ap.error("existing confirmed partition has different provenance; refusing to modify it")
    else:
        raw_dir.mkdir(parents=True)
        shutil.copyfile(src,raw_copy)
        shutil.copystat(src,raw_copy)
    doc=json.loads(src.read_text(encoding="utf-8"));features=doc["features"]
    confirmed=json.loads(json.dumps(doc))
    confirmed["metadata"]["status"]="human_verified_spatial_partition_pending_topology_review"
    confirmed["metadata"]["partition_review"]={"status":"user_confirmed","confirmed_date":"2026-10-04",
        "scope":"spatial partition only; does not confirm functional labels, doorway adjacency, class mappings, object-room assignments, or unannotated apartment areas",
        "source_sha256":source_hash,"normalization":"none; coordinates and rings copied without geometric edits"}
    for f in confirmed["features"]:
        f["properties"]["annotation_status"]="human_verified_spatial_partition_pending_topology_review"
        f["properties"]["reviewer"]="user-confirmed 2026-10-04 (spatial partition only)"
    version_dir.mkdir(parents=True,exist_ok=True)
    confirmed_copy=version_dir/"spatial_partition_confirmed_unmodified.geojson"
    if not confirmed_copy.exists():
        write_json(confirmed_copy,confirmed)
    rooms=load_rooms(doc)
    contours,exact_overlaps=exact_convex_overlaps(features)
    gt_by_room,overlap_cells,lattice_overlaps=raster_overlap_cells(rooms)
    gt_roi=set().union(*gt_by_room.values())
    strict_error=None
    try: strict_validation=validate_room_annotations(rooms,resolution_m=RESOLUTION_M)
    except EvaluationContractError as e: strict_validation=None;strict_error=str(e)
    # Extract baseline M8 rooms strictly from the locked final snapshot.
    snapshot=json.loads((BASE/"replay/snapshot.json").read_text())["snapshot"]
    pred_m8=[]
    for n in snapshot["nodes"]:
        if n.get("kind")!="room": continue
        ring=n.get("attributes",{}).get("outer_ring",[])
        if len(ring)>=3:
            if ring[0]!=ring[-1]:ring=ring+[ring[0]]
            pred_m8.append({"id":n["node_id"],"polygon":{"coordinates":[ring]}})
    overlap_polys=[r["intersection_polygon_xy"] for r in exact_overlaps]
    # Use protocol raster to create conservative cells for the tiny overlap bands.
    clipped_m8=clip_to_roi_metrics(rooms,pred_m8,gt_roi,gt_by_room,overlap_cells)
    raw_m8=room_metrics(rooms,pred_m8)
    # Score every M4 raw-region snapshot against the same ROI from this user partition.
    index=json.loads((BASE/"grid_trace/index.json").read_text())
    temporal=[]
    for frame in index["frames"]:
        meta=json.loads((BASE/"grid_trace"/frame["metadata_file"]).read_text())
        raw=[]
        for reg in meta.get("m4",{}).get("raw_regions",[]):
            ring=reg.get("outer",[])
            if len(ring)>=3:
                if ring[0]!=ring[-1]:ring=ring+[ring[0]]
                raw.append({"id":str(reg.get("raw_id")),"polygon":{"coordinates":[ring]}})
        temporal.append({"frame_id":frame["frame_id"],"frame_index":frame["frame_index"],
            "map_revision":frame["map_revision"],"projection_mode":frame["projection_mode"],
            "m4_branch":meta.get("m4",{}).get("branch"),"m4_raw_region_count":len(raw),
            "metrics":clip_to_roi_metrics(rooms,raw,gt_roi,gt_by_room,overlap_cells)})
    diagnostics=[json.loads(x) for x in (BASE/"replay/frame_diagnostics.json").read_text().splitlines()] if (BASE/"replay/frame_diagnostics.json").suffix==".jsonl" else json.loads((BASE/"replay/frame_diagnostics.json").read_text())
    promotions=[]
    for d in diagnostics:
        ev=d.get("events",[])
        if any(t in ev for t in ("rooms_promoted","room_relinked","rooms_pruned","room_pruned")):
            promotions.append({"frame_id":d.get("frame_id"),"frame_index":d.get("frame_index"),
                "timestamp_s":d.get("timestamp_s"),"events":ev,"node_counts":d.get("node_counts"),
                "m4_branch":d.get("m4_backend_branch"),"m4_stale":d.get("m4_stale")})
    validation={"source_file":str(src),"source_sha256":source_hash,"source_copy_sha256":sha256(raw_dir/"room_polygons.geojson"),
        "opencv_version":cv2.__version__,"protocol_raster_resolution_m":RESOLUTION_M,
        "room_count":len(rooms),"room_ids":[r["id"] for r in rooms],"strict_protocol_validation":"pass" if strict_validation else "fail",
        "strict_protocol_error":strict_error,"continuous_convex_overlaps":exact_overlaps,
        "overlaps_on_protocol_lattice":lattice_overlaps,
        "continuous_overlap_total_m2":sum(x["intersection_area_m2"] for x in exact_overlaps),
        "lattice_overlap_total_m2":len(overlap_cells)*RESOLUTION_M**2,
        "overlap_cells_excluded_from_provisional_scoring":len(overlap_cells),
        "room_1_nonconvex_ring_note":"No exact intersection-area claim is made for room_1; its Y-bounds are disjoint from the three northern room polygons, and protocol-lattice validation found no overlap involving room_1.",
        "room_4_review_note":"Polygon is preserved exactly. This script does not decide whether its footprint includes stair geometry or is a separate single-floor room; see room4_vertical_context.png and request reviewer confirmation.",
        "function_labels_status":"not confirmed; all remain null/uncertain",
        "doorways_adjacency_status":"not confirmed; no adjacency edges inferred",
        "object_room_assignments_status":"not evaluated"}
    run_dir.mkdir(parents=True)
    stage_tri=stage_triangles(STAGE)
    room4=next(f for f in features if f["properties"]["room_id"]=="room_4")
    stage_sections_with_polygon(stage_tri,features,pred_m8,room4,run_dir/"room4_vertical_context.png")
    stage_gt_m8_overlay(stage_tri,features,pred_m8,exact_overlaps,run_dir/"baseline_m8_gt_overlay.png")
    stage_m4_checkpoints(features,temporal,index,BASE,run_dir/"baseline_m4_checkpoints.png")
    write_json(run_dir/"geometry_validation.json",validation)
    write_json(run_dir/"baseline_room_metrics.json",{"status":"provisional_baseline; raw and ROI-clipped metrics reported separately; no tuning performed",
        "capture_id":CAPTURE_ID,"capture_manifest_sha256":sha256(BASE/"capture/capture_manifest.json"),
        "baseline_snapshot_sha256":sha256(BASE/"replay/snapshot.json"),"baseline_delta_sha256":sha256(BASE/"replay/delta_log.json"),
        "builder_received_room_gt":False,"roi":{"rule":"union of four submitted room polygons, globally anchored cell-centre lattice at 0.05m; same ROI clipped onto GT and each prediction",
            "area_before_overlap_exclusion_m2":len(gt_roi)*RESOLUTION_M**2,"area_excluded_overlap_m2":len(overlap_cells)*RESOLUTION_M**2,
            "scored_area_m2":(len(gt_roi)-len(overlap_cells))*RESOLUTION_M**2,"provisional_overlap_exclusion":True},
        "m8_raw_full_polygon_diagnostic_not_roi_clipped":raw_m8,
        "m8_roi_clipped_overlap_excluded":clipped_m8,
        "m4_by_frame":temporal,
        "m5_tracked_region_metrics":{"status":"not_available","reason":"locked replay retains M4 raw regions per frame and final M8 snapshot, but not historical M5 tracked polygon geometry"},
        "m8_temporal_geometry":{"status":"not_available","reason":"frame diagnostics retain promotion/relink/prune events and counts, not each historical RoomNode footprint"},
        "room_promotion_lifecycle_events":promotions,
        "boundary_metrics":None,"boundary_metrics_reason":"outer ROI boundary and overlap topology are not fully resolved; no clipping-created edges counted",
        "adjacency_metrics":None,"semantic_metrics":None,"membership_metrics":None})
    provenance={"kind":"apt0_room_gt_version_provenance","version_id":args.gt_version_id,"source_path":str(src),
        "source_sha256":source_hash,"copied_raw_sha256":sha256(raw_dir/"room_polygons.geojson"),"user_confirmation_date":"2026-10-04",
        "confirmed_scope":"spatial partition represented by the four submitted polygons only",
        "not_confirmed":["functional room labels","doorways/adjacency","object class mapping","object-room assignments","unannotated apartment areas"],
        "raw_submission_unchanged":True,"normalized_geometry_created":False,"topology":"pending overlap review",
        "source_build":"workspace has no Git metadata; locked M3/DuDe binary hashes are in the baseline manifests"}
    provenance_path=version_dir/"provenance.json"
    geometry_path=version_dir/"geometry_validation.json"
    if not provenance_path.exists(): write_json(provenance_path,provenance)
    if not geometry_path.exists(): write_json(geometry_path,validation)
    write_json(run_dir/"baseline_temporal_metrics.json",{"frames":temporal,"room_promotion_lifecycle_events":promotions,
        "m5_and_m8_historical_geometry_status":"not retained in locked artifacts; no reconstructed estimates were used"})
    lock=json.loads(LOCK.read_text())
    write_json(run_dir/"run_manifest.json",{"schema_version":1,"run_id":args.run_id,"status":"provisional_baseline_pending_overlap_and_room4_scope_review",
        "gt_version_id":args.gt_version_id,"gt_source_sha256":source_hash,"input_lock_run_id":lock.get("evaluation_run_id"),
        "capture_id":CAPTURE_ID,"capture_manifest_sha256":sha256(BASE/"capture/capture_manifest.json"),
        "snapshot_sha256":sha256(BASE/"replay/snapshot.json"),"delta_sha256":sha256(BASE/"replay/delta_log.json"),
        "config_sha256":json.loads((BASE/"replay/manifest.json").read_text()).get("config_sha256"),
        "m2_m3_binary_sha256":json.loads((BASE/"replay/manifest.json").read_text()).get("m2_m3_binary_sha256"),
        "m4_binary_sha256":json.loads((BASE/"replay/manifest.json").read_text()).get("dude_binary_sha256"),
        "profile":"source/reference locked baseline; no algorithm/config changes","gt_in_builder":False,
        "outputs":{p.name:sha256(p) for p in run_dir.iterdir() if p.is_file()}})
    report=(f"# apt_0 user-confirmed partition — provisional baseline\n\n"
        f"GT source SHA-256: `{source_hash}`. Raw copy is preserved under `{version_dir.relative_to(ROOT)}/raw_submission/`. "
        "The user confirmed the four-region spatial partition on 2026-10-04 only; functional labels, doorways/adjacency, object-room links, and outside areas remain unconfirmed.\n\n"
        f"## Geometry gate\n\nStrict 5 cm protocol validation: **{'PASS' if strict_validation else 'FAIL'}**. "
        f"Overlap pairs: room_2/room_3 = **{next((x['intersection_area_m2'] for x in exact_overlaps if x['room_a']=='room_2' and x['room_b']=='room_3'),0):.6f} m²**; "
        f"room_3/room_4 = **{next((x['intersection_area_m2'] for x in exact_overlaps if x['room_a']=='room_3' and x['room_b']=='room_4'),0):.6f} m²**. "
        f"Evaluator lattice reports {sum(x['overlap_cells'] for x in lattice_overlaps)} overlapping cells ({len(overlap_cells)*RESOLUTION_M**2:.6f} m²); these cells are excluded only from the explicitly provisional ROI score. "
        "No geometry was normalized. `room_4` stair/single-floor interpretation remains a reviewer question; see `room4_vertical_context.png`.\n\n"
        "## Baseline interpretation\n\nSee `baseline_room_metrics.json` for both raw full-polygon diagnostic and ROI-clipped provisional metrics, and `baseline_temporal_metrics.json` for M4 per-frame scores. "
        "The ROI is the union of the four submitted room footprints, identical for GT and predictions. Boundary scores remain null because external ROI edges are not confirmed room boundaries and historical M5/M8 geometry was not retained. "
        "No candidate change has been tested yet; this is the locked reference score only.\n\n"
        "## Review images\n\n- `baseline_m8_gt_overlay.png`: architecture section, submitted GT, baseline M8 outlines and overlap slivers.\n"
        "- `baseline_m4_checkpoints.png`: M4 raw-region outlines at five sequence checkpoints against the same GT context.\n"
        "- `room4_vertical_context.png`: same room_4 footprint over five architectural height slices.\n")
    (run_dir/"report.vi.md").write_text(report,encoding="utf-8")
    print(json.dumps({"status":"provisional_baseline_pending_review","gt_version":str(version_dir),"run_dir":str(run_dir),
        "strict_validation_error":strict_error,"overlaps":exact_overlaps,"m8_raw":{"matched":raw_m8["matched_count"],
            "macro_iou":raw_m8["macro_iou_matched"],"union_iou":raw_m8["union_iou"],"splits":raw_m8["over_segmentation"],"merges":raw_m8["under_segmentation"]},
        "m8_roi":{"matched":clipped_m8["matched_count"],"macro_iou":clipped_m8["macro_iou_matched"],"union_iou":clipped_m8["union_iou"]}},indent=2,allow_nan=False))


if __name__=="__main__":
    main()
