#!/usr/bin/env python3
"""Export evaluator-only pairs/contact sheets after causal predictions are frozen."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/u3_causal_prefix"
HOLDOUT_START, HOLDOUT_END = 231, 238


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    report = json.loads((OUT / "causal_holdout_report.json").read_text())
    input_index = json.loads((OUT / "causal_input_index.json").read_text())
    index = {r["detection_id"]: r for r in input_index["records"]}
    resources = json.loads((ROOT / "locks/external_resources.json").read_text())["files"]
    gt_lock = resources["dense_object_evaluation_observation_matches"]
    gt_path = Path(gt_lock["path"])
    if gt_path.stat().st_size != gt_lock["bytes"] or sha_file(gt_path) != gt_lock["sha256"]:
        raise RuntimeError("evaluator match file changed from lock")
    gt = {r["detection_id"]: r for r in (json.loads(line) for line in gt_path.read_text().splitlines())}
    pred: dict[str, dict[str, str]] = {}
    for mode in ("source_raw", "source_dedup", "geometry_only", "geometry_appearance"):
        pred[mode] = {}
        for row in (json.loads(line) for line in (OUT / f"decisions_{mode}.jsonl").read_text().splitlines()):
            if row.get("track_id") is not None:
                pred[mode][row["detection_id"]] = row["track_id"]
    raw_ids = sorted(index, key=lambda did: (int(index[did]["frame_index"]), did))
    holdout_ids = {did for did, r in index.items() if HOLDOUT_START <= int(r["frame_index"]) <= HOLDOUT_END}
    rows = []
    for i, left in enumerate(raw_ids):
        for right in raw_ids[i + 1:]:
            if left not in holdout_ids and right not in holdout_ids:
                continue
            if index[left]["frame_id"] == index[right]["frame_id"]:
                continue
            gl, gr = gt.get(left, {}), gt.get(right, {})
            left_matched = gl.get("match_status") == "matched" and gl.get("gt_semantic_id") is not None
            right_matched = gr.get("match_status") == "matched" and gr.get("gt_semantic_id") is not None
            gt_relation = ("unknown_or_unmatched" if not (left_matched and right_matched)
                           else "same_evaluator_instance" if gl["gt_semantic_id"] == gr["gt_semantic_id"]
                           else "different_evaluator_instances")
            # The locked feature index intentionally does not carry class labels;
            # read detector classes from saved P3 only, after the prediction log is frozen.
            row = {"detection_id_left": left, "frame_left": index[left]["frame_index"],
                   "detection_id_right": right, "frame_right": index[right]["frame_index"],
                   "gt_match_status_left_posthoc": gl.get("match_status", "missing_from_evaluator"),
                   "gt_category_left_posthoc": gl.get("gt_category"),
                   "gt_semantic_id_left_posthoc": gl.get("gt_semantic_id"),
                   "gt_match_status_right_posthoc": gr.get("match_status", "missing_from_evaluator"),
                   "gt_category_right_posthoc": gr.get("gt_category"),
                   "gt_semantic_id_right_posthoc": gr.get("gt_semantic_id"),
                   "gt_relation_posthoc": gt_relation,
                   "crop_ref_left": index[left]["crop_ref"], "crop_ref_right": index[right]["crop_ref"]}
            for mode in pred:
                tl, tr = pred[mode].get(left), pred[mode].get(right)
                row[f"same_track_{mode}"] = bool(tl and tl == tr)
                row[f"track_{mode}_left"], row[f"track_{mode}_right"] = tl, tr
            row["false_merge_modes_posthoc"] = ";".join(
                mode for mode in pred if row[f"same_track_{mode}"] and gt_relation == "different_evaluator_instances")
            rows.append(row)

    # Join detector classes after all strategy outputs are frozen; used for
    # contact-sheet labels only, never to alter strata based on an individual class.
    d3_lock = resources["dense_detection_3d_cache"]
    d3_path = Path(d3_lock["path"])
    if d3_path.stat().st_size != d3_lock["bytes"] or sha_file(d3_path) != d3_lock["sha256"]:
        raise RuntimeError("P3 cache changed from lock")
    classes = {d["detection_id"]: d.get("class_name") for line in d3_path.read_text().splitlines()
               for d in json.loads(line).get("detections", []) if d["detection_id"] in index}
    for row in rows:
        row["class_left_review_only"] = classes.get(row["detection_id_left"])
        row["class_right_review_only"] = classes.get(row["detection_id_right"])
        cl, cr = row["class_left_review_only"], row["class_right_review_only"]
        row["class_relation_review_only"] = "same_detector_class" if (cl or "").lower() == (cr or "").lower() else "cross_class"

    pair_path = OUT / "holdout_pair_review_posthoc.csv"
    with pair_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["detection_id_left"])
        writer.writeheader(); writer.writerows(rows)

    cross_same = [r for r in rows if r["class_relation_review_only"] == "cross_class" and r["gt_relation_posthoc"] == "same_evaluator_instance"]
    same_diff_all = [r for r in rows if r["class_relation_review_only"] == "same_detector_class" and r["gt_relation_posthoc"] == "different_evaluator_instances"]
    # One deterministic pair per class/evaluator-instance pair keeps the sheet
    # inspectable while the CSV retains every pair and every prediction.
    same_diff = []
    seen = set()
    for r in same_diff_all:
        key = (r["class_left_review_only"], *sorted((str(r["gt_semantic_id_left_posthoc"]), str(r["gt_semantic_id_right_posthoc"]))))
        if key not in seen:
            seen.add(key); same_diff.append(r)
    false_merge = [r for r in rows if r["false_merge_modes_posthoc"]]
    strata = {"cross_class_same_evaluator_instance": cross_same,
              "same_class_different_evaluator_instance": same_diff,
              "likely_false_merge_posthoc": false_merge}
    sheet_manifest = {}
    for name, pair_rows in strata.items():
        per_page, row_h, width = 6, 190, 820
        page_refs = []
        for page_no, start in enumerate(range(0, len(pair_rows), per_page), 1):
            batch = pair_rows[start:start + per_page]
            canvas = Image.new("RGB", (width, row_h * len(batch)), "white")
            draw = ImageDraw.Draw(canvas)
            for i, row in enumerate(batch):
                y = i * row_h
                title = (f"{row['detection_id_left']} ({row['class_left_review_only']}) GT:{row['gt_semantic_id_left_posthoc']} | "
                         f"{row['detection_id_right']} ({row['class_right_review_only']}) GT:{row['gt_semantic_id_right_posthoc']}")
                draw.text((7, y + 3), title[:135], fill="black")
                if row["false_merge_modes_posthoc"]:
                    draw.text((7, y + 20), "Predicted same track: " + row["false_merge_modes_posthoc"], fill="darkred")
                for side, field in enumerate(("crop_ref_left", "crop_ref_right")):
                    im = Image.open(ROOT / row[field]).convert("RGB")
                    im.thumbnail((390, 145))
                    x = side * 410 + 8
                    canvas.paste(im, (x, y + 42))
                    draw.rectangle((x, y + 42, x + 390, y + 187), outline="gray", width=1)
            path = OUT / f"holdout_contact_{name}_{page_no:02d}.png"
            canvas.save(path)
            page_refs.append(str(path.relative_to(ROOT)))
        sheet_manifest[name] = {"pair_count": len(pair_rows), "pages": page_refs}
    summary = {
        "schema_version": 1, "status": "posthoc_review_exported",
        "all_cross_frame_pairs_with_holdout": len(rows),
        "unknown_or_unmatched_pair_count": sum(r["gt_relation_posthoc"] == "unknown_or_unmatched" for r in rows),
        "pair_csv": str(pair_path.relative_to(ROOT)), "contact_sheets": sheet_manifest,
        "gt_used_only_after_predictions_frozen": True,
        "candidate_match_or_threshold_changed_by_review": False,
        "holdout_scope": "same-scene temporal holdout; no scene-generalization claim",
    }
    (OUT / "holdout_review_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
