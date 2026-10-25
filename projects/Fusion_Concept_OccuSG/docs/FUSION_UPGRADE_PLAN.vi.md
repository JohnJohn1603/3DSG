# Fusion_Concept_OccuSG — Kế hoạch nâng cấp có đối chứng

> **LEGACY / HISTORY (2026-10-12):** This plan/handoff belongs to the earlier U/K/Q development track. It is retained for provenance and is not the active roadmap. Follow [the current ConceptGraphs → OccuSG plan](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md).


Ngày lập: 07-10-2026. Cập nhật trạng thái: 11-10-2026.

## Trạng thái triển khai hiện tại

| Mức | Thành phần | Trạng thái |
|---|---|---|
| **Đã dùng được, có điều kiện đầu vào** | Launcher `source`/cached replay/`compare-profiles`; `fuse-objects` chạy geometry fusion trên assignments cố định, có `--check` và `open ... fusion` | Sidecar selected-point P3 chỉ được reuse khi khớp hash/config; nếu thiếu, dựng lại từ RGB-D/P2 cache vào output riêng và so Detection3D với cache chuẩn. Cache cũ không lưu pixel indices thì không tuyên bố exact point-set parity. Không đổi association, class, graph hoặc room scores. |
| **Experimental** | K4 accumulated-surface association | Candidate offline trên P3/cache cố định; Case A được human review xác nhận match đúng, Case B vẫn defer/missed association. Chưa thay default, chưa tích hợp K4 vào M9. |
| **Diagnostic-only** | Pixel visibility v2 và U4-OV | Không tạo identity/match hoặc room-score decisions. Pixel visibility dùng đúng Habitat intrinsics contract; không nối matcher. |

`source` vẫn là default. Geometry-only profile là opt-in. K4 không phải “toàn bộ ConceptGraphs đã bật”; candidate appearance chỉ là ablation. U5 overall vẫn partial: integration/reconciliation kỹ thuật có, policy merge/retirement thật defer và dense replay chưa kiểm chứng. Các milestone/gate chi tiết bên dưới là kế hoạch/định nghĩa gốc; bảng này là trạng thái triển khai mới nhất.

Diagnostic calibration correction và counts mới nhất: [K4 pixel visibility v2 report](../artifacts/audits/object_graph_improvement/k4_pixel_visibility_v2/report.vi.md). Visibility v1 được giữ để truy vết và có [notice về calibration mismatch](../artifacts/audits/object_graph_improvement/k4_pixel_visibility_v1/calibration_notice.vi.md).

## 0. Quyết định và mục tiêu

Giữ xương sống Habitat → RGB-D → OctoMap → MapConverter → DuDe/tracker → rooms/graph/launcher. Nâng cấp tầng object bằng association hình học + appearance, tích lũy evidence đa-view và class có thể sửa. Sau đó nghiên cứu refinement ranh giới phòng riêng, không đánh đồng nhãn phòng với polygon phòng.

Ba đầu ra phải báo riêng:

1. **Object quality:** identity, geometry và class đúng hơn; giảm class flicker/duplicate/false merge.
2. **Room semantics/membership:** object nằm đúng region; nhãn công năng dùng evidence đáng tin, có abstention.
3. **Room boundary quality:** footprint, split/merge, adjacency chính xác hơn theo GT độc lập.

ConceptGraphs giúp trực tiếp mục 1; có thể giúp mục 2. Nó **không cung cấp sẵn một bộ sửa ranh giới DuDe**, và không bảo đảm mục 3 tăng đáng kể. Phần refinement ở U7 là nghiên cứu mới của dự án, không được gọi là thuật toán gốc ConceptGraphs. Mục tiêu “đáng kể” được định lượng ở §10; đó là tiêu chí thử nghiệm, không phải lời hứa kết quả.

Không viết lại toàn bộ. Không navigation/Value Map/SLAM, không Webots runtime mới, không đổi detector cùng lúc với fusion, không chạy cloud, không tải lại dataset. Một tầng, scene tĩnh, pose GT vẫn là giả định ban đầu.

## 1. Nguồn đã kiểm tra và khóa

### 1.1 Reference repositories

| Reference | Commit | Đường dẫn mới |
|---|---|---|
| OccuSG | `2bca2fa06af87fd9dd0542957039be8f580f0dca` | `ref_OccuSG/OccuSG/` |
| ConceptGraphs main | `93277a02bd89171f8121e84203121cf7af9ebb5d` | `ref_ConceptGraph/concept-graphs/` |
| ConceptGraphs ali-dev, chỉ tham khảo | `72f5962822b5e8678a446f367a06df1a977d2a4d` | remote ref trong cùng clone; không mix source ngầm |

Repo OccuSG mới được clone độc lập `--no-hardlinks` từ checkout local đã pin để tránh tải lại; origin trỏ về GitHub chính thức. ConceptGraphs clone từ GitHub. Cả hai HEAD được detach ở commit đã khóa. Không sửa reference trees. Submodules chưa init; MapConversion đang có checkout độc lập sẽ tái dùng bằng explicit path.

Baseline triển khai thực tế là `../Rebuild_3DScene_OccuSG`, không đồng nhất với OccuSG upstream. Root baseline không có Git HEAD hợp lệ; khóa theo file SHA-256 trong `locks/baseline_seed_manifest.json`, không bịa baseline commit. Repo tham chiếu riêng vẫn là Git sạch.

### 1.2 Các điểm nối đã đọc trong baseline

| File/symbol | Phát hiện | Hệ quả thiết kế |
|---|---|---|
| `src/standalone_sg/objects/association.py:ObjectAssociationManager` | same-class nearest, same-pose suppression khác class; giữ class/geometry/score lần đầu | Giữ làm strategy `source`; fusion phải là strategy khác |
| `contracts/objects.py:Detection3D` | center/extents/provenance; chưa có point-set, appearance feature hay view ledger đầy đủ | Thêm observation sidecar riêng, không nhét vector lớn vào JSON node |
| `ObjectAssociationResult.associations` | đã có per-detection decision | Export đầy đủ, đừng chỉ dùng first/last endpoints |
| `pipeline/builder.py:SceneGraphBuilder` | nhận injected object manager; serialize fields ObjectNode cụ thể; dùng object center cho M8 | Không chỉ thay manager: cần adapter export, merge/removal events, ownership reconciliation |
| `scripts/run_habitat_native_replay.py:_run_once` | tạo manager explicit từ pipeline config; mapping/native defaults nằm trong runner | Lock **effective config**, không chỉ hash YAML |
| `scripts/project_detection_masks_to_3d.py` | Euclidean clusters; output center/extents, chưa persist selected point-set | Lưu accepted cloud/pixel indices đúng thuật toán hiện tại để fusion dùng lại |
| `semantics/evidence.py`, `adapters.py`, `room_labeler.py` | RegionEvidence; rule scores additive, không phải probability | Thêm eligibility/resolved class và evidence revision; giữ weights để ablation |
| `scripts/scene_launcher.py` | CLI/interactive shell; strict registry/environment schema | Extend có version/migration, giữ lệnh cũ; không thêm key tùy tiện làm schema fail |
| `scripts/evaluate_hm3d_object_gt.py` | evaluator-only GT, mapping có trạng thái review; lineage cũ incomplete | Dùng lại evaluator, bổ sung full observation lineage ở run mới |

`configs/perception/...json` chứa association metadata 0.50/0.10 nhưng replay dùng pipeline profile (ví dụ 0.75/0.15). Không xem metadata detector là runtime M6. Mapping cũng có các giá trị literal trong runner: ghi effective values, chưa sửa để giả tương đương.

### 1.3 ConceptGraphs: lấy gì, không lấy gì

| Source chính xác ở commit main | Cơ chế có thật | Cách dùng |
|---|---|---|
| `conceptgraph/slam/mapping.py:compute_spatial_similarities` | 3D IoU/GIoU hoặc cloud overlap | Tham khảo và port có fixture parity cho metric được chọn |
| `compute_visual_similarities`, `aggregate_similarities` | cosine image CLIP features, weighted sum spatial/visual | Geometry gating + appearance matching; không hard same-class gate |
| `merge_detections_to_objects` | mỗi detection chọn argmax object đủ score | Oracle tham khảo; chính sách one-to-one/cannot-link mới phải ghi là extension |
| `conceptgraph/slam/utils.py:merge_obj2_into_obj1` | nối observation lists, fusion cloud/bbox, average/normalize CLIP/text features | Port phần cần thiết; giữ provenance và giới hạn bộ nhớ |
| `filter_objects`, `merge_objects` | lọc theo số điểm/lượt phát hiện; merge overlap với các gate | Tham khảo lifecycle và duplicate handling, không đồng nhất số detection với số view độc lập |
| `conceptgraph/utils/model_utils.py:compute_clip_features*` | crop bbox padded, encode image/text | Tách extractor nhẹ cho YOLO crops; không import nguyên module kéo SAM/CUDA |
| `conceptgraph/slam/cfslam_pipeline_batch.py` | threshold association, periodic denoise/filter/merge | Tham khảo orchestration, không thay launcher/graph engine hiện có |

Các chi tiết không copy mù:

- `create_object_pcd` có perturbation ngẫu nhiên; giữ M1/P3 hiện tại, không thêm noise để “giống source”.
- Feature averaging upstream theo số detection, chưa chứng minh loại được tương quan các frame kề nhau.
- Có code `.to("cuda")`, imports SAM/FAISS/PyTorch3D/GradSLAM; không kéo tất cả vào runtime CPU hiện tại.
- Upstream chọn argmax theo detection không tự bảo đảm one-object-per-physical-instance hoặc giải quyết false merge.
- Text feature từ class detector có thể mang chính lỗi `bed`; không coi đây là bằng chứng độc lập xác nhận class.
- Caption/LLM/API, SAM/GroundingDINO, SLAM/GradSLAM và giao diện riêng: chưa cần trong phiên bản đầu.
- `tentative/confirmed/conflicted`, sửa class có margin, viewpoint bins, room refinement là **thiết kế mới**, không tuyên bố nguyên bản ConceptGraphs.

Link khóa: [ConceptGraphs](https://github.com/concept-graphs/concept-graphs/tree/93277a02bd89171f8121e84203121cf7af9ebb5d), [OccuSG](https://github.com/crcz25/OccuSG/tree/2bca2fa06af87fd9dd0542957039be8f580f0dca).

## 2. Cấu trúc workspace và chính sách mang sang

```text
Fusion_Concept_OccuSG/
  README.vi.md                       # trạng thái thật và điểm bắt đầu
  run_scene.sh                       # launcher baseline đã copy; nâng cấp về sau
  src/standalone_sg/                  # source baseline độc lập, giữ namespace ban đầu
    objects/                         # source strategy; fusion strategy sẽ thêm tại đây
    semantics/                       # region/object evidence và labeler
    regions/                         # DuDe baseline; refinement optional về sau
  adapters/habitat/                   # capture/frame conversion hiện có
  scripts/                           # runners/evaluators hiện có
  configs/                           # profiles hiện có, chưa đổi runtime
  native/                            # wrappers C++ hiện có
  tests/                             # regression source-only hiện có
  evaluation/                        # evaluator code/protocol, không copy evaluation runs
  webots/                            # source-only compatibility cho imports/tests cũ
  docs/
    FUSION_UPGRADE_PLAN.vi.md
    NEXT_AGENT_TASK.vi.md
    SOURCE_AUDIT.vi.md
    baseline_reference/              # tài liệu cũ, có thể chứa link artifacts không mang sang
  locks/
    baseline_seed_manifest.json      # checksum từng viên gạch đã copy
    reference_repositories.json
    reuse_resources.json             # explicit external inputs, không phải runtime config
    sources.json                     # source lock cũ giữ nguyên để audit/parity
  ref_OccuSG/OccuSG/
  ref_ConceptGraph/concept-graphs/
  tools/prepare_source_seed.py        # one-shot bootstrap, refuse overwrite
  artifacts/                         # run/cache MỚI; không symlink vào old runs
  build/                             # build tree MỚI nếu cần rebuild
```

Đã copy source/config/tests/native/scripts và launcher theo allowlist, SHA-256 từng file. Không copy old artifacts, dataset, model weights, env, build tree, credential, `.aws`, `.codex`, `.agents` hoặc cache. Webots source nhỏ được giữ vì runner/test baseline còn import; không chạy Webots và không mang output Webots.

Không đặt runtime import vào source của baseline bằng sys.path hoặc editable install. Python source dùng bản copy mới; baseline chỉ là nguồn input/cache/resource đã khai báo. Hai reference repos để tra cứu/oracle, không tự thành runtime dependencies toàn bộ.

## 3. Reuse môi trường và tài nguyên an toàn

### Những gì đã kiểm kê (metadata/find_spec, chưa kiểm chứng inference feature)

| Interpreter | Hiện có | Thiếu đáng chú ý |
|---|---|---|
| Baseline `.venv-perception/bin/python`, Python 3.12.3 | NumPy 2.5.3, ONNX Runtime 1.24.4 | SciPy, Torch, torchvision, OpenCLIP, Open3D, FAISS |
| `/home/kiettran05/miniconda3/envs/habitat/bin/python`, Python 3.9.23 | Habitat-Sim 0.3.3, NumPy 1.26.4, SciPy 1.13.1 | Torch/OpenCLIP/Open3D |
| `.../envs/hovsg/bin/python`, Python 3.9.25 | NumPy, FAISS import spec | Torch/OpenCLIP/Open3D chưa tìm thấy |

Không kết luận model CLIP/GPU không tồn tại trên toàn máy: chỉ kiểm tra một số path/môi trường. `nvidia-smi` không nằm trong PATH lúc kiểm tra, không chứng minh máy không có GPU.

Kiểm chứng seed cuối lượt: system Python chạy 25 targeted M6/semantic tests pass; `.venv-perception` không có pytest. Tách interpreter test/runtime trong U0, không cài thêm vào baseline env để sửa nhanh.

Quy tắc:

1. Capture dùng Habitat env hiện tại, perception/replay dùng baseline env **read-only**. Không pip/conda upgrade vào hai env này.
2. Dataset/model/native binaries/prefix dùng absolute path đã khóa. Không symlink toàn bộ `artifacts/` hoặc `build/` cũ vào workspace mới.
3. Nếu cần relative asset path tương thích, U0 có thể tạo alias chỉ cho dataset/models sau khi audit mọi writer; symlink không phải hàng rào read-only. Mọi output/cache mới phải được resolver kiểm tra nằm trong root mới, không resolve sang baseline.
4. Không cài nguyên `environment.yml` ConceptGraphs. Feature extractor có interpreter riêng nếu deps chưa có; kiểm kê weights/cache trước, đề xuất checkpoint + dung lượng + license và xin quyết định trước khi tải model lớn. Không dùng API cloud/LLM.
5. Có thể benchmark một OpenCLIP encoder nhẹ hơn upstream ViT-H-14; pin checkpoint/hash/preprocessing và ghi là hybrid extension, không claim source-model parity. Không đổi encoder giữa baseline/candidate âm thầm.
6. Feature process giao tiếp bằng NPZ + JSONL có schema/checksum, không dùng pickle từ nguồn ngoài. Embedding dim/model/preprocess version phải tương thích tuyệt đối.
7. Native worker cũ có RPATH tới prefix cũ: U0 chạy `ldd` và smoke. Rebuild về sau dùng build dir mới, prefix cũ làm input; không dùng lại CMakeCache cũ.
8. MapConversion pinned checkout hiện ở `Documents/Codex/.../research/occusg_phase1/upstream/mapconversion`; truyền path tường minh. Clone OccuSG không tự có tất cả submodules.

**Phát hiện hiệu năng cần đo:** P3 `cluster_components()` import cKDTree, fallback all-pairs khi không có SciPy. Launcher dùng project_python cho P3, và env đó hiện thiếu SciPy. Đây là ứng viên bottleneck O(n²), không đủ để quy cả 87 phút cho nó. Tối ưu phải ở môi trường/profile riêng và kiểm tra point-set/cluster parity, không vừa thay P3 vừa đo fusion.

## 4. Kiến trúc nâng cấp

```text
Habitat RGB-D + pose GT
       ├── M1 → M2 → M3 → M4 → M5 ───────────────┐
       │         baseline geometry frozen       │
       └── YOLO cache → P3 accepted observations │
                          ├── bounded cloud     │
                          └── image features    │
                                   ↓            │
                        ObjectBackend           │
                        source | fusion         │
                                   ↓            ↓
                          M8 membership/rooms ← M7
                                   ↓
                         M9/M10 graph + deltas
                                   ↓
                  confirmed object evidence → room labels

Optional later: M5 → structural refinement → refined regions
                (separate profile, GT-only evaluator, original M5 kept)
```

U1–U4 chạy fusion sidecar trên capture/cache và fixed region trace trước, không replay native hai lượt mỗi thử nghiệm. U5 mới nối builder full. Phân biệt:

- **Fixed-partition control:** cùng polygons M5/M8, chỉ đổi objects/semantic evidence.
- **Integrated run:** M8 promotion/membership có thể đổi khi object set/geometry đổi; phải đo riêng. Không tuyên bố M8 nguyên vẹn chỉ vì M4 không đổi.
- **Refinement run:** có thay boundaries; chỉ được phép ở U7.

## 5. Contracts và association cần có

### 5.1 Observation ledger (immutable, append-only)

Mỗi observation có `observation_id`, run/frame/detection ID, timestamp, camera pose, intrinsics hash, mask/bbox hash, raw class/score, P3 decision, selected-cloud reference/hash, center/extents method, valid-depth counts, crop/feature hash, view-bin, quality flags, model IDs. Lưu cả rejected metadata nhưng không tự đưa rejected cloud vào fusion.

`ObjectTrack`: stable ID, observation IDs, prototype image feature, bounded geometry, raw class histogram, view-weighted class evidence, resolved class, existence status và class status tách riêng, first/last seen, revision và alias/tombstone history.

`AssociationEvent`: candidates/gates/scores, chosen track, create/match/defer/merge/reject reason, ambiguity margin, no-GT provenance. Không chỉ first/last endpoints. GT instance IDs không nằm trong predictor contracts.

### 5.2 Ghép khác class nhưng không gộp bừa

1. Spatial index và floor/layer gate chọn ứng viên; khoảng cách chỉ là broad phase, không là bằng chứng identity.
2. Kiểm tra visible cloud overlap/geometry, depth-consistent reprojection khi có đủ overlap, và image feature cosine. Hai góc thấy hai mặt không giao nhau cần được phép **defer**, không nới gate vô hạn.
3. Không hard-gate same class trong fusion profile; class chỉ là evidence mềm, không cho text embedding của nhãn sai tự củng cố identity.
4. Một frame có hai vật riêng không được nhập chỉ vì gần nhau/appearance giống. Lưu cannot-link khi có bằng chứng đồng thời là hai instance riêng. Mask rời nhau đơn thuần chưa đủ vì một vật có thể bị phân mảnh.
5. Assignment one-to-one trên observations đã xử lý duplicate trong frame; cho phép unmatched/dummy. Nếu chọn khác argmax upstream phải ghi extension và có test riêng.
6. Multiple masks gần trùng trong một frame được deduplicate thành tối đa một đóng góp evidence/track/view; giữ raw records để audit, không đổi detector cache.
7. Merge track cũ chỉ khi bằng chứng mạnh, có event và canonical alias; không merge chỉ vì cùng room label. Mặc định conservative merge, không merge phá hủy source observations.
8. New unmatched observation trở thành tentative track, không ngay lập tức là semantic object confirmed.

### 5.3 Fusion class và geometry

Khởi đầu class evidence không là calibrated probability:

`E_t(c) = Σ_view_bin capped_contribution(bin, c)`

Contribution dựa trên raw detector score, view quality và consistency, nhưng không cộng số ảnh giống nhau như evidence độc lập. Params như bin angle/distance, cap, minimum independent views, switch margin khóa trên development. Chất lượng bị cắt khung/occluded chỉ giảm trọng số/flag; không gọi một heuristic quality là “truth”.

- First class chỉ là `initial_class`; `resolved_class` có revision.
- Confirm cần support đủ và margin; conflict không ép nhãn.
- Có thể sửa/deconfirm khi evidence mới mâu thuẫn. Không bảo toàn class đầu tiên như source.
- Một object tồn tại chắc nhưng class chưa rõ là trạng thái hợp lệ.
- Không dùng room predicted label để quyết định class object ở phiên bản đầu (tránh vòng tự củng cố).
- Geometry fusion giữ bounded voxelized cloud; không dùng union AABB không kiểm soát hoặc weighted centroid tùy tiện khi chỉ thấy bề mặt khác nhau. Có outlier/extent growth checks và reset/replay khả dụng.
- Class-only và geometry-fusion bật riêng để đo tác động membership. Không đổi center trong ablation class-only.
- Không đủ thông tin full class distribution từ ONNX cache thì lưu top-1 evidence, không bịa xác suất cho các class còn lại.

## 6. Room semantics, membership và lifecycle

- `source` dùng M6 và rules cũ; `fusion_semantic` dùng canonical object ID + resolved class có eligibility.
- Preserve observed/tentative objects trong output; không xóa khỏi diagnostics để làm precision đẹp hơn.
- Evidence room dùng mỗi physical-track một lần, không cộng theo số frame; dedup aliases sau merge.
- Class correction/membership change/merge/deconfirm phải **rút đóng góp cũ**, tính lại signature/hash/scores và phát delta. Không để `bedroom` giữ điểm bed cũ khi object đã đổi class.
- Geometry revision, evidence revision, observation timestamp và processing timestamp tách riêng. Region stale không làm mới evidence giả.
- `supporting_object_ids` per candidate phải giải thích được; giữ nguyên rule weights trong ablation đầu.
- Room IDs không đồng nhất region IDs; nhiều-object changes không được gây dangling edges hoặc hai owners cho cùng object.
- Thêm graph schema/version cho fusion metadata; raw vectors/cloud lưu ngoài snapshot. Reload/replay và query hỗ trợ alias, removed IDs.
- Pending/tentative có thể xuất vào debug layer riêng; chỉ confirmed exported objects tham gia semantic scoring. Báo coverage bị mất, không che bằng unknown.
- Với room:2 reported `living_room=8.5`, `dining_room=8.0`, `bedroom=5.0`: sửa bed không bảo đảm hết ambiguous. Tính counterfactual bằng rules thực, không đặt kỳ vọng label dining_room bằng mọi giá.

## 7. Milestones, gate và điểm dừng

### U0 — Baseline transplant và safety gate (làm đầu tiên)

- Kiểm tra manifest seed và refs; không sửa baseline nguồn.
- Bind external resources theo §3; đảm bảo mọi writer vào root mới. GT/profile paths có thể trỏ old runs: phân biệt input-only và output, không rewrite mù.
- Giữ launcher `/help /all /maps /use /info /routes /check /run /status /results /object-results /open /register`.
- Profile hiện tại là source; route alias và resolution giữ đúng. Config inheritance/version v2 khi thêm backend.
- Chạy source-only unit suite và một HM3D smoke replay từ capture có sẵn, output mới; optional ReplicaCAD smoke.
- So canonical snapshot/delta/trajectory với input identical; runtime/path metadata không yêu cầu byte-equal nhưng semantic content phải equal. Native artifacts và effective config hashes có provenance.
- Nếu replay sai, dừng transplant; không triển khai fusion để che sai khác.

Gate: nguồn được bảo toàn; launcher source chạy được trong root mới; output không viết qua baseline. Lần chuẩn bị plan hiện tại **chưa đạt U0 runtime gate**.

### U1 — Observation data + full lineage

- Thêm immutable ledger/sidecar refs; persist accepted P3 selected points hoặc pixel indices để reconstruct chính xác.
- Export M6 decisions vốn đã có; trace ON/OFF không đổi baseline output.
- Ghi lịch sử case frames 224–230 và các vật bàn/ghế/sofa lân cận. Chạy prefix đủ dài để giữ state; không start tại 224 rồi so như state đầy đủ.
- Schema validate frame/world units/IDs/hash; masks RGB-depth aligned, empty provider tách absent.
- P3 cluster cache keyed capture+mask+intrinsics+P3 config+code hash.

Gate: replay cùng observations deterministic; no GT leakage; không phải chỉ first/last lineage.

### U2 — Appearance extraction tối thiểu và parity microtests

- Kiểm kê feature env/weights, đề xuất tối thiểu; CPU microbenchmark trước, không tự tải model lớn.
- Chọn encoder/checkpoint chính xác; crop policy cố định. Upstream bbox-padding và masked crop là hai variants khác nhau, không đổi âm thầm.
- Cache features theo crop/model hash. Batched inference và missing-feature policy rõ ràng; không default vector zeros rồi gọi fusion thành công.
- Tách pure geometry/cosine/fusion helpers khỏi imports nặng; ghi source path/commit/license và modifications ledger.
- Microfixtures so công thức spatial/cosine/feature average với pinned source. Nếu upstream không chạy do deps, ghi chưa direct-source parity, không tự gọi parity.

Gate: finite normalized vectors, repeatability trong tolerance đã khóa, đo runtime/memory. CLIP embeddings giúp matching nhưng chưa là class correctness proof.

### U3 — Fusion association trong sidecar

- Implement backend tách source/fusion, geometry-only ablation và geometry+appearance ablation.
- Cannot-link, duplicate-mask handling, deterministic assignment, tentative tracks, score ledger.
- Test hai vật gần nhau, cùng class; một vật đổi class; view không overlap; mask lớn/nhỏ; object occluded; empty frame; order/tie handling.
- Không xóa node/GT để làm đẹp metric; defer là output hợp lệ.

Gate: report pairwise association precision/recall trên GT subset confirmed, fragmentation và false merge. Fix case bàn đơn lẻ chưa đủ promote.

### U4 — Semantic fusion/confirmation offline

- Thêm evidence bins, initial/resolved class, existence/class status, class-switch/retraction.
- Kiểm tra cùng detection duplicate hoặc frame lặp 20 lần không tự đạt 20 independent views.
- Test class đúng ban đầu rồi sai sau; sai ban đầu rồi đúng sau; hai class hòa; tất cả views sai; một vật chỉ thấy một lần.
- Trên fixed partitions, so source labels / tentative-gating-only / association+fusion.

Gate: class correction và regressions có GT/evidence, coverage/delay được báo. Không chọn parameters chỉ để chiếc bàn cụ thể được sửa.

### U5 — Integrate builder và semantic graph

- Define protocol object backend `.update/.snapshot` + lifecycle events, adapter sang M9; source strategy giữ nguyên.
- Canonical aliases/retirement, node removal và edge reconciliation thật; update class/geometry gây recompute membership/rules.
- M9 failure giữ graph committed snapshot, backend có thể mutated → failed/replay-required như contract cũ; không hứa rollback nếu chưa có.
- Fixed M5 partition control trước; integrated M8 sau. Check M2–M5 grids/polygons unchanged trên identical inputs khi refinement off.
- Run 29/280 frames bằng cache, fresh-process repeatability, snapshot/delta replay, no dangling/self/conflicting owner edges.

Gate: full regression bản mới + report theo tầng. Bản cũ không bị thay executable/code/config.

### U6 — Đánh giá độc lập và chọn profile object/semantic

- Chốt confirmed category mapping, supported vocabulary, GT visible masks, room-function/assignment review độc lập.
- `apt_0` và HM3D 00802 đã dùng nhiều lần để phát triển: **không gọi held-out**.
- `apt_1/apt_2` chỉ kiểm tra nội thất/độ ổn định cùng họ kiến trúc; không là bằng chứng generalization kiến trúc mạnh.
- Chọn ít nhất 2 scene HM3D mới dựa trên kiến trúc/annotation readiness, không theo điểm. Candidates trong gói đã tải: 00800/00803/00808, kiểm kê lại trước chọn.
- Khóa sensor/FOV, route budget và observation protocol; paired comparison cùng capture. Không dùng 600×340 vs 320×240 làm fusion ablation vì FOV/aspect có thể khác.
- Chấm online tại thời điểm t chỉ bằng observations ≤t; offline all-views final output báo riêng.

Gate: report per-scene, per-object/class, uncertainty/CI nếu đủ sample; chưa đủ independent scenes thì chỉ technical/development evidence.

### U7 — Room boundary refinement: nhánh riêng có thể fail

Mục tiêu trực tiếp cải thiện boundary không thể thay bằng “bed đổi thành table”. Chỉ bắt đầu khi U0/U1 và GT room protocol đủ; không bắt buộc chờ nhãn semantic hoàn hảo để audit geometry.

U7a, structural-only control:

- Audit M3 occupied/free/unknown và M4/M5/M8 history để xác định lỗi thật: coverage thiếu, projection, over-split, under-split, promotion hay stale.
- Định nghĩa partition trên ROI đã quan sát; phân biệt structural free-space của M3 với toàn sàn GT. Report cả full ROI và observed-support, không chọn miền đẹp hơn.
- Dùng M5 polygons làm seeds. Đề xuất candidate merge chỉ cho vùng kề có separator yếu và temporal evidence ổn định; cấm nối xuyên occupied wall hoặc unknown gap chưa quan sát.
- Đề xuất candidate split chỉ khi có structural bottleneck/doorway evidence từ occupancy qua nhiều observations. Không split bằng số functional labels, furniture count hay số GT rooms.
- Log candidate geometry, support cells, boundary cost, accept/reject; mỗi thay đổi kiểm connectedness, no overlap, no unsupported expansion, topology validity và stable ID mapping.
- Fallback giữ raw M5 khi evidence yếu. Giữ holes/outer-ring limitations explicit; không dùng convex hull để lấp tường/unknown.

U7b, object-assisted control:

- Chỉ đưa confirmed object evidence làm **soft secondary evidence** cho candidate đã có cơ sở geometry.
- Object footprint/semantic conflict có thể cảnh báo boundary; không tự vẽ tường, không bắt mọi phòng thuần một chức năng.
- Không dùng predicted room label làm GT, không feedback room label → object class → room split trong cùng vòng tự xác nhận.
- So structural-only vs structural+object ở cùng input và GT. Nếu geometry-first đã tốt mà semantic làm xấu, reject semantic term.

Chi tiết optimizer/cost/threshold sẽ chốt bằng một design note **trước implementation** sau audit. Plan này không giả rằng một energy hay heuristic đã được chứng minh hiệu quả.

Gate: Room matched IoU/boundary F1/split-merge/adjacency theo §10 trên development và independent scenes. Nếu không đạt: giữ DuDe source, báo fusion chỉ cải thiện objects/labels. Không ép claim “phân phòng tốt hơn đáng kể”.

### U8 — Launcher, profiling và handoff cuối

- Giữ source default cho đến khi profile được promote có bằng chứng.
- Short terminal tables: objects confirmed/tentative/conflicted, class corrections/harmful switches, merge events, room boundaries vs labels, runtime.
- Verification và evaluation là bước riêng/optional nhưng manifest ghi chính xác chạy hay chưa; không báo fresh_replay_equal=true khi không chạy.
- Chạy source/fusion/refined cùng captured run bằng cache, không capture/YOLO lại mỗi ablation.
- Hoàn thiện QUICKSTART một trang, lệnh register scene+route/profile, open evidence và compare reports.

## 8. Launcher và lệnh hiện có

Lệnh đã kiểm tra bằng `help` và CLI trong workspace; cú pháp profile là `--profile`, và profile mặc định vẫn là `source`.

Chạy full pipeline với source:

```bash
./run_scene.sh run hm3d_00802 --route main_320 --profile source
```

So source với candidate trên cùng full capture/P2/P3 run. Launcher tái dùng cache perception và tạo/reuse native replay riêng cho từng profile:

```bash
./run_scene.sh compare-profiles <FULL_RUN_ID> --candidate geometry-only-dedup-v2
```

Fixed-assignment object geometry fusion có lệnh launcher mỏng; xem [Object Fusion Quickstart](OBJECT_FUSION_QUICKSTART.vi.md). Lệnh kiểm tra và chạy:

```bash
./run_scene.sh fuse-objects <FULL_RUN_ID> --check
./run_scene.sh fuse-objects <FULL_RUN_ID>
./run_scene.sh open <FUSION_RESULT_ID> fusion
```

Wrapper đọc lineage của chính input run và từ chối nếu lineage thiếu. `--check` chỉ đọc. Nếu thiếu selected points, P3 dựng sidecar từ RGB-D/P2 cache, không capture hoặc chạy detector; nếu Detection3D khác cache P3 chuẩn thì dừng trước fusion. Không suy assignments từ endpoint của graph.

Lệnh source/comparison đã xác minh cú pháp bằng `python3 scripts/scene_launcher.py help run`, `help compare-profiles`, và `help open`. Không dùng các lệnh lịch sử `replay`, `compare`, `fusion_semantic` nêu trong bản plan ban đầu; launcher hiện dùng `replay-cached`, `compare-profiles`, và các profile `source|geometry-only|geometry-only-dedup-v2`.

`register` chỉ đăng ký alias/route paths; không tự tạo route hợp lệ từ GLB hoặc xác nhận navigability. Run artifacts luôn có ID riêng, giữ input cache nguyên trạng; manifest ghi effective config, hashes và stages đã chạy.

## 9. Thử nghiệm và đánh giá

### Ablation tối thiểu

| Profile | Association | Class policy | Partition |
|---|---|---|---|
| B0 source | OccuSG | first observation | DuDe/M5 baseline |
| B1 confirmation-only | source | eligibility gate only | fixed baseline |
| B2 geometry-fusion | geometry cross-class | view-capped evidence | fixed baseline |
| B3 fusion-semantic | geometry+image feature | view-capped evidence | fixed baseline |
| B4 integrated | B3 + optional bounded geometry fusion | như B3 | M8 integrated, M4/M5 giữ nguyên |
| B5 structural-refined | B4 | như B4 | U7a |
| B6 object-assisted-refined | B4 | như B4 | U7b |

Không nhất thiết chạy full 280 frames mọi variant ngay. Dùng microfixtures, short prefix/subsequence với state replay đúng rồi mới full selected profiles. Phải có ít nhất B0 vs B3 và B5 vs B6 để tách attribution trước claim tương ứng.

### Metrics cần báo và mẫu số

- 2D: confirmed-vocabulary precision/recall/F1 và mask IoU; diagnostic class-agnostic riêng. Giữ detections cố định thì không claim sửa chất lượng YOLO.
- Unique-instance recall: unique eligible visible GT instances trên ROI/views định nghĩa trước, không tổng frame observations.
- Object class: accuracy/F1 trên matched tracks; false confirmed objects, class correction, harmful switch; confirmed coverage và time-to-confirm. Room-derived guesses không là GT.
- Association: observation→GT lineage, fragmentation/false merge; pairwise precision/recall nếu protocol phù hợp; incomplete lineage → null. Không chạy GT association trong predictor.
- Geometry: visible-surface consistency/depth reprojection error; object GT origin/center không mặc định bằng visible surface AABB center.
- Membership: independent object→room assignments và ambiguous-boundary exclusion; stable != correct.
- Semantic room: functional-label accuracy/macro-F1 trên confirmed labels, prediction coverage và unknown/ambiguous rate. Không coi spatial room GT là functional GT.
- Room boundaries: Hungarian matched IoU với unmatched GT tính 0, precision/recall of rooms theo threshold khóa; split/merge, boundary F1 có tolerance mét, adjacency P/R khi doorway GT đủ. Union IoU phụ, không thay partition quality.
- Runtime: capture/features/P3/M2/M3/M4/M5/M6/M8/serialization/eval, first pass vs verification, median/p95, peak RSS, point counts và cache hit rates.
- Statistical unit: scene/physical instance hoặc view group, không bootstrap 280 correlated frames như 280 trials độc lập.

## 10. Gate “đáng kể” — đề xuất đăng ký trước, không phải kết quả

Trước tuning candidate, freeze protocol/config và ghi ngưỡng promotion. Đề xuất khởi đầu để người dùng duyệt:

- Object/class: giảm ≥25% lỗi class trên matched confirmed tracks so source, đồng thời unique-instance recall không giảm quá 5 điểm phần trăm và false-merge rate không tăng quá 2 điểm phần trăm (báo cả số tuyệt đối).
- Room semantics: tăng ≥10 điểm phần trăm accuracy trên cùng confirmed GT, prediction coverage không giảm quá 5 điểm phần trăm. Sample quá nhỏ thì chỉ báo exploratory.
- Room boundary: tăng ≥0.10 absolute macro matched-IoU trung bình per-scene, không scene nào giảm quá 0.03; boundary F1 không giảm và tổng split/merge không tăng. Chỉ xét cùng protocol/ROI/resolution.
- Safety: no GT leakage, no dangling owner edges, deterministic causal replay trong tolerance đã đăng ký, immutable source baseline.

Ngưỡng trên là engineering targets chứ không phải tiêu chuẩn cộng đồng. Không sửa sau khi xem holdout để candidate “pass”. Muốn claim tổng quát cần nhiều kiến trúc độc lập hơn 2 scene; nếu CI chưa đủ hoặc chỉ có apt_0 GT, không gọi statistically significant/generalization.

Nếu B3 đạt object/semantic nhưng B6 fail boundary, promote B3 có điều kiện và giữ partition source. Đó là kết quả hợp lệ, không giấu failure.

## 11. Performance và dependency gate

- Run dense reported P3 ~1699s, replay ~3307s gồm verification; đây là baseline chi phí cần đo lại có instrumentation, không là ngân sách realtime.
- Trước full feature extraction, benchmark 10–20 crops/frames và forecast whole run; cache/lazy feature extraction, spatial gating, bounded object cloud.
- Không gọi all-pairs point distances cho mọi track nếu có spatial index; không giữ mọi raw cloud trong RAM.
- Lưu masks/depth gốc như input immutable, lưu feature/cloud cache mới dưới root mới.
- Profiling không cộng trùng nested timers. Serialization/PNG/eval không tính là online association latency.
- Không giảm frame/P3 min_points hoặc đổi resolution cùng ablation object fusion; nếu tối ưu đó cần profile riêng.
- Không hứa 10 Hz; online safety/navigation nằm ngoài nâng cấp này.

## 12. Kiểm thử bắt buộc

1. Baseline source replay không đổi sau instrumentation; trace ON/OFF.
2. Full lineage và absence/empty batch; malformed batch không mutate state.
3. Same object different class; two adjacent different objects; repeated identical frames; occluded partial view; inconsistent depths; zero/NaN feature reject.
4. Confirmation/demotion/class revision rút đúng room scores, không cộng lại qua replay/flush.
5. Merge canonical aliases, tombstone/removal và membership edges; no double vote.
6. Equal score deterministic tie/defer; masks ordering policy documented; multiple observations cùng frame không tăng independent view count.
7. Feature cache invalidation theo model/crop/mask/pose; no mixed embedding space.
8. Stale regions, room reassign, floor/layer separation, timestamp backwards, close/flush và failed/replay-required.
9. GT assets chỉ mở trong evaluator: kiểm import/data flow, không truyền GT category/instance/room polygons sang predictor.
10. Run từ root mới không ghi đường dẫn baseline; tests/imports resolve bản seed mới, không import nhầm package baseline.

## 13. License, reproducibility, phạm vi hoàn thành

ConceptGraphs clone có MIT LICENSE; giữ copyright/notice khi port. Dependencies/models/datasets không tự được bao phủ bởi MIT. Giữ giới hạn hiện tại của DuDe/mathtool/FIST/CGAL và YOLO weights; không tự suy ra quyền cloud/distribution. Lượt này không mở rộng quyền sử dụng, không upload.

Mỗi code lấy từ upstream ghi file/symbol/commit/hash, adaptation và parity scope trong ledger. Không để `ref_*` là nơi sửa runtime.

Hoàn tất nâng cấp không chỉ là tests pass: phải có source-vs-fusion reports, provenance, GT scope, UX ngắn, và outcome trung thực từng tầng. Nếu điều kiện cải thiện boundaries chưa đạt, ghi rõ chưa đạt thay vì đổi tên object semantics thành room segmentation accuracy.

## 14. Thứ tự giao việc cho AI

1. Giao U0 + U1 trước, dừng ở baseline smoke và ledger case bàn.
2. Duyệt encoder/dependencies rồi U2–U4, offline sidecar và regression.
3. Khi có lợi ích object, U5–U6 tích hợp và kiểm scene độc lập.
4. U7a/U7b chỉ khi GT boundaries và source diagnostics đủ; hoàn thiện U8.

Không giao “làm hết để chính xác hơn” trong một lượt không có gates. Đọc `NEXT_AGENT_TASK.vi.md` để bắt đầu từ state thực tế hiện nay.
