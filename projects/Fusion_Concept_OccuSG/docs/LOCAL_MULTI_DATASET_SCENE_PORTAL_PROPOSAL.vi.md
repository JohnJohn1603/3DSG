# Đề xuất cổng scene/route dùng chung: ReplicaCAD + HM3D

Ngày kiểm kê ban đầu: 2026-10-17. Bản đầu là kiểm kê read-only/proposal;
trạng thái sau implementation và pilot attempt được ghi trong addendum ngay
dưới đây.

## Kết quả pilot HM3D 00802 (2026-10-17, cập nhật cuối)

Phần proposal/preflight dưới đây được giữ làm lịch sử. Implementation shared
registry/menu/HM3D adapter và pilot đã chạy. Workflow
`scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T063106Z_f91798` đạt C2
và D1+D2 simultaneous; C2 `c2_20261017T063106Z_99bc38`, D1
`d1_20261017T063106Z_554073fd`. D2 được xác nhận browser-ready trước khi
producer start và render revision 4/29 khi D1 còn chạy. Details về stage
counts, timing, hashes, GUI, hai attempt trước và giới hạn được ghi tại
[HM3D_00802_SHARED_WORKFLOW_PILOT_20261017.vi.md](HM3D_00802_SHARED_WORKFLOW_PILOT_20261017.vi.md).

Hai C2 full runs đã xảy ra trong lịch sử: lần thứ nhất hoàn tất C2 nhưng D1
bootstrap thất bại `EPERM` trước khi producers chạy; lần kế tiếp hoàn tất C2
và một D1 producer run đạt. Attempt trước đó chỉ dừng ở RAM preflight. Không
đánh dấu các failure cũ thành pass. C2 output hashes 767/767 và D1 immutable
hashes 795/795 khớp; hai nested hashes của `viewer.assets_sha256` cho
`live_head/index` phản ánh bootstrap bytes, còn final files được kiểm tra qua
immutable output ledger. Metadata discrepancy này vẫn được nêu rõ.

**Gate hiện tại:** registry/menu/adapter implementation đạt; HM3D 00802/main
C2 + simultaneous D1/D2 đạt theo workflow trên. Membership và room semantics
vẫn unavailable do spatial scope; chưa có quality/realtime/scene-portability
claim. Infrastructure v1 được đóng trong phạm vi đã nghiệm thu.

## Cập nhật sau implementation được duyệt (2026-10-17)

Phần proposal dưới đây là bản kế hoạch gốc. Checkpoint implementation đã
được thực hiện trong workspace:

- `configs/launcher/scenes.json` có một `catalog` dùng chung; resolver trong
  `integration_v2/scene_registry.py` phát hiện **91 ReplicaCAD** và
  **10 HM3D** scenes từ assets local, ghép routes theo cấu hình và lấy trạng
  thái acceptance chỉ từ run manifests hoàn tất. HM3D có routes/cache cho
  00800, 00802 và 00808; bảy scene còn lại hiện trong menu với lý do Run bị
  chặn vì thiếu route.
- `./run_scene.sh menu` là terminal picker thật; `scene-run` gọi cùng
  resolver/orchestration. Menu đã được mở thực tế và hiển thị inventory HM3D,
  các route của 00802 và các scene thiếu route. `apt_5/navmesh` và
  `00802-wcojb4TFT35/main` resolve thành effective config hợp lệ qua cùng
  API. Các lệnh legacy `fresh-replica`, `live-replay`, `results`, `open` vẫn
  còn trong launcher.
- HM3D C2 adapter dùng chung `fresh_replica.py`/capture workers; không thêm
  scene-specific mapper hoặc viewer. Scope membership và semantics tiếp tục
  `unavailable_elevation_scope` / `unavailable_membership_scope`.
- Targeted tests: **59 passed** sau khi thêm kiểm tra trạng thái failed-run
  manifests. Không có capture/perception/mapping nào được khởi chạy.
- Pilot HM3D 00802 `main` đã vào preflight một lần, run
  `scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T061655Z_e0390d` /
  C2 `c2_20261017T061655Z_79bb9b`. Gate RAM dừng trước producer:
  **2,050,600,960 bytes khả dụng, dưới ngưỡng đã khóa 3,221,225,472 bytes**.
  Capture, P2/P3, features, ConceptGraphs mapping, OccuSG replay, D1 và D2
  chưa chạy. Không hạ gate và không đánh dấu pilot là đạt. Manifests giữ lỗi
  và trạng thái stages đã được sửa để chỉ rõ preflight thất bại.
- Lệnh khi môi trường đáp ứng RAM gate vẫn là:
  `./run_scene.sh scene-run --dataset hm3d_minival_v0.2 --scene 00802-wcojb4TFT35 --route main --port 8766`.
  Launcher yêu cầu xác nhận sau preflight, tạo C2 một lần, khởi tạo D2 trước
  D1 và chờ xác nhận browser trước khi producer bắt đầu.

Tách thời gian: development được thực hiện trong trần 12 giờ đã duyệt; pilot
runtime chưa được đo. Lần preflight dừng sau khoảng 2.5 giây; không có thời
gian capture/perception/mapping hay D1/D2 để báo. Phần còn thiếu là chạy pilot
sau khi RAM khả dụng đạt gate, không phải thêm adapter hoặc sửa thuật toán.

## Kết quả cần hướng tới

```text
chọn dataset → chọn scene local → chọn route → preflight → chạy pipeline chung
                                                        → cùng unified graph viewer
```

Registry chọn adapter theo loại dataset. Adapter chỉ chuẩn hóa cách tìm và kiểm tra assets; route, RGB-D contract, C2 stages, D1 revisions và viewer dùng chung. Việc đổi scene hợp lệ chỉ cần config/route. Một scene chưa có route thì vẫn là `assets_present_route_missing`, không phải pipeline-ready.

## Hiện trạng được kiểm tra

### ReplicaCAD

Nguồn local read-only được khóa trong `configs/launcher/local_environment.json` và `locks/external_resources.json`. Có **91 scene instances**: `apt_0`–`apt_5`, 84 staging scenes (`v3_sc0`–`v3_sc3`, mỗi nhóm 21) và `empty_stage`. Inventory hiện có ghi cả 91 dòng và kiểm tra scene-instance JSON, navmesh tương ứng cùng asset references: [REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md](REPLICACAD_LOCAL_SCENE_INVENTORY.vi.md).

| Nhóm | Assets | Route | C2 / D1+D2 hiện có |
|---|---|---|---|
| `apt_0` | Scene instance, navmesh, asset references có | 5 route configs | C2 đạt; D1 đã chạy nhưng chưa có simultaneous D1+D2 XYZ acceptance |
| `apt_1`–`apt_5` | Có | `apt_1`/`apt_2`: 2 route configs mỗi scene; `apt_3`–`apt_5`: 1 mỗi scene | C2 và simultaneous D1+D2 được nghiệm thu theo các route đã ghi trong inventory |
| 84 staging scenes | Scene instance/navmesh/assets có | Chưa có route riêng cho từng scene | Chưa có C2/D1/D2; cần route config và preflight |
| `empty_stage` | Scene instance/navmesh/assets có | Route cũ trỏ `frl_apartment_stage`, không khớp instance `empty_stage` | Chưa chạy; route mismatch cần giải quyết bằng config riêng trước |

Không gọi 91 assets là 91 workflow-ready scenes. Staging scenes và `empty_stage` chưa có route hợp lệ.

### HM3D minival 00800–00809

Đã kiểm tra trực tiếp thư mục HM3D local được khai báo trong external resource lock. Cả 10 scene đều có `.basis.glb` và `.basis.navmesh`. HM3D scene-dataset config dùng chung hiện có. Semantic mesh/descriptor chỉ có cho 00800, 00802, 00803 và 00808; các file semantic không được dùng predictor input. File presence đã kiểm tra; lượt này không tính SHA-256 cho mọi HM3D asset. Preflight pilot phải hash inputs trước run.

| Scene | Basis + navmesh | Semantic assets | Route/cache hiện có | Trạng thái với workflow C2→D1/D2 chung |
|---|---|---|---|---|
| `00800-TEEsavR23oF` | Có | Có | 10 route configs; 9 capture runs đã lưu, gồm route 15 frame; P2/P3 có. U4OV crop features có nhưng chưa xác nhận tương thích với C2 OpenCLIP feature contract | Legacy/cache development; chưa nghiệm thu C2→D1/D2 chung |
| `00801-HaxA7YrQdEC` | Có | Không | Không có route config hoặc capture cache được tìm thấy | Assets-only; thiếu route/cache |
| `00802-wcojb4TFT35` | Có | Có | 5 route configs; capture smoke 10 frame, main 29 frame và dense 280 frame; P2/P3 và room replay snapshots có. C1 cached replay đã đạt | Workflow C2→D1/D2 đã được nối; một lần preflight C2 bị chặn bởi RAM 1.91 GiB < 3 GiB. Không có producer stage chạy, simultaneous acceptance pending |
| `00803-k1cupFYWXJ6` | Có | Có | Không có route config hoặc capture cache được tìm thấy | Assets-only; thiếu route/cache |
| `00804-BHXhpBwSMLh` | Có | Không | Không có route config hoặc capture cache được tìm thấy | Assets-only; thiếu route/cache |
| `00805-SUHsP6z2gcJ` | Có | Không | Không có route config hoặc capture cache được tìm thấy | Assets-only; thiếu route/cache |
| `00806-tQ5s4ShP627` | Có | Không | Không có route config hoặc capture cache được tìm thấy | Assets-only; thiếu route/cache |
| `00807-rsggHU7g7dh` | Có | Không | Không có route config hoặc capture cache được tìm thấy | Assets-only; thiếu route/cache |
| `00808-y9hTuugGdiq` | Có | Có | 2 route configs; U6 5-frame và Q1 7-frame capture caches có, cùng P2/P3. U2 appearance feature sidecar có; contract với C2 OpenCLIP features chưa xác nhận | Hybrid/static development có; chưa nghiệm thu C2→D1/D2 chung |
| `00809-Qpor2mEya8F` | Có | Không | Không có route config hoặc capture cache được tìm thấy | Assets-only; thiếu route/cache |

“Không tìm thấy” ở bảng route/cache là kết quả kiểm kê các route config `configs/habitat` và capture manifests trong hai run roots local/read-only đã khóa; không phải khẳng định không thể tạo route.

**Tóm tắt inventory HM3D:** đủ 10 scenes local; 3 scenes có route/cache được tìm thấy (`00800`, `00802`, `00808`); 7 scenes còn lại (`00801`, `00803`–`00807`, `00809`) thiếu route/cache trong inventory hiện tại. Việc có asset trên disk chỉ là readiness đầu vào, không phải bằng chứng pipeline đã chạy.

### Adapter và launcher hiện hữu

- [scene_launcher.py](../scripts/scene_launcher.py) legacy `_scene_context` chấp nhận `hm3d` và `replica_cad`; HM3D lấy `dataset.scene_file`, ReplicaCAD lấy `dataset.scene_instance`. `configs/launcher/scenes.json` hiện chỉ đăng ký alias legacy `hm3d_00802` và `replica_apt0`. Lệnh legacy `run` dùng P2/P3 + native replay/association pipeline cũ; không đồng nghĩa C2 hybrid + D1/D2.
- [capture_habitat_replica.py](../scripts/capture_habitat_replica.py) nhận scene path qua CLI và ghi `dataset_type`, depth/camera/pose contract; đây là capture worker tái sử dụng được nếu preflight xác nhận cấu hình HM3D.
- [fresh_replica.py](../integration_v2/fresh_replica.py) là đường C2 hiện hành, nhưng config contract yêu cầu `dataset.type == replica_cad`, `dataset.scene_instance` và frame prefix `replica-cad-*`. Vì thế C2 hiện không chấp nhận HM3D.
- D1/D2 có thể được giữ nguyên nếu HM3D C2 xuất đúng capture/cache/room contracts hiện hành. Tương thích đó phải được chứng minh bằng contract tests và pilot; chưa được suy từ C1 cache replay.

## Registry và run interface đề xuất

### Một registry, hai adapter

Schema versioned nên tách **dataset adapter** khỏi **scene instance** và **route**:

```yaml
schema_version: 2
datasets:
  - id: hm3d_minival_v0.2
    adapter: habitat.hm3d.v1
    root_ref: local_habitat_data
    scene_dataset_config: <locked local path>
  - id: replica_cad_v1.6
    adapter: habitat.replica_cad.v1
    root_ref: local_replica_cad_data
scenes:
  - key: hm3d_minival_v0.2:00802-wcojb4TFT35
    dataset: hm3d_minival_v0.2
    scene_asset: <basis.glb>
    navmesh: <basis.navmesh>
    semantic_assets: optional-and-never-predictor-input
    routes: [main, smoke, main_dense]
  - key: replica_cad_v1.6:apt_5
    dataset: replica_cad_v1.6
    scene_instance: <scene-instance.json>
    navmesh: <navmesh>
    routes: [navmesh_short]
```

Hai adapter chỉ chuyển scene metadata thành một `SceneSpec` chung: asset path, scene-dataset config, navmesh, optional semantic assets, scene/version IDs, frame-ID namespace, file hashes và license/provenance. Route thành `RouteSpec` chung: sequence ID, ordered poses, yaw, resolution, HFOV, intrinsics/calibration, sensor offset, depth range và route provenance. Route phải khai báo scene key khớp; preflight xác nhận assets/hash/pose/camera/scene identity trước khi tạo run.

Registry là **nguồn duy nhất** cho menu và CLI. Một resolver chung tạo `SceneSpec`/`RouteSpec`, effective config và stage plan; orchestration chung nhận kết quả đó. Menu không được duy trì scene/route list riêng. Trạng thái inventory (asset/route có trên disk) được tách khỏi **acceptance evidence**: C2, D1, D2-simultaneous chỉ được đánh dấu đạt từ run manifests hợp lệ có input hashes, scene/sequence và output hashes khớp; không suy ra từ config tồn tại hoặc thư mục có file.

Readiness được hiển thị theo gate, không gộp thành một boolean: `assets`, `route_config`, `route_preflight`, `capture_cache`, `C2`, `D1`, `D2_simultaneous`, `scope/membership`. Scene thiếu route vẫn có trong menu với nguyên nhân chặn cụ thể; không sinh route tự động. Các semantic assets không phải predictor input. Nếu scope chưa đạt, membership/room semantics vẫn `unavailable`.

### Giao diện V1: menu terminal và CLI dùng chung

V1 bao gồm menu tương tác terminal ngay trong launcher; map-picker không bị hoãn sang giai đoạn sau. Menu và CLI gọi cùng registry, resolver, preflight và orchestration:

1. Chọn dataset.
2. Chọn scene local; scene thiếu assets/route vẫn hiện cùng lý do bị chặn.
3. Chọn route hợp lệ.
4. Xem readiness, effective config và các stages/verification passes dự kiến.
5. Chạy preflight.
6. Hiển thị kết quả preflight rồi yêu cầu xác nhận `Run`.
7. Theo dõi progress theo stage và mở viewer sau khi D2 sẵn sàng.

Menu phải cho thấy được tối thiểu một ReplicaCAD scene đã có config và HM3D `00802` / route `main`; cùng một resolver phải tạo effective config và stage plan giống CLI. Đây là acceptance của config/menu, không yêu cầu thêm một ReplicaCAD producer run thật.

CLI tương ứng và lệnh tương thích hiện có:

```text
./run_scene.sh menu
./run_scene.sh datasets
./run_scene.sh maps --dataset hm3d_minival_v0.2
./run_scene.sh routes --dataset hm3d_minival_v0.2 --scene 00802-wcojb4TFT35
./run_scene.sh check --dataset hm3d_minival_v0.2 --scene 00802-wcojb4TFT35 --route main
./run_scene.sh scene-run --dataset hm3d_minival_v0.2 --scene 00802-wcojb4TFT35 --route main --viewer live
./run_scene.sh results <run-id>
./run_scene.sh open <run-id> live
```

`menu` là lệnh V1 đã triển khai. Các lệnh `fresh-replica`, `live-replay`, `results`, `open` vẫn hoạt động như compatibility entrypoints; `scene-run` dùng cùng resolver/orchestration với menu. Workflow pilot tạo C2 cache, dựng D2 viewer trước producer, yêu cầu explicit confirmation sau khi viewer/server gate đạt, rồi chạy đúng một D1. Viewer D1/D2, graph algorithm và mapper không được fork theo dataset/scene ID.

## HM3D integration pilot (đã duyệt; preflight blocked)

**Chọn scene:** HM3D minival `00802-wcojb4TFT35`, route `configs/habitat/hm3d_minival_v02_00802_main_route.json` — 29 fixed poses, 160×120; route có capture cache cùng scene/sequence và room snapshot. Đây là development input đã được xem, không phải holdout. Không chọn route theo predicted output.

**Kết quả lượt được duyệt:** implementation/tests đạt; lần C2 duy nhất dừng ở preflight vì RAM gate. Các producer passes bên dưới là số lượt dự kiến cho pilot, chưa được chạy:

1. Menu/CLI cùng resolver tạo một effective config cho HM3D; HM3D adapter chuẩn hóa scene asset, shared scene-dataset config, navmesh, route, frame IDs, calibration và provenance thành contracts chung. Tái sử dụng capture worker/legacy HM3D path thích hợp. Không sao chép toàn bộ C2 pipeline: các stage workers hiện có được gọi lại qua orchestration C2 chung.
2. Preflight assets/route/calibration/hash và scene/sequence identity. Dùng scene basis GLB, shared dataset config, navmesh và main route đã có. Semantic mesh/descriptor không vào predictor.
3. Chạy đúng một invocation C2 trên 29 bước, rồi đúng một invocation D1 trên cache C2; browser D2 được khởi động, kiểm tra và đặt ở trạng thái chờ trước khi D1 bắt đầu. D1 và D2 không được thay bằng replay hoàn tất/synthetic stream.
4. Kiểm tra graph/revision lineage, XYZ/XY/trajectory đồng revision, live-follow/seek, terminal final stage và hashes. Membership/room semantics giữ unavailable theo scope gate.

**Gate:** HM3D C2 hoàn tất từ adapter config; các scene/sequence/frame IDs khớp giữa capture, room producer, object mapping và unified revisions; D2 đã render revision thật khi D1 còn running; không có code branch theo scene ID; output/manifest hashes kiểm tra được. Hiện chỉ code/resolver/menu gates đạt; C2/D1/D2 pilot gate **pending** do preflight RAM failure. Khi đạt, gate chứng minh integration cho 00802, không phải quality, realtime hay mọi HM3D scene.

**Số lượt producer được phép (đã tính theo call path hiện tại):**

- Trước producer: chạy registry/resolver, menu, adapter contract, regression và viewer readiness tests; chỉ dùng fixtures/artifacts đã có, không chạy producer thật.
- C2: đúng **một invocation** 29 frame. Bên trong invocation hiện có: capture ×1; P2 ×1; P3 ×1; OccuSG replay chính ×1 **cộng một deterministic `fresh-check` replay nội bộ** (runner bật mặc định); image-feature extraction ×1; text-feature extraction ×1; ConceptGraphs mapping ×1; unified graph/viewer build ×1. Vì vậy C2 không đồng nghĩa một native room pass duy nhất; determinism pass được tính trước và không chạy lại riêng.
- D1: đúng **một invocation** trên C2 cache. D1 chạy một ConceptGraphs producer và một OccuSG producer song song, mỗi nhánh một pass qua 29 frame; C2 capture/P2/P3/features được reuse, không lặp. D2 được bootstrap và kiểm tra trước khi giải phóng hai producer.
- Sau D1: chỉ validate manifest/hashes và nghiệm thu browser trên chính run đó. Không chạy thêm C2/D1 replay hoặc scene khác. Nếu code path bắt buộc thêm producer invocation, dừng để xin duyệt.

## Ngân sách riêng

| Khoản | Ước lượng | Có sẵn / phải làm |
|---|---:|---|
| Registry, schema `SceneSpec`/`RouteSpec`, resolver, manifest-backed status | 2–3 giờ | Mới: schema chung, asset/hash contracts và tách readiness khỏi acceptance. Tái dùng run manifests làm nguồn evidence. |
| Menu terminal + orchestration chung với CLI + compatibility | 2–3 giờ | Mới: chọn dataset/scene/route, xem effective config/stage plan, preflight, xác nhận Run, progress/open. Tái dùng launcher và các entrypoints `fresh-replica`, `live-replay`, `results`, `open`; giữ tương thích. |
| HM3D adapter nối vào C2 | 2–3 giờ | Mới: resolve HM3D SceneSpec, frame namespace và config contract. Tái dùng capture worker/legacy HM3D adapter thích hợp, route format, C2 workers, unified graph, D1 coordinator và D2 viewer; không copy toàn bộ C2 pipeline. |
| Targeted tests + ReplicaCAD regression bằng tests/artifacts | 2–3 giờ | Mới: menu↔CLI resolver parity, route-missing block, manifest acceptance, HM3D contract, compatibility commands. Không thêm ReplicaCAD producer run thật. |
| **Phát triển một lần** | **8–12 giờ** | Không gồm runtime pilot. |
| Chuẩn bị HM3D 00802 | 15–30 phút | Hash input assets/route, preflight pose/calibration/connectivity, effective config/stage plan. |
| C2, một invocation 29 frame | 15–35 phút dự kiến | Bao gồm capture/P2/P3, OccuSG chính + built-in deterministic replay, image/text features, ConceptGraphs mapping và graph/viewer build. Chưa đo trên C2 HM3D. |
| D1, một invocation 29 frame | 10–25 phút dự kiến | ConceptGraphs và OccuSG mỗi nhánh một pass; dùng lại C2 cache, không chạy perception. |
| D2 readiness/GUI acceptance | 10–20 phút | Viewer được mở trước D1; kiểm tra revision thật khi D1 còn chạy, seek/picking/final stage. Một phần chồng với runtime D1. |

**Ngân sách pilot runtime:** khoảng **45–105 phút** elapsed cho preflight + C2 + D1 + GUI. Các stage runtime được ước lượng riêng ở trên; GUI acceptance có thể chồng một phần với D1 nhưng không giả định chồng toàn bộ. Tùy tốc độ environment/model/native workers; đây là dự toán, không phải cam kết. Nếu tests/preflight cho thấy cần thêm producer invocation, phát triển vượt 12 giờ hoặc pilot có khả năng vượt 105 phút, dừng và trình lại ngân sách trước khi tiếp tục.

## Sau pilot: mở các scene HM3D khác bằng config

- 00800 và 00808 đã có route/cache; sau khi pilot 00802 chứng minh C2 contract, chạy preflight rồi tạo config tham chiếu route hiện có. Feature sidecars chỉ được reuse khi model/checkpoint, preprocessing, dimensions và normalization hash khớp; nếu không, adapter ghi cache miss/không tương thích và dùng stage feature đã khóa, không trộn embeddings.
- 00801, 00803–00807 và 00809 đang thiếu route/cache. Khi chọn một trong số đó, tạo route config cho scene asset/navmesh đã có, chạy preflight, sau đó cùng `run` interface; không thêm nhánh theo ID. Không chạy tự động tất cả scenes.
- 84 ReplicaCAD staging instances cũng cần route configs; assets sẵn có không làm chúng tự động pipeline-ready.
- HM3D C2 pilot không đóng scope/membership/room-label gate. HM3D, ReplicaCAD và scene revisions tiếp tục báo `unavailable` đúng provenance khi evidence không đủ.

## Trạng thái quyết định implementation

Implementation theo checkpoint **registry/resolver → menu + HM3D adapter → tests → pilot** đã được duyệt; code và tests hoàn thành. Pilot preflight đã được thử đúng một lần nhưng không qua RAM gate nên không có C2 producer hay D1. Không chạy toàn bộ 10 HM3D hoặc 91 ReplicaCAD scenes, không tải thêm dataset, không chuyển MP3D. Các scene khác chỉ được đánh dấu acceptance theo run manifests tương ứng; config hoặc asset presence chỉ là readiness.
