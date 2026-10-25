"""Offline HTML viewer for ConceptGraphs pre-filter hypotheses and final map."""
from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np

from standalone_sg.serialization.json_graph import load_snapshot


def _esc(value):
    return html.escape(str(value), quote=True)


def _map_svg(snapshot, objects, cloud_root: Path, transform, *, mode: str, width=1040, height=760):
    rooms = [n for n in snapshot.nodes if n.kind == "room"]
    if not rooms:
        raise ValueError("room snapshot has no room polygons")
    transformed = []
    xy_points = []
    for room in rooms:
        ring = room.attributes.get("outer_ring")
        if not ring:
            raise ValueError(f"room {room.node_id} lacks outer_ring")
        xy_points.extend((float(p[0]), float(p[1])) for p in ring)
    for obj in objects:
        cloud = np.load(cloud_root / obj["cloud_npz"], allow_pickle=False)
        xyz = np.asarray(cloud["xyz"], dtype=np.float64)
        if xyz.ndim != 2 or xyz.shape[1] != 3 or not len(xyz) or not np.isfinite(xyz).all():
            raise ValueError(f"invalid viewer cloud for {obj.get('id')}")
        world = (transform @ np.column_stack((xyz, np.ones(len(xyz)))).T).T[:, :3]
        stride = max(1, int(np.ceil(len(world) / 650)))
        sample = world[::stride]
        lo, hi = world.min(axis=0), world.max(axis=0)
        xy_points.extend((float(p[0]), float(p[1])) for p in sample)
        transformed.append((obj, world, lo, hi, stride))
    min_x = min(p[0] for p in xy_points); max_x = max(p[0] for p in xy_points)
    min_y = min(p[1] for p in xy_points); max_y = max(p[1] for p in xy_points)
    pad_x, pad_y = max((max_x-min_x)*0.05, 0.2), max((max_y-min_y)*0.05, 0.2)
    min_x -= pad_x; max_x += pad_x; min_y -= pad_y; max_y += pad_y
    span_x, span_y, margin = max(max_x-min_x, 1e-5), max(max_y-min_y, 1e-5), 42
    scale = min((width-2*margin)/span_x, (height-2*margin)/span_y)
    draw_w, draw_h = span_x*scale, span_y*scale
    ox, oy = (width-draw_w)/2, (height-draw_h)/2

    def xy(p):
        return ox+(p[0]-min_x)*scale, height-(oy+(p[1]-min_y)*scale)

    elements = [f'<rect width="{width}" height="{height}" fill="#fff"/>']
    for room in rooms:
        ring = room.attributes["outer_ring"]
        pts = " ".join(f"{x:.2f},{y:.2f}" for x,y in (xy(p) for p in ring))
        elements.append(f'<polygon points="{pts}" fill="#e5edf7" fill-opacity=".56" stroke="#34445c" stroke-width="3"/>')
        cx,cy=xy(room.attributes.get("centroid_xy", ring[0]))
        elements.append(f'<text x="{cx:.2f}" y="{cy:.2f}" class="room-label">{_esc(room.attributes.get("name",room.node_id))}</text>')
    for obj, world, lo, hi, stride in transformed:
        oid = obj["id"]
        if mode == "pre_filter":
            color = "#16845b" if obj["status_after_filter"] == "retained" else "#dc2626"
            label = "retained" if obj["status_after_filter"] == "retained" else "filtered"
        else:
            color = "#16845b" if obj.get("membership_status") == "assigned" else "#d97706"
            label = obj.get("membership_status", "unknown")
        x0,y0=xy((lo[0],lo[1])); x1,y1=xy((hi[0],hi[1]))
        rect_x,rect_y=min(x0,x1),min(y0,y1)
        rect_w,rect_h=max(abs(x1-x0),2),max(abs(y1-y0),2)
        elements.append(f'<g class="object-geometry" data-object="{_esc(oid)}" tabindex="0" role="button" aria-label="{_esc(oid)}">')
        elements.append(f'<rect x="{rect_x:.2f}" y="{rect_y:.2f}" width="{rect_w:.2f}" height="{rect_h:.2f}" fill="{color}" fill-opacity=".12" stroke="{color}" stroke-width="2"><title>{_esc(oid)} · {label}</title></rect>')
        for point in world[::stride]:
            x,y=xy(point)
            elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.8" fill="{color}" fill-opacity=".58"/>')
        cx,cy=xy(((lo[0]+hi[0])/2,(lo[1]+hi[1])/2))
        elements.append(f'<text x="{cx+5:.2f}" y="{cy-5:.2f}" class="object-label">{_esc(oid[:24])}</text></g>')
    if mode == "pre_filter":
        legend='<circle cx="26" cy="24" r="7" fill="#16845b"/><text x="40" y="29">retained after filter</text><circle cx="190" cy="24" r="7" fill="#dc2626"/><text x="204" y="29">filtered (not merged)</text>'
    else:
        statuses = {str(obj.get("membership_status")) for obj in objects}
        if "unavailable" in statuses:
            legend='<circle cx="26" cy="24" r="7" fill="#16845b"/><text x="40" y="29">room assigned</text><circle cx="154" cy="24" r="7" fill="#d97706"/><text x="168" y="29">membership unavailable</text>'
        else:
            legend='<circle cx="26" cy="24" r="7" fill="#16845b"/><text x="40" y="29">room assigned</text><circle cx="154" cy="24" r="7" fill="#d97706"/><text x="168" y="29">ambiguous/unassigned</text>'
    elements.append(f'<g class="legend">{legend}</g>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">'
            '<style>text{font-family:system-ui,sans-serif;fill:#172033}.room-label{font-size:17px;font-weight:700;text-anchor:middle}.object-label{font-size:11px}.legend{font-size:12px}.object-geometry{cursor:pointer}.object-geometry.selected rect{stroke:#111827;stroke-width:5}</style>'
            + "".join(elements) + "</svg>")


def build_hybrid_mapping_viewer(room_snapshot_path: Path, mapping_root: Path, final_graph_root: Path, output_html: Path,
                                *, viewer_context: dict | None = None):
    mapping_root, final_graph_root, output_html = Path(mapping_root).resolve(), Path(final_graph_root).resolve(), Path(output_html).resolve()
    diag = json.loads((mapping_root / "diagnostic/diagnostic_manifest.json").read_text())
    evidence = json.loads((mapping_root / "diagnostic/evidence_manifest.json").read_text())
    export = json.loads((mapping_root / "export/export_manifest.json").read_text())
    snapshot = load_snapshot(Path(room_snapshot_path))
    final_snapshot = load_snapshot(final_graph_root / "unified_snapshot.json")
    if (snapshot.scene_id, snapshot.sequence_id) != (final_snapshot.scene_id, final_snapshot.sequence_id):
        raise ValueError("unified graph and room snapshot scene/sequence differ")
    transform = np.asarray(export["T_capture_world_from_cg_world"], dtype=np.float64)
    pre_objects = [{**row, "id": row["hypothesis_id"],
                    "raw_class_history": row.get("class_history_raw", []),
                    "cloud_npz": f"diagnostic/{row['cloud_npz']}"} for row in diag["objects"]]
    export_objects = []
    nodes = {n.node_id: n for n in final_snapshot.nodes}
    for item in export["objects"]:
        node = nodes.get(f"object:conceptgraphs:{item['export_id']}")
        if node is None:
            raise ValueError(f"final object missing from unified graph: {item['export_id']}")
        attrs = node.attributes
        ids = list(item.get("evidence", []))
        rows = [evidence["observations"][oid] for oid in ids]
        poses = {tuple(np.round(np.asarray(r["camera_pose_W_Copt"], dtype=np.float64).reshape(-1), 6)) for r in rows}
        export_objects.append({"id": item["export_id"], "cloud_npz": f"export/{item['cloud_npz']}",
            "external_object_id": item["export_id"], "membership_status": attrs.get("membership_status"),
            "membership_reason": attrs.get("membership_reason"),
            "raw_class_history": item.get("reference_class_history", []), "observation_ids": ids,
            "detection_count": len(ids), "distinct_frame_count": len({r["frame_index"] for r in rows}),
            "distinct_pose_count": len(poses), "status_after_filter": "retained",
            "postprocess_disposition": "final_reference_graph_object"})
    # Map original observation IDs to final graph IDs for membership lookup.
    final_by_obs = {oid: obj["id"] for obj in export_objects for oid in obj["observation_ids"]}
    for obj in pre_objects:
        obj["observation_assets"] = []
        for oid in obj["observation_ids"]:
            item = evidence["observations"][oid]
            obj["observation_assets"].append({**item, "observation_id": oid,
                "panels": {k: f"mapping/diagnostic/{v}" for k,v in item["panels"].items()},
                "final_object_id": final_by_obs.get(oid)})
    for obj in export_objects:
        obj["observation_assets"] = []
        for oid in obj["observation_ids"]:
            item = evidence["observations"][oid]
            obj["observation_assets"].append({**item, "observation_id": oid,
                "panels": {k: f"mapping/diagnostic/{v}" for k,v in item["panels"].items()}})
    pre_svg = _map_svg(snapshot, pre_objects, mapping_root, transform, mode="pre_filter")
    final_svg = _map_svg(final_snapshot, export_objects, mapping_root, transform, mode="final")
    # Inline map panels; evidence images remain as uniquely named sidecars.
    data = {"pre_filter": pre_objects, "final": export_objects,
            "counts": {"pre_filter": diag["counts"], "final": len(export_objects)},
            "source_label": export.get("provenance", {}).get("display_label", "Hybrid cached-perception + ConceptGraphs mapping core"),
            "scene_id": snapshot.scene_id, "sequence_id": snapshot.sequence_id,
            "viewer_context": viewer_context or {}}
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(_viewer_html(data_json, pre_svg, final_svg), encoding="utf-8")
    return output_html


def _viewer_html(data_json, pre_svg, final_svg):
    return f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hybrid object mapping — pre-filter and final</title>
<style>body{{font:15px system-ui,sans-serif;margin:20px;color:#172033;background:#f8fafc}}h1{{margin:0}}.sub{{color:#475569}}.layout{{display:grid;grid-template-columns:minmax(420px,1.3fr) minmax(350px,1fr);gap:18px}}.card{{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:14px;margin:12px 0}}button,.obj{{border:1px solid #94a3b8;border-radius:7px;background:#fff;padding:8px 12px;cursor:pointer}}button.active{{background:#0f4c81;color:white}}.obj{{display:block;width:100%;text-align:left;margin:6px 0;overflow-wrap:anywhere}}.obj .observation-ids{{display:block;font-size:11px;color:#475569;margin-top:4px}}.obj.selected{{outline:3px solid #38bdf8}}.danger{{color:#b91c1c;font-weight:700}}.good{{color:#047857;font-weight:700}}.map svg{{width:100%;height:auto;border:1px solid #cbd5e1;background:white}}.images{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.images img{{width:100%;border:1px solid #cbd5e1}}.meta{{font-size:13px;color:#334155}}.obs{{border-top:1px solid #e2e8f0;padding-top:10px;margin-top:10px;overflow-wrap:anywhere}}@media(max-width:900px){{.layout{{grid-template-columns:1fr}}}}</style>
<h1>Hybrid object mapping</h1><p class="sub">{html.escape(json.loads(data_json)['source_label'])} · scene {html.escape(json.loads(data_json)['scene_id'])} · sequence {html.escape(json.loads(data_json)['sequence_id'])}<br>Diagnostic hypotheses are not added to the final graph or used as room semantic evidence. A hypothesis retained by the filter is not thereby a confirmed physical identity.</p>
{('<div class="card"><b>Run limitation:</b> '+html.escape(str(json.loads(data_json).get('viewer_context',{}).get('display_note','')))+'<br>Membership: '+html.escape(str(json.loads(data_json).get('viewer_context',{}).get('membership_status','unavailable')))+' · room semantics: '+html.escape(str(json.loads(data_json).get('viewer_context',{}).get('room_semantics_status','unavailable')))+' · execution: '+html.escape(str(json.loads(data_json).get('viewer_context',{}).get('execution_mode','static final map')))+'.</div>') if json.loads(data_json).get('viewer_context') else ''}
<div><button id="preBtn" class="active">Before filter · {json.loads(data_json)['counts']['pre_filter']['before_filter']} hypotheses</button> <button id="finalBtn">Final map · {json.loads(data_json)['counts']['final']} objects</button></div>
<div class="layout"><section class="card"><h2 id="modeTitle">Before filter: online object hypotheses</h2><div class="map" id="preMap">{pre_svg}</div><div class="map" id="finalMap" hidden>{final_svg}</div><p class="meta">Object cloud shown as sampled XY points plus its bounds; room outlines come from the unchanged OccuSG snapshot.</p><div id="objectList"></div></section>
<section class="card"><div id="objectDetails">Select an object.</div></section></div>
<script>const DATA={data_json};let mode='pre_filter',selected=null;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function active(){{return DATA[mode]}}function selectObject(id){{selected=id;renderList();renderDetails();document.querySelectorAll('.object-geometry').forEach(x=>x.classList.toggle('selected',x.dataset.object===id))}}
function renderList(){{const list=document.getElementById('objectList');list.innerHTML='';for(const o of active()){{const b=document.createElement('button');b.className='obj'+(o.id===selected?' selected':'');const status=mode==='pre_filter'?(o.status_after_filter==='filtered'?'FILTERED · not merged':'RETAINED'):'FINAL · '+(o.membership_status||'unknown');const ids=(o.observation_ids||[]).map(esc);b.innerHTML='<b>'+esc(o.id)+'</b><br><span class="'+(o.status_after_filter==='filtered'?'danger':'good')+'">'+esc(status)+'</span> · detections '+esc(o.detection_count)+' · frames '+esc((o.frame_indices||[]).join(', '))+'<br><span class="observation-ids">'+ids.join('<br>')+'</span>';b.onclick=()=>selectObject(o.id);list.appendChild(b)}}}}
function renderDetails(){{const o=active().find(x=>x.id===selected),box=document.getElementById('objectDetails');if(!o){{box.textContent='Select an object.';return}}let status=mode==='pre_filter'?(o.status_after_filter==='filtered'?'<span class="danger">Filtered; not merged into survivors</span>':'<span class="good">Retained by reference filter</span>'):'Final reference graph object';let s='<h2>'+esc(o.id)+'</h2><p>'+status+'</p><div class="meta">Detection count: '+esc(o.detection_count)+'<br>Distinct frames: '+esc(o.distinct_frame_count)+'<br>Distinct camera poses: '+esc(o.distinct_pose_count)+' (pose count, not independent views)<br>Raw class history: '+esc((o.raw_class_history||[]).join(', '))+'<br>Postprocess: '+esc(o.postprocess_disposition||'')+(o.filter_reason?'<br>Filter reason: '+esc(o.filter_reason):'')+(o.membership_status?'<br>Membership: '+esc(o.membership_status)+' · '+esc(o.membership_reason||''):'')+'</div><h3>Observations / evidence</h3>';for(const e of (o.observation_assets||[])){{s+='<div class="obs"><b>'+esc(e.observation_id)+'</b><br>Frame '+esc(e.frame_index)+' · raw class '+esc(e.raw_class)+' · score '+esc(e.score_raw);if(e.final_object_id)s+='<br>Final object: '+esc(e.final_object_id);s+='<div class="images">';for(const [k,label] of [['rgb','RGB original'],['mask','Prediction mask'],['overlay','Mask overlay'],['crop','Mask crop']])s+='<div>'+label+'<br><img loading="lazy" src="'+esc(e.panels[k])+'" alt="'+label+'"></div>';s+='</div></div>'}}box.innerHTML=s}}
function setMode(m){{mode=m;document.getElementById('preBtn').classList.toggle('active',m==='pre_filter');document.getElementById('finalBtn').classList.toggle('active',m==='final');document.getElementById('preMap').hidden=m!=='pre_filter';document.getElementById('finalMap').hidden=m!=='final';document.getElementById('modeTitle').textContent=m==='pre_filter'?'Before filter: online object hypotheses':'Final reference object map';selected=active().length?active()[0].id:null;renderList();renderDetails()}}
document.getElementById('preBtn').onclick=()=>setMode('pre_filter');document.getElementById('finalBtn').onclick=()=>setMode('final');
document.querySelectorAll('.object-geometry').forEach(el=>{{el.addEventListener('click',()=>selectObject(el.dataset.object));el.addEventListener('keydown',ev=>{{if(ev.key==='Enter'||ev.key===' '){{ev.preventDefault();selectObject(el.dataset.object)}}}})}});
setMode('pre_filter');</script>'''
