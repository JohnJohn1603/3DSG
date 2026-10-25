Nhiệm vụ: Xây dựng GT và evaluator độc lập cho pipeline scene graph
1. Mục tiêu và phạm vi
Tôi muốn đánh giá độ chính xác của pipeline hiện tại trước khi triển khai Value Map hoặc navigation.
Hãy xây dựng một bộ ground truth nhỏ nhưng đầy đủ cho đúng scene apt_0 và capture hiện tại, cùng evaluator để đánh giá riêng:
1. Room segmentation và room boundaries.
2. Object detection/mask.
3. Mask + depth → observation 3D.
4. Object association qua thời gian.
5. Object–room membership.
6. Room semantic labels và room adjacency.
Sau khi GT/evaluator của apt_0 được kiểm chứng, mở rộng sang apt_1/apt_2.
Đây là nhiệm vụ xây dựng bộ đánh giá, chưa phải tối ưu thuật toán.
2. Nguyên tắc bắt buộc
- Giữ nguyên model, mapping, segmentation, M6 association và các threshold baseline.
- Không thêm hysteresis, centroid fusion hoặc thuật toán sửa kết quả.
- Không triển khai Value Map, navigation, SLAM hoặc chuyển dataset trong nhiệm vụ này.
- Không ghi đè captures/checkpoints/artifacts hiện có.
- GT chỉ được evaluator sử dụng; không đưa GT room polygons, object IDs/classes/centers vào builder.
- Không lấy output M3/M4/M8 hoặc nhãn do room labeler dự đoán làm GT.
- Phân biệt rõ:
  - annotation đề xuất bởi AI;
  - annotation đã được người xác nhận;
  - dữ liệu tham chiếu trích xuất từ simulator đã kiểm chứng.
- Chưa có GT phù hợp thì metric phải là null kèm lý do, không tự suy ra accuracy.
- GT pose vẫn được dùng như baseline hiện tại. Kết quả là đánh giá có điều kiện theo pose đúng, không đánh giá SLAM.
- Không dừng ở việc tạo CSV trống hoặc thêm ảnh audit. Phải hoàn thành công cụ, GT extraction, evaluator và hướng dẫn xác nhận annotation.
3. G0 — Kiểm kê và khóa đầu vào
Đọc code, manifests và status hiện tại trước khi làm.
1. Xác định chính xác:
   - scene instance;
   - stage/object assets;
   - capture;
   - sensor intrinsics/extrinsics;
   - coordinate transforms;
   - model/cache;
   - pipeline config;
   - source/build fingerprints.
2. Dùng run apt_0 route dài làm run đánh giá đầu tiên, sau khi xác minh đường dẫn và tính toàn vẹn.
3. Kiểm kê object thực sự được load, không chỉ các entry khai báo trong JSON.
4. Ghi rõ articulated objects nào hiện diện, trạng thái nào được tái hiện và trường hợp nào không được hỗ trợ.
5. Tạo thư mục evaluation riêng, run ID duy nhất, manifest có checksum.
6. Viết evaluation_protocol trước khi xem kết quả metric, gồm:
   - định nghĩa room;
   - phạm vi object/classes;
   - visibility eligibility;
   - coordinate conventions;
   - matching rules;
   - ignore/ambiguous policy;
   - metric definitions.
Không tuyên bố checkpoint đã đáp ứng những điều chưa kiểm tra.
4. G1 — Tạo Room GT độc lập cho apt_0
4.1 Phân biệt đối tượng đánh giá
Tách rõ:
- Architectural room footprint: vùng sàn thuộc phòng theo kiến trúc.
- Observed free-space segmentation: phân vùng mà pipeline dựng từ dữ liệu đã quan sát.
Không mặc nhiên coi hai loại polygon này phải trùng nhau dưới đồ đạc hoặc ở vùng chưa quan sát.
4.2 Chuẩn bị mặt bằng tham chiếu
Từ source stage mesh và transform thật của scene:
- Tạo mặt bằng metric trong canonical world.
- Hiển thị sàn, tường, cửa và các chi tiết kiến trúc cần thiết.
- Có chế độ ẩn nội thất để annotate kiến trúc.
- Có ảnh hoặc góc nhìn 3D bổ sung để xác nhận các chỗ mơ hồ.
- Lưu phép biến đổi pixel ↔ world; kiểm tra một số điểm mốc và khoảng cách.
- Không dùng occupancy/footprints dự đoán làm nền GT.
4.3 Chuẩn bị phương tiện annotate thực dụng
Cung cấp công cụ hoặc workflow đơn giản để tôi:
- Click tạo/sửa room polygons.
- Đặt GT room ID.
- Đánh dấu doorway và adjacency.
- Đánh dấu ignore/ambiguous regions.
- Lưu và mở lại annotation.
Ưu tiên công cụ nhẹ, tái sử dụng thư viện sẵn có; không xây frontend lớn nếu không cần.
Quy ước đề xuất, cần tôi xác nhận:
- Polygon theo mặt trong tường.
- Đường chia tại doorway được xác định rõ.
- Corridor có thể là vùng riêng theo quy ước đã chốt.
- Không tự chia open-plan thành nhiều phòng chỉ dựa trên furniture.
- Tách functional labels khỏi architectural partition khi cần.
- Furniture không tự tạo lỗ trong architectural room polygon.
- Holes chỉ dùng cho cấu trúc phù hợp như cột hoặc khoảng sàn không tồn tại.
4.4 Kiểm chứng annotation
- Khi annotate/review, mặc định ẩn prediction để giảm thiên lệch.
- Kiểm tra polygon validity, overlap, holes, frame và units.
- Lưu annotator/reviewer, trạng thái xác nhận, version và scene checksum.
- AI có thể đề xuất polygon, nhưng không tự nâng nhãn đó thành human-verified GT.
- Chuẩn bị một gói review rõ ràng để tôi xác nhận, không yêu cầu tôi tự đo tọa độ hoặc suy đoán từ ảnh thiếu thông tin.
- Nếu cần người dùng xác nhận, gom câu hỏi theo một lượt review; tiếp tục các phần evaluator không phụ thuộc câu trả lời.
5. G2 — Object GT và visible-instance masks
5.1 Registry object thực sự tồn tại
Tạo bảng evaluator-only gồm:
- Stable GT instance ID.
- Runtime handle/ID và asset reference.
- Canonical class, nguồn class mapping và trạng thái review.
- Transform đầy đủ, gồm scale.
- Geometry/bounding box và phương pháp tính.
- Articulation state nếu có.
- Trạng thái loaded/supported/excluded cùng lý do.
Không coi asset translation là centroid. Nếu báo center error, phải định nghĩa rõ center của geometry nào.
Không coi object không được nạp là false negative của detector.
5.2 Visible-instance masks theo capture
Tại đúng pose, intrinsics, resolution và scene state của từng frame:
- Render hoặc trích xuất visible instance-ID masks.
- Xác minh mapping rendered ID → GT instance.
- Kiểm tra ID có thật sự duy nhất theo instance hay chỉ theo class.
- Xử lý occlusion bằng full scene.
- Kiểm tra alignment với RGB/depth trên nhiều object/frame.
Nếu cần gán instance IDs trong bản scene dành riêng cho evaluator:
- Không thay đổi geometry, pose hoặc visibility.
- Kiểm chứng tính tương đương với capture.
- Không chuyển IDs đó sang input builder.
Nếu runtime hiện tại không cung cấp mask đúng, báo blocker cụ thể và dùng annotation thủ công cho tập frame giới hạn. Không tạo pseudo-GT từ chính YOLO.
5.3 Visibility và vocabulary
Chốt trước:
- Object thế nào được coi là đủ nhìn thấy để chấm detector.
- Cách xử lý object quá nhỏ, occluded, truncated.
- Classes model hỗ trợ/không hỗ trợ.
- Synonym mapping.
Lưu cả các instance nhìn thấy nhưng detector bỏ sót, không chỉ annotate những detection đã có.
6. G3 — GT hình học object và lineage
Tách hai mức:
Observation geometry
So observation từ predicted mask với reference từ GT visible mask và observed depth.
- Cùng K, depth convention và coordinate frame.
- Báo center/extent hoặc point-distance metric theo định nghĩa rõ.
- Không lặp lại toàn bộ clustering P3 rồi gọi kết quả đó là kiểm chứng độc lập của P3.
- Reference không hợp lệ phải được đánh dấu, không tạo điểm GT giả.
Persistent object geometry
So ObjectNode với geometry toàn instance theo định nghĩa đã chốt.
- Tách first-view/partial-surface bias khỏi lỗi coordinate.
- Không đánh đồng visible-AABB center với full-object centroid.
Detection-to-instance lineage
Ghép detection với GT visible instances theo quy tắc mask/class/matching đã định nghĩa.
Lưu:
- matched GT instance;
- unmatched prediction;
- missed eligible GT instance;
- ambiguous association;
- matching score.
GT lineage chỉ evaluator thấy, không dùng làm key M6.
7. G4 — Evaluator theo từng tầng
7.1 Room geometry
Đánh giá riêng:
- Raw/tracked regions M4/M5.
- RoomNode footprints M8.
Mục tiêu là phân biệt lỗi segmentation với promotion/relink/anchoring.
Báo hai chế độ:
Full-scene evaluation
- So với toàn bộ GT room footprints.
- Báo missing rooms và coverage.
- Nêu rõ kết quả chịu ảnh hưởng vùng chưa quan sát.
Observed-domain evaluation
- Dùng một evaluation domain chung cho prediction và GT.
- Domain được xác định từ capture/visibility với quy tắc cố định, không chỉ từ vùng prediction thành công.
- Không tính cạnh nhân tạo do clipping domain như boundary thật.
Metrics tối thiểu:
- Room IoU qua matching một-một.
- Số phòng matched/missed/unmatched.
- Over-segmentation và under-segmentation theo định nghĩa overlap đã ghi.
- Boundary precision/recall/F1 với tolerance chốt trước.
- Coverage.
- Adjacency precision/recall sau room matching.
Định nghĩa rõ matching objective và thresholds. Không chỉ báo mean IoU của các cặp tốt rồi bỏ các phòng unmatched.
Không tự align prediction bằng translation/rotation để tối đa điểm; GT-pose evaluation phải dùng frame đã kiểm chuẩn. Nếu có phép alignment bổ sung, báo riêng.
7.2 Object detection
- Precision/recall trên eligible visible instances.
- Mask IoU của các cặp matched.
- Báo theo class và tổng.
- Ghi counts của false positives, false negatives và ignored cases.
Hai mask pilot trước đó không được dùng làm đại diện toàn scene.
7.3 P3 lifting/filtering
Phân biệt:
- True detection được P3 giữ.
- True detection bị P3 loại.
- False detection bị giữ hoặc bị loại.
- Trường hợp chưa đủ reference.
Báo geometry error của observation được giữ.
Không gọi tỷ lệ Detection3D/Detection2D là recall.
7.4 Association
Dùng GT lineage để đo:
- Một GT instance bị chia thành nhiều predicted tracks.
- Một predicted track chứa observation của nhiều GT instances.
- ID consistency khi tái quan sát.
Không dùng “không có hai node gần nhau” làm bằng chứng loại trừ false merge.
7.5 Membership
- Match predicted objects với GT instances.
- Match predicted rooms với GT rooms.
- Đánh giá room assignment.
- Báo riêng unassigned, ambiguous, stale và object thuộc predicted room không match được.
- Với object sát cửa/open-plan, dùng quy ước GT đã chốt; không tự loại sau khi thấy sai.
- Có cả metric conditional trên matched entities và thống kê toàn bộ eligible objects để không che lỗi upstream.
7.6 Room semantics
- Label accuracy/confusion matrix trên các room có annotation và matching phù hợp.
- Báo label coverage, unknown/ambiguous rate.
- Báo matched-room coverage và missing rooms.
- Không coi semantic label do chính pipeline suy ra là GT.
8. G5 — Kiểm chứng evaluator
Trước khi chấm apt_0, tạo fixtures nhỏ có đáp án biết trước:
- Perfect prediction.
- Một room bị split.
- Hai room bị merge.
- Missing room và extra room.
- Boundary lệch có kiểm soát.
- Duplicate object track.
- Hai GT instances bị gộp thành một track.
- Object bị gán sai room.
- Empty prediction, empty eligible GT và toàn bộ ignored.
- Coordinate/scale mismatch.
Kiểm tra kết quả metric đúng với định nghĩa, không chỉ script chạy không lỗi.
Khóa version evaluator và annotation; lưu effective config vào manifest.
9. G6 — Chấm apt_0 rồi mở rộng
Sau khi GT apt_0 được xác nhận:
1. Chạy evaluator trên baseline hiện tại.
2. Xuất bảng metrics có mẫu số và uncertainty/ignore reasons.
3. Tạo overlay dễ đọc:
   - GT vs predicted room boundaries;
   - GT vs predicted object locations;
   - các false merge/split;
   - membership đúng/sai;
   - semantic labels.
4. Xếp lỗi theo tầng và độ ảnh hưởng.
5. Chưa sửa thuật toán trong lượt xây evaluator này.
Sau đó áp dụng sang apt_1/apt_2:
- Chỉ reuse room annotation sau khi xác minh geometry và frame tương ứng.
- Nếu không tương ứng, annotate riêng.
- Giữ cấu hình baseline/evaluator chung.
- Ghi rõ đây là các biến thể ReplicaCAD, chưa phải generalization đa kiến trúc.
10. Artifacts cần bàn giao
Cấu trúc có thể điều chỉnh theo repo, nhưng phải có tương đương:
evaluation/
├── protocol.md
├── gt/
│   └── apt_0/
│       ├── manifest.json
│       ├── room_polygons.geojson
│       ├── doorways_and_adjacency.json
│       ├── ignore_regions.geojson
│       ├── object_registry.json
│       ├── class_mapping.json
│       ├── visibility_index.json
│       └── annotation_review.json
└── runs/
    └── <unique_run_id>/
        ├── manifest.json
        ├── room_matching.json
        ├── object_matching.json
        ├── detection_lineage.jsonl
        ├── metrics.json
        ├── metrics.csv
        ├── failure_cases.json
        ├── overlays/
        └── report.vi.md
Nếu dùng GeoJSON cho tọa độ local metric, phải ghi rõ đây là canonical local frame theo mét, không phải kinh độ/vĩ độ.
11. Điều kiện nghiệm thu
Chỉ gọi bộ đánh giá apt_0 hoàn thành khi:
- GT room polygons có quy ước và trạng thái xác nhận rõ.
- Object registry và visible-instance mapping được kiểm chứng.
- Evaluator có tests với đáp án biết trước.
- GT không đi vào pipeline prediction.
- Có kết quả định lượng cho những phần GT hỗ trợ.
- Không có metric bị thiếu được thay bằng số 0.
- Có báo cáo lỗi cụ thể theo tầng, không chỉ số nodes hoặc tests pass.
Nếu annotation đang chờ tôi xác nhận, ghi “evaluator sẵn sàng, GT đang chờ review”, không tuyên bố accuracy đã nghiệm thu.
12. Cách làm việc
Bắt đầu bằng G0 và một kế hoạch file/module ngắn dựa trên repo hiện tại. Sau đó triển khai tuần tự; tái sử dụng capture và artifacts sẵn có, không chạy thêm simulation nếu chưa cần.
Mỗi giai đoạn báo:
- Đã làm gì.
- Bằng chứng và artifacts.
- Tests thực sự đã chạy.
- Những lựa chọn cần tôi xác nhận.
- Phần nào vẫn chưa thể kết luận.
Mục tiêu cuối là biết pipeline đúng/sai ở đâu và đo được mức cải thiện sau này — không phải tạo GT để làm kết quả baseline đẹp hơn.