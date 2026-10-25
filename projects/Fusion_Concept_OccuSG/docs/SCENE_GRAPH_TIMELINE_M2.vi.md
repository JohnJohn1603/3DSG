# M2 — Causal scene-graph timeline viewer

## Phạm vi

Viewer đọc chỉ từ timeline M0/M1 của `00802`, không chạy lại mapping,
perception, Qwen hay native backend.  Assets dẫn xuất nằm riêng trong
`artifacts/scene_graph_timeline/m0_m1_00802/timeline_viewer_v3/`; NPZ,
snapshots và room revisions nguồn không bị sửa.

Timeline hiện có 29 prefix revisions.  Object state là
`prefix_state_only`; room state là `prefix_state_only_native_replay_reference`.
Hybrid membership và room semantics là `unavailable`, nên viewer không vẽ
`room_contains` và không điền nhãn ngược từ final map.

## Build và mở

```bash
python3 scripts/build_causal_timeline_viewer.py \
  --timeline artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline \
  --output artifacts/scene_graph_timeline/m0_m1_00802/timeline_viewer_v3 \
  --max-points 300

python3 scripts/serve_causal_timeline_viewer.py \
  --root artifacts/scene_graph_timeline/m0_m1_00802 \
  --viewer timeline_viewer_v3 --port 8765
```

Mở `http://127.0.0.1:8765/timeline_viewer_v3/index.html`.  Dùng HTTP
server vì browser cần `fetch()` các JSON/ảnh; server chỉ đọc thư mục này.

## Tương tác đã kiểm chứng

- Play/Pause, Previous/Next, slider seek, reset view và zoom.
- Frame đầu, giữa và cuối; seek ngược; camera trajectory chỉ tới prefix hiện tại.
- Object click/list: observation IDs, raw class history, detection/frame/pose counts.
- Room click: polygon, room revision và trạng thái semantic unavailable.
- RGB/mask/overlay/crop theo observation hiện tại.
- Request cũ không ghi đè revision mới qua `loadToken`; geometry chỉ tải khi
  revision được chọn và giữ cache trong phiên browser.

## Derived asset contract

`timeline_index.json` giữ frame/revision/timestamp/pose và URL nguồn.
`geometry/rev-*.json` là mẫu tối đa 300 điểm/object, có SHA-256 NPZ nguồn;
đây là visualization asset, không thay thế cloud độ phân giải đầy đủ.
`evidence_index.json` giữ URL và hash các panel nguồn.  `viewer_manifest.json`
ghi source manifest hash, chính sách geometry read-only và LRU cache tối đa 6
revision; mỗi revision chỉ fetch khi được chọn.

## Giới hạn

Prefix admission chưa được reference mapper phơi bày; final filter/merge là
stage riêng chưa nạp vào prefix viewer.  Room revisions là native replay
reference, không phải membership causal của hybrid objects; semantic labels
chưa có.  Viewer chưa phải live/realtime processor và chưa hỗ trợ M2 cho nhiều
scene.  Điểm cloud được sample để hiển thị, không dùng đánh giá geometry.

## Roadmap sau M2 — Replica → causal graph → live viewer

M2 chỉ chứng minh viewer đọc được prefix artifacts của HM3D 00802. Nó không
chứng minh hỗ trợ map mới. Đường đích tiếp theo phải có một scene Replica và
route hợp lệ được khóa riêng; không yêu cầu autonomous navigation.

### M3 — Khóa Replica scene/route và preflight end-to-end

- Chọn một scene Replica có scene asset, calibration, RGB-D, timestamp và pose
  nhất quán; khóa scene ID, route ID, sensor config, frame list và provenance.
- Kiểm tra route có chuyển vị trí và tái quan sát, không chỉ lặp một pose.
- Xuất capture manifest, route manifest và input hashes. Đây vẫn là
  development route, không phải holdout hay bằng chứng generalization.
- Gate đạt khi một frame packet độc lập đi qua schema kiểm tra mà không cần
  GT/annotation; chưa chạy autonomous controller.

### M4 — Causal hybrid membership và room belief

- Tại frame `t`, chỉ đọc object/room state đến `t`; chốt object revision trước
  khi cập nhật membership và room evidence.
- Gán membership bằng voxel support có scope floor/elevation đã xác minh;
  lưu support fraction, outside fraction, runner-up và tie/ambiguous reason.
  Thiếu scope thì giữ `unassigned`, không dùng XY-only xuyên tầng.
- Room belief dùng một persistent object/category làm đơn vị evidence; view
  revisit, alias và duplicate không bỏ phiếu lần hai. Ledger cho phép retract
  khi object đổi class, đổi membership hoặc bị merge.
- Lưu room revision theo prefix, candidate labels, belief, status và evidence
  IDs. Không backfill final membership/semantic label vào prefix.
- Gate gồm prefix/future guard, recorder on/off parity, snapshot/delta seek,
  retract/recompute parity và test boundary/outside/two-floor.

### M5 — End-to-end scene/route runner

- Một entrypoint nhận scene + route + sensor/config và tạo capture → object
  pipeline → OccuSG room state → adapter → unified graph timeline.
- Hỗ trợ rõ hai policy: `cached_replay` đọc input đã lưu và `live_processing`
  xử lý frame packet tuần tự. Cả hai ghi run manifest, stage revisions và
  provenance riêng.
- Không gọi `live_processing` là realtime nếu chưa đo throughput, queue delay
  và p95 frame latency trên cùng phần cứng/config.
- Gate đạt khi chạy được một route Replica đã khóa, export/import giữ IDs,
  transforms, room geometry/adjacency và causal prefix state.

### M6 — Live incremental graph viewer

- Tái sử dụng state schema và viewer M2; nhận snapshot/delta khi M5 phát frame,
  hiển thị object/room revisions, evidence và unavailable states đúng lúc.
- Có play/pause/seek cho replay; live mode chỉ append revision, không sửa quá
  khứ. Nếu queue chậm hơn input, hiển thị backlog và gọi là live processing.
- Chỉ dùng nhãn `realtime` sau khi có benchmark throughput/latency ổn định;
  không suy ra từ cache replay HM3D hoặc một lần chạy nhanh.

### Trạng thái hiện tại

M3–M6 chưa triển khai trong lượt M2 này. HM3D 00802 chỉ là cached replay dùng
để kiểm tra recorder/viewer. Chưa có bằng chứng rằng pipeline hỗ trợ scene mới,
Replica route, causal hybrid membership/room belief end-to-end hoặc realtime.
