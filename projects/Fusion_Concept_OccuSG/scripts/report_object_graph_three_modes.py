#!/usr/bin/env python3
"""Create K0/K1 provenance and three-mode evaluator report from locked cache replays."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.compare_association_profiles import assignment_map, read_json, read_jsonl, sha  # noqa: E402
from standalone_sg.objects.profile_comparison import pairwise_identity_metrics, relation_changes  # noqa: E402


def run(input_root: Path, source_root: Path, geometry_root: Path, dedup_root: Path,
        output: Path, gate_ids: list[str]) -> dict:
    if output.exists():
        raise FileExistsError(output)
    input_root, source_root, geometry_root, dedup_root = [p.resolve() for p in (input_root, source_root, geometry_root, dedup_root)]
    input_hashes = {"capture_manifest": sha(input_root / "capture/capture_manifest.json"),
                    "p2_index": sha(input_root / "detection_2d/detection_2d_index.json"),
                    "p3_cache": sha(input_root / "detection_3d/detection_3d.jsonl"),
                    "p3_manifest": sha(input_root / "detection_3d/detection_3d_manifest.json")}
    roots = {"source": source_root, "geometry-only": geometry_root, "geometry-only-dedup-v2": dedup_root}
    for name, path in roots.items():
        launcher = read_json(path / "launcher_manifest.json")
        if launcher.get("source_cache_run") != str(input_root):
            raise ValueError(f"{name} replay is not bound to exact input cache")
        if launcher.get("input_hashes") != input_hashes:
            raise ValueError(f"{name} replay input hashes differ from requested cache")
        if name == "source" and launcher.get("association_profile") != "source":
            raise ValueError("source replay profile mismatch")
        if name == "geometry-only" and launcher.get("association_profile") != "geometry-only":
            raise ValueError("geometry-only replay profile mismatch")
        if name == "geometry-only-dedup-v2" and launcher.get("association_profile") != "geometry-only-dedup-v2":
            raise ValueError("dedup-v2 replay profile mismatch")

    p3 = read_jsonl(input_root / "detection_3d/detection_3d.jsonl")
    raw_rows = [det for frame in p3 for det in frame.get("detections", [])]
    raw_ids = {row["detection_id"] for row in raw_rows}
    evaluations = sorted((input_root / "object_evaluations").glob("eval_*/observation_matches.jsonl"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    if not evaluations:
        raise FileNotFoundError("official one-to-one object-evaluation ledger missing")
    eval_path = evaluations[0]
    eval_rows = read_jsonl(eval_path)
    eval_map = {r["detection_id"]: r for r in eval_rows if r.get("detection_id") in raw_ids}
    gt = {oid: (str(r["gt_semantic_id"]) if r.get("match_status") == "matched" and r.get("gt_semantic_id") is not None else None)
          for oid, r in eval_map.items()}
    eligible = {oid for oid in raw_ids if gt.get(oid) is not None}
    assignment_data = {}
    metrics = {}
    replay_manifests = {}
    for name, root in roots.items():
        replay = root / "replay"
        assignments, rows = assignment_map(replay, raw_ids)
        if set(assignments) != raw_ids:
            raise ValueError(f"{name} assignment denominator differs from P3 raw observations")
        assignment_data[name] = (assignments, rows)
        metrics[name] = pairwise_identity_metrics(assignments, gt, eligible_ids=eligible)
        replay_manifests[name] = read_json(replay / "manifest.json")
        metrics[name]["tracks_final"] = sum(n.get("kind") == "object" for n in read_json(replay / "snapshot.json")["snapshot"].get("nodes", []))
        metrics[name]["runtime_seconds"] = replay_manifests[name].get("runtime_ms", 0) / 1000
        metrics[name]["peak_process_rss_mib"] = replay_manifests[name].get("python_process_ru_maxrss_kib", 0) / 1024
        summary = replay_manifests[name].get("same_frame_duplicate_summary") or {}
        metrics[name]["aliases"] = sum(row.get("decision") == "duplicate_suppressed" for row in rows.values())
        diagnostics = read_json(replay / "frame_diagnostics.json")
        ambiguous_ids = {str(did) for frame in diagnostics
                         for did in frame.get("object_backend", {}).get("ambiguous_detection_ids", [])}
        metrics[name]["ambiguous"] = max(int(summary.get("ambiguous_observations", 0)), len(ambiguous_ids))
        metrics[name]["deferred"] = sum(str(row.get("decision", "")).startswith("defer") for row in rows.values())
        metrics[name]["unassigned_raw_observations"] = sum(value is None for value in assignments.values())

    relation_changes_by_step = {}
    for left, right in (("source", "geometry-only"), ("geometry-only", "geometry-only-dedup-v2"),
                        ("source", "geometry-only-dedup-v2")):
        changes = relation_changes(assignment_data[left][0], assignment_data[right][0], sorted(raw_ids))
        categorized = {"total": len(changes), "evaluator_verified_changes": 0,
                       "verified_corrections": 0, "verified_regressions": 0,
                       "same_frame_changes": 0, "unverified_missing_identity": 0,
                       "by_evaluator_identity": {}}
        for item in changes:
            a, b = item["observation_ids"]
            aid, bid = gt.get(a), gt.get(b)
            if a.split(":det-")[0] == b.split(":det-")[0]:
                item["status"] = "same_frame_diagnostic_only"
                categorized["same_frame_changes"] += 1
            elif aid is not None and bid is not None:
                first_same = aid == bid
                item["status"] = "evaluator_confirmed_relation_change"
                item["evaluator_ids"] = [aid, bid]
                item["relation_was_correct"] = (item["source_relation"] == "same_track") == first_same
                item["relation_now_correct"] = (item["candidate_relation"] == "same_track") == first_same
                categorized["evaluator_verified_changes"] += 1
                if item["relation_now_correct"] and not item["relation_was_correct"]:
                    categorized["verified_corrections"] += 1
                elif item["relation_was_correct"] and not item["relation_now_correct"]:
                    categorized["verified_regressions"] += 1
                categorized["by_evaluator_identity"].setdefault(aid if first_same else f"{aid}|{bid}", 0)
                categorized["by_evaluator_identity"][aid if first_same else f"{aid}|{bid}"] += 1
            else:
                item["status"] = "unverified_missing_identity"
                categorized["unverified_missing_identity"] += 1
        relation_changes_by_step[f"{left}_to_{right}"] = {"summary": categorized, "rows": changes}

    geometry_diag = read_json(geometry_root / "replay/frame_diagnostics.json")
    p3_by_id = {d["detection_id"]: d for d in raw_rows}
    gates = []
    for oid in gate_ids:
        frame_name, short_det = oid.rsplit(":det-", 1)
        index = int(frame_name.rsplit("-", 1)[-1])
        frame = geometry_diag[index]
        backend = frame.get("object_backend", {})
        assignment = assignment_data["geometry-only"][1].get(oid, {})
        candidates = [r for r in backend.get("candidate_ledger", []) if r.get("detection_id") == oid]
        target = min(candidates, key=lambda r: r.get("center_distance_m", float("inf"))) if candidates else None
        track_hint = target.get("track_id") if target else assignment.get("object_id")
        det = p3_by_id[oid]
        gates.append({"observation_id": oid, "frame_index": index, "track_candidate": track_hint,
                      "gate_decision": target, "p3_center_W": det["center_W"], "p3_extents_m": det["extents_m"],
                      "raw_class": det["class_name"], "official_evaluator_match_status": eval_map.get(oid, {}).get("match_status"),
                      "evaluator_semantic_id": eval_map.get(oid, {}).get("gt_semantic_id")})

    output.mkdir(parents=True)
    gate_html = _write_gate_gallery(output / "gate_failure_gallery.html", input_root, gate_ids, p3_by_id)
    metrics_doc = {"schema_version": 1, "input_run_id": input_root.name, "input_hashes": input_hashes,
                   "official_evaluator_ledger": str(eval_path), "official_evaluator_ledger_sha256": sha(eval_path),
                   "metric_identity_proxy": "HM3D semantic instance IDs; no human physical identity labels for this input",
                   "raw_p3_observations": len(raw_ids), "evaluator_identity_covered_observations": len(eligible),
                   "evaluator_identity_coverage": len(eligible) / len(raw_ids) if raw_ids else None,
                   "same_frame_pair_population_excluded_from_cross_frame": metrics["source"]["same_frame_pairs_excluded_from_cross_frame"],
                   "modes": metrics, "relation_changes": relation_changes_by_step,
                   "gate_cases": gates, "gate_gallery": gate_html}
    (output / "metrics.json").write_text(json.dumps(metrics_doc, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    _write_report(output / "report.vi.md", metrics_doc)
    return metrics_doc


def _write_gate_gallery(path: Path, input_root: Path, observation_ids: list[str], p3_by_id: dict) -> str:
    p2 = read_json(input_root / "detection_2d/detection_2d_index.json")
    dets = {d["detection_id"]: d for f in p2["frames"] for d in f["detections"]}
    assets = path.parent / "gate_gallery_assets"; assets.mkdir(parents=True)
    cards = []
    for oid in observation_ids:
        frame_name, det_suffix = oid.rsplit(":det-", 1)
        frame_idx = int(frame_name.rsplit("-", 1)[-1])
        rgb = np.asarray(np.load(input_root / "capture/frames" / f"{frame_idx:06d}/rgb.npy", allow_pickle=False), dtype=np.uint8)[..., :3]
        det = dets[oid]
        mask = np.asarray(np.load(input_root / "detection_2d" / det["mask_file"], allow_pickle=False), dtype=bool)
        overlay = rgb.copy(); overlay[mask] = (0.48 * overlay[mask] + 0.52 * np.asarray([30, 210, 250])).astype(np.uint8)
        image = Image.fromarray(overlay, "RGB")
        draw = ImageDraw.Draw(image); draw.rectangle((0,0,min(image.width,600),22),fill=(0,0,0)); draw.text((4,4),oid,fill="white")
        name = hashlib.sha256(oid.encode()).hexdigest()[:16] + ".jpg"
        image.save(assets / name, quality=88)
        d3 = p3_by_id[oid]
        cards.append(f"<article><h2>{html.escape(oid)}</h2><img src='gate_gallery_assets/{name}'>"
                     f"<pre>raw class={html.escape(d3['class_name'])}; center_W={d3['center_W']}; extents={d3['extents_m']}</pre></article>")
    page = "<!doctype html><meta charset='utf-8'><title>Gate failures</title><style>body{font:14px system-ui;max-width:1100px;margin:2rem auto;background:#f5f6f8;color:#19232d}article{background:white;border:1px solid #ccd;padding:1rem;margin:1rem}img{max-width:100%}</style>" + "".join(cards)
    path.write_text(page, encoding="utf-8")
    return path.name


def _write_report(path: Path, doc: dict) -> None:
    lines = ["# Object graph improvement — K0/K1 status", "",
             f"Input: `{doc['input_run_id']}`; raw P3 observations: {doc['raw_p3_observations']}; evaluator identity coverage: {doc['evaluator_identity_covered_observations']}/{doc['raw_p3_observations']} ({doc['evaluator_identity_coverage']:.1%}).",
             "", "Evaluator identities are HM3D semantic instance IDs, not user-confirmed physical identity. Same-frame pairs are excluded from cross-frame pairwise metrics.", "",
             "| Mode | Tracks | TP/FP/FN | P/R/F1 | False-merge tracks | Fragmented evaluator IDs | Aliases | Ambiguous | Deferred | Runtime | Peak RSS |",
             "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for mode in ("source", "geometry-only", "geometry-only-dedup-v2"):
        m = doc["modes"][mode]
        lines.append(f"| {mode} | {m['tracks_final']} | {m['pairwise_tp']}/{m['pairwise_fp']}/{m['pairwise_fn']} | {m['association_precision']}/{m['association_recall']}/{m['association_f1']} | {m['false_merge_tracks']} | {m['fragmented_evaluator_instances']} | {m['aliases']} | {m['ambiguous']} | {m['deferred']} | {m['runtime_seconds']:.1f}s | {m['peak_process_rss_mib']:.1f} MiB |")
    lines += ["", "## Pairwise relation changes", ""]
    for step, item in doc["relation_changes"].items():
        s = item["summary"]
        lines.append(f"- `{step}`: {s['total']} changed pairs; evaluator-verified {s['evaluator_verified_changes']} ({s['verified_corrections']} relation corrections, {s['verified_regressions']} regressions); same-frame diagnostic {s['same_frame_changes']}; identity unavailable {s['unverified_missing_identity']}.")
    lines += ["", "## Gate failure traces", "", "| Observation | Candidate track | Center distance | AABB IoU | Extent consistency | Gate failure | P3 extents (m) |", "|---|---|---:|---:|---:|---|---|"]
    for item in doc["gate_cases"]:
        gate = item["gate_decision"] or {}
        lines.append(f"| `{item['observation_id']}` | `{item['track_candidate']}` | {gate.get('center_distance_m')} | {gate.get('aabb_iou')} | {gate.get('extent_consistency')} | {', '.join(gate.get('gate_failures', []))} | {item['p3_extents_m']} |")
    lines += ["", f"Gallery: `{doc['gate_gallery']}`.", "", "Interpretation: a gate failure establishes why the geometry backend did not associate that observation. It does not by itself establish that the candidate was a true same-object match. Human identity is unavailable in this run.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-run", type=Path, required=True)
    ap.add_argument("--source-replay", type=Path, required=True)
    ap.add_argument("--geometry-replay", type=Path, required=True)
    ap.add_argument("--dedup-v2-replay", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--gate-observation", action="append", default=[])
    a = ap.parse_args()
    doc = run(a.input_run, a.source_replay, a.geometry_replay, a.dedup_v2_replay, a.output, a.gate_observation)
    print(json.dumps({"status": "pass", "output": str(a.output.resolve()),
                      "gt_coverage": f"{doc['evaluator_identity_covered_observations']}/{doc['raw_p3_observations']}",
                      "modes": {k: {"TP_FP_FN": [v["pairwise_tp"],v["pairwise_fp"],v["pairwise_fn"]], "P_R_F1": [v["association_precision"],v["association_recall"],v["association_f1"]]} for k,v in doc["modes"].items()}},indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
