# M3 status — Structural free-space projection

Ngày: 25-09-2026

## Phạm vi đã triển khai

- Thêm `FreeSpaceGrid2D` với contract `values:int8[H,W]`, `-1=unknown`,
  `0=structural free`, `1..100=occupation score`, world `origin_xy`,
  resolution, revision, frame, height diagnostics và changed bounds.
- `project_snapshot()` nhận accumulated occupancy snapshot từ M2; không dùng
  observations của scan cuối.
- `project_leaves()` là Python port để debug/test theo dependency MapConversion:
  `MapConverter::updateMap`, `HeightRangeMap` interval union/filter và
  `Map2D` row/column convention. Có rasterization leaf lớn theo các XY cells
  mà leaf bao phủ, interval epsilon `resolution * 0.001`, strict
  `height > minZ`, unknown gaps không được nối và neighbour occupation scan.
- `project_snapshot()` và `project_leaves()` vẫn là `python_port_debug`; Python
  port không thay reference/runtime backend.
- Native M2 worker link trực tiếp `MapConverter.cpp` từ checkout đã pin, giữ
  một `ProjectionBackend`/`MapConverter` cùng process với OcTree và nhận
  operation `project full|partial`. Response có revision, origin/world grid,
  occupation, floor/ceiling heights, changed bounds, mode và cờ `updated`.
- `full_refresh` gửi toàn bộ leaf tích lũy vào cùng MapConverter đang sống;
  đây **không phải** khởi tạo Map2D mới/xóa projection cũ. `partial_update`
  theo callback nguồn: so sánh key và occupancy để lấy changed XY, sau đó thay
  Z bằng toàn metric Z range của cây mới, truy vấn leaf trong BBX và gọi
  `updateMap` nếu BBX còn leaf. Full-map bounds và BBX query dùng
  `getMetricMin/Max`, không lấy tâm leaf làm map extent.
- Harness biên dịch source C++ đã pin thành reference oracle trên fixtures.
  Parity kiểm occupation, floor/ceiling và world cell coordinates trong vùng
  giao giữa output Map2D và lưới Python.

## Files chính

- `src/standalone_sg/mapping/projection.py`
- `src/standalone_sg/mapping/native.py`
- `native/occupancy/projection_backend.{hpp,cpp}`
- `native/occupancy/projection_backend_smoke.cpp`
- `native/CMakeLists.txt`, `native/occupancy/CMakeLists.txt`,
  `native/occupancy/fixture_runner.cpp`
- `src/standalone_sg/mapping/__init__.py`
- `src/standalone_sg/config/schema.py`
- `configs/reference_code.yaml`
- `configs/reference_code_mp3d.yaml`
- `configs/test_fixture.yaml`
- `tests/fixtures/m3_mapconversion_reference.json`
- `tests/fixtures/m3_mapconversion_disjoint_ranges.json`
- `tests/fixtures/m3_mapconversion_resolution005.json`
- `tests/test_m3_projection.py`
- `tests/test_m3_source_parity.py`
- `tests/test_m3_native_integration.py`

## Reference/provenance

- MapConversion source commit:
  `14b8cb9e6534ba21e84f624c94d20960f6613f3c`.
- Golden fixtures ghi source commit và thuật toán. Source checkout được lấy từ
  `locks/sources.json`, override được bằng
  `STANDALONE_SG_MAPCONVERSION_SOURCE`; harness kiểm tra commit trước khi build.
- Kết luận parity giới hạn theo từng fixture và vùng tọa độ world giao nhau;
  chưa suy rộng thành parity toàn M3 hoặc toàn callback.
- Các test có clearance quanh `0.3 m`, epsilon boundary, unknown gap, low
  table, wall, leaf lớn, tọa độ âm và origin dịch chuyển; source parity thêm
  fixture `0.05 m`, height diagnostics, world coordinates, hai khoảng Z rời
  nhau và neighbour occupation.

## Kết quả

- M3 projection/debug/source/integration tests: **19 passed**, gồm direct parity cases build/run
  `MapConverter.cpp` từ checkout đã pin.
- Native M2+M3 build pass với local OctoMap prefix, Eigen và MapConversion
  commit `14b8cb9e6534ba21e84f624c94d20960f6613f3c`; không build ROS node vì
  ROS transport không thuộc target.
- Toàn bộ regression M0–M3: **59 passed**. Integration gồm
  `integrate → project → integrate → project`, revision, world grid,
  occupation/heights, occupancy change, map expansion/origin âm và 48 lần
  full-refresh liên tiếp. RSS tăng dưới ngưỡng test 12 MiB trên fixture nhỏ.
- Callback reference harness tái hiện cả bounds preparation, BBX leaf query và
  `MapConverter::updateMap`: full metric bounds khớp; partial occupancy flip
  khớp sau khi mở Z; xóa một leaf trong cột còn leaf được cập nhật; xóa hết
  leaf của cột tạo BBX rỗng và giữ nguyên projection như nguồn.

## Ranh giới còn lại

- Deletion parity phân biệt hai trường hợp: xóa một leaf nhưng cột còn leaf
  cùng XY thì full-Z BBX vẫn được cập nhật; chỉ khi xóa hết leaf trong vùng XY
  truy vấn rỗng, `MapConverter::updateMap(empty, ...)` return ngay và projection
  cũ giữ nguyên (stale cell). Đây là fidelity theo nguồn, không phải deletion
  clearing.
- `full_refresh` là replay toàn bộ cây lên **MapConverter stateful hiện tại**,
  không phải independent clean rebuild; mode này chứng minh độ ổn định khi lặp
  trên cùng state, không chứng minh tương đương partial-update. Không recreate
  Map2D mỗi lần vì source dùng raw submap allocations và không cung cấp
  ownership/reset rõ ràng.
- Native tests cover các trường hợp callback đã nêu nhưng chưa tuyên bố parity
  toàn callback/pipeline. C++ harness source fixture tiếp tục là oracle;
  Python debug parity vẫn fixture-scoped.
- Chưa triển khai DuDe, region segmentation hoặc SceneGraphBuilder.
- Kaggle sạch vẫn là portability gate riêng: cần dựng lại local dependency
  artifact và chạy native build trong image mục tiêu.
