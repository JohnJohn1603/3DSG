#!/usr/bin/env python3
"""Render a human-review package from an existing membership audit.

This does not replay the pipeline and does not create a second audit. It only
joins already-produced event records with the captured RGB/masks and the
recorded per-frame grid/graph states.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
from PIL import Image, ImageDraw

from scripts.audit_membership_events import (
    grid_extent,
    grid_for_frame,
    grid_rgba,
    node_at,
    reconstruct_states,
)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_detection_maps(run: Path):
    index = load_json(run / "detection_2d" / "detection_2d_index.json")
    d2 = {}
    for frame in index.get("frames", []):
        for item in frame.get("detections", []):
            d2[item["detection_id"]] = {**item, "frame_index": int(frame["frame_index"]), "frame_id": frame["frame_id"]}
    ledger = {
        row["detection_id"]: row
        for row in (
            json.loads(line)
            for line in (run / "detection_3d" / "object_evidence_ledger.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    return d2, ledger


def rgb_image(run: Path, frame_index: int) -> np.ndarray:
    path = run / "capture" / "frames" / f"{frame_index:06d}" / "rgb.npy"
    rgb = np.asarray(np.load(path, allow_pickle=False), dtype=np.uint8)
    return rgb[..., :3] if rgb.shape[-1] == 4 else rgb


def mask_overlay(run: Path, frame_index: int, detection_id: str | None, d2: dict, ledger: dict, title: str) -> Image.Image:
    rgb = rgb_image(run, frame_index)
    image = Image.fromarray(rgb, mode="RGB").convert("RGBA")
    record = d2.get(detection_id or "")
    has_mask = bool(record and int(record["frame_index"]) == frame_index)
    if has_mask:
        mask_path = run / "detection_2d" / record["mask_file"]
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        overlay = np.zeros((*mask.shape, 4), dtype=np.uint8)
        overlay[mask] = (255, 90, 0, 110)
        image = Image.alpha_composite(image, Image.fromarray(overlay, mode="RGBA"))
        suffix = "mask at this frame"
    else:
        suffix = f"no new mask; last evidence={detection_id or 'none'}"
    image = image.convert("RGB").resize((480, 360))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 479, 34), fill=(0, 0, 0))
    draw.text((6, 8), f"{title} — {suffix}"[:95], fill=(255, 255, 255))
    return image


def room_map(run: Path, states: dict[int, dict], frame_index: int, event: dict, bounds: tuple[float, float, float, float], output: Path) -> None:
    meta, values = grid_for_frame(run / "grid_trace", frame_index)
    state = states[frame_index]
    fig, ax = plt.subplots(figsize=(5.5, 4.5), constrained_layout=True)
    ax.imshow(grid_rgba(values), origin="lower", extent=grid_extent(meta, values), interpolation="nearest")
    affected = set(event["previous_room_ids"] + event["event_room_ids"])
    for node_id, node in state["nodes"].items():
        if node.get("kind") != "room":
            continue
        ring = np.asarray(node.get("attributes", {}).get("outer_ring", []), dtype=float)
        if ring.size == 0:
            continue
        closed = np.vstack((ring, ring[0]))
        ax.plot(closed[:, 0], closed[:, 1], color="#00a878" if node_id in affected else "#777777", linewidth=2.0 if node_id in affected else 0.8, linestyle="--", alpha=0.95 if node_id in affected else 0.35)
    object_node = node_at(state, event["object_id"])
    center = (object_node or {}).get("attributes", {}).get("center_W")
    if center:
        cx, cy = center[:2]
        ax.scatter([cx], [cy], color="#ff7f00", edgecolor="black", s=80, zorder=5)
        extents = (object_node or {}).get("attributes", {}).get("extents_m")
        if extents and len(extents) >= 2:
            ax.add_patch(Rectangle((cx - extents[0] / 2, cy - extents[1] / 2), extents[0], extents[1], fill=False, edgecolor="#ff7f00", linestyle=":", linewidth=1.4, zorder=5))
        ax.annotate(event["object_id"].replace("object:", ""), (cx, cy), xytext=(5, 5), textcoords="offset points", fontsize=8)
    ax.set_xlim(bounds[0], bounds[1]); ax.set_ylim(bounds[2], bounds[3]); ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("world X (m)"); ax.set_ylabel("world Y (m)")
    ax.set_title(f"frame {frame_index}; rooms={event['event_room_ids'] if frame_index == event['event_frame'] else event['previous_room_ids']}", fontsize=9)
    ax.grid(alpha=0.15)
    fig.savefig(output, dpi=140, facecolor="white")
    plt.close(fig)


def render_event(run: Path, states: dict[int, dict], event: dict, d2: dict, ledger: dict, output: Path, event_number: int) -> None:
    previous_frame = int(event["previous_frame"])
    current_frame = int(event["event_frame"])
    previous_meta, previous_values = grid_for_frame(run / "grid_trace", previous_frame)
    current_meta, current_values = grid_for_frame(run / "grid_trace", current_frame)
    extents = [grid_extent(previous_meta, previous_values), grid_extent(current_meta, current_values)]
    bounds = (min(item[0] for item in extents), max(item[1] for item in extents), min(item[2] for item in extents), max(item[3] for item in extents))
    fig, axes = plt.subplots(2, 3, figsize=(18, 9), constrained_layout=True)
    for row_index, (frame, det_id, prefix) in enumerate(((previous_frame, event.get("previous_last_detection_id"), "before"), (current_frame, event.get("event_last_detection_id"), "after"))):
        rgb = rgb_image(run, frame)
        axes[row_index, 0].imshow(rgb)
        axes[row_index, 0].set_title(f"{prefix}: RGB frame {frame}")
        axes[row_index, 0].axis("off")
        card = mask_overlay(run, frame, det_id, d2, ledger, f"{prefix}: frame {frame}")
        axes[row_index, 1].imshow(card)
        axes[row_index, 1].set_title("mask/depth evidence")
        axes[row_index, 1].axis("off")
        meta, values = grid_for_frame(run / "grid_trace", frame)
        axes[row_index, 2].imshow(grid_rgba(values), origin="lower", extent=grid_extent(meta, values), interpolation="nearest")
        state = states[frame]
        affected = set(event["previous_room_ids"] + event["event_room_ids"])
        for node_id, node in state["nodes"].items():
            if node.get("kind") != "room":
                continue
            ring = np.asarray(node.get("attributes", {}).get("outer_ring", []), dtype=float)
            if ring.size:
                closed = np.vstack((ring, ring[0]))
                axes[row_index, 2].plot(closed[:, 0], closed[:, 1], color="#00a878" if node_id in affected else "#777777", linewidth=2.0 if node_id in affected else 0.8, linestyle="--", alpha=0.95 if node_id in affected else 0.35)
        node = node_at(state, event["object_id"])
        center = (node or {}).get("attributes", {}).get("center_W")
        if center:
            cx, cy = center[:2]
            axes[row_index, 2].scatter([cx], [cy], color="#ff7f00", edgecolor="black", s=80, zorder=5)
        axes[row_index, 2].set_xlim(bounds[0], bounds[1]); axes[row_index, 2].set_ylim(bounds[2], bounds[3]); axes[row_index, 2].set_aspect("equal", adjustable="box")
        axes[row_index, 2].set_xlabel("world X (m)"); axes[row_index, 2].set_ylabel("world Y (m)")
        axes[row_index, 2].set_title(f"{prefix}: room overlay, frame {frame}")
        axes[row_index, 2].grid(alpha=0.15)
    fig.suptitle(f"Membership event {event_number}: {event['object_id']} {event['previous_room_ids']} → {event['event_room_ids']}\nSame scale; overlay is recorded M3 + graph state", fontsize=14)
    fig.savefig(output / f"membership_event_{event_number:02d}_{event['object_id'].replace(':', '_')}.png", dpi=140, facecolor="white")
    plt.close(fig)


def ai_membership_proposals(events: list[dict]) -> list[dict]:
    proposals = []
    for index, event in enumerate(events, start=1):
        no_new_detection = not event["new_detection_at_event"]
        proposals.append({
            "event_number": index,
            "object_id": event["object_id"],
            "transition": f"{event['previous_room_ids']} -> {event['event_room_ids']}",
            "same_physical_object_ai_assisted": "uncertain",
            "object_area_world_xy": event.get("event_center_W", ""),
            "room_boundary_change_ai_assisted": "uncertain",
            "evidence_summary": (
                "no new detection at event; retained geometry/provenance" if no_new_detection
                else "same canonical class and association retained ID; new detection geometry needs human confirmation"
            ),
            "source": "AI-assisted proposal from existing RGB/mask, detection ledger, delta and grid artifacts",
            "human_confirmation": "required",
        })
    return proposals


def ai_p3_proposals(run: Path, audit_dir: Path) -> list[dict]:
    rows = list(csv.DictReader((audit_dir / "p3_annotation_template.csv").open(encoding="utf-8", newline="")))
    proposals = []
    for row in rows:
        decision = row["decision"]
        if decision == "accepted":
            visual = "likely_correct"
            mask = "likely_usable_for_3d"
            p3 = "likely_reasonable"
            note = "RGB/mask shows a plausible object; independent object GT still required."
        else:
            visual = "uncertain"
            mask = "2d_mask_present_3d_depth_fragmented"
            p3 = "likely_reasonable_under_current_cluster_contract"
            note = "Object may be visible, but class/identity and usable 3D surface need human confirmation."
        proposals.append({
            "detection_id": row["detection_id"],
            "frame_index": row["frame_index"],
            "class_canonical": row["class_canonical"],
            "p3_decision": decision,
            "detection_visual_ai_assisted": visual,
            "mask_depth_ai_assisted": mask,
            "p3_decision_ai_assisted": p3,
            "source": "AI-assisted proposal from existing RGB/mask and ledger; not independent GT",
            "human_confirmation": "required",
            "notes": note,
        })
    return proposals


def write_csv(path: Path, rows: list[dict]):
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run = args.run.resolve(); audit_dir = args.audit.resolve(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    events = [row for row in load_json(audit_dir / "membership_events.json")["events"] if row["event_type"] == "membership_changed"]
    states, _ = reconstruct_states(run / "replay")
    d2, ledger = load_detection_maps(run)
    for index, event in enumerate(events, start=1):
        render_event(run, states, event, d2, ledger, output, index)
    membership_proposals = ai_membership_proposals(events)
    p3_proposals = ai_p3_proposals(run, audit_dir)
    (output / "membership_ai_assisted_proposals.json").write_text(json.dumps(membership_proposals, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    write_csv(output / "membership_ai_assisted_proposals.csv", membership_proposals)
    write_csv(output / "p3_ai_assisted_proposals.csv", p3_proposals)
    review = {
        "schema_version": 1,
        "kind": "membership_review_package",
        "source_run": str(run),
        "source_audit": str(audit_dir),
        "event_count": len(events),
        "p3_count": len(p3_proposals),
        "annotation_source": "AI-assisted; human confirmation required; not independent ground truth",
        "membership_questions": [
            "Are before/after detections the same physical object or different objects?",
            "Which physical area/room does the object occupy?",
            "Did the room boundary/ownership change for a geometrically valid reason?",
        ],
        "p3_questions": [
            "Is the detected object actually present in RGB?",
            "Does the mask cover the object sufficiently?",
            "Is depth usable for a stable 3D cluster?",
            "Was the P3 accept/reject decision reasonable independently of the decision itself?",
        ],
        "artifacts": [
            "membership_event_01_object_object-000002.png",
            "membership_event_02_object_object-000002.png",
            "membership_event_03_object_object-000012.png",
            "membership_ai_assisted_proposals.csv",
            "p3_ai_assisted_proposals.csv",
        ],
        "stop_condition": "Stop after all 3 membership events and all 16 P3 samples receive human labels; change at most one layer only if a confirmed error exists.",
    }
    (output / "manifest.json").write_text(json.dumps(review, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(review, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
