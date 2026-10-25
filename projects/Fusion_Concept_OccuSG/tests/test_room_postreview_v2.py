import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/human_review/hm3d00802_main29_min3_v1_review_v1/postreview_v2"


def test_room6_variant_scoring_is_separate_and_unmodified():
    data = json.loads((OUT / "room6_scoring.json").read_text())
    assert {x["variant"] for x in data["variants"]} == {
        "mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral"
    }
    by = {x["variant"]: x for x in data["variants"]}
    assert by["mean_no_neutral"]["candidate_label"] == "living_room"
    assert by["sum_no_neutral"]["commit_label"] == "living_room"
    assert by["mean_neutral"]["abstained"]
    assert by["sum_neutral"]["abstained"]
    assert {"chair", "sofa"} == set(by["mean_no_neutral"]["eligible_categories"])
    assert len(data["sensitivity_analysis"]) == 12


def test_membership_diagnostics_preserve_review_conflicts():
    data = json.loads((OUT / "membership_h04_h05.json").read_text())
    by = {x["hypothesis_id"]: x for x in data}
    h04 = by["hypothesis_0415c816d24c0a17"]
    h05 = by["hypothesis_d579ba788876c64a"]
    assert h04["membership"]["support_counts"]["room:6"] == 20
    assert h04["membership"]["outside_fraction"] > 0
    assert h05["membership"]["support_fraction"] == 0
    assert h05["membership"]["outside_fraction"] == 1.0
    assert h04["coordinate_contract"]["room_floor_metadata_present"] is False
    assert h04["coordinate_contract"]["point_frame_units"].endswith("Z-up; room polygons use XY")


def test_postreview_manifest_lists_outputs():
    manifest = json.loads((OUT / "postreview_manifest.json").read_text())
    paths = {x["path"] for x in manifest["files"]}
    assert "postreview_report_v2.md" in paths
    assert "membership_h04_h05.json" in paths
    assert any(p.endswith("H04/cloud_room_polygons.svg") for p in paths)
    assert any(p.endswith("H05/cloud_room_polygons.svg") for p in paths)
