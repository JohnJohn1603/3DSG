"""World-aligned structural navigation blocks, ported from OccuSG M7 source."""
from __future__ import annotations

from dataclasses import dataclass
import math

from ..contracts.errors import ContractError
from ..config.schema import NavigationConfig
from ..mapping.projection import FreeSpaceGrid2D


@dataclass(frozen=True, slots=True)
class NavNode:
    block_id: tuple[int, int]
    node_id: str
    # Public order: (min_x, min_y, max_x, max_y), same as Polygon2D.bounds.
    bounds_xy: tuple[float, float, float, float]
    center_xy: tuple[float, float]
    free_cell_count: int
    meets_minimum_free_cells: bool
    representative_cell_rc: tuple[int, int] | None
    representative_center_W: tuple[float, float] | None
    revision: int


@dataclass(frozen=True, slots=True)
class NavEdge:
    source_block: tuple[int, int]
    target_block: tuple[int, int]
    revision: int
    connectivity: int = 8
    evidence: str = "structural_free_cell_bridge"
    robot_footprint_passability: bool = False


@dataclass(frozen=True, slots=True)
class NavGraphSnapshot:
    revision: int
    nodes: tuple[NavNode, ...]
    edges: tuple[NavEdge, ...]
    removed_node_ids: tuple[str, ...]
    full_rescan: bool
    connectivity_scope: str = "structural_connectivity_not_robot_passability"


class NavigationBlockGraph:
    """Stateful block/node manager matching source lifecycle and cell connectivity."""
    def __init__(self, config: NavigationConfig | None = None, *, cell_stride_cells: int | None = None,
                 min_free_cell_count: int | None = None, connectivity: int | None = None) -> None:
        config = config or NavigationConfig()
        cell_stride_cells = config.cell_stride_cells if cell_stride_cells is None else cell_stride_cells
        min_free_cell_count = config.min_free_cell_count if min_free_cell_count is None else min_free_cell_count
        connectivity = config.connectivity if connectivity is None else connectivity
        if type(cell_stride_cells) is not int or cell_stride_cells <= 0:
            raise ContractError("cell_stride_cells must be a positive integer")
        if type(min_free_cell_count) is not int or min_free_cell_count <= 0:
            raise ContractError("min_free_cell_count must be a positive integer")
        if connectivity not in (4, 8) or type(connectivity) is not int:
            raise ContractError("connectivity must be 4 or 8")
        self.cell_stride_cells = cell_stride_cells
        self.min_free_cell_count = min_free_cell_count
        self.connectivity = connectivity
        self.resolution_change_tolerance_m = config.resolution_change_tolerance_m
        self._nodes: dict[tuple[int, int], NavNode] = {}
        self._origin: tuple[float, float] | None = None
        self._resolution: float | None = None
        self._last_revision: int | None = None

    def snapshot(self) -> tuple[NavNode, ...]:
        return tuple(self._nodes[key] for key in sorted(self._nodes))

    def update(self, grid: FreeSpaceGrid2D) -> NavGraphSnapshot:
        if not isinstance(grid, FreeSpaceGrid2D):
            raise ContractError("navigation input must be FreeSpaceGrid2D")
        if grid.kind != "structural_clearance":
            raise ContractError("navigation input must be a structural_clearance grid")
        if grid.frame_id != "world":
            raise ContractError("navigation grid must use canonical world frame")
        if self._last_revision is not None and grid.revision < self._last_revision:
            raise ContractError("navigation grid revision must not move backwards")
        ox, oy = map(float, grid.origin_xy)
        res = float(grid.resolution_m)
        origin_changed = self._origin is not None and (
            abs(ox-self._origin[0]) > 1e-6 or abs(oy-self._origin[1]) > 1e-6)
        resolution_changed = self._resolution is not None and abs(float(self._resolution) - res) > self.resolution_change_tolerance_m
        full_rescan = origin_changed or resolution_changed
        block_size = self.cell_stride_cells * res
        rows, cols = (grid.values == 0).nonzero()
        free_by_block: dict[tuple[int,int], set[tuple[int,int]]] = {}
        for row, col in zip(rows.tolist(), cols.tolist()):
            wx = ox + (col + 0.5) * res
            wy = oy + (row + 0.5) * res
            bid = (math.floor(wx / block_size), math.floor(wy / block_size))
            free_by_block.setdefault(bid, set()).add((col, row))
        counts = {key: len(cells) for key,cells in free_by_block.items()}
        removed: list[str] = []
        if full_rescan:
            for key in tuple(self._nodes):
                if counts.get(key, 0) < self.min_free_cell_count:
                    removed.append(self._nodes.pop(key).node_id)

        keys = set(self._nodes) | set(counts)
        for key in sorted(keys):
            count = counts.get(key, 0)
            if key not in self._nodes and count < self.min_free_cell_count:
                continue
            min_x, max_x = key[0]*block_size, (key[0]+1)*block_size
            min_y, max_y = key[1]*block_size, (key[1]+1)*block_size
            rep = self._representative(key, free_by_block.get(key, set()), grid, block_size)
            rep_world = None if rep is None else (ox+(rep[0]+0.5)*res, oy+(rep[1]+0.5)*res)
            self._nodes[key] = NavNode(
                key, f"nav:{key[0]}:{key[1]}", (min_x,min_y,max_x,max_y),
                ((min_x+max_x)/2,(min_y+max_y)/2), count,
                count >= self.min_free_cell_count, None if rep is None else (rep[1],rep[0]),
                rep_world, grid.revision)

        pairs: set[tuple[tuple[int,int],tuple[int,int]]] = set()
        offsets = ((1,0),(0,1),(1,1),(1,-1)) if self.connectivity == 8 else ((1,0),(0,1))
        for a in self._nodes:
            for dx,dy in offsets:
                b=(a[0]+dx,a[1]+dy)
                if b in self._nodes and self._has_path(a,b,free_by_block,grid,block_size):
                    pairs.add((a,b))
        edges = tuple(edge for a,b in sorted(pairs) for edge in (
            NavEdge(a,b,grid.revision,self.connectivity), NavEdge(b,a,grid.revision,self.connectivity)))
        self._origin, self._resolution, self._last_revision = (ox,oy), res, grid.revision
        return NavGraphSnapshot(grid.revision,self.snapshot(),edges,tuple(sorted(removed)),full_rescan)

    @staticmethod
    def _representative(block: tuple[int,int], cells: set[tuple[int,int]],
                        grid: FreeSpaceGrid2D, block_size: float) -> tuple[int,int] | None:
        if not cells:
            return None
        cx,cy=(block[0]+0.5)*block_size,(block[1]+0.5)*block_size
        ox,oy=grid.origin_xy; res=grid.resolution_m
        return min(cells,key=lambda c: ((ox+(c[0]+0.5)*res-cx)**2 +
                                        (oy+(c[1]+0.5)*res-cy)**2,c[1],c[0]))

    def _has_path(self, a: tuple[int,int], b: tuple[int,int],
                  free: dict[tuple[int,int],set[tuple[int,int]]], grid: FreeSpaceGrid2D,
                  block_size: float) -> bool:
        start=self._representative(a,free.get(a,set()),grid,block_size)
        target=self._representative(b,free.get(b,set()),grid,block_size)
        if start is None or target is None:
            return False
        allowed=free.get(a,set()) | free.get(b,set())
        neighbors=((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1))
        if self.connectivity == 4:
            neighbors=neighbors[:4]
        stack=[start]; visited={start}
        while stack:
            cell=stack.pop()
            if cell == target:
                return True
            for dx,dy in neighbors:
                nxt=(cell[0]+dx,cell[1]+dy)
                if nxt in allowed and nxt not in visited:
                    visited.add(nxt); stack.append(nxt)
        return False
