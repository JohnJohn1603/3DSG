#!/usr/bin/env python3
"""Compare two fresh-process cached ConceptGraphs mapping replays."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def _rows_by_lineage(rows):
    return {row["lineage_key"]: row for row in rows}


def arrays_equal_as_point_sets(a_xyz, a_rgb, b_xyz, b_rgb):
    a = np.column_stack((np.asarray(a_xyz), np.asarray(a_rgb, dtype=np.float64)))
    b = np.column_stack((np.asarray(b_xyz), np.asarray(b_rgb, dtype=np.float64)))
    if a.shape != b.shape:
        return False
    ao = np.lexsort(tuple(a[:, i] for i in reversed(range(a.shape[1]))))
    bo = np.lexsort(tuple(b[:, i] for i in reversed(range(b.shape[1]))))
    return bool(np.array_equal(a[ao], b[bo]))


def compare_runs(run_a: Path, run_b: Path, *, allow_unseeded_history=False):
    run_a, run_b = Path(run_a), Path(run_b)
    ma, mb = json.loads((run_a / "run_manifest.json").read_text()), json.loads((run_b / "run_manifest.json").read_text())
    ca, cb = json.loads((run_a / "experiment_contract.json").read_text()), json.loads((run_b / "experiment_contract.json").read_text())
    la = json.loads((run_a / "lineage_stages.json").read_text())
    lb = json.loads((run_b / "lineage_stages.json").read_text())
    if ca["input_observation_ids"] != cb["input_observation_ids"]:
        raise ValueError("input observation order differs")
    if ca["p2_index_sha256"] != cb["p2_index_sha256"] or ca["capture_manifest_sha256"] != cb["capture_manifest_sha256"]:
        raise ValueError("input cache hashes differ")
    if ca["effective_config"] != cb["effective_config"]:
        raise ValueError("effective mapping config differs")
    seed_a = ca.get("rng_control", {}).get("seed")
    seed_b = cb.get("rng_control", {}).get("seed")
    same_seed = seed_a == seed_b if seed_a is not None and seed_b is not None else None
    if same_seed is False:
        raise ValueError("run seeds differ")
    if same_seed is None and not allow_unseeded_history:
        raise ValueError("seedless legacy replay requires --allow-unseeded-history-comparison")

    stage_results = {}
    stage_order = ("online_before_postprocess", "after_denoise",
                   "after_min_detections_filter", "after_text_gated_merge")
    if tuple(la["stages"]) != tuple(lb["stages"]):
        raise ValueError("lineage stages differ")
    for stage in stage_order:
        if stage not in la["stages"]:
            raise ValueError(f"missing lineage stage: {stage}")
        ra, rb = _rows_by_lineage(la["stages"][stage]), _rows_by_lineage(lb["stages"][stage])
        same_keys = set(ra) == set(rb)
        same_membership = same_keys and all(sorted(ra[k]["observation_ids"]) == sorted(rb[k]["observation_ids"]) for k in ra)
        same_count_bounds = same_keys and all(
            ra[k]["point_count"] == rb[k]["point_count"] and ra[k]["bounds_minmax"] == rb[k]["bounds_minmax"]
            for k in ra)
        stage_results[stage] = {"object_membership_equal": same_membership,
                                "point_count_and_bounds_equal": same_count_bounds,
                                "object_count_a": len(ra), "object_count_b": len(rb)}

    ea = json.loads((run_a / "export/export_manifest.json").read_text())
    eb = json.loads((run_b / "export/export_manifest.json").read_text())
    oa = {x["export_id"]: x for x in ea["objects"]}
    ob = {x["export_id"]: x for x in eb["objects"]}
    id_equal = set(oa) == set(ob)
    membership_equal = id_equal and all(oa[k]["evidence"] == ob[k]["evidence"] for k in oa)
    geom = {}
    ordered_equal = point_sets_equal = bounds_equal = True
    if id_equal:
        for key in sorted(oa):
            with np.load(run_a / "export" / oa[key]["cloud_npz"], allow_pickle=False) as a, \
                 np.load(run_b / "export" / ob[key]["cloud_npz"], allow_pickle=False) as b:
                xyz_equal = np.array_equal(a["xyz"], b["xyz"])
                rgb_equal = np.array_equal(a["rgb"], b["rgb"])
                pointset_equal = arrays_equal_as_point_sets(a["xyz"], a["rgb"], b["xyz"], b["rgb"])
                bound_equal = oa[key]["bounds_minmax"] == ob[key]["bounds_minmax"]
                ordered_equal &= xyz_equal and rgb_equal
                point_sets_equal &= pointset_equal
                bounds_equal &= bound_equal
                geom[key] = {"point_count_a": len(a["xyz"]), "point_count_b": len(b["xyz"]),
                             "xyz_ordered_equal": xyz_equal, "rgb_ordered_equal": rgb_equal,
                             "xyz_rgb_point_set_equal": pointset_equal, "bounds_equal": bound_equal}
    first_stage_divergence = next((s for s, r in stage_results.items()
                                   if not r["object_membership_equal"] or not r["point_count_and_bounds_equal"]), None)
    checks = {"same_input_hashes_order_config": True,
              "seed_a": seed_a, "seed_b": seed_b, "same_seed": same_seed,
              "stage_results": stage_results,
              "first_divergent_stage": first_stage_divergence,
              "export_object_ids_equal": id_equal,
              "export_observation_membership_equal": membership_equal,
              "export_ordered_xyz_rgb_arrays_equal": ordered_equal if id_equal else False,
              "export_xyz_rgb_point_sets_equal": point_sets_equal if id_equal else False,
              "export_bounds_equal": bounds_equal if id_equal else False,
              "geometry_by_object": geom,
              "postprocess_counts_equal": all(ma.get(k) == mb.get(k) for k in (
                  "foreground_objects_before_postprocess", "objects_after_filter", "final_objects_after_text_gated_merge"))}
    checks["replay_parity_pass"] = (same_seed is True and first_stage_divergence is None and id_equal and membership_equal and
                                    ordered_equal and point_sets_equal and bounds_equal and checks["postprocess_counts_equal"])
    return checks


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-a", type=Path, required=True)
    p.add_argument("--run-b", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--allow-unseeded-history-comparison", action="store_true",
                   help="Permit comparison to an older replay with no recorded RNG seed; parity cannot pass.")
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    result = compare_runs(a.run_a, a.run_b, allow_unseeded_history=a.allow_unseeded_history_comparison)
    result.update({"run_a": str(a.run_a), "run_b": str(a.run_b)})
    a.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"replay_parity_pass": result["replay_parity_pass"],
                      "first_divergent_stage": result["first_divergent_stage"], "output": str(a.output)}, indent=2))


if __name__ == "__main__":
    main()
