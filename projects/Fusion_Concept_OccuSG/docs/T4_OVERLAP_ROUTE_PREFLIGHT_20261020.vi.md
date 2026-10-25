# T4 overlap route preflight — HM3D 00802

Ngày: 2026-10-20  
Kết quả: **dừng trước producer**. Chưa có continuation nào vượt toàn bộ gate navmesh/clearance hiện hành, nên không tạo effective route config, không chạy Habitat capture, perception, mapper hoặc semantic session. T4 overlap acceptance vẫn pending.

## Phạm vi đã kiểm tra

- Giữ nguyên prefix T1.1 trong `configs/habitat/hm3d_minival_v02_00802_t1_1_turn_continue_route.json`: ba waypoint từ `(1.05, 0.124, -4.586)` qua `(0.65, 0.124, -4.586)` đến `(0.65, 0.124, -4.700)`.
- Dùng navmesh HM3D 00802 đã khóa, cùng lát route `Y=0.124 m`, Habitat-Sim 0.3.3 và PathFinder. Không dùng predictor, detections, graph hoặc room labels.
- Waypoint prefix hiện có được dựng lại bằng từng cặp liên tiếp của `ShortestPath`, rồi kiểm tra bằng cùng `validate_navmesh_polyline` mà route runner gọi. Prefix đạt: 14 mẫu cách tối đa 0.04 m, 13 đoạn `try_step_no_sliding`, clearance nhỏ nhất `0.1500000 m` với yêu cầu footprint `0.1 m`.

## Kết quả tìm continuation

PathFinder seed được khóa là `20261020`. Endpoint candidates được lấy từ navmesh, chiếu về đúng lát Y đã có, rồi nối bằng shortest paths từ endpoint prefix. Với từng candidate, kiểm tra `is_navigable`, `distance_to_closest_obstacle >= 0.1 m` trên mẫu mỗi 0.04 m và sai số `try_step_no_sliding <= 0.025 m`.

- Trong 50,000 mẫu: 10,520 điểm không còn navigable sau khi chiếu về Y đã khóa; 32,745 mẫu không có path hoặc không nằm trong khoảng continuation geodesic 6–12 m; 5,433 đường fail clearance; 1,302 đường bị `try_step_no_sliding` clip. Không có continuation trong nhóm tìm kiếm này đạt cả hai kiểm tra.
- Một lượt geometry-only riêng với seed và tiêu chí route duration tạo 80 candidates; cả 80 đều được chạy qua validator hình học đầy đủ và không candidate nào pass. Candidate xếp đầu theo mục tiêu thời lượng có extension geodesic `6.067 m`, `823` bước tiến + `271` bước quay (1,094 action ước tính). Con số là `43.76 s` theo requested 40 ms/action; chiếu theo mean observed 42.224 ms/action của pilot trước là khoảng `46.19 s`. Dùng 48 ms/action sẽ là `52.512 s` và là mức observed lớn nhất của pilot trước, không phải tốc độ trung bình. Đây chỉ là ước lượng, không phải runtime.
- Candidate đó vẫn fail production clearance gate tại mẫu 25: `0.094507 m < 0.1 m`. Hai candidates kế tiếp fail tại mẫu 24 với `0.089795 m` và `0.087335 m`. Candidate đầu còn có điểm PathFinder cao hơn lát route `0.0709 m`, vượt `vertical_navmesh_tolerance_m=0.05 m`; do clearance đã fail nên không được đưa sang effective config.

Không nới clearance, vertical tolerance, route speed, keyframe policy hoặc queue để cứu candidate. Kết quả là **không tìm được route đạt gate trong phạm vi tìm kiếm hữu hạn này**, không phải chứng minh rằng mọi continuation hình học đều bất khả thi.

## Collision assurance và giới hạn

Navmesh ghi nhận agent radius `0.1 m`, height `1.5 m`, max climb `0.2 m`, max slope `45°`, cell size `0.05 m`, cell height `0.2 m`, `include_static_objects=false`. Route runner dùng `Simulator.step_filter → PathFinder.try_step_no_sliding`; physics tắt. Các gate mô tả navmesh/free-space và action movement filter, không kiểm tra độc lập va chạm với static scene objects. Vì thế, ngay cả một route pass cũng không chứng minh an toàn vật lý tuyệt đối.

## Run, timing và policy

- Không chạy capture, producer, inference, mapper/native worker, semantic update, browser/server bootstrap hoặc retry.
- Không sinh hay thay route config. Không có `max_camera_frames`, keyframe budget hoặc producer timeout mới được khóa; do đó không được coi pilot preflight là pass.
- Policy/config T4 cũ được giữ nguyên. `max_camera_frames=400` hiện không đủ cho mục tiêu 45–60 giây ở 40 ms/action; nếu có route mới đạt hình học, guard phải được tính lại từ route và policy trong config version mới trước khi mở pilot.
- Không thay artifact/cache nguồn, model, matcher, thresholds, A tables, admission/grouping, scope hoặc verified-only.

## Hashes của đầu vào được đọc

| Input | SHA-256 |
|---|---|
| T1.1 source route | `f76c124bc80b7a9a895666a7802b00abd48ef5ba20a2d692d6e02703abbd20a0` |
| HM3D 00802 navmesh | `ae5ddd8ca77480eea3c401059844ae1ccc42d9cce75770a25b0d79b02d077232` |
| T4 effective config cũ | `adb5f97c16b1627b39026e2fa98354ddd38a99ece46b6e6be7eabe8ced9497e7` |
| T4 timebase policy cũ | `74ddfd4399af8cbb84e3623492df2a90fd4f53c84fc5b8bef5e2223d7a580765` |
| Habitat route runner | `a706e338cb382c774a362757126327a8495635a173767554dd559f6a4075a778` |
| Route geometry/clearance helper | `1cc0d666630ff199fa5d0843645834cdbd68912610720c2c1561af06cfc0d590` |

## Trạng thái kế hoạch và bước tiếp theo

Theo kế hoạch smooth-route, phần capture → perception → hai mapper → unified revisions đã có producer execution trước đây; gate còn thiếu là nhiều graph revisions và GUI render trong lúc camera capture đang chạy. Lượt này không bổ sung bằng chứng overlap vì route continuation không đạt preflight.

Bước nhỏ nhất tiếp theo là **một geometry-only proposal cho route HM3D 00802 có start/continuation khác**, được khóa từ navmesh trước inference. T1.1 prefix hiện tại không được nối nếu continuation vẫn fail gate; cần người dùng duyệt đổi đoạn route trước khi tạo config mới. Không chạy producer hoặc semantic replay trong bước này.
