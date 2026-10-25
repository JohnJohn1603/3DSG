# Nghiệm thu D2 — XYZ live graph viewer

Ngày: 2026-10-16  
Workspace: `Fusion_Concept_OccuSG`  
Run nguồn: `d1_c2_apt0_20261016_v2`  
Phạm vi: viewer-only; không chạy capture, perception, Qwen, hybrid mapping hay native replay.

## Kết quả

D2 đạt trên 12 revisions của ReplicaCAD `apt_0`, route
`replica-cad-apt0-instance-route-01`. Browser hiển thị object point clouds và
camera-position trajectory trong XYZ; room polygons/adjacency vẫn ở panel XY.
Room volumes không được dựng vì room artifacts không có elevation bounds đã
xác minh.

Renderer dùng WebGL tích hợp browser, không tải dependency và không gọi CDN.
Nó đọc XYZ/RGB từ geometry JSON đã tạo từ source cloud; cloud nguồn không bị
sửa. `T_W_Copt` được kiểm tra cho mọi frame; thế giới dùng X/Y/Z, Z-up và đơn
vị mét. Viewer không vẽ camera frustum. Trajectory chỉ gồm camera positions
đến revision đang xem; không nội suy hướng camera từ trajectory.

## GUI đã kiểm tra

Đã mở HTML v9 trong browser và kiểm tra trực quan:

| State | Quan sát được |
|---|---|
| Đầu — revision 1/12 | 1 online hypothesis, 1 camera pose, 2 room polygons; membership/room semantics unavailable. |
| Giữa — revision 6/12 | 12 online hypotheses, 6 camera poses, 6 room polygons; RGB/mask/overlay/crop evidence hiện cho frame. |
| Cuối — revision 12/12 | 15 online hypotheses, 12 camera poses, 7 room polygons, 2,695 display points / 6,537 source points. |
| Final postprocess | Stage terminal riêng: 1 final retained object và 5 room nodes. Đây không phải 15 hypotheses được viết ngược thành một object ở các prefix. |

15 hypotheses và 1 retained object là kết quả ở hai stage khác nhau, không phải
độ đo accuracy. Object selection/picking giữ object ID, geometry revision và
lineage; trong final stage đã kiểm tra `cg_5445e2648152dcf7` ở revision 12.
`room_contains` được phân loại theo target type: navigation 120, agent 12,
OccuSG object 0, ConceptGraphs object 0. Không tạo object-room edge.

Đã kiểm tra play/pause, seek đầu–giữa–cuối, seek ngược, orbit, pan, zoom,
fit/reset và giữ nguyên góc khi revision mới tới. Một synthetic XYZ fixture
với điểm trục đã biết xác nhận hướng +X/+Y/+Z, thước mét và picking. Fixture
được ghi rõ là synthetic, không phải mapping run.

### Live-follow test

D1 nguồn đã hoàn tất, nên không có producer mapping thật đang phát event trong
lượt viewer-only này. Để kiểm tra hành vi live-follow, tạo fixture riêng dùng
ba event/revision đã lưu của D1, không tính toán lại mapping:

`artifacts/live_processing_runs/d1_c2_apt0_20261016_v2/validation/d2_xyz_live_follow_fixture_v1/`

Browser theo head từ revision 1 lên 2. Sau đó seek lùi về revision 1, phát
revision 3 và xác nhận viewer vẫn đứng ở lịch sử khi follow tắt. Bật follow
trở lại thì viewer chuyển đến revision 3. Fixture manifest ghi
`pipeline_executed=false`; đây là kiểm thử event replay tổng hợp, không phải
live mapping run mới.

## Causality và provenance

- Objects: `prefix_state_only`.
- Rooms: `prefix_state_only_native_replay_reference`, lấy từ room replay và
  run/live-index provenance; room revision phải khớp frame ID, timestamp và
  source revision đã join.
- Membership: `unavailable_elevation_scope`.
- Room semantics: `unavailable_membership_scope`.
- Prefix admission: unavailable vì producer không xuất contract này.
- Final filter/merge: terminal stage riêng, không backfill prefix.

Trong lần đối chiếu, phát hiện manifest D2 v7 lấy `rooms` causality nhầm từ
object-timeline manifest (`fixed_final_room_map`). D2 builder đã được sửa để
lấy và đối chiếu causality với D1 run manifest/live index, đồng thời ghi hash
và provenance của `room_replay/manifest.json`. V9 là manifest/viewer dùng cho
nghiệm thu; v7, v8 và các artifact trước được giữ nguyên.

Manifest v9 xác thực 202 input hashes và 3 output hashes, không có mismatch.
Input inventory gồm cả builder, viewer template và fixture template.
Room source ghi 12 frames, `delta_replay_equal=true`, cùng capture/config và
native binary hashes. JSON/HTML nguồn giữ nguyên; derived viewer assets nằm
trong `viewer/`.

## Bộ nhớ và hiệu năng hiển thị

- LRU JSON geometry tối đa 6 revisions; renderer giữ một revision GPU buffers
  hiện hành và dispose khi rời trang.
- Tối đa 300 display points/object. Viewer ghi shown/total; điểm hiển thị
  không dùng cho scoring.
- Auto-fit chỉ lúc khởi tạo; revision mới không làm camera nhảy.
- Trong các thao tác GUI v9, browser status đo receive khoảng 23–292 ms và
  render khoảng 71–202 ms. `render` là thời gian phía JS ghi lệnh vẽ, không phải GPU
  completion hoặc end-to-end display latency. RSS/VRAM thực tế chưa được đo.
- Tổng dung lượng các input files được hash trong manifest khoảng 11.3 MB;
  đây không phải mức RAM của viewer.

## Kiểm tra đã chạy

```bash
PYTHONPATH=.:src /usr/bin/python3 -m pytest -q \
  tests/test_d2_xyz_viewer.py tests/test_causal_timeline_viewer.py
node tests/test_xyz_renderer_math.js
node --check scripts/xyz_renderer.js
```

Kết quả: **14 tests passed**; phép thử tọa độ xác nhận world axes, metre
scale và translation từ `T_W_Copt`; JavaScript syntax check pass. GUI đã được
mở và kiểm tra thực tế; screenshot D2 v9 được quan sát trong phiên browser,
nhưng CUA không lưu PNG screenshot vào workspace.

## Lệnh mở trực tiếp ở nghiệm thu D2 v9 (lịch sử)

Tại nghiệm thu D2 v9, server localhost read-only phục vụ run trên cổng 8765.
URL dưới đây xác nhận đúng artifact v9 ở thời điểm đó; không dùng URL chứa tên
version làm workflow người dùng hiện hành:

`http://127.0.0.1:8765/viewer/index_d2_v9.html?live=1`

Nếu server chưa chạy, từ workspace mở một terminal và chạy:

```bash
/usr/bin/python3 -m http.server 8765 --bind 127.0.0.1 \
  --directory "/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/live_processing_runs/d1_c2_apt0_20261016_v2"
```

Sau đó mở URL trên. Synthetic follow test có URL riêng:

`http://127.0.0.1:8765/validation/d2_xyz_live_follow_fixture_v1/viewer/index.html?live=1`

Manifest/hash: `artifacts/live_processing_runs/d1_c2_apt0_20261016_v2/viewer/d2_xyz_manifest_v9.json`.

## Trạng thái các gate

- **D1 live processing:** đã chạy thật một lần trên cache C2; D2 không chạy lại
  producer. Đây là live processing từ cache, không phải live capture.
- **D2 XYZ visualization:** đạt trên browser thật với D1 artifacts; synthetic
  event replay kiểm tra follow/seek behavior.
- **Chưa đạt/chưa đo:** event arrival từ một producer mới trong lượt D2,
  realtime throughput, room 3D volumes, spatial scope/membership, room labels,
  hỗ trợ scene/route khác, crash resume, và producer failure thật trên run dài.

Không có dependency mới. Không thay matcher, thresholds, bảng A, room geometry,
default hoặc baseline.

## Handoff launcher sau nghiệm thu D2 — 2026-10-16

- D2 builder hiện ghi `viewer/d2_viewer_entrypoint.json` bằng atomic replace.
  Pointer trỏ tới D2 manifest và entrypoint, kèm SHA-256 để launcher kiểm tra;
  tên phiên bản HTML không phải tham số người dùng.
- `./run_scene.sh results <d1-run-id>` đọc pointer và in entrypoint hiện hành.
  `./run_scene.sh open <d1-run-id> live --port <port>` lấy đường dẫn từ pointer,
  kiểm tra server loopback có sẵn và không dừng/thay server ngoài run.
- Với D1 run hiện có, pointer trỏ tới `d2_xyz_manifest_v9.json`; `results`
  đã chạy thành công và in đường dẫn/manifest cùng lệnh open. 44 targeted
  tests trong `tests/test_d2_xyz_viewer.py` và `tests/test_scene_launcher.py`
  đạt.
- Browser đã xác nhận trang XYZ tại
  `http://127.0.0.1:8765/viewer/index_d2_v9.html?live=1`. Lệnh `open` được
  thử từ sandbox của agent nhưng sandbox không kết nối được localhost đang
  chạy ở môi trường browser; vì vậy GUI URL được kiểm tra trực tiếp, còn
  end-to-end CLI→server check phải xác nhận từ terminal người dùng.
- D1 run thật và D2 renderer vẫn là hai checkpoint riêng: D1 producer đã
  hoàn tất trước khi D2 v9 được build. Synthetic event replay kiểm tra live
  follow/seek, không thay cho D1+D2 đồng thời trên producer thật. D2 builder
  hiện đòi D1 completed; hỗ trợ dựng entrypoint XYZ trước khi producer bắt
  đầu là prerequisite cho acceptance E, chưa triển khai trong handoff này.

Lệnh workflow không cần tên HTML:

```bash
./run_scene.sh results d1_c2_apt0_20261016_v2
./run_scene.sh open d1_c2_apt0_20261016_v2 live --port 8765
```

Nếu không có server localhost, có thể chạy server read-only từ workspace root
với đúng run viewer (chỉ khi port được xác nhận đang trống):

```bash
/usr/bin/python3 scripts/serve_causal_timeline_viewer.py \
  --root "$PWD/artifacts/live_processing_runs/d1_c2_apt0_20261016_v2" \
  --viewer viewer --host 127.0.0.1 --port 8765
```
