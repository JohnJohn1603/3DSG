"""Bounded local NavMap trace for diagnosing connectivity changes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from .contracts import GridSnapshot, GridState, RobotPose2


class NavMapTraceRecorder:
    """Keep small robot-centered map patches in memory and persist after a run.

    This avoids synchronous diagnostic disk I/O in the camera/control loop.
    The fixed sample and byte caps bound memory use for long episodes.
    """

    ARRAY_FIELDS = (
        "states", "floor_support", "clearance_evidence",
        "occupancy_log_odds", "spawn_validated",
    )

    def __init__(self, path: str | Path, *, sample_hz: float = 1.0,
                 radius_cells: int = 12, max_samples: int = 128,
                 max_bytes: int = 64 * 1024 * 1024) -> None:
        self.path = Path(path)
        if not np.isfinite(sample_hz) or sample_hz <= 0:
            raise ValueError("trace sample_hz must be finite and positive")
        self.sample_period_s = 1.0 / float(sample_hz)
        self.radius_cells = int(radius_cells)
        self.max_samples = int(max_samples)
        self.max_bytes = int(max_bytes)
        if self.radius_cells < 1 or self.max_samples < 1 or self.max_bytes < 1:
            raise ValueError("trace frequency, radius, and caps must be positive")
        self._samples: list[dict[str, Any]] = []
        self._payload_bytes = 0
        self._last_sample_s: float | None = None
        self._status = "capturing"

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def record(self, *, snapshot: GridSnapshot, robot_pose: RobotPose2,
               elapsed_s: float, decision: Any, command: Any,
               frontier_set: Any, map_worker_pending: bool,
               planner_pending: bool) -> bool:
        elapsed = float(elapsed_s)
        if self._last_sample_s is not None and elapsed - self._last_sample_s < self.sample_period_s:
            return False
        center = snapshot.world_to_cell(robot_pose.x_m, robot_pose.y_m)
        if center is None:
            return False

        radius = self.radius_cells
        side = radius * 2 + 1
        row0, col0 = center[0] - radius, center[1] - radius
        slices = {
            "states": (snapshot.states, np.uint8, int(GridState.UNKNOWN)),
            "floor_support": (snapshot.floor_support, np.uint16, 0),
            "clearance_evidence": (snapshot.clearance_evidence, np.uint16, 0),
            "occupancy_log_odds": (snapshot.occupancy_log_odds, np.float32, 0.0),
            "spawn_validated": (snapshot.spawn_validated, np.bool_, False),
        }
        arrays: dict[str, np.ndarray] = {}
        row_src0, col_src0 = max(0, row0), max(0, col0)
        row_src1 = min(snapshot.shape[0], row0 + side)
        col_src1 = min(snapshot.shape[1], col0 + side)
        for name, (source, dtype, fill) in slices.items():
            patch = np.full((side, side), fill, dtype=dtype)
            if row_src1 > row_src0 and col_src1 > col_src0:
                row_dst0, col_dst0 = row_src0 - row0, col_src0 - col0
                patch[row_dst0:row_dst0 + row_src1 - row_src0,
                      col_dst0:col_dst0 + col_src1 - col_src0] = source[
                          row_src0:row_src1, col_src0:col_src1]
            arrays[name] = patch
        sample_bytes = sum(int(arr.nbytes) for arr in arrays.values())
        if sample_bytes > self.max_bytes:
            self._status = "sample_exceeds_byte_cap"
            return False

        path = None if decision is None else getattr(decision, "path", None)
        frontier = None if decision is None else getattr(decision, "frontier", None)
        path_cells = [] if path is None else [list(cell) for cell in path.cells[:32]]
        metadata = {
            "elapsed_s": elapsed,
            "frame_id": snapshot.as_of_frame_id,
            "frame_index": snapshot.as_of_frame_index,
            "revision": snapshot.revision,
            "host_monotonic_s": snapshot.host_monotonic_s,
            "map_origin_xy": [snapshot.origin_x_m, snapshot.origin_y_m],
            "resolution_m": snapshot.resolution_m,
            "patch_origin_cell": [row0, col0],
            "patch_radius_cells": radius,
            "robot_pose_xy_yaw": [robot_pose.x_m, robot_pose.y_m,
                                   robot_pose.yaw_rad],
            "robot_cell": list(center),
            "robot_cell_state": int(snapshot.states[center]),
            "robot_cell_floor_support": int(snapshot.floor_support[center]),
            "robot_cell_clearance_evidence": int(snapshot.clearance_evidence[center]),
            "robot_cell_spawn_validated": bool(snapshot.spawn_validated[center]),
            "decision_state": None if decision is None else decision.state,
            "decision_reason": None if decision is None else decision.reason,
            "goal_id": None if frontier is None else frontier.frontier_id,
            "path_success": None if path is None else bool(path.success),
            "path_reason": None if path is None else path.reason,
            "path_cost_m": None if path is None else float(path.cost_m),
            "path_cells": path_cells,
            "command_reason": command.reason,
            "command_linear_mps": float(command.linear_mps),
            "command_yaw_rate_radps": float(command.yaw_rate_radps),
            "reachable_free_cells": None if frontier_set is None
            else int(frontier_set.reachable_free_cells),
            "known_free_cells": None if frontier_set is None
            else int(frontier_set.known_free_cells),
            "frontier_count": None if frontier_set is None else len(frontier_set.frontiers),
            # The snapshot being traced and the frontier diagnostics can be
            # produced asynchronously. Preserve both identities so offline
            # audits can detect a mixed-revision sample instead of treating
            # the counts as if they described the captured arrays.
            "frontier_map_revision": None if frontier_set is None
            else getattr(frontier_set, "map_revision", None),
            "frontier_as_of_frame_id": None if frontier_set is None
            else getattr(frontier_set, "as_of_frame_id", None),
            "map_worker_pending": bool(map_worker_pending),
            "planner_pending": bool(planner_pending),
        }
        sample = {"metadata": metadata, "arrays": arrays, "bytes": sample_bytes}
        self._samples.append(sample)
        self._payload_bytes += sample_bytes
        self._last_sample_s = elapsed
        while (len(self._samples) > self.max_samples
               or self._payload_bytes > self.max_bytes):
            removed = self._samples.pop(0)
            self._payload_bytes -= int(removed["bytes"])
        return True

    def close(self, *, status: str = "closed") -> None:
        self._status = str(status)
        if not self._samples:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "schema": "navmap-local-trace/v1",
            "status": self._status,
            "sample_hz": 1.0 / self.sample_period_s,
            "radius_cells": self.radius_cells,
            "sample_count": len(self._samples),
            "payload_bytes": self._payload_bytes,
            "samples": [sample["metadata"] for sample in self._samples],
        }
        arrays = {
            field: np.stack([sample["arrays"][field] for sample in self._samples])
            for field in self.ARRAY_FIELDS
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                np.savez_compressed(
                    stream,
                    metadata_json=np.asarray(json.dumps(metadata, sort_keys=True,
                                                         separators=(",", ":"))),
                    **arrays,
                )
                stream.flush()
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
