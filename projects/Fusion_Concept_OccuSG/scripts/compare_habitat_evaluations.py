#!/usr/bin/env python3
"""Compare Habitat evaluation bundles without mixing incompatible metric scopes."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def sha256(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()


def walk_metrics(value,prefix=""):
    if not isinstance(value,dict): return
    if "value" in value and ("unit" in value or "unavailable_reason" in value):
        yield prefix,value
        return
    for key,item in value.items():
        if isinstance(item,dict):
            yield from walk_metrics(item,f"{prefix}.{key}" if prefix else key)


def compatibility_key(metric_id,row,protocol_id,conditions):
    gt=row.get("gt_status")
    if isinstance(gt,(dict,list)): gt=json.dumps(gt,sort_keys=True)
    thresholds=json.dumps(row.get("thresholds",{}),sort_keys=True,separators=(",",":"))
    scope=row.get("scope")
    condition_key=json.dumps(conditions,sort_keys=True,separators=(",",":"))
    return (protocol_id,metric_id,row.get("unit"),str(gt),scope,thresholds,condition_key)


def compare(paths,output):
    bundles=[]
    for path in paths:
        path=path.resolve()
        doc=json.loads((path/"metrics.json").read_text(encoding="utf-8"))
        manifest=json.loads((path/"manifest.json").read_text(encoding="utf-8"))
        bundles.append((path,doc,manifest))
    groups=defaultdict(list)
    run_rows=[]
    for path,doc,manifest in bundles:
        s=doc["run_summary"]
        run_rows.append({
            "evaluation_run_id":s.get("evaluation_run_id"),"scene_id":s.get("scene_id"),
            "dataset_type":s.get("dataset_type"),"dataset_version":s.get("dataset_version"),
            "sequence_id":s.get("sequence_id"),"evaluation_mode":s.get("evaluation_mode"),
            "protocol_id":doc.get("protocol_id"),"input_capture_sha256":manifest.get("input_hashes",{}).get("capture_manifest",{}).get("sha256"),
            "frame_count_input":s.get("frame_count_input"),"room_macro_iou":s.get("room_macro_iou"),
            "object_mask_precision":s.get("object_mask_precision"),"object_mask_recall":s.get("object_mask_recall"),
            "valid_depth_fraction":s.get("valid_depth_fraction"),"trajectory_length_m":s.get("trajectory_length_m"),
        })
        for metric_id,row in walk_metrics(doc.get("metrics",{})):
            if row.get("value") is None or row.get("unavailable_reason"):
                continue
            meta=doc.get("metrics",{}).get("run_metadata",{})
            conditions={k:meta.get(k) for k in ("resolution_px","horizontal_fov_deg",
                "pipeline_config_sha256","detector_config_sha256","detector_weights_sha256",
                "p3_effective_sampling_stride","p3_effective_cluster_eps_m","p3_effective_cluster_min_points")}
            groups[compatibility_key(metric_id,row,doc.get("protocol_id"),conditions)].append({
                "run_id":s.get("evaluation_run_id"),"scene_id":s.get("scene_id"),
                "value":row["value"],"numerator":row.get("numerator"),
                "denominator":row.get("denominator"),"sample_count":row.get("sample_count"),
                "aggregation":row.get("aggregation"),"conditions":conditions,
            })
    aggregates=[]
    for key,rows in sorted(groups.items(),key=lambda item:str(item[0])):
        protocol_id,metric_id,unit,gt_status,scope,thresholds,condition_key=key
        numeric=[r for r in rows if isinstance(r["value"],(int,float)) and not isinstance(r["value"],bool)]
        if not numeric:
            continue
        scene_values=defaultdict(list)
        for r in numeric: scene_values[r["scene_id"]].append(float(r["value"]))
        scene_means={scene:sum(vals)/len(vals) for scene,vals in scene_values.items()}
        numerators=[r["numerator"] for r in numeric]
        denominators=[r["denominator"] for r in numeric]
        pooled=None
        if numeric and all(isinstance(x,(int,float)) for x in numerators+denominators) and sum(denominators)>0:
            pooled=sum(numerators)/sum(denominators)
        aggregates.append({
            "protocol_id":protocol_id,"metric_id":metric_id,"unit":unit,"gt_status":gt_status,
            "scope":scope,"thresholds":json.loads(thresholds),
            "conditions":json.loads(condition_key),
            "run_count":len(numeric),"scene_count":len(scene_means),
            "macro_mean_per_scene":sum(scene_means.values())/len(scene_means) if scene_means else None,
            "scene_means":scene_means,"pooled_ratio_from_counts":pooled,
            "pooled_numerator":sum(numerators) if numeric and all(isinstance(x,(int,float)) for x in numerators) else None,
            "pooled_denominator":sum(denominators) if numeric and all(isinstance(x,(int,float)) for x in denominators) else None,
            "sample_count_sum":sum(r["sample_count"] for r in numeric if isinstance(r["sample_count"],int)) if any(isinstance(r["sample_count"],int) for r in numeric) else None,
            "included_runs":[r["run_id"] for r in numeric],
            "note":"Only identical protocol/unit/GT status/scope/threshold groups are aggregated; median/p95 are not pooled."
        })
    output.mkdir(parents=True,exist_ok=False)
    result={"schema_version":1,"kind":"habitat_evaluation_comparison","created_utc":datetime.now(timezone.utc).isoformat(),
        "protocol_id":"habitat_scene_eval_v1","run_count":len(run_rows),"runs":run_rows,"compatible_aggregates":aggregates,
        "excluded_or_unavailable_metrics":"Null/unavailable metrics are omitted from aggregates, not treated as zero."}
    json_path=output/"comparison.json"
    json_path.write_text(json.dumps(result,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8")
    if run_rows:
        with (output/"metrics.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(run_rows[0]));w.writeheader();w.writerows(run_rows)
    lines=["# Habitat run comparison","","One row per evaluation bundle. Aggregates include only exact matches of protocol, unit, GT status, scope and thresholds.","","| Run | Dataset / scene | Mode | Frames | Room macro IoU | Object precision / recall | Valid depth | Route length |","|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in run_rows:
        lines.append(f"| {r['evaluation_run_id']} | {r['dataset_type']} {r['scene_id']} | {r['evaluation_mode']} | {r['frame_count_input']} | {r['room_macro_iou']} | {r['object_mask_precision']} / {r['object_mask_recall']} | {r['valid_depth_fraction']} | {r['trajectory_length_m']} |")
    lines += ["","## Compatible metric aggregates",""]
    for a in aggregates:
        cond=a["conditions"]
        condition_text=f"{cond.get('resolution_px')} px, HFOV {cond.get('horizontal_fov_deg')}°, pipeline {str(cond.get('pipeline_config_sha256') or 'n/a')[:10]}, detector {str(cond.get('detector_weights_sha256') or 'n/a')[:10]}"
        lines.append(f"- `{a['metric_id']}` ({a['unit']}, GT `{a['gt_status']}`; {condition_text}): macro mean {a['macro_mean_per_scene']} across {a['scene_count']} scenes ({a['run_count']} runs); pooled count ratio={a['pooled_ratio_from_counts']}.")
    lines += ["","Only scalar numeric metrics are aggregated. Pooled ratios require numerator/denominator counts; latency percentiles and structured records are not pooled. Runs with different resolution/FOV/config/P3 settings or GT scope are kept in separate buckets. Dataset/scene remains a grouping axis, not by itself a reason to hide compatible operational measurements.",""]
    (output/"report.vi.md").write_text("\n".join(lines),encoding="utf-8")
    manifest={"schema_version":1,"kind":"habitat_evaluation_comparison_manifest","protocol_id":"habitat_scene_eval_v1",
        "inputs":[{"bundle":str(p),"manifest_sha256":sha256(p/"manifest.json"),"metrics_sha256":sha256(p/"metrics.json")} for p,_,_ in bundles],
        "outputs":{p.name:sha256(p) for p in (json_path,output/"metrics.csv",output/"report.vi.md") if p.is_file()}}
    (output/"manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evaluations",type=Path,nargs="+",required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    result=compare(args.evaluations,args.output)
    print(json.dumps({"status":"pass","run_count":result["run_count"],
        "aggregate_count":len(result["compatible_aggregates"]),"output":str(args.output.resolve()),
        "report":str((args.output.resolve()/"report.vi.md"))},indent=2,sort_keys=True))


if __name__=="__main__": main()
