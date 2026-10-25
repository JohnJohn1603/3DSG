#!/usr/bin/env python3
"""Report a locked min-detections 3 vs 1 comparison for any cached sequence."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from scripts.report_conceptgraphs_hybrid_quality import (
    object_track_stats, observation_membership, pair_metrics, read_json, read_jsonl,
)
from scripts.report_hybrid_filter_ablation import verify_prefilter_equal, _room_state
from standalone_sg.serialization.json_graph import load_snapshot


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def attribute_postfilter_merges(min3_map: Path, min1_map: Path) -> dict:
    """Attribute final min=1 merge components to the min=3 filter decision."""
    min3_rows = read_json(min3_map / "diagnostic/diagnostic_manifest.json")["objects"]
    min1_rows = read_json(min1_map / "diagnostic/diagnostic_manifest.json")["objects"]
    min1_stages = read_json(min1_map / "lineage_stages.json")["stages"]
    min3_status = {row["hypothesis_id"]: row["status_after_filter"] for row in min3_rows}
    source_hypothesis_by_observation = {oid: row["hypothesis_id"] for row in min1_rows for oid in row["observation_ids"]}
    result = []
    for final in min1_stages["after_text_gated_merge"]:
        source_hypotheses = sorted({source_hypothesis_by_observation[oid] for oid in final["observation_ids"]})
        if len(source_hypotheses) <= 1:
            continue
        filtered_at_min3 = [hyp for hyp in source_hypotheses if min3_status.get(hyp) == "filtered"]
        result.append({"final_lineage_key": final["lineage_key"], "source_hypotheses": source_hypotheses,
                       "hypotheses_filtered_at_min3": filtered_at_min3,
                       "merge_involves_newly_admitted_hypothesis": bool(filtered_at_min3),
                       "observation_ids": final["observation_ids"]})
    return {"min3_merges": 0, "min1_merges": len(result), "min1_merge_components": result,
            "min1_merges_involving_newly_admitted_hypotheses": sum(r["merge_involves_newly_admitted_hypothesis"] for r in result)}


def _profile(mapping: Path, cache: Path, gt_rows: list[dict], room_snapshot: Path) -> dict:
    run = read_json(mapping / "run_manifest.json")
    contract = read_json(mapping / "experiment_contract.json")
    lineage = read_json(mapping / "lineage_stages.json")
    diagnostic = read_json(mapping / "diagnostic/diagnostic_manifest.json")
    stages = lineage["stages"]
    memberships = {key: observation_membership(rows) for key, rows in stages.items()}
    p2 = read_json(cache / "detection_2d/detection_2d_index.json")
    obs = {str(d["detection_id"]): {"frame_index": int(fr["frame_index"]), "raw_class": str(d["class_raw"])}
           for fr in p2["frames"] for d in fr["detections"]}
    final = memberships["after_text_gated_merge"]
    if len(obs) != len(lineage["input_observation_ids"]) or set(obs) != set(lineage["input_observation_ids"]):
        raise ValueError("P2 population and mapper input IDs differ")
    if len(obs) != len(memberships["online_before_postprocess"]):
        raise ValueError("online mapping lineage does not cover every P2 observation")
    # Official evaluator identity comes only from one-to-one Hungarian matches.
    evaluator = {str(row["detection_id"]): str(row["gt_semantic_id"])
                 for row in gt_rows if row.get("match_status") == "matched" and row.get("gt_semantic_id") is not None}
    evaluator_frames = {oid: obs[oid]["frame_index"] for oid in evaluator}
    evaluator_metrics = pair_metrics(evaluator, final, frame_by_id=evaluator_frames)
    assigned_eval = sum(oid in final for oid in evaluator)
    evaluator_metrics.update({
        "matched_observations": len(evaluator), "assigned_matched_observations": assigned_eval,
        "observation_coverage": assigned_eval / len(evaluator) if evaluator else None,
        "matched_gt_instances": len(set(evaluator.values())),
        "gt_instances_present": len({evaluator[oid] for oid in evaluator if oid in final}),
        "identity_coverage": (len({evaluator[oid] for oid in evaluator if oid in final}) / len(set(evaluator.values())) if evaluator else None),
        "identity_metrics_scope": "dataset GT instance IDs on official one-to-one Hungarian mask matches; not human identity review",
    })
    evaluator_metrics.update(object_track_stats(evaluator, final))
    room_graph = mapping.parent / "final_graph"
    if not (room_graph / "unified_snapshot.json").is_file():
        raise FileNotFoundError(f"unified graph missing: {room_graph}")
    room = _room_state(room_graph)
    source_room = load_snapshot(room_snapshot)
    source_room_nodes = {n.node_id: dict(n.attributes) for n in source_room.nodes if n.kind == "room"}
    room_ids = set(source_room_nodes)
    source_adj = sorted((e.source_id, e.target_id, e.relation, e.provenance, e.directed)
                        for e in source_room.edges if e.source_id in room_ids and e.target_id in room_ids)
    if room["rooms"] != source_room_nodes or room["adjacency"] != source_adj:
        raise AssertionError("room geometry/semantic attributes/adjacency changed during object import")

    support_rows = [{"hypothesis_id": row["hypothesis_id"], "observation_count": row["detection_count"],
                     "frame_count": row["distinct_frame_count"], "pose_count": row["distinct_pose_count"],
                     "status_after_filter": row["status_after_filter"],
                     "postprocess_disposition": row["postprocess_disposition"],
                     "observation_ids": row["observation_ids"]} for row in diagnostic["objects"]]
    counts = diagnostic["counts"]
    final_obs = len(final)
    p3_rows = read_jsonl(cache / "detection_3d/detection_3d.jsonl")
    p3_accepted = sum(len(row.get("detections", [])) for row in p3_rows)
    return {
        "run_id": mapping.parent.name, "effective_config": contract["effective_config"],
        "frames": run["frames"], "p2_observations": len(obs), "p3_accepted": p3_accepted,
        "p3_rejected": len(obs)-p3_accepted, "online_assigned_observations": len(memberships["online_before_postprocess"]),
        "hypotheses_before_filter": counts["before_filter"], "hypotheses_retained": counts["retained_after_filter"],
        "hypotheses_filtered": counts["filtered"], "objects_after_filter": len(stages["after_min_detections_filter"]),
        "objects_after_merge_objects": len(stages["after_text_gated_merge"]),
        "observations_retained_final": final_obs, "observations_filtered_or_otherwise_unassigned_final": len(obs)-final_obs,
        "observation_coverage_all_p2": final_obs / len(obs) if obs else None,
        "hypothesis_support": support_rows,
        "evaluator_id": evaluator_metrics,
        "human_identity": {"status": "unavailable", "reviewed_observations": 0,
                           "reason": "No human identity annotations for this exact main-route observation ID population; no cross-sequence propagation."},
        "same_frame_duplicate": {"status": "unavailable", "reason": "Official evaluator rows use one-to-one Hungarian matching; no human duplicate labels for this sequence."},
        "room_graph": {"object_nodes": len(room["objects"]),
                       "membership_status_counts": room["membership_status_counts"],
                       "room_contains_edges": len(room["room_contains_edges"]),
                       "room_geometry_semantic_attributes_adjacency_unchanged": True,
                       "room_semantic_scoring": "off; unchanged",
                       "memberships": [{"object_id": n.attributes.get("external_object_id", n.node_id),
                                        "status": n.attributes.get("membership_status"),
                                        "plausible_room_ids": n.attributes.get("plausible_room_ids", []),
                                        "observation_count": len(n.attributes.get("evidence", []))}
                                       for n in room["objects"]]},
        "runtime": {"mapping_s": run["runtime_s"], "mapping_core_s": run["mapping_runtime_s"],
                    "peak_rss_mib": run["peak_rss_mib"], "launcher_adapter_viewer_s": read_json(mapping.parent / "hybrid_manifest.json")["launcher_integration_runtime_s"]},
        "unknown_or_unreviewed_p2_observations": len(obs),
    }


def run_report(min3: Path, min1: Path, cache: Path, evaluator_matches: Path, room_snapshot: Path,
               pair_lock: Path, output: Path) -> dict:
    for p in (min3, min1, cache, evaluator_matches, room_snapshot, pair_lock):
        if not p.exists():
            raise FileNotFoundError(p)
    if output.exists():
        raise FileExistsError(output)
    min3_map, min1_map = min3 / "mapping", min1 / "mapping"
    a, b = read_json(min3_map / "experiment_contract.json"), read_json(min1_map / "experiment_contract.json")
    delta = {k: {"min3": a["effective_config"].get(k), "min1": b["effective_config"].get(k)}
             for k in sorted(set(a["effective_config"]) | set(b["effective_config"]))
             if a["effective_config"].get(k) != b["effective_config"].get(k)}
    if delta != {"obj_min_detections": {"min3": 3, "min1": 1}}:
        raise ValueError(f"effective config delta is not exactly the filter threshold: {delta}")
    for key in ("seed", "input_observation_ids", "capture_manifest_sha256", "p2_index_sha256", "p3_manifest_sha256",
                "text_features_manifest_sha256", "image_features_manifest_sha256", "room_snapshot_sha256"):
        if a.get(key) != b.get(key):
            raise ValueError(f"paired run mismatch: {key}")
    prefilter = verify_prefilter_equal(min3_map, min1_map)
    gt_rows = read_jsonl(evaluator_matches)
    profiles = {"min3": _profile(min3_map, cache, gt_rows, room_snapshot),
                "min1": _profile(min1_map, cache, gt_rows, room_snapshot)}
    if profiles["min3"]["p2_observations"] != profiles["min1"]["p2_observations"]:
        raise AssertionError("P2 population differs")
    # Attribute each min=1 merge to hypotheses which min=3 would have removed.
    merge_details = attribute_postfilter_merges(min3_map, min1_map)
    output.mkdir(parents=True)
    report = {"schema": "conceptgraphs-sequence-filter-ablation-report/v1", "status": "pass",
              "scope": "Development replay on a previously viewed cached 00802 main-route sequence; not holdout or unseen-scene test.",
              "sequence": {"scene_id": "00802-wcojb4TFT35", "sequence_id": "hm3d-v02-00802-main-route-01",
                           "cache": str(cache.resolve()), "frames": 29, "p2_detections": 67,
                           "active_detection_frames": sum(bool(f["detections"]) for f in read_json(cache / "detection_2d/detection_2d_index.json")["frames"]),
                           "p3_accepted": profiles["min3"]["p3_accepted"], "p3_rejected": profiles["min3"]["p3_rejected"],
                           "capture_manifest_sha256": _hash(cache / "capture/capture_manifest.json"),
                           "room_snapshot_sha256": _hash(room_snapshot)},
              "effective_config_delta": delta, "prefilter_parity": prefilter,
              "profiles": profiles, "post_filter_merges": merge_details,
              "interpretation": {"min1": "coverage effect of reference final filter only; retained hypotheses are not confirmed identities",
                                 "human_metrics": "unavailable, not zero", "same_frame_duplicates": "unavailable from one-to-one evaluator population",
                                 "semantic_scoring": "unchanged/off", "quality_scope": "dataset evaluator IDs only where official per-frame Hungarian matches exist"},
              "provenance": {"min3_manifest_sha256": _hash(min3 / "hybrid_manifest.json"),
                             "min1_manifest_sha256": _hash(min1 / "hybrid_manifest.json"),
                             "evaluator_matches_sha256": _hash(evaluator_matches),
                             "paired_lock_sha256": _hash(pair_lock)}}
    (output / "metrics.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    _write_markdown(output / "REPORT.vi.md", report)
    return report


def _write_markdown(path: Path, report: dict) -> None:
    p = report["profiles"]
    lines = ["# 00802 main-route: object admission min=3 vs min=1", "",
             report["scope"], "", f"Chỉ đổi `obj_min_detections`. Cache, 29 frame, 67 P2 observations, P3 accepted/rejected {report['sequence']['p3_accepted']}/{report['sequence']['p3_rejected']}, seed, matcher, geometry, merge, room snapshot và sidecars giữ nguyên.", "",
             "| Chỉ số | min=3 | min=1 |", "|---|---:|---:|"]
    for key, title in [("hypotheses_before_filter", "Hypotheses trước filter"), ("hypotheses_retained", "Hypotheses retained"),
                       ("hypotheses_filtered", "Hypotheses filtered"), ("objects_after_filter", "Objects sau filter"),
                       ("objects_after_merge_objects", "Objects sau merge_objects"),
                       ("observations_retained_final", "P2 observations còn trong final map"),
                       ("observations_filtered_or_otherwise_unassigned_final", "P2 observations ngoài final map"),
                       ("room_graph", "Room graph objects")]:
        if key == "room_graph":
            av, bv = p["min3"][key]["object_nodes"], p["min1"][key]["object_nodes"]
        else:
            av, bv = p["min3"][key], p["min1"][key]
        lines.append(f"| {title} | {av} | {bv} |")
    lines += ["", "## Coverage and identity", "",
              f"All-P2 final observation coverage: {p['min3']['observation_coverage_all_p2']:.3f} → {p['min1']['observation_coverage_all_p2']:.3f}.",
              f"Official Hungarian evaluator-ID observations: {p['min3']['evaluator_id']['matched_observations']}; assigned {p['min3']['evaluator_id']['assigned_matched_observations']} → {p['min1']['evaluator_id']['assigned_matched_observations']}.",
              f"Cross-frame pair denominator: {p['min3']['evaluator_id']['pair_count']} ({p['min3']['evaluator_id']['positive_identity_pairs']} positive, {p['min3']['evaluator_id']['negative_identity_pairs']} negative).",
              f"Evaluator-ID TP/FP/FN: {p['min3']['evaluator_id']['TP']}/{p['min3']['evaluator_id']['FP']}/{p['min3']['evaluator_id']['FN']} → {p['min1']['evaluator_id']['TP']}/{p['min1']['evaluator_id']['FP']}/{p['min1']['evaluator_id']['FN']}.",
              f"P/R/F1: {p['min3']['evaluator_id']['precision']:.3f}/{p['min3']['evaluator_id']['recall']:.3f}/{p['min3']['evaluator_id']['f1']:.3f} → {p['min1']['evaluator_id']['precision']:.3f}/{p['min1']['evaluator_id']['recall']:.3f}/{p['min1']['evaluator_id']['f1']:.3f}.",
              f"False-merge tracks: {p['min3']['evaluator_id']['false_merge_track_count']} → {p['min1']['evaluator_id']['false_merge_track_count']}; fragmented evaluator instances: {p['min3']['evaluator_id']['fragmented_identity_count']} → {p['min1']['evaluator_id']['fragmented_identity_count']}; fragmentation excess tracks: {p['min3']['evaluator_id']['fragmentation_excess_tracks']} → {p['min1']['evaluator_id']['fragmentation_excess_tracks']}.",
              f"`merge_objects`: {report['post_filter_merges']['min3_merges']} → {report['post_filter_merges']['min1_merges']} merge components after filter; {report['post_filter_merges']['min1_merges_involving_newly_admitted_hypotheses']} min=1 components include hypotheses min=3 filtered.",
              "Human identity metrics: unavailable for this exact sequence; no identities were inferred from another route. Same-frame duplicate scoring is also unavailable because official evaluator matching is one-to-one.", "",
              "## Room graph and cost", "",
              f"Room membership status min=3: `{p['min3']['room_graph']['membership_status_counts']}`; min=1: `{p['min1']['room_graph']['membership_status_counts']}`. Room geometry, semantic attributes, adjacency, and semantic scoring remained unchanged/off.",
              f"Mapping runtime: {p['min3']['runtime']['mapping_s']:.2f}s → {p['min1']['runtime']['mapping_s']:.2f}s; peak RSS {p['min3']['runtime']['peak_rss_mib']:.1f} → {p['min1']['runtime']['peak_rss_mib']:.1f} MiB. Launcher+adapter+viewer: {p['min3']['runtime']['launcher_adapter_viewer_s']:.2f}s → {p['min1']['runtime']['launcher_adapter_viewer_s']:.2f}s.", "",
              "Retained does not mean confirmed. All 67 observations remain unreviewed by human identity; evaluator IDs cover only the matched subset. `min=1` is still experimental and this is development evidence only.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    for arg in ("min3", "min1", "cache", "evaluator-matches", "room-snapshot", "pair-lock", "output"):
        parser.add_argument("--" + arg, type=Path, required=True)
    a = parser.parse_args()
    r = run_report(a.min3.resolve(), a.min1.resolve(), a.cache.resolve(), a.evaluator_matches.resolve(),
                   a.room_snapshot.resolve(), a.pair_lock.resolve(), a.output.resolve())
    print(json.dumps({"status": r["status"], "effective_config_delta": r["effective_config_delta"],
                      "prefilter_parity": r["prefilter_parity"],
                      "counts": {k: {x: v[x] for x in ("hypotheses_before_filter", "hypotheses_retained", "hypotheses_filtered", "objects_after_filter", "objects_after_merge_objects", "observations_retained_final")}
                                for k, v in r["profiles"].items()},
                      "evaluator_id": {k: v["evaluator_id"] for k, v in r["profiles"].items()},
                      "room_graph": {k: v["room_graph"] for k, v in r["profiles"].items()}, "output": str(a.output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
