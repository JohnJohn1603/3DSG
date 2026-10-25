#!/usr/bin/env python3
"""Build a blinded annotation package for a locked HM3D P2 population.

This is an artifact-only operation. It reads existing C2/D1 outputs and does
not run perception, mapping, room scoring, or alter source artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PANEL_KINDS = ("rgb", "overlay", "crop", "mask")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_panel_id(index: int, observation_id: str) -> str:
    safe = observation_id.replace(":", "__").replace("/", "_").replace("\\", "_")
    if not safe or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in safe):
        raise ValueError(f"Unexpected characters in observation ID: {observation_id!r}")
    return f"{index:03d}_{safe}"


def expected_overlay(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    out = rgb.copy()
    if mask.any():
        out[mask] = (0.50 * out[mask] + 0.50 * np.asarray([255, 48, 48])).astype(np.uint8)
    return out


def expected_crop(overlay: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, list[int]]:
    if not mask.any():
        return overlay, [0, 0, 0, 0]
    ys, xs = np.where(mask)
    pad = max(8, int(0.12 * max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)))
    x0, x1 = max(0, int(xs.min()) - pad), min(overlay.shape[1], int(xs.max()) + pad + 1)
    y0, y1 = max(0, int(ys.min()) - pad), min(overlay.shape[0], int(ys.max()) + pad + 1)
    return overlay[y0:y1, x0:x1], [x0, y0, x1, y1]


def rgb_image(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def json_safe(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def build_html(review_rows: list[dict], population_manifest_hash: str, run_id: str) -> str:
    data = json.dumps(review_rows, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    return r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HM3D 00802 · blind P2 review</title>
<style>
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#172033;background:#f2f5f9}*{box-sizing:border-box}body{margin:0}header{background:#102b48;color:#fff;padding:20px 24px}header h1{margin:0 0 8px;font-size:24px}header p{margin:4px 0;color:#d9e5f2}.wrap{max-width:1500px;margin:auto;padding:18px}.toolbar,.card,.compare{background:#fff;border:1px solid #cbd5e1;border-radius:12px;padding:14px;margin-bottom:14px;box-shadow:0 2px 8px #0f172a0a}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}button{font:inherit;padding:8px 12px;border:1px solid #94a3b8;background:#fff;border-radius:8px;cursor:pointer}button.primary{background:#155e75;color:#fff;border-color:#155e75}button:disabled{opacity:.5;cursor:not-allowed}.status{margin-left:auto;color:#475569}.card h2{font-size:17px;margin:0 0 5px;overflow-wrap:anywhere}.provenance{color:#475569;font-size:13px;margin:0 0 12px}.panels{display:grid;grid-template-columns:2fr 2fr 1fr 1fr;gap:10px}.panel{min-width:0}.panel h3{font-size:13px;margin:0 0 5px;color:#334155}.panel img{display:block;width:100%;height:260px;object-fit:contain;background:#111827;border-radius:7px}.panel.crop img{height:260px}.annot{display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:10px;margin-top:12px}.annot label{display:block;font-size:13px;font-weight:650}.annot input,.annot select,.annot textarea{font:inherit;width:100%;margin-top:4px;padding:7px;border:1px solid #94a3b8;border-radius:6px;background:#fff}.annot textarea{min-height:40px;resize:vertical}.compare-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}.compare-item{border:1px solid #cbd5e1;border-radius:8px;padding:8px}.compare-item h3{font-size:13px;overflow-wrap:anywhere}.compare-item img{width:100%;height:210px;object-fit:contain;background:#111827}.checkline{display:flex;align-items:center;gap:7px;margin-top:8px;font-size:13px}.checkline input{width:auto}.foot{font-size:13px;color:#475569}.hidden{display:none!important}@media(max-width:900px){.panels{grid-template-columns:1fr 1fr}.annot{grid-template-columns:1fr 1fr}}@media(max-width:580px){.panels,.annot{grid-template-columns:1fr}.panel img{height:240px}.status{margin-left:0;width:100%}}
</style></head><body>
<header><h1>Blind review · HM3D 00802 / P2 observations</h1>
<p>Population: 25 detections from the locked C2/D1 run. Review each observation independently; the gallery intentionally omits predicted labels, model grouping, consensus, and room scores.</p>
<p>Enter physical identity token, class, and mask purity as separate judgments. Use “uncertain” when evidence is insufficient. Same token means the same physical object across observations.</p></header>
<main class="wrap"><section class="toolbar">
<button id="export" class="primary">Export annotation JSON</button><button id="compare">Compare selected (max 4)</button><button id="clearCompare">Clear comparison</button>
<span id="status" class="status">All 25 observations start as unreviewed.</span></section>
<section id="comparePanel" class="compare hidden"><h2>Side-by-side comparison</h2><div id="compareGrid" class="compare-grid"></div></section>
<div id="cards"></div>
<p class="foot">Run: __RUN_ID__ · population manifest SHA-256: __POP_HASH__. Images are copied from verified C2/D1 artifacts. This page is an annotation aid; it does not alter predictions or score room labels.</p></main>
<script>
const DATA = DATA_JSON;
const RUN_ID = __RUN_ID__;
const POP_HASH = __POP_HASH__;
const KIND_LABEL = {rgb:"Full RGB",overlay:"RGB + mask overlay",crop:"Mask crop",mask:"Binary mask"};
const state = new Map();
function esc(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function storageGet(key){try{return localStorage.getItem(key)}catch{return null}}
function storageSet(key,value){try{localStorage.setItem(key,value)}catch{}}
function initialRow(d){return {observation_id:d.observation_id,identity_status:"unreviewed",identity_token:"",class_status:"unreviewed",class_label:"",mask_purity:"unreviewed",notes:""};}
function getRow(id){if(!state.has(id)){const saved=storageGet("blind-review:"+RUN_ID+":"+POP_HASH+":"+id);let row=initialRow(DATA.find(x=>x.observation_id===id));if(saved){try{row={...row,...JSON.parse(saved)}}catch{}}state.set(id,row)}return state.get(id)}
function saveRow(id,patch){const row={...getRow(id),...patch};state.set(id,row);storageSet("blind-review:"+RUN_ID+":"+POP_HASH+":"+id,JSON.stringify(row));updateStatus()}
function updateStatus(){let complete=0,uncertain=0;for(const d of DATA){const r=getRow(d.observation_id);if(r.identity_status!=="unreviewed"&&r.class_status!=="unreviewed"&&r.mask_purity!=="unreviewed")complete++;if(r.identity_status==="uncertain"||r.class_status==="uncertain"||r.mask_purity==="uncertain")uncertain++}document.getElementById("status").textContent=`Reviewed all fields: ${complete}/${DATA.length}; at least one uncertain field: ${uncertain}; remaining observations may be unreviewed.`}
function fieldSelect(id,key,label,options){const row=getRow(id);return `<label>${label}<select data-id="${esc(id)}" data-key="${key}">${options.map(([v,t])=>`<option value="${v}" ${row[key]===v?"selected":""}>${t}</option>`).join("")}</select></label>`}
function render(){const root=document.getElementById("cards");root.innerHTML=DATA.map(d=>{const id=d.observation_id,row=getRow(id);return `<article class="card" id="obs-${esc(d.dom_id)}"><h2>${esc(id)}</h2><p class="provenance">Frame ${d.frame_index} · ${esc(d.frame_id)} · t=${Number(d.timestamp_s).toFixed(3)} s · camera position W (m): ${d.camera_pose_W_Copt.slice(0,3).map(r=>Number(r[3]).toFixed(3)).join(", ")}<details><summary>Pose convention and full camera transform</summary><p>Z-up world, meters; optical camera +X right, +Y down, +Z forward. Pose source: capture sensor metadata, cross-checked against D1 pose record.</p><pre>${esc(JSON.stringify(d.camera_pose_W_Copt,null,2))}</pre></details></p>
<div class="panels">${["rgb","overlay","crop","mask"].map(k=>`<div class="panel ${k}"><h3>${KIND_LABEL[k]}</h3><a href="${esc(d.panels[k].path)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(d.panels[k].path)}" alt="${KIND_LABEL[k]} for ${esc(id)}"></a></div>`).join("")}</div>
<label class="checkline"><input type="checkbox" class="compareCheck" data-id="${esc(id)}">Add to side-by-side comparison</label>
<div class="annot">${fieldSelect(id,"identity_status","Physical identity",[["unreviewed","Unreviewed"],["confirmed","Certain / assign token"],["uncertain","Uncertain"]])}<label>Identity token (same token = same physical object)<input data-id="${esc(id)}" data-key="identity_token" value="${esc(row.identity_token)}" placeholder="e.g. object_1; blank if uncertain"></label>${fieldSelect(id,"class_status","Class judgment",[["unreviewed","Unreviewed"],["confirmed","Certain / enter class"],["uncertain","Uncertain"]])}<label>Class, independently judged<input data-id="${esc(id)}" data-key="class_label" value="${esc(row.class_label)}" placeholder="Your class label; do not infer from token"></label>${fieldSelect(id,"mask_purity","Mask purity",[["unreviewed","Unreviewed"],["pure_single_object","Pure: one object"],["mixed_multiple_objects","Mixed: multiple objects"],["invalid_or_misaligned","Invalid / misaligned / mostly wrong pixels"],["uncertain","Uncertain"]])}<label>Notes<textarea data-id="${esc(id)}" data-key="notes" placeholder="Optional visual note">${esc(row.notes)}</textarea></label></div></article>`}).join("");
root.querySelectorAll("[data-id][data-key]").forEach(el=>el.addEventListener("input",()=>saveRow(el.dataset.id,{[el.dataset.key]:el.value})));root.querySelectorAll("[data-id][data-key]").forEach(el=>el.addEventListener("change",()=>saveRow(el.dataset.id,{[el.dataset.key]:el.value})));updateStatus()}
function selectedIds(){return [...document.querySelectorAll(".compareCheck:checked")].map(x=>x.dataset.id)}
document.getElementById("compare").onclick=()=>{const ids=selectedIds();if(!ids.length||ids.length>4){alert("Select between 1 and 4 observations to compare.");return}const panel=document.getElementById("comparePanel"),grid=document.getElementById("compareGrid");grid.innerHTML=ids.map(id=>{const d=DATA.find(x=>x.observation_id===id);return `<div class="compare-item"><h3>${esc(id)} · frame ${d.frame_index}</h3>${["rgb","overlay","crop"].map(k=>`<p>${KIND_LABEL[k]}<img src="${esc(d.panels[k].path)}" alt="${KIND_LABEL[k]}"></p>`).join("")}</div>`}).join("");panel.classList.remove("hidden");panel.scrollIntoView({behavior:"smooth",block:"start"})};
document.getElementById("clearCompare").onclick=()=>{document.querySelectorAll(".compareCheck").forEach(x=>x.checked=false);document.getElementById("comparePanel").classList.add("hidden")};
document.getElementById("export").onclick=()=>{const rows=DATA.map(d=>getRow(d.observation_id));const payload={schema:"hm3d-p2-blind-review-response/v1",run_id:RUN_ID,population_manifest_sha256:POP_HASH,review_source:"human visual review of current C2 RGB/masks",created_utc:new Date().toISOString(),observations:rows};const blob=new Blob([JSON.stringify(payload,null,2)+"\n"],{type:"application/json"});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="hm3d00802_p2_review_response_v1.json";a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
render();
</script></body></html>'''.replace("DATA_JSON", data).replace("__RUN_ID__", json.dumps(run_id)).replace("__POP_HASH__", json.dumps(population_manifest_hash))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--c2-run-id", required=True)
    ap.add_argument("--d1-run-id", required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    c2 = ROOT / "artifacts" / "conceptgraphs_hybrid_runs" / args.c2_run_id
    d1 = ROOT / "artifacts" / "live_processing_runs" / args.d1_run_id
    out = args.output if args.output.is_absolute() else ROOT / args.output
    out = out.resolve()
    if out.exists():
        raise SystemExit(f"Refusing to overwrite existing output: {out}")
    if out == ROOT or ROOT not in out.parents:
        raise SystemExit("Output must be a new path inside this workspace.")

    capture_manifest_path = c2 / "capture/capture_manifest.json"
    p2_path = c2 / "detection_2d/detection_2d_index.json"
    c2_manifest_path = c2 / "c2_run_manifest.json"
    effective_route_path = c2 / "effective_route_config.json"
    d1_manifest_path = d1 / "run_manifest.json"
    contract_path = d1 / "object_mapping/experiment_contract.json"
    d1_object_run_path = d1 / "object_mapping/run_manifest.json"
    evidence_path = d1 / "object_mapping/diagnostic/evidence_manifest.json"
    required = [capture_manifest_path, p2_path, c2_manifest_path, d1_manifest_path, contract_path, d1_object_run_path, evidence_path]
    for p in required:
        if not p.is_file():
            raise SystemExit(f"Required source artifact missing: {p}")

    capture = json.loads(capture_manifest_path.read_text(encoding="utf-8"))
    p2 = json.loads(p2_path.read_text(encoding="utf-8"))
    c2_manifest = json.loads(c2_manifest_path.read_text(encoding="utf-8"))
    d1_manifest = json.loads(d1_manifest_path.read_text(encoding="utf-8"))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    d1_object_run = json.loads(d1_object_run_path.read_text(encoding="utf-8"))
    evidence_doc = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence = evidence_doc["observations"]

    if c2_manifest.get("status") != "completed" or d1_manifest.get("status") != "completed":
        raise SystemExit("C2 and D1 source run manifests must both be completed.")
    if c2_manifest.get("run_id") != args.c2_run_id or d1_manifest.get("run_id") != args.d1_run_id:
        raise SystemExit("Run IDs do not match the selected source directories.")
    if p2.get("status") != "pass" or p2.get("cache_authoritative") is not True:
        raise SystemExit("P2 index is not an authoritative successful cache.")
    scene_id, sequence_id = c2_manifest.get("scene_id"), c2_manifest.get("sequence_id")
    if scene_id != d1_manifest.get("scene_id") or sequence_id != d1_manifest.get("sequence_id"):
        raise SystemExit("C2/D1 scene or sequence mismatch.")
    if capture.get("frame_count") != len(p2.get("frames", [])):
        raise SystemExit("Capture and P2 frame counts differ.")

    frames_by_index = {int(f["frame_index"]): f for f in capture["frames"]}
    p2_detections: list[tuple[dict, dict]] = []
    for frame in p2["frames"]:
        if frame.get("scene_id") != scene_id or frame.get("sequence_id") != sequence_id:
            raise SystemExit(f"P2 scene/sequence mismatch at frame {frame.get('frame_index')}.")
        for det in frame.get("detections", []):
            p2_detections.append((frame, det))
    p2_detections.sort(key=lambda x: (int(x[0]["frame_index"]), x[1]["detection_id"]))
    observation_ids = [d["detection_id"] for _, d in p2_detections]
    if len(observation_ids) != 25 or len(set(observation_ids)) != 25:
        raise SystemExit(f"Expected exactly 25 unique P2 observations; found {len(observation_ids)}.")
    if set(contract.get("input_observation_ids", [])) != set(observation_ids):
        raise SystemExit("D1 locked input observation IDs differ from the P2 population.")
    if set(evidence) != set(observation_ids):
        raise SystemExit("D1 evidence panels do not cover exactly the locked P2 population.")

    source_artifacts = {
        "c2_run_manifest": c2_manifest_path,
        "capture_manifest": capture_manifest_path,
        "p2_index": p2_path,
        "d1_run_manifest": d1_manifest_path,
        "d1_object_mapping_run_manifest": d1_object_run_path,
        "d1_experiment_contract": contract_path,
        "d1_evidence_manifest": evidence_path,
    }
    if effective_route_path.is_file():
        source_artifacts["effective_route_config"] = effective_route_path
    declared_route_config = c2_manifest.get("route_config")
    route_identity = sequence_id
    if isinstance(declared_route_config, str):
        declared_route_path = Path(declared_route_config)
        if declared_route_path.is_file():
            source_artifacts["declared_route_config"] = declared_route_path.resolve()
            route_identity = declared_route_path.stem
    source_hashes = {name: {"path": str(path.relative_to(ROOT)), "sha256": sha256(path), "bytes": path.stat().st_size}
                     for name, path in source_artifacts.items()}

    selected_frames = {int(fr["frame_index"]) for fr, _ in p2_detections}
    same_frame_pairs = []
    by_frame: dict[int, list[str]] = {}
    for fr, det in p2_detections:
        by_frame.setdefault(int(fr["frame_index"]), []).append(det["detection_id"])
    for frame_idx, ids in sorted(by_frame.items()):
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                same_frame_pairs.append({"frame_index": frame_idx, "observation_ids": [ids[i], ids[j]]})

    # Check that D1's camera pose is the source capture sensor pose transformed
    # into the declared Z-up world and optical-camera convention.
    A = np.asarray(capture["canonical_from_habitat"], dtype=np.float64)
    optical_from_habitat_sensor = np.diag([1.0, -1.0, -1.0, 1.0])
    review_rows: list[dict] = []
    panel_targets: list[Path] = []
    panel_records = []
    temp = out.with_name(out.name + ".building")
    if temp.exists():
        raise SystemExit(f"Refusing to use an existing build path: {temp}")
    out.parent.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True)
    (temp / "assets").mkdir()

    for ordinal, (p2_frame, det) in enumerate(p2_detections, start=1):
        obs_id = det["detection_id"]
        frame_idx = int(p2_frame["frame_index"])
        capture_frame = frames_by_index.get(frame_idx)
        if not capture_frame or capture_frame.get("frame_id") != p2_frame.get("frame_id"):
            raise SystemExit(f"Capture/P2 frame alignment mismatch for {obs_id}.")
        if abs(float(capture_frame["timestamp_s"]) - float(p2_frame["timestamp_s"])) > 1e-7:
            raise SystemExit(f"Capture/P2 timestamp mismatch for {obs_id}.")
        meta_path = c2 / "capture" / capture_frame["frame_dir"] / "metadata.json"
        rgb_path = c2 / "capture" / capture_frame["frame_dir"] / "rgb.npy"
        mask_path = c2 / "detection_2d" / det["mask_file"]
        if not meta_path.is_file() or not rgb_path.is_file() or not mask_path.is_file():
            raise SystemExit(f"Missing RGB, mask, or frame metadata for {obs_id}.")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("frame_id") != p2_frame["frame_id"] or int(meta.get("frame_index", -1)) != frame_idx:
            raise SystemExit(f"Frame metadata ID mismatch for {obs_id}.")
        rgb_full = np.load(rgb_path, allow_pickle=False)
        rgb = np.asarray(rgb_full[..., :3], dtype=np.uint8)
        mask_bytes_hash = sha256(mask_path)
        if mask_bytes_hash != det.get("mask_sha256"):
            raise SystemExit(f"P2 mask hash mismatch for {obs_id}.")
        mask = np.asarray(np.load(mask_path, allow_pickle=False), dtype=bool)
        if rgb.shape[:2] != mask.shape or list(rgb.shape[:2]) != det.get("image_shape_hw"):
            raise SystemExit(f"RGB/mask dimensions mismatch for {obs_id}.")

        ev = evidence[obs_id]
        if int(ev.get("frame_index", -1)) != frame_idx:
            raise SystemExit(f"D1 evidence frame index mismatch for {obs_id}.")
        if Path(ev.get("source_rgb", "")).resolve() != rgb_path.resolve() or ev.get("source_rgb_sha256") != sha256(rgb_path):
            raise SystemExit(f"D1 source RGB provenance mismatch for {obs_id}.")
        if Path(ev.get("source_mask", "")).resolve() != mask_path.resolve() or ev.get("source_mask_sha256") != mask_bytes_hash:
            raise SystemExit(f"D1 source mask provenance mismatch for {obs_id}.")

        pose = np.asarray(ev.get("camera_pose_W_Copt"), dtype=np.float64)
        sensor_position = np.asarray(meta.get("sensor_position_habitat"), dtype=np.float64)
        sensor_rotation = np.asarray(meta.get("sensor_rotation_habitat"), dtype=np.float64)
        expected_position = (A @ np.r_[sensor_position, 1.0])[:3]
        expected_rotation = A[:3, :3] @ sensor_rotation @ optical_from_habitat_sensor[:3, :3]
        if pose.shape != (4, 4) or not np.isfinite(pose).all() or not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-7):
            raise SystemExit(f"Invalid D1 camera transform for {obs_id}.")
        if not np.allclose(pose[:3, 3], expected_position, atol=1e-5) or not np.allclose(pose[:3, :3], expected_rotation, atol=1e-5):
            raise SystemExit(f"D1 camera transform does not match capture metadata for {obs_id}.")

        source_panels = {}
        expected_images = {
            "rgb": rgb,
            "mask": np.repeat((mask.astype(np.uint8) * 255)[..., None], 3, axis=2),
        }
        overlay = expected_overlay(rgb, mask)
        crop, bbox = expected_crop(overlay, mask)
        expected_images["overlay"] = overlay
        expected_images["crop"] = crop
        if ev.get("mask_crop_xyxy") != bbox:
            raise SystemExit(f"D1 crop box does not match P2 mask for {obs_id}.")
        for kind in PANEL_KINDS:
            rel = ev.get("panels", {}).get(kind)
            if not rel:
                raise SystemExit(f"D1 {kind} panel missing for {obs_id}.")
            src = (d1 / "object_mapping/diagnostic" / rel).resolve()
            if d1.resolve() not in src.parents or not src.is_file():
                raise SystemExit(f"D1 panel path invalid for {obs_id}: {src}")
            actual = rgb_image(src)
            if not np.array_equal(actual, expected_images[kind]):
                raise SystemExit(f"D1 {kind} panel pixels do not match source RGB/P2 mask for {obs_id}.")
            source_panels[kind] = src

        panel_key = safe_panel_id(ordinal, obs_id)
        copied_panels = {}
        for kind in PANEL_KINDS:
            target_rel = Path("assets") / f"{panel_key}_{kind}.png"
            target = temp / target_rel
            if target in panel_targets or target.exists():
                raise SystemExit(f"Duplicate output asset path: {target}")
            panel_targets.append(target)
            shutil.copyfile(source_panels[kind], target)
            copied_panels[kind] = {"path": target_rel.as_posix(), "sha256": sha256(target), "bytes": target.stat().st_size}
            panel_records.append({"observation_id": obs_id, "kind": kind, **copied_panels[kind]})

        review_rows.append({
            "observation_id": obs_id,
            "frame_id": p2_frame["frame_id"],
            "frame_index": frame_idx,
            "timestamp_s": float(p2_frame["timestamp_s"]),
            "camera_pose_W_Copt": pose.tolist(),
            "pose_source": {
                "capture_metadata_path": str(meta_path.relative_to(ROOT)),
                "capture_metadata_sha256": sha256(meta_path),
                "d1_pose_record_manifest_path": str(evidence_path.relative_to(ROOT)),
                "d1_pose_record_manifest_sha256": source_hashes["d1_evidence_manifest"]["sha256"],
                "coordinate_frame": "canonical world, Z-up",
                "camera_convention": "+X right, +Y down, +Z forward",
                "units": "m",
                "validated_against_capture_metadata": True,
            },
            "panels": copied_panels,
            "source_hashes": {
                "rgb_npy_sha256": sha256(rgb_path),
                "p2_mask_sha256": mask_bytes_hash,
                "capture_metadata_sha256": sha256(meta_path),
            },
            "dom_id": f"o{ordinal:03d}",
        })

    population = {
        "schema": "hm3d-p2-blind-review-population/v1",
        "status": "review_package_ready_pending_human_annotations",
        "run_ids": {"c2": args.c2_run_id, "d1": args.d1_run_id},
        "scene_id": scene_id,
        "sequence_id": sequence_id,
        "route_id": route_identity,
        "capture": {
            "frame_count": int(capture["frame_count"]),
            "width": int(p2_detections[0][1]["image_shape_hw"][1]),
            "height": int(p2_detections[0][1]["image_shape_hw"][0]),
            "resolution_hw": [int(x) for x in p2_detections[0][1]["image_shape_hw"]],
            "hfov_deg": float(json.loads((c2 / "capture/frames/000000/metadata.json").read_text(encoding="utf-8")).get("hfov_deg", 90.0)),
            "canonical_world": capture.get("canonical_world"),
            "camera_optical": capture.get("camera_optical"),
            "depth_units": capture.get("depth_units"),
        },
        "population": {
            "definition": "All unique detection IDs in the authoritative P2 index for this exact C2 run; no hypothesis-based sampling.",
            "observation_count": len(review_rows),
            "frame_count_with_detections": len(selected_frames),
            "pose_count_semantics": "unique camera pose matrices among the 25 observations, rounded to 1e-6; not independent views",
            "unique_pose_count": len({tuple(np.round(np.asarray(row["camera_pose_W_Copt"]).reshape(-1), 6)) for row in review_rows}),
            "same_frame_pair_count": len(same_frame_pairs),
            "same_frame_pairs": same_frame_pairs,
            "review_state_at_package_creation": {"reviewed": 0, "uncertain": 0, "unreviewed": len(review_rows)},
            "metrics_scope": "Only these 25 P2 observations and identities later confirmed within this reviewed population; not detector recall or room coverage.",
        },
        "blinding": {
            "excluded_from_gallery_and_package_data": ["predicted class", "detection score", "hypothesis IDs/grouping", "consensus winner", "room scores", "room label"],
            "visible": ["full observation ID", "frame/timestamp", "capture pose provenance", "RGB", "mask overlay", "crop", "binary mask"],
        },
        "baseline_config": {
            "seed": d1_object_run.get("seed"),
            "obj_min_detections": contract.get("effective_config", {}).get("obj_min_detections"),
            "reference_commit": contract.get("reference_commit"),
            "detector_weights_sha256": p2.get("weights_sha256"),
            "d1_runner_sha256": contract.get("runner_sha256"),
        },
        "source_artifacts": source_hashes,
        "observations": review_rows,
        "panel_files": panel_records,
        "old_annotation_provenance": {
            "source_package": "artifacts/human_review/hm3d00802_main29_min3_v1_review_v1/",
            "old_blind_package_panel_observations": 33,
            "old_blind_package_unique_observation_ids": 33,
            "old_response_observation_level_reviews": 12,
            "old_response_observation_level_review_subset": "All 12 are under old H01; H02-H06 have group-level summaries rather than per-observation review rows.",
            "old_reviewed_IDs_overlapping_current_population": 5,
            "old_displayed_IDs_overlapping_current_population": 14,
            "reuse_policy": "No old annotations reused. Matching IDs alone do not establish matching source capture/panel provenance; old review is preserved unchanged.",
        },
        "scope_note": "No room scoring, membership change, assumed scope, model run, capture, detector, mapping, native replay, or tuning performed.",
    }
    write_json(temp / "population_manifest.json", population)
    pop_hash = sha256(temp / "population_manifest.json")
    write_json(temp / "annotation_template.json", {
        "schema": "hm3d-p2-blind-review-response/v1",
        "run_id": args.d1_run_id,
        "population_manifest_sha256": pop_hash,
        "review_source": "human visual review of current C2 RGB/masks",
        "observations": [
            {"observation_id": row["observation_id"], "identity_status": "unreviewed", "identity_token": "",
             "class_status": "unreviewed", "class_label": "", "mask_purity": "unreviewed", "notes": ""}
            for row in review_rows
        ],
    })
    html_text = build_html(review_rows, pop_hash, args.d1_run_id)
    (temp / "review_gallery.html").write_text(html_text, encoding="utf-8")
    write_json(temp / "package_validation.json", {
        "schema": "hm3d-p2-blind-review-validation/v1",
        "status": "pass",
        "checks": {
            "exact_population_25_unique_ids": True,
            "c2_d1_scene_sequence_match": True,
            "d1_input_population_matches_p2": True,
            "rgb_panels_pixel_match_capture": True,
            "mask_panels_pixel_match_p2_mask": True,
            "overlay_and_crop_pixel_match_rgb_plus_p2_mask": True,
            "capture_pose_matches_d1_pose_record": True,
            "all_panels_reference_correct_observation": True,
            "panel_target_paths_unique": len(panel_targets) == len(set(panel_targets)) == len(review_rows) * len(PANEL_KINDS),
            "no_predicted_labels_or_hypothesis_ids_in_review_payload": True,
        },
        "validated_observations": len(review_rows),
        "validated_panel_files": len(panel_records),
        "gallery_html_sha256": sha256(temp / "review_gallery.html"),
        "annotation_template_sha256": sha256(temp / "annotation_template.json"),
        "population_manifest_sha256": sha256(temp / "population_manifest.json"),
    })

    # Blindness validation inspects only review-facing payloads, not source manifests.
    html_payload = (temp / "review_gallery.html").read_text(encoding="utf-8").lower()
    manifest_payload = (temp / "population_manifest.json").read_text(encoding="utf-8").lower()
    if "hypothesis_" in html_payload or "class_raw" in html_payload or "class_canonical" in html_payload or "score_raw" in html_payload:
        raise SystemExit("Blinding validation failed: prediction/group fields appeared in gallery HTML.")
    if "hypothesis_" in manifest_payload or '"raw_class"' in manifest_payload or '"predicted_class"' in manifest_payload:
        raise SystemExit("Blinding validation failed: prediction/group fields appeared in population manifest.")
    if len(panel_targets) != len(set(panel_targets)) or len(review_rows) != 25:
        raise SystemExit("Output path uniqueness or population validation failed.")

    readme = f"""# Blind review package — HM3D 00802 P2 observations

Status: **ready for human review**. This is step 1 only; no annotations have been scored and no predictor output was changed.

## Open the gallery

Open `review_gallery.html` directly in a local browser. The page needs no network, CDN, or server. It shows the full current RGB, exact P2 mask overlay, crop, and binary mask for each of the {len(review_rows)} observations. Multiple observations can be selected for side-by-side comparison (up to four).

Use **Export annotation JSON** after review. Identity token, class, and mask purity are independent fields. Give the same identity token only to observations you judge to be the same physical object. If uncertain, explicitly choose `Uncertain`; leave a field `Unreviewed` if you did not assess it. Do not infer physical-object count from any model grouping.

## Population and provenance

- C2: `{args.c2_run_id}`; D1: `{args.d1_run_id}`.
- Scene/sequence: `{scene_id}` / `{sequence_id}`.
- Rebuild (writes to a new path only): `python3 scripts/build_hm3d_p2_blind_review.py --c2-run-id {args.c2_run_id} --d1-run-id {args.d1_run_id} --output artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1_rebuilt`.
- Exactly 25 unique P2 observation IDs from the authoritative index; detections occur in {len(selected_frames)} frames. Same-frame observation pairs are listed separately in `population_manifest.json`.
- RGB pixels, mask pixels, overlay, crop, dimensions, and camera transforms were checked against source files. See `package_validation.json` and SHA-256s in `population_manifest.json`.
- The population is only these 25 P2 observations. Any later coverage/retention metric is limited to the observations and identities confirmed inside this review; it is not detector recall or room coverage.

## Why old annotation counts differ

The preserved old blind package contained 33 unique observation panels across H01–H06. Its response contains 12 individual observation review rows, all under H01; H02–H06 were recorded as group-level summaries. Five of those 12 individual IDs and 14 of the 33 displayed IDs overlap the current P2 population. None is reused: identical IDs alone do not prove matching capture/image provenance, and the old review package remains unchanged.

## Later scoring categories

After annotation is returned, singleton model hypotheses will be distinguished as: a real object with one detection; a real object fragmented across multiple hypotheses; a false merge; mixed/invalid mask; or uncertain. These are conclusions from the reviewed identity/class/mask fields joined to the locked assignments; they are not inferred from hypothesis counts. Same-frame pairs will be reported separately.

No room scoring, assumed scope, model inference, capture, mapping, native replay, or threshold change occurred in this package step.
"""
    (temp / "README.vi.md").write_text(readme, encoding="utf-8")

    # Add final package file hashes after all review assets and explanatory files exist.
    files = []
    for path in sorted(p for p in temp.rglob("*") if p.is_file() and p.name != "package_manifest.json"):
        files.append({"path": path.relative_to(temp).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
    write_json(temp / "package_manifest.json", {
        "schema": "hm3d-p2-blind-review-package/v1",
        "status": "ready_for_human_review",
        "run_id": args.d1_run_id,
        "builder_source": {"path": str(Path(__file__).resolve().relative_to(ROOT)), "sha256": sha256(Path(__file__).resolve())},
        "population_manifest_sha256": sha256(temp / "population_manifest.json"),
        "file_count": len(files),
        "files": files,
    })
    out.parent.mkdir(parents=True, exist_ok=True)
    temp.rename(out)
    print(json.dumps({"status": "ready_for_human_review", "output": str(out), "observations": len(review_rows),
                      "panel_files": len(panel_records), "population_manifest_sha256": sha256(out / "population_manifest.json"),
                      "package_manifest_sha256": sha256(out / "package_manifest.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
