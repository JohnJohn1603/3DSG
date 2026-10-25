# Q0 hoàn tất và proposal Q1

> **LEGACY / HISTORY (2026-10-12):** This plan/handoff belongs to the earlier U/K/Q development track. It is retained for provenance and is not the active roadmap. Follow [the current ConceptGraphs → OccuSG plan](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md).


Ngày: 2026-10-11. Plan chính là [`OBJECT_GRAPH_QUALITY_RECOVERY_PLAN.vi.md`](OBJECT_GRAPH_QUALITY_RECOVERY_PLAN.vi.md); tài liệu này chỉ ghi tiến độ Q0/Q1, không thay đổi lịch sử U0–U6/K0–K6 hoặc trạng thái gate cũ.

## Q0 — offline association runner

Đã mở rộng `scripts/run_u3_surface_support_candidate.py` để dựng ba mode trực tiếp từ capture/P2/P3 và selected-point sidecar:

- **Source:** gọi `ObjectAssociationManager` thật, giữ thứ tự frame và thứ tự detections trong P3 packet. `same_pose_suppressed` vẫn là source association decision, không tạo alias.
- **Geometry-only-dedup-v2:** tái dùng `canonicalize_same_frame_duplicates_v2` và `ExperimentalAssociation`; alias/canonical được tính lại từ mask/P3 của frame hiện tại. Alias không vào matcher hay surface accumulator.
- **K4:** giữ matcher/accumulator hiện tại; mọi assignment của frame được chốt trước khi fusion nhận geometry frame đó. Defer không cập nhật surface.

Source/dedup native replay và K4 lock giờ là **tùy chọn parity references**. Runner từ chối capture/P2/P3/sidecar hash mismatch; nếu reference không được truyền, predictor vẫn chạy và ghi `reference_parity_status=not_requested`. Evaluator ledger là hậu kiểm tùy chọn, không phải predictor input.

Một thử nghiệm mismatch sidecar được chạy bằng manifest tạm trong `/tmp`: cố ý thay `capture_manifest_sha256`, runner từ chối ngay với `P3 evidence capture hash mismatch`. Artifacts nguồn không bị sửa.

Lệnh đã chạy trực tiếp, **không có replay arguments**:

```bash
python3 scripts/run_u3_surface_support_candidate.py \
  --input-run artifacts/runs/hm3d_00802_20261011T033053.236654Z_086a73 \
  --p3-output artifacts/audits/object_graph_improvement/p3_prefix_0_9_v1 \
  --p3-output artifacts/audits/object_graph_improvement/p3_suffix_10_28_k4_v1 \
  --output artifacts/audits/object_graph_improvement/q0_offline_three_mode_no_references_v2
```

Lệnh parity trên cùng cache đã chạy thành công với `--source-replay`, `--dedup-v2-replay` và `--k4-reference` trỏ đến artifacts đã khóa; kết quả ở [`q0_offline_three_mode_validation_v4`](../artifacts/audits/object_graph_improvement/q0_offline_three_mode_validation_v4/manifest.json). Chạy không reference và chạy có reference tạo assignment maps giống nhau cho cả 45 observations.

| Mode | Partition so reference | Decision/evidence check |
|---|---|---|
| Source | bằng nhau; 23 membership groups không đổi | 0 decision mismatch; 23 created, 21 same-class match, 1 same-pose suppression |
| Geometry-only-dedup-v2 | bằng nhau; 26 groups không đổi, 10 group chỉ đổi track ID | 4/4 alias canonical owners khớp; backend alias event được phân biệt với `duplicate_suppressed` ở assignment layer |
| K4 | partition và per-observation track assignment khớp K4 lock | 1 defer, 1 surface-continuity match; alias/defer không góp geometry hai lần |

Metrics hậu kiểm tái lập trên 00802: source `9/0/8`, F1 `0.692`; dedup-v2 `7/0/10`, F1 `0.583`; K4 `9/0/8`, F1 `0.692`. Đây là development cache, không phải quality claim mới. K4 touching-surfaces fixture vẫn là known failure; test tái hiện nó không được tính là safety pass.

Targeted tests thực chạy:

```bash
PYTHONPATH=. pytest -q \
  tests/test_p3_object_evidence_indices.py \
  tests/test_u3_experimental_association.py \
  tests/test_surface_support_association.py \
  tests/test_same_frame_duplicate_candidate_v2.py \
  tests/test_u5_duplicate_v2_backend.py \
  tests/test_profile_comparison.py \
  tests/test_m6_object_association.py
```

Kết quả: **58 passed**. Cũng chạy `py_compile`, CLI `--help`, replay offline có reference và không reference. Một invocation test ban đầu thiếu `PYTHONPATH=.` và pytest không import được package `scripts`; lệnh chuẩn bên trên đã chạy thành công. Không chạy full suite. Không chạy capture, detector, P3 clustering hoặc native replay.

## Q1 — inventory và pilot proposal

Inventory version mới: [`quality_recovery_q1_inventory_v1/manifest.json`](../artifacts/audits/object_graph_improvement/quality_recovery_q1_inventory_v1/manifest.json). 00800/00802/00808 hiện có đều là development/diagnostic đã xem; không có test untouched nào được khóa. 00802 có 45 direct selected-point records ở hai sidecar roots. Existing 00800/00808 caches chưa có selected-point sidecar K4 cần. Human annotations chỉ evaluator, giữ nguyên full observation IDs và không mở rộng ngoài scope.

Đề xuất route 00808 đã đăng ký trong [`route config`](../configs/habitat/hm3d_minival_v02_00808_k4_accumulated_geometry_route_v1.json): 7 pose ở 640×480, HFOV 90°. Preview RGB và navmesh preflight có sẵn. A/B/C nhìn khu seating từ các vị trí khác nhau; preview thấy sofa, bàn thấp, bàn ăn và ghế ở gần nhau. Detour kitchen rồi quay lại tạo re-observation. Preview chỉ xác nhận vùng nằm trong field of view, **không bảo đảm** P3 chấp nhận cùng vật qua nhiều pose.

Pilot cần thu được đủ ba điều kiện trước khi chấm: (1) một vật P3-accepted từ nhiều vị trí camera; (2) partial-view expansion; (3) vật gần nhau có nguy cơ false merge. Nếu route 7 pose không có evidence đó, dừng `insufficient pilot evidence`, không thêm frame hoặc sửa route.

| Stage | Ước lượng |
|---|---:|
| Capture 7 frame | 2–4 phút |
| P2 | 1–3 phút |
| P3 + selected-point sidecar trong cùng lượt clustering | 5–8 phút |
| Offline source/dedup-v2/K4 + manifest | 1–3 phút |
| **Tổng pipeline** | **9–18 phút** |

Đây là new-route development test, không phải unseen-scene test. Không gồm native replay, U4-OV, OpenCLIP hoặc feature extraction. Peak RAM hires chưa có phép đo cùng phạm vi; sẽ ghi khi được duyệt chạy. Runner implementation/parity/tests nằm trong ngân sách Q0 tối đa 45 phút đã duyệt, tách khỏi 9–18 phút pipeline; thời gian code chính xác không được instrument riêng.

**Q1 capture chưa được duyệt và chưa chạy.** Bước tiếp theo là xin duyệt riêng route/budget 9–18 phút; không tự capture.
