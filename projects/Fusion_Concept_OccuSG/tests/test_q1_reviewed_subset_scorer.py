from scripts.score_q1_00808_reviewed_subset import score


def test_q1_primary_pair_denominator_excludes_same_frame_table_pair():
    ids = [
        "hm3d-00808-k4-ag-v1-0000:det-0000",
        "hm3d-00808-k4-ag-v1-0001:det-0001",
        "hm3d-00808-k4-ag-v1-0002:det-0004",
        "hm3d-00808-k4-ag-v1-0002:det-0005",
        "hm3d-00808-k4-ag-v1-0004:det-0001",
        "hm3d-00808-k4-ag-v1-0005:det-0000",
        "hm3d-00808-k4-ag-v1-0006:det-0000",
    ]
    labels = dict(zip(ids, ["sofa_1", "chair_1", "table_1", "table_1", "chair_1", "chair_2", "sofa_1"]))
    tracks = dict(zip(ids, ["sofa", "chair1", "table", "table", "chair1", "chair2", "sofa"]))

    result = score(tracks, labels, ids)

    assert result["cross_frame_pair_population"] == 20
    assert result["same_frame_pairs_excluded"] == 1
    assert (result["TP"], result["FP"], result["FN"], result["TN"]) == (2, 0, 0, 18)
    assert result["false_merge_tracks"] == 0
    assert result["fragmented_identity_count"] == 0
    assert result["unassigned_observations"] == 0


def test_q1_revisit_exclusion_is_scoring_only_and_changes_denominator():
    ids = [
        "hm3d-00808-k4-ag-v1-0000:det-0000",
        "hm3d-00808-k4-ag-v1-0001:det-0001",
        "hm3d-00808-k4-ag-v1-0002:det-0004",
        "hm3d-00808-k4-ag-v1-0002:det-0005",
        "hm3d-00808-k4-ag-v1-0004:det-0001",
        "hm3d-00808-k4-ag-v1-0005:det-0000",
    ]
    labels = dict(zip(ids, ["sofa_1", "chair_1", "table_1", "table_1", "chair_1", "chair_2"]))
    tracks = dict(zip(ids, ["sofa", "chair1", "table", "table", "chair1", "chair2"]))

    result = score(tracks, labels, ids)

    assert result["cross_frame_pair_population"] == 14
    assert result["same_frame_pairs_excluded"] == 1
    assert (result["TP"], result["FP"], result["FN"], result["TN"]) == (1, 0, 0, 13)
