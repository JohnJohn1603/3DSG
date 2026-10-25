# M12 — Webots adapter: kiểm chuẩn sensor trước tích hợp builder

## Trạng thái

**M12 đã đạt các tiêu chí capture, offline replay, thay world bằng config và live-to-replay parity trong phạm vi local đã chạy. M12 chưa nghiệm thu đầy đủ cho simulator/benchmark thực tế.** Không có upload cloud.

## Môi trường và tài liệu đã pin để đối chiếu

- Runtime cục bộ: Webots **R2025a**, Snap revision 27; `webots --version` trả `Webots version: R2025a`.
- API/reference đọc theo tag **R2025a**, không dùng tài liệu `master`: [WorldInfo](https://github.com/cyberbotics/webots/blob/R2025a/docs/reference/worldinfo.md), [Camera](https://github.com/cyberbotics/webots/blob/R2025a/docs/reference/camera.md), [RangeFinder](https://github.com/cyberbotics/webots/blob/R2025a/docs/reference/rangefinder.md), [Supervisor](https://github.com/cyberbotics/webots/blob/R2025a/docs/reference/supervisor.md).
- Quy ước axes thiết bị: tài liệu chuyển đổi của Webots ghi từ R2022a các geometry/device/PROTO dùng FLU (`+X` forward, `+Y` left, `+Z` up); quy ước đó áp dụng cho runtime R2025a này. [Webots axis migration note](https://github.com/cyberbotics/webots/wiki/How-to-adapt-your-world-or-PROTO-to-Webots-R2022a).
- WorldInfo `ENU` nghĩa là X east, Y north, Z up. Camera và RangeFinder cùng resolution/FOV; Camera là planar, buffer màu BGRA; RangeFinder trả float32 theo mét, là axial distance tới local `z=0` plane và trả `Inf` ngoài min/max. Supervisor `getPose()` là ma trận 4×4 absolute, row-major; pose pointer chỉ dùng được trong timestep hiện tại.
- Controller chạy theo Python runtime Webots cô lập và không có NumPy. Vì vậy controller chỉ capture raw sensor buffers bằng API Webots/stdlib; postprocessor chạy offline bằng Python dự án và gọi adapter + M1 `backproject_depth` hiện có. Không cài package hoặc thay đổi system-wide.

## Frame chain đã xác minh

Adapter không tự tạo identity world transform. Nó bắt buộc caller truyền `T_world_webots` có đúng labels `world ← webots_world`; frame sai bị reject. Với fixture do dự án sở hữu, `WorldInfo` được đọc **lúc runtime** và phải là ENU; fixture tự định nghĩa canonical origin trùng gốc Webots, không import map hoặc thêm scene offset. Đơn vị mét được đối chiếu với RangeFinder docs và hit hình học đã biết.

```text
T_world_camera_optical
  = T_world_webots
  @ T_webots_sensor_actual
  @ T_sensor_camera_optical
```

- `T_world_webots = I` chỉ trong fixture này: ENU đã kiểm tra, mét, cùng gốc được fixture khai báo; world khác phải cung cấp transform riêng.
- `T_webots_sensor_actual` lấy từ `Supervisor.getPose()` trên `CAMERA_NODE`, không lấy pose base. Webots R2025a device frame FLU trùng sensor-body contract, nên adapter không chèn legacy OpenGL-axis rotation.
- `T_sensor_camera_optical` được áp dụng qua helper `webots_to_canonical` hiện có; frame validation core không bị nới hoặc tắt.
- Robot base yaw `+π/2`; camera mount yaw tương đối `−π/2`, camera offset tương đối `(0, −0.25, 1)` m. Kết quả sensor world pose ở `(0.25, 0, 1)` m, hướng world `+X`; sensor khác base cả translation lẫn rotation. Camera và RangeFinder world poses khớp chính xác trong capture.

## Fixture và lệnh tái lập

Fixture đơn giản gồm sàn, một tường có mặt trước tại `x=3.0 m`, và một hộp ở vị trí đã biết. Camera/RangeFinder là `65×49`, FOV ngang `π/3`, không noise/motion blur; center pixel là `(u,v)=(32,24)`. World SHA-256: `8336ed6660ab6726a358f2e35d596dc408564a45b55cbb14be16955231adb612`.

```bash
/snap/bin/webots --port=1255 --mode=fast --batch --stdout --stderr \
  webots/worlds/m12_sensor_calibration.wbt
PYTHONPATH=.:src python3 scripts/process_m12_sensor_capture.py \
  --output artifacts/m12_sensor_calibration
PYTHONPATH=.:src pytest -q tests/test_webots_adapter.py
PYTHONPATH=.:src pytest -q
```

Lần đầu fixture không thấy hit tại center. Pose và ảnh depth chỉ ra adapter đang dùng nhầm axes legacy của Camera node; theo Webots device FLU convention, camera đã nhìn lệch khỏi tường. Adapter được sửa thành dùng trực tiếp actual FLU sensor pose và giữ explicit world transform/frame checks. Fixture được đặt lại bằng base yaw và mount yaw đối nhau để camera optical axis hướng đúng vào tường; không thay core hoặc nới tolerance.

## Kết quả chạy thật

- Webots khởi động world/controller thành công. Capture simulation timestamp `0.032 s`.
- Center RangeFinder depth `2.75 m`; dự kiến mặt trước tường tại world `(3.0, 0, 1.0) m`; M1 backprojection trả `(3.0, −5.6e−17, 1.0) m`, sai số Euclidean `5.6e−17 m` với tolerance đã chốt `0.02 m`.
- Intrinsics từ FOV và resolution: `fx=56.291651`, `fy=56.291651`, `cx=32`, `cy=24` pixel.
- Sensor offset error so với khai báo: `5.6e−17 m`; Frobenius norm chênh rotation camera/base `2.0`; chênh lệch pose Camera/RangeFinder `0`.
- Depth: `3185` valid, `0` invalid trong capture này. Do đó nhánh invalid/Inf được kiểm qua adapter unit tests, không được khẳng định là xuất hiện trong live fixture.
- BGRA→RGB: ba mẫu live (center và hai góc) khớp chính xác các getter `Camera.imageGetRed/Green/Blue` của Webots (`[0,0,0]` channel deltas). Unit test riêng kiểm tra byte order và buffer ownership.
- Controller copy raw Camera/RangeFinder buffer trước timestep kế tiếp; owned snapshots còn nguyên sau bước kế tiếp. RGB/depth raw bytes và canonicalized NPZ được giữ lại.
- Tại checkpoint sensor trước phần motor: `tests/test_webots_adapter.py` **5 passed**; full regression khi đó **204 passed trong 94.63 s**. Đây là mốc lịch sử của sensor fixture, không thay thế kết quả calibration.

Artifacts trong `artifacts/m12_sensor_calibration/`: `camera_bgra.bin`, `range_depth_f32.bin`, `capture_metadata.json`, `capture_processed.npz`, `calibration.json`, `rgb.png`, `depth_preview.png`.

## Tích hợp capture → graph đã chạy

Runner [run_m12_webots_native_replay.py](../scripts/run_m12_webots_native_replay.py) dùng các tham số scene/config cho capture directory, world transform, route và pipeline config; không đưa tọa độ fixture vào core. `m12_sequence_capture` chỉ dùng Webots stdlib APIs trong controller, lưu BGRA/depth float32/metadata/pose thật; Python dự án đảm nhận `RGBDFrame`, `FramePacket` và M1–M10.

- Sensor fixture đơn tường/hộp: 4 frame, marker trái/phải/trên/dưới, chuyển động và quay; kiểm tra ray/AABB độc lập đạt dưới tolerance 0.02 m. Offline native graph có 1 region/1 RoomNode.
- World hai phòng [m12_two_rooms.wbt](../webots/worlds/m12_two_rooms.wbt) với tường hữu hạn dày 0.12 m, doorway rộng 1.5 m, hai đồ vật, route 11 frame đi qua cửa rồi quay lại. [Manifest](../artifacts/m12_webots_two_rooms_graph/manifest.json) ghi native result: **2 regions, 2 RoomNodes**, revision 12, 66 nodes, 358 edges; snapshot reload, delta replay, trajectory và graph invariants pass. Đây là kết quả baseline thực tế, không ép room count.
- World thứ hai [m12_alt_corridor.wbt](../webots/worlds/m12_alt_corridor.wbt) dùng route/config khác nhưng cùng controller/runner/core: 4 frame, **1 region, 1 RoomNode**, revision 5; replay deterministic và export/reload pass. Đây là bằng chứng tái sử dụng nhiều world, không phải chất lượng phân phòng tổng quát.
- Ảnh mốc trước cửa/sau cửa/quay lại nằm trong `artifacts/m12_webots_two_rooms_graph/` cùng `trajectory_*` và `scene_graph_*` PNG. Diagnostics từng frame ghi branch M4, M5 accepted/stale, region/room IDs, pose, revision và runtime.

## Live bridge và replay cùng packets

[run_m12_webots_live_bridge.py](../scripts/run_m12_webots_live_bridge.py) mở localhost TCP; controller gửi raw packet khi Webots đang chạy, host Python vừa cập nhật native builder vừa lưu đúng bytes/metadata. Với world hai phòng: 11 frame, live snapshot revision 12 gồm 66 nodes/358 edges, 2 regions/2 rooms; delta log live tự replay đúng. Fresh replay từ chính `artifacts/m12_webots_two_rooms_live_capture` khớp snapshot live (`live_equals_same_packet_replay: true`). Đây là live bridge tới Python runtime dự án, chưa phải `SceneGraphBuilder` chạy bên trong Webots controller.

## M12 mở rộng — robot vi sai chạy bằng motor

Đây là phần mở rộng **sau checkpoint M12 capture/replay đã chạy được**. Không thay logic M1–M10, không thêm obstacle avoidance, navigation policy, SLAM hoặc detector. Robot Webots mới dùng `M12DiffDriveRGBD.proto`: hai bánh chủ động có `Motor`, bánh caster, `Physics`, `boundingObject`, Camera và RangeFinder đồng vị trí/hướng. Tọa độ khởi tạo nằm trong `.wbt`; controller không đặt translation/rotation trong lúc chạy.

### Controller, capture và replay

- `m12_motor_capture.py` điều khiển vận tốc bánh trái/phải bằng waypoint feedback từ Supervisor; có giới hạn vận tốc/gia tốc, giảm tốc khi quay/gần đích, timeout/stuck detection và luôn dừng motor khi kết thúc/lỗi.
- Physics timestep `32 ms` tách khỏi capture cadence. World hai phòng capture mỗi `40` physics steps (`1.28 s/frame`); corridor capture mỗi `120` steps (`3.84 s/frame`). Số capture frame độc lập với số waypoint.
- Mỗi packet giữ BGRA/depth raw đã copy, depth metric axial, intrinsics, valid mask, pose sensor thực tế, base pose, simulation timestamp và frame ID. `detections=absent`; không đưa GT room/object vào builder.
- Offline runner dùng đúng pipeline M1 → native M2 → native M3 → DuDe/M5 → M6–M9 → M10. Live bridge dùng queue giới hạn, chính sách block rồi fail rõ ràng, không silently drop packet; live được so sánh với replay của chính packets đã xử lý.

### Kết quả chạy thật

- **Hai phòng:** `artifacts/m12_webots_two_rooms_motor_capture/` có `84` frame, `11/11` waypoint; offline graph có `2` regions/`2` rooms, revision `85`, `153` nodes và `625` edges. Sensor extrinsics và collision check pass; không xuyên các AABB tường/obstacle đã khai báo.
- **Corridor:** `artifacts/m12_webots_corridor_motor_capture/` có `24` frame, `10/10` waypoint; offline graph có `2` regions/`2` rooms, revision `25`, `59` nodes và `210` edges. Đây là **kết quả cần đánh giá**, không phải bằng chứng corridor có hai phòng đúng: world này không có partition/doorway. Diagnostics cho thấy frame `0–13` chỉ có region `1`; region `2` xuất hiện lần đầu ở frame `14`, `t=57.6 s`, khi robot đã quay đầu và đang quay lại (`M4=success/accept_incremental`, `M5 accepted`, `stale=false`), sau đó tồn tại tới frame cuối. Live bridge cũng pass và `live_equals_same_packet_replay=true` với queue `maxsize=2`.
- Cả hai run đều pass snapshot reload, delta replay, deterministic fresh replay, không dangling/self edges. Manifest giữ `world_file` và SHA-256; route commanded và actual trajectory được lưu riêng.
- Runtime offline là thời gian update/flush/close hoặc tổng xử lý đã ghi trong manifest, không gồm Webots capture; RSS là sampled/high-water observation, không phải benchmark peak toàn hệ thống. Hai run này là scripted synthetic Webots route với Supervisor ground-truth feedback, chưa phải navigation policy hay realtime benchmark.

### Collision coverage và phân biệt waypoint

- Config collision đã khớp hình học world: corridor có tường `x ∈ [-3.5, 3.5]`, không còn vùng giả `[-0.06, 7.06]`; cả hai fixture đều khai báo tường và obstacle AABB riêng.
- Controller ghi `physics_trace.json` ở **mọi physics timestep**, gồm pose Supervisor, wheel command, waypoint error và clearance. Replay tái tính lại từng trace row từ AABB cấu hình, không nội suy từ RGB-D capture frame.
- Corridor: `2894` physics steps, `2` wall AABB + `2` obstacle AABB, minimum clearance `0.1617 m`; two-room: `3383` steps, `6` wall + `2` obstacle AABB, minimum clearance `0.1588 m`. Cả hai đều `clear=all`, `contact=0`, `penetration=0`.
- `contact` là clearance nằm trong `contact_tolerance_m`; `penetration` là clearance âm. Waypoint completion được báo riêng (`10/10` và `11/11`), không được dùng thay cho kiểm tra collision. Đây là kiểm tra hình học từ pose/AABB bổ sung cho Webots `Physics`/`boundingObject`, chưa phải báo cáo contact sensor hay continuous swept-volume proof.
- Corridor route được bổ sung waypoint `before_obstacle` và `turn_back` tường minh; tolerance waypoint hiện hành là một tham số config (`0.25 m`), không phải thay đổi thuật toán mapping/segmentation.

Corridor có hai region không được ép hoặc đổi kernel. Overlay trajectory và diagnostics chỉ cho phép kết luận region thứ hai xuất hiện sau khi quay lại; chưa đủ để gọi đó là room segmentation đúng. Cần giữ fixture này làm regression chất lượng/coverage và phân tích contour/occupancy nếu muốn giải thích sâu hơn.

### Đổi map, robot và route

1. Sao chép/chỉnh một `.wbt` dùng `webots/protos/M12DiffDriveRGBD.proto`, đặt `translation`/`rotation` chỉ cho pose khởi tạo và `customData` trỏ tới config JSON.
2. Trong config, đổi `world_file`, `output_dir`, `route`, `timing`, `sensor`, `motor` và `collision_check`; không sửa core. `world_file` phải là đường dẫn tương đối trong project và manifest sẽ kiểm tra SHA-256.
3. Chạy Webots capture (trong môi trường hiện tại wrapper Snap bị chặn bởi AppArmor nên dùng binary trực tiếp):

```bash
XDG_RUNTIME_DIR=/tmp/webots-runtime WEBOTS_TMPDIR=/tmp/webots-tmp \
QT_QPA_PLATFORM=xcb QT_QPA_PLATFORM_PLUGIN_PATH=/snap/webots/27/usr/share/webots/lib/webots/qt/plugins \
LD_LIBRARY_PATH=/snap/webots/27/usr/share/webots/lib/webots \
/snap/webots/27/usr/share/webots/bin/webots-bin --mode=fast --batch --stdout --stderr \
  webots/worlds/m12_two_rooms_motor.wbt
```

4. Replay offline bằng config tương ứng:

```bash
MPLCONFIGDIR=/tmp/m12-webots-mpl PYTHONPATH=.:src \
python3 scripts/run_m12_webots_native_replay.py \
  --capture artifacts/m12_webots_two_rooms_motor_capture \
  --config configs/webots/m12_two_room_motor_route.json \
  --output artifacts/m12_webots_two_rooms_motor_graph
```

Để đổi map, thay cả `.wbt` và config; route là mục tiêu chuyển động còn trajectory là pose thực tế đã capture. Có thể dùng live bridge sau khi offline replay pass; không cần cài dependency vào Python controller.

## Regression và giới hạn

- Tập trung adapter/motor contract: `tests/test_webots_adapter.py` **5 passed**, motor contract **7 passed**. Full regression cuối lượt: **211 passed trong 142.88 s**; hai Webots world offline và corridor live bridge đều pass theo manifest ở trên.
- `detections=absent`: chưa có object nodes từ nhận dạng RGB. Không đưa room polygons, room IDs hoặc GT instance IDs vào builder.
- Route là scripted Supervisor ground-truth pose, không phải navigation policy, SLAM hay kiểm chứng động lực học robot. Live bridge đo parity packet/graph, không phải hiệu năng realtime.
- Một region/RoomNode ở single-wall và một region/RoomNode ở corridor không chứng minh segmentation quality tổng quát; world hai phòng đạt 2 rooms trong fixture cụ thể này.
- Invalid/Inf masking được giữ theo contract; capture Webots hiện tại chủ yếu có depth hợp lệ. M4 local technical/reference scope, cloud và license gates vẫn mở; chưa upload cloud/Kaggle.

## M12 mở rộng — live 2D Webots Display overlay

Đây là phần mở rộng sau checkpoint motor/collision và capture/replay M12. Mục tiêu của lượt này là hiển thị **FreeSpaceGrid2D native M3**, robot và trajectory trong Webots; không phải viewer OctoMap 3D.

### Đã triển khai

- `M12DiffDriveRGBD.proto` có thêm `Display` `m12_overlay` mặc định `640×480`; kích thước phải khớp `visualization.width/height` trong route config.
- `SceneGraphBuilder.last_grid` chỉ expose grid đã được mapping stage tạo và downstream sử dụng; overlay không gọi `project()` lần thứ hai.
- Host renderer `standalone_sg.visualization.live_overlay` dùng đúng semantics M3: `-1=unknown`, `0=free`, `1..100=MapConverter neighbour occupation score`, **không diễn giải score thành xác suất occupancy hoặc traversability**.
- World XY được đổi sang pixel với origin/resolution của grid thực, +Y được lật đúng chiều pixel, nearest-neighbor và aspect ratio giữ nguyên. Viewport được tính lại khi grid mở rộng/dời origin từ grid và pose/trajectory đã xử lý; không dùng wall/room ground truth.
- Host → controller dùng TCP downlink riêng cho visualization, framing JSON-header + RGB888 payload, kiểm tra kích thước/schema. Kênh này có latest-only sender; queue RGB-D/mapping vẫn là queue riêng, block-then-fail rõ ràng, không silently drop.
- Controller poll không blocking trong vòng motor, chỉ controller thread gọi Webots Display API. Có trạng thái `waiting/active/stale/finished/error`, map revision, simulation timestamp, grid size, legend semantics, robot đỏ và trajectory xanh. Display chậm/mất kết nối không được dùng để dừng motor hay rollback mapping.
- Lifecycle đã tách: controller gửi `done`; host xử lý hết packet, flush builder, gửi overlay `finished`; controller giữ frame cuối theo `hold_after_finish_s` hoặc đóng theo `finish_behavior`.
- Config đã thêm vào `configs/webots/m12_two_room_motor_route.json` và `configs/webots/m12_corridor_motor_route.json`: `visualization.enabled`, kích thước, publish/render frequency, stale timeout, finish behavior, layers và colors.

### Regression và giới hạn kiểm chứng

- Unit/protocol/renderer/controller-contract mới: **13 passed** (`test_m12_live_overlay.py` + `test_m12_motor_contract.py`); full Python regression sau tích hợp: **217 passed trong 96.75 s**.
- Đã kiểm tra tĩnh/compile được controller, PROTO contract và framing; chưa có artifact Webots live overlay thành công trong môi trường hiện tại.
- Hai lần thử Webots live (GUI và batch với direct `webots-bin`) bị chặn **trước khi controller gửi `done`** bởi Qt/Webots runtime:

```text
could not connect to display :0
Could not load the Qt platform plugin "xcb"
This application failed to start because no Qt platform plugin could be initialized
```

Do đó chưa được tuyên bố `Webots Display live pass`, chưa có screenshot/video hoặc số render thực tế. Đây là blocker môi trường hiển thị, không phải bằng chứng lỗi M3/mapping. Cần chạy lại trong desktop session có X/Wayland hợp lệ hoặc X server hỗ trợ Qt trước khi nghiệm thu live overlay.

### Lệnh chạy khi có Webots GUI

Dùng thư mục artifact riêng cho từng run. Với GUI, bỏ `--batch` bằng `--show-webots`:

```bash
XDG_RUNTIME_DIR=/tmp/webots-runtime WEBOTS_TMPDIR=/tmp/webots-tmp \
QT_QPA_PLATFORM=xcb QT_QPA_PLATFORM_PLUGIN_PATH=/snap/webots/27/usr/share/webots/lib/webots/qt/plugins \
LD_LIBRARY_PATH=/snap/webots/27/usr/share/webots/lib/webots \
MPLCONFIGDIR=/tmp/m12-webots-mpl PYTHONPATH=.:src \
python3 scripts/run_m12_webots_live_bridge.py \
  --world webots/worlds/m12_two_rooms_motor.wbt \
  --config configs/webots/m12_two_room_motor_route.json \
  --capture artifacts/<run>_capture \
  --output artifacts/<run>_graph \
  --replay-output artifacts/<run>_replay \
  --webots /snap/webots/27/usr/share/webots/bin/webots-bin \
  --show-webots
```

Sau khi chạy pass, Display nằm trong cửa sổ robot/Webots; artifact graph vẫn gồm `snapshot.json`, `delta_log.json`, `frame_diagnostics.json`, `scene_graph.png` và `trajectory_rooms.png`. Overlay này là 2D M3 + pose/trajectory; chưa phải reconstructed 3D map.

## Cập nhật nghiệm thu overlay live — 2026-09-29

Đã chạy lại sau khi tách render overlay sang worker latest-only riêng. Luồng nhận RGB-D/mapping không còn tự render ảnh; visualization chỉ nhận snapshot mới nhất trên TCP channel riêng. Khi queue raw mặc định `2` làm host mapping chậm hơn controller, run two-room đầu bị timeout ở frame gửi thứ 75; run kiểm chứng dùng queue bounded `128`, `queue_put_timeout_s=30`, không bỏ packet. Đây là điều chỉnh vận hành của CLI, không đổi mapping/segmentation baseline.

### GUI environment

Kiểm tra chỉ đọc trong môi trường agent:

- `DISPLAY=:0`, `WAYLAND_DISPLAY` không có, `XDG_RUNTIME_DIR=/run/user/1000`, `DESKTOP_SESSION=plasma`, `XDG_SESSION_TYPE=x11`, `XDG_CURRENT_DESKTOP=KDE`.
- `xdpyinfo` không mở được `:0`; `xauth info` báo lỗi lock trên `/run/user/1000/.Xauthority`; `loginctl` không kết nối được user bus.
- `webots-bin` độc lập chạy được ở batch khi dùng đúng `LD_LIBRARY_PATH`/Qt plugin path, nhưng agent không có quyền truy cập desktop display. Không tự gán `DISPLAY`, sửa Xauthority hoặc cài lại Qt.

Vì vậy: **headless/batch live overlay đã có bằng chứng chạy thật; GUI/Webots Display chưa nghiệm thu**, và chưa có screenshot/video từ cửa sổ GUI trong môi trường này.

### Batch live overlay thật

Two-room:

- Artifact: `artifacts/m12_webots_two_rooms_overlay_queue_capture/`, `artifacts/m12_webots_two_rooms_overlay_queue_graph/`, `artifacts/m12_webots_two_rooms_overlay_queue_replay/`.
- 84 RGB-D packets, 11/11 waypoint; map revisions `1..14` được publish/render theo latest-only, grid thay đổi kích thước/origin trong diagnostics.
- Controller nhận/render `13/13`, `0` lỗi trong route; host submit `85`, render `14`, thay thế `69` ảnh cũ; live graph và no-visualization same-packet replay khớp (`revision=85`, `153` nodes, `625` edges, `2` regions, `2` rooms).
- Có `BrokenPipe` ở host khi đóng sau khi controller đã hoàn tất; không làm route/mapping/replay thất bại, nhưng được giữ là shutdown-cleanup limitation.

Corridor:

- Artifact: `artifacts/m12_webots_corridor_overlay_queue_capture/`, `artifacts/m12_webots_corridor_overlay_queue_graph/`, `artifacts/m12_webots_corridor_overlay_queue_replay/`.
- 24 RGB-D packets, 10/10 waypoint; map revisions `1..24`, grid mở rộng và đổi origin đúng theo world coordinates.
- Controller render `25/25`, host submit/render/send `25/25`; live graph và same-packet replay khớp (`revision=25`, `59` nodes, `210` edges, `2` regions, `2` rooms). Controller ghi một disconnect ở giai đoạn đóng sau trạng thái `finished`; không làm route thất bại.

Hai run đều giữ `detections=absent`, không đưa room/region GT vào builder, không dangling/self edge và delta/snapshot reload pass. Kết quả `2` rooms của corridor chỉ là output baseline cần đánh giá, không phải tuyên bố phân phòng đúng.

Lệnh batch tái lập (đổi `world/config` và thư mục artifact tương ứng):

```bash
LD_LIBRARY_PATH=/snap/webots/27/usr/share/webots/lib/webots \
QT_QPA_PLATFORM=xcb \
QT_QPA_PLATFORM_PLUGIN_PATH=/snap/webots/27/usr/share/webots/lib/webots/qt/plugins \
MPLCONFIGDIR=/tmp/m12-webots-mpl PYTHONPATH=.:src \
python3 scripts/run_m12_webots_live_bridge.py \
  --world webots/worlds/m12_two_rooms_motor.wbt \
  --config configs/webots/m12_two_room_motor_route.json \
  --capture artifacts/<run>_capture \
  --output artifacts/<run>_graph \
  --replay-output artifacts/<run>_replay \
  --webots /snap/webots/27/usr/share/webots/bin/webots-bin \
  --queue-size 128 --queue-put-timeout-s 30 \
  --show-webots
```

Trong desktop Terminal có display hợp lệ, không tự đặt `DISPLAY`; kế thừa biến môi trường của Terminal và dùng `--show-webots`. Khi GUI chạy được, cần lưu screenshot/video và gửi kèm `capture_manifest.json`, `live_bridge_manifest.json` để chốt nghiệm thu GUI. Full regression sau hardening: **218 passed trong 95.16 s**; targeted overlay/motor contracts: **14 passed**.

## Cập nhật chẩn đoán độ trễ occupancy overlay — 2026-09-29

Đã đo cùng world/config two-room, 84 packet RGB-D, resolution và baseline mapping không đổi. Mỗi packet hiện lưu simulation timestamp, các mốc `time.monotonic()` ở capture/receive/process/publish/render, thời gian M1/M2/M3/M4/M5/M6–M9, queue wait/length, revision và map timestamp mà Webots Display thực sự vẽ. Báo cáo nằm trong `latency_diagnostics.json` và được nhúng vào `live_bridge_manifest.json`.

Kết quả trước thay đổi, Webots `fast` và publish sau builder:

- host processing: median **1832.6 ms**, p95 **3105.2 ms**, max **3318.2 ms**;
- M2: **243.9 / 591.3 / 733.7 ms**; M3: **492.2 / 641.9 / 684.8 ms**;
- M4/M5: **21.5 / 32.3 / 47.1 ms**; M6–M9: **982.2 / 1861.3 / 1950.0 ms**;
- Display render: **0.364 / 0.787 / 1.037 ms**; host overlay render: **46.4 / 54.3 / 54.7 ms**;
- capture interval là **1.28 s simulation**; queue wait **77.5 / 138.7 / 144.7 s** wall-clock; raw queue high-water **77/128**;
- map age khi vẽ: **98.7 / 259.7 / 295.8 s simulation**; chỉ 6 revision được vẽ trước khi route kết thúc; drain sau dừng khoảng **17.0 s**.

Đã tách publish occupancy grid ngay sau M3 (`stage=m3_ready`) qua visualization channel riêng, trước M4–M9. Cùng world/config cho kết quả kỹ thuật: live graph và same-packet replay khớp; 84 mapping updates, không drop packet mapping. Tuy nhiên Fast vẫn bị host xử lý chậm hơn simulation; early-M3 không làm giảm tổng throughput vì bottleneck chính là M2/M3 và M6–M9, không phải Webots Display.

Đã chạy lại cùng sequence ở chế độ `real-time` của Webots. Với Webots R2025a, binary này không nhận `--mode=real-time`; chế độ này được thực hiện bằng cách **bỏ option `--mode`**, để Webots dùng mặc định. Kết quả:

- host processing: **1960.0 / 2521.1 / 2727.4 ms**;
- queue wait: **32.35 / 54.30 / 55.00 s**; high-water **30/128**;
- map age: **25.5 / 42.0 / 45.8 s simulation**;
- Display nhận/render **62** revision, trong đó **54** trước route end; Display render **0.323 / 0.532 / 0.783 ms**; drain khoảng **17.4 s**;
- live graph và replay cùng captured packets vẫn khớp, status `pass`.

Kết luận: bottleneck là throughput của host pipeline, chủ yếu M2/M3 và graph update M6–M9; tăng queue chỉ che backlog và không phải giải pháp chính. `real-time` là chế độ phù hợp hơn để quan sát vì map age giảm mạnh, nhưng máy hiện tại vẫn chưa theo kịp capture cadence 1.28 s cho toàn bộ sequence. Fast phù hợp đo throughput/batch, không phù hợp đánh giá độ tươi overlay. Một demo đồng bộ/pacing hoặc capture cadence thưa hơn cần được cấu hình riêng nếu muốn map bám sát hơn; không tự đổi resolution/stride, không bỏ packet, không tăng capture frequency.

Artifacts đối chiếu:

- Fast baseline: `artifacts/m12_webots_two_rooms_latency_baseline2_graph/`;
- early-M3 publish: `artifacts/m12_webots_two_rooms_latency_earlym3_graph/`;
- real-time: `artifacts/m12_webots_two_rooms_latency_realtime_graph/`;
- mỗi thư mục có `latency_diagnostics.json`, `live_bridge_manifest.json`; capture/replay tương ứng dùng hậu tố `_capture` và `_replay`.

Regression sau instrumentation: **219 passed trong 107.42 s**; targeted live-overlay tests: **7 passed**. Snapshot/delta same-packet replay vẫn khớp. Chưa có screenshot/video GUI vì môi trường agent không truy cập được X display; GUI/Webots Display vẫn chưa nghiệm thu. Batch/headless overlay đã chạy thật và có revision/map-age diagnostics; chưa gọi đây là realtime performance benchmark.

## M12 mở rộng — ghi route bằng motor và bàn phím

Đã thêm chế độ tùy chọn `route_recording.enabled=true` trong
`m12_motor_capture.py`. Đây là tiện ích ở lớp Webots integration, không
thay đổi M1–M10, mapping, segmentation hoặc SceneGraphBuilder.

- `W`/mũi tên lên: chạy tiến; `S`/mũi tên xuống: chạy lùi;
  `A`/mũi tên trái và `D`/mũi tên phải: quay bằng hai motor.
- `SPACE` ghi pose thực tế hiện tại từ `Supervisor.getPose()` thành waypoint.
  Waypoint gần trùng trong ngưỡng `min_waypoint_distance_m` bị bỏ qua.
- `Q` dừng motor an toàn và ghi một config route chạy được.
- Khi Webots mất keyboard focus, lệnh hiện tại trở thành stop; controller
  không giữ lại lệnh bánh xe cũ.
- Không set `translation`/`rotation` trong lúc ghi; physics trace và actual
  trajectory vẫn là bằng chứng pose thực tế.
- Config được kiểm tra project-local; output route ghi atomically và mặc định
  không ghi đè file cũ.

Template dùng lại:
`configs/webots/m12_motor_route_recording_template.json`.
Template đang dùng collision AABB của two-room; khi đổi world phải thay
`world_file`, output/sequence và toàn bộ AABB cho khớp world mới.

Quy trình:

1. Mở world có `M12DiffDriveRGBD` và đặt `customData` tới template.
2. Chạy live bridge với output riêng cho lần ghi route.
3. Điều khiển robot, nhấn `SPACE` tại các điểm cần giữ, nhấn `Q` để kết thúc.
4. Lấy file `route_recording.output_path` (ví dụ
   `artifacts/recorded_routes/m12_route_config.json`), kiểm tra route và AABB,
   rồi đặt `customData` của world tới config này để chạy lại waypoint.
5. Đổi `output_dir`, `sequence_id` và artifact directories trước mỗi lần
   replay để không ghi đè capture cũ.

Artifact capture vẫn ghi route đã thấy trong `capture_manifest.json`; config
ghi ra chứa `route_recording_provenance`, nguồn pose Supervisor và cờ không
teleport. Tính năng này chưa phải navigation policy, obstacle avoidance hay
route planner; các đoạn nối waypoint vẫn phải được kiểm tra clearance/collision
trước khi chạy lại.

Regression sau bổ sung recorder: targeted route-recording **20 passed**;
full Python regression **228 passed trong 135.36 s**. Chưa có Webots GUI
interactive run trong lượt này; cần chạy thủ công với keyboard focus để xác
nhận artifact route thực tế.

## Hardening kết thúc live bridge — 2026-09-30

Đã sửa lỗi Q/GUI shutdown làm mất PNG. Sau khi host nhận `done` và xử lý được
ít nhất một packet, bridge ghi ngay `snapshot.json`, `delta_log.json`,
trajectory và bốn PNG (`spatial_map.png`, `relationship_graph.png`,
`scene_graph.png`, `trajectory_rooms.png`) **trước** khi chờ Webots đóng.
Vì vậy hộp thoại `Save before closing?`, timeout đóng GUI hoặc controller
failure không còn xóa kết quả đã xử lý.

- Capture hoàn chỉnh vẫn chạy replay cùng packet và báo `status=pass`.
- Collision/timeout vẫn báo `status=partial`, giữ nguyên nguyên nhân trong
  manifest và bỏ qua replay không hợp lệ; không được diễn giải partial graph
  là run thành công.
- `main` trả mã 0 cho `pass`, mã 2 cho `partial`; PNG vẫn được giữ để chẩn
  đo.

Targeted live-overlay regression: **8 passed**; full regression sau sửa:
**231 passed trong 101.48 s**. Chưa có GUI live run mới trong môi trường agent.

## Phân tích complete_apartment — baseline và coverage/latency — 2026-09-30

Đã xác minh lại `artifacts/runs/complete_apartment_latest` trước khi thử
thay đổi:

- capture `complete`: **207/207 RGB-D frames**, 8307 physics steps, không
  penetration, waypoint **1/1**;
- bridge `pass`, 207 mapping updates, `live_delta_replay_equal=true` và
  `live_equals_same_packet_replay=true`;
- snapshot cuối revision **208**, 374 nodes, 1465 edges, **8 regions và
  8 RoomNodes**. Đây là số entity của baseline, **chưa phải kết luận có
  8 phòng đúng hình học**;
- branch M4: **128** `accept_reuse_adjusted_no_differential`, **48**
  `accept_incremental`, **29** `fallback_full_decomposition`, **2**
  `reject_expanded_empty_preserve_previous`; stale count **2**;
- queue high-water **94**. Latency baseline p50/p95/max (ms): M2
  **201.4/398.7/491.9**, M3 **563.0/913.1/1177.5**, M4–M5
  **40.3/63.6/110.6**, M6–M9 **1283.6/1842.3/2336.6**, host tổng
  **2138.8/3110.7/3796.0**. Queue wait p50/p95/max là
  **46.8/208.3/229.0 s**; map age p50/p95/max là **0.872/61.324/
  72.576 s simulation** (chỉ các revision thực sự được render); map age và simulation/capture semantics vẫn
  được đọc từ `latency_diagnostics.json`, không trộn với wall-clock.

Input hashes của baseline: world
`2f8066c422b54f56d383693b08752f0733ad542971c9615cadfd981bfbba7470`,
config `f0642708fb4ea95ef2f1e7d4f89656ef818d82a707f364fd40c358edd88a982d`,
pipeline `210124b16fd3651c06fd548a1704f27595f61761dd1bfb4db4099e51895d1523`.

### Thay đổi latency có kiểm chứng

M8 membership hiện chỉ tính intersection polygon chính xác khi một
navigation entity có **nhiều hơn một region ứng viên**; nếu chỉ có một ứng
viên, kết quả chọn region không cần triangulate. Kết quả triangulation của
cùng ring cũng được cache bounded. Đây là tối ưu thực thi, không đổi tie-break,
geometry, source rule hay graph contract.

Evidence:

- M8/M9 targeted tests: **37 passed**;
- replay lại đúng 207 raw packets baseline bằng code mới: revision 208,
  374 nodes, 1465 edges, 8 regions/rooms; snapshot, delta log và trajectory
  JSON giống baseline; `snapshot_reload_equal=true`, `delta_replay_equal=true`;
- builder update+flush giảm từ **319.64 s** xuống **309.63 s** trên cùng
  packets (khoảng **3.1%**). Đây là throughput offline của builder, chưa
  phải realtime benchmark.

Artifacts A/B: `artifacts/runs/complete_apartment_latest/replay/` và
`artifacts/runs/complete_apartment_latest_cache_replay/`.

### Coverage route — kết quả thực tế

Coverage thử nghiệm dùng world/config riêng và AABB chỉ cho collision
validation, không đưa AABB/room GT vào builder.

- Lượt `complete_apartment_coverage_baseline`: dừng do **collision
  penetration**, 88 frames, 8/20 waypoint; giữ làm regression route lỗi.
- Route an toàn lượt `coverage_v2`: **183 frames**, 12/20 waypoint, không
  penetration nhưng timeout ở waypoint 12. Collision trace có 1279 trạng
  thái contact và không penetration; `live_delta_replay_equal=true`, nhưng
  run partial nên `live_equals_same_packet_replay=false`. Snapshot partial
  revision 184 có 8 regions/8 RoomNodes; không dùng để gọi là phân đúng 8
  phòng.
- Provenance v2 cần thận trọng: `metadata.json` của các frame ghi config
  SHA-256 `34e46ce5952b5243a778c40c027078302b55cf1edf5352fbf1304edb107265bb`,
  trong khi `capture_manifest` ghi `ff26ca4ad832c11e1afb953f2baa5d2c1fe31cf198cfbb552e4f11197bda64b4`.
  File config đã bị thay trong lúc phiên host partial đang kết thúc; vì vậy
  v2 là raw-capture evidence có hash theo frame, không phải baseline config
  sạch để tái lập bằng một lệnh hiện tại.
- Coverage v2 latency vẫn bị backlog: host p50/p95/max **3947.8/5440.6/
  6680.5 ms**, M3 **1090.6/1523.5/1730.0 ms**, M6–M9
  **2478.8/3638.4/4635.5 ms**, queue high-water **128**, map age
  p50/p95/max **45.4/81.7/86.3 s simulation**. Điều này xác nhận queue
  lớn không giải quyết bottleneck; graph update và M2/M3 là phần cần pacing
  hoặc capture cadence riêng.

Artifacts coverage: `artifacts/runs/complete_apartment_coverage_baseline/`
và `artifacts/runs/complete_apartment_coverage_v2/`. Coverage v2 chưa là
một baseline hoàn tất; chưa có bằng chứng định lượng rằng vùng phải nghi ngờ
bên phải/đường chéo trung tâm được phân phòng tốt hơn.

Đã thêm exporter đánh giá `scripts/export_complete_apartment_evaluation.py`.
Hai ảnh cùng hệ tọa độ world, có authored wall/window/obstacle AABB chỉ để
đối chiếu, native RoomNode footprints và actual trajectory:

- `artifacts/runs/complete_apartment_latest/graph/evaluation_reference_overlay.png`;
- `artifacts/runs/complete_apartment_coverage_v2/graph/evaluation_reference_overlay.png`.

Ảnh này không tô occupancy M3: snapshot M10 hiện không lưu `FreeSpaceGrid2D`.
Do đó không được đọc vùng trắng/tím trong ảnh như free/occupied/unknown. Việc
replay cùng raw packets vẫn khớp graph, nhưng muốn kết luận M2/M3 tại từng cột
phải bổ sung artifact grid exact revision trong replay/diagnostics riêng.

### Trạng thái môi trường Webots

Coverage v3 chưa được nghiệm thu: lần kiểm tra độc lập
`webots-bin --batch` không khởi động ổn định trong môi trường hiện tại và
timeout/core-dump sau thông báo `Webots has not been started regularly`.
Đây là blocker runtime để chạy thêm route, không phải bằng chứng lỗi M3,
DuDe hay M8. Không thay Webots system-wide và không tuyên bố coverage v3.

Regression cuối lượt sau tối ưu: **234 passed trong 110.45 s**. Chưa có
claim cải thiện số phòng; claim hiện tại chỉ là parity hình học và giảm
throughput builder khoảng 3.1% trên input baseline.

## Trace grid M3 và chẩn đoán complete_apartment — 2026-09-30

Đã replay offline **đúng 207/207 packets** của
`artifacts/runs/complete_apartment_latest/capture`, giữ nguyên world/config/
pipeline và không gọi `project` thêm. `GridTraceWriter` chỉ ghi lại object
`FreeSpaceGrid2D` mà M3 đã trả cho cùng update downstream; trace không được
đưa vào `SceneGraphSnapshot` và không làm thay đổi converter state.

Lệnh tái lập:

```bash
PYTHONPATH=.:src python3 scripts/run_m12_webots_native_replay.py \
  --capture artifacts/runs/complete_apartment_latest/capture \
  --config configs/webots/m12_complete_apartment_route_recording.json \
  --output artifacts/runs/complete_apartment_latest_grid_replay/graph \
  --grid-output artifacts/runs/complete_apartment_latest_grid_replay/m3_m4_grid_trace \
  --no-determinism-check
```

Trace nằm ở `artifacts/runs/complete_apartment_latest_grid_replay/m3_m4_grid_trace/`:

- `index.json` và 207 metadata sidecars;
- mỗi frame một `.npz` lossless gồm `values`, `z_floor`, `z_ceiling`;
- khi bật trace, worker DuDe trả thêm và lưu `m4_preprocessed_mask`/
  `m4_black_mask` thực tế, cùng raw M4 regions, M5 current/retained regions
  và M8 RoomNodes của đúng snapshot frame đó;
- semantics được giữ nguyên: `-1=unknown`, `0=free`, `1..100` là source
  occupation score, không được diễn giải thành xác suất occupancy.

Kiểm tra bật/tắt trace: `snapshot.json`, `delta_log.json`, `trajectory.csv` và
`trajectory.json` của replay có trace giống byte với
`artifacts/runs/complete_apartment_latest_cache_replay/`. Snapshot reload và
delta replay đều pass. Phân tích tự động nằm tại
`artifacts/runs/complete_apartment_latest_grid_replay/grid_trace_analysis.json`;
overlay cùng hệ tọa độ world nằm tại thư mục `diagnostic_selected/`.

Kết quả truy nguồn hiện tại:

- Frame **43**, M3 revision **44**, là frame đầu tiên của nhánh
  `fallback_full_decomposition`. Biên chéo free/unknown đã có ngay trong
  `M3 raw FreeSpaceGrid2D`: 2296 free, 15876 unknown cells. Vì vậy chưa có
  bằng chứng M5/M8 tạo ra đường chéo này.
- Ở frame 43, preprocessing source đưa 2833 cells vào mask; 1194 trong số
  đó nằm trên cells M3 đang mã hóa unknown. Đây là hiệu ứng morphology/
  preprocessing của source (mask nhị phân được mở rộng), không phải lý do để
  âm thầm sửa adapter hoặc thêm policy occupied-wins.
- Frame **44**, revision **45**, là lần đầu raw M4 trả 2 regions; M5 nhận
  accepted và M8 có 2 RoomNodes cùng revision. Các lớp sau không làm mất
  region ở điểm chuyển này.
- Hai frame **117–118** là `reject_expanded_empty_preserve_previous`; M5 stale
  giữ evidence cũ đúng contract, không tạo observation mới.
- Final frame 206/revision 207 có 8 raw regions, 8 current regions và 8
  RoomNodes. Đây là output baseline trên vùng đã quan sát, chưa phải bằng chứng
  8 phòng đúng với kiến trúc căn hộ.

Ảnh chẩn đoán được tạo bởi `scripts/export_complete_apartment_grid_diagnostics.py`;
`wall_aabbs_xy`/`obstacle_aabbs_xy` chỉ là lớp tham chiếu đánh giá, không đi vào
M3–M8. Các overlay không chứng minh ground-truth segmentation hoặc source
parity toàn pipeline. Kết luận hiện tại: cần ưu tiên coverage/độ biểu diễn
hình học và đánh giá sensitivity của preprocessing source; chưa chỉnh tham số,
chưa sửa kernel và chưa tuyên bố cải thiện phân phòng.

Full regression sau grid trace: **235 passed trong 80.22 s** (tăng một test
unit cho artifact writer). V1/V2 coverage vẫn partial; lỗi khởi động Webots
v3 giữ tách riêng và không chặn chẩn đoán offline này.

## Run keyboard 276-frame và thử nghiệm cải thiện — 2026-09-30

### Bảo toàn và xác minh capture

Run gốc được giữ nguyên tại `artifacts/runs/complete_apartment_latest/` và
được sao lưu không chỉnh sửa sang
`artifacts/runs/complete_apartment_keyboard_unsaved_276_source_20260930/`.
Manifest gốc khai báo `capture_status=failed`, `status=error` với lỗi chính xác
`no waypoint was recorded; press SPACE before Q`, `waypoint_count=0`, nhưng có
**276/276 RGB-D frames**. Đây không phải commanded-route run hoàn chỉnh.

Kiểm tra read-only trên bản sao:

- frame index liên tục `0..275`, simulation timestamp tăng từ `0.64` đến
  `176.64 s`;
- đủ `camera_bgra.bin`, `range_depth_f32.bin`, `metadata.json` cho mọi frame,
  kích thước buffer khớp metadata;
- `physics_trace.json` có **11.072 physics steps**;
- world/config hashes giữ nguyên: world
  `2f8066c422b54f56d383693b08752f0733ad542971c9615cadfd981bfbba7470`, config
  `f0642708fb4ea95ef2f1e7d4f89656ef818d82a707f364fd40c358edd88a982d`;
- sensor/base extrinsics pass với sai số translation tối đa khoảng
  `4.2e-15 m`, yaw `6.3e-15 rad`; collision AABB trace pass, không penetration,
  minimum clearance `0.0140 m`.

Chi tiết validation ở `validation_report.json`: 861.774 depth samples hợp lệ
metric/axial và 17.286 sample invalid/no-return được giữ nguyên trong capture,
nhưng replay mask chúng và không biến chúng thành free ray.

Đã thêm cờ replay hẹp `--allow-unsaved-route-capture`. Cờ này chỉ chấp nhận
đúng loại lỗi keyboard ở trên sau khi vẫn validate toàn bộ raw packet; output
được ghi `capture_status=partial_unsaved_route`, `route_replayable=false`,
`commanded_route=[]`, và trajectory là pose Supervisor đã capture. Không sửa
manifest gốc và không biến capture thành route có waypoint.

### Baseline replay mới trên đúng 276 packets

Lệnh:

```bash
PYTHONPATH=.:src python3 scripts/run_m12_webots_native_replay.py \
  --capture artifacts/runs/complete_apartment_keyboard_unsaved_276_source_20260930/capture \
  --config configs/webots/m12_complete_apartment_route_recording.json \
  --output artifacts/runs/complete_apartment_276_source_replay_v3/graph \
  --grid-output artifacts/runs/complete_apartment_276_source_replay_v3/m3_m4_grid_trace \
  --allow-unsaved-route-capture --no-determinism-check
```

Kết quả: pipeline offline native pass, graph revision `277`, **492 nodes / 2004
edges**, 8 regions và 8 RoomNodes ở frame cuối; 276 trajectory poses; snapshot
reload và delta replay pass; không dangling/self edge. Có 1 frame M4 rejected/stale
(frame 227), các frame còn lại vẫn giữ đúng provenance của source. Đây là baseline
packet replay, chưa phải bằng chứng Webots live builder hay room accuracy.

Trace exact M3/M4 nằm ở
`artifacts/runs/complete_apartment_276_source_replay_v3/m3_m4_grid_trace/`;
ảnh cùng world coordinates và trajectory ở thư mục `diagnostics/`. Grid trace
ghi `values`, `z_floor`, `z_ceiling`, projection metadata, sensor pose/intrinsics,
M4 preprocessing mask, raw M4 regions, M5 current/retained regions và M8 rooms.
Writer chỉ quan sát `FreeSpaceGrid2D` đã trả cho downstream, không gọi project
thêm và không đưa grid vào snapshot.

### Nguyên nhân đã định vị trong run 276

Trace cho thấy ở frame cuối M3 có `38.025` free, `1.079` occupied và `25.102`
unknown cells. Với profile reference/source, M4 mask có `36.103` cells và **3.921
cells nằm trên M3-unknown**; trên toàn bộ 276 frame, overlap này xuất hiện ở
276/276 frame, tổng **1.221.253 cells**, max `6.745/frame`. Vì vậy source
morphology là cơ chế có thể biến frontier chưa quan sát thành input free cho
decomposition. Đây là điểm đầu tiên quan sát được sau M3; chưa có bằng chứng
M5/M8 tự tạo biên chéo. Ảnh frame 275 và frame stale 227 thể hiện đồng thời
M3 raw, mask M4, M5 regions, M8 RoomNodes và trajectory.

### Thử nghiệm cải thiện riêng, không đổi reference profile

Đã thêm policy opt-in native `extended_unknown_boundary`: sau khi chạy đúng
preprocessing source, mask được giao cho DuDe bị cắt lại theo valid M3
(`unknown` không được coi là free). Profile source vẫn là mặc định; policy mới
được ghi rõ là **extended, không source-parity**.

Replay cùng 276 packets:

```bash
PYTHONPATH=.:src python3 scripts/run_m12_webots_native_replay.py \
  --capture artifacts/runs/complete_apartment_keyboard_unsaved_276_source_20260930/capture \
  --config configs/webots/m12_complete_apartment_route_recording.json \
  --output artifacts/runs/complete_apartment_276_extended_unknown_boundary/graph \
  --grid-output artifacts/runs/complete_apartment_276_extended_unknown_boundary/m3_m4_grid_trace \
  --allow-unsaved-route-capture \
  --dude-preprocessing-policy extended_unknown_boundary --no-determinism-check
```

So sánh tại `artifacts/runs/complete_apartment_276_extended_unknown_boundary/profile_comparison.json`:

- M3 `values`, `z_floor`, `z_ceiling` **giống nhau ở cả 276 frame**; mapping
  không bị thay đổi bởi thử nghiệm M4;
- M4 unknown-overlap giảm từ `1.221.253` xuống **0 ở 276/276 frame**;
- graph vẫn reload/delta-replay pass, nhưng kết quả cuối đổi thành **491 nodes /
  1989 edges, 7 regions và 7 RoomNodes**, stale tăng từ 1 lên 2.

Kết luận: policy extended đã chứng minh một cải thiện semantics/evidence rõ ràng
— không dùng unknown như free — nhưng **chưa đủ GT để tuyên bố room IoU/accuracy
tăng**. Do room count và stale behavior thay đổi, chưa đặt policy này làm default;
giữ `source` làm reference và dùng extended như profile đánh giá bảo thủ.

Full regression sau thay đổi: **241 passed trong 87.28 s**. Native worker build
pass; warning chỉ từ upstream `mathtool`/SVG headers. Chưa import map mới, chưa
upload cloud, chưa tuyên bố complete_apartment đã phân phòng chính xác.

### Kiểm tra bật/tắt grid export trên đúng 276 packets

Đã chạy thêm source replay không `--grid-output` tại
`artifacts/runs/complete_apartment_276_source_replay_nogrid/graph/`. Kết quả
trajectory CSV/JSON giống byte với replay có trace, và cả hai đều pass
snapshot reload/delta replay. Tuy nhiên snapshot/delta graph **không giống**:

- frame đầu tiên khác observable là frame 18, lúc đó tập region ID bị hoán vị
  (`[4,1,3,2]` so với `[4,2,3,1]`), trong khi branch/status/count giống nhau;
- frame cuối trace có 8 RoomNodes/492 nodes/2004 edges, còn no-grid có 9
  RoomNodes/493 nodes/2006 edges;
- branch counts và stale count vẫn giống (`4 fallback`, `1 reject`, `1 stale`).

Do đó chưa thể kết luận graph-toggle parity. Đây là bằng chứng về khác biệt thứ
tự/stateful của native incremental output giữa hai fresh replay; chưa đủ để quy
cho `GridTraceWriter`, vì writer chỉ chạy sau khi builder update đã trả grid và
snapshot. Cần giữ hai artifact này làm regression/nondeterminism case, không
chọn run 8 hay 9 phòng làm “đúng” hơn. Runtime đo được cũng chỉ là offline
builder+flush: khoảng `670.2 s` có trace và `451.1 s` không trace, không phải
live latency benchmark. Chi tiết hash và frame divergence nằm tại
`artifacts/runs/complete_apartment_276_source_replay_v3/graph_toggle_comparison.json`.

### Repeatability source trace on/off trên cùng 276 packets — 2026-09-30

Đã chạy fresh process với cùng capture, config và binary hiện tại; không dùng
`extended_unknown_boundary` làm mặc định. Ba run có trace M3/M4 đầy đủ:

- `artifacts/runs/complete_apartment_276_repeatability/source_trace_01_retry/`
- `artifacts/runs/complete_apartment_276_repeatability/source_trace_03/`
- `artifacts/runs/complete_apartment_276_repeatability/source_trace_04/`

Ba run không export trace (render bị tắt riêng trong process đánh giá để tránh
layout PNG quan hệ graph lớn làm chậm hậu xử lý) là:

- `artifacts/runs/complete_apartment_276_repeatability/source_nogrid_02_fast/`
- `artifacts/runs/complete_apartment_276_repeatability/source_nogrid_03_fast/`
- `artifacts/runs/complete_apartment_276_repeatability/source_nogrid_04_fast/`

Kết quả hiện tại:

- Trace-on: cả 3 run đều **276 frame, revision 277, 493 nodes / 2006 edges,
  9 regions / 9 RoomNodes**; snapshot/delta hash giống nhau giữa các run.
- Trace-off: cả 3 run đều có cùng kết quả như trên; snapshot hash và delta hash
  giống trace-on hiện tại. Khác biệt runtime/RSS bị loại khỏi so sánh semantic.
- Cả 6 run đều có `4 fallback`, `1 reject/stale`, trajectory 276 pose,
  snapshot reload và delta replay pass; terminal delta đều có
  `room_maintenance_flushed, terminal_flush` tại revision 277.
- So sánh theo thứ tự lớp `M3 grid → M4 mask → M4 geometry/adjacency → M5 → M8`
  không tìm thấy divergence giữa ba trace-on hiện tại; trace-on/off hiện tại
  cũng không khác output graph.

Run trace cũ `complete_apartment_276_source_replay_v3` vẫn cho 8 phòng và khác
ở frame 18; đối chiếu trace cho thấy M3 và M4 mask giống nhau nhưng raw M4
geometry khác. Artifact cũ không lưu hash binary tại thời điểm chạy, nên chỉ
có thể ghi nhận là **historical fresh-process difference**, chưa quy kết cho
native nondeterminism hay cho GridTraceWriter. Kết quả repeatability hiện tại
đã ổn định sau khi cố định binary/config; report đầy đủ ở
`artifacts/runs/complete_apartment_276_repeatability/repeatability_comparison_current.json`.

Binary hash dùng cho các run current:

- `standalone_sg_octomap_fixture_runner`:
  `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29`
- `occusg_dude_worker`:
  `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959`

### So sánh source với extended trên cửa sổ hình học đã kiểm tra

Đã tạo evaluation-only reference từ các Plane được đặt tên trong
`complete_apartment.wbt` (`LIVING_ROOM_1`, `KITCHEN`, `CORRIDOR`,
`BATHROOM_1`, `ROOM_1`, `ROOM_2`, v.v.) và hai khe cửa suy ra từ các wall
segments tương ứng. Reference này nằm ở
`configs/webots/m12_complete_apartment_evaluation.json`; không đi vào builder.
Đây là authored floor reference đã kiểm tra theo world, chưa phải bộ nhãn room
độc lập; vì vậy các IoU dưới đây chỉ là đối chiếu hình học trong vùng quan sát,
không phải accuracy claim.
Script và report:

- `scripts/evaluate_complete_apartment_profiles.py`
- `artifacts/runs/complete_apartment_276_repeatability/source_trace_04_vs_extended.json`
- `artifacts/runs/complete_apartment_276_repeatability/profile_evaluation/source_vs_extended.json`
- `.../source_evaluation_overlay.png`, `.../extended_evaluation_overlay.png`

Trên cùng packets và revision 276:

- Cả hai profile có coverage M3 giống nhau. Vùng rõ nhất được chọn là
  `BATHROOM_1`: source observed IoU tốt nhất `0.681`, extended `0.643`.
- Vùng doorway `CORRIDOR_ROOM_1_DOOR` và `CORRIDOR_ROOM_2_DOOR` đều có
  region spanning hai phía ở cả hai profile; extended không chứng minh được
  kết nối cửa tốt hơn. Coverage doorway lần lượt là `0.929` và `0.829` cho
  cả hai profile.
- Vùng còn nhiều unknown là `GARDEN`: coverage `0.0`, unknown fraction `1.0`
  cho cả hai; đây là thiếu quan sát, không phải cơ sở đánh giá room accuracy.
- Trên các floor rectangle khác, extended cải thiện nhẹ một vùng (`ROOM_1`
  `0.767 → 0.771`) nhưng giảm ở `LIVING_ROOM_1` (`0.760 → 0.691`),
  `LIVING_ROOM_2` (`0.640 → 0.101`) và các vùng khác. Vì vậy không có bằng
  chứng cải thiện hình học phòng nhất quán.
- Unknown-overlap của source vẫn là policy metric phụ (`1,221,253` cell trên
  276 frame), extended là `0`; đây **không phải accuracy metric**. Room count
  `9` so với `7` cũng không phải accuracy.

Kết luận: giữ `source` làm reference/default. `extended_unknown_boundary` chỉ
được giữ như profile thử nghiệm bảo thủ; chưa có bằng chứng độc lập rằng nó
cải thiện ranh giới phòng mà không làm giảm các vùng đã quan sát. Không sửa
kernel, không thêm room GT vào builder. Full regression mới nhất là
**241 passed trong 121.03 s**; focused M4/replay-policy tests là **13 passed**.
Các lượt trên là offline integration replays và evaluation scripts, không phải
test pytest mới.
