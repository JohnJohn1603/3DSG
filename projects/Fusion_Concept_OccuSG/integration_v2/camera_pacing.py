"""Monotonic wall-clock pacing for simulation-time camera capture."""
from __future__ import annotations

import math
import time
from typing import Callable, Dict, Optional


class SimulationClockPacer:
    """Keep Habitat simulation time aligned with wall time at a fixed RTF.

    ``pace_after_frame`` is called only when another simulation step will run.
    It never waits for a consumer and never adds a terminal post-capture delay.
    If simulation falls behind, it records the overrun and continues without
    catch-up bursts.
    """

    def __init__(self, real_time_factor: float, *, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        factor = float(real_time_factor)
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("real_time_factor must be positive and finite")
        self.real_time_factor = factor
        self._monotonic = monotonic
        self._sleep = sleep
        self._simulation_origin_s: Optional[float] = None
        self._wall_origin_s: Optional[float] = None
        self.total_wait_s = 0.0
        self.overrun_count = 0
        self.max_lag_s = 0.0

    def start(self, simulation_time_s: float, capture_monotonic_s: float) -> None:
        sim = float(simulation_time_s)
        wall = float(capture_monotonic_s)
        if not math.isfinite(sim) or not math.isfinite(wall):
            raise ValueError("pacing origins must be finite")
        if self._simulation_origin_s is not None:
            raise RuntimeError("pacer has already started")
        self._simulation_origin_s = sim
        self._wall_origin_s = wall

    def pace_after_frame(self, simulation_time_s: float, *, continuing: bool) -> Dict[str, float | bool]:
        if self._simulation_origin_s is None or self._wall_origin_s is None:
            raise RuntimeError("pacer must be started from the first captured frame")
        sim = float(simulation_time_s)
        if not math.isfinite(sim) or sim < self._simulation_origin_s:
            raise ValueError("simulation_time_s must be finite and monotonic")
        now = self._monotonic()
        deadline = self._wall_origin_s + (sim - self._simulation_origin_s) / self.real_time_factor
        lag_before_wait = max(0.0, now - deadline)
        wait_s = max(0.0, deadline - now) if continuing else 0.0
        if wait_s:
            self._sleep(wait_s)
            self.total_wait_s += wait_s
        finished = self._monotonic()
        lag_after_wait = max(0.0, finished - deadline)
        if continuing and lag_after_wait > 1e-6:
            self.overrun_count += 1
        self.max_lag_s = max(self.max_lag_s, lag_after_wait)
        return {
            "simulation_time_s": sim,
            "deadline_monotonic_s": deadline,
            "clock_domain": "host_monotonic",
            "continuing": bool(continuing),
            "wait_s": wait_s,
            "lag_before_wait_s": lag_before_wait,
            "lag_after_wait_s": lag_after_wait,
        }


def motion_increment_for_step(linear_speed_mps: float, angular_speed_deg_per_s: float,
                              simulation_dt_s: float) -> tuple[float, float]:
    """Return per-action translation/yaw using the same simulation dt as Habitat.step."""
    values = (float(linear_speed_mps), float(angular_speed_deg_per_s), float(simulation_dt_s))
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("motion speeds and simulation_dt_s must be positive and finite")
    return values[0] * values[2], values[1] * values[2]


def validate_simulation_step(previous_s: Optional[float], current_s: float,
                             expected_dt_s: float, *, tolerance_s: Optional[float] = None) -> float:
    """Validate a source simulation clock step, without treating requested dt as observed dt.

    ``expected_dt_s`` remains required to validate the locked request and is
    reported by callers. Habitat may advance its world clock by a different
    positive amount; that deviation is diagnostic data, not a per-step error.
    Operational limits (simulation-duration, wall timeout, frame budget and
    route/collision checks) are enforced by the route runner.
    ``tolerance_s`` is retained for source compatibility but intentionally has
    no effect: widening a tolerance is not the timebase contract.
    """
    current = float(current_s)
    expected = float(expected_dt_s)
    if not math.isfinite(current) or not math.isfinite(expected) or expected <= 0:
        raise ValueError("simulation clock and expected dt must be finite; dt must be positive")
    if current < 0:
        raise ValueError("simulation timestamp must be non-negative")
    if previous_s is None:
        return 0.0
    previous = float(previous_s)
    if not math.isfinite(previous) or previous < 0:
        raise ValueError("previous simulation timestamp must be finite and non-negative")
    delta = current - previous
    if not math.isfinite(delta) or delta <= 0:
        raise ValueError("simulation timestamp did not advance; duplicate/stale camera observation")
    return delta


def simulation_step_record(previous_s: Optional[float], current_s: float,
                           requested_dt_s: float, *, cumulative_deviation_before_s: float = 0.0) -> dict:
    """Return a validated, auditable requested-versus-observed clock record."""
    requested = float(requested_dt_s)
    cumulative_before = float(cumulative_deviation_before_s)
    if not math.isfinite(cumulative_before):
        raise ValueError("cumulative simulation deviation must be finite")
    observed = validate_simulation_step(previous_s, current_s, requested)
    deviation = 0.0 if previous_s is None else observed - requested
    return {
        "requested_simulation_dt_s": None if previous_s is None else requested,
        "observed_simulation_dt_s": observed,
        "simulation_dt_deviation_s": deviation,
        "cumulative_simulation_dt_deviation_s": cumulative_before + deviation,
        "deviation_policy": "record_only_per_step; stop only on non-finite/non-increasing clock or locked operational guard",
    }


def validate_simulation_elapsed(simulation_elapsed_s: float, max_simulation_elapsed_s: float) -> float:
    """Enforce the pre-locked whole-run simulation-time limit, not a dt tolerance."""
    elapsed = float(simulation_elapsed_s)
    limit = float(max_simulation_elapsed_s)
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("simulation elapsed time must be finite and non-negative")
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("max simulation elapsed time must be positive and finite")
    if elapsed > limit:
        raise TimeoutError(f"observed Habitat simulation elapsed time {elapsed:.6f}s exceeded locked limit {limit:.6f}s")
    return elapsed


def actual_pose_speeds(previous_position, current_position, previous_yaw_rad: float,
                       current_yaw_rad: float, observed_dt_s: float) -> dict:
    """Measure planar translation and wrapped yaw rate over observed simulation time."""
    import math
    before = tuple(float(v) for v in previous_position)
    after = tuple(float(v) for v in current_position)
    dt = float(observed_dt_s)
    if len(before) != 3 or len(after) != 3 or not all(math.isfinite(v) for v in before + after):
        raise ValueError("pose positions must be finite 3D coordinates")
    if not math.isfinite(dt) or dt <= 0:
        raise ValueError("observed_dt_s must be positive and finite")
    yaw_delta = math.atan2(math.sin(float(current_yaw_rad) - float(previous_yaw_rad)),
                           math.cos(float(current_yaw_rad) - float(previous_yaw_rad)))
    if not math.isfinite(yaw_delta):
        raise ValueError("yaw values must be finite")
    distance = math.hypot(after[0] - before[0], after[2] - before[2])
    return {
        "actual_planar_speed_mps": distance / dt,
        "actual_angular_speed_deg_per_s": abs(math.degrees(yaw_delta)) / dt,
        "actual_pose_delta_xz_m": distance,
        "actual_wrapped_yaw_delta_deg": math.degrees(yaw_delta),
        "measurement_timebase": "observed_habitat_simulation_timestamp_delta",
    }


def derive_route_budget(route_points, *, simulation_dt_s: float, linear_speed_mps: float,
                        angular_speed_deg_per_s: float, path_sample_step_m: float,
                        keyframe_min_translation_m: float, keyframe_min_yaw_deg: float,
                        max_camera_frames: int) -> dict:
    """Estimate route-frame budget from locked geometry/timebase, without predictor data.

    The conservative upper bound includes one extra movement step per sampled
    path target and one per non-zero leg for alignment/tolerance. Runtime also
    records the exact navmesh polyline and measured frame count.
    """
    points = [tuple(float(v) for v in row["position_habitat"]) for row in route_points]
    if len(points) < 2 or any(len(point) != 3 or not all(math.isfinite(v) for v in point) for point in points):
        raise ValueError("route budget requires at least two finite 3D waypoints")
    dt = float(simulation_dt_s)
    move_step, turn_step = motion_increment_for_step(linear_speed_mps, angular_speed_deg_per_s, dt)
    path_sample = float(path_sample_step_m)
    trans_threshold = float(keyframe_min_translation_m)
    yaw_threshold = float(keyframe_min_yaw_deg)
    if min(path_sample, trans_threshold, yaw_threshold) <= 0:
        raise ValueError("route and keyframe distances/angles must be positive")

    lengths = []
    headings = []
    for start, end in zip(points, points[1:]):
        dx, dz = end[0] - start[0], end[2] - start[2]
        length = math.hypot(dx, dz)
        if length <= 1e-9:
            continue
        lengths.append(length)
        headings.append(math.atan2(-dx, -dz))
    if not lengths:
        raise ValueError("route budget has no horizontal movement")
    angle_delta = lambda a, b: (b - a + math.pi) % (2 * math.pi) - math.pi
    initial_yaw = float(route_points[0]["yaw_rad"])
    final_yaw = float(route_points[-1]["yaw_rad"])
    yaw_total_rad = abs(angle_delta(initial_yaw, headings[0]))
    yaw_total_rad += sum(abs(angle_delta(a, b)) for a, b in zip(headings, headings[1:]))
    yaw_total_rad += abs(angle_delta(headings[-1], final_yaw))
    distance = sum(lengths)
    move_steps_min = math.ceil(distance / move_step)
    turn_steps_min = math.ceil(math.degrees(yaw_total_rad) / turn_step)
    sample_targets = sum(math.ceil(length / path_sample) for length in lengths)
    conservative_steps = move_steps_min + turn_steps_min + sample_targets + len(lengths)
    estimated_frames = 1 + move_steps_min + turn_steps_min
    frame_upper = 1 + conservative_steps
    cap = int(max_camera_frames)
    if cap < 1 or frame_upper > cap:
        raise ValueError(f"derived route frame upper bound {frame_upper} exceeds configured cap {cap}")
    candidate_upper = min(frame_upper, cap)
    return {
        "schema": "camera-live-route-budget/v1",
        "basis": "locked waypoint geometry, Habitat simulation dt, configured linear/angular speeds, keyframe policy; no predictor outputs",
        "route_horizontal_distance_m": distance,
        "absolute_turn_angle_deg": math.degrees(yaw_total_rad),
        "simulation_dt_s": dt,
        "camera_sampling_hz_simulation": 1.0 / dt,
        "linear_speed_mps": float(linear_speed_mps),
        "angular_speed_deg_per_s": float(angular_speed_deg_per_s),
        "per_step_displacement_m": move_step,
        "per_step_yaw_deg": turn_step,
        "minimum_translation_steps": move_steps_min,
        "minimum_turn_steps": turn_steps_min,
        "estimated_camera_frames_including_initial": estimated_frames,
        "conservative_camera_frame_upper_bound": frame_upper,
        "keyframe_candidate_upper_bound": candidate_upper,
        "configured_camera_frame_cap": cap,
        "max_candidate_count_basis": "one candidate per camera frame is the hard upper bound; actual OR-policy selection is measured in the run",
        "sampled_route_targets_upper_bound": sample_targets,
        "keyframe_policy": {
            "min_translation_m": trans_threshold,
            "min_yaw_deg": yaw_threshold,
            "policy": "pose_delta_or_v1",
        },
    }
