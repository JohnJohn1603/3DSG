# Kiến trúc active: ConceptGraphs objects → OccuSG rooms

Ngày cập nhật: 2026-10-17. Đây là đường phát triển chủ động mới; lịch sử U/K/Q và kết quả cũ vẫn được giữ làm baseline/development records.

### Update 2026-10-20 — T4 overlap route preflight

Giữ nguyên T1.1 prefix và timebase/model/policy. Geometry-only PathFinder
preflight không tìm được continuation 45–60 giây đạt clearance `0.1 m` và
`try_step_no_sliding` tolerance `0.025 m`; candidate đủ thời lượng tốt nhất
fail clearance tại mẫu 25 (`0.094507 m`). Không tạo route config và không
chạy producer/pilot trong lượt này. T4 overlap/render-during-capture vẫn
pending; không có thay đổi semantic hoặc scope. Bằng chứng, hashes và giới
hạn tìm kiếm ở [T4_OVERLAP_ROUTE_PREFLIGHT_20261020.vi.md](T4_OVERLAP_ROUTE_PREFLIGHT_20261020.vi.md).

### T4 timebase contract A (2026-10-19)

- Đã khóa policy v5 riêng cho pilot T4: action increment giữ nguyên
  `0.008 m` / `1.2°` với requested `dt=0.040 s`; `0.2 m/s` và `30°/s`
  chỉ là tốc độ danh nghĩa suy từ requested dt. Habitat `get_world_time()`
  là timestamp simulation có thẩm quyền; tốc độ thực đo bằng pose delta chia
  observed simulation delta.
- Sai lệch observed delta 40/48 ms được ghi lại, không bị ép về 40 ms và
  không bị chặn bằng tolerance tùy chọn. Dừng theo giới hạn đã khóa:
  simulation elapsed 60 s, 400 camera frames, CLI wall timeout, cùng các
  kiểm tra collision/navmesh, displacement và route. Pacing dùng simulation
  timestamp thực với host monotonic clock; không thêm bước hoặc terminal wait.
- Model-free startup preflight v5 đã đi tới capture boundary trên cùng
  launcher, viewer bootstrap, 5 readiness IPC workers và Habitat route
  validation; 0 model loads và 0 camera frames. Đây chưa phải T4 live pilot.
- Production pilot vẫn pending cho tới khi D2 mở trước start và các gates
  capture/perception/two mappers/unified commit/viewer overlap được đo riêng.

```text
Habitat RGB-D + K + poses
       ├── ConceptGraphs-Detect (Kaggle GPU) → object map
       └── OccuSG non-ROS (local)             → room map
                         └── local adapter → unified object–room graph
```

- **ConceptGraphs owns:** detections, masks, image/text features, lifting, object association/fusion, and its object map.
- **OccuSG owns:** occupancy, room regions/IDs, adjacency and room geometry.
- **Local adapter owns:** coordinate conversion, object lifecycle provenance, room membership and graph reconciliation.
- No second association through legacy M6/K4 after ConceptGraphs assigns object identity.
- The old launcher/default, baseline, reference checkouts, artifacts and annotation ledgers remain unchanged.

## Execution boundary

Các đường chạy local gồm C1 cached replay, C2 batch fresh route cho một cấu hình, và D1 live processing từ cache C2 đã khóa. D1 chạy lại ConceptGraphs hybrid mapping core cùng OccuSG theo thứ tự frame và phát unified revisions trước khi run kết thúc. D1 không phải live capture, realtime, full ConceptGraphs-Detect hay bằng chứng hỗ trợ mọi Replica scene.

## Current gates

- R0 checkpoint and R1 recipe/resource preflight: complete, see the versioned reports.
- R2 package builder/validator and CPU calibration/pose contract: implemented and locally tested. The actual pinned ConceptGraphs loader cannot be executed locally because this environment has no Torch; its real-loader check is in the notebook and remains pending Kaggle.
- R3: notebook is prepared, but Kaggle runtime is unverified. User-shared logs show an isolated micromamba setup was attempted and the pinned GradSLAM commit was fetched; the GradSLAM install then stopped because the isolated Python was missing, and a retry stopped at the free-disk guard. No environment version/import/pip-check, checkpoint download, reference-loader, inference, or pilot success is confirmed.
- Local adapter vertical slice: verified with a schema-aligned fixture against an existing OccuSG room snapshot. Object IDs/provenance are preserved, membership uses room geometry, room geometry/adjacency remain unchanged, and the existing spatial viewer is reused. This is local integration verification only; it is not ConceptGraphs inference output.
- Local hybrid mapping: a real 00802 object export exists from cached YOLO11n-seg masks + ViT-B-32 features/text features + pinned ConceptGraphs mapping core. It is explicitly not full ConceptGraphs-Detect or reference parity. Full ConceptGraphs-Detect loader/inference on Kaggle remains pending; causal object-to-room import is blocked on the Scope Bridge findings below.
- C2 fresh routes: ReplicaCAD `apt_0`–`apt_5` have completed configured 12-pose route runs; D1+D2 simultaneous GUI acceptance is documented for `apt_1`–`apt_5`. `apt_0` still lacks evidence that D2 XYZ was open during its producer. Routes are fixed observation poses, not autonomous movement. Membership and room semantics remain unavailable because spatial scope is unverified. These are workflow checks, not semantic/identity quality or general dataset-portability validation. Full per-scene status is in [REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md](REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md).
- Source/default and all legacy U/K/Q behavior remain unchanged.
- Room Belief v1: RB0/RB1 contracts are implemented; RB2 produced four experimental A tables from the pinned Qwen run and their local import/formula checks passed. Scores remain uncalibrated and are not defaults. Causal use on 00802 is still unavailable because spatial scope is not verified. No default room-label behavior is enabled.

### A.1 causal integration status

- Đã kiểm chứng trên timeline 29 frame: room lifecycle theo revision,
  frame-atomic RB1 updates, prefix-only semantic state, admission/A-table
  provenance và result mapping theo `event_id`.
- Scope được đánh giá theo từng prefix. `floor_0`, final replay snapshot hoặc
  pose Z không tự mở membership. Scope contract chỉ được chấp nhận khi có
  scene/sequence, coordinate frame, đơn vị, source refs tồn tại và hash khớp;
  contract sai/missing giữ gate `unavailable`.
- Artifact hiện hành:
  `artifacts/scene_graph_timeline/m4_causal_hybrid_00802_a1_v6/`.
  Run 00802 chưa có scope contract hợp lệ nên chưa tạo `room_contains` hoặc
  room semantic label; đây là giới hạn provenance, không phải quality claim.
- Tests A.1: 56 targeted tests pass. Prefix/full-run equivalence đầy đủ,
  metadata status page và end-to-end launcher vẫn là phần chưa triển khai.

### Mốc B — unified incremental graph/viewer (2026-10-16)

- Đã nối immutable object snapshots M0/M1, room geometry/lifecycle theo
  revision và A.1 causal object/evidence status vào unified graph v1.
  Mỗi graph revision ghi source hashes/revisions, object geometry refs,
  room geometry, prefix admission, semantic status, membership status/reason
  và `future_observations_used=false`. Final postprocess không được nhập vào
  prefix; không tạo object IDs mới.
- Artifact thực: `artifacts/scene_graph_timeline/unified_b_00802_v3/`
  (29 revisions). Với cache 00802, toàn bộ 29 revisions giữ membership là
  `unavailable_elevation_scope`, room semantics là
  `unavailable_membership_scope`, không có `room_contains` edges. Room graph
  vẫn hiển thị room lifecycle/adjacency đúng từng revision. Đây không phải
  room labeling đã chạy trên dữ liệu thật.
- Viewer: `artifacts/scene_graph_timeline/unified_b_00802_viewer_v4/`.
  Đã mở trên browser và kiểm tra frame đầu/giữa/cuối, seek ngược, play/pause,
  ảnh RGB/mask/overlay/crop và chọn room từ danh sách. Room ID/geometry được
  hiển thị riêng với semantic availability/reason; viewer không backfill
  label hay vẽ membership edge khi scope đóng.
- Fixture verified-scope riêng kiểm tra membership, room-belief update,
  transfer/retract và room retirement. Fixture chỉ xác minh integration
  contract, không phải bằng chứng quality cho 00802.
- Targeted tests bổ sung: 9 pass; `node --check` viewer JavaScript pass.
  Chi tiết command, schema và giới hạn ở
  [SCENE_GRAPH_MILESTONE_B_UNIFIED_GRAPH_00802.vi.md](SCENE_GRAPH_MILESTONE_B_UNIFIED_GRAPH_00802.vi.md).
- **Trạng thái mốc B:** revision integration và biểu diễn chính xác trạng
  thái unavailable đã kiểm chứng; membership/room labels trên run thật vẫn
  blocked bởi spatial scope. Không thay đổi Scope Bridge result C.

### C2 — fresh ReplicaCAD scene+route workflow (2026-10-16)

- Run: `artifacts/conceptgraphs_hybrid_runs/c2_replica_apt0_short_20261016_retry3/`.
- Launcher/config-driven workflow đã chạy capture → YOLO P2 → projection P3
  → OccuSG native room replay → ViT-B/32 image/text sidecars → pinned
  ConceptGraphs mapping core → unified graph → final-map viewer. Capture/P2
  được reuse sau hash verification từ cùng lần chạy incomplete; các stage còn
  lại chạy trong output retry mới. Không có dependency install, model download
  hay GPU use.
- Route: 12/12 fixed poses navigable/connected, 12 pose matrices, RGB-D
  160×120. ConceptGraphs tạo 15 hypotheses và sau min-detections=3/filter
  + reference postprocess còn 1 object. Không gọi số này là quality.
- OccuSG replay tạo 5 room nodes. P3 legacy cache có 0 accepted/18 rejected:
  14 có largest cluster 1–24 điểm dưới ngưỡng cấu hình 100, 4 thiếu valid
  depth points (25/36/77/85). Điều này không ngăn ConceptGraphs hybrid
  lifting từ P2+RGB-D, nhưng P3 vẫn được truyền vào/kiểm tra bởi OccuSG
  replay; không có P3 object detections được nhập trong run này.
- Unified graph không tạo cạnh `room_contains` từ room tới ConceptGraphs
  object; giữ nguyên 5 cạnh `room_adjacency` và 132 cạnh `room_contains`
  vốn có trong OccuSG snapshot (120 room→navigation, 12 room→agent). Membership là
  `unavailable_elevation_scope`, room semantics là
  `unavailable_membership_scope`; room geometry/adjacency không bị adapter sửa.
- Successful invocation mất 131.93 s nhưng capture/P2 được reuse; đó không
  phải thời gian fresh từ đầu. Capture 16.36 s và P2 4.22 s đến từ stage
  invocation trước. 48 targeted tests và 428 output hashes pass.
- Ở nghiệm thu C2 ban đầu, browser GUI của final-map viewer chưa được kiểm tra;
  việc đó không còn là blocker cho D1/D2 live timeline viewer. D2 đã xác nhận
  XYZ object clouds/camera trajectory trên prefix, còn room polygons giữ ở
  panel XY. Đây là hai viewer/run mode khác nhau.
- **Phạm vi đạt:** C2 kỹ thuật trên một ReplicaCAD `apt_0` config/route.
  Chưa chứng minh workflow chạy trên một config/scene thứ hai, không hỗ trợ
  mặc nhiên Replica gốc, và chưa triển khai live processing. Chi tiết/hash/
  command ở [SCENE_GRAPH_C2_FRESH_REPLICA_APT0_STATUS.vi.md](SCENE_GRAPH_C2_FRESH_REPLICA_APT0_STATUS.vi.md).

### Scope Bridge status (2026-10-16, post native diagnostic)

- Đã truy nguyên room producer: M1 tạo điểm world-frame từ RGB-D metric axial
  depth và pose Habitat đã đổi từ Y-up sang canonical Z-up; M2 tích lũy điểm
  XYZ trong OctoMap; M3/MapConverter chiếu occupancy sang XY. Tham số
  `min_free_height_m=0.3` bỏ các khoảng trống đứng quá thấp, không chọn tầng.
  Với mỗi ô, MapConverter lưu floor ở đáy khoảng free thấp nhất và ceiling ở
  đỉnh khoảng free cao nhất; đây là envelope của các khoảng, không phải layer
  selector. M4 nhận grid 2D (values, resolution, origin XY); polygon room chỉ
  giữ world XY và revision nguồn.
- Replay đã lưu không có `grid_trace`; 155 room-node revisions không có
  `floor_id`, `z_bounds`, `elevation` hoặc scope. Diagnostics chỉ có
  `m3_changed_bounds_xyz` theo update, không phải scope theo room. Vì vậy
  polygons hiện tại có thể gộp free space ở nhiều cao độ khi chiếu cùng XY.
- Đính chính trạng thái v5: `verified_single_observed_floor_scope` khi đó dựa
  vào `floor_0` của evaluation profile, P3 accepted layers và layer IDs trong
  OccuSG snapshot; P2 được kế thừa profile. P3 chỉ có layer metadata cho
  45/67 observations, scope contract là `null`, và room polygons không có
  elevation. Đây không phải bằng chứng room producer đã phân tầng. Giữ v5
  nguyên trạng như lịch sử; không dùng status đó làm scope verification.
- Hybrid mapping dùng P2 masks + cached metric RGB-D + `T_W_Copt` tính từ
  capture record trong `to_dets`; pose P3 chỉ cross-check khi có. Các
  observations không có accepted P3 row vẫn tham gia hybrid mapping. Object
  export ở capture/world, đơn vị mét, với transform identity; đây cho biết
  object points có world XYZ nhưng không tự tạo `floor_id`. Khi xây scope
  bridge, object scope phải lấy từ prefix hybrid geometry/lineage, không yêu
  cầu P3 row và không kế thừa `floor_0`.
- Đã chạy đúng một native M1–M10 replay có instrumentation trên capture 29
  frame đã khóa. Fixture kiểm tra trace bật/tắt đạt parity; replay khớp
  baseline ở snapshot/delta, M3 direct-source oracle và các quyết định M4
  theo frame. Không chạy lại hybrid mapping hoặc perception.
- Trace xác nhận M3 nhận các cell có nhiều free-height intervals rời nhau rồi
  lưu một envelope Z thấp/cao. Tuy nhiên các khoảng đó cũng có thể do
  furniture/occlusion; không có metadata phân loại chúng thành tầng. Gate
  kết quả `C_scope_evidence_insufficient`, không phải bằng chứng scene nhiều
  tầng và cũng không xác minh single-floor. Không tạo scope contract,
  membership hay room belief cho run này.
- Hybrid object prefixes độc lập đã được kiểm tra: 29 snapshot, 665 hypothesis
  clouds, 664,705 điểm XYZ hữu hạn, cùng capture-world frame/metre và identity
  transform. Elevation Z có sẵn theo prefix nhưng không có floor/layer ID.
  Final membership không được dùng để mở gate quá khứ.
- Bản replay giữ nguyên hash trace ban đầu. Một lỗi trường Z chẩn đoán trong
  trace được sửa sau replay và kiểm tra bằng fixture riêng; binary sửa đó
  không được dùng để chạy cache lần nữa. Xem
  [SCOPE_BRIDGE_00802_NATIVE_REPLAY_REPORT.vi.md](SCOPE_BRIDGE_00802_NATIVE_REPLAY_REPORT.vi.md)
  và `artifacts/scope_bridge/00802_29frame/run_20261016T022841Z/`.
- **Gate hiện tại: membership và room semantics vẫn unavailable.** Thay đổi
  producer nhỏ nhất cần đánh giá tiếp là xuất per-room-revision vertical
  support profile liên kết các cell/interval nguồn với polygon, có provenance
  và chỉ rõ phần nào là occupied/free/unknown; chỉ các scope được xác minh mới
  mở membership. Không triển khai thay đổi producer trong lượt diagnostic.

## Roadmap A–E (cập nhật 2026-10-16)

Mục tiêu là scene/route config-driven và graph viewer incrementally cập nhật.
Autonomous navigation, planner và camera-rate realtime không thuộc bước hiện
tại.

1. **C1 cached replay — đạt:** launcher gọi unified graph/viewer builders từ
   manifest đã khóa; HM3D 00802 cache replay khớp B. Chi tiết ở
   [SCENE_GRAPH_C1_CACHED_REPLAY_STATUS.vi.md](SCENE_GRAPH_C1_CACHED_REPLAY_STATUS.vi.md).
2. **C2 fresh scene+route — đạt trên sáu ReplicaCAD apartment configs:**
   `apt_0`–`apt_5` chạy 12 fixed poses ở 160×120. Đây là các portability
   checks trên apartments, không chứng minh mọi scene/dataset đều tương thích.
3. **Spatial scope/membership/room belief — pending riêng:** Scope Bridge
   trên 00802 insufficient; apt_0/apt_1 cũng giữ `unavailable_elevation_scope`.
   Không dùng `floor_0`, final snapshot hoặc giả định single-level để mở gate.
4. **D1+D2 simultaneous — đạt trên cache C2 apt_1–apt_5:** producer phát
   revisions và XYZ viewer nhận/render revisions trước khi producer hoàn tất.
   D1 là live processing từ cache, không phải simulator cấp RGB-D mới.
   `apt_0` simultaneous gate vẫn pending.
5. **E portability integration — đạt giới hạn:** các route C2 + D1/D2 đã được
   nghiệm thu trên apt_1–apt_5; apt_0 thiếu simultaneous GUI evidence. Đây là
   development routes, không phải holdout, quality evaluation hay khẳng định
   HM3D/MP3D đã tích hợp vào C2/D1/D2.
6. **Ưu tiên sau E (quyết định người dùng, 2026-10-16):** mở rộng scene/route
   bằng workflow C2 hiện có, sau đó D1/D2 trên cache. Live simulation S1–S3
   được deferred và không phải prerequisite để chạy scene mới.

| Mốc | Trạng thái hiện tại |
|---|---|
| A — Causal correctness | Lifecycle/frame/prefix correctness đã kiểm thử; spatial scope trên 00802 và ReplicaCAD runs vẫn unavailable, nên membership portion chưa đạt. |
| B — Unified incremental graph | Integration/viewer đạt; unavailable được biểu diễn rõ. Verified-scope fixture kiểm tra membership/belief/retract; không phải room-quality validation. |
| C1 — Cached replay | Đạt trên cache đã khóa; không phải fresh scene workflow. |
| C2 — Fresh scene/route | Đạt trên ReplicaCAD `apt_0`–`apt_5` với configs/routes riêng; không chứng minh các scene staging hoặc dataset HM3D. |
| C — End-to-end runner | C2 ReplicaCAD routes đã đi tới unified graph/viewer; scope/semantics vẫn unavailable. HM3D chưa nối vào C2 chung. |
| D1 — Live processing | Đạt trên cache C2 apt_1–apt_5; không phải live capture/simulation. |
| D2 — XYZ viewer | Simultaneous D1+D2 được nghiệm thu trên apt_1–apt_5; apt_0 GUI timing evidence còn thiếu. |
| E — Portability + D1/D2 acceptance | Đạt trong phạm vi các route ReplicaCAD apt_1–apt_5; không phải holdout/quality claim và chưa là HM3D portability. |
| Live simulation S1–S3 | Deferred theo quyết định người dùng; không phải prerequisite cho mở rộng scene/route. |
| Dataset portability | ReplicaCAD C2 đã chạy trên apt_0–apt_5. HM3D có 10 minival assets local và adapter/launcher legacy; chưa có C2→D1/D2 fresh-scene acceptance. MP3D chưa có integrated workflow được nghiệm thu. |
| Spatial membership/room labels | Pending; unavailable được giữ đúng, không có ConceptGraphs `room_contains` edges. |

Execution modes phải giữ riêng: cached playback đọc artifacts đã hoàn tất;
live processing chạy producers trên cache; live simulation lấy sensor mới mỗi
Habitat step; wall-clock realtime là phép đo riêng chưa có. Camera panels chỉ
để trình diễn và không được lén đưa vào predictor inputs. Scope/membership và
room labels không trở thành pass nhờ viewer hoặc simulator integration.

## Resource/privacy boundary

The local package contains only the seven locked RGB-D frames, axial depth in metres, calibration and poses. It excludes semantic GT, annotations, legacy P2/P3 outputs, Habitat scene assets and credentials. The capture metadata carries HM3D's academic/non-commercial notice. Third-party transfer authorization is unverified; no upload was performed. User must verify license/transfer permission and explicitly authorize any upload.

## Bằng chứng nghiệm thu E và scene mở rộng (cập nhật 2026-10-17)

Các bullets đầu giữ bằng chứng gốc của E trên apt_1. Bổ sung apt_2–apt_5 và
ma trận đầy đủ được ghi tại inventory ReplicaCAD bên dưới. Lịch sử run cũ giữ
nguyên; phần trên phản ánh trạng thái mới nhất.

- **C2:** một config/route thứ hai chạy trên ReplicaCAD `apt_1`, 12 fixed
  pose+yaw steps tại 160×120. C2 capture → perception/features → ConceptGraphs
  mapping + OccuSG room build → unified graph/viewer hoàn tất. Đây là
  config-driven portability check trên một scene thứ hai, không phải bảo đảm
  mọi scene ReplicaCAD/HM3D/MP3D đều dùng được.
- **D1+D2 simultaneous:** D1 chạy lại mapper/OccuSG trên cache C2 để phát 12
  graph revisions; D2 XYZ viewer đã mở và nhận revisions trước khi producer
  hoàn tất. GUI thực tế kiểm tra live-follow, historical seek, object lineage,
  final postprocess stage, XYZ cloud/camera path và XY room panel. Lệnh mở:
  `./run_scene.sh open d1_c2_replica_apt1_short_e_v1_retry1 live --port 8766`.
- **Mốc E trên một scene/route thứ hai:** đạt cho portability integration và
  simultaneous D1+D2 acceptance trong phạm vi run trên. Không phải
  multi-scene quality evaluation hoặc holdout.
- **Phân biệt execution modes:** cached playback xem artifacts đã hoàn tất;
  D1 là live processing từ cache đã có; live simulation với RGB-D/pose mới
  mỗi Habitat step chưa có; wall-clock realtime chưa đo. E không biến D1
  thành live simulation.
- **Kết quả object:** 12 hypotheses trước filter, 0 object sau
  `min_detections=3` và 0 sau final merge. Giữ nguyên config, không tuning.
  Đây là giới hạn evidence của route ngắn trong run này, không phải metric
  accuracy.
- **Room state:** room geometry/adjacency hiển thị; membership và semantics
  vẫn `unavailable_elevation_scope` / `unavailable_membership_scope`.
  Không có multi-floor claim hoặc ConceptGraphs `room_contains` edge.
- **Kiểm chứng:** 63 targeted tests pass; recovery chỉ build lại unified
  graph/viewer từ outputs đã có sau khi sửa gate chấp nhận zero retained
  objects; mapping/capture không bị chạy lại trong recovery. Chi tiết, hashes,
  timings và limitations tại
  [SCENE_GRAPH_E_APT1_D1D2_STATUS.vi.md](SCENE_GRAPH_E_APT1_D1D2_STATUS.vi.md).
- **Mở rộng ReplicaCAD sau E:** apt_2–apt_5 có C2 và D1+D2 simultaneous
  acceptance theo route đã khóa. apt_0 vẫn thiếu simultaneous XYZ evidence.
  Tổng inventory giữ 91 scene instances; 84 staging scenes và `empty_stage`
  tiếp tục pending route/workflow. Chi tiết per-scene tại
  [REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md](REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md).

### Quyết định ưu tiên sau E (2026-10-16)

**Ưu tiên hiện tại:** xây registry và run interface dataset-agnostic cho
ReplicaCAD + HM3D local, sau đó nghiệm thu một HM3D pilot qua C2→D1+D2.
Đây là tích hợp adapter theo contract, không phải bảo đảm mọi scene tự chạy
chỉ bằng đổi ID.

- **ReplicaCAD:** local inventory có 91 instances (apt_0–apt_5, 84 staging,
  `empty_stage`). C2 đã chạy apt_0–apt_5; D1+D2 simultaneous acceptance đạt
  apt_1–apt_5, còn apt_0 pending. Staging scenes chưa có route configs.
  Xem [REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md](REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md).
- **HM3D:** 10 local minival scenes 00800–00809 có basis mesh + navmesh.
  Routes/caches tập trung ở 00800, 00802 và 00808. Legacy launcher/scene
  adapter nhận HM3D, và C1 cached replay 00802 đã đạt; C2 runner vẫn khóa
  `replica_cad`, nên fresh HM3D C2→D1/D2 chưa được nghiệm thu. Kiểm kê và
  proposal registry/pilot nằm trong
  [LOCAL_MULTI_DATASET_SCENE_PORTAL_PROPOSAL.vi.md](LOCAL_MULTI_DATASET_SCENE_PORTAL_PROPOSAL.vi.md).
- **MP3D:** chưa có loader/workflow scene+route tích hợp và nghiệm thu;
  file `reference_code_mp3d.yaml` tự nó không chứng minh hỗ trợ MP3D.
- **Proposal tại 2026-10-16 (được supersede bởi update dưới):** HM3D 00802
  `main`, route 29 bước 160×120,
  chọn vì có assets, route, RGB-D/P2/P3 cache, room snapshot và C1 replay.
  Đề xuất implementation + regression tests 5–8 giờ, cộng 40–80 phút cho
  preflight, một C2, một D1/D2 GUI acceptance. Đây là ngân sách dự kiến,
  chưa được duyệt; chi tiết ở proposal registry.
- **Vẫn pending:** membership và room labels do `unavailable_elevation_scope`;
  không đổi scope gate. S1–S3 live simulation được deferred; không capture
  RGB-D từng bước từ simulator trong pilot scene expansion.

Tại thời điểm đoạn trên được ghi, HM3D pilot chưa được duyệt/chạy.
ReplicaCAD apt_2 đã có run artifact; lệnh và evidence nằm trong
[REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md](REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md).

### Cập nhật cuối 2026-10-17 — HM3D pilot đạt

Đính chính trạng thái pending trong các đoạn lịch sử phía trên. Shared
registry/menu/adapter vẫn là một checkpoint implementation riêng; HM3D
00802 `main` hiện đã chạy qua C2 và nghiệm thu simultaneous D1+D2:

- Workflow hoàn tất:
  `scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T063106Z_f91798`;
  C2 `c2_20261017T063106Z_99bc38`; D1
  `d1_20261017T063106Z_554073fd`.
- D2 được mở ở trạng thái chờ, người dùng xác nhận browser-ready, sau đó
  producer bắt đầu. Browser thật render revision 4/29 khi D1 còn `running`
  và revision 29/29 sau khi completed. Đây là live processing từ C2 cache,
  không phải live capture hoặc realtime.
- C2 route có 29 steps, 160×120, 26 unique poses; 17 hypotheses trước
  filter, 2 objects sau final postprocess, 5 room nodes. Không suy quality từ
  các số này.
- Membership `unavailable_elevation_scope`, room semantics
  `unavailable_membership_scope`; không có ConceptGraphs-object
  `room_contains` edges. Scope Bridge gate không đổi.
- Integrity: toàn bộ 767 C2 output hashes và 795 D1 immutable output hashes
  khớp; workflow manifest trỏ đúng hash C2/D1. D1 manifest có hai hash lồng
  `viewer.assets_sha256` cũ cho các file live `live_head/index`; final file
  hashes khớp immutable ledger, nhưng embedded asset references còn stale.
  Chi tiết và lịch sử run nằm trong
  [HM3D_00802_SHARED_WORKFLOW_PILOT_20261017.vi.md](HM3D_00802_SHARED_WORKFLOW_PILOT_20261017.vi.md).
- Có ba workflow invocations trong toàn lượt: một preflight-only fail RAM;
  một C2 hoàn tất nhưng D1 bootstrap dừng `EPERM` trước producer; một C2
  hoàn tất và D1+D2 simultaneous đạt. Hai full C2 executions được giữ làm
  lịch sử; chỉ một D1 producer execution thành công.

**Trạng thái hiện tại:** registry/menu/adapter gates đạt; C2 và simultaneous
D1+D2 đạt cho HM3D 00802/main. Đây chưa phải hỗ trợ tổng quát 10 HM3D scenes
hoặc toàn bộ scene types. Membership, object semantic quality và room labels
vẫn pending riêng. Infrastructure v1 được đóng trong phạm vi pilot; bước
tiếp theo là proposal chất lượng evidence/annotation, không phải thêm scene
hoặc S1–S3 tự động.

### Update 2026-10-17 — shared scene/route workflow

Đính chính trạng thái ở các đoạn phía trên: implementation registry/menu và
HM3D C2 adapter đã được duyệt và được nối vào launcher hiện hành.

- `configs/launcher/scenes.json` là nguồn catalog; `integration_v2/scene_registry.py`
  resolve 91 ReplicaCAD và 10 HM3D local scenes cùng route configs. Terminal
  menu `./run_scene.sh menu` và CLI `scene-run` dùng chung resolver/orchestration.
  Scene thiếu route hiện rõ lý do block. Các lệnh legacy vẫn được giữ.
- Resolver/config tests xác nhận ReplicaCAD `apt_5/navmesh` và HM3D
  `00802-wcojb4TFT35/main`; workflow/manifest-status tests xác nhận acceptance
  chỉ lấy từ completed run manifests. Targeted suite sau cùng đạt **59 passed**.
- Đã thử đúng một invocation HM3D pilot:
  `scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T061655Z_e0390d`.
  C2 `c2_20261017T061655Z_79bb9b` dừng ở preflight do available RAM
  `2,050,600,960 B` thấp hơn gate `3,221,225,472 B`. Không capture,
  perception, feature build, mapping, native replay hoặc D1/D2 producer nào
  chạy trong lượt này. Workflow manifest giữ `failed` và D1/D2
  `not_started_due_to_prior_failure`; không có simultaneous acceptance.
- Một lỗi manifest-status được sửa: C2/preflight exception giờ ghi stage
  `failed`, và workflow đánh dấu downstream stages chưa chạy. Manifest pilot
  mới giữ correction history; artifact inputs/config không bị sửa.
- Pilot runtime chưa được đo: preflight dừng sau khoảng 2.5 giây. Development
  nằm trong trần 12 giờ đã duyệt. Runtime capture/perception/mapping/D1/D2 sẽ
  chỉ được đo nếu host có RAM khả dụng ít nhất 3 GiB khi người dùng chạy lại
  cùng lệnh:
  `./run_scene.sh scene-run --dataset hm3d_minival_v0.2 --scene 00802-wcojb4TFT35 --route main --port 8766`.

**Trạng thái:** shared registry/menu + HM3D adapter/code gates đạt; HM3D
00802 C2→D1+D2 integration acceptance pending vì preflight resource gate.
Điều này không thay đổi status đã ghi cho ReplicaCAD routes. Membership và
room semantics vẫn unavailable do spatial scope; không có quality hoặc
realtime claim. Chi tiết và manifest ở
[LOCAL_MULTI_DATASET_SCENE_PORTAL_PROPOSAL.vi.md](LOCAL_MULTI_DATASET_SCENE_PORTAL_PROPOSAL.vi.md)
và `artifacts/scene_workflows/scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T061655Z_e0390d/`.

### Update 2026-10-17 — operational capture profile 640×480

Theo quyết định người dùng, run chất lượng mới trên ReplicaCAD và HM3D
dùng `native_640x480_v1` làm profile mặc định chung. Menu cho phép chọn
profile và Enter chọn 640×480; `scene-run` cũng mặc định 640×480, còn
`legacy_160x120_smoke_v1` phải được chọn tường minh. HFOV/extrinsics/depth
contract được giữ theo route, intrinsics do Habitat adapter tính. Route
JSON, manifests và runs cũ không bị sửa. Detector input/preprocessing,
model, thresholds, matcher và admission giữ nguyên.

Cache mới bind resolution, intrinsics, sensor settings, raw RGB/depth/mask
hashes và detector preprocessing; P3/feature reuse xác minh lineage trước
khi đọc. Annotations không được nối theo observation ID đơn thuần khi nguồn
ảnh khác.

Đã hoàn thành assistant visual triage của 18 P2 detections từ candidate
640×480 ba pose; đây không phải human-confirmed labels/identity/purity.
Chi tiết ở
[HM3D_00802_640_CAPTURE_PROFILE_AND_BASELINE_PROPOSAL.vi.md](HM3D_00802_640_CAPTURE_PROFILE_AND_BASELINE_PROPOSAL.vi.md).

Baseline C2 HM3D 00802/main, 29 poses, native capture 640×480 đã hoàn tất
ở run `hm3d00802_main29_640_quality_v1_attempt2` trong 552.88 giây.
Một attempt trước được giữ nguyên và fail trước frame đầu do import không
tương thích Python 3.9/3.10; không phải lỗi tài nguyên. Run thành công có
75 P2 detections, 40 hypotheses, 34 filtered theo min-detections=3, 6
retained và không có postprocess merge. Đây là workflow result, chưa phải
quality claim; annotation 160×120 không được reuse. Chi tiết/hash/runtime
ở `artifacts/quality_baselines/hm3d00802_main29_640_v1/c2_execution_report.vi.md`.

Object viewer artifact đã tạo; GUI visual acceptance chưa xác minh do
browser URL policy chặn mở file. Membership và room labels vẫn unavailable;
S1–S3/live capture và UI work vẫn deferred.

### Update 2026-10-18 — smooth-route T0/T1 camera-only checkpoint

Đối chiếu Habitat-Sim 0.3.3 xác nhận camera-only worker có thể phát ảnh và
depth từ các bước `sim.step`, lấy actual agent/sensor poses và K từ calibration
adapter. Đã thêm route executor/keyframe policy dùng shared registry và native
capture profile 640×480; không chạy perception hoặc mapping.

- Một route development ngắn riêng `t1_navmesh_clearance` trên HM3D 00802
  hoàn tất tại `smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7`:
  30 camera frames, 2 candidate/admitted raw keyframes, không va chạm.
  Manifest, events, hashes và timing ở
  `artifacts/smooth_route_previews/smooth_t1_hm3d00802_t1_navmesh_clearance_20261018_v7/`.
- Các đoạn lấy từ main route đã thử bị chặn bởi collision hoặc thiếu
  footprint clearance; navmesh ghi `include_static_objects=false`, do đó
  valid navmesh route một mình không chứng minh collision-free trong scene.
  Route T1 thành công được chọn riêng bằng navmesh clearance, không predictor
  hoặc semantic inputs; đây chỉ là demo 0.4 m, không nghiệm thu main route.
- Targeted suite đạt **48 passed**; manifest/hash validation đạt. HTTP viewer
  trả 200 và frame ảnh đã được kiểm tra, nhưng browser GUI acceptance còn
  pending vì browser không hiện diện trong CUA session.
- T1 chỉ hoàn tất camera-only preview và keyframe logging. Chưa có stateful
  object/room graph updates; membership và room semantics vẫn unavailable.
  T2 mapper sessions và T3 causal evidence ledger chưa triển khai; live
  simulation, realtime, scope và room labels vẫn pending. S1–S3 live-session
  refactor không được bắt đầu trong lượt này.

Chi tiết, commands, timing/RSS và giới hạn ở
[SMOOTH_ROUTE_T0_T1_REPORT.vi.md](SMOOTH_ROUTE_T0_T1_REPORT.vi.md).

### Update 2026-10-19 — T3 verified-only prefix replay trên T1.1

Đã nối 13 prefix revisions của mapper-only run
`hm3d00802_t1_1_all13_mapper_only_20261019_v1` vào RB1/T3 hiện có bằng
semantic replay local; không chạy capture, perception, Qwen, ConceptGraphs
mapping hoặc OccuSG replay. Admission đọc `obj_min_detections=3` từ
effective mapper config; consensus dùng `exact_camera_pose_matrix_v1`.
Object/room/unified revision IDs được join theo frame ID và provenance,
không coi các số revision là cùng một namespace.

Prefix admission và object semantic consensus đã được tính trên các snapshot
tại frame tương ứng. Verified-only scope vẫn unavailable; membership,
room_contains, room candidates và committed room labels không được tạo.
Bảng A có bốn variants nguyên trạng; không có variant winner hoặc quality
claim. Declaration assumed cho T1.1 chỉ là draft chờ quyết định người dùng.

Viewer derived A-only được build với 13 prefixes và source evidence panels.
HTTP entrypoint/assets đã kiểm tra; GUI interaction chưa được browser-tested.
T3 cached prefix replay đã chạy; T4 semantic update sau mỗi unified commit
trong camera-live vẫn pending. Chi tiết và lệnh mở ở
[T1_1_T3_PREFIX_ROOM_BELIEF_20261019.vi.md](T1_1_T3_PREFIX_ROOM_BELIEF_20261019.vi.md).

### Update 2026-10-19 — T4 semantic wiring/model-free verified

- Đã nối `RoomBeliefCommitSession` vào camera-live coordinator sau khi
  object và room branches cùng trả kết quả cho frame, sau khi unified graph
  artifact được ghi, nhưng trước khi live pointer/journal được publish và
  trước ACK hai mapper. Semantic overlay phải khớp graph hash, frame ID,
  unified/object/room revisions và source hashes.
- Session giữ bốn RB1 ledgers xuyên commit, dùng consensus/view grouping và
  category-cap core hiện có, đọc bốn A variants đã pin, cùng admission
  `obj_min_detections=3` từ cấu hình ConceptGraphs mà cached-packet worker
  cũng khởi tạo. `verified_only` là cấu hình camera-live duy nhất được phép;
  do scope chưa có bằng chứng verified, membership unavailable và room
  scoring `not_scored`. Declaration B của T1.1 không được chuyển sang capture.
- Semantic lỗi tạo overlay `failed` gắn đúng graph revision, không mang nhãn
  cũ; graph artifact vẫn giữ nguyên. Viewer xác minh overlay SHA-256 và ràng
  buộc revision trước khi hiển thị. Browser ACK/render không nằm trong đường
  producer.
- Model-free verification: **89 targeted tests passed**, Python compile,
  viewer overlay binding test, atomic-revision fixture và `node --check`
  inline viewer scripts đều đạt. Một startup dry-run qua cùng launcher đạt
  D2 HTTP/bootstrap, 5 fake worker readiness IPC và route preflight tới
  capture boundary; cleanup đủ 5 workers. Xác nhận 0 model loads, 0 camera
  frames và Habitat simulator chưa khởi tạo. Chưa chạy capture, perception, encoder,
  ConceptGraphs, OccuSG hoặc Qwen; do đó T4 **live pilot chưa được nghiệm thu**.
- GUI T1.1 V5 vẫn pending: CUA hiện không có browser/app surface. Không tính
  manifest/hash/HTTP checks là GUI acceptance. Handoff và checklist ở
  [T4_ROOM_BELIEF_WIRING_20261019.vi.md](T4_ROOM_BELIEF_WIRING_20261019.vi.md).
- T3 B replay cho 13 prefix T1.1 sau đó đã được người dùng duyệt và chạy
  riêng dưới declaration `assumed_not_verified`; kết quả ở
  [T1_1_T3_ASSUMED_B_REPLAY_20261019.vi.md](T1_1_T3_ASSUMED_B_REPLAY_20261019.vi.md).
  Đây không phải scope verification và không được dùng cho capture mới.
- Route timing đã biết không thuận lợi cho overlap: T1.1/v8 có 113 camera
  frames, 13 keyframe candidates, capture-frame span 3.642 s wall-clock; hai
  mapper chỉ xử lý 2 keyframes do 11 pending bị supersede. Unified revision
  đầu tiên đến 6.276 s sau capture end. Đây là bằng chứng về run cũ, không
  dự báo chắc chắn cho run mới; cùng route có nguy cơ cao không đạt graph /
  camera overlap. Không tăng tốc, kéo dài route hoặc đổi queue để che giới hạn.
- Trạng thái kế hoạch: **T4 wiring/model-free verified**; **T4 live pilot
  pending**; không phải realtime, room-semantic quality hoặc scope verification.

### Update 2026-10-19 — publication/timebase hardening và pilot startup

- Publication được kiểm tra xuyên `_join_one → semantic overlay → atomic
  publisher → live index/head → HTTP → D2` bằng fixture hai revisions;
  `tests/test_t4_publication_http_e2e.py` đạt. Test không thay thế revision
  thật từ mapper.
- Timebase v4 khóa Habitat `dt=0.04 s`, 25 FPS, RTF 1, speed 0.2 m/s /
  30°/s, tương ứng 0.008 m / 1.2° mỗi bước. Config hash-bound mới và
  geometry-based frame budget đã kiểm tra; chưa có capture mới để xác nhận
  actual FPS/trajectory.
- Model-free targeted suite: **31 tests passed**. Startup dry-run qua cùng
  launcher đạt tới capture boundary với fake workers, không load model hoặc
  capture.
- Pilot `t4_paced_publication_timebase_00802_v2` đã mở D2 waiting page và
  đạt readiness của 5 workers, nhưng bị validator từ chối tại pre-capture
  vì nó chỉ cho `control/` rỗng trong khi cùng run có explicit
  `control/start.json`. Camera/perception/mapper frame updates đều bằng 0;
  không có unified revision. Pilot không retry.
- Validator hiện tại đã được sửa và có fixture kiểm tra start marker đúng
  run/hash/entrypoint; run failed và artifacts của nó vẫn giữ nguyên. T4
  live pilot, camera/graph overlap, render revision thật và room scoring
  tiếp tục pending. Chi tiết:
  [T4_PUBLICATION_TIMEBASE_PILOT_20261019_UPDATE.vi.md](T4_PUBLICATION_TIMEBASE_PILOT_20261019_UPDATE.vi.md).

### Update 2026-10-19 — T4 startup contract closure

Startup dry-run v1 trước đây dừng trước route validator do lỗi browser-ready
callback; pilot thật tiếp theo cũng không có bằng chứng một dry-run hoàn chỉnh
đã pass trước khi bắt đầu. Hai sự kiện lịch sử này được giữ nguyên trong
addendum. Theo quyền cho phép mới, dry-run đầu bị sandbox từ chối mở localhost
socket trước worker readiness; dry-run thứ hai đi hết production launcher,
HTTP/control handshake, readiness IPC của 5 fake workers, Habitat route
validator/preflight và dừng ngay trước lần gọi `sim.get_sensor_observations()`.

Dry-run pass ghi 0 model loads, 0 camera frames, 0 inference, 0 mapper updates,
0 unified revisions; worker, route child và viewer server thuộc run đều dừng
sạch. Tests model-free đạt **47 passed**. Đây là xác nhận startup contract ở
ranh giới trước capture, không phải capture/pipeline/overlap hoặc GUI
acceptance. HTTP/control đã được kiểm chứng; GUI trực quan chưa. T4 live pilot
vẫn pending và cần duyệt riêng. Chi tiết, hashes và giới hạn ở
[T4_STARTUP_ATTEMPT_ADDENDUM_20261019.vi.md](T4_STARTUP_ATTEMPT_ADDENDUM_20261019.vi.md).

### Update 2026-10-19 — T4 timebase A pilot outcome

Đã khóa và dùng contract `habitat-motion-timebase/v2`: giữ `dt=0.040 s`,
increments `0.008 m` / `1.2°`, timestamps simulation lấy nguyên trạng từ
`get_world_time()`, tốc độ thực tính theo pose delta / observed delta. Một
pilot mới duy nhất chạy trên HM3D 00802, route T1.1, 640×480; không retry.
Effective-config SHA-256 là
`adb5f97c16b1627b39026e2fa98354ddd38a99ece46b6e6be7eabe8ced9497e7`.

- Pilot producer hoàn tất bình thường: 278 frames, 277 actions, 0 collision
  events, 23.761 FPS, RTF 1.0033. Observed timestep mean 42.224 ms, p50
  40 ms, p95/max 48 ms; không clamp timestamp hoặc thêm simulator step.
- 13 candidates/raw keyframes; 11 superseded trước processing dispatch, 2
  admitted/processed. Persistent P2/P3/OpenCLIP và mỗi mapper chạy qua hai
  keyframes; hai nhánh cùng tạo hai unified revisions. Mỗi revision có
  semantic overlay hash-bound.
- Perception bắt đầu trước capture end, nhưng revision 1 commit **5.959 s
  sau** camera frame cuối/capture completion. Gate unified graph/camera
  overlap vì vậy **chưa đạt**. D2 đã ready trước start và về sau hiển thị
  revision thật, nhưng render đồng thời với capture không thể đạt khi chưa có
  graph revision trước camera end. Đây là producer execution thành công với
  overlap acceptance chưa đạt, không phải pilot code failure.
- `verified_only` giữ nguyên. Hai semantic updates đều `not_scored` do
  `unavailable_elevation_scope`; membership và room labels không được tạo.
  Đây không phải scored abstention, accuracy hoặc verified scope.
- Tất cả năm worker thoát mã 0; run/bootstrap và terminal mapper stage hoàn
  tất. Terminal semantic lineage còn unavailable, không backfill prefix.
- Kiểm tra inventory hash gốc: 386 entries, 373 khớp, 13 lệch, không thiếu;
  cả 13 lệch thuộc logs/journals, bootstrap hoặc live pointers còn được cập
  nhật theo lifecycle. 370 immutable outputs khớp inventory gốc. Giữ inventory
  gốc nguyên trạng và phân loại lại trong
  [integrity_final_addendum.json](../artifacts/live_processing_runs/t4_camera_live_00802_t1_1_timebaseA_20261019_pilot_v1/integrity_final_addendum.json).
- Handoff chi tiết, gate-by-gate và hashes ở
  [T4_TIMEBASE_A_PILOT_ACCEPTANCE_20261019.vi.md](T4_TIMEBASE_A_PILOT_ACCEPTANCE_20261019.vi.md).

Trạng thái kế hoạch: T4 camera-live capture → perception → hai mapper → unified
revision và per-revision semantic overlay **đã chạy end-to-end** trong đúng
run này; T4 **chưa đạt acceptance overlap/render-during-capture**. Không phải
camera-rate realtime, room-label scoring/accuracy hoặc verified membership.
Bước nhỏ nhất hướng tới semantic demo là xin quyết định riêng về một
declaration experimental `assumed_single_level_demo` giới hạn đúng run này;
không chuyển declaration main29 và không áp dụng mặc định.
