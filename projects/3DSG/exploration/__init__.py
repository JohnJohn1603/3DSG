"""Observed-map frontier exploration primitives.

This package deliberately has no Habitat, Qt, or perception dependency. Simulator
and UI adapters can be layered on top without making the navigation core depend
on scene ground truth or a particular runtime installation.
"""

from .contracts import (
    ActionCommand,
    CameraIntrinsics,
    FramePacket,
    GridSnapshot,
    GridState,
    PathResult,
    Pose3,
    RobotPose2,
)
from .nav_mapper import NavMapper, NavMapperConfig
from .frontier import FrontierExtractor, FrontierSet
from .planner import AStarPlanner, PlannerConfig
from .controller import Controller, ControllerConfig
from .habitat_online import HabitatOnlineProducer, HabitatStep, RGBFrame
from .depth_safety import DepthSafetyGate, DepthSafetyConfig, DepthSafetyResult
from .policy import FrontierPolicy, NavigationDecision, PolicyConfig
from .keyframe_scheduler import (
    AdmissionDecision, KeyframeScheduler, PersistedKeyframe, WorkerLease,
)
from .keyframe_store import KeyframeStore
from .telemetry import EventJournal
from .supervisor import EpisodeSnapshot, EpisodeState, EpisodeSupervisor
from .runtime import NavigationRuntime, NavigationRuntimeConfig, NavigationTick

__all__ = [
    "ActionCommand", "CameraIntrinsics", "FramePacket", "GridSnapshot",
    "GridState", "PathResult", "Pose3", "RobotPose2", "NavMapper", "NavMapperConfig",
    "FrontierExtractor", "FrontierSet", "AStarPlanner", "PlannerConfig",
    "Controller", "ControllerConfig",
    "HabitatOnlineProducer", "HabitatStep", "RGBFrame",
    "DepthSafetyGate", "DepthSafetyConfig", "DepthSafetyResult",
    "FrontierPolicy", "NavigationDecision", "PolicyConfig",
    "AdmissionDecision", "KeyframeScheduler", "PersistedKeyframe", "WorkerLease",
    "KeyframeStore",
    "EventJournal",
    "EpisodeSnapshot", "EpisodeState", "EpisodeSupervisor",
    "NavigationRuntime", "NavigationRuntimeConfig", "NavigationTick",
]
