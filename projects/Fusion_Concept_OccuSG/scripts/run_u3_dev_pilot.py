#!/usr/bin/env python3
"""Run the locked U3 association modes on one saved development capture.

This diagnostic consumes cached P2/P3 observations and frozen image features.
It never runs a detector, mapper, evaluator, or graph builder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import sys
import time
from collections import defaultdict
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


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_features(feature_dir: Path) -> tuple[dict[str, np.ndarray], dict[str, dict]]:
    manifest = read_jsonl(feature_dir / "feature_manifest.jsonl")
    features: dict[str, np.ndarray] = {}
    refs: dict[str, dict] = {}
    for row in manifest:
        path = ROOT / row["feature_path"]
        if sha(path) != row["feature_file_sha256"]:
            raise RuntimeError(f"feature hash mismatch: {row['observation_id']}")
        with np.load(path, allow_pickle=False) as archive:
            vector = np.asarray(archive["image_feature"], dtype=np.float32).reshape(-1)
        if vector.shape != (512,) or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5, rtol=1e-5):
            raise RuntimeError(f"invalid image feature: {row['observation_id']}")
        features[row["observation_id"]] = vector
        refs[row["observation_id"]] = row
    return features, refs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--u2-feature-dir", type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    output = args.output_dir.resolve()
    feature_dir = args.u2_feature_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    lock_path = ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json"
    lock = json.loads(lock_path.read_text())
    profile_path = ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json"
    p2_path = run / "detection_2d/detection_2d_index.json"
    p3_path = run / "detection_3d/detection_3d.jsonl"
    p2 = json.loads(p2_path.read_text())
    if p2.get("status") != "pass":
        raise RuntimeError("P2 cache is not authoritative/pass")
    if p2.get("capture_manifest_sha256") != sha(run / "capture/capture_manifest.json"):
        raise RuntimeError("P2 cache does not correspond to this capture")
    d2_by_id = {d["detection_id"]: d for frame in p2["frames"] for d in frame["detections"]}
    p3_batches = read_jsonl(p3_path)
    p3_by_id = {d["detection_id"]: d for batch in p3_batches for d in batch.get("detections", [])}
    if not p3_by_id:
        raise RuntimeError("P3 cache has no accepted observations")
    if set(p3_by_id) - set(d2_by_id):
        raise RuntimeError("P3 observations are absent from the P2 cache")
    features, feature_refs = load_features(feature_dir)
    if set(features) != set(p3_by_id):
        raise RuntimeError("U2 feature set must exactly cover accepted P3 observations")

    masks: dict[str, np.ndarray] = {}
    by_frame: dict[int, list[U3Observation]] = defaultdict(list)
    for batch in p3_batches:
        for det in batch.get("detections", []):
            did = det["detection_id"]
            p2det = d2_by_id[did]
            meta = det["provider_metadata"]
            obs = U3Observation(
                detection_id=did, frame_id=str(batch["frame_id"]), timestamp_s=float(det["timestamp_s"]),
                center=tuple(float(x) for x in det["center_W"]),
                extents=tuple(float(x) for x in det["extents_m"]),
                layer_id=str(det.get("layer_id", "floor_0")), class_name=str(det["class_name"]),
                feature=tuple(float(x) for x in features[did]), mask_hash=str(p2det["mask_sha256"]),
            )
            by_frame[int(meta["frame_index"])].append(obs)
            mask_path = run / "detection_2d" / p2det["mask_file"]
            masks[did] = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
    all_obs = [o for fi in sorted(by_frame) for o in sorted(by_frame[fi], key=lambda x: x.detection_id)]
    dedup_obs, duplicates = deduplicate_frame_masks(
        all_obs, masks, iou_threshold=float(lock["experimental"]["duplicate_mask_iou_min"])
    )
    duplicate_ids = {r["detection_id"] for r in duplicates}
    modes: dict[str, dict[str, str]] = {}
    reports: dict[str, dict] = {}

    # Recreate source manager independently for raw and deduplicated observations.
    for mode_name in ("source_raw", "source_dedup"):
        manager = ObjectAssociationManager(ObjectAssociationConfig(
            float(lock["source_manager"]["same_class_radius_m"]),
            float(lock["source_manager"]["same_pose_suppression_cap_m"]),
        ))
        excluded = duplicate_ids if mode_name == "source_dedup" else set()
        assignments: dict[str, str] = {}
        decisions: list[dict] = []
        candidates: list[dict] = []
        mode_start = time.perf_counter()
        for fi in sorted(by_frame):
            for obs in sorted(by_frame[fi], key=lambda x: x.detection_id):
                if obs.detection_id in excluded:
                    continue
                for node in manager.snapshot():
                    dist = math.dist(obs.center, node.center_W)
                    same_class = ObjectAssociationManager._normalize(obs.class_name) == ObjectAssociationManager._normalize(node.class_name)
                    candidates.append({"frame_index": fi, "detection_id": obs.detection_id,
                        "track_id": node.object_id, "distance_m": dist, "same_class": same_class,
                        "same_class_gate_pass": same_class and dist <= float(lock["source_manager"]["same_class_radius_m"]),
                        "same_pose_gate_pass": dist <= float(lock["source_manager"]["same_pose_suppression_cap_m"])})
                src = p3_by_id[obs.detection_id]
                detection = Detection3D(
                    detection_id=obs.detection_id, timestamp_s=obs.timestamp_s, center_W=obs.center,
                    center_method=str(src["center_method"]), class_name=str(src["class_name"]),
                    class_id=str(src.get("class_id", "")), confidence=src.get("confidence"),
                    frame_id="world", layer_id=obs.layer_id, extents_m=obs.extents,
                )
                result = manager.update(DetectionBatch("saved_p3", "world", obs.timestamp_s, (detection,))).associations[0]
                assignments[obs.detection_id] = result.object_id
                decisions.append({"frame_index": fi, "detection_id": obs.detection_id, "track_id": result.object_id,
                                  "decision": result.decision, "distance_m": result.distance_m})
        modes[mode_name] = assignments
        reports[mode_name] = {"decisions": decisions, "candidates": candidates,
            "runtime_seconds": time.perf_counter() - mode_start,
            "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0}

    exp = lock["experimental"]
    for mode_name, internal_mode in (("geometry_only", "geometry"), ("geometry_appearance", "geometry_appearance")):
        engine = ExperimentalAssociation(
            internal_mode, max_center_m=float(exp["candidate_center_radius_m"]),
            geom_min_iou=float(exp["appearance_iou_min"]),
            geom_only_min_iou=float(exp["geometry_only_iou_min"]),
            appearance_min_cosine=float(exp["appearance_cosine_min"]),
            combined_min_score=float(exp["combined_score_min"]), ambiguity_margin=0.05,
        )
        assignments: dict[str, str] = {}
        decisions: list[dict] = []
        candidates: list[dict] = []
        mode_start = time.perf_counter()
        for fi in sorted(by_frame):
            observations = [o for o in by_frame[fi] if o.detection_id not in duplicate_ids]
            result = engine.update_frame(observations)
            decisions.extend({"frame_index": fi, **d.__dict__} for d in result)
            candidates.extend({"frame_index": fi, **r} for r in engine.ledger)
            engine.ledger.clear()
            for d in result:
                if d.track_id is not None:
                    assignments[d.detection_id] = d.track_id
        modes[mode_name] = assignments
        reports[mode_name] = {"decisions": decisions, "candidates": candidates,
            "runtime_seconds": time.perf_counter() - mode_start,
            "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0}

    alias = {r["detection_id"]: r["duplicate_of"] for r in duplicates}
    for name, assignments in modes.items():
        path = output / f"assignments_{name}.jsonl"
        rows = []
        for obs in all_obs:
            did = obs.detection_id
            rep = alias.get(did) if name != "source_raw" else None
            track_id = assignments.get(did) or (assignments.get(rep) if rep else None)
            rows.append({"mode": name, "frame_index": int(p3_by_id[did]["provider_metadata"]["frame_index"]),
                "frame_id": obs.frame_id, "detection_id": did, "track_id": track_id,
                "decision": "duplicate_alias" if rep else next((d["decision"] for d in reports[name]["decisions"] if d["detection_id"] == did), "unassigned"),
                "duplicate_alias": bool(rep), "duplicate_of": rep})
        path.write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for row in rows))
        reports[name]["assignments_path"] = path.name
        reports[name]["assigned_observations"] = sum(bool(r["track_id"]) for r in rows)
        reports[name]["unique_tracks"] = len({r["track_id"] for r in rows if r["track_id"]})
        (output / f"decisions_{name}.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for r in reports[name]["decisions"]))
        (output / f"candidates_{name}.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for r in reports[name]["candidates"]))
    (output / "duplicate_aliases.json").write_text(json.dumps({"threshold_iou": exp["duplicate_mask_iou_min"], "duplicates": duplicates}, indent=2, ensure_ascii=False) + "\n")
    report = {"status": "completed_unscored_development", "run_dir": str(run.relative_to(ROOT)),
        "accepted_p3_observations": len(all_obs), "same_frame_duplicate_masks_suppressed": len(duplicates),
        "u3_config_sha256": sha(lock_path), "p3_sha256": sha(p3_path), "p2_index_sha256": sha(p2_path),
        "u2_feature_manifest_sha256": sha(feature_dir / "feature_manifest.jsonl"),
        "source_association_code_modified": False, "predictor_used_gt_or_room_labels": False,
        "state_initialization": "each mode starts from empty state at the beginning of this captured five-frame route",
        "modes": reports, "runtime_seconds_total": time.perf_counter() - started,
        "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        "metrics": {"status": "unavailable_pending_user_identity_review", "reason": "no evaluator identity annotations for this new development pilot"}}
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"status": report["status"], "accepted": len(all_obs), "duplicates": len(duplicates),
        "mode_tracks": {k: v["unique_tracks"] for k, v in reports.items()}, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
