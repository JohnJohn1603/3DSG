import json

import numpy as np
import pytest

from scripts.project_detection_masks_to_3d import cluster_components, cluster_largest
from scripts.run_habitat_native_replay import _load_detection_cache


def test_detection_cache_preserves_empty_present_and_rejects_duplicate_frame(tmp_path):
    path = tmp_path / "detections.jsonl"
    empty = {"provider_id": "p", "frame_id": "f0", "timestamp_s": 0.0, "detections": [], "provenance": {}}
    present = {"provider_id": "p", "frame_id": "f1", "timestamp_s": 1.0, "detections": [{
        "detection_id": "d1", "timestamp_s": 1.0, "center_W": [1.0, 2.0, 3.0],
        "center_method": "fixture", "class_name": "sofa", "class_id": "57",
        "confidence": 0.8, "frame_id": "world", "layer_id": "floor_0", "extents_m": [1, 1, 1],
        "provider_metadata": {"mask_file": "m.npy"},
    }], "provenance": {"semantic_gt_used": False}}
    path.write_text(json.dumps(empty) + "\n" + json.dumps(present) + "\n", encoding="utf-8")
    loaded = _load_detection_cache(path)
    assert loaded["f0"].status == "empty"
    assert loaded["f1"].status == "present"
    assert loaded["f1"].detections[0].class_name == "sofa"
    path.write_text(json.dumps(empty) + "\n" + json.dumps(empty) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate detection batch"):
        _load_detection_cache(path)


def test_source_style_largest_cluster_is_deterministic():
    points = np.array([[0.0, 0.0, 0.0], [0.005, 0.0, 0.0], [1.0, 1.0, 1.0]], dtype=np.float64)
    assert cluster_largest(points, 0.02).tolist() == [0, 1]
    assert [len(component) for component in cluster_components(points, 0.02)] == [2, 1]
