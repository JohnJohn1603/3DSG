#!/usr/bin/env python3
"""Prepare or finalize a viewer-only XYZ entrypoint for a D1 run."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import re
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_DISPLAY_POINTS = 300


def redirect_index_html() -> bytes:
    """Small compatibility page: resolve the viewer through the published pointer."""
    return '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scene graph viewer</title><p id="status">Resolving the published scene-graph viewer…</p>
<script>
(async()=>{const s=document.getElementById('status');try{
 const r=await fetch('./d2_viewer_entrypoint.json',{cache:'no-store'});if(!r.ok)throw Error('entrypoint pointer unavailable (HTTP '+r.status+')');
 const p=await r.json();if(p.schema!=='d2-viewer-entrypoint/v1'||!['ready','completed','failed','cancelled'].includes(p.status)||!/^[A-Za-z0-9_-]+\\.html$/.test(p.entrypoint))throw Error('invalid D2 viewer entrypoint pointer');
 location.replace('./'+p.entrypoint+location.search);
}catch(e){s.textContent='Viewer entrypoint unavailable: '+e.message+' — use ./run_scene.sh results <run-id> to inspect the published manifest.';}})();
</script>'''.encode("utf-8")


class _ScriptSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            source = dict(attrs).get("src")
            if source:
                self.sources.append(source)


def validate_entrypoint_bundle(viewer: Path) -> dict[str, Any]:
    """Validate pointer, D2 manifest, HTML, and every local JS asset before serving."""
    viewer = Path(viewer).resolve()
    pointer_path = viewer / "d2_viewer_entrypoint.json"
    pointer = read(pointer_path)
    if pointer.get("schema") != "d2-viewer-entrypoint/v1" or pointer.get("status") not in {"ready", "completed", "failed", "cancelled"}:
        raise ValueError(f"invalid D2 viewer entrypoint pointer: {pointer_path}")
    manifest_name, entrypoint = pointer.get("manifest_path"), pointer.get("entrypoint")
    if (not isinstance(manifest_name, str) or Path(manifest_name).name != manifest_name
            or not isinstance(entrypoint, str) or Path(entrypoint).name != entrypoint):
        raise ValueError("D2 viewer pointer paths must be filenames within the viewer directory")
    manifest_path, html_path = viewer / manifest_name, viewer / entrypoint
    if not manifest_path.is_file() or not html_path.is_file():
        raise FileNotFoundError("D2 viewer pointer references a missing manifest or entrypoint")
    if sha(manifest_path) != pointer.get("manifest_sha256"):
        raise ValueError("D2 viewer manifest hash differs from its entrypoint pointer")
    manifest = read(manifest_path)
    expected_pointer_status = "ready" if manifest.get("status") == "running" else manifest.get("status")
    if (manifest.get("status") not in {"running", "completed", "failed", "cancelled"}
            or pointer.get("pipeline_status") != manifest.get("status")
            or pointer.get("status") != expected_pointer_status
            or manifest.get("viewer_entrypoint") != entrypoint
            or manifest.get("viewer_version") != pointer.get("viewer_version")):
        raise ValueError("D2 viewer manifest and entrypoint pointer disagree")
    hashes = manifest.get("output_files_sha256", {})
    if not hashes or hashes.get(entrypoint) != pointer.get("entrypoint_sha256"):
        raise ValueError("D2 entrypoint hash is missing from its manifest")
    parser = _ScriptSourceParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    if not parser.sources:
        raise ValueError("D2 HTML has no local renderer script")
    for relative in parser.sources:
        normalized = relative.removeprefix("./") if isinstance(relative, str) else ""
        if (not normalized or relative.startswith(("http:", "https:", "//"))
                or Path(normalized).name != normalized):
            raise ValueError(f"D2 HTML references a non-local or unsafe script: {relative!r}")
        asset = viewer / normalized
        if not asset.is_file():
            raise FileNotFoundError(f"D2 JavaScript asset is missing: {asset}")
        expected = hashes.get(normalized)
        if not expected or sha(asset) != expected:
            raise ValueError(f"D2 JavaScript asset hash mismatch: {normalized}")
    for name, expected in hashes.items():
        if Path(name).name != name or not (viewer / name).is_file():
            raise FileNotFoundError(f"D2 manifest output asset is missing or unsafe: {name}")
        if sha(viewer / name) != expected:
            raise ValueError(f"D2 manifest output asset hash mismatch: {name}")
    return {"pointer": pointer, "manifest": manifest, "entrypoint_path": html_path,
            "script_assets": [source.removeprefix("./") for source in parser.sources]}


def validate_served_entrypoint(viewer: Path, url: str) -> dict[str, Any]:
    """HTTP preflight of the selected page, its JS assets, and empty live head."""
    bundle = validate_entrypoint_bundle(viewer)
    with urlopen(url, timeout=2.0) as response:
        if response.status != 200:
            raise RuntimeError(f"viewer entrypoint returned HTTP {response.status}")
        page = response.read()
    if page != bundle["entrypoint_path"].read_bytes():
        raise RuntimeError("HTTP viewer entrypoint bytes differ from the pointer-verified local asset")
    base = url.rsplit("/", 1)[0] + "/"
    for source in bundle["script_assets"]:
        with urlopen(urljoin(base, source), timeout=2.0) as response:
            payload = response.read()
        if sha_bytes(payload) != bundle["manifest"]["output_files_sha256"][source]:
            raise RuntimeError(f"HTTP JavaScript asset hash mismatch: {source}")
    for name in ("live_index.json", "live_head.json"):
        with urlopen(urljoin(base, name), timeout=2.0) as response:
            value = json.loads(response.read().decode("utf-8"))
        if name == "live_index.json" and value.get("frames"):
            raise ValueError("viewer bootstrap must be checked before first revision")
        if name == "live_head.json" and int(value.get("revision_count", -1)) != 0:
            raise ValueError("viewer bootstrap head must be checked before first revision")
    return bundle


def sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_entrypoint_pointer(viewer: Path, manifest_name: str, manifest: dict[str, Any]) -> Path:
    """Atomically publish the D2 manifest that user-facing launcher commands should open."""
    manifest_path = viewer / manifest_name
    entrypoint = str(manifest.get("viewer_entrypoint", ""))
    if not entrypoint or Path(entrypoint).name != entrypoint:
        raise ValueError("D2 manifest has an invalid viewer entrypoint")
    entrypoint_path = viewer / entrypoint
    if not manifest_path.is_file() or not entrypoint_path.is_file():
        raise FileNotFoundError("cannot publish D2 entrypoint pointer before manifest and viewer exist")
    state = manifest.get("status")
    if state not in {"running", "completed", "failed", "cancelled"}:
        raise ValueError(f"cannot publish D2 entrypoint for state {state!r}")
    pointer = {
        "schema": "d2-viewer-entrypoint/v1",
        "status": "ready" if state == "running" else state,
        "pipeline_status": state,
        "manifest_path": manifest_name,
        "manifest_sha256": sha(manifest_path),
        "entrypoint": entrypoint,
        "entrypoint_sha256": sha(entrypoint_path),
        "viewer_version": manifest.get("viewer_version"),
    }
    target = viewer / "d2_viewer_entrypoint.json"
    fd, temp_name = tempfile.mkstemp(prefix=".d2_viewer_entrypoint.", suffix=".tmp", dir=viewer)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(pointer, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return target


def terminalize_live_viewer(run_root: Path, *, status: str, error: str | None = None) -> dict[str, Any]:
    """Publish the authoritative run terminal state to live index/head and D2 pointer.

    The coordinator calls this only after it has written the terminal run
    manifest. This helper updates mutable operational documents and leaves
    immutable revision/evidence assets untouched.
    """
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError(f"invalid live viewer terminal status: {status!r}")
    run_root = Path(run_root).resolve()
    viewer = run_root / "viewer"
    run = read(run_root / "run_manifest.json")
    pointer_path = viewer / "d2_viewer_entrypoint.json"
    pointer = read(pointer_path)
    manifest_name = pointer.get("manifest_path")
    if (pointer.get("schema") != "d2-viewer-entrypoint/v1" or not isinstance(manifest_name, str)
            or Path(manifest_name).name != manifest_name
            or not isinstance(pointer.get("entrypoint"), str)
            or Path(pointer["entrypoint"]).name != pointer["entrypoint"]):
        raise ValueError("cannot terminalize an invalid or unsafe D2 pointer")
    manifest_path = viewer / manifest_name
    manifest = read(manifest_path)
    if sha(manifest_path) != pointer.get("manifest_sha256"):
        raise ValueError("D2 viewer manifest hash differs from its pointer before terminalization")
    run_status = str(run.get("status"))
    status_matches = ((status == "completed" and run_status == "completed")
                      or (status == "failed" and (run_status.startswith("failed") or run_status == "startup_preflight_failed"))
                      or (status == "cancelled" and run_status.startswith("cancelled")))
    if not status_matches:
        raise ValueError("viewer terminal state must match the authoritative run manifest")
    if manifest.get("run_id") != run.get("run_id"):
        raise ValueError("D2 viewer manifest belongs to a different run")
    index_path, head_path = viewer / "live_index.json", viewer / "live_head.json"
    index = read(index_path)
    head = read(head_path)
    if index.get("run_id") != run.get("run_id"):
        raise ValueError("live index belongs to a different run")
    head_was_synchronized = sha(index_path) == head.get("live_index_sha256")
    index["status"] = status
    atomic_write_bytes(index_path, (json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    head.update({"status": status, "revision_count": len(index.get("frames", [])),
                 "live_index_sha256": sha(index_path), "updated_utc": datetime.now(timezone.utc).isoformat(),
                 "final_postprocess_available": index.get("final_postprocess") is not None})
    atomic_write_bytes(head_path, (json.dumps(head, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    manifest["status"] = status
    manifest["source_run_status"] = run_status
    manifest["terminal_error"] = error
    manifest["terminal_updated_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_write_bytes(manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))
    write_entrypoint_pointer(viewer, manifest_path.name, manifest)
    return {"status": status, "live_index_sha256": sha(index_path),
            "live_head_sha256": sha(head_path), "manifest_sha256": sha(manifest_path),
            "pointer_sha256": sha(pointer_path), "revision_count": len(index.get("frames", [])),
            "preterminal_head_was_synchronized": head_was_synchronized}


def bootstrap(run_root: Path, version: str = "live") -> dict[str, Any]:
    """Publish the existing XYZ page before D1 producers start writing revisions."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", version):
        raise ValueError("version may contain only letters, digits, underscore and hyphen")
    run_root = Path(run_root).resolve()
    viewer = run_root / "viewer"
    run_path = run_root / "run_manifest.json"
    live_index_path = viewer / "live_index.json"
    live_head_path = viewer / "live_head.json"
    for path in (run_path, live_index_path, live_head_path):
        if not path.is_file():
            raise FileNotFoundError(f"D2 live bootstrap input missing: {path}")
    run = read(run_path)
    index = read(live_index_path)
    head = read(live_head_path)
    if run.get("status") not in {"started", "running"}:
        raise ValueError("D2 bootstrap requires a D1 run that has not completed")
    if index.get("status") not in {"starting", "running"} or head.get("status") not in {"starting", "running"}:
        raise ValueError("D2 bootstrap requires an empty/running live index and head")
    if index.get("frames") or int(head.get("revision_count", -1)) != 0:
        raise ValueError("D2 bootstrap must happen before the first producer revision")
    template = ROOT / "scripts/causal_timeline_viewer.html"
    renderer = ROOT / "scripts/xyz_renderer.js"
    if not template.is_file() or not renderer.is_file():
        raise FileNotFoundError("D2 live bootstrap source HTML or renderer is missing")
    html_name = f"index_d2_{version}.html"
    renderer_name = f"xyz_renderer_d2_{version}.js"
    manifest_name = f"d2_xyz_manifest_{version}.json"
    html_bytes = template.read_text(encoding="utf-8").replace("./xyz_renderer.js", f"./{renderer_name}").encode("utf-8")
    legacy_index = viewer / "index.html"
    expected_legacy = template.read_bytes()
    if legacy_index.exists() and legacy_index.read_bytes() not in {expected_legacy, redirect_index_html()}:
        raise FileExistsError(f"refusing to replace an unrecognized viewer index: {legacy_index}")
    assets = {"index.html": redirect_index_html(), html_name: html_bytes,
              renderer_name: renderer.read_bytes()}
    for name, payload in assets.items():
        target = viewer / name
        if (target.exists() and target.read_bytes() != payload
                and not (name == "index.html" and target.read_bytes() == expected_legacy)):
            raise FileExistsError(f"refusing to overwrite different D2 bootstrap asset: {target}")
        if not target.exists() or target.read_bytes() != payload:
            atomic_write_bytes(target, payload)
    output_hashes = {name: sha(viewer / name) for name in assets}
    manifest = {
        "schema": "d2-xyz-viewer/v1", "status": "running", "run_id": run.get("run_id"),
        "source_execution_mode": run.get("execution_mode"), "viewer_only": True,
        "viewer_version": version, "viewer_entrypoint": html_name,
        "bootstrap": True, "frame_count": 0,
        "renderer": {"api": "browser WebGL", "external_dependencies": [], "cdn": False,
                     "xyz": "object point samples + camera position trajectory", "camera_frustum": "not rendered"},
        "spatial_panels": {"object_clouds_and_camera_path": "XYZ",
                           "room_polygons_and_adjacency": "XY only; no room volumes inferred"},
        "causality": {"objects": "prefix_state_only",
                      "rooms": "prefix_state_only_native_replay_reference",
                      "membership": "unavailable_elevation_scope",
                      "room_semantics": "unavailable_membership_scope"},
        "membership_status": "unavailable_elevation_scope",
        "room_semantics_status": "unavailable_membership_scope",
        "prefix_admission": "unavailable_until_source_contract_exposes_it",
        "final_postprocess": "separate_terminal_stage; not backfilled",
        "output_files_sha256": output_hashes,
        "input_files_sha256": {"scripts/causal_timeline_viewer.html": sha(template),
                               "scripts/xyz_renderer.js": sha(renderer),
                               "scripts/build_d2_xyz_viewer.py": sha(Path(__file__).resolve())},
    }
    manifest_path = viewer / manifest_name
    if manifest_path.exists():
        current = read(manifest_path)
        if current.get("run_id") != run.get("run_id") or current.get("status") != "running":
            raise FileExistsError(f"refusing to overwrite non-bootstrap D2 manifest: {manifest_path}")
    temp_path = manifest_path.with_name(f".{manifest_name}.tmp")
    temp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp_path, manifest_path)
    write_entrypoint_pointer(viewer, manifest_name, manifest)
    return manifest


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _inside(root: Path, base: Path, relative: str) -> Path:
    value = (base / relative).resolve()
    if not value.is_relative_to(root.resolve()):
        raise ValueError(f"viewer reference escapes run directory: {relative}")
    if not value.is_file():
        raise FileNotFoundError(f"viewer asset is missing: {relative}")
    return value


def _pose_ok(pose: Any, frame_id: str) -> bool:
    if (not isinstance(pose, list) or len(pose) != 4 or any(not isinstance(row, list) or len(row) != 4 for row in pose)
            or any(not math.isfinite(float(v)) for row in pose for v in row)):
        raise ValueError(f"invalid T_W_Copt for frame {frame_id}")
    if any(abs(float(pose[3][i]) - target) > 1e-6 for i, target in enumerate((0, 0, 0, 1))):
        raise ValueError(f"invalid homogeneous last row in T_W_Copt for frame {frame_id}")
    # In the optical camera convention, local +Y points down. Here it maps to
    # world -Z, so world Z is the displayed up axis for this run.
    camera_down_world = [float(pose[i][1]) for i in range(3)]
    if abs(camera_down_world[2] + 1.0) > 1e-5:
        raise ValueError(f"world-Z-up display convention is not supported by frame {frame_id}")
    return True


def validate_run(run_root: Path) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    run_root = run_root.resolve()
    viewer = run_root / "viewer"
    run_manifest_path = run_root / "run_manifest.json"
    live_index_path = viewer / "live_index.json"
    live_head_path = viewer / "live_head.json"
    timeline_manifest_path = run_root / "object_timeline/manifest.json"
    room_manifest_path = run_root / "room_replay/manifest.json"
    export_path = run_root / "object_mapping/export/export_manifest.json"
    for path in (run_manifest_path, live_index_path, live_head_path, timeline_manifest_path, room_manifest_path, export_path):
        if not path.is_file():
            raise FileNotFoundError(f"required completed D1 artifact missing: {path}")

    run = read(run_manifest_path); index_bytes = live_index_path.read_bytes()
    index = json.loads(index_bytes); head = read(live_head_path)
    timeline = read(timeline_manifest_path); rooms_source = read(room_manifest_path); export = read(export_path)
    if run.get("status") != "completed" or index.get("status") != "completed" or head.get("status") != "completed":
        raise ValueError("D2 requires a completed D1 producer run, live index and live head")
    if hashlib.sha256(index_bytes).hexdigest() != head.get("live_index_sha256"):
        raise ValueError("D1 live index does not match its published live head hash")
    frames = index.get("frames", [])
    if not frames or len(frames) != int(head.get("revision_count", -1)):
        raise ValueError("D1 live head revision count does not match its index")
    scene_seq = (str(index.get("timeline_manifest", {}).get("scene_id")),
                 str(index.get("timeline_manifest", {}).get("sequence_id")))
    if scene_seq != (str(run.get("scene_id")), str(run.get("sequence_id"))):
        raise ValueError("D1 run manifest and live index scene/sequence do not match")
    if scene_seq != (str(timeline.get("scene_id")), str(timeline.get("sequence_id"))):
        raise ValueError("D1 object timeline and live index scene/sequence do not match")
    if scene_seq != (str(export.get("scene_id")), str(export.get("sequence_id"))):
        raise ValueError("D1 object export and live index scene/sequence do not match")
    if export.get("units") != "m" or not export.get("coordinate_frame"):
        raise ValueError("D1 export must declare a coordinate frame and metre units")
    if timeline.get("causality", {}).get("objects") != "prefix_state_only":
        raise ValueError("D1 object source is not a prefix-state timeline")
    run_causality = run.get("causality", {})
    index_causality = index.get("timeline_manifest", {}).get("causality", {})
    if run_causality.get("rooms") != index_causality.get("rooms"):
        raise ValueError("D1 run manifest and live index room causality do not match")
    if run_causality.get("objects") != timeline.get("causality", {}).get("objects"):
        raise ValueError("D1 run manifest and object timeline causality do not match")
    if (rooms_source.get("scene_id") != scene_seq[0]
            or int(rooms_source.get("frames", -1)) != len(frames)
            or rooms_source.get("delta_replay_equal") is not True):
        raise ValueError("D1 room replay manifest is not aligned to the scene/frame set or delta parity is unverified")

    files: dict[str, str] = {}
    def track(path: Path) -> str:
        key = path.relative_to(run_root).as_posix()
        files[key] = sha(path)
        return key
    for path in (run_manifest_path, live_index_path, live_head_path, timeline_manifest_path, room_manifest_path, export_path):
        track(path)

    previous_frame = -1; previous_revision = -1
    frame_map = []
    for row in frames:
        revision, frame_index = int(row["revision"]), int(row["frame_index"])
        frame_id = str(row["frame_id"])
        if revision <= previous_revision or frame_index <= previous_frame:
            raise ValueError("D1 revisions/frame indices must be strictly increasing")
        previous_revision, previous_frame = revision, frame_index
        if not _pose_ok(row.get("camera_pose_W_Copt"), frame_id):
            raise ValueError(f"missing camera pose for {frame_id}")
        snap_path = _inside(run_root, viewer, row["snapshot_url"])
        room_path = _inside(run_root, viewer, row["room_url"])
        geom_path = _inside(run_root, viewer, row["geometry_url"])
        joined_path = _inside(run_root, viewer, row["joined_state_url"])
        unified_path = _inside(run_root, viewer, row["unified_url"])
        snap, room_raw, geom, joined, unified = map(read, (snap_path, room_path, geom_path, joined_path, unified_path))
        room = room_raw.get("snapshot", room_raw)
        joined_pose = joined.get("camera_pose_W_Copt")
        if joined_pose is None or any(abs(float(joined_pose[i][j]) - float(row["camera_pose_W_Copt"][i][j])) > 1e-8 for i in range(4) for j in range(4)):
            raise ValueError(f"camera trajectory pose does not match joined state at source frame {frame_id}")
        if (snap.get("scene_id"), snap.get("sequence_id"), int(snap.get("frame_index", -1)), snap.get("frame_id")) != (*scene_seq, frame_index, frame_id):
            raise ValueError(f"object snapshot is not aligned to source frame {frame_id}")
        if (joined.get("source_frame_index"), joined.get("source_frame_id")) != (frame_index, frame_id):
            raise ValueError(f"joined state is not aligned to source frame {frame_id}")
        if (int(joined.get("object_revision", {}).get("source_revision", -1)),
                int(joined.get("room_revision", {}).get("source_revision", -1))) != (int(row["object_source_revision"]), int(row["room_source_revision"])):
            raise ValueError(f"object/room revision mapping mismatch at source frame {frame_id}")
        if int(room.get("revision", -1)) != int(row["room_source_revision"]):
            raise ValueError(f"room geometry revision mismatch at source frame {frame_id}")
        room_ts = room.get("timestamp_s")
        if room_ts is not None and abs(float(room_ts) - float(row["timestamp_s"])) > 1e-8:
            raise ValueError(f"room timestamp mismatch at source frame {frame_id}")
        if (unified.get("scene_id"), unified.get("sequence_id"), unified.get("frame_id"), int(unified.get("frame_index", -1))) != (*scene_seq, frame_id, frame_index):
            raise ValueError(f"unified graph revision is not aligned to source frame {frame_id}")
        snapshot_ids = {o["hypothesis_id"] for o in snap.get("objects", [])}
        geom_ids = set()
        for obj in geom.get("objects", []):
            oid = obj["hypothesis_id"]; geom_ids.add(oid)
            points = obj.get("points", []); colors = obj.get("colors", [])
            if oid not in snapshot_ids or len(points) != len(colors) or len(points) != int(obj.get("sampled_points", -1)):
                raise ValueError(f"object geometry/snapshot/display-count mismatch at {frame_id}: {oid}")
            if len(points) > MAX_SOURCE_DISPLAY_POINTS or int(obj.get("source_points", len(points))) < len(points):
                raise ValueError(f"D1 display sampling contract mismatch at {frame_id}: {oid}")
            source_cloud = _inside(run_root, run_root, obj["source_npz"])
            if sha(source_cloud) != obj.get("source_sha256"):
                raise ValueError(f"source cloud hash mismatch at {frame_id}: {oid}")
            track(source_cloud)
        if geom_ids != snapshot_ids:
            raise ValueError(f"geometry object partition differs from snapshot at {frame_id}")
        for path in (snap_path, room_path, geom_path, joined_path, unified_path):
            track(path)
        frame_map.append({"revision": revision, "source_frame_index": frame_index, "frame_id": frame_id,
                          "object_revision": int(row["object_source_revision"]),
                          "room_revision": int(row["room_source_revision"]),
                          "timestamp_s": float(row["timestamp_s"])})

    final = index.get("final_postprocess")
    if final and final.get("status") == "available":
        for key in ("geometry_url", "snapshot_url", "unified_url", "room_url"):
            path = _inside(run_root, viewer, final[key]); track(path)
            if key in ("geometry_url", "snapshot_url", "unified_url") and sha(path) != final.get({"geometry_url": "geometry_sha256", "snapshot_url": "snapshot_sha256", "unified_url": "unified_sha256"}[key]):
                raise ValueError(f"final postprocess {key} hash mismatch")
        final_snapshot = read(viewer / final["snapshot_url"])
        final_geometry = read(viewer / final["geometry_url"])
        if final_snapshot.get("stage") != "final_postprocess" or {x["hypothesis_id"] for x in final_snapshot.get("objects", [])} != {x["hypothesis_id"] for x in final_geometry.get("objects", [])}:
            raise ValueError("terminal final postprocess snapshot/geometry mismatch")

    return run, files, {
        "scene_id": scene_seq[0], "sequence_id": scene_seq[1], "status": "completed",
        "frames": frame_map, "frame_count": len(frames), "final_postprocess_status": final.get("status", "unavailable") if final else "unavailable",
        "coordinate_frame": export["coordinate_frame"], "units": export["units"],
        "objects_causality": timeline.get("causality", {}).get("objects", "unavailable"),
        "rooms_causality": run_causality.get("rooms", "unavailable"),
        "room_source_provenance": {
            "manifest": "room_replay/manifest.json",
            "manifest_sha256": sha(room_manifest_path),
            "kind": rooms_source.get("kind"),
            "scene_id": rooms_source.get("scene_id"),
            "frames": rooms_source.get("frames"),
            "delta_replay_equal": rooms_source.get("delta_replay_equal"),
            "config_sha256": rooms_source.get("config_sha256"),
            "capture_manifest_sha256": rooms_source.get("capture_manifest_sha256"),
            "m2_m3_binary_sha256": rooms_source.get("m2_m3_binary_sha256"),
            "dude_binary_sha256": rooms_source.get("dude_binary_sha256"),
        },
        "membership_causality": run.get("causality", {}).get("membership", "unavailable"),
        "room_semantics_causality": run.get("causality", {}).get("room_semantics", "unavailable"),
        "prefix_admission": run.get("prefix_admission", "unavailable"),
    }


def build(run_root: Path, version: str = "v1") -> dict[str, Any]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", version):
        raise ValueError("version may contain only letters, digits, underscore and hyphen")
    run_root = Path(run_root).resolve()
    viewer = run_root / "viewer"
    run, tracked, state = validate_run(run_root)
    template = ROOT / "scripts/causal_timeline_viewer.html"
    renderer = ROOT / "scripts/xyz_renderer.js"
    fixture = ROOT / "scripts/xyz_renderer_fixture.html"
    if not template.is_file() or not renderer.is_file() or not fixture.is_file():
        raise FileNotFoundError("D2 source HTML/renderer/synthetic fixture is incomplete")
    html_name = f"index_d2_{version}.html"
    renderer_name = f"xyz_renderer_d2_{version}.js"
    fixture_name = f"xyz_renderer_fixture_{version}.html"
    manifest_name = f"d2_xyz_manifest_{version}.json"
    outputs = {"index.html": viewer / "index.html", html_name: viewer / html_name, renderer_name: viewer / renderer_name,
               fixture_name: viewer / fixture_name,
               manifest_name: viewer / manifest_name}
    html_bytes = template.read_text(encoding="utf-8").replace('./xyz_renderer.js', f'./{renderer_name}').encode("utf-8")
    fixture_bytes = fixture.read_text(encoding="utf-8").replace('./xyz_renderer.js', f'./{renderer_name}').encode("utf-8")
    legacy_index = viewer / "index.html"
    expected_legacy = template.read_bytes()
    if legacy_index.exists() and legacy_index.read_bytes() not in {expected_legacy, redirect_index_html()}:
        raise FileExistsError(f"refusing to replace an unrecognized viewer index: {legacy_index}")
    source_bytes = {"index.html": redirect_index_html(), html_name: html_bytes,
                    renderer_name: renderer.read_bytes(), fixture_name: fixture_bytes}
    for name, payload in source_bytes.items():
        path = outputs[name]
        if path.exists() and path.read_bytes() != payload:
            raise FileExistsError(f"refusing to overwrite different D2 viewer asset: {path}")
    if outputs[manifest_name].exists():
        existing_manifest = read(outputs[manifest_name])
        if existing_manifest.get("status") != "running" or existing_manifest.get("run_id") != run.get("run_id"):
            raise FileExistsError(f"refusing to overwrite existing D2 manifest: {outputs[manifest_name]}")
    for name, payload in source_bytes.items():
        atomic_write_bytes(outputs[name], payload)
    tracked["scripts/build_d2_xyz_viewer.py"] = sha(Path(__file__).resolve())
    tracked["scripts/causal_timeline_viewer.html"] = sha(template)
    tracked["scripts/xyz_renderer.js"] = sha(renderer)
    tracked["scripts/xyz_renderer_fixture.html"] = sha(fixture)
    output_hashes = {name: sha(path) for name, path in outputs.items() if name != manifest_name}
    manifest = {
        "schema": "d2-xyz-viewer/v1", "status": "completed", "run_id": run.get("run_id"),
        "source_execution_mode": run.get("execution_mode"), "viewer_only": True,
        "renderer": {"api": "browser WebGL", "external_dependencies": [], "cdn": False,
                     "xyz": "object point samples + camera position trajectory", "camera_frustum": "not rendered",
                     "camera_pose_field": "T_W_Copt", "world_up": "Z (optical +Y maps to world -Z; validated on every frame)"},
        "spatial_panels": {"object_clouds_and_camera_path": "XYZ", "room_polygons_and_adjacency": "XY only; no room volumes inferred"},
        "coordinate_contract": {"frame": state["coordinate_frame"], "units": state["units"], "axes": "world X,Y,Z in metres"},
        "revision_alignment": state["frames"], "frame_count": state["frame_count"],
        "stage_contract": {"online_hypotheses": "prefix snapshot and geometry at selected revision",
                           "prefix_admission": state["prefix_admission"],
                           "final_postprocess": "separate terminal artifact; not backfilled"},
        "causality": {"objects": state["objects_causality"], "rooms": state["rooms_causality"],
                      "membership": state["membership_causality"], "room_semantics": state["room_semantics_causality"]},
        "room_source_provenance": state["room_source_provenance"],
        "unavailable_states_are_preserved": True,
        "display_policy": {"source_max_points_per_object": MAX_SOURCE_DISPLAY_POINTS,
                           "per_revision_counts": "geometry JSON sampled_points/source_points",
                           "source_clouds_unchanged": True, "display_samples_used_for_scoring": False},
        "memory_policy": {"json_geometry_cache": "existing LRU, maximum 6 revisions",
                          "gpu_buffers": "one current revision; reused and replaced, deleted on dispose",
                          "live_update_camera_fit": "never automatic after initial frame"},
        "viewer_version": version, "viewer_entrypoint": html_name,
        "synthetic_fixture_entrypoint": fixture_name,
        "input_files_sha256": dict(sorted(tracked.items())), "output_files_sha256": output_hashes,
    }
    temp_path = outputs[manifest_name].with_name(f".{manifest_name}.tmp")
    temp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp_path, outputs[manifest_name])
    write_entrypoint_pointer(viewer, manifest_name, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="completed D1 run directory; input artifacts are read-only")
    parser.add_argument("--version", default="v1", help="new D2 asset suffix; existing versions are preserved")
    args = parser.parse_args()
    manifest = build(args.run, args.version)
    print(json.dumps({"status": manifest["status"], "run_id": manifest["run_id"],
                      "viewer": str((args.run.resolve() / "viewer" / manifest["viewer_entrypoint"])),
                      "frames": manifest["frame_count"], "room_volumes": False,
                      "dependencies": manifest["renderer"]["external_dependencies"]}, indent=2))


if __name__ == "__main__":
    main()
