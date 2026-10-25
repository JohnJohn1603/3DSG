# U3 same-frame duplicate candidate — offline development

## Phạm vi và quyết định

Đây là candidate offline riêng; source association và geometry-only hiện tại không đổi. Candidate chưa nối vào M9, không sửa default, không gọi semantic classifier, không dùng GT/identity/room label trong predictor. Capture, P2, P3 và native mapping đều được tái sử dụng từ cache.

**Quyết định: giữ geometry-only hiện tại; chưa đưa candidate vào M9.** Trên sáu observation đã review ở 00808, candidate giữ nguyên D1 và giải quyết D2. Nhưng regression 00802 tạo nhiều quyết định suppression mới, toàn bộ cross-class và chưa có identity GT để xác nhận an toàn. Candidate hứa hẹn trong subset đã review, song chưa đủ evidence để chấp nhận nguy cơ suppress vật khác.

## Vì sao D2 tách thành hai track

Hai mask D2 ở frame `hm3d-00808-u6q-v1-0002` có area `4737` và `4023` px; giao `4013`, hợp `4747`, IoU `0.8454`, containment theo mask nhỏ `0.9975`. P3 báo valid-depth support `4737/4023` pixel, tương đương `100%` diện tích mask mỗi observation; cluster points lần lượt `2490/2412`.

Center cách nhau `0.00313 m`, AABB IoU 3D `0.9499`, extent consistency `0.9832`. Dedup cũ chỉ suppress khi mask IoU ≥`0.98`, nên không alias hai mask này. Geometry backend xử lý detections trong frame bằng cách đối chiếu với **track từ các frame trước**; hai D2 không match được prior track và đều nhận `created`, vì backend không ghép hai observation cùng frame với nhau.

## Candidate đã khóa

Profile: `configs/association/u3_same_frame_duplicate_candidate_v1.json`. Baseline source/geometry values được chép từ locked U3 config và runner fail-fast nếu hash/reference khác. Candidate yêu cầu đồng thời:

- mask IoU ≥`0.80` **hoặc** containment ≥`0.95`;
- valid-depth ≥`100` pixel, ≥`50%` mask và cluster ≥`100` điểm cho cả hai;
- cùng layer, center ≤`0.10 m`, AABB IoU 3D ≥`0.75`, extent consistency ≥`0.80`.

Không có raw-class gate. Canonical được chọn theo cluster points, valid-depth support, detector score, mask area giảm dần, rồi detection ID tăng dần. Chỉ suppress khi đúng một canonical đã chọn vượt mọi gate; nhiều canonical phù hợp thì giữ observation ở trạng thái ambiguous. Alias vẫn lưu observation gốc, `duplicate_of` và canonical ID; alias không thêm view hoặc semantic vote. Không có cross-track merge/retirement.

## Paired cache comparison

Mỗi mode replay từ frame đầu với state riêng, cùng thứ tự P3 packets. Các assignment source/geometry-only trên 00800 và 00808 khớp chính xác cached assignment reference: lần lượt `30/30` và `18/18` observation, cùng track IDs. 00802 không có assignment U3 reference; source và geometry current được replay mới từ cache.

| Cache | P3 obs | Tracks source / geometry / candidate | Suppressed cũ / candidate | Deferred source / geometry / candidate |
|---|---:|---:|---:|---:|
| HM3D 00800 multiview 640 | 30 | 12 / 14 / 14 | 0 / 0 | 0 / 0 / 0 |
| HM3D 00802 dense 280-frame | 713 | 75 / 80 / 78 | 17 / 42 | 0 / 79 / 15 |
| HM3D 00808 quality cache | 18 | 10 / 10 / 9 | 0 / 1 | 0 / 0 / 0 |

00800 không có association decision nào đổi giữa geometry hiện tại và candidate. 00808 có đúng một suppression mới: D2 det-0005 alias vào det-0004. Trong sáu observation review, kết quả là:

| Mode | D1 cross-frame TP/FP/FN; P/R | D1 bỏ frame 4 | D2 same-frame | Track structure trên 2 vật |
|---|---|---|---|---|
| Source | 3/0/3; 1.00/0.50 | 1/0/2 | 1/1 cùng track | 1 identity phân mảnh; 1 track thừa |
| Geometry-only | 6/0/0; 1.00/1.00 | 3/0/0 | 0/1 cùng track; split | 1 identity phân mảnh; 1 track thừa |
| Candidate | 6/0/0; 1.00/1.00 | 3/0/0 | 1/1 cùng track | không fragmentation/false merge trên subset |

Frame 4 chỉ bị loại khỏi sensitivity scoring; lịch sử predictor không đổi. D2 không tính vào mẫu số cross-frame.

### Dense regression 00802 — kết quả uncertain

Candidate có `42` aliases so với `17` alias của dedup cũ: `35` observation mới bị suppress, `10` observation cũ được giữ lại làm canonical; 7 alias giữ nguyên. **Tất cả 35 suppression mới là cặp khác raw class**, ví dụ sofa/chair, bed/sofa, car/truck, dining_table/sofa, vase/potted_plant. Đây có thể là class flip của cùng object hoặc masks/point sets của vật riêng bị gộp; cache không có human identity GT phù hợp nên từng trường hợp đang `uncertain`.

Candidate giảm geometry defers từ `79` xuống `15`, giảm track count từ `80` xuống `78`. So với geometry-only, có `98` observation decisions đổi; trong pairwise status ledger có `2` quan hệ đã assigned đổi từ different-track sang same-track, cùng `44,982` pair chuyển từ deferred/unassigned sang assigned (`43,341` different-track và `1,641` same-track). Các chuyển đổi từ deferred/unassigned không phải TP/FP hay bằng chứng đúng: identity GT chưa có. Vì thế không kết luận candidate tốt hơn trên 00802.

Danh sách review hẹp chỉ chứa các suppression mới ở 00802: `artifacts/runs/u3_same_frame_dup_candidate_20261010_v6/00802/candidate_new_suppressions_for_review.jsonl`. Toàn bộ changed observation decisions, pairwise relation deltas và gate evidence cũng nằm trong cùng thư mục. Đây là diagnostic ledger, chưa phải annotation package.

## Tests và provenance

Đã chạy:

```text
python3 -m pytest -q tests/test_same_frame_duplicate_candidate.py tests/test_u3_experimental_association.py tests/test_u6_human_diagnostic.py tests/test_u6_scoring.py
25 passed
python3 -m py_compile scripts/run_u3_same_frame_duplicate_experiment.py src/standalone_sg/objects/same_frame_duplicates.py
```

Tests có near/exact mask duplicate, khác raw class, nearby distinct masks, same image mask but depth-separated objects, large-mask containment, partial/weak-depth evidence, multiple-canonical ambiguity, detection-order determinism, pairwise track-ID renumber invariance và defer transitions. Full suite không chạy. Không có capture, P2/P3, OpenCLIP, native mapping hoặc M9 replay.

Input P2/P3/capture hashes, config/code hashes, test command và assignment parity được lưu trong `artifacts/runs/u3_same_frame_dup_candidate_20261010_v6/experiment_manifest.json`. Output report tổng nằm tại `artifacts/runs/u3_same_frame_dup_candidate_20261010_v6/report.json`. Cache 00802 được đọc từ baseline theo lock; không có file baseline nào sửa.

## Bước tiếp theo

Candidate **chưa qua gate an toàn** cho M9. Giữ geometry-only cũ. Nếu muốn tiếp tục, chỉ review identity của đúng 35 suppression mới ở ledger 00802; sau đó bác bỏ candidate nếu có vật khác bị suppress, hoặc mới xem xét một offline regression mới. Không tuning threshold từ các trường hợp này và không đổi classifier.
