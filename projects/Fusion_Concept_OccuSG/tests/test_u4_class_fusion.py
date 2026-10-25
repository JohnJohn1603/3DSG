from standalone_sg.semantics.u4_class_fusion import CausalClassEvidence, ClassObservation


def obs(i, cls, confidence, view, frame):
    return ClassObservation(f"d{i}", frame, float(frame), cls, confidence, view)


def test_repeated_observations_in_same_view_bin_are_capped():
    state = CausalClassEvidence()
    for i in range(20):
        row = state.add(obs(i, "chair", 0.9, "az00_el03", i))
    assert row["class_status"] == "tentative"
    assert row["resolved_class"] is None
    assert row["independent_view_bins_by_class"] == {"chair": 1}


def test_initial_wrong_class_can_be_corrected_after_independent_views():
    state = CausalClassEvidence()
    state.add(obs(0, "bed", 0.7, "az00_el03", 0))
    state.add(obs(1, "table", 0.8, "az01_el03", 1))
    final = state.add(obs(2, "table", 0.8, "az02_el03", 2))
    assert final["initial_class"] == "bed"
    assert final["resolved_class"] == "table"
    assert final["class_status"] == "confirmed"
    assert [e["event"] for e in state.events].count("class_correction") == 1


def test_confirmed_class_can_be_deconfirmed_and_replaced():
    state = CausalClassEvidence()
    state.add(obs(0, "chair", 0.7, "az00_el03", 0))
    state.add(obs(1, "chair", 0.7, "az01_el03", 1))
    assert state.resolved_class == "chair"
    state.add(obs(2, "sofa", 0.95, "az02_el03", 2))
    state.add(obs(3, "sofa", 0.95, "az03_el03", 3))
    row = state.add(obs(4, "sofa", 0.95, "az04_el03", 4))
    assert row["resolved_class"] == "sofa"
    assert any(e["event"] == "deconfirmed" and e["class"] == "chair" for e in state.events)
    assert any(e["event"] == "class_correction" and e["to"] == "sofa" for e in state.events)


def test_conflicting_views_do_not_force_a_class():
    state = CausalClassEvidence()
    state.add(obs(0, "chair", 0.8, "az00_el03", 0))
    row = state.add(obs(1, "sofa", 0.8, "az01_el03", 1))
    assert row["resolved_class"] is None
    assert row["class_status"] == "conflicted"


def test_noncausal_order_is_rejected():
    state = CausalClassEvidence()
    state.add(obs(0, "chair", 0.8, "az00_el03", 2))
    try:
        state.add(obs(1, "chair", 0.8, "az01_el03", 1))
    except ValueError as exc:
        assert "causal order" in str(exc)
    else:
        raise AssertionError("non-causal observation order should fail")
