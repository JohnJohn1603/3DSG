from __future__ import annotations

import numpy as np

from scripts.validate_replica_cad_instance_capture import _preview


def test_replica_cad_instance_preview_validates_raw_rgbd_shapes(tmp_path):
    capture = tmp_path / "capture"
    frame_dir = capture / "frames" / "000000"
    frame_dir.mkdir(parents=True)
    rgb = np.zeros((3, 4, 4), dtype=np.uint8)
    rgb[..., 0] = 17
    depth = np.array(
        [[1.0, np.nan, 2.0, 0.0], [1.5, 2.5, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0]],
        dtype=np.float32,
    )
    valid = np.isfinite(depth) & (depth > 0.0)
    np.save(frame_dir / "rgb.npy", rgb)
    np.save(frame_dir / "depth_m.npy", depth)
    np.save(frame_dir / "valid_mask.npy", valid)

    output = tmp_path / "preview"
    output.mkdir()
    result = _preview(
        capture,
        {"frame_dir": "frames/000000", "frame_id": "replica-cad-0000"},
        output,
    )

    assert result["rgb_shape"] == [3, 4, 4]
    assert result["depth_shape"] == [3, 4]
    assert result["valid_depth_pixels"] == 10
    assert result["total_depth_pixels"] == 12
    assert (output / "replica-cad-0000_rgb.png").is_file()
    assert (output / "replica-cad-0000_depth.png").is_file()
