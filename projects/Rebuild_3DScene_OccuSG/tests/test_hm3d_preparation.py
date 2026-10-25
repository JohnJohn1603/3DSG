from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_hm3d_minival import inventory, semantic_descriptor_inventory


ROOT = Path(__file__).resolve().parents[1]


def test_hm3d_inventory_distinguishes_missing_and_present_semantics(tmp_path):
    dataset = tmp_path / "hm3d"
    scene_a = dataset / "minival" / "00001-scene-a"
    scene_b = dataset / "minival" / "00002-scene-b"
    scene_a.mkdir(parents=True)
    scene_b.mkdir(parents=True)
    for scene_dir, scene_name in ((scene_a, "00001-scene-a"), (scene_b, "00002-scene-b")):
        (scene_dir / (scene_name + ".basis.glb")).write_bytes(b"mesh")
        (scene_dir / (scene_name + ".basis.navmesh")).write_bytes(b"nav")
    (scene_b / "00002-scene-b.semantic.glb").write_bytes(b"semantic-mesh")
    (scene_b / "00002-scene-b.semantic.txt").write_text("descriptor fixture\n", encoding="utf-8")
    config = dataset / "hm3d_annotated_basis.scene_dataset_config.json"
    config.write_text(json.dumps({"scene": "minival/00001-scene-a/00001-scene-a.basis.glb"}), encoding="utf-8")

    report = inventory(dataset)

    assert report["scene_count_basis_glb"] == 2
    assert report["semantic_annotation_presence_counts"] == {
        "semantic_annotation_not_found": 1,
        "semantic_mesh_and_descriptor_present": 1,
    }
    assert report["unresolved_config_path_references"] == []
    scene_without_semantics = next(item for item in report["scenes"] if item["scene_id_candidate"] == scene_a.name)
    assert scene_without_semantics["navmesh"]["present"] is True
    assert scene_without_semantics["semantic_annotation_status"] == "semantic_annotation_not_found"


def test_semantic_descriptor_inventory_maps_instances_to_object_categories_and_regions(tmp_path):
    descriptor = tmp_path / "fixture.semantic.txt"
    descriptor.write_text(
        'HM3D Semantic Annotations\n1,FFFFFF,"wall",3\n2,000000,"door",3\n3,AAAAAA,"chair",4\n',
        encoding="utf-8",
    )
    summary = semantic_descriptor_inventory(descriptor)
    assert summary["record_count"] == 3
    assert summary["unique_instance_id_count"] == 3
    assert summary["unique_region_id_count"] == 2
    assert summary["class_counts"] == {"chair": 1, "door": 1, "wall": 1}
    assert summary["region_object_ids"] == {"3": ["1", "2"], "4": ["3"]}
    assert summary["room_polygon_or_boundary_ground_truth_in_descriptor"] is False


def test_hm3d_template_is_unselected_and_keeps_semantic_gt_out_of_predictor():
    lock = json.loads((ROOT / "artifacts/runs/hm3d_integration_20261005/baseline_lock.json").read_text())
    route = json.loads((ROOT / "configs/habitat/m12_replica_cad_apt0_perception_dev_route.json").read_text())
    template = json.loads((ROOT / "configs/habitat/hm3d_minival_v02_preview_template.json").read_text())

    assert template["scene_id"] is None
    assert template["route"] == []
    assert template["semantic_evaluator_only"]["allowed_for_predictor"] is False
    assert template["predictor"]["semantic_gt_input"] is False
    for key in ("resolution_px", "horizontal_fov_deg", "sensor_position_habitat",
                "sensor_orientation_habitat_rad", "min_depth_m", "max_depth_m"):
        assert template[key] == route[key]
    assert lock["status"] == "locked_read_only"
    assert lock["detector_or_p3_m6_or_segmentation_modified"] is False
    assert lock["semantic_score_weights_or_thresholds_modified"] is False
