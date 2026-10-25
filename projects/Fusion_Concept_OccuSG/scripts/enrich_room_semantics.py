#!/usr/bin/env python3
"""P5/P6 rule-based semantic enrichment for one checked G1 replay.

This post-stage keeps the geometry snapshot immutable and writes an explicit
semantic-only delta.  It never uses scene asset names, semantic GT, navmesh or
room annotations as evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.pipeline.contracts import GraphDelta, GraphNode, SceneGraphSnapshot  # noqa: E402
from standalone_sg.semantics import RuleBasedRoomLabeler  # noqa: E402
from standalone_sg.serialization import (  # noqa: E402
    load_snapshot, replay_deltas, write_delta_log, write_snapshot,
)
from standalone_sg.visualization import render_relationship_graph, render_spatial_map  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def enrich(snapshot: SceneGraphSnapshot, states) -> SceneGraphSnapshot:
    by_room = {state.room_id: state for state in states}
    nodes = []
    for node in snapshot.nodes:
        state = by_room.get(node.node_id)
        if state is None:
            nodes.append(node)
            continue
        attrs = dict(node.attributes)
        attrs.update({
            "semantic_label": state.label,
            "semantic_status": state.status,
            "semantic_candidates": [[label, score] for label, score in state.candidates],
            "semantic_observed_object_ids": list(state.observed_object_ids),
            "semantic_supporting_object_ids": list(state.supporting_object_ids),
            "semantic_evidence_hash": state.evidence_hash,
            "semantic_score_kind": state.score_kind,
            "semantic_method": state.method,
            "semantic_source_revision": state.source_revision,
            "semantic_computed_timestamp_s": state.computed_timestamp_s,
            "semantic_last_evidence_timestamp_s": state.last_evidence_timestamp_s,
            "semantic_stale_reason": state.stale_reason,
        })
        nodes.append(GraphNode(node.node_id, node.kind, node.first_seen_s, node.last_seen_s, node.provenance, attrs))
    return SceneGraphSnapshot(
        snapshot.schema_version, snapshot.scene_id, snapshot.sequence_id,
        snapshot.revision + 1, snapshot.timestamp_s, tuple(nodes), snapshot.edges,
        snapshot.config_hash, snapshot.stale_flags,
    )


def run(replay_dir: Path, config: Path, output: Path) -> dict:
    replay_dir, config, output = replay_dir.resolve(), config.resolve(), output.resolve()
    snapshot = load_snapshot(replay_dir / "snapshot.json")
    labeler = RuleBasedRoomLabeler(json.loads(config.read_text(encoding="utf-8")))
    states = labeler.label_snapshot(snapshot)
    semantic_snapshot = enrich(snapshot, states)
    output.mkdir(parents=True, exist_ok=True)
    write_snapshot(output / "snapshot.json", semantic_snapshot)
    with (output / "room_semantics.jsonl").open("w", encoding="utf-8") as handle:
        for state in states:
            handle.write(json.dumps(state.as_dict(), sort_keys=True, allow_nan=False) + "\n")
    semantic_delta = GraphDelta(
        base_revision=snapshot.revision, new_revision=semantic_snapshot.revision,
        timestamp_s=float(snapshot.timestamp_s or 0.0), added_nodes=(),
        updated_nodes=tuple(node for node in semantic_snapshot.nodes if node.kind == "room"),
        removed_node_ids=(), added_edges=(), removed_edges=(),
        events=("semantic_enrichment",),
        diagnostics={"semantic_config_sha256": sha256(config), "state_count": len(states),
                     "room_gt_used": False, "scene_asset_names_used": False},
        stale_flags=dict(snapshot.stale_flags), stage_versions={"semantics": "P5:rule_based_object_evidence_v1"},
    )
    write_delta_log(output / "delta_log.json", (semantic_delta,))
    replayed = replay_deltas(snapshot, (semantic_delta,))
    if replayed != semantic_snapshot:
        raise RuntimeError("semantic-only delta replay does not reconstruct semantic snapshot")
    render_spatial_map(semantic_snapshot, output / "spatial_semantic_map.png", show_objects=True, label_kinds=("room",))
    render_relationship_graph(semantic_snapshot, output / "room_object_graph.png", show_navigation=False, show_temporal=False,
                              show_agents=False, show_objects=True, label_kinds=("room",))
    counts = {}
    for state in states:
        counts[state.status] = counts.get(state.status, 0) + 1
    report = {
        "schema_version": 1, "kind": "room_semantic_enrichment",
        "status": "pass", "source_snapshot": str(replay_dir / "snapshot.json"),
        "source_snapshot_sha256": sha256(replay_dir / "snapshot.json"),
        "semantic_config": str(config), "semantic_config_sha256": sha256(config),
        "source_graph_revision": snapshot.revision, "semantic_graph_revision": semantic_snapshot.revision,
        "room_count": len(states), "status_counts": counts,
        "room_gt_used": False, "scene_asset_names_used": False, "navmesh_used": False,
        "accuracy": None,
        "limitations": [
            "rule scores are uncalibrated additive evidence, not probabilities",
            "no independent room annotations were available; no accuracy is reported",
            "labels are semantic attributes, never room IDs or graph keys",
            "first-view object center bias and M6 false merge/split limits remain",
        ],
        "artifacts": ["snapshot.json", "delta_log.json", "room_semantics.jsonl", "spatial_semantic_map.png", "room_object_graph.png"],
    }
    (output / "manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/semantics/room_rules_replica_cad_dev.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.replay, args.config, args.output), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
