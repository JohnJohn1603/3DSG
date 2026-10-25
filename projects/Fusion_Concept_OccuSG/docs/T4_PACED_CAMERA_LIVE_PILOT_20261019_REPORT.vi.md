# Báo cáo pacing và pilot camera-live T4

**Trạng thái:** một pilot thật đã chạy từ state rỗng nhưng thất bại ở atomic publication của unified revision. Không chạy lại. Run và partial artifacts được giữ nguyên.

## Pacing/config

- Route hình học tái sử dụng: T1.1 `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`, giữ route move → turn → continue; không chọn lại theo predictor.
- Capture giữ 640×480, HFOV 90°, calibration adapter, sensor pose, depth contract, seed, model, thresholds, matcher, admission và scope policy.
- Policy mới dùng target 30 FPS, `real_time_factor=1.0`, monotonic simulation-time pacing; không catch-up burst. Chỉ chờ giữa các bước còn tiếp tục, không chờ sau frame cuối hoặc ACK/browser. Linear/angular speed cấu hình giữ 0.2 m/s và 30°/s.
- Habitat thực tế tiến `0.04 s/step`, không phải cấu hình `0.033333 s`; vì thế run đo được khoảng 25 FPS. Action increment được tính từ 1/30 s nên tốc độ hiệu dụng trong partial run xấp xỉ 0.167 m/s và 25°/s, thấp hơn tốc độ cấu hình 0.2 m/s và 30°/s. Không coi target 30 FPS hoặc tốc độ vật lý cấu hình là đã đạt.
- Không có pacing trong run v8 cũ: 11.648 s simulation chạy trong 3.642 s wall (xấp xỉ 3.20×). Pilot mới đo pacing riêng; đây không phải realtime claim.

## Pilot thực tế

Run: `t4_paced_overlap_00802_v1` tại `artifacts/live_processing_runs/t4_paced_overlap_00802_v1`.

- Effective config SHA-256: `31f9dd668a80d56954685b287de020a54e13e3fa37a91dbf4fc888ca2dc03376`.
- Pacing policy SHA-256: `eeb75a270529da9651f7d93187729ee4a94a3fb1db534c208c8b510c3bee44fe`.
- C2 source manifest SHA-256: `48e62b2f4031061d77c69fd603269e78707174e600d5ad89f7dc5a0b0a6c4753`.
- RAM khả dụng trước run: 6.73 GB; disk trống: 302 GB; port 8776 trống.
- Chính xác một pilot, một Habitat process và một worker/session cho P2, P3, OpenCLIP, ConceptGraphs, OccuSG. Không chạy lại capture, detector, encoder hoặc mapper.
- Browser mở D2 và hiển thị `waiting_for_first_revision` trước capture. HTTP log ghi trang được tải lúc 16:36:56; frame camera đầu tiên lúc 16:37:19.897 (+07:00).

### Pacing đo được trên partial capture

Camera bị dừng trước khi route hoàn tất, nên các số dưới đây chỉ mô tả 300 frame đã ghi:

| Chỉ số | Kết quả partial |
| --- | ---: |
| Frames | 300 (0–299) |
| Thời lượng simulation giữa frame đầu/cuối | 11.960 s |
| Thời lượng wall giữa frame đầu/cuối | 11.938 s |
| Tốc độ camera đo được | 25.05 FPS |
| Khoảng frame wall mean / p50 / p95 / max | 39.93 / 39.86 / 50.26 / 104.34 ms |
| Khoảng simulation trung bình | 40.00 ms |
| Simulation/wall | 1.002× |
| Pacing wait đã ghi | 3.084 s; 299 lần vượt deadline cấu hình |

Frame cuối là `turn_left`, event cuối ghi `continuing=true`; không có `capture_finished` bình thường. Do vậy đây không phải route hoàn chỉnh.

### Perception, mapper và publication

- Lỗi `timestamp_s` cũ **không tái xuất hiện**: P2, P3 và OpenCLIP đã chạy thật cho keyframe `...cam-000000`; packet join đã được tạo. P2 trả 5 detections, P3 trả 5 detections; OpenCLIP load model 1 lần và encode 5 image rows.
- P2 bắt đầu 0.124 s sau frame đầu trong khi camera producer đang sống. Một frame được gửi tới cả hai mapper. Object branch ghi state/event; room branch ghi state/event. Processing ghi nhận lần lượt khoảng 0.369 s và 8.903 s. Camera tiếp tục phát frame trong thời gian xử lý đó.
- Queue ghi 11 keyframe packets: 1 được dispatch, 9 bị supersede, 1 còn pending khi lỗi xảy ra. Coordinator đếm 0 processed/0 unified revisions vì pair chưa được publish.
- Atomic publish dừng với `ValueError: joined revision is missing, outside the run or hash-mismatched before publication`. Cả hai branch events/snapshots và `joined/revisions/rev-000001.json` tồn tại, nhưng `viewer/live_index.json` vẫn có 0 frames.
- Nguyên nhân đã xác định: `_join_one` ghi `joined_state_url` tương đối với viewer (`../joined/...`), nhưng publisher resolve nó tương đối với run root. Sau pilot, resolver được sửa để dùng cùng viewer-relative contract và regression test được cập nhật. Đây là sửa source; không áp dụng ngược vào manifest/run cũ và không tái chạy producer.
- Vì không có unified revision, không thể xếp revision đầu trước/sau capture end: revision đầu không tồn tại và capture không kết thúc bình thường.

## Acceptance tách riêng

| Gate | Trạng thái |
| --- | --- |
| Browser/D2 bootstrap trước capture | Đạt; GUI hiện `waiting_for_first_revision` |
| Camera và perception/mapper có thời gian chồng lấp | Có overlap trong partial run; camera route bị hủy trước hoàn tất |
| Pipeline end-to-end thành công | Không đạt: lỗi contract ở atomic publication |
| Unified revision được commit/render khi camera còn chạy | Không; 0 revisions, GUI render overlap chưa đạt |
| Room scoring | Không chạy; verified-only giữ nguyên, membership scope unavailable, `not_scored` |
| Realtime / quality improvement | Không được xác nhận |

Coordinator đánh dấu pilot/bootstrap `failed` và dừng các process thuộc run. Port 8776 không còn listener. Partial artifacts giữ nguyên. Không có retry pilot.

## Preflight, tests và sửa sau pilot

- Startup dry-run cuối cùng đạt qua launcher/HTTP/IPC tới capture boundary: 0 model loads, 0 capture frames. Lần đầu bị sandbox chặn quyền bind socket; lần kế tiếp thành công.
- Sau khi thêm regression cho path lỗi, targeted suite chạy **50 tests, pass**; `py_compile` pass.
- Test không thay thế pilot: atomic publication bug chỉ được phát hiện trong run thật; pilot vẫn giữ trạng thái failed.

Artifacts chính:

- [Pilot manifest](../artifacts/live_processing_runs/t4_paced_overlap_00802_v1/run_manifest.json)
- [Partial camera event log](../artifacts/live_processing_runs/t4_paced_overlap_00802_v1/events.jsonl)
- [Partial camera keyframe journal](../artifacts/live_processing_runs/t4_paced_overlap_00802_v1/camera_keyframes.jsonl)
- [Object branch event](../artifacts/live_processing_runs/t4_paced_overlap_00802_v1/branches/objects/events.jsonl)
- [Room branch event](../artifacts/live_processing_runs/t4_paced_overlap_00802_v1/branches/rooms/events.jsonl)
- [Pacing policy](../configs/habitat/smooth_route_t1_paced_v3.json)
- [Effective route config](../configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_paced_640x480_v1.effective.json)
