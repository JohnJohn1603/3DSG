# Tích hợp tối thiểu: ConceptGraphs Object Core → Adapter → OccuSG Rooms

Ngày: 2026-10-12; trạng thái thực thi cập nhật 2026-10-14: **local hybrid mapping/integration đã kiểm chứng trên cache perception có sẵn; full ConceptGraphs perception và quality improvement vẫn pending; R3 Kaggle chưa chạy; chưa cleanup**.

Workspace đích: `/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/`.

## Phạm vi điều chỉnh — có ưu tiên khi đọc các mục bên dưới

Cập nhật 2026-10-12 theo yêu cầu người dùng: **chỉ lấy perception recipe đã chọn và các hàm lõi object mapping cần thiết từ ConceptGraphs; tái sử dụng OccuSG không ROS đã hoạt động**. Không tích hợp toàn bộ repository, không port lại OccuSG, không clone lại hai source, không bắt đầu lại R0/R2. “Object pipeline” trong plan nghĩa là đường gọi tối thiểu này, không phải mọi tính năng/dependency của upstream.

Kiến trúc đích giữ ranh giới: **Habitat RGB-D → ConceptGraphs objects + OccuSG rooms → adapter local → unified graph**. Perception recipe ConceptGraphs đầy đủ vẫn được thiết kế cho Kaggle; object mapping core đã chạy local CPU theo thí nghiệm hybrid dùng masks/embeddings/text features cache sẵn. Kết quả hybrid không chứng minh full perception/reference parity hoặc cải thiện chất lượng.

### Trạng thái thực thi hiện tại — 2026-10-14

- **Đã dùng được/đã kiểm chứng local:** launcher `hybrid-map` nhận cache, text-feature sidecar, room snapshot và seed; runner dùng environment CPU riêng, xuất ConceptGraphs mapping final export cùng diagnostic pre-filter hypotheses, adapter chỉ nhập final export vào OccuSG, và viewer chuyển đổi hai phạm vi. Pilot cache U6 00808 5 frame cùng seed `20261014` khớp membership, bounds và point geometry với checkpoint seeded đã khóa; 9 hypotheses trước filter, 7 bị filter (không merge vào survivors), 2 object final. Room geometry/adjacency/attributes và semantic scores không đổi.
- **Nguồn/provenance:** hybrid cached YOLO11n-seg + ViT-B-32 image/text features + ConceptGraphs reference mapping core. Đây không phải full ConceptGraphs-Detect, không phải detector comparison, và không xác nhận threshold/model compatibility hay chất lượng tổng quát.
- **Còn pending:** full ConceptGraphs perception recipe/runtime, độc lập chất lượng association/semantic/object-room, và mọi promotion/default change. Source/default và room semantic scoring giữ nguyên.
- **Lệnh đã chạy:** `./run_scene.sh hybrid-map --cache artifacts/runs/u6_hm3d_00808_quality_v1_20261010 --text-features artifacts/conceptgraphs_cpu_hybrid_00808/text_features_v1 --room-snapshot artifacts/runs/u5m9_20261010T031023.553758Z_0cda0d/replay/snapshot.json --seed 20261014 --run-id hybrid_u6_00808_seed20261014_v2`; mở bằng `./run_scene.sh open hybrid_u6_00808_seed20261014_v2 hybrid`.

### A. Lấy chính xác phần nào

| Thành phần | Quyết định |
|---|---|
| RAM → GroundingDINO → SAM → CLIP | Giữ recipe perception hiện đã chọn; không đổi model để làm nhẹ mà chưa duyệt |
| `slam/utils.py`: `filter_gobs`, `gobs_to_detection_list` và helpers được gọi | Tái sử dụng filtering/lifting masks + depth + pose thành observations |
| `slam/mapping.py`: `compute_spatial_similarities`, `compute_visual_similarities`, `aggregate_similarities`, `merge_detections_to_objects` | Tái sử dụng association reference; không xây matcher K4 thay thế |
| `slam/utils.py`: `process_pcd`, `get_bounding_box`, `merge_obj2_into_obj1` | Tái sử dụng xử lý cloud, geometry/feature fusion và bbox update |
| `slam/slam_classes.py`: detection/object containers cần dùng | Giữ schema phục vụ core và export |
| Denoise/filter/merge | Chỉ chức năng và lịch chạy effective recipe bật, kể cả final postprocessing; không bỏ silent để giảm dependencies |
| Dataset input | Habitat capture adapter đã có và helpers cần thiết, không triển khai mọi dataset loader |
| Batch runner upstream | Oracle/call-flow tham chiếu và smoke khi khả thi; không bắt toàn bộ script thành production runtime |
| OccuSG không ROS | Giữ occupancy, rooms/regions, geometry, adjacency, contracts và native bindings đã chạy |
| Adapter mới | Chuyển tọa độ, export/import, graph IDs/lifecycle, membership; không association lần hai |

Tên hàm được đối chiếu source đã pin. Khi triển khai cần map dependencies theo đường gọi thật, không giả định copy riêng vài hàm là chạy độc lập.

### B. Không đưa vào active runtime

- LLM/caption/relationship generation, chat/query demos và API trả phí.
- Navigation demos, viewers upstream không dùng, training và benchmark suites ngoài nhiệm vụ.
- Dataset downloaders hoặc loader implementations không cần cho Habitat capture.
- SLAM/pose estimation khác: dùng poses Habitat đã có. Tuy nhiên thư viện như GradSLAM có thể cung cấp helper cần cho core; không loại chỉ vì tên thư viện chứa SLAM.
- Legacy K4/Q2/Q3/dedup/U4-OV không trở thành dependency object backend mới. Giữ baseline/history, không xóa theo plan edit này.

### C. Cách tích hợp tối thiểu, không fork thuật toán

1. Ưu tiên import/call reference qua wrapper nhỏ, giữ reference repos read-only.
2. Nếu eager imports kéo tooling không dùng, tách lazy imports hoặc extraction tối thiểu có upstream path/commit/hash và patch diff. Không đổi gate, score, update order, numerical policy trong cùng patch thu gọn.
3. Dependency manifest chia `perception-required`, `core-required`, `reference-smoke-only`, `unused`. Với GradSLAM/chamferdist/CUDA extensions đang pending, truy call path trước khi quyết định cài hoặc loại; không mặc định lấy toàn bộ requirements.
4. Kiểm tra output intermediates/final với reference trên cùng cached observations/config: matches, membership, clouds/bounds/features và tolerance. Không cần chạy detector hai lần chỉ để kiểm tra mapping wrapper. Parity pending phải ghi rõ nếu Kaggle/model chưa sẵn sàng.
5. Core nhận features đã cache; không reload detector/encoder nếu đường mapping thật sự không dùng. Loại load thừa phải được kiểm chứng không đổi outputs.
6. Chỉ tách model stages/placement khi giữ preprocessing, frame order, accumulated vocabulary và recipe semantics. Hai T4 không tự thành GPU 32 GB; module tối thiểu không bảo đảm toàn bộ perception sẽ nhẹ hoặc realtime.

### D. Boundary và reuse code đã có

- Perception stage: RGB → masks/boxes/raw labels/features, provenance rõ.
- Object core wrapper: observations + depth/K/pose → xử lý frame, snapshot/export và final postprocess. Đây là API boundary cần thiết, không tuyên bố reference đã có sẵn đúng API này.
- Local graph adapter: object export + room snapshot → unified graph. OccuSG không cần import Torch/SAM/CLIP; CG core không cần import OccuSG.
- Tiếp tục dùng `integration_v2/capture_package.py`, `conceptgraphs_adapter.py`, `object_export_contract.py`, notebook và bundle R2/R3 đã làm. Không tạo bản trùng để khớp sơ đồ thư mục.
- Room backend đã hoạt động: chỉ bổ sung room-only API boundary nếu cần; không port từ ROS lại, rebuild native hoặc đổi room algorithm khi chưa có blocker cụ thể. Có thể reuse room snapshot cùng capture/config hợp lệ để test adapter.
- Sơ đồ thư mục phía dưới là phân vai, không bắt tạo mọi thư mục/framework/registry. Ưu tiên ít wrapper và một workflow chính.

### E. Nhiệm vụ tiếp theo hữu hạn

1. Không làm lại R0/R1 inventory hay R2 package. Đọc handoff và code đang có; giữ nguyên artifacts/version cũ.
2. Lập bảng ngắn component→reference function→dependencies→keep/exclude; chốt pins còn thiếu cho đường thực sự dùng.
3. Sửa wrapper/notebook tối thiểu theo phạm vi trên. Không đóng gói modules/datasets không dùng; không cài/tải/upload khi chưa được duyệt.
4. Chạy CPU adapter/package/contract tests hiện có. Chuẩn bị smoke/reference-parity tests cho Kaggle; đánh dấu pending nếu chưa chạy thực tế.
5. Bàn giao diff và notebook/bundle version mới nếu thay đổi, commands đã verify, dependencies cần thiết và thao tác user phải làm. Không gọi parse/compile thành GPU runtime pass.
6. Khi quyền dữ liệu và GPU/model setup được duyệt: smoke một frame → fresh-map bảy frames → export → adapter local với OccuSG rooms. Chưa tối ưu model hoặc realtime trong vòng này.

**Đây là điều chỉnh kế hoạch, chưa sửa runtime/notebook/bundle. Các execution status cuối tài liệu là lịch sử thực thi được giữ nguyên, không phải nghiệm thu phạm vi tối thiểu mới.**

## 1. Quyết định kiến trúc và mục tiêu

Đường phát triển chính mới dùng **perception đã chọn và minimal object core của ConceptGraphs reference**, kết hợp **room backend OccuSG không ROS đã có**. Minimal nghĩa là bỏ module/tooling không sử dụng, không tự thay thuật toán lõi. Không tiếp tục phát triển K4/Q2/Q3 hoặc U4-OV tự xây như object backend chính. Không viết matcher khác rồi gọi là ConceptGraphs.

Mục tiêu: cải thiện object identity, object geometry, semantic evidence, object–room membership và room labels bằng kết quả đối chứng. Không hứa “chính xác nhất” trước khi đo, không đánh đồng nhiều objects/points hơn với accuracy.

Plan này thay thứ tự công việc chủ động U/K/Q cũ; kết quả cũ trở thành baseline và lịch sử. Nếu task cũ còn chạy, yêu cầu checkpoint/bàn giao trước khi di chuyển file nó sử dụng.

```text
                  Habitat RGB-D + intrinsics + pose
                         │                 │
                         ▼                 ▼
          ConceptGraphs object pipeline   OccuSG không ROS
          tagging → boxes → masks         occupancy → rooms
          → features → lifting            → geometry/adjacency
          → association → fusion                  │
                         │                        │
                         └──────────┬─────────────┘
                                    ▼
                         Adapter object–room graph
                                    ▼
                 ObjectNodes + RoomNodes + membership edges
                                    ▼
                      Semantic consumer và evaluator
```

| Quyết định/dữ liệu | Chủ sở hữu |
|---|---|
| Frames, calibration, camera pose, timestamps | Habitat capture adapter |
| Object masks, points, identity, fusion, filtering/merge | ConceptGraphs backend |
| Occupancy, room boundaries, room IDs, adjacency | OccuSG room backend |
| Coordinate conversion, graph IDs, lifecycle, membership | Integration adapter |
| Functional room labels | Consumer semantic riêng |
| GT/annotations/scoring | Evaluator độc lập |

**Không association lần hai:** M6 source/geometry/K4 không được gán lại identity cho object đã do ConceptGraphs quản lý. Không dùng legacy P3 largest-cluster output làm input CG rồi gọi đó là pipeline reference. Hai backend chia sẻ capture, không cần chia sẻ object perception.

## 2. Chốt reference recipe, không chỉ “dùng detector ConceptGraphs”

Reference đang có: `ref_ConceptGraph/concept-graphs`, commit `93277a02bd89171f8121e84203121cf7af9ebb5d`.

README reference có hai đường: SAM segment-all và ConceptGraphs-Detect. Đường chính mới chọn **ConceptGraphs-Detect** vì yêu cầu dùng cả detector giống reference.

Recipe cần xác minh/khóa theo README và code cùng commit:

**RAM → GroundingDINO → SAM ViT-H → OpenCLIP ViT-H-14/laion2b_s32b_b79k → reference object mapping.**

Không âm thầm thay bằng YOLO11n, YOLO-World, MobileSAM hoặc ViT-B-32. Nếu tài nguyên buộc phải dùng variant nhẹ, trình duyệt và đặt tên riêng; không gọi variant đó là cùng reference recipe.

Đọc các entry points:

- `conceptgraph/scripts/generate_gsa_results.py`
- `conceptgraph/slam/cfslam_pipeline_batch.py`
- `conceptgraph/slam/mapping.py`
- `conceptgraph/slam/utils.py`
- Dataset loaders, README và dependency instructions tại cùng commit.

Không dùng nguyên `base.yaml`: có path máy tác giả và giá trị cần README overrides. Resolve effective config gồm stride, mask filters, spatial/visual similarity, match method, voxel/DBSCAN, periodic/final merge và filtering; kiểm tra code thực sự hỗ trợ options.

Vocabulary sinh từ RAM/predictor theo recipe. Không dùng class list lấy từ semantic GT của scene. Kiểm tra đặc biệt `class_set=scene`, phrase/token handling, random-label fallback và seeds. GT-based vocabulary chỉ được dùng trong oracle diagnostic riêng, không trong kết quả chính.

“Reference-compatible” nghĩa là implementation/model/preprocessing/config đã khai báo tương ứng; không hứa bitwise parity giữa phần cứng khác nhau hay tự nhận tái lập paper benchmark trên Habitat/HM3D.

## 3. Reset và dọn dẹp có thể phục hồi

Reset ở đây là **reset đường phát triển, trạng thái active và entrypoints**, không xóa trắng workspace. Không dùng `git reset --hard`, `git clean -fdx` hoặc xóa đệ quy workspace. Kiểm tra root có Git hay không; hiện không được giả định có commit để phục hồi.

### Phần phải giữ

- Baseline ngoài workspace `Rebuild_3DScene_OccuSG`: chỉ đọc, không sửa/xóa.
- Hai reference repos, pinned commits, local changes nếu có.
- Habitat datasets/captures, intrinsics/poses, checkpoints/models/envs/native binaries được tái sử dụng.
- Resource locks, annotation versions, seed provenance và evaluator contracts.
- Room backend không ROS, capture/route utilities, coordinate utilities, evaluator dùng được.
- Baseline run và lệnh tái lập tối thiểu để so sánh.

### Legacy đóng băng

K4/Q2/Q3, U4/U4-OV, experimental dedup/matcher/fixed-assignment fusion và runners liên quan không còn là dependency mặc định của đường mới. Chúng được giữ làm baseline/fixtures, không nhất thiết phải di chuyển ngay.

Nhiều scripts/configs chứa path cố định: di chuyển hàng loạt trước khi có dependency map sẽ phá tái lập. Ưu tiên tách đường chạy trước, dọn vật lý sau.

### Trình tự cleanup

1. Kiểm tra tasks/processes đang dùng files. Inventory có vai trò `active / reference / baseline / legacy / disposable` và kích thước.
2. Checkpoint code/config/docs/locks, manifest hashes và hướng dẫn khôi phục. Dữ liệu lớn được giữ tại chỗ; manifest không được gọi là backup dữ liệu.
3. Archive docs cũ không còn active vào `docs/legacy/<checkpoint-id>/` khi dependencies/links đã kiểm tra. Lập old→new path index; giữ redirect/notice nếu cần. Không di chuyển artifacts lớn hàng loạt.
4. README mới chỉ trỏ active architecture/quickstart/status; commands planned khác implemented. Không xóa lịch sử failure để làm sạch hình thức.
5. Lập deletion manifest cho target paths chính xác, lý do, dung lượng và cách phục hồi. Chỉ xét cache tái tạo/output tạm/doc trùng đã có bản lưu.
6. Trình danh sách xóa material cụ thể để user chốt trước đợt không thể phục hồi. Ưu tiên archive/trash. Không follow symlinks để xóa shared resources ngoài workspace.

Quyền dọn docs không đồng nghĩa phải xóa mọi docs. Mục tiêu là đường phát triển dễ hiểu, không phải số file ít nhất.

## 4. Cấu trúc đích

Đây là cấu trúc vai trò đề xuất, không phải yêu cầu tạo đủ directories hoặc di chuyển code hiện có. R2 đã triển khai một số chức năng dưới dạng file trực tiếp trong `integration_v2/`; tái sử dụng chúng, không tạo bản trùng. Commands mới vẫn cần kiểm chứng:

```text
Fusion_Concept_OccuSG/
├── run_scene.sh                    # giữ default cũ đến khi nghiệm thu
├── integration_v2/
│   ├── cli.py
│   ├── capture_adapter/
│   ├── conceptgraph_backend/
│   ├── occusg_room_backend/
│   ├── graph_adapter/
│   ├── semantics/
│   ├── evaluation/
│   ├── configs/
│   └── tests/
├── ref_ConceptGraph/               # read-only
├── ref_OccuSG/                     # read-only
├── locks/integration_v2/
├── docs/
│   ├── CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md
│   ├── ACTIVE_ARCHITECTURE.vi.md
│   ├── QUICKSTART_V2.vi.md
│   ├── STATUS_V2.vi.md
│   └── legacy/
└── artifacts/integration_v2/
    ├── runs/
    ├── evaluations/
    └── checkpoints/
```

Không copy toàn bộ `src/` hoặc clone lại repos. Tái sử dụng room modules qua API boundary; không kéo legacy object backend vào đường mới qua imports. Wrapper/import reference được ưu tiên; nếu phải vendoring hoặc compatibility patch, lưu upstream path/commit/hash và diff, không sửa reference silent.

## 5. Các giai đoạn

| Stage | Sản phẩm | Gate |
|---|---|---|
| R0 | Checkpoint, inventory, legacy freeze, active docs | Không mất resources và baseline |
| R1 | Reference recipe, minimal call/dependency map, hardware/model manifest | Khả thi; chỉ dependencies cần thiết; tải/cài được duyệt |
| R2 | Habitat→CG dataset adapter | Calibration/axes/depth/pose đúng |
| R3 | Perception + minimal CG object core chạy độc lập | Tương thích reference, không legacy object substitution |
| R4 | CG objects→OccuSG room graph adapter | Lifecycle/membership/invariants/replay đúng |
| R5 | Semantic consumer và paired quality evaluation | Bằng chứng đúng phạm vi, known failures rõ |
| R6 | Launcher đơn giản và chuyển default | User nghiệm thu, rollback, cleanup cuối |

Mỗi stage phải có code chạy được, tests và báo cáo ngắn. Không cần identity hoàn hảo mới cho làm adapter. Nhưng integration pass không tự đủ điều kiện promote quality/default.

## 6. R0 — checkpoint và boundary

- Lưu trạng thái task Q4 nếu còn chạy; không giẫm lên user changes.
- Xác minh resource bindings, captures và room binaries còn dùng được.
- Map chức năng giữ lại→file/module cụ thể, không audit mọi artifact cũ.
- Tạo checkpoint phục hồi được. Nếu dùng Git, tránh commit datasets/models/envs và giữ nested reference repos; không bắt buộc init Git nếu chưa thống nhất.
- Freeze legacy algorithm development, giữ old commands có cảnh báo. Chưa đổi launcher default sang hệ thống chưa chạy.
- Lập cleanup proposal nhưng không biến việc dọn toàn bộ docs thành một dự án kéo dài riêng.

## 7. R1 — hardware, dependencies và model

Kiểm tra GPU/VRAM/driver/CUDA, RAM/disk/interpreters. Không đoán máy đủ CUDA. Pin dependencies/submodules, checkpoint names/revisions/SHA, nguồn và licenses. Kiểm tra option SAM/parser với code thực tế thay vì chỉ tin tên trong README.

Tái sử dụng env/weights chỉ khi đúng model và preprocessing contract. Env ViT-B-32 hiện có không mặc định phù hợp ViT-H-14. Giữ môi trường Habitat/perception/room cũ bất biến; env CG riêng và giao tiếp qua artifacts/subprocess khi cần.

Trình recipe chính, tài nguyên thiếu, download size, setup/pilot budget, peak memory dự kiến và mức bất định. Xin duyệt trước tải/cài/remote compute có phí. Không đủ tài nguyên thì hỏi chọn remote GPU hoặc variant nhẹ đã khai báo, không âm thầm đổi model.

Không chạy LLM/API generation có phí chỉ vì reference có chức năng đó. Object–object semantic relationship generation ngoài phạm vi pilot ban đầu; graph trước tiên có objects, rooms, membership và adjacency.

## 8. R2 — adapter RGB-D/pose

Dùng capture cache phù hợp hiện có, chưa cần capture mới. Loader cung cấp RGB, axial depth mét, K, camera-to-world, IDs/timestamps theo convention CG dataset API.

- Kiểm tra optical/Habitat/canonical axes, `T_W_C` và inverse, relative/absolute poses, normalization pose đầu của loader.
- Công bố transform CG-world↔OccuSG-world. Không sửa pose để làm outputs trông khớp.
- Giữ RGB/depth align, resolution và pixel-center convention. Nếu resize/crop thì cập nhật K tương ứng.
- Test project/backproject round-trip, known world points, depth scale và reprojection qua nhiều poses.
- Hai backend dùng frame selection thống nhất; nếu stride khác phải ghi frame mapping và phạm vi so sánh.
- Semantic GT images/category annotations không vào predictor loader.

## 9. R3 — perception và minimal ConceptGraphs object core chạy thật

Chọn một cached sequence ngắn có multiview trong development data hiện có. Khóa frame list và effective recipe trước run; chưa chạy dense 280 frame.

Chạy đường reference perception→mapping đã chọn: RAM, GroundingDINO, SAM, CLIP, lifting/point processing, spatial+visual association, geometry/feature fusion và postprocessing đã cấu hình. Không kéo toàn repository thành runtime: LLM, demos, viewers, training và các dataset loaders không dùng nằm ngoài phạm vi. Batch script reference dùng để đối chiếu; wrapper production chỉ gọi core cần thiết.

Không dùng YOLO P2/P3 cũ để thay stages. Không thêm K4 guards, dedup-v2 hoặc U4-OV gates vào wrapper. Compatibility fixes tối thiểu phải có diff/tests và status `reference-compatible` hoặc `adapted`.

Lưu native outputs CG rồi export schema chuẩn. Kiểm tra wrapper/instrumentation không đổi partition/cloud/features so với native path cùng inputs/seed; báo non-determinism đúng mức, không hứa bitwise parity giữa hardware khác nhau.

### Artifacts

- Per-frame detections, masks, raw labels/confidence semantics, features và IDs.
- Native snapshots/clouds/bboxes/features, observation membership theo revision khi hỗ trợ.
- Input/model/config/code hashes, exact commands/logs/exit codes.
- Viewer nhẹ: RGB/masks, object timeline/cloud. Tách majority detector label, semantic query label và identity.
- Runtime/RAM/VRAM từng stage; cold/warm/cache reuse riêng.

Nếu calibration/schema sai thì sửa trước. Nếu kỹ thuật đúng nhưng chất lượng chưa cao, lưu kết quả và sang integration/evaluation có giới hạn; không âm thầm tuning đến khi ảnh đẹp.

## 10. R4 — room-only backend và unified graph

### OccuSG rooms

Dùng occupancy/room segmentation/binaries từ bản không ROS hiện có. Nếu builder bắt buộc M6, tách room-only path; không dùng fake object outputs để che side effects. So room geometry/adjacency với baseline trên cùng RGB-D trong phạm vi deterministic đã xác minh.

### Object identity/lifecycle

CG list index không phải persistent graph ID: merge/filter có thể đổi index. Adapter cần lineage cho create/update/merge/filter, canonical aliases và revisions.

- IDs namespaced, coordinate frame và provenance tường minh.
- Raw labels theo observation bất biến; fused geometry/features và resolved label/status riêng.
- Merge cập nhật canonical node và incident edges, giữ aliases/history; không để hai active nodes cùng vật do alias.
- Filter loại node/edges khỏi active snapshot, giữ raw evidence và lý do.
- Replay idempotent: cùng observation không góp evidence hai lần.
- Nếu chỉ export final objects đáng tin, triển khai final-map adapter trước. Không bịa online identities/lifecycle; causal mode cần instrumentation thực sự có provenance.
- Snapshot graph reload không đồng nghĩa resume mutable CG state; capability được báo riêng.

### Object–room membership

Tái sử dụng membership code khi phù hợp; ghi rõ centroid hay surface support, tolerance và room revision. Vật ở boundary/doorway có thể ambiguous/unassigned thay vì ép owner. Không dùng room semantic label để ép object class/identity.

Tests bao phủ transform, duplicate ingestion, merge/filter reconciliation, stale edges, dangling/self edges, owner contract, room revision và serialization. Đường mới không gọi legacy M6 association.

## 11. R5 — semantics và evaluation

### Nhãn object và phòng

Xác định rõ reference cung cấp nhãn bằng detector vote, text/image similarity hay query. Feature fusion không tự đồng nghĩa sửa class. Consumer bổ sung để room rules đọc được phải ghi là adaptation, không lén đưa U4-OV prototype cũ vào dưới tên reference.

Taxonomy mapping có version: synonym khác parent–child, unsupported không ép thành class COCO gần nhất. Raw và resolved semantic fields riêng; unknown/conflicted không tự thành evidence confirmed. Không dùng GT hoặc room labels để quyết định object identity/class.

### So sánh với baseline

- Cùng RGB-D/pose/frame list cho source và CG-Detect, nhưng masks/points/features khác là đúng bản chất thí nghiệm.
- Reuse baseline đúng hashes/config; không native replay lại nếu không cần.
- Không so detection IDs giữa hai detectors. Match mỗi pipeline với GT/human identities riêng rồi công bố coverage.
- Báo end-to-end object coverage và association conditional-on-matched-observations; intersection chỉ là diagnostic, không được che misses.
- Annotation cũ theo observation/mask không tự áp dụng cho SAM mask mới cùng frame. Chỉ tái sử dụng khi có bằng chứng mapping phù hợp; otherwise review riêng.

| Mục tiêu | Chỉ số |
|---|---|
| 2D masks | Class-agnostic precision/recall/IoU; class-aware với mapping riêng |
| Identity | Pair P/R/F1, false merges, fragmented identities, excess tracks, unassigned và coverage |
| Geometry | Surface accuracy/completeness khi có GT; nếu không diagnostic |
| Object labels | Correct/wrong/abstain, correction/harmful change, unsupported và coverage |
| Membership | Object→room accuracy khi có independent annotation |
| Functional room labels | Correct/wrong/unknown/ambiguous trên GT phù hợp |
| Room boundaries | Polygon/region metrics riêng, không suy từ object F1 |
| Cost | Stage runtimes, RAM/VRAM, throughput và latency cùng phạm vi |

Tách causal khỏi final-map, nhất là khi reference có final merge/filter. Không chấm final labels như robot đã biết từ đầu. Development tuning được phép trong vòng hữu hạn đã khai báo, sau đó khóa và test mới; không dùng test labels chọn vocabulary/config.

Định nghĩa trước ưu tiên false merge/recall/harmful labels/runtime và mức trade-off user chấp nhận. Không yêu cầu hoàn hảo, không promote chỉ vì một case bed được sửa. Thiếu room GT thì ghi N/A; không tự xác nhận label để có accuracy.

## 12. R6 — launcher mỏng và default mới

Commands dưới đây là **giao diện đích chưa được xác minh**, không phải lệnh đang tồn tại:

```text
./run_scene.sh check <scene>
./run_scene.sh run <scene> --route <route>
./run_scene.sh replay <capture-run-id>
./run_scene.sh results <run-id>
./run_scene.sh evaluate <run-id>
./run_scene.sh open <run-id> objects
./run_scene.sh open <run-id> rooms
./run_scene.sh open <run-id> graph
./run_scene.sh compare <baseline-run-id> <candidate-run-id>
```

Trong migration dùng `integration_v2/cli.py` hoặc opt-in rõ; chỉ đổi default sau nghiệm thu. Có backup wrapper/rollback và legacy entrypoint, không đổi nghĩa commands âm thầm.

Default mới chỉ một recipe CG-Detect + OccuSG rooms. Ablations nằm trong experiment runner, không bắt user nhớ K4/dedup/U4-OV. Interactive `/help`, `/status`, `/results` giữ phong cách quen thuộc khi được triển khai.

Run output riêng, cache keys gồm inputs/model/config/code provenance. Đổi detector phải invalidate perception cache tương ứng. Results hiển thị backend thật, stage status, object/room counts, semantic status/eval coverage và timings. Failed stage không biến thành completed.

## 13. Realtime/navigation là gate riêng

Ban đầu là offline mapping từ posed RGB-D, không phải SLAM/navigation closed loop. Reference có thể nặng hơn YOLO CPU; đo GPU/VRAM và latency trước khi hứa realtime. Capture đặt pose Habitat không chứng minh robot di chuyển vật lý đồng thời dựng graph.

Sau quality/integration pilot mới xét keyframes, asynchronous perception, bounded queues và semantic cadence. Các tối ưu này là variant benchmark riêng, không âm thầm thay reference recipe.

## 14. Quyền, ngân sách và điểm dừng

- Lượt đầu chỉ R0 checkpoint/inventory và R1 read-only preflight/proposal. Chưa xóa material, tải/cài, capture hoặc chạy benchmark nặng.
- Sau duyệt tài nguyên, triển khai R2/R3 thật; không dừng ở tài liệu nếu không có blocker cụ thể.
- Reference pilot kỹ thuật hợp lệ thì làm R4 adapter, không đợi tất cả identity hoàn hảo. Giữ experimental status đến quality gate.
- Dự kiến batch vượt 30 phút, tải lớn hoặc có phí: xin duyệt tổng setup/code/tests/run. Không báo mỗi inference time mà bỏ setup.
- Không tự mở scene/model khác vì kết quả chưa đẹp. Báo kết quả âm và một đề xuất cụ thể, không vòng audit vô hạn.
- Cleanup cuối sau đường mới hoạt động, theo exact deletion manifest đã duyệt. Không xóa baseline/reference/data/shared resources.

## 15. Definition of done

1. Một workflow thật: Habitat→perception recipe + minimal CG object core→adapter→OccuSG room graph hiện có; không triển khai lại room backend.
2. Reference provenance/adaptations rõ; không legacy association lẫn trong backend mới.
3. IDs/lifecycle/membership nhất quán; tests/replay đạt; causal/final/resume capabilities ghi đúng.
4. Paired quality report với baseline theo đúng population, không chỉ integration tests pass.
5. Cost/runtime có số đo; không hứa accuracy tối ưu hoặc realtime thiếu bằng chứng.
6. Active docs gọn, một cấu hình chính; legacy có checkpoint và không cản workflow.
7. Xóa cuối có targets được duyệt và thông báo khả năng phục hồi.

**Đích đến là tích hợp hai backend có trách nhiệm rõ ràng, không phải tiếp tục tự tái tạo ConceptGraphs bằng một chuỗi matcher/guards thử nghiệm.**

## Execution status — R0/R1 preflight, 2026-10-12

- R0 checkpoint được tạo tại `artifacts/checkpoints/r0_20261012/`; workspace root không phải Git repository, nên phục hồi dùng archive + SHA-256, không dựa vào commit. Chỉ source/config/docs/locks/entrypoints được archive; datasets/models/envs/reference repos giữ nguyên vị trí.
- R0 inventory và cleanup proposal: `docs/REBUILD_R0_R1_PREFLIGHT.vi.md` và `docs/CLEANUP_MANIFEST_R0_R1.vi.md`. Không xóa/di chuyển material; README chỉ được thêm banner chỉ hướng kiến trúc mới và đánh dấu nội dung cũ là lịch sử.
- R1 recipe proposal ở `locks/integration_v2/conceptgraphs_detect_preflight_proposal.json`. Recipe đã đối chiếu README + code ở pinned ConceptGraphs commit. Không cài dependencies, không tải checkpoints, không chạy perception/capture/native replay.
- Historical R1 hardware finding: máy local không có NVIDIA CUDA GPU, RAM tổng khoảng 15.9 GB và khoảng 6.4 GB available; model checkpoints chưa có. Đây không còn là blocker cho R2 local CPU adapter. ConceptGraphs runtime được chuẩn bị ở Kaggle nhưng vẫn chờ quyền upload, dependency/source pin hoàn chỉnh, checkpoint setup và GPU run.
- U/K/Q files, results, annotations và gates giữ nguyên làm baseline/history; không có gate cũ nào được đánh dấu lại hoặc xóa.

## Execution status — R2/R3 preparation, 2026-10-12

- R2 generated a versioned seven-frame input package from the locked Q1 00808 capture. Package schema, source capture hash, frame order, relative filenames, calibration, pose conversion, depth validity and included-file hashes are recorded. The original capture was not modified; no semantic GT, annotation, legacy P2/P3 output or Habitat scene asset was included.
- R2 CPU tests cover pixel-center projection round-trip, pose conversion matching the pinned HM3D loader, first-camera-relative transform, frame order, package hash mismatch and output contract validation. The real `get_dataset` call is provided as a Kaggle preflight; this local interpreter has no Torch, so actual reference-loader execution remains pending.
- R3 notebook uses versioned scripts for probe, package/loader validation, one-frame smoke, seven-frame fresh-map run and artifact export. User-shared logs show an isolated micromamba setup was attempted and the pinned GradSLAM commit was fetched; GradSLAM installation stopped because the isolated Python was missing, and a later environment retry stopped at the free-disk guard. Environment compatibility/import checks, reference loader, checkpoint download, inference and pilot remain unverified.
- The pinned reference detector loads RAM, GroundingDINO, SAM and OpenCLIP on one selected device in the same process. The notebook does not split or pool two T4s. It checks one GPU's free VRAM and stops conservatively below the configured floor.
- Existing R1 proposal still lacks exact GradSLAM `conceptfusion` and chamferdist source commits. Resolve and record those before calling remote setup reproducible. No local dependency/model install was performed.
- Active architecture/status details and manual Kaggle flow: `docs/ACTIVE_ARCHITECTURE.vi.md`, `docs/R2_R3_KAGGLE_HANDOFF.vi.md`.

## Execution status — local object–room vertical slice, 2026-10-12

- Local integration is verified with a schema-aligned fixture and an existing OccuSG room snapshot from development run `u5m9_20261011T035542.851355Z_b39b87`. The fixture is explicitly tagged `source_kind=fixture`; it is not ConceptGraphs output.
- The local adapter validates the final-map object export contract, metre units, scene/sequence IDs, rigid transform and file hashes; transforms object clouds/bounds into the OccuSG world frame; preserves external object IDs and raw/fused label fields; and does not run M6 or any second association.
- Object-to-room membership is geometric. A footprint wholly inside one room is assigned; boundary/multiple-room evidence is ambiguous; no overlap is unassigned. Ambiguous/unassigned objects have no `room_contains` edge. The imported graph retains room geometry and room adjacency hashes from the source snapshot.
- Launcher path: `unified-graph`; viewer reuses the OccuSG spatial renderer and adds an object/membership table. Targeted adapter/contract/serialization/launcher tests passed (59 tests).
- **Full ConceptGraphs-Detect end-to-end remains pending:** a genuine export from the pinned ConceptGraphs mapping core now exists for the cached-perception hybrid pilot and was imported into the matching OccuSG room graph. The upstream detector/segmentation/CLIP perception recipe, its runtime parity, and broader quality remain unverified. Kaggle is paused; the hybrid result is not full-pipeline parity.
