"""Geometry-first keyframe proposals for asynchronous scene-graph updates."""

from __future__ import annotations

import math

from .contracts import RobotPose2
from .policy import NavigationDecision


class KeyframePolicy:
    """Admit first view, novel translation/rotation and reached viewpoints."""

    def __init__(self, *, min_translation_m: float = 0.25,
                 min_yaw_rad: float = math.radians(20.0)) -> None:
        if min_translation_m <= 0 or min_yaw_rad <= 0:
            raise ValueError("keyframe thresholds must be positive")
        self.min_translation_m = float(min_translation_m)
        self.min_yaw_rad = float(min_yaw_rad)
        self._last_pose: RobotPose2 | None = None
        self._previous_yaw: float | None = None
        self._yaw_since_admission = 0.0
        self._seen_frontiers: set[str] = set()

    def propose(self, pose: RobotPose2, decision: NavigationDecision | None,
                frontier_ids: set[str]) -> tuple[bool, str | None, float]:
        delta_yaw = 0.0
        if self._previous_yaw is not None:
            delta_yaw = _wrap(pose.yaw_rad - self._previous_yaw)
            self._yaw_since_admission += abs(delta_yaw)
        self._previous_yaw = pose.yaw_rad
        if self._last_pose is None:
            return True, "first_view", 1.0

        distance = math.hypot(pose.x_m - self._last_pose.x_m,
                              pose.y_m - self._last_pose.y_m)
        reached = decision is not None and decision.state in {"OBSERVE", "REACHED"}
        new_ids = frontier_ids - self._seen_frontiers
        new_frontier = bool(new_ids)
        reason = None
        protected = None
        if reached and decision.frontier is not None:
            reason = "frontier_viewpoint"
            protected = "frontier_viewpoint"
        elif distance >= self.min_translation_m or self._yaw_since_admission >= self.min_yaw_rad:
            reason = "novel_view"
        elif new_frontier:
            reason = "new_region"
            protected = "new_region"
        if reason is None:
            return False, None, 0.0
        novelty = max(0.01, distance + self._yaw_since_admission * 0.25
                      + 0.1 * len(new_ids))
        return True, protected or reason, novelty

    def commit(self, pose: RobotPose2, frontier_ids: set[str]) -> None:
        """Advance admission baselines only after durable queue admission."""
        self._last_pose = pose
        self._seen_frontiers.update(frontier_ids)
        self._yaw_since_admission = 0.0


def _wrap(angle: float) -> float:
    return (angle + math.pi) % math.tau - math.pi
