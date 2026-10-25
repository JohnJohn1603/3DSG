from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "webots" / "controllers" / "m12_motor_capture"))

from standalone_sg.mapping import FreeSpaceGrid2D
from standalone_sg.visualization.live_overlay import LiveVisualizationConfig, render_grid_overlay
from live_overlay_protocol import LatestVisualizationReceiver, VisualizationProtocolError


def _grid() -> FreeSpaceGrid2D:
    values = np.asarray([
        [-1, 0, 100],
        [0, 25, -1],
        [100, 0, 0],
    ], dtype=np.int8)
    nan = np.full(values.shape, np.nan, dtype=np.float64)
    return FreeSpaceGrid2D(values, 0.5, (-1.0, -2.0), nan, nan, 7, frame_id="world")


def _message(payload: bytes, *, status: str = "active") -> bytes:
    header = {
        "type": "visualization", "schema_version": 1,
        "sequence_id": "seq", "source_frame_id": "frame-2", "map_revision": 7,
        "simulation_timestamp_s": 2.0, "frame_id": "world", "origin_xy": [-1.0, -2.0],
        "resolution_m": 0.5, "grid_width": 3, "grid_height": 3,
        "payload_kind": "rgb888", "display_width": 2, "display_height": 2,
        "payload_bytes": len(payload), "status": status,
    }
    return (json.dumps(header) + "\n").encode() + payload


def test_overlay_uses_native_grid_semantics_and_world_aligned_viewport():
    config = LiveVisualizationConfig.from_mapping({"enabled": True, "width": 160, "height": 120})
    payload, diagnostics = render_grid_overlay(
        _grid(), base_xy=(-0.25, -1.25), yaw_rad=0.0,
        trajectory_xy=[(-0.75, -1.75), (-0.25, -1.25)], config=config,
    )
    assert len(payload) == 160 * 120 * 3
    assert diagnostics["grid_semantics"].startswith("-1=unknown, 0=free")
    assert diagnostics["origin_xy"] == [-1.0, -2.0]
    assert diagnostics["resolution_m"] == pytest.approx(0.5)
    assert diagnostics["trajectory_point_count"] == 2
    assert diagnostics["viewport_world_xy"][0] < -1.0


def test_overlay_flips_world_positive_y_to_display_up():
    values = np.asarray([[0], [100]], dtype=np.int8)
    nan = np.full(values.shape, np.nan, dtype=np.float64)
    grid = FreeSpaceGrid2D(values, 1.0, (0.0, 0.0), nan, nan, 1, frame_id="world")
    config = LiveVisualizationConfig.from_mapping({"enabled": True, "width": 160, "height": 120})
    payload, diagnostics = render_grid_overlay(grid, base_xy=None, yaw_rad=None, config=config)
    image = np.frombuffer(payload, dtype=np.uint8).reshape(config.height, config.width, 3)
    xmin, xmax, ymin, ymax = diagnostics["viewport_world_xy"]
    scale = diagnostics["world_to_pixel_scale"]
    viewport_left = (config.width - (xmax - xmin) * scale) / 2.0
    viewport_top = (config.height - (ymax - ymin) * scale) / 2.0
    grid_top = viewport_top + (ymax - 2.0) * scale
    occupied_y = int(round(grid_top + 0.25 * scale))
    free_y = int(round(grid_top + 1.25 * scale))
    x = int(round(viewport_left + (0.5 - xmin) * scale))
    assert tuple(image[occupied_y, x]) == config.occupied_high_color
    assert tuple(image[free_y, x]) == config.free_color


def test_visualization_protocol_handles_fragmented_payload_and_latest_only_replacement():
    receiver = LatestVisualizationReceiver(max_payload_bytes=100)
    first = _message(bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]))
    second = _message(bytes([13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]), status="finished")
    for chunk in (first[:11], first[11:40], first[40:], second[:7], second[7:]):
        receiver.feed(chunk)
    header, payload = receiver.take_latest()
    assert header["status"] == "finished"
    assert payload == bytes([13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24])
    assert receiver.received == 2
    assert receiver.replaced == 1


def test_visualization_protocol_rejects_payload_size_mismatch_and_oversize_header():
    receiver = LatestVisualizationReceiver(max_payload_bytes=10)
    with pytest.raises(VisualizationProtocolError, match="payload_bytes"):
        receiver.feed(_message(bytes(range(12))))

    receiver = LatestVisualizationReceiver(max_header_bytes=20, max_payload_bytes=100)
    with pytest.raises(VisualizationProtocolError, match="header exceeds"):
        receiver.feed(b"{" + b"x" * 30)


def test_motor_configs_enable_visualization_with_display_contract():
    for name in ("m12_two_room_motor_route.json", "m12_corridor_motor_route.json"):
        config = json.loads((ROOT / "configs" / "webots" / name).read_text())
        visualization = LiveVisualizationConfig.from_mapping(config["visualization"])
        assert visualization.enabled
        assert (visualization.width, visualization.height) == (640, 480)
        assert visualization.show_occupied and visualization.show_trajectory and visualization.show_robot


def test_live_bridge_uses_separate_visualization_channel_and_keeps_mapping_queue_contract():
    source = (ROOT / "scripts" / "run_m12_webots_live_bridge.py").read_text()
    assert "--visualization-port" in source
    assert "--webots-mode" in source
    assert "LatestVisualizationSender" in source
    assert "LatestVisualizationWorker" in source
    assert "visualization_worker.submit" in source
    assert "queue.Queue(maxsize=" in source
    assert "publish_visualization" in source


def test_live_bridge_publishes_m3_grid_and_persists_latency_diagnostics():
    source = (ROOT / "scripts" / "run_m12_webots_live_bridge.py").read_text()
    assert "grid_ready_callback" in source
    assert 'stage="m3_ready"' in source
    assert 'output / "latency_diagnostics.json"' in source
    assert "queue_wait_ms" in source
    assert "map_age_simulation_s" in source


def test_live_bridge_exports_graph_before_webots_shutdown_wait():
    source = (ROOT / "scripts" / "run_m12_webots_live_bridge.py").read_text()
    export_pos = source.index("_write_live_graph_artifacts(output, final_live")
    shutdown_pos = source.index("webots.communicate(timeout=45)")
    assert export_pos < shutdown_pos
    assert "controller_capture_error" in source
    assert 'else "partial"' in source
