#!/usr/bin/env python3
"""Run the bounded T2-D cached-packet coordinator once."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))

from integration_v2.t2d_coordinator import run_t2d


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True,
                        help="completed C2 run directory supplying capture/P2/native config")
    parser.add_argument("--packet-manifest", type=Path, required=True,
                        help="locked T2-B2 packet_join_manifest.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--viewer-ready-timeout", type=float, default=180.0)
    parser.add_argument("--auto-start", action="store_true",
                        help="headless bootstrap-only start; not GUI acceptance")
    args = parser.parse_args()
    try:
        result = run_t2d(source_run=args.source_run, packet_manifest=args.packet_manifest,
                         run_id=args.run_id, port=args.port,
                         viewer_ready_timeout_s=args.viewer_ready_timeout,
                         auto_start=args.auto_start)
    except KeyboardInterrupt:
        print("T2-D cancelled; partial artifacts were retained.", flush=True)
        return 130
    except BaseException as exc:
        print(f"T2-D failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    print(json.dumps({"status": result.get("status"), "run_id": result.get("run_id"),
                      "manifest": str(Path("artifacts/live_processing_runs") / args.run_id / "run_manifest.json"),
                      "viewer": result.get("viewer", {}),
                      "joined_revision_count": result.get("joined_revision_count")}, indent=2))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
