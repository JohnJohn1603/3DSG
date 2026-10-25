"""Synthetic M5 regressions; none of these polygons are DuDe output."""

from __future__ import annotations

import math

import pytest

from standalone_sg.config import RegionGuardConfig, RegionTrackerConfig, load_config
from standalone_sg.contracts.errors import ContractError
from standalone_sg.contracts.regions import Polygon2D, RegionObservation
from standalone_sg.regions import RegionTracker, RegionUpdateGuard, RegionUpdateProcessor


def rect(
    raw_id: int | str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    t: float = 0.0,
    revision: int = 0,
    area: float | None = None,
    holes=(),
    adjacent=(),
) -> RegionObservation:
    outer = ((x, y), (x + width, y), (x + width, y + height), (x, y + height))
    return RegionObservation(
        raw_id=raw_id,
        polygon=Polygon2D(outer, holes),
        timestamp_s=t,
        source_grid_revision=revision,
        centroid_xy=(x + width / 2.0, y + height / 2.0),
        area_m2=width * height if area is None else area,
        convex_hull_xy=outer,
        adjacent_raw_ids=tuple(adjacent),
        metadata={"fixture": "synthetic"},
    )


def _guard_args(observations, *, free=20, unknown=0, cells=100):
    return dict(observations, free_cells=free, unknown_cells=unknown, expected_cells=cells)


def test_config_resolves_active_runtime_yaml_values_and_provenance():
    config = load_config("configs/reference_code.yaml")
    assert config.region_tracker.enabled is True
    assert config.region_tracker.min_bbox_iou == 0.20
    # Active inc_dude_params.yaml overrides BasicRegionTracker::Config's 1.0 m.
    assert config.region_tracker.max_centroid_distance_m == 1.5
    assert config.region_tracker.min_area_ratio == 0.25
    assert config.region_tracker.max_area_ratio == 4.0
    assert config.region_tracker.max_missed_updates == 3
    assert config.region_guard.max_unknown_ratio_for_update == 0.98
    assert config.region_guard.max_region_area_drop_ratio == 0.85
    assert config.region_guard.max_region_count_drop_ratio == 0.85
    assert config.config_hash


@pytest.mark.parametrize(
    "patch",
    [
        {"regions": {"tracker": {"max_centroid_distance_m": 1.5, "typo": 7}}},
        {"regions": {"guard": {"max_region_area_drop_ratio": float("nan")}}},
        {"regions": {"guard": {"min_valid_region_count": 1.0}}},
    ],
)
def test_region_config_rejects_unknown_wrong_type_and_nonfinite(tmp_path, patch):
    base = load_config("configs/reference_code.yaml").to_dict()
    base.update(patch)
    config_file = tmp_path / "config.yaml"
    import yaml

    config_file.write_text(yaml.safe_dump(base), encoding="utf-8")
    with pytest.raises(ContractError):
        load_config(config_file)


def test_observation_contract_keeps_raw_metadata_and_polygon_holes():
    hole = ((1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (1.0, 2.0))
    observation = rect("raw-A", 0, 0, 4, 4, holes=(hole,), t=2.5, revision=9)
    assert observation.raw_id == "raw-A"
    assert observation.polygon.net_area_m2 == 15.0
    assert observation.reference_area_m2 == 16.0  # source area field wins; source tracker ignores rings/holes
    assert observation.source_grid_revision == 9
    assert observation.metadata["fixture"] == "synthetic"
    with pytest.raises(TypeError):
        observation.metadata["fixture"] = "changed"


def test_raw_id_reorder_does_not_change_unique_geometry_identity():
    tracker = RegionTracker()
    first = tracker.update((rect(80, 0, 0, 2, 2), rect(10, 10, 0, 2, 2)))
    assert [item.persistent_id for item in first.current_regions] == [1, 2]

    # Manual bbox/centroid/area calculation: each new rectangle has IoU=1 with
    # exactly one old track and area ratio 1, so IDs follow geometry, not raw IDs.
    second = tracker.update(
        (rect("new-right", 10.1, 0, 2, 2, t=1, revision=1), rect("new-left", 0.1, 0, 2, 2, t=1, revision=1))
    )
    assert [item.persistent_id for item in second.current_regions] == [2, 1]
    assert second.current_regions[0].raw_id == "new-right"
    assert second.current_regions[1].raw_id == "new-left"
    assert [track.first_seen_s for track in second.retained_tracks] == [0.0, 0.0]
    assert [track.last_seen_s for track in second.retained_tracks] == [1.0, 1.0]


def test_area_growth_and_holes_use_reported_source_area_and_bbox_iou():
    tracker = RegionTracker()
    tracker.update((rect(1, 0, 0, 2, 2),))
    # A hole changes polygon topology, not source convex-hull bbox matching;
    # the supplied Region2D area remains the tracker area feature.
    hole = ((0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5))
    result = tracker.update((rect(99, 0, 0, 2, 2, t=1, area=4.0, holes=(hole,)),))
    assert result.current_regions[0].persistent_id == 1
    assert result.matched_ids == (1,)


def test_tracker_uses_shell_area_fallback_only_when_message_area_is_not_positive():
    tracker = RegionTracker()
    tracker.update((rect(1, 0, 0, 2, 2, area=0.0),))
    result = tracker.update((rect(2, 0, 0, 2, 2, t=1, area=-1.0),))
    assert result.current_regions[0].persistent_id == 1
    assert result.current_regions[0].area_m2 == 4.0


@pytest.mark.parametrize("scale,expected_same", [(0.5, True), (2.0, True), (0.499, False), (2.001, False)])
def test_area_ratio_gate_is_inclusive_at_quarter_and_four(scale, expected_same):
    tracker = RegionTracker(RegionTrackerConfig(max_centroid_distance_m=0.0))
    tracker.update((rect(1, 0, 0, 2, 2),))
    current = rect(2, 1.0 - scale, 1.0 - scale, 2 * scale, 2 * scale, t=1)
    result = tracker.update((current,))
    assert (result.current_regions[0].persistent_id == 1) is expected_same


def test_bbox_iou_gate_and_centroid_distance_or_gate_boundaries():
    # Equal 1x1 boxes shifted by 2/3 have intersection-over-union 0.2.
    cfg = RegionTrackerConfig(min_bbox_iou=0.2, max_centroid_distance_m=0.5)
    tracker = RegionTracker(cfg)
    tracker.update((rect(1, 0, 0, 1, 1),))
    at_iou = tracker.update((rect(2, 2.0 / 3.0, 0, 1, 1, t=1),))
    assert at_iou.current_regions[0].persistent_id == 1

    # Separate tracker to make IoU=0 and centroid distance exactly the inclusive 1.5m limit.
    tracker = RegionTracker(RegionTrackerConfig(min_bbox_iou=0.2, max_centroid_distance_m=1.5))
    tracker.update((rect(1, 0, 0, 1, 1),))
    at_distance = tracker.update((rect(2, 1.5, 0, 1, 1, t=1),))
    assert at_distance.current_regions[0].persistent_id == 1

    outside = tracker.update((rect(3, 3.000001, 0, 1, 1, t=2),))
    assert outside.current_regions[0].persistent_id == 2


def test_bbox_iou_is_used_instead_of_polygon_iou():
    tracker = RegionTracker(RegionTrackerConfig(max_centroid_distance_m=0.0))
    outer = ((0, 0), (4, 0), (4, 4), (0, 4))
    # Same axis-aligned bbox and same message centroid/area despite a different
    # concave polygon/hole arrangement: source features use convex-hull bounds.
    first = RegionObservation(1, Polygon2D(outer), 0, 0, (2, 2), 16, outer)
    second = RegionObservation(2, Polygon2D(outer, (((1, 1), (3, 1), (3, 3), (1, 3)),)), 1, 1, (2, 2), 16, outer)
    tracker.update((first,))
    assert tracker.update((second,)).current_regions[0].persistent_id == 1


def test_greedy_conflict_and_tie_break_use_id_then_current_input_index():
    tracker = RegionTracker(RegionTrackerConfig(max_centroid_distance_m=1.5))
    tracker.update((rect(1, 0, 0, 2, 2), rect(2, 0, 0, 2, 2)))
    # All four candidate scores/IoUs/distances tie. Source comparator orders
    # canonical ID then current index; greedy consumes (track 1,current 0),
    # then (track 2,current 1), unlike a global Hungarian reassignment.
    result = tracker.update((rect(30, 0, 0, 2, 2, t=1), rect(20, 0, 0, 2, 2, t=1)))
    assert [item.persistent_id for item in result.current_regions] == [1, 2]
    assert result.matched_ids == (1, 2)


def test_reference_score_orders_candidates_before_greedy_assignment():
    tracker = RegionTracker()
    tracker.update((rect(1, 0, 0, 2, 2), rect(2, 1, 0, 2, 2)))
    # For current box [0.7,2.7]x[0,2], hand calculation from source formula:
    # track 1 IoU=2.6/5.4, d=.7; track 2 IoU=3.4/4.6, d=.3.
    # With ratio=1, score=2*IoU + (1-d/1.5) + .25, so track 2 wins.
    current = tracker.update((rect("candidate", 0.7, 0, 2, 2, t=1),))
    assert current.current_regions[0].persistent_id == 2
    assert next(track for track in current.retained_tracks if track.persistent_id == 1).miss_count == 1


@pytest.mark.parametrize("misses", [1, 2, 3])
def test_missing_track_is_retained_but_not_published(misses):
    tracker = RegionTracker()
    tracker.update((rect(1, 0, 0, 2, 2),))
    step = None
    for _ in range(misses):
        step = tracker.update(())
    assert step is not None and step.current_regions == ()
    assert len(step.retained_tracks) == 1
    assert step.retained_tracks[0].persistent_id == 1
    assert step.retained_tracks[0].miss_count == misses
    assert step.retained_tracks[0].status == "temporarily_missing"


def test_track_expires_on_fourth_miss_and_reappearance_gets_new_id():
    tracker = RegionTracker(RegionTrackerConfig(max_missed_updates=3))
    tracker.update((rect(1, 0, 0, 2, 2),))
    for _ in range(3):
        step = tracker.update(())
        assert step.retained_tracks[0].miss_count in (1, 2, 3)
        assert step.expired_ids == ()
    fourth = tracker.update(())
    assert fourth.retained_tracks == ()
    assert fourth.expired_ids == (1,)
    reappeared = tracker.update((rect(8, 0, 0, 2, 2, t=5),))
    assert reappeared.current_regions[0].persistent_id == 2
    assert reappeared.new_ids == (2,)


def test_reappearance_before_expiration_resets_miss_count_and_preserves_first_seen():
    tracker = RegionTracker()
    tracker.update((rect(1, 0, 0, 2, 2, t=2),))
    tracker.update(())
    tracker.update(())
    result = tracker.update((rect(9, 0, 0, 2, 2, t=7),))
    assert result.current_regions[0].persistent_id == 1
    assert result.retained_tracks[0].first_seen_s == 2
    assert result.retained_tracks[0].last_seen_s == 7
    assert result.retained_tracks[0].miss_count == 0


def test_current_snapshot_and_retained_track_memory_are_distinct():
    tracker = RegionTracker()
    tracker.update((rect(1, 0, 0, 1, 1), rect(2, 10, 0, 1, 1)))
    result = tracker.update((rect(3, 10, 0, 1, 1, t=1),))
    assert [region.persistent_id for region in result.current_regions] == [2]
    assert {track.persistent_id for track in result.retained_tracks} == {1, 2}
    assert next(track for track in result.retained_tracks if track.persistent_id == 1).miss_count == 1


def test_raw_adjacency_remaps_to_sorted_unique_persistent_ids():
    tracker = RegionTracker()
    result = tracker.update((rect(17, 0, 0, 1, 1), rect(9, 2, 0, 1, 1)))
    assert result.current_regions[0].adjacent_region_ids == ()
    result = tracker.update((rect("a", 0, 0, 1, 1, t=1, adjacent=("b", "b", "unknown")), rect("b", 2, 0, 1, 1, t=1)))
    assert result.current_regions[0].persistent_id == 1
    assert result.current_regions[0].adjacent_region_ids == (2,)


def test_adjacency_is_current_snapshot_only_not_retained_track_state():
    tracker = RegionTracker()
    result = tracker.update(
        (
            rect("raw-left", 0, 0, 1, 1, adjacent=("raw-right",)),
            rect("raw-right", 1, 0, 1, 1, adjacent=("raw-left",)),
        )
    )

    assert result.current_regions[0].adjacent_region_ids == (2,)
    assert result.current_regions[1].adjacent_region_ids == (1,)
    # Adjacency is scoped to the published snapshot. Retained tracks do not
    # expose empty or stale adjacency as if it were current.
    assert not hasattr(result.retained_tracks[0], "adjacent_persistent_ids")


@pytest.mark.parametrize(
    "shape",
    ["split", "merge"],
)
def test_split_merge_shaped_observations_have_only_reference_greedy_identity(shape):
    tracker = RegionTracker()
    if shape == "split":
        tracker.update((rect(1, 0, 0, 4, 1),))
        result = tracker.update((rect(2, 0, 0, 2, 1, t=1), rect(3, 2, 0, 2, 1, t=1)))
        # Both children pass area ratio=.5 and bbox-IoU=.5; current index wins
        # the exact score tie, while the unmatched child receives a new ID.
        assert [region.persistent_id for region in result.current_regions] == [1, 2]
    else:
        tracker.update((rect(1, 0, 0, 2, 1), rect(2, 2, 0, 2, 1)))
        result = tracker.update((rect(3, 0, 0, 4, 1, t=1),))
        # One merged observation greedily matches ID 1 on equal score; ID 2 is
        # merely retained as missing. There is no source split/merge lineage rule.
        assert [region.persistent_id for region in result.current_regions] == [1]
        assert {track.persistent_id for track in result.retained_tracks} == {1, 2}
        assert next(t for t in result.retained_tracks if t.persistent_id == 2).miss_count == 1


def test_guard_first_update_empty_unknown_and_count_semantics():
    guard = RegionUpdateGuard()
    empty = guard.evaluate((), free_cells=0, unknown_cells=100, expected_cells=100)
    assert not empty.accepted
    assert empty.reason == "initial_update_below_min_region_count"
    assert not empty.stale
    first = guard.evaluate((rect(1, 0, 0, 2, 2),), free_cells=0, unknown_cells=99, expected_cells=100)
    assert first.accepted  # source applies only minimum-count gate before first valid update


def test_guard_unknown_ratio_strict_boundary_and_empty_output_after_valid():
    guard = RegionUpdateGuard()
    first = guard.evaluate((rect(1, 0, 0, 2, 2),), free_cells=10, unknown_cells=0, expected_cells=100)
    guard.commit(first, ())
    at_limit = guard.evaluate((rect(2, 0, 0, 2, 2),), free_cells=1, unknown_cells=98, expected_cells=100)
    assert at_limit.accepted  # source rejects only `unknown_ratio > threshold`
    too_unknown = guard.evaluate((rect(2, 0, 0, 2, 2),), free_cells=0, unknown_cells=99, expected_cells=100)
    assert "unknown_ratio_too_high" in too_unknown.reasons
    empty = guard.evaluate((), free_cells=0, unknown_cells=0, expected_cells=100)
    assert "zero_regions_after_valid_update" in empty.reasons
    assert "region_area_drop_too_large" in empty.reasons
    assert "region_count_drop_too_large" in empty.reasons


def test_guard_area_drop_boundary_and_both_sides_use_output_polygon_area():
    guard = RegionUpdateGuard()
    base = rect(1, 0, 0, 1, 1, area=1.0)
    first = guard.evaluate((base,), free_cells=80, unknown_cells=0, expected_cells=100)
    guard.commit(first, ())
    threshold = 1.0 - 0.85
    at_boundary = guard.evaluate((rect(2, 0, 0, 1, 1, area=threshold),), free_cells=0, unknown_cells=0, expected_cells=100)
    assert at_boundary.total_region_area_m2 == threshold
    assert at_boundary.accepted
    below = guard.evaluate((rect(3, 0, 0, 1, 1, area=math.nextafter(threshold, 0.0)),), free_cells=100, unknown_cells=0, expected_cells=100)
    assert "region_area_drop_too_large" in below.reasons
    above = guard.evaluate((rect(4, 0, 0, 1, 1, area=math.nextafter(threshold, 1.0)),), free_cells=0, unknown_cells=0, expected_cells=100)
    assert above.accepted  # different free-space area is intentionally irrelevant to this gate


def test_guard_sums_reported_output_region_areas_not_free_space_or_polygon_net_area():
    hole = ((0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5))
    with_hole = rect(1, 0, 0, 2, 2, area=3.5, holes=(hole,))
    no_reported_area = rect(2, 3, 0, 2, 2, area=0.0)
    guard = RegionUpdateGuard()
    decision = guard.evaluate((with_hole, no_reported_area), free_cells=90, unknown_cells=0, expected_cells=100)
    assert decision.total_region_area_m2 == 3.5


def test_guard_count_drop_85_percent_code_semantics_and_integer_sides():
    guard = RegionUpdateGuard()
    initial = tuple(rect(i, i * 3, 0, 1, 1) for i in range(20))
    first = guard.evaluate(initial, free_cells=100, unknown_cells=0, expected_cells=100)
    guard.commit(first, ())
    # Source formula is `count/current < 1 - 0.85`; 3/20 is the nearest
    # representable ratio below the computed 0.15000000000000002 boundary.
    nominal_15pct = guard.evaluate(tuple(rect(i, i * 3, 0, 1, 1) for i in range(3)), free_cells=0, unknown_cells=0, expected_cells=100)
    assert "region_count_drop_too_large" in nominal_15pct.reasons
    just_above_integer = guard.evaluate(tuple(rect(i, i * 3, 0, 1, 1) for i in range(4)), free_cells=0, unknown_cells=0, expected_cells=100)
    assert just_above_integer.accepted
    just_below_integer = guard.evaluate(tuple(rect(i, i * 3, 0, 1, 1) for i in range(2)), free_cells=0, unknown_cells=0, expected_cells=100)
    assert "region_count_drop_too_large" in just_below_integer.reasons


def test_guard_reject_then_accept_does_not_refresh_guard_baseline():
    guard = RegionUpdateGuard()
    first = guard.evaluate((rect(1, 0, 0, 4, 4),), free_cells=1, unknown_cells=0, expected_cells=100)
    guard.commit(first, ())
    reject = guard.evaluate((), free_cells=1, unknown_cells=0, expected_cells=100)
    assert not reject.accepted
    next_valid = guard.evaluate((rect(7, 0, 0, 4, 4, t=3),), free_cells=1, unknown_cells=0, expected_cells=100)
    assert next_valid.accepted
    assert next_valid.total_region_area_m2 == 16


def test_rejected_pipeline_update_publishes_stale_without_aging_or_refreshing_track():
    processor = RegionUpdateProcessor()
    first = processor.update((rect(1, 0, 0, 2, 2, t=1),), free_cells=20, unknown_cells=0, expected_cells=100)
    assert first.accepted and first.current_regions[0].persistent_id == 1
    before = processor.tracker.accepted_update_index
    rejected = processor.update((), free_cells=0, unknown_cells=99, expected_cells=100)
    assert not rejected.accepted
    assert rejected.stale
    assert rejected.current_regions == first.current_regions
    assert processor.tracker.accepted_update_index == before
    assert processor.tracker.snapshot()[0].miss_count == 0
    assert processor.tracker.snapshot()[0].last_seen_s == 1
    # A later valid observation still matches ID 1; the rejected event did not
    # refresh last_seen, age the track, or reset guard comparison baselines.
    recovered = processor.update((rect(9, 0.1, 0, 2, 2, t=4, revision=4),), free_cells=20, unknown_cells=0, expected_cells=100)
    assert recovered.accepted and recovered.current_regions[0].persistent_id == 1
    assert recovered.retained_tracks[0].first_seen_s == 1
    assert recovered.retained_tracks[0].last_seen_s == 4


def test_rejected_empty_before_first_valid_has_no_stale_snapshot():
    processor = RegionUpdateProcessor()
    result = processor.update((), free_cells=0, unknown_cells=100, expected_cells=100)
    assert not result.accepted
    assert not result.stale
    assert result.current_regions == ()
    assert result.retained_tracks == ()


def test_tracker_disabled_passes_raw_identity_without_claiming_persistent_id():
    processor = RegionUpdateProcessor(RegionTrackerConfig(enabled=False))
    result = processor.update((rect("frame-local", 0, 0, 1, 1),), free_cells=1, unknown_cells=0, expected_cells=2)
    assert result.accepted
    assert result.current_regions[0].raw_id == "frame-local"
    assert result.current_regions[0].persistent_id is None
    assert result.retained_tracks == ()
