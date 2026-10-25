from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from standalone_sg.objects.same_frame_duplicates import canonicalize_same_frame_duplicates
from standalone_sg.objects.u3_experimental import U3Observation
from run_u3_same_frame_duplicate_experiment import compare_decisions


CFG = json.loads((ROOT / "configs/association/u3_same_frame_duplicate_candidate_v1.json").read_text())["candidate"]


def obs(did, *, frame="f0", cls="chair", center=(0.0, 0.0, 0.0), ext=(1.0, 1.0, 1.0)):
    return U3Observation(did, frame, 0.0, center, ext, "floor_0", cls, None, did)


def support(obs_rows, *, points=500, valid=100, score=0.5):
    return {o.detection_id: {"cluster_points": points, "valid_depth_pixels": valid,
                             "detector_score": score} for o in obs_rows}


def pair(mask_a, mask_b, a=None, b=None, support_rows=None):
    a = a or obs("a")
    b = b or obs("b")
    masks = {a.detection_id: mask_a, b.detection_id: mask_b}
    kept, aliases, ledger = canonicalize_same_frame_duplicates(
        [a, b], masks, support_rows or support([a, b]), CFG)
    return kept, aliases, ledger


def test_near_duplicate_masks_same_physical_observation_suppressed_once():
    a = np.zeros((10, 10), dtype=bool); a[:9, :] = True
    b = np.zeros((10, 10), dtype=bool); b[:, :] = True
    kept, aliases, ledger = pair(a, b)
    assert len(kept) == 1
    assert aliases[0]["duplicate_of"] == "b"  # full mask has greater deterministic support tie-break via area
    assert aliases[0]["counts_as_view"] is False
    assert aliases[0]["semantic_vote_count"] == 0
    assert ledger["pair_evidence"][0]["eligible_duplicate"] is True


def test_same_object_different_raw_classes_can_be_canonicalized():
    mask = np.ones((8, 8), dtype=bool)
    a, b = obs("a", cls="table"), obs("b", cls="chair")
    kept, aliases, ledger = pair(mask, mask, a, b)
    assert len(kept) == 1 and len(aliases) == 1
    assert ledger["pair_evidence"][0]["raw_class_gate_used"] is False


def test_nearby_distinct_objects_do_not_suppress_without_mask_overlap():
    a, b = obs("a"), obs("b", center=(0.05, 0, 0))
    ma = np.zeros((10, 10), dtype=bool); ma[:5, :5] = True
    mb = np.zeros((10, 10), dtype=bool); mb[5:, 5:] = True
    kept, aliases, ledger = pair(ma, mb, a, b)
    assert len(kept) == 2 and aliases == []
    assert "insufficient_2d_mask_overlap" in ledger["pair_evidence"][0]["gate_failures"]


def test_image_overlap_but_depth_separated_objects_are_kept():
    mask = np.ones((10, 10), dtype=bool)
    a, b = obs("a"), obs("b", center=(0.5, 0, 0))
    kept, aliases, ledger = pair(mask, mask, a, b)
    assert len(kept) == 2 and aliases == []
    assert "center_distance" in ledger["pair_evidence"][0]["gate_failures"]
    assert "3d_aabb_iou" in ledger["pair_evidence"][0]["gate_failures"]


def test_large_mask_containing_small_object_is_not_enough():
    large = np.ones((10, 10), dtype=bool)
    small = np.zeros((10, 10), dtype=bool); small[3:7, 3:7] = True
    a, b = obs("a", ext=(2.0, 2.0, 1.0)), obs("b", ext=(0.3, 0.3, 0.2))
    kept, aliases, ledger = pair(large, small, a, b)
    assert len(kept) == 2 and aliases == []
    assert ledger["pair_evidence"][0]["mask_containment_min_area"] == 1.0
    assert "3d_aabb_iou" in ledger["pair_evidence"][0]["gate_failures"]
    assert "extent_consistency" in ledger["pair_evidence"][0]["gate_failures"]


def test_partial_view_or_weak_depth_support_stays_unmerged():
    ma = np.zeros((10, 10), dtype=bool); ma[:6, :6] = True
    mb = np.zeros((10, 10), dtype=bool); mb[4:, 4:] = True
    rows = [obs("a"), obs("b")]
    low = support(rows, points=50, valid=20)
    kept, aliases, ledger = pair(ma, mb, *rows, support_rows=low)
    assert len(kept) == 2 and aliases == []
    failures = ledger["pair_evidence"][0]["gate_failures"]
    assert "insufficient_valid_depth_or_cluster_support" in failures


def test_detection_order_does_not_change_canonical_or_alias():
    a, b, c = obs("a"), obs("b"), obs("c")
    full = np.ones((8, 8), dtype=bool)
    masks = {"a": full, "b": full, "c": full}
    supports = {"a": {"cluster_points": 100, "valid_depth_pixels": 100, "detector_score": .9},
                "b": {"cluster_points": 200, "valid_depth_pixels": 100, "detector_score": .5},
                "c": {"cluster_points": 200, "valid_depth_pixels": 100, "detector_score": .5}}
    outputs = []
    for order in itertools.permutations([a, b, c]):
        kept, aliases, _ = canonicalize_same_frame_duplicates(list(order), masks, supports, CFG)
        outputs.append(([x.detection_id for x in kept], [(r["detection_id"], r["duplicate_of"]) for r in aliases]))
    assert len(set(json.dumps(x) for x in outputs)) == 1
    assert outputs[0] == (["b"], [("c", "b"), ("a", "b")])


def test_observation_matching_multiple_canonicals_is_kept_ambiguous():
    left, right, bridge = obs("a"), obs("b"), obs("c")
    ma = np.zeros((10, 10), dtype=bool); ma[:, :5] = True
    mb = np.zeros((10, 10), dtype=bool); mb[:, 5:] = True
    mc = np.ones((10, 10), dtype=bool)
    masks = {"a": ma, "b": mb, "c": mc}
    supports = {"a": {"cluster_points": 600, "valid_depth_pixels": 100, "detector_score": .5},
                "b": {"cluster_points": 600, "valid_depth_pixels": 100, "detector_score": .5},
                "c": {"cluster_points": 500, "valid_depth_pixels": 100, "detector_score": .5}}
    kept, aliases, ledger = canonicalize_same_frame_duplicates([left, right, bridge], masks, supports, CFG)
    assert {x.detection_id for x in kept} == {"a", "b", "c"}
    assert aliases == []
    decisions = {x["detection_id"]: x for x in ledger["observation_decisions"]}
    assert decisions["c"]["decision"] == "kept_ambiguous"
    assert decisions["c"]["candidate_canonicals"] == ["a", "b"]


def test_pairwise_delta_ignores_track_renumbering_but_reports_defer_transitions():
    current = [
        {"detection_id": "a", "track_id": "old-1", "decision": "matched", "suppressed_from_association": False},
        {"detection_id": "b", "track_id": "old-2", "decision": "matched", "suppressed_from_association": False},
        {"detection_id": "c", "track_id": None, "decision": "defer", "suppressed_from_association": False},
    ]
    candidate = [
        {"detection_id": "a", "track_id": "new-1", "decision": "matched", "suppressed_from_association": False},
        {"detection_id": "b", "track_id": "new-2", "decision": "matched", "suppressed_from_association": False},
        {"detection_id": "c", "track_id": "new-2", "decision": "matched", "suppressed_from_association": False},
    ]
    obs_delta, pair_delta, summary = compare_decisions(current, candidate)
    assert len(obs_delta) == 1
    assert summary["relation_transitions"]["deferred_or_unassigned_to_different_tracks"] == 1
    assert summary["relation_transitions"]["deferred_or_unassigned_to_same_track"] == 1
    assert len(pair_delta) == 2
