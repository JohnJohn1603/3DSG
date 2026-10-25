# Đóng checkpoint T3 viewer và proposal replay 13 keyframes v8

Ngày: 2026-10-19. Tài liệu này giữ handoff T3 và proposal ban đầu. Proposal
được duyệt một lượt 180 phút; kết quả thực chạy, integrity và blocker được ghi
riêng tại
[`V8_ALL13_ORDERED_REPLAY_ATTEMPT_20261019.vi.md`](V8_ALL13_ORDERED_REPLAY_ATTEMPT_20261019.vi.md).

## 1. Đóng checkpoint T3 viewer

GUI acceptance được ghi là **user-verified** theo báo cáo kiểm tra trực tiếp
của người dùng. Đây không phải browser-agent verification và không phải room
label accuracy.

Người dùng xác nhận đã kiểm tra đúng sáu thao tác:

1. Đổi A/B: B hiện assumed/experimental cùng declaration provenance; A không
   hiện membership của B.
2. Chọn semantic variant tường minh.
3. Mở frame index 11, `hm3d-00802-main-0011`.
4. Mở frame index 12, `hm3d-00802-main-0012`.
5. Mở frame index 28, `hm3d-00802-main-0028`, rồi seek ngược giữa các prefix.
6. Xác nhận chair chỉ tạo một category contribution dù có nhiều chair objects;
   phân biệt số room đang có nhãn với số commit/relabel transitions.

Record nằm ở
[`viewer_acceptance.json`](../artifacts/t3_room_belief_timeline_viewer_00802_v4/viewer_acceptance.json).
Đã loại bỏ hai khóa GUI status mâu thuẫn trong JSON cũ và đồng bộ hash trong
`viewer_artifact_manifest.json`. Hash hiện tại:

- `viewer_acceptance.json`: `972a29b62b59f9ab55f02c8a2a318cf5644309c29a16b5442261244f36246c2b`
- `viewer_manifest.json`: `737f426c1d20220217cdd11e26f56640f3923bcee26c5bb5befd2f9f0ffe7c62`
- `viewer_artifact_manifest.json`: `37075d7bc437281d50711fc1defc0a22efdc973ab35adc0d5f57ca2ba488c682`

Checkpoint T3 viewer đã đóng. Không sửa object/room/A-table artifacts gốc và
không chạy lại pipeline.

## 2. Bằng chứng read-only về v8

Nguồn là
[`camera_live_t1_1_20261018_v8`](../artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/),
route `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`, scene
`00802-wcojb4TFT35`, sequence
`hm3d-v02-00802-t1-1-turn-continue-v1_smooth_smooth_t1_1_hm3d00802_turn_continue_20261018_v2`.

Run giữ 113 camera frames và 13 native 640×480 RGB-D keyframes, có các frame
indices:

`[0, 26, 33, 40, 50, 61, 68, 75, 82, 89, 96, 103, 110]`.

Inventory/hash:

- [`keyframe_inventory.json`](../artifacts/diagnostics/camera_live_v8_keyframe_inventory_20261018/keyframe_inventory.json)
  SHA-256 `20618ad07686ddbff586fb93ba7d25402fed77cf146f43bb1d3ff711452a697d`.
- [`pilot_acceptance_manifest.json`](../artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/pilot_acceptance_manifest.json)
  SHA-256 `67e10ed9adb021eb19820f6710bfe97fea86e42f107a0d4609770cab945e6fef`.
- [`pilot_integrity_manifest.json`](../artifacts/live_processing_runs/camera_live_t1_1_20261018_v8/pilot_integrity_manifest.json)
  SHA-256 `185c2cb8ef246fba9cfe1b5d1983a56d1b22df5084b619c6963b132d377c0cc4`;
  572 file entries, không có file thiếu. 571 hash còn khớp; sai khác duy nhất
  là `logs/viewer_server.log`, log vận hành có thể thay đổi.

Mỗi keyframe có bốn file raw: RGB, depth metres, valid mask và metadata.
Inventory ghi resolution, calibration/intrinsics, timestamps và hash cho từng
file. Không cần capture lại để xử lý đủ 13 keyframes.

### Perception có sẵn

`000000` và `000110` đều có P2, P3, OpenCLIP và joined packet ở trạng thái
completed; mỗi frame có 5 P2 detections, 5 P3 rows và 5 feature rows. Source
hashes trong các output trùng với bốn file raw tương ứng. Output file hashes
được khóa trong integrity manifest. P2 model/profile và OpenCLIP checkpoint /
feature contract có provenance hash trong acceptance manifest; vectors có
shape/finite/normalization checks trong output.

11 frame còn lại — `000026`, `000033`, `000040`, `000050`, `000061`, `000068`,
`000075`, `000082`, `000089`, `000096`, `000103` — chỉ có raw RGB-D; v8 ghi
chúng là `superseded_before_processing` và không có perception outputs.

Giới hạn provenance cần giữ rõ: P3 output nêu geometry method và profile ID,
nhưng manifest không có hash riêng cho P3 producer source. Ba source hashes
của camera runner, Habitat runner và startup contract hiện khác hash ghi trong
v8; hash `perception_feature_sessions.py` và mapper worker còn khớp. Vì vậy,
preflight của lượt đề xuất phải xác nhận lại schema, profile/config, input/output
hashes và consumer adapters; không được coi code-source parity P3 đã chứng minh.

`run_manifest.json` của v8 có `run_id` là route ID và bootstrap còn ở trạng thái
`bootstrapping`; pilot acceptance sidecar ghi rõ lỗi bookkeeping này. Lượt mới
phải lấy pilot ID, sequence và stage evidence từ manifest có schema tương ứng,
không suy trạng thái từ `run_manifest.json` cũ.

## 3. Proposal: ordered replay đủ 13 keyframes

### Phạm vi dữ liệu và cách xử lý

1. Preflight cả 13 bộ raw hashes, shape/resolution, calibration, frame IDs,
   timestamps và scene/sequence. Không ghi vào v8.
2. Reuse perception artifacts của `000000` và `000110` chỉ khi hashes, schema,
   source IDs, P2 masks, P3 rows, feature coverage/model contract và adapter
   validation cùng đạt. Nếu gate reuse không đạt, dừng trước producer; không tự
   tính lại hai endpoint trong lượt này.
3. Chạy đúng một ordered perception pass cho 11 keyframes còn thiếu bằng các
   session/provider hiện có: một P2 session, một P3 geometry session và một
   OpenCLIP session. Model P2/OpenCLIP mỗi loại load một lần; empty detections
   là output hợp lệ, còn lỗi stage là failed riêng. Không lọc P2 observations
   theo P3 accepted.
4. Ghép thành đúng 13 current-frame packets theo frame index và full IDs.
   Cả hai mapper nhận cùng packet set/order/source hashes.
5. Mở ConceptGraphs và OccuSG từ state rỗng, một session cho mỗi nhánh, mỗi
   nhánh nhận 13 updates. Không dùng hai mapper revisions của v8. Ghi 13 prefix
   revisions/paired commits; ConceptGraphs terminal postprocess và OccuSG
   terminal flush được ghi riêng sau frame cuối.
6. Chỉ dùng worker live/current-frame adapters để prefix không hydrate poses,
   classes hay detections tương lai. Existing T2-D worker đã có `--live`, nhưng
   coordinator T2-D hiện khóa đúng ba T2-B2 packets và source completed-C2
   manifest; cần mở rộng giao diện input có kiểm soát cho ordered 13-frame
   packet manifest và tách config-source của mapper khỏi capture/sequence-source.
   Tái sử dụng session workers, packet adapters, atomic branch publication và
   pair join hiện có; không sao chép thuật toán.
7. Room geometry được tạo lại từ đúng 13 frames. Membership và room semantics
   giữ `unavailable_elevation_scope` / `unavailable_membership_scope` trừ khi có
   declaration riêng cho chính run này. Declaration B của main29 không áp dụng;
   proposal này không bật assumed scope và không chạy A tables.

### Command/interface dự kiến

Lệnh sau là **đề xuất, chưa triển khai và chưa chạy**:

```bash
python3 scripts/run_ordered_keyframe_replay.py \
  --source-run artifacts/live_processing_runs/camera_live_t1_1_20261018_v8 \
  --mapper-config-source artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2 \
  --run-id hm3d00802_t1_1_all13_replay_v1
```

`mapper-config-source` chỉ cung cấp interpreter/binary/config lock đã nghiệm
thu cùng scene; không cung cấp capture, object state, room state hoặc sequence
frames. Preflight phải xác minh config compatibility trước model load. Output
được tạo ở run directory mới. Nếu C2 source không chứng minh cùng thuật toán,
thresholds, seed/config và native resources, dừng trước xử lý thay vì tự lấy
config khác.

### Producer counts và ngân sách đề xuất

Đề xuất duyệt một lượt với trần **180 phút tổng**:

- Engineering tối đa 150 phút: packet manifest/preflight, adapter nối 13 frames
  vào perception sessions, mở rộng T2-D coordinator để nhận N packets, và
  model-free tests cho reuse/hash/order, equal branch frame sets, prefix-only,
  failure/terminal separation.
- Một lần chạy dữ liệu tối đa 30 phút; runtime dự kiến khoảng 3–8 phút dựa trên
  hai room updates v8 đo được 7.14 s và 9.01 s, nhưng đây chỉ là ước lượng.
  Nếu vượt 30 phút hoặc lỗi sau mapper mutation, giữ partial outputs và dừng;
  không retry.

Số lượt được đề xuất:

- Habitat capture: **0**.
- Integrated ordered replay: **1**, không retry.
- P2: một worker/model load, 11 frame updates; reuse hai frame endpoint.
- P3: một geometry session, 11 frame updates; reuse hai frame endpoint nếu
  preflight đạt.
- OpenCLIP: một worker/model load, 11 frame updates; reuse hai frame endpoint
  và feature contract nếu preflight đạt.
- ConceptGraphs: **1** session mới từ state rỗng, 13 updates, 1 terminal
  postprocess.
- OccuSG: **1** session mới từ state rỗng, 13 updates, 1 terminal flush.
- Unified output: tối đa 13 prefix commits. Không chạy Qwen, C2 capture, D1
  camera-live route, native replay riêng, A-table scoring hay room-belief pass.

### Acceptance và giới hạn

- Mỗi frame trong danh sách 13 xuất hiện đúng một lần trong cả hai branch, cùng
  order/identity/source hashes.
- 13 prefix state được publish riêng; không dùng v8 final/terminal state để
  backfill, không tính terminal như frame update.
- Mọi output mới có manifest, stage counts, source/config/model hashes, timing,
  failure classification và immutable output hashes.
- Membership/room semantics vẫn unavailable theo scope hiện có. Không tuyên bố
  room label quality hoặc quality improvement từ object/detection counts.
- Đây là replay cache đầy đủ trên 13 persisted keyframes, không phải capture
  live, không tái tạo đủ 113 camera frames và không phải realtime.

Chưa code hoặc chạy proposal này. Cần duyệt cả trần 180 phút và số lượt trên
trước khi bắt đầu.
