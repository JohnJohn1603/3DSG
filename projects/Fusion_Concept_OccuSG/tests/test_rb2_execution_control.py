import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.rb2_execution_control import (
    CheckpointMismatch,
    canonical_sha256,
    load_category_checkpoint,
    run_streamed_process,
    run_category_units,
    save_category_checkpoint,
)


def test_canonical_hash_is_order_independent():
    assert canonical_sha256({"a": 1, "b": [2, 3]}) == canonical_sha256({"b": [2, 3], "a": 1})


def test_category_checkpoint_resume_is_hash_bound_and_integrity_checked(tmp_path):
    key = canonical_sha256({"model_revision": "pinned", "config": {"seed": 4}})
    payload = {"room_scores": {"r1": -1.25}, "token_logprobs": [-0.5, -0.75]}
    save_category_checkpoint(tmp_path, run_hash=key, category="potted_plant", payload=payload)
    assert load_category_checkpoint(tmp_path, run_hash=key, category="potted_plant") == payload
    with pytest.raises(CheckpointMismatch, match="another run"):
        load_category_checkpoint(tmp_path, run_hash=canonical_sha256({"model_revision": "other"}),
                                 category="potted_plant")
    file_path = next((tmp_path / "checkpoints").glob("*.json"))
    envelope = json.loads(file_path.read_text())
    envelope["payload"]["room_scores"]["r1"] = 999
    file_path.write_text(json.dumps(envelope))
    with pytest.raises(CheckpointMismatch, match="payload hash"):
        load_category_checkpoint(tmp_path, run_hash=key, category="potted_plant")


def test_streamed_child_flushes_progress_and_marks_complete(tmp_path):
    progress = []
    key = canonical_sha256({"case": "stream"})
    command = [sys.executable, "-u", "-c", "import time; print('stage=started', flush=True); time.sleep(.15); print('stage=done', flush=True)"]
    assert run_streamed_process(command, run_dir=tmp_path, run_hash=key,
                                timeout_seconds=3, on_progress=progress.append) == 0
    assert progress == ["stage=started", "stage=done"]
    assert (tmp_path / "process.log").read_text().splitlines() == progress
    state = json.loads((tmp_path / "run_state.json").read_text())
    assert state["status"] == "completed"


def test_timeout_terminates_child_and_marks_failed(tmp_path):
    key = canonical_sha256({"case": "timeout"})
    child = "import os,time; print('child_started', flush=True); time.sleep(30)"
    with pytest.raises(TimeoutError, match="process group terminated"):
        run_streamed_process([sys.executable, "-u", "-c", child], run_dir=tmp_path,
                            run_hash=key, timeout_seconds=0.35, terminate_grace_seconds=0.1)
    state = json.loads((tmp_path / "run_state.json").read_text())
    assert state["status"] == "failed"
    assert state["stage"] == "timeout"
    assert "child_started" in (tmp_path / "process.log").read_text()


def test_category_units_log_durable_progress_checkpoint_and_resume(tmp_path):
    key = canonical_sha256({"model": "pinned", "execution": "single_only"})
    calls = []
    progress = []

    def score(category):
        calls.append(category)
        return {"rows": [{"room": "bedroom", "score": -1.0}]}

    first = run_category_units(tmp_path, run_hash=key, categories=["bed", "sofa"],
                               score_category=score, on_progress=progress.append)
    assert calls == ["bed", "sofa"]
    assert set(first) == {"bed", "sofa"}
    assert [json.loads(line)["event"] for line in progress] == [
        "category_started", "category_completed", "category_started", "category_completed"]
    state = json.loads((tmp_path / "run_state.json").read_text())
    assert state["status"] == "completed"

    def should_not_score(_category):
        raise AssertionError("valid completed category checkpoint should be reused")

    resumed = run_category_units(tmp_path, run_hash=key, categories=["bed", "sofa"],
                                 score_category=should_not_score, on_progress=progress.append)
    assert resumed == first
    assert [json.loads(line)["event"] for line in progress[-2:]] == [
        "category_resumed", "category_resumed"]


def test_category_unit_failure_is_logged_and_run_marked_failed(tmp_path):
    key = canonical_sha256({"model": "pinned"})
    progress = []

    def fail(_category):
        raise RuntimeError("synthetic scorer error")

    with pytest.raises(RuntimeError, match="synthetic scorer error"):
        run_category_units(tmp_path, run_hash=key, categories=["bed"],
                           score_category=fail, on_progress=progress.append)
    events = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["category_started", "category_failed"]
    state = json.loads((tmp_path / "run_state.json").read_text())
    assert state["status"] == "failed"
    assert state["stage"] == "category_failed"
