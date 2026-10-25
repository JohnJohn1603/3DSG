#!/usr/bin/env python3
"""Import and inspect the RB2 A-table package without Qwen/Transformers.

This is deliberately a post-run, read-only scorer.  It validates the four
tables and their raw-token provenance, then produces a small HTML/SVG report
and an RB1-controller-backed static diagnostic over the existing readiness
artifact. It never changes the A tables, the readiness cache, room geometry,
or the predictor.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping


EXPECTED_ARCHIVE_SHA256 = "0e30cc12058ec22372fe8b2df49279ec6a917027bef74182f8831374a7418997"
VARIANTS = ("mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral")
ALIASES = {
    "couch": "sofa",
    "dining table": "dining_table",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def read_json(zf: zipfile.ZipFile, name: str) -> Any:
    return json.loads(zf.read(name).decode("utf-8"))


def canonical_category(value: str) -> str:
    key = re.sub(r"\s+", " ", value.strip().lower())
    return ALIASES.get(key, key.replace(" ", "_"))


def validate_tables(zf: zipfile.ZipFile, prefix: str, categories: list[str], rooms: list[str]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    errors: list[str] = []
    tables: dict[str, list[dict[str, Any]]] = {}
    expected_keys = {(c, r) for c in categories for r in rooms}
    for variant in VARIANTS:
        name = f"{prefix}/full_a/a_table_{variant}.csv"
        rows: list[dict[str, Any]] = []
        with zf.open(name) as raw:
            for row in csv.DictReader((line.decode("utf-8") for line in raw)):
                for field in ("raw_variant_score", "uncalibrated_normalized_score", "A_log_ratio_uncalibrated"):
                    try:
                        row[field] = float(row[field])
                    except (KeyError, TypeError, ValueError):
                        errors.append(f"{name}: non-numeric {field}")
                        continue
                    if not finite(row[field]):
                        errors.append(f"{name}: non-finite {field}")
                rows.append(row)
        keys = {(row.get("category"), row.get("room_id")) for row in rows}
        if len(rows) != 88 or keys != expected_keys:
            errors.append(f"{name}: expected exactly 88 unique category/room rows, got {len(rows)}")
        if any(row.get("variant") != variant for row in rows):
            errors.append(f"{name}: variant column mismatch")
        tables[variant] = rows
    return tables, errors


def validate_raw_contract(zf: zipfile.ZipFile, prefix: str, categories: list[str], rooms: list[str], epsilon: float) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    raw = read_json(zf, f"{prefix}/full_a/raw_token_scores.json")
    neutral = read_json(zf, f"{prefix}/full_a/neutral_token_scores.json")
    raw_by_key = {(row["category"], row["room_id"]): row for row in raw}
    if len(raw_by_key) != 88:
        errors.append(f"raw_token_scores: expected 88 unique pairs, got {len(raw_by_key)}")
    if set(neutral) != set(rooms):
        errors.append("neutral_token_scores: room IDs do not match ontology")
    max_formula_delta = 0.0
    max_softmax_delta = 0.0
    max_a_delta = 0.0
    display_strings: dict[str, str] = {}
    for category in categories:
        category_rows = [raw_by_key[(category, room)] for room in rooms]
        category_scores: dict[str, float] = {}
        for row in category_rows:
            cp = row["category_prefix"]
            nprefix = row["neutral_prefix"]
            if cp.get("completion") != row.get("completion"):
                errors.append(f"raw completion mismatch for {category}/{row['room_id']}")
            display_strings[category] = cp.get("prefix", "").split("Observed object: ", 1)[-1].split(".\nRoom type:", 1)[0]
            for section_name, section in (("category_prefix", cp), ("neutral_prefix", nprefix)):
                values = section.get("continuation_logprobs", [])
                if not values or not all(finite(v) for v in values):
                    errors.append(f"non-finite token log-probs for {category}/{row['room_id']}/{section_name}")
                mean = sum(float(v) for v in values) / len(values)
                summ = sum(float(v) for v in values)
                max_formula_delta = max(max_formula_delta, abs(mean - float(section.get("mean_logprob"))))
                max_formula_delta = max(max_formula_delta, abs(summ - float(section.get("sum_logprob"))))
            category_scores[row["room_id"]] = float(cp["mean_logprob"])
            expected_mean_neutral = float(cp["mean_logprob"]) - float(nprefix["mean_logprob"])
            expected_sum_neutral = float(cp["sum_logprob"]) - float(nprefix["sum_logprob"])
            row_scores = {"mean_no_neutral": float(cp["mean_logprob"]), "mean_neutral": expected_mean_neutral,
                          "sum_no_neutral": float(cp["sum_logprob"]), "sum_neutral": expected_sum_neutral}
            # The CSV is checked against raw provenance later by the caller.
            row["_expected_variants"] = row_scores
        max_val = max(category_scores.values())
        exps = {room: math.exp(score - max_val) for room, score in category_scores.items()}
        denom = sum(exps.values())
        expected = {room: value / denom for room, value in exps.items()}
        # The A table stores the mean/sum variants, so this is only a compact
        # contract check for one variant here; all variants are checked below.
        _ = expected
    return {"raw": raw, "neutral": neutral, "display_strings": display_strings,
            "max_token_aggregation_delta": max_formula_delta,
            "max_softmax_delta": max_softmax_delta, "max_a_log_ratio_delta": max_a_delta}, errors


def compare_contract(tables: dict[str, list[dict[str, Any]]], raw_info: dict[str, Any], rooms: list[str], temperature: float, epsilon: float) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    raw_by_key = {(row["category"], row["room_id"]): row for row in raw_info["raw"]}
    checks = {"max_token_aggregation_delta": raw_info["max_token_aggregation_delta"],
              "max_neutral_subtraction_delta": 0.0, "max_normalization_delta": 0.0,
              "max_a_log_ratio_delta": 0.0, "prompt_display_strings": raw_info["display_strings"]}
    for variant, rows in tables.items():
        by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_category[row["category"]].append(row)
            source = raw_by_key[(row["category"], row["room_id"])]
            expected = source["_expected_variants"][variant]
            delta = abs(float(row["raw_variant_score"]) - expected)
            if variant.endswith("neutral"):
                checks["max_neutral_subtraction_delta"] = max(checks["max_neutral_subtraction_delta"], delta)
            else:
                checks["max_neutral_subtraction_delta"] = max(checks["max_neutral_subtraction_delta"], delta)
        for category, group in by_category.items():
            values = {row["room_id"]: float(row["raw_variant_score"]) for row in group}
            max_val = max(values.values())
            exp_values = {room: math.exp((value - max_val) / temperature) for room, value in values.items()}
            denom = sum(exp_values.values())
            dist = {room: value / denom for room, value in exp_values.items()}
            for row in group:
                room = row["room_id"]
                checks["max_normalization_delta"] = max(checks["max_normalization_delta"], abs(float(row["uncalibrated_normalized_score"]) - dist[room]))
                prior = 1.0 / len(rooms)
                expected_a = math.log((dist[room] + epsilon) / (prior + epsilon))
                checks["max_a_log_ratio_delta"] = max(checks["max_a_log_ratio_delta"], abs(float(row["A_log_ratio_uncalibrated"]) - expected_a))
    tolerances = {"token_aggregation": 1e-6, "neutral_subtraction": 1e-6, "normalization": 1e-6, "a_log_ratio": 1e-6}
    if checks["max_token_aggregation_delta"] > tolerances["token_aggregation"]:
        errors.append("token mean/sum does not match stored token log-probs")
    if checks["max_neutral_subtraction_delta"] > tolerances["neutral_subtraction"]:
        errors.append("A-table raw scores do not match category-minus-neutral contract")
    if checks["max_normalization_delta"] > tolerances["normalization"]:
        errors.append("A-table normalization does not match temperature contract")
    if checks["max_a_log_ratio_delta"] > tolerances["a_log_ratio"]:
        errors.append("A-table log-ratio does not match uniform-prior contract")
    return {"checks": checks, "tolerances": tolerances, "status": "pass" if not errors else "blocked"}, errors


def summary_rows(tables: dict[str, list[dict[str, Any]]], categories: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for variant, rows in tables.items():
        by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_cat[row["category"]].append(row)
        for category in categories:
            ordered = sorted(by_cat[category], key=lambda r: float(r["uncalibrated_normalized_score"]), reverse=True)
            out.append({"variant": variant, "category": category,
                        "top1": ordered[0]["room_id"], "top1_score": float(ordered[0]["uncalibrated_normalized_score"]),
                        "top2": ordered[1]["room_id"], "top2_score": float(ordered[1]["uncalibrated_normalized_score"]),
                        "margin": float(ordered[0]["uncalibrated_normalized_score"]) - float(ordered[1]["uncalibrated_normalized_score"]),
                        "winner_count": len(ordered)})
    return out


def svg_heatmap(tables: dict[str, list[dict[str, Any]]], categories: list[str], rooms: list[str]) -> str:
    cell_w, cell_h, left, top = 120, 24, 150, 46
    panel_w = left + cell_w * len(rooms) + 20
    panel_h = top + cell_h * len(categories) + 35
    width, height = panel_w * 2, panel_h * 2
    chunks = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<style>text{font:11px sans-serif}.axis{font-weight:bold}</style>']
    for i, variant in enumerate(VARIANTS):
        rows = {row["category"]: row for row in tables[variant]}
        by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in tables[variant]: by_cat[row["category"]].append(row)
        ox, oy = (i % 2) * panel_w, (i // 2) * panel_h
        chunks.append(f'<text x="{ox + 4}" y="{oy + 18}" class="axis">{html.escape(variant)}</text>')
        for j, room in enumerate(rooms): chunks.append(f'<text x="{ox + left + j*cell_w + 2}" y="{oy + 36}" transform="rotate(-35 {ox + left + j*cell_w + 2},{oy + 36})">{html.escape(room)}</text>')
        for r, category in enumerate(categories):
            chunks.append(f'<text x="{ox + 4}" y="{oy + top + r*cell_h + 16}">{html.escape(category)}</text>')
            vals = {row["room_id"]: float(row["uncalibrated_normalized_score"]) for row in by_cat[category]}
            for c, room in enumerate(rooms):
                value = vals[room]
                color = int(max(0, min(255, round(255 * (1 - value)))));
                fill = f'rgb(255,{color},{color})'
                chunks.append(f'<rect x="{ox + left + c*cell_w}" y="{oy + top + r*cell_h}" width="{cell_w-2}" height="{cell_h-2}" fill="{fill}" stroke="#ddd"/>')
                chunks.append(f'<text x="{ox + left + c*cell_w + 3}" y="{oy + top + r*cell_h + 15}">{value:.3f}</text>')
    chunks.append('</svg>')
    return ''.join(chunks)


def _rb1_types() -> tuple[Any, ...]:
    """Load the checked-in RB1 controller without adding runtime dependencies."""
    root = Path(__file__).resolve().parents[1]
    source = root / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from standalone_sg.semantics.room_belief_v1 import RoomBeliefEvent, RoomBeliefLedger, RoomBeliefPolicy, RoomObjectEvidence
    return RoomBeliefEvent, RoomBeliefLedger, RoomBeliefPolicy, RoomObjectEvidence


def _readiness_objects(readiness_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    readiness = json.loads(readiness_path.read_text())
    objects = list(readiness.get("objects", []))
    if not objects:
        raise ValueError("readiness artifact has no object consensus records")
    return readiness, objects


def _variant_a_scores(tables: dict[str, list[dict[str, Any]]], variant: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = defaultdict(dict)
    for row in tables[variant]:
        result[row["category"]][row["room_id"]] = float(row["A_log_ratio_uncalibrated"])
    return {category: dict(values) for category, values in sorted(result.items())}


def _readiness_evidence(obj: Mapping[str, Any], *, variant: str, a_scores: Mapping[str, Mapping[str, float]],
                        RoomObjectEvidence: Any) -> tuple[Any, dict[str, Any]]:
    object_id = str(obj["hypothesis_id"])
    original_status = str(obj.get("semantic_status", "unknown"))
    ledger_status = original_status if original_status in {"unknown", "provisional", "stable", "conflicted"} else "unknown"
    raw_category = obj.get("semantic_category_raw_exact")
    category = canonical_category(raw_category) if isinstance(raw_category, str) and raw_category else None
    membership = dict(obj.get("membership") or {})
    membership_status = str(membership.get("status", "unassigned"))
    assigned_room = membership.get("room_id") if membership_status == "assigned" else None
    retained = obj.get("admission_status") == "retained"
    membership["object_id"] = object_id
    semantic_evidence = {
        "object_id": object_id, "status": ledger_status, "category": category,
        "consensus_status": original_status,
        "raw_category": raw_category,
        "category_support_by_pose_unit": dict(obj.get("semantic_category_support_by_pose_unit") or {}),
        "semantic_policy": dict(obj.get("semantic_policy") or {}),
        "source": "RB1 readiness semantic consensus output",
    }
    evidence = RoomObjectEvidence(
        object_id=object_id, revision=1, retained=retained, semantic_status=ledger_status,
        category=category, membership_status=membership_status, room_id=assigned_room,
        source_observation_ids=tuple(obj.get("semantic_observations") or ()),
        provenance=f"rb1-readiness:{variant}", membership_evidence=membership,
        semantic_evidence=semantic_evidence,
    )
    raw_history = list(obj.get("raw_class_history") or [])
    canonical_history = [canonical_category(value) for value in raw_history]
    reasons = []
    if not retained:
        reasons.append("admission_filtered")
    if membership_status != "assigned":
        reasons.append("membership_not_assigned")
    if original_status != "stable":
        reasons.append(f"semantic_status_{original_status}")
    if category not in a_scores:
        reasons.append("consensus_category_not_in_A" if category else "consensus_category_unavailable")
    return evidence, {
        "hypothesis_id": object_id, "raw_class_history": raw_history,
        "canonical_class_history": canonical_history, "consensus_category_raw": raw_category,
        "category": category, "semantic_status": original_status,
        "admission_status": obj.get("admission_status"), "membership_room_id": assigned_room,
        "detection_count": int(obj.get("detection_count", 0)),
        "distinct_frame_count": int(obj.get("distinct_frame_count", 0)),
        "distinct_pose_count": int(obj.get("distinct_pose_count_from_reference", 0)),
        "semantic_observation_ids": list(obj.get("semantic_observations") or ()),
        "consensus_support_by_pose_unit": dict(obj.get("semantic_category_support_by_pose_unit") or {}),
        "eligible": False, "reason": "excluded: " + ",".join(reasons) if reasons else "eligible",
    }


def _static_variant(readiness_objects: list[dict[str, Any]], tables: dict[str, list[dict[str, Any]]],
                   categories: list[str], variant: str, policy_config: Mapping[str, Any],
                   physical_room_ids: list[str] | None = None) -> dict[str, Any]:
    RoomBeliefEvent, RoomBeliefLedger, RoomBeliefPolicy, RoomObjectEvidence = _rb1_types()
    a_scores = _variant_a_scores(tables, variant)
    semantic_rooms = sorted({row["room_id"] for row in tables[variant]})
    physical_rooms = sorted(set(physical_room_ids or []))
    if not physical_rooms:
        # Backward-compatible fallback for old readiness artifacts without a
        # recorded membership scope. New reports always pass all candidate
        # room IDs so empty rooms remain explicit unknown/no_evidence states.
        physical_rooms = sorted({str(obj["membership"]["room_id"]) for obj in readiness_objects
                                  if (obj.get("membership") or {}).get("status") == "assigned"
                                  and (obj.get("membership") or {}).get("room_id")})
    label_policy = RoomBeliefPolicy(
        policy_id=str(policy_config["policy_id"]),
        minimum_supported_categories=int(policy_config["minimum_supported_categories"]),
        minimum_top_belief=float(policy_config["minimum_top_belief"]),
        minimum_margin=float(policy_config["minimum_margin"]),
        relabel_margin=float(policy_config["relabel_margin"]),
        relabel_evidence_revisions=int(policy_config["relabel_evidence_revisions"]),
    )
    ledger = RoomBeliefLedger(
        room_ids=semantic_rooms, room_node_ids=physical_rooms,
        room_priors={room: 1.0 for room in semantic_rooms}, category_room_scores=a_scores,
        label_policy=label_policy, evidence_policy_id="rb1-room-belief-static-v2",
        ontology_version="rb2-A-room-ontology-v1", a_table_hash=f"A:{variant}",
    )
    objects: list[dict[str, Any]] = []
    for obj in sorted(readiness_objects, key=lambda item: str(item["hypothesis_id"])):
        evidence, item = _readiness_evidence(obj, variant=variant, a_scores=a_scores,
                                              RoomObjectEvidence=RoomObjectEvidence)
        result = ledger.apply(RoomBeliefEvent(
            event_id=f"static:{variant}:{evidence.object_id}", object_id=evidence.object_id,
            producer_revision=1, evidence=evidence,
        ))
        item["ledger_apply_status"] = result["status"]
        item["eligible"] = any(evidence.object_id in ids
                                for room_members in ledger.category_members.values()
                                for ids in room_members.values())
        if item["eligible"]:
            item["reason"] = "eligible_from_consensus"
        objects.append(item)

    rooms: list[dict[str, Any]] = []
    for room_node_id in physical_rooms:
        state = ledger.room_state(room_node_id)
        category_members = ledger.category_members[room_node_id]
        eligible_categories = sorted(category_members)
        raw_scores = dict(state.raw_scores)
        beliefs = dict(state.candidates)
        ordered = list(state.candidates)
        if not eligible_categories:
            reason = "no_evidence"
        elif len(eligible_categories) < label_policy.minimum_supported_categories:
            reason = (f"insufficient_support: {len(eligible_categories)} unique eligible categories "
                      f"< {label_policy.minimum_supported_categories}")
        elif state.status in {"unknown", "ambiguous", "provisional"}:
            reason = "belief_or_margin_below_development_proposal"
        else:
            reason = "proposal_thresholds_satisfied_uncalibrated"
        rooms.append({
            "room_id": room_node_id, "eligible_object_count": sum(len(ids) for ids in category_members.values()),
            "eligible_categories": eligible_categories,
            "category_object_ids": {category: list(ids) for category, ids in category_members.items()},
            "A_contributions": {category: dict(a_scores[category]) for category in eligible_categories},
            "runtime_prior": dict(ledger.room_priors), "raw_S": raw_scores, "belief": beliefs,
            "candidate_label": ordered[0][0] if ordered else None, "commit_label": state.label,
            "status": state.status, "reason": reason,
            "supporting_objects": list(state.supporting_object_ids),
            "ledger_state": state.as_dict(),
        })
    return {
        "variant": variant, "A_source": "A_log_ratio_uncalibrated", "objects": objects, "rooms": rooms,
        "excluded_object_count": sum(not item["eligible"] for item in objects),
        "ledger": ledger.as_dict(),
    }


def static_diagnostic(readiness_path: Path, tables: dict[str, list[dict[str, Any]]], categories: list[str], variant: str, policy_path: Path) -> dict[str, Any]:
    readiness, readiness_objects = _readiness_objects(readiness_path)
    policy_config = json.loads(policy_path.read_text())
    physical_room_ids = [str(room_id) for room_id in (readiness.get("membership_scope", {}).get("candidate_room_ids") or [])]
    variants = {name: _static_variant(readiness_objects, tables, categories, name, policy_config, physical_room_ids)
                for name in VARIANTS}
    selected = variants[variant]
    return {
        "schema": "rb2-static-room-diagnostic/v2",
        "source": "RB1 readiness semantic consensus + RoomBeliefLedger; no GT/annotations; static non-causal diagnostic",
        "selected_variant": variant, "variant": variant, "canonical_aliases": ALIASES,
        "policy": {key: policy_config[key] for key in ("policy_id", "minimum_supported_categories",
                                                        "minimum_top_belief", "minimum_margin", "relabel_margin",
                                                        "relabel_evidence_revisions")},
        "semantic_consensus_source": "readiness.json semantic_category_raw_exact/status/support_by_pose_unit",
        "membership_source": readiness.get("membership_scope", {}),
        "objects": selected["objects"], "rooms": selected["rooms"],
        "excluded_object_count": selected["excluded_object_count"], "variants": variants,
    }


def render_html(out: Path, quality: dict[str, Any], static: dict[str, Any], svg_name: str) -> None:
    summary = quality["summary"]
    rows = ["<table><tr><th>variant</th><th>category</th><th>top1</th><th>top2</th><th>margin</th></tr>"]
    for row in summary:
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(row[key]))}</td>" for key in ("variant", "category", "top1", "top2", "margin")) + "</tr>")
    rows.append("</table>")
    obj_rows = ["<table><tr><th>hypothesis</th><th>raw→canonical</th><th>admission</th><th>eligible</th><th>reason</th></tr>"]
    for item in static["objects"]:
        obj_rows.append(f"<tr><td>{html.escape(item['hypothesis_id'])}</td><td>{html.escape(', '.join(item['raw_class_history']))} → {html.escape(str(item['category']))}</td><td>{html.escape(item['admission_status'])}</td><td>{item['eligible']}</td><td>{html.escape(item['reason'])}</td></tr>")
    obj_rows.append("</table>")
    room_rows = ["<table><tr><th>room</th><th>eligible categories</th><th>belief</th><th>candidate</th><th>status/reason</th></tr>"]
    for room in static["rooms"]:
        room_rows.append(f"<tr><td>{html.escape(room['room_id'])}</td><td>{html.escape(', '.join(room['eligible_categories']))}</td><td><pre>{html.escape(json.dumps({'A': room['A_contributions'], 'S': room['raw_S'], 'belief': room['belief']}, sort_keys=True, indent=2))}</pre></td><td>{html.escape(str(room['candidate_label']))}</td><td>{html.escape(room['status'] + ': ' + room['reason'])}</td></tr>")
    room_rows.append("</table>")
    variant_rows = []
    for name, payload in static["variants"].items():
        for room in payload["rooms"]:
            variant_rows.append(
                f"<tr><td>{html.escape(name)}</td><td>{html.escape(room['room_id'])}</td>"
                f"<td>{html.escape(', '.join(room['eligible_categories']))}</td>"
                f"<td>{html.escape(str(room['candidate_label']))}</td><td>{html.escape(str(room['commit_label']))}</td>"
                f"<td>{html.escape(room['status'] + ': ' + room['reason'])}</td></tr>"
            )
    variant_table = "<table><tr><th>variant</th><th>room node</th><th>categories</th><th>candidate</th><th>commit</th><th>status/reason</th></tr>" + ''.join(variant_rows) + "</table>"
    body = f"""<!doctype html><meta charset='utf-8'><title>RB2 A-table local report</title><style>body{{font:14px sans-serif;max-width:1500px;margin:20px}}table{{border-collapse:collapse;margin:10px 0}}td,th{{border:1px solid #bbb;padding:4px;vertical-align:top}}pre{{margin:0;white-space:pre-wrap}}</style><h1>RB2 A-table: local import and RB1 static diagnostic v2</h1><p>Experimental, uncalibrated compatibility scores. No variant is selected as a quality winner. Static scoring uses the checked-in RoomBeliefLedger: log(runtime prior) + one A contribution per unique consensus category, then softmax.</p><h2>Formula/provenance checks</h2><pre>{html.escape(json.dumps(quality['contract'], indent=2, sort_keys=True))}</pre><h2>Heatmaps</h2><img src='{html.escape(svg_name)}' alt='A-table heatmaps'><h2>Top-1/top-2 summary</h2>{''.join(rows)}<h2>Static object readiness (consensus source)</h2>{''.join(obj_rows)}<h2>Selected static diagnostic ({html.escape(static['variant'])})</h2>{''.join(room_rows)}<h2>All RB1 variants</h2>{variant_table}"""
    (out / "rb2_a_table_report.html").write_text(body, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--readiness", type=Path, required=True, help="readiness.json")
    ap.add_argument("--policy", type=Path, default=Path("configs/room_semantics/room_belief_v1.proposal.json"))
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    archive_sha = sha256_file(args.archive)
    with zipfile.ZipFile(args.archive) as zf:
        names = set(zf.namelist())
        run_dirs = sorted({name.split("/", 1)[0] for name in names if name.endswith("/manifest.json")})
        if len(run_dirs) != 1:
            raise SystemExit(f"expected one run in archive, got {run_dirs}")
        prefix = run_dirs[0]
        manifest = read_json(zf, f"{prefix}/manifest.json")
        effective = read_json(zf, f"{prefix}/effective_scoring_config.json")
        categories = list(effective["category_strings"])
        rooms = [row["id"] for row in effective["room_labels"]]
        tables, table_errors = validate_tables(zf, prefix, categories, rooms)
        raw_info, raw_errors = validate_raw_contract(zf, prefix, categories, rooms, float(effective["epsilon"]))
        contract, formula_errors = compare_contract(tables, raw_info, rooms, float(effective["temperature"]), float(effective["epsilon"]))
        # Preserve the original package file hashes from the embedded manifest.
        package_manifest = read_json(zf, f"{prefix}/manifest.json")
        internal_files = package_manifest.get("output_files", {})
        rows = summary_rows(tables, categories)
        winners = {variant: Counter(row["top1"] for row in rows if row["variant"] == variant) for variant in VARIANTS}
        static = static_diagnostic(args.readiness, tables, categories, "mean_neutral", args.policy)
        quality = {"archive_sha256": archive_sha, "expected_archive_sha256": EXPECTED_ARCHIVE_SHA256, "archive_hash_match": archive_sha == EXPECTED_ARCHIVE_SHA256, "run_manifest": manifest, "effective_scoring_config": {k: effective.get(k) for k in ("model_id", "model_revision", "temperature", "epsilon", "category_strings", "room_labels", "variants", "execution_policy", "runtime_weights", "score_dtype", "selection_claim")}, "internal_output_files": internal_files, "contract": {**contract, "errors": table_errors + raw_errors + formula_errors}, "summary": rows, "winner_distribution": {variant: dict(counter) for variant, counter in winners.items()}}
    (args.output / "import_manifest.json").write_text(json.dumps({"schema": "rb2-a-table-import/v2", "archive": str(args.archive.resolve()), "archive_sha256": archive_sha, "archive_hash_match": archive_sha == EXPECTED_ARCHIVE_SHA256, "source_run_hash": manifest.get("run_hash"), "source_manifest": manifest, "static_diagnostic_schema": "rb2-static-room-diagnostic/v2", "supersedes": "artifacts/room_belief/rb2_local_import_20261015/static_room_diagnostic.json", "derived_files": {}, "runtime_dependency": "stdlib-only; no Qwen/Transformers", "validation_status": "pass" if not quality["contract"]["errors"] and quality["archive_hash_match"] else "blocked"}, indent=2), encoding="utf-8")
    (args.output / "a_table_quality.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")
    (args.output / "a_table_top_summary.csv").write_text("variant,category,top1,top1_score,top2,top2_score,margin\n" + "\n".join(",".join(str(row[key]) for key in ("variant","category","top1","top1_score","top2","top2_score","margin")) for row in rows) + "\n", encoding="utf-8")
    (args.output / "a_table_heatmaps.svg").write_text(svg_heatmap(tables, categories, rooms), encoding="utf-8")
    (args.output / "static_room_diagnostic.json").write_text(json.dumps(static, indent=2), encoding="utf-8")
    render_html(args.output, quality, static, "a_table_heatmaps.svg")
    derived = {}
    for path in sorted(args.output.iterdir()):
        if path.name == "import_manifest.json":
            continue
        derived[path.name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    import_manifest = json.loads((args.output / "import_manifest.json").read_text())
    import_manifest["derived_files"] = derived
    (args.output / "import_manifest.json").write_text(json.dumps(import_manifest, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "archive_sha256": archive_sha, "archive_hash_match": quality["archive_hash_match"], "validation": import_manifest["validation_status"], "contract_errors": quality["contract"]["errors"], "winner_distribution": quality["winner_distribution"], "static_room_count": len(static["rooms"]), "static_excluded_objects": static["excluded_object_count"], "static_diagnostic": str((args.output / 'static_room_diagnostic.json').resolve()), "html_report": str((args.output / 'rb2_a_table_report.html').resolve())}, indent=2))
    return 0 if import_manifest["validation_status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
