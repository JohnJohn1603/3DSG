# Audit membership dining_table — ReplicaCAD apt_0

Ngày: 2026-10-02  
Nguồn: `artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001`  
Phạm vi: audit chỉ đọc trên replay delta, M3 grid trace và perception ledger.
Không thay đổi baseline, mapping, M4, M6 hoặc M8.

## Artifacts

Kết quả nằm trong thư mục `membership_audit/` của run:

- `membership_events.json/csv`: sự kiện gán/đổi membership theo revision;
- `dining_table_membership_events.png`: overlay trước/sau cùng world scale;
- `p3_review_sheet.png`: mẫu accepted/rejected với mask trên RGB;
- `membership_annotation_template.csv`: biểu mẫu annotation room/object độc lập;
- `p3_annotation_template.csv`: biểu mẫu đánh giá detection thật/reject đúng;
- `manifest.json`: provenance và giới hạn.

## Kết quả chính

Hai ObjectNode được kiểm tra:

- `object-000002` (`dining_table`):
  - frame 2: gán ban đầu `room:3`;
  - frame 2 → 3: đổi `room:3 → room:2`;
  - frame 4 → 5: đổi `room:2 → room:3`;
- `object-000012` (`dining_table`):
  - frame 19: gán ban đầu `room:1`;
  - frame 22 → 23: đổi `room:1 → room:3`.

Ba lần đổi membership đều có `m4_backend_status=success`, không stale.
Không có bằng chứng object bị đổi ID tại các sự kiện này.

### Diễn giải theo evidence hiện có

1. `object-000002`, frame 2 → 3:
   center/extent và `last_detection_id` không đổi; không có detection mới.
   Delta chỉ ghi `regions_accepted`; ownership đổi cùng lúc với cập nhật room
   footprint. Đây là bằng chứng nghiêng về biến động region/ownership, chưa phải
   object identity drift.

2. `object-000002`, frame 4 → 5:
   có detection mới `replica-cad-0005:det-0000`, nhưng ObjectNode vẫn giữ
   geometry first-observation theo contract association. Delta ghi thêm
   `stale_room_ownership_reconciled`; membership trở lại `room:3`. Cần annotation
   độc lập để biết đây là sửa ownership hợp lệ hay dao động biên.

3. `object-000012`, frame 22 → 23:
   không có detection mới; center/extent và provenance vẫn từ frame 19. Delta ghi
   `rooms_promoted`, region count tăng từ 2 lên 5, membership đổi `room:1 → room:3`.
   Đây là bằng chứng mạnh hơn cho thay đổi region/room state, không phải quan sát
   object mới.

### Nguyên nhân cơ học đã xác định được

Các nguyên nhân dưới đây mô tả cơ chế của implementation, không phải kết luận
rằng room assignment đúng hay sai trong môi trường thật:

- **Frame 2 → 3, `object-000002`:** M4 trả accepted incremental snapshot mới.
  Không có detection mới và object center/provenance giữ nguyên. M8 gọi lại
  `RegionMembershipResolver` trên polygon/region hiện hành; resolver không có
  hysteresis và có thể chọn candidate mới khi tập polygon/anchor đổi. Room cũ
  `room:3` bị thay bởi ownership hiện hành `room:2`. Artifact hiện không ghi đủ
  predicate `inside/boundary/tie` để khẳng định nhánh hình học cụ thể hơn.
- **Frame 4 → 5, `object-000002`:** Có detection mới nhưng M6 vẫn giữ cùng
  ObjectNode theo association; graph giữ first-observation geometry. Sau khi M8
  resolver chọn `room:3`, builder phát hiện edge `room_contains` cũ của room stale
  `room:2` xung đột với ownership hiện hành và phát sự kiện
  `stale_room_ownership_reconciled`, chỉ suppress edge xung đột để giữ một owner.
  Đây là cơ chế nhất quán graph, chưa chứng minh room:3 là room đúng.
- **Frame 22 → 23, `object-000012`:** Không có detection mới; M4 accepted snapshot
  làm số region tăng từ 2 lên 5 và M8 ghi `rooms_promoted`. Resolver đánh giá lại
  object center/provenance cũ trên topology region mới, nên ownership hiện hành
  chuyển từ `room:1` sang `room:3`. Cơ chế xác định được là promotion +
  re-evaluation, không phải object evidence mới; tính đúng của ranh giới vẫn
  `uncertain`.

Ba mục trên đã chốt phần cơ học. Phần đánh giá “đúng phòng” vẫn provisional và
không dùng để thay đổi baseline.

## P3 review

Đã tạo mẫu 16 detection gồm accepted và rejected. Các reject trong mẫu chủ yếu
do `rejected_cluster_below_minimum`; largest cluster có từ 1 đến 8 điểm ở các
trường hợp representative. Đây mới là review sheet, chưa phải annotation chất
lượng: cần người kiểm tra độc lập điền `p3_annotation_template.csv`.

## Giới hạn

- Chưa có room/object GT độc lập, nên chưa thể gọi các membership change là đúng
  hoặc sai.
- Không thêm hysteresis hoặc khóa object vào room đầu tiên.
- Overlay dùng đúng grid M3 đã ghi theo frame và graph state tái dựng từ delta;
  không project lại và không đưa GT vào pipeline.
- Nhãn AI-assisted/provisional không phải room/object GT độc lập.
- Chưa có lỗi implementation được xác nhận từ review này; baseline được giữ nguyên.
- Hướng dẫn review: `docs/MEMBERSHIP_REVIEW_GUIDE_20261002.vi.md`.
- `py_compile` cho audit tool và review renderer đã pass; full regression gần nhất
  sau hardening/review là 256 passed.

## Checkpoint kỹ thuật

**Đạt checkpoint kỹ thuật của gói review.** Đã có đủ ba membership events và 16
mẫu P3 để xem xét, cùng artifact RGB/mask/overlay và proposal AI-assisted.
Annotation chất lượng vẫn mở, nhưng không phải điều kiện chặn toàn dự án. Chỉ
đề xuất thay đổi khi có lỗi được người review xác nhận; nếu không, tiếp tục dùng
baseline với các giới hạn đã nêu.
