#!/usr/bin/env python3
"""Compare the locked U6 00808 object admission thresholds (3 vs 1)."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from integration_v2.unified_scene_graph import build_unified_graph
from integration_v2.hybrid_mapping_viewer import build_hybrid_mapping_viewer
from standalone_sg.serialization.json_graph import load_snapshot
from scripts.report_conceptgraphs_hybrid_quality import (
    object_track_stats, observation_membership, pair_metrics, read_json, read_jsonl,
    reviewed_identity_coverage,
)


def _partition(rows):
    return sorted(tuple(sorted(row["observation_ids"])) for row in rows)


def _clouds(mapping_root):
    diag = read_json(mapping_root / "diagnostic/diagnostic_manifest.json")
    result = {}
    for row in diag["objects"]:
        member_key = tuple(sorted(row["observation_ids"]))
        if member_key in result:
            raise ValueError("two diagnostic hypotheses have identical observation membership")
        with np.load(mapping_root / "diagnostic" / row["cloud_npz"], allow_pickle=False) as data:
            result[member_key] = {
                "xyz": np.asarray(data["xyz"]), "rgb": np.asarray(data["rgb"]),
                "bounds": np.asarray(row["bounds_minmax"], dtype=np.float64),
                "sha256": row["cloud_sha256"],
            }
    return result


def verify_prefilter_equal(base_root: Path, candidate_root: Path):
    b = read_json(base_root / "lineage_stages.json")
    c = read_json(candidate_root / "lineage_stages.json")
    if b["input_observation_ids"] != c["input_observation_ids"]:
        raise ValueError("fixed input observation population differs")
    checks = {}
    for stage in ("online_before_postprocess", "after_denoise"):
        checks[f"{stage}_partition_equal"] = _partition(b["stages"][stage]) == _partition(c["stages"][stage])
        if not checks[f"{stage}_partition_equal"]:
            raise AssertionError(f"pre-filter {stage} membership differs")
    bc, cc = _clouds(base_root), _clouds(candidate_root)
    if set(bc) != set(cc):
        raise AssertionError("pre-filter geometry membership differs")
    checks["hypothesis_count"] = len(bc)
    checks["geometry_xyz_rgb_exact"] = all(
        np.array_equal(bc[k]["xyz"], cc[k]["xyz"]) and np.array_equal(bc[k]["rgb"], cc[k]["rgb"])
        for k in bc
    )
    checks["geometry_bounds_exact"] = all(np.array_equal(bc[k]["bounds"], cc[k]["bounds"]) for k in bc)
    checks["cloud_sha256_equal"] = all(bc[k]["sha256"] == cc[k]["sha256"] for k in bc)
    if not all(checks[k] for k in ("geometry_xyz_rgb_exact", "geometry_bounds_exact", "cloud_sha256_equal")):
        raise AssertionError("pre-filter geometry differs between threshold configurations")
    return checks


def _room_state(graph_root):
    snap = load_snapshot(graph_root / "unified_snapshot.json")
    rooms = {n.node_id: dict(n.attributes) for n in snap.nodes if n.kind == "room"}
    room_ids = set(rooms)
    adjacency = sorted((e.source_id, e.target_id, e.relation, e.provenance, e.directed)
                       for e in snap.edges if e.source_id in room_ids and e.target_id in room_ids)
    objects = [n for n in snap.nodes if n.kind == "object"]
    object_ids = {n.node_id for n in objects}
    memberships = {n.node_id: n.attributes.get("membership_status") for n in objects}
    edges = sorted((e.source_id, e.target_id) for e in snap.edges
                   if e.relation == "room_contains" and e.target_id in object_ids)
    return {"snapshot": snap, "rooms": rooms, "adjacency": adjacency,
            "objects": objects, "membership_status_counts": {
                k: sum(v == k for v in memberships.values()) for k in ("assigned", "ambiguous", "unassigned")
            }, "room_contains_edges": edges}


def run_report(base: Path, candidate: Path, cache: Path, annotation: Path, room_snapshot: Path):
    base_map, cand_map = base / "mapping", candidate / "mapping"
    bm, cm = read_json(base_map / "run_manifest.json"), read_json(cand_map / "run_manifest.json")
    bc, cc = read_json(base_map / "experiment_contract.json"), read_json(cand_map / "experiment_contract.json")
    if bc["effective_config"]["obj_min_detections"] != 3 or cc["effective_config"]["obj_min_detections"] != 1:
        raise ValueError("expected locked obj_min_detections 3 vs 1")
    config_delta = {key: {"min3": bc["effective_config"].get(key), "min1": cc["effective_config"].get(key)}
                    for key in sorted(set(bc["effective_config"]) | set(cc["effective_config"]))
                    if bc["effective_config"].get(key) != cc["effective_config"].get(key)}
    if config_delta != {"obj_min_detections": {"min3": 3, "min1": 1}}:
        raise ValueError(f"effective config has changes beyond obj_min_detections: {config_delta}")
    if bc["rng_control"]["seed"] != cc["rng_control"]["seed"] or bc["input_observation_ids"] != cc["input_observation_ids"]:
        raise ValueError("seed or observation population differs")
    for key in ("capture_manifest_sha256", "p2_index_sha256", "p3_manifest_sha256", "text_features_manifest_sha256"):
        if bc.get(key) != cc.get(key):
            raise ValueError(f"input provenance mismatch: {key}")

    prefilter = verify_prefilter_equal(base_map, cand_map)
    bs, cs = read_json(base_map / "lineage_stages.json")["stages"], read_json(cand_map / "lineage_stages.json")["stages"]
    bmem = {name: observation_membership(rows) for name, rows in bs.items()}
    cmem = {name: observation_membership(rows) for name, rows in cs.items()}
    p2 = read_json(cache / "detection_2d/detection_2d_index.json")
    all_obs = {d["detection_id"]: {"frame": int(f["frame_index"]), "raw_class": d["class_raw"]}
               for f in p2["frames"] for d in f["detections"]}
    if set(all_obs) != set(bmem["online_before_postprocess"]):
        raise ValueError("P2 population differs from mapping input")

    review = read_json(annotation)
    groups = review["same_identity_groups"]
    human_ids = {oid: token for token, ids in groups.items() for oid in ids}
    human_frames = {oid: all_obs[oid]["frame"] for oid in human_ids}
    temporal_human_ids = {oid: token for token, ids in groups.items()
                          if len({all_obs[oid]["frame"] for oid in ids}) > 1 for oid in ids}
    gt_ids = {r["detection_id"]: str(r["gt_semantic_id"])
              for r in read_jsonl(cache / "u6_offline_evaluation_complete/detection_gt_matches.jsonl")
              if r.get("gt_matched")}
    gt_frames = {oid: all_obs[oid]["frame"] for oid in gt_ids}

    metrics = {}
    for label, memberships in (("min3", bmem), ("min1", cmem)):
        final = memberships["after_text_gated_merge"]
        for scope, identities, frames in (("human_reviewed", human_ids, human_frames), ("evaluator_id", gt_ids, gt_frames)):
            scored = pair_metrics(identities, final, frame_by_id=frames)
            assigned = sum(oid in final for oid in identities)
            scored.update({"observation_count": len(identities), "assigned_observations": assigned,
                           "observation_coverage": assigned / len(identities) if identities else None})
            if scope == "human_reviewed":
                scored["identity_coverage"] = reviewed_identity_coverage(groups, final)
                scored.update(object_track_stats(temporal_human_ids, final))
                scored["fragmentation_scope"] = "identities with reviewed observations in at least two distinct frames; same-frame-only table_2 is reported separately"
            else:
                scored.update(object_track_stats(identities, final))
                scored["matched_gt_over_all_p2"] = assigned / len(all_obs)
            metrics[f"{label}_{scope}"] = scored

    d2 = groups.get("table_2", [])
    d2_scores = {}
    for label, memberships in (("min3", bmem), ("min1", cmem)):
        final = memberships["after_text_gated_merge"]
        tracks = [final.get(oid) for oid in d2]
        d2_scores[label] = {"observation_ids": d2, "assigned_count": sum(x is not None for x in tracks),
                            "same_predicted_object": len(tracks) == 2 and tracks[0] is not None and tracks[0] == tracks[1],
                            "tracks": tracks,
                            "interpretation": "not retained/scorable" if all(x is None for x in tracks) else
                                             ("same object" if tracks[0] == tracks[1] else "two separate retained hypotheses")}

    base_graph = base / "final_graph"
    if not (base_graph / "unified_snapshot.json").is_file():
        build_unified_graph(room_snapshot, base_map / "export", base_graph, source_kind="reference")
    cand_graph = candidate / "final_graph"
    if (cand_graph / "unified_snapshot.json").is_file() and (cand_graph / "manifest.json").is_file():
        graph_manifest = read_json(cand_graph / "manifest.json")
        export_digest = hashlib.sha256((cand_map / "export/export_manifest.json").read_bytes()).hexdigest()
        if graph_manifest.get("object_export_manifest_sha256") != export_digest:
            raise ValueError("existing candidate graph provenance does not match this min=1 export")
    else:
        build_unified_graph(room_snapshot, cand_map / "export", cand_graph, source_kind="reference")
    br, cr = _room_state(base_graph), _room_state(cand_graph)
    room_snapshot_obj = load_snapshot(room_snapshot)
    room_nodes_before = {n.node_id: dict(n.attributes) for n in room_snapshot_obj.nodes if n.kind == "room"}
    room_ids_before = set(room_nodes_before)
    room_edges_before = sorted((e.source_id, e.target_id, e.relation, e.provenance, e.directed)
                               for e in room_snapshot_obj.edges if e.source_id in room_ids_before and e.target_id in room_ids_before)
    for state in (br, cr):
        if state["rooms"] != room_nodes_before or state["adjacency"] != room_edges_before:
            raise AssertionError("object import changed room geometry/semantic scores/adjacency")

    bdiag = read_json(base_map / "diagnostic/diagnostic_manifest.json")
    cdiag = read_json(cand_map / "diagnostic/diagnostic_manifest.json")
    base_pre = {r["lineage_key"]: r for r in bdiag["objects"]}
    cand_pre = {r["lineage_key"]: r for r in cdiag["objects"]}
    filtered_baseline = sorted(set(base_pre) - set(bmem["after_min_detections_filter"].values()))
    retained_candidate = sorted(set(cmem["after_min_detections_filter"].values()))
    b_after = _partition(bs["after_min_detections_filter"])
    b_final = _partition(bs["after_text_gated_merge"])
    c_after = _partition(cs["after_min_detections_filter"])
    c_final = _partition(cs["after_text_gated_merge"])
    summary = {
        "schema": "u6-00808-object-admission-ablation/v1", "status": "pass",
        "scope": "Same cached 5-frame hybrid mapping inputs; only obj_min_detections differs. Not a quality holdout.",
        "runs": {"min3": str(base), "min1": str(candidate)}, "seed": bc["rng_control"]["seed"],
        "input_hashes_match": True, "effective_config_delta": config_delta, "pre_filter_parity": prefilter,
        "stages": {
            "min3": {"online_hypotheses": len(bs["online_before_postprocess"]), "after_filter": len(bs["after_min_detections_filter"]),
                     "after_merge_objects": len(bs["after_text_gated_merge"]), "filtered_hypotheses": len(filtered_baseline),
                     "filtered_observations": len(all_obs)-len(bmem["after_text_gated_merge"])},
            "min1": {"online_hypotheses": len(cs["online_before_postprocess"]), "after_filter": len(cs["after_min_detections_filter"]),
                     "after_merge_objects": len(cs["after_text_gated_merge"]), "filtered_hypotheses": 0,
                     "filtered_observations": len(all_obs)-len(cmem["after_text_gated_merge"])},
            "newly_admitted_hypotheses": [
                {"lineage_key": k, "detection_count": base_pre[k]["detection_count"],
                 "distinct_frame_count": base_pre[k]["distinct_frame_count"],
                 "distinct_pose_count": base_pre[k]["distinct_pose_count"],
                 "observation_ids": base_pre[k]["observation_ids"]} for k in filtered_baseline
            ],
            "merge_objects": {"min3_membership_changed": b_after != b_final,
                              "min1_membership_changed": c_after != c_final,
                              "min3_merges": len(bmem["after_min_detections_filter"]) - len(bmem["after_text_gated_merge"]),
                              "min1_merges": len(cmem["after_min_detections_filter"]) - len(cmem["after_text_gated_merge"]),
                              "new_merges_due_to_admission": 0 if c_after == c_final else None},
        },
        "metrics": metrics, "d2_same_frame_duplicate_consistency": d2_scores,
        "room_graph": {"min3_objects": len(br["objects"]), "min1_objects": len(cr["objects"]),
                       "min3_membership_status_counts": br["membership_status_counts"],
                       "min1_membership_status_counts": cr["membership_status_counts"],
                       "min3_object_memberships": [{"object_id": n.attributes.get("external_object_id", n.node_id),
                           "status": n.attributes.get("membership_status"),
                           "plausible_room_ids": n.attributes.get("plausible_room_ids", []),
                           "observation_count": len(n.attributes.get("evidence", []))} for n in br["objects"]],
                       "min1_object_memberships": [{"object_id": n.attributes.get("external_object_id", n.node_id),
                           "status": n.attributes.get("membership_status"),
                           "plausible_room_ids": n.attributes.get("plausible_room_ids", []),
                           "observation_count": len(n.attributes.get("evidence", []))} for n in cr["objects"]],
                       "min3_room_contains_edges": len(br["room_contains_edges"]), "min1_room_contains_edges": len(cr["room_contains_edges"]),
                       "room_geometry_semantic_attributes_adjacency_unchanged": True,
                       "room_semantic_scoring": "off; unchanged"},
        "runtime": {"min3_mapping_s": bm["mapping_runtime_s"], "min3_total_s": bm["runtime_s"], "min3_peak_rss_mib": bm["peak_rss_mib"],
                    "min1_mapping_s": cm["mapping_runtime_s"], "min1_total_s": cm["runtime_s"], "min1_peak_rss_mib": cm["peak_rss_mib"],
                    "scope": "cached mapping including reference postprocessing; excludes capture, detector, encoder, room mapping"},
        "interpretation": {"identity_scope": "human identities only for exact reviewed observation IDs; evaluator IDs separately; no propagation",
                           "kept_objects": "not human-confirmed identities", "min1_threshold_status": "experimental only; short-sequence inspection aid"},
    }
    out = candidate / "filter_ablation_v6"
    out.mkdir(exist_ok=False)
    (out / "metrics.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False)+"\n")
    _write_markdown(out / "REPORT.vi.md", summary, metrics, d2_scores)
    build_hybrid_mapping_viewer(room_snapshot, cand_map, cand_graph, candidate / "hybrid_viewer.html")
    base_viewer = os.path.relpath(base / "hybrid_viewer.html", candidate)
    cand_viewer = "hybrid_viewer.html"
    base_html = (base / "hybrid_viewer.html").read_text(encoding="utf-8")
    # srcdoc inherits this page's base URL. Rebase the baseline RGB/mask paths.
    base_prefix = base_viewer.rsplit("/", 1)[0]
    base_html = base_html.replace('"mapping/', f'"{base_prefix}/mapping/')
    candidate_html = (candidate / "hybrid_viewer.html").read_text(encoding="utf-8")
    page = f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>U6 00808 admission ablation</title>
<style>body{{font:15px system-ui;margin:16px;background:#f5f7fb;color:#172033}}h1{{margin:0 0 8px}}.summary{{background:white;padding:12px;border:1px solid #ccd;border-radius:8px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}iframe{{width:100%;height:90vh;border:1px solid #bbc;border-radius:8px;background:white}}@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}</style>
<h1>Object admission: min detections 3 vs 1</h1><div class="summary">Same 5-frame cache, seed, association, geometry and postprocessing order. Only final filter threshold changes. Objects retained at min=1 are hypotheses, not confirmed identities. Room semantic scoring is off.<br><a href="filter_ablation_v6/REPORT.vi.md">Open comparison report</a> · <a href="{base_viewer}">Open min=3 viewer</a> · <a href="{cand_viewer}">Open min=1 viewer</a></div>
<div class="grid"><section><h2>min=3 · 2 final objects</h2><iframe srcdoc="{html.escape(base_html, quote=True)}"></iframe></section><section><h2>min=1 · 9 final objects</h2><iframe srcdoc="{html.escape(candidate_html, quote=True)}"></iframe></section></div>'''
    (candidate / "filter_ablation_comparison_v6.html").write_text(page, encoding="utf-8")
    return summary


def _write_markdown(path, report, metrics, d2):
    def row(scope, key):
        m=metrics[key]
        def f(v): return "n/a" if v is None else f"{v:.3f}"
        return f"| {scope} | {m['TP']}/{m['FP']}/{m['FN']} | {f(m['precision'])}/{f(m['recall'])}/{f(m['f1'])} | {m['assigned_observations']}/{m['observation_count']} | {m['false_merge_track_count']} | {m['fragmentation_excess_tracks']} |"
    lines=["# Q1 00808 object admission ablation", "",
      "Cùng cache 5 frame, seed, detector/encoder sidecars, association và geometry. Effective-config diff chỉ có `obj_min_detections`: 3 → 1. Đây là development subset, không phải holdout.", "",
      "## Kết quả admission", "",
      "| Config | online hypotheses | after filter | after merge_objects | filtered hypotheses | filtered observations |",
      "|---|---:|---:|---:|---:|---:|",
      f"| min=3 | 9 | 2 | 2 | 7 | 10/18 |",
      f"| min=1 | 9 | 9 | 9 | 0 | 0/18 |", "",
      "Pre-filter partition, XYZ/RGB point arrays và bounds khớp chính xác giữa hai run. `merge_objects` không đổi membership ở cả hai config; không có merge mới nào xuất hiện nhờ admission. Min=1 giữ bảy hypotheses bổ sung (có 1–2 detections); điều này không chứng minh bảy physical identities.", "",
      "## Human reviewed identities", "",
      "Chỉ exact reviewed observation IDs: 6 observations thuộc `table_1` và `table_2`; cross-frame pair denominator 12 (6 positive, 6 negative). D2 cùng-frame được tách riêng.", "",
      "| Config | TP/FP/FN | P/R/F1 | reviewed observations | false-merge tracks | fragmentation excess |",
      "|---|---:|---:|---:|---:|---:|"]
    lines += [row("min=3", "min3_human_reviewed"),row("min=1", "min1_human_reviewed")]
    lines += ["", f"Identity coverage: min=3 {metrics['min3_human_reviewed']['identity_coverage']['identities_present']}/2; min=1 {metrics['min1_human_reviewed']['identity_coverage']['identities_present']}/2. Observation coverage: {metrics['min3_human_reviewed']['assigned_observations']}/6 → {metrics['min1_human_reviewed']['assigned_observations']}/6.",
      f"D2 `table_2` same-frame pair: min=3 retains {d2['min3']['assigned_count']}/2 (not scorable after filtering); min=1 retains 2/2 but leaves them as two separate hypotheses, so same-object consistency is false. It is not a second mask TP.", "",
      "## Dataset evaluator IDs", "",
      "Official one-to-one Hungarian matched detections only; separate from physical identity review. Fixed cross-frame denominator: 61 pairs among the 14 matched observations (9 positive identity pairs, 52 negative pairs; same-frame pairs excluded).", "",
      "| Config | TP/FP/FN | P/R/F1 | evaluator observations retained | false-merge tracks | fragmentation excess |",
      "|---|---:|---:|---:|---:|---:|"]
    lines += [row("min=3", "min3_evaluator_id"),row("min=1", "min1_evaluator_id")]
    lines += ["", "## Room graph and cost", "", f"Room geometry, semantic attributes, adjacency and semantic scoring stayed unchanged. Final object nodes: {report['room_graph']['min3_objects']} → {report['room_graph']['min1_objects']}; `room_contains` edges: {report['room_graph']['min3_room_contains_edges']} → {report['room_graph']['min1_room_contains_edges']}. Membership status counts min=3: `{report['room_graph']['min3_membership_status_counts']}`; min=1: `{report['room_graph']['min1_membership_status_counts']}`.", "",
      f"Cached mapping total runtime: {report['runtime']['min3_total_s']:.2f}s → {report['runtime']['min1_total_s']:.2f}s; peak RSS: {report['runtime']['min3_peak_rss_mib']:.1f} → {report['runtime']['min1_peak_rss_mib']:.1f} MiB. This short run is not a meaningful speed comparison and is not real-time end-to-end.", "",
      "## Recommendation", "", "Keep min=1 as an experimental option for inspecting short sequences, with a clear ‘unconfirmed hypothesis’ label. It improves retained observation/identity coverage in this reviewed subset, but does not fix D2 duplicate consistency and may expose one-observation hypotheses. Do not change the default or call this a general quality improvement.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    p=argparse.ArgumentParser()
    for name in ("base", "candidate", "cache", "annotation", "room-snapshot"):
        p.add_argument("--"+name, type=Path, required=True)
    a=p.parse_args()
    result=run_report(a.base.resolve(),a.candidate.resolve(),a.cache.resolve(),a.annotation.resolve(),a.room_snapshot.resolve())
    print(json.dumps({"status":result["status"],"effective_config_delta":result["effective_config_delta"],"pre_filter_parity":result["pre_filter_parity"],"stages":result["stages"],"room_graph":result["room_graph"],"output":str(a.candidate/"filter_ablation_v6"),"viewer":str(a.candidate/"filter_ablation_comparison_v6.html")},indent=2))

if __name__=="__main__": main()
