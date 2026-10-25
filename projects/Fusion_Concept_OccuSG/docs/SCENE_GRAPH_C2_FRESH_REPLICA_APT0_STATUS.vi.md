# C2 — Fresh ReplicaCAD route workflow: status và giới hạn

Ngày chạy: 2026-10-16  
Run hoàn tất: `c2_replica_apt0_short_20261016_retry3`

Run manifest: [c2_run_manifest.json](../artifacts/conceptgraphs_hybrid_runs/c2_replica_apt0_short_20261016_retry3/c2_run_manifest.json), SHA-256 `e6ba63501521cf51e34266f5fcd7573699c4df1af66ac99c6f291f1d4da59c2c`.

## Kết luận

Launcher C2 đã chạy được một route mới trên Habitat-Sim ReplicaCAD `apt_0`, từ capture RGB-D đến ConceptGraphs object export, OccuSG room snapshot, unified graph và viewer. C2 là **batch xử lý route từ các fixed observation poses**; chưa phải live processing, realtime, autonomous navigation hoặc hỗ trợ mọi Replica/ReplicaCAD scene. Code lấy scene/route từ config, nhưng lần kiểm chứng hiện tại chỉ dùng một scene/config.

Room geometry được xuất cùng graph. Membership và room labels được giữ `unavailable` vì spatial elevation scope chưa được xác minh. Unified graph có 5 room nodes, 1 ConceptGraphs object node và 0 cạnh `room_contains` từ room tới ConceptGraphs object. Năm cạnh `room_adjacency` và 132 cạnh `room_contains` có sẵn trong OccuSG snapshot được giữ nguyên: 120 cạnh room→navigation node và 12 cạnh room→agent; không backfill nhãn hoặc membership.

## Lệnh

Lệnh đã chạy thành công (capture và P2 được reuse sau khi hash-verify từ run C2 dở dang):

```bash
./run_scene.sh fresh-replica \
  --config configs/scene_runs/replica_cad_apt0_short_c2_v1.json \
  --run-id c2_replica_apt0_short_20261016_retry3 \
  --reuse-from c2_replica_apt0_short_20261016_v1
```

`--reuse-from` chỉ nhận run C2 incomplete có scene/sequence, config, route, source assets và hashes khớp; capture manifest, P2 index và toàn bộ mask files được xác minh. Lệnh mở viewer được launcher cung cấp:

```bash
./run_scene.sh open c2_replica_apt0_short_20261016_retry3 hybrid
```

Viewer HTML đã được tạo và hash-verify. Tôi chưa xác minh tương tác GUI: browser policy chặn mở URL `file://` trong phiên này, nên không dùng cách vòng qua policy. Có thể mở bằng lệnh launcher trên máy người dùng.

## Input và provenance

| Input | Giá trị / SHA-256 |
|---|---|
| C2 config | `configs/scene_runs/replica_cad_apt0_short_c2_v1.json` — `34a3c6dd9bf033804fd7ada2f956db01f3d9ff2c5d9a0437300899b26b8d1ffb` |
| Route config | `configs/habitat/m12_replica_cad_apt0_instance_route.json` — `fd0cbfe9c5fdd5a094b8670219756fa0aa3810596a938a5653a6d8a9a1f08cc4` |
| Effective route | `7494c56dd5b160ebc490fe9c94bae573b3acd050b94de6e205e30d0ecc3766dd` |
| ReplicaCAD scene instance | `7935087e35b8c0fb23d3503749eaa8b5b766cf44dc21c20ce70b79a64e8651fe` |
| ReplicaCAD dataset config | `a51d4fe5e1853e616c13710930191bad33c0b277edece7c4e520d1194831f5e3` |
| Navmesh | `1b5336a6b22278412ab9297f0a18d9a821942754fc1413274832bfe80bd54e01` |
| YOLO11n-seg weights | `0bc32bc92e985b881141ef9bd2216e2a746f70519d0d24da9fc85decc4428cf4` |
| OpenCLIP ViT-B/32 checkpoint | `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31` |
| ConceptGraphs reference | commit `93277a02bd89171f8121e84203121cf7af9ebb5d` |
| OccuSG reference | commit `2bca2fa06af87fd9dd0542957039be8f580f0dca` |
| Native M2/M3 binary | `bd171a8ac9974eec18957af97e6fc369729358ca62985390eb994c26f2040b29` |
| Native DuDe binary | `7868c892f3584faa17712e1ede0660006d97531b05be5c258fe303015f1ef959` |

Không cài dependency, tải model hoặc dùng GPU. Environment probes đạt: Habitat-Sim 0.3.3; P2 environment NumPy 2.5.3/ONNX Runtime 1.24.4; feature environment Torch 2.6.0 CPU/OpenCLIP 3.2.0; P3/ConceptGraphs CPU environment Torch 2.0.1 CPU/PyTorch3D import ok/Open3D 0.17.0/FAISS 1.7.4/SciPy 1.10.1.

## Kết quả run

- Route: 12 poses, 12 pose matrices khác nhau; toàn bộ poses navigable và các đoạn liên tiếp connected theo navmesh. Đây là fixed ground-truth observation poses; navmesh chỉ kiểm tra tính hợp lệ, không lập kế hoạch di chuyển.
- Capture: 12 RGB-D frames, 160×120, depth axial mét, K và pose theo frame.
- P2: 18 detections/masks trên 12 frame.
- P3: stage hoàn tất theo contract, nhưng 0 accepted và 18 rejected với config hiện có. Ledger cho thấy 14 detections có đủ valid depth pixels nhưng largest Euclidean cluster chỉ 1–24 points, dưới `cluster_min_points=100`; 4 detections còn lại bị `rejected_insufficient_depth_points` với 25, 36, 77, 85 valid pixels. Effective `cluster_eps_m=0.02`, sampling stride 1. Đây là lý do thực tế theo policy hiện hành, không phải kết luận các mask sai hoặc cần tuning. ConceptGraphs hybrid vẫn lift trực tiếp từ P2 masks + RGB-D.
- OccuSG native replay: 12 frame, 5 rooms, 6 regions, 956 edges; node types gồm 134 navigation, 12 agent, 5 room và 0 native object node. Snapshot reload, delta replay và fresh replay checks đều pass. Replay mất 81.97 giây. Snapshot không có room semantic labels.
- Feature sidecars: 18 image vectors và 18 text vectors, dimension 512, finite và L2-normalized. Checkpoint local hash khớp. Text builder gọi `create_model(... pretrained=None)` rồi load toàn bộ state dict từ checkpoint safetensors với `strict=True`; cảnh báo OpenCLIP nói không nạp pretrained tại bước khởi tạo, không có nghĩa vectors được tạo từ weights ngẫu nhiên.
- ConceptGraphs reference mapping: commit đã khóa; seed 20261014; CPU; config min detections 3. 18 detections → 15 online hypotheses → 15 sau denoise → 1 sau filter → 1 sau text-gated merge. Mười bốn hypotheses bị filter vì mỗi hypothesis có một detection; một hypothesis giữ lại có bốn detections từ bốn frame/pose. Đây là số đếm pipeline, không phải đánh giá chất lượng identity.
- Unified graph: 5 rooms + 1 ConceptGraphs object; 0 room→ConceptGraphs-object edges; 5 `room_adjacency` và 132 source `room_contains` edges được giữ nguyên (120 room→navigation, 12 room→agent). Membership `unavailable_elevation_scope`; semantics `unavailable_membership_scope`.
- 428 output file hashes kiểm tra đạt; tổng output khoảng 14.3 MB. Capture/P2 source run: capture 16.36 giây, P2 4.22 giây. Successful C2 invocation với capture/P2 reuse chạy 131.93 giây; các stage được tính riêng trong manifest.
- Mapping process peak RSS 461.84 MiB; image encoder khoảng 1.49 GiB; OccuSG process khoảng 650 MiB. Preflight ghi 6.28 GiB RAM khả dụng và khoảng 327 GB disk trống.
- Tài nguyên, default launcher, source/reference repositories và baseline không bị sửa.

## Lỗi integration đã gặp và cách sửa

Các run lỗi được giữ nguyên trong artifacts, không tính là lần C2 hoàn tất:

1. `c2_replica_apt0_short_20261016_v1`: capture và P2 đạt; runner gọi P3 bằng `--cache`, trong khi CLI yêu cầu `--detection-2d`. Sửa đúng tên argument.
2. `..._retry1`: image vectors đủ 18/18 nhưng orchestration đòi `image_features/manifest.json`, file mà builder không tạo. Sửa required-output contract sang `feature_manifest.jsonl` và `feature_extraction_report.json`; thêm kiểm tra status, population và checkpoint hash.
3. `..._retry2`: P3 không có accepted detections nên pose index P3 rỗng; diagnostic export lỗi khi tra pose theo frame. Mapping đã có fallback RGB-D capture adapter. Pose map giờ lấy P3 pose khi có, nếu không thì dùng cùng `frame_from_capture_record`; P3 pose nếu có sẽ được đối chiếu với capture. Không thay ConceptGraphs association/lifting.

OpenCLIP text feature builder phát cảnh báo `pretrained=None`; code xác minh hash checkpoint rồi `load_state_dict(..., strict=True)`. Manifest text sidecar ghi checkpoint hash, model revision, raw class strings và tokenizer.

## Kiểm chứng

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_c2_fresh_replica.py tests/test_unified_scene_graph.py \
  tests/test_hybrid_mapping_viewer.py tests/test_scene_launcher.py \
  --basetemp /tmp/c2-scene-tests-final
```

Kết quả: **48 passed**. Tests bao gồm scene/route config gate, unavailable elevation policy, viewer status, pose fallback khi P3 rỗng, đối chiếu P3 pose với capture, missing/mismatched pose rejection và room graph signature. So sánh snapshot với unified graph xác nhận polygons, 5 cạnh adjacency và 132 cạnh `room_contains` (120 room→navigation, 12 room→agent) giống hệt; không có room→ConceptGraphs-object edge. Toàn bộ output hashes cũng đạt.

**GUI visual inspection chưa đạt** do browser policy chặn `file://`; HTML build, output hashes và launcher path đã kiểm tra. Không tuyên bố browser interaction đã pass.

## Nghiệm thu viewer C2 — kiểm tra artifacts/source, chưa phải GUI pass

Không chạy lại stage nào. Kiểm tra tĩnh `hybrid_viewer.html`, builder,
manifest và các sidecars của run cho thấy:

- Có hai chế độ `Before filter` và `Final map`: lần lượt 15 hypotheses và
  1 final object. Hypotheses bị filter được ghi riêng, không tính là đã merge
  vào object sống sót.
- Cả hai panel vẽ room polygons và object cloud chiếu XY; cloud được lấy mẫu
  để hiển thị, geometry nguồn không bị đổi. Đây không phải camera 3D/orbit view.
- Có 18 observation records; RGB, mask, overlay và crop sidecars đều tồn tại
  (72 asset references, 0 file thiếu). Builder đưa observation IDs, raw class
  history, detection/frame/pose counts và disposition vào phần chọn object.
- Banner ghi membership `unavailable_elevation_scope` và room semantics
  `unavailable_membership_scope`; không có room→ConceptGraphs-object edge.
- Viewer C2 hiện không vẽ camera pose/trajectory. Trường camera poses có trong
  evidence data nhưng không được hiển thị thành trajectory ở trang này.
- Snapshot có 5 `room_adjacency`; 132 `room_contains` được phân loại theo
  target node: 120 room→navigation, 12 room→agent, 0 room→ConceptGraphs object.

GUI thực tế vẫn **chưa được nghiệm thu**: phiên browser hiện tại không cho mở
local `file://`, còn HTTP helper có sẵn chỉ phục vụ `cached_replay_*`, không
phải run C2 này. Không dựng server/đường mở khác để vòng qua giới hạn đó.
Lệnh người dùng có thể chạy trên máy local vẫn là:

```bash
./run_scene.sh open c2_replica_apt0_short_20261016_retry3 hybrid
```

Lệnh trên chưa được xác minh tương tác bằng browser trong lượt này.

## P3 còn phục vụ gì trong workflow C2

P3 không phải nguồn point lifting cho ConceptGraphs hybrid: hybrid mapper dùng
P2 + RGB-D và capture pose; khi P3 pose có mặt, runner đối chiếu nó với pose
capture. Nhưng P3 vẫn là input tường minh của OccuSG replay (`--detection-3d`,
manifest và exact frame-set validation) và là stage/ledger trong C2 workflow.
Trong run này P3 rỗng về accepted detections, nên native replay không có P3
object detections để nhập; room graph vẫn được dựng từ replay. Lượt này không
thay hoặc bỏ P3.

## Trạng thái milestone

| Phần | Trạng thái |
|---|---|
| Thành phần có sẵn | Capture, P2, P3, OccuSG native runner, ViT-B/32 sidecar builders, ConceptGraphs hybrid mapping, unified graph adapter và viewer được tái sử dụng. |
| C1 cached replay | Đạt trên timeline HM3D cache đã khóa; không phải capture mới. |
| C2 fresh scene+route | Đạt cho một ReplicaCAD v1.6 `apt_0` config/route, fixed-pose batch. Chưa xác nhận portability trên scene/config khác. |
| C2 GUI nghiệm thu | Chưa đạt: artifacts/source được kiểm tra; browser interaction chưa quan sát được. Camera trajectory cũng chưa được vẽ trong viewer C2. |
| D live processing viewer | Chưa triển khai; C2 viewer là final-map viewer, không phát revisions khi pipeline chạy. Proposal: [SCENE_GRAPH_D_LIVE_PROCESSING_PROPOSAL.vi.md](SCENE_GRAPH_D_LIVE_PROCESSING_PROPOSAL.vi.md). |
| Spatial scope/membership/room labels | Pending riêng; spatial scope unavailable, không được C2 bypass. |
| Multi-map/route, realtime, ObjectNav | Chưa kiểm chứng/triển khai. |

Bước tiếp theo là GUI acceptance do người dùng mở lệnh trên và kiểm tra hai mode/evidence; sau đó, theo thứ tự roadmap, D live processing proposal cần được duyệt trước khi code. Route ReplicaCAD thứ hai thuộc validation portability riêng, không bị trộn vào nghiệm thu viewer hoặc D.
