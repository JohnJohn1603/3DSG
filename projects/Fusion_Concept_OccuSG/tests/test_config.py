from pathlib import Path

import pytest

from standalone_sg.config import load_config
from standalone_sg.contracts import ContractError


def test_reference_code_config_is_strict_and_deterministic() -> None:
    config = load_config(Path("configs/reference_code.yaml"))
    assert config.reference_target == "code"
    assert config.source["commit"] == "2bca2fa06af87fd9dd0542957039be8f580f0dca"
    assert config.backprojection.scale_for("16UC1") == 0.001
    assert len(config.config_hash) == 64
    assert config.config_hash == config.config_hash


def test_unknown_root_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "schema_version: 1\nprofile: x\nmode: reference\nreference_target: code\n"
        "source: {commit: abc}\ncoordinate: {}\nbackprojection: {}\nnative: {}\n"
        "unexpected: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="unknown config key"):
        load_config(path)


def test_missing_source_commit_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "schema_version: 1\nprofile: x\nmode: reference\nreference_target: code\n"
        "source: {}\ncoordinate: {}\nbackprojection: {}\nnative: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="source.commit"):
        load_config(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("stride", "1.9", "backprojection.stride"),
        ("min_depth_m", "NaN", "backprojection.min_depth_m"),
    ],
)
def test_invalid_numeric_config_values_are_rejected(tmp_path: Path, field: str, value: str, message: str) -> None:
    text = Path("configs/test_fixture.yaml").read_text(encoding="utf-8")
    text = text.replace("  stride: 1\n", f"  stride: {value}\n") if field == "stride" else text.replace(
        "  min_depth_m: 0.1\n", f"  min_depth_m: {value}\n"
    )
    path = tmp_path / f"standalone_sg_bad_{field}.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ContractError, match=message):
        load_config(path)


def test_string_boolean_is_not_coerced(tmp_path: Path) -> None:
    text = Path("configs/test_fixture.yaml").read_text(encoding="utf-8")
    text = text.replace("  require_reference_backends: false\n", "  require_reference_backends: \"false\"\n")
    path = tmp_path / "standalone_sg_bad_bool.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ContractError, match="native.require_reference_backends"):
        load_config(path)


def test_runtime_and_mp3d_profiles_are_explicit() -> None:
    runtime = load_config(Path("configs/reference_code.yaml"))
    mp3d = load_config(Path("configs/reference_code_mp3d.yaml"))
    fixture = load_config(Path("configs/test_fixture.yaml"))
    assert runtime.parameter_profile == "node_default"
    assert runtime.backprojection.stride == 1
    assert mp3d.parameter_profile == "mp3d_audit"
    assert mp3d.backprojection.stride == 2
    assert fixture.parameter_profile == "fixture"
    assert runtime.projection.min_free_height_m == 0.3
    assert runtime.projection.minimum_occupancy == 5


def test_object_association_config_rejects_coercion_unknown_keys_and_negative_values(tmp_path: Path) -> None:
    text = Path("configs/test_fixture.yaml").read_text(encoding="utf-8")
    for value, message in (("\"0.75\"", "objects.association.same_class_radius_m"),
                          ("-0.1", "non-negative")):
        bad = text.replace("same_class_radius_m: 0.75", f"same_class_radius_m: {value}")
        path = tmp_path / f"bad_object_radius_{value.replace('-', 'minus').replace(chr(34), 'quote')}.yaml"
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ContractError, match=message):
            load_config(path)


def test_navigation_config_rejects_invalid_types_ranges_and_unknown_keys(tmp_path: Path) -> None:
    text=Path("configs/test_fixture.yaml").read_text(encoding="utf-8")
    for old,new,match in (("cell_stride_cells: 15","cell_stride_cells: 15.5","navigation.cell_stride_cells"),
                          ("connectivity: 8","connectivity: 6","connectivity must be 4 or 8"),
                          ("resolution_change_tolerance_m: 0.0001","resolution_change_tolerance_m: -0.1","non-negative")):
        bad=text.replace(old,new)
        path=tmp_path/"bad_navigation.yaml"; path.write_text(bad,encoding="utf-8")
        with pytest.raises(ContractError,match=match): load_config(path)


def test_room_config_is_strict_and_profiles_pin_m8_tolerances() -> None:
    runtime=load_config("configs/reference_code.yaml").rooms
    assert runtime.boundary_epsilon_m==pytest.approx(.15)
    assert runtime.maintenance_hz==pytest.approx(.5)
    assert runtime.relink_min_old_overlap_ratio==pytest.approx(.65)
