from __future__ import annotations

import functools
import hashlib
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen

import numpy as np

from integration_v2 import live_processing as lp
from integration_v2.room_belief_commit_session import RoomBeliefCommitSession
from scripts.build_d2_xyz_viewer import bootstrap, validate_served_entrypoint
from scripts.serve_causal_timeline_viewer import ManifestPointerHandler


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _event(branch: str, event_seq: int, state_path: Path, state_sha: str,
           source_hashes: dict[str, str], asset_sha: dict[str, str]) -> dict:
    payload = {
        "schema": "t2-d-branch-event/v1", "branch": branch,
        "event_seq": event_seq, "source_revision": event_seq,
        "source_frame_id": f"frame-{event_seq - 1}", "source_frame_index": event_seq - 1,
        "source_timestamp_s": event_seq * 0.04, "source_timestamp_clock_domain": "habitat_simulation",
        "source_hashes": source_hashes,
        "state_path": state_path.relative_to(state_path.parents[3]).as_posix(),
        "state_sha256": state_sha, "asset_sha256": asset_sha,
    }
    payload["event_sha256"] = hashlib.sha256(lp.canonical_json(payload)).hexdigest()
    return payload


def _make_branch_pair(run: Path, frame_index: int) -> tuple[dict, dict, dict, dict]:
    seq = frame_index + 1
    fid = f"frame-{frame_index}"
    scene, sequence = "fixture-scene", "fixture-sequence"
    source_hashes = {key: hashlib.sha256(f"{key}-{frame_index}".encode()).hexdigest()
                     for key in ("rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json")}
    object_cloud_dir = run / "branches/objects/clouds"
    object_state_dir = run / "branches/objects/snapshots"
    room_state_dir = run / "branches/rooms/snapshots"
    object_cloud_dir.mkdir(parents=True, exist_ok=True)
    object_state_dir.mkdir(parents=True, exist_ok=True)
    room_state_dir.mkdir(parents=True, exist_ok=True)
    cloud = object_cloud_dir / f"rev-{frame_index:06d}.npz"
    np.savez(cloud, xyz=np.asarray([[1.0, 4.5, 0.2]], dtype=np.float32),
             rgb=np.asarray([[200, 100, 50]], dtype=np.uint8))
    cloud_sha = _digest(cloud)
    camera_pose = [[0.0, 0.0, -1.0, 1.0], [1.0, 0.0, 0.0, 4.58],
                   [0.0, -1.0, 0.0, 1.62], [0.0, 0.0, 0.0, 1.0]]
    object_state = {
        "schema": "t2-d-object-snapshot/v1", "scene_id": scene, "sequence_id": sequence,
        "revision": seq, "frame_index": frame_index, "frame_id": fid,
        "timestamp_s": seq * 0.04, "camera_pose_W_Copt": camera_pose,
        "future_observations_used": False,
        "objects": [{"hypothesis_id": "hypothesis-1", "causal_object_id": "hypothesis-1",
            "detection_count": 1, "distinct_frame_count": 1, "distinct_pose_count": 1,
            "class_history_raw": ["chair"], "observation_ids": [],
            "cloud_npz": f"clouds/{cloud.name}", "cloud_sha256": cloud_sha, "point_count": 1}],
    }
    object_state_path = object_state_dir / f"rev-{frame_index:06d}.json"
    lp._write_json(object_state_path, object_state)
    object_state_sha = _digest(object_state_path)
    room_state = {"schema_version": 1, "kind": "snapshot", "snapshot": {
        "scene_id": scene, "sequence_id": sequence, "revision": seq,
        "timestamp_s": seq * 0.04,
        "nodes": [{"kind": "room", "node_id": "room-1", "attributes": {
            "outer_ring": [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]}}],
        "edges": [],
    }}
    room_state_path = room_state_dir / f"rev-{frame_index:06d}.json"
    lp._write_json(room_state_path, room_state)
    room_state_sha = _digest(room_state_path)
    object_event = _event("objects", seq, object_state_path, object_state_sha,
                          source_hashes, {f"branches/objects/clouds/{cloud.name}": cloud_sha})
    room_event = _event("rooms", seq, room_state_path, room_state_sha, source_hashes, {})
    return object_event, room_event, object_state, room_state


def test_real_join_semantic_publication_and_d2_http_load(tmp_path: Path):
    run = tmp_path / "pilot-fixture"
    for rel in ("branches/objects/acks", "branches/rooms/acks", "joined/revisions",
                "joined/graph_states", "semantics/overlays"):
        (run / rel).mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source-c2-fixture"
    evidence_dir = source / "mapping/diagnostic"
    evidence_dir.mkdir(parents=True)
    lp._write_json(evidence_dir / "evidence_manifest.json", {"schema": "fixture-evidence/v1", "observations": {}})
    lp._write_json(run / "run_manifest.json", {
        "schema": "camera-live-pilot/v1", "run_id": "pilot-fixture",
        "execution_mode": "camera_live_file_backed", "status": "started"})
    viewer, _ = lp._prepare_viewer(source, run, "fixture-scene", "fixture-sequence")
    base = lp._read(viewer / "live_index.json")
    base["room_belief_variants"] = ["mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral"]
    base["room_belief_scope_modes"] = ["verified_only"]
    lp._write_json(viewer / "live_index.json", base)
    head = lp._read(viewer / "live_head.json")
    head["live_index_sha256"] = _digest(viewer / "live_index.json")
    lp._write_json(viewer / "live_head.json", head)
    bootstrap(run, version="t4regression")

    handler = functools.partial(ManifestPointerHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/pilot-fixture/viewer/index.html?live=1"
    served = validate_served_entrypoint(viewer, url)
    assert served["pointer"]["entrypoint"] == "index_d2_t4regression.html"

    session = RoomBeliefCommitSession.open(
        config_path=lp.ROOT / "configs/room_semantics/t4_live_room_belief_v1.json",
        overlay_root=run / "semantics/overlays", admission_min_detections=3)
    frames: list[dict] = []
    try:
        for frame_index in range(2):
            object_event, room_event, object_state, room_state = _make_branch_pair(run, frame_index)
            joined_event = lp._join_one(run, viewer, base, frames, object_event, room_event,
                publish_index=False, publish_event=False, acknowledge=False)
            frame = frames[-1]
            graph_path = lp._resolve_viewer_artifact_url(run, viewer, frame["unified_url"], label="fixture graph")
            graph = lp._read(graph_path)
            object_path = lp._resolve_run_artifact(run, object_event["state_path"], label="fixture object state")
            room_path = lp._resolve_run_artifact(run, room_event["state_path"], label="fixture room state")
            envelope = {
                "event_id": f"unified:{frame_index}:{frame['frame_id']}", "run_id": "pilot-fixture",
                "scene_id": "fixture-scene", "sequence_id": "fixture-sequence",
                "frame_index": frame_index, "frame_id": frame["frame_id"],
                "timestamp_s": frame["timestamp_s"], "unified_revision": frame["revision"],
                "graph_revision_id": graph["graph_revision_id"], "graph_path": str(graph_path),
                "graph_sha256": _digest(graph_path), "object_revision": object_state["revision"],
                "object_state_sha256": _digest(object_path), "room_revision": room_state["snapshot"]["revision"],
                "room_state_sha256": _digest(room_path),
                "source_hashes": dict(object_event["source_hashes"]) | {
                    "object_event": object_event["event_sha256"], "room_event": room_event["event_sha256"]},
                "current_observations": [],
                "objects": [{"object_id": "hypothesis-1", "observation_ids": [], "detection_count": 1,
                    "membership": {"status": "unavailable", "reason": "unavailable_elevation_scope"}}],
                "rooms": [{"node_id": "room-1"}],
                "scope": {"status": "unavailable", "evidence_status": "unavailable",
                    "reason": "unavailable_elevation_scope"},
            }
            semantic = session.update_after_unified_commit(envelope)
            assert semantic["overlay"]["room_semantics_availability"]["status"] == "not_scored"
            overlay_path = Path(semantic["overlay_path"])
            frame["room_belief_overlay_url"] = lp._viewer_artifact_url(run, viewer, overlay_path)
            frame["room_belief_overlay_sha256"] = semantic["overlay_sha256"]
            lp._publish_joined_semantic_commit(run, viewer, base, frames, joined_event,
                                               object_event, room_event)

            with urlopen(urljoin(url.rsplit("/", 1)[0] + "/", "live_head.json?test=1"), timeout=2) as response:
                live_head = json.loads(response.read())
            with urlopen(urljoin(url.rsplit("/", 1)[0] + "/", "live_index.json?test=1"), timeout=2) as response:
                index_bytes = response.read()
            assert hashlib.sha256(index_bytes).hexdigest() == live_head["live_index_sha256"]
            live_index = json.loads(index_bytes)
            assert live_head["revision_count"] == len(live_index["frames"]) == frame_index + 1
            assert live_index["frames"][-1]["frame_id"] == frame["frame_id"]
            assert semantic["overlay"]["scope_status"] == "unavailable"

        # D2 consumes these exact viewer-relative URLs; the original prefix
        # remains seekable after a later revision is committed.
        viewer_base = url.rsplit("/", 1)[0] + "/"
        first = live_index["frames"][0]
        assert first["frame_id"] == "frame-0"
        urls = [first["snapshot_url"], first["room_url"], first["geometry_url"],
                first["unified_url"], first["room_belief_overlay_url"], first["joined_state_url"]]
        for artifact_url in urls:
            with urlopen(urljoin(viewer_base, artifact_url), timeout=2) as response:
                assert response.status == 200
                assert response.read()
        with urlopen(url, timeout=2) as response:  # index.html redirects through the pointer
            page = response.read().decode("utf-8")
        assert "LIVE_MODE" in page and "window.createXyzViewer" in page
        assert lp._read(viewer / "live_head.json")["revision_count"] == 2
        for branch in ("objects", "rooms"):
            assert (run / f"branches/{branch}/acks/000001.json").is_file()
            assert (run / f"branches/{branch}/acks/000002.json").is_file()
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_viewer_url_resolution_rejects_external_and_ambiguous_references(tmp_path: Path):
    run = tmp_path / "run"
    viewer = run / "viewer"
    viewer.mkdir(parents=True)
    inside = viewer / "graph.json"
    inside.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    assert lp._resolve_viewer_artifact_url(run, viewer, "graph.json", label="graph") == inside
    for unsafe in ("../../outside.json", "%2e%2e/%2e%2e/outside.json", str(inside),
                   "http://127.0.0.1/graph.json", "graph.json?retry=1"):
        try:
            lp._resolve_viewer_artifact_url(run, viewer, unsafe, label="graph")
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError(f"unsafe/ambiguous URL reference was accepted: {unsafe}")
