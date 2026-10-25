from pathlib import Path
import inspect

from standalone_sg.pipeline import GraphEdge, GraphNode, SceneGraphSnapshot
from standalone_sg.visualization import (
    render_relationship_graph,
    render_snapshot_views,
    render_spatial_map,
)


def _snapshot():
    nodes = (
        GraphNode("room:1", "room", 0.0, 1.0, "test", {
            "name": "Room 1", "anchor_region_id": 1,
            "outer_ring": ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)),
            "centroid_xy": (1.0, 1.0),
        }),
        GraphNode("room:2", "room", 0.0, 1.0, "test", {
            "name": "Room 2", "anchor_region_id": 2,
            "outer_ring": ((2.0, 0.0), (4.0, 0.0), (4.0, 2.0), (2.0, 2.0)),
            "centroid_xy": (3.0, 1.0),
        }),
        GraphNode("agent:0", "agent", 0.0, 0.0, "test", {
            "pose_W": (0.5, 1.0, 0.0), "frame_id": "f0", "frame_index": 0,
        }),
        GraphNode("agent:1", "agent", 1.0, 1.0, "test", {
            "pose_W": (2.5, 1.0, 0.0), "frame_id": "f1", "frame_index": 1,
        }),
        GraphNode("object:chair", "object", 0.0, 1.0, "test", {
            "center_W": (1.4, 1.2, 0.4),
        }),
        GraphNode("nav:0", "navigation", 0.0, 1.0, "test", {
            "center_xy": (1.0, 1.0), "bounds_xy": (0.0, 2.0, 0.0, 2.0),
        }),
    )
    edges = (
        GraphEdge("room:1", "agent:0", "room_contains", "test", True),
        GraphEdge("room:2", "agent:1", "room_contains", "test", True),
        GraphEdge("room:1", "room:2", "room_adjacency", "test", False),
        GraphEdge("agent:0", "agent:1", "temporal_successor", "test", True),
        GraphEdge("nav:0", "nav:0", "navigable_path", "test", True),
    )
    # Do not include the invalid self-edge in the snapshot; exercise navigation
    # filtering with a valid pair instead.
    edges = edges[:-1] + (GraphEdge("room:1", "room:2", "navigable_path", "test", True),)
    return SceneGraphSnapshot(1, "scene", "seq", 2, 1.0, nodes, edges, "test", {"regions": False})


def test_default_views_are_separate_and_do_not_mutate_snapshot(tmp_path: Path):
    snapshot = _snapshot()
    before = snapshot
    outputs = render_snapshot_views(snapshot, tmp_path, include_debug=True)
    assert snapshot == before
    assert set(outputs) == {"spatial_map", "relationship_graph", "trajectory_rooms", "scene_graph", "scene_graph_debug"}
    assert all(path.is_file() and path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for path in outputs.values())


def test_default_relationship_view_filters_navigation_and_temporal_mesh():
    signature = inspect.signature(render_relationship_graph)
    assert signature.parameters["show_navigation"].default is False
    assert signature.parameters["show_temporal"].default is False
    assert signature.parameters["label_kinds"].default == ("room",)


def test_spatial_map_supports_explicit_debug_filters(tmp_path: Path):
    snapshot = _snapshot()
    render_spatial_map(snapshot, tmp_path / "filtered.png", show_navigation=True, show_objects=True, label_kinds=("room", "object"))
    assert (tmp_path / "filtered.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
