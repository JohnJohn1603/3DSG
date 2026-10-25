# Nhiệm vụ: cải thiện phân phòng apt_0 bằng đánh giá có kiểm soát

## Mục tiêu và phạm vi

Làm việc trong `/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG`, đọc `docs/PHASE1_PLAN.vi.md` và các status liên quan trước khi thực hiện. Từ baseline 24-frame apt_0 và GT người dùng đã xác nhận, hoàn thành một vòng **chuẩn hóa GT → đo baseline → thử cải thiện → đo lại → chọn hoặc bác bỏ candidate**. Không dừng ở báo cáo audit nếu không gặp blocker thực sự.

Chưa chuyển map/dataset, chưa làm navigation/Value Map/SLAM; không thay detector hay M6. Giữ baseline source nguyên vẹn, mọi thay đổi thuật toán là profile extension riêng, không được gọi là source parity. Không bảo đảm tìm được tối ưu toàn cục hoặc buộc phải tăng điểm.

## 1. Khóa dữ liệu và giải quyết GT, không hỏi lại phần đã được cho phép

- GT nguồn: `/home/kiettran05/Downloads/room_polygons.geojson`; dùng bản đã khóa có hash khớp, không dùng các draft Z1–Z4 cũ. Người dùng xác nhận bốn polygon của bản này; các overlap nhỏ là lỗi thao tác và cho phép gán cho một trong hai vùng.
- Giữ raw nguyên byte. Tạo GT version mới, ghi provenance: người dùng xác nhận partition và cho phép giải quyết overlap; thuật toán chuẩn hóa thực hiện hình học, không phải người dùng vẽ lại từng vertex.
- Quy tắc cố định: ID số nhỏ hơn sở hữu phần overlap. Dùng polygon difference chính xác, không lấy convex hull hoặc buffer đại trà. Không chọn quy tắc dựa trên điểm prediction.
- Kiểm tra polygon hợp lệ, không overlap có diện tích dương, union trước/sau không đổi trong tolerance khai báo, không tạo khoảng hở mới, các room vẫn không rỗng. Chỉ sửa miền overlap được cấp phép. Xuất diện tích thay đổi từng room và overlay trước/sau.
- Không tự lấp khoảng trống sẵn có giữa các polygon: chúng chưa được annotate. Không đổi room_4 hoặc loại vùng cầu thang chỉ để tăng điểm. Functional labels/adjacency/object assignment chưa xác nhận vẫn để null.
- Khóa capture/config/model/native binaries/source commits và evaluator version; thư mục mỗi run riêng, không ghi đè dữ liệu cũ.

## 2. Chốt metric trước khi thử candidate

Đọc `scripts/evaluate_apt0_user_confirmed_partition.py` và `evaluation/metrics.py`. Báo cáo baseline cũ là provisional, không so trực tiếp với candidate theo GT/protocol mới; phải chấm lại cả hai.

- ROI chính là union GT đã chuẩn hóa, lattice world 0,05 m cố định. Cùng ROI cho mọi profile.
- Metric chính: one-to-one assignment tối đa tổng IoU; tính macro IoU trên **tất cả GT rooms**, room không match nhận 0. Báo thêm từng room, matched-only IoU, matching threshold và unmatched predictions trong ROI. Không để matched-only metric tăng giả khi bỏ sót phòng khó.
- Với ROI là union GT và predictions đã clip vào ROI, union-IoU bằng coverage; ghi rõ, không trình bày hai số như bằng chứng độc lập về chất lượng partition.
- Split/merge: báo ma trận diện tích giao và tỷ lệ theo cả GT/prediction. Tách giao nhỏ ở biên khỏi split/merge đáng kể bằng ngưỡng định trước, công bố sensitivity; không chỉ dùng điều kiện có ít nhất một cell giao nhau.
- Báo overlap giữa các predicted rooms nếu có. Phân biệt prediction nằm hoàn toàn ngoài ROI với extra prediction trong ROI; ngoài ROI là chưa được chấm, không tự coi là FP.
- Điểm toàn ROI thể hiện chất lượng cuối của route. Nếu có điểm trên vùng đã quan sát, dùng cùng mask coverage từ M3 baseline cho mọi candidate, báo riêng; không cho candidate tự thu nhỏ miền chấm để tăng điểm.
- Boundary metric chỉ dùng ranh giới đã có quy ước hợp lệ. Không tự coi cạnh ROI cắt cụt hoặc các khoảng trống annotation là tường. Nếu chưa đủ thì null, không chặn việc tối ưu IoU partition.
- Thêm regression evaluator cho: missing room, extra room ngoài/trong ROI, tiny overlap, split, merge và polygon sau normalization.

## 3. Tận dụng dữ liệu hiện có để khoanh vùng lỗi

- Khôi phục snapshot M8 từng revision từ `replay/delta_log.json`: RoomNode đã chứa `outer_ring`, `holes`, `anchor_region_id`, timestamp và revision. Đối chiếu snapshot cuối với artifact gốc. Không khẳng định thiếu lịch sử M8 trước khi thử cách này.
- M5: nếu chưa có polygon theo frame, bổ sung trace chỉ đọc tại ranh giới stage; xác minh trace ON/OFF không làm đổi snapshot/delta. Không gọi projection hoặc segmentation thêm chỉ để trace.
- Xuất chuỗi M3 mask → M4 preprocessing/raw regions → M5 tracked regions → M8 room footprints, bao gồm stale/reject và terminal flush. Tách frame, grid revision và graph revision.
- Baseline cũ có final M4/M8 cùng các IoU, nhưng điều đó chưa chứng minh mọi frame giống nhau. Đối chiếu hình học và ownership thực tế.
- Ưu tiên hai vấn đề thấy trong overlay: GT room_2 match thấp (~0,216 ở protocol cũ), và các đường chia xuyên vùng GT room_1 rộng hình chữ L. Phân biệt thiếu coverage/tường ở M3, morphology M4, đường cắt decomposition, với đổi ID/promotion M5/M8.
- Kiểm tra effective M4 threshold/tau và đơn vị m→pixel, morphology và chính sách unknown. Không chỉ đọc YAML nếu runner có default/override.
- Kiểm tra timestamp capture và maintenance 0,5 Hz: route này gồm 24 pose có timestamp sát nhau. Ghi rõ ảnh hưởng cadence/flush; không âm thầm retime baseline hoặc đổi scheduler để lấy điểm cao.
- Phân biệt vùng free-space do decomposition với phòng kiến trúc/open-plan người dùng annotate. Một phòng lõm hoặc hình chữ L không bắt buộc tương ứng một mảnh decomposition. Đây là giả thuyết cần kiểm tra bằng cut/mask, không tự kết luận từ hình ảnh.

## 4. Thử cải thiện có giới hạn, không chỉ tiếp tục audit

Giới hạn vòng đầu tối đa 6 candidate configurations, mỗi thí nghiệm chỉ đổi một yếu tố so baseline. Ghi giả thuyết và grid tham số trước khi xem điểm. Dùng cùng 24 packets, pose, detector cache, M2/M3 khi thử riêng M4.

Các hướng ứng viên, chọn theo bằng chứng ở bước 3:

1. Profile `extended_unknown_boundary` đã tồn tại: đối chứng xem hạn chế mask vào unknown có giúp partition hay chỉ làm mất coverage.
2. Một dải nhỏ threshold/tau quanh effective baseline, với đơn vị rõ ràng; không đặt theo số phòng mục tiêu.
3. Nếu mất cửa/tường do morphology: một extension điều chỉnh đúng phép morphology gây lỗi, dùng scale theo mét và có tests cửa hẹp/tường/vật cản; không sửa source reference.

Không mặc định phải thử đủ mọi hướng. Nếu có implementation bug chứng minh được, sửa tối thiểu và tách khỏi experimental policy.

Nếu các thí nghiệm trên không cải thiện và chứng minh decomposition đang chia một vùng mở thành nhiều mảnh, đề xuất tối đa một extension hợp nhất region dựa trên evidence hình học quan sát được (shared boundary, wall support, connector width). Không dùng GT polygons, room IDs, tọa độ apt_0, tên scene, GT mesh/navmesh/semantic IDs hoặc số bốn phòng trong thuật toán. Trước khi triển khai extension này, giải thích tiêu chí và kiểm thử chống gộp xuyên tường/cửa; tách rõ region decomposition và RoomNode grouping nếu cần thay contract.

GT chỉ dùng offline evaluator. Không nạp GT hoặc geometry kiến trúc chuẩn vào runtime predictor. Không ép ra bốn phòng, không đổi M6/hysteresis để che lỗi footprint.

## 5. Chọn candidate và kiểm chứng

- So baseline/candidates trên cùng protocol mới: macro IoU toàn GT, per-room IoU, coverage, split/merge có ý nghĩa, extras, temporal stability, rejection/stale, runtime/RSS.
- Mục tiêu development đề xuất: tăng macro IoU ít nhất 0,05 tuyệt đối, coverage không giảm quá 0,02 tuyệt đối; mọi room giảm IoU trên 0,05 phải được giải thích và không tự chọn làm mặc định. Đây là tiêu chí thực nghiệm đề xuất, không phải chuẩn từ paper.
- Nếu không đạt, báo trung thực candidate bị bác bỏ; không hạ ngưỡng sau khi xem kết quả. Một cải thiện nhỏ vẫn được báo đúng mức nhưng chưa promote.
- Candidate tốt nhất chạy fresh process ít nhất 3 lần, replay/reload/delta và full regression. Không tính ổn định ID bằng so raw ID giữa các run có topology khác; dùng matching hình học và báo lifecycle riêng.
- Giữ Webots two-room/corridor regressions hiện có để phát hiện regression, không dùng GT apt_0 để sửa chúng.
- Trước khi tuyên bố robust, thử thêm một route quan sát độc lập trong chính apt_0 với sensor/capture budget tương đương, không tuning lại theo route này. Nếu coverage khác, báo rõ. Không cần đợi hoàn hảo mới chuyển sang map khác sau checkpoint này.

## 6. Bàn giao và điểm dừng

Xuất GT normalized + validation/provenance; baseline mới; bảng candidate; cấu hình/hash/lệnh tái lập; time series M3–M8; overlay cạnh nhau dễ đọc; báo cáo candidate được chọn hoặc không có candidate đạt.

Kết luận phải phân biệt: sửa evaluator/GT, tăng coverage, cải thiện partition, cải thiện temporal stability. Không dùng số test pass hoặc số RoomNode làm chứng minh accuracy.

Sau một vòng tối đa 6 candidates và kiểm chứng finalist, dừng để báo kết quả, không tiếp tục audit/tuning vô hạn. Nếu nguyên nhân còn lại là thiếu quan sát, yêu cầu route bổ sung có mục tiêu thay vì sửa threshold mù. Nếu giới hạn là định nghĩa phòng hoặc decomposition, nêu rõ và đề xuất thay đổi thiết kế riêng. Chưa mở rộng apt_1/apt_2 trong nhiệm vụ này.
