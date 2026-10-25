#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration_v2.capture_package import build_package, validate_package

PROPOSAL = ROOT / "locks/integration_v2/conceptgraphs_detect_preflight_proposal.json"

def main() -> int:
    p = argparse.ArgumentParser(description="Build the locked Q1 00808 RGB-D package for ConceptGraphs.")
    p.add_argument("--output", type=Path, default=ROOT / "artifacts/integration_v2/packages/q1_00808_cg_v6")
    args = p.parse_args()
    proposal = json.loads(PROPOSAL.read_text())
    cap = proposal["proposed_existing_capture"]
    expected_root = ROOT / cap["capture_root"]
    if args.output.exists():
        existing = validate_package(args.output)
        if (existing.get("source", {}).get("capture_manifest_sha256") != cap["capture_manifest_sha256"]
                or [f["frame_id"] for f in existing.get("frames", [])] != cap["frame_ids_in_order"]):
            raise FileExistsError(f"output exists but does not match this locked capture: {args.output}")
        print(json.dumps({"status":"reused_valid_existing_package","package":str(args.output.resolve()),
                          "content_sha256":existing["content_sha256"],"frames":len(existing["frames"]),
                          "written":False},indent=2))
        return 0
    manifest = build_package(expected_root, args.output,
                             expected_capture_sha256=cap["capture_manifest_sha256"],
                             expected_frame_ids=cap["frame_ids_in_order"])
    validate_package(args.output)
    print(json.dumps({"status": "valid", "package": str(args.output.resolve()),
                      "content_sha256": manifest["content_sha256"],
                      "frames": len(manifest["frames"])}, indent=2))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
