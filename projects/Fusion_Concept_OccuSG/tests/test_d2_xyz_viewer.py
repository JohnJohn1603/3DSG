from __future__ import annotations

import hashlib
import json
import functools
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from scripts.build_d2_xyz_viewer import (bootstrap, build, validate_entrypoint_bundle,
                                         validate_run, redirect_index_html,
                                         terminalize_live_viewer)
from scripts.scene_launcher import resolve_d2_viewer_entrypoint
from scripts.serve_causal_timeline_viewer import ManifestPointerHandler


ROOT = Path(__file__).resolve().parents[1]


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_run(root: Path) -> Path:
    run = root / "example-scene-short-route"
    viewer = run / "viewer"
    obj_root = run / "object_timeline"
    (obj_root / "snapshots").mkdir(parents=True)
    (obj_root / "geometry").mkdir()
    (run / "room_replay/prefix_snapshots").mkdir(parents=True)
    (run / "joined/revisions").mkdir(parents=True)
    (run / "joined/graph_states").mkdir(parents=True)
    (viewer / "geometry").mkdir(parents=True)
    sequence = "example-instance-route"
    frame_id = "example-0000"
    pose = [[-1, 0, 0, 1], [0, 0, -1, 2], [0, -1, 0, 1.5], [0, 0, 0, 1]]
    cloud = obj_root / "geometry/hypothesis_a.npz"
    cloud.write_bytes(b"fixture cloud source bytes")
    cloud_hash = sha(cloud)
    snap = {"scene_id": "example-scene", "sequence_id": sequence, "revision": 1, "frame_index": 0,
            "frame_id": frame_id, "timestamp_s": 0.0, "objects": [{"hypothesis_id": "hypothesis_a"}]}
    snap_path = obj_root / "snapshots/rev-000001.json"
    dump(snap_path, snap)
    room = {"snapshot": {"scene_id": "example-scene", "sequence_id": sequence, "revision": 1,
                          "timestamp_s": 0.0, "nodes": [{"kind": "room", "node_id": "room:1"}]}}
    room_path = run / "room_replay/prefix_snapshots/rev-000001.json"
    dump(room_path, room)
    geometry = {"schema": "d1-live-derived-geometry/v1", "objects": [{
        "hypothesis_id": "hypothesis_a", "points": [[0, 0, 0], [1, 0, 0]], "colors": [[255, 0, 0], [0, 255, 0]],
        "sampled_points": 2, "source_points": 4, "source_npz": "object_timeline/geometry/hypothesis_a.npz",
        "source_sha256": cloud_hash,
    }]}
    geometry_path = viewer / "geometry/rev-000001.json"
    dump(geometry_path, geometry)
    unified_path = run / "joined/graph_states/rev-000001.json"
    dump(unified_path, {"scene_id": "example-scene", "sequence_id": sequence, "frame_id": frame_id,
                        "frame_index": 0, "edges": [], "membership_availability": {"status": "unavailable"}})
    joined_path = run / "joined/revisions/rev-000001.json"
    dump(joined_path, {"source_frame_index": 0, "source_frame_id": frame_id, "timestamp_s": 0.0,
                       "camera_pose_W_Copt": pose,
                       "object_revision": {"source_revision": 1}, "room_revision": {"source_revision": 1}})
    timeline = {"status": "complete", "schema": "conceptgraphs-causal-object-timeline/v1", "scene_id": "example-scene",
                "sequence_id": sequence, "frame_count": 1,
                "causality": {"objects": "prefix_state_only", "rooms": "fixed_final_room_map"}}
    dump(obj_root / "manifest.json", timeline)
    dump(run / "room_replay/manifest.json", {
        "kind": "fixture-room-replay", "scene_id": "example-scene", "frames": 1,
        "delta_replay_equal": True, "config_sha256": "fixture-config",
        "capture_manifest_sha256": "fixture-capture", "m2_m3_binary_sha256": "fixture-m2m3",
        "dude_binary_sha256": "fixture-dude",
    })
    export = {"scene_id": "example-scene", "sequence_id": sequence, "coordinate_frame": "conceptgraphs_world_from_capture_contract",
              "units": "m"}
    dump(run / "object_mapping/export/export_manifest.json", export)
    frame = {"revision": 1, "frame_index": 0, "frame_id": frame_id, "timestamp_s": 0.0,
             "object_source_revision": 1, "room_source_revision": 1, "camera_pose_W_Copt": pose,
             "snapshot_url": "../object_timeline/snapshots/rev-000001.json",
             "room_url": "../room_replay/prefix_snapshots/rev-000001.json",
             "geometry_url": "geometry/rev-000001.json", "joined_state_url": "../joined/revisions/rev-000001.json",
             "unified_url": "../joined/graph_states/rev-000001.json"}
    index = {"status": "completed", "timeline_manifest": {"scene_id": "example-scene", "sequence_id": sequence,
             "causality": {"rooms": "prefix_state_only_native_replay_reference"}},
             "frames": [frame], "final_postprocess": None, "evidence_index": "evidence_index.json"}
    index_path = viewer / "live_index.json"
    dump(index_path, index)
    dump(viewer / "live_head.json", {"status": "completed", "revision_count": 1, "live_index_sha256": sha(index_path)})
    dump(run / "run_manifest.json", {"status": "completed", "run_id": "example-run",
                                      "execution_mode": "live_processing_from_cached_inputs", "scene_id": "example-scene",
                                      "sequence_id": sequence,
                                      "causality": {"membership": "unavailable_elevation_scope",
                                                    "room_semantics": "unavailable_membership_scope",
                                                    "objects": "prefix_state_only",
                                                    "rooms": "prefix_state_only_native_replay_reference"},
                                      "prefix_admission": "unavailable"})
    return run


def test_d2_build_is_viewer_only_and_records_contract_and_hashes(tmp_path: Path):
    run = fixture_run(tmp_path)
    before = sha(run / "object_timeline/geometry/hypothesis_a.npz")
    result = build(run, "test")
    viewer = run / "viewer"
    assert result["viewer_only"] is True
    assert result["renderer"]["external_dependencies"] == []
    assert result["coordinate_contract"]["units"] == "m"
    assert result["spatial_panels"]["object_clouds_and_camera_path"] == "XYZ"
    assert result["spatial_panels"]["room_polygons_and_adjacency"].startswith("XY only")
    assert result["causality"]["membership"] == "unavailable_elevation_scope"
    assert result["causality"]["rooms"] == "prefix_state_only_native_replay_reference"
    assert result["room_source_provenance"]["kind"] == "fixture-room-replay"
    assert result["stage_contract"]["prefix_admission"] == "unavailable"
    assert (viewer / "index_d2_test.html").is_file()
    assert (viewer / "xyz_renderer_d2_test.js").is_file()
    assert (viewer / "xyz_renderer_fixture_test.html").is_file()
    assert (viewer / "index.html").read_bytes() == redirect_index_html()
    assert result["output_files_sha256"]["xyz_renderer_d2_test.js"] == sha(viewer / "xyz_renderer_d2_test.js")
    pointer = json.loads((viewer / "d2_viewer_entrypoint.json").read_text())
    assert pointer["schema"] == "d2-viewer-entrypoint/v1"
    assert pointer["manifest_path"] == "d2_xyz_manifest_test.json"
    assert pointer["entrypoint"] == result["viewer_entrypoint"]
    resolved = resolve_d2_viewer_entrypoint(run)
    assert resolved["entrypoint"] == result["viewer_entrypoint"]
    assert resolved["manifest"]["viewer_version"] == "test"
    assert "xyz_renderer_d2_test.js" in validate_entrypoint_bundle(viewer)["script_assets"]
    assert sha(run / "object_timeline/geometry/hypothesis_a.npz") == before


def test_d2_bootstrap_is_openable_before_first_revision_then_finalizes_same_entrypoint(tmp_path: Path):
    run = fixture_run(tmp_path)
    viewer = run / "viewer"
    run_manifest = json.loads((run / "run_manifest.json").read_text())
    run_manifest["status"] = "started"
    dump(run / "run_manifest.json", run_manifest)
    live_index_path = viewer / "live_index.json"
    live_index = json.loads(live_index_path.read_text())
    live_index.update({"status": "starting", "frames": []})
    dump(live_index_path, live_index)
    dump(viewer / "live_head.json", {"status": "starting", "revision_count": 0,
                                      "live_index_sha256": sha(live_index_path)})

    running = bootstrap(run, "live")
    assert running["status"] == "running"
    assert running["viewer_entrypoint"] == "index_d2_live.html"
    pointer = json.loads((viewer / "d2_viewer_entrypoint.json").read_text())
    assert pointer["status"] == "ready"
    assert pointer["pipeline_status"] == "running"
    resolved = resolve_d2_viewer_entrypoint(run)
    assert resolved["pipeline_status"] == "running"
    assert resolved["entrypoint"] == running["viewer_entrypoint"]

    run_manifest["status"] = "completed"
    dump(run / "run_manifest.json", run_manifest)
    live_index["status"] = "completed"
    live_index["frames"] = json.loads((viewer / "live_index.json").read_text())["frames"]
    # Restore the complete one-frame source fixture so the normal D2 validator
    # exercises the exact completion/finalization path.
    original = fixture_run(tmp_path / "complete")
    for rel in ("run_manifest.json", "viewer/live_index.json", "viewer/live_head.json",
                "object_timeline/manifest.json", "room_replay/manifest.json",
                "object_mapping/export/export_manifest.json", "viewer/geometry/rev-000001.json",
                "object_timeline/snapshots/rev-000001.json", "room_replay/prefix_snapshots/rev-000001.json",
                "joined/revisions/rev-000001.json", "joined/graph_states/rev-000001.json",
                "object_timeline/geometry/hypothesis_a.npz"):
        target = run / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((original / rel).read_bytes())
    final = build(run, "live")
    assert final["status"] == "completed"
    assert final["viewer_entrypoint"] == running["viewer_entrypoint"]
    pointer = json.loads((viewer / "d2_viewer_entrypoint.json").read_text())
    assert pointer["status"] == "completed"
    assert pointer["pipeline_status"] == "completed"
    assert resolve_d2_viewer_entrypoint(run)["pipeline_status"] == "completed"


def test_d2_bootstrap_refuses_to_start_after_a_revision_exists(tmp_path: Path):
    run = fixture_run(tmp_path)
    run_manifest = json.loads((run / "run_manifest.json").read_text())
    run_manifest["status"] = "running"
    dump(run / "run_manifest.json", run_manifest)
    live_index = json.loads((run / "viewer/live_index.json").read_text())
    live_index["status"] = "running"
    dump(run / "viewer/live_index.json", live_index)
    dump(run / "viewer/live_head.json", {"status": "running", "revision_count": 1})
    with pytest.raises(ValueError, match="before the first producer revision"):
        bootstrap(run, "live")


@pytest.mark.parametrize(("run_status", "viewer_status"), [
    ("completed", "completed"), ("failed_before_capture", "failed"),
    ("cancelled_before_capture", "cancelled"),
])
def test_live_viewer_terminal_status_tracks_authoritative_run(tmp_path: Path, run_status: str, viewer_status: str):
    run = fixture_run(tmp_path)
    run_doc = json.loads((run / "run_manifest.json").read_text())
    run_doc["status"] = "started"
    dump(run / "run_manifest.json", run_doc)
    viewer = run / "viewer"
    index_path = viewer / "live_index.json"
    index = json.loads(index_path.read_text())
    index.update({"status": "starting", "frames": [], "run_id": run_doc["run_id"]})
    dump(index_path, index)
    head = json.loads((viewer / "live_head.json").read_text())
    head.update({"status": "starting", "revision_count": 0, "live_index_sha256": sha(index_path)})
    dump(viewer / "live_head.json", head)
    bootstrap(run, "live")

    run_doc["status"] = run_status
    dump(run / "run_manifest.json", run_doc)
    index["status"] = "running"
    dump(index_path, index)
    head["status"] = "running"
    head["live_index_sha256"] = sha(index_path)
    dump(viewer / "live_head.json", head)
    result = terminalize_live_viewer(run, status=viewer_status,
        error="fixture failure" if viewer_status == "failed" else None)

    final_index = json.loads(index_path.read_text())
    final_head = json.loads((viewer / "live_head.json").read_text())
    final_manifest = json.loads((viewer / "d2_xyz_manifest_live.json").read_text())
    pointer = json.loads((viewer / "d2_viewer_entrypoint.json").read_text())
    assert final_index["status"] == final_head["status"] == viewer_status
    assert final_head["live_index_sha256"] == sha(index_path)
    assert final_manifest["status"] == viewer_status
    assert final_manifest["source_run_status"] == run_status
    assert pointer["pipeline_status"] == pointer["status"] == viewer_status
    assert result["revision_count"] == 0
    assert resolve_d2_viewer_entrypoint(run)["pipeline_status"] == viewer_status


def test_launcher_rejects_stale_d2_entrypoint_pointer(tmp_path: Path):
    run = fixture_run(tmp_path)
    build(run, "pointer")
    pointer_path = run / "viewer/d2_viewer_entrypoint.json"
    pointer = json.loads(pointer_path.read_text())
    pointer["manifest_sha256"] = "0" * 64
    dump(pointer_path, pointer)
    with pytest.raises(ValueError, match="manifest hash differs"):
        resolve_d2_viewer_entrypoint(run)


def test_entrypoint_bundle_rejects_renderer_hash_mismatch(tmp_path: Path):
    run = fixture_run(tmp_path)
    build(run, "asset-check")
    renderer = run / "viewer/xyz_renderer_d2_asset-check.js"
    renderer.write_text(renderer.read_text() + "\n// tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="output asset hash mismatch|JavaScript asset hash mismatch"):
        validate_entrypoint_bundle(run / "viewer")


def test_legacy_index_http_path_redirects_to_manifest_selected_entrypoint(tmp_path: Path):
    run = fixture_run(tmp_path)
    build(run, "redirect-check")
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        ManifestPointerHandler, directory=str(tmp_path)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/{run.relative_to(tmp_path).as_posix()}/viewer/index.html?live=1"
        with urlopen(url, timeout=2) as response:
            assert response.status == 200
            assert response.geturl().endswith("/viewer/index_d2_redirect-check.html?live=1")
            assert b"createXyzViewer" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_d2_rejects_live_head_hash_mismatch(tmp_path: Path):
    run = fixture_run(tmp_path)
    head = json.loads((run / "viewer/live_head.json").read_text())
    head["live_index_sha256"] = "0" * 64
    dump(run / "viewer/live_head.json", head)
    with pytest.raises(ValueError, match="does not match its published live head"):
        validate_run(run)


def test_d2_rejects_room_causality_disagreement_between_run_and_live_index(tmp_path: Path):
    run = fixture_run(tmp_path)
    path = run / "viewer/live_index.json"
    index = json.loads(path.read_text())
    index["timeline_manifest"]["causality"]["rooms"] = "fixed_final_room_map"
    dump(path, index)
    head = json.loads((run / "viewer/live_head.json").read_text())
    head["live_index_sha256"] = sha(path)
    dump(run / "viewer/live_head.json", head)
    with pytest.raises(ValueError, match="room causality do not match"):
        validate_run(run)


@pytest.mark.parametrize("field,value,reason", [
    ("scene_id", "other-scene", "scene/sequence"),
    ("sequence_id", "other-route", "scene/sequence"),
])
def test_d2_rejects_scene_or_sequence_mismatch(tmp_path: Path, field: str, value: str, reason: str):
    run = fixture_run(tmp_path)
    path = run / "object_mapping/export/export_manifest.json"
    export = json.loads(path.read_text()); export[field] = value; dump(path, export)
    with pytest.raises(ValueError, match=reason):
        validate_run(run)


def test_d2_rejects_room_revision_or_pose_misalignment(tmp_path: Path):
    run = fixture_run(tmp_path)
    path = run / "room_replay/prefix_snapshots/rev-000001.json"
    room = json.loads(path.read_text()); room["snapshot"]["revision"] = 2; dump(path, room)
    with pytest.raises(ValueError, match="room geometry revision mismatch"):
        validate_run(run)


def test_d2_rejects_unsupported_world_up_orientation(tmp_path: Path):
    run = fixture_run(tmp_path)
    index_path = run / "viewer/live_index.json"
    index = json.loads(index_path.read_text())
    index["frames"][0]["camera_pose_W_Copt"][2][1] = 1
    dump(index_path, index)
    head_path = run / "viewer/live_head.json"
    head = json.loads(head_path.read_text()); head["live_index_sha256"] = sha(index_path); dump(head_path, head)
    with pytest.raises(ValueError, match="world-Z-up display convention is not supported"):
        validate_run(run)


def test_d2_rejects_corrupt_source_cloud_and_preserves_existing_output(tmp_path: Path):
    run = fixture_run(tmp_path)
    cloud = run / "object_timeline/geometry/hypothesis_a.npz"
    cloud.write_bytes(b"different bytes")
    with pytest.raises(ValueError, match="source cloud hash mismatch"):
        validate_run(run)
    run = fixture_run(tmp_path / "second")
    build(run, "safe")
    page = run / "viewer/index_d2_safe.html"
    original = page.read_bytes(); page.write_bytes(b"sentinel")
    with pytest.raises(FileExistsError, match="refusing to overwrite different D2 viewer asset"):
        build(run, "safe")
    assert page.read_bytes() == b"sentinel" and original


def test_viewer_keeps_prefix_trajectory_and_stale_load_guard():
    html = (ROOT / "scripts/causal_timeline_viewer.html").read_text()
    assert "trajectory=INDEX.frames.slice(0,targetIndex+1)" in html
    assert "atomicRevisionGate.request" in html
    assert "setRevisionLoadStatus('Loading '" in html
    assert "assertFrameBundle(frame,snap,room,geom,graph,finalMode)" in html
    assert "await preloadFrameEvidence(frame)" in html
    assert "loadToken" not in html
    assert "xyzViewer.update" in html
    assert "$('fitXYZ').onclick" in html and "$('resetXYZ').onclick" in html
    assert 'id="panXYZ" aria-pressed="false"' in html
    assert ".xyz-map canvas.xyz-gizmo{position:absolute" in html
    assert "LIVE_FINAL=INDEX.final_postprocess||null;$('stageFinal').disabled=!LIVE_FINAL" in html
    assert "ACTIVE_STAGE==='final_postprocess'?'final postprocess status':'prefix admission'" in html
    assert "prefix admission ${this.scene.prefixAdmissionStatus}" in (ROOT / "scripts/xyz_renderer.js").read_text()
    assert "MAX_CACHE=6" in html
    assert "deleteBuffer" in (ROOT / "scripts/xyz_renderer.js").read_text()
    fixture = (ROOT / "scripts/xyz_renderer_fixture.html").read_text()
    assert "SYNTHETIC XYZ RENDERER FIXTURE" in fixture
    assert "points:[[1,0,0]]" in fixture and "points:[[0,1,0]]" in fixture and "points:[[0,0,1]]" in fixture
    renderer = (ROOT / "scripts/xyz_renderer.js").read_text()
    assert "panMode = !this.panMode" in renderer and "pan: this.panMode ||" in renderer
    assert "window.createAtomicRevisionGate = createAtomicRevisionGate" in renderer
    assert "validate(data)" in renderer
    assert 'canvas.addEventListener("wheel"' in renderer
    assert "no room volumes are inferred" in html
