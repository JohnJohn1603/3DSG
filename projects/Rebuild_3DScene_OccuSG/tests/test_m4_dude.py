from __future__ import annotations

import numpy as np
import pytest

from standalone_sg.contracts.errors import ContractError
from standalone_sg.mapping.projection import FreeSpaceGrid2D
from standalone_sg.regions.dude_client import DuDeConfig, DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor


BIN = "build/native-m4-dude/occusg_dude_worker"


def grid(values: np.ndarray, revision: int, origin=(-1.0, 2.0), resolution=0.05):
    shape = values.shape
    floors = np.zeros(shape, dtype=float)
    ceilings = np.full(shape, 2.5, dtype=float)
    return FreeSpaceGrid2D(values, resolution, origin, floors, ceilings, revision)


def shape_fixture(kind: str, n=120) -> np.ndarray:
    a = np.full((n, n), -1, dtype=np.int8)
    if kind == "rectangle":
        a[15:105, 15:105] = 0
    elif kind == "l_shape":
        a[15:105, 15:48] = 0
        a[72:105, 15:105] = 0
    elif kind == "doorway":
        a[15:105, 10:52] = 0
        a[15:105, 68:110] = 0
        a[52:68, 48:72] = 0
    elif kind == "obstacle":
        a[15:105, 15:105] = 0
        a[45:75, 45:75] = 100
    elif kind == "unknown":
        pass
    return a


def test_duDe_real_kernel_rectangle_l_doorway_obstacle_and_unknown():
    with DuDeWorkerClient(BIN) as worker:
        for rev, kind in enumerate(("rectangle", "l_shape", "doorway", "obstacle", "unknown"), 1):
            response, observations = worker.integrate(grid(shape_fixture(kind), rev), timestamp_s=float(rev))
            assert response["input_revision"] == rev
            assert response["protocol_status"] == "ok"
            assert response["status"] in {"success", "fallback", "rejected"}
            assert response["diagnostics"]["unknown_byte_value"] == 255
            assert response["diagnostics"]["pixel_tau"] == pytest.approx(30.0)
            assert all(obs.metadata["backend"] == "dude_incremental_cpp" for obs in observations)
            assert response["branch"] in {
                "accept_incremental", "accept_empty_free_space", "accept_reuse_adjusted_no_differential",
                "fallback_full_decomposition", "accept_free_contours_fallback",
                "reject_empty_fallback_preserve_previous", "reject_expanded_empty_preserve_previous",
                "reject_empty_candidate_preserve_previous",
            }


def test_m5_adapter_commits_accepted_regions_and_preserves_current_adjacency():
    p = RegionUpdateProcessor()
    with DuDeWorkerClient(BIN) as worker:
        response, result = worker.update_regions(p, grid(shape_fixture("rectangle"), 1), timestamp_s=1.0)
    assert response["branch"] == "accept_incremental"
    assert result.accepted
    assert len(result.current_regions) == 1
    assert result.current_regions[0].source_grid_revision == 1
    assert result.current_regions[0].metadata["holes_supported"] is False


def test_opt_in_preprocessing_diagnostics_include_cleaned_and_black_masks():
    values = shape_fixture("obstacle", n=120)
    with DuDeWorkerClient(BIN) as worker:
        response, _ = worker.integrate(grid(values, 1), timestamp_s=1.0,
                                       capture_preprocessed_mask=True)
    diagnostics = response["diagnostics"]
    cleaned = np.asarray(diagnostics["preprocessed_mask"], dtype=bool)
    black = np.asarray(diagnostics["black_mask"], dtype=bool)
    assert cleaned.shape == values.shape
    assert black.shape == values.shape
    assert cleaned.any()
    assert black.any()


def test_extended_unknown_boundary_policy_is_explicit_and_clamps_unknown_overlap():
    values = shape_fixture("rectangle", n=120)
    with DuDeWorkerClient(BIN, DuDeConfig(preprocessing_policy="extended_unknown_boundary")) as worker:
        response, _ = worker.integrate(grid(values, 1), timestamp_s=1.0,
                                       capture_preprocessed_mask=True)
    assert response["diagnostics"]["preprocessing_policy"] == "extended_unknown_boundary"
    assert response["diagnostics"]["preprocessed_unknown_pixels"] == 0


def test_unknown_preprocessing_policy_is_rejected():
    with pytest.raises(ContractError, match="preprocessing_policy"):
        DuDeConfig(preprocessing_policy="not-a-policy")


def test_empty_then_real_update_guard_reject_does_not_age_tracker():
    p = RegionUpdateProcessor()
    with DuDeWorkerClient(BIN) as worker:
        _, rejected = worker.update_regions(p, grid(shape_fixture("unknown"), 1), timestamp_s=1.0)
        assert not rejected.accepted
        before = rejected.retained_tracks
        _, accepted = worker.update_regions(p, grid(shape_fixture("rectangle"), 2), timestamp_s=2.0)
    assert accepted.accepted
    assert accepted.new_ids == (1,)
    assert before == ()


def test_worker_rejects_parameter_change_and_can_explicitly_reset():
    with DuDeWorkerClient(BIN, DuDeConfig(decomp_threshold_m=1.5)) as worker:
        worker.integrate(grid(shape_fixture("rectangle"), 1), timestamp_s=1.0)
        req = {
            "protocol_version": 1, "op": "integrate", "request_id": "bad", "revision": 2,
            "frame_id": "world", "grid": {"width": 120, "height": 120, "resolution_m": .05,
              "origin_xy": [-1., 2.], "values": shape_fixture("rectangle").astype(int).ravel().tolist()},
            "config": {"decomp_threshold_m": 0.8, "safety_distance_m": 0.5},
        }
        with pytest.raises(ContractError, match="config_changed_requires_reset"):
            worker._exchange(req)
    with DuDeWorkerClient(BIN) as worker:
        worker.integrate(grid(shape_fixture("rectangle"), 1), timestamp_s=1.0)
        assert worker.reset()["status"] == "reset"
        response, _ = worker.integrate(grid(shape_fixture("rectangle"), 2), timestamp_s=2.0)
        assert response["input_revision"] == 2


def test_resolution_change_requires_reset_and_validation_is_strict():
    with DuDeWorkerClient(BIN) as worker:
        worker.integrate(grid(shape_fixture("rectangle"), 1), timestamp_s=1.0)
        with pytest.raises(ContractError, match="resolution_changed_requires_reset"):
            worker.integrate(grid(shape_fixture("rectangle"), 2, resolution=0.1), timestamp_s=2.0)
    with pytest.raises(ContractError, match="finite"):
        DuDeConfig(decomp_threshold_m=float("nan"))


def test_decomposer_handles_map_growth_and_origin_shift_in_same_process():
    with DuDeWorkerClient(BIN) as worker:
        small = np.full((80, 80), -1, dtype=np.int8); small[10:70, 10:70] = 0
        first, _ = worker.integrate(grid(small, 1, origin=(-2, -3)), timestamp_s=1.0)
        larger = np.full((120, 120), -1, dtype=np.int8); larger[30:90, 30:90] = 0
        second, _ = worker.integrate(grid(larger, 2, origin=(-3, -4)), timestamp_s=2.0)
    assert first["regions"] and second["regions"]
    assert second["input_revision"] == 2
    assert second["diagnostics"]["state_dimensions"] == [120, 120]
