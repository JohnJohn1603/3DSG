"""Small, testable helpers for the planned Qwen room-type A-table scorer.

The module does not import Transformers or load weights at import time. It
scores only the continuation tokens of a causal prompt; no generation, EOS,
GT, or scene data is needed by the math helpers.
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Mapping, Sequence

import numpy as np

try:  # package import inside the workspace
    from ..contracts.errors import ContractError
except ImportError:  # standalone copy used by the isolated Kaggle scorer notebook
    class ContractError(ValueError):
        """Contract failure for the standalone notebook copy."""


def object_prompt(canonical_object_name: str) -> str:
    if type(canonical_object_name) is not str or not canonical_object_name.strip():
        raise ContractError("object category string must be non-empty")
    return f"Observed object: {canonical_object_name}.\nRoom type:"


def neutral_object_prompt() -> str:
    return object_prompt("object")


def _one_dimensional_ids(value: object, *, label: str) -> list[int]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], (list, tuple)):
        value = value[0]
    if not isinstance(value, (list, tuple)) or not value:
        raise ContractError(f"{label} token IDs must be a non-empty sequence")
    output: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, np.integer)) or int(item) < 0:
            raise ContractError(f"{label} token IDs must be non-negative integers")
        output.append(int(item))
    return output


def encode_without_special_tokens(tokenizer: object, text: str) -> list[int]:
    """Encode exactly the supplied text with special-token insertion disabled."""
    if type(text) is not str or not text:
        raise ContractError("text to tokenize must be a non-empty string")
    try:
        encoded = tokenizer(text, add_special_tokens=False)
    except Exception as exc:  # tokenizer implementations expose varying exceptions
        raise ContractError(f"tokenizer failed for text: {exc}") from exc
    if isinstance(encoded, Mapping):
        if "input_ids" not in encoded:
            raise ContractError("tokenizer output lacks input_ids")
        ids = encoded["input_ids"]
    else:
        ids = getattr(encoded, "input_ids", None)
    return _one_dimensional_ids(ids, label="encoded")


def split_conditional_continuation(tokenizer: object, prefix: str, completion: str) -> tuple[list[int], list[int]]:
    """Return prefix and completion IDs only when the full-string boundary is exact.

    The prefix is deliberately tokenized both alone and as the start of the
    complete string. If BPE merges across the boundary, the scorer fails closed
    instead of charging a token partly attributable to the prefix.
    """
    if type(prefix) is not str or not prefix:
        raise ContractError("prompt prefix must be a non-empty string")
    if type(completion) is not str or not completion:
        raise ContractError("prompt completion must be a non-empty string")
    prefix_ids = encode_without_special_tokens(tokenizer, prefix)
    full_ids = encode_without_special_tokens(tokenizer, prefix + completion)
    if len(full_ids) <= len(prefix_ids) or full_ids[:len(prefix_ids)] != prefix_ids:
        raise ContractError("tokenizer boundary mismatch: completion changes prefix tokenization")
    completion_ids = full_ids[len(prefix_ids):]
    if not completion_ids:
        raise ContractError("completion produced no continuation tokens")
    return prefix_ids, completion_ids


def causal_continuation_logprobs(
    logits: object,
    input_ids: object,
    *,
    prefix_lengths: Sequence[int],
    continuation_lengths: Sequence[int],
    attention_mask: object | None = None,
) -> list[dict[str, object]]:
    """Score target continuation tokens using logits at the previous position.

    Arrays are ``[batch, sequence, vocabulary]`` and ``[batch, sequence]``.
    Padding is masked by per-row lengths and, when provided, ``attention_mask``.
    Accumulation uses FP32 to match the planned inference contract.
    """
    raw_logits = np.asarray(logits, dtype=np.float32)
    ids = np.asarray(input_ids)
    if raw_logits.ndim != 3 or ids.ndim != 2 or raw_logits.shape[:2] != ids.shape:
        raise ContractError("logits/input_ids must have shapes [B,T,V] and [B,T]")
    batch, steps, vocab = raw_logits.shape
    if not np.isfinite(raw_logits).all():
        raise ContractError("logits must be finite before FP32 log-softmax")
    if len(prefix_lengths) != batch or len(continuation_lengths) != batch:
        raise ContractError("prefix/continuation length arrays must match batch size")
    if attention_mask is None:
        mask = np.ones((batch, steps), dtype=np.int8)
    else:
        mask = np.asarray(attention_mask)
        if mask.shape != (batch, steps) or not np.isin(mask, (0, 1, False, True)).all():
            raise ContractError("attention_mask must be a binary [B,T] array")

    # Stable FP32 log-softmax; don't upcast before the normalization.
    row_max = np.max(raw_logits, axis=-1, keepdims=True)
    shifted = raw_logits - row_max
    log_normalizer = np.log(np.sum(np.exp(shifted, dtype=np.float32), axis=-1, keepdims=True, dtype=np.float32))
    log_probs = shifted - log_normalizer

    results: list[dict[str, object]] = []
    for row in range(batch):
        prefix_len, continuation_len = prefix_lengths[row], continuation_lengths[row]
        if type(prefix_len) is not int or prefix_len < 1:
            raise ContractError("each prefix must contain at least one token")
        if type(continuation_len) is not int or continuation_len < 1:
            raise ContractError("each continuation must contain at least one token")
        end = prefix_len + continuation_len
        if end > steps or not bool(np.asarray(mask[row, :end]).all()):
            raise ContractError("prefix/continuation positions must be unpadded and within sequence bounds")
        targets = ids[row, prefix_len:end].astype(np.int64, copy=False)
        if np.any(targets < 0) or np.any(targets >= vocab):
            raise ContractError("target token ID is outside logits vocabulary")
        prediction_positions = np.arange(prefix_len - 1, end - 1, dtype=np.int64)
        token_logprobs = log_probs[row, prediction_positions, targets].astype(np.float32).tolist()
        if any(not math.isfinite(value) for value in token_logprobs):
            raise ContractError("continuation log probabilities must be finite")
        results.append({
            "continuation_token_ids": targets.tolist(),
            "continuation_logprobs": token_logprobs,
            "token_count": continuation_len,
            "sum_logprob": float(np.sum(np.asarray(token_logprobs, dtype=np.float32), dtype=np.float32)),
            "mean_logprob": float(np.mean(np.asarray(token_logprobs, dtype=np.float32), dtype=np.float32)),
        })
    return results


def aggregate_token_scores(logprobs: Sequence[Real], reduction: str) -> float:
    if reduction not in ("mean", "sum"):
        raise ContractError("score reduction must be 'mean' or 'sum'")
    values = np.asarray(logprobs, dtype=np.float32)
    if values.ndim != 1 or not len(values) or not np.isfinite(values).all():
        raise ContractError("token scores must be a non-empty finite vector")
    result = np.sum(values, dtype=np.float32) if reduction == "sum" else np.mean(values, dtype=np.float32)
    return float(result)


def neutral_corrected_score(category_logprobs: Sequence[Real], neutral_logprobs: Sequence[Real], reduction: str) -> float:
    return aggregate_token_scores(category_logprobs, reduction) - aggregate_token_scores(neutral_logprobs, reduction)


def _softmax(values: Mapping[str, float], temperature: float) -> dict[str, float]:
    if not values or not math.isfinite(temperature) or temperature <= 0:
        raise ContractError("softmax needs scores and a finite positive temperature")
    if any(type(key) is not str or not key or not math.isfinite(float(value)) for key, value in values.items()):
        raise ContractError("softmax scores must use non-empty names and finite values")
    maximum = max(float(value) / temperature for value in values.values())
    exps = {key: math.exp(float(value) / temperature - maximum) for key, value in values.items()}
    denom = sum(exps.values())
    return {key: value / denom for key, value in exps.items()}


def make_a_row(
    room_scores: Mapping[str, float],
    room_prior: Mapping[str, float],
    *,
    temperature: float = 1.0,
    epsilon: float = 1e-8,
) -> dict[str, object]:
    """Build one explicit, uncalibrated log-ratio A row from scored labels."""
    if set(room_scores) != set(room_prior) or not room_scores:
        raise ContractError("room scores and prior must cover the same non-empty room IDs")
    if isinstance(epsilon, bool) or not isinstance(epsilon, Real) or not math.isfinite(float(epsilon)) or float(epsilon) <= 0:
        raise ContractError("epsilon must be finite and positive")
    if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) or float(value) <= 0
           for value in room_prior.values()):
        raise ContractError("room priors must be finite and positive")
    prior_total = sum(float(value) for value in room_prior.values())
    normalized_prior = {key: float(value) / prior_total for key, value in room_prior.items()}
    distribution = _softmax(room_scores, temperature)
    a_values = {key: math.log((distribution[key] + float(epsilon)) /
                              (normalized_prior[key] + float(epsilon))) for key in distribution}
    if any(not math.isfinite(value) for value in a_values.values()):
        raise ContractError("A row must contain finite values")
    return {
        "normalized_room_score_uncalibrated": distribution,
        "normalized_reference_prior": normalized_prior,
        "A_log_ratio_uncalibrated": a_values,
        "temperature": float(temperature), "epsilon": float(epsilon),
    }


def score_batch_with_torch_model(
    model: object,
    tokenizer: object,
    prompts: Sequence[tuple[str, str]],
    *,
    device: str,
) -> list[dict[str, object]]:
    """Score right-padded continuation prompts in one causal-LM forward pass.

    BOS and EOS are intentionally omitted in v1. The actual model is only
    imported/invoked by the notebook after its explicit download/load gate.
    """
    import torch

    if not prompts:
        raise ContractError("batched scoring requires at least one prompt")
    split = [split_conditional_continuation(tokenizer, prefix, completion)
             for prefix, completion in prompts]
    sequences = [prefix_ids + continuation_ids for prefix_ids, continuation_ids in split]
    pad_id = getattr(tokenizer, "pad_token_id", None)
    if pad_id is None:
        pad_id = getattr(tokenizer, "eos_token_id", None)
    if isinstance(pad_id, bool) or not isinstance(pad_id, (int, np.integer)) or int(pad_id) < 0:
        raise ContractError("tokenizer must expose a non-negative pad_token_id or eos_token_id")
    max_length = max(map(len, sequences))
    input_ids = torch.full((len(sequences), max_length), int(pad_id), dtype=torch.long, device=device)
    attention_mask = torch.zeros_like(input_ids)
    for row, sequence in enumerate(sequences):
        input_ids[row, :len(sequence)] = torch.tensor(sequence, dtype=torch.long, device=device)
        attention_mask[row, :len(sequence)] = 1
    model.eval()
    with torch.inference_mode():
        output = model(input_ids=input_ids, attention_mask=attention_mask, use_cache=False)
    score_rows = causal_continuation_logprobs(
        output.logits.detach().float().cpu().numpy(), input_ids.detach().cpu().numpy(),
        prefix_lengths=[len(prefix_ids) for prefix_ids, _ in split],
        continuation_lengths=[len(continuation_ids) for _, continuation_ids in split],
        attention_mask=attention_mask.detach().cpu().numpy(),
    )
    return [
        {
            "prefix": prefix,
            "completion": completion,
            "prefix_token_ids": prefix_ids,
            **scores,
            "bos_policy": "omitted_v1",
            "eos_policy": "not_scored_v1",
            "score_dtype": "float32_log_softmax_and_accumulation",
            "padding_policy": "right_padding_masked_v1",
        }
        for (prefix, completion), (prefix_ids, _), scores in zip(prompts, split, score_rows)
    ]


def score_with_torch_model(model: object, tokenizer: object, prefix: str, completion: str, *, device: str) -> dict[str, object]:
    """Score one continuation using a caller-loaded causal LM; no generate()."""
    return score_batch_with_torch_model(model, tokenizer, [(prefix, completion)], device=device)[0]
