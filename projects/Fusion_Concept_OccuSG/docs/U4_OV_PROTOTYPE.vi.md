# U4-OV offline prototype

## Phạm vi và khóa

Prototype này chạy ngoài graph runtime. U3, U4 hiện hữu, baseline, detector, P3, room scores và các evaluator raw YOLO không bị sửa. Predictor đọc các image embeddings U2 đã lưu, không chạy lại detector/P3/mapping. Ground truth, review annotations và room labels chỉ được đọc ở bước hậu kiểm.

Vocabulary `indoor_objects_v1` gồm 105 candidate labels, được khóa cùng prompt templates trong `artifacts/audits/u4_ov/u4_ov_config.locked.json` trước khi chấm. Danh sách do tác giả lập từ inventory đồ vật indoor rộng và nhãn distractor chung; không lấy object/category của scene từ GT. Nó có các nhãn như armchair, chair, table, dining table, printer và piano trong một vocabulary rộng, nên đây là zero-shot chọn trong tập candidate đã khóa, không phải nhận diện class tùy ý.

Model dùng lại checkpoint OpenCLIP ViT-B-32/OpenAI của U2, revision `a6f597a30f7b82c51704746581f9a4e41421e878`, SHA-256 `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31`. Dependency lock vẫn là `locks/appearance_requirements.lock.txt` và `locks/appearance_dependencies.lock.json`; không cài thêm package. Text embeddings được mã hóa từ bốn prompt templates cố định cho mỗi label, chuẩn hóa rồi lấy trung bình. Cache hash ràng buộc checkpoint, vocabulary, templates và tokenizer.

Config cố định dùng cosine similarity; margin dưới 0.015 tạo trạng thái `conflicted`, top score dưới 0.20 tạo `unknown`, multi-view cần ít nhất hai view bins hợp lệ. Các ngưỡng này là mặc định prototype, chưa calibration; cosine không phải xác suất đúng. Raw detector class không chặn candidate. Multi-view chọn observation sớm nhất ở mỗi bin góc nhìn 30 độ, rồi chuẩn hóa trung bình image embeddings.

## Tách đầu ra và evaluator

- `raw_detector_class`: lớp detector bất biến theo observation trong `observations_predictions.jsonl`.
- `fused_semantic_label`: nhãn U4-OV observation-level hoặc nhãn gộp theo predicted U3 track; trạng thái `classified`, `conflicted` hoặc `unknown` đi cùng diagnostics.
- Raw YOLO tiếp tục dùng evaluator/mapping cũ, chỉ được tham chiếu qua `raw_detector_evaluator_mapping_reference.json`; không chạy hoặc sửa evaluator đó.
- Fused labels dùng mapping riêng `fused_label_evaluator_mapping_v1.json`: 17 lexical proposal ở trạng thái provisional, 88 nhãn unmapped, 0 confirmed. Provisional/unmapped không được tính correctness; unknown/conflicted là abstention; evaluator GT unmatched/uncertain bị loại khỏi mẫu số và vẫn được thống kê.

Vì chưa có mapping fused-label nào được người có thẩm quyền xác nhận, evaluator trên GT hiện trả `N/A` (denominator 0). Các metrics development bên dưới là exact match với nhãn người dùng review, chỉ để chẩn đoán; đây không phải xác nhận category mapping hay benchmark.

## Kết quả development

Phạm vi A có 18 reviewed observations. Strict exact match hiện tính đủ 18 nhãn, kể cả nhãn `table`; prediction `dining table` được báo là specificity relation pending taxonomy approval, không bị lặng lẽ nhập làm `table` hoặc loại khỏi mẫu số. Các case review đều là development, không phải benchmark/holdout.

| Phương thức | Đúng | Sai | Abstain | Coverage | Correct trên phần có nhãn | Sửa lỗi raw | Harmful correction |
|---|---:|---:|---:|---:|---:|---:|---:|
| Raw YOLO | 3/18 | 15/18 | 0 | 100% | 16.7% | — | — |
| U4 voting raw labels | 2/18 | 5/18 | 11 | 38.9% | 28.6% | 0 | 0 |
| U4-OV single-view | 1/18 | 3/18 | 14 | 22.2% | 25.0% | 1 | 0 |
| U4-OV online causal track state at each observation | 1/18 | 0/18 | 17 | 5.6% | 100% | 1 | 0 |
| U4-OV final track label projected to each reviewed observation | 4/18 | 0/18 | 14 | 22.2% | 100% | 4 | 0 |

Con số 4 đúng của final-map projection là bốn observation của cùng một predicted track piano `u3-0046`, tại frames 75, 126, 199, 234; đó là **một track**, không phải bốn vật được sửa đúng. Ở từng thời điểm causal, nhãn piano chỉ được xuất ở frame 234; ở các frames 75, 126, 199 trạng thái lần lượt unknown/conflicted/conflicted.

Đếm một lần mỗi predicted track trong nhãn review: có 5 reviewed tracks, trong đó `u3-0015` mixed identity và bị loại khỏi class score, còn 4 track identity-pure. Raw YOLO đúng 1/4; U4 voting đúng 1/4; U4-OV single-view đúng 0/4; causal state tại observation cuối đúng 1/4; final-map đúng 1/4 (đều có 3 abstention nếu có). Track piano `u3-0046` là positive duy nhất của final-map. Final-map không xác nhận đây là cải thiện end-to-end vì identity vẫn do U3 dự đoán.

Nhóm B dùng bốn identity groups người dùng xác nhận chỉ để gom observation chẩn đoán, không phải end-to-end predictor:

| Phương thức | Đúng | Sai | Abstain | Coverage | Ghi chú |
|---|---:|---:|---:|---:|---|
| Raw YOLO plurality | 1/4 | 3/4 | 0 | 100% | — |
| U4 voting plurality | 1/4 | 2/4 | 1 | 75% | — |
| U4-OV single-view plurality | 0/4 | 1/4 | 3 | 25% | `dining table` so với generic `table` là specificity pending, strict exact mismatch |
| U4-OV multi-view image aggregation | 0/4 | 1/4 | 3 | 25% | Case 4 có candidate `dining table`; taxonomy relation chờ duyệt |
| U4-OV predicted U3 track output | 1/4 | 0/4 | 3 | 25% | Case 3 piano được phân loại; các nhóm khác abstain |

Trong các observation đã review, U4-OV single-view và causal đều có coverage thấp. Causal tạo đúng piano tại observation cuối track; final-map đếm piano một lần ở track-level. Multi-view và hierarchy relation vẫn là chẩn đoán development.

Case 2 được U4-OV multi-view chấm là `conflicted` (top candidate sofa; margin 0.00225), không xác nhận armchair/sofa. Case 3 multi-view theo review group conflicted, trong khi causal state tại frame 234 và final U3 track `u3-0046` là piano. Case 4 có candidate `dining table` với quan hệ cụ thể→tổng quát pending approval; exact-match cũ vẫn được lưu làm reference. Case 1 vẫn có false merge U3 track `u3-0015`; ba reviewed observations không được gộp thành một class truth.

Taxonomy proposal tại `artifacts/audits/u4_ov/taxonomy_hierarchy_proposal_v1.json` đề xuất `armchair → chair`, `dining table → table`, `coffee table → table`, `side table → table`; tất cả đều `proposed_unapproved`, không đổi predictor/evaluator mapping. `bed/table` và `printer/table` được giữ là ví dụ không liên quan. Strict exact result trước đó với case 4 bị loại được giữ trong JSON dưới `legacy_exact_match_reference`, chỉ làm historical reference.

Nhóm C trên 622 observations U3 prefix: 118 single-view outputs được classified, 504 conflicted, 0 unknown; 79 predicted tracks có 3 classified, 30 conflicted, 46 unknown (thường thiếu hai view bins). Identity diagnostics trên các quan hệ review có 21 pair TP, 2 pair FP, 0 pair FN và 1 pair uncertain; false merge trong mixed reviewed track `u3-0015` được giữ riêng, không dùng majority class để làm track sạch. Các con số này chỉ bao phủ relations có annotation tin cậy, không phải toàn bộ identity precision/recall.

## Evaluator và giới hạn

Evaluator mapping giờ có implementation tính accuracy, micro precision/recall/F1, coverage, per-class TP/FP/FN, abstentions và mẫu số. Mapping hiện vẫn có 0 confirmed, nên metrics thực tế trả unavailable với denominator 0. Trong 622 observations, 426 GT matches và 196 unmatched/ignored; matched GT hiện không có category nào được phủ bởi một fused mapping confirmed. Mapping cũ cũng giới hạn điểm: nhãn ngoài vocabulary/mapping như piano và subtype chưa xác nhận không thể chấm chính thức.

Không có unseen multi-frame sequence trong cache hiện tại. Scene 00802 và frames 0–278 đã được xem/chấm ở các lượt trước; frame 279 chỉ có một detection nên không thay thế được temporal test. Vì vậy báo cáo không gọi phần đó là test mới hay holdout.

Runtime prediction gốc cho 622 embeddings, gồm nạp/tạo text cache, là 45.70 giây; peak RSS 1491 MiB trên CPU. Tạo causal track timeline từ cache mất 0.65 giây; 37 classified, 236 conflicted và 349 unknown snapshots. Cache text key SHA-256 `23ae1da7b7f41d89608e86db277593b77517f26db4e93891ac4a043081d2bfb4`; file SHA-256 `d432d5614570afac59d53d40a75955d05c0c0736bbd799056e3599cb4ed4efd2`.

Chưa có sequence mới: mọi input index hiện có đều thuộc scene `hm3d-00802` (622 prefix observations, 712 temporal test cache, 47 association inputs). Không tái dùng các frame đó làm test unseen.

## Tái lập và artifacts

```bash
.venv-appearance/bin/python scripts/lock_u4_ov_vocabulary.py
.venv-appearance/bin/python scripts/prepare_u4_ov_evaluator_mappings.py
.venv-appearance/bin/python scripts/run_u4_ov_offline.py
.venv-appearance/bin/python scripts/build_u4_ov_causal_track_predictions.py
.venv-appearance/bin/python scripts/evaluate_u4_ov_offline.py
```

Hai lệnh khóa chỉ dùng khi tạo prototype mới; không chạy lại trên artifacts đã khóa. Causal script chỉ đọc frozen predictions, U2 features và text vectors; evaluator chạy sau và chỉ đọc predictions. Dữ liệu chi tiết gồm `causal_track_predictions.jsonl`, `review_observation_comparison.csv`, `review_track_comparison.csv`, `confirmed_identity_group_diagnostic.csv`, `fused_label_evaluator_observations_posthoc.csv`, vocabulary/config/mapping/taxonomy và hash cache.

Đã chạy `py_compile` cho evaluator/helpers và 5 unit tests (mapping confirmed/provisional/conflict, abstention/mẫu số, taxonomy pending/approved edge, causal prefix/duplicate view); tất cả pass. Đã chạy causal artifact builder (0.65 giây) và evaluator hậu kiểm thành công. Không chạy full test suite. Prototype dừng tại report này; không thay association, class mặc định, graph runtime, room scores hoặc baseline.
