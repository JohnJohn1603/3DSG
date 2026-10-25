"""Join immutable object, room, and causal room-belief prefixes by revision.

This is a serialization/integration layer only. It does not run association,
membership, semantic consensus, or room-belief prediction.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "unified-incremental-scene-graph/v1"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _room_doc_payload(doc: dict[str, Any]) -> dict[str, Any]:
    return doc.get("snapshot", doc)


def _frame_from_observation_id(observation_id: str) -> int | None:
    match = re.search(r"-(\d{4,}):det-", observation_id)
    return int(match.group(1)) if match else None


def _validate_source_pair(timeline: Path, causal_state: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[int, dict[str, Any]]]:
    timeline_manifest_path = timeline / "manifest.json"
    causal_manifest_path = causal_state / "manifest.json"
    if not timeline_manifest_path.is_file():
        raise FileNotFoundError(f"object timeline manifest missing: {timeline_manifest_path}")
    if not causal_manifest_path.is_file():
        raise FileNotFoundError(f"causal state manifest missing: {causal_manifest_path}")
    tm = _read(timeline_manifest_path)
    cm = _read(causal_manifest_path)
    if tm.get("schema") != "conceptgraphs-causal-object-timeline/v1":
        raise ValueError(f"unsupported object timeline schema: {tm.get('schema')!r}")
    if cm.get("schema") != "causal-hybrid-room-belief-timeline/v1":
        raise ValueError(f"unsupported causal state schema: {cm.get('schema')!r}")
    if (tm.get("scene_id"), tm.get("sequence_id")) != (cm.get("scene_id"), cm.get("sequence_id")):
        raise ValueError("object and causal state scene/sequence IDs differ")
    expected_timeline_hash = cm.get("source_timeline_manifest_sha256")
    actual_timeline_hash = sha256(timeline_manifest_path)
    if expected_timeline_hash != actual_timeline_hash:
        raise ValueError("causal state was not produced from this exact object timeline manifest")
    frame_index_path = timeline / tm.get("snapshot_index", "frame_index.json")
    if not frame_index_path.is_file():
        raise FileNotFoundError(f"object frame index missing: {frame_index_path}")
    frame_rows = _read(frame_index_path).get("frames")
    if not isinstance(frame_rows, list) or not frame_rows:
        raise ValueError("object frame index must contain non-empty frames")
    causal_rows = cm.get("revisions")
    if not isinstance(causal_rows, list):
        raise ValueError("causal manifest revisions must be a list")
    causal_by_frame = {int(row["frame_index"]): row for row in causal_rows}
    if len(causal_by_frame) != len(causal_rows):
        raise ValueError("causal manifest contains duplicate frame indices")
    if {int(row["frame_index"]) for row in frame_rows} != set(causal_by_frame):
        raise ValueError("object and causal frame populations differ")
    return tm, cm, sorted(frame_rows, key=lambda row: int(row["frame_index"])), causal_by_frame


def _semantic_state(causal: dict[str, Any], room_id: str, variant: str) -> dict[str, Any]:
    scope_status = causal.get("membership_scope", {}).get("status", "unavailable")
    membership_valid = bool(causal.get("membership_causal")) and scope_status in {
        "verified", "verified_single_observed_floor_scope"
    }
    controller_state = causal.get("rooms", {}).get(variant, {}).get(room_id)
    if not membership_valid or causal.get("room_semantics_status") != "causal":
        reason = causal.get("membership_scope", {}).get("reason") or "membership_unavailable"
        return {
            "status": "unavailable",
            "reason": "membership_unavailable_elevation_scope" if not membership_valid else reason,
            "label": None,
            "beliefs": None,
            "candidates": None,
            "evidence_revision": None,
            "supporting_object_ids": [],
            "source_controller_status": controller_state.get("status") if controller_state else None,
        }
    if controller_state is None:
        return {"status": "unavailable", "reason": "room_not_in_controller_revision",
                "label": None, "beliefs": None, "candidates": None,
                "evidence_revision": None, "supporting_object_ids": []}
    return {
        "status": controller_state.get("status", "unknown"),
        "reason": controller_state.get("reason"),
        "label": controller_state.get("label"),
        "beliefs": controller_state.get("candidates"),
        "candidates": controller_state.get("candidates"),
        "raw_scores": controller_state.get("raw_scores"),
        "evidence_revision": controller_state.get("evidence_revision"),
        "eligible_categories": sorted(controller_state.get("category_counts", {})),
        "supporting_object_ids": controller_state.get("supporting_object_ids", []),
        "last_supported_label": controller_state.get("last_supported_label"),
        "label_history": controller_state.get("label_history", []),
        "score_kind": controller_state.get("score_kind"),
    }


def build_unified_revision(timeline: Path, timeline_row: dict[str, Any],
                           causal_state_root: Path, causal_row: dict[str, Any],
                           variants: list[str]) -> dict[str, Any]:
    """Create one immutable graph revision from exactly matching prefix inputs."""
    snapshot_path = timeline / timeline_row["snapshot"]
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"object prefix snapshot missing: {snapshot_path}")
    source = _read(snapshot_path)
    causal_path = causal_state_root / causal_row["state"]
    if not causal_path.is_file():
        raise FileNotFoundError(f"causal prefix state missing: {causal_path}")
    causal = _read(causal_path)
    frame = int(source["frame_index"])
    revision = int(source["revision"])
    if (frame, revision, source["frame_id"]) != (int(causal.get("frame_index", -1)),
                                                  int(causal.get("revision", -1)),
                                                  causal.get("frame_id")):
        raise ValueError(f"object and causal prefix revision mismatch at frame {frame}")
    if causal.get("future_observations_used") is not False or source.get("future_observations_used") is not False:
        raise ValueError(f"future state is not allowed in graph revision {revision}")

    room_ref = source.get("rooms", {}).get("state_ref")
    if not room_ref:
        raise ValueError(f"room prefix reference missing at frame {frame}")
    room_path = timeline / room_ref
    if not room_path.is_file():
        raise FileNotFoundError(f"room prefix revision missing: {room_path}")
    room_doc = _read(room_path)
    room_snapshot = _room_doc_payload(room_doc)
    room_nodes = [node for node in room_snapshot.get("nodes", []) if node.get("kind") == "room"]
    room_ids = {str(node["node_id"]) for node in room_nodes}
    lifecycle = causal.get("room_lifecycle", {})
    active_ids = set(map(str, lifecycle.get("active_room_ids", [])))
    if room_ids != active_ids:
        raise ValueError(f"room lifecycle and room geometry differ at frame {frame}: {sorted(active_ids)} != {sorted(room_ids)}")

    source_objects = source.get("objects", [])
    causal_objects = causal.get("objects", [])
    source_by_id = {str(obj["hypothesis_id"]): obj for obj in source_objects}
    causal_by_id = {str(obj.get("snapshot_hypothesis_id")): obj for obj in causal_objects}
    if len(source_by_id) != len(source_objects) or len(causal_by_id) != len(causal_objects):
        raise ValueError(f"duplicate object hypothesis IDs at frame {frame}")
    if set(source_by_id) != set(causal_by_id):
        raise ValueError(f"object/causal hypothesis population differs at frame {frame}")

    objects = []
    graph_nodes = []
    for hypothesis_id in sorted(source_by_id):
        obj = source_by_id[hypothesis_id]
        cobj = causal_by_id[hypothesis_id]
        observations = [str(value) for value in obj.get("observation_ids", [])]
        bad = [oid for oid in observations if (_frame_from_observation_id(oid) is not None
                                                and _frame_from_observation_id(oid) > frame)]
        if bad:
            raise ValueError(f"future observation(s) in {hypothesis_id} at frame {frame}: {bad}")
        admission = dict(cobj.get("prefix_admission", {}))
        semantic = dict(cobj.get("semantic", {}))
        membership = dict(cobj.get("membership", {}))
        eligibility_reasons = []
        if not admission.get("eligible"):
            eligibility_reasons.append(admission.get("reason") or "prefix_admission_ineligible")
        if semantic.get("status") != "stable" or not semantic.get("category"):
            eligibility_reasons.append(f"semantic_{semantic.get('status', 'unavailable')}")
        if membership.get("status") != "assigned":
            eligibility_reasons.append(membership.get("reason") or f"membership_{membership.get('status', 'unavailable')}")
        eligible = bool(cobj.get("room_evidence_eligible", False))
        object_state = {
            "node_id": f"object:{hypothesis_id}",
            "kind": "object",
            "hypothesis_id": hypothesis_id,
            "snapshot_hypothesis_id": hypothesis_id,
            "causal_object_id": cobj.get("causal_object_id"),
            "raw_class_history": list(obj.get("class_history_raw", [])),
            "observation_ids": observations,
            "detection_count": int(obj.get("detection_count", 0)),
            "distinct_frame_count": int(obj.get("distinct_frame_count", 0)),
            "distinct_pose_count": int(obj.get("distinct_pose_count", 0)),
            "geometry_revision": obj.get("geometry_revision"),
            "geometry_ref": {"path": obj.get("cloud_npz"), "sha256": obj.get("cloud_sha256"),
                             "point_count": obj.get("point_count"), "bounds_minmax": obj.get("bounds_minmax")},
            "prefix_admission": admission,
            "semantic_consensus": semantic,
            "membership": membership,
            "evidence_eligibility": {"eligible": eligible, "reasons": eligibility_reasons},
            "source_kind": "hybrid_cached_perception_conceptgraphs_mapping_core",
        }
        objects.append(object_state)
        graph_nodes.append({"node_id": object_state["node_id"], "kind": "object",
                            "attributes": object_state})

    rooms = []
    for node in sorted(room_nodes, key=lambda value: str(value["node_id"])):
        rid = str(node["node_id"])
        semantic_variants = {variant: _semantic_state(causal, rid, variant) for variant in variants}
        attrs = dict(node.get("attributes", {}))
        room_state = {
            "node_id": rid,
            "kind": "room",
            "name": attrs.get("name", rid),
            "geometry_revision": room_snapshot.get("revision"),
            "geometry": {key: attrs[key] for key in ("outer_ring", "holes", "centroid_xy") if key in attrs},
            "semantic_by_variant": semantic_variants,
            "semantic_availability": {
                "status": "available" if causal.get("room_semantics_status") == "causal"
                         and causal.get("membership_causal") else "unavailable",
                "reason": None if causal.get("room_semantics_status") == "causal"
                          and causal.get("membership_causal") else "membership_unavailable_elevation_scope",
            },
            "source_semantic_state": {"path": causal_row["state"], "sha256": sha256(causal_path)},
        }
        rooms.append(room_state)
        graph_nodes.append({"node_id": rid, "kind": "room", "attributes": room_state})

    edges = []
    for edge in room_snapshot.get("edges", []):
        if edge.get("source_id") in room_ids and edge.get("target_id") in room_ids:
            edges.append({"source_id": edge["source_id"], "target_id": edge["target_id"],
                          "relation": edge.get("relation"), "directed": bool(edge.get("directed", False)),
                          "provenance": edge.get("provenance"), "source_kind": "room_revision"})
    membership_edges = causal.get("room_contains_edges", [])
    membership_valid = bool(causal.get("membership_causal")) and causal.get("membership_scope", {}).get("status") in {
        "verified", "verified_single_observed_floor_scope"
    }
    if membership_edges and not membership_valid:
        raise ValueError(f"membership edges exist while scope is unavailable at frame {frame}")
    causal_id_to_hypothesis = {str(cobj.get("causal_object_id")): hypothesis_id
                               for hypothesis_id, cobj in causal_by_id.items()}
    for edge in membership_edges:
        room_id = str(edge.get("source_id"))
        target_causal_id = str(edge.get("target_id"))
        hypothesis_id = causal_id_to_hypothesis.get(target_causal_id)
        if room_id not in room_ids or hypothesis_id is None:
            raise ValueError(f"membership edge has no active room/object at frame {frame}: {edge}")
        obj = causal_by_id[hypothesis_id]
        if obj.get("membership", {}).get("status") != "assigned" or obj["membership"].get("room_id") != room_id:
            raise ValueError(f"membership edge conflicts with object membership at frame {frame}: {edge}")
        edges.append({"source_id": room_id, "target_id": f"object:{hypothesis_id}",
                      "relation": "room_contains", "directed": True,
                      "provenance": edge.get("provenance"), "source_kind": "causal_hybrid_membership"})
    edge_keys = [(e["source_id"], e["target_id"], e["relation"]) for e in edges]
    if len(edge_keys) != len(set(edge_keys)):
        raise ValueError(f"duplicate graph edges at frame {frame}")
    node_ids = {node["node_id"] for node in graph_nodes}
    for edge in edges:
        if edge["source_id"] not in node_ids or edge["target_id"] not in node_ids:
            raise ValueError(f"dangling edge at frame {frame}: {edge}")

    return {
        "schema": SCHEMA,
        "revision": revision,
        "graph_revision_id": f"{source.get('scene_id')}:{source.get('sequence_id')}:r{revision:06d}",
        "frame_index": frame,
        "frame_id": source.get("frame_id"),
        "timestamp_s": source.get("timestamp_s"),
        "scene_id": source.get("scene_id"),
        "sequence_id": source.get("sequence_id"),
        "future_observations_used": False,
        "causality": {
            "objects": "prefix_state_only",
            "rooms": "prefix_state_only_native_replay_reference",
            "membership": "prefix_greedy_voxel_support" if membership_valid else "unavailable_elevation_scope",
            "room_semantics": "causal_rb1_prefix" if membership_valid and causal.get("room_semantics_status") == "causal"
                             else "unavailable_membership_scope",
        },
        "stage_semantics": {"objects": "online_hypotheses", "admission": "prefix_eligibility",
                            "final_postprocess": "separate_not_in_revision"},
        "room_lifecycle": lifecycle,
        "membership_scope": causal.get("membership_scope", {}),
        "membership_availability": {"status": "available" if membership_valid else "unavailable",
                                    "reason": None if membership_valid else "unavailable_elevation_scope"},
        "room_semantics_availability": {"status": "available" if membership_valid and causal.get("room_semantics_status") == "causal" else "unavailable",
                                        "reason": None if membership_valid and causal.get("room_semantics_status") == "causal"
                                                 else "membership_unavailable_elevation_scope"},
        "objects": objects,
        "rooms": rooms,
        "nodes": graph_nodes,
        "edges": edges,
        "source_revisions": {
            "object": {"path": timeline_row["snapshot"], "revision": revision,
                       "sha256": sha256(snapshot_path)},
            "room": {"path": room_ref, "revision": room_snapshot.get("revision"),
                     "sha256": sha256(room_path)},
            "causal": {"path": causal_row["state"], "revision": revision,
                       "sha256": sha256(causal_path)},
        },
        "source_manifest_hashes": {
            "object_timeline": sha256(timeline / "manifest.json"),
            "causal_timeline": sha256(causal_state_root / "manifest.json"),
        },
        "semantic_variants": variants,
    }


def build_unified_timeline(timeline: Path, causal_state: Path, output: Path) -> dict[str, Any]:
    timeline = Path(timeline).resolve()
    causal_state = Path(causal_state).resolve()
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"unified timeline output must be new or empty: {output}")
    tm, cm, frame_rows, causal_by_frame = _validate_source_pair(timeline, causal_state)
    variants = list(cm.get("variants", []))
    if not variants:
        raise ValueError("causal timeline has no semantic variants")
    output.mkdir(parents=True, exist_ok=True)
    (output / "revisions").mkdir(exist_ok=True)
    rows = []
    for timeline_row in frame_rows:
        frame = int(timeline_row["frame_index"])
        causal_row = causal_by_frame[frame]
        if int(timeline_row["revision"]) != int(causal_row["revision"]):
            raise ValueError(f"revision index mismatch at frame {frame}")
        graph = build_unified_revision(timeline, timeline_row, causal_state, causal_row, variants)
        rel = f"revisions/rev-{int(graph['revision']):06d}.json"
        target = output / rel
        target.write_text(json.dumps(graph, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                          encoding="utf-8")
        rows.append({"frame_index": frame, "frame_id": graph["frame_id"],
                     "revision": graph["revision"], "state": rel,
                     "state_sha256": sha256(target), "future_observations_used": False,
                     "object_count": len(graph["objects"]), "room_count": len(graph["rooms"]),
                     "edge_count": len(graph["edges"]),
                     "membership_status": graph["membership_availability"]["status"],
                     "room_semantics_status": graph["room_semantics_availability"]["status"]})
    manifest = {
        "schema": "unified-incremental-scene-graph-timeline/v1",
        "status": "complete",
        "scene_id": tm.get("scene_id"), "sequence_id": tm.get("sequence_id"),
        "revision_count": len(rows), "revisions": rows,
        "source_timeline": str(timeline), "source_timeline_manifest_sha256": sha256(timeline / "manifest.json"),
        "source_causal_state": str(causal_state), "source_causal_manifest_sha256": sha256(causal_state / "manifest.json"),
        "variants": variants,
        "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference",
                      "membership": "per_revision_status_recorded",
                      "room_semantics": "per_revision_status_recorded"},
        "default_scope_policy": "verified_only",
        "final_postprocess_included": False,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
    index = [{key: row[key] for key in ("frame_index", "frame_id", "revision", "state", "state_sha256")}
             for row in rows]
    (output / "frame_index.json").write_text(json.dumps({"schema": "unified-graph-frame-index/v1", "frames": index},
                                                         indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                               encoding="utf-8")
    # Full immutable snapshots are the delta representation for this offline
    # integration. Seeking a revision is a direct load; no mutable state leaks.
    deltas = [{"base_revision": rows[i - 1]["revision"] if i else 0,
               "new_revision": row["revision"], "kind": "immutable_snapshot_reference",
               "state": row["state"], "state_sha256": row["state_sha256"]}
              for i, row in enumerate(rows)]
    (output / "delta_log.json").write_text(json.dumps({"schema": "unified-graph-snapshot-deltas/v1",
                                                       "deltas": deltas}, indent=2, sort_keys=True) + "\n",
                                            encoding="utf-8")
    return manifest


def replay_unified_snapshots(unified_timeline: Path) -> list[dict[str, Any]]:
    """Load immutable revision snapshots in order and verify their hashes/chain."""
    root = Path(unified_timeline).resolve()
    manifest = _read(root / "manifest.json")
    frame_index = _read(root / "frame_index.json").get("frames", [])
    delta_doc = _read(root / "delta_log.json")
    deltas = delta_doc.get("deltas", [])
    if len(frame_index) != len(deltas) or len(frame_index) != int(manifest.get("revision_count", -1)):
        raise ValueError("unified snapshot/delta/frame counts differ")
    result = []
    previous_revision = 0
    for frame, delta in zip(frame_index, deltas):
        if int(delta["base_revision"]) != previous_revision or int(delta["new_revision"]) != int(frame["revision"]):
            raise ValueError("unified snapshot delta chain is not contiguous")
        path = root / frame["state"]
        if sha256(path) != frame["state_sha256"] or frame["state_sha256"] != delta["state_sha256"]:
            raise ValueError(f"unified revision hash mismatch: {path}")
        state = _read(path)
        if (state.get("frame_index"), state.get("revision"), state.get("frame_id")) != (
                frame["frame_index"], frame["revision"], frame["frame_id"]):
            raise ValueError(f"unified revision index mismatch: {path}")
        result.append(state)
        previous_revision = int(frame["revision"])
    return result
