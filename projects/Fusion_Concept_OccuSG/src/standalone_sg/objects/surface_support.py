"""Deterministic voxel-surface support helpers for offline U3 experiments."""
from __future__ import annotations

import numpy as np


def voxel_representatives(points_xyz: np.ndarray, voxel_size_m: float) -> np.ndarray:
    points = np.asarray(points_xyz, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
        raise ValueError("surface points must be finite Nx3")
    if not np.isfinite(voxel_size_m) or voxel_size_m <= 0:
        raise ValueError("voxel size must be finite and positive")
    if not len(points):
        return np.empty((0, 3), dtype=np.float32)
    keys = np.floor(points / voxel_size_m).astype(np.int64)
    sums: dict[tuple[int, int, int], np.ndarray] = {}
    counts: dict[tuple[int, int, int], int] = {}
    for key_row, point in zip(keys, points):
        key = tuple(int(v) for v in key_row)
        sums.setdefault(key, np.zeros(3, dtype=np.float64))
        sums[key] += point
        counts[key] = counts.get(key, 0) + 1
    return np.asarray([sums[key] / counts[key] for key in sorted(counts)], dtype=np.float32)


def _supported(points: np.ndarray, references: np.ndarray, radius_m: float) -> np.ndarray:
    """Mark each point with at least one reference within radius, using a spatial hash."""
    if not len(points) or not len(references):
        return np.zeros(len(points), dtype=bool)
    cell_size = float(radius_m)
    buckets: dict[tuple[int, int, int], list[np.ndarray]] = {}
    for point in references:
        key = tuple(int(v) for v in np.floor(point / cell_size).astype(np.int64))
        buckets.setdefault(key, []).append(point)
    result = np.zeros(len(points), dtype=bool)
    radius_sq = radius_m * radius_m
    for index, point in enumerate(points):
        center = np.floor(point / cell_size).astype(np.int64)
        found = False
        for dx in (-1, 0, 1):
            if found:
                break
            for dy in (-1, 0, 1):
                if found:
                    break
                for dz in (-1, 0, 1):
                    key = (int(center[0] + dx), int(center[1] + dy), int(center[2] + dz))
                    for candidate in buckets.get(key, ()):
                        delta = point - candidate
                        if float(delta @ delta) <= radius_sq:
                            found = True
                            break
                    if found:
                        break
        result[index] = found
    return result


def surface_support(observation_points: np.ndarray, track_points: np.ndarray, *,
                    voxel_size_m: float, radius_m: float) -> dict:
    """Return asymmetric coverage diagnostics; neither ratio alone is identity proof."""
    obs = voxel_representatives(observation_points, voxel_size_m)
    track = np.asarray(track_points, dtype=np.float32)
    if track.ndim != 2 or track.shape[1] != 3 or not np.isfinite(track).all():
        raise ValueError("track surface representatives must be finite Nx3")
    obs_to_track = _supported(obs, track, radius_m)
    track_to_obs = _supported(track, obs, radius_m)
    forward_count = int(obs_to_track.sum())
    reverse_count = int(track_to_obs.sum())
    return {
        "observation_surface_voxels": int(len(obs)),
        "track_surface_voxels": int(len(track)),
        "forward_supported_voxels": forward_count,
        "reverse_supported_voxels": reverse_count,
        "forward_support_ratio": forward_count / len(obs) if len(obs) else 0.0,
        "reverse_support_ratio": reverse_count / len(track) if len(track) else 0.0,
    }
