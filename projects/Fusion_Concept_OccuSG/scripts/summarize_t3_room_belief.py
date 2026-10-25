#!/usr/bin/env python3
"""Create a read-only room/revision report from the completed T3 A/B outputs."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((path / "manifest.json").read_text())
    rows = [json.loads((path / item["state"]).read_text()) for item in manifest["revisions"]]
    return manifest, rows


def exclusion_reason(obj: dict[str, Any], scope_status: str, supported_categories: set[str]) -> str | None:
    if scope_status == "unavailable":
        return "unavailable_elevation_scope"
    if not obj["prefix_admission"].get("eligible"):
        return "prefix_admission_provisional"
    if obj["semantic"].get("status") != "stable":
        return "semantic_" + str(obj["semantic"].get("status", "unavailable"))
    if obj["semantic"].get("category") not in supported_categories:
        return "unsupported_category"
    if obj["membership"].get("status") != "assigned":
        return str(obj["membership"].get("reason", "membership_unavailable"))
    return None


def failed_gates(state: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Report the first support gate before score gates, matching RB1 admission semantics."""
    if state.get("scope_status") == "unavailable":
        return ["unavailable_elevation_scope"]
    category_count = len(state.get("category_counts", {}))
    if category_count < int(policy["minimum_supported_categories"]):
        return ["insufficient_support"]
    reasons = []
    candidates = state.get("candidates", [])
    if candidates:
        top = float(candidates[0][1])
        second = float(candidates[1][1]) if len(candidates) > 1 else 0.0
        if top < float(policy["minimum_top_belief"]):
            reasons.append("belief_below_threshold")
        if top - second < float(policy["minimum_margin"]):
            reasons.append("margin_below_threshold")
    if state.get("pending_label") is not None:
        reasons.append("relabel_hysteresis_pending")
    return reasons


def summarize(verified_dir: Path, assumed_dir: Path) -> dict[str, Any]:
    a_manifest, a_rows = read_run(verified_dir)
    b_manifest, b_rows = read_run(assumed_dir)
    variants = a_manifest["variants"]
    # The A output may contain no eligible category. Load the immutable A ZIP
    # to report unsupported canonical categories without inferring aliases.
    table_categories = set(a_manifest["membership_policy"].get("a_table_categories", []))
    if not table_categories:
        from run_causal_hybrid_room_belief import load_a_tables
        tables, table_categories_list, _, _ = load_a_tables(Path(a_manifest["source_a_zip"]))
        table_categories = set(table_categories_list)
    label_policy = b_manifest["room_belief_policy"]

    revisions: list[dict[str, Any]] = []
    for a, b in zip(a_rows, b_rows):
        pair = {"frame_index": a["frame_index"], "frame_id": a["frame_id"],
                "object_revision": a["revision"],
                "room_revision": a["room_lifecycle"]["room_revision"],
                "future_observations_used": False,
                "source_prefix": b["source_prefix"], "modes": {}}
        for mode, row in (("verified_only", a), ("assumed_single_level_demo", b)):
            object_status = Counter(obj["semantic"].get("status", "unknown") for obj in row["objects"])
            admission_count = sum(bool(obj["prefix_admission"].get("eligible")) for obj in row["objects"])
            membership_count = Counter(obj["membership"].get("status", "unknown") for obj in row["objects"])
            global_exclusions = []
            for obj in row["objects"]:
                reason = exclusion_reason(obj, row["scope_status"], table_categories)
                if reason:
                    global_exclusions.append({
                        "object_id": obj["causal_object_id"], "reason": reason,
                        "prefix_admission": obj["prefix_admission"],
                        "semantic_status": obj["semantic"].get("status"),
                        "category": obj["semantic"].get("category"),
                        "membership_status": obj["membership"].get("status"),
                        "membership_reason": obj["membership"].get("reason"),
                        "room_id": obj["membership"].get("room_id"),
                    })
            room_rows = []
            for variant in variants:
                for room_id in sorted(row["rooms"][variant]):
                    diagnostic = row["room_diagnostics"][variant][room_id]
                    room_rows.append({
                        "variant": variant, "room_id": room_id,
                        "room_geometry_revision": row["room_lifecycle"]["room_revision"],
                        "eligible_objects": diagnostic["eligible_objects"],
                        "eligible_object_count": len(diagnostic["eligible_objects"]),
                        "eligible_categories": diagnostic["eligible_categories"],
                        "eligible_category_count": len(diagnostic["eligible_categories"]),
                        "category_contributions": diagnostic["category_contributions"],
                        "excluded_objects_in_room": diagnostic["excluded_objects"],
                        "membership_support": diagnostic["membership_support"],
                        "raw_S": diagnostic["raw_S"], "beliefs": diagnostic["beliefs"],
                        "candidate_label": diagnostic["candidate_label"],
                        "committed_label": diagnostic["committed_label"],
                        "commit_status": diagnostic["commit_status"],
                        "abstain_reason": diagnostic["abstain_reason"],
                        "decision_reason": diagnostic["decision_reason"],
                        "failed_gates": failed_gates(row["rooms"][variant][room_id], label_policy),
                        "label_history_delta": diagnostic["label_history_delta"],
                        "scope_status": diagnostic["scope_status"],
                        "declaration_id": diagnostic["declaration_id"],
                        "declaration_sha256": diagnostic["declaration_sha256"],
                    })
            pair["modes"][mode] = {
                "scope_status": row["scope_status"],
                "declaration_id": row.get("declaration_id"),
                "declaration_sha256": row.get("declaration_sha256"),
                "object_count": len(row["objects"]), "prefix_admission_eligible_count": admission_count,
                "semantic_status_counts": dict(sorted(object_status.items())),
                "membership_status_counts": dict(sorted(membership_count.items())),
                "room_contains_count": len(row["room_contains_edges"]),
                "global_excluded_objects": global_exclusions,
                "rooms": room_rows,
            }
        revisions.append(pair)

    final_rooms: dict[str, Any] = {}
    label_transition_events = []
    for revision in revisions:
        for mode, mode_data in revision["modes"].items():
            for room in mode_data["rooms"]:
                for event in room["label_history_delta"]:
                    label_transition_events.append({
                        "mode": mode, "variant": room["variant"], "room_id": room["room_id"],
                        "frame_index": revision["frame_index"], "room_evidence_revision": event["revision"],
                        "from": event["from"], "to": event["to"], "reason": event["reason"],
                        "scope_status": room["scope_status"],
                        "declaration_id": room["declaration_id"],
                        "declaration_sha256": room["declaration_sha256"],
                    })
    for mode, manifest, rows in (("verified_only", a_manifest, a_rows),
                                  ("assumed_single_level_demo", b_manifest, b_rows)):
        final = rows[-1]
        final_rooms[mode] = {}
        for variant in variants:
            final_rooms[mode][variant] = {
                room_id: {
                    key: final["rooms"][variant][room_id][key]
                    for key in (
                        "candidate_label", "committed_label", "commit_status", "abstain_reason",
                        "decision_reason", "category_counts", "raw_scores", "candidates",
                        "pending_label", "pending_revision_count", "label_history", "scope_status",
                        "declaration_id", "declaration_sha256",
                    )
                }
                for room_id in sorted(final["rooms"][variant])
            }
            for room_id, state in final_rooms[mode][variant].items():
                state["failed_gates"] = failed_gates(state, label_policy)
    return {
        "schema": "t3-room-belief-a-b-prefix-report/v1", "status": "complete",
        "source_run_id": a_manifest.get("run_id"), "scene_id": a_manifest.get("scene_id"),
        "sequence_id": a_manifest.get("sequence_id"), "frame_range_inclusive": [0, len(revisions)-1],
        "variants": variants, "quality_metrics": "unavailable_no_matching_room_function_ground_truth",
        "winner_selected": False, "default_changed": False,
        "room_belief_policy": label_policy,
        "a_verified_only": {"scope_status": a_manifest["scope_status"],
                             "input_provenance": a_manifest["input_provenance"],
                             "manifest_path": str((verified_dir / "manifest.json").resolve())},
        "b_assumed_single_level_demo": {"scope_status": b_manifest["scope_status"],
                                        "declaration_id": b_manifest["scope_declaration_id"],
                                        "declaration_sha256": b_manifest["scope_declaration_sha256"],
                                        "input_provenance": b_manifest["input_provenance"],
                                        "manifest_path": str((assumed_dir / "manifest.json").resolve())},
        "final_room_states_by_variant": final_rooms,
        "label_transition_events": label_transition_events,
        "revisions": revisions,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# T3 prefix room belief — kết quả A/B",
        "",
        f"- Run: `{report['source_run_id']}`; sequence: `{report['sequence_id']}`; frames: 0–{report['frame_range_inclusive'][1]}.",
        "- A dùng `verified_only`; gate scope đóng, không có membership edge hay nhãn.",
        f"- B dùng declaration `{report['b_assumed_single_level_demo']['declaration_id']}` với `scope_status=assumed`; không phải scope verified.",
        "- Bốn bảng A chạy độc lập. Không chọn variant thắng, không đổi default.",
        "- Room accuracy/F1: unavailable — không có room-function GT tương ứng.",
        "- Chi tiết từng room/revision, eligible/excluded objects, category contributions, support, S, beliefs và quyết định nằm trong `t3_room_revision_report.json`.",
        f"- Số lần label history ghi nhận commit/retract/relabel: {len(report['label_transition_events'])}; không tính lặp trạng thái đã commit ở mỗi frame.",
        "",
        "## Cuối prefix (frame 28)",
        "",
        "| Variant | Room | Eligible categories | Candidate | Commit | Abstain reason / failed gates |",
        "|---|---|---|---|---|---|",
    ]
    for mode in ("verified_only", "assumed_single_level_demo"):
        for variant, rooms in report["final_room_states_by_variant"][mode].items():
            for room_id, state in rooms.items():
                categories = ", ".join(state["category_counts"].keys()) or "—"
                failed = ", ".join(state.get("failed_gates", [])) or "—"
                lines.append(f"| {variant} ({'A' if mode == 'verified_only' else 'B'}) | {room_id} | {categories} | {state['candidate_label'] or '—'} | {state['committed_label'] or '—'} | {state['abstain_reason'] or '—'} / {failed} |")
    lines += ["", "## Label transitions", ""]
    if report["label_transition_events"]:
        lines += ["| Mode | Variant | Frame | Room | From → to | Reason |", "|---|---|---:|---|---|---|"]
        for event in report["label_transition_events"]:
            lines.append(f"| {event['mode']} | {event['variant']} | {event['frame_index']} | {event['room_id']} | {event['from'] or 'unknown'} → {event['to'] or 'unknown'} | {event['reason']} |")
    else:
        lines.append("Không có chuyển trạng thái label trong prefix.")
    lines += ["", "## Prefix coverage", "", "| Frame | Objects | Admission eligible | Stable | Conflicted | Provisional/unknown | A assigned | B assigned | B eligible categories across rooms |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for rev in report["revisions"]:
        a, b = rev["modes"]["verified_only"], rev["modes"]["assumed_single_level_demo"]
        stable = b["semantic_status_counts"].get("stable", 0)
        conflicted = b["semantic_status_counts"].get("conflicted", 0)
        provisional = b["semantic_status_counts"].get("provisional", 0) + b["semantic_status_counts"].get("unknown", 0)
        assigned_a = a["membership_status_counts"].get("assigned", 0)
        assigned_b = b["membership_status_counts"].get("assigned", 0)
        cats = sorted({cat for room in b["rooms"] for cat in room["eligible_categories"]})
        lines.append(f"| {rev['frame_index']} | {b['object_count']} | {b['prefix_admission_eligible_count']} | {stable} | {conflicted} | {provisional} | {assigned_a} | {assigned_b} | {', '.join(cats) or '—'} |")
    lines += ["", "## Interpretation boundary", "", "B labels are outputs under an approved experimental XY-scope assumption. They do not verify elevation compatibility, room function accuracy, or causal live execution. A stays the verified-only control. The run used existing causal object and room prefixes; terminal/final objects and room snapshots were not backfilled.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verified", type=Path, required=True)
    parser.add_argument("--assumed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = summarize(args.verified, args.assumed)
    (args.output / "t3_room_revision_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    (args.output / "T3_ROOM_BELIEF_REPORT.vi.md").write_text(markdown(report))
    print(json.dumps({"status": report["status"], "output": str(args.output.resolve()),
                      "frames": len(report["revisions"]), "variants": report["variants"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
