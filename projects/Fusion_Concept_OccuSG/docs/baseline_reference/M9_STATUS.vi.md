# M9 — SceneGraphBuilder standalone

## Trạng thái

M9 có builder ROS-free tại `src/standalone_sg/pipeline/`, và facade công khai từ `standalone_sg`: `FramePacket`, `SceneGraphBuilder`, `GraphPatch`, `GraphDelta`, `SceneGraphSnapshot`. Kết quả đạt các contract, offline fixture và regression hiện có; đây chưa phải toàn hệ thống Phase 1, không phải Habitat/Webots end-to-end hoặc nghiệm thu cloud.

## Luồng tích hợp

- `FramePacket` mô tả scene/sequence/frame, thứ tự, timestamp, world base pose, một input mode authoritative và optional detections/metadata. Hỗ trợ canonical `grid`, `regions`, `grid_regions`, cùng RGB-D/pointcloud/occupancy qua `MappingStage` được inject. Builder không tự chọn ROS callback hoặc khởi tạo detector.
- `SequenceScheduler` từ chối scene/sequence trộn, frame trùng, frame index không tăng và timestamp giảm trước khi gọi các stage. `flush()` yêu cầu timestamp không giảm, kết thúc sequence và builder từ chối mọi update tiếp theo. Detections `None` giữ nghĩa provider absent; batch rỗng là một lần provider chạy không có detection.
- Mapping/segmentation là constructor-injected. `DudeM5RegionStage` adapter có sẵn để bọc pinned DuDe worker + M5 processor; test M4→M5→M8 doorway đang là integration riêng, không được trình bày nhầm là builder đang chạy Habitat/Webots.
- M6 object association, M7 navigation structural graph và M8 RoomNode/member/adjacency được chiếu thành typed graph nodes/edges. Cạnh `navigable_path` do M7 sở hữu được reconcile theo từng snapshot: cạnh mất bị xóa và có mặt lại được thêm, không cộng dồn. NavNode giữ `first_seen_s` khi ID còn tồn tại. M7 edge không khẳng định robot-footprint passability. Region stale giữ nguyên RoomNode graph evidence (last_seen, observation timestamp/revision và provenance), trong khi processing timestamp chỉ nằm ở snapshot/diagnostics. Membership chạy theo từng accepted/cached snapshot; relink/prune được gate bởi `maintenance_due()` 0,5 Hz. Work bị defer được lưu và terminal flush thực thi maintenance, phát đúng graph delta rồi đóng sequence.
- Mỗi packet hợp lệ tạo revision graph liên tiếp. `GraphPatch` áp dụng chỉ khi base revision khớp; `GraphDelta` nêu add/update/remove và diagnostics; snapshot/delta bất biến. Public graph invariant từ chối dangling/self edges, nhiều room owner trực tiếp cho cùng entity và nhiều room neo cùng region. `close()` idempotent và đóng stage có `close`.
- Deterministic canonical graph/event hashes loại wall-clock timestamp/environment metadata; cùng fixture replay cho hash trùng.

## Kiểm thử

- `tests/test_m9_builder.py`: **25 passed** — mode contract/order, navigation edge loss/restore, timestamp/provenance, M8 membership-vs-maintenance cadence, terminal flush + lifecycle, mutation-then-failure replay-required, pre-stage validation, detection absent-vs-empty, four graph layers, immutability, deterministic hashes, and atomic delta-failure cases.
- `tests/test_m7_m8_contract.py`: **9 passed**; `tests/test_m4_m5_m8_integration.py`: **1 passed**. Các test này xác nhận NavNode thật qua adapter M7→M8 và doorway sequence với DuDe worker/M5/M8; entity metadata là synthetic.
- Latest full regression `PYTHONPATH=src python3 -m pytest -q`: **195 passed, 0 skipped** (32.69 s; M9/M10 reader and point-cloud/RGB-D M11 smokes included).

## Giới hạn và gates còn mở

Candidate graph, GraphPatch, snapshot, GraphDelta và scheduler cursor được dựng/kiểm tra hoàn tất trước khi thay public state; lỗi tạo delta ở update hoặc flush giữ snapshot/revision/scheduler cursor đã commit gần nhất. Metadata đầu vào chỉ nhận JSON-native (key string, không NaN/Inf; reject kiểu không hỗ trợ) trước stage; canonical serializer không ép kiểu key và từ chối set. Backend state bên trong native DuDe/M5 hoặc managers M6–M8 không được rollback. Nếu exception xảy ra sau khi một stage có thể đã mutate, snapshot graph commit gần nhất được giữ nguyên và builder vào trạng thái failed/replay-required; mọi update/flush tiếp theo bị từ chối. `close()` vẫn dùng được; hồi phục bằng builder instance mới và replay. Lỗi packet/scheduler validation trước stage không poison instance.

Mapping từ RGB-D, point cloud hoặc occupancy vẫn là provider được inject. Đã có smoke builder native M2→M3→M4→M10 riêng cho point-cloud và synthetic depth-channel; chưa có RGB/color capture, Habitat/Webots hay simulator end-to-end. Native full-refresh/partial-update caveats M3, M4 outer-rings-only/no real-hole evidence, M4 recovery fixture quay về partial grid, cloud validation và các câu hỏi quyền DuDe/mathtool/FIST vẫn giữ trạng thái mở tương ứng. M10 exports đã tách riêng; M11/Kaggle benchmark chưa làm.
