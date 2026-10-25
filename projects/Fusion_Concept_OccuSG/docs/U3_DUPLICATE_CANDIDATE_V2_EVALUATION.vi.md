# U3 duplicate candidate v2 — kết quả development offline

## Kết luận

V2 tránh canonicalize mixed-object pair R23 mà không làm mất 24/24 cặp same-object 00802 đã được review, giữ D2 cùng track và không đổi kết quả D1. Đây là tín hiệu hợp lý cho một candidate offline; chưa phải bằng chứng an toàn tổng quát và chưa đủ để nối M9. Geometry-only gốc, v1 và các artifact trước đó được giữ nguyên.

## Guard đã khóa

- Config: `configs/association/u3_same_frame_duplicate_candidate_v2.json`; SHA-256 `257dd354c7b562fed4b8d2b6548fcf38fe67946aef2e95ae1c084d79044d3ec3`.
- Chỉ xét containment cùng frame sau các gate mask, depth/P3 support và hình học đã có. Tỷ lệ diện tích mask lớn/nhỏ trên 1.5 khiến pair ambiguous nếu hình học đủ dữ liệu nhưng vẫn đồng thuận; không alias cả hai.
- Thiếu depth/point support hoặc hình học trong trường hợp mask overlap dẫn đến ambiguous. Nếu geometry đầy đủ nhưng center/AABB/extent hiện có bác bỏ duplicate, giữ pair riêng. Valid depth ngoài mask nhỏ tự nó không kết luận hai vật khác nhau.
- Không dùng class, GT, room label hay observation tương lai. Raw observations giữ nguyên; alias không thêm view/semantic vote. Không tái dựng point sets cho lần chạy này.
- Trên cache 00802 chỉ có hai pair đi vào containment branch: frame 234 (ratio khoảng 1.32; canonicalize) và R23 frame 235 (ratio khoảng 1.80; ambiguous). Cache 00800/00808 không có containment pair cần guard. Chỉ replay association cache; không chạy capture, P2/P3, native mapping, OpenCLIP hay clustering lại.

## Paired result

| Cache | Input P3 observations | V1 suppressed | V2 suppressed | V2 ambiguous observations / pairs | V2 tracks / deferred | Thay đổi duplicate policy V1→V2 |
|---|---:|---:|---:|---:|---:|---:|
| 00800 | 30 | 0 | 0 | 0 / 0 | 14 / 0 | 0 |
| 00802 | 713 | 42 | 41 | 2 / 1 | 78 / 16 | 2 observations, đều là R23 |
| 00808 | 18 | 1 | 1 | 0 / 0 | 9 / 0 | 0 |

Trong 00802, candidate v1 có 78 tracks/15 deferred; v2 vẫn 78 tracks nhưng deferred tăng lên 16. Khác biệt là observation `hm3d-00802-main-dense-0235:det-0003` không còn bị canonicalize; geometry-only replay defer nó. Observation det-0001 vẫn vào `u3-0018`. Vì vậy v2 ngăn geometry của det-0003 được nhập vào track đó trong replay này, nhưng giữ raw observation không đồng nghĩa nó đã được xử lý đúng hoặc graph sạch trong mọi backend.

### Các pair đã review và các mốc U6

- 00802 R01–R22, R24–R25: **24/24** pair same-object tiếp tục canonicalize; không có false suppression trong subset này.
- 00802 R23: v1 canonicalize/suppress det-0001 dưới det-0003. V2 giữ cả hai ambiguous, không alias; downstream det-0001 vào `u3-0018`, det-0003 defer. Đây là một duplicate chưa được gộp/association evidence chưa đủ, không được tính như identity đã sửa hoàn toàn.
- 00808 D2 (`det-0004` + `det-0005`, table_2): vẫn cùng track `u3-0008`; 1/1 same-frame consistency. Official one-to-one mask metric không bị thay đổi.
- 00808 D1 cross-frame: giữ **6 TP / 0 FP / 0 FN**. Bỏ exact-pose revisit frame 4: **3/0/0**.
- 10 nhóm canonical-only swaps geometry-only→v1 vẫn là 10 geometry-only→v2; v1→v2 có **0 canonical-only swap**. Thành viên các nhóm không đổi ngoài split/release R23.
- Toàn cache 00802: 2 observation assignment decisions đổi, pairwise track relation diff báo 695 transitions do det-0003 chuyển từ track-assigned sang deferred; đây không phải 695 vật hay lỗi độc lập. Candidate v2 tạo 24 membership expansions so geometry-only, so với 25 ở v1; số node/track thấp hơn không được xem là accuracy gain.

## R23 trace: mask, depth và P3

Annotation user-reviewed xác nhận đây là `mixed_object_mask`: det-0003 phủ vật trong det-0001 và thêm vật khác. Annotation chỉ được scorer dùng.

| Cache evidence | det-0001 | det-0003 |
|---|---:|---:|
| Pixel trong mask | 3,210 | 5,782 |
| Pixel có depth hợp lệ | 3,210 | 5,782 |
| P3 largest-cluster points đã cache | 3,093 | 3,397 |
| Reconstructed largest-cluster points từ vùng mask chung | 3,084 | 3,084 |
| Reconstructed points từ phần mask riêng | 9 | 313 |

Intersection 3,175 px; IoU 0.5458; containment của mask nhỏ 0.9891; phần riêng của mask lớn 2,607 px. Depth hợp lệ ở toàn mask; vùng riêng có range 1.542–2.991 m. Geometry cache giữa hai detection lại rất gần: center distance 0.0261 m, 3D AABB IoU 0.868, extent consistency 0.960. Do đó v1 vượt các gate hình học dù mask lớn chứa thêm vật. Phần riêng có depth không tự chứng minh vật thứ hai; kết luận mixed-object đến từ review, và trace cho thấy P3 largest cluster của det-0003 có 313 điểm tái dựng từ vùng mask riêng nên nội dung đó đã ảnh hưởng geometry canonical.

**Giới hạn provenance:** artifact P3 gốc không lưu selected pixel indices. Điểm theo vùng mask được tái dựng từ mask/depth/cache bằng profile clustering khóa; số point và center khớp P3 cache. Đây là reconstructed lineage, không phải bằng chứng exact parity của selected-point indices gốc.

## Trade-off và runtime

- Trên 25 pair 00802 đã review, 24 same-object pair được canonicalize; R23 mixed-object pair được giữ ambiguous. Không coi 25 cặp là 25 vật độc lập.
- D2 đúng được giữ; D1 không đổi. Các mốc này là development, không phải holdout.
- V2 thêm 1 deferred observation ở R23 so với v1. Ambiguous decision không ép alias, nhưng có thể làm observation thiếu track assignment.
- Paired offline replay 3 cache: 2.93 giây wall time, peak RSS 265.9 MiB. Không có graph/M9 replay.
- Targeted tests: 9 passed. Full suite không chạy.

## Run và provenance

- Final run: `artifacts/runs/u3_same_frame_dup_candidate_v2_20261011_v2/report.json`.
- Run đầu `..._v1` được giữ và đánh dấu superseded vì đã gọi nhầm hai pair frame 246 là ambiguous dù geometry đầy đủ đã bác bỏ duplicate. Config không đổi; sửa precedence chẩn đoán, rồi chạy lại cùng cache trong thư mục mới.
- Annotation: `artifacts/reviews/u3_duplicate_partition_review_v3/annotations/annotation_v1/annotations.json`, provenance `user_reviewed`, evaluator-only; phản hồi nguyên văn được giữ cạnh file.
- Không chạy capture/P2/P3/native mapping, không đổi default, không nối M9, không sửa room scores hoặc U4-OV.

## Quyết định

Giữ v2 là candidate **offline experimental**, chưa tích hợp. Trên subset đã review, trade-off là tránh false suppression R23 và giữ các duplicate đúng đã xác nhận, đổi lại một observation deferred. Vì chỉ có một mixed-mask reviewed case và các cache đều là development, chưa có cơ sở tuyên bố guard an toàn tổng quát. Trước khi đề xuất nối M9 cần cân nhắc riêng việc ambiguous observations được defer, đồng thời cần tập đánh giá độc lập; không thay thresholds trong vòng này.
