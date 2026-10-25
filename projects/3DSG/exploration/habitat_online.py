"""Thin online adapter for an already-configured Habitat-Sim instance.

This module intentionally does not resolve routes, query full-scene occupancy,
or call PathFinder.find_path. The caller supplies the project's validated pose
conversion and a one-step collision-filtered action executor.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from .contracts import ActionCommand, CameraIntrinsics, FramePacket, Pose3, RobotPose2


@dataclass(frozen=True)
class RGBFrame:
    run_id: str
    scene_id: str
    frame_id: str
    frame_index: int
    simulator_time_s: float
    host_monotonic_s: float
    rgb: np.ndarray


@dataclass(frozen=True)
class HabitatStep:
    rgb: RGBFrame
    packet: FramePacket
    robot_pose: RobotPose2
    collided: bool
    action: ActionCommand | None
    executed_action: str | None = None
    collision_position_world_xy: tuple[float, float] | None = None
    collision_filter_details: Mapping[str, Any] | None = None
    navmesh_projection_details: Mapping[str, Any] | None = None


PoseProvider = Callable[[Any], np.ndarray]
RobotPoseProvider = Callable[[Any], RobotPose2]
ActionExecutor = Callable[[Any, ActionCommand], Mapping[str, Any]]
HabitatVectorFactory = Callable[[np.ndarray], Any]


def _host_monotonic_domain() -> str:
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if not boot_id_path.is_file():
        raise RuntimeError("cannot identify host monotonic clock domain")
    boot_id = boot_id_path.read_text(encoding="ascii").strip()
    if not boot_id:
        raise RuntimeError("host monotonic clock boot ID is empty")
    return f"host_monotonic:{boot_id}"


class HabitatOnlineProducer:
    """Capture latest RGB-D and execute incremental commands on one simulator.

    ``pose_provider`` must return the sensor's optical camera-to-canonical
    world transform using the validated Habitat sensor extrinsics and the same
    basis conversion as the existing project. ``action_executor`` must perform
    a single incremental move/turn through Habitat's movement filter. These
    callbacks are mandatory so this adapter cannot silently invent either
    transform or collision semantics.
    """

    def __init__(self, *, simulator: Any, agent: Any, run_id: str, scene_id: str,
                 calibration_hash: str, intrinsics: CameraIntrinsics,
                 pose_provider: PoseProvider, robot_pose_provider: RobotPoseProvider,
                 nav_depth_pose_provider: PoseProvider | None = None,
                 nav_depth_intrinsics: CameraIntrinsics | None = None,
                 nav_depth_min_m: float | None = None,
                 nav_depth_max_m: float | None = None,
                 nav_depth_update_period_frames: int = 1,
                 action_executor: ActionExecutor,
                 habitat_vector_factory: HabitatVectorFactory,
                 depth_min_m: float, depth_max_m: float, floor_z_m: float,
                 geometry_contract: Mapping[str, float],
                 rgb_sensor_uuid: str = "color_sensor",
                 depth_sensor_uuid: str = "depth_sensor",
                 nav_depth_sensor_uuid: str = "nav_depth_sensor") -> None:
        if not run_id or not scene_id or not calibration_hash:
            raise ValueError("run, scene, and calibration IDs are required")
        self.sim = simulator
        self.agent = agent
        self.run_id = run_id
        self.scene_id = scene_id
        self.calibration_hash = calibration_hash
        self.intrinsics = intrinsics
        self.pose_provider = pose_provider
        self.robot_pose_provider = robot_pose_provider
        self.nav_depth_pose_provider = nav_depth_pose_provider
        self.nav_depth_intrinsics = nav_depth_intrinsics
        self.nav_depth_min_m = nav_depth_min_m
        self.nav_depth_max_m = nav_depth_max_m
        self.nav_depth_update_period_frames = int(nav_depth_update_period_frames)
        self.action_executor = action_executor
        self.habitat_vector_factory = habitat_vector_factory
        self.depth_min_m = float(depth_min_m)
        self.depth_max_m = float(depth_max_m)
        self.floor_z_m = float(floor_z_m)
        self.rgb_sensor_uuid = rgb_sensor_uuid
        self.depth_sensor_uuid = depth_sensor_uuid
        self.nav_depth_sensor_uuid = nav_depth_sensor_uuid
        nav_configuration = (nav_depth_pose_provider, nav_depth_intrinsics,
                            nav_depth_min_m, nav_depth_max_m)
        if any(value is not None for value in nav_configuration):
            if any(value is None for value in nav_configuration):
                raise ValueError("navigation depth sensor configuration must be complete")
            if (not math.isfinite(float(nav_depth_min_m))
                    or not math.isfinite(float(nav_depth_max_m))
                    or float(nav_depth_min_m) <= 0
                    or float(nav_depth_max_m) <= float(nav_depth_min_m)):
                raise ValueError("navigation depth range must be positive and ordered")
            if self.nav_depth_update_period_frames < 1:
                raise ValueError("navigation depth update period must be positive")
        try:
            self.agent_radius_m = float(geometry_contract["navmesh_baked_agent_radius_m"])
            self.agent_height_m = float(geometry_contract["navmesh_baked_agent_height_m"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("geometry contract must provide serialized navmesh radius and height") from exc
        if (not math.isfinite(self.depth_min_m) or not math.isfinite(self.depth_max_m)
                or self.depth_min_m <= 0 or self.depth_max_m <= self.depth_min_m):
            raise ValueError("depth limits must be finite, positive, and ordered")
        self._frame_index = 0
        self._last_sim_time: float | None = None
        self._spawn_initialized = False

    def validate_geometry_contract(self, *, tolerance_m: float = 1e-6) -> None:
        """Fail closed when requested geometry differs from the loaded navmesh."""
        pathfinder = self.sim.pathfinder
        if not pathfinder.is_loaded:
            raise RuntimeError("Habitat PathFinder must have a loaded navmesh")
        settings = pathfinder.nav_mesh_settings
        if settings is None:
            raise RuntimeError("loaded navmesh does not expose serialized agent geometry")
        actual = float(settings.agent_radius), float(settings.agent_height)
        expected = self.agent_radius_m, self.agent_height_m
        if any(not math.isclose(a, e, rel_tol=0.0, abs_tol=tolerance_m)
               for a, e in zip(actual, expected)):
            raise RuntimeError(f"geometry contract {expected} differs from loaded navmesh {actual}")

    def initialize_spawn(self, habitat_agent_state: Any) -> None:
        """Set the initial pose exactly once, after an explicit spawn check."""
        if self._spawn_initialized:
            raise RuntimeError("initial spawn has already been set; runtime pose assignment is forbidden")
        self.validate_geometry_contract()
        position = np.asarray(habitat_agent_state.position, dtype=np.float64)
        if position.shape != (3,) or not np.isfinite(position).all():
            raise ValueError("spawn position must be a finite 3-vector")
        if not bool(self.sim.pathfinder.is_navigable(self.habitat_vector_factory(position))):
            raise ValueError("configured spawn is not navigable for the loaded navmesh")
        self.agent.set_state(habitat_agent_state, infer_sensor_states=True)
        self._spawn_initialized = True

    def observe(self, observations: Mapping[str, Any] | None = None,
                action: ActionCommand | None = None) -> HabitatStep:
        if not self._spawn_initialized:
            raise RuntimeError("call initialize_spawn before capturing observations")
        observations = self.sim.get_sensor_observations() if observations is None else observations
        required = {self.rgb_sensor_uuid, self.depth_sensor_uuid}
        if self.nav_depth_intrinsics is not None:
            required.add(self.nav_depth_sensor_uuid)
        missing = required - observations.keys()
        if missing:
            raise RuntimeError(f"Habitat observation is missing sensors: {sorted(missing)}")
        rgb = np.asarray(observations[self.rgb_sensor_uuid])
        depth = np.asarray(observations[self.depth_sensor_uuid], dtype=np.float32)
        expected_shape = (self.intrinsics.height, self.intrinsics.width)
        if rgb.ndim != 3 or rgb.shape[:2] != expected_shape or rgb.shape[2] not in (3, 4):
            raise RuntimeError(f"RGB shape {rgb.shape} does not match configured image profile")
        if depth.shape != expected_shape:
            raise RuntimeError(f"depth shape {depth.shape} does not match configured image profile")
        rgb = np.ascontiguousarray(rgb[..., :3].astype(np.uint8, copy=False))
        depth = np.ascontiguousarray(depth.copy())
        valid = np.isfinite(depth) & (depth >= self.depth_min_m) & (depth <= self.depth_max_m)
        depth[~valid] = np.nan
        nav_depth = None
        if self.nav_depth_intrinsics is not None:
            nav_depth = np.asarray(observations[self.nav_depth_sensor_uuid], dtype=np.float32)
            expected_nav_shape = (self.nav_depth_intrinsics.height,
                                  self.nav_depth_intrinsics.width)
            if nav_depth.shape != expected_nav_shape:
                raise RuntimeError("navigation depth shape does not match its calibration")
            if self._frame_index % self.nav_depth_update_period_frames == 0:
                nav_depth = np.ascontiguousarray(nav_depth.copy())
                nav_valid = (np.isfinite(nav_depth)
                             & (nav_depth >= float(self.nav_depth_min_m))
                             & (nav_depth <= float(self.nav_depth_max_m)))
                nav_depth[~nav_valid] = np.nan
            else:
                nav_depth = None

        sim_time = float(self.sim.get_world_time())
        if not math.isfinite(sim_time) or (self._last_sim_time is not None and sim_time <= self._last_sim_time):
            raise RuntimeError("Habitat simulation clock did not advance monotonically")
        self._last_sim_time = sim_time
        host_time = time.monotonic()
        wall_time = time.time()
        monotonic_domain = _host_monotonic_domain()
        frame_id = f"{self.run_id}:{self.frame_index:08d}"
        T_world_camera = Pose3(self.pose_provider(self.agent), frame_id)
        robot_pose = self.robot_pose_provider(self.agent)
        if not isinstance(robot_pose, RobotPose2):
            raise TypeError("robot_pose_provider must return canonical-world RobotPose2")
        packet = FramePacket(
            run_id=self.run_id, scene_id=self.scene_id, frame_id=frame_id,
            frame_index=self.frame_index, simulator_time_s=sim_time,
            host_monotonic_s=host_time, depth_m=depth,
            intrinsics=self.intrinsics, T_world_camera=T_world_camera,
            floor_z_m=self.floor_z_m, calibration_hash=self.calibration_hash,
            depth_min_m=self.depth_min_m, depth_max_m=self.depth_max_m,
            capture_wall_time_unix_s=wall_time,
            capture_monotonic_domain=monotonic_domain,
            nav_depth_m=nav_depth,
            nav_depth_intrinsics=self.nav_depth_intrinsics if nav_depth is not None else None,
            T_world_nav_depth_camera=(Pose3(self.nav_depth_pose_provider(self.agent), frame_id)
                                      if nav_depth is not None else None),
            nav_depth_min_m=self.nav_depth_min_m if nav_depth is not None else None,
            nav_depth_max_m=self.nav_depth_max_m if nav_depth is not None else None,
        )
        rgb.setflags(write=False)
        rgb_frame = RGBFrame(self.run_id, self.scene_id, frame_id, self.frame_index,
                             sim_time, host_time, rgb)
        self._frame_index += 1
        return HabitatStep(rgb_frame, packet, robot_pose,
                           bool(observations.get("collided", False)), action,
                           None if observations.get("_frontier_action") is None
                           else str(observations["_frontier_action"]),
                           None if observations.get("_collision_position_world_xy") is None
                           else tuple(map(float, observations["_collision_position_world_xy"])),
                           observations.get("_collision_filter_details"),
                           observations.get("_navmesh_projection_details"))

    @property
    def frame_index(self) -> int:
        return self._frame_index

    def step(self, command: ActionCommand) -> HabitatStep:
        """Apply one bounded action, then return its post-action observation."""
        if not self._spawn_initialized:
            raise RuntimeError("call initialize_spawn before stepping the simulator")
        if command.valid_until_monotonic_s < time.monotonic():
            stopped = ActionCommand(0.0, 0.0, "expired_action_brake", command.frame_id,
                                    command.map_revision, time.monotonic() + 0.04)
            command = stopped
        result = self.action_executor(self.sim, command)
        if not isinstance(result, Mapping):
            raise TypeError("action_executor must return Habitat observation mapping")
        return self.observe(result, action=command)
