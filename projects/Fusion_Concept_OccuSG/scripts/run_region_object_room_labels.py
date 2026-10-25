#!/usr/bin/env python3
"""Replay cached M5 regions and M6 objects into explainable room-function labels.

This semantic sidecar never invokes perception, mapping, segmentation, GT, or
navmesh. It reads saved replay/trace artifacts and writes a new output folder.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from standalone_sg.config import load_config  # noqa: E402
from standalone_sg.contracts.regions import CanonicalRegion, Polygon2D  # noqa: E402
from standalone_sg.pipeline.contracts import GraphNode, SceneGraphSnapshot  # noqa: E402
from standalone_sg.rooms.membership import RegionMembershipResolver  # noqa: E402
from standalone_sg.semantics import RegionEvidence, RuleBasedRoomLabeler  # noqa: E402
from standalone_sg.semantics.adapters import evidence_from_m5_regions, evidence_from_m8_snapshot  # noqa: E402
from standalone_sg.serialization import load_snapshot  # noqa: E402


DEFAULT_LOCKED_RUN = ROOT / "artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001"
DEFAULT_CONFIG = ROOT / "configs/semantics/region_object_room_rules_replica_cad_v1.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _regions_from_trace(raw_regions: list[dict]) -> tuple[CanonicalRegion, ...]:
    result = []
    for item in raw_regions:
        polygon_data = item["polygon"]
        polygon = Polygon2D(
            tuple(tuple(float(value) for value in point) for point in polygon_data["outer"]),
            tuple(tuple(tuple(float(value) for value in point) for point in ring)
                  for ring in polygon_data.get("holes", [])),
        )
        result.append(CanonicalRegion(
            persistent_id=int(item["persistent_id"]), raw_id=item.get("raw_id", item["persistent_id"]),
            polygon=polygon,
            centroid_xy=tuple(float(value) for value in item["centroid_xy"]),
            area_m2=float(item["area_m2"]), timestamp_s=float(item["timestamp_s"]),
            source_grid_revision=int(item["source_grid_revision"]),
            adjacent_region_ids=tuple(item.get("adjacent_region_ids", ())),
            metadata=item.get("metadata", {}),
        ))
    return tuple(result)


def _object_nodes_for_frame(rows: list[dict], detections_by_id: dict[str, dict]) -> tuple[GraphNode, ...]:
    result = []
    for row in rows:
        detection_id = row.get("last_detection_id")
        # M6's ObjectNode geometry is first-observation geometry. Diagnostics
        # expose first/last detection IDs, not center_W; resolve center from
        # the first accepted Detection3D instead of silently fusing/replacing
        # it with a later viewpoint.
        geometry_detection = detections_by_id.get(row.get("first_detection_id")) or detections_by_id.get(detection_id)
        attrs = {
            "object_id": row["node_id"].removeprefix("object:"),
            "class_name": row.get("class_name"),
            "first_detection_id": row.get("first_detection_id"),
            "last_detection_id": detection_id,
        }
        if geometry_detection:
            attrs.update({
                "center_W": geometry_detection.get("center_W"),
                "confidence": geometry_detection.get("confidence"),
                "class_name": geometry_detection.get("class_name", attrs["class_name"]),
            })
        result.append(GraphNode(
            row["node_id"], "object", row.get("first_seen_s"), row.get("last_seen_s"), "cached_M6_diagnostics", attrs,
        ))
    return tuple(result)


def _trace_object_observations(diags, d3_by_id, d2_by_id, detection_2d_root: Path):
    """Build causal cumulative observation refs; frame N never sees frame N+1."""
    history: dict[str, list[dict]] = {}
    last_ids: dict[str, str] = {}
    by_frame = {}
    for diag in diags:
        frame_index = int(diag["frame_index"])
        frame_id = diag["frame_id"]
        for row in diag.get("object_nodes", []):
            object_id = row["node_id"]
            detection_id = row.get("last_detection_id")
            if not detection_id or last_ids.get(object_id) == detection_id:
                continue
            last_ids[object_id] = detection_id
            d3 = d3_by_id.get(detection_id)
            d2 = d2_by_id.get(detection_id, {})
            if d3 is None:
                continue
            mask_rel = d2.get("mask_file") or d3.get("provider_metadata", {}).get("mask_file")
            history.setdefault(object_id, []).append({
                "frame_id": frame_id, "frame_index": frame_index,
                "timestamp_s": float(diag["timestamp_s"]),
                "detection_id": detection_id,
                "class_raw": d3.get("provider_metadata", {}).get("class_raw", d3.get("class_name")),
                "class_canonical": d3.get("class_name"),
                "score_raw": d3.get("confidence"),
                "center_W": d3.get("center_W"),
                "bbox_xyxy": d2.get("bbox_xyxy") or d3.get("provider_metadata", {}).get("bbox_xyxy"),
                "mask_file": None if mask_rel is None else str((detection_2d_root / mask_rel).resolve()),
                "mask_sha256": d2.get("mask_sha256"),
                "mask_frame": "original_rgb_pixels",
                "provider_id": d3.get("provider_id"),
            })
        by_frame[frame_index] = {key: tuple(value) for key, value in history.items()}
    return by_frame


def _region_states_json(states):
    return [state.as_dict() for state in states]


def _render_mask_previews(output: Path, capture: Path, observations: dict[str, list[dict]]):
    preview_dir = output / "evidence_previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    rendered = 0
    for refs in observations.values():
        for item in refs:
            detection_id = item["detection_id"]
            if detection_id in seen or not item.get("mask_file"):
                continue
            seen.add(detection_id)
            try:
                frame_index = int(item["frame_index"])
                rgb = np.load(capture / "frames" / f"{frame_index:06d}" / "rgb.npy", allow_pickle=False)
                mask = np.load(item["mask_file"], allow_pickle=False).astype(bool)
                if rgb.dtype != np.uint8:
                    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
                if rgb.ndim == 3 and rgb.shape[2] == 4:
                    rgb = rgb[..., :3]
                if mask.shape != rgb.shape[:2]:
                    continue
                preview = rgb.copy()
                tint = np.asarray([255, 55, 45], dtype=np.uint8)
                preview[mask] = np.rint(0.45 * preview[mask].astype(np.float32) + 0.55 * tint).astype(np.uint8)
                name = re.sub(r"[^A-Za-z0-9_.-]", "_", detection_id) + ".png"
                Image.fromarray(preview, mode="RGB").save(preview_dir / name)
                item["preview"] = f"evidence_previews/{name}"
                rendered += 1
            except (OSError, ValueError, KeyError):
                continue
    return rendered


def _render_spatial_semantic_map(path: Path, grid_trace: dict, grid_file: Path,
                                 final_states, final_evidence, trajectory, object_nodes):
    arrays = np.load(grid_file, allow_pickle=False)
    values = arrays["values"]
    origin_x, origin_y = map(float, grid_trace["origin_xy"])
    resolution = float(grid_trace["resolution_m"])
    h, w = values.shape
    extent = [origin_x, origin_x + w * resolution, origin_y, origin_y + h * resolution]
    # M3 contract: row zero is world +Y; positive values are occupied,
    # zero is free, negative values are unknown. This is categorical, not prob.
    image = np.full(values.shape, 0, dtype=np.uint8)
    image[values == 0] = 1
    image[values > 0] = 2
    cmap = ListedColormap(["#9b9b9b", "#ffffff", "#161616"])
    fig, ax = plt.subplots(figsize=(10, 9))
    ax.imshow(image, cmap=cmap, vmin=0, vmax=2, origin="upper", extent=extent, interpolation="nearest", zorder=0)
    colors = {"predicted": "#00a878", "unknown": "#e0a526", "ambiguous": "#bf55a4", "stale": "#d62728"}
    evidence_by_id = {item.region_id: item for item in final_evidence}
    for state in final_states:
        evidence = evidence_by_id.get(state.region_id)
        if evidence is None or evidence.polygon is None:
            continue
        polygon = evidence.polygon
        color = colors.get(state.status, "#333333")
        xs, ys = zip(*(polygon.outer + (polygon.outer[0],)))
        ax.fill(xs, ys, color=color, alpha=0.12, zorder=2)
        ax.plot(xs, ys, color=color, linewidth=1.5 if state.status != "stale" else 1.8,
                linestyle="--" if state.status == "stale" else "-", zorder=3)
        cx = sum(x for x, _ in polygon.outer) / len(polygon.outer)
        cy = sum(y for _, y in polygon.outer) / len(polygon.outer)
        text = state.label if state.label is not None else state.status
        ax.text(cx, cy, f"{state.region_id}\n{text}", ha="center", va="center", fontsize=8,
                bbox={"facecolor": "white", "alpha": 0.78, "edgecolor": "none"}, zorder=6)
    if trajectory:
        ax.plot([p[0] for p in trajectory], [p[1] for p in trajectory], color="#0077ff", linewidth=1.5,
                label="captured base trajectory", zorder=4)
        ax.scatter([trajectory[0][0]], [trajectory[0][1]], marker="o", color="#2ca02c", s=25, zorder=5)
        ax.scatter([trajectory[-1][0]], [trajectory[-1][1]], marker="^", color="#ff3030", s=35, zorder=5)
    for node in object_nodes:
        center = node.attributes.get("center_W")
        if isinstance(center, (list, tuple)) and len(center) >= 2:
            ax.scatter([center[0]], [center[1]], s=24, edgecolors="black", linewidths=.5, color="#00bfc4", zorder=5)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("canonical world X (m)")
    ax.set_ylabel("canonical world Y (m)")
    ax.set_title("M3 occupancy + current M5 region object labels (not 3D map)")
    ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3])
    legend = [
        Patch(facecolor="#9b9b9b", label="M3 unknown"),
        Patch(facecolor="#ffffff", edgecolor="#888888", label="M3 free"),
        Patch(facecolor="#161616", label="M3 occupied"),
        Patch(facecolor="#00a878", alpha=.3, label="M5 predicted"),
        Patch(facecolor="#e0a526", alpha=.3, label="M5 unknown"),
        Patch(facecolor="#bf55a4", alpha=.3, label="M5 ambiguous"),
        Patch(facecolor="#d62728", alpha=.3, label="M5 stale (dashed)"),
        Line2D([0], [0], color="#0077ff", lw=1.8, label="actual base trajectory"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#00bfc4", markeredgecolor="black", markersize=6, label="M6 object center"),
    ]
    fig.legend(handles=legend, loc="lower center", bbox_to_anchor=(.5, .005), ncol=3, fontsize=8, frameon=True)
    fig.tight_layout(rect=[0, .07, 1, 1])
    fig.savefig(path, dpi=180)
    plt.close(fig)


def _render_component_overlays(output: Path, grid_trace: dict, grid_file: Path,
                               final_states, final_evidence, trajectory, object_nodes):
    """Export the occupancy base and separate region, object and label overlays."""
    arrays = np.load(grid_file, allow_pickle=False)
    values = arrays["values"]
    origin_x, origin_y = map(float, grid_trace["origin_xy"])
    resolution = float(grid_trace["resolution_m"])
    h, w = values.shape
    extent = [origin_x, origin_x + w * resolution, origin_y, origin_y + h * resolution]
    image = np.full(values.shape, 0, dtype=np.uint8)
    image[values == 0] = 1
    image[values > 0] = 2
    cmap = ListedColormap(["#9b9b9b", "#ffffff", "#161616"])
    colors = {"predicted": "#00a878", "unknown": "#e0a526", "ambiguous": "#bf55a4", "stale": "#d62728"}
    evidence_by_id = {item.region_id: item for item in final_evidence}

    def base(ax, title):
        ax.imshow(image, cmap=cmap, vmin=0, vmax=2, origin="upper", extent=extent, interpolation="nearest")
        ax.set_aspect("equal", adjustable="box")
        ax.set_xlim(extent[0], extent[1]); ax.set_ylim(extent[2], extent[3])
        ax.set_xlabel("canonical world X (m)"); ax.set_ylabel("canonical world Y (m)")
        ax.set_title(title)

    def save(name, draw):
        fig, ax = plt.subplots(figsize=(10, 9))
        base(ax, name.replace("_", " ").title())
        draw(ax)
        fig.tight_layout()
        fig.savefig(output / f"{name}.png", dpi=180)
        plt.close(fig)

    save("occupancy", lambda ax: None)

    def draw_regions(ax):
        for state in final_states:
            evidence = evidence_by_id.get(state.region_id)
            if evidence is None or evidence.polygon is None:
                continue
            xs, ys = zip(*(evidence.polygon.outer + (evidence.polygon.outer[0],)))
            color = colors.get(state.status, "#333333")
            ax.fill(xs, ys, color=color, alpha=.15)
            ax.plot(xs, ys, color=color, linewidth=1.7)
            ax.text(sum(xs[:-1]) / len(xs[:-1]), sum(ys[:-1]) / len(ys[:-1]), state.region_id,
                    ha="center", va="center", fontsize=8, bbox={"facecolor": "white", "alpha": .8, "edgecolor": "none"})
    save("region_overlay", draw_regions)

    def draw_objects(ax):
        if trajectory:
            ax.plot([p[0] for p in trajectory], [p[1] for p in trajectory], color="#0077ff", linewidth=1.5)
        for node in object_nodes:
            center = node.attributes.get("center_W")
            if isinstance(center, (list, tuple)) and len(center) >= 2:
                ax.scatter([center[0]], [center[1]], s=28, edgecolors="black", linewidths=.5, color="#00bfc4")
    save("object_overlay", draw_objects)

    def draw_labels(ax):
        draw_regions(ax)
        for state in final_states:
            evidence = evidence_by_id.get(state.region_id)
            if evidence is None or evidence.polygon is None:
                continue
            points = evidence.polygon.outer
            cx = sum(x for x, _ in points) / len(points)
            cy = sum(y for _, y in points) / len(points)
            ax.text(cx, cy, state.label or state.status, ha="center", va="center", fontsize=8,
                    bbox={"facecolor": "white", "alpha": .8, "edgecolor": "none"})
    save("label_overlay", draw_labels)


def _write_review_html(path: Path, labels: list[dict], history: list[dict], evidence_by_revision: dict[str, list[dict]],
                       observation_refs: dict[str, list[dict]], previews_count: int):
    cards = []
    for state in labels:
        region_evidence = evidence_by_revision.get(state["region_id"], [])
        members = region_evidence[-1].get("objects", []) if region_evidence else []
        details = []
        for obj in members:
            refs = observation_refs.get(obj["object_id"], [])
            thumbs = "".join(
                f'<figure><img loading="lazy" src="{html.escape(ref["preview"])}" alt="RGB with mask overlay">'
                f'<figcaption>{html.escape(ref["frame_id"])} · <span class="prediction">{html.escape(ref["class_raw"] or "class?")}</span> · '
                f'{html.escape(ref["detection_id"])}</figcaption></figure>'
                for ref in refs if ref.get("preview")
            )
            details.append(
                f'<details><summary><span class="prediction">{html.escape(str(obj["class_name"]))}</span> — '
                f'{html.escape(obj["object_id"])} · {len(refs)} cached views</summary>'
                f'<p class="prediction">membership={html.escape(obj["membership_reason"])}; '
                f'xy={html.escape(str(obj["position_xy_m"]))}; raw detector score={html.escape(str(obj["detector_score_raw"]))}</p>'
                f'<div class="thumbs">{thumbs or "<em>No cached RGB/mask preview</em>"}</div></details>'
            )
        candidate_rows = "".join(f"<li>{html.escape(k)}: {v:g}</li>" for k, v in state["candidates"])
        cards.append(
            f'<section class="region"><h2>{html.escape(state["region_id"])} — '
            f'<span class="prediction">{html.escape(state["label"] or state["status"])}</span></h2>'
            f'<p class="prediction">status={html.escape(state["status"])} · M5 geometry revision={state["geometry_revision"]} · '
            f'evidence revision={state["evidence_revision"]} · objects={len(members)} · '
            f'score kind={html.escape(state["score_kind"])}</p>'
            f'<p class="prediction">Reason: {html.escape(str(state["stale_reason"]))}; evidence hash {html.escape(state["evidence_hash"][:16])}</p>'
            f'<div class="prediction"><p>Candidate scores:</p><ul>{candidate_rows}</ul>'
            f'<p>Observed IDs: {html.escape(", ".join(state["observed_object_ids"]))}</p>'
            f'<p>Supporting IDs: {html.escape(", ".join(state["supporting_object_ids"]))}</p></div>'
            f'{"".join(details)}</section>'
        )
    history_rows = "".join(
        f'<tr><td>{h["frame_index"]}</td><td>{html.escape(h["frame_id"])}</td><td>{html.escape(str(h.get("region_id", h.get("object_id", "—"))))}</td>'
        f'<td>{html.escape(str(h.get("status", "—")))}</td><td>{html.escape(str(h.get("label", "—")))}</td>'
        f'<td>{html.escape(h["event"])}</td><td>{html.escape(str(h.get("evidence_revision", "—")))}</td></tr>'
        for h in history
    )
    document = f'''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Region-object evidence review</title><style>
body{{font:16px system-ui,sans-serif;margin:0;background:#f3f5f7;color:#17212b}}header{{position:sticky;top:0;background:#fff;padding:1rem 2rem;border-bottom:1px solid #ccd3da;z-index:2}}main{{max-width:1250px;margin:auto;padding:1rem}}.region{{background:#fff;border:1px solid #ccd3da;border-radius:8px;padding:1rem 1.3rem;margin:1rem 0}}.thumbs{{display:flex;gap:.8rem;flex-wrap:wrap}}figure{{margin:.3rem;width:min(320px,45vw)}}img{{width:100%;height:auto;border:1px solid #888}}figcaption{{font-size:.78rem;overflow-wrap:anywhere}}summary{{cursor:pointer;font-weight:600}}table{{border-collapse:collapse;background:#fff;width:100%}}td,th{{border:1px solid #ccd3da;padding:.35rem;text-align:left;font-size:.83rem}}.warning{{background:#fff3cf;padding:.8rem}}</style>
<header><h1>Region ↔ object semantic evidence review</h1><p>AI/system output for human review; this is not independent GT. Predictions can be hidden using the switch.</p>
<label><input id="hidepred" type="checkbox"> Hide semantic labels/scores (region geometry, object membership and masks remain visible)</label>
<p>Cached RGB/mask previews: {previews_count}; no scene names, room GT or navmesh used by inference.</p></header><main>
<p class="warning">Predicted means rule-based hypothesis, not verified room function. Repeated frame detections count once per persistent object ID. M5 geometry remains unchanged.</p>
{''.join(cards)}<h2>Region history and lifecycle events</h2><table><thead><tr><th>frame</th><th>frame ID</th><th>region</th><th>status</th><th>label</th><th>event</th><th>evidence rev</th></tr></thead><tbody>{history_rows}</tbody></table></main>
<script>document.getElementById('hidepred').addEventListener('change',e=>{{document.querySelectorAll('.prediction').forEach(n=>n.hidden=e.target.checked);document.querySelectorAll('.region h2 .prediction').forEach(n=>{{if(e.target.checked)n.dataset.saved=n.textContent,n.textContent='[hidden]';else if(n.dataset.saved)n.textContent=n.dataset.saved;}});}});</script></html>'''
    path.write_text(document, encoding="utf-8")


def run(replay: Path, trace: Path, detection_3d: Path, detection_2d: Path, config: Path, output: Path,
        capture: Path | None = None) -> dict:
    replay, trace, detection_3d, detection_2d, config, output = [p.resolve() for p in (replay, trace, detection_3d, detection_2d, config, output)]
    replay_manifest = read_json(replay / "manifest.json")
    capture = (capture or Path(replay_manifest.get("capture", replay.parent / "capture"))).resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty; use a new run directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    run_started = time.monotonic()
    snapshot_path = replay / "snapshot.json"
    snapshot_sha_before = sha256(snapshot_path)
    snapshot = load_snapshot(snapshot_path)
    diagnostics = read_json(replay / "frame_diagnostics.json")
    if not isinstance(diagnostics, list) or not diagnostics:
        raise ValueError("replay frame_diagnostics.json must contain a non-empty frame list")
    trace_frames = []
    for diag in diagnostics:
        sidecar = trace / "frames" / f"{int(diag['frame_index']):06d}.json"
        trace_frames.append(read_json(sidecar))
    d2_index = read_json(detection_2d / "detection_2d_index.json")
    d3_records = read_jsonl(detection_3d / "detection_3d.jsonl")
    if len(trace_frames) != len(diagnostics) or len(diagnostics) != int(d2_index["frame_count"]):
        raise ValueError("frame count mismatch between replay diagnostics, M5/M8 trace and cached 2D index")
    d3_by_id = {det["detection_id"]: det for batch in d3_records for det in batch.get("detections", [])}
    d2_by_id = {det["detection_id"]: det for frame in d2_index["frames"] for det in frame.get("detections", [])}
    d2_vocabulary_counts = {}
    for frame in d2_index["frames"]:
        for det in frame.get("detections", []):
            cls = det["class_canonical"]
            d2_vocabulary_counts[cls] = d2_vocabulary_counts.get(cls, 0) + 1
    d3_vocabulary_counts = {}
    for det in d3_by_id.values():
        cls = det["class_name"]
        d3_vocabulary_counts[cls] = d3_vocabulary_counts.get(cls, 0) + 1
    observations = _trace_object_observations(diagnostics, d3_by_id, d2_by_id, detection_2d)

    old_config_path = ROOT / "configs/semantics/room_rules_replica_cad_dev.json"
    old_labeler = RuleBasedRoomLabeler(read_json(old_config_path))
    new_config = read_json(config)
    labeler = RuleBasedRoomLabeler(new_config)
    p5_baseline_states = old_labeler.label_snapshot(snapshot)
    p5_locked_states = labeler.label_snapshot(snapshot)
    same_labels = [(s.label, s.status, s.candidates, s.observed_object_ids, s.supporting_object_ids) for s in p5_baseline_states] == [
        (s.label, s.status, s.candidates, s.observed_object_ids, s.supporting_object_ids) for s in p5_locked_states]
    if not same_labels:
        raise RuntimeError("versioned semantic profile changed valid P5 M8 outputs; baseline-equivalence assertion failed")

    source_config_path = ROOT / "configs/reference_code_mp3d.yaml"
    phase_config = load_config(source_config_path)
    resolver = RegionMembershipResolver(phase_config.rooms)
    prior_regions: dict[int, object] = {}
    last_valid_evidence = {}
    last_valid_labels = {}
    previous_ids: set[int] = set()
    previous_object_owner: dict[str, str | None] = {}
    frame_outputs = []
    history_rows = []
    all_membership_audit = []
    final_evidence = ()
    final_states = ()
    final_trace = trace_frames[-1]
    trajectory = []
    for diag, sidecar in zip(diagnostics, trace_frames):
        frame_id = diag["frame_id"]
        frame_index = int(diag["frame_index"])
        timestamp_s = float(diag["timestamp_s"])
        m5 = sidecar["m5"]
        accepted = bool(m5.get("accepted", False)) and not bool(m5.get("stale", False))
        current_regions = _regions_from_trace(m5.get("current_regions", []))
        current_ids = {int(r.persistent_id) for r in current_regions}
        previous_regions = prior_regions
        metadata_path = capture / "frames" / f"{frame_index:06d}" / "metadata.json"
        pose_context = {
            "base_translation_W": sidecar.get("base_pose_W"),
            "sensor_pose_W_Copt": sidecar.get("sensor_pose_W_Copt"),
            "pose_source": "saved replay trace; actual simulator state, not semantic GT",
        }
        if metadata_path.is_file():
            capture_meta = read_json(metadata_path)
            pose_context.update({
                "base_pose_W": capture_meta.get("base_pose_canonical"),
                "base_pose_source": capture_meta.get("base_pose_source"),
                "sensor_pose_source": capture_meta.get("sensor_pose_source"),
            })
        objects = _object_nodes_for_frame(diag.get("object_nodes", []), d3_by_id)
        observation_map = {node.node_id: observations.get(frame_index, {}).get(node.node_id, ()) for node in objects}
        rooms = []
        for room in sidecar.get("m8", {}).get("rooms", []):
            rooms.append(GraphNode(room["node_id"], "room", room.get("first_seen_s"), room.get("last_seen_s"), room.get("provenance", "M8"), room.get("attributes", {})))
        if accepted:
            evidence, membership_audit = evidence_from_m5_regions(
                current_regions, objects, membership_resolver=resolver,
                observation_revision=int(m5.get("accepted_observation_revision", 0)),
                frame_id=frame_id, pose_context=pose_context, observation_timestamp_s=timestamp_s,
                processing_timestamp_s=time.monotonic() - run_started,
                stale=False, room_nodes=rooms, observations=observation_map,
            )
            last_valid_evidence = {item.region_id: item for item in evidence}
            states = labeler.label_regions(evidence)
            last_valid_labels.update({state.region_id: state.label for state in states if state.status == "predicted"})
            disappeared = sorted(previous_ids - current_ids)
            created = sorted(current_ids - previous_ids)
            previous_ids = current_ids
        else:
            evidence = tuple(replace(item, freshness="stale", warnings=item.warnings + ("latest M5 observation rejected/stale; retained last accepted evidence only",))
                             for _, item in sorted(last_valid_evidence.items()))
            states = labeler.label_regions(evidence)
            membership_audit = ()
            disappeared = []
            created = []
        evidence_by_id = {item.region_id: item for item in evidence}
        for rid in disappeared:
            history_rows.append({
                "event": "region_removed", "frame_index": frame_index, "frame_id": frame_id,
                "timestamp_s": timestamp_s, "region_id": f"region:{rid}", "status": "removed",
                "label": None, "retained_last_valid_label": last_valid_labels.get(f"region:{rid}"),
                "geometry_revision": previous_regions.get(rid).source_grid_revision if rid in previous_regions else None,
                "evidence_revision": int(m5.get("accepted_observation_revision", 0)),
            })
        if accepted:
            prior_regions = {int(r.persistent_id): r for r in current_regions}
        for rid in created:
            history_rows.append({
                "event": "region_added", "frame_index": frame_index, "frame_id": frame_id,
                "timestamp_s": timestamp_s, "region_id": f"region:{rid}", "status": "created",
                "label": None, "evidence_revision": int(m5.get("accepted_observation_revision", 0)),
            })
        if accepted:
            current_object_owner = {item["object_id"]: item["region_id"] for item in membership_audit}
            for object_id in sorted(set(previous_object_owner) | set(current_object_owner)):
                if previous_object_owner.get(object_id) != current_object_owner.get(object_id) and (object_id in previous_object_owner or object_id in current_object_owner):
                    history_rows.append({
                        "event": "object_membership_changed", "frame_index": frame_index, "frame_id": frame_id,
                        "timestamp_s": timestamp_s, "object_id": object_id,
                        "from_region_id": previous_object_owner.get(object_id),
                        "to_region_id": current_object_owner.get(object_id),
                        "evidence_revision": int(m5.get("accepted_observation_revision", 0)),
                    })
            previous_object_owner = current_object_owner
        for state in states:
            state_data = state.as_dict()
            state_data["retained_last_valid_label"] = last_valid_labels.get(state.region_id) if state.status == "stale" else state.label
            state_data["frame_id"] = frame_id
            state_data["frame_index"] = frame_index
            state_data["observation_timestamp_s"] = timestamp_s
            state_data["m5_accepted"] = accepted
            state_data["m4_backend_branch"] = diag.get("m4_backend_branch")
            state_data["m4_reason"] = diag.get("m4_reason")
            state_data["event"] = "semantic_update" if accepted else "stale_reuse"
            frame_outputs.append(state_data)
        all_membership_audit.extend({**item, "frame_id": frame_id, "frame_index": frame_index} for item in membership_audit)
        trajectory.append(tuple(float(v) for v in sidecar["base_pose_W"][:2]))
        final_evidence, final_states = evidence, states

    # S0 baseline is the locked final M8 snapshot. S1/S4 final view labels
    # every current M5 region; the mapping and segmentation are untouched.
    baseline_rows = _region_states_json(p5_baseline_states)
    versioned_rows = _region_states_json(p5_locked_states)
    final_rows = []
    final_evidence_map = {item.region_id: item for item in final_evidence}
    for state in final_states:
        row = state.as_dict()
        row["retained_last_valid_label"] = last_valid_labels.get(state.region_id) if state.status == "stale" else state.label
        final_rows.append(row)
    (output / "p5_m8_baseline.json").write_text(json.dumps(baseline_rows, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (output / "p5_m8_versioned_profile.json").write_text(json.dumps(versioned_rows, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (output / "region_evidence.jsonl").write_text("".join(json.dumps(item.as_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for item in final_evidence), encoding="utf-8")
    (output / "region_labels.json").write_text(json.dumps(final_rows, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    (output / "region_labels_history.jsonl").write_text("".join(json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for item in frame_outputs), encoding="utf-8")
    (output / "region_lifecycle_events.jsonl").write_text("".join(json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for item in history_rows), encoding="utf-8")
    (output / "membership_audit.jsonl").write_text("".join(json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for item in all_membership_audit), encoding="utf-8")
    (output / "room_label_config.json").write_text(json.dumps(new_config, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    evidence_readback = [RegionEvidence.from_dict(json.loads(line)).as_dict()
                         for line in (output / "region_evidence.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    labels_readback = read_json(output / "region_labels.json")
    export_reload_equal = evidence_readback == [item.as_dict() for item in final_evidence] and labels_readback == final_rows
    if not export_reload_equal:
        raise RuntimeError("semantic evidence/label export reload did not preserve the generated records")
    with (output / "region_object_evidence.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["region_id", "source_room_id", "object_id", "class_name", "position_x_m", "position_y_m", "detector_score_raw", "membership_reason", "first_detection_id", "last_detection_id", "observation_frame_ids", "mask_files"])
        writer.writeheader()
        for item in final_evidence:
            for obj in item.objects:
                writer.writerow({
                    "region_id": item.region_id, "source_room_id": item.source_room_id,
                    "object_id": obj.object_id, "class_name": obj.class_name,
                    "position_x_m": None if obj.position_xy_m is None else obj.position_xy_m[0],
                    "position_y_m": None if obj.position_xy_m is None else obj.position_xy_m[1],
                    "detector_score_raw": obj.detector_score_raw, "membership_reason": obj.membership_reason,
                    "first_detection_id": obj.first_detection_id, "last_detection_id": obj.last_detection_id,
                    "observation_frame_ids": ";".join(str(ref.get("frame_id")) for ref in obj.observations),
                    "mask_files": ";".join(str(ref.get("mask_file")) for ref in obj.observations if ref.get("mask_file")),
                })

    # Save final reference grid from the already-recorded M3 output, with no
    # extra call into MapConverter or any stateful projection.
    final_grid_path = trace / "frames" / f"{int(final_trace['frame_index']):06d}.npz"
    final_objects = _object_nodes_for_frame(diagnostics[-1].get("object_nodes", []), d3_by_id)
    with (output / "actual_trajectory.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frame_index", "frame_id", "simulation_timestamp_s", "base_x_m", "base_y_m", "base_z_m", "yaw_rad"])
        for diag, sidecar in zip(diagnostics, trace_frames):
            metadata_path = capture / "frames" / f"{int(diag['frame_index']):06d}" / "metadata.json"
            capture_meta = read_json(metadata_path) if metadata_path.is_file() else {}
            matrix = capture_meta.get("base_pose_canonical")
            if matrix and len(matrix) >= 3:
                x, y, z = (float(matrix[i][3]) for i in range(3))
                yaw = math.atan2(float(matrix[1][0]), float(matrix[0][0]))
            else:
                x, y, z = map(float, sidecar["base_pose_W"][:3])
                yaw = ""
            writer.writerow([diag["frame_index"], diag["frame_id"], diag["timestamp_s"], x, y, z, yaw])
    _render_spatial_semantic_map(output / "spatial_semantic_map.png", final_trace, final_grid_path,
                                 final_states, final_evidence, trajectory, final_objects)
    _render_component_overlays(output, final_trace, final_grid_path,
                               final_states, final_evidence, trajectory, final_objects)
    final_observations = observations.get(int(diagnostics[-1]["frame_index"]), {})
    preview_count = _render_mask_previews(output, capture, final_observations)
    _write_review_html(output / "region_object_evidence.html", final_rows, frame_outputs + history_rows,
                       {key: [item.as_dict()] for key, item in final_evidence_map.items()}, final_observations, preview_count)

    # Evaluation is intentionally a coverage/abstention report until functional
    # room labels are independently human-confirmed; boundary GT is not used.
    counts = {}
    for state in final_states:
        counts[state.status] = counts.get(state.status, 0) + 1
    label_counts = {}
    for state in final_states:
        if state.label:
            label_counts[state.label] = label_counts.get(state.label, 0) + 1
    m8_owner = {edge.target_id: edge.source_id for edge in snapshot.edges if edge.relation == "room_contains" and edge.target_id.startswith("object:")}
    anchor_to_room = {int(node.attributes["anchor_region_id"]): node.node_id for node in snapshot.nodes
                      if node.kind == "room" and node.attributes.get("anchor_region_id") is not None}
    final_m5_owner = {item["object_id"]: item["region_id"] for item in all_membership_audit if item["frame_index"] == int(diagnostics[-1]["frame_index"])}
    membership_differences = []
    for object_id in sorted(set(m8_owner) | set(final_m5_owner)):
        m5_region = final_m5_owner.get(object_id)
        m5_room = None if m5_region is None else anchor_to_room.get(int(m5_region.split(":")[1]))
        if m5_room != m8_owner.get(object_id):
            membership_differences.append({"object_id": object_id, "m5_resolver_room": m5_room, "m8_room_contains": m8_owner.get(object_id)})
    evaluation = {
        "schema_version": 1, "status": "technical_pass_no_functional_room_GT",
        "final_m5_region_count": len(final_states), "status_counts": counts, "predicted_label_counts": label_counts,
        "prediction_coverage": (counts.get("predicted", 0) / len(final_states)) if final_states else 0.0,
        "functional_room_accuracy": None, "functional_room_gt_available": False,
        "semantic_export_reload_equal": export_reload_equal,
        "review_state": "not_human_verified", "boundary_gt_used_for_inference": False,
        "room_gt_used_for_inference": False,
        "history_rows": len(frame_outputs), "region_removal_tombstones": sum(row["event"] == "region_removed" for row in history_rows),
        "region_creation_events": sum(row["event"] == "region_added" for row in history_rows),
        "object_membership_change_events": sum(row["event"] == "object_membership_changed" for row in history_rows),
        "stale_reuse_rows": sum(row["event"] == "stale_reuse" for row in frame_outputs),
        "membership_outside_or_unassigned": sum(item["region_id"] is None for item in all_membership_audit),
        "m5_m8_final_membership_differences": membership_differences,
        "m8_baseline_label_counts": {status: sum(s.status == status for s in p5_baseline_states) for status in ("predicted", "unknown", "ambiguous", "stale")},
        "versioned_profile_preserves_p5_m8_outputs": same_labels,
        "limitations": [
            "functional labels are uncalibrated P5 rule hypotheses, not accuracy claims",
            "room-function annotations have not been human-confirmed; architectural boundary GT is not functional room GT",
            "membership and geometry inherit the locked M5/M6 and GT-pose replay",
            f"observed detector vocabulary is limited to classes present in this {snapshot.scene_id} cache",
            "score values are additive evidence, not probabilities",
        ],
    }
    (output / "evaluation.json").write_text(json.dumps(evaluation, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")

    alias_map = dict(new_config.get("class_aliases", {}))
    supported_vocab = {cls for mapping in new_config["labels"].values() for cls in mapping}
    all_vocab = sorted(set(d2_vocabulary_counts) | set(d3_vocabulary_counts) | supported_vocab | set(alias_map))
    vocabulary_inventory = {
        "source": "locked detection_2d_index and accepted detection_3d cache; counts are observations, not recall",
        "aliases": alias_map,
        "classes": [{
            "class_name": cls,
            "canonical_class": alias_map.get(cls, cls),
            "detection_2d_observations": d2_vocabulary_counts.get(cls, 0),
            "accepted_detection_3d_observations": d3_vocabulary_counts.get(cls, 0),
            "supported_by_room_rules": alias_map.get(cls, cls) in supported_vocab,
        } for cls in all_vocab],
        "unsupported_2d_classes": sorted(cls for cls in d2_vocabulary_counts if alias_map.get(cls, cls) not in supported_vocab),
        "supported_classes_not_seen_in_2d_cache": sorted(cls for cls in supported_vocab if cls not in d2_vocabulary_counts and cls not in alias_map.values()),
        "note": "2D counts are detector observations; P3 accepted 3D counts are successful lifts only. Neither is GT recall/accuracy.",
    }
    (output / "detector_vocabulary_inventory.json").write_text(json.dumps(vocabulary_inventory, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")

    trace_jsons = sorted((trace / "frames").glob("*.json"))
    file_inputs = [replay / "manifest.json", snapshot_path, replay / "frame_diagnostics.json",
                   trace_jsons[0], trace_jsons[-1],
                   detection_3d / "detection_3d.jsonl", detection_2d / "detection_2d_index.json", config, old_config_path, source_config_path]
    capture_manifest = capture / "capture_manifest.json"
    if capture_manifest.is_file():
        file_inputs.append(capture_manifest)
    file_inputs.extend(sorted((trace / "frames").glob("*.json")))
    file_inputs.extend(sorted((trace / "frames").glob("*.npz")))
    file_inputs.extend([Path(__file__).resolve(), Path(__file__).resolve().parents[1] / "src/standalone_sg/semantics/evidence.py",
                        Path(__file__).resolve().parents[1] / "src/standalone_sg/semantics/adapters.py",
                        Path(__file__).resolve().parents[1] / "src/standalone_sg/semantics/room_labeler.py"])
    hashes = {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): sha256(path) for path in file_inputs}
    manifest = {
        "schema_version": 1, "kind": "region_object_room_semantics_replay", "status": "pass",
        "source_run": str(replay.parent), "scene_id": snapshot.scene_id, "sequence_id": snapshot.sequence_id,
        "frames": len(diagnostics), "regions_final": len(final_states),
        "accepted_detection_3d_observations": len(d3_by_id),
        "persistent_object_nodes_final": len(diagnostics[-1].get("object_nodes", [])),
        "detection_2d_count": int(d2_index["detection_count"]), "detector_weights_sha256": d2_index["weights_sha256"],
        "detector_config_sha256": d2_index["config_sha256"], "p3_cache_sha256": sha256(detection_3d / "detection_3d.jsonl"),
        "detector_vocabulary_inventory": "detector_vocabulary_inventory.json",
        "pipeline_config_sha256": read_json(replay / "manifest.json")["config_sha256"],
        "m2_m3_binary_sha256": read_json(replay / "manifest.json")["m2_m3_binary_sha256"],
        "dude_binary_sha256": read_json(replay / "manifest.json")["dude_binary_sha256"],
        "source_segmentation_profile": "source; unchanged; only saved M5 trace consumed",
        "effective_m6_config": read_json(replay / "manifest.json")["effective_runtime_config"]["objects.association"],
        "semantic_config_sha256": sha256(config), "prior_p5_config_sha256": sha256(old_config_path),
        "inputs_sha256": hashes, "m8_snapshot_sha256_before": snapshot_sha_before,
        "m8_snapshot_sha256_after": sha256(snapshot_path), "m8_snapshot_unchanged": snapshot_sha_before == sha256(snapshot_path),
        "source_aligned": ["M5 region geometry", "existing M8 RegionMembershipResolver", "unique persistent object IDs", "OccuSG-like set signature (class, rounded XY)"],
        "project_extension": ["candidate score room-function rules", "predicted/unknown/ambiguous room-function labels", "region-level lifecycle sidecar", "visual evidence review page"],
        "forbidden_inference_inputs_used": {"room_gt": False, "scene_asset_names": False, "navmesh": False, "semantic_gt": False},
        "output_files": ["manifest.json", "p5_m8_baseline.json", "p5_m8_versioned_profile.json", "region_evidence.jsonl", "region_object_evidence.csv", "region_labels.json", "region_labels_history.jsonl", "region_lifecycle_events.jsonl", "actual_trajectory.csv", "membership_audit.jsonl", "room_label_config.json", "detector_vocabulary_inventory.json", "spatial_semantic_map.png", "occupancy.png", "region_overlay.png", "object_overlay.png", "label_overlay.png", "region_object_evidence.html", "evidence_previews/", "evaluation.json", "report.vi.md"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    report = f'''# Region–object room labels — apt_0\n\n- Trạng thái: kỹ thuật pass; **chưa có functional room GT được người dùng xác nhận**, vì vậy không có accuracy.\n- Input khóa: `{replay.parent.name}`; {len(diagnostics)} frame; {len(d3_by_id)} Detection3D cache observations; {len(final_states)} M5 regions ở frame cuối.\n- P5 M8 baseline và profile versioned giữ nguyên nhãn/score/candidate/IDs: **{same_labels}**.\n- Kết quả M5 cuối: {json.dumps(counts, ensure_ascii=False)}; label counts: {json.dumps(label_counts, ensure_ascii=False)}.\n- Detections lặp được gộp theo persistent ObjectNode ID trước khi đếm; segmentation và geometry không bị sửa.\n- Snapshot không đổi: `{snapshot_sha_before}`.\n\n## Ranh giới nguồn và extension\n\n- Source-aligned: M5 region IDs/geometries; object-to-region assignment bằng `RegionMembershipResolver` dùng trong M8; unique object ID; semantic signature dạng set `(class, round(x,1), round(y,1))`.\n- Extension đồ án: rule-based function labels (`living_room`, `dining_room`, `kitchen`, `bathroom`, `bedroom`), scores additive chưa calibration, abstention states, lifecycle history và review UI. Không gọi đây là OccuSG source parity cho classifier công năng.\n\n## Giới hạn\n\nGT pose/route thuộc Habitat replay; object classes chỉ có vocabulary quan sát trong cache này; boundary GT không được dùng để suy functional label; prediction chưa human-verified. Room geometry, detector/P3/M6, segmentation profile `source` và pipeline snapshot được giữ nguyên.\n'''
    (output / "report.vi.md").write_text(report, encoding="utf-8")
    return {**manifest, "evaluation": evaluation}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, default=DEFAULT_LOCKED_RUN / "replay")
    parser.add_argument("--trace", type=Path, default=DEFAULT_LOCKED_RUN / "grid_trace")
    parser.add_argument("--detection-3d", type=Path, default=DEFAULT_LOCKED_RUN / "detection_3d")
    parser.add_argument("--detection-2d", type=Path, default=DEFAULT_LOCKED_RUN / "detection_2d")
    parser.add_argument("--capture", type=Path, default=None,
                        help="raw RGB-D capture directory; defaults to the capture recorded in replay/manifest.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.replay, args.trace, args.detection_3d, args.detection_2d, args.config, args.output, args.capture), indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
