import json
from pathlib import Path
import numpy as np
import pytest
from integration_v2.capture_package import sha256_file
from integration_v2.object_export_contract import validate_object_export

def test_final_map_export_contract_requires_hashes_and_scope(tmp_path):
    export=tmp_path/"export"; export.mkdir()
    cloud=export/"object.npz"; np.savez(cloud,xyz=np.array([[0,0,0],[1,2,3]],dtype=np.float32))
    native=tmp_path/"workspace_native.pkl.gz"; native.write_bytes(b"trusted fixture bytes")
    m={"schema":"conceptgraphs-object-export/v1","identity_scope":"final_map_only_no_persistent_lifecycle_claim",
       "coordinate_frame":"conceptgraphs_world_from_capture_contract","units":"m","scene_id":"fixture","sequence_id":"seq",
       "provenance":{"source_kind":"fixture"},"files_sha256":{"object.npz":sha256_file(cloud)},
       "native_map_file":"workspace_native.pkl.gz","native_map_sha256":sha256_file(native),
       "T_capture_world_from_cg_world":np.eye(4).tolist(),
       "objects":[{"export_id":"cg-final-hash","cloud_npz":"object.npz","bounds_minmax":[[0,0,0],[1,2,3]],"membership_status":"available"}]}
    (export/"export_manifest.json").write_text(json.dumps(m))
    assert len(validate_object_export(export)["objects"])==1
    m["identity_scope"]="stable_online_ids"
    (export/"export_manifest.json").write_text(json.dumps(m))
    with pytest.raises(ValueError,match="final-map-only"):
        validate_object_export(export)
