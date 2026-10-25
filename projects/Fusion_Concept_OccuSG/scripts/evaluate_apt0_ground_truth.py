#!/usr/bin/env python3
"""Run the evaluator-only apt_0 checks against one locked capture/replay.

GT never enters the mapping/perception builder. Missing human GT is represented
as null with a reason; it is never replaced with a zero score.
"""
from __future__ import annotations

import csv
import argparse
import hashlib
import json
import math
import sys
from datetime import datetime
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.metrics import (association_metrics, object_mask_metrics, p3_filter_metrics,
                                room_metrics, semantic_metrics, validate_room_annotations)  # noqa: E402
from src.standalone_sg.contracts.frames import CameraIntrinsics, Transform  # noqa: E402
from src.standalone_sg.geometry.backproject import backproject_depth  # noqa: E402

LOCK_RUN_ID = "apt0_gt_20261003_01"
RUN_ID = "apt0_gt_eval_uninitialized"
CAPTURE_ID = "habitat_replica_cad_apt0_perception_dev_long_20261001"
BASE = ROOT / "artifacts/runs" / CAPTURE_ID
GT = ROOT / "evaluation/gt/apt_0"
LOCK_DIR = ROOT / "evaluation/runs" / LOCK_RUN_ID
OUT = ROOT / "evaluation/runs" / RUN_ID


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def preflight_inputs() -> dict:
    lock = json.loads((LOCK_DIR / "manifest.json").read_text())
    capture_manifest = BASE / "capture/capture_manifest.json"
    d2_path = BASE / "detection_2d/detection_2d_index.json"
    visibility_path = GT / "visibility_index.json"
    snapshot_path = BASE / "replay/snapshot.json"
    delta_path = BASE / "replay/delta_log.json"
    for key, path in (("capture_manifest_sha256", capture_manifest),):
        if sha256(path) != lock.get(key):
            raise ValueError(f"locked input hash mismatch for {path}")
    visibility = json.loads(visibility_path.read_text())
    d2 = json.loads(d2_path.read_text())
    capture = json.loads(capture_manifest.read_text())
    gt_manifest = json.loads((GT / "manifest.json").read_text())
    registry = json.loads((GT / "object_registry.json").read_text())
    geometry_report = json.loads((GT / "object_geometry.json").read_text())
    if gt_manifest.get("object_registry_sha256") != sha256(GT / "object_registry.json"):
        raise ValueError("object registry checksum differs from apt_0 GT manifest")
    if gt_manifest.get("object_geometry_sha256") != sha256(GT / "object_geometry.json"):
        raise ValueError("object geometry checksum differs from apt_0 GT manifest")
    gt_ids = [x.get("gt_instance_id") for x in registry.get("instances", [])]
    sem_ids = [int(x["simulator_instance_semantic_id"]) for x in registry.get("instances", [])]
    if len(gt_ids) != 113 or len(set(gt_ids)) != len(gt_ids) or len(set(sem_ids)) != len(sem_ids):
        raise ValueError("loaded rigid-object GT registry must contain 113 unique GT IDs and semantic instance IDs")
    if len(geometry_report.get("objects", [])) != 113 or any(
            not row.get("observed_mask_depth_aabb_checks") or
            min(x["fraction_inside_aabb_plus_0_05m"] for x in row["observed_mask_depth_aabb_checks"]) < .98
            for row in geometry_report["objects"]):
        raise ValueError("simulator-reference object geometry/visible-depth containment preflight failed")
    if visibility.get("capture_manifest_sha256") != sha256(capture_manifest):
        raise ValueError("visible-instance reference was not generated from this locked capture")
    if d2.get("capture_manifest_sha256") != sha256(capture_manifest):
        raise ValueError("2D detector cache capture hash mismatch")
    if d2.get("room_gt_used") is not False or d2.get("semantic_gt_used") is not False:
        raise ValueError("baseline detector cache declares GT contamination")
    if len(capture.get("frames", [])) != 24 or len(visibility.get("frames", [])) != 24:
        raise ValueError("locked apt_0 evaluation expects exactly the 24-frame capture")
    capture_ids = [str(x["frame_id"]) for x in capture["frames"]]
    if len(set(capture_ids)) != len(capture_ids) or set(capture_ids) != {str(x["frame_id"]) for x in visibility["frames"]}:
        raise ValueError("capture and unique-instance mask frame IDs are not a one-to-one match")
    for entry in capture["frames"]:
        frame_dir = BASE / "capture" / entry["frame_dir"]
        rgb = np.load(frame_dir / "rgb.npy", allow_pickle=False)
        depth = np.load(frame_dir / "depth_m.npy", allow_pickle=False)
        valid = np.load(frame_dir / "valid_mask.npy", allow_pickle=False)
        if rgb.shape not in {(240, 320, 3), (240, 320, 4)} or depth.shape != (240, 320) or valid.shape != depth.shape:
            raise ValueError(f"unexpected capture array dimensions in {entry['frame_id']}")
        if not np.isfinite(depth[valid]).all():
            raise ValueError(f"valid capture depth contains non-finite values in {entry['frame_id']}")
    baseline = lock.get("baseline_replay", {})
    if sha256(snapshot_path) != baseline.get("snapshot_sha256") or sha256(delta_path) != baseline.get("delta_log_sha256"):
        raise ValueError("baseline snapshot/delta hash differs from locked evaluation run")
    replay_manifest = json.loads((BASE / "replay/manifest.json").read_text())
    if replay_manifest.get("snapshot_reload_equal") is not True or replay_manifest.get("delta_replay_equal") is not True:
        raise ValueError("locked baseline replay/reload verification did not pass")
    result = {"status": "pass", "capture_id": CAPTURE_ID, "frame_count": 24,
              "capture_manifest_sha256": sha256(capture_manifest), "detection_cache_capture_hash_match": True,
              "instance_mask_capture_hash_match": True, "detector_gt_inputs_declared_false": True,
              "capture_array_shapes_and_valid_depth_finite": True,
              "snapshot_reload_equal": replay_manifest["snapshot_reload_equal"],
              "delta_replay_equal": replay_manifest["delta_replay_equal"],
              "rigid_instance_registry_unique_count": len(gt_ids), "geometry_visible_depth_containment_pass_count": 113,
              "snapshot_sha256": sha256(snapshot_path), "delta_sha256": sha256(delta_path)}
    write_json(OUT / "preflight.json", result)
    return result


def object_vocabulary_inventory():
    """Inventory eligible visible GT observations without promoting proposals to confirmed vocabulary."""
    registry = json.loads((GT / "object_registry.json").read_text())
    visibility = json.loads((GT / "visibility_index.json").read_text())
    mapping = json.loads((GT / "class_mapping.json").read_text()).get("detector_to_replicacad_category", {})
    gt_class = {x["gt_instance_id"]: str(x.get("replicacad_semantic_class") or "__unmapped__")
                for x in registry["instances"]}
    detector_candidates = {}
    for detector_class, row in mapping.items():
        category = row.get("candidate_replicacad_category")
        if category:
            detector_candidates.setdefault(str(category), []).append({"detector_class": detector_class,
                "mapping_status": row.get("status")})
    class_obs = Counter(); eligible = 0; small_visible = 0
    for frame in visibility["frames"]:
        for instance_id, pixels in frame["visible_pixel_count_by_gt_id"].items():
            if pixels >= 64:
                eligible += 1; class_obs[gt_class.get(instance_id, "__unmapped__")] += 1
            elif pixels > 0:
                small_visible += 1
    by_category = []
    confirmed_obs = candidate_unverified_obs = no_candidate_obs = 0
    for category, count in sorted(class_obs.items()):
        candidates = detector_candidates.get(category, [])
        confirmed = [x["detector_class"] for x in candidates if x["mapping_status"] == "human_verified"]
        if confirmed:
            bucket = "human_confirmed_supported_mapping"; confirmed_obs += count
        elif candidates:
            bucket = "candidate_mapping_unverified"; candidate_unverified_obs += count
        else:
            bucket = "no_detector_class_candidate_currently_mapped"; no_candidate_obs += count
        by_category.append({"replicacad_category": category, "eligible_visible_observations": count,
            "vocabulary_bucket": bucket, "detector_class_candidates": candidates,
            "interpretation": "candidate equivalence remains unverified" if candidates and not confirmed else
                ("no current detector-class candidate; not manually adjudicated unsupported" if not candidates else "confirmed mapping")})
    articulated = json.loads((GT / "class_mapping.json").read_text()).get("articulated_categories_excluded", [])
    return {"status": "inventory_only_supported_vocabulary_metrics_unavailable",
        "capture_id": CAPTURE_ID, "eligible_loaded_rigid_instance_gt_observations": eligible,
        "class_agnostic_coverage_scope": "all loaded rigid instances with visible mask >=64 pixels; not supported-vocabulary YOLO recall",
        "visible_but_below_64px_ignored_gt_observations": small_visible,
        "category_bucket_counts": {"human_confirmed_supported_mapping_observations": confirmed_obs,
            "candidate_mapping_unverified_observations": candidate_unverified_obs,
            "no_detector_class_candidate_observations": no_candidate_obs},
        "supported_vocabulary_metrics": None if confirmed_obs == 0 else "computed separately from human-verified mappings",
        "reason_supported_vocabulary_unavailable": "No detector-to-ReplicaCAD class mapping is human_verified; do not label current all-instance recall as YOLO in-vocabulary recall.",
        "categories": by_category,
        "runtime_unsupported_articulated_declarations": {"count": len(articulated), "categories": articulated,
            "visible_instance_mask_observations": 0, "in_297_denominator": False,
            "reason": "not loaded/rendered by pinned non-Bullet runtime; no visible instance-ID mask exists to score"}}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def geom_feature(room_id: str, ring, properties=None):
    coords = [[float(x), float(y)] for x, y in ring]
    if len(coords) < 3:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return {"type": "Feature", "properties": {"room_id": str(room_id), **(properties or {})},
            "geometry": {"type": "Polygon", "coordinates": [coords]}}


def verified_room_gt():
    data = json.loads((GT / "room_polygons.geojson").read_text())
    metadata = data.get("metadata", {})
    if metadata.get("coordinate_frame") != "canonical_world_z_up_xy_m" or metadata.get("units") != "m":
        return None, "room annotation frame/units are not verified canonical XY metres"
    if metadata.get("status") != "human_verified":
        return None, "room polygons are not human_verified"
    features = data.get("features", [])
    if not features or any(f.get("properties", {}).get("annotation_status") != "human_verified" for f in features):
        return None, "room polygons are empty or contain unverified features"
    rooms = []
    for f in features:
        ring = f.get("geometry", {}).get("coordinates", [[]])[0]
        rooms.append({"id": f["properties"]["room_id"], "polygon": {"coordinates": [ring]}})
    return rooms, None


def predicted_graph_rooms():
    snap = json.loads((BASE / "replay/snapshot.json").read_text())["snapshot"]
    rooms = []; features = []
    for node in snap["nodes"]:
        if node.get("kind") != "room":
            continue
        a = node.get("attributes", {})
        rid = str(node.get("node_id", a.get("room_id", "")))
        ring = a.get("outer_ring", [])
        if len(ring) < 3:
            continue
        rooms.append({"id": rid, "polygon": {"coordinates": [ring]}})
        feat = geom_feature(rid, ring, {"node_id": node.get("node_id"), "stale_flags": snap.get("stale_flags", {}),
                                        "observation_revision": a.get("observation_revision"),
                                        "observation_timestamp_s": a.get("observation_timestamp_s")})
        if feat:
            features.append(feat)
    write_json(OUT / "predicted_rooms.geojson", {"type": "FeatureCollection", "name": "M8_final_room_nodes",
        "metadata": {"coordinate_frame": "canonical_world_z_up_xy_m", "units": "m", "source": "locked baseline snapshot", "gt": False}, "features": features})
    return rooms


def observed_domain_cells():
    """Union of cells known by the exact M3 grids, mapped to global 5cm lattice."""
    index = json.loads((BASE / "grid_trace/index.json").read_text())
    domain = set(); resolution = None
    for frame in index["frames"]:
        meta = json.loads((BASE / "grid_trace" / frame["metadata_file"]).read_text())
        npz = np.load(BASE / "grid_trace" / frame["array_file"], allow_pickle=False)
        values = np.asarray(npz["values"])
        res = float(meta["resolution_m"]); resolution = res
        ox, oy = map(float, meta["origin_xy"])
        ix0 = round(ox / res); iy0 = round(oy / res)
        rows, cols = np.where(values != -1)
        domain.update((ix0 + int(col), iy0 + int(row)) for row, col in zip(rows, cols))
    if resolution != 0.05:
        raise ValueError(f"locked evaluation protocol expects M3 resolution 0.05m, got {resolution}")
    return domain


def export_prediction_overlay():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    index = json.loads((BASE / "grid_trace/index.json").read_text())
    final = index["frames"][-1]
    meta = json.loads((BASE / "grid_trace" / final["metadata_file"]).read_text())
    grid = np.load(BASE / "grid_trace" / final["array_file"], allow_pickle=False)["values"]
    resolution = float(meta["resolution_m"]); ox, oy = map(float, meta["origin_xy"])
    # Semantics preserved from MapConverter: -1 unknown, 0 free, positive source occupation score.
    rgb = np.empty((*grid.shape, 3), dtype=np.uint8)
    rgb[grid == -1] = (155, 155, 155); rgb[grid == 0] = (250, 250, 248)
    positive = grid > 0
    rgb[positive] = (20, 20, 20)
    fig, ax = plt.subplots(figsize=(12, 12), dpi=160)
    ax.imshow(rgb, origin="lower", extent=(ox, ox + grid.shape[1]*resolution, oy, oy + grid.shape[0]*resolution), interpolation="nearest")
    snap = json.loads((BASE / "replay/snapshot.json").read_text())["snapshot"]
    for node in snap["nodes"]:
        if node.get("kind") == "room":
            a = node.get("attributes", {}); ring = a.get("outer_ring", [])
            if ring:
                xy = np.asarray(ring, dtype=float)
                ax.plot(xy[:, 0], xy[:, 1], color="#00a77b", linestyle="--", linewidth=1.4)
                ax.text(float(a.get("centroid_xy", xy.mean(axis=0))[0]), float(a.get("centroid_xy", xy.mean(axis=0))[1]),
                        f"{node.get('node_id')} (pred)", fontsize=7, ha="center", color="#006747",
                        bbox={"facecolor": "white", "alpha": .75, "edgecolor": "none", "pad": 1})
        elif node.get("kind") == "object":
            a = node.get("attributes", {}); c = a.get("center_W")
            if c:
                ax.scatter([c[0]], [c[1]], s=22, facecolors="none", edgecolors="#d14a32", linewidths=1)
        elif node.get("kind") == "agent":
            a = node.get("attributes", {}); p = a.get("pose_W", a.get("position_W"))
            if p and len(p) >= 2:
                ax.scatter([p[0]], [p[1]], s=10, c="#126de5")
    trajectory = []
    with (BASE / "replay/trajectory.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f): trajectory.append((float(row["x_m"]), float(row["y_m"])))
    if trajectory:
        xy = np.asarray(trajectory); ax.plot(xy[:, 0], xy[:, 1], color="#168cff", linewidth=1.2, label="captured GT-pose trajectory")
        ax.scatter([xy[0, 0]], [xy[0, 1]], c="#22a447", s=22, label="start")
        ax.scatter([xy[-1, 0]], [xy[-1, 1]], c="#ec5146", s=22, label="end")
    ax.set_aspect("equal", adjustable="box"); ax.set_xlabel("canonical world X (m)"); ax.set_ylabel("canonical world Y (m)")
    ax.set_title(f"apt_0 baseline prediction only — M3 revision {final['map_revision']} / {meta['simulation_timestamp_s']:.1f}s\nUnknown gray · free white · positive source occupation score black (not probability); rooms dashed; no room GT overlay")
    ax.legend(loc="lower left", fontsize=7); fig.tight_layout()
    (OUT / "overlays").mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / "overlays/baseline_prediction_only.png"); plt.close(fig)


def export_m4_regions():
    features = []
    grid_index = json.loads((BASE / "grid_trace/index.json").read_text())
    for frame in grid_index["frames"]:
        meta = json.loads((BASE / "grid_trace" / frame["metadata_file"]).read_text())
        for region in meta.get("m4", {}).get("raw_regions", []):
            feat = geom_feature(f"{frame['frame_id']}:m4:{region.get('raw_id', len(features))}",
                                region.get("outer", []), {"frame_id": frame["frame_id"], "frame_index": frame["frame_index"],
                                "m3_revision": frame["map_revision"], "m4_branch": meta.get("m4", {}).get("branch"),
                                "centroid_xy": region.get("centroid_xy"), "area_m2": region.get("area_m2"),
                                "adjacent_raw_ids": region.get("adjacent_raw_ids", []), "gt": False})
            if feat:
                features.append(feat)
    write_json(OUT / "m4_raw_regions.geojson", {"type": "FeatureCollection", "name": "M4 raw regions by frame",
        "metadata": {"coordinate_frame": "canonical_world_z_up_xy_m", "units": "m", "source": "exact grid trace metadata", "gt": False},
        "features": features})
    return len(features)


def score_room_layers(gt_rooms):
    validate_room_annotations(gt_rooms, resolution_m=0.05)
    domain = observed_domain_cells()
    doorway = json.loads((GT / "doorways_and_adjacency.json").read_text())
    adj_confirmed = doorway.get("status") == "human_verified"
    gt_adj = [(x["room_a"], x["room_b"]) for x in doorway.get("adjacency", [])
              if x.get("status") == "human_verified"] if adj_confirmed else []
    snapshot = json.loads((BASE / "replay/snapshot.json").read_text())["snapshot"]
    pred_adj = [(e["source_id"], e["target_id"]) for e in snapshot["edges"]
                if e.get("relation") == "room_adjacency"]
    m8 = room_metrics(gt_rooms, predicted_graph_rooms(), observed_domain=domain,
                      adjacency_gt=gt_adj, adjacency_pred=pred_adj)
    if not adj_confirmed:
        m8["adjacency_after_room_matching"] = None
        m8["adjacency_reason"] = "doorway/adjacency annotation is not human_verified"
    index = json.loads((BASE / "grid_trace/index.json").read_text())
    m4_per_frame = []
    for frame in index["frames"]:
        meta = json.loads((BASE / "grid_trace" / frame["metadata_file"]).read_text())
        raw = []
        raw_edges = []
        for region in meta.get("m4", {}).get("raw_regions", []):
            rid = str(region["raw_id"])
            raw.append({"id": rid, "polygon": {"coordinates": [region["outer"]]}})
            raw_edges.extend((rid, str(target)) for target in region.get("adjacent_raw_ids", []))
        scored = room_metrics(gt_rooms, raw, observed_domain=domain, adjacency_pred=raw_edges)
        if not adj_confirmed:
            scored["adjacency_after_room_matching"] = None
            scored["adjacency_reason"] = "doorway/adjacency annotation is not human_verified"
        m4_per_frame.append({"frame_id": frame["frame_id"], "frame_index": frame["frame_index"],
                             "map_revision": frame["map_revision"], "projection_mode": frame["projection_mode"],
                             "raw_region_count": len(raw), "metrics": scored})
    room_gt_doc = json.loads((GT / "room_polygons.geojson").read_text())
    truth_labels = {str(f["properties"]["room_id"]): str(f["properties"]["function_label"])
                    for f in room_gt_doc.get("features", [])
                    if f.get("properties", {}).get("function_label") and
                    f.get("properties", {}).get("function_label_status") == "human_verified"}
    if truth_labels:
        pred_to_gt = {x["predicted_room_id"]: x["gt_room_id"] for x in m8["matches"]}
        semantics = [json.loads(line) for line in (BASE / "semantic/room_semantics.jsonl").read_text().splitlines() if line.strip()]
        sem_pred = [{"room_id": pred_to_gt[r["room_id"]], "label": r.get("label") or "unknown", "status": r.get("status", "unknown")}
                    for r in semantics if r.get("room_id") in pred_to_gt and pred_to_gt[r["room_id"]] in truth_labels]
        sem_result = {"status": "scored", "metrics": semantic_metrics(sem_pred, truth_labels)}
    else:
        sem_result = {"status": "pending_human_function_label", "metrics": None,
                      "reason": "room geometry may be confirmed without confirming functional room labels; set function_label_status=human_verified only when independently supported"}
    return {"status": "scored", "observed_domain_rule": "union of cells with M3 values != -1 over all locked frames; each grid cell mapped by integer index to globally anchored 0.05m lattice",
            "observed_domain_cell_count": len(domain), "m8_room_nodes": m8,
            "m4_raw_regions_by_frame": m4_per_frame,
            "m5_tracked_regions": {"metrics": None, "reason": "tracked M5 polygon snapshots are not retained in locked replay outputs"},
            "room_semantics": sem_result}


def association_and_geometry(lineage):
    by_detection = {x["detection_id"]: x for x in lineage if x["mask_match_at_iou_0_50"]}
    audit = json.loads((BASE / "graph_quality_audit/object_audit.json").read_text())
    registry = json.loads((GT / "object_registry.json").read_text())
    registry_by_id = {x["gt_instance_id"]: x for x in registry["instances"]}
    obs = []; geometry_rows = []; ambiguous_tracks = []; track_gt_map = {}
    for track in audit.get("objects", []):
        track_id = str(track.get("node_id", ""))
        gts = set()
        for did in track.get("associated_detection_ids", []):
            row = by_detection.get(did)
            if row:
                gid = row["gt_instance_id"]
                gts.add(gid); obs.append({"gt_instance_id": gid, "predicted_track_id": track_id})
        if len(gts) != 1:
            if len(gts) > 1:
                ambiguous_tracks.append({"predicted_track_id": track_id, "matched_gt_instances": sorted(gts), "status": "possible_track_merge_review"})
            continue
        gid = next(iter(gts)); gt = registry_by_id[gid]; geom = gt.get("geometry", {})
        track_gt_map[track_id] = gid
        if gt.get("geometry_status") != "simulator_reference_verified_geometry_and_visible_depth_consistent":
            continue
        bounds = geom["aabb_world_canonical_xyz_m"]
        gt_lo, gt_hi = np.asarray(bounds[0], dtype=float), np.asarray(bounds[1], dtype=float)
        gt_center = (gt_lo + gt_hi) / 2
        pred_center = np.asarray(track["center_W"], dtype=float)
        pred_extent = np.asarray(track.get("extents_m", []), dtype=float)
        if pred_extent.shape != (3,):
            pred_extent = None
        row = {"predicted_track_id": track_id, "gt_instance_id": gid,
               "predicted_center_W": pred_center.tolist(), "full_mesh_aabb_center_W": gt_center.tolist(),
               "center_error_to_full_mesh_aabb_center_m": float(np.linalg.norm(pred_center - gt_center)),
               "predicted_extents_m": None if pred_extent is None else pred_extent.tolist(),
               "full_mesh_aabb_extents_m": (gt_hi - gt_lo).tolist(),
               "scope_note": "baseline node geometry uses first accepted visible cluster; difference includes partial-surface bias, not solely coordinate error"}
        geometry_rows.append(row)
    assoc = association_metrics(obs)
    assoc.update({"status": "scored_on_mask-matched_detection_lineage_at_IoU_0.50",
                  "observation_count": len(obs), "unmatched_detection_lineage_excluded": sum(not x["mask_match_at_iou_0_50"] for x in lineage)})
    geometry = {"status": "scored_for_tracks_with_one_unambiguous_mask-matched_simulator_instance_and_verified_mesh_geometry",
                "matched_track_count": len(geometry_rows), "median_aabb_center_error_m": float(np.median([x["center_error_to_full_mesh_aabb_center_m"] for x in geometry_rows])) if geometry_rows else None,
                "per_track": geometry_rows, "ambiguous_track_lineage": ambiguous_tracks}
    return assoc, geometry, track_gt_map


def score_membership(track_gt_map, predicted_to_gt_room):
    path = GT / "object_room_membership.json"
    if not path.is_file():
        return {"status": "pending_annotation", "metrics": None, "reason": "object_room_membership.json is missing"}
    reference = json.loads(path.read_text())
    truth = {str(x["gt_instance_id"]): str(x["gt_room_id"]) for x in reference.get("items", [])
             if x.get("status") in {"human_verified", "simulator_reference_verified"} and x.get("gt_room_id")}
    if not truth:
        return {"status": "pending_annotation", "metrics": None,
                "reason": "no independently reviewed object-room assignments; default template values are uncertain"}
    snapshot = json.loads((BASE / "replay/snapshot.json").read_text())["snapshot"]
    assignments = [{"predicted_object_id": e["target_id"], "predicted_room_id": e["source_id"], "status": "accepted"}
                   for e in snapshot["edges"] if e.get("relation") == "room_contains" and
                   str(e.get("target_id", "")).startswith("object:")]
    result = membership_metrics(assignments, object_match=track_gt_map,
                                room_match=predicted_to_gt_room, gt_membership=truth)
    result.update({"status": "scored_on_human_verified_membership_annotations",
                   "annotation_count": len(truth), "predicted_object_count": sum(n.get("kind") == "object" for n in snapshot["nodes"]),
                   "matched_track_count_with_lineage": len(track_gt_map),
                   "total_unmatched_or_ineligible_memberships": len(assignments) - result["matched_eligible_count"]})
    return result


def build_object_matches():
    d2 = json.loads((BASE / "detection_2d/detection_2d_index.json").read_text())
    visibility = json.loads((GT / "visibility_index.json").read_text())
    registry = json.loads((GT / "object_registry.json").read_text())
    class_map = json.loads((GT / "class_mapping.json").read_text()).get("detector_to_replicacad_category", {})
    verified_mapping = {k: v.get("candidate_replicacad_category") for k, v in class_map.items()
                        if v.get("status") == "human_verified" and v.get("candidate_replicacad_category")}
    mapped_categories = set(verified_mapping.values())
    gt_by_id = {int(x["simulator_instance_semantic_id"]): x for x in registry["instances"]
                if x.get("simulator_instance_semantic_id") is not None}
    vis_by_frame = {x["frame_id"]: x for x in visibility["frames"]}
    predictions = []; truths = []; pred_arrays = {}; truth_arrays = {}; frame_for_det = {}; lineage = []
    class_predictions_by_frame = {}; class_truth_by_frame = {}
    raw_match = {}
    for frame in d2.get("frames", []):
        fid = frame["frame_id"]
        vis = vis_by_frame[fid]
        labels = np.load(GT / "visible_instance_masks" / vis["mask_file"], allow_pickle=False)
        for sid, obj in gt_by_id.items():
            mask = labels == sid
            if int(mask.sum()) == 0:
                continue
            key = f"{fid}|{obj['gt_instance_id']}"
            rec = {"instance_id": key, "class_name": "__class_agnostic__", "mask": mask,
                   "status": "eligible" if int(mask.sum()) >= 64 else "below_min_visible_pixels"}
            truths.append(rec); truth_arrays[key] = mask
            class_truth_by_frame.setdefault(fid, []).append({"instance_id": key,
                "class_name": str(obj.get("replicacad_semantic_class") or "__unmapped__"), "mask": mask,
                "status": "eligible" if int(mask.sum()) >= 64 else "below_min_visible_pixels"})
        for det in frame.get("detections", []):
            mask = np.asarray(np.load(BASE / "detection_2d" / det["mask_file"], allow_pickle=False), dtype=bool)
            did = str(det["detection_id"]); key = did
            predictions.append({"detection_id": did, "class_name": "__class_agnostic__", "mask": mask})
            class_predictions_by_frame.setdefault(fid, []).append({"detection_id": did,
                "model_class": str(det.get("class_canonical", "")), "class_name": str(det.get("class_canonical", "")), "mask": mask})
            pred_arrays[key] = mask; frame_for_det[key] = fid
    # Instances in different frames are distinct evaluation items. Match each
    # image independently, then aggregate counts; never solve one huge cross-
    # frame assignment which could associate a detection to another frame.
    frame_ids = sorted(set(frame_for_det.values()) | {x["instance_id"].split("|", 1)[0] for x in truths})
    agg = {"tp": 0, "fp": 0, "fn": 0, "ignored_count": 0, "ignored_prediction_count": 0,
           "ignored_gt": [], "ignored_predictions": [], "matched": [],
           "unmatched_prediction_ids": [], "missed_eligible_gt_ids": []}
    for fid in frame_ids:
        frame_preds = [x for x in predictions if frame_for_det[x["detection_id"]] == fid]
        frame_truth = [x for x in truths if x["instance_id"].startswith(fid + "|")]
        result = object_mask_metrics(frame_preds, frame_truth, minimum_pixels=64, match_iou_threshold=0.50)
        cls = result["per_class"].get("__class_agnostic__", {})
        for key in ("tp", "fp", "fn", "ignored_count", "ignored_prediction_count"):
            agg[key] += int(result.get(key, 0))
        agg["ignored_gt"].extend(result.get("ignored_gt", []))
        agg["matched"].extend(cls.get("matched", []))
        matched_pred = {x["detection_id"] for x in cls.get("matched", [])}
        ignored_pred = {x["detection_id"] for x in cls.get("ignored_predictions", [])}
        agg["ignored_predictions"].extend(cls.get("ignored_predictions", []))
        matched_gt = {x["instance_id"] for x in cls.get("matched", [])}
        agg["unmatched_prediction_ids"].extend(x["detection_id"] for x in frame_preds
            if x["detection_id"] not in matched_pred and x["detection_id"] not in ignored_pred)
        agg["missed_eligible_gt_ids"].extend(x["instance_id"] for x in frame_truth
            if int(x["mask"].sum()) >= 64 and x["instance_id"] not in matched_gt and x.get("status") not in {"uncertain", "ignored", "unsupported"})
    total_pred = agg["tp"] + agg["fp"]
    total_gt = agg["tp"] + agg["fn"]
    class_agnostic = {"minimum_visible_pixels": 64, "match_iou_threshold": 0.50,
        "matching_scope": "independent per frame; counts then aggregated", "tp": agg["tp"], "fp": agg["fp"], "fn": agg["fn"],
        "precision": agg["tp"] / total_pred if total_pred else None,
        "recall": agg["tp"] / total_gt if total_gt else None,
        "ignored_count": agg["ignored_count"], "ignored_gt": agg["ignored_gt"],
        "ignored_prediction_count": agg["ignored_prediction_count"], "ignored_predictions": agg["ignored_predictions"],
        "ignored_prediction_overlap_policy": "same-class intersection-over-prediction >= 0.50 after eligible GT assignment",
        "matched": agg["matched"],
        "unmatched_prediction_ids": agg["unmatched_prediction_ids"],
        "missed_eligible_gt_ids": agg["missed_eligible_gt_ids"]}
    class_aware = None
    if verified_mapping:
        totals = Counter(); by_class = {}; excluded_prediction_count = 0; excluded_gt_count = 0
        for fid in frame_ids:
            preds = []
            for p in class_predictions_by_frame.get(fid, []):
                category = verified_mapping.get(p["model_class"])
                if category is None:
                    excluded_prediction_count += 1; continue
                preds.append({"detection_id": p["detection_id"], "class_name": category, "mask": p["mask"]})
            gts = []
            for g in class_truth_by_frame.get(fid, []):
                if g["class_name"] not in mapped_categories:
                    excluded_gt_count += 1; continue
                gts.append(g)
            scored = object_mask_metrics(preds, gts, minimum_pixels=64, match_iou_threshold=0.50)
            for key in ("tp", "fp", "fn", "ignored_count", "ignored_prediction_count"):
                totals[key] += int(scored.get(key, 0))
            for cls, values in scored["per_class"].items():
                dst = by_class.setdefault(cls, Counter())
                for key in ("predictions", "eligible_gt", "tp", "fp", "fn"):
                    dst[key] += int(values.get(key, 0))
        denom_p = totals["tp"] + totals["fp"]; denom_g = totals["tp"] + totals["fn"]
        class_aware = {"status": "scored_only_for_human_verified_class_mappings", "verified_detector_to_category": verified_mapping,
            "tp": totals["tp"], "fp": totals["fp"], "fn": totals["fn"], "ignored_count": totals["ignored_count"],
            "ignored_prediction_count": totals["ignored_prediction_count"],
            "precision": totals["tp"] / denom_p if denom_p else None, "recall": totals["tp"] / denom_g if denom_g else None,
            "excluded_unmapped_predictions": excluded_prediction_count, "excluded_unmapped_gt_observations": excluded_gt_count,
            "per_class_counts": {k: dict(v) for k, v in by_class.items()}, "matching_scope": "one-to-one per frame"}
    for match in agg["matched"]:
        raw_match[match["detection_id"]] = match["instance_id"]
    p3_by_id = {}
    ledger_path = BASE / "detection_3d/object_evidence_ledger.jsonl"
    for line in ledger_path.read_text().splitlines():
        if line.strip():
            row = json.loads(line); p3_by_id[row["detection_id"]] = row
    d3_by_id = {}
    for line in (BASE / "detection_3d/detection_3d.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for det in row.get("detections", []):
            d3_by_id[det["detection_id"]] = det
    # Camera-space to canonical-world geometry uses exactly the recorded M3 sensor pose and M1 routine.
    grid_meta_by_frame = {}
    for f in json.loads((BASE / "grid_trace/index.json").read_text())["frames"]:
        grid_meta_by_frame[f["frame_id"]] = json.loads((BASE / "grid_trace" / f["metadata_file"]).read_text())
    capture_frames = {x["frame_id"]: x for x in json.loads((BASE / "capture/capture_manifest.json").read_text())["frames"]}
    out_rows = []
    for did, pred_mask in pred_arrays.items():
        fid = frame_for_det[did]; match_key = raw_match.get(did)
        iou_best = 0.0; gt_id = None
        for gkey, gmask in truth_arrays.items():
            if not gkey.startswith(fid + "|"):
                continue
            union = int(np.count_nonzero(pred_mask | gmask))
            score = int(np.count_nonzero(pred_mask & gmask)) / union if union else 0.0
            if score > iou_best:
                iou_best, gt_id = score, gkey.split("|", 1)[1]
        ledger = p3_by_id.get(did, {})
        d3 = d3_by_id.get(did)
        row = {"frame_id": fid, "detection_id": did, "gt_instance_id": gt_id,
               "best_visible_instance_mask_iou": iou_best, "mask_match_at_iou_0_50": match_key is not None,
               "p3_decision": ledger.get("decision", "missing"), "p3_reason": ledger.get("reason"),
               "mask_pixels": ledger.get("mask_pixel_count", int(pred_mask.sum())),
               "valid_depth_pixels": ledger.get("valid_depth_pixels"), "backprojected_points": ledger.get("backprojected_points"),
               "cluster_count": ledger.get("cluster_count"), "largest_cluster_points": ledger.get("largest_cluster_points"),
               "independent_reference_status": "simulator_instance_mask_and_captured_depth" if match_key else "no_visible_instance_mask_match_at_threshold"}
        if match_key and d3:
            vis = vis_by_frame[fid]; gt_obj_id = match_key.split("|", 1)[1]
            gt_obj = next(o for o in registry["instances"] if o["gt_instance_id"] == gt_obj_id)
            labels = np.load(GT / "visible_instance_masks" / vis["mask_file"], allow_pickle=False)
            sid = int(gt_obj["simulator_instance_semantic_id"])
            cap_index = int(vis["frame_index"])
            depth = np.load(BASE / "capture/frames" / f"{cap_index:06d}" / "depth_m.npy", allow_pickle=False)
            valid = np.load(BASE / "capture/frames" / f"{cap_index:06d}" / "valid_mask.npy", allow_pickle=False)
            md = grid_meta_by_frame[fid]
            intrd = md["intrinsics"]
            intr = CameraIntrinsics(width=int(intrd["width"]), height=int(intrd["height"]), fx=float(intrd["fx"]), fy=float(intrd["fy"]),
                                    cx=float(intrd["cx"]), cy=float(intrd["cy"]), calibration_id=str(intrd["calibration_id"]))
            transform = Transform(np.asarray(md["sensor_pose_W_Copt"], dtype=float), "canonical_world", "camera_optical", float(md["simulation_timestamp_s"]))
            ref = backproject_depth(depth, intr, transform, meters_per_unit=1.0, stride=1, min_depth_m=0.1, max_depth_m=10.0,
                                    valid_mask=(labels == sid) & valid, timestamp=float(md["simulation_timestamp_s"]), scan_id=fid)
            if len(ref.xyz_W):
                lo, hi = np.min(ref.xyz_W, axis=0), np.max(ref.xyz_W, axis=0)
                expected_center = (lo + hi) / 2
                predicted_center = np.asarray(d3["center_W"], dtype=float)
                row["visible_depth_aabb_center_W"] = expected_center.tolist()
                row["predicted_detection3d_center_W"] = predicted_center.tolist()
                row["center_error_to_visible_depth_aabb_m"] = float(np.linalg.norm(predicted_center - expected_center))
                row["reference_visible_depth_points"] = int(len(ref.xyz_W))
                row["geometry_metric_note"] = "visible-mask depth AABB center; not full instance centroid"
            else:
                row["geometry_metric_note"] = "no valid GT-mask depth; geometry score unavailable"
        out_rows.append(row); lineage.append(row)
    return class_agnostic, class_aware, out_rows, lineage, p3_by_id


def render_object_review(rows):
    # Human-review aid: RGB with predicted contour and exact simulator GT instance contour.
    from PIL import ImageFont
    d2 = json.loads((BASE / "detection_2d/detection_2d_index.json").read_text())
    frames = {f["frame_id"]: f for f in d2["frames"]}
    vis = {f["frame_id"]: f for f in json.loads((GT / "visibility_index.json").read_text())["frames"]}
    reg = json.loads((GT / "object_registry.json").read_text())
    reg_by_gt = {o["gt_instance_id"]: o for o in reg["instances"]}
    tiles = []; index_html = ["<!doctype html><meta charset='utf-8'><title>apt_0 detection mask review</title>",
        "<style>body{font:14px sans-serif}article{display:inline-block;vertical-align:top;width:660px;margin:8px;border:1px solid #bbb;padding:6px}img{width:640px}small{color:#444}</style>",
        "<h1>apt_0 detection review — red fill=model mask, cyan=model contour, yellow=simulator instance-mask contour</h1>",
        "<p>AI/evaluator-assisted triage only; mask match is class-agnostic and is not human-adjudicated detector truth.</p>"]
    detail_dir = OUT / "overlays/detections"
    detail_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        fid = row["frame_id"]; det = next(x for x in frames[fid]["detections"] if x["detection_id"] == row["detection_id"])
        idx = int(vis[fid]["frame_index"])
        rgb = np.load(BASE / "capture/frames" / f"{idx:06d}" / "rgb.npy", allow_pickle=False)
        rgb = np.asarray(rgb[..., :3], dtype=np.uint8)
        pred = np.asarray(np.load(BASE / "detection_2d" / det["mask_file"], allow_pickle=False), dtype=bool)
        if row["gt_instance_id"]:
            obj = reg_by_gt[row["gt_instance_id"]]
            ids = np.load(GT / "visible_instance_masks" / vis[fid]["mask_file"], allow_pickle=False)
            gtmask = ids == int(obj["simulator_instance_semantic_id"])
        else:
            gtmask = np.zeros(pred.shape, dtype=bool)
        overlay = rgb.copy()
        overlay[pred] = (0.55 * overlay[pred] + 0.45 * np.array([255, 40, 40])).astype(np.uint8)
        # GT-only outline in yellow; prediction remains red fill.
        im = Image.fromarray(overlay); dr = ImageDraw.Draw(im)
        for mask, color in ((gtmask, (255, 230, 0)), (pred, (0, 255, 255))):
            edge = mask ^ np.roll(mask, 1, axis=0) ^ np.roll(mask, 1, axis=1)
            yy, xx = np.where(edge)
            for x, y in zip(xx.tolist(), yy.tolist()):
                dr.point((x, y), fill=color)
        panel = Image.new("RGB", (640, 310), "white"); panel.paste(im.resize((640, 480)), (0, 0))
        # Fit 320x240 at 2x => 640x480, extend panel for audit caption.
        panel = Image.new("RGB", (640, 530), "white"); panel.paste(im.resize((640, 480)), (0, 0))
        font = ImageFont.load_default()
        caption = f"{fid} {row['detection_id']} | pred red / contour cyan; simulator GT contour yellow\n"
        caption += f"class={det.get('class_canonical')} P3={row['p3_decision']} {row.get('p3_reason')} | best mask IoU={row['best_visible_instance_mask_iou']:.3f}"
        panel_draw = ImageDraw.Draw(panel); panel_draw.multiline_text((6, 485), caption, fill="black", font=font)
        safe_name = row["detection_id"].replace(":", "__")
        panel.save(detail_dir / f"{safe_name}.png")
        import html as html_escape
        index_html.append(f"<article><h3>{html_escape.escape(row['detection_id'])} — {html_escape.escape(str(det.get('class_canonical')))}</h3>"
                          f"<small>P3={html_escape.escape(str(row['p3_decision']))} / {html_escape.escape(str(row.get('p3_reason')))}; "
                          f"best mask IoU={row['best_visible_instance_mask_iou']:.3f}; GT instance={html_escape.escape(str(row.get('gt_instance_id')))}</small><br>"
                          f"<a href='detections/{safe_name}.png'><img src='detections/{safe_name}.png'></a></article>")
        tiles.append(panel)
    cols = 2; rows_n = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * 640, rows_n * 530), "#ddd")
    for i, tile in enumerate(tiles): sheet.paste(tile, ((i % cols) * 640, (i // cols) * 530))
    (OUT / "overlays").mkdir(parents=True, exist_ok=True)
    sheet.save(OUT / "overlays/object_mask_review_contact_sheet.png")
    (OUT / "overlays/object_mask_review.html").write_text("\n".join(index_html), encoding="utf-8")


def main():
    global RUN_ID, OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="unique output run ID; defaults to a timestamped ID")
    args = parser.parse_args()
    RUN_ID = args.run_id or datetime.now().astimezone().strftime("apt0_gt_eval_%Y%m%d_%H%M%S")
    if not RUN_ID.startswith("apt0_gt_eval_") or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in RUN_ID):
        parser.error("--run-id must start with 'apt0_gt_eval_' and contain only letters, digits, '_' or '-'")
    OUT = ROOT / "evaluation/runs" / RUN_ID
    if OUT.exists():
        parser.error(f"refusing to overwrite existing evaluation run: {OUT}")
    OUT.mkdir(parents=True, exist_ok=True)
    preflight = preflight_inputs()
    predicted_rooms = predicted_graph_rooms()
    export_prediction_overlay()
    m4_count = export_m4_regions()
    object_metrics, class_aware_metrics, p3_rows, lineage, p3 = build_object_matches()
    vocabulary_inventory = object_vocabulary_inventory()
    write_json(OUT / "object_scope_inventory.json", vocabulary_inventory)
    render_object_review(p3_rows)
    association, persistent_geometry, track_gt_map = association_and_geometry(lineage)
    p3_counts = Counter(x.get("decision", "missing") for x in p3.values())
    p3_true = Counter("matched" if x["mask_match_at_iou_0_50"] else "unmatched" for x in p3_rows)
    matched_ids = {x["detection_id"] for x in p3_rows if x["mask_match_at_iou_0_50"]}
    p3_cross_tab = p3_filter_metrics([{"detection_id": k, "decision": v.get("decision", "unknown")} for k, v in p3.items()],
                                     mask_matched_detection_ids=matched_ids)
    p3_decision = {"ledger_detection_count": len(p3), "decisions": dict(p3_counts),
                   "independent_visible_instance_mask_match": dict(p3_true),
                   "mask_lineage_by_filter_decision": p3_cross_tab,
                   "matched_rejection_reason_counts": dict(Counter(x.get("p3_reason") for x in p3_rows
                       if x["p3_decision"] == "rejected" and x["mask_match_at_iou_0_50"])),
                   "accepted_and_matched": sum(x["p3_decision"] == "accepted" and x["mask_match_at_iou_0_50"] for x in p3_rows),
                   "rejected_but_matched_at_2d_mask_threshold": sum(x["p3_decision"] == "rejected" and x["mask_match_at_iou_0_50"] for x in p3_rows),
                   "rejected_and_unmatched_at_threshold": sum(x["p3_decision"] == "rejected" and not x["mask_match_at_iou_0_50"] for x in p3_rows),
                   "interpretation": "mask-based 2D instance association is independent of P3 decisions; unmatched does not by itself prove a false detection, and matched rejected masks indicate an eligible visible object observation was filtered before 3D output"}
    write_json(OUT / "object_matching.json", {"status": "class_agnostic_simulator_visible_instance_mask_scored",
        "class_agnostic_visible_mask_metrics": object_metrics, "class_aware_metrics": class_aware_metrics,
        "class_aware_reason": None if class_aware_metrics is not None else "detector-to-ReplicaCAD class mapping remains AI-assisted provisional"})
    (OUT / "detection_lineage.jsonl").write_text("".join(json.dumps(x, sort_keys=True, allow_nan=False) + "\n" for x in lineage), encoding="utf-8")
    write_json(OUT / "p3_evaluation.json", p3_decision | {"per_detection": p3_rows})
    room_gt, reason = verified_room_gt()
    room_pred_geo = predicted_rooms
    if room_gt is None:
        room_result = {"status": "pending_gt", "reason": reason, "metrics": None,
                       "layers": {"m4_raw_regions": {"geometry_count_exported": m4_count, "metrics": None, "reason": reason},
                                  "m5_tracked_regions": {"metrics": None, "reason": "tracked region footprint history is not retained in the locked replay outputs"},
                                  "m8_room_nodes": {"predicted_count": len(room_pred_geo), "metrics": None, "reason": reason}}}
    else:
        room_result = score_room_layers(room_gt)
    write_json(OUT / "room_matching.json", room_result)
    # Membership and semantics are scored only for explicitly human-verified
    # annotations. Provisional/uncertain template rows never enter the metrics.
    membership_reference = json.loads((GT / "object_room_membership.json").read_text()) if (GT / "object_room_membership.json").is_file() else {}
    verified_room_matches = {x["predicted_room_id"]: x["gt_room_id"]
                             for x in room_result.get("m8_room_nodes", {}).get("matches", [])}
    membership = score_membership(track_gt_map, verified_room_matches)
    semantics = room_result.get("room_semantics", {"status": "pending_gt", "metrics": None,
        "reason": "requires independently confirmed functional room labels; predicted labels cannot serve as GT"})
    if room_gt is None:
        membership = {"status": "pending_gt", "metrics": None,
                      "reason": "requires human-verified room polygons and object-room assignments under the documented doorway/open-plan policy"}
        semantics = {"status": "pending_gt", "metrics": None,
                     "reason": "requires independently confirmed functional room labels; predicted labels cannot serve as GT"}
    write_json(OUT / "metrics.json", {"schema_version": 1, "kind": "apt0_ground_truth_evaluation", "status": "partial_gt_evaluator_ready_room_annotation_pending",
        "preflight": preflight,
        "capture_id": CAPTURE_ID, "room": room_result, "object_detection": {"class_agnostic_visible_mask": object_metrics,
        "scope_inventory": vocabulary_inventory,
        "evaluation_unit": "one eligible visible object instance in one captured frame; not unique-object recall over the scene",
        "class_aware": class_aware_metrics, "class_aware_reason": None if class_aware_metrics is not None else "provisional category mapping"}, "p3_filtering": p3_decision,
        "observation_3d": {"status": "scored_for_independently_mask_matched_accepted_observations",
            "matched_accepted_count_with_center_error": sum(x.get("center_error_to_visible_depth_aabb_m") is not None and x["p3_decision"] == "accepted" for x in p3_rows),
            "center_error_median": float(np.median([x["center_error_to_visible_depth_aabb_m"] for x in p3_rows if x.get("center_error_to_visible_depth_aabb_m") is not None and x["p3_decision"] == "accepted"])) if any(x.get("center_error_to_visible_depth_aabb_m") is not None and x["p3_decision"] == "accepted" for x in p3_rows) else None,
            "center_error_p95": float(np.percentile([x["center_error_to_visible_depth_aabb_m"] for x in p3_rows if x.get("center_error_to_visible_depth_aabb_m") is not None and x["p3_decision"] == "accepted"], 95)) if any(x.get("center_error_to_visible_depth_aabb_m") is not None and x["p3_decision"] == "accepted" for x in p3_rows) else None,
            "center_error_max": max([x["center_error_to_visible_depth_aabb_m"] for x in p3_rows if x.get("center_error_to_visible_depth_aabb_m") is not None and x["p3_decision"] == "accepted"], default=None),
            "reference": "GT instance visible mask + captured valid depth + recorded M1 pose/intrinsics; reference is observed-visible depth AABB, not full object centroid"},
        "persistent_object_geometry": persistent_geometry, "association": association,
        "membership": membership, "room_semantics": semantics,
        "limitations": ["No human-confirmed architectural room polygons yet; room accuracy is not reported.",
                        "Class-agnostic mask score is not detector class accuracy.",
                        "P3 unmatched masks are not automatically false detections.",
                        "Capture uses GT pose; SLAM is not evaluated."]})
    # CSV contains only populated, correctly scoped scalars; pending metrics use blank value + status/reason.
    with (OUT / "metrics.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["metric", "value", "status", "scope", "reason"]); w.writeheader()
        for key in ("precision", "recall", "tp", "fp", "fn", "ignored_count"):
            w.writerow({"metric": f"object_mask_class_agnostic.{key}", "value": object_metrics.get(key), "status": "scored", "scope": "24-frame captured visible instances; class agnostic", "reason": ""})
        w.writerow({"metric": "object_mask.ignored_prediction_count", "value": object_metrics.get("ignored_prediction_count", 0),
                    "status": "scored", "scope": "unmatched predictions overlapping ignored same-class GT masks at protocol IoP", "reason": ""})
        w.writerow({"metric": "object_mask.supported_vocabulary_recall", "value": None,
                    "status": "pending_human_class_mapping", "scope": "GT categories mapped to detector vocabulary by reviewed mapping only",
                    "reason": vocabulary_inventory["reason_supported_vocabulary_unavailable"]})
        w.writerow({"metric": "room_m8.union_iou", "value": None, "status": "pending_gt", "scope": "full architectural room footprints", "reason": reason or ""})
        w.writerow({"metric": "membership.conditional_accuracy", "value": membership.get("metrics", {}).get("conditional_accuracy") if membership.get("metrics") else None, "status": membership["status"], "scope": "matched eligible object-room pairs", "reason": membership.get("reason", "")})
        w.writerow({"metric": "room_semantics.accuracy", "value": semantics.get("metrics", {}).get("accuracy") if semantics.get("metrics") else None, "status": semantics["status"], "scope": "confirmed functional room labels", "reason": semantics.get("reason", "")})
    failure_cases = {"kind": "evaluator_review_queue", "status": "provisional_machine_triage_not_human_adjudication",
                     "missed_eligible_visible_instance_observations": object_metrics["missed_eligible_gt_ids"],
                     "unmatched_prediction_detection_ids": object_metrics["unmatched_prediction_ids"],
                     "p3_rejected_with_independent_2d_mask_match": [x for x in p3_rows if x["p3_decision"] == "rejected" and x["mask_match_at_iou_0_50"]],
                     "accepted_with_center_error_over_0_50m": {"triage_only_threshold_m": 0.50,
                         "not_a_protocol_accuracy_cutoff": True,
                         "items": sorted([x for x in p3_rows if x["p3_decision"] == "accepted" and
                             x.get("center_error_to_visible_depth_aabb_m") is not None and
                             x["center_error_to_visible_depth_aabb_m"] > 0.50],
                             key=lambda x: x["center_error_to_visible_depth_aabb_m"], reverse=True)},
                     "detection_masks_unmatched_to_visible_instances": [x for x in p3_rows if not x["mask_match_at_iou_0_50"]],
                     "room_failures": None, "reason_room_failures_null": reason or "",
                     "object_scope_inventory": vocabulary_inventory}
    write_json(OUT / "failure_cases.json", failure_cases)
    status = "partial_evaluation_room_gt_pending"
    eligible_obs = object_metrics["tp"] + object_metrics["fn"]
    matched_iou = [x["mask_iou"] for x in object_metrics["matched"]]
    p3_cross = p3_decision["mask_lineage_by_filter_decision"]["counts"]
    three_d_errors = [x["center_error_to_visible_depth_aabb_m"] for x in p3_rows
                      if x.get("center_error_to_visible_depth_aabb_m") is not None and x["p3_decision"] == "accepted"]
    report = f'''# apt_0 GT evaluation — {RUN_ID}\n\nStatus: **{status}**. This is a locked-input evaluation run, not a claim that the full apt_0 benchmark is complete.\n\n## Scored from available simulator-reference GT\n\n- Capture: `{CAPTURE_ID}`; 24 RGB-D frames, existing detector cache and baseline replay. Inputs remain unchanged; GT was not passed into the builder.\n- Visible-instance masks: RGB, metric depth, and valid-mask equivalence passed for all 24 captured views. Registry contains 113 loaded rigid instances; six articulated declarations unsupported by the pinned non-Bullet runtime are excluded, not detector false negatives.\n- Class-agnostic instance-mask detection, per-frame one-to-one matching at IoU ≥ 0.50 and visibility ≥64 px: **TP={object_metrics['tp']}, FP={object_metrics['fp']}, FN={object_metrics['fn']}, ignored GT observations={object_metrics['ignored_count']}**; precision={object_metrics['precision']:.3f}, recall={object_metrics['recall']:.3f}; eligible denominator={eligible_obs}. This is observation-level visible-instance recall across frames, not unique-scene-object recall, and it does **not** score class correctness. Mean matched-mask IoU={float(np.mean(matched_iou)):.3f} over {len(matched_iou)} matches.\n- P3 ledger: {p3_counts.get('accepted', 0)} accepted / {p3_counts.get('rejected', 0)} rejected from 40 model detections. Independent class-agnostic mask lineage yielded {p3_cross['mask_matched_kept']} matched-and-kept and {p3_cross['mask_matched_rejected']} matched-but-rejected observations; {p3_cross['unmatched_kept']} kept / {p3_cross['unmatched_rejected']} rejected had no eligible mask match at the protocol threshold. The 13 matched rejects all hit `rejected_cluster_below_minimum`; valid depth existed, but the largest cluster did not meet the baseline minimum. This means the filter followed its rule; whether every such observation should be lifted is a separate quality question. Unmatched is not automatically a semantic false positive.\n- For {len(three_d_errors)} accepted detections matched to independent visible-instance masks with valid depth, the median centre error to the GT-visible-depth AABB centre is {float(np.median(three_d_errors)):.3f} m (p95={float(np.percentile(three_d_errors, 95)):.3f} m). This is a visible partial-surface reference, not a full object centroid.\n- Association on accepted mask-matched lineages: {association['gt_instance_count_observed']} GT instances observed, {association['fragmented_count']} fragmented predicted tracks, {association['merged_track_count']} predicted tracks with evidence from multiple GT instances. This is conditional on the 17 accepted-and-matched observations; see `metrics.json`.\n- For {persistent_geometry['matched_track_count']} unambiguous matched tracks, median distance from predicted first-observation centre to full-mesh AABB centre is {persistent_geometry['median_aabb_center_error_m']:.3f} m. This includes expected partial-surface/first-view bias and is not an isolated coordinate-transform error.\n\n## Metrics deliberately null\n\nArchitectural room segmentation, M5 tracked-region geometry, object-room membership, room adjacency and room-semantic accuracy remain null because independent room polygons, doorway/open-plan decisions, object-room assignments and functional labels are not human-confirmed. Five final M8 RoomNodes are predictions, not five verified rooms. M4 raw polygon traces, M8 footprints, and the prediction-only occupancy/trajectory overlay are exported separately. The stage-only slice is an annotation aid, not room GT.\n\n## Review and reproduction\n\nOpen `evaluation/gt/apt_0/room_annotation_tool_v1/apt0_room_annotation.html` in a browser. Review the inside-wall, doorway, corridor and open-plan conventions in `evaluation/protocol.md`; keep uncertain boundaries uncertain. Use `overlays/object_mask_review.html` for RGB/model-mask/simulator-mask review. Do not use predicted overlays as the source of room polygons.\n\nRun `MPLCONFIGDIR=/tmp/apt0-gt-mpl PYTHONPATH=.:src python3 scripts/evaluate_apt0_ground_truth.py` to reproduce these checks against the locked capture. The capture used Habitat-Sim GT poses, so SLAM is not evaluated.\n'''
    scope_counts = vocabulary_inventory["category_bucket_counts"]
    scope_section = ("\n## Vocabulary scope and ignored predictions\n\n"
        f"Among the 297 eligible visible observations from loaded rigid instances: {scope_counts['human_confirmed_supported_mapping_observations']} have a human-confirmed detector mapping, {scope_counts['candidate_mapping_unverified_observations']} have provisional candidate mappings, and {scope_counts['no_detector_class_candidate_observations']} have no current detector-class candidate. The latter is not a manually confirmed unsupported label. Consequently supported-vocabulary metrics are withheld; the class-agnostic score above is not YOLO in-vocabulary recall.\n\n"
        f"The 320 visible masks below 64 px are ignored GT. Unmatched predictions with at least 50% intersection-over-prediction against one same-class ignored mask are counted as ignored predictions rather than FP; eligible GT matches take priority. This run marks {object_metrics.get('ignored_prediction_count', 0)} predictions ignored by that rule.\n\n")
    report = report.replace("\n## Metrics deliberately null\n", scope_section + "## Metrics deliberately null\n")
    (OUT / "report.vi.md").write_text(report, encoding="utf-8")
    (OUT / "overlays").mkdir(exist_ok=True)
    # Lock evaluator code/protocol and output inventory after all generated scores.
    manifest = {"schema_version": 1, "evaluation_run_id": RUN_ID, "status": status,
        "capture_id": CAPTURE_ID, "input_lock_run_id": LOCK_RUN_ID,
        "input_lock_manifest_sha256": sha256(LOCK_DIR / "manifest.json"),
        "protocol_sha256": sha256(ROOT / "evaluation/protocol.md"), "metrics_code_sha256": sha256(ROOT / "evaluation/metrics.py"),
        "runner_sha256": sha256(Path(__file__)), "baseline_snapshot_sha256": sha256(BASE / "replay/snapshot.json"),
        "baseline_delta_sha256": sha256(BASE / "replay/delta_log.json"), "gt_manifest_sha256": sha256(GT / "manifest.json"),
        "object_registry_sha256": sha256(GT / "object_registry.json"), "class_mapping_status": json.loads((GT / "class_mapping.json").read_text()).get("status"),
        "ignored_prediction_overlap_policy": object_metrics.get("ignored_prediction_policy"),
        "ignored_prediction_overlap_threshold_iop": object_metrics.get("ignored_prediction_overlap_threshold_iop"),
        "object_room_membership_sha256": sha256(GT / "object_room_membership.json") if (GT / "object_room_membership.json").is_file() else None,
        "gt_visible_instance_mask_sha256": {str(p.relative_to(GT)): sha256(p) for p in sorted((GT / "visible_instance_masks").glob("*.npy"))},
        "room_annotation_status": json.loads((GT / "room_polygons.geojson").read_text()).get("metadata", {}).get("status"),
        "builder_input_contains_gt": False, "baseline_algorithm_or_config_changed": False,
        "outputs": {str(p.relative_to(OUT)): sha256(p) for p in sorted(OUT.rglob("*")) if p.is_file() and p.name != "evaluator_manifest.json"}}
    write_json(OUT / "evaluator_manifest.json", manifest)
    print(json.dumps({"status": status, "object_mask": {k: object_metrics.get(k) for k in ("tp", "fp", "fn", "ignored_count", "precision", "recall")},
                      "p3": {"decisions": p3_decision["decisions"],
                            "independent_lineage_counts": p3_decision["mask_lineage_by_filter_decision"]["counts"],
                            "matched_rejection_reason_counts": p3_decision["matched_rejection_reason_counts"]},
                      "association": {k: v for k, v in association.items() if k not in {"fragmented_gt_instances", "merged_predicted_tracks"}},
                      "persistent_geometry_median_center_error_m": persistent_geometry["median_aabb_center_error_m"],
                      "room_metrics": room_result["metrics"] if "metrics" in room_result else None,
                      "outputs": str(OUT)}, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
