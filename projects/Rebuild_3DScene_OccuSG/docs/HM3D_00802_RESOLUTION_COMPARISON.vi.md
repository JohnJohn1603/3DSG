# Báo cáo thí nghiệm: ảnh hưởng của độ phân giải RGB-D trên HM3D 00802

## Tóm tắt

Thí nghiệm so sánh cùng một route quan sát cố định trong scene HM3D `00802-wcojb4TFT35` ở độ phân giải RGB-D 160×120 và 320×240. Chỉ độ phân giải cảm biến thay đổi; route, 29 pose quan sát, timestamps, HFOV 90°, extrinsics, giới hạn depth và các cấu hình detector/mapping/segmentation/semantic được giữ nguyên.

Ở 320×240, số Detection2D tăng từ 25 lên 69; Detection3D được chấp nhận tăng từ 11 lên 46; ObjectNodes tăng từ 9 lên 23. Trong khi đó số RoomNodes giữ nguyên ở 5, còn số region cuối M4 là 5 thay vì 6. Runtime tổng các stage capture đến evaluation tăng từ 169,3 s lên 270,8 s. Peak RSS của riêng tiến trình Python native replay tăng từ 528,8 MiB lên 606,1 MiB.

Kết quả cho thấy tăng resolution tạo thêm evidence ảnh/depth và tăng chi phí xử lý. **Các số đếm cao hơn không chứng minh detection, object association hay room segmentation chính xác hơn.** Không có ground truth độc lập phù hợp để kết luận accuracy trong thí nghiệm này.

## Câu hỏi và thiết kế thí nghiệm

**Câu hỏi:** Khi giữ nguyên scene, route và thuật toán, việc tăng RGB-D từ 160×120 lên 320×240 ảnh hưởng thế nào đến observations được pipeline tiếp nhận, kết quả graph và chi phí xử lý?

| Thành phần | Thiết lập |
|---|---|
| Scene | HM3D minival v0.2, `00802-wcojb4TFT35` |
| Baseline | `hm3d_00802_20261006T012423.570046Z_9336d5`, route `main`, 160×120 |
| Candidate | `hm3d_00802_20261006T091921.402660Z_0220b9`, route `main_320`, 320×240 |
| Capture | 29 frame; cùng route, pose, timestamp và sensor extrinsics |
| Camera | HFOV 90°; depth limits 0,1–10 m; các cấu hình khác giữ nguyên |
| Detector/P3 | Cùng weights, detector input/preprocessing, thresholds/NMS; P3 stride 1, `cluster_eps=0,02 m`, `min_points=100` |
| Mapping/graph | Cùng pipeline/native binaries, M6 association, M4 source preprocessing và semantic rules |

Route được cố định bằng các pose quan sát trong Habitat-Sim; đây không phải robot tự định vị hoặc tự lập kế hoạch đường đi. Navmesh chỉ dùng kiểm tra navigability của route. Room/semantic GT không được đưa vào builder.

Ở HFOV 90°, intrinsics được kiểm tra trên grid trace của tất cả 29 frame mỗi run:

| Resolution | fx | fy | cx | cy |
|---|---:|---:|---:|---:|
| 160×120 | 80 | 80 | 79,5 | 59,5 |
| 320×240 | 160 | 160 | 159,5 | 119,5 |

Ảnh baseline được phóng nearest-neighbor chỉ trong contact sheet để đặt cạnh ảnh 320×240; raw capture không bị upscale. Capture 320×240 được render trực tiếp ở kích thước mới.

## Kết quả

| Chỉ số | 160×120 | 320×240 |
|---|---:|---:|
| Capture frames | 29 | 29 |
| Detection2D observations | 25 | 69 |
| Detection3D accepted / rejected | 11 / 14 | 46 / 23 |
| ObjectNodes cuối graph | 9 | 23 |
| Regions cuối M4 | 6 | 5 |
| RoomNodes cuối graph | 5 | 5 |
| Nhãn semantic predicted / unknown | 1 / 5 | 2 / 3 |
| Tổng runtime stage capture→evaluation | 169,323 s | 270,768 s |
| Native replay stage (launcher timing) | 143,031 s | 208,857 s |
| Peak RSS tiến trình Python native replay | 528,8 MiB | 606,1 MiB |

RSS được ghi bằng `resource.getrusage(RUSAGE_SELF)` trong tiến trình Python replay. Đây không phải peak memory tổng của simulator, tiến trình native con hoặc toàn bộ pipeline. Tổng runtime là tổng elapsed time của các stage capture, Detection2D, Detection3D, native replay, semantic và evaluation; không bao gồm bước tạo report so sánh.

### Hình ảnh đối chiếu

Mỗi hàng là cùng frame ID và simulation timestamp ở hai resolution.

![Ảnh RGB cùng frame ở 160×120 và 320×240](../artifacts/runs/hm3d_00802_20261006T091921.402660Z_0220b9/comparison_to_baseline/resolution_side_by_side.png)

## Diễn giải

- 320×240 có gấp đôi số pixel theo mỗi chiều, tức gấp bốn pixel ảnh mỗi frame. Khi P3 sampling stride giữ ở 1, depth cung cấp mật độ điểm ảnh cao hơn; số Detection2D/Detection3D accepted và ObjectNodes tăng là kết quả có thể quan sát được.
- Detection3D accepted/rejected là số observation được phân loại ở bước lifting/clustering, không phải object recall hoặc precision. ObjectNodes nhiều hơn cũng chưa loại trừ duplicate hoặc association sai.
- RoomNodes vẫn là 5; M4 regions đổi từ 6 thành 5. Không có room GT đã xác nhận để đánh giá ranh giới hoặc khẳng định phân phòng tốt hơn.
- Tổng runtime tăng khoảng 59,9%; peak RSS của tiến trình Python replay tăng khoảng 14,6%. Đây là so sánh giữa hai run cụ thể, không phải benchmark realtime tổng quát.

## Kiểm tra tính toàn vẹn và giới hạn

- Preflight route `main_320` pass; cả 29 pose navigable và các pose liền kề trong route được kiểm tra kết nối.
- RGB(A), depth và valid mask được kiểm tra đúng 320×240 trên candidate; timestamp, route entry, pose base và sensor extrinsics khớp baseline.
- Snapshot reload, delta replay và fresh replay đều pass trên candidate.
- Baseline run được giữ nguyên; hash launcher manifest, capture manifest và effective route config trước/sau đối chiếu khớp.
- Không có object-mask GT hay room-boundary GT phù hợp được dùng cho phép so sánh này. Vì vậy không báo object/room accuracy và không kết luận candidate chính xác hơn.

## Kết luận

> Với cùng scene, route và cấu hình xử lý, RGB-D 320×240 tạo nhiều observations và object evidence hơn 160×120, đồng thời tăng runtime và peak RSS của tiến trình replay được đo. Số RoomNodes cuối không đổi, còn số region M4 thay đổi nhẹ. Thí nghiệm xác nhận pipeline xử lý được capture 320×240 và định lượng được chi phí/lượng evidence; nó chưa chứng minh chất lượng phân phòng hoặc nhận diện object tăng. Để kết luận chất lượng, cần đánh giá trên annotation độc lập có phạm vi phù hợp.

## Tái lập

Từ workspace root:

```bash
./run_scene.sh check hm3d_00802 --route main_320
./run_scene.sh run hm3d_00802 --route main_320
./run_scene.sh results
./run_scene.sh open <RUN_ID> comparison
```

Run reference 160×120 được khai báo trong route `main_320` và giữ nguyên tại `artifacts/runs/hm3d_00802_20261006T012423.570046Z_9336d5`.
