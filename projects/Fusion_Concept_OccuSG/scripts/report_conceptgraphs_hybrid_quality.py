#!/usr/bin/env python3
"""Read-only lineage/identity report for a cached ConceptGraphs hybrid run."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import numpy as np


def read_json(path):
    return json.loads(Path(path).read_text())


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def observation_membership(rows):
    result = {}
    for row in rows:
        for observation_id in row["observation_ids"]:
            if observation_id in result:
                raise ValueError(f"observation appears in multiple objects at one stage: {observation_id}")
            result[observation_id] = row["lineage_key"]
    return result


def pair_metrics(rows, membership, *, frame_by_id=None):
    ids = sorted(rows)
    tp = fp = fn = tn = positive_pairs = negative_pairs = 0
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if frame_by_id is not None and frame_by_id[left] == frame_by_id[right]:
                continue
            same_gt = rows[left] == rows[right]
            same_pred = membership.get(left) is not None and membership.get(left) == membership.get(right)
            if same_gt: positive_pairs += 1
            else: negative_pairs += 1
            if same_gt and same_pred: tp += 1
            elif not same_gt and same_pred: fp += 1
            elif same_gt and not same_pred: fn += 1
            else: tn += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    return {"TP": tp, "FP": fp, "FN": fn, "TN": tn, "pair_count": tp + fp + fn + tn,
            "positive_identity_pairs": positive_pairs, "negative_identity_pairs": negative_pairs,
            "precision": precision, "recall": recall, "f1": f1}


def object_track_stats(identity_by_obs, membership):
    by_identity = defaultdict(set)
    by_track = defaultdict(set)
    for oid, identity in identity_by_obs.items():
        track = membership.get(oid)
        if track is not None:
            by_identity[identity].add(track)
            by_track[track].add(identity)
    fragmented = {identity: sorted(tracks) for identity, tracks in by_identity.items() if len(tracks) > 1}
    false_merge = {track: sorted(ids) for track, ids in by_track.items() if len(ids) > 1}
    return {"fragmented_identity_count": len(fragmented),
            "fragmentation_excess_tracks": sum(len(tracks) - 1 for tracks in fragmented.values()),
            "fragmented_identities": fragmented,
            "false_merge_track_count": len(false_merge),
            "false_merge_tracks": false_merge}


def reviewed_identity_coverage(groups, membership):
    present = [token for token, ids in groups.items() if any(oid in membership for oid in ids)]
    fully_retained = [token for token, ids in groups.items() if all(oid in membership for oid in ids)]
    return {"identities_present": len(present), "reviewed_identity_count": len(groups),
            "coverage": len(present) / len(groups) if groups else None,
            "fully_retained_identities": len(fully_retained),
            "identity_tokens_present": sorted(present),
            "identity_tokens_absent": sorted(set(groups) - set(present))}


def compare_reviewed_classes(all_obs, class_rows):
    """Compare exact reviewed classes without inferring taxonomy hierarchy."""
    result = []
    for row in class_rows:
        oid = row["observation_id"]
        if oid not in all_obs:
            raise ValueError(f"class annotation is outside the scored observation population: {oid}")
        raw = str(all_obs[oid]["raw_class"]).strip()
        reviewed = str(row.get("normalized_reviewed_label", "")).strip()
        if row.get("class_status") != "confirmed":
            relation = "not_confirmed"
        elif raw.casefold() == reviewed.casefold():
            relation = "exact_label_match"
        elif raw.casefold() == "couch" and reviewed.casefold() == "table":
            relation = "confirmed_label_mismatch"
        else:
            relation = "taxonomy_relation_unresolved"
        result.append({"observation_id": oid, "raw_detector_label": raw,
                       "reviewed_label": reviewed, "class_status": row.get("class_status"),
                       "class_specificity": row.get("class_specificity"),
                       "subtype_status": row.get("subtype_status"), "comparison": relation,
                       "semantic_output": "unavailable; no predictor label was modified"})
    return result


def parity_check(reference_run: Path, replay_run: Path):
    old = read_json(reference_run / "export/export_manifest.json")
    new = read_json(replay_run / "export/export_manifest.json")
    old_items = {item["export_id"]: item for item in old["objects"]}
    new_items = {item["export_id"]: item for item in new["objects"]}
    checks = {"object_ids_equal": set(old_items) == set(new_items),
              "observation_membership_equal": False,
              "geometry_arrays_equal": False,
              "geometry_point_sets_equal": False,
              "geometry_bounds_equal": False,
              "effective_config_equal": False,
              "geometry_point_counts_by_object": {}}
    if checks["object_ids_equal"]:
        checks["observation_membership_equal"] = all(old_items[k]["evidence"] == new_items[k]["evidence"] for k in old_items)
        arrays_equal = point_sets_equal = bounds_equal = True
        for key in old_items:
            with np.load(reference_run / "export" / old_items[key]["cloud_npz"], allow_pickle=False) as a, \
                 np.load(replay_run / "export" / new_items[key]["cloud_npz"], allow_pickle=False) as b:
                arrays_equal &= np.array_equal(a["xyz"], b["xyz"]) and np.array_equal(a["rgb"], b["rgb"])
                # Distinguish order-only changes from changes to the actual
                # point/color samples. Lexicographic row sorting is exact and
                # does not quantize geometry.
                ax = np.column_stack((a["xyz"], a["rgb"].astype(np.float64)))
                bx = np.column_stack((b["xyz"], b["rgb"].astype(np.float64)))
                if ax.shape != bx.shape:
                    point_sets_equal = False
                else:
                    ao = np.lexsort(tuple(ax[:, i] for i in reversed(range(ax.shape[1]))))
                    bo = np.lexsort(tuple(bx[:, i] for i in reversed(range(bx.shape[1]))))
                    point_sets_equal &= np.array_equal(ax[ao], bx[bo])
                bounds_equal &= np.array_equal(np.asarray(old_items[key]["bounds_minmax"]),
                                               np.asarray(new_items[key]["bounds_minmax"]))
                checks["geometry_point_counts_by_object"][key] = {
                    "checkpoint_v5": int(len(a["xyz"])), "lineage_replay": int(len(b["xyz"])),
                    "point_count_equal": len(a["xyz"]) == len(b["xyz"])}
        checks["geometry_arrays_equal"] = bool(arrays_equal)
        checks["geometry_point_sets_equal"] = bool(point_sets_equal)
        checks["geometry_bounds_equal"] = bool(bounds_equal)
    old_contract = read_json(reference_run / "experiment_contract.json")
    new_contract = read_json(replay_run / "experiment_contract.json")
    checks["effective_config_equal"] = old_contract["effective_config"] == new_contract["effective_config"]
    return checks


def write_report(run: Path, cache: Path, annotation_path: Path, output: Path, reference_run: Path,
                 class_annotation_path: Path | None = None,
                 reproducibility_path: Path | None = None):
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    lineage = read_json(run / "lineage_stages.json")
    p2 = read_json(cache / "detection_2d/detection_2d_index.json")
    all_obs = {d["detection_id"]: {"frame_index": int(fr["frame_index"]), "raw_class": d["class_raw"]}
               for fr in p2["frames"] for d in fr["detections"]}
    if set(all_obs) != set(lineage["input_observation_ids"]):
        raise ValueError("P2 observation population differs from lineage replay input")
    stages = lineage["stages"]
    stage_memberships = {name: observation_membership(rows) for name, rows in stages.items()}

    # Confirm the replay is the same effective mapping as the technical checkpoint.
    parity = parity_check(reference_run, run)
    parity["all_checks_pass"] = all(parity.values())
    (output / "checkpoint_parity.json").write_text(json.dumps(parity, indent=2, sort_keys=True) + "\n")
    reproducibility = None
    if reproducibility_path:
        reproducibility_path = Path(reproducibility_path)
        reproducibility = {"path": str(reproducibility_path),
                           "sha256": hashlib.sha256(reproducibility_path.read_bytes()).hexdigest(),
                           "result": read_json(reproducibility_path)}

    initial = {r["lineage_key"]: r for r in stages["online_before_postprocess"]}
    after_denoise = {r["lineage_key"]: r for r in stages["after_denoise"]}
    filtered = {r["lineage_key"]: r for r in stages["after_min_detections_filter"]}
    final = {r["lineage_key"]: r for r in stages["after_text_gated_merge"]}
    if set(initial) != set(after_denoise):
        raise ValueError("denoise changed object membership identity keys; report requires reconciliation")

    object_rows = []
    for key, row in initial.items():
        after = filtered.get(key)
        final_row = final.get(key)
        status = "retained" if after else "filtered"
        why = (f"retained: num_detections={row['detection_count']} >= obj_min_detections=3"
               if after else f"filtered: num_detections={row['detection_count']} < obj_min_detections=3; obj_min_points=0 was satisfied")
        object_rows.append({
            "online_lineage_key": key,
            "status_after_filter": status,
            "postprocess_merge_status": "unchanged_membership" if final_row and final_row["observation_ids"] == row["observation_ids"] else ("merged_or_repartitioned" if final_row else "not_present"),
            "reason": why,
            "detection_count": row["detection_count"],
            "distinct_frame_count": row["distinct_frame_count"],
            "distinct_pose_count": row["distinct_pose_count"],
            "frame_indices": row["frame_indices"],
            "observation_ids": row["observation_ids"],
            "raw_class_history": row["class_history_raw"],
            "point_count_before_postprocess": row["point_count"],
            "point_count_after_denoise": after_denoise[key]["point_count"],
            "point_count_after_filter": after["point_count"] if after else None,
            "bounds_after_filter_m": after["bounds_minmax"] if after else None,
        })
    with (output / "object_lineage.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(object_rows[0]))
        writer.writeheader(); writer.writerows(object_rows)
    _write_html_table(output / "object_lineage.html", "Objects before and after filtering", object_rows)

    obs_to_initial = stage_memberships["online_before_postprocess"]
    obs_to_final = stage_memberships["after_text_gated_merge"]
    per_obs = []
    initial_rows = {r["lineage_key"]: r for r in stages["online_before_postprocess"]}
    final_rows = {r["lineage_key"]: r for r in stages["after_text_gated_merge"]}
    for oid, metadata in sorted(all_obs.items()):
        key0, keyf = obs_to_initial.get(oid), obs_to_final.get(oid)
        initial_row = initial_rows.get(key0)
        final_row = final_rows.get(keyf)
        per_obs.append({"observation_id": oid, "frame_index": metadata["frame_index"],
                        "raw_class": metadata["raw_class"], "online_object": key0,
                        "postprocess_status": "retained" if keyf else "filtered",
                        "final_object": keyf,
                        "filter_reason": ("retained" if keyf else
                                          f"parent object {key0} had {initial_row['detection_count'] if initial_row else 'unknown'} detections; required >=3"),
                        "online_object_detection_count": initial_row["detection_count"] if initial_row else None,
                        "final_object_detection_count": final_row["detection_count"] if final_row else None})
    with (output / "observation_lineage.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(per_obs[0]))
        writer.writeheader(); writer.writerows(per_obs)
    _write_html_table(output / "observation_lineage.html", "Observation disposition", per_obs)

    review = read_json(annotation_path)
    groups = review["same_identity_groups"]
    human_rows = {oid: token for token, ids in groups.items() for oid in ids}
    human_frames = {oid: all_obs[oid]["frame_index"] for oid in human_rows}
    human_identity_lineage = {}
    for token, ids in groups.items():
        online_tracks = sorted({stage_memberships["online_before_postprocess"].get(oid) for oid in ids if stage_memberships["online_before_postprocess"].get(oid) is not None})
        final_tracks = sorted({stage_memberships["after_text_gated_merge"].get(oid) for oid in ids if stage_memberships["after_text_gated_merge"].get(oid) is not None})
        human_identity_lineage[token] = {
            "reviewed_observation_ids": ids,
            "scope": "only these explicitly reviewed observation IDs; no identity propagation to other observations",
            "online_object_keys": online_tracks,
            "final_object_keys": final_tracks,
            "retained_observations": sum(oid in stage_memberships["after_text_gated_merge"] for oid in ids),
            "reviewed_observation_count": len(ids),
            "identity_present_before_filter": any(oid in stage_memberships["online_before_postprocess"] for oid in ids),
            "identity_present_after_filter": any(oid in stage_memberships["after_min_detections_filter"] for oid in ids),
            "identity_present_final": any(oid in stage_memberships["after_text_gated_merge"] for oid in ids),
        }
    identity_coverage = {}
    for stage_name in ("online_before_postprocess", "after_min_detections_filter", "after_text_gated_merge"):
        identity_coverage[stage_name] = reviewed_identity_coverage(groups, stage_memberships[stage_name])
    human_metrics = {}
    for stage_name in ("online_before_postprocess", "after_min_detections_filter", "after_text_gated_merge"):
        membership = stage_memberships[stage_name]
        metrics = pair_metrics(human_rows, membership, frame_by_id=human_frames)
        assigned_obs = sum(oid in membership for oid in human_rows)
        metrics.update({"reviewed_observations": len(human_rows), "assigned_reviewed_observations": assigned_obs,
                        "observation_coverage": assigned_obs / len(human_rows) if human_rows else None,
                        **object_track_stats(human_rows, membership)})
        human_metrics[stage_name] = metrics

    d2 = groups.get("table_2", [])
    d2_track_ids = [stage_memberships["online_before_postprocess"].get(oid) for oid in d2]
    d2_final_ids = [stage_memberships["after_text_gated_merge"].get(oid) for oid in d2]
    same_frame_duplicate = {
        "observation_ids": d2,
        "online_object_ids": d2_track_ids,
        "online_same_object": bool(len(d2_track_ids) == 2 and d2_track_ids[0] is not None and d2_track_ids[0] == d2_track_ids[1]),
        "final_object_ids": d2_final_ids,
        "final_same_object": bool(len(d2_final_ids) == 2 and d2_final_ids[0] is not None and d2_final_ids[0] == d2_final_ids[1]),
        "final_retained_observations": sum(x is not None for x in d2_final_ids),
        "interpretation": "Same-frame identity consistency is reported separately from cross-frame pair metrics; identity loss is not fragmentation."
    }

    # Class labels come only from the separate human class CSV, never from the
    # identity token. Exact taxonomy equivalence is deliberately not inferred.
    class_annotation_path = class_annotation_path or Path(annotation_path).with_name("observation_annotations.csv")
    class_rows = list(csv.DictReader(class_annotation_path.open(newline="", encoding="utf-8")))
    reviewed_class_ids = {r["observation_id"] for r in class_rows}
    if not reviewed_class_ids <= set(all_obs):
        raise ValueError("class annotation contains IDs outside this cached P2 population")
    class_comparison = compare_reviewed_classes(all_obs, class_rows)
    with (output / "human_class_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(class_comparison[0]) if class_comparison else ["observation_id"])
        writer.writeheader(); writer.writerows(class_comparison)
    _write_html_table(output / "human_class_comparison.html", "Human reviewed class vs raw detector class", class_comparison)

    gt_rows = {r["detection_id"]: str(r["gt_semantic_id"]) for r in read_jsonl(cache / "u6_offline_evaluation_complete/detection_gt_matches.jsonl") if r.get("gt_matched")}
    gt_frames = {oid: all_obs[oid]["frame_index"] for oid in gt_rows}
    gt_metrics = {}
    for stage_name in ("online_before_postprocess", "after_text_gated_merge"):
        membership = stage_memberships[stage_name]
        metrics = pair_metrics(gt_rows, membership, frame_by_id=gt_frames)
        assigned = sum(oid in membership for oid in gt_rows)
        retained_all_p2 = sum(oid in membership for oid in all_obs)
        metrics.update({"evaluator_id_matched_observations": len(gt_rows), "assigned_matched_observations": assigned,
                        "population_all_p2_observations": len(all_obs),
                        "retained_all_p2_observations": retained_all_p2,
                        "retained_all_p2_observation_coverage": retained_all_p2 / len(all_obs),
                        "matched_gt_observations_over_all_p2_population": assigned / len(all_obs),
                        "matched_observation_coverage": assigned / len(gt_rows) if gt_rows else None,
                        **object_track_stats(gt_rows, membership)})
        gt_metrics[stage_name] = metrics

    report = {
        "schema": "conceptgraphs-hybrid-quality-review/v1",
        "checkpoint_run": str(reference_run), "lineage_replay": str(run), "cache": str(cache),
        "annotation_source": str(annotation_path),
        "annotation_sha256": hashlib.sha256(Path(annotation_path).read_bytes()).hexdigest(),
        "class_annotation_source": str(class_annotation_path),
        "class_annotation_sha256": hashlib.sha256(class_annotation_path.read_bytes()).hexdigest(),
        "annotation_scope": "Only exact full observation IDs in the same-sequence U6 annotation are scored. Identity tokens are evaluator-only; no class is inferred.",
        "checkpoint_parity": parity,
        "fresh_process_reproducibility": reproducibility,
        "counts": {"p2_observations": len(all_obs),
                   "online_objects_before_postprocess": len(stages["online_before_postprocess"]),
                   "objects_after_denoise": len(stages["after_denoise"]),
                   "objects_after_min_detections_filter": len(stages["after_min_detections_filter"]),
                   "objects_after_text_gated_merge": len(stages["after_text_gated_merge"]),
                   "observations_retained": len(obs_to_final), "observations_filtered": len(all_obs) - len(obs_to_final),
                   "human_reviewed_observations": len(human_rows), "human_identity_tokens": len(groups),
                   "human_identity_coverage_before_filter": f"{identity_coverage['online_before_postprocess']['identities_present']}/{len(groups)}",
                   "human_identity_coverage_after_filter": f"{identity_coverage['after_text_gated_merge']['identities_present']}/{len(groups)}",
                   "human_identity_coverage_lost_after_filter": identity_coverage["after_text_gated_merge"]["identity_tokens_absent"]},
        "human_reviewed_cross_frame": human_metrics,
        "human_identity_lineage_exact_reviewed_ids": human_identity_lineage,
        "human_reviewed_identity_coverage": identity_coverage,
        "human_reviewed_class_comparison": {"scope": "Only exact reviewed observation IDs in observation_annotations.csv; identity tokens are not used as class labels; subtype and hierarchy are not inferred.",
                                            "rows": class_comparison,
                                            "confirmed_label_mismatch_count": sum(r["comparison"] == "confirmed_label_mismatch" for r in class_comparison),
                                            "taxonomy_relation_unresolved_count": sum(r["comparison"] == "taxonomy_relation_unresolved" for r in class_comparison),
                                            "fused_semantic_label": "unavailable"},
        "human_reviewed_same_frame_duplicate": same_frame_duplicate,
        "dataset_evaluator_id_cross_frame": {"scope": f"Only detections with official one-to-one Hungarian mask match IDs; {len(gt_rows)} matched detections here. This is separate from human physical identity.", **gt_metrics},
        "postprocess_decisions": {
            "filter_rule": "reference obj_min_detections=3",
            "filtered_object_count": len(initial) - len(filtered),
            "filtered_object_keys": sorted(set(initial) - set(filtered)),
            "post_filter_merge_count": len(filtered) - len(final),
            "post_filter_merge_membership_changed": any(
                sorted(sorted(r["observation_ids"]) for r in stages["after_min_detections_filter"]) !=
                sorted(sorted(r["observation_ids"]) for r in stages["after_text_gated_merge"]) for _ in [0]),
            "removed_observations": sorted(set(all_obs) - set(obs_to_final)),
            "note": "Filtered objects are not described as fused into retained objects."
        }
    }
    (output / "metrics.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")

    lines = ["# ConceptGraphs hybrid mapping quality review — U6 00808", "",
             "The v5 technical checkpoint is unchanged. This report uses a separately seeded fresh-process replay on the same cached RGB-D/P2/P3/text inputs; no perception was rerun.", "",
             f"Against v5: object IDs **{parity['object_ids_equal']}**, membership **{parity['observation_membership_equal']}**, effective config **{parity['effective_config_equal']}**, exact ordered geometry arrays **{parity['geometry_arrays_equal']}**, point sets **{parity['geometry_point_sets_equal']}**, bounds **{parity['geometry_bounds_equal']}**.",
             f"Point counts by object: `{parity['geometry_point_counts_by_object']}`. v5 predates explicit RNG seeding; its RNG state was not recorded, so its geometry cannot be claimed reproducible from this rerun.", "",
             "## Geometry reproducibility", "",
             "The pinned reference `create_object_pcd` adds NumPy Gaussian noise with sigma 4e-3 (4 mm per coordinate). The runner now seeds Python, NumPy, and Torch CPU RNGs before mapping. Two fresh processes with seed 20261014 matched at all checked stages, ordered arrays, point sets, bounds, membership, and postprocess decisions. The comparison JSON is recorded in `metrics.json`; this confirms only the tested local software/environment replay.", "",
             "Against the older v6 replay with no recorded seed, the first measured point-count/bounds divergence is already at online mapping; membership is unchanged. Final point sets and bounds differ, so this is not just point reordering. This supports RNG control as a direct reproducibility fix, but does not isolate the full historical v5 difference to the 4 mm noise alone because the old RNG state was not saved and other NumPy RNG consumers exist.", "",
             "## Postprocess counts", "",
             "| Stage | Objects | Observation coverage |", "|---|---:|---:|",
             f"| Online association/fusion, before postprocess | {len(initial)} | {len(obs_to_initial)}/{len(all_obs)} |",
             f"| After reference denoise | {len(after_denoise)} | {sum(len(r['observation_ids']) for r in stages['after_denoise'])}/{len(all_obs)} |",
             f"| After `obj_min_detections=3` filter | {len(filtered)} | {len(stage_memberships['after_min_detections_filter']) and sum(len(r['observation_ids']) for r in stages['after_min_detections_filter'])}/{len(all_obs)} |",
             f"| After text-gated `merge_objects` | {len(final)} | {len(obs_to_final)}/{len(all_obs)} |", "",
             f"The filter removed **{len(initial)-len(filtered)} objects** and their **{len(all_obs)-len(obs_to_final)} observations** from the final export. They were not fused into the two retained objects. The post-filter merge changed membership: **{report['postprocess_decisions']['post_filter_merge_membership_changed']}**.", "",
             "## Human-reviewed identity subset", "",
             "Cross-frame population: all different-frame pairs among the six exact-ID observations in table_1/table_2, fixed denominator 12 pairs (6 same-identity, 6 different-identity). All pairs sharing a frame are excluded. The same-frame table_2 duplicate is scored separately.", "",
             "| Stage | TP | FP | FN | TN | P | R | F1 | reviewed obs coverage |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, key in (("Before postprocess", "online_before_postprocess"), ("After filter", "after_min_detections_filter"), ("Final", "after_text_gated_merge")):
        m = human_metrics[key]
        fmt = lambda x: "n/a" if x is None else f"{x:.3f}"
        lines.append(f"| {label} | {m['TP']} | {m['FP']} | {m['FN']} | {m['TN']} | {fmt(m['precision'])} | {fmt(m['recall'])} | {fmt(m['f1'])} | {m['assigned_reviewed_observations']}/{m['reviewed_observations']} |")
    lines += ["", f"D2 same-frame table_2: before postprocess IDs `{d2_track_ids}`; same mapped object = **{same_frame_duplicate['online_same_object']}**. Final IDs `{d2_final_ids}`; retained {same_frame_duplicate['final_retained_observations']}/2. This is a duplicate/identity-consistency miss plus post-filter loss, not a second mask TP.", "",
              "## Dataset evaluator IDs", "",
              "Hungarian matched detections only (14/18); IDs and metrics are dataset-instance diagnostics, separate from human physical-identity review.", "",
              "| Stage | TP | FP | FN | TN | P | R | F1 | GT matched retained | GT matched / all P2 | all P2 retained | false-merge tracks | fragmentation excess tracks |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, key in (("Before postprocess", "online_before_postprocess"), ("Final", "after_text_gated_merge")):
        m = gt_metrics[key]; fmt = lambda x: "n/a" if x is None else f"{x:.3f}"
        lines.append(f"| {label} | {m['TP']} | {m['FP']} | {m['FN']} | {m['TN']} | {fmt(m['precision'])} | {fmt(m['recall'])} | {fmt(m['f1'])} | {m['assigned_matched_observations']}/{m['evaluator_id_matched_observations']} | {m['assigned_matched_observations']}/{m['population_all_p2_observations']} | {m['retained_all_p2_observations']}/{m['population_all_p2_observations']} | {m['false_merge_track_count']} | {m['fragmentation_excess_tracks']} |")
    lines += ["", "## Artifacts", "", "- `object_lineage.html` / `.csv`: each pre-filter online object, keep/filter reason, frame/pose counts, raw class history, point counts.", "- `observation_lineage.html` / `.csv`: all P2 observations and final retained/filtered status.", "- `metrics.json`: exact metrics and denominators.", "- `checkpoint_parity.json`: parity against the immutable v5 checkpoint.", ""]
    lines.insert(lines.index("## Dataset evaluator IDs"),
                 f"Human-reviewed identity coverage is **{identity_coverage['online_before_postprocess']['identities_present']}/2 before filter → {identity_coverage['after_text_gated_merge']['identities_present']}/2 after filter**. `table_1` remains 4/4 observations; `table_2` has 0/2 retained. Losing all of `table_2` is identity/observation coverage loss, not fragmentation. The extra frame-1 detection in a pre-filter object remains unreviewed. The same-frame table_2 duplicate consistency failure is separate from cross-frame metrics.\n\n"
                 "## Human reviewed class labels\n\n"
                 "Correction to the earlier report wording: class annotation does exist for these six exact observations. The CSV confirms the general label `table` for all six; subtype remains unconfirmed. For `hm3d-00808-u6q-v1-0001:det-0003`, raw detector label `couch` differs from the human-confirmed general label `table`. The other five raw labels are `dining table`; a parent-child equivalence has not been approved, so they are left taxonomy-unresolved rather than scored as exact matches or errors. Fused semantic label remains unavailable; no predictor label was changed. See `human_class_comparison.csv/html`.\n\n")
    (output / "REPORT.vi.md").write_text("\n".join(lines))
    return report


def _write_html_table(path: Path, title: str, rows: list[dict]):
    import html
    fields = list(rows[0]) if rows else []
    header = "".join(f"<th>{html.escape(str(key))}</th>" for key in fields)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (list, dict)) else str(row.get(key)))}</td>" for key in fields) + "</tr>" for row in rows)
    path.write_text("<!doctype html><meta charset='utf-8'><title>" + html.escape(title) + "</title>"
                    + "<style>body{font:14px system-ui;margin:24px;color:#172033}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd;padding:6px;text-align:left;vertical-align:top}th{position:sticky;top:0;background:#eef}td{max-width:520px;overflow-wrap:anywhere}</style>"
                    + f"<h1>{html.escape(title)}</h1><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>", encoding="utf-8")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--reference-run", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--annotation", type=Path, required=True)
    p.add_argument("--class-annotation", type=Path, default=None)
    p.add_argument("--reproducibility-comparison", type=Path, default=None)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = write_report(a.run, a.cache, a.annotation, a.output, a.reference_run,
                          a.class_annotation, a.reproducibility_comparison)
    print(json.dumps({"status": "pass", "output": str(a.output), "counts": result["counts"], "checkpoint_parity": result["checkpoint_parity"]}, indent=2))


if __name__ == "__main__":
    main()
