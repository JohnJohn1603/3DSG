from __future__ import annotations

import json
import hashlib
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "artifacts/human_review/hm3d00802_main29_640_p2_review_v1"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def test_640_review_package_covers_exact_locked_population_blindly():
    population = json.loads((PACKAGE / "population_manifest.json").read_text())
    assert population["schema"] == "hm3d-p2-640-development-review/v1"
    assert population["review_type"] == "development_not_holdout"
    assert population["run_id"] == "hm3d00802_main29_640_quality_v1_attempt2"
    observations = population["observations"]
    ids = [row["observation_id"] for row in observations]
    assert len(ids) == 75 and len(set(ids)) == 75
    assert population["population"]["observation_count"] == 75
    assert population["capture"]["resolution_px"] == [640, 480]
    assert population["no_prior_annotations_reused"] is True
    assert "prohibited" in population["prior_160x120_annotation_join"]
    assert population["blinding"]["hidden"] == [
        "predicted class", "hypothesis IDs/grouping", "retained/filtered status",
        "room scores", "semantic consensus",
    ]


def test_640_review_assets_are_unique_hash_locked_and_manifested():
    population = json.loads((PACKAGE / "population_manifest.json").read_text())
    all_paths = []
    for row in population["observations"]:
        assert set(row["panels"]) == {"rgb", "overlay", "crop", "mask"}
        assert len(row["source_hashes"]["rgb_npy_sha256"]) == 64
        assert len(row["source_hashes"]["p2_mask_sha256"]) == 64
        for panel in row["panels"].values():
            path = PACKAGE / panel["path"]
            assert path.is_file()
            assert _sha256(path) == panel["sha256"]
            all_paths.append(panel["path"])
    assert len(all_paths) == 300 and len(set(all_paths)) == 300
    validation = json.loads((PACKAGE / "package_validation.json").read_text())
    assert validation["status"] == "pass"
    assert all(validation["checks"].values())
    assert validation["validated_observations"] == 75
    assert validation["validated_panel_files"] == 300


def test_640_gallery_is_paged_resumable_and_has_independent_fields():
    html = (PACKAGE / "review_gallery.html").read_text().lower()
    for required in ("previous page", "next page", "compare selected", "save / export draft json",
                    "continue from json", "identity_token", "class_status", "class_label",
                    "mask_purity", "development review", "not a holdout"):
        assert required in html
    for hidden in ("class_raw", "class_canonical", "score_raw", "hypothesis_", "retained", "filtered",
                   "room_scores", "consensus_winner"):
        assert hidden not in html


def test_640_annotation_template_has_one_unreviewed_row_per_observation():
    template = json.loads((PACKAGE / "annotation_template.json").read_text())
    population = json.loads((PACKAGE / "population_manifest.json").read_text())
    assert template["schema"] == "hm3d-p2-640-development-review-response/v1"
    assert template["population_manifest_sha256"] == _sha256(PACKAGE / "population_manifest.json")
    rows = template["observations"]
    assert len(rows) == 75
    assert [row["observation_id"] for row in rows] == [row["observation_id"] for row in population["observations"]]
    for row in rows:
        assert row["identity_status"] == row["class_status"] == row["mask_purity"] == "unreviewed"
        assert row["identity_token"] == row["class_label"] == ""


def test_640_rgb_panels_match_source_content_at_first_middle_and_last_observation():
    population = json.loads((PACKAGE / "population_manifest.json").read_text())
    source_root = ROOT / "artifacts/conceptgraphs_hybrid_runs" / population["run_id"]
    for row in (population["observations"][0], population["observations"][37], population["observations"][-1]):
        frame_index = row["frame_index"]
        capture = json.loads((source_root / "capture/capture_manifest.json").read_text())
        cap_frame = capture["frames"][frame_index]
        source = source_root / "capture" / cap_frame["frame_dir"] / "rgb.npy"
        import numpy as np
        expected = np.load(source, allow_pickle=False)[..., :3].astype("uint8")
        actual = np.asarray(Image.open(PACKAGE / row["panels"]["rgb"]["path"]).convert("RGB"))
        assert np.array_equal(actual, expected)


def test_package_manifest_lists_every_output_file_and_hash():
    manifest = json.loads((PACKAGE / "package_manifest.json").read_text())
    assert manifest["status"] == "ready_for_human_review"
    assert manifest["file_count"] == len(manifest["files"])
    for item in manifest["files"]:
        path = PACKAGE / item["path"]
        assert path.is_file()
        assert path.stat().st_size == item["bytes"]
        assert _sha256(path) == item["sha256"]
