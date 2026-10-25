from __future__ import annotations

import numpy as np

from scripts.analyze_scope_bridge_trace import (
    inside_ring,
    interval_envelope,
    intervals_are_disjoint,
    remap_prefix_events,
)


def test_prefix_remapping_excludes_future_and_uses_latest_available_event():
    early = {"world_xy_m": [0.025, 0.025], "tag": "early"}
    future = {"world_xy_m": [0.025, 0.025], "tag": "future"}
    events = [(1, early), (2, future)]

    prefix_one = remap_prefix_events(events, 1, [0.0, 0.0], 0.05, (4, 4))
    prefix_two = remap_prefix_events(events, 2, [0.0, 0.0], 0.05, (4, 4))

    assert prefix_one[(0, 0)] == (1, early)
    assert prefix_two[(0, 0)] == (2, future)


def test_interval_helpers_distinguish_disjoint_free_intervals():
    intervals = [
        {"bottom_m": 2.10, "top_m": 2.40},
        {"bottom_m": 0.55, "top_m": 1.95},
    ]
    assert intervals_are_disjoint(intervals)
    assert interval_envelope(intervals) == (0.55, 2.40)
    assert not intervals_are_disjoint([
        {"bottom_m": 0.0, "top_m": 1.2},
        {"bottom_m": 1.0, "top_m": 2.0},
    ])


def test_world_xy_contour_rasterization_includes_polygon_boundary():
    ring = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    result = inside_ring(
        np.array([0.5, 1.0, 1.1]),
        np.array([0.5, 0.5, 0.5]),
        ring,
    )
    assert result.tolist() == [True, True, False]
