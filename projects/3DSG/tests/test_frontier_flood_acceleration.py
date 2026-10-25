"""Accelerated reachability must exactly match the reference BFS."""

import numpy as np
import pytest

import exploration.frontier as frontier


@pytest.mark.parametrize("connectivity", [4, 8])
@pytest.mark.parametrize("prevent_corner_cut", [False, True])
def test_accelerated_flood_matches_reference_random_maps(monkeypatch, connectivity,
                                                       prevent_corner_cut):
    accelerated = frontier._binary_propagation
    if accelerated is None:
        pytest.skip("optional SciPy accelerator is unavailable")
    rng = np.random.default_rng(1739)
    for probability in (0.15, 0.55, 0.85, 1.0):
        for start in ((0, 0), (18, 24), (36, 48)):
            mask = rng.random((37, 49)) < probability
            mask[start] = True
            monkeypatch.setattr(frontier, "_binary_propagation", None)
            expected = frontier._flood(mask, start, frontier._neighbors(connectivity),
                                       prevent_corner_cut=prevent_corner_cut)
            monkeypatch.setattr(frontier, "_binary_propagation", accelerated)
            actual = frontier._flood(mask, start, frontier._neighbors(connectivity),
                                     prevent_corner_cut=prevent_corner_cut)
            assert np.array_equal(actual, expected)


def test_false_seed_and_custom_neighborhood_keep_reference_semantics(monkeypatch):
    accelerated = frontier._binary_propagation
    mask = np.ones((5, 7), dtype=bool)
    mask[2, 3] = False
    for directions in (frontier._neighbors(8), ((0, 1),)):
        monkeypatch.setattr(frontier, "_binary_propagation", None)
        expected = frontier._flood(mask, (2, 3), directions, prevent_corner_cut=True)
        monkeypatch.setattr(frontier, "_binary_propagation", accelerated)
        assert np.array_equal(frontier._flood(mask, (2, 3), directions,
                                             prevent_corner_cut=True), expected)
