"""Native PyQt5 three-panel live viewer and optional Habitat run launcher.

The UI consumes only ``3dsg-live-state/v1`` files. It does not import the
simulator, navigation runtime, detector or scene-graph backend, so it can also
view recordings produced by another ObjectNav host that implements the same
stream contract.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import hashlib
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import uuid

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets


ROOT = Path(__file__).resolve().parents[1]
from .handoff_paths import interpreter
DEFAULT_RUNNER_PYTHON = interpreter("habitat")


class _Panel(QtWidgets.QWidget):
    def __init__(self, title: str):
        super().__init__()
        self.title = title
        self.setMinimumSize(280, 220)
        self.setAutoFillBackground(False)

    def _begin(self, painter: QtGui.QPainter, subtitle: str = "") -> QtCore.QRectF:
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.fillRect(rect, QtGui.QColor("#111827"))
        painter.setPen(QtGui.QPen(QtGui.QColor("#334155"), 1))
        painter.drawRect(rect)
        painter.setPen(QtGui.QColor("#e2e8f0"))
        painter.setFont(QtGui.QFont("Sans Serif", 10, QtGui.QFont.DemiBold))
        painter.drawText(QtCore.QRectF(rect.left() + 12, rect.top() + 6,
                                       rect.width() - 24, 22),
                         QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self.title)
        if subtitle:
            painter.setPen(QtGui.QColor("#94a3b8"))
            painter.setFont(QtGui.QFont("Sans Serif", 8))
            subtitle = painter.fontMetrics().elidedText(
                subtitle, QtCore.Qt.ElideRight, int(rect.width() - 24))
            painter.drawText(QtCore.QRectF(rect.left() + 12, rect.top() + 28,
                                           rect.width() - 24, 18),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, subtitle)
        return QtCore.QRectF(rect.left() + 8, rect.top() + 50,
                             rect.width() - 16, rect.height() - 58)


class _RGBPanel(_Panel):
    def __init__(self):
        super().__init__("RGB camera")
        self.image: QtGui.QImage | None = None
        self.subtitle = "waiting for camera"

    def update_frame(self, rgb: np.ndarray | None, subtitle: str) -> None:
        self.subtitle = subtitle
        self.image = None
        if rgb is not None:
            arr = np.ascontiguousarray(rgb, dtype=np.uint8)
            if arr.ndim == 3 and arr.shape[2] == 3:
                h, w = arr.shape[:2]
                self.image = QtGui.QImage(arr.data, w, h, 3 * w,
                                          QtGui.QImage.Format_RGB888).copy()
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        area = self._begin(painter, self.subtitle)
        if self.image is None:
            painter.setPen(QtGui.QColor("#64748b"))
            painter.drawText(area, QtCore.Qt.AlignCenter, "Waiting for RGB frame…")
        else:
            scaled = self.image.scaled(area.size().toSize(), QtCore.Qt.KeepAspectRatio,
                                       QtCore.Qt.SmoothTransformation)
            target = QtCore.QRectF(area.center().x() - scaled.width() / 2,
                                   area.center().y() - scaled.height() / 2,
                                   scaled.width(), scaled.height())
            painter.drawImage(target, scaled)


class _MapPanel(_Panel):
    def __init__(self):
        super().__init__("Top-down occupancy / frontier map")
        self.states: np.ndarray | None = None
        self.nav: dict = {}
        self.camera_index = -1

    def set_map(self, states: np.ndarray | None, nav: dict, camera_index: int):
        self.states = states
        self.nav = nav
        self.camera_index = camera_index
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        rev = self.nav.get("revision", "—")
        frame = self.nav.get("as_of_frame_id", "—")
        nav_index = self.nav.get("as_of_frame_index")
        lag = (self.camera_index - int(nav_index)
               if isinstance(nav_index, int) and self.camera_index >= nav_index else None)
        lag_text = "—" if lag is None else f"{lag} frames behind camera"
        area = self._begin(painter, f"nav revision {rev} · as-of {frame} · {lag_text}")
        states = self.states
        if states is None or states.ndim != 2:
            painter.setPen(QtGui.QColor("#64748b"))
            painter.drawText(area, QtCore.Qt.AlignCenter, "Waiting for occupancy map…")
            return
        # Unknown slate, known free light, occupied dark. Flip so +world-Y is up.
        palette = np.array([[68, 78, 91], [220, 228, 236], [15, 23, 42]], dtype=np.uint8)
        colored = palette[np.clip(states.astype(np.int16), 0, 2)]
        colored = np.ascontiguousarray(np.flipud(colored))
        h, w = colored.shape[:2]
        image = QtGui.QImage(colored.data, w, h, 3 * w,
                             QtGui.QImage.Format_RGB888).copy()
        map_area = QtCore.QRectF(area.left(), area.top(), area.width(),
                                 max(1.0, area.height() - 24.0))
        image = image.scaled(map_area.size().toSize(), QtCore.Qt.KeepAspectRatio,
                             QtCore.Qt.FastTransformation)
        target = QtCore.QRectF(map_area.center().x() - image.width() / 2,
                               map_area.center().y() - image.height() / 2,
                               image.width(), image.height())
        painter.drawImage(target, image)
        ox = float(self.nav.get("origin_x_m", 0.0))
        oy = float(self.nav.get("origin_y_m", 0.0))
        res = float(self.nav.get("resolution_m", 0.05))
        rows, cols = states.shape

        def pixel(xy):
            x, y = map(float, xy)
            col = (x - ox) / res
            row = rows - 1 - (y - oy) / res
            return QtCore.QPointF(target.left() + col / cols * target.width(),
                                  target.top() + row / rows * target.height())

        path = self.nav.get("path_xy", [])
        if len(path) > 1:
            painter.setPen(QtGui.QPen(QtGui.QColor("#2563eb"), 2.0))
            points = [pixel(xy) for xy in path]
            for a, b in zip(points, points[1:]):
                painter.drawLine(a, b)
        for frontier in self.nav.get("frontiers", []):
            point = pixel(frontier["xy"])
            painter.setPen(QtGui.QPen(QtGui.QColor("#f59e0b"), 2))
            painter.setBrush(QtGui.QColor(245, 158, 11, 90))
            painter.drawEllipse(point, 4.5, 4.5)
        goal = self.nav.get("goal_xy")
        if goal:
            point = pixel(goal)
            painter.setPen(QtGui.QPen(QtGui.QColor("#ef4444"), 2))
            painter.setBrush(QtGui.QColor("#ef4444"))
            painter.drawEllipse(point, 6, 6)
        pose = self.nav.get("robot_pose")
        if pose:
            point = pixel(pose[:2])
            yaw = float(pose[2])
            tip = QtCore.QPointF(point.x() + 15 * math.cos(yaw),
                                 point.y() - 15 * math.sin(yaw))
            painter.setPen(QtGui.QPen(QtGui.QColor("#22c55e"), 3))
            painter.drawLine(point, tip)
            painter.setBrush(QtGui.QColor("#22c55e"))
            painter.drawEllipse(point, 4, 4)
        painter.setPen(QtGui.QColor("#cbd5e1"))
        painter.setFont(QtGui.QFont("Sans Serif", 8))
        footer = QtCore.QRectF(area.left() + 4, area.bottom() - 18,
                               area.width() - 8, 16)
        _draw_elided(painter, footer,
                     f"{res:.3f} m/cell · green robot · blue path · amber frontiers · red goal",
                     color="#cbd5e1")


class _SceneGraphPanel(_Panel):
    def __init__(self):
        super().__init__("3D scene / scene graph")
        self.rgbd: tuple[np.ndarray, dict, np.ndarray] | None = None
        self.frame_id: str | None = None
        self.graph: dict = {}
        self.cloud: OrderedDict[tuple[int, int, int], tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self.yaw = math.radians(35)
        self.pitch = math.radians(28)
        self._drag_pos: QtCore.QPoint | None = None
        self.setMouseTracking(True)

    def set_frame(self, frame_id: str, rgb: np.ndarray, depth: np.ndarray,
                  intrinsics: dict, camera_pose: np.ndarray, graph: dict):
        if frame_id != self.frame_id:
            self._add_cloud(depth, rgb, intrinsics, camera_pose)
            self.frame_id = frame_id
        self.graph = graph
        self.update()

    def _add_cloud(self, depth, rgb, intr, pose):
        stride = max(1, int(max(depth.shape) / 42))
        vv, uu = np.mgrid[0:depth.shape[0]:stride, 0:depth.shape[1]:stride]
        z = depth[vv, uu].astype(np.float64)
        ok = np.isfinite(z) & (z > 0.15) & (z < 8.0)
        if not np.any(ok):
            return
        u = uu[ok].astype(np.float64)
        v = vv[ok].astype(np.float64)
        z = z[ok]
        xyz_c = np.column_stack(((u - float(intr["cx"])) * z / float(intr["fx"]),
                                 (v - float(intr["cy"])) * z / float(intr["fy"]), z))
        xyz = xyz_c @ np.asarray(pose, dtype=np.float64)[:3, :3].T + np.asarray(pose)[:3, 3]
        colors = rgb[vv[ok], uu[ok]].astype(np.uint8)
        voxel = np.floor(xyz / 0.08).astype(np.int32)
        for point, color, key_arr in zip(xyz, colors, voxel):
            key = tuple(int(x) for x in key_arr)
            self.cloud[key] = (point.astype(np.float32), color)
            self.cloud.move_to_end(key)
        while len(self.cloud) > 26000:
            self.cloud.popitem(last=False)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = event.pos()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            delta = event.pos() - self._drag_pos
            self.yaw += delta.x() * 0.008
            self.pitch = min(1.35, max(-1.35, self.pitch + delta.y() * 0.008))
            self._drag_pos = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = None

    def wheelEvent(self, event):
        self._zoom = getattr(self, "_zoom", 1.0) * (1.1 if event.angleDelta().y() > 0 else 0.9)
        self._zoom = min(4.0, max(0.25, self._zoom))
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        frame = self.frame_id or "—"
        graph_rev = self.graph.get("revision", "—")
        status = self.graph.get("status", "waiting_for_graph_backend")
        graph_frame = str(self.graph.get("as_of_frame_id", "—"))
        graph_index = -1
        try:
            graph_index = int(graph_frame.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            pass
        camera_index = -1
        try:
            camera_index = int(str(frame).rsplit(":", 1)[1])
        except (IndexError, ValueError):
            pass
        graph_lag = (f"{camera_index - graph_index} frames behind camera"
                     if camera_index >= 0 and graph_index >= 0
                     else "frame alignment pending")
        graph_host_s = self.graph.get("host_monotonic_s")
        publication_age = "publish age unknown"
        if isinstance(graph_host_s, (int, float)):
            age_s = time.monotonic() - float(graph_host_s)
            if age_s >= 0:
                publication_age = f"published {age_s:.1f}s ago"
        source_host_s = self.graph.get("source_capture_monotonic_s")
        source_age = "source age unknown"
        if isinstance(source_host_s, (int, float)):
            age_s = time.monotonic() - float(source_host_s)
            if age_s >= 0:
                source_age = f"source {age_s:.1f}s old"
        area = self._begin(
            painter,
            f"RGB-D point cloud · graph r{graph_rev} · as-of {graph_frame} · "
            f"{graph_lag} · {source_age} · {publication_age} · {status} · RGB-D {frame}")
        nodes = self.graph.get("nodes", [])
        edges = self.graph.get("edges", [])
        points = [item[0] for item in self.cloud.values()]
        colors = [item[1] for item in self.cloud.values()]
        for node in nodes:
            points.append(np.asarray(node["position"], dtype=np.float32))
            colors.append(_kind_color(str(node.get("kind", "object"))))
        if not points:
            painter.setPen(QtGui.QColor("#64748b"))
            painter.drawText(area, QtCore.Qt.AlignCenter,
                             "Waiting for RGB-D; graph backend has not published a revision")
            return
        xyz = np.asarray(points, dtype=np.float64)
        center = np.median(xyz, axis=0)
        yaw = self.yaw
        pitch = self.pitch
        relative = xyz - center
        x1 = math.cos(yaw) * relative[:, 0] - math.sin(yaw) * relative[:, 1]
        y1 = math.sin(yaw) * relative[:, 0] + math.cos(yaw) * relative[:, 1]
        z1 = relative[:, 2]
        y2 = math.cos(pitch) * y1 - math.sin(pitch) * z1
        z2 = math.sin(pitch) * y1 + math.cos(pitch) * z1
        span = max(float(np.ptp(x1)), float(np.ptp(y2)), float(np.ptp(z2)), 1.0)
        scale = min(area.width(), area.height()) * 0.78 / span * getattr(self, "_zoom", 1.0)
        sx = area.center().x() + x1 * scale
        sy = area.center().y() - y2 * scale
        cloud_count = len(self.cloud)
        for i in range(cloud_count):
            c = colors[i]
            painter.setPen(QtGui.QPen(QtGui.QColor(int(c[0]), int(c[1]), int(c[2]), 170), 2))
            painter.drawPoint(QtCore.QPointF(sx[i], sy[i]))
        node_indices = {}
        for offset, node in enumerate(nodes, start=cloud_count):
            node_indices[str(node["id"])] = offset
        painter.setPen(QtGui.QPen(QtGui.QColor("#94a3b8"), 1.4))
        for edge in edges:
            a, b = node_indices.get(str(edge["source"])), node_indices.get(str(edge["target"]))
            if a is not None and b is not None:
                painter.drawLine(QtCore.QPointF(sx[a], sy[a]), QtCore.QPointF(sx[b], sy[b]))
        painter.setFont(QtGui.QFont("Sans Serif", 8))
        label_rects = []
        label_anchors = []
        ordered = sorted(enumerate(nodes, start=cloud_count),
                         key=lambda pair: {"robot": 0, "object": 1,
                                           "navigation": 2, "viewpoint": 3}.get(
                                               str(pair[1].get("kind", "")), 4))
        for i, node in ordered:
            color = _kind_color(str(node.get("kind", "object")))
            painter.setPen(QtGui.QPen(QtGui.QColor("#0f172a"), 1))
            painter.setBrush(QtGui.QColor(*color))
            painter.drawEllipse(QtCore.QPointF(sx[i], sy[i]), 6, 6)
            label = _compact_node_label(node)
            anchor = QtCore.QPointF(sx[i], sy[i])
            if any(math.hypot(anchor.x() - old.x(), anchor.y() - old.y()) < 62.0
                   for old in label_anchors):
                continue
            metrics = painter.fontMetrics()
            label_width = min(metrics.horizontalAdvance(label), 150)
            label_height = metrics.height()
            candidates = ((8, -4), (8, -label_height - 3),
                          (-label_width - 8, -4), (8, 5))
            chosen = None
            for dx, dy in candidates:
                rect = QtCore.QRectF(sx[i] + dx, sy[i] + dy - label_height,
                                     label_width + 2, label_height + 2)
                if rect.left() < area.left() or rect.right() > area.right():
                    continue
                if all(not rect.adjusted(-2, -1, 2, 1).intersects(old)
                       for old in label_rects):
                    chosen = rect
                    break
            if chosen is not None:
                label_rects.append(chosen)
                label_anchors.append(anchor)
                painter.setPen(QtGui.QColor("#e2e8f0"))
                painter.drawText(chosen.adjusted(1, 0, 0, 0),
                                 QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, label)
            painter.setPen(QtGui.QColor("#e2e8f0"))
            painter.setFont(QtGui.QFont("Sans Serif", 8))
            painter.drawText(QtCore.QPointF(sx[i] + 8, sy[i] - 4), label[:32])
        painter.setPen(QtGui.QColor("#94a3b8"))
        painter.setFont(QtGui.QFont("Sans Serif", 8))
        footer = QtCore.QRectF(area.left() + 4, area.bottom() - 17,
                               area.width() - 8, 15)
        _draw_elided(painter, footer,
                     f"{cloud_count:,} voxel points · {len(nodes)} nodes · {len(edges)} relations · "
                     "gold objects · purple views · drag orbit / wheel zoom",
                     color="#94a3b8")


def _compact_node_label(node: dict) -> str:
    kind = str(node.get("kind", ""))
    if kind == "object":
        return f"{node.get('class', 'object')} · P · {int(node.get('observations', 0))}v"
    if kind == "viewpoint":
        return f"V{str(node.get('id', '')).removeprefix('view-')}"
    if kind == "robot":
        return "Robot"
    if kind == "navigation":
        return str(node.get("label", "frontier"))[:22]
    return str(node.get("label", node.get("id", "")))[:22]


def _draw_elided(painter: QtGui.QPainter, rect: QtCore.QRectF,
                 text: str, *, color: str) -> None:
    painter.setPen(QtGui.QColor(color))
    text = painter.fontMetrics().elidedText(text, QtCore.Qt.ElideRight,
                                            max(0, int(rect.width())))
    painter.drawText(rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)


def _kind_color(kind: str) -> tuple[int, int, int]:
    palette = {"room": (96, 165, 250), "object": (251, 191, 36),
               "navigation": (52, 211, 153), "robot": (248, 113, 113)}
    return palette.get(kind.lower(), (192, 132, 252))


class LiveDashboardWidget(QtWidgets.QWidget):
    def __init__(self, live_root: Path):
        super().__init__()
        self.live_root = live_root
        self._state_mtime_ns = -1
        self._graph_mtime_ns = -1
        self._rgb_mtime_ns = -1
        self._depth_mtime_ns = -1
        self._map_mtime_ns = -1
        self._rgb_sha256 = None
        self._depth_sha256 = None
        self._map_sha256 = None
        self._state: dict = {}
        self._graph: dict = {}
        self._rgb = self._depth = self._map = None

        self.rgb_panel = _RGBPanel()
        self.map_panel = _MapPanel()
        self.scene_panel = _SceneGraphPanel()
        left = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        left.addWidget(self.rgb_panel)
        left.addWidget(self.map_panel)
        left.setStretchFactor(0, 1)
        left.setStretchFactor(1, 1)
        body = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        body.addWidget(left)
        body.addWidget(self.scene_panel)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 2)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(body)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)

    def set_live_root(self, root: Path):
        self.live_root = root
        self._state_mtime_ns = self._graph_mtime_ns = -1
        self._rgb_mtime_ns = self._depth_mtime_ns = self._map_mtime_ns = -1
        self._rgb_sha256 = self._depth_sha256 = self._map_sha256 = None
        self._state = self._graph = {}
        self._rgb = self._depth = self._map = None
        self.refresh()

    def refresh(self):
        state_update = self._read_json("state.json", self._state_mtime_ns)
        state, state_mtime_ns = (self._state, self._state_mtime_ns)
        if state_update is not None:
            state, state_mtime_ns = state_update
        graph_name = state.get("graph_file", "graph.json")
        graph = self._read_json(str(graph_name), self._graph_mtime_ns)
        camera = state.get("camera", {})
        nav = state.get("navigation", {})
        self._live_snapshot_mismatch = False
        rgb, rgb_mtime_ns, rgb_sha256 = self._read_array(
            camera.get("rgb_file", "rgb.npy"), self._rgb_mtime_ns, self._rgb,
            camera.get("rgb_sha256"), self._rgb_sha256)
        depth, depth_mtime_ns, depth_sha256 = self._read_array(
            camera.get("depth_file", "depth_m.npy"), self._depth_mtime_ns, self._depth,
            camera.get("depth_sha256"), self._depth_sha256)
        states, map_mtime_ns, map_sha256 = self._read_array(
            nav.get("states_file", "nav_states.npy"), self._map_mtime_ns, self._map,
            nav.get("states_sha256"), self._map_sha256)
        if self._live_snapshot_mismatch:
            # state.json may have been read while the publisher was replacing
            # the next generation's arrays. Keep every displayed component on
            # the previous generation and retry this pointer next tick.
            self._state_mtime_ns = -1
            return
        self._state, self._state_mtime_ns = state, state_mtime_ns
        self._rgb, self._rgb_mtime_ns, self._rgb_sha256 = rgb, rgb_mtime_ns, rgb_sha256
        self._depth, self._depth_mtime_ns, self._depth_sha256 = depth, depth_mtime_ns, depth_sha256
        self._map, self._map_mtime_ns, self._map_sha256 = states, map_mtime_ns, map_sha256
        if graph is not None:
            self._graph, self._graph_mtime_ns = graph
        camera = self._state.get("camera", {})
        nav = self._state.get("navigation", {})
        frame_id = camera.get("frame_id", "waiting")
        idx = camera.get("frame_index", "—")
        fps = self._state.get("capture_fps", "—")
        self.rgb_panel.update_frame(self._rgb, f"{frame_id} · index {idx} · {fps} FPS")
        self.map_panel.set_map(self._map, nav, int(idx) if str(idx).isdigit() else -1)
        if self._rgb is not None and self._depth is not None and camera:
            self.scene_panel.set_frame(str(frame_id), self._rgb, self._depth,
                                       camera.get("intrinsics", {}),
                                       np.asarray(camera.get("T_world_camera", np.eye(4))), self._graph)
        else:
            self.scene_panel.graph = self._graph
            self.scene_panel.update()
        self.update()

    def _read_json(self, name: str, old_mtime: int):
        path = self.live_root / name
        try:
            stat = path.stat()
            if stat.st_mtime_ns == old_mtime:
                return None
            return json.loads(path.read_text(encoding="utf-8")), stat.st_mtime_ns
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _read_array(self, name: str, old_mtime: int, previous,
                    expected_sha256: str | None = None,
                    previous_sha256: str | None = None):
        path = self.live_root / name
        try:
            stat = path.stat()
            if stat.st_mtime_ns == old_mtime:
                if expected_sha256 is not None and expected_sha256 != previous_sha256:
                    self._live_snapshot_mismatch = True
                    return previous, old_mtime, previous_sha256
                return previous, old_mtime, previous_sha256
            payload = path.read_bytes()
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                self._live_snapshot_mismatch = True
                return previous, old_mtime, previous_sha256
            import io
            return np.load(io.BytesIO(payload), allow_pickle=False), stat.st_mtime_ns, actual_sha256
        except (EOFError, FileNotFoundError, ValueError, OSError):
            if expected_sha256 is not None:
                self._live_snapshot_mismatch = True
            return previous, old_mtime, previous_sha256


class DesktopWindow(QtWidgets.QMainWindow):
    def __init__(self, *, live_root: Path, runner_python: Path | None,
                 scene_config: Path, profile: Path, output_root: Path,
                 duration_s: float, enable_p2: bool = True,
                 graph_plugin: str = "exploration.demo_graph_plugin:create_plugin"):
        super().__init__()
        self.setWindowTitle("3DSG · ObjectNav exploration")
        self.resize(1500, 920)
        self.runner_python = runner_python
        self.scene_config = scene_config
        self.profile = profile
        self.output_root = output_root
        self.duration_s = float(duration_s)
        self.enable_p2 = bool(enable_p2)
        self.graph_plugin = str(graph_plugin)
        self.run_id: str | None = None
        self.control_file: Path | None = None
        self.paused = False
        self.process = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_log)
        self.process.finished.connect(self._finished)

        toolbar = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start exploration")
        self.pause_button = QtWidgets.QPushButton("Pause")
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.status = QtWidgets.QLabel("Idle · module: frontier exploration · graph backend: waiting")
        self.graph_checkbox = QtWidgets.QCheckBox("Build live RGB-D object graph")
        self.graph_checkbox.setChecked(self.enable_p2)
        self.graph_checkbox.setToolTip(
            "Runs the local P2 ONNX detector and provisional RGB-D graph backend. "
            "Turn off for navigation-only runs."
        )
        self.graph_checkbox.toggled.connect(self._graph_option_changed)
        self.start_button.clicked.connect(self.start_run)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button.clicked.connect(self.stop_run)
        toolbar.addWidget(self.start_button)
        toolbar.addWidget(self.pause_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addWidget(self.graph_checkbox)
        toolbar.addSpacing(12)
        toolbar.addWidget(self.status, 1)

        self.viewer = LiveDashboardWidget(live_root)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1200)
        self.log.setMaximumHeight(120)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.addLayout(toolbar)
        layout.addWidget(self.viewer, 1)
        layout.addWidget(self.log)
        self.setCentralWidget(central)

    def start_run(self):
        if self.process.state() != QtCore.QProcess.NotRunning:
            return
        if self.runner_python is None or not self.runner_python.is_file():
            QtWidgets.QMessageBox.critical(self, "Python environment missing",
                f"Habitat Python was not found:\n{self.runner_python}\n\n"
                "Select the Python environment that has habitat-sim 0.3.3 installed.")
            return
        self.run_id = f"desktop-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.paused = False
        self.control_file = Path(tempfile.gettempdir()) / f"3dsg-control-{self.run_id}.json"
        self._write_control("run")
        run_live = self.output_root / self.run_id / "live"
        self.viewer.set_live_root(run_live)
        args = [str(ROOT / "scripts/run_frontier_habitat.py"),
                "--scene-config", str(self.scene_config), "--profile", str(self.profile),
                "--output-root", str(self.output_root), "--run-id", self.run_id,
                "--budget-s", str(self.duration_s),
                "--control-file", str(self.control_file)]
        if self.enable_p2:
            args.extend(["--enable-p2", "--enable-graph",
                         "--graph-plugin", self.graph_plugin])
        self.log.appendPlainText(f"$ {self.runner_python} {' '.join(args)}")
        self.process.start(str(self.runner_python), args)
        if not self.process.waitForStarted(5000):
            self.status.setText("Failed to start Habitat runner")
            self.start_button.setEnabled(True)
            return
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        backend = "P2 RGB-D graph starting" if self.enable_p2 else "navigation only"
        self.status.setText(f"Starting · run {self.run_id} · {backend}")

    def _graph_option_changed(self, enabled: bool):
        self.enable_p2 = bool(enabled)
        if self.process.state() == QtCore.QProcess.NotRunning:
            self.status.setText("Idle · module: frontier exploration · graph backend: "
                                + ("P2 RGB-D enabled" if enabled else "navigation only"))

    def stop_run(self):
        if self.process.state() == QtCore.QProcess.NotRunning:
            return
        self.status.setText("Stopping safely · sending interrupt to owned Habitat runner")
        try:
            self._write_control("stop")
            return
        except OSError:
            pass
        try:
            os.kill(int(self.process.processId()), signal.SIGINT)
        except (OSError, ValueError):
            self.process.terminate()

    def toggle_pause(self):
        if self.process.state() == QtCore.QProcess.NotRunning:
            return
        self.paused = not self.paused
        self._write_control("pause" if self.paused else "run")
        self.pause_button.setText("Resume" if self.paused else "Pause")
        self.status.setText("Paused · capture/map continue while movement is stopped"
                            if self.paused else "Resuming exploration")

    def _write_control(self, command: str):
        if self.control_file is None:
            return
        fd, temporary = tempfile.mkstemp(prefix=".3dsg-control-",
                                         dir=self.control_file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"command": command, "run_id": self.run_id}, stream,
                          sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
            os.replace(temporary, self.control_file)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _read_log(self):
        raw = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            self.log.appendPlainText(line)
            if line.startswith("PROGRESS"):
                self.status.setText(line)

    def _finished(self, exit_code, exit_status):
        self._read_log()
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pause")
        self.stop_button.setEnabled(False)
        self.paused = False
        if self.control_file is not None:
            try:
                self.control_file.unlink()
            except FileNotFoundError:
                pass
        self.status.setText(f"Runner stopped · exit {exit_code} · run {self.run_id}")

    def closeEvent(self, event):
        if self.process.state() != QtCore.QProcess.NotRunning:
            self.stop_run()
            if not self.process.waitForFinished(30000):
                self.process.kill()
                self.process.waitForFinished(3000)
        event.accept()


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-dir", type=Path, default=None,
                        help="view an existing run's live folder without launching Habitat")
    parser.add_argument("--runner-python", type=Path, default=DEFAULT_RUNNER_PYTHON)
    parser.add_argument("--scene-config", type=Path, default=ROOT / "configs/exploration/hm3d_00802_cpu.json")
    parser.add_argument("--profile", type=Path, default=ROOT / "configs/exploration/cpu_smooth.yaml")
    parser.add_argument("--output-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--duration-s", type=float, default=600.0)
    parser.add_argument("--autostart", action="store_true",
                        help="start exploration as soon as the desktop window opens")
    parser.add_argument("--navigation-only", action="store_true",
                        help="disable local P2 inference and run frontier navigation only")
    parser.add_argument("--graph-plugin", default="exploration.demo_graph_plugin:create_plugin",
                        help="loadable graph provider in module:factory form")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    if args.live_dir is not None:
        window = QtWidgets.QMainWindow()
        window.setWindowTitle("3DSG · live/replay viewer")
        window.resize(1400, 900)
        window.setCentralWidget(LiveDashboardWidget(args.live_dir.resolve()))
    else:
        window = DesktopWindow(
            live_root=Path("/tmp/3dsg-no-live-run"), runner_python=args.runner_python,
            scene_config=args.scene_config.resolve(), profile=args.profile.resolve(),
            output_root=args.output_root.resolve(), duration_s=args.duration_s,
            enable_p2=not args.navigation_only, graph_plugin=args.graph_plugin,
        )
    window.show()
    if args.autostart and args.live_dir is None:
        QtCore.QTimer.singleShot(0, window.start_run)
    return app.exec_()
