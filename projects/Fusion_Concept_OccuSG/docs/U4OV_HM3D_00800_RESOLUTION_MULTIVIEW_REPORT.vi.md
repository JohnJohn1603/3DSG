# Báo cáo development U4-OV HM3D 00800: độ phân giải và multiview

Ngày chạy: 2026-10-08. Scene `00800-TEEsavR23oF`. Đây là development, không phải unseen test. Baseline và các cấu hình mặc định được giữ nguyên; không chạy native mapping, không sửa graph/room scores và chưa bắt đầu U5.

## Kết luận ngắn

- `fy=93.24` trong báo cáo diagnostic trước là lỗi tài liệu. Capture adapter, metadata, packet đưa vào P3 và công thức validator đều cho `fx=fy=80` ở 160×120; ở 640×480 là `fx=fy=320`. Không phát hiện lỗi intrinsics trong capture hoặc backprojection, không cần sửa dữ liệu/code. Báo cáo cũ đã được đính chính kèm ghi chú.
- Trên đúng năm pose và cùng sensor profile, 640×480 tăng P3 từ 0/5 lên 22/22 accepted. Đây là bằng chứng bottleneck P3 của capture 160×120 được gỡ trong ablation này; không phải bằng chứng detection hay identity chính xác hơn. P3 chậm do clustering fallback hiện có.
- Pilot 640×480 tạo 30 observation P3 accepted. Có ba quan sát accepted của một vùng vật thể từ ba vị trí camera khác nhau; lượt nhìn vật lân cận và quay lại cũng có trong route. Điều kiện thu thập multiview đạt, nhưng identity/class chưa được người dùng xác nhận nên mọi correctness metric vẫn unavailable.

## A. Audit intrinsics và projection

Capture dùng `CameraSensorSpec` với kích thước Habitat `(height,width)`, HFOV 90°, không khai báo vertical FOV hay distortion riêng. Metadata ghi width/height, HFOV, timestamp và pose cảm biến; ma trận K được tính bởi `adapters/habitat/frames.py::habitat_camera_intrinsics`. P3 gọi `frame_from_capture_record` rồi chuyển intrinsics vào `src/standalone_sg/geometry/backproject.py::backproject_depth`.

Backprojection dùng axial depth: `x=((u-cx)/fx)d`, `y=((v-cy)/fy)d`, `z=d`, sau đó biến đổi bằng `T_W_Copt`. Validator tính vertical FOV từ HFOV và aspect ratio, nên với pixel vuông ở cả hai profile 4:3 thì `fy=fx`.

| Profile | Intrinsics thực tế `(fx, fy, cx, cy)` |
|---|---|
| 160×120 | `(80, 80, 79.5, 59.5)` |
| 640×480 | `(320, 320, 319.5, 239.5)` |

Đã chạy fresh-render geometry validation cho capture v6 160, reference 160, candidate 640 và pilot; các kiểm tra depth/mask/pose qua. Artifact chi tiết: [intrinsics audit](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/intrinsics_audit.json). Báo cáo historical [diagnostic trước](U4OV_HM3D_00800_DEV_DIAGNOSTIC.vi.md) có ghi chú sửa `fy` và phạm vi kết luận.

## B. Ablation cùng năm pose

Hai cấu hình riêng: [reference 160](../configs/habitat/hm3d_minival_v02_00800_u4ov_resolution_v1_ref160.json) và [candidate 640](../configs/habitat/hm3d_minival_v02_00800_u4ov_resolution_v1_candidate640.json). Khác biệt hiệu lực duy nhất ngoài metadata/profile ID là resolution. Pose, timestamp, extrinsics, HFOV, depth limits, detector weights/preprocessing và P3 stride/eps/min_points/backend được giữ nguyên. Không upscale; candidate capture native 640×480.

| Profile | P2 detections | P3 accepted / rejected | P3 wall / peak RSS | Capture wall / peak RSS | P2 wall / peak RSS |
|---|---:|---:|---:|---:|---:|
| 160×120 | 5 | 0 / 5 | 0.64 s / 43.6 MiB | 16.58 s / 1,124.5 MiB | 2.25 s / 183.7 MiB |
| 640×480 | 22 | 22 / 0 | 310.73 s / 101.3 MiB | 17.80 s / 1,137.5 MiB | 4.43 s / 190.2 MiB |

So sánh vùng chỉ ghép trong cùng frame bằng bbox chuẩn hóa, class-agnostic, greedy 1:1 ở IoU≥0.10; đây là căn chỉnh vùng để review, không phải ID matching hay identity. Có 4 cặp vùng ghép, 1 detection chỉ thấy ở 160, 18 detection chỉ thấy ở 640. Trong bốn cặp, mask/depth valid count lần lượt tăng từ 494–672 lên 7,855–12,020 pixel; largest cluster từ 3–6 lên 7,168–10,721. Một cặp đổi raw class từ `truck` sang `refrigerator`, cho thấy P2 cũng thay đổi khi nhận ảnh native độ phân giải cao hơn. Không diễn giải các class/cluster này là đúng vật.

Ảnh: [side-by-side detections](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/resolution_side_by_side_detections.png). Bảng ghép: [matched regions](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/matched_regions.csv); các danh sách unmatched nằm cùng thư mục. Tóm tắt số liệu: [comparison JSON](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/comparison.json).

P3 vẫn dùng local fallback clustering, không đổi sang SciPy hay backend khác. 640×480 vì vậy tốn khoảng 5 phút cho 5 frame; peak RSS thấp hơn 110 MiB. Đây là chi phí thực tế cần cân nhắc trước khi mở rộng route.

## C. Một pilot multiview

Route [pilot v2](../configs/habitat/hm3d_minival_v02_00800_u4ov_multiview_pilot_v2_640.json) được khóa trước capture. Navmesh validation đạt 5/5 vị trí và mọi cạnh nối route đều có đường đi. Route gồm view A, dịch ngang/đổi vị trí sang B, glance về vùng lân cận từ B, dịch sang C rồi quay lại đúng pose A. Vị trí A/B/C khác nhau; lượt cuối A2 trùng pose A1 nên chỉ là revisit, không tính view độc lập. Glance frame ở cùng camera position với B cũng không được tính là vị trí view độc lập.

Pilot capture/P2/P3: 13.32 s / 1,149 MiB; 3.48 s / 192.8 MiB; 238.27 s / 91.8 MiB. P2 có 30 detection; P3 chấp nhận 30, loại 0. Không chạy detector/P3 trên dữ liệu mới sau đó. Ba candidate P3 của cùng vùng mục tiêu ở frame 0, 1, 3 có camera-origin bearing tương ứng khoảng 219.5°, 179.7°, 139.9° nhìn về vùng chung, tức ba vị trí quan sát khác nhau. Chúng có ID `...0000:det-0003`, `...0001:det-0003`, `...0003:det-0002`; nhãn detector `dining table` chỉ mô tả output model, chưa phải annotation identity.

Frame 2 là glance từ vị trí B; detection mục tiêu khác (`...0002:det-0001`) và centroid P3 thay đổi đáng kể. Không gộp nó vào ba viewpoint độc lập nếu chưa review. Frame 4 là exact-pose revisit A2; RGB SHA trùng frame 0 (`7d19e292…716ae`) và cluster trùng, nên chỉ kiểm tra tái quan sát, không phải bằng chứng view độc lập.

Route, pose và hashes được khóa tại [pilot lock](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/multiview_pilot_v2_lock.json); navmesh: [route validation](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/multiview_pilot_v2_route_validation.json). Observation index gồm toàn bộ 30 detection/P3/U3/U4-OV outputs: [CSV](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/multiview_pilot_observation_index.csv) và [JSONL](../artifacts/audits/u4ov_new_scene_hm3d_00800/resolution_ablation_v1/multiview_pilot_observation_index.jsonl).

## D. Association và semantic output

Các association modes đọc cùng 30 P3 observations và mỗi mode tự khởi tạo state rỗng trên đầu pilot. Không có GT/review hoặc room label trong predictor. Source raw/dedup cho 12 predicted tracks mỗi mode; geometry-only 14; geometry+appearance 16. Tất cả 30 observation được gán, 0 defer; source raw và dedup giống nhau do không có duplicate mask bị loại ở input này. Đây là cấu trúc output, chưa phải metric correctness. Association precision/recall, false merge và fragmentation: **unavailable** trước khi có identity review.

Vùng mục tiêu được gán vào cùng một predicted track qua năm frame ở source và geometry-only. Geometry+appearance tách frame 2 thành track khác, còn frame 0/1/3/4 ở cùng track. Đây chỉ là quyết định khác nhau giữa mode; không thể kết luận mode nào đúng nếu chưa xác nhận identity.

U4-OV dùng đúng checkpoint, vocabulary 105 nhãn, text cache, preprocessing và thresholds đã khóa; CLIP cosine không phải xác suất hiệu chuẩn. 30 observation được phân loại single-view: 5 classified, 25 conflicted, 0 unknown. Tất cả mode có 3 classified causal observations trong 30; số còn lại conflicted/unknown khác nhau. Final-map geometry-only có 1 classified, 5 conflicted, 8 unknown trên 14 predicted tracks. Geometry+appearance có 1/5/10 trên 16 tracks; source raw/dedup 1/6/5 trên 12 tracks. Coverage/abstention ở đây chỉ mô tả trạng thái model, không nói đúng/sai.

Runtime U2 image features: 13.96 s tổng, 3.83 s inference, peak RSS 1,522.5 MiB; 30 vectors 512D finite và normalized (atol=rtol=1e-5). U3 cả bốn mode: 0.54 s, peak RSS 48.1 MiB. U4-OV: 22.98 s, peak RSS 666.7 MiB. Chỉ correctness review mới cho phép tính causal/final-map correctness, harmful label change và association metrics. Review gallery theo geometry-only tracks: [index.html](../artifacts/runs/hm3d_00800_u4ov_multiview_pilot_v2_640/u4ov/review/index.html). Vui lòng xác nhận identity và class riêng cho từng observation/track; exact-pose revisit và glance cùng vị trí cần được phân biệt với view độc lập.

## Provenance, lệnh và kiểm tra

Model, vocabulary và predictor locks cùng hashes có trong pilot lock. Detector ONNX SHA-256: `0bc32bc92e985b881141ef9bd2216e2a746f70519d0d24da9fc85decc4428cf4`. P3 profile giữ stride 1, eps 0.02 m, min_points 100 và fallback backend cũ. Capture dùng Habitat env; P2 dùng perception env có sẵn; P3/U2/U3/U4-OV dùng `.venv-appearance` có sẵn. Không cài package, tải model hay dataset.

Lệnh capture candidate 640 đã chạy thành công từ root workspace:

```bash
/home/kiettran05/miniconda3/envs/habitat/bin/python -B scripts/capture_habitat_replica.py \
  --scene /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/habitat_data/versioned_data/hm3d-0.2/hm3d/minival/00800-TEEsavR23oF/TEEsavR23oF.basis.glb \
  --config configs/habitat/hm3d_minival_v02_00800_u4ov_resolution_v1_candidate640.json \
  --output artifacts/runs/hm3d_00800_u4ov_resolution_v1_candidate640/capture
```

P2/P3 launcher đã chạy trên cùng capture; đối chiếu chính xác tham số ở `capture_time.txt`, `p2_time.txt`, `p3_time.txt` trong thư mục run candidate. Pilot U2/U3/U4-OV launchers và timing nằm trong thư mục run pilot.

Đây là các lệnh P2/P3 candidate đã thực thi (không cần chạy lại để xem kết quả):

```bash
/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/.venv-perception/bin/python -B /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/scripts/run_replica_cad_instance_segmentation.py \
  --capture /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/runs/hm3d_00800_u4ov_resolution_v1_candidate640/capture \
  --output /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/artifacts/runs/hm3d_00800_u4ov_resolution_v1_candidate640/detection_2d \
  --config /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/perception/yolo11n_seg_replica_cad_dev.json \
  --weights /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/models/ultralytics/yolo11n-seg.onnx \
  --names /home/kiettran05/Desktop/test_Room_segmentation/Fusion_Concept_OccuSG/configs/perception/coco.names

.venv-appearance/bin/python -B scripts/project_detection_masks_to_3d.py \
  --capture artifacts/runs/hm3d_00800_u4ov_resolution_v1_candidate640/capture \
  --detection-2d artifacts/runs/hm3d_00800_u4ov_resolution_v1_candidate640/detection_2d \
  --output artifacts/runs/hm3d_00800_u4ov_resolution_v1_candidate640/detection_3d \
  --profile configs/perception/yolo11n_seg_replica_cad_dev.json
```

Đã chạy: `py_compile` cho ba runner U2/U3/U4-OV; route/navmesh validation; ba fresh-render geometry validators; capture, P2, P3, U2, U3 và U4-OV pilot. Không chạy full test suite. Không sửa thuật toán; sửa tài liệu duy nhất là đính chính `fy` trong diagnostic trước.

## Điểm dừng

Độ phân giải cao đã gỡ bottleneck P3 trong ablation năm pose, nhưng chi phí fallback P3 đáng kể. Pilot đạt tiêu chí thu thập ba góc camera với P3 observations của một vùng mục tiêu; kết quả chưa được chấm vì thiếu xác nhận identity/class. Bước kế tiếp là review gallery/observation index, rồi tính metrics trên annotations evaluator-only. 00800 vẫn là development; không promote, không thay default hoặc graph runtime.
