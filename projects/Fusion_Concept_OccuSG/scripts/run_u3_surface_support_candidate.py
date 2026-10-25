#!/usr/bin/env python3
"""Causal offline A/B for accumulated surface support on fixed P3 inputs."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import resource
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.compare_association_profiles import assignment_map, read_jsonl  # noqa: E402
from standalone_sg.config.schema import ObjectAssociationConfig  # noqa: E402
from standalone_sg.contracts.objects import Detection3D, DetectionBatch  # noqa: E402
from standalone_sg.objects.association import ObjectAssociationManager  # noqa: E402
from standalone_sg.objects.object_geometry_fusion import ObjectGeometryFusion  # noqa: E402
from standalone_sg.objects.profile_comparison import pairwise_identity_metrics, partition_delta  # noqa: E402
from standalone_sg.objects.profile_comparison import partition_members  # noqa: E402
from standalone_sg.objects.same_frame_duplicates_v2 import canonicalize_same_frame_duplicates_v2  # noqa: E402
from standalone_sg.objects.u3_experimental import (  # noqa: E402
    ExperimentalAssociation, RawClassEvidenceHistory, U3Observation, exclude_same_frame_aliases,
)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows), encoding="utf-8")


def _d3_signature(batch: dict) -> dict:
    return {"frame_id": batch["frame_id"], "timestamp_s": batch["timestamp_s"],
            "status": batch["status"], "detections": batch.get("detections", [])}


def load_locked_inputs(input_root: Path, p3_roots: list[Path], source_replay: Path | None, base_replay: Path | None,
                       candidate_config: Path, fusion_config: Path, k4_reference: Path | None = None) -> dict:
    capture_manifest_path = input_root / "capture/capture_manifest.json"
    p2_index_path = input_root / "detection_2d/detection_2d_index.json"
    p3_cache_path = input_root / "detection_3d/detection_3d.jsonl"
    capture_manifest = read_json(capture_manifest_path)
    p2_index = read_json(p2_index_path)
    p3_cache_rows = read_jsonl(p3_cache_path)
    p3_manifest_path = input_root / "detection_3d/detection_3d_manifest.json"
    p3_manifest = read_json(p3_manifest_path)
    if p2_index.get("status") != "pass":
        raise ValueError("P2 cache status is not pass")
    if p2_index.get("capture_manifest_sha256") != sha(capture_manifest_path):
        raise ValueError("P2 cache/capture manifest hash mismatch")
    if p3_manifest.get("status") != "pass":
        raise ValueError("authoritative P3 cache status is not pass")
    if p3_manifest.get("capture_manifest_sha256") != sha(capture_manifest_path):
        raise ValueError("authoritative P3 cache/capture manifest hash mismatch")
    if p3_manifest.get("detection_2d_cache_sha256") != sha(p2_index_path):
        raise ValueError("authoritative P3 cache/P2 index hash mismatch")
    frame_index = {str(row["frame_id"]): int(row["frame_index"]) for row in capture_manifest["frames"]}
    if len(frame_index) != len(capture_manifest["frames"]):
        raise ValueError("capture manifest contains duplicate frame IDs")
    p3_frame_order = [str(batch["frame_id"]) for batch in p3_cache_rows]
    if len(p3_frame_order) != len(set(p3_frame_order)):
        raise ValueError("authoritative P3 cache contains duplicate frame batches")
    if any(frame_id not in frame_index for frame_id in p3_frame_order):
        raise ValueError("authoritative P3 cache contains a frame absent from capture manifest")
    if [frame_index[frame_id] for frame_id in p3_frame_order] != sorted(frame_index[frame_id] for frame_id in p3_frame_order):
        raise ValueError("authoritative P3 frame ordering is not chronological by capture index")
    raw_d3 = {str(row["detection_id"]): row for batch in p3_cache_rows for row in batch.get("detections", [])}
    raw_frame_by_id = {str(row["detection_id"]): str(batch["frame_id"])
                       for batch in p3_cache_rows for row in batch.get("detections", [])}
    raw_ids = set(raw_d3)
    if len(raw_ids) != sum(len(batch.get("detections", [])) for batch in p3_cache_rows):
        raise ValueError("duplicate raw P3 observation ID in authoritative cache")

    evidence_by_id: dict[str, tuple[Path, dict]] = {}
    p3_batch_by_frame: dict[str, dict] = {}
    p3_manifest_hashes = []
    for p3_root in p3_roots:
        p3_root = p3_root.resolve()
        manifest = read_json(p3_root / "detection_3d_manifest.json")
        if manifest.get("status") != "pass":
            raise ValueError(f"P3 evidence output is not complete: {p3_root}")
        if manifest.get("capture_manifest_sha256") != sha(capture_manifest_path):
            raise ValueError(f"P3 evidence capture hash mismatch: {p3_root}")
        if manifest.get("detection_2d_cache_sha256") != sha(p2_index_path):
            raise ValueError(f"P3 evidence P2 hash mismatch: {p3_root}")
        sidecar_status = manifest.get("object_evidence_sidecar", {})
        if sidecar_status.get("status") != "pass" or sidecar_status.get("complete", True) is not True:
            raise ValueError(f"P3 evidence sidecar is partial/missing: {p3_root}")
        p3_manifest_hashes.append({"root": str(p3_root), "manifest_sha256": sha(p3_root / "detection_3d_manifest.json")})
        for batch in read_jsonl(p3_root / "detection_3d.jsonl"):
            frame_id = str(batch["frame_id"])
            if frame_id in p3_batch_by_frame:
                raise ValueError(f"overlapping P3 partial outputs include frame {frame_id}")
            p3_batch_by_frame[frame_id] = batch
        for row in read_jsonl(p3_root / "object_evidence/index.jsonl"):
            oid = str(row["observation_id"])
            if oid in evidence_by_id:
                raise ValueError(f"duplicate direct P3 sidecar record {oid}")
            if row.get("point_selection_provenance", "").startswith("direct_from_current_P3") is not True:
                raise ValueError(f"P3 evidence is not directly selected-point provenance: {oid}")
            if row.get("capture_manifest_sha256") != sha(capture_manifest_path):
                raise ValueError(f"sidecar capture provenance mismatch: {oid}")
            if row.get("detection_2d_index_sha256") != sha(p2_index_path):
                raise ValueError(f"sidecar P2 provenance mismatch: {oid}")
            evidence_by_id[oid] = (p3_root, row)

    if set(evidence_by_id) != raw_ids:
        raise ValueError(f"K4 requires complete evidence for all accepted P3 observations: "
                         f"missing={len(raw_ids - set(evidence_by_id))}, extra={len(set(evidence_by_id) - raw_ids)}")
    if set(p3_batch_by_frame) != {str(batch["frame_id"]) for batch in p3_cache_rows}:
        raise ValueError("P3 partial outputs do not cover the authoritative input frame population exactly")
    for batch in p3_cache_rows:
        frame_id = str(batch["frame_id"])
        if _d3_signature(p3_batch_by_frame[frame_id]) != _d3_signature(batch):
            raise ValueError(f"P3 sidecar extraction changed Detection3D output for frame {frame_id}")

    for oid, (p3_root, row) in evidence_by_id.items():
        point_path = p3_root / "object_evidence" / row["point_file"]
        if sha(point_path) != row.get("point_file_sha256"):
            raise ValueError(f"P3 point evidence checksum mismatch: {oid}")
        with np.load(point_path, allow_pickle=False) as item:
            xyz = np.asarray(item["xyz_W"])
            pixels = np.asarray(item["pixel_rc"])
            rgb = np.asarray(item["rgb_u8"])
            pose = np.asarray(item["T_W_Copt"])
        if (xyz.shape != (int(row["point_count"]), 3) or pixels.shape != (len(xyz), 2)
                or rgb.shape != xyz.shape or pose.shape != (4, 4)
                or not np.isfinite(xyz).all() or not np.isfinite(pose).all()):
            raise ValueError(f"P3 sidecar shape/finite contract failed: {oid}")
        if int(raw_d3[oid]["provider_metadata"]["cluster_points"]) != len(xyz):
            raise ValueError(f"P3 sidecar selected-point count differs from cached Detection3D: {oid}")

    p2_by_id = {str(det["detection_id"]): det for frame in p2_index["frames"] for det in frame.get("detections", [])}
    if set(raw_ids) - set(p2_by_id):
        raise ValueError("authoritative P3 observations are missing from P2 index")
    for oid, d3 in raw_d3.items():
        batch_frame = raw_frame_by_id[oid]
        if oid.rsplit(":det-", 1)[0] != batch_frame:
            raise ValueError(f"P3 detection frame and full observation ID disagree: {oid}")
        if oid not in p2_by_id:
            raise ValueError(f"P2 record missing for P3 observation {oid}")

    cfg = read_json(candidate_config)
    fusion_cfg = read_json(fusion_config)
    duplicate_config_path = ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json"
    duplicate_config = read_json(duplicate_config_path)

    # Build source and geometry-only-dedup-v2 assignments directly from the
    # ordered P3 packets. Optional native replay references are loaded below
    # only for parity verification; they never feed the offline predictor.
    raw_observations: list[U3Observation] = []
    masks: dict[str, np.ndarray] = {}
    support: dict[str, dict] = {}
    obs_by_frame: dict[int, list[U3Observation]] = defaultdict(list)
    for batch in p3_cache_rows:
        frame_id = str(batch["frame_id"])
        if frame_id not in frame_index:
            raise ValueError(f"P3 frame missing from capture manifest: {frame_id}")
        frame_idx = frame_index[frame_id]
        for det in batch.get("detections", []):
            oid = str(det["detection_id"])
            p2det = p2_by_id[oid]
            mask_path = input_root / "detection_2d" / p2det["mask_file"]
            if sha(mask_path) != p2det.get("mask_sha256"):
                raise ValueError(f"P2 mask hash mismatch: {oid}")
            masks[oid] = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
            obs = U3Observation(
                detection_id=oid, frame_id=frame_id, timestamp_s=float(det["timestamp_s"]),
                center=tuple(float(x) for x in det["center_W"]),
                extents=tuple(float(x) for x in det["extents_m"]) if det.get("extents_m") else None,
                layer_id=str(det.get("layer_id", "floor_0")), class_name=det.get("class_name"),
                feature=None, mask_hash=str(p2det["mask_sha256"]))
            raw_observations.append(obs)
            obs_by_frame[frame_idx].append(obs)
            metadata = det.get("provider_metadata", {})
            support[oid] = {"valid_depth_pixels": int(metadata.get("valid_depth_pixels", 0)),
                            "cluster_points": int(metadata.get("cluster_points", 0)),
                            "detector_score": float(p2det.get("score_raw", p2det.get("score", 0.0)))}

    canonical_observations, alias_rows, duplicate_ledger = canonicalize_same_frame_duplicates_v2(
        raw_observations, masks, support, duplicate_config["candidate"])
    aliases = {str(row["detection_id"]): str(row["duplicate_of"]) for row in alias_rows}
    if len(aliases) != len(alias_rows) or set(aliases) & set(aliases.values()):
        raise ValueError("dedup-v2 produced duplicate/cyclic alias ownership")
    for alias_id, canonical_id in aliases.items():
        if alias_id not in raw_ids or canonical_id not in raw_ids:
            raise ValueError(f"dedup-v2 alias references unknown P3 observation: {alias_id}")
        if alias_id.rsplit(":det-", 1)[0] != canonical_id.rsplit(":det-", 1)[0]:
            raise ValueError(f"dedup-v2 alias crosses frame: {alias_id} -> {canonical_id}")

    source_assignments: dict[str, str | None] = {}
    source_decisions: dict[str, dict] = {}
    source_cfg = duplicate_config["baseline_config"]["source_manager"]
    source_manager = ObjectAssociationManager(ObjectAssociationConfig(
        same_class_radius_m=float(source_cfg["same_class_radius_m"]),
        same_pose_suppression_cap_m=float(source_cfg["same_pose_suppression_cap_m"])))
    # Preserve both frame and detection order as serialized in authoritative P3.
    for batch in p3_cache_rows:
        frame_id = str(batch["frame_id"])
        dets = []
        for src in batch.get("detections", []):
            oid = str(src["detection_id"])
            dets.append(Detection3D(
                detection_id=oid, timestamp_s=float(src["timestamp_s"]),
                center_W=tuple(float(x) for x in src["center_W"]),
                center_method=str(src.get("center_method", "saved_p3")),
                class_name=src.get("class_name"), class_id=str(src.get("class_id", "")) or None,
                confidence=src.get("confidence"), frame_id="world",
                layer_id=str(src.get("layer_id", "floor_0")),
                extents_m=tuple(float(x) for x in src["extents_m"]) if src.get("extents_m") else None,
                provider_metadata=src.get("provider_metadata", {})))
        timestamp = float(batch.get("timestamp_s", min((d.timestamp_s for d in dets), default=0.0)))
        result = source_manager.update(DetectionBatch("cached_p3", frame_id, timestamp, tuple(dets)))
        for association in result.associations:
            source_assignments[association.detection_id] = association.object_id
            source_decisions[association.detection_id] = {"decision": association.decision,
                                                           "track_id": association.object_id,
                                                           "distance_m": association.distance_m}
    if set(source_assignments) != raw_ids:
        raise ValueError("source manager did not produce one result for every raw P3 observation")

    exp_cfg = duplicate_config["baseline_config"]["geometry_only"]
    geometry_matcher = ExperimentalAssociation(
        "geometry", max_center_m=float(exp_cfg["candidate_center_radius_m"]),
        geom_min_iou=float(exp_cfg["appearance_iou_min"]),
        geom_only_min_iou=float(exp_cfg["geometry_only_iou_min"]),
        ambiguity_margin=float(exp_cfg["ambiguity_margin"]))
    geometry_assignments: dict[str, str | None] = {}
    geometry_decisions: dict[str, dict] = {}
    canonical_by_frame: dict[int, list[U3Observation]] = defaultdict(list)
    for obs in canonical_observations:
        canonical_by_frame[frame_index[obs.frame_id]].append(obs)
    last_geometry_timestamp = float("-inf")
    for frame_idx in sorted(canonical_by_frame):
        observations = canonical_by_frame[frame_idx]
        if observations:
            timestamps = [o.timestamp_s for o in observations]
            if min(timestamps) < last_geometry_timestamp:
                raise ValueError(f"geometry-only input timestamps moved backwards at frame {frame_idx}")
            last_geometry_timestamp = max(timestamps)
        for decision in geometry_matcher.update_frame(observations):
            geometry_assignments[decision.detection_id] = decision.track_id
            geometry_decisions[decision.detection_id] = {"decision": decision.decision,
                                                          "track_id": decision.track_id,
                                                          "reason": decision.reason}
            geometry_decisions[decision.detection_id]["timestamp_s"] = next(
                o.timestamp_s for o in observations if o.detection_id == decision.detection_id)
        # Preserve canonical ownership without allowing alias evidence into the matcher.
        for alias_id, canonical_id in aliases.items():
            if alias_id.rsplit(":det-", 1)[0] == observations[0].frame_id:
                geometry_assignments[alias_id] = geometry_assignments.get(canonical_id)
                geometry_decisions[alias_id] = {"decision": "duplicate_alias", "track_id": geometry_assignments[alias_id],
                                                "reason": "locked_dedup_v2", "duplicate_of": canonical_id}
    if set(geometry_assignments) != raw_ids:
        raise ValueError("geometry-only-dedup-v2 did not cover every raw P3 observation")

    source_root = source_replay.resolve() if source_replay else None
    base_root = base_replay.resolve() if base_replay else None
    source_replay_root = (source_root / "replay" if source_root and (source_root / "replay").is_dir() else source_root)
    base_replay_root = (base_root / "replay" if base_root and (base_root / "replay").is_dir() else base_root)
    source_manifest = base_replay_manifest = None
    reference_assignments = {}
    reference_aliases = {}
    reference_decisions = {}
    reference_alias_decisions = {}
    if base_replay_root is not None:
        replay_manifest = read_json(base_replay_root / "manifest.json")
        base_launcher_path = base_root / "launcher_manifest.json"
        base_launcher = read_json(base_launcher_path) if base_launcher_path.is_file() else {}
        if replay_manifest.get("association_profile") != "geometry-only-dedup-v2":
            raise ValueError("dedup-v2 reference replay profile mismatch")
        expected_duplicate_config = duplicate_config_path
        if replay_manifest.get("same_frame_duplicate_v2_config_sha256") != sha(expected_duplicate_config):
            raise ValueError("dedup-v2 reference replay config hash differs from locked config")
        expected_inputs = {"capture_manifest": sha(capture_manifest_path), "p2_index": sha(p2_index_path),
                           "p3_cache": sha(p3_cache_path), "p3_manifest": sha(input_root / "detection_3d/detection_3d_manifest.json")}
        if base_launcher and (base_launcher.get("source_cache_run") != str(input_root.resolve())
                              or base_launcher.get("association_profile") != "geometry-only-dedup-v2"
                              or base_launcher.get("input_hashes") != expected_inputs):
            raise ValueError("dedup-v2 reference binding/profile/input hashes mismatch")
        diagnostics = read_json(base_replay_root / "frame_diagnostics.json")
        for frame in diagnostics:
            backend = frame.get("object_backend", {})
            for row in backend.get("duplicate_aliases", []):
                alias_id = str(row["detection_id"])
                canonical_id = str(row.get("canonical_observation_id", row["duplicate_of"]))
                reference_aliases[alias_id] = canonical_id
            for row in backend.get("decisions", []):
                if row.get("decision") == "duplicate_alias":
                    reference_alias_decisions[str(row["detection_id"])] = row
        if reference_aliases != aliases:
            raise ValueError("offline dedup-v2 canonical/alias ownership differs from reference")
        reference_assignments["geometry-only-dedup-v2"], reference_decisions["geometry-only-dedup-v2"] = assignment_map(base_replay_root, raw_ids)
        base_replay_manifest = replay_manifest
    if source_replay_root is not None:
        source_manifest = read_json(source_replay_root / "manifest.json")
        source_launcher_path = source_root / "launcher_manifest.json"
        source_launcher = read_json(source_launcher_path) if source_launcher_path.is_file() else {}
        if source_manifest.get("association_profile") != "source-wrapper":
            raise ValueError("source reference replay profile mismatch")
        expected_inputs = {"capture_manifest": sha(capture_manifest_path), "p2_index": sha(p2_index_path),
                           "p3_cache": sha(p3_cache_path), "p3_manifest": sha(input_root / "detection_3d/detection_3d_manifest.json")}
        if source_launcher and (source_launcher.get("source_cache_run") != str(input_root.resolve())
                                or source_launcher.get("association_profile") != "source"
                                or source_launcher.get("input_hashes") != expected_inputs):
            raise ValueError("source reference binding/profile/input hashes mismatch")
        reference_assignments["source"], reference_decisions["source"] = assignment_map(source_replay_root, raw_ids)

    k4_reference_assignments = None
    if k4_reference is not None:
        k4_reference = k4_reference.resolve()
        reference_rows = read_jsonl(k4_reference)
        k4_reference_assignments = {str(row["observation_id"]): row.get("track_id") for row in reference_rows}
        if set(k4_reference_assignments) != raw_ids:
            raise ValueError("locked K4 assignment reference does not cover exact raw P3 observation population")

    cfg = cfg
    fusion_cfg = fusion_cfg
    return {
        "capture_manifest": capture_manifest, "frame_index": frame_index,
        "p2_index": p2_index, "p2_by_id": p2_by_id,
        "p3_cache_rows": p3_cache_rows, "p3_by_id": raw_d3, "raw_ids": raw_ids,
        "p3_frame_by_id": raw_frame_by_id,
        "evidence_by_id": evidence_by_id, "aliases": aliases,
        "masks": masks, "support": support, "raw_observations": raw_observations,
        "source_assignments": source_assignments, "source_decisions": source_decisions,
        "geometry_assignments": geometry_assignments, "geometry_decisions": geometry_decisions,
        "duplicate_ledger": duplicate_ledger, "reference_assignments": reference_assignments,
        "reference_aliases": reference_aliases, "reference_decisions": reference_decisions,
        "reference_alias_decisions": reference_alias_decisions,
        "k4_reference_assignments": k4_reference_assignments,
        "candidate_config": cfg, "fusion_config": fusion_cfg,
        "p3_manifest_hashes": p3_manifest_hashes,
        "input_hashes": {"capture_manifest": sha(capture_manifest_path), "p2_index": sha(p2_index_path),
                          "p3_cache": sha(p3_cache_path), "p3_manifest": sha(p3_manifest_path)},
        "duplicate_config_sha256": sha(duplicate_config_path),
        "base_replay": base_replay_root, "base_replay_manifest": base_replay_manifest,
        "source_replay": source_replay_root, "source_replay_manifest": source_manifest,
    }


def load_point_evidence(oid: str, evidence_by_id: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    p3_root, row = evidence_by_id[oid]
    path = p3_root / "object_evidence" / row["point_file"]
    with np.load(path, allow_pickle=False) as item:
        return (np.asarray(item["xyz_W"], dtype=np.float32).copy(),
                np.asarray(item["pixel_rc"], dtype=np.int32).copy(),
                np.asarray(item["rgb_u8"], dtype=np.uint8).copy(),
                np.asarray(item["T_W_Copt"], dtype=np.float64).copy(), row)


def run_predictor(input_root: Path, inputs: dict, output: Path, *, representation_mode: str = "k4_current",
                 class_soft_lambda: float = 0.0) -> dict:
    if representation_mode not in {"k4_current", "q2_prior_surface_bounds"}:
        raise ValueError(f"unsupported matcher representation: {representation_mode}")
    cfg = inputs["candidate_config"]
    fusion_cfg = inputs["fusion_config"]
    assoc_cfg = cfg["association"]
    s_cfg = cfg["surface_support"]
    surface_args = {
        "voxel_size_m": float(s_cfg["observation_downsample_voxel_m"]),
        "support_radius_m": float(s_cfg["nearest_surface_tolerance_m"]),
        "minimum_supported_voxels": int(s_cfg["minimum_forward_supported_voxels"]),
        "minimum_forward_support_ratio": float(s_cfg["minimum_forward_support_ratio"]),
        "ambiguity_score_margin": float(s_cfg["continuity_candidate_ambiguity_margin"]),
        "mixed_mask_size_ratio": float(s_cfg["possible_containment_guard"]["observation_to_track_surface_size_ratio"]),
        "mixed_mask_reverse_support_min": float(s_cfg["possible_containment_guard"]["minimum_reverse_support_ratio"]),
        "mixed_mask_forward_support_max": float(s_cfg["possible_containment_guard"]["maximum_forward_support_ratio"]),
    }
    matcher = ExperimentalAssociation("geometry", max_center_m=float(assoc_cfg["candidate_center_radius_m"]),
                                      geom_min_iou=float(assoc_cfg["geometry_min_iou_prefilter"]),
                                      geom_only_min_iou=float(assoc_cfg["original_geometry_acceptance_iou"]),
                                      ambiguity_margin=float(assoc_cfg["original_ambiguity_score_margin"]))
    fusion = ObjectGeometryFusion(voxel_size_m=float(fusion_cfg["voxel_size_m"]),
                                  max_voxels_per_object=int(fusion_cfg["resource_policy"]["max_voxels_per_object"]))
    obs_by_frame: dict[int, list[U3Observation]] = defaultdict(list)
    for oid, d3 in inputs["p3_by_id"].items():
        fi = inputs["frame_index"][oid.rsplit(":det-", 1)[0]]
        # Class is deliberately absent from candidate matcher observations.
        obs_by_frame[fi].append(U3Observation(
            detection_id=oid, frame_id=oid.rsplit(":det-", 1)[0],
            timestamp_s=float(d3["timestamp_s"]),
            center=tuple(float(x) for x in d3["center_W"]),
            extents=tuple(float(x) for x in d3["extents_m"]),
            layer_id=str(d3["layer_id"]), class_name=d3.get("class_name"), feature=None,
            mask_hash=str(inputs["p2_by_id"][oid].get("mask_sha256", "")),
            class_confidence=d3.get("confidence")))

    assignments: dict[str, str | None] = {}
    decision_rows: list[dict] = []
    candidate_rows: list[dict] = []
    alias_rows: list[dict] = []
    last_timestamp = float("-inf")
    class_evidence_history = RawClassEvidenceHistory()
    start = time.perf_counter()
    for frame_index in sorted(obs_by_frame):
        raw_frame_obs = sorted(obs_by_frame[frame_index], key=lambda obs: obs.detection_id)
        current_aliases = {alias: canonical for alias, canonical in inputs["aliases"].items()
                           if alias.rsplit(":det-", 1)[0] == raw_frame_obs[0].frame_id}
        canonical_obs = exclude_same_frame_aliases(raw_frame_obs, current_aliases)
        if not canonical_obs:
            raise RuntimeError(f"frame {frame_index} has aliases but no canonical observation")
        frame_timestamp = min(obs.timestamp_s for obs in raw_frame_obs)
        if frame_timestamp < last_timestamp:
            raise ValueError(f"input frame timestamps moved backwards at frame {frame_index}")
        last_timestamp = frame_timestamp

        # Capture all prior support and its lineage before scoring this frame.
        prior_surface = {}
        prior_ids = {}
        prior_matcher_geometry = {}
        prior_class_evidence = class_evidence_history.snapshot()
        for track in matcher.tracks:
            if track.track_id in fusion.objects:
                surface_state = fusion.snapshot(track.track_id)
                prior_surface[track.track_id] = surface_state["xyz_W"]
                prior_ids[track.track_id] = list(surface_state["evidence_ids"])
                if representation_mode == "q2_prior_surface_bounds":
                    prior_matcher_geometry[track.track_id] = fusion.prior_matcher_geometry(track.track_id)
        frame_points = {obs.detection_id: load_point_evidence(obs.detection_id, inputs["evidence_by_id"])[0]
                        for obs in canonical_obs}
        ledger_start = len(matcher.ledger)
        decisions = matcher.update_frame(canonical_obs,
            surface_points_by_detection=frame_points,
            surface_points_by_track=prior_surface,
            surface_config=surface_args,
            prior_geometry_by_track=prior_matcher_geometry if representation_mode == "q2_prior_surface_bounds" else None,
            prior_class_evidence_by_track=prior_class_evidence if class_soft_lambda else None,
            class_soft_lambda=class_soft_lambda)
        frame_ledger = [dict(row) for row in matcher.ledger[ledger_start:]]
        for row in frame_ledger:
            row["frame_index"] = frame_index
            row["prior_surface_revision"] = len(prior_ids.get(row["track_id"], [])) if row.get("track_id") else 0
            row["prior_evidence_ids"] = prior_ids.get(row["track_id"], []) if row.get("track_id") else []
        candidate_rows.extend(frame_ledger)
        by_id = {decision.detection_id: decision for decision in decisions}
        for obs in canonical_obs:
            decision = by_id[obs.detection_id]
            track_id = decision.track_id
            assignments[obs.detection_id] = track_id
            row_candidates = [row for row in frame_ledger if row["detection_id"] == obs.detection_id]
            chosen = next((row for row in row_candidates if row.get("track_id") == track_id and row.get("eligible")), None)
            decision_rows.append({
                "observation_id": obs.detection_id, "frame_index": frame_index,
                "timestamp_s": obs.timestamp_s, "track_id": track_id,
                "decision": decision.decision, "reason": decision.reason,
                "association_branch": None if chosen is None else chosen.get("association_branch"),
                "selected_candidate_score": decision.selected_candidate_score,
                "deferred": decision.decision == "defer",
                "geometry_admission": "not_applied" if track_id is None else "applied_after_frame_assignment",
                "raw_class_used_by_predictor": False,
            })
        # Aliases retain raw observation identity in evaluator population but
        # never contribute a second point set or matcher update.
        for alias_id, canonical_id in sorted(current_aliases.items()):
            canonical_track = assignments.get(canonical_id)
            assignments[alias_id] = canonical_track
            alias_rows.append({"observation_id": alias_id, "canonical_observation_id": canonical_id,
                               "track_id": canonical_track, "decision": "duplicate_alias",
                               "reason": "same_frame_duplicate_v2"})
            decision_rows.append({"observation_id": alias_id, "frame_index": frame_index,
                                  "track_id": canonical_track, "decision": "duplicate_suppressed",
                                  "reason": "same_frame_duplicate_v2", "duplicate_of": canonical_id,
                                  "association_branch": None,
                                  "geometry_admission": "excluded_alias", "raw_class_used_by_predictor": False})

        # Commit current-frame assigned point sets only after every detection
        # has been scored and assigned using prior-frame surfaces.
        for obs in canonical_obs:
            track_id = assignments[obs.detection_id]
            if track_id is None:
                continue
            xyz, pixels, rgb, pose, evidence = load_point_evidence(obs.detection_id, inputs["evidence_by_id"])
            fusion.apply(object_id=track_id, observation_id=obs.detection_id,
                         timestamp_s=obs.timestamp_s, xyz_W=xyz, rgb_u8=rgb,
                         pixel_rc=pixels, T_W_Copt=pose,
                         payload_sha256=None)
        if class_soft_lambda:
            class_evidence_history.commit_frame(canonical_obs, assignments)

    if set(assignments) != inputs["raw_ids"]:
        raise RuntimeError("candidate assignment population differs from raw P3 observations")
    predictor_runtime = time.perf_counter() - start
    output.mkdir(parents=True, exist_ok=False)
    source_assignments = inputs["source_assignments"]
    base_assignments = inputs["geometry_assignments"]
    base_assignment_rows = {oid: {"decision": row.get("decision"), "reason": row.get("reason"),
                                   "track_id": row.get("track_id"),
                                   "duplicate_of": row.get("duplicate_of")}
                           for oid, row in inputs["geometry_decisions"].items()}
    assignment_order = lambda oid: (inputs["frame_index"][oid.rsplit(":det-", 1)[0]], oid)
    write_jsonl(output / "source_assignments.jsonl", [
        {"observation_id": oid, "track_id": source_assignments[oid],
         "decision": inputs["source_decisions"][oid]["decision"]}
        for oid in sorted(source_assignments, key=assignment_order)])
    write_jsonl(output / "geometry_only_dedup_v2_assignments.jsonl", [
        {"observation_id": oid, "track_id": base_assignments[oid],
         "decision": inputs["geometry_decisions"][oid]["decision"],
         "reason": inputs["geometry_decisions"][oid].get("reason"),
         "duplicate_of": inputs["geometry_decisions"][oid].get("duplicate_of")}
        for oid in sorted(base_assignments, key=assignment_order)])
    write_jsonl(output / "candidate_assignments.jsonl", [
        {"observation_id": oid, "track_id": assignments[oid],
         "decision": next(row["decision"] for row in decision_rows if row["observation_id"] == oid)}
        for oid in sorted(assignments, key=lambda x: (inputs["frame_index"][x.rsplit(":det-", 1)[0]], x))])
    if representation_mode == "q2_prior_surface_bounds":
        write_jsonl(output / "q2_prior_surface_bounds_assignments.jsonl", [
            {"observation_id": oid, "track_id": assignments[oid],
             "decision": next(row["decision"] for row in decision_rows if row["observation_id"] == oid)}
            for oid in sorted(assignments, key=lambda x: (inputs["frame_index"][x.rsplit(":det-", 1)[0]], x))])
    write_jsonl(output / "candidate_decisions.jsonl", decision_rows)
    write_jsonl(output / "candidate_ledger.jsonl", candidate_rows)
    write_jsonl(output / "candidate_aliases.jsonl", alias_rows)

    # The prediction is now fixed; only now load evaluator identity labels.
    eval_paths = sorted((input_root / "object_evaluations").glob("eval_*/observation_matches.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    evaluation_path = eval_paths[0] if eval_paths else None
    if evaluation_path is not None:
        eval_rows = read_jsonl(evaluation_path)
        eval_by_id = {str(row["detection_id"]): row for row in eval_rows if str(row.get("detection_id")) in inputs["raw_ids"]}
        evaluator_ids = {oid: (str(row["gt_semantic_id"]) if row.get("match_status") == "matched"
                               and row.get("gt_semantic_id") is not None else None)
                         for oid, row in eval_by_id.items()}
        if set(evaluator_ids) != inputs["raw_ids"]:
            raise ValueError("official evaluator ledger does not cover fixed raw P3 population")
        eligible = {oid for oid, identity in evaluator_ids.items() if identity is not None}
        metrics = {
            "source": pairwise_identity_metrics(source_assignments, evaluator_ids, eligible_ids=eligible),
            "geometry-only-dedup-v2": pairwise_identity_metrics(base_assignments, evaluator_ids, eligible_ids=eligible),
            "accumulated-surface-support": pairwise_identity_metrics(assignments, evaluator_ids, eligible_ids=eligible),
        }
    else:
        eval_rows, eval_by_id, evaluator_ids, eligible = [], {}, {}, set()
        metrics = {name: {"status": "unavailable_no_evaluator_ledger", "pairwise_tp": None,
                          "pairwise_fp": None, "pairwise_fn": None, "association_precision": None,
                          "association_recall": None, "association_f1": None}
                   for name in ("source", "geometry-only-dedup-v2", "accumulated-surface-support")}

    reference_parity = {}
    direct_maps = {"source": source_assignments, "geometry-only-dedup-v2": base_assignments,
                   "accumulated-surface-support": assignments}
    for mode, reference in inputs["reference_assignments"].items():
        direct = direct_maps[mode]
        if set(direct) != set(reference):
            raise ValueError(f"{mode} direct/reference observation populations differ")
        reference_parity[mode] = {
            "partition_equal": partition_members(direct) == partition_members(reference),
            "observation_assignment_coverage_equal": all((direct[o] is None) == (reference[o] is None) for o in inputs["raw_ids"]),
            "track_id_strings_compared": False,
            "partition_delta": partition_delta(reference, direct, sorted(inputs["raw_ids"])),
        }
        if mode == "source":
            ref_rows = inputs["reference_decisions"][mode]
            mismatches = []
            for oid in sorted(inputs["raw_ids"]):
                expected = ref_rows.get(oid, {}).get("decision")
                actual = inputs["source_decisions"].get(oid, {}).get("decision")
                if expected != actual:
                    mismatches.append({"observation_id": oid, "reference": expected, "offline": actual})
            reference_parity[mode]["decision_mismatches"] = mismatches
        if mode == "geometry-only-dedup-v2":
            mismatches = []
            for oid, canonical in sorted(inputs["aliases"].items()):
                ref_row = inputs["reference_alias_decisions"].get(oid, {})
                ref_canonical = ref_row.get("duplicate_of")
                if ref_canonical != canonical or ref_row.get("decision") != "duplicate_alias":
                    mismatches.append({"observation_id": oid, "reference_canonical": ref_canonical,
                                       "offline_canonical": canonical,
                                       "reference_decision": ref_row.get("decision")})
            reference_parity[mode]["alias_decision_mismatches"] = mismatches
            reference_parity[mode]["reference_assignment_layer_decision"] = "duplicate_suppressed"
            reference_parity[mode]["backend_canonical_alias_decision"] = "duplicate_alias"
    if inputs["k4_reference_assignments"] is not None:
        reference = inputs["k4_reference_assignments"]
        reference_parity["accumulated-surface-support"] = {
            "partition_equal": partition_members(assignments) == partition_members(reference),
            "observation_assignment_coverage_equal": all((assignments[o] is None) == (reference[o] is None) for o in inputs["raw_ids"]),
            "track_id_strings_compared": False,
            "partition_delta": partition_delta(reference, assignments, sorted(inputs["raw_ids"])),
        }
    if reference_parity:
        parity_failures = []
        for mode, result in reference_parity.items():
            if not result.get("partition_equal") or not result.get("observation_assignment_coverage_equal"):
                parity_failures.append(mode + ":partition_or_coverage")
            if result.get("decision_mismatches"):
                parity_failures.append(mode + ":source_decisions")
            if result.get("alias_decision_mismatches"):
                parity_failures.append(mode + ":alias_decisions")
        reference_parity_status = "pass" if not parity_failures else "mismatch"
    else:
        parity_failures = []
        reference_parity_status = "not_requested"
    relation_rows = []
    relation_summaries = {}
    for left_name, right_name, left, right in (
            ("source", "geometry-only-dedup-v2", source_assignments, base_assignments),
            ("geometry-only-dedup-v2", "accumulated-surface-support", base_assignments, assignments),
            ("source", "accumulated-surface-support", source_assignments, assignments)):
        step = f"{left_name}_to_{right_name}"
        summary = {"changed_pairs": 0, "evaluator_verified": 0, "corrections": 0,
                   "regressions": 0, "same_frame_diagnostic": 0, "identity_unavailable": 0,
                   "verified_by_identity": {}, "corrections_by_identity": {}, "regressions_by_identity": {}}
        for i, left_id in enumerate(sorted(inputs["raw_ids"])):
            for right_id in sorted(inputs["raw_ids"])[i + 1:]:
                left_same = left.get(left_id) is not None and left.get(left_id) == left.get(right_id)
                right_same = right.get(left_id) is not None and right.get(left_id) == right.get(right_id)
                if left_same == right_same:
                    continue
                row = {"step": step, "observation_ids": [left_id, right_id],
                       "left_relation": "same_track" if left_same else "different_or_unassigned",
                       "right_relation": "same_track" if right_same else "different_or_unassigned"}
                summary["changed_pairs"] += 1
                if left_id.rsplit(":det-", 1)[0] == right_id.rsplit(":det-", 1)[0]:
                    row["status"] = "same_frame_diagnostic_only"
                    summary["same_frame_diagnostic"] += 1
                elif evaluator_ids.get(left_id) is None or evaluator_ids.get(right_id) is None:
                    row["status"] = "identity_unavailable"
                    summary["identity_unavailable"] += 1
                else:
                    gt_same = evaluator_ids[left_id] == evaluator_ids[right_id]
                    was_correct = left_same == gt_same
                    now_correct = right_same == gt_same
                    row.update({"status": "evaluator_verified_relation_change",
                                "evaluator_ids": [evaluator_ids[left_id], evaluator_ids[right_id]],
                                "was_correct": was_correct, "now_correct": now_correct})
                    summary["evaluator_verified"] += 1
                    key = str(evaluator_ids[left_id]) if gt_same else f"{evaluator_ids[left_id]}|{evaluator_ids[right_id]}"
                    if now_correct and not was_correct:
                        summary["corrections"] += 1
                        summary["corrections_by_identity"][key] = summary["corrections_by_identity"].get(key, 0) + 1
                    elif was_correct and not now_correct:
                        summary["regressions"] += 1
                        summary["regressions_by_identity"][key] = summary["regressions_by_identity"].get(key, 0) + 1
                    summary["verified_by_identity"][key] = summary["verified_by_identity"].get(key, 0) + 1
                relation_rows.append(row)
        relation_summaries[step] = summary

    decision_by_id = {row["observation_id"]: row for row in decision_rows}
    base_by_id = base_assignments
    changed_decisions = []
    for oid in sorted(inputs["raw_ids"]):
        row = decision_by_id[oid]
        base_decision = str(base_assignment_rows.get(oid, {}).get("decision", ""))
        is_surface_match = row.get("association_branch") == "surface_continuity"
        became_deferred = assignments[oid] is None and base_by_id[oid] is not None
        if assignments[oid] != base_by_id[oid] or is_surface_match or became_deferred:
            row["evaluator_id_posthoc"] = evaluator_ids.get(oid)
            row["evaluator_identity_eligible_posthoc"] = oid in eligible
            changed_decisions.append({"observation_id": oid, "base_track": base_by_id[oid],
                                      "base_decision": base_decision,
                                      "candidate_track": assignments[oid], **row})
    write_jsonl(output / "changed_decisions_posthoc.jsonl", changed_decisions)
    write_jsonl(output / "relation_changes.jsonl", relation_rows)

    gate_suffixes = ["hm3d-00802-main-0003:det-0001", "hm3d-00802-main-0017:det-0001",
                     "hm3d-00802-main-0018:det-0000"]
    gate_results = []
    for oid in gate_suffixes:
        if oid in assignments:
            candidate_dec = decision_by_id[oid]
            support_rows = [r for r in candidate_rows if r["detection_id"] == oid]
            gate_results.append({"observation_id": oid, "base_track": base_by_id[oid],
                                 "candidate_track": assignments[oid], "candidate_decision": candidate_dec,
                                 "surface_candidates": [r for r in support_rows if r.get("surface_continuity_evaluated")]})

    evaluator_ledger_hash = sha(evaluation_path) if evaluation_path is not None else None
    fusion_objects = []
    surface_root = output / "surface_states"
    surface_root.mkdir(exist_ok=True)
    for track_id in sorted(fusion.objects):
        state = fusion.snapshot(track_id)
        safe_track_id = hashlib.sha256(track_id.encode("utf-8")).hexdigest()[:20]
        surface_path = surface_root / f"{safe_track_id}.npz"
        np.savez_compressed(surface_path, voxel_keys=state["voxel_keys"], xyz_W=state["xyz_W"],
                            rgb_u8=state["rgb_u8"], point_support=state["point_support"],
                            bounds_min_W=state["bounds_min_W"], bounds_max_W=state["bounds_max_W"],
                            center_W=state["center_W"])
        fusion_objects.append({"track_id": track_id, "observation_count": state["observation_count"],
                               "unique_sensor_pose_count": state["unique_sensor_pose_count"],
                               "raw_point_count": state["raw_point_count"], "voxel_count": state["voxel_count"],
                               "surface_snapshot_npz": str(surface_path.relative_to(output)),
                               "evidence_ids": state["evidence_ids"], "bounds_min_W": state["bounds_min_W"].tolist(),
                               "bounds_max_W": state["bounds_max_W"].tolist(), "center_W": state["center_W"].tolist()})
    runtime_s = time.perf_counter() - start
    k1_metrics_path = ROOT / "artifacts/audits/object_graph_improvement/k1_three_modes_v2/metrics.json"
    k1_metrics = read_json(k1_metrics_path)["modes"] if k1_metrics_path.is_file() else {}
    comparison_runtime_memory = {
        "source": {"runtime_seconds": k1_metrics.get("source", {}).get("runtime_seconds"),
                   "peak_process_rss_mib": k1_metrics.get("source", {}).get("peak_process_rss_mib")},
        "geometry-only-dedup-v2": {"runtime_seconds": k1_metrics.get("geometry-only-dedup-v2", {}).get("runtime_seconds"),
                                    "peak_process_rss_mib": k1_metrics.get("geometry-only-dedup-v2", {}).get("peak_process_rss_mib")},
    }
    manifest = {
        "schema_version": 2,
        "status": "completed" if reference_parity_status != "mismatch" else "completed_with_reference_parity_mismatch",
        "kind": "causal_offline_three_mode_surface_association",
        "input_run_id": input_root.name, "input_hashes": inputs["input_hashes"],
        "P3_direct_evidence_manifests": inputs["p3_manifest_hashes"],
        "P3_Detection3D_exact_cache_parity": True,
        "association_modes": ["source", "geometry-only-dedup-v2", "accumulated-surface-support"],
        "matcher_representation_mode": representation_mode,
        "class_soft_rerank_lambda": float(class_soft_lambda),
        "raw_class_semantic_label_updates": False,
        "matcher_geometry_source": ("prior accumulated surface voxel representatives; min/max bounds, midpoint center, max-min extents"
                                     if representation_mode == "q2_prior_surface_bounds"
                                     else "K4 current EMA center/extents"),
        "q2_representation_config_sha256": (sha(ROOT / "configs/association/q2_prior_surface_bounds_v1.json")
                                             if representation_mode == "q2_prior_surface_bounds" else None),
        "native_replay_required_for_prediction": False,
        "base_profile": "geometry-only-dedup-v2",
        "base_replay_manifest_sha256": sha(inputs["base_replay"] / "manifest.json") if inputs["base_replay"] else None,
        "source_replay_manifest_sha256": sha(inputs["source_replay"] / "manifest.json") if inputs["source_replay"] else None,
        "reference_parity": reference_parity,
        "reference_parity_status": reference_parity_status,
        "reference_parity_failures": parity_failures,
        "dedup_aliases_recomputed_from_current_P2_P3": True,
        "source_same_pose_suppression_is_not_alias": True,
        "candidate_config": str((ROOT / "configs/association/geometry_only_accumulated_surface_v1.json").resolve()),
        "candidate_config_sha256": sha(ROOT / "configs/association/geometry_only_accumulated_surface_v1.json"),
        "same_frame_duplicate_v2_config": str((ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json").resolve()),
        "same_frame_duplicate_v2_config_sha256": inputs["duplicate_config_sha256"],
        "source_manager_config_sha256": hashlib.sha256(json.dumps(
            read_json(ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json")["baseline_config"]["source_manager"],
            sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "geometry_only_config_sha256": hashlib.sha256(json.dumps(
            read_json(ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json")["baseline_config"]["geometry_only"],
            sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "geometry_config": str((ROOT / "configs/fusion/object_geometry_v2.json").resolve()),
        "geometry_config_sha256": sha(ROOT / "configs/fusion/object_geometry_v2.json"),
        "dedup_alias_count": len(inputs["aliases"]), "raw_observation_count": len(inputs["raw_ids"]),
        "sidecar_complete_for_raw_P3": True, "semantic_or_GT_predictor_input": False,
        "uses_raw_class": False, "uses_appearance": False, "uses_future_observations": False,
        "state_initialization": "empty matcher and surface state at frame 0",
        "causal_replay_runtime_seconds": predictor_runtime, "total_runtime_seconds": runtime_s,
        "runtime_scope": "offline K4 candidate from preflight over existing complete P3 sidecars through scoring; excludes P3 sidecar generation and M1-M10 native replay",
        "P3_sidecar_generation_runtime_seconds": None,
        "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "surface_track_count": len(fusion_objects), "total_accumulated_voxels": sum(x["voxel_count"] for x in fusion_objects),
        "total_accumulated_points": sum(x["raw_point_count"] for x in fusion_objects),
        "serialized_surface_snapshot_bytes": sum((output / row["surface_snapshot_npz"]).stat().st_size for row in fusion_objects),
        "deferred_count": sum(x["decision"] == "defer" for x in decision_rows),
        "continuity_matches": sum(x.get("association_branch") == "surface_continuity" for x in decision_rows),
        "same_frame_aliases_add_surface": False,
        "source_input_order": "authoritative P3 frame order and per-frame detection order preserved",
        "geometry_modes_input_order": "authoritative frame order; deterministic existing matcher ordering within frame",
        "aliases_excluded_from_matcher_and_surface_update": True,
        "deferred_observations_update_track_surface": False,
        "whole_frame_decisions_precede_surface_commit": True,
        "fixed_candidate_assignments_written_before_evaluator_read": True,
        "evaluator_ledger": str(evaluation_path) if evaluation_path else None, "evaluator_ledger_sha256": evaluator_ledger_hash,
        "evaluator_identity_scope": "HM3D one-to-one semantic instance IDs; not human-confirmed physical identity",
        "evaluator_covered_observations": len(eligible), "GT_coverage": len(eligible) / len(inputs["raw_ids"]),
        "metrics": metrics, "relation_change_summaries": relation_summaries,
        "comparison_runtime_memory": comparison_runtime_memory,
        "k1_metrics_sha256": sha(k1_metrics_path) if k1_metrics_path.is_file() else None,
        "gate_cases": gate_results, "fusion_objects": fusion_objects,
        "limitations": ["development evaluation on HM3D 00802, not holdout",
                        "surface support is evidence, not an identity oracle",
                        "no M9/graph/room-score integration", "accumulator resume is unsupported"]
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    _write_gallery(output, input_root, changed_decisions, gate_results, decision_by_id, inputs)
    _write_report(output / "report.vi.md", manifest, relation_rows, changed_decisions)
    return manifest


def _write_gallery(output: Path, input_root: Path, changed: list[dict], gates: list[dict], decision_by_id: dict, inputs: dict) -> None:
    gallery_dir = output / "gallery_assets"
    gallery_dir.mkdir(parents=True, exist_ok=True)
    ids = []
    for item in changed:
        if item.get("association_branch") == "surface_continuity" or item.get("decision") == "defer":
            ids.append(item["observation_id"])
    ids.extend(item["observation_id"] for item in gates if item["candidate_decision"]["decision"] != "matched")
    ids = list(dict.fromkeys(ids))[:16]
    cards = []
    for oid in ids:
        frame_name, _ = oid.rsplit(":det-", 1)
        frame_index = inputs["frame_index"][frame_name]
        rgb_path = input_root / "capture/frames" / f"{frame_index:06d}/rgb.npy"
        rgb = np.asarray(np.load(rgb_path, allow_pickle=False), dtype=np.uint8)[..., :3].copy()
        det = inputs["p2_by_id"][oid]
        mask = np.asarray(np.load(input_root / "detection_2d" / det["mask_file"], allow_pickle=False), dtype=bool)
        rgb[mask] = (0.48 * rgb[mask] + 0.52 * np.asarray([20, 210, 250])).astype(np.uint8)
        image = Image.fromarray(rgb, "RGB")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, min(image.width, 850), 22), fill=(0, 0, 0))
        draw.text((5, 4), oid, fill="white")
        name = hashlib.sha256(oid.encode()).hexdigest()[:20] + ".jpg"
        image.save(gallery_dir / name, quality=90)
        row = decision_by_id[oid]
        cards.append(f"<article><h3>{html.escape(oid)}</h3><img src='gallery_assets/{name}'>"
                     f"<pre>decision={html.escape(str(row['decision']))}; branch={html.escape(str(row.get('association_branch')))}; "
                     f"track={html.escape(str(row.get('track_id')))}; reason={html.escape(str(row['reason']))}</pre></article>")
    page = ("<!doctype html><meta charset='utf-8'><title>K4 surface support diagnostic</title>"
            "<style>body{font:14px system-ui;max-width:1200px;margin:1.5rem auto;background:#f3f5f7;color:#1b2630}"
            "article{background:white;border:1px solid #ccd5df;margin:1rem 0;padding:1rem}img{max-width:100%}pre{white-space:pre-wrap}</style>"
            "<h1>K4 surface continuity — changed/deferred observations</h1>"
            "<p>Prediction decisions are shown without GT labels; cyan overlay is the cached P2 mask.</p>" + "\n".join(cards))
    (output / "gallery.html").write_text(page, encoding="utf-8")


def _write_report(path: Path, manifest: dict, relation_rows: list[dict], changed: list[dict]) -> None:
    has_evaluator = bool(manifest.get("evaluator_ledger") and manifest.get("evaluator_covered_observations"))
    if has_evaluator:
        coverage = (f"{manifest['evaluator_covered_observations']}/"
                    f"{manifest['raw_observation_count']} ({manifest.get('GT_coverage', 0):.1%})")
    else:
        coverage = "unavailable (no evaluator ledger)"
    lines = ["# K4 accumulated surface support — development report", "",
             f"Input `{manifest['input_run_id']}`; raw P3={manifest['raw_observation_count']}; evaluator identity coverage={coverage}.",
             "Evaluator identities are HM3D semantic IDs, not human-confirmed physical identity. This is development data, not a holdout.", "",
             "| Mode | TP/FP/FN | Precision | Recall | F1 | False-merge tracks | Fragmented IDs | Runtime/RSS |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for mode, result in manifest["metrics"].items():
        if mode == "accumulated-surface-support":
            runtime = f"{manifest['causal_replay_runtime_seconds']:.2f} s"
            rss = f"{manifest['peak_process_rss_mib']:.1f} MiB"
        else:
            timing = manifest["comparison_runtime_memory"].get(mode, {})
            runtime = f"{timing.get('runtime_seconds')} s"
            rss = f"{timing.get('peak_process_rss_mib')} MiB"
        if result.get("status", "available") != "available":
            unavailable = f"N/A ({result['status']})"
            counts = precision = recall = f1 = false_merge = fragmented = unavailable
        else:
            counts = f"{result.get('pairwise_tp', 'N/A')}/{result.get('pairwise_fp', 'N/A')}/{result.get('pairwise_fn', 'N/A')}"
            precision = result.get("association_precision", "N/A")
            recall = result.get("association_recall", "N/A")
            f1 = result.get("association_f1", "N/A")
            false_merge = result.get("false_merge_tracks", "N/A")
            fragmented = result.get("fragmented_evaluator_instances", "N/A")
        lines.append(f"| {mode} | {counts} | {precision} | {recall} | {f1} | {false_merge} | {fragmented} | {runtime} / {rss} |")
    lines += ["", "## Decision changes", "",
              f"Surface-continuity assignments: {manifest['continuity_matches']}; deferred observations: {manifest['deferred_count']}; dedup aliases excluded from fusion: {manifest['dedup_alias_count']}; accumulated voxels: {manifest['total_accumulated_voxels']}.", "",
              "| Comparison | Changed pairs | Verified | Corrections | Regressions | Same-frame diagnostics | Identity unavailable |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for step, row in manifest["relation_change_summaries"].items():
        corrected_ids = row.get("corrections_by_identity", {})
        regressed_ids = row.get("regressions_by_identity", {})
        lines.append(f"| {step} | {row['changed_pairs']} | {row['evaluator_verified']} | {row['corrections']} / {corrected_ids} | {row['regressions']} / {regressed_ids} | {row['same_frame_diagnostic']} | {row['identity_unavailable']} |")
    lines += ["", "## K1 gate cases", ""]
    for item in manifest["gate_cases"]:
        decision = item["candidate_decision"]
        support = item["surface_candidates"]
        support_text = "; ".join(
            f"track {r['track_id']}: forward={r.get('forward_supported_voxels')}/{r.get('observation_surface_voxels')} "
            f"({r.get('forward_support_ratio'):.3f}), reverse={r.get('reverse_support_ratio'):.3f}, "
            f"status={r.get('surface_branch_status')}" for r in support)
        lines.append(f"- `{item['observation_id']}`: baseline track `{item['base_track']}`, candidate `{item['candidate_track']}`, decision `{decision['decision']}` / `{decision['reason']}`. {support_text}")
    lines += ["", "## Changed decisions", "", f"Changed/deferred decision rows retained: {len(changed)} (`changed_decisions_posthoc.jsonl`). Full per-candidate ledger: `candidate_ledger.jsonl`. Gallery: `gallery.html`.", "",
              "## Conclusion", "",
              "Surface support is a causal experimental branch after the existing layer/radius gates and only when no original geometry candidate is eligible. Original IoU acceptance remains 0.05; no global threshold was lowered. A surface match is not identity proof. Compare evaluator corrections and regressions with coverage before deciding whether to continue.", "",
              f"Runtime after sidecars were ready: candidate replay {manifest['causal_replay_runtime_seconds']:.2f}s; preflight {manifest.get('preflight_runtime_seconds', 'not measured')}s; post-P3 total {manifest.get('post_p3_wall_seconds', manifest['total_runtime_seconds']):.2f}s; peak process RSS {manifest['peak_process_rss_mib']:.1f} MiB; serialized surface snapshots {manifest['serialized_surface_snapshot_bytes']} bytes. P3 sidecar generation time was not recorded; source and dedup-v2 wall times include native M1-M10, so are not directly comparable to this offline association timing.", "",
              (f"Cross-frame pair metric excluded {manifest['metrics']['accumulated-surface-support'].get('same_frame_pairs_excluded_from_cross_frame', 'N/A')} same-frame pairs; "
               f"{manifest['metrics']['accumulated-surface-support'].get('observations_without_evaluator_identity', 'N/A')} raw observations had no eligible evaluator identity."
               if manifest['metrics']['accumulated-surface-support'].get('status') == 'available'
               else "Cross-frame pair counts and evaluator coverage are unavailable because no evaluator ledger was supplied."), "",
              "No GT/class/room label or appearance features entered predictor. U4-OV, M9, graph nodes and room scores were not changed. Accumulator begins empty at frame 0; resume is unsupported."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-run", type=Path, required=True)
    parser.add_argument("--p3-output", type=Path, action="append", required=True,
                        help="direct P3 sidecar output; repeat for non-overlapping frame slices")
    parser.add_argument("--dedup-v2-replay", type=Path,
                        help="optional native replay reference; used only for parity validation")
    parser.add_argument("--source-replay", type=Path,
                        help="optional native replay reference; used only for parity validation")
    parser.add_argument("--k4-reference", type=Path,
                        help="optional locked K4 candidate_assignments.jsonl for partition parity")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/association/geometry_only_accumulated_surface_v1.json")
    parser.add_argument("--fusion-config", type=Path, default=ROOT / "configs/fusion/object_geometry_v2.json")
    parser.add_argument("--matcher-representation", choices=("k4_current", "q2_prior_surface_bounds"),
                        default="k4_current")
    args = parser.parse_args()
    started = time.perf_counter()
    if args.output.exists():
        raise FileExistsError(args.output)
    preflight_start = time.perf_counter()
    inputs = load_locked_inputs(args.input_run.resolve(), [p.resolve() for p in args.p3_output],
                                args.source_replay.resolve() if args.source_replay else None,
                                args.dedup_v2_replay.resolve() if args.dedup_v2_replay else None,
                                args.config.resolve(), args.fusion_config.resolve(),
                                args.k4_reference.resolve() if args.k4_reference else None)
    preflight_s = time.perf_counter() - preflight_start
    result = run_predictor(args.input_run.resolve(), inputs, args.output.resolve(),
                           representation_mode=args.matcher_representation)
    result["preflight_runtime_seconds"] = preflight_s
    result["post_p3_wall_seconds"] = time.perf_counter() - started
    result["total_runtime_seconds"] = result["post_p3_wall_seconds"]
    (args.output.resolve() / "manifest.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    _write_report(args.output.resolve() / "report.vi.md", result,
                  read_jsonl(args.output.resolve() / "relation_changes.jsonl"),
                  read_jsonl(args.output.resolve() / "changed_decisions_posthoc.jsonl"))
    metrics = result["metrics"]["accumulated-surface-support"]
    print(json.dumps({"status": result["status"], "output": str(args.output.resolve()),
                      "modes": result["association_modes"],
                      "aliases": result["dedup_alias_count"], "deferred": result["deferred_count"],
                      "continuity_matches": result["continuity_matches"],
                      "reference_parity": result["reference_parity_status"],
                      "K4_TP_FP_FN": [metrics["pairwise_tp"], metrics["pairwise_fp"], metrics["pairwise_fn"]],
                      "runtime_s": result["total_runtime_seconds"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
