#!/usr/bin/env python3
"""Static RB1 evidence-readiness report for one verified hybrid mapping run.

This command reads existing cache/run artifacts only. It does not mutate the
graph, import annotations, infer room labels, or replay perception/mapping.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.contracts.regions import Polygon2D  # noqa: E402
from standalone_sg.semantics.room_belief_readiness import (  # noqa: E402
    group_raw_classes_by_exact_pose,
    validate_xy_membership_scope,
)
from standalone_sg.semantics.room_belief_v1 import (  # noqa: E402
    ConsensusPolicy,
    SemanticObservation,
    aggregate_object_semantics,
    greedy_voxel_support_membership,
)
from standalone_sg.objects.surface_support import voxel_representatives  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object at {path}")
    return value


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def _snapshot(path: Path) -> dict:
    payload = _read_json(path)
    if isinstance(payload.get("snapshot"), dict):
        return payload["snapshot"]
    return payload


def _write_csv(path: Path, rows: list[dict]) -> None:
    columns = [
        "hypothesis_id", "lineage_key", "admission_status", "filter_reason",
        "detection_count", "distinct_frame_count", "distinct_pose_count",
        "raw_class_history", "raw_class_vote_units", "semantic_status",
        "membership_status", "membership_room_id", "support_fraction",
        "runner_up_room_ids", "runner_up_fraction", "outside_fraction", "membership_tied",
        "room_evidence_eligible", "exclusion_reasons",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            values = dict(row)
            membership = row["membership"]
            values.update({
                "distinct_pose_count": row["distinct_pose_count_from_reference"],
                "membership_status": membership["status"],
                "membership_room_id": membership["room_id"],
                "support_fraction": membership["support_fraction"],
                "runner_up_room_ids": membership["runner_up_room_ids"],
                "runner_up_fraction": membership["runner_up_fraction"],
                "outside_fraction": membership["outside_fraction"],
                "membership_tied": membership["tied"],
            })
            values["raw_class_vote_units"] = row["raw_class_vote_report"]["one_unit_per_group_raw_class_support"]
            values["exclusion_reasons"] = row["eligibility_exclusion_reasons"]
            writer.writerow({key: json.dumps(values.get(key), ensure_ascii=False) if key in {
                "raw_class_history", "raw_class_vote_units", "runner_up_room_ids", "exclusion_reasons"} else values.get(key)
                for key in columns})


def build_report(hybrid_root: Path) -> dict:
    hybrid_root = hybrid_root.resolve()
    hybrid_manifest_path = hybrid_root / "hybrid_manifest.json"
    mapping_root = hybrid_root / "mapping"
    hybrid = _read_json(hybrid_manifest_path)
    if hybrid.get("status") != "pass" or hybrid.get("schema") != "local-conceptgraphs-occusg-hybrid-run/v1":
        raise ValueError("input is not a completed hybrid mapping run")

    cache_root = _resolve(hybrid_root, hybrid["cache_root"])
    room_snapshot_path = _resolve(hybrid_root, hybrid["room_snapshot"])
    mapping_manifest_path = mapping_root / "run_manifest.json"
    export_root = mapping_root / "export"
    export_manifest_path = export_root / "export_manifest.json"
    lineages_path = mapping_root / "lineage_stages.json"
    diagnostic_path = mapping_root / "diagnostic" / "diagnostic_manifest.json"
    evidence_path = mapping_root / "diagnostic" / "evidence_manifest.json"
    final_manifest_path = hybrid_root / "final_graph" / "manifest.json"
    final_snapshot_path = hybrid_root / "final_graph" / "unified_snapshot.json"
    capture_manifest_path = cache_root / "capture" / "capture_manifest.json"
    p2_index_path = cache_root / "detection_2d" / "detection_2d_index.json"

    checked_files = {
        "hybrid_manifest": hybrid_manifest_path,
        "mapping_run_manifest": mapping_manifest_path,
        "object_export_manifest": export_manifest_path,
        "room_snapshot": room_snapshot_path,
        "capture_manifest": capture_manifest_path,
        "p2_index": p2_index_path,
    }
    for label, path in checked_files.items():
        if not path.is_file():
            raise ValueError(f"required {label} is missing: {path}")

    mapping_manifest = _read_json(mapping_manifest_path)
    export_manifest = _read_json(export_manifest_path)
    lineages = _read_json(lineages_path)
    diagnostics = _read_json(diagnostic_path)
    evidence_manifest = _read_json(evidence_path)
    final_manifest = _read_json(final_manifest_path)
    capture_manifest = _read_json(capture_manifest_path)
    p2_index = _read_json(p2_index_path)
    room_snapshot = _snapshot(room_snapshot_path)
    final_snapshot = _snapshot(final_snapshot_path)

    expected_hashes = {
        "mapping_run_manifest": hybrid.get("mapping_run_manifest_sha256"),
        "room_snapshot": hybrid.get("room_snapshot_sha256"),
        "capture_manifest": hybrid.get("capture_manifest_sha256"),
    }
    actual_hashes = {name: _sha256(path) for name, path in checked_files.items()}
    for label, expected in expected_hashes.items():
        if expected != actual_hashes[label]:
            raise ValueError(f"{label} hash differs from the hybrid manifest")
    if export_manifest.get("schema") != "conceptgraphs-object-export/v1":
        raise ValueError("unsupported object export schema")

    scene_ids = {
        "hybrid": hybrid.get("scene_id"),
        "capture": capture_manifest.get("scene_id"),
        "object_export": export_manifest.get("scene_id"),
        "room_snapshot": room_snapshot.get("scene_id"),
        "final_snapshot": final_snapshot.get("scene_id"),
    }
    sequence_ids = {
        "hybrid": hybrid.get("sequence_id"),
        "capture": capture_manifest.get("sequence_id"),
        "object_export": export_manifest.get("sequence_id"),
        "room_snapshot": room_snapshot.get("sequence_id"),
        "final_snapshot": final_snapshot.get("sequence_id"),
    }
    if len(set(scene_ids.values())) != 1 or None in scene_ids.values():
        raise ValueError(f"scene ID mismatch across inputs: {scene_ids}")
    if len(set(sequence_ids.values())) != 1 or None in sequence_ids.values():
        raise ValueError(f"sequence ID mismatch across inputs: {sequence_ids}")

    p2_detections: dict[str, dict] = {}
    for frame in p2_index.get("frames", []):
        if frame.get("scene_id") != hybrid["scene_id"] or frame.get("sequence_id") != hybrid["sequence_id"]:
            raise ValueError("P2 frame scene/sequence differs from hybrid run")
        for detection in frame.get("detections", []):
            observation_id = detection.get("detection_id")
            if not observation_id or observation_id in p2_detections:
                raise ValueError("P2 detection IDs must be present and unique")
            p2_detections[observation_id] = detection

    if lineages.get("source_cache_capture_manifest_sha256") != actual_hashes["capture_manifest"]:
        raise ValueError("lineage capture hash does not match selected cache")
    if lineages.get("input_observation_ids") != mapping_manifest.get("input_observation_ids", lineages.get("input_observation_ids")):
        # Older mapping manifests need not repeat the input list; if they do, require exact ordering.
        raise ValueError("mapping and lineage input observation order differs")
    if set(lineages.get("input_observation_ids", [])) != set(evidence_manifest.get("observations", {})):
        raise ValueError("lineage and evidence manifest observation populations differ")
    if set(lineages.get("input_observation_ids", [])) - set(p2_detections):
        raise ValueError("some mapped observation IDs are absent from P2 index")

    room_nodes = [node for node in room_snapshot.get("nodes", []) if node.get("kind") == "room"]
    if not room_nodes:
        raise ValueError("room snapshot contains no room geometry nodes")
    room_polygons: dict[str, Polygon2D] = {}
    room_revisions: dict[str, int] = {}
    room_floor_ids: dict[str, str] = {}
    for node in room_nodes:
        attrs = node.get("attributes", {})
        room_id = node.get("node_id")
        if not room_id or not attrs.get("outer_ring"):
            raise ValueError("room node lacks ID or 2D polygon geometry")
        room_polygons[room_id] = Polygon2D(tuple(map(tuple, attrs["outer_ring"])),
                                           tuple(tuple(map(tuple, h)) for h in attrs.get("holes", [])))
        room_revisions[room_id] = int(attrs.get("revision", 0))
        floor = attrs.get("floor_id", attrs.get("layer_id"))
        if isinstance(floor, str) and floor:
            room_floor_ids[room_id] = floor

    agent_nodes = [node for node in room_snapshot.get("nodes", []) if node.get("kind") == "agent"]
    agent_z = sorted({round(float(node["attributes"]["pose_W"][2]), 6) for node in agent_nodes
                      if len(node.get("attributes", {}).get("pose_W", [])) == 3})
    source_object_layers = sorted({node.get("attributes", {}).get("layer_id") for node in room_snapshot.get("nodes", [])
                                   if node.get("kind") == "object" and node.get("attributes", {}).get("layer_id")})
    # The snapshot's agent Z can vary slightly along a navigable route even
    # when all mapped object nodes carry one explicit floor/layer.  Use that
    # existing layer metadata as the scope proof; do not invent a floor ID
    # from Z or treat the route's Z samples as independent floors.
    single_floor_verified = len(source_object_layers) == 1 and bool(source_object_layers)
    observed_floor_ids = source_object_layers
    if not observed_floor_ids and len(agent_z) == 1:
        # No floor ID is fabricated from Z; an explicit elevation provenance key is used only as a scope token.
        observed_floor_ids = [f"observed_z_m:{agent_z[0]:.6f}"]
    membership_scope = validate_xy_membership_scope(
        sorted(room_polygons), observed_floor_ids=observed_floor_ids,
        room_floor_ids=room_floor_ids or None,
        single_floor_scope_verified=single_floor_verified,
        scope_provenance=(f"capture_manifest_sha256={actual_hashes['capture_manifest']}; "
                          f"room_snapshot_sha256={actual_hashes['room_snapshot']}; "
                          f"agent_base_z_samples={agent_z}; source_object_layers={source_object_layers}; "
                          "single_floor_scope_basis=all_snapshot_object_nodes_share_one_explicit_layer"),
    )
    if not membership_scope["candidate_room_ids"]:
        raise ValueError("XY membership has no room candidates in the observed floor scope")
    room_polygons = {key: room_polygons[key] for key in membership_scope["candidate_room_ids"]}
    room_revisions = {key: room_revisions[key] for key in room_polygons}

    consensus_config_path = ROOT / "configs" / "room_semantics" / "consensus_v1.proposal.json"
    belief_config_path = ROOT / "configs" / "room_semantics" / "room_belief_v1.proposal.json"
    consensus_config = _read_json(consensus_config_path)
    belief_config = _read_json(belief_config_path)
    map_contract = _read_json(mapping_root / "experiment_contract.json")
    voxel_size = float(map_contract["effective_config"]["downsample_voxel_size"])
    voxel_grid_origin = [0.0, 0.0, 0.0]
    if belief_config.get("membership", {}).get("voxel_size_m") not in (None, voxel_size):
        raise ValueError("RB1 proposal voxel size differs from mapping provenance")

    policy = ConsensusPolicy(
        policy_id=consensus_config["policy_id"],
        view_group_policy_id="exact_camera_pose_matrix_v1",
        minimum_view_groups=int(consensus_config["minimum_view_groups"]),
        minimum_top_share=float(consensus_config["minimum_top_share"]),
        minimum_margin=float(consensus_config["minimum_margin"]),
    )
    diagnostic_by_key = {item["lineage_key"]: item for item in diagnostics.get("objects", [])}
    export_by_key = {item["export_id"]: item for item in export_manifest.get("objects", [])}
    membership_manifest = final_manifest.get("membership", {})
    membership_by_graph_id = dict(membership_manifest)
    observations_meta = evidence_manifest.get("observations", {})
    output_objects: list[dict] = []
    diagnostic_dir = mapping_root / "diagnostic"
    geometry_counts = {room_id: {"hypotheses_with_xy_support": 0,
                                 "eligible_semantic_objects": 0,
                                 "eligible_categories": []} for room_id in room_polygons}
    a_table_paths = sorted((ROOT / "configs" / "room_semantics").glob("*a_table*.json"))
    a_table_available = bool(a_table_paths)

    for hypothesis in diagnostics.get("objects", []):
        key = hypothesis["lineage_key"]
        status = hypothesis["status_after_filter"]
        observation_rows = []
        semantic_observations = []
        for observation_id in hypothesis.get("observation_ids", []):
            meta = observations_meta.get(observation_id)
            if not isinstance(meta, dict):
                raise ValueError(f"missing pose/raw-class metadata for {observation_id}")
            detection = p2_detections[observation_id]
            raw_class = detection.get("class_raw", meta.get("raw_class"))
            if raw_class != meta.get("raw_class"):
                raise ValueError(f"raw class mismatch between P2 and mapping evidence for {observation_id}")
            pose = meta.get("camera_pose_W_Copt")
            pose_id = __import__("standalone_sg.semantics.room_belief_readiness", fromlist=["exact_pose_group_id"]).exact_pose_group_id(pose)
            frame_id = observation_id.split(":", 1)[0]
            observation_rows.append({"observation_id": observation_id, "frame_id": frame_id,
                                     "frame_index": int(meta["frame_index"]), "raw_class": raw_class,
                                     "p2_class_canonical_unvetted": detection.get("class_canonical"),
                                     "camera_pose": pose, "view_group_id": pose_id,
                                     "alias_marker_available": "alias_of" in detection})
            semantic_observations.append(SemanticObservation(
                observation_id=observation_id, frame_id=frame_id, view_group_id=pose_id,
                raw_class=raw_class, category=raw_class,
                admission_status=status if status in ("retained", "filtered") else "deferred",
                alias_of=detection.get("alias_of"),
            ))
        grouped = group_raw_classes_by_exact_pose(observation_rows)
        consensus = aggregate_object_semantics(key, semantic_observations,
                                              semantic_revision=1, policy=policy)
        cloud_rel = hypothesis.get("cloud_npz")
        if not cloud_rel:
            raise ValueError(f"diagnostic geometry path is absent for {key}")
        cloud_path = diagnostic_dir / cloud_rel
        if not cloud_path.is_file():
            raise ValueError(f"diagnostic cloud is missing: {cloud_path}")
        cloud_sha = _sha256(cloud_path)
        if hypothesis.get("cloud_sha256") and cloud_sha != hypothesis["cloud_sha256"]:
            raise ValueError(f"diagnostic cloud hash mismatch for {key}")
        with np.load(cloud_path, allow_pickle=False) as cloud:
            if "xyz" not in cloud.files:
                raise ValueError(f"diagnostic cloud has no xyz array: {cloud_path}")
            xyz = np.asarray(cloud["xyz"], dtype=np.float32)
        if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
            raise ValueError(f"diagnostic object cloud is not finite Nx3: {key}")
        reps = voxel_representatives(xyz, voxel_size)
        membership = greedy_voxel_support_membership(
            f"hypothesis:{key}", reps.tolist(), room_polygons,
            voxel_size_m=voxel_size, voxel_grid_origin_xyz_m=voxel_grid_origin,
            geometry_revisions=room_revisions,
        ).as_dict()
        support_counts = membership["support_counts"]
        winner_count = max(support_counts.values(), default=0)
        runner_up_count = max((count for count in support_counts.values() if count < winner_count), default=0)
        membership["runner_up_room_ids"] = sorted(
            room_id for room_id, count in support_counts.items()
            if runner_up_count > 0 and count == runner_up_count
        )
        membership["support_fraction_denominator"] = int(len(reps))
        membership["support_fraction_semantics"] = (
            "per-room voxel representatives / all object voxel representatives; "
            "overlapping XY polygons can make room fractions non-exclusive"
        )
        if membership["status"] == "assigned" and membership["room_id"] in geometry_counts:
            geometry_counts[membership["room_id"]]["hypotheses_with_xy_support"] += 1

        export = export_by_key.get(key)
        final_graph_id = f"object:conceptgraphs:cg_{key}"
        existing_membership = membership_by_graph_id.get(final_graph_id)
        retained = status == "retained"
        reasons = []
        if not retained:
            reasons.append(hypothesis.get("filter_reason", "filtered_by_reference_postprocess"))
        if consensus.status != "stable":
            reasons.append(f"semantic_status_{consensus.status}")
        if membership["status"] != "assigned":
            reasons.append(f"xy_membership_{membership['status']}")
        if not a_table_available:
            reasons.append("room_A_table_unavailable")
        eligible = retained and consensus.status == "stable" and membership["status"] == "assigned" and a_table_available
        if eligible:
            geometry_counts[membership["room_id"]]["eligible_semantic_objects"] += 1
            raw_category = consensus.category
            geometry_counts[membership["room_id"]]["eligible_categories"].append(raw_category)
        output_objects.append({
            "hypothesis_id": hypothesis.get("hypothesis_id"),
            "lineage_key": key,
            "admission_status": status,
            "filter_reason": hypothesis.get("filter_reason"),
            "postprocess_disposition": hypothesis.get("postprocess_disposition"),
            "final_object_keys": hypothesis.get("final_object_keys", []),
            "detection_count": int(hypothesis.get("detection_count", len(observation_rows))),
            "distinct_frame_count": int(hypothesis.get("distinct_frame_count", 0)),
            "distinct_pose_count_from_reference": int(hypothesis.get("distinct_pose_count", 0)),
            "raw_class_history": [row["raw_class"] for row in observation_rows],
            "raw_class_vote_report": grouped,
            "semantic_status": "excluded_filtered" if not retained else consensus.status,
            "semantic_category_raw_exact": consensus.category if retained else None,
            "semantic_category_support_by_pose_unit": dict(consensus.category_support) if retained else {},
            "semantic_policy": policy.as_dict(),
            "semantic_observations": [row["observation_id"] for row in observation_rows],
            "semantic_excluded_observations": [list(item) for item in consensus.excluded_observations],
            "existing_unified_graph_membership": existing_membership,
            # RB1 readiness uses its locked greedy voxel support policy as the
            # primary membership result. The adapter membership is retained
            # separately for provenance and is not allowed to override it.
            "membership": membership,
            "legacy_adapter_membership": existing_membership,
            "rb1_xy_voxel_support_diagnostic": membership,
            "membership_policy_id": "rb1_greedy_voxel_support_v1",
            "room_evidence_eligible": eligible,
            "eligibility_exclusion_reasons": reasons,
            "geometry_source": str(cloud_path.relative_to(hybrid_root)),
            "geometry_sha256": cloud_sha,
            "point_count": int(len(xyz)),
            "voxel_representative_count": int(len(reps)),
            "export_raw_class_history": export.get("reference_class_history", []) if export else [],
            "observation_records": observation_rows,
        })

    rooms = []
    for room_id, counts in sorted(geometry_counts.items()):
        categories = sorted(set(counts["eligible_categories"]))
        rooms.append({
            "room_id": room_id,
            "geometry_revision": room_revisions[room_id],
            "semantic_label_status": "not_computed_A_table_unavailable" if not a_table_available else "readiness_only",
            "candidate_labels": [],
            "belief": None,
            "xy_geometry_supported_hypothesis_count_including_filtered_diagnostics": counts["hypotheses_with_xy_support"],
            "eligible_semantic_object_count": counts["eligible_semantic_objects"],
            "eligible_category_count": len(categories),
            "eligible_categories": categories,
            "unknown_or_ambiguous_evidence_is_not_a_label": True,
        })

    report = {
        "schema": "room-belief-evidence-readiness/v1",
        "report_kind": "static_evidence_readiness_not_quality_or_causal_evaluation",
        "status": "blocked_pending_real_A_table" if not a_table_available else "readiness_only",
        "hybrid_run_id": hybrid["run_id"],
        "source_display_label": hybrid.get("source_display_label"),
        "source_kind": export_manifest.get("provenance", {}).get("source_kind"),
        "scene_id": hybrid["scene_id"],
        "sequence_id": hybrid["sequence_id"],
        "input_scope": {
            "cache_root": str(cache_root), "frame_count": int(capture_manifest.get("frame_count", 0)),
            "mapped_observation_count": int(mapping_manifest.get("observations", 0)),
            "pre_filter_hypotheses": len(diagnostics.get("objects", [])),
            "retained_hypotheses": sum(o["admission_status"] == "retained" for o in output_objects),
            "filtered_hypotheses": sum(o["admission_status"] == "filtered" for o in output_objects),
            "room_snapshot": str(room_snapshot_path), "room_count": len(room_polygons),
            "room_snapshot_revision": room_snapshot.get("revision"),
            "room_geometry_adjacency_unchanged": hybrid.get("room_geometry_adjacency_unchanged"),
        },
        "provenance": {
            "source_cache_id": export_manifest.get("provenance", {}).get("source_cache_id"),
            "capture_manifest_sha256": actual_hashes["capture_manifest"],
            "p2_index_sha256": actual_hashes["p2_index"],
            "mapping_run_manifest_sha256": actual_hashes["mapping_run_manifest"],
            "object_export_manifest_sha256": _sha256(export_manifest_path),
            "room_snapshot_sha256": actual_hashes["room_snapshot"],
            "final_graph_manifest_sha256": _sha256(final_manifest_path),
            "effective_mapping_config_sha256": map_contract.get("runner_sha256"),
            "image_feature_status": "cached image features used by mapping core; not room semantic evidence",
            "text_feature_status": "cached detector-text features present in hybrid run; not Qwen A table",
        },
        "membership_scope": {
            **membership_scope,
            "room_geometry_kind": "2D XY polygons; room nodes have no floor/layer/Z bounds",
            "observed_agent_base_z_m": agent_z,
            "observed_source_object_layer_ids": source_object_layers,
            "room_floor_metadata_present": bool(room_floor_ids),
            "voxel_size_m": voxel_size,
            "voxel_size_source": "locked ConceptGraphs experiment_contract.effective_config.downsample_voxel_size",
            "voxel_grid_origin_xyz_m": voxel_grid_origin,
            "voxel_grid_origin_source": "existing voxel_representatives helper uses floor(xyz / voxel_size), i.e. world origin zero; explicit RB1 measurement convention, not a cloud rewrite",
            "point_frame_units": export_manifest.get("coordinate_frame") + "; meters; Z-up; room polygons use XY",
            "membership_is_diagnostic_only": True,
            "membership_policy_id": "rb1_greedy_voxel_support_v1",
            "membership_policy": "Among verified-scope room polygons, choose the room with maximum positive voxel-representative support; zero total room support is unassigned. Ties are recorded and resolved by the function's deterministic rule.",
            "membership_warning": "The primary membership in this report is recomputed from object cloud and room polygon geometry under RB1 greedy voxel support. The prior unified-graph adapter status is historical provenance only; this report does not mutate that graph or establish multi-floor safety.",
        },
        "policy": {
            "consensus_config": str(consensus_config_path.relative_to(ROOT)),
            "consensus_config_sha256": _sha256(consensus_config_path),
            "consensus_approval_status": consensus_config.get("approval_status"),
            "view_group_policy_id": "exact_camera_pose_matrix_v1",
            "view_group_semantics": "exact repeated camera transform shares one vote; different transforms form different pose groups; no statistical independence claim; aliases are not represented in this mapper lineage and cannot be counted as new views",
            "minimum_view_groups": policy.minimum_view_groups,
            "minimum_top_share": policy.minimum_top_share,
            "minimum_margin": policy.minimum_margin,
            "belief_config": str(belief_config_path.relative_to(ROOT)),
            "belief_config_sha256": _sha256(belief_config_path),
            "belief_approval_status": belief_config.get("approval_status"),
            "room_thresholds_are_calibrated": False,
            "raw_class_handling": "exact detector raw class strings only; P2 canonical aliases are displayed for provenance but not used for voting; no synonyms inferred",
        },
        "a_table": {
            "status": "missing" if not a_table_available else "present_not_scored_by_this_command",
            "paths": [str(path.relative_to(ROOT)) for path in a_table_paths],
            "predictor_room_labels_generated": False,
            "quality_claim": False,
        },
        "objects": output_objects,
        "rooms": rooms,
        "limitations": [
            "This is a static readiness report on a previously viewed five-frame development cache, not a holdout or causal online evaluation.",
            "The matching room snapshot has one 2D room node only; it cannot evaluate semantic discrimination among multiple functional rooms.",
            "The snapshot has no room floor IDs or Z bounds. The verified single-elevation scope applies only to this observed sequence/snapshot, not all floors of the HM3D scene.",
            "A table is absent, so no room belief or room accuracy is computed.",
            "Threshold proposal values are reported as development-only and uncalibrated.",
            "Filtered hypotheses are shown for diagnostics and excluded from semantic evidence.",
        ],
    }
    return report


def markdown_report(report: dict) -> str:
    lines = [
        "# Room Belief v1 — static evidence readiness",
        "",
        f"- Status: **{report['status']}**",
        f"- Scope: {report['report_kind']}",
        f"- Run: `{report['hybrid_run_id']}` | Scene: `{report['scene_id']}` | Sequence: `{report['sequence_id']}`",
        f"- Frames/observations: {report['input_scope']['frame_count']} / {report['input_scope']['mapped_observation_count']}",
        f"- Hypotheses: {report['input_scope']['pre_filter_hypotheses']} before filter, {report['input_scope']['retained_hypotheses']} retained, {report['input_scope']['filtered_hypotheses']} filtered",
        f"- Room nodes in linked snapshot: {report['input_scope']['room_count']} (geometry-only; no functional room labels)",
        f"- Room A table: **{report['a_table']['status']}**; room belief not computed.",
        "",
        "## Object readiness",
        "",
        "| Hypothesis | Admission | Raw labels by exact-pose units | Pose groups | Semantic status | RB1 greedy membership | Legacy adapter membership | Room-evidence eligible |",
        "|---|---|---:|---:|---|---|---|---|",
    ]
    for item in report["objects"]:
        raw = ", ".join(f"{key}: {value}" for key, value in item["raw_class_vote_report"]["one_unit_per_group_raw_class_support"].items()) or "—"
        legacy = item["legacy_adapter_membership"] or {}
        legacy_text = f"{legacy.get('status')} / {legacy.get('room_id')} (legacy)" if legacy else "not in final graph"
        membership = item["membership"]
        greedy_text = (f"{membership['status']} / {membership['room_id']} "
                       f"(max {membership['support_fraction']:.3f}, runner-up {membership['runner_up_fraction']:.3f}, "
                       f"outside {membership['outside_fraction']:.3f}, tie={membership['tied']})")
        lines.append(f"| `{item['hypothesis_id']}` | {item['admission_status']} | {raw} | {item['raw_class_vote_report']['view_group_count']} | {item['semantic_status']} | {greedy_text} | {legacy_text} | {'yes' if item['room_evidence_eligible'] else 'no'} |")
    lines += ["", "## Per-room readiness", "", "| Room | XY-supported hypotheses (diagnostic) | Eligible objects | Eligible categories | Label status |", "|---|---:|---:|---:|---|"]
    for room in report["rooms"]:
        lines.append(f"| `{room['room_id']}` | {room['xy_geometry_supported_hypothesis_count_including_filtered_diagnostics']} | {room['eligible_semantic_object_count']} | {room['eligible_category_count']} | {room['semantic_label_status']} |")
    lines += ["", "## Membership policy", "",
              "RB1 membership above is computed from the cached object cloud and the linked room polygon: maximum positive voxel support selects a room; zero support is unassigned. Support fractions use all object voxel representatives as the denominator. Legacy adapter membership is shown only for provenance. The measured XY scope is limited to the verified single observed elevation; this does not establish multi-floor safety.",
              "", "## Limits", ""]
    lines.extend(f"- {item}" for item in report["limitations"])
    lines += ["", "Detailed observations, class histories, support fractions and hashes are in `readiness.json` and `objects.csv`.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hybrid-run", type=Path, required=True, help="Existing hybrid run directory")
    parser.add_argument("--output", type=Path, required=True, help="New, empty output directory")
    args = parser.parse_args(argv)
    if args.output.exists():
        if (args.output / "manifest.json").exists():
            raise SystemExit(f"Refusing to overwrite completed output: {args.output}")
        allowed_partial = {"readiness.json", "readiness.md", "objects.csv"}
        present = {path.name for path in args.output.iterdir()}
        if not present <= allowed_partial:
            raise SystemExit(f"Refusing to overwrite non-report files in output: {args.output}")
        print(f"Resuming incomplete report output without manifest: {args.output}")
    else:
        args.output.mkdir(parents=True)
    report = build_report(args.hybrid_run)
    json_path = args.output / "readiness.json"
    md_path = args.output / "readiness.md"
    csv_path = args.output / "objects.csv"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    md_path.write_text(markdown_report(report))
    _write_csv(csv_path, report["objects"])
    manifest = {
        "schema": "room-belief-evidence-readiness-artifact/v1",
        "report_kind": report["report_kind"],
        "hybrid_run_id": report["hybrid_run_id"],
        "scene_id": report["scene_id"], "sequence_id": report["sequence_id"],
        "files_sha256": {name: _sha256(args.output / name) for name in ("readiness.json", "readiness.md", "objects.csv")},
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "output": str(args.output.resolve()),
                      "objects": len(report["objects"]), "rooms": len(report["rooms"]),
                      "A_table": report["a_table"]["status"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
