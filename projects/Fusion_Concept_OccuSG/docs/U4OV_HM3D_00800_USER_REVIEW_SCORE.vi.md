# Chấm review U4-OV multiview HM3D 00800

Ngày: 2026-10-09. Đây là chấm evaluator-only trên review development, không phải benchmark hay holdout. Predictor, assignments, thresholds, capture và baseline không bị sửa.

## Annotation và phạm vi

Annotation version mới: [CSV theo observation ID](../artifacts/audits/u4ov_multiview_pilot_user_review/versions/u4ov-00800-mv-pilot-v2-review-20261009-v1/observation_annotations.csv). Phản hồi người dùng nguyên văn và hashes provenance nằm trong [version directory](../artifacts/audits/u4ov_multiview_pilot_user_review/versions/u4ov-00800-mv-pilot-v2-review-20261009-v1/); identity/class status được lưu độc lập. Identity `obj_C` ở frame 1 det 2 chỉ nằm ở trường proposal, status uncertain và không dùng chấm identity. Frame 3 det 2 chưa trả lời nên unreviewed. Class `chair` ở frame 1 det 2 vẫn được chấm observation-level.

- Gói có 20 observations. Identity confirmed: 18 observations, **9 physical identity tokens**. Identity uncertain: 1; unreviewed: 1.
- Class confirmed: 18 observations. Trong đó **12** nhãn nằm trong vocabulary U4-OV và có thể chấm exact-label; **6** observations không được tính đúng/sai vì vocabulary không có `kitchen island` (4) hoặc `stove` (2). Một class uncertain và một class unreviewed bị loại.
- Chỉ whitespace/underscore/case được chuẩn hóa. Không có COCO/evaluator category mapping nào được xác nhận. `kitchen island` không ánh xạ sang `dining_table`/`table`; `stove` không ánh xạ sang `oven`.
- Exact-label subset gồm 12 observations thuộc 6 identity token: 5 `chair`, 2 `clock`, 2 `microwave`, 3 `refrigerator`. Đây là exact string diagnostic trên vocabulary đã khóa, không phải COCO-mapped metric.

## Association

Pairwise metric dùng unique **cross-frame** observation pairs; same-frame pairs bị bỏ qua. Hai đầu pair phải có identity confirmed. Cùng token nghĩa là cùng vật; token khác nghĩa là vật khác. Unmatched prediction trên positive pair tính FN. False merge là predicted track chứa từ hai confirmed identity token; fragmentation là một identity được chia vào nhiều predicted tracks. Exact-pose revisit được tính trong association primary vì là observation theo timestamp, và có sensitivity bỏ revisit; nó không tăng số view độc lập.

18 identity-confirmed observations tạo 125 scored cross-frame pairs: 13 positive pairs cùng vật, 112 negative pairs khác vật. 29 cross-frame pairs bị loại vì một đầu là uncertain/unreviewed; 28 same-frame pairs không nằm trong metric. Khi bỏ exact-pose revisit, còn 108 pairs: 10 positive, 98 negative.

| Mode | Predicted tracks toàn pilot | TP / FP / FN | Pairwise P / R | False-merge tracks | Fragmented identities |
|---|---:|---:|---:|---:|---:|
| Source raw | 12 | 13 / 0 / 0 | 1.00 / 1.00 | 0 | 0 |
| Source dedup | 12 | 13 / 0 / 0 | 1.00 / 1.00 | 0 | 0 |
| Geometry-only | 14 | 13 / 0 / 0 | 1.00 / 1.00 | 0 | 0 |
| Geometry + appearance | 16 | 10 / 0 / 3 | 1.00 / 0.77 | 0 | 1 (`obj_A`) |

Không có duplicate masks trong input, nên source raw và source dedup cho cùng kết quả. Bỏ revisit không đổi source/geometry-only (10/0/0, P=R=1.00); geometry+appearance còn 8 TP và 2 FN trên 108 pairs (P=1.00, R=0.80). Với appearance, frame 2 của `obj_A` bị tách khỏi các quan sát còn lại thành hai predicted tracks; đây là fragmentation đã được identity review xác nhận.

`obj_A` gồm frame 0/1/2/4 theo review. Frame 3 det 2 vẫn unreviewed, không được gán identity từ các frame khác. Frame 4 có RGB/hash và locked view bin trùng frame 0 nên revisit không tăng support. Các observations `obj_A` có 3 locked U4 view bins nhưng chỉ 2 vị trí camera khác nhau: frame 1 và frame 2 dùng cùng vị trí, khác yaw. Do đó không diễn giải 3 bins này thành 3 vị trí độc lập.

## Causal U4-OV và final-map labels

Chỉ chấm 12 class-confirmed observations có exact label trong vocabulary. `classified` là coverage; `unknown`/`conflicted` tính abstention. `accuracy_on_covered` được báo kèm coverage để tránh đọc precision 100% như độ chính xác tổng thể.

| Mode | Causal correct / wrong / abstain | Causal coverage | Final tracks đủ identity/class để chấm | Final correct / wrong / abstain | Final coverage |
|---|---:|---:|---:|---:|---:|
| Source raw | 1 / 0 / 11 | 8.3% | 6 tracks, 6 objects | 1 / 0 / 5 | 16.7% |
| Source dedup | 1 / 0 / 11 | 8.3% | 6 tracks, 6 objects | 1 / 0 / 5 | 16.7% |
| Geometry-only | 1 / 0 / 11 | 8.3% | 6 tracks, 6 objects | 1 / 0 / 5 | 16.7% |
| Geometry + appearance | 1 / 0 / 11 | 8.3% | 6 tracks, 6 objects | 1 / 0 / 5 | 16.7% |

Mỗi mode có accuracy 1/1 trên nhãn đã xuất, nhưng coverage rất thấp: chỉ track `obj_B` được ghi `clock`. First-observation raw detector class khớp exact label ở cả 12/12 supported observations. U4-OV sửa đúng 0 raw errors; có 0 wrong relabel, nhưng abstain ở 11 observations mà raw class ban đầu đã đúng. Đây là mất coverage của sidecar nếu dùng nó thay nhãn; raw class vẫn được lưu riêng.

Final-map chấm một lần mỗi reviewed-scope predicted track, chỉ khi có đúng một confirmed identity token và một class truth exact-supported. Track trộn nhiều identity token bị đánh dấu false merge và không chấm majority class; track không có identity confirmed, như observation `0001:det-0002`, bị loại khỏi final-track class denominator dù class `chair` vẫn tham gia causal observation scoring. Trong các mode này không có mixed-identity track nào ở phạm vi review. Năm track còn lại trên sáu eligible tracks abstain; không có correction, có năm trường hợp final map abstain sau first raw class đúng.

## Kết luận và quyết định

**Quyết định B: chỉ đủ cơ sở cho plumbing thử nghiệm opt-in; chưa chứng minh cải thiện chất lượng.** Geometry-only bằng source trên 125 cặp đã chấm, không hơn baseline trong mẫu này. Geometry+appearance kém hơn do fragmentation `obj_A`. U4-OV chỉ phân loại được một nhãn chính xác và abstain ở 11 nhãn khác vốn đã đúng ở raw detector. Không có bằng chứng để đưa fused label vào room evidence hoặc tuyên bố U3/U4 nâng chất lượng.

Nếu triển khai plumbing U5 sau này, giữ source mặc định, feature flag và rollback; giữ raw detector class tách biệt, chỉ xuất fused sidecar khi status classified, và không dùng unknown/conflicted làm evidence. Appearance association chưa nên bật. Đây là khuyến nghị, không phải thay đổi graph/runtime trong lượt này.

## Tests và artifacts

Đã chạy scorer evaluator-only và 33 targeted tests: U3 experimental association, U4 class fusion, U4-OV evaluation core, source object association và các test metric mới. Tất cả pass; không chạy full suite. Không chạy capture, detector, P3, predictor hay tuning.

- [Evaluation report JSON](../artifacts/audits/u4ov_multiview_pilot_user_review/versions/u4ov-00800-mv-pilot-v2-review-20261009-v1/evaluation_report.json)
- [Scored class table](../artifacts/audits/u4ov_multiview_pilot_user_review/versions/u4ov-00800-mv-pilot-v2-review-20261009-v1/scored_observation_class_table.csv)
- [Pairwise rows theo mode](../artifacts/audits/u4ov_multiview_pilot_user_review/versions/u4ov-00800-mv-pilot-v2-review-20261009-v1/pairwise_rows_by_mode.json)
- [Category mapping status](../artifacts/audits/u4ov_multiview_pilot_user_review/versions/u4ov-00800-mv-pilot-v2-review-20261009-v1/category_mapping_status.json)
