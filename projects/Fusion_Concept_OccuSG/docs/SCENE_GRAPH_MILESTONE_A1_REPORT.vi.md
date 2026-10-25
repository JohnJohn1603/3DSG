# Mốc A.1 — causal correctness và scope provenance

Ngày: 2026-10-15  
Phạm vi: tiếp tục mốc A; chưa triển khai B–E.

## Thay đổi đã triển khai

### Prefix scope thay cho scope toàn timeline

`run_causal_hybrid_room_belief.py` giờ đánh giá scope với các room revisions
và observation/layer metadata có trong prefix hiện tại. Final replay snapshot
chỉ được lưu làm provenance cross-check; nó không mở membership cho prefix
trước đó.

Manifest của mỗi revision có `membership_scope`; manifest tổng có thêm
`membership_scope_by_frame`. Khi layer mới xuất hiện ở frame sau, scope của
prefix trước không bị đổi ngược.

### Room registry theo lifecycle

`RoomBeliefLedger` hỗ trợ `register_room_node()`. Runner khởi tạo ledger
không có catalog physical-room cuối run và đăng ký room khi room revision đó
active. Room state chỉ serialize room active; split/merge identity không được
suy diễn.

### Scope contract có provenance

Scope contract chỉ được mở khi có:

- `verified_single_floor_scope` và đúng một `floor_id`;
- scene/sequence khớp timeline;
- `coordinate_frame` và đơn vị mét;
- `source_refs` là file thật, hash (nếu khai báo) khớp;
- contract còn hiệu lực với prefix;
- prefix layers không mixed và khớp evaluation profile.

Contract thiếu/sai trả `unavailable` với reason cụ thể. Không tạo JSON
`verified` để vượt gate.

### Metadata và transaction

- `room_evidence_eligible` được tính riêng cho object hiện tại.
- Kết quả `apply_frame()` được trả và ghép theo `event_id`, kể cả batch có
  idempotent events; không phụ thuộc thứ tự controller sort.
- Transaction rollback và category cap hiện có được giữ nguyên.

## Đối chiếu room producer trên cache 00802

Các nguồn đã kiểm tra:

- Timeline: `artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline/manifest.json`
- Room delta: `artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/replay/delta_log.json`
- Replay manifest SHA-256:
  `43984bce827c932e4c26e46ce550b3b5eba13ce06c06b3d42be6b94c1aac9b1f`
- Replay config: `configs/reference_code_mp3d.yaml`, SHA-256
  `210124b16fd3651c06fd548a1704f27595f61761dd1bfb4db4099e51895d1523`
- Mapping backend ghi trong replay: `NativeRGBDMappingStage -> native M2/M3`.
- Detection manifest ghi backprojection
  `standalone_sg.geometry.backproject_depth`; `room_gt_used=false`.

Room nodes hiện chứa polygon `outer_ring` trên world XY nhưng không mang
`floor_id`, `z_bounds` hoặc elevation. Cache có `floor_0` trong evaluation
profile và P3 layers đã ghi, nhưng layer metadata chỉ bao phủ 45/67
observation IDs; các giá trị đó cũng không phải scope contract độc lập chứng
minh polygon XY cùng một elevation. Vì vậy run thật vẫn giữ:

```text
scope: unavailable
reason: room_scope_contract_missing_for_2d_polygons
membership: unavailable_elevation_scope
room_contains: none
room_semantics: no_eligible_membership_evidence
```

Đây là blocker provenance còn lại. Không chạy native replay mới và không tự
đặt Z bounds/elevation threshold.

## Artifact và lệnh tái lập

Artifact mới, không ghi đè A/v1:

`artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a1_v6/`

```bash
python3 scripts/run_causal_hybrid_room_belief.py \
  --timeline artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline \
  --a-zip artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip \
  --output artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a1_v6
```

Artifact có 29 prefix revisions, scope theo từng frame, 4 A variants và
không có `room_contains` vì gate chưa mở. Manifest cũng lưu SHA-256 của
runner và RB1 ledger implementation đang dùng.

## Tests

Đã chạy 56 tests:

```bash
python3 -m pytest -q -p no:cacheprovider \
  tests/test_room_belief_v1.py \
  tests/test_hybrid_timeline.py \
  tests/test_causal_hybrid_room_belief.py \
  tests/test_causal_timeline_viewer.py \
  tests/test_conceptgraphs_adapter.py \
  tests/test_unified_scene_graph.py \
  tests/test_causal_correctness_a.py \
  --basetemp /tmp/a1-tests-final
```

Các kiểm tra mới gồm:

- prefix/full-run prefix parity trên runner khi scope unavailable;
- scope hợp lệ mở membership bằng fixture cloud nhỏ;
- layer xuất hiện muộn không làm thay đổi quyết định prefix trước;
- source missing/hash/scene/coordinate mismatch bị từ chối;
- object eligible sau đó object ineligible không kế thừa cờ;
- event order và idempotent replay trả đúng `event_id`;
- registry room tạo theo active revision;
- transaction rollback, category cap và các regression tests cũ.

Kết quả: **56 passed**.

## Finding status

- **P1-A:** đã sửa lifecycle/serialized prefix; catalog physical-room không
  còn khởi tạo từ final run.
- **P1-B:** gate provenance đã chặt hơn; membership run 00802 vẫn blocked vì
  thiếu contract producer có thể xác minh.
- **P1-C:** đã sửa transaction frame và event-result mapping.
- **P2-A:** tiếp tục đạt: config/admission/A digests/revision index động.
- **P2-B:** đã bổ sung các test A.1 nêu trên; test portability/hash matrix
  đầy đủ cho mọi runner input vẫn có thể mở rộng sau.
- **P2-C:** ACTIVE_ARCHITECTURE đã cập nhật trạng thái A.1; lịch sử cũ giữ
  nguyên.
- **P2-D và B–E:** chưa triển khai.

## Bước duy nhất còn cần để bật membership

Room producer cần xuất metadata scope versioned cho cùng scene/sequence:
coordinate frame, đơn vị, spatial/elevation scope áp dụng cho từng room
revision, effective frame/revision, source file và SHA-256. Sau khi metadata
này được khóa và kiểm tra, có thể chạy lại adapter/room-belief trên cache;
không cần đổi matcher, A tables, thresholds hoặc room geometry.
