"""Exact grid paths over known-free cells, using A* or a shared Dijkstra search."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from itertools import count
from typing import Iterable

import numpy as np

from .contracts import GridSnapshot, GridState, PathResult


@dataclass(frozen=True)
class PlannerConfig:
    agent_radius_m: float
    inflation_margin_m: float = 0.05
    allow_diagonal: bool = True
    max_expansions: int = 500_000

    def __post_init__(self) -> None:
        if self.agent_radius_m <= 0 or self.inflation_margin_m < 0 or self.max_expansions < 1:
            raise ValueError("invalid planner configuration")


class AStarPlanner:
    def __init__(self, config: PlannerConfig) -> None:
        self.config = config
        self._traversable_cache_key: tuple[object, ...] | None = None
        self._traversable_cache: np.ndarray | None = None

    def plan(self, snapshot: GridSnapshot, start_xy: tuple[float, float],
             goal_cell: tuple[int, int], *,
             allow_validated_start_anchor: bool = False,
             blocked_edges: frozenset[tuple[tuple[int, int], tuple[int, int]]] = frozenset()) -> PathResult:
        start = snapshot.world_to_cell(*start_xy)
        if start is None:
            return PathResult(False, (), math.inf, snapshot.revision, "start_outside_map")
        if not _inside(snapshot.states.shape, goal_cell):
            return PathResult(False, (), math.inf, snapshot.revision, "goal_outside_map")
        traversable = self._traversable(snapshot)
        if not traversable[start]:
            if (allow_validated_start_anchor
                    and snapshot.states[start] == GridState.FREE):
                # This exception applies only to the externally validated
                # current pose. Successors and diagonal side cells remain
                # subject to the normal observed-footprint rule.
                traversable = traversable.copy()
                traversable[start] = True
            else:
                return PathResult(False, (), math.inf, snapshot.revision, "start_not_traversable")
        if not traversable[goal_cell]:
            return PathResult(False, (), math.inf, snapshot.revision, "goal_not_traversable")

        directions = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0))
        if self.config.allow_diagonal:
            root2 = math.sqrt(2.0)
            directions += ((-1, -1, root2), (-1, 1, root2), (1, -1, root2), (1, 1, root2))
        serial = count()
        open_heap: list[tuple[float, int, tuple[int, int]]] = []
        heapq.heappush(open_heap, (_heuristic(start, goal_cell), next(serial), start))
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        g = {start: 0.0}
        closed: set[tuple[int, int]] = set()
        expansions = 0
        while open_heap and expansions < self.config.max_expansions:
            _, _, current = heapq.heappop(open_heap)
            if current in closed:
                continue
            if current == goal_cell:
                cells = [current]
                while cells[-1] in came_from:
                    cells.append(came_from[cells[-1]])
                cells.reverse()
                return PathResult(
                    True, tuple(cells), g[current] * snapshot.resolution_m, snapshot.revision,
                    grid_origin_x_m=snapshot.origin_x_m,
                    grid_origin_y_m=snapshot.origin_y_m,
                    grid_resolution_m=snapshot.resolution_m,
                )
            closed.add(current)
            expansions += 1
            r, c = current
            for dr, dc, step in directions:
                nxt = r + dr, c + dc
                if not _inside(snapshot.states.shape, nxt) or not traversable[nxt] or nxt in closed:
                    continue
                if _edge_key(current, nxt) in blocked_edges:
                    continue
                if dr and dc and (not traversable[r + dr, c] or not traversable[r, c + dc]):
                    continue
                tentative = g[current] + step
                if tentative < g.get(nxt, math.inf):
                    came_from[nxt] = current
                    g[nxt] = tentative
                    heapq.heappush(open_heap, (tentative + _heuristic(nxt, goal_cell), next(serial), nxt))
        reason = "expansion_budget_exceeded" if expansions >= self.config.max_expansions else "no_known_free_path"
        return PathResult(False, (), math.inf, snapshot.revision, reason)

    def plan_many(self, snapshot: GridSnapshot, start_xy: tuple[float, float],
                  goal_cells: Iterable[tuple[int, int]], *,
                  allow_validated_start_anchor: bool = False,
                  blocked_edges: frozenset[
                      tuple[tuple[int, int], tuple[int, int]]
                  ] = frozenset()) -> dict[tuple[int, int], PathResult]:
        """Find exact shortest paths to all goals with one shared Dijkstra search.

        The footprint, start anchor, corner and edge rules match ``plan``.
        The expansion budget applies to the shared wavefront; goals already
        settled retain their optimal paths if that budget is exhausted.
        """
        goals = tuple(dict.fromkeys(goal_cells))
        results: dict[tuple[int, int], PathResult] = {}
        if not goals:
            return results

        def failure(reason: str) -> PathResult:
            return PathResult(False, (), math.inf, snapshot.revision, reason)

        start = snapshot.world_to_cell(*start_xy)
        if start is None:
            return {goal: failure("start_outside_map") for goal in goals}
        pending = set()
        # Match single-goal validation precedence, including out-of-map goals
        # when the start itself is not traversable.
        for goal in goals:
            if not _inside(snapshot.states.shape, goal):
                results[goal] = failure("goal_outside_map")
            else:
                pending.add(goal)
        if not pending:
            return results
        traversable = self._traversable(snapshot)
        if not traversable[start]:
            if allow_validated_start_anchor and snapshot.states[start] == GridState.FREE:
                traversable = traversable.copy()
                traversable[start] = True
            else:
                results.update((goal, failure("start_not_traversable")) for goal in pending)
                return results
        for goal in tuple(pending):
            if not traversable[goal]:
                results[goal] = failure("goal_not_traversable")
                pending.remove(goal)
        if not pending:
            return results

        directions = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0))
        if self.config.allow_diagonal:
            root2 = math.sqrt(2.0)
            directions += ((-1, -1, root2), (-1, 1, root2),
                           (1, -1, root2), (1, 1, root2))
        serial = count()
        open_heap = [(0.0, next(serial), start)]
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        distances = {start: 0.0}
        closed: set[tuple[int, int]] = set()
        expansions = 0
        while open_heap and pending and expansions < self.config.max_expansions:
            distance, _, current = heapq.heappop(open_heap)
            if current in closed:
                continue
            if current in pending:
                cells = [current]
                while cells[-1] in came_from:
                    cells.append(came_from[cells[-1]])
                cells.reverse()
                results[current] = PathResult(
                    True, tuple(cells), distance * snapshot.resolution_m,
                    snapshot.revision, grid_origin_x_m=snapshot.origin_x_m,
                    grid_origin_y_m=snapshot.origin_y_m,
                    grid_resolution_m=snapshot.resolution_m,
                )
                pending.remove(current)
                if not pending:
                    break
            closed.add(current)
            expansions += 1
            r, c = current
            for dr, dc, step in directions:
                nxt = r + dr, c + dc
                if not _inside(snapshot.states.shape, nxt) or not traversable[nxt] or nxt in closed:
                    continue
                if blocked_edges and _edge_key(current, nxt) in blocked_edges:
                    continue
                if dr and dc and (not traversable[r + dr, c] or not traversable[r, c + dc]):
                    continue
                tentative = distance + step
                if tentative < distances.get(nxt, math.inf):
                    came_from[nxt] = current
                    distances[nxt] = tentative
                    heapq.heappush(open_heap, (tentative, next(serial), nxt))
        reason = ("expansion_budget_exceeded" if expansions >= self.config.max_expansions
                  else "no_known_free_path")
        results.update((goal, failure(reason)) for goal in pending)
        return results

    def revalidate(self, snapshot: GridSnapshot, path: PathResult,
                   current_xy: tuple[float, float] | None = None, *,
                   allow_validated_start_anchor: bool = False,
                   blocked_edges: frozenset[tuple[tuple[int, int], tuple[int, int]]] = frozenset()) -> PathResult:
        """Refresh a path on the current map without treating traveled cells as future route.

        When the current robot pose is provided, preserve the world-space goal
        but run A* from the current pose. Old path prefixes can become occupied
        after the robot has passed them; those cells must not invalidate the
        still-untraveled route. Without a pose, retain strict whole-path
        validation for callers that need that behavior.
        """
        if not path.success or not path.cells:
            return PathResult(False, (), math.inf, snapshot.revision, "no_path_to_revalidate")
        if (path.grid_origin_x_m is None or path.grid_origin_y_m is None
                or path.grid_resolution_m is None):
            return PathResult(False, (), math.inf, snapshot.revision, "path_grid_geometry_missing")
        if not math.isclose(path.grid_resolution_m, snapshot.resolution_m, rel_tol=0.0, abs_tol=1e-9):
            return PathResult(False, (), math.inf, snapshot.revision, "grid_resolution_changed")
        if current_xy is not None:
            goal_row, goal_col = path.cells[-1]
            goal_world_x = path.grid_origin_x_m + (goal_col + 0.5) * path.grid_resolution_m
            goal_world_y = path.grid_origin_y_m + (goal_row + 0.5) * path.grid_resolution_m
            goal_cell = snapshot.world_to_cell(goal_world_x, goal_world_y)
            if goal_cell is None:
                return PathResult(False, (), math.inf, snapshot.revision,
                                  "path_world_point_outside_map")
            return self.plan(
                snapshot, current_xy, goal_cell,
                allow_validated_start_anchor=allow_validated_start_anchor,
                blocked_edges=blocked_edges,
            )
        remapped: list[tuple[int, int]] = []
        for row, col in path.cells:
            world_x = path.grid_origin_x_m + (col + 0.5) * path.grid_resolution_m
            world_y = path.grid_origin_y_m + (row + 0.5) * path.grid_resolution_m
            cell = snapshot.world_to_cell(world_x, world_y)
            if cell is None:
                return PathResult(False, (), math.inf, snapshot.revision, "path_world_point_outside_map")
            remapped.append(cell)
        traversable = self._traversable(snapshot)
        previous = None
        cost_cells = 0.0
        for cell in remapped:
            if not _inside(snapshot.states.shape, cell) or not traversable[cell]:
                return PathResult(False, (), math.inf, snapshot.revision, "path_blocked_by_new_map")
            if previous is not None:
                dr, dc = abs(cell[0] - previous[0]), abs(cell[1] - previous[1])
                if max(dr, dc) > 1 or (dr == 0 and dc == 0):
                    return PathResult(False, (), math.inf, snapshot.revision, "path_discontinuous")
                if dr and dc and (not traversable[previous[0], cell[1]]
                                  or not traversable[cell[0], previous[1]]):
                    return PathResult(False, (), math.inf, snapshot.revision, "path_corner_cut_blocked")
                cost_cells += math.sqrt(2.0) if dr and dc else 1.0
            previous = cell
        return PathResult(True, tuple(remapped), cost_cells * snapshot.resolution_m,
                          snapshot.revision, "revalidated_on_new_map",
                          snapshot.origin_x_m, snapshot.origin_y_m, snapshot.resolution_m)

    def traversable(self, snapshot: GridSnapshot) -> np.ndarray:
        """Return the footprint-safe mask shared with the local controller.

        Callers must treat the cached mask as read-only.
        """
        return self._traversable(snapshot)

    def _traversable(self, snapshot: GridSnapshot) -> np.ndarray:
        cache_key = (snapshot.revision, snapshot.states.shape, snapshot.origin_x_m,
                     snapshot.origin_y_m, snapshot.resolution_m)
        if cache_key == self._traversable_cache_key and self._traversable_cache is not None:
            return self._traversable_cache
        free = snapshot.states == GridState.FREE
        radius = int(math.ceil((self.config.agent_radius_m + self.config.inflation_margin_m) / snapshot.resolution_m))
        # Unknown is a hard boundary for the whole robot footprint. This
        # prevents a center cell marked free from placing the body into unseen
        # space; frontier viewpoints are selected inside the known region.
        blocked_source = snapshot.states != GridState.FREE
        if radius:
            h, w = free.shape
            blocked = np.zeros_like(free)
            for dr in range(-radius, radius + 1):
                for dc in range(-radius, radius + 1):
                    if (dr * dr + dc * dc) * snapshot.resolution_m ** 2 > (self.config.agent_radius_m + self.config.inflation_margin_m) ** 2:
                        continue
                    src_r0, src_r1 = max(0, -dr), min(h, h - dr)
                    src_c0, src_c1 = max(0, -dc), min(w, w - dc)
                    dst_r0, dst_r1 = src_r0 + dr, src_r1 + dr
                    dst_c0, dst_c1 = src_c0 + dc, src_c1 + dc
                    blocked[src_r0:src_r1, src_c0:src_c1] |= blocked_source[dst_r0:dst_r1, dst_c0:dst_c1]
            free &= ~blocked
        if snapshot.spawn_center_xy is not None:
            spawn_cell = snapshot.world_to_cell(*snapshot.spawn_center_xy)
            if spawn_cell is not None and snapshot.states[spawn_cell] == GridState.FREE:
                # The simulator validated the single spawn pose against its
                # radius-eroded navmesh. This exception seeds that pose only;
                # every neighboring step still needs observed footprint space.
                free[spawn_cell] = True
        self._traversable_cache_key = cache_key
        self._traversable_cache = free
        return free


def _inside(shape: tuple[int, int], cell: tuple[int, int]) -> bool:
    return 0 <= cell[0] < shape[0] and 0 <= cell[1] < shape[1]


def _edge_key(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return an undirected, hashable transition key."""
    return (a, b) if a <= b else (b, a)


def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> float:
    dr, dc = abs(a[0] - b[0]), abs(a[1] - b[1])
    return max(dr, dc) + (math.sqrt(2.0) - 1.0) * min(dr, dc)
