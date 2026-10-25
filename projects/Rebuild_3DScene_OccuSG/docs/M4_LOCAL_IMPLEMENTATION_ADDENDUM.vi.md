# Addendum M4 local — 2026-09-27

Đọc cùng [`M4_STATUS.vi.md`](M4_STATUS.vi.md). Addendum này cập nhật kết quả
thực thi local; không thay gate acceptance nguyên thủy ở Phase 1.

## Trạng thái tách riêng

- **technical_local: đạt cho executable và pipeline local hiện tại.** Worker
  C++ chạy pinned `Incremental_Decomposer`, DuDe wrapper và archive FIST thật;
  không ROS runtime. Python gửi `FreeSpaceGrid2D`, nhận raw regions/status, rồi
  gọi M5 guard trước tracker. Không thay kernel bằng thuật toán khác.
- **reference_parity: đạt có giới hạn cho 5 fixture đầu vào đơn khung**
  (rectangle, L-shape, doorway, obstacle, all-unknown): direct-source oracle
  build riêng, tự tái tạo callback preprocessing, gọi trực tiếp source pinned
  và active `are_contours_connected`, không dùng worker parser/serializer.
  So sánh branch, số region, canonicalized outer geometry (tọa độ world, làm
  tròn 5 chữ số), area (abs tol `1e-8`) và adjacency. Đây không phải parity
  multi-update state, toàn ROS callback hoặc toàn pipeline OccuSG.
- **cloud_validation: chưa chạy** (Kaggle clean image chưa build); không upload
  source/archive/artifacts.
- **licensing/distribution: chưa resolve.** Người dùng cho phép thử nghiệm
  academic phi thương mại local với checkout/archive có sẵn. Đây là permission
  thao tác cho lượt này, không phải grant từ tác giả. Không kết luận quyền
  cloud, sửa/redistribute hay phát hành; DuDe/mathtool/FIST gates vẫn mở.

## Source closure và bản vá ngoài baseline

`native/dude/CMakeLists.txt` build executable worker. TUs là các GLOB upstream
`Third_Party/dude_final/src/*.cpp`, `skeleton/*.cpp`, `graph/*.cpp`, `draw/*.cpp`
(trừ `main.cpp`), cùng `include/wrapper.cpp` và `include/inc_decomp.cpp` được
configure-copy vào build dir, `globals.cpp`, `preprocessing.cpp`, `worker.cpp`.
ROS node/evaluation/messages/publishers và GUI không được link vào runtime.
Đây là closure build/run đã chứng minh, chưa tuyên bố minimum-TU closure.

Adapter logging chỉ thay log macros, output sang stderr; worker JSON Lines stdout
được giữ riêng. FIST static archive đã có, không build/sửa/tải lại; binary là
executable `-no-pie`, không phải Python extension. Baseline
`ref_OccuSG/OccuSG` không bị sửa. Source lock lưu commit, FIST SHA-256,
parameter provenance và patch ledger.

Preprocessing tái hiện callback: signed `-1` thành byte `255`, phép
`Occ_Image < 101`, bbox/crop union, source `clean_image2`, dán crop về full-size,
không flip; `pixel_tau=threshold_m/resolution`. Input occupancy bytes không bị
nhầm với binary result sau preprocessing. Origin đi riêng, contour coordinates
được map `x=origin_x+col*resolution`, `y=origin_y+row*resolution`. Active
adjacency dùng `are_contours_connected`. Pinned `Stable_graph` và raw ROS message
chỉ phát outer ring; output ghi rõ holes không được hỗ trợ.

Parameters từ pinned `config/inc_dude_params.yaml`: threshold `1.5 m`, safety
distance `0.5 m`; profile local tại `configs/m4_local.yaml`. Worker protocol v1
gồm integrate/reset/close, request ID/revision, grid shape/data/geometry/frame,
config, branch/rejection reason, `status=success|fallback|rejected`,
`protocol_status`, polygons/raw IDs/adjacency, units/frame, crop/tau/unknown-byte
diagnostics và flag `kernel_invoked`. Thay config/resolution mid-state bị reject
cho tới explicit reset. Python client áp dụng deadline cho toàn request, drain
stderr, kill process group khi timeout và chuyển client sang error state khi
response dở, sai protocol hoặc process chết; worker restart không tự replay.
Caller phải mở process mới và replay từ đầu. M5 guard reject không age/refresh
tracker; điều này không rollback state native DuDe.

## Kết quả tái lập

```sh
cmake -S native/dude -B build/native-m4-dude -DCMAKE_BUILD_TYPE=Release \
  -DM4_PREFIX="$PWD/build/deps/m4-prefix/usr" \
  -DGMP_INCLUDE_DIR="$PWD/build/deps/m4-prefix/usr/include/x86_64-linux-gnu" \
  -DGMP_LIBRARY_RELEASE="$PWD/build/deps/m4-prefix/usr/lib/x86_64-linux-gnu/libgmp.so" \
  -DGMP_LIBRARY_DEBUG="$PWD/build/deps/m4-prefix/usr/lib/x86_64-linux-gnu/libgmp.so" \
  -DMPFR_INCLUDE_DIR="$PWD/build/deps/m4-prefix/usr/include" \
  -DMPFR_LIBRARIES="$PWD/build/deps/m4-prefix/usr/lib/x86_64-linux-gnu/libmpfr.so"
cmake --build build/native-m4-dude --target occusg_dude_worker --parallel 2
cmake -S native/dude_reference -B build/native-m4-dude-reference \
  -DCMAKE_BUILD_TYPE=Release -DM4_PREFIX="$PWD/build/deps/m4-prefix/usr" \
  -DGMP_INCLUDE_DIR="$PWD/build/deps/m4-prefix/usr/include/x86_64-linux-gnu" \
  -DGMP_LIBRARY_RELEASE="$PWD/build/deps/m4-prefix/usr/lib/x86_64-linux-gnu/libgmp.so" \
  -DGMP_LIBRARY_DEBUG="$PWD/build/deps/m4-prefix/usr/lib/x86_64-linux-gnu/libgmp.so" \
  -DMPFR_INCLUDE_DIR="$PWD/build/deps/m4-prefix/usr/include" \
  -DMPFR_LIBRARIES="$PWD/build/deps/m4-prefix/usr/lib/x86_64-linux-gnu/libmpfr.so"
cmake --build build/native-m4-dude-reference --target occusg_dude_source_reference --parallel 2
PYTHONPATH=src python3 -m pytest -q tests/test_m4_dude.py tests/test_m4_worker_lifecycle.py tests/test_m4_source_parity.py
PYTHONPATH=src python3 scripts/m4_local_demo.py
```

Checkpoint trước lượt hardening: worker build/link thành công; khi đó có 12
M4 tests và 106 full-suite tests. Các con số/hành vi demo ở checkpoint này đã
được thay thế bởi kết quả cuối trong mục “Hardening incremental và parity
stateful” bên dưới. Đặc biệt, backend-rejected contour không còn được gửi như
M5 evidence mới.


## Hardening incremental và parity stateful — 2026-09-27

### Contract backend rejection → M5

- `DuDeWorkerClient.integrate()` tạo `RegionObservation` chỉ cho status
  `success` hoặc `fallback`. Khi status `rejected`, các contour giữ lại trong
  response native chỉ là diagnostic; trả về không observation. `update_regions()`
  không gọi guard, `RegionUpdateProcessor.update()`, tracker hay guard commit.
- Đầu ra hiển thị là snapshot đã publish gần nhất bởi M5, đánh dấu `stale`;
  geometry, timestamp và `source_grid_revision` giữ nguyên provenance cũ.
  `request_revision` riêng với `accepted_observation_revision`. Native DuDe vẫn
  có state riêng; adapter không tuyên bố rollback.
- Branch policy: `success` và `fallback` là candidate để M5 guard xét; fallback
  không tự bị gắn nhãn rejection. Các source-accepted reuse branch như
  `accept_reuse_adjusted_no_differential` cũng là candidate update. Nếu M5 guard
  từ chối candidate, chính M5 giữ stale snapshot và không age/refresh tracker.
  Chỉ status `rejected` từ backend bỏ qua M5 hoàn toàn.

### Oracle stateful và điều tra t2/t3

Harness `occusg_dude_source_reference demo_sequence` giữ một object
`Incremental_Decomposer` xuyên suốt năm revision, tự dựng các input fixture,
preprocess trực tiếp bằng OpenCV callback ops đã tái hiện, rồi gọi source và
`are_contours_connected`; không reset từng frame và không dùng worker serializer.
Test chạy một worker sống qua cùng sequence, đối chiếu mỗi revision: branch,
algorithm status, mask đã preprocess, số pixels, canonical geometry/world
coordinates, area và adjacency.

Nguồn và worker cùng trả `reject_expanded_empty_preserve_previous` tại t2/t3,
với cùng mask (`10664` rồi `17019` free pixels) và cùng geometry/adjacency. Khi
chạy grid t3 một mình trên decomposer mới, source trả `accept_incremental` và
geometry mới; trong sequence incremental, nó giữ contour cũ và từ chối candidate.
Điều này cô lập nguyên nhân cho fixture/tham số/nhánh stateful của source trên
sequence hiện tại, không phải sai preprocessing hay adapter. Không sửa kernel,
threshold, safety distance hoặc default để ép kết quả.

### Sequence hình học thành công

Sequence khác bắt đầu với một phòng nhìn thấy được, sau đó lộ phòng thứ hai
phía sau doorway. Cùng worker sống cho hai lần `accept_incremental`; số region
tăng từ 1 lên 2, adjacency raw là `0↔1`. Direct-source live oracle khớp branch,
status, mask, geometry, area và adjacency ở cả hai revision. Đây là synthetic
fixture kiểm chứng khả năng update geometry, không phải bằng chứng tổng quát
hoặc trajectory robot. Demo lưu grid/masks và summary tại
`artifacts/m4_local_demo/doorway_geometry/`.

### Regression và acceptance boundary

Regression t1 accepted → t2/t3 backend-rejected → t4 accepted reuse xác nhận ở
t2/t3: tracker accepted-update index, `last_seen_s`, `miss_count`, source revision
và guard published snapshot không đổi; t4 được chấp nhận và refresh cùng ID.
Lưu ý t4 quay lại đúng grid `partial` của t1; regression này chứng minh xử lý
lại một update được source chấp nhận sau rejection, **không** chứng minh source
tự thoát trạng thái giữ contour khi agent tiếp tục mở rộng bản đồ liên tục.

Kết quả chạy cuối: **15 M4 pytest tests pass, 0 skipped**; full suite **109
passed, 0 skipped**. Đây là pytest test functions thu thập được; scenario nằm
trong loops không cộng thành test riêng. Các fixture/parity scenarios: 5 fixture
đơn, 5 revision trong sequence rejection/reuse, 2 revision doorway geometry, và
một fresh-final comparison.

M4 vẫn là technical-local, không nghiệm thu toàn pipeline: chưa xác nhận clean
Kaggle build, clearance DuDe/mathtool/FIST, cloud/distribution hoặc parity toàn
callback/pipeline.
