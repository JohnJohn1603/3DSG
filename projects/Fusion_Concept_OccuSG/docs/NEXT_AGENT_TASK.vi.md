# Bàn giao sau U4 review package — dừng trước U5

> **LEGACY / HISTORY (2026-10-12):** This plan/handoff belongs to the earlier U/K/Q development track. It is retained for provenance and is not the active roadmap. Follow [the current ConceptGraphs → OccuSG plan](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md).


Đọc [kế hoạch](FUSION_UPGRADE_PLAN.vi.md), [U3 causal status](U3_CAUSAL_STATUS.vi.md), [U4 status](U4_STATUS.vi.md), [U4 pre-review](U4_PREREVIEW_SCOPE.vi.md) và [source audit](SOURCE_AUDIT.vi.md).

Workspace duy nhất được sửa:
`/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG`

Baseline chỉ đọc:
`/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG`

## Trạng thái đã khóa

- U3 geometry-only là candidate chính; source là baseline; geometry+appearance là ablation. Không đổi U3/U4 config hoặc assignments trong lượt này.
- Gói review ngắn: `artifacts/audits/u4_review_package/index.html`; response sheets là `identity_review_responses.csv` và `category_mapping_review.csv`. Review physical identity độc lập với category mapping. Không tự promote provisional mappings.
- U4 correctness chưa chấm: trước đó có zero confirmed category mappings. Chờ phản hồi review để phân biệt sửa đúng/sửa sai trên cùng fixed assignments.
- Temporal test mới được khóa theo thứ tự frame 239–278 (40 frames, 90 cached P3 observations), manifest/hash tại `artifacts/audits/u4_temporal_test_lock/`. Chưa replay/chấm; đây là same-scene temporal test, không phải scene holdout.
- Frames 231–238 và các case trong package là development/seen, không gọi là untouched holdout.
- Không sửa GT, class/room scores, graph/baseline; chưa bắt đầu U5.

## Khi có review phản hồi

1. Đọc riêng identity responses và mapping decisions; uncertain là đáp án hợp lệ.
2. Chỉ chấm class correctness với mapping có reviewer/date/decision provenance và confirmed status. Chấm correction/harmful change trên đúng assignments U3 đã khóa; tách lỗi association khỏi class aggregation.
3. Replay prefix 0–238 causal, rồi đánh giá frames 239–278 bằng đúng U3/U4 configs hiện khóa. Không xem/chọn theo kết quả trước khi hoàn tất predictions; sau đó mới scorer đọc evaluator GT.
4. Report precision của confirmed classes cùng coverage; không dùng confirmed/track coverage đơn độc để so.

Dừng sau review/evaluation offline này. Chưa promote và chưa thực hiện U5.
