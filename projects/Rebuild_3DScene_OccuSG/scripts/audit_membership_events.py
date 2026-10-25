#!/usr/bin/env python3
"""Audit dining-table membership changes and prepare review sheets.

This is an evaluation-only tool. It reconstructs the committed graph state
from the replay delta log and reads the lossless M3 grid trace. It never feeds
annotations, scene geometry, or review results back into the pipeline.
"""
from __future__ import annotations

import argparse
import csv
import copy
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
from PIL import Image, ImageDraw


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def edge_key(edge: dict) -> tuple:
    return (
        edge.get("source_id"),
        edge.get("target_id"),
        edge.get("relation"),
        bool(edge.get("directed", False)),
        edge.get("provenance"),
    )


def edge_from_key(key: tuple) -> dict:
    return {
        "source_id": key[0],
        "target_id": key[1],
        "relation": key[2],
        "directed": key[3],
        "provenance": key[4],
    }


def reconstruct_states(replay: Path) -> tuple[dict[int, dict], dict]:
    deltas = load_json(replay / "delta_log.json")["deltas"]
    nodes: dict[str, dict] = {}
    edges: set[tuple] = set()
    states: dict[int, dict] = {}
    terminal = None
    for delta in deltas:
        for node in delta.get("added_nodes", []) + delta.get("updated_nodes", []):
            nodes[node["node_id"]] = copy.deepcopy(node)
        for node_id in delta.get("removed_node_ids", []):
            nodes.pop(node_id, None)
        for edge in delta.get("removed_edges", []):
            edges.discard(edge_key(edge))
        for edge in delta.get("added_edges", []):
            edges.add(edge_key(edge))
        frame_index = delta.get("diagnostics", {}).get("frame_index")
        if frame_index is None:
            terminal = copy.deepcopy(delta)
            continue
        states[int(frame_index)] = {
            "revision": int(delta["new_revision"]),
            "timestamp_s": float(delta["timestamp_s"]),
            "nodes": copy.deepcopy(nodes),
            "edges": set(edges),
            "delta": copy.deepcopy(delta),
        }
    return states, terminal


def rooms_for_object(state: dict, object_id: str) -> list[str]:
    return sorted(
        key[0]
        for key in state["edges"]
        if key[2] == "room_contains" and key[1] == object_id
    )


def node_at(state: dict, node_id: str) -> dict | None:
    return state["nodes"].get(node_id)


def sha_json(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def grid_for_frame(grid_trace: Path, frame_index: int) -> tuple[dict, np.ndarray]:
    index = load_json(grid_trace / "index.json")
    entry = next(item for item in index["frames"] if int(item["frame_index"]) == frame_index)
    meta = load_json(grid_trace / entry["metadata_file"])
    with np.load(grid_trace / entry["array_file"], allow_pickle=False) as arrays:
        return meta, arrays["values"].copy()


def grid_extent(meta: dict, values: np.ndarray) -> tuple[float, float, float, float]:
    ox, oy = meta["origin_xy"]
    resolution = float(meta["resolution_m"])
    return ox, ox + values.shape[1] * resolution, oy, oy + values.shape[0] * resolution


def grid_rgba(values: np.ndarray) -> np.ndarray:
    rgba = np.empty((*values.shape, 4), dtype=float)
    rgba[values < 0] = (0.63, 0.63, 0.63, 1.0)
    rgba[values == 0] = (1.0, 1.0, 1.0, 1.0)
    rgba[values > 0] = (0.05, 0.05, 0.05, 1.0)
    return rgba


def room_ring(state: dict, room_id: str):
    room = node_at(state, room_id)
    if not room:
        return None
    ring = room.get("attributes", {}).get("outer_ring", [])
    return np.asarray(ring, dtype=float) if ring else None


def event_rows(states: dict[int, dict], object_id: str, frame_diagnostics: dict[int, dict], detection_ledger: dict[str, dict]) -> list[dict]:
    frames = sorted(states)
    memberships = {frame: rooms_for_object(states[frame], object_id) for frame in frames}
    rows = []
    previous_frame = None
    previous_membership = None
    previous_present = False
    for frame in frames:
        membership = memberships[frame]
        present = object_id in states[frame]["nodes"]
        if present and not previous_present and membership:
            event_type = "initial_membership"
        elif present and previous_present and membership != previous_membership:
            event_type = "membership_changed"
        else:
            event_type = None
        if event_type is not None:
            previous_state = states[previous_frame]
            current_state = states[frame]
            previous_node = node_at(previous_state, object_id) or {}
            current_node = node_at(current_state, object_id) or {}
            previous_attrs = previous_node.get("attributes", {})
            current_attrs = current_node.get("attributes", {})
            previous_rooms = {room_id: node_at(previous_state, room_id) for room_id in previous_membership}
            current_rooms = {room_id: node_at(current_state, room_id) for room_id in membership}
            previous_rings = {room_id: (previous_rooms[room_id] or {}).get("attributes", {}).get("outer_ring", []) for room_id in previous_rooms}
            current_rings = {room_id: (current_rooms[room_id] or {}).get("attributes", {}).get("outer_ring", []) for room_id in current_rooms}
            previous_center = previous_attrs.get("center_W")
            current_center = current_attrs.get("center_W")
            center_delta = None
            if previous_center and current_center:
                center_delta = float(np.linalg.norm(np.asarray(current_center, dtype=float) - np.asarray(previous_center, dtype=float)))
            previous_detection = detection_ledger.get(previous_attrs.get("last_detection_id"), {})
            current_detection = detection_ledger.get(current_attrs.get("last_detection_id"), {})
            rows.append({
                "event_type": event_type,
                "object_id": object_id,
                "class_name": current_attrs.get("class_name"),
                "previous_frame": previous_frame,
                "event_frame": frame,
                "previous_revision": previous_state["revision"],
                "event_revision": current_state["revision"],
                "previous_timestamp_s": previous_state["timestamp_s"],
                "event_timestamp_s": current_state["timestamp_s"],
                "previous_room_ids": previous_membership,
                "event_room_ids": membership,
                "added_room_ids": sorted(set(membership) - set(previous_membership)),
                "removed_room_ids": sorted(set(previous_membership) - set(membership)),
                "previous_anchor_region_ids": {room_id: (node_at(previous_state, room_id) or {}).get("attributes", {}).get("anchor_region_id") for room_id in previous_membership},
                "event_anchor_region_ids": {room_id: (node_at(current_state, room_id) or {}).get("attributes", {}).get("anchor_region_id") for room_id in membership},
                "previous_center_W": previous_center,
                "event_center_W": current_center,
                "center_delta_m": center_delta,
                "previous_extents_m": previous_attrs.get("extents_m"),
                "event_extents_m": current_attrs.get("extents_m"),
                "center_changed": center_delta is not None and center_delta > 1e-6,
                "extents_changed": previous_attrs.get("extents_m") != current_attrs.get("extents_m"),
                "previous_last_detection_id": previous_attrs.get("last_detection_id"),
                "event_last_detection_id": current_attrs.get("last_detection_id"),
                "new_detection_at_event": previous_attrs.get("last_detection_id") != current_attrs.get("last_detection_id"),
                "previous_detection_center_W": previous_detection.get("center_W"),
                "event_detection_center_W": current_detection.get("center_W"),
                "previous_detection_extents_m": previous_detection.get("extents_m"),
                "event_detection_extents_m": current_detection.get("extents_m"),
                "detection_geometry_available": bool(previous_detection or current_detection),
                "previous_room_footprint_sha256": {room_id: sha_json(previous_rings[room_id]) for room_id in previous_rings},
                "event_room_footprint_sha256": {room_id: sha_json(current_rings[room_id]) for room_id in current_rings},
                "m4_backend_branch": frame_diagnostics.get(frame, {}).get("m4_backend_branch"),
                "m4_backend_status": frame_diagnostics.get(frame, {}).get("m4_backend_status"),
                "m4_region_count": frame_diagnostics.get(frame, {}).get("m4_region_count"),
                "m4_guard_accepted": frame_diagnostics.get(frame, {}).get("m4_guard_accepted"),
                "m4_stale": frame_diagnostics.get(frame, {}).get("m4_stale"),
                "delta_events": current_state["delta"].get("events", []),
                "removed_node_ids": current_state["delta"].get("removed_node_ids", []),
                "rooms_changed_in_delta": [
                    node["node_id"] for node in current_state["delta"].get("updated_nodes", []) if node.get("kind") == "room"
                ],
                "interpretation": "needs_independent_annotation",
            })
        previous_frame = frame
        previous_membership = membership
        previous_present = present
    return rows


def all_room_rings(states: dict[int, dict], frame_index: int):
    state = states[frame_index]
    return [
        (node_id, node.get("attributes", {}).get("outer_ring", []))
        for node_id, node in state["nodes"].items()
        if node.get("kind") == "room" and node.get("attributes", {}).get("outer_ring")
    ]


def render_event_overlay(states: dict[int, dict], grid_trace: Path, rows: list[dict], output: Path) -> None:
    panels = []
    for row in rows:
        panels.extend([(row["object_id"], row["previous_frame"], "before"), (row["object_id"], row["event_frame"], "after")])
    bounds = []
    loaded = {}
    for _, frame, _ in panels:
        meta, values = grid_for_frame(grid_trace, frame)
        loaded[frame] = (meta, values)
        bounds.append(grid_extent(meta, values))
    xmin = min(x[0] for x in bounds); xmax = max(x[1] for x in bounds)
    ymin = min(x[2] for x in bounds); ymax = max(x[3] for x in bounds)
    fig, axes = plt.subplots(2, max(1, len(rows)), figsize=(6 * max(1, len(rows)), 9), squeeze=False, constrained_layout=True)
    for col, row in enumerate(rows):
        for row_index, (frame, label) in enumerate(((row["previous_frame"], "before"), (row["event_frame"], "after"))):
            ax = axes[row_index][col]
            meta, values = loaded[frame]
            ax.imshow(grid_rgba(values), origin="lower", extent=grid_extent(meta, values), interpolation="nearest")
            state = states[frame]
            for room_id, ring in all_room_rings(states, frame):
                arr = np.asarray(ring, dtype=float)
                if arr.size:
                    closed = np.vstack((arr, arr[0]))
                    selected = room_id in set(row["previous_room_ids"] + row["event_room_ids"])
                    ax.plot(closed[:, 0], closed[:, 1], color="#00a878" if selected else "#777777", linewidth=2.0 if selected else 0.8, linestyle="--", alpha=0.9 if selected else 0.35)
            node = node_at(state, row["object_id"])
            attrs = (node or {}).get("attributes", {})
            center = attrs.get("center_W")
            if center:
                cx, cy = center[:2]
                ax.scatter([cx], [cy], color="#ff7f00", edgecolor="black", s=80, zorder=5)
                ext = attrs.get("extents_m")
                if ext and len(ext) >= 2:
                    ax.add_patch(Rectangle((cx - ext[0] / 2, cy - ext[1] / 2), ext[0], ext[1], fill=False, edgecolor="#ff7f00", linestyle=":", linewidth=1.5, zorder=5))
                ax.annotate(row["object_id"].replace("object:", ""), (cx, cy), xytext=(5, 5), textcoords="offset points", fontsize=8)
            ax.set_xlim(xmin, xmax); ax.set_ylim(ymin, ymax); ax.set_aspect("equal", adjustable="box")
            ax.set_title(f"{row['object_id'].replace('object:', '')} — {label}\nframe {frame}, rooms={rooms_for_object(state, row['object_id'])}", fontsize=9)
            ax.set_xlabel("world X (m)"); ax.set_ylabel("world Y (m)")
            ax.grid(alpha=0.15)
    fig.suptitle("Dining-table membership events — same world/grid scale", fontsize=14)
    fig.savefig(output, dpi=150, facecolor="white")
    plt.close(fig)


def make_mask_review_image(rgb_path: Path, mask_path: Path, title: str, output_size=(320, 240)) -> Image.Image:
    rgb = np.asarray(np.load(rgb_path, allow_pickle=False), dtype=np.uint8)
    if rgb.shape[-1] == 4:
        rgb = rgb[..., :3]
    mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
    image = Image.fromarray(rgb, mode="RGB").convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_array = np.zeros((*mask.shape, 4), dtype=np.uint8)
    overlay_array[mask] = (255, 80, 0, 105)
    image = Image.alpha_composite(image, Image.fromarray(overlay_array, mode="RGBA"))
    image = image.convert("RGB").resize(output_size)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, output_size[0] - 1, 28), fill=(0, 0, 0))
    draw.text((4, 6), title[:60], fill=(255, 255, 255))
    return image


def build_detection_maps(run: Path) -> tuple[dict, dict]:
    d2 = load_json(run / "detection_2d" / "detection_2d_index.json")
    by_id = {}
    for frame in d2.get("frames", []):
        for detection in frame.get("detections", []):
            by_id[detection["detection_id"]] = {**detection, "frame_id": frame["frame_id"], "frame_index": int(frame["frame_index"])}
    ledger = [json.loads(line) for line in (run / "detection_3d" / "object_evidence_ledger.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return by_id, {row["detection_id"]: row for row in ledger}


def render_detection_review(run: Path, object_rows: list[dict], output: Path) -> list[dict]:
    by_id, ledger = build_detection_maps(run)
    selected_ids = []
    seen = set()
    accepted_ids = []
    for row in object_rows:
        for evidence in row.get("evidence", []):
            did = evidence["detection_id"]
            if did not in accepted_ids and ledger.get(did, {}).get("decision") == "accepted":
                accepted_ids.append(did)
    accepted_ids.extend(row["detection_id"] for row in ledger.values() if row.get("decision") == "accepted" and row["detection_id"] not in accepted_ids)
    rejected_ids = [row["detection_id"] for row in ledger.values() if row.get("decision") != "accepted"]
    selected_ids = accepted_ids[:8] + rejected_ids[:8]
    cards = []
    rows = []
    d2_root = run / "detection_2d"
    capture = run / "capture" / "frames"
    for did in selected_ids:
        p3 = ledger[did]
        d2 = by_id.get(did, {})
        mask_rel = d2.get("mask_file") or p3.get("mask_file")
        if not mask_rel:
            continue
        rgb_path = capture / f"{int(p3['frame_index']):06d}" / "rgb.npy"
        mask_path = d2_root / mask_rel
        if not rgb_path.is_file() or not mask_path.is_file():
            continue
        title = f"{p3['decision']} {p3['class_canonical']} f{p3['frame_index']} n={p3.get('largest_cluster_points')}"
        cards.append(make_mask_review_image(rgb_path, mask_path, title))
        rows.append({
            "detection_id": did,
            "frame_index": p3.get("frame_index"),
            "class_canonical": p3.get("class_canonical"),
            "decision": p3.get("decision"),
            "reason": p3.get("reason"),
            "mask_pixel_count": p3.get("mask_pixel_count"),
            "valid_depth_fraction": p3.get("valid_depth_fraction"),
            "cluster_count": p3.get("cluster_count"),
            "largest_cluster_points": p3.get("largest_cluster_points"),
            "review_true_object": "",
            "review_should_accept_3d": "",
            "review_notes": "",
        })
    cols = 4
    rows_count = math.ceil(len(cards) / cols) if cards else 1
    sheet = Image.new("RGB", (cols * 320, rows_count * 240), (230, 230, 230))
    for index, card in enumerate(cards):
        sheet.paste(card, ((index % cols) * 320, (index // cols) * 240))
    sheet.save(output)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run = args.run.resolve(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    states, terminal = reconstruct_states(run / "replay")
    frame_diagnostics = {
        int(item["frame_index"]): item
        for item in load_json(run / "replay" / "frame_diagnostics.json")
    }
    detection_ledger = {
        row["detection_id"]: row
        for row in (
            json.loads(line)
            for line in (run / "detection_3d" / "object_evidence_ledger.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    final_snapshot = load_json(run / "semantic" / "snapshot.json")
    final_snapshot = final_snapshot.get("snapshot", final_snapshot)
    targets = [
        node["node_id"] for node in final_snapshot.get("nodes", [])
        if node.get("kind") == "object" and node.get("attributes", {}).get("class_name") == "dining_table"
    ]
    events = []
    for object_id in targets:
        events.extend(event_rows(states, object_id, frame_diagnostics, detection_ledger))
    events.sort(key=lambda row: (row["event_frame"], row["object_id"]))
    if events:
        render_event_overlay(states, run / "grid_trace", events, output / "dining_table_membership_events.png")
    else:
        raise RuntimeError("no dining_table membership change found")
    audit = {"schema_version": 1, "kind": "membership_event_audit", "events": events, "terminal_delta_events": (terminal or {}).get("events", [])}
    (output / "membership_events.json").write_text(json.dumps(audit, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    with (output / "membership_events.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = sorted({key for row in events for key in row})
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for row in events:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    annotation_rows = []
    for row in events:
        annotation_rows.append({
            "event_object_id": row["object_id"], "event_frame": row["event_frame"], "previous_frame": row["previous_frame"],
            "detection_identity_same_physical_object": "", "previous_room_gt": "", "event_room_gt": "",
            "boundary_mode": "", "annotation_confidence": "", "notes": "",
        })
    with (output / "membership_annotation_template.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = list(annotation_rows[0]) if annotation_rows else ["event_object_id", "event_frame", "previous_frame", "detection_identity_same_physical_object", "previous_room_gt", "event_room_gt", "boundary_mode", "annotation_confidence", "notes"]
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(annotation_rows)
    object_rows = load_json(run / "graph_quality_audit" / "object_audit.json").get("objects", [])
    p3_rows = render_detection_review(run, object_rows, output / "p3_review_sheet.png")
    with (output / "p3_annotation_template.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = list(p3_rows[0]) if p3_rows else ["detection_id", "frame_index", "class_canonical", "decision", "reason", "mask_pixel_count", "valid_depth_fraction", "cluster_count", "largest_cluster_points", "review_true_object", "review_should_accept_3d", "review_notes"]
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(p3_rows)
    report = {
        "schema_version": 1,
        "kind": "membership_event_audit",
        "status": "pass",
        "source_run": str(run),
        "baseline_unchanged": True,
        "objects_checked": targets,
        "event_count": len(events),
        "initial_assignment_count": sum(row["event_type"] == "initial_membership" for row in events),
        "membership_change_count": sum(row["event_type"] == "membership_changed" for row in events),
        "p3_review_count": len(p3_rows),
        "artifacts": ["membership_events.json", "membership_events.csv", "dining_table_membership_events.png", "membership_annotation_template.csv", "p3_review_sheet.png", "p3_annotation_template.csv"],
        "limitations": [
            "room and object correctness are not labeled; interpretations remain review-only",
            "the event overlay uses the exact recorded M3 grid per frame and replay delta states; it does not reproject",
            "independent room/object annotation is still required to call a membership change valid or erroneous",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
