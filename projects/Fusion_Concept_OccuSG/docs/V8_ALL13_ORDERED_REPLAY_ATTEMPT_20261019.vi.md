# Kết quả lượt replay 13 keyframe v8

Ngày 2026-10-19. Đây là lượt replay duy nhất được duyệt, không phải capture
hay live run. Nguồn v8 được giữ read-only.

## Trạng thái

Lượt replay dừng ở **mapper readiness, trước `update_frame`**. Không có prefix
graph revision nào được commit; không có terminal ConceptGraphs postprocess
hoặc OccuSG flush. Đây là lỗi nối contract giữa tên ready-file role và giá trị
`kind` trong marker, không phải lỗi perception.

- Ready file của coordinator: `ready/occusg.json`.
- Marker OccuSG thực tế: `kind=occusg_rooms`.
- Validator cũ đòi `kind=occusg`, nên từ chối readiness dù marker còn lại đúng
  `run_id`, `bootstrap_id`, PID, route hash và clock domain.
- ConceptGraphs và OccuSG worker đã khởi tạo và ghi ready marker. Run-owned
  cleanup dừng cả hai sau khi gate readiness báo lỗi.
- Không có object/room update marker, branch event hay unified commit. Cleanup
  ghi hai mapper process đã thoát với return code `-15`; không còn worker sống.

## Perception đã hoàn tất và được kiểm tra

Run mới: `hm3d00802_t1_1_all13_replay_20261019_v1`.

- 13 raw RGB-D packets theo đúng thứ tự đã khóa; không capture frame nào.
- Reuse endpoint perception `000000` và `000110`, mỗi endpoint có 5 detections.
- Chạy P2, P3 và OpenCLIP trên 11 frame còn thiếu bằng session workers giữ model
  sống: P2 ONNX load 1 lần, OpenCLIP load 1 lần; 11 frame calls cho mỗi stage.
- Tổng packet outputs: 45 P2 rows, 44 P3 accepted, 1 P3 rejected và 45 feature
  rows. P3 rejection không loại feature row.
- Các source hashes, masks, feature hashes, vector shape `512`, finiteness và
  chuẩn hóa đã được kiểm tra lại trên cả 13 packets.
- Perception wall time cộng theo 11 frame mới: 14.33 s; trung bình 1.30 s/frame,
  lớn nhất 2.54 s. Source preflight 3.07 s; hai adapter preflight mỗi nhánh
  khoảng 0.52–0.62 s. Tổng wall time toàn run không được ghi trong failure path.

## Integrity và đính chính

- 232 immutable files trong manifest đều tồn tại và khớp hash; manifest receipt
  khớp SHA-256.
- Manifest gốc được giữ nguyên. Trường progress lúc chạy báo 15/13 vì cộng hai
  endpoint reuse lần thứ hai; kiểm tra 13 packet/frame IDs xác nhận số đúng là
  13. Đính chính nằm trong `post_run_validation_addendum.json` cùng hash riêng.
- D2 entrypoint được bootstrap và kiểm tra local bundle thành công trước khi
  mapper khởi chạy. Browser chưa được mở; GUI acceptance vẫn pending.
- Membership và room semantics vẫn unavailable theo spatial-scope gate.

## Hardening sau failure

Đã sửa readiness validator để tách ready-file role `occusg` khỏi marker kind
`occusg_rooms`, thêm regression fixture qua đúng readiness helper. Đồng thời
sửa progress counter và ghi trạng thái perception đang chạy trong manifest.
Các sửa đổi này chỉ được compile/test; không khởi tạo lại mapper hoặc replay.

Kiểm tra sau sửa: `23 passed`; compile và `--help` pass.

## Bước để hoàn tất

Lần replay đã dùng hết lượt tích hợp được duyệt. Để hoàn tất graph, bước kế tiếp
cần là **một lượt mapper-only mới từ state rỗng**, lấy 13 joined packets đã được
kiểm tra trong run này; không chạy lại capture, P2, P3 hoặc OpenCLIP. Mỗi mapper
nhận đúng 13 packets cùng thứ tự, một session mỗi nhánh, rồi terminal postprocess
và flush. Cần duyệt lượt mapper này riêng vì execution count một lượt trước đó
đã kết thúc ở readiness gate.
