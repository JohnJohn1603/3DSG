# Chạy và đánh giá Habitat-Sim — hướng dẫn thực hành

Workflow này dùng các runner hiện có cho HM3D/ReplicaCAD, giữ nguyên M1–M10, detector, P3/M6 và semantic rules. Hai loại kết quả được tách biệt: `diagnostic_only` (không có GT phù hợp) và `gt_evaluation` (chỉ chấm các nhãn GT đã xác nhận). Không dùng số RoomNode, test pass hay navmesh làm accuracy.

Muốn dùng lệnh ngắn thay vì gọi runner theo từng stage, xem [Habitat Scene Launcher](SCENE_LAUNCHER.vi.md): `./run_scene.sh maps → info → check → run`. Launcher dùng cùng configs/runner bên dưới và dừng khi output contract hoặc một stage không pass.

## 1. Map nào có thể dùng ngay?

Hiện đã có cấu hình kiểm chứng cho:

| Bộ dữ liệu | Scene/floor | Profile đánh giá | Route | Phạm vi GT |
|---|---|---|---|---|
| HM3D minival v0.2 | `00802-wcojb4TFT35`, floor_0 | `configs/evaluation/hm3d_00802_scene_eval_v1.json` | `configs/habitat/hm3d_minival_v02_00802_main_route.json` | Diagnostic only. Capture lưu RGB-D/pose nhưng `semantic_gt_saved=false`; semantic GLB/TXT không được xem là room/object-instance GT. |
| ReplicaCAD v1.6 | `apt_0`, floor_0 | `configs/evaluation/replica_cad_apt0_scene_eval_v1.json` | `configs/habitat/m12_replica_cad_apt0_perception_dev_long_route.json` | Room boundary trên bốn polygon đã xác nhận; object mask dùng evaluator apt_0 hiện hành. Functional labels, doorway adjacency và object-room GT chưa xác nhận. |

Đường dẫn dataset đã cài nằm trong `artifacts/habitat_data/versioned_data/`; symlink tiện dụng nằm trong `artifacts/habitat_data/scene_datasets/`. **Không chép mesh/dataset vào mỗi run, không tải map khác trong hướng dẫn này.** HM3D có điều kiện license phi thương mại; đọc ghi chú license trong config trước khi chia sẻ hình/kết quả.

## 2. Môi trường và preflight bắt buộc

Làm việc từ workspace:

```bash
cd /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG
HAB_PY=/home/kiettran05/miniconda3/envs/habitat/bin/python
```

Ví dụ preflight HM3D (đổi RUN_ID mỗi lượt; đường dẫn đích phải chưa tồn tại):

```bash
RUN_ID="hm3d_00802_$(date +%Y%m%d_%H%M%S)"
RUN_ROOT="artifacts/runs/$RUN_ID"
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/preflight_habitat_run.py \
  --profile configs/evaluation/hm3d_00802_scene_eval_v1.json \
  --run-root "$RUN_ROOT" \
  --habitat-python "$HAB_PY"
```

Preflight đọc config/đường dẫn, kiểm tra Habitat import/version, weights và hash khóa, hai native binaries, GT/profile tương thích, output không tồn tại; nếu route yêu cầu navigability, nó nạp scene và navmesh rồi kiểm tra mọi pose cùng mọi đoạn nối waypoint. `status` phải là `pass`. Đây là kiểm tra đường đi trên navmesh, **không phải kiểm tra collision vật lý, room GT hay đầu vào predictor**. `--skip-route-validation` chỉ dành cho kiểm tra asset sơ bộ; khi dùng nó không được tuyên bố route đã kiểm tra.

ReplicaCAD:

```bash
RUN_ID="replica_apt0_$(date +%Y%m%d_%H%M%S)"
RUN_ROOT="artifacts/runs/$RUN_ID"
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/preflight_habitat_run.py \
  --profile configs/evaluation/replica_cad_apt0_scene_eval_v1.json \
  --run-root "$RUN_ROOT" \
  --habitat-python "$HAB_PY"
```

Nếu preflight pass, mới tạo thư mục run:

```bash
mkdir -p "$RUN_ROOT"
```

## 3. Chọn map và chỉnh route

Không có route editor GUI trong workflow hiện tại. Route được khai báo trong JSON; có thể sao chép route config thành file mới dưới `configs/habitat/` rồi sửa JSON — không sửa controller/Python. Giữ bản gốc và baseline config nguyên vẹn.

Mỗi phần tử `route` là một lần đặt pose quan sát:

```json
{"position_habitat": [x, y, z], "yaw_rad": 1.570796, "label": "look_into_room"}
```

- `position_habitat` dùng trục Habitat: X/Z nằm ngang, **Y là độ cao**; đơn vị mét.
- `yaw_rad` quay quanh trục đứng Habitat +Y, tính bằng radian. `0`, `π/2`, `π`, `-π/2` là bốn hướng quay quanh đứng; hãy kiểm tra bằng ảnh preview/capture vì dấu hướng phụ thuộc frame Habitat của scene.
- Nhiều waypoint có cùng vị trí nhưng yaw khác nhau nghĩa là robot/camera được đặt tại chỗ và chỉ quay để chụp nhiều hướng. Đây không phải chuyển động vật lý giữa các frame.
- `require_navigable`, `scene_dataset_config`, `navmesh_file`, sensor calibration, resolution/FOV/depth range và `simulation_step_s` cần được giữ đồng nhất giữa capture và route validator.
- HM3D capture nhận `--scene` trỏ tới `.basis.glb`; ReplicaCAD full-scene capture nhận `--scene` trỏ tới `apt_0.scene_instance.json` cùng ReplicaCAD scene dataset config.

Khi thêm scene đã tải sẵn, tạo route config mới: đổi scene ID/provenance, scene file/instance, scene dataset config, navmesh, route, sequence/frame prefix và output run. Không đổi pipeline/perception/semantic profile nếu mục tiêu là so sánh cùng điều kiện. Chỉ capture scene mới sau khi preflight, xem RGB/depth và xác nhận tọa độ/độ cao sensor.

## 4. Smoke RGB-D rồi capture

Trước khi đi qua pipeline, kiểm tra capture nhỏ: mở PNG/preview đã xuất nếu có; xác nhận RGB có đúng cảnh, depth có đơn vị mét/invalid mask, timestamps tăng, sensor/base pose đồng bộ. Runner hiện lấy pose GT đặt sẵn trong route; không có agent tự đi, planner hay SLAM.

Capture HM3D 00802, dùng một `RUN_ROOT` mới đã preflight:

```bash
"$HAB_PY" -B scripts/capture_habitat_replica.py \
  --scene artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/00802-wcojb4TFT35/wcojb4TFT35.basis.glb \
  --config configs/habitat/hm3d_minival_v02_00802_main_route.json \
  --output "$RUN_ROOT/capture"
```

ReplicaCAD apt_0:

```bash
"$HAB_PY" -B scripts/capture_habitat_replica.py \
  --scene artifacts/habitat_data/versioned_data/replica_cad_dataset/configs/scenes/apt_0.scene_instance.json \
  --config configs/habitat/m12_replica_cad_apt0_perception_dev_long_route.json \
  --output "$RUN_ROOT/capture"
```

Capture ghi raw `rgb.npy`, `depth_m.npy`, `valid_mask.npy`, metadata và `capture_manifest.json`. Tuyệt đối không dùng lại cùng output path: một số runner chấp nhận thư mục tồn tại và có thể ghi đè nội dung bên trong; tính duy nhất của `RUN_ROOT` là hàng rào an toàn.

## 5. Perception/lifting, replay và semantic sidecar

P1 instance masks và overlays:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-habitat-mpl python3 -B scripts/run_replica_cad_instance_segmentation.py \
  --capture "$RUN_ROOT/capture" \
  --output "$RUN_ROOT/detection_2d" \
  --config configs/perception/yolo11n_seg_replica_cad_dev.json \
  --weights artifacts/models/ultralytics/yolo11n-seg.onnx \
  --names configs/perception/coco.names
```

P3 nâng masks lên Detection3D, profile mặc định/stride 1:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-habitat-mpl python3 -B scripts/project_detection_masks_to_3d.py \
  --capture "$RUN_ROOT/capture" \
  --detection-2d "$RUN_ROOT/detection_2d" \
  --output "$RUN_ROOT/detection_3d" \
  --profile configs/perception/yolo11n_seg_replica_cad_dev.json
```

Replay native M1–M10 với detections thật, xuất grid trace đúng grid M3 đã tạo (không project thêm):

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-habitat-mpl python3 -B scripts/run_habitat_native_replay.py \
  --capture "$RUN_ROOT/capture" \
  --detection-3d "$RUN_ROOT/detection_3d/detection_3d.jsonl" \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --output "$RUN_ROOT/replay" \
  --grid-output "$RUN_ROOT/grid_trace"
```

Không truyền `--detection-3d` thì detections là `absent`: vẫn có thể tạo occupancy/room geometry nhưng không có ObjectNodes do detector cung cấp. Dùng đúng file Detection3D có cùng frame IDs/capture; không trộn cache của run khác.

Semantic sidecar (chỉ đọc replay/cache; không sửa segmentation):

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-habitat-mpl python3 -B scripts/run_region_object_room_labels.py \
  --replay "$RUN_ROOT/replay" --trace "$RUN_ROOT/grid_trace" \
  --detection-3d "$RUN_ROOT/detection_3d" --detection-2d "$RUN_ROOT/detection_2d" \
  --capture "$RUN_ROOT/capture" \
  --config configs/semantics/region_object_room_rules_replica_cad_v1.json \
  --output "$RUN_ROOT/semantic"
```

Room label là dự đoán theo object evidence, không phải nhãn GT. Không đưa mesh semantic, navmesh, room polygon hoặc scene annotations vào capture, perception, replay hay semantic predictor.

## 6. Đánh giá một run

Tạo evaluation bundle mới, ngoài thư mục capture/replay:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-habitat-mpl python3 -B scripts/evaluate_habitat_run.py \
  --capture "$RUN_ROOT/capture" --replay "$RUN_ROOT/replay" \
  --grid-trace "$RUN_ROOT/grid_trace" --semantic "$RUN_ROOT/semantic" \
  --run-config configs/habitat/hm3d_minival_v02_00802_main_route.json \
  --evaluation-profile configs/evaluation/hm3d_00802_scene_eval_v1.json \
  --output "$RUN_ROOT/evaluation" --run-id "$RUN_ID"
```

Với HM3D hiện tại, để `room_macro_iou`, object GT precision/recall, semantic accuracy, membership accuracy và adjacency là `null`: không có GT tương thích đã lưu. `semantic.glb/.txt` không bù được việc capture không lưu semantic instance IDs. Vẫn đọc được số frame, valid-depth, trajectory, M3 area/counts, runtime có sẵn, rejects/stale, graph invariants và các replay assertions.

Với apt_0, room score chỉ hợp lệ khi cùng canonical floor/frame và dùng polygon GT đã xác nhận. Ví dụ đúng với capture apt0 đã khóa:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-habitat-mpl python3 -B scripts/evaluate_habitat_run.py \
  --capture artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/capture \
  --replay artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/replay \
  --grid-trace artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/grid_trace \
  --semantic artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/semantic \
  --run-config configs/habitat/m12_replica_cad_apt0_perception_dev_long_route.json \
  --evaluation-profile configs/evaluation/replica_cad_apt0_scene_eval_v1.json \
  --room-gt evaluation/gt/apt_0/versions/apt0_room_gt_user_confirmed_20261004_normalized_v1/room_polygons_normalized.geojson \
  --object-evaluation evaluation/runs/apt0_gt_eval_multi_scene_20261005/metrics.json \
  --output evaluation/runs/apt0_gt_eval_multi_scene_20261005/bundle_next_unique \
  --run-id replica_cad_apt0_dev_long_bundle_next_unique
```

**Object-evaluation JSON trên chỉ thuộc capture apt_0 đã được evaluator khóa.** Không gắn nó vào capture/route mới: evaluator `evaluate_apt0_ground_truth.py` hiện chưa nhận capture tùy ý. Route mới vẫn cho diagnostic + room polygon scoring nếu GT hợp lệ; object mask quality sẽ null cho tới khi evaluator hỗ trợ chính capture đó. `object_mask_recall` hiện là observation-level, class-agnostic mask recall theo evaluator apt0 — không phải recall YOLO trong vocabulary, không phải unique-instance coverage và không phải AP/mAP.

Bundle chứa `manifest.json` (input/output hashes, protocol, provenance), `metrics.json` (giá trị, đơn vị, denominator/sample count, scope, GT status/threshold/reason), `metrics.csv` (một dòng cho run), `report.vi.md`, `failure_samples/index.json`, `membership_and_labels.json`. M3 cell count dùng semantics contract của grid; occupation score không được diễn giải như probability. Observed-area ratio vẫn null nếu thiếu ROI độc lập.

## 7. So sánh nhiều run

So sánh bundle bằng cùng comparator; nó giữ riêng các nhóm khác protocol, GT status/scope, thresholds, resolution/FOV, pipeline/detector hash hoặc P3 settings. Không pool p50/p95, không biến null thành zero. Mỗi run là một dòng CSV; compatible aggregate báo cả macro theo scene và pooled ratio từ tử/mẫu nếu có.

Ví dụ so hai bundle đã tạo trong checkpoint hiện tại:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/compare_habitat_evaluations.py \
  --evaluations \
    artifacts/runs/hm3d_cleanup_smoke_20261005/evaluation_protocol_v3 \
    evaluation/runs/apt0_gt_eval_multi_scene_20261005/bundle_v3 \
  --output evaluation/runs/habitat_comparison_NEXT_UNIQUE
```

Hai run này không tạo thành phép so accuracy trực tiếp: resolutions khác nhau (160×120 so với 320×240), dataset/scene khác, HM3D không có GT còn apt0 có GT từng phần. Các metrics sẽ được bucket riêng theo điều kiện tương thích. Cần thêm nhiều scene/capture và GT cùng protocol mới có macro đa-scene có ý nghĩa.

## 8. Đọc lỗi và giới hạn số liệu

- RGB và dự đoán 2D: `detection_2d/overlays/`; masks lossless: `detection_2d/masks/`.
- P3 rejection và lý do: `detection_3d/rejection_overlays/`, `object_evidence_ledger.jsonl`, `detection_3d_manifest.json`.
- Mapping/rooms: `grid_trace/`, `replay/spatial_map.png`, `trajectory_rooms.png`, `snapshot.json`, `delta_log.json`, `frame_diagnostics.json`.
- Objects/labels: `replay/scene_graph.png`, `relationship_graph.png`, `semantic/`.
- Đầu tiên xem `capture_manifest.json`, sau đó replay manifest/diagnostics, rồi overlays và evaluation `failure_samples/index.json`.

Runtime báo từ diagnostics hiện là M1, M2, M3 và combined builder update. M4/M5/graph riêng, detector warm-up, export/render và live latency chưa được instrument riêng. `ru_maxrss` là RSS process Python ghi nhận, không phải peak đồng thời của cả process tree. Route hiện là fixed GT-pose sampled observation route; không gọi runtime đó là robot realtime/FPS. Snapshot reload/delta replay chỉ pass khi manifest nói rõ; fresh-process equality là `not_run` nếu không được thực hiện.

## 9. GT và dữ liệu không được trộn/xóa

GT polygon/masks chỉ thuộc evaluator. Trạng thái uncertain/unsupported/unmapped không được coi như nhãn đúng/sai tùy tiện. Chưa có functional room labels, doorway/adjacency và object-room assignment GT độc lập đủ điều kiện thì các metric ấy phải null. Navmesh chỉ dùng route validation và không phải room GT.

Giữ nguyên HM3D/ReplicaCAD assets, model weights, env/native binaries, GT gốc/normalized, run/checkpoint, P3 evidence cache, route/config và manifests. Đừng xóa bất kỳ `artifacts/` hoặc `evaluation/gt/` tổng thể nào. Các run Webots lịch sử đã archive; chi tiết ở `docs/WEBOTS_ARCHIVE_INDEX.vi.md`.
