# M8 — Region membership và RoomNode anchoring

## Trạng thái

M8 đã triển khai như core standalone ROS-free trong `src/standalone_sg/rooms/`; M9 builder hiện tích hợp các API M6–M8 (xem [M9 status](M9_STATUS.vi.md)). `CanonicalRegion` là geometric region; chỉ tạo RoomNode khi region có direct agent/object/navigation member theo đúng current snapshot. Không thêm semantic room label.

## Reference behavior và interfaces

Source OccuSG `2bca2fa06af87fd9dd0542957039be8f580f0dca`: `RegionManager.gather_region_member_ids/_resolve_tracker_region_for_node` và `RoomManager` promotion, `associate_room_with_tracker_region`, membership sync, replacement relink, prune, adjacency. Boundary epsilon `0.15 m`; point candidates ưu tiên interior, previous membership, polygon distance, centroid distance, region ID. NAV candidates ưu tiên previous membership, footprint overlap ratio/area, boundary distance, centroid distance, prior-neighbour support, ID. Direct membership one-to-one: object, agent, navigation.

- Contract công khai bounds thống nhất `(min_x,min_y,max_x,max_y)` như `Polygon2D.bounds`; `RoomEntity` reject tuple sai kiểu/không hữu hạn và footprint suy biến. `room_entity_from_nav_node()` là adapter tường minh, giữ NavNode ID, tâm hình học block và bounds (không chuyển sang representative free-cell center). Regression dùng NavNode M7 thật, gồm diện tích 0.5625 m², tọa độ âm/asymmetric, chọn overlap và room adjacency qua bridge.
- `RoomEntity` là adapter contract nhẹ cho agent/object/navigation positions và optional nav bounds. `RoomAnchoringManager.update` nhận current canonical regions, retained M5 tracks, graph entities và M7 nav edges. GT instance metadata/semantic class không tham gia room assignment.
- Promotion chỉ khi có meaningful member. `RoomNode.polygon` giữ polygon của anchor region; region-ID ↔ room-ID là one-to-one. M4 backend hiện chỉ xuất outer rings nên test không khẳng định holes thật từ M4; geometry contract vẫn xử lý holes nếu upstream thực sự cung cấp.
- Khi anchor vắng trong accepted current snapshot, thử source-style conservative relink trước prune: old-area overlap ≥0.65, member support ≥0.5, centroid fallback yêu cầu support ≥0.75, score margin ≥0.15, mỗi region/room dùng tối đa một lần. Nếu input snapshot stale, không relink/prune. Retained-but-not-current M5 track không tự cứu room khi snapshot accepted; source prunes theo current prepared-region IDs. Expired track IDs được truyền tường minh riêng.
- Room adjacency là giao của region adjacency hiện hành với cross-region M7 navigation bridge, và yêu cầu hai region đều có room; output loại self-loop/dangling references. Đây là structural/source relation, không phải robot passability.
- `maintenance_due()` giữ cadence source 0.5 Hz và cho phép terminal flush ngay; M9 gọi manager theo packet và terminal flush hiện xuất graph delta, không tự tạo region update mới.

## Kiểm thử và phạm vi

Focused M8: **9 passed**; focused M7→M8 contract: **9 passed**; M4→M5→M8 doorway integration: **1 passed**. Latest full regression after RGB-D smoke: **195 passed, 0 skipped** (`PYTHONPATH=src python3 -m pytest -q`). OccuSG baseline and DuDe kernel were not changed.


`tests/test_m8_rooms.py` covers no-entity/no-promotion, agent promotion, object/nav membership, outer+synthetic-hole geometry, boundary epsilon/sticky tie, nav-overlap tie, one-room-per-region, grow → doorway → second region with symmetric nav bridge, adjacency integrity, missing-vs-retained-vs-stale behavior, successful split successor relink, ambiguous replacement margin rejection/prune, 0.5 Hz maintenance and terminal flush.

Polygon operations are local simple-ring geometry (no Shapely dependency); self-intersecting contour repair/robustness parity with upstream Shapely is not claimed. Relink fixtures test deterministic valid polygon cases, not all contour topologies. No M4 hole ground-truth claim. M7→M8 tests use actual M7 NavNodes; M4→M5→M8 doorway integration uses synthetic agent/object/navigation metadata and is not Habitat/Webots end-to-end. No graph database adapter, semantic labels or multi-view confirmation.
