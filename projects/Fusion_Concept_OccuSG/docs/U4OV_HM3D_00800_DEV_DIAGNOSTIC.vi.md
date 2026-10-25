# U4-OV HM3D 00800 — chẩn đoán P3 và pilot development

**Phân loại dữ liệu:** HM3D 00800 là development từ thời điểm này. Không promote; không tuning threshold, model, vocabulary, prompts, P3/U3/U4 config. Baseline, graph và room scores giữ nguyên.

**Cập nhật sau resolution ablation:** phần dưới giữ lại kết quả lịch sử của các pilot 160×120. Giá trị `fy=93.24 px` ở bản trước là lỗi tài liệu; đã sửa thành `fy=80 px`. Adapter và P3 đã dùng `fx=fy=80 px`, không có lỗi dữ liệu/code. Kết quả resolution ablation 640×480 và pilot đa-view v2 nằm trong [báo cáo mới](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/docs/U4OV_HM3D_00800_RESOLUTION_MULTIVIEW_REPORT.vi.md) và [audit intrinsics](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/intrinsics_audit.json); các kết luận trạng thái ở phần 4 dưới đây đã được cập nhật ở báo cáo mới.

## 1. Chẩn đoán 14 P3 rejection ở route v1

### Cấu hình và alignment

- RGB `120×160×4`, axial depth `120×160`, valid mask `120×160`; cả 15 frame đều có cùng pixel grid. 16 mask P2 cũng đúng `120×160`. Valid mask đã lưu khớp với kiểm tra finite depth trong khoảng 0.1–10.0 m. Metadata khai báo co-located Habitat color/depth sensor và cùng sensor pose.
- Intrinsics được adapter tính từ HFOV 90°: `fx=80 px`, `fy=80 px`, `cx=79.5`, `cy=59.5`. Giá trị `fy=93.24 px` trong bản báo cáo trước là lỗi tài liệu; audit adapter và phép so sánh full-scene render xác nhận phép backprojection dùng đúng intrinsics.
- Effective P3: stride 1, `eps=0.02 m`, `min_points=100`, largest Euclidean cluster. Detector profile SHA-256 `ec64d9e8c508d54b313abf8495b15631fa95e047e894131db5c5fec7c2dfd0b3`; P3 script SHA-256 `61614c8f2d1083f0aad9998747e166cb90542b1c85c5b2b27e4319f259e2cdc0`.
- Cả run 00800 và run 00802 dùng cùng P3 source, profile, ngưỡng, Open3D-free fallback và cùng interpreter path từ `.venv-perception`/`.venv-appearance` tương ứng. SciPy không có trong các interpreter P3 đã dùng, nên cả hai chạy fallback brute-force neighborhood trong `cluster_components`; **không phát hiện đổi backend giữa hai run**. 00802 dùng cùng fallback nhưng có nhiều pixel hơn để tạo cụm.

### Kết quả từng rejection

14 rejection gồm 7 mask có dưới 100 depth points nên P3 bỏ qua clustering; 7 mask còn lại có ít nhất 100 points nhưng largest cluster chỉ 1 hoặc 3 points. Trong nhóm sau, mask diện tích lớn nhất 1665 px vẫn chỉ có cluster 1. 12/14 mask có valid-depth fraction 1.0; hai ngoại lệ là 10/135 (0.074) và 1327/1665 (0.797). Vì vậy depth invalid không giải thích phần lớn rejection.

Khoảng cách camera tới các điểm mask hợp lệ, tính từ cloud diagnostic, có median từ 2.06 đến 6.27 m. Với `fx=80`, một pixel tương ứng khoảng 2.5 cm ở 2 m và 3.75 cm ở 3 m, lớn hơn `eps=2 cm` trong nhiều vùng gần phẳng. Ở 00802 (`600×340`, HFOV 90°), `fx=300`; tương ứng 0.67 cm ở 2 m và 1 cm ở 3 m. Đây là khác biệt lớn về point density khi ngưỡng clustering giữ nguyên.

| Run | Capture | P2 detections | P3 accepted | P3 rejected |
|---|---:|---:|---:|---:|
| HM3D 00800 v1 | 160×120, 15 frame | 16 | 2 (12.5%) | 14 (87.5%) |
| HM3D 00802 existing | 600×340, 280 frame | 738 | 713 (96.6%) | 25 (3.4%) |

Đây không phải so sánh scene-controlled; scene, route và khoảng cách vật khác nhau. Tuy nhiên config, script và clustering backend giống nhau, còn độ phân giải và phân bố khoảng cách khác rõ rệt. Route v1 cũng lấy mẫu navmesh rải đều thay vì quay lại vật, nên thiếu temporal coverage là nguyên nhân thêm vào, không phải lời giải duy nhất cho P3 rejection.

Chi tiết từng ID gồm mask area, valid-depth count/fraction, selected points, largest cluster, depth/range và pose: [CSV](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/audits/u4ov_new_scene_hm3d_00800/p3_rejection_diagnostic/rejections.csv), [JSON](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/audits/u4ov_new_scene_hm3d_00800/p3_rejection_diagnostic/diagnostic.json), [RGB/mask/depth gallery](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/audits/u4ov_new_scene_hm3d_00800/p3_rejection_diagnostic/index.html). Visualization depth được normalize riêng từng mask để xem, không dùng trong P3.

## 2. U4-OV ở ba phạm vi

| Scope | Kết quả |
|---|---|
| 16 crop 2D ở route v1 | 1 classified (`coffee table`), 15 conflicted, 0 unknown. Đây là prediction, chưa chấm đúng/sai. |
| Hai observation vượt P3 | `hm3d-00800-u4ov-dense-0008:det-0002` (`dining table`) và `hm3d-00800-u4ov-dense-0014:det-0001` (`umbrella`); cả hai U4-OV conflicted. |
| Coffee-table classified crop | `hm3d-00800-u4ov-dense-0011:det-0000`, raw detector `truck`, U4-OV `coffee table`; **không vượt P3**: 445 valid points, largest cluster 1, `rejected_cluster_below_minimum`. |
| Track-level multiview | Route v1 có 2 singleton tracks, mỗi track 1 P3 observation; không có track với ≥2 distinct view bins. Multi-view fused label unavailable. Không có temporal label correctness/coverage để chấm. |

Cosine values là similarity chứ không phải xác suất. Không có GT, human identity/class review hay category mapping đã xác nhận đưa vào predictor/evaluator của phần này.

## 3. Development pilots

Các route được validate trên navmesh; mọi capture vẫn 160×120/90°/sensor 1.5 m, P2/P3 thresholds và code giữ nguyên. Mỗi pilot 5 frame; chưa chạy association replay modes vì điều kiện multiview chưa đạt.

| Pilot | Mục tiêu route | P2 | P3 accepted | Kết quả |
|---|---|---:|---:|---|
| v2 | Ba vị trí gần center hình học của bàn, neighbor glance, revisit | 6 | 0 | Không có mask bàn được nhận; các detections là person/refrigerator/truck, cluster không đạt 100. |
| v3 | Tái tạo pose bàn đã pass, hai offset lớn hơn, neighbor glance, revisit | 7 | 2 | Chỉ A1/A2 ở đúng cùng tọa độ/yaw; RGB SHA giống nhau. B/C không tạo observation bàn P3. |
| v4 | Cùng pose bàn, hai offset nhỏ hơn, neighbor glance, revisit | 8 | 2 | Vẫn chỉ A1/A2 cùng RGB/mask hash; B có mask `oven` 1062 px nhưng largest cluster 66; C không pass. |
| v5 | Dịch gần quanh cluster oven 66 điểm, neighbor glance, revisit | 1 | 0 | Không thu được P3 observation. |
| v6 | Giữ hướng detector của oven; dịch gần 0.3/0.6 m, nhìn neighbor, quay lại A1 | 5 | 0 | P2 có 5 detections, nhưng không có detection nào qua P3; A1 và revisit cuối cho cùng refrigerator detection/hash, largest cluster 3. |

Chi tiết v6: tất cả 5 mask có valid-depth fraction 1.0. Bốn mask có đủ depth points nhưng largest cluster lần lượt 3, 6, 3, 3 (<100); mask `person` còn lại có 67/67 points và bị `rejected_insufficient_depth_points`. Một frame không có P2 detection. Hai lần quan sát refrigerator ở A1/revisit có cùng kích thước mask (494), cùng score (0.6584), cùng depth range (1.71–4.00 m) và cùng largest cluster (3), nên không cung cấp observation P3 nào. Đây tiếp tục là giới hạn density/geometry của profile 160×120 với `eps=0.02 m`, không phải lỗi RGB/depth grid alignment.

Kết luận pilot: chưa có ít nhất 3 accepted views của cùng vật tại các vị trí khác nhau. Hai lần nhận lại bàn ở v3/v4 là pixel-identical exact-pose revisit, không tính multiview evidence; v6 cũng không có accepted P3 observation. Không chạy route dài.

Route/config/lock từng vòng: [v2 route](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/habitat/hm3d_minival_v02_00800_u4ov_dev_pilot_v2.json), [v3 route](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/habitat/hm3d_minival_v02_00800_u4ov_dev_pilot_v3.json), [v4 route](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/habitat/hm3d_minival_v02_00800_u4ov_dev_pilot_v4.json), [v5 route](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/habitat/hm3d_minival_v02_00800_u4ov_dev_pilot_v5.json), [v6 route](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/habitat/hm3d_minival_v02_00800_u4ov_dev_pilot_v6.json). Lock manifests và route validation nằm cùng thư mục `artifacts/audits/u4ov_new_scene_hm3d_00800/`.

## 4. Quyết định tiếp theo

Tại thời điểm hoàn tất pilot v2–v6 ở 160×120, chưa có dữ liệu đủ điều kiện để chấm association precision/recall, false merge, fragmentation, causal class correctness hay final-track classification. Resolution ablation và pilot đa-view sau đó đã chạy; kết quả mới nhất, bao gồm các metrics còn unavailable khi chưa có review, xem trong [báo cáo resolution/multiview](/home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/docs/U4OV_HM3D_00800_RESOLUTION_MULTIVIEW_REPORT.vi.md).

Giữ nguyên model, sensor và P3 profile như yêu cầu; chưa chạy U3/U4 comparison dài. Một phương án riêng cần cân nhắc nếu mục tiêu bắt buộc là multiview ổn định: xin duyệt một capture profile độ phân giải cao hơn (00802 dùng 600×340) trong khi giữ nguyên detector weights và P3/U3/U4 thresholds. Đây là thay đổi ngoài khóa hiện tại, chưa áp dụng. Nếu sensor 160×120 phải giữ tuyệt đối, cần tiếp tục các micro-pilot target-specific; chỉ mở rộng lên 3–5 vật khi pilot xác nhận nhiều vị trí/góc có P3 accepted, gồm quay lại sau khi quan sát vật khác và có cặp vật gần nhau.

Không có test suite chạy. Không sửa baseline, `ref_OccuSG`, `ref_ConceptGraph`, graph runtime, room scores hay U5; không tải dataset/model và không cài package.
