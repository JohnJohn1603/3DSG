"""One ordered replay of every persisted v8 RGB-D keyframe.

The module orchestrates the existing current-frame perception workers and
T2-D mapper sessions. It does not implement perception or mapping algorithms.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
V8_DEFAULT = ROOT / "artifacts/live_processing_runs/camera_live_t1_1_20261018_v8"
INVENTORY_DEFAULT = ROOT / "artifacts/diagnostics/camera_live_v8_keyframe_inventory_20261018/keyframe_inventory.json"
C2_DEFAULT = ROOT / "artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2"
RUNS_ROOT = ROOT / "artifacts/live_processing_runs"
EXPECTED_INDICES = [0, 26, 33, 40, 50, 61, 68, 75, 82, 89, 96, 103, 110]

from integration_v2.camera_live_contract import (  # noqa: E402
    build_joined_perception_packet, host_monotonic_domain,
    validate_current_source_packet, validate_event_file_hashes,
    validate_published_event, validate_stage_response,
)
from integration_v2.live_processing import (  # noqa: E402
    _join_one, _read, _sha, _verify_event, _write_live_index,
)
from scripts.run_camera_live_pilot import (  # noqa: E402
    _wait_ready_workers, _stop_owned_processes, atomic_json, read,
    send, sha, wait_worker_json, write_capture_packet,
)
from standalone_sg.serialization.live_revision_io import append_jsonl  # noqa: E402


def _option(command: list[str], name: str) -> Path:
    try:
        return Path(command[command.index(name) + 1]).expanduser().resolve()
    except (ValueError, IndexError) as exc:
        raise ValueError(f"locked C2 command is missing {name}") from exc


def _worker_python(c2: dict[str, Any], stage: str) -> Path:
    command = c2.get("stages", {}).get(stage, {}).get("command")
    if not isinstance(command, list) or not command:
        raise ValueError(f"C2 manifest has no interpreter for {stage}")
    # Preserve the venv path itself: resolving its python symlink to the base
    # interpreter bypasses pyvenv.cfg and loses that environment's packages.
    result = Path(os.path.abspath(os.path.expanduser(str(command[0]))))
    if not result.is_file():
        raise FileNotFoundError(f"locked interpreter missing: {result}")
    return result


def validate_locked_frames(rows: list[dict[str, Any]]) -> None:
    """Reject omissions, duplicates, or reordering before any inference."""
    identities = [(int(row["frame_index"]), str(row["frame_id"])) for row in rows]
    if [i for i, _ in identities] != EXPECTED_INDICES:
        raise ValueError(f"v8 persisted frame indices differ from locked 13-frame sequence: {[i for i, _ in identities]}")
    if len({fid for _, fid in identities}) != 13 or identities != sorted(identities):
        raise ValueError("v8 persisted frames contain duplicate IDs or are not strictly ordered")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_hashes_from_event(source_root: Path, event: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    payload = event["payload"]
    result = payload["result"]
    frame_id = str(payload["packet"]["frame_id"])
    path = (source_root / str(result["path"])).resolve()
    if source_root.resolve() not in path.parents:
        raise ValueError("v8 keyframe path escapes source run")
    hashes = result.get("files_sha256")
    if set(hashes or {}) != {"rgb.npy", "depth_m.npy", "valid_mask.npy", "metadata.json"}:
        raise ValueError(f"incomplete raw keyframe hashes for {frame_id}")
    validate_event_file_hashes(path, hashes)
    metadata = _read(path / "metadata.json")
    if metadata.get("raw_files_sha256") != {k: hashes[k] for k in ("rgb.npy", "depth_m.npy", "valid_mask.npy")}:
        raise ValueError(f"raw keyframe metadata hashes differ for {frame_id}")
    return path, dict(hashes)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _copy_endpoint_p2(source_root: Path, run_root: Path, response: dict[str, Any]) -> dict[str, Any]:
    result = dict(response)
    rows = []
    src_root = source_root / "packets/assets/detection_2d"
    dst_root = run_root / "packets/assets/detection_2d"
    for raw in response.get("detections", []):
        row = dict(raw)
        source = src_root / str(row["mask_file"])
        if not source.is_file() or _hash(source) != row.get("mask_sha256"):
            raise ValueError(f"endpoint P2 mask missing/hash mismatch: {source}")
        target = dst_root / str(row["mask_file"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _hash(target) != row["mask_sha256"]:
            raise ValueError(f"copied endpoint mask hash mismatch: {target}")
        rows.append(row)
    result["detections"] = rows
    return result


def _copy_endpoint_features(source_root: Path, run_root: Path, response: dict[str, Any]) -> dict[str, Any]:
    result = dict(response)
    rows = []
    target_root = run_root / "packets/features"
    target_root.mkdir(parents=True, exist_ok=True)
    for raw in response.get("rows", []):
        row = dict(raw)
        source = (ROOT / str(row["feature_path"])).resolve()
        if not source.is_file() or _hash(source) != row.get("feature_file_sha256"):
            raise ValueError(f"endpoint feature missing/hash mismatch: {source}")
        target = target_root / source.name
        shutil.copy2(source, target)
        if _hash(target) != row["feature_file_sha256"]:
            raise ValueError(f"copied endpoint feature hash mismatch: {target}")
        row["feature_path"] = target.relative_to(ROOT).as_posix()
        rows.append(row)
    result["rows"] = rows
    if "result" in result:
        result["result"] = {**result["result"], "rows": rows}
    return result


def _validate_endpoint(source_root: Path, base: dict[str, Any], acceptance: dict[str, Any],
                       feature_contract: dict[str, Any], run_root: Path) -> dict[str, Any]:
    idx = int(base["frame_index"])
    stem = f"{idx:06d}"
    packet_path = source_root / "packets" / f"joined-{stem}.json"
    p2_path = source_root / "packets" / f"p2-{stem}.json"
    p3_path = source_root / "packets" / f"p3-{stem}.json"
    feat_path = source_root / "packets" / f"feat-{stem}.json"
    for path in (packet_path, p2_path, p3_path, feat_path):
        if not path.is_file():
            raise ValueError(f"endpoint perception artifact missing: {path}")
    old_packet = _read(packet_path)
    p2 = _read(p2_path); p3 = _read(p3_path); feat = _read(feat_path)
    for stage, response in (("P2", p2), ("P3", p3), ("OpenCLIP", feat)):
        validate_stage_response(base, response, stage=f"v8 endpoint {stage}")
    if old_packet.get("source_hashes") != base.get("source_hashes"):
        raise ValueError(f"endpoint packet source hashes do not match regenerated capture adapter: {base['frame_id']}")
    p2 = _copy_endpoint_p2(source_root, run_root, p2)
    feat = _copy_endpoint_features(source_root, run_root, feat)
    p2_ids = [str(row["detection_id"]) for row in p2.get("detections", [])]
    p3_ids = [str(row["detection_id"]) for row in p3.get("detections", [])]
    feat_ids = [str(row["observation_id"]) for row in feat.get("rows", [])]
    if len(p2_ids) != len(set(p2_ids)) or p2_ids != p3_ids or p2_ids != feat_ids:
        raise ValueError(f"endpoint P2/P3/feature observation coverage differs: {base['frame_id']}")
    for row in feat.get("rows", []):
        if row.get("feature_contract") != feature_contract:
            raise ValueError(f"endpoint feature contract mismatch: {row.get('observation_id')}")
        archive_path = ROOT / row["feature_path"]
        with np.load(archive_path, allow_pickle=False) as archive:
            image = np.asarray(archive["image_feature"], dtype=np.float32)
            text = np.asarray(archive["text_feature"], dtype=np.float32)
        for label, vector in (("image", image), ("text", text)):
            if vector.shape != (512,) or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5, rtol=1e-5):
                raise ValueError(f"endpoint {label} vector fails shape/finite/normalization: {row['observation_id']}")
    if p3.get("result", {}).get("provenance", {}).get("profile_id") != "replica_cad_yolo11n_seg_dev_v2":
        raise ValueError(f"endpoint P3 profile provenance mismatch: {base['frame_id']}")
    # Keep the endpoint joined output as a provenance check, but rebuild the
    # runtime-ready packet with run-local masks/features and current capture root.
    joined = build_joined_perception_packet(base, p2, p3, feat)
    return {"p2": p2, "p3": p3, "features": feat, "joined": joined,
            "source_joined_sha256": _hash(packet_path), "reused": True}


def _preflight_sources(source_root: Path, c2_root: Path, run_root: Path) -> dict[str, Any]:
    acceptance_path = source_root / "pilot_acceptance_manifest.json"
    integrity_path = source_root / "pilot_integrity_manifest.json"
    inventory_path = INVENTORY_DEFAULT
    route_path = ROOT / "artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2/effective_config.json"
    c2_manifest_path = c2_root / "c2_run_manifest.json"
    for path in (acceptance_path, integrity_path, inventory_path, route_path, c2_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(f"preflight source missing: {path}")
    acceptance, integrity, inventory, route, c2 = map(_read, (acceptance_path, integrity_path, inventory_path, route_path, c2_manifest_path))
    if acceptance.get("run_id") != source_root.name or acceptance.get("status") != "pipeline_completed":
        raise ValueError("v8 acceptance manifest does not identify a completed source run")
    if c2.get("status") != "completed" or c2.get("scene_id") != acceptance.get("scene_id"):
        raise ValueError("C2 source is not a completed same-scene config source")
    if inventory.get("source_run_id") != source_root.name or inventory.get("source_acceptance_manifest_sha256") != _hash(acceptance_path):
        raise ValueError("v8 keyframe inventory source/acceptance hash mismatch")
    locked_integrity = acceptance.get("integrity_manifest", {})
    if (locked_integrity.get("path") != integrity_path.name
            or locked_integrity.get("sha256") != _hash(integrity_path)):
        raise ValueError("v8 integrity manifest does not match the acceptance record")
    if _hash(route_path) != acceptance.get("route_config_sha256"):
        raise ValueError("v8 effective route config hash differs from acceptance manifest")
    model_hashes = acceptance.get("source_and_model_hashes", {})
    resolved_hashes = {}
    for name in ("p2_onnx", "p2_profile", "p2_names", "openclip_checkpoint", "feature_checkpoint_report", "capture_profile", "effective_route_config"):
        row = model_hashes.get(name, {})
        path = Path(str(row.get("path", ""))).expanduser().resolve()
        if not path.is_file() or _hash(path) != row.get("sha256"):
            raise ValueError(f"v8 locked input/config hash mismatch: {name} ({path})")
        resolved_hashes[name] = {"path": str(path), "sha256": _hash(path)}
    if route.get("scene_id") != acceptance.get("scene_id") or route.get("sequence_id") != acceptance.get("sequence_id"):
        raise ValueError("v8 route config scene/sequence differs from acceptance")
    if route.get("resolution_px") != [640, 480] or float(route.get("horizontal_fov_deg")) != 90.0:
        raise ValueError("v8 route resolution/HFOV differs from locked camera contract")
    config_source_route_path = c2_root / "effective_route_config.json"
    config_source_route = _read(config_source_route_path)
    if (config_source_route.get("resolution_px") != [640, 480]
            or config_source_route.get("scene_id") != acceptance.get("scene_id")):
        raise ValueError("mapper config source does not use the locked 640x480 input profile")
    if _hash(c2_manifest_path) != acceptance.get("source_c2_manifest_sha256"):
        raise ValueError("v8 C2 source manifest hash differs from acceptance")
    mapper_contract_path = c2_root / "mapping/experiment_contract.json"
    mapper_contract = _read(mapper_contract_path)
    from scripts.run_conceptgraphs_cached_hybrid import base_mapping_config
    if mapper_contract.get("effective_config") != base_mapping_config():
        raise ValueError("C2 mapping effective config differs from current locked base config")
    seed = mapper_contract.get("rng_control", {}).get("seed")
    if not isinstance(seed, int):
        raise ValueError("C2 mapping seed is missing")
    p2_cmd = c2["stages"]["perception_p2"]["command"]
    p3_cmd = c2["stages"]["projection_p3"]["command"]
    clip_cmd = c2["stages"]["image_features"]["command"]
    mapper_cg_py = _worker_python(c2, "conceptgraphs_mapping")
    mapper_occ_py = _worker_python(c2, "occusg_rooms")
    p2_py = _worker_python(c2, "perception_p2")
    p3_py = _worker_python(c2, "projection_p3")
    clip_py = _worker_python(c2, "image_features")
    if _option(p3_cmd, "--profile") != _option(p2_cmd, "--config"):
        raise ValueError("P2/P3 locked profiles do not match")
    feature_report_path = _option(clip_cmd, "--checkpoint-report")
    feature_report = _read(feature_report_path)
    clip_checkpoint = _option(clip_cmd, "--checkpoint")
    if _hash(clip_checkpoint) != model_hashes["openclip_checkpoint"]["sha256"]:
        raise ValueError("OpenCLIP worker checkpoint differs from v8 locked checkpoint")
    feature_contract = {
        "checkpoint_sha256": str(feature_report["checkpoint_sha256"]),
        "model_fingerprint": str(feature_report["model_fingerprint"]),
        "preprocessing_id": str(feature_report["preprocessing_id"]),
        "prompt_policy": "exact cached class_raw string; no prompt template or canonicalization",
        "tokenizer_id": "open_clip.get_tokenizer('ViT-B-32')",
    }
    if feature_contract["checkpoint_sha256"] != model_hashes["openclip_checkpoint"]["sha256"]:
        raise ValueError("feature report checkpoint digest differs from locked checkpoint")
    journal_path = source_root / "camera_keyframes.jsonl"
    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = [row for row in events if row.get("event") == "keyframe_persisted"]
    if len(events) != 13:
        raise ValueError(f"expected all 13 persisted keyframe events, found {len(events)}")
    rows = []
    inv_by_id = {str(row["frame_id"]): row for row in inventory.get("keyframes", [])}
    for event in events:
        validate_published_event(event["payload"])
        packet = event["payload"]["packet"]
        meta = packet["metadata"]
        fid = str(packet["frame_id"])
        idx = int(meta["frame_index"])
        inv = inv_by_id.get(fid)
        if inv is None or int(inv["frame_index"]) != idx:
            raise ValueError(f"keyframe absent/misaligned in locked inventory: {fid}")
        if (meta.get("scene_id"), meta.get("sequence_id")) != (acceptance["scene_id"], acceptance["sequence_id"]):
            raise ValueError(f"raw keyframe scene/sequence mismatch: {fid}")
        if meta.get("resolution_px") != [640, 480] or meta.get("calibration_id") != "habitat-rgbd-stage-v1-640x480":
            raise ValueError(f"raw keyframe resolution/calibration mismatch: {fid}")
        if meta.get("depth_units") != "m" or meta.get("depth_representation") != "axial":
            raise ValueError(f"raw keyframe depth contract mismatch: {fid}")
        src_dir, raw_hashes = _file_hashes_from_event(source_root, event)
        if set(raw_hashes) != set(inv.get("files", {})):
            raise ValueError(f"inventory raw-file population mismatch: {fid}")
        for name, digest in raw_hashes.items():
            if digest != inv["files"][name]["sha256"]:
                raise ValueError(f"inventory hash mismatch for {fid}/{name}")
        rgb = np.load(src_dir / "rgb.npy", allow_pickle=False)
        depth = np.load(src_dir / "depth_m.npy", allow_pickle=False)
        valid = np.load(src_dir / "valid_mask.npy", allow_pickle=False)
        if rgb.shape != (480, 640, 3) or depth.shape != (480, 640) or valid.shape != (480, 640):
            raise ValueError(f"raw RGB-D array shape mismatch: {fid}")
        if not np.isfinite(depth[valid]).all():
            raise ValueError(f"valid depth contains non-finite values: {fid}")
        intr = meta.get("intrinsics", {})
        if not (np.isclose(float(intr.get("fx", 0)), 320.0, atol=1e-6)
                and np.isclose(float(intr.get("fy", 0)), 320.0, atol=1e-6)
                and np.isclose(float(intr.get("cx", 0)), 319.5, atol=1e-6)
                and np.isclose(float(intr.get("cy", 0)), 239.5, atol=1e-6)):
            raise ValueError(f"raw keyframe intrinsics differ from locked calibration: {fid}")
        rows.append({"frame_index": idx, "frame_id": fid, "event": event,
                     "source_frame_dir": src_dir, "raw_hashes": raw_hashes})
    rows.sort(key=lambda row: row["frame_index"])
    validate_locked_frames(rows)
    if int(acceptance.get("counts", {}).get("p2_keyframes", -1)) != 2 or int(acceptance.get("counts", {}).get("p3_keyframes", -1)) != 2:
        raise ValueError("v8 endpoint counts no longer match the two reusable processed keyframes")
    # Verify these are the known first/last completed endpoints, not arbitrary cache rows.
    if [rows[0]["frame_index"], rows[-1]["frame_index"]] != [0, 110]:
        raise ValueError("expected reusable endpoint frames 000000 and 000110")
    return {"acceptance": acceptance, "inventory": inventory, "route": route, "c2": c2,
            "mapper_contract": mapper_contract, "feature_report": feature_report,
            "config_source_route": config_source_route,
            "feature_contract": feature_contract, "seed": seed, "rows": rows,
            "python": {"perception_p2": str(p2_py), "projection_p3": str(p3_py),
                       "openclip": str(clip_py), "conceptgraphs": str(mapper_cg_py),
                       "occusg": str(mapper_occ_py)},
            "paths": {"source_run": str(source_root), "source_c2": str(c2_root),
                      "route_config": str(route_path), "feature_report": str(feature_report_path),
                      "mapper_config_source_route": str(config_source_route_path),
                      "p2_profile": str(_option(p2_cmd, "--config")),
                      "p2_names": str(_option(p2_cmd, "--names")),
                      "p2_weights": str(_option(p2_cmd, "--weights")),
                      "p3_profile": str(_option(p3_cmd, "--profile")),
                      "openclip_checkpoint": str(clip_checkpoint)},
            "hashes": {"v8_acceptance": _hash(acceptance_path), "v8_integrity": _hash(integrity_path),
                       "v8_inventory": _hash(inventory_path), "v8_journal": _hash(journal_path),
                       "route_config": _hash(route_path), "c2_manifest": _hash(c2_manifest_path),
                       "mapper_contract": _hash(mapper_contract_path), "feature_report": _hash(feature_report_path),
                       **resolved_hashes}}


def _materialize_base_packets(source_root: Path, run_root: Path, preflight: dict[str, Any]) -> list[dict[str, Any]]:
    route = preflight["route"]
    route_cfg = {"scene_asset": route["scene_asset"], "min_depth_m": route["min_depth_m"],
                 "max_depth_m": route["max_depth_m"]}
    output = []
    for row in preflight["rows"]:
        event = row["event"]["payload"]
        payload_packet, payload_result = event["packet"], event["result"]
        source_path = (source_root / payload_result["path"]).resolve()
        relpath = os.path.relpath(source_path, run_root)
        capture_result = {"frame_id": row["frame_id"], "path": relpath,
                          "files_sha256": payload_result["files_sha256"]}
        data = write_capture_packet(run_root, payload_packet, capture_result, route_cfg)
        data["asset_root"] = str(run_root / "packets/assets")
        data["raw_keyframe_source"] = {"run_id": source_root.name,
            "source_path": str(source_path), "files_sha256": dict(row["raw_hashes"]),
            "source_event_sha256": hashlib.sha256(json.dumps(row["event"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()}
        validate_current_source_packet(data, context=f"ordered replay raw frame {row['frame_id']}")
        from adapters.habitat.frames import frame_from_capture_record
        rgbd, fp = frame_from_capture_record(data["capture_record"], Path(data["capture_root"]))
        if fp.frame_id != row["frame_id"] or fp.frame_index != row["frame_index"]:
            raise ValueError(f"Habitat frame adapter identity mismatch: {row['frame_id']}")
        if rgbd.depth_m.shape != (480, 640) or fp.rgbd.intrinsics.width != 640:
            raise ValueError(f"Habitat frame adapter RGB-D/calibration mismatch: {row['frame_id']}")
        data["p2_detections"] = []
        data["p3_detections"] = []
        data["features"] = []
        data["perception_status"] = "not_started"
        frame_json = run_root / "packets" / f"frame-{row['frame_index']:06d}.json"
        atomic_json(frame_json, data)
        atomic_json(run_root / "packets" / f"packet-{row['frame_index']:06d}.json", data)
        data["frame_json"] = str(frame_json)
        output.append(data)
    return output


def _preflight_mapper_adapters(run_root: Path, c2_root: Path, worker_pythons: dict[str, str],
                               *, model_free: bool) -> dict[str, Any]:
    results = {}
    worker_script = ROOT / "scripts/run_t2_d_cached_packet_worker.py"
    for kind in ("conceptgraphs", "occusg"):
        python = worker_pythons[kind]
        command = [python, "-u", str(worker_script), "--worker", kind,
                   "--run-root", str(run_root), "--packet-root", str(run_root / "packets"),
                   "--source", str(c2_root), "--preflight"]
        started = time.perf_counter()
        log_path = run_root / "logs" / f"{kind}_{'raw' if model_free else 'packet'}_preflight.log"
        with log_path.open("ab", buffering=0) as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1",
                     "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src")))},
                timeout=180, check=False)
        if result.returncode != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
            raise RuntimeError(f"{kind} worker adapter preflight failed (code {result.returncode}); see {log_path}: {tail}")
        result_doc = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        results[kind] = {"status": "passed", "interpreter": python,
                         "frame_count": len(result_doc.get("frames", [])),
                         "adapter_log": str(log_path), "elapsed_s": time.perf_counter() - started}
    return results


def _rss_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _wait_response(proc: subprocess.Popen, path: Path, *, stage: str,
                   peak_rss: dict[str, int], timeout_s: float = 180.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sample = _rss_bytes(proc.pid)
        if sample is not None:
            peak_rss[stage] = max(peak_rss.get(stage, 0), sample)
        if path.is_file():
            response = read(path)
            if response.get("status") != "completed":
                raise RuntimeError(f"{stage} failed: {response.get('error', response.get('status'))}")
            return response
        code = proc.poll()
        if code is not None:
            raise RuntimeError(f"{stage} worker exited with code {code} before response")
        time.sleep(.02)
    raise TimeoutError(f"{stage} response timeout: {path}")


def _spawn_perception(kind: str, python: str, run_root: Path, run_id: str,
                      bootstrap_id: str, route_hash: str, extra: list[str],
                      handles: list[Any], processes: list[subprocess.Popen]) -> subprocess.Popen:
    log = (run_root / "logs" / f"{kind}_worker.log").open("ab", buffering=0)
    handles.append(log)
    ready = run_root / "ready" / f"{kind}.json"
    cmd = [python, "-u", str(ROOT / "scripts/run_camera_live_pilot.py"), "--worker", kind,
           "--run-root", str(run_root), "--ready-file", str(ready), "--run-id", run_id,
           "--bootstrap-id", bootstrap_id, "--route-config-sha256", route_hash, *extra]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=log,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1",
             "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src")))})
    processes.append(proc)
    return proc


def _spawn_mapper(kind: str, python: str, c2_root: Path, run_root: Path, run_id: str,
                  bootstrap_id: str, route_hash: str, seed: int,
                  handles: list[Any], processes: list[subprocess.Popen]) -> subprocess.Popen:
    log = (run_root / "logs" / f"{kind}_worker.log").open("ab", buffering=0)
    handles.append(log)
    ready = run_root / "ready" / f"{kind}.json"
    cmd = [python, "-u", str(ROOT / "scripts/run_t2_d_cached_packet_worker.py"),
           "--worker", kind, "--run-root", str(run_root), "--packet-root", str(run_root / "packets"),
           "--source", str(c2_root), "--ready-file", str(ready), "--live", "--run-id", run_id,
           "--bootstrap-id", bootstrap_id, "--route-config-sha256", route_hash]
    if kind == "conceptgraphs":
        cmd += ["--seed", str(seed)]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=log,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1",
             "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src")))})
    processes.append(proc)
    return proc


def _mapper_ready_worker_pairs(conceptgraphs: subprocess.Popen,
                                occusg: subprocess.Popen) -> list[tuple[str, subprocess.Popen]]:
    # File names/launch roles use "occusg", while its marker schema kind is
    # "occusg_rooms". Keep the ready-file role here and validate kind separately.
    return [("conceptgraphs", conceptgraphs), ("occusg", occusg)]


def _prepare_viewer(run_root: Path, scene_id: str, sequence_id: str) -> Path:
    viewer = run_root / "viewer"
    viewer.mkdir(parents=True, exist_ok=False)
    (viewer / "geometry").mkdir()
    shutil.copy2(ROOT / "scripts/causal_timeline_viewer.html", viewer / "index.html")
    index = {"schema": "d1-live-timeline-index/v1", "timeline_manifest": {
        "scene_id": scene_id, "sequence_id": sequence_id,
        "causality": {"objects": "prefix_state_only", "rooms": "prefix_state_only_native_replay_reference",
                      "membership": "unavailable_elevation_scope", "room_semantics": "unavailable_membership_scope"}},
        "source_timeline": "ordered replay of the 13 persisted v8 keyframes",
        "frames": [], "world_bounds_xy": [-10, 10, -10, 10], "live_mode": True,
        "status": "starting", "final_postprocess": None,
        "membership_status": "unavailable_elevation_scope",
        "room_semantics_status": "unavailable_membership_scope",
        "derived_geometry_policy": {"max_points_per_object": 300,
            "source_npz_unchanged": True, "cache_policy": "viewer LRU 6"}}
    atomic_json(viewer / "live_index.json", index)
    atomic_json(viewer / "live_head.json", {"schema": "d1-live-head/v1", "status": "starting",
        "revision_count": 0, "scene_id": scene_id, "sequence_id": sequence_id,
        "live_index_sha256": _hash(viewer / "live_index.json"), "final_postprocess_available": False})
    return viewer


def _bootstrap_replay_viewer(run_root: Path, viewer: Path,
                             manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Start the run record, then publish and validate one empty D2 entrypoint."""
    base_index = _read(viewer / "live_index.json")
    manifest["status"] = "started"
    manifest["viewer"] = {"entrypoint": None, "url": None,
                          "gui_acceptance": "pending", "source_data": "this run's own prefixes"}
    atomic_json(run_root / "run_manifest.json", manifest)
    from scripts.build_d2_xyz_viewer import bootstrap, validate_entrypoint_bundle
    d2 = bootstrap(run_root, "v8_13_keyframes")
    validate_entrypoint_bundle(viewer)
    manifest["viewer"]["entrypoint"] = d2["viewer_entrypoint"]
    manifest["viewer"]["bootstrap_status"] = "passed"
    manifest["status"] = "running_mapper_sessions"
    return base_index, d2


def _write_integrity(run_root: Path, manifest: dict[str, Any]) -> None:
    immutable = {}
    operational = {}
    for path in sorted(run_root.rglob("*")):
        if not path.is_file() or path.name == "run_manifest.json":
            continue
        relative = path.relative_to(run_root).as_posix()
        if relative.startswith("logs/"):
            operational[relative] = {"sha256_at_checkpoint": _hash(path), "mutable_after_checkpoint": False}
        else:
            immutable[relative] = _hash(path)
    manifest["immutable_output_sha256"] = immutable
    manifest["immutable_output_count"] = len(immutable)
    manifest["operational_log_hashes"] = operational
    manifest["hash_checkpoint_utc"] = time.time()
    atomic_json(run_root / "run_manifest.json", manifest)
    atomic_json(run_root / "run_manifest.sha256.json", {
        "schema": "v8-ordered-replay-manifest-receipt/v1", "manifest": "run_manifest.json",
        "sha256": _hash(run_root / "run_manifest.json"), "status": manifest.get("status")})


def run_ordered_replay(*, source_run: Path = V8_DEFAULT, mapper_config_source: Path = C2_DEFAULT,
                       run_id: str = "hm3d00802_t1_1_all13_replay_20261019_v1",
                       available_ram_floor_bytes: int = 3 * 1024**3) -> dict[str, Any]:
    source_run = Path(source_run).resolve(); c2_root = Path(mapper_config_source).resolve()
    run_root = (RUNS_ROOT / run_id).resolve()
    if run_root.exists():
        raise FileExistsError(f"refusing to overwrite existing replay run: {run_root}")
    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
    available_ram = int(mem["MemAvailable"].split()[0]) * 1024
    if available_ram < available_ram_floor_bytes:
        raise MemoryError(f"RAM preflight below existing 3 GiB safety floor: {available_ram} bytes")
    run_root.mkdir(parents=True, exist_ok=False)
    for rel in ("logs", "ready", "control", "packets/assets/detection_2d/masks",
                "packets/features", "perception", "branches/objects/acks", "branches/rooms/acks",
                "joined"):
        (run_root / rel).mkdir(parents=True, exist_ok=True)
    run_id = run_root.name
    bootstrap_id = uuid.uuid4().hex
    run_started = time.perf_counter()
    run_deadline = time.monotonic() + 30 * 60
    processes: list[subprocess.Popen] = []
    handles: list[Any] = []
    worker_groups: dict[str, dict[str, subprocess.Popen]] = {"perception": {}, "mappers": {}}
    server = None
    status = "failed_preflight"
    error: str | None = None
    timings: dict[str, Any] = {"source_preflight_s": None, "raw_adapter_preflight_s": None,
        "perception_frames": [], "mapper_frames": [], "terminal_s": None}
    manifest: dict[str, Any] = {"schema": "v8-ordered-keyframe-replay/v1", "run_id": run_id,
        "status": "preflight", "execution_mode": "ordered_cached_keyframe_replay",
        "source_run_id": source_run.name, "source_run": str(source_run),
        "mapper_config_source": str(c2_root), "scene_id": None, "sequence_id": None,
        "frame_count_expected": 13, "ordered_frame_indices": EXPECTED_INDICES,
        "execution_plan": {"capture_runs": 0, "p2_session": 1, "p3_session": 1,
            "openclip_session": 1, "p2_updates": 11, "p3_updates": 11,
            "image_feature_updates": 11, "conceptgraphs_sessions": 1,
            "conceptgraphs_updates": 13, "conceptgraphs_terminal_postprocess": 1,
            "occusg_sessions": 1, "occusg_updates": 13, "occusg_terminal_flush": 1,
            "unified_prefix_revisions": 13, "mapper_retries": 0},
        "scope": {"membership": "unavailable_elevation_scope",
                  "room_semantics": "unavailable_membership_scope"},
        "main29_assumed_scope_declaration_applied": False,
        "terminal_postprocess_is_separate": True, "future_prefix_state_backfill": False,
        "partial_artifacts_preserved": True, "resume_after_mapper_mutation": False}
    atomic_json(run_root / "run_manifest.json", manifest)
    try:
        from integration_v2.live_processing import append_jsonl as append_event
        execution_events = run_root / "logs/execution_events.jsonl"
        def event(name: str, **fields: Any) -> None:
            payload = {"event": name, "monotonic_s": time.monotonic(),
                "clock_domain": host_monotonic_domain(), "wall_time_unix_s": time.time(), **fields}
            append_event(execution_events, payload)
            print(json.dumps(payload, sort_keys=True), flush=True)

        def remaining_s() -> float:
            remaining = run_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("single replay exceeded the approved 30-minute runtime cap")
            return remaining

        preflight_started = time.perf_counter()
        preflight = _preflight_sources(source_run, c2_root, run_root)
        manifest.update({"scene_id": preflight["acceptance"]["scene_id"],
                         "sequence_id": preflight["acceptance"]["sequence_id"],
                         "route_run_id": preflight["acceptance"]["route_run_id"],
                         "source_hashes": preflight["hashes"],
                         "worker_interpreters": preflight["python"],
                         "feature_contract": preflight["feature_contract"],
                         "mapping_effective_config": preflight["mapper_contract"]["effective_config"],
                         "mapping_seed": preflight["seed"],
                         "ram_preflight": {"available_bytes": available_ram,
                                            "required_floor_bytes": available_ram_floor_bytes},
                         "stages": {"source_preflight": "completed", "raw_frame_adapters": "running",
                             "perception": "not_started", "mapper_adapters": "not_started",
                             "mapper_sessions": "not_started", "terminal": "not_started"}})
        route_hash = preflight["hashes"]["route_config"]
        raw_packets = _materialize_base_packets(source_run, run_root, preflight)
        timings["source_preflight_s"] = time.perf_counter() - preflight_started
        adapter_started = time.perf_counter()
        raw_adapter_preflight = _preflight_mapper_adapters(run_root, c2_root,
            {"conceptgraphs": preflight["python"]["conceptgraphs"],
             "occusg": preflight["python"]["occusg"]}, model_free=True)
        timings["raw_adapter_preflight_s"] = time.perf_counter() - adapter_started
        manifest["adapter_preflight"] = {"raw_rgbd_current_frame": raw_adapter_preflight}
        manifest["stages"]["raw_frame_adapters"] = "completed"
        atomic_json(run_root / "run_manifest.json", manifest)

        # Source v8 gave completed perception only to frames 0 and 110.
        endpoint_indices = {0, 110}
        reused = {}
        for base in raw_packets:
            if int(base["frame_index"]) not in endpoint_indices:
                continue
            expected = preflight["inventory"]["keyframes"]
            idx_map = {int(r["frame_index"]): r for r in expected}
            inventory_row = idx_map[int(base["frame_index"])]
            result = _validate_endpoint(source_run, base, preflight["acceptance"],
                                        preflight["feature_contract"], run_root)
            result["inventory_processing_status"] = inventory_row.get("processing", {}).get("perception_status")
            reused[int(base["frame_index"])] = result
        if set(reused) != endpoint_indices:
            raise ValueError("preflight did not validate exactly the two approved endpoint reuse packets")

        # Initialize all three persistent inference processes only after the
        # raw source, endpoint outputs, config contracts, and mapper adapters pass.
        manifest["stages"]["perception"] = "starting_persistent_sessions"
        manifest["status"] = "running_perception_sessions"
        manifest["perception_model_load_attempts"] = {"p2": 1, "openclip": 1}
        atomic_json(run_root / "run_manifest.json", manifest)
        profile = preflight["paths"]["p2_profile"]
        names = preflight["paths"]["p2_names"]
        weights = preflight["paths"]["p2_weights"]
        checkpoint = preflight["paths"]["openclip_checkpoint"]
        report = preflight["feature_report"]
        p2 = _spawn_perception("p2", preflight["python"]["perception_p2"], run_root, run_id,
            bootstrap_id, route_hash, ["--profile", profile, "--names", names, "--weights", weights], handles, processes)
        p3 = _spawn_perception("p3", preflight["python"]["projection_p3"], run_root, run_id,
            bootstrap_id, route_hash, ["--profile", preflight["paths"]["p3_profile"]], handles, processes)
        features = _spawn_perception("features", preflight["python"]["openclip"], run_root, run_id,
            bootstrap_id, route_hash, ["--checkpoint", checkpoint,
                "--checkpoint-sha256", preflight["feature_contract"]["checkpoint_sha256"],
                "--model-fingerprint", preflight["feature_contract"]["model_fingerprint"],
                "--preprocessing-id", preflight["feature_contract"]["preprocessing_id"]], handles, processes)
        worker_groups["perception"] = {"p2": p2, "p3": p3, "features": features}
        readiness = _wait_ready_workers(run_root, list(worker_groups["perception"].items()),
            run_id=run_id, bootstrap_id=bootstrap_id, route_config_sha256=route_hash,
            timeout_s=min(300, remaining_s()))
        manifest["perception_worker_readiness"] = readiness
        manifest["perception_model_load_attempts"] = {"p2": 1, "openclip": 1}
        manifest["stages"]["perception"] = "running_on_11_uncached_frames"
        atomic_json(run_root / "run_manifest.json", manifest)
        peak_rss: dict[str, int] = {}
        for completed_count, base in enumerate(raw_packets, start=1):
            index, frame_id = int(base["frame_index"]), str(base["frame_id"])
            if index in reused:
                result = reused[index]
                joined = result["joined"]
                base.update({"p2_detections": joined["p2_detections"],
                             "p3_detections": joined["p3_detections"],
                             "features": joined["features"], "perception_status": joined["perception_status"]})
                p2r, p3r, fr = result["p2"], result["p3"], result["features"]
                reused_status = "reused_validated_v8_endpoint"
                timings["perception_frames"].append({"frame_index": index, "frame_id": frame_id,
                    "status": reused_status, "p2_count": len(p2r["detections"]),
                    "p3_accepted": sum(r.get("status") == "accepted" for r in p3r.get("detections", [])),
                    "p3_rejected": sum(r.get("status") != "accepted" for r in p3r.get("detections", [])),
                    "feature_count": len(fr.get("rows", [])), "inference_attempted": False})
            else:
                remaining_s()
                started = time.perf_counter()
                p2_path = run_root / "packets" / f"p2-{index:06d}.json"
                event("perception_stage_started", stage="P2", frame_index=index, frame_id=frame_id)
                send(p2, {"op": "frame", "frame_json": base["frame_json"], "response_json": str(p2_path)})
                p2r = _wait_response(p2, p2_path, stage="p2", peak_rss=peak_rss,
                                     timeout_s=min(180, remaining_s()))
                validate_stage_response(base, p2r, stage="P2")
                event("perception_stage_completed", stage="P2", frame_index=index, frame_id=frame_id,
                      detection_count=len(p2r["detections"]))
                base["p2_detections"] = p2r["detections"]
                atomic_json(run_root / "packets" / f"joined-{index:06d}.json", base)
                current_path = run_root / "packets" / f"joined-{index:06d}.json"
                p3_path = run_root / "packets" / f"p3-{index:06d}.json"
                feat_path = run_root / "packets" / f"feat-{index:06d}.json"
                event("perception_stage_started", stage="P3", frame_index=index, frame_id=frame_id)
                event("perception_stage_started", stage="OpenCLIP", frame_index=index, frame_id=frame_id)
                send(p3, {"op": "frame", "packet_json": str(current_path), "response_json": str(p3_path)})
                send(features, {"op": "frame", "packet_json": str(current_path), "response_json": str(feat_path)})
                p3r = _wait_response(p3, p3_path, stage="p3", peak_rss=peak_rss,
                                     timeout_s=min(180, remaining_s()))
                fr = _wait_response(features, feat_path, stage="openclip", peak_rss=peak_rss,
                                    timeout_s=min(180, remaining_s()))
                validate_stage_response(base, p3r, stage="P3")
                validate_stage_response(base, fr, stage="OpenCLIP")
                event("perception_stage_completed", stage="P3", frame_index=index, frame_id=frame_id,
                      observation_count=len(p3r.get("detections", [])))
                event("perception_stage_completed", stage="OpenCLIP", frame_index=index, frame_id=frame_id,
                      feature_count=len(fr.get("rows", [])))
                joined = build_joined_perception_packet(base, p2r, p3r, fr)
                base.update({"p2_detections": joined["p2_detections"],
                             "p3_detections": joined["p3_detections"],
                             "features": joined["features"], "perception_status": joined["perception_status"]})
                reused_status = "inferred_current_frame"
                timings["perception_frames"].append({"frame_index": index, "frame_id": frame_id,
                    "status": reused_status, "perception_wall_s": time.perf_counter() - started,
                    "p2_count": len(p2r["detections"]),
                    "p3_accepted": sum(r.get("status") == "accepted" for r in p3r.get("detections", [])),
                    "p3_rejected": sum(r.get("status") != "accepted" for r in p3r.get("detections", [])),
                    "feature_count": len(fr.get("rows", [])),
                    "p2_model_load_count": p2r.get("model_load_count"),
                    "openclip_model_load_count": fr.get("model_load_count"),
                    "openclip_image_encode_count": fr.get("image_encode_count"),
                    "openclip_text_encode_count": fr.get("text_encode_count"),
                    "openclip_text_cache_size": fr.get("text_cache_size")})
            # Preserve a canonical copy of every stage response and ready packet.
            atomic_json(run_root / "packets" / f"p2-{index:06d}.json", p2r)
            atomic_json(run_root / "packets" / f"p3-{index:06d}.json", p3r)
            atomic_json(run_root / "packets" / f"feat-{index:06d}.json", fr)
            atomic_json(run_root / "packets" / f"joined-{index:06d}.json", base)
            atomic_json(run_root / "packets" / f"packet-{index:06d}.json", base)
            manifest["perception_progress"] = {"completed_frames": completed_count,
                "expected_frames": 13, "last_frame_index": index, "last_frame_id": frame_id,
                "last_frame_status": reused_status}
            event("perception_frame_ready", frame_index=index, frame_id=frame_id,
                  status=reused_status, inference_attempted=index not in reused)
            atomic_json(run_root / "run_manifest.json", manifest)
        for proc in worker_groups["perception"].values():
            send(proc, {"op": "close"})
        for proc in worker_groups["perception"].values():
            proc.wait(timeout=20)
        if any(proc.returncode != 0 for proc in worker_groups["perception"].values()):
            raise RuntimeError("perception session worker did not close cleanly")
        manifest["perception_model_load_counts"] = {
            "p2_onnx": 1, "openclip": 1, "p3_geometry_model": "not_applicable"}
        manifest["perception_peak_rss_bytes_by_worker"] = peak_rss
        manifest["stages"]["perception"] = "completed"
        # Mapper preflight consumes the exact 13 ready packets using each
        # worker's own interpreter and current-frame adapter, before mutation.
        mapper_preflight = _preflight_mapper_adapters(run_root, c2_root,
            {"conceptgraphs": preflight["python"]["conceptgraphs"],
             "occusg": preflight["python"]["occusg"]}, model_free=False)
        manifest["adapter_preflight"]["ready_mapper_packets"] = mapper_preflight
        manifest["stages"]["mapper_adapters"] = "completed"
        atomic_json(run_root / "run_manifest.json", manifest)

        # Build a D2 viewer against this replay's own prefix states, not the
        # final room/object geometry from the config-source C2 run.
        viewer = _prepare_viewer(run_root, manifest["scene_id"], manifest["sequence_id"])
        # Bootstrap exactly once, before mapper sessions and before any prefix
        # revision can be published.
        base_index, d2 = _bootstrap_replay_viewer(run_root, viewer, manifest)
        manifest["stages"]["mapper_sessions"] = "starting_one_fresh_session_per_branch"
        atomic_json(run_root / "run_manifest.json", manifest)

        mapper_handles_start = len(handles)
        cg = _spawn_mapper("conceptgraphs", preflight["python"]["conceptgraphs"], c2_root,
            run_root, run_id, bootstrap_id, route_hash, int(preflight["seed"]), handles, processes)
        occ = _spawn_mapper("occusg", preflight["python"]["occusg"], c2_root,
            run_root, run_id, bootstrap_id, route_hash, int(preflight["seed"]), handles, processes)
        worker_groups["mappers"] = {"conceptgraphs": cg, "occusg": occ}
        mapper_ready = _wait_ready_workers(run_root, _mapper_ready_worker_pairs(cg, occ),
            run_id=run_id, bootstrap_id=bootstrap_id, route_config_sha256=route_hash,
            timeout_s=min(300, remaining_s()), marker_kinds={"occusg": "occusg_rooms"})
        manifest["mapper_worker_readiness"] = mapper_ready
        manifest["stages"]["mapper_sessions"] = "running"
        atomic_json(run_root / "run_manifest.json", manifest)
        mapping_peaks: dict[str, int] = {}
        from integration_v2.camera_live_contract import validate_mapper_pair_events
        for ordinal, base in enumerate(raw_packets, start=1):
            index = int(base["frame_index"]); frame_id = str(base["frame_id"])
            packet_path = run_root / "packets" / f"joined-{index:06d}.json"
            packet = _read(packet_path)
            packet_digest = _hash(packet_path)
            message = {"op": "frame", "packet_path": str(packet_path), "packet_sha256": packet_digest}
            started = time.perf_counter(); dispatch_mono = time.monotonic()
            event("mapper_pair_dispatch_started", frame_index=index, frame_id=frame_id,
                  packet_sha256=packet_digest, source_hashes=packet["source_hashes"])
            send(cg, message); send(occ, message)
            object_log = run_root / "branches/objects/events.jsonl"
            room_log = run_root / "branches/rooms/events.jsonl"
            deadline = min(time.monotonic() + 900, run_deadline)
            while time.monotonic() < deadline:
                for name, proc in (("conceptgraphs", cg), ("occusg", occ)):
                    value = _rss_bytes(proc.pid)
                    if value is not None:
                        mapping_peaks[name] = max(mapping_peaks.get(name, 0), value)
                object_events = [json.loads(x) for x in object_log.read_text().splitlines() if x.strip()] if object_log.is_file() else []
                room_events = [json.loads(x) for x in room_log.read_text().splitlines() if x.strip()] if room_log.is_file() else []
                if len(object_events) >= ordinal and len(room_events) >= ordinal:
                    object_event, room_event = object_events[ordinal - 1], room_events[ordinal - 1]
                    if (int(object_event["source_frame_index"]) != index
                            or str(object_event["source_frame_id"]) != frame_id
                            or int(room_event["source_frame_index"]) != index
                            or str(room_event["source_frame_id"]) != frame_id
                            or object_event.get("source_hashes") != packet.get("source_hashes")
                            or room_event.get("source_hashes") != packet.get("source_hashes")):
                        raise ValueError(f"mapper pair identity/provenance mismatch at {frame_id}")
                    validate_mapper_pair_events(packet, object_event, room_event)
                    _verify_event(run_root, object_event, "objects")
                    _verify_event(run_root, room_event, "rooms")
                    frames = [json.loads(x) for x in (run_root / "joined/frames.jsonl").read_text().splitlines() if x.strip()] if (run_root / "joined/frames.jsonl").is_file() else []
                    _join_one(run_root, viewer, base_index, frames, object_event, room_event)
                    frame_record = dict(frames[-1])
                    frame_record["branch_source_hashes"] = {"objects": object_event["source_hashes"],
                                                            "rooms": room_event["source_hashes"]}
                    append_jsonl(run_root / "joined/frames.jsonl", frame_record)
                    for branch, branch_event in (("objects", object_event), ("rooms", room_event)):
                        atomic_json(run_root / "branches" / branch / "acks" / f"{int(branch_event['event_seq']):06d}.json",
                            {"schema": "t2-d-branch-ack/v1", "event_sha256": branch_event["event_sha256"],
                             "joined_revision": ordinal, "durable": True})
                    _write_live_index(run_root, viewer, base_index,
                        [json.loads(x) for x in (run_root / "joined/frames.jsonl").read_text().splitlines() if x.strip()],
                        "running")
                    timings["mapper_frames"].append({"ordinal": ordinal, "frame_index": index,
                        "frame_id": frame_id, "dispatch_monotonic_s": dispatch_mono,
                        "committed_monotonic_s": time.monotonic(), "clock_domain": host_monotonic_domain(),
                        "pair_wait_wall_s": time.perf_counter() - started,
                        "conceptgraphs_processing_ms": object_event.get("processing_ms"),
                        "occusg_processing_ms": room_event.get("processing_ms"),
                        "object_revision": object_event.get("source_revision"),
                        "room_revision": room_event.get("source_revision")})
                    event("unified_revision_committed", ordinal=ordinal, frame_index=index,
                          frame_id=frame_id, object_event_seq=object_event.get("event_seq"),
                          room_event_seq=room_event.get("event_seq"))
                    manifest["unified_prefix_revisions"] = ordinal
                    manifest["last_committed_frame"] = {"frame_index": index, "frame_id": frame_id}
                    atomic_json(run_root / "run_manifest.json", manifest)
                    break
                if cg.poll() not in (None, 0) or occ.poll() not in (None, 0):
                    raise RuntimeError(f"mapper process exited before paired commit at {frame_id}: cg={cg.poll()} occ={occ.poll()}")
                time.sleep(.05)
            else:
                raise TimeoutError(f"paired mapper barrier timed out at {frame_id}")
        terminal_started = time.perf_counter()
        remaining_s()
        send(cg, {"op": "finalize"}); send(occ, {"op": "finalize"})
        for proc in (cg, occ):
            proc.wait(timeout=min(900, remaining_s()))
        if cg.returncode != 0 or occ.returncode != 0:
            raise RuntimeError(f"terminal mapper workers failed: ConceptGraphs={cg.returncode}, OccuSG={occ.returncode}")
        timings["terminal_s"] = time.perf_counter() - terminal_started
        manifest["mapping_peak_rss_bytes_by_worker"] = mapping_peaks
        manifest["mapper_worker_exit_codes"] = {"conceptgraphs": cg.returncode, "occusg": occ.returncode}
        manifest["stages"].update({"mapper_sessions": "completed", "terminal": "completed"})
        manifest["terminal_outputs"] = {
            "conceptgraphs": _read(run_root / "branches/objects/terminal.json"),
            "occusg": _read(run_root / "branches/rooms/terminal.json")}
        _write_live_index(run_root, viewer, base_index,
            [json.loads(x) for x in (run_root / "joined/frames.jsonl").read_text().splitlines() if x.strip()],
            "completed", {"status": "terminal", "frames": 13,
                          "conceptgraphs_postprocess": "branches/objects/terminal.json",
                          "occusg_flush": "branches/rooms/terminal.json"})
        manifest["runtime_metrics"] = {"total_wall_s": time.perf_counter() - run_started,
            "source_preflight_s": timings["source_preflight_s"],
            "raw_adapter_preflight_s": timings["raw_adapter_preflight_s"],
            "perception_frames": timings["perception_frames"],
            "mapper_frames": timings["mapper_frames"], "terminal_s": timings["terminal_s"],
            "coordinator_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
            "peak_rss_scope": "separate per-process samples; not summed as host peak"}
        manifest["source_code_sha256"] = {
            rel: _hash(ROOT / rel) for rel in (
                "integration_v2/v8_ordered_replay.py", "integration_v2/camera_live_contract.py",
                "integration_v2/perception_feature_sessions.py", "integration_v2/conceptgraphs_session.py",
                "integration_v2/occusg_session.py", "integration_v2/live_processing.py",
                "scripts/run_camera_live_pilot.py", "scripts/run_t2_d_cached_packet_worker.py",
                "scripts/run_v8_ordered_replay.py",
                "scripts/run_conceptgraphs_cached_hybrid.py", "scripts/build_d2_xyz_viewer.py",
                "scripts/causal_timeline_viewer.html", "scripts/xyz_renderer.js")}
        manifest["perception_summary"] = {"reused_endpoint_frames": sorted(reused),
            "newly_processed_frames": [idx for idx in EXPECTED_INDICES if idx not in reused],
            "p2_model_load_count": 1, "openclip_model_load_count": 1,
            "p3_geometry_model_load_count": "not_applicable"}
        manifest["mapper_execution_counts"] = {"conceptgraphs_sessions": 1, "conceptgraphs_updates": 13,
            "conceptgraphs_terminal_postprocess": 1, "occusg_sessions": 1, "occusg_updates": 13,
            "occusg_terminal_flush": 1, "paired_commits": len(timings["mapper_frames"]), "retries": 0}
        manifest["membership"] = {"status": "unavailable", "reason": "unavailable_elevation_scope"}
        manifest["room_semantics"] = {"status": "unavailable", "reason": "unavailable_membership_scope"}
        manifest["quality_claim"] = "none; object counts are not quality metrics"
        manifest["gui_acceptance"] = "pending; viewer assets generated, no browser verification performed"
        manifest["status"] = "completed"
        manifest["stages"]["unified_revisions"] = "completed"
        manifest["timing_data"] = timings
        _write_integrity(run_root, manifest)
        status = "completed"
        return manifest
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
        manifest["status"] = status
        manifest["error"] = error
        manifest["timing_data"] = timings
        object_markers = list((run_root / "branches/objects/updates").glob("frame-*.json"))
        room_markers = list((run_root / "branches/rooms/updates").glob("frame-*.json"))
        manifest["failure_classification"] = {
            "mapper_mutation_possible": bool(object_markers or room_markers),
            "replay_required": bool(object_markers or room_markers),
            "unified_prefix_revisions": int(manifest.get("unified_prefix_revisions", 0)),
            "retry_count": 0}
        atomic_json(run_root / "run_manifest.json", manifest)
        raise
    finally:
        cleanup = _stop_owned_processes(processes)
        for handle in handles:
            try:
                handle.close()
            except BaseException:
                pass
        if status != "completed":
            manifest["owned_worker_cleanup"] = cleanup
            manifest["status"] = status
            manifest["error"] = error
            try:
                _write_integrity(run_root, manifest)
            except BaseException:
                atomic_json(run_root / "run_manifest.json", manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, default=V8_DEFAULT,
                        help="read-only v8 camera-live run containing the 13 persisted keyframes")
    parser.add_argument("--mapper-config-source", type=Path, default=C2_DEFAULT,
                        help="locked C2 run supplying mapper config/interpreters, not scene state")
    parser.add_argument("--run-id", default="hm3d00802_t1_1_all13_replay_20261019_v1")
    args = parser.parse_args()
    result = run_ordered_replay(source_run=args.source_run,
                                mapper_config_source=args.mapper_config_source,
                                run_id=args.run_id)
    print(json.dumps({"status": result["status"], "run_id": result["run_id"],
                      "run_directory": str(RUNS_ROOT / result["run_id"]),
                      "viewer_entrypoint": result.get("viewer", {}).get("entrypoint"),
                      "prefix_revisions": result.get("unified_prefix_revisions"),
                      "mapper_execution_counts": result.get("mapper_execution_counts"),
                      "quality_claim": result.get("quality_claim")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
