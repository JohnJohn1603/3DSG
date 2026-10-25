# RB2 static room-belief diagnostic — 00802 / min=3

## Phạm vi

- Run chính: `hm3d00802_main29_min3_v1`
- Scene/sequence: `00802-wcojb4TFT35` / `hm3d-v02-00802-main-route-01`
- Cache: 29 frames, 67 observations, 33 hypotheses trước filter.
- Filter giữ nguyên: `obj_min_detections=3`, `obj_min_points=0`.
- Seed: `20261014`.
- Không chạy Qwen, capture, detector, encoder hoặc mapping lại.
- Đây là static development diagnostic; functional room GT chưa có nên accuracy/F1 unavailable.

## Provenance khóa

- Hybrid manifest SHA-256: `1ba963cdcd96405b8a08e7a25bd29af266481fc73c30e6c8d26d1ae248571894`
- Mapping run manifest SHA-256: `082b90eadbf80ff3bc6fc76cbea4fbba0a9acdfc1de2f9ae6bd211a39a81d4d6`
- Effective mapping config SHA-256: `a58d8df78f5f888722fcdb26d33ed493d1320a48d2403c65e82f0de7fed5bf15`
- Room snapshot SHA-256: `892d5bc36e4cad54b1c2979b860a484330bb19f92f855eaeb245fbdfb5a0d150`
- A archive SHA-256: `0e30cc12058ec22372fe8b2df49279ec6a917027bef74182f8831374a7418997`

Membership dùng greedy voxel support trong scope `floor_0` đã có trong object-layer metadata. Snapshot không có floor IDs/Z bounds; không suy rộng thành multi-floor.

## Readiness

- Retained: **6**; filtered: **27**.
- Semantic: **5 stable**, **1 conflicted**, filtered được loại khỏi semantic evidence.
- Membership: **30 assigned**, **3 unassigned**.
- `car` là consensus category stable nhưng ngoài 11-row A vocabulary, nên unsupported.
- `tv` stable nhưng unassigned, không đóng góp cho room belief.
- Alias `couch → sofa` được canonicalize trước lookup.

## Kết quả theo room

Chỉ `room:6` có evidence eligible: ba object stable từ hai unique categories `chair` và `sofa`. `room:2`, `room:3`, `room:5`, `room:7` đều `unknown / no_evidence`.

| Variant | Room:6 candidate | Commit | Status/reason |
|---|---|---|---|
| mean_no_neutral | living_room | living_room | proposal thresholds satisfied, uncalibrated |
| mean_neutral | kitchen | — | belief/margin gate không đạt |
| sum_no_neutral | living_room | living_room | proposal thresholds satisfied, uncalibrated |
| sum_neutral | living_room | — | belief/margin gate không đạt |

Các commit ở trên chỉ là output của development proposal chưa calibrated, không phải room-label quality claim. Không có variant nào được chọn làm default.

## Artifacts

- [Readiness JSON](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/room_belief/readiness/hm3d00802_main29_min3_static_v1/readiness.json)
- [Static diagnostic v2](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/room_belief/rb2_00802_min3_static_v2/static_room_diagnostic.json)
- [Static report manifest](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/room_belief/rb2_00802_min3_static_v2/report_v1/static_report_manifest.json)
- [Static HTML report](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/room_belief/rb2_00802_min3_static_v2/report_v1/static_room_belief_report.html)
- [Hybrid geometry/evidence viewer](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/conceptgraphs_hybrid_runs/hm3d00802_main29_min3_v1/hybrid_viewer.html)

## Tests

```text
31 passed in 1.24s
```

Đã kiểm tra công thức RB1 incremental/recompute parity cho cả bốn variant và cả năm room nodes.

## Bước tiếp theo duy nhất

Nếu cần thêm bằng chứng, review thủ công riêng các object stable ở `room:6` và các object stable bị loại vì `unsupported/unassigned`; không tuning, không đổi A/threshold và không capture thêm.
