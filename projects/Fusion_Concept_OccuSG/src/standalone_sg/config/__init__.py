"""Validated Phase 1 YAML configuration."""

from .schema import (
    NavigationConfig,
    ObjectAssociationConfig,
    RoomConfig,
    OccupancyConfig,
    Phase1Config,
    ProjectionConfig,
    RegionGuardConfig,
    RegionTrackerConfig,
    load_config,
)

__all__ = [
    "NavigationConfig", "ObjectAssociationConfig", "OccupancyConfig", "Phase1Config", "ProjectionConfig",
    "RegionGuardConfig", "RegionTrackerConfig", "RoomConfig", "load_config",
]
