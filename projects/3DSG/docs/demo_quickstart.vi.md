# Chạy demo 3DSG

Trạng thái và giới hạn đã kiểm chứng: [implementation_status.md](implementation_status.md). Native ConceptGraphs và OccuSG được chạy trong các repository sibling, không được copy vào `3DSG`: OccuSG ở `../Rebuild_3DScene_OccuSG`, ConceptGraphs ở `../Fusion_Concept_OccuSG`. Adapter/plugin và worker orchestration nằm trong `exploration/native_graph_plugin.py`, `exploration/native_perception_client.py` và `scripts/replay_saved_native_keyframe.py`. Dùng lệnh native bên dưới để bật paired workers; lệnh desktop mặc định vẫn dùng graph demo trừ khi cấu hình UI đổi plugin.

## Desktop live

Từ thư mục `3DSG`, chạy bằng Python có PyQt5:

```bash
python3 scripts/run_desktop.py
```

Desktop mặc định dùng Habitat Python tại `/home/kiettran05/miniconda3/envs/habitat/bin/python`, scene HM3D 00802 và profile CPU. Nút **Start exploration** mở runner riêng; checkbox **Build live RGB-D object graph** bật P2 + provisional RGB-D graph. Bỏ checkbox để chỉ chạy navigation. Có thể đặt thời lượng episode bằng:

```bash
python3 scripts/run_desktop.py --duration-s 60
```

Chạy không có detector hoặc mở phiên navigation để tách lỗi:

```bash
python3 scripts/run_desktop.py --navigation-only
```

## Xem lại một run

```bash
python3 scripts/run_desktop.py --live-dir runs/p2-rgbd-graph-smoke-20261021-c/live
```

Viewer đọc state, RGB-D, NavMap và graph revisions từ folder live; không tự khởi động simulator. Có thể mở folder này trong lúc runner khác đang chạy để theo dõi live, hoặc mở sau khi run hoàn tất để replay.

Để vừa khởi động vừa xem live trong cùng cửa sổ, chạy `python3 scripts/run_desktop.py`, giữ checkbox graph bật rồi bấm **Start exploration**. Không truyền `--live-dir` trong cách chạy này; `--live-dir` chỉ gắn viewer vào một run do tiến trình khác sở hữu.

Muốn mở một cửa sổ UI để vừa chạy vừa xem native ConceptGraphs/OccuSG, dùng lệnh sau rồi bấm **Start exploration**. Không thêm `--live-dir`: tùy chọn đó chỉ mở viewer cho một run khác và không khởi động simulator.

```bash
python3 scripts/run_desktop.py \
  --scene-config configs/exploration/hm3d_00802_floor_endpoint_calibrated.json \
  --profile configs/exploration/cpu_p2_motion.yaml \
  --duration-s 120 \
  --graph-plugin exploration.native_graph_plugin:create_plugin
```

## Yêu cầu P2

P2 mặc định dùng worker interpreter, weights và cấu hình trong hai project sibling:

- `../Rebuild_3DScene_OccuSG/.venv-perception/bin/python`
- `../Rebuild_3DScene_OccuSG/artifacts/models/ultralytics/yolo11n-seg.onnx`
- `../Fusion_Concept_OccuSG/configs/perception/`

Có thể override interpreter/model/config khi gọi `scripts/run_frontier_habitat.py --enable-p2`. Desktop UI hiện dùng các giá trị mặc định này. Scene config, profile và output root có CLI flags ở `scripts/run_desktop.py`.

Launcher hỗ trợ thay graph integration bằng plugin `module:factory` qua `--graph-plugin`. Một plugin tự quản lý packet conversion/session/publish có thể chạy với `--enable-graph` mà không cần P2; plugin demo hiện tại yêu cầu `--enable-p2` vì dùng các YOLO masks.

Graph được hiển thị là provisional YOLO-mask + RGB-D geometry hypotheses. Nó chứng minh được luồng keyframe, lift, revision và viewer; chưa phải ConceptGraphs/OccuSG upstream parity, chưa có room membership và chưa được chấm độ chính xác.

## Tái lập gate P2 và integrated live graph

Các lệnh dưới đây dùng cấu hình chẩn đoán đã qua locomotion gate; profile mặc định của desktop chưa đổi.

Navigation-only, 120 giây:

```bash
/home/kiettran05/miniconda3/envs/habitat/bin/python scripts/run_frontier_habitat.py \
  --scene-config configs/exploration/hm3d_00802_split_depth_fast_motion.json \
  --profile configs/exploration/cpu_p2_motion.yaml \
  --budget-s 120 --run-id my-p2-run
python3 scripts/evaluate_frontier_run.py runs/my-p2-run
```

Frontier + P2 + native ConceptGraphs/OccuSG paired workers, 120 giây:

```bash
/home/kiettran05/miniconda3/envs/habitat/bin/python scripts/run_frontier_habitat.py \
  --scene-config configs/exploration/hm3d_00802_floor_endpoint_calibrated.json \
  --profile configs/exploration/cpu_p2_motion.yaml \
  --budget-s 120 --run-id my-native-integrated-run \
  --enable-graph --enable-p2 \
  --graph-plugin exploration.native_graph_plugin:create_plugin
```

Để xem live trong lúc lệnh trên đang chạy, mở terminal thứ hai tại thư mục `3DSG`:

```bash
python3 scripts/run_desktop.py --live-dir runs/my-native-integrated-run/live
```

Sau khi runner kết thúc, có thể đánh giá movement gate:

```bash
python3 scripts/evaluate_frontier_run.py runs/my-native-integrated-run
```

Thay `my-native-integrated-run` bằng một tên run mới cho mỗi lần chạy và dùng cùng tên ở cả hai terminal. Native provider cần các sibling repositories, checkpoints, mapper runtimes và model assets đã được cài tại đường dẫn mặc định. Nó gửi frame qua packet/worker adapters, không yêu cầu import ROS2 vào Habitat process. Snapshot viewer lấy room/navigation prefix từ OccuSG và thêm P3 frame observations; ConceptGraphs branch được lưu riêng, hiện chưa semantic-join vào snapshot. Room labels còn provisional và chưa được chấm accuracy; auxiliary navigation depth chưa được đưa vào native mapper.

`navigation_depth` là depth phụ hướng xuống để hỗ trợ floor evidence; RGB/depth semantic vẫn nhìn về trước. Capture adapter native lưu kênh này cùng calibration/hash riêng và OccuSG adapter tích hợp nó như một sensor riêng; ConceptGraphs vẫn dùng RGB-D camera chính. Đây là mapping evidence bổ sung, chưa phải đánh giá độ chính xác room/object. Các lệnh chưa xác nhận 10-minute resource gate, graph drain ổn định hoặc graph quality.
