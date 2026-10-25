#!/usr/bin/env python3
"""Build an inspectable OF0-OF2 geometry-fusion sidecar from fixed replays."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import resource
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from standalone_sg.objects.object_geometry_fusion import ObjectGeometryFusion  # noqa: E402
from scripts.object_fusion_launcher import compare_detection3d, validate_lineage, validate_sidecar  # noqa: E402


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def assignments_for(replay_root: Path, observation_ids: set[str]) -> dict[str, dict]:
    replay = replay_root / "replay" if (replay_root / "replay").is_dir() else replay_root
    rows: dict[str, dict] = {}
    lineage = replay / "object_association_lineage.jsonl"
    for row in jsonl(lineage):
        did = str(row.get("detection_id", ""))
        if did in observation_ids:
            rows[did] = dict(row)
    diagnostics = json.loads((replay / "frame_diagnostics.json").read_text(encoding="utf-8"))
    for frame in diagnostics:
        backend = frame.get("object_backend", {})
        for row in backend.get("decisions", []) + backend.get("assignments", []):
            did = str(row.get("detection_id", ""))
            if did not in observation_ids:
                continue
            merged = rows.setdefault(did, {"detection_id": did, "frame_id": frame.get("frame_id")})
            decision = row.get("decision")
            object_id = row.get("object_id", row.get("track_id"))
            # Lineage stores the association disposition (duplicate_suppressed),
            # while frame diagnostics may additionally describe the same event
            # as an observation alias (duplicate_alias). Preserve the lineage
            # vocabulary as authoritative, but keep rejecting real conflicts.
            equivalent_decisions = {
                frozenset({"duplicate_suppressed", "duplicate_alias"}),
                frozenset({"geometry_match", "matched"}),
            }
            current_decision = merged.get("decision")
            decisions_equivalent = (
                current_decision == decision
                or frozenset({str(current_decision), str(decision)}) in equivalent_decisions
            )
            if current_decision and decision and not decisions_equivalent:
                raise ValueError(f"association lineage/diagnostics decision conflict for {did}: {merged['decision']} != {decision}")
            if merged.get("object_id") is not None and object_id is not None and merged["object_id"] != object_id:
                raise ValueError(f"association lineage/diagnostics object_id conflict for {did}: {merged['object_id']} != {object_id}")
            # Direct lineage rows are loaded first and remain authoritative.
            if current_decision is None:
                merged["decision"] = decision
            merged["reason"] = row.get("reason", merged.get("reason"))
            if object_id is not None:
                merged["object_id"] = object_id
            if row.get("duplicate_of") is not None:
                merged["duplicate_of"] = row["duplicate_of"]
            if row.get("canonical_observation_id") is not None:
                merged["canonical_observation_id"] = row["canonical_observation_id"]
    for did in observation_ids:
        row = rows.setdefault(did, {"detection_id": did, "decision": "unassigned", "object_id": None})
        if row.get("decision", "").startswith("defer"):
            row["object_id"] = None
    return rows


def validate_assignment_lineage(replay_root: Path, observation_ids: set[str]) -> dict:
    """Require an explicit causal disposition for every accepted P3 observation."""
    replay = replay_root / "replay" if (replay_root / "replay").is_dir() else replay_root
    lineage = replay / "object_association_lineage.jsonl"
    if not lineage.is_file():
        raise ValueError(f"association lineage missing: {lineage}")
    rows = jsonl(lineage)
    by_id: dict[str, dict] = {}
    for row in rows:
        oid = str(row.get("detection_id", ""))
        if oid not in observation_ids:
            continue
        if oid in by_id:
            raise ValueError(f"duplicate association lineage rows for {oid}")
        by_id[oid] = row
    missing = sorted(observation_ids - set(by_id))
    if missing:
        raise ValueError(f"association lineage incomplete: {len(missing)} P3 observations lack decisions; examples={missing[:5]}")
    assigned = {"created", "geometry_match", "same_class_match", "same_pose_suppressed", "matched"}
    aliases = {"duplicate_suppressed", "duplicate_alias"}
    unassigned = {"defer_ambiguous", "defer", "unassigned", "unmatched"}
    allowed = assigned | aliases | unassigned
    for oid, row in by_id.items():
        decision = str(row.get("decision", ""))
        if decision not in allowed:
            raise ValueError(f"unsupported/missing association decision for {oid}: {decision or 'missing'}")
        if decision in assigned and not row.get("object_id"):
            raise ValueError(f"assigned observation has no object_id in lineage: {oid}")
        if decision in aliases and not (row.get("canonical_observation_id") or row.get("duplicate_of")):
            # frame diagnostics may carry canonical lineage; allow it only when
            # the direct ledger still provides an explicit same-frame target.
            diagnostics_path = replay / "frame_diagnostics.json"
            if diagnostics_path.is_file():
                diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
                extra = {str(item.get("detection_id")): item
                         for frame in diagnostics for item in frame.get("object_backend", {}).get("decisions", [])
                         if item.get("detection_id")}
                detail = extra.get(oid, {})
                if not (detail.get("canonical_observation_id") or detail.get("duplicate_of")):
                    raise ValueError(f"same-frame alias has no canonical observation reference: {oid}")
            else:
                raise ValueError(f"same-frame alias has no canonical observation reference: {oid}")
    return {"rows": len(by_id), "assigned": sum(r.get("decision") in assigned for r in by_id.values()),
            "aliases": sum(r.get("decision") in aliases for r in by_id.values()),
            "deferred_or_unassigned": sum(r.get("decision") in unassigned for r in by_id.values())}


def validate_p3_sidecar(input_root: Path, p3_output: Path) -> dict:
    """Validate complete evidence sidecar and its binding to capture/P2/P3 inputs."""
    required = [p3_output / "detection_3d.jsonl", p3_output / "detection_3d_manifest.json",
                p3_output / "object_evidence/index.jsonl"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"P3 selected-point sidecar incomplete; missing={missing}")
    p3_manifest = json.loads(required[1].read_text(encoding="utf-8"))
    if p3_manifest.get("status") != "pass":
        raise ValueError("P3 sidecar manifest status is not pass")
    capture_hash = sha(input_root / "capture/capture_manifest.json")
    p2_hash = sha(input_root / "detection_2d/detection_2d_index.json")
    authoritative = json.loads((input_root / "detection_3d/detection_3d_manifest.json").read_text(encoding="utf-8"))
    if p3_manifest.get("capture_manifest_sha256") != capture_hash or p3_manifest.get("detection_2d_cache_sha256") != p2_hash:
        raise ValueError("P3 evidence sidecar capture/P2 hashes do not match input run")
    sidecar = p3_manifest.get("object_evidence_sidecar", {})
    if sidecar.get("status") != "pass" or sidecar.get("complete") is False:
        raise ValueError(f"P3 evidence sidecar is not complete: {sidecar}")
    if int(sidecar.get("records", -1)) != int(authoritative.get("detection_3d_count", -2)):
        raise ValueError("P3 evidence record count differs from authoritative accepted Detection3D count")
    evidence_rows = jsonl(required[2])
    detections = jsonl(required[0])
    p3_by_id = {d["detection_id"]: d for batch in detections for d in batch.get("detections", [])}
    evidence_by_id = {str(row.get("observation_id")): row for row in evidence_rows}
    if len(p3_by_id) != sum(len(b.get("detections", [])) for b in detections):
        raise ValueError("duplicate Detection3D IDs in P3 sidecar")
    if set(p3_by_id) != set(evidence_by_id):
        raise ValueError(f"P3 sidecar detection/evidence ID mismatch: detections={len(p3_by_id)} evidence={len(evidence_by_id)}")
    for oid, row in evidence_by_id.items():
        if int(row.get("point_count", -1)) != int(p3_by_id[oid].get("provider_metadata", {}).get("cluster_points", -2)):
            raise ValueError(f"selected point count differs from Detection3D: {oid}")
        point_path = p3_output / "object_evidence" / str(row.get("point_file", ""))
        if not point_path.is_file() or sha(point_path) != row.get("point_file_sha256"):
            raise ValueError(f"selected-point file missing/hash mismatch: {oid}")
    return {"p3_manifest": p3_manifest, "p3_by_id": p3_by_id, "evidence_by_id": evidence_by_id,
            "input_capture_sha256": capture_hash, "input_p2_sha256": p2_hash}


def fusion_admission(assignment: dict) -> tuple[bool, str]:
    decision = str(assignment.get("decision", "unassigned"))
    if decision in {"duplicate_suppressed", "duplicate_alias"}:
        return False, "same_frame_alias"
    if assignment.get("object_id") is None or decision.startswith("defer") or decision == "unassigned":
        return False, "unassigned_or_deferred"
    return True, "assigned_observation"


def write_ply(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> None:
    with path.open("w", encoding="ascii") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write(f"element vertex {len(xyz)}\nproperty float x\nproperty float y\nproperty float z\n")
        f.write("property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
        for p, c in zip(xyz, rgb):
            f.write(f"{p[0]:.6f} {p[1]:.6f} {p[2]:.6f} {int(c[0])} {int(c[1])} {int(c[2])}\n")


def evidence_card(input_root: Path, observation_id: str, p2_by_id: dict, assets: Path) -> str | None:
    det = p2_by_id.get(observation_id)
    if det is None:
        return None
    frame_name = observation_id.rsplit(":det-", 1)[0]
    frame_index = int(frame_name.rsplit("-", 1)[-1])
    rgb = np.asarray(np.load(input_root / "capture/frames" / f"{frame_index:06d}/rgb.npy", allow_pickle=False), dtype=np.uint8)[..., :3]
    mask = np.asarray(np.load(input_root / "detection_2d" / det["mask_file"], allow_pickle=False), dtype=bool)
    overlay = rgb.copy()
    overlay[mask] = (0.45 * overlay[mask] + 0.55 * np.asarray([35, 205, 250])).astype(np.uint8)
    image = Image.fromarray(overlay, "RGB")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, min(image.width, 540), 22), fill=(0, 0, 0))
    draw.text((4, 4), observation_id, fill=(255, 255, 255))
    name = hashlib.sha256(observation_id.encode()).hexdigest()[:16] + ".jpg"
    image.save(assets / name, quality=88)
    return name


def render_cloud(path: Path, xyz: np.ndarray, rgb: np.ndarray, xyz_first: np.ndarray | None = None,
                 rgb_first: np.ndarray | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    panels = 2 if xyz_first is not None else 1
    fig = plt.figure(figsize=(10 if panels == 2 else 6, 4.8))
    all_xyz = np.concatenate([xyz, xyz_first], axis=0) if xyz_first is not None else xyz
    lo, hi = all_xyz.min(axis=0), all_xyz.max(axis=0)
    center = (lo + hi) / 2
    radius = max(float(np.max(hi - lo)) / 2, 0.03)
    clouds = [(xyz_first, rgb_first, "First observation"), (xyz, rgb, "Fused voxels")] if panels == 2 else [(xyz, rgb, "Fused voxels")]
    for i, (cloud, colors, title) in enumerate(clouds, 1):
        ax = fig.add_subplot(1, panels, i, projection="3d")
        if len(cloud) > 25000:
            select = np.linspace(0, len(cloud) - 1, 25000, dtype=np.int64)
            cloud, colors = cloud[select], colors[select]
        ax.scatter(cloud[:, 0], cloud[:, 1], cloud[:, 2], c=colors.astype(np.float32) / 255.0,
                   s=1.2, linewidths=0, alpha=.85)
        ax.set_title(f"{title} ({len(cloud):,} points)")
        ax.set_xlim(center[0] - radius, center[0] + radius)
        ax.set_ylim(center[1] - radius, center[1] + radius)
        ax.set_zlim(center[2] - radius, center[2] + radius)
        ax.set_xlabel("world X (m)"); ax.set_ylabel("world Y (m)"); ax.set_zlabel("world Z (m)")
        ax.view_init(elev=24, azim=-55)
    fig.tight_layout(); fig.savefig(path, dpi=130, facecolor="white"); plt.close(fig)


def run(input_root: Path, p3_output: Path, replay_specs: list[str], output: Path,
        fusion_config: Path) -> dict:
    started = time.perf_counter()
    if output.exists():
        raise FileExistsError(output)
    cfg = json.loads(fusion_config.read_text(encoding="utf-8"))
    sidecar_contract = validate_sidecar(input_root, p3_output)
    parity = compare_detection3d(input_root / "detection_3d/detection_3d.jsonl", sidecar_contract["detection_path"])
    if parity["status"] != "pass":
        raise ValueError(f"recomputed P3 Detection3D diverges from authoritative cache: {parity}")
    p3_rows = jsonl(p3_output / "detection_3d.jsonl")
    p3_by_id = {d["detection_id"]: d for frame in p3_rows for d in frame.get("detections", [])}
    raw_ids = set(p3_by_id)
    sidecar_index = p3_output / "object_evidence/index.jsonl"
    evidence_rows = jsonl(sidecar_index)
    evidence_by_id = {row["observation_id"]: row for row in evidence_rows}
    if len(evidence_by_id) != len(evidence_rows):
        raise ValueError("duplicate observation ID in direct P3 evidence sidecar")
    if set(evidence_by_id) != raw_ids:
        missing, extra = sorted(raw_ids - set(evidence_by_id)), sorted(set(evidence_by_id) - raw_ids)
        raise ValueError(f"P3 output/evidence ID set mismatch: missing={missing[:5]} extra={extra[:5]}")
    p2_index = json.loads((input_root / "detection_2d/detection_2d_index.json").read_text(encoding="utf-8"))
    p2_by_id = {d["detection_id"]: d for f in p2_index["frames"] for d in f["detections"]}
    modes = []
    for spec in replay_specs:
        if "=" not in spec:
            raise ValueError("replay arguments must be profile=/path/to/replay-root")
        profile, path = spec.split("=", 1)
        modes.append((profile, Path(path).resolve()))
    if not modes:
        raise ValueError("at least one fixed-assignment replay is required")
    output.mkdir(parents=True)
    all_summaries = {}
    all_aliases = {}
    all_excluded = {}
    for profile, replay_root in modes:
        validate_lineage(replay_root, raw_ids)
        assignments = assignments_for(replay_root, raw_ids)
        mode_root = output / profile
        objects_root = mode_root / "objects"
        images_root = mode_root / "evidence_images"
        objects_root.mkdir(parents=True); images_root.mkdir()
        engine = ObjectGeometryFusion(voxel_size_m=float(cfg["voxel_size_m"]),
                                      max_voxels_per_object=int(cfg["resource_policy"]["max_voxels_per_object"]))
        event_rows = []
        aliases = []
        grouped: dict[str, list[dict]] = {}
        ordered_ids = sorted(evidence_rows, key=lambda r: (int(r["frame_index"]), r["observation_id"]))
        for evidence in ordered_ids:
            oid = evidence["observation_id"]
            assignment = assignments.get(oid, {})
            decision = str(assignment.get("decision", "unassigned"))
            object_id = assignment.get("object_id")
            admitted, admission_reason = fusion_admission(assignment)
            if not admitted and admission_reason == "same_frame_alias":
                aliases.append({"observation_id": oid, "canonical_observation_id": assignment.get("canonical_observation_id", assignment.get("duplicate_of")),
                                "decision": "alias_excluded_from_fusion", "reason": "same_frame_alias"})
                event_rows.append({"observation_id": oid, "frame_index": evidence["frame_index"], "timestamp_s": evidence["timestamp_s"],
                                   "decision": "alias_excluded_from_fusion", "object_id": object_id,
                                   "canonical_observation_id": assignment.get("canonical_observation_id", assignment.get("duplicate_of")),
                                   "point_file": evidence["point_file"], "point_count": evidence["point_count"]})
                continue
            if not admitted:
                event_rows.append({"observation_id": oid, "frame_index": evidence["frame_index"], "timestamp_s": evidence["timestamp_s"],
                                   "decision": "excluded_unassigned_or_deferred", "association_decision": decision,
                                   "reason": assignment.get("reason", "no_assigned_object"), "point_count": evidence["point_count"]})
                continue
            assignment_time = assignment.get("decision_time_s", assignment.get("timestamp_s"))
            if assignment_time is not None and float(assignment_time) > float(evidence["timestamp_s"]) + 1e-9:
                raise ValueError(f"future association evidence for {oid}: {assignment_time} > {evidence['timestamp_s']}")
            point_path = p3_output / "object_evidence" / evidence["point_file"]
            if sha(point_path) != evidence["point_file_sha256"]:
                raise ValueError(f"P3 evidence sidecar checksum mismatch: {oid}")
            with np.load(point_path, allow_pickle=False) as side:
                xyz = side["xyz_W"].copy(); pixels = side["pixel_rc"].copy()
                colors = side["rgb_u8"].copy(); pose = side["T_W_Copt"].copy()
            if int(evidence["frame_index"]) >= len(p3_rows) and p3_rows:
                # P3 fragments preserve absolute frame_index in the evidence rows.
                pass
            p3det = p3_by_id[oid]
            if len(xyz) != int(p3det["provider_metadata"]["cluster_points"]):
                raise ValueError(f"selected-point count differs from P3 Detection3D for {oid}")
            applied = engine.apply(object_id=str(object_id), observation_id=oid,
                                   timestamp_s=float(evidence["timestamp_s"]), xyz_W=xyz,
                                   rgb_u8=colors, pixel_rc=pixels, T_W_Copt=pose)
            observation = {"observation_id": oid, "frame_id": evidence["frame_id"],
                           "frame_index": evidence["frame_index"], "timestamp_s": evidence["timestamp_s"],
                           "object_id": str(object_id), "association_decision": decision,
                           "raw_detector_class": p3det["class_name"], "score": p3det["confidence"],
                           "point_file": str(point_path), "point_count": len(xyz),
                           "point_file_sha256": evidence["point_file_sha256"],
                           "mask_sha256": evidence["mask_sha256"], "rgb_sha256": evidence["rgb_sha256"],
                           "payload_sha256": applied["payload_sha256"],
                           "unique_sensor_pose_added": applied["unique_sensor_pose_added"],
                           "unique_sensor_pose_count": applied["unique_sensor_pose_count"],
                           "applied": True}
            grouped.setdefault(str(object_id), []).append(observation)
            snapshot = engine.snapshot(str(object_id))
            revision = len(grouped[str(object_id)])
            safe_id = hashlib.sha256(str(object_id).encode()).hexdigest()[:16]
            object_dir = objects_root / safe_id
            object_dir.mkdir(exist_ok=True)
            snapshot_name = f"revision_{revision:03d}.npz"
            snapshot_path = object_dir / snapshot_name
            np.savez_compressed(snapshot_path, voxel_keys=snapshot["voxel_keys"], xyz_W=snapshot["xyz_W"],
                                rgb_u8=snapshot["rgb_u8"], point_support=snapshot["point_support"],
                                bounds_min_W=snapshot["bounds_min_W"], bounds_max_W=snapshot["bounds_max_W"],
                                center_W=snapshot["center_W"])
            ply_path = object_dir / f"revision_{revision:03d}.ply"
            write_ply(ply_path, snapshot["xyz_W"], snapshot["rgb_u8"])
            observation["revision"] = revision
            observation["snapshot_npz"] = str(snapshot_path.relative_to(output))
            observation["snapshot_ply"] = str(ply_path.relative_to(output))
            event_rows.append(observation)

        summaries = []
        for object_id, obs in sorted(grouped.items()):
            state = engine.snapshot(object_id)
            safe_id = hashlib.sha256(object_id.encode()).hexdigest()[:16]
            object_dir = objects_root / safe_id
            first_path = output / obs[0]["snapshot_npz"]
            with np.load(first_path, allow_pickle=False) as first:
                first_xyz, first_rgb = first["xyz_W"].copy(), first["rgb_u8"].copy()
            final_path = object_dir / f"revision_{len(obs):03d}.png"
            render_cloud(final_path, state["xyz_W"], state["rgb_u8"], first_xyz, first_rgb)
            for row in obs:
                image_name = evidence_card(input_root, row["observation_id"], p2_by_id, images_root)
                row["evidence_image"] = f"{profile}/evidence_images/{image_name}" if image_name else None
            summaries.append({"object_id": object_id, "observation_count": len(obs),
                              "unique_sensor_pose_count": state["unique_sensor_pose_count"],
                              "raw_point_count": state["raw_point_count"], "voxel_count": state["voxel_count"],
                              "bounds_min_W": state["bounds_min_W"].tolist(), "bounds_max_W": state["bounds_max_W"].tolist(),
                              "center_W": state["center_W"].tolist(), "timeline_png": str(final_path.relative_to(output)),
                              "evidence": obs})
        with (mode_root / "object_timeline.jsonl").open("w", encoding="utf-8") as f:
            for row in event_rows:
                f.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        all_excluded[profile] = [row for row in event_rows
                                 if row.get("decision") in {"alias_excluded_from_fusion", "excluded_unassigned_or_deferred"}]
        all_summaries[profile] = summaries
        all_aliases[profile] = aliases
        (mode_root / "objects.json").write_text(json.dumps(summaries, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    elapsed = time.perf_counter() - started
    p3_ledger = jsonl(p3_output / "object_evidence_ledger.jsonl")
    capture_manifest = json.loads((input_root / "capture/capture_manifest.json").read_text(encoding="utf-8"))
    frame_index_by_id = {str(row["frame_id"]): int(row["frame_index"])
                         for row in capture_manifest.get("frames", [])}
    input_frames = []
    for batch in p3_rows:
        frame_id = str(batch.get("frame_id", ""))
        if frame_id not in frame_index_by_id:
            raise ValueError(f"P3 batch frame is absent from capture manifest: {frame_id}")
        input_frames.append(frame_index_by_id[frame_id])
    p2_index_doc = json.loads((input_root / "detection_2d/detection_2d_index.json").read_text(encoding="utf-8"))
    p2_included = set(input_frames)
    p2_count = sum(len(frame.get("detections", [])) for frame in p2_index_doc.get("frames", [])
                   if frame_index_by_id.get(str(frame["frame_id"])) in p2_included)
    frame_indices = sorted(set(input_frames))
    applied_count = sum(sum(int(obj["observation_count"]) for obj in objects)
                        for objects in all_summaries.values())
    deferred_count = 0
    for profile, _ in modes:
        deferred_count += sum(1 for row in jsonl(output / profile / "object_timeline.jsonl")
                              if row.get("decision") == "excluded_unassigned_or_deferred")
    manifest = {"schema_version": 1, "status": "completed", "kind": "offline_object_geometry_fusion",
                "input_run_id": input_root.name, "input_capture_sha256": sha(input_root / "capture/capture_manifest.json"),
                "input_p2_index_sha256": sha(input_root / "detection_2d/detection_2d_index.json"),
                "p3_partial_output": str(p3_output), "p3_detection_manifest_sha256": sha(p3_output / "detection_3d_manifest.json"),
                "p3_selected_point_relation": sidecar_contract["manifest"].get("fusion_reconstruction_provenance", "sidecar_relation_not_declared"),
                "authoritative_detection3d_parity": parity,
                "p3_evidence_index_sha256": sha(sidecar_index), "fusion_config": str(fusion_config),
                "fusion_config_sha256": sha(fusion_config), "voxel_size_m": cfg["voxel_size_m"],
                "association_assignments_are_fixed": True, "feedback_to_matcher": False,
                "semantic_state": "off", "appearance_state": "off", "gt_or_annotations_read_by_predictor": False,
                "modes": [profile for profile, _ in modes], "source_frame_indices": input_frames,
                "state_initialization": f"empty state at first included frame; source frame indices {frame_indices[0]}..{frame_indices[-1]}",
                "assigned_observations_applied": applied_count,
                "aliases_excluded": sum(len(items) for items in all_aliases.values()),
                "deferred_or_unassigned_excluded": deferred_count,
                "raw_p2_detection_count": p2_count, "p3_accepted_observations": len(raw_ids),
                "p3_rejected_observations": sum(r.get("decision") == "rejected" for r in p3_ledger),
                "observation_evidence_records": len(evidence_rows), "runtime_seconds": elapsed,
                "peak_process_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                "resource_policy": cfg["resource_policy"], "technical_scope": "OF0-OF2 geometry-only offline sidecar; no M9/graph/room score updates",
                "objects": all_summaries, "aliases": all_aliases, "excluded_observations": all_excluded}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    _write_index(output, manifest)
    return manifest


def _write_index(output: Path, manifest: dict) -> None:
    rows = []
    excluded_sections = []
    object_options = []
    for mode, objects in manifest["objects"].items():
        for obj in objects:
            object_key = "obj-" + hashlib.sha256(f"{mode}/{obj['object_id']}".encode()).hexdigest()[:16]
            object_options.append(f'<option value="{object_key}">{html.escape(mode)} / {html.escape(obj["object_id"])}</option>')
            evidence = "".join(
                f'<li><code>{html.escape(e["observation_id"])}</code> t={e["timestamp_s"]:.2f}s; '
                f'class={html.escape(str(e["raw_detector_class"]))}; {e["point_count"]} pts; '
                f'unique-sensor-pose-added={e["unique_sensor_pose_added"]} '
                f'<a href="{html.escape(e["evidence_image"] or "#")}">RGB/mask</a></li>' for e in obj["evidence"])
            rows.append(f'<article class="object-card" id="{object_key}"><h2>{html.escape(mode)} / {html.escape(obj["object_id"])}</h2>'
                         f'<p>obs={obj["observation_count"]}; unique sensor poses={obj["unique_sensor_pose_count"]}; '
                         f'points={obj["raw_point_count"]}; voxels={obj["voxel_count"]}</p>'
                         f'<p>bounds min={obj["bounds_min_W"]} max={obj["bounds_max_W"]}; center={obj["center_W"]}</p>'
                         f'<img class="cloud" src="{html.escape(obj["timeline_png"])}">'
                         f'<details><summary>Observation lineage</summary><ol>{evidence}</ol></details></article>')
    for mode, events in manifest.get("excluded_observations", {}).items():
        if not events:
            excluded_sections.append(f"<section><h2>{html.escape(mode)} aliases/deferred</h2><p>None</p></section>")
            continue
        table_rows = []
        for row in events:
            table_rows.append("<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else '—'))}</td>" for value in (
                row.get("observation_id"), row.get("decision"), row.get("canonical_observation_id"),
                row.get("object_id"), row.get("reason"), row.get("association_decision"))) + "</tr>")
        excluded_sections.append(f"<section><h2>{html.escape(mode)} aliases/deferred</h2><table><thead><tr>"
            "<th>Observation ID</th><th>Fusion decision</th><th>Canonical observation</th>"
            "<th>Track</th><th>Reason</th><th>Association decision</th></tr></thead><tbody>"
            + "".join(table_rows) + "</tbody></table></section>")
    provenance = {key: manifest.get(key) for key in ("input_run_id", "input_capture_sha256", "input_p2_index_sha256",
        "p3_detection_manifest_sha256", "p3_evidence_index_sha256", "association_assignments_are_fixed",
        "feedback_to_matcher", "semantic_state", "appearance_state", "p3_selected_point_relation", "source_frame_indices")}
    page = ("<!doctype html><meta charset='utf-8'><title>OF0-OF2 Geometry Fusion</title>"
            "<style>body{font:14px system-ui;max-width:1300px;margin:1.4rem auto;padding:0 1rem;color:#1e2630}"
            "article{border:1px solid #ccd3da;border-radius:8px;padding:1rem;margin:1rem 0}"
            ".cloud{width:min(100%,850px);height:auto;border:1px solid #ddd}code{overflow-wrap:anywhere}"
            "li{margin:.4rem 0}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd3da;padding:.4rem;text-align:left;overflow-wrap:anywhere}"
            "section{border:1px solid #ccd3da;border-radius:8px;padding:1rem;margin:1rem 0}</style><h1>Object geometry fusion — offline sidecar</h1>"
            "<p>Fixed assignments; prediction uses no GT. Geometry is diagnostic and is not projected into M9 or room scores."
            " First-observation vs fused-voxel clouds use the same world-coordinate axis limits.</p>"
            "<h2>Run provenance</h2><pre>" + html.escape(json.dumps(provenance, indent=2, sort_keys=True)) + "</pre>"
            + "<label for='object-picker'>Object: </label><select id='object-picker'><option value='all'>All objects</option>"
            + "".join(object_options) + "</select><script>document.getElementById('object-picker').addEventListener('change',e=>{"
            "document.querySelectorAll('.object-card').forEach(x=>x.hidden=e.target.value!=='all'&&x.id!==e.target.value)})</script>"
            + "\n".join(excluded_sections) + "\n".join(rows))
    (output / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-run", type=Path, required=True)
    ap.add_argument("--p3-output", type=Path, required=True)
    ap.add_argument("--replay", action="append", required=True, help="profile=/path/to/native replay run")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--config", type=Path, default=ROOT / "configs/fusion/object_geometry_v1.json")
    args = ap.parse_args()
    result = run(args.input_run.resolve(), args.p3_output.resolve(), args.replay, args.output.resolve(), args.config.resolve())
    print(json.dumps({"status": result["status"], "modes": result["modes"],
                      "raw_p3_observations": result["p3_accepted_observations"],
                      "runtime_seconds": result["runtime_seconds"], "output": str(args.output.resolve()),
                      "viewer": str(args.output.resolve() / "index.html")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
