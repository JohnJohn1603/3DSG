import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from integration_v2.capture_package import build_package, sha256_file, validate_package
from integration_v2.conceptgraphs_adapter import (
    backproject_pixel, decode_reference_pose, encode_pose_for_reference,
    project_camera_point, relative_pose_to_capture_world,
)


def test_pose_matches_pinned_hm3d_loader_transform_and_roundtrip():
    angle = 0.31
    T = np.eye(4)
    T[:3, :3] = [[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]
    T[:3, 3] = [1.2, -3.4, 0.8]
    np.testing.assert_allclose(decode_reference_pose(encode_pose_for_reference(T)), T, atol=1e-12)
    relative = np.eye(4); relative[:3, 3] = [0.4, 0.2, 0]
    np.testing.assert_allclose(relative_pose_to_capture_world(relative, T), T @ relative)


def test_reference_pinhole_backproject_project_roundtrip_at_pixel_centers_and_edges():
    K = np.array([[410.0, 0, 319.5], [0, 410.0, 239.5], [0, 0, 1]])
    for u, v in [(0, 0), (639, 479), (319.5, 239.5), (55, 102)]:
        p = backproject_pixel(u, v, 2.25, K)
        uv, z = project_camera_point(p, K)
        np.testing.assert_allclose(uv, [u, v], atol=1e-12)
        assert z == pytest.approx(2.25)


def _fixture_capture(root: Path):
    root.mkdir()
    rows=[]
    for i in range(2):
        d=root/f"frames/{i:06d}"; d.mkdir(parents=True)
        rgb=np.zeros((4,6,3),dtype=np.uint8); rgb[...,0]=i*20
        depth=np.full((4,6),2.0+i,dtype=np.float32); valid=np.ones((4,6),dtype=bool)
        np.save(d/"rgb.npy",rgb); np.save(d/"depth_m.npy",depth); np.save(d/"valid_mask.npy",valid)
        Tbase=np.eye(4); Tbase[0,3]=float(i)
        meta={"frame_id":f"f{i}","frame_index":i,"timestamp_s":i*0.1,"scene_id":"fixture",
              "width":6,"height":4,"hfov_deg":90.0,"calibration_id":"k0","min_depth_m":0.1,
              "max_depth_m":10.0,"depth_units":"m","depth_representation":"axial",
              "canonical_from_habitat":np.eye(4).tolist(),"sensor_position_habitat":[float(i),0,0],
              "sensor_rotation_habitat":np.eye(3).tolist(),"base_pose_canonical":Tbase.tolist(),
              "base_pose_habitat":Tbase.tolist(),"dataset_type":"hm3d","dataset_version":"v0.2",
              "scene_file":"fixture.glb","sequence_id":"seq","navmesh_used_for_route":False,
              "route_entry":{"label":"fixture"}}
        (d/"metadata.json").write_text(json.dumps(meta))
        rows.append({"frame_dir":f"frames/{i:06d}","frame_id":f"f{i}","frame_index":i,"timestamp_s":i*0.1})
    m={"scene_id":"fixture","frames":rows}
    (root/"capture_manifest.json").write_text(json.dumps(m))
    return sha256_file(root/"capture_manifest.json"),["f0","f1"]


def test_build_and_validate_portable_package_and_hash_mismatch(tmp_path):
    capture=tmp_path/"capture"; digest,ids=_fixture_capture(capture)
    output=tmp_path/"package"
    built=build_package(capture,output,expected_capture_sha256=digest,expected_frame_ids=ids)
    checked=validate_package(output)
    assert len(checked["frames"]) == 2
    assert [f["frame_id"] for f in checked["frames"]]==ids
    assert built["frames"][0]["intrinsics"]["cx"] == 2.5
    assert built["frames"][0]["depth_axial_m"].startswith("dataset/")
    assert set(built["frames"][0]["source_raw_files_sha256"]) == {"rgb.npy","depth_m.npy","valid_mask.npy"}
    for i,frame in enumerate(checked["frames"]):
        source_rgb=np.load(capture/f"frames/{i:06d}/rgb.npy",allow_pickle=False)[...,:3]
        with Image.open(output/frame["rgb"]) as im: np.testing.assert_array_equal(np.asarray(im),source_rgb)
        source_depth=np.load(capture/f"frames/{i:06d}/depth_m.npy",allow_pickle=False)
        exported_depth=np.load(output/frame["depth_axial_m"],allow_pickle=False)
        np.testing.assert_array_equal(exported_depth,source_depth)
    assert not any("annotation" in p.lower() or "semantic" in p.lower() for p in built["files_sha256"])
    with (output/built["frames"][0]["depth_axial_m"]).open("ab") as f: f.write(b"x")
    with pytest.raises(ValueError,match="hash mismatch"):
        validate_package(output)


def test_build_rejects_locked_hash_or_order_mismatch(tmp_path):
    capture=tmp_path/"capture"; digest,ids=_fixture_capture(capture)
    with pytest.raises(ValueError,match="manifest SHA"):
        build_package(capture,tmp_path/"bad",expected_capture_sha256="0"*64,expected_frame_ids=ids)
    with pytest.raises(ValueError,match="frame IDs/order"):
        build_package(capture,tmp_path/"bad2",expected_capture_sha256=digest,expected_frame_ids=list(reversed(ids)))


def test_validate_rejects_manifest_calibration_tamper(tmp_path):
    capture=tmp_path/"capture"; digest,ids=_fixture_capture(capture); output=tmp_path/"package"
    build_package(capture,output,expected_capture_sha256=digest,expected_frame_ids=ids)
    p=output/"input_package_manifest.json"; m=json.loads(p.read_text())
    m["frames"][0]["intrinsics"]["cx"]+=0.5; p.write_text(json.dumps(m))
    with pytest.raises(ValueError,match="content hash"):
        validate_package(output)


def test_pinned_reference_loader_pose_conversion_and_mapping_pose_field():
    root=Path(__file__).resolve().parents[1]
    source=(root/"ref_ConceptGraph/concept-graphs/conceptgraph/dataset/datasets_common.py").read_text()
    mapping=(root/"ref_ConceptGraph/concept-graphs/conceptgraph/slam/cfslam_pipeline_batch.py").read_text()
    assert "pose = P @ pose @ P.T" in source
    assert "self.transformed_poses = self._preprocess_poses(self.poses)" in source
    # The pinned object lifting path uses the absolute, loader-converted poses.
    assert "unt_pose = dataset.poses[idx]" in mapping
    assert "trans_pose = adjusted_pose" in mapping
