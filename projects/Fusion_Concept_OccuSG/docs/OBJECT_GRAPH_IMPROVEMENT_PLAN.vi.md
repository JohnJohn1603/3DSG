# Object graph quality và Object-Level Fusion — plan thống nhất

> **LEGACY / HISTORY (2026-10-12):** This plan/handoff belongs to the earlier U/K/Q development track. It is retained for provenance and is not the active roadmap. Follow [the current ConceptGraphs → OccuSG plan](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md).


Cập nhật: 2026-10-11. K0, K1, K3 và một candidate K4 offline đã chạy trên cache development; K2 chỉ kiểm kê/đề xuất. K4 chưa tích hợp M9, K5–K6 chưa triển khai.

## Trạng thái theo giai đoạn

| Giai đoạn | Trạng thái | Kết quả / điều kiện còn lại |
|---|---|---|
| K0 — khóa mốc | **Đạt** | Manifest khóa input/config/resource/replay, metric contract và evaluator coverage: `artifacts/audits/object_graph_improvement/baseline_lock_v1.json`. Không sửa baseline cũ. |
| K1 — cô lập association regression | **Đạt trong phạm vi cache 00802** | So source, geometry-only thuần (same-frame dedup tắt trong diagnostic replay) và geometry-only-dedup-v2 trên cùng 29-frame cache. Có failure ledger/gallery và metric contract. Dữ liệu evaluator bao phủ 24/45 observations; identity là HM3D semantic instance ID, không phải human physical identity. |
| K2 — runtime P3 | **Kiểm kê xong; chưa thay đổi môi trường** | Interpreter là `.venv-perception` của baseline, Python 3.12.3; SciPy import không có, nên code dùng fallback all-pairs O(n²). Không cài package. Đề xuất riêng ở mục K2. |
| K3 — OF0–OF2 geometry sidecar | **Prototype offline đạt; resume-state chưa hỗ trợ** | Trích point evidence trực tiếp cho toàn bộ 45 P3 observations trong cache 29 frame. Fixed assignments, voxel accumulator/viewer, causal lineage, exact-pose không tăng số pose duy nhất, idempotence/hash conflict và deterministic repeat được kiểm tra. Snapshot NPZ đọc lại được; chưa có API khôi phục accumulator để tiếp tục run. Chưa chiếu geometry vào graph/M9/room scores. |
| K4 — association improvement | **Candidate offline đã chạy; chỉ development** | Một config khóa trước scoring dùng accumulated surface support làm continuity branch sau layer/radius gates, chỉ khi nhánh geometry cũ không có candidate đạt. Causal repeat cho assignments/decision ledgers giống byte-for-byte. Cache 00802, không phải holdout; không nối M9/default. |
| K5 — semantic evidence | **Chưa làm** | U4-OV tiếp tục diagnostics-only; không mở rộng vocabulary hoặc thay classifier. |
| K6 — room evidence/launcher | **Chưa làm** | Room rules/scores và launcher không thay đổi trong lượt này. |

## Quy tắc bất biến

- Source/default và baseline `Rebuild_3DScene_OccuSG` giữ nguyên; native binaries, cache, GT/evaluator và models bên ngoài chỉ đọc.
- Không chạy lại capture, detector/P2, full P3, native mapping dài, hoặc dense 280 frame. K1 dùng cache đã khóa; K3 P3 chỉ chạy prefix 0–9 vào output riêng.
- Predictor không đọc GT, semantic instance ID, room label hay human annotations. Evaluator chỉ dùng HM3D official one-to-one matches hậu kiểm.
- Không tuning threshold/model/vocabulary/room rules. Không triển khai K4–K6.
- K4 candidate đọc P2/P3 evidence theo thứ tự thời gian; evaluator IDs chỉ được đọc sau khi assignments/decision ledger đã ghi. Không phản hồi vào graph builder, object nodes hay room scores.

## K0 — baseline lock và metric contract

Input duy nhất: `hm3d_00802_20261011T033053.236654Z_086a73`, 29 frame, 320×240. Hashes capture/P2/P3/P3 manifest và U3/P3/pipeline/fusion configs được lưu trong `baseline_lock_v1.json`; ba replay manifests cùng native-resource hashes cũng được khóa.

Evaluator ledger là `object_evaluations/eval_20261011T033506.897379Z_32ddce/observation_matches.jsonl`, one-to-one theo frame. Có 45 raw P3 observations, trong đó 24 có evaluator semantic instance ID hợp lệ (53.3%). Không có human physical identity annotation cho input này.

Metric contract:

- Population association: unordered cross-frame pairs chỉ trong các observations có evaluator identity; cùng frame bị loại khỏi cross-frame score.
- TP: cùng evaluator ID và cùng assigned track. FP: khác evaluator IDs trong cùng assigned track. FN: cùng evaluator ID nhưng bị chia track, deferred hoặc unmatched/unassigned. Evaluator-unmatched identities không mặc định đúng/sai; coverage và số raw observations vẫn được báo.
- False merge: predicted track chứa nhiều hơn một evaluator ID eligible. Fragmentation: evaluator ID đi vào nhiều predicted tracks; báo số instance bị fragmented và excess tracks.
- HM3D semantic IDs là identity proxy của dataset, không tương đương xác nhận physical identity. Các cặp có thiếu GT identity được ghi unverified.

## K1 — paired association replay và chẩn đoán

Replays source và dedup-v2 được tái sử dụng theo hash. Để tách association khỏi same-frame dedup, đã chạy một native diagnostic geometry-only cùng cache với `--disable-same-frame-mask-dedup`; profile công khai/default không đổi. Ba replay dùng đúng capture/P2/P3 input hashes.

| Mode | Final tracks | TP/FP/FN | Precision / Recall / F1 | False-merge tracks | Fragmented evaluator IDs | Alias / ambiguous / deferred | Runtime | Peak RSS |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| Source | 23 | 9/0/8 | 1.000 / 0.529 / 0.692 | 1 | 4 | 0 / 0 / 0 | 157.9 s | 606.9 MiB |
| Geometry-only thuần, no-dedup diagnostic | 27 | 7/0/10 | 1.000 / 0.412 / 0.583 | 0 | 6 | 0 / 0 / 3 | 177.7 s | 618.7 MiB |
| Geometry-only-dedup-v2 | 26 | 7/0/10 | 1.000 / 0.412 / 0.583 | 0 | 6 | 4 / 0 / 0 | 182.7 s | 609.2 MiB |

Các số này chỉ mô tả cache development 00802 và evaluator-covered pairs, không phải chất lượng tổng quát. Geometry-only bỏ false-merge track trong evaluator-covered subset nhưng tăng fragmentation và FN; source có recall/F1 cao hơn. V2 so với geometry-only thuần đổi 12 cặp quan hệ: 0 evaluator-verified correction/regression, 4 same-frame diagnostic, 8 thiếu identity; không có cơ sở nói v2 tăng accuracy ở cross-frame.

Ba gate failure được xác minh trong replay geometry-only: các observation ở frame 3, 17, 18 đều có center distance trong candidate radius nhưng AABB IoU lần lượt 0.0260, 0.0349, 0.0141, thấp hơn acceptance 0.05. Vì vậy chúng bị từ chối bởi `geometry_iou_below_acceptance`, không phải thiếu candidate do khoảng cách. P3 extents rất khác so với prior views (extent consistency xấp xỉ 0.415, 0.423, 0.423), phù hợp với visible partial cluster/geometry dao động nhưng chưa chứng minh nguyên nhân duy nhất. Evaluator IDs 340, 428, 433 chỉ là dataset identity; chưa có xác nhận human identity. Năm regression relations là các cặp, thuộc ba evaluator IDs trong ledger, không phải năm vật đã được xác nhận.

Artifacts: `artifacts/audits/object_graph_improvement/k1_three_modes_v2/metrics.json`, `report.vi.md`, `gate_failure_gallery.html`. Raw observations/alias/defer vẫn giữ trong lineage; same-frame pairs không nhập cross-frame denominator.

## K2 — P3 runtime, chỉ kiểm kê/đề xuất

Launcher/replay dùng `/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/.venv-perception/bin/python`, Python 3.12.3. `scipy.spatial.cKDTree` không import được; P3 dùng fallback all-pairs khoảng cách O(n²). Chưa cài đặt hoặc thay interpreter.

Đề xuất bước riêng nếu được duyệt: tạo environment perception riêng trong Fusion workspace, khóa một bản SciPy tương thích, rồi chạy parity trên các cloud thật ở nhiều kích thước. So component membership/index sets, largest cluster, selected point sets và Detection3D dưới cùng eps/min-points/stride/tie-break. Báo runtime/peak RSS; không đổi semantics hoặc cài vào Habitat/baseline env. K2 không chặn K1/K3.

## K3 — OF0–OF2

### Contract/config

`configs/fusion/object_geometry_v2.json`: voxel 0.02 m, voxel key `floor(xyz / size)` kể cả tọa độ âm; sufficient stats là tổng XYZ/RGB và point count; representative là trung bình mỗi voxel. Admission chỉ assigned non-alias observations; deferred/unassigned và alias không apply; same observation + cùng payload idempotent, payload hash khác hoặc owner khác thì lỗi; timestamp mới của mỗi object không giảm; giới hạn 2,000,000 voxel/object fail rõ, không bỏ evidence âm thầm. `unique_sensor_pose_count` chỉ đếm pose matrix duy nhất sau round 6 chữ số; không khẳng định góc nhìn độc lập. `object_geometry_v1.json` và K3 v1 artifacts giữ nguyên lịch sử cũ; trường cũ `independent_view_count` là thuật ngữ legacy, không nên đọc là bằng chứng independent view. Appearance/semantic off; feedback matcher off; không denoise, cross-track merge hoặc retirement.

### P3 evidence sidecar

Sidecar xuất trực tiếp trong P3, không chiếu/clustering lại: selected `xyz_W`, pixel `(row,column)`, RGB, pose, IDs/timestamp, config/input hashes. Prefix frames 0–9: 26 P2 detections, 14 P3 accepted, 12 rejected, 8 frame batches có detections. Sidecar gồm đúng 14 accepted observations. Để K4, sidecar prefix này được kết hợp với phần frames 10–28 chạy từ cached P2/capture; không chạy lại detector hoặc capture.

Verifier đối chiếu full cached P3, source mask/RGB pixels và backprojection: 14/14 Detection3D center/extent/class/score parity; mask/RGB pixels trùng; max XYZ error `2.3842e-7 m` với tolerance `2e-5 m`. Đây là direct P3 sidecar với pixel indices được kiểm tra, không phải reconstruction từ count/center.

Lỗi optional sidecar export hiện được ghi riêng trong manifest `partial`/errors và không làm Detection3D hợp lệ thành rejected; điều này được bảo vệ bằng targeted unit test. Đã chạy P3 export-off trên đúng 10 frame, cùng capture/P2/config; `detection_3d.jsonl` export-on/off bằng nhau chính xác, cả hai đều 14 accepted và 12 rejected.

### Fixed assignments, geometry accumulation và viewer

`scripts/run_object_fusion_offline.py` sử dụng lineage của source, geometry-only thuần và dedup-v2. Prefix 0–9 bắt đầu từ empty fusion state; assignments lấy từ replay có prefix causal. Raw observation lineage được giữ; duplicate aliases và deferred/unassigned không được nhập vào accumulator. Không có GT input hay matcher feedback. Mỗi mode có `objects.json`, revisions NPZ/PLY/PNG, timelines, RGB/mask cards và `index.html` viewer.

Kết quả prefix 0–9: runtime 11.69 s, peak process RSS 124.7 MiB. Source: 10 fused tracks, 3 multi-observation tracks (2, 1, 3 unique sensor poses), 7,784 voxels / 18,786 raw selected points. Geometry-only thuần: 12 tracks, 1 multi-observation track (3 unique sensor poses), 8,102 voxels / 18,786 points. Dedup-v2: 11 tracks, 1 multi-observation track (3 unique sensor poses), 7,785 voxels / 17,505 points, 1 alias excluded. Đây là output tích lũy geometry, không phải accuracy; track/assignment differences không được che. Số pose duy nhất không chứng minh góc nhìn độc lập.

Hai lần chạy offline với cùng config/assignments cho `objects.json` và toàn bộ snapshot NPZ arrays giống nhau ở cả ba mode: 14 / 14 / 13 snapshots. Runtime khác nhau do wall clock; tests cũng khóa idempotence, payload conflict, tọa độ âm, exact-pose view count, alias/defer admission và resource cap.

Artifacts chính: `artifacts/audits/object_graph_improvement/p3_prefix_0_9_v1/`, `of2_prefix0_9_v5/` và `of2_prefix0_9_repeat_v1/`. Các thư mục thử lỗi v1–v4 giữ nguyên để truy vết; dùng v5/repeat làm kết quả hợp lệ.

### Giới hạn

- Prefix 10 frame, state rỗng tại frame 0; không tương đương fusion state full sequence.
- Snapshot NPZ/PLY/JSON được ghi và đọc lại để so sánh, nhưng engine chưa khôi phục mutable accumulator từ snapshot; không gọi đây là resume support.
- Chưa đo geometry accuracy/completeness trên physical-object GT; cloud nhìn dày hơn không đồng nghĩa đúng hơn.
- Chưa tích hợp vào ObjectNode/M9/room labeler; không làm appearance/semantic accumulation, denoise, merge/retirement hay feedback.
- Same-frame alias policy từ replay được giữ nguyên; K3 chỉ loại alias khỏi lần fusion thứ hai.

## K4 — accumulated surface support (offline development)

### Khóa candidate và nguồn

Config `configs/association/geometry_only_accumulated_surface_v1.json` được khóa trước scoring; SHA-256 `64417561e65ff04bb01cfd9de420b1909e7ab3a7ef904ff4d5f63e66658d4cf2`. Dùng prior-only accumulated surface support sau các layer/radius gates hiện có, và chỉ khi không có candidate nào đủ điều kiện ở nhánh geometry gốc. IoU acceptance cũ vẫn là 0.05; không sửa cross-frame thresholds. 0.02 m voxel đại diện, 0.04 m nearest-surface tolerance, ít nhất 100 điểm/voxel representatives được support và forward ratio tối thiểu 0.35. Guard possible containment chỉ defer khi các điều kiện bất đối xứng đã khóa cùng đạt; đây là heuristic bảo thủ, không phải mixed-object detector.

ConceptGraphs tham khảo `conceptgraph/slam/utils.py::compute_overlap_matrix_2set` tại pinned commit `93277a02bd89171f8121e84203121cf7af9ebb5d`: nearest-point support từ detection tới map object với mẫu số bất đối xứng. K4 adaptation dùng state tích lũy nhân quả, forward/reverse diagnostics và defer guard; không sao chép association policy tổng thể của ConceptGraphs.

### Input completeness và tính nhân quả

Input là cùng cache 29 frame 00802 đã dùng ở K1. Có 45 raw P3 observations; sidecar trực tiếp cho frame 0–9 được kết hợp với sidecar frames 10–28 dựng từ capture/P2/P3 cache đã lưu. Preflight xác nhận 45/45 point sets khớp hash/count/shape với Detection3D nguồn; tổng 4 aliases từ dedup-v2 bị loại khỏi matching/evidence accumulation lần hai. Point sidecar sau frame 9 là output tái dựng từ dữ liệu cache hiện có; không gọi đó là exact selected-point parity của toàn bộ cache nếu chưa có provenance gốc cho các indices. Không chạy capture/detector, native M1–M10 hoặc clustering lại toàn bộ cache trong candidate K4. State bắt đầu rỗng ở frame 0, không có resume.

Candidate không dùng class, appearance, GT, annotation, room label hoặc observation tương lai. Matcher quyết định trước khi surface hiện tại được apply; deferred/alias observations không thêm surface evidence. Raw P3 observations vẫn được giữ trong denominator/evidence ledger.

### Kết quả trên development cache

Evaluation dùng official one-to-one HM3D semantic instance IDs, có eligible identity cho 24/45 raw P3 observations (53.3%). Đây không phải human-confirmed physical identity, không phải holdout, và không đại diện scene generalization.

| Mode | TP/FP/FN | Precision / Recall / F1 | False-merge tracks | Fragmented IDs | Final tracks |
|---|---:|---|---:|---:|---:|
| Source | 9/0/8 | 1.000 / 0.529 / 0.692 | 1 | 4 | 23 |
| Geometry-only-dedup-v2 | 7/0/10 | 1.000 / 0.412 / 0.583 | 0 | 6 | 26 |
| Accumulated surface support | 9/0/8 | 1.000 / 0.529 / 0.692 | 0 | 5 | 25 |

Candidate có 1 continuity match, 1 defer, 4 aliases không cộng evidence lần hai; tích lũy 21,593 voxel representatives. So với dedup-v2: 7 cặp quan hệ thay đổi, gồm 3 correction thuộc evaluator ID 428, 1 regression thuộc ID 433, 3 thiếu identity; ba cặp tốt hơn đến từ một observation mới được nối, không phải ba vật độc lập được sửa. So với source: F1 giữ ngang, giảm false-merge track 1→0 nhưng fragmentation tăng 4→5; có 4 relation corrections, 4 regressions và 10 cặp thiếu identity trong thay đổi được xét. Vì vậy chưa có bằng chứng tổng thể candidate tốt hơn source.

Gate cases: frame 3/det-0001 vẫn tạo track mới vì forward support chỉ 10/3064 (0.003); frame 17/det-0001 được surface-continuity nối vào track, forward 192/207 (0.928); frame 18/det-0000 bị defer do possible mixed-mask expansion, forward 534/1217 (0.439), reverse 1.0. Đây chỉ là kết quả scorer/evaluator; relation đổi quanh ID 433 là regression theo dataset identity proxy.

Một lần chạy lặp dùng cùng input/config tạo byte-identical `candidate_assignments.jsonl`, `candidate_ledger.jsonl`, `changed_decisions_posthoc.jsonl` và `relation_changes.jsonl`. Artifact: [`k4_surface_support_v1_locked`](../artifacts/audits/object_graph_improvement/k4_surface_support_v1_locked/report.vi.md) và [`repeat`](../artifacts/audits/object_graph_improvement/k4_surface_support_repeat_v1/report.vi.md).

Runtime candidate 2.69 s sau khi P3 sidecars sẵn sàng; preflight 0.11 s; post-P3 wall time khoảng 3.09 s; peak RSS 59.5 MiB; snapshots 370,779 bytes. Thời gian tạo sidecar P3 không được đo. Source/dedup timings 157.9/182.7 s gồm native M1–M10 nên không so trực tiếp với offline association time. Kết quả mới không phải end-to-end runtime.

### Trạng thái và điểm dừng

Candidate tiếp tục là offline experimental, chưa nối M9/default và chưa chứng minh cải thiện tổng quát. Một bước kế tiếp duy nhất nếu muốn xác nhận decision-level evidence là review identity có mục tiêu cho observation `hm3d-00802-main-0017:det-0001` (surface match) và `hm3d-00802-main-0018:det-0000` (defer/regression), cùng các observation liên quan trong gallery; không cần review lại toàn bộ cache. Nếu không có xác nhận phù hợp thì giữ candidate offline.

## K5–K6 — chưa triển khai

### K5: semantic evidence

Tái sử dụng U4/U4-OV hiện có; tách raw detector label khỏi fused label/status và provenance. Đánh giá causal và final-map riêng, coverage/abstention/harmful correction cùng unsupported taxonomy. Không bật vào predictor/room evidence khi chưa có lợi ích đo được.

### K6: room evidence/consumer workflow

Chỉ dùng semantic evidence đã qua gate K5; đánh giá room functional labels riêng với room geometry/membership, có GT room phù hợp. Giữ source/default và rollback. Không suy ra cải thiện segmentation từ object metrics.

## Tests/kiểm tra lượt này

Đã chạy K4 targeted set: `python3 -m pytest -q tests/test_object_geometry_fusion.py tests/test_surface_support_association.py tests/test_profile_comparison.py tests/test_p3_object_evidence_indices.py tests/test_u5_duplicate_v2_backend.py` — **37 passed**. Đã chạy lại K4 candidate độc lập: bốn JSONL outputs chính byte-identical với run khóa. Full test suite không chạy trong lượt K4 này. Native paired 29-frame replay/serialization checks được thực hiện ở U5 trước đây; K4 candidate này là offline scorer, không phải M9 integration check.

## Điểm dừng và lần tiếp theo

K0/K1/K3 technical outputs và K4 offline candidate có thể được review theo artifacts đã khóa; điều này **không** chứng minh candidate association hoặc fused geometry cải thiện chất lượng tổng quát. K2 vẫn cần duyệt dependency/env riêng. K4 chưa được nối M9; không tự động promote, không nối room scores và không bắt đầu K5/K6.
