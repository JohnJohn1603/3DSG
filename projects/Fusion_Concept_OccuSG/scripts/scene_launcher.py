#!/usr/bin/env python3
"""Thin, safe CLI/orchestrator for the existing Habitat workflow."""
from __future__ import annotations

import fcntl
import builtins
import hashlib
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from standalone_sg.resource_binding import load_resource_lock, resolve_external_file
from scripts.object_fusion_launcher import compare_detection3d, inspect_input, read_json as read_fusion_json, sha256 as fusion_sha256, validate_sidecar

REGISTRY = ROOT / "configs/launcher/scenes.json"
LOCAL_ENV = ROOT / "configs/launcher/local_environment.json"
RUNS = ROOT / "artifacts/runs"
OBJECT_FUSIONS = ROOT / "artifacts/object_fusions"
HYBRID_MAPPINGS = ROOT / "artifacts/conceptgraphs_hybrid_runs"
SCENE_WORKFLOWS = ROOT / "artifacts/scene_workflows"
CACHED_REPLAYS = ROOT / "artifacts/cached_replays"
HYBRID_MAPPING_PYTHON = ROOT / ".venv-cg-cpu/bin/python"
LOCK_PATH = RUNS / ".launcher.lock"
ACTIVE_POINTER = RUNS / ".launcher_active.json"
RUNNER = ROOT / "scripts"
COMMANDS = ("help", "all", "maps", "use", "info", "routes", "check", "run", "runs",
            "status", "logs", "results", "open", "replay-cached", "compare-profiles", "fuse-objects",
            "compare-object-fusions", "unified-graph", "hybrid-map", "cached-replay", "fresh-replica",
            "live-replay", "start-live", "evaluate", "evaluate-objects", "object-results", "cancel", "register", "exit",
            "menu", "scene-run", "camera-preview")


def colorize_terminal_text(text: str, *, enabled: bool | None = None) -> str:
    """Color CLI labels and statuses without changing text or redirected output."""
    if enabled is None:
        stream = sys.stderr if getattr(sys, "_launcher_printing_stderr", False) else sys.stdout
        enabled = (hasattr(stream, "isatty") and stream.isatty()
                   and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")
    if not enabled or "\033[" in text:
        return text
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, TypeError):
        pass

    reset = "\033[0m"
    styles = {
        "heading": "\033[1;36m",
        "key": "\033[1;36m",
        "success": "\033[32m",
        "warning": "\033[33m",
        "failure": "\033[1;31m",
        "path": "\033[36m",
        "muted": "\033[2;37m",
    }
    output = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        stripped = body.strip()
        if not stripped:
            output.append(line)
            continue

        lowered = body.lower()
        if any(token in lowered for token in (" fail", "failed", "failure", "error", "lỗi", "blocked", "cancelled")):
            output.append(f"{styles['failure']}{body}{reset}{ending}")
            continue
        if any(token in lowered for token in ("warning", "cảnh báo", "unavailable", "not_available", "n/a", "unknown", "conflicted", "deferred", "ambiguous", "experimental", "chưa có")):
            output.append(f"{styles['warning']}{body}{reset}{ending}")
            continue
        if any(token in lowered for token in (" pass", "passed", "complete", "completed", "success", " sẵn sàng", "đã đăng ký")):
            output.append(f"{styles['success']}{body}{reset}{ending}")
            continue

        # Emphasize field names and workflow stages; color paths independently.
        import re
        body = re.sub(r"^([^:|]+:)", lambda m: f"{styles['key']}{m.group(1)}{reset}", body)
        body = re.sub(r"(?<![\w])((?:/[^\s|,;]+)+)", lambda m: f"{styles['path']}{m.group(1)}{reset}", body)
        output.append(body + ending)
    return "".join(output)


def print(*args: Any, **kwargs: Any) -> None:
    """Terminal-only presentation wrapper; preserve built-in print semantics."""
    sep = kwargs.get("sep", " ")
    if sep is None:
        sep = " "
    file = kwargs.get("file", None) or sys.stdout
    end = kwargs.get("end", "\n")
    text = sep.join(str(arg) for arg in args)
    if file is sys.stderr:
        sys._launcher_printing_stderr = True
    try:
        rendered = colorize_terminal_text(text, enabled=(
            hasattr(file, "isatty") and file.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"))
    finally:
        if hasattr(sys, "_launcher_printing_stderr"):
            del sys._launcher_printing_stderr
    builtins.print(rendered, **kwargs)


def colorize_object_results(text: str, *, enabled: bool | None = None) -> str:
    """Add restrained ANSI hierarchy for an interactive terminal only."""
    if enabled is None:
        enabled = (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
                   and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")
    if not enabled:
        return text
    reset = "\033[0m"
    bold_cyan = "\033[1;36m"
    cyan = "\033[36m"
    green = "\033[32m"
    yellow = "\033[33m"
    red = "\033[31m"
    dim = "\033[2m"
    rendered = []
    for index, line in enumerate(text.splitlines()):
        color = None
        if index == 0 or line.strip() == "TOTAL OBSERVATIONS":
            color = bold_cyan
        elif line.startswith("Frame |") or line.startswith("Detection |"):
            color = bold_cyan
        elif line.startswith("-+-"):
            color = dim
        elif "N/A" in line or "unavailable" in line:
            color = yellow
        elif "False positive" in line or "False negative" in line or " | false_positive |" in line:
            color = red
        elif "Matched" in line or "matched" in line:
            color = green
        elif line.startswith("Scope:") or line.startswith("Output:") or "Output: " in line:
            color = dim
        elif line.startswith("Scene:"):
            color = cyan
        rendered.append(f"{color}{line}{reset}" if color else line)
    return "\n".join(rendered)


def colorize_profile_comparison(text: str, *, enabled: bool | None = None) -> str:
    """Add terminal-only visual hierarchy to the paired-profile summary."""
    if enabled is None:
        enabled = (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
                   and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")
    if not enabled or "\033[" in text:
        return text
    reset = "\033[0m"
    title = "\033[1;36m"
    metric = "\033[1;37m"
    source = "\033[1;36m"
    candidate = "\033[1;35m"
    caution = "\033[33m"
    muted = "\033[2;37m"
    label_width, value_width = 30, 22
    rendered = []
    for index, line in enumerate(text.splitlines()):
        if index == 0:
            color = title
        elif line.startswith("Metric"):
            label = line[:label_width]
            source_cell = line[label_width + 1:label_width + 1 + value_width]
            candidate_cell = line[label_width + value_width + 2:label_width + 2 * value_width + 2]
            rendered.append(f"{title}{label}{reset} {source}{source_cell}{reset} {candidate}{candidate_cell}{reset}")
            continue
        elif line.startswith("Input:"):
            color = muted
        elif line.startswith("P3 accepted/rejected="):
            color = caution
        elif line.startswith("Human physical-identity metrics:"):
            color = caution
        elif line.startswith("Report:") or line.startswith("Open with:"):
            color = "\033[36m"
        elif line.strip():
            # Fixed-width cells are padded before ANSI is applied, so color
            # escape sequences cannot affect the visible column alignment.
            label = line[:label_width]
            source_cell = line[label_width + 1:label_width + 1 + value_width]
            candidate_cell = line[label_width + value_width + 2:label_width + 2 * value_width + 2]
            rendered.append(f"{metric}{label}{reset} {source}{source_cell}{reset} {candidate}{candidate_cell}{reset}")
            continue
        else:
            color = None
        rendered.append(f"{color}{line}{reset}" if color else line)
    return "\n".join(rendered)


def profile_terminal_line(text: str, profile: str) -> str:
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"):
        return text
    color = {"source": "\033[1;36m", "geometry-only": "\033[1;33m",
             "geometry-only-dedup-v2": "\033[1;35m"}.get(profile, "\033[1;36m")
    return f"{color}{text}\033[0m"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def resolve_d2_viewer_entrypoint(run_root: Path) -> dict[str, Any] | None:
    """Resolve the published D2 page from its manifest pointer, verifying both hashes."""
    viewer_root = Path(run_root).resolve() / "viewer"
    pointer_path = viewer_root / "d2_viewer_entrypoint.json"
    if not pointer_path.is_file():
        return None
    pointer = read_json(pointer_path)
    if (pointer.get("schema") != "d2-viewer-entrypoint/v1"
            or pointer.get("status") not in {"ready", "completed", "failed", "cancelled"}):
        raise ValueError(f"invalid D2 viewer entrypoint pointer: {pointer_path}")
    manifest_name = str(pointer.get("manifest_path", ""))
    entrypoint = str(pointer.get("entrypoint", ""))
    if not manifest_name or Path(manifest_name).name != manifest_name or not entrypoint or Path(entrypoint).name != entrypoint:
        raise ValueError("D2 viewer pointer paths must be filenames within the viewer directory")
    manifest_path = viewer_root / manifest_name
    entrypoint_path = viewer_root / entrypoint
    if not manifest_path.is_file() or not entrypoint_path.is_file():
        raise FileNotFoundError("D2 viewer pointer references a missing manifest or entrypoint")
    if sha256(manifest_path) != pointer.get("manifest_sha256"):
        raise ValueError("D2 viewer manifest hash differs from its entrypoint pointer")
    d2_manifest = read_json(manifest_path)
    expected_pointer_status = "ready" if d2_manifest.get("status") == "running" else d2_manifest.get("status")
    if (d2_manifest.get("status") not in {"running", "completed", "failed", "cancelled"}
            or pointer.get("pipeline_status") != d2_manifest.get("status")
            or pointer.get("status") != expected_pointer_status
            or d2_manifest.get("viewer_entrypoint") != entrypoint
            or d2_manifest.get("viewer_version") != pointer.get("viewer_version")):
        raise ValueError("D2 viewer manifest and entrypoint pointer disagree")
    output_hash = d2_manifest.get("output_files_sha256", {}).get(entrypoint)
    if output_hash != pointer.get("entrypoint_sha256") or sha256(entrypoint_path) != output_hash:
        raise ValueError("D2 viewer entrypoint hash differs from its manifest/pointer")
    from scripts.build_d2_xyz_viewer import validate_entrypoint_bundle
    bundle = validate_entrypoint_bundle(viewer_root)
    return {"entrypoint": entrypoint, "manifest_path": manifest_path,
            "manifest": d2_manifest, "pointer_path": pointer_path,
            "pipeline_status": d2_manifest.get("status"),
            "script_assets": bundle["script_assets"]}


def find_duplicate_v2_reference(cache_run: Path) -> Path | None:
    """Find a matching offline-v2 reference; absence is diagnostic, not a run blocker."""
    cache_run = cache_run.resolve()
    p2_path = cache_run / "detection_2d/detection_2d_index.json"
    p3_path = cache_run / "detection_3d/detection_3d.jsonl"
    capture_path = cache_run / "capture/capture_manifest.json"
    expected = {"p2_sha256": sha256(p2_path), "p3_sha256": sha256(p3_path),
                "capture_sha256": sha256(capture_path)}
    matches = []
    for report_path in RUNS.glob("u3_same_frame_dup_candidate_v2_*/**/report.json"):
        try:
            run_root = report_path.parents[1]
            if (run_root / "SUPERSEDED.md").is_file():
                continue
            report = read_json(report_path)
            manifest = report.get("input_manifest", {})
            if all(manifest.get(key) == value for key, value in expected.items()):
                assignment_path = report_path.parent / "assignments_candidate_v2.jsonl"
                if assignment_path.is_file():
                    matches.append(assignment_path.resolve())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    unique = sorted(set(matches))
    if len(unique) > 1:
        raise ValueError(f"multiple offline v2 references match these P2/P3/capture hashes: {len(unique)}")
    if not unique:
        return None
    return unique[0]


def audit_external_resources(env: dict | None = None) -> list[str]:
    """Validate the explicit, input-only resource lock before launcher work."""
    env = env or load_environment()
    lock_path = Path(env["resource_manifest"])
    doc = read_json(lock_path)
    issues: list[str] = []
    if doc.get("schema_version") != 1 or doc.get("status") != "bound_input_only":
        issues.append("external resource manifest schema/status is not bound_input_only")
    if Path(doc.get("workspace_root", "")).resolve() != ROOT:
        issues.append("external resource manifest workspace_root differs from this workspace")
    for name, record in doc.get("files", {}).items():
        path = Path(record.get("path", "")).expanduser().resolve()
        if path.is_relative_to(ROOT):
            issues.append(f"external input {name} unexpectedly resolves inside workspace: {path}")
            continue
        if not path.is_file():
            issues.append(f"external input missing: {name}: {path}")
            continue
        if sha256(path) != record.get("sha256"):
            issues.append(f"external input checksum mismatch: {name}: {path}")
    for name, record in doc.get("directories", {}).items():
        path = Path(record.get("path", "")).expanduser().resolve()
        if path.is_relative_to(ROOT) or not path.is_dir():
            issues.append(f"external input directory invalid: {name}: {path}")
    pinned = doc.get("mapconversion", {})
    try:
        current = subprocess.check_output(["git", "-C", pinned["path"], "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT).strip()
        if current != pinned.get("expected_commit") or current != pinned.get("actual_commit"):
            issues.append(f"MapConversion commit differs from lock: {current}")
    except Exception as exc:
        issues.append(f"MapConversion checkout unavailable: {exc}")
    return issues


def rel_or_abs(path: Path) -> str:
    path = path.resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def resolve_path(value: str | Path) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p.resolve()
    # Dataset/model paths in profiles keep their historical artifact-relative
    # spelling, while their bytes are explicitly bound to read-only external
    # roots. All other relative paths remain workspace-local.
    parts = p.parts
    if len(parts) >= 2 and parts[0] == "artifacts" and parts[1] in {"habitat_data", "models"}:
        env_doc = read_json(LOCAL_ENV)
        root_key = "habitat_data_root" if parts[1] == "habitat_data" else "model_root"
        external_root = Path(env_doc[root_key]).expanduser().resolve()
        return external_root.joinpath(*parts[2:]).resolve()
    if len(parts) >= 3 and parts[:2] == ("artifacts", "runs") and parts[2] == "hm3d_integration_20261005":
        env_doc = read_json(LOCAL_ENV)
        return Path(env_doc["baseline_read_root"]).expanduser().resolve().joinpath(*parts).resolve()
    if len(parts) >= 2 and parts[:2] == ("evaluation", "gt"):
        env_doc = read_json(LOCAL_ENV)
        return Path(env_doc["baseline_read_root"]).expanduser().resolve().joinpath(*parts).resolve()
    return (ROOT / p).resolve()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def unique_run_id(alias: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{alias}_{stamp}_{uuid.uuid4().hex[:6]}"


def _safe_manifest_args(args: list[str]) -> list[str]:
    result: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next:
            result.append("<redacted>")
            hide_next = False
            continue
        if any(word in arg.lower() for word in ("token", "secret", "password", "credential")):
            result.append("<redacted>")
            hide_next = arg.startswith("--") and "=" not in arg
        else:
            result.append(arg)
    return result


def validate_registry(doc: Any) -> None:
    if (not isinstance(doc, dict) or doc.get("schema_version") != 1
            or set(doc) not in ({"schema_version", "scenes"}, {"schema_version", "scenes", "catalog"})):
        raise ValueError("registry requires schema_version=1, scenes, and optional catalog")
    if not isinstance(doc["scenes"], list):
        raise ValueError("registry.scenes must be a list")
    seen: set[str] = set()
    for entry in doc["scenes"]:
        if not isinstance(entry, dict) or set(entry) != {"alias", "evaluation_profile", "routes", "default_route"}:
            raise ValueError("each scene entry must contain alias, evaluation_profile, routes, default_route only")
        alias = entry["alias"]
        if not isinstance(alias, str) or not alias or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in alias):
            raise ValueError(f"invalid scene alias: {alias!r}")
        if alias in seen:
            raise ValueError(f"duplicate scene alias: {alias}")
        seen.add(alias)
        if not isinstance(entry["routes"], dict) or not entry["routes"] or entry["default_route"] not in entry["routes"]:
            raise ValueError(f"{alias}: routes must be non-empty and contain default_route")
        for name, config in entry["routes"].items():
            if not isinstance(name, str) or not name or not isinstance(config, str):
                raise ValueError(f"{alias}: route aliases and config paths must be non-empty strings")
    if "catalog" in doc:
        catalog = doc["catalog"]
        if (not isinstance(catalog, dict) or catalog.get("schema_version") != 1
                or not isinstance(catalog.get("datasets"), list) or not catalog["datasets"]
                or not isinstance(catalog.get("c2_template"), str)):
            raise ValueError("registry.catalog must define schema_version=1, datasets, and c2_template")


def load_registry(path: Path = REGISTRY) -> dict:
    doc = read_json(path)
    validate_registry(doc)
    return doc


def load_environment(path: Path = LOCAL_ENV) -> dict:
    doc = read_json(path)
    required = {"schema_version", "project_python", "habitat_python", "baseline_read_root", "habitat_data_root", "model_root",
                "native_dependency_prefix", "mapconversion_source", "mapconversion_expected_commit",
                "pythonpath", "mplconfigdir", "detection_3d_profile", "native_m2_m3", "native_dude",
                "resource_manifest", "project_package_pins"}
    if not isinstance(doc, dict) or set(doc) != required or doc["schema_version"] != 2:
        raise ValueError(f"local environment config must have exactly: {', '.join(sorted(required))}")
    for key in ("habitat_python", "baseline_read_root", "habitat_data_root", "model_root", "native_dependency_prefix",
                "mapconversion_source", "detection_3d_profile", "native_m2_m3", "native_dude", "resource_manifest"):
        doc[key] = str(resolve_path(doc[key]))
    doc["native_m2_m3"] = str(resolve_external_file("native_m2_m3", required=True, executable=True))
    doc["native_dude"] = str(resolve_external_file("native_dude", required=True, executable=True))
    for key in ("project_python",):
        if key == "project_python":
            # Keep a venv's lexical bin/python path: resolving its symlink can
            # turn it into /usr/bin/python and bypass the venv site-packages.
            interpreter = Path(doc[key]).expanduser()
            doc[key] = str((ROOT / interpreter).absolute() if not interpreter.is_absolute() else interpreter.absolute())
        else:
            doc[key] = str(resolve_path(doc[key]))
    mpl_path = Path(doc["mplconfigdir"]).expanduser()
    doc["mplconfigdir"] = str((ROOT / mpl_path).resolve() if not mpl_path.is_absolute() else mpl_path.resolve())
    if not isinstance(doc["project_package_pins"], dict) or any(
            not isinstance(name, str) or not isinstance(version, str)
            for name, version in doc["project_package_pins"].items()):
        raise ValueError("project_package_pins must map distribution names to exact versions")
    return doc


def scene_entry(alias: str, registry: dict | None = None) -> dict:
    registry = registry or load_registry()
    for entry in registry["scenes"]:
        if entry["alias"] == alias:
            return entry
    raise ValueError(f"scene alias không tồn tại: {alias}; dùng `maps` để xem danh sách")


def validate_comparison_baseline(ctx: dict) -> dict | None:
    """Validate a route's declared comparison run without modifying either run."""
    route = ctx["route"]
    baseline_id = route.get("comparison_baseline_run_id")
    if not baseline_id:
        return None
    if not isinstance(baseline_id, str) or "/" in baseline_id or baseline_id.startswith("."):
        raise ValueError("comparison_baseline_run_id must be a launcher run ID")
    baseline_root = RUNS / baseline_id
    manifest_path = baseline_root / "launcher_manifest.json"
    capture_manifest_path = baseline_root / "capture/capture_manifest.json"
    route_path = baseline_root / "effective_route_config.json"
    if not all(path.is_file() for path in (manifest_path, capture_manifest_path, route_path)):
        # Seed baselines are immutable inputs and intentionally live outside
        # this workspace. Resolve only through the workspace-bound resource
        # lock; never copy them into Fusion or modify their artifacts.
        resource_lock = load_resource_lock()
        baseline_parent = Path(resource_lock.get("baseline_root", "")).expanduser().resolve()
        if not baseline_parent.is_dir() or baseline_parent.is_relative_to(ROOT.resolve()):
            raise ValueError(f"comparison baseline is incomplete and locked baseline root is unavailable: {baseline_id}")
        baseline_root = (baseline_parent / "artifacts/runs" / baseline_id).resolve()
        if not baseline_root.is_relative_to((baseline_parent / "artifacts/runs").resolve()):
            raise ValueError("comparison baseline path escaped the locked baseline run directory")
        manifest_path = baseline_root / "launcher_manifest.json"
        capture_manifest_path = baseline_root / "capture/capture_manifest.json"
        route_path = baseline_root / "effective_route_config.json"
        if not all(path.is_file() for path in (manifest_path, capture_manifest_path, route_path)):
            raise ValueError(f"comparison baseline is incomplete in workspace and locked baseline: {baseline_id}")
    manifest = read_json(manifest_path)
    capture = read_json(capture_manifest_path)
    baseline_route = read_json(route_path)
    if manifest.get("status") != "completed" or manifest.get("route_alias") != "main":
        raise ValueError("comparison baseline must be a completed `main` route run")
    if manifest.get("scene_id") != ctx["dataset"].get("scene_id"):
        raise ValueError("comparison baseline scene_id differs from selected scene")
    if capture.get("resolution_px") != [160, 120] or route.get("resolution_px") != [320, 240]:
        raise ValueError("main_320 comparison requires baseline 160x120 and candidate 320x240")
    if capture.get("frame_count") != len(baseline_route.get("route", [])):
        raise ValueError("comparison baseline capture frame_count differs from its route")
    allowed_metadata_differences = {"resolution_px", "route_policy", "comparison_baseline_run_id"}
    old_core = {k: v for k, v in baseline_route.items() if k not in allowed_metadata_differences}
    new_core = {k: v for k, v in route.items() if k not in allowed_metadata_differences}
    if old_core != new_core:
        raise ValueError("main_320 must match baseline main route/config except resolution and comparison metadata")
    return {
        "run_id": baseline_id,
        "path": rel_or_abs(baseline_root),
        "launcher_manifest_sha256": sha256(manifest_path),
        "capture_manifest_sha256": sha256(capture_manifest_path),
        "effective_route_config_sha256": sha256(route_path),
        "resolution_px": capture.get("resolution_px"),
        "frame_count": capture.get("frame_count"),
    }


def _scene_context(entry: dict, route_alias: str | None = None) -> dict:
    profile_path = resolve_path(entry["evaluation_profile"])
    profile = read_json(profile_path)
    dataset = profile.get("dataset", {})
    route_alias = route_alias or entry["default_route"]
    if route_alias not in entry["routes"]:
        raise ValueError(f"route `{route_alias}` không đăng ký cho scene `{entry['alias']}`")
    route_path = resolve_path(entry["routes"][route_alias])
    route = read_json(route_path)
    kind = dataset.get("type")
    if kind not in {"hm3d", "replica_cad"}:
        raise ValueError(f"loader chưa hỗ trợ dataset: {kind!r}")
    if kind == "hm3d":
        scene_value = dataset.get("scene_file")
    else:
        scene_value = dataset.get("scene_instance")
    if route.get("dataset_type", kind) != kind:
        raise ValueError(f"route dataset_type không khớp evaluation profile của {entry['alias']}")
    if route.get("scene_id") and dataset.get("scene_id") and route["scene_id"] != dataset["scene_id"]:
        raise ValueError(f"route scene_id không khớp evaluation profile của {entry['alias']}")
    for key in ("scene_dataset_config", "navmesh_file"):
        if dataset.get(key) and route.get(key) and resolve_path(dataset[key]) != resolve_path(route[key]):
            # A route can intentionally use another dataset config for the same
            # dataset release; preflight's effective profile will reflect it.
            if key == "navmesh_file":
                raise ValueError(f"route navmesh không khớp evaluation profile của {entry['alias']}")
    return {"entry": entry, "profile_path": profile_path, "profile": profile,
            "route_alias": route_alias, "route_path": route_path, "route": route,
            "dataset": dataset, "scene_path": resolve_path(scene_value) if scene_value else None}


def _static_check(entry: dict, route_alias: str | None = None) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    warnings: list[str] = []
    try:
        ctx = _scene_context(entry, route_alias)
    except Exception as exc:
        return [str(exc)], warnings
    file_values = [ctx["profile_path"], ctx["route_path"], ctx["scene_path"],
                   ctx["dataset"].get("scene_dataset_config"), ctx["dataset"].get("navmesh_file"),
                   ctx["profile"].get("pipeline_config"), ctx["profile"].get("perception", {}).get("detector_profile"),
                   ctx["profile"].get("perception", {}).get("weights"), ctx["profile"].get("semantic_rules")]
    files = [v if isinstance(v, Path) else resolve_path(v) for v in file_values if v]
    for p in files:
        if not p or not p.is_file():
            missing.append(f"thiếu file: {p}")
    try:
        env = load_environment()
        for key in ("project_python", "habitat_python", "detection_3d_profile", "native_m2_m3", "native_dude", "resource_manifest"):
            p = Path(env[key])
            if not p.is_file():
                missing.append(f"thiếu {key}: {p}")
        for key in ("baseline_read_root", "habitat_data_root", "model_root", "native_dependency_prefix", "mapconversion_source"):
            p = Path(env[key])
            if not p.is_dir():
                missing.append(f"thiếu {key}: {p}")
        missing.extend(f"resource lock: {issue}" for issue in audit_external_resources(env))
    except Exception as exc:
        missing.append(f"local environment config lỗi: {exc}")
    if not ctx["route"].get("route"):
        missing.append("route không có waypoint")
    if ctx["dataset"].get("type") not in {"hm3d", "replica_cad"}:
        missing.append("loader dataset chưa hỗ trợ")
    if not missing:
        warnings.append("cấu hình và asset có mặt; cần chạy `check` để xác nhận dependency và navigability")
    return missing, warnings


class Launcher:
    def __init__(self, *, registry_path: Path = REGISTRY, env_path: Path = LOCAL_ENV):
        self.registry_path, self.env_path = registry_path, env_path
        self.active_thread: threading.Thread | None = None
        self.active_process: subprocess.Popen | None = None
        self.active_run: Path | None = None
        self.active_state: dict | None = None
        self.active_object_eval_thread: threading.Thread | None = None
        self.active_object_eval: dict | None = None
        self.active_fusion_thread: threading.Thread | None = None
        self.active_fusion: dict | None = None
        self.cancel_requested = threading.Event()
        self.selected: str | None = None
        self.registry = load_registry(registry_path)
        self.env = load_environment(env_path)

    def _save(self) -> None:
        if self.active_run is not None and self.active_state is not None:
            atomic_json(self.active_run / "launcher_manifest.json", self.active_state)
            if ACTIVE_POINTER.exists():
                atomic_json(ACTIVE_POINTER, self.active_state)

    def _update_stage(self, name: str, status: str, **extra: Any) -> None:
        assert self.active_state is not None
        stage = self.active_state["stages"].setdefault(name, {})
        stage.update({"status": status, "updated_utc": datetime.now(timezone.utc).isoformat(), **extra})
        if status == "running":
            stage.setdefault("started_utc", datetime.now(timezone.utc).isoformat())
            stage.setdefault("started_monotonic_ns", time.monotonic_ns())
        if status in {"pass", "failed", "cancelled", "skipped"}:
            stage["finished_utc"] = datetime.now(timezone.utc).isoformat()
            started_ns = stage.get("started_monotonic_ns")
            if started_ns:
                stage["elapsed_s"] = round((time.monotonic_ns() - started_ns) / 1e9, 6)
        self.active_state["current_stage"] = name if status == "running" else self.active_state.get("current_stage")
        self._save()

    def _subprocess(self, cmd: list[str], *, stage: str, log_path: Path, env: dict[str, str], cwd: Path = ROOT,
                    progress_prefix: str | None = None) -> tuple[int, str]:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            log.write("$ " + shlex.join(_safe_manifest_args(cmd)) + "\n")
            log.flush()
            progress_reader = log_path.open("r", encoding="utf-8", errors="replace") if progress_prefix else None
            if progress_reader: progress_reader.seek(0, os.SEEK_END)
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, text=True)
            self.active_process = proc
            def emit_progress() -> None:
                if not progress_reader: return
                while True:
                    line = progress_reader.readline()
                    if not line: break
                    if line.startswith(progress_prefix): print(f"{stage}: {line[len(progress_prefix):].strip()}", flush=True)
            try:
                while proc.poll() is None:
                    emit_progress()
                    if self.cancel_requested.wait(0.1):
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            proc.wait()
                        self.active_process = None
                        if progress_reader: progress_reader.close()
                        return 130, "đã yêu cầu hủy"
            except KeyboardInterrupt:
                self.cancel_requested.set()
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                self.active_process = None
                if progress_reader: progress_reader.close()
                return 130, "Ctrl+C: đã dừng process tree của stage"
            code = int(proc.returncode or 0)
            emit_progress()
            if progress_reader: progress_reader.close()
        self.active_process = None
        content = log_path.read_text(encoding="utf-8", errors="replace")
        return code, content[-3000:]

    def _run_logged_stage(self, stage: str, cmd: list[str], log_name: str, *, py_env: dict[str, str],
                          required: list[Path], run_root: Path, progress_prefix: str | None = None) -> None:
        self._update_stage(stage, "running", command=_safe_manifest_args(cmd), log=rel_or_abs(run_root / "logs" / log_name))
        code, tail = self._subprocess(cmd, stage=stage, log_path=run_root / "logs" / log_name,
                                      env=py_env, progress_prefix=progress_prefix)
        self.active_state["stages"][stage]["exit_code"] = code
        if code != 0:
            status = "cancelled" if code == 130 else "failed"
            self._update_stage(stage, status, error_tail=tail)
            raise RuntimeError(f"{stage} {status}: {tail[-1000:]}")
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            self._update_stage(stage, "failed", error_tail=f"runner exit 0 ma output contract thiếu: {missing}")
            raise RuntimeError(f"{stage} không tạo đủ output contract: {', '.join(missing)}")
        self._update_stage(stage, "pass", output_files=[rel_or_abs(p) for p in required])

    def _run_pipeline(self, alias: str, route_alias: str, dry_run: bool = False,
                      association_profile: str = "source") -> dict:
        if association_profile not in {"source", "geometry-only", "geometry-only-dedup-v2"}:
            raise ValueError("association profile must be source|geometry-only|geometry-only-dedup-v2")
        resource_issues = audit_external_resources(self.env)
        if resource_issues:
            raise RuntimeError("external input lock check failed: " + "; ".join(resource_issues))
        ctx = _scene_context(scene_entry(alias, self.registry), route_alias)
        profile, route, dataset = ctx["profile"], ctx["route"], ctx["dataset"]
        comparison_baseline = validate_comparison_baseline(ctx)
        profile_path, route_path = ctx["profile_path"], ctx["route_path"]
        scene_path = ctx["scene_path"]
        run_id = unique_run_id(alias)
        run_root = RUNS / run_id
        if run_root.exists():
            raise FileExistsError(f"run path đã tồn tại: {run_root}")
        if dry_run:
            plan = {"run_id": run_id, "scene": alias, "scene_id": dataset.get("scene_id"), "route": route_alias,
                    "association_profile": association_profile,
                    "graph_backend": {"source": "source", "geometry-only": "geometry-only",
                                      "geometry-only-dedup-v2": "geometry-only-dedup-v2"}[association_profile],
                    "room_labeler": "existing_rule_based_region_object_room_labeler",
                    "u4_ov_diagnostics": "off",
                    "route_config": rel_or_abs(route_path), "scene_asset": rel_or_abs(scene_path),
                    "capture_resolution_px": route.get("resolution_px"),
                    "comparison_baseline": comparison_baseline,
                    "environment": {k: self.env[k] for k in ("project_python", "habitat_python", "detection_3d_profile")},
                    "stages": ["preflight", "capture", "detection_2d", "detection_3d", "native_replay", "semantic", "evaluation"] + (["comparison"] if comparison_baseline else []),
                    "outputs": {name: rel_or_abs(run_root / name) for name in ("capture", "detection_2d", "detection_3d", "replay", "grid_trace", "semantic", "evaluation")} | ({"comparison": rel_or_abs(run_root / "comparison_to_baseline")} if comparison_baseline else {})}
            print(json.dumps(plan, indent=2, ensure_ascii=False))
            return plan

        RUNS.mkdir(parents=True, exist_ok=True)
        lock = LOCK_PATH.open("a+")
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("đang có một launcher run khác hoạt động; dùng `runs/status` để theo dõi") from exc
            preflight_path, temp_profile = self._temp_profile(scene_entry(alias, self.registry), route_alias)
            preflight_log = Path(tempfile.gettempdir()) / f"scene-launcher-preflight-{run_id}.log"
            pf_cmd = [self.env["project_python"], "-B", str(RUNNER / "preflight_habitat_run.py"),
                      "--profile", str(preflight_path), "--run-root", str(run_root),
                      "--habitat-python", self.env["habitat_python"],
                      "--m2-m3-binary", self.env["native_m2_m3"], "--dude-binary", self.env["native_dude"]]
            self.active_run = run_root
            self.active_state = {"schema_version": 1, "kind": "habitat_launcher_run", "run_id": run_id,
                "scene_alias": alias, "scene_id": dataset.get("scene_id"), "dataset": dataset.get("type"),
                "route_alias": route_alias, "association_profile": association_profile,
                "graph_backend": {"source": "source", "geometry-only": "geometry-only",
                                  "geometry-only-dedup-v2": "geometry-only-dedup-v2"}[association_profile],
                "room_labeler": "existing_rule_based_region_object_room_labeler",
                "u4_ov_diagnostics": "off",
                "status": "preflighting", "current_stage": "preflight",
                "capture_resolution_px": route.get("resolution_px"),
                "comparison_baseline_run_id": None if comparison_baseline is None else comparison_baseline["run_id"],
                "comparison_baseline": comparison_baseline,
                "started_utc": datetime.now(timezone.utc).isoformat(), "finished_utc": None,
                "input_paths": {"evaluation_profile": rel_or_abs(profile_path), "route_config": rel_or_abs(route_path), "scene_asset": rel_or_abs(scene_path)},
                "input_sha256": {}, "environment": {k: self.env[k] for k in ("project_python", "habitat_python", "detection_3d_profile", "native_m2_m3", "native_dude")},
                "stages": {"preflight": {"status": "running", "started_utc": datetime.now(timezone.utc).isoformat(), "log": str(preflight_log)}}}
            self.active_state["stages"]["preflight"]["started_monotonic_ns"] = time.monotonic_ns()
            self.active_state["run_root"] = rel_or_abs(run_root)
            atomic_json(ACTIVE_POINTER, self.active_state)
            py_probe = self._project_python_probe(resolve_path(profile["perception"]["detector_profile"]))
            if py_probe.get("status") != "pass":
                code, tail = 1, f"project Python environment check failed: {json.dumps(py_probe, ensure_ascii=False)}"
                preflight_log.write_text(tail + "\n", encoding="utf-8")
            else:
                self.active_state["environment_probe"] = py_probe
                code, tail = self._subprocess(pf_cmd, stage="preflight", log_path=preflight_log,
                                             env=self._stage_env(project=True))
            pf_report = None
            try:
                pf_report = json.loads(tail[tail.rfind("{"):])
            except Exception:
                # Runner writes the JSON as the final stdout block; parse from complete log.
                raw = preflight_log.read_text(encoding="utf-8", errors="replace")
                start = raw.find("{\n")
                if start >= 0:
                    try: pf_report = json.loads(raw[start:])
                    except Exception: pass
            run_root.mkdir(parents=True, exist_ok=False)
            (run_root / "logs").mkdir()
            (run_root / "preflight.json").write_text(json.dumps(pf_report or {"status": "fail", "log_tail": tail}, indent=2) + "\n", encoding="utf-8")
            shutil_copy(preflight_log, run_root / "logs/preflight.log")
            effective_profile = read_json(preflight_path) if temp_profile else profile
            effective_route = json.loads(json.dumps(route))
            for resource_key in ("scene_dataset_config", "navmesh_file"):
                if effective_route.get(resource_key):
                    effective_route[resource_key] = str(resolve_path(effective_route[resource_key]))
            atomic_json(run_root / "effective_route_config.json", effective_route)
            eval_profile_path = run_root / "effective_evaluation_profile.json"
            atomic_json(eval_profile_path, effective_profile)
            configured_hash_paths = [profile_path, route_path, scene_path,
                resolve_path(dataset.get("scene_dataset_config")) if dataset.get("scene_dataset_config") else None,
                resolve_path(dataset.get("navmesh_file")) if dataset.get("navmesh_file") else None,
                resolve_path(profile.get("pipeline_config")) if profile.get("pipeline_config") else None,
                resolve_path(profile.get("perception", {}).get("detector_profile")) if profile.get("perception", {}).get("detector_profile") else None,
                resolve_path(profile.get("perception", {}).get("weights")) if profile.get("perception", {}).get("weights") else None,
                resolve_path(profile.get("semantic_rules")) if profile.get("semantic_rules") else None,
                Path(self.env["detection_3d_profile"]), Path(self.env["native_m2_m3"]), Path(self.env["native_dude"]),
                self.env_path, eval_profile_path]
            if association_profile in {"geometry-only", "geometry-only-dedup-v2"}:
                configured_hash_paths.append(ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json")
            if association_profile == "geometry-only-dedup-v2":
                configured_hash_paths.append(ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json")
            self.active_state["input_sha256"] = {rel_or_abs(p): sha256(p) for p in configured_hash_paths if p and p.is_file()}
            self.active_state["input_sha256"][rel_or_abs(run_root / "effective_route_config.json")] = sha256(run_root / "effective_route_config.json")
            self.active_state["effective_config"] = {
                "route_config_path": rel_or_abs(run_root / "effective_route_config.json"),
                "route_config_sha256": sha256(run_root / "effective_route_config.json"),
                "capture_resolution_px": route.get("resolution_px"),
                "association_profile": association_profile,
                "graph_backend": self.active_state["graph_backend"],
                "room_labeler": self.active_state["room_labeler"],
                "u4_ov_diagnostics": "off",
            }
            if comparison_baseline:
                for field in ("launcher_manifest", "capture_manifest", "effective_route_config"):
                    path = Path(comparison_baseline["path"]) / {
                        "launcher_manifest": "launcher_manifest.json",
                        "capture_manifest": "capture/capture_manifest.json",
                        "effective_route_config": "effective_route_config.json",
                    }[field]
                    self.active_state["input_sha256"][rel_or_abs(path)] = sha256(path)
            self.active_state["stages"]["preflight"].update({"status": "pass" if code == 0 and pf_report and pf_report.get("status") == "pass" else "failed",
                "command": _safe_manifest_args(pf_cmd), "exit_code": code,
                "finished_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": round((time.monotonic_ns() - self.active_state["stages"]["preflight"]["started_monotonic_ns"]) / 1e9, 6),
                "log": "logs/preflight.log"})
            self.active_state["preflight_status"] = (pf_report or {}).get("status", "unreadable")
            if pf_report:
                habitat = next((row for row in pf_report.get("checks", []) if row.get("label") == "Habitat-Sim import"), {})
                self.active_state.setdefault("environment_versions", {})["habitat_sim"] = habitat.get("version")
            if code != 0 or not pf_report or pf_report.get("status") != "pass":
                raise RuntimeError(f"preflight failed: {tail[-1200:]}")
            if temp_profile:
                temp_profile.unlink(missing_ok=True)
            self.active_state["status"] = "running"
            self.active_state["run_root"] = rel_or_abs(run_root)
            self._save()

            common_env = self._stage_env(project=True)
            cap_dir, d2_dir, d3_dir = (run_root / n for n in ("capture", "detection_2d", "detection_3d"))
            replay_dir, grid_dir, semantic_dir = (run_root / n for n in ("replay", "grid_trace", "semantic"))
            scene_arg = dataset.get("scene_file") or dataset.get("scene_instance")
            self._run_logged_stage("capture", [self.env["habitat_python"], "-B", str(RUNNER / "capture_habitat_replica.py"),
                "--scene", str(resolve_path(scene_arg)), "--config", str(run_root / "effective_route_config.json"), "--output", str(cap_dir)],
                "capture.log", py_env=common_env, required=[cap_dir / "capture_manifest.json"], run_root=run_root,
                progress_prefix="PROGRESS:")
            cap_manifest = read_json(cap_dir / "capture_manifest.json")
            if cap_manifest.get("status") != "pass" or cap_manifest.get("frame_count") != len(route.get("route", [])):
                raise RuntimeError("capture contract mismatch: status/frame_count không khớp route")

            detector_cfg = resolve_path(profile["perception"]["detector_profile"])
            weights = resolve_path(profile["perception"]["weights"])
            self._run_logged_stage("detection_2d", [self.env["project_python"], "-B", str(RUNNER / "run_replica_cad_instance_segmentation.py"),
                "--capture", str(cap_dir), "--output", str(d2_dir), "--config", str(detector_cfg), "--weights", str(weights),
                "--names", str(ROOT / "configs/perception/coco.names"), "--resource-manifest", self.env["resource_manifest"]], "detection_2d.log", py_env=common_env,
                required=[d2_dir / "detection_2d_index.json"], run_root=run_root, progress_prefix="PROGRESS:")
            d2 = read_json(d2_dir / "detection_2d_index.json")
            if d2.get("status") != "pass" or d2.get("frame_count") != cap_manifest["frame_count"]:
                raise RuntimeError("P1 output contract không khớp số capture frame")

            self._run_logged_stage("detection_3d", [self.env["project_python"], "-B", str(RUNNER / "project_detection_masks_to_3d.py"),
                "--capture", str(cap_dir), "--detection-2d", str(d2_dir), "--output", str(d3_dir),
                "--profile", self.env["detection_3d_profile"]], "detection_3d.log", py_env=common_env,
                required=[d3_dir / "detection_3d_manifest.json", d3_dir / "detection_3d.jsonl", d3_dir / "object_evidence_ledger.jsonl"],
                run_root=run_root, progress_prefix="PROGRESS:")
            d3 = read_json(d3_dir / "detection_3d_manifest.json")
            if d3.get("status") != "pass" or d3.get("frame_count") != cap_manifest["frame_count"]:
                raise RuntimeError("P3 output contract không khớp số capture frame")

            replay_cmd = [self.env["project_python"], "-B", str(RUNNER / "run_habitat_native_replay.py"),
                "--capture", str(cap_dir), "--detection-3d", str(d3_dir / "detection_3d.jsonl"),
                "--detection-3d-manifest", str(d3_dir / "detection_3d_manifest.json"),
                "--resource-manifest", self.env["resource_manifest"],
                "--pipeline-config", str(resolve_path(profile["pipeline_config"])), "--m2-m3-binary", self.env["native_m2_m3"],
                "--dude-binary", self.env["native_dude"], "--object-backend", association_profile,
                "--output", str(replay_dir), "--grid-output", str(grid_dir)]
            if association_profile in {"geometry-only", "geometry-only-dedup-v2"}:
                replay_cmd.extend(["--detection-2d-index", str(d2_dir / "detection_2d_index.json"),
                                   "--u3-config-lock", str(ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json")])
            if association_profile == "geometry-only-dedup-v2":
                replay_cmd.extend(["--duplicate-v2-config", str(ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json")])
                offline_reference = find_duplicate_v2_reference(run_root)
                if offline_reference is not None:
                    replay_cmd.extend(["--duplicate-v2-reference", str(offline_reference)])
            self._run_logged_stage("native_replay", replay_cmd, "native_replay.log", py_env=common_env,
                required=[replay_dir / "manifest.json", replay_dir / "snapshot.json", replay_dir / "delta_log.json",
                          replay_dir / "spatial_map.png", replay_dir / "trajectory.csv", grid_dir / "index.json"],
                run_root=run_root, progress_prefix="PROGRESS:")
            rep = read_json(replay_dir / "manifest.json")
            if rep.get("pipeline_status") != "pass" or rep.get("frames") != cap_manifest["frame_count"]:
                raise RuntimeError("native replay output contract không pass hoặc frame_count không khớp")

            self._run_logged_stage("semantic", [self.env["project_python"], "-B", str(RUNNER / "run_region_object_room_labels.py"),
                "--replay", str(replay_dir), "--trace", str(grid_dir), "--detection-3d", str(d3_dir),
                "--detection-2d", str(d2_dir), "--capture", str(cap_dir), "--config", str(resolve_path(profile["semantic_rules"])),
                "--output", str(semantic_dir)], "semantic.log", py_env=common_env,
                required=[semantic_dir / "manifest.json", semantic_dir / "region_labels.json", semantic_dir / "report.vi.md",
                          semantic_dir / "spatial_semantic_map.png"], run_root=run_root)
            sem = read_json(semantic_dir / "manifest.json")
            if sem.get("status") != "pass" or sem.get("frames") != cap_manifest["frame_count"]:
                raise RuntimeError("semantic sidecar contract không pass/frame_count mismatch")

            eval_dir = run_root / "evaluation"
            eval_cmd = [self.env["project_python"], "-B", str(RUNNER / "evaluate_habitat_run.py"),
                "--capture", str(cap_dir), "--replay", str(replay_dir), "--grid-trace", str(grid_dir),
                "--semantic", str(semantic_dir), "--run-config", str(route_path), "--evaluation-profile", str(eval_profile_path),
                "--output", str(eval_dir), "--run-id", run_id]
            room_gt = profile.get("ground_truth", {}).get("room_polygons")
            if room_gt:
                eval_cmd.extend(["--room-gt", str(resolve_path(room_gt))])
            self._run_logged_stage("evaluation", eval_cmd, "evaluation.log", py_env=common_env,
                required=[eval_dir / "manifest.json", eval_dir / "metrics.json", eval_dir / "report.vi.md"], run_root=run_root)
            ev = read_json(eval_dir / "manifest.json")
            if ev.get("status") not in {"pass", "partial"}:
                raise RuntimeError("evaluation manifest status không hợp lệ")
            if comparison_baseline:
                comparison_dir = run_root / "comparison_to_baseline"
                compare_cmd = [self.env["project_python"], "-B", str(RUNNER / "compare_hm3d_resolution_runs.py"),
                    "--baseline", comparison_baseline["path"], "--candidate", str(run_root),
                    "--output", str(comparison_dir)]
                self._run_logged_stage("comparison", compare_cmd, "comparison.log", py_env=common_env,
                    required=[comparison_dir / "report.vi.md", comparison_dir / "resolution_side_by_side.png",
                              comparison_dir / "manifest.json"], run_root=run_root)
            self.active_state["status"] = "completed"
            self.active_state["finished_utc"] = datetime.now(timezone.utc).isoformat()
            self.active_state["current_stage"] = None
            self.active_state["summary"] = self._summarize(run_root)
            self.active_state["summary"]["association_profile"] = association_profile
            self._save()
            ACTIVE_POINTER.unlink(missing_ok=True)
            return self.active_state
        except Exception as exc:
            if self.active_state is None:
                raise
            if self.active_run and not self.active_run.exists():
                self.active_run.mkdir(parents=True, exist_ok=True)
                (self.active_run / "logs").mkdir(exist_ok=True)
            status = "cancelled" if self.cancel_requested.is_set() else "failed"
            self.active_state["status"] = status
            self.active_state["error"] = f"{type(exc).__name__}: {exc}"
            self.active_state["finished_utc"] = datetime.now(timezone.utc).isoformat()
            if self.active_run:
                self._save()
            ACTIVE_POINTER.unlink(missing_ok=True)
            print(f"Run {status}: {exc}\nRun ID: {run_id}\nPartial/output directory: {self.active_run}")
            return self.active_state
        finally:
            if "temp_profile" in locals() and temp_profile:
                temp_profile.unlink(missing_ok=True)
            lock.close()

    def _stage_env(self, *, project: bool) -> dict[str, str]:
        env = os.environ.copy()
        env["MPLCONFIGDIR"] = self.env["mplconfigdir"]
        configured_paths = [str(resolve_path(p)) for p in self.env.get("pythonpath", [".", "src"])]
        env["PYTHONPATH"] = os.pathsep.join(configured_paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if project:
            Path(env["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
        return env

    def _project_python_probe(self, detector_profile: Path | None = None) -> dict:
        code = """
import importlib, importlib.metadata as md, io, json, sys, traceback
specs = {'numpy': ('numpy', 'numpy'), 'PIL': ('Pillow', 'Pillow'),
         'yaml': ('PyYAML', 'PyYAML'), 'matplotlib': ('matplotlib', 'matplotlib'),
         'onnxruntime': ('onnxruntime', 'onnxruntime')}
modules, versions, errors = {}, {}, {}
for module, (dist, _) in specs.items():
    try:
        imported = importlib.import_module(module)
        modules[module] = True
        try: versions[module] = md.version(dist)
        except md.PackageNotFoundError: versions[module] = getattr(imported, '__version__', None)
    except Exception as exc:
        modules[module] = False
        errors[module] = f'{type(exc).__name__}: {exc}'
plot_smoke = False
if modules.get('matplotlib') and modules.get('numpy'):
    try:
        import matplotlib
        matplotlib.use('Agg', force=True)
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(1, 1)); ax.plot([0, 1], [0, 1])
        buf = io.BytesIO(); fig.savefig(buf, format='png'); plt.close(fig)
        plot_smoke = buf.getvalue().startswith(b'\\x89PNG')
    except Exception as exc:
        errors['matplotlib_plot_smoke'] = f'{type(exc).__name__}: {exc}'
print(json.dumps({'executable': sys.executable, 'version': sys.version.split()[0],
                  'modules': modules, 'versions': versions, 'import_errors': errors,
                  'matplotlib_plot_smoke': plot_smoke}))
"""
        result = subprocess.run([self.env["project_python"], "-B", "-c", code], cwd=ROOT,
                                text=True, capture_output=True, check=False)
        if result.returncode:
            return {"status": "fail", "error": result.stderr[-1500:], "executable": self.env["project_python"]}
        try:
            report = json.loads(result.stdout.strip().splitlines()[-1])
        except Exception:
            return {"status": "fail", "error": "không đọc được kết quả kiểm tra Python", "stdout": result.stdout[-1000:]}
        report["missing"] = [module for module, present in report["modules"].items() if not present]
        pins = self.env["project_package_pins"]
        module_by_distribution = {"PyYAML": "yaml", "Pillow": "PIL"}
        pin_mismatches: dict[str, str] = {}
        for distribution, expected_version in pins.items():
            module = module_by_distribution.get(distribution, distribution.lower())
            actual_version = report.get("versions", {}).get(module)
            if actual_version and actual_version != expected_version:
                issue = f"{distribution} version mismatch (expected {expected_version}, found {actual_version})"
                report["missing"].append(issue)
                pin_mismatches[module] = issue
        if not report.get("matplotlib_plot_smoke"):
            report["missing"].append("matplotlib headless render smoke failed")
        profile = read_json(detector_profile or resolve_path("configs/perception/yolo11n_seg_replica_cad_dev.json"))
        expected = profile.get("runtime", {}).get("onnxruntime_version")
        actual = report.get("versions", {}).get("onnxruntime")
        if expected and actual and expected != actual:
            issue = f"onnxruntime version mismatch (detector expects {expected}, found {actual})"
            report["missing"].append(issue)
            pin_mismatches["onnxruntime"] = issue
        required = {
            "perception": ("numpy", "PIL", "onnxruntime"),
            "replay": ("numpy", "PIL", "yaml", "matplotlib"),
            "semantic": ("numpy", "PIL", "yaml", "matplotlib"),
            "evaluation": ("numpy", "PIL", "matplotlib"),
        }
        report["capabilities"] = {
            name: {"status": "pass" if all(report["modules"].get(m) for m in modules)
                    and not any(m in pin_mismatches for m in modules)
                    and (name not in {"semantic", "evaluation", "replay"} or report.get("matplotlib_plot_smoke"))
                    else "fail", "modules": list(modules),
                   "issues": [pin_mismatches[m] for m in modules if m in pin_mismatches]
                            + [report.get("import_errors", {}).get(m) for m in modules
                               if report.get("import_errors", {}).get(m)]
                            + (["matplotlib headless render smoke failed"]
                               if name in {"semantic", "evaluation", "replay"}
                               and not report.get("matplotlib_plot_smoke") else [])}
            for name, modules in required.items()
        }
        report["status"] = "pass" if not report["missing"] else "fail"
        return report

    def _managed_runs(self) -> list[tuple[Path, dict]]:
        rows = []
        if not RUNS.is_dir():
            return rows
        for p in sorted(RUNS.iterdir(), key=lambda p: p.name, reverse=True):
            mf = p / "launcher_manifest.json"
            if p.is_dir() and mf.is_file():
                try: rows.append((p, read_json(mf)))
                except Exception: continue
        return rows

    def _recover_interrupted(self) -> None:
        lock_path = LOCK_PATH
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if ACTIVE_POINTER.is_file():
                try:
                    pointer = read_json(ACTIVE_POINTER)
                    run_id = pointer.get("run_id")
                    if not isinstance(run_id, str) or not run_id or "/" in run_id or run_id.startswith("."):
                        return
                    run_path = RUNS / run_id
                    if pointer.get("status") in {"running", "preflighting"}:
                        if not run_path.exists():
                            run_path.mkdir(parents=True, exist_ok=False)
                            (run_path / "logs").mkdir()
                        pointer["status"] = "interrupted"
                        pointer["finished_utc"] = datetime.now(timezone.utc).isoformat()
                        pointer["error"] = "launcher process ended before terminal status; no automatic resume"
                        atomic_json(run_path / "launcher_manifest.json", pointer)
                    ACTIVE_POINTER.unlink(missing_ok=True)
                except Exception:
                    # Do not erase a pointer whose identity or contents are unclear.
                    pass
            for p, doc in self._managed_runs():
                if doc.get("status") in {"running", "preflighting"}:
                    doc["status"] = "interrupted"
                    doc["finished_utc"] = datetime.now(timezone.utc).isoformat()
                    doc["error"] = "launcher process ended before terminal status; no automatic resume"
                    atomic_json(p / "launcher_manifest.json", doc)
                jobs = p / "object_evaluations" / ".jobs"
                if jobs.is_dir():
                    for job_path in jobs.glob("*.json"):
                        try:
                            job = read_json(job_path)
                            if job.get("status") in {"queued", "running"}:
                                job.update({"status": "interrupted", "finished_utc": datetime.now(timezone.utc).isoformat(),
                                            "error": "launcher stopped before terminal evaluator status; predictor run unchanged"})
                                atomic_json(job_path, job)
                        except Exception:
                            continue

    def _find_run(self, run_id: str | None) -> tuple[Path, dict]:
        if not run_id:
            if self.active_run and self.active_state:
                return self.active_run, self.active_state
            rows = self._managed_runs()
            if not rows: raise ValueError("chưa có run nào do launcher quản lý")
            return rows[0]
        if "/" in run_id or run_id.startswith("."):
            raise ValueError("run-id chỉ được là ID, không nhận path")
        path = RUNS / run_id
        mf = path / "launcher_manifest.json"
        if not mf.is_file(): raise ValueError(f"không tìm thấy launcher run `{run_id}`")
        return path, read_json(mf)

    def _summarize(self, run_root: Path) -> dict:
        result: dict[str, Any] = {"processed_frames": None, "regions": None, "room_nodes": None, "objects": None,
                                  "predicted_labels": None, "unknown_labels": None, "ambiguous_labels": None,
                                  "capture_resolution_px": None, "p3_accepted": None, "p3_rejected": None,
                                  "duplicate_aliases": None, "duplicate_ambiguous": None,
                                  "association_deferred": None,
                                  "metrics_report": rel_or_abs(run_root / "evaluation/report.vi.md"),
                                  "spatial_map": rel_or_abs(run_root / "replay/spatial_map.png"),
                                  "object_evidence": rel_or_abs(run_root / "semantic/region_object_evidence.html"),
                                  "comparison_report": rel_or_abs(run_root / "comparison_to_baseline/report.vi.md")}
        try:
            manifest = read_json(run_root / "launcher_manifest.json")
            result["comparison_baseline_run_id"] = manifest.get("comparison_baseline_run_id")
        except Exception:
            result["comparison_baseline_run_id"] = None
        try:
            capture = read_json(run_root / "capture/capture_manifest.json")
            result["processed_frames"] = capture.get("frame_count")
            result["capture_resolution_px"] = capture.get("resolution_px")
        except Exception: pass
        try:
            p3 = read_json(run_root / "detection_3d/detection_3d_manifest.json")
            result["p3_accepted"] = p3.get("detection_3d_count")
            result["p3_rejected"] = p3.get("rejected_detection_count")
        except Exception: pass
        try:
            replay = read_json(run_root / "replay/manifest.json")
            duplicate = replay.get("same_frame_duplicate_summary") or {}
            result["duplicate_aliases"] = duplicate.get("aliased_observations")
            result["duplicate_ambiguous"] = duplicate.get("ambiguous_observations")
            result["association_deferred"] = duplicate.get("association_deferred_observations")
        except Exception: pass
        try:
            snap = read_json(run_root / "replay/snapshot.json").get("snapshot", {})
            nodes = snap.get("nodes", [])
            result["room_nodes"] = sum(1 for n in nodes if n.get("kind") == "room")
            result["objects"] = sum(1 for n in nodes if n.get("kind") == "object")
            diagnostics = read_json(run_root / "replay/frame_diagnostics.json")
            result["regions"] = diagnostics[-1].get("m4_region_count") if diagnostics else None
        except Exception: pass
        try:
            labels = read_json(run_root / "semantic/region_labels.json")
            counts = {s: sum(1 for row in labels if row.get("status") == s) for s in ("predicted", "unknown", "ambiguous")}
            result["predicted_labels"], result["unknown_labels"], result["ambiguous_labels"] = counts.values()
        except Exception: pass
        return result

    def handle(self, argv: list[str], *, interactive: bool = False) -> int:
        if not argv:
            return self.interactive()
        command, args = argv[0].lstrip("/"), argv[1:]
        if command == "all": command = "help"
        if command not in COMMANDS:
            print(f"Lệnh không rõ: {argv[0]}. Dùng `help` hoặc `all`.")
            return 2
        try:
            if command == "help": self.cmd_help(args)
            elif command == "maps": self.cmd_maps(args)
            elif command == "use": self.cmd_use(args)
            elif command == "info": self.cmd_info(args)
            elif command == "routes": self.cmd_routes(args)
            elif command == "check": return self.cmd_check(args)
            elif command == "run": return self.cmd_run(args, interactive=interactive)
            elif command == "replay-cached": return self.cmd_replay_cached(args)
            elif command == "compare-profiles": return self.cmd_compare_profiles(args)
            elif command == "fuse-objects": return self.cmd_fuse_objects(args, interactive=interactive)
            elif command == "compare-object-fusions": return self.cmd_compare_object_fusions(args)
            elif command == "unified-graph": return self.cmd_unified_graph(args)
            elif command == "hybrid-map": return self.cmd_hybrid_map(args)
            elif command == "cached-replay": return self.cmd_cached_replay(args)
            elif command == "fresh-replica": return self.cmd_fresh_replica(args)
            elif command == "menu": return self.cmd_scene_menu(args)
            elif command == "scene-run": return self.cmd_scene_run(args, interactive=interactive)
            elif command == "camera-preview": return self.cmd_camera_preview(args)
            elif command == "live-replay": return self.cmd_live_replay(args)
            elif command == "start-live": return self.cmd_start_live(args)
            elif command == "runs": self.cmd_runs(args)
            elif command == "status": self.cmd_status(args)
            elif command == "logs": self.cmd_logs(args)
            elif command == "results": self.cmd_results(args)
            elif command == "open": return self.cmd_open(args)
            elif command == "evaluate": return self.cmd_evaluate(args)
            elif command == "evaluate-objects": return self.cmd_evaluate_objects(args, interactive=interactive)
            elif command == "object-results": return self.cmd_object_results(args)
            elif command == "cancel": return self.cmd_cancel(args)
            elif command == "register": self.cmd_register(args)
            elif command == "exit": return 10 if interactive else 0
            return 0
        except Exception as exc:
            print(f"Lỗi: {exc}")
            return 2

    def cmd_help(self, args: list[str]) -> None:
        if args and args[0] not in COMMANDS:
            print(f"Không có lệnh `{args[0]}`. Lệnh: {', '.join(COMMANDS)}"); return
        if args:
            usage = {
                "maps": "maps [--scene alias]", "use": "use <scene-alias>", "info": "info [scene-alias]",
                "routes": "routes [scene-alias]", "check": "check [scene-alias] [--route route-alias] | check --native-resources",
                "run": "run [scene-alias] [--route route-alias] [--profile source|geometry-only|geometry-only-dedup-v2] [--dry-run]", "runs": "runs [--scene alias]",
                "replay-cached": "replay-cached <run-id-or-path> [--profile source|geometry-only|geometry-only-dedup-v2] [--semantic off|u4ov]",
                "compare-profiles": "compare-profiles <input-run-id> --candidate geometry-only-dedup-v2 [--baseline source]",
                "fuse-objects": "fuse-objects <run-id> [--check]",
                "compare-object-fusions": "compare-object-fusions <source-fusion-id> <candidate-fusion-id>",
                "unified-graph": "unified-graph --room-snapshot <snapshot.json> --object-export <export-dir> [--source-kind fixture|legacy|reference] [--out <new-output-dir>]",
                "hybrid-map": "hybrid-map --cache <cached-perception-run> --text-features <sidecar-dir> --room-snapshot <snapshot.json> [--seed N] [--run-id ID]",
                "cached-replay": "cached-replay --config <cached-replay-inputs.json> [--out <new-run-dir>] [--max-points N]",
                "fresh-replica": "fresh-replica --config <scene-route-c2.json> [--run-id ID] [--reuse-from FAILED_C2_RUN]",
                "menu": "menu [--port PORT] — chọn dataset/scene/route, xem preflight, xác nhận Run, theo dõi C2→D1/D2",
                "scene-run": "scene-run --dataset ID --scene ID --route ID [--port PORT] [--yes]",
                "camera-preview": "camera-preview --dataset ID --scene ID --route ID --from-index N --to-index N [--run-id ID] [--timeout-s SEC] — camera-only smooth Habitat preview; no perception/mapping",
                "live-replay": "live-replay --source-run-id <completed-C2-run> [--run-id ID] [--port PORT] [--xyz-viewer] [--viewer-base-url http://127.0.0.1:PORT]",
                "start-live": "start-live <d1-run-id> --confirm-browser-ready [--port PORT]",
                "status": "status [run-id]", "logs": "logs [run-id] [--stage stage] [--tail N]",
                "results": "results [run-id]", "open": "open [run-id-or-comparison-id-or-hybrid-id] [report|comparison|map|objects|labels|u5|fusion|fusion-comparison|unified|hybrid|timeline|live|object-evaluation|object-review|association-report|association-gallery|folder] [--port PORT]",
                "evaluate": "evaluate <run-id>", "cancel": "cancel [run-id]", "register": "register --config <scene-entry.json>",
                "evaluate-objects": "evaluate-objects <run-id> [--check]",
                "object-results": "object-results <run-id> [--frames | --frame N] [--eval-id ID] [--json]",
            }
            print(f"Cú pháp: {usage.get(args[0], args[0])}")
            return
        print("Workflow: maps → info → check → run → status/logs/results/open; cache replay: check --native-resources → replay-cached → results/open.\n"
              "Lệnh: help [command], all, maps, use <alias>, info [alias], routes [alias],\n"
              "check [alias] [--route name] | check --native-resources, run [alias] [--route name] [--profile source|geometry-only|geometry-only-dedup-v2] [--dry-run],\n"
              "replay-cached <run-id-or-path> [--profile source|geometry-only|geometry-only-dedup-v2] [--semantic off|u4ov],\n"
              "compare-profiles <input-run-id> --candidate geometry-only-dedup-v2 [--baseline source], fuse-objects <run-id> [--check],\n"
              "compare-object-fusions <source-fusion-id> <candidate-fusion-id>,\n"
              "unified-graph --room-snapshot <snapshot.json> --object-export <export-dir> [--source-kind fixture|legacy|reference] [--out <new-output-dir>],\n"
              "hybrid-map --cache <cached-perception-run> --text-features <sidecar-dir> --room-snapshot <snapshot.json> [--seed N] [--run-id ID],\n"
              "cached-replay --config <cached-replay-inputs.json> [--out <new-run-dir>] [--max-points N],\n"
              "fresh-replica --config <scene-route-c2.json> [--run-id ID] [--reuse-from FAILED_C2_RUN],\n"
              "menu, scene-run --dataset ID --scene ID --route ID [--port PORT] [--yes],\n"
              "camera-preview --dataset ID --scene ID --route ID --from-index N --to-index N [--run-id ID] [--timeout-s SEC],\n"
              "live-replay --source-run-id <completed-C2-run> [--run-id ID] [--port PORT] [--xyz-viewer] [--viewer-base-url http://127.0.0.1:PORT],\n"
              "start-live <d1-run-id> --confirm-browser-ready [--port PORT],\n"
              "runs [--scene alias], status [run-id], logs [run-id] [--stage stage] [--tail N],\n"
              "results [run-id], open [run-id-or-comparison-id-or-hybrid-id] [report|comparison|map|objects|labels|u5|fusion|fusion-comparison|unified|hybrid|timeline|live|object-evaluation|object-review|association-report|association-gallery|folder] [--port PORT],\n"
              "evaluate <run-id>, evaluate-objects <run-id> [--check [--json]], object-results <run-id> [--frames|--frame N] [--eval-id ID] [--json], cancel [run-id], register --config <scene-entry.json>, exit.\n"
              "Interactive: /help, /all, /maps, /use hm3d_00802, /info, /check, /run, /status, /logs, /results, /open, /exit.\n"
              "`all` chỉ là help đầy đủ; không chạy toàn bộ map. Route hiện là fixed GT-pose observations, không autonomous navigation.")

    def cmd_maps(self, args: list[str]) -> None:
        scene_filter = None
        if args:
            if len(args) == 2 and args[0] == "--scene": scene_filter = args[1]
            else: raise ValueError("cú pháp: maps [--scene alias]")
        for entry in self.registry["scenes"]:
            if scene_filter and entry["alias"] != scene_filter: continue
            missing, warn = _static_check(entry)
            try:
                ctx = _scene_context(entry)
                identity = f"{ctx['dataset'].get('type')} | {ctx['dataset'].get('scene_id')}"
            except Exception as exc:
                identity = f"profile/route lỗi: {exc}"
            status = "đã đăng ký, thiếu cấu hình/asset" if missing else "đã cấu hình; cần check (chưa xác minh navigability)"
            print(f"{entry['alias']} | {identity} | routes={','.join(entry['routes'])} | {status}")
            for issue in missing: print(f"  - {issue}")
        if scene_filter and not any(x["alias"] == scene_filter for x in self.registry["scenes"]):
            raise ValueError(f"alias không tồn tại: {scene_filter}")

    def cmd_use(self, args: list[str]) -> None:
        if len(args) != 1: raise ValueError("cú pháp: use <scene-alias>")
        entry = scene_entry(args[0], self.registry)
        self.selected = args[0]
        print(f"Đã chọn {args[0]} — route mặc định {entry['default_route']}. Chỉ đổi lựa chọn phiên, chưa chạy capture.")

    def _selected_alias(self, args: list[str]) -> tuple[str, list[str]]:
        if args: return args[0], args[1:]
        if self.selected: return self.selected, []
        raise ValueError("chưa chọn scene; dùng `use <alias>` hoặc truyền alias trực tiếp")

    def cmd_info(self, args: list[str]) -> None:
        alias, tail = self._selected_alias(args)
        if tail: raise ValueError("cú pháp: info [alias]")
        entry = scene_entry(alias, self.registry)
        ctx = _scene_context(entry)
        p, d, r = ctx["profile"], ctx["dataset"], ctx["route"]
        print(f"{alias} — {d.get('type')} {d.get('version')} / {d.get('scene_id')} / {d.get('floor_id','floor unspecified')}")
        print(f"Scene asset: {ctx['scene_path']} ({'OK' if ctx['scene_path'] and ctx['scene_path'].is_file() else 'MISSING'})")
        print(f"Dataset config: {resolve_path(d.get('scene_dataset_config',''))}")
        print(f"Route mặc định: {entry['default_route']} — {len(r.get('route',[]))} pose — {ctx['route_path']}")
        print(f"Sensor: {r.get('resolution_px')} px, HFOV {r.get('horizontal_fov_deg')}°, profile {p.get('sensor',{}).get('profile_id')}")
        for route_name, route_config in entry["routes"].items():
            route_doc = read_json(resolve_path(route_config))
            if route_name != entry["default_route"]:
                print(f"Route {route_name}: {len(route_doc.get('route', []))} pose, {route_doc.get('resolution_px')} px, HFOV {route_doc.get('horizontal_fov_deg')}°")
        print(f"Pipeline: {resolve_path(p.get('pipeline_config',''))}; detector: {resolve_path(p.get('perception',{}).get('detector_profile',''))}; semantic: {resolve_path(p.get('semantic_rules',''))}")
        print(f"GT scope: {p.get('workflow_mode')} — {p.get('gt_scope', p.get('ground_truth',{}).get('note',''))}")
        print(f"Pose source: {p.get('route_pose_source','fixed GT poses')}; not autonomous navigation/SLAM.")

    def cmd_routes(self, args: list[str]) -> None:
        alias, tail = self._selected_alias(args)
        if tail: raise ValueError("cú pháp: routes [alias]")
        entry = scene_entry(alias, self.registry)
        for name, path in entry["routes"].items():
            rpath = resolve_path(path)
            route = read_json(rpath) if rpath.is_file() else {}
            n = len(route.get("route", [])) if route else "?"
            print(f"{name}: {rpath} ({n} observation poses; resolution={route.get('resolution_px','?')} px; HFOV={route.get('horizontal_fov_deg','?')}°)")

    def _temp_profile(self, entry: dict, route_alias: str) -> tuple[Path, Path | None]:
        ctx = _scene_context(entry, route_alias)
        base = ctx["profile"]
        route = ctx["route"]
        altered = json.loads(json.dumps(base))
        altered["route_config"] = rel_or_abs(ctx["route_path"])
        # A selected route controls its own dataset paths and sensor profile.
        # HM3D's smoke and main routes, for example, intentionally use distinct
        # dataset config paths while sharing the same scene instance.
        for key in ("scene_dataset_config", "navmesh_file"):
            if route.get(key):
                altered["dataset"][key] = str(resolve_path(route[key]))
        for key in ("resolution_px", "horizontal_fov_deg"):
            if key in route:
                altered["sensor"][key] = route[key]
        if altered == base:
            return ctx["profile_path"], None
        temp = Path(tempfile.gettempdir()) / f"scene-launcher-check-{uuid.uuid4().hex}.json"
        temp.write_text(json.dumps(altered, indent=2) + "\n", encoding="utf-8")
        return temp, temp

    def cmd_check(self, args: list[str]) -> int:
        if args == ["--native-resources"]:
            lock_path = Path(self.env["resource_manifest"])
            lock = read_json(lock_path)
            if lock.get("status") != "bound_input_only" or Path(lock.get("workspace_root", "")).resolve() != ROOT:
                raise ValueError("external resource lock is not bound to this workspace")
            checks = []
            for name, env_key in (("native_m2_m3", "native_m2_m3"), ("native_dude", "native_dude")):
                record = lock.get("files", {}).get(name)
                path = Path(self.env[env_key]).resolve()
                if not record or Path(record["path"]).resolve() != path:
                    raise ValueError(f"{name} launcher path differs from resource lock")
                if not path.is_file() or not os.access(path, os.X_OK):
                    raise ValueError(f"{name} is missing or not executable: {path}")
                digest = sha256(path)
                if digest != record.get("sha256"):
                    raise ValueError(f"{name} SHA-256 differs from lock")
                dep = subprocess.run(["ldd", str(path)], capture_output=True, text=True, check=False)
                deps = dep.stdout + dep.stderr
                if dep.returncode != 0 or "not found" in deps:
                    raise ValueError(f"{name} dynamic runtime dependencies unresolved: {deps}")
                help_probe = subprocess.run([str(path), "--help"], capture_output=True, text=True,
                                            timeout=10, check=False)
                probe_text = help_probe.stdout + help_probe.stderr
                if name == "native_m2_m3" and "usage:" not in probe_text.lower():
                    raise ValueError(f"{name} executable did not return its usage probe")
                if name == "native_dude" and help_probe.returncode != 0:
                    raise ValueError(f"{name} help/runtime probe failed: {probe_text}")
                checks.append({"name": name, "path": str(path), "sha256": digest,
                    "bytes": path.stat().st_size, "ldd_exit": dep.returncode,
                    "dependencies_not_found": False, "usage_probe_exit": help_probe.returncode})
            print(json.dumps({"status": "pass", "resource_manifest": str(lock_path),
                "resource_manifest_sha256": sha256(lock_path), "binaries": checks}, indent=2))
            return 0
        alias, tail = self._selected_alias(args)
        route_alias = scene_entry(alias, self.registry)["default_route"]
        if tail:
            if len(tail) != 2 or tail[0] != "--route": raise ValueError("cú pháp: check [alias] [--route name]")
            route_alias = tail[1]
        entry = scene_entry(alias, self.registry)
        ctx = _scene_context(entry, route_alias)
        comparison_baseline = validate_comparison_baseline(ctx)
        py_probe = self._project_python_probe(resolve_path(ctx["profile"]["perception"]["detector_profile"]))
        run_probe = RUNS / f".preflight-{unique_run_id(alias)}"
        prof, temp = self._temp_profile(entry, route_alias)
        env = self._stage_env(project=True)
        cmd = [self.env["project_python"], "-B", str(RUNNER / "preflight_habitat_run.py"), "--profile", str(prof),
               "--run-root", str(run_probe), "--habitat-python", self.env["habitat_python"],
               "--m2-m3-binary", self.env["native_m2_m3"], "--dude-binary", self.env["native_dude"]]
        log = Path(tempfile.gettempdir()) / f"scene-launcher-check-{uuid.uuid4().hex}.log"
        RUNS.mkdir(parents=True, exist_ok=True)
        check_lock = LOCK_PATH.open("a+")
        try:
            fcntl.flock(check_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            check_lock.close()
            if temp: temp.unlink(missing_ok=True)
            print("Check đang bị từ chối vì một launcher run khác đang hoạt động.")
            return 1
        try:
            code, tail_text = self._subprocess(cmd, stage="preflight", log_path=log, env=env)
        finally:
            check_lock.close()
        if temp: temp.unlink(missing_ok=True)
        try:
            raw = log.read_text(encoding="utf-8", errors="replace")
            start = raw.find("{\n")
            report = json.loads(raw[start:]) if start >= 0 else None
        except Exception: report = None
        if report:
            route_ok = code == 0 and report.get("status") == "pass"
            overall_ok = route_ok and py_probe.get("status") == "pass"
            print(f"Check {'PASS' if overall_ok else 'BLOCKED/FAIL'} — {alias}/{route_alias}; asset/route preflight {'PASS' if route_ok else 'FAIL'}")
            print(f"Project Python: {py_probe.get('executable')} ({py_probe.get('version')}), package versions={py_probe.get('versions')}")
            for capability, result in py_probe.get("capabilities", {}).items():
                print(f"  Dependency capability {capability}: {result.get('status')}")
                for issue in result.get("issues", []): print(f"    {issue}")
            for item in py_probe.get("missing", []): print(f"  Project Python dependency issue: {item}")
            for row in report.get("checks", []):
                if row.get("exists") is False or row.get("returncode", 0): print(f"  {row.get('label')}: {row.get('path',row.get('interpreter'))} — lỗi")
            for warning in report.get("warnings", []): print(f"  Cảnh báo: {warning}")
            for error in report.get("errors", []): print(f"  Lỗi: {error}")
            print(f"Route validation: {report.get('route_validation')}")
            if comparison_baseline:
                print(f"Comparison baseline: {comparison_baseline['run_id']} — {comparison_baseline['resolution_px']} px, {comparison_baseline['frame_count']} frames; same route/config verified")
        else: print(f"Preflight lỗi; xem {log}:\n{tail_text}")
        return 0 if code == 0 and report and report.get("status") == "pass" and py_probe.get("status") == "pass" else 1

    def cmd_replay_cached(self, args: list[str]) -> int:
        if not args:
            raise ValueError("cú pháp: replay-cached <run-id-or-path> [--profile source|geometry-only|geometry-only-dedup-v2] [--semantic off|u4ov]")
        cache_token = args[0]
        profile, semantic = "source", "off"
        tail = list(args[1:])
        while tail:
            option = tail.pop(0)
            if option == "--profile" and tail:
                profile = tail.pop(0)
            elif option == "--semantic" and tail:
                semantic = tail.pop(0)
            else:
                raise ValueError(f"tham số replay-cached không hợp lệ: {option}")
        if profile not in {"source", "geometry-only", "geometry-only-dedup-v2"} or semantic not in {"off", "u4ov"}:
            raise ValueError("profile phải source|geometry-only|geometry-only-dedup-v2; semantic phải off|u4ov")
        if profile == "geometry-only-dedup-v2" and semantic == "u4ov":
            raise ValueError("U4-OV sidecar assignments hiện khóa theo geometry-only; không thể gắn nhãn đó vào dedup-v2 replay")

        token_path = Path(cache_token).expanduser()
        cache_run = token_path.resolve() if token_path.is_absolute() else (RUNS / cache_token).resolve()
        allowed_roots = (RUNS.resolve(), Path(self.env["baseline_read_root"]).resolve() / "artifacts/runs")
        if not cache_run.is_dir() or not any(cache_run.is_relative_to(root) for root in allowed_roots):
            raise ValueError("cache input phải là run existente dentro workspace ou baseline read-only artifacts/runs")
        capture = cache_run / "capture"
        p2_index = cache_run / "detection_2d/detection_2d_index.json"
        p3 = cache_run / "detection_3d/detection_3d.jsonl"
        p3_manifest_path = cache_run / "detection_3d/detection_3d_manifest.json"
        if not all(path.is_file() for path in (capture / "capture_manifest.json", p2_index, p3, p3_manifest_path)):
            raise FileNotFoundError("cache cần capture manifest, P2 index, P3 cache và P3 manifest")
        capture_manifest_path = capture / "capture_manifest.json"
        capture_manifest = read_json(capture_manifest_path)
        p2 = read_json(p2_index)
        p3_meta = read_json(p3_manifest_path)
        capture_hash = sha256(capture_manifest_path)
        if (p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != capture_hash
                or p3_meta.get("status") != "pass" or p3_meta.get("capture_manifest_sha256") != capture_hash
                or p2.get("frame_count") != capture_manifest.get("frame_count")
                or p3_meta.get("frame_count") != capture_manifest.get("frame_count")):
            raise ValueError("capture/P2/P3 cache provenance or frame counts do not match")

        # A versioned, local test lock binds the read-only 00802 29-frame input.
        if cache_run.resolve() == (Path(self.env["baseline_read_root"]).resolve() / "artifacts/runs/hm3d_integration_20261005/00802/main").resolve():
            lock_path = ROOT / "locks/u5_m9_00802_29frame.lock.json"
            if not lock_path.is_file():
                raise ValueError("locked 29-frame cache manifest is not installed in this workspace")
            test_lock = read_json(lock_path)
            if test_lock.get("status") != "locked_read_only_input" or test_lock.get("source_run") != str(cache_run):
                raise ValueError("29-frame cache lock status/source-run mismatch")
            for field, record in test_lock.get("inputs", {}).items():
                path = Path(record["path"]).resolve()
                if (not path.is_file() or path.stat().st_size != record.get("bytes")
                        or sha256(path) != record.get("sha256")):
                    raise ValueError(f"29-frame locked input changed: {field}")
            if capture_manifest.get("frame_count") != 29:
                raise ValueError("locked 29-frame test no longer has 29 frames")

        run_id = unique_run_id("u5m9")
        run_root = RUNS / run_id
        run_root.mkdir(parents=False, exist_ok=False)
        (run_root / "logs").mkdir()
        replay_dir, grid_dir = run_root / "replay", run_root / "grid_trace"
        backend = {"source": "source-wrapper", "geometry-only": "geometry-only",
                   "geometry-only-dedup-v2": "geometry-only-dedup-v2"}[profile]
        duplicate_v2_reference = find_duplicate_v2_reference(cache_run) if profile == "geometry-only-dedup-v2" else None
        sidecar_path = None
        semantic_status = "off"
        if semantic == "u4ov":
            from scripts.u5_experimental import build_sidecar, verify_inputs
            sidecar_out = run_root / "u4ov_sidecar"
            hashes = verify_inputs(cache_run)
            payload = build_sidecar(cache_run, sidecar_out, profile, hashes)
            sidecar_path = sidecar_out / "u5_sidecar.json"
            semantic_status = "u4ov_causal_diagnostics_only"

        cmd = [self.env["project_python"], "-B", str(RUNNER / "run_habitat_native_replay.py"),
            "--capture", str(capture), "--detection-3d", str(p3), "--detection-3d-manifest", str(p3_manifest_path),
            "--pipeline-config", str(ROOT / "configs/reference_code_mp3d.yaml"),
            "--m2-m3-binary", self.env["native_m2_m3"], "--dude-binary", self.env["native_dude"],
            "--resource-manifest", self.env["resource_manifest"], "--object-backend", backend,
            "--output", str(replay_dir), "--grid-output", str(grid_dir)]
        if backend == "geometry-only":
            cmd.extend(["--detection-2d-index", str(p2_index),
                        "--u3-config-lock", str(ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json")])
        elif backend == "geometry-only-dedup-v2":
            cmd.extend(["--detection-2d-index", str(p2_index),
                        "--u3-config-lock", str(ROOT / "artifacts/audits/u3_causal_prefix/association_config.locked.json"),
                        "--duplicate-v2-config", str(ROOT / "configs/association/u3_same_frame_duplicate_candidate_v2.json")])
            if duplicate_v2_reference is not None:
                cmd.extend(["--duplicate-v2-reference", str(duplicate_v2_reference)])
        if sidecar_path is not None:
            cmd.extend(["--u4-semantic-sidecar", str(sidecar_path)])

        lock = LOCK_PATH.open("a+")
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("đang có launcher run/check khác hoạt động") from exc
            started = datetime.now(timezone.utc).isoformat()
            state = {"schema_version": 1, "kind": "u5_cached_native_replay", "run_id": run_id,
                "status": "running", "source_cache_run": str(cache_run), "scene_id": capture_manifest.get("scene_id"),
                "profile": profile, "graph_backend": backend, "semantic_extension": semantic_status,
                "association_profile": profile,
                "room_labeler": "not_run_cached_native_replay",
                "u4_ov_diagnostics": semantic_status,
                "same_frame_duplicate_profile": (profile if profile == "geometry-only-dedup-v2" else "existing"),
                "started_utc": started, "finished_utc": None, "run_root": rel_or_abs(run_root),
                "input_hashes": {"capture_manifest": capture_hash, "p2_index": sha256(p2_index),
                    "p3_cache": sha256(p3), "p3_manifest": sha256(p3_manifest_path)},
                "environment": {key: self.env[key] for key in ("project_python", "native_m2_m3", "native_dude", "resource_manifest")},
                "stages": {"native_replay": {"status": "running", "command": _safe_manifest_args(cmd)}}}
            atomic_json(run_root / "launcher_manifest.json", state)
            self.active_run, self.active_state = run_root, state
            code, tail_text = self._subprocess(cmd, stage="native_replay", log_path=run_root / "logs/native_replay.log",
                env=self._stage_env(project=True), progress_prefix="PROGRESS:")
            replay_manifest_path = replay_dir / "manifest.json"
            if code != 0 or not replay_manifest_path.is_file():
                state.update({"status": "failed", "finished_utc": datetime.now(timezone.utc).isoformat(),
                              "error": tail_text[-3000:]})
                state["stages"]["native_replay"] = {"status": "failed", "exit_code": code}
                atomic_json(run_root / "launcher_manifest.json", state)
                print(f"Cached native replay failed; output: {run_root}\n{tail_text[-1500:]}")
                return 1
            report = read_json(replay_manifest_path)
            if report.get("pipeline_status") != "pass" or report.get("frames") != capture_manifest.get("frame_count"):
                raise RuntimeError("native replay manifest did not pass frame-count contract")
            state.update({"status": "completed", "finished_utc": datetime.now(timezone.utc).isoformat(),
                "frames": report["frames"], "objects": len([n for n in read_json(replay_dir / "snapshot.json")["snapshot"]["nodes"] if n["kind"] == "object"]),
                "runtime_ms": report["runtime_ms"], "fresh_replay_equal": report["fresh_replay_equal"],
                "snapshot_reload_equal": report["snapshot_reload_equal"], "delta_replay_equal": report["delta_replay_equal"],
                "replay_manifest": rel_or_abs(replay_manifest_path), "u4ov_sidecar": None if sidecar_path is None else rel_or_abs(sidecar_path),
                "object_backend_config_sha256": report.get("object_backend_config_sha256"),
                "native_resource_binding": report.get("native_resource_binding")})
            state["stages"]["native_replay"] = {"status": "pass", "exit_code": 0,
                "output_files": [rel_or_abs(replay_dir / name) for name in ("manifest.json", "snapshot.json", "delta_log.json", "trajectory.json", "frame_diagnostics.json")]}
            atomic_json(run_root / "launcher_manifest.json", state)
            print(f"Cached replay pass | graph backend={backend} | diagnostic profile={profile} | semantic={semantic_status} | frames={report['frames']} | runtime={report['runtime_ms'] / 1000:.1f}s | run-id={run_id}")
            if profile == "geometry-only-dedup-v2":
                duplicate_summary = report.get("same_frame_duplicate_summary", {})
                parity = report.get("geometry_offline_reference", {})
                parity_value = parity.get("exact_assignment_parity")
                parity_label = "not_available" if parity_value is None else parity_value
                print(f"Dedup v2 | aliases={duplicate_summary.get('aliased_observations')} | ambiguous={duplicate_summary.get('ambiguous_observations')} | association-deferred={duplicate_summary.get('association_deferred_observations')} | offline assignment parity={parity_label}")
                print("Experimental profile: ambiguous blocks observation aliasing only; association may still accept the observation. No U4-OV or room-score changes.")
            print(f"Results: python3 scripts/scene_launcher.py results {run_id}")
            print(f"Open: python3 scripts/scene_launcher.py open {run_id} u5")
            return 0
        finally:
            self.active_process = None
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally: lock.close()

    def _fusion_environment_check(self) -> dict:
        interpreter = Path(self.env["project_python"]).expanduser()
        if not interpreter.is_file():
            return {"status": "blocked", "interpreter": str(interpreter), "error": "configured project interpreter is missing"}
        probe = subprocess.run([str(interpreter), "-B", "-c",
            "import json, numpy, PIL, matplotlib; print(json.dumps({'numpy':numpy.__version__,'Pillow':PIL.__version__,'matplotlib':matplotlib.__version__}))"],
            cwd=ROOT, env=self._stage_env(project=True), text=True, capture_output=True, check=False, timeout=30)
        if probe.returncode:
            return {"status": "blocked", "interpreter": str(interpreter), "error": probe.stderr[-1200:]}
        return {"status": "pass", "interpreter": str(interpreter), "versions": json.loads(probe.stdout.strip().splitlines()[-1])}

    def _fusion_preflight(self, input_root: Path) -> dict:
        info = inspect_input(input_root)
        cache_root = Path(info["cache_root"])
        p3_profile = Path(str(info["p3_manifest"].get("profile", ""))).expanduser()
        if not p3_profile.is_file() or fusion_sha256(p3_profile) != info["p3_manifest"].get("profile_sha256"):
            raise ValueError(f"P3 effective profile missing or hash mismatch: {p3_profile}")
        env = self._fusion_environment_check()
        if env.get("status") != "pass":
            raise ValueError(f"fusion runtime preflight blocked: {env.get('error')}")
        if info.get("reused_sidecar"):
            parity = compare_detection3d(cache_root / "detection_3d/detection_3d.jsonl",
                                         info["reused_sidecar"]["detection_path"])
            if parity["status"] != "pass":
                info["reused_sidecar"] = None
                info["sidecar_issue"] = f"existing sidecar Detection3D diverges: {parity}"
            else:
                info["sidecar_parity"] = parity
                info["sidecar_source_path"] = cache_root / "detection_3d"
                info["sidecar_source_kind"] = "input_run"
        if not info.get("reused_sidecar") and OBJECT_FUSIONS.is_dir():
            candidates = sorted(OBJECT_FUSIONS.glob("*/sidecar"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
            for candidate in candidates:
                status_path = candidate.parent / "status.json"
                try:
                    if not status_path.is_file() or read_json(status_path).get("status") != "completed":
                        continue
                    reusable = validate_sidecar(cache_root, candidate)
                    parity = compare_detection3d(cache_root / "detection_3d/detection_3d.jsonl", reusable["detection_path"])
                    if parity["status"] != "pass":
                        continue
                    info["reused_sidecar"] = reusable
                    info["sidecar_parity"] = parity
                    info["sidecar_source_path"] = candidate
                    info["sidecar_source_kind"] = "prior_fusion_result"
                    break
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
        stage = info["cache_run_manifest"].get("stages", {}).get("detection_3d", {})
        previous_p3_seconds = float(stage.get("elapsed_s", 0) or 0)
        previous_fusion = ROOT / "artifacts/audits/object_graph_improvement/of2_prefix0_9_v5/manifest.json"
        try:
            smaller = read_fusion_json(previous_fusion)
            prior_fusion_seconds = float(smaller["runtime_seconds"]) * max(1, info["accepted_p3"]) / max(1, int(smaller.get("p3_accepted_observations", 1)))
        except (OSError, ValueError, KeyError, TypeError):
            prior_fusion_seconds = 0.0
        estimate = 30.0 if info.get("reused_sidecar") else 2.0 * previous_p3_seconds
        estimate += max(30.0, prior_fusion_seconds) + 30.0
        info["runtime_estimate"] = {"seconds": round(estimate, 1), "basis": {
            "same_input_p3_elapsed_seconds": previous_p3_seconds,
            "sidecar_serialization_factor": 2 if not info.get("reused_sidecar") else 0,
            "scaled_prior_fusion_seconds": round(prior_fusion_seconds, 1), "fixed_allowance_seconds": 30,
            "confidence": "planning estimate from the same P3 cache and one smaller fusion run; not a performance guarantee"}}
        if estimate > 30 * 60:
            raise ValueError(f"estimated fusion job is {estimate/60:.1f} minutes (>30 min); explicit user approval is required before running")
        info["runtime"] = env
        return info

    def _write_fusion_state(self, job: dict) -> None:
        root = Path(job["output_path"])
        atomic_json(root / "status.json", job)
        atomic_json(root / "manifest.json", job)

    def _fusion_result_root(self, fusion_id: str) -> Path:
        if (not fusion_id.startswith("fusion_") or Path(fusion_id).name != fusion_id
                or "/" in fusion_id or "\\" in fusion_id):
            raise ValueError("fusion result ID không hợp lệ")
        root = (OBJECT_FUSIONS / fusion_id).resolve()
        if not root.is_relative_to(OBJECT_FUSIONS.resolve()):
            raise ValueError("fusion result ID không hợp lệ")
        return root

    def _fusion_stage(self, job: dict, stage: str, status: str, **fields: Any) -> None:
        record = job["stages"].setdefault(stage, {})
        record.update({"status": status, "updated_utc": datetime.now(timezone.utc).isoformat(), **fields})
        if status == "running":
            record["started_monotonic_ns"] = time.monotonic_ns()
        if status in {"pass", "failed", "cancelled", "reused"}:
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            start = record.get("started_monotonic_ns")
            if start:
                record["elapsed_s"] = round((time.monotonic_ns() - start) / 1e9, 6)
        job["current_stage"] = stage if status == "running" else job.get("current_stage")
        self._write_fusion_state(job)

    def _execute_fusion_job(self, job: dict, info: dict) -> int:
        output_root = Path(job["output_path"])
        lock = LOCK_PATH.open("a+")
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another launcher pipeline/check/evaluation holds the shared lock")
            self.cancel_requested.clear()
            started_all = time.monotonic()
            input_root = Path(job["input_path"])
            authoritative = input_root / "detection_3d"
            if info.get("reused_sidecar"):
                p3_output = Path(info["sidecar_source_path"])
                self._fusion_stage(job, "sidecar", "reused", path=str(p3_output),
                                   evidence_records=info["reused_sidecar"]["evidence_count"],
                                   reused_from=info.get("sidecar_source_kind"))
            else:
                p3_output = output_root / "sidecar"
                p3_manifest = info["p3_manifest"]
                cmd = [self.env["project_python"], "-B", str(RUNNER / "project_detection_masks_to_3d.py"),
                       "--capture", str(input_root / "capture"), "--detection-2d", str(input_root / "detection_2d"),
                       "--output", str(p3_output), "--profile", str(Path(p3_manifest["profile"]).resolve()),
                       "--sampling-stride", str(int(p3_manifest.get("effective_sampling_stride", 1))),
                       "--cluster-min-points", str(int(p3_manifest["effective_cluster_min_points"])),
                       "--cluster-eps-m", str(float(p3_manifest["effective_cluster_eps_m"])),
                       "--export-object-evidence"]
                self._fusion_stage(job, "sidecar", "running", action="recompute_p3_from_cached_capture_and_p2",
                                   command=_safe_manifest_args(cmd), log=str(output_root / "logs/p3_sidecar.log"))
                code, tail = self._subprocess(cmd, stage="fusion-sidecar", log_path=output_root / "logs/p3_sidecar.log",
                                              env=self._stage_env(project=True), progress_prefix="PROGRESS:")
                if code:
                    self._fusion_stage(job, "sidecar", "cancelled" if code == 130 else "failed", exit_code=code, error=tail[-2500:])
                    raise RuntimeError(f"P3 sidecar stage failed: {tail[-1000:]}")
                sidecar_manifest_path = p3_output / "detection_3d_manifest.json"
                if not sidecar_manifest_path.is_file():
                    raise RuntimeError("P3 sidecar runner exited without detection_3d_manifest.json")
                parity = compare_detection3d(authoritative / "detection_3d.jsonl", p3_output / "detection_3d.jsonl")
                if parity["status"] != "pass":
                    job["p3_parity"] = parity
                    self._fusion_stage(job, "sidecar", "failed", error="recomputed Detection3D diverged; fusion was not started")
                    raise RuntimeError(f"recomputed P3 Detection3D diverged from authoritative cache: {parity}")
                side_manifest = read_json(sidecar_manifest_path)
                side_manifest["object_evidence_sidecar"]["reconstructed"] = True
                side_manifest["fusion_reconstruction_provenance"] = {
                    "kind": "recomputed_selected_points_from_cached_rgbd_and_p2",
                    "authoritative_detection3d_parity": parity,
                    "exact_selected_point_parity_to_historical_cache": "not_claimed; original selected pixel indices were not stored",
                    "capture_manifest_sha256": info["capture_sha256"], "detection_2d_index_sha256": info["p2_sha256"]}
                atomic_json(sidecar_manifest_path, side_manifest)
                checked = validate_sidecar(input_root, p3_output)
                self._fusion_stage(job, "sidecar", "pass", action="recomputed_from_cached_capture_and_p2",
                                   evidence_records=checked["evidence_count"], detection3d_parity="pass",
                                   exact_selected_point_parity="not_claimed", path=str(p3_output))

            self._fusion_stage(job, "fusion", "running", fixed_assignments=True, backend=info["backend"])
            fusion_output = output_root / "fusion"
            cmd = [self.env["project_python"], "-B", str(RUNNER / "run_object_fusion_offline.py"),
                   "--input-run", str(input_root), "--p3-output", str(p3_output),
                   "--replay", f"{info['backend']}={info['replay_root']}",
                   "--output", str(fusion_output), "--config", str(ROOT / "configs/fusion/object_geometry_v1.json")]
            self._fusion_stage(job, "fusion", "running", command=_safe_manifest_args(cmd), log=str(output_root / "logs/fusion.log"))
            code, tail = self._subprocess(cmd, stage="fixed-assignment-fusion", log_path=output_root / "logs/fusion.log",
                                          env=self._stage_env(project=True))
            fusion_elapsed_s = round(time.monotonic() - job["stages"]["fusion"]["started_monotonic_ns"] / 1e9, 3)
            if code:
                self._fusion_stage(job, "fusion", "cancelled" if code == 130 else "failed", exit_code=code, error=tail[-2500:])
                raise RuntimeError(f"fixed-assignment fusion stage failed: {tail[-1000:]}")
            fusion_manifest_path = fusion_output / "manifest.json"
            if not fusion_manifest_path.is_file():
                raise RuntimeError("fusion runner exited without manifest.json")
            result = read_json(fusion_manifest_path)
            if result.get("status") != "completed":
                raise RuntimeError("fusion runner manifest status is not completed")
            self._fusion_stage(job, "export", "running", action="write_launcher_viewer_report")
            export_started = time.monotonic()
            (output_root / "index.html").write_text(
                "<!doctype html><meta charset='utf-8'><title>Object geometry fusion</title>"
                "<p><a href='fusion/index.html'>Mở viewer fixed-assignment geometry fusion</a></p>"
                "<iframe src='fusion/index.html' style='width:100%;height:94vh;border:0'></iframe>", encoding="utf-8")
            assigned = int(result.get("assigned_observations_applied", 0))
            aliases = int(result.get("aliases_excluded", 0))
            excluded = int(result.get("deferred_or_unassigned_excluded", 0))
            object_rows = [obj for mode in result.get("objects", {}).values() for obj in mode]
            objects = len(object_rows)
            multi = sum(int(obj.get("observation_count", 0)) > 1 for obj in object_rows)
            unique_pose_total = sum(int(obj.get("unique_sensor_pose_count", 0)) for obj in object_rows)
            total_points = sum(int(obj.get("raw_point_count", 0)) for obj in object_rows)
            total_voxels = sum(int(obj.get("voxel_count", 0)) for obj in object_rows)
            p3_peak_rss = (read_json(p3_output / "detection_3d_manifest.json").get("peak_process_rss_mib")
                           if not info.get("reused_sidecar") else None)
            job["p3_peak_rss_mib"] = p3_peak_rss
            report_lines = ["# Fixed-assignment object geometry fusion", "",
                f"- Input run/backend: `{info['run_id']}` / `{info['backend']}`",
                f"- Scene/route/resolution/frames: `{info['scene_id']}` / `{info['route_alias']}` / `{info['resolution_px']}` / {info['frames']}",
                "- Fixed assignments: YES; Geometry fusion: ON; association/class/room scores: UNCHANGED.",
                "- Output is a fusion sidecar/viewer; it does not replace the graph.",
                f"- Assigned observations applied: {assigned}; aliases excluded: {aliases}; deferred/unassigned excluded: {excluded}.",
                f"- Fused objects: {objects}; objects with multiple observations: {multi}; points: {total_points}; voxels: {total_voxels}.",
                f"- Unique sensor poses summed per object: {unique_pose_total} (not a global or independent-view count).",
                f"- P3 evidence: {('reused from ' + str(info.get('sidecar_source_kind'))) if info.get('reused_sidecar') else 'recomputed from cached capture/P2'}; exact historical selected-point parity: {'not claimed for reconstructed evidence' if (not info.get('reused_sidecar') or info['reused_sidecar']['manifest'].get('object_evidence_sidecar', {}).get('reconstructed')) else 'bound to stored sidecar'}.",
                "- No semantic fusion; no graph/room-score updates.",
                "- Geometry accumulation on existing assignments is not evidence that accuracy improved.", "",
                f"- Stage runtime seconds: sidecar={job['stages'].get('sidecar', {}).get('elapsed_s', 'reused')}; fusion={fusion_elapsed_s}; export={round(time.monotonic()-export_started, 3)}.",
                f"- Peak RSS: P3 sidecar process={p3_peak_rss if p3_peak_rss is not None else 'unavailable (reused)'} MiB; fusion process={result.get('peak_process_rss_mib', 'unavailable')} MiB.",
                f"Viewer: `{output_root / 'index.html'}`", f"Fusion manifest: `{fusion_manifest_path}`"]
            (output_root / "report.vi.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
            job.update({"status": "completed", "finished_utc": datetime.now(timezone.utc).isoformat(),
                "runtime_seconds": round(time.monotonic() - started_all, 3),
                "p3_evidence_source": info.get("sidecar_source_kind") if info.get("reused_sidecar") else "recomputed",
                "fusion_manifest": str(fusion_manifest_path), "viewer": str(output_root / "index.html"),
                "assigned_observations_applied": assigned, "aliases_excluded": aliases,
                "deferred_or_unassigned_excluded": excluded, "fused_objects": objects,
                "objects_with_multiple_observations": multi, "raw_points": total_points, "voxels": total_voxels,
                "unique_sensor_pose_total_per_object": unique_pose_total,
                "fusion_peak_rss_mib": result.get("peak_process_rss_mib"),
                "p3_peak_rss_mib": p3_peak_rss})
            self._fusion_stage(job, "export", "pass", viewer=str(output_root / "index.html"), report=str(output_root / "report.vi.md"),
                               elapsed_s=round(time.monotonic() - export_started, 3))
            self._fusion_stage(job, "fusion", "pass", exit_code=0, summary={"assigned": assigned, "aliases": aliases,
                "excluded": excluded, "objects": objects, "multi_observation_objects": multi,
                "points": total_points, "voxels": total_voxels})
            job["status"] = "completed"
            job["current_stage"] = None
            self._write_fusion_state(job)
            print(f"Fixed-assignment fusion complete | input={info['run_id']} backend={info['backend']} | assigned={assigned} aliases={aliases} deferred/unassigned={excluded} | objects={objects} multi-observation={multi} points={total_points} voxels={total_voxels}")
            sidecar_elapsed = job['stages'].get('sidecar', {}).get('elapsed_s')
            sidecar_runtime = f"{sidecar_elapsed}s" if sidecar_elapsed is not None else "reused"
            export_elapsed = job['stages'].get('export', {}).get('elapsed_s')
            export_runtime = f"{export_elapsed}s" if export_elapsed is not None else "unavailable"
            print(f"Unique sensor poses summed per object={job['unique_sensor_pose_total_per_object']} (not an independent-view count) | runtime: sidecar={sidecar_runtime} fusion={fusion_elapsed_s}s export={export_runtime} | peak RSS: P3={p3_peak_rss if p3_peak_rss is not None else 'reused/unavailable'} MiB fusion={result.get('peak_process_rss_mib', 'unavailable')} MiB")
            print("Geometry accumulation uses existing assignments; it does not show improved association accuracy.")
            print(f"Fusion result ID: {job['fusion_result_id']} | Viewer: {output_root / 'index.html'}")
            print(f"Open with: ./run_scene.sh open {job['fusion_result_id']} fusion")
            return 0
        except Exception as exc:
            if job.get("status") not in {"failed", "cancelled"}:
                job.update({"status": "cancelled" if self.cancel_requested.is_set() else "failed",
                            "finished_utc": datetime.now(timezone.utc).isoformat(), "error": f"{type(exc).__name__}: {exc}"})
                self._write_fusion_state(job)
            print(f"Object fusion {job.get('status')}: {job.get('error', str(exc))}; output={output_root}")
            return 130 if job.get("status") == "cancelled" else 1
        finally:
            self.active_process = None
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()

    def cmd_fuse_objects(self, args: list[str], *, interactive: bool = False) -> int:
        if not args:
            raise ValueError("cú pháp: fuse-objects <run-id> [--check]")
        run_id = args[0]
        if any(arg != "--check" for arg in args[1:]) or args[1:].count("--check") > 1:
            raise ValueError("cú pháp: fuse-objects <run-id> [--check]")
        check_only = "--check" in args[1:]
        input_root, _ = self._find_run(run_id)
        info = self._fusion_preflight(input_root)
        if check_only:
            sidecar_state = "ready/reusable" if info.get("reused_sidecar") else f"needs P3 sidecar from cached RGB-D/P2 ({info.get('sidecar_issue', 'not present')})"
            print(f"Fusion check: READY | run={info['run_id']} scene={info['scene_id']} route={info['route_alias']} resolution={info['resolution_px']} frames={info['frames']}")
            print(f"Association backend={info['backend']} | fixed assignments=YES | geometry fusion=ON | association/class/room scores=UNCHANGED")
            print(f"P3 accepted={info['accepted_p3']} | lineage assigned={info['lineage']['assigned']} aliases={info['lineage']['aliases']} deferred/unassigned={info['lineage']['deferred_or_unassigned']}")
            print(f"Selected-point sidecar: {sidecar_state}")
            print(f"Runtime estimate={info['runtime_estimate']['seconds']}s; basis={info['runtime_estimate']['basis']}")
            print(f"Python runtime: {info['runtime']['interpreter']} | dependencies={info['runtime']['versions']}")
            print("--check is read-only; no artifacts were written and no P3/fusion was run.")
            return 0
        if self.active_thread and self.active_thread.is_alive():
            raise RuntimeError("launcher pipeline đang chạy; chờ hoặc hủy trước object fusion")
        if self.active_object_eval_thread and self.active_object_eval_thread.is_alive():
            raise RuntimeError("object evaluation đang chạy; chờ hoặc hủy trước object fusion")
        if self.active_fusion_thread and self.active_fusion_thread.is_alive():
            raise RuntimeError(f"object fusion đang chạy: {self.active_fusion.get('fusion_result_id')}")
        OBJECT_FUSIONS.mkdir(parents=True, exist_ok=True)
        fusion_id = unique_run_id("fusion")
        output_root = OBJECT_FUSIONS / fusion_id
        output_root.mkdir(parents=False, exist_ok=False)
        (output_root / "logs").mkdir()
        job = {"schema_version": 1, "kind": "fixed_assignment_object_geometry_fusion", "fusion_result_id": fusion_id,
            "input_run_id": info["run_id"], "association_run_path": str(input_root),
            "input_path": str(info["cache_root"]), "source_cache_run": str(info["cache_root"]),
            "output_path": str(output_root),
            "scene_id": info["scene_id"], "route_alias": info["route_alias"], "resolution_px": info["resolution_px"],
            "frames": info["frames"], "association_backend": info["backend"], "fixed_assignments": True,
            "geometry_fusion": True, "association_class_room_scores_unchanged": True,
            "runtime_estimate": info["runtime_estimate"], "status": "running", "current_stage": "preflight",
            "started_utc": datetime.now(timezone.utc).isoformat(), "stages": {},
            "input_hashes": {"capture_manifest": info["capture_sha256"], "p2_index": info["p2_sha256"],
                "p3_detection3d": info["p3_sha256"], "p3_manifest": info["p3_manifest_sha256"]}}
        self.active_fusion = job
        self.cancel_requested.clear()
        self._write_fusion_state(job)
        if interactive:
            self.active_fusion_thread = threading.Thread(target=self._execute_fusion_job, args=(job, info),
                name=f"fusion-{fusion_id}", daemon=False)
            self.active_fusion_thread.start()
            print(f"Đã khởi chạy fixed-assignment fusion {fusion_id} cho {info['run_id']}; /status {fusion_id}, /logs {fusion_id}, /cancel {fusion_id}.")
            return 0
        return self._execute_fusion_job(job, info)

    def cmd_compare_object_fusions(self, args: list[str]) -> int:
        if len(args) != 2:
            raise ValueError("cú pháp: compare-object-fusions <source-fusion-id> <candidate-fusion-id>")
        source_id, candidate_id = args
        for fusion_id in (source_id, candidate_id):
            root = self._fusion_result_root(fusion_id)
            manifest = root / "manifest.json"
            if not manifest.is_file() or read_json(manifest).get("status") != "completed":
                raise ValueError(f"fusion result chưa completed: {fusion_id}")
        compare_root = OBJECT_FUSIONS / "comparisons"
        compare_root.mkdir(parents=True, exist_ok=True)
        comparison_id = unique_run_id("fusion_compare")
        output = compare_root / comparison_id
        cmd = [self.env["project_python"], "-B", str(RUNNER / "compare_object_fusion_results.py"),
               source_id, candidate_id, "--output", str(output), "--fusion-root", str(OBJECT_FUSIONS)]
        code, tail = self._subprocess(cmd, stage="object-fusion-comparison",
            log_path=compare_root / f"{comparison_id}.log", env=self._stage_env(project=True))
        if code:
            raise RuntimeError(f"object fusion comparison failed: {tail[-1600:]}")
        report = read_fusion_json(output / "manifest.json")
        changed = sum(bool(row.get("topology_changed")) for row in report.get("membership_components", []))
        print(f"Fusion comparison ready | ID={comparison_id} | shared observations={report.get('shared_observations')} | "
              f"tracks source/candidate={report.get('source_track_count')}/{report.get('candidate_track_count')} | "
              f"changed membership groups={changed}/{len(report.get('membership_components', []))}")
        print(f"Open: ./run_scene.sh open {comparison_id} fusion-comparison")
        print(f"Report: {output / 'report.vi.md'}")
        print("This is a fixed-assignment geometry comparison, not an accuracy claim.")
        return 0

    def cmd_unified_graph(self, args: list[str]) -> int:
        """Opt-in local ConceptGraphs→OccuSG adapter; never runs association."""
        import argparse
        from datetime import datetime, timezone
        from integration_v2.unified_scene_graph import build_unified_graph
        parser = argparse.ArgumentParser(prog="unified-graph", description="Import final object export into an OccuSG room snapshot")
        parser.add_argument("--room-snapshot", required=True)
        parser.add_argument("--object-export", required=True)
        parser.add_argument("--source-kind", choices=("reference", "legacy", "fixture"))
        parser.add_argument("--out")
        options = parser.parse_args(args)
        room_path = Path(options.room_snapshot).expanduser().resolve()
        export_path = Path(options.object_export).expanduser().resolve()
        if not room_path.is_file(): raise FileNotFoundError(f"OccuSG room snapshot not found: {room_path}")
        if not (export_path / "export_manifest.json").is_file():
            raise FileNotFoundError(f"Object export manifest missing: {export_path / 'export_manifest.json'}; this command needs a ConceptGraphs export or an explicitly marked fixture/legacy package.")
        out = Path(options.out).expanduser().resolve() if options.out else ROOT / "artifacts/unified_graphs" / datetime.now(timezone.utc).strftime("unified_%Y%m%dT%H%M%SZ")
        try:
            result = build_unified_graph(room_path, export_path, out, source_kind=options.source_kind)
        except Exception as exc:
            if "unsupported ConceptGraphs object export schema" in str(exc):
                raise ValueError("Input is not a ConceptGraphs object export. Use a schema-aligned fixture for adapter development; do not label a legacy graph as ConceptGraphs output.") from exc
            raise
        manifest = json.loads((result / "manifest.json").read_text())
        counts = {status:sum(1 for item in manifest["membership"].values() if item["status"]==status) for status in ("assigned","ambiguous","unassigned")}
        print(f"Unified graph ready | source={manifest['source_kind']} | scene={manifest['scene_id']} | objects={len(manifest['object_ids'])} | membership assigned/ambiguous/unassigned={counts['assigned']}/{counts['ambiguous']}/{counts['unassigned']}")
        print(f"Graph: {result / 'unified_snapshot.json'} | Viewer: {result / 'index.html'} | Provenance: {result / 'manifest.json'}")
        return 0

    def cmd_hybrid_map(self, args: list[str]) -> int:
        """Run the pinned local hybrid mapper, import only final objects, and build the review viewer."""
        import argparse
        import re
        import secrets
        from integration_v2.unified_scene_graph import build_unified_graph
        from integration_v2.hybrid_mapping_viewer import build_hybrid_mapping_viewer
        from standalone_sg.serialization.json_graph import load_snapshot

        parser = argparse.ArgumentParser(prog="hybrid-map", description="Cached-perception ConceptGraphs mapping + OccuSG room graph")
        parser.add_argument("--cache", required=True, help="Existing cache with capture, P2 masks and P3 support")
        parser.add_argument("--text-features", required=True, help="Validated text feature sidecar directory")
        parser.add_argument("--image-features", help="Validated image-feature sidecar root; defaults to cache appearance/u2_features")
        parser.add_argument("--room-snapshot", required=True, help="Matching OccuSG room snapshot")
        parser.add_argument("--frames", type=int, default=5, help="Causal prefix length (default: 5; may select all cached frames)")
        parser.add_argument("--experiment-config", help="Locked experimental config, e.g. min=1 filter ablation")
        parser.add_argument("--seed", type=int, default=20261014)
        parser.add_argument("--run-id", help="Optional output ID; otherwise generated")
        options = parser.parse_args(args)
        cache = Path(options.cache).expanduser().resolve()
        text_root = Path(options.text_features).expanduser().resolve()
        room_path = Path(options.room_snapshot).expanduser().resolve()
        required_cache = ("capture/capture_manifest.json", "detection_2d/detection_2d_index.json",
                          "detection_3d/detection_3d_manifest.json")
        for relative in required_cache:
            if not (cache / relative).is_file():
                raise FileNotFoundError(f"cached input missing: {cache / relative}")
        for name in ("manifest.json", "text_features.npz", "text_feature_manifest.jsonl"):
            if not (text_root / name).is_file(): raise FileNotFoundError(f"text feature sidecar incomplete: {text_root / name}")
        if not room_path.is_file(): raise FileNotFoundError(f"room snapshot not found: {room_path}")
        if not HYBRID_MAPPING_PYTHON.is_file():
            raise FileNotFoundError(f"locked local CPU mapping interpreter missing: {HYBRID_MAPPING_PYTHON}")
        cache_manifest = read_fusion_json(cache / "capture/capture_manifest.json")
        p3_manifest = read_fusion_json(cache / "detection_3d/detection_3d_manifest.json")
        text_manifest = read_fusion_json(text_root / "manifest.json")
        if p3_manifest.get("status") != "pass": raise ValueError("P3 cache manifest status is not pass")
        if text_manifest.get("status") != "pass": raise ValueError("text feature sidecar status is not pass")
        if (text_manifest.get("text_features_sha256") != fusion_sha256(text_root / "text_features.npz")
                or text_manifest.get("rows_sha256") != fusion_sha256(text_root / "text_feature_manifest.jsonl")):
            raise ValueError("text feature sidecar hashes do not match its manifest")
        room = load_snapshot(room_path)
        scene_sequence = (cache_manifest.get("scene_id"), cache_manifest.get("sequence_id"))
        if scene_sequence != (room.scene_id, room.sequence_id):
            raise ValueError(f"room snapshot scene/sequence mismatch: cache={scene_sequence!r}, room={(room.scene_id, room.sequence_id)!r}")

        run_id = options.run_id or f"hybrid_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,96}", run_id): raise ValueError("run-id may contain letters, digits, underscore and hyphen only")
        HYBRID_MAPPINGS.mkdir(parents=True, exist_ok=True)
        output_root = (HYBRID_MAPPINGS / run_id).resolve()
        if not output_root.is_relative_to(HYBRID_MAPPINGS.resolve()): raise ValueError("hybrid output must stay inside artifacts/conceptgraphs_hybrid_runs")
        output_root.mkdir(parents=True, exist_ok=False)
        mapping_root = output_root / "mapping"
        runner = RUNNER / "run_conceptgraphs_cached_hybrid.py"
        cmd = [str(HYBRID_MAPPING_PYTHON), str(runner), "--cache", str(cache),
               "--text-features", str(text_root), "--room-snapshot", str(room_path),
               "--frames", str(options.frames), "--seed", str(options.seed),
               "--output", str(mapping_root)]
        if options.image_features:
            cmd.extend(["--image-features", str(Path(options.image_features).expanduser().resolve())])
        if options.experiment_config:
            cmd.extend(["--experiment-config", str(Path(options.experiment_config).expanduser().resolve())])
        env = os.environ.copy()
        mpl_dir = ROOT / "artifacts/cache/matplotlib"
        mpl_dir.mkdir(parents=True, exist_ok=True)
        env["MPLCONFIGDIR"] = str(mpl_dir)
        started = time.monotonic()
        code, tail = self._subprocess(cmd, stage="local-hybrid-mapping",
            log_path=output_root / "mapping.log", env=env)
        if code:
            raise RuntimeError(f"cached ConceptGraphs mapping failed; log={output_root / 'mapping.log'}; {tail[-1400:]}")

        mapping_run = read_fusion_json(mapping_root / "run_manifest.json")
        export_manifest = read_fusion_json(mapping_root / "export/export_manifest.json")
        diag_manifest = read_fusion_json(mapping_root / "diagnostic/diagnostic_manifest.json")
        if (export_manifest.get("scene_id"), export_manifest.get("sequence_id")) != scene_sequence:
            raise ValueError("ConceptGraphs export scene/sequence differs from the selected room snapshot")
        final_graph_root = output_root / "final_graph"
        build_unified_graph(room_path, mapping_root / "export", final_graph_root, source_kind="reference")
        unified = load_snapshot(final_graph_root / "unified_snapshot.json")
        room_nodes = lambda snap: {n.node_id: dict(n.attributes) for n in snap.nodes if n.kind == "room"}
        before_rooms, after_rooms = room_nodes(room), room_nodes(unified)
        room_ids = set(before_rooms)
        before_adj = {(e.source_id, e.target_id, e.relation, e.provenance, e.directed) for e in room.edges
                      if e.source_id in room_ids and e.target_id in room_ids}
        after_ids = set(room_nodes(unified))
        after_adj = {(e.source_id, e.target_id, e.relation, e.provenance, e.directed) for e in unified.edges
                     if e.source_id in after_ids and e.target_id in after_ids}
        if before_rooms != after_rooms or before_adj != after_adj:
            raise AssertionError("object import changed room geometry/attributes, semantic scores, or adjacency")
        viewer = build_hybrid_mapping_viewer(room_path, mapping_root, final_graph_root, output_root / "hybrid_viewer.html")
        final_count = sum(n.kind == "object" for n in unified.nodes)
        if final_count != len(export_manifest.get("objects", [])):
            raise AssertionError("unified graph object count differs from ConceptGraphs final export")
        manifest = {"schema": "local-conceptgraphs-occusg-hybrid-run/v1", "status": "pass", "run_id": run_id,
            "source_display_label": "Hybrid YOLO11n-seg + ViT-B-32 / ConceptGraphs reference mapping core",
            "scope": "cached perception + ConceptGraphs mapping core + OccuSG adapter; not full ConceptGraphs-Detect",
            "cache_root": str(cache), "capture_manifest_sha256": fusion_sha256(cache / "capture/capture_manifest.json"),
            "text_features_root": str(text_root), "text_features_manifest_sha256": fusion_sha256(text_root / "manifest.json"),
            "room_snapshot": str(room_path), "room_snapshot_sha256": fusion_sha256(room_path),
            "scene_id": room.scene_id, "sequence_id": room.sequence_id, "seed": options.seed,
            "mapping_interpreter": str(HYBRID_MAPPING_PYTHON), "mapping_runtime_s": mapping_run.get("runtime_s"),
            "mapping_core_runtime_s": mapping_run.get("mapping_runtime_s"), "mapping_peak_rss_mib": mapping_run.get("peak_rss_mib"),
            "launcher_integration_runtime_s": round(time.monotonic()-started, 6),
            "pre_filter_hypotheses": diag_manifest["counts"], "final_objects": final_count,
            "final_graph": str(final_graph_root / "unified_snapshot.json"),
            "final_graph_manifest_sha256": fusion_sha256(final_graph_root / "manifest.json"),
            "viewer": str(viewer), "room_geometry_adjacency_unchanged": True,
            "room_semantic_scores_changed": False, "diagnostic_hypotheses_imported_to_graph": False,
            "effective_config_sha256": fusion_sha256(mapping_root / "experiment_contract.json"),
            "mapping_run_manifest_sha256": fusion_sha256(mapping_root / "run_manifest.json")}
        (output_root / "hybrid_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True)+"\n", encoding="utf-8")
        print(f"Hybrid mapping complete | id={run_id} | seed={options.seed} | source=ConceptGraphs reference mapping core")
        print(f"Mode: {manifest['source_display_label']} (cached perception; không chạy detector/encoder; không phải full ConceptGraphs-Detect)")
        print(f"Objects: pre-filter hypotheses={diag_manifest['counts']['before_filter']} (retained={diag_manifest['counts']['retained_after_filter']}, filtered={diag_manifest['counts']['filtered']}) | final graph={final_count}")
        print(f"Runtime: mapping={mapping_run.get('runtime_s'):.2f}s (core={mapping_run.get('mapping_runtime_s'):.2f}s, peak RSS={mapping_run.get('peak_rss_mib'):.1f} MiB); launcher+adapter+viewer={manifest['launcher_integration_runtime_s']:.2f}s")
        print(f"Viewer: {viewer} | Final graph viewer: {final_graph_root / 'index.html'} | Manifest: {output_root / 'hybrid_manifest.json'}")
        print(f"Open later: ./run_scene.sh open {run_id} hybrid")
        return 0

    def cmd_fresh_replica(self, args: list[str]) -> int:
        import argparse
        from integration_v2.fresh_replica import make_c2_run_id, run_fresh_replica
        parser = argparse.ArgumentParser(prog="fresh-replica", description="Run one configured Habitat route through capture, perception, OccuSG rooms and ConceptGraphs mapping")
        parser.add_argument("--config", required=True, help="versioned scene+route C2 JSON; scene identity and route are read from this file")
        parser.add_argument("--run-id", help="optional unique output ID")
        parser.add_argument("--reuse-from", help="reuse hash-verified capture and P2 from an incomplete C2 run with identical inputs")
        parser.add_argument("--overall-timeout-s", type=int, help="optional hard wall-clock budget for preflight and every C2 stage")
        options = parser.parse_args(args)
        run_id = options.run_id or make_c2_run_id()
        result = run_fresh_replica(Path(options.config).expanduser().resolve(), run_id=run_id,
                                   command=["./run_scene.sh", "fresh-replica", *args],
                                   reuse_from=options.reuse_from,
                                   overall_timeout_s=options.overall_timeout_s)
        print(f"C2 status: {result['status']} | run={run_id} | scene={result.get('scene_id')} / {result.get('sequence_id')}")
        print(f"Manifest: {result['manifest_path']}")
        if result.get("status") == "completed":
            print(f"Unified graph: {result['outputs']['unified_graph']}")
            print(f"Viewer: {result['outputs']['viewer']}")
            print(f"Open: ./run_scene.sh open {run_id} hybrid")
        else:
            print(f"Failed at: {result.get('current_stage')} | {result.get('error')}")
        return 0 if result.get("status") == "completed" else 1

    def _scene_catalog_items(self, dataset_id: str | None = None):
        from integration_v2.scene_registry import discover_scenes
        return discover_scenes(dataset_id, registry_path=self.registry_path, env_path=self.env_path)

    @staticmethod
    def _scene_slug(value: str) -> str:
        import re
        return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_").lower()

    def _print_scene_resolution(self, scene, route, config: dict, *, run_id: str, port: int) -> None:
        catalog = self.registry.get("catalog", {})
        template = catalog.get("c2_template", "")
        route_doc = read_json(Path(route.config_path))
        d = config["dataset"]
        print("\nEffective scene configuration")
        print(f"  Dataset / adapter: {scene.dataset_id} / {scene.adapter}")
        print(f"  Scene / route / sequence: {scene.scene_id} / {route.route_id} / {route.sequence_id}")
        print(f"  Asset: {d.get('scene_asset') or d.get('scene_instance')}")
        print(f"  Navmesh: {d.get('navmesh_file')}")
        print(f"  Scene dataset config: {d.get('scene_dataset_config')}")
        selected_profile = config.get("capture_profile")
        effective_resolution = selected_profile.get("resolution_px") if selected_profile else route_doc.get("resolution_px")
        print(f"  Capture: {effective_resolution} px (profile={selected_profile.get('profile_id') if selected_profile else 'route-config-explicit'}), HFOV {route_doc.get('horizontal_fov_deg')}°, {route.pose_count} route poses")
        print("  Detector input size/preprocessing and all thresholds remain locked; masks/features are bound to this capture source.")
        print(f"  Predictor: cached-configured YOLO11n-seg + OpenCLIP ViT-B/32; semantic assets are not predictor inputs")
        print(f"  Membership / room semantics: {config['mapping']['membership_policy']} / {config['mapping']['room_semantics_status']}")
        print(f"  C2 template: {template}")
        print("  Stage plan: C2 capture→P2→P3→OccuSG(+built-in deterministic replay)→features→ConceptGraphs→unified graph; then D1 object+room producers with D2 XYZ viewer gated before start")
        print(f"  Planned IDs: C2={run_id}; D1 generated after C2; viewer port={port}")

    def _run_scene_workflow(self, dataset_id: str, scene_id: str, route_id: str, *, port: int,
                            assume_run_confirmed: bool = False, capture_profile_id: str | None = None) -> int:
        from integration_v2.scene_registry import resolve_scene_route, manifest_acceptance
        from integration_v2.fresh_replica import make_c2_run_id, run_fresh_replica

        scene, route, config = resolve_scene_route(dataset_id, scene_id, route_id,
                                                   registry_path=self.registry_path, env_path=self.env_path,
                                                   capture_profile_id=capture_profile_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        workflow_id = f"scene_{self._scene_slug(dataset_id)}_{self._scene_slug(scene_id)}_{stamp}_{uuid.uuid4().hex[:6]}"
        c2_run_id = make_c2_run_id()
        d1_run_id = f"d1_{stamp}_{uuid.uuid4().hex[:8]}"
        workflow_root = SCENE_WORKFLOWS / workflow_id
        workflow_root.mkdir(parents=True, exist_ok=False)
        config_path = workflow_root / "effective_c2_config.json"
        atomic_json(config_path, config)
        c2_template = (ROOT / self.registry["catalog"]["c2_template"]).resolve()
        source_paths = {
            "scene_registry": ROOT / "integration_v2/scene_registry.py",
            "fresh_replica_runner": ROOT / "integration_v2/fresh_replica.py",
            "scene_launcher": ROOT / "scripts/scene_launcher.py",
        }
        command = ["./run_scene.sh", "scene-run", "--dataset", dataset_id, "--scene", scene_id,
                   "--route", route_id, "--port", str(port)]
        if capture_profile_id:
            command.extend(["--capture-profile", capture_profile_id])
        workflow = {"schema": "dataset-neutral-scene-workflow/v1", "run_id": workflow_id,
                    "status": "started", "execution_mode": "C2_then_D1_live_processing_from_c2_cache",
                    "dataset_id": dataset_id, "scene_id": scene_id, "sequence_id": route.sequence_id,
                    "route_id": route_id, "command": command, "started_utc": datetime.now(timezone.utc).isoformat(),
                    "inputs": {"registry": str(self.registry_path), "registry_sha256": sha256(self.registry_path),
                               "c2_template": str(c2_template), "c2_template_sha256": sha256(c2_template),
                               "route_config": route.config_path, "route_config_sha256": sha256(Path(route.config_path)),
                               "capture_profile": config.get("capture_profile"),
                               "scene_asset": scene.scene_asset, "scene_asset_sha256": sha256(Path(scene.scene_asset)),
                               "navmesh": scene.navmesh,
                               "navmesh_sha256": sha256(Path(scene.navmesh)) if scene.navmesh else None,
                               "scene_dataset_config": scene.scene_dataset_config,
                               "scene_dataset_config_sha256": sha256(Path(scene.scene_dataset_config))
                                   if scene.scene_dataset_config else None,
                               "source_sha256": {name: sha256(path) for name, path in source_paths.items()}},
                    "effective_c2_config": str(config_path), "effective_c2_config_sha256": sha256(config_path),
                    "stages": {"preflight": {"status": "pending"}, "C2": {"status": "pending", "run_id": c2_run_id},
                               "D2_bootstrap": {"status": "pending"}, "D1": {"status": "pending", "run_id": d1_run_id}},
                    "scope_status": {"membership": "unavailable_elevation_scope",
                                     "room_semantics": "unavailable_membership_scope"},
                    "acceptance_evidence": {"source": "completed C2/D1 run manifests only"}}
        workflow_path = workflow_root / "run_manifest.json"
        atomic_json(workflow_path, workflow)
        self._print_scene_resolution(scene, route, config, run_id=c2_run_id, port=port)

        def confirm_after_preflight(preflight: dict, c2_doc: dict) -> bool:
            workflow["stages"]["preflight"] = {"status": "completed",
                "route_pose_count": preflight["route_validation"].get("pose_count"),
                "navigable_pose_count": preflight["route_validation"].get("navigable_pose_count"),
                "input_sha256": preflight.get("sha256"), "resources": preflight.get("resources")}
            atomic_json(workflow_path, workflow)
            print("\nPreflight passed")
            print(f"  route poses: {workflow['stages']['preflight']['route_pose_count']}; navigable: {workflow['stages']['preflight']['navigable_pose_count']}")
            print(f"  available RAM: {preflight.get('resources', {}).get('host_ram_available_bytes')}; free disk: {preflight.get('resources', {}).get('free_disk_bytes')}")
            print("  No model/dependency downloads are planned. Predictor inputs exclude semantic assets.")
            if assume_run_confirmed:
                print("Run explicitly confirmed by --yes.")
                return True
            if not sys.stdin.isatty():
                print("Run not started: use an interactive terminal or pass --yes after reviewing the effective config.")
                return False
            answer = input("Preflight is clear. Start the single C2 + D1 workflow now? [y/N] ").strip().lower()
            return answer in {"y", "yes", "run"}

        workflow["stages"]["preflight"] = {"status": "running"}
        workflow["stages"]["C2"]["status"] = "preflight_pending_confirmation"
        atomic_json(workflow_path, workflow)
        try:
            c2 = run_fresh_replica(config_path, run_id=c2_run_id, command=command,
                                   before_stages=confirm_after_preflight)
            c2_manifest = Path(c2["manifest_path"])
            workflow["stages"]["C2"].update({"manifest": str(c2_manifest),
                "manifest_sha256": sha256(c2_manifest), "status": c2.get("status"),
                "error": c2.get("error"), "outputs": c2.get("outputs", {})})
            if c2.get("status") == "cancelled":
                workflow["status"] = "cancelled"
                workflow["finished_utc"] = datetime.now(timezone.utc).isoformat()
                workflow["stages"]["preflight"]["status"] = "completed_run_declined"
                atomic_json(workflow_path, workflow)
                print(f"Run cancelled before producer stages. Workflow manifest: {workflow_path}")
                return 1
            if c2.get("status") != "completed":
                raise RuntimeError(f"C2 did not complete: {c2.get('status')} at {c2.get('current_stage')}")

            workflow["stages"]["preflight"]["status"] = "completed"
            workflow["stages"]["D2_bootstrap"]["status"] = "starting_before_D1_producers"
            workflow["stages"]["D1"]["status"] = "starting_viewer_gate"
            atomic_json(workflow_path, workflow)
            print(f"\nC2 completed: {c2_run_id}. Starting one D1 invocation; D2 must pass its viewer gate before either producer starts.", flush=True)
            d1_code = self._run_scene_d1_gate(c2_run_id, d1_run_id, port, workflow, workflow_path)
            d1_manifest = ROOT / "artifacts/live_processing_runs" / d1_run_id / "run_manifest.json"
            if not d1_manifest.is_file():
                raise RuntimeError(f"D1 run manifest was not created: {d1_manifest}")
            d1_doc = read_json(d1_manifest)
            workflow["stages"]["D1"].update({"status": d1_doc.get("status"), "manifest": str(d1_manifest),
                "manifest_sha256": sha256(d1_manifest), "joined_revision_count": d1_doc.get("joined_revision_count"),
                "outputs": d1_doc.get("outputs", {})})
            workflow["stages"]["D2_bootstrap"].update({"status": d1_doc.get("viewer", {}).get("d2_xyz", {}).get("status"),
                "viewer_url": d1_doc.get("viewer", {}).get("url"),
                "start_gate": d1_doc.get("viewer_start_gate", {}).get("status")})
            if d1_code != 0 or d1_doc.get("status") != "completed":
                raise RuntimeError(f"D1/D2 did not complete: exit={d1_code}, status={d1_doc.get('status')}")

            evidence = manifest_acceptance(scene_id, route.sequence_id)
            workflow["acceptance_evidence"] = {"source": "completed C2/D1 run manifests only",
                                                "summary": evidence,
                                                "c2_manifest_sha256": sha256(c2_manifest),
                                                "d1_manifest_sha256": sha256(d1_manifest)}
            if evidence["c2_status"] != "accepted_by_manifest" or evidence["d1_status"] != "accepted_by_manifest":
                raise RuntimeError("completed acceptance evidence could not be read back from the C2/D1 manifests")
            workflow["status"] = "completed"
            workflow["finished_utc"] = datetime.now(timezone.utc).isoformat()
            atomic_json(workflow_path, workflow)
            print(f"\nScene workflow completed: {workflow_id}")
            print(f"C2 manifest: {c2_manifest}")
            print(f"D1 manifest: {d1_manifest}")
            print(f"Workflow manifest: {workflow_path}")
            print(f"Viewer: {d1_doc.get('viewer', {}).get('url')}")
            print(f"Open later: ./run_scene.sh open {d1_run_id} live --port {port}")
            return 0
        except BaseException as exc:
            workflow["status"] = "failed"
            workflow["error"] = f"{type(exc).__name__}: {exc}"
            workflow["finished_utc"] = datetime.now(timezone.utc).isoformat()
            preflight = workflow["stages"].get("preflight", {})
            if preflight.get("status") == "running":
                preflight.update({"status": "failed", "error": workflow["error"]})
            c2_stage = workflow["stages"].get("C2", {})
            if c2_stage.get("status") in {"pending", "preflight_pending_confirmation"}:
                c2_manifest = ROOT / "artifacts/conceptgraphs_hybrid_runs" / c2_run_id / "c2_run_manifest.json"
                if c2_manifest.is_file():
                    c2_doc = read_json(c2_manifest)
                    c2_stage.update({"status": c2_doc.get("status", "failed"),
                                     "manifest": str(c2_manifest), "manifest_sha256": sha256(c2_manifest),
                                     "error": c2_doc.get("error") or workflow["error"]})
                else:
                    c2_stage.update({"status": "failed", "error": workflow["error"]})
            for name in ("D2_bootstrap", "D1"):
                stage = workflow["stages"].get(name, {})
                if stage.get("status") == "pending":
                    stage.update({"status": "not_started_due_to_prior_failure",
                                  "blocked_by": "C2/preflight", "error": workflow["error"]})
            atomic_json(workflow_path, workflow)
            print(f"Scene workflow failed; partial artifacts retained: {workflow_root}")
            raise

    @staticmethod
    def _terminate_scene_child(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    def _run_scene_d1_gate(self, c2_run_id: str, d1_run_id: str, port: int,
                           workflow: dict, workflow_path: Path) -> int:
        runner = RUNNER / "run_live_cached_processing.py"
        if not runner.is_file() or not HYBRID_MAPPING_PYTHON.is_file():
            raise FileNotFoundError("D1 runner or locked ConceptGraphs CPU interpreter is missing")
        command = [str(HYBRID_MAPPING_PYTHON), "-B", str(runner), "--source-run-id", c2_run_id,
                   "--run-id", d1_run_id, "--port", str(port), "--xyz-viewer"]
        env = self._stage_env(project=True)
        env["PYTHONUNBUFFERED"] = "1"
        print("D1 command: " + shlex.join(_safe_manifest_args(command)), flush=True)
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, start_new_session=True)
        gate_handled = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                print(line, end="", flush=True)
                if not gate_handled and "D1 start gate waiting_for_first_revision:" in line:
                    gate_handled = True
                    workflow["stages"]["D2_bootstrap"].update({"status": "waiting_for_first_revision",
                                                                  "ready_before_producers": True})
                    workflow["stages"]["D1"]["status"] = "waiting_for_explicit_browser_confirmation"
                    atomic_json(workflow_path, workflow)
                    self._open_live_timeline(d1_run_id, port)
                    if sys.stdin.isatty():
                        answer = input("D2 viewer đã mở. Hãy xác nhận trang hiển thị waiting_for_first_revision; Enter để giải phóng producer, q để dừng: ").strip().lower()
                        if answer == "q":
                            self._terminate_scene_child(proc)
                            return 130
                        self.cmd_start_live([d1_run_id, "--confirm-browser-ready", "--port", str(port)])
                    else:
                        print(f"Mở viewer: ./run_scene.sh open {d1_run_id} live --port {port}", flush=True)
                        print(f"Sau khi xác nhận waiting_for_first_revision, chạy: ./run_scene.sh start-live {d1_run_id} --confirm-browser-ready --port {port}", flush=True)
            return int(proc.wait())
        except KeyboardInterrupt:
            self._terminate_scene_child(proc)
            raise
        finally:
            if proc.poll() is None:
                self._terminate_scene_child(proc)

    def _scene_menu_input(self, prompt: str, maximum: int, *, default_index: int | None = None) -> int | None:
        try:
            raw = input(prompt).strip()
        except EOFError:
            return None
        if raw == "" and default_index is not None:
            return default_index
        if raw.lower() in {"q", "quit", "back", "b"}:
            return None
        try:
            value = int(raw)
        except ValueError:
            print("Enter a listed number or q to cancel.")
            return -1
        if not 1 <= value <= maximum:
            print(f"Choose a number between 1 and {maximum}.")
            return -1
        return value - 1

    def cmd_scene_menu(self, args: list[str]) -> int:
        import argparse
        parser = argparse.ArgumentParser(prog="menu", description="Interactive dataset/scene/route picker using the shared registry")
        parser.add_argument("--port", type=int, default=8766)
        options = parser.parse_args(args)
        from integration_v2.scene_registry import manifest_acceptance
        catalog = self.registry.get("catalog", {})
        datasets = catalog.get("datasets", [])
        if not datasets:
            raise ValueError("launcher catalog is missing; see configs/launcher/scenes.json")
        while True:
            print("\nLocal datasets")
            for i, dataset in enumerate(datasets, 1):
                scenes = self._scene_catalog_items(dataset["id"])
                print(f"  {i}. {dataset['display_name']} — {len(scenes)} local scenes discovered")
            choice = self._scene_menu_input("Select dataset (q to exit): ", len(datasets))
            if choice is None: return 0
            if choice < 0: continue
            dataset = datasets[choice]
            scenes = self._scene_catalog_items(dataset["id"])
            print(f"\n{dataset['display_name']} scenes (source: registered adapter discovery)")
            for i, scene in enumerate(scenes, 1):
                route_text = ", ".join(route.route_id for route in scene.routes) if scene.routes else "NO ROUTE — Run blocked"
                print(f"  {i}. {scene.scene_id} | assets={scene.asset_status} | routes={route_text}")
                if not scene.routes:
                    print(f"      Blocked: {scene.route_block_reason}")
            scene_choice = self._scene_menu_input("Select local scene (q to choose another dataset): ", len(scenes))
            if scene_choice is None: continue
            if scene_choice < 0: continue
            scene = scenes[scene_choice]
            if not scene.routes:
                print(f"Run blocked for {scene.scene_id}: {scene.route_block_reason}")
                continue
            print(f"\nRoutes for {scene.scene_id}")
            for i, route in enumerate(scene.routes, 1):
                evidence = manifest_acceptance(scene.scene_id, route.sequence_id)
                print(f"  {i}. {route.route_id} | {route.pose_count} poses | {route.resolution_px} | "
                      f"C2={evidence['c2_status']} D1={evidence['d1_status']} D2-simultaneous={evidence['d2_simultaneous_status']}")
            route_choice = self._scene_menu_input("Select route (q to choose another scene): ", len(scene.routes))
            if route_choice is None: continue
            if route_choice < 0: continue
            route = scene.routes[route_choice]
            profile_catalog = catalog.get("capture_profiles", {})
            profile_ids = list(profile_catalog.get("profiles", {}))
            default_profile = profile_catalog.get("default_profile_id")
            print("\nCapture profile (effective resolution overrides route resolution only; route HFOV/extrinsics and detector preprocessing stay fixed)")
            for i, profile_id in enumerate(profile_ids, 1):
                profile_path = ROOT / profile_catalog["profiles"][profile_id]
                profile_doc = read_json(profile_path)
                default_text = " [default]" if profile_id == default_profile else ""
                print(f"  {i}. {profile_id} | {profile_doc.get('resolution_px')} px{default_text}")
            profile_default_index = profile_ids.index(default_profile)
            profile_choice = self._scene_menu_input(
                "Select capture profile [Enter=default] (q to choose another route): ",
                len(profile_ids), default_index=profile_default_index)
            if profile_choice is None: continue
            if profile_choice < 0: continue
            selected_profile = profile_ids[profile_choice]
            return self._run_scene_workflow(dataset["id"], scene.scene_id, route.route_id,
                                            port=options.port, assume_run_confirmed=False,
                                            capture_profile_id=selected_profile)

    def cmd_scene_run(self, args: list[str], *, interactive: bool = False) -> int:
        import argparse
        parser = argparse.ArgumentParser(prog="scene-run", description="Resolve one local scene/route through the shared registry and run C2→D1/D2")
        parser.add_argument("--dataset", required=True)
        parser.add_argument("--scene", required=True)
        parser.add_argument("--route", required=True)
        profile_catalog = self.registry.get("catalog", {}).get("capture_profiles", {})
        parser.add_argument("--capture-profile", choices=list(profile_catalog.get("profiles", {})),
                            default=profile_catalog.get("default_profile_id"),
                            help="registered native capture profile; defaults to native 640x480")
        parser.add_argument("--port", type=int, default=8766)
        parser.add_argument("--yes", action="store_true", help="confirm C2 after the resolved effective config and preflight are shown")
        options = parser.parse_args(args)
        return self._run_scene_workflow(options.dataset, options.scene, options.route, port=options.port,
                                        assume_run_confirmed=options.yes, capture_profile_id=options.capture_profile)

    def cmd_camera_preview(self, args: list[str]) -> int:
        """Resolve one registered route slice and execute only smooth camera motion/capture."""
        import argparse
        import json
        import shutil
        from integration_v2.capture_compatibility import canonical_sha256, sensor_contract
        from integration_v2.scene_registry import resolve_scene_route

        parser = argparse.ArgumentParser(prog="camera-preview",
            description="Run a navmesh-following Habitat camera-only preview and record pose-delta keyframe candidates")
        parser.add_argument("--dataset", required=True, help="registered dataset ID")
        parser.add_argument("--scene", required=True, help="local scene ID from the shared registry")
        parser.add_argument("--route", required=True, help="registered route ID")
        parser.add_argument("--from-index", type=int, required=True, help="inclusive source route waypoint index")
        parser.add_argument("--to-index", type=int, required=True, help="inclusive final source route waypoint index")
        parser.add_argument("--run-id", help="new output ID; defaults to a timestamped T1 run ID")
        parser.add_argument("--timeout-s", type=float, default=300.0, help="bounded worker wall timeout")
        parser.add_argument("--capture-profile", default="native_640x480_v1",
                            help="T1 is locked to the registered native_640x480_v1 profile")
        options = parser.parse_args(args)
        if options.from_index < 0 or options.to_index <= options.from_index:
            raise ValueError("route slice requires 0 <= --from-index < --to-index")
        if options.capture_profile != "native_640x480_v1":
            raise ValueError("T1 camera preview is locked to native_640x480_v1 (640x480)")
        if not (5.0 <= float(options.timeout_s) <= 1800.0):
            raise ValueError("--timeout-s must be in [5, 1800]")
        scene, route, resolved_c2 = resolve_scene_route(
            options.dataset, options.scene, options.route, capture_profile_id=options.capture_profile)
        route_path = Path(route.config_path).resolve()
        route_doc = json.loads(route_path.read_text(encoding="utf-8"))
        route_rows = route_doc.get("route")
        if not isinstance(route_rows, list) or options.to_index >= len(route_rows):
            raise ValueError(f"route index {options.to_index} is outside 0..{len(route_rows) - 1 if isinstance(route_rows, list) else -1}")
        selected = []
        for index in range(options.from_index, options.to_index + 1):
            row = route_rows[index]
            if (not isinstance(row, dict) or len(row.get("position_habitat", [])) != 3
                    or not all(math.isfinite(float(v)) for v in row["position_habitat"])
                    or not math.isfinite(float(row.get("yaw_rad", 0.0)))):
                raise ValueError(f"route waypoint {index} has invalid position/orientation")
            selected.append({"source_index": index, **row})
        profile = resolved_c2["capture_profile"]
        resolution = [int(v) for v in profile["resolution_px"]]
        if resolution != [640, 480]:
            raise ValueError(f"registered native profile resolved to {resolution}, expected [640,480]")
        env = self.env
        scene_path = Path(resolved_c2["dataset"].get("scene_asset") or resolved_c2["dataset"].get("scene_instance") or "").resolve()
        navmesh_path = Path(resolved_c2["dataset"].get("navmesh_file") or "").resolve()
        scene_dataset_path = Path(resolved_c2["dataset"].get("scene_dataset_config") or "").resolve()
        for name, path in (("scene asset", scene_path), ("navmesh", navmesh_path),
                           ("scene dataset config", scene_dataset_path)):
            if not path.is_file():
                raise FileNotFoundError(f"camera-preview preflight missing {name}: {path}")
        policy_path = ROOT / "configs/habitat/smooth_route_t1_v2.json"
        if not policy_path.is_file():
            raise FileNotFoundError(f"T1 policy file missing: {policy_path}")
        policy = read_json(policy_path)
        if policy.get("schema") != "smooth-route-t1-policy/v2" or policy.get("status") != "development_unvalidated":
            raise ValueError("T1 policy must be a versioned development/unvalidated policy")
        import hashlib
        route_sha = sha256(route_path)
        policy_sha = sha256(policy_path)
        run_id = options.run_id or f"smooth_t1_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
        if Path(run_id).name != run_id or not run_id.startswith("smooth_t1_"):
            raise ValueError("run ID must be a safe filename beginning with smooth_t1_")
        output_root = (ROOT / "artifacts/smooth_route_previews" / run_id).resolve()
        if not output_root.is_relative_to((ROOT / "artifacts/smooth_route_previews").resolve()):
            raise ValueError("camera-preview output escapes its run root")
        if output_root.exists():
            raise FileExistsError(f"refusing to overwrite camera-preview output: {output_root}")
        output_root.mkdir(parents=True)
        (output_root / "operational").mkdir()
        # Read-only resource preflight. Values are recorded rather than used to
        # bypass the project's existing RAM policy or kill unrelated processes.
        available_ram = None
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="ascii").splitlines():
                if line.startswith("MemAvailable:"):
                    available_ram = int(line.split()[1]) * 1024
                    break
        disk = shutil.disk_usage(output_root)
        max_frames = int(policy["route_follow"]["max_camera_frames"])
        max_keyframes = min(max_frames, int(math.ceil(
            float(policy["route_follow"]["max_sim_time_s"]) /
            float(policy["keyframes"]["min_interval_s"])) + 1))
        pixels = resolution[0] * resolution[1]
        preflight = {"available_ram_bytes": available_ram, "output_disk_free_bytes": disk.free,
                     "estimated_worst_case_output_bytes": int(max_frames * pixels * 3 + max_keyframes * pixels * 8),
                     "estimate_assumption": "uncompressed RGB for every guard frame plus RGB/depth/valid-mask for every interval-bounded keyframe",
                     "resource_action": "read_only_measurement; no process changes"}
        if disk.free < 1024 * 1024 * 1024:
            raise RuntimeError(f"camera-preview needs >=1 GiB free disk by local preflight; available={disk.free}")

        sensor_config = {
            "resolution_px": resolution,
            "horizontal_fov_deg": float(route_doc["horizontal_fov_deg"]),
            "calibration_id": str(profile["calibration_id"]),
            "capture_profile": {"profile_id": profile["profile_id"], "profile_sha256": profile["profile_sha256"]},
            "sensor_position_habitat": [float(v) for v in route_doc["sensor_position_habitat"]],
            "sensor_orientation_habitat_rad": [float(v) for v in route_doc.get("sensor_orientation_habitat_rad", [0, 0, 0])],
            "min_depth_m": float(route_doc["min_depth_m"]), "max_depth_m": float(route_doc["max_depth_m"]),
        }
        sensor_doc = sensor_contract(sensor_config)
        effective = {
            "schema": "habitat-smooth-route-effective/v1", "run_id": run_id,
            "dataset_id": options.dataset, "dataset_version": scene.dataset_version,
            "scene_id": scene.scene_id, "sequence_id": f"{route.sequence_id}_smooth_{run_id}",
            "route_id": route.route_id, "source_sequence_id": route.sequence_id,
            "source_route_path": str(route_path), "source_route_sha256": route_sha,
            "selected_route_indices": [options.from_index, options.to_index],
            "route_points": selected,
            "scene_asset": str(scene_path), "scene_asset_sha256": sha256(scene_path),
            "navmesh_file": str(navmesh_path), "navmesh_sha256": sha256(navmesh_path),
            "scene_dataset_config": str(scene_dataset_path), "scene_dataset_config_sha256": sha256(scene_dataset_path),
            "resolution_px": resolution, "horizontal_fov_deg": sensor_config["horizontal_fov_deg"],
            "calibration_id": sensor_config["calibration_id"], "sensor_position_habitat": sensor_config["sensor_position_habitat"],
            "sensor_orientation_habitat_rad": sensor_config["sensor_orientation_habitat_rad"],
            "min_depth_m": sensor_config["min_depth_m"], "max_depth_m": sensor_config["max_depth_m"],
            "sensor_contract": sensor_doc, "sensor_contract_sha256": canonical_sha256(sensor_doc),
            "capture_profile": profile, "policy_path": str(policy_path), "policy_sha256": policy_sha,
            "policy": policy, "seed": int(policy.get("simulator_seed", 0)),
            "resource_preflight": preflight,
            "stage_semantics": {"camera_stream": "actual Habitat sensor render after each simulator action",
                                "keyframes": "pose-delta raw RGB-D candidates only; mapper_processed=false",
                                "detector_encoder_mapper_room_backend": "not run"},
        }
        effective_path = output_root / "effective_config.json"
        atomic_json(effective_path, effective)
        log_path = output_root / "operational/worker.log"
        command = [env["habitat_python"], "-B", str(RUNNER / "run_habitat_smooth_route_preview.py"),
                   "--config", str(effective_path), "--output", str(output_root),
                   "--timeout-s", str(options.timeout_s)]
        print("Camera-only smooth-route preview (no detector/encoder/mapping/Qwen)")
        print(f"Scene/route: {options.dataset}/{scene.scene_id}/{route.route_id} | waypoints={options.from_index}..{options.to_index}")
        print(f"Effective capture: {resolution[0]}x{resolution[1]} | HFOV={sensor_config['horizontal_fov_deg']}° | K={sensor_doc['intrinsics']}")
        print(f"Read-only preflight: available RAM={available_ram} bytes | free disk={disk.free} bytes")
        print("Command: " + shlex.join(_safe_manifest_args(command)))
        (output_root / "operational/command.txt").write_text(shlex.join(_safe_manifest_args(command)) + "\n", encoding="utf-8")
        proc = None
        timed_out = False
        try:
            with log_path.open("w", encoding="utf-8") as log:
                log.write("$ " + shlex.join(_safe_manifest_args(command)) + "\n")
                log.flush()
                proc = subprocess.Popen(command, cwd=ROOT, env=self._stage_env(project=False),
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                deadline = time.monotonic() + float(options.timeout_s)
                offset = log.tell()
                while proc.poll() is None:
                    if time.monotonic() > deadline:
                        timed_out = True
                        self._terminate_scene_child(proc)
                        break
                    log.flush()
                    with log_path.open("r", encoding="utf-8", errors="replace") as reader:
                        reader.seek(offset)
                        lines = reader.readlines()
                        offset = reader.tell()
                    for line in lines:
                        if line.startswith("PROGRESS:") or line.startswith("FAILED:"):
                            print(line.rstrip(), flush=True)
                    time.sleep(0.1)
                code = proc.wait()
                log.flush()
            if timed_out:
                manifest_path = output_root / "run_manifest.json"
                doc = read_json(manifest_path) if manifest_path.is_file() else {"schema": "smooth-route-camera-preview-run/v1", "run_id": run_id}
                doc.update({"status": "failed", "error": f"launcher timeout after {options.timeout_s}s",
                            "finished_utc": datetime.now(timezone.utc).isoformat(), "timeout_enforced_by_launcher": True})
                atomic_json(manifest_path, doc)
                print(f"FAILED: camera-preview timed out; child process group stopped; partial output retained: {output_root}")
                return 124
            manifest_path = output_root / "run_manifest.json"
            if not manifest_path.is_file():
                raise RuntimeError(f"worker exited {code} without run manifest; inspect {log_path}")
            doc = read_json(manifest_path)
            if code != 0 or doc.get("status") != "completed":
                print(f"Camera-only run failed (exit={code}, status={doc.get('status')}); partial artifacts retained: {output_root}")
                print(f"Log: {log_path}")
                return code or 1
            print(f"Camera-only preview completed: {run_id}")
            print(f"Manifest: {manifest_path}")
            print(f"Preview: {output_root / doc['stages']['viewer_build_validate']['entrypoint']}")
            print(f"Open: ./run_scene.sh open {run_id} camera-preview --port 8770")
            print("Stage boundary: camera-only; no perception, mapping, room membership or room labels.")
            return 0
        finally:
            if proc is not None and proc.poll() is None:
                self._terminate_scene_child(proc)

    def cmd_cached_replay(self, args: list[str]) -> int:
        """Build a unified timeline and viewer from pinned cached prefix artifacts."""
        import argparse
        from integration_v2.cached_replay import make_run_id, run_cached_replay

        parser = argparse.ArgumentParser(prog="cached-replay",
            description="Build unified graph + viewer from existing object/causal timelines; no perception or remapping")
        parser.add_argument("--config", required=True, help="versioned cached-replay-inputs/v1 JSON with explicit paths and hashes")
        parser.add_argument("--out", help="new run directory under artifacts/cached_replays; defaults to a generated ID")
        parser.add_argument("--max-points", type=int, help="override viewer display sampling only; does not change graph data")
        options = parser.parse_args(args)
        config = Path(options.config).expanduser().resolve()
        if not config.is_file():
            raise FileNotFoundError(f"cached replay input config not found: {config}")
        output = Path(options.out).expanduser().resolve() if options.out else CACHED_REPLAYS / make_run_id()
        result = run_cached_replay(config, output, command=["./run_scene.sh", "cached-replay", *args],
                                   workspace_root=ROOT, max_points=options.max_points)
        print(f"Cached replay completed | id={result['run_id']} | scene={result['inputs']['scene_id']} | sequence={result['inputs']['sequence_id']} | revisions={result['summary']['revisions']}")
        print(f"Run manifest: {result['outputs']['run_manifest']}")
        print(f"Results: ./run_scene.sh results {result['run_id']}")
        print(f"Open: ./run_scene.sh open {result['run_id']} timeline")
        print("Mode: cached_replay; no capture, perception, mapping, room scoring, or native replay was run.")
        return 0

    def cmd_live_replay(self, args: list[str]) -> int:
        """Run D1 lockstep processors from an already completed C2 cache."""
        import argparse
        parser = argparse.ArgumentParser(prog="live-replay",
            description="Run hybrid mapping + OccuSG from locked C2 cached inputs and publish joined revisions locally")
        parser.add_argument("--source-run-id", required=True, help="completed C2 run ID; no capture/perception is rerun")
        parser.add_argument("--run-id", help="new D1 output ID")
        parser.add_argument("--port", type=int, default=8766, help="localhost viewer port; busy ports are left untouched")
        parser.add_argument("--viewer-base-url", help="reuse an existing read-only loopback workspace server")
        parser.add_argument("--timeout", type=float, default=3600.0, help="bounded overall run timeout")
        parser.add_argument("--viewer-ready-timeout", type=float, default=600.0,
                            help="maximum seconds to wait for explicit start-live after D2 page is checked")
        parser.add_argument("--xyz-viewer", action="store_true",
                            help="publish the existing D2 XYZ page before producers start, then finalize its manifest after D1")
        options = parser.parse_args(args)
        run_id = options.run_id or f"d1_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
        runner = ROOT / "scripts/run_live_cached_processing.py"
        if not runner.is_file() or not HYBRID_MAPPING_PYTHON.is_file():
            raise FileNotFoundError("D1 runner or the locked ConceptGraphs CPU interpreter is missing")
        command = [str(HYBRID_MAPPING_PYTHON), "-B", str(runner),
                   "--source-run-id", options.source_run_id, "--run-id", run_id,
                   "--port", str(options.port), "--timeout", str(options.timeout)]
        if options.viewer_base_url:
            command.extend(["--viewer-base-url", options.viewer_base_url])
        if options.xyz_viewer:
            command.append("--xyz-viewer")
            command.extend(["--viewer-ready-timeout", str(options.viewer_ready_timeout)])
        env = self._stage_env(project=True)
        env["PYTHONUNBUFFERED"] = "1"
        print(f"Starting D1 from completed cache | source={options.source_run_id} | run={run_id}")
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        print(f"D1 process exit code: {result.returncode} | run={run_id}")
        print(f"Manifest: {ROOT / 'artifacts/live_processing_runs' / run_id / 'run_manifest.json'}")
        return result.returncode

    def cmd_start_live(self, args: list[str]) -> int:
        """Release an XYZ D1 run only after the operator checked its bootstrap page."""
        import argparse
        from standalone_sg.serialization.live_revision_io import atomic_write_json
        from scripts.build_d2_xyz_viewer import validate_served_entrypoint

        parser = argparse.ArgumentParser(prog="start-live",
            description="Validate the waiting D2 page/assets and explicitly release a gated D1 producer")
        parser.add_argument("run_id")
        parser.add_argument("--port", type=int)
        parser.add_argument("--confirm-browser-ready", action="store_true", required=True,
                            help="confirm that the displayed page says waiting_for_first_revision")
        options = parser.parse_args(args)
        if Path(options.run_id).name != options.run_id or not options.run_id.startswith("d1_"):
            raise ValueError("D1 live processing ID không hợp lệ")
        run_root = (ROOT / "artifacts/live_processing_runs" / options.run_id).resolve()
        if not run_root.is_relative_to((ROOT / "artifacts/live_processing_runs").resolve()):
            raise ValueError("D1 output path escapes live processing run root")
        manifest_path = run_root / "run_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"D1 manifest missing: {manifest_path}")
        manifest = read_json(manifest_path)
        if manifest.get("status") != "waiting_for_viewer_ready":
            raise ValueError(f"D1 run is not waiting for viewer confirmation: {manifest.get('status')}")
        viewer_doc = manifest.get("viewer", {})
        if not viewer_doc.get("url") or viewer_doc.get("host") != "127.0.0.1":
            raise ValueError("D1 manifest does not contain a loopback viewer URL")
        port = int(options.port if options.port is not None else viewer_doc.get("port", -1))
        if port != int(viewer_doc.get("port", -2)):
            raise ValueError(f"D1 viewer is on port {viewer_doc.get('port')}; use that port")
        d2 = resolve_d2_viewer_entrypoint(run_root)
        if d2 is None:
            raise ValueError("D2 viewer pointer is unavailable; producer remains stopped")
        url = viewer_doc["url"]
        bundle = validate_served_entrypoint(run_root / "viewer", url)
        if bundle["pointer"].get("entrypoint") != d2["entrypoint"]:
            raise ValueError("served viewer and launcher pointer disagree")
        start_path = run_root / "control/start.json"
        start_record = {"schema": "d1-explicit-viewer-start/v1", "run_id": options.run_id,
                        "confirmed_browser_ready": True,
                        "entrypoint_pointer_sha256": sha256(d2["pointer_path"]),
                        "entrypoint": d2["entrypoint"], "url": url, "started_by": "launcher",
                        "started_utc": datetime.now(timezone.utc).isoformat()}
        if start_path.exists():
            existing = read_json(start_path)
            expected = {key: start_record[key] for key in ("schema", "run_id", "confirmed_browser_ready",
                                                            "entrypoint_pointer_sha256", "entrypoint", "url")}
            if any(existing.get(key) != value for key, value in expected.items()):
                raise FileExistsError(f"a different start signal already exists: {start_path}")
        else:
            atomic_write_json(start_path, start_record)
        print(f"Viewer gate released for {options.run_id}; D1 producers may start.")
        print(f"Verified manifest-selected entrypoint: {url}")
        return 0

    def cmd_compare_profiles(self, args: list[str]) -> int:
        """Paired native replay of source and one candidate on the same cache."""
        if not args:
            raise ValueError("cú pháp: compare-profiles <input-run-id> --candidate geometry-only-dedup-v2 [--baseline source]")
        input_token = args[0]
        baseline, candidate = "source", None
        tail = list(args[1:])
        while tail:
            option = tail.pop(0)
            if option == "--candidate" and tail and candidate is None:
                candidate = tail.pop(0)
            elif option == "--baseline" and tail and baseline == "source":
                baseline = tail.pop(0)
            else:
                raise ValueError(f"tham số compare-profiles không hợp lệ: {option}")
        if baseline != "source" or candidate != "geometry-only-dedup-v2":
            raise ValueError("hiện hỗ trợ --baseline source --candidate geometry-only-dedup-v2")
        input_root, input_doc = self._find_run(input_token)
        if input_doc.get("status") != "completed" or input_doc.get("source_cache_run"):
            raise ValueError("input phải là run capture/P2/P3 completed, không phải cached graph replay")
        capture_manifest = input_root / "capture/capture_manifest.json"
        p2_index = input_root / "detection_2d/detection_2d_index.json"
        p3_cache = input_root / "detection_3d/detection_3d.jsonl"
        p3_manifest_path = input_root / "detection_3d/detection_3d_manifest.json"
        if not all(p.is_file() for p in (capture_manifest, p2_index, p3_cache, p3_manifest_path)):
            raise FileNotFoundError("input thiếu capture/P2/P3 cache bắt buộc")
        capture = read_json(capture_manifest)
        frames = int(capture.get("frame_count", 0))
        input_hashes = {"capture_manifest": sha256(capture_manifest), "p2_index": sha256(p2_index),
                        "p3_cache": sha256(p3_cache), "p3_manifest": sha256(p3_manifest_path)}
        prior_replay = input_root / "replay/manifest.json"
        prior_seconds = (read_json(prior_replay).get("runtime_ms", 0) / 1000) if prior_replay.is_file() else 0
        # Add 35% headroom around the known native replay time, plus launch overhead.
        estimate_seconds = (prior_seconds * 2.7 + 90) if prior_seconds > 0 else frames * 24.0 + 90
        print(f"Paired replay budget if a matching replay is unavailable: {estimate_seconds/60:.1f} min for {frames} frames (source + candidate, fresh verification included); capture/P2/P3 are reused in either case.")
        if estimate_seconds > 30 * 60:
            print("Estimate exceeds the 30-minute budget; no replay was started. Reduce the input scope or approve a separate long run.")
            return 1

        def verified_existing_replay(profile: str) -> Path | None:
            matches = []
            for path in RUNS.iterdir():
                if not path.is_dir():
                    continue
                manifest_path = path / "launcher_manifest.json"
                replay_manifest_path = path / "replay/manifest.json"
                if not manifest_path.is_file() or not replay_manifest_path.is_file():
                    continue
                try:
                    doc, replay_doc = read_json(manifest_path), read_json(replay_manifest_path)
                except Exception:
                    continue
                if (doc.get("status") == "completed" and doc.get("source_cache_run") == str(input_root)
                        and doc.get("association_profile") == profile and doc.get("input_hashes") == input_hashes
                        and replay_doc.get("pipeline_status") == "pass" and replay_doc.get("frames") == frames
                        and replay_doc.get("capture_manifest_sha256") == input_hashes.get("capture_manifest")):
                    matches.append(path)
            return max(matches, key=lambda p: p.stat().st_mtime) if matches else None

        def run_profile(profile: str) -> Path | None:
            reused = verified_existing_replay(profile)
            if reused is not None:
                print(f"Reuse verified replay: profile={profile} run-id={reused.name}; capture/P2/P3 hashes and frame count match.")
                return reused
            before = {p.name for p in RUNS.iterdir() if p.is_dir()}
            code = self.cmd_replay_cached([str(input_root), "--profile", profile, "--semantic", "off"])
            if code:
                return None
            for path in sorted((p for p in RUNS.iterdir() if p.is_dir() and p.name not in before),
                               key=lambda p: p.stat().st_mtime, reverse=True):
                manifest = path / "launcher_manifest.json"
                if not manifest.is_file():
                    continue
                doc = read_json(manifest)
                if doc.get("status") == "completed" and doc.get("source_cache_run") == str(input_root) and doc.get("association_profile") == profile:
                    return path
            raise RuntimeError(f"replay completed but launcher manifest for profile={profile} was not found")

        source_root = run_profile("source")
        if source_root is None:
            print("Source replay failed; candidate comparison was not started. Original input unchanged.")
            return 1
        candidate_root = run_profile(candidate)
        if candidate_root is None:
            print(f"Candidate replay failed; source replay retained at {source_root}. Original input unchanged.")
            return 1

        comparison_id = "assoc_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
        output = ROOT / "artifacts/profile_comparisons" / comparison_id
        log_path = ROOT / "artifacts/profile_comparisons" / f".{comparison_id}.log"
        cmd = [self.env["project_python"], "-B", str(RUNNER / "compare_association_profiles.py"),
               "--input-run", str(input_root), "--source-replay", str(source_root),
               "--candidate-replay", str(candidate_root), "--candidate", candidate,
               "--output", str(output)]
        code, tail_text = self._subprocess(cmd, stage="profile_comparison", log_path=log_path,
                                           env=self._stage_env(project=True))
        if code:
            print(f"Comparison scoring failed; paired replay runs are retained. Log: {log_path}\n{tail_text[-1200:]}")
            return code
        metrics = read_json(output / "metrics.json")
        source_q, candidate_q = metrics["source"]["quality"], metrics["candidate"]["quality"]
        summary_lines = ["Association profile comparison (native graph replay; no perception rerun)"]
        resolution = capture.get("resolution_px", "unknown")
        p3_meta = read_json(p3_manifest_path)
        summary_lines.append(f"Input: {input_root.name} | scene={capture.get('scene_id')} route={input_doc.get('route_alias','unknown')} | resolution={resolution} | frames={frames} | hash parity=PASS")
        summary_lines.append(f"P3 accepted/rejected={p3_meta.get('detection_3d_count','?')}/{p3_meta.get('rejected_detection_count','?')} | raw P3 observations={metrics['raw_p3_observations_common_denominator']} | GT identity coverage={metrics['evaluator_identity_eligible_common_observations']}/{metrics['raw_p3_observations_common_denominator']}")
        summary_lines.append(f"{'Metric':<30} {'Source':>22} {'Candidate':>22}")
        def comparison_row(label: str, source_value: Any, candidate_value: Any) -> str:
            return f"{label:<30} {str(source_value):>22} {str(candidate_value):>22}"
        summary_lines.append(comparison_row("Final tracks", metrics['source']['tracks_final'], metrics['candidate']['tracks_final']))
        summary_lines.append(comparison_row("Duplicate aliases", metrics['source']['same_frame_duplicate_summary'].get('aliased_observations',0), metrics['candidate']['same_frame_duplicate_summary'].get('aliased_observations',0)))
        summary_lines.append(comparison_row("Deferred observations", source_q['unassigned_scored_observations'], candidate_q['unassigned_scored_observations']))
        summary_lines.append(comparison_row("Association P/R/F1", f"{source_q['association_precision']}/{source_q['association_recall']}/{source_q['association_f1']}", f"{candidate_q['association_precision']}/{candidate_q['association_recall']}/{candidate_q['association_f1']}"))
        summary_lines.append(comparison_row("False-merge tracks", source_q['false_merge_tracks'], candidate_q['false_merge_tracks']))
        summary_lines.append(comparison_row("Fragmented evaluator IDs", source_q['fragmented_evaluator_instances'], candidate_q['fragmented_evaluator_instances']))
        summary_lines.append(comparison_row("Runtime / peak RSS", f"{metrics['source']['runtime_seconds']:.1f}s / {metrics['source']['peak_process_rss_mib']:.1f}MiB", f"{metrics['candidate']['runtime_seconds']:.1f}s / {metrics['candidate']['peak_process_rss_mib']:.1f}MiB"))
        summary_lines.append("Human physical-identity metrics: unavailable for this exact run; HM3D evaluator IDs are reported separately.")
        summary_lines.append(f"Report: {output / 'report.vi.md'}\nGallery: {output / 'gallery.html'}")
        summary_lines.append(f"Open with: /open {comparison_id} association-report | /open {comparison_id} association-gallery")
        print(colorize_profile_comparison("\n".join(summary_lines)))
        return 0

    def cmd_run(self, args: list[str], *, interactive: bool) -> int:
        alias = self.selected
        tail = list(args)
        if tail and not tail[0].startswith("-"): alias, tail = tail[0], tail[1:]
        if not alias: raise ValueError("chưa chọn scene; dùng `use <alias>` hoặc `run <alias>")
        entry = scene_entry(alias, self.registry)
        route_alias = entry["default_route"]
        association_profile = "source"
        dry = False
        while tail:
            item = tail.pop(0)
            if item == "--dry-run": dry = True
            elif item == "--route" and tail: route_alias = tail.pop(0)
            elif item == "--profile" and tail: association_profile = tail.pop(0)
            else: raise ValueError(f"tham số run không hợp lệ: {item}")
        if association_profile not in {"source", "geometry-only", "geometry-only-dedup-v2"}:
            raise ValueError("profile phải source|geometry-only|geometry-only-dedup-v2")
        if self.active_thread and self.active_thread.is_alive(): raise RuntimeError("launcher đã có run đang hoạt động")
        if self.active_object_eval_thread and self.active_object_eval_thread.is_alive(): raise RuntimeError("object evaluation đang chạy; chờ hoặc hủy trước khi chạy pipeline")
        if dry:
            self._run_pipeline(alias, route_alias, True, association_profile)
            return 0
        if interactive:
            try:
                validate_comparison_baseline(_scene_context(entry, route_alias))
            except Exception as exc:
                print(f"Không khởi chạy run: {type(exc).__name__}: {exc}")
                return 2
        self.cancel_requested.clear()
        if interactive:
            self.active_thread = threading.Thread(target=self._interactive_run,
                args=(alias, route_alias, association_profile), daemon=False)
            self.active_thread.start()
            print(f"Đã khởi chạy {alias}/{route_alias} profile={association_profile} ở foreground-managed mode; /status và /logs dùng được ngay.")
            return 0
        state = self._run_pipeline(alias, route_alias, association_profile=association_profile)
        if state.get("status") == "completed": self._print_summary(state)
        return 0 if state.get("status") == "completed" else 1

    def _interactive_run(self, alias: str, route_alias: str, association_profile: str = "source") -> None:
        try:
            state = self._run_pipeline(alias, route_alias, association_profile=association_profile)
            if state.get("status") == "completed":
                self._print_summary(state)
        except Exception as exc:
            print(f"Run không bắt đầu hoặc thất bại trước khi tạo trạng thái: {type(exc).__name__}: {exc}")

    def cmd_runs(self, args: list[str]) -> None:
        scene_filter = None
        if args:
            if len(args) == 2 and args[0] == "--scene": scene_filter = args[1]
            else: raise ValueError("cú pháp: runs [--scene alias]")
        active_id = self.active_state.get("run_id") if self.active_state else None
        for path, doc in self._managed_runs():
            if scene_filter and doc.get("scene_alias") != scene_filter: continue
            print(f"{doc.get('run_id')} | {doc.get('scene_id')} | {doc.get('started_utc')} | {doc.get('status')} | stage={doc.get('current_stage') or '-'} | {doc.get('run_root',rel_or_abs(path))}")
        if active_id and not any(d.get("run_id") == active_id for _, d in self._managed_runs()):
            print(f"{active_id} | active stage={self.active_state.get('current_stage')}")

    def cmd_status(self, args: list[str]) -> None:
        if len(args) > 1: raise ValueError("cú pháp: status [run-id]")
        fusion_id = args[0] if args and args[0].startswith("fusion_") else None
        if fusion_id:
            if self.active_fusion and self.active_fusion.get("fusion_result_id") == fusion_id:
                doc = self.active_fusion
            else:
                root = self._fusion_result_root(fusion_id)
                status_path = root / "status.json"
                if not status_path.is_file(): raise FileNotFoundError(f"không tìm thấy fusion result `{fusion_id}`")
                doc = read_json(status_path)
            print(f"Fusion {doc.get('fusion_result_id')}: {doc.get('status')} — stage: {doc.get('current_stage') or 'kết thúc'}")
            for name, stage in doc.get("stages", {}).items(): print(f"  {name}: {stage.get('status')} {stage.get('error','')[:240]}")
            print(f"Input run: {doc.get('input_run_id')} | Viewer: {doc.get('viewer', Path(doc.get('output_path','')) / 'index.html')} | Output: {doc.get('output_path')}")
            if doc.get("error"): print(f"Lỗi gần nhất: {doc['error']}")
            return
        if self.active_object_eval and self.active_object_eval.get("status") in {"queued", "running"} and (
                not args or args[0] == self.active_object_eval.get("run_id")):
            job = self.active_object_eval
            print(f"Object GT evaluation: {job['status']} — run {job['run_id']} / {job['evaluation_id']}")
            print("Predictor run status độc lập; evaluation hậu xử lý không đổi trạng thái completed của run.")
            print(f"Log: {job['log_path']}\nOutput: {job['output_path']}")
            if job.get("error"): print(f"Lỗi: {job['error']}")
            return
        if self.active_state and (not args or args[0] == self.active_state.get("run_id")):
            path, doc = self.active_run, self.active_state
        else: path, doc = self._find_run(args[0] if args else None)
        print(f"{doc.get('run_id')}: {doc.get('status')} — stage: {doc.get('current_stage') or 'kết thúc'}")
        for name, stage in doc.get("stages", {}).items(): print(f"  {name}: {stage.get('status')} {stage.get('error_tail','')[:240]}")
        if doc.get("error"): print(f"Lỗi gần nhất: {doc['error']}")
        print(f"Run: {path}; log: {path / 'logs'}")
        jobs_dir = path / "object_evaluations" / ".jobs"
        if jobs_dir.is_dir():
            jobs = []
            for job_file in jobs_dir.glob("*.json"):
                try: jobs.append(read_json(job_file))
                except Exception: pass
            if jobs:
                latest = max(jobs, key=lambda row: row.get("started_request_utc", ""))
                print(f"Object GT evaluation: {latest.get('status')} — {latest.get('evaluation_id')} (predictor status độc lập)")

    def cmd_logs(self, args: list[str]) -> None:
        run_id = None; stage = None; n = 80; i = 0
        while i < len(args):
            if args[i] == "--stage" and i + 1 < len(args): stage = args[i+1]; i += 2
            elif args[i] == "--tail" and i + 1 < len(args): n = int(args[i+1]); i += 2
            elif not args[i].startswith("-") and run_id is None: run_id = args[i]; i += 1
            else: raise ValueError("cú pháp: logs [run-id] [--stage name] [--tail N]")
        if n < 1 or n > 10000: raise ValueError("--tail phải nằm trong 1..10000")
        if run_id and run_id.startswith("fusion_"):
            root = self._fusion_result_root(run_id)
            status_path = root / "status.json"
            if not status_path.is_file(): raise FileNotFoundError(f"không tìm thấy fusion result `{run_id}`")
            candidates = ([root / "logs" / f"{stage}.log"] if stage else sorted((root / "logs").glob("*.log")))
            if stage == "sidecar": candidates = [root / "logs/p3_sidecar.log"]
            if stage == "fusion": candidates = [root / "logs/fusion.log"]
            log = next((p for p in candidates if p.is_file()), None)
            if not log: raise FileNotFoundError("fusion stage log chưa được tạo")
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"{log} (tail {n})\n" + "\n".join(lines[-n:]))
            return
        if stage in {"object-evaluation", "object_evaluation"}:
            if self.active_object_eval and (run_id is None or run_id == self.active_object_eval.get("run_id")):
                log = Path(self.active_object_eval["log_path"])
            else:
                run_root, _ = self._find_run(run_id)
                candidates = sorted((run_root / "logs").glob("object_evaluation_*.log"))
                log = candidates[-1] if candidates else None
            if log is None or not log.is_file(): raise FileNotFoundError("chưa có object evaluation log")
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            print(f"{log} (tail {n})\n" + "\n".join(lines[-n:]))
            return
        path, doc = self._find_run(run_id)
        if stage:
            candidates = [path / "logs" / f"{stage}.log"]
            if stage == "replay": candidates.insert(0, path / "logs/native_replay.log")
            stage_log = doc.get("stages", {}).get(stage, {}).get("log")
            if stage_log:
                logged_path = Path(stage_log)
                candidates.insert(0, logged_path if logged_path.is_absolute() else path / logged_path)
            log = next((p for p in candidates if p.is_file()), None)
            if not log: raise FileNotFoundError(f"không có log stage `{stage}`")
        else:
            logs = sorted((path / "logs").glob("*.log")) if (path / "logs").is_dir() else []
            if not logs: raise FileNotFoundError("run chưa có log")
            log = max(logs, key=lambda p: p.stat().st_mtime_ns)
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"{log} (tail {n})")
        print("\n".join(lines[-n:]))

    def cmd_results(self, args: list[str]) -> None:
        if len(args) > 1: raise ValueError("cú pháp: results [run-id]")
        if args and args[0].startswith("smooth_t1_"):
            run_id = args[0]
            if Path(run_id).name != run_id:
                raise ValueError("camera-preview run ID không hợp lệ")
            root = (ROOT / "artifacts/smooth_route_previews" / run_id).resolve()
            if not root.is_relative_to((ROOT / "artifacts/smooth_route_previews").resolve()):
                raise ValueError("camera-preview run ID không hợp lệ")
            manifest_path = root / "run_manifest.json"
            if not manifest_path.is_file(): raise FileNotFoundError(f"camera-preview manifest missing: {manifest_path}")
            doc = read_json(manifest_path)
            if doc.get("schema") != "smooth-route-camera-preview-run/v1" or doc.get("run_id") != run_id:
                raise ValueError("camera-preview manifest schema/run ID mismatch")
            print(f"Run ID: {run_id} | Status: {doc.get('status')} | Mode: {doc.get('execution_mode')}")
            print(f"Scene/route: {doc.get('dataset_id')}/{doc.get('scene_id')}/{doc.get('route_id')} | sequence={doc.get('sequence_id')}")
            print(f"Frames/candidates/admitted: {doc.get('summary', {}).get('camera_frames', 'partial')}/"
                  f"{doc.get('summary', {}).get('candidate_keyframes', 'partial')}/"
                  f"{doc.get('summary', {}).get('admitted_raw_keyframes', 'partial')}")
            print(f"Membership: unavailable | Room semantics: unavailable | Predictor/mapper: not run")
            print(f"Manifest: {manifest_path}")
            if doc.get("status") == "completed":
                print(f"Viewer: {root / doc.get('stages', {}).get('viewer_build_validate', {}).get('entrypoint', 'viewer/index.html')}")
                print(f"Open: ./run_scene.sh open {run_id} camera-preview --port 8770")
            else:
                print(f"Failure: {doc.get('error', 'run has not completed')}")
                print(f"Operational log: {root / 'operational/worker.log'}")
            return
        if args and args[0].startswith("d1_"):
            run_id = args[0]
            if Path(run_id).name != run_id:
                raise ValueError("D1 run ID không hợp lệ")
            root = (ROOT / "artifacts/live_processing_runs" / run_id).resolve()
            if not root.is_relative_to((ROOT / "artifacts/live_processing_runs").resolve()):
                raise ValueError("D1 run ID không hợp lệ")
            manifest_path = root / "run_manifest.json"
            if not manifest_path.is_file(): raise FileNotFoundError(f"D1 manifest missing: {manifest_path}")
            doc = read_json(manifest_path)
            receipt_path = root / "run_manifest.sha256.json"
            if receipt_path.is_file():
                receipt = read_json(receipt_path)
                if (receipt.get("schema") != "d1-run-manifest-hash-receipt/v1"
                        or receipt.get("manifest") != manifest_path.name
                        or receipt.get("sha256") != sha256(manifest_path)):
                    raise ValueError(f"D1 root manifest hash receipt mismatch: {receipt_path}")
                print(f"Root run manifest SHA receipt: verified ({receipt_path})")
            elif doc.get("status") in {"completed", "failed", "cancelled"}:
                print("Root run manifest SHA receipt: not recorded (legacy run format)")
            else:
                print("Root run manifest SHA receipt: pending final integrity checkpoint")
            print(f"Run ID: {run_id} | Status: {doc.get('status')} | Mode: {doc.get('execution_mode')}")
            print(f"Scene/sequence: {doc.get('scene_id')} / {doc.get('sequence_id')} | Joined frames: {doc.get('joined_event_count', 0)}")
            print(f"Membership: {doc.get('membership')} | Causality: {doc.get('causality')}")
            d2_viewer = resolve_d2_viewer_entrypoint(root)
            if d2_viewer:
                viewer_path = root / "viewer" / d2_viewer["entrypoint"]
                print(f"D2 XYZ viewer: {viewer_path}")
                print(f"D2 entrypoint manifest: {d2_viewer['manifest_path']}")
                from urllib.parse import urlsplit, urlunsplit
                base_url = doc.get("viewer", {}).get("url")
                if base_url:
                    parsed = urlsplit(base_url)
                    selected_path = "/" + viewer_path.relative_to(ROOT).as_posix()
                    selected_url = urlunsplit((parsed.scheme, parsed.netloc, selected_path, "live=1", ""))
                    print(f"Viewer URL (pointer-selected): {selected_url}")
                else:
                    print("Viewer URL: unavailable (run manifest has no server URL)")
                print(f"Viewer command: ./run_scene.sh open {run_id} live --port {doc.get('viewer', {}).get('port', 8765)}")
                if doc.get("status") == "waiting_for_viewer_ready":
                    print(f"After visually confirming waiting_for_first_revision: ./run_scene.sh start-live {run_id} --confirm-browser-ready --port {doc.get('viewer', {}).get('port', 8765)}")
            else:
                print(f"D1 XY viewer: {root / 'viewer/index.html'} | URL: {doc.get('viewer', {}).get('url')}")
            print(f"Manifest: {manifest_path}")
            if doc.get("status") != "completed": print(f"Error: {doc.get('error')}")
            return
        c2_candidate_root = None
        if args and Path(args[0]).name == args[0]:
            candidate = (HYBRID_MAPPINGS / args[0]).resolve()
            if candidate.is_relative_to(HYBRID_MAPPINGS.resolve()):
                c2_candidate_root = candidate
        c2_candidate_manifest = (c2_candidate_root / "c2_run_manifest.json") if c2_candidate_root else None
        # C2 run IDs are user-selected labels and need not use a `c2_` prefix.
        # The manifest/schema is the authoritative discriminator.
        if args and (args[0].startswith("c2_") or (c2_candidate_manifest and c2_candidate_manifest.is_file())):
            run_id = args[0]
            if Path(run_id).name != run_id:
                raise ValueError("C2 run ID không hợp lệ")
            root = c2_candidate_root or (HYBRID_MAPPINGS / run_id).resolve()
            if not root.is_relative_to(HYBRID_MAPPINGS.resolve()):
                raise ValueError("C2 run ID không hợp lệ")
            manifest_path = root / "c2_run_manifest.json"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"C2 manifest missing: {manifest_path}")
            doc = read_json(manifest_path)
            if (doc.get("schema") != "scene-graph-c2-fresh-replica-run/v1"
                    or doc.get("run_id") != run_id):
                raise ValueError(f"C2 manifest schema/run ID mismatch: {manifest_path}")
            summary = doc.get("summary", {})
            print(f"Run ID: {run_id} | Status: {doc.get('status')} | Mode: {doc.get('execution_mode')}")
            print(f"Scene/sequence: {doc.get('scene_id')} / {doc.get('sequence_id')} | Frames: {summary.get('frame_count', 'unavailable')}")
            print(f"Objects/rooms: {summary.get('final_conceptgraphs_objects', 'unavailable')}/{summary.get('room_count', 'unavailable')} | Membership: {summary.get('membership_status', 'unavailable')} | Room semantics: {summary.get('room_semantics_status', 'unavailable')}")
            print(f"Manifest: {manifest_path}")
            if doc.get("status") == "completed":
                print(f"Unified graph: {doc.get('outputs', {}).get('unified_graph')}")
                print(f"Viewer: {doc.get('outputs', {}).get('viewer')}")
                print(f"Open: ./run_scene.sh open {run_id} hybrid")
            if doc.get("error"): print(f"Error: {doc['error']}")
            return
        if args and args[0].startswith("cached_replay_"):
            run_id = args[0]
            if Path(run_id).name != run_id:
                raise ValueError("cached replay ID không hợp lệ")
            root = (CACHED_REPLAYS / run_id).resolve()
            if not root.is_relative_to(CACHED_REPLAYS.resolve()):
                raise ValueError("cached replay ID không hợp lệ")
            manifest_path = root / "run_manifest.json"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"cached replay manifest missing: {manifest_path}")
            doc = read_json(manifest_path)
            summary = doc.get("summary", {})
            print(f"Run ID: {doc.get('run_id')} | Status: {doc.get('status')} | Mode: {doc.get('execution_mode')}")
            print(f"Scene/sequence: {doc.get('inputs', {}).get('scene_id')} / {doc.get('inputs', {}).get('sequence_id')} | Revisions: {summary.get('revisions', doc.get('inputs', {}).get('frame_count'))}")
            print(f"Objects/rooms/edges at final prefix: {summary.get('objects_final_prefix', 'unavailable')}/{summary.get('rooms_final_prefix', 'unavailable')}/{summary.get('edges_final_prefix', 'unavailable')}")
            print(f"Membership status by revision: {summary.get('membership_status_counts', doc.get('scope_status', {}).get('status_by_input_revision'))}")
            print(f"Room semantics status by revision: {summary.get('room_semantics_status_counts', 'unavailable')}")
            print(f"Runtime by stage: {doc.get('runtime_seconds', {})}")
            print(f"Manifest: {manifest_path}")
            print(f"Viewer: {root / 'viewer/index.html'}")
            if doc.get("status") == "completed": print(f"Open: ./run_scene.sh open {run_id} timeline")
            if doc.get("error"): print(f"Error: {doc['error']}")
            return
        path, doc = self._find_run(args[0] if args else None)
        if doc.get("kind") == "u5_cached_native_replay":
            replay_manifest = read_json(path / "replay/manifest.json") if (path / "replay/manifest.json").is_file() else {}
            cache_root = Path(doc.get("source_cache_run", ""))
            p3_path = cache_root / "detection_3d/detection_3d_manifest.json"
            p3 = read_json(p3_path) if p3_path.is_file() else {}
            snapshot_path = path / "replay/snapshot.json"
            snapshot = read_json(snapshot_path).get("snapshot", {}) if snapshot_path.is_file() else {}
            node_rows = snapshot.get("nodes", [])
            room_count = sum(row.get("kind") == "room" for row in node_rows)
            duplicate_summary = replay_manifest.get("same_frame_duplicate_summary", {}) or {}
            parity_value = (replay_manifest.get("geometry_offline_reference") or {}).get("exact_assignment_parity")
            parity = "not_available" if parity_value is None else parity_value
            print(f"Run ID: {doc.get('run_id')} | Status: {doc.get('status')} | Scene: {doc.get('scene_id')}")
            print(profile_terminal_line(f"Association profile: {doc.get('association_profile', doc.get('profile'))} | Graph backend: {doc.get('graph_backend')}", doc.get("association_profile", doc.get("profile", "source"))))
            print(f"Frames: {doc.get('frames')} | P3 accepted/rejected: {p3.get('detection_3d_count', 'unavailable')}/{p3.get('rejected_detection_count', 'unavailable')} | Objects/rooms: {len([n for n in node_rows if n.get('kind') == 'object'])}/{room_count} | Runtime: {doc.get('runtime_ms', 0)/1000:.1f}s")
            print(f"Dedup aliases/ambiguous/deferred: {duplicate_summary.get('aliased_observations', 'n/a')}/{duplicate_summary.get('ambiguous_observations', 'n/a')}/{duplicate_summary.get('association_deferred_observations', 'n/a')} | Room labeler: {doc.get('room_labeler', 'not run')} | U4-OV: {doc.get('semantic_extension', 'off')}")
            integrity = replay_manifest.get("graph_integrity", {})
            print(f"Checks: fresh={doc.get('fresh_replay_equal')} reload={doc.get('snapshot_reload_equal')} delta={doc.get('delta_replay_equal')} | graph dangling/self/conflicting-owner={integrity.get('dangling_edge_count', 'n/a')}/{integrity.get('self_edge_count', 'n/a')}/{integrity.get('conflicting_room_owner_target_count', 'n/a')} | offline parity={parity}")
            print("Quality metrics: unavailable (cached native replay does not run GT evaluation); unavailable metrics are not zero.")
            if replay_manifest.get("same_frame_duplicate_profile") == "u3_same_frame_duplicate_candidate_v2":
                parity = replay_manifest.get("geometry_offline_reference", {})
                print("Experimental: ambiguous means alias blocked, not automatically deferred; no quality promotion.")
            semantic = replay_manifest.get("u4_semantic_sidecar")
            if semantic:
                sidecar_path = Path(semantic["path"])
                sidecar = read_json(sidecar_path)
                if sha256(sidecar_path) != semantic.get("sha256"):
                    raise ValueError("U4-OV sidecar changed since replay; refusing stale semantic summary")
                counts = sidecar.get("track_status_counts", {})
                coverage = sidecar.get("label_coverage", {})
                unsupported = sidecar.get("unsupported_raw_detector_labels_exact_vocab", [])
                print(f"U4-OV diagnostics only | tracks={sidecar.get('track_count')} confirmed={counts.get('classified', 0)} conflicted={counts.get('conflicted', 0)} unknown={counts.get('unknown', 0)} | label coverage={coverage.get('classified_tracks', 0)}/{coverage.get('total_tracks', sidecar.get('track_count', 0))} | raw labels outside exact vocabulary={unsupported}")
                labelled = [row for row in sidecar.get("observations", []) if row.get("fused_semantic_label") is not None]
                if labelled:
                    for row in labelled:
                        evidence = ",".join(row.get("evidence_ids", [])) or "none"
                        print(f"LABEL {row.get('observation_id')} | raw={row.get('raw_detector_class')} | fused={row.get('fused_semantic_label')} ({row.get('fused_semantic_status')}) | evidence={evidence}")
                else:
                    print("U4-OV abstained on all observations; raw detector classes remain separate (see sidecar evidence table).")
            else:
                print("Semantic labels: U4-OV diagnostics off; no raw fallback reported as fusion.")
            print(f"U5 gate: {replay_manifest.get('u5_gate_status', 'unavailable')}")
            lifecycle = replay_manifest.get("u5_lifecycle_support", {})
            if lifecycle:
                print(f"Lifecycle: protocol=v{lifecycle.get('protocol_version')} | backend events={lifecycle.get('backend_supported_event_kinds')} | merge/retire policy={lifecycle.get('policy_decision')} | real merge/retire observed={lifecycle.get('real_alias_or_retirement_observed')}")
            print(f"Replay manifest: {path / 'replay/manifest.json'}")
            print(f"Graph: {path / 'replay/scene_graph.png'} | Spatial map: {path / 'replay/spatial_map.png'} | Cache: {cache_root}")
            print(f"Output directory: {path}")
            return
        summary = doc.get("summary") or self._summarize(path)
        profile = doc.get("association_profile", "source")
        graph_backend = doc.get("graph_backend", profile)
        profile_line = profile_terminal_line(
            "Association profile: {} | Graph backend: {}".format(profile, graph_backend), profile)
        print(f"Run ID: {doc.get('run_id')} | Status: {doc.get('status')} | Scene: {doc.get('scene_id')} / route {doc.get('route_alias')}")
        print(profile_terminal_line(f"Association profile: {profile} | Graph backend: {doc.get('graph_backend', profile)}", profile))
        print(f"Capture resolution: {summary.get('capture_resolution_px') or doc.get('capture_resolution_px') or 'chưa có'} px")
        p3_manifest = path / "detection_3d/detection_3d_manifest.json"
        p3 = read_json(p3_manifest) if p3_manifest.is_file() else {}
        replay_manifest_path = path / "replay/manifest.json"
        replay_manifest = read_json(replay_manifest_path) if replay_manifest_path.is_file() else {}
        dup = replay_manifest.get("same_frame_duplicate_summary", {}) or {}
        print(f"Frames: {summary.get('processed_frames', 'unavailable')} | P3 accepted/rejected: {p3.get('detection_3d_count', 'unavailable')}/{p3.get('rejected_detection_count', 'unavailable')} | Objects/rooms: {summary.get('objects', 'unavailable')}/{summary.get('room_nodes', 'unavailable')} | Runtime: {replay_manifest.get('runtime_ms', 0)/1000:.1f}s")
        print(f"Dedup aliases/ambiguous/deferred: {dup.get('aliased_observations', 'n/a')}/{dup.get('ambiguous_observations', 'n/a')}/{dup.get('association_deferred_observations', 'n/a')} | Room labeler: {doc.get('room_labeler', 'existing rule-based labeler')} | U4-OV: {doc.get('u4_ov_diagnostics', 'off')}")
        integrity = replay_manifest.get("graph_integrity", {})
        parity_value = (replay_manifest.get("geometry_offline_reference") or {}).get("exact_assignment_parity")
        parity = "not_available" if parity_value is None else parity_value
        if replay_manifest:
            print(f"Checks: fresh={replay_manifest.get('fresh_replay_equal')} reload={replay_manifest.get('snapshot_reload_equal')} delta={replay_manifest.get('delta_replay_equal')} | graph dangling/self/conflicting-owner={integrity.get('dangling_edge_count', 'n/a')}/{integrity.get('self_edge_count', 'n/a')}/{integrity.get('conflicting_room_owner_target_count', 'n/a')} | offline parity={parity}")
        if doc.get("comparison_baseline_run_id"):
            print(f"Comparison baseline: {doc['comparison_baseline_run_id']}")
        for key, label in (("processed_frames","Frames"),("regions","Regions"),("room_nodes","RoomNodes"),("objects","Objects"),
                           ("predicted_labels","Predicted"),("unknown_labels","Unknown"),("ambiguous_labels","Ambiguous")):
            print(f"{label}: {summary.get(key) if summary.get(key) is not None else 'chưa có'}")
        metrics = path / "evaluation/metrics.json"
        if metrics.is_file():
            m = read_json(metrics).get("metrics", {})
            selections = [
                ("Room macro IoU", m.get("room", {}).get("macro_iou_all_gt_rooms")),
                ("Room union IoU", m.get("room", {}).get("union_iou")),
                ("Object-mask precision", m.get("object_detection", {}).get("precision")),
                ("Object-mask recall", m.get("object_detection", {}).get("recall")),
                ("Room semantic accuracy", m.get("semantics", {}).get("accuracy")),
                ("Object-room membership accuracy", m.get("membership", {}).get("conditional_accuracy")),
                ("Doorway adjacency precision", m.get("adjacency", {}).get("precision")),
                ("Doorway adjacency recall", m.get("adjacency", {}).get("recall")),
            ]
            for label, metric in selections:
                if not isinstance(metric, dict):
                    print(f"{label}: chưa có metric trong evaluator")
                elif metric.get("value") is None:
                    reason = metric.get("unavailable_reason") or f"GT status={metric.get('gt_status','unavailable')}"
                    print(f"{label}: chưa có ({reason})")
                else:
                    print(f"{label}: {metric['value']} {metric.get('unit','')} (n={metric.get('sample_count')})")
            print(f"Metrics report: {path / 'evaluation/report.vi.md'}")
        else: print("Metrics: chưa có evaluation/GT scope chưa hỗ trợ; xem manifest nếu metric null.")
        for key in ("spatial_map", "object_evidence"):
            print(f"{key}: {summary.get(key)}")
        print(f"Capture: {path / 'capture'} | Graph: {path / 'replay/scene_graph.png'} | Output directory: {path}")
        comparison = path / "comparison_to_baseline/report.vi.md"
        if comparison.is_file(): print(f"Resolution comparison: {comparison}")

    def _open_target(self, path: Path, kind: str) -> Path | None:
        choices = {
            "report": [path / "comparison_to_baseline/report.vi.md", path / "evaluation/report.vi.md", path / "semantic/report.vi.md"],
            "comparison": [path / "comparison_to_baseline/report.vi.md", path / "comparison_to_baseline/resolution_side_by_side.png"],
            "map": [path / "replay/spatial_map.png"],
            "objects": [path / "semantic/region_object_evidence.html", path / "detection_2d/overlays"],
            "labels": [path / "semantic/region_labels.json", path / "semantic/region_labels.html"],
            "u5": [path / "u4ov_sidecar/index.html", path / "replay/scene_graph.png", path / "replay/spatial_map.png"],
            "object-evaluation": [],
            "object-review": [],
            "folder": [path],
        }.get(kind)
        if kind in {"object-evaluation", "object-review"}:
            root = path / "object_evaluations"
            complete = []
            if root.is_dir():
                for candidate in root.iterdir():
                    manifest = candidate / "manifest.json"
                    if candidate.is_dir() and manifest.is_file():
                        try:
                            doc = read_json(manifest)
                            if doc.get("status") == "completed": complete.append((doc.get("finished_utc", ""), candidate))
                        except Exception: pass
            if not complete: return None
            _, newest = max(complete)
            self._last_opened_object_eval = newest.name
            return newest / ("report.vi.md" if kind == "object-evaluation" else "review/index.html")
        if choices is None: raise ValueError("artifact: report|comparison|map|objects|labels|u5|object-evaluation|object-review|folder")
        return next((p for p in choices if p.exists()), None)

    def _open_cached_timeline(self, run_id: str, port: int) -> int:
        import socket
        import urllib.error
        import urllib.request

        if Path(run_id).name != run_id or not run_id.startswith("cached_replay_"):
            raise ValueError("cached replay ID không hợp lệ")
        run_root = (CACHED_REPLAYS / run_id).resolve()
        if not run_root.is_relative_to(CACHED_REPLAYS.resolve()):
            raise ValueError("cached replay ID không hợp lệ")
        manifest_path = run_root / "run_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"cached replay manifest missing: {manifest_path}")
        manifest = read_json(manifest_path)
        if manifest.get("status") != "completed":
            raise ValueError(f"chỉ mở viewer của cached replay completed; status={manifest.get('status')}")
        viewer_index = run_root / "viewer/index.html"
        if not viewer_index.is_file():
            raise FileNotFoundError(f"cached replay viewer missing: {viewer_index}")
        if not 1024 <= port <= 65535:
            raise ValueError("port phải nằm trong 1024..65535")
        host = "127.0.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))
        except OSError as exc:
            raise ValueError(f"localhost port {port} is busy; choose another with --port PORT. Existing processes are left untouched.") from exc

        viewer_rel = viewer_index.parent.relative_to(ROOT).as_posix()
        url = f"http://{host}:{port}/{viewer_rel}/index.html"
        server_script = RUNNER / "serve_causal_timeline_viewer.py"
        log_path = run_root / f"viewer_server_{port}.log"
        with log_path.open("ab") as log:
            proc = subprocess.Popen([sys.executable, "-B", str(server_script), "--root", str(ROOT),
                                     "--viewer", viewer_rel, "--host", host, "--port", str(port)],
                                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                raise RuntimeError(f"viewer server exited with {proc.returncode}; log={log_path}\n{tail}")
            try:
                with urllib.request.urlopen(url, timeout=0.4) as response:
                    if response.status == 200 and b"<!doctype html" in response.read(512).lower():
                        break
                    raise RuntimeError(f"viewer server returned unexpected response: HTTP {response.status}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                time.sleep(0.1)
        else:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
            raise RuntimeError(f"viewer server did not become ready at {url}; log={log_path}; last_error={last_error}")

        print(f"Cached replay viewer URL: {url}")
        print(f"Viewer server PID: {proc.pid}; log: {log_path}; bind={host}")
        try:
            subprocess.Popen(["xdg-open", url], cwd=ROOT, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            print(f"Browser launch unavailable ({exc}); open this URL manually: {url}")
            return 1
        return 0

    def _open_smooth_route_preview(self, run_id: str, port: int) -> int:
        import socket
        import urllib.error
        import urllib.request

        if Path(run_id).name != run_id or not run_id.startswith("smooth_t1_"):
            raise ValueError("camera-preview run ID không hợp lệ")
        run_root = (ROOT / "artifacts/smooth_route_previews" / run_id).resolve()
        if not run_root.is_relative_to((ROOT / "artifacts/smooth_route_previews").resolve()):
            raise ValueError("camera-preview path escapes the run root")
        manifest_path = run_root / "run_manifest.json"
        if not manifest_path.is_file(): raise FileNotFoundError(f"camera-preview manifest missing: {manifest_path}")
        manifest = read_json(manifest_path)
        if manifest.get("status") != "completed":
            raise ValueError(f"only completed camera-preview runs can be opened; status={manifest.get('status')}")
        entrypoint = manifest.get("stages", {}).get("viewer_build_validate", {}).get("entrypoint")
        if not isinstance(entrypoint, str) or Path(entrypoint).is_absolute() or ".." in Path(entrypoint).parts:
            raise ValueError("camera-preview manifest has an unsafe viewer entrypoint")
        viewer_file = (run_root / entrypoint).resolve()
        if not viewer_file.is_relative_to(run_root) or not viewer_file.is_file():
            raise FileNotFoundError(f"camera-preview viewer entrypoint missing: {viewer_file}")
        hash_manifest = run_root / "artifacts_sha256.json"
        if not hash_manifest.is_file() or sha256(hash_manifest) != manifest.get("artifacts_sha256_manifest_sha256"):
            raise ValueError("camera-preview artifact hash manifest is missing or has changed")
        hash_doc = read_json(hash_manifest)
        for rel, expected in hash_doc.get("files", {}).items():
            asset = (run_root / rel).resolve()
            if not asset.is_relative_to(run_root) or not asset.is_file() or sha256(asset) != expected:
                raise ValueError(f"camera-preview immutable artifact hash mismatch: {rel}")
        if not 1024 <= port <= 65535: raise ValueError("port must be in [1024,65535]")
        viewer_dir_rel = viewer_file.parent.relative_to(ROOT).as_posix()
        viewer_file_rel = viewer_file.relative_to(ROOT).as_posix()
        host = "127.0.0.1"
        url = f"http://{host}:{port}/{viewer_file_rel}"
        already_served = False
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                body = response.read(1024).lower()
                already_served = response.status == 200 and b"camera-only smooth route preview" in body
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if not already_served:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try: probe.bind((host, port))
                except OSError as exc:
                    raise ValueError(f"localhost port {port} is busy and does not serve this preview; choose --port. Existing process left untouched.") from exc
            server_script = RUNNER / "serve_causal_timeline_viewer.py"
            log_path = run_root / f"viewer_server_{port}.log"
            with log_path.open("ab") as log:
                proc = subprocess.Popen([sys.executable, "-B", str(server_script), "--root", str(ROOT),
                                         "--viewer", viewer_dir_rel, "--host", host, "--port", str(port)],
                                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"preview server exited with {proc.returncode}; inspect {log_path}")
                try:
                    with urllib.request.urlopen(url, timeout=0.4) as response:
                        body = response.read(1024).lower()
                        if response.status == 200 and b"camera-only smooth route preview" in body:
                            break
                except (urllib.error.URLError, TimeoutError, OSError):
                    time.sleep(0.1)
            else:
                raise RuntimeError(f"preview server did not become ready at {url}; inspect {log_path}")
        print(f"Camera-only preview URL: {url}")
        print("Server: localhost read-only; existing process not replaced or stopped.")
        try:
            subprocess.Popen(["xdg-open", url], cwd=ROOT, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            print(f"Browser launch unavailable ({exc}); open this URL manually: {url}")
            return 1
        return 0

    def _open_live_timeline(self, run_id: str, port: int) -> int:
        import socket
        import urllib.error
        import urllib.request
        from urllib.parse import urlsplit, urlunsplit
        from standalone_sg.serialization.live_revision_io import atomic_write_json

        if Path(run_id).name != run_id or not run_id.startswith("d1_"):
            raise ValueError("D1 live processing ID không hợp lệ")
        run_root = (ROOT / "artifacts/live_processing_runs" / run_id).resolve()
        if not run_root.is_relative_to((ROOT / "artifacts/live_processing_runs").resolve()):
            raise ValueError("D1 output path escapes live processing run root")
        manifest_path, viewer_index = run_root / "run_manifest.json", run_root / "viewer/index.html"
        if not manifest_path.is_file() or not viewer_index.is_file():
            raise FileNotFoundError(f"D1 viewer/manifest is not available: {run_root}")
        manifest = read_json(manifest_path)
        manifest_port = int(manifest.get("viewer", {}).get("port", port))
        if port == 8765 and manifest_port != 8765:
            # 8765 is the historical default; honor the run's explicit port unless
            # the operator supplies a different non-default port for a reason.
            port = manifest_port
        if not 1024 <= port <= 65535: raise ValueError("port phải nằm trong 1024..65535")
        d2_viewer = resolve_d2_viewer_entrypoint(run_root)
        entrypoint = d2_viewer["entrypoint"] if d2_viewer else viewer_index.name
        viewer_target = viewer_index.parent / entrypoint
        if not viewer_target.is_file():
            raise FileNotFoundError(f"viewer entrypoint is missing: {viewer_target}")
        viewer_rel = viewer_index.parent.relative_to(ROOT).as_posix()
        viewer_file_rel = (viewer_index.parent / entrypoint).relative_to(ROOT).as_posix()
        host = "127.0.0.1"
        url = f"http://{host}:{port}/{viewer_file_rel}?live=1"
        if manifest.get("viewer", {}).get("server_mode") == "existing_read_only_loopback_server":
            existing_url = manifest["viewer"].get("url")
            if not existing_url:
                raise ValueError("manifest ghi server ngoài nhưng thiếu viewer URL")
            if int(manifest["viewer"].get("port", -1)) != port:
                raise ValueError(f"D1 đang dùng server có sẵn ở port {manifest['viewer'].get('port')}; dùng --port {manifest['viewer'].get('port')}")
            parsed = urlsplit(existing_url)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("D1 existing viewer URL must be an absolute loopback URL")
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("D1 existing viewer server is not bound to a loopback host")
            candidate_paths = ["/" + viewer_file_rel, "/" + (Path("viewer") / entrypoint).as_posix()]
            failures: list[str] = []
            selected_url = None
            for candidate_path in dict.fromkeys(candidate_paths):
                candidate_url = urlunsplit((parsed.scheme, parsed.netloc, candidate_path, "live=1", ""))
                try:
                    with urllib.request.urlopen(candidate_url, timeout=1.0) as response:
                        if response.status == 200 and b"<!doctype html" in response.read(512).lower():
                            selected_url = candidate_url
                            break
                        failures.append(f"{candidate_path}: HTTP {response.status}/non-HTML")
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    failures.append(f"{candidate_path}: {type(exc).__name__}")
            if selected_url is None:
                raise RuntimeError(f"existing server on port {port} does not serve the manifest-selected viewer ({'; '.join(failures)}); server was left untouched")
            url = selected_url
            print(f"D2 XYZ viewer URL (existing read-only server; process not managed by this run): {url}")
            try:
                subprocess.Popen(["xdg-open", url], cwd=ROOT, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                print(f"Browser launch unavailable ({exc}); open this URL manually: {url}")
            print("Launcher did not replace or stop the existing server.")
            return 0
        already_served = False
        try:
            with urllib.request.urlopen(url, timeout=0.7) as response:
                already_served = response.status == 200 and b"<!doctype html" in response.read(256).lower()
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if not already_served:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try: probe.bind((host, port))
                except OSError as exc:
                    raise ValueError(f"localhost port {port} is busy and does not serve this D1 viewer; choose another. Existing server left untouched.") from exc
            server_script = RUNNER / "serve_causal_timeline_viewer.py"
            log_path = run_root / f"viewer_server_{port}.log"
            with log_path.open("ab") as log:
                proc = subprocess.Popen([sys.executable, "-B", str(server_script), "--root", str(ROOT),
                                         "--viewer", viewer_rel, "--host", host, "--port", str(port)],
                                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"D1 viewer server exited with {proc.returncode}; inspect {log_path}")
                try:
                    with urllib.request.urlopen(url, timeout=0.4) as response:
                        if response.status == 200: break
                except (urllib.error.URLError, TimeoutError, OSError): time.sleep(0.1)
            else:
                self._stop_process_group(proc)
                raise RuntimeError(f"D1 viewer server failed to start at {url}; inspect {log_path}")
            manifest.setdefault("viewer_servers", []).append({"pid": proc.pid, "port": port,
                "url": url, "status": "serving", "log": str(log_path)})
            atomic_write_json(manifest_path, manifest)
        label = "D2 XYZ viewer" if d2_viewer else "D1 XY viewer"
        print(f"{label} URL: {url}")
        try:
            subprocess.Popen(["xdg-open", url], cwd=ROOT, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            print(f"Browser launch unavailable ({exc}); open URL manually: {url}")
        return 0

    def cmd_open(self, args: list[str]) -> int:
        run_id = None; kind = "report"; port = 8765; i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--port" and i + 1 < len(args):
                port = int(args[i + 1]); i += 2; continue
            if arg in {"report", "comparison", "map", "objects", "labels", "u5", "fusion", "fusion-comparison", "unified", "hybrid", "timeline", "live", "camera-preview", "object-evaluation", "object-review", "folder", "association-report", "association-gallery"}:
                kind = arg
            elif run_id is None: run_id = arg
            else: raise ValueError("cú pháp: open [run-id] [report|comparison|map|objects|labels|u5|fusion|fusion-comparison|unified|hybrid|timeline|folder] [--port PORT]")
            i += 1
        if run_id and run_id.startswith("cached_replay_"):
            if kind != "timeline": raise ValueError("cached replay chỉ hỗ trợ `open <run-id> timeline [--port PORT]`")
            return self._open_cached_timeline(run_id, port)
        if run_id and run_id.startswith("smooth_t1_"):
            if kind != "camera-preview": raise ValueError("camera-only run dùng `open <run-id> camera-preview [--port PORT]`")
            return self._open_smooth_route_preview(run_id, port)
        if run_id and run_id.startswith("d1_"):
            if kind != "live": raise ValueError("D1 run dùng `open <run-id> live [--port PORT]`")
            return self._open_live_timeline(run_id, port)
        if run_id and kind == "fusion-comparison":
            comparison_root = (OBJECT_FUSIONS / "comparisons" / run_id).resolve()
            if (Path(run_id).name != run_id or not run_id.startswith("fusion_compare_")
                    or not comparison_root.is_relative_to((OBJECT_FUSIONS / "comparisons").resolve())):
                raise ValueError("fusion comparison ID không hợp lệ")
            target = comparison_root / "index.html"
            if not target.is_file(): raise FileNotFoundError(f"không có viewer fusion comparison: {target}")
            print(f"Mở source/candidate object fusion comparison: {target}")
            try: subprocess.Popen(["xdg-open", str(target)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc: print(f"Không mở được GUI ({exc}); đường dẫn: {target}")
            return 0
        if run_id and kind == "hybrid":
            if Path(run_id).name != run_id:
                raise ValueError("hybrid mapping ID không hợp lệ")
            hybrid_root = (HYBRID_MAPPINGS / run_id).resolve()
            if not hybrid_root.is_relative_to(HYBRID_MAPPINGS.resolve()):
                raise ValueError("hybrid mapping ID không hợp lệ")
            target = hybrid_root / "hybrid_viewer.html"
            if not target.is_file(): raise FileNotFoundError(f"không có hybrid mapping viewer: {target}")
            print(f"Mở hybrid object mapping pre-filter/final: {target}")
            try: subprocess.Popen(["xdg-open", str(target)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc: print(f"Không mở được GUI ({exc}); đường dẫn: {target}")
            return 0
        if run_id and kind == "fusion":
            root = self._fusion_result_root(run_id)
            target = root / "index.html"
            if not target.is_file(): raise FileNotFoundError(f"không có viewer fusion: {target}")
            print(f"Mở fixed-assignment geometry fusion: {target}")
            try:
                subprocess.Popen(["xdg-open", str(target)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                print(f"Không mở được GUI ({exc}); đường dẫn: {target}")
            return 0
        if run_id and kind == "unified":
            if Path(run_id).name != run_id or not run_id.startswith("unified_"):
                raise ValueError("unified graph ID không hợp lệ")
            target = (ROOT / "artifacts/unified_graphs" / run_id / "index.html").resolve()
            if not target.is_relative_to((ROOT / "artifacts/unified_graphs").resolve()) or not target.is_file():
                raise FileNotFoundError(f"không có unified graph viewer: {target}")
            print(f"Mở unified object–room graph: {target}")
            try: subprocess.Popen(["xdg-open", str(target)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc: print(f"Không mở được GUI ({exc}); đường dẫn: {target}")
            return 0
        if run_id and kind in {"association-report", "association-gallery"}:
            comparison_root = (ROOT / "artifacts/profile_comparisons" / run_id).resolve()
            if not comparison_root.is_relative_to((ROOT / "artifacts/profile_comparisons").resolve()):
                raise ValueError("comparison ID không hợp lệ")
            target = comparison_root / ("report.vi.md" if kind == "association-report" else "gallery.html")
            if not target.is_file(): raise FileNotFoundError(f"không có {kind} cho comparison {run_id}")
            print(f"Mở: {target}")
            try: subprocess.Popen(["xdg-open", str(target)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                print(f"Không mở được GUI ({exc}); đường dẫn: {target}")
                return 1
            return 0
        path, _ = self._find_run(run_id)
        target = self._open_target(path, kind)
        if target is None:
            print(f"Chưa có artifact `{kind}` trong run: {path}; pipeline không bị ảnh hưởng.")
            return 1
        eval_id = getattr(self, "_last_opened_object_eval", None) if kind in {"object-evaluation", "object-review"} else None
        print(f"Mở evaluation {eval_id}: {target}" if eval_id else f"Mở: {target}")
        try: subprocess.Popen(["xdg-open", str(target)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            print(f"Không mở được GUI ({exc}); đường dẫn: {target}")
            return 1
        return 0

    def cmd_evaluate(self, args: list[str]) -> int:
        if len(args) != 1: raise ValueError("cú pháp: evaluate <run-id>")
        path, doc = self._find_run(args[0])
        if doc.get("status") != "completed": raise ValueError("chỉ evaluate run đã completed; không chạy lại capture/perception")
        reg = scene_entry(doc["scene_alias"], self.registry)
        ctx = _scene_context(reg, doc["route_alias"])
        run_config = path / "effective_route_config.json"
        profile_path = path / "effective_evaluation_profile.json"
        profile = read_json(profile_path) if profile_path.is_file() else ctx["profile"]
        evroot = path / "evaluations"
        out = evroot / (datetime.now(timezone.utc).strftime("eval_%Y%m%dT%H%M%S.%fZ") + "_" + uuid.uuid4().hex[:6])
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self.env["project_python"], "-B", str(RUNNER / "evaluate_habitat_run.py"),
            "--capture", str(path / "capture"), "--replay", str(path / "replay"), "--grid-trace", str(path / "grid_trace"),
            "--semantic", str(path / "semantic"), "--run-config", str(run_config), "--evaluation-profile", str(profile_path if profile_path.is_file() else ctx["profile_path"]),
            "--output", str(out), "--run-id", doc["run_id"]]
        room_gt = profile.get("ground_truth", {}).get("room_polygons")
        if room_gt: cmd.extend(["--room-gt", str(resolve_path(room_gt))])
        log = path / "logs" / f"evaluate_{out.name}.log"
        lock = LOCK_PATH.open("a+")
        try:
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Không evaluate đồng thời với run/check launcher khác.")
                return 1
            started = datetime.now(timezone.utc).isoformat()
            code, tail = self._subprocess(cmd, stage="evaluation", log_path=log, env=self._stage_env(project=True))
            if code or not (out / "manifest.json").is_file():
                print(f"Evaluation thất bại; run gốc không đổi. {tail}")
                return 1
            doc.setdefault("additional_evaluations", []).append({"output": rel_or_abs(out), "started_utc": started,
                "status": read_json(out / "manifest.json").get("status"), "manifest_sha256": sha256(out / "manifest.json")})
            atomic_json(path / "launcher_manifest.json", doc)
            print(f"Evaluation mới: {out}\nReport: {out / 'report.vi.md'}")
            return 0
        finally:
            lock.close()

    def _object_eval_job_path(self, run_root: Path, evaluation_id: str) -> Path:
        return run_root / "object_evaluations" / ".jobs" / f"{evaluation_id}.json"

    def _execute_object_evaluation(self, run_root: Path, run_doc: dict, output: Path, log_path: Path,
                                   job_path: Path) -> int:
        lock = LOCK_PATH.open("a+")
        job = self.active_object_eval or {}
        try:
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                job.update({"status": "blocked", "finished_utc": datetime.now(timezone.utc).isoformat(),
                            "error": "another launcher pipeline/check/evaluation holds the shared lock"})
                atomic_json(job_path, job); print("Không chạy object evaluation đồng thời với pipeline/check/evaluation.")
                return 1
            job.update({"status": "running", "started_utc": datetime.now(timezone.utc).isoformat()})
            atomic_json(job_path, job)
            cmd = [self.env["project_python"], "-B", str(RUNNER / "evaluate_hm3d_object_gt.py"),
                   "--run-root", str(run_root), "--output", str(output),
                   "--mapping", str(resolve_path("configs/evaluation/hm3d_00802_object_category_mapping_v1.json")),
                   "--habitat-python", self.env["habitat_python"]]
            code, tail = self._subprocess(cmd, stage="object_evaluation", log_path=log_path, env=self._stage_env(project=True),
                                          progress_prefix="PROGRESS:")
            job["exit_code"] = code
            manifest_path = output / "manifest.json"
            evaluation_status = read_json(manifest_path).get("status") if manifest_path.is_file() else None
            if code == 0 and evaluation_status == "completed":
                job.update({"status": "completed", "finished_utc": datetime.now(timezone.utc).isoformat(), "evaluation_manifest": str(manifest_path)})
                print(f"Object evaluation hoàn tất — Evaluation ID: {output.name}\nReport: {output / 'report.vi.md'}")
                try: self.cmd_object_results([run_doc.get("run_id", run_root.name), "--eval-id", output.name])
                except Exception as exc: print(f"Không in được bảng tổng tự động ({exc}); dùng object-results {run_root.name} --eval-id {output.name} để đọc lại.")
                return 0
            job.update({"status": "cancelled" if code == 130 else "failed", "finished_utc": datetime.now(timezone.utc).isoformat(),
                        "error": tail[-2500:] or f"evaluation runner exit={code}"})
            if manifest_path.is_file():
                evaluation = read_json(manifest_path)
                evaluation.update({"status": "cancelled" if code == 130 else "failed",
                                   "finished_utc": datetime.now(timezone.utc).isoformat(), "launcher_job_error": job["error"]})
                atomic_json(manifest_path, evaluation)
            print(f"Object evaluation {job['status']}; predictor run vẫn giữ nguyên trạng thái. Partial: {output}\n{tail[-1000:]}")
            return 1
        finally:
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally: lock.close()
            atomic_json(job_path, job)

    def _interactive_object_eval(self, run_root: Path, run_doc: dict, output: Path, log_path: Path, job_path: Path) -> None:
        self._execute_object_evaluation(run_root, run_doc, output, log_path, job_path)

    def cmd_evaluate_objects(self, args: list[str], *, interactive: bool = False) -> int:
        if not args: raise ValueError("cú pháp: evaluate-objects <run-id> [--check [--json]]")
        run_id = args[0]; check_only = "--check" in args[1:]; json_output = "--json" in args[1:]
        if any(arg not in {"--check", "--json"} for arg in args[1:]) or len(set(args[1:])) != len(args[1:]):
            raise ValueError("cú pháp: evaluate-objects <run-id> [--check [--json]]")
        if json_output and not check_only: raise ValueError("--json chỉ dùng với --check")
        run_root, run_doc = self._find_run(run_id)
        if run_doc.get("kind") == "u5_cached_native_replay" or run_doc.get("source_cache_run"):
            source_run = run_doc.get("source_cache_run") or "full HM3D source run"
            raise ValueError(f"{run_id} là cached graph replay, không chứa đầu vào đầy đủ cho object GT evaluator. "
                             f"Dùng source run {source_run} cho /evaluate-objects; lưu ý evaluator chấm masks/P3, "
                             "không chấm chất lượng association của cached candidate.")
        if run_doc.get("status") != "completed": raise ValueError("chỉ đánh giá run predictor đã completed")
        if run_doc.get("dataset") != "hm3d": raise ValueError("HM3D semantic-object evaluator hiện chỉ hỗ trợ run HM3D")
        script = RUNNER / "evaluate_hm3d_object_gt.py"
        if check_only:
            cmd = [self.env["project_python"], "-B", str(script), "--run-root", str(run_root), "--check"]
            if json_output: cmd.append("--json")
            result = subprocess.run(cmd, cwd=ROOT, env=self._stage_env(project=True), text=True, capture_output=True, check=False)
            if result.stdout: print(result.stdout.rstrip())
            if result.stderr: print(result.stderr.rstrip())
            return result.returncode
        # Fail before creating an evaluation directory/job when immutable
        # inputs are unavailable. This gives a clear, synchronous reason and
        # lets the user retry after fixing a resource binding.
        preflight_cmd = [self.env["project_python"], "-B", str(script), "--run-root", str(run_root),
                         "--check"]
        preflight_result = subprocess.run(preflight_cmd, cwd=ROOT, env=self._stage_env(project=True),
                                          text=True, capture_output=True, check=False)
        if preflight_result.stdout: print(preflight_result.stdout.rstrip())
        if preflight_result.stderr: print(preflight_result.stderr.rstrip(), file=sys.stderr)
        if preflight_result.returncode:
            print("Object evaluation chưa bắt đầu; xem blockers ở preflight phía trên. Run predictor không bị thay đổi.")
            return preflight_result.returncode
        if self.active_thread and self.active_thread.is_alive(): raise RuntimeError("launcher pipeline đang chạy; chờ hoặc hủy trước object evaluation")
        if self.active_object_eval_thread and self.active_object_eval_thread.is_alive(): raise RuntimeError("đã có object evaluation đang chạy")
        evaluation_id = "eval_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "_" + uuid.uuid4().hex[:6]
        output = run_root / "object_evaluations" / evaluation_id
        if output.exists(): raise FileExistsError(f"evaluation output already exists: {output}")
        log_path = run_root / "logs" / f"object_evaluation_{evaluation_id}.log"
        job_path = self._object_eval_job_path(run_root, evaluation_id)
        job = {"schema_version": 1, "kind": "object_gt_evaluation_job", "run_id": run_id,
               "evaluation_id": evaluation_id, "status": "queued", "output_path": str(output), "log_path": str(log_path),
               "started_request_utc": datetime.now(timezone.utc).isoformat(), "predictor_status_isolated": True}
        self.active_object_eval = job
        self.cancel_requested.clear()
        atomic_json(job_path, job)
        if interactive:
            self.active_object_eval_thread = threading.Thread(target=self._interactive_object_eval,
                args=(run_root, run_doc, output, log_path, job_path), daemon=False)
            self.active_object_eval_thread.start()
            print(f"Đã khởi chạy object evaluation {evaluation_id}; /status {run_id}, /logs {run_id} --stage object-evaluation, /cancel {run_id} dùng được ngay.")
            return 0
        return self._execute_object_evaluation(run_root, run_doc, output, log_path, job_path)

    def cmd_object_results(self, args: list[str]) -> int:
        if not args: raise ValueError("cú pháp: object-results <run-id> [--frames | --frame N] [--eval-id ID] [--json]")
        run_id = args[0]; frames = False; frame = None; eval_id = None; json_output = False; i = 1
        while i < len(args):
            value = args[i]
            if value == "--frames" and not frames and frame is None:
                frames = True; i += 1
            elif value == "--frame" and i + 1 < len(args) and not frames and frame is None:
                try: frame = int(args[i + 1])
                except ValueError: raise ValueError("--frame cần frame index dạng số nguyên bắt đầu từ 0")
                i += 2
            elif value == "--eval-id" and i + 1 < len(args) and eval_id is None:
                eval_id = args[i + 1]; i += 2
            elif value == "--json" and not json_output:
                json_output = True; i += 1
            else:
                raise ValueError("cú pháp: object-results <run-id> [--frames | --frame N] [--eval-id ID] [--json]")
        run_root, _ = self._find_run(run_id)
        cmd = [self.env["project_python"], "-B", str(RUNNER / "evaluate_hm3d_object_gt.py"),
               "--run-root", str(run_root), "--object-results"]
        if frames: cmd.append("--frames")
        if frame is not None: cmd.extend(["--frame", str(frame)])
        if eval_id: cmd.extend(["--eval-id", eval_id])
        if json_output: cmd.append("--json")
        result = subprocess.run(cmd, cwd=ROOT, env=self._stage_env(project=True), text=True, capture_output=True, check=False)
        if result.stdout:
            output = result.stdout.rstrip()
            print(output if json_output else colorize_object_results(output))
        if result.stderr: print(result.stderr.rstrip(), file=sys.stderr)
        return result.returncode

    def cmd_cancel(self, args: list[str]) -> int:
        if len(args) > 1: raise ValueError("cú pháp: cancel [run-id]")
        if self.active_fusion_thread and self.active_fusion_thread.is_alive() and self.active_fusion:
            if args and args[0] != self.active_fusion.get("fusion_result_id"):
                raise ValueError("run-id không trùng object fusion đang chạy")
            self.cancel_requested.set()
            print(f"Đã gửi graceful cancel cho object fusion {self.active_fusion.get('fusion_result_id')}; partial result được giữ lại.")
            return 0
        if self.active_object_eval_thread and self.active_object_eval_thread.is_alive() and self.active_object_eval:
            if args and args[0] != self.active_object_eval.get("run_id"):
                raise ValueError("run-id không trùng object evaluation đang chạy")
            self.cancel_requested.set()
            print(f"Đã gửi graceful cancel object evaluation {self.active_object_eval.get('evaluation_id')}; predictor run không đổi.")
            return 0
        if not self.active_thread or not self.active_thread.is_alive() or not self.active_state:
            print("Không có run active trong launcher phiên này."); return 1
        if args and args[0] != self.active_state.get("run_id"):
            raise ValueError("chỉ cancel run do phiên launcher này quản lý")
        self.cancel_requested.set()
        print(f"Đã gửi yêu cầu graceful cancel cho {self.active_state.get('run_id')}; giữ partial artifacts.")
        return 0

    def cmd_register(self, args: list[str]) -> None:
        if len(args) != 2 or args[0] != "--config": raise ValueError("cú pháp: register --config <scene-entry.json>")
        config_path = resolve_path(args[1])
        entry = read_json(config_path)
        trial = dict(self.registry)
        trial["scenes"] = self.registry["scenes"] + [entry]
        validate_registry(trial)
        _scene_context(entry)
        temp = dict(entry)
        temp["evaluation_profile"] = rel_or_abs(resolve_path(entry["evaluation_profile"]))
        temp["routes"] = {k: rel_or_abs(resolve_path(v)) for k, v in entry["routes"].items()}
        self.registry["scenes"].append(temp)
        atomic_json(self.registry_path, self.registry)
        print(f"Đã đăng ký `{entry['alias']}`. Chưa chạy simulator; dùng `check {entry['alias']}` để xác minh.")

    def _print_summary(self, doc: dict) -> None:
        s = doc.get("summary", {})
        profile = doc.get("association_profile", "source")
        print(f"Run ID: {doc.get('run_id')}\nScene / route: {doc.get('scene_id')} / {doc.get('route_alias')}\nStatus: {doc.get('status')}\n"
              f"{profile_line}\n"
              f"Capture resolution: {s.get('capture_resolution_px') or doc.get('capture_resolution_px')} px\n"
              f"P3 accepted / rejected: {s.get('p3_accepted')} / {s.get('p3_rejected')}\n"
              f"Duplicate aliases / ambiguous / association deferred: {s.get('duplicate_aliases', 'n/a')} / {s.get('duplicate_ambiguous', 'n/a')} / {s.get('association_deferred', 'n/a')}\n"
              f"Room labeler: {doc.get('room_labeler', 'existing rule-based labeler')} | U4-OV: {doc.get('u4_ov_diagnostics', 'off')}\n"
              f"Comparison baseline: {doc.get('comparison_baseline_run_id') or 'n/a'}\n"
              f"Processed frames: {s.get('processed_frames')}\nRegions / RoomNodes / Objects: {s.get('regions')} / {s.get('room_nodes')} / {s.get('objects')}\n"
              f"Predicted / Unknown / Ambiguous labels: {s.get('predicted_labels')} / {s.get('unknown_labels')} / {s.get('ambiguous_labels')}\n"
              f"Metrics report: {s.get('metrics_report')}\nSpatial map: {s.get('spatial_map')}\nObject evidence: {s.get('object_evidence')}\n"
              f"Resolution comparison: {s.get('comparison_report') if doc.get('comparison_baseline_run_id') else 'n/a'}\n"
              f"Output directory: {doc.get('run_root')}")

    def interactive(self) -> int:
        print("Habitat Scene Launcher — gõ /help hoặc /all; /exit để thoát.")
        while True:
            try: raw = input("scene> ").strip()
            except EOFError: raw = "/exit"
            except KeyboardInterrupt:
                if ((self.active_thread and self.active_thread.is_alive()) or
                        (self.active_object_eval_thread and self.active_object_eval_thread.is_alive()) or
                        (self.active_fusion_thread and self.active_fusion_thread.is_alive())):
                    print("Ctrl+C: gửi yêu cầu dừng an toàn; giữ partial artifacts.")
                    self.cancel_requested.set()
                    continue
                return 130
            if not raw: continue
            try: args = shlex.split(raw)
            except ValueError as exc:
                print(f"Cú pháp: {exc}"); continue
            exit_code = self.handle(args, interactive=True)
            if exit_code == 10:
                if ((self.active_thread and self.active_thread.is_alive()) or
                        (self.active_object_eval_thread and self.active_object_eval_thread.is_alive()) or
                        (self.active_fusion_thread and self.active_fusion_thread.is_alive())):
                    print("Job đang chạy. Gõ /cancel để dừng an toàn; /status để theo dõi; /exit không bỏ orphan worker.")
                    continue
                return 0


def shutil_copy(source: Path, destination: Path) -> None:
    import shutil
    shutil.copy2(source, destination)


def main() -> int:
    if len(sys.argv) == 1:
        try:
            app = Launcher()
            app._recover_interrupted()
            return app.interactive()
        except Exception as exc:
            print(f"Launcher không khởi động được: {exc}", file=sys.stderr)
            return 2
    try:
        app = Launcher()
        app._recover_interrupted()
        return app.handle(sys.argv[1:])
    except Exception as exc:
        print(f"Launcher error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
