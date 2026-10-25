#!/usr/bin/env python3
"""Build a blinded, input-sampled Q1 review package and report from locked outputs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def safe_slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


def panel_name(observation_id: str, panel: str) -> str:
    # Full observation ID is in the manifest; hashed filename prevents collisions.
    return f"{hashlib.sha256(observation_id.encode()).hexdigest()[:24]}__{panel}.png"


def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = int(np.count_nonzero(a & b))
    union = int(np.count_nonzero(a | b))
    return inter / union if union else 0.0


def render_panels(observation_id: str, rgb: np.ndarray, mask: np.ndarray, panel_dir: Path) -> dict:
    panel_dir.mkdir(parents=True, exist_ok=True)
    rgb_path = panel_dir / panel_name(observation_id, "rgb_original")
    mask_path = panel_dir / panel_name(observation_id, "mask_overlay")
    crop_path = panel_dir / panel_name(observation_id, "crop_overlay")
    Image.fromarray(rgb).save(rgb_path)
    rgba = np.asarray(rgb).copy()
    color = np.asarray([0, 205, 220], dtype=np.uint8)
    rgba[mask] = (0.48 * rgba[mask] + 0.52 * color).astype(np.uint8)
    overlay = Image.fromarray(rgba)
    draw = ImageDraw.Draw(overlay)
    edge = mask ^ np.pad(mask[1:, :], ((0, 1), (0, 0)), mode="constant")
    edge |= mask ^ np.pad(mask[:-1, :], ((1, 0), (0, 0)), mode="constant")
    edge |= mask ^ np.pad(mask[:, 1:], ((0, 0), (0, 1)), mode="constant")
    edge |= mask ^ np.pad(mask[:, :-1], ((0, 0), (1, 0)), mode="constant")
    ys, xs = np.nonzero(edge)
    if len(xs):
        px = np.asarray(overlay).copy()
        px[ys, xs] = [255, 245, 0]
        overlay = Image.fromarray(px)
    expected_overlay = np.asarray(overlay).copy()
    overlay.save(mask_path)
    ys, xs = np.nonzero(mask)
    h, w = mask.shape
    if len(xs):
        pad = max(8, int(round(0.04 * max(h, w))))
        x0, x1 = max(0, int(xs.min()) - pad), min(w, int(xs.max()) + 1 + pad)
        y0, y1 = max(0, int(ys.min()) - pad), min(h, int(ys.max()) + 1 + pad)
    else:
        x0, y0, x1, y1 = 0, 0, w, h
    crop = np.asarray(overlay)[y0:y1, x0:x1]
    Image.fromarray(crop).save(crop_path)
    # Re-open and verify source panel is an exact lossless rendering of cached RGB.
    if not np.array_equal(np.asarray(Image.open(rgb_path)), rgb):
        raise RuntimeError(f"RGB panel pixel mismatch: {observation_id}")
    if not np.array_equal(np.asarray(Image.open(mask_path)), expected_overlay):
        raise RuntimeError(f"mask overlay pixel mismatch: {observation_id}")
    if not np.array_equal(np.asarray(Image.open(crop_path)), crop):
        raise RuntimeError(f"crop panel pixel mismatch: {observation_id}")
    return {
        "rgb_original": {"path": str(rgb_path), "sha256": sha(rgb_path), "source_rgb_exact_pixels": True},
        "mask_overlay": {"path": str(mask_path), "sha256": sha(mask_path), "source_mask_sha256": None},
        "crop_overlay": {"path": str(crop_path), "sha256": sha(crop_path), "crop_xyxy": [x0, y0, x1, y1]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--association-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_root, association_root, output = (p.resolve() for p in (args.run_root, args.association_root, args.output))
    if output.exists():
        raise FileExistsError(output)
    lock_path = run_root / "pilot_lock.json"
    capture_path = run_root / "capture/capture_manifest.json"
    p2_path = run_root / "detection_2d/detection_2d_index.json"
    p3_path = run_root / "detection_3d/detection_3d.jsonl"
    p3_manifest_path = run_root / "detection_3d/detection_3d_manifest.json"
    lock, capture, p2, p3_manifest = map(read_json, (lock_path, capture_path, p2_path, p3_manifest_path))
    if lock.get("status") != "locked_before_capture" or lock.get("role", "").find("development") < 0:
        raise ValueError("missing pre-capture lock/development scope")
    if p2.get("capture_manifest_sha256") != sha(capture_path):
        raise ValueError("P2/capture hash mismatch")
    if p3_manifest.get("capture_manifest_sha256") != sha(capture_path) or p3_manifest.get("detection_2d_cache_sha256") != sha(p2_path):
        raise ValueError("P3 inputs do not match locked capture/P2")
    if p3_manifest.get("object_evidence_sidecar", {}).get("status") != "pass":
        raise ValueError("direct P3 selected-point sidecar is incomplete")
    p3_rows = read_jsonl(p3_path)
    p2_by_id = {str(d["detection_id"]): d for frame in p2["frames"] for d in frame.get("detections", [])}
    cap_by_id = {str(f["frame_id"]): f for f in capture["frames"]}
    p3_by_id = {str(d["detection_id"]): (batch, d) for batch in p3_rows for d in batch.get("detections", [])}
    accepted_ids = set(p3_by_id)
    if len(accepted_ids) != int(p3_manifest.get("detection_3d_count", -1)):
        raise ValueError("P3 observation count mismatch")

    assignment_files = {
        "source": association_root / "source_assignments.jsonl",
        "geometry-only-dedup-v2": association_root / "geometry_only_dedup_v2_assignments.jsonl",
        "K4 accumulated surface support": association_root / "candidate_assignments.jsonl",
    }
    assignments: dict[str, dict] = {}
    for mode, path in assignment_files.items():
        rows = read_jsonl(path)
        assignments[mode] = {str(row["observation_id"]): row for row in rows}
        if set(assignments[mode]) != accepted_ids:
            raise ValueError(f"{mode}: assignment population differs from accepted P3 observations")

    # Predeclared sampling rule from pilot_lock.json: one hash-minimum observation per frame,
    # plus all same-frame accepted-mask overlaps with IoU >= 0.10. No assignments/classes are read.
    selected_reasons: dict[str, set[str]] = defaultdict(set)
    overlap_pairs = []
    salt = "q1-00808-primary-v1"
    p3_by_frame: dict[str, list[str]] = defaultdict(list)
    for oid, (batch, _det) in p3_by_id.items():
        p3_by_frame[str(batch["frame_id"])].append(oid)
    masks: dict[str, np.ndarray] = {}
    for oid in accepted_ids:
        p2det = p2_by_id[oid]
        mpath = run_root / "detection_2d" / p2det["mask_file"]
        if sha(mpath) != p2det.get("mask_sha256"):
            raise ValueError(f"P2 mask hash mismatch: {oid}")
        masks[oid] = np.asarray(np.load(mpath, allow_pickle=False), dtype=bool)
    for frame_id, ids in p3_by_frame.items():
        if not ids:
            continue
        anchor = min(ids, key=lambda oid: hashlib.sha256((oid + salt).encode()).hexdigest())
        selected_reasons[anchor].add("one_hash_selected_observation_per_frame")
        overlapping = [(oid, mask_iou(masks[anchor], masks[oid])) for oid in ids if oid != anchor]
        overlapping = [(oid, iou) for oid, iou in overlapping if iou >= 0.10]
        if overlapping:
            extra, iou = min(overlapping, key=lambda item: hashlib.sha256((item[0] + salt).encode()).hexdigest())
            selected_reasons[extra].add("hash_selected_same_frame_mask_overlap_partner")
            overlap_pairs.append({"frame_id": frame_id, "observation_ids": [anchor, extra], "mask_iou": iou})
    primary_ids = set(selected_reasons)

    # Relations differ by membership, never by track-ID string. Read the runner's pairwise ledger.
    relation_rows = read_jsonl(association_root / "relation_changes.jsonl")
    raw_diagnostic_rows = [row for row in relation_rows if row.get("status") != "same_frame_diagnostic_only"]
    pair_steps: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in raw_diagnostic_rows:
        pair = tuple(sorted(row["observation_ids"]))
        pair_steps[pair].add(str(row.get("step", "unknown")))
    diagnostic_pairs = [{"observation_ids": list(pair), "changed_in": sorted(steps)} for pair, steps in sorted(pair_steps.items())]
    diagnostic_ids = {oid for row in diagnostic_pairs for oid in row["observation_ids"]}
    diagnostics_only_ids = diagnostic_ids - primary_ids
    order = {str(frame["frame_id"]): int(frame["frame_index"]) for frame in capture["frames"]}
    sort_id = lambda oid: (order[oid.rsplit(":det-", 1)[0]], oid)
    all_review_ids = sorted(primary_ids | diagnostic_ids, key=sort_id)

    output.mkdir(parents=True)
    assets = output / "assets"
    panel_manifest: dict[str, dict] = {}
    used_paths: dict[str, str] = {}
    pose_by_frame: dict[str, dict] = {}
    route_entries = lock["route"].get("poses", [])
    for frame_index, frame in enumerate(capture["frames"]):
        label = route_entries[frame_index].get("label") if frame_index < len(route_entries) else None
        pose_by_frame[str(frame["frame_id"])] = {"frame_index": frame_index, "route_label": label}
    for oid in all_review_ids:
        batch, d3 = p3_by_id[oid]
        p2det = p2_by_id[oid]
        frame_id = str(batch["frame_id"])
        cap_record = cap_by_id[frame_id]
        rgb_path = run_root / "capture" / cap_record["frame_dir"] / "rgb.npy"
        mask_path = run_root / "detection_2d" / p2det["mask_file"]
        rgb = np.asarray(np.load(rgb_path, allow_pickle=False)[..., :3], dtype=np.uint8)
        mask = masks[oid]
        if rgb.shape[:2] != mask.shape:
            raise ValueError(f"RGB/mask shape mismatch: {oid}")
        panels = render_panels(oid, rgb, mask, assets)
        panels["mask_overlay"]["source_mask_sha256"] = p2det["mask_sha256"]
        for panel_name_, panel in panels.items():
            path = panel["path"]
            if path in used_paths and used_paths[path] != oid:
                raise ValueError(f"output panel path collision: {path}")
            used_paths[path] = oid
        evidence_row = next((r for r in read_jsonl(run_root / "detection_3d/object_evidence/index.jsonl")
                             if r["observation_id"] == oid), None)
        if evidence_row is None:
            raise ValueError(f"missing selected-point sidecar row: {oid}")
        with np.load(run_root / "detection_3d/object_evidence" / evidence_row["point_file"], allow_pickle=False) as data:
            T = np.asarray(data["T_W_Copt"], dtype=np.float64)
            selected_point_count = int(len(data["xyz_W"]))
        if T.shape != (4, 4):
            raise ValueError(f"invalid camera pose sidecar: {oid}")
        panel_manifest[oid] = {
            "observation_id": oid, "frame_id": frame_id, "frame_index": order[frame_id],
            "source_rgb": {"path": str(rgb_path), "sha256": sha(rgb_path)},
            "source_mask": {"path": str(mask_path), "sha256": sha(mask_path)},
            "panels": panels,
            "camera_pose_T_W_Copt": T.tolist(), "camera_origin_W_m": T[:3, 3].tolist(),
            "route_label": pose_by_frame[frame_id]["route_label"],
            "p3_selected_point_count": selected_point_count,
            "p2_mask_pixels": int(mask.sum()),
            "primary_reasons": sorted(selected_reasons.get(oid, set())),
            "diagnostic_only": oid in diagnostics_only_ids,
        }

    # Build primary/diagnostic HTML. Predicted tracks, classes, scores and evaluator labels are omitted.
    css = """body{font:16px system-ui;margin:24px;background:#f4f6f8;color:#17212b}article{background:white;border:1px solid #ccd5df;border-radius:8px;padding:16px;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}img{width:100%;height:auto;border:1px solid #bbc5ce}.id{font-family:monospace;font-size:14px;word-break:break-all}.meta{color:#465564}.forms{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:12px}label{display:block;margin:6px 0}textarea{width:100%;min-height:55px}h1{margin-bottom:4px}"""

    def card(oid: str, scope: str) -> str:
        rec = panel_manifest[oid]
        panels = rec["panels"]
        def im(name: str, title: str) -> str:
            return f'<div><b>{title}</b><br><img src="assets/{Path(panels[name]["path"]).name}" alt="{title} {html.escape(oid)}"></div>'
        meta = f"Frame {rec['frame_index']} · camera origin W={rec['camera_origin_W_m']} m · P3 points={rec['p3_selected_point_count']} · mask pixels={rec['p2_mask_pixels']}"
        return (f'<article><div class="id">{html.escape(oid)}</div><div class="meta">{meta}</div>'
                f'<div class="grid">{im("rgb_original","Original RGB")}{im("mask_overlay","P2 mask overlay (cyan; yellow edge)")}{im("crop_overlay","Mask crop")}</div>'
                f'<div class="forms"><div><b>Physical identity</b><label>Token <input name="identity_token_{html.escape(oid)}"></label>'
                f'<label>Status <select name="identity_status_{html.escape(oid)}"><option value="unreviewed">unreviewed</option><option>confirmed</option><option>uncertain</option></select></label>'
                f'<label>Relation notes <textarea name="identity_notes_{html.escape(oid)}"></textarea></label></div>'
                f'<div><b>Mask purity</b><label>Status <select name="mask_purity_{html.escape(oid)}"><option value="unreviewed">unreviewed</option><option>single_object</option><option>mixed_object_mask</option><option>uncertain</option></select></label>'
                f'<label>Notes <textarea name="purity_notes_{html.escape(oid)}"></textarea></label></div></div><div class="meta">Review scope: {scope}</div></article>')

    primary_html = "\n".join(card(oid, "primary; shared observation population for all three modes") for oid in sorted(primary_ids, key=sort_id))
    (output / "primary_review.html").write_text(
        f"<!doctype html><meta charset='utf-8'><title>Q1 primary blinded review</title><style>{css}</style>"
        f"<h1>Q1 00808 — primary blinded review</h1><p>Review identity and mask purity independently. Predicted tracks/classes are intentionally hidden. This is a development sample.</p><p>Selection: hash-minimum P3-accepted observation per frame, plus every same-frame P3-accepted mask-overlap pair with IoU ≥0.10. Selection did not read mode assignments.</p>{primary_html}", encoding="utf-8")
    diagnostic_cards = []
    seen_pairs = set()
    for row in diagnostic_pairs:
        pair = tuple(sorted(row["observation_ids"], key=sort_id))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        diagnostic_cards.append(f"<article><h3>Diagnostic changed relation</h3><p class='meta'>This pair is excluded from the primary sample summary.</p>{card(pair[0], 'diagnostic only; excluded from primary metrics')}{card(pair[1], 'diagnostic only; excluded from primary metrics')}</article>")
    (output / "diagnostic_review.html").write_text(
        f"<!doctype html><meta charset='utf-8'><title>Q1 diagnostic review</title><style>{css}</style>"
        f"<h1>Q1 00808 — changed-relation diagnostics</h1><p>Selected only after running modes; diagnostic cases are not added to primary metrics. Predicted tracks/classes remain hidden on these cards.</p>{''.join(diagnostic_cards)}", encoding="utf-8")

    with (output / "annotation_template.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["observation_id", "review_scope", "identity_token", "identity_status", "mask_purity", "identity_notes", "purity_notes"])
        writer.writeheader()
        for oid in sorted(primary_ids, key=sort_id):
            writer.writerow({"observation_id": oid, "review_scope": "primary", "identity_token": "", "identity_status": "unreviewed", "mask_purity": "unreviewed", "identity_notes": "", "purity_notes": ""})
        for oid in sorted(diagnostics_only_ids, key=sort_id):
            writer.writerow({"observation_id": oid, "review_scope": "diagnostic_only_excluded_from_primary", "identity_token": "", "identity_status": "unreviewed", "mask_purity": "unreviewed", "identity_notes": "", "purity_notes": ""})
    with (output / "diagnostic_relations_template.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["diagnostic_case_id", "observation_id_a", "observation_id_b", "same_physical_object", "uncertain", "notes", "observations_already_in_primary"])
        writer.writeheader()
        for index, pair in enumerate(sorted({tuple(sorted(row["observation_ids"], key=sort_id)) for row in diagnostic_pairs}), start=1):
            writer.writerow({"diagnostic_case_id": f"D{index:02d}", "observation_id_a": pair[0], "observation_id_b": pair[1], "same_physical_object": "", "uncertain": "", "notes": "", "observations_already_in_primary": str(all(oid in primary_ids for oid in pair)).lower()})

    pose_keys = {}
    accepted_pose_keys = {}
    all_pose_matrices = {}
    for oid, rec in panel_manifest.items():
        frame_id = rec["frame_id"]
        T = np.asarray(rec["camera_pose_T_W_Copt"])
        accepted_pose_keys[frame_id] = hashlib.sha256(np.round(T, 6).tobytes()).hexdigest()[:16]
    for frame in capture["frames"]:
        frame_id = str(frame["frame_id"])
        metadata_path = run_root / "capture" / frame["frame_dir"] / "metadata.json"
        metadata = read_json(metadata_path)
        actual_pose = np.eye(4, dtype=np.float64)
        actual_pose[:3, :3] = np.asarray(metadata["sensor_rotation_habitat"], dtype=np.float64)
        actual_pose[:3, 3] = np.asarray(metadata["sensor_position_habitat"], dtype=np.float64)
        pose_keys[frame_id] = hashlib.sha256(np.round(actual_pose, 6).tobytes()).hexdigest()[:16]
        all_pose_matrices[frame_id] = actual_pose.tolist()
    p2_counts = {str(frame["frame_id"]): len(frame.get("detections", [])) for frame in p2["frames"]}
    p3_counts = {str(batch["frame_id"]): len(batch.get("detections", [])) for batch in p3_rows}
    mode_summary = {}
    mode_tracks = {}
    for mode, rows in assignments.items():
        by_track: dict[str, list[str]] = defaultdict(list)
        decision_counts = Counter()
        for oid, row in rows.items():
            decision_counts[str(row.get("decision", "unknown"))] += 1
            track = row.get("track_id")
            if track is not None:
                by_track[str(track)].append(oid)
        alias_count = sum(1 for row in rows.values() if row.get("decision") in {"duplicate_alias", "duplicate_suppressed"})
        deferred = sum(1 for row in rows.values() if row.get("decision") == "defer" or row.get("track_id") is None)
        multiview_tracks = 0
        for ids in by_track.values():
            views = {pose_keys[oid.rsplit(":det-", 1)[0]] for oid in ids if oid.rsplit(":det-", 1)[0] in pose_keys}
            if len(views) >= 2:
                multiview_tracks += 1
        mode_summary[mode] = {"raw_P3_observations": len(rows), "tracks": len(by_track), "aliases": alias_count,
                              "deferred_or_unassigned": deferred, "decisions": dict(decision_counts),
                              "predicted_tracks_spanning_two_or_more_unique_camera_poses_not_identity_verified": multiview_tracks}
        mode_tracks[mode] = {track: sorted(ids, key=sort_id) for track, ids in by_track.items()}

    changed_summary = {}
    for key, row in read_json(association_root / "manifest.json")["relation_change_summaries"].items():
        changed_summary[key] = {k: row.get(k) for k in ["changed_pairs", "evaluator_verified", "identity_unavailable", "same_frame_diagnostic"]}
    association_manifest = read_json(association_root / "manifest.json")
    pilot_manifest = {
        "schema_version": 1, "pilot_id": lock["pilot_id"], "role": lock["role"],
        "lock_path": str(lock_path), "lock_sha256_final": sha(lock_path), "pre_capture_lock_sha256": lock.get("pre_capture_lock_sha256"),
        "inputs": {"capture_manifest_sha256": sha(capture_path), "p2_index_sha256": sha(p2_path),
                   "p3_cache_sha256": sha(p3_path), "p3_manifest_sha256": sha(p3_manifest_path),
                   "association_manifest_sha256": sha(association_root / "manifest.json")},
        "frames": len(capture["frames"]), "resolution_px": capture.get("resolution_px"),
        "P2_detections": int(p2.get("detection_count", 0)), "P3_accepted": int(p3_manifest.get("detection_3d_count", 0)),
        "P3_rejected": int(p3_manifest.get("rejected_detection_count", 0)), "P3_selected_point_records": int(p3_manifest["object_evidence_sidecar"].get("records", 0)),
        "p2_per_frame": p2_counts, "p3_accepted_per_frame": p3_counts,
        "unique_actual_camera_pose_count_route": len(set(pose_keys.values())),
        "unique_actual_camera_pose_count_with_accepted_p3_observations": len(set(accepted_pose_keys.values())),
        "actual_camera_pose_habitat_by_frame": all_pose_matrices,
        "mode_summary": mode_summary, "mode_track_membership": mode_tracks,
        "relation_change_summary": changed_summary,
        "relation_change_pairs": diagnostic_pairs,
        "K4_surface_continuity_matches": association_manifest.get("continuity_matches"),
        "K4_deferred": association_manifest.get("deferred_count"),
        "quality_metrics": {"status": "unavailable_no_human_identity_annotations_or_scene_evaluator_ledger", "pairwise_P_R_F1": None, "false_merge": None, "fragmentation": None},
        "review": {"primary_observation_ids": sorted(primary_ids, key=sort_id), "primary_count": len(primary_ids),
                   "diagnostic_pair_count": len(diagnostic_pairs), "diagnostic_observation_ids": sorted(diagnostic_ids, key=sort_id),
                   "diagnostic_only_observation_ids": sorted(diagnostics_only_ids, key=sort_id),
                   "same_frame_p3_mask_overlap_pairs_iou_at_least_0_10": overlap_pairs,
                   "panel_records": panel_manifest, "panel_paths_unique_across_observations": len(used_paths) == sum(len(rec["panels"]) for rec in panel_manifest.values())},
        "runner_note": "Offline predictor assignments/manifest completed. The runner exited nonzero only during legacy report formatting because the no-evaluator metrics omit false_merge_tracks; Q1 report is generated here with metrics unavailable, not zero.",
        "review_paths": {"primary": str(output / "primary_review.html"), "diagnostic": str(output / "diagnostic_review.html"), "annotation_csv": str(output / "annotation_template.csv"), "diagnostic_relations_csv": str(output / "diagnostic_relations_template.csv")},
    }
    (output / "manifest.json").write_text(json.dumps(pilot_manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    # Human-readable report uses only measurements from this pilot; stale 00802 timing entries are excluded.
    lines = [
        "# Q1 HM3D 00808 — 7-pose development pilot", "",
        "Status: pipeline completed; same-scene new-route development, not an unseen-scene test. No GT or annotation entered the predictors.", "",
        f"- Capture: {len(capture['frames'])} frames at {capture.get('resolution_px')} px; P2={p2.get('detection_count')} detections; P3 accepted/rejected={p3_manifest.get('detection_3d_count')}/{p3_manifest.get('rejected_detection_count')}; direct P3 selected-point sidecar={p3_manifest['object_evidence_sidecar'].get('records')} records.",
        f"- Route: {len(capture['frames'])} frames across {len(set(pose_keys.values()))} unique actual sensor poses; P3-accepted observations cover {len(set(accepted_pose_keys.values()))} of those poses. Exact-pose revisits count once. Frame 3 has P2/P3={p2_counts.get('hm3d-00808-k4-ag-v1-0003', 0)}/{p3_counts.get('hm3d-00808-k4-ag-v1-0003', 0)} detections, so no accepted object evidence at the kitchen detour.", "",
        "| Mode | Tracks | Aliases | Deferred/unassigned | Decisions | Predicted tracks span ≥2 camera poses* |",
        "|---|---:|---:|---:|---|---:|"
    ]
    for mode, s in mode_summary.items():
        lines.append(f"| {mode} | {s['tracks']} | {s['aliases']} | {s['deferred_or_unassigned']} | `{s['decisions']}` | {s['predicted_tracks_spanning_two_or_more_unique_camera_poses_not_identity_verified']} |")
    lines += ["", "*Pose span is computed within predicted tracks and is not identity verification.", "",
              "## Association differences", "",
              f"- Source → geometry-only-dedup-v2: {changed_summary.get('source_to_geometry-only-dedup-v2', {}).get('changed_pairs')} cross-frame pair relations changed. See `association/relation_changes.jsonl`; track suffix renaming is not counted.",
              f"- Geometry-only-dedup-v2 → K4: {changed_summary.get('geometry-only-dedup-v2_to_accumulated-surface-support', {}).get('changed_pairs')} pair relations changed; K4 continuity branch matches={association_manifest.get('continuity_matches')}, deferred={association_manifest.get('deferred_count')}. No partial-view-continuity branch was exercised on this pilot.",
              "- Changed observation pairs (diagnostic only; not correctness claims):"]
    for row in diagnostic_pairs:
        lines.append(f"  - `{row['observation_ids'][0]}` ↔ `{row['observation_ids'][1]}` — changed in `{', '.join(row['changed_in'])}`.")
    lines += [
              "- The source and geometry-only modes differ in same-frame alias policy; there are 2 dedup-v2 aliases. This output does not establish that those aliases are correct.",
              "- Pairwise precision/recall/F1, false merges and fragmentation are unavailable: no human identity annotations or scene evaluator ledger is bound to this new route. Unavailable is not zero.", "",
              "## Runtime and memory", "",
              "| Stage | Wall time | Peak RSS | Measurement scope |", "|---|---:|---:|---|"]
    time_specs = [("capture", "logs_capture_time.txt"), ("P2", "logs_p2_time.txt"), ("P3 + direct point sidecar", "logs_p3_time.txt"), ("offline association all modes", "logs_association_time.txt")]
    import re
    for label, timefile in time_specs:
        txt = (run_root / timefile).read_text()
        et = re.search(r"Elapsed \(wall clock\) time \(h:mm:ss or m:ss\): ([^\n]+)", txt)
        rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", txt)
        elapsed = et.group(1) if et else "not recorded"
        mem = f"{int(rss.group(1))/1024:.1f} MiB" if rss else "not recorded"
        scope = "single process, /usr/bin/time -v" if et else ""
        lines.append(f"| {label} | {elapsed} | {mem} | {scope} |")
    lines += ["", f"K4's internal causal predictor timer: {association_manifest.get('causal_replay_runtime_seconds'):.2f} s; process peak RSS in its manifest: {association_manifest.get('peak_process_rss_mib'):.1f} MiB. Source/dedup per-mode wall time was not separately instrumented in this run; do not reuse the runner's imported 00802 comparison timing values.",
              "", "## Does this pilot supply the needed evidence?", "",
              f"- Different camera positions: yes; accepted P3 observations occur at {len(set(accepted_pose_keys.values()))} actual sensor poses and revisit earlier poses. Exact revisits are not extra views.",
              "- Partial-view expansion: not established. K4 used zero surface-continuity matches; cross-pose track membership is still a prediction to review, not proof of same physical object.",
              f"- Nearby-object risk: same-frame accepted mask overlap pairs (IoU ≥0.10) found: {len(overlap_pairs)}. These are included as the predeclared difficult stratum; proximity alone is not evidence of different/same identity.",
              "- Route visibility limitation: frame 3 kitchen detour produced no P2 detections, so the planned kitchen-side object view is absent. Do not extend the route or infer anything about that view in this pilot.",
              "", "## Review", "",
              f"Primary blinded review: `{output / 'primary_review.html'}` ({len(primary_ids)} observations). Changed-relation diagnostics: `{output / 'diagnostic_review.html'}` ({len(diagnostic_pairs)} pairs), separately excluded from primary sample. Primary CSV: `{output / 'annotation_template.csv'}`; diagnostic relation CSV: `{output / 'diagnostic_relations_template.csv'}`.",
              "Identity and mask purity are separate fields. Predicted labels/tracks are hidden in review pages. All panel paths are keyed from the full observation ID and checked for collisions.",
              "", "## Reproduction", "",
              "Capture/P2/P3 were run once and their commands plus `/usr/bin/time -v` logs are preserved in the run folder. Offline modes: `python3 scripts/run_u3_surface_support_candidate.py --input-run <RUN_ROOT> --p3-output <RUN_ROOT>/detection_3d --output <NEW_ASSOCIATION_DIR>`. The runner currently exits nonzero during legacy report formatting when no evaluator ledger is present; assignment outputs and manifest were completed. This report/review builder handles that no-GT case explicitly.",
              "", "No native replay, semantic GT rendering, U4-OV, OpenCLIP, graph/M9, room-score changes, or parameter tuning were run."]
    (output / "report.vi.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status":"pass","report":str(output/"report.vi.md"),"primary_review":str(output/"primary_review.html"),"diagnostic_review":str(output/"diagnostic_review.html"),"primary_count":len(primary_ids),"diagnostic_pairs":len(diagnostic_pairs),"panels":sum(len(rec["panels"]) for rec in panel_manifest.values()),"unique_panel_paths":len(used_paths)},indent=2,ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
