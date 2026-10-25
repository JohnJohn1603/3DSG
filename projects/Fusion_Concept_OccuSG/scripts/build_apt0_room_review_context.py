#!/usr/bin/env python3
"""Create prediction-free architectural context views for apt_0 room review."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from matplotlib import pyplot as plt
from matplotlib.collections import LineCollection
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from prepare_apt0_room_annotation import slice_segments, stage_triangles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_STAGE = ROOT / "artifacts/habitat_data/versioned_data/replica_cad_dataset/stages/frl_apartment_stage.glb"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_mesh(stage_path: Path):
    source = stage_triangles(stage_path)
    return [(name, np.stack((tri[:, :, 0], -tri[:, :, 2], tri[:, :, 1]), axis=2))
            for name, tri in source]


def bounds_of(mesh):
    all_points = np.concatenate([tri.reshape(-1, 3) for _name, tri in mesh], axis=0)
    return all_points.min(axis=0), all_points.max(axis=0)


def save_plan(mesh, bounds, output: Path):
    projected_edges = set()
    for _name, tris in mesh:
        for tri in tris:
            # Canonical z is Habitat elevation. Vertical wall faces have a
            # near-horizontal normal and non-zero height; floors/ceilings drop out.
            normal = np.cross(tri[1] - tri[0], tri[2] - tri[0])
            norm = float(np.linalg.norm(normal))
            if norm < 1e-10 or abs(float(normal[2] / norm)) > 0.22:
                continue
            if float(tri[:, 2].max() - tri[:, 2].min()) < 0.08:
                continue
            if float(tri[:, 2].max()) < 0.05 or float(tri[:, 2].min()) > 2.15:
                continue
            for i, j in ((0, 1), (1, 2), (2, 0)):
                a, b = tri[i, :2], tri[j, :2]
                if np.linalg.norm(a - b) > 0.025:
                    edge = tuple(sorted((tuple(np.round(a, 4)), tuple(np.round(b, 4)))))
                    projected_edges.add(edge)
    fig, ax = plt.subplots(figsize=(11, 12), dpi=170)
    collection = LineCollection(list(projected_edges), colors="#354451", linewidths=1.05, alpha=.78, rasterized=True)
    ax.add_collection(collection)
    lo, hi = bounds
    ax.set(xlim=(lo[0] - .25, hi[0] + .25), ylim=(lo[1] - .25, hi[1] + .25),
           xlabel="Canonical world X (m)", ylabel="Canonical world Y (m)",
           title="apt_0 architectural plan context — projected vertical stage surfaces\n(stage mesh only; no furniture, predictions, occupancy, or GT room polygons)")
    ax.set_aspect("equal", adjustable="box"); ax.grid(True, color="#d7dadd", linewidth=.5)
    fig.tight_layout(); fig.savefig(output, bbox_inches="tight"); plt.close(fig)
    return len(projected_edges)


def save_slices(stage_path: Path, output_dir: Path, heights: list[float], bounds):
    from prepare_apt0_room_annotation import slice_segments
    source = stage_triangles(stage_path)
    lo, hi = bounds
    result = []
    for height in heights:
        segments, _points = slice_segments(source, height)
        dest = output_dir / f"slice_habitat_y_{height:.2f}m.png"
        fig, ax = plt.subplots(figsize=(8.5, 10), dpi=150)
        for a, b in segments:
            ax.plot([a[0], b[0]], [a[1], b[1]], color="#222a32", linewidth=.55)
        ax.set(xlim=(lo[0] - .25, hi[0] + .25), ylim=(lo[1] - .25, hi[1] + .25),
               xlabel="Canonical world X (m)", ylabel="Canonical world Y (m)",
               title=f"Stage-mesh horizontal section at Habitat Y={height:.2f} m\ncanonical Y = −Habitat Z; architecture only")
        ax.set_aspect("equal", adjustable="box"); ax.grid(True, color="#e1e3e5", linewidth=.5)
        fig.tight_layout(); fig.savefig(dest, bbox_inches="tight"); plt.close(fig)
        result.append({"height_habitat_y_m": height, "segment_count": len(segments), "file": dest.name})
    return result


def save_3d_views(mesh, bounds, output: Path):
    faces = np.concatenate([tri for _name, tri in mesh], axis=0)
    face_z = faces[:, :, 2].mean(axis=1)
    lo, hi = bounds
    fig = plt.figure(figsize=(15, 12), dpi=140)
    angles = [(24, 35), (24, 125), (24, 215), (24, 305)]
    names = ["South-East", "North-East", "North-West", "South-West"]
    for i, ((elev, azim), name) in enumerate(zip(angles, names), 1):
        ax = fig.add_subplot(2, 2, i, projection="3d")
        coll = Poly3DCollection(faces, cmap="viridis", linewidths=0, alpha=.88, rasterized=True)
        coll.set_array(face_z)
        ax.add_collection3d(coll)
        ax.set(xlim=(lo[0], hi[0]), ylim=(lo[1], hi[1]), zlim=(lo[2], hi[2]),
               xlabel="X (m)", ylabel="Y (m)", zlabel="Z elevation (m)")
        ax.set_box_aspect(hi - lo); ax.view_init(elev=elev, azim=azim)
        ax.set_title(f"Architectural stage mesh — {name} view")
    fig.suptitle("apt_0 stage-only 3D context (height-colored; no furniture/predictions)")
    fig.tight_layout(); fig.savefig(output, bbox_inches="tight"); plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage-mesh", type=Path, default=DEFAULT_STAGE)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--slice-heights", type=float, nargs="+", default=[0.25, 0.85, 1.45, 1.85, 2.05],
                    help="Habitat Y-up horizontal section heights, metres")
    args = ap.parse_args()
    stage_path, out = args.stage_mesh.resolve(), args.output_dir.resolve()
    if out.exists() and any(out.iterdir()):
        ap.error(f"refusing to overwrite non-empty review output directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    mesh = canonical_mesh(stage_path); bounds = bounds_of(mesh)
    wall_face_count = save_plan(mesh, bounds, out / "architectural_plan_projected_walls.png")
    slices = save_slices(stage_path, out, args.slice_heights, bounds)
    save_3d_views(mesh, bounds, out / "architectural_stage_3d_views.png")
    image_links = ["architectural_plan_projected_walls.png", "architectural_stage_3d_views.png"] + [x["file"] for x in slices]
    cards = "\n".join(f'<figure><a href="{name}"><img src="{name}"></a><figcaption>{name}</figcaption></figure>' for name in image_links)
    html = f'''<!doctype html><meta charset="utf-8"><title>apt_0 room annotation context</title>
<style>body{{font:16px sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#20242a}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}figure{{margin:0;border:1px solid #ccd;padding:.5rem}}img{{width:100%;height:auto}}small{{color:#555}}</style>
<h1>apt_0 room annotation — geometry context</h1>
<p>This is independent stage-mesh context only. It contains no room predictions, occupancy, furniture, object labels, or room GT. Use it to inspect wall continuity and openings; confirm room boundaries, doorway thresholds and open-plan policy yourself. Unclear geometry stays uncertain.</p>
<p>Plan image projects vertical architectural mesh surfaces onto canonical XY; it is a review aid, not a pre-authored room polygon. Sections use Habitat Y-up heights and the same canonical XY extent. 3D views show the architecture-only stage and provide context where one horizontal section is ambiguous.</p>
<div class="grid">{cards}</div>
<p><a href="../room_annotation_tool_v2/apt0_room_annotation.html">Open polygon annotation tool</a> (its drawing plane remains the 0.85 m section; use this gallery as independent context, not as automatic annotation).</p>'''
    (out / "index.html").write_text(html, encoding="utf-8")
    manifest = {"schema_version": 1, "scene_id": "apt_0", "kind": "prediction_free_architectural_review_context",
        "status": "annotation_aid_not_room_gt", "stage_mesh_sha256": sha256(stage_path),
        "coordinate_frame": "canonical_world_z_up_xy_m", "mapping": "canonical x=Habitat X; canonical y=-Habitat Z; canonical z=Habitat Y",
        "bounds_canonical_xyz_m": [bounds[0].tolist(), bounds[1].tolist()], "projected_unique_vertical_mesh_edge_count": wall_face_count,
        "horizontal_sections": slices, "includes_prediction_or_room_gt": False,
        "limitations": ["No automatic room polygons are produced.", "Vertical-surface plan projection may include wall/window/door frame geometry; human review decides architectural boundary.", "3D views are stage mesh only and omit furniture and runtime articulated assets.", "Room annotation tool remains on the 0.85m slice; supplemental images are contextual, not a replacement for reviewing door/open-plan ambiguity."],
        "outputs_sha256": {p.name: sha256(p) for p in sorted(out.iterdir()) if p.is_file() and p.name != "manifest.json"}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": manifest["status"], "output_dir": str(out), "slice_heights": slices,
                      "projected_vertical_triangles": wall_face_count}, indent=2))


if __name__ == "__main__":
    main()
