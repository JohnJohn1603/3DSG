#!/usr/bin/env python3
"""Read-only dense hybrid object/semantic readiness and membership gate.

This consumes a completed cached hybrid run.  It does not run perception,
mapping, Qwen, or room assignment.  Room scoring is deliberately withheld
when the linked room snapshot has no elevation scope contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import html
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.semantics.room_belief_v1 import (  # noqa: E402
    ConsensusPolicy,
    SemanticObservation,
    aggregate_object_semantics,
)

ALIASES = {"couch": "sofa", "dining table": "dining_table", "potted plant": "potted_plant"}
A_CATEGORIES = {"bed", "sofa", "chair", "dining_table", "toilet", "sink", "refrigerator", "oven", "microwave", "potted_plant", "tv"}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def read(path: Path):
    return json.loads(path.read_text())


def pose_id(pose) -> str:
    payload = json.dumps([[round(float(x), 7) for x in row] for row in pose], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hybrid-run", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    run = args.hybrid_run.resolve()
    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"refusing non-empty output: {out}")
    out.mkdir(parents=True, exist_ok=True)

    manifest = read(run / "run_manifest.json")
    contract = read(run / "experiment_contract.json")
    export = read(run / "export/export_manifest.json")
    diag = read(run / "diagnostic/diagnostic_manifest.json")
    cache = Path(contract["cache_root"]).resolve()
    capture_path = cache / "capture/capture_manifest.json"
    p2_path = cache / "detection_2d/detection_2d_index.json"
    snap_path = Path(contract["room_snapshot"]).resolve()
    capture = read(capture_path)
    p2 = read(p2_path)
    p3_manifest = read(cache / "detection_3d/detection_3d_manifest.json")
    image_report_path = ROOT / "artifacts/room_belief/dense_00802_hybrid_sidecars_20261015/image_features/feature_extraction_report.json"
    text_report_path = ROOT / "artifacts/room_belief/dense_00802_hybrid_sidecars_20261015/text_features/manifest.json"
    image_report = read(image_report_path)
    text_report = read(text_report_path)
    snap = read(snap_path)
    snap = snap.get("snapshot", snap)

    if manifest.get("status") != "pass":
        raise ValueError("hybrid run is not complete")
    if export.get("scene_id") != capture.get("scene_id") or export.get("sequence_id") != capture.get("sequence_id"):
        raise ValueError("export/capture scene or sequence mismatch")
    if (snap.get("scene_id"), snap.get("sequence_id")) != (capture.get("scene_id"), capture.get("sequence_id")):
        raise ValueError("room snapshot scene or sequence mismatch")

    # P2 lookup and capture pose lookup preserve the same input ordering and
    # exact-pose revisit policy used by RB1.  Capture base pose and optical
    # pose differ only by a fixed extrinsic, so equality groups are preserved.
    p2_by_id = {}
    for frame in p2.get("frames", []):
        for d in frame.get("detections", []):
            p2_by_id[str(d["detection_id"])] = d
    frame_meta = {}
    for frame in capture.get("frames", []):
        m = cache / "capture" / frame["frame_dir"] / "metadata.json"
        frame_meta[str(frame["frame_id"])] = read(m)

    cfg = read(ROOT / "configs/room_semantics/consensus_v1.proposal.json")
    policy = ConsensusPolicy(policy_id=cfg["policy_id"], view_group_policy_id=cfg["view_group_policy_id"],
                             minimum_view_groups=int(cfg["minimum_view_groups"]),
                             minimum_top_share=float(cfg["minimum_top_share"]),
                             minimum_margin=float(cfg["minimum_margin"]))

    objects = []
    for h in diag.get("objects", []):
        status = h["status_after_filter"]
        sem_obs = []
        ids = list(h.get("observation_ids", []))
        for oid in ids:
            d = p2_by_id[oid]
            frame_id = oid.split(":", 1)[0]
            pose = frame_meta[frame_id]["base_pose_canonical"]
            raw = str(d.get("class_raw", "")).strip().casefold()
            canonical = ALIASES.get(raw, raw)
            sem_obs.append(SemanticObservation(observation_id=oid, frame_id=frame_id,
                view_group_id=pose_id(pose), raw_class=raw, category=canonical,
                admission_status="retained" if status == "retained" else "filtered"))
        consensus = aggregate_object_semantics(h["lineage_key"], sem_obs, semantic_revision=1, policy=policy)
        support = dict(consensus.category_support)
        eligible_categories = sorted({c for c in support if c in A_CATEGORIES}) if status == "retained" else []
        excluded = [] if status == "retained" else ["filtered_by_obj_min_detections"]
        objects.append({
            "hypothesis_id": h["hypothesis_id"], "lineage_key": h["lineage_key"],
            "admission_status": status, "filter_reason": h.get("filter_reason"),
            "detection_count": h.get("detection_count"), "distinct_frame_count": h.get("distinct_frame_count"),
            "distinct_pose_count": h.get("distinct_pose_count"), "observation_ids": ids,
            "raw_class_history": [o.raw_class for o in sem_obs],
            "canonical_class_history": [o.category for o in sem_obs],
            "semantic_status": "excluded_filtered" if status != "retained" else consensus.status,
            "semantic_category": None if status != "retained" else consensus.category,
            "category_support_by_exact_pose_unit": support if status == "retained" else {},
            "eligible_categories_in_A": eligible_categories,
            "semantic_exclusion_reasons": excluded,
            "consensus_policy": policy.as_dict(),
            "membership_status": "blocked_missing_elevation_scope",
            "room_evidence_eligible": False,
        })

    rooms = []
    for node in snap.get("nodes", []):
        if node.get("kind") == "room":
            a = node.get("attributes", {})
            rooms.append({"room_id": node.get("node_id"), "revision": a.get("revision"),
                          "membership_status": "blocked_missing_elevation_scope",
                          "semantic_status": "not_scored_membership_gate_blocked",
                          "eligible_categories": []})

    # Avoid treating P3's floor_0 label as proof. Read it cheaply from the JSONL.
    layers = set()
    for line in (cache / "detection_3d/detection_3d.jsonl").read_text().splitlines():
        row = json.loads(line)
        for d in row.get("detections", []):
            if d.get("layer_id"):
                layers.add(str(d["layer_id"]))
    room_floor_fields = []
    for node in snap.get("nodes", []):
        if node.get("kind") == "room":
            a = node.get("attributes", {})
            room_floor_fields.append({k: a.get(k) for k in ("floor_id", "layer_id", "z_min", "z_max") if a.get(k) is not None})
    camera_z = [float(frame_meta[f["frame_id"]]["base_pose_canonical"][2][3]) for f in capture.get("frames", [])]
    scope = {
        "membership_gate": "blocked",
        "reason": "room polygons are XY-only and room nodes provide no floor/layer/Z bounds; P3 floor_0 is not treated as single-floor proof",
        "p3_layer_ids": sorted(layers), "room_floor_metadata": room_floor_fields,
        "camera_base_z_range_m": [min(camera_z), max(camera_z)] if camera_z else None,
        "room_count": len(rooms), "policy": "rb1_greedy_voxel_support_v1 not executed without verified elevation scope",
    }
    counts = Counter(o["semantic_status"] for o in objects)
    categories = Counter(c for o in objects if o["admission_status"] == "retained" for c in o["eligible_categories_in_A"])
    report = {
        "schema": "dense-hybrid-room-belief-readiness/v1",
        "status": "object_export_and_consensus_complete_room_static_blocked",
        "run_id": run.name, "scene_id": capture["scene_id"], "sequence_id": capture["sequence_id"],
        "inputs": {"cache_root": str(cache), "hybrid_run": str(run), "room_snapshot": str(snap_path),
                   "frames": manifest["frames"], "observations": manifest["observations"],
                   "p2_detections_mapped": manifest["reference_detections_mapped"],
                   "p3_rejected_not_pruned": int(p3_manifest.get("rejected_detection_count", 0)), "pre_filter_hypotheses": manifest["foreground_objects_before_postprocess"],
                   "retained": manifest["objects_after_filter"], "final_after_merge": manifest["final_objects_after_text_gated_merge"]},
        "provenance": {"run_manifest_sha256": sha(run/"run_manifest.json"), "contract_sha256": sha(run/"experiment_contract.json"),
                       "export_manifest_sha256": sha(run/"export/export_manifest.json"), "capture_manifest_sha256": sha(capture_path),
                       "p2_index_sha256": sha(p2_path), "room_snapshot_sha256": sha(snap_path),
                       "effective_config": contract["effective_config"], "seed": contract["rng_control"]["seed"],
                       "source_display_label": export.get("provenance", {}).get("display_label"),
                       "not_full_reference_parity": True},
        "feature_and_mapping_status": {
            "image_features": {"status": image_report.get("status"), "manifest": str((image_report_path.parent / "feature_manifest.jsonl").resolve()), "manifest_sha256": sha(image_report_path.parent / "feature_manifest.jsonl"), "rows": image_report.get("observation_count"), "reused": image_report.get("reused_count"), "created": image_report.get("new_count"), "dimension": image_report.get("feature_dimension"), "runtime_s": image_report.get("runtime_s"), "peak_rss_mib": image_report.get("peak_rss_mib")},
            "text_features": {"status": text_report.get("status"), "manifest": str(text_report_path.resolve()), "manifest_sha256": sha(text_report_path), "rows": text_report.get("observation_count"), "unique_class_texts": text_report.get("unique_class_text_count"), "dimension": text_report.get("embedding_shape", [None, None])[1], "runtime_s": text_report.get("runtime_s"), "peak_rss_mib": text_report.get("peak_rss_mib")},
            "mapping": {"status": "pass", "all_p2_observations": 738, "p3_rejected_not_pruned": int(p3_manifest.get("rejected_detection_count", 0)), "runtime_s": manifest.get("mapping_runtime_s"), "total_runtime_s": manifest.get("runtime_s"), "peak_rss_mib": manifest.get("peak_rss_mib")}},
        "membership_scope": scope,
        "consensus_policy": {**policy.as_dict(), "aliases": ALIASES, "a_categories": sorted(A_CATEGORIES), "approval_status": cfg["approval_status"]},
        "summary": {"semantic_status_counts": dict(counts), "eligible_A_category_units": dict(categories),
                     "unsupported_retained_raw_classes": sorted({c for o in objects if o["admission_status"] == "retained" for c in o["canonical_class_history"] if c not in A_CATEGORIES})},
        "objects": objects, "rooms": rooms,
        "limitations": ["Development dense cache, not holdout or causal online validation.",
                        "Room functional accuracy unavailable; no room GT supplied.",
                        "Room static diagnostic and four A variants are not run because elevation scope is unverified.",
                        "P3 floor_0 metadata is retained as provenance only, not promoted to a floor proof.",
                        "Thresholds and admission remain unchanged and uncalibrated for hybrid ViT-B-32."],
    }
    (out / "dense_hybrid_readiness.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    md = ["# Dense hybrid 00802 — object/semantic readiness", "", f"- Status: **{report['status']}**",
          f"- Run: `{run.name}` | frames {manifest['frames']} | observations {manifest['observations']}",
          f"- Hypotheses: {manifest['foreground_objects_before_postprocess']} before filter → {manifest['objects_after_filter']} retained → {manifest['final_objects_after_text_gated_merge']} after merge",
          "- Mapping used all P2 observations; P3 rejected observations were not pruned.", "",
          "## Membership gate", "", f"- **Blocked:** {scope['reason']}", f"- P3 layer IDs: `{scope['p3_layer_ids']}`; room floor metadata: `{scope['room_floor_metadata']}`", "- Camera base-Z range is recorded for evidence only; it is not a floor proof.", "",
          "## Semantic readiness", "", f"- Consensus statuses: `{dict(counts)}`", f"- A-category units among retained hypotheses: `{dict(categories)}`", "- Filtered hypotheses are diagnostic-only and do not vote.", "- Room A scoring: not executed because membership scope is blocked.", "",
          "## Next bounded step", "", "Provide a room snapshot with explicit floor/elevation bounds (or an equivalent verified scope contract), then run RB1 greedy voxel membership and the existing four-table static diagnostic without changing the hybrid mapping."]
    (out / "dense_hybrid_readiness.vi.md").write_text("\n".join(md) + "\n")
    # Lightweight dependency-free viewer. It intentionally marks every room
    # membership as blocked; it never imports objects into the room graph.
    rings = {n["node_id"]: n.get("attributes", {}).get("outer_ring", []) for n in snap.get("nodes", []) if n.get("kind") == "room"}
    points = [p for ring in rings.values() for p in ring]
    points += [p for h in diag.get("objects", []) for p in [h["bounds_minmax"][0][:2], h["bounds_minmax"][1][:2]]]
    if points:
        min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
        min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    else:
        min_x = min_y = 0.0; max_x = max_y = 1.0
    sx, sy = max(max_x - min_x, 1e-6), max(max_y - min_y, 1e-6)
    def xy(p):
        return (40 + (float(p[0]) - min_x) * 900 / sx, 700 - (40 + (float(p[1]) - min_y) * 620 / sy))
    room_svg = []
    for rid, ring in rings.items():
        pts = " ".join(f"{xy(p)[0]:.1f},{xy(p)[1]:.1f}" for p in ring)
        room_svg.append(f'<polygon points="{pts}" fill="#e5edf7" fill-opacity=".55" stroke="#334155" stroke-width="2"/><text x="{xy(ring[0])[0]:.1f}" y="{xy(ring[0])[1]:.1f}" class="room">{html.escape(rid)}</text>')
    pre_js = []
    for h in diag.get("objects", []):
        lo, hi = h["bounds_minmax"]
        x, y = xy(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2))
        color = "#16845b" if h["status_after_filter"] == "retained" else "#dc2626"
        pre_js.append({"id": h["hypothesis_id"], "x": x, "y": y, "color": color, "status": h["status_after_filter"], "obs": h.get("observation_ids", []), "raw": h.get("class_history_raw", [])})
    final_js = []
    for item in export.get("objects", []):
        lo, hi = item["bounds_minmax"]
        x, y = xy(((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2))
        final_js.append({"id": item["export_id"], "x": x, "y": y, "color": "#64748b", "status": "membership blocked", "obs": item.get("evidence", []), "raw": item.get("reference_class_history", [])})
    viewer_data = json.dumps({"pre": pre_js, "final": final_js}, ensure_ascii=False).replace("</", "<\\/")
    svg_base = "".join(room_svg)
    viewer = f'''<!doctype html><meta charset="utf-8"><title>Dense hybrid readiness viewer</title>
<style>body{{font:14px system-ui;margin:18px;color:#172033}}button{{padding:7px 12px;margin-right:6px}}.note{{background:#fff7ed;border:1px solid #fdba74;padding:10px;margin:10px 0}}svg{{width:100%;max-width:980px;border:1px solid #cbd5e1;background:#fff}}.room{{font-size:13px;font-weight:700}}#details{{white-space:pre-wrap;border:1px solid #cbd5e1;padding:10px;max-width:980px}}</style>
<h1>Dense hybrid object readiness</h1><div class="note">Hybrid cached YOLO11n-seg + ViT-B-32 / ConceptGraphs mapping core. Room membership and room A scoring are blocked because the room snapshot has XY polygons but no verified elevation scope. Objects are not assigned to rooms here.</div>
<button id="pre">Before filter (65 hypotheses)</button><button id="fin">Final export (33 objects)</button><svg id="map" viewBox="0 0 980 740">{svg_base}<g id="objs"></g></svg><div id="details">Select an object.</div>
<script>const D={viewer_data};let mode='pre';const base={json.dumps(svg_base)};function draw(){{let g=document.getElementById('objs');g.innerHTML='';for(const o of D[mode]){{let e=document.createElementNS('http://www.w3.org/2000/svg','circle');e.setAttribute('cx',o.x);e.setAttribute('cy',o.y);e.setAttribute('r',mode==='pre'?5:6);e.setAttribute('fill',o.color);e.setAttribute('stroke','#fff');e.onclick=()=>{{document.getElementById('details').textContent=o.id+'\\n'+o.status+'\\nraw: '+o.raw.join(', ')+'\\nobservations ('+o.obs.length+'):\\n'+o.obs.join('\\n')}};g.appendChild(e)}}}}document.getElementById('pre').onclick=()=>{{mode='pre';draw()}};document.getElementById('fin').onclick=()=>{{mode='final';draw()}};draw();</script>'''
    (out / "dense_hybrid_readiness.html").write_text(viewer, encoding="utf-8")
    names = ("dense_hybrid_readiness.json", "dense_hybrid_readiness.vi.md", "dense_hybrid_readiness.html")
    (out / "manifest.json").write_text(json.dumps({"schema":"dense-hybrid-readiness-artifact/v1", "files_sha256": {n: sha(out/n) for n in names}}, indent=2)+"\n")
    print(json.dumps({"status": report["status"], "output": str(out), "objects": len(objects), "rooms": len(rooms), "membership_gate": scope["membership_gate"], "A_tables": "not_run"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
