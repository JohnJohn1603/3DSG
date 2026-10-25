#!/usr/bin/env python3
"""Offline OpenCLIP image-text class diagnostics on frozen U3 assignments.

This predictor phase reads no annotations, evaluator matches, GT categories,
room labels, or human identity groups. It writes separate raw, U4-voting and
U4-OV fields; it does not mutate graph/runtime state.
"""
from __future__ import annotations
import hashlib, json, resource, sys, time
from collections import defaultdict
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/u4_ov"
U3 = ROOT / "artifacts/audits/u3_causal_prefix"
U4 = ROOT / "artifacts/audits/u4_semantic_fusion"
sys.path.insert(0, str(ROOT / "scripts"))
import extract_u2_appearance as u2  # noqa: E402

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def normalize(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    norm = float(np.linalg.norm(x))
    if not np.isfinite(x).all() or not np.isfinite(norm) or norm <= 0:
        raise RuntimeError("non-finite/zero embedding")
    return x / np.float32(norm)

def verify_lock():
    lock_path = OUT / "u4_ov_config.locked.json"
    lock = json.loads(lock_path.read_text())
    vocab_path = OUT / "vocabulary_v1.json"
    if sha(vocab_path) != lock["vocabulary_sha256"]:
        raise RuntimeError("locked vocabulary changed")
    model_dir = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878"
    metadata = json.loads((model_dir / "checkpoint_metadata.json").read_text())
    checkpoint = model_dir / metadata["filename"]
    if sha(checkpoint) != lock["model"]["checkpoint_sha256"]:
        raise RuntimeError("locked OpenCLIP checkpoint changed")
    return lock, json.loads(vocab_path.read_text()), checkpoint

def load_image_features():
    index_path = U3 / "causal_input_index.json"
    expected = (U3 / "causal_input_index.sha256").read_text().split()[0]
    if sha(index_path) != expected:
        raise RuntimeError("U2 image feature index hash mismatch")
    idx = json.loads(index_path.read_text())
    features = {}; feature_refs = {}
    for row in idx["records"]:
        path = ROOT / row["feature_ref"]
        if sha(path) != row["feature_sha256"]:
            raise RuntimeError(f"U2 image feature changed: {row['detection_id']}")
        with np.load(path, allow_pickle=False) as z:
            raw_vec = np.asarray(z["image_feature"], dtype=np.float32)
            vec = normalize(raw_vec)
        if vec.shape != (512,) or not np.allclose(vec, raw_vec, atol=1e-5, rtol=1e-5):
            raise RuntimeError(f"unexpected U2 image feature contract: {row['detection_id']}")
        features[row["detection_id"]] = vec
        feature_refs[row["detection_id"]] = row
    return idx, features, feature_refs

def build_text_features(lock, vocab, checkpoint):
    import open_clip
    model, _preprocess, torch = u2.load_model(checkpoint)
    tokenizer = open_clip.get_tokenizer(u2.MODEL_NAME)
    templates = vocab["prompt_templates"]
    cats = vocab["categories"]
    prompt_rows = [(cat["label"], template.format(label=cat["description"]))
                   for cat in cats for template in templates]
    prompt_key = hashlib.sha256(json.dumps({
        "checkpoint_sha256": lock["model"]["checkpoint_sha256"],
        "vocabulary_sha256": lock["vocabulary_sha256"],
        "prompt_templates": templates,
        "prompts": prompt_rows,
        "tokenizer": "open_clip.get_tokenizer(ViT-B-32)",
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    cache_dir = OUT / "text_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"text_features__{prompt_key[:20]}.npz"
    manifest_path = cache_dir / f"text_features__{prompt_key[:20]}.json"
    labels = [c["label"] for c in cats]
    if cache_path.exists() and manifest_path.exists():
        meta = json.loads(manifest_path.read_text())
        if meta["cache_key_sha256"] != prompt_key or sha(cache_path) != meta["cache_file_sha256"]:
            raise RuntimeError("cached text embeddings fail hash contract")
        with np.load(cache_path, allow_pickle=False) as z:
            cached_labels = z["labels"].astype(str).tolist()
            label_features = np.asarray(z["label_features"], dtype=np.float32)
            prompt_features = np.asarray(z["prompt_features"], dtype=np.float32)
        if cached_labels != labels:
            raise RuntimeError("cached text labels do not match locked vocabulary")
        return labels, label_features, prompt_features, meta

    text_features = []
    batch = 128
    for start in range(0, len(prompt_rows), batch):
        texts = [r[1] for r in prompt_rows[start:start+batch]]
        tokens = tokenizer(texts)
        with torch.inference_mode():
            vecs = model.encode_text(tokens)
        text_features.extend(normalize(row) for row in vecs.detach().cpu().numpy())
    prompt_features = np.stack(text_features).astype(np.float32)
    per_category = prompt_features.reshape(len(cats), len(templates), 512)
    label_features = np.stack([normalize(group.mean(axis=0)) for group in per_category]).astype(np.float32)
    np.savez_compressed(cache_path, labels=np.asarray(labels, dtype="U"),
                        label_features=label_features, prompt_features=prompt_features)
    meta = {"cache_key_sha256": prompt_key, "cache_file_sha256": sha(cache_path),
            "checkpoint_sha256": lock["model"]["checkpoint_sha256"],
            "vocabulary_sha256": lock["vocabulary_sha256"],
            "prompt_template_sha256": lock["prompt_templates_sha256"],
            "tokenizer": "open_clip.get_tokenizer(ViT-B-32)",
            "prompt_count": len(prompt_rows), "category_count": len(labels),
            "embedding_dimension": 512, "dtype": "float32", "normalized": True,
            "prompt_records": [{"label": label, "prompt": prompt,
                                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
                               for label, prompt in prompt_rows]}
    meta["cache_file_sha256"] = sha(cache_path)
    manifest_path.write_text(json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return labels, label_features, prompt_features, meta

def score(vec: np.ndarray, labels: list[str], label_features: np.ndarray, lock: dict) -> dict:
    # No class-name gate: every vocabulary entry is scored for every image.
    sims = np.asarray(label_features @ normalize(vec), dtype=np.float32)
    order = sorted(range(len(labels)), key=lambda i: (-float(sims[i]), labels[i]))
    best, second = order[:2]
    top_score = float(sims[best]); margin = top_score - float(sims[second])
    if top_score < float(lock["classification"]["top1_unknown_if_max_cosine_below"]):
        status, output = "unknown", None
    elif margin < float(lock["classification"]["top1_conflicted_if_top1_minus_top2_below"]):
        status, output = "conflicted", None
    else:
        status, output = "classified", labels[best]
    return {"fused_semantic_label": output, "status": status,
            "top1_candidate": labels[best], "top1_cosine": top_score,
            "top2_candidate": labels[second], "top2_cosine": float(sims[second]),
            "cosine_margin": margin, "top5": [{"label": labels[i], "cosine": float(sims[i])} for i in order[:5]],
            "score_semantics": "cosine similarity; not a calibrated probability"}

def main():
    t0 = time.perf_counter()
    lock, vocab, checkpoint = verify_lock()
    index, features, refs = load_image_features()
    labels, text_matrix, prompt_matrix, text_meta = build_text_features(lock, vocab, checkpoint)
    u4_rows = {r["detection_id"]: r for r in
               (json.loads(line) for line in (U4 / "observations_geometry_only.jsonl").read_text().splitlines() if line)}
    if set(features) != set(u4_rows):
        raise RuntimeError("U2 image features and frozen U3/U4 observation sets differ")
    # Import just the deterministic view quantizer from the frozen U4 module.
    sys.path.insert(0, str(ROOT / "src"))
    from standalone_sg.semantics.u4_class_fusion import quantize_view_bin
    d3_path = json.loads((ROOT / "locks/external_resources.json").read_text())["files"]["dense_detection_3d_cache"]["path"]
    d3_lock = json.loads((ROOT / "locks/external_resources.json").read_text())["files"]["dense_detection_3d_cache"]
    if sha(Path(d3_path)) != d3_lock["sha256"]:
        raise RuntimeError("locked P3 cache changed")
    d3_by_id = {}
    for line in Path(d3_path).read_text().splitlines():
        if not line: continue
        frame = json.loads(line)
        if int(frame["frame_id"].rsplit("-", 1)[-1]) <= 238:
            for d in frame["detections"]: d3_by_id[d["detection_id"]] = d

    observations = []
    for did in sorted(features, key=lambda x: (int(x.split(":",1)[0].rsplit("-",1)[-1]), x)):
        row = u4_rows[did]; d3 = d3_by_id[did]
        pose = d3.get("provider_metadata", {}).get("camera_pose_W_Copt")
        if isinstance(pose, list) and len(pose) >= 3:
            camera = (float(pose[0][3]), float(pose[1][3]), float(pose[2][3]))
            view_bin = quantize_view_bin(camera, d3["center_W"], azimuth_bin_deg=30, elevation_bin_deg=30)
        else: view_bin = "unknown"
        one = score(features[did], labels, text_matrix, lock)
        observations.append({
            "observation_id": did, "frame_id": did.split(":",1)[0],
            "frame_index": row["frame_index"], "detection_id": did.split(":",1)[1],
            "raw_detector_class": row.get("raw_class"), "raw_detector_confidence": row.get("raw_confidence"),
            "u3_geometry_only_track_id": row.get("track_id"),
            "u4_voting": {"fused_semantic_label": row.get("resolved_class") if row.get("class_status") == "confirmed" else None,
                          "status": row.get("class_status"), "initial_class": row.get("initial_class")},
            "u4_ov_single_view": one,
            "image_feature_sha256": refs[did]["feature_sha256"], "image_feature_ref": refs[did]["feature_ref"],
            "view_bin": view_bin,
            "duplicate_alias": bool(row.get("duplicate_alias")),
            "vocabulary_sha256": lock["vocabulary_sha256"],
            "text_cache_key_sha256": text_meta["cache_key_sha256"]})

    # Unsupervised multi-view track outputs: only predicted U3 assignments are used.
    by_track = defaultdict(list)
    for row in observations:
        if row["u3_geometry_only_track_id"] and not row["duplicate_alias"]:
            by_track[row["u3_geometry_only_track_id"]].append(row)
    track_rows = []
    for tid, items in sorted(by_track.items()):
        known_bins = {}
        for row in sorted(items, key=lambda r: (r["frame_index"], r["observation_id"])):
            if row["view_bin"] != "unknown" and row["view_bin"] not in known_bins:
                known_bins[row["view_bin"]] = row
        reps = list(known_bins.values())
        if len(reps) >= int(lock["classification"]["multi_view"]["minimum_distinct_known_view_bins"]):
            agg = normalize(np.mean([features[r["observation_id"]] for r in reps], axis=0))
            result = score(agg, labels, text_matrix, lock)
            per_view = [score(features[r["observation_id"]], labels, text_matrix, lock) for r in reps]
            if result["fused_semantic_label"]:
                result["supporting_view_bins"] = sum(x["top1_candidate"] == result["fused_semantic_label"] for x in per_view)
            else: result["supporting_view_bins"] = None
        else:
            result = {"fused_semantic_label": None, "status": "unknown",
                      "top1_candidate": None, "top1_cosine": None,
                      "top2_candidate": None, "top2_cosine": None,
                      "cosine_margin": None, "top5": [],
                      "supporting_view_bins": None,
                      "score_semantics": "not classified: fewer than two known view bins"}
        track_rows.append({"u3_geometry_only_track_id": tid,
                           "fused_semantic_label": result["fused_semantic_label"],
                           "status": result["status"], "top1_candidate": result["top1_candidate"],
                           "top1_cosine": result["top1_cosine"], "top2_candidate": result["top2_candidate"],
                           "top2_cosine": result["top2_cosine"], "cosine_margin": result["cosine_margin"],
                           "supporting_view_bins": result["supporting_view_bins"],
                           "distinct_known_view_bins": len(reps), "observation_count": len(items),
                           "selected_view_observation_ids": [r["observation_id"] for r in reps],
                           "top5": result["top5"],
                           "output_kind": "predicted_U3_track_diagnostic; not end-to-end identity"})

    (OUT / "observations_predictions.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False)+"\n" for r in observations))
    (OUT / "predicted_track_labels.jsonl").write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False, allow_nan=False)+"\n" for r in track_rows))
    report = {"schema_version": 1, "status": "u4_ov_offline_predictions_ready_unscored",
              "vocabulary_sha256": lock["vocabulary_sha256"], "locked_config_sha256": sha(OUT / "u4_ov_config.locked.json"),
              "checkpoint_sha256": lock["model"]["checkpoint_sha256"],
              "text_cache_key_sha256": text_meta["cache_key_sha256"], "text_cache_sha256": text_meta["cache_file_sha256"],
              "candidate_count": len(labels), "observations_scored": len(observations),
              "predicted_u3_tracks_scored": len(track_rows),
              "observation_status_counts": {k: sum(r["u4_ov_single_view"]["status"] == k for r in observations) for k in ("classified","unknown","conflicted")},
              "track_status_counts": {k: sum(r["status"] == k for r in track_rows) for k in ("classified","unknown","conflicted")},
              "raw_detector_class_used_as_gate": False,
              "ground_truth_or_user_annotations_loaded_for_prediction": False,
              "room_label_used": False,
              "u3_assignments_modified": False,
              "score_is_calibrated_probability": False,
              "zero_shot_scope": f"selects among the locked {len(labels)}-label candidate vocabulary; not arbitrary open-vocabulary recognition",
              "runtime_seconds_including_text_cache_load_or_encode": time.perf_counter()-t0,
              "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
              "outputs": ["observations_predictions.jsonl","predicted_track_labels.jsonl","text_cache/"]}
    (OUT / "u4_ov_prediction_report.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)+"\n")
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))

if __name__ == "__main__": main()
