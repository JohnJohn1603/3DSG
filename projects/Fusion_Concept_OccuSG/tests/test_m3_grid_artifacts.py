from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from standalone_sg.mapping import FreeSpaceGrid2D
from standalone_sg.mapping.grid_artifacts import GridTraceWriter
from standalone_sg.pipeline.contracts import GraphNode, SceneGraphSnapshot
from standalone_sg.regions.dude_client import DuDeUpdateResult


class _Packet:
    frame_index = 3
    frame_id = "frame-3"
    timestamp_s = 4.5
    base_pose_W = (1.0, 2.0, 0.0)


def test_grid_trace_is_lossless_and_keeps_cross_stage_provenance(tmp_path: Path):
    values = np.asarray([[-1, 0, 100], [0, 25, -1]], dtype=np.int8)
    nan = np.full(values.shape, np.nan, dtype=np.float64)
    grid = FreeSpaceGrid2D(values, .05, (-2.0, 3.0), nan, nan, 8, projection_mode="partial_update")
    writer = GridTraceWriter(tmp_path / "trace")
    response = {"status": "fallback", "branch": "fallback_full_decomposition", "input_revision": 8,
                "diagnostics": {"preprocessed_mask": [[False, True, False], [True, False, False]],
                                "black_mask": [[False, False, True], [False, False, False]],
                                "preprocessed_free_pixels": 2, "crop": [0, 0, 3, 2]}}
    result = DuDeUpdateResult("fallback", "fallback_full_decomposition", 8, 8, False, (), (), None)
    snapshot = SceneGraphSnapshot(1, "scene", "seq", 4, 4.5,
                                  (GraphNode("room:1", "room", 1.0, 4.5, "test", {"anchor_region_id": 1}),), (), "cfg", {"regions": False})
    writer.write_frame(packet=_Packet(), grid=grid, response=response, result=result, snapshot=snapshot)
    writer.close()
    meta = json.loads((tmp_path / "trace/frames/000003.json").read_text())
    assert meta["map_revision"] == 8
    assert meta["m4"]["branch"] == "fallback_full_decomposition"
    with np.load(tmp_path / "trace/frames/000003.npz", allow_pickle=False) as arrays:
        np.testing.assert_array_equal(arrays["values"], values)
        assert arrays["m4_preprocessed_mask"].dtype == np.bool_
        assert set(arrays.files) == {"values", "z_floor", "z_ceiling", "m4_preprocessed_mask", "m4_black_mask"}
    index = json.loads((tmp_path / "trace/index.json").read_text())
    assert index["frame_count"] == 1
    assert index["does_not_reproject"] is True
