#!/usr/bin/env python3
"""Build a versioned diagnostic/GT evaluation bundle from existing Habitat artifacts.

Room scores reuse evaluation.metrics. Object GT scoring is not reimplemented here;
attach the output of the locked apt_0 evaluator with --object-evaluation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.metrics import room_partition_metrics, validate_room_annotations  # noqa: E402

PROTOCOL = ROOT / "evaluation/protocol_habitat_v1.json"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def percentile(values, p):
    return float(np.percentile(np.asarray(values, dtype=float), p)) if values else None


def record(value=None, *, unit=None, numerator=None, denominator=None, sample_count=None,
           scope=None, gt_source=None, gt_status="not_applicable", thresholds=None,
           unavailable_reason=None, aggregation=None):
    return {
        "value": value, "unit": unit, "numerator": numerator, "denominator": denominator,
        "sample_count": sample_count, "scope": scope, "gt_source": gt_source,
        "gt_status": gt_status, "thresholds": thresholds or {},
        "unavailable_reason": unavailable_reason, "aggregation": aggregation,
    }


def metric_rows(metrics):
    rows = []
    for group, items in metrics.items():
        for name, item in items.items():
            if isinstance(item, dict) and "value" in item:
                rows.append({"metric_id": f"{group}.{name}", **item})
    return rows


def sha_inputs(paths):
    result = {}
    for label, path in paths.items():
        if path is not None and path.is_file():
            result[label] = {"path": str(path.resolve()), "sha256": sha256(path)}
    return result


def polygon_rooms_from_geojson(path: Path):
    data = read_json(path)
    meta = data.get("metadata", {})
    status = str(meta.get("status", ""))
    if meta.get("coordinate_frame") != "canonical_world_z_up_xy_m" or meta.get("units") != "m":
        raise ValueError("room GT must declare canonical_world_z_up_xy_m and metres")
    if not any(token in status for token in ("human_verified", "user_confirmed")):
        raise ValueError(f"room GT not confirmed; refusing score (metadata.status={status!r})")
    rooms = []
    uncertain = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        feature_status = str(props.get("annotation_status", props.get("review_status", "")))
        row = {"id": str(props.get("room_id", props.get("id", ""))),
               "polygon": feature.get("geometry", {})}
        if any(t in feature_status for t in ("uncertain", "ignored", "provisional")):
            uncertain.append(row["id"])
            continue
        if not row["id"] or row["polygon"].get("type") != "Polygon":
            raise ValueError("room GT feature must have a stable room_id and Polygon geometry")
        rooms.append(row)
    if not rooms:
        raise ValueError("confirmed room GT contains no scorable polygons")
    validation = validate_room_annotations(rooms, resolution_m=0.05)
    return rooms, {"source": str(path.resolve()), "status": status,
                   "coordinate_frame": meta["coordinate_frame"], "units": meta["units"],
                   "room_count_scored": len(rooms), "uncertain_or_ignored_room_ids": uncertain,
                   "validation": validation}


def predicted_rooms_from_snapshot(path: Path):
    data = read_json(path)
    snap = data.get("snapshot", data)
    rooms = []
    for node in snap.get("nodes", []):
        if node.get("kind") != "room":
            continue
        attrs = node.get("attributes", {})
        ring = attrs.get("outer_ring")
        if not ring or len(ring) < 3:
            continue
        closed = [[float(p[0]), float(p[1])] for p in ring]
        if closed[0] != closed[-1]:
            closed.append(closed[0])
        rooms.append({"id": str(node["node_id"]),
                      "polygon": {"type": "Polygon", "coordinates": [closed]}})
    return rooms, snap


def trajectory_length(path: Path):
    if not path.is_file():
        return None, 0
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    points = []
    for row in rows:
        if "x_m" in row and "y_m" in row:
            points.append((float(row["x_m"]), float(row["y_m"])))
    return sum(math.dist(a, b) for a, b in zip(points, points[1:])), len(rows)


def evaluate(args):
    capture = args.capture.resolve()
    replay = args.replay.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty evaluation output: {output}")
    cap_manifest_path = capture / "capture_manifest.json"
    replay_manifest_path = replay / "manifest.json"
    diag_path = replay / "frame_diagnostics.json"
    snapshot_path = replay / "snapshot.json"
    delta_path = replay / "delta_log.json"
    for p in (cap_manifest_path, replay_manifest_path, diag_path, snapshot_path, delta_path):
        if not p.is_file():
            raise FileNotFoundError(p)

    cap = read_json(cap_manifest_path)
    rep = read_json(replay_manifest_path)
    replay_capture_hash = rep.get("capture_manifest_sha256")
    if replay_capture_hash and replay_capture_hash != sha256(cap_manifest_path):
        raise ValueError("replay manifest capture hash does not match the supplied capture")
    diagnostics = read_json(diag_path)
    p1_manifest_path = capture.parent / "detection_2d" / "detection_2d_index.json"
    p1_manifest = read_json(p1_manifest_path) if p1_manifest_path.is_file() else {}
    p3_manifest_path = capture.parent / "detection_3d" / "detection_3d_manifest.json"
    p3_ledger_path = capture.parent / "detection_3d" / "object_evidence_ledger.jsonl"
    p3_manifest = read_json(p3_manifest_path) if p3_manifest_path.is_file() else {}
    p3_ledger_rows = ([json.loads(line) for line in p3_ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                      if p3_ledger_path.is_file() else [])
    snapshot = read_json(snapshot_path).get("snapshot", read_json(snapshot_path))
    if len(diagnostics) > int(cap.get("frame_count", -1)):
        raise ValueError("processed diagnostics exceed capture frame count")
    input_frames = len(cap.get("frames", []))
    if input_frames != int(cap.get("frame_count", -1)):
        raise ValueError("capture manifest frame_count mismatch")
    frame_map = {str(x["frame_id"]): x for x in cap["frames"]}
    if len(frame_map) != input_frames:
        raise ValueError("capture manifest contains duplicate frame IDs")
    if any(str(x.get("frame_id")) not in frame_map for x in diagnostics):
        raise ValueError("replay diagnostics contain frames outside capture")
    diagnostic_ids = [str(x.get("frame_id")) for x in diagnostics]
    if len(set(diagnostic_ids)) != len(diagnostic_ids):
        raise ValueError("replay diagnostics contain duplicate frame IDs")
    missing_diagnostic_ids = sorted(set(frame_map) - set(diagnostic_ids))

    mode = "diagnostic_only"
    metric = {}
    failures = []
    valid_pixels = valid_total = 0
    valid_fraction_per_frame = []
    capture_times = [float(x["timestamp_s"]) for x in cap["frames"]]
    capture_gaps = [b-a for a,b in zip(capture_times,capture_times[1:])]
    for entry in cap["frames"]:
        mask_path = capture / entry["frame_dir"] / "valid_mask.npy"
        if not mask_path.is_file():
            continue
        mask = np.load(mask_path, mmap_mode="r", allow_pickle=False).astype(bool)
        count = int(mask.sum()); size = int(mask.size)
        valid_pixels += count; valid_total += size
        valid_fraction_per_frame.append(count / size if size else 0.0)

    stage_values = {key: [] for key in ("m1_runtime_ms","m2_runtime_ms","m3_runtime_ms","update_runtime_ms")}
    m4_rejected = m4_stale = stage_failed_frames = 0
    failed_frame_ids = set()
    free_cells = occupied_cells = unknown_cells = None
    map_counts = []
    for row in diagnostics:
        mapping = row.get("native_mapping", {})
        for key in ("m1_runtime_ms","m2_runtime_ms","m3_runtime_ms"):
            value = mapping.get(key)
            if isinstance(value, (int,float)) and math.isfinite(value):
                stage_values[key].append(float(value))
        value = row.get("update_runtime_ms")
        if isinstance(value, (int,float)) and math.isfinite(value):
            stage_values["update_runtime_ms"].append(float(value))
        if row.get("m4_guard_accepted") is False or row.get("m4_backend_status") in {"rejected","error"}:
            m4_rejected += 1
            failures.append({"frame_id":row.get("frame_id"),"frame_index":row.get("frame_index"),
                             "failure":"m4_rejected","reason":row.get("m4_reason"),
                             "backend_status":row.get("m4_backend_status")})
        if row.get("m4_stale") is True:
            m4_stale += 1
        status = row.get("stage_status", {})
        if any(str(v).lower().startswith(("failed","error")) for v in status.values()):
            stage_failed_frames += 1
            failed_frame_ids.add(str(row.get("frame_id")))
            failures.append({"frame_id":row.get("frame_id"),"frame_index":row.get("frame_index"),
                             "failure":"stage_failure","stage_status":status})
    failed_frames = len(missing_diagnostic_ids) + len(failed_frame_ids)
    for frame_id in missing_diagnostic_ids:
        failures.append({"frame_id": frame_id, "failure":"not_processed", "reason":"capture frame has no replay diagnostic"})
        nm = row.get("native_mapping", {})
        counts = [nm.get("m3_free_cell_count"),nm.get("m3_occupied_cell_count"),nm.get("m3_unknown_cell_count")]
        if all(isinstance(x,(int,float)) for x in counts):
            map_counts.append([int(x) for x in counts])
    if map_counts:
        free_cells, occupied_cells, unknown_cells = map_counts[-1]
    revs=[int(x["graph_revision"]) for x in diagnostics if x.get("graph_revision") is not None]
    times=[float(x["timestamp_s"]) for x in diagnostics if x.get("timestamp_s") is not None]
    revision_time_violations=sum(1 for a,b in zip(revs,revs[1:]) if b<a)+sum(1 for a,b in zip(times,times[1:]) if b<=a)
    grid_info = None
    grid_trace_hashes = {}
    if args.grid_trace:
        trace = args.grid_trace.resolve()
        index_path = trace / "index.json"
        index = read_json(index_path)
        if index.get("capture_dir") and Path(index["capture_dir"]).resolve() != capture:
            raise ValueError("M3 grid trace belongs to a different capture directory")
        entries = index.get("frames", [])
        if not entries:
            raise ValueError("grid trace contains no frames")
        last = entries[-1]
        metadata_path = trace / last["metadata_file"]
        array_path = trace / last["array_file"]
        meta = read_json(metadata_path)
        with np.load(array_path, allow_pickle=False) as arrays:
            values = arrays["values"]
            # M3's serialized FreeSpaceGrid2D convention: <0 unknown, 0 free,
            # >0 occupied. These are categorical score semantics, not probabilities.
            unknown_cells = int(np.count_nonzero(values < 0))
            free_cells = int(np.count_nonzero(values == 0))
            occupied_cells = int(np.count_nonzero(values > 0))
            grid_shape = list(values.shape)
        grid_info = {"frame_count":len(entries),"map_revision":last.get("map_revision"),
                     "simulation_timestamp_s":last.get("simulation_timestamp_s"),
                     "origin_xy":meta.get("origin_xy"),"resolution_m":meta.get("resolution_m"),
                     "shape":grid_shape,"value_semantics":"negative=unknown, zero=free, positive=occupied; not probability"}
        grid_trace_hashes = {"grid_trace_index": index_path, "final_grid_metadata": metadata_path,
                             "final_grid_values": array_path}
    tlen, trajectory_rows = trajectory_length(replay / "trajectory.csv")
    pose_points = []
    if (replay/"trajectory.csv").is_file():
        with (replay/"trajectory.csv").open(newline="",encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if "x_m" in row and "y_m" in row:
                    pose_points.append((float(row["x_m"]),float(row["y_m"])))
    unique_pose_count = len(set(pose_points))
    frame_ms = stage_values["update_runtime_ms"]
    runtime = {}
    for key, vals in stage_values.items():
        runtime[key] = {
            "median_ms": percentile(vals,50), "p95_ms": percentile(vals,95),
            "max_ms": max(vals) if vals else None, "sample_count":len(vals),
            "first_frame_ms": vals[0] if vals else None,
            "steady_state_median_ms": percentile(vals[1:],50) if len(vals)>1 else None,
            "steady_state_p95_ms": percentile(vals[1:],95) if len(vals)>1 else None,
        }

    grid_resolution_m = (grid_info or {}).get("resolution_m")
    if grid_resolution_m is None:
        grid_resolution_m = rep.get("m4_effective_parameters", {}).get("grid_resolution_m")
    cell_area_m2 = float(grid_resolution_m) ** 2 if grid_resolution_m is not None else None
    op = {
        "frames":{
            "input":record(input_frames,unit="frames",numerator=input_frames,denominator=input_frames,sample_count=input_frames,scope="capture sequence"),
            "processed":record(len(diagnostics),unit="frames",numerator=len(diagnostics),denominator=input_frames,sample_count=input_frames,scope="replay diagnostics"),
            "failed":record(failed_frames,unit="frames",numerator=failed_frames,denominator=input_frames,sample_count=input_frames,scope="input capture frames; missing diagnostics plus frames with stage errors"),
            "m4_rejected":record(m4_rejected,unit="frames",numerator=m4_rejected,denominator=len(diagnostics),sample_count=len(diagnostics),scope="processed frames",thresholds={"decision":"m4_guard_accepted=false or backend rejected/error"}),
            "m4_rejected_rate":record(m4_rejected/len(diagnostics) if diagnostics else None,unit="fraction",numerator=m4_rejected,denominator=len(diagnostics),sample_count=len(diagnostics),scope="processed frames"),
            "region_update_stale":record(m4_stale,unit="frames",numerator=m4_stale,denominator=len(diagnostics),sample_count=len(diagnostics),scope="processed frames",thresholds={"source_field":"m4_stale"}),
            "m5_stale_updates":record(None,unit="frames",scope="processed frames",unavailable_reason="current diagnostics do not expose an independent M5 stale counter; m4_stale is not relabeled as M5"),
        },
        "replay_checks":{
            "snapshot_reload_equal":record(rep.get("snapshot_reload_equal"),unit="boolean",scope="run",unavailable_reason=None if isinstance(rep.get("snapshot_reload_equal"),bool) else "not recorded"),
            "delta_replay_equal":record(rep.get("delta_replay_equal"),unit="boolean",scope="run",unavailable_reason=None if isinstance(rep.get("delta_replay_equal"),bool) else "not recorded"),
            "fresh_process_equal":record(rep.get("fresh_replay_equal"),unit="boolean",scope="run",unavailable_reason=None if isinstance(rep.get("fresh_replay_equal"),bool) else "fresh-process check not run"),
        },
        "runtime":{
            "simulation_capture_interval":record({"median":percentile(capture_gaps,50),"p95":percentile(capture_gaps,95),"max":max(capture_gaps) if capture_gaps else None},unit="simulation_seconds",sample_count=len(capture_gaps),scope="between consecutive captured timestamps; not wall-clock capture cadence"),
            "stage_runtime":record(runtime,unit="milliseconds",sample_count={k:v["sample_count"] for k,v in runtime.items()},scope="per processed frame; host monotonic durations from replay diagnostics",gt_status="not_applicable",unavailable_reason="M4, M5, graph update are not separately timed in current runner"),
            "runner_total_wall_time":record(rep.get("runtime_ms"),unit="milliseconds",scope="runner; current manifest timing may include fresh replay and output work",unavailable_reason=None if rep.get("runtime_ms") is not None else "not recorded"),
            "export_render_runtime":record(None,unit="milliseconds",scope="run",unavailable_reason="current replay runner does not separately instrument export/render"),
            "model_load_and_warmup":record(None,unit="milliseconds",scope="model process",unavailable_reason="not recorded by current saved-cache replay"),
            "memory":record(rep.get("python_process_ru_maxrss_kib"),unit="KiB",scope="Python replay host process",unavailable_reason=None if rep.get("python_process_ru_maxrss_kib") else "not recorded",thresholds={"method":"resource.getrusage(RUSAGE_SELF).ru_maxrss; not total simultaneous peak RSS of child workers"}),
        },
        "observation":{
            "capture_frame_count":record(input_frames,unit="frames",sample_count=input_frames,scope="sequence"),
            "trajectory_length":record(tlen,unit="m",sample_count=max(0,len(pose_points)-1),scope="GT-pose sampled observation trajectory; polyline in canonical XY"),
            "trajectory_pose_count":record(trajectory_rows,unit="poses",sample_count=trajectory_rows,scope="trajectory.csv rows"),
            "unique_xy_pose_count":record(unique_pose_count,unit="poses",sample_count=len(pose_points),scope="distinct exact XY poses; yaw-only views may share position"),
            "valid_depth_fraction":record(valid_pixels/valid_total if valid_total else None,unit="fraction",numerator=valid_pixels,denominator=valid_total,sample_count=len(valid_fraction_per_frame),scope="pixels across available capture valid_mask arrays",unavailable_reason=None if valid_total else "valid_mask arrays unavailable"),
            "valid_depth_fraction_per_frame":{"value":valid_fraction_per_frame,"unit":"fraction","sample_count":len(valid_fraction_per_frame),"scope":"capture order"},
            "m3_final_free_area":record(None if free_cells is None or cell_area_m2 is None else free_cells*cell_area_m2,unit="m2",numerator=free_cells,denominator=None,sample_count=1,scope="final accumulated M3 grid; current grid cell-count semantics; not a probability",unavailable_reason=None if free_cells is None or cell_area_m2 is not None else "grid resolution unavailable"),
            "m3_final_occupied_area":record(None if occupied_cells is None or cell_area_m2 is None else occupied_cells*cell_area_m2,unit="m2",numerator=occupied_cells,denominator=None,sample_count=1,scope="final accumulated M3 grid; current grid cell-count semantics; not a probability",unavailable_reason=None if occupied_cells is None or cell_area_m2 is not None else "grid resolution unavailable"),
            "m3_final_unknown_area":record(None if unknown_cells is None or cell_area_m2 is None else unknown_cells*cell_area_m2,unit="m2",numerator=unknown_cells,denominator=None,sample_count=1,scope="final M3 grid extent; unknown count is in current published grid",unavailable_reason=None if unknown_cells is None or cell_area_m2 is not None else "grid resolution unavailable"),
            "m3_grid_provenance":record(grid_info,unit=None,sample_count=grid_info["frame_count"] if grid_info else None,scope="lossless exact pipeline grid trace",unavailable_reason=None if grid_info else "grid trace not supplied; using last-frame mapping diagnostics"),
            "observed_area_ratio":record(None,unit="fraction",scope="scene ROI",unavailable_reason="no independent scene ROI denominator supplied"),
            "route_type":record(cap.get("route_type"),unit=None,scope="capture provenance",thresholds={"motion_model":cap.get("motion_model")}),
        },
    }
    metric["operations"] = op
    gt_meta = None
    room_result = None
    if args.room_gt:
        gt_rooms, gt_meta = polygon_rooms_from_geojson(args.room_gt.resolve())
        pred_rooms, snap = predicted_rooms_from_snapshot(snapshot_path)
        result = room_partition_metrics(gt_rooms,pred_rooms,resolution_m=0.05,match_iou_threshold=0.10,
                                        significant_overlap_thresholds=(0.01,0.05,0.10))
        mode = "gt_evaluation"
        room_result = {"protocol":"room_partition_metrics","gt":gt_meta,"metrics":result}
        gt_record = {"source":gt_meta["source"],"status":gt_meta["status"]}
        gt_status = gt_meta["status"]
        room_metrics = {
            "macro_iou_all_gt_rooms":record(result["macro_iou_all_gt_rooms"],unit="IoU",numerator=sum(r["iou"] for r in result["per_gt_room"]),denominator=result["gt_room_count"],sample_count=result["gt_room_count"],scope="all GT rooms; unmatched GT contributes zero",gt_source=gt_record,gt_status=gt_status,thresholds={"raster_resolution_m":0.05,"match_iou":0.10}),
            "union_iou":record(result["union_iou"],unit="IoU",scope="fixed GT-union ROI",gt_source=gt_record,gt_status=gt_status),
            "extra_predictions_in_roi":record(len(result["extra_predicted_room_ids_inside_roi"]),unit="rooms",numerator=len(result["extra_predicted_room_ids_inside_roi"]),denominator=result["predicted_room_count_intersecting_roi"],sample_count=result["predicted_room_count_intersecting_roi"],scope="predictions intersecting GT union ROI",gt_source=gt_record,gt_status=gt_status),
            "missed_gt_rooms":record(len(result["missed_gt_room_ids"]),unit="rooms",numerator=len(result["missed_gt_room_ids"]),denominator=result["gt_room_count"],sample_count=result["gt_room_count"],scope="GT rooms",gt_source=gt_record,gt_status=gt_status),
        }
        metric["room"] = room_metrics
        metric["room_details"] = {"value":result,"unit":None,"sample_count":result["gt_room_count"],"scope":"full room partition","gt_source":gt_record,"gt_status":gt_status,"thresholds":{"split_merge_directional_fraction":[0.01,0.05,0.10]}}
    else:
        reason = "no confirmed independent room polygon GT supplied"
        metric["room"] = {k:record(None,unit=u,scope=s,unavailable_reason=reason,gt_status="unavailable") for k,u,s in [
            ("macro_iou_all_gt_rooms","IoU","all GT rooms"),("union_iou","IoU","GT union ROI"),
            ("extra_predictions_in_roi","rooms","GT ROI"),("missed_gt_rooms","rooms","GT rooms")]}
    metric["object_detection"] = {
        k:record(None,unit=u,scope=s,unavailable_reason="no compatible confirmed object-mask GT/evaluator supplied",gt_status="unavailable")
        for k,u,s in [("precision","fraction","per-frame masks"),("recall","fraction","per-frame masks"),("f1","fraction","per-frame masks"),("mean_matched_mask_iou","IoU","one-to-one mask matches")]
    }
    object_eval = None
    if args.object_evaluation:
        source = read_json(args.object_evaluation.resolve())
        if source.get("capture_id") and source["capture_id"] not in {capture.name, capture.parent.name}:
            raise ValueError("object-evaluation metrics belong to a different capture; refusing to attach them")
        row = source.get("object_detection",{}).get("class_agnostic_visible_mask",{})
        if row:
            object_eval = {"source_metrics_json":str(args.object_evaluation.resolve()),"source_kind":source.get("kind"),"scope":row.get("scope")}
            tp,fp,fn = row.get("tp"),row.get("fp"),row.get("fn")
            prec = tp/(tp+fp) if tp is not None and tp+fp else None
            rec = tp/(tp+fn) if tp is not None and tp+fn else None
            matched_ious = [float(x["mask_iou"]) for x in row.get("matched", []) if x.get("mask_iou") is not None]
            metric["object_detection"]={
                "precision":record(prec,unit="fraction",numerator=tp,denominator=(tp+fp) if tp is not None and fp is not None else None,sample_count=(tp+fp) if tp is not None and fp is not None else None,scope=row.get("evaluation_unit") or row.get("matching_scope"),gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks",thresholds={"mask_iou":0.50,"minimum_visible_pixels":64}),
                "recall":record(rec,unit="fraction",numerator=tp,denominator=(tp+fn) if tp is not None and fn is not None else None,sample_count=(tp+fn) if tp is not None and fn is not None else None,scope=row.get("evaluation_unit") or row.get("matching_scope"),gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks",thresholds={"mask_iou":0.50,"minimum_visible_pixels":64}),
                "f1":record(2*prec*rec/(prec+rec) if prec is not None and rec is not None and prec+rec else None,unit="fraction",scope=row.get("evaluation_unit") or row.get("matching_scope"),gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks"),
                "mean_matched_mask_iou":record(sum(matched_ious)/len(matched_ious) if matched_ious else None,unit="IoU",sample_count=len(matched_ious),scope=row.get("matching_scope"),gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks",unavailable_reason=None if matched_ious else "no eligible mask matches"),
                "tp":record(tp,unit="observations",sample_count=tp,scope=row.get("evaluation_unit") or row.get("matching_scope"),gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks"),
                "fp":record(fp,unit="observations",sample_count=fp,scope=row.get("evaluation_unit") or row.get("matching_scope"),gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks"),
                "fn":record(fn,unit="observations",sample_count=fn,scope=row.get("evaluation_unit") or row.get("matching_scope"),gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks"),
                "supported_vocabulary_metrics":record(None,unit="class-aware detection metrics",scope="human-confirmed detector vocabulary",unavailable_reason=source.get("object_detection",{}).get("class_aware_reason") or "mapping not human-confirmed",gt_status="unavailable")
            }
            p3_source=source.get("p3_filtering",{})
            decisions=p3_source.get("decisions",{})
            det_count=int(p3_source.get("ledger_detection_count",0))
            accepted=int(decisions.get("accepted",0)); rejected=int(decisions.get("rejected",0))
            metric["p3_and_association"]={
                "p3_decision_ledger":record({"accepted":accepted,"rejected":rejected,
                    "mask_matched_kept":p3_source.get("mask_lineage_by_filter_decision",{}).get("counts",{}).get("mask_matched_kept"),
                    "mask_matched_rejected":p3_source.get("mask_lineage_by_filter_decision",{}).get("counts",{}).get("mask_matched_rejected"),
                    "unmatched_kept":p3_source.get("mask_lineage_by_filter_decision",{}).get("counts",{}).get("unmatched_kept"),
                    "unmatched_rejected":p3_source.get("mask_lineage_by_filter_decision",{}).get("counts",{}).get("unmatched_rejected")},
                    unit="detections",numerator=det_count,denominator=det_count,sample_count=det_count,
                    scope="P3 filter decisions cross-tabbed against independent 2D visible-mask lineage; not detection accuracy"),
                "p3_acceptance_rate":record(accepted/det_count if det_count else None,unit="fraction",numerator=accepted,denominator=det_count,sample_count=det_count,scope="P3 filter only; not precision/recall"),
                "association_fragmentation":record(source.get("association",{}).get("fragmented_count"),unit="GT instances",sample_count=source.get("association",{}).get("gt_instance_count_observed"),scope="accepted observations with independent mask-matched identity lineage",gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks"),
                "association_false_merge":record(source.get("association",{}).get("merged_track_count"),unit="predicted tracks",sample_count=source.get("association",{}).get("gt_instance_count_observed"),scope="accepted observations with independent mask-matched identity lineage",gt_source=object_eval,gt_status="simulator_reference_visible_instance_masks"),
            }
            lineage_rows=p3_source.get("mask_lineage_by_filter_decision",{}).get("per_detection",[])
            lineage_by_id={str(r.get("detection_id")):r for r in lineage_rows}
            metric["p3_and_association"]["detection_ledger"] = record([
                {"frame_id":r.get("frame_id"),"frame_index":r.get("frame_index"),
                 "detection_id":r.get("detection_id"),"class":r.get("class_canonical"),
                 "decision":r.get("decision"),"reason":r.get("reason"),
                 "mask_pixels":r.get("mask_pixel_count"),"valid_depth_pixels":r.get("valid_depth_pixels"),
                 "valid_depth_fraction":r.get("valid_depth_fraction"),"cluster_count":r.get("cluster_count"),
                 "largest_cluster_points":r.get("largest_cluster_points"),
                 "independent_mask_lineage":lineage_by_id.get(str(r.get("detection_id")),{}).get("independent_mask_lineage")}
                for r in p3_ledger_rows], unit="detections", sample_count=len(p3_ledger_rows),
                scope="P3 raw ledger with independent 2D mask lineage where available")
            failures.extend({"failure":"p3_rejected_detection","detection_id":r.get("detection_id"),
                             "independent_mask_lineage":r.get("independent_mask_lineage"),
                             "triage_only":True,"evidence_bundle":str(args.object_evaluation.resolve().parent)}
                            for r in lineage_rows if r.get("p3_decision")=="rejected")
            mode = "gt_evaluation"
    semantic_file = None
    semantic_file_kind = None
    if args.semantic:
        semantic_root = args.semantic.resolve()
        if (semantic_root / "region_labels.json").is_file():
            semantic_file = semantic_root / "region_labels.json"
            semantic_file_kind = "region_labels_json"
        elif (semantic_root / "room_semantics.jsonl").is_file():
            semantic_file = semantic_root / "room_semantics.jsonl"
            semantic_file_kind = "room_semantics_jsonl"
    semantic_manifest_path = args.semantic.resolve() / "manifest.json" if args.semantic else None
    semantic_manifest = read_json(semantic_manifest_path) if semantic_manifest_path and semantic_manifest_path.is_file() else {}
    semantic_counts = {}
    semantic_rows=[]
    if semantic_file and semantic_file.is_file():
        if semantic_file_kind == "room_semantics_jsonl":
            sem_rows = [json.loads(line) for line in semantic_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            sem_rows = read_json(semantic_file)
        semantic_rows=sem_rows
        semantic_counts = {status:sum(1 for row in sem_rows if row.get("status")==status) for status in sorted({row.get("status") for row in sem_rows})}
    membership_by_entity={}
    for edge in snapshot.get("edges",[]):
        if edge.get("relation")=="room_contains":
            membership_by_entity.setdefault(str(edge.get("target_id")),[]).append(str(edge.get("source_id")))
    object_nodes=[n for n in snapshot.get("nodes",[]) if n.get("kind")=="object"]
    membership_rows=[]
    for node in object_nodes:
        oid=str(node.get("node_id")); owners_for=sorted(set(membership_by_entity.get(oid,[])))
        membership_rows.append({"object_id":oid,"class_name":node.get("attributes",{}).get("class_name"),
            "room_ids":owners_for,"status":"unassigned" if not owners_for else ("ambiguous_multiple_owners" if len(owners_for)>1 else "assigned")})
    metric["semantics"] = {
        "room_label_prediction_counts":record(semantic_counts,unit="regions",sample_count=sum(semantic_counts.values()),scope="predicted sidecar label states; not accuracy"),
        "predicted_label_coverage":record(sum(n for s,n in semantic_counts.items() if s not in {"unknown","ambiguous","stale"})/sum(semantic_counts.values()) if semantic_counts else None,unit="fraction",numerator=sum(n for s,n in semantic_counts.items() if s not in {"unknown","ambiguous","stale"}),denominator=sum(semantic_counts.values()),sample_count=sum(semantic_counts.values()),scope="sidecar room-region outputs; output coverage only"),
        "unknown_rate":record(semantic_counts.get("unknown",0)/sum(semantic_counts.values()) if semantic_counts else None,unit="fraction",numerator=semantic_counts.get("unknown",0),denominator=sum(semantic_counts.values()),sample_count=sum(semantic_counts.values()),scope="sidecar outputs, not accuracy"),
        "ambiguous_rate":record(semantic_counts.get("ambiguous",0)/sum(semantic_counts.values()) if semantic_counts else None,unit="fraction",numerator=semantic_counts.get("ambiguous",0),denominator=sum(semantic_counts.values()),sample_count=sum(semantic_counts.values()),scope="sidecar outputs, not accuracy"),
        "stale_rate":record(semantic_counts.get("stale",0)/sum(semantic_counts.values()) if semantic_counts else None,unit="fraction",numerator=semantic_counts.get("stale",0),denominator=sum(semantic_counts.values()),sample_count=sum(semantic_counts.values()),scope="sidecar outputs, not accuracy"),
        "accuracy":record(None,unit="fraction",scope="confirmed functional room GT",unavailable_reason="no compatible confirmed functional-label GT supplied; apt_0 functional labels remain uncertain",gt_status="unavailable"),
        "room_label_rows":record(semantic_rows,unit="rows",sample_count=len(semantic_rows),scope="region→room labeler evidence; predictor output only")
    }
    metric["membership"]={
        "predicted_object_room_assignments":record(membership_rows,unit="objects",sample_count=len(membership_rows),scope="snapshot room_contains edges; prediction-only"),
        "conditional_accuracy":record(None,unit="fraction",scope="GT-identity-matched eligible objects",unavailable_reason="independent object→room assignment GT was not supplied",gt_status="unavailable"),
        "unassigned_count":record(sum(1 for r in membership_rows if r["status"]=="unassigned"),unit="objects",sample_count=len(membership_rows),scope="predicted object nodes"),
        "ambiguous_multiple_owner_count":record(sum(1 for r in membership_rows if r["status"]=="ambiguous_multiple_owners"),unit="objects",sample_count=len(membership_rows),scope="structural ownership diagnostic")
    }
    metric["adjacency"]={"precision":record(None,unit="fraction",scope="confirmed doorway adjacency",unavailable_reason="no confirmed doorway/adjacency GT supplied",gt_status="unavailable"),
                          "recall":record(None,unit="fraction",scope="confirmed doorway adjacency",unavailable_reason="no confirmed doorway/adjacency GT supplied",gt_status="unavailable")}
    nodes={str(n.get("node_id")) for n in snapshot.get("nodes",[])}
    dangling=sum(1 for e in snapshot.get("edges",[]) if e.get("source_id") not in nodes or e.get("target_id") not in nodes)
    owners={}
    for edge in snapshot.get("edges",[]):
        if edge.get("relation")=="room_contains":
            owners.setdefault(str(edge.get("target_id")),set()).add(str(edge.get("source_id")))
    multiple_room_owners={key:sorted(value) for key,value in owners.items() if len(value)>1}
    rooms={str(n.get("node_id")) for n in snapshot.get("nodes",[]) if n.get("kind")=="room"}
    if semantic_file and semantic_file.is_file():
        semantic_for_invariants = (sem_rows if semantic_file_kind == "room_semantics_jsonl" else read_json(semantic_file))
    else:
        semantic_for_invariants = []
    sem_orphans=sum(1 for r in semantic_for_invariants if str(r.get("room_id")) not in rooms)
    metric["graph_invariants"]={
        "dangling_edges":record(dangling,unit="edges",numerator=dangling,denominator=len(snapshot.get("edges",[])),sample_count=len(snapshot.get("edges",[])),scope="snapshot graph"),
        "multiple_direct_room_owners":record(len(multiple_room_owners),unit="entities",numerator=len(multiple_room_owners),denominator=len(owners),sample_count=len(owners),scope="room_contains targets; structural contract"),
        "orphan_semantic_rows":record(sem_orphans,unit="rows",numerator=sem_orphans,denominator=sum(semantic_counts.values()),sample_count=sum(semantic_counts.values()),scope="semantic sidecar vs final room IDs"),
        "revision_or_timestamp_order_violations":record(revision_time_violations,unit="events",numerator=revision_time_violations,denominator=max(0,len(diagnostics)-1),sample_count=len(diagnostics),scope="frame diagnostics in capture order")
    }
    if "p3_and_association" not in metric:
        p3_counts = {name:sum(1 for row in p3_ledger_rows if row.get("decision")==name)
                     for name in ("accepted", "rejected")}
        p3_count = len(p3_ledger_rows)
        metric["p3_and_association"]={
            "p3_decision_ledger":record(p3_counts if p3_ledger_rows else None,unit="detections",
                numerator=p3_count,denominator=p3_count,sample_count=p3_count,
                scope="P3 filter decisions; acceptance is not accuracy",
                unavailable_reason=None if p3_ledger_rows else "P3 evidence ledger not supplied"),
            "p3_effective_parameters":record({key:p3_manifest.get(key) for key in (
                "effective_sampling_stride","effective_cluster_eps_m","effective_cluster_min_points")}
                if p3_manifest else None,unit="profile parameters",scope="P3 manifest",
                unavailable_reason=None if p3_manifest else "P3 manifest not supplied"),
            "detection_ledger":record([{"frame_id":r.get("frame_id"),"frame_index":r.get("frame_index"),
                "detection_id":r.get("detection_id"),"class":r.get("class_canonical"),
                "decision":r.get("decision"),"reason":r.get("reason"),
                "mask_pixels":r.get("mask_pixel_count"),"valid_depth_pixels":r.get("valid_depth_pixels"),
                "valid_depth_fraction":r.get("valid_depth_fraction"),"cluster_count":r.get("cluster_count"),
                "largest_cluster_points":r.get("largest_cluster_points")} for r in p3_ledger_rows]
                if p3_ledger_rows else None,unit="detections",sample_count=p3_count,
                scope="P3 raw evidence by class, mask size, depth and cluster; not an accuracy metric",
                unavailable_reason=None if p3_ledger_rows else "P3 evidence ledger not supplied"),
            "association_fragmentation":record(None,unit="GT instances",scope="matched object lineages",unavailable_reason="requires confirmed object-identity lineage"),
            "association_false_merge":record(None,unit="predicted tracks",scope="matched object lineages",unavailable_reason="requires confirmed object-identity lineage"),
        }
    metric["room_partition_details"] = metric.get("room_details",record(None,unit=None,scope="room partition",unavailable_reason="no room GT"))
    route_config = read_json(args.run_config.resolve()) if args.run_config else {}
    evaluation_profile_path = getattr(args, "evaluation_profile", None)
    evaluation_profile = read_json(evaluation_profile_path.resolve()) if evaluation_profile_path else {}
    if route_config:
        if cap.get("scene_id") and route_config.get("scene_id") and cap["scene_id"] != route_config["scene_id"]:
            raise ValueError("run config scene_id differs from the capture manifest")
        if cap.get("sequence_id") and route_config.get("sequence_id") and cap["sequence_id"] != route_config["sequence_id"]:
            raise ValueError("run config sequence_id differs from the capture manifest")
        if cap.get("resolution_px") and route_config.get("resolution_px") and list(cap["resolution_px"]) != list(route_config["resolution_px"]):
            raise ValueError("run config resolution differs from the capture manifest")
        if cap.get("horizontal_fov_deg") and route_config.get("horizontal_fov_deg") and not math.isclose(float(cap["horizontal_fov_deg"]), float(route_config["horizontal_fov_deg"]), abs_tol=1e-6):
            raise ValueError("run config horizontal FOV differs from the capture manifest")
        if cap.get("dataset_type") and route_config.get("dataset_type") and cap["dataset_type"] != route_config["dataset_type"]:
            raise ValueError("run config dataset type differs from the capture manifest")
    inferred_dataset=cap.get("dataset_type") or ("replica_cad" if "replica" in str(route_config.get("scene_provenance","")).lower() else None)
    inferred_version=cap.get("dataset_version") or ("v1.6" if inferred_dataset=="replica_cad" and "v1.6" in str(route_config.get("scene_provenance","")) else None)
    if evaluation_profile:
        if evaluation_profile.get("protocol_id") != "habitat_scene_eval_v1":
            raise ValueError("evaluation profile uses a different protocol")
        profile_dataset = evaluation_profile.get("dataset", {})
        summary_scene_id = cap.get("scene_id") or route_config.get("scene_id")
        if profile_dataset.get("scene_id") and summary_scene_id:
            if profile_dataset["scene_id"] != summary_scene_id:
                raise ValueError("evaluation profile scene_id differs from capture/config")
        if profile_dataset.get("type") and inferred_dataset and profile_dataset["type"] != inferred_dataset:
            raise ValueError("evaluation profile dataset type differs from capture/config")
        expected_room_gt = evaluation_profile.get("ground_truth", {}).get("room_polygons")
        expected_room_path = (ROOT / expected_room_gt).resolve() if expected_room_gt else None
        if expected_room_gt and args.room_gt and args.room_gt.resolve() != expected_room_path:
            raise ValueError("room GT input differs from evaluation profile")
        if expected_room_gt and not args.room_gt:
            raise ValueError("evaluation profile declares room GT but --room-gt was not supplied")
        if evaluation_profile.get("workflow_mode") == "diagnostic_only" and (args.room_gt or args.object_evaluation):
            raise ValueError("diagnostic_only profile cannot be combined with scored GT inputs")
    summary = {
        "evaluation_run_id":args.run_id or output.name,
        "scene_id":cap.get("scene_id") or route_config.get("scene_id"),"dataset_type":inferred_dataset,"dataset_version":inferred_version,
        "sequence_id":cap.get("sequence_id"),"evaluation_mode":mode,
        "frame_count_input":input_frames,"frame_count_processed":len(diagnostics),
        "valid_depth_fraction":op["observation"]["valid_depth_fraction"]["value"],
        "trajectory_length_m":tlen,"room_macro_iou":metric["room"]["macro_iou_all_gt_rooms"]["value"],
        "object_mask_precision":metric["object_detection"]["precision"]["value"],
        "object_mask_recall":metric["object_detection"]["recall"]["value"],
        "m4_rejected":m4_rejected,"region_update_stale":m4_stale,
        "end_to_end_update_p50_ms":runtime["update_runtime_ms"]["median_ms"],
        "end_to_end_update_p95_ms":runtime["update_runtime_ms"]["p95_ms"],
        "gt_scope_id":gt_meta["status"] if gt_meta else "none",
        "protocol_id":"habitat_scene_eval_v1",
    }
    metrics_doc = {
        "schema_version":1,"protocol_id":"habitat_scene_eval_v1","evaluation_mode":mode,
        "run_summary":summary,"metrics":metric,"room_evaluation":room_result,
        "semantic_label_state_counts":semantic_counts,
        "limitations":[
            "GT-pose sampled observation route, not physical robot locomotion, autonomous navigation, SLAM, or realtime performance.",
            "No observed-area ratio without independently defined ROI.",
            "Memory is host Python RUSAGE_SELF only; not aggregate simultaneous peak RSS of child workers.",
            "Current runner has no separate M4/M5/graph, model warm-up, export/render or live-latency timings.",
            "Object mask metrics only attach when produced by the existing locked object evaluator; this script does not implement a competing mask matcher."
        ],
    }
    metric["run_metadata"]={
        "scene_id":cap.get("scene_id") or route_config.get("scene_id"),"dataset_type":inferred_dataset,"dataset_version":inferred_version,
        "sequence_id":cap.get("sequence_id"),"route_type":cap.get("route_type"),
        "resolution_px":cap.get("resolution_px"),"horizontal_fov_deg":cap.get("horizontal_fov_deg"),
        "habitat_sim_version":cap.get("habitat_sim_version"),"python_version":platform.python_version(),
        "platform":platform.platform(),"cpu_identifier":platform.processor() or platform.machine(),
        "gpu_device_id":cap.get("gpu_device_id",route_config.get("gpu_device_id")),
        "sensor_extrinsics":cap.get("sensor_extrinsics"),"route_pose_source":cap.get("motion_model"),
        "capture_runtime_metadata":cap.get("route_policy"),
        "route_config_path":str(args.run_config.resolve()) if args.run_config else None,
        "route_config_sha256":sha256(args.run_config.resolve()) if args.run_config else None,
        "evaluation_profile_id":evaluation_profile.get("profile_id"),
        "evaluation_profile_sha256":sha256(evaluation_profile_path.resolve()) if evaluation_profile_path else None,
        "configured_route_pose_count":len(route_config.get("route",[])) if isinstance(route_config.get("route",[]),list) else None,
        "semantic_ground_truth_saved":cap.get("semantic_gt_saved"),"semantic_sidecar_format":semantic_file_kind,
        "route_requires_navigability":route_config.get("require_navigable"),
        "detector_weights_sha256":semantic_manifest.get("detector_weights_sha256") or p1_manifest.get("weights_sha256"),
        "detector_config_sha256":semantic_manifest.get("detector_config_sha256") or p1_manifest.get("config_sha256"),
        "pipeline_config_sha256":semantic_manifest.get("pipeline_config_sha256",rep.get("effective_runtime_config",{}).get("source_sha256")),
        "m2_m3_binary_sha256":rep.get("m2_m3_binary_sha256"),"dude_binary_sha256":rep.get("dude_binary_sha256"),
    }
    if p3_manifest:
        metric["run_metadata"].update({
            "p3_effective_sampling_stride": p3_manifest.get("effective_sampling_stride"),
            "p3_effective_cluster_eps_m": p3_manifest.get("effective_cluster_eps_m"),
            "p3_effective_cluster_min_points": p3_manifest.get("effective_cluster_min_points"),
            "p3_profile_sha256": p3_manifest.get("profile_sha256"),
            "p3_status": p3_manifest.get("status"),
        })
        decisions_by_reason = defaultdict(int)
        decisions_by_class = defaultdict(lambda: defaultdict(int))
        mask_sizes_by_decision = defaultdict(list)
        for row in p3_ledger_rows:
            decision = str(row.get("decision", "unknown"))
            decisions_by_reason[f"{decision}:{row.get('reason') or 'unspecified'}"] += 1
            decisions_by_class[str(row.get("class_canonical") or "unmapped")][decision] += 1
            if isinstance(row.get("mask_pixel_count"), (int, float)):
                mask_sizes_by_decision[decision].append(int(row["mask_pixel_count"]))
        metric["p3_and_association"]["p3_effective_parameters"] = record({key:p3_manifest.get(key) for key in (
            "effective_sampling_stride", "effective_cluster_eps_m", "effective_cluster_min_points")},
            unit="profile parameters", sample_count=len(p3_ledger_rows), scope="effective P3 filtering configuration")
        metric["p3_and_association"]["filter_breakdown"] = record({
            "decision_reason_counts":dict(sorted(decisions_by_reason.items())),
            "class_decision_counts":{name:dict(sorted(counts.items())) for name,counts in sorted(decisions_by_class.items())},
            "mask_pixel_count_by_decision":{decision:{"sample_count":len(values),"median":percentile(values,50),
                "p95":percentile(values,95),"max":max(values) if values else None}
                for decision,values in sorted(mask_sizes_by_decision.items())}},
            unit="detections", sample_count=len(p3_ledger_rows),
            scope="P3 filtering diagnostics only; not detector accuracy")
    output.mkdir(parents=True,exist_ok=True)
    (output/"failure_samples").mkdir(exist_ok=True)
    (output/"metrics.json").write_text(json.dumps(metrics_doc,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8")
    (output/"membership_and_labels.json").write_text(json.dumps({"schema_version":1,"predictor_membership_rows":membership_rows,
        "semantic_region_rows":semantic_rows,"gt_used_by_predictor":False},indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8")
    summary_fields=list(summary)
    with (output/"metrics.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=summary_fields);w.writeheader();w.writerow(summary)
    (output/"failure_samples/index.json").write_text(json.dumps({"schema_version":1,"status":"evidence_index_only","rows":failures,
        "capture_root":str(capture),"diagnostics":str(diag_path),
        "object_evaluator_evidence":str(args.object_evaluation.resolve().parent) if args.object_evaluation else None,
        "p3_rejection_rows_are_triage_not_accuracy":True},indent=2,sort_keys=True)+"\n",encoding="utf-8")
    report_lines=[
        f"# Habitat evaluation — {summary['evaluation_run_id']}",
        "",
        f"- Mode: **{mode}**; protocol: `habitat_scene_eval_v1`.",
        f"- Dataset/scene/sequence: `{inferred_dataset}/{inferred_version}/{summary['scene_id']}` / `{cap.get('sequence_id')}`.",
        f"- Frames: {input_frames} input, {len(diagnostics)} processed, {failed_frames} failed; M4 rejects={m4_rejected}, region-update stale={m4_stale}.",
        f"- Route: {cap.get('route_type')} / {cap.get('motion_model')}; this is a sampled GT-pose observation route, not physical robot navigation.",
        f"- Valid-depth fraction: {op['observation']['valid_depth_fraction']['value']}; trajectory length: {tlen} m.",
        f"- Builder update runtime p50/p95: {runtime['update_runtime_ms']['median_ms']} / {runtime['update_runtime_ms']['p95_ms']} ms; first update={runtime['update_runtime_ms']['first_frame_ms']} ms; export/render overhead is not separately measured.",
        f"- Snapshot reload / delta replay / fresh replay: {rep.get('snapshot_reload_equal')} / {rep.get('delta_replay_equal')} / {rep.get('fresh_replay_equal') if rep.get('fresh_replay_equal') is not None else 'not_run'}.",
        f"- Room macro IoU: {metric['room']['macro_iou_all_gt_rooms']['value']} ({metric['room']['macro_iou_all_gt_rooms']['unavailable_reason'] or 'scored'}).",
        f"- Object mask precision/recall: {metric['object_detection']['precision']['value']} / {metric['object_detection']['recall']['value']} ({metric['object_detection']['precision']['unavailable_reason'] or 'existing locked evaluator'}).",
        f"- Sidecar label states: {semantic_counts or 'not supplied'}; semantic accuracy remains null without confirmed function GT.",
        "",
        "## Interpretation limits",
        "",
        "Null means unavailable, not zero. No score is inferred from RoomNode count or successful tests. Memory is process-local RUSAGE_SELF; it is not the concurrent peak of the host plus native child workers. Stage timings available in current diagnostics are M1, M2, M3 and combined builder update; M4/M5/graph, model warm-up, export/render and live latency are not separately instrumented.",
        "",
        "Failure evidence pointers: `failure_samples/index.json`.",
        "",
    ]
    (output/"report.vi.md").write_text("\n".join(report_lines),encoding="utf-8")
    input_hashes=sha_inputs({"capture_manifest":cap_manifest_path,"replay_manifest":replay_manifest_path,
        "snapshot":snapshot_path,"delta_log":delta_path,"diagnostics":diag_path,"protocol":PROTOCOL,
        "pipeline_config":ROOT/"configs/reference_code_mp3d.yaml",
        "route_config":args.run_config.resolve() if args.run_config else None,
        "evaluation_profile":evaluation_profile_path.resolve() if evaluation_profile_path else None,
        "grid_trace_index":args.grid_trace.resolve()/"index.json" if args.grid_trace else None,
        "semantic_manifest":semantic_manifest_path,
        "p1_manifest":p1_manifest_path if p1_manifest else None,
        "room_gt":args.room_gt.resolve() if args.room_gt else None,
        "object_evaluation":args.object_evaluation.resolve() if args.object_evaluation else None,
        "semantic_sidecar":semantic_file,
        **grid_trace_hashes,
        "p3_manifest":p3_manifest_path if p3_manifest else None,
        "p3_evidence_ledger":p3_ledger_path if p3_ledger_rows else None,
        "p3_profile":Path(p3_manifest["profile"]) if p3_manifest.get("profile") else None})
    outputs={}
    for p in sorted(output.rglob("*")):
        if p.is_file() and p.name!="manifest.json":
            outputs[str(p.relative_to(output))]=sha256(p)
    manifest={
        "schema_version":1,"kind":"habitat_scene_evaluation_bundle","evaluation_run_id":summary["evaluation_run_id"],
        "created_utc":datetime.now(timezone.utc).isoformat(),"status":"pass" if failed_frames==0 and len(diagnostics)==input_frames else "partial",
        "evaluation_mode":mode,"protocol_id":"habitat_scene_eval_v1","protocol_sha256":sha256(PROTOCOL),
        "input_hashes":input_hashes,"scene":{"dataset_type":inferred_dataset,"dataset_version":inferred_version,
        "scene_id":summary["scene_id"],"sequence_id":cap.get("sequence_id"),"capture_frame_count":input_frames},
        "evaluator":"scripts/evaluate_habitat_run.py","evaluator_sha256":sha256(Path(__file__)),
        "output_hashes":outputs,"builder_input_contains_gt":False,
        "replay_assertions":{"fresh_process":rep.get("fresh_replay_equal"),"snapshot_reload":rep.get("snapshot_reload_equal"),"delta_replay":rep.get("delta_replay_equal")},
        "source_evaluators":{"room":"evaluation.metrics.room_partition_metrics","object":"existing apt_0 evaluator only when attached"},
        "p3_effective_parameters":{key:metric["run_metadata"].get(key) for key in (
            "p3_effective_sampling_stride","p3_effective_cluster_eps_m",
            "p3_effective_cluster_min_points","p3_profile_sha256","p3_status")},
    }
    (output/"manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture",type=Path,required=True)
    p.add_argument("--replay",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--grid-trace",type=Path)
    p.add_argument("--semantic",type=Path)
    p.add_argument("--room-gt",type=Path)
    p.add_argument("--object-evaluation",type=Path,help="metrics.json from existing locked object GT evaluator")
    p.add_argument("--run-config",type=Path,help="scene and route config used by the capture")
    p.add_argument("--evaluation-profile",type=Path,help="versioned scene/GT evaluation profile")
    p.add_argument("--run-id")
    args=p.parse_args()
    print(json.dumps(evaluate(args),indent=2,sort_keys=True,allow_nan=False))


if __name__=="__main__":
    main()
