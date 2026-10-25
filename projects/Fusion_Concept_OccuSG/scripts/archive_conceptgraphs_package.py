#!/usr/bin/env python3
"""Create a portable ZIP from a validated RGB-D package; does not upload it."""
from __future__ import annotations
import argparse, hashlib, json, zipfile, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from integration_v2.capture_package import validate_package

def main():
    p=argparse.ArgumentParser(); p.add_argument("package",type=Path); p.add_argument("--output",type=Path); a=p.parse_args()
    source=a.package.resolve(); manifest=validate_package(source)
    target=(a.output or source.with_suffix(".zip")).resolve(); target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists(): raise FileExistsError(f"choose a new archive path: {target}")
    with zipfile.ZipFile(target,"x",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for f in sorted(x for x in source.rglob("*") if x.is_file()):
            z.write(f,arcname=f"{source.name}/{f.relative_to(source)}")
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    print(json.dumps({"status":"archived_local_only","archive":str(target),"bytes":target.stat().st_size,
                      "sha256":digest,"package_content_sha256":manifest["content_sha256"],"uploaded":False},indent=2))
    return 0
if __name__=="__main__": raise SystemExit(main())

