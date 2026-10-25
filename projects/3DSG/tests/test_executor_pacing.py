from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from exploration.contracts import ActionCommand
from exploration.habitat_factory import HabitatIncrementalExecutor


class _Simulator:
    def __init__(self, *, blocked: bool = False) -> None:
        self.position = np.zeros(3)
        self.steps: list[tuple[str, float]] = []
        self.preflight_calls = 0
        self.navigability_calls = 0
        self.blocked = blocked
        self.pathfinder = SimpleNamespace(try_step_no_sliding=self._try_step,
                                          is_navigable=self._is_navigable)

    def _try_step(self, start, end):
        self.preflight_calls += 1
        return start if self.blocked else end

    def _is_navigable(self, _position, *, max_y_delta):
        assert max_y_delta == 0.05
        self.navigability_calls += 1
        return True

    def get_agent(self, index):
        assert index == 0
        return SimpleNamespace(get_state=lambda: SimpleNamespace(
            position=tuple(self.position), rotation="identity"))

    def step(self, action, *, dt):
        self.steps.append((action, dt))
        if action == "move_forward":
            self.position[2] -= 0.02
        return {"sensor": "observation"}


def _executor() -> HabitatIncrementalExecutor:
    return HabitatIncrementalExecutor(
        step_s=0.04, move_increment_m=0.02, turn_increment_deg=6.0,
        vector_factory=tuple,
        quaternion_module=SimpleNamespace(as_rotation_matrix=lambda _: np.eye(3)),
    )


def _command(*, linear: float = 0.15, yaw: float = 0.0,
             metadata=None) -> ActionCommand:
    return ActionCommand(linear_mps=linear, yaw_rate_radps=yaw, reason="tracking",
                         frame_id="frame", map_revision=1,
                         valid_until_monotonic_s=100.0,
                         metadata={} if metadata is None else metadata)


def test_explicit_command_interval_emits_translation_pulse_earlier() -> None:
    executor, simulator = _executor(), _Simulator()
    command = _command(metadata={"execution_dt_s": 0.1})

    results = [executor(simulator, command)["_frontier_action"] for _ in range(2)]

    assert results == ["stop", "move_forward"]
    assert simulator.steps == [(action, 0.04) for action in results]
    assert simulator.position[2] == pytest.approx(-0.02)
    assert simulator.preflight_calls == 1 and simulator.navigability_calls == 2


def test_missing_interval_retains_fixed_step_pacing() -> None:
    executor, simulator = _executor(), _Simulator()

    results = [executor(simulator, _command())["_frontier_action"] for _ in range(4)]

    assert results == ["stop", "stop", "stop", "move_forward"]
    assert all(dt == 0.04 for _action, dt in simulator.steps)


def test_explicit_interval_also_accumulates_yaw_earlier() -> None:
    command = _command(linear=0.0, yaw=math.radians(40.0),
                       metadata={"execution_dt_s": 0.1})
    executor, simulator = _executor(), _Simulator()
    assert [executor(simulator, command)["_frontier_action"] for _ in range(2)] == [
        "stop", "turn_left"]
    assert simulator.steps == [("stop", 0.04), ("turn_left", 0.04)]


@pytest.mark.parametrize("interval, expected", [(-1.0, "stop"), (0.0, "stop"),
                                                 (100.0, "move_forward")])
def test_interval_is_clamped_without_increasing_action_size(interval, expected) -> None:
    executor, simulator = _executor(), _Simulator()

    result = executor(simulator, _command(linear=0.2, metadata={"execution_dt_s": interval}))

    assert result["_frontier_action"] == expected
    assert simulator.steps == [(expected, 0.04)]
    assert simulator.position[2] == pytest.approx(-0.02 if expected == "move_forward" else 0.0)


@pytest.mark.parametrize("interval", [math.nan, math.inf, -math.inf, "invalid", None])
def test_invalid_interval_is_rejected_before_simulator_or_accumulator_changes(interval) -> None:
    executor, simulator = _executor(), _Simulator()
    executor._linear_remainder_m = 0.005

    with pytest.raises(ValueError, match="execution_dt_s must be finite"):
        executor(simulator, _command(metadata={"execution_dt_s": interval}))

    assert simulator.steps == []
    assert executor._linear_remainder_m == 0.005


@pytest.mark.parametrize("sign, action", [(1, "turn_left"), (-1, "turn_right")])
def test_both_yaw_directions_drop_excess_instead_of_leaving_a_turn_backlog(sign, action) -> None:
    executor, simulator = _executor(), _Simulator()
    large = _command(linear=10.0, yaw=sign * 20.0, metadata={"execution_dt_s": 0.1})

    result = executor(simulator, large)

    assert result["_frontier_action"] == action
    assert simulator.steps == [(action, 0.04)]
    assert executor._yaw_remainder_rad == pytest.approx(0.0)
    assert 0 <= executor._linear_remainder_m <= executor.move_increment_m
    # A subsequent small yaw request cannot consume a backlog from the large
    # command. Zero linear velocity also cancels the one pending move pulse.
    small = _command(linear=0.0, yaw=sign * 0.01, metadata={"execution_dt_s": 0.1})
    assert executor(simulator, small)["_frontier_action"] == "stop"
    assert abs(executor._yaw_remainder_rad) < executor.turn_increment_rad
    assert executor(simulator, _command(linear=0.0))["_frontier_action"] == "stop"
    assert executor._linear_remainder_m == executor._yaw_remainder_rad == 0.0


def test_large_translation_requests_still_make_one_small_move_per_observation() -> None:
    executor, simulator = _executor(), _Simulator()
    command = _command(linear=10.0, metadata={"execution_dt_s": 0.1})

    for index in range(3):
        assert executor(simulator, command)["_frontier_action"] == "move_forward"
        assert len(simulator.steps) == index + 1
        assert simulator.position[2] == pytest.approx(-0.02 * (index + 1))
        assert executor._linear_remainder_m == 0.0


def test_longer_interval_still_applies_navmesh_preflight_filter() -> None:
    executor, simulator = _executor(), _Simulator(blocked=True)

    result = executor(simulator, _command(linear=0.2, metadata={"execution_dt_s": 0.1}))

    assert result["_frontier_action"] == "move_blocked"
    assert result["collided"] is True
    assert result["_collision_filter_details"]["reason"] == ["filtered_step_displacement_invalid"]
    assert simulator.steps == [("stop", 0.04)]
    assert simulator.preflight_calls == simulator.navigability_calls == 1
    assert np.array_equal(simulator.position, np.zeros(3))
    assert executor._linear_remainder_m == 0.0
