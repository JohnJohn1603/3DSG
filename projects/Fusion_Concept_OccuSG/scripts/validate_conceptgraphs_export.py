#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from integration_v2.object_export_contract import validate_object_export
def main():
 p=argparse.ArgumentParser(description="Validate a downloaded ConceptGraphs object export.")
 p.add_argument("export",type=Path); a=p.parse_args(); m=validate_object_export(a.export)
 print(json.dumps({"status":"valid","objects":len(m["objects"]),"coordinate_frame":m["coordinate_frame"],"identity_scope":m["identity_scope"]},indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())

