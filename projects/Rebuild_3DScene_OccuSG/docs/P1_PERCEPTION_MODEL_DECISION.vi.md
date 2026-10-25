# P1 — quyết định model perception development

Ngày: 2026-10-01  
Trạng thái: đã chốt cho development `apt_0`; chưa tuyên bố source-model
parity và chưa chạy inference toàn bộ holdout.

## Quyết định

Chọn **Ultralytics YOLO11n-seg ONNX, pretrained COCO**, chạy CPU bằng
ONNX Runtime. Đây là một model instance-segmentation thật, xuất class + raw
score + instance mask; không dùng semantic GT, object metadata hay room GT
làm detection.

Lý do chọn `n` thay vì model source cấu hình `yolo11x-seg.onnx`: repo source
chỉ có cấu hình/path mẫu, không có weights; môi trường core không có
PyTorch/Ultralytics và OpenCV DNN `4.6.0` không import được graph opset 22.
`n` đủ để kiểm thử pipeline offline
trên CPU và giữ cùng model family/task. Đây là khác biệt được ghi nhận, không
gọi là tái hiện đúng weight `x` của source.

## Artifact và quyền sử dụng

- URL: `https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-seg.onnx`
- Local path:
  `artifacts/models/ultralytics/yolo11n-seg.onnx`
- SHA-256:
  `0bc32bc92e985b881141ef9bd2216e2a746f70519d0d24da9fc85decc4428cf4`
- Source release: Ultralytics assets `v8.3.0`.
- License note: Ultralytics YOLO11 weights/software ghi AGPL-3.0; artifact
  hiện chỉ dùng local development. Chưa có quyền phân phối/public packaging
  được suy ra.
- Runtime: ONNX Runtime `1.24.4`, `CPUExecutionProvider`, trong
  `.venv-perception`; Pillow chỉ dùng xuất overlay; không cài ROS/system-wide.

Model docs xác nhận YOLO11 có task instance segmentation và các biến thể
`n/s/m/l/x`; nguồn source OccuSG hiện tham chiếu `yolo11x-seg.onnx`. Quyết
định này chọn `n` vì điều kiện CPU/weight local, không phải claim chất lượng
tương đương `x`.

## Preprocessing và thresholds khóa cho development

- RGB input, letterbox `640×640`, stride 32, float32 `/255`.
- Mask được unletterbox về đúng kích thước ảnh gốc `160×120`.
- confidence `0.25`, NMS IoU `0.45`, mask threshold `0.50`.
- `0.25` là ngưỡng development assumption: smoke trên capture thật ở
  `160×120` cho thấy `0.70` hầu như không tạo detection; ngưỡng này phải
  được kiểm tra trên `apt_0` và holdout, không được xem là calibrated
  probability hay bằng chứng room quality.
- COCO-80 vocabulary trong `configs/perception/coco.names`.
- Synonym map giới hạn: `couch→sofa`, `dining table→dining_table`,
  `potted plant→potted_plant`, `tv→tv`.
- P3 geometry giữ source-inspired thresholds: axial depth, `eps=0.02 m`,
  cluster min `100`, max `2,500,000`, chọn largest cluster, center của
  visible-cluster AABB; world extents tính lại từ transformed corners.
- P2/P3 profile không phải nguồn runtime của M6. P4 replay đọc M6 từ
  `configs/reference_code_mp3d.yaml`: effective same-class radius `0.75 m`,
  suppression cap `0.15 m`, first-observation geometry, không fusion. Hai giá
  trị `0.50/0.10` từng nằm trong metadata perception đã được đánh dấu là
  không hiệu lực cho P4 để tránh nhầm provenance.

Toàn bộ giá trị nằm trong
`configs/perception/yolo11n_seg_replica_cad_dev.json`; không rải hard-code
trong core.

## Coverage và giới hạn

COCO có một số class nội thất cần cho development (`bed`, `sofa/couch`,
`chair`, `dining table`, `toilet`, `sink`, `refrigerator`, `oven`, ...),
nhưng không có semantic room classes. Room labeler sau G1 không được suy
nhãn phòng chỉ từ một class đơn lẻ.

P1 nghiệm thu ở mức decision/config/provenance và weights integrity. P2 chỉ
được đánh dấu đạt khi cache có mask/class/score thật từ RGB và negative/error
handling pass. P3/P4 chưa được gọi đạt trước khi inference và geometry
fixtures chạy.
