from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from report_room_belief_readiness import build_report  # noqa: E402


HYBRID_RUN = ROOT / "artifacts/conceptgraphs_hybrid_runs/hybrid_u6_00808_seed20261014_v3"


def test_readiness_primary_membership_is_recomputed_greedy_geometry_not_legacy_adapter():
    report = build_report(HYBRID_RUN)
    assert report["membership_scope"]["membership_policy_id"] == "rb1_greedy_voxel_support_v1"
    assert report["membership_scope"]["status"] == "verified_single_observed_floor_scope"
    assert report["input_scope"]["pre_filter_hypotheses"] == 9

    objects = {item["hypothesis_id"]: item for item in report["objects"]}
    couch = objects["hypothesis_40d3b4a3e1033fd2"]
    assert couch["membership"]["status"] == "assigned"
    assert couch["membership"]["room_id"] == "room:1"
    assert couch["membership"]["support_counts"] == {"room:1": 6558}
    assert couch["membership"]["support_fraction_denominator"] == 6915
    assert couch["membership"]["support_fraction"] == 6558 / 6915
    assert couch["membership"]["outside_fraction"] == 357 / 6915
    assert couch["membership"]["runner_up_fraction"] == 0.0
    assert couch["membership"]["runner_up_room_ids"] == []
    assert couch["membership"]["tied"] is False
    assert couch["legacy_adapter_membership"]["status"] == "ambiguous"
    assert couch["room_evidence_eligible"] is False

    assert all(item["membership"]["status"] == "assigned" for item in objects.values())
    assert all(item["membership"]["room_id"] == "room:1" for item in objects.values())
    assert all(item["membership"]["tied"] is False for item in objects.values())

