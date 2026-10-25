# Bắt đầu ở đây — workflow Habitat-Sim

Workspace hiện tập trung phát triển trên **Habitat-Sim với HM3D và ReplicaCAD**. Hướng dẫn này dùng CLI có sẵn; không đổi thuật toán hay config baseline.

## Đọc trước

1. [PHASE1_PLAN.vi.md](docs/PHASE1_PLAN.vi.md) — mục tiêu và ranh giới core.
2. [HABITAT_REPLICA_CAD_INSTANCE_STATUS.vi.md](docs/HABITAT_REPLICA_CAD_INSTANCE_STATUS.vi.md) — tích hợp Habitat/ReplicaCAD.
3. [P0_PERCEPTION_STATUS.vi.md](docs/P0_PERCEPTION_STATUS.vi.md), [P1_PERCEPTION_MODEL_DECISION.vi.md](docs/P1_PERCEPTION_MODEL_DECISION.vi.md), [P2_P4_G1_STATUS.vi.md](docs/P2_P4_G1_STATUS.vi.md), [P5_P7_STATUS.vi.md](docs/P5_P7_STATUS.vi.md) — perception checkpoints và giới hạn đánh giá.
4. [REGION_OBJECT_ROOM_LABEL_STATUS.vi.md](docs/REGION_OBJECT_ROOM_LABEL_STATUS.vi.md) — semantic sidecar; không phản hồi sửa segmentation.
5. [GROUND_TRUTH_BUILD_STATUS.vi.md](docs/GROUND_TRUTH_BUILD_STATUS.vi.md) — phạm vi GT, trạng thái annotation/evaluator.
6. [CLEANUP_INVENTORY.vi.md](CLEANUP_INVENTORY.vi.md) và [chỉ mục archive Webots](docs/WEBOTS_ARCHIVE_INDEX.vi.md) — những gì đã archive, giữ lại và cách phục hồi.
7. [RUN_AND_EVALUATE.vi.md](docs/RUN_AND_EVALUATE.vi.md) — quy trình chọn scene/route, preflight, capture, perception, replay, semantic và evaluation bundle.
8. [Evaluation protocol Habitat v1](evaluation/protocol_habitat_v1.json) — định nghĩa scope/threshold/null semantics cho so sánh các run.

## Launcher (CLI gọn)

Các lệnh thao tác scene/run và lệnh tương tác nằm trong [SCENE_LAUNCHER.vi.md](docs/SCENE_LAUNCHER.vi.md). Entry point là `./run_scene.sh`; registry ban đầu có `hm3d_00802` và `replica_apt0`. Bắt đầu bằng `./run_scene.sh maps`, rồi `info`, `check`, `run`. Launcher chỉ điều phối runner hiện có; không thay thuật toán. `check` phải pass trước full run.

## Dữ liệu, cấu hình và môi trường

- HM3D minival v0.2: `artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/`; symlink tiện dụng `artifacts/habitat_data/scene_datasets/hm3d` trỏ vào đây.
- ReplicaCAD assets: `artifacts/habitat_data/versioned_data/replica_cad_dataset/`.
- Dataset receipt/lock và run HM3D đã kiểm tra: `artifacts/runs/hm3d_integration_20261005/`; scene 00802 instance `wcojb4TFT35`.
- Config scene/route: `configs/habitat/`. HM3D route ngắn: `hm3d_minival_v02_00802_smoke.json`; route chính: `hm3d_minival_v02_00802_main_route.json`. ReplicaCAD apt_0 routes nằm ở `m12_replica_cad_apt0_*_route.json`; apt_1/apt_2 holdouts có config riêng.
- Pipeline baseline: `configs/reference_code_mp3d.yaml`; không sửa để đổi scene/route.
- Habitat Python environment hiện có: `/home/kiettran05/miniconda3/envs/habitat/bin/python`. Không nâng cấp environment trong workflow dọn dẹp này.
- Model perception: `artifacts/models/`; weights, dependency prefixes và native binaries đang dùng được giữ nguyên.

Kiểm tra Habitat import trước capture:

```bash
/home/kiettran05/miniconda3/envs/habitat/bin/python -c 'import habitat_sim; print(habitat_sim.__version__)'
```

## Quy trình hiện có: capture → perception → replay → semantic sidecar

Muốn chạy lại từ đầu hoặc thêm route/scene, bắt đầu bằng `docs/RUN_AND_EVALUATE.vi.md`. Chạy `scripts/preflight_habitat_run.py` trước mỗi capture; lệnh này đã kiểm tra thành công cả HM3D 00802 và ReplicaCAD apt_0 với Habitat-Sim 0.3.3, assets/model/native binaries và mọi waypoint/đoạn route trong navmesh.

Có thể dùng launcher thay cho việc gọi từng runner: `./run_scene.sh check hm3d_00802 --route smoke`, tiếp theo `./run_scene.sh run hm3d_00802 --route smoke`. Project Python được cấu hình là `.venv-perception/bin/python` (không đổi sang `/usr/bin/python3`); dependency check và HM3D smoke end-to-end đã pass ngày 2026-10-05. Run smoke được xác minh: `hm3d_00802_20261005T155214.379554Z_657f1e`. Xem [hướng dẫn launcher](docs/SCENE_LAUNCHER.vi.md) để biết package/capability checks và đường dẫn output.

Mỗi thử nghiệm dùng `RUN_ID` riêng để tránh ghi đè. Ví dụ HM3D scene 00802, route smoke:

```bash
cd /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG
RUN_ID="hm3d_00802_$(date +%Y%m%d_%H%M%S)"
RUN_ROOT="artifacts/runs/$RUN_ID"
mkdir -p "$RUN_ROOT"

/home/kiettran05/miniconda3/envs/habitat/bin/python scripts/capture_habitat_replica.py \
  --scene artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/00802-wcojb4TFT35/wcojb4TFT35.basis.glb \
  --config configs/habitat/hm3d_minival_v02_00802_smoke.json \
  --output "$RUN_ROOT/capture"
```

Capture route dài hơn bằng cách đổi sang `hm3d_minival_v02_00802_main_route.json` và output mới. Đây là capture ở các pose quan sát GT cố định, **không phải** robot vật lý/SLAM/planner.

Perception dùng các script hiện có:

```bash
python3 scripts/run_replica_cad_instance_segmentation.py --help
python3 scripts/project_detection_masks_to_3d.py --help
```

P1 tạo instance masks/cache 2D; P3 nâng detection lên 3D từ RGB-D. Giữ chúng thành artifacts trung gian riêng. Dùng capture hiện có thì có thể replay trực tiếp; dùng cache Detection3D đã tạo thì truyền đường dẫn tương ứng:

```bash
PYTHONPATH=.:src MPLCONFIGDIR=/tmp/m12-mpl \
python3 scripts/run_habitat_native_replay.py \
  --capture "$RUN_ROOT/capture" \
  --output "$RUN_ROOT/replay" \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --grid-output "$RUN_ROOT/grid_trace"
```

**Quan trọng:** nếu không truyền `--detection-3d`, replay chạy `detections=absent`: có thể tạo occupancy/regions/room graph hình học và trajectory, nhưng không có ObjectNodes thật từ detector. Để chạy graph có objects, thêm `--detection-3d PATH_TO_detection_3d.jsonl` theo output P3/cache đã khớp cùng sequence/frame IDs.

Semantic sidecar không sửa segmentation:

```bash
python3 scripts/run_region_object_room_labels.py --help
```

Chạy với đúng replay, grid trace, cache detection 2D/3D và capture cùng sequence; ghi output vào `"$RUN_ROOT/semantic"`. Không cấp room GT/navmesh/scene metadata làm predictor evidence.

## Kết quả nên mở đầu tiên

Trong run đã hoàn tất:

1. `capture/capture_manifest.json` — frame IDs, timestamps, sensor/base pose và provenance.
2. `replay/manifest.json` — config/hash hiệu lực, trạng thái pipeline, fresh/reload/delta checks.
3. `replay/spatial_map.png`, `replay/scene_graph.png`, `replay/relationship_graph.png` nếu được xuất.
4. `replay/snapshot.json`, `delta_log.json`, trajectory và per-frame diagnostics.
5. `grid_trace/` — FreeSpaceGrid2D M3 đã tạo trong pipeline; không project thêm chỉ để vẽ.
6. `detection_2d/overlays/`, `detection_3d/` — bằng chứng detection/lifting; `semantic/` — candidate scores/evidence nhãn region.

Một PNG đẹp hoặc số RoomNode không chứng minh phân phòng chính xác. Dùng GT độc lập phù hợp mới báo accuracy; scene semantic labels/navmesh không tự động là room GT.

Smoke gần nhất sau cleanup: `artifacts/runs/hm3d_cleanup_smoke_20261005/`. Nó replay 29 HM3D packets với detection cache đã có; fresh replay, snapshot reload, delta replay và semantic export/reload đều pass. Đây là xác nhận tích hợp, không phải room accuracy.

Evaluation bundles mới theo protocol v1: HM3D diagnostic-only tại `artifacts/runs/hm3d_cleanup_smoke_20261005/evaluation_protocol_v3/`; apt_0 GT từng phần tại `evaluation/runs/apt0_gt_eval_multi_scene_20261005/bundle_v3/`; bảng so sánh tại `evaluation/runs/habitat_comparison_20261005_v3/`. Đọc `report.vi.md` trước, rồi `metrics.csv`, `metrics.json`, `manifest.json` và `failure_samples/index.json`. HM3D room/object GT metrics là null; apt_0 room score chỉ trên bốn polygon đã xác nhận và vùng union GT; functional-room accuracy/adjacency/object-room membership accuracy vẫn null. Hai bundle dùng resolution khác nhau và không được gộp thành một accuracy chung.

## Đổi map/scene an toàn

1. Tạo config mới trong `configs/habitat/`; không sửa config checkpoint.
2. Xác nhận scene asset và dataset config; `--scene` nhận scene GLB nhưng full scene instance cần dataset config/object assets tương ứng.
3. Xác minh trục/tọa độ, scale, intrinsics/FOV, depth semantics/invalid mask, sensor extrinsics và route navigability.
4. Capture vào run mới; kiểm tra manifest, RGB/depth, timestamps, poses.
5. Chạy P1/P3 nếu cần object nodes; sau đó replay và semantic sidecar.
6. Giữ config, manifests, logs, hashes và cache cần thiết. Đổi route/scene/sensor yêu cầu capture mới; chỉ đổi hậu xử lý thì có thể replay capture cũ nếu contract khớp.

## Webots — legacy, không phải workflow phát triển hiện tại

Webots source, configs, tests, fixtures cần thiết và `artifacts/m12_sensor_calibration/` vẫn còn trong workspace vì regression/code còn dùng chúng. Không được xóa hoặc skip tests để gỡ Webots.

Các output lịch sử độc lập đã chuyển sang:

`/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG_webots_archive_20261005`

Tra đường dẫn cũ/checksum/cách phục hồi tại [WEBOTS_ARCHIVE_INDEX.vi.md](docs/WEBOTS_ARCHIVE_INDEX.vi.md). Archive cùng filesystem, chưa giải phóng dung lượng; không chạy trực tiếp từ archive vì một số artifact có thể chứa absolute paths. Muốn xem/replay, phục hồi cây cần thiết về đường dẫn gốc trước.

Cleanup checkpoint trước đó chạy 284 tests. Sau khi bổ sung workflow đánh giá đa scene, full regression mới nhất chạy **287 tests passed**. Xem [cleanup_manifest.json](cleanup_manifest.json) cho bằng chứng archive/cleanup; xem `evaluation_protocol_v3` bundles để biết hash và metrics đánh giá.
