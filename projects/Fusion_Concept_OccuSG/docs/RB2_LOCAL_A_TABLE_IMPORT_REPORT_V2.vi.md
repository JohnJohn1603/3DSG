# RB2 local static diagnostic v2

## Thay đổi contract

Bản v1 trong `rb2_local_import_20261015` đã bị superseded vì cộng
`uncalibrated_normalized_score` rồi normalize. Bản v2 dùng trực tiếp
RB1 `RoomBeliefLedger`:

```text
S_R(r) = log(runtime_prior(r))
         + sum(A_log_ratio_uncalibrated(category, r))
belief_R = softmax(S_R)
```

Mỗi category eligible chỉ góp một lần. Nhiều object cùng category vẫn
được giữ trong ledger để retract, nhưng không nhân đôi A contribution.
Consensus lấy từ `readiness.json` (`semantic_category_raw_exact`,
`semantic_status`, `semantic_category_support_by_pose_unit`), không tính
lại majority từ raw frame history.

Ledger đã tách `room_node_ids` vật lý khỏi semantic room labels trong A;
API cũ không truyền `room_node_ids` vẫn giữ hành vi cũ.

## Lệnh tái lập

```bash
python3 scripts/import_rb2_a_tables.py \
  --archive artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip \
  --readiness artifacts/room_belief/readiness/u6_00808_hybrid_v3_static_v4/readiness.json \
  --output artifacts/room_belief/rb2_local_import_20261015_v2_final
```

Output:

- [Manifest](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/room_belief/rb2_local_import_20261015_v2_final/import_manifest.json)
- [Static diagnostic v2](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/room_belief/rb2_local_import_20261015_v2_final/static_room_diagnostic.json)
- [HTML report](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/room_belief/rb2_local_import_20261015_v2_final/rb2_a_table_report.html)

## Kết quả cache hiện có

Trên cả bốn biến thể, chỉ `sofa` là category consensus stable và eligible
cho `room:1`. Hypothesis conflicted và hypotheses filtered không đóng góp.
Vì có 1 unique eligible category trong khi policy yêu cầu 2, room giữ
`abstain`; không hạ threshold.

Các output v2 lưu riêng `A_contributions`, `runtime_prior`, `raw_S`,
`belief`, candidate/commit/status/reason, category object IDs và ledger
snapshot cho từng biến thể.

## Tests

```bash
python3 -m pytest -q \
  tests/test_rb2_room_belief_integration.py \
  tests/test_room_belief_v1.py \
  tests/test_rb2_a_table_import.py \
  tests/test_qwen_room_scorer.py \
  tests/test_room_belief_readiness.py
```

Kết quả: `38 passed, 1 skipped`. Test Qwen bị skip vì môi trường local
không có `torch`; không có model nào được chạy lại.

Các bảng A gốc không bị invalidated. Chỉ static diagnostic v1 bị thay thế;
RB2 quality selection và causal online integration vẫn pending.
