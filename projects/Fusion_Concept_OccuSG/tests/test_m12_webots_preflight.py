import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from m12_webots_preflight import validate_world_contract


def _apartment_config():
    return json.loads(
        (ROOT / "configs/webots/m12_complete_apartment_route_recording.json").read_text()
    )


def test_complete_apartment_satisfies_reusable_world_contract():
    result = validate_world_contract(
        ROOT / "webots/worlds/complete_apartment.wbt",
        ROOT / "configs/webots/m12_complete_apartment_route_recording.json",
        _apartment_config(),
        ROOT,
    )
    assert result["basic_time_step_ms"] == 16.0
    assert result["coordinate_system"] == "ENU"
    assert result["robot_proto"] == "M12DiffDriveRGBD"


def test_preflight_rejects_world_config_timestep_mismatch():
    config = _apartment_config()
    config["timing"]["physics_timestep_ms"] = 32
    with pytest.raises(RuntimeError, match="basicTimeStep mismatch"):
        validate_world_contract(
            ROOT / "webots/worlds/complete_apartment.wbt",
            ROOT / "configs/webots/m12_complete_apartment_route_recording.json",
            config,
            ROOT,
        )
