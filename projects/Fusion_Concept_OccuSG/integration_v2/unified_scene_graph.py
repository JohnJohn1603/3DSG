"""Import final-map object artifacts into an existing OccuSG room snapshot.

This adapter deliberately does not run object association. ConceptGraphs export IDs
remain the object identity; ambiguous room membership is represented without an edge.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .object_export_contract import validate_object_export
from standalone_sg.contracts.regions import Polygon2D
from standalone_sg.pipeline.contracts import GraphEdge, GraphNode, SceneGraphSnapshot
from standalone_sg.rooms.geometry import point_in_polygon
from standalone_sg.serialization.json_graph import load_snapshot, serialize_snapshot, write_snapshot


def _hash_json(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _boundary(point, polygon: Polygon2D, eps=1e-7) -> bool:
    x, y = point
    for ring in (polygon.outer, *polygon.holes):
        for i, a in enumerate(ring):
            b = ring[(i + 1) % len(ring)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            den = dx * dx + dy * dy
            t = 0.0 if den == 0 else max(0.0, min(1.0, ((x-a[0])*dx + (y-a[1])*dy) / den))
            if math.hypot(x-(a[0]+t*dx), y-(a[1]+t*dy)) <= eps:
                return True
    return False


def _segments_intersect(a, b, c, d, eps=1e-9):
    def orient(p,q,r): return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    def on(p,q,r): return (min(p[0],q[0])-eps <= r[0] <= max(p[0],q[0])+eps and
                           min(p[1],q[1])-eps <= r[1] <= max(p[1],q[1])+eps and abs(orient(p,q,r)) <= eps)
    o1,o2,o3,o4=orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
    if ((o1>eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and ((o3>eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
        return True
    return on(a,b,c) or on(a,b,d) or on(c,d,a) or on(c,d,b)


def _polygon_footprint_intersects(bounds_xy, polygon):
    x0,y0,x1,y1=bounds_xy
    corners=((x0,y0),(x1,y0),(x1,y1),(x0,y1))
    if any(point_in_polygon(p,polygon) for p in corners): return True
    rect_edges=tuple((corners[i],corners[(i+1)%4]) for i in range(4))
    for ring in (polygon.outer,*polygon.holes):
        if any(x0<=p[0]<=x1 and y0<=p[1]<=y1 for p in ring): return True
        for i,a in enumerate(ring):
            b=ring[(i+1)%len(ring)]
            if any(_segments_intersect(a,b,c,d) for c,d in rect_edges): return True
    return False


def _polygon_boundary_intersects_footprint(bounds_xy, polygon):
    x0,y0,x1,y1=bounds_xy
    corners=((x0,y0),(x1,y0),(x1,y1),(x0,y1))
    rect_edges=tuple((corners[i],corners[(i+1)%4]) for i in range(4))
    return any(_segments_intersect(a,ring[(i+1)%len(ring)],c,d)
               for ring in (polygon.outer,*polygon.holes)
               for i,a in enumerate(ring)
               for c,d in rect_edges)


def _room_polygons(snapshot):
    result = []
    for node in snapshot.nodes:
        if node.kind != "room":
            continue
        ring = node.attributes.get("outer_ring")
        if not ring:
            raise ValueError(f"room node {node.node_id} has no outer_ring")
        result.append((node, Polygon2D(tuple(tuple(p) for p in ring), tuple(tuple(tuple(p) for p in h) for h in node.attributes.get("holes", ())))))
    if not result:
        raise ValueError("room snapshot contains no room geometry")
    return result


def _room_membership(center, bounds_xy, rooms):
    x, y = center[:2]
    samples = [(x, y)]
    if bounds_xy is not None:
        x0, y0, x1, y1 = bounds_xy
        samples += [(x0,y0),(x1,y0),(x1,y1),(x0,y1),((x0+x1)/2,y0),(x1,(y0+y1)/2),((x0+x1)/2,y1),(x0,(y0+y1)/2)]
    per_sample = []
    possible = set()
    boundary_hit = False
    if bounds_xy is not None:
        for room, polygon in rooms:
            if _polygon_footprint_intersects(bounds_xy, polygon):
                possible.add(room.node_id)
                if _polygon_boundary_intersects_footprint(bounds_xy, polygon): boundary_hit=True
    for point in samples:
        owners = []
        for room, polygon in rooms:
            if point_in_polygon(point, polygon):
                possible.add(room.node_id)
                if _boundary(point, polygon):
                    boundary_hit = True
                else:
                    owners.append(room.node_id)
        per_sample.append(owners)
    if len(possible) == 1 and not boundary_hit and all(len(owners) == 1 and owners[0] in possible for owners in per_sample):
        return "assigned", next(iter(possible)), tuple(sorted(possible)), "center_and_footprint_inside_one_room"
    if possible or boundary_hit:
        return "ambiguous", None, tuple(sorted(possible)), "footprint_crosses_room_boundary_or_multiple_rooms"
    return "unassigned", None, (), "no_room_geometry_contains_object_footprint"


def _transform_box(bounds, transform):
    corners = np.array([[x,y,z,1.0] for x in (bounds[0][0],bounds[1][0])
                       for y in (bounds[0][1],bounds[1][1])
                       for z in (bounds[0][2],bounds[1][2])], dtype=np.float64)
    world = (transform @ corners.T).T[:, :3]
    return world.min(axis=0), world.max(axis=0)


def _room_geometry_digest(snapshot):
    rooms = [n for n in snapshot.nodes if n.kind == "room"]
    room_ids = {n.node_id for n in rooms}
    adjacency = [e for e in snapshot.edges if e.source_id in room_ids and e.target_id in room_ids]
    return _hash_json({"rooms":[{"id":n.node_id,"attributes":dict(n.attributes)} for n in rooms],
                       "adjacency":[{"source":e.source_id,"target":e.target_id,"relation":e.relation,"provenance":e.provenance,"directed":e.directed} for e in adjacency]})


def import_object_export(room_snapshot: SceneGraphSnapshot, export_root: Path, *, source_kind: str | None = None,
                         membership_policy: str = "geometry_footprint_xy"):
    """Return (unified snapshot, import manifest)."""
    if membership_policy not in {"geometry_footprint_xy", "unavailable_elevation_scope"}:
        raise ValueError(f"unsupported membership policy: {membership_policy}")
    export_root = Path(export_root).resolve()
    export = validate_object_export(export_root)
    provenance = export.get("provenance", {})
    declared_source = provenance.get("source_kind")
    display_source_label = provenance.get("display_label")
    if not display_source_label:
        pipeline = str(provenance.get("pipeline", ""))
        if "YOLO11n-seg" in pipeline and "ConceptGraphs reference mapping core" in pipeline:
            display_source_label = "Hybrid YOLO11n-seg + ViT-B-32 / ConceptGraphs reference mapping core"
        else:
            display_source_label = declared_source
    if declared_source not in {"reference", "legacy", "fixture"}:
        raise ValueError("export provenance.source_kind must be reference, legacy, or fixture")
    if source_kind is not None and source_kind != declared_source:
        raise ValueError(f"requested source kind {source_kind!r} conflicts with export provenance {declared_source!r}")
    if export.get("scene_id") != room_snapshot.scene_id:
        raise ValueError(f"scene_id mismatch: room={room_snapshot.scene_id!r}, objects={export.get('scene_id')!r}")
    if export.get("sequence_id") != room_snapshot.sequence_id:
        raise ValueError(f"sequence_id mismatch: room={room_snapshot.sequence_id!r}, objects={export.get('sequence_id')!r}")
    if export.get("units") != "m":
        raise ValueError("object coordinates must be in metres")

    rooms = _room_polygons(room_snapshot)
    before_room_digest = _room_geometry_digest(room_snapshot)
    keep_nodes = {n.node_id:n for n in room_snapshot.nodes if n.kind != "object"}
    keep_edges = {e for e in room_snapshot.edges if e.source_id in keep_nodes and e.target_id in keep_nodes}
    transform = np.asarray(export["T_capture_world_from_cg_world"], dtype=np.float64)
    new_ids = []
    for item in export.get("objects", []):
        export_id = item["export_id"]
        cloud_path = export_root / item["cloud_npz"]
        with np.load(cloud_path, allow_pickle=False) as payload:
            xyz = np.asarray(payload["xyz"], dtype=np.float64)
        if not np.isfinite(xyz).all() or xyz.ndim != 2 or xyz.shape[1] != 3 or not len(xyz):
            raise ValueError(f"invalid cloud points for {export_id}")
        center_cg = xyz.mean(axis=0)
        center = (transform @ np.r_[center_cg, 1.0])[:3]
        bmin, bmax = _transform_box(item["bounds_minmax"], transform)
        bounds_xy = (float(bmin[0]), float(bmin[1]), float(bmax[0]), float(bmax[1]))
        if membership_policy == "unavailable_elevation_scope":
            membership, room_id, plausible, reason = "unavailable", None, (), "unavailable_elevation_scope"
        else:
            membership, room_id, plausible, reason = _room_membership(center, bounds_xy, rooms)
        node_id = f"object:conceptgraphs:{export_id}"
        raw_label = item.get("raw_detector_class", item.get("raw_label", item.get("class_name")))
        fused_label = item.get("fused_semantic_label")
        fused_status = item.get("fused_semantic_status", "unavailable" if fused_label is None else "provided")
        attrs = {
            "external_object_id": export_id,
            "center_W": [float(v) for v in center],
            "bounds_min_W": [float(v) for v in bmin],
            "bounds_max_W": [float(v) for v in bmax],
            "extents_m": [float(v) for v in (bmax-bmin)],
            "point_count": int(len(xyz)),
            "raw_label": raw_label,
            "raw_detector_class": item.get("raw_detector_class", raw_label),
            "fused_semantic_label": fused_label,
            "fused_semantic_status": fused_status,
            "confidence": item.get("confidence"),
            "evidence": item.get("evidence", []),
            "membership_status": membership,
            "membership_reason": reason,
            "plausible_room_ids": list(plausible),
            "source_kind": declared_source,
            "source_display_label": display_source_label,
            "source_export_manifest": str(export_root / "export_manifest.json"),
            "coordinate_frame": "canonical_world_z_up",
            "units": "m",
        }
        node = GraphNode(node_id, "object", float(item.get("first_seen_s", room_snapshot.timestamp_s or 0.0)),
                         float(item.get("last_seen_s", room_snapshot.timestamp_s or 0.0)),
                         f"{declared_source}:conceptgraphs_final_map", attrs)
        existing = keep_nodes.get(node_id)
        if existing is not None and existing != node:
            raise ValueError(f"object ID collision with different provenance/content: {node_id}")
        keep_nodes[node_id] = node
        new_ids.append(node_id)
        if membership == "assigned" and room_id:
            keep_edges.add(GraphEdge(room_id, node_id, "room_contains", f"adapter:geometry:{reason}", True))

    unified = SceneGraphSnapshot(room_snapshot.schema_version, room_snapshot.scene_id,
        room_snapshot.sequence_id, room_snapshot.revision + 1, room_snapshot.timestamp_s,
        tuple(keep_nodes[k] for k in sorted(keep_nodes)), tuple(sorted(keep_edges)),
        room_snapshot.config_hash, dict(room_snapshot.stale_flags), {})
    if (unified.nodes == room_snapshot.nodes and unified.edges == room_snapshot.edges):
        unified = room_snapshot
    after_room_digest = _room_geometry_digest(unified)
    if before_room_digest != after_room_digest:
        raise AssertionError("object import changed room geometry or room adjacency")
    _check_graph(unified)
    import_manifest = {
        "schema": "occusg-unified-object-room-import/v1",
        "scene_id": unified.scene_id, "sequence_id": unified.sequence_id,
        "source_kind": declared_source,
        "source_display_label": display_source_label,
        "object_export_manifest": str(export_root / "export_manifest.json"),
        "object_export_manifest_sha256": hashlib.sha256((export_root / "export_manifest.json").read_bytes()).hexdigest(),
        "room_snapshot_revision": room_snapshot.revision,
        "unified_snapshot_revision": unified.revision,
        "room_geometry_adjacency_sha256": before_room_digest,
        "membership_policy": membership_policy,
        "object_ids": new_ids,
        "membership": {n.node_id: {"status":n.attributes["membership_status"],"room_id": next((e.source_id for e in unified.edges if e.target_id==n.node_id and e.relation=="room_contains"),None),"plausible_room_ids":n.attributes["plausible_room_ids"],"reason":n.attributes["membership_reason"]} for n in unified.nodes if n.kind=="object"},
        "association_rerun": False,
        "room_semantic_scoring_changed": False,
    }
    return unified, import_manifest


def _check_graph(snapshot):
    ids = [node.node_id for node in snapshot.nodes]
    if len(ids) != len(set(ids)):
        raise ValueError("unified graph contains duplicate node IDs")
    node_map = {node.node_id:node for node in snapshot.nodes}
    for edge in snapshot.edges:
        if edge.source_id not in node_map or edge.target_id not in node_map:
            raise ValueError(f"dangling edge {edge}")
    ownership = {}
    for edge in snapshot.edges:
        if edge.relation == "room_contains" and node_map[edge.target_id].kind == "object":
            if edge.target_id in ownership and ownership[edge.target_id] != edge.source_id:
                raise ValueError(f"conflicting room ownership for {edge.target_id}")
            ownership[edge.target_id] = edge.source_id
    for node in snapshot.nodes:
        if node.kind == "object":
            status = node.attributes.get("membership_status")
            if status == "assigned" and node.node_id not in ownership:
                raise ValueError(f"assigned object has no room_contains edge: {node.node_id}")
            if status != "assigned" and node.node_id in ownership:
                raise ValueError(f"non-assigned object has room ownership edge: {node.node_id}")


def build_unified_graph(room_snapshot_path: Path, object_export_root: Path, output_dir: Path, *, source_kind=None,
                        membership_policy: str = "geometry_footprint_xy"):
    room_snapshot_path = Path(room_snapshot_path).resolve()
    room = load_snapshot(room_snapshot_path)
    unified, manifest = import_object_export(room, object_export_root, source_kind=source_kind,
                                             membership_policy=membership_policy)
    output_dir = Path(output_dir).resolve()
    room_run_id = room_snapshot_path.parent.parent.name if room_snapshot_path.parent.name == "replay" else None
    object_export = validate_object_export(Path(object_export_root))
    object_source_run_id = object_export.get("source_run_id")
    if object_source_run_id is not None and room_run_id is not None and object_source_run_id != room_run_id:
        raise ValueError(f"run_id mismatch: room={room_run_id!r}, objects={object_source_run_id!r}")
    manifest["room_run_id"] = room_run_id
    manifest["object_source_run_id"] = object_source_run_id
    manifest["run_linkage_status"] = "matched" if object_source_run_id is not None and room_run_id is not None else "scene_sequence_only_source_run_id_unavailable"
    output_dir.mkdir(parents=True, exist_ok=False)
    write_snapshot(output_dir / "unified_snapshot.json", unified)
    viewer_renderer = _render_scene_map(unified, output_dir)
    manifest["viewer_renderer"] = viewer_renderer
    (output_dir / "manifest.json").write_text(json.dumps({**manifest,
        "room_snapshot":str(room_snapshot_path),
        "unified_graph_id":output_dir.name,
        "room_snapshot_sha256":hashlib.sha256(room_snapshot_path.read_bytes()).hexdigest(),
        "unified_snapshot_sha256":hashlib.sha256(serialize_snapshot(unified).encode()).hexdigest()},
        indent=2, sort_keys=True, ensure_ascii=False)+"\n", encoding="utf-8")
    _write_html_viewer(output_dir, unified, manifest, map_filename="object_room_map.png" if viewer_renderer == "occusg_matplotlib" else "object_room_map.svg")
    return output_dir


def _render_scene_map(snapshot, output_dir):
    """Prefer the existing OccuSG map renderer; fall back to stdlib SVG.

    The local demo should remain usable in lightweight project environments
    where matplotlib is an optional visualization dependency.
    """
    try:
        from standalone_sg.visualization.plots import render_spatial_map
    except ModuleNotFoundError as exc:
        if exc.name != "matplotlib":
            raise
        _render_spatial_svg(snapshot, output_dir / "object_room_map.svg")
        return "stdlib_svg_fallback_matplotlib_unavailable"
    render_spatial_map(snapshot, output_dir / "object_room_map.png", show_objects=True, label_kinds=("room", "object"))
    return "occusg_matplotlib"


def _render_spatial_svg(snapshot, path):
    """Small dependency-free top-down room/object map for the demo viewer."""
    rooms = _room_polygons(snapshot)
    objects = [node for node in snapshot.nodes if node.kind == "object"]
    points = [p for _, polygon in rooms for p in polygon.outer]
    points.extend((node.attributes["center_W"][0], node.attributes["center_W"][1]) for node in objects)
    min_x = min(p[0] for p in points); max_x = max(p[0] for p in points)
    min_y = min(p[1] for p in points); max_y = max(p[1] for p in points)
    span_x = max(max_x - min_x, 1e-6); span_y = max(max_y - min_y, 1e-6)
    width, height, margin = 960, 700, 48
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
    draw_w, draw_h = span_x * scale, span_y * scale
    offset_x = (width - draw_w) / 2
    offset_y = (height - draw_h) / 2

    def xy(point):
        return offset_x + (point[0] - min_x) * scale, height - (offset_y + (point[1] - min_y) * scale)

    elements = []
    for index, (room, polygon) in enumerate(rooms):
        vertices = " ".join(f"{x:.2f},{y:.2f}" for x, y in (xy(p) for p in polygon.outer))
        elements.append(f'<polygon points="{vertices}" fill="#e8eef7" fill-opacity="0.55" stroke="#334155" stroke-width="3"/>')
        cx, cy = xy(room.attributes.get("centroid_xy", polygon.outer[0]))
        elements.append(f'<text x="{cx:.2f}" y="{cy:.2f}" class="room-label">{_html(room.attributes.get("name", room.node_id))}</text>')
    colors = {"assigned": "#16845b", "ambiguous": "#d97706", "unassigned": "#64748b"}
    for node in objects:
        attrs = node.attributes
        x, y = xy(attrs["center_W"][:2])
        color = colors.get(attrs.get("membership_status"), "#64748b")
        title = _html(f'{attrs.get("external_object_id", node.node_id)} · {attrs.get("membership_status", "unknown")}')
        label = _html(str(attrs.get("external_object_id", node.node_id))[:24])
        elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="8" fill="{color}" stroke="white" stroke-width="2"><title>{title}</title></circle>')
        elements.append(f'<text x="{x + 11:.2f}" y="{y - 9:.2f}" class="object-label">{label}</text>')
    legend = '<g class="legend"><text x="24" y="28">Object membership:</text><circle cx="190" cy="23" r="7" fill="#16845b"/><text x="203" y="28">assigned</text><circle cx="300" cy="23" r="7" fill="#d97706"/><text x="313" y="28">ambiguous</text><circle cx="430" cy="23" r="7" fill="#64748b"/><text x="443" y="28">unassigned</text></g>'
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Room boundaries and object centers">
<style>text{{font-family:system-ui,sans-serif;fill:#172033}}.room-label{{font-size:18px;font-weight:700;text-anchor:middle}}.object-label{{font-size:12px}}.legend{{font-size:13px}}</style>
<rect width="100%" height="100%" fill="#fff"/>{legend}{''.join(elements)}</svg>'''
    Path(path).write_text(svg, encoding="utf-8")


def _write_html_viewer(output_dir, snapshot, manifest, *, map_filename="object_room_map.png"):
    objects = [n for n in snapshot.nodes if n.kind == "object"]
    rows = []
    ownership = {e.target_id:e.source_id for e in snapshot.edges if e.relation=="room_contains"}
    for node in objects:
        a=node.attributes
        rows.append("<tr>"+"".join(f"<td>{_html(v)}</td>" for v in (a["external_object_id"],a["source_kind"],a.get("raw_label") or "—",a.get("fused_semantic_label") or "—",a["membership_status"],ownership.get(node.node_id) or ", ".join(a["plausible_room_ids"]) or "—"))+"</tr>")
    source = _html(manifest.get("source_display_label") or manifest["source_kind"])
    html = f"""<!doctype html><meta charset='utf-8'><title>Object–room graph</title>
<style>body{{font:15px system-ui;margin:2rem;color:#172033}}img{{max-width:100%;border:1px solid #ccd}}table{{border-collapse:collapse;width:100%;margin-top:1rem}}th,td{{border:1px solid #ccd;padding:.45rem;text-align:left}}th{{background:#eef}}.tag{{font-weight:bold;color:#7046a0}}</style>
<h1>Unified object–room graph</h1><p>Source: <span class='tag'>{source}</span> · scene {_html(snapshot.scene_id)} · sequence {_html(snapshot.sequence_id)}</p>
<p>Objects preserve external IDs. Room membership is geometric; ambiguous/unassigned objects have no room edge. Room semantic scores were not changed.</p>
<img src='{_html(map_filename)}' alt='Room boundaries and object centers'><table><thead><tr><th>Object ID</th><th>Source</th><th>Raw label</th><th>Fused label</th><th>Membership</th><th>Room / plausible rooms</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>Machine-readable graph: <code>unified_snapshot.json</code> · provenance: <code>manifest.json</code></p>"""
    (output_dir / "index.html").write_text(html, encoding="utf-8")


def _html(value):
    import html
    return html.escape(str(value))
