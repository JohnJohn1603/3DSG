import json
from pathlib import Path

import numpy as np
import pytest

from standalone_sg.contracts import ContractError
from standalone_sg.mapping import project_leaves, project_snapshot


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/m3_mapconversion_reference.json"


def test_projection_matches_pinned_mapconversion_fixture() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    grid = project_leaves(
        fixture["leaves"],
        resolution_m=fixture["resolution_m"],
        min_free_height_m=fixture["min_free_height_m"],
        minimum_occupancy=fixture["minimum_occupancy"],
        bounds_xy=tuple(fixture["bounds_xy"]),
        revision=fixture["revision"],
    )
    np.testing.assert_array_equal(grid.values, np.asarray(fixture["expected_values"], dtype=np.int8))
    np.testing.assert_allclose(
        grid.z_floor[2],
        np.asarray(fixture["expected_z_floor"], dtype=np.float64),
        equal_nan=True,
        atol=2e-6,
    )
    np.testing.assert_allclose(
        grid.z_ceiling[2],
        np.asarray(fixture["expected_z_ceiling"], dtype=np.float64),
        equal_nan=True,
        atol=2e-6,
    )
    assert grid.revision == 7
    assert grid.kind == "structural_clearance"
    assert grid.z_floor[2, 3] == pytest.approx(-0.001000006, abs=2e-7)
    assert grid.z_ceiling[2, 3] == pytest.approx(1.201000003, abs=2e-7)


def test_clearance_boundary_is_strict_and_epsilon_matches_source() -> None:
    rejected = project_leaves(
        [{"center": (1.5, 2.5, 0.15), "size": 0.2979, "occupied": False}],
        resolution_m=1.0,
        min_free_height_m=0.3,
        bounds_xy=(0.0, 3.0, 0.0, 5.0),
    )
    accepted = project_leaves(
        [{"center": (1.5, 2.5, 0.2981 / 2), "size": 0.2981, "occupied": False}],
        resolution_m=1.0,
        min_free_height_m=0.3,
        bounds_xy=(0.0, 3.0, 0.0, 5.0),
    )
    assert rejected.values[2, 1] == -1
    assert accepted.values[2, 1] == 0


def test_unknown_gap_is_not_filled_and_low_table_remains_structural_free() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    grid = project_leaves(
        fixture["leaves"],
        resolution_m=1.0,
        min_free_height_m=0.3,
        bounds_xy=(0.0, 8.0, 0.0, 5.0),
    )
    # x=3 has two free intervals separated by an unknown gap. Diagnostics use
    # the lowest floor and highest ceiling without implying connected space.
    assert grid.values[2, 3] == 0
    assert grid.z_floor[2, 3] < grid.z_ceiling[2, 3]
    # x=4 contains a low occupied table and a valid structural free interval.
    assert grid.values[2, 4] == 0
    # x=6 is an occupied wall adjacent to free x=5; HeightRangeMap reports 100.
    assert grid.values[2, 6] == 100


def test_disjoint_z_intervals_select_lowest_free_range_for_neighbour_occupation() -> None:
    leaves = [
        {"center": (4.5, 3.5, 0.2), "size": 0.4, "occupied": False},
        {"center": (4.5, 3.5, 1.2), "size": 0.4, "occupied": False},
        {"center": (5.5, 3.5, 0.2), "size": 0.4, "occupied": True},
    ]
    grid = project_leaves(
        leaves,
        resolution_m=1.0,
        min_free_height_m=0.3,
        bounds_xy=(0.0, 8.0, 0.0, 7.0),
    )
    # The neighbouring occupied interval overlaps only the lowest free range.
    assert grid.values[3, 5] == 100
    assert grid.z_floor[3, 4] == pytest.approx(-0.001000003, abs=2e-7)
    assert grid.z_ceiling[3, 4] == pytest.approx(1.401000003, abs=2e-7)


def test_large_leaf_is_rasterized_over_covered_xy_cells() -> None:
    grid = project_leaves(
        [{"center": (2.5, 2.5, 0.15), "size": 2.0, "occupied": False}],
        resolution_m=1.0,
        min_free_height_m=0.3,
        bounds_xy=(0.0, 6.0, 0.0, 6.0),
    )
    assert grid.values[1:3, 1:3].tolist() == [[0, 0], [0, 0]]


def test_negative_and_shifted_origins_are_world_anchored() -> None:
    leaves = [{"center": (-1.5, -0.5, 0.15), "size": 0.3, "occupied": False}]
    negative = project_leaves(
        leaves,
        resolution_m=1.0,
        bounds_xy=(-3.0, 1.0, -2.0, 2.0),
        revision=3,
    )
    shifted = project_leaves(
        [{"center": (8.5, 4.5, 0.15), "size": 0.3, "occupied": False}],
        resolution_m=1.0,
        bounds_xy=(7.0, 11.0, 3.0, 7.0),
        revision=3,
    )
    assert negative.origin_xy == (-3.0, -2.0)
    assert shifted.origin_xy == (7.0, 3.0)
    np.testing.assert_array_equal(negative.values, shifted.values)
    assert negative.values[1, 1] == shifted.values[1, 1] == 0


def test_snapshot_projection_preserves_accumulated_revision_bounds() -> None:
    snapshot = {
        "revision": 11,
        "resolution": 1.0,
        "bounds_min": [0.0, 0.0, 0.0],
        "bounds_max": [4.0, 4.0, 1.0],
        "leaves": [{"center": [1.5, 1.5, 0.15], "size": 0.3, "occupied": False}],
    }
    grid = project_snapshot(snapshot)
    assert grid.revision == 11
    assert grid.origin_xy == (0.0, 0.0)
    assert grid.changed_bounds_xy is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resolution_m": 0.0},
        {"resolution_m": 1.0, "min_free_height_m": -0.1},
        {"resolution_m": 1.0, "minimum_occupancy": 5.5},
    ],
)
def test_projection_rejects_invalid_contracts(kwargs: dict) -> None:
    with pytest.raises(ContractError):
        project_leaves(
            [{"center": (1.5, 1.5, 0.15), "size": 0.3, "occupied": False}],
            bounds_xy=(0.0, 3.0, 0.0, 3.0),
            **kwargs,
        )
