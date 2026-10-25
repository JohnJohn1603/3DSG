#!/usr/bin/env python3
"""Render a provenance-first report for an existing RB2 static diagnostic.

This is a read-only presentation layer.  It does not score Qwen, replay
perception/mapping, or alter A tables, room geometry, or the default graph.
The existing hybrid mapping viewer remains the geometry/evidence viewer.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def top_two(belief: dict[str, float]) -> tuple[str | None, str | None, float | None]:
    ordered = sorted(belief.items(), key=lambda pair: pair[1], reverse=True)
    if not ordered:
        return None, None, None
    return ordered[0][0], ordered[1][0] if len(ordered) > 1 else None, (
        ordered[0][1] - ordered[1][1] if len(ordered) > 1 else None
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--readiness", type=Path, required=True)
    parser.add_argument("--static", type=Path, required=True)
    parser.add_argument("--hybrid-manifest", type=Path, required=True)
    parser.add_argument("--a-import-manifest", type=Path, required=True)
    parser.add_argument("--a-archive", type=Path, required=True)
    parser.add_argument("--viewer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    readiness = read(args.readiness)
    static = read(args.static)
    hybrid = read(args.hybrid_manifest)
    a_import = read(args.a_import_manifest)
    if static.get("schema") != "rb2-static-room-diagnostic/v2":
        raise SystemExit("unsupported static diagnostic schema")
    if readiness.get("hybrid_run_id") != hybrid.get("run_id"):
        raise SystemExit("readiness and hybrid run IDs differ")
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output.parent.parent.parent.parent
    # The viewer is kept as the existing geometry/evidence surface.  Use a
    # relative link so the report remains portable inside the workspace.
    viewer_rel = Path(__import__("os").path.relpath(args.viewer.resolve(), args.output.resolve()))
    readiness_rel = Path(__import__("os").path.relpath(args.readiness.resolve(), args.output.resolve()))
    static_rel = Path(__import__("os").path.relpath(args.static.resolve(), args.output.resolve()))

    objs = readiness.get("objects", [])
    admission = Counter(obj.get("admission_status") for obj in objs)
    semantic = Counter(obj.get("semantic_status") for obj in objs)
    membership = Counter((obj.get("membership") or {}).get("status") for obj in objs)
    unsupported: list[dict[str, Any]] = []
    with zipfile.ZipFile(args.a_archive) as archive:
        names = sorted(name for name in archive.namelist() if name.endswith("/full_a/a_table_mean_no_neutral.csv"))
        if len(names) != 1:
            raise SystemExit("A archive does not contain exactly one mean_no_neutral table")
        a_categories = {row["category"] for row in csv.DictReader(__import__("io").StringIO(archive.read(names[0]).decode("utf-8")))}
    # The static scorer records both the raw consensus string and its approved
    # canonical category.  Use the latter for A-vocabulary membership so
    # couch->sofa is not reported as unsupported.
    selected_static_objects = static.get("objects", [])
    for obj in selected_static_objects:
        category = obj.get("category")
        if category and obj.get("semantic_status") == "stable" and category not in a_categories:
            unsupported.append({"hypothesis_id": obj.get("hypothesis_id"), "category": category,
                                "raw_consensus": obj.get("consensus_category_raw")})

    manifest = {
        "schema": "rb2-00802-static-room-diagnostic/v1",
        "static_kind": "development_static_noncausal",
        "hybrid_run_id": hybrid.get("run_id"),
        "scene_id": hybrid.get("scene_id"),
        "sequence_id": hybrid.get("sequence_id"),
        "seed": hybrid.get("seed"),
        "cache_root": hybrid.get("cache_root"),
        "room_snapshot": hybrid.get("room_snapshot"),
        "hybrid_manifest_sha256": sha256(args.hybrid_manifest),
        "readiness_manifest_sha256": sha256(args.readiness.parent / "manifest.json"),
        "readiness_sha256": sha256(args.readiness),
        "static_diagnostic_sha256": sha256(args.static),
        "a_import_manifest_sha256": sha256(args.a_import_manifest),
        "a_archive_sha256": a_import.get("archive_sha256"),
        "effective_config_sha256": hybrid.get("effective_config_sha256"),
        "mapping_run_manifest_sha256": hybrid.get("mapping_run_manifest_sha256"),
        "room_snapshot_sha256": hybrid.get("room_snapshot_sha256"),
        "filter_config": "obj_min_detections=3; obj_min_points=0",
        "viewer": str(args.viewer.resolve()),
        "viewer_sha256": sha256(args.viewer),
        "population": {
            "frames": readiness.get("input_scope", {}).get("frame_count"),
            "observations": readiness.get("input_scope", {}).get("mapped_observation_count"),
            "hypotheses": len(objs),
            "admission": dict(admission),
            "semantic_status": dict(semantic),
            "membership_status": dict(membership),
            "unsupported_consensus_categories": unsupported,
        },
        "functional_room_gt": "unavailable",
        "multi_floor_claim": False,
    }
    (args.output / "static_report_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    room_rows: list[str] = []
    for variant, payload in static.get("variants", {}).items():
        for room in payload.get("rooms", []):
            top1, top2, margin = top_two(room.get("belief", {}))
            room_rows.append(
                "<tr>"
                f"<td>{html.escape(variant)}</td><td>{html.escape(room['room_id'])}</td>"
                f"<td>{html.escape(', '.join(room.get('eligible_categories', [])) or '—')}</td>"
                f"<td><pre>{html.escape(json.dumps(room.get('A_contributions', {}), ensure_ascii=False, sort_keys=True, indent=2))}</pre></td>"
                f"<td><pre>{html.escape(json.dumps(room.get('raw_S', {}), ensure_ascii=False, sort_keys=True, indent=2))}</pre></td>"
                f"<td>{html.escape(str(top1))}<br>{html.escape(str(top2))}<br>margin={html.escape(str(margin))}</td>"
                f"<td>{html.escape(str(room.get('commit_label')))}</td>"
                f"<td>{html.escape(str(room.get('status')))}<br>{html.escape(str(room.get('reason')))}</td>"
                "</tr>"
            )
    obj_rows = []
    for obj in objs:
        mem = obj.get("membership") or {}
        obj_rows.append(
            "<tr>"
            f"<td>{html.escape(str(obj.get('hypothesis_id')))}</td>"
            f"<td>{html.escape(str(obj.get('admission_status')))}</td>"
            f"<td>{html.escape(str(obj.get('semantic_status')))}</td>"
            f"<td>{html.escape(str(obj.get('semantic_category_raw_exact')))}</td>"
            f"<td>{html.escape(str(mem.get('status')))} / {html.escape(str(mem.get('room_id')))}</td>"
            f"<td>{html.escape(str(obj.get('detection_count')))} / {html.escape(str(obj.get('distinct_frame_count')))} / {html.escape(str(obj.get('distinct_pose_count_from_reference')))}</td>"
            f"<td>{html.escape(', '.join(obj.get('semantic_observations', [])))}</td>"
            "</tr>"
        )
    scope = readiness.get("membership_scope", {})
    body = f"""<!doctype html><meta charset='utf-8'><title>RB2 00802 static room belief diagnostic</title>
<style>body{{font:14px system-ui,sans-serif;margin:22px;color:#172033}}table{{border-collapse:collapse;width:100%;margin:12px 0}}td,th{{border:1px solid #cbd5e1;padding:5px;vertical-align:top}}pre{{margin:0;white-space:pre-wrap;max-height:220px;overflow:auto}}.note{{background:#fff7ed;border:1px solid #fdba74;padding:10px}}a{{color:#075985}}</style>
<h1>RB2 00802 — static room-belief diagnostic</h1>
<p>Development-only static diagnostic. No Qwen, capture, detector, encoder, mapping replay or room-graph mutation was performed.</p>
<p><b>Run:</b> {html.escape(str(hybrid.get('run_id')))} · <b>Scene:</b> {html.escape(str(hybrid.get('scene_id')))} · <b>Sequence:</b> {html.escape(str(hybrid.get('sequence_id')))} · <b>Filter:</b> min detections = 3</p>
<p><a href='{html.escape(str(viewer_rel))}'>Open existing hybrid geometry/evidence viewer</a> · <a href='{html.escape(str(readiness_rel))}'>Readiness JSON</a> · <a href='{html.escape(str(static_rel))}'>Static JSON</a></p>
<div class='note'>Room functional GT is unavailable; no accuracy/F1 claim is made. XY membership is restricted to the verified single observed layer <b>{html.escape(','.join(scope.get('observed_floor_ids', [])))}</b>. Room polygons have no floor IDs or Z bounds, so this is not a multi-floor claim.</div>
<h2>Population and stage outcomes</h2><pre>{html.escape(json.dumps(manifest['population'], ensure_ascii=False, indent=2))}</pre>
<h2>Object evidence</h2><p>Columns: hypothesis, admission, consensus status, canonical category, greedy membership, detection/frame/pose support, semantic observation IDs. Filtered and conflicted objects are not room evidence.</p>
<table><tr><th>Hypothesis</th><th>Admission</th><th>Semantic</th><th>Category</th><th>Membership</th><th>Det/frame/pose</th><th>Observation IDs</th></tr>{''.join(obj_rows)}</table>
<h2>Room belief by variant</h2><p>Formula: <code>S(r)=log(runtime_prior(r))+sum(A_log_ratio_uncalibrated(category,r))</code> over unique eligible categories, followed by softmax. Candidate labels are not committed labels.</p>
<table><tr><th>Variant</th><th>Room</th><th>Eligible categories</th><th>A contributions</th><th>Raw S</th><th>Top-1 / Top-2 / margin</th><th>Commit</th><th>Status / reason</th></tr>{''.join(room_rows)}</table>
<h2>Interpretation</h2><ul><li><code>insufficient_support</code> means fewer than the configured two unique eligible categories.</li><li><code>belief_or_margin_below_development_proposal</code> means the RB1 proposal gates were not met; thresholds remain uncalibrated.</li><li><code>no_evidence</code> means no retained, stable, assigned, A-supported consensus object reached that room.</li><li>Categories outside the 11-row A vocabulary are reported as unsupported and do not receive a substitute label.</li></ul>"""
    (args.output / "static_room_belief_report.html").write_text(body, encoding="utf-8")
    (args.output / "static_room_belief_report.md").write_text(
        "# RB2 00802 static room-belief diagnostic\n\n"
        f"- Run: `{hybrid.get('run_id')}`\n- Objects: {len(objs)}; retained {admission.get('retained', 0)}; filtered {admission.get('filtered', 0)}\n"
        f"- Membership: assigned {membership.get('assigned', 0)}; unassigned {membership.get('unassigned', 0)}\n"
        f"- Functional room GT: unavailable\n- Viewer: `{args.viewer.resolve()}`\n\n"
        "The HTML report contains per-object evidence and all four A variants.\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output.resolve()), "manifest": str((args.output / 'static_report_manifest.json').resolve()), "viewer": str(args.viewer.resolve())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
