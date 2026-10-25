"""Versioned file-backed stream for the optional desktop viewer.

This transport is deliberately independent from Habitat, perception and graph
implementations. A future ObjectNav host can publish the same contracts from
another simulator or planner without importing the desktop application.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Mapping

import numpy as np

from .contracts import GridSnapshot, RobotPose2
from .frontier import FrontierSet
from .habitat_online import RGBFrame
from .policy import NavigationDecision


LIVE_SCHEMA = "3dsg-live-state/v1"
GRAPH_SCHEMA = "3dsg-live-graph/v1"


class LiveStatePublisher:
    """Publish latest camera/map state and optional graph revisions atomically."""

    def __init__(self, live_root: str | Path, *, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self.root = Path(live_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = str(run_id)
        self._lock = threading.RLock()
        self._last_map_revision = -1
        self._last_rgb_index = -1
        self._last_rgb_host_s: float | None = None
        self._capture_fps_ema: float | None = None
        self._graph_revision = -1
        self._rgb_sha256: str | None = None
        self._depth_sha256: str | None = None
        self._map_sha256: str | None = None
        self._write_json(self.root / "graph.json", {
            "schema": GRAPH_SCHEMA, "status": "waiting_for_graph_backend",
            "revision": -1, "as_of_frame_id": None, "nodes": [], "edges": [],
        })
        self._write_json(self.root / "state.json", {
            "schema": LIVE_SCHEMA, "run_id": self.run_id,
            "status": "starting", "graph_file": "graph.json",
        })

    def publish_tick(self, *, rgb: RGBFrame, snapshot: GridSnapshot,
                     robot_pose: RobotPose2, frontiers: FrontierSet | None,
                     decision: NavigationDecision | None,
                     command: Mapping[str, Any], depth_m: np.ndarray,
                     intrinsics: Mapping[str, float], T_world_camera: np.ndarray,
                     elapsed_s: float, runtime_status: str = "RUNNING") -> None:
        """Update latest stream files; large arrays are replaced, never appended."""
        if rgb.frame_id != snapshot.as_of_frame_id and snapshot.as_of_frame_index > rgb.frame_index:
            raise ValueError("navigation snapshot is newer than camera frame")
        if rgb.frame_index < self._last_rgb_index:
            raise ValueError("live RGB frame index moved backwards")
        depth = np.asarray(depth_m, dtype=np.float32)
        if depth.shape != (int(intrinsics["height"]), int(intrinsics["width"])):
            raise ValueError("live depth shape differs from intrinsics")
        pose = np.asarray(T_world_camera, dtype=np.float64)
        if pose.shape != (4, 4) or not np.isfinite(pose).all():
            raise ValueError("camera pose must be finite 4x4")

        with self._lock:
            capture_host_s = float(rgb.host_monotonic_s)
            if self._last_rgb_host_s is not None:
                capture_dt = capture_host_s - self._last_rgb_host_s
                if capture_dt > 0:
                    instantaneous_fps = 1.0 / capture_dt
                    self._capture_fps_ema = (instantaneous_fps if self._capture_fps_ema is None
                                             else 0.2 * instantaneous_fps
                                             + 0.8 * self._capture_fps_ema)
            self._last_rgb_host_s = capture_host_s
            self._rgb_sha256 = self._write_npy(self.root / "rgb.npy", rgb.rgb)
            self._depth_sha256 = self._write_npy(self.root / "depth_m.npy", depth)
            if snapshot.revision != self._last_map_revision:
                self._map_sha256 = self._write_npy(
                    self.root / "nav_states.npy", snapshot.states.astype(np.uint8, copy=False))
                self._last_map_revision = snapshot.revision
            self._last_rgb_index = rgb.frame_index
            goal_xy = None
            if decision is not None and decision.frontier is not None:
                goal_xy = list(decision.frontier.viewpoint_world_xy or
                               snapshot.cell_to_world(*decision.frontier.viewpoint_cell))
            frontier_xy = []
            if frontiers is not None:
                for frontier in frontiers.frontiers:
                    frontier_xy.append({
                        "id": frontier.frontier_id,
                        "xy": list(frontier.viewpoint_world_xy or
                                   snapshot.cell_to_world(*frontier.viewpoint_cell)),
                        "gain_cells": frontier.predicted_gain_cells,
                    })
            path_xy = []
            if decision is not None and decision.path is not None and decision.path.success:
                path_xy = [list(snapshot.cell_to_world(r, c))
                           for r, c in decision.path.cells]
            state = {
                "schema": LIVE_SCHEMA,
                "run_id": self.run_id,
                "status": runtime_status,
                "generation_id": f"{self.run_id}:{rgb.frame_id}:nav{snapshot.revision}",
                "elapsed_s": float(elapsed_s),
                "capture_fps": None if self._capture_fps_ema is None
                else round(self._capture_fps_ema, 2),
                "camera": {
                    "frame_id": rgb.frame_id, "frame_index": rgb.frame_index,
                    "simulator_time_s": rgb.simulator_time_s,
                    "host_monotonic_s": rgb.host_monotonic_s,
                    "rgb_file": "rgb.npy", "depth_file": "depth_m.npy",
                    "rgb_sha256": self._rgb_sha256,
                    "depth_sha256": self._depth_sha256,
                    "intrinsics": dict(intrinsics),
                    "T_world_camera": pose.tolist(),
                },
                "navigation": {
                    "revision": snapshot.revision,
                    "as_of_frame_id": snapshot.as_of_frame_id,
                    "as_of_frame_index": snapshot.as_of_frame_index,
                    "resolution_m": snapshot.resolution_m,
                    "origin_x_m": snapshot.origin_x_m,
                    "origin_y_m": snapshot.origin_y_m,
                    "shape": list(snapshot.shape), "states_file": "nav_states.npy",
                    "states_sha256": self._map_sha256,
                    "robot_pose": [robot_pose.x_m, robot_pose.y_m, robot_pose.yaw_rad],
                    "path_xy": path_xy, "frontiers": frontier_xy,
                    "goal_xy": goal_xy,
                    "decision_state": None if decision is None else decision.state,
                    "decision_reason": None if decision is None else decision.reason,
                    "command": dict(command),
                },
                "graph_file": "graph.json",
                "graph_revision": self._graph_revision,
            }
            self._write_json(self.root / "state.json", state)

    def publish_graph(self, graph: Mapping[str, Any]) -> None:
        """Publish a complete graph prefix revision after backend validation.

        Node rows use ``{id, kind, position:[x,y,z], label?, status?}``; edges
        use ``{source, target, relation}``. This is a viewer transport schema,
        not a replacement for the mapper's authoritative graph contract.
        """
        payload = dict(graph)
        revision = int(payload["revision"])
        frame_id = str(payload["as_of_frame_id"])
        if revision <= self._graph_revision:
            raise ValueError("published graph revision must increase monotonically")
        nodes = list(payload.get("nodes", ()))
        ids: set[str] = set()
        for node in nodes:
            node_id = str(node["id"])
            position = np.asarray(node["position"], dtype=np.float64)
            if not node_id or position.shape != (3,) or not np.isfinite(position).all():
                raise ValueError("graph nodes require unique IDs and finite XYZ positions")
            if node_id in ids:
                raise ValueError(f"duplicate graph node ID: {node_id}")
            ids.add(node_id)
        edges = list(payload.get("edges", ()))
        for edge in edges:
            if str(edge["source"]) not in ids or str(edge["target"]) not in ids:
                raise ValueError("graph edge references an absent node")
        row = {
            "schema": GRAPH_SCHEMA, "status": str(payload.get("status", "live")),
            "run_id": self.run_id, "revision": revision,
            "as_of_frame_id": frame_id,
            "host_monotonic_s": float(payload.get("host_monotonic_s", 0.0)),
            "source_capture_monotonic_s": payload.get(
                "provenance", {}).get("source_capture_monotonic_s"),
            "source_capture_monotonic_domain": payload.get(
                "provenance", {}).get("source_capture_monotonic_domain"),
            "backend": str(payload.get("backend", "unknown")),
            "nodes": nodes, "edges": edges,
        }
        source_capture_s = row["source_capture_monotonic_s"]
        if source_capture_s is not None:
            source_capture_s = float(source_capture_s)
            if not np.isfinite(source_capture_s):
                raise ValueError("graph source capture time must be finite")
            row["source_capture_monotonic_s"] = source_capture_s
        with self._lock:
            if revision <= self._graph_revision:
                raise ValueError("graph revision raced with another publisher")
            self._write_json(self.root / "graph.json", row)
            self._graph_revision = revision
            state_path = self.root / "state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                state = {"schema": LIVE_SCHEMA, "run_id": self.run_id}
            state["graph_revision"] = revision
            self._write_json(state_path, state)

    def close(self, *, status: str = "stopped") -> None:
        with self._lock:
            path = self.root / "state.json"
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                state = {"schema": LIVE_SCHEMA, "run_id": self.run_id}
            state["status"] = str(status)
            self._write_json(path, state)

    @staticmethod
    def _write_npy(path: Path, array: np.ndarray) -> str:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                np.save(stream, array, allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return digest
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                               allow_nan=False) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
