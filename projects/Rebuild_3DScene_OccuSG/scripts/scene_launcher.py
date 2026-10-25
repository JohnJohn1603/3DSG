#!/usr/bin/env python3
"""Thin, safe CLI/orchestrator for the existing Habitat workflow."""
from __future__ import annotations

import fcntl
import hashlib
import json
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
REGISTRY = ROOT / "configs/launcher/scenes.json"
LOCAL_ENV = ROOT / "configs/launcher/local_environment.json"
RUNS = ROOT / "artifacts/runs"
LOCK_PATH = RUNS / ".launcher.lock"
ACTIVE_POINTER = RUNS / ".launcher_active.json"
RUNNER = ROOT / "scripts"
COMMANDS = ("help", "all", "maps", "use", "info", "routes", "check", "run", "runs",
            "status", "logs", "results", "open", "evaluate", "evaluate-objects", "object-results", "cancel", "register", "exit")


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


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def rel_or_abs(path: Path) -> str:
    path = path.resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def resolve_path(value: str | Path) -> Path:
    p = Path(value).expanduser()
    return (ROOT / p).resolve() if not p.is_absolute() else p.resolve()


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
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "scenes"} or doc["schema_version"] != 1:
        raise ValueError("registry requires exactly schema_version=1 and scenes")
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


def load_registry(path: Path = REGISTRY) -> dict:
    doc = read_json(path)
    validate_registry(doc)
    return doc


def load_environment(path: Path = LOCAL_ENV) -> dict:
    doc = read_json(path)
    required = {"schema_version", "project_python", "habitat_python", "pythonpath", "mplconfigdir",
                "detection_3d_profile", "native_m2_m3", "native_dude", "project_package_pins"}
    if not isinstance(doc, dict) or set(doc) != required or doc["schema_version"] != 1:
        raise ValueError(f"local environment config must have exactly: {', '.join(sorted(required))}")
    for key in ("project_python", "habitat_python", "detection_3d_profile", "native_m2_m3", "native_dude"):
        if key == "project_python":
            # Keep a venv's lexical bin/python path: resolving its symlink can
            # turn it into /usr/bin/python and bypass the venv site-packages.
            interpreter = Path(doc[key]).expanduser()
            doc[key] = str((ROOT / interpreter).absolute() if not interpreter.is_absolute() else interpreter.absolute())
        else:
            doc[key] = str(resolve_path(doc[key]))
    doc["mplconfigdir"] = str(Path(doc["mplconfigdir"]).expanduser())
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
        raise ValueError(f"comparison baseline is incomplete: {baseline_id}")
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
        for key in ("project_python", "habitat_python", "detection_3d_profile", "native_m2_m3", "native_dude"):
            p = Path(env[key])
            if not p.is_file():
                missing.append(f"thiếu {key}: {p}")
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

    def _run_pipeline(self, alias: str, route_alias: str, dry_run: bool = False) -> dict:
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
                      "--habitat-python", self.env["habitat_python"]]
            self.active_run = run_root
            self.active_state = {"schema_version": 1, "kind": "habitat_launcher_run", "run_id": run_id,
                "scene_alias": alias, "scene_id": dataset.get("scene_id"), "dataset": dataset.get("type"),
                "route_alias": route_alias, "status": "preflighting", "current_stage": "preflight",
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
            atomic_json(run_root / "effective_route_config.json", route)
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
            self.active_state["input_sha256"] = {rel_or_abs(p): sha256(p) for p in configured_hash_paths if p and p.is_file()}
            self.active_state["input_sha256"][rel_or_abs(run_root / "effective_route_config.json")] = sha256(run_root / "effective_route_config.json")
            self.active_state["effective_config"] = {
                "route_config_path": rel_or_abs(run_root / "effective_route_config.json"),
                "route_config_sha256": sha256(run_root / "effective_route_config.json"),
                "capture_resolution_px": route.get("resolution_px"),
            }
            if comparison_baseline:
                for field in ("launcher_manifest", "capture_manifest", "effective_route_config"):
                    path = RUNS / comparison_baseline["run_id"] / {
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
                "--scene", str(resolve_path(scene_arg)), "--config", str(route_path), "--output", str(cap_dir)],
                "capture.log", py_env=common_env, required=[cap_dir / "capture_manifest.json"], run_root=run_root,
                progress_prefix="PROGRESS:")
            cap_manifest = read_json(cap_dir / "capture_manifest.json")
            if cap_manifest.get("status") != "pass" or cap_manifest.get("frame_count") != len(route.get("route", [])):
                raise RuntimeError("capture contract mismatch: status/frame_count không khớp route")

            detector_cfg = resolve_path(profile["perception"]["detector_profile"])
            weights = resolve_path(profile["perception"]["weights"])
            self._run_logged_stage("detection_2d", [self.env["project_python"], "-B", str(RUNNER / "run_replica_cad_instance_segmentation.py"),
                "--capture", str(cap_dir), "--output", str(d2_dir), "--config", str(detector_cfg), "--weights", str(weights),
                "--names", str(ROOT / "configs/perception/coco.names")], "detection_2d.log", py_env=common_env,
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
                "--pipeline-config", str(resolve_path(profile["pipeline_config"])), "--m2-m3-binary", self.env["native_m2_m3"],
                "--dude-binary", self.env["native_dude"], "--output", str(replay_dir), "--grid-output", str(grid_dir)]
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
                    "--baseline", str(RUNS / comparison_baseline["run_id"]), "--candidate", str(run_root),
                    "--output", str(comparison_dir)]
                self._run_logged_stage("comparison", compare_cmd, "comparison.log", py_env=common_env,
                    required=[comparison_dir / "report.vi.md", comparison_dir / "resolution_side_by_side.png",
                              comparison_dir / "manifest.json"], run_root=run_root)
            self.active_state["status"] = "completed"
            self.active_state["finished_utc"] = datetime.now(timezone.utc).isoformat()
            self.active_state["current_stage"] = None
            self.active_state["summary"] = self._summarize(run_root)
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
                                  "capture_resolution_px": None,
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
                "routes": "routes [scene-alias]", "check": "check [scene-alias] [--route route-alias]",
                "run": "run [scene-alias] [--route route-alias] [--dry-run]", "runs": "runs [--scene alias]",
                "status": "status [run-id]", "logs": "logs [run-id] [--stage stage] [--tail N]",
                "results": "results [run-id]", "open": "open [run-id] [report|comparison|map|objects|labels|object-evaluation|object-review|folder]",
                "evaluate": "evaluate <run-id>", "cancel": "cancel [run-id]", "register": "register --config <scene-entry.json>",
                "evaluate-objects": "evaluate-objects <run-id> [--check]",
                "object-results": "object-results <run-id> [--frames | --frame N] [--eval-id ID] [--json]",
            }
            print(f"Cú pháp: {usage.get(args[0], args[0])}")
            return
        print("Workflow: maps → info → check → run → status/logs/results/open.\n"
              "Lệnh: help [command], all, maps, use <alias>, info [alias], routes [alias],\n"
              "check [alias] [--route name], run [alias] [--route name] [--dry-run],\n"
              "runs [--scene alias], status [run-id], logs [run-id] [--stage stage] [--tail N],\n"
              "results [run-id], open [run-id] [report|comparison|map|objects|labels|object-evaluation|object-review|folder],\n"
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
                altered["dataset"][key] = route[key]
        for key in ("resolution_px", "horizontal_fov_deg"):
            if key in route:
                altered["sensor"][key] = route[key]
        if altered == base:
            return ctx["profile_path"], None
        temp = Path(tempfile.gettempdir()) / f"scene-launcher-check-{uuid.uuid4().hex}.json"
        temp.write_text(json.dumps(altered, indent=2) + "\n", encoding="utf-8")
        return temp, temp

    def cmd_check(self, args: list[str]) -> int:
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
               "--run-root", str(run_probe), "--habitat-python", self.env["habitat_python"]]
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

    def cmd_run(self, args: list[str], *, interactive: bool) -> int:
        alias = self.selected
        tail = list(args)
        if tail and not tail[0].startswith("-"): alias, tail = tail[0], tail[1:]
        if not alias: raise ValueError("chưa chọn scene; dùng `use <alias>` hoặc `run <alias>")
        entry = scene_entry(alias, self.registry)
        route_alias = entry["default_route"]
        dry = False
        while tail:
            item = tail.pop(0)
            if item == "--dry-run": dry = True
            elif item == "--route" and tail: route_alias = tail.pop(0)
            else: raise ValueError(f"tham số run không hợp lệ: {item}")
        if self.active_thread and self.active_thread.is_alive(): raise RuntimeError("launcher đã có run đang hoạt động")
        if self.active_object_eval_thread and self.active_object_eval_thread.is_alive(): raise RuntimeError("object evaluation đang chạy; chờ hoặc hủy trước khi chạy pipeline")
        if dry:
            self._run_pipeline(alias, route_alias, True)
            return 0
        self.cancel_requested.clear()
        if interactive:
            self.active_thread = threading.Thread(target=self._interactive_run, args=(alias, route_alias), daemon=False)
            self.active_thread.start()
            print(f"Đã khởi chạy {alias}/{route_alias} ở foreground-managed mode; /status và /logs dùng được ngay.")
            return 0
        state = self._run_pipeline(alias, route_alias)
        if state.get("status") == "completed": self._print_summary(state)
        return 0 if state.get("status") == "completed" else 1

    def _interactive_run(self, alias: str, route_alias: str) -> None:
        state = self._run_pipeline(alias, route_alias)
        if state.get("status") == "completed":
            self._print_summary(state)

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
        path, doc = self._find_run(args[0] if args else None)
        summary = doc.get("summary") or self._summarize(path)
        print(f"Run ID: {doc.get('run_id')} | Status: {doc.get('status')} | Scene: {doc.get('scene_id')} / route {doc.get('route_alias')}")
        print(f"Capture resolution: {summary.get('capture_resolution_px') or doc.get('capture_resolution_px') or 'chưa có'} px")
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
        comparison = path / "comparison_to_baseline/report.vi.md"
        if comparison.is_file(): print(f"Resolution comparison: {comparison}")
        print(f"Output directory: {path}")

    def _open_target(self, path: Path, kind: str) -> Path | None:
        choices = {
            "report": [path / "comparison_to_baseline/report.vi.md", path / "evaluation/report.vi.md", path / "semantic/report.vi.md"],
            "comparison": [path / "comparison_to_baseline/report.vi.md", path / "comparison_to_baseline/resolution_side_by_side.png"],
            "map": [path / "replay/spatial_map.png"],
            "objects": [path / "semantic/region_object_evidence.html", path / "detection_2d/overlays"],
            "labels": [path / "semantic/region_labels.json", path / "semantic/region_labels.html"],
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
        if choices is None: raise ValueError("artifact: report|comparison|map|objects|labels|object-evaluation|object-review|folder")
        return next((p for p in choices if p.exists()), None)

    def cmd_open(self, args: list[str]) -> int:
        run_id = None; kind = "report"
        for arg in args:
            if arg in {"report", "comparison", "map", "objects", "labels", "object-evaluation", "object-review", "folder"}: kind = arg
            elif run_id is None: run_id = arg
            else: raise ValueError("cú pháp: open [run-id] [report|comparison|map|objects|labels|folder]")
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
        if run_doc.get("status") != "completed": raise ValueError("chỉ đánh giá run predictor đã completed")
        if run_doc.get("dataset") != "hm3d": raise ValueError("HM3D semantic-object evaluator hiện chỉ hỗ trợ run HM3D")
        mapping = resolve_path("configs/evaluation/hm3d_00802_object_category_mapping_v1.json")
        script = RUNNER / "evaluate_hm3d_object_gt.py"
        if check_only:
            cmd = [self.env["project_python"], "-B", str(script), "--run-root", str(run_root), "--mapping", str(mapping), "--check"]
            if json_output: cmd.append("--json")
            result = subprocess.run(cmd, cwd=ROOT, env=self._stage_env(project=True), text=True, capture_output=True, check=False)
            if result.stdout: print(result.stdout.rstrip())
            if result.stderr: print(result.stderr.rstrip())
            return result.returncode
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
        trial = {"schema_version": 1, "scenes": self.registry["scenes"] + [entry]}
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
        print(f"Run ID: {doc.get('run_id')}\nScene / route: {doc.get('scene_id')} / {doc.get('route_alias')}\nStatus: {doc.get('status')}\n"
              f"Capture resolution: {s.get('capture_resolution_px') or doc.get('capture_resolution_px')} px\n"
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
                        (self.active_object_eval_thread and self.active_object_eval_thread.is_alive())):
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
                        (self.active_object_eval_thread and self.active_object_eval_thread.is_alive())):
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
