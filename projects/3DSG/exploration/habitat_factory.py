"""Concrete CPU Habitat-Sim bootstrap and incremental action adapter.

Habitat imports are lazy so the navigation core remains usable in a plain
NumPy environment. The factory loads a hash-locked scene and navmesh, seeds
one validated spawn, and never queries a scene-wide path.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .contracts import ActionCommand, CameraIntrinsics, RobotPose2
from .habitat_online import HabitatOnlineProducer


_CANONICAL_FROM_HABITAT = np.array(
    [[1.0, 0.0, 0.0, 0.0],
     [0.0, 0.0, -1.0, 0.0],
     [0.0, 1.0, 0.0, 0.0],
     [0.0, 0.0, 0.0, 1.0]], dtype=np.float64,
)
_HABITAT_SENSOR_FROM_OPTICAL = np.diag([1.0, -1.0, -1.0, 1.0])
_STEP_DISTANCE_TOLERANCE_M = 1e-6


def _actual_step_displacement_valid(delta_habitat: np.ndarray, increment_m: float,
                                    max_vertical_delta_m: float) -> bool:
    """Check horizontal action distance separately from navmesh height projection."""
    delta = np.asarray(delta_habitat, dtype=np.float64)
    if delta.shape != (3,) or not np.isfinite(delta).all():
        return False
    horizontal = float(np.linalg.norm(delta[[0, 2]]))
    vertical = float(abs(delta[1]))
    return (0.5 * increment_m - _STEP_DISTANCE_TOLERANCE_M
            <= horizontal <= 1.5 * increment_m + _STEP_DISTANCE_TOLERANCE_M
            and vertical <= max_vertical_delta_m)


@dataclass
class HabitatBundle:
    simulator: Any
    agent: Any
    producer: HabitatOnlineProducer
    scene_config: Mapping[str, Any]
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            self.simulator.close()
            self._closed = True

    def __enter__(self) -> "HabitatBundle":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_scene_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve(strict=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("schema") != "frontier-habitat-scene/v1":
        raise ValueError(f"unsupported Habitat scene config schema in {config_path}")
    provenance = config.get("provenance", {})
    if provenance.get("spawn_only") is not True:
        raise ValueError("scene config must declare a spawn-only source contract")
    if "route" in config or "route_points" in config or "waypoints" in config:
        raise ValueError("frontier scene config must not contain a route or waypoint list")
    hash_fields = {
        "scene_asset": "scene_asset_sha256",
        "scene_dataset_config": "scene_dataset_config_sha256",
        "navmesh_file": "navmesh_sha256",
    }
    for key, hash_field in hash_fields.items():
        value = Path(config[key]).expanduser().resolve(strict=True)
        expected = str(config[hash_field])
        actual = _sha256(value)
        if not expected or actual != expected:
            raise RuntimeError(f"{key} hash mismatch: expected {expected}, got {actual}")
        config[key] = str(value)
    route_index = provenance.get("spawn_source_route_index")
    if route_index is not None:
        if type(route_index) is not int or route_index != 0:
            raise ValueError("only the original initial spawn may be imported from a route artifact")
    elif (provenance.get("spawn_source_kind") != "reached_viewpoint"
          or not str(provenance.get("spawn_source_run", "")).strip()
          or type(provenance.get("spawn_source_frame_index")) is not int
          or provenance["spawn_source_frame_index"] < 0):
        raise ValueError("non-route spawns require provenance for a previously reached viewpoint")
    if "floor_z_m" in config and not math.isfinite(float(config["floor_z_m"])):
        raise ValueError("configured floor_z_m must be finite in canonical world coordinates")
    source_config = Path(provenance["source_effective_config"]).expanduser().resolve(strict=True)
    source_hash = str(provenance.get("source_effective_config_sha256", ""))
    if not source_hash or _sha256(source_config) != source_hash:
        raise RuntimeError("source effective-config hash mismatch")
    return config


def _build_pose_providers(quaternion_module: Any):
    def rotation_matrix(rotation: Any) -> np.ndarray:
        matrix = np.asarray(quaternion_module.as_rotation_matrix(rotation), dtype=np.float64)
        if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
            raise RuntimeError("Habitat returned an invalid quaternion rotation")
        return matrix

    def sensor_pose(agent: Any, sensor_uuid: str) -> np.ndarray:
        sensor = agent.get_state().sensor_states[sensor_uuid]
        T_habitat_sensor = np.eye(4, dtype=np.float64)
        T_habitat_sensor[:3, :3] = rotation_matrix(sensor.rotation)
        T_habitat_sensor[:3, 3] = np.asarray(tuple(sensor.position), dtype=np.float64)
        return _CANONICAL_FROM_HABITAT @ T_habitat_sensor @ _HABITAT_SENSOR_FROM_OPTICAL

    def robot_pose(agent: Any) -> RobotPose2:
        state = agent.get_state()
        position_h = np.asarray(tuple(state.position), dtype=np.float64)
        position_w = _CANONICAL_FROM_HABITAT[:3, :3] @ position_h
        forward_h = rotation_matrix(state.rotation) @ np.array([0.0, 0.0, -1.0])
        forward_w = _CANONICAL_FROM_HABITAT[:3, :3] @ forward_h
        return RobotPose2(float(position_w[0]), float(position_w[1]),
                          math.atan2(float(forward_w[1]), float(forward_w[0])))

    return (lambda agent: sensor_pose(agent, "depth_sensor"),
            lambda agent: sensor_pose(agent, "nav_depth_sensor"), robot_pose)


class HabitatIncrementalExecutor:
    """Quantize velocity commands into NavMesh-checked simulator actions."""

    def __init__(self, *, step_s: float, move_increment_m: float,
                 turn_increment_deg: float, vector_factory: Any,
                 quaternion_module: Any) -> None:
        self.step_s = float(step_s)
        self.move_increment_m = float(move_increment_m)
        self.turn_increment_rad = math.radians(float(turn_increment_deg))
        if min(self.step_s, self.move_increment_m, self.turn_increment_rad) <= 0:
            raise ValueError("Habitat action time and increments must be positive")
        self.vector_factory = vector_factory
        self.quaternion_module = quaternion_module
        self._linear_remainder_m = 0.0
        self._yaw_remainder_rad = 0.0

    def __call__(self, simulator: Any, command: ActionCommand) -> Mapping[str, Any]:
        # Observation processing can run slower than the simulator's fixed
        # physics step. Integrate the bounded command interval while retaining
        # exactly one small, collision-filtered action per observation.
        execution_dt_s = self.step_s
        if "execution_dt_s" in command.metadata:
            try:
                execution_dt_s = float(command.metadata["execution_dt_s"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("execution_dt_s must be finite") from exc
            if not math.isfinite(execution_dt_s):
                raise ValueError("execution_dt_s must be finite")
            execution_dt_s = min(0.10, max(0.0, execution_dt_s))
        if command.linear_mps <= 0.0:
            self._linear_remainder_m = 0.0
        if command.yaw_rate_radps == 0.0:
            self._yaw_remainder_rad = 0.0
        elif self._yaw_remainder_rad * command.yaw_rate_radps < 0.0:
            self._yaw_remainder_rad = 0.0
        self._linear_remainder_m = min(
            self.move_increment_m,
            self._linear_remainder_m + max(0.0, command.linear_mps) * execution_dt_s,
        )
        self._yaw_remainder_rad = max(-self.turn_increment_rad, min(
            self.turn_increment_rad,
            self._yaw_remainder_rad + command.yaw_rate_radps * execution_dt_s,
        ))
        move_due = self._linear_remainder_m + 1e-9 >= self.move_increment_m
        turn_due = abs(self._yaw_remainder_rad) + 1e-9 >= self.turn_increment_rad

        # Rotate first when both axes request an action. Keep at most one move
        # pulse pending so stale commands cannot accumulate into a long burst.
        if turn_due:
            action = "turn_left" if self._yaw_remainder_rad > 0 else "turn_right"
            self._yaw_remainder_rad -= math.copysign(self.turn_increment_rad, self._yaw_remainder_rad)
        elif move_due:
            action = "move_forward"
            self._linear_remainder_m = max(0.0, self._linear_remainder_m - self.move_increment_m)
        else:
            action = "stop"

        agent = simulator.get_agent(0)
        navmesh_projection_details = None
        if action == "move_forward":
            before = np.asarray(tuple(agent.get_state().position), dtype=np.float64)
            rotation = np.asarray(self.quaternion_module.as_rotation_matrix(agent.get_state().rotation),
                                  dtype=np.float64)
            expected = before + rotation @ np.array([0.0, 0.0, -self.move_increment_m])
            filtered = np.asarray(tuple(simulator.pathfinder.try_step_no_sliding(
                self.vector_factory(before), self.vector_factory(expected))), dtype=np.float64)
            filter_distance_m = (float(np.linalg.norm(filtered - expected))
                                 if filtered.shape == (3,) and np.isfinite(filtered).all()
                                 else None)
            horizontal_delta_m = (float(np.linalg.norm((filtered - expected)[[0, 2]]))
                                  if filtered.shape == (3,) and np.isfinite(filtered).all()
                                  else None)
            vertical_delta_m = (float(abs(filtered[1] - expected[1]))
                                if filtered.shape == (3,) and np.isfinite(filtered).all()
                                else None)
            navigability_max_y_delta_m = 0.05
            endpoint_navigable = (bool(simulator.pathfinder.is_navigable(
                self.vector_factory(filtered), max_y_delta=navigability_max_y_delta_m))
                if filtered.shape == (3,) and np.isfinite(filtered).all() else False)
            rejection_reasons = []
            if filtered.shape != (3,) or not np.isfinite(filtered).all():
                rejection_reasons.append("invalid_filtered_endpoint")
            else:
                intended_horizontal = expected[[0, 2]] - before[[0, 2]]
                intended_norm = float(np.linalg.norm(intended_horizontal))
                projected_horizontal = filtered[[0, 2]] - before[[0, 2]]
                if intended_norm > 1e-9:
                    direction = intended_horizontal / intended_norm
                    forward_progress = float(projected_horizontal @ direction)
                    lateral_error = float(np.linalg.norm(
                        projected_horizontal - forward_progress * direction))
                else:
                    forward_progress = 0.0
                    lateral_error = math.inf
                projected_step_valid = (
                    0.5 * self.move_increment_m - _STEP_DISTANCE_TOLERANCE_M
                    <= forward_progress
                    <= 1.5 * self.move_increment_m + _STEP_DISTANCE_TOLERANCE_M
                    and lateral_error <= 0.001
                    and vertical_delta_m is not None
                    and vertical_delta_m <= navigability_max_y_delta_m
                )
                if not projected_step_valid:
                    rejection_reasons.append("filtered_step_displacement_invalid")
                elif horizontal_delta_m is not None and horizontal_delta_m > 0.001:
                    # The preflight filter may shorten a step at a baked
                    # NavMesh boundary. Accept only a forward projection that
                    # still meets the minimum step and lateral tolerance
                    # checked again after the simulator move below.
                    navmesh_projection_details = {
                        "expected_endpoint_habitat_m": expected.tolist(),
                        "filtered_endpoint_habitat_m": filtered.tolist(),
                        "filtered_endpoint_delta_m": filter_distance_m,
                        "forward_progress_m": forward_progress,
                        "lateral_error_m": lateral_error,
                        "filtered_endpoint_navigable": endpoint_navigable,
                    }
            if not endpoint_navigable:
                rejection_reasons.append("filtered_endpoint_not_navigable")
            if rejection_reasons:
                self._linear_remainder_m = 0.0
                observations = simulator.step("stop", dt=self.step_s)
                blocked_world = _CANONICAL_FROM_HABITAT[:3, :3] @ expected
                return {**observations, "collided": True, "_frontier_action": "move_blocked",
                        "_collision_position_world_xy": (float(blocked_world[0]),
                                                          float(blocked_world[1])),
                        "_collision_filter_details": {
                            "reason": rejection_reasons,
                            "expected_endpoint_habitat_m": expected.tolist(),
                            "filtered_endpoint_habitat_m": filtered.tolist()
                            if filtered.shape == (3,) and np.isfinite(filtered).all() else None,
                            "filtered_endpoint_delta_m": filter_distance_m,
                            "filtered_endpoint_horizontal_delta_m": horizontal_delta_m,
                            "filtered_endpoint_vertical_delta_m": vertical_delta_m,
                            "forward_progress_m": forward_progress if filtered.shape == (3,) else None,
                            "lateral_error_m": lateral_error if filtered.shape == (3,) else None,
                            "step_distance_tolerance_m": _STEP_DISTANCE_TOLERANCE_M,
                            "filtered_endpoint_navigable": endpoint_navigable,
                            "navigability_max_y_delta_m": navigability_max_y_delta_m,
                        }}

        observations = simulator.step(action, dt=self.step_s)
        if action == "move_forward":
            after = np.asarray(tuple(agent.get_state().position), dtype=np.float64)
            actual_delta = after - before
            vertical_tolerance_m = 0.05
            endpoint_navigable = (after.shape == (3,) and np.isfinite(after).all()
                                  and bool(simulator.pathfinder.is_navigable(
                                      self.vector_factory(after),
                                      max_y_delta=vertical_tolerance_m)))
            if (not _actual_step_displacement_valid(
                    actual_delta, self.move_increment_m, vertical_tolerance_m)
                    or not endpoint_navigable):
                horizontal = (float(np.linalg.norm(actual_delta[[0, 2]]))
                              if actual_delta.shape == (3,) and np.isfinite(actual_delta).all()
                              else math.inf)
                vertical = (float(abs(actual_delta[1]))
                            if actual_delta.shape == (3,) and np.isfinite(actual_delta).all()
                            else math.inf)
                raise RuntimeError(
                    "Habitat move step had unexpected endpoint: "
                    f"horizontal={horizontal:.4f} m vertical={vertical:.4f} m "
                    f"navigable={endpoint_navigable}"
                )
            if (navmesh_projection_details is not None
                    and np.linalg.norm(after - filtered) > 0.002):
                raise RuntimeError(
                    "Habitat projected move endpoint differs from NavMesh preflight: "
                    f"delta={np.linalg.norm(after - filtered):.4f} m"
                )
        result = {**observations, "_frontier_action": action}
        if navmesh_projection_details is not None:
            result["_navmesh_projection_details"] = navmesh_projection_details
        return result


def create_habitat_bundle(config_path: str | Path, *, run_id: str) -> HabitatBundle:
    """Create one CPU Habitat instance and set its validated initial pose once."""
    if not run_id:
        raise ValueError("run_id is required")
    try:
        import habitat_sim
        import magnum as mn
        import quaternion
        from habitat_sim.utils.common import quat_from_angle_axis
    except ImportError as exc:
        raise RuntimeError("Habitat-Sim, Magnum, and numpy-quaternion must be installed in this environment") from exc

    config = load_scene_config(config_path)
    agent_cfg_data = config["agent"]
    camera = config["camera"]
    simulation = config["simulation"]
    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.scene_id = config["scene_asset"]
    sim_cfg.scene_dataset_config_file = config["scene_dataset_config"]
    sim_cfg.gpu_device_id = int(simulation["gpu_device_id"])
    sim_cfg.enable_physics = bool(simulation["enable_physics"])
    sim_cfg.allow_sliding = bool(simulation["allow_sliding"])
    sim_cfg.random_seed = int(config["seed"])

    agent_cfg = habitat_sim.AgentConfiguration()
    agent_cfg.radius = float(agent_cfg_data["navmesh_baked_radius_m"])
    agent_cfg.height = float(agent_cfg_data["navmesh_baked_height_m"])
    agent_cfg.action_space = {
        # Habitat-Sim 0.3.x has no built-in "stop" control. Register the
        # launcher key as a zero-distance forward control to advance time and
        # render a fresh observation without moving the agent.
        "stop": habitat_sim.ActionSpec("move_forward", habitat_sim.ActuationSpec(amount=0.0)),
        "move_forward": habitat_sim.ActionSpec("move_forward", habitat_sim.ActuationSpec(
            amount=float(simulation["move_increment_m"]))),
        "turn_left": habitat_sim.ActionSpec("turn_left", habitat_sim.ActuationSpec(
            amount=float(simulation["turn_increment_deg"]))),
        "turn_right": habitat_sim.ActionSpec("turn_right", habitat_sim.ActuationSpec(
            amount=float(simulation["turn_increment_deg"]))),
    }
    resolution = mn.Vector2i(int(camera["height"]), int(camera["width"]))
    sensor_position = mn.Vector3(*map(float, agent_cfg_data["sensor_position_habitat_m"]))
    sensor_orientation = mn.Vector3(*map(float, agent_cfg_data["sensor_orientation_habitat_rad"]))
    sensor_specs = [(habitat_sim.SensorType.COLOR, "color_sensor", sensor_orientation,
                     resolution, float(camera["depth_min_m"]), float(camera["depth_max_m"])),
                    (habitat_sim.SensorType.DEPTH, "depth_sensor", sensor_orientation,
                     resolution, float(camera["depth_min_m"]), float(camera["depth_max_m"]))]
    nav_depth = config.get("navigation_depth")
    if nav_depth is not None and nav_depth.get("enabled", False):
        nav_orientation = mn.Vector3(*map(float, nav_depth["orientation_habitat_rad"]))
        nav_resolution = mn.Vector2i(int(nav_depth["height"]), int(nav_depth["width"]))
        sensor_specs.append((habitat_sim.SensorType.DEPTH, "nav_depth_sensor", nav_orientation,
                             nav_resolution, float(nav_depth["depth_min_m"]),
                             float(nav_depth["depth_max_m"])))
    for sensor_type, sensor_uuid, orientation, sensor_resolution, near, far in sensor_specs:
        spec = habitat_sim.CameraSensorSpec()
        spec.uuid = sensor_uuid
        spec.sensor_type = sensor_type
        spec.resolution = sensor_resolution
        nav_mount = (nav_depth.get("sensor_position_habitat_m")
                     if sensor_uuid == "nav_depth_sensor" and nav_depth is not None else None)
        spec.position = (mn.Vector3(*map(float, nav_mount))
                         if nav_mount is not None else sensor_position)
        spec.orientation = orientation
        spec.hfov = mn.Deg(float(camera["horizontal_fov_deg"]))
        spec.near = near
        spec.far = far
        if sensor_type == habitat_sim.SensorType.DEPTH and hasattr(spec, "normalize_depth"):
            spec.normalize_depth = False
        agent_cfg.sensor_specifications.append(spec)

    simulator = habitat_sim.Simulator(habitat_sim.Configuration(sim_cfg, [agent_cfg]))
    try:
        if not simulator.pathfinder.load_nav_mesh(config["navmesh_file"]):
            raise RuntimeError("Habitat failed to load the configured navmesh")
        settings = simulator.pathfinder.nav_mesh_settings
        if settings is None:
            raise RuntimeError("loaded Habitat navmesh does not expose its geometry settings")
        for actual, key in ((float(settings.agent_radius), "navmesh_baked_radius_m"),
                            (float(settings.agent_height), "navmesh_baked_height_m")):
            if not math.isclose(actual, float(agent_cfg_data[key]), rel_tol=0.0, abs_tol=1e-6):
                raise RuntimeError(f"configured agent geometry does not match navmesh {key}")

        vector_factory = lambda values: mn.Vector3(*[float(v) for v in values])
        spawn = config["spawn"]
        initial_state = habitat_sim.AgentState()
        initial_position = np.asarray(spawn["position_habitat_m"], dtype=np.float64)
        initial_state.position = vector_factory(initial_position)
        initial_state.rotation = quat_from_angle_axis(
            float(spawn["yaw_habitat_rad"]), np.array([0.0, 1.0, 0.0], dtype=np.float32))
        agent = simulator.get_agent(0)
        if not simulator.pathfinder.is_navigable(vector_factory(initial_position), max_y_delta=0.05):
            raise RuntimeError("configured initial spawn is not navigable")
        camera_pose_provider, nav_depth_pose_provider, robot_pose_provider = _build_pose_providers(quaternion)
        intrinsics = _intrinsics(camera)
        nav_depth_intrinsics = (_intrinsics({**camera,
                                              "width": int(nav_depth["width"]),
                                              "height": int(nav_depth["height"])})
                                if nav_depth is not None and nav_depth.get("enabled", False)
                                else None)
        executor = HabitatIncrementalExecutor(
            step_s=float(simulation["step_s"]),
            move_increment_m=float(simulation["move_increment_m"]),
            turn_increment_deg=float(simulation["turn_increment_deg"]),
            vector_factory=vector_factory, quaternion_module=quaternion)
        default_floor_z_m = float(_CANONICAL_FROM_HABITAT[2, :3] @ initial_position)
        floor_z_m = float(config.get("floor_z_m", default_floor_z_m))
        if abs(floor_z_m - default_floor_z_m) > 1.0:
            raise ValueError("configured floor_z_m differs from spawn elevation by more than 1 m")
        producer = HabitatOnlineProducer(
            simulator=simulator, agent=agent, run_id=run_id, scene_id=config["scene_id"],
            calibration_hash=config["camera"]["calibration_id"], intrinsics=intrinsics,
            pose_provider=camera_pose_provider, robot_pose_provider=robot_pose_provider,
            nav_depth_pose_provider=nav_depth_pose_provider if nav_depth_intrinsics else None,
            action_executor=executor, habitat_vector_factory=vector_factory,
            depth_min_m=float(camera["depth_min_m"]), depth_max_m=float(camera["depth_max_m"]),
            floor_z_m=floor_z_m,
            nav_depth_intrinsics=nav_depth_intrinsics,
            nav_depth_min_m=float(nav_depth["depth_min_m"]) if nav_depth_intrinsics else None,
            nav_depth_max_m=float(nav_depth["depth_max_m"]) if nav_depth_intrinsics else None,
            nav_depth_update_period_frames=(int(nav_depth.get("update_period_frames", 1))
                                            if nav_depth_intrinsics else 1),
            geometry_contract={
                "navmesh_baked_agent_radius_m": float(settings.agent_radius),
                "navmesh_baked_agent_height_m": float(settings.agent_height),
            })
        producer.initialize_spawn(initial_state)
        return HabitatBundle(simulator, agent, producer, config)
    except Exception:
        simulator.close()
        raise


def _intrinsics(camera: Mapping[str, Any]) -> CameraIntrinsics:
    width, height = int(camera["width"]), int(camera["height"])
    hfov = math.radians(float(camera["horizontal_fov_deg"]))
    fx = width / (2.0 * math.tan(hfov / 2.0))
    fy = height / (2.0 * math.tan(math.atan(math.tan(hfov / 2.0) * height / width)))
    return CameraIntrinsics(fx, fy, (width - 1) / 2.0, (height - 1) / 2.0, width, height)
