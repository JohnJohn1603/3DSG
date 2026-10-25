#!/usr/bin/env python3
"""Offline paired replay for the same-frame duplicate suppression candidate.

Consumes only saved P2/P3 caches and masks. It does not run perception or M9.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import resource
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.config.schema import ObjectAssociationConfig  # noqa: E402
from standalone_sg.contracts.objects import Detection3D, DetectionBatch  # noqa: E402
from standalone_sg.objects.association import ObjectAssociationManager  # noqa: E402
from standalone_sg.objects.same_frame_duplicates import canonicalize_same_frame_duplicates  # noqa: E402
from standalone_sg.objects.u3_experimental import ExperimentalAssociation, U3Observation, deduplicate_frame_masks  # noqa: E402
from u6_scoring import pairwise_association  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_cache(run: Path) -> tuple[list[U3Observation], dict[str, np.ndarray], dict[str, dict], dict[str, dict], dict]:
    p2_path, p3_path = run / "detection_2d/detection_2d_index.json", run / "detection_3d/detection_3d.jsonl"
    capture_path = run / "capture/capture_manifest.json"
    if not all(p.is_file() for p in (p2_path, p3_path, capture_path)):
        raise FileNotFoundError(f"cache needs capture manifest, P2 index, and P3 jsonl: {run}")
    p2 = json.loads(p2_path.read_text(encoding="utf-8"))
    if p2.get("status") != "pass":
        raise ValueError(f"P2 status is not pass: {run}")
    if p2.get("capture_manifest_sha256") != sha(capture_path):
        raise ValueError(f"P2 cache/capture manifest hash mismatch: {run}")
    p2_by_id = {d["detection_id"]: d for f in p2["frames"] for d in f["detections"]}
    p3_batches = read_jsonl(p3_path)
    p3_by_id = {d["detection_id"]: d for batch in p3_batches for d in batch.get("detections", [])}
    if set(p3_by_id) - set(p2_by_id):
        raise ValueError("P3 cache references a detection absent from P2")
    observations, masks, support = [], {}, {}
    frame_map: dict[int, list[U3Observation]] = defaultdict(list)
    d2_root = run / "detection_2d"
    for batch in p3_batches:
        for det in batch.get("detections", []):
            did = det["detection_id"]
            p2det = p2_by_id[did]
            mask_path = d2_root / p2det["mask_file"]
            if sha(mask_path) != p2det["mask_sha256"]:
                raise ValueError(f"P2 mask hash mismatch: {did}")
            mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
            masks[did] = mask
            metadata = det.get("provider_metadata", {})
            frame_index = int(metadata.get("frame_index", str(batch["frame_id"]).rsplit("-", 1)[-1]))
            obs = U3Observation(
                detection_id=did, frame_id=str(batch["frame_id"]), timestamp_s=float(det["timestamp_s"]),
                center=tuple(float(x) for x in det["center_W"]),
                extents=tuple(float(x) for x in det["extents_m"]) if det.get("extents_m") else None,
                layer_id=str(det.get("layer_id", "floor_0")), class_name=str(det.get("class_name", "")),
                feature=None, mask_hash=str(p2det["mask_sha256"]),
            )
            frame_map[frame_index].append(obs)
            support[did] = {"valid_depth_pixels": int(metadata.get("valid_depth_pixels", 0)),
                            "cluster_points": int(metadata.get("cluster_points", 0)),
                            "detector_score": float(p2det.get("score_raw", 0.0))}
    for frame_index in sorted(frame_map):
        observations.extend(sorted(frame_map[frame_index], key=lambda x: x.detection_id))
    return observations, masks, support, p3_by_id, {"p2": str(p2_path), "p3": str(p3_path), "capture": str(capture_path),
        "p2_sha256": sha(p2_path), "p3_sha256": sha(p3_path), "capture_sha256": sha(capture_path),
        "p2_detections": len(p2_by_id), "p3_accepted": len(observations)}


def source_replay(frames: dict[int, list[U3Observation]], p3_by_id: dict[str, dict], source_cfg: dict) -> tuple[dict, dict]:
    manager = ObjectAssociationManager(ObjectAssociationConfig(
        float(source_cfg["same_class_radius_m"]), float(source_cfg["same_pose_suppression_cap_m"])))
    assignments, decisions = {}, []
    for frame_index in sorted(frames):
        for obs in sorted(frames[frame_index], key=lambda x: x.detection_id):
            src = p3_by_id[obs.detection_id]
            detection = Detection3D(
                detection_id=obs.detection_id, timestamp_s=obs.timestamp_s, center_W=obs.center,
                center_method=str(src.get("center_method", "saved_p3")), class_name=obs.class_name,
                class_id=str(src.get("class_id", "")), confidence=src.get("confidence"),
                frame_id="world", layer_id=obs.layer_id, extents_m=obs.extents)
            result = manager.update(DetectionBatch("saved_p3", "world", obs.timestamp_s, (detection,))).associations[0]
            assignments[obs.detection_id] = result.object_id
            decisions.append({"detection_id": obs.detection_id, "frame_index": frame_index,
                              "decision": result.decision, "track_id": result.object_id,
                              "distance_m": result.distance_m})
    return assignments, {"decisions": decisions, "track_count": len(manager.snapshot())}


def geometry_replay(frames: dict[int, list[U3Observation]], dedup_aliases: dict[str, str], cfg: dict) -> tuple[dict[str, str | None], dict]:
    exp = cfg["geometry_only"]
    engine = ExperimentalAssociation("geometry", max_center_m=float(exp["candidate_center_radius_m"]),
        geom_min_iou=float(exp["appearance_iou_min"]), geom_only_min_iou=float(exp["geometry_only_iou_min"]),
        ambiguity_margin=float(exp["ambiguity_margin"]))
    assignments, decisions, candidates = {}, [], []
    for frame_index in sorted(frames):
        current = [obs for obs in sorted(frames[frame_index], key=lambda x: x.detection_id) if obs.detection_id not in dedup_aliases]
        start = time.perf_counter()
        rows = engine.update_frame(current)
        _ = time.perf_counter() - start
        decisions.extend({"frame_index": frame_index, **d.__dict__} for d in rows)
        candidates.extend({"frame_index": frame_index, **r} for r in engine.ledger)
        engine.ledger.clear()
        for decision in rows:
            if decision.track_id:
                assignments[decision.detection_id] = decision.track_id
    for alias, canonical in dedup_aliases.items():
        # A canonical observation can itself defer/unassign in the cross-frame
        # backend. Its alias inherits that status, never an invented track.
        assignments[alias] = assignments.get(canonical)
        decisions.append({"detection_id": alias, "track_id": assignments[alias], "decision": "duplicate_alias",
                          "reason": "same_frame_canonicalization" if assignments[alias] else "canonical_unassigned_or_deferred",
                          "duplicate_of": canonical})
    return assignments, {"decisions": decisions, "candidate_rows": candidates,
                         "track_count": len(engine.tracks), "defer_count": sum(d.decision == "defer" for d in engine.decisions)}


def assignment_rows(observations: list[U3Observation], assignments: dict[str, str], decisions: dict,
                    aliases: dict[str, str], mode: str, frame_index_by_id: dict[str, int]) -> list[dict]:
    decision_by_id = {r["detection_id"]: r for r in decisions["decisions"]}
    return [{"mode": mode, "detection_id": o.detection_id, "frame_id": o.frame_id,
             "frame_index": frame_index_by_id[o.detection_id], "track_id": assignments.get(o.detection_id),
             "decision": decision_by_id.get(o.detection_id, {}).get("decision", "unassigned"),
             "duplicate_of": aliases.get(o.detection_id), "suppressed_from_association": o.detection_id in aliases,
             "counts_as_independent_view": o.detection_id not in aliases}
            for o in observations]


def compare_decisions(reference_rows: list[dict], candidate_rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    ref = {r["detection_id"]: r for r in reference_rows}
    cand = {r["detection_id"]: r for r in candidate_rows}
    observation_changes = []
    for did in sorted(set(ref) | set(cand)):
        a, b = ref.get(did, {}), cand.get(did, {})
        if a.get("decision") != b.get("decision") or a.get("suppressed_from_association") != b.get("suppressed_from_association") or bool(a.get("track_id")) != bool(b.get("track_id")):
            observation_changes.append({"detection_id": did, "frame_index": b.get("frame_index", a.get("frame_index")),
                        "current_geometry": {k: a.get(k) for k in ("track_id", "decision", "duplicate_of", "suppressed_from_association")},
                        "candidate": {k: b.get(k) for k in ("track_id", "decision", "duplicate_of", "suppressed_from_association")}})
    ids = sorted(set(ref) & set(cand))
    pair_changes = []
    transitions: Counter[str] = Counter()
    def relation(track_a: str | None, track_b: str | None) -> str:
        if track_a is None or track_b is None:
            return "deferred_or_unassigned"
        return "same_track" if track_a == track_b else "different_tracks"
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            old_left, old_right = ref[left].get("track_id"), ref[right].get("track_id")
            new_left, new_right = cand[left].get("track_id"), cand[right].get("track_id")
            old_relation = relation(old_left, old_right)
            new_relation = relation(new_left, new_right)
            if old_relation != new_relation:
                change = f"{old_relation}_to_{new_relation}"
                transitions[change] += 1
                pair_changes.append({"left_observation_id": left, "right_observation_id": right,
                    "current_geometry_relation": old_relation, "candidate_relation": new_relation,
                    "current_geometry_track_ids": [old_left, old_right], "candidate_track_ids": [new_left, new_right],
                    "change": change})
    return observation_changes, pair_changes, {"relation_transitions": dict(sorted(transitions.items())),
        "changed_pairwise_relations": len(pair_changes)}


def metrics_for_review(annotations_csv: Path, assignments_by_mode: dict[str, dict], observations: list[U3Observation]) -> dict:
    annotations = list(csv.DictReader(annotations_csv.open(encoding="utf-8", newline="")))
    frame_by_id = {o.detection_id: int(o.frame_id.rsplit("-", 1)[-1]) for o in observations}
    # The fixed review file is the only annotation source; no GT or room labels
    # enter replay, candidate suppression, or the candidate decision ledger.
    grouped = defaultdict(list)
    for row in annotations:
        if row.get("identity_status") == "confirmed":
            grouped[row["identity_token"]].append(row)
    result = {"annotation_scope": "confirmed user-reviewed observations only", "modes": {}}
    for mode, assignments in assignments_by_mode.items():
        per = {}
        tracks_by_identity: dict[str, set[str]] = defaultdict(set)
        identities_by_track: dict[str, set[str]] = defaultdict(set)
        for row in annotations:
            if row.get("identity_status") != "confirmed":
                continue
            track = assignments.get(row["observation_id"])
            if track is None:
                continue
            token = row["identity_token"]
            tracks_by_identity[token].add(track)
            identities_by_track[track].add(token)
        track_ids_by_identity = {k: sorted(v) for k, v in sorted(tracks_by_identity.items())}
        fragmented = {k: v for k, v in track_ids_by_identity.items() if len(v) > 1}
        false_merge = {k: sorted(v) for k, v in identities_by_track.items() if len(v) > 1}
        per["confirmed_identity_track_structure"] = {
            "unique_confirmed_physical_objects": len(tracks_by_identity),
            "track_ids_by_identity": track_ids_by_identity,
            "fragmented_identities": fragmented,
            "fragmented_identity_count": len(fragmented),
            "excess_tracks_across_identities": sum(max(0, len(v) - 1) for v in track_ids_by_identity.values()),
            "false_merge_tracks": false_merge,
            "false_merge_track_count": len(false_merge),
            "confirmed_review_observations": sum(row.get("identity_status") == "confirmed" for row in annotations),
            "assigned_review_observations": sum(assignments.get(row["observation_id"]) is not None for row in annotations if row.get("identity_status") == "confirmed"),
            "deferred_or_unassigned_review_observations": sum(assignments.get(row["observation_id"]) is None for row in annotations if row.get("identity_status") == "confirmed"),
        }
        for identity in ("table_1", "table_2"):
            obs_rows = grouped.get(identity, [])
            rows = [{"observation_id": a["observation_id"], "frame_id": a["frame_id"],
                     "frame_index": int(a["frame_index"]), "gt_identity": identity,
                     "track_id": assignments.get(a["observation_id"]),
                     "association_status": "duplicate_alias" if a["observation_id"] not in assignments else "assigned"}
                    for a in obs_rows]
            if identity == "table_1":
                per["D1_cross_frame"] = pairwise_association(rows)
                # Frame 4 is only a scoring sensitivity; predictor history stays intact.
                per["D1_without_frame4_revisit"] = pairwise_association(
                    [r for r in rows if r["frame_index"] != 4], excluded_frame_indices={4})
            else:
                tracks = [assignments.get(r["observation_id"]) for r in rows]
                per["D2_same_frame"] = {"pair_count": 1 if len(rows) == 2 else max(0, len(rows)*(len(rows)-1)//2),
                    "same_track_consistent_pair_count": 1 if len(tracks) == 2 and tracks[0] is not None and tracks[0] == tracks[1] else 0,
                    "split_pair_count": 1 if len(tracks) == 2 and tracks[0] is not None and tracks[1] is not None and tracks[0] != tracks[1] else 0,
                    "deferred_or_unassigned_pair_count": 1 if len(tracks) == 2 and (tracks[0] is None or tracks[1] is None) else 0,
                    "observation_ids": [r["observation_id"] for r in rows], "track_ids": tracks,
                    "cross_frame_denominator_includes_D2": False}
        result["modes"][mode] = per
    return result


def run_one(run: Path, scene: str, config: dict, out: Path, annotations: Path | None) -> dict:
    observations, masks, support, p3_by_id, input_manifest = build_cache(run)
    frame_map: dict[int, list[U3Observation]] = defaultdict(list)
    frame_index_by_id = {}
    for obs in observations:
        frame_index = int(obs.frame_id.rsplit("-", 1)[-1])
        frame_index_by_id[obs.detection_id] = frame_index
        frame_map[frame_index].append(obs)
    raw_assignments, raw_diag = source_replay(frame_map, p3_by_id, config["baseline_config"]["source_manager"])
    old_alias_obs, old_dup_rows = [], []
    for frame_index in sorted(frame_map):
        kept, duplicates = deduplicate_frame_masks(frame_map[frame_index], masks,
            iou_threshold=float(config["baseline_config"]["existing_mask_dedup_iou_min"]))
        old_alias_obs.extend(duplicates)
        old_dup_rows.extend(duplicates)
        frame_map[frame_index] = kept
    old_alias = {r["detection_id"]: r["duplicate_of"] for r in old_dup_rows}
    current_assignments, current_diag = geometry_replay(frame_map, old_alias, config["baseline_config"])

    # Candidate suppression operates over the immutable raw P3 observation list.
    candidate_canon, candidate_alias_rows, diagnostic = canonicalize_same_frame_duplicates(
        observations, masks, support, config["candidate"])
    candidate_alias = {r["detection_id"]: r["duplicate_of"] for r in candidate_alias_rows}
    candidate_frames: dict[int, list[U3Observation]] = defaultdict(list)
    for obs in candidate_canon:
        candidate_frames[frame_index_by_id[obs.detection_id]].append(obs)
    candidate_assignments, candidate_diag = geometry_replay(candidate_frames, {}, config["baseline_config"])
    for alias, canonical in candidate_alias.items():
        candidate_assignments[alias] = candidate_assignments.get(canonical)
    for row in candidate_alias_rows:
        candidate_diag["decisions"].append({"detection_id": row["detection_id"], "track_id": candidate_assignments[row["detection_id"]],
            "decision": "duplicate_alias", "reason": row["decision"], "duplicate_of": row["duplicate_of"]})

    raw_rows = assignment_rows(observations, raw_assignments, raw_diag, {}, "source", frame_index_by_id)
    # Reconstruct current geometry decision rows against every raw observation.
    current_rows = assignment_rows(observations, current_assignments, current_diag, old_alias, "geometry_only", frame_index_by_id)
    candidate_rows = assignment_rows(observations, candidate_assignments, candidate_diag, candidate_alias, "geometry_only_candidate", frame_index_by_id)
    old_alias_map = {r["detection_id"]: r["duplicate_of"] for r in current_rows if r["suppressed_from_association"]}
    alias_delta = []
    for did in sorted(set(old_alias_map) | set(candidate_alias)):
        old_target, new_target = old_alias_map.get(did), candidate_alias.get(did)
        if old_target == new_target:
            continue
        if old_target is None:
            transition = "newly_suppressed"
        elif new_target is None:
            transition = "released_to_canonical"
        else:
            transition = "canonical_target_changed"
        alias_delta.append({"detection_id": did, "transition": transition,
                            "current_geometry_duplicate_of": old_target,
                            "candidate_duplicate_of": new_target,
                            "identity_status": "uncertain_without_confirmed_review"})
    scene_out = out / scene
    scene_out.mkdir(parents=True, exist_ok=False)
    for name, rows in (("source", raw_rows), ("geometry_only", current_rows), ("geometry_only_candidate", candidate_rows)):
        (scene_out / f"assignments_{name}.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (scene_out / "candidate_pair_evidence.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in diagnostic["pair_evidence"]), encoding="utf-8")
    (scene_out / "candidate_observation_decisions.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in diagnostic["observation_decisions"]), encoding="utf-8")
    comparisons = {}
    for left_name, left_rows, right_name, right_rows in (
        ("source", raw_rows, "geometry_only", current_rows),
        ("source", raw_rows, "geometry_only_candidate", candidate_rows),
        ("geometry_only", current_rows, "geometry_only_candidate", candidate_rows),
    ):
        obs_delta, pair_delta, summary_delta = compare_decisions(left_rows, right_rows)
        key = f"{left_name}_vs_{right_name}"
        comparisons[key] = {"observation_decisions": obs_delta, "pairwise_relations": pair_delta,
                            "pairwise_summary": summary_delta}
        (scene_out / f"changed_observation_decisions_{key}.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in obs_delta), encoding="utf-8")
        (scene_out / f"changed_pairwise_relations_{key}.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in pair_delta), encoding="utf-8")
    observation_changes = comparisons["geometry_only_vs_geometry_only_candidate"]["observation_decisions"]
    pair_changes = comparisons["geometry_only_vs_geometry_only_candidate"]["pairwise_relations"]
    pair_change_summary = comparisons["geometry_only_vs_geometry_only_candidate"]["pairwise_summary"]
    pair_evidence_index = {frozenset((r["left_detection_id"], r["right_detection_id"])): r for r in diagnostic["pair_evidence"]}
    new_suppressions = []
    for row in alias_delta:
        if row["transition"] != "newly_suppressed":
            continue
        evidence = pair_evidence_index.get(frozenset((row["detection_id"], row["candidate_duplicate_of"])), {})
        new_suppressions.append({**row, "pair_evidence": evidence})
    (scene_out / "candidate_new_suppressions_for_review.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in new_suppressions), encoding="utf-8")
    (scene_out / "alias_decision_delta.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in alias_delta), encoding="utf-8")
    report = {
        "scene": scene, "run_path": str(run), "input_manifest": input_manifest,
        "raw_p3_observations": len(observations), "candidate_suppressed_observations": len(candidate_alias),
        "existing_mask_iou_suppressed_observations": len(old_alias),
        "track_counts": {"source": raw_diag["track_count"], "geometry_only": current_diag["track_count"],
                         "geometry_only_candidate": candidate_diag["track_count"]},
        "defer_counts": {"source": sum(d["decision"] == "defer" for d in raw_diag["decisions"]),
                          "geometry_only": current_diag["defer_count"], "geometry_only_candidate": candidate_diag["defer_count"]},
        "candidate_aliases": candidate_alias_rows,
        "alias_decision_delta_counts": {kind: sum(r["transition"] == kind for r in alias_delta)
                                        for kind in ("newly_suppressed", "released_to_canonical", "canonical_target_changed")},
        "alias_decision_delta_path": "alias_decision_delta.jsonl",
        "candidate_new_suppressions_review_path": "candidate_new_suppressions_for_review.jsonl",
        "decision_comparisons": {k: {"changed_observation_decisions": len(v["observation_decisions"]),
                                      "changed_pairwise_relations": v["pairwise_summary"]}
                                 for k, v in comparisons.items()},
        "changed_observation_decision_count_vs_geometry_only": len(observation_changes),
        "changed_pairwise_relations_vs_geometry_only": pair_change_summary,
        "changed_observation_decisions_path": "changed_observation_decisions.jsonl",
        "changed_pairwise_relations_path": "changed_pairwise_relations.jsonl",
        "pair_evidence_rows": len(diagnostic["pair_evidence"]),
        "candidate_pair_accepts": sum(bool(r["eligible_duplicate"]) for r in diagnostic["pair_evidence"]),
        "official_identity_metrics": "unavailable_without a fixed confirmed identity annotation set",
        "predictor_used_gt_or_room_labels": False,
    }
    if annotations:
        report["human_reviewed_metrics"] = metrics_for_review(annotations,
            {"source": raw_assignments, "geometry_only": current_assignments,
             "geometry_only_candidate": candidate_assignments}, observations)
    (scene_out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", action="append", required=True, help="SCENE_ID=/path/to/cached/run (repeatable)")
    ap.add_argument("--annotations", type=Path, help="fixed human-review annotation CSV for one scene only")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--config", type=Path, default=ROOT / "configs/association/u3_same_frame_duplicate_candidate_v1.json")
    args = ap.parse_args()
    out = args.output_dir.resolve()
    if out.exists() or ROOT / "artifacts/runs" not in out.parents:
        raise FileExistsError(f"output must be new and located under {ROOT / 'artifacts/runs'}: {out}")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    baseline_path = ROOT / config["baseline_reference_lock"]
    if sha(baseline_path) != config["baseline_reference_lock_sha256"]:
        raise ValueError("locked source/geometry config hash changed; refusing paired replay")
    locked = json.loads(baseline_path.read_text(encoding="utf-8"))
    exp, baseline_cfg = locked["experimental"], config["baseline_config"]
    if baseline_cfg["existing_mask_dedup_iou_min"] != exp["duplicate_mask_iou_min"]:
        raise ValueError("existing mask dedup threshold differs from locked U3 config")
    if (baseline_cfg["geometry_only"]["candidate_center_radius_m"] != exp["candidate_center_radius_m"]
            or baseline_cfg["geometry_only"]["geometry_only_iou_min"] != exp["geometry_only_iou_min"]):
        raise ValueError("geometry-only thresholds differ from locked U3 config")
    if baseline_cfg["source_manager"] != locked["source_manager"]:
        raise ValueError("source-manager config differs from locked U3 config")
    started = time.perf_counter()
    out.mkdir(parents=True)
    reports = []
    for spec in args.cache:
        scene, raw_path = spec.split("=", 1)
        run = Path(raw_path).resolve()
        report = run_one(run, scene, config, out, args.annotations if scene == "00808" else None)
        reports.append(report)
    summary = {"schema_version": 1, "status": "completed_offline_development", "config_path": str(args.config.resolve()),
        "config_sha256": sha(args.config.resolve()), "runtime_seconds": time.perf_counter() - started,
        "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        "perception_rerun": False, "native_mapping_or_m9_rerun": False,
        "gt_or_room_labels_used_by_predictor": False, "scenes": reports}
    (out / "report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"], "output": str(out),
        "scenes": [{"scene": x["scene"], "obs": x["raw_p3_observations"], "tracks": x["track_counts"],
                    "suppressed": x["candidate_suppressed_observations"],
                    "changed_observations": x["changed_observation_decision_count_vs_geometry_only"],
                    "changed_pairs": x["changed_pairwise_relations_vs_geometry_only"]} for x in reports]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
