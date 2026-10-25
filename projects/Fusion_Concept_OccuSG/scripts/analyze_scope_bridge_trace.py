#!/usr/bin/env python3
"""Analyze an instrumented native M2/M3 height-interval trace by prefix.

This is a read-only diagnostic. It does not run or change the mapper. The
interval state is reconstructed only from trace updates at or before each
frame, then joined to that frame's exact M3 grid and M4 room revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inside_ring(x: np.ndarray, y: np.ndarray, ring: list[list[float]]) -> np.ndarray:
    """Vectorized boundary-inclusive point-in-ring test."""
    inside = np.zeros(x.shape, dtype=bool)
    boundary = np.zeros(x.shape, dtype=bool)
    eps = 1e-9
    for i, a in enumerate(ring):
        b = ring[(i + 1) % len(ring)]
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        cross = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        on_segment = (
            (np.abs(cross) <= eps)
            & (x >= min(ax, bx) - eps)
            & (x <= max(ax, bx) + eps)
            & (y >= min(ay, by) - eps)
            & (y <= max(ay, by) + eps)
        )
        boundary |= on_segment
        crosses = (ay > y) != (by > y)
        denominator = by - ay
        if denominator != 0.0:
            x_at_y = (bx - ax) * (y - ay) / denominator + ax
            inside ^= crosses & (x < x_at_y)
    return inside | boundary


def in_polygon_grid(x: np.ndarray, y: np.ndarray, attrs: dict[str, Any]) -> np.ndarray:
    inside = inside_ring(x, y, attrs["outer_ring"])
    for hole in attrs.get("holes", []):
        # M4 currently exports no holes; this preserves the geometry contract
        # if a future source revision starts emitting them.
        inside &= ~inside_ring(x, y, hole)
    return inside


def interval_envelope(intervals: list[dict[str, float]]) -> tuple[float, float]:
    return (
        min(float(value["bottom_m"]) for value in intervals),
        max(float(value["top_m"]) for value in intervals),
    )


def intervals_are_disjoint(intervals: list[dict[str, float]]) -> bool:
    ordered = sorted(
        ((float(value["bottom_m"]), float(value["top_m"])) for value in intervals),
        key=lambda item: item[0],
    )
    return all(a[1] < b[0] for a, b in zip(ordered, ordered[1:]))


def remap_prefix_events(
    events: list[tuple[int, dict[str, Any]]],
    prefix_revision: int,
    origin_xy: list[float],
    resolution: float,
    shape: tuple[int, int],
) -> dict[tuple[int, int], tuple[int, dict[str, Any]]]:
    """Build M3 cell state using only interval events at/before a revision."""
    height, width = shape
    origin_x, origin_y = (float(v) for v in origin_xy)
    current_cells: dict[tuple[int, int], tuple[int, dict[str, Any]]] = {}
    for event_revision, cell in events:
        if event_revision > prefix_revision:
            continue
        x, y = (float(v) for v in cell["world_xy_m"])
        col = math.floor((x - origin_x) / resolution)
        row = math.floor((y - origin_y) / resolution)
        if 0 <= row < height and 0 <= col < width:
            current_cells[(row, col)] = (event_revision, cell)
    return current_cells


def validate_hybrid_prefix_geometry(timeline_dir: Path, capture_manifest_sha256: str) -> dict[str, Any]:
    """Validate hybrid XYZ scope provenance without using final-map geometry.

    This verifies that each saved prefix cloud is a finite world-frame metric
    cloud with only observations available by that prefix. It does not assign
    a floor/layer or claim the room projection is single-level.
    """
    timeline_manifest_path = timeline_dir / "manifest.json"
    timeline_manifest = json.loads(timeline_manifest_path.read_text())
    parent = timeline_dir.parent
    experiment_path = parent / "mapping" / "experiment_contract.json"
    export_path = parent / "mapping" / "export" / "export_manifest.json"
    if not experiment_path.is_file() or not export_path.is_file():
        raise ValueError("hybrid experiment/export provenance is missing")
    experiment = json.loads(experiment_path.read_text())
    export = json.loads(export_path.read_text())
    frame_index = json.loads((timeline_dir / "frame_index.json").read_text())
    if experiment["capture_manifest_sha256"] != capture_manifest_sha256:
        raise ValueError("hybrid mapping contract belongs to a different capture")
    if timeline_manifest["input_hashes"]["capture_manifest_sha256"] != capture_manifest_sha256:
        raise ValueError("hybrid timeline capture hash differs from native replay")
    if timeline_manifest["causality"]["objects"] != "prefix_state_only":
        raise ValueError("hybrid object timeline is not marked prefix-state-only")
    if export["coordinate_frame"] != "conceptgraphs_world_from_capture_contract" or export["units"] != "m":
        raise ValueError("hybrid object export frame/units do not match the capture metric-world contract")
    transform = np.asarray(export["T_capture_world_from_cg_world"], dtype=np.float64)
    if transform.shape != (4, 4) or not np.array_equal(transform, np.eye(4)):
        raise ValueError("hybrid object export requires an unexpected world transform")

    verified_snapshots = 0
    verified_object_clouds = 0
    total_points = 0
    point_z_min = math.inf
    point_z_max = -math.inf
    input_observation_ids = set(experiment["input_observation_ids"])
    for frame in frame_index["frames"]:
        idx = int(frame["frame_index"])
        snapshot_path = timeline_dir / frame["snapshot"]
        if sha256(snapshot_path) != frame["snapshot_sha256"]:
            raise ValueError(f"hybrid prefix snapshot hash mismatch at frame {idx}")
        snapshot = json.loads(snapshot_path.read_text())
        if snapshot["frame_index"] != idx or snapshot["frame_id"] != frame["frame_id"]:
            raise ValueError(f"hybrid prefix snapshot index mismatch at frame {idx}")
        if snapshot.get("future_observations_used") is not False:
            raise ValueError(f"hybrid prefix does not attest no-future evidence at frame {idx}")

        def observation_frame(observation_id: str) -> int:
            try:
                frame_id, _ = observation_id.rsplit(":det-", 1)
                return int(frame_id.rsplit("-", 1)[1])
            except (ValueError, IndexError) as exc:
                raise ValueError(f"malformed full observation ID: {observation_id}") from exc

        prefix_ids = set(snapshot["observation_ids"])
        if not prefix_ids <= input_observation_ids:
            raise ValueError(f"hybrid prefix contains an unknown observation at frame {idx}")
        if any(observation_frame(obs_id) > idx for obs_id in prefix_ids):
            raise ValueError(f"hybrid prefix contains future observations at frame {idx}")
        for obj in snapshot["objects"]:
            object_ids = set(obj["observation_ids"])
            if not object_ids <= prefix_ids:
                raise ValueError(f"object {obj.get('hypothesis_id')} has non-prefix lineage at frame {idx}")
            if any(observation_frame(obs_id) > idx for obs_id in object_ids):
                raise ValueError(f"object {obj.get('hypothesis_id')} contains future lineage at frame {idx}")
            cloud_path = timeline_dir / obj["cloud_npz"]
            if not cloud_path.is_file() or sha256(cloud_path) != obj["cloud_sha256"]:
                raise ValueError(f"hybrid prefix cloud missing/hash mismatch at frame {idx}: {cloud_path}")
            with np.load(cloud_path) as archive:
                xyz = np.asarray(archive["xyz"], dtype=np.float64)
            if xyz.ndim != 2 or xyz.shape[1] != 3 or not np.isfinite(xyz).all():
                raise ValueError(f"hybrid prefix cloud has invalid XYZ at frame {idx}")
            if len(xyz) != int(obj["point_count"]):
                raise ValueError(f"hybrid prefix point count mismatch at frame {idx}")
            bounds = np.asarray(obj["bounds_minmax"], dtype=np.float64)
            actual_bounds = np.stack((xyz.min(axis=0), xyz.max(axis=0)))
            if bounds.shape != (2, 3) or not np.allclose(bounds, actual_bounds, atol=1e-5, rtol=0):
                raise ValueError(f"hybrid prefix bounds mismatch at frame {idx}")
            verified_object_clouds += 1
            total_points += len(xyz)
            point_z_min = min(point_z_min, float(xyz[:, 2].min()))
            point_z_max = max(point_z_max, float(xyz[:, 2].max()))
        verified_snapshots += 1

    return {
        "status": "prefix_geometry_provenance_and_xyz_validated",
        "timeline_manifest_sha256": sha256(timeline_manifest_path),
        "experiment_contract_sha256": sha256(experiment_path),
        "object_export_manifest_sha256": sha256(export_path),
        "prefix_snapshot_count": verified_snapshots,
        "prefix_object_cloud_count": verified_object_clouds,
        "prefix_xyz_point_count": total_points,
        "prefix_world_z_range_m": [point_z_min, point_z_max] if verified_object_clouds else None,
        "coordinate_frame": export["coordinate_frame"],
        "units": export["units"],
        "T_capture_world_from_cg_world": transform.tolist(),
        "floor_or_layer_assignment": "unavailable; finite world Z is not a verified floor/layer label",
        "membership": "unavailable; final hybrid export membership is not used",
    }


def analyze(run_dir: Path, timeline_dir: Path) -> dict[str, Any]:
    grid_dir = run_dir / "grid_trace"
    index = json.loads((grid_dir / "index.json").read_text())
    replay_manifest = json.loads((run_dir / "replay" / "manifest.json").read_text())
    locked_contract_path = run_dir / "scope_bridge_replay_contract.json"
    locked_contract = json.loads(locked_contract_path.read_text())
    capture_manifest_path = Path(replay_manifest["capture"]) / "capture_manifest.json"
    capture_manifest = json.loads(capture_manifest_path.read_text())
    timeline_manifest = json.loads((timeline_dir / "manifest.json").read_text())
    if index["frame_count"] != replay_manifest["frames"]:
        raise ValueError("grid trace and replay frame counts differ")
    if timeline_manifest["frame_count"] != index["frame_count"]:
        raise ValueError("hybrid timeline and native replay frame counts differ")
    if timeline_manifest["scene_id"] != capture_manifest["scene_id"]:
        raise ValueError("hybrid timeline and native replay scene IDs differ")
    if timeline_manifest["sequence_id"] != capture_manifest["sequence_id"]:
        raise ValueError("hybrid timeline and native replay sequence IDs differ")
    if locked_contract["status"] != "locked_before_native_replay":
        raise ValueError("Scope Bridge gate was not locked before the replay")
    if locked_contract["input_identity"]["capture_manifest_sha256"] != replay_manifest["capture_manifest_sha256"]:
        raise ValueError("replay capture hash differs from the locked pre-replay contract")
    if locked_contract["effective_configuration"]["pipeline_config_sha256"] != replay_manifest["config_sha256"]:
        raise ValueError("replay pipeline config differs from the locked pre-replay contract")
    if locked_contract["instrumentation"]["instrumented_binary_sha256"] != replay_manifest["m2_m3_binary_sha256"]:
        raise ValueError("replay M2/M3 binary differs from the locked instrumented build")
    locked_min_free_height = float(locked_contract["effective_configuration"]["m3_min_free_height_m"])
    hybrid_geometry_scope = validate_hybrid_prefix_geometry(
        timeline_dir,
        replay_manifest["capture_manifest_sha256"],
    )

    update_dir = grid_dir / "height_intervals" / "native_updates"
    prior_events: list[tuple[int, dict[str, Any]]] = []
    prefix_results: list[dict[str, Any]] = []
    trace_current_value_matches = 0
    trace_current_free_count = 0
    trace_current_envelope_matches = 0
    trace_current_envelope_checks = 0
    trace_total_cells = 0
    trace_multi_disjoint = 0
    trace_outside_grid_cells = 0
    trace_outside_grid_unknown_without_free = 0
    latest_update_revision = 0
    room_scope_projection_collapse_prefixes: dict[str, int] = {}
    missing_geometry_links = 0

    for frame_index in range(index["frame_count"]):
        meta_path = grid_dir / "frames" / f"{frame_index:06d}.json"
        array_path = grid_dir / "frames" / f"{frame_index:06d}.npz"
        meta = json.loads(meta_path.read_text())
        if int(meta["frame_index"]) != frame_index:
            raise ValueError(f"frame index mismatch at {meta_path}")
        trace_meta = meta["m3_height_interval_trace"]
        if int(trace_meta["map_revision"]) != int(meta["map_revision"]):
            raise ValueError(f"M3 trace revision mismatch at frame {frame_index}")
        if int(meta["m4"]["input_revision"]) != int(meta["map_revision"]):
            raise ValueError(f"M4 did not consume the M3 revision at frame {frame_index}")
        trace_path = grid_dir / trace_meta["path"]
        if not trace_path.is_file() or sha256(trace_path) != trace_meta["sha256"]:
            raise ValueError(f"missing or hash-mismatched interval trace at frame {frame_index}")
        trace = json.loads(trace_path.read_text())
        revision = int(meta["map_revision"])
        if int(trace["map_revision"]) != revision:
            raise ValueError(f"interval sidecar revision mismatch at frame {frame_index}")
        if trace["coordinate_frame"] != "world" or trace["units"] != "m":
            raise ValueError(f"unexpected coordinate contract at frame {frame_index}")
        if trace["updated"]:
            latest_update_revision = revision
        for cell in trace["cells"]:
            prior_events.append((revision, cell))
            trace_total_cells += 1
            intervals = cell["free_intervals_used_by_projection"]
            if len(intervals) >= 2 and intervals_are_disjoint(intervals):
                trace_multi_disjoint += 1

        with np.load(array_path) as archive:
            values = archive["values"].copy()
            z_floor = archive["z_floor"].copy()
            z_ceiling = archive["z_ceiling"].copy()
            m4_mask = archive["m4_preprocessed_mask"].copy()
        if values.shape != m4_mask.shape or values.shape != z_floor.shape or values.shape != z_ceiling.shape:
            raise ValueError(f"M3/M4 grid arrays are misaligned at frame {frame_index}")
        height, width = values.shape
        origin_x, origin_y = (float(v) for v in meta["origin_xy"])
        resolution = float(meta["resolution_m"])

        # Reproject only the interval ledger into the current M3 map's index
        # system. This is the same floor((position-offset)/resolution) mapping
        # used by the pinned Map2D positionToIndex; no epsilon is added.
        current_cells = remap_prefix_events(
            prior_events,
            revision,
            meta["origin_xy"],
            resolution,
            (height, width),
        )

        support_rows, support_cols = np.where(m4_mask)
        pixel_x = origin_x + support_cols.astype(np.float64) * resolution
        pixel_y = origin_y + support_rows.astype(np.float64) * resolution
        rooms_result: list[dict[str, Any]] = []
        overlap_count = np.zeros(len(support_rows), dtype=np.uint8)
        for room in meta["m8"]["rooms"]:
            attrs = room["attributes"]
            room_mask = in_polygon_grid(pixel_x, pixel_y, attrs)
            overlap_count += room_mask.astype(np.uint8)
            counts = {
                "m4_supported_cells": 0,
                "trace_present": 0,
                "trace_missing": 0,
                "no_surviving_free_interval": 0,
                "one_surviving_free_interval": 0,
                "multiple_surviving_free_intervals": 0,
                "multiple_disjoint_intervals_m3_free": 0,
                "confirmed_envelope_collapse_cells": 0,
                "single_interval_cells": 0,
            }
            samples: list[dict[str, Any]] = []
            for offset in np.flatnonzero(room_mask):
                row = int(support_rows[offset])
                col = int(support_cols[offset])
                counts["m4_supported_cells"] += 1
                current = current_cells.get((row, col))
                if current is None:
                    counts["trace_missing"] += 1
                    continue
                counts["trace_present"] += 1
                cell_revision, cell = current
                intervals = cell["free_intervals_used_by_projection"]
                n_intervals = len(intervals)
                if n_intervals == 0:
                    counts["no_surviving_free_interval"] += 1
                elif n_intervals == 1:
                    counts["one_surviving_free_interval"] += 1
                else:
                    counts["multiple_surviving_free_intervals"] += 1
                    if intervals_are_disjoint(intervals) and int(values[row, col]) == 0:
                        counts["multiple_disjoint_intervals_m3_free"] += 1
                        low, high = interval_envelope(intervals)
                        if (
                            np.isfinite(z_floor[row, col])
                            and np.isfinite(z_ceiling[row, col])
                            and abs(float(z_floor[row, col]) - low) <= 1e-5
                            and abs(float(z_ceiling[row, col]) - high) <= 1e-5
                        ):
                            counts["confirmed_envelope_collapse_cells"] += 1
                            if len(samples) < 3:
                                samples.append({
                                    "frame_index": frame_index,
                                    "frame_id": meta["frame_id"],
                                    "m3_revision": revision,
                                    "room_id": room["node_id"],
                                    "cell_row_col": [row, col],
                                    "world_xy_m": [float(pixel_x[offset]), float(pixel_y[offset])],
                                    "interval_source_revision": cell_revision,
                                    "free_intervals_used_for_projection": intervals,
                                    "m3_value": int(values[row, col]),
                                    "m3_z_floor_m": float(z_floor[row, col]),
                                    "m3_z_ceiling_m": float(z_ceiling[row, col]),
                                })
                if n_intervals == 1:
                    counts["single_interval_cells"] += 1
            room_id = str(room["node_id"])
            if counts["confirmed_envelope_collapse_cells"]:
                room_scope_projection_collapse_prefixes.setdefault(room_id, frame_index)
            rooms_result.append({
                "room_id": room_id,
                "room_observation_revision": attrs.get("observation_revision"),
                "anchor_region_id": attrs.get("anchor_region_id"),
                "polygon_area_m2": attrs.get("area_m2"),
                **counts,
                "all_supported_cells_have_one_interval": (
                    counts["m4_supported_cells"] > 0
                    and counts["trace_present"] == counts["m4_supported_cells"]
                    and counts["one_surviving_free_interval"] == counts["m4_supported_cells"]
                ),
                "collapse_examples": samples,
            })

        m8 = meta["m8"]
        m3_trace_link = meta["m3_height_interval_trace"]
        if not m3_trace_link.get("room_revisions"):
            missing_geometry_links += 1
        prefix_results.append({
            "frame_index": frame_index,
            "frame_id": meta["frame_id"],
            "m3_revision": revision,
            "m4_input_revision": meta["m4"]["input_revision"],
            "m5_accepted_observation_revision": m3_trace_link.get("m5_accepted_observation_revision"),
            "m8_snapshot_revision": m8.get("snapshot_revision"),
            "room_revision_links": m3_trace_link.get("room_revisions", []),
            "m4_preprocessed_support_cells": int(m4_mask.sum()),
            "room_polygon_count": len(rooms_result),
            "support_cells_in_room_polygons": int(sum(x["m4_supported_cells"] for x in rooms_result)),
            "overlap_support_cells": int(np.count_nonzero(overlap_count > 1)),
            "uncovered_support_cells": int(np.count_nonzero(overlap_count == 0)),
            "projection_collapse_cells": int(sum(x["confirmed_envelope_collapse_cells"] for x in rooms_result)),
            "rooms": rooms_result,
        })

        # Validate each current M3 update against its exact output grid. The
        # raw trace's z_floor_m/z_ceiling_m fields are deliberately not used:
        # the emitted values come from the NPZ M3 array joined by grid index.
        for cell in trace["cells"]:
            x, y = (float(v) for v in cell["world_xy_m"])
            col = math.floor((x - origin_x) / resolution)
            row = math.floor((y - origin_y) / resolution)
            if 0 <= row < height and 0 <= col < width:
                if int(values[row, col]) == int(cell["m3_value"]):
                    trace_current_value_matches += 1
                intervals = cell["free_intervals_used_by_projection"]
                if intervals:
                    trace_current_free_count += 1
                    trace_current_envelope_checks += 1
                    low, high = interval_envelope(intervals)
                    if (
                        np.isfinite(z_floor[row, col])
                        and np.isfinite(z_ceiling[row, col])
                        and abs(float(z_floor[row, col]) - low) <= 1e-5
                        and abs(float(z_ceiling[row, col]) - high) <= 1e-5
                    ):
                        trace_current_envelope_matches += 1
            else:
                trace_outside_grid_cells += 1
                if int(cell["m3_value"]) == -1 and not cell["free_intervals_used_by_projection"]:
                    trace_outside_grid_unknown_without_free += 1

    first_b = next((p for p in prefix_results if p["projection_collapse_cells"] > 0), None)
    strict_single_interval_rooms = [
        (p["frame_index"], room["room_id"])
        for p in prefix_results
        for room in p["rooms"]
        if room["all_supported_cells_have_one_interval"]
    ]
    return {
        "schema_version": 1,
        "kind": "scope_bridge_prefix_diagnostic",
        "run_dir": str(run_dir.resolve()),
        "timeline_dir": str(timeline_dir.resolve()),
        "input_hashes": {
            "replay_manifest_sha256": sha256(run_dir / "replay" / "manifest.json"),
            "grid_trace_index_sha256": sha256(grid_dir / "index.json"),
            "timeline_manifest_sha256": sha256(timeline_dir / "manifest.json"),
            "locked_pre_replay_contract_sha256": sha256(locked_contract_path),
            "capture_manifest_sha256": sha256(capture_manifest_path),
            "pipeline_config_sha256": replay_manifest["config_sha256"],
            "m2_m3_binary_sha256": replay_manifest["m2_m3_binary_sha256"],
            "m4_binary_sha256": replay_manifest["dude_binary_sha256"],
            "source_lock_sha256": replay_manifest["source_lock_sha256"],
            "resource_lock_sha256": replay_manifest["native_resource_binding"]["resource_manifest_sha256"],
            "hybrid_experiment_contract_sha256": hybrid_geometry_scope["experiment_contract_sha256"],
            "hybrid_object_export_manifest_sha256": hybrid_geometry_scope["object_export_manifest_sha256"],
        },
        "coordinate_contract": {
            "world_frame": "world",
            "units": "m",
            "world_up": "Z",
            "room_projection_plane": "XY",
            "grid_row_axis": "+Y",
            "grid_col_axis": "+X",
            "grid_index_transform": "Map2D.positionToIndex: floor((world_xy - origin_xy)/resolution), without epsilon",
            "M4_contour_transform": "world_xy = grid_origin_xy + contour_pixel_xy * resolution; source: native/dude/worker.cpp",
            "depth_and_pose_transform": "M1 cached metric axial RGB-D backprojection with T_W_Copt; Habitat basis conversion recorded in capture manifest",
        },
        "semantics": {
            "intervals": "free_intervals_used_by_projection are non-occupied intervals surviving locked minimum free-height pruning; they are not floor labels",
            "multi_interval_interpretation": "two or more disjoint intervals in the same XY cell show a vertical projection-collapse indicator only; furniture and occlusion remain possible causes",
            "m4_support": "true cells in the exact per-frame M4 preprocessed mask, intersected with that frame's M8 room polygon revision",
            "future_data_used": False,
            "membership_or_room_semantics_run": False,
        },
        "hybrid_object_geometry_scope": hybrid_geometry_scope,
        "instrumentation_checks": {
            "fixture_test_command": "PYTHONPATH=src SCOPE_BRIDGE_NATIVE_BINARY=/tmp/scope_bridge_native_build_20261016/scope_bridge_m2_m3_worker python3 -m pytest -q -p no:cacheprovider tests/test_scope_bridge_height_trace.py --basetemp /tmp/scope_bridge_height_fixture_tests_final",
            "fixture_tests_passed": 2,
            "trace_cells": trace_total_cells,
            "disjoint_multi_interval_trace_cells": trace_multi_disjoint,
            "trace_cells_outside_captured_m3_grid": trace_outside_grid_cells,
            "outside_grid_cells_unknown_without_free_intervals": trace_outside_grid_unknown_without_free,
            "current_m3_value_join_matches": trace_current_value_matches,
            "current_m3_free_interval_cells": trace_current_free_count,
            "current_m3_envelope_join_checks": trace_current_envelope_checks,
            "current_m3_envelope_join_matches": trace_current_envelope_matches,
            "direct_trace_z_floor_ceiling_fields": "invalid: trace reader called Map2D getHeight with world-coordinate doubles, implicitly selecting integer-index overload; diagnostic ignores these fields and joins intervals to exact M3 z arrays by Map2D index",
            "envelope_join_note": "current M3 z_floor/z_ceiling NPZ arrays are the exact values captured from Map2D index access; equality is checked to 1e-5 m",
            "post_replay_trace_field_fix": "The current instrumentation source fixes this diagnostic indexing error; the fix was verified on fixtures only and was not used for the one recorded cache replay. See scope_bridge_postflight.json.",
        },
        "scope_gate": {
            "locked_min_free_height_m": locked_min_free_height,
            "outcome": "C_scope_evidence_insufficient",
            "projection_collapse_indicator_confirmed": first_b is not None,
            "first_prefix_with_confirmed_projection_collapse": None if first_b is None else first_b["frame_index"],
            "first_prefix_frame_id": None if first_b is None else first_b["frame_id"],
            "room_ids_with_collapse_indicator": room_scope_projection_collapse_prefixes,
            "room_prefixes_passing_strict_single_interval_condition": len(strict_single_interval_rooms),
            "room_prefixes_checked": sum(len(p["rooms"]) for p in prefix_results),
            "room_revision_link_missing_count": missing_geometry_links,
            "gate_rule": "strict source threshold and criteria were written to scope_bridge_replay_contract.json before the replay",
            "not_a_single_floor_proof": "absence of a second interval cannot prove no unobserved or occluded elevation exists",
            "post_replay_interpretation": "a confirmed free-interval envelope collapse is a projection warning; it does not by itself establish incompatible floors because occupied geometry/furniture/occlusion can separate the free bands",
            "reason": "A spatial scope cannot be certified: disjoint free intervals with an M3 envelope occur inside room footprints, and no producer metadata identifies whether they represent furniture/occlusion or distinct level support. No room-prefix passed the strict one-surviving-interval condition; trace gaps are additionally present in some room masks.",
        },
        "prefixes": prefix_results,
    }


def write_envelope_join(run_dir: Path, output_path: Path) -> dict[str, Any]:
    """Join each source free interval to the exact corresponding M3 z arrays."""
    grid_dir = run_dir / "grid_trace"
    frame_index = json.loads((grid_dir / "index.json").read_text())
    written = 0
    exact = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as stream:
        for i in range(frame_index["frame_count"]):
            meta = json.loads((grid_dir / "frames" / f"{i:06d}.json").read_text())
            trace_meta = meta["m3_height_interval_trace"]
            trace_path = grid_dir / trace_meta["path"]
            if sha256(trace_path) != trace_meta["sha256"]:
                raise ValueError(f"interval trace hash mismatch at frame {i}")
            trace = json.loads(trace_path.read_text())
            with np.load(grid_dir / "frames" / f"{i:06d}.npz") as archive:
                values = archive["values"]
                z_floor = archive["z_floor"]
                z_ceiling = archive["z_ceiling"]
            origin_x, origin_y = (float(v) for v in meta["origin_xy"])
            resolution = float(meta["resolution_m"])
            for cell in trace["cells"]:
                intervals = cell["free_intervals_used_by_projection"]
                if not intervals:
                    continue
                x, y = (float(v) for v in cell["world_xy_m"])
                col = math.floor((x - origin_x) / resolution)
                row = math.floor((y - origin_y) / resolution)
                if not (0 <= row < values.shape[0] and 0 <= col < values.shape[1]):
                    raise ValueError(f"free trace cell outside M3 grid at frame {i}")
                low, high = interval_envelope(intervals)
                floor_value = float(z_floor[row, col])
                ceiling_value = float(z_ceiling[row, col])
                matches = (
                    np.isfinite(floor_value)
                    and np.isfinite(ceiling_value)
                    and abs(floor_value - low) <= 1e-5
                    and abs(ceiling_value - high) <= 1e-5
                )
                exact += int(matches)
                written += 1
                record = {
                    "frame_index": i,
                    "frame_id": meta["frame_id"],
                    "m3_revision": int(meta["map_revision"]),
                    "m4_input_revision": int(meta["m4"]["input_revision"]),
                    "m5_accepted_observation_revision": trace_meta.get("m5_accepted_observation_revision"),
                    "trace_path": trace_meta["path"],
                    "trace_sha256": trace_meta["sha256"],
                    "trace_cell_index_xy": cell["cell_index_xy"],
                    "m3_grid_row_col": [row, col],
                    "world_xy_m": cell["world_xy_m"],
                    "m3_value": int(values[row, col]),
                    "free_intervals_used_for_projection": intervals,
                    "m3_z_floor_m": floor_value if np.isfinite(floor_value) else None,
                    "m3_z_ceiling_m": ceiling_value if np.isfinite(ceiling_value) else None,
                    "envelope_matches_intervals": bool(matches),
                }
                stream.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
    return {
        "path": str(output_path),
        "sha256": sha256(output_path),
        "bytes": output_path.stat().st_size,
        "free_interval_cells": written,
        "envelope_matches": exact,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeline-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.run_dir, args.timeline_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result["derived_envelope_join"] = write_envelope_join(
        args.run_dir,
        args.output.with_name("height_interval_m3_envelope_join.jsonl"),
    )
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "output": str(args.output),
        "outcome": result["scope_gate"]["outcome"],
        "first_collapse_prefix": result["scope_gate"]["first_prefix_with_confirmed_projection_collapse"],
        "trace_cells": result["instrumentation_checks"]["trace_cells"],
        "envelope_matches": result["instrumentation_checks"]["current_m3_envelope_join_matches"],
        "prefixes": len(result["prefixes"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
