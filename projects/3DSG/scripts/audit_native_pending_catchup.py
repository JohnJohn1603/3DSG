#!/usr/bin/env python3
"""Verify replay of shutdown-pending keyframes against the live graph prefix."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def state_event(root: Path, branch: str, frame_index: int) -> dict | None:
    rows = events(root / "branches" / branch / "events.jsonl")
    return next((row for row in reversed(rows)
                 if int(row.get("source_frame_index", -1)) == frame_index), None)


def audit(source_run: Path, catchup_run: Path,
          pending_replay_roots: list[Path]) -> dict:
    source_graph = source_run / "native_graph"
    catchup_graph = catchup_run
    commits = [json.loads(line) for line in
               (source_graph / "paired_commits.jsonl").read_text().splitlines()
               if line.strip()]
    committed_indices = [int(row["frame_index"]) for row in commits]
    source_events = events(source_run / "events.jsonl")
    shutdown = next((row for row in reversed(source_events)
                     if row.get("event") == "graph_worker_shutdown"), None)
    if shutdown is None:
        raise ValueError("source live run has no graph_worker_shutdown event")
    pending_ids = shutdown.get("pending_keyframes_preserved_for_replay", [])
    if len(pending_replay_roots) != len(pending_ids):
        raise ValueError("provide exactly one single-frame replay artifact per pending keyframe")
    pending_indices = [int(frame_id.rsplit(":", 1)[1]) for frame_id in pending_ids]
    expected_indices = sorted(set(committed_indices + pending_indices))
    summary = read_json(catchup_graph / "summary.json")
    if summary.get("status") != "pass" or summary.get("processed_frames") != len(expected_indices):
        raise ValueError("catch-up replay did not process every committed and pending keyframe")
    if summary.get("frame_indices") != expected_indices:
        raise ValueError("catch-up frame sequence differs from expected committed+pending frames")

    common_prefix_index = max(committed_indices)
    prefix_checks = {}
    for branch in ("rooms", "objects"):
        source_event = state_event(source_graph, branch, common_prefix_index)
        replay_event = state_event(catchup_graph, branch, common_prefix_index)
        if source_event is None or replay_event is None:
            raise ValueError(f"missing {branch} state at common prefix frame {common_prefix_index}")
        source_hash = source_event.get("state_sha256")
        replay_hash = replay_event.get("state_sha256")
        prefix_checks[branch] = {
            "frame_index": common_prefix_index,
            "source_state_sha256": source_hash,
            "replay_state_sha256": replay_hash,
            "exact_match": bool(source_hash and source_hash == replay_hash),
        }
        if not prefix_checks[branch]["exact_match"]:
            raise ValueError(f"{branch} state diverged at committed prefix frame {common_prefix_index}")

    pending_checks = []
    for root, frame_index in zip(pending_replay_roots, pending_indices):
        replay = read_json(root / "summary.json")
        if replay.get("status") != "pass" or int(replay.get("frame_index", -1)) != frame_index:
            raise ValueError(f"single-frame replay failed for pending frame {frame_index}")
        if replay.get("aux_depth_present") is not True:
            raise ValueError(f"auxiliary depth missing in pending frame {frame_index}")
        for worker in ("conceptgraphs", "occusg"):
            if replay.get("mapper_results", {}).get(worker, {}).get("exit_code") != 0:
                raise ValueError(f"{worker} failed to finalize pending frame {frame_index}")
        pending_checks.append({"frame_index": frame_index, "frame_id": replay["frame_id"],
                               "auxiliary_depth_integrated": True,
                               "conceptgraphs_exit_code": 0, "occusg_exit_code": 0})

    pair_events = [row for row in events(catchup_graph / "paired_commits.jsonl")]
    final_counts = summary["final_graph"]
    return {
        "schema": "3dsg-native-pending-catchup-audit/v1",
        "source_run": str(source_run),
        "catchup_run": str(catchup_run),
        "status": "pass",
        "committed_frame_indices": committed_indices,
        "pending_frame_ids": pending_ids,
        "replayed_frame_indices": expected_indices,
        "processed_frames": summary["processed_frames"],
        "common_prefix_state_parity": prefix_checks,
        "pending_single_frame_replay_checks": pending_checks,
        "paired_commits": len(pair_events),
        "graph_join_performed": False,
        "reconstructed_final_graph": final_counts,
        "scope": "Fresh native sessions replayed the complete committed prefix, then all preserved pending keyframes; this reconstructs state and does not mutate the closed live run.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--catchup-run", type=Path, required=True)
    parser.add_argument("--pending-replay", type=Path, action="append", required=True,
                        help="single-keyframe replay output, in the same order as shutdown pending frames")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.source_run, args.catchup_run, args.pending_replay)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
