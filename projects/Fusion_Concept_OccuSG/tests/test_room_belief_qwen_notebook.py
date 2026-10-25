from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = ROOT / "artifacts/room_belief/rb2_qwen_inputs_v2"
NOTEBOOK = ROOT / "notebooks/room_belief_qwen3_rb2_v2.ipynb"
ZIP_PATH = ROOT / "artifacts/room_belief/room_belief_qwen_rb2_inputs_v2.zip"


def test_data_free_package_hashes_and_vocabulary() -> None:
    manifest = json.loads((INPUT_ROOT / "input_manifest.json").read_text())
    assert manifest["contains_scene_data"] is False
    assert manifest["contains_annotations"] is False
    assert manifest["contains_model_weights"] is False
    for name, expected in manifest["files"].items():
        data = (INPUT_ROOT / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected["sha256"]
        assert len(data) == expected["bytes"]

    ontology = json.loads((INPUT_ROOT / "ontology_v1.json").read_text())
    categories = json.loads((INPUT_ROOT / "object_categories_v1.json").read_text())
    assert len(ontology["rooms"]) == 8
    assert len(categories["category_strings"]) == 11
    assert all("review" not in name.lower() and "gt" not in name.lower() for name in categories["category_strings"])
    assert not any(key in categories for key in ("gt_labels", "ground_truth", "annotations", "room_labels"))


def test_zip_contains_only_packaged_metadata_and_code() -> None:
    with zipfile.ZipFile(ZIP_PATH) as archive:
        names = set(archive.namelist())
    assert names == {
        "environment_candidate_lock.json",
        "input_manifest.json",
        "object_categories_v1.json",
        "ontology_v1.json",
        "qwen_room_scorer.py",
        "scoring_contract_v1.json",
    }
    notebook = json.loads(NOTEBOOK.read_text())
    notebook_source = "\n".join("".join(cell.get("source", [])) for cell in notebook["cells"])
    assert hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest() in notebook_source


def test_notebook_code_compiles_and_approved_stages_are_explicitly_gated() -> None:
    notebook = json.loads(NOTEBOOK.read_text())
    code_cells = [cell["source"] for cell in notebook["cells"] if cell["cell_type"] == "code"]
    assignments: dict[str, object] = {}
    for source in code_cells:
        source_text = "".join(source)
        compile(source_text, "<rb2-notebook-cell>", "exec")
        tree = ast.parse(source_text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        try:
                            assignments[target.id] = ast.literal_eval(node.value)
                        except (ValueError, TypeError):
                            pass
    for name in (
        "ALLOW_DEPENDENCY_INSTALL",
        "ALLOW_PINNED_MODEL_DOWNLOAD",
        "ALLOW_MODEL_LOAD",
        "RUN_SCORING_SMOKE",
        "RUN_FULL_A_EXPORT",
    ):
        assert assignments[name] is True
    source_all = "\n".join("".join(cell) for cell in code_cells)
    assert "trust_remote_code=False" in source_all
    assert "local_files_only=True" in source_all
    assert "model.generate" not in source_all
    assert "torch_dtype=torch.float16" in source_all
    assert "SMOKE_PASSED" in source_all
    assert "score_batch_with_torch_model" in source_all
    assert "repeatability_tolerance" in source_all
    assert "RUN_FULL_A_EXPORT = True" in source_all
    assert "/artifacts/runs/" not in source_all
    assert "room_gt" not in source_all.lower()
    assert "review.csv" not in source_all.lower()
    assert "'GT' not in str(categories_doc)" not in source_all


def test_fp16_runtime_approval_and_upstream_checkpoint_provenance_are_explicit():
    lock = json.loads((INPUT_ROOT / "environment_candidate_lock.json").read_text())
    assert lock["model"]["upstream_tensor_type_reported_by_upstream"] == "BF16"
    assert lock["execution_contract"]["dtype"].endswith("FP16 on Tesla T4")
    assert lock["execution_contract"]["quantization"] == "none"
    assert lock["model"]["expected_upstream_sha256_manifest"] is None
    assert lock["execution_contract"]["repeatability_tolerance"]["absolute_logprob"] == 0.002


def test_scorer_imports_as_standalone_packaged_module() -> None:
    path = INPUT_ROOT / "qwen_room_scorer.py"
    spec = importlib.util.spec_from_file_location("qwen_room_scorer_standalone_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.object_prompt("sofa") == "Observed object: sofa.\nRoom type:"
    assert module.neutral_object_prompt() == "Observed object: object.\nRoom type:"
