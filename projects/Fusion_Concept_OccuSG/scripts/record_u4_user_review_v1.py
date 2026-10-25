#!/usr/bin/env python3
"""Create an immutable, observation-scoped record of the six U4 reviews."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artifacts/audits/u4_review_package"
OUT = ROOT / "artifacts/audits/u4_user_review/versions/u4-review-20261008-v1"

CASES = [
    ("case_1", "class_change_tv_to_suitcase", "different_objects", [
        (10, "det-0005", "printer"), (230, "det-0005", "table"), (238, "det-0000", "printer")]),
    ("case_2", "class_change_chair_to_sofa", "same_object", [
        (42, "det-0000", "armchair"), (67, "det-0004", "armchair"),
        (95, "det-0002", "armchair"), (200, "det-0000", "armchair")]),
    ("case_3", "class_change_sofa_to_bench", "same_object", [
        (75, "det-0002", "piano"), (126, "det-0001", "piano"),
        (199, "det-0000", "piano"), (234, "det-0000", "piano")]),
    ("case_4", "conflicted_table_bed", "same_object", [
        (127, "det-0003", "table"), (223, "det-0001", "table"),
        (230, "det-0004", "table"), (232, "det-0004", "table")]),
    ("case_5", "stable_control_candidate_chair", "same_object", [
        (12, "det-0001", "chair"), (219, "det-0000", "chair"), (237, "det-0001", "chair")]),
    ("case_6", "false_merge_table_suitcase", "different_objects", [
        (230, "det-0005", "table"), (238, "det-0000", "printer")]),
]

ORIGINAL_REPLY = """Tôi đã review sáu case trong gói U4 và xác nhận:

- Case 1: khác vật.\\
  Theo thứ tự ảnh: printer → table → printer.\\
  Tôi chưa xác nhận hai ảnh printer có phải cùng một máy in.
- Case 2: cùng một vật; class thực tế là armchair.
- Case 3: cùng một vật; class thực tế là piano.
- Case 4: cùng một vật; class thực tế là table.\\
  Chưa xác nhận subtype dining_table.
- Case 5: cùng một vật; class thực tế là chair.
- Case 6: khác vật.\\
  Theo thứ tự ảnh: table → printer."""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    package = json.loads((PACKAGE / "package_manifest.json").read_text())
    if package["representative_observation_count"] != 18:
        raise RuntimeError("unexpected U4 review package observation count")
    OUT.mkdir(parents=True, exist_ok=False)
    rows: dict[str, dict] = {}
    case_map = []
    identity_relations = []
    for case_id, section_id, relation, items in CASES:
        expected = len(items)
        if expected == 0:
            raise RuntimeError(f"empty review case {case_id}")
        ids = []
        for frame, det, label in items:
            did = f"hm3d-00802-main-dense-{frame:04d}:{det}"
            ids.append(did)
            row = rows.setdefault(did, {
                "frame_id": f"hm3d-00802-main-dense-{frame:04d}", "frame_index": frame,
                "detection_id": det, "observation_id": did, "reviewed_category": label,
                "category_scope": "this observation only; no global category mapping",
                "provenance": "user-reviewed", "case_refs": [],
            })
            if row["reviewed_category"] != label:
                raise RuntimeError(f"conflicting user labels for {did}")
            row["case_refs"].append(case_id)
        case_map.append({"case_id": case_id, "html_section_id": section_id,
                         "review_order_observation_ids": ids,
                         "reviewed_image_count": expected,
                         "identity_relation_within_case": relation})
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                # Case 1 does not establish whether its first and third printer
                # observations are the same physical printer.
                rel = "uncertain" if case_id == "case_1" and left == ids[0] and right == ids[2] else relation
                identity_relations.append({"left_observation_id": left, "right_observation_id": right,
                                           "relation": rel, "provenance": "user-reviewed",
                                           "case_ref": case_id})

    annotations = sorted(rows.values(), key=lambda r: (r["frame_index"], r["detection_id"]))
    with (OUT / "observation_annotations.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(annotations[0]))
        writer.writeheader()
        for row in annotations:
            row = dict(row); row["case_refs"] = ";".join(row["case_refs"])
            writer.writerow(row)
    (OUT / "identity_relations.json").write_text(json.dumps({
        "schema_version": 1, "scope": "only observations explicitly reviewed in six U4 cases",
        "identity_is_separate_from_category": True,
        "no_same_identity_inference_from_shared_category": True,
        "relations": identity_relations,
    }, indent=2, ensure_ascii=False) + "\n")
    (OUT / "case_order_mapping.json").write_text(json.dumps({
        "source_html": "artifacts/audits/u4_review_package/index.html",
        "source_html_sha256": sha(PACKAGE / "index.html"),
        "cases": case_map,
    }, indent=2, ensure_ascii=False) + "\n")
    (OUT / "user_response_verbatim.vi.txt").write_text(ORIGINAL_REPLY + "\n")
    (OUT / "category_mapping_status.json").write_text(json.dumps({
        "provenance": "user-reviewed category labels are observation annotations, not global mappings",
        "mappings": {
            "table_to_dining_table": "unconfirmed; subtype explicitly not confirmed",
            "armchair_to_chair": "unconfirmed; no mapping protocol/approval supplied",
            "printer": "review label retained; unmapped if outside detector vocabulary",
            "piano": "review label retained; unmapped if outside detector vocabulary",
            "chair": "review label retained for reviewed observations only",
        },
        "predictor_input": False,
    }, indent=2, ensure_ascii=False) + "\n")
    manifest = {"schema_version": 1, "annotation_version": "u4-review-20261008-v1",
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "provenance": "user-reviewed", "scope_observation_count": len(annotations),
                "case_count": len(CASES), "predictor_input": False,
                "prior_review_package_preserved": True,
                "files": {p.name: sha(p) for p in sorted(OUT.iterdir()) if p.is_file()}}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"annotation_version": manifest["annotation_version"],
                      "unique_observations": len(annotations), "identity_relation_rows": len(identity_relations),
                      "output": str(OUT)}, indent=2))


if __name__ == "__main__":
    main()
