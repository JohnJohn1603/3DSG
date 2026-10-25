# Bàn giao toàn cảnh dự án Scene Graph

**Ngày chốt:** 21-10-2026  
**Workspace:** /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG  
**Mục đích:** tài liệu nền để chọn hướng mới mà không nối tiếp nhầm một pilot cũ, nhầm chế độ chạy hoặc nhầm mức bằng chứng.

## 1. Tóm tắt điều hành

Dự án đã đi từ các nhánh thử nghiệm object fusion và room semantics riêng lẻ tới workflow nhận scene/route, lấy RGB-D, dựng object map bằng ConceptGraphs, dựng room map bằng OccuSG, ghép unified graph và xem graph theo revision. Ở pilot mới nhất, camera Habitat chạy trong khi keyframes được xử lý bởi các worker sống xuyên run; nhiều unified revisions đã được commit và hiển thị khi camera vẫn còn chạy.

Trạng thái cần mang sang hướng mới:

- **Đã có:** cached/batch workflows và registry/menu; C2 trên các ReplicaCAD apartments đã cấu hình và HM3D 00802; D1/D2 trên cache; session API cho ConceptGraphs, OccuSG và perception; replay đủ 13 keyframes T1.1; room-belief causal integration và viewer A/B; một camera-live T4 run đạt graph/camera overlap trên HM3D 00802.
- **Chưa có:** verified object–room membership hoặc verified room labels; room-label accuracy; workflow đã nghiệm thu cho mọi scene ReplicaCAD/HM3D; MP3D workflow tích hợp; autonomous ObjectNav; camera-rate realtime.
- **T4 mới nhất:** run **t4_camera_live_00802_geometry_candidate_20261020_v5** hoàn tất. Có 1,362 camera frames, 31 keyframe candidates, 5 keyframes được xử lý bởi cả hai mapper, 5 unified revisions. Ba unified revisions đầu commit trước capture kết thúc; browser thật quan sát được revision 2 và 3 khi capture còn hoạt động. Đây là camera-live incremental integration và overlap trên đúng pilot này, không phải realtime hay quality gain.
- **Room semantics trong pilot mới nhất:** verified-only; scope unavailable_elevation_scope; semantic updates not_scored; không có ConceptGraphs-object room_contains edges hay room label. Không áp assumed declaration từ run khác.
- **Không có thay đổi pipeline ở lượt tổng hợp này.** File này là handoff; source, config, artifacts và baseline gốc được giữ nguyên.

## 2. Nguồn sự thật và phạm vi rà soát

Các kế hoạch chuẩn:

- Kế hoạch scene graph chính: /home/kiettran05/Documents/Codex/2026-09-22/c-c/SCENE_GRAPH_PROJECT_REVIEW_AND_COMPLETION_PLAN.vi.md
- Kế hoạch smooth route/keyframes/room belief: /home/kiettran05/Documents/Codex/2026-09-22/c-c/SMOOTH_ROUTE_KEYFRAMES_ROOM_BELIEF_IMPLEMENTATION_PLAN.vi.md
- Room Belief V1 đã chốt: [docs/ROOM_BELIEF_V1_IMPLEMENTATION_PLAN.vi.md](docs/ROOM_BELIEF_V1_IMPLEMENTATION_PLAN.vi.md)
- Kiến trúc/tình trạng tích hợp: [docs/ACTIVE_ARCHITECTURE.vi.md](docs/ACTIVE_ARCHITECTURE.vi.md)
- Index lịch sử U/K/Q: [docs/LEGACY_INDEX.vi.md](docs/LEGACY_INDEX.vi.md)

Tài liệu này tổng hợp các kế hoạch trên cùng milestone reports, source entrypoints, registry/configs, scene inventory, review outputs và manifests liên quan. Binary datasets, model weights, toàn bộ third-party source và từng file cache không được diễn giải như văn bản; đường dẫn/hash được giữ ở manifests và resource locks. Đây là tổng hợp trạng thái dự án, không phải audit từng dòng mọi file.

**Độ ưu tiên khi các nguồn bất đồng:**

1. Artifact/run manifest và ledger của run thực mới hơn.
2. Báo cáo nghiệm thu của run đó và xác nhận GUI trực tiếp.
3. Báo cáo milestone trước đó.
4. Kế hoạch/architecture page có ngày cập nhật cũ hơn.

Một số tài liệu chủ động chưa cập nhật sau T4 v5. Ví dụ, ACTIVE_ARCHITECTURE và phần đầu smooth-route plan còn nói overlap pending dựa trên pilot 19-10 hoặc route preflight 20-10. Chúng không phủ nhận run v5 ngày 20-10; dùng manifest v5 bên dưới làm trạng thái T4 mới nhất. Giữ tài liệu cũ làm lịch sử, không sửa ngược trong lượt này.

Lệnh git status tại workspace báo not a git repository. Vì vậy không có Git commit/diff đáng tin cậy để mô tả thay đổi workspace; run manifests và source SHA-256 là nguồn provenance. Cần tính đến điều này nếu hướng mới cần version control hoặc rollback.

## 3. Mục tiêu sản phẩm đã chốt

Đích dài hạn:

    chọn dataset → chọn scene local → chọn route/config → chạy agent/simulator
    → lấy RGB-D + pose → cập nhật object/room graph theo thời gian
    → xem camera và unified 3D scene graph trong cùng viewer

Người dùng muốn chọn một map như chọn map trong game, rồi chạy cùng pipeline/viewer bằng config. Trải nghiệm đích có camera chuyển động tự nhiên và preview liên tục; graph chỉ cập nhật ở keyframes hữu hạn để không đưa mọi camera frame vào perception/mapping.

Room label là thuộc tính RoomNode, được cập nhật từ object evidence đủ điều kiện qua frozen A tables và ledger; không phải nhãn độc lập dự đoán cho mỗi ảnh.

**Ngoài phạm vi đã chốt:**

- Chưa phải autonomous ObjectNav, planner hoặc robot controller.
- Chưa yêu cầu camera-rate inference hoặc wall-clock realtime.
- Không cần gửi mọi frame camera qua detector/mappers.
- Không ép mỗi object phải được giữ lại hoặc mỗi room phải có label.
- Không coi preview đẹp, object count, point count hoặc nhiều frame là accuracy.
- Không suy verified single-floor từ floor_0, độ cao camera gần nhau hay route nằm trên cùng navmesh island.
- Không dùng assumed scope làm mặc định hoặc trộn vào verified metrics.

## 4. Kiến trúc đang tồn tại

### 4.1. Phân quyền các thành phần

    Habitat capture / simulator
      ├── RGB, depth, K, pose, timestamp, source hashes
      ├── P2/P3/OpenCLIP perception theo frame/keyframe
      ├── ConceptGraphs session → object identity, geometry, object revisions
      └── OccuSG session → occupancy, room IDs, room geometry, adjacency
                    ↓
       coordinator ghép cùng frame identity và source hashes
                    ↓
       unified graph revision + optional semantic overlay
                    ↓
       D2 viewer: camera/evidence, room XY, object/trajectory XYZ

- **Habitat:** scene, navmesh, camera/simulator, capture, pose và timebase.
- **P2:** ONNX YOLO11n-seg theo profile đang khóa; masks/raw detections.
- **P3:** RGB-D/masks/K/pose geometry adapter/room input theo contract hiện có. P3 rejection không tự xóa P2 observation khỏi ConceptGraphs nếu hybrid contract cho phép lift trực tiếp.
- **OpenCLIP:** ViT-B/32 image/text feature path ở local workflow; text input/cache có provenance.
- **ConceptGraphs:** object mapping/association/fusion. Không chạy lại legacy M6/K4 để gán identity lần hai.
- **OccuSG non-ROS:** occupancy, region/room lifecycle, room geometry và adjacency.
- **Adapter/coordinator:** frame identity join, source hashes, atomic unified revision, semantics overlay binding, lifecycle/error state.
- **Viewer D2:** HTML/JavaScript read-only viewer; XYZ point clouds và trajectory, room polygons ở panel XY riêng. Không dựng room volume thiếu elevation.

Đây là hybrid local path, không phải full upstream ConceptGraphs-Detect recipe GroundingDINO + SAM ViT-H + OpenCLIP ViT-H-14. Không gọi YOLO11n + ViT-B/32 là full ConceptGraphs-Detect hoặc parity với paper/reference perception.

### 4.2. Source entrypoints để định hướng

- Launcher/menu/CLI: run_scene.sh, scripts/scene_launcher.py.
- Registry/resolver: configs/launcher/scenes.json, integration_v2/scene_registry.py.
- Fresh route workflow: integration_v2/fresh_replica.py.
- Smooth route/geometry: integration_v2/smooth_route.py, integration_v2/camera_pacing.py, scripts/run_habitat_smooth_route_preview.py.
- Camera-live orchestration: integration_v2/camera_live.py, integration_v2/camera_live_startup.py, integration_v2/camera_live_contract.py, integration_v2/live_evidence.py.
- Current-frame perception: integration_v2/perception_session.py, integration_v2/perception_feature_sessions.py.
- Stateful mapper sessions: integration_v2/conceptgraphs_session.py, integration_v2/occusg_session.py.
- Cached pair coordinator: integration_v2/t2d_coordinator.py, integration_v2/live_processing.py.
- Unified graph/timeline: integration_v2/unified_scene_graph.py, integration_v2/unified_incremental_timeline.py.
- Room belief: src/standalone_sg/semantics/room_belief_v1.py, integration_v2/room_belief_commit_session.py.
- D2 viewer build/serve: scripts/build_d2_xyz_viewer.py, scripts/serve_causal_timeline_viewer.py, scripts/causal_timeline_viewer.html, scripts/xyz_renderer.js.
- Ordered 13-frame replay: scripts/run_v8_mapper_only_replay.py; T3 replay: scripts/run_t1_1_t3_assumed_b.py.

Các Python environments cố ý tách biệt, gồm Habitat, P2/perception, feature/OpenCLIP, ConceptGraphs CPU và OccuSG/native worker. Đường dẫn environment/resource được ghi trong configs/launcher/local_environment.json và locks/; không gộp dependencies hoặc nạp mapper/model mới mỗi frame.

## 5. Các mức chạy — không được đánh đồng

| Chế độ | Nghĩa | Bằng chứng hiện có |
|---|---|---|
| Completed-cache playback / C1 | Đọc artifacts đã có và phát timeline/viewer | C1 29-frame HM3D 00802; B graph/viewer parity |
| Fresh batch route / C2 | Capture route rồi chạy batch perception/mapping | ReplicaCAD apt_0–apt_5; HM3D 00802 route main |
| Live processing từ cache / D1 | Chạy producer trên cache theo frame và phát revision trước khi run kết thúc | ReplicaCAD apt_1–apt_5, HM3D00802 shared workflow, T2-D cached integration |
| Camera-live simulation / T4 | Habitat tạo frame mới; keyframe được perception và mapper tiêu thụ khi simulator vẫn chạy | T4 v5 trên một route HM3D00802; graph/camera overlap đạt trong run |
| Wall-clock realtime | Đáp ứng target latency/rate đo bằng clock phù hợp ở tải và phạm vi xác định | **Chưa nghiệm thu / chưa cam kết** |

File-backed keyframe IPC vẫn là live khi frame được tạo và tiêu thụ trước khi capture kết thúc. Cần phân biệt simulation time, host monotonic time, wall-clock Unix time và browser performance clock.

## 6. Roadmap và kết quả đã đạt

### 6.1. Scene graph A–E

| Mốc | Kết quả chính xác |
|---|---|
| A — causal correctness | Frame/prefix/lifecycle, room lifecycle integration và frame-atomic RB1 được kiểm tra. **Spatial scope gate chưa đạt**; vì vậy verified object–room membership/room semantics vẫn pending. |
| B — unified graph/viewer | Object snapshots, room revisions/lifecycle và trạng thái membership/semantic cùng graph timeline; unavailable được hiển thị đúng. Fixture verified-scope kiểm tra transfer/retract/lifecycle, không phải dữ liệu quality thật. |
| C1 — cached workflow | Đạt trên cache 29 frame đã khóa; input preflight, builder, viewer builder, manifest/hash và launcher results/open đã kiểm chứng. |
| C2 — fresh scene/route | Đạt trên các ReplicaCAD apt_0–apt_5 đã cấu hình; HM3D 00802 cũng đã chạy trong shared workflow. Đây là routes phát triển cụ thể, không phải mọi local map. |
| D1/D2 — cache processing + XYZ viewer | Đồng thời đạt trên ReplicaCAD apt_1–apt_5 và HM3D00802 shared workflow. apt_0 thiếu bằng chứng XYZ mở trong lúc producer chạy. |
| E — portability | Đạt có giới hạn trên apt_1–apt_5 theo routes đã khóa; không phải nghiệm thu 91 scene, mọi HM3D, MP3D hoặc quality holdout. |

### 6.2. Smooth route / session / room belief

| Checkpoint | Đã làm | Ranh giới còn lại |
|---|---|---|
| T0/T1 | Route executor, camera-only preview, bounded keyframe candidate/log; route T1.1 có 113 camera frames và 13 keyframes persisted. | T1.1 là route dev đã chạy, không phải route tối ưu hay collision proof cho toàn scene. |
| T1.1 | Camera di chuyển/quay/đi tiếp được kiểm tra trong scope route. | Không gọi toàn bộ motion/obstacle safety đã hoàn tất. |
| T2-A | ConceptGraphs open/update/finalize/close; batch dùng cùng session core; partition/geometry/lineage parity với pre-refactor reference. | Cached session parity, chưa tự chứng minh live capture. Class IDs là prefix registry; resolve class_id trong artifact/run tương ứng. |
| T2-B | P2 session smoke thật; sau đó P3 + OpenCLIP image/text packet-boundary smoke trên subset 3 frame; P2 reuse được xác minh; packets đủ cho ConceptGraphs và OccuSG trong integration subset. | Không phải end-to-end validation mọi frame/scene; full upstream perception vẫn khác. |
| T2-C | OccuSG session giữ builder/native workers sống, per-frame update, prefix room revisions và terminal flush riêng; parity trên cache đã khóa. | Không có scope/membership proof; native worker/session chỉ được xác minh trong input/cache contract hiện hành. |
| T2-D | Một cached-input integration run từ state rỗng: 3 packets, mỗi mapper một session, 3 atomic unified revisions, viewer trước producer, seek; terminal riêng. | Cached packets, không phải future frame chưa capture. |
| v8 13-frame replay | Perception cho 11 frames thiếu; 13 packets theo thứ tự. ConceptGraphs 1 session/13 update; OccuSG 1 session/13 update; 13 prefix revisions và terminal riêng. | Replay persisted keyframes, không phải camera-live; scope unavailable trong A. |
| T3 A/B | A verified-only đối chứng; B replay dưới declaration experimental của đúng T1.1 all-13 run; bốn A variants độc lập; viewer có provenance. | B assumed XY-compatible, không xác nhận cùng tầng hoặc membership đúng; không áp dụng cho T4 v5. |
| T3 viewer | Người dùng trực tiếp xác nhận GUI v4: A/B và variant selection; frame 11/12/28; seek lùi; chair category cap; labeled-state count khác commit/relabel transitions. | User-verified viewer interaction, không phải browser-agent verification hoặc room-label accuracy. |
| T4 | Một camera-live run v5 hoàn tất và có backend overlap + browser render trong capture trên HM3D 00802. | Viewer chưa trình bày riêng latest camera frame, graph-as-of frame và lag; wall-clock realtime và semantics quality pending. |

Các checkpoint S1–S3 từng được hoãn như nhánh nâng cấp live simulation. Sau đó T2-A/B/C/D đã được người dùng duyệt và triển khai theo smooth-route plan. Không mở lại refactor S1–S3 nếu không có mục tiêu và gate riêng.

## 7. Local maps, routes và workflow

Registry: configs/launcher/scenes.json; môi trường/assets ngoài workspace được trỏ từ configs/launcher/local_environment.json và locks/external_resources.json. Menu và CLI dùng resolver chung. Acceptance phải lấy từ run manifest, không suy ra từ config hay việc assets tồn tại.

### 7.1. ReplicaCAD

- 91 scene instances local: apt_0–apt_5, 84 staging scenes (4 nhóm × 21), và empty_stage.
- apt_0–apt_5 có C2 runs theo routes 12 fixed poses ở 160×120 trong các checkpoint đã lưu.
- D1+D2 simultaneous GUI acceptance: apt_1–apt_5 đạt theo inventory/run receipts.
- apt_0 có C2/D1 nhưng thiếu nghiệm thu XYZ D2 trong lúc producer chạy.
- 84 staging scenes chưa có route/config nghiệm thu. empty_stage có route mismatch cũ và chưa được chạy.
- Không gọi đây là toàn bộ ReplicaCAD được hỗ trợ; chỉ apartments đã cấu hình được kiểm chứng.

Chi tiết 91 dòng, hashes, routes và receipts: docs/REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md và artifacts/replica_cad_local_inventory/inventory.json.

### 7.2. HM3D minival v0.2

- 10 scenes local: 00800–00809.
- Có route/cache cũ cho 00800, 00802, 00808; bảy scene 00801, 00803–00807, 00809 chưa có route/cache theo inventory hiện có.
- Shared C2→D1/D2 workflow đã nghiệm thu trên 00802/main (29 frame, ban đầu 160×120); C1 cache replay cũng đã nghiệm thu.
- Baseline quality mới hơn: HM3D 00802/main, 29 frames, native capture 640×480.
- T4 camera-live v5 chạy HM3D 00802 trên sequence/route riêng. C2 main29 chỉ cấp config/resource source; không lấy main29 RGB-D/poses/observations/room snapshots làm runtime evidence cho route T4.
- 00800 và 00808 có development caches/route artifacts nhưng chưa có cùng shared-workflow acceptance như 00802.

### 7.3. MP3D

Có reference/config nghiên cứu trong workspace, nhưng catalog shared workflow hiện chỉ đăng ký ReplicaCAD và HM3D. Không có MP3D scene/route workflow C2→D1/D2 hoặc T4 được nghiệm thu. Không suy adapter từng được thử thành hỗ trợ run mới.

### 7.4. Capture profile

Người dùng đã chọn capture native **640×480** làm working profile cho quality runs trên ReplicaCAD và HM3D. native_640x480_v1 là default profile trong registry. HFOV/calibration giữ theo route; intrinsics tính qua calibration adapter. legacy_160x120_smoke_v1 chỉ chọn explicit. Config/manifest lịch sử 160×120 giữ nguyên. Resolution, intrinsics, sensor settings và preprocessing tham gia cache compatibility; không reuse masks/depth/features giữa 160×120 và 640×480.

## 8. Object quality: những gì có thể kết luận

### 8.1. Baseline 160×120 shared workflow

HM3D 00802/main, 29 frames, 160×120: 25 P2 detections → 17 online hypotheses → 17 sau denoise → 2 sau filter → 2 sau merge. Đây là pipeline output, không phải accuracy.

Blind review đúng 25 P2 observations:

- 20/25 identity fields dùng được, 12 identity tokens được xác nhận; class fields dùng được 20/25.
- Cross-frame association trên 183 reviewed pairs: 15 TP, 0 FP, 4 FN; precision 1.000, recall 0.789, F1 0.882. Đây là development slice nhỏ với annotation không đầy đủ.
- 9/20 identity-confirmed observations được giữ; trong 12 identity tokens, 1 được giữ trọn, 1 giữ một phần, 10 không còn observation nào trong final graph.
- Một identity armchair_1 bị fragmentation qua hai online hypotheses; chưa có false-merge hypothesis được annotation đủ chứng minh trong tập này.
- 15 hypotheses bị filter đều singleton vì min_detections=3. Chín singleton là identity một-detection/pure-mask trong population review; còn lại gồm mixed mask, fragmentation, uncertain/incomplete/unreviewed.
- Exact raw class match 2/20. Không áp taxonomy/synonym mapping; 18 string mismatch không tự đồng nghĩa 18 predictor errors.
- Đây không phải detector recall, room coverage hoặc quality estimate toàn scene.

Kế hoạch/report: docs/QUALITY_EVALUATION_PLAN_HM3D_00802.vi.md và artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1_scoring/.

### 8.2. Baseline quality 640×480

Run hm3d00802_main29_640_quality_v1_attempt2, scene 00802-wcojb4TFT35, sequence hm3d-v02-00802-main-route-01, 29 frames; có 75 P2 observations. Gallery/template/hash manifest đã tạo tại artifacts/human_review/hm3d00802_main29_640_p2_review_v1/. Trong artifacts được kiểm tra, package/template tồn tại nhưng chưa thấy response/score cho 75 observations. Vì vậy 640×480 review metrics còn pending; không gán annotation 160×120 theo observation ID.

Không chạy lại capture/perception/mapping chỉ để tạo review package hoặc thay annotations. Không coi 31/75 retained hay số hypotheses là quality.

## 9. Room scope, membership và Room Belief V1

### 9.1. Scope Bridge kết luận gì

OccuSG tạo room polygons bằng cách chiếu occupancy/free-space sang XY. M3 có thể thấy các free-height intervals nhưng M3/MapConverter collapse chúng thành vertical envelope; M4 nhận 2D grid và polygon room exports không giữ elevation scope theo room revision. min_free_height_m không phải floor selector. Data cũ thiếu đủ room-side elevation provenance để xác nhận object/room population cùng một level.

Kết luận chính xác là **spatial scope insufficient**, không phải “scene đã chứng minh nhiều tầng” và cũng không phải “scene đã chứng minh một tầng”. Direct-Z fields từ binary replay cũ không hợp lệ; interval-to-M3 join được kiểm chứng riêng; single-interval gate fail không chứng minh có tầng thứ hai. Không chạy lại Scope Bridge/native replay nếu không có nguồn evidence mới.

Object clouds có world XYZ, mét và transform lineage theo frame; điều đó không cấp floor/layer scope cho room polygons. P3 legacy coverage thiếu không phải lý do tự loại hybrid object nếu RGB-D lift contract hợp lệ, nhưng object cũng không tự thừa hưởng floor.

### 9.2. Room Belief V1 giữ nguyên

- Tám room-function categories: bedroom, living_room, dining_room, kitchen, bathroom, office, storage_room, circulation_space. Unknown/ambiguous/unavailable là trạng thái, không là class thứ chín.
- Bốn A variants: mean/sum × neutral/no-neutral. A tables frozen/experimental; viewer chọn tường minh; không chọn winner làm default hoặc tạo lại bảng trong runtime.
- Stable object consensus theo view groups; raw history giữ riêng; alias/revisit không tăng support lần hai; provisional/conflicted/unsupported không góp room evidence.
- Admission theo prefix lấy từ effective mapper config (baseline min_detections=3); không dùng final retained list để backfill prefix.
- Greedy voxel-support membership từ object geometry và room polygon cùng prefix/revision; positive support chọn winner, zero support unassigned, tie theo policy deterministic.
- Per-object ledger dùng để retract; category cap là **một contribution cho mỗi unique eligible category trong mỗi room**, không phải mỗi object/frame. Chuyển phòng/class/mất eligibility rút/add trong frame-atomic update.
- S_R(r) = log(prior(r)) + Σ_c 1[n_R(c)>0] × A(c,r). Belief là normalized score chưa calibration, không phải probability accuracy.
- Candidate/commit/abstain/relabel có hysteresis; không ép label. Room geometry node tồn tại khi label unknown.
- A verified-only là default: thiếu scope → not_scored/unavailable, không phải scored abstention.
- B assumed-single-level chỉ dùng khi có declaration run/scene/sequence/frame/hash cụ thể được duyệt; badge assumed/experimental, không trộn verified metrics. Declaration T1.1 all13 không áp T4 v5.

Plan/controller/config: docs/ROOM_BELIEF_V1_IMPLEMENTATION_PLAN.vi.md, src/standalone_sg/semantics/room_belief_v1.py, configs/room_semantics/.

### 9.3. T3 B run cụ thể

Run hm3d00802_t1_1_all13_mapper_only_20261019_v1 có 13 prefixes, mapper state ban đầu rỗng. B declaration hm3d00802_t1_1_all13_assumed_single_level_demo_v1 có evidence_status=assumed_not_verified. A verified-only giữ gate đóng.

Ở prefix cuối: 15 hypotheses, 6 admission-eligible, 4 stable+supported+admitted; theo giả định B có 14 objects assigned, 1 unassigned, không tie. Chỉ hai no-neutral variants commit living_room cho room:1; các variant khác/room khác abstain theo policy. Không có room-function GT nên không accuracy/F1. Đây là integration demonstration trên đúng sequence, không quality promotion.

Viewer v4 được người dùng trực tiếp xác nhận qua sáu thao tác: A/B isolation; chọn variant; xem frame 11/12/28; seek ngược; kiểm tra chair category cap; labeled-state count khác commit/relabel transitions. Record: artifacts/t3_room_belief_timeline_viewer_00802_v4/viewer_acceptance.json.

## 10. T4 pilot mới nhất — bằng chứng thực tế

### 10.1. Identity/config

- Run: **t4_camera_live_00802_geometry_candidate_20261020_v5**.
- Scene: 00802-wcojb4TFT35.
- Sequence: hm3d-v02-00802-t4-geometry-candidate-v1.
- Route runner: t4_hm3d00802_geometry_candidate_v1_live_20261020.
- Run manifest: artifacts/live_processing_runs/t4_camera_live_00802_geometry_candidate_20261020_v5/run_manifest.json.
- Producer manifest: cùng thư mục, camera_producer_manifest.json.
- Unified revisions, logs, keyframes, semantics, integrity list, D2 viewer nằm trong run root này.
- Source C2 hm3d00802_main29_640_quality_v1_attempt2 chỉ làm source/config/resources. Route v5 capture frame IDs mới; không lấy main29 RGB-D/poses/observations/room snapshot làm runtime input.
- Native 640×480, HFOV 90° theo route/calibration; dt yêu cầu 0.040 s; action increments 0.008 m / 1.2°; nominal 0.2 m/s / 30°/s; target camera 25 FPS, RTF1.0; latest-wins, pending capacity1; verified-only.
- Effective config SHA-256: 8fe632bddf6c67d0ca091059f8763411142a8299b2ae33ddcada55665668e8e7.
- C2 source manifest SHA-256: 48e62b2f4031061d77c69fd603269e78707174e600d5ad89f7dc5a0b0a6c4753.
- Route geometry config SHA-256: d4f482fc2e40f37fa29c5e11330a422f8360c5efda9a1e2585fdf7847ccf61da.
- Geometry policy v6 SHA-256: 0bdf39da7569b4b67c74acc7db5577261baacfd442096b518c4e0532c4f44955.
- Run claim boundary: camera-live, not realtime, semantics not-scored while verified scope unavailable.

### 10.2. Camera/route/queue

- Capture hoàn tất bình thường: 1,362 camera frames; 58.440 simulation seconds; capture monotonic span 58.515 s; route producer wall 72.828 s.
- Achieved 23.290 FPS so với target25; simulation RTF 1.00005. Observed simulator dt mean 42.939 ms, p50 40 ms, p95/max 48 ms. Wall interval mean 42.937 ms, p95 53.580 ms, max 149.269 ms. 1,360 pacing overruns, tổng pacing wait 15.286 s.
- Route 1,361 actions: 1,147 forward, 19 left, 195 right; 9.176 m planar traveled; tổng góc quay tuyệt đối 256.8°, net yaw −211.2°. Không có collision/block/clipped event trong route report.
- Hạn chế geometry: navmesh include_static_objects=false; không chứng minh va chạm với mọi static object instance. Clearance diagnostic 234/234 samples bị cap ở 0.0002 m; không phải measured minimum clearance. Geometry v6 dùng baked agent radius tương thích, extra margin0, numeric tolerance0.0001m và vertical navmesh tolerance0.05m. Route complete theo checks này nhưng không phải physical safety guarantee.

### 10.3. Causal processing và overlap

- Persistent workers: P2, P3, features, ConceptGraphs, OccuSG; 5/5 ready, 5/5 exit code0; không restart model/mapper mỗi frame.
- 31 keyframe candidates/queued; 26 superseded trước processing; 5 admitted/dispatched/processed; ConceptGraphs 5 updates; OccuSG 5 updates; 5 paired unified commits. Hai nhánh nhận cùng frame IDs/order/source hashes:
  - cam-000000 → revision1.
  - cam-000352 → revision2.
  - cam-000616 → revision3.
  - cam-000955 → revision4.
  - cam-001343 → revision5.
- Capture kết thúc monotonic 8095.461. Unified rev1 commit 8052.558, rev2 khoảng 8065.288, rev3 khoảng 8079.314: ba commit trước capture end. Rev4/5 commit trong queue drain sau camera stop.
- First perception marker 8037.285; first unified commit 8052.558, chênh 15.271 s cùng host-monotonic clock. Camera tiếp tục chạy trong khoảng này. Queue-drain không tính là capture overlap.
- Browser D2 mở trước start. Quan sát qua browser thật: live-follow hiển thị revision2/frame352 rồi revision3/frame616 khi capture còn hoạt động. Đây là visual render-overlap evidence; browser performance.now() không so trực tiếp với host monotonic và không có persisted host-stamped render telemetry.
- **T4 gate:** camera-live capture + keyframe processing + hai persistent mappers + atomic graph/semantic revision + ít nhất hai graph revisions commit trước camera end + browser render trong capture đã được chứng minh trên run/route cụ thể này. Không gọi realtime.

### 10.4. Semantics, viewer và integrity

- Năm semantic overlay commits đúng graph/frame/hash tương ứng; tất cả not_scored, membership unavailable_elevation_scope, policy verified_only. Không có room_contains của ConceptGraphs object hoặc semantic room labels. Terminal semantics terminal_semantics_unavailable; không backfill terminal vào prefix.
- Viewer entrypoint: index_d2_camera_live.html; URL:
  
      http://127.0.0.1:8777/artifacts/live_processing_runs/t4_camera_live_00802_geometry_candidate_20261020_v5/viewer/index_d2_camera_live.html?live=1
  
- Viewer đồng revision cho XYZ clouds/trajectory, XY room geometry, evidence và semantic overlay. **Thiếu indicator độc lập hiển thị latest camera_frame_id, graph_as_of_frame_id và lag.** Do đó viewer render overlap đã quan sát, nhưng camera-vs-graph status/telemetry chưa hoàn chỉnh theo product contract.
- Hash inventory có 1,661 entries; 1,641 khớp và 20 sai khác thuộc mutable operational logs/journals/status pointers/bootstrap/live head/index. Không thấy immutable capture arrays, branch snapshots/geometry, joined revision snapshots/evidence bị sai hash. Manifest ban đầu không phân lớp mutable/immutable, nên whole-tree hash không thể gọi clean; giữ addendum/nhận định này.
- d2_xyz_manifest_camera_live.json là bootstrap manifest frame_count0; live index kết thúc 5 revisions; viewer đọc live index. Không sửa run v5 sau khi hoàn tất.

### 10.5. Overall T4 status

**Đạt trong run v5:** producer end-to-end; camera theo route; perception và hai mapper chạy khi camera còn hoạt động; ít nhất hai paired unified revisions commit trước camera end; browser thật render ít nhất hai revisions trong capture; per-revision semantics hash-bound và not_scored; final stages/terminal state riêng.

**Còn mở:** camera frame vs graph-as-of/lag UI rõ ràng; immutable-vs-mutable integrity manifest có phân loại máy đọc; physical static-object collision coverage; room scope/membership/labels; room quality; wall-clock realtime; scene portability ngoài runs đã đo.

Diễn đạt chuẩn: **T4 camera-live overlap được nghiệm thu trên một route development của HM3D 00802, ở run và cấu hình nêu trên.**

## 11. Lỗi cũ, thất bại và tránh lặp lại

Các lần fail trước giữ nguyên, không đổi thành pass:

- T4 pilot cũ trên T1.1: perception có chạy nhưng unified commits xuất hiện sau capture end; overlap chưa đạt ở pilot đó.
- T4 v6/v7: lỗi contract/startup trước khi có unified revision hoặc trước capture; v7 không phải lỗi timestamp OpenCLIP.
- Startup dry-runs sớm: có run lỗi trước route validator; closure mới nhất đi hết production startup path tới capture boundary với fake workers, 0 model loads/0 frame.
- Geometry contract/route schema mismatch từng khiến route runner fail trước capture; schema v2/policy v6 được đồng bộ trước pilot v5.
- T2 v8 initial replay fail readiness trước mapper update; giữ run fail. Mapper-only replay thành công sau đó có run ID mới.
- Viewer T3 v3 từng báo object snapshot/frame alignment fail ở một số revisions; viewer v4 được build và người dùng trực tiếp kiểm tra. Không sửa T3 source artifacts gốc để che run lỗi.
- T4 v5 có một lần sandbox-denied khi gọi start control; lần gọi tiếp theo thành công sau escalation. Chỉ một marker/start thực sự giải phóng coordinator và một real producer attempt; lần từ chối trước start không ghi thành capture.

Không retry mapping/pilot chỉ để sửa GUI. Nếu producer artifacts đạt mà UI lỗi, sửa viewer từ artifacts đó.

## 12. Các khoản còn nợ / giới hạn

### Chặn semantic claim

1. Room producer chưa xuất đủ per-room-revision vertical scope để xác định các mức độ cao có tương thích hay không.
2. Membership/room belief verified-only phải unavailable/not_scored.
3. B assumed demo có thể dùng sau declaration mới và approval riêng, nhưng không thay gate và không tạo verified evidence.
4. Chưa có room-function GT độc lập khớp polygons/revisions v5; không có room accuracy/F1.

### Chặn product completeness

1. D2 cần hiển thị camera_frame_id, graph_as_of_frame_id và lag, không làm đổi historical seek/follow.
2. Hash report cần phân lớp mutable operational files theo contract, không bỏ qua mismatch.
3. Nhiều scene có assets nhưng thiếu route; C2/D1/D2 acceptance chỉ áp dụng các scene/routes đã chạy.
4. Catalog hiện không có MP3D integrated adapter acceptance.
5. Camera rate v5 khoảng 23.29 FPS, target25; đây là đo route cụ thể, không phải bảo đảm.
6. OccuSG update + serialization là nhánh chậm trong một T1.1 latency analysis trước đó; không tự xem là bottleneck mọi run và không benchmark thêm khi chưa có giả thuyết mới.
7. Không có Git repository được phát hiện; source tracking dựa vào hashes/reports.

### Không phải blocker hiện tại

- Không cần chạy lại mọi scene để khởi đầu hướng mới.
- Không cần gộp environments Python.
- Không cần camera-rate model inference.
- Không cần Qwen online.
- Không cần xây viewer/engine mới để thấy graph.
- Không cần mở Scope Bridge nếu không có nguồn evidence mới.

## 13. Lịch sử nghiên cứu cần giữ, không trộn vào active path

Các nhánh sau nằm trong docs/LEGACY_INDEX.vi.md và artifacts tương ứng. Chúng là baseline/failure knowledge, không tự động là roadmap hiện tại:

- **U0–U2:** source/M6 association baseline, launcher và scoring nền.
- **U3:** causal-prefix và duplicate diagnostics; hoàn tất như chẩn đoán, không promote.
- **U4/U4-OV:** semantic/object fusion experiments; giữ model/vocabulary/prompt/threshold baseline, không promote theo HM3D00800 report.
- **U5:** lifecycle/resource plumbing và experimental integration; source association vẫn default tại thời điểm report, geometry-only experimental.
- **U6:** quality work trên HM3D00808 route ngắn; không phải generalization hoặc promotion.
- **K series:** geometry/association experiments, gồm K4 accumulated geometry; không thay ConceptGraphs backend hiện hành.
- **Q series:** offline quality-recovery runners/diagnostics; không là active matcher/object backend.
- Fixed-assignment, U4OV, duplicate-v2, M6/M8/M9/M10/M12 và Webots experiments: giữ theo từng report; không nhập tự động vào object identity do ConceptGraphs sở hữu.

Không xóa, move hoặc đổi tên baseline, annotations, reference trees, checkpoints, caches, failed runs hay manifests để “làm sạch” dự án. Dọn dẹp material là task riêng, có exact-path manifest và approval.

## 14. Lệnh và đường dẫn hữu ích

### Map picker và workflow chung

Menu terminal:

    ./run_scene.sh menu

Ví dụ HM3D 00802/main qua shared workflow:

    ./run_scene.sh scene-run --dataset hm3d_minival_v0.2 --scene 00802-wcojb4TFT35 --route main --port 8766

Các lệnh tra cứu/mở:

    ./run_scene.sh results <run_id>
    ./run_scene.sh open <run_id> live --port <port>

C1 cached replay:

    ./run_scene.sh cached-replay --config configs/cached_replay/hm3d_00802_m0_m1_a1_v1.json

Lệnh chỉ chạy sau preflight/RAM/assets gates; không mặc định invocation mới là đã chạy hoặc pass.

### Mở T4 v5 hiện có

Manifest ghi entrypoint và URL:

    http://127.0.0.1:8777/artifacts/live_processing_runs/t4_camera_live_00802_geometry_candidate_20261020_v5/viewer/index_d2_camera_live.html?live=1

Run root:

    artifacts/live_processing_runs/t4_camera_live_00802_geometry_candidate_20261020_v5/

Viewer/server thuộc run có thể còn hoạt động. Không kill server/process chỉ từ handoff; xác định ownership trước.

### Nguồn nhanh cho người bắt đầu hướng mới

- Roadmap gốc A–E: SCENE_GRAPH_PROJECT_REVIEW_AND_COMPLETION_PLAN.vi.md ở Documents/Codex/2026-09-22/c-c.
- Smooth route/T1–T5: SMOOTH_ROUTE_KEYFRAMES_ROOM_BELIEF_IMPLEMENTATION_PLAN.vi.md cùng thư mục.
- Integration map: docs/ACTIVE_ARCHITECTURE.vi.md — đối chiếu T4 v5 vì page này cũ hơn.
- Room contracts: docs/ROOM_BELIEF_V1_IMPLEMENTATION_PLAN.vi.md.
- 91 ReplicaCAD entries: docs/REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md.
- Shared HM3D pilot: docs/HM3D_00802_SHARED_WORKFLOW_PILOT_20261017.vi.md.
- T3 B outcomes: docs/T1_1_T3_ASSUMED_B_REPLAY_20261019.vi.md.
- 640 review status: docs/QUALITY_EVALUATION_PLAN_HM3D_00802.vi.md.

## 15. Hướng phát triển mới: lựa chọn, không mặc định mở tất cả

Nên chốt một mục tiêu có gate rõ trước khi viết thêm code. Các hướng hợp lý:

### Hướng A — semantic demo trên graph live đã có

Tái sử dụng 5 unified prefix commits T4 v5; không capture/perception/mapping lại. Giữ A verified-only làm control; chỉ nếu người dùng duyệt thì tạo declaration B assumed riêng cho scene/sequence/frame/source hashes v5, chạy T3/RB1 trên đúng 5 prefixes và hiển thị overlay trong viewer. Acceptance là causality, category cap, candidate/commit/abstain/retract và đúng badge/provenance; không phải verified scope hay accuracy.

Ưu điểm: nối T4 camera-live với A tables trong thử nghiệm nhỏ, tránh pipeline run. Giới hạn: chỉ 5 sparse keyframes và giả định XY-compatible; có thể không đủ evidence để commit label, vẫn là kết quả hợp lệ.

### Hướng B — chất lượng object trên 640×480

Hoàn tất user review đúng 75 P2 observations của hm3d00802_main29_640_quality_v1_attempt2; score retention/fragmentation/class/mask theo population đó. Không chạy model hay mapping. Chỉ sau khi có lỗi được xác nhận mới chọn một experiment object-side.

Ưu điểm: evidence object-side trước khi đổi pipeline. Giới hạn: development set một route, không room quality, không holdout.

### Hướng C — mở rộng map support

Chọn một scene/route còn pending qua registry và workflow chung, chỉ sau preflight; tiếp tục C2→D1/D2, không sửa mapper theo scene ID. Cần chọn scene/route và cap producer riêng. Không chạy toàn bộ 101 scenes hoặc chuyển MP3D như mặc định.

Ưu điểm: portability evidence. Giới hạn: không tự giải quyết membership/room labels hoặc quality.

### Khuyến nghị khởi đầu

Nếu mục tiêu mới vẫn là “thấy Room label cập nhật từ object evidence”, **Hướng A** là bước nhỏ nhất vì graph revisions T4 đã tồn tại. Cần quyết định rõ assumed scope B cho đúng v5; nếu không, A tiếp tục not_scored theo verified-only. Nếu ưu tiên đo object identity/class trước, chọn Hướng B. Không làm đồng thời A+B+C.

## 16. Prompt chuyển giao có thể sao chép

> Đọc PROJECT_HANDOFF_TO_NEW_DIRECTION_2026-10-21.vi.md trước khi dùng các plan/report cũ. Đối chiếu run manifest mới nhất và source hiện hành; không lặp trạng thái T4 pending từ tài liệu 19-10 nếu v5 là current. Mục tiêu mới tôi sẽ chốt riêng; trước khi code hãy nêu tiêu chí A–E/T checkpoint liên quan, input/run ID, acceptance, runtime và ngân sách. Giữ nguyên baseline/config/model/matcher/thresholds/A tables/verified-only trừ khi có quyết định riêng. Không chạy producer/model/native replay hoặc Scope Bridge ngoài phạm vi được duyệt. Phân biệt cached playback, batch, live processing, camera-live, realtime; phân biệt assumed và verified; giữ failed runs/artifacts nguyên trạng. Nếu source/status conflict, báo path/hash/evidence cụ thể thay vì suy từ tài liệu cũ.

## 17. Kết luận bàn giao

Hệ thống đã chứng minh đường quan trọng cho demo chuyển động: Habitat tạo camera frames mới; keyframes có kiểm soát đi qua persistent perception và hai mapper; unified graph được commit; browser hiển thị nhiều revisions trước khi camera dừng. T4 v5 là mốc kỹ thuật mới nhất và mạnh nhất cho mục tiêu này.

Khoảng cách chính còn lại không phải “chưa có pipeline”: đó là **room-side scope để membership có thể được xác minh**, **object-quality evidence trên 640×480**, và **product-level status/lag/integrity/portability**. Room labels chưa thể gọi verified. Camera/live overlap cũng không đồng nghĩa realtime.

Tài liệu này ghi trạng thái và đường quyết định để mở hướng mới; nó không tự duyệt pilot, assumed declaration, quality experiment hay dataset mới.

