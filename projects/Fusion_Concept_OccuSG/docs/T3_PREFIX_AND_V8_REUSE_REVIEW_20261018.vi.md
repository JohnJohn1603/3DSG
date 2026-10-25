# Kiểm tra dữ liệu trước T3 và proposal declaration B

Ngày: 2026-10-18. Đây là kiểm tra artifacts hiện có; không chạy perception, mapper, Qwen hoặc T3.

## 1. Main29 640×480: có prefix, không chỉ final artifacts

Nguồn: `artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2/`.

- `object_timeline/timeline/manifest.json` ghi `frame_count=29`, `revision_count=29`, `objects=prefix_state_only`; có 29 snapshot từ `rev-000001` tới `rev-000029`. Các snapshot ánh xạ frame 0–28, và cả 29 ghi `future_observations_used=false`.
- Có 29 room revision snapshots từ `room_revisions/rev-000000.json` tới `rev-000028.json`, được tham chiếu theo từng frame. Manifest ghi `rooms=prefix_state_only_native_replay_reference`; nguồn là `replay/delta_log.json`. Replay manifest ghi 29 frames, `delta_replay_equal=true` và `fresh_replay_equal=true`.
- Đối chiếu từng object snapshot với `rooms.state_ref`: cả 29 đều trỏ đúng room revision theo frame index. Kiểm tra 29 room files cho thấy agent frame indices không vượt quá khóa revision tương ứng (0–28); không thấy agent/future frame đi trước trong room snapshots.
- Đây là prefix room geometry/lifecycle từ native replay reference, không phải final room snapshot được chép ngược vào mọi frame. Nó cũng không chứng minh hybrid object membership: manifest ghi `membership=not_causal_for_hybrid_objects`, `room_semantics=unavailable`; `c2_run_manifest.json` giữ `membership_policy=unavailable_elevation_scope`.
- Vì vậy T3 có thể dùng object và room prefixes đúng frame để kiểm thử thời gian/evidence. Trong verified-only, membership edges và room belief từ object evidence vẫn phải unavailable cho tới khi spatial scope đạt gate.

Hashes khóa nguồn:

| Nguồn | SHA-256 |
|---|---|
| C2 run manifest | `48e62b2f4031061d77c69fd603269e78707174e600d5ad89f7dc5a0b0a6c4753` |
| Capture manifest | `2df7be9cab33213cf11c322778586e53a8e78209375f380ac3213ca7e0d0cbb8` |
| Object timeline manifest | `cb2c5148072918897465f82e6df864ab1bd15ef1aff04d7ba3a7de211117cc1f` |
| Tập 29 object snapshots (hash set) | `1425cb1963f36e9e3f0214ffecc8aaab699fe7c555786d21941e2511d4221e80` |
| Tập 29 room revision snapshots (hash set) | `70a30ea1941f090ad671cd40a6bb8b6166ea95e03939132a3284d1b528736ad4` |
| Native replay manifest | `bccea233c10405c2dd498ea9bd893b4887e3589823afc88a108c25bcff859a1d` |

## 2. A đối chứng và declaration B để duyệt

T3 giữ **A verified-only** làm đối chứng bắt buộc. Nó dùng đúng 29 object prefixes và 29 room revisions ở trên. Kết quả dự kiến theo gate hiện tại: semantic consensus/admission có thể được tính theo prefix; membership là `unavailable_elevation_scope`, không có `room_contains`, room evidence không được đưa vào belief và không ép nhãn.

Declaration dưới đây chỉ là **đề xuất giả định experimental**, `approval: pending`. Nó không xác nhận spatial scope đúng và không được dùng ngoài cache/frame được nêu:

Runner hiện chỉ nhận verified scope contract; B chưa được chạy và sẽ cần một nhánh contract riêng có provenance/label `assumed_single_level_demo` sau khi được duyệt. Không ghi declaration này thành `verified` để lách gate.

```yaml
schema: assumed-spatial-scope-declaration/v1
declaration_id: hm3d00802_main29_640_assumed_single_level_demo_v1
status: proposed_assumption_requires_user_approval
mode: assumed_single_level_demo
dataset: hm3d_minival_v0.2
scene_id: 00802-wcojb4TFT35
run_id: hm3d00802_main29_640_quality_v1_attempt2
sequence_id: hm3d-v02-00802-main-route-01
frame_indices_inclusive: [0, 28]
object_prefix:
  timeline_manifest_sha256: cb2c5148072918897465f82e6df864ab1bd15ef1aff04d7ba3a7de211117cc1f
  snapshot_revisions_inclusive: [1, 29]
  snapshot_set_sha256: 1425cb1963f36e9e3f0214ffecc8aaab699fe7c555786d21941e2511d4221e80
room_prefix:
  source: replay/delta_log.json
  room_revision_keys_inclusive: [0, 28]
  revision_set_sha256: 70a30ea1941f090ad671cd40a6bb8b6166ea95e03939132a3284d1b528736ad4
assumption:
  statement: >-
    Chỉ cho experiment này, giả định object clouds và room polygons trong
    các prefix đã khóa biểu diễn một spatial projection/elevation scope
    tương thích để thử greedy XY voxel membership.
  evidence_status: assumed_not_verified
  approval: pending
limits:
  applies_to_only_this_run_and_frames: true
  verified_single_floor_claim: false
  multi_floor_claim: false
  reusable_for_other_routes_or_runs: false
  quality_or_accuracy_claim: false
  will_modify_A_tables: false
  will_modify_thresholds: false
  will_change_default: false
```

Nếu được duyệt, B chỉ chạy song song với A trên cùng evidence và được đánh dấu experimental/assumed trong artifact, manifest và viewer. Không đổi A, threshold hoặc default. Không có room-function GT đã khớp với các polygon hiện tại nên không báo accuracy/F1; không suy annotation từ prediction.

## 3. Kiểm kê 13 raw RGB-D keyframes T1.1/v8

Run: `camera_live_t1_1_20261018_v8`; route `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`. Có 113 camera frames nhưng chỉ 13 selected keyframes được persist thành RGB-D; inventory ngoài run chứa full frame IDs, timestamp, đường dẫn và SHA-256 cho cả bốn file mỗi keyframe (`rgb.npy`, `depth_m.npy`, `valid_mask.npy`, `metadata.json`):

`artifacts/diagnostics/camera_live_v8_keyframe_inventory_20261018/keyframe_inventory.json`

SHA-256 inventory: `20618ad07686ddbff586fb93ba7d25402fed77cf146f43bb1d3ff711452a697d`. Tất cả 52 raw-file hashes khớp `artifacts_sha256.json` trong v8; v8 không bị ghi thêm.

Frame indices và xử lý trong v8:

| Frame index | Trạng thái v8 |
|---|---|
| 000000 | P2, P3, OpenCLIP; ConceptGraphs + OccuSG; unified revision 1 |
| 000026, 000033, 000040, 000050, 000061, 000068, 000075, 000082, 000089, 000096, 000103 | RGB-D persisted; superseded trước processing; không có perception/mapper update |
| 000110 | P2, P3, OpenCLIP; ConceptGraphs + OccuSG; unified revision 2 |

Mỗi dòng tương ứng frame ID đầy đủ `...-cam-<index>` trong inventory. Trong v8, các nhánh mapper chỉ có state từ frame 000000 và 000110; không dùng hai state này làm tiền tố cho lượt đủ 13 frame.

### Tái sử dụng sau T3 — chưa chạy

Có thể thực hiện một ordered cached-input replay, state mapper mới từ rỗng, trên **đúng 13 raw RGB-D keyframes hiện có** theo frame index; không cần capture lại. Dispatch mỗi frame một lần, không latest-wins/drop, ghi revisions và source hashes. Đây là replay từ cache, không phải live camera hay realtime; cũng không thể tái tạo 113 RGB-D frames vì chỉ 13 keyframes được lưu.

Hai keyframes 000000/000110 có perception outputs trong v8. Có thể reuse chúng chỉ sau khi xác minh source hashes, effective P2/P3/OpenCLIP config và schema; 11 keyframes còn lại cần perception từ raw RGB-D. Mappers phải bắt đầu state rỗng rồi nhận đủ 13 theo thứ tự; không tiếp tục mapper state chỉ có hai frame của v8. Nếu outputs cũ không qua hash/config validation, dừng hoặc xin duyệt một pass perception đồng nhất; không bỏ frame âm thầm.

Declaration B ở mục 2 chỉ áp dụng `hm3d00802_main29_640_quality_v1_attempt2`, frames 0–28. Nó **không áp dụng** cho run/sequence T1.1/v8; membership và room semantics của 13 keyframes vẫn unavailable nếu chưa có declaration riêng được duyệt cho chính run đó.

## 4. Timing v8: route startup khác capture

Số `12.80883 s` mà handoff trước gọi là capture wall time thực ra là `summary.wall_clock_runtime_s` của route runner, tính từ lúc runner bắt đầu tới khi hoàn tất. Không dùng nó như thời lượng capture-only.

- `events.jsonl` ghi route `run_started` lúc `09:23:11.904055Z`, route preflight pass lúc `09:23:20.955259Z`, và camera frame đầu được ghi lúc `09:23:21.027239Z` (capture timestamp trong packet là `09:23:20.994844Z`). Run start → route preflight pass là **9.051 s**.
- P2 bắt đầu tại monotonic `3907.244892900`; first frame capture tại `3907.048130998`. Pilot manifest đo trực tiếp first-frame-capture → P2 start là **0.196762 s**.
- Route start → P2 start suy ra khoảng **9.288 s** khi đổi wall event sang cùng host monotonic domain; phần lớn nằm trước frame camera đầu, gồm route preflight/khởi tạo. Đây không phải capture-start → perception-start.
- v8 không giữ chính xác `capture_started_monotonic_s`. Theo thứ tự stage trong runner, capture start nằm sau preflight pass và trước capture timestamp của frame đầu; do đó capture-start → P2 được chặn trong khoảng **0.197–0.236 s**, không phải 9.352 s. 113 frame capture timestamps span **3.642 s** từ đầu tới cuối; `capture_finished_monotonic_s=3910.701816` nằm khoảng **11.5 ms** sau timestamp frame cuối. Capture-stage elapsed chính xác thiếu start timestamp; không benchmark lại.
