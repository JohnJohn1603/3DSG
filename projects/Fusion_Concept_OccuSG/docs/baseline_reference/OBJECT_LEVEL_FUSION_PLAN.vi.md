# Kế hoạch Object-Level Fusion — Fusion ConceptGraphs × OccuSG

Ngày: 2026-10-11. Trạng thái: đề xuất triển khai, chưa nghiệm thu.

Workspace đích: `/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG`.

## 1. Mục tiêu và ranh giới

Sau khi association xác định object ID, hợp nhất evidence RGB-D qua nhiều view thành persistent object có hình học, appearance và semantic evidence tốt hơn. Không viết lại mapping/segmentation hoặc association. Không hứa cải thiện room boundaries chỉ nhờ object fusion.

Mốc đầu tiên: có thể xem một object, các observation nguồn và cloud tích lũy theo thời gian; so với geometry backend hiện tại trên cùng assignments.

Phân biệt ba mức nghiệm thu:

- Technical: contract, causal update, replay, serialization và integration đúng.
- Development quality: có lợi ích đo được với trade-off công bố trên dữ liệu đã dùng phát triển.
- Promotion: cần đánh giá độc lập; không tự thay default khi technical tests pass.

Giữ source/default và baseline `Rebuild_3DScene_OccuSG` nguyên vẹn. Geometry-only/dedup-v2 vẫn experimental; U4-OV diagnostics-only. Không chờ semantic classifier hoàn hảo mới nghiệm thu geometry fusion.

## 2. Căn cứ code đã kiểm tra

Reference local: `ref_ConceptGraph/concept-graphs`, commit `93277a02bd89171f8121e84203121cf7af9ebb5d` tại thời điểm khảo sát. Agent triển khai phải xác minh lại commit/worktree.

Các điểm đọc:

- `conceptgraph/slam/utils.py::merge_obj2_into_obj1`: nối cloud, process cloud, tính lại bounding box, tổng hợp normalized CLIP/text features theo số detections và tích lũy metadata.
- `process_pcd`: voxel downsampling, DBSCAN tùy chọn.
- `denoise_objects`, `filter_objects`, `merge_objects`: các bước postprocessing riêng, không đồng nhất với per-object fusion.
- `conceptgraph/slam/mapping.py::merge_detections_to_objects`: association quyết định object đích trước khi fusion.
- `conceptgraph/slam/cfslam_pipeline_batch.py`: lịch cập nhật/postprocessing.

Code dự án hiện tại:

- `src/standalone_sg/objects/u5_backends.py`: persistent IDs, center/extent hiện cập nhật `(old + new)/2`; raw class giữ riêng với semantic sidecar.
- `src/standalone_sg/contracts/objects.py::Detection3D`: chưa mang cloud đầy đủ.
- `scripts/project_detection_masks_to_3d.py`: đã có selected cluster points trong lúc P3 chạy; cần xuất sidecar ngay tại đây.
- M9/M10 đã có protocol backend, lifecycle và reconciliation; graph reload không đồng nghĩa khôi phục backend runtime state.

### Source fidelity và adaptation

Học ý tưởng cloud accumulation, bounds recomputation, feature aggregation, evidence lineage. Không tuyên bố toàn pipeline là bản tái hiện nguyên bản ConceptGraphs.

- Reference thêm nhiễu ngẫu nhiên khi tạo cloud: không tự mang sang pipeline deterministic.
- Không copy ngưỡng voxel/DBSCAN mà không xét sensor/scale.
- Không tạo text features từ class YOLO rồi coi là evidence độc lập.
- Weighted sum được lưu rồi normalize không hoàn toàn giống recurrence normalize-each-update của reference: phải đặt tên và kiểm chứng riêng.
- Không thêm cross-track merge/retirement hoặc appearance-driven association trong vòng fusion đầu.
- Khi tái sử dụng source, giữ attribution/license notices và ghi rõ phần port/adaptation.

## 3. Kiến trúc

```text
RGB-D + masks
  -> P3 + ObjectObservationEvidence sidecar
  -> dedup + association đã khóa
  -> ObjectFusionState.update(object_id, evidence)
       geometry / appearance / class evidence
  -> projection sang ObjectNode -> M9/M8 -> export/viewer
```

Trong phiên bản đầu, fusion không phản hồi geometry/features mới vào matcher. Duy trì association state riêng và fusion state riêng. Nếu về sau bật feedback, đó là thí nghiệm khác với config/version riêng.

GT, semantic instance IDs, room labels và annotation người dùng chỉ ở evaluator; predictor không được đọc chúng.

## 4. Contracts và layout đề xuất — OF0

Tên file là đề xuất, tái sử dụng module hiện có khi phù hợp, không tạo framework mới.

```text
src/standalone_sg/objects/fusion/
  contracts.py
  geometry.py
  state.py
  appearance.py          # giai đoạn sau
  serialization.py
configs/fusion/
  object_geometry_v1.json
scripts/
  run_object_fusion_offline.py
tests/
  test_object_fusion_*.py
artifacts/runs/<unique-run>/object_fusion/
  manifest.json
  observations/index.jsonl
  observations/<safe-id>.npz
  objects/<safe-object-id>/<revision>.npz
  object_timeline.jsonl
  report.vi.md
  index.html
```

### ObjectObservationEvidence

Schema version, observation/frame/sequence ID, timestamp, world frame/units, sensor pose, assigned object ID, canonical observation/alias information, points/pixel references + hashes, appearance reference, raw class/confidence, input/config provenance và quality status. Giữ association decision tách khỏi fusion decision.

### PersistentObjectState

Object ID, revision, first/last seen, applied observation IDs, voxel accumulator, derived bounds/center, geometry observation count, independent-view count, appearance accumulator (sau), class evidence/status, pending/rejected evidence với lý do.

Observation count khác independent-view count. Pending fusion khác association defer và khác duplicate ambiguity.

Gate OF0: khóa schema/config và ownership; xác minh trạng thái code thực tế; không thay output baseline.

## 5. Observation point-cloud sidecar — OF1

Với capture mới, xuất ngay tại P3 sau lựa chọn cluster:

- `xyz_W`, pixel indices tương ứng, RGB nếu có.
- Intrinsics, pose, resolution, depth interpretation và point-selection provenance.
- Dtype, units, count, bounds, checksums và config version.

Không projection/clustering lần hai để xuất cùng dữ liệu. Bảo đảm pixel-index bookkeeping chính xác qua valid-depth mask, stride, filter và cluster selection. Không thay Detection3D schema bắt buộc nếu sidecar reference đủ đáp ứng backward compatibility.

Cache cũ có thể dựng lại sidecar bằng code/profile đã khóa cho development. Ghi `reconstructed`; count/center parity không chứng minh exact selected-point parity. Không tái dựng cả dense cache trước thử nghiệm nhỏ.

Gate OF1:

- Bật/tắt xuất sidecar không đổi Detection3D và assignment outputs trên cùng inputs.
- Source pixel/depth/transform khớp XYZ trong tolerance đã ghi.
- Missing/mismatched sidecar phải báo lỗi hoặc unavailable có lý do; không silently dùng cloud khác.
- Tests gồm invalid depth, stride, transforms, empty/rejected detection, duplicate ID và cache hash mismatch.

## 6. Geometry fusion offline tối thiểu — OF2

Implement causal `update` và `snapshot` theo object ID có sẵn. Inputs là assignments/evidence đã khóa, không re-association.

### Thuật toán đầu tiên

- Deterministic world-frame voxel keys, xử lý tọa độ âm đúng.
- Mỗi voxel lưu sufficient statistics: tổng tọa độ/màu và support weights/counts.
- Point-cloud representative lấy từ voxel statistics; center/AABB được tính lại từ fused representation, ghi rõ định nghĩa.
- AABB trước; OBB và robust bounds là extension riêng.
- Không lặp lại kiểu `(old_center + new_center)/2` và gọi đó là cloud fusion.
- Chọn một voxel profile trước chấm; không sweep tham số để khớp các case review.

### Evidence admission

- Chỉ fusion observations có assignment hợp lệ.
- Alias được lưu provenance nhưng không apply lại geometry/feature/vote.
- Cùng observation ID apply lại phải idempotent; nếu ID giống nhưng payload hash khác thì fail rõ.
- Deferred/unassigned không được ép vào object.
- Exact-pose revisit không tăng independent-view support. Quy tắc weighting geometry đối với view lặp phải ghi rõ; không nhầm đó là tăng confidence độc lập.
- Duplicate-ambiguous không tự động là bad geometry; admission policy phải tách khỏi alias policy và giữ nguyên matcher.

### Determinism và tài nguyên

- Freeze causal order, deterministic tie-breaks/exports, tolerance số thực.
- Không hứa order invariance byte-for-byte nếu floating accumulation chưa bảo đảm.
- Ghi memory/point/voxel growth; giới hạn tài nguyên fail rõ hoặc có deterministic policy được công bố, không silently drop evidence.
- Raw evidence append-only cho phép dựng lại object khi cần; không cần triển khai undo/subtraction ngay.

Gate OF2: persistent fused clouds nhìn được qua các revision, repeatability, idempotency, reference statistics và input preservation pass. Đây là technical gate, chưa phải chứng minh geometry chính xác hơn.

## 7. Geometry quality/denoise — OF3

Đo geometry fusion OF2 trước khi thêm denoise. Sau đó tối đa một ablation riêng:

- Theo dõi expansion, contamination/outliers, support theo view.
- Có pending/rejected evidence và lý do; không xóa dữ liệu gốc.
- Không mặc định giữ largest component của fused cloud: các view bề mặt có thể rời nhưng cùng vật.
- R23 là regression mixed-mask đã biết, không hard-code observation ID hoặc dùng annotation để cắt cloud.
- Reference DBSCAN chỉ là phương án; kiểm tra dependency/runtime trước khi port.

Gate OF3: báo accuracy/completeness trade-off và failure cases; không gọi giảm point count là cải thiện.

## 8. Appearance aggregation — OF4

Tái sử dụng U2 OpenCLIP/checkpoint/preprocessing; không đổi encoder trong cùng thí nghiệm.

Tách chế độ reference-inspired recurrence theo detection counts khỏi adaptation weighted-sum accumulator. Nếu triển khai cả hai, ghi công thức/tolerance và source parity scope.

Lưu feature sums/weights, model fingerprint, evidence IDs, independent views và dispersion diagnostics. Không mix vectors từ encoder/preprocessing khác. Không để aliases hoặc repeated frames tăng semantic vote độc lập. Không dùng fused appearance thay matcher ở giai đoạn này.

Gate OF4: causal accumulation, normalized/finite features, serialization và deterministic repeatability pass; có so sánh với single-view. Cosine/dispersion không phải calibrated confidence hoặc identity GT.

## 9. Semantic fusion — OF5

Tái sử dụng U4/U4-OV thay vì tạo classifier thứ ba. Giữ raw labels, object evidence và resolved label/status độc lập.

So trên cùng assignments: raw/first observation; U4 voting; U4-OV dùng fused appearance. Class GT/mapping chỉ ở evaluator. Vocabulary expansion, threshold tuning hoặc prompt change là thí nghiệm riêng.

Unknown/conflicted hợp lệ; không fallback raw âm thầm rồi chấm là fusion thành công. Category không có trong candidates không tự xuất hiện từ accumulation.

Gate OF5: báo class correctness, coverage/abstention, harmful corrections và unsupported scope. Không chặn nghiệm thu geometry vì semantic labels chưa tốt.

## 10. Graph và launcher — OF6

CLI mong muốn, chưa khẳng định đã tồn tại:

```text
./run_scene.sh run <scene> --route <route> \
  --profile geometry-only-dedup-v2 --object-fusion geometry
./run_scene.sh replay-cached <run> \
  --profile geometry-only-dedup-v2 --object-fusion geometry
```

Giữ `--object-fusion off` default. Nối options vào cả run mới lẫn cached replay; không phụ thuộc scene-specific references.

- ObjectNode geometry projection lấy từ fusion state khi bật; matcher vẫn state riêng.
- M8 re-evaluate membership theo contract fresh/stale; M3/M4/M5 structural outputs giữ nguyên.
- Raw class và fused semantic state tách trường; room rules mặc định chưa dùng fused labels.
- Clouds/large arrays ngoài snapshot JSON; graph giữ versioned references/hashes/revisions.
- Snapshot export phải khai báo file dependencies và kiểm tra missing/corrupt assets.
- Graph reload khác fusion runtime resume. Nếu hỗ trợ resume, lưu đủ voxel/feature statistics, applied IDs, configs và revision để continuation khớp uninterrupted run; nếu không, ghi replay-required.
- Failure không được commit graph với fusion state không nhất quán. Bám failed/replay-required hiện có, không hứa backend rollback.

Gate OF6: tests/replay/reload/delta/invariants; fusion-off parity; cùng input/assignments ở paired run; launcher, results và object viewer đã chạy thực tế. Technical completion không tự promote.

## 11. Kế hoạch đánh giá

### Controls

So current center/extent update, geometry fusion và (sau) geometry+appearance trên cùng assignments. Association errors phải báo riêng; có thể dùng confirmed-identity groups như oracle diagnostic nhưng không gọi đó là end-to-end predictor result.

### Metrics

- Geometry: accuracy/completeness trên visible GT surfaces khi GT alignment/identity tin cậy; contamination/outlier rate; per-object bounds errors nếu reference phù hợp.
- Không so visible partial cloud với full-object GT mà không công bố visibility scope.
- Nếu chưa có GT: center/bounds stability, surface consistency, growth, evidence counts là diagnostics, không accuracy.
- Appearance/semantics: class correctness/coverage, harmful changes; single-view/fused comparison trên cùng eligible scope.
- Runtime/memory: update latency, object/voxel growth, serialized size; không tuyên bố realtime từ offline wall time.
- Graph: membership changes/provenance, integrity; room accuracy chỉ khi có GT riêng.

Không dùng chính accumulated input cloud làm GT rồi gọi là accuracy. Synthetic geometry fixtures dùng để kiểm tra toán học; datasets đã review là development. Mọi threshold sửa theo output phải được ghi nhận trước test độc lập mới.

## 12. Phạm vi implementation đợt đầu

**Chỉ OF0–OF2.** Không làm OF3–OF6 cùng lượt.

1. Kiểm kê trạng thái code, resource locks và reference; giữ thay đổi sẵn có của người dùng.
2. Chọn một capture ngắn khoảng 5–10 frames có nhiều observation đã assigned cùng vật; chọn theo availability/coverage trước khi chấm fusion.
3. Khóa assignments và geometry baseline; cảnh báo track mixed/uncertain để diễn giải, không tự sửa bằng GT.
4. Implement sidecar xuất trực tiếp từ P3 cho run tương lai; với cache ngắn hiện có dùng reconstruction được ghi rõ, hoặc chạy lại P3 vào output riêng để xuất evidence mới và so detection/assignment parity. Không overwrite P3 cache cũ.
5. Implement deterministic geometry accumulator và offline runner.
6. Xuất cloud từng object/revision, RGB/mask evidence, ảnh trước/sau cùng camera/scale và một index dễ mở. PLY/NPZ được phép; không thêm framework web.
7. Tests OF0–OF2, replay repeatability, memory/runtime và report limitations.

Tái sử dụng Habitat/perception/appearance env và native resources theo lock. Không tự cài Open3D hoặc package, tải model, clone reference mới; kiểm kê dependency trước, xin duyệt nếu bắt buộc. Có thể implement voxel accumulator bằng thư viện đã có thay vì kéo dependency nặng.

Ngân sách: dự toán trước; job trên 30 phút cần duyệt. Không dense native replay 280 frames, không capture scene mới để hoàn thành đợt này.

### Deliverables đợt đầu

- Contract/schema/config versioned và source/adaptation notes.
- Observation evidence sidecars và fusion state snapshots.
- Run manifest: inputs, assignments, point lineage, source/config hashes.
- Per-object timeline: input count, fused voxels, bounds, admission/rejection, timing.
- Viewer/export thấy được cloud tích lũy và nguồn evidence.
- Lệnh tái lập, targeted tests, regression phạm vi liên quan.
- Kết luận: technical achieved / blocked cụ thể; quality improved / unchanged / insufficient evidence, tách biệt.

### Điểm dừng

Kết thúc OF0–OF2 khi có prototype geometry fusion dùng được và có thể kiểm tra. Không kéo dài audit nếu không phục vụ quyết định; không tự tiến sang denoise, classifier hoặc feedback-to-association. Bước tiếp theo được chọn từ kết quả thực tế, không từ mong muốn phải dùng mọi thành phần ConceptGraphs.
