"""Pixel-registered surface visibility diagnostics; this module never assigns identity.

The scorer compares a prior world-space surface to one target camera's axial
RGB-D/mask. Outputs are evidence counts, not probabilities or match decisions.
"""
from __future__ import annotations

import numpy as np
from standalone_sg.contracts import CameraIntrinsics


def _validate_points(points: np.ndarray, label: str) -> np.ndarray:
    value = np.asarray(points, dtype=np.float64)
    if value.ndim != 2 or value.shape[1] != 3 or not np.isfinite(value).all():
        raise ValueError(f"{label} must be a finite Nx3 array")
    return value


def _validate_transform(T_W_C: np.ndarray) -> np.ndarray:
    value = np.asarray(T_W_C, dtype=np.float64)
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise ValueError("T_W_C must be a finite 4x4 matrix")
    if not np.allclose(value[3], [0, 0, 0, 1], atol=1e-8):
        raise ValueError("T_W_C must be homogeneous")
    return value


def enforce_available_by_decision(source_frame_index: int, target_view_frame_index: int,
                                  decision_time_frame_index: int) -> None:
    """Guard evidence availability; projection direction is not temporal order."""
    if max(int(source_frame_index), int(target_view_frame_index)) > int(decision_time_frame_index):
        raise ValueError("visibility evidence includes a frame later than decision time")


def project_world_points(points_W: np.ndarray, T_W_C: np.ndarray,
                         intrinsics: CameraIntrinsics) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project world points using the shared calibrated camera contract."""
    points = _validate_points(points_W, "points_W")
    pose = _validate_transform(T_W_C)
    if not isinstance(intrinsics, CameraIntrinsics):
        raise TypeError("intrinsics must be the shared CameraIntrinsics contract")
    cam = (np.linalg.inv(pose) @ np.column_stack([points, np.ones(len(points))]).T).T[:, :3]
    z = cam[:, 2]
    safe_z = np.where(z > 0, z, 1.0)
    u_float = intrinsics.fx * cam[:, 0] / safe_z + intrinsics.cx
    v_float = intrinsics.fy * cam[:, 1] / safe_z + intrinsics.cy
    return cam, u_float, v_float


def score_prior_surface_visibility(
    points_W: np.ndarray,
    T_W_C: np.ndarray,
    depth_axial_m: np.ndarray,
    valid_depth: np.ndarray,
    predicted_mask: np.ndarray,
    intrinsics: CameraIntrinsics,
    *,
    depth_tolerance_m: float,
) -> dict:
    """Project prior surface into a target view and classify visible pixel evidence.

    One nearest prior-surface sample is retained per raster pixel. A nearer
    observed surface is classified as occlusion; a farther observed surface is
    a depth contradiction for this projected sample. Mask disagreement is
    reported separately and never upgrades/downgrades identity.
    """
    points = _validate_points(points_W, "points_W")
    pose = _validate_transform(T_W_C)
    depth = np.asarray(depth_axial_m, dtype=np.float64)
    valid = np.asarray(valid_depth, dtype=bool)
    mask = np.asarray(predicted_mask, dtype=bool)
    if depth.ndim != 2 or valid.shape != depth.shape or mask.shape != depth.shape:
        raise ValueError("depth, valid_depth and predicted_mask must have one identical HxW shape")
    if (depth.shape != (intrinsics.height, intrinsics.width)
            or not np.isfinite(depth_tolerance_m) or depth_tolerance_m < 0):
        raise ValueError("frame shape must match CameraIntrinsics and tolerance must be finite/nonnegative")
    h, w = depth.shape
    cam, u_float, v_float = project_world_points(points, pose, intrinsics)
    z = cam[:, 2]
    in_front = z > 0
    u = np.rint(u_float).astype(np.int64)
    v = np.rint(v_float).astype(np.int64)
    in_bounds = in_front & (u >= 0) & (u < w) & (v >= 0) & (v < h)

    # Deterministic target-camera z-buffer for the projected prior surface.
    nearest: dict[tuple[int, int], tuple[float, int, float, float]] = {}
    for idx in np.flatnonzero(in_bounds):
        key = (int(v[idx]), int(u[idx]))
        old = nearest.get(key)
        if old is None or z[idx] < old[0] or (z[idx] == old[0] and int(idx) < old[1]):
            nearest[key] = (float(z[idx]), int(idx), float(u_float[idx]), float(v_float[idx]))

    counts = {
        "prior_point_count": int(len(points)),
        "behind_camera": int((~in_front).sum()),
        "outside_image": int((in_front & ~in_bounds).sum()),
        "projected_prior_samples": int(len(nearest)),
        "prior_zbuffer_occluded": int(in_bounds.sum() - len(nearest)),
        "support": 0,
        "depth_agreement_outside_mask": 0,
        "occluded": 0,
        "depth_contradiction": 0,
        "invalid_depth": 0,
    }
    pixels = []
    for row, col in sorted(nearest):
        expected_z, source_idx, projected_u, projected_v = nearest[(row, col)]
        observed_z = float(depth[row, col])
        if not valid[row, col] or not np.isfinite(observed_z) or observed_z <= 0:
            status = "invalid_depth"
        elif observed_z < expected_z - depth_tolerance_m:
            status = "occluded"
        elif observed_z > expected_z + depth_tolerance_m:
            status = "depth_contradiction"
        elif mask[row, col]:
            status = "support"
        else:
            status = "depth_agreement_outside_mask"
        counts[status] += 1
        pixels.append({
            "row": row, "column": col, "source_point_index": source_idx,
            "projected_u_float": projected_u, "projected_v_float": projected_v,
            "projected_axial_depth_m": expected_z,
            "observed_axial_depth_m": observed_z if np.isfinite(observed_z) else None,
            "mask_contains_pixel": bool(mask[row, col]), "status": status,
        })

    usable = counts["support"] + counts["depth_agreement_outside_mask"] + counts["occluded"] + counts["depth_contradiction"]
    depth_valid = usable
    return {
        "diagnostic_only": True,
        "identity_decision": None,
        "counts": counts,
        "denominators": {
            "all_prior_points": int(len(points)),
            "front_facing_prior_points": int(in_front.sum()),
            "in_bounds_prior_points_before_zbuffer": int(in_bounds.sum()),
            "unique_zbuffer_pixels": int(len(nearest)),
            "valid_observed_depth_zbuffer_pixels": int(depth_valid),
            "valid_depth_coverage_of_zbuffer_pixels": depth_valid / len(nearest) if nearest else 0.0,
            "support_fraction_of_valid_observed_depth": counts["support"] / depth_valid if depth_valid else 0.0,
        },
        "pixels": pixels,
    }
