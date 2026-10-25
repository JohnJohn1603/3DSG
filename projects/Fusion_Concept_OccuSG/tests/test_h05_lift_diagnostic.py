import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/human_review/hm3d00802_main29_min3_v1_review_v1/postreview_v2/h05_lift_diagnostic"


def test_selected_h05_observations_have_aligned_cached_inputs():
    data = json.loads((OUT / "h05_lift_provenance.json").read_text())
    assert data["selected_observation_ids"] == [
        "hm3d-00802-main-0011:det-0001",
        "hm3d-00802-main-0016:det-0002",
        "hm3d-00802-main-0019:det-0001",
    ]
    assert len(data["observations"]) == 3
    for row in data["observations"]:
        assert row["rgb_shape"][:2] == row["depth_shape"] == row["mask_shape"]
        assert row["mask_pixels"] == row["valid_depth_pixels_in_mask"]
        assert row["room6_support_fraction"] == 0.0
        assert Path(row["diagnostic_files"]["projection_overlay"]).is_file()


def test_reconstructed_and_saved_bounds_are_close_without_claiming_exact_parity():
    data = json.loads((OUT / "h05_lift_provenance.json").read_text())
    final = data["final_cloud_bounds_minmax"]
    for row in data["observations"]:
        for a, b in zip(row["world_bounds_minmax"], final):
            assert max(abs(x - y) for x, y in zip(a, b)) < 0.10
    assert any("exact cloud parity" in x for x in data["limitations"])


def test_h05_manifest_is_present():
    m = json.loads((OUT / "h05_lift_manifest.json").read_text())
    assert m["status"] == "complete"
    assert m["final_hypothesis_id"] == "hypothesis_d579ba788876c64a"
