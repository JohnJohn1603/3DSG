# HM3D 00802: nghiệm thu shared workflow C2 → D1/D2

Ngày chạy: 2026-10-17. Đây là development pilot trên HM3D minival `00802-wcojb4TFT35`, route `main`; không phải holdout, đánh giá chất lượng, realtime hay bằng chứng mọi scene HM3D đều chạy được.

## Kết quả

- **Menu/registry/adapter:** đã có từ checkpoint implementation trước lượt pilot. Registry `configs/launcher/scenes.json`, resolver `integration_v2/scene_registry.py`, terminal picker và HM3D adapter cùng dùng một orchestration. Targeted suite gần nhất của checkpoint đó: 59 tests passed.
- **C2:** hoàn tất trên route 29 frame, 160×120, HFOV 90°. Capture/P2/P3/OccuSG/features/ConceptGraphs/unified graph đều hoàn tất.
- **D1+D2 simultaneous:** đạt trên lần chạy cuối. D2 được mở ở trạng thái chờ; sau khi browser xác nhận sẵn sàng, D1 bắt đầu. Browser đã render revision 4/29 của frame `hm3d-00802-main-0003` khi producer còn `running`, rồi revision 29/29 khi run `completed`.
- **Scope/semantics:** giữ nguyên `unavailable_elevation_scope` và `unavailable_membership_scope`. Không có ConceptGraphs-object `room_contains` edges. Đây không phải room labeling.
- **Object counts:** C2 tạo 17 hypotheses trước filter và 2 objects sau final processing; D1 kết thúc với 17 online hypotheses, 2 final objects. Đây là workflow output, không phải accuracy evidence. Prefix admission được ghi unavailable; final postprocess là terminal stage riêng.

## Run IDs và số lượt producer

Ba `scene-run` invocations đã được ghi lại; hai invocation C2 hoàn tất đầy đủ, còn invocation đầu dừng ở preflight:

| Workflow | C2 | D1 | Kết quả thực tế |
|---|---|---|---|
| `scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T061655Z_e0390d` | `c2_20261017T061655Z_79bb9b` | chưa chạy | Preflight dừng: 2,050,600,960 B RAM khả dụng < gate 3,221,225,472 B. Không có producer chạy. |
| `scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T062430Z_35cafd` | `c2_20261017T062430Z_d0bcf3` | `d1_20261017T062430Z_4cac9d25` | C2 hoàn tất. D1 launcher thất bại `EPERM` khi tạo process server trước khi mapping producers được khởi chạy. |
| `scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T063106Z_f91798` | `c2_20261017T063106Z_99bc38` | `d1_20261017T063106Z_554073fd` | C2, D1 và simultaneous D2 hoàn tất. |

Tổng thực thi: một C2 preflight-only attempt; **hai C2 full producer invocations**, mỗi lượt gồm capture×1, P2×1, P3×1, OccuSG replay chính×1 cùng fresh-check determinism replay×1, image-feature pass×1, text-feature pass×1 và ConceptGraphs mapping×1; **một D1 producer invocation thành công** với ConceptGraphs×1 và OccuSG×1, mỗi nhánh xử lý 29 frame. D1 orchestration trước đó thất bại trước khi producer được spawn. Không có producer retry thứ hai sau lỗi mapping/OOM.

Việc C2 chạy đủ lần thứ hai là do `scene-run` tạo output/cache mới theo yêu cầu; D1 không chạy lại trên cache cũ sau lỗi bootstrap. Giữ cả ba workflow và mọi logs/artifacts nguyên trạng.

## Tài nguyên và timings

RAM gate không đổi. Read-only probe trước run cuối đo RAM khả dụng trên gate; log preflight ghi 8.1664 GB khả dụng và 325.3 GB disk trống. Snapshot sau khi run kết thúc ghi 8,352,919,552 B khả dụng. Không process nào bị dừng. PID cũ không xác định được trong process namespace lúc preflight; `ss` không đọc được netlink. Lúc nghiệm thu, manifest D1 ghi mapper PIDs 315866/315868 đã exit và server thuộc run PID 311615 còn sống. Browser thực tế vẫn tải được viewer trên port 8766.

C2 `started_utc`→`finished_utc`: 283.69 s. Thời gian stage trong C2 manifest: preflight 17.66 s; capture 16.08 s; P2 11.53 s; P3 1.76 s; OccuSG main + deterministic fresh-check tổng 176.58 s; image features 16.18 s; text features 9.00 s; ConceptGraphs mapping process 16.58 s; unified graph/viewer 1.25 s. Route có 29 pose steps và 26 unique poses. RSS có trong manifest: text feature process 1,487.71 MiB; C2 ConceptGraphs mapping 469.40 MiB; OccuSG Python replay 545,832 KiB. Capture/P2/P3 không xuất per-process RSS trong manifest, nên không ước đoán các giá trị đó.

D1 runtime tổng 243.98 s, gồm 129.67 s chờ explicit browser-ready/start gate và 114.13 s từ lúc producer start tới khi hai producer kết thúc. ConceptGraphs child report: 104.98 s, peak RSS 459.33 MiB. OccuSG child report: 110.66 s, `ru_maxrss` 535,876 KiB. Coordinator join: median 118.1 ms, max 280.1 ms/revision. Trong manifest, object processing median/max 19.1/166.0 ms mỗi frame; room processing median/max 3,171.4/7,178.1 ms. Đây là processing/barrier/coordinator metrics, không phải realtime throughput.

GUI ghi receive/render sample tại revision 4 là 110.4/205.5 ms; ở revision cuối là 162.0/181.4 ms. Browser render timing không được runner thu thập đồng bộ cho mọi frame; không gọi các sample này là end-to-end display latency.

## Integrity và provenance

- C2 manifest khai báo 767 output hashes: **767/767 tồn tại và khớp**. Workflow manifest trỏ đúng SHA-256 của C2 và D1 manifests.
- D1 `immutable_output_sha256`: **795/795 file tồn tại và khớp**, bao gồm graph, geometry, branch events/acks, viewer, object/room outputs. Detached receipt khớp SHA-256 run manifest. Hash checkpoint: `2026-10-17T06:39:56.481743Z`; object và room producers đã exit, run-owned viewer PID 311615 còn sống. Viewer server log được đánh dấu mutable sau checkpoint và không được coi là immutable output.
- Có một metadata discrepancy được giữ rõ: `viewer.assets_sha256` lồng trong D1 manifest có 2 hash cũ cho `live_head.json` và `live_index.json` (bootstrap state). Hai file này được cập nhật sang terminal state khi producer hoàn tất, vì vậy hash lồng không khớp bytes cuối. Final bytes của cả hai **khớp** `immutable_output_sha256`; browser xác minh live head/index và render được revision cuối. Đây là stale duplicate hash metadata, không phải graph/geometry hash mismatch. Không sửa/ghi đè lịch sử run trong lượt này; cần sửa cách ghi hash field ở lần implementation metadata riêng nếu muốn manifest hoàn toàn self-consistent.
- Text feature log có cảnh báo OpenCLIP factory `pretrained=None`; code sau đó nạp checkpoint local bằng `load_state_dict(..., strict=True)`. Manifest sidecar ghi `model_downloaded=false`, checkpoint SHA-256 `e6d1bd7789aa45192b3bf90570a789b478bae1b74ebcce7eddd908e83a2b7c31`, 25 observation vectors, 11 class strings; không tải checkpoint mới.
- Effective config SHA-256: `c32b21712930b99b98a6a0925ac2c8fc4a748e3bcd2b422c8a2a83b24196be74`; route SHA-256: `3ea1a21f6870e1ef292ec2774efa551b9ede6128b72438d20c2b5568db46b691`; scene asset SHA-256: `075e243b372458df4055a55e27c93885ba44ae8c36b4bc4446e0393c60c14540`.

## GUI evidence

Trong tab browser thật trên `127.0.0.1:8766`, trước khi start hiển thị `waiting_for_first_revision` và 0 revisions. Sau explicit start, khi D1 còn chạy, accessibility state hiển thị `4/4`, frame `hm3d-00802-main-0003`, status `running`, cùng XYZ renderer. Sau khi kết thúc, tab hiển thị `29/29`, frame `hm3d-00802-main-0028`, status `completed`, XYZ cloud/trajectory và panel room XY; 17 online hypotheses, 5 room nodes, membership/room semantics unavailable, prefix admission unavailable, final count 2. `room_contains` phân loại riêng: navigation 133, agent 29, OccuSG native object 9, ConceptGraphs object 0. Không có final membership nào được đưa vào prefix.

## Commands

Chạy lại workflow theo cùng registry/config (sinh run ID mới):

```bash
./run_scene.sh scene-run --dataset hm3d_minival_v0.2 --scene 00802-wcojb4TFT35 --route main --port 8766
```

Kết quả đã nghiệm thu:

```bash
./run_scene.sh results c2_20261017T063106Z_99bc38
./run_scene.sh results d1_20261017T063106Z_554073fd
./run_scene.sh open d1_20261017T063106Z_554073fd live --port 8766
```

`results` hỗ trợ C2 và D1 run IDs; workflow manifest được đọc trực tiếp tại `artifacts/scene_workflows/scene_hm3d_minival_v0-2_00802-wcojb4tft35_20261017T063106Z_f91798/run_manifest.json`. Viewer URL đã mở và kiểm tra: `http://127.0.0.1:8766/artifacts/live_processing_runs/d1_20261017T063106Z_554073fd/viewer/index_d2_live.html?live=1`.

## Gate status và bước kế tiếp

- **Registry/menu/adapter:** đạt riêng ở mức config-driven implementation và tests.
- **HM3D 00802 C2:** đạt cho scene/route này.
- **D1+D2 simultaneous:** đạt; D2 nhận/render revision thật trước khi producers hoàn tất.
- **Spatial scope, object→room membership, room semantics:** pending/unavailable; không bị mở bởi pilot.
- **Scene portability ngoài 00802:** chưa được chứng minh bởi run này.

Infrastructure v1 được đóng ở phạm vi đã nghiệm thu; không chạy thêm scene hoặc mở S1–S3. Bước tiếp theo là lập kế hoạch đánh giá có annotation/evidence phù hợp cho object identity/semantics, membership scope và room labels, giữ riêng workflow gates với quality gates.
