# Room Belief v1 — kế hoạch implementation đã chốt

Ngày: 2026-10-14. Trạng thái: được chốt về thiết kế; chưa triển khai/benchmark.

Workspace đích: `/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG`.
Thiết kế đầu vào: `/home/kiettran05/Downloads/room_label_scoring_design.md` và các quyết định người dùng trong hội thoại.

Plan này thay thế các đề xuất chưa chốt về room semantics của những plan trước, không xóa lịch sử. Khi đưa vào workspace, kiểm tra file cùng tên và cập nhật mục liên kết kiến trúc hiện hành, không ghi đè thay đổi người dùng.

## 1. Mục tiêu và ranh giới

ConceptGraphs object states → semantic consensus + geometric membership → evidence ledger → incremental room belief → room label/status.

- OccuSG giữ room geometry, room IDs và adjacency; room node tồn tại trước khi có label.
- Không đổi matcher, object geometry, detector, room segmentation, source/default hoặc các baseline đã khóa.
- Chỉ object → room; không dùng room predictions để sửa object class/membership.
- Không dùng GT/annotation làm predictor input. Dataset mapping chỉ trong evaluator.
- Không import toàn bộ MIT-SPARK benchmark; continuation scoring và incremental controller là adaptation của dự án, không tuyên bố upstream parity.
- Không lấy hypotheses bị filter làm semantic evidence. Admission tiếp tục theo cấu hình object pipeline; không tự thay min_detections.
- LM chỉ dùng offline để tạo bảng A. Runtime không cần model/tokenizer/Transformers.

## 2. Các quyết định đã được người dùng duyệt

| Hạng mục | Quyết định v1 |
|---|---|
| Phạm vi | Nhà ở, mở rộng bằng version mới |
| Ontology | bedroom, living_room, dining_room, kitchen, bathroom, office, storage_room, circulation_space |
| Model chính | Qwen/Qwen3-4B-Base |
| Nếu local không phù hợp | Báo blocker; ưu tiên đề xuất tạo bảng offline trên Kaggle; 1.7B-Base chỉ là phương án thay thế cần duyệt, không tự đổi |
| Scoring | Conditional continuation; chỉ room-label tokens; mean log-prob chính, sum là ablation |
| Neutral | `Observed object: object.\nRoom type:`; subtraction là candidate chính, giữ uncorrected đối chứng |
| Temperature | T=1.0 baseline; chưa tự mở sweep |
| Prior | Uniform; reference prior và runtime prior tách trường, cùng uniform ở v1 |
| Object semantics | Temporal raw-class consensus; chỉ stable góp evidence |
| Category cap | Category presence: mỗi category tối đa một contribution/room; có thể thay policy trong tương lai |
| Membership | Greedy voxel support; zero support → unassigned; không dùng LM |
| Label decision | Có abstention, hysteresis, retract và stale handling |

Không tự chạy cài đặt, tải weights, upload hay GPU job chỉ vì plan đã được duyệt. Xin quyền cụ thể khi cần. Tác vụ dự kiến vượt 30 phút phải xin ngân sách trước.

## 3. Ontology contract

Mỗi canonical ID có definition, display name, prompt completion, aliases và exclusions. Prompt completion dùng dạng ngôn ngữ tự nhiên, ví dụ ` living room`; không chấm token underscore như display ID.

- bedroom: ngủ/nghỉ qua đêm.
- living_room: sinh hoạt chung/tiếp khách/thư giãn.
- dining_room: ăn uống.
- kitchen: chuẩn bị/nấu thức ăn.
- bathroom: vệ sinh cá nhân/toilet/tắm.
- office: làm việc/học tập tại bàn.
- storage_room: lưu trữ là chức năng chính.
- circulation_space: lưu thông giữa các khu vực. Không suy hallway từ việc ít object. Chốt tên completion tự nhiên cho chức năng này trước khi sinh bảng.

Unknown/ambiguous/stale là trạng thái, không là softmax classes. Không thêm `other` để giả làm open-set detector. Không tự xác nhận mixed-use từ top-2; giữ candidates và primary label tùy chọn. Out-of-ontology cần provenance đánh giá riêng, không suy ra chỉ từ softmax thấp.

Dataset mapping có exact/broader/ambiguous/out_of_scope/unmapped; chưa có room GT thì room accuracy unavailable. Building type không được trộn vào ontology room functions.

## 4. Preflight Qwen, tài nguyên và lock

1. Đọc môi trường hiện có; đo RAM available, disk, CPU/GPU, dtype support, package versions. Không dựa riêng vào RAM tổng.
2. Pin model revision, tokenizer, config, nguồn/license/checkpoint hashes và dependency versions trong môi trường cô lập; không sửa environment perception/reference.
3. Ước lượng weights 4B khoảng 8 GB ở 16-bit, 16 GB ở FP32; đây không phải peak RAM. Kiểm tra headroom, load peak và scoring peak thực tế sau khi được duyệt tải/chạy.
4. Không tự quantize, offload hoặc đổi precision/backend để vượt budget; mọi adaptation phải có config/provenance và được duyệt.
5. Tách kiểm tra offline/no-download, quyền tải model và benchmark thật. Tiny fake-model tests không được gọi là Qwen smoke pass.
6. Nếu local bị chặn, hoàn thành phần CPU controller/tests vẫn làm được; đề xuất Kaggle chỉ với code, ontology/object strings và weights. Không cần upload HM3D RGB-D, GT, annotations.
7. Không tự fallback 1.7B. Nếu được duyệt sau, tạo model/bảng version riêng, không gắn tên 4B vào kết quả đó.

Nguồn model: https://huggingface.co/Qwen/Qwen3-4B-Base . Cần pin revision khi triển khai, không chỉ URL main.

## 5. Scorer và bảng A

Prefix: `Observed object: {canonical_object_name}.\nRoom type:`.
Completion: leading-space + room prompt name. Neutral dùng `object` thay category.

Với L room-label tokens:

`s_mean(c,r) = sum_k log P(y_k | prefix, y_<k) / L`

`s_corrected(c,r) = s_mean(c,r) - s_mean(neutral,r)`

`p_tilde = softmax(s / T)`

`A(c,r) = log((p_tilde(r|c)+epsilon)/(reference_prior(r)+epsilon))`

Yêu cầu:
- Teacher forcing, eval/inference mode; không gọi generate để lấy câu trả lời rồi chấm.
- Không tính prefix, padding vào loss; attention mask đúng; chốt BOS/EOS policy. V1 không tính EOS vào room-label score.
- Kiểm tra prefix/completion token boundary trên full string, không giả định tokenization ghép riêng luôn tương đương. Nếu có token vượt boundary, fail preflight và thiết kế delimiter rõ ràng; không âm thầm chấm cả prefix.
- Kiểm tra causal shift logits, leading space, batch-vs-single parity, multi-token labels.
- FP32 log-softmax/score accumulation nếu tương thích; ghi rõ dtype của weights và scoring. Không tự tuyên bố bit-parity khác thiết bị.
- Dùng stable log-softmax; epsilon phải explicit/versioned. Log raw scores, token IDs/counts, mean/sum, neutral, corrected và A.
- Không gọi p_tilde là xác suất room thật hoặc A là calibrated likelihood ratio.
- Uniform K=8: compatibility không vượt log(8) theo công thức; test bound và finite values.
- Phạm vi ablation đầu: mean/sum × neutral/no-neutral, T=1.0. Khóa tiêu chí chọn trên dev trước chấm; nếu chưa đủ room GT, không tuyên bố phương án thắng, giữ baseline provisional.
- Generic objects, synonyms, neutral row và prompt bias cần sanity tests; không sửa thủ công các hàng theo nhãn test.
- Object vocabulary lấy từ predictor categories/canonical strings, không GT room labels. Unknown row không tự sinh runtime.
- Bổ sung row cần version/hash mới, invalidate/recompute affected state. Thay ontology phải tính lại normalization toàn bảng.

## 6. Object semantic consensus v1

Không cần thêm image encoder/classifier trong bước này. Tái sử dụng raw class history; không sửa raw observations.

Input: observation ID, frame/time, class, alias status, pose/view-group provenance, object revision. Canonicalization chỉ synonyms được duyệt; không tự table=dining_table hoặc stove=oven.

- Alias không góp lần hai. Exact-pose/pixel revisit không tạo hỗ trợ mới.
- Nhóm viewpoint là heuristic versioned, không phải độc lập thống kê. Khóa cách grouping và xử lý observations mâu thuẫn trong nhóm.
- Mỗi nhóm có tối đa một đơn vị support. Đề xuất phân phối support theo tỷ lệ class votes duy nhất trong nhóm; không dùng confidence như xác suất đã hiệu chuẩn.
- Semantic states: unknown, provisional, stable, conflicted. Stable cần support tối thiểu và dominance/margin đã cấu hình; những giá trị này phải được đề xuất/khóa trước eval, không nhúng số vô căn cứ trong code.
- Stable output là temporal consensus, không đảm bảo class đúng. Nhãn sai nhất quán và class ngoài detector vocabulary là giới hạn phải báo.
- q dùng one-hot stable class; w=1 khi stable + retained + assigned + supported A row, còn lại 0.
- Semantic chuyển stable→conflicted phải retract ngay; không giữ vote chỉ vì label từng stable.
- Lưu raw history, vote ledger, eligible/excluded observation IDs/reasons, label/status/method và semantic revision.

Không đánh đồng admission min_detections với semantic stability. Retained provisional objects vẫn hiện trên graph/viewer nhưng không góp room score.

## 7. Greedy room membership

Tái sử dụng geometry OccuSG theo revision hiện hành. Tạo voxel representatives dùng riêng cho đo membership nếu cần; không sửa object cloud/reference fusion.

`N(o,R) = số representatives thuộc geometry room R`

- Chọn room có N lớn nhất nếu max N > 0. N được đo trên cùng resolution/coordinate frame và boundary policy.
- Zero support hoặc room geometry không hợp lệ → unassigned.
- Tie: ưu tiên owner cũ nếu vẫn trong tập đồng hạng; nếu không, chọn ID theo thứ tự deterministic. Ghi tie flag. Không dùng room label/LM để phá hòa.
- Output assigned không hàm ý chắc chắn: lưu support_fraction, runner_up_fraction, outside_fraction, tie và geometry revision.
- Greedy 51/49 có thể flip; đo số membership flips, không che mất trade-off. V1 chưa thêm membership hysteresis.
- Phân biệt unassigned object, empty room và unvisited room; robot chưa vào không có nghĩa object không thể assigned.
- Room geometry/membership update phải retract/reassign evidence đúng revision.
- Policy greedy áp dụng feature-flagged trong đường room-semantics v1; không âm thầm thay default membership của baseline.

## 8. Category presence và belief ledger

Per room giữ object evidence ledger và reverse index category→eligible object IDs.

`n_R(c) = số retained/stable/assigned objects có class c và A row hợp lệ`

`S_R(r) = log(room_prior(r)) + sum_c 1[n_R(c)>0] A(c,r)`

`belief_R = softmax(S_R)`

- Chỉ category transition 0→positive hoặc positive→0 làm thay contribution category.
- Class sửa: cập nhật counts category cũ/mới. Object chuyển room: thực hiện trong một transaction.
- Alias/merge/retirement: bỏ IDs cũ đúng provenance rồi thêm state mới; không double count.
- Room split/merge: rebuild từ union/membership mới, không cộng trực tiếp posterior các room.
- Repeated events idempotent, out-of-order không ghi đè revision mới; cùng revision khác payload hash phải báo lỗi.
- Recompute từ ledger phải khớp incremental trong tolerance; kiểm tra numeric drift.
- Category cap giảm tác động fragmentation cùng class nhưng không sửa identity; fragment khác class vẫn có thể double evidence. Ghi known limitation.
- Không dùng absence của category như negative evidence. Không dùng số frame làm số object votes.
- Chưa xử lý dependency giữa categories: output là normalized belief scores, không calibrated posterior.
- Policy có version để nâng cấp cap/soft weights sau; không thay âm thầm giữa run.

## 9. Label policy

Room state gồm primary label, status, candidates, raw scores/beliefs, category support, object provenance, evidence/config/ontology versions và revision.

- Không commit argmax nếu thiếu evidence, support hoặc margin.
- Ngưỡng gán nhãn và relabel, min category support và hysteresis là config; đề xuất trước khi chấm, không dùng threshold cũ của rule-based labeler cho score mới.
- Không bắt buộc ví dụ sofa→TV→bed→wardrobe phải kết thúc bedroom.
- Hysteresis chỉ tính evidence revisions thực, không đếm số lần chạy lại classifier.
- Mất toàn bộ eligible evidence → unknown; label trước đó lưu history, không còn là current supported label.
- Evidence đổi nhưng chưa đạt nhãn mới: ghi rõ status/last_supported_label, không giữ label cũ như stable vô điều kiện.
- Table/ontology version mismatch hoặc update chưa hoàn tất → stale/error có nguyên nhân; không commit kết quả cũ.
- Chưa triển khai tự xác nhận mixed-use/open-set; lưu candidates và giới hạn.

## 10. Causal event integration

Export hiện tại final-map-only không tự trở thành persistent runtime contract.

Tách ba mức:
1. Static snapshot real objects: kiểm chứng scoring/adapter.
2. Causal replay: lấy object/room states thực tại từng prefix, không dùng final geometry/merge/semantics vào quá khứ.
3. Online runtime integration: events thật, revisions thật, latency đo được.

Event envelope: schema, event_id, event_type, sequence_id, frame/time, producer_revision, object/room IDs, payload_hash, provenance/config hashes.
Batch/frame transaction quyết định rồi commit room semantic state một lần. Async results phải đối chiếu revisions, không ghi đè newer evidence.

Nếu backend chưa phát alias/split/merge/retirement thật, chỉ tuyên bố contract + synthetic tests; ghi riêng runtime events đã kiểm chứng. Snapshot reload không được mặc nhiên xem là khôi phục mutable object backend state.

## 11. Phases và gate

| Phase | Công việc | Điều kiện hoàn tất |
|---|---|---|
| RB0 | Kiểm kê code/contracts, ontology, preflight Qwen, locks và ngân sách | Báo khả thi/blocker; không tự tải/cài/chuyển model |
| RB1 | Semantic consensus, greedy membership adapter, category ledger/controller | Unit/property tests; fixture ghi rõ không phải quality |
| RB2 | Scorer Qwen smoke và tạo A sau phê duyệt tài nguyên | Token scoring verified, raw logs/hash/model provenance, runtime/RSS |
| RB3 | Static thật trên hybrid cache và room snapshot đúng sequence | Viewer evidence, classification vs admission phân biệt, không gọi online |
| RB4 | Causal object/room revisions và relabel events | Prefix causality + retract/idempotence/resume; phân biệt events synthetic/thật |
| RB5 | Dev evaluation hữu hạn, freeze configs | GT room đủ điều kiện hoặc metrics unavailable; ablations công bằng |
| RB6 | Feature-flag launcher và regression handoff | Default unchanged, rollback, commands thực chạy, giới hạn rõ |

RB1 có thể tiếp tục nếu Qwen local bị chặn, nhưng không thay bảng thật bằng fixture rồi tuyên bố RB2/RB3 quality đạt. Không tạo scene/capture mới nếu chưa cần hoặc chưa được duyệt.

## 12. Evaluation và tests

Tests bắt buộc:
- Tokenizer boundary, shift, padding, label-only loss, mean/sum, neutral, batching, numeric stability.
- Consensus alias/revisit/conflict/unsupported, stable→conflicted và class sửa.
- Membership transform/unit/room revisions, zero support, tie, boundary and outside points.
- Category counts: 1 vs 12 chair cùng contribution; remove một trong nhiều chair không rút cả category.
- Incremental==recompute; replay idempotent; stale event rejected; atomic transfer; serialization/resume.
- Missing/changed A row, ontology version mismatch, no implicit model download.
- Graph room geometry/adjacency và baseline/default không đổi.

Quality:
- Static room accuracy/macro-F1 kèm labeled coverage, selective error và unmapped/OOV denominators.
- Causal first-correct/stable-label delay, wrong relabels, flip count, recovery sau correction, stale durations.
- Room segmentation/matching protocol riêng: không coi predicted region ID là GT room ID.
- Semantic consensus coverage/accuracy riêng để biết bottleneck nằm ở object hay room scorer.
- Baseline rule-based và LM dùng cùng evidence/admission/membership khi so classifier.
- Development caches hiện có không là holdout. Room annotation phải riêng; object identity review không tự thành room GT.
- Zero-shot mô tả rõ pretrained-LM compatibility, còn dev chọn prompt/temperature/threshold là model selection phải công bố.
- Room label chỉ là navigation hint; chưa tuyên bố cải thiện ObjNav khi chưa đo navigation.

## 13. Artifacts, dependencies và bàn giao

Đề xuất đường dẫn trong workspace, kiểm tra tồn tại trước khi tạo:
- `docs/ROOM_BELIEF_V1_IMPLEMENTATION_PLAN.vi.md`
- `configs/room_semantics/ontology_v1.json`, `consensus_v1.json`, `room_belief_v1.json`
- `locks/room_semantics/` cho model/env/scoring contracts.
- Module dưới semantics/integration_v2 hiện có, ưu tiên reuse; không tạo graph backend mới.
- Scripts riêng cho offline A generation và causal/static room-semantics replay.
- `artifacts/room_semantics/<run_id>/`: manifest, A/raw scores, ledger, events, metrics, viewer, commands.

Viewer giải thích object nào eligible/excluded vì sao, class support, category cap, room support ratios, contribution trước/sau, label transitions. Không cần framework mới.

Đo riêng: model download/setup nếu có, load/scoring peak, A generation, runtime update p50/p95/max, memory ledger, adapter/viewer overhead. Không so runtime cached lookup với end-to-end mapping như speedup.

## 14. Prompt khởi động cho AI implementation

Đọc đầy đủ plan này và thiết kế gốc. Sao chép plan vào docs workspace nếu chưa có; nếu đã có, đối chiếu trước, không ghi đè thay đổi người dùng.

Lượt đầu thực hiện RB0 và phần RB1 không cần model: kiểm tra code/events/semantic history hiện có, chốt schema và cấu hình đề xuất, triển khai ledger/category-presence cùng tests nếu nằm trong ngân sách được duyệt. Không chỉ tạo thêm audit nếu có phần implementation an toàn làm được.

Giữ Qwen3-4B-Base làm model chính. Preflight local read-only trước. Không tải weights/cài packages/quantize/đổi sang 1.7B/upload Kaggle nếu chưa được duyệt riêng. Nếu local không phù hợp, báo blocker cụ thể và ưu tiên đề xuất offline Kaggle chỉ cho sinh bảng A; CPU controller vẫn có thể triển khai local.

Không sửa matcher, geometry, source/default hoặc room scores baseline. Không dùng GT để tạo predictor evidence. Không chạy final-map-only demo rồi gọi là causal online. Không tự mở thêm sweep/tuning, model mới hoặc scene mới.

Bàn giao code/tests thực chạy, resource findings, schema/config proposals còn cần duyệt, lệnh tái lập và gate status. Việc dự kiến vượt 30 phút phải xin ngân sách trước. Dừng trước RB2 khi chưa có quyền tải/chạy model; không tự coi xác nhận thiết kế là quyền thực hiện external actions.
