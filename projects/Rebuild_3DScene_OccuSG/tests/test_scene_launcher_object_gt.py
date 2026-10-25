from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import scene_launcher as sl  # noqa: E402


def test_object_results_color_is_optional_and_no_color_is_plain(monkeypatch):
    sample = "Object GT results — run / eval\nTOTAL OBSERVATIONS\nFalse positives                2"
    assert sl.colorize_object_results(sample, enabled=False) == sample
    colored = sl.colorize_object_results(sample, enabled=True)
    assert "\033[1;36m" in colored and "\033[31m" in colored and "\033[0m" in colored
    monkeypatch.setenv("NO_COLOR", "")
    assert sl.colorize_object_results(sample) == sample


def _completed_hm3d_run(tmp_path: Path) -> tuple[Path, str]:
    run_id = "hm3d-fixture-run"
    root = tmp_path / run_id
    root.mkdir(parents=True)
    sl.atomic_json(root / "launcher_manifest.json", {"run_id": run_id, "status": "completed", "dataset": "hm3d"})
    return root, run_id


def test_object_gt_check_uses_shared_cli_and_interactive_handler_without_output(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs"; root, run_id = _completed_hm3d_run(runs)
    monkeypatch.setattr(sl, "RUNS", runs)
    launcher = sl.Launcher()
    calls = []

    class Result:
        returncode = 0
        stdout = '{"status":"ready_with_limits"}'
        stderr = ""

    monkeypatch.setattr(sl.subprocess, "run", lambda cmd, **kwargs: (calls.append(cmd) or Result()))
    assert launcher.handle(["evaluate-objects", run_id, "--check"]) == 0
    first = capsys.readouterr().out
    assert launcher.handle(["/evaluate-objects", run_id, "--check"], interactive=True) == 0
    second = capsys.readouterr().out
    assert first == second
    assert len(calls) == 2 and "--check" in calls[0]
    assert not (root / "object_evaluations").exists()


def test_open_object_eval_selects_latest_completed_only(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs"; root, run_id = _completed_hm3d_run(runs)
    monkeypatch.setattr(sl, "RUNS", runs)
    launcher = sl.Launcher()
    eval_root = root / "object_evaluations"
    old, new, failed = eval_root / "eval_old", eval_root / "eval_new", eval_root / "eval_failed"
    for p, status, finished in ((old, "completed", "2026-01-01"), (new, "completed", "2026-02-01"), (failed, "failed", "2027-01-01")):
        p.mkdir(parents=True); sl.atomic_json(p / "manifest.json", {"status": status, "finished_utc": finished})
    assert launcher._open_target(root, "object-evaluation") == new / "report.vi.md"
    assert launcher._open_target(root, "object-review") == new / "review/index.html"
    assert launcher._last_opened_object_eval == "eval_new"
    assert "eval_new" in str(new)


def test_launcher_evaluator_failure_does_not_write_predictor_manifest(tmp_path, monkeypatch):
    runs = tmp_path / "runs"; root, run_id = _completed_hm3d_run(runs)
    monkeypatch.setattr(sl, "RUNS", runs)
    monkeypatch.setattr(sl, "LOCK_PATH", runs / ".launcher.lock")
    launcher = sl.Launcher()
    before = (root / "launcher_manifest.json").read_bytes()
    monkeypatch.setattr(launcher, "_subprocess", lambda *a, **k: (9, "expected fixture failure"))
    import threading
    launcher.cancel_requested = threading.Event()
    eval_id = "eval_fixture"
    output = root / "object_evaluations" / eval_id
    log = root / "logs" / f"object_evaluation_{eval_id}.log"
    job_path = launcher._object_eval_job_path(root, eval_id)
    job = {"run_id": run_id, "evaluation_id": eval_id, "status": "queued", "log_path": str(log), "output_path": str(output)}
    launcher.active_object_eval = job
    rc = launcher._execute_object_evaluation(root, sl.read_json(root / "launcher_manifest.json"), output, log, job_path)
    assert rc == 1
    assert (root / "launcher_manifest.json").read_bytes() == before
    assert sl.read_json(job_path)["status"] == "failed"


def test_status_labels_object_evaluation_separately_from_completed_predictor(capsys):
    launcher = sl.Launcher()
    launcher.active_object_eval = {"status": "running", "run_id": "hm3d-fixture-run",
                                   "evaluation_id": "eval_fixture", "log_path": "/tmp/eval.log",
                                   "output_path": "/tmp/eval"}
    launcher.cmd_status(["hm3d-fixture-run"])
    output = capsys.readouterr().out
    assert "Object GT evaluation: running" in output
    assert "không đổi trạng thái completed" in output


def test_object_results_has_shared_cli_interactive_handler_and_eval_selector(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs"; _, run_id = _completed_hm3d_run(runs)
    monkeypatch.setattr(sl, "RUNS", runs)
    launcher = sl.Launcher()
    calls = []

    class Result:
        returncode = 0
        stdout = "selected eval and table"
        stderr = ""

    monkeypatch.setattr(sl.subprocess, "run", lambda cmd, **kwargs: (calls.append(cmd) or Result()))
    command = ["object-results", run_id, "--frame", "12", "--eval-id", "eval_saved"]
    assert launcher.handle(command) == 0
    first = capsys.readouterr().out
    assert launcher.handle(["/" + command[0], *command[1:]], interactive=True) == 0
    second = capsys.readouterr().out
    assert first == second == "selected eval and table\n"
    assert len(calls) == 2
    assert "--object-results" in calls[0] and "--frame" in calls[0] and "eval_saved" in calls[0]
