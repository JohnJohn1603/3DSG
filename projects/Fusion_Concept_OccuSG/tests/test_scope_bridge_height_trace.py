from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

from standalone_sg.mapping.native import OctoMapWorker, ScanPoint
from standalone_sg.regions.dude_client import DuDeWorkerClient
from standalone_sg.resource_binding import resolve_external_file


NATIVE = os.environ.get("SCOPE_BRIDGE_NATIVE_BINARY")
if not NATIVE:
    pytest.skip("set SCOPE_BRIDGE_NATIVE_BINARY to run the isolated native instrumentation parity tests",
                allow_module_level=True)
NATIVE = Path(NATIVE)
BASELINE_NATIVE = resolve_external_file("native_m2_m3", required=True, executable=True)
DUDE = resolve_external_file("native_dude", required=True, executable=True)


def _scan_sequence(multi_height: bool):
    # A 2 m square of vertical rays gives DuDe enough horizontal support for
    # a non-empty candidate region while keeping each test deterministic.
    for ix in range(41):
        x = ix * 0.05 + 0.025
        for iy in range(41):
            y = iy * 0.05 + 0.025
            if multi_height:
                yield (x, y, 0.15), (x, y, 1.05)
                yield (x, y, 1.20), (x, y, 2.20)
            else:
                yield (x, y, 0.15), (x, y, 2.20)


def _run_projection(binary: Path, multi_height: bool, trace_dir: Path | None):
    worker = OctoMapWorker(
        binary, resolution_m=0.05, projection_min_free_height_m=0.3,
        projection_minimum_occupancy=5, timeout_s=20.0,
        height_interval_trace_dir=trace_dir,
    )
    try:
        for origin, endpoint in _scan_sequence(multi_height):
            worker.integrate((ScanPoint(*endpoint, has_hit=True),), origin=origin,
                             max_range_m=5.0)
        grid = worker.project(mode="full_refresh")
        trace = worker.last_height_interval_trace
        return grid, trace
    finally:
        worker.close()


def _m4_signature(grid):
    with DuDeWorkerClient(DUDE) as worker:
        response, _ = worker.integrate(grid, timestamp_s=1.0, capture_preprocessed_mask=True)
    diagnostics = response.get("diagnostics", {})
    return {
        "status": response.get("status"),
        "branch": response.get("branch"),
        "regions": response.get("regions", []),
        "preprocessed_mask": diagnostics.get("preprocessed_mask"),
        "black_mask": diagnostics.get("black_mask"),
    }


@pytest.mark.parametrize("multi_height", [False, True], ids=["single-observed-height", "two-free-intervals-same-xy"])
def test_read_only_interval_trace_preserves_m3_grid_and_m4_rooms(tmp_path: Path, multi_height: bool):
    baseline_grid, baseline_trace = _run_projection(BASELINE_NATIVE, multi_height, None)
    plain_grid, plain_trace = _run_projection(NATIVE, multi_height, None)
    trace_dir = tmp_path / "native-height-intervals"
    traced_grid, trace_ref = _run_projection(NATIVE, multi_height, trace_dir)

    assert baseline_trace is None
    assert plain_trace is None
    assert trace_ref is not None
    assert trace_ref["file"].startswith(str(trace_dir.resolve()))
    trace_path = Path(trace_ref["file"])
    trace_doc = json.loads(trace_path.read_text())
    assert trace_doc["capture_point"] == "after_min_free_height_pruning_before_z_envelope_collapse"
    assert trace_doc["coordinate_frame"] == "world"
    assert trace_doc["units"] == "m"
    assert len(trace_doc["cells"]) == trace_ref["cell_count"]

    # The optional trace is indexed through the same world-XY -> Map2D
    # transform as the M3 output. It must not rely on an implicit double-to-int
    # overload for height access.
    origin_x, origin_y = traced_grid.origin_xy
    for cell in trace_doc["cells"]:
        world_x, world_y = cell["world_xy_m"]
        col = math.floor((world_x - origin_x) / traced_grid.resolution_m)
        row = math.floor((world_y - origin_y) / traced_grid.resolution_m)
        assert 0 <= row < traced_grid.z_floor.shape[0]
        assert 0 <= col < traced_grid.z_floor.shape[1]
        for key, array in (("z_floor_m", traced_grid.z_floor), ("z_ceiling_m", traced_grid.z_ceiling)):
            actual = cell[key]
            expected = float(array[row, col])
            if math.isnan(expected):
                assert actual is None
            else:
                assert actual is not None
                assert actual == pytest.approx(expected, abs=1e-5)

    for reference_grid in (baseline_grid, plain_grid):
        np.testing.assert_array_equal(reference_grid.values, traced_grid.values)
        np.testing.assert_array_equal(reference_grid.z_floor, traced_grid.z_floor)
        np.testing.assert_array_equal(reference_grid.z_ceiling, traced_grid.z_ceiling)
        assert reference_grid.origin_xy == traced_grid.origin_xy
        assert reference_grid.resolution_m == traced_grid.resolution_m
        assert reference_grid.shape == traced_grid.shape
        assert _m4_signature(reference_grid) == _m4_signature(traced_grid)
    assert len(_m4_signature(traced_grid)["regions"]) == 1

    center = min(trace_doc["cells"], key=lambda row: (
        (row["world_xy_m"][0] - 1.025) ** 2 + (row["world_xy_m"][1] - 1.025) ** 2
    ))
    interval_count = len(center["free_intervals_used_by_projection"])
    assert interval_count == (2 if multi_height else 1)
