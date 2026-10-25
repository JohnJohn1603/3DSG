#!/usr/bin/env python3
"""Build the small, data-free Kaggle input package and gated RB2 notebook."""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = ROOT / "artifacts/room_belief/rb2_qwen_inputs_v2"
NOTEBOOK = ROOT / "notebooks/room_belief_qwen3_rb2_v2.ipynb"
ZIP_PATH = ROOT / "artifacts/room_belief/room_belief_qwen_rb2_inputs_v2.zip"
SOURCE_FILES = {
    "ontology_v1.json": ROOT / "configs/room_semantics/ontology_v1.json",
    "object_categories_v1.json": ROOT / "configs/room_semantics/object_categories_u6_predictor_v1.json",
    "scoring_contract_v1.json": ROOT / "configs/room_semantics/qwen_room_scoring_v1.proposal.json",
    "environment_candidate_lock.json": ROOT / "locks/room_semantics/qwen3_4b_base_rb2_candidate.lock.json",
    "qwen_room_scorer.py": ROOT / "src/standalone_sg/semantics/qwen_room_scorer.py",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md(text: str) -> dict[str, object]:
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)}


def code(text: str) -> dict[str, object]:
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": text.splitlines(keepends=True)}


CELLS = [
md("""# RB2 — Qwen3-4B-Base room A-table scorer (approved FP16 T4 run)

This notebook is prepared for the approved RB2 run. It scores only the eight versioned room-label continuations for the eleven canonical room-relevant object strings. Inputs contain no HM3D frames, masks, GT, room labels, or annotations.

Run cells in order and stop at the stated checkpoints; do not run the full A export until the smoke output passes. The dependency install, pinned model download, model load, and scoring smoke gates are enabled for this approved run. The full A export remains gated until smoke passes. Model revision is pinned; a local SHA computed after download establishes local file integrity only because no upstream checksum manifest is recorded here.

The upstream checkpoint is BF16. With explicit approval, the same pinned checkpoint is loaded as FP16 on one Tesla T4 (GPU 0). No quantization, offload, or multi-GPU split is used. Log-softmax and score accumulation are FP32.

Output scores and A rows are uncalibrated LM compatibility scores, not probabilities that a room label is correct. Local scorer unit tests do not establish Qwen, tokenizer, or Kaggle runtime parity.
"""),
    md("""## Before attaching this notebook

1. Upload `artifacts/room_belief/room_belief_qwen_rb2_inputs_v2.zip` as a **private Kaggle Dataset** containing only the files in the package. Do not attach capture, room snapshots, review CSVs, or the U6 cache.
2. Attach that dataset to a **private Kaggle Notebook** with slug `room-belief-qwen-rb2-v2` (or edit `INPUT_MOUNT` in cell B to Kaggle's actual mount path).
3. Enable internet for the pinned model download cell only; it uses the official Hugging Face repository and immutable revision in the lock. Keep output private.
4. This input package contains code, ontology, categories, contracts, and locks only; it is not a perception dataset.
"""),
    code("""# A — read-only environment probe; no install, network, model, or input data access
import json, os, platform, shutil, sys
from pathlib import Path

def read_meminfo_kib(key):
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith(key + ':'):
                return int(line.split()[1])
    except OSError:
        pass
    return None

probe = {
    'python': sys.version,
    'python_executable': sys.executable,
    'platform': platform.platform(),
    'ram_total_kib': read_meminfo_kib('MemTotal'),
    'ram_available_kib': read_meminfo_kib('MemAvailable'),
    'working_disk': shutil.disk_usage('/kaggle/working')._asdict(),
    'gpu': [],
}
try:
    import torch
    probe['torch'] = torch.__version__
    probe['torch_cuda_build'] = torch.version.cuda
    probe['cuda_available'] = torch.cuda.is_available()
    for i in range(torch.cuda.device_count()):
        free, total = torch.cuda.mem_get_info(i)
        probe['gpu'].append({'index': i, 'name': torch.cuda.get_device_name(i), 'free_bytes': free, 'total_bytes': total, 'bf16_supported': torch.cuda.is_bf16_supported()})
except Exception as exc:
    probe['torch_probe_error'] = repr(exc)
for package in ('numpy', 'transformers', 'tokenizers', 'safetensors', 'huggingface_hub'):
    try:
        module = __import__(package)
        probe[package] = getattr(module, '__version__', 'imported_no_version')
    except Exception as exc:
        probe[package] = 'missing_or_import_error: ' + type(exc).__name__
print(json.dumps(probe, indent=2))
"""),
    code("""# B — verify the data-free input bundle and load its small metadata/code
import hashlib, importlib.util, json, zipfile
from pathlib import Path
import sys

INPUT_MOUNT = Path('/kaggle/input/room-belief-qwen-rb2-v2')
EXPECTED_INPUT_ZIP_SHA256 = '__INPUT_ZIP_SHA256__'
if not INPUT_MOUNT.is_dir():
    raise FileNotFoundError(f'Attach the private RB2 input dataset or update INPUT_MOUNT: {INPUT_MOUNT}')
if (INPUT_MOUNT / 'input_manifest.json').is_file():
    INPUT_ROOT = INPUT_MOUNT  # Kaggle dataset contains the extracted package files
else:
    zip_files = list(INPUT_MOUNT.glob('room_belief_qwen_rb2_inputs_v2.zip'))
    if len(zip_files) != 1:
        raise FileNotFoundError(f'Expected either input_manifest.json or the one prepared ZIP under {INPUT_MOUNT}')
    zip_path = zip_files[0]
    archive_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if archive_sha != EXPECTED_INPUT_ZIP_SHA256:
        raise ValueError(f'RB2 input ZIP SHA-256 mismatch: {archive_sha}')
    INPUT_ROOT = Path('/kaggle/working/room_belief_rb2_inputs_v2')
    with zipfile.ZipFile(zip_path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise ValueError(f'Corrupt ZIP member: {corrupt}')
        for member in archive.namelist():
            member_path = Path(member)
            if member_path.is_absolute() or '..' in member_path.parts:
                raise ValueError(f'Unsafe ZIP member path: {member}')
        if INPUT_ROOT.exists():
            if not (INPUT_ROOT / 'input_manifest.json').is_file():
                raise FileExistsError(f'Incomplete extraction directory already exists; inspect or reset it manually: {INPUT_ROOT}')
        else:
            archive.extractall(INPUT_ROOT)
manifest_path = INPUT_ROOT / 'input_manifest.json'
input_manifest = json.loads(manifest_path.read_text())
for name, expected in input_manifest['files'].items():
    path = INPUT_ROOT / name
    if not path.is_file():
        raise FileNotFoundError(f'Missing packaged input: {path}')
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected['sha256']:
        raise ValueError(f'Input hash mismatch for {name}: {actual}')
print('Input file hashes verified; no archive hash applies because Kaggle mounts extracted dataset files.')
ontology = json.loads((INPUT_ROOT / 'ontology_v1.json').read_text())
categories_doc = json.loads((INPUT_ROOT / 'object_categories_v1.json').read_text())
score_contract = json.loads((INPUT_ROOT / 'scoring_contract_v1.json').read_text())
env_lock = json.loads((INPUT_ROOT / 'environment_candidate_lock.json').read_text())
room_rows = ontology['rooms']
room_ids = [row['id'] for row in room_rows]
category_strings = categories_doc['category_strings']
assert len(room_rows) == 8 and len(set(room_ids)) == 8
assert category_strings and len(category_strings) == len(set(category_strings))
assert not any(key in categories_doc for key in ('gt_labels','ground_truth','annotations','room_labels'))
spec = importlib.util.spec_from_file_location('qwen_room_scorer', INPUT_ROOT / 'qwen_room_scorer.py')
qwen_room_scorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qwen_room_scorer)
sys.path.insert(0, str(INPUT_ROOT))
print(json.dumps({'ontology_id': ontology['ontology_id'], 'room_count': len(room_rows), 'category_count': len(category_strings), 'categories': category_strings, 'scorer_loaded': True, 'model_revision': env_lock['model']['immutable_revision']}, indent=2))
"""),
    md("""## C — install the four locked direct dependencies

This is the only package-install cell. It preserves Kaggle's Python, Torch, and CUDA, installs only the four direct pins with `--no-deps`, then runs imports and `pip check`. A missing transitive dependency is a stop condition; do not widen the install speculatively.
"""),
code("""import subprocess
ALLOW_DEPENDENCY_INSTALL = True  # user-approved RB2 package installation
if ALLOW_DEPENDENCY_INSTALL:
    import subprocess, sys
    if 'probe' not in globals():
        raise RuntimeError('Run cell A first; dependency installation is blocked before resource preflight.')
    gpu0 = next((gpu for gpu in probe.get('gpu', []) if gpu.get('index') == 0), None)
    if not probe.get('cuda_available') or gpu0 is None or 'T4' not in gpu0.get('name', ''):
        raise RuntimeError(f'Expected a visible Tesla T4 at GPU 0; stop: {probe.get("gpu")}')
    if gpu0.get('free_bytes', 0) < 12 * 1024**3:
        raise RuntimeError('Install/model setup stopped: GPU 0 has less than 12 GiB free VRAM.')
    if (probe.get('ram_available_kib') or 0) * 1024 < 12 * 1024**3:
        raise RuntimeError('Install/model setup stopped: less than 12 GiB host RAM is available.')
    if probe.get('working_disk', {}).get('free', 0) < 10 * 1024**3:
        raise RuntimeError('Install/model setup stopped: less than 10 GiB free working disk.')
    pins = ['transformers==4.51.3', 'tokenizers==0.21.1', 'safetensors==0.5.3', 'huggingface_hub==0.30.2']
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', *pins], check=True)
else:
    print('Skipped: no package installation or network access requested.')

from importlib.metadata import PackageNotFoundError, version
expected = {'transformers':'4.51.3','tokenizers':'0.21.1','safetensors':'0.5.3','huggingface_hub':'0.30.2'}
actual = {}
for package, wanted in expected.items():
    try:
        actual[package] = version(package.replace('_', '-'))
    except PackageNotFoundError:
        actual[package] = None
if all(actual[p] == v for p, v in expected.items()):
    import transformers, tokenizers, safetensors, huggingface_hub
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
    print('Direct versions and pip check pass; model download/load and GPU scoring remain unverified.')
else:
    raise RuntimeError(f'Locked direct dependencies are not installed: {actual}')
"""),
    md("""## D — download the pinned official checkpoint and tokenizer

This cell accesses the network and downloads only the exact immutable Hugging Face revision in the lock. It requires internet enabled and at least 10 GiB free working disk. Local SHA-256 values are recorded for integrity checks, not presented as upstream-authenticated checksums.
"""),
    code("""ALLOW_PINNED_MODEL_DOWNLOAD = True  # user-approved; official model repo and immutable revision only
MODEL_ID = env_lock['model']['repo_id']
MODEL_REVISION = env_lock['model']['immutable_revision']
if not ALLOW_PINNED_MODEL_DOWNLOAD:
    raise RuntimeError('Pinned checkpoint download gate is off; no model files downloaded.')
import hashlib, shutil
from huggingface_hub import snapshot_download
if shutil.disk_usage('/kaggle/working').free < 10 * 1024**3:
    raise RuntimeError('Model download stopped: less than 10 GiB free on /kaggle/working.')
MODEL_SNAPSHOT = Path(snapshot_download(
    repo_id=MODEL_ID,
    revision=MODEL_REVISION,
    allow_patterns=['*.json', '*.safetensors', '*.model', '*.tiktoken', 'tokenizer*', 'vocab*', 'merges*'],
))
if MODEL_SNAPSHOT.name != MODEL_REVISION:
    raise RuntimeError(f'Unexpected model snapshot revision directory: {MODEL_SNAPSHOT}')
CACHED_MODEL_FILE_HASHES = {}
for file_path in sorted(MODEL_SNAPSHOT.rglob('*')):
    if file_path.is_file():
        digest = hashlib.sha256()
        with file_path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024), b''):
                digest.update(chunk)
        CACHED_MODEL_FILE_HASHES[str(file_path.relative_to(MODEL_SNAPSHOT))] = {
            'sha256_local_integrity_only':digest.hexdigest(),'bytes':file_path.stat().st_size}
print(json.dumps({'model_id':MODEL_ID,'resolved_revision':MODEL_SNAPSHOT.name,'upstream_tensor_type':'BF16','downloaded_snapshot':str(MODEL_SNAPSHOT),'downloaded_files':CACHED_MODEL_FILE_HASHES,'expected_upstream_checksum_manifest':None,'hash_provenance':'local SHA-256 only; not upstream-authenticated'}, indent=2))
"""),
    md("""## E — local config and tokenizer checks (no network)

Verify the downloaded snapshot, tokenizer boundaries for all 11 categories plus neutral, and exact leading-space continuation strings. Prefix/completion tokenization disables automatic BOS/EOS; EOS is not a scored target. This cell does not load weights or score labels.
"""),
    code("""from transformers import AutoConfig, AutoTokenizer
if MODEL_SNAPSHOT.name != MODEL_REVISION:
    raise RuntimeError('Tokenizer check stopped: downloaded snapshot does not match pinned revision.')
config = AutoConfig.from_pretrained(str(MODEL_SNAPSHOT), local_files_only=True, trust_remote_code=False)
tokenizer = AutoTokenizer.from_pretrained(str(MODEL_SNAPSHOT), local_files_only=True, trust_remote_code=False)
assert config.model_type == 'qwen3'
assert config.architectures == ['Qwen3ForCausalLM']
if any(not row['prompt_completion'].startswith(' ') for row in room_rows):
    raise RuntimeError('Ontology continuation lost its leading space.')
all_categories = category_strings + ['object']
TOKENIZER_CHECKS = []
for category in all_categories:
    prefix = qwen_room_scorer.object_prompt(category)
    for room in room_rows:
        prefix_ids, completion_ids = qwen_room_scorer.split_conditional_continuation(
            tokenizer, prefix, room['prompt_completion'])
        TOKENIZER_CHECKS.append({'category':category,'room_id':room['id'],
                                 'prefix_token_count':len(prefix_ids),
                                 'continuation_token_ids':completion_ids,
                                 'leading_space':room['prompt_completion'].startswith(' ')})
if len(TOKENIZER_CHECKS) != 96:
    raise RuntimeError(f'Expected 96 exact tokenizer boundaries, got {len(TOKENIZER_CHECKS)}')
print(json.dumps({'snapshot_revision':MODEL_SNAPSHOT.name,'config_model_type':config.model_type,
                  'architecture':config.architectures,'tokenizer_class':type(tokenizer).__name__,
                  'tokenizer_vocab_size':len(tokenizer),'bos_token_id':tokenizer.bos_token_id,
                  'eos_token_id':tokenizer.eos_token_id,'padding_token_id':tokenizer.pad_token_id,
                  'automatic_special_tokens':False,'bos_policy':'omitted_v1',
                  'eos_policy':'not_scored_v1','exact_boundaries':len(TOKENIZER_CHECKS),
                  'all_leading_space':all(row['leading_space'] for row in TOKENIZER_CHECKS)}, indent=2))
"""),
    md("""## F — load pinned BF16 source weights as approved FP16 runtime

The source checkpoint remains the pinned upstream BF16 files. `from_pretrained(... torch_dtype=float16)` casts them at load; this is not quantization. Use GPU 0 only and stop below 12 GiB free VRAM or 12 GiB available RAM. No model sharding/offload is configured.
"""),
    code("""ALLOW_MODEL_LOAD = True  # user-approved RB2 inference on the pinned checkpoint
SELECTED_GPU = 0
model = None
if ALLOW_MODEL_LOAD:
    import json, torch
    from transformers import AutoModelForCausalLM
    if not torch.cuda.is_available():
        raise RuntimeError('Qwen load stopped: CUDA GPU is unavailable.')
    if torch.cuda.device_count() < 1:
        raise RuntimeError('Qwen load stopped: no GPU 0 is visible.')
    gpu_name = torch.cuda.get_device_name(SELECTED_GPU)
    if 'T4' not in gpu_name:
        raise RuntimeError(f'Expected approved Tesla T4 runtime on GPU 0, got {gpu_name}.')
    free_bytes, total_bytes = torch.cuda.mem_get_info(SELECTED_GPU)
    if free_bytes < 12 * 1024**3:
        raise RuntimeError(f'Qwen load stopped: GPU {SELECTED_GPU} has less than 12 GiB free VRAM before load.')
    ram_available = read_meminfo_kib('MemAvailable')
    if ram_available is None or ram_available * 1024 < 12 * 1024**3:
        raise RuntimeError('Qwen load stopped: less than 12 GiB host RAM is available before load.')
    if not TOKENIZER_CHECKS or MODEL_SNAPSHOT.name != MODEL_REVISION:
        raise RuntimeError('Qwen load stopped: pinned snapshot and tokenizer checks must pass first.')
    torch.cuda.reset_peak_memory_stats(SELECTED_GPU)
    model = AutoModelForCausalLM.from_pretrained(str(MODEL_SNAPSHOT), local_files_only=True,
             trust_remote_code=False, use_safetensors=True, torch_dtype=torch.float16)
    model.to(f'cuda:{SELECTED_GPU}').eval()
    free_after, total_after = torch.cuda.mem_get_info(SELECTED_GPU)
    print(json.dumps({'loaded_revision':MODEL_REVISION,'upstream_checkpoint_dtype':'bfloat16',
      'runtime_weight_dtype':'float16','runtime_cast':'approved BF16-to-FP16 at load',
      'quantization':'none','offload':False,'device_map':None,'gpu_index':SELECTED_GPU,
      'gpu':gpu_name,'free_vram_after_bytes':free_after,'total_vram_bytes':total_after,
      'peak_vram_allocated_bytes':torch.cuda.max_memory_allocated(SELECTED_GPU),
      'peak_vram_reserved_bytes':torch.cuda.max_memory_reserved(SELECTED_GPU),
      'ram_available_bytes':read_meminfo_kib('MemAvailable')*1024,
      'checkpoint_file_hashes_local_integrity_only':CACHED_MODEL_FILE_HASHES}, indent=2))
else:
    print('Skipped: no model weights loaded.')
"""),
    md("""## G — scoring smoke (few categories, conditional scoring only)

The smoke scores two packaged category strings across all eight room continuations plus neutral. It checks finite outputs, same-process repeatability, and single-versus-right-padded-batch parity under the locked tolerance. Full A export is a later cell and runs only if this smoke passes. No generate/chat output or scene/cache/annotation is used.
"""),
    code("""RUN_SCORING_SMOKE = True  # approved; runs after model and tokenizer gates pass
OUTPUT_ROOT = Path('/kaggle/working/room_belief_qwen_rb2_output_v2')
TEMPERATURE = float(score_contract['score']['temperature'])
EPSILON = float(score_contract['score']['epsilon'])
UNIFORM_PRIOR = {room_id: 1.0 / len(room_ids) for room_id in room_ids}
SMOKE_PASSED = False

def process_memory_bytes():
    values = {}
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith(('VmRSS:', 'VmHWM:')):
                key, rest = line.split(':', 1)
                values[key] = int(rest.strip().split()[0]) * 1024
    except OSError:
        pass
    return values

def run_a_rows(categories, *, phase, verify_smoke=False):
    if model is None or 'tokenizer' not in globals():
        raise RuntimeError('Scoring requires a loaded model and pinned tokenizer.')
    import csv, time
    from datetime import datetime, timezone
    from collections import defaultdict
    import numpy as np, torch
    from qwen_room_scorer import (make_a_row, neutral_object_prompt, neutral_corrected_score,
        object_prompt, score_batch_with_torch_model, score_with_torch_model)
    device = f'cuda:{SELECTED_GPU}'
    raw = []
    table = []
    ledger_rows = defaultdict(dict)
    start = time.perf_counter()
    torch.cuda.reset_peak_memory_stats(SELECTED_GPU)
    neutral_cache = {}
    for room in room_rows:
        neutral_cache[room['id']] = score_with_torch_model(model, tokenizer, neutral_object_prompt(), room['prompt_completion'], device=device)
    for category in categories:
        category_scores = {}
        for room in room_rows:
            scored = score_with_torch_model(model, tokenizer, object_prompt(category), room['prompt_completion'], device=device)
            neutral = neutral_cache[room['id']]
            raw.append({'category':category,'room_id':room['id'],'completion':room['prompt_completion'],'category_prefix':scored,'neutral_prefix':neutral})
            category_scores[room['id']] = {
                'mean_no_neutral': scored['mean_logprob'],
                'sum_no_neutral': scored['sum_logprob'],
                'mean_neutral': neutral_corrected_score(scored['continuation_logprobs'], neutral['continuation_logprobs'], 'mean'),
                'sum_neutral': neutral_corrected_score(scored['continuation_logprobs'], neutral['continuation_logprobs'], 'sum'),
            }
        for variant, score_key in [('mean_no_neutral','mean_no_neutral'),('mean_neutral','mean_neutral'),('sum_no_neutral','sum_no_neutral'),('sum_neutral','sum_neutral')]:
            arow = make_a_row({r: category_scores[r][score_key] for r in room_ids}, UNIFORM_PRIOR, temperature=TEMPERATURE, epsilon=EPSILON)
            for room_id in room_ids:
                table.append({'category':category,'variant':variant,'room_id':room_id,'uncalibrated_normalized_score':arow['normalized_room_score_uncalibrated'][room_id],'A_log_ratio_uncalibrated':arow['A_log_ratio_uncalibrated'][room_id],'raw_variant_score':category_scores[room_id][score_key]})
                ledger_rows[variant].setdefault(category, {})[room_id] = arow['A_log_ratio_uncalibrated'][room_id]
    smoke_checks = None
    if verify_smoke:
        sample_category = categories[0]
        sample_room = room_rows[0]
        sample_prefix = object_prompt(sample_category)
        sample = score_with_torch_model(model, tokenizer, sample_prefix,
                    sample_room['prompt_completion'], device=device)
        repeated = score_with_torch_model(model, tokenizer, sample_prefix,
                    sample_room['prompt_completion'], device=device)
        neutral_sample = neutral_cache[sample_room['id']]
        batched = score_batch_with_torch_model(model, tokenizer, [
            (sample_prefix, sample_room['prompt_completion']),
            (neutral_object_prompt(), sample_room['prompt_completion'])], device=device)
        tolerance = score_contract['runtime_validation']['repeatability_tolerance']
        repeat_delta = float(np.max(np.abs(np.asarray(sample['continuation_logprobs'], dtype=np.float32) -
                                         np.asarray(repeated['continuation_logprobs'], dtype=np.float32))))
        batch_delta = float(max(
            np.max(np.abs(np.asarray(sample['continuation_logprobs'], dtype=np.float32) -
                          np.asarray(batched[0]['continuation_logprobs'], dtype=np.float32))),
            np.max(np.abs(np.asarray(neutral_sample['continuation_logprobs'], dtype=np.float32) -
                          np.asarray(batched[1]['continuation_logprobs'], dtype=np.float32))),
        ))
        repeat_ok = np.allclose(sample['continuation_logprobs'], repeated['continuation_logprobs'],
                                atol=tolerance['absolute_logprob'], rtol=tolerance['relative_logprob'])
        batch_ok = np.allclose(sample['continuation_logprobs'], batched[0]['continuation_logprobs'],
                               atol=tolerance['absolute_logprob'], rtol=tolerance['relative_logprob']) and \\
                   np.allclose(neutral_sample['continuation_logprobs'], batched[1]['continuation_logprobs'],
                               atol=tolerance['absolute_logprob'], rtol=tolerance['relative_logprob'])
        smoke_checks = {'finite_scores':True,'repeatability_pass':bool(repeat_ok),
                        'repeatability_max_abs_logprob_delta':repeat_delta,
                        'batch_vs_single_pass':bool(batch_ok),
                        'batch_vs_single_max_abs_logprob_delta':batch_delta,
                        'tolerance':tolerance,'mean_sum_and_neutral_variants':
                        ['mean_no_neutral','mean_neutral','sum_no_neutral','sum_neutral'],
                        'temperature':TEMPERATURE}
        if not repeat_ok or not batch_ok:
            raise RuntimeError(f'Qwen scoring smoke failed numeric parity: {smoke_checks}')
    if not all(np.isfinite(value) for row in table for value in
               (row['uncalibrated_normalized_score'], row['A_log_ratio_uncalibrated'], row['raw_variant_score'])):
        raise RuntimeError('Scoring produced a non-finite normalized value, A row, or raw variant score.')
    output_dir = OUTPUT_ROOT / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') / phase
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir/'raw_token_scores.json').write_text(json.dumps(raw, indent=2, ensure_ascii=False) + '\\n')
    (output_dir/'a_table.json').write_text(json.dumps(table, indent=2, ensure_ascii=False) + '\\n')
    (output_dir/'category_room_scores_by_variant.json').write_text(json.dumps(dict(ledger_rows), indent=2, ensure_ascii=False) + '\\n')
    with (output_dir/'a_table.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(table[0]))
        writer.writeheader(); writer.writerows(table)
    elapsed = time.perf_counter() - start
    manifest = {'schema':'room-belief-qwen-a-table/v1','phase':phase,'model_id':MODEL_ID,'model_revision':MODEL_REVISION,'upstream_checkpoint_dtype':'bfloat16','runtime_dtype':'float16','runtime_cast':'approved BF16 checkpoint cast to FP16 at model load','quantization':'none','device':device,'selected_gpu_index':SELECTED_GPU,'gpu_name':torch.cuda.get_device_name(SELECTED_GPU),'ontology_sha256':hashlib.sha256((INPUT_ROOT/'ontology_v1.json').read_bytes()).hexdigest(),'category_set_sha256':hashlib.sha256((INPUT_ROOT/'object_categories_v1.json').read_bytes()).hexdigest(),'scoring_contract_sha256':hashlib.sha256((INPUT_ROOT/'scoring_contract_v1.json').read_bytes()).hexdigest(),'input_categories':categories,'category_count':len(categories),'room_count':len(room_rows),'a_table_row_count':len(table),'temperature':TEMPERATURE,'epsilon':EPSILON,'variants':['mean_no_neutral','mean_neutral','sum_no_neutral','sum_neutral'],'bos_policy':'omitted_v1','eos_policy':'not_scored_v1','padding_policy':'right_padding_masked_v1','score_math':'FP32 log-softmax and accumulation','smoke_checks':smoke_checks,'elapsed_seconds':elapsed,'peak_vram_allocated_bytes':torch.cuda.max_memory_allocated(SELECTED_GPU),'peak_vram_reserved_bytes':torch.cuda.max_memory_reserved(SELECTED_GPU),'process_memory_bytes':process_memory_bytes(),'claim':'uncalibrated language-model compatibility table; not room correctness probabilities','checkpoint_file_hashes_local_integrity_only':CACHED_MODEL_FILE_HASHES,'expected_upstream_checksum_manifest':None}
    manifest['output_file_sha256'] = {name:hashlib.sha256((output_dir/name).read_bytes()).hexdigest() for name in ('raw_token_scores.json','a_table.json','a_table.csv','category_room_scores_by_variant.json')}
    (output_dir/'manifest.json').write_text(json.dumps(manifest, indent=2) + '\\n')
    print(json.dumps({'output':str(output_dir),'phase':phase,'categories':len(categories),'A_rows':len(table),'runtime_seconds':elapsed,'manifest':manifest}, indent=2))
    return output_dir

if RUN_SCORING_SMOKE:
    SMOKE_OUTPUT = run_a_rows(category_strings[:2], phase='smoke', verify_smoke=True)
    SMOKE_PASSED = True
else:
    print('Scoring smoke disabled.')
"""),
    md("""## H — full A table (11 categories × 8 room types)

This runs only after the smoke in cell G passes. It writes all four mean/sum × neutral/no-neutral views; mean + neutral is the current experiment candidate, not a proven winner. The eleven predictor strings are a documented subset, not the detector's full vocabulary.
"""),
    code("""RUN_FULL_A_EXPORT = True  # user approved; guarded by the preceding smoke result
if RUN_FULL_A_EXPORT:
    if not SMOKE_PASSED:
        raise RuntimeError('Full A export stopped: scoring smoke did not pass in this kernel.')
    FULL_A_OUTPUT = run_a_rows(category_strings, phase='full_a', verify_smoke=False)
    expected_rows = len(category_strings) * len(room_rows) * 4
    saved_rows = json.loads((FULL_A_OUTPUT/'a_table.json').read_text())
    if len(saved_rows) != expected_rows or len(category_strings) != 11 or len(room_rows) != 8:
        raise RuntimeError(f'Full A table has an unexpected population: {len(saved_rows)} rows.')
    print(json.dumps({'full_a_output':str(FULL_A_OUTPUT),'category_count':len(category_strings),
                      'room_count':len(room_rows),'expected_variant_rows':expected_rows,
                      'candidate_variant':'mean_neutral; not selected by evaluation'}, indent=2))
else:
    print('Full A export skipped.')
"""),
    md("""## Stop conditions and handoff

Stop if any of these fail: GPU 0 is not a T4; memory/disk gates fail; direct dependency lock or `pip check` fails; pinned Qwen3 config/tokenizer or any of the 96 prompt boundaries fail; model load fails; logits/scores are non-finite; repeatability or batch parity exceeds the recorded tolerance; or the full A population is not exactly 11 × 8 × 4 variant rows. Do not switch model/revision, quantize, use another GPU, install extra packages speculatively, or load scene/GT files.

Keep the notebook and dataset private. Return the environment probe, locked dependency imports/`pip check`, resolved model revision and local file hashes, tokenizer boundary log, GPU and RAM memory logs, smoke checks, full A-table `manifest.json`, `raw_token_scores.json`, `a_table.csv/json`, and output checksums. This notebook contains no capture, GT, or annotations. Passing local synthetic scorer tests is not a Kaggle model smoke; passing the Qwen smoke is not room-label quality validation.
"""),
]


def main() -> None:
    INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for name, source in SOURCE_FILES.items():
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copyfile(source, INPUT_ROOT / name)
    file_records = {
        name: {"sha256": sha256(INPUT_ROOT / name), "bytes": (INPUT_ROOT / name).stat().st_size}
        for name in SOURCE_FILES
    }
    (INPUT_ROOT / "input_manifest.json").write_text(json.dumps({
        "schema": "room-belief-qwen-rb2-input-package/v2",
        "contains_scene_data": False,
        "contains_annotations": False,
        "contains_model_weights": False,
        "files": file_records,
    }, indent=2) + "\n")
    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(INPUT_ROOT.iterdir()):
            if path.is_file():
                info = zipfile.ZipInfo(path.name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, path.read_bytes())
    zip_hash = sha256(ZIP_PATH)
    notebook_cells = []
    for cell in CELLS:
        cell_copy = dict(cell)
        if cell_copy["cell_type"] == "code":
            cell_copy["source"] = [line.replace("__INPUT_ZIP_SHA256__", zip_hash) for line in cell_copy["source"]]
        notebook_cells.append(cell_copy)
    NOTEBOOK.parent.mkdir(parents=True, exist_ok=True)
    NOTEBOOK.write_text(json.dumps({
        "cells": notebook_cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({"input_package":str(INPUT_ROOT),"zip":str(ZIP_PATH),"notebook":str(NOTEBOOK),"files":file_records,"archive_sha256":zip_hash}, indent=2))


if __name__ == "__main__":
    main()
