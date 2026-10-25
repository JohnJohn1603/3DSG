# T2-D hardened cached integration — v4

## Kết quả

Run mới từ state rỗng đã **completed** sau khi preflight và viewer bootstrap
đạt. Đây là cached-input integration trên đúng ba packet T2-B2; chưa phải
live capture hay realtime.

- run: `t2d_cached_b2_20261018_v4`
- scene: `00802-wcojb4TFT35`
- sequence: `hm3d-v02-00802-main-route-01`
- ConceptGraphs sessions: `1`
- OccuSG sessions: `1`
- frame updates mỗi nhánh: `3`
- unified revisions: `3`
- terminal ConceptGraphs finalize: `1`
- terminal OccuSG flush: `1`
- mapper retries: `0`
- status: `completed`

Manifest: `artifacts/live_processing_runs/t2d_cached_b2_20261018_v4/run_manifest.json`.

## Preflight và tests

Preflight gọi chính packet adapter trong từng interpreter, không gọi mapper:

- ConceptGraphs: `.venv-cg-cpu/bin/python` — 3/3 frame pass, P2/features IDs,
  masks, RGB-D, pose, intrinsics và source hashes hợp lệ.
- OccuSG: `.venv-perception/bin/python` — 3/3 frame pass, RGB-D metadata,
  world-frame detections, IDs và source hashes hợp lệ.

Model-free test command:

```bash
python3 -m pytest -q tests/test_t2d_coordinator.py \
  tests/test_perception_feature_sessions.py \
  tests/test_conceptgraphs_session.py tests/test_occusg_session.py
```

Kết quả: **23 passed**. Fake-worker tests kiểm tra branch chậm/lỗi,
frame/order/hash barrier, update marker khi mutation chưa chắc chắn và
cleanup idempotent.

## Runtime và lifecycle

| Hạng mục | Kết quả |
|---|---:|
| adapter preflight | 1.244 s |
| viewer bootstrap | 0.402 s |
| frame 0 join wait | 5.935 s |
| frame 1 join wait | 5.047 s |
| frame 2 join wait | 7.319 s |
| terminal stage | 1.202 s |
| coordinator peak RSS | 137.18 MiB |
| tổng thời gian | 68.63 s |

Object/room processing times theo frame (ms):

```text
frame 0: objects 42.9,   rooms 5392.9
frame 1: objects 204.1,  rooms 4939.0
frame 2: objects 336.0,  rooms 7186.0
```

Anomaly `48.821 s` được giữ nguyên trong manifest như một observation cũ,
không dùng để suy nguyên nhân và không chạy benchmark bổ sung.

Worker logs ghi `update_started` trước mapper call. Nếu worker chết sau marker
nhưng trước publish, coordinator đặt `mutation_possible/replay_required`;
run v4 không rơi vào trường hợp đó.

## Viewer evidence

Viewer được mở trước producer, xác nhận trạng thái
`waiting_for_first_revision`, sau đó producer được release bằng
`control/start.json`.

URL:

```text
http://127.0.0.1:8774/artifacts/live_processing_runs/t2d_cached_b2_20261018_v4/viewer/index_d2_t2d.html?live=1
```

Browser đã xác nhận:

- `completed · 3 revisions`;
- frame cuối `hm3d-00802-main-0002`, graph revision 3;
- XYZ point cloud, camera trajectory và XY room geometry cùng revision;
- seek lùi với live-follow OFF về frame 2/3, hiển thị đúng 2 camera poses;
- `membership: unavailable_elevation_scope` và room semantics unavailable;
- không có ConceptGraphs `room_contains` giả; target counts vẫn tách
  navigation/agent/OccuSG object/ConceptGraphs object.

Trong lúc kiểm tra, phát hiện lỗi viewer serialization làm hai frame đầu của
`live_index` bị ghi nhầm event object. Đã sửa coordinator và phục hồi
`live_index` từ các immutable joined revisions, không chạy lại mapper.
Historical seek sau sửa đã pass.

Terminal artifacts tồn tại riêng ở
`branches/objects/terminal.json` và `branches/rooms/terminal.json`. Viewer
không giả lập terminal state thành prefix; nút final postprocess vẫn không
được bật khi chưa có viewer-compatible terminal snapshot.

## Acceptance

- Packet integration: **pass**.
- Same frame/order/source hashes ở hai nhánh: **pass**.
- Atomic unified commits: **3/3 pass**.
- Persistent worker lifecycle: **pass**.
- Terminal separation: **pass**.
- GUI bootstrap trước producer: **pass**.
- GUI live revision + historical seek: **pass sau viewer-only repair**.
- Room membership/room labels: **unavailable**, đúng scope gate.
- Camera-live pilot: **chưa chạy**.

Bước tiếp theo nhỏ nhất là proposal camera-live pilot với cùng coordinator,
không tự chạy trong lượt này.
