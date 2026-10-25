import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_u6_diagnostic_review as review  # noqa: E402
import evaluate_hm3d_object_gt as gt_eval  # noqa: E402


def test_full_observation_id_prevents_same_detection_suffix_panel_collision():
    left = "hm3d-00808-u6q-v1-0000:det-0001"
    right = "hm3d-00808-u6q-v1-0004:det-0001"
    assert review.panel_filename(left, "rgb") != review.panel_filename(right, "rgb")
    assert review.panel_filename(left, "rgb") != review.panel_filename(left, "gt-mask")


def test_gt_panel_status_keeps_official_match_separate_from_best_overlap_candidate():
    official = {"gt_matched": True, "gt_semantic_id": 584, "gt_category": "table", "mask_iou": 0.7245}
    candidate = {"gt_semantic_id": 584, "category": "table", "mask_iou": 0.6673}
    assert review.gt_panel_decision(official, candidate)["panel_kind"] == "official_one_to_one_match"
    result = review.gt_panel_decision({"gt_matched": False}, candidate)
    assert result["panel_kind"] == "best_overlap_candidate_not_official_match"
    assert result["gt_semantic_id"] == 584
    assert review.gt_panel_decision({"gt_matched": False}, {"mask_iou": 0.0})["panel_kind"] == "unmatched_no_positive_overlap_candidate"


def test_hungarian_match_ledger_is_one_to_one_even_when_two_masks_overlap_same_gt():
    # Two detections exceed IoU threshold for one GT; the official ledger can
    # match only one detection to that GT. The other remains unmatched.
    matches = gt_eval._assign(np.array([[0.724567], [0.667307]]), 0.50)
    assert matches == [(0, 0, 0.724567)]


def test_saved_png_panel_pixels_match_expected_source_composition(tmp_path):
    pixels = np.zeros((4, 5, 3), dtype=np.uint8)
    pixels[1:3, 2:4] = [20, 235, 70]
    record = review.save_verified_panel(tmp_path, "f0:det-0001", "prediction-mask", pixels)
    reopened = np.asarray(Image.open(record["output_panel"]).convert("RGB"))
    assert record["pixel_exact_after_reload"] is True
    assert np.array_equal(reopened, pixels)
