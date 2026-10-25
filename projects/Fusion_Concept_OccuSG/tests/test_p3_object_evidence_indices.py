import numpy as np

from scripts.project_detection_masks_to_3d import record_sidecar_failure, sampled_valid_pixel_indices


def test_selected_pixel_indices_follow_backproject_stride_and_validity_order():
    depth = np.asarray([[1.0, 9.0, 2.0, 3.0],
                        [4.0, 5.0, 6.0, 7.0],
                        [8.0, 9.0, 0.05, 1.5],
                        [1.0, 1.0, 1.0, 1.0]], dtype=np.float32)
    mask = np.ones(depth.shape, dtype=bool)
    mask[0, 2] = False
    got = sampled_valid_pixel_indices(depth, mask, stride=2, min_depth_m=0.1, max_depth_m=5.0)
    assert got.tolist() == [[0, 0]]  # (0,2) is masked; (2,2) is below min depth.


def test_selected_pixel_indices_reject_invalid_and_out_of_range_depth():
    depth = np.asarray([[np.nan, 0.05], [10.1, 1.0]], dtype=np.float32)
    mask = np.ones(depth.shape, dtype=bool)
    got = sampled_valid_pixel_indices(depth, mask, stride=1, min_depth_m=0.1, max_depth_m=10.0)
    assert got.tolist() == [[1, 1]]


def test_optional_evidence_failure_is_recorded_separately_from_p3_status():
    errors = []
    record_sidecar_failure(errors, "frame-0001:det-0002", OSError("disk full"))
    assert errors == [{"observation_id": "frame-0001:det-0002", "error": "OSError: disk full"}]
