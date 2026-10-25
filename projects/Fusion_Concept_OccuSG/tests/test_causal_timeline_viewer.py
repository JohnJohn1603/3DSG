from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_causal_timeline_viewer import build
from integration_v2.unified_incremental_timeline import build_unified_timeline


ROOT = Path(__file__).resolve().parents[1]
TIMELINE = ROOT / "artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline"


def test_viewer_build_indexes_all_prefix_revisions_and_separates_stages(tmp_path: Path):
    output = tmp_path / "viewer"
    index = build(TIMELINE, output, max_points=7)
    assert len(index["frames"]) == 29
    assert [f["revision"] for f in index["frames"]] == list(range(1, 30))
    assert [f["timestamp_s"] for f in index["frames"][:3]] == [0.0, 0.1, 0.2]
    assert all(f["object_source_revision"] == f["revision"] for f in index["frames"])
    assert all(isinstance(f["room_source_revision"], int) for f in index["frames"])
    for frame in index["frames"]:
        snap = json.loads((TIMELINE / frame["snapshot"]).read_text())
        room_doc = json.loads((output / frame["room_url"]).read_text())
        room_state = room_doc.get("snapshot", room_doc)
        assert frame["object_source_revision"] == snap["revision"]
        assert frame["room_source_revision"] == room_state["revision"]
    assert len(list((output / "geometry").glob("rev-*.json"))) == 29
    manifest = json.loads((output / "viewer_manifest.json").read_text())
    assert manifest["geometry_source_is_read_only"] is True
    assert manifest["cache_policy"]["max_revision_entries"] == 6
    assert index["stages"] == {
        "online_hypotheses": "available",
        "prefix_admission": "unavailable",
        "final_postprocess": "separate_reference_artifact_not_loaded",
    }
    html = (output / "index.html").read_text()
    assert "atomicRevisionGate.request" in html
    assert "MAX_CACHE=6" in html and "cachePut" in html
    assert "no room_contains edges" in html
    assert "room semantics remain unavailable" in html
    assert 'id="roomList"' in html
    assert "function renderRoomList" in html
    assert 'id="scopeModeSelect"' in html
    assert "not probability of accuracy" in html
    assert "one category contribution" in html
    assert "object:${ids.get(String(e.target_id))}" in html
    assert 'id="roomBeliefSummary"' in html
    assert "rooms currently with committed labels at this prefix" in html
    assert "label commit/relabel transitions at this revision" in html
    assert "getJSON(t3Entry[ACTIVE_SCOPE_MODE].url)" in html


def test_viewer_rejects_missing_timeline_assets(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        build(tmp_path / "does-not-exist", tmp_path / "viewer")


def test_viewer_derived_geometry_has_source_hash_and_bounded_points(tmp_path: Path):
    output = tmp_path / "viewer"
    build(TIMELINE, output, max_points=5)
    first = json.loads((output / "geometry/rev-000001.json").read_text())
    assert first["max_points"] == 5
    assert first["objects"]
    for obj in first["objects"]:
        assert len(obj["points"]) <= 5
        assert len(obj["source_sha256"]) == 64


def test_viewer_does_not_overwrite_existing_derived_output(tmp_path: Path):
    output = tmp_path / "viewer"
    output.mkdir()
    (output / "sentinel").write_text("keep")
    with pytest.raises(FileExistsError):
        build(TIMELINE, output)


def test_viewer_uses_unified_revision_as_graph_state_and_displays_unavailable_reason(tmp_path: Path):
    causal = ROOT / "artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a1_v6"
    unified = tmp_path / "unified"
    build_unified_timeline(TIMELINE, causal, unified)
    output = tmp_path / "viewer"
    index = build(TIMELINE, output, max_points=3, unified_timeline=unified)
    assert len(index["frames"]) == 29
    assert all(frame["unified_url"] for frame in index["frames"])
    graph = json.loads((output / index["frames"][0]["unified_url"]).read_text())
    assert graph["membership_availability"]["reason"] == "unavailable_elevation_scope"
    assert not [edge for edge in graph["edges"] if edge["relation"] == "room_contains"]
    html = (output / "index.html").read_text()
    assert "membershipReason(graph)" in html
    assert "no room_contains edges unless present in this revision" in html
    assert "atomicRevisionGate.request" in html
    assert 'id="roomList"' in html
    assert "function renderRoomList" in html
