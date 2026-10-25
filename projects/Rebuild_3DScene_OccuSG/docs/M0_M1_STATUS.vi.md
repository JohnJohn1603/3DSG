# M0–M1 status

Ngày: 25-09-2026

## Đã tạo

- `pyproject.toml`: package `standalone-sg`, dependency tối thiểu NumPy/PyYAML.
- `locks/sources.json`: baseline OccuSG và các pinned submodule revisions.
- `configs/reference_code.yaml`: profile `reference/code` theo node default,
  `stride=1`.
- `configs/reference_code_mp3d.yaml`: profile MP3D audit riêng, `stride=2`.
- `configs/test_fixture.yaml`: profile fixture riêng.
- `src/standalone_sg/contracts/`: `CameraIntrinsics`, `Transform`, `RGBDFrame`,
  `PointCloud`, `ContractError`.
- `src/standalone_sg/geometry/`: transform/inverse, grid index, axial depth
  decode, radial-to-axial adapter và back-projection.
- `scripts/doctor.py`: read-only baseline/toolchain/dependency report.
- `native/`: C++17/CMake minimum build probe; không chứa DuDe thay thế.
- `tests/`: config strictness, transform, grid, frame contract và back-projection
  fixtures.
- `docs/M0_M1_DECISIONS.vi.md`: các quyết định contract/config của milestone.

## Kết quả kiểm tra

- M0–M1 regression subset: **27 passed**; full suite including M2/M3: **54 passed**.
- Baseline commit check: **pass**, đúng
  `2bca2fa06af87fd9dd0542957039be8f580f0dca`.
- `cmake -S native -B build/native-probe` + build + executable probe: **pass**.
- `python3 -m pip wheel --no-deps --no-build-isolation .`: **pass**.
- Toolchain quan sát được: GCC 13.3.0, CMake 3.28.3, Ninja 1.11.1.
- System headers: OpenCV4 và Eigen3 có; OctoMap và CGAL headers không có.
  OctoMap development headers đã được cung cấp qua local prefix cho M2.

## Baseline test note

Lệnh chạy test `ref_OccuSG/OccuSG/src/scene_graph_core/tests` bằng Python hệ
thống không collection được vì môi trường này thiếu `networkx` (9 module lỗi
ngay khi import). Không cài dependency ngoài phạm vi M0–M1 và không sửa baseline.
Đây là environment blocker riêng; không làm fail 27 test độc lập của package
M0–M1.

## Gate trước M2/M4

- M1 đạt cho contract/coordinate/back-projection fixtures.
- M2 được nghiệm thu riêng trong [M2 status](M2_STATUS.vi.md); native dependency
  dùng local prefix, không system-install.
- M4 chưa được nghiệm thu: cần xác minh build/link DuDe dependencies và quyền sử
  dụng FIST/DuDe; không được public-vendor chỉ vì source đang nằm trong baseline.

## Regression fixes sau review

- Config không còn ép `int()`/`bool()`; reject fractional integer, string boolean,
  sai kiểu và NaN/Inf.
- Distortion khác rỗng bị reject vì M1 chỉ nhận ảnh đã rectified.
- Rigid transform dùng `rtol=0` cho row/orthonormal/determinant checks.
- Back-projection nhận và áp dụng `RGBDFrame.valid_mask`.
- Có regression cho pitch/roll, Habitat/Webots chain, sensor extrinsics, uint16,
  depth invalid và golden back-projection semantics.
