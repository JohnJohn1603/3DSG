from __future__ import annotations

import numpy as np

from standalone_sg.regions.dude_client import DuDeWorkerClient
from standalone_sg.regions.pipeline import RegionUpdateProcessor
from test_m4_dude import BIN, grid, shape_fixture


def test_repeated_update_reports_source_reuse_branch_and_preserved_geometry():
    source = shape_fixture("rectangle")
    with DuDeWorkerClient(BIN) as worker:
        first, first_obs = worker.integrate(grid(source, 1), timestamp_s=1.0)
        second, second_obs = worker.integrate(grid(source.copy(), 2), timestamp_s=2.0)
    assert first["branch"] == "accept_incremental"
    assert second["branch"] == "accept_reuse_adjusted_no_differential"
    assert second["diagnostics"]["stale_or_reused"]
    assert len(first_obs) == len(second_obs) == 1
    np.testing.assert_allclose(first_obs[0].polygon.outer, second_obs[0].polygon.outer)


def test_demo_backend_rejections_preserve_m5_evidence_until_source_accepts_again():
    height = width = 160
    unknown = np.full((height, width), -1, dtype=np.int8)
    partial = unknown.copy(); partial[25:135, 18:85] = 0
    expanded = unknown.copy(); expanded[18:142, 12:98] = 0
    doorway = expanded.copy(); doorway[18:142, 110:150] = 0; doorway[70:90, 95:116] = 0
    frames = [("t0_unknown", unknown), ("t1_partial_free", partial),
              ("t2_room_expanded", expanded), ("t3_doorway_revealed", doorway),
              ("t4_recovery", partial)]
    processor = RegionUpdateProcessor()
    outputs = []
    states = []
    with DuDeWorkerClient(BIN) as worker:
        for revision, (name, values) in enumerate(frames, 1):
            outputs.append(worker.update_regions(
                processor, grid(values, revision, origin=(-4.0, -2.0), resolution=0.05),
                timestamp_s=float(revision),
            ))
            states.append((processor.tracker.accepted_update_index,
                           processor.guard.last_published_regions))

    (r0, m0), (r1, m1), (r2, m2), (r3, m3), (r4, m4) = outputs
    assert r0["status"] == "success" and not m0.accepted
    assert r1["branch"] == "accept_incremental" and m1.accepted
    accepted_regions = m1.current_regions
    accepted_tracks = m1.retained_tracks

    for response, result, request_revision in ((r2, m2, 3), (r3, m3, 4)):
        assert response["status"] == "rejected"
        assert response["branch"] == "reject_expanded_empty_preserve_previous"
        assert response["m5_observation_policy"] == "stale_snapshot_only"
        assert response["input_revision"] == request_revision
        assert response["accepted_observation_revision"] == 2
        assert result.request_revision == request_revision
        assert result.accepted_observation_revision == 2
        assert result.stale and not result.accepted and result.m5_result is None
        assert result.current_regions == accepted_regions
        assert result.retained_tracks == accepted_tracks
        assert states[request_revision - 1][0] == states[1][0]
        assert states[request_revision - 1][1] == states[1][1]
        assert result.retained_tracks[0].last_seen_s == 2.0
        assert result.retained_tracks[0].last_source_grid_revision == 2
        assert result.retained_tracks[0].miss_count == 0

    assert r4["status"] == "success"
    assert r4["branch"] == "accept_reuse_adjusted_no_differential"
    assert r4["m5_observation_policy"] == "candidate_update"
    assert m4.accepted and m4.accepted_observation_revision == 5
    assert m4.retained_tracks[0].persistent_id == accepted_tracks[0].persistent_id
    assert m4.retained_tracks[0].last_seen_s == 5.0
    assert states[4][0] == states[1][0] + 1
