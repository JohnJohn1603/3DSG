# M2 status — OctoMap occupancy backend

Ngày: 25-09-2026

## Phạm vi đã triển khai

- Native `OccupancyMap` dùng trực tiếp `octomap::OcTree`.
- Scan integration dùng `OcTree::computeUpdate` và `KeySet`: free/occupied keys
  được gom theo toàn scan, mỗi key update một lần; OctoMap giữ occupied-over-free.
- Sensor model được cấu hình: resolution, hit/miss, clamping và occupancy threshold.
- Clipped branch parity với pinned `insertScan`: ray tới max-range và voxel chứa
  đúng clipped endpoint đều vào `free_cells`; sau đó occupied thắng free khi hai
  ray xung đột.
- `has_hit=false` có nghĩa **confirmed no-return tới max-range**, không phải
  depth invalid/NaN và không phải một khoảng trống ngắn hơn max-range.
- Python `OctoMapWorker` giữ một native process/OcTree qua nhiều update, trả
  revision, free/occupied key sets và log-odds/probability observations.
- Worker có strict request validation, stderr drain thread, một deadline cho
  toàn request (ghi + đọc đủ newline), child termination khi timeout/request
  failure và `snapshot()` đọc accumulated leaves.
- Response JSON được kiểm tra schema theo operation; response sai định dạng,
  partial line treo hoặc worker không đọc stdin chuyển worker sang error state,
  không cho sequence tiếp tục như thể state còn hợp lệ.
- `snapshot()` trả revision, bounds, resolution và mỗi leaf gồm key, center, size,
  occupied state, log-odds và probability; đây là interface chuẩn bị cho M3.
- Chưa triển khai projection M3, DuDe M4, checkpoint serialization hoặc
  SceneGraphBuilder.

## Files chính

- `native/occupancy/occupancy_map.hpp/.cpp`
- `native/occupancy/fixture_runner.cpp`
- `native/occupancy/reference_insert_scan.hpp/.cpp`
- `src/standalone_sg/mapping/native.py`
- `tests/fixtures/m2_octomap_basic.scan`
- `tests/fixtures/m2_octomap_basic.golden.json`
- `tests/test_m2_octomap.py`
- `tests/test_m2_worker.py`
- `tests/fixtures/m2_octomap_edgecases*.scan`
- `tests/fixtures/m2_octomap_resolution005.scan`

## Dependency/build

System install bị chặn bởi quyền apt lock. Đã tải và giải nén local
`liboctomap-dev 1.9.7+dfsg-3.1build3` vào
`build/deps/octomap-prefix`, sau đó build với:

```bash
cmake -S native -B build/native-m2 \
  -DSTANDALONE_SG_ENABLE_M2=ON \
  -DOCTOMAP_ROOT="$PWD/build/deps/octomap-prefix/usr"
cmake --build build/native-m2 --parallel 2
```

Build thành công với GCC 13.3.0/CMake 3.28.3. Warning duy nhất đến từ
OctoMap 1.9.7 dùng `std::iterator` deprecated dưới libstdc++ mới; không phải
lỗi M2.

## Golden/regression result

- M2 regression tests: **13 passed**; full suite M0–M2: **40 passed**.
- Golden fixture kiểm tra hai update liên tiếp, duplicate endpoint, hit vượt
  max range, no-return free-only ray, revision tăng và log-odds state persistence.
- Reference harness độc lập trích logic `computeRayKeys` + `coordToKeyChecked`
  của pinned `insertScan`; parity test bao phủ hit trong range, exact/beyond
  range, occupied-over-free conflict, duplicate/reordered points, origin lệch,
  tọa độ âm và ray chéo.
- Golden provenance ghi source commit, OctoMap version và reference harness;
  expected key sets không được sinh chỉ từ `OccupancyMap` implementation.
- Worker tests bao phủ timeout khi child treo sau partial response, timeout khi
  child không đọc request lớn, invalid-response error state, close/request
  errors, stderr-safe handling, accumulated leaf snapshot và hit/miss clamping
  tới `0.97`/`0.12`.
- Baseline OccuSG commit vẫn khớp và baseline worktree không bị sửa.

## Gate

M2 đạt native build + golden/reference fixtures trong local OctoMap prefix. M2 chưa phải
cam kết portability trên mọi Kaggle image: môi trường phải cung cấp tương thích
OctoMap headers/library hoặc local asset có checksum. M3 đã bắt đầu và được
nghiệm thu riêng trong [M3 status](M3_STATUS.vi.md); M4 vẫn chưa bắt đầu;
CGAL và DuDe/FIST không chặn việc nghiệm thu M2 này.
