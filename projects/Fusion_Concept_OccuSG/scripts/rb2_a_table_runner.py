#!/usr/bin/env python3
"""Transparent, single-only RB2 smoke/A-table runner.

This file never loads scene, GT, or annotation data. It scores only the locked
11 object strings against the 8 room continuations in the RB2 input package.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.metadata as metadata
import json
import math
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any

try:
    from rb2_execution_control import (
        canonical_sha256, load_category_checkpoint, run_category_units,
        save_category_checkpoint, write_run_state,
    )
except ModuleNotFoundError:
    from scripts.rb2_execution_control import (
        canonical_sha256, load_category_checkpoint, run_category_units,
        save_category_checkpoint, write_run_state,
    )

MODEL_ID = "Qwen/Qwen3-4B-Base"
MODEL_REVISION = "906bfd4b4dc7f14ee4320094d8b41684abff8539"
DIRECT_PINS = {
    "transformers": "4.51.3",
    "tokenizers": "0.21.1",
    "safetensors": "0.5.3",
    "huggingface-hub": "0.30.2",
}
ROOM_VARIANTS = ("mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class EventLog:
    def __init__(self, run_dir: Path):
        self.path = run_dir / "execution.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        row = {"time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
               "event": event, **fields}
        line = json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8", buffering=1) as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(line, flush=True)


def _hash_manifest_files(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    verified: dict[str, dict[str, Any]] = {}
    for relative, expected in sorted(manifest["files"].items()):
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"Missing RB2 input file: {path}")
        if path.stat().st_size != expected["bytes"]:
            raise ValueError(f"RB2 input size mismatch for {relative}")
        actual = sha256_file(path)
        if actual != expected["sha256"]:
            raise ValueError(f"RB2 input hash mismatch for {relative}: {actual}")
        verified[relative] = {"sha256": actual, "bytes": path.stat().st_size}
    return verified


def _read_mem_available_bytes() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("Cannot determine available host RAM; stop before loading Qwen.")


def _find_snapshot(cache_root: Path) -> Path | None:
    snapshot = (cache_root / "models--Qwen--Qwen3-4B-Base" / "snapshots" /
                MODEL_REVISION)
    return snapshot if snapshot.is_dir() else None


def _snapshot_hashes(snapshot: Path) -> dict[str, dict[str, Any]]:
    files = sorted(path for path in snapshot.rglob("*") if path.is_file())
    if not files or snapshot.name != MODEL_REVISION:
        raise RuntimeError(f"Expected cached immutable model snapshot {MODEL_REVISION}: {snapshot}")
    result = {}
    for path in files:
        relative = str(path.relative_to(snapshot))
        result[relative] = {"sha256_local_integrity_only": sha256_file(path),
                            "bytes": path.stat().st_size}
    if not any(name.endswith(".safetensors") for name in result):
        raise RuntimeError("Pinned snapshot has no safetensors weights.")
    return result


def validate_static_inputs(input_root: Path, closure_report: Path) -> dict[str, Any]:
    manifest_path = input_root / "input_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"RB2 data-free input package is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema") != "room-belief-qwen-rb2-input-package/v2" or
            manifest.get("contains_scene_data") or manifest.get("contains_annotations")):
        raise ValueError("Input package is not the approved data-free RB2 v2 bundle.")
    input_hashes = _hash_manifest_files(input_root, manifest)
    lock = json.loads((input_root / "environment_candidate_lock.json").read_text())
    contract = json.loads((input_root / "scoring_contract_v1.json").read_text())
    ontology = json.loads((input_root / "ontology_v1.json").read_text())
    categories_doc = json.loads((input_root / "object_categories_v1.json").read_text())
    if lock["model"]["repo_id"] != MODEL_ID or lock["model"]["immutable_revision"] != MODEL_REVISION:
        raise ValueError("Model identity/revision differs from the approved RB2 lock.")
    categories = categories_doc["category_strings"]
    rooms = ontology["rooms"]
    if len(categories) != 11 or len(set(categories)) != 11:
        raise ValueError("A-table category population must contain 11 unique canonical strings.")
    if len(rooms) != 8 or len({row["id"] for row in rooms}) != 8:
        raise ValueError("A-table ontology must contain 8 unique room labels.")
    if contract["score"]["temperature"] != 1.0 or contract["score"]["epsilon"] != 1e-8:
        raise ValueError("T/epsilon differ from the locked scoring contract.")
    if float(contract["runtime_validation"]["repeatability_tolerance"]["absolute_logprob"]) != 0.002 or float(contract["runtime_validation"]["repeatability_tolerance"]["relative_logprob"]) != 0.0001:
        raise ValueError("Repeatability tolerance differs from the approved lock.")
    closure = json.loads(closure_report.read_text(encoding="utf-8"))
    if closure.get("gate") != "pass":
        raise RuntimeError("RB2 dependency-closure gate is not pass.")
    return {
        "manifest": manifest,
        "input_hashes": input_hashes,
        "input_manifest_sha256": sha256_file(manifest_path),
        "lock": lock,
        "contract": contract,
        "ontology": ontology,
        "categories": categories,
        "rooms": rooms,
        "input_root": input_root,
        "closure_report": closure_report,
        "closure_report_sha256": sha256_file(closure_report),
    }


def validate_static_population(categories: list[str], rooms: list[dict[str, Any]]) -> None:
    if len(categories) != 11:
        raise ValueError("Expected exactly 11 canonical categories.")
    if len(set(categories)) != 11:
        raise ValueError("Expected 11 unique canonical categories.")
    if len(rooms) != 8:
        raise ValueError("Expected exactly 8 unique room labels.")
    if len({room["id"] for room in rooms}) != 8:
        raise ValueError("Expected exactly 8 unique room labels.")


def build_a_tables(
    category_results: dict[str, dict[str, Any]],
    neutral_results: dict[str, dict[str, Any]],
    categories: list[str],
    rooms: list[dict[str, Any]],
    *,
    make_a_row_fn: Any,
    neutral_corrected_score_fn: Any,
    temperature: float,
    epsilon: float,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Assemble the four locked A views from one set of raw token scores."""
    validate_static_population(categories, rooms)
    room_ids = [room["id"] for room in rooms]
    prior = {room_id: 1.0 / len(room_ids) for room_id in room_ids}
    tables: dict[str, list[dict[str, Any]]] = {name: [] for name in ROOM_VARIANTS}
    raw_rows = []
    for category in categories:
        room_scores: dict[str, dict[str, float]] = {}
        for room in rooms:
            room_id = room["id"]
            category_score = category_results[category]["room_scores"][room_id]
            neutral_score = neutral_results[room_id]
            if not all(math.isfinite(float(value)) for value in
                       category_score["continuation_logprobs"] + neutral_score["continuation_logprobs"]):
                raise ValueError(f"Non-finite raw token score for {category}/{room_id}")
            room_scores[room_id] = {
                "mean_no_neutral": float(category_score["mean_logprob"]),
                "sum_no_neutral": float(category_score["sum_logprob"]),
                "mean_neutral": float(neutral_corrected_score_fn(
                    category_score["continuation_logprobs"], neutral_score["continuation_logprobs"], "mean")),
                "sum_neutral": float(neutral_corrected_score_fn(
                    category_score["continuation_logprobs"], neutral_score["continuation_logprobs"], "sum")),
            }
            raw_rows.append({"category": category, "room_id": room_id,
                             "completion": room["prompt_completion"],
                             "category_prefix": category_score,
                             "neutral_prefix": neutral_score})
        for variant in ROOM_VARIANTS:
            a_row = make_a_row_fn({key: room_scores[key][variant] for key in room_ids}, prior,
                                  temperature=temperature, epsilon=epsilon)
            for room_id in room_ids:
                row = {"category": category, "variant": variant, "room_id": room_id,
                       "raw_variant_score": room_scores[room_id][variant],
                       "uncalibrated_normalized_score": a_row["normalized_room_score_uncalibrated"][room_id],
                       "A_log_ratio_uncalibrated": a_row["A_log_ratio_uncalibrated"][room_id]}
                if not all(math.isfinite(float(row[key])) for key in
                           ("raw_variant_score", "uncalibrated_normalized_score", "A_log_ratio_uncalibrated")):
                    raise ValueError(f"Non-finite A value for {category}/{variant}/{room_id}")
                tables[variant].append(row)
    if len(raw_rows) != 88 or any(len(table) != 88 for table in tables.values()):
        raise ValueError("A-table row count mismatch; expected 88 raw pairs and 88 rows per variant.")
    return tables, raw_rows


def _load_torch_and_model(ctx: dict[str, Any], args: argparse.Namespace,
                          log: EventLog) -> dict[str, Any]:
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HOME"] = str(args.hf_home)
    sys.path.insert(0, str(ctx["input_root"]))
    import numpy as np
    import torch
    import transformers
    import tokenizers
    import safetensors
    import huggingface_hub
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
    from huggingface_hub import snapshot_download
    from qwen_room_scorer import (
        neutral_corrected_score,
        neutral_object_prompt,
        object_prompt,
        split_conditional_continuation,
        make_a_row,
    )

    versions = {name: metadata.version(name) for name in DIRECT_PINS}
    if versions != DIRECT_PINS:
        raise RuntimeError(f"Direct locked dependency versions mismatch: {versions}")
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError("Scorer must see exactly one CUDA GPU (physical GPU 0 via CUDA_VISIBLE_DEVICES=0).")
    gpu_name = torch.cuda.get_device_name(0)
    if "T4" not in gpu_name:
        raise RuntimeError(f"Visible GPU 0 is not a Tesla T4: {gpu_name}")
    free_vram, total_vram = torch.cuda.mem_get_info(0)
    ram_available = _read_mem_available_bytes()
    disk_free = shutil.disk_usage("/kaggle/working").free
    if free_vram < 12 * 1024**3 or ram_available < 12 * 1024**3:
        raise RuntimeError("Pre-load resource gate failed: need at least 12 GiB free VRAM and host RAM.")
    snapshot = _find_snapshot(args.hf_home)
    download_seconds = 0.0
    if snapshot is None:
        if disk_free < 10 * 1024**3:
            raise RuntimeError("Pinned model cache absent and less than 10 GiB disk free; stop before download.")
        started = time.perf_counter()
        log.emit("pinned_model_download_started", repo_id=MODEL_ID, revision=MODEL_REVISION,
                 destination=str(args.hf_home), network_access=True)
        snapshot = Path(snapshot_download(
            repo_id=MODEL_ID, revision=MODEL_REVISION, cache_dir=str(args.hf_home),
            allow_patterns=["*.json", "*.safetensors", "*.model", "*.tiktoken",
                            "tokenizer*", "vocab*", "merges*"],
        ))
        download_seconds = time.perf_counter() - started
        log.emit("pinned_model_download_completed", seconds=download_seconds,
                 snapshot=str(snapshot))
    if snapshot.name != MODEL_REVISION:
        raise RuntimeError(f"Resolved snapshot is not the locked immutable revision: {snapshot}")
    model_hashes = _snapshot_hashes(snapshot)
    run_identity_inputs = {
        "model_file_hashes": model_hashes,
        "input_manifest_sha256": ctx["input_manifest_sha256"],
        "input_file_hashes": ctx["input_hashes"],
        "scoring_contract_sha256": sha256_file(ctx["input_root"] / "scoring_contract_v1.json"),
        "ontology_sha256": sha256_file(ctx["input_root"] / "ontology_v1.json"),
        "categories_sha256": sha256_file(ctx["input_root"] / "object_categories_v1.json"),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "execution_control_sha256": sha256_file(Path(__import__("rb2_execution_control").__file__).resolve()),
        "dependency_closure_report_sha256": ctx["closure_report_sha256"],
        "model_revision": MODEL_REVISION,
        "runtime_versions": {name: metadata.version(name) for name in DIRECT_PINS} |
                            {"torch": torch.__version__, "numpy": np.__version__},
        "execution_policy": "single_only",
        "device": "physical_gpu_0_visible_as_cuda:0",
    }
    run_hash = canonical_sha256(run_identity_inputs)
    args.run_dir = args.output_root / ("run_" + run_hash[:16])
    args.run_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.run_dir / "run_identity.json", {
        "run_hash": run_hash, "hash_inputs": run_identity_inputs,
    })
    config = AutoConfig.from_pretrained(str(snapshot), local_files_only=True, trust_remote_code=False)
    tokenizer = AutoTokenizer.from_pretrained(str(snapshot), local_files_only=True, trust_remote_code=False)
    if config.model_type != "qwen3" or config.architectures != ["Qwen3ForCausalLM"]:
        raise RuntimeError("Resolved checkpoint has unexpected architecture/model_type.")
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is None:
        raise RuntimeError("Tokenizer has neither pad nor eos token for locked masking policy.")
    tokenizer_checks = []
    for category in ctx["categories"] + ["object"]:
        prefix = object_prompt(category)
        for room in ctx["rooms"]:
            prefix_ids, target_ids = split_conditional_continuation(
                tokenizer, prefix, room["prompt_completion"])
            tokenizer_checks.append({"category": category, "room_id": room["id"],
                                     "prefix_token_ids": prefix_ids,
                                     "continuation_token_ids": target_ids})
    if len(tokenizer_checks) != 96:
        raise RuntimeError(f"Expected 96 token-boundary checks, got {len(tokenizer_checks)}")
    log.emit("tokenizer_gate_pass", boundary_count=len(tokenizer_checks),
             tokenizer_class=type(tokenizer).__name__, model_type=config.model_type,
             architecture=config.architectures, bos_policy="omitted_v1", eos_policy="not_scored_v1")

    torch.cuda.reset_peak_memory_stats(0)
    load_start = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        str(snapshot), local_files_only=True, trust_remote_code=False,
        use_safetensors=True, torch_dtype=torch.float16,
    )
    model.to("cuda:0")
    model.eval()
    load_seconds = time.perf_counter() - load_start
    devices = sorted({str(parameter.device) for parameter in model.parameters()})
    if devices != ["cuda:0"] or next(model.parameters()).dtype != torch.float16:
        raise RuntimeError(f"Unexpected model placement/dtype: devices={devices}, dtype={next(model.parameters()).dtype}")
    placement = {
        "parameter_devices": devices,
        "parameter_dtype": str(next(model.parameters()).dtype),
        "hf_device_map": getattr(model, "hf_device_map", None),
        "attention_implementation": getattr(config, "_attn_implementation", None),
        "model_load_seconds": load_seconds,
        "runtime_dtype": "float16",
        "score_dtype": "float32",
        "device_map": None,
        "offload": False,
        "quantization": "none",
    }
    log.emit("model_load_pass", **placement,
             gpu_name=gpu_name,
             free_vram_bytes=torch.cuda.mem_get_info(0)[0],
             peak_vram_allocated_bytes=torch.cuda.max_memory_allocated(0),
             peak_vram_reserved_bytes=torch.cuda.max_memory_reserved(0))
    env = {
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "numpy": np.__version__,
        "direct_locked_versions": versions,
        "module_paths": {name: __import__(name).__file__ for name in
                         ("torch", "numpy", "transformers", "tokenizers", "safetensors", "huggingface_hub")},
        "gpu_visible_index": 0,
        "gpu_name": gpu_name,
        "gpu_free_vram_before_load_bytes": free_vram,
        "gpu_total_vram_bytes": total_vram,
        "ram_available_before_load_bytes": ram_available,
        "disk_free_before_load_bytes": disk_free,
        "download_seconds": download_seconds,
        **placement,
    }
    write_json(args.run_dir / "effective_environment.json", env)
    write_json(args.run_dir / "model_tokenizer_provenance.json", {
        "repo_id": MODEL_ID, "immutable_revision": MODEL_REVISION,
        "model_type": config.model_type, "architectures": config.architectures,
        "tokenizer_class": type(tokenizer).__name__, "tokenizer_vocab_size": len(tokenizer),
        "bos_token_id": tokenizer.bos_token_id, "eos_token_id": tokenizer.eos_token_id,
        "pad_token_id": tokenizer.pad_token_id, "tokenizer_boundary_count": len(tokenizer_checks),
        "source_weights": "BF16", "runtime_weights": "FP16",
        "score_dtype": "FP32", "model_file_hashes_local_integrity_only": model_hashes,
        "upstream_checksum_manifest": None,
        "hash_provenance": "local integrity only; no upstream checksum is represented",
    })
    write_json(args.run_dir / "tokenizer_checks.json", tokenizer_checks)
    return {"torch": torch, "numpy": np, "model": model, "tokenizer": tokenizer,
            "config": config, "snapshot": snapshot, "model_hashes": model_hashes,
            "placement": placement, "environment": env, "run_hash": run_hash,
            "run_identity_inputs": run_identity_inputs,
            "make_a_row": make_a_row, "neutral_corrected_score": neutral_corrected_score,
            "object_prompt": object_prompt, "neutral_object_prompt": neutral_object_prompt,
            "split_continuation": split_conditional_continuation,
            "score_cfg": ctx["contract"]["score"]}


def _score_one(ctx: dict[str, Any], prefix: str, completion: str,
               *, keep_target_logits: bool = False) -> tuple[dict[str, Any], Any | None]:
    torch = ctx["torch"]
    prefix_ids, completion_ids = ctx["split_continuation"](ctx["tokenizer"], prefix, completion)
    token_ids = prefix_ids + completion_ids
    input_ids = torch.tensor([token_ids], dtype=torch.long, device="cuda:0")
    attention_mask = torch.ones_like(input_ids)
    start = time.perf_counter()
    with torch.inference_mode():
        logits = ctx["model"](input_ids=input_ids, attention_mask=attention_mask,
                              use_cache=False).logits
        target_logits = logits[0, len(prefix_ids) - 1:len(token_ids) - 1, :].float()
        targets = input_ids[0, len(prefix_ids):]
        if target_logits.shape[0] != len(completion_ids) or len(completion_ids) != targets.numel():
            raise RuntimeError("Causal shift/label-only target span length mismatch.")
        token_logprobs = torch.log_softmax(target_logits, dim=-1).gather(
            1, targets[:, None]).squeeze(1)
        token_values = token_logprobs.detach().float().cpu().tolist()
        if not all(math.isfinite(float(value)) for value in token_values):
            raise RuntimeError("Non-finite token log-probability.")
        values = torch.tensor(token_values, dtype=torch.float32)
        row = {
            "prefix": prefix,
            "completion": completion,
            "prefix_token_ids": prefix_ids,
            "continuation_token_ids": completion_ids,
            "continuation_logprobs": token_values,
            "token_count": len(completion_ids),
            "mean_logprob": float(values.mean().item()),
            "sum_logprob": float(values.sum(dtype=torch.float32).item()),
            "score_dtype": "FP32_log_softmax_and_accumulation",
            "device": str(input_ids.device),
            "forward_seconds": time.perf_counter() - start,
        }
    copied_logits = target_logits.detach().clone() if keep_target_logits else None
    del logits, target_logits, input_ids, attention_mask
    return row, copied_logits


def _tolerance_pass(left: list[float], right: list[float], atol: float, rtol: float) -> tuple[bool, float]:
    if len(left) != len(right):
        return False, math.inf
    maximum = max((abs(float(a) - float(b)) for a, b in zip(left, right)), default=0.0)
    passed = all(abs(float(a) - float(b)) <= atol + rtol * abs(float(b))
                 for a, b in zip(left, right))
    return passed, maximum


def run_s1_s2(ctx: dict[str, Any], run_dir: Path, run_hash: str, log: EventLog) -> dict[str, Any]:
    torch = ctx["torch"]
    category = ctx["categories"][0]
    room_ids = [room["id"] for room in ctx["rooms"]]
    first_room = ctx["rooms"][0]
    tolerance = ctx["contract"]["runtime_validation"]["repeatability_tolerance"]
    atol = float(tolerance["absolute_logprob"])
    rtol = float(tolerance["relative_logprob"])
    stage_start = time.perf_counter()
    log.emit("S1_started", category=category, room_id=first_room["id"],
             execution_policy="single_only", model_revision=MODEL_REVISION)
    first, first_target_logits = _score_one(
        ctx, ctx["object_prompt"](category), first_room["prompt_completion"],
        keep_target_logits=True)
    if first["device"] != "cuda:0":
        raise RuntimeError(f"S1 input device is not cuda:0: {first['device']}")
    log.emit("S1_completed", category=category, room_id=first_room["id"],
             token_ids=first["continuation_token_ids"], token_count=first["token_count"],
             finite=all(math.isfinite(x) for x in first["continuation_logprobs"]),
             mean=first["mean_logprob"], sum=first["sum_logprob"],
             forward_seconds=first["forward_seconds"], device=first["device"],
             model_parameter_devices=ctx["placement"]["parameter_devices"],
             runtime_dtype=ctx["placement"]["runtime_dtype"],
             score_dtype=first["score_dtype"])
    if not all(math.isfinite(x) for x in
               (first["mean_logprob"], first["sum_logprob"], *first["continuation_logprobs"])):
        raise RuntimeError("S1 finite score check failed.")

    log.emit("S2_started", category=category, room_count=len(ctx["rooms"]),
             neutral_count=len(ctx["rooms"]), reuse_s1_pair=True)
    category_scores = {first_room["id"]: first}
    neutral_scores: dict[str, dict[str, Any]] = {}
    forward_calls = 1
    for room in ctx["rooms"][1:]:
        row, _ = _score_one(ctx, ctx["object_prompt"](category), room["prompt_completion"])
        category_scores[room["id"]] = row
        forward_calls += 1
        log.emit("S2_category_room_scored", category=category, room_id=room["id"],
                 forward_seconds=row["forward_seconds"], token_count=row["token_count"],
                 finite=all(math.isfinite(x) for x in row["continuation_logprobs"]))
    for room in ctx["rooms"]:
        row, _ = _score_one(ctx, ctx["neutral_object_prompt"](), room["prompt_completion"])
        neutral_scores[room["id"]] = row
        forward_calls += 1
        log.emit("S2_neutral_room_scored", room_id=room["id"],
                 forward_seconds=row["forward_seconds"], token_count=row["token_count"],
                 finite=all(math.isfinite(x) for x in row["continuation_logprobs"]))
    # Repeat the first candidate after interleaved room/neutral calls.
    repeated, _ = _score_one(ctx, ctx["object_prompt"](category), first_room["prompt_completion"])
    forward_calls += 1
    repeat_pass, repeat_max_delta = _tolerance_pass(
        first["continuation_logprobs"], repeated["continuation_logprobs"], atol, rtol)

    # Independent CE reduction over only completion targets, using the S1 logits.
    prefix_ids = first["prefix_token_ids"]
    continuation_ids = first["continuation_token_ids"]
    targets = torch.tensor(continuation_ids, dtype=torch.long, device="cuda:0")
    if first_target_logits is None or first_target_logits.shape[0] != targets.numel():
        raise RuntimeError("S2 masked-CE target span does not match completion tokens.")
    selected_logprobs = torch.log_softmax(first_target_logits.float(), dim=-1)
    gathered = selected_logprobs.gather(1, targets[:, None]).squeeze(1)
    gather_mean = -gathered.mean()
    cross_entropy_mean = torch.nn.functional.cross_entropy(
        first_target_logits.float(), targets, reduction="mean")
    ce_delta = float(torch.abs(gather_mean - cross_entropy_mean).item())
    ce_pass = bool(torch.allclose(gather_mean, cross_entropy_mean, atol=1e-6, rtol=1e-6))
    # Causal shift is explicitly checked against the unpadded input target range.
    target_positions = list(range(len(prefix_ids), len(prefix_ids) + len(continuation_ids)))
    prediction_positions = list(range(len(prefix_ids) - 1, len(prefix_ids) + len(continuation_ids) - 1))
    causal_pass = (len(target_positions) == len(continuation_ids) == len(prediction_positions) and
                   target_positions[0] == len(prefix_ids) and
                   prediction_positions[0] == len(prefix_ids) - 1)
    label_only_pass = first_target_logits.shape[0] == len(continuation_ids) and len(prefix_ids) > 0
    finite_pass = all(math.isfinite(float(value)) for mapping in
                      (category_scores, neutral_scores) for row in mapping.values()
                      for value in row["continuation_logprobs"])
    measured_forward_seconds = [first["forward_seconds"],
                                *(row["forward_seconds"] for room_id, row in category_scores.items()
                                  if room_id != first_room["id"]),
                                *(row["forward_seconds"] for row in neutral_scores.values()),
                                repeated["forward_seconds"]]
    mean_forward_seconds = sum(measured_forward_seconds) / len(measured_forward_seconds)
    estimated_full_a_seconds = (float(ctx["placement"]["model_load_seconds"]) +
                                mean_forward_seconds * 80 + 60.0)
    gate_pass = bool(repeat_pass and ce_pass and causal_pass and label_only_pass and finite_pass)
    if not gate_pass:
        raise RuntimeError("S2 scoring gate failed; see s1_s2_report.json; locked tolerance was not changed.")

    # Save S2 category and neutral score payloads under exact provenance-bound run hash.
    save_category_checkpoint(run_dir, run_hash=run_hash, category=category,
                             payload={"room_scores": category_scores})
    save_category_checkpoint(run_dir, run_hash=run_hash, category="__neutral__",
                             payload={"room_scores": neutral_scores})
    report = {
        "schema": "rb2-s1-s2-gate/v1",
        "run_hash": run_hash,
        "status": "passed",
        "s1": {"category": category, "room_id": first_room["id"],
               "token_ids": first["continuation_token_ids"], "token_count": first["token_count"],
               "mean_logprob": first["mean_logprob"], "sum_logprob": first["sum_logprob"],
               "finite": finite_pass, "forward_seconds": first["forward_seconds"],
               "device": first["device"]},
        "s2": {"category": category, "conditional_room_scores": len(category_scores),
               "neutral_room_scores": len(neutral_scores), "total_forward_calls_including_repeated_sample": forward_calls,
               "s1_score_reused": True, "single_only": True,
               "batch_parity": "failed/unsupported from previous measured run; not invoked as a gate",
               "repeatability": {"pass": repeat_pass, "max_abs_logprob_delta": repeat_max_delta,
                                 "locked_tolerance": tolerance},
               "gather_vs_masked_cross_entropy": {"pass": ce_pass, "absolute_difference": ce_delta,
                                                   "tolerance": {"atol": 1e-6, "rtol": 1e-6}},
               "causal_shift_pass": causal_pass, "label_only_span_pass": label_only_pass,
               "finite_scores_pass": finite_pass,
               "forward_calls": forward_calls,
               "mean_observed_forward_seconds": mean_forward_seconds,
               "full_a_estimated_seconds_excluding_checkpoint_hashing": estimated_full_a_seconds,
               "full_a_estimate_method": "one model load + 80 remaining category-room forwards at observed S2 mean + 60s serialization/checkpoint allowance; bed category and 8 neutral rows reused",
               "seconds": time.perf_counter() - stage_start},
        "full_a_not_started": True,
        "source_weights": "BF16", "runtime_weights": "FP16", "score_dtype": "FP32",
        "device_placement": ctx["placement"],
    }
    write_json(run_dir / "s1_s2_report.json", report)
    write_json(run_dir / "s1_s2_raw_token_scores.json", {
        "category": category, "category_room_scores": category_scores,
        "neutral_room_scores": neutral_scores,
        "repeated_first_candidate": repeated,
    })
    log.emit("S2_gate_pass", category=category, room_scores=len(category_scores),
             neutral_scores=len(neutral_scores), forward_calls=forward_calls,
             total_seconds=report["s2"]["seconds"], repeatability_max_abs_delta=repeat_max_delta,
             masked_ce_abs_delta=ce_delta, full_a_estimated_seconds=estimated_full_a_seconds,
             full_a_not_started=True)
    return report


def _write_full_outputs(ctx: dict[str, Any], run_dir: Path, results: dict[str, dict[str, Any]],
                        neutral_scores: dict[str, Any], run_hash: str,
                        log: EventLog, started_at: float) -> dict[str, Any]:
    from qwen_room_scorer import make_a_row, neutral_corrected_score
    tables, raw_rows = build_a_tables(
        results, neutral_scores, ctx["categories"], ctx["rooms"],
        make_a_row_fn=make_a_row, neutral_corrected_score_fn=neutral_corrected_score,
        temperature=float(ctx["contract"]["score"]["temperature"]),
        epsilon=float(ctx["contract"]["score"]["epsilon"]),
    )
    output_dir = run_dir / "full_a"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "raw_token_scores.json", raw_rows)
    write_json(output_dir / "neutral_token_scores.json", neutral_scores)
    scores_by_variant = {}
    for variant, rows in tables.items():
        scores_by_variant[variant] = {}
        for row in rows:
            scores_by_variant[variant].setdefault(row["category"], {})[row["room_id"]] = row["raw_variant_score"]
    write_json(output_dir / "category_room_scores_by_variant.json", scores_by_variant)
    for variant, rows in tables.items():
        write_json(output_dir / f"a_table_{variant}.json", rows)
        with (output_dir / f"a_table_{variant}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    write_json(output_dir / "a_tables_all_variants.json", [row for variant in ROOM_VARIANTS
                                                             for row in tables[variant]])
    outputs = {}
    for path in sorted(output_dir.iterdir()):
        if path.is_file():
            outputs[path.name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    effective_config = {
        "scoring_contract_id": ctx["contract"]["scoring_contract_id"],
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "category_strings": ctx["categories"],
        "room_labels": [{"id": room["id"], "completion": room["prompt_completion"]}
                        for room in ctx["rooms"]],
        "variants": list(ROOM_VARIANTS),
        "temperature": float(ctx["contract"]["score"]["temperature"]),
        "epsilon": float(ctx["contract"]["score"]["epsilon"]),
        "prior": "uniform_over_eight_ontology_room_labels",
        "execution_policy": "single_only",
        "upstream_weights": "BF16",
        "runtime_weights": "FP16",
        "score_dtype": "FP32",
        "bos_policy": "omitted_v1",
        "eos_policy": "not_scored_v1",
        "batch_parity": "failed/unsupported; not used",
        "selection_claim": "no development winner selected",
        "source_and_config_hashes": json.loads((run_dir / "run_identity.json").read_text())["hash_inputs"],
    }
    write_json(run_dir / "effective_scoring_config.json", effective_config)
    report = {
        "schema": "room-belief-qwen-rb2-a-table/v3",
        "status": "full_a_complete",
        "run_hash": run_hash,
        "provenance": "Qwen3-4B-Base pinned checkpoint; scoring-only; no capture/GT/annotations",
        "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
        "input_manifest_sha256": ctx["input_manifest_sha256"],
        "category_count": len(ctx["categories"]), "room_count": len(ctx["rooms"]),
        "raw_pair_count": len(raw_rows),
        "rows_per_variant": {key: len(value) for key, value in tables.items()},
        "execution_policy": "single_only",
        "batch_parity": "failed/unsupported; retained as a separate diagnostic",
        "runtime_seconds_full_stage": time.perf_counter() - started_at,
        "cpu_peak_rss_bytes": int(__import__("resource").getrusage(__import__("resource").RUSAGE_SELF).ru_maxrss * 1024),
        "peak_vram_allocated_bytes": int(ctx["torch"].cuda.max_memory_allocated(0)),
        "peak_vram_reserved_bytes": int(ctx["torch"].cuda.max_memory_reserved(0)),
        "gpu_free_bytes_after": int(ctx["torch"].cuda.mem_get_info(0)[0]),
        "source_weights": "BF16", "runtime_weights": "FP16", "score_dtype": "FP32",
        "temperature": float(ctx["contract"]["score"]["temperature"]),
        "epsilon": float(ctx["contract"]["score"]["epsilon"]),
        "variant_policy": list(ROOM_VARIANTS),
        "candidate_status": "mean_neutral is a candidate only; no development winner selected",
        "output_files": outputs,
    }
    write_json(run_dir / "manifest.json", report)
    log.emit("full_a_complete", **{key: report[key] for key in
             ("category_count", "room_count", "raw_pair_count", "rows_per_variant",
              "runtime_seconds_full_stage", "peak_vram_allocated_bytes", "cpu_peak_rss_bytes")})
    write_run_state(run_dir, run_hash=run_hash, status="completed", stage="full_a_complete",
                    details={"manifest": str(run_dir / "manifest.json")})
    import zipfile
    archive_path = run_dir.parent / (run_dir.name + "_artifacts.zip")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(run_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(run_dir.parent))
    archive_manifest = {
        "status": "full_a_complete",
        "run_hash": run_hash,
        "archive": archive_path.name,
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": sha256_file(archive_path),
        "manifest_sha256": sha256_file(run_dir / "manifest.json"),
    }
    write_json(run_dir.parent / (run_dir.name + "_download_package_manifest.json"), archive_manifest)
    log.emit("download_package_ready", **archive_manifest)
    return report


def run_full(ctx: dict[str, Any], run_dir: Path, run_hash: str, log: EventLog) -> dict[str, Any]:
    gate_path = run_dir / "s1_s2_report.json"
    if not gate_path.is_file():
        raise RuntimeError("Full A blocked: S1/S2 report is missing.")
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    if gate.get("status") != "passed" or gate.get("run_hash") != run_hash or gate.get("full_a_not_started") is not True:
        raise RuntimeError("Full A blocked: S1/S2 gate/hash does not match this exact run.")
    neutral_checkpoint = load_category_checkpoint(run_dir, run_hash=run_hash, category="__neutral__")
    if not neutral_checkpoint or len(neutral_checkpoint.get("room_scores", {})) != 8:
        raise RuntimeError("Full A blocked: validated S2 neutral scores are incomplete.")
    started_at = time.perf_counter()
    log.emit("full_a_started", category_count=11, room_count=8, expected_new_category_forwards=80,
             neutral_scores_reused=8, smoke_category_reused=True, single_only=True,
             model_loads_in_this_stage=1)

    def score_category(category: str) -> dict[str, Any]:
        scores = {}
        for room in ctx["rooms"]:
            score_start = time.perf_counter()
            row, _ = _score_one(ctx, ctx["object_prompt"](category), room["prompt_completion"])
            scores[room["id"]] = row
            log.emit("category_room_score", category=category, room_id=room["id"],
                     seconds=time.perf_counter() - score_start,
                     token_count=row["token_count"], mean=row["mean_logprob"],
                     sum=row["sum_logprob"])
        return {"room_scores": scores}

    results = run_category_units(
        run_dir, run_hash=run_hash, categories=ctx["categories"],
        score_category=score_category,
        on_progress=lambda line: print("[category-progress] " + line, flush=True),
    )
    return _write_full_outputs(ctx, run_dir, results, neutral_checkpoint["room_scores"],
                               run_hash, log, started_at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("s1-s2", "full"), required=True,
                        help="Run only the requested stage; s1-s2 never falls through to full A.")
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--closure-report", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--hf-home", type=Path, default=Path("/kaggle/working/qwen_rb2_hf_cache_v2"))
    args = parser.parse_args(argv)
    args.input_root = args.input_root.resolve()
    args.closure_report = args.closure_report.resolve()
    args.output_root = args.output_root.resolve()
    args.hf_home = args.hf_home.resolve()
    provisional_dir = args.output_root / "preflight"
    log = EventLog(provisional_dir)
    write_run_state(provisional_dir, run_hash="pending", status="started", stage=args.stage,
                    details={"input_root": str(args.input_root), "stage": args.stage})
    try:
        ctx = validate_static_inputs(args.input_root, args.closure_report)
        log.emit("static_inputs_pass", input_manifest_sha256=ctx["input_manifest_sha256"],
                 categories=len(ctx["categories"]), rooms=len(ctx["rooms"]),
                 closure_report_sha256=ctx["closure_report_sha256"])
        # _load_torch_and_model computes the identity before choosing its actual run directory.
        context = _load_torch_and_model(ctx, args, log)
        run_dir = args.run_dir
        run_hash = context["run_hash"]
        log = EventLog(run_dir)
        log.emit("run_identity_resolved", run_hash=run_hash, stage=args.stage,
                 run_dir=str(run_dir), model_loads_this_stage=1)
        if args.stage == "s1-s2":
            write_run_state(run_dir, run_hash=run_hash, status="started", stage="s1-s2")
            report = run_s1_s2(ctx | context, run_dir, run_hash, log)
            write_run_state(run_dir, run_hash=run_hash, status="completed", stage="s1-s2_passed",
                            details={"report": str(run_dir / "s1_s2_report.json"),
                                     "full_a_started": False})
            print(json.dumps({"stage": "s1-s2", "status": "passed",
                              "full_a_started": False, "run_dir": str(run_dir),
                              "run_hash": run_hash,
                              "s1_s2_report": str(run_dir / "s1_s2_report.json"),
                              "s2_seconds": report["s2"]["seconds"]}, indent=2), flush=True)
            return 0
        write_run_state(run_dir, run_hash=run_hash, status="started", stage="full_a")
        report = run_full(ctx | context, run_dir, run_hash, log)
        print(json.dumps({"stage": "full", "status": report["status"],
                          "run_dir": str(run_dir), "manifest": str(run_dir / "manifest.json"),
                          "rows_per_variant": report["rows_per_variant"]}, indent=2), flush=True)
        return 0
    except BaseException as exc:
        active_dir = getattr(args, "run_dir", provisional_dir)
        active_context = locals().get("context", {})
        active_hash = active_context.get("run_hash", "pending")
        failure_path = active_dir / "failure.json"
        write_json(failure_path, {"error_type": type(exc).__name__, "error": str(exc),
                                  "stage": args.stage})
        write_run_state(active_dir, run_hash=active_hash, status="failed", stage=args.stage,
                        details={"error_type": type(exc).__name__, "error": str(exc)})
        log.emit("stage_failed", error_type=type(exc).__name__, error=str(exc), stage=args.stage)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
