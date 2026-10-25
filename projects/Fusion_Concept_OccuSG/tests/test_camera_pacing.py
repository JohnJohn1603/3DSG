from __future__ import annotations

import pytest

from integration_v2.camera_pacing import (
    SimulationClockPacer, actual_pose_speeds, derive_route_budget, motion_increment_for_step,
    simulation_step_record, validate_simulation_elapsed, validate_simulation_step,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_pacer_waits_to_simulation_deadline_only_when_continuing():
    clock = FakeClock()
    pacer = SimulationClockPacer(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    pacer.start(4.0, 10.0)
    clock.now = 10.02
    row = pacer.pace_after_frame(4.1, continuing=True)
    assert row["wait_s"] == pytest.approx(0.08)
    assert row["lag_after_wait_s"] == pytest.approx(0.0)
    assert clock.now == pytest.approx(10.1)
    terminal = pacer.pace_after_frame(4.2, continuing=False)
    assert terminal["wait_s"] == 0.0
    assert len(clock.sleeps) == 1


def test_pacer_records_overrun_without_catchup_sleep():
    clock = FakeClock()
    pacer = SimulationClockPacer(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    pacer.start(0.0, 0.0)
    clock.now = 0.15
    row = pacer.pace_after_frame(0.1, continuing=True)
    assert row["wait_s"] == 0.0
    assert row["lag_before_wait_s"] == pytest.approx(0.05)
    assert pacer.overrun_count == 1
    assert clock.sleeps == []


def test_pacer_rejects_invalid_rtf_and_unstarted_use():
    with pytest.raises(ValueError, match="real_time_factor"):
        SimulationClockPacer(0.0)
    pacer = SimulationClockPacer(1.0)
    with pytest.raises(RuntimeError, match="must be started"):
        pacer.pace_after_frame(0.0, continuing=True)


def test_v4_locked_motion_increment_uses_actual_40ms_simulation_step():
    move_m, turn_deg = motion_increment_for_step(0.2, 30.0, 0.04)
    assert move_m == pytest.approx(0.008)
    assert turn_deg == pytest.approx(1.2)
    assert move_m / 0.04 == pytest.approx(0.2)
    assert turn_deg / 0.04 == pytest.approx(30.0)


def test_simulation_time_accepts_requested_40ms_and_observed_48ms_and_records_deviation():
    assert validate_simulation_step(10.0, 10.04, 0.04) == pytest.approx(0.04)
    assert validate_simulation_step(10.04, 10.088, 0.04) == pytest.approx(0.048)
    row = simulation_step_record(10.04, 10.088, 0.04, cumulative_deviation_before_s=0.0)
    assert row["requested_simulation_dt_s"] == pytest.approx(0.04)
    assert row["observed_simulation_dt_s"] == pytest.approx(0.048)
    assert row["simulation_dt_deviation_s"] == pytest.approx(0.008)
    assert row["cumulative_simulation_dt_deviation_s"] == pytest.approx(0.008)
    second = simulation_step_record(10.088, 10.136, 0.04,
                                    cumulative_deviation_before_s=row["cumulative_simulation_dt_deviation_s"])
    assert second["cumulative_simulation_dt_deviation_s"] == pytest.approx(0.016)
    with pytest.raises(ValueError, match="duplicate/stale"):
        validate_simulation_step(10.04, 10.04, 0.04)
    with pytest.raises(ValueError, match="duplicate/stale"):
        validate_simulation_step(10.04, 10.0, 0.04)
    for bad in (-0.1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            validate_simulation_step(10.0, bad, 0.04)


def test_actual_speed_uses_observed_sim_time_not_nominal_requested_dt():
    measured = actual_pose_speeds((0, 0, 0), (0.008, 0, 0), 0.0, 0.0, 0.048)
    assert measured["actual_planar_speed_mps"] == pytest.approx(0.008 / 0.048)
    turn = actual_pose_speeds((0, 0, 0), (0, 0, 0), 0.0, 0.020943951023931952, 0.048)
    assert turn["actual_angular_speed_deg_per_s"] == pytest.approx(25.0)
    assert motion_increment_for_step(0.2, 30, 0.04) == pytest.approx((0.008, 1.2))
    assert measured["actual_planar_speed_mps"] != pytest.approx(0.2)


def test_operational_elapsed_and_wall_guards_are_independent_of_requested_delta():
    stamps = [0.0]
    for _ in range(3):
        stamps.append(stamps[-1] + 0.048)
    requested_elapsed = 3 * 0.04
    observed_elapsed = stamps[-1] - stamps[0]
    assert observed_elapsed == pytest.approx(0.144)
    assert observed_elapsed - requested_elapsed == pytest.approx(0.024)
    assert validate_simulation_elapsed(observed_elapsed, 60.0) == pytest.approx(0.144)
    with pytest.raises(TimeoutError, match="locked limit"):
        validate_simulation_elapsed(60.048, 60.0)
    with pytest.raises(ValueError):
        validate_simulation_elapsed(float("inf"), 60.0)
    assert 3 < 400  # locked camera frame guard; wall timeout remains independent


def test_absolute_pacer_deadlines_do_not_accumulate_frame_drift():
    clock = FakeClock()
    pacer = SimulationClockPacer(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    pacer.start(0.0, 0.0)
    for frame in range(1, 101):
        clock.now += 0.01  # simulated render/processing work before the wait
        pacer.pace_after_frame(frame * 0.04, continuing=True)
    assert clock.now == pytest.approx(4.0)
    assert pacer.overrun_count == 0


def test_route_frame_and_candidate_budget_comes_from_geometry_and_timebase():
    route = [
        {"position_habitat": [1.05, 0.124, -4.586], "yaw_rad": 1.5707963267948966},
        {"position_habitat": [0.65, 0.124, -4.586], "yaw_rad": 3.141592653589793},
        {"position_habitat": [0.65, 0.124, -4.7], "yaw_rad": 3.141592653589793},
    ]
    budget = derive_route_budget(route, simulation_dt_s=0.04, linear_speed_mps=0.2,
        angular_speed_deg_per_s=30.0, path_sample_step_m=0.04,
        keyframe_min_translation_m=0.4, keyframe_min_yaw_deg=20.0, max_camera_frames=400)
    assert budget["estimated_camera_frames_including_initial"] == 291
    assert budget["conservative_camera_frame_upper_bound"] == 306
    assert budget["keyframe_candidate_upper_bound"] == 306
    assert budget["configured_camera_frame_cap"] == 400
    with pytest.raises(ValueError, match="exceeds configured cap"):
        derive_route_budget(route, simulation_dt_s=0.04, linear_speed_mps=0.2,
            angular_speed_deg_per_s=30.0, path_sample_step_m=0.04,
            keyframe_min_translation_m=0.4, keyframe_min_yaw_deg=20.0, max_camera_frames=300)
