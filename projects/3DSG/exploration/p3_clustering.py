"""Project-owned acceleration for the reference P3 Euclidean components.

The neighborhood radius and connected-component semantics stay the same as
the reference adapter. This module only changes how the neighbor graph and
its components are materialized; the reference repository remains untouched.
"""

from __future__ import annotations

import importlib
import threading
from typing import Any, Mapping

import numpy as np


_PATCH_LOCK = threading.Lock()
CLUSTER_BACKEND = "spatial_hash_exact_union_find/v1"


def euclidean_components(points: np.ndarray, eps: float) -> list[np.ndarray]:
    """Return radius-connected components in the reference's deterministic order.

    Edges use the same inclusive Euclidean radius as the reference
    ``query_ball_point`` implementation. Spatial buckets restrict exact
    distance checks to local candidates, and union-find avoids materializing
    Python neighbor lists or a quadratic fallback matrix.
    """
    xyz = np.asarray(points, dtype=np.float64)
    radius = float(eps)
    if xyz.ndim != 2 or xyz.shape[1] != 3:
        raise ValueError("P3 points must have shape Nx3")
    if not np.isfinite(xyz).all():
        raise ValueError("P3 points must be finite")
    if not np.isfinite(radius) or radius <= 0:
        raise ValueError("P3 cluster radius must be finite and positive")
    count = len(xyz)
    if count == 0:
        return []

    cell_size = radius + 4.0 * float(np.spacing(radius))
    cell_coords = np.floor(xyz / cell_size).astype(np.int64)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    parent = np.arange(count, dtype=np.int64)
    sizes = np.ones(count, dtype=np.int64)

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = int(parent[root])
        while parent[index] != index:
            next_index = int(parent[index])
            parent[index] = root
            index = next_index
        return root

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if sizes[left_root] < sizes[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        sizes[left_root] += sizes[right_root]

    for index, coordinate in enumerate(cell_coords):
        cx, cy, cz = (int(value) for value in coordinate)
        candidates: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    candidates.extend(buckets.get((cx + dx, cy + dy, cz + dz), ()))
        if candidates:
            candidate_ids = np.asarray(candidates, dtype=np.int64)
            delta = xyz[candidate_ids] - xyz[index]
            distances = np.linalg.norm(delta, axis=1)
            hits = candidate_ids[distances <= radius]
            if len(hits):
                # All hits connect through this point; merge each existing
                # component once instead of iterating all neighbor edges.
                roots = {find(int(hit)) for hit in hits}
                for root in roots:
                    union(index, root)
        buckets.setdefault((cx, cy, cz), []).append(index)

    grouped: dict[int, list[int]] = {}
    for index in range(count):
        grouped.setdefault(find(index), []).append(index)
    groups = [np.asarray(indices, dtype=np.int64) for indices in grouped.values()]
    groups.sort(key=lambda group: (-len(group), int(group[0])))
    return groups


def project_current_frame_optimized(packet: Mapping[str, Any],
                                    profile: Mapping[str, Any]) -> dict[str, Any]:
    """Use the reference projector with this module's sparse graph routine."""
    session_module = importlib.import_module("integration_v2.perception_feature_sessions")
    source_module = importlib.import_module("scripts.project_detection_masks_to_3d")
    reference_projector = session_module._p3_project_current_frame

    # The legacy projector resolves ``cluster_components`` inside each frame.
    # Its API has no strategy parameter, so override that function only during
    # the serialized P3 call and restore the module immediately afterwards.
    with _PATCH_LOCK:
        original = source_module.cluster_components
        source_module.cluster_components = euclidean_components
        try:
            result = reference_projector(packet, profile)
        finally:
            source_module.cluster_components = original

    result.setdefault("provenance", {})["cluster_backend"] = CLUSTER_BACKEND
    result["provenance"]["cluster_equivalence_status"] = "validated_on_saved_native_frames"
    return result
