# T2-D cached packet integration — báo cáo

## Phạm vi

Lượt này nối ba packet T2-B2 đã khóa vào một ConceptGraphs session và một
OccuSG session lâu sống, không chạy capture, detector, encoder, Qwen hoặc
OccuSG/ConceptGraphs batch lại ngoài các lần integration được ghi dưới đây.
Membership và room semantics giữ `unavailable` vì elevation scope chưa hợp lệ.

Input chính:

- source C2: `artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2`
- packet manifest: `artifacts/t2_b2_perception_smoke_20261018_v3/packet_join_manifest.json`
- scene: `00802-wcojb4TFT35`
- sequence: `hm3d-v02-00802-main-route-01`
- packet manifest SHA-256: `f2e6b687750f4caac1940ee22a8f003ac4f934bacc9a694e43eda9d1fca8a6a0`
- P2 index SHA-256: `bdfc8dc5b8a37c2cca6b343d074932643d435e29e41467f7883e09bd17d77a44`

## Code đã triển khai

- `integration_v2/t2d_coordinator.py`: packet preflight, viewer bootstrap,
  coordinator/barrier, atomic branch join, lifecycle, failure classification
  và runtime timing fields.
- `scripts/run_t2_d_cached_packet_integration.py`: entrypoint một run cached.
- `scripts/run_t2_d_cached_packet_worker.py`: một worker lâu sống cho mỗi
  nhánh; dùng interpreter đã khóa từ C2 manifest.
- `tests/test_t2d_coordinator.py`: packet/hash/worker-contract tests.

Các nhánh gọi core hiện có qua session wrappers; không tạo mapper subprocess
mới cho từng frame. Packet bundle ghi đủ ba packet trước khi worker khởi chạy,
kiểm tra source hashes và `future_manifest_required=false`. Unified revision
chỉ được ghi sau khi cả hai branch events, state files và hashes đã validate.
Worker interpreters được lấy từ C2 manifest: `.venv-cg-cpu/bin/python` cho
ConceptGraphs và `Rebuild_3DScene_OccuSG/.venv-perception/bin/python` cho
OccuSG; không gộp hai môi trường.

## Kiểm chứng model-free

Lệnh đã chạy:

```bash
python3 -m py_compile integration_v2/t2d_coordinator.py \
  scripts/run_t2_d_cached_packet_integration.py \
  scripts/run_t2_d_cached_packet_worker.py
python3 -m pytest -q tests/test_t2d_coordinator.py \
  tests/test_perception_feature_sessions.py \
  tests/test_conceptgraphs_session.py tests/test_occusg_session.py
```

Kết quả: **20 passed**. Các test bao gồm packet hash/schema, interpreter
separation, world-frame/depth metadata cho OccuSG packet và lifecycle/session
regressions hiện có.

Viewer D2 được bootstrap trước producer. Browser đã thấy trạng thái
`waiting_for_first_revision` tại:

```text
http://127.0.0.1:8773/artifacts/live_processing_runs/t2d_cached_b2_20261018_v3/viewer/index_d2_t2d.html?live=1
```

Đây là GUI bootstrap evidence; chưa phải simultaneous render acceptance.

## Execution ledger

| Run | Kết quả | Mapper mutation | Chi tiết |
|---|---|---:|---|
| `t2d_cached_b2_20261018_v1` | failed trước server/mapper | 0 | sandbox chặn bind socket (`PermissionError`); không có worker |
| `t2d_cached_b2_20261018_v2` | failed | 0 | ConceptGraphs thiếu accessor pose; OccuSG thiếu depth metadata; lỗi xảy ra trước frame update |
| `t2d_cached_b2_20261018_v3` | failed / `replay_required` | 1 object event | frame 0 được gửi cho cả hai; OccuSG dừng trước `builder.update` vì `Detection3D.frame_id` sai; ConceptGraphs đã publish prefix event và chờ barrier |

Artifacts và manifest tương ứng nằm dưới:

- `artifacts/live_processing_runs/t2d_cached_b2_20261018_v1/`
- `artifacts/live_processing_runs/t2d_cached_b2_20261018_v2/`
- `artifacts/live_processing_runs/t2d_cached_b2_20261018_v3/`

Ở v3: `object_branch_events=1`, `room_branch_events=0`,
`unified_revisions=0`, `terminal_postprocess=pending`; manifest đã ghi
`failure_classification.replay_required=true`. Không có retry trong cùng
session và không có unified revision giả.

Sau v3, code đã được sửa để dùng `frame_id="world"` cho `center_W`, thêm
depth metadata (`m`, `axial`, scale 1.0) và dùng pose từ `RGBDFrame` đúng
adapter. Các sửa này chưa được chạy lại bằng mapper trong lượt này; vì v3 đã
mutation một nhánh, replay tiếp theo phải bắt đầu từ run directory mới và
cần được duyệt riêng.

## Gate status

- Packet preflight/hash validation: **đạt**.
- Worker environments/lifecycle design: **đã triển khai; smoke bị chặn bởi lỗi contract trên run v3**.
- Browser/server bootstrap: **đạt ở mức waiting state**.
- Hai mapper nhận cùng ba frame và commit ba unified revisions: **chưa đạt**.
- Terminal ConceptGraphs finalize + OccuSG flush: **chưa chạy thành công**.
- GUI simultaneous acceptance khi producer còn chạy: **pending**.
- Live capture/realtime: **ngoài phạm vi**.

Không có runtime thành công để báo per-stage processing/join/render. Code đã
có trường timing cho preflight, viewer bootstrap, per-frame join, terminal và
RSS; run v3 dừng trước khi ghi được frame join hợp lệ. Anomaly 48.821 s của
frame 3 vẫn chỉ là observation cũ, không được dùng để suy nguyên nhân.

## Bước tiếp theo nhỏ nhất

Không tự chạy tiếp. Cần một run replay mới từ state rỗng bằng code đã sửa,
vẫn đúng ba packet và không chạy perception lại, để kiểm chứng OccuSG
`world` frame/depth contract rồi mới đánh giá packet integration. Nếu replay
đạt, bước sau đó mới là đề xuất camera-live pilot; chưa triển khai trong
lượt này.
