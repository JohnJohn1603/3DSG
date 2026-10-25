# Graph quality audit — ReplicaCAD apt_0/apt_1/apt_2

Ngày: 2026-10-01  
Phạm vi: audit trên capture/cache đã khóa; không đổi model, mapping,
segmentation hoặc M6 association.

## Tái lập replay

Fresh-process replay riêng cho cả ba run đều đạt:

- `fresh_replay_equal=true`;
- `snapshot_reload_equal=true`;
- `delta_replay_equal=true`.

Các manifest và cache vẫn ghi rõ scene/config/binary hash. Đây là tính tái lập
của pipeline trên capture đã ghi, không phải accuracy của detector hay room
segmentation.

## Object audit apt_0

Audit nằm ở:
`artifacts/runs/habitat_replica_cad_apt0_perception_dev_long_20261001/graph_quality_audit/`.

`object_audit.json/csv` phân biệt:

- `retained_frames`: snapshot còn giữ ObjectNode;
- `observation_frames`: frame có detection ID mới được association;
- các detection ID, mask/overlay P2 và ledger P3 tương ứng;
- room membership theo thời gian.

Các tái quan sát rõ trong route dài:

- `potted_plant` object-000003: frame 6, 13, 17;
- `vase` object-000004: frame 6, 9, 17;
- `sofa` object-000005: frame 7, 8;
- `potted_plant` object-000006: frame 7, 14;
- `tv` object-000007: frame 7, 14, 18;
- `dining_table` object-000002: frame 2, 5, 9.

Không có cặp cùng class nào trong apt_0 có khoảng cách tâm dưới 0,75 m,
nên audit không thấy tín hiệu heuristic rõ của duplicate/false merge giữa các
ObjectNode gần nhau. Đây không phải chứng minh không có false merge: không có
object GT hoặc track GT độc lập.

Hai cảnh báo membership cần kiểm tra bằng overlay/room annotation:

- `object-000002` (`dining_table`) ở `room:3` tại frame 2, chuyển tạm sang
  `room:2` ở frame 3–4, rồi trở lại `room:3` từ frame 5;
- `object-000012` (`dining_table`) ở `room:1` từ frame 19–22, chuyển sang
  `room:3` ở frame 23.

Đây có thể là region split/relink tạm thời hoặc membership sai; audit không
đủ thông tin để chọn một nguyên nhân.

## Semantic output

Semantic enrichment đã chạy trên cả ba graph:

| run | nhãn dự đoán | ambiguous | unknown |
|---|---:|---:|---:|
| apt_0 dài | 1 (`living_room`) | 0 | 4 |
| apt_1 holdout | 1 (`dining_room`) | 1 | 2 |
| apt_2 holdout | 1 (`dining_room`) | 0 | 3 |

`room_semantics.jsonl` ghi riêng `observed_object_ids`,
`supporting_object_ids`, candidate scores, evidence hash và provenance. Các
nhãn trên là rule-based evidence chưa hiệu chuẩn; không phải room GT.

## Hình tổng hợp và RGB

Mỗi run có:

- `graph_quality_audit/occupancy_rooms_objects_trajectory.png`: M3 final grid
  (unknown/free/occupied), room footprints, object markers và trajectory trong
  world coordinates;
- `graph_quality_audit/room_rgb_contact_sheet.png`: RGB frame đại diện cho
  từng room có frame quan sát; nhãn room là nhãn semantic hiện tại hoặc
  `unknown`;
- `object_audit.csv`: bảng để mở nhanh trong spreadsheet;
- `object_audit.json`: evidence đầy đủ kèm đường dẫn mask/overlay.

Các hình dùng grid M3 cuối đã được ghi trong replay; audit không project lại
và không đưa room/object GT vào builder.

## Kết luận theo tầng

1. **Detector/P3:** là bottleneck định lượng rõ nhất. Tỷ lệ Detection3D được
   chấp nhận ở ba run khoảng 54,5–60,5%, nhưng đây không phải recall. Ledger
   cho thấy nhiều rejection do cluster nhỏ; cần annotation object gồm cả vật
   bị bỏ sót trước khi gọi là lỗi detector.
2. **Lifting 3D:** cache giữ mask, depth, cluster và center provenance; chưa có
   marker/GT độc lập trên các object ReplicaCAD để đo sai số vị trí.
3. **Association:** route apt_0 có nhiều re-observation giữ cùng ID; heuristic
   close same-class pair không phát hiện cặp đáng ngờ. Vẫn cần GT/annotation
   để phân biệt merge/split thật.
4. **Membership/room segmentation:** membership đổi ở các object nêu trên là
   cảnh báo mạnh nhất của audit. Cần xem overlay theo frame 2–5 và 19–23,
   sau đó đối chiếu room boundary độc lập; chưa sửa M8 hoặc DuDe.
5. **Labeler:** evidence giải thích được; `unknown/ambiguous` được giữ đúng,
   không ép nhãn. Chưa có room GT nên chưa báo accuracy.

### Phạm vi nghiệm thu

Đã đạt quality-audit kỹ thuật và reproducibility trên ba capture. Chưa đạt
nghiệm thu chất lượng graph/room segmentation. Bước tiếp theo hợp lý là tạo
annotation độc lập nhỏ cho các room/object và kiểm tra cụ thể hai chuỗi
membership nêu trên, thay vì tăng frame hoặc đổi model.
