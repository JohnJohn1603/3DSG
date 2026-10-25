"""Stable ports for using 3DSG as one replaceable ObjectNav module.

The contracts intentionally describe data and lifecycle, not a particular
simulator, detector, graph mapper, task benchmark or UI toolkit. Implementations
can be supplied by Habitat now and other ObjectNav hosts later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .contracts import ActionCommand, FramePacket, GridSnapshot, RobotPose2
from .habitat_online import RGBFrame


MODULE_API_VERSION = "3dsg-objectnav-module/v1"


@dataclass(frozen=True)
class ObjectNavTask:
    task_id: str
    mode: str = "frontier_exploration"
    target_concepts: tuple[str, ...] = ()
    budget_s: float = 600.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id is required")
        if self.mode not in {"frontier_exploration", "object_nav", "replay"}:
            raise ValueError(f"unsupported task mode: {self.mode}")
        if not math.isfinite(float(self.budget_s)) or self.budget_s <= 0:
            raise ValueError("task budget must be finite and positive")
        if self.mode == "object_nav" and not self.target_concepts:
            raise ValueError("object_nav tasks require at least one target concept")
        object.__setattr__(self, "target_concepts", tuple(str(x) for x in self.target_concepts))


@dataclass(frozen=True)
class ObservationFrame:
    rgb: RGBFrame
    packet: FramePacket
    robot_pose: RobotPose2
    collided: bool = False
    collision_position_world_xy: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        rgb_identity = (self.rgb.run_id, self.rgb.scene_id, self.rgb.frame_id, self.rgb.frame_index)
        packet_identity = (self.packet.run_id, self.packet.scene_id,
                           self.packet.frame_id, self.packet.frame_index)
        if rgb_identity != packet_identity:
            raise ValueError("RGB and RGB-D packet identity mismatch")


@dataclass(frozen=True)
class GraphRevision:
    revision: int
    as_of_frame_id: str
    backend: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ModuleFrameResult:
    observation_id: str
    command: ActionCommand
    navigation: GridSnapshot
    graph: GraphRevision | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class NavigationBackend(Protocol):
    """Fast geometry-only local navigation port; must not wait for graph work."""

    def update(self, observation: ObservationFrame, *, dt_s: float) -> ModuleFrameResult: ...


@runtime_checkable
class PerceptionBackend(Protocol):
    """Optional semantic frontend, usually invoked only on admitted keyframes."""

    def update_frame(self, observation: ObservationFrame) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


@runtime_checkable
class SceneGraphBackend(Protocol):
    """Stateful graph backend consuming the perception result for one frame."""

    def update_frame(self, observation: ObservationFrame,
                     perception: Mapping[str, Any]) -> GraphRevision: ...
    def close(self) -> None: ...


@runtime_checkable
class TargetSelector(Protocol):
    """ObjectNav extension point for ranking frontiers/views using task targets."""

    def select(self, task: ObjectNavTask, *, candidates: Sequence[Any],
               graph: GraphRevision | None, navigation: GridSnapshot) -> Any: ...


@runtime_checkable
class ObjectNavModule(Protocol):
    """Host-facing module lifecycle shared by simulator and benchmark adapters."""

    api_version: str

    def submit_task(self, task: ObjectNavTask) -> None: ...
    def on_observation(self, observation: ObservationFrame, *, dt_s: float) -> ModuleFrameResult: ...
    def close(self) -> None: ...
