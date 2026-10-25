"""Integration checks for the pinned native M3 backend in the M2 worker."""

from pathlib import Path

import numpy as np
import pytest

from standalone_sg.contracts import ContractError
from standalone_sg.mapping import OctoMapWorker, ScanPoint


ROOT = Path(__file__).resolve().parents[1]
M3_BINARY = ROOT / "build/native-m3/occupancy/standalone_sg_octomap_fixture_runner"
M3_SMOKE_BINARY = ROOT / "build/native-m3/occupancy/standalone_sg_m3_projection_smoke"


def _initial_free_scan() -> list[ScanPoint]:
    # Populate a small 3-D volume with confirmed no-return rays, giving
    # MapConverter enough XY support to produce floor/ceiling diagnostics.
    return [
        ScanPoint(x, y, z, has_hit=False)
        for x in (0.05, 0.15, 0.25, 0.35, 0.45)
        for y in (0.05, 0.15, 0.25, 0.35, 0.45)
        for z in (0.35, 0.45, 0.55)
    ]


@pytest.mark.skipif(not M3_BINARY.exists(), reason="native M3 worker has not been built")
def test_integrate_project_integrate_project_uses_accumulated_native_tree() -> None:
    with OctoMapWorker(M3_BINARY, resolution_m=0.1, timeout_s=10.0, response_mode="debug") as worker:
        first_update = worker.integrate(
            _initial_free_scan(), origin=(0.05, 0.05, 0.05), max_range_m=0.7
        )
        first_snapshot = worker.snapshot()
        first = worker.project(mode="full_refresh")
        second_update = worker.integrate(
            [
                ScanPoint(x, y, 0.55)
                for x in (0.25, 0.35, 0.45)
                for y in (0.25, 0.35, 0.45)
            ],
            origin=(0.05, 0.05, 0.05),
            max_range_m=0.7,
        )
        second_snapshot = worker.snapshot()
        second = worker.project(mode="partial_update")

    assert first_update["revision"] == first.revision == 1
    assert second_update["revision"] == second.revision == 2
    assert first.backend == second.backend == "cpp_mapconverter"
    assert first.projection_mode == "full_refresh" and first.updated is True
    assert second.projection_mode == "partial_update" and second.updated is True
    assert first.shape == second.shape
    assert first.resolution_m == second.resolution_m == pytest.approx(0.1)
    assert first.origin_xy == second.origin_xy == pytest.approx((0.0, 0.0))
    first_min, first_max = first_snapshot["bounds_min"], first_snapshot["bounds_max"]
    assert first.changed_bounds_xyz == pytest.approx(
        (first_min[0], first_max[0], first_min[1], first_max[1], first_min[2], first_max[2]),
        abs=1e-6,
    )
    assert second.changed_bounds_xyz is not None
    assert second.changed_bounds_xyz[0] == pytest.approx(0.25)
    assert second.changed_bounds_xyz[1] == pytest.approx(0.45)
    assert second.changed_bounds_xyz[4] == pytest.approx(second_snapshot["bounds_min"][2], abs=1e-6)
    assert second.changed_bounds_xyz[5] == pytest.approx(second_snapshot["bounds_max"][2], abs=1e-6)
    assert second_update["occupied_keys"]
    assert any(item["occupied"] for item in second_update["observations"])
    assert np.any(first.values == 0)
    assert np.isfinite(first.z_floor).any()
    assert np.isfinite(first.z_ceiling).any()
    assert np.isfinite(second.z_floor).any()


@pytest.mark.skipif(not M3_BINARY.exists(), reason="native M3 worker has not been built")
def test_partial_update_expands_map_and_shifts_world_origin() -> None:
    with OctoMapWorker(M3_BINARY, resolution_m=0.1, timeout_s=10.0) as worker:
        worker.integrate(
            _initial_free_scan(), origin=(0.05, 0.05, 0.05), max_range_m=0.7
        )
        before = worker.project(mode="full_refresh")
        worker.integrate(
            [ScanPoint(-0.35, -0.25, 0.45)],
            origin=(0.05, 0.05, 0.05),
            max_range_m=1.0,
        )
        after = worker.project(mode="partial_update")

    assert after.updated is True
    assert after.shape[0] > before.shape[0]
    assert after.shape[1] > before.shape[1]
    assert after.origin_xy[0] < before.origin_xy[0]
    assert after.origin_xy[1] < before.origin_xy[1]
    assert after.changed_bounds_xy is not None
    assert after.changed_bounds_xy[1] < 0


@pytest.mark.skipif(not M3_BINARY.exists(), reason="native M3 worker has not been built")
def test_full_refresh_is_stable_across_repeated_calls() -> None:
    with OctoMapWorker(M3_BINARY, resolution_m=0.1, timeout_s=10.0) as worker:
        worker.integrate(
            _initial_free_scan(), origin=(0.05, 0.05, 0.05), max_range_m=0.7
        )
        reference = worker.project(mode="full_refresh")
        worker.project(mode="full_refresh")
        rss_before_kib = _worker_rss_kib(worker._process.pid)
        repeated = [worker.project(mode="full_refresh") for _ in range(48)]
        rss_after_kib = _worker_rss_kib(worker._process.pid)

    for grid in repeated:
        assert grid.origin_xy == reference.origin_xy
        assert grid.shape == reference.shape
        np.testing.assert_array_equal(grid.values, reference.values)
        np.testing.assert_allclose(grid.z_floor, reference.z_floor, equal_nan=True)
        np.testing.assert_allclose(grid.z_ceiling, reference.z_ceiling, equal_nan=True)
    # Small fixture: generous allowance for allocator high-water behavior,
    # while still catching a per-call leak of Map2D/HeightRangeMap buffers.
    assert rss_after_kib - rss_before_kib < 12 * 1024


def _worker_rss_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError("worker process has no VmRSS entry")


@pytest.mark.skipif(not M3_SMOKE_BINARY.exists(), reason="native M3 smoke harness has not been built")
def test_native_callback_reference_matches_metric_bounds_and_deletion_cases() -> None:
    import subprocess

    subprocess.run([str(M3_SMOKE_BINARY)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(not M3_BINARY.exists(), reason="native M3 worker has not been built")
def test_native_projection_rejects_invalid_mode_before_request() -> None:
    with OctoMapWorker(M3_BINARY, resolution_m=0.1) as worker:
        with pytest.raises(ContractError, match="projection mode"):
            worker.project(mode="python_port_debug")


@pytest.mark.skipif(not M3_BINARY.exists(), reason="native M3 worker has not been built")
def test_clean_full_refresh_uses_new_converter_on_same_tree_and_matches_source() -> None:
    with OctoMapWorker(M3_BINARY, resolution_m=0.1, timeout_s=10.0) as worker:
        worker.integrate(_initial_free_scan(), origin=(0.05, 0.05, 0.05), max_range_m=0.7)
        live = worker.project(mode="full_refresh")
        clean = worker.project(mode="clean_full_refresh")
        oracle = dict(worker.last_projection_diagnostics)

    assert oracle["backend"] == "pinned_cpp_MapConverter_direct"
    assert oracle["converter_state"] == "new"
    assert oracle["bounds_match"] is True
    assert oracle["input_leaves_match"] is True
    assert oracle["geometry_match"] is True
    assert oracle["value_mismatches"] == 0
    assert oracle["floor_mismatches"] == 0
    assert oracle["ceiling_mismatches"] == 0
    assert clean.revision == live.revision
    np.testing.assert_array_equal(clean.values, live.values)
    np.testing.assert_allclose(clean.z_floor, live.z_floor, equal_nan=True)
    np.testing.assert_allclose(clean.z_ceiling, live.z_ceiling, equal_nan=True)
