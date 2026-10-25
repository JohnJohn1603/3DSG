# P0 — dữ liệu và kiểm chuẩn perception/room semantics

Ngày: 2026-10-01  
Trạng thái: **đạt checkpoint kỹ thuật P0; chưa có room-accuracy claim**

## Phạm vi và bảo toàn dữ liệu

Ba capture full ReplicaCAD ban đầu được giữ nguyên, không ghi đè:

| split | capture/checkpoint | vai trò |
|---|---|---|
| `apt_0` | `artifacts/runs/habitat_replica_cad_apt0_instance_20261001/` | development perception |
| `apt_1` | `artifacts/runs/habitat_replica_cad_apt1_instance_20261001/` | holdout cấu hình perception |
| `apt_2` | `artifacts/runs/habitat_replica_cad_apt2_instance_20261001/` | holdout cấu hình perception |

Các run trên giữ cùng source segmentation profile và không đưa object/room
semantic GT vào builder. `apt_1` và `apt_2` là holdout của cùng development
profile, không phải ba kiến trúc độc lập.

Dev capture bổ sung, route riêng, được lưu tại:

`artifacts/runs/habitat_replica_cad_apt0_perception_dev_20261001/`

Capture này có 16 RGB-D frames, timestamp `0.0..1.5 s`, các pose nhìn đổi
hướng quanh vùng sofa/bàn và route quay lại. Nó được tạo vì checkpoint 12
frame trước chưa đủ bằng chứng quan sát đa pose cho perception. Scene/object
metadata chỉ dùng để thiết kế route và kiểm tra instance, không được truyền
thành detection vào pipeline.

## Full-scene calibration gap

Đối chứng stage-only cũ không được dùng để kết luận đúng/sai tại pixel bị
object che khuất. Validator mới dùng **fresh full-scene Habitat-Sim render**
từ cùng scene instance đã load qua dataset config:

`artifacts/runs/habitat_replica_cad_p0_fullscene_validation_20261001/geometry_calibration.json`

Kết quả: `pass=true`, 12/12 frame full-frame depth match, maximum absolute
depth error `0.0`, valid-mask XOR `0`, sensor pose errors `0`. Đây là kiểm tra
tính nhất quán của capture với render đầy đủ scene/occlusion; nó không thay
thế kiểm chuẩn hình học độc lập stage-only đã pass trước đó.

Đã kiểm chuẩn:

- sensor pose/extrinsics và world transform trên stage-only calibration;
- depth metric axial, valid mask, RGB shape/channel và timestamp;
- capture full-scene khớp fresh render đầy đủ object instances;
- scene instance inventory, stage/object templates và route navigability.

Chưa kiểm chứng:

- articulated-object dynamics vì build local thiếu Bullet;
- room annotations độc lập phù hợp cho `apt_0/1/2`;
- chất lượng detector/instance identity và room segmentation accuracy.

## Giải thích `6 regions / 5 RoomNodes`

Trong replay `apt_0` checkpoint, diagnostics cho thấy regions tăng/giảm theo
quan sát và có các sự kiện `rooms_promoted` cùng
`stale_room_ownership_reconciled`. Final snapshot có 6 geometric regions,
nhưng chỉ 5 RoomNodes với anchor regions `1, 4, 5, 7, 8`; region hiện hành
còn lại không có promotion/anchor hợp lệ ở thời điểm cuối. Room IDs không
liên tục vì pruning/relink. Các room được tạo do `agent_bootstrap` hoặc
`region_contains_existing_entities`; đó là chính sách promotion/anchoring,
không phải đếm phòng từ annotation.

Vì vậy `regions_final=6` và `rooms_final=5` là output của route/baseline,
không phải kết luận có 6/5 phòng thật.

## Annotation inventory và giới hạn

Inventory full-scene đã ghi trong `instance_validation/scene_instance_validation.json`:

- `apt_0`: 113 object instances, 6 articulated declarations, 80 templates;
- `apt_1`: 120 object instances, 6 articulated declarations, 86 templates;
- `apt_2`: 119 object instances, 6 articulated declarations, 87 templates.

Scene có object metadata/semantic IDs và navmesh route checks, nhưng không có
room polygons/room labels độc lập đã kiểm tra. Navmesh và semantic object IDs
không đi vào M1–M10 và không được dùng làm room GT.

## Bằng chứng và lệnh tái lập

Dev capture/replay pass ở phạm vi kỹ thuật: 16 frames, native M1–M10,
`fresh_replay_equal=true`, snapshot reload và delta replay pass. Artifact
chính nằm trong `artifacts/runs/habitat_replica_cad_apt0_perception_dev_20261001/`.

Lệnh replay:

```bash
MPLCONFIGDIR=/tmp/habitat-replay-mpl PYTHONPATH=.:src \
python3 scripts/run_habitat_native_replay.py \
  --capture artifacts/runs/habitat_replica_cad_apt0_perception_dev_20261001/capture \
  --output artifacts/runs/habitat_replica_cad_apt0_perception_dev_20261001/replay \
  --pipeline-config configs/reference_code_mp3d.yaml \
  --grid-output artifacts/runs/habitat_replica_cad_apt0_perception_dev_20261001/grid_trace
```

P0 không nghiệm thu detector, room accuracy hoặc robustness holdout. P1 là
decision record riêng; P2 chỉ bắt đầu khi cache mask/class/score thật từ RGB
được tạo và kiểm tra negative/error handling.
