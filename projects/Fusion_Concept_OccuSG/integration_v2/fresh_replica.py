"""Config-driven C2 fresh Habitat scene+route workflow.

Reuses the current capture, perception, native room, ConceptGraphs mapping,
unified graph, and viewer code. This produces a batch final map; it is not the
live revision viewer and does not claim realtime processing.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
HYBRID_RUNS = ROOT / "artifacts/conceptgraphs_hybrid_runs"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, doc: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def _root_path(value: str) -> Path:
    path = Path(value).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def _interpreter_path(value: str) -> Path:
    """Keep virtualenv bin/python lexical path; resolving it can bypass site-packages."""
    path = Path(value).expanduser()
    return (ROOT / path).absolute() if not path.is_absolute() else path.absolute()


def _external_path(value: str, baseline_root: Path) -> Path:
    path = Path(value).expanduser()
    return (baseline_root / path).resolve() if not path.is_absolute() else path.resolve()


def _apply_capture_profile(route_doc: dict, profile: dict | None) -> dict:
    """Create an effective route without mutating its immutable source config."""
    effective = dict(route_doc)
    if profile is not None:
        effective["resolution_px"] = list(profile["resolution_px"])
        effective["calibration_id"] = str(profile["calibration_id"])
        effective["capture_profile"] = {key: profile[key] for key in
            ("schema", "profile_id", "profile_sha256", "calibration_id", "intrinsics_adapter")}
    return effective


def room_graph_projection_signature(graph) -> tuple[dict, tuple]:
    """Capture room nodes and their adjacency/containment edges for adapter parity."""
    rooms = {node.node_id: node for node in graph.nodes if node.kind == "room"}
    room_ids = set(rooms)
    edges = tuple(sorted(
        (edge.relation, edge.source_id, edge.target_id, bool(edge.directed), str(edge.provenance or ""))
        for edge in graph.edges
        if edge.source_id in room_ids and edge.relation in {"room_adjacency", "room_contains"}
    ))
    return rooms, edges


def validate_unavailable_membership(graph) -> int:
    """Validate the unavailable-scope contract, including a valid empty export.

    A reference filter may legitimately remove every hypothesis on a short
    route. Empty final object population is a pipeline result, not an
    integration failure; any object that is present must still carry the
    unavailable reason, and no ConceptGraphs membership edge may be emitted.
    """
    object_nodes = [node for node in graph.nodes if node.kind == "object"]
    if any(edge.relation == "room_contains"
           and edge.target_id.startswith("object:conceptgraphs:") for edge in graph.edges):
        raise AssertionError("unavailable-scope policy emitted a ConceptGraphs room_contains edge")
    if any(node.attributes.get("membership_status") != "unavailable"
           or node.attributes.get("membership_reason") != "unavailable_elevation_scope"
           for node in object_nodes):
        raise AssertionError("unified object graph lost unavailable_elevation_scope")
    return len(object_nodes)


def room_graph_integration_signature(graph) -> tuple[dict, tuple, tuple]:
    """Room geometry, adjacency, and non-object room relations kept by the adapter."""
    rooms = {node.node_id: node for node in graph.nodes if node.kind == "room"}
    room_ids = set(rooms)
    node_kinds = {node.node_id: node.kind for node in graph.nodes}
    kept_relations = tuple(sorted(
        (edge.relation, edge.source_id, edge.target_id, bool(edge.directed), str(edge.provenance or ""))
        for edge in graph.edges
        if edge.source_id in room_ids and edge.relation in {"room_adjacency", "room_contains"}
        and (edge.relation == "room_adjacency" or node_kinds.get(edge.target_id) != "object")
    ))
    return rooms, kept_relations, tuple(sorted(room_ids))


def excluded_native_object_memberships(source_graph, unified_graph) -> list[dict]:
    """List room edges intentionally omitted with non-imported source object nodes."""
    unified_ids = {node.node_id for node in unified_graph.nodes}
    source_nodes = {node.node_id: node for node in source_graph.nodes}
    result = []
    for edge in source_graph.edges:
        target = source_nodes.get(edge.target_id)
        if (edge.relation == "room_contains" and edge.source_id.startswith("room:")
                and target is not None and target.kind == "object" and edge.target_id not in unified_ids):
            result.append({"room_id": edge.source_id, "target_node_id": edge.target_id,
                           "target_provider": target.provenance,
                           "target_class": target.attributes.get("class_name"),
                           "edge_provenance": edge.provenance,
                           "status": "source_native_object_excluded_from_conceptgraphs_import"})
    return sorted(result, key=lambda row: (row["room_id"], row["target_node_id"]))


def _locked_file(resources: dict, name: str, *, executable: bool = False) -> Path:
    record = resources.get("files", {}).get(name)
    if not isinstance(record, dict):
        raise ValueError(f"external resource lock has no file entry {name!r}")
    path = Path(record["path"]).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"locked external input missing: {name}: {path}")
    if path.stat().st_size != int(record["bytes"]) or sha(path) != record["sha256"]:
        raise ValueError(f"locked external input does not match its bound hash: {name}")
    if executable and not os.access(path, os.X_OK):
        raise PermissionError(f"locked native resource is not executable: {path}")
    return path


def _mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _probe(interpreter: Path, code: str, *, timeout: int = 30) -> dict:
    proc = subprocess.run([str(interpreter), "-B", "-c", code], cwd=ROOT, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    return {"interpreter": str(interpreter), "returncode": proc.returncode, "output": proc.stdout[-4000:]}


def _process_tree_rss_bytes(root_pid: int, proc_root: Path = Path("/proc")) -> int | None:
    """Return sampled RSS bytes for a child process tree on Linux, or None."""
    if not proc_root.is_dir():
        return None
    parents: dict[int, int] = {}
    rss: dict[int, int] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text()
            tail = stat[stat.rfind(")") + 2:].split()
            ppid = int(tail[1])
            status = (entry / "status").read_text()
            vmrss = next((line for line in status.splitlines() if line.startswith("VmRSS:")), None)
            if vmrss:
                rss[pid] = int(vmrss.split()[1]) * 1024
            parents[pid] = ppid
        except (OSError, ValueError, IndexError):
            continue
    if root_pid not in parents and root_pid not in rss:
        return None
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in selected and pid not in selected:
                selected.add(pid); changed = True
    return sum(rss.get(pid, 0) for pid in selected) if selected.intersection(rss) else None


def _start_run(run_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,96}", run_id):
        raise ValueError("run-id may contain letters, digits, underscore and hyphen only")
    HYBRID_RUNS.mkdir(parents=True, exist_ok=True)
    output = (HYBRID_RUNS / run_id).resolve()
    if not output.is_relative_to(HYBRID_RUNS.resolve()):
        raise ValueError("C2 output must stay under artifacts/conceptgraphs_hybrid_runs")
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    return output, output / "c2_run_manifest.json"


def make_c2_run_id() -> str:
    return f"c2_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:6]}"


def _record_stage(doc: dict, manifest_path: Path) -> None:
    _write(manifest_path, doc)


def _run_command(doc: dict, manifest_path: Path, run_root: Path, stage: str, command: list[str],
                 *, env: dict[str, str], required: list[Path], timeout_s: int,
                 deadline_monotonic: float | None = None) -> dict:
    start = time.monotonic()
    log_path = run_root / "logs" / f"{stage}.log"
    record = {"status": "running", "command": list(command), "log": str(log_path.relative_to(run_root)),
              "started_utc": datetime.now(timezone.utc).isoformat(), "timeout_s": timeout_s,
              "overall_deadline_enforced": deadline_monotonic is not None}
    doc["current_stage"] = stage
    doc["stages"][stage] = record
    _record_stage(doc, manifest_path)
    print(f"[C2 {stage}] started; timeout={timeout_s}s", flush=True)
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True)
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    ended = False
    output_tail: list[str] = []
    timed_out = False
    overall_deadline_exceeded = False
    peak_process_tree_rss_bytes: int | None = None
    rss_sample_count = 0
    last_rss_sample_s = -1.0
    with log_path.open("w", encoding="utf-8") as log:
        while not ended or proc.poll() is None:
            elapsed = time.monotonic() - start
            if elapsed - last_rss_sample_s >= 0.25:
                current_rss = _process_tree_rss_bytes(proc.pid)
                last_rss_sample_s = elapsed
                if current_rss is not None:
                    rss_sample_count += 1
                    peak_process_tree_rss_bytes = max(peak_process_tree_rss_bytes or 0, current_rss)
            deadline_hit = deadline_monotonic is not None and time.monotonic() >= deadline_monotonic
            if (elapsed >= timeout_s or deadline_hit) and proc.poll() is None:
                timed_out = True
                overall_deadline_exceeded = bool(deadline_hit)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=8)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                line = "__C2_QUEUE_EMPTY__"
            if line is None:
                ended = True
            elif line != "__C2_QUEUE_EMPTY__":
                log.write(line); log.flush()
                output_tail.append(line)
                output_tail = output_tail[-100:]
                if line.startswith("PROGRESS:") or len(output_tail) == 1 or int(elapsed) % 30 == 0:
                    print(f"[C2 {stage} {elapsed:.1f}s] {line.rstrip()}", flush=True)
            if proc.poll() is not None and ended:
                break
    reader.join(timeout=1)
    code = proc.returncode
    missing = [str(path) for path in required if not Path(path).is_file()]
    status = "failed" if code != 0 or timed_out or missing else "completed"
    record.update({"status": status, "exit_code": code, "timed_out": timed_out,
                   "overall_deadline_exceeded": overall_deadline_exceeded,
                   "elapsed_s": round(time.monotonic() - start, 4),
                   "peak_process_tree_rss_bytes": peak_process_tree_rss_bytes,
                   "rss_sample_count": rss_sample_count,
                   "rss_measurement": "sampled /proc process-tree VmRSS; per-stage process tree, not host-wide peak; 250ms loop cadence",
                   "finished_utc": datetime.now(timezone.utc).isoformat(),
                   "required_files_missing": missing})
    doc["current_stage"] = None if status == "completed" else stage
    _record_stage(doc, manifest_path)
    if status != "completed":
        tail = "".join(output_tail)[-4000:]
        reason = f"{stage} failed (exit={code}, timed_out={timed_out}, missing={missing}); log={log_path}\n{tail}"
        raise RuntimeError(reason)
    print(f"[C2 {stage}] completed in {record['elapsed_s']:.2f}s", flush=True)
    return record


def _config_contract(config: dict) -> None:
    required_top = {"schema", "dataset", "route_config", "perception", "models", "interpreters", "mapping", "resource_policy"}
    allowed_top = required_top | {"capture_profile"}
    if not required_top <= set(config) or set(config) - allowed_top or config.get("schema") != "scene-graph-c2-fresh-replica-route/v1":
        raise ValueError("C2 config must match scene-graph-c2-fresh-replica-route/v1")
    profile = config.get("capture_profile")
    if profile is not None:
        if (profile.get("schema") != "habitat-capture-profile/v1" or not profile.get("profile_id")
                or not profile.get("profile_path") or not re.fullmatch(r"[0-9a-f]{64}", str(profile.get("profile_sha256", "")))
                or profile.get("intrinsics_adapter") != "adapters.habitat.frames.habitat_camera_intrinsics"):
            raise ValueError("C2 capture_profile must be a resolved, hash-bound Habitat profile")
        resolution = profile.get("resolution_px")
        if (not isinstance(resolution, list) or len(resolution) != 2
                or any(type(v) is not int or v < 2 for v in resolution)
                or not profile.get("calibration_id")):
            raise ValueError("C2 capture_profile resolution/calibration is invalid")
    dataset = config["dataset"]
    for key in ("type", "version", "scene_id", "sequence_id", "scene_dataset_config", "navmesh_file"):
        if not dataset.get(key):
            raise ValueError(f"C2 config dataset.{key} is required")
    if dataset["type"] == "replica_cad":
        if not dataset.get("scene_instance"):
            raise ValueError("C2 ReplicaCAD config requires dataset.scene_instance")
    elif dataset["type"] == "hm3d":
        if not dataset.get("scene_asset"):
            raise ValueError("C2 HM3D config requires dataset.scene_asset")
    else:
        raise ValueError(f"C2 dataset adapter is unsupported: {dataset['type']!r}")
    mapping = config["mapping"]
    if profile is not None:
        width, height = profile["resolution_px"]
        expected_policy = f"native Habitat sensor capture at {width}x{height}"
        policy = str(mapping.get("resolution_policy", ""))
        if expected_policy not in policy or "ignore route source resolution" not in policy:
            raise ValueError("C2 mapping.resolution_policy contradicts the resolved native capture profile")
    if mapping.get("membership_policy") != "unavailable_elevation_scope":
        raise ValueError("C2 must keep membership unavailable until spatial scope is verified")
    if mapping.get("room_semantics_status") != "unavailable_membership_scope":
        raise ValueError("C2 room semantics must remain unavailable while membership scope is unavailable")
    if int(mapping.get("seed", 0)) < 0:
        raise ValueError("mapping.seed must be a non-negative integer")
    if config["resource_policy"].get("download_models") is not False or config["resource_policy"].get("install_dependencies") is not False:
        raise ValueError("C2 local execution must not download models or install dependencies")


def _budget_remaining(deadline_monotonic: float | None) -> float | None:
    if deadline_monotonic is None:
        return None
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("overall C2 execution budget expired")
    return remaining


def _prepare(config_path: Path, config: dict, run_root: Path, *, deadline_monotonic: float | None = None) -> dict:
    resource_path = ROOT / "locks/external_resources.json"
    resources = _read(resource_path)
    if resources.get("status") != "bound_input_only" or Path(resources.get("workspace_root", "")).resolve() != ROOT:
        raise ValueError("external resource lock is not bound input-only to this workspace")
    env_config_path = ROOT / "configs/launcher/local_environment.json"
    local_env = _read(env_config_path)
    baseline_root = Path(local_env["baseline_read_root"]).resolve()
    dataset = config["dataset"]
    scene_key = "scene_instance" if dataset["type"] == "replica_cad" else "scene_asset"
    scene = _external_path(dataset[scene_key], baseline_root)
    scene_config = _external_path(dataset["scene_dataset_config"], baseline_root)
    navmesh = _external_path(dataset["navmesh_file"], baseline_root)
    for label, path in (("scene instance", scene), ("scene dataset config", scene_config), ("navmesh", navmesh)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} missing: {path}")

    route_path = _root_path(config["route_config"])
    route = _read(route_path)
    if str(route.get("scene_id")) != str(dataset["scene_id"]) or str(route.get("sequence_id")) != str(dataset["sequence_id"]):
        raise ValueError("route scene_id/sequence_id does not match C2 dataset config")
    if route.get("dataset_type") and route["dataset_type"] != dataset["type"]:
        raise ValueError("route dataset_type does not match C2 dataset config")
    capture_profile = config.get("capture_profile")
    if capture_profile is not None:
        profile_path = _root_path(capture_profile["profile_path"])
        if not profile_path.is_file() or sha(profile_path) != capture_profile["profile_sha256"]:
            raise ValueError("capture profile file hash changed after scene resolution")
        profile_doc = _read(profile_path)
        if profile_doc.get("profile_id") != capture_profile["profile_id"]:
            raise ValueError("capture profile identity changed after scene resolution")
    effective = _apply_capture_profile(route, capture_profile)
    effective.update({"dataset_type": dataset["type"], "dataset_version": dataset["version"],
                      "scene_id": dataset["scene_id"], "sequence_id": dataset["sequence_id"],
                      "scene_provenance": dataset.get("provenance"), "scene_license_note": dataset.get("license_note"),
                      "frame_id_prefix": route.get("frame_id_prefix") or (
                          f"replica-cad-{dataset['scene_id']}" if dataset["type"] == "replica_cad"
                          else f"hm3d-{dataset['scene_id'].split('-', 1)[0]}-{route.get('sequence_id', 'route')}"),
                      "scene_dataset_config": str(scene_config),
                      "navmesh_file": str(navmesh)})
    effective_route = run_root / "effective_route_config.json"
    _write(effective_route, effective)
    scene_profile = _root_path(config["perception"]["detector_profile"])
    p3_profile = _root_path(config["perception"]["p3_profile"])
    names = _root_path(config["perception"]["names"])
    pipeline = _root_path(config["perception"]["pipeline_config"])
    feature_checkpoint = _root_path(config["models"]["openclip_checkpoint"])
    checkpoint_meta_path = _root_path(config["models"]["openclip_checkpoint_metadata"])
    feature_report = _root_path(config["models"]["image_feature_provenance_report"])
    for label, path in (("route", route_path), ("detector profile", scene_profile), ("P3 profile", p3_profile),
                        ("COCO names", names), ("native pipeline config", pipeline),
                        ("OpenCLIP checkpoint", feature_checkpoint), ("checkpoint metadata", checkpoint_meta_path),
                        ("OpenCLIP image feature report", feature_report), ("external resource lock", resource_path),
                        ("local environment config", env_config_path)):
        if not path.is_file():
            raise FileNotFoundError(f"{label} missing: {path}")
    detector = _read(scene_profile)
    if detector.get("model", {}).get("variant") != "yolo11n-seg":
        raise ValueError("C2 requires the already selected YOLO11n-seg detector profile")
    yolo = _locked_file(resources, str(config["models"]["yolo_weights_external_resource"]))
    expected_yolo = detector["model"]["weights_sha256"]
    if sha(yolo) != expected_yolo:
        raise ValueError("YOLO11n-seg locked weights hash differs from detector profile")
    checkpoint_meta = _read(checkpoint_meta_path)
    if sha(feature_checkpoint) != checkpoint_meta.get("sha256"):
        raise ValueError("local OpenCLIP ViT-B/32 checkpoint hash differs from pinned metadata")
    feature_meta = _read(feature_report)
    if feature_meta.get("checkpoint_sha256") != checkpoint_meta.get("sha256"):
        raise ValueError("existing image-feature provenance report names a different checkpoint")
    if _read(p3_profile).get("geometry", {}).get("depth_units") != "m":
        raise ValueError("P3 profile is not locked to metric depth")

    hab = _interpreter_path(config["interpreters"]["habitat"])
    perception = _interpreter_path(local_env["project_python"])
    features = _interpreter_path(config["interpreters"]["features"])
    mapping = _interpreter_path(config["interpreters"]["mapping"])
    for name, path in (("Habitat", hab), ("perception", perception), ("features", features), ("mapping", mapping)):
        if not path.is_file():
            raise FileNotFoundError(f"{name} interpreter missing: {path}")

    binaries = {name: _locked_file(resources, name, executable=True) for name in ("native_m2_m3", "native_dude")}
    available_ram = _mem_available_bytes()
    disk_free = shutil.disk_usage(run_root).free
    min_ram = int(config["resource_policy"].get("minimum_available_ram_bytes", 0))
    min_disk = int(config["resource_policy"].get("minimum_free_disk_bytes", 0))
    if available_ram is not None and available_ram < min_ram:
        raise RuntimeError(f"preflight memory gate: available RAM {available_ram} < configured minimum {min_ram}")
    if disk_free < min_disk:
        raise RuntimeError(f"preflight disk gate: free disk {disk_free} < configured minimum {min_disk}")

    probe_codes = {
        "habitat": "import habitat_sim,magnum,quaternion; print({'habitat_sim':getattr(habitat_sim,'__version__','unknown')})",
        "perception": "import numpy,PIL,yaml,onnxruntime,matplotlib; print({'numpy':numpy.__version__,'onnxruntime':onnxruntime.__version__})",
        "features": "import torch,open_clip,safetensors; assert torch.version.cuda is None; assert open_clip.__version__=='3.2.0'; print({'torch':torch.__version__,'open_clip':open_clip.__version__})",
        "p3": "import numpy,scipy,PIL; from scipy.spatial import cKDTree; print({'numpy':numpy.__version__,'scipy':scipy.__version__,'p3_cluster':'cKDTree'})",
        "mapping": "import torch,open3d,faiss,scipy; from pytorch3d.ops import box3d_overlap; assert torch.version.cuda is None; assert torch.__version__.split('+')[0]=='2.0.1'; assert open3d.__version__=='0.17.0'; assert faiss.__version__=='1.7.4'; print({'torch':torch.__version__,'pytorch3d':'import_ok','open3d':open3d.__version__,'faiss':faiss.__version__,'scipy':scipy.__version__})",
    }
    probes = {}
    p3_python = _interpreter_path(config["interpreters"].get("p3", config["interpreters"]["mapping"]))
    if not p3_python.is_file():
        raise FileNotFoundError(f"P3 interpreter missing: {p3_python}")
    for name, interpreter in (("habitat", hab), ("perception", perception), ("features", features),
                              ("p3", p3_python), ("mapping", mapping)):
        remaining = _budget_remaining(deadline_monotonic)
        timeout = 30 if remaining is None else max(1, min(30, int(remaining)))
        result = _probe(interpreter, probe_codes[name], timeout=timeout)
        probes[name] = result
        if result["returncode"] != 0:
            raise RuntimeError(f"{name} dependency probe failed: {result['output']}")

    route_report = run_root / "preflight/route_validation.json"
    route_report.parent.mkdir(parents=True, exist_ok=True)
    (run_root / "logs").mkdir(parents=True, exist_ok=True)
    validator = ROOT / "scripts/validate_habitat_route.py"
    remaining = _budget_remaining(deadline_monotonic)
    route_timeout = 180 if remaining is None else max(1, min(180, int(remaining)))
    route_proc = subprocess.run([str(hab), "-B", str(validator), "--scene", str(scene),
                                 "--config", str(effective_route), "--output", str(route_report)],
                                cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=route_timeout, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    (run_root / "logs/preflight_route.log").write_text(route_proc.stdout, encoding="utf-8")
    if route_proc.returncode != 0 or not route_report.is_file() or _read(route_report).get("status") != "pass":
        raise RuntimeError(f"selected route failed Habitat validation: {route_proc.stdout[-3000:]}")

    references = resources.get("references", {})
    cg = references.get("ConceptGraphs", {})
    if cg.get("commit") != config["mapping"].get("reference_commit"):
        raise ValueError("C2 config ConceptGraphs reference commit differs from the locked reference")
    cg_path = _root_path(str(cg.get("path", "")))
    remaining = _budget_remaining(deadline_monotonic)
    git_timeout = 15 if remaining is None else max(1, min(15, int(remaining)))
    actual_cg = subprocess.run(["git", "-C", str(cg_path), "rev-parse", "HEAD"], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=git_timeout)
    if actual_cg.returncode != 0 or actual_cg.stdout.strip() != cg.get("commit"):
        raise ValueError(f"ConceptGraphs checkout commit mismatch: {actual_cg.stdout.strip()} {actual_cg.stderr.strip()}")

    input_paths = {
        "c2_config": config_path, "route_config": route_path, "effective_route_config": effective_route,
        scene_key: scene, "scene_dataset_config": scene_config, "navmesh": navmesh,
        "detector_profile": scene_profile, "detector_weights": yolo, "class_names": names,
        "p3_profile": p3_profile, "native_pipeline_config": pipeline,
        "checkpoint": feature_checkpoint, "checkpoint_metadata": checkpoint_meta_path,
        "image_feature_provenance_report": feature_report, "resource_lock": resource_path,
        "local_environment": env_config_path, "native_m2_m3": binaries["native_m2_m3"],
        "native_dude": binaries["native_dude"],
    }
    if capture_profile is not None:
        input_paths["capture_profile"] = _root_path(capture_profile["profile_path"])
    for rel in ("adapters/habitat/calibration.py", "adapters/habitat/frames.py",
                "scripts/capture_habitat_replica.py", "scripts/run_replica_cad_instance_segmentation.py",
                "scripts/project_detection_masks_to_3d.py", "scripts/run_habitat_native_replay.py",
                "scripts/build_dense_hybrid_image_features.py", "scripts/build_dense_hybrid_text_features.py",
                "scripts/run_conceptgraphs_cached_hybrid.py", "integration_v2/unified_scene_graph.py",
                "integration_v2/hybrid_mapping_viewer.py", "integration_v2/fresh_replica.py",
                "integration_v2/capture_compatibility.py",
                "integration_v2/scene_registry.py", "scripts/scene_launcher.py", "configs/launcher/scenes.json"):
        input_paths[f"code:{rel}"] = ROOT / rel
    if not all(p.is_file() for p in input_paths.values()):
        missing = [str(p) for p in input_paths.values() if not p.is_file()]
        raise FileNotFoundError(f"C2 preflight source inputs missing: {missing}")
    return {
        "paths": {name: str(path) for name, path in input_paths.items()},
        "sha256": {name: sha(path) for name, path in input_paths.items()},
        "external_resources_lock_sha256": sha(resource_path),
        "conceptgraphs_reference_commit": cg.get("commit"),
        "occusg_reference_commit": references.get("OccuSG", {}).get("commit"),
        "mapconversion_commit": resources.get("mapconversion", {}).get("actual_commit"),
        "route_validation": _read(route_report),
        "environment_probes": probes,
        "resources": {"host_ram_available_bytes": available_ram, "free_disk_bytes": disk_free,
                      "gpu_used": False, "models_downloaded": False, "dependencies_installed": False},
        "resolved": {"scene": str(scene), "scene_dataset_config": str(scene_config), "navmesh": str(navmesh),
                     "detector_weights": str(yolo), "habitat_python": str(hab), "perception_python": str(perception),
                     "p3_python": str(p3_python), "feature_python": str(features), "mapping_python": str(mapping),
                     "native_m2_m3": str(binaries["native_m2_m3"]), "native_dude": str(binaries["native_dude"]),
                     "effective_route_config": str(effective_route)},
    }


def _stage_env(run_root: Path, extra: dict | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "src"),
        env.get("PYTHONPATH", ""))).rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    mpl = run_root / "cache/matplotlib"
    mpl.mkdir(parents=True, exist_ok=True)
    env["MPLCONFIGDIR"] = str(mpl)
    if extra:
        env.update(extra)
    return env


def _verified_reuse_source(run_id: str, current_preflight: dict, expected_scene: str, expected_sequence: str) -> Path:
    if Path(run_id).name != run_id or not re.fullmatch(r"[A-Za-z0-9_-]{4,96}", run_id):
        raise ValueError("reuse-from run ID is invalid")
    source = (HYBRID_RUNS / run_id).resolve()
    if not source.is_relative_to(HYBRID_RUNS.resolve()):
        raise ValueError("reuse-from must refer to a run under artifacts/conceptgraphs_hybrid_runs")
    source_manifest_path = source / "c2_run_manifest.json"
    if not source_manifest_path.is_file():
        raise FileNotFoundError(f"reuse-from C2 manifest missing: {source_manifest_path}")
    previous = _read(source_manifest_path)
    if previous.get("status") != "failed":
        raise ValueError("C2 reuse source must be an incomplete/failed run, not a completed run")
    if (previous.get("scene_id"), previous.get("sequence_id")) != (expected_scene, expected_sequence):
        raise ValueError("reuse-from scene/sequence mismatch")
    for stage in ("capture", "perception_p2"):
        if previous.get("stages", {}).get(stage, {}).get("status") != "completed":
            raise ValueError(f"reuse-from stage {stage} is not completed")
    scene_hash_key = "scene_instance" if previous.get("dataset_type") == "replica_cad" else "scene_asset"
    stable_keys = {
        "c2_config", "route_config", "effective_route_config", "capture_profile", scene_hash_key, "scene_dataset_config",
        "navmesh", "detector_profile", "detector_weights", "class_names", "p3_profile",
        "native_pipeline_config", "checkpoint", "checkpoint_metadata", "image_feature_provenance_report",
        "resource_lock", "local_environment", "native_m2_m3", "native_dude",
    }
    previous_hashes = previous.get("input_sha256", {})
    current_hashes = current_preflight.get("sha256", {})
    differences = [key for key in stable_keys if previous_hashes.get(key) != current_hashes.get(key)]
    if differences:
        raise ValueError(f"reuse-from inputs changed since prior capture/P2: {differences}")
    capture_root, p2_root = source / "capture", source / "detection_2d"
    capture_manifest_path = capture_root / "capture_manifest.json"
    p2_index_path = p2_root / "detection_2d_index.json"
    if not capture_manifest_path.is_file() or not p2_index_path.is_file():
        raise FileNotFoundError("reuse-from capture manifest or P2 index is missing")
    cap = _read(capture_manifest_path)
    p2 = _read(p2_index_path)
    if (cap.get("status"), cap.get("scene_id"), cap.get("sequence_id")) != ("pass", expected_scene, expected_sequence):
        raise ValueError("reuse-from capture manifest does not match its declared scene/sequence")
    if p2.get("status") != "pass" or p2.get("capture_manifest_sha256") != sha(capture_manifest_path):
        raise ValueError("reuse-from P2 index is not hash-bound to the capture manifest")
    from integration_v2.capture_compatibility import canonical_sha256
    if not cap.get("capture_sensor_contract_sha256") or not p2.get("cache_compatibility_sha256"):
        raise ValueError("reuse-from capture/P2 predates sensor/preprocessing compatibility fingerprints; recapture and rerun P2")
    if canonical_sha256(p2.get("cache_compatibility", {})) != p2.get("cache_compatibility_sha256"):
        raise ValueError("reuse-from P2 compatibility fingerprint is corrupt")
    cache_contract = p2["cache_compatibility"]
    if (cache_contract.get("capture_manifest_sha256") != sha(capture_manifest_path)
            or cache_contract.get("capture_sensor_contract_sha256") != cap.get("capture_sensor_contract_sha256")
            or cache_contract.get("resolution_px") != cap.get("resolution_px")
            or cache_contract.get("detector_config_sha256") != current_hashes.get("detector_profile")
            or cache_contract.get("weights_sha256") != current_hashes.get("detector_weights")):
        raise ValueError("reuse-from P2 capture/calibration/detector contract differs from current run")
    for cap_frame in cap.get("frames", []):
        frame_dir = capture_root / str(cap_frame["frame_dir"])
        for name, expected_hash in cap_frame.get("raw_files_sha256", {}).items():
            raw_path = frame_dir / str(name)
            if not raw_path.is_file() or sha(raw_path) != expected_hash:
                raise ValueError(f"reuse-from capture file missing/hash mismatch: {cap_frame.get('frame_id')}:{name}")
    checked_masks = 0
    capture_frame_by_id = {str(row["frame_id"]): row for row in cap.get("frames", [])}
    for frame in p2.get("frames", []):
        cap_frame = capture_frame_by_id.get(str(frame.get("frame_id")))
        if cap_frame is None:
            raise ValueError(f"reuse-from P2 frame is absent from capture: {frame.get('frame_id')}")
        rgb_path = capture_root / str(cap_frame["frame_dir"]) / "rgb.npy"
        if (not rgb_path.is_file() or sha(rgb_path) != frame.get("rgb_input_sha256")
                or cache_contract.get("rgb_input_sha256_by_frame", {}).get(str(frame.get("frame_id"))) != frame.get("rgb_input_sha256")):
            raise ValueError(f"reuse-from P2 source RGB missing/hash mismatch: {frame.get('frame_id')}")
        for det in frame.get("detections", []):
            mask = p2_root / str(det["mask_file"])
            if not mask.is_file() or sha(mask) != det.get("mask_sha256"):
                raise ValueError(f"reuse-from P2 mask missing/hash mismatch: {det.get('detection_id')}")
            checked_masks += 1
    if int(p2.get("observation_count", checked_masks)) != checked_masks:
        raise ValueError("reuse-from P2 observation count differs from verified masks")
    return source


def run_fresh_replica(config_path: Path, *, run_id: str, command: list[str] | None = None,
                      reuse_from: str | None = None, before_stages=None,
                      overall_timeout_s: int | None = None) -> dict:
    started_monotonic = time.monotonic()
    if overall_timeout_s is not None and overall_timeout_s <= 0:
        raise ValueError("overall_timeout_s must be positive")
    deadline_monotonic = None if overall_timeout_s is None else started_monotonic + overall_timeout_s
    config_path = Path(config_path).resolve()
    config = _read(config_path)
    _config_contract(config)
    run_root, manifest_path = _start_run(run_id)
    dataset = config["dataset"]
    doc = {
        "schema": "scene-graph-c2-fresh-replica-run/v1", "run_id": run_id, "status": "started",
        "execution_mode": "fresh_scene_route_batch", "command": list(command or []),
        "started_utc": datetime.now(timezone.utc).isoformat(), "finished_utc": None,
        "execution_budget_s": overall_timeout_s,
        "overall_deadline_enforced": overall_timeout_s is not None,
        "scene_id": dataset["scene_id"], "sequence_id": dataset["sequence_id"],
        "dataset_type": dataset["type"], "route_config": config["route_config"],
        "capture_profile": config.get("capture_profile"),
        "source_display_label": "Hybrid YOLO11n-seg + ViT-B-32 / ConceptGraphs reference mapping core",
        "claim_boundary": "fresh local fixed-pose route batch; not full ConceptGraphs-Detect, not live processing, not realtime, no quality claim",
        "causality": {"objects": "prefix object timeline generated by ConceptGraphs mapping core",
                      "rooms": "final native OccuSG snapshot from the same capture sequence",
                      "membership": "unavailable_elevation_scope; no room_contains edges emitted",
                      "room_semantics": "unavailable_membership_scope; no room label inferred"},
        "scope_status": {"status": "unavailable", "reason": "room projection spatial elevation scope is not verified",
                         "membership_policy": config["mapping"]["membership_policy"]},
        "stages": {}, "current_stage": "preflight", "outputs": {},
    }
    _record_stage(doc, manifest_path)
    try:
        doc["stages"]["preflight"] = {"status": "running", "started_utc": datetime.now(timezone.utc).isoformat()}
        _record_stage(doc, manifest_path)
        start = time.monotonic()
        preflight = _prepare(config_path, config, run_root, deadline_monotonic=deadline_monotonic)
        doc["preflight"] = preflight
        doc["input_sha256"] = preflight["sha256"]
        doc["stages"]["preflight"] = {"status": "completed", "elapsed_s": round(time.monotonic()-start, 4),
                                      "finished_utc": datetime.now(timezone.utc).isoformat(),
                                      "route_pose_count": preflight["route_validation"].get("pose_count"),
                                      "navigable_pose_count": preflight["route_validation"].get("navigable_pose_count")}
        doc["current_stage"] = None
        _record_stage(doc, manifest_path)

        if before_stages is not None and not before_stages(preflight, doc):
            doc["status"] = "cancelled"
            doc["error"] = "Run was not confirmed after successful preflight; no producer stage started."
            doc["finished_utc"] = datetime.now(timezone.utc).isoformat()
            doc["current_stage"] = None
            _record_stage(doc, manifest_path)
            return doc

        env = _stage_env(run_root)

        def run_stage(stage: str, command: list[str], *, required: list[Path], timeout_s: int,
                      env_override: dict[str, str] | None = None) -> dict:
            try:
                remaining = _budget_remaining(deadline_monotonic)
            except TimeoutError as exc:
                doc["current_stage"] = stage
                doc["stages"][stage] = {"status": "failed", "error": str(exc),
                                        "finished_utc": datetime.now(timezone.utc).isoformat()}
                _record_stage(doc, manifest_path)
                raise
            bounded_timeout = timeout_s if remaining is None else max(1, min(timeout_s, int(remaining)))
            return _run_command(doc, manifest_path, run_root, stage, command,
                                env=env_override or env, required=required, timeout_s=bounded_timeout,
                                deadline_monotonic=deadline_monotonic)

        route = _root_path(config["route_config"])
        scene = Path(preflight["resolved"]["scene"])
        hab = Path(preflight["resolved"]["habitat_python"])
        perception = Path(preflight["resolved"]["perception_python"])
        p3_python = Path(preflight["resolved"]["p3_python"])
        feature_python = _interpreter_path(config["interpreters"]["features"])
        mapping_python = _interpreter_path(config["interpreters"]["mapping"])
        weights = Path(preflight["resolved"]["detector_weights"])
        dataset_config = Path(preflight["resolved"]["scene_dataset_config"])
        navmesh = Path(preflight["resolved"]["navmesh"])
        resource_manifest = ROOT / "locks/external_resources.json"
        p2_dir = run_root / "detection_2d"; p3_dir = run_root / "detection_3d"
        replay_dir = run_root / "replay"; grid_dir = run_root / "grid_trace"
        capture_dir = run_root / "capture"

        reused_stages = []
        if reuse_from:
            reuse_root = _verified_reuse_source(reuse_from, preflight, dataset["scene_id"], dataset["sequence_id"])
            shutil.copytree(reuse_root / "capture", capture_dir)
            shutil.copytree(reuse_root / "detection_2d", p2_dir)
            for stage, files in (("capture", [capture_dir / "capture_manifest.json"]),
                                 ("perception_p2", [p2_dir / "detection_2d_index.json"])):
                doc["stages"][stage] = {"status": "reused", "source_run_id": reuse_from,
                    "verified_files": {str(p.relative_to(run_root)): sha(p) for p in files},
                    "finished_utc": datetime.now(timezone.utc).isoformat()}
                reused_stages.append(stage)
            _record_stage(doc, manifest_path)
            print(f"[C2] reused verified capture/P2 from {reuse_from}; masks hash-checked", flush=True)
        else:
            run_stage("capture",
                [str(hab), "-B", str(ROOT / "scripts/capture_habitat_replica.py"), "--scene", str(scene),
                 "--config", str(run_root / "effective_route_config.json"), "--output", str(capture_dir)],
                required=[capture_dir / "capture_manifest.json"], timeout_s=900)
        cap = _read(capture_dir / "capture_manifest.json")
        if cap.get("status") != "pass" or cap.get("scene_id") != dataset["scene_id"] or cap.get("sequence_id") != dataset["sequence_id"]:
            raise ValueError("capture manifest scene/sequence/status does not match C2 config")

        doc["current_stage"] = "capture_validation"
        doc["stages"]["capture_validation"] = {"status": "running", "started_utc": datetime.now(timezone.utc).isoformat()}
        _record_stage(doc, manifest_path)
        from integration_v2.capture_compatibility import validate_effective_capture
        capture_verification = validate_effective_capture(capture_dir, _read(run_root / "effective_route_config.json"),
            expected_scene_id=dataset["scene_id"], expected_sequence_id=dataset["sequence_id"],
            expected_effective_config_sha256=sha(run_root / "effective_route_config.json"))
        doc["stages"]["capture_validation"] = {**capture_verification, "finished_utc": datetime.now(timezone.utc).isoformat()}
        doc["current_stage"] = None
        _record_stage(doc, manifest_path)

        if not reuse_from:
            run_stage("perception_p2",
                [str(perception), "-B", str(ROOT / "scripts/run_replica_cad_instance_segmentation.py"),
                 "--capture", str(capture_dir), "--output", str(p2_dir),
                 "--config", str(_root_path(config["perception"]["detector_profile"])), "--weights", str(weights),
                 "--names", str(_root_path(config["perception"]["names"])), "--resource-manifest", str(resource_manifest)],
                required=[p2_dir / "detection_2d_index.json"], timeout_s=1800)
        p2 = _read(p2_dir / "detection_2d_index.json")
        if p2.get("status") != "pass" or int(p2.get("frame_count", -1)) != len(cap.get("frames", [])):
            raise ValueError("P2 frame/status contract does not match the capture")

        run_stage("projection_p3",
            [str(p3_python), "-B", str(ROOT / "scripts/project_detection_masks_to_3d.py"),
             "--capture", str(capture_dir), "--detection-2d", str(p2_dir), "--output", str(p3_dir),
             "--profile", str(_root_path(config["perception"]["p3_profile"]))],
            required=[p3_dir / "detection_3d_manifest.json", p3_dir / "detection_3d.jsonl",
                      p3_dir / "object_evidence_ledger.jsonl"], timeout_s=1800)
        p3 = _read(p3_dir / "detection_3d_manifest.json")
        if p3.get("status") != "pass" or int(p3.get("frame_count", -1)) != len(cap.get("frames", [])):
            raise ValueError("P3 frame/status contract does not match the capture")

        native_env = dict(env)
        native_env["MPLCONFIGDIR"] = str(run_root / "cache/matplotlib")
        run_stage("occusg_rooms",
            [str(perception), "-B", str(ROOT / "scripts/run_habitat_native_replay.py"),
             "--capture", str(capture_dir), "--detection-3d", str(p3_dir / "detection_3d.jsonl"),
             "--detection-3d-manifest", str(p3_dir / "detection_3d_manifest.json"),
             "--resource-manifest", str(resource_manifest),
             "--pipeline-config", str(_root_path(config["perception"]["pipeline_config"])),
             "--m2-m3-binary", preflight["resolved"]["native_m2_m3"],
             "--dude-binary", preflight["resolved"]["native_dude"],
             "--object-backend", "source", "--output", str(replay_dir), "--grid-output", str(grid_dir)],
            required=[replay_dir / "manifest.json", replay_dir / "snapshot.json",
                      replay_dir / "delta_log.json", grid_dir / "index.json"], timeout_s=2400,
            env_override=native_env)
        replay_manifest = _read(replay_dir / "manifest.json")
        if replay_manifest.get("pipeline_status") != "pass" or int(replay_manifest.get("frames", -1)) != len(cap.get("frames", [])):
            raise ValueError("OccuSG native replay did not produce a passing room snapshot for every capture frame")

        checkpoint = _root_path(config["models"]["openclip_checkpoint"])
        feature_report = _root_path(config["models"]["image_feature_provenance_report"])
        empty_manifest = run_root / "empty_existing_feature_manifest.jsonl"
        empty_manifest.write_text("", encoding="utf-8")
        image_root = run_root / "image_features"
        run_stage("image_features",
            [str(feature_python), "-B", str(ROOT / "scripts/build_dense_hybrid_image_features.py"),
             "--cache", str(run_root), "--existing-manifest", str(empty_manifest),
             "--checkpoint", str(checkpoint), "--output", str(image_root),
             "--checkpoint-report", str(feature_report)],
            required=[image_root / "feature_manifest.jsonl", image_root / "feature_extraction_report.json"], timeout_s=2400)
        image_report = _read(image_root / "feature_extraction_report.json")
        expected_observation_count = sum(len(fr.get("detections", [])) for fr in p2["frames"])
        if (image_report.get("status") != "pass"
                or int(image_report.get("observation_count", -1)) != expected_observation_count
                or image_report.get("checkpoint_sha256") != sha(checkpoint)):
            raise ValueError("image feature report is not complete for the P2 population/checkpoint")
        text_root = run_root / "text_features"
        run_stage("text_features",
            [str(feature_python), "-B", str(ROOT / "scripts/build_dense_hybrid_text_features.py"),
             "--cache", str(run_root), "--checkpoint", str(checkpoint),
             "--image-manifest", str(image_root / "feature_manifest.jsonl"), "--output", str(text_root)],
            required=[text_root / "manifest.json", text_root / "text_features.npz",
                      text_root / "text_feature_manifest.jsonl"], timeout_s=1800)
        image_rows = [json.loads(line) for line in (image_root / "feature_manifest.jsonl").read_text().splitlines() if line.strip()]
        text_rows = [json.loads(line) for line in (text_root / "text_feature_manifest.jsonl").read_text().splitlines() if line.strip()]
        if {r["observation_id"] for r in image_rows} != {str(d["detection_id"]) for fr in p2["frames"] for d in fr["detections"]}:
            raise ValueError("image feature sidecar population differs from P2 observations")
        if {r["observation_id"] for r in text_rows} != {r["observation_id"] for r in image_rows}:
            raise ValueError("text feature sidecar population differs from image features")

        mapping_root = run_root / "mapping"
        object_timeline = run_root / "object_timeline"
        timeline_path = object_timeline / "timeline"
        run_stage("conceptgraphs_mapping",
            [str(mapping_python), "-B", str(ROOT / "scripts/run_conceptgraphs_cached_hybrid.py"),
             "--cache", str(run_root), "--text-features", str(text_root),
             "--image-features", str(image_root), "--room-snapshot", str(replay_dir / "snapshot.json"),
             "--frames", str(len(cap["frames"])), "--seed", str(config["mapping"]["seed"]),
             "--output", str(mapping_root), "--timeline-out", str(timeline_path)],
            required=[mapping_root / "run_manifest.json", mapping_root / "export/export_manifest.json",
                      mapping_root / "lineage_stages.json", timeline_path / "manifest.json"], timeout_s=2400)

        _budget_remaining(deadline_monotonic)
        final_graph = run_root / "final_graph"
        from integration_v2.unified_scene_graph import build_unified_graph
        from integration_v2.hybrid_mapping_viewer import build_hybrid_mapping_viewer
        from standalone_sg.serialization.json_graph import load_snapshot
        graph_start = time.monotonic()
        build_unified_graph(replay_dir / "snapshot.json", mapping_root / "export", final_graph,
                            source_kind="reference", membership_policy=config["mapping"]["membership_policy"])
        base_room = load_snapshot(replay_dir / "snapshot.json")
        graph = load_snapshot(final_graph / "unified_snapshot.json")
        room_nodes = {n.node_id: n for n in graph.nodes if n.kind == "room"}
        room_ids = set(room_nodes)
        if not room_nodes:
            raise ValueError("OccuSG replay has no room polygon nodes; C2 cannot build an object-room viewer")
        object_count = validate_unavailable_membership(graph)
        object_nodes = [n for n in graph.nodes if n.kind == "object"]
        base_room_projection = room_graph_integration_signature(base_room)
        unified_room_projection = room_graph_integration_signature(graph)
        if base_room_projection != unified_room_projection:
            raise AssertionError("C2 object import changed room geometry, adjacency, or non-object room relations")
        excluded_native_memberships = excluded_native_object_memberships(base_room, graph)
        viewer_context = {
            "display_note": "Room geometry is present, but its spatial elevation scope is unverified. Object-room membership and room labels are unavailable; this is a batch final view, not live or realtime.",
            "membership_status": "unavailable_elevation_scope",
            "room_semantics_status": config["mapping"]["room_semantics_status"],
            "execution_mode": "fresh_scene_route_batch / final map",
        }
        viewer_path = run_root / "hybrid_viewer.html"
        build_hybrid_mapping_viewer(replay_dir / "snapshot.json", mapping_root, final_graph, viewer_path,
                                    viewer_context=viewer_context)
        graph_elapsed = time.monotonic() - graph_start
        if not viewer_path.is_file() or not (final_graph / "index.html").is_file():
            raise FileNotFoundError("unified graph/viewer outputs were not created")
        _budget_remaining(deadline_monotonic)
        doc["stages"]["unified_graph_and_viewer"] = {"status": "completed", "elapsed_s": round(graph_elapsed, 4),
            "finished_utc": datetime.now(timezone.utc).isoformat(), "membership_status": "unavailable_elevation_scope",
            "room_semantics_status": config["mapping"]["room_semantics_status"],
            "object_count": object_count, "room_count": len(room_nodes), "room_contains_object_edges": 0,
            "room_graph_projection_unchanged": True,
            "source_native_object_memberships_excluded": len(excluded_native_memberships),
            "source_native_object_membership_exclusions": excluded_native_memberships,
            "room_adjacency_edges": sum(1 for edge in graph.edges if edge.relation == "room_adjacency" and edge.source_id in room_ids),
            "preserved_room_contains_edges": sum(1 for edge in graph.edges if edge.relation == "room_contains" and edge.source_id in room_ids)}
        doc["current_stage"] = None
        _record_stage(doc, manifest_path)

        mapping_manifest = _read(mapping_root / "run_manifest.json")
        doc["outputs"] = {
            "capture_manifest": str(capture_dir / "capture_manifest.json"),
            "p2_index": str(p2_dir / "detection_2d_index.json"),
            "p3_manifest": str(p3_dir / "detection_3d_manifest.json"),
            "room_snapshot": str(replay_dir / "snapshot.json"),
            "object_timeline_manifest": str(timeline_path / "manifest.json"),
            "object_export_manifest": str(mapping_root / "export/export_manifest.json"),
            "unified_graph": str(final_graph / "unified_snapshot.json"),
            "viewer": str(viewer_path),
        }
        pose_keys = []
        for rec in cap["frames"]:
            meta = _read(capture_dir / rec["frame_dir"] / "metadata.json")
            matrix = meta.get("base_pose_canonical")
            if matrix is not None:
                pose_keys.append(tuple(round(float(x), 6) for row in matrix for x in row))
        doc["summary"] = {
            "frame_count": len(cap["frames"]),
            "unique_pose_count": len(set(pose_keys)),
            "p2_observation_count": sum(len(fr.get("detections", [])) for fr in p2["frames"]),
            "p3_accepted": p3.get("detection_3d_count"),
            "p3_rejected": p3.get("rejected_detection_count"),
            "image_feature_count": len(image_rows), "text_feature_count": len(text_rows),
            "pre_filter_hypotheses": mapping_manifest.get("foreground_objects_before_postprocess"),
            "final_conceptgraphs_objects": mapping_manifest.get("final_objects_after_text_gated_merge"),
            "room_count": len(room_nodes), "membership_status": "unavailable_elevation_scope",
            "room_semantics_status": config["mapping"]["room_semantics_status"],
            "mapping_runtime_s": mapping_manifest.get("mapping_runtime_s"),
            "mapping_peak_rss_mib": mapping_manifest.get("peak_rss_mib"),
            "route_type": cap.get("route_type"),
        }
        doc["execution_mode"] = "fresh_scene_route_batch"
        doc["stages_actually_executed"] = [name for name, stage in doc["stages"].items()
                                            if stage.get("status") == "completed"]
        doc["stages_reused"] = [name for name, stage in doc["stages"].items()
                                if stage.get("status") == "reused"]
        doc["resources_reused"] = ["local model checkpoints", "pinned reference code",
                                   "existing project environments", "existing OccuSG native binaries"]
        doc["finished_utc"] = datetime.now(timezone.utc).isoformat()
        doc["overall_elapsed_s"] = round(time.monotonic() - started_monotonic, 4)
        doc["status"] = "completed"
        doc["current_stage"] = None
        output_hashes = {}
        for path in sorted(run_root.rglob("*")):
            if path.is_file() and path != manifest_path and not path.name.endswith(".tmp"):
                output_hashes[str(path.relative_to(run_root))] = sha(path)
        doc["output_sha256"] = output_hashes
        _record_stage(doc, manifest_path)
        doc["manifest_path"] = str(manifest_path)
        print(f"[C2] completed | frames={doc['summary']['frame_count']} | objects={doc['summary']['final_conceptgraphs_objects']} | rooms={doc['summary']['room_count']} | membership=unavailable_elevation_scope", flush=True)
        print(f"[C2] model/mapping stage RSS={doc['summary']['mapping_peak_rss_mib']} MiB; output files hashed={len(output_hashes)}", flush=True)
        return doc
    except Exception as exc:
        doc["status"] = "failed"
        doc["error"] = f"{type(exc).__name__}: {exc}"
        doc["finished_utc"] = datetime.now(timezone.utc).isoformat()
        doc["overall_elapsed_s"] = round(time.monotonic() - started_monotonic, 4)
        failed_stage = doc.get("current_stage") or "preflight"
        doc["current_stage"] = failed_stage
        stage = doc.setdefault("stages", {}).setdefault(failed_stage, {})
        stage["status"] = "failed"
        stage["error"] = doc["error"]
        stage["finished_utc"] = doc["finished_utc"]
        started = stage.get("started_utc")
        if started:
            try:
                stage["elapsed_s"] = round(time.monotonic() - start, 4)
            except UnboundLocalError:
                pass
        try:
            _record_stage(doc, manifest_path)
        except OSError:
            pass
        print(f"[C2 failed] {doc['current_stage']}: {doc['error']}", flush=True)
        raise
