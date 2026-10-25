"""Pure-Python contracts shared by the Webots motor controller and replay.

This module deliberately has no Webots or NumPy dependency so the geometry and
wheel-command contracts can be regression-tested outside the simulator.
"""
from __future__ import annotations

import math
import queue


def slew_command(current: float, desired: float, max_accel: float, dt: float) -> float:
    if not all(math.isfinite(float(v)) for v in (current, desired, max_accel, dt)):
        raise ValueError("wheel command slew inputs must be finite")
    if max_accel < 0.0 or dt < 0.0:
        raise ValueError("max_accel and dt must be non-negative")
    limit = max_accel * dt
    return current + max(-limit, min(limit, desired - current))


def capture_due(physics_step_index: int, cadence_steps: int) -> bool:
    if cadence_steps < 1 or physics_step_index < 0:
        raise ValueError("physics step and capture cadence must be non-negative/positive")
    return physics_step_index > 0 and physics_step_index % cadence_steps == 0


def timeout_reached(now: float, started: float, limit: float) -> bool:
    if not all(math.isfinite(float(v)) for v in (now, started, limit)) or limit < 0.0:
        raise ValueError("timeout inputs must be finite and limit must be non-negative")
    return now - started > limit


def enqueue_bounded_or_raise(packet_queue, item, timeout_s: float) -> None:
    if not math.isfinite(float(timeout_s)) or timeout_s < 0.0:
        raise ValueError("queue timeout must be finite and non-negative")
    try:
        packet_queue.put(item, timeout=float(timeout_s))
    except queue.Full as exc:
        raise RuntimeError("live packet queue full; sender blocked until timeout") from exc


def validate_collision_config(collision: dict) -> None:
    if not isinstance(collision, dict):
        raise ValueError("collision must be an object")
    radius = float(collision["robot_radius_m"])
    tolerance = float(collision.get("contact_tolerance_m", 1e-3))
    if not math.isfinite(radius) or radius <= 0.0:
        raise ValueError("collision.robot_radius_m must be finite and positive")
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise ValueError("collision.contact_tolerance_m must be finite and non-negative")
    count = 0
    for field in ("wall_aabbs_xy", "obstacle_aabbs_xy"):
        boxes = collision.get(field, [])
        if not isinstance(boxes, list):
            raise ValueError(f"collision.{field} must be a list")
        for index, box in enumerate(boxes):
            if not isinstance(box, dict) or not isinstance(box.get("min"), list) or not isinstance(box.get("max"), list):
                raise ValueError(f"collision.{field}[{index}] must contain min/max")
            if len(box["min"]) != 2 or len(box["max"]) != 2:
                raise ValueError(f"collision.{field}[{index}] must be planar")
            low = [float(v) for v in box["min"]]
            high = [float(v) for v in box["max"]]
            if not all(math.isfinite(v) for v in low + high) or not (low[0] < high[0] and low[1] < high[1]):
                raise ValueError(f"collision.{field}[{index}] has invalid bounds")
            count += 1
    if count == 0:
        raise ValueError("collision must declare at least one wall or obstacle AABB")


def collision_report(x: float, y: float, collision: dict) -> dict:
    """Classify circle-vs-authored-AABB geometry at one physics timestep."""
    validate_collision_config(collision)
    radius = float(collision["robot_radius_m"])
    tolerance = float(collision.get("contact_tolerance_m", 1e-3))
    checks = []
    for kind, field in (("wall", "wall_aabbs_xy"), ("obstacle", "obstacle_aabbs_xy")):
        for box in collision.get(field, []):
            low = [float(v) for v in box["min"]]
            high = [float(v) for v in box["max"]]
            dx = max(low[0] - x, 0.0, x - high[0])
            dy = max(low[1] - y, 0.0, y - high[1])
            distance = math.hypot(dx, dy)
            clearance = distance - radius
            if clearance < -tolerance:
                state = "penetration"
            elif abs(clearance) <= tolerance:
                state = "contact"
            else:
                state = "clear"
            checks.append({
                "name": str(box.get("name", f"{kind}_{len(checks)}")),
                "kind": kind,
                "state": state,
                "distance_m": distance,
                "clearance_m": clearance,
            })
    checks.sort(key=lambda item: item["clearance_m"])
    minimum = checks[0]["clearance_m"]
    penetrations = [item["name"] for item in checks if item["state"] == "penetration"]
    contacts = [item["name"] for item in checks if item["state"] == "contact"]
    return {
        "state": "penetration" if penetrations else ("contact" if contacts else "clear"),
        "minimum_clearance_m": minimum,
        "nearest_name": checks[0]["name"],
        "penetration_names": penetrations,
        "contact_names": contacts,
        "checks": len(checks),
    }


def summarize_collision_trace(trace: list[dict]) -> dict:
    if not trace:
        raise ValueError("physics trace must not be empty")
    states = {"clear": 0, "contact": 0, "penetration": 0}
    minimum = float("inf")
    contact_names = set()
    penetration_names = set()
    for row in trace:
        state = row["collision_state"]
        if state not in states:
            raise ValueError(f"invalid collision state {state!r}")
        states[state] += 1
        minimum = min(minimum, float(row["minimum_clearance_m"]))
        contact_names.update(row.get("contact_names", []))
        penetration_names.update(row.get("penetration_names", []))
    return {
        "status": "pass" if states["penetration"] == 0 else "penetration_detected",
        "checked_physics_steps": len(trace),
        "state_counts": states,
        "minimum_clearance_m": minimum,
        "contact_names": sorted(contact_names),
        "penetration_names": sorted(penetration_names),
        "contact_is_not_penetration": True,
    }
