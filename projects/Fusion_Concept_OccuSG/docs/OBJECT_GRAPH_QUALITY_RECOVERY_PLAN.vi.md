# Kế hoạch cải thiện chất lượng object graph và room semantics

> **LEGACY / HISTORY (2026-10-12):** This plan/handoff belongs to the earlier U/K/Q development track. It is retained for provenance and is not the active roadmap. Follow [the current ConceptGraphs → OccuSG plan](CONCEPTGRAPHS_OCCUSG_REBUILD_PLAN.vi.md).


Ngày: 2026-10-11. Workspace đích: `/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG`.

## 1. Mục tiêu và nguyên tắc

Chuyển từ kiểm chứng lặp lại một prototype sang các thí nghiệm phát triển hữu hạn, có đối chứng và điểm dừng. Mục tiêu là cải thiện identity association và object semantic labels, sau đó mới đo tác động lên room labels. Không đồng nhất room functional label với room-boundary segmentation.

Không bỏ code đã làm hoặc thay toàn bộ bằng ConceptGraphs. Tái sử dụng launcher, evaluator, lineage, native adapters, selected-point sidecar, geometry accumulator và K4. Không cam kết cải thiện đáng kể trước khi đo.

Plan này là thứ tự ưu tiên chất lượng mới, không xóa lịch sử U0–U6/K0–K6. Khi sao chép vào `docs/`, ghi rõ quan hệ với các plan cũ; không đánh dấu những gate chưa đạt thành hoàn tất.

## 2. Hiện trạng làm mốc

- Source là mặc định. Geometry-only-dedup-v2 là opt-in qua M9. K4 accumulated surface là offline experimental. U4-OV diagnostics-only, chưa thay graph class hoặc room scores.
- Fixed-assignment fusion đã tích lũy geometry: không tự sửa assignments, class hoặc room label.
- K4 giữ matcher AABB cũ làm nhánh chính, dùng surface tích lũy làm fallback. Center/extents matcher được trung bình old/new, không phải bbox tính lại từ accumulated cloud.
- Source sử dụng class và khoảng cách. Geometry-only bỏ class, thêm IoU/ambiguity/one-to-one gates. Đây là thay chính sách, không phải source cộng thêm một module luôn có lợi.
- ObjectNode trong backend geometry khi matched cập nhật hình học/timestamps nhưng giữ class cũ. Room labeler cộng điểm từ class đó. Do vậy cloud tốt hơn chưa trực tiếp sửa bàn→bed.
- ConceptGraphs reference có tích lũy cloud, tính lại bbox và tích lũy features; workspace hiện tại là adaptation, không phải bản sao toàn bộ pipeline/model.
- U4-OV có 105 candidate labels và prototype thresholds chưa hiệu chỉnh. Không coi cosine là xác suất hoặc zero abstention là mục tiêu.

### Mốc 00802 đang có

Input: `hm3d_00802_20261011T033053.236654Z_086a73`, 29 frame, 320×240, 45 P3 observations; 24 observations có evaluator identity. 266 cặp cross-frame, trong đó 17 positive pairs.

| Mode | TP/FP/FN | F1 | False-merge tracks / tracks có evaluator identity | Fragmented IDs |
|---|---:|---:|---:|---:|
| Source | 9/0/8 | 0.692 | 1/16 | 4 |
| Geometry-only-dedup-v2 | 7/0/10 | 0.583 | 0/18 | 6 |
| K4 | 9/0/8 | 0.692 | 0/17 | 5 |

Source và K4 có 4 evaluator-verified corrections và 4 regressions khi đối chiếu relations, không phải cùng partition. Các mẫu số track và cross-frame pair khác nhau; precision 1 không chứng minh không có false merge.

Case A được human review xác nhận K4 nối đúng. Case B cùng vật nhưng K4 bỏ lỡ. Fixture hai vật chạm nhau tái hiện false match của K4; test pass vì tái hiện failure không phải safety gate pass. R23 là regression same-frame mixed mask riêng.

### Cập nhật pilot 00808

Route 7 frame hires đã được đề xuất, chưa capture. Ước lượng 9–18 phút chỉ gồm capture/P2/P3+sidecar/offline association, chưa gồm chuẩn bị runner offline. K4 runner còn phụ thuộc native replay references; phải gỡ phụ thuộc đúng cách trước khi xin duyệt capture.

## 3. Bảng thực hiện

| Giai đoạn | Việc triển khai | Kết quả bắt buộc | Điểm dừng |
|---|---|---|---|
| Q0 | Chuẩn hóa runner association offline dùng trực tiếp P3 | Chạy source/dedup-v2/K4 trên cùng cache; parity với references sẵn có | Không capture nếu chưa chạy được |
| Q1 | Bộ development có phạm vi rõ và pilot đã duyệt | Manifest, annotations tái sử dụng, mask purity/identity riêng, mẫu số rõ | Thiếu dữ liệu thì báo chính xác, không tự kéo dài route |
| Q2 | Ablation representation của track | So state trung bình hiện tại với geometry tích lũy trong nhánh chính | Một candidate representation; kết quả âm vẫn hợp lệ |
| Q3 | Hiệu chỉnh evidence/association có giới hạn | Class/appearance là tín hiệu mềm; shortlist cấu hình development | Không vô hạn thêm guard hoặc ép Case B |
| Q4 | Semantic fusion độc lập với identity errors | Raw/voting/OV so causal và final, correction/harm/coverage | Chưa đạt chất lượng thì giữ diagnostics |
| Q5 | Tích hợp candidate đạt gate, launcher và room evidence | Opt-in, rollback, paired replay, test mới khóa trước | Không promote mặc định chỉ vì tests pass |

Q4 có thể nghiên cứu song song sau Q1 trên nhóm identity đã xác nhận; không phải đợi association hoàn hảo. Chưa làm Q2–Q5 trong lượt khởi động Q0.

## 4. Q0 — Đường offline thực sự độc lập native replay

### Implementation

1. Đọc runner/backend/evaluator đang có và tái sử dụng hàm chung. Không tạo bản chép thuật toán source/dedup/K4 dễ lệch nhau.
2. Nhận capture manifest, P2/P3 và selected-point sidecar từ cùng run. Tạo typed packets đúng coordinate frame, timestamp, layer, observation IDs; preserve source-order nơi source contract yêu cầu.
3. Source phải gọi implementation source thật; dedup-v2 phải dùng đúng dedup policy và canonical selection hiện có; K4 phải dùng cùng aliases với dedup-v2 và prior-only accumulated state.
4. Xuất assignments, aliases, deferred reasons, decision ledger, profile/config/input hashes. Không yêu cầu chạy M1–M10 để tạo source/dedup references mới.
5. Với cache cũ có native replay references: so assignments theo observation ID và partitions; không coi ID renaming là identity change. Alias/canonical/defer semantics phải khớp, không chỉ số track.
6. Native references là oracle kiểm thử khi có, không phải input bắt buộc của predictor offline. Với cache mới không có native references, báo parity unavailable, không giả mạo parity pass.
7. Chỉ normalise tên quyết định thật sự tương đương; không che khác object ownership, ordering hay dedup membership.

### Verification

- Chạy trên cache 00802 có đủ sidecar; source và dedup-v2 đối chiếu native references sẵn có, K4 đối chiếu output đã khóa.
- Tests: source-order, coordinate conversion, aliases, same-pose suppression khác same-frame alias, defer, input mismatch, causality trước/sau frame, repeatability.
- Giữ detector/P3/native artifacts bất biến. Không chạy capture, detector, clustering hoặc native replay trong Q0.
- Bàn giao một lệnh CLI chạy được trên cache; launcher wrapper chỉ khi ghép mỏng được vào cấu trúc sẵn có, không cần framework.

### Gate Q0

Đạt khi cả ba mode chạy trực tiếp từ cache, kết quả khớp references trong phạm vi đã khóa, tests liên quan pass và không có GT vào predictor. Nếu không đạt, sửa một lỗi cụ thể; không mở audit toàn dự án.

## 5. Q1 — Dữ liệu đủ để phát triển, không chạy lại vô hạn

1. Lập manifest development từ cache 00800/00802/00808 có sẵn. Giữ riêng resolution và route; mỗi so sánh paired phải cùng inputs. Không gộp tất cả observations thành mẫu độc lập.
2. Tái sử dụng annotation bằng full observation IDs/hash. Không chuyển token theo suffix sang run khác; không hỏi lại trường hợp đã xác nhận nếu dữ liệu không thay đổi.
3. Ghi rõ nhóm tình huống: class flip, partial-view expansion, hai vật gần/chạm nhau, duplicate cùng frame, mixed mask, singleton và revisit.
4. Candidate-selected review dùng để chẩn đoán, không đại diện overall accuracy. Tập chấm chính phải cố định độc lập với mode và có cả trường hợp bình thường.
5. Sau Q0, cập nhật dự toán pilot 00808: chi phí chuẩn bị đã tiêu, chi phí pipeline còn lại, peak-memory chưa biết và trần thời gian. Xin duyệt trước capture.
6. Nếu được duyệt: capture/P2/P3 một lần; sidecar xuất ngay khi P3 chọn points. Chạy ba mode offline. Không native replay/U4-OV/OpenCLIP nếu thí nghiệm association không dùng chúng.
7. Sau 7 frame kiểm tra có vật accepted qua các vị trí camera khác nhau, partial view và vật gần nhau. Nếu thiếu, dừng với insufficient evidence; không tự tăng route.

Pilot này là development. Test mới cho nghiệm thu phải được khóa trước khi xem outcomes; cùng scene thì gọi new-route test, không gọi unseen-scene.

## 6. Q2 — Cho fusion ảnh hưởng đúng biểu diễn track

Giả thuyết: trung bình bbox của các partial observations không biểu diễn cùng thông tin như bbox của surface tích lũy. Đây là giả thuyết cần thử, không phải bug đã được chứng minh gây mọi regression.

- Giữ source và dedup-v2 làm controls, K4 v1 làm reference.
- Thử đúng một candidate: center/bounds nhánh matcher lấy từ prior accumulated geometry. Giữ dedup, policy assignment, evidence inputs và thresholds còn lại cố định để cô lập representation.
- Quy định bbox từ min/max representatives hay extent voxel cells; test rõ không nhập nhằng. Không cập nhật geometry bằng observation hiện tại trước khi quyết định frame.
- Log candidate nào bị radius/IoU gate loại ở hai representation. Không giả định union bbox lớn hơn luôn tốt: nó có thể tăng false merge hoặc khuếch đại contamination.
- Raw observation center/bounds giữ nguyên, fused representation là trường riêng có revision/provenance.
- Chấm toàn population cố định và các strata, không chỉ Case A/B. Báo correction/regression, merge/fragmentation/defer, memory và runtime cùng phạm vi.
- Kết thúc bằng giữ hoặc bác bỏ candidate. Không bắt buộc nó phải thắng, không tự thêm nhiều guard trong cùng vòng.

## 7. Q3 — Cho phép development calibration có giới hạn

Không đóng băng prototype defaults vĩnh viễn. Chỉ hiệu chỉnh trên development, không sử dụng nhãn GT/human như input chạy thực tế.

### Phạm vi

- Trước hết kiểm tra tín hiệu hiện có: class agreement/history và appearance similarity phân biệt same/different identities ra sao trên development.
- Class không làm hard identity gate; bất đồng class không tự động là khác vật, đồng class không tự động là cùng vật.
- Appearance cũng không được coi là identity oracle. Cấu hình cũ thất bại không chứng minh mọi cách dùng appearance đều vô ích.
- Không đổi model/checkpoint/preprocessing trong cùng vòng association calibration.
- Chọn tối đa hai candidate policy ngoài controls, tối đa sáu cấu hình tổng; ghi search space, selection rule và trần chi phí trước khi chạy. Không nhân rộng Cartesian grid tùy tiện.
- Chọn một candidate cuối bằng policy đã công bố, rồi khóa trước test. Không chỉnh tiếp trên test mà vẫn gọi đó là holdout.

### Chất lượng

- Báo micro pair metrics và macro theo identity/sequence, coverage, false merge, fragmentation, missed/unassigned và runtime.
- Không yêu cầu chứng minh không bao giờ sai. Nhưng candidate có false merge nghiêm trọng đã biết không được lờ đi; ghi phạm vi lỗi, mitigation nếu có và đánh giá trade-off.
- Nếu không candidate nào hơn source theo tiêu chí đã chốt, giữ source. Đó là điểm dừng hợp lệ, không lý do cho vòng audit vô hạn.

## 8. Q4 — Sửa nhãn object, đúng vấn đề bàn→bed

Tách hai tầng đánh giá:

1. **Identity được cung cấp để cô lập semantic fusion:** nhóm observations người dùng xác nhận cùng vật. Identity annotation chỉ để dựng benchmark chẩn đoán, không đi vào predictor production. Class annotation chỉ vào scorer.
2. **End-to-end:** dùng predicted tracks, giữ cả mixed/fragmented tracks và báo ảnh hưởng của association; không chỉ chấm identity-pure tracks rồi gọi đó là accuracy toàn hệ thống.

So raw detector, voting hiện có và U4-OV trên cùng population; không mặc định OV sẽ thắng voting. Causal tại t chỉ dùng evidence đến t; final-map chấm mỗi physical object/track theo contract riêng, không dùng final label để giả kết quả online.

- Canonical synonyms, parent–child và supported vocabulary phải có version. Không tự đổi table thành dining_table; chấm coarse/fine riêng nếu hierarchy được duyệt.
- Báo unknown/conflicted, unsupported và abstention riêng. Không loại silent các class ngoài vocabulary để làm đẹp số liệu.
- Báo correct→wrong, wrong→correct, correct→abstain; coverage và correctness cùng nhau. Không promote vì precision cao trên một nhãn duy nhất.
- Cho phép calibration gate trên development có ngân sách như Q3; không ép mọi track phải được gán nhãn.
- Kiểm tra crop/visibility/view sampling trước khi kết luận phải đổi model. Không tự tải model hoặc cài dependencies.

Nếu labels chưa tốt hơn, giữ raw và fused fields riêng; không đưa diagnostic guesses vào room scores. Việc từ chối nhãn có thể giảm evidence sai nhưng cũng làm phòng unknown nhiều hơn, phải đo cả hai.

## 9. Q5 — Tích hợp và launcher

Chỉ candidate đã vượt kiểm tra kỹ thuật và có bằng chứng chất lượng phù hợp mới được opt-in vào runtime. Default source không đổi nếu chưa duyệt promote.

- Tái sử dụng M9/lifecycle/serialization contracts. Chạy paired short native replay sau khi chọn candidate, không chạy native cho mọi cấu hình development.
- Với fused semantics: ObjectNode raw label bất biến theo provenance; resolved label/status/history riêng. Consumer room labeler phải khai báo đang dùng raw hay resolved, và không dùng unknown/conflicted như confirmed evidence.
- Room functional accuracy chỉ chấm với annotation phù hợp. Room geometry/adjacency/membership là metrics khác; không suy accuracy phân phòng từ F1 association.
- Launcher hiển thị ngắn: association backend, geometry feedback on/off, semantic consumer raw/fused/diagnostic, experimental/default và artifact path.
- Không xây nhiều profile tên gần giống. Giữ source + một candidate người dùng thử; các ablation nằm trong experiment runner.

## 10. Gate nghiệm thu thực dụng

Trước test, thống nhất ưu tiên giữa false merge, fragmentation, semantic harmful change, coverage và runtime; không đổi tiêu chí sau khi thấy số liệu.

Candidate chỉ được đề xuất dùng experimental khi:

1. Tests input/causality/idempotence/alias/serialization liên quan pass; provenance đầy đủ.
2. Có lợi ích đo được trên mục tiêu đã chọn, không chỉ số points/nodes tăng.
3. Báo mọi regression và coverage thiếu; trade-off nằm trong giới hạn đã duyệt.
4. Test mới không cho thấy suy giảm vượt giới hạn đó; nếu sample thiếu thì giữ kết luận inconclusive.

Promote mặc định là quyết định riêng, không tự động sau experimental integration. Không đòi mọi failure mode biến mất mới được thử, cũng không gọi test fixture tái hiện lỗi là chứng minh an toàn.

## 11. Quản lý công việc và chi phí

- Mỗi vòng chỉ một câu hỏi chính, một tập inputs khóa, một report ngắn và quyết định keep/reject/inconclusive.
- Unit tests chứng minh contract; repeated byte parity chứng minh tái lập; không dùng chúng thay quality metrics.
- Không tái chạy predictor chỉ để sửa bảng báo cáo. Không native replay khi chỉ đo association offline.
- Tách preparation, capture, P2, P3/sidecar, association, fusion, semantic, native và rendering time; không tính speedup giữa phạm vi khác nhau.
- Giữ baseline/reference read-only và user changes. Không tự cài package, tải model, đổi resource lock hoặc ghi đè run.
- Dự kiến vượt 30 phút hoặc cần capture/model/dependency mới: trình phạm vi và xin duyệt. Không coi 9–18 phút pipeline là đã gồm thời gian sửa runner.
- README/plan status cần phản ánh runtime hiện tại; không thêm tài liệu dài nếu một bảng trạng thái đủ. Không xóa lịch sử báo cáo.

## 12. Nhiệm vụ khởi động được giao

Chỉ thực hiện Q0 và chuẩn bị manifest/dự toán Q1. Kết quả cần có là runner offline thật, parity/tests thật và proposal capture hoàn chỉnh. Chưa capture, chưa tuning, chưa Q2–Q5, chưa promote. Lượt kế tiếp sau Q0 sẽ duyệt pilot và chọn ablation representation, không quay lại audit tổng quát từ đầu.

## 13. Execution status — 2026-10-12

Mục 12 được giữ nguyên như lịch sử giao việc ban đầu. Các vòng Q0/Q1/Q2 và Q3 sau đó đã được thực hiện theo các phê duyệt riêng; status dưới đây bổ sung lịch sử, không thay đổi định nghĩa gate cũ.

### Q2 — closed

Kết luận đã khóa: **“Representation-only ablation không thay đổi assignments hoặc cải thiện quality trên hai cache đã chấm.”** Candidate Q2, code, config và artifacts được giữ để tham chiếu, không promote.

### Q3 — completed, development only

- Candidate duy nhất dùng raw detector class/confidence làm soft reranking trên K4-eligible candidates. Source, geometry-only-dedup-v2 và K4 giữ vai trò controls; Q2 chỉ là reference.
- Đã chạy ba lambda 0.05/0.15/0.30 trên cache 00802 và lambda 0.05 đã chọn trên Q1 00808. Không đổi thresholds, eligibility, dedup, model, vocabulary, graph semantic labels hoặc room scores.
- Không có assignment/partition changes ở ba cấu hình. Trên 00802, 15/45 detections có ít nhất một eligible candidate nhưng không observation nào có hơn một eligible candidate; vì vậy reranker không có lựa chọn cạnh tranh để đổi.
- Case A giữ continuity của K4; Case B vẫn defer do mixed-mask surface guard và không thể được cứu bởi reranking. Known same-frame R23 dedup và touching-surface failure không được tính là Q3 pass.
- Kết luận Q3: không có lợi ích association được đo trên hai cache; không giữ candidate như cải thiện, không tích hợp M9. Không tự mở tuning hoặc tìm cache mới.
- Báo cáo, contract/config hash, lệnh tái lập và targeted test results nằm trong `artifacts/audits/object_graph_improvement/q3_raw_class_review_v1_20261012/`.

Q4/Q5 chưa được triển khai hoặc phê duyệt trong vòng Q3 này. Source vẫn là default.

### Q3 report correction and Q4 — 2026-10-12

Report Q3 v1 được giữ nguyên làm lịch sử; bản đính chính được lưu ở `artifacts/audits/object_graph_improvement/q3_raw_class_review_v2_20261012/report.vi.md`. Source có 4 `fragmented_evaluator_instances` nhưng 5 `fragmentation_excess_tracks`. Ledger cho thấy 15/45 observations có ít nhất một geometry-eligible track, không observation nào có hơn một; kiểm tra riêng one-to-one cũng tìm thấy 0 track có eligible proposals từ nhiều observations trong cùng frame. `class_soft_delta` chỉ cộng ở nhánh geometry-eligible; surface-continuity fallback dùng forward-support score và không nhận class delta. Không có lợi ích Q3 đo được trên các cache đã chạy; không suy rộng thành raw class vô ích nói chung.

Q4 baseline đã chấm trên cached 00802 `hm3d-00802-main-dense-*`: 18 class annotations confirmed, 4 same-object groups cho diagnostic identity-conditioned scoring, 604 observations còn lại unreviewed. U4/ U4-OV prediction outputs và embeddings được tái sử dụng; predictor không đọc identity/class GT. Hierarchical score unavailable vì taxonomy chưa duyệt. Q3 K4 IDs dùng namespace `hm3d-00802-main-*`, không khớp population Q4 nên K4 predicted-track metrics unavailable ở đây.

Một candidate duy nhất bỏ U4-OV margin veto (giữ cosine floor/model/vocabulary/prompts) được khóa rồi chấm từ cached scores. Coverage tăng nhưng exact wrong predictions tăng lên 15/18, gồm hai `correct→wrong`; candidate bị bác bỏ. Không có candidate semantic được giữ để tích hợp. U4 class evidence và U4-OV tiếp tục diagnostics-only; không đổi graph class, room scores, model, vocabulary hoặc default. Report/timeline/track diagnostics và lệnh tái lập ở `artifacts/audits/object_graph_improvement/q4_semantic_baseline_v1_20261012/`.

Targeted tests: Q4 scorer/candidate + U4 regressions: 23 passed; `py_compile` cho năm script Q4 mới pass. Full suite chưa chạy. Q4 development baseline đã hoàn tất trong phạm vi hiện tại; không gọi là holdout và không promote.
