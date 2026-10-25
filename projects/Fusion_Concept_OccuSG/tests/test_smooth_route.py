from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from integration_v2.smooth_route import (
    BoundedKeyframeWriter,
    KeyframePolicyV1,
    RouteExecutionError,
    build_clearance_preflight_artifacts,
    distance3,
    find_waypoint_polyline,
    resolve_vertical_navmesh_tolerance,
    sample_polyline,
    validate_navmesh_polyline,
    validate_navmesh_polyline_v2,
    validate_navmesh_profile_compatibility,
    yaw_delta_rad,
    yaw_toward_habitat_xz,
)


class ClearPathfinder:
    def __init__(self, *, clearance=1.0, clip=0.0):
        self.clearance = clearance
        self.clip = clip
        self.vertical_tolerances = []

    def is_navigable(self, point, max_y_delta=0.5):
        self.vertical_tolerances.append(max_y_delta)
        return True

    def distance_to_closest_obstacle(self, point, max_search_radius):
        return self.clearance

    def try_step_no_sliding(self, start, end):
        if not self.clip:
            return end
        return (end[0] + self.clip, end[1], end[2])


class FakeShortestPath:
    def __init__(self):
        self.requested_start = None
        self.requested_end = None
        self.points = []
        self.geodesic_distance = 0.0


class SegmentPathfinder:
    def find_path(self, shortest):
        start = tuple(float(v) for v in shortest.requested_start)
        end = tuple(float(v) for v in shortest.requested_end)
        shortest.points = [start, end]
        shortest.geodesic_distance = distance3(start, end)
        return True


class NavSettings:
    agent_radius = 0.1
    agent_height = 1.5


def geometry_contract(*, margin=0.0, epsilon=0.0001, runtime_radius=0.1,
                      baked_radius=0.1, runtime_height=1.5, baked_height=1.5,
                      vertical=0.05):
    return {
        "schema": "habitat-navmesh-clearance-contract/v1",
        "navmesh_baked_agent_radius_m": baked_radius,
        "navmesh_baked_agent_height_m": baked_height,
        "runtime_agent_profile": {"radius_m": runtime_radius, "height_m": runtime_height},
        "profile_compatibility_tolerance_m": 1e-6,
        "extra_navmesh_clearance_margin_m": margin,
        "numeric_boundary_tolerance_m": epsilon,
        "vertical_navmesh_tolerance_m": vertical,
    }


def test_waypoint_polyline_resolves_each_consecutive_segment():
    report = find_waypoint_polyline(
        SegmentPathfinder(), [(0, 0, 0), (1, 0, 0), (1, 0, 1)],
        shortest_path_factory=FakeShortestPath,
        vector_factory=lambda *values: values,
    )
    assert len(report["segments"]) == 2
    assert report["segments"][0]["waypoint_end"] == [1.0, 0.0, 0.0]
    assert report["segments"][1]["waypoint_start"] == [1.0, 0.0, 0.0]
    assert report["points"] == [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 1.0)]


def test_route_sampling_and_clearance_contract():
    points = sample_polyline([(0, 0, 0), (0.1, 0, 0), (0.1, 0, 0.1)], 0.04)
    assert points[0] == (0.0, 0.0, 0.0)
    assert points[-1] == (0.1, 0.0, 0.1)
    assert max(distance3(a, b) for a, b in zip(points, points[1:])) <= 0.0400001
    report = validate_navmesh_polyline(ClearPathfinder(), points,
        max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.025)
    assert report["status"] == "pass"
    assert report["checked_no_sliding_segments"] == len(points) - 1


def test_route_rejects_blocked_or_undersized_footprint():
    path = [(0, 0, 0), (0.1, 0, 0)]
    with pytest.raises(RouteExecutionError, match="clipped"):
        validate_navmesh_polyline(ClearPathfinder(clip=0.1), path,
            max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.02)
    with pytest.raises(RouteExecutionError, match="footprint clearance"):
        validate_navmesh_polyline(ClearPathfinder(clearance=0.11), path,
            max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.02)


def test_route_uses_serialized_navmesh_footprint_only_when_optional_query_is_nonfinite():
    path = [(0, 0, 0), (0.1, 0, 0)]
    for unavailable_value in (float("nan"),):
        report = validate_navmesh_polyline(ClearPathfinder(clearance=unavailable_value), path,
            max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.0,
            navmesh_agent_radius_m=0.1, navmesh_agent_height_m=1.5,
            required_agent_height_m=1.5)
        assert {row["clearance_method"] for row in report["samples"]} == {"serialized_navmesh_agent_footprint"}
    for measured_zero in (0.0, 1.1920928955078125e-7):
        with pytest.raises(RouteExecutionError, match="footprint clearance"):
            validate_navmesh_polyline(ClearPathfinder(clearance=measured_zero), path,
                max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.0,
                navmesh_agent_radius_m=0.1, navmesh_agent_height_m=1.5,
                required_agent_height_m=1.5)
    with pytest.raises(RouteExecutionError, match="serialized navmesh radius"):
        validate_navmesh_polyline(ClearPathfinder(clearance=float("nan")), path,
            max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.0,
            navmesh_agent_radius_m=0.08, navmesh_agent_height_m=1.5,
            required_agent_height_m=1.5)


def test_v2_baked_radius_is_compatibility_only_not_an_extra_clearance_margin():
    pathfinder = ClearPathfinder(clearance=0.00015)
    report = validate_navmesh_polyline_v2(
        pathfinder, [(0, 0, 0), (0.1, 0, 0)], max_step_m=0.04,
        geometry_contract=geometry_contract(), navmesh_settings=NavSettings(),
        endpoint_tolerance_m=0.025, vertical_navmesh_tolerance_m=0.05)
    assert report["status"] == "pass"
    assert report["profile_compatibility"]["navmesh_baked_agent_radius_m"] == pytest.approx(0.1)
    assert report["extra_navmesh_clearance_margin_m"] == 0.0
    assert max(pathfinder.vertical_tolerances) == pytest.approx(0.05)


def test_v2_positive_extra_margin_is_enforced_separately():
    args = dict(max_step_m=0.04, geometry_contract=geometry_contract(margin=0.05),
                navmesh_settings=NavSettings(), endpoint_tolerance_m=0.025,
                vertical_navmesh_tolerance_m=0.05)
    report = validate_navmesh_polyline_v2(
        ClearPathfinder(clearance=0.0502), [(0, 0, 0), (0.1, 0, 0)], **args)
    assert report["min_extra_navmesh_clearance_m"] == pytest.approx(0.0502)
    with pytest.raises(RouteExecutionError, match="extra navmesh clearance failed"):
        validate_navmesh_polyline_v2(
            ClearPathfinder(clearance=0.0498), [(0, 0, 0), (0.1, 0, 0)], **args)
    with pytest.raises(RouteExecutionError, match="numeric boundary tolerance"):
        validate_navmesh_polyline_v2(
            ClearPathfinder(clearance=0.05005), [(0, 0, 0), (0.1, 0, 0)], **args)


def test_v2_actual_validator_output_flows_through_production_preflight_manifest_and_journal():
    report = validate_navmesh_polyline_v2(
        ClearPathfinder(clearance=0.00015), [(0, 0, 0), (0.1, 0, 0)],
        max_step_m=0.04, geometry_contract=geometry_contract(), navmesh_settings=NavSettings(),
        endpoint_tolerance_m=0.025, vertical_navmesh_tolerance_m=0.05)
    assert "clearance_method" not in report["samples"][0]
    assert report["samples"][0]["clearance_status"] == "above_extra_margin_and_boundary_tolerance"
    assert report["samples"][0]["clearance_value_status"] == "measured"

    artifacts = build_clearance_preflight_artifacts(report)
    manifest = artifacts["manifest"]
    journal = artifacts["journal"]
    assert manifest["clearance_validation"] is report
    summary = manifest["clearance_summary"]
    assert summary["clearance_api_counts"] == {"PathFinder.distance_to_closest_obstacle": report["sample_count"]}
    assert summary["validation_status_counts"] == {"above_extra_margin_and_boundary_tolerance": report["sample_count"]}
    assert summary["legacy_clearance_method_counts"] == {}
    assert summary["min_uncapped_navmesh_boundary_distance_m"] == pytest.approx(0.00015)
    assert summary["physical_obstacle_clearance_min_m"] is None
    assert journal["clearance_summary"] == summary
    # Exercise the same strict JSON serialization used by the production journal.
    import json
    json.dumps({"manifest_stage": manifest, "journal_event": journal}, allow_nan=False)


def test_v2_query_capped_value_is_a_lower_bound_not_a_measured_minimum():
    query_cap = geometry_contract()["extra_navmesh_clearance_margin_m"] + 2 * geometry_contract()["numeric_boundary_tolerance_m"]
    # Habitat's binding exposes this query result through float32; its
    # representable cap is slightly below the Python float query radius.
    import struct
    float32_cap = struct.unpack("<f", struct.pack("<f", query_cap))[0]
    report = validate_navmesh_polyline_v2(
        ClearPathfinder(clearance=float32_cap), [(0, 0, 0), (0.1, 0, 0)],
        max_step_m=0.04, geometry_contract=geometry_contract(), navmesh_settings=NavSettings(),
        endpoint_tolerance_m=0.025, vertical_navmesh_tolerance_m=0.05)
    assert report["samples"][0]["clearance_value_status"] == "query_capped_lower_bound"
    artifacts = build_clearance_preflight_artifacts(report)
    summary = artifacts["manifest"]["clearance_summary"]
    assert summary["query_capped_sample_count"] == report["sample_count"]
    assert summary["min_reported_navmesh_boundary_query_value_status"] == "query_capped_lower_bound"
    assert summary["min_uncapped_navmesh_boundary_distance_m"] is None
    assert summary["min_capped_navmesh_boundary_lower_bound_m"] == pytest.approx(query_cap)
    assert summary["physical_obstacle_clearance_min_m"] is None


def test_legacy_clearance_report_is_explicit_and_keeps_method_separate_from_status():
    report = validate_navmesh_polyline(
        ClearPathfinder(clearance=0.14), [(0, 0, 0), (0.1, 0, 0)],
        max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.0)
    assert report["report_schema"] == "habitat-navmesh-clearance-legacy/v1"
    assert report["samples"][0]["clearance_method"] == "PathFinder.distance_to_closest_obstacle"
    assert report["samples"][0]["sample_validation_status"] == "navigable_and_clearance_requirement_passed"
    assert report["samples"][0]["sample_validation_status"] != report["samples"][0]["clearance_method"]
    assert report["samples"][0]["clearance_value_status"] == "measured"
    artifacts = build_clearance_preflight_artifacts(report)
    summary = artifacts["manifest"]["clearance_summary"]
    assert summary["legacy_clearance_method_counts"] == {"PathFinder.distance_to_closest_obstacle": report["sample_count"]}
    assert summary["validation_status_counts"] == {
        "navigable_and_clearance_requirement_passed": report["sample_count"]}
    assert summary["min_uncapped_navmesh_boundary_distance_m"] == pytest.approx(0.14)
    import json
    json.dumps(artifacts, allow_nan=False)


def test_legacy_footprint_fallback_is_not_serialized_as_numeric_clearance():
    report = validate_navmesh_polyline(
        ClearPathfinder(clearance=float("nan")), [(0, 0, 0), (0.1, 0, 0)],
        max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.0,
        navmesh_agent_radius_m=0.1, navmesh_agent_height_m=1.5, required_agent_height_m=1.5)
    assert report["samples"][0]["obstacle_clearance_m"] is None
    artifacts = build_clearance_preflight_artifacts(report)
    summary = artifacts["manifest"]["clearance_summary"]
    assert summary["legacy_clearance_method_counts"] == {"serialized_navmesh_agent_footprint": report["sample_count"]}
    assert summary["uncapped_api_sample_count"] == 0
    assert summary["min_reported_navmesh_boundary_query_value_m"] is None
    import json
    json.dumps(artifacts, allow_nan=False)


def test_clearance_preflight_rejects_missing_fields_and_does_not_swap_method_for_status():
    report = validate_navmesh_polyline_v2(
        ClearPathfinder(clearance=0.00015), [(0, 0, 0), (0.1, 0, 0)],
        max_step_m=0.04, geometry_contract=geometry_contract(), navmesh_settings=NavSettings(),
        endpoint_tolerance_m=0.025, vertical_navmesh_tolerance_m=0.05)
    bad_sample = {**report, "samples": [dict(sample) for sample in report["samples"]]}
    del bad_sample["samples"][0]["clearance_status"]
    with pytest.raises(RouteExecutionError, match="clearance_status"):
        build_clearance_preflight_artifacts(bad_sample)
    bad_api = dict(report)
    del bad_api["clearance_api"]
    with pytest.raises(RouteExecutionError, match="clearance_api"):
        build_clearance_preflight_artifacts(bad_api)
    bad_uncapped = dict(report)
    del bad_uncapped["min_uncapped_extra_navmesh_clearance_m"]
    with pytest.raises(RouteExecutionError, match="min_uncapped_extra_navmesh_clearance_m"):
        build_clearance_preflight_artifacts(bad_uncapped)


def test_legacy_clearance_contract_rejects_implicit_or_incomplete_schema():
    report = validate_navmesh_polyline(
        ClearPathfinder(clearance=0.14), [(0, 0, 0), (0.1, 0, 0)],
        max_step_m=0.04, footprint_radius_m=0.1, clearance_margin_m=0.0)
    inferred_schema = dict(report)
    del inferred_schema["report_schema"]
    with pytest.raises(RouteExecutionError, match="schema is missing or unsupported"):
        build_clearance_preflight_artifacts(inferred_schema)
    missing_api = dict(report)
    del missing_api["clearance_api"]
    with pytest.raises(RouteExecutionError, match="clearance_api"):
        build_clearance_preflight_artifacts(missing_api)


@pytest.mark.parametrize("clearance, message", [
    (0.0, "numeric boundary tolerance"),
    (1e-8, "numeric boundary tolerance"),
    (float("nan"), "unavailable/invalid"),
    (float("inf"), "unavailable/invalid"),
    (-1e-8, "unavailable/invalid"),
])
def test_v2_clearance_zero_invalid_and_boundary_fail_closed(clearance, message):
    with pytest.raises(RouteExecutionError, match=message):
        validate_navmesh_polyline_v2(
            ClearPathfinder(clearance=clearance), [(0, 0, 0), (0.1, 0, 0)],
            max_step_m=0.04, geometry_contract=geometry_contract(),
            navmesh_settings=NavSettings(), endpoint_tolerance_m=0.025,
            vertical_navmesh_tolerance_m=0.05)


@pytest.mark.parametrize("contract", [
    geometry_contract(runtime_radius=0.08),
    geometry_contract(baked_radius=0.08),
    geometry_contract(runtime_height=1.4),
    geometry_contract(baked_height=1.4),
])
def test_v2_rejects_navmesh_runtime_profile_mismatch(contract):
    with pytest.raises(RouteExecutionError, match="incompatible"):
        validate_navmesh_profile_compatibility(NavSettings(), geometry_contract=contract)


def test_v2_preflight_uses_configured_vertical_tolerance_and_rejects_clipping():
    pathfinder = ClearPathfinder(clearance=0.2, clip=0.03)
    with pytest.raises(RouteExecutionError, match="clipped"):
        validate_navmesh_polyline_v2(
            pathfinder, [(0, 0, 0), (0.1, 0, 0)], max_step_m=0.04,
            geometry_contract=geometry_contract(vertical=0.05), navmesh_settings=NavSettings(),
            endpoint_tolerance_m=0.025, vertical_navmesh_tolerance_m=0.05)
    assert pathfinder.vertical_tolerances
    assert set(pathfinder.vertical_tolerances) == {0.05}


def test_v2_vertical_tolerance_has_one_authoritative_policy_value():
    policy = {
        "route_follow": {"vertical_navmesh_tolerance_m": 0.5},
        "geometry_contract": geometry_contract(vertical=0.05),
    }
    assert resolve_vertical_navmesh_tolerance(policy) == pytest.approx(0.05)
    assert resolve_vertical_navmesh_tolerance({
        "route_follow": {"vertical_navmesh_tolerance_m": 0.05}}) == pytest.approx(0.05)


def test_v2_rejects_missing_clearance_api_without_footprint_fallback():
    class NoClearancePathfinder:
        def is_navigable(self, point, max_y_delta):
            return True

        def try_step_no_sliding(self, start, end):
            return end

    with pytest.raises(RouteExecutionError, match="clearance API is unavailable"):
        validate_navmesh_polyline_v2(
            NoClearancePathfinder(), [(0, 0, 0), (0.1, 0, 0)], max_step_m=0.04,
            geometry_contract=geometry_contract(), navmesh_settings=NavSettings(),
            endpoint_tolerance_m=0.025, vertical_navmesh_tolerance_m=0.05)


def test_waypoint_endpoint_mismatch_is_rejected_without_snapping():
    class OffsetPathfinder:
        def find_path(self, shortest):
            start = tuple(float(v) for v in shortest.requested_start)
            end = tuple(float(v) for v in shortest.requested_end)
            shortest.points = [start, (end[0] + 0.04, end[1], end[2])]
            shortest.geodesic_distance = distance3(shortest.points[0], shortest.points[1])
            return True

    with pytest.raises(RouteExecutionError, match="endpoint mismatch"):
        find_waypoint_polyline(
            OffsetPathfinder(), [(0, 0, 0), (1, 0, 0)],
            shortest_path_factory=FakeShortestPath, vector_factory=lambda *values: values,
            endpoint_tolerance_m=0.025)


def test_yaw_wrap_and_habitat_heading():
    assert abs(yaw_delta_rad(3.13, -3.13)) < 0.03
    # Habitat zero faces -Z; west (-X) is positive 90 degrees.
    assert yaw_toward_habitat_xz((0, 0, 0), (-1, 0, 0)) == pytest.approx(1.57079632679)


def test_keyframe_policy_is_causal_and_exact_revisits_do_not_add_candidates():
    policy = KeyframePolicyV1(min_translation_m=0.4, min_yaw_deg=20,
                              min_interval_s=0.5, first_frame=True)
    first = policy.evaluate("f0", (0, 0, 0), 0, 0)
    assert first.candidate and first.reason == "first_valid_frame"
    policy.mark_queue_accepted("f0", (0, 0, 0), 0, 0)
    revisit = policy.evaluate("f1", (0, 0, 0), 0, 1)
    assert not revisit.candidate and revisit.reason == "pose_delta_below_thresholds"
    too_soon = policy.evaluate("f2", (0.5, 0, 0), 0, 0.2)
    assert not too_soon.candidate and too_soon.reason == "below_min_interval"
    translated = policy.evaluate("f3", (0.5, 0, 0), 0, 0.5)
    assert translated.candidate and translated.reason == "translation_threshold"
    rotated = policy.evaluate("f4", (0, 0, 0), 0.5, 0.5)
    assert rotated.candidate and rotated.reason == "yaw_threshold"


def test_bounded_writer_supersedes_pending_and_drains_latest():
    entered = threading.Event()
    release = threading.Event()
    writes = []
    events = []

    def sink(packet):
        if packet["frame_id"] == "f0":
            entered.set()
            assert release.wait(2)
        writes.append(packet["frame_id"])
        return {"path": packet["frame_id"]}

    writer = BoundedKeyframeWriter(sink, events.append)
    writer.submit({"frame_id": "f0"})
    assert entered.wait(2)
    writer.submit({"frame_id": "f1"})
    writer.submit({"frame_id": "f2"})
    with pytest.raises(ValueError, match="duplicate"):
        writer.submit({"frame_id": "f2"})
    release.set()
    writer.close(timeout_s=2)
    assert writes == ["f0", "f2"]
    superseded = [e for e in events if e["event"] == "keyframe_superseded"]
    assert superseded == [{"event": "keyframe_superseded", "frame_id": "f1",
                           "replacement_frame_id": "f2", "reason": "superseded_before_processing"}]
    admitted = [e["frame_id"] for e in events if e["event"] == "keyframe_admitted"]
    assert admitted == ["f0", "f2"]


def test_bounded_writer_propagates_sink_failure():
    def fail(_packet):
        raise OSError("disk full")
    writer = BoundedKeyframeWriter(fail, lambda _event: None)
    writer.submit({"frame_id": "bad"})
    with pytest.raises(RuntimeError, match="failed"):
        writer.close(timeout_s=2)
