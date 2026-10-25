#!/usr/bin/env python3
"""Replay raw Webots R2025a RGB-D captures through the existing native graph pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from adapters.webots.capture import (  # noqa: E402
    camera_intrinsics, copy_bgra_rgb, copy_rangefinder_depth_m,
    webots_camera_node_pose_to_optical,
)
from standalone_sg.config import load_config  # noqa: E402
from standalone_sg.contracts import RGBDFrame, Transform  # noqa: E402
from standalone_sg.mapping import NativeRGBDMappingStage  # noqa: E402
from standalone_sg.mapping.grid_artifacts import GridTraceWriter  # noqa: E402
from standalone_sg.navigation import NavigationBlockGraph  # noqa: E402
from standalone_sg.pipeline import DudeM5RegionStage, FramePacket, SceneGraphBuilder  # noqa: E402
from standalone_sg.regions.dude_client import DuDeConfig, DuDeWorkerClient  # noqa: E402
from standalone_sg.regions.pipeline import RegionUpdateProcessor  # noqa: E402
from standalone_sg.serialization import (  # noqa: E402
    load_delta_log, load_snapshot, query_nodes, replay_deltas, trajectory_rows,
    write_delta_log, write_snapshot, write_trajectory_csv, write_trajectory_json,
)
from standalone_sg.visualization import render_graph, render_relationship_graph, render_spatial_map, render_trajectory_map  # noqa: E402
from m12_motor_geometry import (  # noqa: E402
    collision_report, summarize_collision_trace, validate_collision_config,
)


M2_M3_BINARY = ROOT / "build/native-m3/occupancy/standalone_sg_octomap_fixture_runner"
DUDE_BINARY = ROOT / "build/native-m4-dude/occusg_dude_worker"
DEFAULT_CAPTURE = ROOT / "artifacts/m12_webots_sensor_sequence"
DEFAULT_CONFIG = ROOT / "configs/webots/m12_sensor_sequence.json"
DEFAULT_OUTPUT = ROOT / "artifacts/m12_webots_capture_graph"


def _sha256_file(path: Path) -> str:
    """Return a content fingerprint for an input or executable artifact."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wrap_angle(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _capture_replay_policy(manifest: dict, *, allow_unsaved_route_capture: bool) -> dict:
    """Validate whether a capture may be replayed without a saved waypoint route.

    The exception is intentionally narrow: the motor controller can have a
    complete RGB-D/pose stream even when the operator pressed Q before SPACE.
    That stream is useful for an offline packet replay, but it is not a
    replayable commanded route and must remain labelled as such.
    """
    if manifest.get("webots_version") != "R2025a":
        raise ValueError("expected a capture from the pinned Webots R2025a fixture")
    if manifest.get("capture_status") == "complete":
        return {
            "input_capture_status": "complete",
            "output_capture_status": "complete",
            "route_replayable": True,
            "route_provenance": "saved_waypoint_route",
            "replay_scope": "complete Webots capture with saved commanded route",
        }
    if not allow_unsaved_route_capture:
        raise ValueError(
            "capture is incomplete; use --allow-unsaved-route-capture only for a "
            "validated RGB-D packet stream with no saved waypoint route"
        )
    required = {
        "capture_status": "failed",
        "status": "error",
        "error": "no waypoint was recorded; press SPACE before Q",
        "waypoint_count": 0,
        "waypoints_reached": 0,
    }
    mismatches = [key for key, expected in required.items() if manifest.get(key) != expected]
    if mismatches or int(manifest.get("frame_count", 0)) <= 0:
        raise ValueError(
            "--allow-unsaved-route-capture is restricted to the keyboard capture "
            f"failure with complete packet data; mismatched fields: {mismatches or ['frame_count']}"
        )
    if manifest.get("detections") != "absent" or manifest.get("regions_or_room_gt_in_capture_or_builder") is not False:
        raise ValueError("unsaved-route replay requires detections absent and no region/room GT")
    return {
        "input_capture_status": "failed",
        "output_capture_status": "partial_unsaved_route",
        "route_replayable": False,
        "route_provenance": "none_saved; actual trajectory comes from captured Supervisor base poses",
        "replay_scope": "raw Webots RGB-D/pose packets replayed without a saved commanded route",
        "input_capture_error": manifest.get("error"),
    }


def _load_capture(capture_dir: Path, config_path: Path, *,
                  allow_unsaved_route_capture: bool = False) -> tuple[dict, dict, list[FramePacket], list[dict], dict]:
    capture_dir, config_path = capture_dir.resolve(), config_path.resolve()
    manifest = json.loads((capture_dir / "capture_manifest.json").read_text(encoding="utf-8"))
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes.decode("utf-8"))
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    replay_policy = _capture_replay_policy(
        manifest, allow_unsaved_route_capture=allow_unsaved_route_capture
    )
    if manifest.get("world_config_sha256") != config_sha:
        raise ValueError("capture config hash does not match the supplied world/route config")
    world_relative = config.get("world_file")
    if world_relative is not None:
        world_path = (ROOT / world_relative).resolve()
        if ROOT not in world_path.parents or not world_path.is_file():
            raise ValueError(f"invalid project-local world_file: {world_relative!r}")
        expected_world_sha = hashlib.sha256(world_path.read_bytes()).hexdigest()
        if manifest.get("world_sha256") != expected_world_sha:
            raise ValueError("capture world hash does not match the supplied world file")
    if manifest.get("world_id") != config.get("world_id"):
        raise ValueError("capture world_id differs from supplied config")
    if manifest.get("detections") != "absent" or manifest.get("regions_or_room_gt_in_capture_or_builder") is not False:
        raise ValueError("M12 replay requires detections absent and no region/room GT in builder inputs")
    if config.get("coordinate_system") != "ENU":
        raise ValueError("this checked fixture path supports only its authored ENU world")
    world_matrix = np.asarray(config.get("world_to_canonical_matrix"), dtype=np.float64)
    if world_matrix.shape != (4, 4):
        raise ValueError("config must provide an explicit 4x4 world_to_canonical_matrix")
    T_world_webots = Transform(world_matrix, "world", "webots_world", source="calibration")
    if config.get("canonical_origin_coincident") is True and not np.array_equal(world_matrix, np.eye(4)):
        raise ValueError("origin-coincident declaration requires an exact identity matrix")

    pipeline_relative = config.get("pipeline_config", "configs/reference_code_mp3d.yaml")
    pipeline_path = (ROOT / pipeline_relative).resolve()
    if ROOT not in pipeline_path.parents or not pipeline_path.is_file():
        raise ValueError(f"invalid project-local pipeline config: {pipeline_relative!r}")
    baseline = load_config(pipeline_path)
    packets, prepared, geometry_reports = [], [], []
    previous_timestamp = -math.inf
    for record in manifest["frames"]:
        frame_dir = (capture_dir / record["frame_dir"]).resolve()
        if capture_dir not in frame_dir.parents:
            raise ValueError("manifest frame path escapes capture directory")
        metadata = json.loads((frame_dir / "metadata.json").read_text(encoding="utf-8"))
        frame_id, timestamp = metadata["frame_id"], float(metadata["timestamp_s"])
        if frame_id != record["frame_id"] or timestamp != float(record["timestamp_s"]):
            raise ValueError("manifest and frame metadata disagree")
        if metadata["coordinate_system"] != config["coordinate_system"] or metadata["world_to_canonical_matrix"] != config["world_to_canonical_matrix"]:
            raise ValueError("frame transform metadata differs from capture config")
        if timestamp <= previous_timestamp:
            raise ValueError("simulation timestamps must be strictly increasing")
        previous_timestamp = timestamp
        width, height = map(int, metadata["resolution_px"])
        bgra = (frame_dir / "camera_bgra.bin").read_bytes()
        depth_bytes = (frame_dir / "range_depth_f32.bin").read_bytes()
        if len(bgra) != int(metadata["camera_bgra_bytes"]) or len(depth_bytes) != int(metadata["range_depth_bytes"]):
            raise ValueError("raw sensor buffer size differs from capture metadata")
        rgb = copy_bgra_rgb(bgra, width, height)
        depth_m, valid_mask = copy_rangefinder_depth_m(depth_bytes, width, height)
        intrinsics = camera_intrinsics(width, height, float(metadata["rangefinder_horizontal_fov_rad"]))
        T_W_Copt = webots_camera_node_pose_to_optical(
            metadata["camera_node_pose_row_major"], timestamp, T_world_webots
        )
        T_webots_base = Transform(np.asarray(metadata["base_pose_row_major"], dtype=np.float64).reshape(4, 4),
                                  "webots_world", "base", timestamp, "ground_truth")
        T_W_base = Transform(T_world_webots.matrix @ T_webots_base.matrix, "world", "base",
                              timestamp, "ground_truth")
        if metadata["sensor_pose_source"] != "Supervisor.getPose(CAMERA_NODE)":
            raise ValueError("sensor pose must come from the actual Webots sensor node")
        if metadata["depth_units"] != "m" or metadata["depth_representation"] != "axial":
            raise ValueError("Webots RangeFinder capture must declare metric axial depth")
        if metadata["rgb_byte_order"] != "BGRA" or metadata["depth_byte_order"] != "native_float32":
            raise ValueError("unexpected Webots raw buffer encoding")
        if np.any(valid_mask & (~np.isfinite(depth_m) | (depth_m < float(metadata["min_range_m"]))
                                | (depth_m > float(metadata["max_range_m"])) )):
            raise ValueError("valid mask includes depth outside the documented RangeFinder range")

        frame = RGBDFrame(depth_m, intrinsics, T_W_Copt, timestamp, rgb=rgb,
                          valid_mask=valid_mask, frame_id=frame_id)
        packet = FramePacket(
            scene_id=str(manifest["world_id"]), sequence_id=str(manifest["sequence_id"]),
            frame_id=frame_id, frame_index=int(metadata["frame_index"]), timestamp_s=timestamp,
            input_mode="rgbd", base_pose_W=tuple(float(x) for x in T_W_base.translation),
            rgbd=frame, detections=None,
            source_metadata={
                "capture_kind": "webots_r2025a_raw_capture_offline_replay",
                "world_id": manifest["world_id"], "webots_version": "R2025a",
                "depth_units": "m", "depth_scale_to_m": 1.0, "depth_representation": "axial",
                "depth_invalid_policy": "non-finite and non-positive RangeFinder samples masked; never converted to no-return rays",
                "valid_mask_source": "finite positive RangeFinder depth samples",
                "sensor_pose_source": "actual Supervisor.getPose(CAMERA_NODE)",
                "base_pose_source": "actual Supervisor.getPose(PROBE_ROBOT); separately transformed",
                "T_world_webots": world_matrix.tolist(), "T_world_sensor_optical": T_W_Copt.matrix.tolist(),
                "T_world_base": T_W_base.matrix.tolist(),
                "intrinsics": {"width": width, "height": height, "fx": intrinsics.fx,
                               "fy": intrinsics.fy, "cx": intrinsics.cx, "cy": intrinsics.cy,
                               "calibration_id": intrinsics.calibration_id},
                "min_depth_m": float(metadata["min_range_m"]),
                "max_depth_m": float(metadata["max_range_m"]),
                "max_range_m": float(metadata["max_range_m"]),
                "backprojection_stride": baseline.backprojection.stride,
                "detections_provider": "absent", "regions_or_room_gt_in_builder": False,
                "capture_config_sha256": config_sha,
            },
        )
        if frame.frame_id != packet.frame_id or frame.timestamp != packet.timestamp_s:
            raise ValueError("RGBDFrame and FramePacket ID/timestamp mismatch")
        packets.append(packet)
        prepared.append({"metadata": metadata, "depth_m": depth_m, "valid_mask": valid_mask,
                         "intrinsics": intrinsics, "T_W_Copt": T_W_Copt, "T_W_base": T_W_base})
    if len(packets) != int(manifest["frame_count"]):
        raise ValueError("capture frame count must match the capture manifest")
    frame_indices = [packet.frame_index for packet in packets]
    if frame_indices != list(range(len(packets))):
        raise ValueError("captured frame indices must be contiguous and independent of waypoint count")
    geometry_reports = validate_off_axis_geometry(config, prepared)
    geometry_reports[0]["sensor_extrinsics"] = validate_sensor_extrinsics(config, prepared)
    if manifest.get("motion_mode") == "motor":
        geometry_reports[0]["collision_check"] = validate_motor_physics_trace(capture_dir, config, manifest)
    else:
        geometry_reports[0]["collision_check"] = validate_motor_route(config, prepared)
    return manifest, config, packets, geometry_reports, replay_policy


def _ray_aabb(origin: np.ndarray, direction: np.ndarray, low: np.ndarray, high: np.ndarray) -> tuple[float, np.ndarray] | None:
    inv = np.divide(1.0, direction, out=np.full(3, np.inf), where=np.abs(direction) > 1e-12)
    t0, t1 = (low - origin) * inv, (high - origin) * inv
    near = float(np.max(np.minimum(t0, t1)))
    far = float(np.min(np.maximum(t0, t1)))
    if far < max(near, 0.0):
        return None
    t = near if near >= 0.0 else far
    if not math.isfinite(t) or t <= 0:
        return None
    return t, origin + direction * t


def validate_off_axis_geometry(config: dict, frames: list[dict]) -> list[dict]:
    """Compare live pixels with independently authored AABBs and analytic ray/AABB hits."""
    fixture = config.get("fixture_geometry", {})
    tolerance = float(fixture.get("geometry_tolerance_m", 0.02))
    initial, reports = frames[0], []
    cx, cy = initial["intrinsics"].cx, initial["intrinsics"].cy
    side_pixels, vertical_pixels = {}, {}
    markers = fixture.get("markers", [])
    for frame in frames:
        T = frame["T_W_Copt"].matrix
        inv_T = np.linalg.inv(T)
        intr = frame["intrinsics"]
        per_marker = []
        for marker in markers:
            center = np.asarray(marker["center_world"], dtype=np.float64)
            local = inv_T[:3, :3] @ center + inv_T[:3, 3]
            if local[2] <= 0:
                continue
            u_float = intr.fx * local[0] / local[2] + intr.cx
            v_float = intr.fy * local[1] / local[2] + intr.cy
            u, v = int(round(u_float)), int(round(v_float))
            if not (0 <= u < intr.width and 0 <= v < intr.height):
                continue
            low = center - np.asarray(marker["size_xyz"], dtype=np.float64) / 2.0
            high = center + np.asarray(marker["size_xyz"], dtype=np.float64) / 2.0
            direction_C = np.array([(u - intr.cx) / intr.fx, (v - intr.cy) / intr.fy, 1.0])
            direction_W = T[:3, :3] @ direction_C
            expected = _ray_aabb(T[:3, 3], direction_W, low, high)
            if expected is None:
                continue
            expected_depth, expected_hit = expected
            measured_depth = float(frame["depth_m"][v, u])
            valid = bool(frame["valid_mask"][v, u])
            if not valid:
                raise AssertionError(f"authored marker pixel is invalid: {marker['id']} frame {frame['metadata']['frame_id']}")
            depth_error = abs(measured_depth - expected_depth)
            if depth_error > tolerance:
                raise AssertionError(f"RangeFinder depth misses authored {marker['id']} AABB by {depth_error:.4f} m")
            measured_hit = T[:3, 3] + direction_W * measured_depth
            hit_error = float(np.linalg.norm(measured_hit - expected_hit))
            if hit_error > tolerance:
                raise AssertionError(f"pixel ray world hit misses authored {marker['id']} AABB by {hit_error:.4f} m")
            entry = {"marker_id": marker["id"], "frame_id": frame["metadata"]["frame_id"],
                     "projected_center_px": [u_float, v_float], "sample_px": [u, v],
                     "expected_axial_depth_m": expected_depth, "measured_axial_depth_m": measured_depth,
                     "depth_error_m": depth_error, "analytic_world_hit_m": expected_hit.tolist(),
                     "backprojected_sample_world_m": measured_hit.tolist(), "world_hit_error_m": hit_error,
                     "expected_geometry_source": "configured fixture marker AABB + analytic camera ray/AABB intersection"}
            per_marker.append(entry)
            if frame is initial:
                if marker["id"] in ("image_left", "image_right"):
                    side_pixels[marker["id"]] = u_float
                if marker["id"] in ("image_top", "image_bottom"):
                    vertical_pixels[marker["id"]] = v_float
        reports.append({"frame_id": frame["metadata"]["frame_id"], "sensor_origin_W": T[:3, 3].tolist(),
                        "marker_checks": per_marker})
    if fixture.get("require_four_off_axis_markers", bool(markers)):
        if not all(k in side_pixels for k in ("image_left", "image_right")):
            raise AssertionError("both authored horizontal markers must project inside the initial image")
        if not (side_pixels["image_left"] < cx < side_pixels["image_right"]):
            raise AssertionError("left/right marker projection order is reversed or inconsistent")
        if not all(k in vertical_pixels for k in ("image_top", "image_bottom")):
            raise AssertionError("both authored vertical markers must project inside the initial image")
        if not (vertical_pixels["image_top"] < cy < vertical_pixels["image_bottom"]):
            raise AssertionError("top/bottom marker projection order is reversed or inconsistent")
    poses = [f["T_W_Copt"].matrix for f in frames]
    moved = max(float(np.linalg.norm(p[:3, 3] - poses[0][:3, 3])) for p in poses[1:])
    rotated = max(float(np.linalg.norm(p[:3, :3] - poses[0][:3, :3], ord="fro")) for p in poses[1:])
    if moved < 0.05 or rotated < 0.02:
        raise AssertionError("capture sequence must include actual sensor translation and rotation")
    return [{"initial_image_order_px": ({"left_u": side_pixels["image_left"], "center_u": cx,
                                          "right_u": side_pixels["image_right"], "top_v": vertical_pixels["image_top"],
                                          "center_v": cy, "bottom_v": vertical_pixels["image_bottom"]}
                                         if markers else None),
             "max_sensor_translation_from_first_m": moved,
             "max_sensor_rotation_frobenius_from_first": rotated,
             "tolerance_m": tolerance,
             "marker_gt_configured": bool(markers),
             "pose_motion_check": "pass",
             "independent_GT": "authored AABBs from world/route config; expected hit computed by ray-box intersection, not M1 backprojection" if markers else "no marker GT configured; authored scene config is used only for capture identity and route",
             "frames": reports}]


def validate_sensor_extrinsics(config: dict, frames: list[dict]) -> dict:
    """Validate the motor PROTO mount independently of RGB-D backprojection."""
    sensor_cfg = config.get("sensor")
    if sensor_cfg is None:
        return {"configured": False, "status": "not_configured"}
    expected_offset = np.asarray(sensor_cfg["base_offset_m"], dtype=np.float64)
    expected_yaw = float(sensor_cfg.get("relative_yaw_rad", 0.0))
    tolerance = float(sensor_cfg.get("extrinsics_tolerance_m", 0.03))
    yaw_tolerance = float(sensor_cfg.get("extrinsics_yaw_tolerance_rad", 0.03))
    translations, yaw_errors = [], []
    for frame in frames:
        base = np.asarray(frame["metadata"]["base_pose_row_major"], dtype=np.float64).reshape(4, 4)
        sensor = np.asarray(frame["metadata"]["camera_node_pose_row_major"], dtype=np.float64).reshape(4, 4)
        relative = np.linalg.inv(base) @ sensor
        offset_error = float(np.linalg.norm(relative[:3, 3] - expected_offset))
        yaw = math.atan2(float(relative[1, 0]), float(relative[0, 0]))
        yaw_error = abs(_wrap_angle(yaw - expected_yaw))
        if offset_error > tolerance:
            raise AssertionError(f"sensor/base translation extrinsics error {offset_error:.6f} m exceeds {tolerance:.6f} m")
        if yaw_error > yaw_tolerance:
            raise AssertionError(f"sensor/base yaw extrinsics error {yaw_error:.6f} rad exceeds {yaw_tolerance:.6f} rad")
        translations.append(offset_error)
        yaw_errors.append(yaw_error)
    return {"configured": True, "status": "pass", "expected_offset_base_m": expected_offset.tolist(),
            "expected_relative_yaw_rad": expected_yaw, "max_translation_error_m": max(translations),
            "max_yaw_error_rad": max(yaw_errors), "tolerance_m": tolerance,
            "yaw_tolerance_rad": yaw_tolerance, "independent_source": "Supervisor base/sensor pose matrices"}


def validate_motor_route(config: dict, frames: list[dict]) -> dict:
    """Check recorded Supervisor poses against authored wall AABBs.

    This is a route/clearance assertion, not a replacement for Webots physics
    contacts. It complements the PROTO boundingObjects and prevents a route
    that visibly crosses an authored wall from being reported as valid.
    """
    collision = config.get("collision")
    if not collision:
        return {"configured": False, "status": "not_configured"}
    radius = float(collision["robot_radius_m"])
    violations = []
    for frame in frames:
        pose = np.asarray(frame["metadata"]["base_pose_row_major"], dtype=np.float64).reshape(4, 4)
        x, y = float(pose[0, 3]), float(pose[1, 3])
        for wall in collision.get("wall_aabbs_xy", []):
            low, high = np.asarray(wall["min"], dtype=np.float64), np.asarray(wall["max"], dtype=np.float64)
            if low[0] - radius <= x <= high[0] + radius and low[1] - radius <= y <= high[1] + radius:
                violations.append({"frame_id": frame["metadata"]["frame_id"], "wall": wall["name"], "x_m": x, "y_m": y})
    if violations:
        raise AssertionError(f"motor route intersects an authored wall clearance envelope: {violations[:3]}")
    return {"configured": True, "status": "pass", "robot_radius_m": radius,
            "wall_count": len(collision.get("wall_aabbs_xy", [])),
            "checked_frames": len(frames), "method": "Supervisor base center against inflated authored wall AABBs"}


def validate_motor_physics_trace(capture_dir: Path, config: dict, manifest: dict) -> dict:
    """Validate every recorded physics step, not only sparse RGB-D frames."""
    collision = config.get("collision")
    validate_collision_config(collision)
    trace_name = manifest.get("physics_trace_file", "physics_trace.json")
    trace_path = (capture_dir / trace_name).resolve()
    if capture_dir not in trace_path.parents or not trace_path.is_file():
        raise ValueError("motor capture must contain a project-local physics_trace.json")
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    if not isinstance(trace, list) or not trace:
        raise ValueError("motor physics trace must be a non-empty list")
    expected_count = int(manifest.get("physics_step_count", len(trace)))
    if expected_count != len(trace):
        raise ValueError("physics_step_count does not match physics_trace.json")
    previous_timestamp = -math.inf
    checked = []
    for expected_index, row in enumerate(trace, start=1):
        if int(row["step_index"]) != expected_index:
            raise ValueError("physics trace step indices must be contiguous")
        timestamp = float(row["timestamp_s"])
        if not math.isfinite(timestamp) or timestamp <= previous_timestamp:
            raise ValueError("physics trace timestamps must be finite and strictly increasing")
        previous_timestamp = timestamp
        pose = np.asarray(row["base_pose_row_major"], dtype=np.float64)
        if pose.shape != (16,) or not np.all(np.isfinite(pose)):
            raise ValueError("physics trace contains an invalid base pose")
        x, y = float(pose[3]), float(pose[7])
        report = collision_report(x, y, collision)
        if row["collision_state"] != report["state"]:
            raise ValueError("physics trace collision state differs from its authored AABB recomputation")
        if not math.isclose(float(row["minimum_clearance_m"]), report["minimum_clearance_m"], rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("physics trace clearance differs from its authored AABB recomputation")
        checked.append({
            "collision_state": report["state"],
            "minimum_clearance_m": report["minimum_clearance_m"],
            "contact_names": report["contact_names"],
            "penetration_names": report["penetration_names"],
        })
    summary = summarize_collision_trace(checked)
    summary.update({
        "configured": True,
        "robot_radius_m": float(collision["robot_radius_m"]),
        "wall_count": len(collision.get("wall_aabbs_xy", [])),
        "obstacle_count": len(collision.get("obstacle_aabbs_xy", [])),
        "checked_physics_steps": len(trace),
        "capture_frame_count": int(manifest["frame_count"]),
        "waypoints_reached": int(manifest.get("waypoints_reached", 0)),
        "waypoint_count": int(manifest.get("waypoint_count", 0)),
        "method": "Supervisor pose at every physics step against authored wall and obstacle AABBs",
        "contact_vs_penetration": "contact is clearance within contact_tolerance_m; penetration is negative clearance",
        "waypoint_completion_separate_from_collision": True,
        "trace_file": trace_name,
    })
    if summary["status"] != "pass":
        raise AssertionError("motor physics trace contains geometric penetration")
    return summary


def _run_builder(packets: list[FramePacket], *, config_sha: str, pipeline_config: Path,
                 output: Path | None = None,
                 m2_binary: Path = M2_M3_BINARY, dude_binary: Path = DUDE_BINARY,
                 grid_writer: GridTraceWriter | None = None,
                 dude_preprocessing_policy: str = "source") -> tuple[object, list, list, list, dict]:
    for binary in (m2_binary, dude_binary):
        if not binary.is_file():
            raise RuntimeError(f"native worker binary missing: {binary}")
    phase_config = load_config(pipeline_config)
    navigation = NavigationBlockGraph(phase_config.navigation)
    mapping = NativeRGBDMappingStage(m2_binary, resolution_m=phase_config.occupancy.resolution_m,
                                     timeout_s=30.0, default_max_range_m=8.0)
    region_stage = DudeM5RegionStage(
        DuDeWorkerClient(dude_binary, config=DuDeConfig(
            preprocessing_policy=dude_preprocessing_policy,
        )), RegionUpdateProcessor(),
        capture_preprocessed_mask=grid_writer is not None,
    )
    builder = SceneGraphBuilder(mapping_stage=mapping, region_stage=region_stage,
                                navigation=navigation, config_hash=f"m12-webots-{config_sha}")
    initial = builder.snapshot()
    deltas, diagnostics, snapshots = [], [], []
    start = time.perf_counter()
    try:
        for packet in packets:
            frame_start = time.perf_counter()
            snapshot, delta = builder.update(packet)
            elapsed_ms = (time.perf_counter() - frame_start) * 1000.0
            deltas.append(delta)
            snapshots.append(snapshot)
            response, result = region_stage.last_response, region_stage.last_result
            if grid_writer is not None:
                # builder.last_grid is the same object returned by the injected
                # M3 stage and consumed by M4/M7/M8; this is observation only.
                grid_writer.write_frame(
                    packet=packet, grid=builder.last_grid, response=response,
                    result=result, snapshot=snapshot,
                )
            nav_snapshot = navigation.snapshot()
            diagnostics.append({
                "frame_id": packet.frame_id, "frame_index": packet.frame_index,
                "simulation_timestamp_s": packet.timestamp_s,
                "processing_runtime_ms": elapsed_ms,
                "base_pose_W": list(packet.base_pose_W),
                "sensor_pose_W_Copt": packet.rgbd.T_W_Copt.matrix.tolist(),
                "mapping": dict(mapping.records[-1]),
                "m4_status": response.get("status"), "m4_branch": response.get("branch"),
                "m4_reason": response.get("reason"),
                "m5_accepted": bool(result.accepted), "m5_stale": bool(result.stale),
                "m5_reason": result.reason,
                "m5_request_revision": result.request_revision,
                "m5_accepted_observation_revision": result.accepted_observation_revision,
                "region_count": len(result.current_regions),
                "region_ids": [int(r.persistent_id) for r in result.current_regions],
                "room_count": len(query_nodes(snapshot, "room")),
                "room_ids": [n.node_id for n in query_nodes(snapshot, "room")],
                "navigation_node_count": len(nav_snapshot),
                "stage_status": dict(delta.diagnostics["stage_status"]),
                "events": list(delta.events), "graph_revision": snapshot.revision,
                "node_count": len(snapshot.nodes), "edge_count": len(snapshot.edges),
                "sampled_python_ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            })
        flush_at = packets[-1].timestamp_s + 1e-6
        final, flush_delta = builder.flush(flush_at)
        deltas.append(flush_delta)
        elapsed_total_ms = (time.perf_counter() - start) * 1000.0
        return final, deltas, diagnostics, snapshots, {"builder_update_flush_runtime_ms": elapsed_total_ms,
                                                        "flush_timestamp_s": flush_at}
    except Exception:
        builder.close()
        raise
    finally:
        if not builder._closed:
            builder.close()


def run_replay(capture_dir: Path, config_path: Path, output: Path, *,
               pipeline_config: Path | None = None,
               m2_binary: Path = M2_M3_BINARY, dude_binary: Path = DUDE_BINARY,
               deterministic_check: bool = True, grid_output: Path | None = None,
               allow_unsaved_route_capture: bool = False,
               dude_preprocessing_policy: str = "source") -> dict:
    manifest, config, packets, geometry, replay_policy = _load_capture(
        capture_dir, config_path,
        allow_unsaved_route_capture=allow_unsaved_route_capture,
    )
    config_sha = manifest["world_config_sha256"]
    pipeline_path = (pipeline_config or (ROOT / config.get("pipeline_config", "configs/reference_code_mp3d.yaml"))).resolve()
    if ROOT not in pipeline_path.parents or not pipeline_path.is_file():
        raise ValueError(f"invalid project-local pipeline config: {pipeline_path}")
    initial = None
    start_total = time.perf_counter()
    grid_writer = GridTraceWriter(grid_output) if grid_output is not None else None
    final, deltas, frame_diagnostics, snapshots, runtime = _run_builder(
        packets, config_sha=config_sha, pipeline_config=pipeline_path, output=output,
        m2_binary=m2_binary, dude_binary=dude_binary, grid_writer=grid_writer,
        dude_preprocessing_policy=dude_preprocessing_policy,
    )
    if grid_writer is not None:
        grid_writer.close(capture_dir=capture_dir, config_path=config_path)
    # Recreate every stateful stage from raw captured packets, rather than reusing M2/M3/M4 state.
    deterministic_equal = None
    if deterministic_check:
        final_again, deltas_again, diagnostics_again, _snapshots_again, _runtime_again = _run_builder(
            packets, config_sha=config_sha, pipeline_config=pipeline_path,
            m2_binary=m2_binary, dude_binary=dude_binary,
            dude_preprocessing_policy=dude_preprocessing_policy,
        )
        deterministic_equal = final_again == final and deltas_again == deltas
        if not deterministic_equal:
            raise RuntimeError("fresh-instance replay from the same captured packets was not deterministic")
    output.mkdir(parents=True, exist_ok=True)
    # The initial snapshot is a valid empty graph at revision zero; the delta log includes each update and flush.
    from standalone_sg.pipeline.contracts import SceneGraphSnapshot
    initial = SceneGraphSnapshot(1, None, None, 0, None, (), (), final.config_hash,
                                 {"mapping": False, "regions": False})
    write_snapshot(output / "snapshot.json", final)
    write_delta_log(output / "delta_log.json", deltas)
    write_trajectory_csv(output / "trajectory.csv", final)
    write_trajectory_json(output / "trajectory.json", final)
    render_trajectory_map(final, output / "trajectory_rooms.png")
    render_graph(final, output / "scene_graph.png")
    render_spatial_map(final, output / "spatial_map.png")
    render_relationship_graph(final, output / "relationship_graph.png")
    reloaded = load_snapshot(output / "snapshot.json")
    replayed = replay_deltas(initial, load_delta_log(output / "delta_log.json"))
    if reloaded != final or replayed != reloaded:
        raise RuntimeError("M10 snapshot reload/delta replay mismatch")
    trajectory = trajectory_rows(reloaded)
    if len(trajectory) != len(packets):
        raise RuntimeError("graph trajectory must contain exactly the accepted captured base poses")
    for row, packet in zip(trajectory, packets):
        if row["frame_id"] != packet.frame_id or row["timestamp_s"] != packet.timestamp_s:
            raise RuntimeError("trajectory frame/timestamp differs from captured packet")
        if not np.allclose([row["x_m"], row["y_m"], row["z_m"]], packet.base_pose_W, rtol=0.0, atol=1e-8):
            raise RuntimeError("trajectory pose differs from captured base pose")
    node_ids = {n.node_id for n in reloaded.nodes}
    if any(e.source_id not in node_ids or e.target_id not in node_ids or e.source_id == e.target_id for e in reloaded.edges):
        raise RuntimeError("reloaded graph contains dangling or self edges")
    diagnostics_path = output / "frame_diagnostics.json"
    diagnostics_path.write_text(json.dumps(frame_diagnostics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    geometry_path = output / "off_axis_geometry.json"
    geometry_path.write_text(json.dumps(geometry, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    # Optional milestone views are selected by route labels in the scene config,
    # never by hardcoded scene coordinates in this runner.
    route_labels = {int(item["frame_index"]): item.get("route_label")
                    for item in manifest.get("frames", [])}
    for snapshot, packet in zip(snapshots, packets):
        label = route_labels.get(packet.frame_index)
        if label in {"before_door", "after_door", "return"}:
            render_trajectory_map(snapshot, output / f"trajectory_{label}.png")
            render_graph(snapshot, output / f"scene_graph_{label}.png")
            render_spatial_map(snapshot, output / f"spatial_map_{label}.png")
            render_relationship_graph(snapshot, output / f"relationship_graph_{label}.png")
    pipeline_sha = _sha256_file(pipeline_path)
    m2_binary = Path(m2_binary).resolve()
    dude_binary = Path(dude_binary).resolve()
    replay_runner_sha = _sha256_file(Path(__file__).resolve())
    source_lock = ROOT / "locks/sources.json"
    manifest_out = {
        "kind": "webots_r2025a_captured_rgbd_offline_native_replay",
        "capture_status": replay_policy["output_capture_status"], "pipeline_status": "pass",
        "input_capture_status": replay_policy["input_capture_status"],
        "input_capture_error": replay_policy.get("input_capture_error"),
        "route_replayable": replay_policy["route_replayable"],
        "route_provenance": replay_policy["route_provenance"],
        "replay_scope": replay_policy["replay_scope"],
        "world_id": manifest["world_id"], "sequence_id": manifest["sequence_id"],
        "webots_version": manifest["webots_version"], "world_config": manifest["world_config"],
        "world_config_sha256": config_sha, "world_file": manifest.get("world_file"),
        "world_sha256": manifest.get("world_sha256"), "pipeline_config": str(pipeline_path.relative_to(ROOT)),
        "pipeline_config_sha256": pipeline_sha, "route_type": manifest["route_type"],
        "dude_preprocessing_policy": dude_preprocessing_policy,
        "build_fingerprint": {
            "replay_runner": str(Path(__file__).resolve().relative_to(ROOT)),
            "replay_runner_sha256": replay_runner_sha,
            "m2_m3_binary": str(m2_binary.relative_to(ROOT)) if ROOT in m2_binary.parents else str(m2_binary),
            "m2_m3_binary_sha256": _sha256_file(m2_binary),
            "m4_binary": str(dude_binary.relative_to(ROOT)) if ROOT in dude_binary.parents else str(dude_binary),
            "m4_binary_sha256": _sha256_file(dude_binary),
            "source_lock": str(source_lock.relative_to(ROOT)),
            "source_lock_sha256": _sha256_file(source_lock),
        },
        "motion_mode": manifest.get("motion_mode", "pose_replay"),
        "commanded_route": config.get("route", []) if replay_policy["route_replayable"] else [],
        "actual_trajectory_artifacts": ["trajectory.csv", "trajectory.json"],
        "frames": len(packets), "input": "raw Webots BGRA + RangeFinder float32 capture; RGBDFrame and FramePacket normalized offline",
        "detections": "absent", "regions_or_room_gt_in_builder": False,
        "stages": ["existing M1 backproject_depth", "native M2 OctoMap", "native M3 C++ MapConverter",
                   "pinned DuDe + M5", "M6 object provider absent", "M7", "M8", "M9", "M10"],
        "mapping_backend": "NativeRGBDMappingStage; same configured native path as non-Webots inputs",
        "builder_live_inside_webots_controller": False,
        "offline_replay_from_raw_capture": True,
        "sensor_geometry": {"off_axis_geometry_pass": bool(geometry[0].get("marker_gt_configured", False)),
                            "pose_motion_pass": geometry[0].get("pose_motion_check") == "pass",
                            "marker_gt_configured": geometry[0].get("marker_gt_configured", False),
                            "sensor_extrinsics": geometry[0].get("sensor_extrinsics"),
                            "collision_check": geometry[0].get("collision_check"),
                            "tolerance_m": config.get("fixture_geometry", {}).get("geometry_tolerance_m", 0.02),
                            "checks": geometry},
        "runtime": {**runtime, "capture_load_geometry_export_total_ms": (time.perf_counter() - start_total) * 1000.0,
                    "measurement_scope": "offline packet validation, fresh native worker updates and flush; excludes Webots capture time; export/render/reload outside builder runtime"},
        "python_process_ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "deterministic_fresh_instance_replay_equal": deterministic_equal,
        "graph": {"revision": reloaded.revision, "nodes": len(reloaded.nodes), "edges": len(reloaded.edges),
                  "regions_final": frame_diagnostics[-1]["region_count"],
                  "rooms_final": len(query_nodes(reloaded, "room")),
                  "region_ids_final": frame_diagnostics[-1]["region_ids"],
                  "room_ids_final": [n.node_id for n in query_nodes(reloaded, "room")],
                  "trajectory_poses": len(trajectory), "stale_flags": dict(reloaded.stale_flags),
                  "snapshot_reload_equal": reloaded == final, "delta_replay_equal": replayed == reloaded,
                  "dangling_or_self_edges": False, "coordinate_and_trajectory_checks": "pass"},
        "artifacts": ["snapshot.json", "delta_log.json", "trajectory.csv", "trajectory.json",
                      "trajectory_rooms.png", "scene_graph.png", "spatial_map.png", "relationship_graph.png",
                      "frame_diagnostics.json", "off_axis_geometry.json"],
        "limitations": ["scripted Supervisor ground-truth poses; not physical robot dynamics or navigation",
                        "segmentation quality is reported, not forced; room count is the native baseline result",
                        "offline captured-packet replay; SceneGraphBuilder is not live in the Webots controller",
                        "M4 local technical/parity scope only; cloud and distribution gates remain open"],
        "cloud_upload": False,
    }
    if grid_output is not None:
        manifest_out["grid_trace"] = {
            "enabled": True,
            "path": str(grid_output),
            "index": str(grid_output / "index.json"),
            "includes_m3_arrays": ["values", "z_floor", "z_ceiling"],
            "includes_m4_preprocessing": True,
            "same_grid_as_downstream": True,
            "project_called_extra_times": False,
        }
        manifest_out["artifacts"].append("grid_trace/index.json")
    else:
        manifest_out["grid_trace"] = {"enabled": False}
    (output / "manifest.json").write_text(json.dumps(manifest_out, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return manifest_out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pipeline-config", type=Path, default=None)
    parser.add_argument("--m2-m3-binary", type=Path, default=M2_M3_BINARY)
    parser.add_argument("--dude-binary", type=Path, default=DUDE_BINARY)
    parser.add_argument("--no-determinism-check", action="store_true")
    parser.add_argument("--grid-output", type=Path, default=None,
                        help="opt-in lossless M3/M4 per-frame trace directory")
    parser.add_argument("--allow-unsaved-route-capture", action="store_true",
                        help="replay a validated keyboard RGB-D capture that has no saved waypoint route")
    parser.add_argument("--dude-preprocessing-policy", choices=("source", "extended_unknown_boundary"),
                        default="source", help="DuDe preprocessing profile; extended mode is not source parity")
    args = parser.parse_args()
    report = run_replay(args.capture, args.config, args.output, pipeline_config=args.pipeline_config,
                        m2_binary=args.m2_m3_binary,
                        dude_binary=args.dude_binary, deterministic_check=not args.no_determinism_check,
                        grid_output=args.grid_output,
                        allow_unsaved_route_capture=args.allow_unsaved_route_capture,
                        dude_preprocessing_policy=args.dude_preprocessing_policy)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
