# Habitat Scene Launcher

Launcher là giao diện mỏng gọi các runner đã có; không thay M1–M10, detector, P3/M6, segmentation hay semantic rules. Hiện đăng ký hai scene thực có config:

| Alias | Dataset / scene | Route |
|---|---|---|
| `hm3d_00802` | HM3D minival v0.2 / `00802-wcojb4TFT35` | `smoke` (10 pose), `main` (29 pose) |
| `replica_apt0` | ReplicaCAD / `apt_0` | `dev_long` (24), `dev` (16), `reobserve` (5), `hires` (16) |

`maps` chỉ đọc registry/config/file; không load simulator. “Đã cấu hình” không đồng nghĩa preflight pass. Chạy `check` để kiểm tra asset, environment, native binaries và route/navmesh.

## CLI

Từ workspace root chạy như dưới. Từ working directory khác, gọi `.../Rebuild_3DScene_OccuSG/run_scene.sh` bằng đường dẫn tuyệt đối; launcher tự tìm project root:

```bash
./run_scene.sh help
./run_scene.sh maps
./run_scene.sh info hm3d_00802
./run_scene.sh routes hm3d_00802
./run_scene.sh check hm3d_00802 --route smoke
./run_scene.sh run hm3d_00802 --route smoke --dry-run
./run_scene.sh run hm3d_00802 --route smoke
./run_scene.sh runs
./run_scene.sh status <run-id>
./run_scene.sh logs <run-id> --stage native_replay --tail 100
./run_scene.sh results <run-id>
./run_scene.sh open <run-id> map
./run_scene.sh evaluate <run-id>
./run_scene.sh evaluate-objects hm3d_00802_20261006T091921.402660Z_0220b9 --check
./run_scene.sh evaluate-objects hm3d_00802_20261006T091921.402660Z_0220b9
./run_scene.sh open hm3d_00802_20261006T091921.402660Z_0220b9 object-evaluation
./run_scene.sh open hm3d_00802_20261006T091921.402660Z_0220b9 object-review
```

### So sánh RGB-D 160×120 và 320×240 trên HM3D 00802

Scene alias vẫn là `hm3d_00802`; route mặc định `main` giữ 160×120. Route `main_320` dùng cùng 29 observation poses/config và chỉ đổi resolution cảm biến. Launcher preflight xác minh baseline route và lưu run riêng, không ghi đè run tham chiếu.

```bash
./run_scene.sh use hm3d_00802
./run_scene.sh routes hm3d_00802
./run_scene.sh check hm3d_00802 --route main_320
./run_scene.sh run hm3d_00802 --route main_320
./run_scene.sh results
```

Trong prompt tương tác tương ứng: `/use hm3d_00802`, `/routes`, `/check hm3d_00802 --route main_320`, `/run hm3d_00802 --route main_320`, `/status`, `/results`. Mỗi lần `run` tạo `artifacts/runs/<run-id-mới>/`. Candidate ghi `capture_resolution_px`, effective route config/hash và baseline run ID trong `launcher_manifest.json`; tự kiểm tra capture RGB(A)/depth cùng pose/extrinsics và intrinsics, rồi xuất `comparison_to_baseline/report.vi.md` và `resolution_side_by_side.png`.

Mở kết quả bằng `./run_scene.sh open <run-id> comparison` để mở report đối chiếu; `report` ưu tiên comparison report nếu có, nếu không sẽ mở evaluation report. `map` mở spatial map và `objects` mở evidence. Ảnh so sánh ở `artifacts/runs/<run-id>/comparison_to_baseline/resolution_side_by_side.png`. Ảnh baseline 160×120 trong contact sheet chỉ được phóng nearest-neighbor để xem cạnh ảnh 320×240; raw capture không bị resize. Giữ cùng HFOV, model, detector preprocessing, P3, M6, segmentation và semantic rules. Tăng resolution ở cùng stride làm dày pixel/depth evidence; nhiều detections/nodes/labels hơn không tự chứng minh accuracy tốt hơn. Launcher hiện chưa đo memory.

### Route capture 600×340

Route `main_600` giữ nguyên 29 pose quan sát của `main`, HFOV 90°, depth limits, sensor extrinsics và profile xử lý; chỉ đổi `resolution_px` sang `[600, 340]`. Đây là cấu hình capture riêng, không tự gắn baseline hay tạo report so sánh resolution. Preflight kiểm tra route/asset/navigability; sau khi run xong có thể gọi `/evaluate RUN_ID`, `/evaluate-objects RUN_ID` và `/object-results RUN_ID` như thường lệ.

```bash
./run_scene.sh check hm3d_00802 --route main_600
./run_scene.sh run hm3d_00802 --route main_600
```

Không có argument thì vào prompt `scene>`; gõ `/help` hoặc `/all`. `/all` chỉ in hướng dẫn, không chạy mọi map. `/use hm3d_00802` chọn scene cho phiên; CLI có thể truyền alias trực tiếp.

`run --dry-run` chỉ in kế hoạch, không tạo run hay khởi chạy simulator. Run bình thường tạo ID có timestamp microsecond + nonce và output độc quyền dưới `artifacts/runs/<run-id>/`; không tái sử dụng output cũ. Các stage chạy nối tiếp và dừng ở lỗi đầu tiên:

`preflight → capture → detection_2d → detection_3d → native_replay/grid_trace → semantic → evaluation`

Run manifest lưu input hashes, đường dẫn môi trường, stage status/timing/exit code, log và artifact paths; không copy dataset/model vào run. `/evaluate` chỉ đọc capture/replay đã hoàn tất, tạo evaluation directory mới và không chạy lại simulator/perception. Object GT metrics không được tự gắn từ capture cũ.

Trong interactive mode, `/run` và `/evaluate-objects RUN_ID` chạy trong worker do launcher quản lý; dùng `/status RUN_ID`, `/logs RUN_ID --stage object-evaluation`, `/cancel RUN_ID`. `evaluate-objects RUN_ID --check` mặc định in preflight ngắn; thêm `--json` để lấy toàn bộ JSON/hash. Check không render GT hoặc tạo evaluation folder. Khi chấm, launcher báo các stage; lúc xong in evaluation ID và summary metrics. Evaluation dùng lock chung để không chạy đồng thời với pipeline launcher. `Ctrl+C` hoặc `/cancel` gửi SIGTERM tới process group evaluator, chờ graceful shutdown rồi mới force-stop nếu cần; partial output giữ trạng thái cancelled/failed riêng. Lỗi/hủy evaluator không sửa `launcher_manifest.json` hay status predictor.

Đọc kết quả đã lưu mà không chạy lại evaluation:

```bash
./run_scene.sh object-results RUN_ID
./run_scene.sh object-results RUN_ID --frames
./run_scene.sh object-results RUN_ID --frame 12
./run_scene.sh object-results RUN_ID --frame 12 --eval-id EVAL_ID
```

Interactive tương ứng: `/object-results RUN_ID`, `/object-results RUN_ID --frames`, `/object-results RUN_ID --frame 12`. Mặc định chọn evaluation `completed` mới nhất và in ID; `--eval-id` chọn một bản completed cụ thể. `--json` dành cho automation. Frame index bắt đầu từ 0; các số đếm là observation-level, không phải unique ObjectNode count. Dòng `matched` chỉ là mask overlap theo protocol, không khẳng định class/identity đúng. `object-results` đọc metrics/matches đã lưu và, với các evaluation cũ chưa lưu ignored status cho từng detection, tái dựng riêng trạng thái ignored-vs-FP từ detection mask + semantic cache cùng protocol; không chạy lại matching hoặc predictor.

### Object GT evaluator (HM3D)

Evaluator semantic-object là bước hậu xử lý riêng trên một run đã completed. Nó render semantic sensor tại pose/extrinsics/resolution đã capture, đối chiếu RGB/depth theo tolerance được khóa trong `protocol.json`, rồi lưu semantic instance maps/cache evaluator-only. GT không được ghi vào capture, D2/P3 hay M1–M10. Chạy đầu tiên trên run 320×240 đã chỉ định:

```bash
./run_scene.sh evaluate-objects hm3d_00802_20261006T091921.402660Z_0220b9 --check
./run_scene.sh evaluate-objects hm3d_00802_20261006T091921.402660Z_0220b9
./run_scene.sh open hm3d_00802_20261006T091921.402660Z_0220b9 object-evaluation
./run_scene.sh open hm3d_00802_20261006T091921.402660Z_0220b9 object-review
```

Trong prompt interactive, thêm `/` trước mỗi lệnh. `--check` không tạo evaluation output. Mỗi chấm điểm tạo `artifacts/runs/RUN_ID/object_evaluations/EVAL_ID/` mới; semantic masks được cache riêng dưới `artifacts/gt_cache/hm3d/SCENE_ID/` theo hash capture-frame RGB/depth/metadata, route, semantic assets, renderer và code. Đổi category mapping chỉ tạo evaluation mới, không cần render lại nếu geometry cache còn hợp lệ. `object-evaluation` mở `report.vi.md`; `object-review` mở HTML có RGB + prediction mask màu xanh, semantic GT màu magenta, IoU, P3 decision và lineage endpoint nếu có. Chỉ evaluation `completed` mới được tự chọn để mở.

Mapping ban đầu ở `configs/evaluation/hm3d_00802_object_category_mapping_v1.json` là **provisional**, không phải xác nhận người dùng. Chỉ `confirmed` kèm reviewer/date/provenance mới được đưa vào class-aware metrics; chưa confirmed thì class-aware precision/recall để unavailable. Class-agnostic mask diagnostic có phạm vi semantic visible-instance đã nêu, không phải AP/mAP hay room accuracy. P3 accepted rate không phải accuracy. Run này chỉ lưu first/last detection IDs trên ObjectNode; không lưu đầy đủ M6 match/create/suppression từng detection. Evaluator không chạy lại association và sẽ báo fragmentation/false merge theo endpoint lineage là incomplete; suppression-vs-match unavailable, không được suy đoán.

## Đăng ký scene mới

Trước tiên phải có route JSON, scene-evaluation profile và asset/config/navmesh tương ứng. Loader hiện chỉ hỗ trợ HM3D scene `.basis.glb` và ReplicaCAD scene instance đã được tích hợp; mesh rời chưa có dataset config/route không tự trở thành runnable scene. Registry không tạo route hay GT.

1. Tạo route/sensor config và evaluation profile mới theo schema hiện dùng; giữ chúng riêng, không sửa baseline.
2. Sao chép entry phù hợp từ `configs/launcher/templates/hm3d_scene_entry.template.json` hoặc `replica_cad_scene_entry.template.json` thành JSON riêng. Điền alias duy nhất, evaluation profile, các route aliases/path và `default_route`.
3. Kiểm tra file paths, scene ID, loader type và profile/route consistency.
4. Đăng ký (chưa chạy simulator):

   ```bash
   ./run_scene.sh register --config configs/launcher/my_scene_entry.json
   ./run_scene.sh maps
   ./run_scene.sh info my_scene
   ./run_scene.sh check my_scene --route smoke
   ```

Registry ở `configs/launcher/scenes.json`; chỉ giữ alias và tham chiếu config, không nhân bản baseline parameters. Alias có thể đặt rõ ràng theo map (ví dụ `hm3d_00802`, `replica_apt0`); không thêm A/B/C/D/E giả nếu chưa có scene/config tương ứng.

## Môi trường

Đường dẫn local đặt trong `configs/launcher/local_environment.json`, không hard-code vào handlers. `project_python` là `.venv-perception/bin/python`; **giữ nguyên đường dẫn symlink này**, không đổi thành `/usr/bin/python3`. Venv chứa NumPy `2.5.3`, Pillow `10.2.0`, PyYAML `6.0.1`, Matplotlib `3.9.1` và ONNX Runtime `1.24.4`; các pin trực tiếp được ghi trong `project_package_pins`, còn phiên bản ONNX Runtime cũng phải khớp detector profile. PyYAML/Matplotlib được bổ sung trong venv; Python hệ thống không bị cài/sửa. `habitat_python` vẫn là môi trường Habitat-Sim riêng đã pin.

`check` import-thử từng dependency, xác minh pin, render PNG Matplotlib headless, kiểm tra Habitat-Sim/assets/native binaries và kiểm tra navigability. Nó báo capability riêng cho perception, replay, semantic và evaluation. Launcher giữ nguyên interpreter path của venv; không dereference symlink Python.

Trạng thái kiểm chứng ngày 2026-10-05: `hm3d_00802/smoke` pass dependency/environment check và route 10/10 navigable. End-to-end run `hm3d_00802_20261005T155214.379554Z_657f1e` pass đủ capture → perception → native replay → semantic → evaluation. Replay xác nhận `snapshot_reload_equal=true`, `delta_replay_equal=true`, 10/10 grid revisions được lưu. Kết quả ở `artifacts/runs/hm3d_00802_20261005T155214.379554Z_657f1e/`. Bốn RoomNode và bốn ObjectNode là smoke integration evidence, không phải đánh giá accuracy; cả bốn semantic label vẫn unknown do route ngắn/không có GT phù hợp.

Run đầu `hm3d_00802_20261005T154958.780292Z_9c264c` được giữ lại với trạng thái failed: pipeline đã replay xong nhưng launcher cũ kiểm tra nhầm `grid_trace/manifest.json`; writer thực tế định nghĩa `grid_trace/index.json`. Contract launcher đã được sửa và lượt tiếp theo pass. Không xóa run lỗi để giữ provenance.

## Đọc kết quả

- `capture/capture_manifest.json`: raw RGB-D, pose, timestamps, frame IDs.
- `detection_2d/overlays/` và `masks/`: RGB/model detections.
- `detection_3d/object_evidence_ledger.jsonl`, `rejection_overlays/`: P3 accepted/rejected.
- `grid_trace/`: grid M3 được tạo bởi pipeline.
- `replay/snapshot.json`, `delta_log.json`, `spatial_map.png`, `scene_graph.png`, `trajectory.csv`.
- `semantic/report.vi.md`, `region_labels.json`, `region_object_evidence.html`.
- `evaluation/report.vi.md`, `metrics.json`, `manifest.json`.

Thiếu GT sẽ giữ metric null/unavailable, không đổi thành 0. P3 acceptance và số object/room không phải accuracy. Route là các pose quan sát GT cố định trong Habitat, chưa phải robot tự đi, navigation policy hay SLAM. Xem thêm [RUN_AND_EVALUATE.vi.md](RUN_AND_EVALUATE.vi.md).
