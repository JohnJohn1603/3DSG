from __future__ import annotations

import numpy as np
import pytest

from standalone_sg.contracts.errors import ContractError
from standalone_sg.mapping.projection import FreeSpaceGrid2D
from standalone_sg.regions.dude_client import DuDeConfig, DuDeWorkerClient


def _grid():
    values = np.zeros((8, 8), dtype=np.int8)
    z = np.zeros((8, 8), dtype=float)
    return FreeSpaceGrid2D(values, 0.05, (0.0, 0.0), z, z + 2, 1)


def test_partial_response_deadline_and_poisoned_worker(tmp_path):
    fake = tmp_path / "partial.py"
    fake.write_text("#!/usr/bin/env python3\nimport sys,time\nsys.stdin.readline()\nsys.stdout.write('{\\\"protocol_version\\\":1')\nsys.stdout.flush()\ntime.sleep(5)\n")
    fake.chmod(0o755)
    with DuDeWorkerClient(fake, DuDeConfig(timeout_s=0.15)) as worker:
        with pytest.raises(ContractError, match="deadline"):
            worker.integrate(_grid(), timestamp_s=1.0)
        with pytest.raises(ContractError, match="error state"):
            worker.integrate(_grid(), timestamp_s=2.0)


def test_worker_crash_does_not_silently_lose_state(tmp_path):
    fake = tmp_path / "crash.py"
    fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.readline()\nsys.exit(7)\n")
    fake.chmod(0o755)
    with DuDeWorkerClient(fake, DuDeConfig(timeout_s=1.0)) as worker:
        with pytest.raises(ContractError):
            worker.integrate(_grid(), timestamp_s=1.0)


def test_response_contract_mismatch_fails_closed(tmp_path):
    fake = tmp_path / "wrong_protocol.py"
    fake.write_text("#!/usr/bin/env python3\nimport sys,json\nsys.stdin.readline()\nprint(json.dumps({'protocol_version':99,'status':'success'}),flush=True)\n")
    fake.chmod(0o755)
    with DuDeWorkerClient(fake, DuDeConfig(timeout_s=1.0)) as worker:
        with pytest.raises(ContractError, match="protocol version"):
            worker.integrate(_grid(), timestamp_s=1.0)
