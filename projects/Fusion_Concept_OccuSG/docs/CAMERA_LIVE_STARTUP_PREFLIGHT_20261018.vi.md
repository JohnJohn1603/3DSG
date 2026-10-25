# Camera-live startup preflight — 2026-10-18

## Kết quả

Startup hardening đạt trong dry-run `startup_preflight_t1_1_20261018_v2`.
Đây không phải producer run: không tải model, không khởi tạo Habitat
Simulator, không capture frame, không chạy perception hoặc mapper.

Route/config được khóa là `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`.
SHA-256 effective config:

```text
4e02f12ed0894047721b720d49f8d1c2f8efd2535acb0b2a2334410c239e2c65
```

Run artifact:

```text
artifacts/live_processing_runs/startup_preflight_t1_1_20261018_v2
```

Run manifest ghi `startup_preflight_passed`, `capture_boundary_reached=true`,
`camera_started=false`, `capture_frames=0` và `model_loads=0`. Route runner trả
`capture_boundary_ready`. Không có thư mục `capture/`. Cả năm fake worker nhận
ready IPC với cùng `run_id`, bootstrap token, route config hash và clock domain;
cả năm đóng graceful với exit code 0. Viewer HTTP và renderer/index/head được
server kiểm tra bằng response 200. Server được tạo bởi dry-run đã dừng; port
8783 không còn listener.

## Đường thực thi được kiểm tra

Dry-run gọi cùng launcher và thứ tự bootstrap dùng cho pilot:

1. Kiểm tra effective config, route arguments, asset hashes và output path.
2. Tạo run directory mới, `control/`, `ready/`, logs và packet asset root.
3. Tạo viewer D2, kiểm tra entrypoint bundle và khởi động localhost HTTP.
4. Khởi chạy năm fake workers dưới dạng subprocess; dùng cùng readiness file
   contract và close qua stdin JSON IPC.
5. Gọi đúng `run_habitat_smooth_route_preview.py` bằng interpreter Habitat,
   với cùng config/output/timeout/ownership token và `--startup-dry-run`.
6. Route runner gọi lại shared validator, xác thực ownership/inventory rồi
   dừng ngay tại boundary trước simulator/capture.

`validate_route_startup()` là logic chung của coordinator, route runner
dry-run và route runner runtime. Nó kiểm tra output policy, schema config,
route points/indices, resolution 640×480, HFOV, calibration profile và SHA-256
của scene, navmesh, source route, policy, profile cùng dataset config nếu có.

Trước khi route runner được phép bắt đầu capture, validator kiểm tra:

- run ID là tên của output directory; bootstrap token và route config hash
  khớp giữa bootstrap, run manifest và năm ready markers;
- source C2 manifest còn tồn tại và hash chưa đổi;
- root inventory là đúng bootstrap layout; `capture/` có mặt sẽ bị từ chối;
- `ready/` chỉ có đúng năm marker P2/P3/OpenCLIP/ConceptGraphs/OccuSG, từng
  marker có schema, PID, run ID, bootstrap token, config hash và clock domain;
- `control/` phải rỗng trước capture;
- branch, packet và perception bootstrap directories không có file lạ;
- D2 live index đang rỗng, live head trỏ đúng hash, entrypoint pointer trỏ
  đúng manifest/HTML và các hashes khớp.

Như vậy, `ready/` chỉ được chấp nhận khi chứng minh thuộc run hiện hành;
allowlist không còn là danh sách tên thư mục đơn thuần. Tests xác nhận marker
của run khác, config bị đổi, capture cũ và nội dung thừa trong `ready/` hoặc
`control/` đều bị từ chối. Output path đã tồn tại bị từ chối trước khi tạo run.

## Một startup attempt bị sandbox chặn

`startup_preflight_t1_1_20261018_v1` được giữ lại và đánh dấu
`startup_preflight_failed`. Lần đó sandbox từ chối tạo localhost socket bằng
`PermissionError`, trước HTTP server, fake worker, route runner, model load,
simulator hoặc capture. Đây không phải pipeline/route failure. Dry-run v2 được
chạy lại sau khi được cấp quyền localhost và đạt. Không sửa hay ghi vào run v7.

## Verification

Targeted suite:

```bash
python3 -m pytest -q \
  tests/test_camera_live_startup.py \
  tests/test_camera_live_contract_ipc.py \
  tests/test_smooth_route.py \
  tests/test_camera_live_coordinator.py \
  tests/test_t2d_coordinator.py
```

Kết quả: **40 passed**. `py_compile` đạt ở Python 3.12 orchestration env và
Python 3.9 Habitat env cho shared startup module/route runner.

Fixture gates gồm: current-run bootstrap được chấp nhận; output path có sẵn,
scene hash mismatch, foreign-run ready marker, file thừa trong ready/control,
capture directory cũ và config hash thay đổi đều bị từ chối.

Lệnh dry-run đã chạy:

```bash
python3 scripts/run_camera_live_pilot.py \
  --startup-dry-run \
  --config artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2/effective_config.json \
  --output artifacts/live_processing_runs/startup_preflight_t1_1_20261018_v2 \
  --source artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2 \
  --port 8783 --timeout-s 240
```

Không mở browser để kiểm tra giao diện bằng mắt trong lượt này. HTTP asset
validation đạt, nhưng GUI rendering vẫn chưa được xác nhận bằng browser.

## Provenance code sau hardening

Các SHA-256 dưới đây là source hiện hành sau sửa, tách biệt với source của
run v7:

| File | SHA-256 |
| --- | --- |
| `scripts/run_camera_live_pilot.py` | `f6f1df2f8ff3c5f265fec171a4d89d32ba42184dfaaf5f168dc9bc90094fa90f` |
| `scripts/run_habitat_smooth_route_preview.py` | `df29a02b4471aa9f5044319be6365cbdce8537f3f4a65ba8bc66f5b60c5a22f8` |
| `scripts/run_t2_d_cached_packet_worker.py` | `c2ce8d7aec6361615f1ad394540961feb2ed35ebd678a87cffa37cd6d5392b1f` |
| `integration_v2/camera_live_startup.py` | `7830fe624d765956c2178940ab0c7d23257b728531423c8c725e330b2cc04f03` |

Sau startup dry-run v2 có thêm một thay đổi nhỏ trong coordinator: nếu viewer
bootstrap lỗi trước workers, manifest/bootstrap giờ được ghi rõ là
`startup_preflight_failed`. Thay đổi này chỉ xử lý nhánh lỗi sớm; không đổi
đường thành công mà v2 vừa kiểm tra. Pycompile và 40 targeted tests được chạy
sau thay đổi này.

## Gate còn pending và bước tiếp theo

Startup config/output/ownership, viewer HTTP bootstrap, fake worker readiness
IPC và route-runner capture boundary: **đạt trong dry-run**.

Chưa kiểm tra: Habitat simulator initialization/navmesh route preflight trong
pilot mới, model readiness thật, camera capture, inference, mapper updates,
unified revision, overlap hoặc GUI render bằng browser. v7 vẫn là lỗi trước
capture; không được tính thành pilot pass.

Một pilot thật tiếp theo cần trần tối đa **120 phút** cho một lần chạy từ state
rỗng trên route/config T1.1 đã khóa: một Habitat run, một session cho từng
perception worker và một session cho mỗi mapper; không retry. Đây là ước lượng
ngân sách cần duyệt riêng, không phải lệnh chạy được ủy quyền trong lượt
hardening này.
