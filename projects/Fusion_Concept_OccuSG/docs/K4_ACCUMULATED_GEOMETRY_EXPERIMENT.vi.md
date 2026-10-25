# K4 accumulated geometry — causal experiment handoff

> **LEGACY / HISTORY (2026-10-12):** This plan/handoff belongs to the earlier U/K/Q development track. It is retained for provenance and is not the active roadmap. Follow [the current ConceptGraphs → OccuSG plan](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md).


Ngày: 2026-10-11. Đây là kiểm chứng development trên cache 00802 đã dùng trước đó; không phải holdout. Không capture, không chạy detector/P3 hoặc native M9 trong lần kiểm chứng này.

## Phạm vi và cách chạy

Candidate C là implementation K4 hiện có trong `scripts/run_u3_surface_support_candidate.py`; không tạo backend thứ hai. Nó dùng geometry tích lũy causal từ `src/standalone_sg/objects/object_geometry_fusion.py` cùng matcher `ExperimentalAssociation`.

Thứ tự mỗi frame: lấy prior surface chỉ đến frame `t-1` → chạy cùng-frame dedup-v2 → chốt mọi assignment/defer của frame `t` → chỉ sau đó cập nhật surface bằng các observation được nhận. Alias không góp evidence lần hai; deferred không cập nhật track. State bắt đầu rỗng ở frame 0. Predictor không đọc GT, annotation, class, appearance hay room label. Causality được ghi `uses_future_observations=false` trong manifest.

Lệnh tái lập đã chạy thành công, tái dùng capture/P2/P3 và hai replay đã khóa:

```bash
python3 scripts/run_u3_surface_support_candidate.py \
  --input-run artifacts/runs/hm3d_00802_20261011T033053.236654Z_086a73 \
  --p3-output artifacts/audits/object_graph_improvement/p3_prefix_0_9_v1 \
  --p3-output artifacts/audits/object_graph_improvement/p3_suffix_10_28_k4_v1 \
  --dedup-v2-replay artifacts/runs/u5m9_20261011T035821.873971Z_5aaaf1 \
  --source-replay artifacts/runs/u5m9_20261011T035542.851355Z_b39b87 \
  --output artifacts/audits/object_graph_improvement/k4_surface_support_validation_20261011
```

Output mới: [`validation report`](../artifacts/audits/object_graph_improvement/k4_surface_support_validation_20261011/report.vi.md) và [`manifest`](../artifacts/audits/object_graph_improvement/k4_surface_support_validation_20261011/manifest.json). Bốn file assignments/ledger/relation output có SHA-256 byte-identical với `k4_surface_support_v1_locked`; config và input hashes cũng khớp. Đây là tái lập K4 v1, không phải thuật toán mới.

## Kết quả trên cùng population

Input có 45 P3 observations. HM3D evaluator ID khả dụng cho 24/45 observations (53.3%), gồm 12 evaluator IDs; 266 unordered cross-frame pairs được chấm, 10 same-frame pairs được loại khỏi metric continuity. Evaluator IDs là proxy dataset, không đồng nghĩa physical identity đã được con người xác nhận.

| Mode | TP/FP/FN | Precision / Recall / F1 | False-merge tracks | Fragmented evaluator IDs | Deferred/unassigned |
|---|---:|---:|---:|---:|---:|
| Source | 9/0/8 | 1.000 / 0.529 / 0.692 | 1 | 4 | 0 |
| Geometry-only-dedup-v2 | 7/0/10 | 1.000 / 0.412 / 0.583 | 0 | 6 | 0 |
| K4 accumulated surface | 9/0/8 | 1.000 / 0.529 / 0.692 | 0 | 5 | 1 |

K4 phục hồi hai TP so với geometry-only-dedup-v2, nhưng kết quả pairwise vẫn ngang source. Nó giảm false-merge count từ 1 xuống 0 so với source, đổi lại fragmentation tăng 4→5 và có một observation defer. Vì vậy đây là trade-off cụ thể, chưa phải cải thiện tổng thể hoặc chất lượng đã được xác nhận.

K4 chỉ đổi một quyết định thành surface-continuity match: `hm3d-00802-main-0017:det-0001`. Tại thời điểm đó, prior evidence là các observation thuộc track đến từ frame 11, 12 và 16; current observation có 192/207 surface representatives được hỗ trợ (0.928), trong khi AABB IoU 0.05 gate không đạt. Human review xác nhận Case A cùng vật, single-object: match đúng.

### Quan hệ cụ thể source ↔ K4 và mẫu số

Trên 24 observations có HM3D evaluator ID (12 evaluator IDs), có `C(24,2)=276` cặp; 10 cặp cùng frame bị loại theo protocol cross-frame, còn **266 cặp** được chấm. GT-positive population là 17 cặp cùng evaluator ID; 249 cặp là evaluator-different. Unmatched/deferred không bị loại: nếu hai observations cùng evaluator ID nhưng không cùng track, cặp là FN. Với source và K4, cùng aggregate `TP/FP/FN = 9/0/8` vì mỗi mode giữ 9 trong 17 positive pairs, tạo 0 false-positive cross-frame pairs và bỏ lỡ 8; TN tương ứng 249. Các cặp cụ thể trong relation ledger cho thấy đây không phải cùng partition:

| Evaluator identity | Cặp observations | Source | K4 | Diễn giải |
|---|---|---|---|---|
| 340 | `hm3d-00802-main-0002:det-0002` ↔ `hm3d-00802-main-0003:det-0001` | cùng track | tách/defer | Source đúng, K4 sai |
| 433 | `hm3d-00802-main-0017:det-0000` ↔ `hm3d-00802-main-0018:det-0000` | cùng track | tách/defer | Source đúng, K4 sai; Case B |
| 433 | `hm3d-00802-main-0017:det-0000` ↔ `hm3d-00802-main-0019:det-0000` | cùng track | tách | Source đúng, K4 sai; frame 19 chỉ hậu kiểm |
| 433 | `hm3d-00802-main-0018:det-0000` ↔ `hm3d-00802-main-0019:det-0000` | cùng track | tách | Source đúng, K4 sai; frame 19 chỉ hậu kiểm |
| 428 | `hm3d-00802-main-0011:det-0000` ↔ `hm3d-00802-main-0023:det-0001` | tách | cùng track | Source sai, K4 đúng theo evaluator ID |
| 428 | `hm3d-00802-main-0016:det-0000` ↔ `hm3d-00802-main-0023:det-0001` | tách | cùng track | Source sai, K4 đúng theo evaluator ID |
| 428 | `hm3d-00802-main-0017:det-0001` ↔ `hm3d-00802-main-0023:det-0001` | tách | cùng track | Source sai, K4 đúng theo evaluator ID |
| 438 | `hm3d-00802-main-0015:det-0000` ↔ `hm3d-00802-main-0023:det-0002` | tách | cùng track | Source sai, K4 đúng theo evaluator ID |

Đây là 4 regression và 4 correction evaluator-verified; các cặp khác trong changed-relation ledger hoặc cùng frame (ngoài mẫu số này), hoặc thiếu evaluator identity và vẫn **unverified**. Aggregate TP/FP/FN không mã hóa *cặp nào* được nối: bốn positive links mất đi và bốn positive links mới được thêm vào triệt tiêu ở tổng TP/FN, dù membership/partition và track membership khác nhau. FP vẫn bằng 0 vì các thay đổi evaluator-verified nêu trên đều là giữa observations cùng evaluator ID; điều đó không loại trừ false merge ở một track-level population khác.

`false_merge_tracks` là số predicted tracks trong đó các observations được evaluator match thuộc hơn một evaluator ID, chia theo số predicted tracks có ít nhất một trong 24 scored observations: source **1/16**, dedup-v2 **0/18**, K4 **0/17**. Đây là track-level count, không phải số pairwise FP. Source có một track chứa nhiều evaluator IDs chỉ qua tổ hợp observations cùng frame; pairwise metric loại cặp cùng frame nên source vẫn có FP=0. Evaluator ID là hậu kiểm dataset proxy, không phải human physical-identity label.

Case B `hm3d-00802-main-0018:det-0000` bị defer bởi `surface_support_possible_mixed_mask`. Human review đã xác nhận frame 17–19 cùng `obj_1`, và ba mask đều single-object. Frame 19 chỉ là hậu kiểm. Đây là missed association của K4, không phải bằng chứng mixed-mask guard đúng.

Human-reviewed subset gồm 7 observations, 2 identity tokens và 20 cross-frame pairs (9 same-object pairs). Source: 6/0/3 TP/FP/FN, F1 0.800; geometry-only-dedup-v2: 4/0/5, F1 0.615; K4: 6/0/3, F1 0.800, với 1 observation defer. K4 ngang source trên pairwise human identity, không thắng source; Case A match đúng nhưng Case B vẫn bị bỏ lỡ. Trên phần 6 observations đồng thời có evaluator ID, K4 là 3/0/3 (F1 0.667), source 6/0/0 (F1 1.000), geometry-only-dedup-v2 2/0/4 (F1 0.500). Human metrics và evaluator-ID metrics vì vậy phải giữ riêng.

R23 là regression **same-frame dedup** trên dense cache khác; nó không thuộc cache K4 29-frame này và không phải cross-frame surface-continuity test. Annotation/user review và duplicate-v2 report hiện có được giữ nguyên. Alias policy của K4 tiếp tục chỉ nhận canonical observation; R23 không được dùng để tuyên bố cross-frame matcher an toàn.

## Technical checks và giới hạn

- Tái lập: `candidate_assignments.jsonl`, `candidate_ledger.jsonl`, `changed_decisions_posthoc.jsonl`, `relation_changes.jsonl` byte-identical với K4 lock.
- Runtime offline K4 trên sidecars có sẵn: 2.32 s; peak RSS 59.5 MiB. Không bao gồm P3 sidecar generation. Source/dedup native runtimes cũ là 157.94/182.66 s cho M1–M10, không được so trực tiếp với offline K4 và không chạy lại ở đây.
- Targeted regression: 39 passed. Một test mới ghi lại counterexample two-touching-surfaces: hai bề mặt phẳng khác vật tiếp xúc ở x=1.3 m; một observation dải biên có AABB IoU 0.0299 với prior boxes nhưng matcher vẫn phát `surface_continuity` cho một track (support forward 1.0, reverse 0.062). Nhãn identity của synthetic fixture không được đưa vào matcher. Test pass nghĩa là failure mode được tái hiện có tính xác định; nó **không** xác nhận candidate an toàn.
- Các test khác bao phủ partial view, nearby disjoint surfaces, small-contact patch, possible mixed-mask containment, candidate tie/defer, alias exclusion, no-self-match, geometry branch unchanged và dedup-v2. Synthetic fixture không chứng minh generalization.

Kết luận hiện tại: accumulated geometry tạo được một continuity match đúng ở Case A và khôi phục metric so với geometry-only-dedup-v2 trên cache này, nhưng không vượt source trên human subset và vẫn bỏ lỡ Case B. Counterexample bề mặt chạm nhau cho thấy support cao có thể match nhầm. **Giữ source/default; K4 tiếp tục offline experimental, chưa tích hợp M9.**

## Một route development đề xuất — chưa capture

Đã tạo route config [`00808 K4 route v1`](../configs/habitat/hm3d_minival_v02_00808_k4_accumulated_geometry_route_v1.json), SHA-256 `80843ef4025aa573a51c381bc0f54bbb2dacd163647b9e6a422691a243e40978`. Route dùng bảy pose theo chuỗi A→B→C→kitchen detour→C→B→A. Các vị trí và cạnh là các pose/cạnh đã được xác nhận navigable trong 00808 U6; validator chạy lại xác nhận 7/7 pose navigable và mọi cạnh liên tiếp connected: [`route_validation_v2.json`](../artifacts/audits/object_graph_improvement/u6_00808_k4_route_proposal_v1/route_validation_v2.json). Đây là **sequence mới cùng scene 00808**, vẫn là development, không phải unseen-scene test.

Ba vị trí A/B/C nhìn khu seating/table từ các góc không gian khác nhau; D là detour sang vùng kitchen/cabinet có vật lân cận. Tuyến quay lại C/B/A sau detour để kiểm tra causal re-observation. Pose lặp lại không tăng independent-view count. Không dùng detector, semantic IDs, GT hay predictor output để chọn route. HM3D semantic assets đã có sẵn; semantic render chỉ dùng cho evaluator sau capture.

Khóa dự kiến: 640×480, HFOV 90°, fx=fy=320 px, principal point (319.5,239.5), axial depth 0.1–10 m; detector/P2/P3 và toàn bộ source, duplicate-v2, K4 thresholds/config hashes giữ nguyên. Một capture/P2/P3 dùng chung cho ba association modes, state độc lập và causal. U4-OV, appearance feature extraction, native M1–M10 replay, room scores nằm ngoài phạm vi.

**Route visibility preflight (không phải chỉ navigability):** RGB preview hiện có cho thấy A/B/C là ba vị trí camera khác nhau cùng nhìn vùng seating: sofa và bàn thấp hiện diện ở các góc khác nhau; B/C còn thấy cụm bàn ăn và ghế ở gần sofa/bàn thấp. Đây là vùng thích hợp để kiểm tra cùng vật qua các pose, partial-view expansion và vật gần nhau có nguy cơ gộp nhầm. Detour kitchen quay lại cùng khu vực sau khi quan sát cabinetry; nó là revisit có detour, không được tính thêm view độc lập cho chính pose lặp. Preview xác nhận vùng nằm trong field of view của các pose đã chọn, nhưng **không chứng minh** detector/P3 sẽ chấp nhận cùng vật ở ≥2–3 pose; đó là điều kiện kiểm tra sau khi có pilot. Vì vậy đây là development pilot, không phải bảo đảm dữ liệu sẽ đủ để kết luận.

**Phạm vi và ngân sách mới (không gồm native replay):** 7 frame hires; capture 2–4 phút, P2 1–3 phút, P3 cùng selected-point sidecar 5–8 phút, offline source/dedup-v2/K4 association + manifest/metrics 1–3 phút; tổng dự kiến **9–18 phút**, độ tin cậy trung bình-thấp. Peak RAM chưa có số đo trên cùng pipeline hires; sẽ đo trong pilot được duyệt, không suy từ replay native. Ước tính P3 dựa trên hires pilot trước đó (4–5 phút/5 frame); không cộng thêm lượt clustering: `project_detection_masks_to_3d.py --export-object-evidence` ghi selected-point sidecar ngay trong cùng lượt P3. Association K4 trước đây mất 2.32 giây/29 frame trên sidecar sẵn có; ba mode và báo cáo được dự toán 1–3 phút kể cả khởi động/ghi artifacts.

Một điểm phụ thuộc cần xử lý trước capture: K4 runner hiện dùng source và dedup-v2 replay làm reference assignments. Vì pilot này loại native replay, phải dùng/hoàn thiện đường association offline source và dedup-v2 trực tiếp trên cùng P3 packets, rồi đưa assignments đó cho K4; không được giả vờ lấy M9 assignment hoặc first/last endpoint làm offline output. Nếu đường offline này chưa sẵn sàng, thời lượng code/kiểm tra chưa nằm trong 9–18 phút và **chưa nên capture** cho đến khi cập nhật ngân sách/launcher. Chỉ sau khi offline path sẵn, dự toán kỹ thuật vẫn dưới 30 phút; chưa có capture được phép/chạy trong lượt này.

Nếu được duyệt, acceptance dữ liệu pilot là: ít nhất một cùng-vật candidate có P3 accepted ở các vị trí camera khác nhau; có evidence partial view mở rộng qua pose; và có một cặp vật ở gần nhau để đo false merge. Nếu các điều kiện không xuất hiện trong 7 frame, dừng với “insufficient pilot evidence”; không mở route hoặc thêm frame. Navigability chỉ cho biết camera có thể tới pose, không thay thế visibility/P3 acceptance.

Route proposal manifest: [`proposal_manifest.json`](../artifacts/audits/object_graph_improvement/u6_00808_k4_route_proposal_v1/proposal_manifest.json). Chưa capture/chạy predictor. Nếu được duyệt, đây là một new-route development test; kết quả tốt vẫn chỉ là tín hiệu thử nghiệm, không promote K4.
