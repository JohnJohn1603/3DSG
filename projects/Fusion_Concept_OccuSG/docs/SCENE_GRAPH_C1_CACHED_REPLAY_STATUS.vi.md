# C1 — Cached replay workflow status

Ngày: 2026-10-16  
Phạm vi: chỉ C1 cached replay. Fresh scene/route, live processing, navigation,
object mapping, perception và room-scope changes không được chạy trong lượt này.

## Kết quả

Launcher chạy theo chuỗi:

```text
input manifest có version
  → preflight input/provenance/alignment
  → unified graph builder hiện có
  → viewer builder hiện có
  → run manifest + lệnh mở viewer
```

Subcommand mới là `cached-replay`; không hardcode scene/frame/artifact vào
launcher. Input run cụ thể nằm trong
`configs/cached_replay/hm3d_00802_m0_m1_a1_v1.json`. Một run mới luôn cần
output directory mới; output cũ không bị ghi đè.

## Run đã thực sự chạy

- Run ID: `cached_replay_20261016T034544Z_e9d5f4`
- Mode/status: `cached_replay` / `completed`
- Scene/sequence: `00802-wcojb4TFT35` /
  `hm3d-v02-00802-main-route-01`
- Input: object timeline M0/M1 cùng causal hybrid timeline đã khóa; room
  revisions được align theo frame/timestamp/state reference, không buộc số
  room revision trùng object revision.
- Kết quả: 29 unified revisions; prefix cuối có 33 objects, 5 rooms, 6 edges.
  Membership unavailable ở 29/29 revisions do `unavailable_elevation_scope`;
  room semantics unavailable ở 29/29; không có `room_contains` edge.
- Causality: objects `prefix_state_only`; rooms
  `prefix_state_only_native_replay_reference`; membership
  `unavailable_elevation_scope`; room semantics
  `no_eligible_membership_evidence`; `final_postprocess_included=false`.
- Tổng runtime 4.983 s: preflight 1.512 s, unified builder 0.914 s, viewer
  builder 2.291 s, output validation 0.202 s.
- Run manifest kiểm tra đủ 95 output files: không thiếu file, hash/size đều
  khớp; tổng 25,820,122 bytes.

Artifacts:

- Run manifest: `artifacts/cached_replays/cached_replay_20261016T034544Z_e9d5f4/run_manifest.json`
- Unified graph: `artifacts/cached_replays/cached_replay_20261016T034544Z_e9d5f4/unified/`
- Viewer: `artifacts/cached_replays/cached_replay_20261016T034544Z_e9d5f4/viewer/index.html`

## Tương đương mốc B và kiểm tra giao diện

Với cùng input hashes, 29 revision identities và JSON snapshots của unified
graph khớp chính xác mốc B hiện có. Không tạo lại associations, object IDs,
geometry hay room state khác.

Viewer đã được mở trong browser và kiểm tra frame đầu, giữa và cuối; seek
ngược từ cuối về đầu; play rồi pause; RGB/mask evidence; danh sách room/object;
variant selector; scope-unavailable state; không có room membership edges.
Viewer giữ revision hiện tại và hiện `Room_N` tách biệt khỏi room semantic
label. Server dùng localhost `127.0.0.1`, port `8765`.

## Lệnh đã kiểm chứng

Tạo run mới từ config đã khóa:

```bash
./run_scene.sh cached-replay --config configs/cached_replay/hm3d_00802_m0_m1_a1_v1.json
```

Xem trạng thái run:

```bash
./run_scene.sh results cached_replay_20261016T034544Z_e9d5f4
```

Mở viewer run đã tạo:

```bash
./run_scene.sh open cached_replay_20261016T034544Z_e9d5f4 timeline --port 8765
```

Nếu port đã bận, chọn port khác bằng `--port`; launcher không dừng process
khác. URL đã mở và kiểm tra là:

`http://127.0.0.1:8765/artifacts/cached_replays/cached_replay_20261016T034544Z_e9d5f4/viewer/index.html`

## Kiểm thử và ranh giới trạng thái

Đã chạy:

```bash
pytest -q tests/test_cached_replay.py tests/test_unified_incremental_timeline.py tests/test_causal_timeline_viewer.py
```

Kết quả: 14 passed. Nhóm test bao gồm happy path và parity, input/hash/scene/
revision/alignment mismatch, output path đã tồn tại, builder failure không tạo
completed run, unavailable scope, launcher help, port bận và cách truyền viewer
subdirectory vào server.

C1 đạt cho cached inputs hợp lệ đã cấu hình. Đây không phải fresh scene/route
workflow hoặc live processing. Không đo throughput/latency realtime. A vẫn một
phần: scope elevation trên room polygons 00802 chưa được xác minh. B integration
đạt; object–room membership và room labels trên dữ liệu thật vẫn unavailable.
Mốc C tổng thể, D và E vẫn pending. Bước kế tiếp nhỏ nhất theo kế hoạch chính
là M3 Replica scene/route preflight; không nằm trong lượt này.
