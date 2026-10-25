from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from standalone_sg.objects.same_frame_duplicates_v2 import canonicalize_same_frame_duplicates_v2
from standalone_sg.objects.u3_experimental import U3Observation


CFG = json.loads((ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json").read_text())["candidate"]


def obs(did, *, frame="f0", cls="chair", center=(0.0, 0.0, 0.0), ext=(1.0, 1.0, 1.0)):
    return U3Observation(did, frame, 0.0, center, ext, "floor_0", cls, None, did)


def supports(rows, *, points=500, valid=100, score=0.5):
    return {o.detection_id: {"cluster_points": points, "valid_depth_pixels": valid,
                             "detector_score": score} for o in rows}


def run(rows, masks, support=None):
    if support is None:
        support = {o.detection_id: {"cluster_points": 500, "valid_depth_pixels": int(masks[o.detection_id].sum()),
                                    "detector_score": 0.5} for o in rows}
    return canonicalize_same_frame_duplicates_v2(rows, masks, support, CFG)


def containment_masks(extra_pixels):
    small = np.zeros((100, 100), dtype=bool)
    small[:60, :80] = True  # 4800 pixels
    large = small.copy()
    # Add deterministic non-overlapping pixels in the lower rows.
    large[60:, :extra_pixels // 40] = True
    remainder = extra_pixels % 40
    if remainder:
        large[60, extra_pixels // 40:extra_pixels // 40 + remainder] = True
    assert int(large.sum()) == 4800 + extra_pixels
    return small, large


def test_exact_duplicate_masks_still_canonicalize():
    rows = [obs("a"), obs("b")]
    mask = np.ones((10, 10), dtype=bool)
    kept, aliases, ledger = run(rows, {"a": mask, "b": mask})
    assert len(kept) == 1 and len(aliases) == 1
    assert aliases[0]["counts_as_view"] is False
    assert ledger["pair_evidence"][0]["v2_guard_disposition"] == "canonicalize"


def test_complete_mask_with_modest_containment_expansion_can_canonicalize():
    small, large = containment_masks(1500)  # area ratio 1.3125
    a, b = obs("small"), obs("large")
    kept, aliases, ledger = run([a, b], {"small": small, "large": large})
    assert len(kept) == 1 and len(aliases) == 1
    assert aliases[0]["duplicate_of"] == "large"
    assert ledger["pair_evidence"][0]["containment_branch"] is True
    assert ledger["pair_evidence"][0]["v2_guard_disposition"] == "canonicalize"


def test_containment_with_large_extra_region_is_ambiguous_not_suppressed():
    small, large = containment_masks(2600)  # area ratio 1.5417
    a, b = obs("small"), obs("large")
    kept, aliases, ledger = run([a, b], {"small": small, "large": large})
    assert {o.detection_id for o in kept} == {"small", "large"}
    assert aliases == []
    row = ledger["pair_evidence"][0]
    assert row["v2_guard_disposition"] == "ambiguous_keep_both_no_alias"
    assert row["v2_guard_reason"] == "containment_area_expansion_over_limit"
    assert ledger["ambiguous_observation_ids"] == ["large", "small"]


def test_large_containment_with_clear_3d_mismatch_is_kept_separate_not_ambiguous():
    small, large = containment_masks(2600)  # area ratio 1.5417
    a = obs("small", center=(0.0, 0.0, 0.0))
    b = obs("large", center=(2.0, 0.0, 0.0))
    kept, aliases, ledger = run([a, b], {"small": small, "large": large})
    assert {o.detection_id for o in kept} == {"small", "large"}
    assert aliases == []
    row = ledger["pair_evidence"][0]
    assert row["v2_guard_disposition"] == "keep_separate"
    assert row["v2_guard_reason"] == "clear_3d_geometry_mismatch"
    assert row["ambiguous_duplicate_decision"] is False


def test_valid_depth_in_extra_region_alone_does_not_make_pair_different():
    small, large = containment_masks(1500)
    a, b = obs("small"), obs("large")
    support = supports([a, b], points=900, valid=int(large.sum()))
    # Both masks have valid depth and the extra region is supported, but the
    # locked modest-expansion case remains eligible when 3D gates agree.
    support["large"]["extra_region_valid_depth_pixels"] = 1500
    support["large"]["extra_region_on_same_surface"] = True
    kept, aliases, _ = run([a, b], {"small": small, "large": large}, support)
    assert len(kept) == 1 and len(aliases) == 1


def test_nearby_masks_with_separated_geometry_are_not_suppressed():
    rows = [obs("a"), obs("b", center=(0.5, 0.0, 0.0))]
    mask = np.ones((10, 10), dtype=bool)
    kept, aliases, ledger = run(rows, {"a": mask, "b": mask})
    assert len(kept) == 2 and aliases == []
    assert ledger["pair_evidence"][0]["v2_guard_disposition"] == "keep_separate"


def test_missing_depth_or_point_support_is_ambiguous():
    small, large = containment_masks(1000)
    a, b = obs("small"), obs("large")
    kept, aliases, ledger = run([a, b], {"small": small, "large": large},
                                {"small": {"cluster_points": 500, "valid_depth_pixels": 4800}})
    assert len(kept) == 2 and aliases == []
    assert ledger["pair_evidence"][0]["v2_guard_disposition"] == "ambiguous_keep_both_no_alias"
    assert ledger["pair_evidence"][0]["v2_guard_reason"] == "missing_or_insufficient_depth_point_support"

    # The masks overlap but no usable P3 bounds are available.
    a, b = obs("small", ext=None), obs("large")
    complete_support = {"small": {"cluster_points": 500, "valid_depth_pixels": int(small.sum())},
                        "large": {"cluster_points": 500, "valid_depth_pixels": int(large.sum())}}
    kept, aliases, ledger = run([a, b], {"small": small, "large": large}, complete_support)
    assert len(kept) == 2 and aliases == []
    assert ledger["pair_evidence"][0]["v2_guard_disposition"] == "ambiguous_keep_both_no_alias"
    assert ledger["pair_evidence"][0]["v2_guard_reason"] == "missing_or_insufficient_3d_geometry"


def test_detection_order_is_deterministic_for_ambiguous_pairs():
    small, large = containment_masks(2600)
    a, b = obs("small"), obs("large")
    outputs = []
    for order in itertools.permutations([a, b]):
        kept, aliases, ledger = run(list(order), {"small": small, "large": large})
        outputs.append((sorted(o.detection_id for o in kept), aliases,
                        ledger["ambiguous_observation_ids"]))
    assert outputs[0] == outputs[1]


def test_same_frame_only_and_no_class_or_future_identity_gate():
    same_frame_a = obs("a", frame="f0", cls="chair")
    same_frame_b = obs("b", frame="f0", cls="table")
    future = obs("future", frame="f1", cls="table")
    mask = np.ones((10, 10), dtype=bool)
    kept, aliases, _ = run([same_frame_a, same_frame_b, future],
                           {"a": mask, "b": mask, "future": mask})
    assert {o.detection_id for o in kept} == {"a", "future"}
    assert [(x["detection_id"], x["duplicate_of"]) for x in aliases] == [("b", "a")]
