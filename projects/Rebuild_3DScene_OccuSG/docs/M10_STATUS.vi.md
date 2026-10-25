# M10 — JSON, trajectory và visualization

## Trạng thái

M10 local standalone đã có serializer/export và renderer headless. Schema JSON hiện ở version 1; reader có migration tường minh cho envelope snapshot v0. Đây là snapshot để đọc/query và replay graph deltas, không phải checkpoint để khôi phục OctoMap/DuDe hay stage state.

## Contract và đầu ra

- `src/standalone_sg/serialization/json_graph.py`: snapshot/delta schema-versioned, canonical sorting, finite JSON only cả sau parse (overflow `1e999` bị reject), validate kiểu/schema, reject key không phải string/sets/NaN/Inf, dangling/self edges, duplicate IDs/edges và room ownership/anchor violations, v0 snapshot migration, atomic same-directory temp + `fsync` + `os.replace`, snapshot/delta-log và trajectory CSV/JSON writers, replay liên tục theo revision, public `query_nodes`.
- Snapshot giữ node/edge IDs, outer/inner geometry rings, node provenance, first/last observation times, processing/snapshot time, stale flags và config hash. Large integer ID/metadata không bị float-cast.
- Trajectory lấy đúng GraphNode `agent` có `pose_W` do builder commit; frame không có pose không được nội suy/bịa thêm. Processing/flush timestamp không được dùng làm pose evidence.
- `src/standalone_sg/visualization/plots.py` dùng Matplotlib Agg, không cần display server. Snapshot được xuất thành hai view độc lập: `spatial_map.png` giữ tọa độ world, room footprints/stale state và lịch sử agent; `relationship_graph.png` dùng layout phân cấp chỉ cho trình bày, không ghi ngược vào graph. `scene_graph.png` vẫn được giữ tương thích và dùng relationship view sạch.
- View mặc định ẩn navigation mesh và temporal agent edges, không ghi nhãn object/agent/navigation, chỉ ghi nhãn room. `render_debug_graph` giữ bộ lọc tường minh cho navigation, temporal edges, agent/object nodes và tập label; không thay đổi snapshot/edge semantics.
- `scripts/export_m10_demo.py` tạo fixture doorway deterministic với dữ liệu synthetic/cached detections/regions, một frame stale và flush; replay bốn deltas khớp chính xác snapshot cuối revision 4. Manifest đánh dấu `simulator_end_to_end=false`, `native_backend_checkpoint=false`.

## Bằng chứng chạy được

- Command demo: `python3 scripts/export_m10_demo.py --output artifacts/m10_demo`.
- Artifacts: `snapshot.json`, `delta_log.json`, `trajectory.csv`, `trajectory.json`, `spatial_map.png`, `relationship_graph.png`, `trajectory_rooms.png`, `scene_graph.png`, `manifest.json`; graph có 22 nodes/107 edges và trajectory có 3 accepted agent poses.
- Focused M10 reader/export: **15 passed**. Full suite after M11 RGB-D integration: `PYTHONPATH=src python3 -m pytest -q` → **195 passed, 0 skipped** (32.69 s).
- M10 tests cover schema/geometry/provenance/staleness roundtrip, large integers, empty snapshot, v0 migration, NaN/Infinity/`1e999`/future-version rejection, malformed graph invariants, atomic snapshot write, real-pose-only trajectory và exported demo delta replay/query.
- Cả hai PNG đã được render headless và kiểm tra trực quan. `visualization` là optional extra `matplotlib>=3.6`; không thêm dependency bắt buộc vào core.
- Có thể xuất lại từ snapshot bằng `scripts/export_scene_views.py`; raw `snapshot.json` là nguồn để một interactive viewer sau này hiển thị node details, nhưng lượt này chưa thêm viewer tương tác.

## Giới hạn

Fixture M10 deterministic nhưng synthetic/cached, không Habitat/Webots/robot end-to-end, không đo chất lượng segmentation, không chứng minh native backend có thể resume từ JSON. Native point-cloud chain chuẩn bị M11 nằm ở `scripts/run_native_pipeline_smoke.py` và có status riêng; không gộp nó vào demo M10. Room holes được giữ nếu graph contract cung cấp nhưng M4 pinned worker hiện chỉ xác nhận outer rings. Demo không tuyên bố M4 full acceptance/cloud/license clearance; M11/Kaggle và M12/Webots chưa làm.
