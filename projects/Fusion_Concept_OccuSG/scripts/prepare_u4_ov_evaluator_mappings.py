#!/usr/bin/env python3
"""Create separate read-only raw and provisional fused-label evaluator maps."""
import hashlib, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/audits/u4_ov"
BASELINE_MAP = Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058/object_evaluations/eval_20261006T184217.537953Z_880c87/category_mapping_snapshot.json")

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def norm(s): return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()

def main():
    vocab = json.loads((OUT / "vocabulary_v1.json").read_text())
    old = json.loads(BASELINE_MAP.read_text())
    lock = json.loads((OUT / "u4_ov_config.locked.json").read_text())
    if sha(BASELINE_MAP) != json.loads((ROOT / "artifacts/audits/u4_semantic_fusion/u4_prereview_manifest.json").read_text())["category_mapping_snapshot_sha256"]:
        raise RuntimeError("baseline raw-evaluator mapping differs from its U4 lock")
    raw_map = {"schema_version": 1, "mapping_id": "hm3d_raw_detector_from_locked_legacy_snapshot_v1",
               "output_field": "raw_detector_class", "source_snapshot_read_only": str(BASELINE_MAP),
               "source_snapshot_sha256": sha(BASELINE_MAP),
               "protocol": "Use the locked legacy evaluator category->detector_class map and only confirmed rows with review provenance; do not alter the existing raw YOLO evaluator.",
               "confirmed_mapping_count_in_source": sum(1 for x in old.get("categories", {}).values()
                   if x.get("status") == "confirmed" and isinstance(x.get("provenance"), dict)
                   and all(isinstance(x["provenance"].get(k), str) and x["provenance"][k].strip()
                           for k in ("reviewer", "date", "decision"))),
               "predictor_input": False}
    rows = []
    gt_categories = old.get("categories", {})
    gt_by_norm = {norm(k): k for k in gt_categories}
    for item in vocab["categories"]:
        label = item["label"]
        category = gt_by_norm.get(norm(label))
        rows.append({"fused_semantic_label": label,
                     "evaluator_category_candidate": category,
                     "status": "provisional" if category else "unmapped",
                     "mapping_basis": "exact lexical candidate only; requires independent human review" if category else "no exact lexical category in locked evaluator snapshot",
                     "reviewer": None, "review_date": None, "decision": None,
                     "usable_for_scored_class_metrics": False})
    fused_map = {"schema_version": 1, "mapping_id": "u4_ov_fused_label_to_hm3d_category_v1",
                 "output_field": "fused_semantic_label", "vocabulary_id": lock["vocabulary_id"],
                 "vocabulary_sha256": lock["vocabulary_sha256"],
                 "evaluator_category_source_sha256": sha(BASELINE_MAP),
                 "protocol": {"matched": "exact candidate label/category only after a reviewer changes status to confirmed and records reviewer/date/decision",
                              "provisional": "excluded from class metrics",
                              "unmapped": "excluded from class metrics and counted unsupported/unmapped",
                              "unknown_or_conflicted_predictions": "abstentions, excluded from precision numerator and retained in coverage denominator",
                              "uncertain_or_unmatched_ground_truth": "excluded from correctness denominator and reported separately",
                              "no_implicit_aliases": True,
                              "no_automatic_category_confirmation": True},
                 "mappings": rows}
    (OUT / "raw_detector_evaluator_mapping_reference.json").write_text(json.dumps(raw_map, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    (OUT / "fused_label_evaluator_mapping_v1.json").write_text(json.dumps(fused_map, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps({"raw_source_sha256": raw_map["source_snapshot_sha256"],
                      "raw_confirmed_mapping_count": raw_map["confirmed_mapping_count_in_source"],
                      "fused_label_count": len(rows),
                      "fused_provisional": sum(r["status"] == "provisional" for r in rows),
                      "fused_unmapped": sum(r["status"] == "unmapped" for r in rows),
                      "fused_confirmed": sum(r["status"] == "confirmed" for r in rows)}, indent=2))

if __name__ == "__main__": main()
