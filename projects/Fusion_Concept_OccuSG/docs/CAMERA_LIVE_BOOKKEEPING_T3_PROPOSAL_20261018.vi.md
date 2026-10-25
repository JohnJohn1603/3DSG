# Camera-live bookkeeping và proposal T3

Ngày: 2026-10-18

## 1. Bookkeeping đã harden

- Tách `pilot_run_id` khỏi `route_run_id`/`sequence_id`; các ID của route không còn ghi đè ID của pilot.
- Route producer trong pilot ghi sidecar `camera_producer_manifest.json`; `run_manifest.json` tiếp tục thuộc pilot. Đường route runner độc lập vẫn dùng `run_manifest.json`.
- Bootstrap và pilot manifest được chuyển cùng trạng thái terminal: `startup_preflight_passed`, `startup_preflight_failed`, `completed`, `failed` hoặc `cancelled`. Từ chối route ID không khớp và chuyển trạng thái terminal mâu thuẫn.
- Giữ nguyên artifacts v8. `pilot_bootstrap.json` v8 vẫn ghi `bootstrapping` vì đó là trạng thái lịch sử trước khi sửa; không sửa ngược manifest.

### Kiểm chứng

Lệnh tests:

```bash
python3 -m pytest -q tests/test_camera_live_startup.py tests/test_camera_live_coordinator.py tests/test_camera_live_contract_ipc.py
```

Kết quả: **34 passed**. Tests bao gồm route/pilot manifest riêng, output path không ghi đè, ID route khác pilot, lifecycle completed/failed/cancelled và startup preflight pass/fail, cùng việc từ chối terminal transition xung đột.

Startup dry-run đã đi qua launcher, IPC readiness giả và route runner validation trong đúng thứ tự runtime, tới ranh giới trước capture:

```bash
python3 scripts/run_camera_live_pilot.py \
  --config artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2/effective_config.json \
  --source artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2 \
  --output artifacts/live_processing_runs/camera_live_startup_hardening_20261018_v1 \
  --port 45241 --startup-dry-run
```

Run `camera_live_startup_hardening_20261018_v1` kết thúc với `startup_preflight_passed`, `terminal=true`; cả hai manifest có cùng pilot ID, route ID và thời điểm kết thúc. Viewer server và năm fake workers khởi động/đóng sạch. Route validation đạt; **capture không bắt đầu, không load model, không chạy inference**. Dry-run chưa xác minh Habitat simulator initialization, collision probe lúc runtime hoặc model initialization/inference. Đây không phải GUI acceptance.

Engineering elapsed không được đo bằng task timer nên không ghi một con số phút giả. Phạm vi thực hiện dừng ở bookkeeping, tests và dry-run; không chạy producer thật.

## 2. Đọc logs v8 — chỉ dữ liệu đo được

Nguồn: `artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/`. Route đúng là `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`.

| Đoạn | Kết quả |
|---|---:|
| Route producer / capture | 113 camera frames; producer wall runtime 12.80883 s (từ route-runner start tới hoàn tất, không phải capture-only); 11.648 s simulation; capture timestamps span 3.642 s từ frame đầu tới frame cuối; capture-stage start không được lưu riêng trong v8 |
| Keyframes | 13 candidates/persisted; 11 superseded trước xử lý; 2 admitted/processed |
| Keyframe đầu | capture 3907.04813; publish 3907.14439; consume 3907.22022; P2 start 3907.24489 (cùng host monotonic clock) |
| Perception frame 0 | P2 khoảng 0.449 s; P3 khoảng 1.835 s; OpenCLIP khoảng 1.969 s từ stage start tới event nhận response |
| Perception frame 110 | P2 khoảng 0.625 s; P3 khoảng 1.378 s; OpenCLIP khoảng 1.395 s từ stage start tới event nhận response |
| ConceptGraphs update | 368 ms và 557 ms |
| OccuSG update | 7.135 s và 9.011 s |
| Mapper pair dispatch → unified commit | 7.276 s và 9.127 s |

Perception durations là hiệu giữa `stage_started_monotonic_s` và thời điểm runner ghi event nhận response; gồm biên IPC/ghi response, không phải benchmark thuần model. Logs không có timestamp kết thúc độc lập cho từng kernel. Hai mapper chạy song song trong cặp; OccuSG có thời gian update lớn nhất được đo và là bottleneck định lượng của thời gian chờ unified commit trong run này. `12.80883 s` trong pilot manifest lấy từ `summary.wall_clock_runtime_s` của route runner (từ lúc runner bắt đầu, bao gồm preflight/khởi tạo và drain); không được gọi là capture-stage duration.

Perception bắt đầu **3.457 s trước capture end**, nhưng revision đầu commit **6.276 s sau capture end**. Revision thứ hai cũng commit sau capture end. Do đó v8 đạt pipeline end-to-end nhưng **không đạt graph/camera overlap**. Simulation duration không được dùng để suy ra wall-clock overlap.

Run có 2 unified revisions, 0 final retained objects; membership `unavailable_elevation_scope`, room semantics `unavailable_membership_scope`. Đây không phải acceptance của room labeling trên dữ liệu thật. GUI live render vẫn pending vì không có browser surface được xác nhận.

Hash sau hardening khớp với giá trị đã ghi trước đó; v8 không bị sửa:

| Artifact | SHA-256 |
|---|---|
| `pilot_acceptance_manifest.json` | `67e10ed9adb021eb19820f6710bfe97fea86e42f107a0d4609770cab945e6fef` |
| `pilot_integrity_manifest.json` | `185c2cb8ef246fba9cfe1b5d1983a56d1b22df5084b619c6963b132d377c0cc4` |
| `artifacts_sha256.json` | `2a6f43f200fd1d3c14ff2827826c5dd1945f63ee0d8342afa3520de69cbc113c` |
| `run_manifest.json` | `bd83c4e9b5def51090b017da29a885713665af36c5d64d82d582a59663241bc9` |
| `pilot_bootstrap.json` | `3b54ce52bc0250237f5d2e1522d80b5752cdec03ed9aeb3879a310c93424ee6d` |

## 3. Hai execution mode — định nghĩa, chưa triển khai

1. `continuous_preview_latest_wins`: giữ camera preview liên tục; queue hữu hạn; chỉ packet chưa dispatch mới có thể bị supersede. Báo frame bị drop/supersede và graph lag theo clock domain phù hợp. Không bảo toàn mọi keyframe.
2. `evidence_preserving_backpressure`: queue đầy thì pause simulation; frame đã admitted không bị drop hoặc xử lý hai lần. Không nhân bản frame để tăng evidence. Đây là chế độ đồng bộ theo pipeline, không phải wall-clock continuous/realtime.

Chưa thay đổi hoặc chạy mode nào trong lượt này.

## 4. Proposal T3 hữu hạn

### Logic giữ nguyên

Tái sử dụng causal room-belief runner và RB1 ledger/controller: prefix admission theo config hiện hành; consensus theo view groups; mỗi unique eligible category đóng góp tối đa một lần cho mỗi room, với ledger theo object để retract; cập nhật frame-atomic; lookup bốn A variants độc lập; commit/abstain/relabel theo policy đang khóa. Không dùng final retained objects, final consensus hoặc final membership để điền ngược prefix. Không chạy Qwen, đổi A tables hoặc thresholds. Fixture logic và dữ liệu scene báo riêng.

### Dữ liệu có thể nghiệm thu

**A. Verified-only (khuyến nghị làm gate đầu):** cache `hm3d00802_main29_640_quality_v1_attempt2`, sequence `hm3d-v02-00802-main-route-01`, 29 frame revisions, 75 P2 observations, cùng object timeline và room revision references hiện có. Đây là development data, không phải holdout. Scope hiện vẫn `unavailable_elevation_scope`; vì vậy real run chỉ nghiệm thu prefix consensus/readiness và việc gate membership giữ đóng. Không kỳ vọng `room_contains` hay room labels; không chấm room accuracy. Có thể kiểm thử commit/abstain, retract và category cap bằng fixtures riêng.

**B. Assumed single-level demo (chỉ sau declaration và duyệt rõ):** đúng scene `00802-wcojb4TFT35`, sequence `hm3d-v02-00802-main-route-01`, frames 0–28 và 5 room polygons của cache trên. Declaration phải nói rõ giả định rằng các object clouds và 2D room polygons đang xét thuộc một elevation/projection scope tương thích; không chứng minh toàn scene một tầng, không phải verified/multi-floor. Cần thêm nhánh contract được gắn `assumed_single_level_demo`, provenance và nhãn trên viewer; tuyệt đối không nhập kết quả vào verified metrics/default. Hiện runner nhận verified scope contract; không tự lách gate bằng cách ghi `verified` vào metadata.

Không có room-function GT đã đối chiếu với đúng 5 polygon hiện tại; do đó cả hai lựa chọn đều **không cho phép claim accuracy/F1**. Annotation room:6 của lượt cũ không tự động ánh xạ sang polygon/run mới.

### Phạm vi, acceptance và ngân sách đề nghị

- Engineering: tối đa **90 phút** cho preflight/cache pinning, reuse/adaptor cần thiết, tests và xuất unified state/viewer report; không sửa mapper/pipeline model.
- Chạy: một lần static causal replay trên 29-frame cache đã khóa (không capture, detector, encoder, mapping, native replay hoặc Qwen); tối đa 5 phút dự kiến, không retry. Fixtures là tests riêng, không tính là real-scene evidence.
- Acceptance chung: prefix không có future evidence; đúng admission/view groups; category cap + retract + idempotence; bốn variants cùng evidence/config; unavailable được giữ đúng; commit/abstain reason được xuất; fixture parity với RB1 controller.
- Nếu chọn A: real-data output xác nhận scope unavailable và không tạo edge/label; đây là integration/readiness pass, không room-label demo.
- Nếu chọn B: chỉ chạy sau khi người dùng duyệt declaration trên; labels được hiển thị rõ là assumed-scope experimental, không có promotion.

**Điểm dừng:** sau một cache replay và test set; không mở scene khác, không tuning, không lặp benchmark. Trước khi chạy cần chọn A hoặc phê duyệt rõ B.
