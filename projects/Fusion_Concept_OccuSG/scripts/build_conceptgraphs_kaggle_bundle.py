#!/usr/bin/env python3
"""Package the versioned runner/notebook files; does not upload or include models/data."""
from __future__ import annotations
import argparse, hashlib, json, zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
COMMON_FILES=["integration_v2/__init__.py","integration_v2/capture_package.py","integration_v2/conceptgraphs_adapter.py",
       "integration_v2/object_export_contract.py","scripts/conceptgraphs_kaggle_runner.py",
       "scripts/check_conceptgraphs_dataset_adapter.py","scripts/validate_conceptgraphs_package.py",
       "scripts/validate_conceptgraphs_export.py","scripts/archive_conceptgraphs_package.py",
       "locks/integration_v2/cg_kaggle_environment.lock.json",
       "docs/CONCEPTGRAPHS_MINIMAL_CORE_MAP.vi.md",
       "docs/R3_KAGGLE_EXECUTION_CHECKLIST.vi.md",
       "docs/R3_KAGGLE_BUNDLE_V9_CHANGELOG.vi.md",
       "docs/R3_KAGGLE_BUNDLE_V10_CHANGELOG.vi.md",
       "docs/R3_KAGGLE_BUNDLE_V11_CHANGELOG.vi.md"]
VERSION_FILES={
  11:["notebooks/conceptgraphs_detect_q1_00808.ipynb"],
  12:["notebooks/conceptgraphs_detect_q1_00808_v12.ipynb",
      "docs/R3_KAGGLE_BUNDLE_V12_CHANGELOG.vi.md"],
}
def main():
 p=argparse.ArgumentParser(); p.add_argument("--version",type=int,choices=sorted(VERSION_FILES),default=11); p.add_argument("--output",type=Path); a=p.parse_args()
 if a.output is None: a.output=ROOT/f"artifacts/integration_v2/kaggle_bundle/conceptgraphs_r2_bundle_v{a.version}.zip"
 files=COMMON_FILES+VERSION_FILES[a.version]
 a.output.parent.mkdir(parents=True,exist_ok=True)
 if a.output.exists(): raise FileExistsError(f"choose a new versioned bundle path: {a.output}")
 with zipfile.ZipFile(a.output,"x",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for rel in files: z.write(ROOT/rel,arcname=f"conceptgraphs_r2_bundle/{rel}")
 digest=hashlib.sha256(a.output.read_bytes()).hexdigest()
 manifest={"bundle":a.output.stem,"sha256":digest,"includes":files,
           "excludes":["capture data package","model checkpoints","credentials","reference repositories"]}
 (a.output.parent/(a.output.stem+".manifest.json")).write_text(json.dumps(manifest,indent=2)+"\n")
 print(json.dumps({"bundle":str(a.output.resolve()),"bytes":a.output.stat().st_size,"sha256":digest,"manifest":manifest},indent=2))
if __name__=="__main__": main()
