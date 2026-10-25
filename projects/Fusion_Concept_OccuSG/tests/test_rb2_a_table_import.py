import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("import_rb2_a_tables", ROOT / "scripts/import_rb2_a_tables.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_canonical_aliases_are_explicit_and_stable():
    assert MODULE.canonical_category("couch") == "sofa"
    assert MODULE.canonical_category(" dining table ") == "dining_table"
    assert MODULE.canonical_category("laptop") == "laptop"


def test_import_artifact_and_static_diagnostic_are_model_free():
    output = ROOT / "artifacts/room_belief/rb2_local_import_20261015"
    manifest = json.loads((output / "import_manifest.json").read_text())
    quality = json.loads((output / "a_table_quality.json").read_text())
    static = json.loads((output / "static_room_diagnostic.json").read_text())
    assert manifest["validation_status"] == "pass"
    assert quality["contract"]["status"] == "pass"
    assert all(len([r for r in quality["summary"] if r["variant"] == v]) == 11 for v in MODULE.VARIANTS)
    assert static["rooms"][0]["status"] == "abstain"
    assert "insufficient_support" in static["rooms"][0]["reason"]

