# Kế hoạch: từ region và object quan sát được đến nhãn phòng

## 1. Mục tiêu, checkpoint và giới hạn

Hoàn thành một module độc lập nhận region đã phân đoạn cùng các object được quan sát bằng RGB-D, xuất nhãn công năng và evidence cho từng region. Chạy thành công và kiểm chứng trên apt_0 trước khi tích hợp sâu hơn vào scene graph.

Không yêu cầu dựng lại graph engine. Dự án đã có M1–M10 và ObjectNode/RoomNode; có thể dùng snapshot hiện có làm adapter đầu vào, nhưng logic semantic mới không được phụ thuộc vào việc xây dựng toàn bộ graph. Kết quả chính là `region_labels.json`, bản đồ nhãn và bảng object evidence.

Giữ segmentation `source`, pose GT, detector/P3/M6 và capture đã khóa. Không tiếp tục tuning bốn phòng GT trong nhiệm vụ này. Không làm navigation, Value Map, SLAM, tự gộp room hoặc sửa ranh giới bằng semantic label.

## 2. Bám OccuSG đến đâu?

Checkout đã kiểm tra: `ref_OccuSG/OccuSG`, commit `2bca2fa06af87fd9dd0542957039be8f580f0dca`.

Các nguồn cần đọc khi triển khai:

- `src/scene_graph_ros/scene_graph_ros/managers/room_manager.py`: room anchor, direct object membership, semantic signatures; `next_room_name()` trả `room_<count>`.
- `src/scene_graph_ros/scene_graph_ros/managers/semantic_signature_utils.py`: semantic token `(class_name, round(x), round(y))`, mặc định một chữ số thập phân; xây tập token từ các object ID duy nhất. Đây là biểu diễn evidence, không phải classifier bedroom/kitchen.
- Kiểm tra call sites đang hoạt động trước khi tuyên bố tái hiện source. Các tên Kitchen/Living Room trong examples/tests hoặc bảng nhãn GT của evaluator không chứng minh runtime tự suy luận tên phòng.

Trong mã đã khảo sát chưa xác định được runtime classifier công năng phòng của OccuSG. Vì vậy phân vai rõ:

1. **OccuSG-aligned:** region → spatial membership → tập object/signature.
2. **Project extension:** tập evidence → nhãn công năng như living_room/kitchen.

Không tự gán cơ chế LLM của một paper khác cho OccuSG. Nếu tìm được source/paper chỉ rõ classifier, lập bảng đối chiếu trước khi đổi thiết kế; không gọi extension hiện tại là source parity.

## 3. Luồng dữ liệu

```text
RGB-D + intrinsics + pose sensor
  → detector/segmentation 2D đã có
  → P3 lifting bằng depth và lọc cloud
  → M6 persistent object association
  → object–region membership từ hình học đã có
  → RegionEvidence (unique IDs, class counts, signature, provenance)
  → RoomLabeler
  → semantic sidecar + bản đồ + evidence review
```

Region đến từ pipeline segmentation hiện tại, không từ GT. Không gán mọi vật trong ảnh cho vùng robot đang đứng: camera có thể nhìn qua cửa vào vùng khác. Membership dùng vị trí object world và polygon.

Không đếm detection qua nhiều frame như nhiều object. Tái quan sát cùng ID giúp truy vết evidence, không tự tăng số lượng vật. Giữ semantics first-observation geometry của M6; chưa thêm centroid fusion/multi-view gate.

## 4. S0 — Kiểm kê và khóa baseline semantic hiện có

Đọc `src/standalone_sg/semantics/room_labeler.py`, `configs/semantics/room_rules_replica_cad_dev.json`, `scripts/enrich_room_semantics.py`, `docs/P5_P7_STATUS.vi.md` và status hardening mới nhất.

Hiện đã có RuleBasedRoomLabeler, unique object counts có cap, support classes, score/margin, unknown/ambiguous/stale và semantic-only export. Tái sử dụng chúng, không viết module cạnh tranh thứ hai.

Khóa capture apt_0 24 frame, detector cache, effective P3/M6/config, source/binary hashes. Chạy semantic baseline hiện tại và xuất số label cùng evidence; không dùng lại số của checkpoint G1 cũ như kết quả run này.

Audit strict config: reject sai kiểu, NaN/Inf, trọng số/ngưỡng ngoài miền hợp lệ; không ép `int()` hoặc `float()` làm mất lỗi input. Mọi thay đổi validation cần regression và không âm thầm đổi kết quả valid config.

Đầu ra: `SEMANTIC_BASELINE_MANIFEST.json`, bảng source-aligned/extension và danh sách khoảng thiếu thực sự.

## 5. S1 — Contract RegionEvidence độc lập với graph

Định nghĩa record tối thiểu:

- region ID, geometry revision, coordinate frame, polygon hoặc geometry reference;
- observation timestamp, processing timestamp, freshness;
- member object IDs và từng object: canonical class, world position, detection score gốc, last_seen, frame/mask provenance;
- class counts từ unique IDs; observed IDs và eligible semantic IDs tách riêng;
- semantic signature tương thích source nếu dùng: set class/rounded-XY; giữ IDs ngoài signature để tránh coi token trùng là cùng instance;
- membership provenance và cảnh báo boundary/uncertain nếu có.

Adapter A đọc M5 regions + M6 objects + membership resolver hiện có. Adapter B đọc snapshot M8/room_contains, phục vụ replay và đối chiếu. Không tạo hai thuật toán membership khác nhau. Phân biệt region chưa được promote và RoomNode đã tồn tại; region chưa có object vẫn xuất unknown, không biến mất khỏi bảng.

Nếu contract chưa hỗ trợ region-level input, refactor labeler thành phần tính toán thuần nhận RegionEvidence; giữ `label_snapshot()` như compatibility adapter. Test adapter cũ và mới cho cùng evidence phải cùng kết quả.

Không dùng GT instance IDs, room polygons, semantic rendering GT, scene name hoặc tọa độ đặc thù apt_0 để suy luận label. GT chỉ vào evaluator.

## 6. S2 — Chốt vocabulary và bằng chứng công năng

Tạo config versioned gồm room label vocabulary, alias detector classes, weights, count cap, minimum support/score và margin. Khởi đầu từ config P5 hiện có, không tự sửa hàng loạt.

Phân biệt label với trạng thái: `unknown`, `ambiguous`, `stale` là trạng thái abstention/freshness, không phải loại phòng kiến trúc. Có thể xuất display_label tương ứng nhưng schema phải rõ.

Kiểm kê detector vocabulary thực tế. Không dựa vào stove/cabinet/stairs nếu model không phát hiện các class ấy; không đổi class thành nhãn khác chỉ để luật chạy. Không có detection không chứng minh vật không tồn tại.

Ví dụ evidence cần review, không mặc định là luật đúng tuyệt đối:

- sofa + TV có thể hỗ trợ living_room;
- dining_table + chair có thể hỗ trợ dining_area;
- refrigerator + thiết bị phù hợp có thể hỗ trợ kitchen;
- bed có thể hỗ trợ bedroom nhưng cần xử lý detection đơn sai.

Vật dùng chung như chair/plant/table không đủ để suy ra chắc chắn một loại phòng. Với open-plan, mặc định abstain khi mâu thuẫn; nếu muốn multi-label phải là profile/schema extension riêng, không trộn vào đánh giá single-label.

## 7. S3 — Labeler có giải thích, trước mắt không thêm LLM

Giữ baseline P5 additive evidence:

`score(label) = Σ_class weight(label,class) × min(unique_count(class), count_cap)`.

Xếp hạng rồi áp dụng minimum score, số class hỗ trợ cho chính candidate và margin top1–top2. Score không phải xác suất hay confidence đã calibration. Không trộn detector confidence vào phép tính mà không đăng ký extension riêng.

Output cho mỗi region:

```text
region_id, geometry_revision, evidence_revision
predicted_label hoặc null
status: predicted / unknown / ambiguous / stale
candidate_scores, score_kind
observed_object_ids, supporting_object_ids
evidence_hash, rule_version, method
observation_timestamp, processing_timestamp, reason
```

`predicted` không đồng nghĩa `human_verified`. Không đổi region/room ID thành tên semantic. Hai vùng cùng nhãn vẫn là hai vùng riêng; không tự merge.

Khi thiếu evidence, xuất lý do và RGB contact sheet để review. Không hạ threshold chỉ để mọi vùng đều có nhãn. Chưa cần LLM để đạt milestone này; chỉ cân nhắc backend khác sau khi có error analysis và bộ nhãn đánh giá.

## 8. S4 — Lifecycle và tích hợp một chiều

- Evidence mới, object đổi class/membership, region split/merge: tính lại evidence của các vùng bị ảnh hưởng.
- M4 reject/stale: không coi là quan sát mới, không refresh timestamp evidence; có thể giữ last valid prediction để hiển thị nhưng phải đánh dấu stale rõ ràng.
- Region bị xóa: ghi tombstone/removal, không để nhãn mồ côi.
- Split/merge không sao chép nhãn như sự thật; gom lại object theo membership hiện hành rồi suy luận lại.
- Không đưa evidence tương lai vào nhãn quá khứ khi replay.
- Hai timestamp processing và observation tách riêng; evidence hash không phụ thuộc global graph revision không liên quan. Lần nhìn lại có thể đổi provenance nhưng không làm tăng unique object count.

Đầu tiên chạy offline trên capture, sau đó mới nối streaming nếu cần. Semantic layer không được mutate geometry hoặc chặn mapping vì lỗi labeler. Export semantic riêng; việc thêm attributes vào scene graph là adapter sau cùng, không phải điều kiện cho demo đầu tiên.

## 9. S5 — Đánh giá trước khi gọi là thành công

### A. Nghiệm thu kỹ thuật

Tests: vùng rỗng; class không hỗ trợ; một object thấy nhiều lần; nhiều object cùng class; tie; bằng chứng mâu thuẫn; object đổi owner; stale→fresh; region split/merge/remove; config invalid; fresh replay và export/reload.

Ba fresh-process replay cho cùng inputs/config phải khớp semantic outputs đã canonicalize. Bật/tắt semantic không thay geometry snapshot, trajectory và segmentation outputs. Chạy full regression khi hoàn tất.

### B. Nghiệm thu ngữ nghĩa trên apt_0

Tạo gói review cho **từng predicted region**, gồm polygon/pose camera/RGB/mask/object IDs và nhãn dự đoán. Bản human-review để tạo reference nên có chế độ ẩn prediction nhằm giảm thiên lệch; dùng ảnh trực tiếp của scene, không suy GT từ tập object do detector dự đoán.

Người dùng xác nhận functional label của vùng đủ rõ hoặc đánh dấu mixed/uncertain. Bốn spatial polygons đã xác nhận không tự cung cấp functional GT. Một predicted region cắt qua hai chức năng phải được ghi mixed/ambiguous, không ép gán theo vùng chiếm đa số mà không có protocol.

Báo: số vùng có đủ reference; accuracy trong phần single-label đã xác nhận; confusion matrix; prediction coverage và abstention; unsupported/uncertain counts; nhãn đổi khi evidence không đổi. Không chỉ báo accuracy trên vài vùng dễ mà giấu unknown.

Tách lỗi: detection/class → P3 → association → membership/segmentation → label rule. Tách hai chế độ nếu có đủ annotation: evidence được người review sửa dùng để đánh giá riêng labeler (oracle diagnostic), và evidence detector thật dùng đánh giá tích hợp. Không đưa evidence đã sửa vào run prediction chính.

Không đặt yêu cầu “mọi region có nhãn”. Chỉ gọi thành công ngữ nghĩa giới hạn khi có trường hợp dùng object thật, membership kiểm tra được, nhãn được người dùng xác nhận và giữ ổn định khi tái quan sát. Báo rõ số lượng mẫu; chưa gọi là benchmark generalization.

## 10. S6 — Bàn giao và bước sau

```text
semantic_run/
  manifest.json
  region_evidence.jsonl
  region_labels.json
  region_labels_history.jsonl
  room_label_config.json
  spatial_semantic_map.png
  region_object_evidence.html
  evaluation.json
  report.vi.md
```

Trên bản đồ chỉ ghi region ID + label/status; object IDs chi tiết nằm ở panel/evidence sheet, tránh vẽ chồng toàn bộ graph.

Giao một lệnh CLI tái lập từ capture/snapshot đã khóa, không phụ thuộc tên apt_0 hard-code trong inference. Xuất report có bảng source-aligned versus extension và giới hạn vocabulary/GT-pose/segmentation.

Thứ tự thực hiện: S0 → S1 → S2/S3 → S4 → S5 → S6. Không bắt đầu lại perception từ đầu, không chờ polygon hoàn hảo, không dùng semantic để che lỗi segmentation. Sau milestone này mới chọn tích hợp attributes semantic vào graph hiện có và kiểm chứng apt_1/apt_2 bằng config đã khóa, không tuning theo từng scene.
