# Hướng dẫn review membership và P3

Ngày: 2026-10-02  
Nguồn: `artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/membership_review/`

Đây là review AI-assisted/provisional trên artifacts đã có. Nhãn người review
được dùng để làm rõ chất lượng, không phải ground truth độc lập nếu chưa có
quy ước và kiểm tra riêng.

## A. Ba membership events

Mở từng ảnh `membership_event_*.png`. Hai hàng của mỗi ảnh dùng cùng world
scale: RGB/mask ở bên trái và room overlay ở bên phải.

Với mỗi event, chỉ cần trả lời những mục nhìn thấy được:

1. `same_physical_object`: `same`, `different`, hoặc `uncertain`.
2. `object_area`: mô tả khu vực vật nằm trong ảnh; nếu không xác định được,
   ghi `uncertain`.
3. `room_change`: `reasonable`, `not_reasonable`, hoặc `uncertain`.
4. `evidence_note`: một câu chỉ ra RGB/mask/overlay nào hỗ trợ nhãn.

Chọn `uncertain` khi một trong các điều sau xảy ra:

- trước hoặc sau không nhìn thấy vật;
- mask bị che/cắt quá nhiều hoặc không phân biệt được vật với nền;
- không có depth/geometry/reference đủ để xác định vị trí;
- room boundary chỉ thay đổi trên overlay nhưng không có reference đáng tin
  để đánh giá đúng/sai.

Không suy ra “room change hợp lý” chỉ vì ID object được giữ; cũng không suy ra
“sai” chỉ vì room ID thay đổi. Câu hỏi này là đánh giá chất lượng phòng, tách
khỏi cơ chế relink/membership đã thực thi.

## B. Mười sáu mẫu P3

Mở `p3_review_sheet.png` và đối chiếu ledger/CSV tương ứng. Đánh giá độc lập
với cột `p3_decision` hiện có.

Các nhãn đề nghị:

- `detection_truth`: `correct`, `wrong_class_or_instance`, `absent`,
  `uncertain`;
- `mask_quality`: `usable`, `partial_but_usable`, `not_usable`, `uncertain`;
- `depth_3d_quality`: `usable`, `fragmented_but_usable`, `not_usable`,
  `uncertain`;
- `p3_decision_review`: `reasonable_filtering`, `too_strict`, `too_lenient`,
  `uncertain`.

Diễn giải quan trọng:

- `accepted` chỉ có nghĩa cloud vượt điều kiện lọc hiện tại; không chứng minh
  detection đúng hoặc object được nhận diện chính xác.
- `rejected_cluster_below_minimum` chỉ có nghĩa cloud không đủ theo contract
  P3; không chứng minh object không tồn tại trong RGB.
- Chọn `uncertain` nếu ảnh không đủ phân biệt object, mask hoặc depth; không
  cần đoán để lấp ô trống.

## C. Cách kết thúc review

Có thể xác nhận từng mẫu đủ rõ và để các mẫu còn lại `uncertain`. Việc chưa
review đủ 19 mẫu không chặn các công việc khác của dự án. Chỉ khi có nhãn đã
được xác nhận mới dùng chúng để tổng hợp lỗi theo tầng và cân nhắc tối đa một
thay đổi có kiểm soát. Nếu chưa có lỗi được xác nhận, giữ baseline.

Không dùng guide này để thay đổi association radius, thêm hysteresis hoặc hạ
clustering threshold chỉ nhằm làm output ổn định hơn.
