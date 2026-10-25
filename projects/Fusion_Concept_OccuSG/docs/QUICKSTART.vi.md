# Hướng dẫn chạy map, profile và đọc kết quả

Chạy các lệnh CLI từ thư mục Fusion_Concept_OccuSG. Trong prompt scene>, thêm / trước lệnh. Ví dụ CLI ./run_scene.sh maps tương đương prompt /maps.

## 1. Chọn map và route

/maps liệt kê các map đã đăng ký. Alias là tên dùng trong lệnh; một map có thể có nhiều route.

~~~text
scene> /maps
hm3d_00802 | hm3d | 00802-wcojb4TFT35 | routes=smoke,main,main_320,main_600,main_dense | ...
~~~

Xem thông tin và các route:

~~~text
scene> /info hm3d_00802
scene> /routes hm3d_00802
scene> /check hm3d_00802 --route main_320
~~~

check xác minh cấu hình, tài nguyên, navmesh và route trước khi chạy. Route main_320 là bộ 29 pose ở 320×240. Route là các pose quan sát cố định; đây không phải autonomous navigation.

## 2. Các profile association hiện có

Tùy chọn tên là --profile (không phải --mode). Nếu bỏ tùy chọn này, launcher dùng source.

| Profile | Graph backend | Ý nghĩa và giới hạn |
| --- | --- | --- |
| source | Source ObjectAssociationManager | Mặc định. Giữ association gốc. |
| geometry-only | Geometry-only experimental | Backend thử nghiệm được gọi qua M9; có thể đổi số object tracks. |
| geometry-only-dedup-v2 | Geometry-only + same-frame duplicate guard v2 | Thử nghiệm; guard containment là heuristic, không phải bộ nhận biết mọi mask nhiều vật. |

Các phần chưa nằm trong một profile “bật tất cả”:

- U4-OV là diagnostics-only, không thay class trong graph hoặc room scores.
- Appearance là ablation; chưa được dùng mặc định trong graph.
- Room labels vẫn do rule-based labeler hiện có tạo.

Vì vậy geometry-only-dedup-v2 là backend graph thử nghiệm mới nhất có thể chọn, nhưng không đồng nghĩa toàn bộ ConceptGraphs/U4-OV đã được tích hợp vào predictor.

U4-OV có thể được xuất thành sidecar diagnostics khi replay cache với profile geometry-only:

~~~text
scene> /replay-cached <SOURCE_RUN_ID> --profile geometry-only --semantic u4ov
~~~

Sidecar này không thay graph class hoặc room labels. Hiện không thể ghép sidecar U4-OV khóa theo geometry-only với geometry-only-dedup-v2. Geometry+appearance cũng chưa phải profile launcher.

## 3. Run mới: capture đến graph

run dùng map alias và route alias. Nó tạo capture mới rồi chạy P2, P3, native graph replay và các bước sau của pipeline. Mỗi lần chạy tạo một RUN_ID riêng.

~~~text
scene> /run hm3d_00802 --route main_320 --profile geometry-only-dedup-v2
~~~

Khi bỏ --profile, tương đương:

~~~text
scene> /run hm3d_00802 --route main_320 --profile source
~~~

Khi hoàn tất, launcher in RUN_ID, profile, số frame, P3 accepted/rejected, objects/rooms và đường dẫn output. Dùng đúng RUN_ID vừa in:

~~~text
scene> /results <RUN_ID>
scene> /open <RUN_ID> map
scene> /open <RUN_ID> objects
~~~

RUN_ID là ID của một lần chạy, ví dụ hm3d_00802_20261011T025309.067351Z_4f3ace. Nó khác alias hm3d_00802, và khác evaluation ID.

## 4. Cache replay: đổi graph backend trên cùng dữ liệu

Cache replay dùng lại capture, P2 masks/detections và P3 geometry đã lưu trong run nguồn, rồi chạy lại native graph replay với profile được chọn. Nó không capture lại, chạy lại YOLO/P2 hoặc dựng lại P3. Đây là cách phù hợp để so sánh association trên đúng cùng observations.

~~~text
scene> /replay-cached <SOURCE_RUN_ID> --profile source
scene> /replay-cached <SOURCE_RUN_ID> --profile geometry-only
scene> /replay-cached <SOURCE_RUN_ID> --profile geometry-only-dedup-v2
~~~

Mỗi lệnh replay tạo một output riêng với ID mới, thường có dạng u5m9_.... Dùng ID mới được in ở dòng run-id=:

~~~text
scene> /results <CACHED_REPLAY_RUN_ID>
scene> /open <CACHED_REPLAY_RUN_ID> u5
~~~

Ví dụ trong workspace: source run hm3d_00802_20261011T025309.067351Z_4f3ace có 23 object nodes; geometry-only cache replay u5m9_20261011T031134.781311Z_040e9f có 26. Đây là thay đổi graph thực tế trên cùng capture/P2/P3, nhưng số node tăng tự nó không chứng minh chất lượng tăng.

Cached replay chỉ tạo graph/replay artifacts. Nó không phải full HM3D run để gọi /evaluate-objects; nếu gọi evaluator trên ID u5m9_..., launcher sẽ từ chối vì replay đó không chứa bộ artifacts của một full run. Dùng SOURCE_RUN_ID để xem object detection/GT metrics.

## 5. Xem geometry fusion trên assignments đã có

Lệnh này dùng chính association assignments của một full run để tích lũy point cloud theo object. Nó không chạy lại capture/detector khi tái dựng selected-point sidecar, không đổi association/class/graph/room scores và không thay graph viewer.

~~~text
scene> /fuse-objects <FULL_RUN_ID> --check
scene> /fuse-objects <FULL_RUN_ID>
scene> /open <FUSION_RESULT_ID> fusion
~~~

Mỗi lần chạy tạo `FUSION_RESULT_ID` riêng trong `artifacts/object_fusions/`. `/status`, `/logs`, `/cancel` nhận fusion result ID khi lệnh chạy interactive. Chi tiết về sidecar, runtime và giới hạn: [Object Fusion Quickstart](OBJECT_FUSION_QUICKSTART.vi.md).

## 6. Evaluation IDs và ý nghĩa TOTAL OBSERVATIONS

Sau khi một full HM3D run hoàn tất, có thể chạy evaluator riêng:

~~~text
scene> /evaluate-objects <FULL_HM3D_RUN_ID>
scene> /object-results <FULL_HM3D_RUN_ID>
~~~

Mỗi lần evaluator tạo EVAL_ID riêng, dạng eval_..., nằm bên trong thư mục của run. Chọn evaluation cụ thể khi cần:

~~~text
scene> /object-results <FULL_HM3D_RUN_ID> --eval-id <EVAL_ID>
scene> /open <FULL_HM3D_RUN_ID> object-review
~~~

RUN_ID chọn run; EVAL_ID chọn kết quả evaluator nằm trong run đó. Không truyền eval_... vào vị trí cần RUN_ID.

Trong bảng TOTAL OBSERVATIONS:

- Eligible GT observations: số instance GT đủ điều kiện nhìn thấy, đếm theo frame.
- Detection observations: số detection masks P2, đếm theo frame.
- Matched masks, FP, FN: so mask P2 với semantic GT theo từng frame.

Đây không phải số unique graph objects/tracks và không đo trực tiếp association. Khi chỉ đổi graph profile trên cùng cache, các tổng detection/GT này được giữ nguyên; hãy xem Objects trong /results, graph và replay artifacts để biết association có thay đổi. Khi detector/P2 chạy lại, detection totals có thể khác. Class-aware metrics vẫn có thể unavailable nếu chưa có category mapping được human-confirm.

## 7. Quy trình khuyến nghị để so sánh

Muốn so riêng graph backend mà không đổi observations:

1. Chạy /check cho map/route.
2. Chọn một full run làm nguồn.
3. Chạy /replay-cached SOURCE_RUN_ID --profile source.
4. Chạy /replay-cached SOURCE_RUN_ID --profile geometry-only hoặc geometry-only-dedup-v2.
5. So sánh results và graph/replay của hai cached replay.

Muốn capture route lại từ đầu với candidate:

~~~text
scene> /run hm3d_00802 --route main_320 --profile geometry-only-dedup-v2
~~~

Đây là lần chạy mới, nên detector có thể tạo số detections khác với run cũ. Không dùng chênh lệch detection totals để quy riêng cho association.

## 8. Đăng ký map/route mới

Map và route phải được chuẩn bị trước. Tạo registry entry dựa trên một template trong configs/launcher/templates/, trỏ tới evaluation profile và route JSON. Sau đó:

~~~bash
./run_scene.sh register --config configs/launcher/my_scene_entry.json
./run_scene.sh maps
./run_scene.sh info my_scene
./run_scene.sh routes my_scene
./run_scene.sh check my_scene --route smoke
~~~

Đăng ký chỉ thêm alias và đường dẫn vào registry; không tải scene/model, không tạo route hoặc xác nhận navigability. check mới là bước xác minh.

## Tóm tắt ID

| ID | Được tạo bởi | Dùng với |
| --- | --- | --- |
| Map alias, ví dụ hm3d_00802 | Registry | maps, info, routes, check, run |
| Full RUN_ID, ví dụ hm3d_... | /run | results, open, evaluate-objects, object-results, làm nguồn cho replay-cached |
| Cached replay RUN_ID, ví dụ u5m9_... | /replay-cached | results, open; không dùng trực tiếp cho evaluate-objects |
| EVAL_ID, ví dụ eval_... | /evaluate-objects | object-results FULL_RUN_ID --eval-id EVAL_ID |
| FUSION_RESULT_ID, dạng fusion_... | /fuse-objects | /status, /logs, /cancel, /open FUSION_RESULT_ID fusion |
