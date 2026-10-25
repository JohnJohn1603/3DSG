#!/usr/bin/env python3
"""Run cached 00802 prefix through the M9 builder with fixed empty regions.

This is an M9 object-backend fixture, not a native M1-M5 replay and not a room
quality evaluation. It starts from frame zero to reach R23 with causal state.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from run_habitat_native_replay import _attach_duplicate_v2_evidence, _load_packets  # noqa: E402
from standalone_sg.objects.u5_backends import GeometryOnlyDuplicateV2AssociationBackend  # noqa: E402
from standalone_sg.pipeline import SceneGraphBuilder  # noqa: E402
from standalone_sg.serialization import load_delta_log, load_snapshot, replay_deltas, write_delta_log, write_snapshot  # noqa: E402


CACHE = Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058")
REFERENCE = ROOT / "artifacts/runs/u3_same_frame_dup_candidate_v2_20261011_v2/00802/assignments_candidate_v2.jsonl"
CONFIG_PATH = ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json"
OUT = ROOT / "artifacts/audits/u5_duplicate_v2_r23_fixed_region_m9_fixture_20261011"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if OUT.exists():
        raise FileExistsError(f"refusing to overwrite fixture output: {OUT}")
    config = json.loads(CONFIG_PATH.read_text())
    geometry = config["baseline_config"]["geometry_only"]
    _manifest, packets = _load_packets(CACHE / "capture", CACHE / "detection_3d/detection_3d.jsonl")
    packets, input_provenance = _attach_duplicate_v2_evidence(
        packets, CACHE / "detection_2d/detection_2d_index.json",
        CACHE / "capture/capture_manifest.json",
    )
    stop_frame = 235
    prefix_packets = []
    for packet in packets:
        frame_index = int(packet.frame_id.rsplit("-", 1)[-1])
        if frame_index > stop_frame:
            break
        prefix_packets.append(replace(packet, input_mode="regions", grid=None, regions=(),
                                      pointcloud=None, rgbd=None, occupancy_snapshot=None))
    if not prefix_packets or int(prefix_packets[-1].frame_id.rsplit("-", 1)[-1]) != stop_frame:
        raise ValueError("cached causal prefix does not reach frame 235")

    backend = GeometryOnlyDuplicateV2AssociationBackend(
        duplicate_config=config["candidate"],
        max_center_m=float(geometry["candidate_center_radius_m"]),
        geom_min_iou=float(geometry["appearance_iou_min"]),
        geom_only_min_iou=float(geometry["geometry_only_iou_min"]),
        ambiguity_margin=float(geometry.get("ambiguity_margin", 0.05)),
    )
    builder = SceneGraphBuilder(objects=backend, config_hash=sha(CONFIG_PATH))
    initial = builder.snapshot()
    deltas = []
    frame_records = []
    assignments = {}
    r23_details = None
    for packet in prefix_packets:
        snapshot, delta = builder.update(packet)
        deltas.append(delta)
        decisions = [dict(row) for row in backend.last_decisions]
        for row in decisions:
            assignments[row["detection_id"]] = row.get("track_id")
        frame_records.append({
            "frame_id": packet.frame_id,
            "duplicate_decisions": [dict(row) for row in backend.last_duplicate_decisions],
            "duplicate_aliases": [dict(row) for row in backend.last_duplicate_aliases],
            "ambiguous_ids": list(backend.last_ambiguous_detection_ids),
            "association_decisions": decisions,
            "deferred_ids": list(backend.last_deferred_detection_ids),
            "object_lifecycle_events": [dict(event) for event in delta.diagnostics.get("object_lifecycle_events", [])],
        })
        if packet.frame_id.endswith("-0235"):
            r23_details = frame_records[-1]
    flushed, flush_delta = builder.flush(prefix_packets[-1].timestamp_s + 1e-3)
    deltas.append(flush_delta)

    reference_rows = [json.loads(line) for line in REFERENCE.read_text().splitlines() if line.strip()]
    reference_prefix = {row["detection_id"]: row.get("track_id") for row in reference_rows
                        if int(row["frame_index"]) <= stop_frame}
    mismatches = [did for did in sorted(reference_prefix)
                  if assignments.get(did) != reference_prefix[did]]
    r23d1 = "hm3d-00802-main-dense-0235:det-0001"
    r23d3 = "hm3d-00802-main-dense-0235:det-0003"
    r23_assignments = {row["detection_id"]: row.get("track_id") for row in (r23_details or {}).get("association_decisions", [])}
    if r23_assignments.get(r23d1) != "u3-0018" or r23_assignments.get(r23d3) is not None:
        raise RuntimeError(f"R23 fixture behavior differs from locked offline reference: {r23_assignments}")
    if r23_details is None or r23d3 not in r23_details["deferred_ids"]:
        raise RuntimeError("R23 det-0003 was not deferred by this geometry-only replay")
    if r23d1 not in r23_details["ambiguous_ids"] or r23d3 not in r23_details["ambiguous_ids"]:
        raise RuntimeError("R23 pair did not retain ambiguous observation decisions")
    if mismatches:
        raise RuntimeError(f"fixed-region M9 fixture assignments differ on {len(mismatches)} observations: {mismatches[:20]}")

    OUT.mkdir(parents=True)
    write_snapshot(OUT / "snapshot.json", flushed)
    write_delta_log(OUT / "delta_log.json", deltas)
    (OUT / "frame_diagnostics.json").write_text(json.dumps(frame_records, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    reloaded = load_snapshot(OUT / "snapshot.json")
    replayed = replay_deltas(initial, load_delta_log(OUT / "delta_log.json"))
    if reloaded != flushed or replayed != reloaded:
        raise RuntimeError("fixed-region M9 fixture snapshot reload/delta replay mismatch")
    node_ids = {node.node_id for node in reloaded.nodes}
    dangling = [edge for edge in reloaded.edges if edge.source_id not in node_ids or edge.target_id not in node_ids]
    self_edges = [edge for edge in reloaded.edges if edge.source_id == edge.target_id]
    if dangling or self_edges:
        raise RuntimeError(f"M9 fixture graph invariant failure: dangling={len(dangling)}, self={len(self_edges)}")
    report = {
        "status": "pass",
        "kind": "m9_fixed_region_object_backend_fixture",
        "scope": "causal cached P3 prefix frames 0-235; fixed empty regions; no M1-M5/native mapping",
        "prefix_frames": len(prefix_packets),
        "prefix_last_frame_id": prefix_packets[-1].frame_id,
        "input_provenance": input_provenance,
        "duplicate_v2_config_sha256": sha(CONFIG_PATH),
        "offline_reference_sha256": sha(REFERENCE),
        "assignment_parity": {"exact": not mismatches, "compared_observations": len(reference_prefix), "mismatch_count": len(mismatches)},
        "R23": {
            "det_0001_track_id": r23_assignments.get(r23d1),
            "det_0003_track_id": r23_assignments.get(r23d3),
            "ambiguous_ids": r23_details["ambiguous_ids"],
            "deferred_ids": r23_details["deferred_ids"],
            "observation_aliases": r23_details["duplicate_aliases"],
        },
        "graph": {"object_nodes": sum(node.kind == "object" for node in reloaded.nodes),
                  "dangling_edges": len(dangling), "self_edges": len(self_edges),
                  "snapshot_reload_equal": reloaded == flushed, "delta_replay_equal": replayed == reloaded},
        "annotations_or_gt_used_by_predictor": False,
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
