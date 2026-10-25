#!/usr/bin/env python3
"""Launch the desktop with a short native run and capture its live three-panel state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from exploration.desktop import DesktopWindow  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-python", type=Path,
                        default=Path("/home/kiettran05/miniconda3/envs/habitat/bin/python"))
    parser.add_argument("--scene-config", type=Path,
                        default=ROOT / "configs/exploration/hm3d_00802_floor_endpoint_calibrated.json")
    parser.add_argument("--profile", type=Path,
                        default=ROOT / "configs/exploration/cpu_p2_motion.yaml")
    parser.add_argument("--output-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--duration-s", type=float, default=60.0)
    parser.add_argument("--capture-after-s", type=float, default=25.0)
    parser.add_argument("--minimum-graph-revision", type=int, default=2)
    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    window = DesktopWindow(
        live_root=Path("/tmp/3dsg-dashboard-smoke"),
        runner_python=args.runner_python.resolve(strict=True),
        scene_config=args.scene_config.resolve(strict=True),
        profile=args.profile.resolve(strict=True),
        output_root=args.output_root.resolve(), duration_s=args.duration_s,
        enable_p2=True, graph_plugin="exploration.native_graph_plugin:create_plugin")
    window.resize(1600, 1000)
    window.show()
    window.start_run()
    if window.run_id is None or window.process.state() == QtCore.QProcess.NotRunning:
        return 2

    started = time.monotonic()
    captured = False
    capture_ok = False

    def poll() -> None:
        nonlocal captured, capture_ok
        window.viewer.refresh()
        state = window.viewer._state
        graph = window.viewer._graph
        elapsed = float(state.get("elapsed_s", 0.0))
        if (not captured and elapsed >= args.capture_after_s
                and int(graph.get("revision", -1)) >= args.minimum_graph_revision):
            live_root = args.output_root.resolve() / window.run_id / "live"
            image_path = live_root / "live_dashboard.png"
            report_path = live_root / "live_dashboard_audit.json"
            image_ok = window.viewer.grab().save(str(image_path))
            camera = state.get("camera", {})
            navigation = state.get("navigation", {})
            graph_frame = str(graph.get("as_of_frame_id", ""))
            camera_frame = str(camera.get("frame_id", ""))
            try:
                graph_index = int(graph_frame.rsplit(":", 1)[1])
                camera_index = int(camera_frame.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                graph_index = camera_index = -1
            audit = {
                "schema": "3dsg-desktop-live-audit/v1",
                "run_id": window.run_id,
                "captured_while_runner_active": window.process.state() != QtCore.QProcess.NotRunning,
                "elapsed_s": elapsed,
                "camera_frame_id": camera_frame,
                "nav_frame_id": navigation.get("as_of_frame_id"),
                "graph_frame_id": graph_frame,
                "camera_frame_index": camera.get("frame_index"),
                "nav_frame_index": navigation.get("as_of_frame_index"),
                "graph_frame_index": graph_index,
                "graph_lag_frames": (camera_index - graph_index
                                     if camera_index >= graph_index >= 0 else None),
                "graph_revision": graph.get("revision"),
                "graph_source_capture_monotonic_s": graph.get("source_capture_monotonic_s"),
                "graph_source_capture_monotonic_domain": graph.get(
                    "source_capture_monotonic_domain"),
                "graph_source_age_s": (
                    max(0.0, time.monotonic() - float(graph["source_capture_monotonic_s"]))
                    if isinstance(graph.get("source_capture_monotonic_s"), (int, float))
                    else None),
                "graph_publication_age_s": (
                    max(0.0, time.monotonic() - float(graph["host_monotonic_s"]))
                    if isinstance(graph.get("host_monotonic_s"), (int, float))
                    else None),
                "graph_status": graph.get("status"),
                "graph_node_count": len(graph.get("nodes", [])),
                "graph_edge_count": len(graph.get("edges", [])),
                "rgb_loaded": window.viewer._rgb is not None,
                "occupancy_loaded": window.viewer._map is not None,
                "graph_loaded": bool(graph.get("revision", -1) >= 0),
                "snapshot_hash_mismatch": bool(window.viewer._live_snapshot_mismatch),
                "screenshot_saved": bool(image_ok),
                "screenshot_path": str(image_path),
            }
            report_path.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
            capture_ok = bool(image_ok and audit["captured_while_runner_active"]
                              and audit["rgb_loaded"] and audit["occupancy_loaded"]
                              and audit["graph_loaded"] and not audit["snapshot_hash_mismatch"]
                              and camera_index >= graph_index >= 0)
            print(json.dumps(audit, sort_keys=True), flush=True)
            captured = True
            window.stop_run()
        if captured and window.process.state() == QtCore.QProcess.NotRunning:
            app.quit()
        elif time.monotonic() - started > args.duration_s * 2.0 + 90.0:
            window.stop_run()
            if window.process.state() == QtCore.QProcess.NotRunning:
                app.quit()

    timer = QtCore.QTimer()
    timer.timeout.connect(poll)
    timer.start(200)
    app.exec_()
    return 0 if captured and capture_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
