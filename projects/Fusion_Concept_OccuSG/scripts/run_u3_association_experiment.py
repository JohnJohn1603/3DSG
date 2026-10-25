#!/usr/bin/env python3
"""Lock and compare U3 object-association diagnostics on saved U2 observations.

Reads only locked detection/crop/feature/evaluator inputs. No detector, P3,
mapping, graph builder, room labeler, or baseline output is invoked or written.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import resource
import sys
import time
from dataclasses import asdict
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from PIL import ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.config.schema import ObjectAssociationConfig  # noqa: E402
from standalone_sg.contracts.objects import Detection3D, DetectionBatch  # noqa: E402
from standalone_sg.objects.association import ObjectAssociationManager  # noqa: E402
from standalone_sg.objects.u3_experimental import (  # noqa: E402
    ExperimentalAssociation, U3Observation, deduplicate_frame_masks,
)

AUDIT = ROOT / "artifacts/audits/dining_table_bed_224_230"
APP = AUDIT / "appearance"
OUT = ROOT / "artifacts/audits/u3_association"


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stable_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def locked_external(key: str) -> Path:
    resources = json.loads((ROOT / "locks/external_resources.json").read_text())
    lock = resources["files"][key]
    path = Path(lock["path"])
    if path.stat().st_size != lock["bytes"] or sha_file(path) != lock["sha256"]:
        raise RuntimeError(f"external input no longer matches lock: {key}")
    return path


def lock_u2_index() -> tuple[list[dict], str]:
    report = json.loads((APP / "extraction_report.json").read_text())
    manifest = [json.loads(line) for line in (APP / "feature_manifest.jsonl").read_text().splitlines() if line]
    active = [r for r in manifest if r["preprocessing_fingerprint"] == report["preprocessing_fingerprint"]]
    active.sort(key=lambda r: r["detection_id"])
    if len(active) != 47 or len({r["detection_id"] for r in active}) != 47:
        raise RuntimeError(f"expected 47 active unique U2 records, got {len(active)}")
    if report["observation_count"] != 47 or report["status"] != "pass":
        raise RuntimeError("U2 extraction report is not passing")
    rows = []
    for r in active:
        for key, path_key in (("crop_sha256", "crop_ref"), ("feature_sha256", "feature_ref")):
            path = ROOT / r[path_key]
            if not path.is_file():
                raise RuntimeError(f"U2 active cache hash mismatch: {r['detection_id']} {path_key}")
            actual_hash = (sha_bytes(np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8).tobytes())
                           if key == "crop_sha256" else sha_file(path))
            if actual_hash != r[key]:
                raise RuntimeError(f"U2 active cache hash mismatch: {r['detection_id']} {path_key}")
        rows.append({
            "record_id": r["observation_id"], "detection_id": r["detection_id"],
            "frame_id": r["frame_id"], "crop_ref": r["crop_ref"],
            "crop_sha256": r["crop_sha256"], "mask_sha256": r["mask_sha256"],
            "crop_file_sha256": sha_file(ROOT / r["crop_ref"]),
            "rgb_sha256": r["rgb_sha256"], "feature_ref": r["feature_ref"],
            "feature_sha256": r["feature_sha256"], "cache_key_sha256": r["cache_key_sha256"],
            "model_fingerprint": r["model_fingerprint"],
            "preprocessing_fingerprint": r["preprocessing_fingerprint"],
        })
    payload = {
        "schema_version": 1, "immutable": True, "record_count": 47,
        "active_u2_preprocessing_fingerprint": report["preprocessing_fingerprint"],
        "model_fingerprint": report["model_fingerprint"],
        "source_manifest_sha256": sha_file(APP / "feature_manifest.jsonl"),
        "records": rows,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    index_path = OUT / "u2_input_index.json"
    if index_path.exists():
        old = json.loads(index_path.read_text())
        if old != payload:
            raise RuntimeError("immutable U2 input index exists and differs; refusing replacement")
    else:
        index_path.write_text(encoded)
    index_hash = sha_file(index_path)
    digest_path = OUT / "u2_input_index.sha256"
    expected_digest = index_hash + "  u2_input_index.json\n"
    if digest_path.exists() and digest_path.read_text() != expected_digest:
        raise RuntimeError("immutable U2 index digest changed")
    if not digest_path.exists():
        digest_path.write_text(expected_digest)
    return active, index_hash


def prepare_observations(active: list[dict]) -> tuple[list[U3Observation], dict[str, dict], dict[str, np.ndarray]]:
    lineage = {r["observation_id"]: r for r in
               (json.loads(line) for line in (AUDIT / "pointsets/observation_pointset_lineage.jsonl").read_text().splitlines())}
    cache_path = locked_external("dense_detection_3d_cache")
    cache_frames = [json.loads(line) for line in cache_path.read_text().splitlines()]
    det3 = {d["detection_id"]: d for f in cache_frames for d in f["detections"]}
    feature_by_id = {}
    rows_by_id = {}
    masks_by_id = {}
    mask_root = locked_external("dense_detection_2d_index").parent
    for r in active:
        did = r["detection_id"]
        if did not in lineage or did not in det3:
            raise RuntimeError(f"U2 ID missing locked U1/P3 geometry: {did}")
        with np.load(ROOT / r["feature_ref"], allow_pickle=False) as data:
            feat = np.asarray(data["image_feature"], dtype=np.float32)
        if feat.shape != (512,) or not np.isfinite(feat).all() or not np.isclose(np.linalg.norm(feat), 1.0, atol=1e-5, rtol=1e-5):
            raise RuntimeError(f"invalid locked U2 vector: {did}")
        feature_by_id[did] = tuple(float(x) for x in feat)
        d = det3[did]
        mask_path = mask_root / d["provider_metadata"]["mask_file"]
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        if sha_file(mask_path) != lineage[did]["mask_sha256"]:
            raise RuntimeError(f"mask hash mismatch: {did}")
        masks_by_id[did] = mask
        rows_by_id[did] = d
    observations = []
    for r in active:
        did = r["detection_id"]
        d, l = det3[did], lineage[did]
        observations.append(U3Observation(
            detection_id=did, frame_id=d["detection_id"].split(":det-")[0],
            timestamp_s=float(d["timestamp_s"]), center=tuple(d["center_W"]),
            extents=tuple(d["extents_m"]) if d.get("extents_m") else None,
            layer_id=d["layer_id"], class_name=d.get("class_name"),
            feature=feature_by_id[did], mask_hash=l["mask_sha256"],
        ))
    return observations, rows_by_id, masks_by_id


def deduplicate_masks(observations: list[U3Observation], masks: dict[str, np.ndarray]) -> tuple[list[U3Observation], list[dict]]:
    return deduplicate_frame_masks(observations, masks, iou_threshold=0.98)


def mode_geometry(observations: list[U3Observation], mode: str) -> ExperimentalAssociation:
    engine = ExperimentalAssociation(mode)  # frozen config below; no GT input
    for frame in sorted({o.frame_id for o in observations}):
        engine.update_frame([o for o in observations if o.frame_id == frame])
    return engine


def mode_source(observations: list[U3Observation], rows: dict[str, dict]) -> tuple[dict[str, str], list[dict]]:
    cfg = ObjectAssociationConfig(same_class_radius_m=0.75, same_pose_suppression_cap_m=0.15)
    manager = ObjectAssociationManager(cfg)
    decisions: dict[str, str] = {}
    ledger = []
    for frame in sorted({o.frame_id for o in observations}):
        batch_obs = sorted((o for o in observations if o.frame_id == frame), key=lambda o: o.detection_id)
        for obs in batch_obs:
            ts = obs.timestamp_s
            # Candidate audit supplements the untouched source strategy; only the source
            # manager's own update result determines its assignments.
            before = manager.snapshot()
            for node in before:
                dist = math.dist(obs.center, node.center_W)
                failures = []
                if obs.layer_id != node.layer_id: failures.append("layer_mismatch")
                if dist > cfg.same_class_radius_m: failures.append("source_radius")
                if obs.class_name != node.class_name and dist > min(cfg.same_class_radius_m, cfg.same_pose_suppression_cap_m):
                    failures.append("source_class_and_suppression_gate")
                ledger.append({"mode": "source", "frame_id": frame, "detection_id": obs.detection_id,
                               "candidate_object_id": node.object_id, "center_distance_m": dist,
                               "same_class": obs.class_name == node.class_name,
                               "gate_failures": failures})
            detection = Detection3D(
                detection_id=obs.detection_id, timestamp_s=obs.timestamp_s, center_W=obs.center,
                center_method=rows[obs.detection_id]["center_method"], class_name=obs.class_name,
                class_id=rows[obs.detection_id].get("class_id"), confidence=rows[obs.detection_id].get("confidence"),
                frame_id="world", layer_id=obs.layer_id, extents_m=obs.extents,
            )
            result = manager.update(DetectionBatch("u3_saved_p3", "world", ts, (detection,)))
            for a in result.associations:
                decisions[a.detection_id] = a.object_id
                ledger.append({"mode": "source", "frame_id": frame, "detection_id": a.detection_id,
                               "decision": a.decision, "assigned_track_id": a.object_id,
                               "distance_m": a.distance_m, "gate_failures": []})
    return decisions, ledger


def prediction_map_source(obs: list[U3Observation], engine: ExperimentalAssociation) -> dict[str, str]:
    out = {}
    for dec in engine.decisions:
        if dec.track_id is not None and dec.decision in ("created", "matched"):
            out[dec.detection_id] = dec.track_id
    return out


def metrics(pred: dict[str, str], obs: list[U3Observation], gt: dict[str, dict], excluded: set[str]) -> dict:
    ids = [o.detection_id for o in obs if o.detection_id not in excluded
           and gt.get(o.detection_id, {}).get("match_status") == "matched"
           and gt[o.detection_id].get("gt_semantic_id") is not None]
    tp = fp = fn = 0
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if left.split(":det-")[0] == right.split(":det-")[0]:
                continue
            same_gt = gt[left]["gt_semantic_id"] == gt[right]["gt_semantic_id"]
            same_pred = pred.get(left) is not None and pred.get(left) == pred.get(right)
            if same_gt and same_pred: tp += 1
            elif not same_gt and same_pred: fp += 1
            elif same_gt and not same_pred: fn += 1
    gt_to_tracks: dict[str, set[str]] = defaultdict(set)
    track_to_gt: dict[str, set[str]] = defaultdict(set)
    for did in ids:
        track, gid = pred.get(did), str(gt[did]["gt_semantic_id"])
        if track is not None:
            gt_to_tracks[gid].add(track)
            track_to_gt[track].add(gid)
    frag_counts = [max(0, len(tracks) - 1) for tracks in gt_to_tracks.values()]
    return {
        "evaluator_matched_observations": len(ids), "pairwise_tp": tp, "pairwise_fp": fp, "pairwise_fn": fn,
        "association_precision": tp / (tp + fp) if tp + fp else None,
        "association_recall": tp / (tp + fn) if tp + fn else None,
        "fragmented_evaluator_instances": sum(x > 0 for x in frag_counts),
        "fragmentation_excess_tracks": sum(frag_counts),
        "evaluator_instances_scored": len(gt_to_tracks),
        "false_merge_predicted_tracks": sum(len(ids_) > 1 for ids_ in track_to_gt.values()),
        "predicted_tracks_scored": len(track_to_gt),
        "identity_scope": "evaluator semantic instance IDs posthoc; not guaranteed physical identity",
    }


def export_review_artifacts(active: list[dict], obs: list[U3Observation], evaluator: dict[str, dict],
                           pred_modes: dict[str, dict[str, str]]) -> dict:
    """Create deterministic posthoc strata/sheets only after prediction maps are fixed."""
    record = {r["detection_id"]: r for r in active}
    observation = {o.detection_id: o for o in obs}
    ids = sorted(set(record) & set(evaluator) & set(observation))
    pair_rows: list[dict] = []
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            gl, gr = evaluator[left], evaluator[right]
            if gl.get("match_status") != "matched" or gr.get("match_status") != "matched":
                continue
            gid_l, gid_r = gl.get("gt_semantic_id"), gr.get("gt_semantic_id")
            if gid_l is None or gid_r is None:
                continue
            cl = observation[left].class_name or ""
            cr = observation[right].class_name or ""
            same_gt = gid_l == gid_r
            same_class = cl.strip().lower() == cr.strip().lower()
            row = {"detection_id_left": left, "detection_id_right": right,
                   "class_left_review_only": cl, "class_right_review_only": cr,
                   "gt_id_left_posthoc": gid_l, "gt_id_right_posthoc": gid_r,
                   "gt_category_left_posthoc": gl.get("gt_category"),
                   "gt_category_right_posthoc": gr.get("gt_category"),
                   "gt_relation_posthoc": "same_evaluator_instance" if same_gt else "different_evaluator_instances",
                   "class_relation_review_only": "same_detector_class" if same_class else "cross_class",
                   "crop_ref_left": record[left]["crop_ref"], "crop_ref_right": record[right]["crop_ref"]}
            for mode, pred in pred_modes.items():
                row[f"same_predicted_track_{mode}"] = bool(pred.get(left) and pred.get(left) == pred.get(right))
                row[f"track_{mode}_left"] = pred.get(left)
                row[f"track_{mode}_right"] = pred.get(right)
            row["likely_false_merge_mode_posthoc"] = ";".join(
                mode for mode, pred in pred_modes.items()
                if pred.get(left) is not None and pred.get(left) == pred.get(right) and not same_gt
            )
            pair_rows.append(row)

    cross_same = [r for r in pair_rows if r["class_relation_review_only"] == "cross_class" and r["gt_relation_posthoc"] == "same_evaluator_instance"]
    same_diff = [r for r in pair_rows if r["class_relation_review_only"] == "same_detector_class" and r["gt_relation_posthoc"] == "different_evaluator_instances"]
    false_merge = [r for r in pair_rows if r["likely_false_merge_mode_posthoc"]]
    strata = {"cross_class_same_evaluator_instance": cross_same,
              "same_class_different_evaluator_instance": same_diff,
              "likely_false_merge_posthoc": false_merge}
    OUT.mkdir(parents=True, exist_ok=True)
    sheets = {}
    for name, rows in strata.items():
        path = OUT / f"contact_{name}.png"
        width, row_h, per_page = 820, 190, 6
        pages = []
        for start in range(0, max(1, len(rows)), per_page):
            batch = rows[start:start + per_page]
            canvas = Image.new("RGB", (width, max(row_h, row_h * len(batch))), "white")
            draw = ImageDraw.Draw(canvas)
            for index, pair in enumerate(batch):
                y = index * row_h
                draw.text((8, y + 4), f"{pair['detection_id_left']} ({pair['class_left_review_only']})  |  {pair['detection_id_right']} ({pair['class_right_review_only']})", fill="black")
                if pair["likely_false_merge_mode_posthoc"]:
                    draw.text((8, y + 22), "Predicted shared track: " + pair["likely_false_merge_mode_posthoc"], fill="darkred")
                for side, key in enumerate(("crop_ref_left", "crop_ref_right")):
                    im = Image.open(ROOT / pair[key]).convert("RGB")
                    im.thumbnail((390, 145))
                    x = side * 410 + 8
                    canvas.paste(im, (x, y + 42))
                    draw.rectangle((x, y + 42, x + 390, y + 187), outline="gray", width=1)
            pages.append(canvas)
        if len(pages) == 1:
            pages[0].save(path)
            page_paths = [str(path.relative_to(ROOT))]
        else:
            page_paths = []
            for i, page in enumerate(pages):
                page_path = OUT / f"contact_{name}_{i + 1:02d}.png"
                page.save(page_path)
                page_paths.append(str(page_path.relative_to(ROOT)))
        sheets[name] = {"pair_count": len(rows), "pages": page_paths}

    pair_path = OUT / "case_review_posthoc.csv"
    fields = sorted({key for row in pair_rows for key in row})
    with pair_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(pair_rows)

    bed_table = []
    for row in pair_rows:
        pair_classes = {row["class_left_review_only"].lower(), row["class_right_review_only"].lower()}
        if pair_classes & {"bed"} and pair_classes & {"dining_table", "dining table", "table"}:
            bed_table.append(row)
    bed_track_members = []
    for case in bed_table:
        for mode, pred in pred_modes.items():
            track = pred.get(case["detection_id_left"])
            if track is None or track != pred.get(case["detection_id_right"]):
                continue
            members = [did for did, tid in pred.items() if tid == track]
            bed_track_members.append({"mode": mode, "pair_left": case["detection_id_left"],
                                     "pair_right": case["detection_id_right"], "track_id": track,
                                     "all_track_member_detection_ids": ";".join(sorted(members)),
                                     "member_classes_review_only": ";".join(sorted({observation[did].class_name or "" for did in members})),
                                     "member_gt_ids_posthoc": ";".join(sorted({str(evaluator[did].get("gt_semantic_id")) for did in members if evaluator.get(did, {}).get("match_status") == "matched"}))})
    bed_path = OUT / "bed_table_case_posthoc.csv"
    bed_fields = ["mode", "pair_left", "pair_right", "track_id", "all_track_member_detection_ids", "member_classes_review_only", "member_gt_ids_posthoc"]
    with bed_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=bed_fields); writer.writeheader(); writer.writerows(bed_track_members)
    return {"strata": {k: len(v) for k, v in strata.items()}, "contact_sheets": sheets,
            "all_posthoc_pairs": len(pair_rows), "bed_table_evaluator_pairs": len(bed_table),
            "bed_table_shared_track_cases": len(bed_track_members),
            "case_review_csv": str(pair_path.relative_to(ROOT)),
            "bed_table_review_csv": str(bed_path.relative_to(ROOT))}


def main() -> int:
    started = time.perf_counter()
    active, index_sha = lock_u2_index()
    obs_all, row_by_id, masks = prepare_observations(active)
    obs, duplicates = deduplicate_masks(obs_all, masks)
    src_pred, source_ledger = mode_source(obs, row_by_id)
    geom = mode_geometry(obs, "geometry")
    geom_app = mode_geometry(obs, "geometry_appearance")
    geom_pred = prediction_map_source(obs, geom)
    app_pred = prediction_map_source(obs, geom_app)

    # Evaluator data is loaded only after all three prediction sets are frozen.
    matches_path = locked_external("dense_object_evaluation_observation_matches")
    protocol_path = locked_external("dense_object_evaluation_protocol")
    evaluator = {r["detection_id"]: r for r in (json.loads(line) for line in matches_path.read_text().splitlines())}
    protocol = json.loads(protocol_path.read_text())
    if protocol.get("protocol_id") != "hm3d_object_gt_v1":
        raise RuntimeError("unsupported evaluator protocol")
    excluded = {r["detection_id"] for r in duplicates}
    evaluations = {"source": metrics(src_pred, obs, evaluator, excluded),
                   "geometry_only": metrics(geom_pred, obs, evaluator, excluded),
                   "geometry_appearance": metrics(app_pred, obs, evaluator, excluded)}
    review = export_review_artifacts(active, obs, evaluator,
                                     {"source": src_pred, "geometry_only": geom_pred,
                                      "geometry_appearance": app_pred})

    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "candidate_ledger.jsonl").open("w") as stream:
        for row in source_ledger + geom.ledger + geom_app.ledger:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    with (OUT / "decisions.jsonl").open("w") as stream:
        for mode, pred, engine in (("source", src_pred, None), ("geometry_only", geom_pred, geom),
                                   ("geometry_appearance", app_pred, geom_app)):
            if engine is None:
                for did, track in sorted(pred.items()):
                    stream.write(json.dumps({"mode": mode, "detection_id": did, "track_id": track,
                                             "decision": "source_manager_assignment"}, sort_keys=True) + "\n")
            else:
                for d in engine.decisions:
                    stream.write(json.dumps({"mode": mode, **asdict(d)}, sort_keys=True, allow_nan=False) + "\n")
        for row in duplicates:
            stream.write(json.dumps({"mode": "all", **row}, sort_keys=True) + "\n")
    with (OUT / "evaluation_observations_posthoc.csv").open("w", newline="") as stream:
        fields = ["detection_id", "record_id", "track_source", "track_geometry", "track_geometry_appearance",
                  "gt_match_status_posthoc", "gt_category_posthoc", "gt_semantic_id_posthoc",
                  "detector_class_review_only", "frame_id"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        active_by_id = {r["detection_id"]: r for r in active}
        for o in obs_all:
            g = evaluator.get(o.detection_id, {})
            writer.writerow({"detection_id": o.detection_id, "record_id": o.detection_id,
                             "track_source": src_pred.get(o.detection_id), "track_geometry": geom_pred.get(o.detection_id),
                             "track_geometry_appearance": app_pred.get(o.detection_id),
                             "gt_match_status_posthoc": g.get("match_status", "not_in_evaluator"),
                             "gt_category_posthoc": g.get("gt_category"),
                             "gt_semantic_id_posthoc": g.get("gt_semantic_id"),
                             "detector_class_review_only": row_by_id[o.detection_id].get("class_name"),
                             "frame_id": o.frame_id})

    # Fixed, label-blind hard-pair and non-bed/table holdout manifest for later
    # validation. It contains IDs/hashes only; it is not promoted into U2 input.
    detections_path = locked_external("dense_detection_3d_cache")
    outside = []
    for frame in (json.loads(line) for line in detections_path.read_text().splitlines()):
        for d in frame["detections"]:
            fid = d["detection_id"].split(":det-")[0]
            if fid[-4:].isdigit() and not 224 <= int(fid[-4:]) <= 230 and int(fid[-4:]) in range(231, 239):
                outside.append({"detection_id": d["detection_id"], "frame_id": fid,
                                "center_sha256": sha_bytes(stable_json(d["center_W"])),
                                "extent_sha256": sha_bytes(stable_json(d.get("extents_m")))})
    holdout = {"schema_version": 1, "status": "reserved_not_scored",
               "selection": "all saved P3 detections in frames 231-238, deterministic detection ID order",
               "count": len(outside), "records": sorted(outside, key=lambda r: r["detection_id"]),
               "reason_not_scored": "U2 appearance cache is intentionally locked to the predeclared 47; no expanded extraction or stateful prefix replay was run."}
    (OUT / "holdout_outside_224_230.json").write_text(json.dumps(holdout, indent=2, sort_keys=True) + "\n")

    cfg = {"source_manager": {"same_class_radius_m": 0.75, "same_pose_suppression_cap_m": 0.15},
           "experimental": {"candidate_center_radius_m": 0.75, "layer_gate": "exact layer equality",
                            "geometry": "3D axis-aligned boxes from saved P3 center/extents",
                            "track_geometry_update": "0.5 EMA of prior track center/extents and matched observation",
                            "track_feature_update": "L2-normalize(previous track feature + current feature), equal per update",
                            "gate_order": ["layer", "center_radius", "3d_iou", "appearance_cosine"],
                            "geometry_only_iou_min": 0.05, "appearance_iou_min": 0.01,
                            "appearance_cosine_min": 0.72, "combined_score_min": 0.55,
                            "score": "0.55*(0.5*IoU+0.3*center_score+0.2*extent_consistency)+0.45*cosine",
                            "assignment": "deterministic greedy one-to-one; exact top-score margin <0.05 defers",
                            "duplicate_mask_iou_min": 0.98}}
    config_path = OUT / "association_config.json"
    config_path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    summary = {
        "schema_version": 1, "status": "diagnostic_complete_not_promoted",
        "input_index_sha256": index_sha, "input_records": 47, "unique_observations_after_dedup": len(obs),
        "duplicate_observations_suppressed": len(duplicates),
        "source_manager_source_modified": False, "source_manager_start_state": "empty local diagnostic",
        "custom_backends_start_state": "empty local diagnostic", "gt_used_for_predictor_or_match_decisions": False,
        "gt_used_posthoc_only": True, "baseline_track_identity_used_as_oracle": False,
        "algorithm_replay": "no YOLO, P3, native mapping, or SceneGraphBuilder; inputs from locked cache",
        "u1_point_set_parity_scope": "fixture only; no exact full-cache parity claim",
        "evaluation": evaluations,
        "defer_counts": {"geometry_only": sum(d.decision == "defer" for d in geom.decisions),
                         "geometry_appearance": sum(d.decision == "defer" for d in geom_app.decisions)},
        "candidate_counts": {"source": len(source_ledger), "geometry_only": len(geom.ledger),
                             "geometry_appearance": len(geom_app.ledger)},
        "posthoc_review": review,
        "runtime_total_u3_diagnostic_s": time.perf_counter() - started,
        "runtime_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        "runtime_memory_note": "Includes feature/mask cache load, association, evaluation export, and contact sheets; no model inference.",
        "thresholds": cfg, "holdout": {"path": str((OUT / "holdout_outside_224_230.json").relative_to(ROOT)),
                                     "count": len(outside), "status": "reserved_not_scored"},
        "outputs": ["u2_input_index.json", "u2_input_index.sha256", "association_config.json",
                    "candidate_ledger.jsonl", "decisions.jsonl", "evaluation_observations_posthoc.csv",
                    "holdout_outside_224_230.json", "case_review_posthoc.csv", "bed_table_case_posthoc.csv",
                    "fixture_matrix.json", "contact sheets listed in posthoc_review.contact_sheets"],
    }
    (OUT / "u3_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
