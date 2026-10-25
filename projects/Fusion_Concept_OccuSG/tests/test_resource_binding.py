import json
from pathlib import Path

import pytest

from standalone_sg.contracts import ContractError
from standalone_sg.resource_binding import RESOURCE_LOCK, WORKSPACE_ROOT, resolve_external_file, verify_required_resources


def test_locked_native_and_baseline_inputs_resolve_with_verified_hashes():
    resolved = verify_required_resources((
        "baseline_lock", "native_m2", "native_m2_m3", "native_dude",
        "native_m3_smoke", "native_dude_source_reference",
    ))
    assert resolved["native_m2"]["path"].endswith("/Rebuild_3DScene_OccuSG/build/native-m2/occupancy/standalone_sg_octomap_fixture_runner")
    assert resolved["native_dude_source_reference"]["path"].endswith("/native-m4-dude-reference/occusg_dude_source_reference")
    assert all(not Path(row["path"]).resolve().is_relative_to(WORKSPACE_ROOT) for row in resolved.values())


def test_resolver_fails_clearly_for_missing_or_hash_mismatched_required_input(tmp_path):
    lock = json.loads(RESOURCE_LOCK.read_text())
    lock["files"]["synthetic_missing"] = {
        "path": str(tmp_path / "not-present"), "bytes": 4, "sha256": "0" * 64,
        "role": "read_only_external_test_input",
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(ContractError, match="is missing"):
        resolve_external_file("synthetic_missing", required=True, lock_path=lock_path)
    missing = tmp_path / "not-present"
    missing.write_bytes(b"data")
    with pytest.raises(ContractError, match="SHA-256 differs"):
        resolve_external_file("synthetic_missing", required=True, lock_path=lock_path)
