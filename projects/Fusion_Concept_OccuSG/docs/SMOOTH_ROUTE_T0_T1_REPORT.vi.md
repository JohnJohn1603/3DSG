# Báo cáo T0/T1 — camera-only smooth route

Ngày: 2026-10-18  
Phạm vi: đối chiếu Habitat API và chạy route preview camera-only trên HM3D 00802. Không chạy detector, encoder, ConceptGraphs, OccuSG, Qwen hoặc room scoring.

## Kết quả

T0 hoàn tất. T1 camera-only đã chạy hết một route development ngắn, với pose thật từ Habitat, keyframe policy/log và raw RGB-D keyframes. Đây chưa phải graph update, live processing hoặc realtime.

### API và route safety

- Môi trường Habitat hiện có: Habitat-Sim 0.3.3, Python 3.9.23; không cài dependency.
- `Simulator.step(action, dt=...)` trả observation dict có `collided: bool`; quan sát thực tế cho thấy khi va chạm, `collided=True` và agent không đổi pose. `Agent.act` cũng ghi rõ trả trạng thái va chạm.
- Navmesh 00802 lưu radius 0.1 m, height 1.5 m, `include_static_objects=false`. Vì vậy navmesh có thể bỏ qua một số va chạm với hình học scene; vẫn cần kiểm tra collision từ Agent khi chạy thật. Câu cũ coi `distance_to_closest_obstacle` bằng 0/epsilon là unavailable đã được đính chính ở T1.1: giá trị hữu hạn bằng 0/epsilon là clearance hình học thật và phải qua footprint gate; chỉ giá trị non-finite mới dùng fallback serialized footprint.
- Các đoạn `main` thử trước đó bị chặn đúng theo policy: `8→9` va chạm ở bước tiến đầu; `17→18` di chuyển một đoạn rồi va chạm; `0→4` bị preflight từ chối vì clearance đầu route 0.029 m < footprint 0.1 m. Các run lỗi và partial artifacts được giữ nguyên.

### Route đã khóa và chạy thành công

Route riêng `t1_navmesh_clearance` được tạo từ PathFinder/navmesh, không dùng predictor, object/room labels, semantic GT hay annotations. Start là navmesh sample seed 13 gần waypoint main-route 4; endpoint đi 0.4 m theo một hướng có clearance phù hợp. File route mới tách biệt, route main lịch sử không đổi.

- Scene: `00802-wcojb4TFT35`, dataset `hm3d_minival_v0.2`.
- Capture: native 640×480, HFOV 90°, metric axial depth; K thực tế `fx=320.00000000000006`, `fy=320`, `cx=319.5`, `cy=239.5`.
- Footprint: radius 0.1 m, height 1.5 m, khớp navmesh. Path được kiểm tra ở bước 0.04 m; clearance nhỏ nhất được báo 0.15 m; các no-slide checks đạt.
- Chuyển động: 0.2 m/s, 30°/s, bước simulator 0.1 s. 19 bước tiến và quay tăng dần; không teleport/snap. Kết thúc cách waypoint 0.02 m, trong tolerance 0.025 m.
- Run: `smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7`.
- 30 camera frames; 2 keyframe candidates, 2 raw RGB-D keyframes admitted, 0 superseded. Candidate thứ hai được chọn bởi `yaw_threshold` sau 21°; queue policy latest-wins được kiểm tra riêng bằng test, nhưng run này không tạo supersede.
- Mỗi frame RGB/JPEG dùng pose camera/agent thật. Keyframe RGB, depth, valid mask và metadata có cùng frame ID/timestamp; `predictor_input=false`, `mapper_processed=false`.

## Timing và tài nguyên

| Stage | Thời gian |
|---|---:|
| Habitat load + route/navmesh preflight | 19.447 s |
| Camera action/capture | 2.183 s |
| Drain keyframe writer | 0.0002 s |
| Tổng wall time | 21.774 s |
| Simulation time | 3.016 s |
| Peak RSS của worker | 1,553,088,512 bytes |

Đây là một route ngắn trên llvmpipe, chưa phải throughput benchmark. Không gọi đây là realtime. Run chứa 42 immutable files được liệt kê trong hash manifest; toàn bộ hash được kiểm tra lại. Các keyframes có RGB `(480,640,3) uint8`, depth/mask `(480,640)`.

## GUI

- Viewer offline HTML có play/pause/previous/next và slider. HTTP server read-only được mở trên localhost; manifest và entrypoint phục vụ thành công HTTP 200, HTML có 30 frame references. Ba RGB frame đầu/giữa/cuối được xem trực tiếp và đều là ảnh Habitat hợp lệ.
- GUI browser acceptance chưa đạt: CUA session không cung cấp browser/app (`browsers=[]`, `apps=[]`, `iab` unavailable). Không gọi `xdg-open` để né giới hạn. Server do run này khởi động vẫn đang nghe ở `127.0.0.1:8770`; người dùng có thể mở URL bên dưới trong Brave.

## Commands

```bash
./run_scene.sh camera-preview \
  --dataset hm3d_minival_v0.2 \
  --scene 00802-wcojb4TFT35 \
  --route t1_navmesh_clearance \
  --from-index 0 --to-index 1 \
  --run-id smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7 \
  --timeout-s 300
```

Kết quả: `artifacts/smooth_route_previews/smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7/`.

```text
http://127.0.0.1:8770/artifacts/smooth_route_previews/smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7/viewer/index.html
```

`./run_scene.sh results smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7` đã chạy và đọc manifest thành công. Lệnh open hiện có là `./run_scene.sh open smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7 camera-preview --port 8770`; thao tác browser không được chạy do browser automation không có sẵn.

## Verification và trạng thái

- `PYTHONPATH=. /usr/bin/pytest -q tests/test_smooth_route.py tests/test_scene_registry.py tests/test_scene_launcher.py`: **48 passed**.
- `py_compile` đạt cho worker/Habitat adapters bằng Python 3.9 và launcher/registry bằng Python 3.12.
- Registry resolve route mới bằng shared resolver; capture profile resolve native 640×480.
- Có 7 worker invocations camera-only trong tổng lịch sử T1; 1 hoàn tất, 6 fail/stop ở preflight, API serialization hoặc collision. Không có inference/mapping nào trong các lượt này.
- Các A tables, model, matcher, thresholds, baseline, room geometry và scope policy không đổi. Membership/room semantics giữ `unavailable`.

## T2/T3 nhỏ nhất, chưa triển khai

1. **T2 — mapper sessions:** nhận các packet admitted từ cùng keyframe writer, validate frame ID/order/hash rồi gọi session `open → update_frame → finalize → close` cho các worker hiện có; mỗi worker load một lần và giữ state qua frame. Không biến toàn bộ camera frames thành mapper inputs. Trước khi nối, xác nhận batch wrapper và session wrapper gọi cùng mapping core, RNG/order và final postprocess parity.
2. **T3 — causal evidence:** sau mỗi object revision, chỉ tính prefix admission, semantic consensus/view groups và membership đã có scope verified; update RB1 theo frame transaction, category-presence cap và per-object retract ledger; giữ cả bốn A variants tường minh. Scope hiện insufficient nên real membership/room labels vẫn unavailable; `verified_only` tiếp tục mặc định. Không dùng assumed scope trong bước này.

T1 checkpoint camera-only đạt cho route 0.4 m nói trên; browser GUI acceptance còn pending. T2/T3, live simulation, graph updates và room semantics vẫn pending.
