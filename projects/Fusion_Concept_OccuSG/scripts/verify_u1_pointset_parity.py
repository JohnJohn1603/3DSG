#!/usr/bin/env python3
"""Audit reconstructed U1 point-set counts and clustering backend parity.

Reads only locked source capture/caches. Writes its report below this workspace.
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from adapters.habitat.frames import frame_from_capture_record  # noqa: E402
from standalone_sg.geometry import backproject_depth  # noqa: E402
from scripts.project_detection_masks_to_3d import cluster_components, sha256  # noqa: E402


def all_pairs_components(points: np.ndarray, eps: float) -> list[np.ndarray]:
    """Literal P3 fallback, retained here as the small-fixture oracle."""
    neighbours = []
    for point in points:
        distance = np.linalg.norm(points - point, axis=1)
        neighbours.append(np.flatnonzero(distance <= eps).tolist())
    seen = np.zeros(len(points), dtype=bool)
    components: list[list[int]] = []
    for start in range(len(points)):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        component: list[int] = []
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in neighbours[current]:
                neighbour = int(neighbour)
                if not seen[neighbour]:
                    seen[neighbour] = True
                    stack.append(neighbour)
        components.append(sorted(component))
    components.sort(key=lambda component: (-len(component), min(component)))
    return [np.asarray(component, dtype=np.int64) for component in components]


def main() -> int:
    resources = json.loads((ROOT / "locks/external_resources.json").read_text())
    dense_manifest = Path(resources["files"]["dense_capture_manifest"]["path"])
    run_root = dense_manifest.parent.parent
    capture = run_root / "capture"
    cache = run_root / "detection_2d"
    d3 = run_root / "detection_3d"
    lineage_path = ROOT / "artifacts/audits/dining_table_bed_224_230/pointsets/observation_pointset_lineage.jsonl"
    profile_path = ROOT / "configs/perception/yolo11n_seg_replica_cad_dev.json"
    frame_id = "hm3d-00802-main-dense-0230"
    detection_id = f"{frame_id}:det-0004"
    fixture_size = 512

    lineage = [json.loads(line) for line in lineage_path.read_text().splitlines()]
    ledger = {
        (str(row["frame_id"]), str(row["detection_id"])): row
        for row in map(json.loads, (d3 / "object_evidence_ledger.jsonl").read_text().splitlines())
    }
    count_mismatches = []
    center_max_error_m = 0.0
    for row in lineage:
        cached = ledger[(row["frame_id"], row["observation_id"])]
        expected_count = int(cached["largest_cluster_points"])
        if row["selected_point_count"] != expected_count or row["selected_pixel_count"] != expected_count:
            count_mismatches.append(row["observation_id"])
        pointset = np.load(lineage_path.parent / row["pointset_ref"], allow_pickle=False)
        points = pointset["points_W"]
        pixels = pointset["pixel_rc"]
        if len(points) != row["selected_point_count"] or len(pixels) != row["selected_pixel_count"]:
            count_mismatches.append(row["observation_id"] + ":npz")
        center = (points.min(axis=0) + points.max(axis=0)) / 2.0
        center_error = float(np.max(np.abs(center - np.asarray(cached["center_W"], dtype=np.float64))))
        center_max_error_m = max(center_max_error_m, center_error)

    profile = json.loads(profile_path.read_text())
    geometry = profile["geometry"]
    capture_manifest = json.loads((capture / "capture_manifest.json").read_text())
    record = next(row for row in capture_manifest["frames"] if row["frame_id"] == frame_id)
    _rgbd, packet = frame_from_capture_record(record, capture)
    cache_index = json.loads((cache / "detection_2d_index.json").read_text())
    frame_cache = next(row for row in cache_index["frames"] if str(row["frame_id"]) == frame_id)
    detection = next(row for row in frame_cache["detections"] if row["detection_id"] == detection_id)
    mask = np.asarray(np.load(cache / detection["mask_file"], allow_pickle=False), dtype=bool)
    valid = packet.rgbd.valid_mask if packet.rgbd.valid_mask is not None else np.isfinite(packet.rgbd.depth_m)
    selected = valid & mask
    masked_depth = np.where(selected, packet.rgbd.depth_m, np.nan).astype(np.float32)
    cloud = backproject_depth(
        masked_depth,
        packet.rgbd.intrinsics,
        packet.rgbd.T_W_Copt,
        meters_per_unit=1.0,
        min_depth_m=float(geometry["min_depth_m"]),
        max_depth_m=float(geometry["max_depth_m"]),
        valid_mask=selected,
        timestamp=packet.timestamp_s,
        scan_id=frame_id,
    )
    candidates = np.asarray(cloud.xyz_W, dtype=np.float64)
    size = min(fixture_size, len(candidates))
    # First row-major points form one compact, real crop of the actual input cloud.
    fixture = candidates[:size]
    eps = float(geometry["cluster_eps_m"])
    scipy_components = cluster_components(fixture, eps)
    fallback_components = all_pairs_components(fixture, eps)
    identical = len(scipy_components) == len(fallback_components) and all(
        np.array_equal(left, right)
        for left, right in zip(scipy_components, fallback_components)
    )
    component_sizes = [int(len(component)) for component in scipy_components]

    report = {
        "schema_version": 1,
        "status": "pass" if not count_mismatches and center_max_error_m <= 2e-6 and identical else "fail",
        "verification_scope": "count parity against preserved P3 ledger plus backend parity on a 512-point real-data fixture; exact original point-set parity unavailable because P3 did not preserve points/pixels",
        "pointset_sidecar": {
            "observations_checked": len(lineage),
            "selected_point_and_pixel_count_mismatches": count_mismatches,
            "max_reconstructed_aabb_center_error_m": center_max_error_m,
            "center_tolerance_m": 2e-6,
            "point_order": "row-major valid-depth ∩ cached-mask pixels, then source largest-cluster indices",
        },
        "backend_fixture": {
            "frame_id": frame_id,
            "detection_id": detection_id,
            "class_input_used": False,
            "candidate_point_count": int(len(candidates)),
            "fixture_point_count": int(size),
            "selection": "first 512 row-major pre-cluster points from cached RGB-D and detection mask",
            "cluster_eps_m": eps,
            "scipy_backend": "scipy.spatial.cKDTree.query_ball_point",
            "fallback_backend": "literal all-pairs Euclidean norm, distance <= eps",
            "component_count": len(scipy_components),
            "largest_component_points": max(component_sizes, default=0),
            "components_with_at_least_2_points": sum(size >= 2 for size in component_sizes),
            "component_index_sets_identical": identical,
        },
        "provenance": {
            "python": platform.python_version(),
            "interpreter": sys.executable,
            "scipy": scipy.__version__,
            "numpy": np.__version__,
            "profile_id": profile["profile_id"],
            "profile_sha256": sha256(profile_path),
            "source_p3_script_sha256": sha256(ROOT / "scripts/project_detection_masks_to_3d.py"),
            "capture_manifest_sha256": sha256(capture / "capture_manifest.json"),
            "detection_2d_index_sha256": sha256(cache / "detection_2d_index.json"),
            "detection_3d_ledger_sha256": sha256(d3 / "object_evidence_ledger.jsonl"),
            "pointset_lineage_sha256": sha256(lineage_path),
            "config": {
                "cluster_eps_m": eps,
                "cluster_min_points": int(geometry["cluster_min_points"]),
                "sampling_stride": 1,
                "min_depth_m": float(geometry["min_depth_m"]),
                "max_depth_m": float(geometry["max_depth_m"]),
            },
        },
    }
    output = ROOT / "artifacts/audits/dining_table_bed_224_230/u1_pointset_backend_parity.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "report": str(output), "backend_fixture": report["backend_fixture"]}, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
