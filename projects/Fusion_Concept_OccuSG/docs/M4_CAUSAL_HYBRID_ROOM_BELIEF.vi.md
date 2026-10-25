# M4 — Causal hybrid membership và room belief trên timeline 00802

## Trạng thái

M4 đã chạy trên artifacts M0/M1 của 29 frame, không chạy lại capture,
detector, encoder, Qwen, native backend hay mapping. Bản `v1` trước đây
đã chặn membership quá bảo thủ vì chỉ nhìn metadata trên room node; bản
`v5` đã đối chiếu lại provenance cache và là output dùng để xem causal room
label. Đây vẫn là diagnostic phát triển theo prefix, không phải multi-floor
hoặc room-quality benchmark.

## Lệnh tái lập

```bash
python3 scripts/run_causal_hybrid_room_belief.py \
  --timeline artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline \
  --a-zip artifacts/room_belief/rb2_kaggle_diagnostics/full_a_20261015/run_7c43877ac67928df_artifacts.zip \
  --output artifacts/scene_graph_timeline/m4_causal_hybrid_00802_v5

python3 scripts/build_causal_timeline_viewer.py \
  --timeline artifacts/scene_graph_timeline/m0_m1_00802/recorder_room_1/timeline \
  --causal-state artifacts/scene_graph_timeline/m4_causal_hybrid_00802_v5 \
  --output artifacts/scene_graph_timeline/m4_causal_hybrid_00802_v5_viewer \
  --max-points 300

python3 scripts/serve_causal_timeline_viewer.py \
  --root artifacts/scene_graph_timeline \
  --viewer m4_causal_hybrid_00802_v5_viewer \
  --port 8766
```

Mở `http://127.0.0.1:8766/m4_causal_hybrid_00802_v5_viewer/index.html`.
Viewer chỉ đọc artifacts; seek/play không thay đổi predictor hoặc graph nguồn.

## Kết quả đã khóa

- 29 prefix revisions; mỗi revision có `future_observations_used=false`.
- Prefix admission dùng `detection_count >= 3` như eligibility phát triển;
  không biến final filter/merge thành policy online. Hypotheses chưa đủ
  bằng chứng vẫn được giữ với trạng thái provisional nhưng không góp room
  evidence.
- Semantic evidence dùng RB1 controller, bốn biến thể A độc lập và một
  đóng góp cho mỗi unique eligible category. Alias/revisit theo view group
  không nhân support.
- Object states ở prefix: 33 object keys cuối timeline; 6 eligible, 5
  semantic stable và 1 conflicted trong số các object có semantic evidence.

## Membership và room belief

Room snapshot hiện có 155 room nodes và polygon vẫn là 2D XY, không có Z bounds
trên từng room. Tuy nhiên scope của sequence đã được xác minh từ provenance
đã có:

- `effective_evaluation_profile.json` ghi `dataset.floor_id=floor_0`;
- các object nodes của replay snapshot đều có `layer_id=floor_0`;
- 45 accepted P3 detections có `layer_id=floor_0`;
- P2 observations còn lại kế thừa cache-level floor scope của cùng capture;
- replay dùng `NativeRGBDMappingStage -> native M2/M3`, config
  `configs/reference_code_mp3d.yaml`, world metric Z-up.

Do đó v5 dùng scope `verified_single_observed_floor_scope` cho **sequence
quan sát này**. Đây không phải chứng minh an toàn multi-floor.

Không dùng native membership của room replay reference cho hybrid objects.
Membership v5 được tính lại theo greedy voxel support trên cloud causal của
từng revision; support dương chọn winner, zero support là unassigned. Chỉ
membership này mới góp vào RB1 ledger và bốn bảng A. Room polygons vẫn là
XY-only, nên trạng thái này chỉ hợp lệ trong scope floor_0 đã đối chiếu và
không phải bảo đảm multi-floor.

Kết quả revision cuối:

- 30 object hypotheses assigned, 3 unassigned;
- `mean_no_neutral` và `sum_no_neutral`: `room:6 → living_room`, status
  `predicted` theo development thresholds;
- `mean_neutral` và `sum_neutral`: `room:6` vẫn ambiguous;
- các room khác: unknown vì không có đủ eligible category evidence.

Các label trên là output uncalibrated của development policy, chưa phải
room-label accuracy và chưa thay default.

## Viewer

Viewer M2 được mở rộng để tải causal state theo frame, hiển thị object
revision, prefix admission, membership/room-semantic availability, object
lineage và room revision. Bộ chọn variant hiển thị cả bốn bảng A; nhãn dùng
ID ổn định như `Room_6 — living_room` hoặc `Room_2 — unknown (unknown)`.
Geometry nguồn vẫn immutable; sampling tối đa 300 điểm chỉ là display policy.
Chỉ các cạnh `room_contains` do membership causal hợp lệ mới được đưa vào
state hiển thị.

## Kiểm chứng

```bash
python3 -m pytest -q \
  tests/test_causal_hybrid_room_belief.py \
  tests/test_causal_timeline_viewer.py \
  tests/test_hybrid_timeline.py \
  tests/test_room_belief_v1.py
```

Kết quả: **27 passed**. Tests bao gồm prefix không chứa future evidence,
scope gate, category cap/retract/idempotence, exact-pose revisit, snapshot /
viewer revision mapping và các contract RB1 hiện có.

## Giới hạn và bước kế tiếp

M4 đã cung cấp causal membership và room belief theo prefix cho scope quan
sát một elevation đã xác minh; đây vẫn là development static/replay, chưa
phải online live processing hoặc multi-floor guarantee. Không dùng final
room geometry/consensus để giả lập prefix và không tự thay threshold hay A
variant.
