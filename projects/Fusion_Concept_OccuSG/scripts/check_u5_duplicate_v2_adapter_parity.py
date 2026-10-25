#!/usr/bin/env python3
"""Compare the M9 geometry+dedup-v2 adapter with locked offline assignments."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from run_habitat_native_replay import _attach_duplicate_v2_evidence, _load_packets  # noqa: E402
from standalone_sg.objects.u5_backends import GeometryOnlyDuplicateV2AssociationBackend  # noqa: E402


CONFIG = ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json"
CONFIG_SHA256 = "257dd354c7b562fed4b8d2b6548fcf38fe67946aef2e95ae1c084d79044d3ec3"
P3_ROOTS = {
    "00800": ROOT / "artifacts/runs/hm3d_00800_u4ov_multiview_pilot_v2_640",
    "00802": Path("/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/hm3d_00802_20261006T163140.468551Z_e4a058"),
    "00808": ROOT / "artifacts/runs/u6_hm3d_00808_quality_v1_20261010",
}
REFERENCE_ROOT = ROOT / "artifacts/runs/u3_same_frame_dup_candidate_v2_20261011_v2"
OUT = ROOT / "artifacts/audits/u5_duplicate_v2_adapter_parity_20261011"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def expected_geometry(rows, packets):
    detections = {d.detection_id: d for p in packets if p.detections is not None
                  for d in (p.detections.detections or ())}
    states = {}
    for row in rows:
        did, track_id, decision = row["detection_id"], row.get("track_id"), row["decision"]
        if track_id is None or decision in {"duplicate_alias", "defer"}:
            continue
        det = detections[did]
        if decision == "created":
            states[track_id] = {
                "center_W": det.center_W, "extents_m": det.extents_m,
                "first_seen_s": det.timestamp_s, "last_seen_s": det.timestamp_s,
                "first_detection_id": did, "last_detection_id": did,
                "class_name": det.class_name, "layer_id": det.layer_id,
            }
        elif decision == "matched":
            prior = states[track_id]
            prior["center_W"] = tuple((a + b) / 2 for a, b in zip(prior["center_W"], det.center_W))
            if prior["extents_m"] is not None and det.extents_m is not None:
                prior["extents_m"] = tuple((a + b) / 2 for a, b in zip(prior["extents_m"], det.extents_m))
            else:
                prior["extents_m"] = det.extents_m or prior["extents_m"]
            prior["last_seen_s"] = det.timestamp_s
            prior["last_detection_id"] = did
    return states


def run_scene(scene: str, cache: Path, config: dict) -> dict:
    capture = cache / "capture"
    p2 = cache / "detection_2d/detection_2d_index.json"
    p3 = cache / "detection_3d/detection_3d.jsonl"
    _manifest, packets = _load_packets(capture, p3)
    packets, input_provenance = _attach_duplicate_v2_evidence(
        packets, p2, capture / "capture_manifest.json",
    )
    geometry = config["baseline_config"]["geometry_only"]
    backend = GeometryOnlyDuplicateV2AssociationBackend(
        duplicate_config=config["candidate"],
        max_center_m=float(geometry["candidate_center_radius_m"]),
        geom_min_iou=float(geometry["appearance_iou_min"]),
        geom_only_min_iou=float(geometry["geometry_only_iou_min"]),
        ambiguity_margin=float(geometry.get("ambiguity_margin", 0.05)),
    )
    actual_assignments = {}
    actual_duplicate_decisions = {}
    evidence_rows = []
    actual_decisions = []
    for packet in packets:
        if packet.detections is None:
            continue
        backend.update(packet.detections)
        for row in backend.last_decisions:
            actual_assignments[row["detection_id"]] = row.get("track_id")
            actual_decisions.append({"frame_id": packet.frame_id, **row})
        for row in backend.last_duplicate_decisions:
            actual_duplicate_decisions[row["detection_id"]] = {
                "decision": row["decision"], "duplicate_of": row.get("duplicate_of")
            }
        evidence_rows.extend(backend.last_duplicate_pair_evidence)

    ref_dir = REFERENCE_ROOT / scene
    reference_rows = [json.loads(line) for line in (ref_dir / "assignments_candidate_v2.jsonl").read_text().splitlines() if line.strip()]
    reference_assignments = {row["detection_id"]: row.get("track_id") for row in reference_rows}
    reference_duplicates = {row["detection_id"]: {
        "decision": row.get("duplicate_decision_status", "alias_suppressed" if row.get("suppressed_from_association") else "kept_canonical"),
        "duplicate_of": row.get("duplicate_of"),
    } for row in reference_rows}
    reference_evidence = [json.loads(line) for line in (ref_dir / "candidate_v2_pair_evidence.jsonl").read_text().splitlines() if line.strip()]
    assignment_mismatches = [did for did in sorted(set(reference_assignments) | set(actual_assignments))
                             if reference_assignments.get(did) != actual_assignments.get(did)]
    duplicate_mismatches = [did for did in sorted(set(reference_duplicates) | set(actual_duplicate_decisions))
                            if reference_duplicates.get(did) != actual_duplicate_decisions.get(did)]
    ref_evidence_by_key = {
        tuple(sorted((row["left_detection_id"], row["right_detection_id"]))): row
        for row in reference_evidence
    }
    actual_evidence_by_key = {
        tuple(sorted((row["left_detection_id"], row["right_detection_id"]))): row
        for row in evidence_rows
    }
    evidence_mismatches = [key for key in sorted(set(ref_evidence_by_key) | set(actual_evidence_by_key))
                           if ref_evidence_by_key.get(key) != actual_evidence_by_key.get(key)]
    actual_objects = {node.object_id: {
        "center_W": node.center_W, "extents_m": node.extents_m,
        "first_seen_s": node.first_seen_s, "last_seen_s": node.last_seen_s,
        "first_detection_id": node.first_detection_id, "last_detection_id": node.last_detection_id,
        "class_name": node.class_name, "layer_id": node.layer_id,
    } for node in backend.snapshot()}
    expected_objects = expected_geometry(reference_rows, packets)
    geometry_exact = actual_objects == expected_objects
    report = {
        "scene": scene,
        "input_provenance": input_provenance,
        "offline_reference_assignments_sha256": sha(ref_dir / "assignments_candidate_v2.jsonl"),
        "observations": len(reference_assignments),
        "assignment_parity": {"exact": not assignment_mismatches, "mismatch_count": len(assignment_mismatches),
                              "mismatch_ids": assignment_mismatches[:100]},
        "duplicate_group_and_alias_parity": {"exact": not duplicate_mismatches,
                                             "mismatch_count": len(duplicate_mismatches),
                                             "mismatch_ids": duplicate_mismatches[:100]},
        "ambiguous_observations": sum(row["decision"] == "kept_ambiguous" for row in actual_duplicate_decisions.values()),
        "alias_observations": sum(row["decision"] == "alias_suppressed" for row in actual_duplicate_decisions.values()),
        "deferred_observations": sum(row["decision"] == "defer" for row in actual_decisions),
        "evidence_lineage_parity": {"exact": not evidence_mismatches,
                                    "reference_pair_count": len(reference_evidence),
                                    "adapter_pair_count": len(evidence_rows),
                                    "mismatch_count": len(evidence_mismatches),
                                    "reference_sha256": canonical_hash(reference_evidence),
                                    "adapter_sha256": canonical_hash(evidence_rows)},
        "geometry_update_parity": {"exact": geometry_exact, "reference_track_count": len(expected_objects),
                                   "adapter_track_count": len(actual_objects)},
        "predictor_used_gt_annotations_or_future": False,
    }
    if assignment_mismatches or duplicate_mismatches or evidence_mismatches or not geometry_exact:
        raise RuntimeError(f"M9 backend adapter parity failed for cache {scene}: {report}")
    return report


def main() -> int:
    if sha(CONFIG) != CONFIG_SHA256:
        raise ValueError("locked duplicate-v2 config hash mismatch")
    if OUT.exists():
        raise FileExistsError(f"refusing to overwrite parity output: {OUT}")
    config = json.loads(CONFIG.read_text())
    OUT.mkdir(parents=True)
    reports = [run_scene(scene, cache, config) for scene, cache in P3_ROOTS.items()]
    report = {
        "status": "pass" if all(item["assignment_parity"]["exact"] and
                                 item["duplicate_group_and_alias_parity"]["exact"] and
                                 item["evidence_lineage_parity"]["exact"] and
                                 item["geometry_update_parity"]["exact"] for item in reports) else "fail",
        "scope": "backend-adapter replay only; no M1-M5 native mapping, no GT, no future evidence",
        "config_sha256": sha(CONFIG),
        "backend_source_sha256": hashlib.sha256((ROOT / "src/standalone_sg/objects/u5_backends.py").read_bytes()).hexdigest(),
        "scenes": reports,
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
