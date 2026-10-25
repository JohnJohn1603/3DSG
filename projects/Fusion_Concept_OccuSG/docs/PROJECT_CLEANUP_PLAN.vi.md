# Kế hoạch làm gọn Fusion_Concept_OccuSG

Ngày khảo sát: 2026-10-12. Trạng thái: đề xuất, chưa xóa/di chuyển.
Root đích: `/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG`.

Kiến trúc active: Habitat local → ConceptGraphs objects trên Kaggle + OccuSG rooms local → adapter → unified graph. Plan này bổ sung `docs/CLEANUP_MANIFEST_R0_R1.vi.md`, không cho phép reset toàn workspace.

## 1. Kết quả khảo sát thực tế

Dung lượng `du` xấp xỉ, không phải bytes tải xuống hoặc dung lượng có thể xóa:

| Nhóm | Dung lượng | Hành động |
|---|---:|---|
| Toàn workspace | 3,1 GB | Không xóa root |
| artifacts | 2,1 GB | Phân loại dependency trước |
| .venv-appearance | 997 MB | Giữ đến khi ngừng dùng scripts/evaluator phụ thuộc |
| artifacts/runs | 749 MB | Giữ capture/baseline/selected runs |
| artifacts/models | 578 MB | Không xóa model đang lock/reuse |
| artifacts/audits | 357 MB | Giữ annotations và dependency closure |
| artifacts/cache/appearance_wheels | 171 MB | Ứng viên thu hồi sau kiểm tra khả năng tái cài |
| artifacts/object_fusions | 80 MB | Archive chọn lọc sau kiểm tra references |
| artifacts/gt_cache | 65 MB | Giữ; tái tạo có chi phí |
| artifacts/profile_comparisons | 50 MB | Giữ mốc đối chứng, xét bản lặp |
| artifacts/reviews | 45 MB | Giữ annotation sources và gallery provenance |
| docs | 664 KB, 36 file top-level | Dọn điều hướng, không nhằm giải phóng disk |
| scripts | 4,2 MB, 124 file top-level | Tách active/legacy, không xóa theo tên |

Các dòng con nằm trong artifacts; không cộng hai lần. Dung lượng checkpoint R0 khoảng 1,2 MB. Reference repos nhỏ so với dữ liệu và bắt buộc giữ.

## 2. Mục tiêu

1. Người dùng nhìn thấy một kiến trúc, một status và một quickstart active.
2. Không còn các plan cũ được AI hiểu nhầm là nhiệm vụ tiếp theo.
3. Giữ được baseline, dữ liệu pilot Kaggle và các annotations có giá trị.
4. Chỉ xóa thứ thật sự tái tạo/không cần; không hứa tiết kiệm GB khi chưa có manifest.
5. Không gián đoạn task đang viết adapter/notebook hoặc sửa scripts.

## 3. Danh sách bảo vệ

Tối thiểu giữ nguyên:

- `ref_ConceptGraph/`, `ref_OccuSG/`, `locks/`, `native/`, `configs/launcher/`.
- `adapters/habitat/`, room/region/mapping/geometry/contracts/serialization modules và dependencies thật của chúng.
- `artifacts/checkpoints/r0_20261012/` và checkpoint delta mới nếu có thay đổi sau R0.
- `artifacts/runs/q1_00808_20261011T160830Z/`: nguồn pilot Kaggle đang đề xuất; proposal trỏ trực tiếp capture ở đây.
- `artifacts/runs/hm3d_00802_20261011T033053.236654Z_086a73/`: baseline development/capture quan trọng.
- Replay source `u5m9_20261011T035542.851355Z_b39b87` và dedup `u5m9_20261011T035821.873971Z_5aaaf1` khi còn làm reference.
- Mọi human annotations, verbatim responses, case/review manifests và input evidence mà chúng trỏ đến.
- P3 selected-point sidecars, evaluator ledgers và feature caches đang được scripts/tests/manifest active sử dụng.
- `integration_v2/`, Kaggle packages/notebooks và output mới nếu đã tạo.
- Baseline ngoài workspace `/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/`: không đụng đến.

Đây chỉ là seed protection list, không phải toàn bộ dependency closure. Phải mở manifests/configs/imports liên quan để mở rộng danh sách trước deletion. Không follow symlink ra ngoài root để xóa.

## 4. Đợt A — làm gọn điều hướng ngay, không xóa dữ liệu

### Docs

README trỏ tới một nhóm active:

- Kiến trúc và plan `CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md`.
- `STATUS_V2.vi.md`: đang ở stage nào, đã/chưa chạy, blocker cụ thể.
- `QUICKSTART_V2.vi.md`: chỉ commands đã verify; commands planned ghi riêng.
- Hướng dẫn Kaggle khi notebook đã có.
- Cleanup index/manifest và legacy index.

`FUSION_UPGRADE_PLAN`, `OBJECT_GRAPH_IMPROVEMENT_PLAN`, `OBJECT_GRAPH_QUALITY_RECOVERY_PLAN`, `NEXT_AGENT_TASK` và U/K/Q status docs phải được phân loại legacy, không tiếp tục chỉ đạo kiến trúc mới.

Không di chuyển ngay tài liệu có relative links/hard-coded references. Bước đầu thêm một notice ngắn `LEGACY — xem active plan`, tạo `docs/LEGACY_INDEX.vi.md`, bỏ chúng khỏi README active navigation. Có thể ẩn artifacts/cache/legacy khỏi explorer bằng setting riêng nếu user muốn, nhưng đây không phải tiết kiệm disk.

Chỉ chuyển docs vào `docs/legacy/<checkpoint-id>/` sau link scan. Rewrite relative links có tests hoặc giữ redirect stubs ở đường cũ; không để gallery/report links bị gãy. Tài liệu chưa có dependency map thì giữ tại chỗ với notice.

### Code

Tạo danh sách active/legacy modules/entrypoints. Giữ source làm đối chứng, nhưng đường mới không import matcher legacy. Chưa move `scripts/` hoặc `src/` hàng loạt: code nhỏ, rủi ro cao hơn lợi ích disk. Các scripts cũ có thể nằm tại chỗ nhưng được gắn trạng thái legacy trong index.

## 5. Đợt B — cache tái tạo được

Ứng viên đầu tiên:

- `.pytest_cache/`.
- Python `__pycache__` của project theo exact paths trong manifest R0/R1; không quét xóa cả env/reference repos.
- `artifacts/cache/appearance_wheels/` (171 MB) chỉ khi xác nhận không được dùng làm offline package source bắt buộc, dependency pins đã lưu và user chấp nhận cần tải lại khi rebuild env.

Checkpoint trước nếu source mới thay đổi. Xác nhận không có process đang ghi targets. Tạo manifest exact paths + realpaths + size + reason + regeneration command. Sau duyệt mới xóa/trash. Không dùng wildcard recursive để xóa targets chưa enumerate.

Pycache/test cache chỉ tiết kiệm ít; wheels là khoản lớn hơn. Không gộp model weights hoặc embedding caches vào nhóm disposable chỉ vì tên chứa cache.

## 6. Đợt C — artifacts lặp/failed/superseded

Trạng thái failed/superseded không đủ để xóa: run có thể chứa capture được tái sử dụng hoặc là evidence giải thích annotation.

Với từng candidate:

1. Đọc run/evaluation manifests, source-cache pointers, review links và test references.
2. Kiểm tra không thuộc seed protection/dependency closure.
3. Nếu duplicate: so relevant content hashes, không chỉ tên hoặc count.
4. Lưu report, config/input/code hashes, commands, status/error và lý do superseded vào archive index.
5. Backup **payload artifacts** cần giữ; checkpoint R0 chỉ lưu source/docs không thể phục hồi các runs này.
6. Kiểm tra archive hash và restore thử sang thư mục riêng, rồi mới đề xuất bỏ bản gốc.

Thứ tự ưu tiên xét: duplicate renders/contact sheets không còn được annotation tham chiếu; repeat run đã có bản giữ tương đương; intermediate debug outputs. Không xóa raw captures trước derived outputs.

Nén archive trong cùng disk có thể không tiết kiệm nhiều do PNG/NPZ vốn đã nén. Archive ngoài workspace giúp gọn đường dẫn, nhưng chỉ giải phóng disk nếu chuyển sang storage khác hoặc bỏ bản gốc sau kiểm chứng. Không tự tạo hai bản lớn rồi báo tiết kiệm.

## 7. Đợt D — env/model legacy, để sau

`.venv-appearance` (~997 MB) và ViT-B-32 checkpoint (~578 MB) không phải recipe reference mới, nhưng có thể còn dùng cho evaluator/legacy replay. Chỉ cân nhắc retire sau khi:

- Notebook CG và đường integration mới đã hoạt động.
- Đã kiểm tra commands/tests nào còn phụ thuộc.
- Dependency lock, checkpoint metadata/hash, nguồn tải và licenses được giữ.
- User chọn giữ tái lập tại chỗ hay giải phóng disk và chấp nhận phải cài/tải lại.

Đây là khoản tiềm năng khoảng 1,5 GB, **chưa phải dung lượng được phép xóa**. Không xóa shared baseline env/model ngoài workspace.

## 8. Hợp đồng thực thi cleanup

Trước mỗi batch:

- Đợi task đang sửa files checkpoint hoặc thống nhất tránh target overlaps.
- Không dùng archive R0 để ghi đè source mới; tạo checkpoint delta/current state cần thiết.
- Manifest có action `keep / tag-legacy / move / archive / delete`, exact path, realpath, size, references, recovery và verification.
- Trình user duyệt exact move/delete targets; câu hỏi “có nên dọn không” không phải authorization xóa tất cả.
- Không thao tác destructive với root, unresolved variables hoặc glob rộng.

Sau batch:

- Báo files/bytes thật sự loại khỏi disk, không cộng archive vào savings.
- Kiểm tra protected manifests/hashes và pilot package validator nếu đã có.
- Chạy lightweight import/CLI help/link checks bị ảnh hưởng; không chạy capture/perception/native replay chỉ để test cleanup.
- Ghi deletion/move receipt, vị trí archive và hướng dẫn khôi phục; nói rõ thứ không phục hồi được nếu có.

## 9. Lượt triển khai đầu tiên đề nghị

Chỉ Đợt A ở mức index/notices và lập exact candidate manifest Đợt B. Không move/delete material, không archive runs, không retire env/model. Tái sử dụng inventory R0/R1, chỉ bổ sung dữ liệu thay đổi thay vì audit toàn dự án lại.

Sau khi user duyệt manifest cache, thực hiện Đợt B riêng. Đợt C/D không chặn việc xây Kaggle adapter/notebook. Dọn dẹp phục vụ kiến trúc mới, không trở thành một chuỗi audit thay thế implementation.
