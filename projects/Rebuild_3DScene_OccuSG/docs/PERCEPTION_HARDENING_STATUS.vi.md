# Perception hardening sau checkpoint G1

Ngày: 2026-10-01  
Phạm vi: giữ nguyên capture G1, model, mapping/segmentation và M6 policy.

## Effective runtime configuration

P4 replay hiện nạp `objects.association` trực tiếp từ
`configs/reference_code_mp3d.yaml` và ghi lại trong `replay_p4/manifest.json`:

- `same_class_radius_m = 0.75`;
- `same_pose_suppression_cap_m = 0.15`.

Hai giá trị `0.50/0.10` trong perception profile là metadata P2/P3 cũ, không
được runner đọc. Việc truyền explicit `ObjectAssociationManager(phase.object_association)`
không đổi output reference: snapshot và delta của G1 cũ và replay effective
config có SHA-256 giống hệt nhau.

## Room labeler

`RuleBasedRoomLabeler` đã được harden:

- support class được tính riêng cho từng candidate label;
- `observed_object_ids` tách khỏi `supporting_object_ids`;
- supporting IDs chỉ chứa object thực sự đóng góp cho label được chọn;
- evidence hash không chứa global graph revision, nhưng có rule fingerprint và
  evidence liên quan;
- regression bao phủ support sai candidate, hash qua revision, stale → fresh và
  membership/evidence thay đổi.

Không đổi room ID, geometry, M6 association hoặc segmentation.

## P3 rejection diagnostics và development ablation

P3 ledger nay lưu mask pixel count, valid-depth count/fraction, depth range,
backprojected points, cluster count, largest-cluster size/extent/diameter,
reason và rejection overlay. Artifact mới:

`artifacts/runs/habitat_replica_cad_apt0_perception_dev_hires_20261001/`

| profile | accepted / 25 | rejection chính |
|---|---:|---|
| reference: stride 1, eps 0.02, min 100 | 13 | largest cluster 1–12 points ở các ca bị loại |
| sampling ablation: stride 2 | 4 | giảm mạnh do lấy mẫu thưa |
| clustering ablation: eps 0.01 | 4 | tách cluster mạnh hơn, không cải thiện |
| min-points ablation: min 50 | 13 | không đổi; ca bị loại vẫn có largest cluster rất nhỏ |

Đối chứng 160×120 trước đó đạt 2/25. Kết luận trong phạm vi development:
độ phân giải và sampling là yếu tố nổi bật hơn việc hạ `cluster_min_points`;
chưa có lý do đổi reference profile hoặc hạ threshold trên holdout.

## Annotation pilot

Đã tạo hai mask thủ công độc lập trên RGB gốc của apt0 re-observation:

- refrigerator, frame 0;
- potted plant, frame 4.

Evaluator: `scripts/evaluate_perception_annotations.py`. Kết quả pilot IoU lần
lượt khoảng `0.679` và `0.702`; đây chỉ là hai mask nhìn thấy, không phải
benchmark. Annotation không đi vào builder. Room annotation độc lập vẫn chưa
có trong ReplicaCAD checkpoint (`room_accuracy=null`), nên chưa báo room
accuracy.

## Semantic lifecycle và kiểm thử

- stale → fresh không biến stale evidence thành evidence mới;
- membership thay đổi tạo evidence/support mới;
- hash giữ nguyên khi chỉ global revision thay đổi;
- targeted hardening regression: **25 passed trong 1,99 s**;
- full regression sau hardening: **256 passed trong 88,01 s**;
- các script mới đã `py_compile` thành công.

Giới hạn còn lại: detector/3D acceptance thấp trên route ngắn, holdout chỉ là
technical smoke, chưa có annotation room/object đủ rộng, chưa có robustness
claim và chưa tuning model.

## Route phát triển dài và holdout đã khóa profile

Đã chạy thêm route phát triển `apt_0` 24 pose ở 320×240, cùng FOV, depth,
model, stride và clustering của profile reference. Capture và P2/P3 đều pass;
P3 nhận 23/40 Detection2D, loại 17 do cluster. Native replay M1–M10 pass:

- 24 frame, 5 regions và 5 RoomNodes ở snapshot cuối;
- 13 ObjectNode, trong đó `object-000001` (bicycle) giữ ID qua cả 24 frame;
- `dining_table` được tái quan sát nhưng membership xuất hiện ở nhiều room
  trong snapshot theo thời gian; đây là điểm cần đánh giá thêm, không được gọi
  là đúng chỉ vì ID giữ được;
- `snapshot_reload_equal=true`, `delta_replay_equal=true`;
- fresh-process replay lần thứ hai có `fresh_replay_equal=true`;
- M3 native oracle khớp từng frame trong diagnostics (`bounds_match`,
  `geometry_match`, `value_mismatches=0` trên các frame đã ghi);
- runtime replay đầu tiên khoảng 150,43 s cho 24 frame, không phải realtime
  benchmark.

Semantic enrichment tách khỏi graph hình học cho route này cho kết quả
`1 predicted / 4 unknown`; không có room GT nên `accuracy=null`.

Sau khi khóa profile, đã chạy hai holdout cùng điều kiện sensor 320×240 và
route 12 pose tương ứng với route holdout cũ:

| run | frame | Detection2D | Detection3D nhận / loại | regions/rooms cuối | semantic |
|---|---:|---:|---:|---:|---|
| apt_1 holdout | 12 | 33 | 18 / 15 | 4 / 4 | 1 predicted, 1 ambiguous, 2 unknown |
| apt_2 holdout | 12 | 38 | 23 / 15 | 4 / 4 | 1 predicted, 3 unknown |

Cả hai holdout đều pass native replay, reload snapshot và replay delta. Các run
mới nằm riêng trong `artifacts/runs/` và không ghi đè checkpoint G1/apt0.
Các cảnh apt_1/apt_2 là biến thể nội thất ReplicaCAD cùng bộ dữ liệu, không
được gọi là các kiến trúc độc lập. Route dùng GT observation poses và navmesh
chỉ để kiểm tra navigability; không có robot vật lý, planner hay SLAM.

## Kết luận nghiệm thu giới hạn

Profile hiện có bằng chứng tích hợp tốt hơn: detector thật → P3 depth/clustering
→ M6 giữ ID qua một số tái quan sát → room membership/semantic export, trên
development và hai holdout cùng profile. Điều này chưa chứng minh recall,
accuracy phân phòng hoặc robustness: P3 vẫn loại nhiều observation, annotation
room độc lập chưa có, và các nhãn `predicted/unknown/ambiguous` chỉ là output
rule-based với score chưa hiệu chuẩn. Không đổi model, mapping/segmentation hay
M6 để đạt các con số trên.

Artifact chính:

- apt0 dài: `artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/`;
- apt1 holdout: `artifacts/runs/habitat_replica_cad_apt1_perception_holdout_hires_20261001/`;
- apt2 holdout: `artifacts/runs/habitat_replica_cad_apt2_perception_holdout_hires_20261001/`.

Full regression đúng invocation của project (`PYTHONPATH=.:src pytest -q`) sau
lượt này: **256 passed trong 68,93 s**. Lệnh `pytest -q` không có PYTHONPATH
đã từng lỗi collection vì thiếu import path; đó là lỗi invocation, không phải
test failure. Các replay/capture mới đều trả `status=pass` và config JSON mới
đã được nạp thành công. Chưa có thay đổi logic mapping, association hay model
trong lượt này.
