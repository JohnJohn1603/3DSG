# T4 publication và camera timebase — cập nhật 2026-10-19

**Trạng thái:** hai blocker được harden và kiểm tra model-free. Một lần khởi chạy pilot mới đã dừng ở contract pre-capture trước frame đầu tiên. Không retry; T4 live pilot, graph/camera overlap và render revision thật vẫn pending.

## Phạm vi đã giữ

- Route: T1.1 `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`; không tạo hoặc chọn route theo predictor.
- C2 source: `hm3d00802_main29_640_quality_v1_attempt2`; không chạy lại C2.
- C2 `c2_run_manifest.json` SHA-256: `48e62b2f4031061d77c69fd603269e78707174e600d5ad89f7dc5a0b0a6c4753`.
- Capture profile: 640×480, HFOV 90°, calibration adapter hiện hành.
- Model, detector preprocessing/thresholds, matcher, admission, A tables, scope và baseline không đổi.
- T4 chạy `verified_only`; membership `unavailable_elevation_scope`, room scoring `not_scored`.
- Run v1 `t4_paced_overlap_00802_v1` cùng artifacts và manifest lịch sử được giữ nguyên.

## Publication contract

`integration_v2/live_processing.py` nay dùng hai tham chiếu tách bạch:

- Branch event `state_path` và cloud asset path được resolve từ run root, chỉ cho phép file trong run.
- `joined_state_url`, graph, room geometry, camera/evidence, semantic overlay được ghi dưới viewer-relative URL; publisher decode/resolve đúng một lần và từ chối traversal, path ngoài run, URL ngoài hoặc tham chiếu mơ hồ.
- Publisher xác minh hash graph/object/room/overlay và binding revision/frame/source trước khi publish. Immutable artifacts và journal được ghi trước live index pointer; ACK hai branch chỉ sau khi pointer được cập nhật.

Regression HTTP model-free chạy qua `_join_one → semantic commit → publisher → immutable graph/overlay → live index/head → HTTP → D2 bundle`. Fixture commit hai prefix revisions; revision đầu vẫn tải được sau revision thứ hai, live-head hash/count khớp index, semantic overlay giữ `not_scored`, và các URL graph/room/geometry/evidence/overlay/joined-state được HTTP phục vụ. Đây là fixture integration, không phải revision từ scene thật.

## Pacing và motion timebase

Config v1 cũ giữ nguyên. Config v2 mới khóa:

- Habitat `step(dt=0.04 s)`; capture target 25 FPS simulation, RTF 1.0, wall interval mục tiêu 40 ms.
- Tốc độ cấu hình giữ 0.2 m/s và 30°/s; mỗi bước 40 ms tương ứng 0.008 m và 1.2°.
- Pacing dùng deadline host-monotonic theo simulation time, không catch-up burst; không chờ sau frame cuối. Mỗi observation ghi simulation delta thực tế và từ chối duplicate/stale hoặc delta khác 40 ms.
- Route budget từ geometry/config: 0.514 m, tổng quay tuyệt đối 270°, 291 frame ước tính gồm frame đầu, conservative bound 306, hard cap 400. Đây là preflight estimate, không phải số đo capture.

Hash khóa:

| Artifact | SHA-256 |
| --- | --- |
| `configs/habitat/smooth_route_t1_paced_v4.json` | `64b2c721128848dedd2b01aa3c976f5aa21e178aea27bccf537c3513eb2c90c5` |
| `configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_v2.effective.json` | `fef21191f9ba2fd6b1c219379289630748ea5876797947d61ba15b3e624fc377` |

Chưa đo được actual FPS, camera intervals hoặc tốc độ chuyển động của config v2 trong capture thật vì pilot dừng trước Habitat capture. Vì vậy pacing hiện **được khóa và kiểm tra bằng code/config**, chưa được xác nhận bằng trajectory mới.

## Kiểm chứng trước pilot

- `python3 -m pytest -q tests/test_t4_publication_http_e2e.py tests/test_camera_pacing.py tests/test_camera_live_startup.py` — **31 passed**. Bài HTTP cần quyền bind loopback và đã chạy ngoài sandbox.
- Startup dry-run `t4_paced_publication_timebase_startup_dryrun_20261019_v2` đạt: cùng live launcher bootstrap, D2 HTTP, readiness IPC của 5 fake workers, route runner preflight đến `capture_boundary_ready`; 0 model loads, 0 capture frames. Habitat simulator/runtime collision probe không được chạy trong dry-run.
- Lần dry-run đầu trên port 8776 dừng trước workers vì port đang được PID 14871 dùng bởi server của run v1. Server đó không bị dừng/thay thế. Dry-run v2 dùng port 8777 và đạt.

## Lần pilot thật duy nhất trong lượt này

Run mới: [t4_paced_publication_timebase_00802_v2](../artifacts/live_processing_runs/t4_paced_publication_timebase_00802_v2/).
Manifest thất bại SHA-256: `64c8f88d183a915e4da3ff34ac23209d648f52fcbedab667e5e07e95d0a950bd`; thư mục partial artifacts hiện khoảng 42.3 MB, chủ yếu là D2 evidence assets sao chép từ C2 source, không có capture mới.

- Worker readiness: P2, P3, OpenCLIP features, ConceptGraphs và OccuSG đều phát ready marker cho cùng run/config. Browser đã mở D2 trên `127.0.0.1:8777`; UI hiển thị `waiting_for_first_revision`, `0 revisions`, XYZ view ready, live-follow ON, và membership/room semantics unavailable. Served entrypoint được xác thực.
- Explicit start signal được ghi và xác minh (SHA-256 `6d6393dc1bf01e3a6bce729c783fe39e2527d3c283f77facf2f1acbbcfa875ad`).
- Sau start, route runner gọi bootstrap validator nhưng validator cũ chỉ chấp nhận `control/` rỗng và từ chối `control/start.json`: `StartupContractError: unexpected pre-capture control contents: ['start.json']`.
- Run được đánh dấu `failed`; cả năm worker thuộc run được thu dọn. `camera_capture=pending`; camera frames/keyframes=0; perception frame calls=0; mapper updates=0; unified revisions=0; terminal postprocess không chạy. Không có semantic overlay hoặc revision thật để render.
- Lưu ý bookkeeping: manifest terminal có `status=failed` và lỗi cụ thể, nhưng các trường `stages.perception/mappers` giữ giá trị khởi tạo `running`. Không sửa lịch sử; `camera_producer_manifest` không tồn tại, camera log rỗng, bộ đếm keyframe bằng 0 và cleanup PID là bằng chứng run dừng trước capture. Đây là giới hạn báo cáo stage của lần lỗi này.
- Tất cả worker đã đạt readiness; manifest không ghi số lần load từng model ở ranh giới này, nên model-load count chính xác không được suy đoán. Không có inference hoặc mapping update.
- Đây là **pipeline/startup contract failure trước camera**, không phải thất bại inference, không phải thiếu compute, và không chứng minh được overlap. Đây là đúng một pilot launch; không retry theo giới hạn đã duyệt.

Sau khi run v2 kết thúc, validator được sửa để chấp nhận **chỉ** `control/start.json` hợp lệ, gắn với cùng run ID, D2 pointer hash, entrypoint, URL và hash marker trong manifest; control file lạ vẫn bị từ chối. Fixture tests cho marker hợp lệ/không hợp lệ đều đạt. Sửa này chỉ có hiệu lực trong source hiện tại, không sửa run v2 hay bất kỳ artifact lịch sử nào.

## Acceptance tách riêng

| Gate | Trạng thái |
| --- | --- |
| Full publication chain trong model-free HTTP fixture | Đạt |
| 40 ms motion/timebase consistency trong config và tests | Đạt; capture thật chưa đo |
| D2 waiting page mở trước start | Đạt trên browser; chưa có revision |
| Camera capture / motion | Chưa chạy trong run mới |
| Perception và hai mapper trên frame mới | 0 frame; chưa chạy update |
| Unified revision thật | Chưa có |
| Camera/graph overlap và viewer render overlap | Chưa kiểm chứng |
| Room scoring | `not_scored`, verified spatial scope unavailable |
| Realtime, quality improvement, scope verification | Không được tuyên bố |

**Bước tiếp theo:** cần duyệt một pilot riêng sau khi đã xem lỗi startup cụ thể này; lượt hiện tại không chạy lại. Mọi pilot tiếp theo phải dùng source đã sửa, run directory mới và giữ nguyên route/config.

## Files/source hashes

- `integration_v2/live_processing.py`: `2b863bfdaf37bcc6ad2947d42fa8ad9369c2d9975288f3ef7387a4b517a6b34d`
- `integration_v2/camera_pacing.py`: `1babc51504390b7a0fc2be19096a7a653f9d12b167c4a126d4c56c2518125213`
- `integration_v2/camera_live_startup.py` sau sửa start marker: `b01606a5994477d90d80185fdcd80484239dee0d4da36976335e89cc36b34042`
- `scripts/run_habitat_smooth_route_preview.py`: `17dfc17e96f08a8371e11ff0f4d2bcaae63d0a0014300b1db72677a298b8bb03`
- `scripts/run_camera_live_pilot.py`: `41ab9681475ce78d3b9e8a51a873c5a6cfa20c0ccfcc65361e47d3bbf829e971`
