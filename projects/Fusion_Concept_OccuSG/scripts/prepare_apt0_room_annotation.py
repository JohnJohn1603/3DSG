#!/usr/bin/env python3
"""Build a stage-mesh-only apt_0 architectural slice and lightweight annotator."""
from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _glb(path: Path):
    data = path.read_bytes()
    magic, version, total = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or total != len(data):
        raise ValueError(f"invalid GLB header: {path}")
    offset = 12; doc = None; binary = None
    while offset < total:
        size, kind = struct.unpack_from("<I4s", data, offset); offset += 8
        chunk = data[offset:offset + size]; offset += size
        if kind == b"JSON": doc = json.loads(chunk.decode("utf-8"))
        elif kind == b"BIN\x00": binary = chunk
    if doc is None or binary is None:
        raise ValueError("GLB must contain JSON and BIN chunks")
    return doc, binary


def _accessor(doc, binary, index):
    accessor = doc["accessors"][index]
    view = doc["bufferViews"][accessor["bufferView"]]
    component = accessor["componentType"]
    dtype = {5121: np.dtype("u1"), 5123: np.dtype("<u2"), 5125: np.dtype("<u4"), 5126: np.dtype("<f4")}[component]
    dimensions = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[accessor["type"]]
    count = int(accessor["count"]); offset = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
    stride = int(view.get("byteStride", dtype.itemsize * dimensions))
    if stride == dtype.itemsize * dimensions:
        values = np.frombuffer(binary, dtype=dtype, count=count * dimensions, offset=offset).reshape(count, dimensions)
    else:
        values = np.empty((count, dimensions), dtype=dtype)
        for i in range(count):
            values[i] = np.frombuffer(binary, dtype=dtype, count=dimensions, offset=offset + i * stride)
    return values


def _local_matrix(node):
    if "matrix" in node:
        return np.asarray(node["matrix"], dtype=np.float64).reshape((4, 4), order="F")
    t = np.asarray(node.get("translation", [0, 0, 0]), dtype=np.float64)
    s = np.asarray(node.get("scale", [1, 1, 1]), dtype=np.float64)
    x, y, z, w = np.asarray(node.get("rotation", [0, 0, 0, 1]), dtype=np.float64)
    r = np.array([
        [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
        [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
        [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)],
    ])
    m = np.eye(4); m[:3, :3] = r @ np.diag(s); m[:3, 3] = t
    return m


def stage_triangles(path: Path):
    doc, binary = _glb(path)
    nodes = doc["nodes"]
    scene_index = int(doc.get("scene", 0))
    roots = doc["scenes"][scene_index]["nodes"]
    result = []

    def visit(index, parent):
        node = nodes[index]; transform = parent @ _local_matrix(node)
        if "mesh" in node:
            for primitive in doc["meshes"][node["mesh"]]["primitives"]:
                pos = _accessor(doc, binary, primitive["attributes"]["POSITION"]).astype(np.float64)
                if "indices" in primitive:
                    indices = _accessor(doc, binary, primitive["indices"]).reshape(-1).astype(np.int64)
                else:
                    indices = np.arange(len(pos), dtype=np.int64)
                triangles = indices.reshape(-1, 3)
                homogeneous = np.column_stack((pos, np.ones(len(pos))))
                world = (transform @ homogeneous.T).T[:, :3]
                result.append((str(node.get("name", f"node-{index}")), world[triangles]))
        for child in node.get("children", []):
            visit(int(child), transform)

    identity = np.eye(4)
    for root in roots:
        visit(int(root), identity)
    return result


def slice_segments(triangles, height_y: float):
    segments = set(); bounds = []
    for name, tris in triangles:
        bounds.append(tris.reshape(-1, 3))
        for tri in tris:
            hits = []
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                da, db = float(a[1] - height_y), float(b[1] - height_y)
                if abs(da) < 1e-8:
                    hits.append((float(a[0]), -float(a[2])))
                if da * db < -1e-12:
                    t = (height_y - float(a[1])) / (float(b[1]) - float(a[1]))
                    p = a + t * (b - a)
                    hits.append((float(p[0]), -float(p[2])))
            unique = sorted(set((round(x, 4), round(y, 4)) for x, y in hits))
            if len(unique) >= 2:
                # A triangle intersects a plane in at most one segment. More
                # than two distinct values are coplanar/tolerance degeneracy.
                a, b = unique[0], unique[-1]
                if math.dist(a, b) > 0.004:
                    seg = tuple(sorted((a, b)))
                    segments.add(seg)
    allp = np.concatenate(bounds, axis=0)
    return sorted(segments), allp


def _svg_and_html(segments, points, slice_y, stage_path: Path, stage_hash, html_path: Path, svg_path: Path, manifest_path: Path):
    xmin, xmax = float(points[:, 0].min()), float(points[:, 0].max())
    ymin, ymax = -float(points[:, 2].max()), -float(points[:, 2].min())
    pad = 0.45; xmin -= pad; xmax += pad; ymin -= pad; ymax += pad
    width, height = xmax - xmin, ymax - ymin
    path_data = " ".join(f"M {a[0]:.4f} {a[1]:.4f} L {b[0]:.4f} {b[1]:.4f}" for a, b in segments)
    view_box = f"{xmin:.5f} {-ymax:.5f} {width:.5f} {height:.5f}"
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" width="1200" height="900">
<rect x="{xmin}" y="{-ymax}" width="{width}" height="{height}" fill="#fbfbf9"/>
<g transform="scale(1,-1)"><path d="{path_data}" fill="none" stroke="#20242a" stroke-width="0.035" vector-effect="non-scaling-stroke"/></g>
</svg>'''
    svg_path.write_text(svg, encoding="utf-8")
    try:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1600, 1400), "#fbfbf9")
        draw = ImageDraw.Draw(image)
        def pix(x, y):
            return (int(round((x - xmin) / width * 1540 + 30)),
                    int(round((ymax - y) / height * 1340 + 25)))
        for gx in range(math.ceil(xmin), math.floor(xmax) + 1):
            draw.line([pix(gx, ymin), pix(gx, ymax)], fill="#e7e8e6", width=1)
            draw.text((pix(gx, ymin)[0], 1370), str(gx), fill="#555")
        for gy in range(math.ceil(ymin), math.floor(ymax) + 1):
            draw.line([pix(xmin, gy), pix(xmax, gy)], fill="#e7e8e6", width=1)
            draw.text((3, pix(xmin, gy)[1]), str(gy), fill="#555")
        for a, b in segments:
            draw.line([pix(*a), pix(*b)], fill="#20242a", width=2)
        draw.text((20, 5), f"ReplicaCAD apt_0 stage-only horizontal slice: Habitat Y={slice_y:.2f} m | canonical XY (m)", fill="#111")
        image.save(html_path.parent / "apt0_architectural_stage_slice.png")
    except ImportError:
        pass
    html = '''<!doctype html><html><head><meta charset="utf-8"><title>apt_0 room GT annotation</title>
<style>body{margin:0;font:14px sans-serif;display:grid;grid-template-columns:300px 1fr;height:100vh;color:#20242a}aside{padding:14px;overflow:auto;border-right:1px solid #bbb}main{min-width:0;background:#f4f4f2}svg{width:100%;height:100%;touch-action:none}button,input,select{margin:4px 0;padding:6px;width:100%;box-sizing:border-box}label{display:block;margin-top:8px}.help{font-size:12px;line-height:1.5;color:#444}.room{fill:#38a8ff22;stroke:#0876bd;stroke-width:.025;vector-effect:non-scaling-stroke}.ignore{fill:#ff333322;stroke:#ce3030;stroke-width:.025;vector-effect:non-scaling-stroke}.door{stroke:#cb721b;stroke-width:.06;vector-effect:non-scaling-stroke}.preview{fill:none;stroke:#0876bd;stroke-width:.025;stroke-dasharray:.08 .05;vector-effect:non-scaling-stroke}.grid{stroke:#aaa;stroke-width:.008;vector-effect:non-scaling-stroke}.lbl{font:0.16px sans-serif;paint-order:stroke;stroke:white;stroke-width:.04;stroke-linejoin:round}.stage{fill:none;stroke:#242a30;stroke-width:.035;vector-effect:non-scaling-stroke}</style></head><body>
<aside><h2>apt_0 architectural GT</h2><p class="help">Nền là lát cắt stage mesh nguồn ở độ cao Habitat Y = SLICE_Y m; không có occupancy, room prediction hay furniture. Tọa độ lưu là canonical world XY (m), với canonical Y = −Habitat Z. Trước khi lưu, xác nhận mặt trong tường, cửa, corridor và open-plan theo protocol.</p>
<label>Feature type<select id="kind"><option value="room">Room footprint</option><option value="doorway">Doorway threshold</option><option value="ignore">Ignore / ambiguous area</option></select></label>
<label>ID<input id="fid" value="room_1"></label><label>Function label (optional; separate review)<input id="label" placeholder="kitchen / bedroom / ..."></label>
<label>Function-label review<select id="labelReviewStatus"><option value="uncertain">Uncertain</option><option value="ai_assisted_provisional">Provisional draft</option></select></label><label><input id="labelVerified" type="checkbox" style="width:auto"> I independently confirm this functional label</label>
<label>Adjacent room A (doorway only)<input id="roomA"></label><label>Adjacent room B (doorway only)<input id="roomB"></label>
<label>Review status<select id="reviewStatus"><option value="uncertain">Uncertain — evidence insufficient</option><option value="ai_assisted_provisional">Provisional draft</option></select></label><label>Reviewer name (required to confirm)<input id="reviewer"></label><label><input id="verified" type="checkbox" style="width:auto"> I reviewed this geometry and confirm it</label>
<button id="begin">Begin feature</button><button id="finish">Finish feature</button><button id="undo">Undo last point</button><button id="undoFeature">Remove last feature</button>
<button id="save">Download annotation files</button><label>Load previous room GeoJSON<input id="load" type="file" accept=".json,.geojson,application/json"></label>
<p id="status">Ready.</p><p class="help">Click points on the plan. Room/ignore features need ≥3 points; doorway uses two jamb-to-jamb threshold endpoints. Zoom with mouse wheel. Saving downloads three JSONs; review them, then place into evaluation/gt/apt_0/. Unresolved details must remain uncertain.</p>
<hr><p class="help">Protocol thresholds are fixed before metrics: room raster 0.05 m, boundary tolerance 0.10 m, visible object mask ≥64 px, object mask IoU 0.50. This drawing is not yet human-verified GT. Scroll to zoom; click to add vertices.</p></aside>
<main><svg id="canvas" viewBox="__VIEWBOX__" xmlns="http://www.w3.org/2000/svg"><rect x="__XMIN__" y="__SVG_YMIN__" width="__WIDTH__" height="__HEIGHT__" fill="#fbfbf9"/><g id="grid" transform="scale(1,-1)"></g><g transform="scale(1,-1)"><path class="stage" d="__PATH__"/></g><g id="features" transform="scale(1,-1)"></g><g id="preview" transform="scale(1,-1)"></g><g id="labels"></g></svg></main>
<script>
const svg=document.getElementById('canvas'), features=document.getElementById('features'), preview=document.getElementById('preview'), labels=document.getElementById('labels');
const V={xmin:__XMIN__,ymin:__CANON_YMIN__,xmax:__XMAX__,ymax:__CANON_YMAX__}; let items=[],active=false,pts=[];
function worldPoint(ev){const p=svg.createSVGPoint();p.x=ev.clientX;p.y=ev.clientY;const q=p.matrixTransform(svg.getScreenCTM().inverse());return [q.x,-q.y]}
function say(s){document.getElementById('status').textContent=s}
function render(){features.replaceChildren();labels.replaceChildren();for(const it of items){const g=document.createElementNS('http://www.w3.org/2000/svg','polygon');g.setAttribute('points',it.coords.map(p=>p.join(',')).join(' '));g.setAttribute('class',it.type==='ignore'?'ignore':'room');features.appendChild(g);if(it.type==='doorway'){const l=document.createElementNS('http://www.w3.org/2000/svg','line');l.setAttribute('x1',it.coords[0][0]);l.setAttribute('y1',it.coords[0][1]);l.setAttribute('x2',it.coords[1][0]);l.setAttribute('y2',it.coords[1][1]);l.setAttribute('class','door');features.replaceChild(l,g)}const cx=it.coords.reduce((a,p)=>a+p[0],0)/it.coords.length,cy=it.coords.reduce((a,p)=>a+p[1],0)/it.coords.length;const t=document.createElementNS('http://www.w3.org/2000/svg','text');t.setAttribute('x',cx);t.setAttribute('y',-cy);t.setAttribute('class','lbl');t.textContent=it.id;labels.appendChild(t)} drawPreview()}
function drawPreview(){preview.replaceChildren();if(!pts.length)return;const p=document.createElementNS('http://www.w3.org/2000/svg','polyline');p.setAttribute('points',pts.map(v=>v.join(',')).join(' '));p.setAttribute('class','preview');preview.appendChild(p)}
document.getElementById('begin').onclick=()=>{pts=[];active=true;say('Click points on the architectural plan.')};
svg.addEventListener('click',ev=>{if(!active)return;const p=worldPoint(ev);pts.push(p);drawPreview();say(`Point ${pts.length}: (${p[0].toFixed(3)}, ${p[1].toFixed(3)}) m`)});
document.getElementById('undo').onclick=()=>{pts.pop();drawPreview()};
document.getElementById('finish').onclick=()=>{if(!active)return;const kind=document.getElementById('kind').value,min=kind==='doorway'?2:3;if(pts.length<min){say(`Need at least ${min} points`);return}const id=document.getElementById('fid').value||`feature_${items.length+1}`;if(items.some(x=>x.id===id)){say('Feature ID must be unique.');return}const area=pts.length<3?1:Math.abs(pts.reduce((a,p,i)=>{const q=pts[(i+1)%pts.length];return a+p[0]*q[1]-q[0]*p[1]},0))/2;if(kind!=='doorway'&&area<0.002){say('Polygon area is degenerate.');return}const reviewer=document.getElementById('reviewer').value.trim(),confirmed=document.getElementById('verified').checked&&reviewer,label=document.getElementById('label').value||null,labelConfirmed=kind==='room'&&label&&document.getElementById('labelVerified').checked&&reviewer;items.push({type:kind,id,label,function_label_status:labelConfirmed?'human_verified':(label?document.getElementById('labelReviewStatus').value:'uncertain'),room_a:document.getElementById('roomA').value||null,room_b:document.getElementById('roomB').value||null,coords:pts.map(p=>[+p[0].toFixed(4),+p[1].toFixed(4)]),status:confirmed?'human_verified':document.getElementById('reviewStatus').value,reviewer:confirmed?reviewer:null,function_label_reviewer:labelConfirmed?reviewer:null});active=false;pts=[];render();say('Feature recorded. Geometry and function label have separate review status.')};
document.getElementById('undoFeature').onclick=()=>{items.pop();render()};
function download(name,obj){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(obj,null,2)],{type:'application/json'}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
document.getElementById('save').onclick=()=>{const rooms=items.filter(x=>x.type==='room'),ign=items.filter(x=>x.type==='ignore'),doors=items.filter(x=>x.type==='doorway');const status=!rooms.length?'not_annotated':rooms.every(x=>x.status==='human_verified')?'human_verified':rooms.some(x=>x.status==='uncertain')?'contains_uncertain':'ai_assisted_provisional';download('room_polygons.geojson',{type:'FeatureCollection',name:'apt_0_architectural_room_gt',metadata:{schema_version:1,scene_id:'apt_0',status,coordinate_frame:'canonical_world_z_up_xy_m',units:'m',source:'stage mesh slice; reviewed annotation',prediction_hidden:true},features:rooms.map(x=>({type:'Feature',properties:{room_id:x.id,function_label:x.label,function_label_status:x.function_label_status||'uncertain',function_label_reviewer:x.function_label_reviewer||null,annotation_status:x.status,reviewer:x.reviewer},geometry:{type:'Polygon',coordinates:[[...x.coords,x.coords[0]]]}}))});download('ignore_regions.geojson',{type:'FeatureCollection',metadata:{schema_version:1,coordinate_frame:'canonical_world_z_up_xy_m',units:'m'},features:ign.map(x=>({type:'Feature',properties:{id:x.id,status:x.status,reviewer:x.reviewer},geometry:{type:'Polygon',coordinates:[[...x.coords,x.coords[0]]]}}))});const doorStatus=doors.length&&doors.every(x=>x.status==='human_verified')?'human_verified':'review_required';download('doorways_and_adjacency.json',{schema_version:1,scene_id:'apt_0',status:doorStatus,coordinate_frame:'canonical_world_z_up_xy_m',units:'m',doorways:doors.map(x=>({doorway_id:x.id,threshold_xy_m:x.coords,room_a:x.room_a,room_b:x.room_b,status:x.status,reviewer:x.reviewer})),adjacency:doors.filter(x=>x.room_a&&x.room_b).map(x=>({room_a:x.room_a,room_b:x.room_b,doorway_id:x.id,status:x.status}))});download('annotation_review.json',{schema_version:1,scene_id:'apt_0',status,human_review_complete:status==='human_verified'&&doors.length>0&&doorStatus==='human_verified',review_instruction:'Boundary, doorway, function labels, and object-room membership are separate decisions. Keep uncertain for unresolved room/wall/door/open-plan evidence.',items:items.map(x=>({feature_id:x.id,feature_type:x.type,status:x.status,reviewer:x.reviewer,function_label:x.label,function_label_status:x.function_label_status||'uncertain',function_label_reviewer:x.function_label_reviewer||null})),decisions_pending:['inside-wall-face convention','doorway thresholds','corridor policy','open-plan partition','functional room labels (review separately from geometry)','object-room assignments (separate annotation)']});say('Downloaded room polygons, doorway adjacency, ignore regions, and review record.')};
document.getElementById('load').onchange=async ev=>{const f=ev.target.files[0];if(!f)return;const d=JSON.parse(await f.text());items=(d.features||[]).map((x,i)=>({type:'room',id:x.properties.room_id||`room_${i+1}`,label:x.properties.function_label||null,function_label_status:x.properties.function_label_status||'uncertain',function_label_reviewer:x.properties.function_label_reviewer||null,coords:x.geometry.coordinates[0].slice(0,-1),status:x.properties.annotation_status||'uncertain',reviewer:x.properties.reviewer||null}));render();say(`Loaded ${items.length} room polygons.`)};
svg.addEventListener('wheel',ev=>{ev.preventDefault();const p=svg.createSVGPoint();p.x=ev.clientX;p.y=ev.clientY;const q=p.matrixTransform(svg.getScreenCTM().inverse());const v=svg.viewBox.baseVal,f=ev.deltaY<0?0.85:1.18,nw=v.width*f,nh=v.height*f;svg.setAttribute('viewBox',`${q.x-(q.x-v.x)*f} ${q.y-(q.y-v.y)*f} ${nw} ${nh}`)},{passive:false});
// Add world-coordinate grid ticks. Geometry group flips Y; labels remain upright.
const grid=document.getElementById('grid'),step=1;for(let x=Math.ceil(V.xmin);x<=V.xmax;x+=step){const l=document.createElementNS('http://www.w3.org/2000/svg','line');l.setAttribute('x1',x);l.setAttribute('x2',x);l.setAttribute('y1',-V.ymin);l.setAttribute('y2',-V.ymax);l.setAttribute('class','grid');grid.appendChild(l)}for(let y=Math.ceil(V.ymin);y<=V.ymax;y+=step){const l=document.createElementNS('http://www.w3.org/2000/svg','line');l.setAttribute('x1',V.xmin);l.setAttribute('x2',V.xmax);l.setAttribute('y1',-y);l.setAttribute('y2',-y);l.setAttribute('class','grid');grid.appendChild(l)}
</script></body></html>'''
    replacements = {
        "__VIEWBOX__": view_box,
        "__XMIN__": f"{xmin:.5f}", "__SVG_YMIN__": f"{-ymax:.5f}",
        "__CANON_YMIN__": f"{ymin:.5f}", "__XMAX__": f"{xmax:.5f}",
        "__CANON_YMAX__": f"{ymax:.5f}", "__WIDTH__": f"{width:.5f}",
        "__HEIGHT__": f"{height:.5f}", "__PATH__": path_data,
        "SLICE_Y": f"{slice_y:.3f}",
    }
    for marker, value in replacements.items():
        html = html.replace(marker, value)
    html_path.write_text(html, encoding="utf-8")
    manifest = {"schema_version": 1, "kind": "apt0_architectural_stage_mesh_slice", "status": "annotation_background_only",
                "stage_mesh": str(stage_path), "stage_sha256": stage_hash, "slice_height_habitat_y_m": slice_y,
                "coordinate_frame": "canonical_world_z_up_xy_m", "mapping": "canonical_x=Habitat_X; canonical_y=-Habitat_Z; metres",
                "bounds_world_xy_m": [xmin, ymin, xmax, ymax], "segment_count": len(segments),
                "prediction_or_occupancy_used": False, "furniture_used": False,
                "limitations": ["This is a horizontal cross-section of the architectural stage mesh, not an orthographic floor rendering.", "Select another slice height or consult 3D/context images at ambiguous door/open-plan boundaries."]}
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage-mesh", type=Path, required=True)
    ap.add_argument("--slice-y", type=float, default=0.85, help="Habitat Y-up height in metres")
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    triangles = stage_triangles(args.stage_mesh.resolve())
    segments, points = slice_segments(triangles, args.slice_y)
    report = _svg_and_html(segments, points, args.slice_y, args.stage_mesh.resolve(), __import__("hashlib").sha256(args.stage_mesh.read_bytes()).hexdigest(),
                           out / "apt0_room_annotation.html", out / "apt0_architectural_stage_slice.svg", out / "stage_slice_manifest.json")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
