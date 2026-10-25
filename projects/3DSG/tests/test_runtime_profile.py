from exploration.runtime_profile import m2_transport_metadata


def test_m2_transport_metadata_separates_selection_from_worker_evidence() -> None:
    metadata = m2_transport_metadata(enabled=True, requested="binary")

    assert metadata == {
        "occusg_m2_transport_requested": "binary",
        "occusg_m2_transport_effective": None,
        "occusg_m2_transport_effective_source": "per_frame_worker_diagnostics",
    }


def test_m2_transport_metadata_is_empty_when_graph_pipeline_is_disabled() -> None:
    assert m2_transport_metadata(enabled=False, requested="binary") == {
        "occusg_m2_transport_requested": None,
        "occusg_m2_transport_effective": None,
        "occusg_m2_transport_effective_source": None,
    }
