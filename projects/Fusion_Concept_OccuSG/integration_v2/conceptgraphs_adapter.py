"""Calibration and pose bridge for the pinned ConceptGraphs Hm3dDataset."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

P_HM3D = np.diag([1.0, -1.0, -1.0, 1.0])


def encode_pose_for_reference(T_W_Copt: np.ndarray) -> np.ndarray:
    """Encode pose so pinned Hm3dDataset's P @ pose @ P.T returns T_W_Copt."""
    T = np.asarray(T_W_Copt, dtype=np.float64)
    if T.shape != (4, 4) or not np.allclose(T[3], [0, 0, 0, 1], atol=1e-10):
        raise ValueError("expected a homogeneous camera-to-world 4x4 pose")
    R = T[:3, :3]
    if not np.allclose(R.T @ R, np.eye(3), atol=1e-7) or not np.isclose(np.linalg.det(R), 1, atol=1e-7):
        raise ValueError("pose rotation must be proper orthonormal")
    return P_HM3D @ T @ P_HM3D


def decode_reference_pose(file_pose: np.ndarray) -> np.ndarray:
    """Apply the exact Hm3dDataset pose normalization from the pinned source."""
    T = np.asarray(file_pose, dtype=np.float64)
    if T.shape != (4, 4):
        raise ValueError("reference pose must be 4x4")
    return P_HM3D @ T @ P_HM3D


def relative_pose_to_capture_world(T_CG0_CGt: np.ndarray, T_W_CG0: np.ndarray) -> np.ndarray:
    """Map a first-camera-relative CG pose back into the capture world."""
    return np.asarray(T_W_CG0, dtype=np.float64) @ np.asarray(T_CG0_CGt, dtype=np.float64)


def backproject_pixel(u: float, v: float, depth_axial_m: float, K: np.ndarray) -> np.ndarray:
    """ConceptGraphs reference pinhole convention: x right, y down, z forward."""
    K = np.asarray(K, dtype=np.float64)
    if K.shape != (3, 3) or depth_axial_m <= 0 or not np.isfinite(depth_axial_m):
        raise ValueError("valid K and positive finite axial depth required")
    return np.array([(u-K[0, 2])*depth_axial_m/K[0, 0],
                     (v-K[1, 2])*depth_axial_m/K[1, 1], depth_axial_m])


def project_camera_point(point: np.ndarray, K: np.ndarray) -> tuple[np.ndarray, float]:
    point = np.asarray(point, dtype=np.float64)
    if point.shape != (3,) or point[2] <= 0 or not np.all(np.isfinite(point)):
        raise ValueError("point must be finite and in front of camera")
    K = np.asarray(K, dtype=np.float64)
    p = K @ point
    return p[:2] / p[2], float(p[2])


def load_reference_dataset(package_root: Path, reference_root: Path) -> Any:
    """Use the pinned get_dataset API; intended to run in the CG environment."""
    import sys
    ref = reference_root.resolve()
    if str(ref) not in sys.path:
        sys.path.insert(0, str(ref))
    from conceptgraph.dataset.datasets_common import get_dataset
    config = package_root / "hm3d_adapter_dataconfig.yaml"
    return get_dataset(dataconfig=str(config), basedir=str(package_root / "dataset"),
                       sequence="pilot_00808", start=0, end=-1, stride=1,
                       desired_height=480, desired_width=640, device="cpu",
                       relative_pose=False)


def verify_reference_dataset(dataset: Any, package_manifest: dict[str, Any]) -> None:
    """Check the actual pinned loader's absolute poses/K against package contract."""
    frames = package_manifest["frames"]
    if len(dataset) != len(frames):
        raise ValueError("reference loader frame count differs from package")
    K = dataset.get_cam_K().cpu().numpy()
    expected = frames[0]["intrinsics"]
    expected_K = np.array([[expected["fx"], 0, expected["cx"]],
                           [0, expected["fy"], expected["cy"]], [0, 0, 1]])
    if not np.allclose(K, expected_K, atol=1e-6, rtol=0):
        raise ValueError("reference loader intrinsics differ from capture K")
    for i, frame in enumerate(frames):
        pose = dataset.poses[i].cpu().numpy()
        if not np.allclose(pose, frame["T_W_Copt"], atol=1e-5, rtol=0):
            raise ValueError(f"reference loader pose mismatch at {frame['frame_id']}")

