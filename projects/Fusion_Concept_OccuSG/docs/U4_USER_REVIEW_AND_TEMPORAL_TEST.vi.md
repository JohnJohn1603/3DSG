# U4: kết quả review người dùng và temporal test

Trạng thái: **đã chấm offline, chưa promote, chưa bắt đầu U5**. U3/U4 thresholds được giữ nguyên. Predictor chỉ dùng cache RGB/P2/P3, feature U2, và assignments causal; evaluator được đọc sau khi bốn prediction ledgers đã ghi xong. Không chạy lại YOLO, P3 hoặc native mapping; không dùng phản hồi review để sửa dự đoán.

## Annotation version

Version mới: `u4-review-20261008-v1`, provenance `user-reviewed`. Bản gốc câu trả lời, mapping order của HTML, category annotations và các liên kết identity được giữ trong [thư mục version](../artifacts/audits/u4_user_review/versions/u4-review-20261008-v1/). Bảng đã gắn ID để kiểm tra: [scored_observation_table.csv](../artifacts/audits/u4_user_review/scored_observation_table.csv). Có 18 observation duy nhất; frame 230/det-0005 và frame 238/det-0000 xuất hiện ở cả case 1 và case 6 nên chỉ lưu một annotation cho mỗi observation.

| Case | Observation theo thứ tự ảnh trong HTML | Nhãn đã xác nhận | Identity đã xác nhận |
|---|---|---|---|
| 1 | `0010:det-0005` → `0230:det-0005` → `0238:det-0000` | printer → table → printer | 0010 khác 0230; 0230 khác 0238; quan hệ 0010↔0238 chưa rõ |
| 2 | `0042:det-0000`, `0067:det-0004`, `0095:det-0002`, `0200:det-0000` | armchair | Cùng một vật trong bốn observation được review |
| 3 | `0075:det-0002`, `0126:det-0001`, `0199:det-0000`, `0234:det-0000` | piano | Cùng một vật trong bốn observation được review |
| 4 | `0127:det-0003`, `0223:det-0001`, `0230:det-0004`, `0232:det-0004` | table | Cùng một vật; subtype dining_table chưa xác nhận |
| 5 | `0012:det-0001`, `0219:det-0000`, `0237:det-0001` | chair | Cùng một vật trong ba observation được review |
| 6 | `0230:det-0005` → `0238:det-0000` | table → printer | Khác vật |

Các số frame ở bảng thuộc `hm3d-00802-main-dense-####`; CSV chứa `frame_id` đầy đủ và `detection_id`. Nhãn category không được mở rộng sang phần còn lại của track. Không tạo mapping `table → dining_table` hoặc `armchair → chair`. Printer/piano vẫn là annotation hợp lệ; mapping vocabulary của chúng chưa xác nhận. Annotation chỉ dùng evaluator.

## Sáu case development

Đây là mẫu review development nhỏ, không phải benchmark hoặc holdout. Trên assignments geometry-only đã khóa, có 21 cặp same-object đúng, 2 cặp different-object bị ghép, 0 FN, và 1 cặp uncertain được loại khỏi điểm. Pairwise precision **0.913**, recall **1.000**. Hai false-merge edges đều nằm trong track `u3-0015`: `0010:det-0005`↔`0230:det-0005` và `0230:det-0005`↔`0238:det-0000`. Không suy quan hệ identity giữa hai ảnh printer đầu/cuối.

U4 có 7 confirmed predictions trong 18 observation được review, 11 abstention. So sánh literal với nhãn review cho thấy 2 match và 5 mismatch (precision literal 2/7); không có output nào chuyển thành một confirmed reviewed label đúng. Năm mismatch là: frame 0095/det-0002 armchair→sofa, 0200/det-0000 armchair→sofa, 0199/det-0000 piano→bench, 0234/det-0000 piano→bench, và 0238/det-0000 printer→suitcase. Các mismatch là lỗi class output trên assignments cố định; riêng track `u3-0015` đồng thời có lỗi association nên class lỗi đó bị association confound.

Trong category có exact label phù hợp với output vocabulary là `chair`, U4 đạt precision **2/2** trên hai confirmed observation và coverage **2/3**; observation frame 0012 abstain. Các nhãn armchair, printer, piano và table generic chưa có mapping xác nhận phù hợp. Với table, `dining_table` không được coi là tương đương. Không tính class metric mapping-aware cho những nhãn này; các mismatch trực tiếp ở trên vẫn được liệt kê để review.

## Temporal test frames 239–278

Đây là temporal test cùng scene, không phải scene holdout. Mỗi mode khởi tạo state riêng ở frame 0 và replay causal đến frame 278; boundary state frame 238 được lưu riêng. Không seed track từ GT hoặc mode khác, không sửa quá khứ bằng observation tương lai. Split khóa chọn đúng 40 frame liên tiếp ngay sau horizon đã review kết thúc tại 238, tức 239–278. Frame 279 không thuộc interval đã khóa và không được thay vào sau khi xem kết quả.

Pairwise scoring dùng các cặp cross-frame trong toàn bộ replay có ít nhất một endpoint thuộc test; nhờ vậy cặp prefix↔test được tính theo state đã tích lũy. Cả hai endpoint cần evaluator `matched` và `gt_semantic_id`. GT không match không được coi là đúng. Source raw giữ duplicate; source dedup, geometry-only và geometry+appearance dùng cùng 89 observation sau dedup. Geometry-only/appearance có 68/89 matched test observation (76.4% coverage); 21 observation còn lại bị loại khỏi pairwise correctness. Với mode đã dedup có 57,765 cặp trước khi loại thiếu GT identity; 26,847 cặp bị loại và 30,918 cặp được chấm. TN chỉ diagnostic.

| Mode | Pairs scored | TP / FP / FN | Pairwise P / R | False-merge tracks / fragmented GT instances | Defer test | Replay seconds |
|---|---:|---:|---:|---:|---:|---:|
| Source raw | 31,682 | 556 / 126 / 324 | 0.815 / 0.632 | 2 / 7 | 0/90 (0.0%) | 0.836 |
| Source dedup | 30,918 | 556 / 126 / 314 | 0.815 / 0.639 | 2 / 7 | 0/89 (0.0%) | 0.745 |
| Geometry-only | 30,918 | 454 / 9 / 416 | 0.981 / 0.522 | 1 / 6 | 5/89 (5.6%) | 1.014 |
| Geometry + appearance | 30,918 | 314 / 55 / 556 | 0.851 / 0.361 | 1 / 6 | 13/89 (14.6%) | 1.128 |

Appearance làm giảm pairwise precision 0.130 và recall 0.161 so với geometry-only trên các cặp prefix/test đã chấm; FP tăng từ 9 lên 55, defer tăng 8 observation. Cả hai experimental mode có một false-merge track và sáu fragmented evaluator instances trong causal scope có endpoint test. Đây là một temporal interval ngắn của cùng scene. Class correctness của temporal test **unavailable** vì category mapping phù hợp chưa được xác nhận; số confirmed class states chỉ là coverage mô tả, không phải accuracy.

Peak RSS toàn process là khoảng **1,499 MiB** khi nạp OpenCLIP; RSS lấy mẫu trong association pass khoảng **565 MiB**. Tất cả 90 temporal feature vectors có shape 512, finite và L2-normalized trong tolerance `atol=rtol=1e-5`; model OpenCLIP ViT-B-32/OpenAI, revision `a6f597a30f7b82c51704746581f9a4e41421e878`, checkpoint SHA-256 `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31`, CPU. Feature files được tạo trong các lần chuẩn bị có cache tiếp diễn; thời gian inference tổng của các lần chuẩn bị đó không được ghi riêng. Lần chạy report cuối dùng cache đủ 90 feature nên inference mới bằng 0 giây; wall time causal replay + scoring là **17.50 giây**.

## Kết luận và artifacts

Ưu tiên xử lý **U3 association** trước U4: review xác nhận mixed-identity false merge `u3-0015`; geometry-only còn fragmentation trên temporal test; appearance không cải thiện P/R và làm recall thấp hơn. U4 hiện không có correction đúng trên các reviewed observations, và có năm confirmed label mismatch. Giữ geometry-only làm candidate chẩn đoán, chưa promote; cần protocol mapping category được xác nhận trước khi chấm class-aware correctness rộng hơn.

- Temporal predictions, pairwise report và posthoc CSV: [u4_temporal_test_run](../artifacts/audits/u4_temporal_test_run/).
- Development scoring: [u4_development_results.json](../artifacts/audits/u4_user_review/u4_development_results.json).
- Frozen temporal selection: [temporal_test_manifest.json](../artifacts/audits/u4_temporal_test_lock/temporal_test_manifest.json).
- New replay command: `.venv-appearance/bin/python scripts/run_u4_locked_temporal_test.py`.
- U4 development score command: `.venv-appearance/bin/python scripts/score_u4_user_review_v1.py`.
- Related tests: `python3 -m pytest -q tests/test_u4_class_fusion.py tests/test_u3_experimental_association.py tests/test_m6_object_association.py` → **23 passed**. `.venv-appearance` không có pytest; không cài thêm dependency.
