#!/usr/bin/env python3
"""Verify T3 A/B isolation, prefix safety, ledger replay parity and provenance."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.semantics.room_belief_v1 import RoomBeliefEvent, RoomBeliefLedger, RoomBeliefPolicy
from run_causal_hybrid_room_belief import VARIANTS, load_a_tables, make_ledgers, sha

RAW_STATE_KEYS = (
    "room_id", "evidence_revision", "label", "status", "last_supported_label", "candidates",
    "raw_scores", "category_counts", "category_object_ids", "supporting_object_ids",
    "pending_label", "pending_revision_count", "label_history", "score_kind",
)


def _read_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((path / "manifest.json").read_text())
    rows = [json.loads((path / item["state"]).read_text()) for item in manifest["revisions"]]
    if manifest.get("status") != "complete" or len(rows) != manifest.get("frame_count"):
        raise ValueError(f"incomplete T3 output: {path}")
    return manifest, rows


def _policy(manifest: dict[str, Any]) -> RoomBeliefPolicy:
    cfg = manifest["room_belief_policy"]
    return RoomBeliefPolicy(cfg["policy_id"], int(cfg["minimum_supported_categories"]),
                            float(cfg["minimum_top_belief"]), float(cfg["minimum_margin"]),
                            float(cfg["relabel_margin"]), int(cfg["relabel_evidence_revisions"]))


def _assert_prefix_recompute(manifest: dict[str, Any], rows: list[dict[str, Any]], a_zip: Path) -> int:
    tables, _, labels, digests = load_a_tables(a_zip)
    checked = 0
    for variant in VARIANTS:
        for last in range(len(rows)):
            cfg = manifest["room_belief_policy"]
            policy = _policy(manifest)
            ledger = RoomBeliefLedger(room_ids=labels, room_node_ids=(), allow_empty_room_nodes=True,
                room_priors={room: 1.0 for room in labels}, category_room_scores=tables[variant],
                label_policy=policy, evidence_policy_id="rb1-causal-hybrid-v1",
                ontology_version="rb2-A-room-ontology-v1", a_table_hash=digests[variant])
            for row in rows[:last + 1]:
                active = row["room_lifecycle"]["active_room_ids"]
                for room_id in active:
                    ledger.register_room_node(room_id)
                encoded = [entry["event"] for entry in row["events"][variant]]
                events = [RoomBeliefEvent.from_dict(value) for value in encoded]
                if events:
                    ledger.apply_frame(events)
            for room_id in rows[last]["room_lifecycle"]["active_room_ids"]:
                expected = ledger.room_state(room_id).as_dict()
                actual = rows[last]["rooms"][variant][room_id]
                projected = {key: actual[key] for key in RAW_STATE_KEYS}
                if projected != expected:
                    raise AssertionError(f"incremental/recompute ledger mismatch: {variant}, frame {last}, {room_id}")
                checked += 1
    return checked


def verify(verified_dir: Path, assumed_dir: Path, a_zip: Path) -> dict[str, Any]:
    a_manifest, a_rows = _read_run(verified_dir)
    b_manifest, b_rows = _read_run(assumed_dir)
    if len(a_rows) != 29 or len(b_rows) != 29:
        raise AssertionError("T3 acceptance requires the locked 29-frame prefix set")
    if a_manifest["scope_status"] != "unavailable" or b_manifest["scope_status"] != "assumed":
        raise AssertionError("A/B scope statuses are not isolated as unavailable/assumed")
    if a_manifest["input_provenance"] != b_manifest["input_provenance"]:
        raise AssertionError("A and B do not use identical input provenance")
    if a_manifest["variants"] != b_manifest["variants"] or a_manifest["a_table_digests"] != b_manifest["a_table_digests"]:
        raise AssertionError("A and B A-table variant/digest sets differ")
    declaration_id = b_manifest["scope_declaration_id"]
    declaration_hash = b_manifest["scope_declaration_sha256"]
    if not declaration_id or not declaration_hash:
        raise AssertionError("B output is missing declaration provenance")

    for a, b in zip(a_rows, b_rows):
        if a["frame_id"] != b["frame_id"] or a["frame_index"] != b["frame_index"]:
            raise AssertionError("A/B frame alignment mismatch")
        if a["future_observations_used"] or b["future_observations_used"]:
            raise AssertionError("future state marker is true")
        if a["scope_status"] != "unavailable" or a["room_contains_edges"]:
            raise AssertionError(f"A opened closed membership gate at frame {a['frame_index']}")
        if any(obj["membership"]["status"] != "unavailable" for obj in a["objects"]):
            raise AssertionError(f"A contains computed membership at frame {a['frame_index']}")
        if any(state["label"] is not None for variant in VARIANTS for state in a["rooms"][variant].values()):
            raise AssertionError(f"A created room label without valid scope at frame {a['frame_index']}")
        if any(state["category_counts"] or state["commit_status"] != "abstain" or
               state["abstain_reason"] != "unavailable_elevation_scope"
               for variant in VARIANTS for state in a["rooms"][variant].values()):
            raise AssertionError(f"A retained invalid room evidence at frame {a['frame_index']}")
        if b["scope_status"] != "assumed" or b["declaration_id"] != declaration_id or b["declaration_sha256"] != declaration_hash:
            raise AssertionError(f"B declaration provenance missing at frame {b['frame_index']}")
        if any(obj["membership"].get("scope_status") != "assumed" or
               obj["semantic"].get("scope_status") != "assumed" for obj in b["objects"]):
            raise AssertionError(f"B object evidence lacks assumed status at frame {b['frame_index']}")
        if any(obj["membership"].get("declaration_id") != declaration_id or
               obj["membership"].get("declaration_sha256") != declaration_hash or
               obj["semantic"].get("declaration_id") != declaration_id or
               obj["semantic"].get("declaration_sha256") != declaration_hash
               for obj in b["objects"]):
            raise AssertionError(f"B object evidence lacks exact declaration provenance at frame {b['frame_index']}")
        if any(state.get("scope_status") != "assumed" for variant in VARIANTS for state in b["rooms"][variant].values()):
            raise AssertionError(f"B room state lacks assumed status at frame {b['frame_index']}")
        if any(state.get("declaration_id") != declaration_id or
               state.get("declaration_sha256") != declaration_hash
               for variant in VARIANTS for state in b["rooms"][variant].values()):
            raise AssertionError(f"B room state lacks exact declaration provenance at frame {b['frame_index']}")
        a_objects = {obj["causal_object_id"]: obj for obj in a["objects"]}
        b_objects = {obj["causal_object_id"]: obj for obj in b["objects"]}
        if set(a_objects) != set(b_objects):
            raise AssertionError("A/B object partition differs")
        for key in a_objects:
            for field in ("observation_ids", "raw_class_history", "prefix_admission", "semantic"):
                ax, bx = a_objects[key][field], b_objects[key][field]
                if field == "semantic":
                    ax = {k: v for k, v in ax.items() if k not in {"scope_status", "declaration_id", "declaration_sha256"}}
                    bx = {k: v for k, v in bx.items() if k not in {"scope_status", "declaration_id", "declaration_sha256"}}
                if ax != bx:
                    raise AssertionError(f"A/B semantic/admission evidence differs: {key}/{field}")
        active_rooms = set(b["room_lifecycle"]["active_room_ids"])
        current_objects = set(b_objects)
        for edge in b["room_contains_edges"]:
            if edge["source_id"] not in active_rooms or edge["target_id"] not in current_objects:
                raise AssertionError(f"dangling membership edge in B frame {b['frame_index']}")
        if b["room_semantics_status"] != "assumed_experimental":
            raise AssertionError("B is not visibly marked experimental/assumed")
        for variant in VARIANTS:
            for room_id, diagnostic in b["room_diagnostics"][variant].items():
                categories = diagnostic["eligible_categories"]
                contributions = diagnostic["category_contributions"]
                if len(categories) != len(set(categories)) or len(contributions) != len(categories):
                    raise AssertionError(f"category-presence cap violated: {variant}/{room_id}/{b['frame_index']}")
                if {item["category"] for item in contributions} != set(categories):
                    raise AssertionError(f"A contribution category mismatch: {variant}/{room_id}/{b['frame_index']}")
                if any(set(item["A_log_ratio_by_room_type"]) != set(diagnostic["raw_S"])
                       for item in contributions):
                    raise AssertionError(f"A row is not keyed by the semantic room ontology: {variant}/{room_id}/{b['frame_index']}")

    parity_room_states = _assert_prefix_recompute(a_manifest, a_rows, a_zip)
    parity_room_states += _assert_prefix_recompute(b_manifest, b_rows, a_zip)
    return {
        "status": "pass", "frames": len(a_rows), "variants": list(VARIANTS),
        "a_scope_status": a_manifest["scope_status"], "b_scope_status": b_manifest["scope_status"],
        "declaration_id": declaration_id, "declaration_sha256": declaration_hash,
        "incremental_recompute_room_states_checked": parity_room_states,
        "a_room_contains_edges": sum(len(row["room_contains_edges"]) for row in a_rows),
        "b_room_contains_edges": sum(len(row["room_contains_edges"]) for row in b_rows),
        "a_committed_labels": sum(state["label"] is not None for row in a_rows for variant in VARIANTS for state in row["rooms"][variant].values()),
        "b_committed_labels": sum(state["label"] is not None for row in b_rows for variant in VARIANTS for state in row["rooms"][variant].values()),
        "input_provenance": a_manifest["input_provenance"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verified", type=Path, required=True)
    parser.add_argument("--assumed", type=Path, required=True)
    parser.add_argument("--a-zip", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.verified, args.assumed, args.a_zip)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
