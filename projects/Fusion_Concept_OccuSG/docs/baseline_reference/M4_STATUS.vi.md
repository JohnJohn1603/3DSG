# M4 — Khảo sát khả năng tái sử dụng và build (chưa nghiệm thu)

> Recovery-fixture limitation: accepted update after backend rejections returns to the original `partial` grid; it does not continue an expanded exploration sequence. It proves processing of a source-accepted update after rejection, not automatic recovery under continued map expansion.

Ngày kiểm tra: 2026-09-27. Baseline là OccuSG commit
`2bca2fa06af87fd9dd0542957039be8f580f0dca` theo `locks/sources.json`.

## Kết luận

M4 được chấp nhận làm **nền tảng technical-local/reference-parity trong fixtures và sequences đã kiểm thử**. M4 **chưa nghiệm thu đầy đủ**: cloud/Kaggle validation và quyền sử dụng/phân phối vẫn mở. Worker DuDe/FIST chạy local; xem [addendum](M4_LOCAL_IMPLEMENTATION_ADDENDUM.vi.md).

### Cập nhật hardening incremental M4→M5 (2026-09-27)

- `status=rejected` hiện không tạo observation và không gọi M5 processor. Client
  trả published M5 snapshot gần nhất ở trạng thái stale, giữ timestamp/source
  revision cũ; request revision tách khỏi accepted observation revision. Không
  giả định native state đã rollback. `success`/`fallback` đều được xem là
  candidate cho guard; nhánh source-accepted reuse vẫn đi qua guard/tracker,
  không bị gộp vào backend rejection.
- Oracle C++ trực tiếp giữ một `Incremental_Decomposer` qua sequence năm revision.
  Worker sống và oracle sống khớp branch/status, preprocessed mask, geometry,
  area và adjacency trên sequence demo; fresh final grid cho kết quả khác với
  final grid trong state incremental. Vì vậy t2/t3 được quy về branch
  `reject_expanded_empty_preserve_previous` của source đối với sequence này,
  không phải khác biệt preprocessing/serialization của adapter. Đây không phải
  kết luận tổng quát về mọi cấu hình hoặc fixture.
- Sequence hình học riêng cập nhật phòng sau doorway thành 2 regions kề nhau,
  parity với source; không dùng để áp đặt số phòng lên sequence rejection.
- Tại checkpoint nghiệm thu M4 trước M6: 15 M4 pytest tests pass, full suite
  109 passed, 0 skipped. Sau khi thêm M6, tổng suite hiện là 120 passed (xem
  [M6 status](M6_STATUS.vi.md)); các fixture loop không phải pytest tests riêng.
- M4 vẫn chưa nghiệm thu: clean Kaggle build và clearance/cloud/distribution
  gates còn mở.
Các quyền và giới hạn kỹ thuật cần tách bạch:

1. **Quyền FIST cần người dùng xác nhận theo cách sử dụng cụ thể.** Thông báo
   trong `Third_Party/dude_final/FIST/makefile:943–953` cho phép dùng miễn phí
   phi thương mại, nội bộ tổ chức và academic evaluation; nói rõ không được
   nhân bản, sửa đổi, bán hoặc phân phối lại nếu chưa có chấp thuận trước bằng
   văn bản; commercial application cần commercial license. Văn bản này không
   nói riêng cloud execution/build artifacts, nên báo cáo không tự kết luận rằng
   mọi thao tác compile đều cần consent, cũng không suy ra chạy trên cloud được
   phép. Trang chính thức nói FIST không thuộc public domain và yêu cầu liên hệ
   tác giả để biết điều khoản:
   [FIST — Martin Held](https://cosy.sbg.ac.at/~held/projects/triang/triang.html).
   OccuSG MIT không cấp thay quyền của FIST. `package.xml` của package DuDe để
   `<license>TODO</license>`; các file trong `Third_Party/dude_final/mathtool`
   tham chiếu `LICENSE` nhưng file đó không có trong cây package đã kiểm tra.
   Đây là khoảng trống clearance riêng, không chỉ là vấn đề FIST.
2. **Toolchain độc lập đã thực sự build và chạy.** Trên Ubuntu 24.04.4 Noble
   amd64, GCC 13.3.0, CMake 3.28.3, CGAL 5.6, GMP 6.3.0 và MPFR 4.2.1,
   executable riêng trong `native/m4_dependency_probe/` đã compile/link/run
   constrained-Delaunay kernel tương tự kiểu API được DuDe dùng, cùng các lời
   gọi GMP/MPFR trực tiếp. CTest **1/1 pass**. Dùng `.deb` từ Ubuntu archive đã
   verify SHA-256, extract vào `build/deps/m4-prefix`; không chạy apt install,
   không sửa system packages. Đây **không** phải DuDe/FIST build hoặc M4 parity.
3. **Dependency closure của worker local đã build; tối thiểu tuyệt đối chưa chứng minh:** CMake upstream yêu cầu
   CGAL Core và MPFR; fallback link thêm GMP; wrapper preprocess cần OpenCV
   core/imgproc; bundled DuDe gọi FIST static archive. CGAL/GMP/MPFR development
   headers/config hiện đã có trong local prefix. Boost 1.83 dev được tìm thấy
   từ hệ thống vốn có, không cài mới. Không cài ROS để vượt lỗi configure và
   không build/sửa/đóng gói FIST trong lượt này.

Clearance và môi trường Kaggle vẫn là blockers nghiệm thu, không phải lý do thay
DuDe bằng heuristic khác. Worker local và fixtures chạy DuDe thật đã được tạo;
kết quả hiện hành ở addendum bên dưới.

## Checklist mở M4 — việc làm được ngay và xác nhận cần thiết

### AI có thể làm ngay trong phạm vi workspace

- [x] Tách M4 thành dependency, source/build, behavior/parity, runtime, và
  distribution/clearance gates; M4 tiếp tục **chưa nghiệm thu**.
- [x] Dành local prefix `build/deps/m4-prefix`, tải và extract package Noble
  amd64 có provenance/hash, rồi build/link/run executable CGAL/GMP/MPFR độc lập.
- [x] Chốt và build translation-unit closure không ROS (closure build được; chưa chứng minh tối thiểu tuyệt đối) bằng
  source/build audit; CMake ROS/GLOB hiện tại chưa chứng minh được tập tối thiểu.
- [x] Ghi version/license/hash cho local-prefix probe và build/run target đó.
  Probe độc lập không phải bằng chứng DuDe parity.
- [x] Build CMake target standalone DuDe không ROS cho local technical run theo
  permission thao tác academic/local đã được người dùng cấp; không suy rộng thành
  grant của chủ sở hữu hoặc quyền cloud/phân phối. Minimum-TU closure chưa chốt.
- [x] Chạy rectangle, L-shape, doorway và
  map-expansion fixtures; so sánh polygon rings/holes, adjacency/status với
  pinned source. Không dùng heuristic thay thế.
- [x] Kiểm tra worker process/repeated calls, cleanup, error/fallback/reject status
  và live sequence parity cục bộ.
- [ ] Build và chạy trên môi trường Kaggle sạch; local success không thay thế gate này.

### Chỉ người dùng/chủ thể có thẩm quyền mới xác nhận được

- [ ] Quyền dùng FIST cho mục đích dự án cụ thể, gồm nội bộ/tổ chức, academic
  hay thương mại; cloud/provider-managed execution; source modification; và
  phân phối archive/object/static/shared binary, notebook/container hoặc public
  repository. Nếu cần, xin văn bản trực tiếp người giữ quyền.
- [ ] Quyền/điều khoản DuDe (package metadata hiện `TODO`) và `mathtool` (license
  được tham chiếu nhưng không có trong checkout); xác định riêng use, modification,
  cloud execution và redistribution. Không suy quyền từ OccuSG MIT.
- [ ] Chấp thuận nguồn cung và cách lưu dependency artifacts trong prefix, cùng
  license obligations của CGAL package/modules và GMP/MPFR cho mục đích phát
  hành dự kiến. Không suy rằng người dùng có quyền phân phối dependency.
- [ ] Chọn mục tiêu triển khai cần clearance: chỉ local research run, cloud
  run, artifact nội bộ, hay public/commercial distribution.

### Bản nháp hỏi quyền (chưa gửi)

> Chúng tôi đang đánh giá một worker C++ nội bộ cho dự án nghiên cứu phân vùng
> phòng, dựa trên OccuSG/DuDe và phần `mathtool` đi kèm; upstream package hiện
> không có license grant rõ ràng trong checkout. Xin xác nhận bằng văn bản
> phạm vi được phép cho: (1) biên dịch/chạy nội bộ phi thương mại tại máy của
> nhóm; (2) biên dịch/chạy trên cloud hoặc hạ tầng bên thứ ba; (3) sửa source;
> (4) lưu/chia sẻ source, object/static/shared binary, container/notebook và
> log/fixture; (5) phát hành công khai hoặc dùng thương mại. Xin chỉ rõ điều
> khoản áp dụng riêng cho DuDe và `mathtool`, và đầu mối có thẩm quyền cấp phép.
>
> Riêng FIST, notice đi kèm nêu điều kiện academic/non-commercial/internal và
> hạn chế modification/redistribution. Xin xác nhận liệu phạm vi dự án, cloud
> execution/build, các artifact nêu trên và cách tích hợp FIST vào worker có
> được phép không; nếu không, xin báo điều kiện xin consent/license. Chúng tôi
> chưa build hoặc phân phối thành phần này và chưa coi câu hỏi này là đã được
> chấp thuận.

Đây chỉ là nội dung chuẩn bị để người dùng xem/chỉnh/gửi; không có email hay
liên hệ bên ngoài nào được gửi.

## Phạm vi C++ có thể tách sau khi clearance được giải quyết

Ứng viên tái sử dụng trực tiếp từ package đã pin (chưa xác nhận build-minimal):

- Preprocess adapter từ `src/inc_dude.cpp`: occupancy bytes → `cv::Mat`, chọn
  `find_image_bounding_Rect`, crop, rồi `clean_image2`; giữ nguyên ngưỡng và
  OpenCV ops nguồn. ROS `OccupancyGrid.data` dùng signed int8; khi được view
  như `uint8`, `-1` có byte value `255`. `find_image_bounding_Rect` dùng phép
  so sánh `Occ_Image < 101`; **101 là ngưỡng**, không phải unknown sentinel.
  Vì vậy phải bảo toàn byte `255` cho unknown và test chính phép so sánh này;
  không remap unknown thành 101.
- `include/wrapper.cpp/.hpp` và lõi `Third_Party/dude_final/src` (DuDe,
  skeleton/graph cần thiết), OpenCV, CGAL, GMP, MPFR và FIST static archive.
  Bản ROS hiện gom source bằng GLOB; tập translation unit tối thiểu chưa được
  xác minh do hai gate đang chặn build.
- `include/inc_decomp.cpp/.hpp` chỉ cần nếu worker nhận chuỗi update stateful;
  đây là policy/stable-state wrapper có nhánh reuse/fallback/reject, không chỉ
  là thuật toán Dual-Space. Không kéo ROS node, ROS messages, image_transport,
  launch, publishers hoặc GUI/OpenGL vào worker.
- Active adjacency path của OccuSG là
  `publish_regions_with_validation()` → `build_raw_region_message()` →
  `Incremental_Decomposer::are_contours_connected()`; tracker remap raw IDs
  trong adjacency sau khi guard accept. `wrapper.extract_graph()` có tồn tại
  nhưng không nằm trong active call path, nên không dùng làm reference mặc định.
  Output worker phải mang status riêng cho success, fallback, reject/error và
  revision; không coi fallback contour là kết quả DuDe thành công.

Coordinate convention cần giữ: `mapCallback` đưa occupancy bytes row-major vào
`cv::Mat` không flip trước decomposition, tính `pixel_Tau = threshold_m /
resolution`, crop cho cleaning rồi dán lại đúng `first_rect`. `origin` truyền
riêng qua stateful decomposer. `CoordinateConverter` ánh xạ pixel trực tiếp:
`world_x = origin_x + col * resolution`,
`world_y = origin_y + row * resolution`; `cv::flip` trong callback chỉ dành cho
ảnh publish/debug. Crop offset phải được cộng lại nếu worker phân rã cropped
image thay vì full-size image.

## Kiểm tra đã chạy trước khi có worker (kết quả hiện tại ở addendum cuối tài liệu)

- Read-only audit source/CMake/license trong checkout đã pin; không sửa
  `ref_OccuSG`.
- Nguồn package: Ubuntu archive chính thức `archive.ubuntu.com`, suite Noble /
  Noble-updates, amd64. Download dùng exact versions từ APT Packages metadata;
  `sha256sum` sau download khớp cả năm SHA-256 trong source lock. `.deb` được
  giải nén bằng `dpkg-deb -x` vào local prefix, không cài bằng dpkg/apt.
- Packages và license metadata: `libcgal-dev 5.6-1build3` (CGAL per-file
  GPL-3-or-later/LGPL-3-or-later; probe dùng **2D Triangulations + 2D TDS,
  GPL**), `libgmp-dev` và `libgmp10 2:6.3.0+dfsg-2ubuntu6.1` (GPL-2-or-later
  **hoặc** LGPL-3-or-later), `libmpfr-dev` và `libmpfr6 4.2.1-1build1.1`
  (LGPL-3-or-later). Giữ notices nằm trong prefix; đây không phải kết luận
  tương thích license hay quyền phân phối cho sản phẩm.
- Official references: [Ubuntu Noble libcgal-dev](https://packages.ubuntu.com/noble/libcgal-dev),
  [Ubuntu Noble libgmp-dev](https://packages.ubuntu.com/noble/amd64/libgmp-dev),
  [Ubuntu Noble libmpfr-dev](https://packages.ubuntu.com/noble/libmpfr-dev),
  [CGAL 5.6 2D Triangulations license](https://doc.cgal.org/5.6/Triangulation_2/group__PkgTriangulation2Ref.html),
  [CGAL licensing by package/file](https://www.cgal.org/license.html),
  [GMP copyright/license](https://gmplib.org/manual/Copying), and
  [MPFR license](https://www.mpfr.org/). Package hashes were checked locally
  against configured Ubuntu APT package metadata.
- Download vào `build/deps/m4-cache`:

  ```sh
  cd build/deps/m4-cache
  apt-get download libcgal-dev=5.6-1build3 libgmp-dev=2:6.3.0+dfsg-2ubuntu6.1 \
    libmpfr-dev=4.2.1-1build1.1 libgmp10=2:6.3.0+dfsg-2ubuntu6.1 \
    libmpfr6=4.2.1-1build1.1
  sha256sum *.deb
  for p in *.deb; do dpkg-deb -x "$p" ../m4-prefix; done
  ```

- Hashes (the same hashes are recorded package-by-package in
  [`locks/sources.json`](../locks/sources.json)):

  | Package/version (amd64) | SHA-256 |
  |---|---|
  | `libcgal-dev 5.6-1build3` | `4ad9d51506d42089b3cf14adfb8b9d9d79a9d32fc08f649feaf4b95f6e28cfb0` |
  | `libgmp-dev 2:6.3.0+dfsg-2ubuntu6.1` | `a9847b5ecfff791a46cb198f29a06d9d23a20afdfdd434812f2b44ccfb61d46a` |
  | `libgmp10 2:6.3.0+dfsg-2ubuntu6.1` | `285f8a505dfa8e1b33f357a9d8d3477ad35bf18c0b34771a6df4c25923f3ae0d` |
  | `libmpfr-dev 4.2.1-1build1.1` | `362fcdeb7dff545af83e65a3a972519185efd4a231c23e13d519cf97a5c8e3e4` |
  | `libmpfr6 4.2.1-1build1.1` | `aebc1c8b69a1f98bb43dfc268daecd181116dbf40b13ee4e822eb4bdd52b493a` |

- Configure:

  ```sh
  cmake -S native/m4_dependency_probe -B build/native-m4-dependency-probe \
    -DM4_DEPENDENCY_PREFIX="$PWD/build/deps/m4-prefix/usr" \
    -DCMAKE_PREFIX_PATH="$PWD/build/deps/m4-prefix/usr"
  ```

  Result: CGAL 5.6 found; GMP header at
  `build/deps/m4-prefix/usr/include/x86_64-linux-gnu`, MPFR header at
  `build/deps/m4-prefix/usr/include`, both libraries and CGAL config resolved
  from `build/deps/m4-prefix/usr/lib/x86_64-linux-gnu`. Existing Boost 1.83
  development files resolve from `/usr`; no new system package was installed.
- Clean build: `cmake --build build/native-m4-dependency-probe --clean-first
  --verbose --parallel 2` — **target created and built successfully**. Compile
  used `-std=c++17 -frounding-math`, local prefix include paths and system Boost
  `/usr/include`; link used local `libgmp.so`, `libmpfr.so`, `libgmpxx.a`, and
  RUNPATH to the local multiarch library directory.
- Run: `./build/native-m4-dependency-probe/m4_dependency_probe` — exit 0,
  `CGAL CDT vertices=4 constrained_edges=5; GMP multiply=ok; MPFR sqrt2=ok`.
  `ctest --test-dir build/native-m4-dependency-probe --output-on-failure` —
  **1/1 passed**. `ldd` confirmed `libgmp.so.10` and `libmpfr.so.6` resolve
  from the local prefix.
- The probe uses the pinned source's type family/API:
  `Exact_predicates_inexact_constructions_kernel`,
  `Triangulation_vertex_base_2`, `Constrained_triangulation_face_base_2`,
  `Triangulation_data_structure_2`, `Exact_predicates_tag`, and
  `Constrained_Delaunay_triangulation_2`. It directly exercises GMP integer
  multiplication and MPFR 128-bit square root. No DuDe source was compiled,
  linked, or executed; FIST was not accessed by the probe.
- `cmake -S ref_OccuSG/OccuSG/src/incremental_dude_ros2/incremental_dude_ros2
  -B /tmp/occusg-m4-configure-probe` — **FAIL** tại thiếu `ament_cmake`; chỉ là
  kết quả configure package ROS gốc, không phải standalone build result.
- ROS 2/ament không cần cho probe; ROS package configure cũ vẫn fail tại
  `ament_cmake`, không liên quan đến native toolchain probe đã pass.
- Ở thời điểm khảo sát ban đầu chưa chạy DuDe/FIST; kết quả build local sau đó ở addendum cuối tài liệu.
- Ghi nhận SHA-256 archive bundled `FIST/libFIST.linux64.a`:
  `84875da959aae5a1abdddc993075d0f91d4afdbdda1a29dd856f77254a63c684`.
  Hash chỉ nhận diện artifact, không chứng minh quyền sử dụng hay build thành
  công.
- The initial survey did not have these tests; the local fixture and integration results are summarized in the addendum linked above.

## Câu hỏi cần người dùng trả lời / chuyển đúng chủ sở hữu (chưa gửi)

- **Người dùng trả lời:** dự án thuộc tổ chức/nhóm nào; mục đích academic,
  internal non-commercial hay commercial; chỉ chạy local hay cả cloud, và nếu
  cloud thì provider/tenant/hạ tầng bên thứ ba nào?
- **Người dùng trả lời:** có dự định sửa DuDe, `mathtool`, FIST hoặc CGAL không?
  Có tạo/chia sẻ source, object/static/shared binaries, notebook/container,
  logs/fixtures; chia sẻ nội bộ, public repository hay thương mại không?
- **Người dùng trả lời:** xác nhận toolchain test hiện tại chỉ chạy local nội bộ
  và không phát hành executable/package? Điều này không hàm ý được phép tích hợp
  DuDe/FIST.
- **Gửi chủ sở hữu DuDe và `mathtool` (hỏi riêng từng thành phần):** xin văn bản
  cho use/compile/run; academic/internal/commercial purpose; local so với cloud;
  source modification; linking/integration; và redistribution source/binary/
  container/notebook. Hỏi ai có thẩm quyền và license/notice nào áp dụng.
  Package metadata để `license=TODO`, còn `mathtool/LICENSE` không có trong
  checkout; không xem đó là grant.
- **Gửi chủ sở hữu FIST:** hỏi riêng phạm vi project/use, local/cloud build-run,
  source changes/patches, static linking vào worker, lưu artifact, và từng dạng
  phân phối; xin consent/license bằng văn bản nếu notice hiện tại không bao phủ.
- **CGAL:** probe dùng 2D Triangulations/TDS có license GPL theo package docs.
  Trước integration/phân phối cần quyết định mục tiêu phát hành và review license
  tương thích hoặc commercial terms; không mặc định kernel nền LGPL bao phủ CDT/TDS.
- **Dependency redistribution:** có cần đóng gói `.deb`/headers/libraries không?
  Nếu có, rà soát notice/attribution/source obligations riêng theo từng package;
  việc tải và extract để test local không tự cấp quyền phân phối lại.

## Điều kiện kỹ thuật để tiếp tục

- Prefix hiện có tại `build/deps/m4-prefix`; packages/checksums trong
  `locks/sources.json`, probe tại `native/m4_dependency_probe/`. Các bản `.deb`
  phải được xử lý theo license notices tương ứng; không suy quyền phân phối.
  FIST clearance/artifact là gate riêng. Build standalone DuDe target không ROS
  chỉ sau khi scope quyền tương ứng được xác nhận. Lỗi `ament_cmake` không phải
  lý do cài ROS cho target.
- Sau build: kiểm thử rectangle, L-shape, hai phòng nối cửa, map mở rộng/crop
  shift; đối chiếu polygon rings/holes, adjacency và status với source pinned.

M5 tracker/guard synthetic được triển khai riêng tại [M5 status](M5_STATUS.vi.md);
điều đó không thay thế DuDe segmentation, không mở license gate và không làm
M4 đạt. SceneGraphBuilder vẫn chưa được triển khai.
