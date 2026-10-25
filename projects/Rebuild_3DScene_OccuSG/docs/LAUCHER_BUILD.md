NHIỆM VỤ: XÂY LAUNCHER MỎNG, DỄ DÙNG CHO PIPELINE HABITAT

Workspace:
/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG

Mục tiêu
Tôi muốn thao tác với map, route, pipeline và kết quả bằng lệnh ngắn.
Launcher là lớp điều phối các runner hiện có, không phải framework mới.
Nó phải dễ mở rộng cho Value Map/navigation về sau nhưng chưa triển khai
những module đó trong nhiệm vụ này.

Đọc START_HERE.vi.md, RUN_AND_EVALUATE.vi.md và kiểm tra CLI thực tế
của các runner trước khi thiết kế. Không suy đoán flags từ tên script.

1. HAI GIAO DIỆN, CHUNG MỘT LOGIC

A. CLI không tương tác:

./run_scene.sh help
./run_scene.sh maps
./run_scene.sh info hm3d_00802
./run_scene.sh check hm3d_00802
./run_scene.sh run hm3d_00802
./run_scene.sh runs
./run_scene.sh status <run-id>
./run_scene.sh evaluate <run-id>
./run_scene.sh open <run-id>

B. Chế độ tương tác:
Chạy ./run_scene.sh không có argument để vào prompt:

scene> /help
scene> /all
scene> /maps
scene> /use hm3d_00802
scene> /info
scene> /check
scene> /run
scene> /status
scene> /logs
scene> /results
scene> /open
scene> /exit

- /all là alias liệt kê đầy đủ các lệnh và ví dụ,
  tuyệt đối không có nghĩa chạy tất cả map.
- /help là hướng dẫn ngắn theo workflow.
- Không tham số thì dùng scene/run đang chọn nếu có.
- Nếu thiếu lựa chọn, hướng dẫn lệnh cần dùng, không tự đoán scene.
- CLI và interactive phải gọi cùng command handlers;
  không duy trì hai pipeline khác nhau.
- Thông báo dễ hiểu bằng tiếng Việt, thuật ngữ/ID giữ nguyên.

2. DANH SÁCH LỆNH CỐT LÕI

/help [command] hoặc /all
  Xem cách dùng và ví dụ.

 /maps
  Liệt kê scene đã đăng ký:
  alias, dataset, scene ID, route và trạng thái.
  Phân biệt registered với runnable; không gắn “ready”
  chỉ vì file config tồn tại.
  Không load mọi simulator scene chỉ để liệt kê.

 /use <scene-alias>
  Chọn scene cho phiên làm việc.
  Không chạy capture hay sửa config.

 /info [scene-alias]
  Hiển thị asset, dataset config, route, sensor profile,
  pipeline/perception/semantic profile, môi trường và GT scope.
  Đường dẫn phải mở/tra cứu được.
  Hiển thị rõ pose-GT sampled route, chưa phải autonomous navigation.

 /routes [scene-alias]
  Liệt kê route đã đăng ký cho đúng scene.
  Không dùng route scene khác chỉ vì cùng schema.

 /check [scene-alias] [--route <route-alias>]
  Gọi preflight hiện có, kiểm tra asset, environment,
  model/native binary, config compatibility và route.
  Báo riêng missing files, invalid config, dependency errors.
  Không tự cài package, tải dataset hoặc sửa môi trường.

 /run [scene-alias] [--route <route-alias>] [--dry-run]
  Chạy pipeline đầy đủ.
  --dry-run chỉ in kế hoạch stage, environment, input/output;
  không tạo run hoặc khởi chạy simulator.

 /runs [--scene <scene-alias>]
  Liệt kê run do launcher quản lý:
  run ID, scene, start time, status, current/failed stage và output.
  Không quét toàn bộ dataset để tìm run.

 /status [run-id]
  Hiển thị stage hiện tại, elapsed time, kết quả từng stage,
  lỗi gần nhất và nơi xem log.
  Không giả tạo % tiến độ nếu runner không cung cấp.
  Phân biệt completed / failed / cancelled / interrupted.

 /logs [run-id] [--stage <stage>] [--tail <N>]
  Đọc log tương ứng, giới hạn số dòng mặc định.
  Không in credentials hoặc dump môi trường.

 /results [run-id]
  Tóm tắt metric chính và artifact quan trọng.
  Metric thiếu GT phải hiện “chưa có GT/phạm vi chưa hỗ trợ”,
  không biến null thành 0.
  Không gọi P3 acceptance hoặc label coverage là accuracy.

 /open [run-id] [report|map|objects|labels|folder]
  Mở artifact hiện có, không đoán tên file.
  Nếu chưa có hoặc thiếu GUI, in lý do và đường dẫn.
  Không xem việc mở viewer thất bại là pipeline thất bại.

 /evaluate <run-id>
  Đánh giá output đã có, không chạy lại capture/perception.
  Mỗi lần tạo evaluation directory mới, không ghi đè.
  Kiểm tra capture/config/GT provenance.
  Không gắn object GT metrics của capture cũ vào capture mới.

 /cancel [run-id]
  Dừng run do launcher này quản lý, ưu tiên graceful termination.
  Không kill process theo tên “python”, “habitat” hoặc PID chưa xác minh.
  Giữ partial artifacts và status rõ ràng.

 /exit
  Nếu có run đang hoạt động, hỏi chờ hoàn tất hay cancel.
  Không âm thầm để lại orphan workers.
  Không cần hỗ trợ background daemon/detach trong phiên bản đầu.

Các tên lệnh trên là giao diện cần triển khai, chưa được coi là tồn tại.

3. MAP MỚI: ĐĂNG KÝ, KHÔNG SỬA PYTHON

Cung cấp:
./run_scene.sh register --config <scene-entry.json>
và lệnh tương tác tương ứng.

- Registry chỉ lưu alias và đường dẫn tới config đã có,
  tránh nhân bản các tham số baseline.
- Schema có version, strict validation và alias uniqueness.
- Không overwrite scene entry cũ mặc định.
- Template cho HM3D và ReplicaCAD.
- Phải có scene asset/config, route và các profile cần thiết.
- Scene chỉ có mesh nhưng chưa route được ghi trạng thái
  “chưa cấu hình đủ”; chưa cho full run.
- Hỗ trợ các loader hiện đã kiểm chứng.
- Dataset khác/GLB độc lập chưa hỗ trợ phải báo rõ, không giả vờ chạy.
- Không tự sinh route đúng hoặc semantic GT từ một GLB bất kỳ.
- Đăng ký config không đồng nghĩa preflight đã pass.

Tôi muốn map A/B/C/D/E tương ứng các alias rõ ràng trong registry.
Thêm map mới bằng config, không phải thêm if/else theo scene trong source.

4. PIPELINE THỰC THI

Tái sử dụng:
preflight → capture → detection 2D → lifting 3D
→ native replay/grid trace → semantic sidecar → evaluation.

- Kiểm tra --help và contracts thật của mỗi runner.
- Đúng Python environment cho từng stage.
- Environment paths nằm trong config máy cục bộ;
  không rải đường dẫn user hard-code trong implementation.
- Không dùng việc activate shell hiện tại làm bảo đảm dependencies.
- Dùng subprocess argument lists, không shell=True/eval
  hoặc ghép chuỗi command chứa dữ liệu người dùng.
- Tính đường dẫn dự án từ vị trí launcher, không phụ thuộc cwd.
- Hỗ trợ đường dẫn có khoảng trắng.
- Dừng pipeline khi một stage thất bại.
- Chỉ báo completed sau khi kiểm tra output contract,
  không chỉ dựa vào process exit code.
- Không thay runner algorithms, weights hoặc baseline configs.

5. RUN MANAGEMENT AN TOÀN

- Mỗi run có ID duy nhất, tạo directory độc quyền,
  không chỉ dựa vào timestamp đến giây.
- Output root ở artifacts/runs hoặc vị trí cấu hình rõ ràng.
- Run manifest chứa input/config hashes, environment versions,
  command arguments đã loại thông tin nhạy cảm,
  stage status/timestamps, exit code và artifact paths.
- Ghi status atomically để /status không đọc JSON nửa chừng.
- Snapshot effective configs hoặc provenance cần tái lập;
  không copy dataset/model vào từng run.
- Log riêng từng stage và tổng hợp lỗi ngắn.
- Một run hoạt động tại một thời điểm trong phiên bản đầu;
  tránh vô tình tạo hai run tranh tài nguyên.
- Phòng chống hai launcher cùng chạy bằng lock đáng tin cậy,
  có xử lý stale lock.
- /status và /logs vẫn dùng được khi interactive run đang chạy.
- Nếu launcher crash, khi mở lại phải nhận diện interrupted;
  không giữ “running” mãi chỉ vì status file cũ.
- Không tự resume stage hoặc dùng cache partial khi chưa xác minh.
- Ctrl+C/cancel xử lý cả subprocess tree do launcher tạo,
  không ảnh hưởng job khác.

6. KẾT QUẢ DỄ ĐỌC

Sau khi hoàn tất, in:

Run ID:
Scene / route:
Status:
Processed frames:
Regions / RoomNodes / Objects:
Predicted / Unknown / Ambiguous labels:
Metrics report:
Spatial map:
Object evidence:
Output directory:

Không gọi các count trên là accuracy.
Không báo fresh replay pass nếu run này chưa thực hiện đối chứng.
Tách pipeline success, structural validity và GT-based quality.

Ví dụ tương tác mong muốn:

scene> /use hm3d_00802
Đã chọn HM3D 00802 — route main.

scene> /check
Preflight PASS.

scene> /run
[1/7] Preflight...
...
[7/7] Evaluation...
Hoàn tất. Run ID: ...
Xem kết quả: /results ...
Mở báo cáo: /open ... report

Các thông báo trên chỉ là mẫu; không hard-code PASS/kết quả.

7. KIẾN TRÚC MỎNG, DỄ MỞ RỘNG

- run_scene.sh chỉ làm entrypoint.
- Một CLI module Python điều phối command handlers.
- Registry/config schema và stage definitions tách rõ.
- Tái sử dụng evaluator/runner hiện có.
- Không thêm web server, database, agent framework hoặc task scheduler.
- Không thêm plugin system tổng quát khi chưa cần.
- Không có lệnh thực thi shell tùy ý.
- Chưa có remove/clean/purge trong launcher phiên bản đầu.
- Không đọc credentials hoặc tự tải dataset.
- Sau này thêm Value Map/navigation bằng stage/command mới,
  không phải sửa mọi handler.

8. TEST VÀ NGHIỆM THU

Unit/integration tests ít nhất:
- CLI và interactive cùng dispatch.
- Invalid command/config, duplicate scene alias.
- Missing route/model/environment.
- Path có khoảng trắng, chạy từ cwd khác.
- Output collision không ghi đè.
- Stage failure làm các stage sau không chạy.
- Cancellation và interrupted state.
- Atomic status và lock.
- Artifact không tồn tại / GUI không có.
- Metrics null không bị đổi thành 0.
- Dry-run không tạo side effects.
- Không trộn GT/cache của capture khác.

Chạy một HM3D end-to-end thật bằng launcher,
bao gồm perception, semantic sidecar và evaluation.
Chạy preflight ReplicaCAD để kiểm tra registry không hard-code HM3D.
Phân biệt mock tests với simulator run thật.
Chạy full regression và giữ baseline không đổi.

9. BÀN GIAO

- Launcher hoạt động theo giao diện đã nêu.
- Registry có HM3D 00802 và ReplicaCAD apt_0 hiện có.
- Template đăng ký map mới.
- QUICKSTART.vi.md tối đa một trang:
  mở launcher → /maps → /use → /check → /run → /results → /open.
- Hướng dẫn riêng “thêm map B sau khi đã chạy map A”,
  nêu rõ cách tạo/chọn route hiện có; không hứa có route editor GUI.
- Status report, test results và một run end-to-end thực tế.

Điểm dừng:
Tôi có thể sử dụng dự án bằng các lệnh ngắn mà không nhớ
chuỗi command dài, nhưng vẫn truy xuất được command/config/log
để tái lập và chẩn đoán. Chưa mở rộng thuật toán.