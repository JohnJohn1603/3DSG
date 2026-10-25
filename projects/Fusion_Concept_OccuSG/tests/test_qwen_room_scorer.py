import math

import numpy as np
import pytest

from standalone_sg.contracts.errors import ContractError
from standalone_sg.semantics.qwen_room_scorer import (
    aggregate_token_scores,
    causal_continuation_logprobs,
    make_a_row,
    neutral_corrected_score,
    neutral_object_prompt,
    object_prompt,
    score_batch_with_torch_model,
    score_with_torch_model,
    split_conditional_continuation,
)


class ExactTokenizer:
    def __init__(self, encodings):
        self.encodings = encodings
        self.calls = []

    def __call__(self, text, *, add_special_tokens):
        self.calls.append((text, add_special_tokens))
        return {"input_ids": self.encodings[text]}


def test_prompt_contract_uses_only_the_supplied_object_string_and_neutral_word():
    assert object_prompt("table") == "Observed object: table.\nRoom type:"
    assert neutral_object_prompt() == "Observed object: object.\nRoom type:"
    with pytest.raises(ContractError, match="non-empty"):
        object_prompt("  ")


def test_token_boundary_uses_full_string_and_disables_special_tokens():
    tokenizer = ExactTokenizer({"prefix": [1, 2], "prefix room": [1, 2, 3]})
    prefix, completion = split_conditional_continuation(tokenizer, "prefix", " room")
    assert prefix == [1, 2]
    assert completion == [3]
    assert tokenizer.calls == [("prefix", False), ("prefix room", False)]


def test_token_boundary_fails_if_completion_retokenizes_prefix():
    tokenizer = ExactTokenizer({"prefix": [1, 2], "prefix room": [1, 9, 3]})
    with pytest.raises(ContractError, match="boundary mismatch"):
        split_conditional_continuation(tokenizer, "prefix", " room")


def test_causal_shift_scores_only_completion_targets_and_ignores_right_padding():
    ids = np.asarray([[1, 2, 3, 0, 0], [4, 5, 6, 7, 0]], dtype=np.int64)
    mask = np.asarray([[1, 1, 1, 0, 0], [1, 1, 1, 1, 0]], dtype=np.int8)
    logits = np.zeros((2, 5, 8), dtype=np.float32)
    # Row 0 prefix=[1], completion=[2,3]; predictions come from positions 0 and 1.
    logits[0, 0, 2] = 8.0
    logits[0, 1, 3] = 8.0
    # Row 1 prefix=[4,5], completion=[6,7]; predictions come from positions 1 and 2.
    logits[1, 1, 6] = 8.0
    logits[1, 2, 7] = 8.0
    # A misleading high logit at the target's own position must not be used.
    logits[0, 2, 2] = 30.0
    scores = causal_continuation_logprobs(
        logits, ids, prefix_lengths=[1, 2], continuation_lengths=[2, 2], attention_mask=mask,
    )
    assert scores[0]["continuation_token_ids"] == [2, 3]
    assert scores[1]["continuation_token_ids"] == [6, 7]
    assert scores[0]["token_count"] == scores[1]["token_count"] == 2
    assert scores[0]["mean_logprob"] > -0.01
    assert scores[1]["mean_logprob"] > -0.01


def test_padding_inside_label_tokens_is_rejected():
    logits = np.zeros((1, 3, 5), dtype=np.float32)
    ids = np.asarray([[1, 2, 3]])
    with pytest.raises(ContractError, match="unpadded"):
        causal_continuation_logprobs(logits, ids, prefix_lengths=[1], continuation_lengths=[2],
                                     attention_mask=[[1, 0, 1]])


def test_batch_scores_equal_single_row_scores_for_each_unpadded_sequence():
    rng = np.random.default_rng(17)
    ids = np.asarray([[1, 2, 3, 0], [4, 5, 6, 7]], dtype=np.int64)
    mask = np.asarray([[1, 1, 1, 0], [1, 1, 1, 1]], dtype=np.int8)
    logits = rng.normal(size=(2, 4, 9)).astype(np.float32)
    batched = causal_continuation_logprobs(
        logits, ids, prefix_lengths=[1, 2], continuation_lengths=[2, 2], attention_mask=mask,
    )
    for row, prefix_len in enumerate((1, 2)):
        length = prefix_len + 2
        single = causal_continuation_logprobs(
            logits[row:row+1, :length], ids[row:row+1, :length],
            prefix_lengths=[prefix_len], continuation_lengths=[2],
            attention_mask=mask[row:row+1, :length],
        )[0]
        assert batched[row]["continuation_token_ids"] == single["continuation_token_ids"]
        assert batched[row]["continuation_logprobs"] == pytest.approx(single["continuation_logprobs"])


def test_extreme_finite_logits_keep_finite_continuation_scores():
    logits = np.asarray([[[1.0e30, -1.0e30, 0.0], [-1.0e30, 1.0e30, 0.0]]], dtype=np.float32)
    scores = causal_continuation_logprobs(logits, [[0, 1]], prefix_lengths=[1], continuation_lengths=[1])
    assert all(math.isfinite(value) for value in scores[0]["continuation_logprobs"])


def test_nonfinite_logits_fail_before_log_softmax():
    logits = np.asarray([[[0.0, np.inf], [0.0, 1.0]]], dtype=np.float32)
    with pytest.raises(ContractError, match="logits must be finite"):
        causal_continuation_logprobs(logits, [[0, 1]], prefix_lengths=[1], continuation_lengths=[1])


def test_model_wrapper_single_and_right_padded_batch_scores_match():
    torch = pytest.importorskip("torch")
    from types import SimpleNamespace

    tokenizer = ExactTokenizer({
        "p": [1], "p a": [1, 2],
        "long": [3, 4], "long b c": [3, 4, 5, 6],
    })
    tokenizer.pad_token_id = 0
    tokenizer.eos_token_id = 0

    class DeterministicToyModel:
        def eval(self):
            return self

        def __call__(self, *, input_ids, attention_mask, use_cache):
            assert use_cache is False
            logits = torch.nn.functional.one_hot(input_ids, num_classes=8).to(torch.float32) * 2.0
            return SimpleNamespace(logits=logits)

    model = DeterministicToyModel()
    prompts = [("p", " a"), ("long", " b c")]
    batched = score_batch_with_torch_model(model, tokenizer, prompts, device="cpu")
    singles = [score_with_torch_model(model, tokenizer, *prompt, device="cpu") for prompt in prompts]
    for batch_row, single_row in zip(batched, singles):
        assert batch_row["continuation_token_ids"] == single_row["continuation_token_ids"]
        assert batch_row["continuation_logprobs"] == pytest.approx(single_row["continuation_logprobs"], abs=1e-7)
        assert batch_row["padding_policy"] == "right_padding_masked_v1"


def test_mean_sum_and_neutral_subtraction_are_separate_score_views():
    category = [-1.0, -3.0]
    neutral = [-2.0, -2.0]
    assert aggregate_token_scores(category, "mean") == pytest.approx(-2.0)
    assert aggregate_token_scores(category, "sum") == pytest.approx(-4.0)
    assert neutral_corrected_score(category, neutral, "mean") == pytest.approx(0.0)
    assert neutral_corrected_score(category, neutral, "sum") == pytest.approx(0.0)
    with pytest.raises(ContractError, match="reduction"):
        aggregate_token_scores(category, "last")


def test_a_row_is_finite_uncalibrated_log_ratio_and_uniform_eight_bound():
    labels = {f"room_{i}": float(i) for i in range(8)}
    prior = {key: 1.0 for key in labels}
    output = make_a_row(labels, prior, temperature=1.0, epsilon=1e-8)
    assert output["temperature"] == 1.0
    assert output["epsilon"] == pytest.approx(1e-8)
    assert sum(output["normalized_room_score_uncalibrated"].values()) == pytest.approx(1.0)
    values = output["A_log_ratio_uncalibrated"].values()
    assert all(math.isfinite(value) for value in values)
    assert max(values) <= math.log(8.0) + 1e-7
    assert max(values) > 0.0
