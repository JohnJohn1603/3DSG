#!/usr/bin/env python3
"""Paired offline Q2 matcher-representation ablation on locked cached inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.run_u3_surface_support_candidate import (
    ROOT, load_locked_inputs, read_json, read_jsonl,
    run_predictor, sha,
)
from scripts.score_q1_00808_reviewed_subset import score as score_human_subset
from standalone_sg.objects.profile_comparison import pairwise_identity_metrics, partition_delta, relation_changes


def _human_annotations(path: Path | None, population: set[str]) -> tuple[dict[str, str], dict]:
    if path is None:
        return {}, {"status": "unavailable_no_human_annotation_file"}
    data = json.loads(path.read_text(encoding="utf-8"))
    if "annotations" in data:
        rows = data["annotations"]
    else:
        rows = data.get("records", [])
    labels = {str(row.get("observation_id")): str(row.get("identity_token", row.get("physical_identity_token")))
              for row in rows if row.get("identity_status") == "confirmed"
              and row.get("observation_id") and row.get("identity_token", row.get("physical_identity_token"))}
    out_of_scope = sorted(set(labels) - population)
    if out_of_scope:
        raise ValueError(f"human annotation contains IDs outside locked cache population: {out_of_scope}")
    return labels, {"status": "available" if labels else "unavailable_no_confirmed_identity_labels",
                    "annotation_file": str(path), "sha256": sha(path),
                    "confirmed_observations": len(labels),
                    "confirmed_identity_tokens": len(set(labels.values()))}


def _assignments(path: Path) -> dict[str, str | None]:
    return {row["observation_id"]: row.get("track_id") for row in read_jsonl(path)}


def _evaluator_ids(input_root: Path, population: set[str]) -> dict[str, str | None]:
    paths = sorted((input_root / "object_evaluations").glob("eval_*/observation_matches.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return {}
    rows = read_jsonl(paths[0])
    result = {oid: None for oid in population}
    for row in rows:
        oid = str(row.get("detection_id"))
        if oid in population and row.get("match_status") == "matched" and row.get("gt_semantic_id") is not None:
            result[oid] = str(row["gt_semantic_id"])
    return result


def _eligibility_changes(k4_path: Path, q2_path: Path) -> list[dict]:
    def load(path: Path) -> dict[tuple[str, tuple[str, ...]], dict]:
        result = {}
        for row in read_jsonl(path):
            prior = row.get("prior_geometry_evidence_ids", row.get("prior_evidence_ids", []))
            result[(row["detection_id"], tuple(sorted(prior)))] = row
        return result

    before, after = load(k4_path), load(q2_path)
    changes = []
    for key in sorted(set(before) | set(after)):
        left, right = before.get(key), after.get(key)
        if left is None or right is None:
            changes.append({"observation_id": key[0], "prior_track_evidence_ids": list(key[1]),
                            "comparison": "no_shared_prior_track_membership", "k4_row_present": left is not None,
                            "q2_row_present": right is not None})
            continue
        states = {}
        for name, row in (("k4", left), ("q2", right)):
            dist = row.get("center_distance_m")
            iou = row.get("aabb_iou")
            states[name] = {
                "center_distance_m": dist,
                "radius_pass": None if dist is None else dist <= 0.75,
                "aabb_iou": iou,
                "iou_prefilter_pass": None if iou is None else iou >= 0.01,
                "iou_acceptance_pass": None if iou is None else iou >= 0.05,
                "eligible": row.get("eligible"),
                "gate_failures": row.get("gate_failures", []),
            }
        if any(states["k4"][key_name] != states["q2"][key_name]
               for key_name in ("radius_pass", "iou_prefilter_pass", "iou_acceptance_pass", "eligible")):
            changes.append({"observation_id": key[0], "prior_track_evidence_ids": list(key[1]),
                            "comparison": "shared_prior_track_pair", "states": states})
    return changes


def _load_case_annotation(path: Path | None, labels: dict[str, str]) -> dict[str, list[str]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: dict[str, list[str]] = {}
    for row in data.get("records", []):
        oid = str(row.get("observation_id", ""))
        case = str(row.get("case_id", ""))
        if case and oid in labels:
            cases.setdefault(case, []).append(oid)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-run", required=True, type=Path)
    parser.add_argument("--p3-output", action="append", required=True, type=Path)
    parser.add_argument("--k4-reference", required=True, type=Path)
    parser.add_argument("--human-annotations", type=Path)
    parser.add_argument("--case-annotations", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    output = args.output.resolve()
    output.mkdir(parents=True)
    config = ROOT / "configs/association/geometry_only_accumulated_surface_v1.json"
    fusion_config = ROOT / "configs/fusion/object_geometry_v2.json"
    q2_config = ROOT / "configs/association/q2_prior_surface_bounds_v1.json"
    started = time.perf_counter()
    inputs = load_locked_inputs(args.input_run.resolve(), [p.resolve() for p in args.p3_output], None, None,
                                config, fusion_config, args.k4_reference.resolve())
    population = set(inputs["raw_ids"])
    human_labels, human_meta = _human_annotations(args.human_annotations, population)
    if human_labels:
        human_meta["population_observations"] = len(population)
        human_meta["coverage_ratio"] = len(human_labels) / len(population)
    lock = {
        "schema_version": 1,
        "status": "locked_before_paired_prediction",
        "input_run": str(args.input_run.resolve()),
        "input_hashes": inputs["input_hashes"],
        "p3_evidence_manifests": inputs["p3_manifest_hashes"],
        "k4_reference": str(args.k4_reference.resolve()),
        "k4_reference_sha256": sha(args.k4_reference.resolve()),
        "k4_config_sha256": sha(config),
        "fusion_config_sha256": sha(fusion_config),
        "q2_config_sha256": sha(q2_config),
        "predictor_code_hashes": {
            str(path.relative_to(ROOT)): sha(path) for path in (
                ROOT / "scripts/run_u3_surface_support_candidate.py",
                ROOT / "src/standalone_sg/objects/u3_experimental.py",
                ROOT / "src/standalone_sg/objects/object_geometry_fusion.py")
        },
        "human_annotation_used_by_predictor": False,
        "evaluator_used_by_predictor": False,
        "association_controls": ["source", "geometry-only-dedup-v2", "K4-current"],
        "candidate": "K4 current with only prior matcher center/extents replaced by min/max AABB of prior accumulated voxel representatives",
        "threshold_tuning": False,
    }
    (output / "pre_score_lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")

    baseline_input = dict(inputs)
    k4_output = output / "k4_current"
    k4_manifest = run_predictor(args.input_run.resolve(), baseline_input, k4_output,
                                representation_mode="k4_current")
    q2_input = dict(inputs)
    q2_input["k4_reference_assignments"] = None
    q2_output = output / "q2_prior_surface_bounds"
    q2_manifest = run_predictor(args.input_run.resolve(), q2_input, q2_output,
                                representation_mode="q2_prior_surface_bounds")

    modes = {
        "source": _assignments(k4_output / "source_assignments.jsonl"),
        "geometry-only-dedup-v2": _assignments(k4_output / "geometry_only_dedup_v2_assignments.jsonl"),
        "K4-current": _assignments(k4_output / "candidate_assignments.jsonl"),
        "Q2-prior-surface-bounds": _assignments(q2_output / "candidate_assignments.jsonl"),
    }
    for control_name, filename in (("source", "source_assignments.jsonl"),
                                   ("geometry-only-dedup-v2", "geometry_only_dedup_v2_assignments.jsonl")):
        paired = _assignments(q2_output / filename)
        if modes[control_name] != paired:
            raise RuntimeError(f"paired {control_name} control assignments differ across representation runs")
    if k4_manifest["reference_parity_status"] == "mismatch":
        raise RuntimeError("K4 current assignments do not match the supplied locked K4 reference")

    evaluator_ids = _evaluator_ids(args.input_run.resolve(), population)
    evaluator_results = {}
    if evaluator_ids and set(evaluator_ids) == population:
        evaluator_results = {
            mode: pairwise_identity_metrics(assignments, evaluator_ids, eligible_ids=population)
            for mode, assignments in modes.items()
        }
        # The same pair scorer is used; label provenance is evaluator IDs, not human identity.
        for result in evaluator_results.values():
            result["identity_source"] = "HM3D one-to-one evaluator instance IDs"
    else:
        evaluator_results = {mode: {"status": "unavailable_no_complete_evaluator_identity_ledger"}
                             for mode in modes}

    human_results = {}
    if human_labels:
        human_ids = sorted(human_labels)
        human_results = {mode: score_human_subset(assignments, human_labels, human_ids)
                         for mode, assignments in modes.items()}
        for result in human_results.values():
            result["identity_source"] = "user-reviewed physical identity; evaluator only"

    case_results = {}
    for case, case_ids in _load_case_annotation(args.case_annotations, human_labels).items():
        if len(case_ids) >= 2:
            labels = {oid: human_labels[oid] for oid in case_ids}
            case_results[case] = {mode: score_human_subset(assignments, labels, sorted(case_ids))
                                  for mode, assignments in modes.items()}

    k4_to_q2 = relation_changes(modes["K4-current"], modes["Q2-prior-surface-bounds"], sorted(population))
    partition = partition_delta(modes["K4-current"], modes["Q2-prior-surface-bounds"], sorted(population))
    k4_aliases = read_jsonl(k4_output / "candidate_aliases.jsonl")
    q2_aliases = read_jsonl(q2_output / "candidate_aliases.jsonl")
    if k4_aliases != q2_aliases:
        raise RuntimeError("Q2 candidate changed same-frame dedup/canonical ownership")
    changed_eligibility = _eligibility_changes(k4_output / "candidate_ledger.jsonl",
                                               q2_output / "candidate_ledger.jsonl")

    def decision_counts(path: Path) -> dict:
        rows = read_jsonl(path)
        return {"deferred": sum(row.get("decision") in {"defer", "deferred"} for row in rows),
                "unassigned": sum(row.get("track_id") is None for row in rows)}

    decision_paths = {
        "source": k4_output / "source_assignments.jsonl",
        "geometry-only-dedup-v2": k4_output / "geometry_only_dedup_v2_assignments.jsonl",
        "K4-current": k4_output / "candidate_assignments.jsonl",
        "Q2-prior-surface-bounds": q2_output / "candidate_assignments.jsonl",
    }
    mode_decision_counts = {mode: decision_counts(path) for mode, path in decision_paths.items()}

    metrics = {
        "schema_version": 1,
        "status": "completed",
        "development_not_holdout": True,
        "input_run_id": args.input_run.name,
        "input_hashes": inputs["input_hashes"],
        "p3_evidence_manifests": inputs["p3_manifest_hashes"],
        "pre_score_lock_sha256": sha(output / "pre_score_lock.json"),
        "candidate_config_sha256": sha(q2_config),
        "candidate_config": str(q2_config),
        "controls_recomputed_on_same_inputs": True,
        "source_and_dedup_controls_equal_between_paired_runs": True,
        "k4_reference_partition_parity": k4_manifest["reference_parity"].get("accumulated-surface-support", {}),
        "human_annotation": human_meta,
        "evaluator_id_metrics": evaluator_results,
        "decision_counts": mode_decision_counts,
        "human_reviewed_metrics": human_results if human_results else {"status": "unavailable"},
        "human_case_metrics": case_results if case_results else {"status": "unavailable_for_cache_or_annotations"},
        "partition_delta_k4_to_q2": partition,
        "changed_relations_k4_to_q2": k4_to_q2,
        "changed_candidate_radius_iou_pairs": changed_eligibility,
        "same_frame_aliases_equal": True,
        "k4": {"continuity_matches": k4_manifest["continuity_matches"],
               "deferred": k4_manifest["deferred_count"],
               "aliases": k4_manifest["dedup_alias_count"],
               "runtime_seconds": k4_manifest["causal_replay_runtime_seconds"],
               "peak_process_rss_mib": k4_manifest["peak_process_rss_mib"]},
        "q2_candidate": {"continuity_matches": q2_manifest["continuity_matches"],
                         "deferred": q2_manifest["deferred_count"],
                         "aliases": q2_manifest["dedup_alias_count"],
                         "runtime_seconds": q2_manifest["causal_replay_runtime_seconds"],
                         "peak_process_rss_mib": q2_manifest["peak_process_rss_mib"]},
        "runtime_scope": "offline paired association using cached capture/P2/P3 and selected-point sidecars; no capture, detector, P3, or native replay",
        "paired_wall_seconds": time.perf_counter() - started,
        "paired_process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "R23": {"present_in_this_input": "hm3d-00802-main-dense-0235:det-0001" in population,
                "dedup_v2_aliases_unchanged": True,
                "interpretation": "R23 is a same-frame dedup regression outside the 29-frame main cache; dedup code/config is identical, not scored as a new Q2 success"},
        "known_touching_surface_failure": "K4 surface-continuity synthetic counterexample remains a known failure; its regression test reproduces the failure and is not a safety-pass claim",
        "limitations": ["Q2 is a representation ablation on development caches only", "evaluator IDs are not human physical-identity truth", "Q1 00808 human review covers seven observations only", "unreviewed changed relations are not assumed correct", "no M9, graph class, room scores, or default changed"],
    }
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    report = [f"# Q2 prior accumulated-surface bounds ablation — {args.input_run.name}", "",
              "Development only; no capture, detector, P3 clustering, or native replay was run.",
              f"Input hashes: `{json.dumps(inputs['input_hashes'], sort_keys=True)}`.",
              f"Candidate config SHA-256: `{sha(q2_config)}`.",
              f"Paired offline wall time: {metrics['paired_wall_seconds']:.2f}s; peak RSS: {metrics['paired_process_peak_rss_mib']:.1f} MiB.", "",
              "## Evaluator-ID metrics", "",
              "| Mode | TP/FP/FN | Precision / Recall / F1 | False-merge tracks | Fragmentation | Deferred / unassigned | Coverage |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for mode, result in evaluator_results.items():
        if "pairwise_tp" not in result:
            report.append(f"| {mode} | N/A | N/A | N/A | N/A | N/A | N/A |")
        else:
            counts = mode_decision_counts[mode]
            report.append(f"| {mode} | {result['pairwise_tp']}/{result['pairwise_fp']}/{result['pairwise_fn']} | {result['association_precision']}/{result['association_recall']}/{result['association_f1']} | {result['false_merge_tracks']} | {result['fragmented_evaluator_instances']} | {counts['deferred']} / {counts['unassigned']} | {result['evaluator_matched_observations_scored']}/{result['eligible_observations']} |")
    report += ["", "## Human-reviewed population", ""]
    if human_results:
        report.append(f"{human_meta['confirmed_observations']}/{human_meta['population_observations']} P3 observations ({human_meta['coverage_ratio']:.1%}); {human_meta['confirmed_identity_tokens']} identity tokens; exact full IDs only. Class was not inferred from identity tokens.")
        report.extend(["", "| Mode | TP/FP/FN | Precision / Recall / F1 | False merge | Fragmentation | Unassigned |",
                       "|---|---:|---:|---:|---:|---:|"])
        for mode, result in human_results.items():
            report.append(f"| {mode} | {result['TP']}/{result['FP']}/{result['FN']} | {result['precision']}/{result['recall']}/{result['F1']} | {result['false_merge_tracks']} | {result['fragmented_identity_count']} | {result['unassigned_observations']} |")
    else:
        report.append("Unavailable for this input; no compatible human annotation was supplied.")
    report += ["", "## K4 versus Q2 representation changes", "",
               f"Pairwise partition changes: {partition['identity_relation_changes']}; ID-renaming-only groups: {partition['track_id_renamed_only_groups']}. Radius/IoU eligibility rows changed: {len(changed_eligibility)}. Same-frame aliases identical: {k4_aliases == q2_aliases}.",
               f"K4 continuity matches={k4_manifest['continuity_matches']}; Q2 continuity matches={q2_manifest['continuity_matches']}. The Q2 change affects only prior matcher geometry; surface fallback policy and thresholds remain K4.", ""]
    for row in k4_to_q2:
        report.append(f"- Relation `{row['transition']}`: `{row['observation_ids'][0]}` ↔ `{row['observation_ids'][1]}`.")
    if not k4_to_q2:
        report.append("- No cross-frame relation changed between K4 and Q2.")
    for row in changed_eligibility:
        if row.get("comparison") == "shared_prior_track_pair":
            left, right = row["states"]["k4"], row["states"]["q2"]
            report.append(f"- Gate-only change for `{row['observation_id']}` against prior evidence {row['prior_track_evidence_ids']}: center-radius pass {left['radius_pass']}→{right['radius_pass']}; IoU prefilter {left['iou_prefilter_pass']}→{right['iou_prefilter_pass']} ({left['aabb_iou']}→{right['aabb_iou']}); accepted-IoU {left['iou_acceptance_pass']}→{right['iou_acceptance_pass']}; final candidate eligibility {left['eligible']}→{right['eligible']}. No assignment relation changed.")
    report += ["", "## Case A/B and known failures", ""]
    if case_results:
        for case, results in case_results.items():
            report.append(f"- Case {case}: " + "; ".join(f"{mode} TP/FP/FN={m['TP']}/{m['FP']}/{m['FN']}" for mode, m in results.items()))
    else:
        report.append("No exact-cache Case A/B human annotation was matched.")
    report += ["- R23 same-frame mixed-mask dedup case is outside this 29-frame main cache; the Q2 change does not alter dedup config or aliases. It is not counted as a Q2 pass.",
               "- K4 still exhibits the known touching-distinct-surfaces false match in its regression fixture. With Q2 prior-surface bounds, that exact synthetic fixture created a separate track because the surface-derived AABB had zero z extent, `extent_consistency` was unavailable, and the original geometry candidate failed. This is a possible fragmentation trade-off in one diagnostic fixture, not a safety-gate pass or a general false-merge guarantee.",
               "", "## Decision and unresolved relations", "",
               "No tuning or promotion was performed. Candidate outcome must be interpreted with all changed relations and evaluator/human coverage; unavailable identity is not treated as correct.",
               f"Unscored changed relations are in `metrics.json` under `changed_relations_k4_to_q2`; there are {len(k4_to_q2)} total changes.", ""]
    (output / "report.vi.md").write_text("\n".join(report), encoding="utf-8")
    print(f"Q2 paired ablation complete: {output}")
    print(f"Report: {output / 'report.vi.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
