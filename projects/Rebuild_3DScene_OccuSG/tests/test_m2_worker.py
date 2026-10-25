from pathlib import Path

import pytest

from standalone_sg.contracts import ContractError
from standalone_sg.mapping import OctoMapWorker, ScanPoint


ROOT = Path(__file__).resolve().parents[1]
M2_BINARY = ROOT / "build/native-m2/occupancy/standalone_sg_octomap_fixture_runner"


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_python_worker_keeps_one_octomap_across_updates() -> None:
    with OctoMapWorker(M2_BINARY, resolution_m=1.0, response_mode="debug") as worker:
        first = worker.integrate(
            [ScanPoint(2.0, 0.0, 0.0), ScanPoint(0.0, 2.0, 0.0)],
            origin=(0.0, 0.0, 0.0),
            max_range_m=5.0,
        )
        second = worker.integrate(
            [ScanPoint(6.0, 0.0, 0.0), ScanPoint(0.0, 3.0, 0.0, has_hit=False)],
            origin=(0.0, 0.0, 0.0),
            max_range_m=5.0,
        )
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert len(first["occupied_keys"]) == 2
    assert second["occupied_keys"] == []
    assert any(item["occupied"] for item in second["observations"])


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_defaults_to_compact_m2_response() -> None:
    with OctoMapWorker(M2_BINARY, resolution_m=1.0) as worker:
        update = worker.integrate(
            [ScanPoint(2.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=5.0,
        )
    assert update["revision"] == 1
    assert type(update["free_key_count"]) is int
    assert type(update["occupied_key_count"]) is int
    assert "free_keys" not in update and "observations" not in update


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_snapshot_exposes_accumulated_leaves_for_projection() -> None:
    with OctoMapWorker(M2_BINARY, resolution_m=1.0) as worker:
        worker.integrate(
            [ScanPoint(2.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=5.0
        )
        snapshot = worker.snapshot()
    assert snapshot["revision"] == 1
    assert snapshot["resolution"] == 1.0
    assert len(snapshot["leaves"]) > 0
    leaf = snapshot["leaves"][0]
    assert set(leaf) >= {"key", "center", "size", "occupied", "log_odds", "probability"}
    assert len(snapshot["bounds_min"]) == len(snapshot["bounds_max"]) == 3


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_validates_requests_and_close() -> None:
    with pytest.raises(ContractError, match="max_range_m"):
        with OctoMapWorker(M2_BINARY, resolution_m=1.0) as worker:
            worker.integrate([ScanPoint(1.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=0.0)
    with pytest.raises(ContractError, match="has_hit"):
        ScanPoint(1.0, 0.0, 0.0, has_hit=1)  # type: ignore[arg-type]

    worker = OctoMapWorker(M2_BINARY, resolution_m=1.0)
    worker.close()
    with pytest.raises(ContractError, match="closed"):
        worker.snapshot()


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_timeout_terminates_hung_child(tmp_path: Path) -> None:
    fake = tmp_path / "hung_worker.py"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n", encoding="utf-8")
    fake.chmod(0o755)
    worker = OctoMapWorker(fake, resolution_m=1.0, timeout_s=0.05)
    try:
        with pytest.raises(ContractError, match="timed out"):
            worker.integrate(
                [ScanPoint(1.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=2.0
            )
    finally:
        worker.close()


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_timeout_covers_partial_response_before_newline(tmp_path: Path) -> None:
    fake = tmp_path / "partial_response_worker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "sys.stdin.readline()\n"
        "sys.stdout.write('{\\\"revision\\\": 1')\n"
        "sys.stdout.flush()\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    worker = OctoMapWorker(fake, resolution_m=1.0, timeout_s=0.05)
    try:
        with pytest.raises(ContractError, match="timed out"):
            worker.integrate(
                [ScanPoint(1.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=2.0
            )
        with pytest.raises(ContractError, match="error state"):
            worker.snapshot()
    finally:
        worker.close()


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_write_timeout_covers_child_that_does_not_read_large_request(tmp_path: Path) -> None:
    fake = tmp_path / "non_reader_worker.py"
    fake.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n", encoding="utf-8")
    fake.chmod(0o755)
    worker = OctoMapWorker(fake, resolution_m=1.0, timeout_s=0.05)
    try:
        points = [ScanPoint(float(index + 1), 0.0, 0.0) for index in range(100_000)]
        with pytest.raises(ContractError, match="timed out writing"):
            worker.integrate(points, origin=(0.0, 0.0, 0.0), max_range_m=2.0)
    finally:
        worker.close()


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_invalid_response_enters_explicit_error_state(tmp_path: Path) -> None:
    fake = tmp_path / "invalid_response_worker.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.readline()\n"
        "print('{}', flush=True)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    worker = OctoMapWorker(fake, resolution_m=1.0, timeout_s=0.2)
    try:
        with pytest.raises(ContractError, match="invalid integrate response schema"):
            worker.integrate(
                [ScanPoint(1.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=2.0
            )
        with pytest.raises(ContractError, match="error state"):
            worker.integrate(
                [ScanPoint(1.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=2.0
            )
    finally:
        worker.close()


@pytest.mark.skipif(not M2_BINARY.exists(), reason="M2 OctoMap worker has not been built")
def test_worker_accumulates_hit_and_miss_to_clamping_thresholds() -> None:
    with OctoMapWorker(
        M2_BINARY,
        resolution_m=1.0,
        prob_hit=0.7,
        prob_miss=0.4,
        clamping_min=0.12,
        clamping_max=0.97,
        response_mode="debug",
    ) as worker:
        hit = None
        for _ in range(32):
            hit = worker.integrate(
                [ScanPoint(2.0, 0.0, 0.0)], origin=(0.0, 0.0, 0.0), max_range_m=5.0
            )
        assert hit is not None
        hit_endpoint = next(item for item in hit["observations"] if item["key"][0] == 32770)
        assert hit_endpoint["probability"] == pytest.approx(0.97, abs=2e-6)

        miss = None
        for _ in range(64):
            miss = worker.integrate(
                [ScanPoint(3.0, 0.0, 0.0, has_hit=False)],
                origin=(0.0, 0.0, 0.0),
                max_range_m=5.0,
            )
        assert miss is not None
        miss_endpoint = next(item for item in miss["observations"] if item["key"][0] == 32770)
        assert miss_endpoint["probability"] == pytest.approx(0.12, abs=2e-6)
        assert miss_endpoint["occupied"] is False
