#!/usr/bin/env python3
"""Create a clearly labelled schema fixture for local adapter/viewer checks."""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"src")]
from integration_v2.unified_scene_graph import _boundary, _room_polygons
from integration_v2.object_export_contract import validate_object_export
from standalone_sg.serialization.json_graph import load_snapshot
from integration_v2.capture_package import sha256_file

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--room-snapshot",required=True)
    ap.add_argument("--out",required=True)
    args=ap.parse_args()
    room_path=Path(args.room_snapshot).resolve(); out=Path(args.out).resolve()
    if out.exists():
        manifest=validate_object_export(out/"object_export")
        if manifest.get("provenance",{}).get("source_kind") != "fixture":
            raise FileExistsError(f"refusing to overwrite non-fixture input: {out}")
        print(json.dumps({"fixture":str(out/"object_export"),"source_kind":"fixture","reused_existing":True,"claim":"not ConceptGraphs inference output"},indent=2))
        return
    snapshot=load_snapshot(room_path); rooms=_room_polygons(snapshot)
    room,poly=rooms[0]
    x0,y0,x1,y1=room.attributes.get("bounds_xy",(min(p[0] for p in poly.outer),min(p[1] for p in poly.outer),max(p[0] for p in poly.outer),max(p[1] for p in poly.outer)))
    inside=None
    for ix in range(1,80):
        for iy in range(1,80):
            p=(x0+(x1-x0)*ix/80,y0+(y1-y0)*iy/80)
            if __import__("standalone_sg.rooms.geometry",fromlist=["point_in_polygon"]).point_in_polygon(p,poly) and not _boundary(p,poly) and min(p[0]-x0,x1-p[0],p[1]-y0,y1-p[1])>.15:
                inside=p; break
        if inside is not None: break
    if inside is None: raise RuntimeError("could not find a clear interior point in the selected room")
    out.mkdir(parents=True)
    export=out/"object_export"; export.mkdir()
    native=out/"fixture_native_map_marker.json"
    native.write_text(json.dumps({"fixture_only":True,"description":"No ConceptGraphs inference was run; marker exists only to exercise export provenance contract."},indent=2)+"\n")
    specs=[
        ("fixture-object-inside",[inside[0],inside[1],0.7],"fixture chair"),
        ("fixture-object-boundary",[poly.outer[0][0],poly.outer[0][1],0.7],"fixture object"),
        ("fixture-object-outside",[x1+2,y1+2,0.7],"fixture object"),
    ]
    objects=[]; files={}
    for oid,center,label in specs:
        p=export/f"{oid}.npz"; c=np.asarray(center,dtype=np.float32)
        delta=np.array([[-.05,-.05,-.05],[.05,.05,.05]],dtype=np.float32)
        np.savez(p,xyz=c[None,:]+delta)
        files[p.name]=sha256_file(p)
        objects.append({"export_id":oid,"cloud_npz":p.name,"bounds_minmax":[(c+delta.min(0)).tolist(),(c+delta.max(0)).tolist()],
            "membership_status":"available","raw_label":label,"fused_semantic_label":None,"fused_semantic_status":"unavailable",
            "first_seen_s":snapshot.timestamp_s or 0.0,"last_seen_s":snapshot.timestamp_s or 0.0})
    manifest={"schema":"conceptgraphs-object-export/v1","identity_scope":"final_map_only_no_persistent_lifecycle_claim",
        "coordinate_frame":"conceptgraphs_world_from_capture_contract","units":"m","scene_id":snapshot.scene_id,"sequence_id":snapshot.sequence_id,
        "source_run_id":room_path.parent.parent.name if room_path.parent.name=="replay" else None,
        "provenance":{"source_kind":"fixture","note":"Schema/adapter demo fixture; not ConceptGraphs output."},
        "files_sha256":files,"native_map_file":native.name,"native_map_sha256":sha256_file(native),
        "T_capture_world_from_cg_world":np.eye(4).tolist(),"objects":objects}
    (export/"export_manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"fixture":str(export),"source_kind":"fixture","scene_id":snapshot.scene_id,"sequence_id":snapshot.sequence_id,"objects":len(objects),"claim":"not ConceptGraphs inference output"},indent=2))
if __name__=="__main__": main()
