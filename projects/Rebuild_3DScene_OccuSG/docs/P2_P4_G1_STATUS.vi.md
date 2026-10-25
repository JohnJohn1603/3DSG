# P2–P4 / G1 — object perception và membership

Ngày: 2026-10-01  
Trạng thái: **G1 đạt trong development re-observation fixture; holdout chỉ là technical smoke**

## P2 — detector/cache

Model/config đã khóa ở P1:

- `artifacts/models/ultralytics/yolo11n-seg.onnx`, SHA-256
  `0bc32bc92e985b881141ef9bd2216e2a746f70519d0d24da9fc85decc4428cf4`;
- CPU ONNX Runtime `1.24.4`, profile
  `configs/perception/yolo11n_seg_replica_cad_dev.json`;
- RGB, letterbox `640×640`, mask unletterbox về ảnh gốc, threshold `0.25`
  development assumption, NMS `0.45`, mask `0.50`;
- COCO vocabulary và synonym map nằm trong config; không dùng object/room GT.

P2 script:
`scripts/run_replica_cad_instance_segmentation.py`.

Development cache trên capture 16 frame `160×120` có 25 Detection2D; cache
hi-res `320×240` có 25 Detection2D. Mask lưu `.npy` lossless với checksum,
frame/timestamp/model/preprocess fingerprint. Lỗi inference không bị biến
thành empty cache.

## P3 — mask + observed depth → Detection3D

Script `scripts/project_detection_masks_to_3d.py` gọi trực tiếp
`standalone_sg.geometry.backproject_depth`, dùng depth axial metric + valid
mask, rồi source-style Euclidean cluster (`eps=0.02 m`, min 100, max
2,500,000, largest cluster, visible AABB center). Mỗi kết quả có evidence
ledger, camera pose, mask reference, valid-depth count và cluster count.

Kết quả cho thấy độ phân giải là giới hạn thực tế của fixture, không phải
thay đổi mapping:

| capture | resolution | Detection2D | Detection3D accepted | rejected P3 |
|---|---:|---:|---:|---:|
| `apt_0_perception_dev` | 160×120 | 25 | 2 | 23 |
| `apt_0_perception_dev_hires` | 320×240 | 25 | 13 | 12 |
| `apt_0_perception_reobserve` | 320×240 | 14 | 6 | 8 |

Ở 160×120 nhiều mask có hàng trăm depth pixel nhưng cluster lớn nhất chỉ có
1–24 điểm dưới `eps=0.02`; không hạ min cluster trong reference profile.

## P4 — G1 native graph

`scripts/run_habitat_native_replay.py` nhận cache P3 qua option
`--detection-3d`; không có option thì semantics vẫn là `detections=absent`.
Empty batch và provider absent được giữ khác nhau. M1–M3, DuDe/M5, M6, M7,
M8, M9, M10 không bị fork.

Development re-observation run:

`artifacts/runs/habitat_replica_cad_apt0_perception_reobserve_20261001/`

- 5 frames, 6 Detection3D accepted;
- `replay_p4/manifest.json`: pipeline pass, revision 6, 107 nodes, 694 edges;
- 4 ObjectNodes cuối, trong đó `refrigerator` được nhìn ở frame 0, 2 và 3;
  M6 giữ một persistent ID (`object-000001`), không tạo duplicate;
- object nodes có room membership (`room:2` trong diagnostics);
- `fresh_replay_equal=true`, snapshot reload và delta replay pass;
- no dangling/self edges; room GT, scene object IDs và navmesh không vào builder.

Đây là G1 technical acceptance trên development fixture. Effective M6 runtime
của replay là `same_class_radius_m=0.75 m` và
`same_pose_suppression_cap_m=0.15 m`, lấy từ `configs/reference_code_mp3d.yaml`
và được ghi lại trong `replay_p4/manifest.json`; block association trong
perception profile chỉ là metadata P2/P3, không được runner đọc. Giới hạn vẫn
phải giữ: first-view center bias, M6 false merge trong bán kính `0.75 m`, false
split khi class/geometry thay đổi, và chất lượng detector chưa có annotation
2D độc lập.

Holdout inference/replay cùng profile:

- `apt_1`: 12 Detection2D, 1 Detection3D; replay pass, fresh/reload/delta pass;
- `apt_2`: 24 Detection2D, 2 Detection3D; replay pass, fresh/reload/delta pass.

Hai holdout này chứng minh đường chạy kỹ thuật, chưa đủ để gọi robustness hay
accuracy vì route ngắn và không có room/object annotation phù hợp.

## Lệnh tái lập G1

```bash
RUN=artifacts/runs/habitat_replica_cad_apt0_perception_reobserve_20261001

./.venv-perception/bin/python scripts/run_replica_cad_instance_segmentation.py \
  --capture "$RUN/capture" --output "$RUN/detection_2d" \
  --config configs/perception/yolo11n_seg_replica_cad_dev.json

PYTHONPATH=.:src python3 scripts/project_detection_masks_to_3d.py \
  --capture "$RUN/capture" --detection-2d "$RUN/detection_2d" \
  --output "$RUN/detection_3d" \
  --profile configs/perception/yolo11n_seg_replica_cad_dev.json

MPLCONFIGDIR=/tmp/habitat-replay-mpl PYTHONPATH=.:src \
python3 scripts/run_habitat_native_replay.py \
  --capture "$RUN/capture" --output "$RUN/replay_p4" \
  --grid-output "$RUN/grid_trace_p4" \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --detection-3d "$RUN/detection_3d/detection_3d.jsonl"
```

Không chạy room labeler trước khi G1 được kiểm tra; G1 hiện đã có checkpoint
riêng. P5/P6 được ghi ở status tiếp theo.

## Xác minh sau checkpoint

- `python3 -m py_compile` cho các script P2–P4 và semantic pass.
- Nhóm targeted regression: **18 passed trong 2,73 s**.
- Full regression: **251 passed trong 85,97 s** (`PYTHONPATH=.:src pytest -q`).
- Không thay đổi mapping/segmentation/M6; đường Webots hiện có vẫn nằm trong
  full regression.
