#!/usr/bin/env python3
"""Paired offline v1/v2 duplicate-candidate evaluation on existing caches."""
from __future__ import annotations

import hashlib
import json
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from run_u3_same_frame_duplicate_experiment import (  # noqa: E402
    assignment_rows,
    build_cache,
    compare_decisions,
    geometry_replay,
)
from standalone_sg.objects.duplicate_partition_review import compare_duplicate_partitions  # noqa: E402
from standalone_sg.objects.same_frame_duplicates_v2 import canonicalize_same_frame_duplicates_v2  # noqa: E402


V1_RUN = ROOT / "artifacts/runs/u3_same_frame_dup_candidate_20261010_v6"
V1_ANNOTATIONS = ROOT / "artifacts/reviews/u3_duplicate_partition_review_v3/annotations/annotation_v1/annotations.json"
OUT = ROOT / "artifacts/runs/u3_same_frame_dup_candidate_v2_20261011_v2"
CACHES = {
    "00800": ROOT / "artifacts/runs/hm3d_00800_u4ov_multiview_pilot_v2_640",
    "00802": Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058"),
    "00808": ROOT / "artifacts/runs/u6_hm3d_00808_quality_v1_20261010",
}
CONFIG_PATH = ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json"
CONFIG_SHA256_LOCKED = "257dd354c7b562fed4b8d2b6548fcf38fe67946aef2e95ae1c084d79044d3ec3"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for r in rows), encoding="utf-8")


def pair_is_alias(assignments: dict[str, dict], left: str, right: str) -> bool:
    return (assignments[left].get("duplicate_of") == right or assignments[right].get("duplicate_of") == left)


def run_scene(scene: str, run: Path, config: dict, out: Path, annotations: dict) -> dict:
    observations, masks, support, p3_by_id, inputs = build_cache(run)
    frame_index_by_id: dict[str, int] = {}
    frames: dict[int, list] = defaultdict(list)
    for obs in observations:
        frame_index = int(obs.frame_id.rsplit("-", 1)[-1])
        frame_index_by_id[obs.detection_id] = frame_index
        frames[frame_index].append(obs)

    v1_dir = V1_RUN / scene
    v1_rows = read_jsonl(v1_dir / "assignments_geometry_only_candidate.jsonl")
    old_geometry_rows = read_jsonl(v1_dir / "assignments_geometry_only.jsonl")
    if len(v1_rows) != len(observations) or len(old_geometry_rows) != len(observations):
        raise ValueError(f"v1 paired assignments do not match current observation count for {scene}")
    v1_map = {r["detection_id"]: r for r in v1_rows}
    v1_geometry_map = {r["detection_id"]: r for r in old_geometry_rows}

    canonical, alias_rows, ledger = canonicalize_same_frame_duplicates_v2(
        observations, masks, support, config["candidate"])
    aliases = {r["detection_id"]: r["duplicate_of"] for r in alias_rows}
    canonical_frames: dict[int, list] = defaultdict(list)
    for obs in canonical:
        canonical_frames[frame_index_by_id[obs.detection_id]].append(obs)
    v2_assignments, v2_backend = geometry_replay(canonical_frames, {}, config["baseline_config"])
    for alias, canonical_id in aliases.items():
        v2_assignments[alias] = v2_assignments.get(canonical_id)
    for alias_row in alias_rows:
        v2_backend["decisions"].append({
            "detection_id": alias_row["detection_id"],
            "track_id": v2_assignments.get(alias_row["detection_id"]),
            "decision": "duplicate_alias",
            "reason": alias_row["decision"],
            "duplicate_of": alias_row["duplicate_of"],
        })

    v2_rows = assignment_rows(observations, v2_assignments, v2_backend, aliases,
                              "geometry_only_candidate_v2", frame_index_by_id)
    decision_by_id = {r["detection_id"]: r for r in ledger["observation_decisions"]}
    pair_evidence_by_pair = {frozenset((r["left_detection_id"], r["right_detection_id"])): r
                             for r in ledger["pair_evidence"]}
    for row in v2_rows:
        d = decision_by_id[row["detection_id"]]
        row["duplicate_decision_status"] = d["decision"]
        row["duplicate_decision_reason"] = d.get("reason")
        row["duplicate_candidates"] = d.get("candidate_canonicals", [])
        if d["decision"] == "kept_ambiguous":
            row["counts_as_independent_view"] = False

    v2_map = {r["detection_id"]: r for r in v2_rows}
    v1_duplicate_status = {
        row["detection_id"]: ("alias_suppressed" if row.get("suppressed_from_association") else "kept_canonical")
        for row in v1_rows
    }
    policy_changes = []
    for detection_id, v2_decision in sorted(decision_by_id.items()):
        old_status = v1_duplicate_status[detection_id]
        if old_status != v2_decision["decision"]:
            policy_changes.append({
                "detection_id": detection_id,
                "v1_duplicate_status": old_status,
                "v1_duplicate_of": v1_map[detection_id].get("duplicate_of"),
                "v2_duplicate_status": v2_decision["decision"],
                "v2_duplicate_of": v2_decision.get("duplicate_of"),
                "v2_reason": v2_decision.get("reason"),
                "association_track_v1": v1_map[detection_id].get("track_id"),
                "association_track_v2": v2_map[detection_id].get("track_id"),
            })
    v1_changes, v1_pair_changes, v1_delta = compare_decisions(v1_rows, v2_rows)
    geometry_changes, geometry_pair_changes, geometry_delta = compare_decisions(old_geometry_rows, v2_rows)
    scene_out = out / scene
    scene_out.mkdir(parents=True, exist_ok=False)
    write_jsonl(scene_out / "assignments_candidate_v2.jsonl", v2_rows)
    write_jsonl(scene_out / "candidate_v2_pair_evidence.jsonl", ledger["pair_evidence"])
    write_jsonl(scene_out / "candidate_v2_observation_decisions.jsonl", ledger["observation_decisions"])
    write_jsonl(scene_out / "v1_vs_v2_changed_observation_decisions.jsonl", v1_changes)
    write_jsonl(scene_out / "v1_vs_v2_changed_pairwise_relations.jsonl", v1_pair_changes)
    write_jsonl(scene_out / "v1_vs_v2_duplicate_policy_changes.jsonl", policy_changes)
    write_jsonl(scene_out / "geometry_only_vs_v2_changed_observation_decisions.jsonl", geometry_changes)
    write_jsonl(scene_out / "geometry_only_vs_v2_changed_pairwise_relations.jsonl", geometry_pair_changes)

    v1_groups = compare_duplicate_partitions(old_geometry_rows, v1_rows)
    v2_groups = compare_duplicate_partitions(old_geometry_rows, v2_rows)
    v1_v2_groups = compare_duplicate_partitions(v1_rows, v2_rows)
    partition_counts = {
        "geometry_to_v1": {kind: sum(x.kind == kind for x in v1_groups)
                            for kind in ("canonical_only", "membership_expansion_or_merge", "split_or_released", "membership_repartition")},
        "geometry_to_v2": {kind: sum(x.kind == kind for x in v2_groups)
                            for kind in ("canonical_only", "membership_expansion_or_merge", "split_or_released", "membership_repartition")},
        "v1_to_v2": {kind: sum(x.kind == kind for x in v1_v2_groups)
                      for kind in ("canonical_only", "membership_expansion_or_merge", "split_or_released", "membership_repartition")},
    }

    scene_review = []
    for item in annotations["annotations"]:
        if item["scene_id"] != scene:
            continue
        left, right = item["observation_ids"]
        if left not in v1_map or right not in v1_map:
            raise ValueError(f"review ID absent from predictor rows: {left}, {right}")
        scene_review.append({
            "review_id": item["review_id"],
            "observation_ids": [left, right],
            "human_relation": item["relation_status"],
            "v1_canonicalized": pair_is_alias(v1_map, left, right),
            "v2_canonicalized": pair_is_alias(v2_map, left, right),
            "v2_ambiguous": pair_evidence_by_pair.get(frozenset((left, right)), {}).get("ambiguous_duplicate_decision", False),
            "v1_track_ids": [v1_map[left].get("track_id"), v1_map[right].get("track_id")],
            "v2_track_ids": [v2_map[left].get("track_id"), v2_map[right].get("track_id")],
            "cross_frame_identity_inferred": False,
        })

    p2_obs = {o.detection_id for o in observations}
    if p2_obs != set(v2_map):
        raise ValueError("v2 assignments do not cover every accepted cached P3 observation")
    report = {
        "scene": scene,
        "input_manifest": inputs,
        "v1_assignment_sha256": sha(v1_dir / "assignments_geometry_only_candidate.jsonl"),
        "v1_geometry_assignment_sha256": sha(v1_dir / "assignments_geometry_only.jsonl"),
        "raw_p3_observations": len(observations),
        "candidate_v1_suppressed": sum(r.get("suppressed_from_association", False) for r in v1_rows),
        "candidate_v2_suppressed": len(alias_rows),
        "candidate_v2_ambiguous_observations": len(ledger["ambiguous_observation_ids"]),
        "candidate_v2_ambiguous_pair_rows": sum(r["ambiguous_duplicate_decision"] for r in ledger["pair_evidence"]),
        "v1_vs_v2_duplicate_policy_changes": len(policy_changes),
        "v1_vs_v2_duplicate_policy_changes_path": "v1_vs_v2_duplicate_policy_changes.jsonl",
        "candidate_v2_track_count": v2_backend["track_count"],
        "candidate_v2_defer_count": v2_backend["defer_count"],
        "v1_vs_v2_changed_observation_decisions": len(v1_changes),
        "v1_vs_v2_changed_pairwise_relations": v1_delta,
        "geometry_only_vs_v2_changed_observation_decisions": len(geometry_changes),
        "geometry_only_vs_v2_changed_pairwise_relations": geometry_delta,
        "partition_deltas": partition_counts,
        "reviewed_pairs": scene_review,
        "ambiguous_pairs": [
            {
                "frame_id": row["frame_id"],
                "observation_ids": [row["left_detection_id"], row["right_detection_id"]],
                "reason": row["v2_guard_reason"],
                "mask_area_ratio": row["larger_to_smaller_mask_area_ratio"],
                "gate_failures": row["gate_failures"],
            }
            for row in ledger["pair_evidence"] if row["ambiguous_duplicate_decision"]
        ],
        "assignments_v2_path": "assignments_candidate_v2.jsonl",
        "pair_evidence_path": "candidate_v2_pair_evidence.jsonl",
        "changed_observation_decisions_path": "v1_vs_v2_changed_observation_decisions.jsonl",
        "changed_pairwise_relations_path": "v1_vs_v2_changed_pairwise_relations.jsonl",
        "predictor_used_annotations_gt_or_room_labels": False,
        "perception_or_native_mapping_rerun": False,
    }
    (scene_out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    if sha(CONFIG_PATH) != CONFIG_SHA256_LOCKED:
        raise ValueError("locked v2 config hash mismatch; refusing paired evaluation")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    annotations = json.loads(V1_ANNOTATIONS.read_text(encoding="utf-8"))
    if OUT.exists() or ROOT / "artifacts/runs" not in OUT.parents:
        raise FileExistsError(f"output must be a new workspace run: {OUT}")
    started = time.perf_counter()
    OUT.mkdir(parents=True)
    reports = [run_scene(scene, run, config, OUT, annotations) for scene, run in CACHES.items()]

    scene08 = next(r for r in reports if r["scene"] == "00808")
    # D1/D2 metrics were scored against the already versioned evaluator-only
    # annotation in v1. Reuse only when every v1/v2 assignment is identical.
    d1_d2 = None
    if scene08["v1_vs_v2_changed_observation_decisions"] == 0:
        v1_report = json.loads((V1_RUN / "00808/report.json").read_text(encoding="utf-8"))
        d1_d2 = v1_report["human_reviewed_metrics"]["modes"]["geometry_only_candidate"]
    elapsed = time.perf_counter() - started
    report = {
        "schema_version": 1,
        "status": "completed_offline_development_corrected_diagnostic_semantics",
        "supersedes_run": "artifacts/runs/u3_same_frame_dup_candidate_v2_20261011_v1",
        "supersession_reason": "Initial run marked two frame-246 pairs ambiguous despite complete, clearly disconfirming 3D geometry; corrected decision-reason precedence without changing locked v2 config.",
        "candidate_v1_preserved": True,
        "candidate_v2_config": str(CONFIG_PATH),
        "candidate_v2_config_sha256": sha(CONFIG_PATH),
        "candidate_v1_reference_run": str(V1_RUN),
        "annotations_evaluator_only": True,
        "perception_or_native_mapping_rerun": False,
        "M9_or_graph_integration": False,
        "runtime_seconds": elapsed,
        "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        "D1_D2_reused_metrics_if_assignments_unchanged": d1_d2,
        "scenes": reports,
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"], "output": str(OUT), "runtime_seconds": elapsed,
        "peak_process_rss_mib": report["peak_process_rss_mib"],
        "scenes": [{"scene": r["scene"], "observations": r["raw_p3_observations"],
                    "v1_suppressed": r["candidate_v1_suppressed"], "v2_suppressed": r["candidate_v2_suppressed"],
                    "v2_ambiguous": r["candidate_v2_ambiguous_observations"],
                    "v1_v2_duplicate_policy_changes": r["v1_vs_v2_duplicate_policy_changes"],
                    "v1_v2_changed_decisions": r["v1_vs_v2_changed_observation_decisions"],
                    "v2_deferred": r["candidate_v2_defer_count"]} for r in reports],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
