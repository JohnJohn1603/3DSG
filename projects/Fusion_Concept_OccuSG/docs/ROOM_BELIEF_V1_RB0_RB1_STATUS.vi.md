# Room Belief v1 — RB0/RB1 status

**Historical checkpoint.** This file records the earlier state before the static U6 00808 readiness pass and RB2 notebook preparation. See the newer [RB1/RB2 preparation status](ROOM_BELIEF_V1_RB1_RB2_PREPARATION_20261014.vi.md) for current gates and results.

Ngày: 2026-10-14. Plan thiết kế đầy đủ: [ROOM_BELIEF_V1_IMPLEMENTATION_PLAN.vi.md](ROOM_BELIEF_V1_IMPLEMENTATION_PLAN.vi.md).

## Gate status

- **RB0 — complete, local preflight only.** Không tải/chạy Qwen, không cài package, không dùng Kaggle.
- **RB1 — model-free controller contracts implemented and targeted-tested.** Chưa nối event stream vào launcher/runtime và chưa chạy trên cache thật.
- **RB2 — blocked pending separate approval and a suitable model environment.** Không có Qwen A table thật.
- Default launcher, matcher, object geometry, OccuSG room geometry, baseline và room scores hiện có không đổi.

## RB0 findings

Read-only checks trên máy local cho thấy:

- RAM khả dụng khoảng **6.3 GiB**, swap khả dụng còn khoảng **2 GiB**; disk workspace còn khoảng **309 GiB**.
- Python mặc định là 3.12.3; có Torch CPU 2.6.0 trong `.venv-appearance` và Torch CPU 2.0.1 trong `.venv-cg-cpu`. Không thấy GPU (`nvidia-smi` không có; Torch báo CUDA unavailable).
- Transformers/tokenizers chưa có trong các Python đã kiểm tra. Không thấy cache weights Qwen3-4B-Base.
- Ước lượng weights FP16 thô khoảng 8 GB, chưa gồm framework/load/scoring overhead. Do đó môi trường hiện tại không đủ headroom để đề nghị chạy model an toàn.
- Chưa có token-scoring path Qwen trong workspace. Chỉ có scoring/evaluation code khác, không phải implementation Qwen theo plan.

Kết luận: giữ Qwen3-4B-Base làm model chính. Bước sinh bảng A cần một execution environment riêng được duyệt; có thể cân nhắc đề xuất offline Kaggle chỉ với ontology và object category strings. Chưa upload hoặc chạy gì.

## RB1 implementation

Code mới ở [room_belief_v1.py](../src/standalone_sg/semantics/room_belief_v1.py) và được export từ package semantics. Đây là module opt-in, không được gọi bởi launcher hay graph backend.

- **Object semantic consensus:** caller cung cấp raw class, canonical category và versioned view-group ID. Output giữ semantic revision, raw observation history, category support và eligible/excluded IDs. Alias, deferred và filtered observations bị loại; mỗi view group có tối đa một đơn vị support, và category xung đột trong cùng nhóm chia đơn vị đó. Không tự ánh xạ synonyms, không dùng detector confidence như xác suất.
- **Room membership:** `greedy_voxel_support_membership` nhận voxel representatives canonical-world XYZ mét và polygon room XY hiện có. Đếm support mỗi room, ghi outside/tie/support fractions, geometry revisions và tie-break deterministic. M8 resolver cũ không bị thay đổi. Vì room geometry hiện là polygon 2D, membership này chưa lọc theo Z.
- **Incremental ledger:** `RoomBeliefLedger` giữ reverse index room→category→object IDs; category A-row đóng góp một lần khi ít nhất một retained, stable, assigned object hỗ trợ category. Thêm/xóa object cùng class cập nhật count nhưng không cộng/rút contribution cho tới khi count qua 0. Class correction và room transfer rút đóng góp cũ rồi thêm mới trong một lần apply.
- **Causality/event safety:** event IDs idempotent; payload reuse sai bị từ chối; revision cũ bị trả `stale_rejected`; cùng revision với payload khác bị lỗi. Retract, serialization/replay và room relabel hysteresis dùng evidence revisions thay vì số lần gọi scoring.
- **Uncertainty:** object không stable, không retained, membership ambiguous/unassigned, hoặc category thiếu A row không góp room score. Room belief được ghi rõ là normalized uncalibrated score, không phải xác suất đã hiệu chuẩn.

API membership yêu cầu voxel size và grid origin tường minh khi gọi. Module hiện chưa tự voxelize cloud hay nhận hình học 3D; caller phải cung cấp representatives và provenance phù hợp. Chưa có A table thật, nên ledger chỉ được kiểm tra bằng score rows tổng hợp trong unit tests.

## Threshold proposal — chưa duyệt, chưa kiểm chứng

Proposal ở [consensus_v1.proposal.json](../configs/room_semantics/consensus_v1.proposal.json) và [room_belief_v1.proposal.json](../configs/room_semantics/room_belief_v1.proposal.json) mang trạng thái `proposed_unapproved`:

- Object consensus: tối thiểu 2 view groups, top share ≥0.70, margin ≥0.20.
- Room label: tối thiểu 2 supported categories, top normalized score ≥0.60, margin ≥0.20.
- Relabel: nhãn mới hơn nhãn đang được hỗ trợ ≥0.15 qua 2 category-presence evidence revisions.

Các giá trị này là điểm bắt đầu để xem xét, không phải defaults đã chốt, không phải calibrated thresholds và chưa được chạy trên dữ liệu chất lượng. `view_group_policy_id` phải do caller cung cấp; chưa có heuristic gộp pose được xác nhận. Voxel size cũng để trống trong config proposal và phải lấy từ representation thật.

## Verification

Command đã chạy:

```bash
PYTHONPATH=src python3 -m pytest -q \
  tests/test_room_belief_v1.py \
  tests/test_region_object_room_semantics.py \
  tests/test_room_semantics.py \
  tests/test_m8_rooms.py
```

Kết quả: **42 passed**. Test mới bao phủ grouped consensus/alias/defer/filter, membership support/tie/outside/empty geometry, category cap/retract, correction/room transfer, stale/idempotent/conflicting revisions, relabel hysteresis, raw semantic/membership evidence serialization, tamper rejection và transaction rollback. Đây là kiểm thử contract/synthetic, không phải quality evaluation.

## Còn cần quyết định trước RB2/R3

1. Duyệt hoặc sửa các consensus/relabel threshold proposal; chốt chính sách view grouping và voxel resolution từ object surface representation.
2. Duyệt riêng môi trường/quyền chạy Qwen3-4B-Base hoặc chọn đề xuất sinh bảng A offline. Local preflight hiện không đủ RAM và thiếu dependencies/weights.
3. Sau khi có bảng A, nối adapter với object semantic state thật và room geometry revisions; sau đó chạy static evaluation. Chưa có causal live event integration hay runtime label output trong lượt này.
