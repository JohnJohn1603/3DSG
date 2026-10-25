# U6 HM3D 00808 — protocol v1.1, khóa trước predictor

Ngày khóa route/protocol: 2026-10-10. Trạng thái: route được khóa; chưa chạy capture/P2/P3/predictor. Scene `00808-y9hTuugGdiq` độc lập với kết quả U3/U4 đã dùng ở 00800/00802. Ảnh preview kiến trúc 00808 đã được xem trong quá trình chọn route; đây không phải blind scene cho người review, nhưng chưa có predictor output trên scene này.

## Route và capture

Cấu hình đầy đủ: [`hm3d_minival_v02_00808_u6_quality_v1_640.json`](../configs/habitat/hm3d_minival_v02_00808_u6_quality_v1_640.json). 5 frame chính xác, dưới mức trần 48:

| Frame | Pose | Mục đích đã định trước |
|---:|---|---|
| 0 | `(9.062847, -2.738609, 2.955713)`, yaw `0` | Ghế sofa/bàn thấp, góc A |
| 1 | `(7.338223, -2.738609, 4.118603)`, yaw `0` | Góc B, dịch ngang |
| 2 | `(5.619532, -2.738609, 2.952104)`, yaw `0` | Góc C, phía đối diện; thấy vùng ăn/kitchen lân cận |
| 3 | `(3.411918, -2.738609, 3.762519)`, yaw `π/2` | Detour sang tủ bếp/vùng kế cận |
| 4 | Lặp chính xác pose frame 0 | Revisit để kiểm tra repeatability; không tính là góc nhìn độc lập |

Waypoint được chọn từ RGB preview và lấy mẫu navmesh, không dùng predictor output, semantic IDs/categories, room labels hoặc GT. RGB preview đúng route ở [`route_rgb_preview.png`](../artifacts/audits/u6_hm3d_00808_v1/route_rgb_preview.png). Validator Habitat-Sim đã xác nhận 5/5 pose navigable và cả 4 đoạn liên tiếp connected. Camera centers frame 0/1/2 khác nhau; ba preview cho thấy sofa/bàn thấp tiếp tục hiện diện ở các góc khác nhau. Frame 3 nhìn sang vùng tủ bếp. Frame 4 là exact-pose revisit.

Sensor khóa: RGB-D 640×480, HFOV 90°, fx=fy=320 px, principal point `(319.5,239.5)`, Habitat sensor offset `[0,1.5,0]`, orientation `[0,0,0]`, depth 0.1–10 m, axial meters, simulation step 0.1 s. Dùng detector profile/weights/preprocessing hiện hành và P3 stride=1, eps=0.02 m, min_points=100, fallback backend hiện tại. U3/U4-OV model, vocabulary, prompts và threshold không đổi; appearance không nằm trong so sánh chính.

## Evaluator-only semantic render gate

Sau RGB-D capture nhưng **trước P2/P3**, renderer độc lập sẽ dựng semantic ID maps ở đúng năm pose đã lưu. GT không được truyền vào predictor. Chấp nhận render chỉ khi tất cả điều kiện sau đạt ở từng frame: RGB cùng kích thước, mean absolute error ≤2, p99 ≤16, tỷ lệ pixel khác ≤5%; valid-depth mask bằng nhau, max depth error ≤0.03 m; sensor pose position/rotation sai số tối đa `2e-5`; mọi semantic ID khác 0 phải có trong descriptor. Nếu gate thất bại, dừng trước P2/P3 và điều tra provenance/assets; không nới tolerance.

Ignore policy khóa theo evaluator v1: semantic ID 0 là void; descriptor `unknown`/`unlabeled` và ID không có descriptor là unavailable/ignored (ID lạ làm render gate fail); architectural ignore categories chính xác là `ceiling`, `door`, `door frame`, `floor`, `wall`, `window`. Không mở rộng ignore list sau khi xem kết quả. Prediction bị bỏ khỏi metric nếu ≥50% pixel của mask rơi trên void/architectural/unsupported vùng theo quy tắc evaluator. Instance ID được xem là lineage annotation của mesh cho metric tự động, không khẳng định physical identity hoàn hảo; ID có dấu hiệu tách/gộp sai được gắn uncertain và đưa vào diagnostic review riêng.

## Tập chấm chính A và tập diagnostic B

**A — primary scoring population:** toàn bộ visible GT object observations trong cả 5 frame có semantic ID hợp lệ, descriptor category không thuộc ignore list và mask area ≥32 pixels. Không lấy mẫu theo predicted class, track, cosine, disagreement hay mode thắng. Nếu phải human-audit GT lineage, chọn deterministic theo SHA-256 của `(protocol_id, scene_id, frame_id, semantic_id)` phân tầng theo frame/category/visibility; ngưỡng và danh sách IDs sẽ được xuất trước khi mở nhãn predictor. Tất cả eligible observations vẫn ở automated primary population; human audit subset báo riêng, không thay mẫu số chính.

**B — targeted diagnostics:** sau khi predictions đã khóa, gồm disagreement/unmatched/deferred/conflicted, candidate mixed tracks, cùng các merge/fragmentation nghi vấn. Đây là gói chẩn đoán có chủ đích; không gộp metrics B vào accuracy/precision đại diện của A.

## Matching, identity và denominator

- Matching mask per-frame, class-agnostic Hungarian one-to-one, IoU ≥0.50; minimum visible GT area 32 pixels. Dưới ngưỡng/architectural/void/unmapped được đếm riêng, không biến thành FN. Unmatched prediction/GT được báo riêng; không coi unmatched prediction là association đúng.
- P2 masks được match với GT trước khi xem class. P3 accepted/rejected counts và GT coverage được báo riêng. Association conditional metric chỉ trên observation IDs có GT mask match và P3 accepted; end-to-end table bổ sung các eligible GT observations bị P3 reject/unmatched như không có track, để không che P3 attrition.
- Association pair scope là mọi unordered pairs giữa hai **khác frame** trong cùng sequence, khi cả hai GT observations thuộc primary population. Cùng physical GT ID và cùng predicted track = TP; khác GT IDs nhưng cùng predicted track = FP/false merge; cùng GT ID nhưng tracks khác hoặc một phía deferred/unmatched = FN, đồng thời deferred/unmatched được báo riêng. Same-frame pairs không vào pairwise continuity denominator; duplicate/same-frame collisions được báo ở bảng diagnostics riêng.
- Coverage: báo tổng eligible GT observations, P2 mask-matched, P3 accepted, cặp identity đủ annotation, cặp association được chấm và số physical semantic IDs độc lập. Fragmentation của mỗi GT identity là `max(0, distinct assigned tracks - 1)` trên accepted/matched observations. Track có nhiều GT IDs confirmed là mixed-identity/false merge; không dùng majority label để chấm class.

## Class outputs và exact-label rule

Raw YOLO, U4 raw-label voting, U4-OV single-view, U4-OV causal multi-view/final-map được chấm thành các đầu ra riêng. Primary class comparison dùng literal exact-label equality sau chuẩn hóa cố định: lowercase, trim, thay spaces bằng underscores; không synonym, không parent-child mapping, không `table=dining_table`, không tự đổi `couch=sofa` ngoài canonical output đã có trong detector profile. Mapping file [`hm3d_00808_object_category_mapping_v1_1.json`](../configs/evaluation/hm3d_00808_object_category_mapping_v1_1.json) giữ mọi category `unmapped`; không có user-confirmed mapping. Exact-label results là diagnostic theo tên category, không được gọi là taxonomy-mapped accuracy.

Mỗi output head có vocabulary cố định trước run. **Supported exact-label denominator** là số eligible GT observations có category đúng literal nằm trong vocabulary head đó, xác định từ GT/vocabulary trước khi đọc predicted label; không phụ thuộc class mà predictor xuất. Báo correctness trên mẫu số supported này. **All-GT coverage** dùng toàn bộ primary confirmed/eligible GT observations làm denominator và báo số có output vs unknown/conflicted/abstain; nó không phải accuracy. **Unsupported/uncertain**: báo GT categories ngoài mỗi vocabulary, GT unclear IDs, predictor label ngoài evaluator taxonomy, và unknown/conflicted riêng; giữ counts trên tổng population. Không rút unsupported khỏi all-GT coverage.

Causal class tại observation frame `t` chỉ dùng evidence ≤`t`. Final-map classification báo riêng theo predicted track và GT identity; mixed-identity tracks luôn nằm trong tổng, status mixed/false-merge, không làm sạch bằng majority. Exact-pose frame 4 không tăng independent-view support. Cosine/softmax không phải calibrated probability. Room correctness unavailable vì chưa có independent room-function/polygon GT; chỉ xuất membership/evidence changes nếu có.

## Sampling/review và quyết định

Primary A dùng toàn bộ eligible GT population; human audit subset độc lập predictor được deterministic-hash chọn như trên. Diagnostic B chọn theo disagreement/unmatched/conflicted chỉ để giải thích lỗi. Review UI hiển thị observation IDs, RGB/mask trước; người review trả lời identity và class riêng, `uncertain` hợp lệ. Semantic instance IDs là evaluator evidence, không phải identity oracle tuyệt đối.

Không tuning sau khi xem output. Precision tăng đi kèm recall/coverage giảm phải báo là trade-off, không tự kết luận cải thiện. Một scene chỉ hỗ trợ quyết định thử nghiệm, không promote toàn cục. Source giữ mặc định; geometry-only experimental; U4-OV diagnostics-only. Dense 00802 vẫn pending, không block U6. Lượt này không có paired native graph replay; offline U3/U4 quality scoring không thay graph integration test.

## Ngân sách dự kiến cho 5 frame

Ước lượng theo đo thực trên 640×480 cùng pipeline: capture ~55 s, P2 ~35 s, P3 fallback ~311 s (đo trực tiếp trên 5-frame 00800 candidate640), U2 30-crop ~14 s, U4-OV ~23 s, U3 association <1 s, semantic renderer khởi tạo+các frame ~20–90 s dự kiến, evaluator/exports ~1–3 min. Tổng offline/capture dự kiến khoảng 8–12 min, dưới 30 min; không có native replay trong budget này. Runtime P3 phụ thuộc point density; không đổi clustering/backend nếu chậm. Nếu profile thực tế vượt 30 min trước bước dài tiếp theo, dừng và xin duyệt.
