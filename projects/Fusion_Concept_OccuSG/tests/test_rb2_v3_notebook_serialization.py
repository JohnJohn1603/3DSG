import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks/room_belief_qwen3_rb2_v3.ipynb"


def test_notebook_sources_are_serialized_as_lines_and_executable_cells_parse():
    notebook = json.loads(NOTEBOOK.read_text())
    assert notebook["nbformat"] == 4
    assert len(notebook["cells"]) == 16

    for index, cell in enumerate(notebook["cells"]):
        source = cell["source"]
        assert isinstance(source, list), f"cell {index} source must be line-array serialized"
        assert all(isinstance(line, str) for line in source)
        joined = "".join(source)
        if source:
            assert not (len(source) > 3 and all(len(line) == 1 for line in source)), (
                f"cell {index} source is split character-by-character"
            )
        if cell["cell_type"] != "code":
            continue
        code = joined
        if code.startswith("%%writefile "):
            _, _, code = code.partition("\n")
        ast.parse(code, filename=f"notebook-cell-{index}")


def test_c2_and_smoke_paths_are_isolated_to_v3():
    notebook = json.loads(NOTEBOOK.read_text())
    c2 = "".join(notebook["cells"][5]["source"])
    smoke = "".join(notebook["cells"][10]["source"])
    full = "".join(notebook["cells"][12]["source"])
    assert "room_belief_qwen_rb2_output_v3" in c2
    assert "room_belief_rb2_env_v3" in c2
    assert "room_belief_qwen_rb2_output_v2" not in c2
    assert "qwen_rb2_hf_cache_v3" in smoke and "qwen_rb2_hf_cache_v3" in full
    assert "gzip.decompress" not in "".join("".join(c["source"]) for c in notebook["cells"])
