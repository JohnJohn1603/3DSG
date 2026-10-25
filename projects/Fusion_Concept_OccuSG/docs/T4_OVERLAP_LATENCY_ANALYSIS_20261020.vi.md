# T4 — phân rã latency tới unified revision đầu

Phân tích read-only các log hiện có của run `t4_camera_live_00802_t1_1_timebaseA_20261019_pilot_v1`. Không chạy producer, mapper, model hoặc semantic replay; không đổi config.

## Mốc thời gian cùng clock

Các mốc monotonic đều thuộc `host_monotonic:f2227172-258d-447a-ad4c-3c9362267157`:

| Mốc | Thời gian | Ý nghĩa |
|---|---:|---|
| Frame 0 được capture | 18083.509849 s | `capture_monotonic_s` trong execution ledger |
| Keyframe được publish | 18083.598665 s | File-backed event đã phát |
| Keyframe được consume | 18083.641708 s | Khoảng 43 ms sau publish |
| P2 bắt đầu | 18083.656695 s | Khoảng 15 ms sau consume |
| Cặp mapper được dispatch | 18086.295775 s | Sau P2/P3/OpenCLIP và packet join/validation |
| Unified revision 1 commit | 18101.144887 s | Frame `...cam-000000`, object/room event seq 1 |
| Capture hoàn tất | 18095.185809 s | Camera đã dừng trước commit |

Từ capture frame đầu tới unified commit đầu là **17.635 s**. Capture kéo dài khoảng **11.676 s** theo monotonic timestamps; commit đầu đến **5.959 s sau capture end**. Vì vậy graph/camera overlap không đạt trong run này.

## Processing và orchestration

- Frame đầu được consume gần như ngay: publish→consume **43 ms**, queue admission khoảng **4 ms** sau khi queued. Queue không giải thích latency của revision đầu.
- P2 start→mapper-pair dispatch mất **2.639 s**. Log có thời điểm bắt đầu P2, P3 và OpenCLIP, nhưng không có mốc hoàn tất riêng cho từng stage. Do đó không thể tách chính xác model compute, file/IPC, packet join và validation trong khoảng này. Dispatch chứng minh các packet đã sẵn sàng ở mốc đó.
- Hai mapper được dispatch như một cặp với cùng packet SHA-256 và source hashes. ConceptGraphs bắt đầu update khoảng 18 ms sau OccuSG. Không thấy bằng chứng dispatch tuần tự hoặc chờ browser.
- `processing_ms` của worker đo `session.update_frame` **cộng serialization state** (ConceptGraphs còn ghi cloud/snapshot; OccuSG ghi snapshot); đây không phải thời gian core thuần:
  - OccuSG: **13.983 s**.
  - ConceptGraphs: **0.422 s**.
  - OccuSG là phần xử lý chi phối critical path, chậm hơn khoảng **13.561 s**.
- Sau khi publish branch event, ConceptGraphs chờ barrier **14.354 s**; OccuSG chờ **0.813 s**. Đây là thời gian tới ACK bền vững sau khi coordinator đã ghép hai nhánh, cập nhật semantic overlay và live index. Hai khoảng chờ chồng lấp nhau, **không cộng lại**. ConceptGraphs chủ yếu đang chờ OccuSG và unified commit; đây không phải 14.354 s xử lý ConceptGraphs.
- Từ mapper-pair dispatch tới unified commit là **14.849 s**. OccuSG bắt đầu khoảng 40 ms sau dispatch và báo 13.983 s update+snapshot; còn khoảng **0.826 s** trên critical path cho publish/ghép/semantic/index/ACK và scheduling. Các thao tác này chưa có timing riêng đủ chi tiết để phân chia chính xác.
- Semantic update ghi **34 ms**. `publication_ms` khoảng 2.36 ms chỉ đo append branch event; nó không bao gồm bước kiểm tra hashes trước đó, nên không đại diện toàn bộ publication latency. Browser không phát ACK để giải phóng mapper; coordinator ghi index rồi mới gửi branch ACK.

## Kết luận từ log

Độ trễ chủ yếu đến từ **xử lý/serialization của OccuSG**, không phải pending queue, ConceptGraphs compute, browser ACK hay thứ tự dispatch. Orchestration/join/publish thêm một đoạn nhỏ hơn nhưng hiện chưa được instrument đủ để tách thành các substage. Tăng route length không làm frame đầu xử lý nhanh hơn; nó chỉ giữ camera chạy đủ lâu để quan sát commit trong khi capture còn hoạt động.

## Một pilot nhỏ nhất được đề xuất — chưa chạy

Chỉ sau khi được duyệt, tạo **một route continuation geometry-only** trên cùng HM3D 00802: tiếp tục hợp lệ từ endpoint T1.1 trên navmesh, không lặp đoạn và không thêm thời gian chờ. Khóa route trước inference. Chọn thời lượng camera-active mục tiêu **ít nhất 25 s** để vượt mốc first-commit 17.635 s đã quan sát với khoảng đệm; nếu preflight không tìm được continuation hợp lệ thì dừng, không đổi sang route khác sau khi xem predictor output.

Giữ nguyên 640×480, `dt=40 ms`, motion increments/speeds, keyframe policy, latest-wins queue, model, thresholds, mapper, scope và verified-only. Chạy đúng một pilot từ state rỗng; không retry sau capture bắt đầu. Acceptance chính: unified revision đầu commit trước camera frame cuối, bằng monotonic timestamps cùng clock domain. Ghi riêng D2 receive/render nếu có; không gọi route dài hơn là tối ưu latency, realtime hoặc room-semantic success. Không chạy pilot này trong lượt phân tích.

## Nguồn

- Run: `artifacts/live_processing_runs/t4_camera_live_00802_t1_1_timebaseA_20261019_pilot_v1/execution_ledger.jsonl`, `logs/pipeline_events.jsonl`.
- Branch timings: `branches/objects/timings.jsonl`, `branches/rooms/timings.jsonl`.
- Branch events: `branches/objects/events.jsonl`, `branches/rooms/events.jsonl`.
- Worker timing boundary: `scripts/run_t2_d_cached_packet_worker.py`.
- Barrier ACK/publish order: `src/standalone_sg/serialization/live_revision_io.py`, `integration_v2/live_processing.py`.
