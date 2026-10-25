"""Small deterministic persistent-geometry accumulator for fixed assignments.

This is an offline adaptation inspired by ConceptGraphs point-cloud
accumulation. It does not perform object association, denoising, merge, or
semantic/appearance fusion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from typing import Any

import numpy as np


class EvidenceConflictError(ValueError):
    """An observation ID was reused for a different evidence payload."""


@dataclass
class _ObjectState:
    object_id: str
    voxel_sum_xyz: dict[tuple[int, int, int], np.ndarray] = field(default_factory=dict)
    voxel_sum_rgb: dict[tuple[int, int, int], np.ndarray] = field(default_factory=dict)
    voxel_counts: dict[tuple[int, int, int], int] = field(default_factory=dict)
    applied_ids: dict[str, str] = field(default_factory=dict)
    view_keys: set[str] = field(default_factory=set)
    first_timestamp_s: float | None = None
    last_timestamp_s: float | None = None
    raw_point_count: int = 0


class ObjectGeometryFusion:
    """Causal voxel statistics keyed by fixed object assignments."""

    def __init__(self, *, voxel_size_m: float = 0.02, max_voxels_per_object: int = 2_000_000) -> None:
        if not math.isfinite(voxel_size_m) or voxel_size_m <= 0:
            raise ValueError("voxel_size_m must be finite and positive")
        if type(max_voxels_per_object) is not int or max_voxels_per_object < 1:
            raise ValueError("max_voxels_per_object must be a positive integer")
        self.voxel_size_m = float(voxel_size_m)
        self.max_voxels_per_object = max_voxels_per_object
        self.objects: dict[str, _ObjectState] = {}
        self._observation_owner: dict[str, tuple[str, str, float]] = {}

    @staticmethod
    def payload_hash(xyz_W: np.ndarray, rgb_u8: np.ndarray, pixel_rc: np.ndarray,
                     T_W_Copt: np.ndarray) -> str:
        digest = hashlib.sha256()
        for array in (np.asarray(xyz_W, dtype="<f4"), np.asarray(rgb_u8, dtype=np.uint8),
                      np.asarray(pixel_rc, dtype="<i4"), np.asarray(T_W_Copt, dtype="<f8")):
            contiguous = np.ascontiguousarray(array)
            digest.update(str(contiguous.shape).encode("ascii"))
            digest.update(contiguous.tobytes())
        return digest.hexdigest()

    def apply(self, *, object_id: str, observation_id: str, timestamp_s: float,
              xyz_W: np.ndarray, rgb_u8: np.ndarray, pixel_rc: np.ndarray,
              T_W_Copt: np.ndarray, payload_sha256: str | None = None) -> dict[str, Any]:
        if not object_id or not observation_id:
            raise ValueError("object_id and observation_id must be non-empty")
        xyz = np.asarray(xyz_W, dtype=np.float64)
        rgb = np.asarray(rgb_u8, dtype=np.uint8)
        pixels = np.asarray(pixel_rc, dtype=np.int32)
        pose = np.asarray(T_W_Copt, dtype=np.float64)
        if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
            raise ValueError("xyz_W must be a finite Nx3 array")
        if rgb.shape != xyz.shape:
            raise ValueError("rgb_u8 must have the same Nx3 shape as xyz_W")
        if pixels.shape != (len(xyz), 2):
            raise ValueError("pixel_rc must have shape Nx2 and align with xyz_W")
        if pose.shape != (4, 4) or not np.isfinite(pose).all():
            raise ValueError("T_W_Copt must be a finite 4x4 matrix")
        if not math.isfinite(float(timestamp_s)):
            raise ValueError("timestamp_s must be finite")
        computed_hash = self.payload_hash(xyz, rgb, pixels, pose)
        if payload_sha256 is not None and payload_sha256 != computed_hash:
            raise EvidenceConflictError("provided payload hash does not match evidence arrays")
        payload_sha256 = computed_hash
        prior_owner = self._observation_owner.get(observation_id)
        if prior_owner is not None:
            owner_id, owner_hash, owner_timestamp = prior_owner
            if owner_id != object_id:
                raise EvidenceConflictError(
                    f"observation {observation_id} is already owned by object {owner_id}, cannot apply to {object_id}")
            if owner_hash != payload_sha256 or owner_timestamp != float(timestamp_s):
                raise EvidenceConflictError(f"observation {observation_id} was replayed with a different payload hash")
            return {"status": "idempotent_noop", "object_id": object_id,
                    "observation_id": observation_id, "payload_sha256": payload_sha256}
        state = self.objects.get(object_id)
        if state is not None and state.last_timestamp_s is not None and float(timestamp_s) < state.last_timestamp_s:
            raise ValueError(
                f"new observation {observation_id} is older than object state: "
                f"{timestamp_s} < {state.last_timestamp_s}")
        if state is None:
            state = _ObjectState(object_id)
            self.objects[object_id] = state

        keys = np.floor(xyz / self.voxel_size_m).astype(np.int64)
        unique_keys = {tuple(int(c) for c in row) for row in keys}
        new_count = sum(key not in state.voxel_counts for key in unique_keys)
        if len(state.voxel_counts) + new_count > self.max_voxels_per_object:
            raise MemoryError(f"object {object_id} would exceed voxel cap; evidence was not silently dropped")
        for key, point, color in zip(keys, xyz, rgb):
            voxel = tuple(int(c) for c in key)
            if voxel not in state.voxel_counts:
                state.voxel_sum_xyz[voxel] = np.zeros(3, dtype=np.float64)
                state.voxel_sum_rgb[voxel] = np.zeros(3, dtype=np.float64)
                state.voxel_counts[voxel] = 0
            state.voxel_sum_xyz[voxel] += point
            state.voxel_sum_rgb[voxel] += color.astype(np.float64)
            state.voxel_counts[voxel] += 1
        state.applied_ids[observation_id] = payload_sha256
        self._observation_owner[observation_id] = (object_id, payload_sha256, float(timestamp_s))
        state.raw_point_count += len(xyz)
        state.first_timestamp_s = float(timestamp_s) if state.first_timestamp_s is None else min(state.first_timestamp_s, float(timestamp_s))
        state.last_timestamp_s = float(timestamp_s) if state.last_timestamp_s is None else max(state.last_timestamp_s, float(timestamp_s))
        view_key = hashlib.sha256(np.round(pose, 6).astype("<f8").tobytes()).hexdigest()
        is_new_pose = view_key not in state.view_keys
        state.view_keys.add(view_key)
        return {"status": "applied", "object_id": object_id,
                "observation_id": observation_id, "payload_sha256": payload_sha256,
                "input_point_count": len(xyz), "new_voxel_count": new_count,
                "fused_voxel_count": len(state.voxel_counts),
                "unique_sensor_pose_added": is_new_pose,
                "unique_sensor_pose_count": len(state.view_keys)}

    def snapshot(self, object_id: str) -> dict[str, Any]:
        state = self.objects[object_id]
        keys = sorted(state.voxel_counts)
        counts = np.asarray([state.voxel_counts[key] for key in keys], dtype=np.int64)
        xyz = np.asarray([state.voxel_sum_xyz[key] / state.voxel_counts[key] for key in keys], dtype=np.float32)
        rgb = np.clip(np.asarray([state.voxel_sum_rgb[key] / state.voxel_counts[key] for key in keys]), 0, 255).astype(np.uint8)
        bounds_min = xyz.min(axis=0) if len(xyz) else np.zeros(3, dtype=np.float32)
        bounds_max = xyz.max(axis=0) if len(xyz) else np.zeros(3, dtype=np.float32)
        center = (bounds_min + bounds_max) / 2.0
        return {"object_id": object_id, "voxel_keys": np.asarray(keys, dtype=np.int64).reshape((-1, 3)),
                "xyz_W": xyz, "rgb_u8": rgb, "point_support": counts,
                "bounds_min_W": bounds_min.astype(np.float32), "bounds_max_W": bounds_max.astype(np.float32),
                "center_W": center.astype(np.float32),
                "extents_m": (bounds_max - bounds_min).astype(np.float32),
                "raw_point_count": state.raw_point_count,
                "voxel_count": len(keys), "observation_count": len(state.applied_ids),
                "unique_sensor_pose_count": len(state.view_keys),
                "evidence_ids": sorted(state.applied_ids),
                "evidence_payload_hashes": dict(sorted(state.applied_ids.items())),
                "first_timestamp_s": state.first_timestamp_s, "last_timestamp_s": state.last_timestamp_s}

    def prior_matcher_geometry(self, object_id: str) -> dict[str, Any]:
        """Return the causal matcher AABB derived from current voxel representatives.

        The representatives are the per-voxel mean xyz values in ``snapshot``.
        The result contains only already-applied observations and is suitable
        for a prior-state association decision.
        """
        state = self.snapshot(object_id)
        return {
            "geometry_source": "prior_accumulated_surface_voxel_representatives",
            "center_W": state["center_W"].copy(),
            "bounds_min_W": state["bounds_min_W"].copy(),
            "bounds_max_W": state["bounds_max_W"].copy(),
            "extents_m": state["extents_m"].copy(),
            "prior_revision": int(state["observation_count"]),
            "evidence_ids": list(state["evidence_ids"]),
        }
