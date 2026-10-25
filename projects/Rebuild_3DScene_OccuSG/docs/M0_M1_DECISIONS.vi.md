# M0–M1 decisions — standalone OccuSG

Ngày: 25-09-2026

## Baseline và phạm vi

- Baseline implementation là `ref_OccuSG/OccuSG` tại commit
  `2bca2fa06af87fd9dd0542957039be8f580f0dca`.
- M0–M1 chỉ tạo contracts, config validation, coordinate helpers và axial-depth
  back-projection. Chưa tạo SceneGraphBuilder, occupancy backend, projection,
  region segmentation, object association hay DuDe worker.
- DuDe không bị thay thế. Các source/commit DuDe và submodule được ghi trong
  `locks/sources.json` cho milestone sau.

## Contract decisions

- World là right-handed, Z-up, mét; camera optical dùng x phải ảnh, y xuống ảnh,
  z hướng trước camera.
- `T_parent_child` dùng column vector và left multiplication. `T_W_Copt` phải có
  child frame `camera_optical`; camera translation là sensor origin trong world.
- Grid helper trả `(row, col)`; row tăng theo +Y, column tăng theo +X; chỉ số
  dùng `floor`, kể cả tọa độ âm.
- `depth_m` là axial depth. Raw depth bắt buộc truyền `meters_per_unit`; không
  suy đoán millimeter từ dtype. Baseline mapping là `16UC1=0.001`,
  `32FC1=1.0`.
- Sampling giữ thứ tự baseline: `depth[::stride, ::stride]` và flatten C-order.
- Invalid/NaN/Inf, giá trị dưới min depth hoặc trên max depth bị loại khỏi cloud;
  no-return không tự biến thành hit.
- Contract arrays được copy và đánh dấu read-only sau boundary. Config YAML reject
  unknown keys và có canonical SHA-256 hash.

## Native feasibility decision

M0 có một C++17/CMake probe không liên quan DuDe/OctoMap để xác minh compiler và
build path. Việc probe pass không phải bằng chứng native OccuSG backend đã build.
M2/M4 vẫn phải chạy spike riêng sau khi có header/library/toolchain hợp lệ và
license clearance.

