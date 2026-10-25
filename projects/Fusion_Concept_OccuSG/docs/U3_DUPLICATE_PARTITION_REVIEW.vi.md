# U3 duplicate partition review — v1

## Phạm vi

Đối chiếu `geometry_only` với `geometry_only_candidate` trên đúng paired outputs v6 của HM3D 00800/00802/00808. Đây là phát triển, không phải tập test độc lập. Không chạy lại predictor, capture, P2/P3 hay M9; không đổi config.

Partition được dựng theo từng frame từ canonical observation và toàn bộ alias. So sánh dựa trên tập observation thành viên; canonical ID/chiều alias/track ID không quyết định hai partition có giống nhau hay không.

## Kết quả partition

| Scene | Nhóm giữ nguyên membership và canonical | Cùng membership, đổi canonical | Membership mở rộng/gộp | Split/release | Case review identity |
|---|---:|---:|---:|---:|---:|
| 00800 | 30 | 0 | 0 | 0 | 0 |
| 00802 | 636 | 10 | 25 | 0 | 25 |
| 00808 | 16 | 0 | 1 | 0 | 1 |

Tại 00802 có 713 observations, 17 alias nhóm cũ và 42 alias candidate. Delta theo observation ghi 35 alias mới và 10 alias được thả; partition-level comparison cho thấy **10 cặp chỉ đổi canonical**, còn **25 cặp thật sự chuyển từ hai singleton thành một duplicate group**. Vì vậy, 35 suppression mới không tương đương 35 nhóm/vật mới được gộp. Không có group nào bị split/release trong so sánh này. 00808 có một thay đổi membership thực: D2 frame 2 `det-0004` + `det-0005`; 00800 không đổi.

## Tác động canonical riêng

Trong 10 đổi canonical ở 00802, raw class của canonical thay ở cả 10 cặp. Tâm P3 giữa đại diện cũ/mới lệch 0–0.0265 m (median 0.0003 m); đây là bằng chứng representative có thể đổi raw class và geometry đầu vào ngay cả khi group membership không đổi. Geometry-only không dùng class làm association gate.

Paired replay v6 hiện có ghi 98 observation decisions và 44,984 pairwise relations khác nhau giữa geometry-only và candidate ở 00802; ở 00808 lần lượt 1 và 1; 00800 là 0 và 0. Phần lớn chuyển tiếp 00802 xuất phát từ trạng thái deferred/unassigned sang assigned. Không có ablation tách riêng canonical choice khỏi membership policy trong lượt này, vì vậy không quy các thay đổi xuyên frame đó riêng cho 10 canonical swaps hay cho 25 group changes. Số track/defer không được xem là accuracy.

## Gói review identity

Gallery gồm 26 case membership-changing (25 ở 00802, 1 ở 00808), tổng 52 observations. Mỗi observation có full ID, RGB toàn khung, prediction mask, RGB+mask; mỗi case có overlay hai mask cùng scale. Không hiển thị class dự đoán hoặc GT identity. Các case là frame-local: gallery không gợi ý identity xuyên frame.

Panel validation: 156/156 panel path duy nhất; nguồn RGB/mask và hash được ghi trong manifest; nội dung ảnh đã pixel-check với đúng source/transform. Mask hash phải khớp P2 index. RGB 00802 lưu RGBA với alpha opaque; gallery đã kiểm tra alpha=255 rồi chỉ render ba kênh RGB như input P2.

Artifacts:

- Gallery (review bị che predicted labels/grouping): `artifacts/reviews/u3_duplicate_partition_review_v3/index.html`
- Case IDs, observation IDs, source paths/hashes: `artifacts/reviews/u3_duplicate_partition_review_v3/case_manifest.json`
- Từng panel và pixel validation: `artifacts/reviews/u3_duplicate_partition_review_v3/panel_validation.jsonl`
- Group-level delta: `artifacts/reviews/u3_duplicate_partition_review_v3/partition_deltas.jsonl`

## Chất lượng đã chấm trên 00808

Giữ nguyên annotation và metrics đã xác nhận trước đó:

| Mode | D1 cross-frame TP/FP/FN | D1 khi bỏ frame 4 TP/FP/FN | D2 same-frame consistency |
|---|---:|---:|---:|
| Source | 3/0/3 | 1/0/2 | 1/1 |
| Geometry-only | 6/0/0 | 3/0/0 | 0/1 (split) |
| Geometry-only + candidate dedup | 6/0/0 | 3/0/0 | 1/1 |

D1 vẫn giữ lợi ích của geometry-only; bỏ exact-pose revisit vẫn là 3/0/0 cho candidate. D2 đã cùng track trong candidate. Đây là cùng 6 confirmed observations và hai physical identities được người dùng xác nhận; không nhân các cặp thành số vật. HM3D 00802 không có identity GT đã xác nhận trong phạm vi các 25 case, nên chưa thể gọi các merge đó là đúng hoặc sai.

## Quyết định

Candidate có tín hiệu cụ thể trên development 00808: giữ D1 và nối D2. Nhưng 25 membership changes ở 00802 chưa được review identity, còn 10 canonical swaps đổi toàn bộ raw class đại diện. Candidate tiếp tục **experimental offline**, chưa nối M9 và không đổi default. Bước kế tiếp duy nhất là review identity 26 case trong gallery; sau đó cân nhắc opt-in integration nếu không thấy false merge nghiêm trọng. Không cần review class.
