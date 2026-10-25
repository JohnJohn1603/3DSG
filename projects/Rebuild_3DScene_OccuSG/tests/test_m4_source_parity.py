from __future__ import annotations

import json
import subprocess

import pytest
import numpy as np

from standalone_sg.regions.dude_client import DuDeWorkerClient
from test_m4_dude import grid, shape_fixture


WORKER = "build/native-m4-dude/occusg_dude_worker"
ORACLE = "build/native-m4-dude-reference/occusg_dude_source_reference"


def canonical_ring(ring):
    points = [tuple(round(float(v), 5) for v in p) for p in ring]
    if points and points[0] == points[-1]:
        points.pop()
    rotations = [tuple(points[i:] + points[:i]) for i in range(len(points))]
    reverse = list(reversed(points))
    rotations.extend(tuple(reverse[i:] + reverse[:i]) for i in range(len(reverse)))
    return min(rotations) if rotations else ()


def test_worker_parity_with_direct_pinned_source_oracle_on_fixed_fixtures():
    for revision, fixture in enumerate(("rectangle", "l_shape", "doorway", "obstacle", "unknown"), 1):
        reference = subprocess.run([ORACLE, fixture], check=True, text=True,
                                   capture_output=True, timeout=60)
        expected = json.loads(reference.stdout)
        with DuDeWorkerClient(WORKER) as worker:
            actual, _ = worker.integrate(grid(shape_fixture(fixture), revision, origin=(-1.0, 2.0)),
                                         timestamp_s=float(revision))
        assert actual["branch"] == expected["branch"], fixture
        assert len(actual["regions"]) == len(expected["regions"]), fixture
        for got, want in zip(actual["regions"], expected["regions"]):
            assert canonical_ring(got["outer"]) == canonical_ring(want["outer"]), fixture
            assert got["area_m2"] == pytest.approx(want["area_m2"], abs=1e-8), fixture
            assert got["adjacent_raw_ids"] == want["adjacent_raw_ids"], fixture


def _assert_frame_parity(actual, expected, label):
    assert actual["branch"] == expected["branch"], label
    assert actual["status"] == expected["status"], label
    assert len(actual["regions"]) == len(expected["regions"]), label
    for got, want in zip(actual["regions"], expected["regions"]):
        assert canonical_ring(got["outer"]) == canonical_ring(want["outer"]), label
        assert got["area_m2"] == pytest.approx(want["area_m2"], abs=1e-8), label
        assert got["adjacent_raw_ids"] == want["adjacent_raw_ids"], label
    np.testing.assert_array_equal(
        np.asarray(actual["diagnostics"]["preprocessed_mask"], dtype=bool),
        np.asarray(expected["diagnostics"]["preprocessed_mask"], dtype=bool),
        err_msg=f"preprocessed mask mismatch: {label}",
    )

def test_live_worker_matches_live_direct_source_for_demo_sequence_and_fresh_final():
    height = width = 160
    unknown = np.full((height, width), -1, dtype=np.int8)
    partial = unknown.copy(); partial[25:135, 18:85] = 0
    expanded = unknown.copy(); expanded[18:142, 12:98] = 0
    doorway = expanded.copy(); doorway[18:142, 110:150] = 0; doorway[70:90, 95:116] = 0
    frames = [unknown, partial, expanded, doorway, partial]
    reference = subprocess.run([ORACLE, "demo_sequence"], check=True, text=True,
                               capture_output=True, timeout=90)
    expected = [json.loads(line) for line in reference.stdout.splitlines()]
    assert len(expected) == len(frames) == 5
    actual = []
    with DuDeWorkerClient(WORKER) as worker:
        for revision, values in enumerate(frames, 1):
            response, _ = worker.integrate(
                grid(values, revision, origin=(-4.0, -2.0), resolution=0.05),
                timestamp_s=float(revision), capture_preprocessed_mask=True,
            )
            actual.append(response)
    for revision, (got, want) in enumerate(zip(actual, expected), 1):
        assert got["input_revision"] == want["input_revision"] == revision
        assert got["diagnostics"]["preprocessed_free_pixels"] == want["diagnostics"]["preprocessed_free_pixels"]
        _assert_frame_parity(got, want, f"demo revision {revision}")

    fresh = subprocess.run([ORACLE, "demo_fresh_final"], check=True, text=True,
                           capture_output=True, timeout=90)
    fresh_final = json.loads(fresh.stdout)
    assert actual[3]["status"] == expected[3]["status"] == "rejected"
    assert fresh_final["status"] == "success"
    assert actual[3]["diagnostics"]["preprocessed_free_pixels"] == fresh_final["diagnostics"]["preprocessed_free_pixels"]
    assert canonical_ring(actual[3]["regions"][0]["outer"]) != canonical_ring(fresh_final["regions"][0]["outer"])

def test_live_doorway_reveal_updates_geometry_and_adjacency_with_source_parity():
    height = width = 180
    hidden = np.full((height, width), -1, dtype=np.int8); hidden[40:140, 15:90] = 0
    revealed = hidden.copy(); revealed[50:130, 110:175] = 0; revealed[85:95, 90:111] = 0
    reference = subprocess.run([ORACLE, "doorway_sequence"], check=True, text=True,
                               capture_output=True, timeout=90)
    expected = [json.loads(line) for line in reference.stdout.splitlines()]
    actual = []
    with DuDeWorkerClient(WORKER) as worker:
        for revision, values in enumerate((hidden, revealed), 1):
            response, _ = worker.integrate(
                grid(values, revision, origin=(-4.0, -2.0), resolution=0.05),
                timestamp_s=float(revision), capture_preprocessed_mask=True,
            )
            actual.append(response)
    assert [r["branch"] for r in actual] == ["accept_incremental", "accept_incremental"]
    for revision, (got, want) in enumerate(zip(actual, expected), 1):
        _assert_frame_parity(got, want, f"doorway revision {revision}")
    assert len(actual[0]["regions"]) == 1
    assert len(actual[1]["regions"]) == 2
    assert [r["adjacent_raw_ids"] for r in actual[1]["regions"]] == [[1], [0]]
    assert canonical_ring(actual[0]["regions"][0]["outer"]) not in {
        canonical_ring(r["outer"]) for r in actual[1]["regions"]
    }
