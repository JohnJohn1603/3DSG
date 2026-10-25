#!/usr/bin/env python3
"""Score a completed finite human-review response against existing artifacts.

This only joins review decisions with already-produced readiness/mapping data.
It never reruns perception, mapping, or room scoring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--readiness", type=Path, required=True)
    ap.add_argument("--static-diagnostic", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    package = args.package.resolve()
    review = json.loads((package / "human_review_response_v1.json").read_text())
    readiness = json.loads(args.readiness.resolve().read_text())
    static = json.loads(args.static_diagnostic.resolve().read_text())
    by_hyp = {x["hypothesis_id"]: x for x in readiness["objects"]}
    by_review = {x["review_id"]: x for x in review["hypotheses"]}
    id_by_review = {"H01": "hypothesis_69353ed1529074db", "H02": "hypothesis_4bcc26680b2ede85", "H03": "hypothesis_e238d343db74a41b", "H04": "hypothesis_0415c816d24c0a17", "H05": "hypothesis_d579ba788876c64a", "H06": "hypothesis_bded5482ee86bd0e"}
    a_categories = {"bed", "sofa", "chair", "dining_table", "toilet", "sink", "refrigerator", "oven", "microwave", "potted_plant", "tv"}

    rows = []
    for rid, human in by_review.items():
        hyp = by_hyp[id_by_review[rid]]
        human_class = human["class"]
        membership = hyp["membership"]
        class_supported = human_class in a_categories
        row = {
            "review_id": rid,
            "hypothesis_id": id_by_review[rid],
            "human_identity": human.get("physical_identity_token"),
            "human_identity_status": human["identity_status"],
            "human_class": human_class,
            "human_mask_purity": human.get("mask_purity"),
            "observation_count": len(human.get("observation_reviews", {})) or hyp["detection_count"],
            "mapping_admission": hyp["admission_status"],
            "mapping_raw_class_history": hyp["raw_class_history"],
            "mapping_semantic_category": hyp.get("semantic_category_raw_exact"),
            "mapping_semantic_status": hyp.get("semantic_status"),
            "mapping_membership_status": membership["status"],
            "mapping_membership_room_id": membership["room_id"],
            "mapping_outside_fraction": membership["outside_fraction"],
            "human_class_in_A_vocabulary": class_supported,
            "diagnostic_error_attribution": [],
        }
        if rid == "H01":
            row["diagnostic_error_attribution"] += ["association_false_merge_armchair_1_armchair_2", "association_fragmentation_armchair_2", "mask_purity_one_observation_not_pure", "semantic_vocabulary_mismatch_armchair_not_in_A"]
        elif rid == "H02":
            row["diagnostic_error_attribution"] += ["association_armchair_2_observation_fragmented_into_H01", "semantic_vocabulary_mismatch_armchair_not_in_A"]
        elif rid == "H03":
            row["diagnostic_error_attribution"] += ["perception_class_or_vocabulary_mismatch_piano_leg", "A_vocabulary_unsupported"]
        elif rid == "H04":
            row["diagnostic_error_attribution"] += ["membership_false_positive_outside_room_polygon", "A_vocabulary_unsupported_car"]
        elif rid == "H05":
            row["diagnostic_error_attribution"] += ["membership_false_negative_unassigned_but_user_review_inside_polygon", "perception_class_or_vocabulary_mismatch_mirror", "A_vocabulary_unsupported_mirror"]
        elif rid == "H06":
            row["diagnostic_error_attribution"] += ["association_and_mask_review_consistent", "class_supported_by_A"]
        rows.append(row)

    room6 = next(x for x in static["rooms"] if x["room_id"] == "room:6")
    human_room = review["room_review"]
    room_result = {
        "room_id": "room:6",
        "human_review_label": human_room["room_type"],
        "human_review_status": human_room["status"],
        "diagnostic_candidate_label": room6.get("candidate_label"),
        "diagnostic_commit_label": room6.get("commit_label"),
        "diagnostic_status": room6.get("status"),
        "diagnostic_reason": room6.get("reason"),
        "comparison": "candidate_mismatch_but_no_committed_wrong_label" if room6.get("commit_label") is None and room6.get("candidate_label") != human_room["room_type"] else "matches_or_requires_review",
        "supported_by_objects": room6.get("supporting_objects", []),
        "room_annotation_is_independent": True,
    }
    metrics = {
        "reviewed_hypotheses": len(rows),
        "reviewed_observations": sum(x["observation_count"] for x in rows),
        "mixed_identity_hypotheses": sum(x["human_identity_status"] == "confirmed_mixed" for x in rows),
        "same_physical_object_hypotheses": sum(x["human_identity_status"] == "confirmed" for x in rows),
        "human_identity_tokens": sorted({t for x in rows for t in ([x["human_identity"]] if isinstance(x["human_identity"], str) else [])}),
        "false_merge_tracks": ["H01: armchair_1 + armchair_2"],
        "fragmentation_relations": ["armchair_2 appears in H01 and H02"],
        "membership_outcomes": {"outside_polygon": ["car_1/H04"], "inside_or_not_outside_by_review": ["H01", "H02", "H03", "H05", "H06"], "existing_unassigned_conflicting_with_review": ["H05"]},
        "human_class_supported_by_A": sum(x["human_class_in_A_vocabulary"] for x in rows),
        "human_class_unsupported_by_A": sum(not x["human_class_in_A_vocabulary"] for x in rows),
        "room_function_accuracy": "unavailable_as_general_metric; one reviewed room only",
    }
    result = {
        "schema": "human-review-scored-diagnostic/00802-v1",
        "run_id": review["run_id"],
        "scope": "completed user review of six retained hypotheses and room:6",
        "no_pipeline_rerun": True,
        "metrics": metrics,
        "hypotheses": rows,
        "room": room_result,
        "limitations": ["small reviewed subset", "room-function review covers one room", "unsupported human classes are not added to A vocabulary", "this report does not change assignments or room geometry"],
        "provenance": {"review_sha256": sha256(package / "human_review_response_v1.json"), "readiness_sha256": sha256(args.readiness.resolve()), "static_diagnostic_sha256": sha256(args.static_diagnostic.resolve())},
    }
    out = args.output.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    md = ["# Human review scoring — 00802 min3", "", "Artifact-only scoring; no pipeline rerun.", "", "## Tóm tắt", "", f"- Hypotheses reviewed: {metrics['reviewed_hypotheses']}", f"- Observations reviewed: {metrics['reviewed_observations']}", "- H01: false merge armchair_1 + armchair_2; armchair_2 also fragmented across H01/H02.", "- H04: car_1 is outside room polygon; existing membership is a false positive.", "- H05: user review places the object inside the polygon; existing unassigned status is a membership false negative.", "- Room:6 human label is living_room; diagnostic candidate is kitchen but commit is null, so no wrong committed room label.", "", "## Attribution by hypothesis", "", "| Review | Human identity/class | Mask | Existing mapping | Attribution |", "|---|---|---|---|---|"]
    for x in rows:
        md.append(f"| {x['review_id']} | {x['human_identity']} / {x['human_class']} | {x['human_mask_purity']} | {x['mapping_semantic_category']} / {x['mapping_membership_status']} | {', '.join(x['diagnostic_error_attribution'])} |")
    md += ["", "## Giới hạn", "", "Room quality accuracy tổng quát chưa thể chấm từ một room. Human classes ngoài A vocabulary được ghi nhận là unsupported; không tự mở rộng vocabulary hoặc sửa predictor.", ""]
    out.with_suffix(".md").write_text("\n".join(md))
    print(json.dumps({"json": str(out), "markdown": str(out.with_suffix('.md')), "reviewed_hypotheses": len(rows), "reviewed_observations": metrics["reviewed_observations"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
