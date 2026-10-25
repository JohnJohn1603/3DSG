#!/usr/bin/env python3
"""Build only the locked U6 evaluator-ID/collision diagnostic review panels."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_hm3d_object_gt as gt_eval  # noqa: E402


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def panel_filename(observation_id: str, panel_kind: str) -> str:
    """Use the complete observation identity so equal det suffixes cannot collide."""
    safe_panel = "".join(c if c.isalnum() or c in "-_" else "_" for c in panel_kind)
    observation_hash = hashlib.sha256(observation_id.encode("utf-8")).hexdigest()
    return f"obs-{observation_hash}__{safe_panel}.png"


def gt_panel_decision(official_match: dict | None, best_candidate: dict | None) -> dict:
    if official_match and official_match.get("gt_matched"):
        return {"panel_kind": "official_one_to_one_match", "gt_semantic_id": official_match.get("gt_semantic_id"),
                "category": official_match.get("gt_category"), "iou": official_match.get("mask_iou")}
    if best_candidate and float(best_candidate.get("mask_iou", 0.0)) > 0.0:
        return {"panel_kind": "best_overlap_candidate_not_official_match",
                "gt_semantic_id": best_candidate.get("gt_semantic_id"), "category": best_candidate.get("category"),
                "iou": best_candidate.get("mask_iou")}
    return {"panel_kind": "unmatched_no_positive_overlap_candidate", "gt_semantic_id": None,
            "category": None, "iou": None}


def save_verified_panel(out: Path, observation_id: str, panel_kind: str, pixels: np.ndarray) -> dict:
    filename = panel_filename(observation_id, panel_kind)
    path = out / filename
    Image.fromarray(pixels.astype(np.uint8)).save(path, format="PNG", optimize=False)
    reread = np.asarray(Image.open(path).convert("RGB"))
    expected = pixels[:, :, :3] if pixels.ndim == 3 and pixels.shape[2] == 4 else pixels
    if not np.array_equal(reread, expected.astype(np.uint8)):
        raise ValueError(f"saved panel pixels differ from source composition: {path}")
    return {"output_panel": str(path.resolve()), "output_sha256": sha256(path), "pixel_exact_after_reload": True}


def overlay(rgb, mask, color):
    out = rgb.copy()
    if out.ndim == 3 and out.shape[2] == 4: out = out[:, :, :3]
    out = out.astype(np.uint8)
    if mask is not None and mask.any():
        c = np.asarray(color, dtype=np.uint16)
        out[mask] = ((out[mask].astype(np.uint16) + c) // 2).astype(np.uint8)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--semantic-dir", type=Path, required=True)
    ap.add_argument("--matches", type=Path, required=True)
    ap.add_argument("--descriptor", type=Path, required=True)
    ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("--primary-review-index", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    if out.exists(): raise FileExistsError(out)
    out.mkdir(parents=True)
    run = args.run_dir.resolve(); sem = args.semantic_dir.resolve()
    cap = json.loads((run / "capture/capture_manifest.json").read_text())
    frames = {r["frame_id"]: r for r in cap["frames"]}
    p2, _ = gt_eval._read_detector(run)
    det_by_id = {d["detection_id"]: {**d, "frame_id": fid} for fid, ds in p2.items() for d in ds}
    matched = {r["detection_id"]: r for r in (json.loads(x) for x in args.matches.read_text().splitlines() if x.strip())}
    descriptors = gt_eval.parse_descriptor(args.descriptor.resolve())
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    gt_instances_by_frame = {}
    for fid in frames:
        gtmap_path = sem / "frames" / f"{fid}__semantic_id_map.npy"
        instances, _, _ = gt_eval._read_gt_frame(gtmap_path, descriptors, mapping)
        gt_instances_by_frame[fid] = instances
    # The official ledger must itself satisfy one-to-one GT matching per frame.
    official_ids_by_frame = {}
    for row in matched.values():
        if row.get("gt_matched"):
            key = (row["frame_id"], int(row["gt_semantic_id"]))
            official_ids_by_frame.setdefault(key[0], []).append(key[1])
    duplicate_official = {fid: ids for fid, ids in official_ids_by_frame.items() if len(ids) != len(set(ids))}
    if duplicate_official:
        raise ValueError(f"official match ledger is not one-to-one by frame: {duplicate_official}")
    primary = [json.loads(x) for x in args.primary_review_index.read_text().splitlines() if x.strip()]
    primary_ids = {r.get("observation_id") for r in primary if r.get("observation_id")}
    prefix = "hm3d-00808-u6q-v1-"
    requested = {
        "D1-evaluator-573": [f"{prefix}{i:04d}:det-{j:04d}" for i,j in ((0,1),(1,3),(2,3),(4,1))],
        "D2-same-frame-collision": [f"{prefix}0002:det-{j:04d}" for j in (4,5)],
    }
    primary_links = {oid: sorted({r["case_id"] for r in primary if r.get("observation_id") == oid}) for oid in primary_ids}
    cases = []; index = []
    for case_id, obs_ids in requested.items():
        rows = []
        for oid in obs_ids:
            if oid in primary_ids:
                index.append({"case_id": case_id, "observation_id": oid, "status": "already_in_primary_review",
                              "primary_case_ids": primary_links.get(oid, [])})
                continue
            if oid not in det_by_id:
                index.append({"case_id": case_id, "observation_id": oid, "status": "observation_not_found"})
                continue
            det = det_by_id[oid]; fid = det["frame_id"]; rec = frames[fid]
            rgb = np.load(run / "capture" / rec["frame_dir"] / "rgb.npy", allow_pickle=False)
            p_mask = det["mask"]
            m = matched.get(oid, {"gt_matched": False})
            candidates = [{"gt_semantic_id": int(inst["semantic_id"]), "category": inst["category"],
                           "area_px": int(inst["area"]), "mask_iou": gt_eval._mask_iou(p_mask, inst["mask"])}
                          for inst in gt_instances_by_frame[fid]]
            best_candidate = max(candidates, key=lambda x: x["mask_iou"], default=None)
            gt_status = gt_panel_decision(m, best_candidate)
            g_mask = None
            if gt_status["gt_semantic_id"] is not None:
                gtmap = np.load(sem / "frames" / f"{fid}__semantic_id_map.npy", allow_pickle=False)
                g_mask = gtmap == int(gt_status["gt_semantic_id"])
            source_rgb = run / "capture" / rec["frame_dir"] / "rgb.npy"
            source_mask = det["mask_path"]
            source_gt = sem / "frames" / f"{fid}__semantic_id_map.npy"
            panel_specs = [
                ("rgb", overlay(rgb, None, (0,0,0)), source_rgb, "capture_rgb"),
                ("prediction-mask", overlay(rgb, p_mask, (20,235,70)), source_mask, "detector_mask_over_rgb"),
                ("gt-mask", overlay(rgb, g_mask, (235,40,160)), source_gt, gt_status["panel_kind"]),
            ]
            panels = []
            for panel_name, pixels, source_path, source_role in panel_specs:
                output = save_verified_panel(out, oid, panel_name, pixels)
                panels.append({"panel_kind": panel_name, "source_role": source_role,
                               "source_path": str(source_path.resolve()), "source_sha256": sha256(source_path),
                               **output})
            row = {"observation_id": oid, "frame_id": fid, "frame_index": rec["frame_index"],
                   "official_matching_status": "official_one_to_one_match" if m.get("gt_matched") else "unmatched",
                   "official_gt_semantic_id": m.get("gt_semantic_id") if m.get("gt_matched") else None,
                   "official_gt_category": m.get("gt_category") if m.get("gt_matched") else None,
                   "official_iou": m.get("mask_iou") if m.get("gt_matched") else None,
                   "best_overlap_candidate": best_candidate,
                   "gt_panel": gt_status,
                   "source_rgb_path": str(source_rgb.resolve()), "source_rgb_sha256": sha256(source_rgb),
                   "source_prediction_mask_path": str(source_mask.resolve()), "source_prediction_mask_sha256": sha256(source_mask),
                   "source_gt_semantic_map_path": str(source_gt.resolve()), "source_gt_semantic_map_sha256": sha256(source_gt),
                   "panels": panels,
                   "review_identity": "unreviewed", "review_class": "unreviewed",
                   "pose_revisit_only": rec["frame_index"] == 4}
            rows.append(row)
            index.append({"case_id": case_id, **row, "status": "diagnostic_only_not_added_to_primary_sample"})
        if not rows: continue
        cards = []
        for row in rows:
            panels = "".join(f"<figure><figcaption>{html.escape(p['panel_kind'])}</figcaption><img src='{html.escape(Path(p['output_panel']).name)}'></figure>" for p in row["panels"])
            extra = " · exact-pose revisit; not an independent view" if row["pose_revisit_only"] else ""
            status = row["gt_panel"]["panel_kind"]
            iou = row["gt_panel"].get("iou")
            gt_note = {"official_one_to_one_match": "GT panel is the official one-to-one Hungarian match",
                       "best_overlap_candidate_not_official_match": "GT panel is the best-overlap candidate only; this observation is unmatched by the one-to-one Hungarian ledger",
                       "unmatched_no_positive_overlap_candidate": "GT panel has no positive-overlap eligible candidate; it shows RGB without a GT mask"}[status]
            cards.append(f"<section><h3>{html.escape(row['observation_id'])}</h3><p>{html.escape(row['frame_id'])}{extra}; {html.escape(gt_note)}; IoU={iou if iou is not None else 'N/A'}.</p><div class='panels'>{panels}</div><p>Physical identity for this observation (identity token, split, or uncertain): ____________________</p><p>Class for this observation (free text or uncertain): ____________________</p></section>")
        if case_id == "D1-evaluator-573":
            question = "Across these four observations, which are the same physical object? Give identity token per observation (or split / uncertain); do not assume evaluator grouping is physical identity."
        else:
            question = "Are these two same-frame detections the same physical object (duplicate observation), different objects, or uncertain? Give class for each observation separately."
        cases.append(f"<article><h2>{case_id}</h2><p>{html.escape(question)}</p>{''.join(cards)}</article>")
    page = """<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>U6 diagnostic review</title><style>body{font:15px system-ui;margin:20px;background:#f4f5f7}article{background:white;border:1px solid #bbc;border-radius:8px;padding:14px;margin:14px 0}section{border-top:1px solid #ddd;padding:12px 0}.panels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}figure{margin:0}figcaption{font-weight:600;text-align:center}img{width:100%;height:auto}h1{font-size:24px}code{overflow-wrap:anywhere}</style><h1>U6 diagnostic review — 00808</h1><p>Only requested diagnostic observations are shown. The primary 13-observation review remains unchanged. Predicted track IDs, raw classes, U4-OV labels, evaluator categories and semantic IDs are hidden. Green is the predicted mask panel; magenta is the evaluator GT mask panel. RGB, prediction, and GT are separate panels. Please annotate identity and class independently; leave uncertain/unreviewed when needed. These cases are diagnostic and do not add rows/pairs to representative primary metrics.</p>""" + "".join(cases) + "</html>"
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "diagnostic_review_index.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in index), encoding="utf-8")
    output_paths = [p["output_panel"] for x in index for p in x.get("panels", [])]
    if len(output_paths) != len(set(output_paths)):
        raise ValueError("panel output path reused across diagnostic observations")
    manifest = {"status": "ready_for_user_review", "primary_review_preserved": True,
                "primary_overlap_observation_count": sum(x.get("status") == "already_in_primary_review" for x in index),
                "diagnostic_observation_count": sum(x.get("status") == "diagnostic_only_not_added_to_primary_sample" for x in index),
                "panel_count": len(output_paths), "unique_panel_output_paths": len(set(output_paths)),
                "all_panel_pixels_verified_exact_after_reload": all(p.get("pixel_exact_after_reload") for x in index for p in x.get("panels", [])),
                "diagnostic_case_ids": list(requested), "predicted_tracks_or_labels_shown": False,
                "prediction_masks_shown_in_separate_panel": True, "gt_masks_shown_in_separate_panel": True,
                "detection_gt_matches_sha256": sha256(args.matches.resolve()),
                "primary_review_index_sha256": sha256(args.primary_review_index.resolve())}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == "__main__": main()
