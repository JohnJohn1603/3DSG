#!/usr/bin/env python3
"""D1 lockstep live-processing viewer over an already completed C2 cache."""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from integration_v2.live_processing import run_live_cached  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True, help="completed C2 run ID supplying locked cached inputs")
    parser.add_argument("--run-id", required=True, help="new D1 output run ID")
    parser.add_argument("--port", type=int, default=8766, help="localhost viewer port; busy ports are never replaced")
    parser.add_argument("--viewer-base-url", help="reuse an existing read-only loopback workspace server; never starts or replaces it")
    parser.add_argument("--timeout", type=float, default=3600.0, help="bounded overall run timeout in seconds")
    parser.add_argument("--viewer-ready-timeout", type=float, default=600.0,
                        help="maximum seconds to wait for explicit start-live after D2 page is checked")
    parser.add_argument("--xyz-viewer", action="store_true",
                        help="publish D2 XYZ before producers start and finalize its manifest after D1 completes")
    args = parser.parse_args()
    try:
        version = "live" if args.xyz_viewer else None
        result = run_live_cached(args.source_run_id, run_id=args.run_id, port=args.port,
                                 viewer_base_url=args.viewer_base_url,
                                 overall_timeout_s=args.timeout,
                                 xyz_viewer_version=version,
                                 viewer_ready_timeout_s=args.viewer_ready_timeout)
    except KeyboardInterrupt:
        print("D1 cancelled; partial artifacts and failed/cancelled manifest were retained.", file=sys.stderr)
        return 130
    except BaseException as exc:
        run_root = ROOT / "artifacts/live_processing_runs" / args.run_id
        print(f"D1 failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"Partial artifacts: {run_root}", file=sys.stderr)
        if (run_root / "run_manifest.json").is_file():
            print(f"Manifest: {run_root / 'run_manifest.json'}", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "run_id": result["run_id"],
                      "viewer_url": result["viewer"].get("url"),
                      "run_manifest": str(ROOT / "artifacts/live_processing_runs" / args.run_id / "run_manifest.json"),
                      "frames": result.get("summary", {}).get("frame_count")}, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
