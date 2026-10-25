# Báo cáo T1.1 — camera-only route có quay và tiếp tục

Ngày: 2026-10-18  
Phạm vi: kiểm chứng hữu hạn trước T2. Không chạy detector, encoder,
ConceptGraphs mapping, OccuSG, Qwen hoặc room scoring.

## Call path collision đã đối chiếu

Môi trường dùng Habitat-Sim 0.3.3, Python 3.9.23. Trong
`habitat_sim/simulator.py`, `Simulator.step_filter` gắn vào
`AgentControls.move_filter_fn`; khi `allow_sliding=false`, nó gọi
`PathFinder.try_step_no_sliding(start_pos, end_pos)`. Worker cũng gọi
`try_step_no_sliding` trước mỗi bước tiến và dừng nếu kết quả bị clipped.

Worker đặt `enable_physics=false`. Navmesh được nạp từ binary đã khóa và
ghi `include_static_objects=false`. Vì vậy kết quả này chứng minh
navmesh footprint, no-slide movement-filter và agent action path đã vượt
qua route; `collided=false` không phải một phép đo độc lập về mọi static
object instance hay Bullet physics. Không bật physics và không rebuild
navmesh trong lượt này.

## Sửa contract route/clearance

- `find_waypoint_polyline` gọi `PathFinder.find_path` cho từng cặp
  waypoint liên tiếp rồi nối các polyline, thay vì chỉ hỏi path từ điểm
  đầu đến điểm cuối.
- Mỗi segment kiểm tra endpoint error và được preflight ở bước 0.04 m.
- Clearance hữu hạn bằng `0.0` hoặc epsilon được xem là clearance hình học
  thật và fail footprint gate nếu nhỏ hơn yêu cầu. Chỉ giá trị non-finite
  mới dùng serialized navmesh footprint fallback.
- Lần đầu với endpoint `z=-4.2` bị giữ nguyên ở run failed vì
  `try_step_no_sliding` clipped 0.059454 m. Route không được snap hoặc
  teleport để vượt lỗi đó. Route T1.1 cuối dùng endpoint đã kiểm tra
  `z=-4.7`, với clip 0.0243 m trong tolerance 0.025 m.

## Route và kết quả

Route được thêm riêng ở
`configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_route.json`:

1. `[1.05, 0.124, -4.586]` — đi về waypoint giữa;
2. `[0.65, 0.124, -4.586]` — quay tại waypoint giữa;
3. `[0.65, 0.124, -4.7]` — tiếp tục đi sau khi quay.

Run thành công:
`smooth_t1_1_hm3d00802_turn_continue_20261018_v2`.

- Native capture 640×480, HFOV 90°, simulation step 0.1 s.
- Hai segment được resolve độc lập; tổng geodesic distance 0.514 m.
- 14 path samples; `distance_to_closest_obstacle` hữu hạn ở toàn bộ
  samples; minimum numeric clearance 0.15000000596 m.
- 113 camera frames, 112 actions, simulation time 11.648 s; frame
  interval thực tế 0.104 s.
- 24 bước `move_forward`, 59 `turn_left`, 29 `turn_right`; không có
  `collided=true`.
- 13 candidate keyframes, 13 admitted, 0 superseded. Candidate đầu do
  `first_valid_frame`; các candidate còn lại do `yaw_threshold` (21°).
  Frames bị bỏ do `below_min_interval` hoặc
  `pose_delta_below_thresholds` theo policy đã khóa.
- Peak RSS worker: 1,567,645,696 bytes; wall time 23.738 s; camera rate
  mô phỏng 10 Hz. Đây không phải realtime benchmark.

Manifest và event journal:
`artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2/`.

## Kiểm chứng

- `PYTHONPATH=. /usr/bin/pytest -q tests/test_smooth_route.py tests/test_scene_registry.py tests/test_scene_launcher.py`
  — **49 passed**.
- `py_compile` worker, integration helper và tests — đạt.
- Manifest/hash validation và `run_scene.sh results` — đạt.
- GUI browser chưa được kiểm tra tự động trong session này; người dùng
  có thể mở viewer bằng lệnh bên dưới. Đây là camera-only preview, chưa
  có graph update.

```bash
./run_scene.sh open smooth_t1_1_hm3d00802_turn_continue_20261018_v2 camera-preview --port 8771
```

URL:
`http://127.0.0.1:8771/artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2/viewer/index.html`

## Điểm dừng

T1.1 camera-only route có movement → turn → continue đã đạt trong phạm vi
navmesh/no-slide được xác minh. Static-object collision coverage,
physics, object graph, membership và room semantics vẫn unavailable.
T2 persistent sessions là bước kế tiếp; T3 giữ `verified_only` mặc định,
`assumed_single_level_demo` chỉ là lựa chọn experimental khai báo riêng.
