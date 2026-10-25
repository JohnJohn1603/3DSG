#!/usr/bin/env python3
"""Causal association replay over saved P3 observations, frames 0..238.

Runs four independent states: source/raw, source/deduplicated, geometry-only,
and geometry+appearance. No GT or source assignments enter prediction. The
evaluator is opened only after every mode has emitted its frame-ordered log.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import resource
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.config.schema import ObjectAssociationConfig  # noqa: E402
from standalone_sg.contracts.objects import Detection3D, DetectionBatch  # noqa: E402
from standalone_sg.objects.association import ObjectAssociationManager  # noqa: E402
from standalone_sg.objects.u3_experimental import (  # noqa: E402
    ExperimentalAssociation, U3Observation, deduplicate_frame_masks,
)

OUT = ROOT / "artifacts/audits/u3_causal_prefix"
APP_DIR = OUT / "appearance"
AUDIT_U2 = ROOT / "artifacts/audits/dining_table_bed_224_230/appearance"
HOLDOUT_START, HOLDOUT_END = 231, 238


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_external_lock(key: str) -> tuple[Path, dict]:
    lock = json.loads((ROOT / "locks/external_resources.json").read_text())["files"][key]
    path = Path(lock["path"])
    if path.stat().st_size != lock["bytes"] or sha_file(path) != lock["sha256"]:
        raise RuntimeError(f"locked external input changed: {key}")
    return path, lock


def current_rss_mib() -> float | None:
    try:
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * 4096 / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None


def read_inputs():
    index_path = OUT / "causal_input_index.json"
    digest_path = OUT / "causal_input_index.sha256"
    if not index_path.is_file() or not digest_path.is_file():
        raise RuntimeError("run extract_u3_causal_features.py first")
    if digest_path.read_text() != f"{sha_file(index_path)}  causal_input_index.json\n":
        raise RuntimeError("causal input index digest mismatch")
    index = json.loads(index_path.read_text())
    if index.get("status") != "pass" or index.get("record_count") != 622:
        raise RuntimeError("causal input index contract failed")
    features: dict[str, tuple[float, ...]] = {}
    for r in index["records"]:
        p = ROOT / r["feature_ref"]
        if sha_file(p) != r["feature_sha256"]:
            raise RuntimeError(f"feature hash mismatch: {r['detection_id']}")
        with np.load(p, allow_pickle=False) as data:
            vec = np.asarray(data["image_feature"], dtype=np.float32)
        if vec.shape != (512,) or not np.isfinite(vec).all() or not np.isclose(np.linalg.norm(vec), 1., atol=1e-5, rtol=1e-5):
            raise RuntimeError(f"feature contract failed: {r['detection_id']}")
        features[r["detection_id"]] = tuple(float(v) for v in vec)
        if r["center_W"] is None or r["extents_m"] is None:
            raise RuntimeError(f"frozen U3 AABB geometry missing: {r['detection_id']}")

    d3_path, d3_lock = load_external_lock("dense_detection_3d_cache")
    d3_rows = [json.loads(line) for line in d3_path.read_text().splitlines() if line]
    detection_map = {}
    for frame in d3_rows:
        frame_no = int(frame["frame_id"].rsplit("-", 1)[-1])
        if not 0 <= frame_no <= 238:
            continue
        for d in frame["detections"]:
            detection_map[d["detection_id"]] = d
    index_ids = {r["detection_id"] for r in index["records"]}
    if set(detection_map) != index_ids:
        raise RuntimeError("locked P3 observations differ from causal input index")

    # The cache and index must carry the same exact geometry/class provenance.
    observations = []
    index_by_id = {r["detection_id"]: r for r in index["records"]}
    for did in sorted(index_ids, key=lambda x: (int(x.split("-dense-")[1][:4]), x)):
        d, r = detection_map[did], index_by_id[did]
        if d["center_W"] != r["center_W"] or d["extents_m"] != r["extents_m"] or d["layer_id"] != r["layer_id"]:
            raise RuntimeError(f"P3 AABB differs from immutable index: {did}")
        frame_no = int(r["frame_index"])
        observations.append(U3Observation(
            detection_id=did, frame_id=r["frame_id"], timestamp_s=float(r["timestamp_s"]),
            center=tuple(float(x) for x in d["center_W"]),
            extents=tuple(float(x) for x in d["extents_m"]),
            layer_id=str(d["layer_id"]), class_name=d.get("class_name"),
            feature=features[did], mask_hash=r["mask_sha256"],
        ))
    return index, index_by_id, observations, detection_map, d3_lock


def load_masks(frame_observations: list[U3Observation], detection_map: dict[str, dict]) -> dict[str, np.ndarray]:
    _, d2_lock = load_external_lock("dense_detection_2d_index")
    d2_root = d2_lock["path"]
    d2_root = Path(d2_root).parent
    return {
        o.detection_id: np.asarray(np.load(d2_root / detection_map[o.detection_id]["provider_metadata"]["mask_file"], allow_pickle=False), dtype=bool)
        for o in frame_observations
    }


def source_replay(name: str, frames: dict[int, list[U3Observation]], detection_map: dict[str, dict], *, raw: bool,
                  duplicate_ids: set[str]) -> tuple[dict[str, str], dict, dict]:
    config = json.loads((OUT / "association_config.locked.json").read_text())
    source_cfg = config["source_manager"]
    manager = ObjectAssociationManager(ObjectAssociationConfig(
        float(source_cfg["same_class_radius_m"]), float(source_cfg["same_pose_suppression_cap_m"])))
    pred: dict[str, str] = {}
    runtime = {"wall_seconds": 0., "peak_sampled_rss_mib": current_rss_mib(), "candidate_rows": 0}
    decision_path = OUT / f"decisions_{name}.jsonl"
    candidate_path = OUT / f"candidates_{name}.jsonl"
    mode_start = time.perf_counter()
    snapshot_prefix = None
    with decision_path.open("w") as dout, candidate_path.open("w") as cout:
        for frame_no in sorted(frames):
            frame_obs = frames[frame_no]
            for obs in sorted(frame_obs, key=lambda o: o.detection_id):
                before = manager.snapshot()
                for node in before:
                    dist = math.dist(obs.center, node.center_W)
                    same_class = ObjectAssociationManager._normalize(obs.class_name) == ObjectAssociationManager._normalize(node.class_name)
                    row = {"mode": name, "frame_index": frame_no, "detection_id": obs.detection_id,
                           "candidate_track_id": node.object_id, "distance_m": dist,
                           "same_class": same_class,
                           "same_class_gate_pass": bool(same_class and dist <= source_cfg["same_class_radius_m"]),
                           "same_pose_gate_pass": bool(dist <= source_cfg["same_pose_suppression_cap_m"]),
                           "source_policy_score": dist,
                           "decision": "candidate_only"}
                    cout.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                    runtime["candidate_rows"] += 1
                d = detection_map[obs.detection_id]
                detection = Detection3D(
                    detection_id=obs.detection_id, timestamp_s=obs.timestamp_s, center_W=obs.center,
                    center_method=str(d["center_method"]), class_name=obs.class_name,
                    class_id=d.get("class_id"), confidence=d.get("confidence"), frame_id="world",
                    layer_id=obs.layer_id, extents_m=obs.extents,
                )
                result = manager.update(DetectionBatch("cached_p3", "world", obs.timestamp_s, (detection,)))
                association = result.associations[0]
                pred[obs.detection_id] = association.object_id
                dout.write(json.dumps({"mode": name, "frame_index": frame_no,
                                       "detection_id": obs.detection_id, "track_id": association.object_id,
                                       "decision": association.decision,
                                       "distance_m": association.distance_m,
                                       "duplicate_mask": obs.detection_id in duplicate_ids},
                                      sort_keys=True, allow_nan=False) + "\n")
            if frame_no == getattr(sys.modules[__name__], "PREFIX_SNAPSHOT_FRAME", 230):
                snapshot_prefix = [{"track_id": n.object_id, "class_name_source_only": n.class_name,
                                    "first_detection_id": n.first_detection_id,
                                    "last_detection_id": n.last_detection_id,
                                    "last_seen_s": n.last_seen_s, "center_W": n.center_W,
                                    "extents_m": n.extents_m, "layer_id": n.layer_id}
                                   for n in manager.snapshot()]
            rss = current_rss_mib()
            if rss is not None:
                runtime["peak_sampled_rss_mib"] = max(runtime["peak_sampled_rss_mib"] or 0., rss)
    runtime["wall_seconds"] = time.perf_counter() - mode_start
    return pred, runtime, {"prefix_track_count": len(snapshot_prefix or []), "tracks": snapshot_prefix or []}


def custom_replay(name: str, mode: str, frames: dict[int, list[U3Observation]], duplicate_ids: set[str]) -> tuple[dict[str, str], list[dict], dict, dict]:
    config = json.loads((OUT / "association_config.locked.json").read_text())["experimental"]
    # Keep the exact numeric thresholds from the U3 implementation/config.
    engine = ExperimentalAssociation(
        mode,
        max_center_m=float(config["candidate_center_radius_m"]),
        geom_min_iou=float(config["appearance_iou_min"]),
        geom_only_min_iou=float(config["geometry_only_iou_min"]),
        appearance_min_cosine=float(config["appearance_cosine_min"]),
        combined_min_score=float(config["combined_score_min"]),
        ambiguity_margin=0.05,
    )
    pred: dict[str, str] = {}
    decisions: list[dict] = []
    runtime = {"wall_seconds": 0., "peak_sampled_rss_mib": current_rss_mib(), "candidate_rows": 0,
               "defer_count": 0, "holdout_defer_count": 0}
    decision_path = OUT / f"decisions_{name}.jsonl"
    candidate_path = OUT / f"candidates_{name}.jsonl"
    mode_start = time.perf_counter()
    snapshot_prefix = None
    with decision_path.open("w") as dout, candidate_path.open("w") as cout:
        for frame_no in sorted(frames):
            frame = frames[frame_no]
            decisions_frame = engine.update_frame(frame)
            for row in engine.ledger:
                cout.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            runtime["candidate_rows"] += len(engine.ledger)
            engine.ledger.clear()
            for d in decisions_frame:
                row = {"mode": name, **asdict(d), "frame_index": frame_no,
                       "duplicate_mask": d.detection_id in duplicate_ids}
                decisions.append(row)
                dout.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                if d.track_id is not None:
                    pred[d.detection_id] = d.track_id
                if d.decision == "defer":
                    runtime["defer_count"] += 1
                    if HOLDOUT_START <= frame_no <= HOLDOUT_END:
                        runtime["holdout_defer_count"] += 1
            if frame_no == getattr(sys.modules[__name__], "PREFIX_SNAPSHOT_FRAME", 230):
                snapshot_prefix = [{"track_id": t.track_id, "observation_count": len(t.observations),
                                    "first_detection_id": t.observations[0],
                                    "last_detection_id": t.observations[-1],
                                    "center_W": t.center, "extents_m": t.extents,
                                    "layer_id": t.layer_id,
                                    "feature_dimension": len(t.feature) if t.feature else None}
                                   for t in engine.tracks]
            rss = current_rss_mib()
            if rss is not None:
                runtime["peak_sampled_rss_mib"] = max(runtime["peak_sampled_rss_mib"] or 0., rss)
    runtime["wall_seconds"] = time.perf_counter() - mode_start
    return pred, decisions, runtime, {"prefix_track_count": len(snapshot_prefix or []), "tracks": snapshot_prefix or []}


def evaluate_holdout(pred: dict[str, str], observations: list[U3Observation], evaluator: dict[str, dict],
                     duplicate_ids: set[str], mode: str) -> dict:
    obs_by_id = {o.detection_id: o for o in observations}
    raw_holdout_ids = {o.detection_id for o in observations
                       if HOLDOUT_START <= int(o.frame_id.rsplit("-", 1)[-1]) <= HOLDOUT_END}
    excluded_duplicates = set() if mode == "source_raw" else duplicate_ids
    holdout_ids = raw_holdout_ids - excluded_duplicates
    scored_ids = set(obs_by_id) - excluded_duplicates
    statuses = defaultdict(int)
    for did in holdout_ids:
        status = evaluator.get(did, {}).get("match_status", "missing_from_evaluator")
        statuses[str(status)] += 1
    matched = {did for did in scored_ids if evaluator.get(did, {}).get("match_status") == "matched"
               and evaluator[did].get("gt_semantic_id") is not None}
    # Only cross-frame pairs with at least one holdout member are scored. Pairs
    # missing evaluator identity are excluded, never treated as true associations.
    tp = fp = fn = tn = excluded = 0
    positive_pairs = negative_pairs = 0
    pair_count_all = 0
    all_ids = sorted(scored_ids)
    for i, left in enumerate(all_ids):
        for right in all_ids[i + 1:]:
            if left not in holdout_ids and right not in holdout_ids:
                continue
            if obs_by_id[left].frame_id == obs_by_id[right].frame_id:
                continue
            pair_count_all += 1
            if left not in matched or right not in matched:
                excluded += 1
                continue
            same_gt = evaluator[left]["gt_semantic_id"] == evaluator[right]["gt_semantic_id"]
            same_pred = pred.get(left) is not None and pred.get(left) == pred.get(right)
            if same_gt:
                positive_pairs += 1
                if same_pred: tp += 1
                else: fn += 1  # includes defer/unassigned
            else:
                negative_pairs += 1
                if same_pred: fp += 1
                else: tn += 1
    gt_to_tracks: dict[str, set[str]] = defaultdict(set)
    track_to_gt: dict[str, set[str]] = defaultdict(set)
    deferred_ids: set[str] = set()
    if mode in ("geometry_only", "geometry_appearance"):
        # Deferred detections have no prediction-map entry. They are explicit
        # abstentions, not correct singleton associations.
        deferred_ids = {did for did in holdout_ids if did not in pred}
    for did in matched:
        track = pred.get(did)
        gid = str(evaluator[did]["gt_semantic_id"])
        if track is not None:
            gt_to_tracks[gid].add(track)
            track_to_gt[track].add(gid)
    holdout_gt_ids = {str(evaluator[did]["gt_semantic_id"]) for did in holdout_ids & matched}
    fragmented = sum(len(gt_to_tracks[gid]) > 1 for gid in holdout_gt_ids)
    fragment_excess = sum(max(0, len(gt_to_tracks[gid]) - 1) for gid in holdout_gt_ids)
    touched_holdout_tracks = {pred[did] for did in holdout_ids if did in pred}
    false_merge_tracks = sum(len(track_to_gt[track]) > 1 for track in touched_holdout_tracks)
    return {
        "mode": mode, "holdout_observations": len(holdout_ids),
        "raw_holdout_observations_before_dedup": len(raw_holdout_ids),
        "holdout_duplicate_masks_excluded": len(raw_holdout_ids & excluded_duplicates),
        "gt_match_status_counts": dict(sorted(statuses.items())),
        "gt_match_coverage": {"matched": len(holdout_ids & matched), "total": len(holdout_ids),
                              "fraction": len(holdout_ids & matched) / len(holdout_ids) if holdout_ids else None,
                              "unmatched_or_unresolved": len(holdout_ids - matched)},
        "pairwise_scope": "cross-frame pairs with at least one frame-231..238 observation; both endpoints must have evaluator match_status=matched and semantic ID",
        "pairwise_pairs_total_before_gt_exclusion": pair_count_all,
        "pairwise_pairs_excluded_for_missing_gt_identity": excluded,
        "pairwise_pairs_scored": positive_pairs + negative_pairs,
        "same_gt_positive_pairs": positive_pairs, "different_gt_negative_pairs": negative_pairs,
        "TP": tp, "FP": fp, "FN": fn, "TN_diagnostic_only": tn,
        "pairwise_precision": tp / (tp + fp) if tp + fp else None,
        "pairwise_recall": tp / (tp + fn) if tp + fn else None,
        "defer_or_unassigned_holdout_count": len(deferred_ids),
        "defer_rate_over_all_holdout_observations": len(deferred_ids) / len(holdout_ids) if holdout_ids else None,
        "defer_rate_over_gt_matched_holdout": len(deferred_ids & matched) / len(holdout_ids & matched) if holdout_ids & matched else None,
        "false_merge_tracks_touched_by_holdout": false_merge_tracks,
        "fragmented_evaluator_instances_with_holdout": fragmented,
        "fragmentation_excess_tracks": fragment_excess,
        "fragmentation_note": "counts assigned predicted tracks only; deferred observations are abstentions and are reported separately, with their positive pairs as FN",
        "unknown_gt_detections_are_not_assumed_correct": True,
    }


def main() -> int:
    started = time.perf_counter()
    OUT.mkdir(parents=True, exist_ok=True)
    config_path = ROOT / "artifacts/audits/u3_association/association_config.json"
    config = json.loads(config_path.read_text())
    locked_config_path = OUT / "association_config.locked.json"
    config_payload = json.dumps(config, indent=2, sort_keys=True) + "\n"
    if locked_config_path.exists() and locked_config_path.read_text() != config_payload:
        raise RuntimeError("U3 thresholds/config changed since causal run was prepared")
    if not locked_config_path.exists():
        locked_config_path.write_text(config_payload)

    index, index_by_id, observations, detection_map, d3_lock = read_inputs()
    by_frame: dict[int, list[U3Observation]] = defaultdict(list)
    for o in observations:
        by_frame[int(o.frame_id.rsplit("-", 1)[-1])].append(o)
    dedup_frames: dict[int, list[U3Observation]] = {}
    duplicate_rows = []
    duplicate_ids: set[str] = set()
    d2_index_path, _ = load_external_lock("dense_detection_2d_index")
    d2_root = d2_index_path.parent
    for frame_no, frame_obs in by_frame.items():
        masks = {o.detection_id: np.asarray(np.load(d2_root / detection_map[o.detection_id]["provider_metadata"]["mask_file"], allow_pickle=False), dtype=bool)
                 for o in frame_obs}
        kept, duplicates = deduplicate_frame_masks(frame_obs, masks, iou_threshold=float(config["experimental"]["duplicate_mask_iou_min"]))
        dedup_frames[frame_no] = kept
        duplicate_rows.extend({**row, "frame_index": frame_no} for row in duplicates)
        duplicate_ids.update(row["detection_id"] for row in duplicates)
    raw_frames = dict(by_frame)
    (OUT / "deduplicated_mask_aliases.json").write_text(json.dumps({
        "policy": "same-frame mask IoU >= frozen U3 threshold",
        "threshold": config["experimental"]["duplicate_mask_iou_min"],
        "raw_observations": len(observations), "retained_observations": sum(map(len, dedup_frames.values())),
        "duplicates": duplicate_rows,
    }, indent=2, sort_keys=True) + "\n")

    mode_predictions: dict[str, dict[str, str]] = {}
    mode_runtime: dict[str, dict] = {}
    prefix_states: dict[str, dict] = {}
    mode_decisions: dict[str, dict[str, dict]] = {}
    # Run each strategy independently from frame zero; no identity/state is copied.
    for name, frames, raw in (("source_raw", raw_frames, True), ("source_dedup", dedup_frames, False)):
        pred, runtime, state = source_replay(name, frames, detection_map, raw=raw, duplicate_ids=duplicate_ids)
        mode_predictions[name] = pred; mode_runtime[name] = runtime; prefix_states[name] = state
    for name, mode in (("geometry_only", "geometry"), ("geometry_appearance", "geometry_appearance")):
        pred, decisions, runtime, state = custom_replay(name, mode, dedup_frames, duplicate_ids)
        mode_predictions[name] = pred; mode_runtime[name] = runtime; prefix_states[name] = state
        mode_decisions[name] = {row["detection_id"]: row for row in decisions}

    prefix_state_path = OUT / "prefix_state_after_230.json"
    prefix_state_path.write_text(json.dumps({"boundary_frame": 230, "states": prefix_states,
                                             "all_modes_started_empty": True,
                                             "source_predictions_not_used_to_seed_other_modes": True,
                                             "gt_used": False}, indent=2, sort_keys=True) + "\n")

    # Prediction logs are already complete and flushed. Only now read GT/evaluator.
    matches_path, matches_lock = load_external_lock("dense_object_evaluation_observation_matches")
    protocol_path, protocol_lock = load_external_lock("dense_object_evaluation_protocol")
    protocol = json.loads(protocol_path.read_text())
    if protocol.get("protocol_id") != "hm3d_object_gt_v1":
        raise RuntimeError("unexpected locked evaluator protocol")
    evaluator = {r["detection_id"]: r for r in (json.loads(line) for line in matches_path.read_text().splitlines() if line)}
    evaluations = {name: evaluate_holdout(pred, observations, evaluator, duplicate_ids, name)
                   for name, pred in mode_predictions.items()}
    prefix_ids = {o.detection_id for o in observations if int(o.frame_id.rsplit("-", 1)[-1]) <= 230}
    prefix_statuses = defaultdict(int)
    prefix_matched = set()
    for did in prefix_ids:
        row = evaluator.get(did, {})
        status = row.get("match_status", "missing_from_evaluator")
        prefix_statuses[str(status)] += 1
        if status == "matched" and row.get("gt_semantic_id") is not None:
            prefix_matched.add(did)
    prefix_coverage = {"matched": len(prefix_matched), "total": len(prefix_ids),
                       "fraction": len(prefix_matched) / len(prefix_ids),
                       "match_status_counts": dict(sorted(prefix_statuses.items()))}
    review = []
    # A deterministic case export, posthoc only; no hard-coded instance/category.
    for o in observations:
        frame_no = int(o.frame_id.rsplit("-", 1)[-1])
        if not HOLDOUT_START <= frame_no <= HOLDOUT_END:
            continue
        g = evaluator.get(o.detection_id, {})
        review.append({"frame_index": frame_no, "detection_id": o.detection_id,
                       "detector_class_review_only": o.class_name,
                       "gt_match_status_posthoc": g.get("match_status", "missing_from_evaluator"),
                       "gt_category_posthoc": g.get("gt_category"),
                       "gt_semantic_id_posthoc": g.get("gt_semantic_id"),
                       "track_source_raw": mode_predictions["source_raw"].get(o.detection_id),
                       "track_source_dedup": mode_predictions["source_dedup"].get(o.detection_id),
                       "track_geometry_only": mode_predictions["geometry_only"].get(o.detection_id),
                       "track_geometry_appearance": mode_predictions["geometry_appearance"].get(o.detection_id),
                       "decision_geometry_only": mode_decisions["geometry_only"].get(o.detection_id, {}).get("decision"),
                       "reason_geometry_only": mode_decisions["geometry_only"].get(o.detection_id, {}).get("reason"),
                       "decision_geometry_appearance": mode_decisions["geometry_appearance"].get(o.detection_id, {}).get("decision"),
                       "reason_geometry_appearance": mode_decisions["geometry_appearance"].get(o.detection_id, {}).get("reason")})
    review_path = OUT / "holdout_observations_posthoc.csv"
    with review_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(review[0]) if review else ["detection_id"])
        writer.writeheader(); writer.writerows(review)

    # Compare causal holdout->past link sets; mode-local track labels themselves
    # are arbitrary and must not be compared as if they were shared IDs.
    obs_by_id = {o.detection_id: o for o in observations}
    holdout_ordered = sorted((o for o in observations
                              if HOLDOUT_START <= int(o.frame_id.rsplit("-", 1)[-1]) <= HOLDOUT_END),
                             key=lambda o: (int(o.frame_id.rsplit("-", 1)[-1]), o.detection_id))
    changed = []
    for o in holdout_ordered:
        did = o.detection_id
        earlier = [p.detection_id for p in observations
                   if int(p.frame_id.rsplit("-", 1)[-1]) < int(o.frame_id.rsplit("-", 1)[-1])
                   and p.detection_id not in duplicate_ids]
        geo_links = sorted(pid for pid in earlier if mode_predictions["geometry_only"].get(did) is not None
                           and mode_predictions["geometry_only"].get(did) == mode_predictions["geometry_only"].get(pid))
        app_links = sorted(pid for pid in earlier if mode_predictions["geometry_appearance"].get(did) is not None
                           and mode_predictions["geometry_appearance"].get(did) == mode_predictions["geometry_appearance"].get(pid))
        if geo_links != app_links:
            g = evaluator.get(did, {})
            changed.append({"detection_id": did, "geometry_prior_links": geo_links,
                            "appearance_prior_links": app_links,
                            "geometry_decision": mode_decisions["geometry_only"].get(did, {}).get("decision"),
                            "appearance_decision": mode_decisions["geometry_appearance"].get(did, {}).get("decision"),
                            "gt_match_status_posthoc": g.get("match_status"),
                            "gt_category_posthoc": g.get("gt_category"),
                            "gt_semantic_id_posthoc": g.get("gt_semantic_id")})
    summary = {
        "schema_version": 1, "status": "causal_prefix_and_holdout_scored_not_promoted",
        "scene_scope": "same-scene temporal holdout; not a scene holdout",
        "prefix_frames": [0, 230], "holdout_frames": [231, 238],
        "raw_observations": len(observations),
        "raw_prefix_observations": sum(len(v) for k,v in raw_frames.items() if k <= 230),
        "raw_holdout_observations": sum(len(v) for k,v in raw_frames.items() if HOLDOUT_START <= k <= HOLDOUT_END),
        "dedup_observations": sum(len(v) for v in dedup_frames.values()),
        "same_frame_duplicate_masks_suppressed": len(duplicate_rows),
        "pointset_sidecars_available": index["counts"]["pointset_sidecars_available"],
        "aabb_geometry_available": index["counts"]["geometry_records_available"],
        "prefix_gt_match_coverage_posthoc": prefix_coverage,
        "models": {name: {"initial_state": "empty", "prefix_tracks_at_frame_230": prefix_states[name]["prefix_track_count"],
                          "prediction_count": len(mode_predictions[name]), **mode_runtime[name]}
                   for name in mode_predictions},
        "holdout_evaluation": evaluations,
        "appearance_effect": {"changed_detection_assignments": len(changed),
                               "changed_detection_ids": changed,
                               "interpretation": "change means the set of prior-frame detections linked to a holdout observation differs; mode-local track ID strings are not compared; no threshold changes after holdout"},
        "causal_constraints": {"gt_used_for_predictions": False,
                               "source_assignments_used_to_seed_other_modes": False,
                               "future_observations_used_to_revise_past": False,
                               "class_or_room_score_updated": False,
                               "source_code_modified": False,
                               "native_m1_m10_or_yolo_rerun": False},
        "evaluator_locks": {"observation_matches_sha256": matches_lock["sha256"],
                             "protocol_sha256": protocol_lock["sha256"], "protocol_id": protocol["protocol_id"]},
        "association_config_sha256": sha_file(locked_config_path),
        "input_index_sha256": sha_file(OUT / "causal_input_index.json"),
        "runtime_total_wall_seconds": time.perf_counter() - started,
        "process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.,
        "outputs": ["prefix_state_after_230.json", "deduplicated_mask_aliases.json",
                    "holdout_observations_posthoc.csv", "decisions_*.jsonl", "candidates_*.jsonl"],
    }
    (OUT / "causal_holdout_report.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
