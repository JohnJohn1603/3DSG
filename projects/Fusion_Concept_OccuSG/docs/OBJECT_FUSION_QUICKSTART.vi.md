# Fixed-assignment Object Geometry Fusion

Chạy trong thư mục `Fusion_Concept_OccuSG`. Công cụ tích lũy point cloud theo assignment đã có của run. Nó không đổi association, class, graph node, room label hay room score.

## Lệnh

```bash
./run_scene.sh fuse-objects <FULL_RUN_ID> --check
./run_scene.sh fuse-objects <FULL_RUN_ID>
./run_scene.sh open <FUSION_RESULT_ID> fusion
```

Trong `scene>` dùng `/fuse-objects ...` và `/open <FUSION_RESULT_ID> fusion`.

`--check` chỉ đọc: kiểm tra capture/P2/P3 hashes, replay gắn cùng P3, lineage có quyết định cho mọi accepted observation, interpreter/dependencies và selected-point sidecar. Nó không tạo output, chạy P3 hay fusion.

Khi chạy, sidecar hợp lệ được reuse. Nếu thiếu, launcher chạy lại **P3 trên RGB-D và P2 mask cache** vào thư mục riêng của fusion result; không capture hoặc chạy detector. Detection3D dựng lại phải khớp cache chuẩn trước khi fusion tiếp tục. Với cache cũ không lưu selected pixel indices, parity này không chứng minh point-set parity chính xác; manifest ghi sidecar là reconstructed.

Mỗi lần chạy sinh `FUSION_RESULT_ID` mới dưới `artifacts/object_fusions/`. Terminal in backend input, số observations applied/alias/deferred, objects, points/voxels, stage times, RSS và viewer. Dùng `/status <FUSION_RESULT_ID>`, `/logs <FUSION_RESULT_ID>`, `/cancel <FUSION_RESULT_ID>` trong phiên interactive khi job chạy. Nếu không có GUI, mở file `index.html` trực tiếp.

Viewer có object selector, RGB/mask evidence theo full observation ID, timeline cloud revisions, alias/deferred provenance và backend input. `unique_sensor_pose_count` là số pose duy nhất; exact-pose revisit không tăng số này. Đây là geometry fusion trên fixed assignments, không phải bằng chứng association chính xác hơn.
