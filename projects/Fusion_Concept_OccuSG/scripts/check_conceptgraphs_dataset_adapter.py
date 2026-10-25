#!/usr/bin/env python3
"""Run only inside the pinned ConceptGraphs Python environment (Kaggle)."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integration_v2.capture_package import validate_package
from integration_v2.conceptgraphs_adapter import load_reference_dataset,verify_reference_dataset

def main():
 p=argparse.ArgumentParser(); p.add_argument("--package",type=Path,required=True); p.add_argument("--reference",type=Path,required=True); a=p.parse_args()
 m=validate_package(a.package); ds=load_reference_dataset(a.package,a.reference); verify_reference_dataset(ds,m)
 print(json.dumps({"status":"pass","dataset_len":len(ds),"loader":"get_dataset/Hm3dDataset",
                   "pose_used_by_mapping":"dataset.poses absolute","relative_pose_normalization":"disabled in preflight adapter",
                   "occusg_world_from_cg_world":m["pose_contract"]["local_to_capture_world"]},indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())

