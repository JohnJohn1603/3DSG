# Habitat-Sim + ReplicaCAD scene-instance — trạng thái Bước 2

Ngày kiểm tra: 2026-10-01  
Phạm vi: tiếp tục từ checkpoint Habitat stage-only đã chạy; không thay đổi
backend Webots, source segmentation profile, detector, SLAM hoặc core M1–M10.

## Kết luận ngắn

Đã chạy được capture RGB-D offline từ **full ReplicaCAD scene instances** qua
dataset config chính thức, sau đó replay cùng capture qua
`M1 → native M2 → native M3 → pinned DuDe/M5 → M6–M10`.

Ba biến thể kiến trúc/nội thất đã chạy riêng, cùng profile cảm biến và cùng
ngân sách quan sát 12 frame:

| Scene | Object instances | Articulated declarations | Frames | Final regions | Final RoomNodes | Nodes | Edges | Replay checks |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `apt_0` | 113 | 6 | 12 | 6 | 5 | 151 | 956 | fresh/reload/delta = pass |
| `apt_1` | 120 | 6 | 12 | 6 | 5 | 152 | 961 | fresh/reload/delta = pass |
| `apt_2` | 119 | 6 | 12 | 6 | 5 | 151 | 969 | fresh/reload/delta = pass |

Đây là bằng chứng **tích hợp và khả năng tái lập kỹ thuật**, không phải bằng
chứng accuracy phân phòng. Chưa tìm thấy room annotation độc lập phù hợp;
không báo IoU, precision/recall hoặc kết luận rằng 5 RoomNodes là 5 phòng
đúng. Semantic object IDs và navmesh không đi vào builder.

## Provenance và baseline

- Habitat-Sim: `0.3.3`, Python capture environment `3.9.23`, renderer
  software (`gpu_device_id=-1`, llvmpipe).
- ReplicaCAD dataset: official Habitat-Sim `ReplicaCAD_dataset` v1.6, tag
  `v1.6`, repository commit
  `6be07e532c83c5799a6e96152f926d07838c2084`.
- Dataset license ghi trong artifact: **CC BY 4.0**; các run này chỉ dùng
  local theo provenance đã lưu.
- Dataset config:
  `artifacts/habitat_data/versioned_data/replica_cad_dataset/replicaCAD.scene_dataset_config.json`
  SHA-256:
  `a51d4fe5e1853e616c13710930191bad33c0b277edece7c4e520d1194831f5e3`.
- Full scene instance hashes:
  - `apt_0.scene_instance.json`:
    `7935087e35b8c0fb23d3503749eaa8b5b766cf44dc21c20ce70b79a64e8651fe`
  - `apt_1.scene_instance.json`:
    `6d718c52d7cdfc24e5c8ed1325e6633e821eeb33e29ec5fb7408f912347242ea`
  - `apt_2.scene_instance.json`:
    `d39c9a5337fb7ff124f83dc7989a40645ff9cf36604bfe34437f8d108535f55e`
- Stage asset used by the prior sensor checkpoint:
  `frl_apartment_stage.glb`, SHA-256
  `9a28a0511ab15cb48bbd1a6a171798918f24b66e54a8406d69d270938d71d923`.
- Source segmentation profile: `reference/code`, config
  `configs/reference_code_mp3d.yaml`; no per-scene tuning.
- Captured assets contain no remaining Git-LFS pointer files after the local
  v1.6 download. Run manifests keep scene/config/navmesh/capture hashes.

Mỗi scene instance được nạp bằng dataset config và inventory validator ghi:
stage template, object template list, object-instance count, articulated
declarations và preview RGB/depth đầu-cuối. Ví dụ `apt_0` có 113 object
instances, 80 unique object templates và 6 articulated declarations.

## Sensor/capture evidence

Raw capture và preview nằm trong từng thư mục `capture/` và
`instance_validation/` của run. Tất cả ba run có:

- RGB `120×160×4` raw từ Habitat; adapter dùng RGB channel contract đã pin.
- Depth metric axial, đơn vị mét, `120×160`, valid mask và timestamp 0.0–1.1 s.
- Actual per-frame Habitat sensor state; không thay bằng base pose.
- Cùng intrinsics/FOV/extrinsics profile với stage-only checkpoint.
- Route có đổi hướng và đi qua các connector/side-room waypoint; mọi waypoint
  và từng đoạn route được kiểm tra `is_navigable` bằng navmesh.

Sensor geometry checkpoint **độc lập** trước đó pass với center/off-axis
points, pose dịch chuyển/quay, optical frame, axial depth và RGB channel
conversion; sai số lớn nhất khoảng `9.2e-06 m` trên stage-only raycast.

Không gộp kết quả đó với kiểm tra full-scene: `geometry_calibration.json`
trong run full instance có thể báo fail một số pixel vì validator stage-only
không chứa hình học/occlusion của object instances. Raw RGB/depth shape,
valid-mask, manifest timestamp và native replay vẫn pass. Vì vậy không gọi
full-scene validator là calibration pass, cũng không coi mismatch đó là lỗi
core khi chưa có raycast oracle đầy đủ cả scene.

## Native replay evidence

Artifacts chính:

- `artifacts/runs/habitat_replica_cad_apt0_instance_20261001/`
- `artifacts/runs/habitat_replica_cad_apt1_instance_20261001/`
- `artifacts/runs/habitat_replica_cad_apt2_instance_20261001/`

Mỗi run có `capture/`, `instance_validation/`, `grid_trace/`, `replay/`,
stdout/stderr và manifest. `replay/` gồm snapshot, delta log, trajectory
CSV/JSON, per-frame diagnostics, `spatial_map.png`, `trajectory_rooms.png`,
`scene_graph.png` và `relationship_graph.png`.

| Scene | Runtime native replay (ms) | M4 branches | M4 status | Mapping/replay |
|---|---:|---|---|---|
| `apt_0` | 81645.93 | 8 incremental, 4 reuse-adjusted | 12 success | fresh/reload/delta pass |
| `apt_1` | 79281.85 | 9 incremental, 3 reuse-adjusted | 12 success | fresh/reload/delta pass |
| `apt_2` | 77797.36 | 8 incremental, 4 reuse-adjusted | 12 success | fresh/reload/delta pass |

Runtime trên là replay wall-clock của process native hiện tại, không phải
real-time benchmark và không bao gồm một claim về Habitat physics. Tất cả
run dùng `detections=absent`; vì vậy không có ObjectNodes từ nhận dạng RGB.

## Scene instance, articulation và room annotations

Scene instance declaration có stage và object/articulated-object entries, và
đã được nạp qua dataset config. Tuy nhiên build Habitat-Sim hiện tại phát:
`PhysicsManager ... Not implemented ... Install with --bullet` khi tạo 6
articulated object types (`fridge`, `kitchen_counter`, `kitchenCupboard_01`,
`chestOfDrawers_01`, `cabinet`, `door2`). Do đó:

- static scene/object assets và declaration inventory đã được kiểm chứng;
- articulated objects chưa được kiểm chứng về dynamics/open-close physics;
- đây là giới hạn build local thiếu Bullet, không phải lý do để đưa object
  metadata vào room builder.

Habitat cũng báo scene active không có semantic annotations dạng room. Các
`semantic_id` trong scene instance chỉ là semantic object metadata. Navmesh
chỉ dùng để kiểm tra waypoint/navigability, không phải room GT và không được
đưa vào M1–M10.

Vì chưa có room polygons/annotation room độc lập đã kiểm tra, kết quả
`regions_final=6`, `rooms_final=5` chỉ là output graph của route này. Không
đánh giá đó là số phòng đúng, không tính accuracy.

## Lệnh tái lập

Capture thay `apt_0` bằng `apt_1` hoặc `apt_2`:

```bash
HAB=/home/kiettran05/miniconda3/envs/habitat/bin/python
RUN=artifacts/runs/habitat_replica_cad_apt0_instance_20261001
$HAB scripts/capture_habitat_replica.py \
  --scene artifacts/habitat_data/versioned_data/replica_cad_dataset/configs/scenes/apt_0.scene_instance.json \
  --config configs/habitat/m12_replica_cad_apt0_instance_route.json \
  --output "$RUN/capture"

PYTHONPATH=.:src python3 scripts/validate_replica_cad_instance_capture.py \
  --capture "$RUN/capture" \
  --scene-instance artifacts/habitat_data/versioned_data/replica_cad_dataset/configs/scenes/apt_0.scene_instance.json \
  --output "$RUN/instance_validation"

MPLCONFIGDIR=/tmp/habitat-replay-mpl PYTHONPATH=.:src \
python3 scripts/run_habitat_native_replay.py \
  --capture "$RUN/capture" \
  --output "$RUN/replay" \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --grid-output "$RUN/grid_trace"
```

Kiểm tra kết quả:

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path("artifacts/runs/habitat_replica_cad_apt0_instance_20261001/replay/manifest.json")
x = json.loads(p.read_text())
for k in ("pipeline_status", "frames", "revision", "regions_final", "rooms_final",
          "fresh_replay_equal", "snapshot_reload_equal", "delta_replay_equal"):
    print(k, x[k])
PY
```

Regression sau tích hợp validator hiện tại:

```bash
PYTHONPATH=.:src pytest -q
```

Không có upload cloud/Kaggle, không có detector/SLAM/Habitat-Lab task, và
không có tuning riêng từng scene trong lượt này.

## Phạm vi nghiệm thu

Đã đạt:

- full scene instance + dataset config được load và inventory được xác minh;
- raw RGB/depth/metadata capture được lưu;
- cùng core native M1–M10 chạy qua ba biến thể;
- grid trace, snapshot, delta, trajectory, PNG và diagnostics có mặt;
- fresh replay, snapshot reload, delta replay và graph invariants pass;
- backend Webots không bị thay đổi.

Chưa đạt/chưa kết luận:

- articulated-object physics do thiếu Bullet;
- live Habitat physics/robot, SLAM, detector;
- room annotation/accuracy và robustness statistical;
- benchmark realtime hoặc nhiều route/scene hơn ngân sách 12 frame;
- claim rằng ranh giới/adjacency 5 RoomNodes đúng theo kiến trúc.

Đây là **technical integration/reference-parity checkpoint cho full
ReplicaCAD scene instances**, chưa phải nghiệm thu chất lượng room
segmentation.
