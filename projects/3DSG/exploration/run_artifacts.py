"""Create immutable per-run effective config and provenance manifest."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any

import numpy as np


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def prepare_run_artifacts(*, output_root: str | Path, run_id: str,
                          scene_config_path: str | Path, exploration_budget_s: float,
                          shutdown_timeout_s: float = 30.0,
                          navigation_profile: dict[str, Any],
                          navigation_profile_path: str | Path,
                          runtime_components: dict[str, Any] | None = None
                          ) -> tuple[Path, dict[str, Any]]:
    """Create a new run directory and persist its config before simulator boot.

    Existing run IDs are rejected. Files are created with exclusive semantics,
    fsynced, and the directory is fsynced so failed runs retain their provenance.
    """
    if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise ValueError("run_id must be a path-safe token of at most 96 characters")
    scene_path = Path(scene_config_path).expanduser().resolve(strict=True)
    raw_scene_config = scene_path.read_bytes()
    scene_config = json.loads(raw_scene_config)
    profile_path = Path(navigation_profile_path).expanduser().resolve(strict=True)
    profile_bytes = profile_path.read_bytes()

    root = Path(output_root).expanduser().resolve()
    run_root = root / run_id
    root.mkdir(parents=True, exist_ok=True)
    run_root.mkdir(exist_ok=False)
    effective = {
        "schema": "frontier-effective-config/v1",
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "clock_domains": {
            "exploration_budget": "host_monotonic",
            "frame_host_timestamp": "python_time.monotonic",
            "simulator_timestamp": "habitat_simulation_time",
        },
        "episode": {
            "exploration_budget_s": float(exploration_budget_s),
            "shutdown_timeout_s": float(shutdown_timeout_s),
            "spawn_warmup_excluded_from_budget": True,
        },
        "runtime_overrides": {
            "exploration_budget_s": float(exploration_budget_s),
            "shutdown_timeout_s": float(shutdown_timeout_s),
        },
        "runtime_components": dict(runtime_components or {}),
        "scene_config": scene_config,
        "scene_config_path": str(scene_path),
        "scene_config_sha256": hashlib.sha256(raw_scene_config).hexdigest(),
        "navigation_profile_path": str(profile_path.resolve()),
        "navigation_profile_sha256": hashlib.sha256(profile_bytes).hexdigest(),
        "navigation_profile_effective": navigation_profile,
        "runtime_versions": {
            "python": sys.version,
            "platform": platform.platform(),
            "numpy": np.__version__,
            "habitat_sim": _package_version("habitat-sim"),
            "magnum": _package_version("magnum"),
            "numpy_quaternion": _package_version("numpy-quaternion"),
        },
    }
    effective_bytes = _json_bytes(effective)
    manifest = {
        "schema": "frontier-run-manifest/v1",
        "run_id": run_id,
        "effective_config_file": "effective_config.json",
        "effective_config_sha256": hashlib.sha256(effective_bytes).hexdigest(),
        "event_journal_file": "events.jsonl",
        "state_file": "run_summary.json",
    }
    _write_exclusive(run_root / "effective_config.json", effective_bytes)
    _write_exclusive(run_root / "run_manifest.json", _json_bytes(manifest))
    _fsync_dir(run_root)
    _fsync_dir(root)
    return run_root, manifest


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def write_run_summary(run_root: str | Path, summary: dict[str, Any]) -> Path:
    """Persist the one final lifecycle record without rewriting earlier runs."""
    path = Path(run_root) / "run_summary.json"
    _write_exclusive(path, _json_bytes({"schema": "frontier-run-summary/v1", **summary}))
    _fsync_dir(path.parent)
    return path


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
