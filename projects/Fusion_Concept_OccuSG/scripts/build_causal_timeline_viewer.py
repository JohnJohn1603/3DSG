#!/usr/bin/env python3
"""Build a dependency-free browser viewer for an M1 causal timeline.

Source snapshots/NPZ files stay untouched.  The builder emits sampled JSON
geometry as derived assets and a small HTML application that lazy-loads only
the selected revision.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
from pathlib import Path

import numpy as np


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def rel_url(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build(timeline: Path, output: Path, max_points: int = 300, causal_state: Path | None = None,
          unified_timeline: Path | None = None, room_belief_verified: Path | None = None,
          room_belief_assumed: Path | None = None) -> dict:
    timeline = Path(timeline).resolve()
    output = Path(output).resolve()
    if causal_state is not None and unified_timeline is not None:
        raise ValueError("choose either --causal-state or --unified-timeline")
    if room_belief_assumed is not None and room_belief_verified is None:
        raise ValueError("assumed-scope B viewer input requires its verified-only A control")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"viewer output must be new and empty: {output}")
    output.mkdir(parents=True, exist_ok=False)
    (output / "geometry").mkdir()

    manifest = json.loads((timeline / "manifest.json").read_text())
    causal_index = None
    causal_by_revision = {}
    unified_index = None
    unified_by_revision = {}
    if unified_timeline is not None:
        unified_timeline = Path(unified_timeline).resolve()
        unified_manifest_path = unified_timeline / "manifest.json"
        unified_frame_index_path = unified_timeline / "frame_index.json"
        if not unified_manifest_path.is_file() or not unified_frame_index_path.is_file():
            raise FileNotFoundError(f"unified graph timeline is incomplete: {unified_timeline}")
        unified_manifest = json.loads(unified_manifest_path.read_text())
        if unified_manifest.get("source_timeline_manifest_sha256") != sha(timeline / "manifest.json"):
            raise ValueError("unified graph timeline was not built from this exact object timeline")
        unified_rows = json.loads(unified_frame_index_path.read_text()).get("frames", [])
        if len(unified_rows) == 0:
            raise ValueError("unified graph frame index is empty")
        (output / "unified" / "revisions").mkdir(parents=True)
        copied_rows = []
        for row in unified_rows:
            source = unified_timeline / row["state"]
            if not source.is_file() or sha(source) != row.get("state_sha256"):
                raise ValueError(f"unified graph revision missing or hash mismatch: {source}")
            target = output / "unified" / row["state"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied = {**row, "state": Path("unified").joinpath(row["state"]).as_posix()}
            copied_rows.append(copied)
            unified_by_revision[int(row["revision"])] = copied["state"]
        unified_index = {"schema": "unified-graph-viewer-index/v1", "manifest": unified_manifest,
                         "source_manifest_sha256": sha(unified_manifest_path), "revisions": copied_rows}
        (output / "unified_index.json").write_text(json.dumps(unified_index, indent=2, sort_keys=True,
                                                               ensure_ascii=False) + "\n")
    if causal_state is not None:
        causal_state = Path(causal_state).resolve()
        causal_manifest_path = causal_state / "manifest.json"
        if not causal_manifest_path.is_file():
            raise FileNotFoundError(f"causal state manifest missing: {causal_manifest_path}")
        causal_manifest = json.loads(causal_manifest_path.read_text())
        (output / "causal" / "revisions").mkdir(parents=True)
        causal_rows = []
        for row in causal_manifest.get("revisions", []):
            source = causal_state / row["state"]
            target = output / "causal" / row["state"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied = {**row, "state": Path("causal") .joinpath(row["state"]).as_posix(),
                      "state_sha256": sha(source)}
            causal_rows.append(copied)
            causal_by_revision[int(row["revision"])] = copied["state"]
        causal_index = {"schema": "causal-hybrid-viewer-state/v1", "manifest": causal_manifest,
                        "source_manifest_sha256": sha(causal_manifest_path), "revisions": causal_rows}
        (output / "causal_index.json").write_text(json.dumps(causal_index, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    frame_index = json.loads((timeline / manifest["snapshot_index"]).read_text())["frames"]
    mapping_output = Path(manifest["mapping_output"])
    evidence_manifest_path = mapping_output / "diagnostic/evidence_manifest.json"
    evidence_raw = json.loads(evidence_manifest_path.read_text())
    evidence = {}
    for oid, row in evidence_raw.get("observations", {}).items():
        panels = {}
        for kind, value in row.get("panels", {}).items():
            source = mapping_output / "diagnostic" / value
            panels[kind] = {"url": rel_url(source, output), "source": str(source),
                            "sha256": sha(source) if source.is_file() else None}
        evidence[oid] = {**row, "panels": panels}
    (output / "evidence_index.json").write_text(json.dumps({
        "schema": "causal-timeline-viewer-evidence/v1", "source": str(evidence_manifest_path),
        "source_sha256": sha(evidence_manifest_path), "observations": evidence
    }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    frames = []
    all_xy = []
    for item in sorted(frame_index, key=lambda x: int(x["frame_index"])):
        snapshot_path = timeline / item["snapshot"]
        snap = load_snapshot(snapshot_path)
        object_source_revision = int(snap.get("revision", -1))
        if object_source_revision != int(item["revision"]):
            raise ValueError(f"object snapshot revision/index mismatch at frame {item['frame_index']}")
        room_ref = snap.get("rooms", {}).get("state_ref")
        room_path = timeline / room_ref if room_ref else None
        room_source_revision = None
        if room_path and room_path.is_file():
            room_doc = load_snapshot(room_path)
            room_state = room_doc.get("snapshot", room_doc)
            room_source_revision = int(room_state.get("revision", -1))
            if room_source_revision < 0:
                raise ValueError(f"room snapshot has no revision at frame {item['frame_index']}")
            room_timestamp = room_state.get("timestamp_s")
            if room_timestamp is not None and abs(float(room_timestamp) - float(snap.get("timestamp_s"))) > 1e-6:
                raise ValueError(f"room snapshot timestamp/index mismatch at frame {item['frame_index']}")
        geometry_rel = Path("geometry") / f"rev-{int(item['revision']):06d}.json"
        geometry_path = output / geometry_rel
        objects = []
        for obj in snap.get("objects", []):
            source = timeline / obj["cloud_npz"]
            cloud = np.load(source, allow_pickle=False)
            xyz = np.asarray(cloud["xyz"], dtype=np.float32)
            rgb = np.asarray(cloud.get("rgb", np.zeros((len(xyz), 3), dtype=np.uint8)), dtype=np.uint8)
            if len(xyz) > max_points:
                indices = np.linspace(0, len(xyz) - 1, max_points, dtype=np.int64)
                xyz, rgb = xyz[indices], rgb[indices]
            if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
                raise ValueError(f"invalid geometry for {obj['hypothesis_id']}")
            objects.append({"hypothesis_id": obj["hypothesis_id"],
                            "points": xyz.astype(float).tolist(),
                            "colors": rgb.astype(int).tolist(),
                            "source_npz": str(source),
                            "source_sha256": obj["cloud_sha256"],
                            "sampled_points": int(len(xyz)),
                            "source_points": int(obj["point_count"])})
            all_xy.extend((float(p[0]), float(p[1])) for p in xyz)
        geometry_path.write_text(json.dumps({
            "schema": "causal-timeline-derived-geometry/v1", "source_timeline": str(timeline),
            "source_snapshot": str(snapshot_path), "source_snapshot_sha256": sha(snapshot_path),
            "max_points": int(max_points), "objects": objects
        }, separators=(",", ":"), ensure_ascii=False) + "\n")
        pose = snap.get("camera_pose_W_Copt")
        if pose and len(pose) >= 2:
            all_xy.append((float(pose[0][3]), float(pose[1][3])))
        # The frame index intentionally contains only indexing metadata; the
        # recorder snapshot is the authoritative source for timestamp.
        frames.append({**item, "timestamp_s": snap.get("timestamp_s"),
                       "object_source_revision": object_source_revision,
                       "room_source_revision": room_source_revision,
                       "snapshot_url": rel_url(snapshot_path, output),
                       "snapshot_sha256": sha(snapshot_path),
                       "room_url": rel_url(room_path, output) if room_path and room_path.is_file() else None,
                       "camera_pose_W_Copt": pose,
                       "geometry_url": geometry_rel.as_posix(),
                       "causal_url": causal_by_revision.get(int(item["revision"])),
                       "unified_url": unified_by_revision.get(int(item["revision"]))})
    room_belief_index = None
    if room_belief_verified is not None:
        room_belief_index = _copy_room_belief_prefixes(
            timeline, output, frames, room_belief_verified, room_belief_assumed)
    if unified_index:
        expected = {(int(row["frame_index"]), int(row["revision"])) for row in unified_index["revisions"]}
        actual = {(int(row["frame_index"]), int(row["revision"])) for row in frames}
        if expected != actual:
            raise ValueError("unified graph and viewer frame/revision populations differ")
    # Include room polygon extents in the fixed view bounds.
    for item in frames:
        if item["room_url"]:
            room_path = output / item["room_url"]
            room = load_snapshot(room_path)
            for node in room.get("snapshot", room).get("nodes", []):
                if node.get("kind") == "room":
                    all_xy.extend((float(p[0]), float(p[1])) for p in node.get("attributes", {}).get("outer_ring", []))
    if not all_xy:
        all_xy = [(0.0, 0.0), (1.0, 1.0)]
    min_x, max_x = min(x for x, _ in all_xy), max(x for x, _ in all_xy)
    min_y, max_y = min(y for _, y in all_xy), max(y for _, y in all_xy)
    pad_x, pad_y = max((max_x - min_x) * .08, .3), max((max_y - min_y) * .08, .3)
    stage_info = {"online_hypotheses": "available", "prefix_admission": "available" if causal_index else "unavailable",
                  "final_postprocess": "separate_reference_artifact_not_loaded"}
    if causal_index:
        stage_info.update({"causal_membership": causal_index.get("manifest", {}).get("causality", {}).get("membership"),
                           "room_semantics": causal_index.get("manifest", {}).get("causality", {}).get("room_semantics")})
    if unified_index:
        stage_info.update({"unified_graph": "available",
                           "causal_membership": unified_index["manifest"].get("causality", {}).get("membership"),
                           "room_semantics": unified_index["manifest"].get("causality", {}).get("room_semantics")})
    viewer_index = {
        "schema": "causal-timeline-viewer-index/v1", "source_timeline": str(timeline),
        "source_manifest_sha256": sha(timeline / "manifest.json"),
        "timeline_manifest": manifest, "frames": frames,
        "evidence_index": "evidence_index.json",
        "causal_index": "causal_index.json" if causal_index else None,
        "unified_index": "unified_index.json" if unified_index else None,
        "room_belief_index": "room_belief_index.json" if room_belief_index else None,
        "world_bounds_xy": [min_x - pad_x, max_x + pad_x, min_y - pad_y, max_y + pad_y],
        "derived_geometry_policy": {"max_points_per_object": int(max_points), "source_npz_unchanged": True},
        "stages": stage_info,
    }
    (output / "timeline_index.json").write_text(json.dumps(viewer_index, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    (output / "viewer_manifest.json").write_text(json.dumps({
        "schema": "causal-timeline-viewer/v1", "status": "complete",
        "source_timeline": str(timeline), "source_manifest_sha256": sha(timeline / "manifest.json"),
        "derived_assets": ["timeline_index.json", "evidence_index.json", "geometry/", "xyz_renderer.js"] + (["causal_index.json", "causal/"] if causal_index else []) + (["unified_index.json", "unified/"] if unified_index else []) + (["room_belief_index.json", "room_belief/"] if room_belief_index else []),
        "max_points_per_object": int(max_points), "geometry_source_is_read_only": True,
        "cache_policy": {"kind": "lru", "max_revision_entries": 6,
                          "selected_revision_only_fetch": True},
        "room_belief": ({"index_path": "room_belief_index.json",
                         "index_sha256": sha(output / "room_belief_index.json"),
                         "scope_modes": room_belief_index["scope_modes"],
                         "variants": room_belief_index["variants"],
                         "assumed_declaration": room_belief_index["assumed_declaration"],
                         "belief_values_are_accuracy_probabilities": False}
                        if room_belief_index else None),
        "server_root_required": str(output.parent)
    }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    (output / "index.html").write_text(_html(), encoding="utf-8")
    shutil.copy2(Path(__file__).with_name("xyz_renderer.js"), output / "xyz_renderer.js")
    return viewer_index


def _html() -> str:
    template = Path(__file__).with_name("causal_timeline_viewer.html")
    return template.read_text(encoding="utf-8")


def _copy_room_belief_prefixes(timeline: Path, output: Path, frames: list[dict],
                               verified_dir: Path, assumed_dir: Path | None = None) -> dict:
    """Copy and validate T3 prefix states against displayed object/room revisions.

    A verified-only run is valid on its own. An assumed B run can only be
    displayed beside its A control and carries explicit declaration provenance.
    """
    verified_dir = verified_dir.resolve()
    assumed_dir = assumed_dir.resolve() if assumed_dir is not None else None
    a_manifest_path = verified_dir / "manifest.json"
    b_manifest_path = assumed_dir / "manifest.json" if assumed_dir is not None else None
    if not a_manifest_path.is_file() or (b_manifest_path is not None and not b_manifest_path.is_file()):
        raise FileNotFoundError("T3 room-belief manifest is missing")
    a_manifest = load_snapshot(a_manifest_path)
    b_manifest = load_snapshot(b_manifest_path) if b_manifest_path is not None else None
    source_manifest = load_snapshot(timeline / "manifest.json")
    if a_manifest.get("schema") != "causal-hybrid-room-belief-timeline/v1" or (b_manifest is not None and b_manifest.get("schema") != a_manifest.get("schema")):
        raise ValueError("unsupported T3 room-belief manifest schema")
    if b_manifest is not None and (a_manifest.get("scene_id"), a_manifest.get("sequence_id"), a_manifest.get("run_id")) != (
            b_manifest.get("scene_id"), b_manifest.get("sequence_id"), b_manifest.get("run_id")):
        raise ValueError("T3 A/B scene, sequence, or run identities differ")
    if (source_manifest.get("scene_id"), source_manifest.get("sequence_id")) != (
            a_manifest.get("scene_id"), a_manifest.get("sequence_id")):
        raise ValueError("T3 scene/sequence does not match the displayed object timeline")
    if a_manifest.get("scope_status") != "unavailable":
        raise ValueError("this A-only viewer expects verified-only scope to remain unavailable")
    if b_manifest is not None and b_manifest.get("scope_status") != "assumed":
        raise ValueError("T3 B must carry assumed scope status")
    if b_manifest is not None:
        a_provenance = a_manifest.get("input_provenance")
        b_provenance = b_manifest.get("input_provenance")
        if not isinstance(a_provenance, dict) or not isinstance(b_provenance, dict):
            raise ValueError("T3 A/B input provenance is missing")
        # B may carry additional scope/declaration hashes. Every field already
        # attested by the A control must still match exactly.
        mismatched = [key for key, value in a_provenance.items() if b_provenance.get(key) != value]
        if mismatched:
            raise ValueError(f"T3 A/B common input provenance differs: {mismatched}")
    variants = a_manifest.get("variants")
    if not isinstance(variants, list) or len(variants) != 4 or (b_manifest is not None and variants != b_manifest.get("variants")):
        raise ValueError("T3 must expose four explicit A variants")
    declaration_id = b_manifest.get("scope_declaration_id") if b_manifest is not None else None
    declaration_sha = b_manifest.get("scope_declaration_sha256") if b_manifest is not None else None
    if b_manifest is not None and (not declaration_id or not declaration_sha):
        raise ValueError("T3 B is missing assumed-scope declaration provenance")
    if len(frames) != int(a_manifest.get("frame_count", -1)) or (b_manifest is not None and len(frames) != int(b_manifest.get("frame_count", -1))):
        raise ValueError("T3 and viewer frame counts differ")

    root = output / "room_belief"
    (root / "verified_only" / "revisions").mkdir(parents=True)
    shutil.copy2(a_manifest_path, root / "verified_only" / "manifest.json")
    declaration_path = None
    if b_manifest is not None:
        (root / "assumed_single_level_demo" / "revisions").mkdir(parents=True)
        shutil.copy2(b_manifest_path, root / "assumed_single_level_demo" / "manifest.json")
        declaration_path = Path(b_manifest.get("scope_declaration", {}).get("path", ""))
        if declaration_path.is_file():
            shutil.copy2(declaration_path, root / "assumed_single_level_demo" / "declaration.json")

    a_rows = a_manifest.get("revisions", [])
    b_rows = b_manifest.get("revisions", []) if b_manifest is not None else []
    if len(a_rows) != len(frames) or (b_manifest is not None and len(b_rows) != len(frames)):
        raise ValueError("T3 revision population does not match the displayed frames")
    indexed = []
    for idx, (frame, a_ref) in enumerate(zip(frames, a_rows)):
        frame_index, frame_id = int(frame["frame_index"]), str(frame["frame_id"])
        object_revision = int(frame.get("object_source_revision", frame["revision"]))
        room_revision_expected = frame.get("room_source_revision")
        if (int(a_ref["frame_index"]), a_ref["frame_id"], int(a_ref["revision"])) != (frame_index, frame_id, object_revision):
            raise ValueError(f"T3 A frame/revision mismatch at frame {frame_index}")
        b_ref = b_rows[idx] if b_manifest is not None else None
        if b_ref is not None and (int(b_ref["frame_index"]), b_ref["frame_id"], int(b_ref["revision"])) != (frame_index, frame_id, object_revision):
            raise ValueError(f"T3 B frame/revision mismatch at frame {frame_index}")
        a_source = verified_dir / a_ref["state"]
        b_source = assumed_dir / b_ref["state"] if b_ref is not None else None
        if not a_source.is_file() or (b_source is not None and not b_source.is_file()):
            raise FileNotFoundError(f"T3 prefix state missing at frame {frame_index}")
        if sha(a_source) != a_ref.get("state_sha256", sha(a_source)) or (b_source is not None and sha(b_source) != b_ref.get("state_sha256", sha(b_source))):
            raise ValueError(f"T3 prefix state hash mismatch at frame {frame_index}")
        a_state = load_snapshot(a_source)
        b_state = load_snapshot(b_source) if b_source is not None else None
        for label, state in (("A", a_state), ("B", b_state)):
            if state is None:
                continue
            if (int(state.get("frame_index", -1)), state.get("frame_id"), int(state.get("revision", -1))) != (frame_index, frame_id, object_revision):
                raise ValueError(f"T3 {label} state identity mismatch at frame {frame_index}")
            if state.get("future_observations_used") is not False:
                raise ValueError(f"T3 {label} contains future evidence at frame {frame_index}")
        room_path = output / frame["room_url"] if frame.get("room_url") else None
        room_revision = None
        if room_path and room_path.is_file():
            room_doc = load_snapshot(room_path)
            room_revision = int((room_doc.get("snapshot") or room_doc).get("revision", -1))
        if room_revision is None or (room_revision_expected is not None and room_revision != int(room_revision_expected)):
            raise ValueError(f"displayed room revision is missing/misaligned at frame {frame_index}")
        if int(a_state.get("room_lifecycle", {}).get("room_revision", -2)) != room_revision:
            raise ValueError(f"T3 room revision does not match displayed room geometry at frame {frame_index}")
        if b_state is not None and int(b_state.get("room_lifecycle", {}).get("room_revision", -2)) != room_revision:
            raise ValueError(f"T3 room revision does not match displayed room geometry at frame {frame_index}")
        if a_state.get("scope_status") != "unavailable" or a_state.get("room_contains_edges"):
            raise ValueError(f"T3 A has membership despite verified-only scope being unavailable at frame {frame_index}")
        if any(obj.get("membership", {}).get("status") != "unavailable" for obj in a_state.get("objects", [])):
            raise ValueError(f"T3 A contains object membership while its scope gate is closed at frame {frame_index}")
        if any(room.get("label") is not None or room.get("category_counts")
               for variant in variants for room in a_state.get("rooms", {}).get(variant, {}).values()):
            raise ValueError(f"T3 A contains room evidence/labels while its scope gate is closed at frame {frame_index}")
        if b_state is not None:
            if b_state.get("scope_status") != "assumed" or b_state.get("declaration_id") != declaration_id or b_state.get("declaration_sha256") != declaration_sha:
                raise ValueError(f"T3 B declaration provenance mismatch at frame {frame_index}")
            if any(obj.get(channel, {}).get("scope_status") != "assumed" or
                   obj.get(channel, {}).get("declaration_id") != declaration_id or
                   obj.get(channel, {}).get("declaration_sha256") != declaration_sha
                   for obj in b_state.get("objects", []) for channel in ("membership", "semantic")):
                raise ValueError(f"T3 B object output is missing declaration provenance at frame {frame_index}")
            if any(room.get("scope_status") != "assumed" or room.get("declaration_id") != declaration_id or
                   room.get("declaration_sha256") != declaration_sha
                   for variant in variants for room in b_state.get("rooms", {}).get(variant, {}).values()):
                raise ValueError(f"T3 B room output is missing declaration provenance at frame {frame_index}")

        a_rel = Path("room_belief/verified_only/revisions") / f"rev-{object_revision:06d}.json"
        shutil.copy2(a_source, output / a_rel)
        index_row = {"frame_index": frame_index, "frame_id": frame_id, "timestamp_s": frame["timestamp_s"],
                     "object_revision": object_revision, "room_revision": room_revision,
                     "verified_only": {"url": a_rel.as_posix(), "sha256": sha(a_source), "scope_status": "unavailable"}}
        if b_source is not None:
            b_rel = Path("room_belief/assumed_single_level_demo/revisions") / f"rev-{object_revision:06d}.json"
            shutil.copy2(b_source, output / b_rel)
            index_row["assumed_single_level_demo"] = {"url": b_rel.as_posix(), "sha256": sha(b_source), "scope_status": "assumed",
                "declaration_id": declaration_id, "declaration_sha256": declaration_sha}
        indexed.append(index_row)
    result = {"schema": "t3-room-belief-viewer-index/v1", "status": "complete",
            "source_run_id": a_manifest["run_id"], "scene_id": a_manifest["scene_id"],
            "sequence_id": a_manifest["sequence_id"], "variants": variants,
            "scope_modes": ["verified_only"] + (["assumed_single_level_demo"] if b_manifest is not None else []),
            "source_manifests": {"verified_only": {"path": str(a_manifest_path), "sha256": sha(a_manifest_path)},
                                 **({"assumed_single_level_demo": {"path": str(b_manifest_path), "sha256": sha(b_manifest_path)}} if b_manifest_path is not None else {})},
            "input_provenance": a_manifest["input_provenance"],
            "assumed_declaration": ({"id": declaration_id, "sha256": declaration_sha,
                "scope_status": "assumed", "evidence_status": "assumed_not_verified",
                "path": str(declaration_path) if declaration_path else None} if b_manifest is not None else None),
            "belief_values_are_accuracy_probabilities": False, "revisions": indexed}
    (output / "room_belief_index.json").write_text(json.dumps(result, indent=2, sort_keys=True,
                                                                 ensure_ascii=False) + "\n")
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--timeline", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--max-points", type=int, default=300)
    p.add_argument("--causal-state", type=Path, default=None,
                   help="optional causal hybrid state directory produced by run_causal_hybrid_room_belief.py")
    p.add_argument("--unified-timeline", type=Path, default=None,
                   help="optional unified graph timeline built from matching object/room/causal prefixes")
    p.add_argument("--room-belief-verified", type=Path, default=None,
                   help="optional T3 verified-only A output directory")
    p.add_argument("--room-belief-assumed", type=Path, default=None,
                   help="optional T3 approved assumed-scope B output directory")
    a = p.parse_args()
    result = build(a.timeline, a.output, a.max_points, a.causal_state, a.unified_timeline,
                   a.room_belief_verified, a.room_belief_assumed)
    print(json.dumps({"status": "complete", "output": str(a.output.resolve()),
                      "frames": len(result["frames"]), "max_points": a.max_points}, indent=2))


if __name__ == "__main__":
    main()
