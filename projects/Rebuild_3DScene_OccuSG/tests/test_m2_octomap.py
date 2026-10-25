import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "build/native-m2/occupancy/standalone_sg_octomap_fixture_runner"
M2_BINARY = Path(os.environ.get("STANDALONE_SG_OCTOMAP_BINARY", DEFAULT_BINARY))
REFERENCE_BINARY = M2_BINARY
FIXTURE = ROOT / "tests/fixtures/m2_octomap_basic.scan"
GOLDEN = ROOT / "tests/fixtures/m2_octomap_basic.golden.json"


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap runner has not been built")
def test_octomap_scan_updates_match_golden_fixture() -> None:
    actual = json.loads(
        subprocess.check_output([str(M2_BINARY), "--fixture", str(FIXTURE)], text=True)
    )
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual["resolution"] == expected["resolution"]
    assert len(actual["updates"]) == 2

    for actual_update, expected_update in zip(actual["updates"], expected["updates"]):
        assert actual_update["revision"] == expected_update["revision"]
        assert actual_update["free_keys"] == expected_update["free_keys"]
        assert actual_update["occupied_keys"] == expected_update["occupied_keys"]
        assert len(actual_update["observations"]) == len(expected_update["observations"])
        for actual_observation, expected_observation in zip(
            actual_update["observations"], expected_update["observations"]
        ):
            assert actual_observation["key"] == expected_observation["key"]
            assert actual_observation["occupied"] is expected_observation["occupied"]
            assert actual_observation["log_odds"] == pytest.approx(expected_observation["log_odds"], abs=2e-6)
            assert actual_observation["probability"] == pytest.approx(expected_observation["probability"], abs=2e-6)

    # The second scan contains an endpoint beyond max range and a no-return.
    # Both must add free keys without creating a new occupied endpoint.
    assert actual["updates"][1]["occupied_keys"] == []
    # Previously occupied voxels remain occupied while their evidence decays.
    assert actual["updates"][1]["observations"][2]["occupied"] is True


@pytest.mark.skipif(not REFERENCE_BINARY.exists(), reason="M2 reference runner has not been built")
def test_octomap_key_sets_match_independent_insert_scan_harness() -> None:
    actual = json.loads(
        subprocess.check_output([str(M2_BINARY), "--fixture", str(FIXTURE)], text=True)
    )
    reference = json.loads(
        subprocess.check_output(
            [str(REFERENCE_BINARY), "--reference", "--fixture", str(FIXTURE)], text=True
        )
    )
    for actual_update, reference_update in zip(actual["updates"], reference["updates"]):
        assert actual_update["free_keys"] == reference_update["free_keys"]
        assert actual_update["occupied_keys"] == reference_update["occupied_keys"]


@pytest.mark.skipif(not REFERENCE_BINARY.exists(), reason="M2 reference runner has not been built")
@pytest.mark.parametrize(
    "fixture_name",
    ["m2_octomap_edgecases.scan", "m2_octomap_edgecases_reversed.scan"],
)
def test_edge_case_key_sets_match_independent_insert_scan_harness(fixture_name: str) -> None:
    fixture = ROOT / "tests/fixtures" / fixture_name
    actual = json.loads(
        subprocess.check_output([str(M2_BINARY), "--fixture", str(fixture)], text=True)
    )
    reference = json.loads(
        subprocess.check_output(
            [str(REFERENCE_BINARY), "--reference", "--fixture", str(fixture)], text=True
        )
    )
    assert actual["updates"][0]["free_keys"] == reference["updates"][0]["free_keys"]
    assert actual["updates"][0]["occupied_keys"] == reference["updates"][0]["occupied_keys"]

    if fixture_name.endswith("edgecases_reversed.scan"):
        original = ROOT / "tests/fixtures/m2_octomap_edgecases.scan"
        original_actual = json.loads(
            subprocess.check_output([str(M2_BINARY), "--fixture", str(original)], text=True)
        )
        assert actual["updates"][0]["free_keys"] == original_actual["updates"][0]["free_keys"]
        assert actual["updates"][0]["occupied_keys"] == original_actual["updates"][0]["occupied_keys"]


@pytest.mark.skipif(not REFERENCE_BINARY.exists(), reason="M2 reference runner has not been built")
def test_resolution005_fixture_matches_reference_key_sets() -> None:
    fixture = ROOT / "tests/fixtures/m2_octomap_resolution005.scan"
    actual = json.loads(
        subprocess.check_output([str(M2_BINARY), "--fixture", str(fixture)], text=True)
    )
    reference = json.loads(
        subprocess.check_output(
            [str(REFERENCE_BINARY), "--reference", "--fixture", str(fixture)], text=True
        )
    )
    assert actual["resolution"] == pytest.approx(0.05)
    assert actual["updates"][0]["free_keys"] == reference["updates"][0]["free_keys"]
    assert actual["updates"][0]["occupied_keys"] == reference["updates"][0]["occupied_keys"]
    assert actual["updates"][0]["occupied_keys"]
