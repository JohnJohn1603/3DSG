# P5–P7 — room semantics và holdout

Ngày: 2026-10-01  
Trạng thái: **P5/P6 rule-based technical checkpoint; P7 evaluation limited**

## P5

Đã thêm `RuleBasedRoomLabeler` tại
`src/standalone_sg/semantics/room_labeler.py` và config
`configs/semantics/room_rules_replica_cad_dev.json`.

Input chỉ là persistent ObjectNodes thực sự có `room_contains`, canonical
class và last evidence. Mỗi object ID chỉ đếm một lần; score là
`uncalibrated_additive_evidence`, không phải xác suất. Kết quả có thể là
`predicted`, `unknown`, `ambiguous` hoặc `stale`; semantic label không thay
room ID/key.

Trên G1 re-observation: 3 RoomNodes → 1 `predicted` (`living_room`) và 2
`unknown`. Đây là kết quả explainable từ object evidence, không phải claim
phòng thật. Không có room GT nên accuracy là `null`.

## P6

`scripts/enrich_room_semantics.py` tạo semantic-only export sau snapshot hình
học đã commit:

- `snapshot.json` semantic có các attributes `semantic_*`;
- `room_semantics.jsonl` có evidence hash, candidate scores, supporting IDs,
  source graph revision, status và stale reason;
- `delta_log.json` semantic-only replay được từ geometry snapshot;
- `spatial_semantic_map.png`, `room_object_graph.png`.

Artifact G1/P5/P6:
`artifacts/runs/habitat_replica_cad_apt0_perception_reobserve_20261001/semantic/`.

Semantic enrichment là post-stage rõ ràng, không mutate geometry snapshot gốc;
geometry-only vẫn chạy khi không có model/cache. Chưa có LLM provider, không
gọi API ngoài và không thêm semantic label vào room ID.

## P7 holdout

Đã chạy cùng model/weights/preprocess/geometry/profile trên `apt_1` và `apt_2`
với capture 12 frame hiện có, không tuning từng scene. Technical checks pass:

- `apt_1`: 1 Detection3D accepted, 5 RoomNodes; rule output 5 `unknown`;
- `apt_2`: 2 Detection3D accepted, 5 RoomNodes; rule output 5 `unknown`;
- cả hai replay có fresh/reload/delta pass và semantic-only delta replay pass.

Các kết quả holdout phản ánh ngân sách quan sát/độ phân giải thấp và detector
coverage hạn chế; không dùng số RoomNodes hoặc tỷ lệ unknown làm accuracy.
Không có annotation 2D/3D object độc lập và room polygons đủ tin cậy, nên
metrics precision/recall/IoU/room-label accuracy đều `null` với lý do đã ghi
trong manifest.

## Regression và giới hạn

Regression mới kiểm tra empty-vs-present cache, duplicate frame cache, cluster
determinism và rule labeler unknown/ambiguous/stale. P7 chưa phải benchmark
generalization: chỉ 3 scene cùng ReplicaCAD family, route ngắn, GT pose, không
SLAM, không detector training, không room label GT.

Xác minh toàn repo sau lượt này: targeted perception/source-parity **18 passed**;
full `PYTHONPATH=.:src pytest -q`: **251 passed trong 85,97 s**. Các script mới
đã được `py_compile`; không có test bị skip trong full run.

Profile đề nghị chỉ dùng cho development/integration replay hiện tại. Không
đổi profile reference mapping/segmentation và không dùng kết quả semantic để
điều khiển mapping.
