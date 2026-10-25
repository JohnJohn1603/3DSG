# Ground-truth build — trạng thái checkpoint

Cập nhật: 2026-10-03  
Phạm vi: **apt_0**, capture đã khóa; không đổi baseline model, mapping, segmentation, M6 association hay thresholds.

## Trạng thái

| Phần | Trạng thái | Bằng chứng / giới hạn |
|---|---|---|
| G0 — khóa đầu vào | Đạt cho capture đã chọn | `habitat_replica_cad_apt0_perception_dev_long_20261001`, 24 frame 320×240, Habitat-Sim 0.3.3, GT pose. Hash/provenance ở `evaluation/runs/apt0_gt_20261003_01/manifest.json` và `evaluator_manifest.json`. |
| G1 — architectural room GT | **Chờ người review/xác nhận** | Có stage-only slice và công cụ polygon/doorway annotation. Chưa có polygons `human_verified`; room metrics để null. |
| G2 — instance registry/masks | Đạt ở mức simulator-reference | 113 rigid instances, 24 visible-instance mask frames; RGB/depth/valid-mask equivalence đã kiểm tra. Sáu articulated declarations runtime non-Bullet không hỗ trợ được loại khỏi eligible set, không tính là detector miss. Class mapping vẫn provisional. |
| G3 — geometry/lineage | Đạt có điều kiện | AABB geometry so với visible depth: 113/113 trong tolerance 0.05 m. Lineage là class-agnostic mask matching; persistent geometry chịu partial-view bias. |
| G4 — evaluator | Đạt cho metric có GT hợp lệ | Room/membership/semantics không chấm khi thiếu room GT xác nhận. M5 tracked-region history không có trong artifact đã khóa. |
| G5 — fixture tests | Đạt | 13 GT metric/validation tests, gồm ignored-GT overlap priority; full regression **269 passed in 103.74 s**. |
| G6 — apt_0 | **Partial** | Object-mask và một số geometry/association metrics đã chấm. Room/membership/adjacency/semantic accuracy null; apt_1/apt_2 chưa mở rộng trước khi apt_0 room GT được xác nhận. |

## Kết quả evaluator

Run hiện tại: `evaluation/runs/apt0_gt_eval_20261003_scope_v1_1/`, status `partial_evaluation_room_gt_pending`; input lock: `evaluation/runs/apt0_gt_20261003_01/manifest.json`. Các run evaluator cũ được giữ nguyên. v1.1 bổ sung chính sách overlap ignored masks sau run đầu; hiện chưa có prediction nào đổi từ FP sang ignored nên TP/FP/FN không đổi.

- All-loaded-rigid-instance mask coverage, class-agnostic, per-frame IoU ≥ 0.50 và visibility ≥ 64 px: 297 eligible GT observations; TP=30, FP=10, FN=267, cùng 320 visible masks dưới ngưỡng bị ignore. Precision=0.750; recall=0.101. Đây là observation-level coverage trên 113 rigid instances đã load, không phải unique-object recall, class accuracy, hay YOLO supported-vocabulary recall.
- Vocabulary inventory trên 297: **0** observation có detector→ReplicaCAD mapping đã human-confirmed; **78** có candidate mapping provisional; **219** không có detector-class candidate trong mapping hiện tại (chưa được người xác nhận là unsupported). Sáu articulated declarations không được non-Bullet runtime load nằm ngoài registry/mẫu số và không có visible instance-ID masks.
- Ignore-overlap: unmatched prediction bị tính ignored thay vì FP chỉ khi ≥50% diện tích prediction mask chồng một ignored GT mask cùng class; eligible match được ưu tiên. **0** prediction đáp ứng điều kiện trong run này; 10 FP vẫn là FP theo policy. Chi tiết ở `object_mask_metrics`/`object_scope_inventory.json` và protocol v1.1.
- P3: 23 accepted / 17 rejected trong 40 detections. Trong các lineage có independent mask match, 17 được giữ, 13 bị loại; cả 13 do `rejected_cluster_below_minimum`, dù có valid depth. Đây là xác nhận filter theo đúng điều kiện cấu hình, không chứng minh các observation ấy vô dụng.
- 17 accepted/mask-matched observations: median center error khoảng 0.181 m so với AABB của bề mặt nhìn thấy qua depth; không phải sai số centroid đầy đủ.
- Association chỉ trên lineage accepted/mask-matched: 10 GT instances được quan sát, 2 predicted tracks phân mảnh, 0 track có bằng chứng gộp nhiều GT instances.
- Persistent geometry: median distance khoảng 0.317 m tới full-mesh AABB center trên 12 track không mơ hồ; có partial-view/first-view bias.
- Có 5 predicted M8 RoomNodes; **không suy ra apt_0 có 5 phòng thật**.

## Artifact và review

- Protocol: [`evaluation/protocol.md`](../evaluation/protocol.md).
- Hướng dẫn review: [`evaluation/gt/apt_0/README.vi.md`](../evaluation/gt/apt_0/README.vi.md).
- Gói xem kiến trúc nhiều góc: [`evaluation/gt/apt_0/room_review_context_v3/index.html`](../evaluation/gt/apt_0/room_review_context_v3/index.html), gồm projected plan, bốn góc nhìn stage 3D và năm mặt cắt đồng trục ở Habitat Y=0.25/0.85/1.45/1.85/2.05 m. Đây chỉ là aid, không chứa GT/prediction.
- Room tool: [`evaluation/gt/apt_0/room_annotation_tool_v2/apt0_room_annotation.html`](../evaluation/gt/apt_0/room_annotation_tool_v2/apt0_room_annotation.html). Trạng thái xác nhận boundary và functional label tách riêng; object-room assignment nằm trong file annotation riêng. Nền là architectural stage-only slice, không phải prediction. Dùng `uncertain` khi cửa/open-plan/ranh giới không rõ; không tự gắn `human_verified`.
- Detection review: `evaluation/runs/apt0_gt_eval_20261003_scope_v1_1/overlays/object_mask_review.html` và PNG trong `overlays/detections/`.
- Prediction-only occupancy/room/object/trajectory: `evaluation/runs/apt0_gt_eval_20261003_scope_v1_1/overlays/baseline_prediction_only.png`; đây không phải GT.
- Visible instance masks, registry, visibility và geometry: `evaluation/gt/apt_0/`.
- Membership template có 113 entries, tất cả ban đầu `uncertain`; AABB center chỉ là gợi ý, không tự gán phòng.
- Failure cases P3 vẫn được lưu nguyên: 13 matched-but-rejected; thêm 6 accepted observations có center error >0.50 m được xếp làm triage-only review cases, không phải ngưỡng accuracy và không dẫn tới sửa P3.

Chạy lại evaluator (không chạy simulator, không gọi projection bổ sung):

```bash
MPLCONFIGDIR=/tmp/apt0-gt-mpl PYTHONPATH=.:src \
  python3 scripts/evaluate_apt0_ground_truth.py
```

Evaluator kiểm tra hashes trước khi chấm. GT chỉ dành cho evaluator, không được đưa vào builder. Baseline và capture được giữ nguyên.

## Còn lại / điều kiện tiếp tục

1. Người review xác nhận room polygons, doorway/corridor/open-plan convention và uncertainties trong `evaluation/gt/apt_0/room_polygons.geojson`.
2. Xác nhận doorway/adjacency trong `doorways_and_adjacency.json`.
3. Sau xác nhận, chạy evaluator để chấm room IoU, split/merge, boundary, coverage và adjacency. Membership cần object-room assignments được xác nhận riêng; semantic accuracy cần functional labels được xác nhận riêng.
4. Class-aware detector metrics cần class mapping được review; hiện không xem AI-assisted mapping là human GT.
5. M5 tracked-region temporal geometry cần instrumentation/lineage riêng vì lịch sử đó không có trong artifact đã khóa.
6. Chỉ sau khi apt_0 GT/evaluator được xác nhận mới mở apt_1/apt_2; chúng không được gọi là kiến trúc độc lập.

Các annotation cần phán đoán kiến trúc/semantic vẫn để `uncertain`; không tự suy diễn thành GT.
