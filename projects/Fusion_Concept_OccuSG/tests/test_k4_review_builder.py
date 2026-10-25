import numpy as np
from PIL import Image

from scripts.build_k4_review_v2 import _render_observation, _safe_id


def test_review_asset_names_hash_full_observation_id():
    assert _safe_id("scene-frame-0:det-0001") != _safe_id("scene-frame-1:det-0001")


def test_review_panels_match_source_rgb_mask_and_unique_observation_paths(tmp_path):
    input_root = tmp_path / "input"
    output = tmp_path / "review"
    (input_root / "capture/frames/000000").mkdir(parents=True)
    (input_root / "capture/frames/000001").mkdir(parents=True)
    (input_root / "detection_2d/masks").mkdir(parents=True)
    rgb0 = np.zeros((12, 16, 3), dtype=np.uint8)
    rgb0[..., 0] = 40
    rgb1 = np.zeros((12, 16, 3), dtype=np.uint8)
    rgb1[..., 1] = 90
    mask = np.zeros((12, 16), dtype=bool)
    mask[3:8, 4:10] = True
    np.save(input_root / "capture/frames/000000/rgb.npy", rgb0)
    np.save(input_root / "capture/frames/000001/rgb.npy", rgb1)
    np.save(input_root / "detection_2d/masks/m0.npy", mask)
    np.save(input_root / "detection_2d/masks/m1.npy", mask)
    output.mkdir()

    row0 = _render_observation(input_root, output, "frame-0:det-0001",
                               {"mask_file": "masks/m0.npy"}, 0)
    row1 = _render_observation(input_root, output, "frame-1:det-0001",
                               {"mask_file": "masks/m1.npy"}, 1)
    paths0 = {panel["path"] for panel in row0["panels"].values()}
    paths1 = {panel["path"] for panel in row1["panels"].values()}
    assert paths0.isdisjoint(paths1)
    assert row0["source_to_panel_verified"] and row1["source_to_panel_verified"]
    rgb_panel0 = np.asarray(Image.open(output / row0["panels"]["rgb_original"]["path"]).convert("RGB"))
    rgb_panel1 = np.asarray(Image.open(output / row1["panels"]["rgb_original"]["path"]).convert("RGB"))
    assert np.array_equal(rgb_panel0, rgb0)
    assert np.array_equal(rgb_panel1, rgb1)
