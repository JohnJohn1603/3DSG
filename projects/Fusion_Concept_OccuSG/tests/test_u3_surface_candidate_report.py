from scripts.run_u3_surface_support_candidate import _write_report


def test_report_without_evaluator_ledger_shows_unavailable_not_zero(tmp_path):
    modes = ("source", "geometry-only-dedup-v2", "accumulated-surface-support")
    manifest = {
        "input_run_id": "fixture-run",
        "raw_observation_count": 3,
        "evaluator_covered_observations": 0,
        "evaluator_ledger": None,
        "metrics": {mode: {
            "status": "unavailable_no_evaluator_ledger",
            "pairwise_tp": None,
            "pairwise_fp": None,
            "pairwise_fn": None,
            "association_precision": None,
            "association_recall": None,
            "association_f1": None,
        } for mode in modes},
        "comparison_runtime_memory": {},
        "causal_replay_runtime_seconds": 0.1,
        "peak_process_rss_mib": 10.0,
        "continuity_matches": 0,
        "deferred_count": 0,
        "dedup_alias_count": 0,
        "total_accumulated_voxels": 0,
        "relation_change_summaries": {},
        "gate_cases": [],
        "changed_decision_count": 0,
        "preflight_runtime_seconds": 0.0,
        "total_runtime_seconds": 0.1,
        "serialized_surface_snapshot_bytes": 0,
    }
    report = tmp_path / "report.md"

    _write_report(report, manifest, [], [])

    text = report.read_text()
    assert "evaluator identity coverage=unavailable (no evaluator ledger)" in text
    assert "N/A (unavailable_no_evaluator_ledger)" in text
    assert "0/0/0" not in text
    assert "Cross-frame pair counts and evaluator coverage are unavailable" in text
