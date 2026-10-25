#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integration_v2.capture_package import validate_package

def main() -> int:
    p=argparse.ArgumentParser(description="Validate local ConceptGraphs RGB-D input package hashes/contracts.")
    p.add_argument("package", type=Path)
    a=p.parse_args()
    m=validate_package(a.package)
    print(json.dumps({"status":"valid", "schema":m["schema"], "frames":len(m["frames"]),
                      "content_sha256":m["content_sha256"]}, indent=2))
    return 0
if __name__ == "__main__": raise SystemExit(main())

