from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_causal_timeline_viewer import _copy_room_belief_prefixes


def dump(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def fixture(tmp_path: Path):
    timeline = tmp_path / "timeline"
    output = tmp_path / "viewer"
    timeline.mkdir()
    output.mkdir()
    dump(timeline / "manifest.json", {"schema": "conceptgraphs-causal-object-timeline/v1",
        "scene_id": "fixture-scene", "sequence_id": "fixture-route"})
    dump(timeline / "room-rev-7.json", {"snapshot": {"revision": 7}})
    dump(output / "room-rev-7.json", {"snapshot": {"revision": 7}})
    frames = [{"frame_index": 0, "frame_id": "fixture-frame-0", "revision": 3,
               "timestamp_s": 0.25, "room_url": "room-rev-7.json"}]
    provenance = {"object_prefix_sha256": "a" * 64}
    variants = ["mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral"]
    declaration_id, declaration_hash = "fixture-assumption", "b" * 64
    a_state = {"schema": "causal-hybrid-room-belief-revision/v1", "frame_index": 0,
        "frame_id": frames[0]["frame_id"], "revision": 3, "timestamp_s": 0.25,
        "future_observations_used": False, "scope_status": "unavailable", "room_contains_edges": [],
        "room_lifecycle": {"room_revision": 7}}
    b_state = {**a_state, "scope_status": "assumed", "declaration_id": declaration_id,
        "declaration_sha256": declaration_hash, "room_contains_edges": [{"relation": "room_contains"}]}
    a_root, b_root = tmp_path / "A", tmp_path / "B"
    dump(a_root / "revisions/rev-000003.json", a_state)
    dump(b_root / "revisions/rev-000003.json", b_state)
    common = {"schema": "causal-hybrid-room-belief-timeline/v1", "status": "complete",
        "run_id": "fixture-run", "scene_id": "fixture-scene", "sequence_id": "fixture-route",
        "frame_count": 1, "variants": variants, "input_provenance": provenance,
        "revisions": [{"frame_index": 0, "frame_id": frames[0]["frame_id"], "revision": 3,
                        "state": "revisions/rev-000003.json"}]}
    dump(a_root / "manifest.json", {**common, "scope_status": "unavailable"})
    dump(b_root / "manifest.json", {**common, "scope_status": "assumed",
        "scope_declaration_id": declaration_id, "scope_declaration_sha256": declaration_hash,
        "scope_declaration": {"path": ""}})
    return timeline, output, frames, a_root, b_root


def test_t3_prefix_join_uses_frame_and_room_revision_and_exports_both_modes(tmp_path: Path):
    timeline, output, frames, a_root, b_root = fixture(tmp_path)
    b_manifest_path = b_root / "manifest.json"
    b_manifest = json.loads(b_manifest_path.read_text())
    b_manifest["input_provenance"]["assumed_declaration_hash"] = "c" * 64
    dump(b_manifest_path, b_manifest)
    index = _copy_room_belief_prefixes(timeline, output, frames, a_root, b_root)
    assert index["schema"] == "t3-room-belief-viewer-index/v1"
    assert index["variants"] == ["mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral"]
    revision = index["revisions"][0]
    assert (revision["frame_index"], revision["frame_id"], revision["object_revision"], revision["room_revision"]) == (0, "fixture-frame-0", 3, 7)
    assert revision["verified_only"]["scope_status"] == "unavailable"
    assert revision["assumed_single_level_demo"]["scope_status"] == "assumed"
    assert (output / revision["verified_only"]["url"]).is_file()
    assert (output / revision["assumed_single_level_demo"]["url"]).is_file()
    assert (output / "room_belief_index.json").is_file()


def test_t3_viewer_rejects_conflicting_shared_provenance_but_allows_b_extensions(tmp_path: Path):
    timeline, output, frames, a_root, b_root = fixture(tmp_path)
    b_manifest_path = b_root / "manifest.json"
    b_manifest = json.loads(b_manifest_path.read_text())
    b_manifest["input_provenance"]["object_prefix_sha256"] = "d" * 64
    dump(b_manifest_path, b_manifest)
    with pytest.raises(ValueError, match="common input provenance differs"):
        _copy_room_belief_prefixes(timeline, output, frames, a_root, b_root)


def test_t3_viewer_rejects_room_revision_mismatch(tmp_path: Path):
    timeline, output, frames, a_root, b_root = fixture(tmp_path)
    b_state_path = b_root / "revisions/rev-000003.json"
    state = json.loads(b_state_path.read_text())
    state["room_lifecycle"]["room_revision"] = 8
    dump(b_state_path, state)
    with pytest.raises(ValueError, match="room revision does not match"):
        _copy_room_belief_prefixes(timeline, output, frames, a_root, b_root)


def test_t3_viewer_supports_verified_only_without_fabricated_assumed_state(tmp_path: Path):
    timeline, output, frames, a_root, _b_root = fixture(tmp_path)
    index = _copy_room_belief_prefixes(timeline, output, frames, a_root)
    assert index["scope_modes"] == ["verified_only"]
    assert index["assumed_declaration"] is None
    assert "assumed_single_level_demo" not in index["revisions"][0]
    assert (output / index["revisions"][0]["verified_only"]["url"]).is_file()
    assert not (output / "room_belief/assumed_single_level_demo").exists()


def test_t3_viewer_aligns_noncontiguous_object_room_and_unified_revisions(tmp_path: Path):
    timeline, output, frames, a_root, _b_root = fixture(tmp_path)
    frames[0]["revision"] = 1  # unified graph revision ordinal
    frames[0]["object_source_revision"] = 27
    frames[0]["room_source_revision"] = 7
    a_state_path = a_root / "revisions/rev-000003.json"
    a_state = json.loads(a_state_path.read_text())
    a_state["revision"] = 27
    dump(a_state_path, a_state)
    a_manifest_path = a_root / "manifest.json"
    a_manifest = json.loads(a_manifest_path.read_text())
    a_manifest["revisions"][0]["revision"] = 27
    dump(a_manifest_path, a_manifest)

    index = _copy_room_belief_prefixes(timeline, output, frames, a_root)
    row = index["revisions"][0]
    assert (row["object_revision"], row["room_revision"]) == (27, 7)
