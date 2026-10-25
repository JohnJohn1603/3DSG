# ReplicaCAD local scene inventory and acceptance status

Ngày kiểm kê gốc: 2026-10-16; cập nhật apt_2 và hoàn tất apt_3–apt_5 ngày
2026-10-17. Apt_0–apt_2 giữ nguyên evidence/run IDs đã có. Với từng apt_3,
apt_4, apt_5, lượt này tạo một C2 run rồi chạy đúng một D1 producer trên cache
C2 đó; D2 đã sẵn sàng trước producer và hiển thị revision khi run còn running.
Apt_3 có một lần bootstrap thất bại trước producer do quyền bind localhost;
không có mapping pass trong lần đó, run thành công dùng ID mới. Không có retry
sau khi producer thật bắt đầu.

## Nguồn và cách đếm

Baseline đọc-only: `/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/habitat_data/versioned_data/replica_cad_dataset`.
Trong `configs/scenes` có **91 scene instances**: sáu căn hộ (`apt_0`–`apt_5`),
84 staging instances (`v3_sc0_staging_00`–`20`, `v3_sc1_staging_00`–`20`,
`v3_sc2_staging_00`–`20`, `v3_sc3_staging_00`–`20`) và `empty_stage`.
Cả 91 instance đều có file navmesh cùng tên; các file asset được tham chiếu
trực tiếp trong scene-instance JSON đều tồn tại.

`configs/launcher/scenes.json` hiện chỉ đăng ký alias legacy `replica_apt0`.
C2 `fresh-replica` nhận config tường minh nên không cần alias registry cho
scene khác. Dataset config có 21 navmesh references cho `v3_sc4_staging_00`–
`20` nhưng không có các navmesh hoặc scene-instance tương ứng trong local
resources. Không tính chúng là scenes có sẵn.

| Scene / nhóm scene | Scene instance + navmesh | Route hiện có | Preflight / C2 | D1+D2 real simultaneous | Run IDs và viewer |
|---|---|---|---|---|---|
| `apt_0` | Có; assets refs đầy đủ | 5 route files; C2 dùng `m12_replica_cad_apt0_instance_route.json` (12 bước, 160×120) | C2 preflight và run hoàn tất | **Chưa đạt simultaneous XYZ.** D1 real run và XY GUI acceptance đạt; D2 viewer được tạo/kiểm tra sau producer, không được tính là simultaneous | C2 `c2_replica_apt0_short_20261016_retry3`; D1 `d1_c2_apt0_20261016_v2` (attempt `..._v1` dừng trước producer, 0 frame). `./run_scene.sh results d1_c2_apt0_20261016_v2`; `./run_scene.sh open d1_c2_apt0_20261016_v2 live --port 8765` |
| `apt_1` | Có; assets refs đầy đủ | 2 route files; C2 dùng `m12_replica_cad_apt1_instance_route.json` (12 bước, 160×120) | C2 preflight và run hoàn tất | **Đạt**: D2 XYZ mở và nhận revisions trước khi D1 producer hoàn tất | C2 `c2_replica_apt1_short_e_v1`; D1 `d1_c2_replica_apt1_short_e_v1_retry1`. `./run_scene.sh results d1_c2_replica_apt1_short_e_v1_retry1`; `./run_scene.sh open d1_c2_replica_apt1_short_e_v1_retry1 live --port 8766` |
| `apt_2` | Có; apt_2 scene instance, dataset config và navmesh hash-checked | 2 route files; chạy route 12 bước 160×120 | Route gate **pass**; C2 `c2_replica_apt2_local_v1` completed: 12 frames, 24 detections; không suy quality từ counts | **Đạt D1+D2 simultaneous revision arrival**: browser đã hiển thị/render revisions khi D1 manifest còn `running`; 12/12 hoàn tất, live-follow, final stage riêng | C2 `c2_replica_apt2_local_v1`; D1 mới `d1_c2_replica_apt2_gate_v2`. `./run_scene.sh results d1_c2_replica_apt2_gate_v2`; `./run_scene.sh open d1_c2_replica_apt2_gate_v2 live --port 8768` |
| `apt_3` | Có; assets refs đầy đủ | Route navmesh 12 bước, 160×120, HFOV 90°; 12/12 pose navigable và kết nối | C2 completed (12 frame, 27 P2 obs); một D1 trên cache C2 | **Đạt**: D2 sẵn sàng trước start và nhận/render revision khi D1 `running`; live-follow/seek/final stage kiểm tra | C2 `c2_replica_apt3_navmesh_v1`; D1 `d1_c2_replica_apt3_gate_retry1`; `./run_scene.sh results d1_c2_replica_apt3_gate_retry1`; `./run_scene.sh open d1_c2_replica_apt3_gate_retry1 live --port 8769`. Membership/semantics unavailable |
| `apt_4` | Có; assets refs đầy đủ | Route navmesh 12 bước, 160×120, HFOV 90°; 12/12 pose navigable và kết nối | C2 completed (12 frame, 31 P2 obs); một D1 trên cache C2 | **Đạt**: D2 sẵn sàng trước start và nhận/render revision khi D1 `running`; historical seek giữ prefix; final stage riêng | C2 `c2_replica_apt4_navmesh_v1`; D1 `d1_c2_replica_apt4_gate_v1`; `./run_scene.sh results d1_c2_replica_apt4_gate_v1`; `./run_scene.sh open d1_c2_replica_apt4_gate_v1 live --port 8770`. Membership/semantics unavailable |
| `apt_5` | Có; assets refs đầy đủ | Route navmesh 12 bước, 160×120, HFOV 90°; 12/12 pose navigable và kết nối | C2 completed (12 frame, 12 P2 obs); một D1 trên cache C2 | **Đạt**: D2 sẵn sàng trước start và render frame/revision khi D1 `running`; final stage riêng; final map rỗng vẫn là workflow pass | C2 `c2_replica_apt5_navmesh_v1`; D1 `d1_c2_replica_apt5_gate_v1`; `./run_scene.sh results d1_c2_replica_apt5_gate_v1`; `./run_scene.sh open d1_c2_replica_apt5_gate_v1 live --port 8771`. 0 final objects không phải kết luận accuracy |
| `v3_sc0_staging_00`–`v3_sc0_staging_20` (21 scenes) | Có; mỗi instance có navmesh | Không có route theo từng staging scene | Chưa có route/C2 config | Chưa chạy | Không có run IDs/viewer. Blocker: route chưa được thiết kế/validate cho các staging instances |
| `v3_sc1_staging_00`–`v3_sc1_staging_20` (21 scenes) | Có; mỗi instance có navmesh | Không có route theo từng staging scene | Chưa có route/C2 config | Chưa chạy | Không có run IDs/viewer. Blocker: route chưa được thiết kế/validate cho các staging instances |
| `v3_sc2_staging_00`–`v3_sc2_staging_20` (21 scenes) | Có; mỗi instance có navmesh | Không có route theo từng staging scene | Chưa có route/C2 config | Chưa chạy | Không có run IDs/viewer. Blocker: route chưa được thiết kế/validate cho các staging instances |
| `v3_sc3_staging_00`–`v3_sc3_staging_20` (21 scenes) | Có; mỗi instance có navmesh | Không có route theo từng staging scene | Chưa có route/C2 config | Chưa chạy | Không có run IDs/viewer. Blocker: route chưa được thiết kế/validate cho các staging instances |
| `empty_stage` | Có; matching navmesh | Không có route matching `empty_stage`; `m12_replica_cad_stage_route.json` names `frl_apartment_stage`, for which no local scene instance exists | Chưa có matching route/C2 config | Chưa chạy | Không có run IDs/viewer. Đây là calibration/empty stage, không phải căn hộ đầy đủ |

## Ma trận nghiệm thu từng scene instance (91 dòng)

Bảng tổng hợp phía trên gom staging theo cohort; ma trận này liệt kê đủ từng instance. Preflight chỉ chứng minh tài nguyên/route gate tương ứng, không đồng nghĩa pipeline pass. Chi tiết hashes và command đầy đủ nằm trong JSON inventory.

| Scene instance | Scene JSON / navmesh / asset refs | Route đã có | Preflight / C2 | D1+D2 real simultaneous | Run IDs / blocker |
|---|---|---|---|---|---|
| `apt_0` | scene=yes; navmesh=yes; missing asset refs=0 | m12_replica_cad_apt0_instance_route.json (12 steps, [160, 120] px), m12_replica_cad_apt0_perception_dev_hires_route.json (16 steps, [320, 240] px), m12_replica_cad_apt0_perception_dev_long_route.json (24 steps, [320, 240] px), m12_replica_cad_apt0_perception_dev_route.json (16 steps, [160, 120] px), m12_replica_cad_apt0_percept… | C2 preflight+run completed; C2 run: c2_replica_apt0_short_20261016_retry3 | not accepted: D1 XY GUI passed; no evidence D2 XYZ was open during producer | C2=c2_replica_apt0_short_20261016_retry3; D1=d1_c2_apt0_20261016_v2; No D1+D2 simultaneous XYZ acceptance. |
| `apt_1` | scene=yes; navmesh=yes; missing asset refs=0 | m12_replica_cad_apt1_instance_route.json (12 steps, [160, 120] px), m12_replica_cad_apt1_perception_holdout_hires_route.json (12 steps, [320, 240] px) | C2 preflight+run completed; C2 run: c2_replica_apt1_short_e_v1 | accepted: D2 XYZ opened and received revisions before D1 completed | C2=c2_replica_apt1_short_e_v1; D1=d1_c2_replica_apt1_short_e_v1_retry1; None within the recorded apt_1 route; membership/semantics remain unavailable. |
| `apt_2` | scene=yes; navmesh=yes; missing asset refs=0 | m12_replica_cad_apt2_instance_route.json (12 steps, [160, 120] px), m12_replica_cad_apt2_perception_holdout_hires_route.json (12 steps, [320, 240] px) | route/assets/hash preflight passed; C2 completed on 12 frames at 160×120; D1 `d1_c2_replica_apt2_gate_v2` completed 12/12; ConceptGraphs and OccuSG each ran once from locked C2 cache | **accepted**: browser was open before producer start; viewer displayed frame `replica-cad-apt_2-0008`, unified revision 9, while run status was `running`; continued to 12/12 and completed | C2=`c2_replica_apt2_local_v1`; D1=`d1_c2_replica_apt2_gate_v2`; `./run_scene.sh results d1_c2_replica_apt2_gate_v2`; `./run_scene.sh open d1_c2_replica_apt2_gate_v2 live --port 8768`. Membership/room semantics remain unavailable. |
| `apt_3` | scene=yes; navmesh=yes; missing asset refs=0 | `m12_replica_cad_apt3_navmesh_route_v1.json` (12 steps, 160×120) | Route validation pass; C2 `c2_replica_apt3_navmesh_v1` completed 12/12 | **accepted**: D2 rendered `replica-cad-apt_3-0001` / `unified-000002` while D1 was running; 12/12 joined; live-follow, seek, picking and terminal stage checked | C2=`c2_replica_apt3_navmesh_v1`; D1=`d1_c2_replica_apt3_gate_retry1`; `./run_scene.sh results d1_c2_replica_apt3_gate_retry1`; `./run_scene.sh open d1_c2_replica_apt3_gate_retry1 live --port 8769`; membership/semantics unavailable |
| `apt_4` | scene=yes; navmesh=yes; missing asset refs=0 | `m12_replica_cad_apt4_navmesh_route_v1.json` (12 steps, 160×120) | Route validation pass; C2 `c2_replica_apt4_navmesh_v1` completed 12/12 | **accepted**: D2 displayed a live revision while D1 was `running`; 12/12 joined; seek to frame 0004/rev 5 held while producer advanced; final stage checked. Exact live frame ID was not persisted | C2=`c2_replica_apt4_navmesh_v1`; D1=`d1_c2_replica_apt4_gate_v1`; `./run_scene.sh results d1_c2_replica_apt4_gate_v1`; `./run_scene.sh open d1_c2_replica_apt4_gate_v1 live --port 8770`; membership/semantics unavailable |
| `apt_5` | scene=yes; navmesh=yes; missing asset refs=0 | `m12_replica_cad_apt5_navmesh_route_v1.json` (12 steps, 160×120) | Route validation pass; C2 `c2_replica_apt5_navmesh_v1` completed 12/12 | **accepted**: D2 rendered `replica-cad-apt_5-0001` / first live revision while D1 was `running`; 12/12 joined; historical seek, follow and terminal stage checked | C2=`c2_replica_apt5_navmesh_v1`; D1=`d1_c2_replica_apt5_gate_v1`; `./run_scene.sh results d1_c2_replica_apt5_gate_v1`; `./run_scene.sh open d1_c2_replica_apt5_gate_v1 live --port 8771`; final objects=0; membership/semantics unavailable |
| `empty_stage` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no matching route C2 preflight; C2 run: not run | not run | C2=—; D1=—; This is an empty calibration stage, not a furnished apartment; current stage route names frl_apartment_stage … |
| `v3_sc0_staging_00` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_01` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_02` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_03` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_04` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_05` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_06` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_07` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_08` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_09` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_10` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_11` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_12` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_13` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_14` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_15` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_16` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_17` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_18` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_19` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc0_staging_20` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_00` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_01` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_02` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_03` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_04` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_05` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_06` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_07` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_08` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_09` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_10` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_11` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_12` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_13` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_14` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_15` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_16` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_17` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_18` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_19` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc1_staging_20` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_00` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_01` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_02` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_03` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_04` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_05` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_06` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_07` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_08` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_09` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_10` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_11` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_12` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_13` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_14` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_15` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_16` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_17` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_18` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_19` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc2_staging_20` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_00` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_01` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_02` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_03` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_04` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_05` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_06` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_07` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_08` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_09` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_10` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_11` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_12` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_13` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_14` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_15` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_16` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_17` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_18` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_19` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |
| `v3_sc3_staging_20` | scene=yes; navmesh=yes; missing asset refs=0 | none | scene instance and matching navmesh present; no route C2 preflight; C2 run: not run | not run | C2=—; D1=—; No route config or C2 run; needs explicit route per staging instance. Dataset config also has missing sc4 nav… |

JSON machine-readable inventory, gồm từng scene ID, instance/navmesh hashes,
route/config lists, statuses và blockers:
[inventory.json](../artifacts/replica_cad_local_inventory/inventory.json).

Lệnh C2/D1 đã dùng trên apt_0/apt_1 được ghi trong JSON cùng với lệnh mở
viewer. Không cần chạy lại hai scene này. Apt_0 D1 command dùng server XY
localhost sẵn có; D2 XYZ không được nghiệm thu simultaneous ở run đó.

## Apt_2 preflight

- Config mới sao từ apt_1, chỉ đổi scene/sequence/scene instance/route; giữ
  nguyên mapping lock, model paths, interpreter paths và resource policy:
  [replica_cad_apt2_short_c2_v1.json](../configs/scene_runs/replica_cad_apt2_short_c2_v1.json).
- Route: [m12_replica_cad_apt2_instance_route.json](../configs/habitat/m12_replica_cad_apt2_instance_route.json),
  12 waypoints, 5 vị trí khác nhau, 160×120, HFOV 90°. Calibration ID khớp
  apt_1 và danh sách waypoint giống route apt_1.
- Scene instance SHA-256: `d39c9a5337fb7ff124f83dc7989a40645ff9cf36604bfe34437f8d108535f55e`.
- Shared scene-dataset config SHA-256: `a51d4fe5e1853e616c13710930191bad33c0b277edece7c4e520d1194831f5e3`.
- Apt_2 navmesh SHA-256: `f1a3fedeed562a799f841d0b5fecb6963ac9b4d161d72d069c3773bb8c1e4669`.
- Route SHA-256: `9051dda294da94affe27ef2ecf5d051c0506e2c4c70b86c36ca822207b4fed64`.
- C2 config SHA-256: `aa51b2f509d64720e487e5aaf6796afbb5b5763d929fbdaa5b1a0209f52f39fd`.
- Route validator output: [apt2_route_validation.json](../artifacts/replica_cad_local_inventory/apt2_route_validation.json).

Validator log cũng có cảnh báo dataset config trỏ tới 21 navmesh `sc4` không
có trong local resources và cảnh báo articulated objects do Habitat build
không có Bullet. Route vẫn pass; các cảnh báo được giữ lại, không sửa assets
hay thay config để che chúng.

## Kết quả pilot apt_2

C2 `c2_replica_apt2_local_v1` hoàn tất trong khoảng 189 s wall-clock:
capture 20.8 s, P2 5.36 s, P3 projection 1.30 s, OccuSG 114.84 s,
image features 16.91 s, text features 12.65 s, ConceptGraphs mapping
12.47 s. C2 tạo 24 image/text feature rows. Warning OpenCLIP “randomly
initialized” được phát ở khởi tạo model `pretrained=None`; extractor sau đó
nạp safetensors đã pin bằng `load_state_dict(..., strict=True)`. Feature
manifest checkpoint hash khớp `e6d1bd...a2b7c31`. Không có model tải mới.

C2 manifest: `artifacts/conceptgraphs_hybrid_runs/c2_replica_apt2_local_v1/c2_run_manifest.json`.
Summary của C2: 12 frames; 24 P2 observations; 20 hypotheses trước filter;
1 object sau min_detections=3; 5 room nodes; membership
`unavailable_elevation_scope`. Đây là workflow/integration evidence, không
phải object/room quality metric.

Integrity check lịch sử sau run: C2 output **486/486 hashes** khớp; D1
**499/500** khớp. Mục lệch duy nhất là `logs/viewer_server.log`, file này
tiếp tục nhận browser polling sau khi hash manifest được ghi. Không có file
thiếu; graph, geometry, manifests và pointer đều không phải mục lệch.
Không sửa hoặc viết lại hash lịch sử của run này. D1 code path mới tách
immutable outputs khỏi operational logs và ghi timestamp/writer còn sống;
hash graph, geometry, D1/D2 manifests, viewer assets và entrypoint pointer
vẫn bắt buộc phải khớp. Run mới ghi detached `run_manifest.sha256.json` cho
manifest gốc; `results` xác minh receipt và báo lỗi nếu hash lệch. Manifest
gốc không tự chứa hash của chính nó; không có manifest con nào bị loại khỏi
output ledger. Run apt_2 lịch sử vẫn giữ nguyên 499/500 và không được viết lại
bằng định dạng mới.

D1 thành công với run ID `d1_c2_replica_apt2_local_v1_retry1`, 12/12 frames,
72.24 s mapping run-time; capture/perception dùng lại từ C2. C2 chỉ chạy một
lần. D1 có một attempt trước retry bị OS sandbox từ chối bind localhost
(`PermissionError`) trước khi producer chạy; retry duy nhất sau khi quyền
bind được cấp mới thực hiện mapping/OccuSG.

D2 entrypoint lấy từ `viewer/d2_viewer_entrypoint.json` là
`index_d2_live.html` (không phải `index.html`). D2 page thật hiển thị XYZ
clouds/trajectory, XY polygons, frame RGB/mask/crop, 20 online hypotheses,
raw history/observation lineage; seek về frame 1 cho 0 objects/1 camera pose,
và chọn hypothesis cho lineage đúng. Prefix admission hiển thị unavailable;
final postprocess vẫn là terminal stage riêng. Room geometry giữ XY; room
membership `unavailable_elevation_scope`, semantics
`unavailable_membership_scope`. `room_contains` trong viewer nhắm navigation,
agent và OccuSG objects; ConceptGraphs-object membership edges là 0.

Ở run lịch sử `d1_c2_replica_apt2_local_v1_retry1`, simultaneous D1+D2 gate
**chưa đạt**: lần mở đầu dùng `/viewer/index.html` và trang đó báo
`window.createXyzViewer is not a function`. Manifest-selected D2 page chỉ được
mở sau khi 12/12 revisions đã publish, nên lần đó chỉ chứng minh render/seek/
picking sau producer. Đây là trạng thái lịch sử, không phải trạng thái hiện
hành. Entry point đã được sửa theo pointer; một run riêng
`d1_c2_replica_apt2_gate_v2` sau đó đạt simultaneous gate như ghi ở phần dưới.

### Lệnh của run D1 lịch sử `retry1`

```bash
./run_scene.sh fresh-replica --config configs/scene_runs/replica_cad_apt2_short_c2_v1.json --run-id c2_replica_apt2_local_v1
./run_scene.sh live-replay --source-run-id c2_replica_apt2_local_v1 --run-id d1_c2_replica_apt2_local_v1_retry1 --port 8767 --xyz-viewer --timeout 420
./run_scene.sh open d1_c2_replica_apt2_local_v1_retry1 live --port 8767
./run_scene.sh results d1_c2_replica_apt2_local_v1_retry1
```

Viewer URL lịch sử: `http://127.0.0.1:8767/artifacts/live_processing_runs/d1_c2_replica_apt2_local_v1_retry1/viewer/index_d2_live.html?live=1`.

Mọi output room membership/semantics tiếp tục `unavailable_elevation_scope` /
`unavailable_membership_scope`; object/room counts không được diễn giải là
accuracy. S1–S3 vẫn deferred.

## Cập nhật vận hành viewer apt_2 · 2026-10-17

- Đã sửa server read-only để `/viewer/index.html` đọc pointer
  `d2_viewer_entrypoint.json`, xác minh manifest/entrypoint hashes rồi redirect
  tới trang được pointer chọn. Pointer thiếu/sai trả HTTP 409 với lỗi rõ;
  không còn gọi D2 HTML thiếu renderer.
- `live-replay --xyz-viewer` hiện mở server trước producer, kiểm tra page,
  renderer JS và live index/head rỗng, rồi dừng ở
  `waiting_for_first_revision`. Sau khi operator mở trang và xác nhận, lệnh
  `start-live` phát start signal; từ đó producer không phụ thuộc browser.
- `results`, `open`, URL server và compatibility redirect cùng chọn entrypoint
  từ pointer. `results` in URL pointer-selected; URL cũ `/viewer/index.html`
  redirect tới cùng target. Bootstrap/manifest/hash checks chạy trước producer.
- Targeted tests/fixture đạt trước producer: 61 passed, 1 legacy HTTP binding
  test được chạy riêng ngoài bước pytest; renderer JS `node --check` và
  Python compile checks đạt. Fixture xác nhận bootstrap
  `waiting_for_first_revision`, explicit start confirmation và timeout không
  spawn producer.
- D1 run thật: `d1_c2_replica_apt2_gate_v2`, tái sử dụng C2
  `c2_replica_apt2_local_v1`; không capture/P2/P3/features lại. ConceptGraphs
  và OccuSG mỗi nhánh chạy đúng một pass. Viewer server + trang được kiểm tra
  trước start; browser xác nhận trang chờ revision; sau explicit start browser
  nhận/render revision khi manifest còn `running`.
- Browser kiểm tra trực tiếp: frame 0003/revision 4 và frame 0008/revision 9
  được nhận trong lúc run chạy; frame cuối 0011/revision 12 hiển thị XYZ cloud,
  trajectory 12 pose, room XY và RGB/mask/evidence. Final postprocess được
  mở riêng như terminal stage; online prefix không bị thay bằng final object.
  Console không có warning/error JS ở page đã kiểm tra.
- Có một trạng thái chuyển tiếp ngắn khi frame header tiến trước lúc geometry
  mới hoàn tất tải; khi render ổn định, frame/object/room revisions khớp. Đây là
  file-backed live processing từ cache, không phải live capture hoặc realtime.
- Run manifest ghi producer start `2026-10-17T02:30:20.451Z`, finish
  `02:31:12.845Z`, 12 joined revisions; viewer `live_head` vẫn `running` tại
  `02:31:04.290Z` khi đã có 10 revisions. GUI observation không có timestamp
  độc lập chính xác từ browser API; bằng chứng được giới hạn bởi các thời điểm
  live-head/producer trong manifest.
- D1+D2 simultaneous revision-arrival gate apt_2 **đạt**. Gate không có nghĩa
  room membership/semantics hoặc object quality đã được xác minh.

Lệnh đã chạy để khởi động D1 (dùng cache C2, không tạo lại C2):

```bash
./run_scene.sh live-replay --source-run-id c2_replica_apt2_local_v1 --run-id d1_c2_replica_apt2_gate_v2 --port 8768 --xyz-viewer --timeout 420 --viewer-ready-timeout 600
```

Khi lệnh báo `waiting_for_first_revision`, mở trang và kiểm tra trước khi
thả producer:

```bash
./run_scene.sh start-live d1_c2_replica_apt2_gate_v2 --confirm-browser-ready --port 8768
```

Mở kết quả bằng `./run_scene.sh results d1_c2_replica_apt2_gate_v2` rồi
`./run_scene.sh open d1_c2_replica_apt2_gate_v2 live --port 8768`. `results`
đọc cùng D2 entrypoint pointer và xác minh detached root manifest receipt.

Parity với D1 cũ `d1_c2_replica_apt2_local_v1_retry1`: object snapshots 12/12,
room prefix snapshots 12/12, ConceptGraphs geometry NPZ 139/139 và viewer
geometry JSON 12/12 byte-identical. Joined graph states/revisions 12/12 khớp
sau khi bỏ riêng event SHA-256 run-specific; graph payload không đổi. Terminal
export vẫn một object, cùng cloud hash. 504 immutable outputs đều hash-match;
run manifest SHA-256 khớp detached receipt. Operational logs được tách khỏi
immutable outputs; hash checkpoint ghi hai producer đã thoát và viewer server
do run sở hữu còn sống. Historical 499/500 record không bị sửa; khác biệt cũ
vẫn được giải thích là access log mutable.

Timing: run duration 176.13 s bao gồm 123.74 s chờ explicit browser start;
hai producer chạy 52.39 s. Child ConceptGraphs mapping ghi 42.21 s và peak RSS
454.3 MiB. OccuSG/native child RSS riêng không được instrument; không suy tổng
RAM từ số đó. Khi viewer đang chạy có sample nhận 76.9 ms/render 242.4 ms;
đây là browser instrumentation, không phải end-to-end display latency.

## Kết quả apt_3–apt_5 trong lượt nghiệm thu

Ba route được khóa trước inference, sinh từ navmesh/free-space geometry, không
đọc predictor output hoặc GT semantic. Mỗi route có 12 pose; validator xác nhận
12/12 navigable và mọi đoạn liên tiếp có kết nối. Revisit turn chỉ là đổi yaw
tại cùng vị trí, không được tính mặc định là independent view. Tất cả dùng
160×120, HFOV 90°, cùng calibration, model, seed mapping 20261014, matcher,
thresholds, admission và scope policy đã khóa.

| Scene | C2 / route preflight | Capture + P2/P3 | OccuSG room processing | Feature + ConceptGraphs | Unified graph/viewer | D1 runtime / mapping RSS | Hypotheses → final | P2 / P3 accepted / rejected |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| apt_3 | 24.57 s | 19.40 / 4.72 / 0.98 s | 93.74 s | image 12.26 s; text 9.15 s; CG stage 9.88 s (mapping 1.36 s) | 1.93 s; C2 wall 177.56 s | 74.95 s / 458.95 MiB | 19 → 1 | 27 / 3 / 24 |
| apt_4 | 17.24 s | 16.12 / 4.79 / 1.06 s | 111.14 s | image 13.62 s; text 9.83 s; CG stage 10.62 s (mapping 1.57 s) | 2.14 s; C2 wall 187.54 s | 158.62 s / 460.29 MiB | 22 → 2 | 31 / 3 / 28 |
| apt_5 | 17.00 s | 16.39 / 4.02 / 0.87 s | 95.11 s | image 11.23 s; text 9.89 s; CG stage 9.42 s (mapping 0.67 s) | 2.08 s; C2 wall 167.01 s | 103.37 s / 456.39 MiB | 10 → 0 | 12 / 4 / 8 |

C2 wall includes its one invocation end-to-end. Inside that invocation, the
OccuSG runner processes the 12 frames and performs its built-in 12-frame
fresh-check; this is disclosed as two room-processing passes within C2, not a
second C2/D1 retry. D1 performs one ConceptGraphs pass and one OccuSG pass over
12 cached frames for each scene. Apt_3 had two failed launcher attempts before
producer start: sandbox denied localhost bind, then a repeated call could not
reuse the failed run directory. Neither started a producer; the successful D1
used a fresh run ID.

D1 timing: object branch processing means 47.8/54.1/27.5 ms per frame;
room branch 3.99/4.55/4.41 s per frame; object barrier wait 3.69/4.25/4.09 s
per frame; coordinator join mean 137.5/125.5/65.9 ms; publish mean object
1.7/1.5/1.3 ms and room 1.5/1.4/1.5 ms for apt_3/4/5. Viewer readiness
wait from the explicit gate was approximately 18.0/94.7/41.5 s; D1 total runtime
includes that wait. Browser receive/render is instrumented in the viewer but is
not collected as a runner metric; page values after completion are not reported
as end-to-end live display latency. Peak RSS is per ConceptGraphs mapping
process; OccuSG child RSS was not measured separately, so no host-wide peak is
claimed.

D1↔C2 parity passed for all three: same input hashes/order/config/seed;
object memberships, point counts, bounds and XYZ/RGB arrays matched at online,
denoise, min-detections filter, merge and export checks. apt_3: 19 hypotheses
→ 1 object; apt_4: 22 → 2; apt_5: 10 → 0. Hash verification covered C2 outputs
533/533, 564/564, 297/297 and D1 immutable outputs 555/555, 590/590, 305/305,
with no missing/mismatch. Detached D1 manifest receipts verified. Mutable
operational logs are outside the immutable-output hash scope.

D2 GUI acceptance was performed during each real D1 producer, not via synthetic
events. The browser showed `waiting_for_first_revision` before explicit start,
then real revisions while the D1 run was `running`; 12/12 revisions arrived.
Live-follow, historical seek with follow off, trajectory prefix, RGB/mask/crop,
object lineage, final terminal stage, XYZ object clouds and XY room geometry
were checked. For apt_5, final-map object count is zero, so picking was checked on an online hypothesis at frame 0011 and showed its two observation IDs. `apt_4`'s exact live frame ID was not persisted in a screenshot;
we record that gap rather than infer it from the terminal export. The D1
manifests, route validations, mapping parity files and per-run acceptance
summary are indexed in
[`apt3_5_acceptance_v1.json`](../artifacts/replica_cad_local_inventory/apt3_5_acceptance_v1.json).

### Commands đã chạy và lệnh mở

Mỗi D1 run dùng 2 terminal: terminal 1 khởi tạo server/producer và chờ ở
`waiting_for_first_revision`; terminal 2 mở viewer trước rồi mới xác nhận start.

```bash
# apt_3 — terminal 1
./run_scene.sh fresh-replica --config configs/scene_runs/replica_cad_apt3_short_c2_v1.json --run-id c2_replica_apt3_navmesh_v1
./run_scene.sh live-replay --source-run-id c2_replica_apt3_navmesh_v1 --run-id d1_c2_replica_apt3_gate_retry1 --port 8769
# apt_3 — terminal 2, while terminal 1 waits
./run_scene.sh open d1_c2_replica_apt3_gate_retry1 live --port 8769
./run_scene.sh start-live d1_c2_replica_apt3_gate_retry1 --confirm-browser-ready --port 8769
# after completion
./run_scene.sh results d1_c2_replica_apt3_gate_retry1
./run_scene.sh open d1_c2_replica_apt3_gate_retry1 live --port 8769

# apt_4 — terminal 1
./run_scene.sh fresh-replica --config configs/scene_runs/replica_cad_apt4_short_c2_v1.json --run-id c2_replica_apt4_navmesh_v1
./run_scene.sh live-replay --source-run-id c2_replica_apt4_navmesh_v1 --run-id d1_c2_replica_apt4_gate_v1 --port 8770
# apt_4 — terminal 2, while terminal 1 waits
./run_scene.sh open d1_c2_replica_apt4_gate_v1 live --port 8770
./run_scene.sh start-live d1_c2_replica_apt4_gate_v1 --confirm-browser-ready --port 8770
# after completion
./run_scene.sh results d1_c2_replica_apt4_gate_v1
./run_scene.sh open d1_c2_replica_apt4_gate_v1 live --port 8770

# apt_5 — terminal 1
./run_scene.sh fresh-replica --config configs/scene_runs/replica_cad_apt5_short_c2_v1.json --run-id c2_replica_apt5_navmesh_v1
./run_scene.sh live-replay --source-run-id c2_replica_apt5_navmesh_v1 --run-id d1_c2_replica_apt5_gate_v1 --port 8771
# apt_5 — terminal 2, while terminal 1 waits
./run_scene.sh open d1_c2_replica_apt5_gate_v1 live --port 8771
./run_scene.sh start-live d1_c2_replica_apt5_gate_v1 --confirm-browser-ready --port 8771
# after completion
./run_scene.sh results d1_c2_replica_apt5_gate_v1
./run_scene.sh open d1_c2_replica_apt5_gate_v1 live --port 8771
```

Trong lượt nghiệm thu, các lệnh `results` đã xác minh trạng thái hoàn tất,
manifest receipt và pointer-selected `index_d2_live.html`. Dùng đúng các lệnh
`open` trên để mở viewer run-owned. Khi chạy mới, `open` phải được gọi trong
lúc producer đang chờ; sau đó mới gọi `start-live`.

## Trạng thái nghiệm thu còn lại

Apt_0–apt_2 giữ nguyên lịch sử: apt_1/apt_2 có simultaneous D1+D2 acceptance;
apt_0 chưa có bằng chứng D2 XYZ mở đồng thời với producer. Apt_3–apt_5 đạt
C2 và simultaneous D1+D2 trong phạm vi route ngắn đã khóa. Do apt_5 có 0 object
sau final filter, đây là workflow success nhưng không phải object-quality pass.
Không scene nào ở lượt này chứng minh room membership/semantic availability:
`unavailable_elevation_scope` và `unavailable_membership_scope` vẫn được hiển
thị. Viewer giữ room polygons ở XY, không tạo room volumes.

Ma trận vẫn gồm đủ **91 scene instances**. 84 staging scenes chưa có route/run;
`empty_stage` chưa có route khớp scene. Vì vậy chưa tuyên bố hỗ trợ toàn bộ
ReplicaCAD local. Không chạy scene nào khác trong lượt này.


## Atomic viewer revision gate · 2026-10-17

Trước khi chạy apt_3, viewer được đổi để giữ revision đang hiển thị trong khi
revision kế tiếp tải; trạng thái loading tách riêng. Chỉ commit đồng thời XYZ,
XY, trajectory và metadata khi đủ assets; load lỗi giữ nguyên màn hình cũ,
request cũ đến muộn bị bỏ. Kiểm chứng trên fixture delayed/out-of-order,
failure/cancel và artifacts viewer apt_2 đã có; không chạy lại producer apt_2.

- `node --check scripts/xyz_renderer.js`: pass; inline script trong
  `scripts/causal_timeline_viewer.html`: pass.
- `node tests/test_viewer_atomic_revision.js`: pass (delayed, out-of-order,
  failure retention, cancellation).
- Targeted D2 tests: 3 passed.
- Full `python3 -m pytest -q tests/test_d2_xyz_viewer.py`: 14 passed khi cho
  phép test bind localhost; lần chạy sandbox-only trước đó bị OS từ chối bind,
  không phải assertion/code failure.
