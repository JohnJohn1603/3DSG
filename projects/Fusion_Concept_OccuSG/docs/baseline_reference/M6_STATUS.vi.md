# M6 — Object association và provider boundary

## Trạng thái

M6 triển khai như module độc lập, ROS-free. Test dùng synthetic/cached detections; chưa tích hợp detector nặng. M6 không tạo RoomNode, không gán room membership và không build `SceneGraphBuilder`.

## Pinned source behavior

Baseline: OccuSG `2bca2fa06af87fd9dd0542957039be8f580f0dca`; source path `src/scene_graph_ros/scene_graph_ros/managers/object_manager.py`, symbols `_detection_label_and_score`, `_get_normalized_class_name`, `_find_or_add`, `_process_single_detection`.

- Adapter cung cấp center đã đổi sang canonical world; source ROS path transform Detection3D pose sang fixed frame trước association. Không đưa TF/ROS vào core. `center_method` phân biệt AABB-center với các nguồn center khác.
- Same-class match: exact normalized `class_name` (`strip().lower()`), nearest candidate trong `spatial_merge_threshold=0.75 m`, tie-break persistent ID. Bản YAML runtime đặt 0.75 m.
- Nếu không có same-class candidate, source chọn nearest same-pose candidate trong `min(spatial_merge_threshold, 0.15 m)`, kể cả class khác hoặc không có label; tie-break ID. Same-class match được ưu tiên trước suppression candidate, dù suppression candidate gần hơn. Candidate chỉ được xét cùng layer và khoảng cách Euclidean 3D.
- Match chỉ cập nhật `last_seen` và last detection/provider provenance. Geometry/center, class, extents và score của ObjectNode giữ giá trị lần quan sát đầu. Không weighted centroid, confidence fusion, multi-view confirmation, class synonym matching hoặc GT instance key.
- `DetectionBatch.detections=None` là provider output absent/cache miss; tuple rỗng là frame provider đã chạy nhưng không phát detection. Cả hai không age/delete objects trong reference. Association evidence là detection evidence, không phải line-of-sight anchor.

## Files và contracts

- [`objects.py`](../src/standalone_sg/contracts/objects.py): `Detection3D` và `DetectionBatch`; center finite, world frame rõ, score giữ raw (không giả calibrated probability), optional extents/provider metadata. Không có GT field trong contract.
- [`cached.py`](../src/standalone_sg/adapters/semantic/cached.py) và `base.py`: provider protocol/cache replay; cache miss trả explicit absent batch.
- [`association.py`](../src/standalone_sg/objects/association.py): persistent ObjectNode state, nearest/tie-break logic và provenance kết quả.
- Config strict `objects.association.same_class_radius_m=0.75`, `same_pose_suppression_cap_m=0.15`.
- [`test_m6_object_association.py`](../tests/test_m6_object_association.py): repeat detections, unrelated classes, close-chair false merge, unlabeled/cross-class suppression, nearest/tie order, 3D/layer, first-observation geometry, empty-vs-absent, cache/provenance, GT metadata non-use và validation.

## Batch atomicity hardening

`ObjectAssociationManager.update` prevalidate frame của toàn bộ detections trước khi bắt đầu association tuần tự. Batch có detection sau sai frame không tạo/match object một phần, không cập nhật last_seen/provenance và không tiêu thụ ID. Regression bao phủ cả manager rỗng lẫn object đã tồn tại; update hợp lệ kế tiếp xác nhận ID liên tục. Đây là hardening contract API; source matching algorithm không đổi.

## Giới hạn baseline và nghiệm thu

- False merge mong đợi: hai ghế cùng class cách nhau dưới 0.75 m có thể thành một node. Cross-class flicker/unlabeled observation trong vùng 0.15 m có thể bị nhập vào node cũ, trong khi class/geometry cũ được giữ lại. Đây là baseline behavior, không được che bằng multi-view fusion.
- Không có direct ROS ObjectManager process test trong runtime vì module ROS-free; source đã audit trực tiếp và fixtures khóa các nhánh/biên cốt lõi. Provider cache mới là test/runtime boundary, chưa có detector adapter cụ thể.
- Focused M6 + config tests: **20 passed** (includes two atomic-batch regressions); full suite after M8: **143 passed, 0 skipped**. M4 sequence tests remain in the full regression and their source/kernel were unchanged. M4 được chấp nhận đúng phạm vi technical-local/reference-parity fixtures/sequences; Kaggle/cloud và quyền vẫn là gate riêng. M7/M8 đã được triển khai riêng (xem [M7 status](M7_STATUS.vi.md), [M8 status](M8_STATUS.vi.md)); M9 chưa triển khai.
