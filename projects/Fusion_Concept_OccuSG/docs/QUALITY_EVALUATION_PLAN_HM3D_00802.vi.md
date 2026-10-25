# Kế hoạch đánh giá chất lượng — HM3D 00802

**Trạng thái:** review mù 25 P2 observations đã nhận và chấm ở phạm vi đã khóa; response gốc được giữ nguyên, các trường annotation thiếu/không nhất quán được loại khỏi metrics tương ứng. Chưa chấm room semantics vì membership scope còn unavailable. Kết quả: [scoring report v1](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1_scoring/quality_report_v1.md).  
**Phạm vi:** baseline HM3D 00802 shared workflow; không tuning, không chạy lại pipeline.  
**Kế hoạch chính:** [SCENE_GRAPH_PROJECT_REVIEW_AND_COMPLETION_PLAN.vi.md](/home/kiettran05/Documents/Codex/2026-09-22/c-c/SCENE_GRAPH_PROJECT_REVIEW_AND_COMPLETION_PLAN.vi.md)

## 1. Baseline được khóa

Baseline là workflow `scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T063106Z_f91798`, gồm C2 `c2_20261017T063106Z_99bc38` và D1 `d1_20261017T063106Z_554073fd`. Nguồn nghiệm thu: [báo cáo pilot](HM3D_00802_SHARED_WORKFLOW_PILOT_20261017.vi.md).

| Hạng mục | Giá trị khóa |
|---|---|
| Scene/route/sequence | HM3D `00802-wcojb4TFT35`, route `main`, `hm3d-v02-00802-main-route-01` |
| Capture | 29 frame; 160×120; HFOV 90°; 26 pose matrices phân biệt; route cố định, không phải navigation |
| Perception | YOLO11n-seg masks; ViT-B-32 image/text feature cache; không phải full ConceptGraphs-Detect |
| Mapping | ConceptGraphs core commit `93277a02bd89171f8121e84203121cf7af9ebb5d`; seed `20261014`; CPU; `obj_min_detections=3`; matcher/config và thresholds giữ nguyên |
| Các hash chính | effective C2 config `c32b2171…96be74`; route `3ea1a21f…46b691`; capture manifest `c0569e7a…d442373`; P2 index `dee97c59…ac582488`; D1 mapping config `da81d983…5a60a24`; checkpoint ViT-B-32 `e6d1bd77…a2b7c31` |
| Causality/stages | 17 online hypotheses; prefix admission không được reference mapper xuất riêng nên `unavailable`; final denoise/filter/merge là terminal stage, không backfill prefix. Room geometry có native replay revisions. Hybrid membership `unavailable_elevation_scope`; room semantics unavailable. |

D1 artifacts ghi 25 detections → 17 hypotheses trước postprocess → 17 sau denoise → 2 sau filter → 2 sau merge. **15 hypotheses bị filter đều chỉ có một detection, một frame và một pose; hai survivors có năm detections mỗi. Merge không giảm thêm object nào.** Hypothesis thứ nhất có raw history `couch` và một `potted plant`; thứ hai có năm raw `tv`. `fused_semantic_label` của cả hai unavailable. Đây là mô tả output, không phải xác nhận identity/class.

`run_hm3d…063106…` là baseline development mới. Các static room-belief report cũ trỏ tới hybrid run/cache khác, nên không được trình bày như kết quả room scoring của baseline này. Bốn A tables vẫn là experimental; không có room prediction/accuracy hợp lệ cho run mới khi membership gate đóng.

## 2. Annotation và failure evidence có thể dùng

| Artifact đã có | Kết luận áp dụng cho baseline này |
|---|---|
| `artifacts/human_review/hm3d00802_main29_min3_v1_review_v1/` | Gói mù cũ có **33 panel observation IDs duy nhất** qua H01–H06. Response có **12 annotation theo từng observation**, tất cả thuộc H01; H02–H06 chỉ có nhận xét tổng hợp theo nhóm, không có 21 dòng annotation riêng. Trong 12 ID đó có 5 ID trùng population P2 hiện tại; trong 33 panel IDs có 14 ID trùng. **Không tái sử dụng annotation nào**: ID giống nhau không tự chứng minh ảnh/capture provenance khớp; gói cũ được giữ nguyên. Room `room:6 → living_room` cũng không được dùng để chấm baseline mới nếu chưa khớp polygon/revision hash. |
| U6 review 00808 | Scene/sequence khác, không ghép theo frame index. Không dùng cho 00802. |
| U3 duplicate review `hm3d-00802-main-dense-*` | Route/observation population khác; chỉ là failure-case tham khảo, không dùng làm ground truth cho route `main`. |
| Baseline D1 lineage/export | Dùng được ngay để mô tả hypotheses, raw labels, observation/frame/pose support và nguyên nhân filter. Không có human identity/class GT mới gắn với 25 detections. |
| Scope Bridge 00802 | Dùng kết luận đã có, không chạy lại trace/native replay: room polygons là world-XY projection; elevation/layer scope không được giữ tới room nodes. Interval-to-M3 join được kiểm chứng riêng nhưng gate single-interval không đạt. Direct-Z fields của binary replay cũ không hợp lệ. Gate fail **không chứng minh** scene có nhiều tầng. |

Object side có RGB-D world XYZ theo mét; điều đó không cung cấp room-side elevation scope. `floor_0`, P3 coverage hoặc pose Z không được dùng riêng lẻ để mở membership.

## 3. Metric contract

### Object identity và admission

- Khóa population là đúng 25 P2 observation IDs trong C2/D1 manifest. Coverage/retention chỉ tính trên các observation và identities được xác nhận trong population này; **không gọi là detector recall hoặc coverage toàn phòng**. Báo mẫu số 25, reviewed/uncertain/unreviewed, cùng các same-frame pairs riêng.
- Chấm association ở online hypotheses trước filter và final objects sau filter/merge trên cùng các observations có identity review chắc chắn.
- Tách false merge (một hypothesis chứa nhiều identity đã xác nhận), fragmentation (một identity bị chia qua nhiều hypotheses), và mất identity do filter. Với singleton, phân loại riêng: vật thật chỉ có một detection; vật thật bị fragmentation qua nhiều hypotheses; false merge; mixed/invalid mask; uncertain. Không suy số physical objects từ số hypotheses. Uncertain không bị ép vào cặp đúng/sai.
- Giữ pairwise cross-frame scoring contract; same-frame duplicate consistency báo riêng. Detection count không phải số view độc lập. View support dùng exact-pose grouping đã khóa; revisit/alias không cộng thêm view.

### Object semantics

- So raw detector label với class người review trên đúng observation; báo canonicalization/version riêng. Không suy class từ identity token.
- Báo raw class history, semantic consensus/status, category `unsupported` và object-level result tách biệt. Chỉ dùng consensus output của policy hiện có; không thay bằng majority vote frame-level. Nếu baseline chưa có consensus artifact thì ghi unavailable.
- `fused_semantic_label` unavailable hiện tại được giữ nguyên; không coi label đúng của room là bằng chứng object class đúng.

### Room semantics

Không chấm room accuracy, membership hay attribution end-to-end khi scope unavailable. Khi có scope hợp lệ/được duyệt, room annotations phải độc lập với predictions và gắn với đúng polygon/revision. Ledger lưu đóng góp theo object để có thể retract khi object/class/membership đổi; nhưng **category cap là một contribution cho mỗi unique eligible category trong mỗi room**, không phải một contribution cho mỗi object/frame. Báo accuracy trên rooms đã commit, commit coverage/abstention, wrong-commit rate, label flips, số observations tới nhãn đúng ổn định và contributions theo category. Unknown/ambiguous không tính là thành công; không ép mọi room phải có label. Tách rõ verified-scope và assumed-scope results.

## 4. Một experiment/review đầu tiên

**Blinded review cho đúng 25 P2 observations của baseline HM3D 00802.** Đây là population nhỏ của một development route, không phải review mọi scene. Tạo cards từ RGB/mask overlay/crop hiện hành, hiện full observation ID và frame/pose; ẩn predicted class, hypothesis membership/ID, consensus và room scores. Người review gán physical identity token/uncertain, class/uncertain, mask purity/uncertain như các trường độc lập; cùng token biểu thị cùng physical object. Annotation chỉ evaluator; không sửa predictor. Gallery có side-by-side compare, và manifest liệt kê same-frame pairs riêng.

**Bước 1 đã bàn giao:** [gallery](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1/review_gallery.html), [annotation template](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1/annotation_template.json), [population/hash manifest](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1/population_manifest.json), [package validation](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1/package_validation.json). Có đúng 25 IDs, 16 detection frames, 16 pose matrices phân biệt (không gọi là independent views), và 10 same-frame pairs. Từng RGB, mask, overlay, crop và transform đã được đối chiếu với C2/P2; 100 panel paths là duy nhất. Package SHA-256: `b8462e60557e29f7fed6aca875d36d4d6ba8e7417f2536bf079e53770c32ccd8`.

**Bước 2 hoàn tất:** response được lưu nguyên byte tại [review_response_v1.json](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1_scoring/annotations/review_response_v1.json), cùng [annotation manifest](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1_scoring/annotations/annotation_manifest_v1.json). SHA-256 response: `8405fdc8b124c05cbc745893f4cb273023f9d9f3bf6b3fd1d45a8ee66a33668b`. Scorer model-free đã lưu tại `scripts/score_hm3d_p2_blind_review.py`; report, JSON metrics, class comparison, singleton breakdown, same-frame pairs và hashes nằm trong [scoring package](../artifacts/human_review/hm3d00802_shared_20261017_p2_review_v1_scoring/).

Trong population này, 9/20 observation có identity xác nhận được giữ; trong 12 identity token hợp lệ, 1 được giữ trọn, 1 giữ một phần và 10 không có observation nào trong final graph. Association trên 183 cặp cross-frame: precision 1.000, recall 0.789, F1 0.882; same-frame consistency được chấm riêng trên 7/10 cặp có annotation đủ dùng. Một identity (`armchair_1`) bị chia qua hai online hypotheses. So sánh raw class string chính xác là 2/20; đây không tự động là 18 lỗi phân loại vì chưa áp dụng taxonomy/alias mapping. Mười lăm hypothesis bị filter đều singleton; trong đó review ghi nhận chín identity một-detection với mask pure, một mask mixed, một fragment của identity lặp, và bốn trường hợp incomplete/uncertain/unreviewed. Đây là kết quả của đúng population 25, không phải detector recall hay chất lượng toàn phòng.

Annotation incompleteness được giữ rõ, không tự sửa: hai identity rows `confirmed` nhưng thiếu token; một row `unreviewed` có token; hai class labels có text nhưng `class_status=unreviewed`. Chúng không được tính vào metric tương ứng. Room review/scoring không chạy; không bật assumed scope.

**Một bước tiếp theo được đề xuất, chưa chạy:** route continuation ngắn trên HM3D 00802, chọn chỉ từ navmesh/free-space geometry trước inference; một C2 mới + một D1, giữ nguyên `obj_min_detections=3`, model, resolution, seed, matcher và thresholds. Mục đích là xem các identity singleton/mask-pure có nhận thêm quan sát và được giữ lại khi evidence tăng tự nhiên hay không; route khác làm population khác nên đây không phải ablation có kiểm soát. Cần preflight và duyệt ngân sách route-specific trước khi chạy. Không có thay đổi ngưỡng trong đề xuất.

Engineering cho việc lưu response, chấm và xác thực report hoàn tất trong lượt đã duyệt. Không chạy model, capture, mapping, native replay hoặc room scoring.

## 5. Quyết định membership cần người dùng chọn

| Phương án | Bằng chứng/giả định và code | Acceptance/giới hạn | Ngân sách sơ bộ |
|---|---|---|---|
| **A — verified scope** | Chỉ mở khi có scope room-side thật theo từng room/revision (nguồn/hash, scene/sequence, frame hiệu lực, world frame, mét) và hybrid object prefix có scope tương thích. Code nhỏ chỉ truyền contract có thật vào adapter/RB1; không gắn `floor_0` mặc định. Hiện chưa thấy source đó trong export 00802. | Membership/semantic output chỉ được gọi verified sau khi scope gate, prefix/retract tests và scene/hash checks đạt. Nếu producer hiện tại không thể xuất scope căn cứ, dừng ở blocker; cần thiết kế producer riêng trước khi định ngân sách chạy. | 30 phút read-only source check; nếu scope tồn tại, 1–2 giờ bridge/tests. Nếu phải sửa producer hoặc native replay thì báo ngân sách riêng trước. |
| **B — assumed single-level, experimental** | Chỉ chạy sau khi người dùng duyệt tường minh. Thêm lựa chọn opt-in riêng cho XY greedy support với giả định toàn bộ object/room population thuộc một level tương thích; ghi `assumed_single_level`, source hashes và assumption vào manifest/viewer. Không thay default verified-only, không trộn metrics. | Có thể tạo membership/room-belief demo cho population này nhưng không phải verified multi-floor/single-floor evidence, không dùng làm verified metric hoặc quality claim tổng quát. Giữ thresholds, RB1 ledger/category cap và bốn A variants. | Khoảng 2–3 giờ cho explicit config/manifest, adapter/controller integration và targeted tests; static run không cần perception/mapping lại. |

Không chọn A/B thay người dùng. Room-label evaluation trên dữ liệu thật phụ thuộc quyết định scope này; không chặn object review ở mục 4.

## 6. Điểm dừng và trạng thái kế hoạch

- Dừng quality experiment sau một review 25 observations và một report; không sweep thresholds, đổi matcher/A table hoặc chạy scene khác.
- Dừng membership/room scoring khi scope thiếu hoặc sai hash. Không hạ gate để có nhãn.
- Hạ tầng C2/D1/D2 đã nghiệm thu cho pilot HM3D 00802; điều đó không đồng nghĩa quality gate, room membership, room labels hay portability mọi map đã đạt. Theo kế hoạch chính, đây là quality planning phụ trợ cho mốc A/B; không mở B–E mới.
- Hai khoản infrastructure debt được giữ tách biệt, không chặn plan này: **(1)** bootstrap hashes cũ của `live_head.json`/`live_index.json` trong trường viewer metadata; hashes cuối của immutable outputs đã được xác minh riêng. **(2)** launcher chưa tiếp tục D1 từ C2 completed sau lỗi bootstrap; invocation mới chạy C2 mới. Không refactor hai khoản này trong quality work.
