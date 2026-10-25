# T4 room-belief wiring — model-free handoff

Ngày: 2026-10-19. Kết quả lượt này là wiring và model-free verification;
camera-live pilot chưa chạy.

## Trạng thái theo phạm vi

| Phần | Kết quả |
|---|---|
| T3 core reuse | `RoomBeliefCommitSession` giữ ledger state xuyên unified commits; gọi lại T3/RB1 consensus, ledger, category cap, aliases và bốn A tables đã pin. Không chạy lại T3 prefix replay. |
| Admission | Runner lấy `obj_min_detections=3` từ `base_mapping_config()`; `run_t2_d_cached_packet_worker.py` khởi tạo ConceptGraphs bằng cùng hàm/cấu hình. Đây là prefix eligibility theo effective filter, không khẳng định final retention. |
| T4 coordinator | Hook sau khi hai branch events cùng frame/hash được xác minh và graph artifact được ghi; trước live pointer/journal publish và ACK. Không khởi tạo perception/mappers theo frame. |
| Graph/semantic binding | Overlay gắn graph SHA, frame ID, unified/object/room revision IDs và packet/branch source hashes. Publication helper kiểm tra binding trước khi cập nhật viewer pointer hoặc ACK. |
| Lỗi semantic | Giữ graph artifact. Ghi overlay failure cho đúng revision, không mang label/edge cũ. Producer không đợi browser render. |
| Scope | Chỉ `verified_only`. Scope hiện thiếu bằng chứng nên membership `unavailable_elevation_scope`, scoring `not_scored`; đây không phải abstention sau khi tính belief. T1.1 assumed declaration không được dùng cho capture mới. |
| GUI | T1.1 V5 chưa được browser-agent kiểm tra. CUA hiện trả `apps=[]`, `browsers=[]`; GUI acceptance pending. |
| Producer | 0 capture, P2, P3, OpenCLIP, ConceptGraphs, OccuSG, Qwen hoặc semantic scene replay trong lượt này. Không có bằng chứng T4 trên producer thật. |

## API và nơi nối

- Stateful session: [room_belief_commit_session.py](../integration_v2/room_belief_commit_session.py)
  cung cấp `open`, `bind_identity`, `update_after_unified_commit`,
  `write_failure_overlay`, `finalize`, `close`.
- Unified publication: [live_processing.py](../integration_v2/live_processing.py)
  xác minh graph/overlay/hash/revision và chỉ sau đó tiến live index/head,
  journal và pair ACK.
- Coordinator hook: [run_camera_live_pilot.py](../scripts/run_camera_live_pilot.py)
  dùng một session cho toàn run; graph/object/room revision IDs được giữ
  thành các namespace riêng và ghép theo frame/provenance.
- Viewer: [causal_timeline_viewer.html](../scripts/causal_timeline_viewer.html)
  xác minh hash/binding overlay rồi merge vào bộ nhớ để render; artifact
  graph gốc không bị sửa.
- Config pins: [t4_live_room_belief_v1.json](../configs/room_semantics/t4_live_room_belief_v1.json).

Input của `update_after_unified_commit(GraphCommitEnvelope)` gồm run/scene/
sequence, frame ID/index/timestamp, unified graph path/hash, object và room
revision/hash, source hashes, observations mới của frame, object/room state
tại commit hiện tại và scope status. Session giữ observation catalog prefix,
four ledgers và object/category membership state xuyên run. Duplicate event
cùng hash idempotent; cùng event ID khác hash, stale/out-of-order và graph/hash
mismatch bị từ chối. Các updates không replay toàn bộ lịch sử.

Output là immutable semantic overlay riêng, không ghi vào graph artifact.
Mỗi variant có category contributions, `raw_S`, normalized beliefs,
candidate/committed label, abstention reason và label-history delta. Khi scope
unavailable, overlay ghi `not_scored` thay vì tạo scores/abstention. Terminal
event tạo tài liệu terminal riêng; không backfill vào prefix.

## Kiểm tra đã chạy

- `python3 -m py_compile integration_v2/room_belief_commit_session.py integration_v2/live_processing.py scripts/run_camera_live_pilot.py` — pass.
- `python3 -m pytest -q tests/test_t4_room_belief_commit_session.py tests/test_t4_semantic_publication.py tests/test_t3_room_belief_viewer.py tests/test_room_belief_v1.py tests/test_d1_live_processing.py tests/test_camera_live_coordinator.py tests/test_camera_live_contract_ipc.py tests/test_camera_live_startup.py` — **89 passed**.
- `node tests/test_t4_live_viewer_overlay.js` — pass.
- `node tests/test_viewer_atomic_revision.js` — pass.
- `node --check /tmp/causal_timeline_viewer_inline.js` (inline script trích từ viewer hiện hành) — pass.
- `python3 scripts/run_camera_live_pilot.py --help` — pass.
- Startup preparation attempt 1 truyền route source JSON thay vì
  `effective_config.json`; dừng ở schema gate, trước khi tạo output/run.
- Startup dry-run attempt 2 dùng effective config đúng nhưng sandbox từ chối
  bind localhost (`PermissionError`); run directory
  `artifacts/live_processing_runs/t4_startup_model_free_20261019_v2/` giữ
  trạng thái `startup_preflight_failed`. Không có model/capture.
- Startup dry-run attempt 3 được cho phép chạy đúng một lần ngoài sandbox:
  `artifacts/live_processing_runs/t4_startup_model_free_20261019_v3/` đạt
  `startup_preflight_passed`, HTTP entrypoint được launcher kiểm tra,
  readiness IPC qua đủ năm fake workers, route validation đạt tới
  `capture_boundary_ready`, fake workers thoát graceful (`returncode=0`).
  Manifest xác nhận `camera_started=false`, `capture_frames=0`,
  `model_loads=0`; Habitat simulator initialization, runtime collision probe
  và model initialization/inference vẫn chưa được kiểm tra.

**Giới hạn verification:** startup dry-run xuyên launcher/fake readiness đã
tới capture boundary, nhưng không tạo camera frame và không xác minh simulator,
model, inference hoặc mapper update. Browser GUI vẫn pending. Không tuyên bố
camera-live acceptance.

## Hash provenance

| File | SHA-256 |
|---|---|
| `integration_v2/room_belief_commit_session.py` | `1db82b5e0319b4e79d4034c2f4ee5ce679904a607bf4b7f26ef6c19f00502da3` |
| `integration_v2/live_processing.py` | `f343cde30a788d5401c302081bec2a7efb02b461ad8a645b26e30d013cce2df7` |
| `scripts/run_camera_live_pilot.py` | `0211d5480756c499de28f2d20e9380a0d1c08d3baafa44f86cccb0cb4f1fd274` |
| `scripts/causal_timeline_viewer.html` | `9bd79408b236bf53750544f60b6fd4f01bafd27a4b02dee44ccb1f50a4fa7261` |
| `configs/room_semantics/t4_live_room_belief_v1.json` | `da4077923da86d1e7288ef9f51413792634b045009bf66b9c832a9dbd169f022` |
| T1.1 `effective_config.json` | `4e02f12ed0894047721b720d49f8d1c2f8efd2535acb0b2a2334410c239e2c65` |
| C2 source `c2_run_manifest.json` | `48e62b2f4031061d77c69fd603269e78707174e600d5ad89f7dc5a0b0a6c4753` |
| Startup dry-run v3 `run_manifest.json` | `e5fdeb33c99b30e6b8026063bd20d319dfcdadc9e326c8236eee8aa4b6191d44` |

Engineering elapsed time was not separately instrumented. All actual commands
in this checkpoint were code/tests or startup dry-run; no producer/model runtime
was charged or executed.

## T1.1 V5 GUI handoff

CUA không thấy browser surface trong phiên này. Mở theo lệnh sau, rồi dùng
checklist:

```bash
cd /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG
python3 scripts/serve_causal_timeline_viewer.py --root artifacts --viewer room_belief/t1_1_all13_t3_assumed_b_20261019_v2/viewer_package_v5/viewer --port 8783
```

URL: `http://127.0.0.1:8783/room_belief/t1_1_all13_t3_assumed_b_20261019_v2/viewer_package_v5/viewer/index.html`

1. Kiểm tra đủ 13 prefix; chọn A/B và lần lượt bốn A variants.
2. Ở A, xác nhận membership/room scoring `unavailable`/`not scored`, không có cạnh/label B.
3. Ở B, xác nhận badge `assumed` và declaration provenance ở frame 0, 61, 110.
4. Mở room:1: sofa/tv là hai unique categories; nhiều sofa vẫn một contribution.
5. Mở room:2 và room:4: lý do thiếu support/evidence phải hiện; candidate khác committed label.
6. Seek 110 → 0: không giữ label/contribution từ prefix tương lai; graph, geometry và semantics cùng revision.

Đây là checklist người dùng cần kiểm tra, không phải browser-agent GUI pass.

## Timing evidence và một pilot đề xuất

Nguồn timing: `docs/CAMERA_LIVE_BOOKKEEPING_T3_PROPOSAL_20261018.vi.md` và
`docs/CAMERA_LIVE_CL1_PILOT_V8_REPORT.vi.md`, cùng route
`smooth_t1_1_hm3d00802_turn_continue_20261018_v2`:

- 113 camera frames, 13 keyframes persisted; timestamps của frame đầu/cuối
  cách nhau 3.642 s wall-clock. Route runner tổng 12.80883 s gồm preflight /
  drain, không phải capture-only. Simulation duration là 11.648 s.
- Queue latest-wins đã xử lý frame 000000 và 000110; 11 pending frames bị
  supersede trước dispatch. Không được tính 13 persisted là 13 processed.
- P2 bắt đầu 3.457 s trước capture end; pair dispatch → unified commit frame
  đầu khoảng 7.276 s; commit đầu tiên là 6.276 s sau capture end. OccuSG
  update frame đầu khoảng 7.135 s.

Do đó route có rủi ro cao không thể đạt gate “unified revision khi camera
còn chạy” với orchestration hiện tại. Không thay route, speed hoặc queue
trong đề xuất này. Nếu vẫn muốn một run trên chính route đó, chỉ nên coi nó
là kiểm tra tích hợp live/prefix semantics (dự kiến scope `unavailable` và
room scoring `not_scored`), không hứa overlap pass. Nếu yêu cầu overlap là
điều kiện bắt buộc trước khi chi ngân sách, cần quyết định riêng về route dài
hơn; không chạy route khác trong handoff này.

Đề xuất đúng một run sau khi được duyệt:

- Route/effective config: `artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2/effective_config.json`.
- Source C2 locks/interpreters: `artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2`.
- Scope: verified-only; bốn variants; không declaration B, không Qwen, không
  đổi A tables, model, route, queue, thresholds hoặc geometry.
- Trần đề xuất: **120 phút tổng wall time cho một pilot**, trong đó tối đa
  4 phút cho camera route (`--timeout-s 240`), phần còn lại cho startup/model
  warmup, tối đa 13 keyframe candidates của route và terminal drain, GUI
  check, hashes/logs/cleanup. Chạy đúng một Habitat producer, một session
  P2/P3/OpenCLIP và một session cho từng mapper; retry 0.
- Resource gate cần đọc ngay trước pilot: available RAM/disk. V8 đo khoảng
  6.3 GiB available RAM trước và 3.8 GiB trong run, Habitat process peak RSS
  477,360,128 bytes; các worker peak/host peak chưa đo. Đề xuất gate vận hành
  tạm thời ≥6 GiB available RAM trước khi start và ≥1 GiB free output disk;
  đây là **đề xuất cho pilot**, không phải gate hiện có hoặc bằng chứng peak
  memory. Nếu không đạt thì không start.
- Command dự kiến (chưa chạy):

```bash
python3 -u scripts/run_camera_live_pilot.py \
  --config artifacts/smooth_route_previews/smooth_t1_1_hm3d00802_turn_continue_20261018_v2/effective_config.json \
  --source artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_640_quality_v1_attempt2 \
  --output artifacts/live_processing_runs/t4_hm3d00802_t1_1_verified_pilot_v1 \
  --port 8776 --timeout-s 240 \
  --room-belief-config configs/room_semantics/t4_live_room_belief_v1.json
```

Output path phải chưa tồn tại; nếu port 8776 bận thì chọn port mới, không
dừng/thay server ngoài run. Run phải ghi camera frame ID/time, graph-as-of
frame/time, same-clock queue/perception/mapper/commit timing, superseded
keyframes, terminal state, worker RSS và GUI receive/render riêng. Không chờ
giả sau frame cuối để tạo overlap. Không tuyên bố realtime, room accuracy,
scope verified hoặc quality improvement. Lệnh này là proposal, chưa được
chạy và chưa được duyệt trong checkpoint wiring.
