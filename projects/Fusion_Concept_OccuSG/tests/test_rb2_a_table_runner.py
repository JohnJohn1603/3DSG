import importlib.util
from pathlib import Path

import pytest

from scripts.rb2_a_table_runner import build_a_tables, validate_static_population


ROOT = Path(__file__).resolve().parents[1]
SCORER = ROOT / "artifacts/room_belief/rb2_qwen_inputs_v2/qwen_room_scorer.py"
_spec = importlib.util.spec_from_file_location("rb2_test_scorer", SCORER)
_scorer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scorer)


def _make_scores():
    categories = [f"object_{index}" for index in range(11)]
    rooms = [{"id": f"room_{index}", "prompt_completion": f" room {index}"}
             for index in range(8)]
    category_results = {}
    neutral_results = {}
    for category_index, category in enumerate(categories):
        category_results[category] = {"room_scores": {}}
        for room_index, room in enumerate(rooms):
            tokens = [-float(category_index + room_index + 1),
                      -float(category_index + room_index + 2)]
            category_results[category]["room_scores"][room["id"]] = {
                "continuation_logprobs": tokens,
                "mean_logprob": sum(tokens) / len(tokens),
                "sum_logprob": sum(tokens),
                "continuation_token_ids": [category_index, room_index],
            }
            neutral_results[room["id"]] = {
                "continuation_logprobs": [-1.0, -2.0],
                "mean_logprob": -1.5,
                "sum_logprob": -3.0,
                "continuation_token_ids": [room_index, room_index + 1],
            }
    return categories, rooms, category_results, neutral_results


@pytest.fixture
def miniature_scores():
    return _make_scores()


def test_population_contract_requires_11_unique_categories_and_8_rooms(miniature_scores):
    categories, rooms, *_ = miniature_scores
    validate_static_population(categories, rooms)
    with pytest.raises(ValueError, match="11 canonical"):
        validate_static_population(categories[:-1], rooms)
    with pytest.raises(ValueError, match="unique"):
        validate_static_population(categories[:-1] + [categories[0]], rooms)
    with pytest.raises(ValueError, match="8 unique"):
        validate_static_population(categories, rooms[:-1])


def test_a_tables_are_four_complete_views_built_from_same_raw_scores(miniature_scores):
    categories, rooms, category_results, neutral_results = _make_scores()
    tables, raw = build_a_tables(
        category_results, neutral_results, categories, rooms,
        make_a_row_fn=_scorer.make_a_row,
        neutral_corrected_score_fn=_scorer.neutral_corrected_score,
        temperature=1.0,
        epsilon=1e-8,
    )
    assert set(tables) == {
        "mean_no_neutral", "mean_neutral", "sum_no_neutral", "sum_neutral"
    }
    assert len(raw) == 11 * 8
    assert all(len(rows) == 11 * 8 for rows in tables.values())
    assert raw[0]["category_prefix"]["continuation_token_ids"] == [0, 0]
    assert raw[0]["neutral_prefix"]["continuation_token_ids"] == [0, 1]
    assert tables["mean_no_neutral"][0]["raw_variant_score"] != tables["mean_neutral"][0]["raw_variant_score"]
    assert tables["sum_no_neutral"][0]["raw_variant_score"] != tables["mean_no_neutral"][0]["raw_variant_score"]
    assert all(row["variant"] == variant for variant, rows in tables.items() for row in rows)


def test_missing_or_nonfinite_raw_scores_fail_closed(miniature_scores):
    categories, rooms, category_results, neutral_results = _make_scores()
    del category_results[categories[0]]["room_scores"][rooms[-1]["id"]]
    with pytest.raises(KeyError):
        build_a_tables(
            category_results, neutral_results, categories, rooms,
            make_a_row_fn=_scorer.make_a_row,
            neutral_corrected_score_fn=_scorer.neutral_corrected_score,
            temperature=1.0, epsilon=1e-8,
        )

    categories, rooms, category_results, neutral_results = miniature_scores
    category_results[categories[0]]["room_scores"][rooms[0]["id"]]["continuation_logprobs"][0] = float("nan")
    with pytest.raises(ValueError, match="Non-finite raw token score"):
        build_a_tables(
            category_results, neutral_results, categories, rooms,
            make_a_row_fn=_scorer.make_a_row,
            neutral_corrected_score_fn=_scorer.neutral_corrected_score,
            temperature=1.0, epsilon=1e-8,
        )
