import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artifacts/human_review/hm3d00802_main29_min3_v1_review_v1"


def test_blinded_package_has_six_hypotheses_and_full_observation_ids():
    data = json.loads((PACKAGE / "blinded_review.json").read_text())
    assert len(data["hypotheses"]) == 6
    all_ids = [obs for h in data["hypotheses"] for obs in h["observation_ids"]]
    assert len(all_ids) == len(set(all_ids))
    assert all(x.startswith("hm3d-00802-main-") and ":det-" in x for x in all_ids)
    assert data["blinding"]["hidden"] == [
        "predicted class", "semantic consensus", "room-label scores", "A-table scores", "existing room labels"
    ]


def test_blinded_payload_does_not_contain_prediction_values():
    data = json.loads((PACKAGE / "blinded_review.json").read_text())
    text = json.dumps(data, ensure_ascii=False)
    for leaked_value in ("chair", "couch", "tv", "car", "living_room", "kitchen", "bedroom", "bathroom"):
        # IDs/timeline/forms remain, but prediction values must not be in the payload.
        assert f'"{leaked_value}"' not in text
    assert data["room_review"]["room_id"] == "room:6"
    assert data["room_review"]["review_response"]["room_type"] is None


def test_review_assets_are_present_and_html_links_are_relative():
    data = json.loads((PACKAGE / "blinded_review.json").read_text())
    for h in data["hypotheses"]:
        for row in h["timeline"]:
            for rel in row["assets"].values():
                assert (PACKAGE / rel).is_file(), rel
    for v in data["room_review"]["viewpoints"]:
        assert (PACKAGE / v["asset"]).is_file()
    assert (PACKAGE / data["room_review"]["polygon_asset"]).is_file()
    html = (PACKAGE / "blinded_review.html").read_text()
    assert "../assets/" not in html
    assert "assets/hypotheses/" in html


def test_tv_membership_keeps_zero_support_and_layer_caveat():
    d = json.loads((PACKAGE / "tv_membership_diagnostic.json").read_text())
    assert d["status"] == "unassigned"
    assert sum(d["support_counts"].values()) == 0
    assert d["outside_fraction"] == 1.0
    provenance = d["layer_provenance"]
    assert provenance["detection_2d_layer_field_present"] is False
    assert provenance["contract_default_layer_id"] == "floor_0"
    assert "not independently proven" in provenance["verification"]


def test_package_manifest_is_complete():
    manifest = json.loads((PACKAGE / "package_manifest.json").read_text())
    assert manifest["review_is_blinded"] is True
    assert manifest["no_pipeline_rerun"] is True
    for item in manifest["files"]:
        assert (PACKAGE / item["path"]).is_file()


def test_user_review_response_preserves_mixed_h01_and_room_pending():
    d = json.loads((PACKAGE / "human_review_response_v1.json").read_text())
    h01 = next(x for x in d["hypotheses"] if x["review_id"] == "H01")
    assert h01["same_physical_object"] is False
    assert h01["physical_identity_tokens"] == ["armchair_1", "armchair_2"]
    assert h01["observation_reviews"]["hm3d-00802-main-0016:det-0001"]["mask_purity"] == "not_pure_wrong_object_in_mask"
    assert next(x for x in d["hypotheses"] if x["review_id"] == "H05")["physical_identity_token"] == "mirror_1"
    assert d["room_review"]["status"] == "confirmed"
    assert d["room_review"]["room_type"] == "living_room"
