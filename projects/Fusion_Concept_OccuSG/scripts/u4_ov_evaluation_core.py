"""Small, testable helpers for the offline U4-OV evaluator."""
from __future__ import annotations

from collections import Counter
from typing import Any


def _is_confirmed(row: dict[str, Any]) -> bool:
    return (row.get("status") == "confirmed" and bool(row.get("reviewer"))
            and bool(row.get("review_date")) and bool(row.get("decision"))
            and bool(row.get("evaluator_category_candidate")))


def confirmed_label_mapping(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Return only explicitly reviewed fused-label -> evaluator-category mappings."""
    result: dict[str, str] = {}
    for row in rows:
        if not _is_confirmed(row):
            continue
        label = row["fused_semantic_label"]
        category = row["evaluator_category_candidate"]
        if label in result and result[label] != category:
            raise ValueError(f"conflicting confirmed mappings for {label!r}")
        result[label] = category
    return result


def mapped_class_metrics(records: list[dict[str, Any]], mapping_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score mapped classes; unmatched GT is excluded, unsupported labels abstain.

    Each record needs `match_status`, `gt_category`, and `predicted_label`.
    The denominator contains matched GT whose category is represented by at
    least one confirmed fused-label mapping. Unsupported predictions count as
    abstentions for coverage and false negatives for recall.
    """
    mapping = confirmed_label_mapping(mapping_rows)
    supported_categories = set(mapping.values())
    counts = Counter()
    tp = Counter(); fp = Counter(); fn = Counter()
    per_class: dict[str, Counter] = {c: Counter() for c in sorted(supported_categories)}
    eligible: list[tuple[str, str | None, str]] = []
    for record in records:
        if record.get("match_status") != "matched" or not record.get("gt_category"):
            counts["excluded_unmatched_or_uncertain_gt"] += 1
            continue
        truth = str(record["gt_category"])
        if truth not in supported_categories:
            counts["excluded_gt_category_without_confirmed_mapping"] += 1
            continue
        raw_prediction = record.get("predicted_label")
        pred_category = mapping.get(raw_prediction) if raw_prediction is not None else None
        if pred_category is None:
            counts["abstention_or_unsupported"] += 1
        else:
            counts["mapped_prediction"] += 1
        eligible.append((truth, pred_category, str(raw_prediction) if raw_prediction is not None else ""))
        per_class[truth]["support"] += 1
        if pred_category is None:
            fn[truth] += 1
        elif pred_category == truth:
            tp[truth] += 1
            per_class[truth]["correct"] += 1
        else:
            fp[pred_category] += 1
            fn[truth] += 1
            per_class[truth]["incorrect"] += 1

    denominator = len(eligible)
    correct = sum(tp.values())
    predicted_mapped = sum(1 for _, pred, _ in eligible if pred is not None)
    total_fp = sum(fp.values())
    total_fn = sum(fn.values())
    precision = correct / predicted_mapped if predicted_mapped else None
    recall = correct / denominator if denominator else None
    f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and precision + recall else (0.0 if denominator else None)
    scored_classes = [c for c in supported_categories if per_class[c]["support"]]
    per_class_result = {}
    for category in sorted(scored_classes):
        pden = tp[category] + fp[category]
        rden = tp[category] + fn[category]
        p = tp[category] / pden if pden else None
        r = tp[category] / rden if rden else None
        per_class_result[category] = {"support": per_class[category]["support"],
            "tp": tp[category], "fp": fp[category], "fn": fn[category],
            "precision": p, "recall": r,
            "f1": 2*p*r/(p+r) if p is not None and r is not None and p+r else (0.0 if rden else None)}
    return {
        "status": "available" if denominator else "unavailable_no_eligible_confirmed_mapping_truths",
        "confirmed_mapping_count": len(mapping),
        "denominator": denominator,
        "correct": correct,
        "incorrect_or_abstained": denominator - correct,
        "mapped_prediction_count": predicted_mapped,
        "abstention_or_unsupported_count": denominator - predicted_mapped,
        "coverage": predicted_mapped / denominator if denominator else None,
        "accuracy": correct / denominator if denominator else None,
        "precision_micro": precision,
        "recall_micro": recall,
        "f1_micro": f1,
        "tp_micro": correct,
        "fp_micro": total_fp,
        "fn_micro": total_fn,
        "excluded_counts": {k: v for k, v in sorted(counts.items())},
        "per_class": per_class_result,
        "protocol": "matched GT with a category represented by confirmed fused-label mapping; unsupported/unmapped/conflicted/unknown prediction is abstention; abstentions contribute FN and denominator; unmatched/uncertain GT and unsupported GT categories are excluded and counted",
    }


def taxonomy_relation(predicted: str | None, reviewed: str | None,
                      parent_by_child: dict[str, str], confirmed_edges: set[tuple[str, str]] | None = None) -> str:
    """Classify label granularity without treating proposed edges as approved."""
    if not predicted or not reviewed:
        return "unavailable"
    # Detector identifiers commonly use underscores; this is a lexical
    # normalization only and does not equate general/specific categories.
    predicted = predicted.replace("_", " ")
    reviewed = reviewed.replace("_", " ")
    if predicted == reviewed:
        return "exact"
    confirmed_edges = confirmed_edges or set()

    def ancestors(label: str) -> set[str]:
        result: set[str] = set()
        while label in parent_by_child:
            parent = parent_by_child[label]
            if parent in result:
                raise ValueError("taxonomy hierarchy contains a cycle")
            result.add(parent)
            label = parent
        return result

    proposed_parent = ancestors(predicted)
    if reviewed in proposed_parent:
        node = predicted
        path_confirmed = True
        while node != reviewed:
            parent = parent_by_child[node]
            path_confirmed = path_confirmed and (node, parent) in confirmed_edges
            node = parent
        if path_confirmed:
            return "confirmed_specific_to_general_match"
        return "prediction_is_specific_to_reviewed_general_label_pending_approval"
    reviewed_ancestors = ancestors(reviewed)
    if predicted in reviewed_ancestors:
        node = reviewed
        path_confirmed = True
        while node != predicted:
            parent = parent_by_child[node]
            path_confirmed = path_confirmed and (node, parent) in confirmed_edges
            node = parent
        if path_confirmed:
            return "confirmed_general_to_specific_relation"
        return "prediction_is_general_to_reviewed_specific_label_pending_approval"
    if any(parent in proposed_parent for parent in reviewed_ancestors):
        return "related_by_proposed_taxonomy_pending_approval"
    return "unrelated_or_conflicting"


def causal_prefix_view_ids(ordered_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """At each row, return only earliest observations in known view bins so far."""
    selected_by_track: dict[str, dict[str, str]] = {}
    result: dict[str, list[str]] = {}
    for row in ordered_rows:
        observation_id = row["observation_id"]
        track_id = row.get("u3_geometry_only_track_id")
        if not track_id:
            result[observation_id] = []
            continue
        bins = selected_by_track.setdefault(track_id, {})
        view_bin = row.get("view_bin")
        if not row.get("duplicate_alias") and view_bin and view_bin != "unknown" and view_bin not in bins:
            bins[view_bin] = observation_id
        result[observation_id] = list(bins.values())
    return result
