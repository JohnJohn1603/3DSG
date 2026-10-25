# Habitat-Sim + ReplicaCAD — trạng thái tích hợp local

Ngày checkpoint: 2026-09-30

## Phạm vi

Đây là phần tích hợp sau checkpoint M12 Webots. Adapter Habitat nằm riêng
trong `adapters/habitat`; core M1–M10 không import `habitat_sim`. Backend
Webots và profile segmentation `reference/code` không thay đổi.

Đã chạy đúng một scene ReplicaCAD stage-only và một route ngắn bằng pose GT.
Đây là capture/replay offline, không phải robot vật lý, SLAM, Habitat-Lab
task hay benchmark cloud.

## Task 1 — sai khác replay 276 packets

Capture được xác minh là
`artifacts/runs/complete_apartment_keyboard_unsaved_276_source_20260930/capture`:

- 276 packets, timestamp mô phỏng `0.64..176.64 s`, 11.072 physics steps.
- Capture manifest SHA-256:
  `ef8d79c2aa895840ad11c8e6d1dd076a813f07642918e346ffe03b3d5a0f19af`.
- World/config/pipeline và source commits được ghi trong
  `artifacts/runs/complete_apartment_276_replay_checkpoint_20260930/checkpoint.json`.

Đã chạy fresh process cùng capture/config/binary:

- trace ON: 3 lần;
- trace OFF/no-grid: 3 lần;
- so sánh M3 grid, M4 mask/geometry/adjacency, M5, M8 và terminal flush.

Trong phạm vi sáu lần chạy, geometry và relations ổn định; trace không làm
thay đổi kết quả: revision 277, 493 nodes, 2.006 edges, 9 regions/9 rooms,
snapshot reload và delta replay đều pass. Không có khác biệt đầu tiên ở các
lớp được so sánh hoặc terminal flush.

Đây không phải tuyên bố determinism tuyệt đối. Artifact lịch sử
`complete_apartment_276_source_replay_v3` cho kết quả 8 rooms và lệch lần đầu
ở M4 frame 18; artifact đó không có binary fingerprint và được tạo trước
worker/replay build hiện tại. Bằng chứng hiện có phù hợp với build/version
drift, chưa phải native nondeterminism.

Regression liên quan fingerprint: `tests/test_m12_replay_policy.py`.

## Task 2 — môi trường và provenance

- Habitat-Sim `0.3.3`, Python `3.9.23`, renderer software `gpu_device_id=-1`
  (llvmpipe). Không cài package system-wide.
- Scene chính thức ReplicaCAD dataset v1.6, tag `v1.6`, stage-only:
  `frl_apartment_stage.glb`.
- Scene SHA-256:
  `9a28a0511ab15cb48bbd1a6a171798918f24b66e54a8406d69d270938d71d923`.
- Navmesh `apt_0.navmesh` chỉ dùng để kiểm tra `is_navigable`, không đưa
  navmesh hoặc mesh/semantic GT vào builder. Navmesh SHA-256:
  `ba189e458849c0a60967664e52660053833ec70885559d4459859a256ff7e827`.
- Source lock: `locks/sources.json`, SHA-256 trong replay manifest.
- Do môi trường Habitat Python 3.9 và core cần Python mới hơn, capture và
  replay được tách thành hai process: capture chỉ dùng Habitat-Sim + NumPy;
  replay không import Habitat-Sim.

## Kiểm chuẩn sensor độc lập

Artifact:
`artifacts/runs/habitat_replica_cad_stage_20260930_v2/geometry_calibration.json`.

Kết quả pass cho cả 6 frame:

- Habitat Y-up → canonical Z-up: `C.x=H.x`, `C.y=-H.z`, `C.z=H.y`,
  determinant `+1`.
- Camera optical: `+X right, +Y down, +Z forward`.
- Center, right off-axis và upper off-axis pixel được đối chiếu với
  `Simulator.cast_ray()` độc lập; sai số lớn nhất khoảng `9.2e-06 m`.
- Depth được xác minh là metric **axial**:
  `ray_distance * abs(camera_direction_z)`, không phải radial distance.
- RGB raw là RGBA `uint8`; adapter copy và bỏ alpha thành RGB.
- Actual sensor pose metadata khớp pose Habitat với sai số `0`; route có
  dịch chuyển khoảng `9.62 m` và có kiểm tra rotation.

## Capture và replay

Raw capture:
`artifacts/runs/habitat_replica_cad_stage_20260930_v2/capture`.

Replay:
`artifacts/runs/habitat_replica_cad_stage_20260930_v2/replay`.

Grid trace lossless M3:
`artifacts/runs/habitat_replica_cad_stage_20260930_v2/grid_trace`.

Kết quả replay cuối:

- 6 RGB-D frames, revision 7, 97 nodes, 677 edges.
- 2 raw regions và 2 RoomNodes trong fixture này.
- `fresh_replay_equal=true`.
- `snapshot_reload_equal=true`.
- `delta_replay_equal=true`.
- Không detections, không room GT/semantic GT vào builder.
- Graph có 6 trajectory poses; không có dangling/self edges.

Các PNG gồm `spatial_map.png`, `trajectory_rooms.png`, `scene_graph.png` và
`relationship_graph.png`; snapshot, delta log, trajectory CSV/JSON,
per-frame diagnostics và manifest cũng được xuất. Manifest ghi fingerprint
của capture, scene, navmesh, source lock, adapter, replay runner và native
M2/M3 cùng DuDe binaries.

## Lệnh tái lập

Capture trong environment Habitat:

```bash
/home/kiettran05/miniconda3/envs/habitat/bin/python \
  scripts/capture_habitat_replica.py \
  --scene artifacts/habitat_data/versioned_data/replica_cad_dataset/stages/frl_apartment_stage.glb \
  --config configs/habitat/m12_replica_cad_stage_route.json \
  --output artifacts/runs/<RUN_ID>/capture
```

Kiểm chuẩn độc lập:

```bash
/home/kiettran05/miniconda3/envs/habitat/bin/python \
  scripts/validate_habitat_capture_geometry.py \
  --capture artifacts/runs/<RUN_ID>/capture \
  --config configs/habitat/m12_replica_cad_stage_route.json \
  --output artifacts/runs/<RUN_ID>/geometry_calibration.json
```

Replay trong environment core:

```bash
MPLCONFIGDIR=/tmp/habitat-replay-mpl PYTHONPATH=.:src \
  python3 scripts/run_habitat_native_replay.py \
  --capture artifacts/runs/<RUN_ID>/capture \
  --output artifacts/runs/<RUN_ID>/replay \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --grid-output artifacts/runs/<RUN_ID>/grid_trace
```

Regression đã chạy sau tích hợp:

```bash
PYTHONPATH=.:src pytest -q \
  tests/test_habitat_adapter.py tests/test_m12_replay_policy.py
```

Kết quả: **8 passed**; các file capture/replay/geometry cũng đã được
`py_compile` bằng đúng Python 3.9 cho nhánh Habitat và Python core cho
nhánh replay.

Full regression sau toàn bộ thay đổi: **245 passed trong 105,76 s**, không
skip. Con số này bao gồm regression replay fingerprint và test contract
Habitat; không thay đổi Webots backend.

## Giới hạn và kết luận

Mốc kỹ thuật local đã đạt: ReplicaCAD raw RGB-D + actual sensor/base pose
→ canonical FramePacket → native M1–M10 → export/reload/replay. Kết quả
2 rooms chỉ là output của một stage-only scene và route ngắn; không phải
độ chính xác phân phòng. Scene chưa có annotation phòng độc lập trong
artifact này, nên không báo IoU/accuracy.

Chưa thực hiện: detector, SLAM, robot vật lý, Habitat-Lab task, nhiều scene,
Kaggle/cloud và chứng minh hiệu năng realtime. Webots backend vẫn giữ nguyên.
