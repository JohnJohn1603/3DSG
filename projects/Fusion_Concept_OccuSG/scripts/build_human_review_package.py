#!/usr/bin/env python3
"""Build a blinded, finite human-review package from an existing hybrid run.

This is an artifact-only operation. It never runs perception, mapping, or
room scoring. Predicted classes and room scores are deliberately excluded from
the review JSON/HTML; the source manifests remain outside the package.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def point_in_polygon(x: float, y: float, polygon: list[list[float]]) -> bool:
    inside = False
    for i in range(len(polygon)):
        x1, y1 = polygon[i - 1]
        x2, y2 = polygon[i]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def bounds_xy(polygons: list[list[list[float]]], points: np.ndarray) -> tuple[float, float, float, float]:
    all_xy = [p for poly in polygons for p in poly]
    if points.size:
        all_xy.extend(points[:, :2].tolist())
    xs = [p[0] for p in all_xy]
    ys = [p[1] for p in all_xy]
    return min(xs), max(xs), min(ys), max(ys)


def make_scene_svg(room_nodes: dict[str, dict], camera_points: list[dict], tv_xy: np.ndarray, path: Path) -> None:
    polygons = [n["attributes"]["outer_ring"] for n in room_nodes.values()]
    xmin, xmax, ymin, ymax = bounds_xy(polygons, tv_xy)
    pad = 0.35
    xmin -= pad
    xmax += pad
    ymin -= pad
    ymax += pad
    width, height = 1000, 760

    def sx(x: float) -> float:
        return 30 + (x - xmin) / (xmax - xmin) * (width - 60)

    def sy(y: float) -> float:
        return height - 30 - (y - ymin) / (ymax - ymin) * (height - 60)

    colors = ["#b8c8e8", "#c8e6c9", "#f7d6a6", "#e8b8c8", "#c9b7e8"]
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Room polygons and TV cloud">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    for idx, (rid, node) in enumerate(sorted(room_nodes.items())):
        poly = node["attributes"]["outer_ring"]
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in poly)
        out.append(f'<polygon points="{pts}" fill="{colors[idx % len(colors)]}" fill-opacity="0.18" stroke="#334155" stroke-width="1.5"/>')
        c = node["attributes"].get("centroid_xy", poly[0])
        out.append(f'<text x="{sx(c[0]):.1f}" y="{sy(c[1]):.1f}" font-size="16" fill="#1e293b">{html.escape(rid)}</text>')
    if tv_xy.size:
        step = max(1, len(tv_xy) // 900)
        for x, y, *_ in tv_xy[::step]:
            out.append(f'<circle cx="{sx(float(x)):.1f}" cy="{sy(float(y)):.1f}" r="1.6" fill="#b91c1c" fill-opacity="0.42"/>')
    for c in camera_points:
        x, y = c["pose_W"][:2]
        out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5" fill="#2563eb" stroke="#fff" stroke-width="1"/>')
        out.append(f'<text x="{sx(x)+7:.1f}" y="{sy(y)-7:.1f}" font-size="13" fill="#1d4ed8">frame {c["frame_index"]}</text>')
    out.append('<text x="30" y="28" font-size="16" fill="#111827">Top-down diagnostic: room polygons, selected camera poses, TV cloud</text>')
    out.append('<text x="30" y="50" font-size="13" fill="#334155">red = TV voxel representatives; blue = full-RGB review viewpoints</text>')
    out.append('</svg>')
    path.write_text("\n".join(out) + "\n")


def copy_rgb_from_npy(source: Path, target: Path) -> None:
    arr = np.load(source)
    if arr.ndim == 3 and arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(target)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--room-snapshot", type=Path)
    args = ap.parse_args()
    run_root = args.run_root.resolve()
    out = args.output.resolve()
    if out.exists():
        raise SystemExit(f"Refusing to overwrite existing review package: {out}")

    mapping = run_root / "mapping"
    diag_path = mapping / "diagnostic/diagnostic_manifest.json"
    evidence_path = mapping / "diagnostic/evidence_manifest.json"
    hybrid_manifest_path = run_root / "hybrid_manifest.json"
    if args.room_snapshot:
        snapshot_path = args.room_snapshot.resolve()
    elif hybrid_manifest_path.is_file():
        snapshot_path = Path(json.loads(hybrid_manifest_path.read_text())["room_snapshot"]).resolve()
    else:
        snapshot_path = run_root / "replay/snapshot.json"
    readiness_path = Path("artifacts/room_belief/readiness/hm3d00802_main29_min3_static_v1/readiness.json").resolve()
    export_path = mapping / "export/export_manifest.json"
    if not hybrid_manifest_path.is_file():
        raise SystemExit(f"Missing hybrid manifest: {hybrid_manifest_path}")
    cache_root = Path(json.loads(hybrid_manifest_path.read_text())["cache_root"]).resolve()
    p2_path = cache_root / "detection_2d/detection_2d_index.json"
    for required in (diag_path, evidence_path, snapshot_path, readiness_path, export_path, p2_path):
        if not required.is_file():
            raise SystemExit(f"Missing required artifact: {required}")

    diag = json.loads(diag_path.read_text())
    evidence = json.loads(evidence_path.read_text())["observations"]
    snapshot = json.loads(snapshot_path.read_text())["snapshot"]
    readiness = json.loads(readiness_path.read_text())
    export = json.loads(export_path.read_text())
    p2 = json.loads(p2_path.read_text())
    retained = [x for x in diag["objects"] if x.get("status_after_filter") == "retained"]
    if len(retained) != 6:
        raise SystemExit(f"Expected six retained hypotheses, got {len(retained)}")

    out.mkdir(parents=True)
    assets = out / "assets"
    (assets / "hypotheses").mkdir(parents=True)
    (assets / "room6").mkdir(parents=True)
    (assets / "tv").mkdir(parents=True)

    # Keep only fields a blinded reviewer needs. In particular, class histories,
    # consensus, A scores and room-belief scores never enter this structure.
    review_hypotheses = []
    asset_records = []
    for idx, obj in enumerate(retained, start=1):
        review_id = f"H{idx:02d}"
        rows = []
        for obs_id in obj["observation_ids"]:
            if obs_id not in evidence:
                raise SystemExit(f"Missing evidence panel for {obs_id}")
            ev = evidence[obs_id]
            obs_dir = assets / "hypotheses" / review_id
            obs_dir.mkdir(parents=True, exist_ok=True)
            copied = {}
            for kind in ("rgb", "mask", "crop"):
                source = mapping / "diagnostic" / ev["panels"][kind]
                target = obs_dir / f"{obs_id.replace(':', '__').replace('-', '_')}_{kind}.png"
                shutil.copy2(source, target)
                copied[kind] = str(target.relative_to(out))
                asset_records.append(target)
            pose = ev["camera_pose_W_Copt"]
            rows.append({
                "observation_id": obs_id,
                "frame_index": ev["frame_index"],
                "camera_pose_W_Copt": pose,
                "assets": copied,
            })
        review_hypotheses.append({
            "review_id": review_id,
            "hypothesis_id": obj["hypothesis_id"],
            "observation_ids": obj["observation_ids"],
            "timeline": rows,
            "counts": {
                "detection_count": obj["detection_count"],
                "distinct_frame_count": obj["distinct_frame_count"],
                "distinct_pose_count": obj["distinct_pose_count"],
            },
            "review_response": {
                "physical_identity_token": None,
                "identity_status": "uncertain",
                "class": None,
                "class_status": "uncertain",
                "mask_purity": None,
                "same_physical_object": None,
            },
        })

    # Room 6 review viewpoints: use unique camera poses whose positions are in
    # the room polygon. Empty-detection frames are rendered from cached RGB.
    rooms = {n["node_id"]: n for n in snapshot["nodes"] if n.get("kind") == "room"}
    room6 = rooms.get("room:6")
    if room6 is None:
        raise SystemExit("room:6 not found in room snapshot")
    poly6 = room6["attributes"]["outer_ring"]
    agents = [n for n in snapshot["nodes"] if n.get("kind") == "agent"]
    selected = []
    seen_poses = set()
    frame_root = cache_root / "capture/frames"
    for node in sorted(agents, key=lambda n: n["attributes"]["frame_index"]):
        a = node["attributes"]
        x, y, z = a["pose_W"]
        if not point_in_polygon(x, y, poly6):
            continue
        pose_key = tuple(round(float(v), 6) for v in a["pose_W"])
        if pose_key in seen_poses:
            continue
        seen_poses.add(pose_key)
        frame_idx = int(a["frame_index"])
        target = assets / "room6" / f"frame_{frame_idx:04d}_rgb.png"
        copy_rgb_from_npy(frame_root / f"{frame_idx:06d}/rgb.npy", target)
        selected.append({"frame_index": frame_idx, "frame_id": a["frame_id"], "pose_W": a["pose_W"], "asset": str(target.relative_to(out))})
    if not selected:
        raise SystemExit("No unique camera pose inside room:6 polygon")

    # TV cloud and membership diagnostic are deliberately diagnostic only.
    tv_obj = next(x for x in readiness["objects"] if x["hypothesis_id"] == "hypothesis_d579ba788876c64a")
    tv_npz = run_root / tv_obj["geometry_source"]
    tv_cloud = np.load(tv_npz)["xyz"]
    make_scene_svg(rooms, selected, tv_cloud, assets / "tv" / "membership_overview.svg")
    room6_svg = assets / "room6" / "room6_polygon.svg"
    make_scene_svg({"room:6": room6}, selected, np.empty((0, 3)), room6_svg)

    object_nodes = [n for n in snapshot["nodes"] if n.get("kind") == "object"]
    tv_snapshot_nodes = [n for n in object_nodes if n.get("attributes", {}).get("class_name") == "tv"]
    layer_values = sorted({n.get("attributes", {}).get("layer_id") for n in object_nodes if n.get("attributes", {}).get("layer_id")})
    tv_membership = {
        "hypothesis_id": tv_obj["hypothesis_id"],
        "status": tv_obj["membership"]["status"],
        "room_id": tv_obj["membership"]["room_id"],
        "support_counts": tv_obj["membership"]["support_counts"],
        "support_fraction": tv_obj["membership"]["support_fraction"],
        "support_fraction_denominator": tv_obj["membership"]["support_fraction_denominator"],
        "outside_fraction": tv_obj["membership"]["outside_fraction"],
        "runner_up_fraction": tv_obj["membership"]["runner_up_fraction"],
        "runner_up_room_ids": tv_obj["membership"]["runner_up_room_ids"],
        "tie": tv_obj["membership"]["tied"],
        "voxel_size_m": tv_obj["membership"]["voxel_size_m"],
        "cloud_npz": str(tv_npz),
        "coordinate_frame": "conceptgraphs_world_from_capture_contract; meters; Z-up; room polygons XY",
        "layer_provenance": {
            "snapshot_tv_nodes": [{"node_id": n["node_id"], "layer_id": n.get("attributes", {}).get("layer_id"), "provenance": n.get("provenance")} for n in tv_snapshot_nodes],
            "all_snapshot_object_layer_ids": layer_values,
            "detection_2d_layer_field_present": False,
            "contract_default_layer_id": "floor_0",
            "room_floor_metadata_present": False,
            "verification": "floor_0 is explicit in the room snapshot but is not independently proven as a physical floor: P2 has no layer field and the object contract default is floor_0.",
        },
    }
    dump_json(out / "tv_membership_diagnostic.json", tv_membership)

    blinded = {
        "schema": "human-review-package/00802-v1",
        "review_scope": "blinded retained-object identity/class/mask review plus independent room:6 function review",
        "run_id": "hm3d00802_main29_min3_v1",
        "scene_id": export["scene_id"],
        "sequence_id": export["sequence_id"],
        "filter_scope": {"retained_hypotheses": 6, "before_filter": diag["counts"]["before_filter"], "filtered": diag["counts"]["filtered"]},
        "blinding": {"hidden": ["predicted class", "semantic consensus", "room-label scores", "A-table scores", "existing room labels"], "visible": ["full observation IDs", "frame index", "camera pose", "RGB", "mask", "crop", "timeline counts"]},
        "hypotheses": review_hypotheses,
        "room_review": {
            "room_id": "room:6",
            "polygon_asset": str(room6_svg.relative_to(out)),
            "viewpoints": selected,
            "review_response": {"room_type": None, "status": "uncertain", "mixed_use_functions": [], "evidence_status": "uncertain"},
        },
        "tv_membership_diagnostic": "tv_membership_diagnostic.json",
        "provenance": {"source_kind": "hybrid cached perception + ConceptGraphs reference mapping core", "source_manifest": str(hybrid_manifest_path.resolve()), "room_snapshot": str(snapshot_path.resolve()), "readiness": str(readiness_path), "no_pipeline_rerun": True},
    }
    dump_json(out / "blinded_review.json", blinded)
    dump_json(out / "human_review_template.json", {"schema": blinded["schema"], "run_id": blinded["run_id"], "hypotheses": [{"review_id": h["review_id"], "physical_identity_token": None, "identity_status": "uncertain", "class": None, "class_status": "uncertain", "mask_purity": None, "same_physical_object": None} for h in review_hypotheses], "room_review": blinded["room_review"]["review_response"]})

    # A compact HTML is intentionally data-only and has no hidden predicted fields.
    def img(src: str, alt: str) -> str:
        return f'<img src="{html.escape(src)}" alt="{html.escape(alt)}" loading="lazy">'
    page = ["<!doctype html><meta charset='utf-8'><title>Blinded human review 00802</title>", "<style>body{font-family:system-ui;margin:24px;color:#172033}section{border:1px solid #cbd5e1;border-radius:8px;padding:16px;margin:18px 0} .obs{display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:8px;margin:10px 0}img{max-width:100%;height:auto;border:1px solid #cbd5e1} .meta{font-family:monospace;font-size:12px;white-space:pre-wrap;background:#f8fafc;padding:8px} .roomviews{display:grid;grid-template-columns:repeat(2,minmax(240px,1fr));gap:12px}</style>", "<h1>Blinded human review — hm3d00802_main29_min3_v1</h1>", "<p>Review labels/identity from the images and timelines. Predicted classes, consensus and room scores are intentionally absent. A hypothesis is not assumed to be one physical object.</p>"]
    for h in review_hypotheses:
        page.append(f"<section><h2>{h['review_id']}</h2><div class='meta'>Observation IDs:\n{html.escape(chr(10).join(h['observation_ids']))}\nCounts: detections={h['counts']['detection_count']}, distinct frames={h['counts']['distinct_frame_count']}, distinct poses={h['counts']['distinct_pose_count']}</div>")
        for row in h["timeline"]:
            page.append(f"<h3>Frame {row['frame_index']} — {html.escape(row['observation_id'])}</h3><div class='meta'>camera_pose_W_Copt={html.escape(json.dumps(row['camera_pose_W_Copt']))}</div><div class='obs'>{img(row['assets']['rgb'], 'full RGB')}{img(row['assets']['mask'], 'mask')}{img(row['assets']['crop'], 'crop')}</div>")
        page.append("<p>Reviewer response: physical identity token / uncertain; class / uncertain; mask purity; same physical object across observations?</p></section>")
    page.append("<section><h2>Independent room-function review: room:6</h2><p>Room polygon and full RGB viewpoints only. Predicted room label and scores are hidden. Room annotation is independent from object annotations.</p><div class='roomviews'><div><h3>Polygon</h3>" + img('assets/room6/room6_polygon.svg', 'room 6 polygon') + "</div>")
    for v in selected:
        page.append(f"<div><h3>Frame {v['frame_index']} — {html.escape(v['frame_id'])}</h3>{img(v['asset'], 'full RGB viewpoint')}</div>")
    page.append("</div><p>Reviewer response: canonical room type; mixed-use with functions; uncertain/insufficient visual evidence.</p></section>")
    page.append("<section><h2>TV membership diagnostic</h2><p>This is diagnostic evidence, not a membership correction. The top-down view shows all room polygons, selected camera poses, and the TV cloud. See <a href='tv_membership_diagnostic.json'>tv_membership_diagnostic.json</a> for support counts and layer provenance.</p>" + img('assets/tv/membership_overview.svg', 'TV membership diagnostic') + "</section>")
    (out / "blinded_review.html").write_text("\n".join(page) + "\n")
    (out / "README.vi.md").write_text(
        "# Human review package — 00802 min3\n\n"
        "Mở `blinded_review.html`. Gói này được tạo từ artifacts đã có và không chạy lại perception, mapping hoặc room scoring.\n\n"
        "## Phần review mù\n\n"
        "- Với H01–H06, trả physical identity token hoặc uncertain; class hoặc uncertain; mask purity; và các observation có cùng physical object hay không.\n"
        "- Predicted class, semantic consensus, room-label scores và A-table scores không được đưa vào payload review.\n"
        "- Full observation IDs, frame/pose, RGB, mask, crop và timeline được giữ nguyên.\n\n"
        "## Room:6\n\n"
        "Đánh giá độc lập chức năng phòng từ polygon và RGB toàn cảnh: canonical room type, mixed-use kèm chức năng, hoặc uncertain/insufficient visual evidence. Không dùng object annotations để suy room review.\n\n"
        "## TV membership\n\n"
        "`tv_membership_diagnostic.json` và `assets/tv/membership_overview.svg` chỉ là diagnostic. Support hiện có bằng 0 cho mọi room, outside fraction là 1.0. `floor_0` xuất hiện explicit trong snapshot nhưng P2 không có layer field và object contract dùng `floor_0` làm default; vì vậy chưa xác minh được đó là physical floor.\n\n"
        "Điền `human_review_template.json` hoặc trả lại cùng các trường trong review response. Không coi hypothesis là physical identity mặc định.\n"
    )

    files = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "package_manifest.json":
            files.append({"path": str(p.relative_to(out)), "bytes": p.stat().st_size, "sha256": sha256(p)})
    dump_json(out / "package_manifest.json", {"schema": "human-review-package-manifest/v1", "run_id": blinded["run_id"], "file_count": len(files), "files": files, "review_is_blinded": True, "no_pipeline_rerun": True})
    print(json.dumps({"output": str(out), "hypotheses": len(review_hypotheses), "observation_assets": len(asset_records), "room6_viewpoints": len(selected), "tv_support_total": sum(tv_membership["support_counts"].values()), "floor_verification": tv_membership["layer_provenance"]["verification"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
