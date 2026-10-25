"""Headless M10 graph views derived from one immutable snapshot.

The spatial view preserves world coordinates.  The relationship view uses a
presentation-only hierarchical layout; it never changes the graph or its
serialized coordinates.  Navigation and temporal edges are explicit filters
so a readable default does not hide the debug view.
"""
from __future__ import annotations

from collections import defaultdict
import os
from pathlib import Path
import tempfile
from typing import Iterable, Sequence

import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Polygon as PolygonPatch

from ..contracts.errors import ContractError
from ..pipeline.contracts import GraphEdge, GraphNode, SceneGraphSnapshot
from ..serialization.json_graph import trajectory_rows


COLORS = {
    "agent": "#1f77b4",
    "object": "#d95f02",
    "navigation": "#2a9d8f",
    "room": "#7b61a8",
    "edge": "#6b7280",
    "temporal": "#3b82f6",
}
MARKERS = {"agent": "o", "object": "s", "navigation": ".", "room": "D"}
_NAVIGATION_RELATIONS = {"navigable_path"}
_TEMPORAL_RELATIONS = {"temporal_successor"}


def _atomic_savefig(fig, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".png", dir=path.parent)
    os.close(fd)
    try:
        fig.savefig(tmp, format="png", dpi=160, bbox_inches="tight", facecolor="white")
        with open(tmp, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    finally:
        plt.close(fig)


def _bounds(ax, points):
    if not points:
        ax.set_xlim(-1.0, 1.0)
        ax.set_ylim(-1.0, 1.0)
    else:
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        dx = max(max(xs) - min(xs), 1.0)
        dy = max(max(ys) - min(ys), 1.0)
        pad = 0.08 * max(dx, dy)
        ax.set_xlim(min(xs) - pad, max(xs) + pad)
        ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("World X (m)")
    ax.set_ylabel("World Y (m)")
    ax.grid(True, alpha=0.18)


def _rooms(snapshot: SceneGraphSnapshot):
    result = []
    for node in snapshot.nodes:
        if node.kind != "room":
            continue
        ring = node.attributes.get("outer_ring")
        if ring:
            result.append((node, ring))
    return result


def _node_xy(node: GraphNode):
    attrs = node.attributes
    if node.kind == "agent":
        pose = attrs.get("pose_W")
        return None if pose is None else (float(pose[0]), float(pose[1]))
    if node.kind == "object":
        center = attrs.get("center_W")
        return None if center is None else (float(center[0]), float(center[1]))
    key = "center_xy" if node.kind == "navigation" else "centroid_xy"
    value = attrs.get(key)
    return None if value is None else (float(value[0]), float(value[1]))


def _is_navigation_edge(edge: GraphEdge, node_map: dict[str, GraphNode]) -> bool:
    return (
        edge.relation in _NAVIGATION_RELATIONS
        or node_map.get(edge.source_id, None) is not None
        and node_map[edge.source_id].kind == "navigation"
        or node_map.get(edge.target_id, None) is not None
        and node_map[edge.target_id].kind == "navigation"
    )


def _is_temporal_edge(edge: GraphEdge) -> bool:
    return edge.relation in _TEMPORAL_RELATIONS


def _room_label(node: GraphNode) -> str:
    base = str(node.attributes.get("name", node.node_id))
    status = node.attributes.get("semantic_status")
    label = node.attributes.get("semantic_label")
    if status == "predicted" and isinstance(label, str) and label:
        return f"{base} [{label}]"
    if status in {"unknown", "ambiguous", "stale"}:
        return f"{base} [{status}]"
    return base


def _add_room_footprints(ax, snapshot, points, *, label_rooms: bool = True):
    stale = bool(snapshot.stale_flags.get("regions", False))
    room_label_added = False
    for node, ring in _rooms(snapshot):
        ring_points = [(float(x), float(y)) for x, y in ring]
        patch = PolygonPatch(
            ring_points,
            closed=True,
            facecolor="none" if stale else COLORS["room"],
            edgecolor=COLORS["room"],
            alpha=1.0 if stale else 0.16,
            linestyle="--" if stale else "-",
            hatch="///" if stale else None,
            linewidth=1.8,
            label=("Room footprint (stale)" if stale else "Room footprint")
            if not room_label_added
            else "_nolegend_",
        )
        ax.add_patch(patch)
        points.extend(ring_points)
        if label_rooms:
            centroid = node.attributes.get("centroid_xy")
            if centroid is not None:
                ax.annotate(
                    _room_label(node),
                    (float(centroid[0]), float(centroid[1])),
                    fontsize=9,
                    ha="center",
                    va="center",
                    color=COLORS["room"],
                    weight="bold",
                )
        room_label_added = True


def _add_trajectory(ax, snapshot, points):
    rows = trajectory_rows(snapshot)
    if not rows:
        return
    xs = [float(row["x_m"]) for row in rows]
    ys = [float(row["y_m"]) for row in rows]
    ax.plot(
        xs,
        ys,
        color=COLORS["agent"],
        linewidth=2.0,
        alpha=0.9,
        zorder=4,
        label="Agent trajectory",
    )
    ax.scatter([xs[0]], [ys[0]], color="#2ca02c", marker="o", s=42, zorder=5, label="Start")
    ax.scatter([xs[-1]], [ys[-1]], color="#d62728", marker="^", s=52, zorder=5, label="Latest pose")
    points.extend(zip(xs, ys))


def render_spatial_map(
    snapshot: SceneGraphSnapshot,
    path,
    *,
    show_navigation: bool = False,
    show_objects: bool = False,
    label_kinds: Sequence[str] = ("room",),
):
    """Render a world-coordinate spatial map without changing the snapshot.

    Defaults intentionally show room labels and the accumulated agent path,
    while hiding M7 navigation mesh.  ``show_navigation`` and
    ``show_objects`` are presentation filters only.
    """
    if not isinstance(snapshot, SceneGraphSnapshot):
        raise ContractError("expected SceneGraphSnapshot")
    label_kinds = set(label_kinds)
    fig, ax = plt.subplots(figsize=(9, 7))
    points = []
    _add_room_footprints(ax, snapshot, points, label_rooms="room" in label_kinds)
    _add_trajectory(ax, snapshot, points)

    node_map = {node.node_id: node for node in snapshot.nodes}
    if show_navigation:
        for node in snapshot.nodes:
            if node.kind != "navigation":
                continue
            xy = _node_xy(node)
            if xy is not None:
                ax.scatter(*xy, color=COLORS["navigation"], marker=MARKERS["navigation"], s=18, alpha=0.65, zorder=2)
                points.append(xy)
        for edge in snapshot.edges:
            if not _is_navigation_edge(edge, node_map):
                continue
            source = _node_xy(node_map.get(edge.source_id)) if node_map.get(edge.source_id) else None
            target = _node_xy(node_map.get(edge.target_id)) if node_map.get(edge.target_id) else None
            if source is not None and target is not None:
                ax.plot(
                    (source[0], target[0]),
                    (source[1], target[1]),
                    color=COLORS["navigation"],
                    linewidth=0.7,
                    alpha=0.32,
                    zorder=1,
                    label="Navigation mesh" if not any(line.get_label() == "Navigation mesh" for line in ax.lines) else "_nolegend_",
                )

    if show_objects:
        object_points = []
        for node in snapshot.nodes:
            if node.kind != "object":
                continue
            xy = _node_xy(node)
            if xy is None:
                continue
            object_points.append(xy)
            ax.scatter(*xy, color=COLORS["object"], marker=MARKERS["object"], s=42, zorder=5, label="Object" if len(object_points) == 1 else "_nolegend_")
            if "object" in label_kinds:
                ax.annotate(node.node_id, xy, xytext=(4, 4), textcoords="offset points", fontsize=7)
        points.extend(object_points)

    if snapshot.stale_flags.get("regions", False):
        ax.text(0.02, 0.02, "STALE ROOMS — last accepted footprints", transform=ax.transAxes, va="bottom", color="#a33", weight="bold")
    ax.set_title("Spatial map — world coordinates")
    _bounds(ax, points)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(handles, labels, loc="best", fontsize=8)
    fig.tight_layout()
    _atomic_savefig(fig, path)


def _relationship_layout(snapshot: SceneGraphSnapshot, visible: set[str]):
    node_map = {node.node_id: node for node in snapshot.nodes}
    rooms = sorted((node for node in snapshot.nodes if node.node_id in visible and node.kind == "room"), key=lambda node: node.node_id)
    positions = {}
    for index, node in enumerate(rooms):
        positions[node.node_id] = (float(index * 3.0), 2.0)

    owners = {}
    for edge in snapshot.edges:
        if edge.relation == "room_contains" and edge.source_id in visible and edge.target_id in visible:
            owners[edge.target_id] = edge.source_id
    grouped = defaultdict(list)
    unowned = []
    for node in sorted(snapshot.nodes, key=lambda item: item.node_id):
        if node.node_id not in visible or node.kind == "room":
            continue
        owner = owners.get(node.node_id)
        if owner in positions:
            grouped[owner].append(node)
        else:
            unowned.append(node)
    for room_index, room in enumerate(rooms):
        group = grouped.get(room.node_id, [])
        for item_index, node in enumerate(group):
            offset = (item_index - (len(group) - 1) / 2.0) * 0.8
            positions[node.node_id] = (room_index * 3.0 + offset, 0.0)
    for index, node in enumerate(unowned):
        positions[node.node_id] = (float(index * 1.2), -1.8)
    navigation = [node for node in snapshot.nodes if node.node_id in visible and node.kind == "navigation"]
    for index, node in enumerate(sorted(navigation, key=lambda item: item.node_id)):
        positions[node.node_id] = (float(index * 0.8), -3.2)
    return node_map, positions


def render_relationship_graph(
    snapshot: SceneGraphSnapshot,
    path,
    *,
    show_navigation: bool = False,
    show_temporal: bool = False,
    show_agents: bool = True,
    show_objects: bool = True,
    label_kinds: Sequence[str] = ("room",),
):
    """Render a hierarchical relationship view from the same snapshot.

    Node positions here are layout coordinates, not world coordinates.  The
    default hides navigation and temporal mesh edges and labels only rooms;
    ``render_debug_graph`` exposes the full filtered relationship view.
    """
    if not isinstance(snapshot, SceneGraphSnapshot):
        raise ContractError("expected SceneGraphSnapshot")
    label_kinds = set(label_kinds)
    visible_kinds = {"room"}
    if show_agents:
        visible_kinds.add("agent")
    if show_objects:
        visible_kinds.add("object")
    if show_navigation:
        visible_kinds.add("navigation")
    visible = {node.node_id for node in snapshot.nodes if node.kind in visible_kinds}
    node_map, positions = _relationship_layout(snapshot, visible)

    fig, ax = plt.subplots(figsize=(11, 7))
    for edge in snapshot.edges:
        if edge.source_id not in positions or edge.target_id not in positions:
            continue
        if _is_navigation_edge(edge, node_map) and not show_navigation:
            continue
        if _is_temporal_edge(edge) and not show_temporal:
            continue
        source = positions[edge.source_id]
        target = positions[edge.target_id]
        color = COLORS["temporal"] if _is_temporal_edge(edge) else COLORS["edge"]
        linestyle = ":" if _is_temporal_edge(edge) else ("--" if not edge.directed else "-")
        if edge.directed:
            ax.annotate(
                "",
                xy=target,
                xytext=source,
                arrowprops={"arrowstyle": "-|>", "color": color, "linewidth": 1.0, "linestyle": linestyle, "alpha": 0.65},
            )
        else:
            ax.plot((source[0], target[0]), (source[1], target[1]), color=color, linewidth=1.0, linestyle=linestyle, alpha=0.65)

    stale_rooms = bool(snapshot.stale_flags.get("regions", False))
    for node in snapshot.nodes:
        if node.node_id not in positions:
            continue
        x, y = positions[node.node_id]
        stale = node.kind == "room" and stale_rooms
        ax.scatter(
            [x],
            [y],
            color=COLORS[node.kind],
            marker=MARKERS[node.kind],
            s=145 if node.kind == "room" else (54 if node.kind != "navigation" else 22),
            facecolors="none" if stale else COLORS[node.kind],
            edgecolors=COLORS[node.kind],
            linewidths=1.5 if stale else 0.7,
            zorder=3,
            label=node.kind.title() if not any(item.get_label() == node.kind.title() for item in ax.collections) else "_nolegend_",
        )
        if node.kind in label_kinds:
            label = _room_label(node) if node.kind == "room" else node.node_id
            ax.annotate(str(label), (x, y), xytext=(5, 5), textcoords="offset points", fontsize=9, weight="bold" if node.kind == "room" else "normal")

    title = "Relationship graph — hierarchical presentation layout"
    if stale_rooms:
        title += " — stale room provenance"
    ax.set_title(title)
    ax.axis("off")
    legend = [
        Line2D([0], [0], marker=MARKERS["room"], color="w", markerfacecolor=COLORS["room"], markeredgecolor=COLORS["room"], label="Room", markersize=9),
        Line2D([0], [0], marker=MARKERS["agent"], color="w", markerfacecolor=COLORS["agent"], markeredgecolor=COLORS["agent"], label="Agent observation", markersize=7),
    ]
    if show_objects:
        legend.append(Line2D([0], [0], marker=MARKERS["object"], color="w", markerfacecolor=COLORS["object"], markeredgecolor=COLORS["object"], label="Object", markersize=7))
    if show_navigation:
        legend.append(Line2D([0], [0], color=COLORS["navigation"], linestyle="--", label="Navigation relation"))
    if show_temporal:
        legend.append(Line2D([0], [0], color=COLORS["temporal"], linestyle=":", label="Agent temporal relation"))
    ax.legend(handles=legend, loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8, borderaxespad=0.0)
    fig.tight_layout()
    _atomic_savefig(fig, path)


def render_debug_graph(
    snapshot: SceneGraphSnapshot,
    path,
    *,
    show_navigation: bool = True,
    show_temporal: bool = True,
    show_agents: bool = True,
    show_objects: bool = True,
    label_kinds: Sequence[str] = ("room", "agent", "object", "navigation"),
):
    """Full relationship view with explicit presentation-only filters."""
    return render_relationship_graph(
        snapshot,
        path,
        show_navigation=show_navigation,
        show_temporal=show_temporal,
        show_agents=show_agents,
        show_objects=show_objects,
        label_kinds=label_kinds,
    )


def render_trajectory_map(snapshot: SceneGraphSnapshot, path):
    """Compatibility name for the clean world-coordinate spatial view."""
    return render_spatial_map(snapshot, path)


def render_graph(snapshot: SceneGraphSnapshot, path):
    """Compatibility name for the clean hierarchical relationship view."""
    return render_relationship_graph(snapshot, path)


def render_snapshot_views(snapshot: SceneGraphSnapshot, output_dir, *, include_debug: bool = False):
    """Write the two clean views and retain the historical filenames."""
    output_dir = Path(output_dir)
    render_spatial_map(snapshot, output_dir / "spatial_map.png")
    render_relationship_graph(snapshot, output_dir / "relationship_graph.png")
    render_trajectory_map(snapshot, output_dir / "trajectory_rooms.png")
    render_graph(snapshot, output_dir / "scene_graph.png")
    if include_debug:
        render_debug_graph(snapshot, output_dir / "scene_graph_debug.png")
    return {
        "spatial_map": output_dir / "spatial_map.png",
        "relationship_graph": output_dir / "relationship_graph.png",
        "trajectory_rooms": output_dir / "trajectory_rooms.png",
        "scene_graph": output_dir / "scene_graph.png",
        **({"scene_graph_debug": output_dir / "scene_graph_debug.png"} if include_debug else {}),
    }
