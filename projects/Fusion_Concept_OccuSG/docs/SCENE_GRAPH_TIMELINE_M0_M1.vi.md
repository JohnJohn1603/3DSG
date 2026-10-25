# M0 + M1 — causal object timeline trên cache 00802

## Phạm vi

Lượt này chỉ thêm recorder read-only và kiểm chứng replay trên cache 29 frame.
Không triển khai M2 timeline viewer, M3 launcher mới hoặc M4 live processing.
Không chạy capture, detector, encoder, Qwen, native backend mới, tuning hay
thay đổi matcher/default/baseline.

## Contract đã khóa

- Scene: `00802-wcojb4TFT35`.
- Sequence: `hm3d-v02-00802-main-route-01`.
- Cache: `artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace`.
- Mapping: ConceptGraphs cached hybrid, `obj_min_detections=3`, seed `20261014`.
- Input population: 29 frames, 67 observations.
- Object state: snapshot sau association/fusion của từng frame, trước denoise,
  filter và text-gated merge.
- Object hypothesis ID là revision-local content lineage; không coi là
  persistent ID sau postprocess.

## Causality

Manifest tách riêng:

- `objects`: `prefix_state_only`.
- `rooms`: `prefix_state_only_native_replay_reference`.
- `membership`: `not_causal_for_hybrid_objects`.
- `room_semantics`: `unavailable`.

Room delta đã có sẵn trong replay native và được đọc lại thành các
`room_revisions/rev-*.json`; native backend không chạy lại. Room IDs và object
membership trong delta thuộc source backend, vì vậy không được dùng để gán
ConceptGraphs hybrid hypotheses tại thời điểm quá khứ.

Final denoise/filter/merge chỉ được tham chiếu qua `lineage_stages.json`; không
được đưa ngược vào các prefix snapshots. Prefix admission không được reference
mapper xuất thành stage riêng nên được ghi `unavailable`.

## Code đã thêm

- [`src/standalone_sg/serialization/hybrid_timeline.py`](../src/standalone_sg/serialization/hybrid_timeline.py)
  - snapshot cloud/lineage độc lập sau mỗi frame;
  - delta log revision chain;
  - room revision extraction từ delta có sẵn;
  - prefix future-observation guard;
  - storage/runtime accounting.
- [`scripts/run_conceptgraphs_cached_hybrid.py`](../scripts/run_conceptgraphs_cached_hybrid.py)
  nhận tùy chọn `--timeline-out`; mặc định không thay đổi.
- [`scripts/validate_causal_timeline.py`](../scripts/validate_causal_timeline.py)
  kiểm tra delta replay, prefix, repeatability và final export parity.

Recorder không gọi mapper, không giữ object reference sau callback và không
tiêu thụ RNG. Cloud được ghi theo `geometry/rev-*/` thay vì nhúng vào mọi JSON.

## Artifacts đã tạo

- Baseline không recorder:
  `artifacts/scene_graph_timeline/m0_m1_00802/baseline/`
- Recorder lần 1:
  `artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/`
- Recorder lần 2:
  `artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_2/`
- Validation:
  `artifacts/scene_graph_timeline/m0_m1_00802/m1_validation.json`

Mỗi timeline có `manifest.json`, `frame_index.json`, `delta_log.json`,
`snapshots/`, `geometry/` và `room_revisions/`.

## Kết quả kiểm chứng

- 29/29 frame snapshots và delta revisions đọc lại được.
- Prefix guard đạt: không snapshot nào chứa observation ở frame tương lai.
- Hai recorder-on fresh process tái lập snapshot hashes.
- Recorder on/off giữ nguyên exported object evidence partition và NPZ geometry.
- Mapping cuối: 33 hypotheses trước postprocess, 6 sau filter, 6 sau merge.
- Recorder runtime: khoảng 17.9 s; baseline: khoảng 8.9 s.
- Peak RSS: baseline khoảng 480.9 MiB; recorder khoảng 492.4 MiB.
- Timeline storage: khoảng 21.8 MB/run.

Native ConceptGraphs pickle không đạt byte-identical giữa fresh processes; đây
là serialization detail. Parity contract dùng exported NPZ geometry, bounds và
observation partition, các giá trị này đạt.

## Lệnh tái lập

```bash
./.venv-cg-cpu/bin/python scripts/run_conceptgraphs_cached_hybrid.py \
  --cache artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace \
  --text-features artifacts/conceptgraphs_hybrid_runs/hm3d_00802_main29_min_filter_v1/text_features \
  --image-features artifacts/conceptgraphs_hybrid_runs/hm3d_00802_main29_min_filter_v1/image_features \
  --room-snapshot artifacts/runs/hm3d_00802_20261011T025309.067351Z_4f3ace/replay/snapshot.json \
  --output <new_mapping_output> --timeline-out <new_timeline_output> \
  --frames 29 --seed 20261014
```

```bash
PYTHONPATH=src:. .venv-cg-cpu/bin/python scripts/validate_causal_timeline.py \
  --baseline artifacts/scene_graph_timeline/m0_m1_00802/baseline/mapping \
  --recorded <new_mapping_output> --recorded-repeat <second_mapping_output> \
  --timeline <new_timeline_output> --timeline-repeat <second_timeline_output> \
  --output <validation.json>
```

## Known limitations

- M1 chưa có viewer play/pause/seek; đây là phần M2.
- Room graph có prefix state từ native replay reference, nhưng hybrid
  object–room membership chưa causal.
- Room semantics không được điền ngược từ final consensus.
- Prefix admission riêng chưa được reference mapper expose.
- Timeline chưa đo live throughput/latency.
- Multi-floor scope vẫn phụ thuộc provenance room geometry hiện có.
