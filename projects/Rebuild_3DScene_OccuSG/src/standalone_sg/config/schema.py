"""Strict, deterministic configuration schema for M0/M1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from numbers import Real
from typing import Any, Mapping

import yaml

from ..contracts.errors import ContractError


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ContractError(f"unknown config key(s) at {path}: {', '.join(unknown)}")


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be a mapping")
    return value


def _require_int(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ContractError(f"{path} must be an integer, got {type(value).__name__}")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{path} must be a boolean, got {type(value).__name__}")
    return value


def _require_str(value: Any, path: str) -> str:
    if type(value) is not str:
        raise ContractError(f"{path} must be a string, got {type(value).__name__}")
    return value


def _require_finite_real(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ContractError(f"{path} must be a real number, got {type(value).__name__}")
    result = float(value)
    if not np_isfinite(result):
        raise ContractError(f"{path} must be finite")
    return result


def np_isfinite(value: float) -> bool:
    # Keep schema.py dependency-light while rejecting YAML NaN/Inf explicitly.
    return value == value and value not in (float("inf"), float("-inf"))


@dataclass(frozen=True, slots=True)
class CoordinateConfig:
    world_frame: str = "world"
    camera_optical_frame: str = "camera_optical"
    base_frame: str = "base"
    world_up_axis: str = "Z"
    grid_row_axis: str = "+Y"
    grid_col_axis: str = "+X"
    transform_convention: str = "T_parent_child_column_vector"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CoordinateConfig":
        _reject_unknown(
            value,
            {
                "world_frame",
                "camera_optical_frame",
                "base_frame",
                "world_up_axis",
                "grid_row_axis",
                "grid_col_axis",
                "transform_convention",
            },
            "coordinate",
        )
        defaults = cls()
        result = cls(
            world_frame=_require_str(value.get("world_frame", defaults.world_frame), "coordinate.world_frame"),
            camera_optical_frame=_require_str(
                value.get("camera_optical_frame", defaults.camera_optical_frame),
                "coordinate.camera_optical_frame",
            ),
            base_frame=_require_str(value.get("base_frame", defaults.base_frame), "coordinate.base_frame"),
            world_up_axis=_require_str(value.get("world_up_axis", defaults.world_up_axis), "coordinate.world_up_axis"),
            grid_row_axis=_require_str(value.get("grid_row_axis", defaults.grid_row_axis), "coordinate.grid_row_axis"),
            grid_col_axis=_require_str(value.get("grid_col_axis", defaults.grid_col_axis), "coordinate.grid_col_axis"),
            transform_convention=_require_str(
                value.get("transform_convention", defaults.transform_convention),
                "coordinate.transform_convention",
            ),
        )
        if result.world_up_axis != "Z" or result.grid_row_axis != "+Y" or result.grid_col_axis != "+X":
            raise ContractError("M1 coordinate axes must be world Z-up, rows +Y, columns +X")
        if result.transform_convention != "T_parent_child_column_vector":
            raise ContractError("unsupported transform convention")
        return result


@dataclass(frozen=True, slots=True)
class BackProjectionConfig:
    stride: int = 1
    min_depth_m: float = 0.1
    max_depth_m: float = 10.0
    depth_scale_by_encoding: tuple[tuple[str, float], ...] = (("16UC1", 0.001), ("32FC1", 1.0))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BackProjectionConfig":
        _reject_unknown(value, {"stride", "min_depth_m", "max_depth_m", "depth_scale_by_encoding"}, "backprojection")
        raw_scales = value.get("depth_scale_by_encoding", {"16UC1": 0.001, "32FC1": 1.0})
        raw_scales = _require_mapping(raw_scales, "backprojection.depth_scale_by_encoding")
        scales = tuple(
            sorted(
                (
                    _require_str(key, "backprojection.depth_scale_by_encoding key"),
                    _require_finite_real(scale, f"backprojection.depth_scale_by_encoding[{key!r}]"),
                )
                for key, scale in raw_scales.items()
            )
        )
        defaults = cls()
        result = cls(
            stride=_require_int(value.get("stride", defaults.stride), "backprojection.stride"),
            min_depth_m=_require_finite_real(value.get("min_depth_m", defaults.min_depth_m), "backprojection.min_depth_m"),
            max_depth_m=_require_finite_real(value.get("max_depth_m", defaults.max_depth_m), "backprojection.max_depth_m"),
            depth_scale_by_encoding=scales,
        )
        if result.stride < 1 or result.max_depth_m <= result.min_depth_m:
            raise ContractError("invalid backprojection stride/depth range")
        if any(scale <= 0 for _, scale in result.depth_scale_by_encoding):
            raise ContractError("depth scales must be positive")
        return result

    def scale_for(self, encoding: str) -> float:
        for key, scale in self.depth_scale_by_encoding:
            if key == encoding:
                return scale
        raise ContractError(f"no depth scale configured for encoding {encoding!r}")


@dataclass(frozen=True, slots=True)
class NativeConfig:
    build_dir: str = "build/native-probe"
    cxx_standard: int = 17
    require_reference_backends: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NativeConfig":
        _reject_unknown(value, {"build_dir", "cxx_standard", "require_reference_backends"}, "native")
        defaults = cls()
        result = cls(
            build_dir=_require_str(value.get("build_dir", defaults.build_dir), "native.build_dir"),
            cxx_standard=_require_int(value.get("cxx_standard", defaults.cxx_standard), "native.cxx_standard"),
            require_reference_backends=_require_bool(
                value.get("require_reference_backends", defaults.require_reference_backends),
                "native.require_reference_backends",
            ),
        )
        if result.cxx_standard < 17:
            raise ContractError("native.cxx_standard must be at least 17")
        return result


@dataclass(frozen=True, slots=True)
class OccupancyConfig:
    resolution_m: float = 0.05
    max_range_m: float = 8.0
    prob_hit: float = 0.7
    prob_miss: float = 0.4
    clamping_min: float = 0.12
    clamping_max: float = 0.97
    occupancy_threshold: float = 0.5

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OccupancyConfig":
        _reject_unknown(
            value,
            {
                "resolution_m",
                "max_range_m",
                "prob_hit",
                "prob_miss",
                "clamping_min",
                "clamping_max",
                "occupancy_threshold",
            },
            "occupancy",
        )
        defaults = cls()
        result = cls(
            resolution_m=_require_finite_real(value.get("resolution_m", defaults.resolution_m), "occupancy.resolution_m"),
            max_range_m=_require_finite_real(value.get("max_range_m", defaults.max_range_m), "occupancy.max_range_m"),
            prob_hit=_require_finite_real(value.get("prob_hit", defaults.prob_hit), "occupancy.prob_hit"),
            prob_miss=_require_finite_real(value.get("prob_miss", defaults.prob_miss), "occupancy.prob_miss"),
            clamping_min=_require_finite_real(value.get("clamping_min", defaults.clamping_min), "occupancy.clamping_min"),
            clamping_max=_require_finite_real(value.get("clamping_max", defaults.clamping_max), "occupancy.clamping_max"),
            occupancy_threshold=_require_finite_real(
                value.get("occupancy_threshold", defaults.occupancy_threshold),
                "occupancy.occupancy_threshold",
            ),
        )
        if result.resolution_m <= 0 or result.max_range_m <= 0:
            raise ContractError("occupancy resolution and max_range_m must be positive")
        for name, probability in (
            ("prob_hit", result.prob_hit),
            ("prob_miss", result.prob_miss),
            ("clamping_min", result.clamping_min),
            ("clamping_max", result.clamping_max),
            ("occupancy_threshold", result.occupancy_threshold),
        ):
            if not 0.0 < probability < 1.0:
                raise ContractError(f"occupancy.{name} must be strictly between 0 and 1")
        if result.clamping_min >= result.clamping_max:
            raise ContractError("occupancy.clamping_min must be less than clamping_max")
        return result


@dataclass(frozen=True, slots=True)
class ProjectionConfig:
    min_free_height_m: float = 0.3
    minimum_occupancy: int = 5
    interval_epsilon_fraction: float = 0.001

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProjectionConfig":
        _reject_unknown(
            value,
            {"min_free_height_m", "minimum_occupancy", "interval_epsilon_fraction"},
            "projection",
        )
        defaults = cls()
        result = cls(
            min_free_height_m=_require_finite_real(
                value.get("min_free_height_m", defaults.min_free_height_m),
                "projection.min_free_height_m",
            ),
            minimum_occupancy=_require_int(
                value.get("minimum_occupancy", defaults.minimum_occupancy),
                "projection.minimum_occupancy",
            ),
            interval_epsilon_fraction=_require_finite_real(
                value.get("interval_epsilon_fraction", defaults.interval_epsilon_fraction),
                "projection.interval_epsilon_fraction",
            ),
        )
        if result.min_free_height_m < 0:
            raise ContractError("projection.min_free_height_m must be non-negative")
        if not 0 <= result.minimum_occupancy <= 100:
            raise ContractError("projection.minimum_occupancy must be in 0..100")
        if result.interval_epsilon_fraction < 0:
            raise ContractError("projection.interval_epsilon_fraction must be non-negative")
        return result


@dataclass(frozen=True, slots=True)
class RegionTrackerConfig:
    enabled: bool = True
    min_bbox_iou: float = 0.20
    max_centroid_distance_m: float = 1.5
    min_area_ratio: float = 0.25
    max_area_ratio: float = 4.0
    max_missed_updates: int = 3

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RegionTrackerConfig":
        _reject_unknown(
            value,
            {
                "enabled", "min_bbox_iou", "max_centroid_distance_m",
                "min_area_ratio", "max_area_ratio", "max_missed_updates",
            },
            "regions.tracker",
        )
        defaults = cls()
        result = cls(
            enabled=_require_bool(value.get("enabled", defaults.enabled), "regions.tracker.enabled"),
            min_bbox_iou=_require_finite_real(
                value.get("min_bbox_iou", defaults.min_bbox_iou), "regions.tracker.min_bbox_iou"
            ),
            max_centroid_distance_m=_require_finite_real(
                value.get("max_centroid_distance_m", defaults.max_centroid_distance_m),
                "regions.tracker.max_centroid_distance_m",
            ),
            min_area_ratio=_require_finite_real(
                value.get("min_area_ratio", defaults.min_area_ratio), "regions.tracker.min_area_ratio"
            ),
            max_area_ratio=_require_finite_real(
                value.get("max_area_ratio", defaults.max_area_ratio), "regions.tracker.max_area_ratio"
            ),
            max_missed_updates=_require_int(
                value.get("max_missed_updates", defaults.max_missed_updates),
                "regions.tracker.max_missed_updates",
            ),
        )
        if not 0.0 <= result.min_bbox_iou <= 1.0:
            raise ContractError("regions.tracker.min_bbox_iou must be in 0..1")
        if result.max_centroid_distance_m < 0.0:
            raise ContractError("regions.tracker.max_centroid_distance_m must be non-negative")
        if result.min_area_ratio <= 0.0 or result.max_area_ratio < result.min_area_ratio:
            raise ContractError("regions.tracker area-ratio limits must be positive and ordered")
        if result.max_missed_updates < 0:
            raise ContractError("regions.tracker.max_missed_updates must be non-negative")
        return result


@dataclass(frozen=True, slots=True)
class RegionGuardConfig:
    preserve_previous_on_invalid_update: bool = True
    publish_stale_regions_on_reject: bool = True
    min_valid_region_count: int = 1
    max_unknown_ratio_for_update: float = 0.98
    max_region_area_drop_ratio: float = 0.85
    max_region_count_drop_ratio: float = 0.85

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RegionGuardConfig":
        _reject_unknown(
            value,
            {
                "preserve_previous_on_invalid_update", "publish_stale_regions_on_reject",
                "min_valid_region_count", "max_unknown_ratio_for_update",
                "max_region_area_drop_ratio", "max_region_count_drop_ratio",
            },
            "regions.guard",
        )
        defaults = cls()
        result = cls(
            preserve_previous_on_invalid_update=_require_bool(
                value.get("preserve_previous_on_invalid_update", defaults.preserve_previous_on_invalid_update),
                "regions.guard.preserve_previous_on_invalid_update",
            ),
            publish_stale_regions_on_reject=_require_bool(
                value.get("publish_stale_regions_on_reject", defaults.publish_stale_regions_on_reject),
                "regions.guard.publish_stale_regions_on_reject",
            ),
            min_valid_region_count=_require_int(
                value.get("min_valid_region_count", defaults.min_valid_region_count),
                "regions.guard.min_valid_region_count",
            ),
            max_unknown_ratio_for_update=_require_finite_real(
                value.get("max_unknown_ratio_for_update", defaults.max_unknown_ratio_for_update),
                "regions.guard.max_unknown_ratio_for_update",
            ),
            max_region_area_drop_ratio=_require_finite_real(
                value.get("max_region_area_drop_ratio", defaults.max_region_area_drop_ratio),
                "regions.guard.max_region_area_drop_ratio",
            ),
            max_region_count_drop_ratio=_require_finite_real(
                value.get("max_region_count_drop_ratio", defaults.max_region_count_drop_ratio),
                "regions.guard.max_region_count_drop_ratio",
            ),
        )
        if result.min_valid_region_count < 0:
            raise ContractError("regions.guard.min_valid_region_count must be non-negative")
        if not 0.0 <= result.max_unknown_ratio_for_update <= 1.0:
            raise ContractError("regions.guard.max_unknown_ratio_for_update must be in 0..1")
        if result.max_region_area_drop_ratio < 0.0 or result.max_region_count_drop_ratio < 0.0:
            raise ContractError("regions.guard drop ratios must be non-negative")
        return result


@dataclass(frozen=True, slots=True)
class ObjectAssociationConfig:
    same_class_radius_m: float = 0.75
    same_pose_suppression_cap_m: float = 0.15

    def __post_init__(self) -> None:
        for name in ("same_class_radius_m", "same_pose_suppression_cap_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real) or not np_isfinite(float(value)):
                raise ContractError(f"objects.association.{name} must be a finite real number")
            if value < 0.0:
                raise ContractError(f"objects.association.{name} must be non-negative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ObjectAssociationConfig":
        _reject_unknown(value, {"same_class_radius_m", "same_pose_suppression_cap_m"}, "objects.association")
        defaults = cls()
        result = cls(
            same_class_radius_m=_require_finite_real(
                value.get("same_class_radius_m", defaults.same_class_radius_m),
                "objects.association.same_class_radius_m",
            ),
            same_pose_suppression_cap_m=_require_finite_real(
                value.get("same_pose_suppression_cap_m", defaults.same_pose_suppression_cap_m),
                "objects.association.same_pose_suppression_cap_m",
            ),
        )
        if result.same_class_radius_m < 0.0:
            raise ContractError("objects.association.same_class_radius_m must be non-negative")
        if result.same_pose_suppression_cap_m < 0.0:
            raise ContractError("objects.association.same_pose_suppression_cap_m must be non-negative")
        return result


@dataclass(frozen=True, slots=True)
class NavigationConfig:
    cell_stride_cells: int = 10
    min_free_cell_count: int = 50
    connectivity: int = 8
    resolution_change_tolerance_m: float = 0.0001

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NavigationConfig":
        _reject_unknown(value, {"cell_stride_cells", "min_free_cell_count", "connectivity",
                                "resolution_change_tolerance_m"}, "navigation")
        defaults=cls()
        result=cls(
            cell_stride_cells=_require_int(value.get("cell_stride_cells",defaults.cell_stride_cells),"navigation.cell_stride_cells"),
            min_free_cell_count=_require_int(value.get("min_free_cell_count",defaults.min_free_cell_count),"navigation.min_free_cell_count"),
            connectivity=_require_int(value.get("connectivity",defaults.connectivity),"navigation.connectivity"),
            resolution_change_tolerance_m=_require_finite_real(value.get("resolution_change_tolerance_m",defaults.resolution_change_tolerance_m),"navigation.resolution_change_tolerance_m"),
        )
        if result.cell_stride_cells < 1 or result.min_free_cell_count < 1:
            raise ContractError("navigation stride and free-cell threshold must be positive")
        if result.connectivity not in (4,8):
            raise ContractError("navigation.connectivity must be 4 or 8")
        if result.resolution_change_tolerance_m < 0:
            raise ContractError("navigation.resolution_change_tolerance_m must be non-negative")
        return result


@dataclass(frozen=True, slots=True)
class RoomConfig:
    boundary_epsilon_m: float = 0.15
    nav_neighbor_tiebreak: bool = True
    maintenance_hz: float = 0.5
    relink_min_old_overlap_ratio: float = 0.65
    relink_min_member_support_ratio: float = 0.5
    relink_min_score_margin: float = 0.15

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RoomConfig":
        _reject_unknown(value,{"boundary_epsilon_m","nav_neighbor_tiebreak","maintenance_hz",
                               "relink_min_old_overlap_ratio","relink_min_member_support_ratio",
                               "relink_min_score_margin"},"rooms")
        defaults=cls()
        result=cls(
            boundary_epsilon_m=_require_finite_real(value.get("boundary_epsilon_m",defaults.boundary_epsilon_m),"rooms.boundary_epsilon_m"),
            nav_neighbor_tiebreak=_require_bool(value.get("nav_neighbor_tiebreak",defaults.nav_neighbor_tiebreak),"rooms.nav_neighbor_tiebreak"),
            maintenance_hz=_require_finite_real(value.get("maintenance_hz",defaults.maintenance_hz),"rooms.maintenance_hz"),
            relink_min_old_overlap_ratio=_require_finite_real(value.get("relink_min_old_overlap_ratio",defaults.relink_min_old_overlap_ratio),"rooms.relink_min_old_overlap_ratio"),
            relink_min_member_support_ratio=_require_finite_real(value.get("relink_min_member_support_ratio",defaults.relink_min_member_support_ratio),"rooms.relink_min_member_support_ratio"),
            relink_min_score_margin=_require_finite_real(value.get("relink_min_score_margin",defaults.relink_min_score_margin),"rooms.relink_min_score_margin"),
        )
        if result.boundary_epsilon_m < 0 or result.maintenance_hz <= 0:
            raise ContractError("rooms boundary tolerance must be non-negative and maintenance_hz positive")
        if not 0 <= result.relink_min_old_overlap_ratio <= 1 or not 0 <= result.relink_min_member_support_ratio <= 1 or result.relink_min_score_margin < 0:
            raise ContractError("rooms relink limits are invalid")
        return result


@dataclass(frozen=True, slots=True)
class Phase1Config:
    schema_version: int
    profile: str
    mode: str
    reference_target: str
    parameter_profile: str
    source: Mapping[str, Any]
    coordinate: CoordinateConfig
    backprojection: BackProjectionConfig
    native: NativeConfig
    occupancy: OccupancyConfig
    projection: ProjectionConfig
    region_tracker: RegionTrackerConfig
    region_guard: RegionGuardConfig
    object_association: ObjectAssociationConfig
    navigation: NavigationConfig
    rooms: RoomConfig

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Phase1Config":
        _reject_unknown(
            value,
            {
                "schema_version",
                "profile",
                "mode",
                "reference_target",
                "parameter_profile",
                "source",
                "coordinate",
                "backprojection",
                "native",
                "occupancy",
                "projection",
                "regions",
                "objects",
                "navigation",
                "rooms",
            },
            "root",
        )
        source = _require_mapping(value.get("source", {}), "source")
        _reject_unknown(source, {"baseline", "commit", "path", "license_status"}, "source")
        regions = _require_mapping(value.get("regions", {}), "regions")
        _reject_unknown(regions, {"tracker", "guard"}, "regions")
        objects = _require_mapping(value.get("objects", {}), "objects")
        _reject_unknown(objects, {"association"}, "objects")
        source_values = dict(source)
        for key, source_value in source_values.items():
            _require_str(source_value, f"source.{key}")
        schema_version = _require_int(value.get("schema_version", 1), "schema_version")
        profile = _require_str(value.get("profile", "reference_code"), "profile")
        mode = _require_str(value.get("mode", "reference"), "mode")
        reference_target = _require_str(value.get("reference_target", "code"), "reference_target")
        parameter_profile = _require_str(
            value.get("parameter_profile", "node_default"), "parameter_profile"
        )
        result = cls(
            schema_version=schema_version,
            profile=profile,
            mode=mode,
            reference_target=reference_target,
            parameter_profile=parameter_profile,
            source=source_values,
            coordinate=CoordinateConfig.from_mapping(_require_mapping(value.get("coordinate", {}), "coordinate")),
            backprojection=BackProjectionConfig.from_mapping(
                _require_mapping(value.get("backprojection", {}), "backprojection")
            ),
            native=NativeConfig.from_mapping(_require_mapping(value.get("native", {}), "native")),
            occupancy=OccupancyConfig.from_mapping(
                _require_mapping(value.get("occupancy", {}), "occupancy")
            ),
            projection=ProjectionConfig.from_mapping(
                _require_mapping(value.get("projection", {}), "projection")
            ),
            region_tracker=RegionTrackerConfig.from_mapping(
                _require_mapping(
                    regions.get("tracker", {}),
                    "regions.tracker",
                )
            ),
            region_guard=RegionGuardConfig.from_mapping(
                _require_mapping(
                    regions.get("guard", {}),
                    "regions.guard",
                )
            ),
            object_association=ObjectAssociationConfig.from_mapping(
                _require_mapping(objects.get("association", {}), "objects.association")
            ),
            navigation=NavigationConfig.from_mapping(_require_mapping(value.get("navigation", {}), "navigation")),
            rooms=RoomConfig.from_mapping(_require_mapping(value.get("rooms", {}), "rooms")),
        )
        if result.schema_version != 1:
            raise ContractError("unsupported config schema_version")
        if result.mode not in {"reference", "extended"}:
            raise ContractError("mode must be reference or extended")
        if result.reference_target not in {"code", "paper"}:
            raise ContractError("reference_target must be code or paper")
        if result.parameter_profile not in {"node_default", "mp3d_audit", "fixture"}:
            raise ContractError("parameter_profile must be node_default, mp3d_audit, or fixture")
        if result.mode == "reference" and result.reference_target != "code":
            raise ContractError("M0 baseline configuration must use reference_target=code")
        if not result.source.get("commit"):
            raise ContractError("source.commit is required")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile": self.profile,
            "mode": self.mode,
            "reference_target": self.reference_target,
            "parameter_profile": self.parameter_profile,
            "source": dict(self.source),
            "coordinate": {
                "world_frame": self.coordinate.world_frame,
                "camera_optical_frame": self.coordinate.camera_optical_frame,
                "base_frame": self.coordinate.base_frame,
                "world_up_axis": self.coordinate.world_up_axis,
                "grid_row_axis": self.coordinate.grid_row_axis,
                "grid_col_axis": self.coordinate.grid_col_axis,
                "transform_convention": self.coordinate.transform_convention,
            },
            "backprojection": {
                "stride": self.backprojection.stride,
                "min_depth_m": self.backprojection.min_depth_m,
                "max_depth_m": self.backprojection.max_depth_m,
                "depth_scale_by_encoding": dict(self.backprojection.depth_scale_by_encoding),
            },
            "native": {
                "build_dir": self.native.build_dir,
                "cxx_standard": self.native.cxx_standard,
                "require_reference_backends": self.native.require_reference_backends,
            },
            "occupancy": {
                "resolution_m": self.occupancy.resolution_m,
                "max_range_m": self.occupancy.max_range_m,
                "prob_hit": self.occupancy.prob_hit,
                "prob_miss": self.occupancy.prob_miss,
                "clamping_min": self.occupancy.clamping_min,
                "clamping_max": self.occupancy.clamping_max,
                "occupancy_threshold": self.occupancy.occupancy_threshold,
            },
            "projection": {
                "min_free_height_m": self.projection.min_free_height_m,
                "minimum_occupancy": self.projection.minimum_occupancy,
                "interval_epsilon_fraction": self.projection.interval_epsilon_fraction,
            },
            "navigation": {
                "cell_stride_cells": self.navigation.cell_stride_cells,
                "min_free_cell_count": self.navigation.min_free_cell_count,
                "connectivity": self.navigation.connectivity,
                "resolution_change_tolerance_m": self.navigation.resolution_change_tolerance_m,
            },
            "rooms": {
                "boundary_epsilon_m": self.rooms.boundary_epsilon_m,
                "nav_neighbor_tiebreak": self.rooms.nav_neighbor_tiebreak,
                "maintenance_hz": self.rooms.maintenance_hz,
                "relink_min_old_overlap_ratio": self.rooms.relink_min_old_overlap_ratio,
                "relink_min_member_support_ratio": self.rooms.relink_min_member_support_ratio,
                "relink_min_score_margin": self.rooms.relink_min_score_margin,
            },
            "objects": {
                "association": {
                    "same_class_radius_m": self.object_association.same_class_radius_m,
                    "same_pose_suppression_cap_m": self.object_association.same_pose_suppression_cap_m,
                },
            },
            "regions": {
                "tracker": {
                    "enabled": self.region_tracker.enabled,
                    "min_bbox_iou": self.region_tracker.min_bbox_iou,
                    "max_centroid_distance_m": self.region_tracker.max_centroid_distance_m,
                    "min_area_ratio": self.region_tracker.min_area_ratio,
                    "max_area_ratio": self.region_tracker.max_area_ratio,
                    "max_missed_updates": self.region_tracker.max_missed_updates,
                },
                "guard": {
                    "preserve_previous_on_invalid_update": self.region_guard.preserve_previous_on_invalid_update,
                    "publish_stale_regions_on_reject": self.region_guard.publish_stale_regions_on_reject,
                    "min_valid_region_count": self.region_guard.min_valid_region_count,
                    "max_unknown_ratio_for_update": self.region_guard.max_unknown_ratio_for_update,
                    "max_region_area_drop_ratio": self.region_guard.max_region_area_drop_ratio,
                    "max_region_count_drop_ratio": self.region_guard.max_region_count_drop_ratio,
                },
            },
        }

    @property
    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return hashlib.sha256(payload).hexdigest()


def load_config(path: str | Path) -> Phase1Config:
    """Load and strictly validate a Phase 1 YAML config."""

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return Phase1Config.from_mapping(_require_mapping(raw, "root"))
