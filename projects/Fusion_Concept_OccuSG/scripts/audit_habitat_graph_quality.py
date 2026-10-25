#!/usr/bin/env python3
"""Audit object identity, room membership and evidence for one replay.

This is an evaluation/reporting tool only.  It reads an already committed
snapshot, frame diagnostics, grid trace and perception caches; it never feeds
reference geometry or annotations back into M1-M10.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import numpy as np
from PIL import Image, ImageDraw


FRAME_RE = re.compile(r"replica-cad-(\d+)")


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot(path: Path) -> dict:
    data = _json(path)
    return data.get("snapshot", data)


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _frame_index(value: str | None) -> int | None:
    if not value:
        return None
    match = FRAME_RE.search(value)
    return None if match is None else int(match.group(1))


def _grid_last(grid_trace: Path) -> tuple[dict, dict[str, np.ndarray]]:
    index = _json(grid_trace / "index.json")
    entry = index["frames"][-1]
    meta = _json(grid_trace / entry["metadata_file"])
    with np.load(grid_trace / entry["array_file"], allow_pickle=False) as arrays:
        values = {name: arrays[name].copy() for name in arrays.files}
    return meta, values


def _detection_evidence(detection_2d: Path, detection_3d: Path) -> dict[str, dict]:
    d2_by_id = {}
    index = _json(detection_2d / "detection_2d_index.json")
    for frame in index.get("frames", []):
        for item in frame.get("detections", []):
            d2_by_id[item["detection_id"]] = {
                "frame_id": frame["frame_id"],
                "class_canonical": item.get("class_canonical"),
                "score_raw": item.get("score_raw"),
                "mask_file": str(detection_2d / item["mask_file"]),
                "overlay_file": str(detection_2d / "overlays" / f"{frame['frame_id']}.png"),
            }
    ledger = {item["detection_id"]: item for item in _load_jsonl(detection_3d / "object_evidence_ledger.jsonl")}
    result = {}
    for detection_id, item in d2_by_id.items():
        result[detection_id] = {**item, "p3": ledger.get(detection_id)}
    return result


def _trajectory(path: Path) -> list[tuple[float, float]]:
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append((float(row["x_m"]), float(row["y_m"])))
    return rows


def _room_rgb_contact_sheet(snapshot: dict, capture: Path, output: Path) -> list[dict]:
    rooms = [node for node in snapshot["nodes"] if node["kind"] == "room"]
    panels = []
    for room in rooms:
        members = room.get("attributes", {}).get("member_ids", [])
        frame = next((_frame_index(item) for item in members if item.startswith("agent:")), None)
        if frame is None:
            continue
        rgb_path = capture / "frames" / f"{frame:06d}" / "rgb.npy"
        if not rgb_path.is_file():
            continue
        rgb = np.asarray(np.load(rgb_path, allow_pickle=False), dtype=np.uint8)
        if rgb.shape[-1] == 4:
            rgb = rgb[..., :3]
        image = Image.fromarray(rgb, mode="RGB").resize((320, 240))
        draw = ImageDraw.Draw(image)
        attrs = room.get("attributes", {})
        label = attrs.get("semantic_label") or "unknown"
        draw.rectangle((0, 0, 319, 28), fill=(0, 0, 0))
        draw.text((6, 6), f"{room['node_id']}  {label}  frame={frame}", fill=(255, 255, 255))
        panels.append({"room_id": room["node_id"], "frame_index": frame, "rgb_file": str(rgb_path), "label": label, "image": image})
    if not panels:
        return []
    cols = 2
    rows = (len(panels) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 320, rows * 240), (230, 230, 230))
    for index, panel in enumerate(panels):
        sheet.paste(panel.pop("image"), ((index % cols) * 320, (index // cols) * 240))
    sheet.save(output)
    return panels


def _composite(snapshot: dict, grid_meta: dict, arrays: dict[str, np.ndarray], trajectory: list[tuple[float, float]], output: Path) -> None:
    values = arrays["values"]
    origin_x, origin_y = grid_meta["origin_xy"]
    resolution = float(grid_meta["resolution_m"])
    extent = (origin_x, origin_x + values.shape[1] * resolution, origin_y, origin_y + values.shape[0] * resolution)
    rgba = np.empty((*values.shape, 4), dtype=float)
    rgba[values < 0] = (0.63, 0.63, 0.63, 1.0)
    rgba[values == 0] = (1.0, 1.0, 1.0, 1.0)
    rgba[values > 0] = (0.05, 0.05, 0.05, 1.0)
    fig, ax = plt.subplots(figsize=(12, 9), constrained_layout=True)
    ax.imshow(rgba, origin="lower", extent=extent, interpolation="nearest")
    object_colors = {"chair": "#1f78b4", "sofa": "#e31a1c", "dining_table": "#ff7f00", "tv": "#6a3d9a"}
    seen = set()
    for room in snapshot["nodes"]:
        if room["kind"] != "room":
            continue
        attrs = room.get("attributes", {})
        ring = np.asarray(attrs.get("outer_ring", []), dtype=float)
        if ring.size:
            closed = np.vstack((ring, ring[0]))
            label = attrs.get("semantic_label") or "unknown"
            ax.plot(closed[:, 0], closed[:, 1], color="#00a878", linewidth=1.6, linestyle="--", alpha=.9)
            center = attrs.get("centroid_xy")
            if center:
                ax.text(center[0], center[1], f"{room['node_id']}\n{label}", fontsize=7, ha="center", bbox={"facecolor": "white", "alpha": .7, "edgecolor": "none"})
    if trajectory:
        xs, ys = zip(*trajectory)
        ax.plot(xs, ys, color="#00b7ff", linewidth=2.0, label="trajectory", zorder=5)
        ax.scatter([xs[0]], [ys[0]], color="#33a02c", s=30, label="start", zorder=6)
        ax.scatter([xs[-1]], [ys[-1]], color="#e31a1c", marker="^", s=38, label="end", zorder=6)
    for node in snapshot["nodes"]:
        if node["kind"] != "object":
            continue
        attrs = node.get("attributes", {})
        center = attrs.get("center_W")
        if not center:
            continue
        cls = attrs.get("class_name", "object")
        color = object_colors.get(cls, "#17becf")
        label = node["node_id"].replace("object:", "")
        ax.scatter([center[0]], [center[1]], color=color, edgecolor="black", s=48, zorder=7)
        ax.annotate(label, (center[0], center[1]), xytext=(4, 4), textcoords="offset points", fontsize=7)
        if cls not in seen:
            ax.scatter([], [], color=color, edgecolor="black", s=48, label=cls)
            seen.add(cls)
    ax.set_title("Graph quality audit — M3 occupancy + M8 rooms + objects + trajectory")
    ax.set_xlabel("world X (m)")
    ax.set_ylabel("world Y (m)")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(alpha=.15)
    ax.legend(loc="best", fontsize=8)
    fig.savefig(output, dpi=150, facecolor="white")
    plt.close(fig)


def run(args: argparse.Namespace) -> dict:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = _snapshot(args.snapshot)
    diagnostics = _json(args.replay / "frame_diagnostics.json")
    evidence = _detection_evidence(args.detection_2d, args.detection_3d)
    by_object = {}
    for item in diagnostics:
        frame = int(item["frame_index"])
        for object_node in item.get("object_nodes", []):
            oid = object_node["node_id"]
            state = by_object.setdefault(oid, {"retained_frames": [], "observation_frames": [], "room_memberships_over_time": [], "associated_detection_ids": [], "last_detection_id": None})
            state["retained_frames"].append(frame)
            state["room_memberships_over_time"].append({"frame_index": frame, "room_ids": object_node.get("room_membership_edges", [])})
            detection_id = object_node.get("last_detection_id")
            if detection_id and detection_id != state["last_detection_id"]:
                state["observation_frames"].append(frame)
                state["associated_detection_ids"].append(detection_id)
                state["last_detection_id"] = detection_id
    final_objects = {node["node_id"]: node for node in snapshot["nodes"] if node["kind"] == "object"}
    object_rows = []
    for oid in sorted(final_objects):
        node = final_objects[oid]
        attrs = node.get("attributes", {})
        state = by_object.get(oid, {})
        ids = state.get("associated_detection_ids", [])
        rooms = sorted({room for item in state.get("room_memberships_over_time", []) for room in item.get("room_ids", [])})
        evidence_rows = [{"detection_id": did, **evidence[did]} for did in ids if did in evidence]
        row = {
            "node_id": oid,
            "class_name": attrs.get("class_name"),
            "center_W": attrs.get("center_W"),
            "extents_m": attrs.get("extents_m"),
            "first_seen_s": node.get("first_seen_s"),
            "last_seen_s": node.get("last_seen_s"),
            "first_detection_id": attrs.get("first_detection_id"),
            "last_detection_id": attrs.get("last_detection_id"),
            "retained_frames": sorted(set(state.get("retained_frames", []))),
            "observation_frames": sorted(set(state.get("observation_frames", []))),
            "associated_detection_ids": ids,
            "room_ids_seen_over_time": rooms,
            "room_memberships_over_time": state.get("room_memberships_over_time", []),
            "evidence": evidence_rows,
            "review_flags": [],
        }
        if len(row["observation_frames"]) > 1 and len(row["associated_detection_ids"]) == 1:
            row["review_flags"].append("multiple_observation_frames_but_only_one_detection_id_recorded")
        if len(rooms) > 1:
            row["review_flags"].append("membership_changed_or_multiple_room_edges_over_time")
        if not row["observation_frames"]:
            row["review_flags"].append("retained_without_new_detection_evidence")
        object_rows.append(row)
    # Pairwise same-class proximity is a review cue, not a false-merge verdict.
    for index, left in enumerate(object_rows):
        for right in object_rows[index + 1:]:
            if left["class_name"] != right["class_name"] or not left["center_W"] or not right["center_W"]:
                continue
            distance = float(np.linalg.norm(np.asarray(left["center_W"]) - np.asarray(right["center_W"])))
            if distance <= 0.75:
                left["review_flags"].append(f"same_class_pair_within_0.75m:{right['node_id']}:{distance:.3f}m")
                right["review_flags"].append(f"same_class_pair_within_0.75m:{left['node_id']}:{distance:.3f}m")
    object_rows_path = output / "object_audit.json"
    object_rows_path.write_text(json.dumps({"schema_version": 1, "kind": "graph_quality_object_audit", "objects": object_rows}, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    with (output / "object_audit.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["node_id", "class_name", "center_W", "first_seen_s", "last_seen_s", "retained_frames", "observation_frames", "associated_detection_ids", "room_ids_seen_over_time", "review_flags"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in object_rows:
            writer.writerow({field: json.dumps(row[field], ensure_ascii=False) if isinstance(row[field], (list, dict)) else row[field] for field in fields})
    grid_meta, arrays = _grid_last(args.grid_trace)
    trajectory = _trajectory(args.replay / "trajectory.csv")
    _composite(snapshot, grid_meta, arrays, trajectory, output / "occupancy_rooms_objects_trajectory.png")
    room_panels = _room_rgb_contact_sheet(snapshot, args.capture, output / "room_rgb_contact_sheet.png")
    report = {
        "schema_version": 1,
        "kind": "graph_quality_audit",
        "status": "pass",
        "snapshot": str(args.snapshot.resolve()),
        "room_gt_used": False,
        "semantic_gt_used": False,
        "object_count": len(object_rows),
        "object_rows": str(object_rows_path),
        "review_only_flags": True,
        "artifacts": ["object_audit.json", "object_audit.csv", "occupancy_rooms_objects_trajectory.png", "room_rgb_contact_sheet.png"],
        "room_rgb_panels": room_panels,
        "limitations": [
            "review flags are not false-merge or membership correctness labels",
            "without independent object/room annotations this report cannot calculate recall, precision or room accuracy",
            "occupancy uses the final M3 grid recorded by the replay; no extra projection is performed",
        ],
    }
    (output / "manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--grid-trace", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--detection-2d", type=Path, required=True)
    parser.add_argument("--detection-3d", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
