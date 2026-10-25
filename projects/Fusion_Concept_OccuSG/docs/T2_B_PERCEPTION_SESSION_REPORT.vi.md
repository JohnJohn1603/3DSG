# T2-B — Perception session

Ngày: 2026-10-18  
Trạng thái: **partial pass — P2 session đã chạy smoke; P3/image/text session còn pending**.

## Phạm vi đã làm

- Thêm `integration_v2/perception_session.py` làm lifecycle chung:
  `open → update_frame → finalize → close`.
- Kiểm tra thứ tự `frame_index`, `frame_id`, timestamp và source hashes trước
  callback; duplicate/stale không gọi detector hoặc tiêu thụ RNG.
- Callback lỗi đặt session vào `failed/replay_required`; không retry cùng packet.
- Empty detections là kết quả hợp lệ.
- Cleanup idempotent, giữ primary failure và thử đóng resource đã sở hữu.
- `scripts/run_replica_cad_instance_segmentation.py` thêm
  `P2DetectorSession`; batch P2 loop dùng session này và giữ một
  `onnxruntime.InferenceSession` xuyên các frame.
- Packet contract hiện mang RGB-D, `K`/intrinsics, pose và source hashes của
  frame hiện tại; phần mở rộng contract này chỉ được compile/test model-free
  sau smoke vì giới hạn một smoke regression.

Không đổi YOLO model, preprocessing, confidence/NMS, vocabulary, matcher,
thresholds, A tables, mapping hoặc OccuSG.

## Smoke thực tế

Subset khóa trước tại:

`artifacts/t2_b_perception_smoke_20261018_v1/subset_manifest.json`

Gồm 3 frame đầu của HM3D 00802 ở 640×480, config/weights/capture hashes đã
khóa. P2 session đã chạy thật một lần:

- 3 frame xử lý, 8 detections;
- model load count = 1;
- không có frame error;
- mask bool hữu hạn, kích thước khớp RGB;
- không chạy P3, image encoder, text encoder, mapping, OccuSG hoặc Qwen.

Output và handoff:

- [detection_2d_index.json](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/t2_b_perception_smoke_20261018_v1/p2/detection_2d_index.json)
- [t2_b_handoff_report.json](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/t2_b_perception_smoke_20261018_v1/t2_b_handoff_report.json)

Timing fields đã được thêm vào runner sau smoke. Theo giới hạn một smoke, không
chạy lại model; vì vậy cold-start/per-frame timing của session này ghi
`not_captured`, không suy đoán từ wall-clock công cụ.

## Tests

```bash
python3 -m pytest -q \
  tests/test_perception_session.py \
  tests/test_conceptgraphs_session.py \
  tests/test_occusg_session.py \
  tests/test_m9_builder.py
```

Kết quả: **41 passed**, compile check pass.

## Phần chưa được bao phủ

- P3 geometry chưa có session API; chưa chạy smoke P3.
- OpenCLIP image/text feature scripts vẫn là batch sidecar; chưa giữ model
  sống qua keyframes trong T2-B.
- Chưa nối perception session với ConceptGraphs/OccuSG session, chưa chạy
  mapping hoặc room-belief.

Do đó T2-B chưa phải “toàn bộ perception session” và chưa mở T2-D.
