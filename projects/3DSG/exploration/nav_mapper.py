"""Depth-only incremental 2D navigation map with conservative height evidence."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

import numpy as np

from .contracts import FramePacket, GridSnapshot, GridState


@dataclass(frozen=True)
class NavMapperConfig:
    resolution_m: float = 0.05
    depth_stride: int = 4
    max_cells: int = 2_000_000
    agent_height_m: float | None = None
    obstacle_min_height_m: float = 0.05
    floor_tolerance_m: float = 0.08
    min_floor_support: int = 1
    min_clearance_rays: int = 1
    free_log_odds_delta: float = -0.35
    occupied_log_odds_delta: float = 0.85
    log_odds_limit: float = 5.0
    occupied_threshold: float = 0.7
    free_threshold: float = -0.35
    max_ray_cells: int = 600
    ray_chunk_size: int = 256

    def __post_init__(self) -> None:
        if self.resolution_m <= 0 or self.depth_stride < 1 or self.max_cells < 1:
            raise ValueError("resolution, depth_stride, and max_cells must be positive")
        if self.agent_height_m is not None and self.agent_height_m <= self.obstacle_min_height_m:
            raise ValueError("agent height must exceed obstacle_min_height_m")
        if self.floor_tolerance_m <= 0 or self.min_floor_support < 1 or self.min_clearance_rays < 1:
            raise ValueError("floor and evidence thresholds must be positive")
        if self.log_odds_limit <= 0 or self.max_ray_cells < 1 or self.ray_chunk_size < 1:
            raise ValueError("log odds limit, max_ray_cells, and ray_chunk_size must be positive")


class NavMapper:
    """Integrate axial depth into an observed map; unknown is never free.

    The input camera pose must already be ``T_world_camera`` in canonical
    world Z-up, with optical camera axes +X right, +Y down, +Z forward. Cells
    become traversable only after both floor support and clearance-band ray
    evidence exist. Geometry parameters such as agent height must be resolved
    from the simulator geometry contract before construction.
    """

    def __init__(self, config: NavMapperConfig, *, agent_radius_m: float | None = None) -> None:
        self.config = config
        if config.agent_height_m is None or agent_radius_m is None:
            raise ValueError("agent height and radius must be resolved from geometry contract")
        if not math.isfinite(agent_radius_m) or agent_radius_m <= 0:
            raise ValueError("agent_radius_m must be finite and positive")
        self.agent_radius_m = float(agent_radius_m)
        self._origin_x = 0.0
        self._origin_y = 0.0
        self._states = np.zeros((1, 1), dtype=np.uint8)
        self._floor = np.zeros((1, 1), dtype=np.uint16)
        self._clearance = np.zeros((1, 1), dtype=np.uint16)
        self._spawn_validated = np.zeros((1, 1), dtype=bool)
        self._odds = np.zeros((1, 1), dtype=np.float32)
        self._revision = 0
        self._last_frame_index = -1
        self._last_frame_id = ""
        self._last_host_time = 0.0
        self._spawn_initialized = False
        self._spawn_center_xy: tuple[float, float] | None = None
        self.last_update_profile_s: dict[str, float] = {}

    def initialize_spawn(self, x_m: float, y_m: float) -> None:
        """Seed only the footprint certified by the loaded navmesh at spawn."""
        if self._spawn_initialized or self._last_frame_index >= 0:
            raise RuntimeError("spawn footprint may be seeded once, before depth integration")
        if not math.isfinite(x_m) or not math.isfinite(y_m):
            raise ValueError("spawn coordinates must be finite")
        self._ensure_bounds(np.array(((float(x_m), float(y_m)),)))
        center = np.floor((np.array((x_m, y_m)) - np.array((self._origin_x, self._origin_y)))
                          / self.config.resolution_m).astype(int)
        radius_cells = int(math.ceil(self.agent_radius_m / self.config.resolution_m))
        for dr in range(-radius_cells, radius_cells + 1):
            for dc in range(-radius_cells, radius_cells + 1):
                if (dr * dr + dc * dc) * self.config.resolution_m ** 2 > self.agent_radius_m ** 2:
                    continue
                row, col = int(center[1] + dr), int(center[0] + dc)
                self._spawn_validated[row, col] = True
                self._floor[row, col] = max(1, int(self._floor[row, col]))
                self._clearance[row, col] = max(1, int(self._clearance[row, col]))
                self._odds[row, col] = min(float(self._odds[row, col]), self.config.free_threshold)
                self._states[row, col] = GridState.FREE
        self._spawn_initialized = True
        self._spawn_center_xy = (float(x_m), float(y_m))

    def update(self, packet: FramePacket) -> GridSnapshot:
        total_wall_started = time.perf_counter()
        total_cpu_started = time.thread_time()
        profile = {
            "prepare_wall_s": 0.0,
            "prepare_cpu_s": 0.0,
            "ray_integration_wall_s": 0.0,
            "ray_integration_cpu_s": 0.0,
            "classification_wall_s": 0.0,
            "classification_cpu_s": 0.0,
            "snapshot_wall_s": 0.0,
            "snapshot_cpu_s": 0.0,
        }
        if packet.frame_index <= self._last_frame_index:
            raise ValueError("frame packets must be integrated in strictly increasing order")
        if packet.intrinsics.width != packet.depth_m.shape[1] or packet.intrinsics.height != packet.depth_m.shape[0]:
            raise ValueError("depth profile and intrinsics disagree")
        sensor_frames = [(packet.depth_m, packet.intrinsics, packet.T_world_camera,
                          packet.depth_min_m, packet.depth_max_m)]
        if packet.nav_depth_m is not None:
            assert packet.nav_depth_intrinsics is not None
            assert packet.T_world_nav_depth_camera is not None
            assert packet.nav_depth_min_m is not None and packet.nav_depth_max_m is not None
            sensor_frames.append((packet.nav_depth_m, packet.nav_depth_intrinsics,
                                  packet.T_world_nav_depth_camera,
                                  packet.nav_depth_min_m, packet.nav_depth_max_m))

        stride = self.config.depth_stride
        floor_low = packet.floor_z_m + self.config.obstacle_min_height_m
        obstacle_high = packet.floor_z_m + float(self.config.agent_height_m)
        for sensor_depth, intrinsics, sensor_pose, depth_min, depth_max in sensor_frames:
            prepare_wall_started = time.perf_counter()
            prepare_cpu_started = time.thread_time()
            if sensor_depth.shape != (intrinsics.height, intrinsics.width):
                raise ValueError("sensor depth profile and intrinsics disagree")
            rows = np.arange(0, sensor_depth.shape[0], stride, dtype=np.int32)
            cols = np.arange(0, sensor_depth.shape[1], stride, dtype=np.int32)
            vv, uu = np.meshgrid(rows, cols, indexing="ij")
            depth = sensor_depth[vv, uu].astype(np.float64, copy=False)
            valid = np.isfinite(depth) & (depth >= depth_min) & (depth <= depth_max)
            if not np.any(valid):
                profile["prepare_wall_s"] += time.perf_counter() - prepare_wall_started
                profile["prepare_cpu_s"] += time.thread_time() - prepare_cpu_started
                continue

            u = uu[valid].astype(np.float64)
            v = vv[valid].astype(np.float64)
            z = depth[valid]
            camera = np.stack(((u - intrinsics.cx) * z / intrinsics.fx,
                               (v - intrinsics.cy) * z / intrinsics.fy,
                               z, np.ones_like(z)), axis=0)
            world = sensor_pose.matrix @ camera
            xyz = world[:3].T
            origin = sensor_pose.position
            self._ensure_bounds(np.vstack((xyz[:, :2], origin[None, :2])))

            res = self.config.resolution_m
            ends = np.floor((xyz[:, :2] - np.array((self._origin_x, self._origin_y))) / res).astype(np.int64)
            start = np.floor((origin[:2] - np.array((self._origin_x, self._origin_y))) / res).astype(np.int64)
            floor_seen = np.abs(xyz[:, 2] - packet.floor_z_m) <= self.config.floor_tolerance_m
            obstacle_hit = (xyz[:, 2] >= floor_low) & (xyz[:, 2] <= obstacle_high)
            profile["prepare_wall_s"] += time.perf_counter() - prepare_wall_started
            profile["prepare_cpu_s"] += time.thread_time() - prepare_cpu_started
            ray_wall_started = time.perf_counter()
            ray_cpu_started = time.thread_time()
            self._integrate_rays(start, ends, origin, xyz, packet.floor_z_m,
                                 floor_seen, obstacle_hit)
            profile["ray_integration_wall_s"] += time.perf_counter() - ray_wall_started
            profile["ray_integration_cpu_s"] += time.thread_time() - ray_cpu_started

        classification_wall_started = time.perf_counter()
        classification_cpu_started = time.thread_time()
        self._classify()
        profile["classification_wall_s"] = time.perf_counter() - classification_wall_started
        profile["classification_cpu_s"] = time.thread_time() - classification_cpu_started
        self._revision += 1
        self._last_frame_id = packet.frame_id
        self._last_frame_index = packet.frame_index
        self._last_host_time = packet.host_monotonic_s
        snapshot_wall_started = time.perf_counter()
        snapshot_cpu_started = time.thread_time()
        result = self.snapshot()
        profile["snapshot_wall_s"] = time.perf_counter() - snapshot_wall_started
        profile["snapshot_cpu_s"] = time.thread_time() - snapshot_cpu_started
        profile["total_wall_s"] = time.perf_counter() - total_wall_started
        profile["total_cpu_s"] = time.thread_time() - total_cpu_started
        self.last_update_profile_s = profile
        return result

    def snapshot(self) -> GridSnapshot:
        return GridSnapshot(
            revision=self._revision,
            as_of_frame_id=self._last_frame_id or "uninitialized",
            as_of_frame_index=max(0, self._last_frame_index),
            host_monotonic_s=self._last_host_time,
            resolution_m=self.config.resolution_m,
            origin_x_m=self._origin_x,
            origin_y_m=self._origin_y,
            states=self._states,
            floor_support=self._floor,
            clearance_evidence=self._clearance,
            spawn_validated=self._spawn_validated,
            occupancy_log_odds=self._odds,
            spawn_center_xy=self._spawn_center_xy,
        )

    def _ensure_bounds(self, points_xy: np.ndarray) -> None:
        res = self.config.resolution_m
        min_x = math.floor(float(np.min(points_xy[:, 0])) / res) * res
        min_y = math.floor(float(np.min(points_xy[:, 1])) / res) * res
        max_x = math.floor(float(np.max(points_xy[:, 0])) / res) * res
        max_y = math.floor(float(np.max(points_xy[:, 1])) / res) * res
        pad = max(1, int(math.ceil(self.agent_radius_m / res)))
        min_x -= pad * res
        min_y -= pad * res
        max_x += pad * res
        max_y += pad * res
        new_h = int(round((max_y - min_y) / res)) + 1
        new_w = int(round((max_x - min_x) / res)) + 1
        if new_h * new_w > self.config.max_cells:
            raise MemoryError(f"NavMap expansion needs {new_h * new_w} cells (cap {self.config.max_cells})")
        old_h, old_w = self._states.shape
        old_max_x = self._origin_x + (old_w - 1) * res
        old_max_y = self._origin_y + (old_h - 1) * res
        if (min_x >= self._origin_x and min_y >= self._origin_y
                and max_x <= old_max_x and max_y <= old_max_y):
            return
        origin_x = min(self._origin_x, min_x)
        origin_y = min(self._origin_y, min_y)
        new_h = max(int(round((max_y - origin_y) / res)) + 1,
                    int(round((old_max_y - origin_y) / res)) + 1)
        new_w = max(int(round((max_x - origin_x) / res)) + 1,
                    int(round((old_max_x - origin_x) / res)) + 1)
        if new_h * new_w > self.config.max_cells:
            raise MemoryError(f"NavMap expansion needs {new_h * new_w} cells (cap {self.config.max_cells})")
        row_off = int(round((self._origin_y - origin_y) / res))
        col_off = int(round((self._origin_x - origin_x) / res))
        self._states = _expand(self._states, new_h, new_w, row_off, col_off, 0)
        self._floor = _expand(self._floor, new_h, new_w, row_off, col_off, 0)
        self._clearance = _expand(self._clearance, new_h, new_w, row_off, col_off, 0)
        self._spawn_validated = _expand(self._spawn_validated, new_h, new_w, row_off, col_off, False)
        self._odds = _expand(self._odds, new_h, new_w, row_off, col_off, 0.0)
        self._origin_x, self._origin_y = origin_x, origin_y

    def _ensure_cell(self, row: int, col: int) -> None:
        h, w = self._states.shape
        if 0 <= row < h and 0 <= col < w:
            return
        res = self.config.resolution_m
        x = self._origin_x + col * res
        y = self._origin_y + row * res
        self._ensure_bounds(np.array(((x, y),)))

    def _integrate_rays(self, start: np.ndarray, ends: np.ndarray, origin: np.ndarray,
                        xyz: np.ndarray, floor_z_m: float, floor_seen: np.ndarray,
                        obstacle_hit: np.ndarray) -> None:
        """Batch depth rays in bounded chunks instead of Python per-cell loops."""
        max_value = np.iinfo(np.uint16).max
        flat_odds = self._odds.reshape(-1)
        flat_floor = self._floor.reshape(-1)
        flat_clearance = self._clearance.reshape(-1)
        width = self._states.shape[1]
        cap = self.config.max_ray_cells - 1
        start_col, start_row = int(start[0]), int(start[1])
        floor_low = floor_z_m + self.config.obstacle_min_height_m
        obstacle_high = floor_z_m + float(self.config.agent_height_m)

        chunk_size = self.config.ray_chunk_size
        for chunk_start in range(0, len(ends), chunk_size):
            chunk_end = min(len(ends), chunk_start + chunk_size)
            chunk_ends = ends[chunk_start:chunk_end]
            delta_col = chunk_ends[:, 0] - start_col
            delta_row = chunk_ends[:, 1] - start_row
            steps = np.maximum(np.abs(delta_col), np.abs(delta_row))
            active = steps <= cap
            if not np.any(active):
                continue
            chunk_ends = chunk_ends[active]
            delta_col = delta_col[active]
            delta_row = delta_row[active]
            steps = steps[active]
            ray_floor_seen = floor_seen[chunk_start:chunk_end][active]
            ray_obstacle_hit = obstacle_hit[chunk_start:chunk_end][active]
            ray_z = xyz[chunk_start:chunk_end, 2][active]
            positions = np.arange(int(np.max(steps)) + 1, dtype=np.int64)[None, :]
            denominator = np.maximum(steps, 1)[:, None]
            valid_cells = positions <= steps[:, None]
            rows = start_row + np.rint(delta_row[:, None] * positions / denominator).astype(np.int64)
            cols = start_col + np.rint(delta_col[:, None] * positions / denominator).astype(np.int64)
            t = positions / denominator
            ray_height = origin[2] + t * (ray_z[:, None] - origin[2])
            endpoint = positions == steps[:, None]
            free_mask = valid_cells & (~endpoint | ~ray_obstacle_hit[:, None])
            # A floor return proves floor support at the measured endpoint.
            # The projected ray may pass above low obstacles or floor edges,
            # so it cannot certify the floor along its full XY projection.
            floor_endpoint_mask = endpoint & ray_floor_seen[:, None]
            height_clear_mask = ((ray_height >= floor_low) & (ray_height <= obstacle_high))
            free_mask &= height_clear_mask | floor_endpoint_mask
            free_indices = (rows[free_mask] * width + cols[free_mask]).astype(np.int64)
            floor_indices = (rows[floor_endpoint_mask] * width
                             + cols[floor_endpoint_mask]).astype(np.int64)
            if free_indices.size or floor_indices.size:
                # Encode free and floor evidence as even/odd keys so one sort
                # groups both event types. Floor endpoints are included in the
                # free ray mask, preserving the prior clearance increments.
                event_keys = np.concatenate((free_indices * 2, floor_indices * 2 + 1))
                unique_events, counts = np.unique(event_keys, return_counts=True)
                cells = unique_events >> 1
                floor_events = (unique_events & 1) == 1
                free_cells, free_counts = cells[~floor_events], counts[~floor_events]
                floor_cells, floor_counts = cells[floor_events], counts[floor_events]
                # Aggregate duplicate ray visits first. Updating each cell once
                # keeps the same evidence count while avoiding per-ray writes.
                flat_odds[free_cells] += (np.float32(self.config.free_log_odds_delta)
                                          * free_counts.astype(np.float32))
                flat_clearance[free_cells] = np.minimum(
                    max_value, flat_clearance[free_cells].astype(np.uint32)
                    + free_counts.astype(np.uint32)
                ).astype(np.uint16)
                if floor_cells.size:
                    flat_floor[floor_cells] = np.minimum(
                        max_value, flat_floor[floor_cells].astype(np.uint32)
                        + floor_counts.astype(np.uint32)
                    ).astype(np.uint16)
                    flat_clearance[floor_cells] = np.minimum(
                        max_value, flat_clearance[floor_cells].astype(np.uint32)
                        + floor_counts.astype(np.uint32)
                    ).astype(np.uint16)

            obstacle_rays = ray_obstacle_hit
            if np.any(obstacle_rays):
                obstacle_indices = (chunk_ends[obstacle_rays, 1] * width
                                    + chunk_ends[obstacle_rays, 0]).astype(np.int64)
                unique, counts = np.unique(obstacle_indices, return_counts=True)
                flat_odds[unique] += (np.float32(self.config.occupied_log_odds_delta)
                                      * counts.astype(np.float32))

        np.clip(self._odds, -self.config.log_odds_limit, self.config.log_odds_limit,
                out=self._odds)

    def _classify(self) -> None:
        occupied = self._odds >= self.config.occupied_threshold
        supported = self._floor >= self.config.min_floor_support
        clear = self._clearance >= self.config.min_clearance_rays
        free = (self._odds <= self.config.free_threshold) & supported & clear & ~occupied
        self._states.fill(GridState.UNKNOWN)
        self._states[free] = GridState.FREE
        self._states[occupied] = GridState.OCCUPIED


def _expand(old: np.ndarray, height: int, width: int, row: int, col: int, fill: float | int) -> np.ndarray:
    new = np.full((height, width), fill, dtype=old.dtype)
    new[row:row + old.shape[0], col:col + old.shape[1]] = old
    return new


def _bresenham(r0: int, c0: int, r1: int, c1: int, cap: int) -> list[tuple[int, int]]:
    """Integer grid ray; returns [] when a malformed range exceeds the cap."""
    dc, dr = abs(c1 - c0), abs(r1 - r0)
    if max(dc, dr) + 1 > cap:
        return []
    sc = 1 if c0 < c1 else -1
    sr = 1 if r0 < r1 else -1
    err = dc - dr
    out = []
    while True:
        out.append((r0, c0))
        if r0 == r1 and c0 == c1:
            return out
        e2 = 2 * err
        if e2 > -dr:
            err -= dr
            c0 += sc
        if e2 < dc:
            err += dc
            r0 += sr
