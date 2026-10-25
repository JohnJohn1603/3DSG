from standalone_sg.objects.duplicate_partition_review import compare_duplicate_partitions


def row(frame, did, duplicate_of=None):
    return {"frame_index": frame, "frame_id": f"f{frame}", "detection_id": did,
            "duplicate_of": duplicate_of}


def test_partition_comparison_ignores_alias_direction_but_reports_canonical_change():
    old = [row(0, "a"), row(0, "b", "a")]
    candidate = [row(0, "a", "b"), row(0, "b")]
    deltas = compare_duplicate_partitions(old, candidate)
    assert len(deltas) == 1
    assert deltas[0].kind == "canonical_only"
    assert deltas[0].old_groups == deltas[0].candidate_groups == (("a", "b"),)
    assert deltas[0].old_canonical_ids == ("a",)
    assert deltas[0].candidate_canonical_ids == ("b",)


def test_partition_comparison_reports_membership_merge_and_split():
    split = compare_duplicate_partitions(
        [row(1, "a"), row(1, "b")],
        [row(1, "a"), row(1, "b", "a")],
    )
    assert len(split) == 1
    assert split[0].kind == "membership_expansion_or_merge"
    assert split[0].old_groups == (("a",), ("b",))
    assert split[0].candidate_groups == (("a", "b"),)

    released = compare_duplicate_partitions(
        [row(2, "a"), row(2, "b", "a")],
        [row(2, "a"), row(2, "b")],
    )
    assert len(released) == 1
    assert released[0].kind == "split_or_released"


def test_same_detection_suffix_in_different_frames_stays_separate():
    old = [row(0, "frame-0:det-0001"), row(1, "frame-1:det-0001")]
    candidate = [row(0, "frame-0:det-0001"), row(1, "frame-1:det-0001", "other")]
    deltas = compare_duplicate_partitions(old, candidate)
    assert len(deltas) == 1
    assert deltas[0].frame_id == "f1"
    assert deltas[0].candidate_groups == (("frame-1:det-0001",),)
