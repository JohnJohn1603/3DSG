# Đề xuất T2-C — OccuSG persistent session

Ngày: 2026-10-18  
Phạm vi: tách lifecycle OccuSG từ các API hiện có; chưa chạy producer mới,
capture, detector, encoder, Qwen hoặc room-belief scoring.

## Kết quả kiểm tra call path

### Core đã có khả năng incremental ở mức stage

- `src/standalone_sg/pipeline/builder.py::SceneGraphBuilder.update(packet)`
  nhận từng `FramePacket`, gọi mapping/region stage được inject, cập nhật
  state và commit snapshot/delta theo revision. `flush(timestamp_s)` là
  terminal room-maintenance/flush; `close()` đóng các stage một lần.
  Builder đã reject frame sai thứ tự qua `SequenceScheduler` trước stage call;
  failure sau mutation đặt `failed/replay_required` và không giả rollback.
- `src/standalone_sg/mapping/native.py::OctoMapWorker` khởi chạy một
  subprocess `--serve` trong constructor. `integrate()` gửi scan từng request,
  `project()` đọc accumulated OcTree bằng `partial_update` sau request đầu;
  worker giữ revision và state cho tới `close()`. Timeout, broken pipe và
  schema lỗi poison worker rồi terminate child.
- `src/standalone_sg/mapping/native_pipeline.py::NativePointCloudMappingStage`
  giữ `OctoMapWorker` và đổi đúng mode `full_refresh` → `partial_update` theo
  `records`; `NativeRGBDMappingStage` chỉ bọc cùng worker, không tạo worker
  mới ở `build_grid()`.
- `src/standalone_sg/regions/dude_client.py::DuDeWorkerClient` giữ một worker
  JSON-line sống xuyên các `update_regions()`; `reset()` và idempotent
  `close()` đã có. `RegionUpdateProcessor` giữ guard/tracker state và chỉ
  commit accepted regions; rejected update trả stale snapshot.

### Hạn chế nằm ở wrapper hiện tại

`scripts/run_habitat_native_replay.py::run_replay` gọi `_load_packets()` và
đọc toàn bộ `capture_manifest.json`/P3 cache trước khi tạo worker/builder.
`_run_once()` sau đó mới lặp qua toàn bộ `packets`, gọi `builder.update()` rồi
`builder.flush()`. Wrapper còn yêu cầu P3 manifest có cùng capture hash và
`frame_count`, và mặc định chạy fresh determinism pass thứ hai. Vì vậy wrapper
đang batch/full-manifest; điều này không chứng minh core native thiếu
incremental capability.

`integration_v2/fresh_replica.py` và `integration_v2/live_processing.py` cũng
khởi chạy wrapper theo subprocess với completed C2/capture manifests. Đây là
chỗ cần adapter/session entrypoint; không cần đổi protocol native theo bằng
chứng cứ hiện tại.

## Thiết kế implementation hữu hạn

Nếu được duyệt, dùng đúng một implementation core cho batch và session:

1. Thêm `OccuSGMappingSession.open(...)`, `update_frame(packet)`,
   `finalize(timestamp_s)`, `close()` dưới một module session mỏng, khởi tạo
   `NativeRGBDMappingStage`/`DudeM5RegionStage`/`SceneGraphBuilder` một lần.
2. Tách packet iterator khỏi `_load_packets()`: session nhận `FramePacket`
   hiện tại đã validate scene/sequence, frame ID/index/timestamp và provenance;
   không cần future manifest/class list.
3. `update_frame` gọi đúng `builder.update(packet)` và phát snapshot/delta
   prefix; `finalize` chỉ gọi terminal `builder.flush()` rồi native close/export.
   Không chuyển occupancy, projection, segmentation, geometry, matcher,
   thresholds hay scope policy.
4. Duplicate/stale được reject trước worker/builder mutation. Lỗi sau
   mutation đặt `failed/replay_required`; cleanup chỉ worker thuộc session.
   `close()` an toàn khi success, failure hoặc gọi lại.
5. Prefix room revisions lấy từ snapshot/delta thật; đối chiếu geometry,
   node lifecycle, edges và revision alignment, không backfill final snapshot.

### Regression budget và điểm dừng đề xuất

Đề xuất trần **150 phút engineering + tối đa một native verification run**:

- 30 phút: API contract/read-only preflight và test packet lifecycle.
- 75 phút: implementation session adapter, batch gọi cùng core, lifecycle,
  failure/cleanup và provenance.
- 45 phút: targeted tests + một parity run trên packet/cache đã khóa; không
  mở pass thứ hai nếu parity fail.

Native runtime nằm trong 45 phút cuối và được ghi riêng; không retry tự động.
Nếu phát hiện cần đổi wire protocol, rebuild binary hoặc dependency mới, dừng
trước bước đó để xin duyệt riêng. Nếu parity không đạt trong run duy nhất,
giữ partial artifacts và báo diff/blocker, không mở thêm verification.

## Acceptance gates

- Worker/builder instances sống xuyên nhiều packet; duplicate/stale không tiêu
  thụ RNG hoặc mutate.
- Batch và session dùng cùng update implementation; không đọc future manifest.
- Prefix snapshot/delta room revisions khớp geometry/lifecycle; seek/reload
  không nhận final state.
- Final flush/export là stage terminal riêng.
- Cancellation/failure đóng đúng child PIDs; `membership` và `room_semantics`
  vẫn `unavailable` nếu scope chưa đủ; không bật assumed-single-level.

## Kết quả thực hiện sau khi được duyệt

Đã triển khai session adapter và chuyển `_run_once()` của native replay wrapper
sang cùng `SceneGraphBuilder.update()`/`flush()` implementation qua
`OccuSGSession`. Không đổi native wire protocol, binary, dependency, occupancy,
projection, region segmentation, geometry, matcher, thresholds hoặc scope.

Các file chính:

- `integration_v2/occusg_session.py`: lifecycle, packet ordering, duplicate/
  stale rejection trước builder/native mutation, failed/replay-required state,
  terminal finalize và idempotent close.
- `scripts/run_habitat_native_replay.py`: batch wrapper tạo session một lần,
  gửi từng packet qua `update_frame`, gọi `finalize` một lần rồi close.
- `tests/test_occusg_session.py`: lifecycle, duplicate/stale, cross-sequence,
  terminal finalize, failure và cleanup.

Targeted tests trước cleanup: **34 passed** (`test_occusg_session`, `test_m9_builder`,
`test_m4_stateful_worker`, `test_m4_worker_lifecycle`). Sau cleanup lifecycle,
fixture suite đã chạy thêm **34 passed** (`test_occusg_session`, `test_m9_builder`),
không chạy lại native mapping.

### Một native verification run

Chỉ chạy một run trên cache apt_3 đã khóa, không chạy capture/perception:

```bash
PYTHONPATH=.:src MPLCONFIGDIR=/tmp/t2c-mpl \
/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/.venv-perception/bin/python -B \
scripts/run_habitat_native_replay.py \
  --capture artifacts/conceptgraphs_hybrid_runs/c2_replica_apt3_navmesh_v1/capture \
  --detection-3d artifacts/conceptgraphs_hybrid_runs/c2_replica_apt3_navmesh_v1/detection_3d/detection_3d.jsonl \
  --detection-3d-manifest artifacts/conceptgraphs_hybrid_runs/c2_replica_apt3_navmesh_v1/detection_3d/detection_3d_manifest.json \
  --resource-manifest locks/external_resources.json \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --m2-m3-binary /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/build/native-m3/occupancy/standalone_sg_octomap_fixture_runner \
  --dude-binary /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/build/native-m4-dude/occusg_dude_worker \
  --object-backend source \
  --output artifacts/t2_c_native_session_verification_20261018/replay \
  --grid-output artifacts/t2_c_native_session_verification_20261018/grid_trace \
  --no-determinism-check
```

Run manifest/sidecars:

- `artifacts/t2_c_native_session_verification_20261018/session_manifest.json`
- `artifacts/t2_c_native_session_verification_20261018/session_parity_report.json`

Kết quả: 12 frames, final revision 13, 137 nodes, 893 edges, 4 rooms.
Có **12 camera-frame revisions + 1 terminal flush revision = 13 graph
revisions**; revision 13 không phải camera-frame update thứ 13.
`pipeline_status=pass`, không dangling/self/conflicting room-owner edges.
Snapshot, delta log, trajectory và object association lineage khớp byte với
reference replay; frame diagnostics khớp về semantics sau khi bỏ các trường
runtime đo theo wall-clock. Runtime candidate `36.815 s`, peak RSS manifest
`507.66 MiB`; lần determinism thứ hai không chạy theo policy đã khóa.

Từ chính `delta_log.json` của session run, đã tạo
`prefix_room_revisions/manifest.json` và 13 snapshot revisions bằng replay
tuần tự từng delta. Đây là persistence của prefix state thật, không điền
ngược từ final snapshot; manifest ghi source hash và `future_observations_used`
false. Việc dựng sidecar này không chạy native backend thêm lần nào.

### Trạng thái T2-C

T2-C đạt ở mức session lifecycle và một native parity run. Cleanup lifecycle
đã được harden: stage close tiếp theo vẫn được thử khi stage trước lỗi,
partial initialization được dọn theo resource đã tạo, cancellation giữ lại
tín hiệu sau cleanup pass, và close lặp lại retry stage chưa đóng thành công.
Wrapper vẫn còn
đọc full capture/P3 manifest trước khi phát packet vào session; đây là giới
hạn orchestration còn lại để nối producer streaming, không phải giới hạn của
`OctoMapWorker`/`DuDeWorkerClient` core. Perception session, coordinator live,
membership và room semantics chưa được triển khai; vẫn `unavailable`.

## Checkpoint kế tiếp được đề xuất: T2-B — perception session

Phạm vi nhỏ nhất là tách lifecycle quanh các hàm perception hiện có trong
`scripts/run_conceptgraphs_cached_hybrid.py` và adapter tương ứng: `open()`
tải checkpoint cùng tokenizer/encoder một lần; `update_frame()` nhận đúng
một admitted keyframe, dùng preprocessing/feature contract hiện tại và trả
packet perception có provenance; `finalize()` chỉ flush terminal export;
`close()` giải phóng worker/model một lần. Batch runner sẽ gọi cùng session
API, không copy detector/encoder logic. Không chạy Qwen, không đổi model,
thresholds, matcher, A tables hay scope policy trong checkpoint này.

Acceptance: duplicate/stale keyframe bị reject trước inference; model/process
sống xuyên ít nhất hai keyframes; output packet/hash và class registry resolve
được trong cùng run; failure-after-inference đặt `replay_required`; close/cancel
không làm rò process. Regression dùng packet cache hiện có, không capture mới.
Ngân sách engineering đề xuất **120 phút**, không gồm runtime model; tối đa
một smoke regression đã khóa. Nếu perception hiện tại chỉ là subprocess
full-manifest hoặc cần đổi dependency, dừng trước thay đổi đó để xin duyệt
riêng.

Kết quả thực hiện T2-B hiện tại được ghi tại
`docs/T2_B_PERCEPTION_SESSION_REPORT.vi.md`: P2 session đã pass smoke trên
3 frame, còn P3/image/text encoder session vẫn pending và chưa được gọi là
toàn bộ perception đã hoàn tất.
