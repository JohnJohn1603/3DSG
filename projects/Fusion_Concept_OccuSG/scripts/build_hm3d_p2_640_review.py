#!/usr/bin/env python3
"""Build a blind, development-only review package from one locked C2/P2 run.

This reads capture RGB and authoritative P2 masks only. It never reads object
hypotheses, admission/filter outputs, room labels, or prior annotations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_hm3d_p2_blind_review import expected_crop, expected_overlay, sha256, write_json

RUN_ID_DEFAULT = "hm3d00802_main29_640_quality_v1_attempt2"
PANEL_KINDS = ("rgb", "overlay", "crop", "mask")
REVIEW_SCHEMA = "hm3d-p2-640-development-review/v1"
RESPONSE_SCHEMA = "hm3d-p2-640-development-review-response/v1"
PAGE_SIZE = 10


def _pose_w_copt(capture: dict, metadata: dict) -> np.ndarray:
    world_from_habitat = np.asarray(capture["canonical_from_habitat"], dtype=np.float64)
    optical_from_habitat_sensor = np.diag([1.0, -1.0, -1.0, 1.0])
    position = np.asarray(metadata["sensor_position_habitat"], dtype=np.float64)
    rotation = np.asarray(metadata["sensor_rotation_habitat"], dtype=np.float64)
    pose = np.eye(4, dtype=np.float64)
    pose[:3, 3] = (world_from_habitat @ np.r_[position, 1.0])[:3]
    pose[:3, :3] = world_from_habitat[:3, :3] @ rotation @ optical_from_habitat_sensor[:3, :3]
    return pose


def _asset_name(ordinal: int, observation_id: str, kind: str) -> str:
    token = hashlib.sha256(observation_id.encode("utf-8")).hexdigest()[:14]
    return f"obs_{ordinal:03d}_{token}_{kind}.png"


def build_html(rows: list[dict], *, run_id: str, population_sha256: str) -> str:
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HM3D 00802 · 640×480 P2 development review</title>
<style>
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033;background:#eef2f7}*{box-sizing:border-box}body{margin:0}header{background:#102b48;color:white;padding:18px 24px}header h1{margin:0 0 7px;font-size:23px}header p{margin:5px 0;color:#d9e5f2}.wrap{max-width:1600px;margin:auto;padding:15px}.toolbar,.card,.compare{background:white;border:1px solid #cbd5e1;border-radius:10px;padding:12px;margin:0 0 12px;box-shadow:0 2px 8px #0f172a0a}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}button,.filebutton{font:inherit;padding:7px 11px;border:1px solid #94a3b8;background:white;border-radius:7px;cursor:pointer;color:#172033}button.primary{background:#155e75;color:white;border-color:#155e75}button:disabled{opacity:.45;cursor:not-allowed}.status{margin-left:auto;color:#475569}.pageinfo{font-weight:650}.card h2{font-size:17px;margin:0 0 5px;overflow-wrap:anywhere}.provenance{color:#475569;font-size:13px;margin:0 0 10px}.panels{display:grid;grid-template-columns:2fr 2fr 1fr 1fr;gap:9px}.panel{min-width:0}.panel h3{font-size:13px;margin:0 0 5px;color:#334155}.panel img{display:block;width:100%;height:245px;object-fit:contain;background:#111827;border-radius:6px}.panel.crop img{height:245px}.annot{display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:9px;margin-top:10px}.annot label{display:block;font-size:13px;font-weight:650}.annot input,.annot select,.annot textarea{font:inherit;width:100%;margin-top:4px;padding:7px;border:1px solid #94a3b8;border-radius:6px;background:white}.annot textarea{min-height:38px;resize:vertical}.compare-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px}.compare-item{border:1px solid #cbd5e1;border-radius:7px;padding:8px}.compare-item h3{font-size:13px;overflow-wrap:anywhere}.compare-item img{width:100%;height:200px;object-fit:contain;background:#111827}.checkline{display:flex;align-items:center;gap:7px;margin-top:8px;font-size:13px}.checkline input{width:auto}.warning{color:#8b3c09;font-weight:650}.hidden{display:none!important}.foot{font-size:13px;color:#475569}@media(max-width:900px){.panels{grid-template-columns:1fr 1fr}.annot{grid-template-columns:1fr 1fr}}@media(max-width:580px){.panels,.annot{grid-template-columns:1fr}.panel img{height:220px}.status{margin-left:0;width:100%}}
</style></head><body>
<header><h1>Blind P2 review · HM3D 00802 · 640×480</h1>
<p class="warning">Development review; not a holdout. This gallery contains all 75 unique P2 observations from one locked capture.</p>
<p>Review each observation once. Identity token, class, and mask purity are separate judgments. Reuse one token only when you judge two observations to show the same physical object. Uncertain and unreviewed are allowed.</p>
<p>The gallery hides predicted classes, hypothesis/group IDs, filter status, consensus, and room scores. It does not infer same-frame duplicates; those will be derived from your identity annotations.</p></header>
<main class="wrap"><section class="toolbar">
<button id="prev">Previous page</button><span class="pageinfo" id="pageInfo"></span><button id="next">Next page</button>
<button id="compare">Compare selected (max 4)</button><button id="clearCompare">Clear comparison</button>
<button id="export" class="primary">Save / export draft JSON</button><label class="filebutton">Continue from JSON<input id="importFile" type="file" accept="application/json,.json" hidden></label>
<span id="status" class="status"></span></section>
<section id="comparePanel" class="compare hidden"><h2>Side-by-side comparison</h2><div id="compareGrid" class="compare-grid"></div></section>
<div id="cards"></div>
<p class="foot">Run: __RUN_ID__ · population manifest SHA-256: __POP_HASH__. Panels are derived from current-run RGB and P2 masks only. No prior-resolution annotations are loaded or joined.</p></main>
<script>
const DATA=DATA_JSON, RUN_ID=__RUN_ID__, POP_HASH=__POP_HASH__, PAGE_SIZE=10;
const LABEL={rgb:"Full RGB",overlay:"RGB + mask overlay",crop:"Mask crop",mask:"Binary mask"};
const IDS=DATA.map(x=>x.observation_id), state=new Map(), compareIds=new Set();
let page=0;
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
const key=id=>"hm3d-p2-640-review:"+RUN_ID+":"+POP_HASH+":"+id;
function initial(d){return {observation_id:d.observation_id,identity_status:"unreviewed",identity_token:"",class_status:"unreviewed",class_label:"",mask_purity:"unreviewed",notes:""};}
function getRow(d){if(!state.has(d.observation_id)){let row=initial(d);try{const saved=localStorage.getItem(key(d.observation_id));if(saved)row={...row,...JSON.parse(saved)}}catch{}state.set(d.observation_id,row)}return state.get(d.observation_id)}
function save(id,patch){const d=DATA.find(x=>x.observation_id===id),row={...getRow(d),...patch};state.set(id,row);try{localStorage.setItem(key(id),JSON.stringify(row))}catch{}updateStatus()}
function updateStatus(){let complete=0,uncertain=0;for(const d of DATA){const r=getRow(d);if(r.identity_status!=="unreviewed"&&r.class_status!=="unreviewed"&&r.mask_purity!=="unreviewed")complete++;if([r.identity_status,r.class_status,r.mask_purity].includes("uncertain"))uncertain++}document.getElementById("status").textContent=`All fields reviewed ${complete}/${DATA.length}; uncertain on at least one field ${uncertain}; autosaved in this browser.`}
function select(id,keyName,label,choices,row){return `<label>${label}<select data-id="${esc(id)}" data-key="${keyName}">${choices.map(([v,t])=>`<option value="${v}" ${row[keyName]===v?"selected":""}>${t}</option>`).join("")}</select></label>`}
function render(){const start=page*PAGE_SIZE, shown=DATA.slice(start,start+PAGE_SIZE);document.getElementById("pageInfo").textContent=`Page ${page+1}/${Math.ceil(DATA.length/PAGE_SIZE)} · observations ${start+1}–${start+shown.length} of ${DATA.length}`;document.getElementById("prev").disabled=page===0;document.getElementById("next").disabled=start+PAGE_SIZE>=DATA.length;
 const root=document.getElementById("cards");root.innerHTML=shown.map(d=>{const id=d.observation_id,r=getRow(d);return `<article class="card" id="${d.dom_id}"><h2>${esc(id)}</h2><p class="provenance">Frame ${d.frame_index} · ${esc(d.frame_id)} · ${Number(d.timestamp_s).toFixed(3)} s · canonical camera position (m): ${d.camera_position_W_m.map(x=>Number(x).toFixed(3)).join(", ")}</p><div class="panels">${["rgb","overlay","crop","mask"].map(k=>`<div class="panel ${k}"><h3>${LABEL[k]}</h3><a href="${esc(d.panels[k].path)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(d.panels[k].path)}" alt="${LABEL[k]} for ${esc(id)}"></a></div>`).join("")}</div><label class="checkline"><input type="checkbox" class="compareCheck" data-id="${esc(id)}" ${compareIds.has(id)?"checked":""}>Add to comparison queue</label><div class="annot">${select(id,"identity_status","Physical identity",[["unreviewed","Unreviewed"],["confirmed","Certain / assign token"],["uncertain","Uncertain"]],r)}<label>Identity token (same token = same object)<input data-id="${esc(id)}" data-key="identity_token" value="${esc(r.identity_token)}" placeholder="e.g. object_1"></label>${select(id,"class_status","Class",[["unreviewed","Unreviewed"],["confirmed","Certain / enter class"],["uncertain","Uncertain"]],r)}<label>Class, judged independently<input data-id="${esc(id)}" data-key="class_label" value="${esc(r.class_label)}" placeholder="Your class label"></label>${select(id,"mask_purity","Mask purity",[["unreviewed","Unreviewed"],["pure_single_object","Pure: one object"],["mixed_multiple_objects","Mixed: multiple objects"],["invalid_or_misaligned","Invalid / misaligned"],["uncertain","Uncertain"]],r)}<label>Optional visual notes<textarea data-id="${esc(id)}" data-key="notes">${esc(r.notes)}</textarea></label></div></article>`}).join("");
 root.querySelectorAll("[data-id][data-key]").forEach(el=>{const fn=()=>save(el.dataset.id,{[el.dataset.key]:el.value});el.addEventListener("input",fn);el.addEventListener("change",fn)});root.querySelectorAll(".compareCheck").forEach(el=>el.addEventListener("change",()=>{el.checked?compareIds.add(el.dataset.id):compareIds.delete(el.dataset.id)}));updateStatus();window.scrollTo({top:0,behavior:"smooth"})}
function showCompare(){const ids=[...compareIds];if(ids.length<1||ids.length>4){alert("Select 1–4 observations; selection can span pages.");return}const grid=document.getElementById("compareGrid");grid.innerHTML=ids.map(id=>{const d=DATA.find(x=>x.observation_id===id);return `<div class="compare-item"><h3>${esc(id)} · frame ${d.frame_index}</h3>${["rgb","overlay","crop"].map(k=>`<p>${LABEL[k]}<img src="${esc(d.panels[k].path)}" alt="${LABEL[k]}"></p>`).join("")}</div>`}).join("");document.getElementById("comparePanel").classList.remove("hidden")}
function response(){return {schema:"hm3d-p2-640-development-review-response/v1",review_type:"development_not_holdout",run_id:RUN_ID,population_manifest_sha256:POP_HASH,review_source:"human visual review of this run's RGB and P2 masks",created_utc:new Date().toISOString(),observations:DATA.map(d=>getRow(d))}}
function download(){const blob=new Blob([JSON.stringify(response(),null,2)+"\n"],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="hm3d00802_p2_640_review_response_v1.json";a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
function importResponse(obj){if(obj.schema!=="hm3d-p2-640-development-review-response/v1"||obj.run_id!==RUN_ID||obj.population_manifest_sha256!==POP_HASH||!Array.isArray(obj.observations))throw Error("Schema/run/population hash does not match this gallery.");const got=obj.observations.map(x=>x.observation_id);if(got.length!==IDS.length||new Set(got).size!==got.length||got.some(x=>!IDS.includes(x)))throw Error("Response must contain exactly one row for each of the 75 locked observations.");const valid={identity_status:["unreviewed","confirmed","uncertain"],class_status:["unreviewed","confirmed","uncertain"],mask_purity:["unreviewed","pure_single_object","mixed_multiple_objects","invalid_or_misaligned","uncertain"]};for(const row of obj.observations){for(const [field,values]of Object.entries(valid))if(!values.includes(row[field]))throw Error("Invalid "+field+" for "+row.observation_id);const clean={...initial(DATA.find(d=>d.observation_id===row.observation_id)),...row};state.set(clean.observation_id,clean);try{localStorage.setItem(key(clean.observation_id),JSON.stringify(clean))}catch{}}
 page=Math.min(page,Math.ceil(DATA.length/PAGE_SIZE)-1);render();alert("Review draft loaded. Continue where you left off and export a new checkpoint when ready.")}
document.getElementById("prev").onclick=()=>{page=Math.max(0,page-1);render()};document.getElementById("next").onclick=()=>{page=Math.min(Math.ceil(DATA.length/PAGE_SIZE)-1,page+1);render()};document.getElementById("compare").onclick=showCompare;document.getElementById("clearCompare").onclick=()=>{compareIds.clear();document.getElementById("comparePanel").classList.add("hidden");render()};document.getElementById("export").onclick=download;document.getElementById("importFile").onchange=async e=>{const f=e.target.files?.[0];if(!f)return;try{importResponse(JSON.parse(await f.text()))}catch(err){alert("Could not continue review: "+err.message)}e.target.value=""};render();
</script></body></html>'''.replace("DATA_JSON", data).replace("__RUN_ID__", json.dumps(run_id)).replace("__POP_HASH__", json.dumps(population_sha256))


def build_package(c2_run_id: str, output: Path) -> Path:
    c2_root = (ROOT / "artifacts/conceptgraphs_hybrid_runs" / c2_run_id).resolve()
    hybrid_root = (ROOT / "artifacts/conceptgraphs_hybrid_runs").resolve()
    if not c2_root.is_relative_to(hybrid_root):
        raise ValueError("Invalid C2 run path")
    out = output if output.is_absolute() else ROOT / output
    out = out.resolve()
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite existing review package: {out}")
    if out == ROOT or not out.is_relative_to(ROOT):
        raise ValueError("Review package output must be a new directory inside this workspace")

    c2_manifest_path = c2_root / "c2_run_manifest.json"
    capture_manifest_path = c2_root / "capture/capture_manifest.json"
    p2_path = c2_root / "detection_2d/detection_2d_index.json"
    required = [c2_manifest_path, capture_manifest_path, p2_path]
    for p in required:
        if not p.is_file():
            raise FileNotFoundError(f"Missing locked C2/P2 input: {p}")
    c2_doc = json.loads(c2_manifest_path.read_text(encoding="utf-8"))
    capture = json.loads(capture_manifest_path.read_text(encoding="utf-8"))
    p2 = json.loads(p2_path.read_text(encoding="utf-8"))
    if c2_doc.get("schema") != "scene-graph-c2-fresh-replica-run/v1" or c2_doc.get("run_id") != c2_run_id or c2_doc.get("status") != "completed":
        raise ValueError("Selected C2 run is not the completed manifest-identified run")
    if capture.get("status") != "pass" or p2.get("status") != "pass" or p2.get("cache_authoritative") is not True:
        raise ValueError("Capture and authoritative P2 manifests must both pass")
    scene_id, sequence_id = c2_doc.get("scene_id"), c2_doc.get("sequence_id")
    p2_frames = p2.get("frames", [])
    p2_scene = p2.get("scene_id") or (p2_frames[0].get("scene_id") if p2_frames else None)
    p2_sequence = p2.get("sequence_id") or (p2_frames[0].get("sequence_id") if p2_frames else None)
    if not scene_id or scene_id != capture.get("scene_id") or scene_id != p2_scene:
        raise ValueError("C2/capture/P2 scene IDs do not match")
    if not sequence_id or sequence_id != capture.get("sequence_id") or sequence_id != p2_sequence:
        raise ValueError("C2/capture/P2 sequence IDs do not match")
    if capture.get("frame_count") != len(capture.get("frames", [])) or capture.get("frame_count") != len(p2_frames):
        raise ValueError("Capture/P2 frame counts differ")
    if capture.get("resolution_px") != [640, 480]:
        raise ValueError(f"Expected native 640x480 capture; got {capture.get('resolution_px')}")

    frame_capture = {int(f["frame_index"]): f for f in capture["frames"]}
    detections = []
    for p2_frame in p2_frames:
        if p2_frame.get("scene_id") != scene_id or p2_frame.get("sequence_id") != sequence_id:
            raise ValueError(f"P2 frame has wrong scene/sequence: {p2_frame.get('frame_id')}")
        frame_index = int(p2_frame["frame_index"])
        cap_frame = frame_capture.get(frame_index)
        if not cap_frame or p2_frame.get("frame_id") != cap_frame.get("frame_id") or abs(float(p2_frame["timestamp_s"]) - float(cap_frame["timestamp_s"])) > 1e-7:
            raise ValueError(f"Capture/P2 frame identity mismatch at index {frame_index}")
        for det in p2_frame.get("detections", []):
            detections.append((p2_frame, det))
    detections.sort(key=lambda x: (int(x[0]["frame_index"]), x[1]["detection_id"]))
    ids = [d["detection_id"] for _, d in detections]
    if len(ids) != 75 or len(set(ids)) != 75:
        raise ValueError(f"Expected exactly 75 unique P2 observations; found {len(ids)}")

    route_path = Path(c2_doc.get("route_config", ""))
    if not route_path.is_absolute():
        route_path = (ROOT / route_path).resolve()
    profile_path = Path(c2_doc.get("capture_profile", {}).get("profile_path", ""))
    if not profile_path.is_absolute():
        profile_path = (ROOT / profile_path).resolve()
    if not route_path.is_file() or not profile_path.is_file():
        raise FileNotFoundError("Locked route config or 640x480 capture profile is missing")
    source_files = {
        "c2_run_manifest": c2_manifest_path,
        "capture_manifest": capture_manifest_path,
        "p2_index": p2_path,
        "route_config": route_path,
        "capture_profile": profile_path,
    }
    source_artifacts = {k: {"path": str(v.relative_to(ROOT)), "sha256": sha256(v), "bytes": v.stat().st_size}
                        for k, v in source_files.items()}

    temp = out.with_name(out.name + ".building")
    if temp.exists():
        raise FileExistsError(f"Build directory already exists: {temp}")
    temp.mkdir(parents=True)
    (temp / "assets").mkdir()
    review_rows, panel_records, panel_targets = [], [], []
    same_frame = {}
    for ordinal, (p2_frame, det) in enumerate(detections, start=1):
        obs_id = det["detection_id"]
        frame_idx = int(p2_frame["frame_index"])
        cap_frame = frame_capture[frame_idx]
        frame_dir = cap_frame["frame_dir"]
        frame_root = c2_root / "capture" / frame_dir
        meta_path, rgb_path = frame_root / "metadata.json", frame_root / "rgb.npy"
        mask_path = c2_root / "detection_2d" / det["mask_file"]
        for p in (meta_path, rgb_path, mask_path):
            if not p.is_file():
                raise FileNotFoundError(f"Source file missing for {obs_id}: {p}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("frame_id") != p2_frame["frame_id"] or int(meta.get("frame_index", -1)) != frame_idx or abs(float(meta.get("timestamp_s", -1)) - float(p2_frame["timestamp_s"])) > 1e-7:
            raise ValueError(f"Frame metadata mismatch for {obs_id}")
        rgb_raw = np.load(rgb_path, allow_pickle=False)
        rgb = np.asarray(rgb_raw[..., :3], dtype=np.uint8)
        rgb_hash, mask_hash = sha256(rgb_path), sha256(mask_path)
        if cap_frame.get("raw_files_sha256", {}).get("rgb.npy") != rgb_hash or p2_frame.get("rgb_input_sha256") != rgb_hash:
            raise ValueError(f"RGB hash does not match capture and P2 records for {obs_id}")
        if mask_hash != det.get("mask_sha256"):
            raise ValueError(f"P2 mask hash mismatch for {obs_id}")
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        shape_hw = [int(x) for x in det.get("image_shape_hw", [])]
        if rgb.shape[:2] != mask.shape or list(rgb.shape[:2]) != shape_hw or shape_hw != [480, 640]:
            raise ValueError(f"RGB/mask dimensions mismatch for {obs_id}")
        pose = _pose_w_copt(capture, meta)
        if pose.shape != (4, 4) or not np.isfinite(pose).all() or not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-7):
            raise ValueError(f"Invalid camera pose metadata for {obs_id}")
        overlay = expected_overlay(rgb, mask)
        crop, crop_xyxy = expected_crop(overlay, mask)
        images = {"rgb": rgb, "overlay": overlay, "crop": crop,
                  "mask": np.repeat((mask.astype(np.uint8) * 255)[..., None], 3, axis=2)}
        panels = {}
        for kind in PANEL_KINDS:
            rel = Path("assets") / _asset_name(ordinal, obs_id, kind)
            target = temp / rel
            if target in panel_targets or target.exists():
                raise ValueError(f"Non-unique panel output path: {target}")
            panel_targets.append(target)
            Image.fromarray(images[kind]).save(target, format="PNG", optimize=True)
            panels[kind] = {"path": rel.as_posix(), "sha256": sha256(target), "bytes": target.stat().st_size,
                            "width": int(images[kind].shape[1]), "height": int(images[kind].shape[0])}
            panel_records.append({"observation_id": obs_id, "kind": kind, **panels[kind]})
            # Lossless pixel round-trip verifies each generated panel is tied to this source observation.
            if not np.array_equal(np.asarray(Image.open(target).convert("RGB")), images[kind]):
                raise ValueError(f"Panel pixel mismatch for {obs_id} ({kind})")
        pose_list = pose.tolist()
        review_rows.append({
            "observation_id": obs_id, "frame_id": p2_frame["frame_id"], "frame_index": frame_idx,
            "timestamp_s": float(p2_frame["timestamp_s"]), "camera_pose_W_Copt": pose_list,
            "camera_position_W_m": pose[:3, 3].tolist(), "coordinate_frame": "canonical world, Z-up",
            "camera_convention": "+X right, +Y down, +Z forward", "units": "m", "mask_crop_xyxy": crop_xyxy,
            "source_hashes": {"rgb_npy_sha256": rgb_hash, "p2_mask_sha256": mask_hash,
                              "capture_metadata_sha256": sha256(meta_path)}, "panels": panels,
            "dom_id": f"obs-{ordinal:03d}",
        })
        same_frame.setdefault(frame_idx, []).append(obs_id)
    same_frame_pairs = [{"frame_index": frame_idx, "observation_ids": [a, b]}
                        for frame_idx, frame_ids in sorted(same_frame.items())
                        for i, a in enumerate(frame_ids) for b in frame_ids[i + 1:]]
    selected_frames = sorted(same_frame)
    population = {
        "schema": REVIEW_SCHEMA, "status": "ready_for_human_review",
        "review_type": "development_not_holdout", "run_id": c2_run_id,
        "scene_id": scene_id, "sequence_id": sequence_id,
        "capture": {"resolution_px": [640, 480], "hfov_deg": capture.get("horizontal_fov_deg"),
                    "frame_count": capture["frame_count"], "depth_units": capture.get("depth_units"),
                    "capture_profile": c2_doc.get("capture_profile"),
                    "capture_sensor_contract_sha256": capture.get("capture_sensor_contract_sha256")},
        "population": {"definition": "All unique detection IDs in this run's authoritative P2 index; no hypothesis-based sampling.",
                       "observation_count": 75, "frame_count_with_detections": len(selected_frames),
                       "frame_count_total": capture["frame_count"], "same_frame_pair_count": len(same_frame_pairs),
                       "same_frame_pairs": same_frame_pairs,
                       "same_frame_relation_source": "computed only after review from shared confirmed human identity tokens; pair list is frame co-occurrence only, not a predicted duplicate label",
                       "metrics_scope": "Only the 75 observations and identities confirmed within this reviewed P2 population; not detector recall or room coverage."},
        "blinding": {"hidden": ["predicted class", "hypothesis IDs/grouping", "retained/filtered status", "room scores", "semantic consensus"],
                     "visible": ["full observation ID", "frame/timestamp/pose provenance", "current-run RGB", "current-run P2 mask overlay/crop"]},
        "annotation_contract": {"identity_token": "same token means same physical identity; independent of class and mask purity",
                                "class": "human visual judgment; do not infer from identity token",
                                "mask_purity": "independent visual judgment", "uncertain_and_unreviewed_allowed": True,
                                "annotation_count": "one row per P2 observation"},
        "model_provenance_non_predictive": {"profile_id": p2.get("model_fingerprint", {}).get("profile_id"),
                                            "weights_sha256": p2.get("weights_sha256"),
                                            "preprocess_fingerprints": sorted({d.get("preprocess_fingerprint") for _, d in detections})},
        "source_artifacts": source_artifacts,
        "capture_frame_hashes": [{"frame_id": frame_capture[i]["frame_id"], "frame_index": i,
                                   "rgb_npy_sha256": frame_capture[i]["raw_files_sha256"]["rgb.npy"]}
                                  for i in range(len(frame_capture))],
        "observations": review_rows,
        "panel_files": panel_records,
        "no_prior_annotations_reused": True,
        "prior_160x120_annotation_join": "prohibited; this package contains no imported prior annotations",
    }
    write_json(temp / "population_manifest.json", population)
    population_hash = sha256(temp / "population_manifest.json")
    template = {"schema": RESPONSE_SCHEMA, "review_type": "development_not_holdout", "run_id": c2_run_id,
                "population_manifest_sha256": population_hash,
                "review_source": "human visual review of current C2 RGB/P2 masks", "observations": [
                    {"observation_id": r["observation_id"], "identity_status": "unreviewed", "identity_token": "",
                     "class_status": "unreviewed", "class_label": "", "mask_purity": "unreviewed", "notes": ""}
                    for r in review_rows]}
    write_json(temp / "annotation_template.json", template)
    html_path = temp / "review_gallery.html"
    html_path.write_text(build_html(review_rows, run_id=c2_run_id, population_sha256=population_hash), encoding="utf-8")
    html_text = html_path.read_text(encoding="utf-8").lower()
    forbidden = ["class_raw", "class_canonical", "score_raw", "hypothesis_", "retained", "filtered", "room_scores", "consensus_winner"]
    if any(token in html_text for token in forbidden):
        raise ValueError("Blind gallery contains a forbidden prediction/grouping field")
    all_panel_paths = [x["path"] for x in panel_records]
    if len(all_panel_paths) != 75 * len(PANEL_KINDS) or len(set(all_panel_paths)) != len(all_panel_paths):
        raise ValueError("Panel paths are missing or non-unique")
    validation = {"schema": "hm3d-p2-640-review-validation/v1", "status": "pass",
                  "checks": {"exactly_75_unique_authoritative_p2_ids": len(ids) == 75 and len(set(ids)) == 75,
                             "capture_p2_scene_sequence_frame_alignment": True,
                             "native_640x480_verified_from_capture_metadata_and_masks": True,
                             "rgb_hashes_match_capture_and_p2": True, "mask_hashes_match_p2_index": True,
                             "panel_pixels_match_source_rgb_and_mask": True,
                             "all_300_panel_paths_unique": len(set(all_panel_paths)) == 300,
                             "prediction_and_hypothesis_metadata_absent_from_gallery": True,
                             "prior_resolution_annotations_not_loaded": True},
                  "validated_observations": 75, "validated_panel_files": len(panel_records),
                  "source_artifact_hashes": source_artifacts,
                  "population_manifest_sha256": population_hash, "gallery_sha256": sha256(html_path),
                  "template_sha256": sha256(temp / "annotation_template.json")}
    write_json(temp / "package_validation.json", validation)
    readme = f"""# HM3D 00802 · P2 human review · 640×480

Status: **ready for development review; not a holdout**.

Open `review_gallery.html` in a browser. The page contains 75 observations over {capture['frame_count']} captured frames, ten observations per page. It shows full RGB, the exact P2 mask overlay, crop, and binary mask. Use the comparison queue to put up to four observations side by side, including observations from different pages.

The gallery autosaves in the current browser. Use **Save / export draft JSON** for a portable checkpoint. To continue later, open the same gallery and use **Continue from JSON** with that checkpoint. Import is accepted only when run ID, population-manifest hash, and all 75 unique IDs match.

Review identity token, class, and mask purity independently. Use one token for observations of the same physical object. Uncertain or unreviewed fields are permitted. Same-frame pair consistency is derived later only from the human identity annotations, never from mask IoU or predicted tracks.

The locked C2 run is `{c2_run_id}` (`{scene_id}` / `{sequence_id}`), native 640×480. The exact source hashes and observation-to-panel mapping are in `population_manifest.json`; build validation is in `package_validation.json`. No old 160×120 annotations, D1 hypotheses, prediction labels, room scores, or assistant triage were used.

Package build command:
`python3 scripts/build_hm3d_p2_640_review.py --c2-run-id {c2_run_id} --output {out.relative_to(ROOT).as_posix()}`

`results` manifest/schema lookup fix is separate from this review package. GUI automation was not used; if browser policy prevents GUI inspection, visual GUI acceptance remains pending.
"""
    (temp / "README.vi.md").write_text(readme, encoding="utf-8")
    files = [{"path": p.relative_to(temp).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p)}
             for p in sorted(temp.rglob("*")) if p.is_file() and p.name != "package_manifest.json"]
    write_json(temp / "package_manifest.json", {"schema": "hm3d-p2-640-review-package/v1", "status": "ready_for_human_review",
                                                 "run_id": c2_run_id, "population_manifest_sha256": population_hash,
                                                 "file_count": len(files), "files": files})
    out.parent.mkdir(parents=True, exist_ok=True)
    temp.rename(out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--c2-run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = build_package(args.c2_run_id, args.output)
    print(json.dumps({"status": "ready_for_human_review", "run_id": args.c2_run_id,
                      "output": str(out), "observations": 75, "panel_files": 300,
                      "population_manifest_sha256": sha256(out / "population_manifest.json"),
                      "package_manifest_sha256": sha256(out / "package_manifest.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
