"""Hash-verified resolver for the workspace's locked external inputs.

External executables and artifacts are input-only. This resolver never copies,
builds, or modifies them. Integration-test mode can make missing resources a
collection-time error by setting ``OCCUSG_REQUIRE_NATIVE_RESOURCES=1``.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from .contracts.errors import ContractError


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_LOCK = WORKSPACE_ROOT / "locks/external_resources.json"
REQUIRE_ENV = "OCCUSG_REQUIRE_NATIVE_RESOURCES"


def integration_resources_required() -> bool:
    value = os.environ.get(REQUIRE_ENV, "0").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ContractError(f"{REQUIRE_ENV} must be a boolean-like value")


def load_resource_lock(path: Path = RESOURCE_LOCK) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read external resource lock {path}: {exc}") from exc
    if (lock.get("status") != "bound_input_only"
            or Path(lock.get("workspace_root", "")).resolve() != WORKSPACE_ROOT):
        raise ContractError("external resource lock is not bound input-only to this workspace")
    return lock


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def resolve_external_file(name: str, *, required: bool | None = None,
                          executable: bool = False, lock_path: Path = RESOURCE_LOCK) -> Path:
    """Resolve and verify one file entry from ``locks/external_resources.json``."""
    if required is None:
        required = integration_resources_required()
    record = load_resource_lock(lock_path).get("files", {}).get(name)
    if not isinstance(record, dict) or not isinstance(record.get("path"), str):
        raise ContractError(f"external resource `{name}` has no file binding in {lock_path}")
    path = Path(record["path"]).expanduser().absolute()
    if path.resolve().is_relative_to(WORKSPACE_ROOT.resolve()):
        raise ContractError(f"external resource `{name}` resolves inside the writable workspace: {path}")
    role=record.get("role")
    if role is not None and not any(token in str(role) for token in ("read_only", "input_only", "posthoc")):
        raise ContractError(f"external resource `{name}` is not declared input-only in the lock")
    if not path.is_file():
        if required:
            raise ContractError(f"required external resource `{name}` is missing: {path}")
        return path
    if path.stat().st_size != record.get("bytes"):
        raise ContractError(f"external resource `{name}` size differs from lock: {path}")
    digest = _digest(path)
    if digest != record.get("sha256"):
        raise ContractError(f"external resource `{name}` SHA-256 differs from lock: {path}")
    if executable and not os.access(path, os.X_OK):
        raise ContractError(f"external resource `{name}` is not executable: {path}")
    return path


def verify_required_resources(names: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """Verify all named inputs; suitable for integration-test preflight."""
    records = load_resource_lock().get("files", {})
    result: dict[str, dict[str, object]] = {}
    for name in names:
        executable=name.startswith("native_")
        path = resolve_external_file(name, required=True, executable=executable)
        row = records[name]
        dependencies = None
        if executable:
            probe = subprocess.run(["ldd", str(path)], capture_output=True, text=True, check=False, timeout=10)
            text = probe.stdout + probe.stderr
            if probe.returncode != 0 or "not found" in text:
                raise ContractError(f"external resource `{name}` has unresolved runtime dependencies: {text}")
            dependencies = "resolved"
        result[name] = {"path": str(path), "bytes": row["bytes"], "sha256": row["sha256"]}
        if dependencies is not None:
            result[name]["runtime_dependencies"] = dependencies
    return result
