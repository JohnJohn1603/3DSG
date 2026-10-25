"""Frontier extraction from the observed known-free component only."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from collections import deque

import numpy as np

try:
    from scipy.ndimage import binary_propagation as _binary_propagation
except ImportError:  # NumPy-only installations retain the reference traversal.
    _binary_propagation = None

from .contracts import GridSnapshot, GridState


@dataclass(frozen=True)
class Frontier:
    frontier_id: str
    map_revision: int
    boundary_cells: tuple[tuple[int, int], ...]
    viewpoint_cell: tuple[int, int]
    predicted_gain_cells: int
    viewpoint_world_xy: tuple[float, float] | None = None


@dataclass(frozen=True)
class FrontierSet:
    map_revision: int
    as_of_frame_id: str
    frontiers: tuple[Frontier, ...]
    reason: str = ""
    known_free_cells: int = 0
    connected_known_free_cells: int = 0
    reachable_free_cells: int = 0
    raw_frontier_cells: int = 0
    raw_component_count: int = 0
    length_qualified_components: int = 0
    components_with_viewpoint: int = 0


class FrontierExtractor:
    """Find free cells bordering unknown and reachable from the robot pose."""

    def __init__(self, *, min_frontier_length_m: float = 0.20,
                 agent_radius_m: float, inflation_margin_m: float = 0.05,
                 connectivity: int = 8, max_frontiers: int = 64,
                 min_viewpoint_offset_m: float = 0.30,
                 max_viewpoint_offset_m: float = 0.60,
                 max_viewpoints_per_frontier: int = 1,
                 min_viewpoint_separation_m: float = 0.35) -> None:
        if (min_frontier_length_m < 0 or agent_radius_m <= 0 or inflation_margin_m < 0
                or connectivity not in (4, 8) or max_frontiers < 1
                or min_viewpoint_offset_m <= 0 or max_viewpoint_offset_m < min_viewpoint_offset_m
                or not isinstance(max_viewpoints_per_frontier, int)
                or max_viewpoints_per_frontier < 1
                or not math.isfinite(min_viewpoint_separation_m)
                or min_viewpoint_separation_m <= 0):
            raise ValueError("invalid frontier extractor configuration")
        self.min_frontier_length_m = float(min_frontier_length_m)
        self.agent_radius_m = float(agent_radius_m)
        self.inflation_margin_m = float(inflation_margin_m)
        self.connectivity = connectivity
        self.max_frontiers = max_frontiers
        self.min_viewpoint_offset_m = float(min_viewpoint_offset_m)
        self.max_viewpoint_offset_m = float(max_viewpoint_offset_m)
        self.max_viewpoints_per_frontier = max_viewpoints_per_frontier
        self.min_viewpoint_separation_m = float(min_viewpoint_separation_m)

    def extract(self, snapshot: GridSnapshot, robot_xy: tuple[float, float], *,
                allow_validated_start_anchor: bool = False,
                excluded_viewpoints: tuple[tuple[float, float], ...] = (),
                excluded_viewpoint_radius_m: float = 0.0) -> FrontierSet:
        """Sample diverse views after excluding already observed world positions.

        Exclusions apply to the full reachable pool on every extraction, so a
        long boundary can supply fresh views after its first samples are visited.
        """
        if (not math.isfinite(excluded_viewpoint_radius_m)
                or excluded_viewpoint_radius_m < 0
                or any(len(point) != 2 or not all(math.isfinite(v) for v in point)
                       for point in excluded_viewpoints)):
            raise ValueError("invalid excluded frontier viewpoints")
        start = snapshot.world_to_cell(*robot_xy)
        if start is None:
            return FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (), "robot_outside_map")
        known_free = snapshot.states == GridState.FREE
        traversable = self._traversable(snapshot)
        if not known_free[start] or (not traversable[start] and not allow_validated_start_anchor):
            return FrontierSet(snapshot.revision, snapshot.as_of_frame_id, (),
                               "robot_not_on_observed_free_cell", int(known_free.sum()))
        if allow_validated_start_anchor and known_free[start] and not traversable[start]:
            # The host has validated the robot's actual current pose (for
            # Habitat: the loaded, radius-eroded NavMesh). Treat this one cell
            # as a search seed only; every cell reached after it still needs
            # observed free space around the full footprint.
            traversable[start] = True
        connected_known_free = _flood(known_free, start, _neighbors(self.connectivity))
        reachable = _flood(traversable, start, _neighbors(self.connectivity),
                           prevent_corner_cut=True)
        unknown = snapshot.states == GridState.UNKNOWN
        frontier_mask = np.zeros_like(known_free, dtype=bool)
        h, w = known_free.shape
        for dr, dc in _neighbors(self.connectivity):
            src_r0, src_r1 = max(0, -dr), min(h, h - dr)
            src_c0, src_c1 = max(0, -dc), min(w, w - dc)
            dst_r0, dst_r1 = src_r0 + dr, src_r1 + dr
            dst_c0, dst_c1 = src_c0 + dc, src_c1 + dc
            frontier_mask[src_r0:src_r1, src_c0:src_c1] |= unknown[dst_r0:dst_r1, dst_c0:dst_c1]
        frontier_mask &= known_free
        components = _components(frontier_mask, _neighbors(self.connectivity))
        component_candidates: list[list[Frontier]] = []
        length_qualified = 0
        components_with_viewpoint = 0
        view_offsets = _view_offsets(
            snapshot.resolution_m, self.min_viewpoint_offset_m,
            self.max_viewpoint_offset_m,
        )
        for cells in components:
            if len(cells) * snapshot.resolution_m < self.min_frontier_length_m:
                continue
            length_qualified += 1
            centroid = np.mean(np.asarray(cells, dtype=np.float64), axis=0)
            # Stand in known free space behind the boundary. Candidate poses
            # directly on frontier cells can put the robot footprint over
            # unknown, so retain the whole component but plan to an offset view.
            viewpoint_pool = _ranked_reachable_viewpoints(
                cells, reachable, view_offsets,
            )
            if not viewpoint_pool:
                continue
            canonical_viewpoint = viewpoint_pool[0]
            viewpoints: list[tuple[int, int]] = []
            for viewpoint in viewpoint_pool:
                point_xy = snapshot.cell_to_world(*viewpoint)
                if (excluded_viewpoint_radius_m > 0
                        and any(math.dist(point_xy, point) < excluded_viewpoint_radius_m
                                for point in excluded_viewpoints)):
                    continue
                if any(math.dist(point_xy, snapshot.cell_to_world(*previous)) + 1e-9
                       < self.min_viewpoint_separation_m for previous in viewpoints):
                    continue
                viewpoints.append(viewpoint)
                if len(viewpoints) >= self.max_viewpoints_per_frontier:
                    break
            if not viewpoints:
                continue
            components_with_viewpoint += 1
            world_x = snapshot.origin_x_m + (centroid[1] + 0.5) * snapshot.resolution_m
            world_y = snapshot.origin_y_m + (centroid[0] + 0.5) * snapshot.resolution_m
            signature = (round(world_x / 0.25), round(world_y / 0.25),
                         round(len(cells) * snapshot.resolution_m / 0.20))
            stable_id = hashlib.sha1(f"{signature[0]}:{signature[1]}:{signature[2]}".encode()).hexdigest()[:12]
            boundary_cells = tuple(sorted(cells))
            views = []
            for viewpoint in viewpoints:
                point_xy = snapshot.cell_to_world(*viewpoint)
                frontier_id = f"frontier-{stable_id}"
                if viewpoint != canonical_viewpoint:
                    # World bins survive integral rolling-origin shifts. Never
                    # give an alternate the canonical ID: visited-ID memory for
                    # that primary must not discard the whole component.
                    qx, qy = (math.floor(value / snapshot.resolution_m + 1e-6)
                              for value in point_xy)
                    frontier_id += f"-view-{qx}-{qy}"
                views.append(Frontier(
                    frontier_id=frontier_id,
                    map_revision=snapshot.revision,
                    boundary_cells=boundary_cells,
                    viewpoint_cell=viewpoint,
                    predicted_gain_cells=len(cells),
                    viewpoint_world_xy=point_xy,
                ))
            component_candidates.append(views)
        component_candidates.sort(
            key=lambda views: (-views[0].predicted_gain_cells, views[0].frontier_id))
        # Preserve one view of each component before spending the global budget
        # on additional views, then distribute extras in the same fair order.
        candidates = [views[level]
                      for level in range(self.max_viewpoints_per_frontier)
                      for views in component_candidates if level < len(views)][:self.max_frontiers]
        reason = "frontiers_found" if candidates else (
            "no_frontier_boundary" if not components else
            "frontier_components_too_short" if not length_qualified else
            "no_reachable_frontier_viewpoint" if not components_with_viewpoint else
            "no_reachable_frontier")
        return FrontierSet(
            snapshot.revision, snapshot.as_of_frame_id, tuple(candidates), reason,
            int(known_free.sum()), int(connected_known_free.sum()), int(reachable.sum()),
            int(frontier_mask.sum()),
            len(components), length_qualified, components_with_viewpoint,
        )

    def _traversable(self, snapshot: GridSnapshot) -> np.ndarray:
        free = snapshot.states == GridState.FREE
        radius_m = self.agent_radius_m + self.inflation_margin_m
        radius = int(math.ceil(radius_m / snapshot.resolution_m))
        blocked_source = snapshot.states != GridState.FREE
        blocked = np.zeros_like(free)
        h, w = free.shape
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                if (dr * dr + dc * dc) * snapshot.resolution_m ** 2 > radius_m ** 2:
                    continue
                src_r0, src_r1 = max(0, -dr), min(h, h - dr)
                src_c0, src_c1 = max(0, -dc), min(w, w - dc)
                dst_r0, dst_r1 = src_r0 + dr, src_r1 + dr
                dst_c0, dst_c1 = src_c0 + dc, src_c1 + dc
                blocked[src_r0:src_r1, src_c0:src_c1] |= blocked_source[dst_r0:dst_r1, dst_c0:dst_c1]
        traversable = free & ~blocked
        if snapshot.spawn_center_xy is not None:
            spawn_cell = snapshot.world_to_cell(*snapshot.spawn_center_xy)
            if spawn_cell is not None and snapshot.states[spawn_cell] == GridState.FREE:
                traversable[spawn_cell] = True
        return traversable


def _view_offsets(resolution_m: float, min_offset_m: float,
                  max_offset_m: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return annulus offsets in the exact row-major order used by the old loops."""
    radius_cells = int(math.ceil(max_offset_m / resolution_m))
    offsets = [(dr, dc, math.hypot(dr, dc) * resolution_m)
               for dr in range(-radius_cells, radius_cells + 1)
               for dc in range(-radius_cells, radius_cells + 1)]
    offsets = [item for item in offsets if min_offset_m <= item[2] <= max_offset_m]
    return (
        np.asarray([item[0] for item in offsets], dtype=np.int32),
        np.asarray([item[1] for item in offsets], dtype=np.int32),
        np.asarray([item[2] for item in offsets], dtype=np.float64),
    )


def _best_reachable_viewpoint(
    cells: list[tuple[int, int]], reachable: np.ndarray,
    offsets: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> tuple[int, int] | None:
    viewpoints = _ranked_reachable_viewpoints(cells, reachable, offsets)
    return viewpoints[0] if viewpoints else None


def _ranked_reachable_viewpoints(
    cells: list[tuple[int, int]], reachable: np.ndarray,
    offsets: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> list[tuple[int, int]]:
    """Vectorized equivalent of the boundary-cell / annulus nested loops.

    Distance and insertion-rank reductions reproduce the old dictionary
    semantics: minimum distance per candidate, followed by stable first-seen
    behavior when both viewpoint score terms tie.
    """
    h, w = reachable.shape
    offset_rows, offset_cols, distances = offsets
    n_offsets = len(distances)
    if not cells or not n_offsets:
        return []

    min_distance = np.full(h * w, np.inf, dtype=np.float64)
    first_seen = np.full(h * w, np.iinfo(np.int64).max, dtype=np.int64)
    offset_order = np.arange(n_offsets, dtype=np.int64)
    batch_size = max(1, min(len(cells), 256))
    for start in range(0, len(cells), batch_size):
        batch = np.asarray(cells[start:start + batch_size], dtype=np.int32)
        rows = batch[:, 0, None] + offset_rows[None, :]
        cols = batch[:, 1, None] + offset_cols[None, :]
        valid = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
        valid &= reachable[np.clip(rows, 0, h - 1), np.clip(cols, 0, w - 1)]
        if not valid.any():
            continue
        source_order = np.arange(start, start + len(batch), dtype=np.int64)[:, None]
        flat_cells = (rows * w + cols)[valid]
        pair_distances = np.broadcast_to(distances, rows.shape)[valid]
        pair_order = (source_order * n_offsets + offset_order[None, :])[valid]
        np.minimum.at(min_distance, flat_cells, pair_distances)
        np.minimum.at(first_seen, flat_cells, pair_order)

    candidates = np.flatnonzero(np.isfinite(min_distance))
    if not len(candidates):
        return []
    candidate_rows, candidate_cols = np.divmod(candidates, w)
    center = np.asarray(cells, dtype=np.float64).mean(axis=0)
    target_distance = np.abs(min_distance[candidates] - 0.45)
    centroid_distance = ((candidate_rows - center[0]) ** 2
                         + (candidate_cols - center[1]) ** 2)
    order = np.lexsort((first_seen[candidates], centroid_distance, target_distance))
    return [divmod(int(cell), w) for cell in candidates[order]]


def _neighbors(connectivity: int) -> tuple[tuple[int, int], ...]:
    directions = [(-1, 0), (0, -1), (0, 1), (1, 0)]
    if connectivity == 8:
        directions.extend(((-1, -1), (-1, 1), (1, -1), (1, 1)))
    return tuple(directions)


def _flood(mask: np.ndarray, start: tuple[int, int], directions: tuple[tuple[int, int], ...],
           *, prevent_corner_cut: bool = False) -> np.ndarray:
    neighborhood = set(directions)
    if (_binary_propagation is not None and mask[start]
            and neighborhood in (set(_neighbors(4)), set(_neighbors(8)))):
        # With corner cutting forbidden, every legal diagonal has a free
        # cardinal two-step alternative. Thus 4-connectivity gives exactly
        # the same reachable set, while SciPy traverses it outside the GIL.
        diagonal = len(neighborhood) == 8 and not prevent_corner_cut
        structure = (np.ones((3, 3), dtype=bool) if diagonal else
                     np.array([[False, True, False], [True, True, True],
                               [False, True, False]], dtype=bool))
        seed = np.zeros_like(mask, dtype=bool)
        seed[start] = True
        return _binary_propagation(seed, structure=structure, mask=mask)
    h, w = mask.shape
    out = np.zeros_like(mask, dtype=bool)
    q = deque([start])
    out[start] = True
    while q:
        r, c = q.popleft()
        for dr, dc in directions:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w and mask[nr, nc] and not out[nr, nc]:
                if (prevent_corner_cut and dr and dc
                        and (not mask[r + dr, c] or not mask[r, c + dc])):
                    continue
                out[nr, nc] = True
                q.append((nr, nc))
    return out


def _components(mask: np.ndarray, directions: tuple[tuple[int, int], ...]) -> list[list[tuple[int, int]]]:
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    groups = []
    for r, c in zip(*np.nonzero(mask)):
        if seen[r, c]:
            continue
        seen[r, c] = True
        q = deque([(int(r), int(c))])
        group = []
        while q:
            cr, cc = q.popleft()
            group.append((cr, cc))
            for dr, dc in directions:
                nr, nc = cr + dr, cc + dc
                if 0 <= nr < h and 0 <= nc < w and mask[nr, nc] and not seen[nr, nc]:
                    seen[nr, nc] = True
                    q.append((nr, nc))
        groups.append(group)
    return groups
