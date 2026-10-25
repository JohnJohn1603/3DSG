"""Small provisional RGB-D scene graph backend for the live demo.

This backend lifts P2 masks through metric depth and tracks same-class object
hypotheses across keyframes. Its interface matches the injected stateful
session boundary used by the larger graph adapters, but it does not claim
ConceptGraphs identity parity or verified room membership.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import numpy as np


class RGBDObjectGraphSession:
    def __init__(self, *, association_radius_m: float = 0.60,
                 min_mask_points: int = 30) -> None:
        if association_radius_m <= 0 or min_mask_points < 1:
            raise ValueError("invalid RGB-D object graph configuration")
        self.association_radius_m = float(association_radius_m)
        self.min_mask_points = int(min_mask_points)
        self._tracks: dict[str, dict[str, Any]] = {}
        self._last_index = -1
        self._seen: set[str] = set()
        self._scene_id: str | None = None
        self._closed = False
        self._views: dict[str, dict[str, Any]] = {}
        self._observation_edges: dict[tuple[str, str], dict[str, Any]] = {}

    def update_frame(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        frame_id = str(packet["frame_id"])
        frame_index = int(packet["frame_index"])
        scene_id = str(packet["scene_id"])
        if self._closed:
            raise RuntimeError("object graph session is closed")
        if frame_id in self._seen or frame_index <= self._last_index:
            raise RuntimeError("duplicate/stale object graph keyframe; replay_required")
        if self._scene_id is not None and scene_id != self._scene_id:
            raise RuntimeError("scene changed inside object graph session")
        depth = np.asarray(packet["depth_m"], dtype=np.float32)
        intr = packet["intrinsics"]
        pose = np.asarray(packet["T_world_camera"], dtype=np.float64)
        detections = packet.get("detections", ())
        used_tracks: set[str] = set()
        for row in detections:
            center, extents, point_count = _lift_detection(row, depth, intr, pose,
                                                            float(packet["depth_min_m"]),
                                                            float(packet["depth_max_m"]),
                                                            self.min_mask_points)
            if center is None:
                continue
            class_name = str(row["class_canonical"])
            best_id, best_distance = None, self.association_radius_m
            for track_id, track in self._tracks.items():
                if track_id in used_tracks or track["class"] != class_name:
                    continue
                distance = float(np.linalg.norm(center - track["position"]))
                if distance < best_distance:
                    best_id, best_distance = track_id, distance
            if best_id is None:
                seed = f"{scene_id}|{frame_id}|{row['detection_id']}".encode()
                best_id = f"object-{hashlib.sha1(seed).hexdigest()[:12]}"
                self._tracks[best_id] = {
                    "class": class_name, "position": center,
                    "extents": extents, "observations": 0,
                    "first_seen_frame_id": frame_id,
                    "first_seen_frame_index": frame_index,
                }
            track = self._tracks[best_id]
            n = int(track["observations"])
            track["position"] = (track["position"] * n + center) / (n + 1)
            track["extents"] = np.maximum(track["extents"], extents)
            track["observations"] = n + 1
            track["last_seen_frame_id"] = frame_id
            track["last_seen_frame_index"] = frame_index
            track["last_detection_id"] = str(row["detection_id"])
            track["confidence"] = float(row["score"])
            track["point_count"] = int(point_count)
            used_tracks.add(best_id)

        self._seen.add(frame_id)
        self._last_index = frame_index
        self._scene_id = scene_id
        camera_position = pose[:3, 3]
        view_id = f"view-{frame_index:08d}"
        nodes = [{
            "id": track_id, "kind": "object", "position": track["position"].tolist(),
            "label": f"{track['class']} · provisional · {track['observations']} views",
            "status": "provisional", "class": track["class"],
            "extents_m": track["extents"].tolist(),
            "observations": track["observations"],
            "confidence": track.get("confidence"),
            "last_seen_frame_id": track.get("last_seen_frame_id"),
        } for track_id, track in sorted(self._tracks.items())]
        view = {"id": view_id, "kind": "viewpoint",
                "position": camera_position.tolist(), "label": f"view {frame_index}"}
        self._views[view_id] = view
        nodes.extend(self._views[key] for key in sorted(self._views))
        for track_id, track in sorted(self._tracks.items()):
            if track.get("last_seen_frame_id") == frame_id:
                edge_key = (track_id, view_id)
                self._observation_edges[edge_key] = {
                    "source": track_id, "target": view_id,
                    "relation": "observed_from", "evidence_frame_id": frame_id,
                }
        edges = [self._observation_edges[key]
                 for key in sorted(self._observation_edges)]
        return {"nodes": nodes, "edges": edges,
                "diagnostics": {"detections": len(detections),
                                "active_tracks": len(self._tracks),
                                "mapped_observations": len(used_tracks)}}

    def close(self) -> None:
        self._closed = True


class FrontierNavGraphSession:
    """Represent robot pose and observed frontier targets in graph coordinates."""

    def __init__(self) -> None:
        self._last_index = -1
        self._seen: set[str] = set()
        self._closed = False

    def update_frame(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        frame_id = str(packet["frame_id"])
        frame_index = int(packet["frame_index"])
        if self._closed:
            raise RuntimeError("navigation graph session is closed")
        if frame_id in self._seen or frame_index <= self._last_index:
            raise RuntimeError("duplicate/stale nav graph keyframe; replay_required")
        self._seen.add(frame_id)
        self._last_index = frame_index
        metadata = packet.get("keyframe_metadata") or {}
        pose = metadata.get("robot_pose", (float(packet["T_world_camera"][0][3]),
                                             float(packet["T_world_camera"][1][3]), 0.0))
        floor_z = float(packet.get("floor_z_m", 0.0))
        nodes = [{"id": "robot", "kind": "robot",
                  "position": [float(pose[0]), float(pose[1]), floor_z],
                  "label": "Robot (graph as-of)"}]
        edges = []
        current_frontier_ids = set()
        for frontier in metadata.get("frontiers", ()):
            frontier_id = str(frontier["id"])
            current_frontier_ids.add(frontier_id)
            node_id = f"frontier-{frontier_id}"
            xy = frontier["xy"]
            nodes.append({"id": node_id, "kind": "navigation",
                          "position": [float(xy[0]), float(xy[1]), floor_z],
                          "label": f"Frontier · gain {int(frontier.get('gain_cells', 0))}"})
            edges.append({"source": "robot", "target": node_id,
                          "relation": "exploration_candidate"})
        goal = metadata.get("goal_xy")
        if goal is not None:
            node_id = f"selected-goal-{frame_index:08d}"
            nodes.append({"id": node_id, "kind": "navigation",
                          "position": [float(goal[0]), float(goal[1]), floor_z],
                          "label": "Selected frontier goal"})
            edges.append({"source": "robot", "target": node_id,
                          "relation": "navigates_toward"})
        return {"nodes": nodes, "edges": edges,
                "diagnostics": {"frontier_count": len(current_frontier_ids)}}

    def close(self) -> None:
        self._closed = True


def _lift_detection(row: Mapping[str, Any], depth: np.ndarray,
                    intrinsics: Mapping[str, Any], T_world_camera: np.ndarray,
                    depth_min_m: float, depth_max_m: float,
                    min_points: int) -> tuple[np.ndarray | None, np.ndarray | None, int]:
    mask_path = Path(str(row["mask_file"]))
    digest = hashlib.sha256(mask_path.read_bytes()).hexdigest()
    if digest != str(row["mask_sha256"]):
        raise ValueError(f"P2 mask hash mismatch: {mask_path}")
    mask = np.load(mask_path, allow_pickle=False)
    if mask.shape != depth.shape or mask.dtype != np.bool_:
        raise ValueError("P2 mask and current depth dimensions differ")
    stride = 2
    valid_mask = mask[::stride, ::stride]
    valid_depth = depth[::stride, ::stride]
    vv, uu = np.nonzero(valid_mask)
    z = valid_depth[vv, uu].astype(np.float64)
    valid = np.isfinite(z) & (z >= depth_min_m) & (z <= depth_max_m)
    if int(valid.sum()) < min_points:
        return None, None, int(valid.sum())
    u = (uu[valid] * stride).astype(np.float64)
    v = (vv[valid] * stride).astype(np.float64)
    z = z[valid]
    xyz_camera = np.column_stack((
        (u - float(intrinsics["cx"])) * z / float(intrinsics["fx"]),
        (v - float(intrinsics["cy"])) * z / float(intrinsics["fy"]), z,
    ))
    xyz_world = xyz_camera @ T_world_camera[:3, :3].T + T_world_camera[:3, 3]
    low, high = np.quantile(xyz_world, [0.10, 0.90], axis=0)
    center = np.median(xyz_world, axis=0)
    extents = np.maximum(0.01, high - low)
    if not np.isfinite(center).all() or not np.isfinite(extents).all():
        raise ValueError("lifted object geometry is non-finite")
    return center, extents, int(len(xyz_world))
