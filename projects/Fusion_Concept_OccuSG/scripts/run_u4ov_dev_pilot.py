#!/usr/bin/env python3
"""Extract locked U2/OpenCLIP features and score a saved development pilot.

Only cached RGB crops, masks, P3 geometry, and frozen U3 assignments are read.
No detector, P3, semantic evaluator, GT, or room label is used here.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import resource
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import extract_u2_appearance as u2  # noqa: E402
from run_u4_ov_offline import normalize, score  # noqa: E402
from standalone_sg.semantics.u4_class_fusion import quantize_view_bin  # noqa: E402

U4OV_DIR = ROOT / "artifacts/audits/u4_ov"
MODES = ("source_raw", "source_dedup", "geometry_only", "geometry_appearance")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--association-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--u2-feature-dir", type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    association_dir = args.association_dir.resolve()
    feature_dir = args.u2_feature_dir.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    lock_path = U4OV_DIR / "u4_ov_config.locked.json"
    lock = json.loads(lock_path.read_text())
    vocab_path = U4OV_DIR / "vocabulary_v1.json"
    if sha(vocab_path) != lock["vocabulary_sha256"]:
        raise RuntimeError("locked U4-OV vocabulary hash mismatch")
    model_dir = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878"
    model_meta = json.loads((model_dir / "checkpoint_metadata.json").read_text())
    checkpoint = model_dir / model_meta["filename"]
    if sha(checkpoint) != lock["model"]["checkpoint_sha256"]:
        raise RuntimeError("locked OpenCLIP checkpoint hash mismatch")
    vocab = json.loads(vocab_path.read_text())
    labels = [row["label"] for row in vocab["categories"]]
    text_files = list((U4OV_DIR / "text_cache").glob("text_features__*.npz"))
    text_path = next((p for p in text_files if json.loads(p.with_suffix(".json").read_text()).get("vocabulary_sha256") == lock["vocabulary_sha256"]), None)
    if text_path is None:
        raise RuntimeError("locked U4-OV text-feature cache is missing")
    text_meta = json.loads(text_path.with_suffix(".json").read_text())
    if sha(text_path) != text_meta["cache_file_sha256"] or text_meta["checkpoint_sha256"] != lock["model"]["checkpoint_sha256"]:
        raise RuntimeError("text-feature cache hash/provenance mismatch")
    with np.load(text_path, allow_pickle=False) as archive:
        text_labels = archive["labels"].astype(str).tolist()
        text_features = np.asarray(archive["label_features"], dtype=np.float32)
    if text_labels != labels or text_features.shape != (len(labels), 512):
        raise RuntimeError("locked text vectors do not match vocabulary")

    cap = json.loads((run / "capture/capture_manifest.json").read_text())
    p2 = json.loads((run / "detection_2d/detection_2d_index.json").read_text())
    if p2.get("capture_manifest_sha256") != sha(run / "capture/capture_manifest.json"):
        raise RuntimeError("P2 cache capture hash mismatch")
    cap_by_id = {row["frame_id"]: row for row in cap["frames"]}
    p2_by_id = {det["detection_id"]: (frame, det) for frame in p2["frames"] for det in frame["detections"]}
    p3_batches = jsonl(run / "detection_3d/detection_3d.jsonl")
    p3_by_id: dict[str, tuple[dict, dict]] = {}
    for batch in p3_batches:
        for det in batch.get("detections", []):
            p3_by_id[det["detection_id"]] = (batch, det)

    # Extract image embeddings from the already-saved detector boxes. The cache
    # key binds RGB, mask, crop, model checkpoint, and actual preprocessing.
    records = []
    for did, (frame, det) in sorted(p2_by_id.items(), key=lambda x: (int(x[1][0]["frame_index"]), x[0])):
        if did not in p3_by_id:
            continue
        caprec = cap_by_id[frame["frame_id"]]
        rgb_path = run / "capture" / caprec["frame_dir"] / "rgb.npy"
        rgb = np.asarray(np.load(rgb_path, allow_pickle=False))
        mask_path = run / "detection_2d" / det["mask_file"]
        if sha(mask_path) != det["mask_sha256"]:
            raise RuntimeError(f"mask hash mismatch: {did}")
        records.append({"observation_id": did, "frame_id": frame["frame_id"], "detection_id": did,
            "rgb": rgb, "rgb_sha256": sha(rgb_path), "mask_path": mask_path,
            "mask_sha256": det["mask_sha256"], "bbox_xyxy": [float(x) for x in det["bbox_xyxy"]],
            "raw_detector_class": det["class_canonical"], "raw_class_name": det["class_raw"],
            "raw_confidence": float(det["score_raw"]), "frame_index": int(frame["frame_index"])})
    if len(records) != len(p3_by_id):
        raise RuntimeError("U2 crop input and accepted P3 record count differ")
    crop_records = u2.prepare_crop_records(records, output / "crops")
    feature_records = jsonl(feature_dir / "feature_manifest.jsonl")
    feature_by_id = {row["observation_id"]: row for row in feature_records}
    if set(feature_by_id) != {row["observation_id"] for row in crop_records}:
        raise RuntimeError("U2 feature cache does not cover exactly these P3-accepted crops")
    features: dict[str, np.ndarray] = {}
    feature_load_start = time.perf_counter()
    for row in crop_records:
        did = row["observation_id"]
        ref = feature_by_id[did]
        if (ref["rgb_sha256"] != row["rgb_sha256"] or ref["mask_sha256"] != row["mask_sha256"]
                or ref["crop_sha256"] != row["crop_sha256"]
                or ref["checkpoint_sha256"] != lock["model"]["checkpoint_sha256"]):
            raise RuntimeError(f"U2 cache contract differs for {did}")
        feature_path = ROOT / ref["feature_path"]
        if sha(feature_path) != ref["feature_file_sha256"]:
            raise RuntimeError(f"U2 feature hash mismatch: {did}")
        with np.load(feature_path, allow_pickle=False) as archive:
            vector = np.asarray(archive["image_feature"], dtype=np.float32).reshape(-1)
        if vector.shape != (512,) or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5, rtol=1e-5):
            raise RuntimeError(f"invalid normalized U2 feature: {did}")
        features[did] = vector
    feature_load_runtime = time.perf_counter() - feature_load_start
    preprocess_fp = feature_records[0]["preprocessing_fingerprint"]

    # Load all frozen U3 assignments, then compute each crop's zero-shot score.
    assignment_by_mode: dict[str, dict[str, dict]] = {}
    for mode in MODES:
        assignment_by_mode[mode] = {r["detection_id"]: r for r in jsonl(association_dir / f"assignments_{mode}.jsonl")}
    duplicate_rows = json.loads((association_dir / "duplicate_aliases.json").read_text())["duplicates"]
    duplicate_ids = {row["detection_id"] for row in duplicate_rows}
    single: dict[str, dict] = {}
    obs_rows = []
    crop_by_id = {row["observation_id"]: row for row in crop_records}
    for row in crop_records:
        did = row["observation_id"]
        result = score(features[did], labels, text_features, lock)
        batch, d3 = p3_by_id[did]
        pose = d3["provider_metadata"]["camera_pose_W_Copt"]
        camera = (float(pose[0][3]), float(pose[1][3]), float(pose[2][3]))
        view_bin = quantize_view_bin(camera, d3["center_W"], azimuth_bin_deg=30, elevation_bin_deg=30)
        single[did] = result
        obs_rows.append({"observation_id": did, "frame_id": row["frame_id"], "frame_index": row["frame_index"],
            "raw_detector_class": row["raw_detector_class"], "raw_class_name": row["raw_class_name"],
            "raw_detector_confidence": row["raw_confidence"], "p3_center_W": d3["center_W"],
            "p3_extents_m": d3["extents_m"], "view_bin": view_bin, "duplicate_mask_alias": did in duplicate_ids,
            "u4_ov_single_view": result, "feature_path": feature_by_id[did]["feature_path"]})

    by_mode_track: dict[str, dict[str, list[dict]]] = {}
    causal_rows: dict[str, list[dict]] = {mode: [] for mode in MODES}
    track_rows = []
    for mode in MODES:
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in obs_rows:
            assignment = assignment_by_mode[mode].get(row["observation_id"], {})
            track = assignment.get("track_id")
            if track:
                groups[str(track)].append(row)
        by_mode_track[mode] = groups
        for track_id, items in sorted(groups.items()):
            ordered = sorted(items, key=lambda x: (int(x["frame_index"]), x["observation_id"]))
            use_items = [r for r in ordered if mode == "source_raw" or not r["duplicate_mask_alias"]]
            earliest_by_bin: dict[str, dict] = {}
            for r in use_items:
                vb = r["view_bin"]
                if vb != "unknown" and vb not in earliest_by_bin:
                    earliest_by_bin[vb] = r
            representatives = list(earliest_by_bin.values())
            if len(representatives) >= int(lock["classification"]["multi_view"]["minimum_distinct_known_view_bins"]):
                final = score(normalize(np.mean([features[r["observation_id"]] for r in representatives], axis=0)), labels, text_features, lock)
            else:
                final = {"fused_semantic_label": None, "status": "unknown", "top1_candidate": None,
                         "top1_cosine": None, "top2_candidate": None, "top2_cosine": None,
                         "cosine_margin": None, "top5": [], "score_semantics": "fewer than two distinct known view bins"}
            track_rows.append({"mode": mode, "track_id": track_id, "observation_count": len(items),
                "non_duplicate_observation_count": len(use_items), "distinct_view_bin_count": len(representatives),
                "selected_view_bins": [r["view_bin"] for r in representatives],
                "selected_observation_ids": [r["observation_id"] for r in representatives],
                "final_map_classification": final, "output_kind": "predicted association track; unscored; not identity truth"})
            selected_by_bin: dict[str, dict] = {}
            for row in ordered:
                is_dup = row["duplicate_mask_alias"] and mode != "source_raw"
                vb = row["view_bin"]
                if not is_dup and vb != "unknown" and vb not in selected_by_bin:
                    selected_by_bin[vb] = row
                reps_now = list(selected_by_bin.values())
                if len(reps_now) >= int(lock["classification"]["multi_view"]["minimum_distinct_known_view_bins"]):
                    causal = score(normalize(np.mean([features[r["observation_id"]] for r in reps_now], axis=0)), labels, text_features, lock)
                else:
                    causal = {"fused_semantic_label": None, "status": "unknown", "top1_candidate": None,
                              "top1_cosine": None, "top2_candidate": None, "top2_cosine": None,
                              "cosine_margin": None, "top5": [], "score_semantics": "fewer than two distinct known view bins"}
                causal_rows[mode].append({"mode": mode, "observation_id": row["observation_id"], "frame_id": row["frame_id"],
                    "frame_index": row["frame_index"], "track_id": track_id, "causal_evidence_end_frame_index": row["frame_index"],
                    "selected_view_bins_so_far": [r["view_bin"] for r in reps_now],
                    "selected_observation_ids_so_far": [r["observation_id"] for r in reps_now],
                    "causal_classification": causal, "future_observations_used": False})

    pred_by_id = {r["observation_id"]: r for r in obs_rows}
    (output / "single_view_predictions.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for r in obs_rows))
    (output / "causal_predictions_by_mode.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for mode in MODES for r in causal_rows[mode]))
    (output / "final_track_predictions_by_mode.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for r in track_rows))

    # Review gallery: one card per accepted observation, grouped by the locked
    # geometry-only association. Full scene, crop, mask, pose, and all modes are shown.
    review_dir = output / "review"; assets = review_dir / "assets"; assets.mkdir(parents=True, exist_ok=True)
    assignment_rows = {mode: assignment_by_mode[mode] for mode in MODES}
    cap_by_id = {r["frame_id"]: r for r in cap["frames"]}
    html_groups = []
    geom_groups = by_mode_track["geometry_only"]
    for track_id, items in sorted(geom_groups.items()):
        cards = []
        for row in sorted(items, key=lambda x: (x["frame_index"], x["observation_id"])):
            did = row["observation_id"]; batch, d3 = p3_by_id[did]; frame = cap_by_id[row["frame_id"]]
            det = p2_by_id[did][1]
            rgb = np.load(run / "capture" / frame["frame_dir"] / "rgb.npy", allow_pickle=False)[..., :3]
            full = Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")
            mask = np.asarray(np.load(run / "detection_2d" / det["mask_file"], allow_pickle=False), dtype=bool)
            color = Image.new("RGBA", full.size, (255, 55, 40, 100))
            full.alpha_composite(Image.composite(color, Image.new("RGBA", full.size), Image.fromarray((mask * 255).astype(np.uint8), "L")))
            draw = ImageDraw.Draw(full); x0,y0,x1,y1 = det["bbox_xyxy"]; draw.rectangle((x0,y0,x1,y1), outline=(255,230,0,255), width=3)
            stem = hashlib.sha256(did.encode()).hexdigest()[:20]
            scene_name=f"{stem}_scene.png"; full.convert("RGB").save(assets/scene_name,optimize=True)
            crop_name=f"{stem}_crop.png"; Image.open(crop_by_id[did]["crop_path"]).convert("RGB").save(assets/crop_name)
            bx0,by0,bx1,by1=[int(max(0,v)) for v in (x0,y0,x1,y1)]; m_crop=mask[by0:by1+1,bx0:bx1+1]
            mask_name=f"{stem}_mask.png"; Image.fromarray((m_crop*255).astype(np.uint8),"L").save(assets/mask_name)
            mode_lines=[]
            for mode in MODES:
                a=assignment_rows[mode].get(did,{})
                mode_lines.append(f"{mode}: {a.get('track_id','unmatched')} ({a.get('decision','?')})")
            single_result=single[did]
            causal=next((r["causal_classification"] for r in causal_rows["geometry_only"] if r["observation_id"]==did),{})
            final=next((r["final_map_classification"] for r in track_rows if r["mode"]=="geometry_only" and r["track_id"]==track_id),{})
            pose=d3["provider_metadata"]["camera_pose_W_Copt"]
            sensor=(pose[0][3],pose[1][3],pose[2][3])
            cards.append(f"<article><h3>{html.escape(did)} · frame {row['frame_index']} · {html.escape(row['raw_class_name'])} {row['raw_detector_confidence']:.2f}</h3>"
                f"<p>view bin {html.escape(row['view_bin'])}; P3 center {html.escape(str(d3['center_W']))}; extents {html.escape(str(d3['extents_m']))}; sensor pose W {html.escape(str([round(float(v),3) for v in sensor]))}</p>"
                f"<div class='imgs'><figure><figcaption>Full RGB with mask</figcaption><img src='assets/{scene_name}'></figure><figure><figcaption>RGB crop</figcaption><img src='assets/{crop_name}'></figure><figure><figcaption>Mask</figcaption><img src='assets/{mask_name}'></figure></div>"
                f"<p>Single-view U4-OV: {html.escape(str(single_result['fused_semantic_label'] or single_result['top1_candidate']))} · {single_result['status']} · margin {single_result['cosine_margin']:.4f}</p>"
                f"<p>Causal geometry-track: {html.escape(str(causal.get('fused_semantic_label') or causal.get('top1_candidate')))} · {causal.get('status')} · bins {html.escape(str(causal.get('selected_view_bins_so_far',[])))}</p>"
                f"<p>Final geometry-track map: {html.escape(str(final.get('fused_semantic_label') or final.get('top1_candidate')))} · {final.get('status')}</p>"
                f"<pre>{html.escape(chr(10).join(mode_lines))}</pre></article>")
        html_groups.append(f"<section><h2>Geometry-only predicted track {html.escape(track_id)} · {len(items)} observation(s)</h2>{''.join(cards)}</section>")
    page="<!doctype html><meta charset='utf-8'><title>00800 multiview development review</title><style>body{font:15px system-ui;max-width:1400px;margin:2rem auto;padding:0 1rem}article{border:1px solid #bbb;padding:1rem;margin:1rem 0}img{max-width:100%;height:auto}.imgs{display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px}pre{white-space:pre-wrap;background:#f3f3f3;padding:.6rem}</style><h1>HM3D 00800 multiview development pilot</h1><p>Predictions are unscored; review identity and class independently. No review label is fed into prediction. Cosine similarity is not calibrated confidence.</p>"+"".join(html_groups)
    (review_dir / "index.html").write_text(page, encoding="utf-8")

    per_mode = {}
    for mode in MODES:
        tracks = [r for r in track_rows if r["mode"] == mode]
        per_mode[mode] = {"predicted_tracks": len(tracks), "multiview_tracks_ge2_bins": sum(r["distinct_view_bin_count"] >= 2 for r in tracks),
            "single_view_status_counts": dict(Counter(r["u4_ov_single_view"]["status"] for r in obs_rows)),
            "final_track_status_counts": dict(Counter(r["final_map_classification"]["status"] for r in tracks)),
            "causal_status_counts": dict(Counter(r["causal_classification"]["status"] for r in causal_rows[mode]))}
    report = {"status":"predictions_ready_unscored_development","scene_id":cap["scene_id"],"frame_count":len(cap["frames"]),
        "accepted_p3_observations":len(obs_rows),"feature_count":len(features),"candidate_vocabulary_count":len(labels),
        "single_view_status_counts":dict(Counter(x["u4_ov_single_view"]["status"] for x in obs_rows)),
        "multiview_classification_by_association_mode":per_mode,
        "locked_config_sha256":sha(lock_path),"vocabulary_sha256":sha(vocab_path),"text_cache_sha256":sha(text_path),
        "checkpoint_sha256":sha(checkpoint),"preprocessing_fingerprint":preprocess_fp,
        "u3_assignments_modified":False,"raw_detector_class_used_as_candidate_gate":False,
        "ground_truth_or_user_annotations_loaded_for_prediction":False,"room_labels_loaded":False,
        "zero_shot_scope":f"locked {len(labels)}-label candidate vocabulary; not arbitrary open-vocabulary recognition",
        "score_semantics":"cosine similarity, not calibrated probability",
        "u2_feature_manifest_sha256":sha(feature_dir/"feature_manifest.jsonl"),
        "runtime_feature_extraction_seconds":0.0,"runtime_feature_load_seconds":feature_load_runtime,"runtime_total_seconds":time.perf_counter()-started,
        "peak_process_rss_mib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024.0,
        "classification_correctness":"unavailable pending user identity/class review and evaluator mapping",
        "review_html":str((review_dir/"index.html").relative_to(ROOT))}
    (output/"prediction_report.json").write_text(json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k not in ("multiview_classification_by_association_mode",)},indent=2,ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
