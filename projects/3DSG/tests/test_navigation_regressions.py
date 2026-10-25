from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import threading
import time

import numpy as np

from exploration.contracts import (
    ActionCommand,
    CameraIntrinsics,
    FramePacket,
    GridSnapshot,
    GridState,
    PathResult,
    Pose3,
    RobotPose2,
)
from exploration.depth_safety import DepthSafetyConfig, DepthSafetyGate
from exploration.habitat_factory import (
    HabitatIncrementalExecutor, _actual_step_displacement_valid, load_scene_config,
)
from exploration.habitat_online import HabitatOnlineProducer
from exploration.map_worker import LatestMapWorker
from exploration.planner import AStarPlanner, PlannerConfig
from exploration.plan_worker import PlanOutcome
from exploration.runtime_profile import (
    _parse_yaml_mapping_subset, keyframe_runtime_settings, load_runtime_profile,
)
from exploration.controller import Controller, ControllerConfig
from exploration.frontier import (
    Frontier, FrontierExtractor, FrontierSet, _best_reachable_viewpoint,
    _flood, _neighbors, _view_offsets,
)
from exploration.nav_mapper import NavMapper, NavMapperConfig
from exploration.policy import FrontierPolicy, NavigationDecision, PolicyConfig
from exploration.runtime import (NavigationRuntime, NavigationRuntimeConfig,
                                  _blocked_step_edge, _next_path_edge)


ROOT = Path(__file__).resolve().parents[1]


def test_real_habitat_scene_config_loads_hash_locked_navmesh() -> None:
    config = load_scene_config(ROOT / "configs/exploration/hm3d_00802_cpu.json")
    assert config["scene_id"] == "00802-wcojb4TFT35"


def test_scene_config_accepts_explicit_canonical_floor_reference(tmp_path) -> None:
    import json
    source = ROOT / "configs/exploration/hm3d_00802_split_depth_fast_motion.json"
    config = json.loads(source.read_text())
    config["floor_z_m"] = 0.011
    target = tmp_path / "scene.json"
    target.write_text(json.dumps(config))
    loaded = load_scene_config(target)
    assert loaded["floor_z_m"] == 0.011
    assert Path(config["navmesh_file"]).is_file()
    assert "route" not in config and "route_points" not in config


class _Pathfinder:
    is_loaded = True
    nav_mesh_settings = type("Settings", (), {"agent_radius": 0.1, "agent_height": 1.5})()

    @staticmethod
    def is_navigable(_position):
        return True


class _Agent:
    def set_state(self, _state, infer_sensor_states=True):
        assert infer_sensor_states


class _Simulator:
    pathfinder = _Pathfinder()

    def __init__(self):
        self.sim_time = 0.0

    def get_world_time(self):
        self.sim_time += 0.04
        return self.sim_time

    @staticmethod
    def get_sensor_observations():
        return {
            "color_sensor": np.zeros((2, 3, 3), dtype=np.uint8),
            "depth_sensor": np.full((2, 3), 2.0, dtype=np.float32),
            "nav_depth_sensor": np.full((1, 2), 1.0, dtype=np.float32),
        }


def test_habitat_producer_advances_private_frame_index() -> None:
    sim, agent = _Simulator(), _Agent()
    producer = HabitatOnlineProducer(
        simulator=sim, agent=agent, run_id="fixture", scene_id="scene",
        calibration_hash="cal", intrinsics=CameraIntrinsics(2, 2, 1, 0.5, 3, 2),
        pose_provider=lambda _agent: np.eye(4),
        robot_pose_provider=lambda _agent: RobotPose2(0, 0, 0),
        action_executor=lambda _sim, _cmd: _sim.get_sensor_observations(),
        habitat_vector_factory=lambda p: p,
        depth_min_m=0.1, depth_max_m=10, floor_z_m=0,
        geometry_contract={"navmesh_baked_agent_radius_m": 0.1,
                           "navmesh_baked_agent_height_m": 1.5},
    )
    producer.initialize_spawn(type("Spawn", (), {"position": np.zeros(3)})())
    first = producer.observe()
    second = producer.observe()
    assert first.packet.frame_index == 0
    assert second.packet.frame_index == 1
    assert producer.frame_index == 2


def test_habitat_producer_throttles_supplemental_navigation_depth() -> None:
    sim, agent = _Simulator(), _Agent()
    producer = HabitatOnlineProducer(
        simulator=sim, agent=agent, run_id="dual-fixture", scene_id="scene",
        calibration_hash="cal", intrinsics=CameraIntrinsics(2, 2, 1, 0.5, 3, 2),
        pose_provider=lambda _agent: np.eye(4),
        robot_pose_provider=lambda _agent: RobotPose2(0, 0, 0),
        nav_depth_pose_provider=lambda _agent: np.eye(4),
        nav_depth_intrinsics=CameraIntrinsics(2, 2, 0.5, 0, 2, 1),
        nav_depth_min_m=0.1, nav_depth_max_m=4.0,
        nav_depth_update_period_frames=2,
        action_executor=lambda _sim, _cmd: _sim.get_sensor_observations(),
        habitat_vector_factory=lambda p: p,
        depth_min_m=0.1, depth_max_m=10, floor_z_m=0,
        geometry_contract={"navmesh_baked_agent_radius_m": 0.1,
                           "navmesh_baked_agent_height_m": 1.5},
    )
    producer.initialize_spawn(type("Spawn", (), {"position": np.zeros(3)})())
    assert producer.observe().packet.nav_depth_m is not None
    assert producer.observe().packet.nav_depth_m is None
    assert producer.observe().packet.nav_depth_m is not None


def test_habitat_movement_filter_reports_expected_and_filtered_endpoints() -> None:
    class Agent:
        @staticmethod
        def get_state():
            return type("State", (), {"position": (0.0, 0.0, 0.0),
                                       "rotation": "identity"})()

    class Pathfinder:
        @staticmethod
        def try_step_no_sliding(_start, end):
            return (0.0, 0.0, 0.0)

        @staticmethod
        def is_navigable(_position, max_y_delta):
            assert max_y_delta == 0.05
            return True

    class Simulator:
        pathfinder = Pathfinder()

        @staticmethod
        def get_agent(_index):
            return Agent()

        @staticmethod
        def step(action, dt):
            assert action == "stop" and dt == 0.04
            return {"color_sensor": np.zeros((1, 1, 3), dtype=np.uint8)}

    quaternion = type("Quaternion", (), {
        "as_rotation_matrix": staticmethod(lambda _rotation: np.eye(3))
    })
    executor = HabitatIncrementalExecutor(
        step_s=0.04, move_increment_m=0.008, turn_increment_deg=1.2,
        vector_factory=tuple, quaternion_module=quaternion,
    )
    result = executor(Simulator(), ActionCommand(
        linear_mps=0.2, yaw_rate_radps=0.0, reason="fixture", frame_id="f0",
        map_revision=1, valid_until_monotonic_s=100.0,
    ))

    details = result["_collision_filter_details"]
    assert result["collided"] is True
    assert details["reason"] == ["filtered_step_displacement_invalid"]
    assert details["expected_endpoint_habitat_m"] == [0.0, 0.0, -0.008]
    assert details["filtered_endpoint_delta_m"] == 0.008


def test_habitat_movement_filter_accepts_safe_forward_projection() -> None:
    class Agent:
        position = [0.0, 0.0, 0.0]

        @classmethod
        def get_state(cls):
            return type("State", (), {"position": tuple(cls.position),
                                       "rotation": "identity"})()

    class Pathfinder:
        @staticmethod
        def try_step_no_sliding(_start, _end):
            return (0.0, 0.0, -0.006)

        @staticmethod
        def is_navigable(_position, max_y_delta):
            assert max_y_delta == 0.05
            return True

    class Simulator:
        pathfinder = Pathfinder()

        @staticmethod
        def get_agent(_index):
            return Agent()

        @staticmethod
        def step(action, dt):
            assert action == "move_forward" and dt == 0.04
            Agent.position = [0.0, 0.0, -0.006]
            return {"color_sensor": np.zeros((1, 1, 3), dtype=np.uint8)}

    quaternion = type("Quaternion", (), {
        "as_rotation_matrix": staticmethod(lambda _rotation: np.eye(3))
    })
    executor = HabitatIncrementalExecutor(
        step_s=0.04, move_increment_m=0.008, turn_increment_deg=1.2,
        vector_factory=tuple, quaternion_module=quaternion,
    )
    result = executor(Simulator(), ActionCommand(
        linear_mps=0.2, yaw_rate_radps=0.0, reason="fixture", frame_id="f0",
        map_revision=1, valid_until_monotonic_s=100.0,
    ))

    assert result["_frontier_action"] == "move_forward"
    assert not result.get("collided", False)
    assert np.isclose(result["_navmesh_projection_details"]["forward_progress_m"], 0.006)
    assert result["_navmesh_projection_details"]["lateral_error_m"] == 0.0


def test_habitat_movement_filter_rejects_projection_below_half_step() -> None:
    class Agent:
        @staticmethod
        def get_state():
            return type("State", (), {"position": (0.0, 0.0, 0.0),
                                       "rotation": "identity"})()

    class Pathfinder:
        @staticmethod
        def try_step_no_sliding(_start, _end):
            return (0.0, 0.0, -0.003)

        @staticmethod
        def is_navigable(_position, max_y_delta):
            assert max_y_delta == 0.05
            return True

    class Simulator:
        pathfinder = Pathfinder()

        @staticmethod
        def get_agent(_index):
            return Agent()

        @staticmethod
        def step(action, dt):
            assert action == "stop" and dt == 0.04
            return {"color_sensor": np.zeros((1, 1, 3), dtype=np.uint8)}

    quaternion = type("Quaternion", (), {
        "as_rotation_matrix": staticmethod(lambda _rotation: np.eye(3))
    })
    executor = HabitatIncrementalExecutor(
        step_s=0.04, move_increment_m=0.008, turn_increment_deg=1.2,
        vector_factory=tuple, quaternion_module=quaternion,
    )
    result = executor(Simulator(), ActionCommand(
        linear_mps=0.2, yaw_rate_radps=0.0, reason="fixture", frame_id="f0",
        map_revision=1, valid_until_monotonic_s=100.0,
    ))

    assert result["_frontier_action"] == "move_blocked"
    assert result["_collision_filter_details"]["reason"] == [
        "filtered_step_displacement_invalid"
    ]


def test_habitat_movement_filter_rejects_lateral_projection() -> None:
    class Agent:
        @staticmethod
        def get_state():
            return type("State", (), {"position": (0.0, 0.0, 0.0),
                                       "rotation": "identity"})()

    class Pathfinder:
        @staticmethod
        def try_step_no_sliding(_start, _end):
            return (0.0015, 0.0, -0.008)

        @staticmethod
        def is_navigable(_position, max_y_delta):
            assert max_y_delta == 0.05
            return True

    class Simulator:
        pathfinder = Pathfinder()

        @staticmethod
        def get_agent(_index):
            return Agent()

        @staticmethod
        def step(action, dt):
            assert action == "stop" and dt == 0.04
            return {"color_sensor": np.zeros((1, 1, 3), dtype=np.uint8)}

    quaternion = type("Quaternion", (), {
        "as_rotation_matrix": staticmethod(lambda _rotation: np.eye(3))
    })
    executor = HabitatIncrementalExecutor(
        step_s=0.04, move_increment_m=0.008, turn_increment_deg=1.2,
        vector_factory=tuple, quaternion_module=quaternion,
    )
    result = executor(Simulator(), ActionCommand(
        linear_mps=0.2, yaw_rate_radps=0.0, reason="fixture", frame_id="f0",
        map_revision=1, valid_until_monotonic_s=100.0,
    ))

    assert result["_frontier_action"] == "move_blocked"
    assert result["_collision_filter_details"]["reason"] == [
        "filtered_step_displacement_invalid"
    ]


def test_habitat_movement_filter_accepts_navigable_vertical_projection() -> None:
    class Agent:
        position = [0.0, 0.0, 0.0]

        @classmethod
        def get_state(cls):
            return type("State", (), {"position": tuple(cls.position),
                                       "rotation": "identity"})()

    class Pathfinder:
        @staticmethod
        def try_step_no_sliding(_start, end):
            return (end[0], end[1] + 0.0015, end[2])

        @staticmethod
        def is_navigable(_position, max_y_delta):
            assert max_y_delta == 0.05
            return True

    class Simulator:
        pathfinder = Pathfinder()

        @staticmethod
        def get_agent(_index):
            return Agent()

        @staticmethod
        def step(action, dt):
            assert action == "move_forward" and dt == 0.04
            Agent.position = [0.0, 0.0, -0.008]
            return {"color_sensor": np.zeros((1, 1, 3), dtype=np.uint8)}

    quaternion = type("Quaternion", (), {
        "as_rotation_matrix": staticmethod(lambda _rotation: np.eye(3))
    })
    executor = HabitatIncrementalExecutor(
        step_s=0.04, move_increment_m=0.008, turn_increment_deg=1.2,
        vector_factory=tuple, quaternion_module=quaternion,
    )
    result = executor(Simulator(), ActionCommand(
        linear_mps=0.2, yaw_rate_radps=0.0, reason="fixture", frame_id="f0",
        map_revision=1, valid_until_monotonic_s=100.0,
    ))

    assert result["_frontier_action"] == "move_forward"
    assert not result.get("collided", False)


def test_habitat_step_tolerance_separates_horizontal_motion_and_navmesh_height() -> None:
    assert _actual_step_displacement_valid(
        np.asarray([0.008, 0.0132, 0.0]), 0.008, 0.05)
    assert not _actual_step_displacement_valid(
        np.asarray([0.0132, 0.0, 0.0]), 0.008, 0.05)
    assert not _actual_step_displacement_valid(
        np.asarray([0.008, 0.0501, 0.0]), 0.008, 0.05)
    # A half-step produced by NavMesh projection can land a few ulps below
    # the mathematical boundary after rotation and coordinate conversion.
    assert _actual_step_displacement_valid(
        np.asarray([0.0, 0.0, -0.0099999999999999]), 0.02, 0.05)
    assert not _actual_step_displacement_valid(
        np.asarray([0.0, 0.0, -0.009998]), 0.02, 0.05)


def _depth_gate(depth: np.ndarray, *, fx: float = 100.0, fy: float = 100.0) -> object:
    height, width = depth.shape
    # Optical +Z points along robot +X; image-down points toward world -Z.
    rotation = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = (0.0, 0.0, 1.5)
    gate = DepthSafetyGate(DepthSafetyConfig(
        corridor_half_width_m=0.25, floor_z_m=0.0, agent_height_m=1.5,
        max_check_distance_m=1.0, min_valid_returns=3, pixel_stride=1,
    ))
    return gate.evaluate(
        frame_id="depth-fixture", host_monotonic_s=1.0, depth_m=depth,
        intrinsics=CameraIntrinsics(fx, fy, (width - 1) / 2, (height - 1) / 2, width, height),
        T_world_camera=Pose3(transform), robot_xy=(0.0, 0.0), robot_yaw_rad=0.0,
        depth_min_m=0.1, depth_max_m=10.0,
    )


def test_depth_gate_counts_far_wall_rays_as_free_corridor_coverage() -> None:
    result = _depth_gate(np.full((7, 7), 3.0, dtype=np.float32))
    assert result.status == "observed"
    assert result.valid_corridor_returns >= 3
    assert result.minimum_obstacle_distance_m is None


def test_depth_gate_near_wall_is_obstacle_evidence() -> None:
    result = _depth_gate(np.full((7, 7), 0.5, dtype=np.float32))
    assert result.status == "observed"
    assert result.minimum_obstacle_distance_m is not None
    assert abs(result.minimum_obstacle_distance_m - 0.5) < 0.02


def test_depth_gate_rejects_empty_and_floor_only_not_as_obstacles() -> None:
    empty = _depth_gate(np.full((7, 7), np.nan, dtype=np.float32))
    assert empty.status == "insufficient_depth"
    assert empty.minimum_obstacle_distance_m is None

    floor_depth = np.full((7, 7), np.nan, dtype=np.float32)
    fy = 10.0
    for row in (5, 6):
        floor_depth[row, :] = 1.5 * fy / (row - 3)
    floor = _depth_gate(floor_depth, fx=10.0, fy=fy)
    assert floor.status == "observed"
    assert floor.minimum_obstacle_distance_m is None


def test_floor_return_projects_verified_clear_corridor_into_navigation_grid() -> None:
    # Optical forward points 30 degrees below horizontal and lands on the
    # same-level floor at 2.6 m, with no obstacle in the ray.
    rotation = np.array([[0.0, 0.5, np.sqrt(3.0) / 2.0],
                         [1.0, 0.0, 0.0],
                         [0.0, np.sqrt(3.0) / 2.0, -0.5]])
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = (0.0, 0.0, 1.5)
    packet = FramePacket(
        run_id="floor-ray-fixture", scene_id="scene", frame_id="floor0", frame_index=0,
        simulator_time_s=0.04, host_monotonic_s=1.0,
        depth_m=np.array([[3.0]], dtype=np.float32),
        intrinsics=CameraIntrinsics(1.0, 1.0, 0.0, 0.0, 1, 1),
        T_world_camera=Pose3(transform), floor_z_m=0.0,
        calibration_hash="cal", depth_min_m=0.1, depth_max_m=10.0,
    )
    mapper = NavMapper(NavMapperConfig(resolution_m=0.05, depth_stride=1,
                                       agent_height_m=1.5), agent_radius_m=0.1)
    mapper.initialize_spawn(0.0, 0.0)
    snapshot = mapper.update(packet)
    # The ray is about 0.92 m above the floor at x=1 m, so it may pass over a
    # low obstacle there. Its distant floor hit is support only at the endpoint.
    low_obstacle_cell = snapshot.world_to_cell(1.0, 0.0)
    endpoint_cell = snapshot.world_to_cell(3.0 * np.sqrt(3.0) / 2.0, 0.0)
    assert low_obstacle_cell is not None and endpoint_cell is not None
    assert snapshot.floor_support[low_obstacle_cell] == 0
    assert snapshot.states[low_obstacle_cell] != GridState.FREE
    assert snapshot.floor_support[endpoint_cell] == 1
    assert snapshot.states[endpoint_cell] == GridState.FREE


def test_navigation_depth_sensor_is_integrated_with_its_own_pose() -> None:
    # The RGB-aligned depth view is empty here; the supplementary sensor sees
    # floor returns from its own downward-facing optical frame.
    rotation = np.array([[0.0, 0.5, np.sqrt(3.0) / 2.0],
                         [1.0, 0.0, 0.0],
                         [0.0, np.sqrt(3.0) / 2.0, -0.5]])
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = (0.0, 0.0, 1.5)
    packet = FramePacket(
        run_id="dual-depth-fixture", scene_id="scene", frame_id="frame0", frame_index=0,
        simulator_time_s=0.04, host_monotonic_s=1.0,
        depth_m=np.full((1, 1), np.nan, dtype=np.float32),
        intrinsics=CameraIntrinsics(1.0, 1.0, 0.0, 0.0, 1, 1),
        T_world_camera=Pose3(np.eye(4)), floor_z_m=0.0,
        calibration_hash="cal", depth_min_m=0.1, depth_max_m=10.0,
        nav_depth_m=np.array([[3.0]], dtype=np.float32),
        nav_depth_intrinsics=CameraIntrinsics(1.0, 1.0, 0.0, 0.0, 1, 1),
        T_world_nav_depth_camera=Pose3(transform),
        nav_depth_min_m=0.1, nav_depth_max_m=10.0,
    )
    mapper = NavMapper(NavMapperConfig(resolution_m=0.05, depth_stride=1,
                                       agent_height_m=1.5), agent_radius_m=0.1)
    mapper.initialize_spawn(0.0, 0.0)
    snapshot = mapper.update(packet)
    low_obstacle_cell = snapshot.world_to_cell(1.0, 0.0)
    endpoint_cell = snapshot.world_to_cell(3.0 * np.sqrt(3.0) / 2.0, 0.0)
    assert low_obstacle_cell is not None and endpoint_cell is not None
    assert snapshot.floor_support[low_obstacle_cell] == 0
    assert snapshot.states[low_obstacle_cell] != GridState.FREE
    assert snapshot.floor_support[endpoint_cell] == 1
    assert snapshot.states[endpoint_cell] == GridState.FREE


def test_aggregated_overlapping_floor_rays_preserve_evidence_counts() -> None:
    rotation = np.array([[0.0, 0.5, np.sqrt(3.0) / 2.0],
                         [1.0, 0.0, 0.0],
                         [0.0, np.sqrt(3.0) / 2.0, -0.5]])
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = (0.0, 0.0, 1.5)
    packet = FramePacket(
        run_id="overlap-rays", scene_id="scene", frame_id="floor0", frame_index=0,
        simulator_time_s=0.04, host_monotonic_s=1.0,
        depth_m=np.full((1, 5), 3.0, dtype=np.float32),
        intrinsics=CameraIntrinsics(10_000.0, 10_000.0, 2.0, 0.0, 5, 1),
        T_world_camera=Pose3(transform), floor_z_m=0.0,
        calibration_hash="cal", depth_min_m=0.1, depth_max_m=10.0,
    )
    mapper = NavMapper(NavMapperConfig(resolution_m=0.05, depth_stride=1,
                                       agent_height_m=1.5), agent_radius_m=0.1)
    mapper.initialize_spawn(0.0, 0.0)
    snapshot = mapper.update(packet)
    path_cell = snapshot.world_to_cell(1.0, 0.0)
    endpoint_cell = snapshot.world_to_cell(3.0 * np.sqrt(3.0) / 2.0, 0.0)
    assert path_cell is not None and endpoint_cell is not None
    # Five near-coincident rays may clear their height band at x=1m, but only
    # their actual floor endpoint establishes support there.
    assert snapshot.floor_support[path_cell] == 0
    assert snapshot.clearance_evidence[path_cell] == 5
    assert snapshot.states[path_cell] != GridState.FREE
    endpoint_row, endpoint_col = endpoint_cell
    endpoint_rows = slice(endpoint_row - 2, endpoint_row + 3)
    endpoint_cols = slice(endpoint_col - 2, endpoint_col + 3)
    assert int(snapshot.floor_support[endpoint_rows, endpoint_cols].sum()) == 5
    assert int(snapshot.clearance_evidence[endpoint_rows, endpoint_cols].sum()) >= 5
    support_window = snapshot.floor_support[endpoint_rows, endpoint_cols] > 0
    odds_window = snapshot.occupancy_log_odds[endpoint_rows, endpoint_cols]
    assert np.isclose(odds_window[support_window].sum(), -1.75, atol=1e-6)
    assert snapshot.states[endpoint_cell] == GridState.FREE


def test_depth_hit_in_agent_height_band_still_marks_obstacle_occupied() -> None:
    rotation = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = (0.0, 0.0, 1.5)
    packet = FramePacket(
        run_id="obstacle-ray-fixture", scene_id="scene", frame_id="hit0", frame_index=0,
        simulator_time_s=0.04, host_monotonic_s=1.0,
        depth_m=np.array([[0.5]], dtype=np.float32),
        intrinsics=CameraIntrinsics(1.0, 1.0, 0.0, 0.0, 1, 1),
        T_world_camera=Pose3(transform), floor_z_m=0.0,
        calibration_hash="cal", depth_min_m=0.1, depth_max_m=10.0,
    )
    mapper = NavMapper(NavMapperConfig(resolution_m=0.05, depth_stride=1,
                                       agent_height_m=1.5), agent_radius_m=0.1)
    mapper.initialize_spawn(0.0, 0.0)
    snapshot = mapper.update(packet)
    hit_cell = snapshot.world_to_cell(0.5, 0.0)
    assert hit_cell is not None
    assert snapshot.states[hit_cell] == GridState.OCCUPIED


def test_rejected_subcell_move_does_not_become_an_occupied_surface() -> None:
    radius, height = 0.1, 1.5
    mapper = NavMapper(NavMapperConfig(agent_height_m=height), agent_radius_m=radius)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=radius))
    runtime = NavigationRuntime(
        mapper=mapper,
        frontier_extractor=FrontierExtractor(agent_radius_m=radius),
        planner=planner,
        policy=FrontierPolicy(planner),
        controller=Controller(ControllerConfig()),
        depth_safety=DepthSafetyGate(DepthSafetyConfig(
            corridor_half_width_m=0.2, floor_z_m=0.0, agent_height_m=height)),
        config=NavigationRuntimeConfig(initial_scan_yaw_rad=0.0),
    )
    packet = FramePacket(
        run_id="collision-fixture", scene_id="scene", frame_id="frame-0", frame_index=0,
        simulator_time_s=0.04, host_monotonic_s=100.0,
        depth_m=np.full((4, 4), np.nan, dtype=np.float32),
        intrinsics=CameraIntrinsics(2.0, 2.0, 1.5, 1.5, 4, 4),
        T_world_camera=Pose3(np.eye(4)), floor_z_m=0.0, calibration_hash="fixture",
        depth_min_m=0.1, depth_max_m=10.0,
    )
    pose = RobotPose2(0.0, 0.0, 0.0)
    tick = runtime.tick(packet, pose, dt_s=0.04, now_monotonic_s=100.0,
                        collided=True, collision_position_world_xy=(0.008, 0.0))

    # An 8 mm movement-filter rejection identifies a failed center pose. It
    # does not localize an obstacle surface to a 5 cm occupancy cell.
    assert not np.any(tick.snapshot.states == GridState.OCCUPIED)
    assert tick.snapshot.states[tick.snapshot.world_to_cell(0.0, 0.0)] == GridState.FREE


def _grid(origin_x: float, origin_y: float, shape=(23, 23)) -> GridSnapshot:
    states = np.full(shape, GridState.FREE, dtype=np.uint8)
    evidence = np.ones(shape, dtype=np.uint16)
    return GridSnapshot(
        revision=1, as_of_frame_id="fixture", as_of_frame_index=0,
        host_monotonic_s=1.0, resolution_m=0.1,
        origin_x_m=origin_x, origin_y_m=origin_y,
        states=states, floor_support=evidence, clearance_evidence=evidence,
        spawn_validated=np.zeros(shape, dtype=bool),
        occupancy_log_odds=np.full(shape, -1.0, dtype=np.float32),
        spawn_center_xy=(0.55, 0.55),
    )


def test_planner_temporarily_avoids_rejected_edge_without_changing_navmap() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    start_cell, goal_cell = (5, 5), (5, 10)
    start_xy = snapshot.cell_to_world(*start_cell)
    baseline = planner.plan(snapshot, start_xy, goal_cell)
    blocked_edge = ((5, 6), (5, 7))
    detour = planner.plan(snapshot, start_xy, goal_cell,
                          blocked_edges=frozenset((blocked_edge,)))

    assert baseline.success and detour.success
    assert detour.cost_m > baseline.cost_m
    assert all(tuple(sorted((a, b))) != blocked_edge
               for a, b in zip(detour.cells, detour.cells[1:]))
    assert np.all(snapshot.states == GridState.FREE)


def test_subcell_step_rejection_maps_to_forward_grid_edge() -> None:
    snapshot = _grid(0.0, 0.0)
    edge = _blocked_step_edge(snapshot, (0.55, 0.55), (0.57, 0.55),
                              allow_diagonal=True)
    assert edge == ((5, 5), (5, 6))


def test_rejected_step_can_be_compared_with_remapped_active_path_edge() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    start_xy = snapshot.cell_to_world(5, 5)
    goal_cell = (8, 8)
    path = planner.plan(snapshot, start_xy, goal_cell)
    assert path.success

    route_edge = _next_path_edge(snapshot, path, start_xy)
    assert route_edge == tuple(sorted((path.cells[0], path.cells[1])))
    route_start, route_end = route_edge
    route_start_xy = snapshot.cell_to_world(*route_start)
    route_end_xy = snapshot.cell_to_world(*route_end)
    blocked_xy = (
        start_xy[0] + (route_end_xy[0] - route_start_xy[0]) * 0.2,
        start_xy[1] + (route_end_xy[1] - route_start_xy[1]) * 0.2,
    )
    rejected_edge = _blocked_step_edge(
        snapshot, start_xy, blocked_xy, allow_diagonal=True)

    assert rejected_edge == route_edge


def test_rejected_step_edge_overlay_expires_without_mutating_snapshot() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    edge = ((5, 5), (5, 6))
    policy.report_goal_failure("", snapshot.revision, blocked_edge=edge)

    assert policy.blocked_edges(snapshot.revision) == frozenset((edge,))
    assert policy.blocked_edges(snapshot.revision + 100) == frozenset((edge,))
    assert policy.blocked_edges(snapshot.revision + 101) == frozenset()
    assert np.all(snapshot.states == GridState.FREE)


def test_controller_stops_immediately_when_active_path_disappears() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    goal = snapshot.world_to_cell(1.55, 0.55)
    assert goal is not None
    path = planner.plan(snapshot, (0.55, 0.55), goal)
    assert path.success
    controller = Controller(ControllerConfig())

    moving = None
    for index in range(8):
        now = 1.0 + index * 0.1
        moving = controller.tick(
            robot_xy=(0.55, 0.55), yaw_rad=0.0, now_monotonic_s=now,
            dt_s=0.1, depth_host_monotonic_s=now,
            depth_front_min_m=None, depth_coverage_ok=True,
            snapshot=snapshot, path=path, frame_id=f"move-{index}",
        )
    assert moving is not None and moving.reason == "tracking"
    assert moving.linear_mps > 0.02

    stopped = controller.tick(
        robot_xy=(0.55, 0.55), yaw_rad=0.0, now_monotonic_s=1.8,
        dt_s=0.1, depth_host_monotonic_s=1.8,
        depth_front_min_m=None, depth_coverage_ok=True,
        snapshot=snapshot, path=None, frame_id="path-lost",
    )
    assert stopped.reason == "no_valid_path"
    assert stopped.linear_mps == 0.0
    assert stopped.yaw_rate_radps == 0.0


def test_path_revalidation_remaps_cells_after_grid_origin_shift() -> None:
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    old = _grid(0.0, 0.0)
    goal = old.world_to_cell(1.55, 1.55)
    assert goal is not None
    path = planner.plan(old, (0.55, 0.55), goal)
    assert path.success
    new = _grid(-0.1, -0.1, shape=(25, 25))
    updated = planner.revalidate(new, path)
    assert updated.success
    old_goal_world = old.cell_to_world(*path.cells[-1])
    new_goal_world = new.cell_to_world(*updated.cells[-1])
    assert np.allclose(old_goal_world, new_goal_world)


def test_path_revalidation_ignores_obstructed_prefix_behind_current_pose() -> None:
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    old = _grid(0.0, 0.0)
    goal = old.world_to_cell(1.55, 0.55)
    assert goal is not None
    path = planner.plan(old, (0.55, 0.55), goal)
    assert path.success

    # New evidence blocks a cell already behind the robot. The remaining
    # route is clear and the original world-space goal is still reachable.
    states = old.states.copy()
    prefix_cell = old.world_to_cell(0.85, 0.55)
    assert prefix_cell is not None
    states[prefix_cell] = GridState.OCCUPIED
    updated_map = replace(old, revision=2, states=states)

    strict = planner.revalidate(updated_map, path)
    assert not strict.success
    assert strict.reason == "path_blocked_by_new_map"

    refreshed = planner.revalidate(updated_map, path, current_xy=(1.05, 0.55))
    assert refreshed.success
    assert refreshed.map_revision == updated_map.revision
    assert refreshed.cells[0] == updated_map.world_to_cell(1.05, 0.55)
    assert refreshed.cells[-1] == goal


def test_current_pose_revalidation_ignores_prefix_outside_shifted_map() -> None:
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    old = _grid(0.0, 0.0)
    goal = old.world_to_cell(1.55, 0.55)
    assert goal is not None
    path = planner.plan(old, (0.55, 0.55), goal)
    assert path.success

    # The map window moved forward: the old start/prefix is out of bounds,
    # while both the robot and original world-space goal remain represented.
    shifted = replace(old, revision=2, origin_x_m=0.9)
    strict = planner.revalidate(shifted, path)
    assert not strict.success
    assert strict.reason == "path_world_point_outside_map"

    refreshed = planner.revalidate(shifted, path, current_xy=(1.05, 0.55))
    assert refreshed.success
    assert refreshed.map_revision == shifted.revision
    assert refreshed.cells[-1] == shifted.world_to_cell(1.55, 0.55)


def test_lookahead_uses_forward_progress_instead_of_path_start() -> None:
    controller = Controller(ControllerConfig(lookahead_m=0.4))
    snapshot = _grid(0.0, 0.0, shape=(20, 45))
    cells = tuple((10, col) for col in range(1, 40))
    target = controller._lookahead(snapshot, cells, (1.45, 1.05))
    assert target is not None
    assert target[0] > 1.75


def test_frontier_diagnostics_report_reachable_component_and_safe_viewpoint() -> None:
    snapshot = _grid(0.0, 0.0, shape=(31, 31))
    states = np.full((31, 31), GridState.UNKNOWN, dtype=np.uint8)
    states[5:26, 5:26] = GridState.FREE
    snapshot = GridSnapshot(
        revision=snapshot.revision, as_of_frame_id=snapshot.as_of_frame_id,
        as_of_frame_index=snapshot.as_of_frame_index,
        host_monotonic_s=snapshot.host_monotonic_s, resolution_m=snapshot.resolution_m,
        origin_x_m=snapshot.origin_x_m, origin_y_m=snapshot.origin_y_m,
        states=states, floor_support=snapshot.floor_support,
        clearance_evidence=snapshot.clearance_evidence,
        spawn_validated=snapshot.spawn_validated,
        occupancy_log_odds=snapshot.occupancy_log_odds,
        spawn_center_xy=(1.55, 1.55),
    )
    result = FrontierExtractor(agent_radius_m=0.05, inflation_margin_m=0.0,
                               min_frontier_length_m=0.2).extract(snapshot, (1.55, 1.55))
    assert result.frontiers
    assert result.reachable_free_cells > 0
    assert result.connected_known_free_cells >= result.reachable_free_cells
    assert result.raw_frontier_cells > 0
    assert result.length_qualified_components > 0
    assert result.components_with_viewpoint > 0
    assert all(f.viewpoint_world_xy == snapshot.cell_to_world(*f.viewpoint_cell)
               for f in result.frontiers)


def _snapshot_with_unobserved_ring_at_start() -> tuple[GridSnapshot, tuple[float, float]]:
    shape = (17, 17)
    states = np.full(shape, GridState.FREE, dtype=np.uint8)
    # At 5 cm resolution with a 5 cm footprint, this single unknown cell
    # erodes the robot's current center cell but leaves a safe observed cell
    # one step ahead. It represents the live run's local evidence gap.
    states[8, 6] = GridState.UNKNOWN
    evidence = (states == GridState.FREE).astype(np.uint16)
    snapshot = GridSnapshot(
        revision=4, as_of_frame_id="local-evidence-gap", as_of_frame_index=4,
        host_monotonic_s=4.0, resolution_m=0.05, origin_x_m=0.0, origin_y_m=0.0,
        states=states, floor_support=evidence, clearance_evidence=evidence,
        spawn_validated=np.zeros(shape, dtype=bool),
        occupancy_log_odds=np.where(states == GridState.FREE, -1.0, 0.0).astype(np.float32),
        spawn_center_xy=None,
    )
    # Cell row 8, col 7.
    return snapshot, snapshot.cell_to_world(8, 7)


def test_planner_uses_validated_current_pose_as_start_only() -> None:
    snapshot, start_xy = _snapshot_with_unobserved_ring_at_start()
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    start = snapshot.world_to_cell(*start_xy)
    goal = (8, 8)
    assert start == (8, 7)
    assert not planner._traversable(snapshot)[start]
    assert not planner.plan(snapshot, start_xy, goal).success

    result = planner.plan(snapshot, start_xy, goal, allow_validated_start_anchor=True)
    assert result.success
    assert result.cells[0] == start and result.cells[-1] == goal
    traversable = planner._traversable(snapshot)
    assert all(traversable[cell] for cell in result.cells[1:])
    # The unknown cell remains unknown and cannot become a goal/path cell.
    assert snapshot.states[8, 6] == GridState.UNKNOWN
    assert (8, 6) not in result.cells


def test_frontier_search_can_depart_validated_pose_without_freeing_unknown() -> None:
    snapshot, start_xy = _snapshot_with_unobserved_ring_at_start()
    # Add an unknown frontier beyond a broad observed area.
    states = snapshot.states.copy()
    states[4:13, 12] = GridState.UNKNOWN
    evidence = (states == GridState.FREE).astype(np.uint16)
    snapshot = GridSnapshot(
        revision=snapshot.revision, as_of_frame_id=snapshot.as_of_frame_id,
        as_of_frame_index=snapshot.as_of_frame_index,
        host_monotonic_s=snapshot.host_monotonic_s, resolution_m=snapshot.resolution_m,
        origin_x_m=snapshot.origin_x_m, origin_y_m=snapshot.origin_y_m,
        states=states, floor_support=evidence, clearance_evidence=evidence,
        spawn_validated=snapshot.spawn_validated,
        occupancy_log_odds=np.where(states == GridState.FREE, -1.0, 0.0).astype(np.float32),
        spawn_center_xy=None,
    )
    extractor = FrontierExtractor(
        min_frontier_length_m=0.0, agent_radius_m=0.05, inflation_margin_m=0.0,
        min_viewpoint_offset_m=0.05, max_viewpoint_offset_m=0.15,
    )
    blocked = extractor.extract(snapshot, start_xy)
    assert blocked.reason == "robot_not_on_observed_free_cell"

    found = extractor.extract(snapshot, start_xy, allow_validated_start_anchor=True)
    assert found.frontiers
    assert found.reachable_free_cells > 1
    assert snapshot.states[8, 6] == GridState.UNKNOWN
    assert all(snapshot.states[cell] == GridState.FREE
               for frontier in found.frontiers for cell in frontier.boundary_cells)


def test_vectorized_viewpoint_search_matches_nested_loop_reference() -> None:
    rng = np.random.default_rng(20261022)
    offsets = _view_offsets(0.1, 0.2, 0.6)
    for _ in range(40):
        reachable = rng.random((19, 23)) > 0.42
        points = list(zip(rng.integers(0, 19, size=12), rng.integers(0, 23, size=12)))
        cells = list(dict.fromkeys((int(r), int(c)) for r, c in points))
        expected_views: dict[tuple[int, int], float] = {}
        radius = int(np.ceil(0.6 / 0.1))
        for br, bc in cells:
            for dr in range(-radius, radius + 1):
                for dc in range(-radius, radius + 1):
                    distance = float(np.hypot(dr, dc)) * 0.1
                    cell = (br + dr, bc + dc)
                    if (0.2 <= distance <= 0.6 and
                            0 <= cell[0] < reachable.shape[0] and
                            0 <= cell[1] < reachable.shape[1] and reachable[cell]):
                        expected_views[cell] = min(expected_views.get(cell, np.inf), distance)
        expected = min(
            expected_views,
            key=lambda rc: (abs(expected_views[rc] - 0.45),
                            (rc[0] - np.mean([p[0] for p in cells])) ** 2
                            + (rc[1] - np.mean([p[1] for p in cells])) ** 2),
        ) if expected_views else None
        actual = _best_reachable_viewpoint(cells, reachable, offsets)
        assert actual == expected


def test_frontier_reachability_matches_astar_corner_cut_rule() -> None:
    traversable = np.zeros((3, 3), dtype=bool)
    traversable[1, 1] = True
    traversable[2, 2] = True
    loose = _flood(traversable, (1, 1), _neighbors(8))
    safe = _flood(traversable, (1, 1), _neighbors(8), prevent_corner_cut=True)
    assert loose[2, 2]
    assert not safe[2, 2]


def test_policy_temporarily_skips_collision_blocked_goal() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    already_viewed = Frontier("already-viewed", snapshot.revision, ((5, 5),), (5, 5), 10)
    near = Frontier("near", snapshot.revision, ((6, 7),), (6, 7), 10)
    farther = Frontier("farther", snapshot.revision, ((8, 8),), (8, 8), 10)
    frontiers = FrontierSet(snapshot.revision, snapshot.as_of_frame_id,
                            (already_viewed, near, farther))
    first = policy.select(snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
                          now_monotonic_s=1.0)
    assert first.frontier is not None and first.frontier.frontier_id == "near"
    assert first.goal_diagnostics["previous_goal_match"] == "no_previous_goal"
    assert first.goal_diagnostics["selected_goal_id"] == "near"
    assert first.goal_diagnostics["reachable_candidate_count"] == 3
    assert {item["frontier_id"] for item in
            first.goal_diagnostics["reachable_candidate_sample"]} == {
                "already-viewed", "near", "farther"}

    policy.report_goal_failure("near", snapshot.revision)
    alternative = policy.select(snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
                                now_monotonic_s=2.0)
    assert alternative.frontier is not None and alternative.frontier.frontier_id == "farther"


def test_policy_can_opt_in_to_spatially_distinct_viewpoints() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner, PolicyConfig(
        reached_distance_m=0.10, visited_viewpoint_radius_m=0.50))
    reached = Frontier("reached", snapshot.revision, ((5, 6),), (5, 6), 10)
    first = policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (reached,)),
        robot_xy=(0.55, 0.55), now_monotonic_s=1.0)
    assert first.state == "OBSERVE"
    assert first.goal_diagnostics["visited_viewpoint_radius_m"] == 0.50

    nearby = Frontier("nearby-new-id", snapshot.revision, ((5, 8),), (5, 8), 10)
    farther = Frontier("farther", snapshot.revision, ((5, 14),), (5, 14), 10)
    second = policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id,
                              (nearby, farther)),
        robot_xy=(0.55, 0.55), now_monotonic_s=2.0)
    assert second.frontier is not None and second.frontier.frontier_id == "farther"
    assert second.goal_diagnostics["spatially_suppressed_candidate_count"] == 1


def test_policy_suppresses_near_alternative_after_reaching_goal_in_same_selection() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner, PolicyConfig(
        reached_distance_m=0.10, visited_viewpoint_radius_m=0.50))
    reached = Frontier("reached", snapshot.revision, ((5, 6),), (5, 6), 10)
    nearby = Frontier("nearby-new-id", snapshot.revision, ((5, 8),), (5, 8), 10)
    farther = Frontier("farther", snapshot.revision, ((5, 14),), (5, 14), 10)
    decision = policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id,
                              (reached, nearby, farther)),
        robot_xy=(0.55, 0.55), now_monotonic_s=1.0)
    assert decision.frontier is not None and decision.frontier.frontier_id == "farther"
    assert decision.goal_diagnostics["spatially_suppressed_candidate_count"] == 1


def test_policy_does_not_count_adjacent_map_cell_as_already_reached() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    candidate = Frontier("adjacent-view", snapshot.revision, ((5, 6),), (5, 6), 10,
                         snapshot.cell_to_world(5, 6))
    frontiers = FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (candidate,))

    legacy = FrontierPolicy(planner).select(
        snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
        now_monotonic_s=1.0,
    )
    map_resolution = FrontierPolicy(
        planner, PolicyConfig(reached_distance_m=snapshot.resolution_m / np.sqrt(2.0)),
    ).select(snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
             now_monotonic_s=1.0)

    assert legacy.reason == "frontier_viewpoint_already_reached"
    assert map_resolution.state == "MOVE"
    assert map_resolution.frontier is not None
    assert map_resolution.frontier.frontier_id == "adjacent-view"
    assert map_resolution.path is not None and map_resolution.path.success
    assert map_resolution.path.cost_m == snapshot.resolution_m


def test_policy_emits_reached_event_at_robot_footprint_tolerance() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner, PolicyConfig(reached_distance_m=0.10))
    target = Frontier("footprint-view", snapshot.revision, ((5, 6),), (5, 6), 10,
                      snapshot.cell_to_world(5, 6))
    candidates = FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (target,))
    first = policy.select(snapshot=snapshot, frontiers=candidates,
                          robot_xy=snapshot.cell_to_world(5, 3), now_monotonic_s=1.0)
    assert first.state == "MOVE" and first.frontier is not None

    reached = policy.select(snapshot=snapshot, frontiers=candidates,
                             robot_xy=snapshot.cell_to_world(5, 6), now_monotonic_s=2.0)
    assert reached.state == "OBSERVE"
    assert reached.reason == "frontier_viewpoint_reached"
    assert reached.frontier is not None and reached.frontier.frontier_id == "footprint-view"


def test_policy_keeps_new_frontier_candidates_after_one_rejected_center_pose() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    near = Frontier("near", snapshot.revision, ((6, 7),), (6, 7), 10)
    nearby_replacement = Frontier("new-id-near-same-wall", snapshot.revision,
                                  ((6, 8),), (6, 8), 10)
    farther = Frontier("farther", snapshot.revision, ((15, 15),), (15, 15), 10)
    frontiers = FrontierSet(snapshot.revision, snapshot.as_of_frame_id,
                            (near, nearby_replacement, farther))
    first = policy.select(snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
                          now_monotonic_s=1.0)
    assert first.frontier is not None and first.frontier.frontier_id == "near"

    policy.report_goal_failure("near", snapshot.revision)
    alternative = policy.select(snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
                                now_monotonic_s=2.0)
    assert alternative.frontier is not None
    assert alternative.frontier.frontier_id == "new-id-near-same-wall"


def test_policy_keeps_old_viewpoint_when_frontier_boundary_moves_farther_than_match_radius() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    old = Frontier("old-boundary", snapshot.revision, ((8, 8),), (8, 8), 10,
                   snapshot.cell_to_world(8, 8))
    first = policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (old,)),
        robot_xy=(0.55, 0.55), now_monotonic_s=1.0,
    )
    assert first.frontier is not None and first.frontier.frontier_id == "old-boundary"

    newer_snapshot = replace(snapshot, revision=2, as_of_frame_id="fixture-2")
    far = Frontier("moved-boundary", newer_snapshot.revision, ((15, 15),), (15, 15), 10,
                   newer_snapshot.cell_to_world(15, 15))
    continued = policy.select(
        snapshot=newer_snapshot,
        frontiers=FrontierSet(newer_snapshot.revision, newer_snapshot.as_of_frame_id, (far,)),
        robot_xy=(0.65, 0.65), now_monotonic_s=2.0,
    )
    assert continued.frontier is not None
    assert continued.frontier.frontier_id == "old-boundary"
    assert continued.frontier.map_revision == newer_snapshot.revision
    assert continued.path is not None and continued.path.success
    assert continued.goal_diagnostics["previous_goal_id"] == "old-boundary"
    assert continued.goal_diagnostics["held_goal_recovery"] == "held_cell_replanned"
    assert continued.goal_diagnostics["selected_goal_id"] == "old-boundary"


def test_policy_holds_exact_viewpoint_when_frontier_moves_within_old_wide_radius() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    old_cell = (8, 8)
    old = Frontier("old-boundary", snapshot.revision, (old_cell,), old_cell, 10,
                   snapshot.cell_to_world(*old_cell))
    first = policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (old,)),
        robot_xy=(0.55, 0.55), now_monotonic_s=1.0,
    )
    assert first.frontier is not None

    newer_snapshot = replace(snapshot, revision=2, as_of_frame_id="fixture-drifting-frontier")
    shifted_cell = (8, 11)  # 0.30 m drift: near the old 0.60 m association radius.
    shifted = Frontier("new-boundary-id", newer_snapshot.revision,
                       (shifted_cell,), shifted_cell, 10,
                       newer_snapshot.cell_to_world(*shifted_cell))
    continued = policy.select(
        snapshot=newer_snapshot,
        frontiers=FrontierSet(newer_snapshot.revision, newer_snapshot.as_of_frame_id,
                              (shifted,)),
        robot_xy=(0.65, 0.65), now_monotonic_s=2.0,
    )

    assert continued.frontier is not None
    assert continued.frontier.frontier_id == "old-boundary"
    assert continued.frontier.viewpoint_world_xy == first.frontier.viewpoint_world_xy
    assert continued.path is not None and continued.path.success
    assert continued.goal_diagnostics["goal_target_match_radius_m"] == 0.2
    assert continued.goal_diagnostics["held_goal_recovery"] == "held_cell_replanned"


def test_policy_does_not_move_same_id_goal_when_viewpoint_jumps_over_match_radius() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    old_cell = (8, 8)
    old = Frontier("stable-component", snapshot.revision, (old_cell,), old_cell, 10,
                   snapshot.cell_to_world(*old_cell))
    first = policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (old,)),
        robot_xy=(0.55, 0.55), now_monotonic_s=1.0,
    )
    assert first.frontier is not None

    newer_snapshot = replace(snapshot, revision=2, as_of_frame_id="fixture-same-id-jump")
    moved_cell = (16, 16)
    moved = Frontier("stable-component", newer_snapshot.revision, (moved_cell,),
                     moved_cell, 10, newer_snapshot.cell_to_world(*moved_cell))
    continued = policy.select(
        snapshot=newer_snapshot,
        frontiers=FrontierSet(newer_snapshot.revision, newer_snapshot.as_of_frame_id, (moved,)),
        robot_xy=(0.65, 0.65), now_monotonic_s=2.0,
    )

    assert continued.frontier is not None
    assert continued.frontier.frontier_id == "stable-component"
    assert continued.frontier.viewpoint_world_xy == first.frontier.viewpoint_world_xy
    assert continued.goal_diagnostics["previous_goal_match"] == (
        "frontier_id_target_moved_over_match_radius")
    assert continued.goal_diagnostics["same_id_target_shift_m"] > 0.60
    assert continued.goal_diagnostics["held_goal_recovery"] == "held_cell_replanned"


def test_policy_diagnostics_explain_replaced_goal_when_held_cell_becomes_occupied() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    old_cell = (8, 8)
    old = Frontier("old-goal", snapshot.revision, (old_cell,), old_cell, 10,
                   snapshot.cell_to_world(*old_cell))
    first = policy.select(
        snapshot=snapshot,
        frontiers=FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (old,)),
        robot_xy=(0.55, 0.55), now_monotonic_s=1.0,
    )
    assert first.frontier is not None and first.frontier.frontier_id == "old-goal"

    states = snapshot.states.copy()
    states[old_cell] = GridState.OCCUPIED
    changed = replace(snapshot, revision=2, as_of_frame_id="fixture-occupied-goal",
                      states=states)
    replacement_cell = (15, 15)
    replacement = Frontier(
        "replacement", changed.revision, (replacement_cell,), replacement_cell, 10,
        changed.cell_to_world(*replacement_cell),
    )
    decision = policy.select(
        snapshot=changed,
        frontiers=FrontierSet(changed.revision, changed.as_of_frame_id, (replacement,)),
        robot_xy=(0.55, 0.55), now_monotonic_s=10.0,
    )

    assert decision.frontier is not None and decision.frontier.frontier_id == "replacement"
    assert decision.goal_diagnostics["previous_goal_id"] == "old-goal"
    assert decision.goal_diagnostics["previous_goal_match"] == "frontier_id_and_nearby_match_missing"
    assert decision.goal_diagnostics["held_goal_recovery"] == "held_cell_occupied"
    assert decision.goal_diagnostics["selected_goal_id"] == "replacement"


def test_policy_goal_cooldown_does_not_remove_all_nearby_candidates() -> None:
    snapshot = _grid(0.0, 0.0)
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    policy = FrontierPolicy(planner)
    near = Frontier("near", snapshot.revision, ((6, 7),), (6, 7), 10)
    replacement = Frontier("replacement", snapshot.revision, ((6, 8),), (6, 8), 10)
    frontiers = FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (near, replacement))
    selected = policy.select(snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
                             now_monotonic_s=1.0)
    assert selected.frontier is not None
    policy.report_goal_failure(selected.frontier.frontier_id, snapshot.revision)

    decision = policy.select(snapshot=snapshot, frontiers=frontiers, robot_xy=(0.55, 0.55),
                              now_monotonic_s=2.0)
    assert decision.frontier is not None
    assert decision.frontier.frontier_id == "replacement"


def test_runtime_freshness_uses_decision_time_not_capture_time() -> None:
    radius, height = 0.1, 1.5
    planner = AStarPlanner(PlannerConfig(agent_radius_m=radius))
    runtime = NavigationRuntime(
        mapper=NavMapper(NavMapperConfig(agent_height_m=height), agent_radius_m=radius),
        frontier_extractor=FrontierExtractor(agent_radius_m=radius),
        planner=planner,
        policy=FrontierPolicy(planner),
        controller=Controller(ControllerConfig(max_depth_age_s=0.3, max_nav_age_s=1.0)),
        depth_safety=DepthSafetyGate(DepthSafetyConfig(
            corridor_half_width_m=0.2, floor_z_m=0.0, agent_height_m=height)),
        config=NavigationRuntimeConfig(initial_scan_yaw_rad=0.0),
    )
    depth = np.full((4, 4), np.nan, dtype=np.float32)
    packet = FramePacket(
        run_id="runtime-fixture", scene_id="scene", frame_id="f0", frame_index=0,
        simulator_time_s=0.04, host_monotonic_s=100.0, depth_m=depth,
        intrinsics=CameraIntrinsics(2, 2, 1.5, 1.5, 4, 4),
        T_world_camera=Pose3(np.eye(4)), floor_z_m=0.0,
        calibration_hash="cal", depth_min_m=0.1, depth_max_m=10.0,
    )
    tick = runtime.tick(packet, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                        now_monotonic_s=100.5)
    assert abs(tick.input_age_s - 0.5) < 0.02
    assert tick.command.reason == "depth_stale"


def _runtime_for_motion_fixture(scan_yaw: float) -> NavigationRuntime:
    radius, height = 0.1, 1.5
    planner = AStarPlanner(PlannerConfig(agent_radius_m=radius))
    return NavigationRuntime(
        mapper=NavMapper(NavMapperConfig(agent_height_m=height), agent_radius_m=radius),
        frontier_extractor=FrontierExtractor(agent_radius_m=radius), planner=planner,
        policy=FrontierPolicy(planner), controller=Controller(ControllerConfig()),
        depth_safety=DepthSafetyGate(DepthSafetyConfig(
            corridor_half_width_m=0.2, floor_z_m=0.0, agent_height_m=height)),
        config=NavigationRuntimeConfig(initial_scan_yaw_rad=scan_yaw,
                                       initial_scan_rate_radps=0.3,
                                       collision_recovery_yaw_rad=0.25),
    )


def _motion_packet(frame_index: int) -> FramePacket:
    return FramePacket(
        run_id="motion-fixture", scene_id="scene", frame_id=f"m{frame_index}",
        frame_index=frame_index, simulator_time_s=frame_index * 0.04,
        host_monotonic_s=10.0 + frame_index * 0.04,
        depth_m=np.full((4, 4), 3.0, dtype=np.float32),
        intrinsics=CameraIntrinsics(2, 2, 1.5, 1.5, 4, 4),
        T_world_camera=Pose3(np.eye(4)), floor_z_m=0.0,
        calibration_hash="cal", depth_min_m=0.1, depth_max_m=10.0,
    )


def test_runtime_reports_path_revalidation_failure_reason() -> None:
    runtime = _runtime_for_motion_fixture(scan_yaw=0.0)
    try:
        first_packet = _motion_packet(0)
        first = runtime.tick(
            first_packet, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
            now_monotonic_s=first_packet.host_monotonic_s,
        )
        runtime._path = PathResult(
            False, (), float("inf"), first.snapshot.revision, "fixture_invalid_path",
        )
        next_packet = _motion_packet(1)
        tick = runtime.tick(
            next_packet, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
            now_monotonic_s=next_packet.host_monotonic_s + 0.3,
        )
        assert tick.path_revalidation_failure_reason == "no_path_to_revalidate"
    finally:
        runtime.close()


def test_runtime_performs_initial_in_place_scan_before_policy_motion() -> None:
    runtime = _runtime_for_motion_fixture(0.5)
    first = runtime.tick(_motion_packet(0), RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                         now_monotonic_s=10.0)
    assert first.decision is not None and first.decision.state == "SCAN"
    assert first.command.reason == "initial_scan"
    assert first.command.linear_mps == 0.0 and first.command.yaw_rate_radps > 0
    assert 10.0 < first.command.valid_until_monotonic_s <= 10.3

    second = runtime.tick(_motion_packet(1), RobotPose2(0.0, 0.0, 0.3), dt_s=0.04,
                          now_monotonic_s=10.04)
    assert second.decision is not None and second.decision.state == "SCAN"
    third = runtime.tick(_motion_packet(2), RobotPose2(0.0, 0.0, 0.6), dt_s=0.04,
                         now_monotonic_s=10.08)
    assert third.command.reason != "initial_scan"


def test_runtime_enters_bounded_rotation_recovery_after_collision() -> None:
    runtime = _runtime_for_motion_fixture(0.0)
    runtime._last_command_yaw_rate = -0.2
    tick = runtime.tick(_motion_packet(0), RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                        now_monotonic_s=10.0, collided=True)
    assert tick.decision is not None and tick.decision.state == "RECOVERY"
    assert tick.command.reason == "collision_recovery"
    assert tick.command.linear_mps == 0.0 and tick.command.yaw_rate_radps > 0


def test_runtime_reports_navmesh_rejected_step_during_active_recovery() -> None:
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        first = runtime.tick(
            _motion_packet(0), RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
            now_monotonic_s=10.0, collided=True,
        )
        assert first.command.reason == "collision_recovery"
        recovery_started_at = runtime._recovery_last_yaw

        second_packet = _motion_packet(1)
        second = runtime.tick(
            second_packet, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
            now_monotonic_s=second_packet.host_monotonic_s,
            collided=True,
            collision_position_world_xy=(0.02, 0.0),
            collision_filter_details={"reason": ["filtered_step_displacement_invalid"]},
        )

        assert second.blocked_step_edge is not None
        assert second.active_blocked_edge_count == 1
        assert second.command.reason == "collision_recovery"
        assert runtime._recovery_last_yaw == recovery_started_at
        assert runtime._recovery_progress_rad == 0.0
    finally:
        runtime.close()


def test_runtime_brakes_if_depth_ages_during_synchronous_processing() -> None:
    runtime = _runtime_for_motion_fixture(0.0)
    evaluate = runtime.depth_safety.evaluate

    def delayed_evaluate(**kwargs):
        time.sleep(0.31)
        return evaluate(**kwargs)

    runtime.depth_safety.evaluate = delayed_evaluate
    tick = runtime.tick(_motion_packet(0), RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                        now_monotonic_s=10.0)

    assert tick.input_age_s > 0.30
    assert tick.command.reason == "depth_stale"
    assert tick.command.linear_mps == 0.0 and tick.command.yaw_rate_radps == 0.0
    assert tick.command.valid_until_monotonic_s <= 10.0 + 0.30


def test_async_planner_does_not_block_tick_or_attach_stale_map_result() -> None:
    class SlowFrontierExtractor(FrontierExtractor):
        def __init__(self, started: threading.Event, **kwargs) -> None:
            super().__init__(**kwargs)
            self.started = started

        def extract(self, snapshot, robot_xy, **kwargs):
            self.started.set()
            time.sleep(0.20)
            return super().extract(snapshot, robot_xy, **kwargs)

    started = threading.Event()
    radius, height = 0.1, 1.5
    planner = AStarPlanner(PlannerConfig(agent_radius_m=radius))
    runtime = NavigationRuntime(
        mapper=NavMapper(NavMapperConfig(agent_height_m=height), agent_radius_m=radius),
        frontier_extractor=SlowFrontierExtractor(started, agent_radius_m=radius),
        planner=planner, policy=FrontierPolicy(planner),
        controller=Controller(ControllerConfig()),
        depth_safety=DepthSafetyGate(DepthSafetyConfig(
            corridor_half_width_m=0.2, floor_z_m=0.0, agent_height_m=height)),
        config=NavigationRuntimeConfig(initial_scan_yaw_rad=0.0, map_update_hz=100.0,
                                       async_planning=True),
    )
    try:
        packet0 = _motion_packet(0)
        first = runtime.tick(packet0, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                             now_monotonic_s=packet0.host_monotonic_s)
        assert started.wait(timeout=0.1)
        assert first.planner_pending
        assert first.planning_duration_s < 0.05

        packet1 = _motion_packet(1)
        second = runtime.tick(packet1, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                              now_monotonic_s=packet1.host_monotonic_s)
        assert second.snapshot.revision > first.snapshot.revision

        deadline = time.monotonic() + 1.0
        while runtime._plan_worker.busy and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not runtime._plan_worker.busy
        packet2 = _motion_packet(2)
        third = runtime.tick(packet2, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                             now_monotonic_s=packet2.host_monotonic_s)
        assert third.planner_result_stale
        assert third.frontiers is None and third.decision is None
        assert third.command.linear_mps == 0.0
    finally:
        assert runtime.close(timeout_s=1.0)


def test_async_move_result_replans_world_goal_on_new_grid_origin() -> None:
    old = _grid(0.0, 0.0)
    old_goal = old.world_to_cell(1.55, 1.55)
    assert old_goal is not None
    frontier = Frontier("async-goal", old.revision, (old_goal,), old_goal, 1,
                        old.cell_to_world(*old_goal))
    frontiers = FrontierSet(old.revision, old.as_of_frame_id, (frontier,))
    planner = AStarPlanner(PlannerConfig(agent_radius_m=0.05, inflation_margin_m=0.0))
    path = planner.plan(old, (0.55, 0.55), old_goal)
    assert path.success
    decision = NavigationDecision("MOVE", frontier, path, "fixture", 1.0)
    outcome = PlanOutcome(
        old.revision, old.as_of_frame_id, old.host_monotonic_s,
        old.resolution_m, old.world_frame, old.scope_id,
        frontiers, decision, 0.01,
    )

    current = _grid(-0.1, -0.1, shape=(25, 25))
    current = GridSnapshot(
        revision=2, as_of_frame_id="new-generation", as_of_frame_index=1,
        host_monotonic_s=1.2, resolution_m=current.resolution_m,
        origin_x_m=current.origin_x_m, origin_y_m=current.origin_y_m,
        states=current.states, floor_support=current.floor_support,
        clearance_evidence=current.clearance_evidence,
        spawn_validated=current.spawn_validated,
        occupancy_log_odds=current.occupancy_log_odds,
        spawn_center_xy=current.spawn_center_xy,
    )
    current_goal = current.world_to_cell(*frontier.viewpoint_world_xy)
    assert current_goal is not None
    current_planner = AStarPlanner(planner.config)
    baseline_current_path = current_planner.plan(current, (0.55, 0.55), current_goal)
    assert baseline_current_path.success and len(baseline_current_path.cells) >= 3
    blocked_edge = tuple(sorted((baseline_current_path.cells[1],
                                 baseline_current_path.cells[2])))
    runtime = _runtime_for_motion_fixture(0.0)
    # Use an independent planner cache for safe path rebasing.
    runtime._path_revalidator = AStarPlanner(planner.config)
    runtime.policy.report_goal_failure("", current.revision, blocked_edge=blocked_edge)
    try:
        accepted, rebased = runtime._adopt_worker_outcome(
            outcome, current, (0.55, 0.55), 1.2,
        )
        assert accepted and rebased
        assert runtime._decision is not None and runtime._decision.path is not None
        assert runtime._decision.path.map_revision == current.revision
        new_goal = current.world_to_cell(*frontier.viewpoint_world_xy)
        assert new_goal is not None
        assert runtime._decision.path.cells[-1] == new_goal
        assert all(tuple(sorted((a, b))) != blocked_edge
                   for a, b in zip(runtime._decision.path.cells,
                                   runtime._decision.path.cells[1:]))
        assert runtime._frontiers is not None
        assert runtime._frontiers.map_revision == old.revision
        assert runtime._frontiers.frontiers[0].viewpoint_world_xy == frontier.viewpoint_world_xy
    finally:
        runtime.close()


def test_async_mapping_publishes_only_completed_immutable_snapshots() -> None:
    class SlowMapper(NavMapper):
        def update(self, packet):
            time.sleep(0.15)
            return super().update(packet)

    radius, height = 0.1, 1.5
    planner = AStarPlanner(PlannerConfig(agent_radius_m=radius))
    runtime = NavigationRuntime(
        mapper=SlowMapper(NavMapperConfig(agent_height_m=height), agent_radius_m=radius),
        frontier_extractor=FrontierExtractor(agent_radius_m=radius),
        planner=planner, policy=FrontierPolicy(planner),
        controller=Controller(ControllerConfig()),
        depth_safety=DepthSafetyGate(DepthSafetyConfig(
            corridor_half_width_m=0.2, floor_z_m=0.0, agent_height_m=height)),
        config=NavigationRuntimeConfig(initial_scan_yaw_rad=0.0, async_mapping=True),
    )
    try:
        packet = _motion_packet(0)
        started = time.perf_counter()
        tick = runtime.tick(packet, RobotPose2(0.0, 0.0, 0.0), dt_s=0.04,
                            now_monotonic_s=packet.host_monotonic_s)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.12
        assert tick.map_worker_pending
        assert tick.command.linear_mps == 0.0

        deadline = time.monotonic() + 1.0
        snapshot = runtime.map_snapshot()
        while snapshot.revision == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
            snapshot = runtime.map_snapshot()
        assert snapshot.revision == 1
        assert snapshot.as_of_frame_id == packet.frame_id
        assert not snapshot.states.flags.writeable
        assert runtime.worker_status()["map"]["completed"] == 1
    finally:
        assert runtime.close(timeout_s=1.0)


def test_map_worker_replaces_pending_frame_and_reports_the_drop() -> None:
    class SlowMapper(NavMapper):
        def __init__(self, *args, started: threading.Event, processed: list[int], **kwargs):
            super().__init__(*args, **kwargs)
            self.started = started
            self.processed = processed

        def update(self, packet):
            self.started.set()
            time.sleep(0.10)
            result = super().update(packet)
            self.processed.append(packet.frame_index)
            return result

    started = threading.Event()
    processed: list[int] = []
    mapper = SlowMapper(NavMapperConfig(agent_height_m=1.5), agent_radius_m=0.1,
                        started=started, processed=processed)
    mapper.initialize_spawn(0.0, 0.0)
    worker = LatestMapWorker(mapper, mapper.snapshot())
    try:
        assert worker.submit(_motion_packet(0))
        assert started.wait(timeout=0.1)
        assert worker.submit(_motion_packet(1))
        assert worker.submit(_motion_packet(2))
        assert worker.stats()["dropped_pending"] == 1

        deadline = time.monotonic() + 1.0
        snapshot = worker.snapshot()
        while snapshot.as_of_frame_id != "m2" and time.monotonic() < deadline:
            time.sleep(0.005)
            snapshot = worker.snapshot()
        assert processed == [0, 2]
        assert snapshot.as_of_frame_index == 2
        stats = worker.stats()
        assert stats["completed"] == 2
        assert stats["last_duration_s"] > 0.0
        assert stats["last_cpu_duration_s"] >= 0.0
        assert stats["last_queue_wait_s"] >= 0.0
        assert stats["last_submit_to_publish_s"] >= stats["last_queue_wait_s"]
        profile = stats["last_update_profile_s"]
        assert profile["total_wall_s"] >= 0.0
        assert profile["total_cpu_s"] >= 0.0
        assert profile["ray_integration_wall_s"] > 0.0
        assert profile["ray_integration_cpu_s"] >= 0.0
        assert profile["snapshot_wall_s"] > 0.0
    finally:
        assert worker.close(timeout_s=1.0)


def test_yaml_profile_is_loaded_and_its_subset_fallback_matches_runtime_values() -> None:
    path = ROOT / "configs/exploration/cpu_smooth.yaml"
    profile = load_runtime_profile(path)
    fallback = _parse_yaml_mapping_subset(path.read_text(encoding="utf-8"))
    assert profile["navigation"]["map_update_hz"] == 5
    assert profile["navigation"]["async_mapping"] is True
    assert profile["navigation"]["async_planning"] is True
    assert fallback["navigation"]["max_speed_mps"] == 0.2
    assert profile["camera"]["target_fps"] == 25
    keyframes = keyframe_runtime_settings(profile)
    assert keyframes["min_translation_m"] == 0.25
    assert keyframes["min_yaw_deg"] == 20.0
    assert keyframes["max_pending"] == 2
    assert keyframes["max_protected_pending"] == 1
    assert keyframes["raw_pending_byte_cap"] == 32 * 1024**2


def test_progress_timer_pauses_across_temporary_wait(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        _install_progress_goal(runtime, monkeypatch)
        runtime._progress_anchor_xy = (0.55, 0.55)
        runtime._progress_commanded_s = 0.3
        runtime._last_motion_requested = False
        tick = runtime.tick(_motion_packet(0), RobotPose2(0.55, 0.55, 0.0), dt_s=0.04,
                            now_monotonic_s=10.4)
        assert tick.command.reason == "depth_stale"
        assert runtime._progress_commanded_s == 0.3
        assert not runtime._last_motion_requested
    finally:
        runtime.close()


def test_collision_after_wait_still_reports_the_failed_goal(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        _install_progress_goal(runtime, monkeypatch)
        pose = RobotPose2(0.55, 0.55, 0.0)
        runtime.tick(_motion_packet(0), pose, dt_s=0.04, now_monotonic_s=10.4)
        runtime.tick(_motion_packet(1), pose, dt_s=0.04, now_monotonic_s=10.44,
                     collided=True)
        assert "progress-goal" in runtime.policy._failed_until_revision
        assert runtime._rejected_goal_targets[0][0] == (1.55, 0.55)
    finally:
        runtime.close()


def test_async_arrival_rejects_robot_far_from_goal_even_same_revision(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        snapshot, decision = _install_progress_goal(runtime, monkeypatch)
        arrival = replace(decision, state="OBSERVE", path=None,
                          reason="frontier_viewpoint_reached")
        outcome = PlanOutcome(snapshot.revision, snapshot.as_of_frame_id,
                              snapshot.host_monotonic_s, snapshot.resolution_m,
                              snapshot.world_frame, snapshot.scope_id, None, arrival, 0.01)
        assert runtime._adopt_worker_outcome(outcome, snapshot, (0.55, 0.55), 10.0) == (False, False)
        assert runtime._path is decision.path
    finally:
        runtime.close()


def test_persistent_front_brake_rejects_goal_and_enters_recovery(monkeypatch):
    from exploration.depth_safety import DepthSafetyResult
    runtime = _runtime_for_motion_fixture(0.0)
    runtime.config = replace(runtime.config, obstacle_block_timeout_s=0.05)
    try:
        _install_progress_goal(runtime, monkeypatch)
        monkeypatch.setattr(runtime.depth_safety, "evaluate", lambda **kw: DepthSafetyResult(
            kw["frame_id"], kw["host_monotonic_s"], "observed", 0.1, 10, 1.0))
        pose = RobotPose2(0.55, 0.55, 0.0)
        first = runtime.tick(_motion_packet(0), pose, dt_s=0.04, now_monotonic_s=10.0)
        assert first.command.reason == "front_obstacle_brake"
        tick = runtime.tick(_motion_packet(2), pose, dt_s=0.08, now_monotonic_s=10.08)
        assert tick.command.reason == "collision_recovery"
        assert tick.command.linear_mps == 0.0
        assert tick.command.yaw_rate_radps > 0.0
        assert "progress-goal" in runtime.policy._failed_until_revision
    finally:
        runtime.close()


def test_stale_depth_does_not_trigger_front_brake_recovery(monkeypatch):
    from exploration.depth_safety import DepthSafetyResult
    runtime = _runtime_for_motion_fixture(0.0)
    runtime.config = replace(runtime.config, obstacle_block_timeout_s=0.05)
    try:
        _install_progress_goal(runtime, monkeypatch)
        monkeypatch.setattr(runtime.depth_safety, "evaluate", lambda **kw: DepthSafetyResult(
            kw["frame_id"], kw["host_monotonic_s"], "observed", 0.1, 10, 1.0))
        pose = RobotPose2(0.55, 0.55, 0.0)
        runtime.tick(_motion_packet(0), pose, dt_s=0.04, now_monotonic_s=10.0)
        tick = runtime.tick(_motion_packet(0), pose, dt_s=0.4, now_monotonic_s=10.4)
        assert tick.command.reason == "depth_stale"
        assert runtime._recovery_last_yaw is None
        assert "progress-goal" not in runtime.policy._failed_until_revision
    finally:
        runtime.close()


def test_cleared_front_obstacle_resumes_without_recovery(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    runtime.config = replace(runtime.config, obstacle_block_timeout_s=0.05)
    try:
        _install_progress_goal(runtime, monkeypatch)
        runtime._front_brake_started_s = 10.0
        tick = runtime.tick(_motion_packet(2), RobotPose2(0.55, 0.55, 0.0), dt_s=0.08,
                            now_monotonic_s=10.08)
        assert tick.command.reason == "tracking"
        assert runtime._recovery_last_yaw is None
        assert "progress-goal" not in runtime.policy._failed_until_revision
    finally:
        runtime.close()


def test_old_front_brake_timer_does_not_cancel_new_heading_alignment(monkeypatch):
    import math
    from exploration.depth_safety import DepthSafetyResult
    runtime = _runtime_for_motion_fixture(0.0)
    runtime.config = replace(runtime.config, obstacle_block_timeout_s=0.05)
    try:
        _install_progress_goal(runtime, monkeypatch)
        monkeypatch.setattr(runtime.depth_safety, "evaluate", lambda **kw: DepthSafetyResult(
            kw["frame_id"], kw["host_monotonic_s"], "observed", 0.1, 10, 1.0))
        runtime._front_brake_started_s = 10.0
        tick = runtime.tick(_motion_packet(2), RobotPose2(0.55, 0.55, -math.pi/2), dt_s=0.08,
                            now_monotonic_s=10.08)
        assert tick.command.reason == "obstacle_align_heading"
        assert tick.command.linear_mps == 0.0 and tick.command.yaw_rate_radps > 0.0
        assert runtime._recovery_last_yaw is None
        assert runtime._front_brake_started_s is None
        assert "progress-goal" not in runtime.policy._failed_until_revision
    finally:
        runtime.close()


def test_invalidated_route_displays_wait_but_preserves_goal_for_replanning(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        _install_progress_goal(runtime, monkeypatch)
        runtime._path = None
        runtime._last_plan_s = 10.0
        # Keep a pending planner result rather than running the synchronous
        # policy immediately, reproducing the async path-invalidated state.
        class PendingWorker:
            busy = True

            def poll(self):
                return None

            def close(self, _timeout):
                return True

        runtime._plan_worker = PendingWorker()
        runtime._last_plan_request_s = 10.0
        tick = runtime.tick(_motion_packet(1), RobotPose2(0.55, 0.55, 0.0), dt_s=0.04,
                            now_monotonic_s=10.04)
        assert tick.command.reason == "no_valid_path"
        assert tick.decision.state == "WAIT"
        assert tick.decision.reason == "path_unavailable_waiting_for_replan"
        assert runtime._decision.frontier.frontier_id == "progress-goal"
    finally:
        runtime.close()


def _install_progress_goal(runtime, monkeypatch):
    from exploration.depth_safety import DepthSafetyResult
    snapshot = replace(_grid(0.0, 0.0), host_monotonic_s=10.0)
    goal_cell = (5, 15)
    frontier = Frontier("progress-goal", snapshot.revision, (goal_cell,), goal_cell, 1,
                        snapshot.cell_to_world(*goal_cell))
    path = runtime.planner.plan(snapshot, (0.55, 0.55), goal_cell)
    assert path.success
    decision = NavigationDecision("MOVE", frontier, path, "fixture", 10.0)
    runtime._decision = decision
    runtime._path = path
    runtime._last_plan_s = 10.0
    monkeypatch.setattr(runtime.mapper, "update", lambda packet: snapshot)
    monkeypatch.setattr(runtime.mapper, "snapshot", lambda: snapshot)
    monkeypatch.setattr(runtime.depth_safety, "evaluate", lambda **kw: DepthSafetyResult(
        kw["frame_id"], kw["host_monotonic_s"], "observed", None, 10, 1.0))
    return snapshot, decision


def test_temporary_depth_wait_preserves_active_goal(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        _snapshot, decision = _install_progress_goal(runtime, monkeypatch)
        tick = runtime.tick(_motion_packet(0), RobotPose2(0.55, 0.55, 0.0), dt_s=0.04,
                            now_monotonic_s=10.4)
        assert tick.command.reason == "depth_stale"
        assert tick.decision.state == "WAIT"
        assert runtime._decision is decision
        assert runtime._decision.frontier.frontier_id == "progress-goal"
    finally:
        runtime.close()


def test_stationary_translation_times_out_and_rejects_world_goal(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    runtime.config = replace(runtime.config, progress_timeout_s=0.45)
    try:
        _snapshot, _decision = _install_progress_goal(runtime, monkeypatch)
        pose = RobotPose2(0.55, 0.55, 0.0)
        runtime.tick(_motion_packet(0), pose, dt_s=0.25, now_monotonic_s=10.0)
        runtime.tick(_motion_packet(1), pose, dt_s=0.25, now_monotonic_s=10.04)
        tick = runtime.tick(_motion_packet(2), pose, dt_s=0.25, now_monotonic_s=10.08)
        assert tick.decision.reason == "goal_progress_timeout"
        assert tick.command.linear_mps == 0.0
        assert runtime._rejected_goal_targets[0][0] == (1.55, 0.55)
    finally:
        runtime.close()


def test_async_arrival_survives_new_map_revision(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        snapshot, decision = _install_progress_goal(runtime, monkeypatch)
        arrival = replace(decision, state="OBSERVE", path=None,
                          reason="frontier_viewpoint_reached")
        frontiers = FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (decision.frontier,))
        outcome = PlanOutcome(snapshot.revision, snapshot.as_of_frame_id,
                              snapshot.host_monotonic_s, snapshot.resolution_m,
                              snapshot.world_frame, snapshot.scope_id, frontiers, arrival, 0.01)
        current = replace(snapshot, revision=2, host_monotonic_s=10.1)
        accepted, rebased = runtime._adopt_worker_outcome(
            outcome, current, decision.frontier.viewpoint_world_xy, 10.1)
        assert accepted and rebased
        assert runtime._path is None
        assert runtime._decision.reason == "frontier_viewpoint_reached"
    finally:
        runtime.close()


def test_arrival_clears_path_and_requests_next_goal_promptly(monkeypatch):
    runtime = _runtime_for_motion_fixture(0.0)
    try:
        _snapshot, decision = _install_progress_goal(runtime, monkeypatch)
        goal = decision.frontier.viewpoint_world_xy
        tick = runtime.tick(_motion_packet(0), RobotPose2(*goal, 0.0), dt_s=0.04,
                            now_monotonic_s=10.0)
        assert tick.command.reason == "goal_reached"
        assert tick.decision.reason == "frontier_viewpoint_reached"
        assert runtime._path is None
        assert runtime._last_plan_request_s is None and runtime._last_plan_s is None
    finally:
        runtime.close()
