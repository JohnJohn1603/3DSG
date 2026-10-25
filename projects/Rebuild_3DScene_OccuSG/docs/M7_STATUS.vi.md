# M7 — Navigation block graph

## Trạng thái

M7 đã triển khai như module ROS-free trong `src/standalone_sg/navigation/`. Input là `FreeSpaceGrid2D` structural clearance có revision/world frame; output là node block, directed reciprocal structural edges và thông tin prune/rescan. Đây chưa phải navigation planner và không chứng minh robot đi qua được.

## Source/config được tái sử dụng

Baseline OccuSG `2bca2fa06af87fd9dd0542957039be8f580f0dca`, `FreeSpaceNodeManager` trong `src/scene_graph_ros/scene_graph_ros/managers/free_space_manager.py`. Hai runtime YAML benchmark MP3D và TurtleBot3 đặt `fs_cell_stride_cells=15`, `fs_min_free_cell_count=25`, connectivity `8`; generic constructor defaults của upstream là 10/50/8. Standalone strict schema giữ đúng hai profile đó; `reference_code.yaml` dùng 10/50/8, còn `reference_code_mp3d.yaml` dùng 15/25/8. Resolution full-rescan dùng source comparison `abs(previous-current) > 0.0001 m`, không phải một tolerance tự chọn.

- Block key: `floor(cell_center_world / (stride * resolution))`, do đó world-aligned và âm đúng theo floor. Public ID là `nav:{block_x}:{block_y}`.
- Chỉ giá trị grid `0` đếm là free; node mới tạo khi count `>=25`.
- Representative cell là free cell gần tâm hình học block nhất; tie-break `(row, col)`. Connectivity BFS chỉ trên hợp free cells của hai block, theo 8-neighbour; node lân cận có hai directed edge đối xứng nếu có đường cell-level.
- Cùng origin/resolution: nodes được giữ lại khi count xuống dưới ngưỡng và cập nhật `meets_minimum_free_cells=false`. Origin/resolution đổi: source full-rescan/prune; nodes vắng hoặc dưới ngưỡng bị xóa, surviving world block keys giữ ID.
- Edge provenance/scope là structural free-cell bridge; không phải swept-volume/collision/robot-footprint guarantee.

## Kiểm thử

Focused M7: **10 passed** (includes native M2→M3→M7 smoke). Focused M6 + config: **20 passed**. Latest full regression after RGB-D smoke: **195 passed, 0 skipped** (`PYTHONPATH=src python3 -m pytest -q`).


`tests/test_m7_navigation.py` kiểm tra ngưỡng 24/25/26, block âm, map expansion, cropped origin/origin shift, diagonal bridge, blocked bridge, retained/pruned node, input contract và integration từ `project_leaves` M3 debug API sang M7 và smoke thật native `OctoMapWorker.integrate → project(cpp_mapconverter) → M7` ở resolution 0.05 m. Smoke kiểm tra frame, origin, resolution, revision và graph invariants; đây không phải full source parity. Integration grid được sinh từ synthetic leaves và backend ghi rõ `python_port_debug`; đây không phải bằng chứng parity với native C++ M3 hoặc toàn pipeline.

## Phạm vi nghiệm thu và còn mở

M7 đạt các unit/scenario hiện có của standalone block graph. Các trường navigation đã được đưa vào strict YAML/schema với tolerance provenance trong `locks/sources.json`. Không triển khai nearest-object links, room/region anchoring, planner, footprint collision, M8/M9 (M8/M9 status reports cover subsequent integration). Chưa kiểm chứng trên replay dữ liệu thực hoặc workload dài. M4 giữ nguyên technical-local/reference-parity scope; cloud validation và quyền sử dụng vẫn mở. Recovery fixture M4 quay lại grid `partial` ban đầu; chưa chứng minh phục hồi khi khám phá tiếp tục mở rộng liên tục.
