# T2-A — ConceptGraphs mapping session

Ngày: 2026-10-18  
Phạm vi: tách lifecycle ConceptGraphs; chưa triển khai T2-B–E.

## Đã thực hiện

- Thêm `ConceptGraphsMappingSession.open/update_frame/finalize/close` tại
  `integration_v2/conceptgraphs_session.py`.
- Chuyển `scripts/run_conceptgraphs_cached_hybrid.py` sang gọi cùng
  per-frame update và terminal finalize qua session.
- Giữ nguyên reference functions, matcher, geometry, seed, thresholds và
  terminal `denoise → filter → merge`.
- Reject duplicate/stale frame trước callback/RNG; lỗi sau mutation chuyển
  session sang `failed/replay_required`; `close()` idempotent.
- Class registry dùng first-seen prefix, không đọc class list của frame tương lai.
  Đây là thay đổi có chủ đích để session không phụ thuộc full-route class list.

## Regression

Input cố định: `c2_replica_apt3_navmesh_v1`, 12 frames, 27 observations,
seed `20261014`, cùng text/image features và room snapshot.

- Baseline trước refactor:
  `artifacts/t2_a_baseline_pre_session_20261018/mapping`
- Session-backed batch:
  `artifacts/t2_a_session_candidate_20261018_v6/mapping`
- Parity:
  `artifacts/t2_a_session_candidate_20261018_v6/session_parity_report.json`

Kết quả: 19 online hypotheses, 19 sau denoise, 1 sau filter, 1 sau merge;
lineage, diagnostic manifest, observation lineage, geometry, partition và các
field object đều khớp. `class_id` khác do baseline dùng global future class list;
`class_name` và geometry vẫn khớp. Report ghi rõ khác biệt này.

## Kiểm thử

```bash
python3 -m pytest -q \
  tests/test_conceptgraphs_session.py \
  tests/test_conceptgraphs_kaggle_runner.py \
  tests/test_conceptgraphs_output_contract.py \
  tests/test_hybrid_timeline.py
```

Kết quả: **10 passed**.

## Handoff bổ sung sau nghiệm thu T2-A

### Thời gian và ngân sách

Không có bộ đếm wall-clock riêng cho toàn bộ thời gian kỹ thuật của lượt T2-A,
nên không thể khẳng định một con số engineering-time chính xác từ artifacts.

- Khoảng thời gian quan sát được từ artifact clock, từ thư mục baseline được
  ghi lúc `09:03:53.992873` đến mapping candidate v6 được ghi lúc
  `09:09:31.423281` ngày 2026-10-18, là `337.430 s` (`5m37.430s`). Đây là
  lower-bound của execution/artifact span, không bao gồm thời gian suy luận,
  chỉnh code và chờ công cụ.
- Các runtime được ghi trong manifest: baseline mapping `4.1196 s`, candidate
  v6 mapping `1.3304 s`, candidate v6 recorder `3.0960 s`, peak RSS
  `461.55 MiB`.
- Trạng thái ngân sách: không có bằng chứng execution vượt trần 90 phút;
  engineering wall-clock tổng thể chưa được instrument nên được ghi là
  `not_measured`, không gọi là đo đủ 90 phút.

### Vì sao có sáu verification passes

Các pass đều dùng cùng cache, seed và reference inputs; không phải sáu
thí nghiệm chất lượng hay sáu lần retry tìm một kết quả đẹp hơn:

1. `v1`: chạy session-backed mapping đầu tiên để xác nhận lifecycle, sau đó
   phát hiện diagnostics được ghi sau finalize nên cần sửa thứ tự persist.
2. `v2`: kiểm tra sửa thứ tự diagnostics/timeline và output terminal.
3. `v3`: kiểm tra registry class first-seen prefix, loại phụ thuộc class list
   của toàn route tương lai.
4. `v4`: kiểm tra lifecycle close và xử lý exception/failure state.
5. `v5`: kiểm tra packet provenance tường minh (`capture_record`,
   `capture_root`, `asset_root`) và update không dò manifest tương lai.
6. `v6`: pass cuối sau cleanup source/import; đây là candidate được dùng cho
   parity report và handoff.

Một lần chạy baseline trước refactor được giữ riêng. Có một lệnh shell ban
đầu dừng trước khi mapper khởi động vì output parent chưa tồn tại; không có
mapping process hay artifact hợp lệ từ lần đó và nó không được tính là pass.

### Class registry và cách consumer resolve `class_id`

Session dùng registry prefix deterministic: tên class mới trong frame hiện tại
được chuẩn hóa, sắp xếp rồi append vào `state.class_names`; không đọc class
list từ frame tương lai. Registry này được ghi cùng native map pickle tại
`mapping/native/conceptgraphs_map.pkl.gz` dưới key `class_names`, còn mỗi
object/gobs giữ `class_id` theo chỉ số của chính list đó. `experiment_contract`
ghi policy và runner hash để ràng buộc provenance.

Consumer phải resolve `class_id` chỉ trong cùng native artifact/run bằng
`class_names[class_id]`; integer không có ý nghĩa dùng xuyên run. Với export
object độc lập, consumer dùng `reference_class_history` và
`raw_detector_class` trong `export/export_manifest.json`/lineage để hiển thị
semantic name, không tự map lại integer theo một ontology hoặc class list
khác. Nếu registry list thiếu hoặc index ngoài phạm vi, consumer phải báo
`class_registry_unavailable` thay vì đoán tên.

### Giới hạn vẫn giữ nguyên

T2-A là session-backed cached batch, chưa phải streaming producer. Perception,
encoder và OccuSG session chưa được nối; membership và room semantics vẫn
`unavailable`.

## Chưa làm

- Chưa giữ sống detector/encoder hoặc OccuSG worker.
- Chưa chạy producer T1.1, D1, capture mới hay model mới.
- Membership và room semantics vẫn `unavailable`.
- T2-B cần kiểm tra perception session; T2-C cần kiểm tra các đường
  `SceneGraphBuilder.update(packet)`, `OctoMapWorker` và `DuDeWorkerClient`.
