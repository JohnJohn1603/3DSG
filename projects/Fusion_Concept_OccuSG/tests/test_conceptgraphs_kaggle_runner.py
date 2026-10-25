from pathlib import Path

from scripts.conceptgraphs_kaggle_runner import WEIGHTS, _models, _prepare_headless_detector_script


def _gsa_with_required_checkpoints(root: Path) -> Path:
    gsa = root / "gsa"
    gsa.mkdir()
    for name in WEIGHTS:
        (gsa / name).write_bytes((name + "-fixture").encode())
    return gsa


def test_model_probe_requires_exact_openclip_checkpoint(tmp_path):
    gsa = _gsa_with_required_checkpoints(tmp_path)
    state = _models(gsa, tmp_path / "home")
    assert state["missing_gsa_weights"] == []
    assert state["openclip_missing_at_probe"] is True
    assert state["missing_openclip_checkpoint"] is True
    assert state["openclip_cached_files"] == []


def test_model_probe_accepts_pinned_huggingface_cache_checkpoint(tmp_path):
    gsa = _gsa_with_required_checkpoints(tmp_path)
    checkpoint = (
        tmp_path / "home/.cache/huggingface/hub/models--laion--CLIP-ViT-H-14-laion2B-s32B-b79K"
        / "snapshots/revision/open_clip_pytorch_model.bin"
    )
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"exact-openclip-fixture")

    state = _models(gsa, tmp_path / "home")
    assert state["missing_openclip_checkpoint"] is False
    assert state["openclip_missing_at_probe"] is False
    assert state["openclip_cached_files"][0]["sha256"]
    assert state["openclip_cached_files"][0]["path"] == str(checkpoint.resolve())


def test_model_probe_ignores_unrelated_similarly_named_cache_file(tmp_path):
    gsa = _gsa_with_required_checkpoints(tmp_path)
    unrelated = tmp_path / "home/.cache/huggingface/hub/models--other--model/snapshots/rev/open_clip_pytorch_model.bin"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"not-the-locked-model")

    state = _models(gsa, tmp_path / "home")
    assert state["missing_openclip_checkpoint"] is True


def test_headless_adaptation_copies_only_detector_entrypoint_and_records_hash(tmp_path):
    reference = tmp_path / "reference"
    source = reference / "conceptgraph/scripts/generate_gsa_results.py"
    source.parent.mkdir(parents=True)
    source.write_text('import matplotlib\nmatplotlib.use("TkAgg")\n')
    (reference / "unrelated_tooling.py").write_text("must not be copied")

    output = tmp_path / "run"
    adapted, digest = _prepare_headless_detector_script(reference, output)

    assert adapted == output / "compat/generate_gsa_results.py"
    assert 'matplotlib.use("Agg")' in adapted.read_text()
    assert 'matplotlib.use("TkAgg")' not in adapted.read_text()
    assert digest and len(digest) == 64
    assert not (output / "reference_adapted").exists()
    assert not (output / "compat/unrelated_tooling.py").exists()
    assert source.read_text().endswith('matplotlib.use("TkAgg")\n')


def test_headless_adaptation_fails_if_upstream_precondition_changes(tmp_path):
    reference = tmp_path / "reference"
    source = reference / "conceptgraph/scripts/generate_gsa_results.py"
    source.parent.mkdir(parents=True)
    source.write_text('matplotlib.use("QtAgg")\n')

    import pytest
    with pytest.raises(RuntimeError, match="adaptation precondition"):
        _prepare_headless_detector_script(reference, tmp_path / "run")
