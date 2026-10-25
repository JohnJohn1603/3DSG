# Camera-live CL1 handoff — pilot T1.1

## Phạm vi

Pilot duy nhất đã khởi chạy trên route đúng:

`smooth_t1_1_hm3d00802_turn_continue_20261018_v2`

Giữ capture native `640×480`, HFOV 90°, model/config/thresholds và scope
policy hiện có. Đây là camera-live file-backed pilot, không phải realtime và
không phải room-semantic validation.

## Kết quả đã xác minh

- Habitat producer đã chạy đủ **113 camera frames** và **13 keyframe events**
  từ route T1.1; simulation duration 11.648 s, không có collision event.
- Keyframe publication xảy ra sau khi RGB-D/metadata được ghi; journal có
  `camera-live-keyframe-event/v1` và hash của các file nguồn.
- P2 worker được khởi tạo một lần và xử lý keyframe đầu; kết quả có 5
  detections và `model_load_count=1`.
- P3 worker được khởi tạo một lần và xử lý cùng keyframe đầu; 5 detection
  results được tạo.
- Viewer D2 đã bootstrap trước producer ở URL run-local, với trạng thái
  `waiting_for_first_revision`.

## Gate chưa đạt

OpenCLIP worker dừng ở frame đầu do lỗi wiring cụ thể: packet gửi vào
`OpenCLIPFeatureSession.update_frame` thiếu trường bắt buộc `timestamp_s`.
Đây là lỗi adapter, chưa có bằng chứng model/checkpoint hoặc route lỗi.
Vì lỗi xảy ra trước packet join nên:

- ConceptGraphs/OccuSG **chưa nhận frame nào**;
- unified revision count = 0;
- chưa có D2 live graph revision;
- không được gọi là camera-live acceptance đạt.

Pilot v6 được đánh dấu failed/partial và không retry trong lượt này. Viewer
server do v6 tạo đã được dừng; các server của run khác không bị dừng.

## Sửa đã chuẩn bị sau điểm dừng

`run_camera_live_pilot.py` nay truyền `timestamp_s` khi gọi feature session.
Sửa này mới được kiểm tra bằng compile và model-free regression; chưa chạy lại
producer/model pilot.

## Artifact

- Partial run: `artifacts/live_processing_runs/camera_live_t1_1_20261018_v6`
- Camera journal: `camera_keyframes.jsonl` (13 events)
- Camera manifest giữ trạng thái failed với lỗi gốc; các keyframe/RGB-D và
  P2/P3 frame đầu được bảo toàn.

## Verification sau điểm dừng

`33 passed` với bộ regression hiện có:

```text
tests/test_camera_live_coordinator.py
tests/test_t2d_coordinator.py
tests/test_smooth_route.py
tests/test_unified_incremental_timeline.py
tests/test_unified_scene_graph.py
```

Bước tiếp theo nhỏ nhất là xin một lượt pilot riêng nếu muốn xác nhận packet
join và hai mapper; không dùng partial v6 để tuyên bố graph live hoặc realtime.

## Cập nhật sau handoff v6

Model-free contract hardening sau đó đã hoàn tất, nhưng không chạy pilot mới.
Xem [`CAMERA_LIVE_CL1_CONTRACT_HARDENING.vi.md`](CAMERA_LIVE_CL1_CONTRACT_HARDENING.vi.md)
để biết timestamp/IPC regression, provenance source và ngân sách pilot đề xuất.

Kết quả duy nhất của pilot sau hardening được ghi tại
[`CAMERA_LIVE_CL1_PILOT_V7_REPORT.vi.md`](CAMERA_LIVE_CL1_PILOT_V7_REPORT.vi.md).
v7 dừng trước Habitat capture do output-root guard không cho phép `ready/`;
đã thêm regression test và sửa allowlist, nhưng không retry pilot.

## Cập nhật startup preflight 2026-10-18

Startup path sau sửa đã qua fake-worker dry-run từ launcher tới route-runner
capture boundary. Xem
[`CAMERA_LIVE_STARTUP_PREFLIGHT_20261018.vi.md`](CAMERA_LIVE_STARTUP_PREFLIGHT_20261018.vi.md)
cho sequence, tests, hashes và giới hạn. Đây chỉ là bootstrap verification;
không thay đổi kết quả v7 và không phải real pilot. Real pilot chưa được chạy.
