#!/usr/bin/env python3
"""Validate M0/M1 cached ConceptGraphs causal object timeline artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from standalone_sg.serialization.hybrid_timeline import replay_timeline


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def export_signature(mapping: Path) -> dict:
    manifest = json.loads((mapping / "export/export_manifest.json").read_text())
    return {"objects": sorted((tuple(sorted(x["evidence"])), x["bounds_minmax"], x.get("raw_detector_class"))
                              for x in manifest["objects"]),
            "files_sha256": dict(sorted(manifest["files_sha256"].items())),
            # The reference pickle is not a stable byte-level artifact across
            # fresh processes; exported NPZ geometry and lineage are the parity
            # contract. Keep its hashes as a diagnostic, not an equality gate.
            "native_map_sha256": manifest["native_map_sha256"]}


def final_stage_signature(mapping: Path) -> list[tuple[str, ...]]:
    lineage = json.loads((mapping / "lineage_stages.json").read_text())
    return sorted(tuple(sorted(row["observation_ids"]))
                  for row in lineage["stages"]["after_text_gated_merge"])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--recorded", type=Path, required=True)
    p.add_argument("--recorded-repeat", type=Path, required=True)
    p.add_argument("--timeline", type=Path, required=True)
    p.add_argument("--timeline-repeat", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    s0, s1 = export_signature(a.baseline), export_signature(a.recorded)
    s2 = export_signature(a.recorded_repeat)
    final_parity = (s0["objects"], s0["files_sha256"]) == (s1["objects"], s1["files_sha256"]) == (s2["objects"], s2["files_sha256"])
    export_evidence = sorted(tuple(sorted(x[0])) for x in s1["objects"])
    final_stage_matches_export = final_stage_signature(a.recorded) == export_evidence
    rows = replay_timeline(a.timeline)
    repeat_rows = replay_timeline(a.timeline_repeat)
    prefix_ok = True
    prefix_counts = []
    for row in rows:
        frame = int(row["frame_index"])
        ids = row.get("observation_ids", [])
        for oid in ids:
            left = str(oid).split(":det-", 1)[0]
            obs_frame = int(left.rsplit("-", 1)[1])
            if obs_frame > frame:
                prefix_ok = False
        prefix_counts.append({"revision": row["revision"], "frame_index": frame,
                              "objects": len(row.get("objects", [])),
                              "observations": len(set(ids))})
    timeline_hashes = [sha(a.timeline / x["snapshot"]) for x in json.loads((a.timeline / "frame_index.json").read_text())["frames"]]
    repeat_hashes = [sha(a.timeline_repeat / x["snapshot"]) for x in json.loads((a.timeline_repeat / "frame_index.json").read_text())["frames"]]
    repeatability = timeline_hashes == repeat_hashes and len(rows) == len(repeat_rows)
    baseline_run = json.loads((a.baseline / "run_manifest.json").read_text())
    recorded_run = json.loads((a.recorded / "run_manifest.json").read_text())
    report = {
        "schema": "conceptgraphs-causal-timeline-validation/v1",
        "status": "pass" if final_parity and final_stage_matches_export and prefix_ok and repeatability else "fail",
        "final_export_parity": final_parity,
        "final_geometry_and_partition_parity": final_parity,
        "final_postprocess_stage_matches_final_export": final_stage_matches_export,
        "native_pickle_byte_hashes": {"baseline": s0["native_map_sha256"], "recorder_on_1": s1["native_map_sha256"], "recorder_on_2": s2["native_map_sha256"]},
        "native_pickle_byte_parity": s0["native_map_sha256"] == s1["native_map_sha256"] == s2["native_map_sha256"],
        "prefix_no_future_observations": prefix_ok,
        "delta_replay_snapshots_checked": len(rows),
        "timeline_repeatability": repeatability,
        "baseline_runtime_s": baseline_run["runtime_s"],
        "recorder_runtime_s": recorded_run["runtime_s"],
        "recorder_overhead_s": recorded_run["runtime_s"] - baseline_run["runtime_s"],
        "baseline_peak_rss_mib": baseline_run["peak_rss_mib"],
        "recorder_peak_rss_mib": recorded_run["peak_rss_mib"],
        "timeline_storage_bytes": json.loads((a.timeline / "manifest.json").read_text())["storage_bytes"],
        "prefix_counts": prefix_counts,
        "causality": json.loads((a.timeline / "manifest.json").read_text())["causality"],
        "stage_semantics": json.loads((a.timeline / "manifest.json").read_text())["stage_semantics"],
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
