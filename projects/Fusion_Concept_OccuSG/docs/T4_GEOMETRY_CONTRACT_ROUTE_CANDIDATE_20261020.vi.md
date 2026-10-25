# T4 geometry contract và route candidate — 2026-10-20

## Kết quả

Đã thêm geometry contract versioned và chạy một bounded, geometry-only search trên navmesh HM3D 00802. Search tìm được một candidate nằm trong mục tiêu thời lượng danh nghĩa. Candidate **qua PathFinder geometry preflight**, nhưng có một gần-U-turn khoảng 178.30° và điểm cuối cách điểm đầu 1.686 m theo XZ. Vì vậy route được bàn giao để người dùng xem hình, chưa gọi là route tự nhiên đã được chấp nhận và chưa dùng cho pilot.

Không chạy Simulator, sensor render, capture, browser, detector, encoder, mapper, native backend hoặc room scoring. Không rebuild navmesh, bật physics hay thay scene. T4 overlap tiếp tục pending; room scope/labels không đổi.

## Geometry contract v6

Config mới: `configs/habitat/geometry_contracts/hm3d_navmesh_clearance_contract_v1.json`.

- Navmesh serialized settings: `agent_radius=0.1000000015 m`, `agent_height=1.5 m`, `cell_size=0.05000000075 m`, `include_static_objects=false`.
- Runtime profile phải khớp bán kính/chiều cao đã bake với tolerance `1e-6 m`; không có metadata/settings thì gate đóng. Trên binary hiện có và runtime profile HM3D, cả bốn compatibility checks đều pass.
- `extra_navmesh_clearance_margin_m=0`. Baked radius là footprint basis; không cộng lại vào margin.
- Khác biệt với v5: ngưỡng cũ đã bị dùng như khoảng đệm cộng thêm nhưng chưa được mô tả đúng là `extra clearance`; v5/config và kết quả cũ được giữ nguyên, chỉ policy v6 áp contract tách bạch này.
- Numeric boundary tolerance là `0.0001 m`, fail-closed và tách khỏi physical margin. Với tọa độ scene có trị tuyệt đối dưới 20 m, float32 ULP dưới khoảng `0.000002 m`; band 0.1 mm lớn hơn khoảng 52 ULP. Navmesh cell 5 cm được ghi làm context, không dùng làm physical margin.
- `vertical_navmesh_tolerance_m=0.05 m` là nguồn duy nhất cho preflight và movement checks. Không buộc waypoint cùng Y; không dùng Y hay `floor_0` làm room scope evidence.
- Zero, non-finite/missing query và clearance nằm trong band `margin + epsilon` đều fail-closed. Không fallback sang baked radius để biến query zero/unavailable thành pass.

Habitat 0.3.3 tính `walkableRadius` từ `agentRadius/cellSize` rồi erosion walkable area theo radius đã bake; `distance_to_closest_obstacle` chuyển sang Detour `findDistanceToWall`. Vì vậy clearance query là khoảng cách tới wall/boundary của navmesh đã bake. Khi không có wall trong `max_search_radius`, Detour trả về chính search radius. Kết quả route ở đây đều chạm cap 0.0002 m, nên báo đúng là **không có wall được báo trong 0.2 mm**, không phải đo được clearance đầy đủ 0.2 mm. [Habitat-Sim PathFinder](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.cpp#L3194-L3199), [erosion](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.cpp#L3351-L3357), [clearance query](https://github.com/facebookresearch/habitat-sim/blob/v0.3.3/src/esp/nav/PathFinder.cpp#L1585-L1607), [Detour max-radius behavior](https://github.com/recastnavigation/recastnavigation/blob/main/Detour/Source/DetourNavMeshQuery.cpp#L3313-L3319).

## Code và tests

- [Route validation](../integration_v2/smooth_route.py): profile compatibility, single-source vertical tolerance, v2 navmesh validation; clearance margin không còn cộng baked radius.
- [Habitat runner](../scripts/run_habitat_smooth_route_preview.py): policy v6 dùng geometry contract trước route search; cùng tolerance 0.05 m cho preflight/runtime; clipped movement vẫn fail và không snap.
- [Startup validator](../integration_v2/camera_live_startup.py): kiểm tra schema v6, margin, epsilon, profile và không cho vertical tolerance mâu thuẫn.
- [Test file](../tests/test_smooth_route.py): baked radius không tính lần hai; positive extra margin; zero/invalid/boundary; profile mismatch; vertical tolerance; clipped step.
- [Startup tests](../tests/test_camera_live_startup.py): policy v6 hợp lệ và drift bị từ chối.

Đã chạy `python3 -m py_compile` cho route/search/materializer và các module runner/validator liên quan. Targeted tests: `python3 -m pytest -q tests/test_smooth_route.py tests/test_camera_live_startup.py` — **70 passed**.

Các config v5/T1.1 và artifacts preflight cũ được giữ nguyên; không diễn giải lại kết quả lịch sử.

## Search lock và thực thi

- Lock dùng cho search: [search_lock.json](../artifacts/route_geometry_search/hm3d00802_t4_geometry_route_search_20261020_v3/search_lock.json), seed `20261021`, tối đa 600 candidate chains, 3 legs × 2.2–4.2 m, tối đa 12 target draws mỗi leg, giới hạn 20 phút. Chỉ dùng PathFinder geometry; không có semantic inputs.
- Preflight lock pass: 11 input hashes, Habitat-Sim `0.3.3`, profile radius/height compatible, island index 2 được chọn vì diện tích navmesh lớn nhất (39.9433 m²) — đây chỉ là lựa chọn routing component, không phải floor assertion.
- Giữ hai lỗi bootstrap code trước sampling riêng tại `...v1/result/startup_error.json` và `...v2/result/startup_error.json`; cả hai xảy ra trước khi lấy candidate. Search chính chỉ chạy một lần sau preflight pass.
- Search chính: 131 chains, 0.674 s, dừng tại candidate đầu tiên trong 45–60 s. Rejection counts: leg length 548; anchor revisit 257; clearance/boundary 67; navigability/vertical 30; thiếu movement turns 18; không lấy đủ leg 12; clipped step 2; tổng turn không đủ 1. Các bộ đếm là kiểm tra mẫu/leg, không phải số chains.

## Candidate để xem

- [Top-down XZ, SVG](../artifacts/route_geometry_search/hm3d00802_t4_geometry_route_search_20261020_v3/result/route_topdown.svg) — Y được lưu trong JSON; hình không phải floor map.
- [Search result đầy đủ](../artifacts/route_geometry_search/hm3d00802_t4_geometry_route_search_20261020_v3/result/search_result.json).
- [Route config mới](../configs/habitat/hm3d_minival_v02_00802_t4_geometry_candidate_route_v1.json), [policy v6](../configs/habitat/smooth_route_t4_geometry_v6.json), [effective config review-only](../artifacts/route_geometry_search/hm3d00802_t4_geometry_route_search_20261020_v3/result/candidate_effective_640x480_v1.json).

| Thuộc tính | Kết quả |
|---|---:|
| Quãng đường PathFinder | 9.242 m |
| Anchor turns | 178.30°, 32.53°; tổng 210.83° |
| Start–end XZ | 1.686 m |
| Thời lượng emulator danh nghĩa | 53.0 s, dt 0.04 s |
| Action estimate | 1,325: 1,147 movement + 178 turn |
| Camera frames | 1,333 estimated; 1,569 conservative upper bound |
| Path samples | 234, cách nhau tối đa 0.04 m |
| `try_step_no_sliding` checks | 233, max error 0.00876 m ≤ 0.025 m gate |
| Clearance query | `max_search_radius=0.0002 m`; all samples above `margin+epsilon`, reported value capped at 0.0002 m |

Các anchor Habitat XYZ theo thứ tự: start `(0.24146, -0.00508, -6.35705)`, turn 1 `(-2.36585, 0.00395, -9.38574)`, turn 2 `(-0.85151, -0.00508, -7.72882)`, end `(-0.33597, -0.00508, -4.77280)` m.

Candidate đi qua ba đoạn có dịch chuyển thực, nên không quay tại chỗ để kéo dài route; tuy vậy góc đầu gần đảo chiều và điểm cuối quay về gần start. Không dùng scene render để xác định hành lang/đồ vật; cần người dùng xem top-down và quyết định route form có phù hợp không.

Config chỉ là proposal geometry-only: giữ 640×480, HFOV 90°, sensor extrinsics, dt 40 ms, nominal 0.2 m/s và 30°/s, keyframe thresholds, matcher/queue/model settings như profile hiện có. Guard đề xuất theo route: 1,800 camera frames/keyframe candidates, 75 s simulation elapsed và `--timeout-s 240`; không phải measured runtime. Startup config validator pass, chưa khởi tạo Simulator.

## Collision và giới hạn

Đã kiểm tra navigability/connected path, vertical tolerance, clearance query và `try_step_no_sliding` trên serialized navmesh. Navmesh metadata báo `include_static_objects=false`; nguồn hiện có nhận diện asset stage `.basis.glb` và navmesh `.basis.navmesh`, nhưng không chứng minh mọi static object instances đều nằm trong navmesh. Do đó kết quả chỉ nói route nằm trong geometry navmesh đã bake, không đảm bảo va chạm với mọi vật thể trong scene. Physics không chạy; agent/sensor transforms chỉ được đối chiếu với config/runner, chưa runtime-verified.

## Trạng thái

- Geometry contract v6: tests pass.
- Candidate: PathFinder geometry preflight pass, near-U-turn cần user review.
- Runtime movement/capture: chưa chạy.
- T4 graph/camera overlap: pending.
- Room spatial scope/membership/labels: unchanged, unavailable.
