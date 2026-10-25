# V8 mapper-only replay — 2026-10-19

## Kết quả

Đã chạy đúng một mapper-only pass từ state rỗng trên 13 joined perception packets của `camera_live_t1_1_20261018_v8`. Không capture, perception, detector, encoder, Qwen hoặc retry trong lượt này. Run cũ `_all13_replay_..._v1` vẫn giữ trạng thái `failed_before_mapper_update`; run mới được lưu riêng.

- Run ID: `hm3d00802_t1_1_all13_mapper_only_20261019_v1`
- Scene/sequence: `00802-wcojb4TFT35` / `hm3d-v02-00802-t1-1-turn-continue-v1_smooth_smooth_t1_1_hm3d00802_turn_continue_20261018_v2`
- Chế độ: `mapper_only_replay_from_validated_perception_packets`
- Frames theo thứ tự: `0, 26, 33, 40, 50, 61, 68, 75, 82, 89, 96, 103, 110`
- ConceptGraphs: một session, 13 updates, một terminal postprocess, exit 0.
- OccuSG: một session, 13 updates, một terminal flush, exit 0.
- Unified graph: 13 prefix revisions đã commit; terminal stage tách riêng.
- Retry: 0. Cả hai worker đã thoát; không còn mapper process của run.

## Readiness và đầu vào

Root cause của run trước là validator so tên ready-file role `occusg` với marker kind `occusg_rooms`. Bản fix đã được test trước khi chạy. Run mới đặt hai mapper worker theo interpreter đã khóa; adapter của từng worker đã validate đủ cả 13 packet trước khi mapping bắt đầu. Hai nhánh nhận cùng packet path/hash theo cùng thứ tự; coordinator gửi cho cả hai trước khi đợi pair completion.

Nguồn thất bại cũ được xác nhận không có object/room update marker và không có mapper process còn sống. Tất cả 232 immutable outputs của nguồn được đối chiếu lại với integrity manifest sau run; không có hash mismatch. Joined packet được copy nguyên byte sang run mới, còn raw capture/mask/feature assets được tham chiếu read-only.

## Thời gian và bộ nhớ

- Tổng mapper-only run: `153.32 s`.
- Runtime adapter preflight hai nhánh: `1.59 s`.
- Tổng thời gian chờ cặp update/commit qua 13 frames: `143.66 s`.
- Terminal postprocess + flush: `1.67 s`.
- Peak RSS riêng ConceptGraphs: `508,071,936 B` (~484.4 MiB).
- Peak RSS riêng OccuSG: `682,938,368 B` (~651.6 MiB).
- Coordinator peak RSS: `47.6 MiB`.

Peak RSS là số đo riêng theo process, không cộng thành host peak. Lượt chạy hoàn tất trong trần 30 phút.

## Graph và giới hạn

Prefix có 13 object snapshots, 13 room snapshots và 13 paired commits. Terminal ConceptGraphs ghi `before_post=15`, `final_count=6`; đây là số node qua các stage xử lý, không phải accuracy. Terminal OccuSG flush là revision 14 sau 13 frame updates. Unified output không có `room_contains` edge.

Membership vẫn `unavailable_elevation_scope`; room semantics vẫn `unavailable_membership_scope`. Không áp dụng declaration B của main29 cho run này. Không có room label hoặc quality claim được tạo.

Run này là replay từ packet đã lưu, không phải camera-live, realtime, hay bằng chứng causal trong lúc camera capture.

## Integrity và viewer

- Run manifest status: `completed`.
- Manifest receipt SHA-256: `d30dc27759bf8eb29ae87fccb1037fa5b2b944d54bbdacc44a622ae3d950fa58`.
- 289 immutable outputs được kiểm tra hash; không có mismatch.
- D2 pointer, manifest và live index đều ở trạng thái completed, 13 frames.
- GUI chưa được browser-tested trong lượt này.

Lệnh mở viewer:

```bash
python3 scripts/serve_causal_timeline_viewer.py \
  --root artifacts/live_processing_runs \
  --viewer hm3d00802_t1_1_all13_mapper_only_20261019_v1/viewer \
  --port 8775
```

Sau đó mở `http://127.0.0.1:8775/hm3d00802_t1_1_all13_mapper_only_20261019_v1/viewer/index.html`.

## Kiểm tra đã chạy

- `python3 -m py_compile scripts/run_v8_mapper_only_replay.py` — pass.
- `python3 -m pytest -q tests/test_v8_ordered_replay_contract.py tests/test_conceptgraphs_session.py tests/test_occusg_session.py tests/test_d1_live_processing.py` — 30 passed.
- Run command: `python3 -u scripts/run_v8_mapper_only_replay.py --run-id hm3d00802_t1_1_all13_mapper_only_20261019_v1 --runtime-cap-s 1800`.

Runner source SHA-256 tại thời điểm run: `37016a4bc1fe1e639c9d5d924dd21a1d72504611e7ce3cefdcf5811e1d4474e6`. Sau run, chỉ bổ sung bookkeeping hoàn tất cho D2 manifest/pointer và lưu exit codes/source provenance; không chạy lại mapper.
