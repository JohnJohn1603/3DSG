# Sổ tay ngắn — chọn map, chạy và xem kết quả

Đối chiếu với `scripts/scene_launcher.py` ngày 06-10-2026. Route `main_320` đã được chạy end-to-end trên HM3D 00802; hướng dẫn bên dưới dùng launcher hiện tại.

## 1. Mở launcher

Nhập trong Terminal:

```bash
cd /home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG
./run_scene.sh
```

Khi thấy `scene>` mới nhập các lệnh `/...` bên dưới. Không gõ chữ `scene>`.

## 2. Chuỗi lệnh cần nhớ

Nhập từng dòng, chờ `/check` hoàn tất và chỉ chạy tiếp khi tổng thể PASS:

```text
/maps
/use hm3d_00802
/info
/routes hm3d_00802
/check hm3d_00802 --route smoke
/run hm3d_00802 --route smoke
```

Trong lúc chạy:

```text
/status
/logs --tail 30
```

Sau khi hoàn tất:

```text
/results
/open report
/open map
/open objects
```

Smoke hoạt động rồi, chạy route dài hơn:

```text
/check hm3d_00802 --route main
/run hm3d_00802 --route main
```

### So sánh cùng route ở 160×120 và 320×240

`main` vẫn là route mặc định 160×120. `main_320` dùng cùng 29 pose, timestamp, HFOV và extrinsics, chỉ đổi resolution; mỗi lượt tạo run riêng. Chạy:

```text
/use hm3d_00802
/routes hm3d_00802
/check hm3d_00802 --route main_320
/run hm3d_00802 --route main_320
/status
/results
```

Sau khi run hoàn tất, lấy ID ở `/results` hoặc `/runs`, rồi mở so sánh và các kết quả:

```text
/open RUN_ID comparison
/open RUN_ID report
/open RUN_ID map
/open RUN_ID objects
```

`comparison` mở report đối chiếu; `report` cũng ưu tiên report so sánh nếu run có comparison, nếu không thì mở evaluation report. Ảnh hai resolution nằm trong `artifacts/runs/RUN_ID/comparison_to_baseline/resolution_side_by_side.png`. Ảnh baseline 160×120 được phóng nearest-neighbor chỉ để xem cạnh ảnh 320×240; raw capture không bị upscale. Tăng resolution ở cùng P3 stride làm tăng mật độ điểm ảnh/depth, nhưng thêm detections hoặc ObjectNodes không tự chứng minh accuracy tốt hơn.

### Capture 600×340

`main_600` dùng cùng 29 pose của `main`, giữ HFOV, depth limits và extrinsics, chỉ đổi resolution thành 600×340. Đây là cấu hình capture riêng; không tự chạy báo cáo so sánh với run khác. Dùng:

```text
/use hm3d_00802
/check hm3d_00802 --route main_600
/run hm3d_00802 --route main_600
```

Sau khi chạy xong, lấy `RUN_ID` từ kết quả hoặc `/runs`, rồi tự tạo/xem evaluation:

```text
/evaluate RUN_ID
/evaluate-objects RUN_ID --check
/evaluate-objects RUN_ID
/object-results RUN_ID
```

Mỗi `/run` tạo kết quả mới; không tiếp tục run trước. Pipeline tự chạy capture → detection 2D → lifting 3D → native replay/grid → semantic → evaluation, sau preflight.

### Capture dày `main_dense`

`main_dense` thêm pose quan sát trung gian trên đường navmesh giữa 29 pose gốc của `main_600`, giữ nguyên thứ tự và sensor 600×340. Có 280 pose; Habitat render RGB-D mới tại từng pose. Đây là route pose cố định dùng GT pose và navmesh shortest path để dựng đường quan sát, không phải robot vật lý hay benchmark realtime.

```text
/use hm3d_00802
/check hm3d_00802 --route main_dense
/run hm3d_00802 --route main_dense
```

Route mặc định vẫn là `main`. Khi chạy, terminal hiển thị tiến độ frame của capture, detection 2D, P3 và native replay (cả fresh deterministic check); mọi output đi vào một `RUN_ID` mới.

**Chú ý:** `/use` chọn map, không đổi run đang xem. `/status`, `/results`, `/open` không có run ID dùng run của phiên nếu có, nếu không dùng run gần nhất do launcher quản lý. Vì vậy khi chuyển map mà chưa chạy, chúng có thể vẫn hiện kết quả map trước. Muốn chắc chắn, dùng `/runs` rồi truyền run ID cụ thể.

## 3. Map và route hiện đăng ký

| Alias | Route | Số pose theo tài liệu |
|---|---|---:|
| `hm3d_00802` | `smoke` | 10 |
| `hm3d_00802` | `main` — mặc định | 29 |
| `hm3d_00802` | `main_320` — so sánh resolution 320×240 | 29 |
| `hm3d_00802` | `main_600` — capture 600×340 | 29 |
| `hm3d_00802` | `main_dense` — route dày 600×340 | 280 |
| `replica_apt0` | `dev_long` — mặc định | 24 |
| `replica_apt0` | `dev` | 16 |
| `replica_apt0` | `reobserve` | 5 |
| `replica_apt0` | `hires` | 16 |

Route là các pose quan sát GT của camera/agent, chưa phải robot tự tìm đường hay chạy bánh xe. Map có trên ổ đĩa chưa chắc đã được đăng ký và có route hợp lệ.

Ví dụ chuyển sang ReplicaCAD:

```text
/use replica_apt0
/check replica_apt0 --route dev_long
/run replica_apt0 --route dev_long
```

## 4. Bảng lệnh tra cứu

`RUN_ID` là chỗ giữ chỗ: thay bằng ID thật từ `/runs`, không nhập đường dẫn thư mục vào vị trí này.

| Lệnh trong launcher | Công dụng |
|---|---|
| `/help` hoặc `/all` | Xem lệnh; không chạy tất cả map |
| `/help register` | Cú pháp đăng ký |
| `/maps` | Xem scene đã đăng ký |
| `/use hm3d_00802` | Chọn scene trong phiên |
| `/info hm3d_00802` | Xem asset/config/profile |
| `/routes hm3d_00802` | Xem route của scene |
| `/check hm3d_00802 --route main_320` | Preflight route 320×240 và đối chiếu baseline main 160×120 |
| `/run hm3d_00802 --route main_320` | Chạy capture và pipeline ở 320×240, output run mới |
| `/check hm3d_00802 --route main_600` | Preflight route 600×340 |
| `/run hm3d_00802 --route main_600` | Chạy capture và pipeline ở 600×340, output run mới |
| `/check hm3d_00802 --route main_dense` | Preflight route dày 600×340, 280 pose |
| `/run hm3d_00802 --route main_dense` | Chạy route dày, output run mới |
| `/check hm3d_00802 --route smoke` | Kiểm tra môi trường, asset và route |
| `/run hm3d_00802 --route smoke --dry-run` | Xem kế hoạch, chưa chạy simulator |
| `/run hm3d_00802 --route smoke` | Chạy đầy đủ |
| `/runs` | Xem các run launcher quản lý |
| `/status RUN_ID` | Trạng thái một run cụ thể |
| `/logs RUN_ID --stage native_replay --tail 50` | Xem cuối log stage |
| `/results RUN_ID` | Tóm tắt kết quả |
| `/open RUN_ID report` | Mở báo cáo |
| `/open RUN_ID comparison` | Mở báo cáo so sánh resolution và ảnh đặt cạnh nhau |
| `/open RUN_ID map` | Mở bản đồ |
| `/open RUN_ID objects` | Mở evidence viewer hoặc ảnh detection |
| `/open RUN_ID labels` | Mở dữ liệu nhãn |
| `/open RUN_ID folder` | Mở thư mục kết quả |
| `/evaluate RUN_ID` | Đánh giá lại output đã có; tạo evaluation mới |
| `/cancel` | Dừng run đang chạy do phiên này quản lý |
| `/exit` | Thoát; nếu còn run thì phải chờ hoặc cancel trước |

`run` đã có evaluation: không cần gọi `/evaluate` ngay sau mỗi run thành công. Sau evaluate lại, xem đường dẫn evaluation mới được in ra. Với run `main_320`, `/open report` ưu tiên report so sánh; dùng đường dẫn trong `/results` nếu cần mở evaluation report riêng.

### Chấm Object GT semantic cho HM3D

Chấm trực tiếp một run đã hoàn tất; thay `RUN_ID` bằng ID cụ thể, không dùng “latest”:

`RUN_ID` là mã định danh của **một lần chạy pipeline** (mỗi `/run` tạo một ID mới), không phải tên map hay tên route. Lấy ID ở dòng kết quả sau khi chạy `/run`, hoặc gõ `/runs` rồi copy cột đầu tiên. Bạn cũng có thể thấy đúng ID là tên thư mục trong `artifacts/runs/`. Ví dụ run 320×240 đã có là `hm3d_00802_20261006T091921.402660Z_0220b9`.

```text
/evaluate-objects hm3d_00802_20261006T091921.402660Z_0220b9 --check
/evaluate-objects hm3d_00802_20261006T091921.402660Z_0220b9
/open hm3d_00802_20261006T091921.402660Z_0220b9 object-evaluation
/open hm3d_00802_20261006T091921.402660Z_0220b9 object-review
```

Trong prompt tương tác, nhập các lệnh trên với dấu `/` như ví dụ. Trong terminal, dùng `./run_scene.sh evaluate-objects hm3d_00802_20261006T091921.402660Z_0220b9 --check`, rồi bỏ `--check` để chạy; CLI `open` tương ứng cũng dùng cùng RUN_ID và loại view. `--check` chỉ kiểm tra, không render/ghi output. Trong interactive, evaluation chạy nền do launcher quản lý; theo dõi bằng `/status RUN_ID`, `/logs RUN_ID --stage object-evaluation --tail 80`, dừng bằng `/cancel RUN_ID`. Mỗi lượt có `object_evaluations/EVAL_ID/` riêng. Review HTML đặt RGB+prediction cạnh RGB+semantic-GT; prediction màu xanh, GT màu hồng/tím.

Lưu ý: ở dấu nhắc `scene>`, không nhập `./run_scene.sh`; đó là prompt lệnh của launcher, hãy dùng `/object-results ...`. Màu chữ tự bật trong terminal tương tác có hỗ trợ ANSI và tự tắt khi chuyển hướng output; đặt biến `NO_COLOR` để tắt màu.

Khi evaluation đã xong, lệnh này **chỉ đọc output đã có** — không chấm lại:

```bash
./run_scene.sh object-results hm3d_00802_20261006T091921.402660Z_0220b9
./run_scene.sh object-results hm3d_00802_20261006T091921.402660Z_0220b9 --frames
./run_scene.sh object-results hm3d_00802_20261006T091921.402660Z_0220b9 --frame 12
```

Interactive tương ứng: `/object-results hm3d_00802_20261006T091921.402660Z_0220b9`, thêm `--frames` hoặc `--frame 12` để xem chi tiết. Frame index bắt đầu từ 0. Mặc định chọn evaluation completed hợp lệ mới nhất; thêm `--eval-id EVAL_ID` để xem bản cụ thể. `--json` xuất JSON cho automation; không có checksum trong bản terminal mặc định. Mỗi detection hiện `matched`, `ignored` hoặc `false_positive`; với evaluation cũ, ignored-vs-FP được phân loại lại chỉ từ mask đã lưu, semantic GT cache đã căn chỉnh và protocol evaluation — không chạy detector, matching hay predictor lại.

Category map tại `configs/evaluation/hm3d_00802_object_category_mapping_v1.json` khởi đầu ở trạng thái provisional. Hãy chỉ chuyển từng category sang `confirmed` sau khi tự kiểm tra equivalence giữa nhãn HM3D và class detector, đồng thời ghi reviewer/date/provenance. Chạy lại đúng lệnh sẽ tạo evaluation ID mới và tái sử dụng semantic GT cache nếu capture/assets/renderer/code không đổi. Không sửa output của run cũ. Mở `report.vi.md`, `metrics.json`, `metrics.csv`, `association_audit.json`; class-aware metric sẽ unavailable cho tới khi có mapping confirmed. Association ở run hiện tại chỉ có lineage đầu/cuối, nên suppression-vs-match và đầy đủ identity metrics không được suy diễn.

Nếu không có GUI, `/open` có thể chỉ in đường dẫn; không có nghĩa pipeline thất bại. Stage chưa chạy xong thì artifact tương ứng có thể chưa tồn tại.

## 5. Kết quả ở đâu? Mở gì trước?

Mỗi run nằm tại:

```text
/home/kiettran05/Desktop/test_Room_segmentation/Rebuild_3DScene_OccuSG/artifacts/runs/RUN_ID/
```

| Đường dẫn bên trong run | Ý nghĩa |
|---|---|
| `launcher_manifest.json` | Trạng thái toàn run, stage, môi trường và provenance |
| `evaluation/report.vi.md` | Đọc đầu tiên: báo cáo metrics và giới hạn |
| `comparison_to_baseline/report.vi.md` | Báo cáo 160×120 so với 320×240; có trong run `main_320` |
| `comparison_to_baseline/resolution_side_by_side.png` | Cùng frame ở hai resolution để xem trực quan |
| `evaluation/metrics.csv` | Số liệu dạng bảng |
| `evaluation/metrics.json` | Metric chi tiết, mẫu số, scope, lý do null |
| `replay/spatial_map.png` | Bản đồ không gian |
| `replay/trajectory_rooms.png` | Quỹ đạo/room footprints nếu runner xuất |
| `semantic/region_object_evidence.html` | Object/ảnh nào hỗ trợ nhãn vùng |
| `semantic/region_labels.json` | Nhãn, unknown/ambiguous và evidence |
| `detection_2d/overlays/` | Ảnh RGB với prediction |
| `detection_3d/object_evidence_ledger.jsonl` | Chấp nhận/loại object 3D và lý do |
| `replay/snapshot.json`, `delta_log.json` | Graph và lịch sử thay đổi |
| `replay/trajectory.csv` | Pose/quỹ đạo |
| `capture/` | RGB-D, valid mask, pose metadata |
| `grid_trace/` | Occupancy và trace theo thời gian |
| `logs/` | Log các stage |

Không có GT phù hợp thì accuracy là null/unavailable. Số room/object, tỷ lệ P3 accept và tỷ lệ có nhãn không phải accuracy. GT chỉ nằm ở evaluator, không phải input phân phòng/labeler.

## 6. Có đăng ký map B được không? Có, bằng config

**Đã có `/register --config ...` trong CLI và interactive.** Không có lệnh chỉ `/register map_B` để tự tìm mesh và tạo route.

Trước khi đăng ký cần chuẩn bị:

1. Dataset/asset B đã tải, dùng loader HM3D hoặc ReplicaCAD hiện hỗ trợ.
2. Route/sensor JSON riêng cho B với pose đúng scene, navmesh/dataset config đúng.
3. Evaluation profile của B, tham chiếu đúng asset/route và GT scope. Không tái sử dụng GT apt_0 cho B.
4. Scene-entry JSON liên kết các file trên. Không sửa baseline A.

Template có sẵn trong project:

```text
configs/launcher/templates/hm3d_scene_entry.template.json
configs/launcher/templates/replica_cad_scene_entry.template.json
```

Ví dụ nội dung `configs/launcher/map_B.json` — đây là mẫu cần tạo và điền đường dẫn thật, chưa phải map đã có:

```json
{
  "alias": "map_B",
  "evaluation_profile": "configs/evaluation/map_B_scene_eval_v1.json",
  "routes": {
    "smoke": "configs/habitat/map_B_smoke.json",
    "main": "configs/habitat/map_B_main_route.json"
  },
  "default_route": "smoke"
}
```

Nếu mới có route smoke, chỉ khai báo smoke; không khai báo file main chưa tồn tại. Scene-entry không chứa mesh trực tiếp; asset được tham chiếu qua evaluation profile/route theo schema dự án. Alias phải duy nhất.

Sau khi các file trên đã chuẩn bị, nhập trong `scene>`:

```text
/register --config configs/launcher/map_B.json
/maps
/use map_B
/info
/routes map_B
/check map_B --route smoke
/run map_B --route smoke --dry-run
/run map_B --route smoke
/status
```

Chỉ chạy thật khi check PASS. Đăng ký chỉ cập nhật registry `configs/launcher/scenes.json`; không tự capture, không tạo route, không chứng minh map runnable. `/status` sau register/use nhưng trước run chưa phải trạng thái của một run B.

Với map B mới, phần cần AI hỗ trợ ban đầu là **tạo route + evaluation profile phù hợp**. Sau khi đăng ký, bạn dùng các lệnh ngắn giống map A.

## 7. Dùng Terminal trực tiếp thay vì chế độ tương tác

Trong workspace root:

```bash
./run_scene.sh maps
./run_scene.sh info hm3d_00802
./run_scene.sh check hm3d_00802 --route smoke
./run_scene.sh run hm3d_00802 --route smoke
./run_scene.sh check hm3d_00802 --route main_320
./run_scene.sh run hm3d_00802 --route main_320
./run_scene.sh runs
```

Với run ID thật:

```bash
./run_scene.sh status RUN_ID
./run_scene.sh results RUN_ID
./run_scene.sh open RUN_ID report
./run_scene.sh open RUN_ID comparison
```

Đăng ký trực tiếp:

```bash
./run_scene.sh register --config configs/launcher/map_B.json
```

Không trông đợi `use` ở một tiến trình CLI giữ lựa chọn cho tiến trình CLI tiếp theo; truyền alias trong lệnh `run/check` hoặc dùng interactive.

## 8. Khi bị lỗi

- `check` FAIL: đọc lý do, chưa chạy; thiếu dependency không phải lỗi map segmentation.
- Stage failed: `/status` rồi `/logs`; giữ run lỗi để chẩn đoán, không ghi đè.
- Không thấy map: `/maps`; kiểm tra đã register chưa, không chỉ kiểm tra mesh trên ổ.
- Muốn dừng: `/cancel`; chỉ hủy run của phiên launcher hiện tại.
- Mở lại launcher sau crash: kiểm tra interrupted, không mặc định run tự resume.
- Không xóa `artifacts/habitat_data`, models, GT, native builds hoặc toàn bộ artifacts.

**Cập nhật môi trường:** config lúc soạn sổ tay đã dùng `.venv-perception/bin/python` cho project, thay vì `/usr/bin/python3` của hướng dẫn cũ. Chưa chạy full preflight trong lượt này; `/check` trên máy bạn là kiểm tra cần thực hiện, không kết luận lỗi dependency cũ vẫn còn.

## Tóm tắt một dòng

**Map đã đăng ký:** `/maps → /use → /check → /run → /status → /results → /open`.

**Map mới:** chuẩn bị asset + route + evaluation profile + entry → `/register --config ...` → quy trình trên.
