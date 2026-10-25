#!/usr/bin/env python3
"""Offline U5 plumbing over verified, already-cached U2/U3/U4-OV artifacts.

This command never reruns capture, P2, P3, or the predictor. It writes an
additional provenance sidecar and leaves graph snapshots/deltas untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import shutil
import webbrowser

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "artifacts/runs/hm3d_00800_u4ov_multiview_pilot_v2_640"
U4_DIR = ROOT / "artifacts/audits/u4_ov"
MODES = {
    "source": ("source_raw", "Source association (default)"),
    "geometry-only": ("geometry_only", "Geometry-only (opt-in diagnostic)"),
    "appearance-ablation": ("geometry_appearance", "Geometry + appearance (ablation only)"),
}


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def verify_inputs(run: Path) -> dict:
    required = [
        run / "capture/capture_manifest.json",
        run / "detection_2d/detection_2d_index.json",
        run / "detection_3d/detection_3d.jsonl",
        run / "association/assignments_source_raw.jsonl",
        run / "association/assignments_geometry_only.jsonl",
        run / "association/assignments_geometry_appearance.jsonl",
        run / "association/report.json",
        run / "u2_features/feature_manifest.jsonl",
        run / "u4ov/single_view_predictions.jsonl",
        run / "u4ov/causal_predictions_by_mode.jsonl",
        run / "u4ov/final_track_predictions_by_mode.jsonl",
        U4_DIR / "u4_ov_config.locked.json",
        U4_DIR / "vocabulary_v1.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("required frozen cache artifact(s) missing: " + ", ".join(missing))
    cap_path = required[0]
    cap_sha = sha(cap_path)
    p2 = json.loads(required[1].read_text(encoding="utf-8"))
    if p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != cap_sha:
        raise ValueError("P2 cache is not pass or capture hash does not match")
    p3_manifest_path = run / "detection_3d/detection_3d_manifest.json"
    p3_manifest = json.loads(p3_manifest_path.read_text(encoding="utf-8"))
    p3_path = run / "detection_3d/detection_3d.jsonl"
    p3_hash = sha(p3_path)
    if p3_manifest.get("detection_2d_index_sha256") and p3_manifest["detection_2d_index_sha256"] != sha(required[1]):
        raise ValueError("P3 cache does not match P2 index")
    if p3_manifest.get("capture_manifest_sha256") != cap_sha:
        raise ValueError("P3 cache does not match capture manifest")
    lock_path, vocab_path = required[-2:]
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
    if sha(vocab_path) != lock.get("vocabulary_sha256"):
        raise ValueError("U4-OV vocabulary hash does not match locked config")
    feature_path = run / "u2_features/feature_manifest.jsonl"
    u4_report = json.loads((run / "u4ov/prediction_report.json").read_text(encoding="utf-8"))
    u3_report = json.loads((run / "association/report.json").read_text(encoding="utf-8"))
    if u3_report.get("p3_sha256") != p3_hash or u3_report.get("p2_index_sha256") != sha(required[1]):
        raise ValueError("U3 assignments do not match cached P2/P3 inputs")
    if sha(feature_path) != u4_report.get("u2_feature_manifest_sha256"):
        raise ValueError("U2 feature manifest differs from the one used by U4-OV")
    if sha(lock_path) != u4_report.get("locked_config_sha256"):
        raise ValueError("U4-OV locked config differs from the prediction report")
    if u4_report.get("ground_truth_or_user_annotations_loaded_for_prediction") is not False or u4_report.get("room_labels_loaded") is not False:
        raise ValueError("U4-OV provenance does not certify predictor isolation from GT/room labels")

    p3_ids = {str(det["detection_id"]) for batch in read_jsonl(p3_path) for det in batch.get("detections", [])}
    feature_rows = read_jsonl(feature_path)
    if {str(row["observation_id"]) for row in feature_rows} != p3_ids:
        raise ValueError("U2 feature rows do not exactly cover cached P3 observations")
    single = read_jsonl(run / "u4ov/single_view_predictions.jsonl")
    if {str(row["observation_id"]) for row in single} != p3_ids:
        raise ValueError("U4-OV observation predictions do not exactly cover cached P3 observations")
    feature_rows = read_jsonl(feature_path)
    checkpoint_dir = ROOT / "artifacts/models/openclip/vit-b-32-openai-a6f597a30f7b82c51704746581f9a4e41421e878"
    checkpoint_meta = json.loads((checkpoint_dir / "checkpoint_metadata.json").read_text(encoding="utf-8"))
    checkpoint = checkpoint_dir / checkpoint_meta["filename"]
    if not checkpoint.is_file() or sha(checkpoint) != lock["model"]["checkpoint_sha256"]:
        raise ValueError("U4-OV checkpoint is missing or differs from locked SHA-256")
    text_cache = next((path for path in (U4_DIR / "text_cache").glob("text_features__*.npz")
                       if json.loads(path.with_suffix(".json").read_text(encoding="utf-8")).get("vocabulary_sha256") == lock["vocabulary_sha256"]), None)
    if text_cache is None or sha(text_cache) != u4_report.get("text_cache_sha256"):
        raise ValueError("locked U4-OV text feature cache is missing or hash differs")
    for row in feature_rows:
        crop = ROOT / row["crop_path"]
        feature = ROOT / row["feature_path"]
        if not crop.is_file():
            raise ValueError(f"U2 cached crop is missing or changed: {row['observation_id']}")
        with Image.open(crop) as image:
            crop_pixels_sha = hashlib.sha256(np.asarray(image.convert("RGB"), dtype=np.uint8).tobytes()).hexdigest()
        if crop_pixels_sha != row["crop_sha256"]:
            raise ValueError(f"U2 cached crop pixels differ from manifest: {row['observation_id']}")
        if not feature.is_file() or sha(feature) != row["feature_file_sha256"]:
            raise ValueError(f"U2 feature cache is missing or changed: {row['observation_id']}")
    return {
        "capture_manifest_sha256": cap_sha, "p2_index_sha256": sha(required[1]),
        "p3_cache_sha256": p3_hash, "u2_feature_manifest_sha256": sha(feature_path),
        "u3_report_sha256": sha(run / "association/report.json"),
        "u3_config_sha256": u3_report.get("u3_config_sha256"),
        "u4ov_config_sha256": sha(lock_path), "u4ov_vocabulary_sha256": sha(vocab_path),
        "u4ov_text_cache_sha256": sha(text_cache),
        "observation_count": len(p3_ids), "vocabulary_count": len(vocab["categories"]),
        "u4ov_checkpoint_sha256": lock["model"]["checkpoint_sha256"],
        "u4ov_vocabulary_id": lock["vocabulary_id"],
    }


def build_sidecar(run: Path, output: Path, profile: str, input_hashes: dict) -> dict:
    mode, mode_label = MODES[profile]
    assignments = read_jsonl(run / f"association/assignments_{mode}.jsonl")
    causal_rows = read_jsonl(run / "u4ov/causal_predictions_by_mode.jsonl")
    final_rows = read_jsonl(run / "u4ov/final_track_predictions_by_mode.jsonl")
    single_rows = read_jsonl(run / "u4ov/single_view_predictions.jsonl")
    selected_assignments = {str(row["detection_id"]): row for row in assignments if row.get("mode") == mode}
    causal = {str(row["observation_id"]): row for row in causal_rows if row.get("mode") == mode}
    singles = {str(row["observation_id"]): row for row in single_rows}
    final_by_track = {str(row["track_id"]): row for row in final_rows if row.get("mode") == mode}
    expected = set(singles)
    if set(selected_assignments) != expected or set(causal) != expected:
        raise ValueError(f"{mode} assignment/causal observation coverage differs from U4-OV single-view set")

    feature_rows = {str(row["observation_id"]): row for row in read_jsonl(run / "u2_features/feature_manifest.jsonl")}
    assets = output / "assets"
    assets.mkdir()
    observations = []
    for oid in sorted(expected, key=lambda key: (int(causal[key]["frame_index"]), key)):
        pred = causal[oid]
        if pred.get("future_observations_used") is not False:
            raise ValueError(f"causal prediction lacks future-free guarantee: {oid}")
        frame_index = int(pred["frame_index"])
        if int(pred["causal_evidence_end_frame_index"]) > frame_index:
            raise ValueError(f"future evidence timestamp in causal output: {oid}")
        evidence_ids = [str(item) for item in pred.get("selected_observation_ids_so_far", [])]
        for evidence_id in evidence_ids:
            evidence_row = causal.get(evidence_id)
            if evidence_row is None or int(evidence_row["frame_index"]) > frame_index or evidence_row.get("track_id") != pred.get("track_id"):
                raise ValueError(f"causal evidence is future or belongs to another track: {oid} -> {evidence_id}")
        raw = singles[oid]
        crop_ref = feature_rows[oid]
        crop_value = crop_ref.get("crop_path") or crop_ref.get("crop_file") or crop_ref.get("crop")
        crop_source = (ROOT / crop_value).resolve() if crop_value else None
        crop_rel = None
        if crop_source is not None and crop_source.is_file():
            crop_name = hashlib.sha256(oid.encode()).hexdigest()[:16] + crop_source.suffix
            shutil.copy2(crop_source, assets / crop_name)
            crop_rel = f"assets/{crop_name}"
        decision = selected_assignments[oid]
        classification = pred["causal_classification"]
        status = str(classification.get("status", "unknown"))
        label = classification.get("fused_semantic_label") if status == "classified" else None
        observations.append({
            "observation_id": oid, "frame_id": pred["frame_id"], "frame_index": frame_index,
            "raw_detector_class": raw.get("raw_detector_class"),
            "raw_detector_class_name": raw.get("raw_class_name"),
            "raw_detector_confidence": raw.get("raw_detector_confidence"),
            "association_profile": mode, "association_track_id": decision.get("track_id"),
            "association_decision": decision.get("decision"),
            "fused_semantic_label": label, "fused_semantic_status": status,
            "label_source": "u4_ov_image_text_multiview" if label is not None else None,
            "evidence_ids": evidence_ids,
            "evidence_end_frame_index": int(pred["causal_evidence_end_frame_index"]),
            "view_bins": pred.get("selected_view_bins_so_far", []),
            "diagnostics": {key: classification.get(key) for key in ("top1_candidate", "top1_cosine", "top2_candidate", "top2_cosine", "cosine_margin", "score_semantics")},
            "config_version": "u4_ov_locked_v1",
            "config_sha256": input_hashes["u4ov_config_sha256"],
            "model_revision": "a6f597a30f7b82c51704746581f9a4e41421e878",
            "model_checkpoint_sha256": input_hashes["u4ov_checkpoint_sha256"],
            "crop_preview": crop_rel,
            "future_observations_used": False,
        })

    if len(observations) != input_hashes["observation_count"]:
        raise ValueError("sidecar observation count differs from verified P3 cache")
    status_counts = {key: sum(row["status"] == key for row in (item["final_map_classification"] for item in final_by_track.values()))
                     for key in ("classified", "conflicted", "unknown")}
    track_count = len(final_by_track)
    classified_tracks = status_counts["classified"]
    vocabulary = json.loads((U4_DIR / "vocabulary_v1.json").read_text(encoding="utf-8"))
    vocab_labels = {str(row["label"]).casefold() for row in vocabulary["categories"]}
    raw_classes = {str(row["raw_detector_class"]).casefold() for row in observations if row["raw_detector_class"] is not None}
    unsupported_raw = sorted(raw_classes - vocab_labels)
    payload = {
        "schema_version": 1,
        "kind": "u5_experimental_semantic_sidecar",
        "status": "experimental_plumbing_unscored",
        "profile": profile,
        "profile_label": mode_label,
        "association_mode": mode,
        "association_applied_to_graph": False,
        "graph_object_association": "source default remains active; this profile reports frozen cache assignments only",
        "semantic_sidecar": "U4-OV causal image-text multiview predictions; unknown/conflicted labels stay null",
        "raw_detector_class_immutable": True,
        "raw_fallback_used": False,
        "evaluator_annotations_loaded": False,
        "room_labels_loaded": False,
        "config_model_provenance": {
            "u4ov_config_sha256": input_hashes["u4ov_config_sha256"],
            "vocabulary_sha256": input_hashes["u4ov_vocabulary_sha256"],
            "checkpoint_sha256": input_hashes["u4ov_checkpoint_sha256"],
            "model_revision": "a6f597a30f7b82c51704746581f9a4e41421e878",
            "vocabulary_id": input_hashes["u4ov_vocabulary_id"],
        },
        "input_provenance": input_hashes,
        "track_status_counts": status_counts,
        "track_count": track_count,
        "label_coverage": {"classified_tracks": classified_tracks, "total_tracks": track_count,
                           "fraction": classified_tracks / track_count if track_count else 0.0},
        "unsupported_raw_detector_labels_exact_vocab": unsupported_raw,
        "unsupported_raw_detector_label_count": len(unsupported_raw),
        "observation_count": len(observations),
        "observations": observations,
        "final_tracks": [
            {"track_id": tid, "observation_count": row["observation_count"],
             "selected_evidence_ids": row.get("selected_observation_ids", []),
             "selected_view_bins": row.get("selected_view_bins", []),
             "fused_semantic_label": row["final_map_classification"].get("fused_semantic_label") if row["final_map_classification"].get("status") == "classified" else None,
             "status": row["final_map_classification"].get("status", "unknown"),
             "diagnostics": row["final_map_classification"]}
            for tid, row in sorted(final_by_track.items())
        ],
        "quality_warning": "Experimental plumbing only; these development results do not show improvement over baseline.",
    }
    write_json(output / "u5_sidecar.json", payload)
    (output / "u5_observations.jsonl").write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for row in observations), encoding="utf-8")
    write_json(output / "u5_report.json", {key: value for key, value in payload.items() if key not in ("observations", "final_tracks")})
    render_html(output / "index.html", payload)
    return payload


def render_html(path: Path, payload: dict) -> None:
    rows = []
    for row in payload["observations"]:
        preview = f'<img loading="lazy" width="128" src="{html.escape(row["crop_preview"])}">' if row.get("crop_preview") else ""
        raw = html.escape(str(row.get("raw_detector_class") or "(none)"))
        fused = html.escape(str(row.get("fused_semantic_label") or "(none; no raw fallback)"))
        evidence = html.escape(", ".join(row["evidence_ids"]))
        rows.append(f'<tr><td>{preview}</td><td>{html.escape(row["observation_id"])}</td><td>{row["frame_index"]}</td><td>{raw}</td><td>{html.escape(row["fused_semantic_status"])}</td><td>{fused}</td><td>{html.escape(str(row.get("association_track_id")))}</td><td>{evidence}</td></tr>')
    status = payload["track_status_counts"]
    body = f'''<!doctype html><meta charset="utf-8"><title>U5 experimental sidecar</title>
<style>body{{font:14px system-ui;margin:2rem;color:#202124}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#f5f5f5}}.warn{{padding:1rem;background:#fff4ce}}code{{overflow-wrap:anywhere}}</style>
<h1>U5 experimental sidecar</h1><p class="warn">Experimental plumbing only; not evidence of quality improvement. Graph association remains source default; profile: {html.escape(payload['profile_label'])}.</p>
<p>Tracks: {payload['track_count']} · confirmed/classified: {status['classified']} · conflicted: {status['conflicted']} · unknown: {status['unknown']} · label coverage: {payload['label_coverage']['fraction']:.1%}. Raw fallback: no.</p>
<table><thead><tr><th>Crop</th><th>Observation ID</th><th>Frame</th><th>Raw detector class</th><th>Fused status</th><th>Fused label</th><th>Diagnostic assignment</th><th>Causal evidence IDs</th></tr></thead><tbody>{''.join(rows)}</tbody></table>'''
    path.write_text(body, encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = root / f"u5-{stamp}-{args.profile}"
    suffix = 1
    while output.exists():
        output = root / f"u5-{stamp}-{args.profile}-{suffix:02d}"
        suffix += 1
    hashes = verify_inputs(run_dir)
    output.mkdir(parents=False, exist_ok=False)
    if args.extension == "off":
        baseline = {"schema_version": 1, "kind": "u5_extension_disabled", "baseline_profile": "source association + current semantics",
                    "extension_enabled": False, "input_provenance": hashes,
                    "baseline_artifacts_rewritten": False, "output": "U5 extension emitted no diagnostics; existing baseline artifacts remain untouched"}
        write_json(output / "baseline_extension_off.json", baseline)
        payload = baseline
        print(f"U5 extension=off | baseline artifacts untouched | verified observations={hashes['observation_count']} | output={output}")
    else:
        payload = build_sidecar(run_dir, output, args.profile, hashes)
        counts = payload["track_status_counts"]
        coverage = payload["label_coverage"]
        print(f"U5 EXPERIMENTAL | association profile={payload['profile']} (graph remains source) | tracks={payload['track_count']} confirmed={counts['classified']} conflicted={counts['conflicted']} unknown={counts['unknown']} | label coverage={coverage['classified_tracks']}/{coverage['total_tracks']} | unsupported raw labels={payload['unsupported_raw_detector_label_count']} | raw fallback=no")
        print("Warning: development plumbing only; quality improvement over baseline is not established.")
        label_rows = [row for row in payload["observations"] if row["fused_semantic_label"] is not None]
        if label_rows:
            for row in label_rows:
                evidence = ",".join(row["evidence_ids"]) or "none"
                print(f"LABEL {row['observation_id']} | raw={row['raw_detector_class']} | fused={row['fused_semantic_label']} ({row['fused_semantic_status']}) | evidence={evidence}")
        else:
            print("LABEL raw detector labels retained; fused labels abstained for all observations (see sidecar evidence table).")
        print(f"Raw/fused comparison and evidence: {output / 'u5_observations.jsonl'}")
        print(f"Review: {output / 'index.html'}")
    write_json(output / "run_manifest.json", {"status": "complete", "extension": args.extension,
        "profile": args.profile, "input_run": str(run_dir), "output_dir": str(output),
        "generated_utc": datetime.now(timezone.utc).isoformat(), "input_provenance": hashes,
        "capture_p2_p3_rerun": False})
    print(f"Open: python scripts/u5_experimental.py open --run-dir {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    run_parser = subs.add_parser("run", help="run cached baseline contract or experimental sidecar")
    run_parser.add_argument("--extension", choices=("off", "experimental"), default="experimental")
    run_parser.add_argument("--profile", choices=tuple(MODES), default="source")
    run_parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    run_parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts/runs/u5_experimental")
    run_parser.set_defaults(func=run)
    open_parser = subs.add_parser("open", help="open an existing U5 result")
    open_parser.add_argument("--run-dir", type=Path, required=True)
    open_parser.set_defaults(func=lambda args: 0 if webbrowser.open((args.run_dir.resolve() / "index.html").as_uri()) else 1)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
