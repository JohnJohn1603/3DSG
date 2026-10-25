# Mốc B — unified incremental graph trên HM3D 00802

Ngày: 2026-10-16  
Phạm vi: tích hợp local read-only trên artifacts M0/M1 và A.1 đã có. Không
chạy mapping, perception, native backend, Qwen hoặc capture.

## Trạng thái gate A/B

- **A — một phần:** causal object prefixes, room lifecycle và frame-aligned
  controller state đã có từ A.1. Spatial scope của room polygons không đủ
  bằng chứng trên cache 00802; gate membership vẫn đóng. Scope Bridge giữ
  nguyên kết quả `C_scope_evidence_insufficient`. Direct-Z fields từ binary
  replay cũ không hợp lệ; interval-to-M3 join được kiểm tra riêng; single-
  interval gate fail không chứng minh scene nhiều tầng.
- **B — đạt cho integration/viewer contract:** objects, rooms, lifecycle,
  admission/semantic evidence status, membership status/reason và provenance
  được ghi trong unified graph revision tương ứng. Run thật giữ trạng thái
  membership/semantics unavailable. Fixture verified-scope kiểm tra đường
  membership và room belief, không chứng minh quality của cache.
- **Semantic end-to-end trên dữ liệu thật: pending.** Không có real
  `room_contains` hoặc room label nào được tạo khi elevation scope chưa xác
  minh.

## Artifacts

- Unified timeline: `artifacts/scene_graph_timeline/unified_b_00802_v3/`
  — 29 immutable revisions, scene `00802-wcojb4TFT35`, sequence
  `hm3d-v02-00802-main-route-01`.
- Viewer package:
  `artifacts/scene_graph_timeline/unified_b_00802_viewer_v4/`.
- Manifest SHA-256: unified graph
  `70c6c3553d28bfbe30405a1ddb9edeaf35fc1ca840f5341729ddb38f2baf8100`;
  viewer
  `5a88ddbff41f86e86b8ad703574ab3044a1146743bbdd6f7cc1dea7529152b02`.
- Unified timeline manifest ghi object/room source manifests, từng revision,
  hashes, frame IDs, causality riêng cho objects/rooms/membership/semantics,
  `default_scope_policy=verified_only` và `final_postprocess_included=false`.
- Final revision có 33 object hypotheses, 5 active room nodes và 6 room-graph
  edges; không có `room_contains`. Tất cả 29 revisions ghi membership
  `unavailable_elevation_scope` và room semantics unavailable do membership
  scope. Room geometry/adjacency được giữ từ revision nguồn.
- Cloud NPZ nguồn không bị sửa. Unified state tham chiếu geometry path/hash/
  revision; viewer sinh JSON display geometry riêng, tối đa 300 điểm/object.

## Causality và stage semantics

Object nodes lấy từ online prefix hypotheses và giữ snapshot hypothesis IDs,
raw class history, observation lineage, detection/frame/pose counts, geometry
revision/ref/hash, prefix admission, semantic consensus, membership và
eligibility reasons. Room nodes lấy đúng room state ref theo frame và kiểm tra
khớp active room lifecycle IDs. Room adjacency lấy từ room revision đó.

Mỗi prefix được kiểm tra `future_observations_used=false`; observations có
frame ID sau frame hiện hành bị từ chối. Unified snapshots là immutable; delta
log chỉ tham chiếu snapshot/hash kế tiếp để seek trực tiếp về revision cũ.
`room_contains` chỉ được thêm nếu causal membership được đánh dấu hợp lệ,
scope verified và edge khớp ownership của object. Trên run 00802 không có
edge nào.

Viewer phân biệt online hypotheses, prefix admission eligibility và final
postprocess riêng. Final filter/merge không được dùng để sửa trạng thái quá
khứ. Room ID hiển thị độc lập với semantic label; khi membership unavailable,
label/beliefs/candidates để null và lý do là
`membership_unavailable_elevation_scope`, không đổi thành unknown/no-evidence.

## Kiểm chứng đã chạy

- `tests/test_unified_incremental_timeline.py`,
  `tests/test_causal_timeline_viewer.py`, `tests/test_hybrid_timeline.py`:
  **9 passed**.
- `node --check` trên JavaScript viewer: pass.
- Tái dựng từng revision từ đúng source rows bằng pure builder cho kết quả
  bằng snapshot đã serialize; delta/frame/state hashes và revision chain hợp
  lệch; object/room edges không dangling.
- Fixture có explicit verified scope kiểm tra object A→B membership transfer,
  retract belief ở room A, cập nhật room B, rồi room B retire và không để lại
  edge/evidence dangling. Đây là contract test tổng hợp, không phải đánh giá
  object/room quality.
- Browser mở thực tế: frame đầu (1/29), giữa (15/29), cuối (29/29), seek lùi
  (3/29), play rồi pause; RGB/mask/overlay/crop tải được. Chọn room trong
  room list mở chi tiết ID, geometry revision, unavailable reason và variant.
  Membership edge count luôn 0 trên cache thật.
- Build unified timeline và viewer mất khoảng 1 giây và 3 giây tương ứng trên
  máy local lần chạy này; viewer package khoảng 19 MB, unified graph khoảng
  6.7 MB. Không có peak RSS instrumentation cho hai lệnh build.

Lệnh targeted tests đã chạy:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_unified_incremental_timeline.py \
  tests/test_causal_timeline_viewer.py \
  tests/test_hybrid_timeline.py \
  --basetemp /tmp/unified-b-tests-r5
```

## Lệnh tái lập

Build output mới (builder từ chối ghi đè thư mục output không rỗng):

```bash
python3 scripts/build_unified_incremental_timeline.py \
  --timeline artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline \
  --causal-state artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a1_v6 \
  --output artifacts/scene_graph_timeline/unified_b_00802_<new-run-id>
```

Viewer build cho artifact đã kiểm tra trong lượt này:

```bash
python3 scripts/build_causal_timeline_viewer.py \
  --timeline artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline \
  --unified-timeline artifacts/scene_graph_timeline/unified_b_00802_v3 \
  --output artifacts/scene_graph_timeline/unified_b_00802_viewer_<new-run-id> \
  --max-points 300
```

Serve viewer đã tạo:

```bash
python3 scripts/serve_causal_timeline_viewer.py \
  --root artifacts/scene_graph_timeline \
  --viewer unified_b_00802_viewer_v4 \
  --host 127.0.0.1 --port 8766
```

Mở `http://127.0.0.1:8766/unified_b_00802_viewer_v4/index.html`.
Chọn phòng qua “Rooms at this revision”; chọn object qua danh sách object;
slider/previous/next/play/pause điều khiển prefix. `Reset view` trả góc camera
về mặc định.

## Bằng chứng, giới hạn và bước kế tiếp

Đây là **cached replay viewer**, không phải live processing hay realtime.
Room geometry là native prefix room revisions, nhưng elevation scope của
polygons chưa được chứng minh; vì vậy không thể nối membership/semantic
belief trên dữ liệu thật. Viewer cố ý hiển thị objects và rooms cùng revision
nhưng không vẽ quan hệ không hợp lệ. Fixture kiểm chứng API đường hợp lệ khi
scope explicit verified; nó không mở scope gate của cache.

Mốc B đã đạt ở phần graph/revision integration và biểu diễn unavailable
chính xác. A chưa hoàn tất cho spatial scope; C–E chưa đạt. Bước nhỏ nhất để
bắt đầu C là một `cached_replay` runner mỏng trên các prefix artifacts hiện có:
kiểm tra hashes/scene/sequence, gọi unified builder và viewer builder, rồi
ghi run manifest chung. Runner giữ membership/room semantics unavailable
trên 00802. Mở lại gate A vẫn là điều kiện trước khi gọi kết quả là
object–room end-to-end hợp lệ. Không có thay đổi matcher, bảng A, thresholds,
default, baseline, room geometry hay predictor trong lượt này.
