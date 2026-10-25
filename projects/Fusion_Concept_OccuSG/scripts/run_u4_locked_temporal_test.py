#!/usr/bin/env python3
"""Causal U3/U4 replay and scoring for the locked 239..278 test interval.

Predictor phase reads locked saved P3/P2/RGB observations and U2 features only.
Evaluator data is loaded after all association and class predictions are frozen.
Each association mode starts empty at frame 0 and replays through frame 278.
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
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import run_u3_causal_prefix as u3  # noqa: E402
import extract_u2_appearance as u2  # noqa: E402
from standalone_sg.objects.u3_experimental import U3Observation, deduplicate_frame_masks  # noqa: E402
from standalone_sg.semantics.u4_class_fusion import CausalClassEvidence, ClassObservation, quantize_view_bin  # noqa: E402

LOCK_DIR = ROOT / "artifacts/audits/u4_temporal_test_lock"
U3_PREFIX = ROOT / "artifacts/audits/u3_causal_prefix"
U4_LOCK = ROOT / "artifacts/audits/u4_semantic_fusion/u4_config.locked.json"
OUT = ROOT / "artifacts/audits/u4_temporal_test_run"
START, END, PREFIX_END = 239, 278, 238
MODES = ("source_raw", "source_dedup", "geometry_only", "geometry_appearance")


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def locked_file(key: str) -> tuple[Path, dict]:
    lock = json.loads((ROOT / "locks/external_resources.json").read_text())["files"][key]
    path = Path(lock["path"])
    if not path.is_file() or path.stat().st_size != lock["bytes"] or sha_file(path) != lock["sha256"]:
        raise RuntimeError(f"locked input changed: {key}")
    return path, lock


def verify_locks() -> tuple[dict, dict]:
    manifest_path = LOCK_DIR / "temporal_test_manifest.json"
    manifest_hash = (LOCK_DIR / "temporal_test_manifest.sha256").read_text().split()[0]
    if sha_file(manifest_path) != manifest_hash:
        raise RuntimeError("temporal test manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text())
    if manifest["frame_range_inclusive"] != [START, END] or manifest["prefix_to_replay_for_state"] != [0, PREFIX_END]:
        raise RuntimeError("unexpected temporal split contract")
    expected = manifest["input_locks"]
    for key, file_key in (("capture_manifest_sha256", "dense_capture_manifest"),
                          ("detection_2d_index_sha256", "dense_detection_2d_index"),
                          ("detection_3d_cache_sha256", "dense_detection_3d_cache"),
                          ("evaluator_matches_sha256", "dense_object_evaluation_observation_matches")):
        _, lock = locked_file(file_key)
        if lock["sha256"] != expected[key]:
            raise RuntimeError(f"manifest input lock mismatch: {file_key}")
    association_cfg = U3_PREFIX / "association_config.locked.json"
    if sha_file(association_cfg) != expected["u3_association_config_sha256"]:
        raise RuntimeError("U3 config no longer matches temporal test lock")
    if sha_file(U4_LOCK) != expected["u4_semantic_config_sha256"]:
        raise RuntimeError("U4 config no longer matches temporal test lock")
    return manifest, expected


def load_inputs(manifest: dict):
    # Prefix features were frozen before this test split. Check immutable index.
    index_path = U3_PREFIX / "causal_input_index.json"
    digest = (U3_PREFIX / "causal_input_index.sha256").read_text().split()[0]
    if sha_file(index_path) != digest:
        raise RuntimeError("locked prefix feature index hash mismatch")
    prefix_index = json.loads(index_path.read_text())
    prefix_rows = {r["detection_id"]: r for r in prefix_index["records"]}
    prefix_features = {}
    for did, row in prefix_rows.items():
        p = ROOT / row["feature_ref"]
        if sha_file(p) != row["feature_sha256"]:
            raise RuntimeError(f"prefix feature changed: {did}")
        with np.load(p, allow_pickle=False) as z:
            vec = np.asarray(z["image_feature"], dtype=np.float32)
        validate_feature(did, vec)
        prefix_features[did] = vec

    capture_path, _ = locked_file("dense_capture_manifest")
    d2_path, _ = locked_file("dense_detection_2d_index")
    d3_path, d3_lock = locked_file("dense_detection_3d_cache")
    capture_root, d2_root = capture_path.parent, d2_path.parent
    capture = json.loads(capture_path.read_text())
    d2_index = json.loads(d2_path.read_text())
    capture_by_fid = {r["frame_id"]: r for r in capture["frames"]}
    d2_frames = {r["frame_id"]: r for r in d2_index["frames"]}
    d2_by_id = {d["detection_id"]: d for f in d2_index["frames"] for d in f["detections"]}
    d3_frames = [json.loads(line) for line in d3_path.read_text().splitlines() if line]
    d3_by_id = {}
    for frame in d3_frames:
        frame_no = int(frame["frame_id"].rsplit("-", 1)[-1])
        if 0 <= frame_no <= END:
            for det in frame["detections"]:
                d3_by_id[det["detection_id"]] = det
    test_records = manifest["observation_ids_and_input_hashes"]
    test_ids = {r["detection_id"] for r in test_records}
    actual_test_ids = {did for did in d3_by_id if START <= int(did.split(":", 1)[0].rsplit("-", 1)[-1]) <= END}
    if test_ids != actual_test_ids or len(test_ids) != manifest["p3_observation_count"]:
        raise RuntimeError("temporal-test observation IDs differ from split lock")

    test_index_rows = []
    new_rows = []
    for test in test_records:
        did = test["detection_id"]
        det = d3_by_id[did]
        fid = did.split(":", 1)[0]
        if det.get("provider_metadata", {}).get("room_gt_used") or det.get("provider_metadata", {}).get("semantic_gt_used"):
            raise RuntimeError(f"GT leakage provenance flag in predictor record: {did}")
        if did not in d2_by_id or fid not in capture_by_fid:
            raise RuntimeError(f"saved RGB/P2 observation missing: {did}")
        d2 = d2_by_id[did]
        rgb_path = capture_root / capture_by_fid[fid]["frame_dir"] / "rgb.npy"
        mask_path = d2_root / d2["mask_file"]
        if sha_file(rgb_path) != test["rgb_sha256"] or sha_file(mask_path) != test["mask_sha256"]:
            raise RuntimeError(f"temporal split RGB/mask hash changed: {did}")
        geom = {"center_W": det["center_W"], "extents_m": det["extents_m"], "layer_id": det["layer_id"]}
        geom_sha = hashlib.sha256(json.dumps(geom, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if geom_sha != test["geometry_sha256"]:
            raise RuntimeError(f"temporal split geometry hash changed: {did}")
        row = {"detection_id": did, "frame_id": fid,
               "frame_index": int(capture_by_fid[fid]["frame_index"]),
               "timestamp_s": float(det["timestamp_s"]), "d3": det, "d2": d2,
               "rgb_path": rgb_path, "mask_path": mask_path,
               "rgb_sha256": test["rgb_sha256"], "mask_sha256": test["mask_sha256"]}
        test_index_rows.append(row)
        if did not in prefix_features:
            new_rows.append(row)

    new_features, feature_manifest = extract_test_features(new_rows, capture_by_fid)
    features = {**prefix_features, **new_features}
    # Build deterministic all-observation view from saved D3 only.
    observations: list[U3Observation] = []
    det_map = {}
    records = []
    for did, det in d3_by_id.items():
        fno = int(did.split(":", 1)[0].rsplit("-", 1)[-1])
        if not 0 <= fno <= END:
            continue
        d2 = d2_by_id.get(did)
        if d2 is None:
            raise RuntimeError(f"P3 observation lacks cached P2 mask: {did}")
        feat = features.get(did)
        if feat is None:
            raise RuntimeError(f"missing image feature: {did}")
        frame_id = did.split(":", 1)[0]
        obs = U3Observation(detection_id=did, frame_id=frame_id,
                           timestamp_s=float(det["timestamp_s"]),
                           center=tuple(float(x) for x in det["center_W"]),
                           extents=tuple(float(x) for x in det["extents_m"]),
                           layer_id=str(det["layer_id"]), class_name=det.get("class_name"),
                           feature=tuple(float(v) for v in feat), mask_hash=d2["mask_sha256"])
        observations.append(obs)
        det_map[did] = det
        records.append({"detection_id": did, "frame_id": frame_id, "frame_index": fno,
                        "timestamp_s": float(det["timestamp_s"]), "center_W": det["center_W"],
                        "extents_m": det["extents_m"], "layer_id": det["layer_id"]})
    observations.sort(key=lambda o: (int(o.frame_id.rsplit("-", 1)[-1]), o.timestamp_s, o.detection_id))
    # Freeze the actual per-observation feature provenance in a new immutable index.
    all_index = []
    for obs in observations:
        did = obs.detection_id
        if did in prefix_rows:
            r = prefix_rows[did]
            feature_ref, feature_sha, mask_sha = r["feature_ref"], r["feature_sha256"], r["mask_sha256"]
        else:
            r = feature_manifest["records"][did]
            feature_ref, feature_sha, mask_sha = r["feature_ref"], r["feature_sha256"], r["mask_sha256"]
        d = det_map[did]
        all_index.append({"detection_id": did, "frame_id": obs.frame_id,
                          "frame_index": int(obs.frame_id.rsplit("-", 1)[-1]),
                          "feature_ref": feature_ref, "feature_sha256": feature_sha,
                          "mask_sha256": mask_sha, "center_W": d["center_W"],
                          "extents_m": d["extents_m"], "layer_id": d["layer_id"]})
    data_index = {"schema_version": 1, "status": "immutable_temporal_test_input_index",
                  "causal_replay_frames": [0, END], "prefix_frames": [0, PREFIX_END],
                  "temporal_test_frames": [START, END], "record_count": len(all_index),
                  "test_record_count": len(test_ids), "records": all_index,
                  "u3_prefix_index_sha256": digest,
                  "test_manifest_sha256": sha_file(LOCK_DIR / "temporal_test_manifest.json"),
                  "feature_provenance": feature_manifest["provenance"]}
    OUT.mkdir(parents=True, exist_ok=True)
    idx_path = OUT / "causal_input_index.json"
    idx_payload = json.dumps(data_index, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if idx_path.exists() and idx_path.read_text() != idx_payload:
        raise RuntimeError("temporal input index already exists and differs; refusing overwrite")
    idx_path.write_text(idx_payload)
    (OUT / "causal_input_index.sha256").write_text(f"{sha_file(idx_path)}  causal_input_index.json\n")
    return observations, det_map, test_ids, d3_lock, feature_manifest


def validate_feature(did: str, vec: np.ndarray):
    if vec.shape != (512,) or not np.isfinite(vec).all() or not np.isclose(float(np.linalg.norm(vec)), 1., atol=1e-5, rtol=1e-5):
        raise RuntimeError(f"feature contract failed: {did}")


def extract_test_features(rows: list[dict], capture_by_fid: dict) -> tuple[dict[str, np.ndarray], dict]:
    out_root = OUT / "appearance"
    crops_dir, feature_dir = out_root / "crops", out_root / "features"
    crops_dir.mkdir(parents=True, exist_ok=True); feature_dir.mkdir(parents=True, exist_ok=True)
    meta_path = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878/checkpoint_metadata.json"
    meta = json.loads(meta_path.read_text())
    checkpoint = meta_path.parent / meta["filename"]
    u2_report = json.loads((ROOT / "artifacts/audits/dining_table_bed_224_230/appearance/extraction_report.json").read_text())
    bench = json.loads((ROOT / "artifacts/audits/dining_table_bed_224_230/appearance/benchmark_report.json").read_text())
    if sha_file(checkpoint) != bench["model"]["checkpoint_file_sha256"] or meta["sha256"] != bench["model"]["checkpoint_file_sha256"]:
        raise RuntimeError("pinned U2 checkpoint missing or changed")
    model, preprocess, torch = u2.load_model(checkpoint)
    model_fp = u2.canonical_hash({"architecture": u2.MODEL_NAME, "checkpoint_repo": u2.CHECKPOINT_REPO,
                                 "checkpoint_revision": u2.REVISION, "checkpoint_sha256": sha_file(checkpoint)})
    preprocess_fp = u2.canonical_hash({"id": u2.PREPROCESSING_ID,
                                       "repr": u2.preprocessing_description(preprocess)})
    if model_fp != u2_report["model_fingerprint"] or preprocess_fp != u2_report["preprocessing_fingerprint"]:
        raise RuntimeError("U2 model/preprocessing fingerprint differs from approved lock")
    result, manifest_rows = {}, {}
    frame_cache = {}
    infer_durations = []
    for row in rows:
        did, fid = row["detection_id"], row["frame_id"]
        if fid not in frame_cache:
            rgb = np.asarray(np.load(row["rgb_path"], allow_pickle=False))
            if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] < 3:
                raise RuntimeError(f"invalid saved RGB array: {row['rgb_path']}")
            frame_cache[fid] = rgb
        d2 = row["d2"]
        crop, crop_box = u2.make_crop({"rgb": frame_cache[fid], "bbox_xyxy": d2["bbox_xyxy"]})
        pixels = np.asarray(crop, dtype=np.uint8)
        crop_sha = u2.sha256_bytes(pixels.tobytes())
        cache_key = u2.canonical_hash({"rgb_sha256": row["rgb_sha256"], "mask_sha256": row["mask_sha256"],
                                      "bbox_xyxy": [float(x) for x in d2["bbox_xyxy"]],
                                      "crop_policy": u2.PREPROCESSING_ID, "crop_sha256": crop_sha,
                                      "model_fingerprint": model_fp,
                                      "preprocessing_fingerprint": preprocess_fp})
        crop_path = crops_dir / f"{did.replace(':', '__')}__{crop_sha[:16]}.png"
        if not crop_path.exists():
            crop.save(crop_path, format="PNG", optimize=False)
        elif u2.sha256_bytes(np.asarray(Image.open(crop_path).convert("RGB"), dtype=np.uint8).tobytes()) != crop_sha:
            raise RuntimeError(f"existing crop cache mismatch: {did}")
        feature_path = feature_dir / f"{did.replace(':', '__')}__{cache_key[:16]}.npz"
        if feature_path.exists():
            with np.load(feature_path, allow_pickle=False) as z:
                vec = np.asarray(z["image_feature"], dtype=np.float32)
        else:
            t0 = time.perf_counter()
            vec = u2.encode_crop(model, preprocess, torch, pixels)
            infer_durations.append(time.perf_counter() - t0)
            np.savez_compressed(feature_path, image_feature=vec)
        validate_feature(did, vec)
        result[did] = vec
        manifest_rows[did] = {"detection_id": did, "frame_id": fid,
                              "rgb_sha256": row["rgb_sha256"], "mask_sha256": row["mask_sha256"],
                              "crop_sha256": crop_sha, "crop_box_xyxy_float": crop_box,
                              "crop_ref": str(crop_path.relative_to(ROOT)), "crop_file_sha256": sha_file(crop_path),
                              "model_fingerprint": model_fp, "preprocessing_id": u2.PREPROCESSING_ID,
                              "preprocessing_fingerprint": preprocess_fp, "cache_key_sha256": cache_key,
                              "feature_ref": str(feature_path.relative_to(ROOT)), "feature_sha256": sha_file(feature_path),
                              "feature_shape": list(vec.shape), "feature_finite": bool(np.isfinite(vec).all()),
                              "feature_l2_norm": float(np.linalg.norm(vec)), "device": "cpu"}
    (out_root / "feature_manifest.jsonl").write_text("".join(
        json.dumps(manifest_rows[did], sort_keys=True) + "\n" for did in sorted(manifest_rows)))
    report = {"new_test_crop_count": len(rows), "model_revision": u2.REVISION,
              "model_fingerprint": model_fp, "preprocessing_id": u2.PREPROCESSING_ID,
              "preprocessing_fingerprint": preprocess_fp, "checkpoint_sha256": sha_file(checkpoint),
              "checkpoint_bytes": checkpoint.stat().st_size, "device": "cpu",
              "feature_dimension": 512, "feature_shape_finite_normalized": True,
              "normalization_tolerance_atol_rtol": 1e-5,
              "inference_seconds_new_files_only": sum(infer_durations),
              "mean_seconds_per_new_feature": float(np.mean(infer_durations)) if infer_durations else None,
              "reused_feature_files": len(rows) - len(infer_durations),
              "feature_timing_scope": "completed replay invocation only; cumulative setup inference was performed across interrupted/retried preparation invocations and is unavailable",
              "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
              "provenance": {"saved_rgb": True, "saved_p2_boxes_masks": True, "saved_p3_accepted_ids": True,
                             "detector_rerun": False, "p3_rerun": False, "native_mapping_rerun": False,
                             "ground_truth_used": False}}
    (out_root / "feature_extraction_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return result, {"provenance": "pinned OpenCLIP ViT-B-32/OpenAI U2 extraction with cache keyed by RGB, mask, bbox/crop, model and preprocessing hashes",
                    "records": manifest_rows, "report": report}


def frame_no(obs: U3Observation) -> int:
    return int(obs.frame_id.rsplit("-", 1)[-1])


def replay_association(observations, det_map, test_ids):
    by_frame: dict[int, list[U3Observation]] = defaultdict(list)
    for o in observations:
        by_frame[frame_no(o)].append(o)
    _, d2_lock = locked_file("dense_detection_2d_index")
    d2_root = Path(d2_lock["path"]).parent
    raw_frames = dict(by_frame)
    dedup_frames, duplicate_ids, duplicate_rows = {}, set(), []
    u3_config = json.loads((U3_PREFIX / "association_config.locked.json").read_text())
    for fno, frame_obs in by_frame.items():
        masks = {o.detection_id: np.asarray(np.load(d2_root / det_map[o.detection_id]["provider_metadata"]["mask_file"], allow_pickle=False), dtype=bool)
                 for o in frame_obs}
        kept, duplicates = deduplicate_frame_masks(frame_obs, masks,
            iou_threshold=float(u3_config["experimental"]["duplicate_mask_iou_min"]))
        dedup_frames[fno] = kept
        duplicate_rows.extend({**r, "frame_index": fno} for r in duplicates)
        duplicate_ids.update(r["detection_id"] for r in duplicates)
    # The original runner's helpers now write only in this new run directory.
    u3.OUT = OUT
    u3.PREFIX_SNAPSHOT_FRAME = PREFIX_END
    config_payload = json.dumps(u3_config, indent=2, sort_keys=True) + "\n"
    (OUT / "association_config.locked.json").write_text(config_payload)
    predictions, decisions, runtimes, prefix_states = {}, {}, {}, {}
    for mode, frames, raw in (("source_raw", raw_frames, True), ("source_dedup", dedup_frames, False)):
        pred, runtime, state = u3.source_replay(mode, frames, det_map, raw=raw, duplicate_ids=duplicate_ids)
        predictions[mode], runtimes[mode], prefix_states[mode] = pred, runtime, state
        decisions[mode] = {json.loads(line)["detection_id"]: json.loads(line)
                           for line in (OUT / f"decisions_{mode}.jsonl").read_text().splitlines() if line}
    for mode_name, mode in (("geometry_only", "geometry"), ("geometry_appearance", "geometry_appearance")):
        pred, rows, runtime, state = u3.custom_replay(mode_name, mode, dedup_frames, duplicate_ids)
        predictions[mode_name], runtimes[mode_name], prefix_states[mode_name] = pred, runtime, state
        decisions[mode_name] = {r["detection_id"]: r for r in rows}
    (OUT / "deduplicated_mask_aliases.json").write_text(json.dumps({
        "policy": "same-frame mask IoU >= locked U3 threshold", "threshold": u3_config["experimental"]["duplicate_mask_iou_min"],
        "raw_observations": len(observations), "retained_observations": sum(map(len, dedup_frames.values())),
        "duplicates": duplicate_rows}, indent=2, sort_keys=True) + "\n")
    (OUT / "prefix_state_after_238.json").write_text(json.dumps({
        "boundary_frame": PREFIX_END, "states": prefix_states, "all_modes_started_empty": True,
        "each_mode_replayed_frames_0_through_278": True,
        "ground_truth_used_for_predictor": False}, indent=2, sort_keys=True) + "\n")
    return predictions, decisions, runtimes, duplicate_ids, duplicate_rows, raw_frames, dedup_frames


def run_class_fusion(observations, predictions, duplicate_ids, d3_lock):
    config = json.loads(U4_LOCK.read_text())
    aliases = {r["detection_id"]: r["duplicate_of"] for r in json.loads((OUT / "deduplicated_mask_aliases.json").read_text())["duplicates"]}
    state_rows_by_mode, obs_rows_by_mode, summaries = {}, {}, {}
    for mode in MODES:
        states, obs_rows, state_rows, track_summaries = {}, [], [], {}
        for obs in observations:
            did, fno = obs.detection_id, frame_no(obs)
            track_id = predictions[mode].get(did)
            duplicate = did in aliases and mode != "source_raw"
            if duplicate and track_id is None:
                track_id = predictions[mode].get(aliases[did])
            det = d3_lock[did]
            raw_class = det.get("class_name") or det.get("provider_metadata", {}).get("class_raw")
            confidence = float(det.get("confidence") or 0.)
            pose = det.get("provider_metadata", {}).get("camera_pose_W_Copt")
            if isinstance(pose, list) and len(pose) >= 3:
                camera = (float(pose[0][3]), float(pose[1][3]), float(pose[2][3]))
                view_bin = quantize_view_bin(camera, obs.center,
                    azimuth_bin_deg=int(config["evidence"]["view_bin_azimuth_degrees"]),
                    elevation_bin_deg=int(config["evidence"]["view_bin_elevation_degrees"]))
            else:
                view_bin = "unknown"
            include = bool(track_id) and not duplicate and raw_class is not None
            row = {"mode": mode, "detection_id": did, "frame_index": fno,
                   "raw_class": raw_class, "raw_confidence": confidence,
                   "track_id": track_id, "duplicate_alias": duplicate,
                   "duplicate_of": aliases.get(did) if duplicate else None,
                   "view_bin": view_bin, "evidence_included": include}
            if include:
                st = states.setdefault(str(track_id), CausalClassEvidence(
                    min_independent_views=int(config["evidence"]["minimum_independent_view_bins"]),
                    minimum_score_margin=float(config["evidence"]["minimum_winner_score_margin"]),
                    minimum_winner_fraction=float(config["evidence"]["minimum_winner_fraction"])))
                current = st.add(ClassObservation(did, fno, obs.timestamp_s, str(raw_class), confidence, view_bin))
                row.update({k: current[k] for k in ("initial_class", "resolved_class", "class_status", "candidate_class", "independent_view_bins_by_class")})
                state_rows.append({"mode": mode, "track_id": track_id, **current})
            else:
                row.update({"initial_class": None, "resolved_class": None,
                            "class_status": "duplicate_alias" if duplicate else "unassigned" if not track_id else "tentative",
                            "candidate_class": None, "independent_view_bins_by_class": {}})
            obs_rows.append(row)
        for tid, st in states.items():
            track_summaries[tid] = {"track_id": tid, **st.summary()}
        obs_rows_by_mode[mode], state_rows_by_mode[mode], summaries[mode] = obs_rows, state_rows, track_summaries
        with (OUT / f"u4_observations_{mode}.jsonl").open("w") as f:
            for row in obs_rows:
                f.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        with (OUT / f"u4_states_{mode}.jsonl").open("w") as f:
            for row in state_rows:
                f.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        (OUT / f"u4_track_summary_{mode}.json").write_text(json.dumps(track_summaries, indent=2, sort_keys=True) + "\n")
    return obs_rows_by_mode, summaries


def posthoc_score(predictions, decisions, runtimes, duplicate_ids, observations, test_ids, obs_rows_by_mode):
    matches_path, matches_lock = locked_file("dense_object_evaluation_observation_matches")
    evaluator = {r["detection_id"]: r for r in (json.loads(line) for line in matches_path.read_text().splitlines() if line)}
    byid = {o.detection_id: o for o in observations}
    results = {}
    for mode in MODES:
        excluded_duplicates = set() if mode == "source_raw" else duplicate_ids
        scoped = test_ids - excluded_duplicates
        scored_ids = set(byid) - excluded_duplicates
        matched_all = {did for did in scored_ids if evaluator.get(did, {}).get("match_status") == "matched"
                       and evaluator[did].get("gt_semantic_id") is not None}
        matched_test = scoped & matched_all
        status_counts = defaultdict(int)
        for did in scoped:
            status_counts[str(evaluator.get(did, {}).get("match_status", "missing_from_evaluator"))] += 1
        tp = fp = fn = tn = excluded = 0
        pos = neg = 0
        pair_total = 0
        ids = sorted(set(byid) - excluded_duplicates)
        holdout_set = scoped
        for i, a in enumerate(ids):
            for b in ids[i+1:]:
                if a not in holdout_set and b not in holdout_set:
                    continue
                if byid[a].frame_id == byid[b].frame_id:
                    continue
                pair_total += 1
                if a not in matched_all or b not in matched_all:
                    excluded += 1
                    continue
                same_gt = evaluator[a]["gt_semantic_id"] == evaluator[b]["gt_semantic_id"]
                same_pred = predictions[mode].get(a) is not None and predictions[mode].get(a) == predictions[mode].get(b)
                if same_gt:
                    pos += 1
                    if same_pred: tp += 1
                    else: fn += 1
                else:
                    neg += 1
                    if same_pred: fp += 1
                    else: tn += 1
        gt_to_tracks, track_to_gt = defaultdict(set), defaultdict(set)
        for did in matched_all:
            track = predictions[mode].get(did)
            if track is not None:
                gid = str(evaluator[did]["gt_semantic_id"])
                gt_to_tracks[gid].add(track); track_to_gt[track].add(gid)
        touched_tracks = {predictions[mode][did] for did in scoped if predictions[mode].get(did) is not None}
        holdout_gt_ids = {str(evaluator[did]["gt_semantic_id"]) for did in matched_test}
        deferred = {did for did in scoped if did not in predictions[mode]}
        prefix_test = [r for r in obs_rows_by_mode[mode] if r["detection_id"] in test_ids]
        class_confirmed = [r for r in prefix_test if r["class_status"] == "confirmed" and r["resolved_class"]]
        # No global category mapping is confirmed; class correctness stays unavailable.
        results[mode] = {
            "test_observation_count": len(scoped),
            "evaluator_match_status_counts": dict(sorted(status_counts.items())),
            "gt_match_coverage": {"matched": len(matched_test), "total": len(scoped), "fraction": len(matched_test)/len(scoped) if scoped else None},
            "pairwise_scope": "cross-frame pairs across the causal replay with at least one test observation; both endpoints require matched evaluator semantic IDs",
            "pairwise_total_before_gt_exclusion": pair_total, "pairs_excluded_missing_gt_identity": excluded,
            "pairs_scored": pos + neg, "same_identity_positive_pairs": pos, "different_identity_negative_pairs": neg,
            "TP": tp, "FP": fp, "FN": fn, "TN_diagnostic_only": tn,
            "pairwise_precision": tp/(tp+fp) if tp+fp else None,
            "pairwise_recall": tp/(tp+fn) if tp+fn else None,
            "false_merge_tracks_touched_by_test": sum(len(track_to_gt[t]) > 1 for t in touched_tracks),
            "fragmented_evaluator_instances_touched_by_test": sum(len(gt_to_tracks[g]) > 1 for g in holdout_gt_ids),
            "defer_or_unassigned_count": len(deferred), "defer_rate": len(deferred)/len(scoped) if scoped else None,
            "confirmed_class_observation_count": len(class_confirmed),
            "class_correctness": "unavailable: no confirmed category mapping applies to the test labels; class status is reported without claiming correctness",
            "runtime": runtimes[mode],
        }
    # Posthoc join kept separate from predictor ledgers.
    rows = []
    for mode in MODES:
        p = {r["detection_id"]: r for r in obs_rows_by_mode[mode]}
        for did in sorted(test_ids):
            e = evaluator.get(did, {})
            r = p[did]
            rows.append({"mode": mode, "detection_id": did, "frame_index": frame_no(byid[did]),
                         "predicted_track_id": predictions[mode].get(did),
                         "association_decision": decisions[mode].get(did, {}).get("decision"),
                         "association_reason": decisions[mode].get(did, {}).get("reason"),
                         "raw_class": r["raw_class"], "initial_class": r["initial_class"],
                         "resolved_class": r["resolved_class"], "class_status": r["class_status"],
                         "evaluator_match_status_posthoc": e.get("match_status"),
                         "gt_category_posthoc": e.get("gt_category"),
                         "gt_semantic_id_posthoc": e.get("gt_semantic_id")})
    with (OUT / "temporal_test_observations_posthoc.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    return results, matches_lock


def main() -> int:
    started = time.perf_counter()
    manifest, lock_hashes = verify_locks()
    OUT.mkdir(parents=True, exist_ok=True)
    observations, det_map, test_ids, d3_lock, feature_manifest = load_inputs(manifest)
    predictions, decisions, runtimes, duplicate_ids, duplicate_rows, raw_frames, dedup_frames = replay_association(
        observations, det_map, test_ids)
    obs_rows_by_mode, summaries = run_class_fusion(observations, predictions, duplicate_ids, det_map)
    evals, evaluator_lock = posthoc_score(predictions, decisions, runtimes, duplicate_ids,
                                         observations, test_ids, obs_rows_by_mode)
    report = {
        "schema_version": 1, "status": "locked_temporal_test_scored_not_promoted",
        "split_kind": "same-scene temporal test; not a scene holdout",
        "test_frames_inclusive": [START, END], "stateful_prefix_frames_inclusive": [0, PREFIX_END],
        "frame_279_exclusion": "The immutable split selects exactly the first 40 consecutive frame indices after the previously reviewed horizon ending at 238; those indices are 239..278. Frame 279 is outside that preregistered interval and was not substituted after seeing scores.",
        "raw_saved_p3_observations_replayed": len(observations), "temporal_test_saved_p3_observations": len(test_ids),
        "same_frame_duplicate_masks_suppressed": len(duplicate_rows),
        "all_modes_started_empty_and_replayed_from_frame_0": True,
        "mode_specific_state": True, "ground_truth_used_for_prediction": False,
        "future_observations_used_to_revise_past": False,
        "source_association_code_modified": False, "detector_p3_native_mapping_rerun": False,
        "class_or_room_scores_changed": False, "u3_config_sha256": sha_file(OUT / "association_config.locked.json"),
        "u4_config_sha256": sha_file(U4_LOCK),
        "test_manifest_sha256": sha_file(LOCK_DIR / "temporal_test_manifest.json"),
        "causal_input_index_sha256": sha_file(OUT / "causal_input_index.json"),
        "new_temporal_features": feature_manifest["report"],
        "mode_metrics": evals,
        "mode_class_summary": {mode: {"track_count": len(summaries[mode]),
                            "final_status_counts": {s: sum(x["class_status"] == s for x in summaries[mode].values()) for s in ("confirmed", "tentative", "conflicted")},
                            "final_confirmed_count": sum(x["class_status"] == "confirmed" for x in summaries[mode].values())}
                               for mode in MODES},
        "runtime_total_wall_seconds": time.perf_counter() - started,
        "process_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "evaluator_loaded_after_predictions": True,
        "evaluator_matches_sha256": evaluator_lock["sha256"],
        "test_selection_lock": "artifacts/audits/u4_temporal_test_lock/temporal_test_manifest.json",
    }
    (OUT / "temporal_test_report.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
