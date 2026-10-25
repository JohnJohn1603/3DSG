# Báo cáo U6 — HM3D 00808

Ngày chạy: 2026-10-10. Đây là đánh giá development trên một route ngắn của scene 00808, không phải tuyên bố generalization hoặc promote. Dense replay 00802 vẫn pending theo quyết định người dùng; không chạy trong lượt này.

## Khóa và đầu vào

- Scene: `00808-y9hTuugGdiq`, HM3D v0.2 minival; dùng assets đã có, không tải dataset/model mới.
- Route/protocol khóa trước P2/P3/predictor: [route lock](../locks/u6_hm3d_00808_quality_v1_1.json), [protocol v1.1](U6_HM3D_00808_PROTOCOL_v1.1.vi.md), [route config](../configs/habitat/hm3d_minival_v02_00808_u6_quality_v1_640.json).
- Route có 5 frame: ba vị trí nhìn khu seating/table, một detour kitchen, rồi exact-pose revisit frame 0. 5 là route tối thiểu đã chọn; 48 là trần, không phải mục tiêu. Tất cả pose navigable, các đoạn liên tiếp connected. Sensor giữ 640×480, HFOV 90°, fx=fy=320 px, depth axial 0.1–10 m.
- Semantic render chỉ dùng evaluator, chạy tại đúng poses. RGB/depth đối chứng sai khác 0; semantic IDs đều có descriptor. Không đưa semantic maps/GT/room labels vào predictor.
- Không tuning model, vocabulary, prompts, thresholds, P3, U3 hoặc U4-OV sau khi xem output. Appearance mode không được dùng làm mode chính.

## Coverage của tập chấm

| Mức | Số lượng | Ghi chú |
|---|---:|---|
| Frame | 5 | Frame 3 detour không có detector output |
| GT object-observations đủ điều kiện | 53 | Toàn bộ non-architectural evaluator instances có ≥32 px; mẫu số độc lập predictor |
| Evaluator instance IDs | 24 | HM3D IDs; chưa được người dùng xác nhận physical identity |
| P2 detections / P3 accepted / rejected | 18 / 18 / 0 | P3 acceptance không đồng nghĩa đúng vật |
| P3 accepted khớp GT mask IoU≥0.50 | 14 | 8 evaluator IDs; 4 accepted detections không khớp GT |
| GT observations không được detection khớp | 39/53 | Detection/route coverage thấp; không coi là association đúng |
| Candidate-vocabulary support | 20/53 | Exact category token nằm trong vocabulary; 33 unsupported |

Matching là Hungarian một-một, class agnostic, theo frame; GT visible area tối thiểu 32 px. Association pairwise chỉ tính unordered cross-frame pairs mà cả hai detection đều P3 accepted và mask matched; 61 cặp đủ điều kiện, gồm 9 positive identity pairs theo semantic IDs. Same-frame collisions báo riêng. Exact-pose revisit không được tính là independent view cho U4-OV.

### Association

| Mode | TP/FP/FN | Precision | Recall | False-merge tracks | Fragmented evaluator IDs | Same-frame collisions |
|---|---:|---:|---:|---:|---:|---:|
| Source raw | 6/0/3 | 1.00 | 0.667 | 0 | 1 | 1 |
| Source dedup | 6/0/3 | 1.00 | 0.667 | 0 | 1 | 1 |
| Geometry-only | 9/0/0 | 1.00 | 1.00 | 0 | 0 | 0 |

Trên lát cắt nhỏ đã matched, geometry-only nối đúng thêm 3 positive pairs so với source. Source fragmentation là evaluator ID `573`: các observation `hm3d-00808-u6q-v1-0000:det-0001`, `0001:det-0003`, `0002:det-0003`, `0004:det-0001` bị chia vào hai source tracks. Source cũng gán hai detections cùng frame 2 vào một track: `0002:det-0004` và `0002:det-0005`. Đây là **same-frame collision**, chưa tính false merge vì chưa có human identity review. Hungarian ledger chính thức chỉ gán GT `584` cho `det-0004` (IoU 0.7246); `det-0005` unmatched chính thức, dù best-overlap candidate là GT `584` (IoU 0.6673). Geometry-only không phát collision này.

Sensitivity loại frame 4 khỏi **scoring** (backend history vẫn giữ nguyên): source `TP=4, FP=0, FN=2` trên 48 cặp; geometry-only `TP=6, FP=0, FN=0` trên 48 cặp. Positive GT pairs giảm từ 9 xuống 6. Ba cặp ID `573` khác quyết định giữa modes đã thuộc primary automatic population; diagnostic chỉ giúp review, không cộng observations/pairs hoặc điểm lần hai.

Kết quả này là tín hiệu có lợi cho geometry-only, nhưng phụ thuộc 14 matched observations/8 IDs trong tổng 53 observations/24 IDs. Semantic IDs có thể tách/gộp khác physical identity; do đó đây chưa phải bằng chứng đủ để promote.

### Raw và U4-OV semantic labels

Không có category mapping nào được user-confirm trong file mapping đã khóa. Vì vậy mapped-class precision/recall/accuracy là **N/A**. Exact-token comparison bên dưới chỉ là diagnostic, không phải mapping-confirmed semantic accuracy; không áp dụng synonym hoặc parent-child mapping.

| Đầu ra | Phạm vi | Kết quả |
|---|---:|---|
| Raw detector exact token | 14 GT-matched P3 observations | 6 đúng, 8 khác; 0.429 exact-token accuracy |
| Raw detector trên nhóm category có token trong vocabulary | 12 matched trên 20 GT observations được vocabulary hỗ trợ | 6 đúng, 6 khác; coverage detections 12/20 = 0.60 |
| U4-OV single-view | 18 detections | 18 conflicted, 0 classified |
| U4-OV causal | 14 GT-matched accepted observations | 4 conflicted, 10 unknown; 0 labeled, 14 abstain; không có future-observation flag |
| U4-OV final-map | 10 predicted tracks/mode | 2 conflicted, 8 unknown; 0 labeled, coverage 0 |

Trong 12 matched observations thuộc nhóm candidate-vocabulary support, U4-OV abstain 12/12. Không có class correction, harmful change hay confirmed class. Đây là thiếu coverage, không phải bằng chứng classifier đúng hoặc sai. 33/53 GT observations nằm ngoài candidate vocabulary theo exact token và vẫn nằm trong all-GT denominator.

## Tập review và phần đang chờ người dùng

- **Primary review**: [gói review blinded](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/review_primary/index.html). SHA-256 ordering cố định và candidate-independent; 3 evaluator IDs xuất hiện nhiều frame + 3 IDs chỉ xuất hiện một frame; 13 observations. Ẩn detector class, track và GT category. Magenta chỉ đánh dấu evaluator mask. Người review cần trả lời identity và class riêng; singleton identity comparison là unavailable.
- **Diagnostic review bổ sung**: [mở gallery v3 D1/D2](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/review_diagnostic_v3/index.html). D1 chứa bốn observation evaluator ID 573; D2 chứa collision frame 2. Gói primary 13 observations được giữ nguyên; overlap D1/D2 với primary bằng 0. Gallery tách RGB, prediction mask và GT mask thành panel riêng, đồng thời ẩn predicted labels/tracks. V2 được giữ để truy vết nhưng đánh dấu `superseded_invalid_for_review` do filename collision.

| Case | Observation ID đầy đủ | Human identity | Human class |
|---|---|---|---|
| D1 / evaluator 573 | `hm3d-00808-u6q-v1-0000:det-0001` | chờ review | chờ review |
| D1 / evaluator 573 | `hm3d-00808-u6q-v1-0001:det-0003` | chờ review | chờ review |
| D1 / evaluator 573 | `hm3d-00808-u6q-v1-0002:det-0003` | chờ review | chờ review |
| D1 / evaluator 573 | `hm3d-00808-u6q-v1-0004:det-0001` | chờ review | chờ review |
| D2 / same-frame collision | `hm3d-00808-u6q-v1-0002:det-0004` | chờ review | chờ review |
| D2 / same-frame collision | `hm3d-00808-u6q-v1-0002:det-0005` | chờ review | chờ review |

Association metrics hiện có dùng HM3D evaluator-instance IDs, không phải human-identity labels. Human-identity metrics và quyết định collision là false merge hay duplicate observation vẫn **unavailable** đến khi nhận review. [Manifest diagnostic v3](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/review_diagnostic_v3/manifest.json) xác nhận primary overlap bằng 0.

Ledger matching D2 đã đối chiếu: protocol Hungarian class-agnostic một-một theo frame, IoU ≥0.50. `det-0004` là official match với GT 584 (0.724567); `det-0005` unmatched chính thức nhưng best-overlap candidate là GT 584 (0.667307). **Scorer/metrics không sai**; lỗi nằm ở wording v2 và cách gallery trước không phân biệt official match với candidate. Metrics và mẫu số giữ nguyên. [Matching audit](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/review_diagnostic_v3/matching_audit.json), [content/card validation](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/review_diagnostic_v3/content_validation.json). V3 có 6 observation, 18 output panel với 18 đường dẫn duy nhất; mọi panel được dựng lại từ đúng source RGB/mask và so pixel-exact sau khi lưu PNG. RGB frame 0/frame 4 giống nhau là exact-pose revisit thật, không phải ghi đè file.

- **Diagnostic set B tự động**: [diagnostic cases](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/diagnostic_cases.jsonl) và [association pair disagreements](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/diagnostic_association_pair_disagreements.jsonl). Một diagnostic row vẫn có thể thuộc primary automatic population; JSON có cờ membership riêng. Diagnostic không tạo thêm điểm.

Các kết quả association/class trong báo cáo là evaluator-side tự động trên HM3D masks/descriptor. Nếu xác nhận physical identity/class qua gói review, đó là nhãn development bổ sung; không sửa predictor hoặc kết quả đã khóa.

## U4-OV trên các observation diagnostic

Cosine là similarity, không phải xác suất đã hiệu chuẩn. Top-1 là ứng viên diagnostic, không phải committed label. Margin gate đã khóa là 0.015; chưa có human class nên top-1 correctness chưa chấm được. Số view bins là số bins tích lũy đến causal time theo mode; frame 4 revisit đúng bin frame 0 nên không thêm independent-view support.

| Observation ID hậu tố | Raw | Top-1 / top-2 (cosine) | Margin | Status source / geometry, lý do | View bins source/geometry | Human class |
|---|---|---|---:|---|---:|---|
| `0000:det-0001` | dining_table | table .2951 / dining table .2929 | .00218 | unknown / unknown; chỉ 1 bin | 1 / 1 | chờ review |
| `0001:det-0003` | sofa | skis .2750 / baseball bat .2748 | .00021 | unknown / conflicted; candidate rất sát, geometry đã có 2 bins | 1 / 2 | chờ review |
| `0002:det-0003` | dining_table | box .2629 / surfboard .2628 | .00014 | conflicted / conflicted; candidate rất sát | 2 / 3 | chờ review |
| `0004:det-0001` | dining_table | table .2951 / dining table .2929 | .00218 | conflicted / conflicted; revisit không thêm independent view | 2 / 3 | chờ review |
| `0002:det-0004` | dining_table | dining table .3078 / table .2958 | .01202 | unknown / unknown; thiếu distinct-view support | 1 / 1 | chờ review |
| `0002:det-0005` | dining_table | dining table .2925 / table .2833 | .00919 | unknown / unknown; thiếu distinct-view support | 1 / 1 | chờ review |

Trước khi có nhãn, nhóm D1 có nhiều bins ở geometry-only nhưng vẫn abstain vì margin dưới ngưỡng: có thể là **A (gate chặn candidate đúng)** hoặc **B (candidate sai)**. Quan hệ `table`/`dining table` cũng có thể liên quan taxonomy **C**, nhưng không tự gộp. D2 hiện thiếu view support; chưa phân biệt được **A/B/C/D (association, crop hoặc evidence)**. Không đổi vocabulary, prompt, model hay threshold. [CSV chi tiết v3](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_complete/review_diagnostic_v3/u4ov_human_review_diagnostics.csv).

## Runtime, tests và phạm vi chưa chạy

- Capture 5 frames khoảng 23 s; semantic alignment render khoảng 17 s; P2 khoảng 3.3 s; P3 fallback backend khoảng 91 s theo wall time quan sát; 18/18 accepted. Không đổi backend.
- U2 OpenCLIP feature extraction: 18 crops, 10.85 s process (model load 6.66 s; inference tổng 2.08 s), peak RSS 1508.5 MiB; vectors 512-D, finite/unit-normalized/repeatability theo tolerance U2 đã khóa.
- U3 offline association: 0.077 s, peak RSS 41.5 MiB.
- U4-OV offline: 11.09 s, feature extraction tái dùng cache, peak RSS 648.0 MiB.
- Paired native replay trên cache capture/P2/P3 hiện có: source 64.90 s, geometry-only 66.19 s; tổng 131.08 s. Peak RSS lần lượt 599340 KiB (~585 MiB) và 585436 KiB (~572 MiB). Không chạy lại capture, P2, P3 hoặc OpenCLIP.
- Source wrapper khớp offline source raw 18/18 assignments (0 mismatch). Geometry-only khớp offline reference 18/18 (0 mismatch); tên decision có thể khác nhưng track assignment giống.
- Cả hai: fresh replay, snapshot reload và delta replay đều equal; 0 dangling edge, 0 self-edge, 0 conflicting room owner. M3 arrays, M4 masks/raw geometry/adjacency, M5 geometry/adjacency và room geometry/adjacency khớp trên cả 5 frame. Frame 1 object-node count source 8, geometry 7; đó là khác biệt association/membership có provenance, không phải M3–M5 structural regression.
- [Paired replay manifest](../artifacts/audits/u6_hm3d_00808_v1/native_paired_replay_v1.json), [source run](../artifacts/runs/u5m9_20261010T030914.318901Z_d8b8e3/launcher_manifest.json), [geometry run](../artifacts/runs/u5m9_20261010T031023.553758Z_0cda0d/launcher_manifest.json).
- `python3 -m pytest -q tests/test_u6_scoring.py tests/test_hm3d_object_gt_evaluator.py`: **18 passed**. Bao gồm unit tests trực tiếp cho U6 scorer; **không chạy full suite** trong lượt này.

## Kết luận và bước tiếp

**Association:** geometry-only có tín hiệu evaluator-instance tốt hơn trên lát cắt matched nhỏ (3 positive pairs bổ sung; recall 1.00 so với .667), và paired M9 integration gate 5-frame pass. Chưa thấy false merge xuyên frame trên evaluator IDs, nhưng coverage route thấp; same-frame collision và fragmentation ID 573 cần human review để kết luận physical identity. Giữ geometry-only là **candidate experimental có bằng chứng hạn chế**; source vẫn default, chưa promote.

**Semantic classifier:** U4-OV chưa có giá trị phân loại đo được ở scene này do abstain 100% trên các observations được chấm. Giữ diagnostics-only; không đưa label vào graph/room scores.

**Phạm vi:** Không đánh giá room labels do không có room GT. Paired native replay là integration check 5 frame, không phải accuracy evidence. Dense 00802 vẫn hoãn. U5 overall partial: integration/reconciliation checkpoint đạt, real merge/retirement policy deferred, dense replay chưa kiểm chứng. Bước tiếp theo là nhận identity/class review của 6 diagnostic observations rồi chấm human-identity metrics trên cùng subset. U4-OV diagnostics-only. Một hướng development duy nhất sau review là kiểm tra margin-gate abstention trên tập diagnostic đã gắn nhãn; bác bỏ nếu giảm abstention làm tăng harmful committed labels hoặc không tăng coverage chính xác. Không triển khai thay đổi này trong lượt hiện tại.

## Lệnh U6 đã chạy

```bash
# P2: cwd=/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG
# to resolve the locked relative model path; all output stays in Fusion.
/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/.venv-perception/bin/python -B /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/scripts/run_replica_cad_instance_segmentation.py \
  --capture /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/runs/u6_hm3d_00808_quality_v1_20261010/capture \
  --output /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/runs/u6_hm3d_00808_quality_v1_20261010/detection_2d \
  --config /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/perception/yolo11n_seg_replica_cad_dev.json \
  --weights /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/models/ultralytics/yolo11n-seg.onnx \
  --names /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/perception/coco.names

# P3: cached capture and P2 masks, with locked source settings.
.venv-appearance/bin/python -B scripts/project_detection_masks_to_3d.py \
  --capture artifacts/runs/u6_hm3d_00808_quality_v1_20261010/capture \
  --detection-2d artifacts/runs/u6_hm3d_00808_quality_v1_20261010/detection_2d \
  --output artifacts/runs/u6_hm3d_00808_quality_v1_20261010/detection_3d \
  --profile configs/perception/yolo11n_seg_replica_cad_dev.json

# U3 causal development replay over saved P3/features
.venv-appearance/bin/python scripts/run_u3_dev_pilot.py \
  --run-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010 \
  --output-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010/association_u3 \
  --u2-feature-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010/appearance/u2_features

# U4-OV with the same frozen features and assignments
.venv-appearance/bin/python scripts/run_u4ov_dev_pilot.py \
  --run-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010 \
  --association-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010/association_u3 \
  --output-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u4ov \
  --u2-feature-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010/appearance/u2_features

# Offline quality metrics; semantic GT is evaluator-only.
python3 scripts/evaluate_u6_offline.py \
  --run-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010 \
  --semantic-dir artifacts/audits/u6_hm3d_00808_v1/semantic_preflight \
  --descriptor /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/00808-y9hTuugGdiq/y9hTuugGdiq.semantic.txt \
  --mapping configs/evaluation/hm3d_00808_object_category_mapping_v1_1.json \
  --u4ov-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u4ov \
  --output-dir artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_scoring_v2

# Paired native M9 integration check using the existing 5-frame cache.
python3 scripts/scene_launcher.py replay-cached \
  /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/runs/u6_hm3d_00808_quality_v1_20261010 \
  --profile source --semantic off
python3 scripts/scene_launcher.py replay-cached \
  /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/runs/u6_hm3d_00808_quality_v1_20261010 \
  --profile geometry-only --semantic off

# U6 scorer plus HM3D matching evaluator tests.
python3 -m pytest -q tests/test_u6_scoring.py tests/test_hm3d_object_gt_evaluator.py
```

Capture command thực tế đi qua Habitat capture adapter; invocation transcript không được lưu. Effective route/poses, camera metadata và asset hashes nằm trong capture artifacts và [route lock](../locks/u6_hm3d_00808_quality_v1_1.json). Scoring output hiện tại: [metrics.json](../artifacts/runs/u6_hm3d_00808_quality_v1_20261010/u6_offline_evaluation_scoring_v2/metrics.json). Artifacts cũ được giữ nguyên. Route preview: [route RGB preview](../artifacts/audits/u6_hm3d_00808_v1/route_rgb_preview.png).
