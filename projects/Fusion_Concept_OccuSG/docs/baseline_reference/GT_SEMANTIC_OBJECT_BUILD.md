NHIỆM VỤ:
Tích hợp object GT evaluation vào launcher dưới dạng bước hậu xử lý riêng.

Run development:
artifacts/runs/hm3d_00802_20261006T091921.402660Z_0220b9
Đây là run 320×240 cần đánh giá đầu tiên.

Mục tiêu:
Sau khi /run hoàn tất, tôi có thể gọi một lệnh ngắn để chấm
object perception, P3 và association bằng semantic annotations HM3D.
Không phải tự ghép đường dẫn capture/cache/graph bằng tay.

1. Giao diện cần bổ sung

Interactive:
/evaluate-objects RUN_ID --check
/evaluate-objects RUN_ID
/open RUN_ID object-evaluation
/open RUN_ID object-review

CLI:
./run_scene.sh evaluate-objects RUN_ID --check
./run_scene.sh evaluate-objects RUN_ID
./run_scene.sh open RUN_ID object-evaluation
./run_scene.sh open RUN_ID object-review

Đây là giao diện cần triển khai, không giả định đã tồn tại.

Dùng chung command handler cho CLI và interactive.
Yêu cầu RUN_ID rõ ràng để tránh chấm nhầm run gần nhất.

2. Ranh giới kiến trúc
- Launcher chỉ điều phối evaluator độc lập.
- Tái sử dụng evaluator/matching/GT utilities hiện có khi phù hợp.
- Không sửa capture, detections, ObjectNodes hoặc config predictor.
- Không chạy lại detector, P3, mapping hay association.
- Không truyền GT vào predictor.
- Không thêm bước này mặc định vào mọi /run.
- Không thay hoặc ghi đè evaluation tổng hiện có.

3. Preflight --check
Tự tìm inputs từ launcher/run manifests, không đoán từ tên thư mục:
- capture và frame metadata;
- Detection2D/masks;
- P3 cache/evidence ledger;
- graph và detection-to-object lineage;
- scene/dataset config và semantic assets;
- environment Habitat và evaluator;
- mapping config, protocol và output location.

Xác minh scene, frame IDs, capture hashes và dependencies tương thích.
Phân biệt:
- đủ điều kiện chạy;
- GT thiếu;
- category mapping chưa xác nhận;
- input/provenance không tương thích.

Thiếu mapping confirmed có thể vẫn chạy GT preparation,
class-agnostic diagnostic và review package.
Không giả tạo class-aware accuracy cho phần chưa đủ điều kiện.

--check không render, không chạy predictor, không tạo evaluation run.

4. Chuẩn bị semantic GT riêng
- Render semantic sensor tại đúng pose, camera intrinsics,
  extrinsics, resolution và scene state của capture đã lưu.
- RGB/depth render đối chứng để kiểm tra alignment với capture.
- Khóa tolerance trước khi chấm và báo sai lệch theo frame.
- Xác minh semantic sensor IDs khớp descriptor;
  không mặc định số thứ tự dòng descriptor chính là sensor ID.
- Báo background/unannotated/invalid IDs và phạm vi GT có thể dùng.
- Nếu calibration/alignment fail, không chấm các frame đó
  như thể GT đúng; báo blocker/phạm vi bị loại rõ ràng.

GT chỉ nằm ở evaluator, không thay dữ liệu run gốc.

5. GT cache có kiểm chứng
- Cache masks để không render lại khi chấm lại cùng capture.
- Cache key bao gồm capture/frame metadata hashes,
  sensor settings, scene/semantic asset/config hashes,
  renderer version và GT-generation code/protocol version.
- Không tái sử dụng cache chỉ vì cùng scene ID.
- Mapping category có version riêng:
  đổi mapping không bắt buộc render lại masks nếu geometry không đổi.
- Ghi rõ cache generated/reused và kết quả validation.
- Không copy dataset/model vào mỗi evaluation.

6. Category mapping và protocol
- Mapping HM3D category → detector canonical class.
- Tách confirmed/provisional/unmapped/unsupported.
- Xác nhận phải có provenance; không tự đánh dấu human-confirmed.
- Chuẩn bị review ngắn cho các category thực sự xuất hiện.
- Không coi mọi HM3D object là class detector hỗ trợ.

Khóa trước:
- one-to-one matching;
- mask IoU threshold, khởi đầu 0.50;
- minimum visible area;
- ignored/unannotated policy;
- eligibility của frame/instance;
- cách xử lý denominator bằng 0.

Class-aware metrics chỉ dùng mapping đủ điều kiện.
Class-agnostic diagnostics phải ghi riêng.
Không coi prediction không match ở vùng thiếu annotation
là FP chắc chắn nếu chưa có protocol phù hợp.

7. Metrics
A. Detection2D
- TP/FP/FN, precision/recall/F1 theo observation.
- Matched-mask IoU, per-class counts và evaluable scope.
- Không gọi single-threshold result là AP/mAP.
- Unique-instance coverage báo riêng nếu tính được.

B. P3
- Trong detections match đúng GT: accepted/rejected và lý do.
- Acceptance rate không phải accuracy.
- Báo unmatched/uncertain riêng.

C. Association
- Nối detection ID → GT instance ID trong evaluator.
- Dùng lineage thực tế để nối observation vào ObjectNode.
- Phân biệt match, tạo node và suppression;
  không mặc định detection suppressed là quan sát match của node gần nhất.
- Nếu lineage không đủ, metric tương ứng unavailable với lý do.
- Báo một GT instance thành nhiều predicted IDs (fragmentation),
  một predicted ID chứa nhiều GT instances (false merge).
- Tách identity matching class-agnostic khỏi class correctness,
  để không bỏ qua identity lỗi chỉ vì class thay đổi.
- Không dùng GT IDs để sửa graph.

Review cụ thể cặp sofa 000013/000016,
node 000004 và dog/cat mismatch của node 000008,
cùng các detection phương tiện qua cửa sổ.

8. Output và bảo toàn run
Mỗi lần chấm tạo thư mục mới:

artifacts/runs/RUN_ID/object_evaluations/EVAL_ID/

Gồm:
- manifest.json;
- protocol.json;
- category_mapping_snapshot.json;
- gt_validation.json;
- metrics.json và metrics.csv;
- observation_matches.jsonl;
- association_audit.json;
- report.vi.md;
- review/index.html và ảnh đối chiếu.

GT cache dùng vị trí riêng có manifest/hash.
Không ghi đè lần chấm cũ.
Giữ hash các inputs predictor trước/sau để chứng minh không bị sửa.

Review hiển thị RGB, prediction mask, GT mask,
class/instance IDs, IoU, P3 decision và ObjectNode liên quan.
Mục chưa đủ bằng chứng giữ uncertain.

9. Lifecycle launcher
- Dùng lock để tránh chạy evaluation nặng đồng thời
  với pipeline khác ngoài ý muốn.
- /status và /logs thể hiện đây là object evaluation job,
  tách khỏi trạng thái completed của predictor run.
- Evaluation fail/cancel không biến run predictor thành failed.
- Giữ partial evaluation với status rõ ràng.
- Tận dụng cơ chế cancel/process management hiện có.
- /open chọn evaluation completed mới nhất và in EVAL_ID/path;
  không âm thầm mở một bản failed hoặc không rõ phiên bản.

10. Hoàn tất review
- Cung cấp mapping review template và hướng dẫn tôi xác nhận.
- Sau khi tôi sửa/xác nhận mapping, chạy lại cùng lệnh:
  dùng mapping version mới, tạo evaluation mới,
  tái sử dụng GT cache nếu còn hợp lệ.
- Không bắt tôi render lại hoặc chạy lại pipeline chỉ vì đổi mapping.
- Không sửa nhãn GT để làm prediction đạt điểm cao.

11. Tests và bàn giao
- Tests matching: perfect, missed, extra, duplicate predictions,
  unsupported/unmapped classes, ignored regions, empty denominator.
- Tests cache invalidation, wrong capture/scene,
  geometry validation failure và incomplete lineage.
- Tests fragmentation/false merge bằng fixture có đáp án.
- Tests output không ghi đè, cancel và evaluator failure isolation.
- Chạy thật trên run 320×240 đã chỉ định.
- Báo rõ phần có metric hợp lệ và phần còn chờ human review.
- Cập nhật SCENE_LAUNCHER.vi.md và quickstart
  với đúng bốn lệnh ngắn mới.

Điểm dừng:
Tôi có thể chấm object sau /run bằng launcher,
xem metrics và chỉ review các trường hợp cần xác nhận.
Chưa triển khai room-boundary GT evaluation mới,
chưa thay model hoặc association để tăng điểm.