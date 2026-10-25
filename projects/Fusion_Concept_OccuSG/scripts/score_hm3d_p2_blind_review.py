#!/usr/bin/env python3
"""Score a returned HM3D P2 blind review against locked C2/D1 assignments.

Model-free and artifact-only. Incomplete/uncertain human fields are preserved
and excluded from metrics that require them; no inference is made to fill them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IDENTITY_STATUSES = {"confirmed", "uncertain", "unreviewed"}
CLASS_STATUSES = {"confirmed", "uncertain", "unreviewed"}
MASK_VALUES = {"pure_single_object", "mixed_multiple_objects", "invalid_or_misaligned", "uncertain", "unreviewed"}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ratio(a: int, b: int):
    return (a / b) if b else None


def pairwise_metrics(ids: list[str], token_by_id: dict[str, str], frame_by_id: dict[str, int], assignment_by_id: dict[str, str | None]) -> dict:
    tp = fp = fn = tn = 0
    compared = 0
    for left, right in itertools.combinations(ids, 2):
        if frame_by_id[left] == frame_by_id[right]:
            continue
        compared += 1
        gt_same = token_by_id[left] == token_by_id[right]
        a, b = assignment_by_id.get(left), assignment_by_id.get(right)
        pred_same = a is not None and b is not None and a == b
        if gt_same and pred_same:
            tp += 1
        elif gt_same:
            fn += 1
        elif pred_same:
            fp += 1
        else:
            tn += 1
    return {
        "population": "pair of identity-confirmed observations in different frames; same-frame pairs are excluded and reported separately",
        "n_cross_frame_pairs": compared,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--response", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    package = args.package.resolve()
    response_path = args.response.resolve()
    out = args.output if args.output.is_absolute() else ROOT / args.output
    out = out.resolve()
    if out.exists():
        raise SystemExit(f"Refusing to overwrite annotation/scoring version: {out}")
    if ROOT not in out.parents:
        raise SystemExit("Output must be a new directory inside this workspace.")

    pman_path = package / "package_manifest.json"
    pop_path = package / "population_manifest.json"
    if not pman_path.is_file() or not pop_path.is_file() or not response_path.is_file():
        raise SystemExit("Package manifest, population manifest, or review response is missing.")
    pman = json.loads(pman_path.read_text(encoding="utf-8"))
    population = json.loads(pop_path.read_text(encoding="utf-8"))
    response = json.loads(response_path.read_text(encoding="utf-8"))
    p2row = json.loads((package / "population_manifest.json").read_text(encoding="utf-8"))
    if pman.get("status") != "ready_for_human_review" or population.get("status") != "review_package_ready_pending_human_annotations":
        raise SystemExit("The selected review package is not the expected completed package.")
    if response.get("schema") != "hm3d-p2-blind-review-response/v1":
        raise SystemExit(f"Unsupported response schema: {response.get('schema')}")
    if response.get("run_id") != population["run_ids"]["d1"]:
        raise SystemExit("Response D1 run ID does not match review population.")
    population_hash = sha(pop_path)
    if response.get("population_manifest_sha256") != population_hash:
        raise SystemExit("Response population-manifest hash mismatch.")

    expected_ids = [row["observation_id"] for row in population["observations"]]
    returned_rows = response.get("observations", [])
    returned_ids = [row.get("observation_id") for row in returned_rows]
    if len(returned_ids) != len(expected_ids) or len(set(returned_ids)) != len(returned_ids) or set(returned_ids) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(returned_ids))
        extra = sorted(set(returned_ids) - set(expected_ids))
        raise SystemExit(f"Response population does not match exactly 25 IDs; missing={missing}; extra={extra}")
    response_by_id = {row["observation_id"]: row for row in returned_rows}

    c2_id, d1_id = population["run_ids"]["c2"], population["run_ids"]["d1"]
    c2 = ROOT / "artifacts/conceptgraphs_hybrid_runs" / c2_id
    d1 = ROOT / "artifacts/live_processing_runs" / d1_id
    diag_path = d1 / "object_mapping/diagnostic/diagnostic_manifest.json"
    p2_index_path = c2 / "detection_2d/detection_2d_index.json"
    diag = json.loads(diag_path.read_text(encoding="utf-8"))
    p2 = json.loads(p2_index_path.read_text(encoding="utf-8"))

    frames = {row["observation_id"]: int(row["frame_index"]) for row in population["observations"]}
    raw_class = {det["detection_id"]: det["class_raw"] for frame in p2["frames"] for det in frame.get("detections", [])}
    hypothesis_by_id: dict[str, str] = {}
    disposition_by_id: dict[str, dict] = {}
    final_by_id: dict[str, str | None] = {}
    object_by_hyp = {}
    for obj in diag["objects"]:
        hid = obj["hypothesis_id"]
        object_by_hyp[hid] = obj
        for oid in obj["observation_ids"]:
            if oid in hypothesis_by_id:
                raise SystemExit(f"Observation assigned to more than one online hypothesis: {oid}")
            hypothesis_by_id[oid] = hid
            disposition_by_id[oid] = obj
            keys = obj.get("final_object_keys", [])
            final_by_id[oid] = keys[0] if obj.get("status_after_filter") == "retained" and len(keys) == 1 else None
    if set(hypothesis_by_id) != set(expected_ids):
        raise SystemExit("D1 diagnostic assignments do not cover exactly the P2 review population.")

    errors = []
    identity_status_counts = Counter()
    class_status_counts = Counter()
    mask_counts = Counter()
    identity_token_by_id: dict[str, str] = {}
    valid_class_by_id: dict[str, str] = {}
    for oid in expected_ids:
        row = response_by_id[oid]
        istatus = row.get("identity_status", "unreviewed")
        cstatus = row.get("class_status", "unreviewed")
        purity = row.get("mask_purity", "unreviewed")
        token = str(row.get("identity_token") or "").strip()
        label = str(row.get("class_label") or "").strip()
        if istatus not in IDENTITY_STATUSES:
            errors.append({"observation_id": oid, "field": "identity_status", "value": istatus, "severity": "invalid"})
            istatus = "unreviewed"
        if cstatus not in CLASS_STATUSES:
            errors.append({"observation_id": oid, "field": "class_status", "value": cstatus, "severity": "invalid"})
            cstatus = "unreviewed"
        if purity not in MASK_VALUES:
            errors.append({"observation_id": oid, "field": "mask_purity", "value": purity, "severity": "invalid"})
            purity = "unreviewed"
        identity_status_counts[istatus] += 1
        class_status_counts[cstatus] += 1
        mask_counts[purity] += 1
        if istatus == "confirmed" and token:
            identity_token_by_id[oid] = token
        elif istatus == "confirmed" and not token:
            errors.append({"observation_id": oid, "field": "identity_token", "severity": "incomplete", "detail": "status confirmed but token is empty; excluded from identity metrics"})
        elif istatus != "confirmed" and token:
            errors.append({"observation_id": oid, "field": "identity_token", "severity": "not_scored", "detail": f"token is present while identity_status={istatus}; token excluded from identity metrics"})
        if cstatus == "confirmed" and label:
            valid_class_by_id[oid] = label
        elif cstatus == "confirmed" and not label:
            errors.append({"observation_id": oid, "field": "class_label", "severity": "incomplete", "detail": "status confirmed but class label is empty; excluded from class metrics"})
        elif cstatus != "confirmed" and label:
            errors.append({"observation_id": oid, "field": "class_label", "severity": "not_scored", "detail": f"label is present while class_status={cstatus}; label excluded from class metrics"})

    token_counts = Counter(identity_token_by_id.values())
    identity_to_obs: dict[str, list[str]] = defaultdict(list)
    for oid, token in identity_token_by_id.items():
        identity_to_obs[token].append(oid)

    assignment_online = {oid: hypothesis_by_id[oid] for oid in identity_token_by_id}
    assignment_final = {oid: final_by_id[oid] for oid in identity_token_by_id}
    pairwise_online = pairwise_metrics(list(identity_token_by_id), identity_token_by_id, frames, assignment_online)
    pairwise_final = pairwise_metrics(list(identity_token_by_id), identity_token_by_id, frames, assignment_final)

    same_frame_rows = []
    sf_correct = sf_evaluable = sf_same_identity = sf_different_identity = 0
    for pair in population["population"]["same_frame_pairs"]:
        left, right = pair["observation_ids"]
        valid = left in identity_token_by_id and right in identity_token_by_id
        row = {
            "frame_index": pair["frame_index"],
            "observation_ids": [left, right],
            "identity_evaluable": valid,
            "human_same_identity": None,
            "online_same_hypothesis": hypothesis_by_id[left] == hypothesis_by_id[right],
            "final_same_object": final_by_id[left] is not None and final_by_id[left] == final_by_id[right],
        }
        if valid:
            same = identity_token_by_id[left] == identity_token_by_id[right]
            row["human_same_identity"] = same
            sf_evaluable += 1
            sf_same_identity += int(same)
            sf_different_identity += int(not same)
            sf_correct += int(row["online_same_hypothesis"] == same)
        same_frame_rows.append(row)

    false_merge_hypotheses = []
    for hid, obj in object_by_hyp.items():
        tokens = sorted({identity_token_by_id[oid] for oid in obj["observation_ids"] if oid in identity_token_by_id})
        if len(tokens) > 1:
            false_merge_hypotheses.append({"hypothesis_id": hid, "confirmed_identity_tokens": tokens,
                                           "observation_ids": [oid for oid in obj["observation_ids"] if oid in identity_token_by_id]})

    identity_rows = []
    for token, members in sorted(identity_to_obs.items()):
        hyps = sorted({hypothesis_by_id[oid] for oid in members})
        retained_members = [oid for oid in members if disposition_by_id[oid].get("status_after_filter") == "retained"]
        identity_rows.append({
            "identity_token": token,
            "confirmed_observation_count": len(members),
            "observation_ids": members,
            "online_hypothesis_ids": hyps,
            "online_fragmented": len(hyps) > 1,
            "retained_observation_ids": retained_members,
            "identity_presence_after_filter": bool(retained_members),
            "all_confirmed_observations_retained": len(retained_members) == len(members),
            "retention_state": "lost" if not retained_members else "fully_retained" if len(retained_members) == len(members) else "partially_retained",
        })

    class_rows = []
    class_agree = 0
    for oid, reviewed_label in valid_class_by_id.items():
        predicted_raw = raw_class[oid]
        exact = predicted_raw.casefold() == reviewed_label.casefold()
        class_agree += int(exact)
        obj = disposition_by_id[oid]
        class_rows.append({
            "observation_id": oid,
            "predicted_raw_class": predicted_raw,
            "reviewed_class": reviewed_label,
            "exact_string_match": exact,
            "mask_purity": response_by_id[oid].get("mask_purity"),
            "online_hypothesis_id": hypothesis_by_id[oid],
            "post_filter_status": obj.get("status_after_filter"),
            "post_filter_reason": obj.get("filter_reason"),
        })

    singleton_rows = []
    for obj in diag["objects"]:
        if int(obj.get("detection_count", 0)) != 1:
            continue
        oid = obj["observation_ids"][0]
        row = response_by_id[oid]
        token = identity_token_by_id.get(oid)
        purity = row.get("mask_purity", "unreviewed")
        if token and token_counts[token] > 1:
            category = "fragment_of_repeated_confirmed_identity"
        elif token and purity == "pure_single_object":
            category = "confirmed_one_detection_identity_pure_mask"
        elif token and purity in {"mixed_multiple_objects", "invalid_or_misaligned"}:
            category = "confirmed_identity_with_mixed_or_invalid_mask"
        elif row.get("identity_status") == "uncertain":
            category = "uncertain_identity"
        elif row.get("identity_status") == "unreviewed":
            category = "unreviewed_identity"
        elif row.get("identity_status") == "confirmed" and not token:
            category = "incomplete_confirmed_identity_token"
        else:
            category = "unclassified_or_uncertain_mask"
        singleton_rows.append({
            "observation_id": oid,
            "identity_status": row.get("identity_status"),
            "identity_token_used_for_metrics": token,
            "class_status": row.get("class_status"),
            "reviewed_class_if_confirmed": valid_class_by_id.get(oid),
            "mask_purity": purity,
            "predicted_raw_class": raw_class[oid],
            "review_category": category,
            "post_filter_status": obj.get("status_after_filter"),
            "filter_reason": obj.get("filter_reason"),
        })

    valid_identity_n = len(identity_token_by_id)
    retained_valid_identity_n = sum(disposition_by_id[oid].get("status_after_filter") == "retained" for oid in identity_token_by_id)
    final_retained_detections = sum(obj.get("status_after_filter") == "retained" for obj in diag["objects"] for _ in obj["observation_ids"])
    identity_retention_counts = Counter(row["retention_state"] for row in identity_rows)
    diagnostics = {
        "schema": "hm3d-p2-human-review-quality-score/v1",
        "status": "scored_with_explicit_incomplete_annotations",
        "scope": "Only this 25-observation HM3D 00802 P2 population and user-confirmed identities/classes; no scene-wide or detector-recall claim.",
        "run_ids": population["run_ids"],
        "scene_id": population["scene_id"],
        "sequence_id": population["sequence_id"],
        "input_hashes": {
            "review_response_sha256": sha(response_path),
            "population_manifest_sha256": population_hash,
            "d1_diagnostic_manifest_sha256": sha(diag_path),
            "p2_index_sha256": sha(p2_index_path),
        },
        "population": {
            "p2_observations": 25,
            "detections_in_frames": population["population"]["frame_count_with_detections"],
            "same_frame_pairs_total": len(population["population"]["same_frame_pairs"]),
            "annotation_identity_status_counts": dict(identity_status_counts),
            "usable_identity_observations": valid_identity_n,
            "confirmed_unique_identity_tokens": len(identity_to_obs),
            "identity_annotation_issues": errors,
            "class_status_counts": dict(class_status_counts),
            "usable_confirmed_class_observations": len(valid_class_by_id),
            "mask_purity_counts": dict(mask_counts),
        },
        "locked_mapping": {
            "online_hypotheses": int(diag["counts"]["before_filter"]),
            "retained_hypotheses": int(diag["counts"]["retained_after_filter"]),
            "filtered_hypotheses": int(diag["counts"]["filtered"]),
            "post_filter_merge_or_repartition_count": int(diag["counts"]["merged_or_repartitioned_after_filter"]),
            "retained_detections": int(final_retained_detections),
            "total_p2_detections": 25,
            "confirmed_identity_observations_retained": int(retained_valid_identity_n),
            "confirmed_identity_observations_total": valid_identity_n,
            "confirmed_identity_observation_retention": ratio(int(retained_valid_identity_n), valid_identity_n),
            "identities_with_any_observation_retained": int(identity_retention_counts["fully_retained"] + identity_retention_counts["partially_retained"]),
            "identities_fully_retained": int(identity_retention_counts["fully_retained"]),
            "identities_partially_retained": int(identity_retention_counts["partially_retained"]),
            "identities_lost": int(identity_retention_counts["lost"]),
            "confirmed_identity_count_denominator": len(identity_rows),
        },
        "association": {
            "online_prefix_hypotheses": pairwise_online,
            "final_retained_graph": pairwise_final,
            "false_merge_hypothesis_count_from_confirmed_tokens": len(false_merge_hypotheses),
            "false_merge_hypotheses": false_merge_hypotheses,
            "fragmented_confirmed_identities": [r for r in identity_rows if r["online_fragmented"]],
            "identity_retention_by_token": identity_rows,
            "same_frame_pairs": {
                "total": len(same_frame_rows),
                "evaluable": sf_evaluable,
                "unevaluable": len(same_frame_rows) - sf_evaluable,
                "same_physical_identity": sf_same_identity,
                "different_physical_identities": sf_different_identity,
                "online_assignment_consistent": sf_correct,
                "online_consistency_rate": ratio(sf_correct, sf_evaluable),
                "rows": same_frame_rows,
            },
        },
        "class": {
            "contract": "Exact case-insensitive comparison of P2 raw detector string to human-confirmed class string. No alias/canonical mapping was applied.",
            "confirmed_n": len(class_rows),
            "exact_string_matches": class_agree,
            "exact_string_mismatches": len(class_rows) - class_agree,
            "exact_string_agreement": ratio(class_agree, len(class_rows)),
            "rows": class_rows,
            "interpretation_limit": "An exact-string mismatch is not automatically a taxonomy error or a predictor error; no subtype/synonym relation was inferred or approved in this scoring pass.",
        },
        "mask_purity": {
            "counts": dict(mask_counts),
            "interpretation": "These are independent human judgments, not inferred from identity or class. Mixed masks remain visible in class/identity tables.",
        },
        "filtered_singleton_review": {
            "hypothesis_count": len(singleton_rows),
            "categories": dict(Counter(row["review_category"] for row in singleton_rows)),
            "rows": singleton_rows,
            "caveat": "A confirmed one-detection identity means one confirmed observation in this reviewed population; uncertain/unreviewed rows may hide additional observations of that object.",
        },
        "room_semantics": {"status": "not_run", "reason": "out of scope for this review response; no room scoring or assumed scope"},
        "next_experiment_proposal": {
            "title": "Fixed-configuration repeat-observation continuation on HM3D 00802",
            "status": "proposal_only_not_run",
            "evidence": "Nine filtered singleton hypotheses have confirmed identities and pure single-object masks; one more confirmed singleton has a mixed mask; one repeated armchair identity is split across two hypotheses and its frame-9 observation is filtered. Ten of twelve confirmed identities have no retained observation.",
            "design": "Prepare one short route extension from navmesh/free-space geometry before inference, with a generic revisit of the traversed areas from different positions/headings. Keep detector, resolution, seed, matcher, thresholds, obj_min_detections=3, admission, and geometry processing fixed. Run one fresh C2 capture/perception and one D1 mapping; manually review the new P2 population before comparing identity association and retention.",
            "route_selection_guard": "Choose the route from navigable geometry and coverage only; do not use predicted classes, object identities, room labels, or GT semantic annotations to choose viewpoints.",
            "purpose": "Test whether the currently observed single-detection identities gain repeated evidence and become retained under the unchanged filter, without threshold tuning.",
            "limitations": "A new route changes the observation population, so this is a development follow-up, not a controlled ablation of route length or a general quality claim. Room membership/semantics remain out of scope while elevation scope is unavailable.",
            "budget": "Not executed; obtain a route-specific preflight and runtime estimate before requesting approval.",
        },
    }

    out.mkdir(parents=True)
    ann_dir = out / "annotations"
    ann_dir.mkdir()
    response_copy = ann_dir / "review_response_v1.json"
    shutil.copyfile(response_path, response_copy)
    write_json(ann_dir / "annotation_manifest_v1.json", {
        "schema": "hm3d-p2-review-annotation-manifest/v1",
        "annotation_version": "v1",
        "response_file": "review_response_v1.json",
        "response_sha256": sha(response_copy),
        "response_source_path": str(response_path),
        "response_bytes": response_copy.stat().st_size,
        "c2_run_id": c2_id,
        "d1_run_id": d1_id,
        "population_manifest_sha256": population_hash,
        "observation_count": len(expected_ids),
        "status": "source_response_preserved_without_edits",
        "validation_issues": errors,
    })
    write_json(out / "quality_score_v1.json", diagnostics)

    with (out / "class_comparison_v1.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["observation_id", "predicted_raw_class", "reviewed_class", "exact_string_match", "mask_purity", "online_hypothesis_id", "post_filter_status", "post_filter_reason"])
        writer.writeheader()
        writer.writerows(class_rows)
    with (out / "filtered_singletons_v1.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(singleton_rows[0].keys()))
        writer.writeheader()
        writer.writerows(singleton_rows)
    with (out / "same_frame_pairs_v1.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["frame_index", "observation_ids", "identity_evaluable", "human_same_identity", "online_same_hypothesis", "final_same_object"])
        writer.writeheader()
        writer.writerows({**row, "observation_ids": " | ".join(row["observation_ids"])} for row in same_frame_rows)

    online = pairwise_online
    final = pairwise_final
    md = [
        "# HM3D 00802 P2 review scoring — v1",
        "",
        f"**Scope:** C2 `{c2_id}` + D1 `{d1_id}`; exactly 25 P2 observations from one development route. This is not detector recall, room coverage, or a general quality estimate.",
        "",
        "## Annotation validity",
        "",
        f"- Identity fields usable for scoring: {valid_identity_n}/25 observations, representing {len(identity_to_obs)} confirmed identity tokens.",
        f"- Identity status counts: `{dict(identity_status_counts)}`. Two rows were marked confirmed but had no identity token; one unreviewed row had a token. Those three entries were not silently corrected or scored as confirmed.",
        f"- Class fields usable: {len(class_rows)}/25 confirmed; `{dict(class_status_counts)}`. Two labels were typed while class status remained unreviewed, so they were excluded.",
        f"- Mask judgments: `{dict(mask_counts)}`. Identity, class, and mask purity are kept separate.",
        "",
        "## Association and retention",
        "",
        f"- P2 detections: 25; online hypotheses: {diag['counts']['before_filter']}; after filter: {diag['counts']['retained_after_filter']} retained / {diag['counts']['filtered']} filtered; post-filter merge/repartition: {diag['counts']['merged_or_repartitioned_after_filter']}. Final graph contains {final_retained_detections}/25 detections.",
        f"- Cross-frame pairwise, online: TP={online['tp']}, FP={online['fp']}, FN={online['fn']}, TN={online['tn']} over {online['n_cross_frame_pairs']} reviewed cross-frame pairs; precision={online['precision']:.3f}, recall={online['recall']:.3f}, F1={online['f1']:.3f}.",
        f"- Final retained graph has the same pairwise counts: TP={final['tp']}, FP={final['fp']}, FN={final['fn']}, TN={final['tn']}; this does not capture the loss of identities with no retained observation, so read it with retention below.",
        f"- Same-frame pairs: {len(same_frame_rows)} total; {sf_evaluable} identity-evaluable, {len(same_frame_rows)-sf_evaluable} unevaluable. Among evaluable pairs, {sf_same_identity} same-identity and {sf_different_identity} different-identity; online assignment was consistent for {sf_correct}/{sf_evaluable}.",
        f"- Confirmed identity observations retained: {retained_valid_identity_n}/{valid_identity_n}. Of {len(identity_rows)} confirmed tokens: {identity_retention_counts['fully_retained']} fully retained, {identity_retention_counts['partially_retained']} partially retained, {identity_retention_counts['lost']} with no retained observation.",
        "- Fragmentation: `armchair_1` is split across two online hypotheses. Its frame-9 observation is filtered; four later confirmed observations remain in one retained hypothesis. `mirror_1` has five observations in one retained hypothesis.",
        f"- False-merge hypotheses supported by confirmed identity tokens: {len(false_merge_hypotheses)}. This is limited by the incomplete/uncertain identity rows listed above.",
        "",
        "### Filtered singleton hypotheses",
        "",
        f"There are {len(singleton_rows)} singleton hypotheses; all were filtered by `detection_count=1 < obj_min_detections=3`. Review-based categories: `{dict(Counter(row['review_category'] for row in singleton_rows))}`. The nine pure-mask one-detection identities are confirmed only within this 25-observation population. One additional confirmed one-detection identity has a mixed mask; one filtered observation is a fragment of the repeated armchair identity. Remaining cases are incomplete, uncertain, or unreviewed.",
        "",
        "## Class and mask purity",
        "",
        f"- Exact raw-string comparison: {class_agree}/{len(class_rows)} match; {len(class_rows)-class_agree}/{len(class_rows)} mismatch. No synonym or taxonomy mapping was applied. Exact mismatch does not by itself establish a taxonomy error or a predictor error.",
        "- See `class_comparison_v1.csv` for every reviewed class and raw detector label; see `filtered_singletons_v1.csv` for per-singleton status/reason.",
        "",
        "## Room semantics and scope",
        "",
        "Room scoring was not run. No assumed scope was enabled; membership and room labels remain unavailable. No model, capture, mapping, native replay, tuning, or min-detections change occurred.",
        "",
        "## One next experiment (proposal only)",
        "",
        "Prepare one short HM3D 00802 route continuation selected only from navmesh/free-space geometry, then run one fresh C2 + one D1 with the current locked settings, including `obj_min_detections=3`. The review found nine filtered singleton identities with pure masks, one mixed-mask singleton, and one repeated `armchair_1` identity split across tracks; ten of twelve confirmed identities have no retained observation. This experiment would test whether additional observations let those identities accumulate evidence under the unchanged filter. It is a proposal only; no route, capture, perception, or mapping was run. Route-specific preflight and budget approval remain required.",
    ]
    (out / "quality_report_v1.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    files = []
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p.name != "scoring_manifest_v1.json"):
        files.append({"path": path.relative_to(out).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)})
    write_json(out / "scoring_manifest_v1.json", {
        "schema": "hm3d-p2-review-scoring-manifest/v1",
        "status": "completed_with_annotation_incompleteness_disclosed",
        "annotation_version": "v1",
        "response_sha256": sha(response_copy),
        "population_manifest_sha256": population_hash,
        "quality_score_sha256": sha(out / "quality_score_v1.json"),
        "file_count": len(files),
        "files": files,
    })
    print(json.dumps({"status": "completed_with_annotation_incompleteness_disclosed", "output": str(out),
                      "response_sha256": sha(response_copy), "quality_score_sha256": sha(out / "quality_score_v1.json"),
                      "package_scoring_manifest_sha256": sha(out / "scoring_manifest_v1.json"),
                      "usable_identity_observations": valid_identity_n, "class_exact_matches": class_agree,
                      "class_exact_total": len(class_rows), "cross_frame_pairwise": online}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
