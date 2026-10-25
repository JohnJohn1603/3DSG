#!/usr/bin/env python3
"""Bounded Q3 development ablation: causal raw-class soft reranking over K4."""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
from scripts.run_u3_surface_support_candidate import (  # noqa: E402
    load_locked_inputs, read_json, read_jsonl, run_predictor, sha,
)
from scripts.run_q2_representation_ablation import (  # noqa: E402
    _assignments, _evaluator_ids, _human_annotations,
)
from scripts.score_q1_00808_reviewed_subset import score as score_human_subset  # noqa: E402
from standalone_sg.objects.profile_comparison import pairwise_identity_metrics, relation_changes  # noqa: E402


def _mode_metrics(input_root: Path, population: set[str], modes: dict[str, dict],
                  human_labels: dict[str, str]) -> dict:
    evaluator = _evaluator_ids(input_root, population)
    evaluator_metrics = ({name: pairwise_identity_metrics(assign, evaluator, eligible_ids=population)
                          for name, assign in modes.items()}
                         if evaluator and set(evaluator) == population else
                         {name: {"status": "unavailable_no_complete_evaluator_identity_ledger",
                                 "pairwise_tp": None, "pairwise_fp": None, "pairwise_fn": None,
                                 "association_precision": None, "association_recall": None,
                                 "association_f1": None, "false_merge_tracks": None}
                          for name in modes})
    human_metrics = ({name: score_human_subset(assign, human_labels, sorted(human_labels))
                      for name, assign in modes.items()} if human_labels else
                     {name: {"status": "unavailable_no_confirmed_human_identity"} for name in modes})
    return {"evaluator_id": evaluator_metrics, "human_reviewed": human_metrics}


def _decision_counts(path: Path) -> dict:
    rows = read_jsonl(path)
    return {"observations": len(rows),
            "deferred": sum(str(row.get("decision", "")).startswith("defer") for row in rows),
            "unassigned": sum(row.get("track_id") is None for row in rows),
            "aliases": sum(row.get("decision") in {"duplicate_alias", "duplicate_suppressed"} for row in rows)}


def run_cache(*, input_run: Path, p3_outputs: list[Path], k4_reference: Path,
              human_annotations: Path | None, output: Path, lambdas: list[float],
              primary_selection: bool) -> dict:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    config_path = ROOT / "configs/association/geometry_only_accumulated_surface_v1.json"
    fusion_path = ROOT / "configs/fusion/object_geometry_v2.json"
    inputs = load_locked_inputs(input_run.resolve(), [p.resolve() for p in p3_outputs], None, None,
                               config_path, fusion_path, k4_reference.resolve())
    population = set(inputs["raw_ids"])
    labels, label_meta = _human_annotations(human_annotations, population)
    contract_path = ROOT / "configs/association/q3_raw_class_soft_rerank_v1.json"
    contract = read_json(contract_path)
    lock = {
        "schema_version": 1, "status": "locked_before_scoring",
        "experiment_contract_sha256": sha(contract_path),
        "input_run": str(input_run.resolve()), "input_hashes": inputs["input_hashes"],
        "P3_sidecar_manifests": inputs["p3_manifest_hashes"],
        "K4_reference_sha256": sha(k4_reference.resolve()),
        "raw_class_population_count": len(population),
        "human_annotation_sha256": label_meta.get("sha256"),
        "predictor_uses_evaluator_or_human_annotations": False,
        "model_or_vocabulary_changed": False,
        "candidate_lambdas": lambdas,
        "predictor_code_hashes": {
            "scripts/run_q3_raw_class_ablation.py": sha(ROOT / "scripts/run_q3_raw_class_ablation.py"),
            "scripts/run_u3_surface_support_candidate.py": sha(ROOT / "scripts/run_u3_surface_support_candidate.py"),
            "src/standalone_sg/objects/u3_experimental.py": sha(ROOT / "src/standalone_sg/objects/u3_experimental.py"),
        },
    }
    (output / "pre_score_lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")

    started = time.perf_counter()
    predictions = {}
    for lam in lambdas:
        name = f"lambda_{lam:.2f}"
        manifest = run_predictor(input_run.resolve(), dict(inputs), output / name,
                                 representation_mode="k4_current", class_soft_lambda=lam)
        if manifest.get("input_hashes") != inputs["input_hashes"]:
            raise RuntimeError(f"candidate input hashes changed for {name}")
        modes = {
            "source": _assignments(output / name / "source_assignments.jsonl"),
            "geometry-only-dedup-v2": _assignments(output / name / "geometry_only_dedup_v2_assignments.jsonl"),
            "K4-current": _assignments(output / name / "candidate_assignments.jsonl") if lam == 0.0 else None,
            "Q3-raw-class-soft": _assignments(output / name / "candidate_assignments.jsonl"),
        }
        # For candidate configs, reconstruct K4 as its locked reference so all
        # controls are compared on precisely the same observation population.
        k4_rows = {str(row["observation_id"]): row.get("track_id") for row in read_jsonl(k4_reference)}
        modes["K4-current"] = k4_rows
        decision_files = {
            "source": output / name / "source_assignments.jsonl",
            "geometry-only-dedup-v2": output / name / "geometry_only_dedup_v2_assignments.jsonl",
            "K4-current": k4_reference,
            "Q3-raw-class-soft": output / name / "candidate_assignments.jsonl",
        }
        predictions[name] = {"manifest": manifest, "modes": modes,
                             "metrics": _mode_metrics(input_run.resolve(), population, modes, labels),
                             "decision_counts": {mode: _decision_counts(path) for mode, path in decision_files.items()}}
        (output / name / "metrics.json").write_text(json.dumps({
            "lambda": lam, "metrics": predictions[name]["metrics"],
            "decision_counts": predictions[name]["decision_counts"],
            "candidate_vs_k4_relations": relation_changes(k4_rows, modes["Q3-raw-class-soft"], sorted(population)),
            "runtime_seconds": manifest.get("causal_replay_runtime_seconds"),
            "peak_process_rss_mib": manifest.get("peak_process_rss_mib"),
        }, indent=2, sort_keys=True) + "\n")

    selected = None
    if primary_selection:
        k4_eval = predictions[f"lambda_{lambdas[0]:.2f}"]["metrics"]["evaluator_id"]["K4-current"]
        k4_human = predictions[f"lambda_{lambdas[0]:.2f}"]["metrics"]["human_reviewed"]["K4-current"]
        admissible = []
        for lam in lambdas:
            row = predictions[f"lambda_{lam:.2f}"]["metrics"]
            ev, hu = row["evaluator_id"]["Q3-raw-class-soft"], row["human_reviewed"]["Q3-raw-class-soft"]
            if (ev.get("association_f1") is not None and hu.get("F1") is not None
                    and ev.get("false_merge_tracks", 0) <= k4_eval.get("false_merge_tracks", 0)
                    and hu["F1"] >= k4_human.get("F1", 0.0)):
                admissible.append((float(ev["association_f1"]), -lam, lam))
        selected = max(admissible)[2] if admissible else None
    report = {
        "schema_version": 1, "status": "completed", "development_not_holdout": True,
        "input_run": input_run.name, "input_hashes": inputs["input_hashes"],
        "population_observations": len(population), "human_annotation": label_meta,
        "experiment_contract_sha256": sha(contract_path),
        "pre_score_lock_sha256": sha(output / "pre_score_lock.json"),
        "primary_selection": primary_selection, "selected_lambda": selected,
        "applied_lambda": selected if primary_selection else lambdas[0],
        "selection_status": ("selected_by_primary_development_rule" if primary_selection else
                             "fixed_lambda_from_primary; not reselected on this cache"),
        "tested_lambdas": lambdas,
        "selection_rule_applied": contract["parameter_search"]["selection_rule"],
        "candidate_results": {name: {
            "evaluator_id": data["metrics"]["evaluator_id"]["Q3-raw-class-soft"],
            "human_reviewed": data["metrics"]["human_reviewed"]["Q3-raw-class-soft"],
            "K4_evaluator_id": data["metrics"]["evaluator_id"]["K4-current"],
            "K4_human_reviewed": data["metrics"]["human_reviewed"]["K4-current"],
            "candidate_vs_k4_relations": relation_changes(data["modes"]["K4-current"],
                                                           data["modes"]["Q3-raw-class-soft"], sorted(population)),
            "decision_counts": data["decision_counts"],
            "runtime_seconds": data["manifest"].get("causal_replay_runtime_seconds"),
            "peak_process_rss_mib": data["manifest"].get("peak_process_rss_mib"),
        } for name, data in predictions.items()},
        "runtime_scope": "offline K4 association with cached RGB-D/P2/P3/selected-point sidecars; no perception or native replay",
        "paired_wall_seconds": time.perf_counter() - started,
        "process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "known_constraints": ["reranking cannot admit K4-ineligible/deferred pairs", "all caches are development", "no semantic label or room output was changed"],
    }
    (output / "metrics.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (output / "report.vi.md").write_text(render_report(report) + "\n", encoding="utf-8")
    return report


def render_report(data: dict) -> str:
    lines = [f"# Q3 raw-class soft rerank — {data['input_run']}", "",
             "Development only; no capture, detector, P3, native replay, appearance, or semantic-label update.",
             f"Population: {data['population_observations']} observations. Contract SHA-256: `{data['experiment_contract_sha256']}`.",
             f"Runtime total {data['paired_wall_seconds']:.2f}s; peak RSS {data['process_peak_rss_mib']:.1f} MiB.", "",
             "| λ | Evaluator TP/FP/FN | P/R/F1 | False merge | Human TP/FP/FN, coverage | Human F1 | Defer/unassigned | Changed relations | Runtime s |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, row in data["candidate_results"].items():
        ev, hu = row["evaluator_id"], row["human_reviewed"]
        dc = row["decision_counts"]["Q3-raw-class-soft"]
        lines.append(f"| {name.removeprefix('lambda_')} | {ev.get('pairwise_tp')}/{ev.get('pairwise_fp')}/{ev.get('pairwise_fn')} | {ev.get('association_precision')}/{ev.get('association_recall')}/{ev.get('association_f1')} | {ev.get('false_merge_tracks')} | {hu.get('TP')}/{hu.get('FP')}/{hu.get('FN')}, {hu.get('assigned_coverage')} | {hu.get('F1')} | {dc['deferred']}/{dc['unassigned']} | {len(row['candidate_vs_k4_relations'])} | {row['runtime_seconds']} |")
    selection = (str(data["selected_lambda"]) if data["primary_selection"] else
                 f"{data['applied_lambda']} fixed from primary development; not reselected here")
    lines += ["", f"Selection: {selection}.",
              "Raw class is a soft reranking term only; K4 eligibility and all gates remain unchanged.",
              "Unreviewed changed relations are listed in metrics.json and are not assumed correct."]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-run", required=True, type=Path)
    parser.add_argument("--p3-output", required=True, action="append", type=Path)
    parser.add_argument("--k4-reference", required=True, type=Path)
    parser.add_argument("--human-annotations", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lambdas", type=float, nargs="+", default=[0.05, 0.15, 0.30])
    parser.add_argument("--primary-selection", action="store_true")
    args = parser.parse_args()
    contract = read_json(ROOT / "configs/association/q3_raw_class_soft_rerank_v1.json")
    locked_values = contract["parameter_search"]["values"]
    if args.primary_selection and args.lambdas != locked_values:
        raise ValueError("primary development search must use the locked lambda list exactly")
    if not args.primary_selection and (len(args.lambdas) != 1 or args.lambdas[0] not in locked_values):
        raise ValueError("secondary cache replay must use exactly one lambda selected on primary development")
    if len(args.p3_output) > 2:
        raise ValueError("Q3 runner accepts at most two cached P3 sidecar roots")
    result = run_cache(input_run=args.input_run, p3_outputs=args.p3_output,
                       k4_reference=args.k4_reference, human_annotations=args.human_annotations,
                       output=args.output, lambdas=args.lambdas, primary_selection=args.primary_selection)
    print(f"Q3 complete: {args.output} selected_lambda={result['selected_lambda']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
