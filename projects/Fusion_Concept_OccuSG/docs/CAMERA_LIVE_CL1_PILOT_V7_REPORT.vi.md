# CL1 pilot v7 — kết quả và điểm dừng

## Kết luận

Đây là **pipeline failure do lỗi code trước capture**, không phải pilot chạy
xong nhưng thiếu overlap và cũng không phải producer thành công nhưng GUI lỗi.
Đúng một lần khởi chạy worker/pilot đã diễn ra; không retry.

Run: `camera_live_t1_1_20261018_v7`  
Route/config: `smooth_t1_1_hm3d00802_turn_continue_20261018_v2`  
Run directory: `../artifacts/live_processing_runs/camera_live_t1_1_20261018_v7`

## Trạng thái các gate

| Câu hỏi | Kết quả |
| --- | --- |
| `timestamp_s` còn lỗi khi OpenCLIP thật chạy? | **Chưa được kiểm tra ở frame.** OpenCLIP session thật khởi tạo và phát readiness marker, nhưng camera producer dừng trước khi capture/publish frame nên `update_frame` không được gọi. Không thấy lỗi cũ, nhưng không thể coi đó là regression pass thực nghiệm. |
| Keyframes perception xử lý | **0** cho P2, P3 và OpenCLIP. |
| Keyframes mapper xử lý | **0** cho ConceptGraphs và OccuSG. Cả hai session khởi tạo xong; chưa gọi update. |
| Unified revisions | **0**. |
| Commit trước/sau capture end | Không áp dụng: capture chưa bắt đầu, không có capture-end timestamp hoặc revision commit. |
| Viewer render đồng thời | **Pending.** D2 bootstrap và HTTP asset checks trả 200; browser GUI không khả dụng trong phiên này và không có graph revision để render. |

## Nguyên nhân đã xác minh

Năm worker (P2, P3, OpenCLIP, ConceptGraphs, OccuSG) đều ghi readiness marker
trước khi Habitat được khởi chạy. Sau đó camera child thoát mã 1 với:

```text
FATAL: FileExistsError: refusing to overwrite existing run contents: ['ready']
```

Coordinator đã tạo `ready/` trong run root. Route runner bật
`--allow-existing-pilot-root`, nhưng allowlist đầu vào không có `ready/`;
guard từ chối root trước khi tạo Habitat simulator. Vì thế chưa có frame,
inference hoặc mapper mutation.

Run manifest ghi `status=failed`, 0 keyframes và 0 revisions. Trường `stages`
còn giữ vài giá trị khởi tạo (`running`/`pending`); cleanup ledger là bằng
chứng cuối cho process state: camera child exit code 1, năm worker đều được
terminate và exit. Không còn listener ở port 8776. Run v7 và logs được giữ.

## Sửa lỗi giới hạn và kiểm thử

Cho phép `ready/` trong output-root guard **chỉ** khi bật
`allow_existing_pilot_root`; các file/thư mục ngoài allowlist vẫn bị từ chối.
Thêm regression test cho đúng tình huống coordinator tạo `ready/`, cùng
kiểm tra flag tắt và unexpected file.

`tests/test_smooth_route.py` và các regression camera-live/coordinator:
**45 passed**. Không chạy capture, inference, mapper hay pilot sau khi sửa.

Hash route runner lúc v7 thất bại:
`d6f8306a55b07ac1aa0f3ccfe78567440fe42e88b348bb5e8b112143d36a2908`  
Hash sau sửa allowlist:
`fadfd9a86f352560730bbba3cbf54cdeced3ea7b37b810dd31a47cf2720df220`  
Regression test:
`029db7e2415abaa498336f9539e0d87d8ccca289ecfc5ac2f4b08f053fc5c5ed`

## Tài nguyên và điểm dừng

Preflight ngay trước run: RAM khả dụng 6.4 GiB; disk trống 303 GiB; port
8776 bind được; route scene/navmesh/config hashes và checkpoint paths đạt.
Không có worker cũ còn sống. Run v7 chưa đo được runtime capture, inference,
mapping hoặc overlap.

Pilot được dừng theo chỉ thị sau lỗi producer; không retry. Để xác nhận
timestamp qua OpenCLIP inference, mapper updates, unified commit và GUI render
đồng thời sẽ cần một pilot mới được duyệt riêng.
