from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from scripts.run_causal_hybrid_room_belief import (
    _load_effective_semantic_config, _load_scope_contract, _input_set_hashes,
    _validate_scope_contract, canonical, membership_for, validate_assumed_scope_declaration,
)
from scripts.summarize_t3_room_belief import failed_gates
from standalone_sg.contracts.regions import Polygon2D

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2"
TIMELINE = RUN / "object_timeline/timeline"
A_ZIP = ROOT / "artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip"
DECLARATION = ROOT / "artifacts/room_belief/t3_declarations/hm3d00802_main29_640_assumed_single_level_demo_v1.json"
ASSUMED_OUTPUT = ROOT / "artifacts/room_belief/t3_hm3d00802_main29_640_assumed_v2"


def _source_inputs():
    manifest = json.loads((TIMELINE / "manifest.json").read_text())
    frames = json.loads((TIMELINE / manifest["snapshot_index"]).read_text())["frames"]
    return manifest, frames


def test_approved_assumption_hashes_and_run_frame_scope_validate():
    manifest, frames = _source_inputs()
    declaration = json.loads(DECLARATION.read_text())
    provenance = _load_effective_semantic_config(TIMELINE, manifest)
    result = validate_assumed_scope_declaration(declaration, DECLARATION, TIMELINE,
                                                manifest, frames, A_ZIP, provenance)
    assert result["status"] == "assumed"
    assert result["frame_indices_inclusive"] == [0, 28]
    assert result["snapshot_set_hashes"]["object_snapshot_set_sha256"] == declaration["object_prefix"]["snapshot_set_sha256"]
    assert result["snapshot_set_hashes"]["room_revision_set_sha256"] == declaration["room_prefix"]["revision_set_sha256"]


def test_assumption_rejects_hash_change_and_other_scope():
    manifest, frames = _source_inputs()
    declaration = json.loads(DECLARATION.read_text())
    provenance = _load_effective_semantic_config(TIMELINE, manifest)
    altered = copy.deepcopy(declaration)
    altered["input_provenance"]["object_cloud_set_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="object_cloud_set_sha256"):
        validate_assumed_scope_declaration(altered, DECLARATION, TIMELINE,
                                           manifest, frames, A_ZIP, provenance)
    altered = copy.deepcopy(declaration)
    altered["sequence_id"] = "another-route"
    with pytest.raises(ValueError, match="sequence_id mismatch"):
        validate_assumed_scope_declaration(altered, DECLARATION, TIMELINE,
                                           manifest, frames, A_ZIP, provenance)


def test_canonicalization_uses_effective_detector_profile_aliases():
    manifest, _ = _source_inputs()
    provenance = _load_effective_semantic_config(TIMELINE, manifest)
    aliases = provenance["canonical_aliases"]
    assert canonical("couch", aliases) == "sofa"
    assert canonical("dining table", aliases) == "dining_table"
    assert canonical("potted plant", aliases) == "potted_plant"


def test_assumed_membership_is_computed_but_never_marked_verified(tmp_path: Path):
    cloud_path = tmp_path / "cloud.npz"
    np.savez(cloud_path, xyz=np.asarray([[0.2, 0.2, 0.0], [0.3, 0.3, 0.0]], dtype=np.float32))
    scope = {"status": "assumed", "reason": "approved_assumed_single_level_demo_not_verified",
             "declaration_id": "decl-test", "declaration_sha256": "a" * 64,
             "assumption_not_verified": True}
    obj = {"observation_ids": ["scene-0000:det-0000"], "cloud_npz": "cloud.npz",
           "hypothesis_id": "hyp-test"}
    polys = {"room:1": Polygon2D(((0, 0), (1, 0), (1, 1), (0, 1)))}
    result = membership_for(obj, polys, scope, tmp_path, None, 1, 0.025, [0.0, 0.0, 0.0])
    assert result["status"] == "assigned"
    assert result["scope_status"] == "assumed"
    assert result["declaration_id"] == "decl-test"
    assert result["assumption_not_verified"] is True


def test_no_categories_reports_insufficient_support_before_score_gates():
    policy = {"minimum_supported_categories": 2, "minimum_top_belief": 0.6,
              "minimum_margin": 0.2}
    # The prior alone has a valid softmax, but cannot be treated as evidence.
    state = {"scope_status": "assumed", "category_counts": {},
             "candidates": [["living_room", 0.4], ["kitchen", 0.2]],
             "pending_label": None}
    assert failed_gates(state, policy) == ["insufficient_support"]


def test_sparse_assumed_scope_is_effective_only_on_exact_prefix_frames():
    declaration = {
        "schema": "assumed-spatial-scope-declaration/v2",
        "mode": "assumed_single_level_demo", "scope_status": "assumed",
        "scene_id": "scene", "sequence_id": "seq",
        "source_frame_indices_exact": [0, 26, 33],
        "effective_prefixes": [
            {"frame_index": i, "frame_id": f"f{i}", "object_revision": i + 1}
            for i in (0, 26, 33)
        ],
        "_assumed_validation": {"status": "assumed"},
    }
    manifest = {"scene_id": "scene", "sequence_id": "seq"}
    for frame in (0, 26, 33):
        item = {"frame_index": frame, "frame_id": f"f{frame}", "revision": frame + 1,
                "object_revision": frame + 1}
        result = _validate_scope_contract(declaration, ROOT, manifest, [item], frame)
        assert result["status"] == "assumed"
    missing = {"frame_index": 27, "frame_id": "f27", "revision": 28, "object_revision": 28}
    result = _validate_scope_contract(declaration, ROOT, manifest, [missing], 27)
    assert result == {"status": "invalid", "reason": "assumed_scope_not_effective_for_exact_prefix"}
    wrong_id = {"frame_index": 26, "frame_id": "other", "revision": 27, "object_revision": 27}
    result = _validate_scope_contract(declaration, ROOT, manifest, [wrong_id], 26)
    assert result == {"status": "invalid", "reason": "assumed_scope_prefix_frame_id_mismatch"}


def test_completed_assumed_outputs_keep_declaration_provenance_per_prefix():
    manifest = json.loads((ASSUMED_OUTPUT / "manifest.json").read_text())
    declaration_id = manifest["scope_declaration_id"]
    declaration_hash = manifest["scope_declaration_sha256"]
    for item in manifest["revisions"]:
        row = json.loads((ASSUMED_OUTPUT / item["state"]).read_text())
        assert row["scope_status"] == "assumed"
        assert row["declaration_id"] == declaration_id
        assert row["declaration_sha256"] == declaration_hash
        # Membership edges inherit the explicitly tagged revision context;
        # individual object membership and semantic records carry it directly.
        for obj in row["objects"]:
            for channel in ("membership", "semantic"):
                assert obj[channel]["scope_status"] == "assumed"
                assert obj[channel]["declaration_id"] == declaration_id
                assert obj[channel]["declaration_sha256"] == declaration_hash
        for variant in manifest["variants"]:
            for room in row["rooms"][variant].values():
                assert room["scope_status"] == "assumed"
                assert room["declaration_id"] == declaration_id
                assert room["declaration_sha256"] == declaration_hash
