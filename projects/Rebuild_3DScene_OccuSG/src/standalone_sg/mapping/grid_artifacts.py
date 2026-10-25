"""Lossless, opt-in M3/M4 diagnostic artifacts.

The writer observes the grid returned by the existing mapping stage.  It does
not call projection, mutate an OctoMap/MapConverter, or add anything to the
SceneGraphSnapshot.  NumPy arrays are stored in NPZ without a lossy conversion;
JSON sidecars contain only finite scalar/provenance data and region snapshots.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from ..contracts.regions import CanonicalRegion, TrackedRegion
from ..pipeline.contracts import GraphEdge, GraphNode, SceneGraphSnapshot
from .projection import FreeSpaceGrid2D


SCHEMA_VERSION = 1


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    return value


def _region(region: CanonicalRegion | TrackedRegion) -> dict[str, Any]:
    return _plain(region)


def _node(node: GraphNode) -> dict[str, Any]:
    return _plain(node)


def _edge(edge: GraphEdge) -> dict[str, Any]:
    return _plain(edge)


class GridTraceWriter:
    """Write one immutable diagnostic record per accepted M3 update."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.frames_dir = self.root / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict[str, Any]] = []

    def write_frame(
        self,
        *,
        packet: Any,
        grid: FreeSpaceGrid2D,
        response: Mapping[str, Any],
        result: Any,
        snapshot: SceneGraphSnapshot,
    ) -> None:
        if not isinstance(grid, FreeSpaceGrid2D):
            raise TypeError("GridTraceWriter requires the exact FreeSpaceGrid2D returned by M3")
        frame_index = int(packet.frame_index)
        stem = f"{frame_index:06d}"
        npz_path = self.frames_dir / f"{stem}.npz"
        diagnostics = response.get("diagnostics", {})
        arrays: dict[str, np.ndarray] = {
            "values": np.asarray(grid.values, dtype=np.int8).copy(),
            "z_floor": np.asarray(grid.z_floor, dtype=np.float64).copy(),
            "z_ceiling": np.asarray(grid.z_ceiling, dtype=np.float64).copy(),
        }
        if diagnostics.get("preprocessed_mask") is not None:
            arrays["m4_preprocessed_mask"] = np.asarray(diagnostics["preprocessed_mask"], dtype=np.bool_).copy()
        if diagnostics.get("black_mask") is not None:
            arrays["m4_black_mask"] = np.asarray(diagnostics["black_mask"], dtype=np.bool_).copy()
        np.savez_compressed(npz_path, **arrays)
        digest = hashlib.sha256(npz_path.read_bytes()).hexdigest()

        rooms = [_node(node) for node in snapshot.nodes if node.kind == "room"]
        room_edges = [
            _edge(edge) for edge in snapshot.edges
            if edge.relation in {"room_contains", "room_adjacency"}
        ]
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "frame_index": frame_index,
            "frame_id": str(packet.frame_id),
            "simulation_timestamp_s": float(packet.timestamp_s),
            "source_frame_id": str(packet.frame_id),
            "frame_id_coordinate": str(grid.frame_id),
            "map_revision": int(grid.revision),
            "projection_mode": grid.projection_mode,
            "projection_kind": grid.kind,
            "projection_backend": grid.backend,
            "projection_source": grid.source,
            "origin_xy": [float(v) for v in grid.origin_xy],
            "resolution_m": float(grid.resolution_m),
            "shape": [int(v) for v in grid.shape],
            "array_file": npz_path.name,
            "array_sha256": digest,
            "arrays": sorted(arrays),
            "semantics": "-1=unknown, 0=free, 1..100=source occupation score; positive values are not probabilities",
            "changed_bounds_xy": None if grid.changed_bounds_xy is None else list(grid.changed_bounds_xy),
            "changed_bounds_xyz": None if grid.changed_bounds_xyz is None else list(grid.changed_bounds_xyz),
            "m4": {
                "status": response.get("status"),
                "branch": response.get("branch"),
                "rejection_reason": response.get("rejection_reason"),
                "stale_or_reused": bool(diagnostics.get("stale_or_reused", False)),
                "kernel_invoked": bool(diagnostics.get("kernel_invoked", False)),
                "input_revision": response.get("input_revision"),
                "preprocessed_free_pixels": diagnostics.get("preprocessed_free_pixels"),
                "preprocessed_unknown_pixels": diagnostics.get("preprocessed_unknown_pixels"),
                "preprocessing_policy": diagnostics.get("preprocessing_policy", "source"),
                "black_pixels": diagnostics.get("black_pixels"),
                "crop": _plain(diagnostics.get("crop")),
                "pixel_tau": diagnostics.get("pixel_tau"),
                "holes_contract": diagnostics.get("holes_contract"),
                "raw_regions": _plain(response.get("regions", [])),
                "contours_contract": "native worker exposes accepted outer contours as regions; no hidden contour list is inferred",
            },
            "m5": {
                "accepted": bool(result.accepted),
                "stale": bool(result.stale),
                "reason": result.reason,
                "request_revision": result.request_revision,
                "accepted_observation_revision": result.accepted_observation_revision,
                "current_regions": [_region(region) for region in result.current_regions],
                "retained_tracks": [_region(track) for track in result.retained_tracks],
            },
            "m8": {
                "rooms": rooms,
                "room_edges": room_edges,
                "snapshot_revision": int(snapshot.revision),
                "snapshot_timestamp_s": snapshot.timestamp_s,
                "regions_stale": bool(snapshot.stale_flags.get("regions", False)),
            },
            "base_pose_W": None if packet.base_pose_W is None else [float(v) for v in packet.base_pose_W],
        }
        # RGB-D replays carry the exact sensor pose/calibration needed by
        # diagnostic overlays.  Keep the writer usable for non-RGB-D unit
        # fixtures that intentionally provide only a packet shell.
        if getattr(packet, "rgbd", None) is not None:
            metadata["sensor_pose_W_Copt"] = packet.rgbd.T_W_Copt.matrix.tolist()
            metadata["intrinsics"] = {
                "width": int(packet.rgbd.intrinsics.width),
                "height": int(packet.rgbd.intrinsics.height),
                "fx": float(packet.rgbd.intrinsics.fx),
                "fy": float(packet.rgbd.intrinsics.fy),
                "cx": float(packet.rgbd.intrinsics.cx),
                "cy": float(packet.rgbd.intrinsics.cy),
                "calibration_id": packet.rgbd.intrinsics.calibration_id,
            }
        json_path = self.frames_dir / f"{stem}.json"
        json_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        self._entries.append(metadata)

    def close(self, *, capture_dir: Path | None = None, config_path: Path | None = None) -> Path:
        index = {
            "schema_version": SCHEMA_VERSION,
            "kind": "m3_m4_lossless_grid_trace",
            "array_format": "npz; arrays copied from the exact M3 FreeSpaceGrid2D and stored losslessly",
            "does_not_reproject": True,
            "does_not_feed_builder": True,
            "capture_dir": None if capture_dir is None else str(capture_dir),
            "config_path": None if config_path is None else str(config_path),
            "frame_count": len(self._entries),
            "frames": [{
                "frame_index": item["frame_index"],
                "frame_id": item["frame_id"],
                "simulation_timestamp_s": item["simulation_timestamp_s"],
                "map_revision": item["map_revision"],
                "projection_mode": item["projection_mode"],
                "array_file": f"frames/{item['array_file']}",
                "metadata_file": f"frames/{item['frame_index']:06d}.json",
            } for item in self._entries],
        }
        path = self.root / "index.json"
        path.write_text(json.dumps(index, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        return path
