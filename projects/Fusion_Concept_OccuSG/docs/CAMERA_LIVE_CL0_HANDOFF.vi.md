# Camera-live pilot — CL0 handoff

Ngày: 2026-10-18

## Phạm vi đã thực hiện

Đã triển khai lớp điều phối current-frame dùng chung cho camera-live:

- `integration_v2/camera_live.py`
- `tests/test_camera_live_coordinator.py`
- `scripts/run_t2_d_cached_packet_worker.py --live` nhận packet từng lần
  và khởi tạo ConceptGraphs state không đọc packet bundle tương lai.

Lớp này chỉ điều phối. Perception và mapper vẫn được truyền vào dưới dạng
session hiện có; không sao chép ConceptGraphs, OccuSG, P3 hoặc encoder.

Các điểm đã khóa:

- Một `CurrentFramePacket` tại một thời điểm, có frame ID, index, camera
  timestamp và source hashes.
- Từ chối packet chứa `future_manifest_required` hoặc `future_packets`.
- Một queue đang xử lý và tối đa một packet pending.
- Candidate, admitted, superseded, update_started và processed được ghi
  thành event.
- Packet đã dispatch không bị thay thế giữa hai mapper.
- Unified publish chỉ xảy ra sau khi cả ConceptGraphs và OccuSG
  `update_frame` thành công.
- Dispatch pair có thể gửi hai worker trước khi chờ ACK; đường gọi trực tiếp
  cũ vẫn giữ để regression, còn worker adapter dùng pair-dispatch hook.
- Lỗi sau khi bắt đầu mapper update được phân loại
  `replay_required`; lỗi trước mutation không bị gọi nhầm là mapper
  mutation.
- Finalize/close được tách khỏi frame publish; cleanup gọi được nhiều lần.
- Coordinator không đọc browser state và không chờ browser ACK.
- Graph lag chỉ tính từ capture wall-clock hoặc monotonic cùng clock domain;
  `timestamp_s` mô phỏng không được trừ vào wall-clock.

## Kiểm chứng đã chạy

```text
python3 -m pytest -q \
  tests/test_camera_live_coordinator.py \
  tests/test_t2d_coordinator.py \
  tests/test_smooth_route.py \
  tests/test_unified_incremental_timeline.py
33 passed in 3.77s
```

Các fixture bao gồm pair barrier, duplicate/stale, future-input rejection,
latest-wins pending replacement, slow/failing branch, failure sau mutation,
perception failure trước mutation và terminal lifecycle.

## Đối chiếu route T1.1 đúng

Input đã đọc:

`artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2`

- 640×480, HFOV 90°.
- 113 camera frames trong 11,648 giây simulation time.
- Đường navmesh 0,514 m, 2 waypoint segments, không có collision event.
- 13 candidate/admitted keyframes.
- `mapper_enabled=false`; đây là camera-only artifact, không phải live
  mapper run.

Manifest ghi `camera_capture.elapsed_s=8,108047` và toàn run
`wall_clock_runtime_s=23,738265`. Simulation time không được dùng thay
wall-clock time. Route này đủ dài để dùng làm CL1 pilot; vẫn cần đo thực tế
perception/mapper overlap thay vì suy ra từ camera-only run.

## Khoảng trống còn lại trước CL1

`integration_v2/camera_live.py` và worker `--live` chưa được nối trực tiếp
vào Habitat route runner và perception process. Đường hiện hành vẫn có
các giới hạn sau:

1. Đường cached mặc định của `scripts/run_t2_d_cached_packet_worker.py`
   vẫn khởi tạo từ packet bundle đã materialize; chỉ nhánh `--live` mới
   nhận packet từng lần.
2. `integration_v2/t2d_coordinator.py` vẫn gọi `_write_packet_bundle`
   trước khi worker chạy.
3. `scripts/run_habitat_smooth_route_preview.py` ghi keyframe file nhưng
   chưa phát `CurrentFramePacket` tới perception session trong lúc
   simulator đang chạy.
4. `P2DetectorSession`, `P3GeometrySession` và
   `OpenCLIPFeatureSession` đã có API current-frame, nhưng cần một adapter
   nối RGB-D/pose của Habitat vào packet, sau đó persist mask/features để
   ConceptGraphs worker đọc đúng frame hiện tại.

Đã bổ sung model-free pair-dispatch hook và test file-backed IPC với hai
fake workers. Real Habitat → perception → mapper wiring vẫn chưa chạy.

Do đó chưa có claim camera-live, realtime hoặc room-semantic quality.
Membership và room semantics tiếp tục `unavailable` theo scope policy.

## Phạm vi CL1 được đề xuất lại

Một real run duy nhất, không retry:

- route: `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`;
- không tạo route mới;
- tối đa 113 camera frames và 13 admitted keyframes theo artifact đã khóa;
- warm-up/model load trước khi route bắt đầu;
- không preload pose, feature hoặc packet tương lai;
- một ConceptGraphs session và một OccuSG session;
- revision đầu tiên phải được publish trước khi camera route kết thúc.

Nếu gate này không đạt, giữ partial artifacts và dừng; không chạy lại mapper
chỉ vì viewer hoặc timing không đạt.

## Trạng thái claim

- CL0 current-frame coordinator: **đã triển khai và test**.
- Camera-live Habitat producer: **chưa nối**.
- Cached T2-D: **không chạy lại trong lượt này**.
- Camera-rate realtime: **chưa đo, chưa claim**.
- Membership/room labels: **unavailable**, không bật assumed scope.
