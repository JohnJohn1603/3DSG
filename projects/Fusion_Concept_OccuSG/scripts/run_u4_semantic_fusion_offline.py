#!/usr/bin/env python3
"""Run locked U4 class evidence on immutable, already-computed U3 assignments.

Predictor phase deliberately does not load evaluator GT or room labels.
"""
from __future__ import annotations

import hashlib
import json
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.semantics.u4_class_fusion import (  # noqa: E402
    CausalClassEvidence, ClassObservation, quantize_view_bin,
)

OUT = ROOT / "artifacts/audits/u4_semantic_fusion"
U3 = ROOT / "artifacts/audits/u3_causal_prefix"
MODES = ("source_raw", "source_dedup", "geometry_only", "geometry_appearance")


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_locked_external(name: str) -> tuple[Path, dict]:
    lock = json.loads((ROOT / "locks/external_resources.json").read_text())["files"][name]
    path = Path(lock["path"])
    if not path.is_file() or path.stat().st_size != lock["bytes"] or sha_file(path) != lock["sha256"]:
        raise RuntimeError(f"external input changed from lock: {name}")
    return path, lock


def frame_number(did: str) -> int:
    return int(did.split(":", 1)[0].rsplit("-", 1)[-1])


def load_inputs():
    config_path = OUT / "u4_config.locked.json"
    config = json.loads(config_path.read_text())
    if config.get("status") != "locked_before_u4_scoring":
        raise RuntimeError("U4 config is not locked")
    idx_path, idx_digest_path = U3 / "causal_input_index.json", U3 / "causal_input_index.sha256"
    expected_line = idx_digest_path.read_text()
    if expected_line != f"{sha_file(idx_path)}  causal_input_index.json\n":
        raise RuntimeError("U3 immutable input-index hash mismatch")
    index = json.loads(idx_path.read_text())
    if index.get("record_count") != 622:
        raise RuntimeError("unexpected U3 input record count")
    index_by_id = {r["detection_id"]: r for r in index["records"]}

    d3_path, d3_lock = load_locked_external("dense_detection_3d_cache")
    d3_by_id: dict[str, dict] = {}
    for line in d3_path.read_text().splitlines():
        if not line:
            continue
        frame = json.loads(line)
        fno = int(frame["frame_id"].rsplit("-", 1)[-1])
        if not 0 <= fno <= 238:
            continue
        for det in frame["detections"]:
            d3_by_id[det["detection_id"]] = det
    if set(d3_by_id) != set(index_by_id):
        raise RuntimeError("P3 observation IDs differ from locked U3 index")
    for did, row in index_by_id.items():
        det = d3_by_id[did]
        if (det.get("center_W") != row.get("center_W")
                or det.get("extents_m") != row.get("extents_m")):
            raise RuntimeError(f"geometry provenance differs from U3 lock: {did}")
        if det.get("provider_metadata", {}).get("room_gt_used") or det.get("provider_metadata", {}).get("semantic_gt_used"):
            raise RuntimeError(f"GT provenance flag present in predictor observation: {did}")

    alias_doc = json.loads((U3 / "deduplicated_mask_aliases.json").read_text())
    aliases = {r["detection_id"]: r["duplicate_of"] for r in alias_doc["duplicates"]}
    return config, index, index_by_id, d3_by_id, aliases, d3_lock


def read_assignments(mode: str) -> dict[str, dict]:
    path = U3 / f"decisions_{mode}.jsonl"
    return {r["detection_id"]: r for r in
            (json.loads(line) for line in path.read_text().splitlines() if line)}


def make_view_bin(d3: dict, center: list[float], config: dict) -> str:
    pose = d3.get("provider_metadata", {}).get("camera_pose_W_Copt")
    if not isinstance(pose, list) or len(pose) < 3:
        return "unknown"
    try:
        camera = (float(pose[0][3]), float(pose[1][3]), float(pose[2][3]))
        return quantize_view_bin(
            camera, center,
            azimuth_bin_deg=int(config["evidence"]["view_bin_azimuth_degrees"]),
            elevation_bin_deg=int(config["evidence"]["view_bin_elevation_degrees"]),
        )
    except (IndexError, TypeError, ValueError):
        return "unknown"


def main() -> int:
    started = time.perf_counter()
    config, index, index_by_id, d3_by_id, aliases, d3_lock = load_inputs()
    OUT.mkdir(parents=True, exist_ok=True)
    mode_report = {}
    for mode in MODES:
        assignments = read_assignments(mode)
        raw_ids = set(index_by_id)
        if mode == "source_raw":
            if set(assignments) != raw_ids:
                raise RuntimeError("raw source assignments do not cover all locked observations")
        else:
            if set(assignments) != raw_ids - set(aliases):
                raise RuntimeError(f"deduplicated assignments do not match alias manifest: {mode}")

        # Recover fixed assignment for a suppressed duplicate only as a ledger
        # alias. It never becomes an independent view-evidence contribution.
        assignment_by_id = dict(assignments)
        for did, representative in aliases.items():
            if mode == "source_raw":
                continue
            assignment_by_id[did] = {
                "track_id": assignments.get(representative, {}).get("track_id"),
                "decision": "duplicate_alias",
                "duplicate_of": representative,
                "frame_index": index_by_id[did]["frame_index"],
                "mode": mode,
            }

        ordered_ids = sorted(raw_ids, key=lambda did: (index_by_id[did]["frame_index"], did))
        states: dict[str, CausalClassEvidence] = {}
        obs_rows: list[dict] = []
        state_rows: list[dict] = []
        for did in ordered_ids:
            d3 = d3_by_id[did]
            idx = index_by_id[did]
            assignment = assignment_by_id.get(did)
            if assignment is None:
                raise RuntimeError(f"missing fixed U3 assignment record: {mode} {did}")
            duplicate = did in aliases and mode != "source_raw"
            raw_class = d3.get("class_name") or d3.get("provider_metadata", {}).get("class_raw")
            confidence = d3.get("confidence")
            confidence = 0.0 if confidence is None else float(confidence)
            track_id = assignment.get("track_id")
            evidence_included = bool(track_id) and not duplicate and raw_class is not None
            row = {
                "mode": mode,
                "detection_id": did,
                "frame_index": int(idx["frame_index"]),
                "timestamp_s": float(idx["timestamp_s"]),
                "raw_class": raw_class,
                "raw_confidence": confidence,
                "track_id": track_id,
                "u3_decision": assignment.get("decision"),
                "duplicate_alias": duplicate,
                "duplicate_of": aliases.get(did) if duplicate else None,
                "evidence_included": evidence_included,
                "view_bin": make_view_bin(d3, d3["center_W"], config),
                "source_observation_sha256": d3_lock["sha256"],
                "u3_input_index_sha256": hashlib.sha256((U3 / "causal_input_index.json").read_bytes()).hexdigest(),
            }
            if evidence_included:
                state = states.setdefault(str(track_id), CausalClassEvidence(
                    min_independent_views=int(config["evidence"]["minimum_independent_view_bins"]),
                    minimum_score_margin=float(config["evidence"]["minimum_winner_score_margin"]),
                    minimum_winner_fraction=float(config["evidence"]["minimum_winner_fraction"]),
                ))
                current = state.add(ClassObservation(
                    did, row["frame_index"], row["timestamp_s"], str(raw_class), confidence, row["view_bin"],
                ))
                row.update({"initial_class": current["initial_class"],
                            "resolved_class": current["resolved_class"],
                            "class_status": current["class_status"],
                            "candidate_class": current["candidate_class"],
                            "independent_view_bins_by_class": current["independent_view_bins_by_class"]})
                state_rows.append({"mode": mode, "track_id": track_id, **current})
            else:
                row.update({"initial_class": None, "resolved_class": None,
                            "class_status": "unassigned" if not track_id else "duplicate_alias",
                            "candidate_class": None,
                            "independent_view_bins_by_class": {}})
            obs_rows.append(row)

        with (OUT / f"observations_{mode}.jsonl").open("w") as stream:
            for row in obs_rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        with (OUT / f"causal_states_{mode}.jsonl").open("w") as stream:
            for row in state_rows:
                stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        summaries = {}
        with (OUT / f"events_{mode}.jsonl").open("w") as stream:
            for track_id, state in sorted(states.items()):
                summary = state.summary()
                summary["track_id"] = track_id
                summaries[track_id] = summary
                for event in state.events:
                    stream.write(json.dumps({"mode": mode, "track_id": track_id, **event},
                                            sort_keys=True, allow_nan=False) + "\n")
        (OUT / f"track_summary_{mode}.json").write_text(
            json.dumps(summaries, indent=2, sort_keys=True, allow_nan=False) + "\n")
        final_state_counts: dict[str, int] = defaultdict(int)
        initial_to_final_changes = 0
        confirmed_count = 0
        for summary in summaries.values():
            final_state_counts[summary["class_status"]] += 1
            confirmed_count += summary["class_status"] == "confirmed"
            initial_to_final_changes += (summary["resolved_class"] is not None
                                         and summary["initial_class"] != summary["resolved_class"])
        mode_report[mode] = {
            "assigned_tracks": len(summaries),
            "observations": len(obs_rows),
            "evidence_included_observations": sum(r["evidence_included"] for r in obs_rows),
            "final_track_status_counts": dict(final_state_counts),
            "confirmed_track_coverage": confirmed_count / len(summaries) if summaries else None,
            "confirmed_tracks": int(confirmed_count),
            "initial_to_final_confirmed_class_changes": int(initial_to_final_changes),
            "event_count": sum(len(s.events) for s in states.values()),
        }

    report = {
        "schema_version": 1,
        "status": "causal_u4_predictions_ready_unscored",
        "config_sha256": sha_file(OUT / "u4_config.locked.json"),
        "u3_input_index_sha256": hashlib.sha256((U3 / "causal_input_index.json").read_bytes()).hexdigest(),
        "u3_geometry_only_assignments_sha256": sha_file(U3 / "decisions_geometry_only.jsonl"),
        "u3_source_assignments_sha256": sha_file(U3 / "decisions_source_dedup.jsonl"),
        "u3_geometry_appearance_assignments_sha256": sha_file(U3 / "decisions_geometry_appearance.jsonl"),
        "predictor_ground_truth_loaded": False,
        "room_labels_loaded": False,
        "association_changed": False,
        "class_assignments_first_observation_vs_fusion_use_same_tracks": True,
        "modes": mode_report,
        "runtime_seconds": time.perf_counter() - started,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "frames_seen_before_u4": {"u3_holdout": [231, 238]},
    }
    (OUT / "u4_prediction_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
