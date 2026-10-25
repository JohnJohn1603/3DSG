# CL1 contract hardening sau run v6

## Phạm vi và trạng thái

Đã hoàn tất hardening model-free cho đường file-backed camera-live hiện có.
Run v6 vẫn là `failed_partial`; không chạy lại camera, perception, mapper,
native processing, model hoặc browser. Không thay route, model, thresholds,
matcher, bảng A hay scope.

Handoff gốc về v6 được giữ nguyên tại
[`CAMERA_LIVE_CL1_HANDOFF.vi.md`](CAMERA_LIVE_CL1_HANDOFF.vi.md). Artifact
v6 không bị sửa.

## Nguồn timestamp và clock domains

`timestamp_s` lấy từ `sim.get_world_time()` tại observation Habitat và phải
có `timestamp_clock_domain=habitat_simulation`. Nó được giữ nguyên qua
keyframe metadata, P2/P3/OpenCLIP session input/output, mapper packet và
branch event. Không có fallback sang 0 hoặc thời gian hiện tại.

Capture wall time là `capture_wall_time_unix_s`; capture timing và stage
timing dùng `capture_monotonic_s` / `stage_started_monotonic_s` cùng
`host_monotonic:<boot-id>`. Journal publication cũng ghi cả wall và
monotonic clock. So sánh overlap/latency chỉ hợp lệ khi clock domain khớp;
simulation time không bị trừ khỏi wall time.

Mỗi perception worker kiểm tra frame identity, timestamp/domain, capture
record, source hashes và hash của RGB/depth/mask/metadata trước khi gọi
`session.update_frame`. P3 xác minh hash mask trước khi dùng mask. Response
của mỗi stage phải trả lại đúng clocks, IDs và source hashes; packet join
reject mismatch hoặc thiếu observation. Empty detection là trạng thái
hợp lệ; stage exception vẫn là lỗi riêng.

## Serialization, admission và readiness

- Camera journal chỉ đọc record JSONL đã kết thúc bằng newline; khi camera
  kết thúc, journal còn tail dang dở thì run lỗi thay vì âm thầm bỏ event.
- Persistent P2/P3/OpenCLIP và hai mapper ghi ready marker sau khi khởi tạo.
  Habitat chỉ được khởi chạy khi cả năm worker ready và cùng monotonic
  clock domain. Lỗi spawn hoặc readiness dọn các process/server thuộc run,
  đóng log handles và ghi manifest `failed_before_camera`; không khởi chạy
  camera khi gate này chưa đạt.
- Sau publication, coordinator giữ tối đa một packet pending. Ghi riêng
  `queued`, `admitted_to_processing`, `superseded`, `processed`; thay thế
  chỉ xảy ra trước dispatch. Packet đã dispatch được gửi giống hệt tới cả
  hai mapper trước khi đợi pair barrier.
- Unified join đối chiếu frame ID/index, source timestamp/domain và hashes
  từ cả hai mapper events. Một nhánh lỗi không thể được lấp bằng kết quả
  nhánh kia. Reader/publisher tiếp tục độc lập với browser.
- Manifest pilot sẽ so sánh capture end, perception start và unified
  revision commit theo cùng monotonic domain, đồng thời báo capture→P2 và
  perception→commit latency khi cùng frame/domain. Đây là timestamp commit
  vào D2 live index; chưa phải bằng chứng browser đã render frame.

## Kiểm chứng model-free

Test mới đi qua raw keyframe file/hash → newline-JSON worker request → P2,
P3 và OpenCLIP worker adapters với fake sessions → joined packet → hai fake
mapper subprocess qua file-backed IPC → unified join builder hiện hành.
Mapper adapters thật được gọi ở pre-mutation validation boundary; không
được gọi `update_frame` thật.

Bao phủ detections, empty detections, P3 rejected, thiếu `timestamp_s` ngay
tại OpenCLIP worker, source hash mismatch, mask hash mismatch, failed fake
inference, complete/partial JSONL, queue replacement/race, worker readiness,
pair dispatch và clock-domain overlap. Ca thiếu timestamp chứng minh encoder
callback chưa chạy. Ca thành công chứng minh timestamp `1.375` từ frame
nguồn giữ nguyên tới cả hai mapper boundaries.

Đã chạy:

```text
python3 -m py_compile \
  integration_v2/camera_live_contract.py \
  scripts/run_camera_live_pilot.py \
  scripts/run_habitat_smooth_route_preview.py \
  scripts/run_t2_d_cached_packet_worker.py \
  tests/test_camera_live_contract_ipc.py

python3 -m pytest -q \
  tests/test_camera_live_contract_ipc.py \
  tests/test_camera_live_coordinator.py \
  tests/test_t2d_coordinator.py \
  tests/test_smooth_route.py \
  tests/test_unified_incremental_timeline.py \
  tests/test_unified_scene_graph.py
```

Kết quả cuối: **44 passed**. Không có model loads, camera frames, P2/P3
inference, mapper update hay native replay trong lượt này.

## Provenance source

Run v6 giữ nguyên các giá trị được lưu lúc lỗi:

- `pilot_failure_summary.json` → `pilot_script_sha256`:
  `12fa8cf50f2d4b3dd91e2198a1c8f505505a82a1d5a1a9188adf32faa4a3549d`
- `pilot_failure_summary.json` → `camera_runner_sha256` và v6 manifest
  → `worker_sha256`:
  `b079a038d71cbd34577c551960f9da82963985f4e15814aceac9e974036eaf54`
- v6 manifest → `smooth_route_module_sha256`:
  `1cc0d666630ff199fa5d0843645834cdbd68912610720c2c1561af06cfc0d590`

SHA-256 hiện tại sau hardening:

| File | SHA-256 hiện tại |
| --- | --- |
| `scripts/run_camera_live_pilot.py` | `ccee2007ed91dbfee93edd6cef24ef17e45a66c460caba261716d529f1d8f796` |
| `scripts/run_habitat_smooth_route_preview.py` | `d6f8306a55b07ac1aa0f3ccfe78567440fe42e88b348bb5e8b112143d36a2908` |
| `scripts/run_t2_d_cached_packet_worker.py` | `e67753d6fc373dafcfe72992f29cfd60e1832bfbea3cea59c92092a24a1d175c` |
| `integration_v2/camera_live_contract.py` | `9800a9e596b8af16a271db24847c280d4e3550155b081971a58c9bcf4c3d764f` |
| `tests/test_camera_live_contract_ipc.py` | `645113117abb2866612aee788bee27ac60b3fdc712903bde38db4bbccd02946d` |

## Gate và đề xuất pilot

**Model-free contract gate: đạt. Pilot thật: chưa chạy.** v6 không tạo
mapper packet nào và không có unified revision; nguồn lỗi ban đầu là
`KeyError: timestamp_s` tại `OpenCLIPFeatureSession.update_frame`.
Regression mới tái hiện lỗi đó trên đúng feature worker path và xác nhận
được chặn trước encoder callback.

Đề xuất đúng **một** pilot mới từ state rỗng, trần **120 phút** gồm cold
start, chạy route, mapper sessions, terminal stage, kiểm tra artifacts và
GUI cơ bản. Đây là ngân sách giới hạn, không phải runtime đã đo. v6 chưa
chạm mapper nên thời gian mapper/native và tổng thời gian pilot vẫn chưa có
đo lường. Không có retry hoặc verification replay thứ hai trong phạm vi này.

Execution dự kiến: một Habitat capture trên route khóa
`smooth_t1_1_hm3d00802_turn_continue_20261018_v2` (route trước đã ghi 113
camera frames, 11.648 simulation seconds; không suy ra wall-clock overlap
từ con số đó); một P2 session, một P3 session, một OpenCLIP session; một
ConceptGraphs session và một OccuSG session; terminal finalize/flush mỗi
nhánh một lần. Số keyframe thực tế phải lấy từ queue log; không mặc định
mọi 13 keyframes trước đây đều được processing-admit trong pilot mới.

Acceptance pilot sẽ yêu cầu: worker readiness trước capture; P2 bắt đầu
trước capture end; ít nhất một unified revision được commit trước capture
end theo cùng monotonic domain; cùng frame/hash tới hai mapper; final stages
tách riêng; queue/drop và cleanup được ghi rõ. Browser render acceptance
cần GUI evidence riêng. Nếu producer thất bại thì giữ partial run và dừng,
không retry. Camera-live không đồng nghĩa wall-clock realtime; membership
và room semantics tiếp tục unavailable khi scope gate chưa đạt.
