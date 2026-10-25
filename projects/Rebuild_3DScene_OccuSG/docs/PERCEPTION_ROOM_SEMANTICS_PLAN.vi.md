# Kế hoạch tích hợp perception và gán nhãn phòng sau checkpoint Habitat

Ngày lập: 2026-10-01. Trạng thái: kế hoạch triển khai, chưa phải báo cáo nghiệm thu.

Project đích: `/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG`.

Tài liệu này bổ sung `docs/PHASE1_PLAN.vi.md`, không thay thế các milestone M0–M13 và không tự đánh dấu chúng hoàn thành. Dùng mã P0–P7 để tránh nhập nhằng với milestone cũ. Không sửa code trong lượt lập kế hoạch này.

## 1. Đích đến và những phân biệt bắt buộc

Đích nghiệm thu là: capture RGB-D trong Habitat → dựng hình học tăng dần → nhận diện object thực từ RGB → định vị bằng depth → object tồn tại qua nhiều quan sát → liên kết object với room → suy luận nhãn ngữ nghĩa phòng → graph, trajectory và báo cáo có thể kiểm tra/replay.

Ba khái niệm khác nhau:

1. **Region:** vùng hình học do M4/M5 tạo và theo dõi.
2. **RoomNode:** thực thể graph được M8 tạo/neo vào region; `room_1` là tên định danh, không phải bedroom.
3. **Semantic room label:** nhãn suy luận như bedroom, kitchen, living_room; có thể unknown hoặc ambiguous.

### 1.1 Bám OccuSG ở đâu, mở rộng ở đâu?

- Source OccuSG đang pin dùng segmentation model qua `YOLOSegDetector`, mask + depth, clustering, lấy cluster lớn nhất và tâm AABB rồi biến đổi pose về frame đích. Đây là đường tham khảo cho perception hình học.
- M6 reference hiện có giữ geometry/class/score quan sát đầu tiên khi association match, chỉ cập nhật thời gian/provenance. Không tự biến nó thành weighted fusion hoặc multi-view confirmation.
- `RoomManager.next_room_name()` trong source tạo `room_<index>`. Không được nói rằng đường này đã tự phân loại living room/bedroom bằng LLM.
- Phase 1 hoãn semantic room naming. Phần gán nhãn ở P5–P6 là **mở rộng được yêu cầu trong tài liệu này**, không phải tuyên bố tái hiện chính xác OccuSG hoặc VL-Nav.
- Model khác, clustering Python hoặc điều chỉnh filter phải có bảng khác biệt với source; “cùng luồng xử lý” không có nghĩa “model/source parity”.

### 1.2 Hai đích bàn giao

- **G1 — Graph perception:** đủ bốn loại agent/object/navigation/room; ObjectNode xuất phát từ detection ảnh thật; có quan hệ membership và truy nguyên evidence.
- **G2 — Graph có ngữ nghĩa phòng:** G1 + RoomSemanticState và nhãn có căn cứ, có cơ chế từ chối kết luận, xuất/reload/replay được.

Không yêu cầu mọi object đều được nhận diện hoặc mọi room đều có tên chắc chắn. Không gọi graph này là tái dựng hoàn hảo toàn bộ môi trường.

## 2. Mốc xuất phát đã biết

- Webots capture/replay và core M1–M10 đã chạy local; giữ backend này làm regression.
- Checkpoint Habitat có 6 RGB-D frames từ ReplicaCAD stage-only, dùng pose GT; đã có kiểm chuẩn hình học và native graph replay. Đây chưa phải scene đầy đủ nội thất.
- Replay 276 packets Webots ổn định trong sáu lần kiểm tra cùng build/config; sai khác artifact cũ phù hợp với build/version drift, chưa có bằng chứng nondeterminism của build đã khóa.
- Có `Detection3D`, `DetectionBatch`, `CachedDetectionProvider` và `FramePacket.detections`; graph đã hỗ trợ ObjectNode. Capture Habitat được báo cáo hiện để detections absent.
- Phase 1 có boundary cho real provider nhưng không milestone bắt buộc detector thật; M13 chỉ là query API.

AI đang thực hiện Bước 2: full scene instance ReplicaCAD → route dài hơn → artifact/coverage → kiểm tra annotation → 2–3 biến thể nội thất → đánh giá robustness. Chưa coi nhiệm vụ đang chạy là hoàn thành.

## 3. Thứ tự triển khai từ bây giờ

```text
Bước 2 hiện tại: scene đầy đủ đầu tiên + capture/replay có kiểm chứng
    └─ P0: khóa capture và baseline
         └─ P1: chốt contracts/config/model
              └─ P2: detector 2D thật + cache
                   └─ P3: mask/depth → Detection3D
                        └─ P4: association + membership → G1
                             └─ P5: room labeler baseline
                                  └─ P6: tích hợp labels vào graph → G2
                                       └─ P7: đánh giá trên biến thể đã thu
```

Các biến thể ở bước 6–7 nhiệm vụ hiện tại có thể tiếp tục thu/evaluate riêng, không chặn P1 khi scene đầu tiên đã qua P0. Nếu AI kia đã làm xong toàn bộ nhiệm vụ hiện tại thì tái sử dụng captures, không chạy lại chỉ để đổi tên milestone.

Không yêu cầu room segmentation đạt một tỷ lệ accuracy chưa định nghĩa mới cho phép perception. Gate chuyển tiếp là: dữ liệu đủ, frame đúng, replay ổn định, lỗi hình học được ghi rõ. Nếu mapping lệch trục/scale hoặc sensor sai thì dừng để sửa; nếu segmentation còn chia thừa/gộp nhầm thì giữ làm hạn chế đo được.

## 4. Kiến trúc và các ranh giới dữ liệu

```text
Capture immutable: RGB, depth, K, sensor pose, base pose, timestamp
  ├─ M1 → M2 → M3 → M4/M5 → geometric regions
  └─ Detector2D → mask cache → ObjectGeometry → DetectionBatch
                                      ↓
                    FramePacket → M6/M7/M8/M9 → geometry/object graph
                                                   ↓
                       observed object evidence → RoomLabelProvider
                                                   ↓
                          validated semantic enrichment → M10 exports
```

- Habitat chỉ thuộc capture adapter; provider và core không import Habitat/Webots/ROS.
- Chạy inference offline rồi cache trước; chưa bắt buộc detector live hoặc realtime.
- Tách environment capture, inference và replay khi dependency/Python khác nhau. Không ép cài model vào environment native hiện có.
- Semantic enrichment không sửa M3 occupancy, M4 segmentation, room ID hoặc footprint để làm khớp tên phòng dự đoán.
- Dùng semantic instance masks/GT để đánh giá hoặc oracle đối chứng riêng. Không đưa danh sách toàn scene, hidden objects, GT class/center hay GT room label vào nhánh detector thật.
- Thêm detections có thể làm M8 promotion/membership thay đổi hợp lệ; không đòi graph cuối giống geometry-only. M2/M3 và raw M4/M5 vẫn phải không đổi khi cùng input/config, nếu không có feedback được thiết kế.

### 4.1 Contracts cần chốt

**Detection2D (mới):** scene/sequence/frame key, capture timestamp, detection ID ổn định trong cache, class raw/canonical, score raw, bbox, mask reference/encoding, original image shape, model fingerprint, preprocess/postprocess metadata. Mask phải trở về đúng hệ pixel ảnh gốc, không dùng mask letterbox trực tiếp.

**Detection3D (đã có):** dùng contract thực tế trong `contracts/objects.py`; `center_W`, `center_method`, class, score, extents, frame `world`, layer và provider metadata. Không đổi `confidence` thành xác suất calibrated. Phân biệt DetectionBatch.frame_id (capture key) với Detection3D.frame_id (coordinate frame).

**Object evidence ledger (mới):** frame/detection → persistent object ID hoặc suppression/rejection; camera pose; số điểm depth hợp lệ; geometry method; ảnh/mask reference; first/last observation; lý do lọc. Nếu M6 chưa trả association events, thêm output diagnostics tối thiểu mà không đổi matching; không ghép lineage bằng nearest lần thứ hai ở evaluator.

**RoomSemanticState (mới):** room ID, region/graph revision nguồn, label nullable, status `unknown|predicted|ambiguous|stale`, candidates/scores, score_kind, supporting object IDs và evidence hash, method/model/prompt/config fingerprint, computed timestamp, last evidence timestamp, stale reason. Không đưa score rule/LLM vào confidence xác suất khi chưa calibration.

### 4.2 Chính sách absent/error

- Provider không chạy/frame không được chọn: absent; không phải quan sát phủ định.
- Provider chạy nhưng không có detection: empty.
- Detector lỗi hoặc cache hỏng: ghi lỗi rõ, không giả thành empty; mặc định run không đạt, checkpoint partial có trạng thái riêng.
- Không tạo observation mới từ cache của frame trước dưới timestamp mới.
- Infer thưa được phép nếu lịch chọn frame được khóa từ đầu và lưu trong manifest; không chọn hậu nghiệm chỉ ảnh đẹp.

## 5. P0 — Khóa một scene có nội thất và baseline dùng chung

**Đầu vào:** kết quả nhiệm vụ Habitat đang chạy.

Việc cần làm:

1. Xác nhận dùng full scene instance qua dataset config, stage/object/articulated inventory và RGB/depth thực; không chỉ stage GLB.
2. Chọn route quan sát nhiều hướng, lối nối và tái quan sát ít nhất một khu vực. Giữ pose GT như hiện tại, không thêm SLAM.
3. Khóa checksum capture, config, native binaries, source commits, scene instance và phần mềm. Run ID mới, không ghi đè `latest` hoặc xóa artifacts cần so sánh.
4. Replay geometry-only và lưu M3 grid, M4/M5 regions, graph, trajectory, diagnostics.
5. Chọn development capture và holdout capture/variant từ đầu. Chốt scene đầu có đủ loại object quan sát được để thử perception; nếu không, bổ sung route có ghi rõ, không sửa capture cũ.
6. Kiểm kê GT sẵn có. Nếu không có room GT, tạo sidecar annotation thủ công độc lập với output DuDe, quy định corridor/open-plan/doorway trước khi chấm.

**Nghiệm thu:** dữ liệu aligned RGB-D/pose hợp lệ, full scene có nội thất, replay reload/delta pass, provenance đầy đủ. Không bắt buộc đúng số phòng kỳ vọng.

**Bàn giao:** capture manifest, geometry-only reference, annotation capability report và danh sách captures development/holdout.

## 6. P1 — Chốt model, vocabulary, config và giao diện

**Quyết định kiến trúc:** dùng một mô hình instance segmentation RGB cho bản đầu, xuất class + score + mask. Không bắt đầu bằng detector bbox-only rồi âm thầm xem cả bbox là vật thể.

Việc cần làm:

1. Audit model/class list/weights thực tế của source OccuSG và artifact sẵn có. Nếu tái sử dụng được model segmentation ONNX phù hợp thì ưu tiên adapter độc lập, không kéo ROS vào.
2. Nếu thiếu weights hoặc class coverage không phù hợp, chọn đúng một model instance segmentation có weights truy cập được, đủ nhóm nội thất cần thiết, phù hợp tài nguyên. Trước tải/cài, ghi bảng lựa chọn: model/version, classes hỗ trợ/thiếu, preprocessing, RAM/VRAM dự kiến, license code/weights, runtime và nguồn chính thức.
3. Không có model/version mặc định được coi là đã duyệt từ tài liệu này. Khóa model sau một smoke nhỏ; không mở cuộc benchmark hàng chục model. Không có class cần thiết thì báo unsupported, không chế tạo detection.
4. Vocabulary ban đầu lấy giao của class model với object quan sát được, ưu tiên nhóm phân biệt phòng như bed, sofa/couch, chair, table, toilet, sink, refrigerator, oven khi model hỗ trợ. Map synonyms về canonical class; không gộp các class khác nghĩa.
5. Strict YAML/JSON cho model path/hash, class map, threshold/NMS, image size, inference cadence, device, geometry filters/clustering, room-label thresholds. Giá trị số phải có nguồn hoặc ghi rõ giả định development, không code cứng rải rác.
6. Room-label ontology phiên bản đầu: `bedroom, living_room, kitchen, bathroom, dining_room, corridor, other`, cộng unknown/ambiguous ở status. Phải cho phép không kết luận; corridor không được suy ra chỉ vì thiếu object.

**Nghiệm thu:** decision record chốt một model, interfaces và config schema; smoke model thực trên vài ảnh P0; không import detector khi chạy geometry-only.

## 7. P2 — Detector thật trên RGB và cache tái lập

Việc cần làm:

- Chạy inference trên lịch frame đã chốt; cache masks lossless, bbox/class/score và errors.
- Kiểm tra resize/letterbox/unletterbox, RGB/BGR, mask threshold, mask/image dimensions, deterministic ordering và detection IDs.
- Xuất ảnh overlay mask/class/score cho cả frame tốt, empty và khó; không chỉ chọn ví dụ thành công.
- Cache key gồm capture/frame + weights/config/preprocess fingerprint. Khác key phải invalidation, không dùng nhầm cache.
- Ghi inference time, device và peak memory theo cách đo được. Nếu GPU inference không bitwise deterministic, lưu raw result làm đầu vào authoritative cho replay; không hứa fresh inference giống từng byte.

**Tests:** mask lệch kích thước, bbox sát mép, ảnh không vuông, empty/absent/error, cache mismatch, class không hỗ trợ.

**Nghiệm thu:** detection/mask từ RGB thực, model provenance đầy đủ; cached provider đọc lại đúng cùng evidence. Thất bại detector không bị che thành “không thấy đồ vật”.

## 8. P3 — Mask + depth thành observation 3D

Luồng reference-inspired:

1. Intersect mask với valid depth; loại NaN/Inf/ngoài range theo metadata. Không biến depth invalid thành free evidence.
2. Back-project bằng K và axial depth đã kiểm chuẩn, không dùng radial formula nhầm.
3. Lọc/cluster masked cloud; lấy cluster lớn nhất như hướng source, ghi tiêu chí tie-break. Nếu viết Python, kiểm tra thuật toán có cùng ý nghĩa Euclidean connectivity; DBSCAN không mặc nhiên tương đương PCL Euclidean clustering.
4. Tính tâm AABB trong camera/source frame rồi transform center sang canonical world, ghi `center_method`. Không gọi đây là centroid vật thể đầy đủ: nó là tâm bbox của phần bề mặt được nhìn thấy.
5. Box extents cần frame rõ ràng. AABB camera quay sang world không thể giữ nguyên extents rồi gọi là world AABB; nếu cần world AABB, transform corners/points và tính lại, ghi khác biệt với source. Không sửa nghĩa contract im lặng.
6. Phát Detection3D và diagnostics: raw/valid/cluster points, center, extents, rejection reasons, image/mask references.

**Tests:** plane/box analytic; sensor translation/rotation; ảnh off-axis; invalid depth; mask lẫn nền; hai depth clusters; ít điểm; occlusion; coordinate convention. Tolerance geometric fixture được khóa trước, tách khỏi sai số detector.

**Đối chứng tùy khả năng:** visible semantic mask + observed depth để cô lập lỗi detector. Không đưa GT center trực tiếp vào nhánh này. Nếu semantic render chưa xác minh, dùng annotation mask thủ công cho tập nhỏ.

**Nghiệm thu:** vị trí 3D có overlay theo world và reprojection center vào ảnh để kiểm tra, fixtures hình học pass; cached DetectionBatch vào đúng frame/time. Không lấy “có node” làm bằng chứng định vị đúng.

## 9. P4 — Graph có object thật và membership (G1)

Việc cần làm:

- Inject DetectionBatch theo frame vào runner Habitat hiện có; không đổi đường mapping.
- Giữ M6 reference association: radius/suppression/first-observation geometry theo config đã khóa. Lưu evidence ledger cho create/match/suppress.
- Cho M8 thực hiện membership/anchoring như core hiện có; audit center/footprint rule đang dùng, không tự thay luật chỉ để graph đẹp.
- Ghi object chưa có room, boundary ambiguous, stale region và stale membership provenance nếu có. Không ép nearest room ngoài contract.
- Kiểm tra revisit, hai object cùng class gần nhau, object sát doorway, một vật nhìn từ hai phía, region ID thay đổi/relink/prune.
- Xuất graph và ledger; query “object này ở đâu/thuộc room nào?” và “room này chứa những object nào?” từ snapshot reload.

**Nghiệm thu G1:** bốn loại node được kiểm chứng trên scene có quan sát tương ứng; có ít nhất một object thật được tái quan sát, kiểm tra association; không dangling edges; snapshot/delta replay pass; cùng cached detections cho kết quả ổn định theo phạm vi kiểm thử.

**Giới hạn bắt buộc báo:** first-view center bias, false merge trong radius M6, false split khi class thay đổi. Không sửa đồng thời centroid fusion, multi-view confirmation và segmentation trong lượt này. Nếu association khiến labeler không thể dùng, mở ablation riêng sau khi lưu reference G1.

## 10. P5 — Suy luận nhãn phòng từ object evidence

### 10.1 Bản đầu: rule-based minh bạch, không bắt buộc LLM

Đây là lựa chọn triển khai để đạt G2 có thể kiểm tra, không phải mô tả thuật toán room naming của OccuSG.

- Input chỉ gồm persistent objects thực sự thuộc room, canonical classes, evidence summary và trạng thái membership/region. Không lấy tên asset, scene config, toàn bộ hidden objects hoặc GT room label.
- Tập evidence dùng unique object IDs, không tăng phiếu vì robot nhìn cùng ghế 100 frame.
- Hạn chế dominant class: dùng presence hoặc capped count theo class. Không tính cả track cũ stale như evidence mới.
- Một baseline gợi ý: `S(r,label) = Σ_class weight[label,class] * min(object_count(r,class), count_cap[class])`. Weight, count_cap, support threshold và top-two margin nằm trong config, được chốt trên development, không coi S là xác suất.
- Nhãn cần minimum support và margin đủ so với ứng viên thứ hai. Không đủ → unknown; nhiều nhãn cạnh tranh → ambiguous. Open-plan living/dining có thể ambiguous; không ép tách room theo tên.
- Tránh rule “table → dining_room” hoặc “chair → living_room” đơn độc. Cần tổ hợp evidence phù hợp; unsupported ontology phải hiển thị rõ.
- Nếu dùng requirement nhiều viewpoint cho label confidence, đó chỉ là gate của labeler với actual observation provenance, không lén thêm confirmation vào M6 reference.

### 10.2 LLM là provider tùy chọn sau baseline

- Cùng input evidence và ontology; output JSON schema chứa label/candidates/supporting IDs/unknown reason.
- Pin model/prompt/options; validate output, cấm viện dẫn object ID không có trong input; cache bằng evidence+prompt+model hash.
- Text output không tự là confidence calibrated. Model đổi thì tách run; repeated fresh calls không mặc nhiên deterministic.
- Timeout/error không sửa label thành chắc chắn. Không tự gửi ảnh/dữ liệu ra API ngoài hoặc phát sinh phí nếu chưa được chấp thuận.
- G2 không bị chặn bởi API/LLM. Nếu người dùng muốn một phiên bản giống hướng LLM của VL-Nav, làm ablation rule-vs-LLM sau baseline, không tuyên bố paper parity khi chưa audit.

**Tests P5:** no objects; lặp cùng object; misleading chair/table; bed có evidence; kitchen/bathroom conflict; missing room; stale region; object chuyển membership; unsupported classes.

**Nghiệm thu:** có predicted label có thể giải thích trên evidence thực, và có unknown/ambiguous đúng chủ đích trên fixtures; không dùng số lượng room làm accuracy nhãn.

## 11. P6 — Tích hợp label vào graph/export/query (G2)

Việc cần làm:

- Thêm semantic enrichment stage có thể tắt, nằm sau geometry/object graph và trước public export/commit thích hợp; thiết kế rõ commit boundary, không sửa snapshot bất biến sau khi publish.
- Phase 1 dự kiến semantic_label nhưng RoomNode runtime hiện chưa có field này. Audit serializer/contracts trước khi thêm; dùng GraphNode attributes với validation hoặc schema mới có migration nếu thay đổi format không tương thích. Không giả field đã tồn tại chỉ vì plan có nhắc.
- Không đổi room ID/name `room_N` thành semantic label dùng làm key. Nhiều room có thể cùng label bedroom.
- Recompute khi evidence hash/membership thay đổi, không khi chỉ processing time thay đổi. Stale geometry không cấp evidence revision mới.
- Room prune/relink/split/merge làm vô hiệu evidence cũ theo ID+footprint revision; không sao chép nhãn vô điều kiện sang region mới. Giữ lịch sử lý do đổi label.
- Delta phải phản ánh thay đổi semantic fields; labeler failure có trạng thái rõ, không làm mất geometry snapshot đã hợp lệ. Replay label decisions từ cache được, không gọi LLM lại khi replay log.
- Viewer spatial: occupancy + room polygons + object markers + trajectory; mặc định ẩn nav labels/dense edges. Viewer quan hệ: room→objects với tên ngắn, chỉ current agent. Show predicted/unknown/ambiguous/stale; đừng tô nhãn sai thành GT.

**Tests:** disable semantics giữ behavior cũ; save/load/migration; semantic-only delta; error/retry; stale→fresh; prune/relink; cached-label replay; no mutation/dangling edges.

**Nghiệm thu G2:** final JSON và PNG/HTML tĩnh nếu có cho thấy object thật nằm trong room và room label có evidence; reload/query/delta khớp; geometry-only vẫn chạy không cần model dependency.

## 12. P7 — Đo chất lượng, kiểm tra biến thể và bàn giao

Tái sử dụng 2–3 biến thể nội thất từ nhiệm vụ Habitat hiện tại. Chúng kiểm tra robustness trên kiến trúc tương ứng, **không** chứng minh generalization sang nhiều kiến trúc.

### 12.1 Các nhánh so sánh

1. Geometry-only baseline (detections absent).
2. Real detector cache + M6 reference + room membership.
3. Nhánh 2 + rule room labeler.
4. Optional visible-mask oracle cùng depth để cô lập detector.
5. Optional LLM room labeler với cùng graph/evidence như nhánh 3.

Khóa sensor/profile/observation budget, vocabulary, model, geometry filter và rules trước holdout. Không tuning từng scene. Một change chỉ được gán công cải thiện qua comparison cùng input hoặc nêu rõ input khác.

### 12.2 Metrics và giới hạn

| Tầng | Đo | Khi thiếu GT |
|---|---|---|
| Detector 2D | precision/recall và mask IoU trên mẫu annotate độc lập | qualitative + thống kê coverage, không báo accuracy |
| Geometry 3D | sai số center theo định nghĩa GT, valid/reject ratio | reprojection/fixture error, không coi mesh origin là GT center |
| Association | false merge/split, duplicate tracks, revisit ID consistency | kiểm tra subset thủ công, ghi kích thước mẫu |
| Membership | tỷ lệ đúng room trên objects quan sát có GT, unassigned/ambiguous rate | audit sidecar riêng, không gán nearest predicted room làm GT |
| Room geometry | matching IoU, over/under segmentation, boundary/adjacency | diagnostics coverage/stability; không suy từ room count |
| Room label | confusion matrix, accuracy/F1 trên matched annotated rooms; label coverage/abstention | đánh giá định tính, không accuracy |
| Runtime | inference/cache/replay/labeling riêng; latency, peak memory và cache size | báo môi trường và cách lấy mẫu |

Quy định room matching trước khi chấm, unmatched GT/predictions phải báo riêng. Label accuracy chỉ trên matched rooms cần đi kèm matching recall và tỷ lệ abstention để tránh giấu lỗi segmentation. Full-room GT và observed-area evaluation phải tách, không phạt unknown chưa quan sát như sai class.

Numerical quality targets: chốt sau P0 annotation feasibility và trước holdout trong `evaluation_protocol`; không tự bịa mục tiêu 90% hoặc chọn threshold sau khi thấy kết quả. Nghiệm thu kỹ thuật G1/G2 và đạt mục tiêu khoa học là hai trạng thái riêng.

### 12.3 Artifact tối thiểu cho mỗi run

```text
<unique_run_id>/
  manifest.json                 # capture/build/model/config fingerprints
  detection_2d_index.json       # masks/images nằm trong cache có checksum
  detection_3d.jsonl
  object_evidence.jsonl
  room_semantics.jsonl          # decision history, evidence hash, provenance
  snapshot.json
  delta_log.json
  trajectory.csv
  metrics.json                  # unavailable metric = null + reason
  diagnostics.json
  spatial_semantic_map.png
  room_object_graph.png
  examples/                    # successes và failures
```

Không sao chép raw capture lớn vào mọi run nếu có immutable reference + checksum. Không xóa baseline khi thiếu dung lượng mà chưa có bản giữ cần thiết.

## 13. Cấu trúc module đề xuất, không phải file đã tồn tại

```text
src/standalone_sg/
  adapters/semantic/            # detector optional, cache I/O
  perception/                  # masks, depth geometry, evidence diagnostics
  semantics/                   # ontology, rule labeler, optional LLM provider
  contracts/                   # Detection2D / semantic state nếu cần
configs/perception/            # locked model, class map, geometry profiles
configs/semantics/             # room ontology / weights / thresholds
scripts/                       # infer cache → lift 3D → semantic replay/evaluate
tests/                         # isolated fixtures + same-packet integration
docs/                          # per-P status, decisions, evaluation protocol
```

Tái sử dụng `contracts/objects.py`, `adapters/semantic/cached.py`, `FramePacket.detections`, M6 association, M8 membership, M9/M10. Không viết lại SceneGraphBuilder thành monolith. Tên file mới được AI triển khai chốt sau kiểm tra code thực.

## 14. Các gate không được làm chậm sai mục tiêu

- Không đợi Kaggle/cloud, SLAM, robot physics Habitat hoặc realtime mới làm offline perception.
- Không đợi hoàn thiện M13 mới tạo object graph; có thể cung cấp query tối thiểu và kiểm tra tương thích M13 sau.
- Không đổi DuDe/MapConverter, centroid fusion và labeler trong cùng experiment.
- Model/license/dependency chưa đủ thì báo blocker cụ thể của model, tiếp tục contracts/fixtures; không gọi synthetic detection là detector thật đã hoàn thành.
- Không dùng lời gọi “semantic graph” để khẳng định room labels đúng; unknown là đầu ra hợp lệ nhưng mọi room đều unknown chưa chứng minh labeler hữu ích.
- Khả năng dùng weights/dependencies local không tự chứng minh quyền cloud/distribution. Rà soát trước hành động tương ứng, không tự upload.

## 15. Checklist bàn giao cuối

- [ ] P0: full furnished scene, capture immutable, geometry reference và splits.
- [ ] P1: một model/weights/config/vocabulary được khóa; optional dependency isolation.
- [ ] P2: masks/classes/scores thực từ RGB, cache và failure handling.
- [ ] P3: Detection3D từ observed depth, geometric fixtures và coordinate checks.
- [ ] P4/G1: object persistence + room membership + evidence ledger, graph replay.
- [ ] P5: room labels có evidence, unknown/ambiguous/stale, rules versioned.
- [ ] P6/G2: semantic serialization/delta/query/visualization; identity không lệ thuộc tên phòng.
- [ ] P7: development/holdout evaluation, failures và capability-limited metrics.
- [ ] Regression core/Webots/Habitat vẫn pass; báo rõ tests nào chưa chạy.

Mỗi P kết thúc bằng báo cáo ngắn: files thay đổi, lệnh tái lập, tests thực chạy, artifacts/run IDs, đạt/chưa đạt và next gate. Không chỉ báo số tests.

## 16. Chỉ thị giao cho AI triển khai

> Đọc kế hoạch này cùng PHASE1_PLAN và status hiện tại. Giữ nhiệm vụ Habitat đang chạy; kiểm tra P0 từ artifacts mới nhất, không giả định đã xong full furnished scene. Sau P0, triển khai P1–P4 để có G1 trước; tiếp đó P5–P6 để có G2, cuối cùng P7 trên biến thể đã thu. Không cần chờ mọi biến thể mới bắt đầu P1. Giữ reference M1–M8, source build và capture; chỉ mở rộng provider/evidence/semantic layer theo contracts. Tích hợp detector thực offline có cache, không thay bằng GT. Gán nhãn phòng là extension riêng, không gọi room_N là bedroom. Chốt và báo decision model/config trước inference toàn bộ. Mỗi milestone có kiểm chứng và bàn giao; không mở rộng sang VLFM/DTPG/SLAM hoặc tuning riêng từng scene.

## 17. Căn cứ local đã đối chiếu khi lập kế hoạch

- `docs/PHASE1_PLAN.vi.md`: mục tiêu/hoãn, S11 perception, contracts, M6, M11–M13.
- `docs/HABITAT_REPLICA_CAD_STATUS.vi.md`: checkpoint stage-only; calibration và capture/replay, chưa detector.
- `src/standalone_sg/contracts/objects.py`: Detection3D/DetectionBatch hiện hành.
- `src/standalone_sg/adapters/semantic/cached.py`: frame/time cache boundary.
- `src/standalone_sg/pipeline/contracts.py`: FramePacket và immutable GraphNode attributes.
- `src/standalone_sg/rooms/anchoring.py`: RoomNode hiện hành chưa có semantic classification field.
- `ref_OccuSG/OccuSG/src/semantic_perception/src/semantic_node.cpp`: segmentation model, largest-cluster/AABB/transform path.
- `ref_OccuSG/OccuSG/src/scene_graph_ros/scene_graph_ros/managers/room_manager.py`: next_room_name và room creation.

Đây là kế hoạch dựa trên các file đã đọc, không phải kiểm toán toàn repo hoặc xác nhận các tác vụ AI khác đang chạy. Các checkpoint mới phải được kiểm tra khi bắt đầu P0.
