"""Fail-fast checks for the reusable M12 Webots world contract."""
from __future__ import annotations

import re
from pathlib import Path


def validate_world_contract(
    world_path: Path,
    config_path: Path,
    config: dict,
    repo_root: Path,
) -> dict:
    """Validate the textual world/config requirements used by m12_motor_capture."""
    world_path = Path(world_path).resolve()
    config_path = Path(config_path).resolve()
    repo_root = Path(repo_root).resolve()
    if not world_path.is_file():
        raise RuntimeError(f"Webots world does not exist: {world_path}")
    declared_world = config.get("world_file")
    if not isinstance(declared_world, str) or not declared_world:
        raise RuntimeError("config must declare world_file")
    expected_world = (repo_root / declared_world).resolve()
    if expected_world != world_path:
        raise RuntimeError(
            f"config.world_file points to {expected_world}, but --world points to {world_path}"
        )
    text = world_path.read_text(encoding="utf-8")
    if not re.search(r"(?m)^DEF\s+WORLD_INFO\s+WorldInfo\s*\{", text):
        raise RuntimeError(
            f"{world_path.name} must declare 'DEF WORLD_INFO WorldInfo'; "
            "the motor controller uses Supervisor.getFromDef('WORLD_INFO')"
        )
    timestep = re.search(r"(?m)^\s*basicTimeStep\s+([0-9]+(?:\.[0-9]+)?)\s*$", text)
    if timestep is None:
        raise RuntimeError(f"{world_path.name} must declare WorldInfo.basicTimeStep")
    configured_timestep = config.get("timing", {}).get("physics_timestep_ms")
    if configured_timestep is None:
        raise RuntimeError("config.timing.physics_timestep_ms is required")
    world_timestep = float(timestep.group(1))
    if abs(float(configured_timestep) - world_timestep) > 1e-9:
        raise RuntimeError(
            f"basicTimeStep mismatch: world={world_timestep:g} ms, "
            f"config.timing.physics_timestep_ms={float(configured_timestep):g} ms"
        )
    world_info = re.search(r"(?m)^\s*coordinateSystem\s+\"([^\"]+)\"\s*$", text)
    configured_coordinate = config.get("coordinate_system")
    if world_info is None:
        raise RuntimeError(f"{world_path.name} must declare WorldInfo.coordinateSystem")
    if world_info.group(1) != configured_coordinate:
        raise RuntimeError(
            f"coordinateSystem mismatch: world={world_info.group(1)!r}, "
            f"config={configured_coordinate!r}"
        )
    if "M12DiffDriveRGBD" not in text:
        raise RuntimeError(
            f"{world_path.name} does not contain M12DiffDriveRGBD; "
            "add the reusable robot PROTO before starting the M12 bridge"
        )
    return {
        "world": str(world_path),
        "config": str(config_path),
        "basic_time_step_ms": world_timestep,
        "coordinate_system": world_info.group(1),
        "robot_proto": "M12DiffDriveRGBD",
    }
