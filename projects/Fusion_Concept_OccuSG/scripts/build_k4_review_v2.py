#!/usr/bin/env python3
"""Build a posthoc K4 identity review and track-label-invariant analysis.

Reads only existing K4/capture/P2/native replay artifacts. It does not run
perception, association, evaluation prediction, or native mapping.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.objects.profile_comparison import (  # noqa: E402
    pairwise_identity_metrics, partition_delta,
)


DEFAULT_INPUT = ROOT / "artifacts/runs/hm3d_00802_20261011T033053.236654Z_086a73"
DEFAULT_K4 = ROOT / "artifacts/audits/object_graph_improvement/k4_surface_support_v1_locked"
DEFAULT_SOURCE = ROOT / "artifacts/runs/u5m9_20261011T035542.851355Z_b39b87/replay"
DEFAULT_DEDUP = ROOT / "artifacts/runs/u5m9_20261011T035821.873971Z_5aaaf1/replay"
DEFAULT_OUTPUT = ROOT / "artifacts/audits/object_graph_improvement/k4_review_v3"
TARGET_A = "hm3d-00802-main-0017:det-0001"
TARGET_B = "hm3d-00802-main-0018:det-0000"


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def assignment_map(path: Path, key: str, value: str) -> dict[str, str | None]:
    return {str(row[key]): None if row.get(value) is None else str(row[value]) for row in read_jsonl(path)}


def lineage_map(path: Path) -> dict[str, str | None]:
    result = {}
    for row in read_jsonl(path):
        oid = str(row["detection_id"])
        result[oid] = str(row["object_id"]) if row.get("object_id") is not None else None
    return result


def exact_memberships(assignments: dict[str, str | None]) -> dict[frozenset[str], str]:
    groups: dict[str, set[str]] = {}
    for oid, tid in assignments.items():
        if tid is not None:
            groups.setdefault(tid, set()).add(oid)
    return {frozenset(members): tid for tid, members in groups.items()}


def compare_modes(left_name: str, right_name: str, left: dict, right: dict,
                  evaluator_ids: dict[str, str | None], ids: list[str]) -> dict:
    delta = partition_delta(left, right, ids)
    transitions = delta.pop("relation_transitions")
    verified = corrections = regressions = same_frame = unavailable = 0
    rows = []
    for row in transitions:
        a, b = row["observation_ids"]
        if a.split(":det-")[0] == b.split(":det-")[0]:
            status = "same_frame_diagnostic_only"
            same_frame += 1
        elif evaluator_ids.get(a) is None or evaluator_ids.get(b) is None:
            status = "identity_unavailable"
            unavailable += 1
        else:
            gt_same = evaluator_ids[a] == evaluator_ids[b]
            before_same = row["source_relation"] == "same_track"
            after_same = row["candidate_relation"] == "same_track"
            was_correct, now_correct = before_same == gt_same, after_same == gt_same
            status = "evaluator_correction" if now_correct and not was_correct else (
                "evaluator_regression" if was_correct and not now_correct else "evaluator_relation_changed")
            verified += 1
            corrections += int(now_correct and not was_correct)
            regressions += int(was_correct and not now_correct)
            row.update({"evaluator_ids": [evaluator_ids[a], evaluator_ids[b]],
                        "was_correct": was_correct, "now_correct": now_correct})
        rows.append({**row, "status": status})
    return {"comparison": f"{left_name}_to_{right_name}", **delta,
            "evaluator_verified_relation_changes": verified,
            "evaluator_corrections": corrections, "evaluator_regressions": regressions,
            "same_frame_diagnostic_only": same_frame, "identity_unavailable": unavailable,
            "changed_observation_relations": rows}


def _safe_id(oid: str) -> str:
    return hashlib.sha256(oid.encode("utf-8")).hexdigest()[:24]


def _render_observation(input_root: Path, output: Path, oid: str, det: dict, frame_index: int) -> dict:
    frame_dir = input_root / "capture/frames" / f"{frame_index:06d}"
    rgb_path = frame_dir / "rgb.npy"
    mask_path = input_root / "detection_2d" / det["mask_file"]
    rgb = np.asarray(np.load(rgb_path, allow_pickle=False), dtype=np.uint8)[..., :3]
    mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
    if mask.shape != rgb.shape[:2]:
        raise ValueError(f"RGB/mask shape mismatch for {oid}: {rgb.shape} vs {mask.shape}")
    if not mask.any():
        raise ValueError(f"empty P2 mask for {oid}")
    overlay = rgb.copy()
    tint = np.asarray([0, 205, 245], dtype=np.uint8)
    overlay[mask] = ((overlay[mask].astype(np.uint16) * 40 + tint.astype(np.uint16) * 60) // 100).astype(np.uint8)
    ys, xs = np.where(mask)
    pad = max(8, int(round(max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1) * 0.15)))
    x0, x1 = max(0, int(xs.min()) - pad), min(rgb.shape[1], int(xs.max()) + pad + 1)
    y0, y1 = max(0, int(ys.min()) - pad), min(rgb.shape[0], int(ys.max()) + pad + 1)
    crop = overlay[y0:y1, x0:x1].copy()

    stem = _safe_id(oid)
    asset_root = output / "assets"
    asset_root.mkdir(parents=True, exist_ok=True)
    paths = {"rgb_original": asset_root / f"{stem}__rgb_original.png",
             "rgb_mask_overlay": asset_root / f"{stem}__rgb_mask_overlay.png",
             "crop_mask_overlay": asset_root / f"{stem}__crop_mask_overlay.png"}
    Image.fromarray(rgb, "RGB").save(paths["rgb_original"])
    Image.fromarray(overlay, "RGB").save(paths["rgb_mask_overlay"])
    Image.fromarray(crop, "RGB").save(paths["crop_mask_overlay"])

    # Pixel-level source-to-panel checks, including exact crop correspondence.
    original_saved = np.asarray(Image.open(paths["rgb_original"]).convert("RGB"))
    overlay_saved = np.asarray(Image.open(paths["rgb_mask_overlay"]).convert("RGB"))
    crop_saved = np.asarray(Image.open(paths["crop_mask_overlay"]).convert("RGB"))
    if not np.array_equal(original_saved, rgb) or not np.array_equal(overlay_saved, overlay):
        raise RuntimeError(f"RGB panel content mismatch for {oid}")
    if not np.array_equal(crop_saved, overlay[y0:y1, x0:x1]):
        raise RuntimeError(f"crop panel content mismatch for {oid}")

    relpaths = {key: path.relative_to(output).as_posix() for key, path in paths.items()}
    return {
        "observation_id": oid, "frame_index": frame_index,
        "source_rgb": str(rgb_path.resolve()), "source_rgb_sha256": sha_file(rgb_path),
        "source_rgb_pixel_sha256": sha_bytes(np.ascontiguousarray(rgb).tobytes()),
        "source_mask": str(mask_path.resolve()), "source_mask_sha256": sha_file(mask_path),
        "source_mask_pixel_sha256": sha_bytes(np.ascontiguousarray(mask.astype(np.uint8)).tobytes()),
        "mask_pixel_count": int(mask.sum()), "image_shape_hw": list(mask.shape),
        "crop_xyxy": [x0, y0, x1, y1],
        "panels": {key: {"path": relpaths[key], "sha256": sha_file(paths[key]),
                         "pixel_sha256": sha_bytes(np.ascontiguousarray(arr).tobytes())}
                   for key, arr in (("rgb_original", original_saved),
                                    ("rgb_mask_overlay", overlay_saved),
                                    ("crop_mask_overlay", crop_saved))},
        "source_to_panel_verified": True,
    }


def build_review(input_root: Path, k4_root: Path, output: Path) -> dict:
    index = json.loads((input_root / "detection_2d/detection_2d_index.json").read_text(encoding="utf-8"))
    frame_indices = {frame["frame_id"]: int(frame["frame_index"]) for frame in index["frames"]}
    detections = {det["detection_id"]: det for frame in index["frames"] for det in frame["detections"]}
    ledger = read_jsonl(k4_root / "candidate_ledger.jsonl")

    def candidate_rows(oid: str) -> list[dict]:
        return [row for row in ledger if row.get("detection_id") == oid]

    row_a = next((row for row in candidate_rows(TARGET_A) if row.get("association_branch") == "surface_continuity"), None)
    row_b = next((row for row in candidate_rows(TARGET_B) if row.get("surface_branch_status") == "ambiguous_possible_mixed_mask_expansion"), None)
    if row_a is None or row_b is None:
        raise ValueError("locked candidate ledger is missing one of the two requested review decisions")
    prior_a = list(row_a["prior_evidence_ids"])
    prior_b = list(row_b["prior_evidence_ids"])
    future_b = "hm3d-00802-main-0019:det-0000"
    cases = [
        {"case_id": "A", "target_observation_id": TARGET_A,
         "observations": prior_a + [TARGET_A], "causal_context": prior_a,
         "selection": "All observations recorded as prior evidence by the candidate at the frame-17 surface-continuity decision, plus the target."},
        {"case_id": "B", "target_observation_id": TARGET_B,
         "observations": list(dict.fromkeys(prior_b + [TARGET_B, future_b])),
         "causal_context": prior_b, "posterior_context": [future_b],
         "selection": "Candidate prior evidence at frame 18, the target, and det-0000 at frame 19 as posterior-only review context; frame 19 was not available to the frame-18 predictor."},
    ]
    all_ids = list(dict.fromkeys(oid for case in cases for oid in case["observations"]))
    if len(all_ids) != len(set(all_ids)):
        raise RuntimeError("duplicate observation ID in review set")
    missing = [oid for oid in all_ids
               if oid not in detections or oid.rsplit(":det-", 1)[0] not in frame_indices]
    if missing:
        raise ValueError(f"review observations missing from P2 index: {missing}")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=True)
    records = [_render_observation(input_root, output, oid, detections[oid],
                                  frame_indices[oid.rsplit(":det-", 1)[0]]) for oid in all_ids]
    paths = [panel["path"] for row in records for panel in row["panels"].values()]
    if len(paths) != len(set(paths)):
        raise RuntimeError("review panel output path reused")
    record_by_id = {row["observation_id"]: row for row in records}

    cards = []
    for case in cases:
        case_cards = []
        for oid in case["observations"]:
            row = record_by_id[oid]
            p = row["panels"]
            case_cards.append(
                "<article class='observation'><h3>" + html.escape(oid) + "</h3>"
                "<div class='panels'>" +
                f"<figure><img src='{html.escape(p['rgb_original']['path'])}'><figcaption>RGB gốc</figcaption></figure>" +
                f"<figure><img src='{html.escape(p['rgb_mask_overlay']['path'])}'><figcaption>RGB + P2 mask</figcaption></figure>" +
                f"<figure class='crop'><img src='{html.escape(p['crop_mask_overlay']['path'])}'><figcaption>Crop + P2 mask</figcaption></figure>" +
                "</div><fieldset><legend>Annotation riêng cho observation</legend>"
                "<label>Identity token: <input class='identity-token' placeholder='ví dụ obj_A'></label> "
                "<label>Identity status: <select class='identity-status'><option>unreviewed</option><option>confirmed</option><option>uncertain</option></select></label> "
                "<label>Mask purity: <select class='mask-purity'><option>unreviewed</option><option>single_object</option><option>object_plus_other</option><option>uncertain</option></select></label>"
                "</fieldset></article>")
        posterior_note = ("<p class='posterior'>Frame 19 chỉ là bằng chứng hậu kiểm cho review, không được dùng trong predictor tại frame 18.</p>"
                         if case["case_id"] == "B" else "")
        cards.append(f"<section data-case-id='{case['case_id']}'><h2>Case {case['case_id']}</h2><p>{html.escape(case['selection'])}</p>{posterior_note}"
                     "<p><strong>a.</strong> Những observations nào cùng physical object? (ghi token/uncertain)"
                     "<br><strong>b.</strong> Mỗi mask phủ một vật hay vật đó kèm vật khác?"
                     "<br><strong>c.</strong> Với target, phần bề mặt mới là cùng vật nhìn rõ hơn, vật khác, hay uncertain?</p>"
                     "<fieldset><legend>Đánh giá quan hệ của case</legend>"
                     "<label>Target cùng physical object với prior evidence? <select class='same-physical-object'><option>unreviewed</option><option>same_object</option><option>different_object</option><option>uncertain</option></select></label> "
                     "<label>Phần bề mặt mới: <select class='new-surface-relation'><option>unreviewed</option><option>same_object_clearer_view</option><option>another_object</option><option>uncertain</option></select></label></fieldset>"
                     + "".join(case_cards) + "</section>")
    page = ("<!doctype html><html lang='vi'><meta charset='utf-8'><title>K4 identity review v3</title>"
            "<style>body{font:15px system-ui;max-width:1400px;margin:1rem auto;padding:0 1rem;background:#f4f6f8;color:#17212b}"
            "section{background:white;border:1px solid #cbd5df;padding:1rem;margin:1.2rem 0}"
            ".observation{border-top:1px solid #d8dee6;padding-top:.8rem;margin-top:1rem}.panels{display:grid;grid-template-columns:1fr 1fr .7fr;gap:.8rem}"
            "figure{margin:0}img{display:block;max-width:100%;max-height:440px;object-fit:contain;background:#111}figcaption{font-size:.9rem;color:#415466;margin:.35rem 0}"
            ".posterior{background:#fff3d2;padding:.6rem}fieldset{margin-top:.7rem}.crop img{max-height:330px}</style>"
            "<h1>K4 — human identity review v3</h1><button id='download'>Tải CSV câu trả lời</button>"
            "<p>Chỉ RGB/mask và full observation IDs. Không hiển thị predicted track, GT category hoặc class prediction. "
            "Hãy điền form hoặc dùng CSV templates; không cần xác nhận class.</p>" + "\n".join(cards) +
            "<script>function csv(rows){return rows.map(r=>r.map(v=>'\"'+String(v??'').replaceAll('\"','\"\"')+'\"').join(',')).join('\\r\\n')}"
            "function save(name,rows){let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv(rows)],{type:'text/csv;charset=utf-8'}));a.download=name;a.click();URL.revokeObjectURL(a.href)}"
            "document.getElementById('download').onclick=()=>{let obs=[['case_id','observation_id','physical_identity_token','identity_status','mask_purity','notes']];"
            "document.querySelectorAll('article.observation').forEach(a=>obs.push([a.closest('section').dataset.caseId,a.querySelector('h3').textContent,a.querySelector('.identity-token').value,a.querySelector('.identity-status').value,a.querySelector('.mask-purity').value,'']));"
            "let cases=[['case_id','same_physical_object','new_surface_relation','notes']];document.querySelectorAll('section[data-case-id]').forEach(s=>cases.push([s.dataset.caseId,s.querySelector('.same-physical-object').value,s.querySelector('.new-surface-relation').value,'']));"
            "save('k4_review_v3_observations.csv',obs);save('k4_review_v3_cases.csv',cases)};</script></html>")
    (output / "gallery.html").write_text(page, encoding="utf-8")

    with (output / "observation_annotations_template.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "observation_id", "physical_identity_token",
                                               "identity_status", "mask_purity", "notes"])
        writer.writeheader()
        for case in cases:
            for oid in case["observations"]:
                writer.writerow({"case_id": case["case_id"], "observation_id": oid,
                                 "physical_identity_token": "", "identity_status": "unreviewed",
                                 "mask_purity": "unreviewed", "notes": ""})
    with (output / "case_assessment_template.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "target_observation_id", "compared_observation_ids",
                                               "same_physical_object", "new_surface_relation", "notes"])
        writer.writeheader()
        for case in cases:
            writer.writerow({"case_id": case["case_id"], "target_observation_id": case["target_observation_id"],
                             "compared_observation_ids": ";".join(case["observations"]),
                             "same_physical_object": "unreviewed", "new_surface_relation": "unreviewed", "notes": ""})
    manifest = {"schema_version": 3, "status": "awaiting_user_review", "review_only": True,
                "input_run_id": input_root.name, "candidate_artifact": str(k4_root.resolve()),
                "candidate_assignments_sha256": sha_file(k4_root / "candidate_assignments.jsonl"),
                "candidate_ledger_sha256": sha_file(k4_root / "candidate_ledger.jsonl"),
                "annotation_scope": "only observations explicitly listed below; no class labels requested",
                "cases": cases, "observation_records": records,
                "review_panel_count": len(records) * 3,
                "unique_output_paths": len(paths) == len(set(paths)),
                "all_source_to_panel_checks_passed": all(r["source_to_panel_verified"] for r in records),
                "predictor_rerun": False, "capture_detector_p3_native_replay_rerun": False,
                "gt_or_predicted_labels_displayed": False,
                "templates": ["observation_annotations_template.csv", "case_assessment_template.csv"]}
    (output / "review_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def build_changed_analysis(input_root: Path, k4_root: Path, source_replay: Path,
                           dedup_replay: Path, output: Path) -> dict:
    candidate = assignment_map(k4_root / "candidate_assignments.jsonl", "observation_id", "track_id")
    source = lineage_map(source_replay / "object_association_lineage.jsonl")
    dedup = lineage_map(dedup_replay / "object_association_lineage.jsonl")
    ids = sorted(candidate)
    if set(source) != set(ids) or set(dedup) != set(ids):
        raise ValueError("source/dedup/candidate assignment populations differ")
    evaluator_file = sorted((input_root / "object_evaluations").glob("eval_*/observation_matches.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True)[0]
    evaluator_rows = read_jsonl(evaluator_file)
    evaluator_ids = {str(r["detection_id"]): (str(r["gt_semantic_id"])
                       if r.get("match_status") == "matched" and r.get("gt_semantic_id") is not None else None)
                     for r in evaluator_rows if str(r.get("detection_id")) in set(ids)}
    if set(evaluator_ids) != set(ids):
        raise ValueError("evaluator population does not match assignments")
    comparisons = [compare_modes("source", "geometry-only-dedup-v2", source, dedup, evaluator_ids, ids),
                   compare_modes("geometry-only-dedup-v2", "accumulated-surface-support", dedup, candidate, evaluator_ids, ids),
                   compare_modes("source", "accumulated-surface-support", source, candidate, evaluator_ids, ids)]
    k1_metrics = json.loads((ROOT / "artifacts/audits/object_graph_improvement/k1_three_modes_v2/metrics.json").read_text())
    candidate_manifest = json.loads((k4_root / "manifest.json").read_text())
    runtime_rows = {
        "source_native_m1_m10_seconds": k1_metrics["modes"]["source"]["runtime_seconds"],
        "geometry_only_dedup_v2_native_m1_m10_seconds": k1_metrics["modes"]["geometry-only-dedup-v2"]["runtime_seconds"],
        "offline_association_surface_accumulation_seconds": candidate_manifest["causal_replay_runtime_seconds"],
        "offline_preflight_seconds": candidate_manifest.get("preflight_runtime_seconds"),
        "offline_post_p3_wall_seconds": candidate_manifest.get("post_p3_wall_seconds",
                                            candidate_manifest.get("end_to_end_runtime_seconds")),
        "p3_sidecar_generation_seconds": candidate_manifest.get("P3_sidecar_generation_runtime_seconds"),
        "comparison_note": "Native M1-M10 and offline association timings cover different work; no speedup ratio is computed. P3 sidecar generation is unmeasured.",
    }
    chosen = {}
    for target in (TARGET_A, TARGET_B):
        row = next(r for r in read_jsonl(k4_root / "candidate_decisions.jsonl") if r["observation_id"] == target)
        chosen[target] = {"decision": row["decision"], "reason": row["reason"],
                          "association_branch": row.get("association_branch"),
                          "timestamp_s": row.get("timestamp_s")}
    # These evidence lists are sourced from the locked causal candidate ledger.
    ledger = read_jsonl(k4_root / "candidate_ledger.jsonl")
    chosen[TARGET_A]["prior_evidence_ids"] = next(r["prior_evidence_ids"] for r in ledger
        if r.get("detection_id") == TARGET_A and r.get("association_branch") == "surface_continuity")
    chosen[TARGET_B]["prior_evidence_ids"] = next(r["prior_evidence_ids"] for r in ledger
        if r.get("detection_id") == TARGET_B and r.get("surface_branch_status") == "ambiguous_possible_mixed_mask_expansion")
    chosen[TARGET_B]["posterior_review_only_ids"] = ["hm3d-00802-main-0019:det-0000"]
    data = {"schema_version": 3, "status": "posthoc_analysis_only", "candidate_assignments_sha256": sha_file(k4_root / "candidate_assignments.jsonl"),
            "comparisons": comparisons, "focus_decisions": chosen,
            "runtime_scopes": runtime_rows,
            "candidate_raw_P3_observation_count": len(ids),
            "evaluator_eligible_observations": sum(v is not None for v in evaluator_ids.values()),
            "evaluator_coverage": sum(v is not None for v in evaluator_ids.values()) / len(ids),
            "predictor_rerun": False, "human_annotations_used": False}
    (output / "changed_decision_analysis_v2.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# K4 changed-decision analysis v2", "",
             "This report compares observation partitions, not track-ID strings. A pure track rename is reported separately and contributes zero identity relation changes.", "",
             f"Population: {len(ids)} raw observations; evaluator identity available for {data['evaluator_eligible_observations']}/{len(ids)} ({data['evaluator_coverage']:.1%}). HM3D IDs are dataset-level evaluator identities, not human-confirmed physical identity.", "",
             "| Comparison | Identity relation changes | ID-only renamed exact groups | Exact groups removed / added | Evaluator corrections / regressions | Same-frame diagnostics | Unavailable identity |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for comp in comparisons:
        lines.append(f"| {comp['comparison']} | {comp['identity_relation_changes']} | {comp['track_id_renamed_only_groups']} | {comp['source_membership_groups_removed']} / {comp['candidate_membership_groups_added']} | {comp['evaluator_corrections']} / {comp['evaluator_regressions']} | {comp['same_frame_diagnostic_only']} | {comp['identity_unavailable']} |")
    lines += ["", "## Runtime scope", "",
              "| Component | Runtime | Scope |", "|---|---:|---|",
              f"| Native source replay | {runtime_rows['source_native_m1_m10_seconds']:.2f} s | Native M1–M10 |",
              f"| Native geometry-only-dedup-v2 replay | {runtime_rows['geometry_only_dedup_v2_native_m1_m10_seconds']:.2f} s | Native M1–M10 |",
              f"| Offline association + surface accumulation | {runtime_rows['offline_association_surface_accumulation_seconds']:.2f} s | Locked P3 sidecars already available |",
              f"| Offline preflight / post-P3 wall time | {runtime_rows['offline_preflight_seconds']:.2f} / {runtime_rows['offline_post_p3_wall_seconds']:.2f} s | Excludes P3 sidecar generation |",
              "| P3 sidecar generation | not measured | No runtime estimate reported |", "",
              "The native and offline rows have different scopes; no speedup ratio is meaningful.", "",
              "## Causal evidence for the review cases", "",
              f"- `{TARGET_A}`: {chosen[TARGET_A]['decision']} / {chosen[TARGET_A]['reason']}; prior evidence: " + ", ".join(f"`{x}`" for x in chosen[TARGET_A]["prior_evidence_ids"]) + ".",
              f"- `{TARGET_B}`: {chosen[TARGET_B]['decision']} / {chosen[TARGET_B]['reason']}; prior evidence: " + ", ".join(f"`{x}`" for x in chosen[TARGET_B]["prior_evidence_ids"]) + ". `hm3d-00802-main-0019:det-0000` is posterior review context only, not frame-18 predictor input.", "",
              "Track IDs may differ while observation membership stays identical. The identity-change counts above come only from pairwise same/different relations. Per-observation track-label changes are not counted as identity changes.", "",
              "No predictor, association, capture, detector, P3, or native replay was rerun. This analysis uses locked assignments and existing evaluator ledger only."]
    (output / "changed_decision_analysis_v2.vi.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-run", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--k4-artifact", type=Path, default=DEFAULT_K4)
    parser.add_argument("--source-replay", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--dedup-replay", type=Path, default=DEFAULT_DEDUP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    review = build_review(args.input_run.resolve(), args.k4_artifact.resolve(), args.output.resolve())
    analysis = build_changed_analysis(args.input_run.resolve(), args.k4_artifact.resolve(),
                                      args.source_replay.resolve(), args.dedup_replay.resolve(), args.output.resolve())
    print(json.dumps({"output": str(args.output.resolve()), "review_observations": len(review["observation_records"]),
                      "panels": review["review_panel_count"],
                      "partition_changed_pairs": {x["comparison"]: x["identity_relation_changes"] for x in analysis["comparisons"]}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
