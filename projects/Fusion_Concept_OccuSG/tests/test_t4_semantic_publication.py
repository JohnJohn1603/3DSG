from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from integration_v2 import live_processing as lp


def write_json(path: Path, value: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True) + "\n").encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_published_pointer_contains_hash_bound_graph_and_overlay_before_ack(tmp_path: Path, monkeypatch):
    run = tmp_path / "run"
    viewer = run / "viewer"
    viewer.mkdir(parents=True)
    (run / "branches/objects/acks").mkdir(parents=True)
    (run / "branches/rooms/acks").mkdir(parents=True)
    graph_sha = write_json(viewer / "graph.json", {"graph_revision_id": "unified-000001", "scene_id": "scene",
        "sequence_id": "sequence", "object_revision": 4, "room_revision": 2})
    overlay_sha = write_json(viewer / "overlay.json", {
        "schema": "t4-room-belief-semantic-overlay/v1", "status": "complete",
        "graph_revision_id": "unified-000001", "graph_sha256": graph_sha,
        "frame_id": "frame-0", "frame_index": 0, "unified_revision": 1,
        "scene_id": "scene", "sequence_id": "sequence", "object_revision": 4, "room_revision": 2,
        "source_hashes": {"packet": "a" * 64},
    })
    joined_sha = write_json(run / "joined/revisions/rev-000001.json", {"joined_revision": 1})
    frame = {"revision": 1, "frame_index": 0, "frame_id": "frame-0", "timestamp_s": 1.0,
        "unified_url": "graph.json", "unified_sha256": graph_sha,
        "room_belief_overlay_url": "overlay.json", "room_belief_overlay_sha256": overlay_sha,
        "joined_state_url": "../joined/revisions/rev-000001.json", "joined_state_sha256": joined_sha,
        "object_source_revision": 4, "room_source_revision": 2}
    frames = [frame]
    event = {"joined_revision": 1, "source_frame_id": "frame-0", "source_frame_index": 0}
    source_hashes = {"packet": "a" * 64}
    object_event = {"event_seq": 1, "event_sha256": "b" * 64, "source_frame_id": "frame-0", "source_hashes": source_hashes}
    room_event = {"event_seq": 1, "event_sha256": "c" * 64, "source_frame_id": "frame-0", "source_hashes": source_hashes}
    original = lp._write_live_index
    seen_at_pointer = {}

    def check_pointer(*args, **kwargs):
        assert not list((run / "branches/objects/acks").glob("*.json"))
        assert not list((run / "branches/rooms/acks").glob("*.json"))
        assert graph_sha == hashlib.sha256((viewer / "graph.json").read_bytes()).hexdigest()
        assert overlay_sha == hashlib.sha256((viewer / "overlay.json").read_bytes()).hexdigest()
        original(*args, **kwargs)
        seen_at_pointer["index"] = json.loads((viewer / "live_index.json").read_text())

    monkeypatch.setattr(lp, "_write_live_index", check_pointer)
    lp._publish_joined_semantic_commit(run, viewer, {"schema": "fixture-index"}, frames,
                                      event, object_event, room_event)
    assert seen_at_pointer["index"]["frames"][0]["room_belief_overlay_sha256"] == overlay_sha
    assert json.loads((viewer / "live_head.json").read_text())["revision_count"] == 1
    assert (run / "joined/events.jsonl").is_file()
    for branch in ("objects", "rooms"):
        ack = json.loads((run / f"branches/{branch}/acks/000001.json").read_text())
        assert ack["durable"] is True and ack["joined_state_sha256"] == joined_sha


def test_mismatched_overlay_is_rejected_before_index_or_ack(tmp_path: Path):
    run = tmp_path / "run"
    viewer = run / "viewer"
    viewer.mkdir(parents=True)
    (run / "branches/objects/acks").mkdir(parents=True)
    (run / "branches/rooms/acks").mkdir(parents=True)
    graph_sha = write_json(viewer / "graph.json", {"graph_revision_id": "unified-000001", "scene_id": "scene",
        "sequence_id": "sequence", "object_revision": 4, "room_revision": 2})
    overlay_sha = write_json(viewer / "overlay.json", {
        "schema": "t4-room-belief-semantic-overlay/v1", "status": "complete",
        "graph_revision_id": "wrong-graph", "graph_sha256": graph_sha,
        "frame_id": "frame-0", "frame_index": 0, "unified_revision": 1,
        "scene_id": "scene", "sequence_id": "sequence", "object_revision": 4, "room_revision": 2,
        "source_hashes": {"packet": "a" * 64},
    })
    joined_sha = write_json(run / "joined/revisions/rev-000001.json", {"joined_revision": 1})
    frames = [{"revision": 1, "frame_index": 0, "frame_id": "frame-0", "timestamp_s": 1.0,
        "unified_url": "graph.json", "unified_sha256": graph_sha,
        "room_belief_overlay_url": "overlay.json", "room_belief_overlay_sha256": overlay_sha,
        "joined_state_url": "../joined/revisions/rev-000001.json", "joined_state_sha256": joined_sha,
        "object_source_revision": 4, "room_source_revision": 2}]
    source_hashes = {"packet": "a" * 64}
    object_event = {"event_seq": 1, "event_sha256": "b" * 64, "source_frame_id": "frame-0", "source_hashes": source_hashes}
    room_event = {"event_seq": 1, "event_sha256": "c" * 64, "source_frame_id": "frame-0", "source_hashes": source_hashes}
    with pytest.raises(ValueError, match="does not bind"):
        lp._publish_joined_semantic_commit(run, viewer, {}, frames,
            {"joined_revision": 1, "source_frame_id": "frame-0", "source_frame_index": 0}, object_event, room_event)
    assert not (viewer / "live_index.json").exists()
    assert not list((run / "branches/objects/acks").glob("*.json"))


def test_camera_runner_uses_existing_session_hook_and_never_publishes_join_early():
    source = (Path(__file__).resolve().parents[1] / "scripts/run_camera_live_pilot.py").read_text()
    assert "RoomBeliefCommitSession.open(" in source
    assert "update_after_unified_commit(envelope)" in source
    assert "publish_index=False,publish_event=False,acknowledge=False" in source
    assert "_publish_joined_semantic_commit(root,viewer,base,joined,joined_event,oe1,re1)" in source
    assert "branch,source_event" not in source
